"""Collector rules with fixture data: python3 -m unittest discover -s tests"""
import json
import os
import sqlite3
import time
import urllib.parse
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _kitenv  # noqa: E402,F401  (scratch config with an example owner, before agentboard reads any)
from _kitenv import AGENTS, OWNER, OWNER_NAME  # noqa: E402
from agentboard import actions, classify, collect, config, gmailread, sources  # noqa: E402

NOW = 1_800_000_000.0


def agent(**kw):
    a = {"id": "11111111-2222-3333-4444-555555555555", "cli": "claude", "live": True, "raw_status": "idle",
         "last_activity": NOW - 60, "last_turn_end": NOW - 60, "mail": {}, "name": "a"}
    a.update(kw)
    return a


class ClassifyTest(unittest.TestCase):
    def state(self, **kw):
        return classify.classify(agent(**kw), NOW)[0]

    def test_busy_wins_over_everything_live(self):
        self.assertEqual(self.state(raw_status="busy", mail={"unanswered": True, "last_out": NOW - 10}), "busy")

    def test_terminal_prompt_is_blocked(self):
        st, why = classify.classify(agent(raw_status="waiting", waiting_for="permission"), NOW)
        self.assertEqual(st, "blocked")
        self.assertIn("permission", why)

    def test_quota_after_last_turn_is_blocked_and_clears(self):
        self.assertEqual(self.state(quota_ts=NOW - 30, last_turn_end=NOW - 100), "blocked")
        self.assertEqual(self.state(quota_ts=NOW - 300, last_turn_end=NOW - 100), "idle")

    def test_plan_limit_and_api_error_events(self):
        st, why = classify.classify(agent(quota_ts=NOW - 60, last_turn_end=NOW - 61,
                                          limit={"ts": NOW - 60, "kind": "limit-wait", "text": "resets 17:29"}), NOW)
        self.assertEqual(st, "blocked")
        self.assertIn("no other login", why)
        why = classify.classify(agent(quota_ts=NOW - 60, last_turn_end=NOW - 61,
                                      limit={"ts": NOW - 60, "kind": "limit", "text": "five-hour limit"}), NOW)[1]
        self.assertIn("five-hour limit", why)
        self.assertEqual(self.state(error={"ts": NOW - 30, "text": "overloaded"}, last_turn_end=NOW - 40), "blocked")
        # a failover after the limit: moved to a login with headroom, no longer blocked
        st, why = classify.classify(agent(quota_ts=NOW - 60, last_turn_end=NOW - 61, limit={"ts": NOW - 60, "kind": "limit"},
                                          failover={"ts": NOW - 50, "account": "work"}), NOW)
        self.assertEqual(st, "idle")
        self.assertIn("moved to work", why)
        # the newer of limit and API error decides the reason
        why = classify.classify(agent(quota_ts=NOW - 60, last_turn_end=NOW - 61, limit={"ts": NOW - 60, "kind": "limit"},
                                      error={"ts": NOW - 30, "text": "overloaded"}), NOW)[1]
        self.assertIn("API error", why)
        self.assertEqual(self.state(error={"ts": NOW - 300, "text": "overloaded"}, last_turn_end=NOW - 40), "idle")

    def test_blocked_note(self):
        note = {"ts": NOW - 600, "text": "BLOCKED: needs the HF token from Alex"}
        self.assertEqual(self.state(last_note=note, last_turn_end=NOW - 590), "blocked")
        named = {"ts": NOW - 600, "text": "waiting on Alex to approve the spend"}
        self.assertEqual(classify.classify(agent(last_note=named, last_turn_end=NOW - 590), NOW, owner="Alex")[0],
                         "blocked")                         # the owner's configured name counts like "you"
        self.assertEqual(self.state(last_note=named, last_turn_end=NOW - 590), "idle")      # no name configured
        self.assertEqual(self.state(last_note=dict(named, text="needs you to approve"), last_turn_end=NOW - 590),
                         "blocked")
        # it carried on working long after the note: no longer blocked
        self.assertEqual(self.state(last_note=note, last_turn_end=NOW - 600 + 3600), "idle")
        self.assertEqual(self.state(last_note={"ts": NOW - 600, "text": "unblocked the build"}), "idle")

    def test_waiting_on_the_owner(self):
        mail = {"unanswered": True, "last_out": NOW - 3600, "last_out_subject": "Done: results"}
        st, why = classify.classify(agent(mail=mail, last_turn_end=NOW - 3590), NOW)
        self.assertEqual(st, "waiting")
        self.assertIn("Done: results", why)

    def test_mail_then_more_turns_is_not_waiting(self):
        mail = {"unanswered": True, "last_out": NOW - 7200}
        self.assertEqual(self.state(mail=mail, last_turn_end=NOW - 60, last_activity=NOW - 60), "idle")

    def test_idle_stale_offline_and_background_shell(self):
        self.assertEqual(self.state(last_activity=NOW - 3600), "idle")
        self.assertEqual(self.state(last_activity=NOW - classify.STALE_AFTER - 1, last_turn_end=0), "stale")
        st, why = classify.classify(agent(raw_status="shell", mail={"unanswered": True, "last_out": NOW - 10}), NOW)
        self.assertEqual(st, "background")                # only waiting on a background command it started
        self.assertIn("background", why)
        self.assertEqual(self.state(live=False), "offline")
        self.assertNotIn("shell", classify.STATES)

    def test_a_background_wait_that_only_finds_itself_is_idle(self):
        stuck = lambda *targets: {"shells": len(targets), "stuck": [
            {"pid": 100 + i, "pattern": "stage %d" % i, "target": t} for i, t in enumerate(targets)]}
        st, why = classify.classify(agent(raw_status="shell", bg_waits=stuck(False)), NOW)
        self.assertEqual(st, "idle")                      # nothing will ever wake it
        self.assertIn("`stage 0` can never end", why)
        st, why = classify.classify(agent(raw_status="shell", bg_waits=stuck(False, False),
                                          last_activity=NOW - classify.STALE_AFTER - 1), NOW)
        self.assertEqual(st, "stale")
        self.assertIn("2 background waits", why)
        st, why = classify.classify(agent(raw_status="shell", bg_waits=stuck(False, True)), NOW)
        self.assertEqual(st, "background")                # what one of them waits for is still running
        self.assertIn("will not end", why)
        st, why = classify.classify(agent(raw_status="shell", bg_waits=dict(stuck(False), shells=2)), NOW)
        self.assertEqual(st, "background")                # another background command is a real one
        self.assertEqual(self.state(raw_status="shell", bg_waits={"shells": 1, "stuck": []}), "background")

    def test_a_red_star_when_on_work_while_personal_has_room(self):
        order, thr = ["personal", "work"], {"session": 10, "weekly": 5}
        def acc(session=97.0, weekly=98.0, fable=98.0):
            return [{"target": "claude:personal", "windows": [
                {"kind": "session", "name": "5-hour", "remaining_pct": session},
                {"kind": "weekly", "name": "weekly", "remaining_pct": weekly},
                {"kind": "model_weekly", "name": "weekly Fable", "remaining_pct": fable, "model": "fable"}]}]
        note = lambda a, u=None: classify.login_note(a, acc() if u is None else u, order, thr, NOW)
        moved = agent(account="work", model="claude-fable-5-1", raw_status="shell",
                      failover={"account": "work", "ts": NOW - 7200})
        why = note(moved)
        self.assertIn("Running on work while personal has room for claude-fable-5-1", why)
        self.assertIn("weekly Fable 98% left", why)
        self.assertIn("moved it here 2.0h ago", why)
        self.assertIn("isn't idle yet", why)
        started = agent(account="work", model="claude-opus-5-5")
        self.assertIn("didn't move it there", note(started))
        self.assertNotIn("Fable", note(started))                       # Opus doesn't use the Fable window
        self.assertIsNone(note(moved, acc(fable=3.0)))                 # personal's Fable is under 5%
        self.assertIsNotNone(note(started, acc(fable=0.0)))            # ...which an Opus agent doesn't need
        self.assertIsNone(note(started, acc(session=9.0)))             # under the 10% the mailroom routes at
        self.assertIsNone(note(agent(account="work"), acc(fable=0.0)))    # model unknown: every window must have room
        self.assertIsNone(note(agent(account="personal")))
        self.assertIsNone(note(agent(account="work", live=False)))
        self.assertIsNone(note(agent(account="work", cli="codex")))
        self.assertIsNone(note(started, []))                            # no usage reading: no star
        self.assertIsNone(note(started, [dict(acc()[0], blocked=["blocked"])]))
        upper = [{"target": "claude:personal", "windows": [
            {"kind": "session", "remaining_pct": 90.0}, {"kind": None, "remaining_pct": 90.0},
            {"kind": "model_weekly", "name": "weekly Fable", "remaining_pct": 2.0, "model": "Fable"}]}]
        self.assertIsNone(note(moved, upper))                          # "Fable" matches claude-fable-5-1

    def test_claude_routing_reads_the_mailroom_order(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps({"usage": {"claude_accounts": {"personal": {}, "work": {}},
                                                 "order": ["claude:work", "codex", "claude:personal", "claude:x"],
                                                 "min_remaining": {"session": 20}}}))
            self.assertEqual(sources.claude_routing(cfg), (["work", "personal"], {"session": 20, "weekly": 5}))
            self.assertEqual(sources.claude_routing(Path(d) / "none.json"), (["default"], {"session": 10, "weekly": 5}))

    def test_stranded_reply(self):
        self.assertTrue(classify.stranded(agent(live=False, last_activity=NOW - 600, mail={"last_in": NOW - 60})))
        self.assertFalse(classify.stranded(agent(live=True, last_activity=NOW - 600, mail={"last_in": NOW - 60})))
        self.assertFalse(classify.stranded(agent(live=False, last_activity=NOW - 60, mail={"last_in": NOW - 600})))

    def test_offline_reasons(self):
        why = classify.classify(agent(live=False, last_activity=NOW - 7200, mail={"last_in": NOW - 60}), NOW)[1]
        self.assertIn("stranded", why)
        why = classify.classify(agent(live=False, closed=True), NOW)[1]
        self.assertEqual(why, "closed")

    def test_freshness_fades_idle_only(self):
        self.assertEqual(classify.freshness("busy", 99999), 1.0)
        self.assertGreater(classify.freshness("idle", 60), classify.freshness("idle", 5 * 3600))
        self.assertGreaterEqual(classify.freshness("idle", 10 ** 6), 0.45)


class ReadInGmailTest(unittest.TestCase):
    def test_read_mail_stops_waiting_and_unread_restores_it(self):
        mail = {"unanswered": True, "last_out": NOW - 3600, "last_out_subject": "Done"}
        a = agent(mail=dict(mail, read=True), last_turn_end=NOW - 3590)
        st, why = classify.classify(a, NOW)
        self.assertEqual(st, "idle")
        self.assertIn("read its mail", why)
        self.assertEqual(classify.classify(agent(mail=dict(mail, read=False), last_turn_end=NOW - 3590), NOW)[0], "waiting")
        self.assertEqual(classify.classify(agent(mail=dict(mail, read=None), last_turn_end=NOW - 3590), NOW)[0], "waiting")

    def fake_gmail(self, me=OWNER):
        calls = []
        box = {"g1": "g1@mail.gmail.com", "g2": "g2@mail.gmail.com"}     # the owner's mailbox: Gmail id -> Message-ID
        self.unread = {"g2"}

        def http(method, url, data=None, headers=None):
            calls.append((method, url, headers))
            if url == gmailread.TOKEN_URL:
                return {"access_token": "AT-secret", "expires_in": 3600}
            if url.endswith("/profile"):
                return {"emailAddress": me}
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
            if q.startswith("rfc822msgid:"):
                want = q.split(":", 1)[1]
                return {"messages": [{"id": g, "threadId": "t-" + g} for g, m in box.items() if m == want]}
            return {"messages": [{"id": g} for g in sorted(self.unread)]}
        d = tempfile.mkdtemp()
        creds = Path(d) / "credentials.json"
        creds.write_text(json.dumps({"type": "authorized_user", "client_id": "c", "client_secret": "s", "refresh_token": "r"}))
        return gmailread.GmailRead(AGENTS, [OWNER], creds_path=creds, http=http), calls

    def test_gmail_read_state(self):
        g, calls = self.fake_gmail()
        for m in ("<G1@mail.gmail.com>", "g2@MAIL.gmail.com", "<never-delivered@x>"):
            self.assertIsNone(g.is_read(m))                             # first ask: unknown, now wanted
        g.refresh()
        self.assertTrue(g.status["ok"], g.status)
        self.assertIs(g.is_read("<G1@mail.gmail.com>"), True)
        self.assertEqual(g.thread_of("<G1@mail.gmail.com>"), "t-g1")              # for a direct Gmail link
        self.assertIs(g.is_read("g2@MAIL.gmail.com"), False)
        self.assertIsNone(g.is_read("<never-delivered@x>"))             # not in the mailbox yet: not "read"
        lookups = len([u for _, u, _ in calls if "rfc822msgid" in u])
        self.unread = {"g1"}                                             # g2 read, g1 marked unread again
        g.refresh()
        self.assertIs(g.is_read("g2@mail.gmail.com"), True)
        self.assertIs(g.is_read("g1@mail.gmail.com"), False)
        self.assertEqual(len([u for _, u, _ in calls if "rfc822msgid" in u]), lookups + 1)   # only the undelivered one again
        self.assertTrue(all("AT-secret" not in u for _, u, _ in calls))                     # token never in a URL
        self.assertTrue(any("from%3Aagents%40example.com" in u for _, u, _ in calls))

    def test_refuses_someone_elses_login_and_degrades(self):
        g, _ = self.fake_gmail(me=AGENTS)
        g.refresh()
        self.assertFalse(g.status["ok"])
        self.assertIn("not an owner address", g.status["error"])
        self.assertIsNone(g.is_read("<G1@mail.gmail.com>"))
        missing = gmailread.GmailRead("a@b", [OWNER], creds_path="/nonexistent/creds.json")
        missing.refresh()
        self.assertFalse(missing.status["ok"])


class MailStateTest(unittest.TestCase):
    def test_last_word_decides(self):
        sent = [{"sent_at": NOW - 500, "subject": "Results [#abcd1234]", "thread_id": "t1"}]
        self.assertTrue(classify.mail_state(sent, [], NOW)["unanswered"])
        reply = [{"received_at": NOW - 100, "subject": "Re: Results", "thread_id": "t1", "action": "delivered"}]
        m = classify.mail_state(sent, reply, NOW)
        self.assertFalse(m["unanswered"])
        self.assertEqual(m["threads"][0]["subject"], "Results")   # routing tag stripped
        # a reply in an older thread does not answer a newer mail
        sent2 = sent + [{"sent_at": NOW - 50, "subject": "Another", "thread_id": "t2"}]
        self.assertTrue(classify.mail_state(sent2, reply, NOW)["unanswered"])

    def test_old_mail_ignored_and_held_counted(self):
        old = [{"sent_at": NOW - 8 * 86400, "subject": "x", "thread_id": "t"}]
        self.assertFalse(classify.mail_state(old, [], NOW)["unanswered"])
        held = [{"received_at": NOW - 10, "subject": "Re", "thread_id": "t", "action": "held"}]
        self.assertEqual(classify.mail_state([], held, NOW)["held"], 1)

    def test_keep_offline(self):
        m0 = classify.mail_state([], [], NOW)
        self.assertFalse(classify.keep_offline({}, m0, NOW - 60, NOW))                     # a probe/subagent
        self.assertTrue(classify.keep_offline({"notes": [1]}, m0, NOW - 3600, NOW))
        self.assertFalse(classify.keep_offline({"notes": [1]}, m0, NOW - 13 * 3600, NOW))
        self.assertFalse(classify.keep_offline({"notes": [1], "closed": True}, m0, NOW - 7200, NOW))
        replied = {"last_in": NOW - 600, "unanswered": False}
        self.assertTrue(classify.keep_offline({}, replied, NOW - 30 * 3600, NOW))


class EventOrderTest(unittest.TestCase):
    def test_out_of_order_lines_keep_the_newest(self):
        sid = "s" * 36
        evs = [{"ts": NOW - 10, "event": "limit-wait", "session_id": sid, "text": "new"},
               {"ts": NOW - 20, "event": "quota", "session_id": sid, "text": "old"},          # landed late
               {"ts": NOW - 5, "event": "api-error", "session_id": sid, "text": "e2"},
               {"ts": NOW - 8, "event": "api-error", "session_id": sid, "text": "e1"}]
        a = collect.aggregate_events(evs, NOW)[sid]
        self.assertEqual((a["quota_ts"], a["limit"]["kind"], a["limit"]["text"]), (NOW - 10, "limit-wait", "new"))
        self.assertEqual(a["error"]["text"], "e2")


class NamesTest(unittest.TestCase):
    def test_display_name_fallbacks(self):
        self.assertEqual(classify.display_name({"name": "e6-runner"}), "e6-runner")
        self.assertEqual(classify.display_name({"repo": "proj", "branch": "fix-x"}), "proj@fix-x")
        self.assertEqual(classify.display_name({"repo": "proj", "branch": "main"}), "proj")
        self.assertEqual(classify.display_name({"title": "You were started by agent-spawn on...", "cwd": "/x/admin"}), "admin")
        self.assertEqual(classify.display_name({"title": "Inspect file contents", "cwd": "/x/y"}), "Inspect file contents")

    def test_disambiguate(self):
        rows = [{"display": "admin", "id": "0190bbbb-aaaa"}, {"display": "admin", "id": "0190bbbb-bbbb"}, {"display": "x", "id": "1"}]
        classify.disambiguate(rows)
        self.assertEqual([r["display"] for r in rows], ["admin · aaaa", "admin · bbbb", "x"])


class GitStageTest(unittest.TestCase):
    base = {"branch": "feat", "dirty": 0, "untracked": 0, "upstream": "origin/feat", "ahead": 0, "behind": 0,
            "not_in_default": 3, "oid": "abc"}

    def stage(self, pr=None, **kw):
        return classify.git_stage(dict(self.base, **kw), pr)

    def test_pipeline(self):
        self.assertEqual(self.stage(dirty=2, untracked=1)["stage"], "uncommitted")
        self.assertEqual(self.stage(dirty=2, untracked=1)["chips"], ["±3", "no PR"])
        self.assertTrue(self.stage(untracked=4)["summary"].startswith("4 untracked ·"))
        self.assertEqual(self.stage(ahead=2)["stage"], "unpushed")
        never_pushed = self.stage(upstream="", ahead=None)
        self.assertEqual((never_pushed["stage"], never_pushed["chips"]), ("unpushed", ["↑3"]))
        self.assertEqual(self.stage()["stage"], "pushed")
        self.assertEqual(self.stage()["chips"], ["no PR"])
        pr = {"number": 14, "state": "OPEN", "headRefOid": "abc", "url": "u"}
        g = self.stage(pr, dirty=1)
        self.assertEqual((g["stage"], g["chips"]), ("uncommitted", ["±1", "PR #14"]))
        self.assertEqual(self.stage(pr)["stage"], "pr")
        self.assertEqual(self.stage(dict(pr, isDraft=True))["chips"], ["draft #14"])
        merged = dict(pr, state="MERGED")
        self.assertEqual(self.stage(merged)["stage"], "merged")            # squash merge: commits not in main, still merged
        self.assertEqual(self.stage(merged, oid="newer")["stage"], "pushed")  # committed again after the merge
        self.assertEqual(self.stage(dict(pr, state="CLOSED"))["stage"], "pr_closed")
        self.assertEqual(self.stage(not_in_default=0)["stage"], "clean")

    def test_unknowns_never_read_as_clean(self):
        timed_out = self.stage(dirty=None, untracked=None)
        self.assertEqual((timed_out["stage"], timed_out["chips"]), ("unknown", ["git ?"]))
        no_origin = self.stage(upstream="", ahead=None, not_in_default=None)
        self.assertEqual((no_origin["stage"], no_origin["chips"]), ("unknown", ["no origin"]))
        self.assertEqual(self.stage(upstream="", ahead=None, not_in_default=None, dirty=2)["stage"], "uncommitted")

    def test_detached_and_merged_elsewhere_are_not_landed(self):
        det = self.stage(branch="HEAD", upstream="", ahead=None, not_in_default=45)
        self.assertEqual((det["stage"], det["chips"]), ("unpushed", ["↑45"]))
        self.assertEqual(self.stage(branch="HEAD", upstream="", ahead=None, not_in_default=0)["stage"], "clean")
        other = {"number": 7, "state": "MERGED", "headRefOid": "abc", "baseRefName": "integration"}
        g = self.stage(other, default="origin/main")
        self.assertEqual((g["stage"], g["chips"]), ("pushed", ["merged #7→integration"]))
        self.assertEqual(self.stage(dict(other, baseRefName="main"), default="origin/main")["stage"], "merged")

    def test_trunk(self):
        self.assertEqual(self.stage(branch="main", not_in_default=0)["stage"], "clean")
        self.assertEqual(self.stage(branch="main", ahead=1)["stage"], "unpushed")
        self.assertEqual(self.stage(branch="master", upstream="", ahead=None, not_in_default=5)["stage"], "clean")
        self.assertIsNone(classify.git_stage({}))

    def test_status_v2_and_slug(self):
        text = ("# branch.oid 487eab0\n# branch.head mac-board\n# branch.upstream origin/mac-board\n"
                "# branch.ab +2 -1\n1 .M N... 100644 100644 100644 a b README.md\n"
                "2 R. N... 100644 100644 100644 a b R100 new\told\n? scratch.txt\n")
        st = sources.parse_status_v2(text)
        self.assertEqual((st["branch"], st["upstream"], st["ahead"], st["behind"], st["changed"], st["untracked"]),
                         ("mac-board", "origin/mac-board", 2, 1, 2, 1))
        self.assertEqual(sources.parse_status_v2("# branch.oid (initial)\n# branch.head main\n")["ahead"], None)
        self.assertEqual(sources.github_slug("git@github.com:example-user/agent-mailroom.git"), "example-user/agent-mailroom")
        self.assertEqual(sources.github_slug("https://github.com/example-user/agent-board"), "example-user/agent-board")
        self.assertEqual(sources.github_slug("/srv/git/x.git"), "")

    def test_pr_lookup_is_cached_and_failure_tolerant(self):
        calls = []

        def runner(slug):
            calls.append(slug)
            return [{"number": 1, "headRefName": "feat", "state": "MERGED"}, {"number": 3, "headRefName": "feat", "state": "OPEN"}]
        prs = sources.PullRequests(runner=runner, sync=True)
        self.assertEqual(prs.lookup("o/r", "feat", NOW)["number"], 3)       # newest PR for the branch
        self.assertIsNone(prs.lookup("o/r", "other", NOW))
        self.assertEqual(calls, ["o/r"])                                     # one call per repo per ttl

        fail = []

        def broken(slug):
            fail.append(slug)
            raise RuntimeError("gh not logged in")
        bad = sources.PullRequests(runner=broken, sync=True)
        self.assertIsNone(bad.lookup("o/r", "feat", NOW))
        bad.lookup("o/r", "feat", NOW + 3)                                   # next poll: no retry yet
        self.assertEqual(len(fail), 1)
        bad.lookup("o/r", "feat", NOW + sources.PullRequests.RETRY + 1)      # retried after a minute
        self.assertEqual(len(fail), 2)
        prs.runner = broken                                                  # a failure keeps the last good table
        self.assertEqual(prs.lookup("o/r", "feat", NOW + prs.ttl + 1)["number"], 3)


class GitRepoTest(unittest.TestCase):
    def git(self, d, *a):
        import subprocess
        subprocess.run(["git", "-C", str(d)] + list(a), check=True, capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t",
                                GIT_COMMITTER_EMAIL="t@x"))

    def test_unborn_and_fresh_repos(self):
        with tempfile.TemporaryDirectory() as d:
            self.git(d, "init", "-q", "-b", "feat")
            (Path(d) / "a.txt").write_text("x")
            f = sources.git_facts(d)
            self.assertEqual((f["branch"], f["dirty"], f["untracked"], f["upstream"]), ("feat", 0, 1, ""))
            self.assertEqual(classify.git_stage(f)["stage"], "uncommitted")    # no origin, but work is visible
            self.git(d, "add", "a.txt")
            self.git(d, "commit", "-q", "-m", "first")
            f = sources.git_facts(d)
            self.assertEqual((f["branch"], f["dirty"], f["untracked"], f["last_commit"]["subject"]), ("feat", 0, 0, "first"))
            self.assertEqual(classify.git_stage(f)["chips"], ["no origin"])
            self.assertFalse(f["linked"])
            wt = Path(d) / "wt"
            self.git(d, "worktree", "add", "-q", "-b", "side", str(wt))
            self.assertTrue(sources.git_facts(str(wt))["linked"])

    def test_refresh_invalidates_and_lifts_budget(self):
        calls = []
        cache = sources.GitCache(ttl=60, budget=1, fetch=lambda c: calls.append(c) or {"repo": c})
        cache.get_many(["a", "b", "c"], NOW)
        cache.get_many(["a", "b", "c"], NOW + 1)
        cache.get_many(["a", "b", "c"], NOW + 2)
        self.assertEqual(sorted(calls), ["a", "b", "c"])
        cache.invalidate()
        cache.get_many(["a", "b", "c"], NOW + 3)          # all three at once after a manual refresh
        self.assertEqual(len(calls), 6)

    def test_subagent_parent(self):
        src = '{"subagent":{"thread_spawn":{"parent_thread_id":"0190bbbb-0000","depth":1}}}'
        self.assertEqual(sources.subagent_parent(src, "subagent"), "0190bbbb-0000")
        self.assertEqual(sources.subagent_parent("cli", "user"), "")
        self.assertEqual(sources.subagent_parent("garbage", "subagent"), "")        # unknown parent: not dropped
        self.assertEqual(sources.subagent_parent('{"subagent": "review"}', "subagent"), "")

    def test_claude_logins_from_usage_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps({"usage": {"claude_accounts": {
                "personal": {"config_dir": None}, "work": {"config_dir": d + "/work"}}}}))
            self.assertEqual([(n, str(p)) for n, p in sources.claude_logins(cfg)],
                             [("personal", str(sources.DEFAULT_CLAUDE_HOME)), ("work", d + "/work")])
            self.assertEqual([n for n, _ in sources.claude_logins(Path(d) / "missing.json")], ["default"])
            # null means ~/.claude even if this process runs under another login's CLAUDE_CONFIG_DIR
            real = sources.CLAUDE_HOME
            sources.CLAUDE_HOME = Path(d) / "work"
            try:
                self.assertEqual([(n, str(p)) for n, p in sources.claude_logins(cfg)],
                                 [("personal", str(sources.HOME / ".claude")), ("work", d + "/work")])
            finally:
                sources.CLAUDE_HOME = real

    def test_subagent_found_by_shared_lock_holder(self):
        if not Path("/proc/locks").exists():
            self.skipTest("needs /proc/locks")
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "thread-writer-locks").mkdir()
            parent, sub = "0190bbbb-0000-7000-8000-00000000b001", "0190bbbb-1111-7000-8000-00000000b002"
            con = sqlite3.connect(str(home / "state_5.sqlite"))
            con.execute("create table threads(id, name, title, cwd, git_branch, rollout_path, updated_at, model, "
                        "reasoning_effort, created_at, archived, source, thread_source, agent_nickname)")
            con.execute("insert into threads values(?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
                        (parent, "garden", "", "/x", "", "", int(NOW), "m", "x", int(NOW), "cli", "user", None))
            con.execute("insert into threads values(?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
                        (sub, "", "", "/x", "", "", int(NOW), "m", "x", int(NOW), '{"subagent": "review"}', "subagent", "Darwin"))
            con.commit()
            con.close()
            handles = []
            for t in (parent, sub):
                (home / "thread-writer-locks" / (t + ".lock")).touch()
                fh = open(home / "thread-writer-locks" / (t + ".lock"))
                fcntl.flock(fh, fcntl.LOCK_EX)
                handles.append(fh)
            try:
                rows = {r["id"]: r for r in sources.CodexReader(home).sessions(now=NOW)}
            finally:
                for fh in handles:
                    fh.close()
            self.assertEqual(rows[sub]["parent"], parent)
            self.assertEqual(rows[parent]["parent"], "")


class StuckWaitTest(unittest.TestCase):
    SNAP = "/bin/bash -c source /home/t/.claude/shell-snapshots/snapshot-bash-1-x.sh 2>/dev/null || true && eval '%s'"

    def shell(self, cmd, tail=" < /dev/null && pwd -P >| /tmp/claude-ab-cwd"):
        return (self.SNAP % cmd.replace("'", "'\"'\"'") + tail).split(" ", 2)

    def test_pgrep_wait_reads_the_pattern(self):
        w = sources.pgrep_wait
        self.assertEqual(w('until ! pgrep -f "stage era_pilot" >/dev/null; do sleep 15; done'),
                         ("stage era_pilot", False, False))
        self.assertEqual(w("while pgrep -af train.py; do sleep 5; done"), ("train.py", False, False))
        self.assertEqual(w('while pgrep -f -u alex "x y"; do :; done'), ("x y", False, False))
        self.assertEqual(w('while pgrep -fualex "x y"; do :; done'), ("x y", False, False))
        self.assertEqual(w('until ! pgrep -fxi "python a.py"; do sleep 1; done'), ("python a.py", True, True))
        self.assertIsNone(w("until ! pgrep python; do sleep 1; done"))           # no -f: process names only
        self.assertIsNone(w('until ! pgrep -f "$P"; do sleep 1; done'))           # unknowable pattern
        self.assertIsNone(w("while pgrep -fv x; do sleep 1; done"))
        self.assertIsNone(w("until pgrep -f x; do sleep 1; done"))                # waits for it to appear: ends at once
        self.assertIsNone(w("while pgrep -f 'unbalanced; do sleep 1; done"))
        self.assertIsNone(w("for i in $(seq 1 9); do sleep 30; done"))

    def test_tool_command_unwraps_a_background_command(self):
        raw = " ".join(self.shell("until ! pgrep -f 'stage x'; do sleep 15; done"))
        self.assertEqual(sources.tool_command(raw), "until ! pgrep -f 'stage x'; do sleep 15; done")

    def test_a_loop_that_finds_itself_is_stuck(self):
        procs = {
            100: (1, ["claude", "--resume", "a"]),
            101: (100, self.shell('until ! pgrep -f "stage era_pilot" >/dev/null; do sleep 15; done')),
            102: (101, ["sleep", "15"]),
            103: (100, self.shell("for i in $(seq 1 9); do sleep 30; done", tail="")),
            200: (1, ["claude", "--resume", "b"]),
            201: (200, self.shell('until ! pgrep -f "[s]tage era_probe"; do sleep 15; done')),   # cannot find itself
            202: (200, self.shell('while pgrep -f "stage era_route"; do sleep 15; done')),
            203: (1, ["python3", "run.py", "--stage=era_route"]),           # not a match for "stage era_route"
            204: (1, ["python3", "run.py", "stage era_route"]),
            300: (1, ["claude", "--resume", "c"]),
            301: (300, self.shell('until ! pgrep -f "stage era_pilot"; do sleep 60; done')),
            302: (301, ["pgrep", "-f", "stage era_pilot"]),                 # a loop's own probe is not the target
            400: (1, ["claude", "--resume", "d"]),
        }
        out = sources.stuck_waits([100, 200, 300, 400], procs)
        self.assertEqual(out[100], {"shells": 2, "stuck": [{"pid": 101, "pattern": "stage era_pilot", "target": False}]})
        self.assertEqual(out[200], {"shells": 2, "stuck": [{"pid": 202, "pattern": "stage era_route", "target": True}]})
        self.assertEqual(out[300]["stuck"], [{"pid": 301, "pattern": "stage era_pilot", "target": False}])
        self.assertNotIn(400, out)
        del procs[204]
        self.assertFalse(sources.stuck_waits([200], procs)[200]["stuck"][0]["target"])


class LocksTest(unittest.TestCase):
    def test_held_locks_parse(self):
        text = ("1: POSIX  ADVISORY  WRITE 2878007 103:02:39321694 0 0\n"
                "2: FLOCK  ADVISORY  WRITE 1174 00:1a:2344 0 EOF\n"
                "3: -> FLOCK  ADVISORY  WRITE 999 00:1a:2344 0 EOF\n")
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write(text)
        real = sources.Path
        try:
            sources.Path = lambda p: real(fh.name) if p == "/proc/locks" else real(p)
            held = sources.held_locks()
        finally:
            sources.Path = real
            os.unlink(fh.name)
        self.assertEqual(held, {"103:02:39321694": 2878007, "00:1a:2344": 1174})

    def test_codex_lock_file_without_holder_is_not_live(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "thread-writer-locks").mkdir()
            tid = "0190aaaa-0000-7000-8000-00000000a001"
            (home / "thread-writer-locks" / (tid + ".lock")).touch()
            con = sqlite3.connect(str(home / "state_5.sqlite"))
            con.execute("create table threads(id, name, title, cwd, git_branch, rollout_path, updated_at, model, "
                        "reasoning_effort, created_at, archived)")
            con.execute("insert into threads values(?,?,?,?,?,?,?,?,?,?,0)",
                        (tid, "astra", "t", "/x", "b", "", int(NOW), "m", "x", int(NOW - 99)))
            con.commit()
            con.close()
            if Path("/proc/locks").exists():
                rows = sources.CodexReader(home).sessions(now=NOW)
                self.assertEqual([(r["name"], r["live"]) for r in rows], [("astra", False)])
                import fcntl
                with open(home / "thread-writer-locks" / (tid + ".lock")) as fh:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                    rows = sources.CodexReader(home).sessions(now=NOW)
                    self.assertTrue(rows[0]["live"])


class EventLogTest(unittest.TestCase):
    def test_incremental_tail_and_rotation(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "events.jsonl"
            def w(p, evs):
                with p.open("a") as fh:
                    fh.write("".join(json.dumps(e) + "\n" for e in evs))
            w(log, [{"ts": NOW - 10, "event": "start", "session_id": "s"}])
            el = sources.EventLog(log)
            self.assertEqual(len(el.poll(NOW)), 1)
            with log.open("a") as fh:
                fh.write(json.dumps({"ts": NOW - 5, "event": "idle", "session_id": "s"}) + "\n" + '{"ts": %d' % NOW)  # half a line
            self.assertEqual(len(el.poll(NOW)), 2)
            with log.open("a") as fh:
                fh.write(', "event": "note", "session_id": "s"}\n')
            evs = el.poll(NOW)
            self.assertEqual(evs[-1]["event"], "note")
            os.replace(log, log.with_suffix(".jsonl.1"))
            w(log, [{"ts": NOW - 1, "event": "end", "session_id": "s"}])
            self.assertEqual([e["event"] for e in el.poll(NOW)], ["start", "idle", "note", "end"])


def mail_fixture(path):
    con = sqlite3.connect(str(path))
    con.executescript("""
      create table sessions(tag text primary key, cli text, session_id text, session_name text, cwd text,
                            created_at integer, origin_host text);
      create table sent(id integer primary key, tag text, gmail_id text, thread_id text, message_id text,
                        to_addr text, subject text, sent_at integer, in_reply_to_inbound integer);
      create table inbound(id integer primary key, gmail_id text, thread_id text, message_id text, from_addr text,
                           from_name text, subject text, received_at integer, matched_tag text, match_rule text,
                           action text, detail text, body text, released_to text, released_at integer);
    """)
    sid_a, sid_b, sid_mac = "a" * 8 + "-1111-2222-3333-444444444444", "b" * 8 + "-1111-2222-3333-444444444444", "c" * 36
    con.executemany("insert into sessions values(?,?,?,?,?,?,?)", [
        ("taga", "claude", sid_a, "alpha", "/x", 0, "desktop"),
        ("tagb", "codex", sid_b, "beta", "/y", 0, "desktop"),
        ("tagm", "claude", sid_mac, "mac", "/z", 0, "mac")])
    con.executemany("insert into sent(tag, thread_id, to_addr, subject, sent_at) values(?,?,?,?,?)", [
        ("taga", "t1", OWNER, "Alpha done [#taga]", NOW - 600),
        ("tagb", "t2", OWNER, "Beta report", NOW - 900),
        ("tagb", "t9", "someone@else.org", "Not to the owner", NOW - 10),
        ("tagm", "t3", OWNER, "Mac agent", NOW - 100)])
    con.executemany("insert into inbound(gmail_id, thread_id, from_addr, subject, received_at, matched_tag, action, detail) "
                    "values(?,?,?,?,?,?,?,?)", [
        ("g1", "t2", OWNER, "Re: Beta report", NOW - 300, "tagb", "delivered", "queued"),
        ("g2", "t4", "spam@x.com", "hi", NOW - 50, None, "held", "sender not on allowlist"),
        ("g3", "t5", OWNER, "Re: old", NOW - 40, None, "held", "ownership unresolved")])
    con.commit()
    con.close()
    return sid_a, sid_b


class MailStoreTest(unittest.TestCase):
    def test_waiting_derivation_from_store(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "mailroom.sqlite"
            sid_a, sid_b = mail_fixture(db)
            got = sources.MailStore(db, owners=[OWNER]).read(NOW)
            self.assertTrue(got["ok"])
            self.assertNotIn(("claude", "c" * 36), got["by_session"])           # Mac-hosted session excluded
            a = got["by_session"][("claude", sid_a)]
            b = got["by_session"][("codex", sid_b)]
            self.assertTrue(classify.mail_state(a["sent"], a["inbound"], NOW)["unanswered"])
            self.assertFalse(classify.mail_state(b["sent"], b["inbound"], NOW)["unanswered"])  # replied; other mail not to the owner
            self.assertEqual([h["owner"] for h in got["held"]], [True, False])               # newest first, owner flagged
            self.assertEqual(os.stat(db).st_mtime, os.stat(db).st_mtime)

    def test_missing_store_is_not_an_error(self):
        got = sources.MailStore(Path("/nonexistent/mailroom.sqlite"), owners=[OWNER]).read(NOW)
        self.assertFalse(got["ok"])
        self.assertEqual(got["by_session"], {})


class CollectorTest(unittest.TestCase):
    def test_end_to_end_with_fixtures(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            db = d / "mailroom.sqlite"
            sid_a, sid_b = mail_fixture(db)
            log = d / "events.jsonl"
            evs = [
                {"ts": NOW - 4000, "event": "start", "session_id": sid_a, "cli": "claude", "name": "alpha", "cwd": "/x"},
                {"ts": NOW - 700, "event": "note", "session_id": sid_a, "text": "tests pass", "cli": "claude"},
                {"ts": NOW - 590, "event": "idle", "session_id": sid_a, "cli": "claude"},
                {"ts": NOW - 50, "event": "start", "session_id": "orchestrator", "cli": "claude", "name": "orchestrator"},
                {"ts": NOW - 3000, "event": "start", "session_id": "d" * 36, "cli": "claude", "name": "probe"},
                {"ts": NOW - 2990, "event": "end", "session_id": "d" * 36, "cli": "claude"},
            ]
            log.write_text("".join(json.dumps(e) + "\n" for e in evs))
            claude = lambda: [{"cli": "claude", "id": sid_a, "name": "alpha", "cwd": "/x", "raw_status": "idle",
                               "kind": "interactive", "live": True, "pid": None, "tmux": "alpha:@1.%1",
                               "waiting_for": "", "started_at": NOW - 4000, "status_at": NOW - 590}]
            class FakeCodex:
                def sessions(self, ids_of_interest=(), now=None):
                    base = {"cli": "codex", "cwd": str(d), "raw_status": "idle", "kind": "interactive", "pid": None,
                            "tmux": "", "waiting_for": "", "started_at": NOW - 900, "status_at": NOW - 100, "title": ""}
                    parent = "0190bbbb-0000-7000-8000-00000000b001"
                    return [dict(base, id=parent, name="garden", live=True, parent=""),
                            dict(base, id="0190cccc-0000-7000-8000-00000000c001", name="", live=True, parent=parent,
                                 nickname="Beauvoir"),
                            dict(base, id="0190aaaa-0000-7000-8000-00000000a001", name="astra", live=False, parent="",
                                 status_at=NOW - 3600)]
            evs.append({"ts": NOW - 3600, "event": "note", "session_id": "0190aaaa-0000-7000-8000-00000000a001",
                        "text": "phase 1 done", "cli": "codex", "name": "astra"})
            evs.append({"ts": NOW - 30, "event": "idle", "session_id": "0190cccc-0000-7000-8000-00000000c001",
                        "cli": "codex"})          # a subagent's own hook event must not become a ghost tile
            evs.append({"ts": NOW - 20, "event": "failover", "session_id": sid_a, "cli": "claude",
                        "account": "work", "text": "personal -> work (limit: five-hour limit, resets 17:29)"})
            log.write_text("".join(json.dumps(e) + "\n" for e in evs))
            col = collect.Collector(events=sources.EventLog(log), codex=FakeCodex(),
                                    mail=sources.MailStore(db, owners=[OWNER]),
                                    git=sources.GitCache(fetch=lambda c: {"repo": "proj", "branch": "feat", "dirty": 2}),
                                    claude=claude, panes=lambda: {})
            col.sessions_fn = lambda: {"astra": {str(d)}, "alpha": {"/somewhere/else"}}
            snap = col.snapshot(NOW)
            by = {a["display"]: a for a in snap["agents"]}
            self.assertEqual(sorted(by), ["alpha", "astra", "garden"])   # no orchestrator, probe or subagent tiles
            self.assertEqual([x["nickname"] for x in by["garden"]["subagents"]], ["Beauvoir"])
            self.assertEqual(by["astra"]["left_behind"], {"cwd_exists": True, "tmux": "astra"})
            self.assertEqual(col._own_tmux(str(d), "alpha"), "")          # same name, different directory
            gone = str(d) + "/removed-worktree"
            self.assertEqual(col._own_tmux(gone, "alpha"), "")             # dir gone: a bare name proves nothing
            self.assertEqual(col._own_tmux(gone, "x", recorded="alpha"), "alpha")   # its registry named that session
            self.assertEqual(col._own_tmux(str(d), "x", recorded="alpha"), "")     # dir there: the pane must match too
            a = by["alpha"]
            self.assertEqual((a["state"], a["tmux"], a["repo"], a["branch"], a["dirty"]),
                             ("waiting", "alpha", "proj", "feat", 2))
            self.assertEqual(a["last_note"]["text"], "tests pass")
            self.assertEqual(snap["summary"]["waiting"], 1)
            self.assertEqual(snap["mail"]["held_count"], 2)
            self.assertEqual(snap["timeline"][0]["event"], "failover")
            self.assertEqual(by["alpha"]["failover"]["account"], "work")
            self.assertIn("tests pass", [t["text"] for t in snap["timeline"]])
            json.dumps(snap)                                        # serialisable

    def test_the_star_reaches_the_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            sid = "11111111-2222-3333-4444-555555555555"
            log = d / "events.jsonl"
            log.write_text(json.dumps({"ts": NOW - 4000, "event": "start", "session_id": sid, "cli": "claude",
                                       "name": "fab", "cwd": "/x"}) + "\n")
            claude = lambda: [{"cli": "claude", "id": sid, "name": "fab", "cwd": "/x", "raw_status": "idle",
                               "kind": "interactive", "live": True, "pid": None, "tmux": "fab:@1.%1", "waiting_for": "",
                               "started_at": NOW - 4000, "status_at": NOW - 3000, "account": "work",
                               "account_default": False}]
            class FakeUsage:
                def maybe_refresh(self, now):
                    return {"accounts": [{"target": "claude:personal", "windows": [
                        {"kind": "session", "name": "5-hour", "remaining_pct": 90.0}]}]}
            col = collect.Collector(events=sources.EventLog(log), codex=sources.CodexReader(d / "none"),
                                    mail=sources.MailStore(d / "none.sqlite", owners=[OWNER]),
                                    git=sources.GitCache(fetch=lambda c: {}), claude=claude, panes=lambda: {},
                                    usage=FakeUsage(), routing=lambda: (["personal", "work"], {"session": 10, "weekly": 5}))
            col.sessions_fn = lambda: {}
            col.models[(sid, None)] = ("claude-opus-5-5", NOW)
            col.models[("gone", 1)] = ("x", NOW)
            a = col.snapshot(NOW)["agents"][0]
            self.assertEqual(a["model"], "claude-opus-5-5")
            self.assertIn("Running on work while personal has room", a["login_note"])
            self.assertEqual(list(col.models), [(sid, None)])          # agents no longer there are forgotten

    def test_a_stuck_background_wait_reads_idle(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            sid = "11111111-2222-3333-4444-555555555555"
            log = d / "events.jsonl"
            log.write_text(json.dumps({"ts": NOW - 4000, "event": "start", "session_id": sid, "cli": "claude",
                                       "name": "todo", "cwd": "/x"}) + "\n")
            claude = lambda: [{"cli": "claude", "id": sid, "name": "todo", "cwd": "/x", "raw_status": "shell",
                               "kind": "interactive", "live": True, "pid": 4242, "tmux": "todo:@1.%1",
                               "waiting_for": "", "started_at": NOW - 4000, "status_at": NOW - 3000}]
            calls = []
            def waits(pids):
                calls.append(list(pids))
                return {4242: {"shells": 1, "stuck": [{"pid": 4243, "pattern": "stage x", "target": False}]}}
            col = collect.Collector(events=sources.EventLog(log), codex=sources.CodexReader(d / "none"),
                                    mail=sources.MailStore(d / "none.sqlite", owners=[OWNER]),
                                    git=sources.GitCache(fetch=lambda c: {}), claude=claude, panes=lambda: {},
                                    waits=waits)
            col.sessions_fn = lambda: {}
            a = col.snapshot(NOW)["agents"][0]
            self.assertEqual(a["state"], "idle")
            self.assertIn("`stage x` can never end", a["reason"])
            col.snapshot(NOW + 5)
            self.assertEqual(calls, [[4242]])                        # /proc is read at most every 30 s
            col.snapshot(NOW + 40)
            self.assertEqual(len(calls), 2)


class LeakedEnvTest(unittest.TestCase):
    def test_an_sdk_cli_session_in_a_tmux_pane_is_an_agent(self):
        # a tmux server an orchestrator run started gives every agent CLAUDE_CODE_ENTRYPOINT=sdk-cli
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            log = d / "events.jsonl"
            log.write_text("")
            row = {"cli": "claude", "cwd": str(d), "raw_status": "idle", "kind": "interactive", "live": True,
                   "tmux": "", "waiting_for": "", "started_at": NOW - 900, "status_at": NOW - 100, "entrypoint": "sdk-cli"}
            agent = dict(row, id="aaaaaaaa-2222-3333-4444-555555555555", name="resumed-agent", pid=4999998)
            probe = dict(row, id="bbbbbbbb-2222-3333-4444-555555555555", name="probe", pid=4999999)
            col = collect.Collector(events=sources.EventLog(log), codex=sources.CodexReader(d / "none"),
                                    mail=sources.MailStore(d / "none.sqlite", owners=[OWNER]),
                                    git=sources.GitCache(fetch=lambda c: {}), claude=lambda: [agent, probe],
                                    panes=lambda: {4999990: "resumed-agent"})
            col.sessions_fn = lambda: {}
            real = sources.ppid
            sources.ppid = lambda pid: {4999998: 4999990, 4999999: 4999991}.get(pid, 0)
            try:
                rows, _, _ = col.agents(NOW, with_git=False)
            finally:
                sources.ppid = real
            # the agent's parent is the pane's shell; the probe's grandparent is (it runs from an agent's tool shell)
            self.assertEqual([a["name"] for a in rows], ["resumed-agent"])

    def test_launches_scrub_the_leaked_variables_and_tmux_forgets_them(self):
        self.assertIn("-u AGENT_ORCHESTRATOR", actions.SCRUB_ENV)
        self.assertIn("-u CLAUDE_CODE_ENTRYPOINT", actions.SCRUB_ENV)
        calls = []

        def run(cmd, stdin=None, timeout=60):
            calls.append(cmd)
            if cmd[1] == "show-environment":
                return 0, "PATH=/usr/bin\nCLAUDE_CONFIG_DIR=/x\nAGENT_ORCHESTRATOR=1\nCLAUDE_CODE_ENTRYPOINT=sdk-cli\n" \
                          "CLAUDE_CODE_SOMETHING_NEW=1\n-DISPLAY\n"
            return 0, ""
        gone = actions.scrub_tmux_env("tmux", run)
        self.assertEqual(gone, ["AGENT_ORCHESTRATOR", "CLAUDE_CODE_ENTRYPOINT"])        # a user's own CLAUDE_CODE_* stays
        self.assertEqual([c[-1] for c in calls if c[1] == "set-environment"], gone)   # PATH, the login dir stay
        self.assertEqual(actions.scrub_tmux_env("tmux", lambda cmd, timeout=0: (1, "no server")), [])


class ActionsTest(unittest.TestCase):
    rows = [{"id": "11111111-2222-3333-4444-555555555555", "cli": "claude", "live": True, "display": "alpha"},
            {"id": "0190aaaa-0000-7000-8000-00000000a001", "cli": "codex", "live": False, "display": "astra"}]

    def run_act(self, req):
        calls = []

        def fake(cmd, stdin=None, timeout=0):
            calls.append((cmd, stdin))
            return 0, "ok"
        real_find, real_audit = sources.find_bin, actions._audit
        sources.find_bin = lambda name: "/bin/" + name
        actions._audit = lambda e: None
        try:
            return actions.perform(req, self.rows, run=fake), calls
        finally:
            sources.find_bin, actions._audit = real_find, real_audit

    def test_close_uses_exact_id_and_fixed_text(self):
        res, calls = self.run_act({"action": "close", "id": self.rows[0]["id"]})
        self.assertTrue(res["ok"])
        cmd, stdin = calls[0]
        self.assertEqual(cmd, ["/bin/agent-send", "--raw", "--cli", "claude", self.rows[0]["id"]])
        self.assertTrue(stdin.startswith("[relayed from Alex via agent-board]"))
        self.assertIn("email Alex (agent-mail send)", stdin)
        self.assertIn("close", stdin.splitlines()[0])

    def test_send_text_goes_on_stdin_never_argv(self):
        res, calls = self.run_act({"action": "send", "id": self.rows[0]["id"], "text": "--help; rm -rf ~ $(x)"})
        self.assertTrue(res["ok"])
        self.assertNotIn("--help; rm -rf ~ $(x)", calls[0][0])
        self.assertIn("--help; rm -rf ~ $(x)", calls[0][1])

    def test_rejections(self):
        bad = [
            {"action": "close", "id": "alpha"},                                   # names are not accepted
            {"action": "close", "id": "11111111-2222-3333-4444-555555555555; rm"},
            {"action": "close", "id": "99999999-2222-3333-4444-555555555555"},     # not on the board
            {"action": "close", "id": self.rows[1]["id"]},                          # not running
            {"action": "send", "id": self.rows[0]["id"], "text": "  "},
            {"action": "force_close", "id": self.rows[0]["id"], "confirm": "alph"},
            {"action": "shell", "id": self.rows[0]["id"]},
            ["close"],
        ]
        for req in bad:
            res, calls = self.run_act(req)
            self.assertFalse(res["ok"], req)
            self.assertEqual(calls, [], req)

    def test_cleanup_is_plain_agent_close_and_guards_shared_worktrees(self):
        res, calls = self.run_act({"action": "cleanup", "id": self.rows[1]["id"]})
        self.assertTrue(res["ok"])
        self.assertEqual(calls[0][0], ["/bin/agent-close", self.rows[1]["id"]])      # no --force: refuses unlanded work
        res, calls = self.run_act({"action": "cleanup", "id": self.rows[0]["id"]})   # live: use Close instead
        self.assertFalse(res["ok"])
        shared = [dict(self.rows[0], cwd="/w/repo-feat"), dict(self.rows[1], cwd="/w/repo-feat")]
        for req in ({"action": "cleanup", "id": shared[1]["id"]},
                    {"action": "force_close", "id": shared[1]["id"], "confirm": "astra"}):
            res = actions.perform(req, shared, run=lambda *a, **k: (0, "should not run"))
            self.assertFalse(res["ok"])
            self.assertIn("same directory", res["error"])

    def test_switch_validates_and_builds_argv(self):
        calls = []

        def fake(cmd, stdin=None, timeout=0):
            calls.append(cmd)
            if "--options" in cmd:
                return 0, json.dumps({"agent": {"busy": True}, "will_interrupt": True, "targets": []})
            return (3, json.dumps({"ok": False, "needs_force": True, "detail": "busy"})) if "--force" not in cmd \
                else (0, json.dumps({"ok": True, "detail": "personal -> work"}))
        real_find, real_audit = sources.find_bin, actions._audit
        sources.find_bin = lambda name: "/bin/" + name
        actions._audit = lambda e: None
        try:
            sid = self.rows[0]["id"]
            res = actions.perform({"action": "switch_options", "id": sid}, self.rows, run=fake)
            self.assertTrue(res["options"]["will_interrupt"])
            self.assertEqual(calls[-1], ["/bin/agent-switch", sid, "--options", "--json"])
            res = actions.perform({"action": "switch", "id": sid, "to": "claude:work"}, self.rows, run=fake)
            self.assertTrue(res["needs_force"])                                   # busy: ask, then --force
            res = actions.perform({"action": "switch", "id": sid, "to": "claude:work", "model": "opus", "effort": "high",
                                   "force": True}, self.rows, run=fake)
            self.assertTrue(res["ok"])
            self.assertEqual(calls[-1], ["/bin/agent-switch", sid, "--to", "claude:work", "--model", "opus",
                                         "--effort", "high", "--force", "--json"])
            for odd in ("[1, 2]", "null", '"text"', "not json"):
                res = actions.perform({"action": "switch_options", "id": sid}, self.rows, run=lambda cmd, **k: (1, odd))
                self.assertFalse(res["ok"])
                res = actions.perform({"action": "switch", "id": sid, "to": "codex"}, self.rows, run=lambda cmd, **k: (1, odd))
                self.assertFalse(res["ok"])
            refuse = lambda cmd, **k: (2, json.dumps({"ok": False, "refused": True, "detail": "not agent-spawn's tmux layout"}))
            res = actions.perform({"action": "switch_options", "id": sid}, self.rows, run=refuse)
            self.assertEqual((res["ok"], res["error"]), (False, "not agent-spawn's tmux layout"))
            for bad in ({"to": "claude:work; rm -rf ~"}, {"to": "gemini"}, {"model": "--help"}, {"effort": "x y"}, {},
                        {"to": "codex", "force": "yes"}):
                n = len(calls)
                self.assertFalse(actions.perform(dict(bad, action="switch", id=sid), self.rows, run=fake)["ok"], bad)
                self.assertEqual(len(calls), n, bad)
            self.assertFalse(actions.perform({"action": "switch", "id": self.rows[1]["id"], "to": "codex"}, self.rows, run=fake)["ok"])
        finally:
            sources.find_bin, actions._audit = real_find, real_audit

    def test_force_close_needs_typed_name_and_works_offline(self):
        res, calls = self.run_act({"action": "force_close", "id": self.rows[1]["id"], "confirm": "astra"})
        self.assertTrue(res["ok"])
        self.assertEqual(calls[0][0], ["/bin/agent-close", self.rows[1]["id"], "--force"])
        res, calls = self.run_act({"action": "check_close", "id": self.rows[1]["id"]})
        self.assertEqual(calls[0][0], ["/bin/agent-close", self.rows[1]["id"], "--dry-run"])


class WorktreeTest(unittest.TestCase):
    wts = [{"path": "/w/feed-fix", "repo": "feed", "main": "/w/feed", "branch": "fix", "detached": False, "locked": False},
           {"path": "/w/proj/.claude/worktrees/agent-1", "repo": "proj", "main": "/w/proj", "branch": "x", "detached": False,
            "locked": False}]

    def test_occupied(self):
        self.assertTrue(collect.occupied(self.wts[0], ["/w/feed-fix/sub"], []))       # an agent working in it
        self.assertTrue(collect.occupied(self.wts[0], [], ["/w/feed-fix"]))           # already an Offline tile
        self.assertFalse(collect.occupied(self.wts[0], ["/w/feed"], []))              # main checkout's agent: free
        self.assertTrue(collect.occupied(self.wts[1], ["/w/proj"], []))                 # its subagent may still use it
        self.assertFalse(collect.occupied(self.wts[1], [], []))
        self.assertTrue(collect.occupied(self.wts[1], ["/w/proj-other-worktree"], [], {"proj"}))   # same repo, elsewhere

    def test_actions_validate_and_route(self):
        calls = []
        closer = lambda info, force, dry_run: (calls.append((info["cwd"], force, dry_run)) or (0, ["ok"]))
        real = actions._audit
        actions._audit = lambda e: None
        try:
            ok = lambda req, agents=(): actions.perform_wt(req, list(agents), self.wts, closer=closer,
                                                           run=lambda cmd, **k: (calls.append(cmd) or (0, "{}")), state="clean")
            self.assertTrue(ok({"action": "wt_cleanup", "path": "/w/feed-fix"})["ok"])
            self.assertEqual(calls[-1], ("/w/feed-fix", False, False))                 # plain agent-close semantics
            self.assertTrue(ok({"action": "wt_check", "path": "/w/feed-fix"})["ok"])
            self.assertEqual(calls[-1], ("/w/feed-fix", False, True))
            self.assertFalse(ok({"action": "wt_force", "path": "/w/feed-fix", "confirm": "feed"})["ok"])
            self.assertTrue(ok({"action": "wt_force", "path": "/w/feed-fix", "confirm": "feed-fix"})["ok"])
            self.assertEqual(calls[-1], ("/w/feed-fix", True, False))
            res = ok({"action": "wt_spawn", "path": "/w/feed-fix", "text": "--help $(rm -rf ~) tidy and close"})
            self.assertTrue(res["ok"])
            cmd = calls[-1]
            self.assertEqual(cmd[1:3], ["--dir", "/w/feed-fix"])
            self.assertIn("$(rm -rf ~) tidy and close", cmd[4])                         # one argv element, no shell
            for bad in ({"action": "wt_cleanup", "path": "/etc"}, {"action": "wt_cleanup", "path": "feed-fix"},
                        {"action": "wt_spawn", "path": "/w/feed-fix", "text": " "}, {"action": "wt_rm", "path": "/w/feed-fix"}):
                self.assertFalse(ok(bad)["ok"], bad)
            busy = [{"id": "x", "live": True, "cwd": "/w/feed-fix"}]
            self.assertIn("working in", ok({"action": "wt_cleanup", "path": "/w/feed-fix"}, busy)["error"])
        finally:
            actions._audit = real

    def test_bulk_landed_cleanup(self):
        wts = self.wts + [{"path": "/w/feed-old", "repo": "feed", "main": "/w/feed", "branch": "old", "detached": False,
                           "locked": False}]
        verdict = {"/w/feed-fix": (0, ["would end no tmux session and remove worktree /w/feed-fix"]),
                   "/w/feed-old": (2, ["not closing worktree feed-old: 1 uncommitted path"])}
        calls, fetched = [], []

        def closer(info, force, dry_run, fetch):
            calls.append((info["cwd"], force, dry_run, fetch))
            return verdict[info["cwd"]] if dry_run else (0, ["removed"])
        real = actions._audit
        actions._audit = lambda e: None
        try:
            busy = [{"id": "x", "live": True, "cwd": "/w/proj"}]                      # guards proj's subagent tree
            chk = actions.landed_check(busy, wts, closer=closer, fetch=fetched.extend)
            got = {c["path"]: c["removable"] for c in chk["candidates"]}
            self.assertEqual(got, {"/w/feed-fix": True, "/w/feed-old": False})      # subagent tree not even offered
            self.assertTrue(all(not f and fe is False for _, f, _, fe in calls))     # never --force; one fetch per repo
            self.assertEqual(fetched, ["/w/feed", "/w/feed"])
            res = actions.landed_cleanup({"paths": ["/w/feed-fix", "/etc", "/w/feed-fix"]}, busy, wts, closer=closer,
                                         fetch=lambda m: None)
            self.assertEqual((res["removed"], res["kept"]), (1, 1))                  # a repeated path counts once
            self.assertEqual(calls[-1], ("/w/feed-fix", False, False, False))
            self.assertFalse(actions.landed_cleanup({"paths": "/w/feed-fix"}, busy, wts, closer=closer)["ok"])
        finally:
            actions._audit = real

    def test_list_worktrees_real_repo(self):
        with tempfile.TemporaryDirectory() as d:
            import subprocess
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
            g = lambda *a: subprocess.run(["git"] + list(a), check=True, capture_output=True, env=env)
            g("init", "-q", "-b", "main", d + "/repo")
            g("-C", d + "/repo", "commit", "-q", "--allow-empty", "-m", "x")
            g("-C", d + "/repo", "worktree", "add", "-q", "-b", "feat", d + "/repo-feat")
            got = sources.list_worktrees([d])
            self.assertEqual([(w["repo"], w["branch"], w["path"]) for w in got], [("repo", "feat", d + "/repo-feat")])


class ResumeTest(unittest.TestCase):
    def test_resume_validation_and_tmux_flow(self):
        d = tempfile.mkdtemp()
        dead = {"id": "0190aaaa-0000-7000-8000-00000000a001", "cli": "codex", "live": False, "display": "astra",
                "name": "astra", "cwd": d, "last_activity": NOW}
        rows = [dead, dict(dead, id="11111111-2222-3333-4444-555555555555", live=True, display="alive"),
                dict(dead, id="22222222-2222-3333-4444-555555555555", cwd=d + "/gone", display="nodir")]
        self.assertFalse(actions.perform({"action": "resume", "id": rows[1]["id"]}, rows)["ok"])     # already running
        self.assertIn("directory is gone", actions.perform({"action": "resume", "id": rows[2]["id"]}, rows)["error"])
        calls, sent = [], []

        def run(cmd, stdin=None, timeout=0):
            calls.append(cmd)
            if stdin:
                sent.append(stdin)
            return 0, ("1234" if "#{pane_pid}" in cmd else "")
        real = (sources.find_bin, sources.tmux_sessions, actions.codex_settings, actions._audit, actions.stranded_mail)
        sources.find_bin = lambda n: "/bin/" + n
        sources.tmux_sessions = lambda: {"astra": {"/x"}}                  # name taken: it must pick astra-2
        actions.codex_settings = lambda tid: ("gpt-6-astra", "xhigh")
        actions._audit = lambda e: None
        actions.stranded_mail = lambda a: [311]
        try:
            # already running again, or a process already carries the id: refuse, start nothing
            self.assertIn("already running", actions.resume({}, dead, run=run, live=lambda a: True, busy=lambda i: [])["error"])
            self.assertIn("already has", actions.resume({}, dead, run=run, live=lambda a: False, busy=lambda i: [42])["error"])
            self.assertEqual(calls, [])
            state = {"n": 0}

            def live(a):                     # dead when checked first, live once the launch has happened
                state["n"] += 1
                return state["n"] > 1
            res = actions.resume({"text": "carry on"}, dead, run=run, live=live, wait=3, cap=lambda *a: "capped",
                                 busy=lambda i: [])
        finally:
            sources.find_bin, sources.tmux_sessions, actions.codex_settings, actions._audit, actions.stranded_mail = real
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["tmux"], "astra-2")
        self.assertEqual(calls[0], ["/bin/tmux", "show-environment", "-g"])     # leaked variables dropped first
        self.assertEqual(calls[1][:4], ["/bin/tmux", "new-session", "-d", "-s"])
        line = next(c[-1] for c in calls if "-l" in c)
        self.assertIn("resume -s danger-full-access -a never -C %s -m gpt-6-astra -c model_reasoning_effort=xhigh %s" % (d, dead["id"]), line)
        self.assertTrue(sent and "agent-mail show 311" in sent[0] and "carry on" in sent[0])
        self.assertTrue(sent[0].startswith("[relayed from Alex via agent-board] resumed\n"))
        self.assertIn("Alex resumed it from the agent board, in tmux session 'astra-2'", sent[0])

    def test_newest_transcript_copy_wins(self):
        d = Path(tempfile.mkdtemp())
        sid = "c0c0c0c0-0000-4000-8000-000000000003"
        for login, age in (("work", 3600), ("personal", 10)):
            f = d / login / "projects" / "p" / (sid + ".jsonl")
            f.parent.mkdir(parents=True)
            f.write_text("{}\n")
            os.utime(f, (time.time() - age, time.time() - age))
        real = sources.claude_logins
        sources.claude_logins = lambda: [("work", d / "work"), ("personal", d / "personal")]
        try:
            self.assertEqual(actions.claude_home_of(sid), ("personal", d / "personal"))   # not the first login listed
        finally:
            sources.claude_logins = real

    def test_claude_keeps_its_own_name_when_tmux_needs_another(self):
        d = tempfile.mkdtemp()
        home = Path(d) / ".claude"
        (home / "projects" / "p").mkdir(parents=True)
        sid = "c0c0c0c0-0000-4000-8000-000000000003"
        (home / "projects" / "p" / (sid + ".jsonl")).write_text(json.dumps(
            {"type": "assistant", "message": {"model": "claude-opus-5-5"}}) + "\n")
        real = (sources.claude_logins, sources.DEFAULT_CLAUDE_HOME, sources.find_bin, actions.place_claude)
        sources.claude_logins = lambda: [("personal", home)]
        sources.DEFAULT_CLAUDE_HOME = home
        sources.find_bin = lambda n: "/bin/" + n
        actions.place_claude = lambda a: (home, "on login personal: test")
        try:
            line, how = actions.launch_line({"id": sid, "cli": "claude", "cwd": d, "name": "inventory-abcd1234"},
                                            "inventory-abcd1234-2", {})
        finally:
            sources.claude_logins, sources.DEFAULT_CLAUDE_HOME, sources.find_bin, actions.place_claude = real
        self.assertIn("--name inventory-abcd1234", line)
        self.assertNotIn("abcd1234-2", line)
        self.assertTrue(line.startswith("env %s -u CLAUDE_CONFIG_DIR " % actions.SCRUB_ENV))   # never as the orchestrator
        self.assertIn("--model claude-opus-5-5", line)

    def test_running_with_scans_command_lines(self):
        d = tempfile.mkdtemp()
        for pid, cmd in (("101", b"/home/t/.local/bin/codex\x00resume\x00abc-123"),
                         ("102", b"bash"),
                         ("103", b"tail\x00-f\x00/x/abc-123.jsonl"),                         # reading the transcript
                         ("104", b"python3\x00/bin/agent-mail\x00send\x00--session\x00abc-123"),
                         ("105", b"node\x00/opt/claude.js\x00--resume\x00abc-123"),
                         ("106", b"/usr/bin/claude\x00--resume\x00abc-123x"),                 # a different id
                         ("107", b"node\x00/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js\x00--resume\x00abc-123"),
                         ("108", b"python3\x00/home/t/claude-code-tools/agent-mail\x00--session\x00abc-123")):  # not a CLI
            os.makedirs(os.path.join(d, pid))
            with open(os.path.join(d, pid, "cmdline"), "wb") as fh:
                fh.write(cmd)
        self.assertEqual(sorted(actions.running_with("abc-123", proc=d)), [101, 105, 107])


class OrchestrateTest(unittest.TestCase):
    def test_runs_one_event_in_the_background_with_the_header(self):
        got = {}

        class FakeProc:
            def __init__(self, cmd, **kw):
                got["cmd"], got["kw"] = cmd, kw
                self.stdin = self

            def write(self, data):
                got["stdin"] = data

            def close(self):
                pass
        real_find, real_audit, real_logs = sources.find_bin, actions._audit, actions.ORCH_LOGS
        sources.find_bin = lambda name: "/bin/" + name
        actions._audit = lambda e: None
        actions.ORCH_LOGS = Path(tempfile.mkdtemp())
        try:
            res = actions.orchestrate({"action": "orchestrate", "text": "spin up an agent on proj --help $(x)"}, popen=FakeProc)
            self.assertTrue(res["ok"])
            self.assertEqual(got["cmd"], ["/bin/agent-orchestrate", "--source", "board"])     # text never in argv
            self.assertTrue(got["kw"]["start_new_session"])
            body = got["stdin"].decode()
            self.assertTrue(body.startswith("[message from Alex via the agent board"))
            self.assertIn("agent-mail send --to owner@example.com", body)
            self.assertTrue(body.endswith("spin up an agent on proj --help $(x)"))
            for bad in ({"text": "  "}, {"text": 5}, {}, {"text": "x" * 9000}):
                self.assertFalse(actions.orchestrate(dict(bad, action="orchestrate"), popen=FakeProc)["ok"])
        finally:
            sources.find_bin, actions._audit, actions.ORCH_LOGS = real_find, real_audit, real_logs


class UsageTest(unittest.TestCase):
    def test_stand_in_reads_cache_and_swaps_to_agent_usage(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "usage-cache.json"
            cache.write_text(json.dumps({"codex": {"plan": "pro", "windows": [], "observed_at": NOW - 60},
                                         "claude:default": {"plan": "team", "windows": [{"kind": "session", "remaining_pct": 40}],
                                                            "observed_at": NOW - 30}}))
            u = sources.Usage(cache, runner=lambda refresh: None)       # agent-usage not installed
            u.refresh()
            self.assertEqual(u.data["source"], "cache")
            self.assertEqual([a["target"] for a in u.data["accounts"]], ["claude:default", "codex"])
            self.assertTrue(all(a["stale"] for a in u.data["accounts"]))
            u = sources.Usage(cache, runner=lambda refresh: {"generated_at": NOW, "accounts": [{"target": "codex", "label": "Codex"}]})
            u.refresh()
            self.assertEqual((u.data["source"], u.data["accounts"][0]["label"]), ("agent-usage", "Codex"))

            for junk in ("null", "[1, 2]", "{\"codex\": 3}", "not json",
                         json.dumps({"codex": {"observed_at": "yesterday"}, "claude:x": {"observed_at": 5}})):
                cache.write_text(junk)
                u = sources.Usage(cache, runner=lambda refresh: None)       # must not raise
                self.assertIsInstance(u.data["accounts"], list)
            cache.write_text(json.dumps({"codex": {"plan": "pro", "windows": [], "observed_at": NOW - 60}}))

            def boom(refresh):
                raise RuntimeError("probe failed")
            u = sources.Usage(cache, runner=boom)
            u.refresh()
            self.assertEqual((u.data["source"], u.data["error"]), ("cache", "probe failed"))


if __name__ == "__main__":
    unittest.main()
