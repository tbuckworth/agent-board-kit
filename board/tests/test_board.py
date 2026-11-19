"""Collector rules with fixture data: python3 -m unittest discover -s tests"""
import contextlib
import io
import json
import os
import sqlite3
import time
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fixtures  # noqa: E402  (first: a scratch config with an example owner, _kitenv)
import stand_ins  # noqa: E402
from agentboard import (actions, classify, cli, collect, config, credits, gmailstate, relaunch, restart,  # noqa: E402
                        sent, sources)

_scratch = Path(tempfile.mkdtemp())          # no test writes the owner's real record of what was sent, its marks, or
sent.PATH, sent.MARKS = _scratch / "sent.jsonl", _scratch / "sent-marks.jsonl"     # the actions log (and its locks)
actions.AUDIT = _scratch / "actions.jsonl"

NOW = 1_800_000_000.0
OWNER, AGENTS = fixtures.OWNER, fixtures.AGENTS


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

    def test_mail_held_by_the_review_shows_once_its_turn_ends(self):
        rev = [{"ts": NOW - 600, "subject": "Results", "reasons": "no summary first", "to": "owner"}]
        st, why = classify.classify(agent(mail={"revise": rev}), NOW)
        self.assertEqual(st, "idle")
        self.assertTrue(why.startswith("its mail to you was sent back for revision 10m ago and hasn't gone out: "
                                       "Results (no summary first) · idle"), why)
        self.assertEqual(classify.classify(agent(raw_status="busy", mail={"revise": rev}), NOW), ("busy", "turn running"))
        collab = {"ts": NOW - 120, "subject": "Data", "reasons": "", "to": "collaborator"}
        why = classify.classify(agent(mail={"revise": [collab] + rev}), NOW)[1]
        self.assertTrue(why.startswith("its mail to a collaborator was sent back for revision 2m ago and hasn't gone "
                                       "out: Data (and 1 more held) · "), why)
        # stopped for a credential: shown until a turn ends well after it (nothing will send it)
        key = {"ts": NOW - 300, "subject": "Keys", "reasons": "", "to": "owner", "secret": True}
        why = classify.classify(agent(mail={"revise": [key]}), NOW)[1]
        self.assertTrue(why.startswith("its mail to you was stopped 5m ago (it looked like it had a credential in it) "
                                       "and won't go out unless re-sent: Keys · idle"), why)
        old = dict(key, ts=NOW - 2000)                                   # last turn ended 1940 s after it
        self.assertEqual(classify.classify(agent(mail={"revise": [old]}), NOW)[1], "idle 60s")
        # an older mail still unanswered: waiting, and the held one is said first
        st, why = classify.classify(agent(last_turn_end=NOW - 3000, mail={
            "revise": rev, "unanswered": True, "last_out": NOW - 3600, "last_out_subject": "Plan"}), NOW)
        self.assertEqual(st, "waiting")
        self.assertIn("sent back for revision 10m ago", why)
        self.assertTrue(why.endswith("no reply yet: Plan"), why)
        st, why = classify.classify(agent(last_turn_end=NOW - 3000, mail={
            "unrevised": True, "unanswered": True, "last_out": NOW - 60, "last_out_subject": "Results"}), NOW)
        self.assertEqual(st, "waiting")
        self.assertTrue(why.endswith("(sent as-is: it didn't revise it after the pre-send review)"), why)

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

    def test_a_signed_out_login_says_which_account_to_sign_in_to(self):
        st, why = classify.classify(agent(error={"ts": NOW - 30, "text": "authentication_failed: "}, last_turn_end=NOW - 40,
                                          account="work", account_label="Work seat"), NOW)
        self.assertEqual(st, "blocked")
        self.assertEqual(why, 'its Claude login "work" (Work seat) is signed out: type /login in its window '
                              'and choose the Work seat account, not an account another login uses')
        why = classify.classify(agent(error={"ts": NOW - 30, "text": "authentication_failed: "}, last_turn_end=NOW - 40,
                                      account=""), NOW)[1]
        self.assertIn('"default" is signed out', why)
        self.assertIn("the account this login is for", why)
        why = classify.classify(agent(cli="codex", error={"ts": NOW - 30, "text": "authentication_failed: x"},
                                      last_turn_end=NOW - 40), NOW)[1]
        self.assertIn("API error", why)

    def test_a_limit_its_transcript_does_not_end_on_is_not_blocked(self):
        # a StopFailure 429 a second after the agent's final answer; nothing failed in its conversation
        lim = {"ts": NOW - 60, "kind": "limit", "text": 'rate_limit: 429 {"type":"error","error":{"type":"rate_limit_error",'
                                                         '"message":"Usage credits are required for long context requests."}'}
        st, why = classify.classify(agent(quota_ts=NOW - 60, last_turn_end=NOW - 61, limit=lim,
                                          stop_unconfirmed=NOW - 60), NOW)
        self.assertEqual(st, "idle")
        self.assertIn("its turn finished; a request after it failed (rate_limit: Usage credits are required", why)
        self.assertEqual(self.state(quota_ts=NOW - 60, last_turn_end=NOW - 61, limit=lim), "blocked")

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

    def test_a_red_star_when_on_work_while_default_has_room(self):
        order, thr = ["default", "work"], {"session": 10, "weekly": 5}
        def acc(session=97.0, weekly=98.0, fable=98.0):
            return [{"target": "claude:default", "windows": [
                {"kind": "session", "name": "5-hour", "remaining_pct": session},
                {"kind": "weekly", "name": "weekly", "remaining_pct": weekly},
                {"kind": "model_weekly", "name": "weekly Fable", "remaining_pct": fable, "model": "fable"}]}]
        note = lambda a, u=None: classify.login_note(a, acc() if u is None else u, order, thr, NOW)
        moved = agent(account="work", model="claude-fable-5-1", raw_status="shell",
                      failover={"account": "work", "ts": NOW - 7200})
        why = note(moved)
        self.assertIn("Running on work while default has room for claude-fable-5-1", why)
        self.assertIn("weekly Fable 98% left", why)
        self.assertIn("moved it here 2.0h ago", why)
        self.assertIn("isn't idle yet", why)
        started = agent(account="work", model="claude-opus-5-5")
        self.assertIn("didn't move it there", note(started))
        self.assertNotIn("Fable", note(started))                       # Opus doesn't use the Fable window
        self.assertIsNone(note(moved, acc(fable=3.0)))                 # default's Fable is under 5%
        self.assertIsNotNone(note(started, acc(fable=0.0)))            # ...which an Opus agent doesn't need
        self.assertIsNone(note(started, acc(session=9.0)))             # under the 10% the mailroom routes at
        self.assertIsNone(note(agent(account="work"), acc(fable=0.0)))   # model unknown: every window must have room
        self.assertIsNone(note(agent(account="default")))
        self.assertIsNone(note(agent(account="work", live=False)))
        self.assertIsNone(note(agent(account="work", cli="codex")))
        self.assertIsNone(note(started, []))                            # no usage reading: no star
        self.assertIsNone(note(started, [dict(acc()[0], blocked=["blocked"])]))
        upper = [{"target": "claude:default", "windows": [
            {"kind": "session", "remaining_pct": 90.0}, {"kind": None, "remaining_pct": 90.0},
            {"kind": "model_weekly", "name": "weekly Fable", "remaining_pct": 2.0, "model": "Fable"}]}]
        self.assertIsNone(note(moved, upper))                          # "Fable" matches claude-fable-5-1

    def test_claude_routing_reads_the_mailroom_order(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps({"usage": {"claude_accounts": {"default": {}, "work": {}},
                                                 "order": ["claude:work", "codex", "claude:default", "claude:x"],
                                                 "min_remaining": {"session": 20}}}))
            self.assertEqual(sources.claude_routing(cfg), (["work", "default"], {"session": 20, "weekly": 5}))
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
        """fixtures.gmail_of for two mails, g2 unread (self.unread, which a test may change)."""
        self.unread = {"g2"}
        return fixtures.gmail_of(me, {"g1": "g1@mail.gmail.com", "g2": "g2@mail.gmail.com"}, set(), self.unread)

    def test_gmail_read_state(self):
        g, calls = self.fake_gmail()
        for m in ("<G1@mail.gmail.com>", "g2@MAIL.gmail.com", "<never-delivered@x>"):
            self.assertIsNone(g.is_read(m))                             # first ask: unknown, now wanted
        g.maybe_refresh(force=True)                                    # sync: checked in this thread
        self.assertTrue(g.status["ok"], g.status)
        self.assertIs(g.is_read("<G1@mail.gmail.com>"), True)
        self.assertEqual(g.thread_of("<G1@mail.gmail.com>"), "t-g1")              # for a direct Gmail link
        self.assertIs(g.is_read("g2@MAIL.gmail.com"), False)
        self.assertIsNone(g.is_read("<never-delivered@x>"))             # not in the mailbox yet: not "read"
        lookups = len([u for _, u, _, _ in calls if "rfc822msgid" in u])
        self.unread.clear()
        self.unread.add("g1")                                            # g2 read, g1 marked unread again
        g.maybe_refresh(force=True)
        self.assertIs(g.is_read("g2@mail.gmail.com"), True)
        self.assertIs(g.is_read("g1@mail.gmail.com"), False)
        self.assertEqual(len([u for _, u, _, _ in calls if "rfc822msgid" in u]), lookups + 1)   # only the undelivered one again
        self.assertTrue(all("AT-secret" not in u for _, u, _, _ in calls))                     # token never in a URL
        self.assertTrue(any("from%3Aagents%40example.com" in u for _, u, _, _ in calls))

    def test_refuses_someone_elses_login_and_degrades(self):
        g, _ = self.fake_gmail(me="agents@example.com")
        g.maybe_refresh(force=True)
        self.assertFalse(g.status["ok"])
        self.assertIn("not the owner's", g.status["error"])
        self.assertIsNone(g.is_read("<G1@mail.gmail.com>"))
        missing = gmailstate.GmailState("a@b", [OWNER], creds_path="/nonexistent/creds.json", sync=True)
        status = missing.maybe_refresh(NOW)                                 # sync: checked in this thread
        self.assertEqual((status["ok"], status["error"]),
                         (False, "RuntimeError: no Gmail login at /nonexistent/creds.json"))


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
        self.assertEqual(classify.mail_state(sent2, reply, NOW)["last_out_thread"], "t2")   # the Mail tab opens it
        self.assertEqual(classify.mail_state([], reply, NOW)["last_out_thread"], "")

    def test_old_mail_ignored_and_held_counted(self):
        old = [{"sent_at": NOW - 8 * 86400, "subject": "x", "thread_id": "t"}]
        self.assertFalse(classify.mail_state(old, [], NOW)["unanswered"])
        held = [{"received_at": NOW - 10, "subject": "Re", "thread_id": "t", "action": "held"}]
        self.assertEqual(classify.mail_state([], held, NOW)["held"], 1)

    def test_review_held_and_sent_as_is(self):
        held = {"created_at": NOW - 120, "verdict": "revise", "subject": "Results [#abcd1234]",
                "reasons": "no summary first", "has_draft": 1, "audience": "owner"}
        self.assertEqual(classify.mail_state([], [], NOW, reviews=[held])["revise"],
                         [{"ts": NOW - 120, "subject": "Results", "reasons": "no summary first", "to": "owner",
                           "secret": False}])
        stopped = dict(held, has_draft=0, kind="secret", reasons=None)      # a credential stop keeps no draft
        self.assertEqual([(r["secret"], r["reasons"]) for r in classify.mail_state([], [], NOW, reviews=[stopped])["revise"]],
                         [(True, "")])
        self.assertEqual(classify.mail_state([], [], NOW, reviews=[dict(held, has_draft=0)])["revise"], [])
        self.assertEqual(classify.mail_state([], [], NOW, reviews=[dict(held, verdict="pass")])["revise"], [])
        self.assertEqual(classify.mail_state([], [], NOW)["revise"], [])
        two = classify.mail_state([], [], NOW, reviews=[held, dict(held, created_at=NOW - 60, audience="collaborator")])
        self.assertEqual([r["to"] for r in two["revise"]], ["collaborator", "owner"])           # newest first
        sent = [{"sent_at": NOW - 30, "subject": "Results", "thread_id": "t1", "message_id": "<m1@x>"}]
        expired = {"created_at": NOW - 30, "verdict": "expired", "subject": "Results", "sent_message_id": "<m1@x>"}
        self.assertTrue(classify.mail_state(sent, [], NOW, reviews=[expired])["unrevised"])
        self.assertTrue(classify.mail_state(sent, [], NOW, reviews=[dict(held, audience="other"), expired])["unrevised"])
        self.assertFalse(classify.mail_state(sent, [], NOW, reviews=[dict(expired, sent_message_id="<m0@x>")])["unrevised"])
        self.assertFalse(classify.mail_state(sent, [], NOW)["unrevised"])

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
        self.assertEqual(classify.display_name({"name": "nightly-runner"}), "nightly-runner")
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

        def runner(slug):           # gh asks GitHub over the network: its answers are given here
            calls.append(slug)
            return [{"number": 1, "headRefName": "feat", "state": "MERGED"}, {"number": 3, "headRefName": "feat", "state": "OPEN"}]
        prs = sources.PullRequests(runner=runner, sync=True)
        self.assertEqual(prs.lookup("o/r", "feat", NOW)["number"], 3)       # newest PR for the branch
        self.assertIsNone(prs.lookup("o/r", "other", NOW))
        self.assertEqual(calls, ["o/r"])                                     # one call per repo per ttl

        fail = []

        def broken(slug):           # ...and here it fails as a logged-out gh does
            fail.append(slug)
            raise RuntimeError("gh not logged in")
        bad = sources.PullRequests(runner=broken, sync=True)
        self.assertIsNone(bad.lookup("o/r", "feat", NOW))
        self.assertEqual(bad.error, "o/r: RuntimeError: gh not logged in")      # shown, not just an empty column
        bad.lookup("o/r", "feat", NOW + 3)                                   # next poll: no retry yet
        self.assertEqual(len(fail), 1)
        bad.lookup("o/r", "feat", NOW + sources.PullRequests.RETRY + 1)      # retried after a minute
        self.assertEqual(len(fail), 2)
        prs.runner = broken                                                  # a failure keeps the last good table
        self.assertEqual(prs.lookup("o/r", "feat", NOW + prs.ttl + 1)["number"], 3)
        prs.runner = runner
        prs.lookup("o/r", "feat", NOW + prs.ttl + prs.RETRY + 2)
        self.assertIsNone(prs.error)                                         # cleared once gh answers again

    def test_a_failing_gh_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            gh = Path(d) / "gh"            # gh talks to GitHub over the network: a stand-in on PATH fails like it
            gh.write_text("#!/bin/sh\necho 'To get started with GitHub CLI, please run:  gh auth login' >&2\nexit 4\n")
            gh.chmod(0o755)
            real = os.environ["PATH"]
            os.environ["PATH"] = d + os.pathsep + real
            try:
                prs = sources.PullRequests(sync=True)
                self.assertIsNone(prs.lookup("o/r", "feat", NOW))
            finally:
                os.environ["PATH"] = real
            self.assertIn("o/r: RuntimeError: gh pr list exit 4: To get started", prs.error)


def codex_home(home, threads, held=()):
    """A Codex home in folder `home`: state_5.sqlite with a threads table holding `threads` (dicts of its columns; the
    rest default to a user's thread in /x), and a locked writer lock file for each thread id in `held`, as a running
    Codex keeps one. Returns the open lock files: closing them is that Codex exiting."""
    import fcntl
    home.mkdir(parents=True, exist_ok=True)
    (home / "thread-writer-locks").mkdir()
    con = sqlite3.connect(str(home / "state_5.sqlite"))
    con.execute("create table threads(id, name, title, cwd, git_branch, rollout_path, updated_at, model, "
                "reasoning_effort, created_at, archived, source, thread_source, agent_nickname)")
    for t in threads:
        row = dict({"name": "", "title": "", "cwd": "/x", "git_branch": "", "rollout_path": "", "model": "m",
                    "reasoning_effort": "x", "archived": 0, "source": "cli", "thread_source": "user",
                    "agent_nickname": None}, **t)
        con.execute("insert into threads(%s) values(%s)" % (", ".join(row), ", ".join("?" * len(row))), list(row.values()))
    con.commit()
    con.close()
    handles = []
    for tid in held:
        fh = open(home / "thread-writer-locks" / (tid + ".lock"), "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        handles.append(fh)
    return handles


class GitRepoTest(unittest.TestCase):
    def git(self, d, *a):
        fixtures.git("-C", str(d), *a)

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

    def test_board_git_never_rewrites_an_agents_index(self):
        with tempfile.TemporaryDirectory() as d:
            self.git(d, "init", "-q", "-b", "main")
            f = Path(d) / "a.txt"
            f.write_text("x")
            self.git(d, "add", "a.txt")
            self.git(d, "commit", "-q", "-m", "first")
            st = os.stat(f)
            os.utime(f, (st.st_atime + 5, st.st_mtime + 5))      # stale stat info: a plain `git status` rewrites the index
            index = Path(d) / ".git" / "index"
            before = index.stat().st_mtime_ns
            time.sleep(0.01)
            self.assertEqual(sources.git_facts(d)["dirty"], 0)
            self.assertEqual(index.stat().st_mtime_ns, before)   # so an agent's commit can't meet our index.lock

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
                "default": {"config_dir": None}, "work": {"config_dir": d + "/work"}}}}))
            self.assertEqual([(n, str(p)) for n, p in sources.claude_logins(cfg)],
                             [("default", str(sources.DEFAULT_CLAUDE_HOME)), ("work", d + "/work")])
            self.assertEqual([n for n, _ in sources.claude_logins(Path(d) / "missing.json")], ["default"])
            # null means ~/.claude even if this process runs under another login's CLAUDE_CONFIG_DIR
            real = sources.CLAUDE_HOME
            sources.CLAUDE_HOME = Path(d) / "work"
            try:
                self.assertEqual([(n, str(p)) for n, p in sources.claude_logins(cfg)],
                                 [("default", str(sources.HOME / ".claude")), ("work", d + "/work")])
            finally:
                sources.CLAUDE_HOME = real

    def test_subagent_found_by_shared_lock_holder(self):
        if not Path("/proc/locks").exists():
            self.skipTest("needs /proc/locks")
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            parent, sub = "0190bbbb-0000-7000-8000-00000000b001", "0190bbbb-1111-7000-8000-00000000b002"
            handles = codex_home(home, [{"id": parent, "name": "garden", "updated_at": int(NOW), "created_at": int(NOW)},
                                        {"id": sub, "updated_at": int(NOW), "created_at": int(NOW),
                                         "source": '{"subagent": "review"}', "thread_source": "subagent",
                                         "agent_nickname": "Darwin"}], held=(parent, sub))
            try:
                rows = {r["id"]: r for r in sources.CodexReader(home).sessions(now=NOW)}
            finally:
                for fh in handles:
                    fh.close()
            self.assertEqual(rows[sub]["parent"], parent)
            self.assertEqual(rows[parent]["parent"], "")


class StuckWaitTest(unittest.TestCase):
    SNAP = "/bin/bash -c source /home/alex/.claude/shell-snapshots/snapshot-bash-1-x.sh 2>/dev/null || true && eval '%s'"

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


class StuckMailTest(unittest.TestCase):
    """An agent's `agent-mail send` blocked on an input that never ends (a mail to the owner can be held for hours
    while the board says only "background") makes the agent blocked."""

    def test_a_real_send_blocked_on_a_socket_is_found(self):
        import socket
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "agent-mail"
            script.write_text("import sys\nsys.stdin.read()\n")
            a, b = socket.socketpair()
            p = subprocess.Popen([sys.executable, str(script), "send", "--to", "x@y", "--subject", "Pick A or B"], stdin=a)
            try:
                deadline = time.time() + 5
                while time.time() < deadline and Path("/proc/%d/wchan" % p.pid).read_text().strip() != "unix_stream_data_wait":
                    time.sleep(0.05)
                found = sources.stuck_mail([os.getpid()], stuck_after=0)
                self.assertEqual([(m["pid"], m["why"], m["subject"]) for m in found[os.getpid()]], [(p.pid, "input", "Pick A or B")])
                self.assertEqual(sources.stuck_mail([os.getpid()], stuck_after=3600), {})     # not stuck long enough yet
                other = subprocess.Popen(["sleep", "30"])
                try:                                                                         # someone else's agent
                    self.assertEqual(sources.stuck_mail([other.pid], stuck_after=0), {})
                finally:
                    other.kill()
                    other.wait()
            finally:
                p.kill()
                p.wait()
                a.close()
                b.close()

    def test_subject_of_a_reply(self):
        self.assertEqual(sources._mail_subject(["python3", "/x/agent-mail", "reply", "1163", "--html", "a.html"]), "reply 1163")
        self.assertEqual(sources._mail_subject(["python3", "/x/agent-mail", "send", "--subject=Hi"]), "Hi")

    def test_blocked_even_while_busy(self):
        st, why = classify.classify(agent(raw_status="busy", mail_stuck=[
            {"pid": 1032069, "age": 75600, "subject": "Release plan: pick A/B/C", "why": "input"}]), NOW)
        self.assertEqual(st, "blocked")
        self.assertTrue(why.startswith("its mail to you \u201cRelease plan: pick A/B/C\u201d never left: agent-mail has waited 21.0h"), why)
        self.assertIn("pid 1032069", why)
        st, why = classify.classify(agent(raw_status="shell", mail_stuck=[
            {"pid": 7, "age": 1200, "subject": "", "why": "slow"}, {"pid": 8, "age": 950, "subject": "", "why": "slow"}]), NOW)
        self.assertEqual((st, why), ("blocked", "its mail to you has been sending for 20m (agent-mail pid 7) (and 1 more)"))


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
                        (tid, "nova", "t", "/x", "b", "", int(NOW), "m", "x", int(NOW - 99)))
            con.commit()
            con.close()
            if Path("/proc/locks").exists():
                rows = sources.CodexReader(home).sessions(now=NOW)
                self.assertEqual([(r["name"], r["live"]) for r in rows], [("nova", False)])
                import fcntl
                with open(home / "thread-writer-locks" / (tid + ".lock")) as fh:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                    rows = sources.CodexReader(home).sessions(now=NOW)
                    self.assertTrue(rows[0]["live"])


class SshPeerTest(unittest.TestCase):
    CONN = "198.51.100.20 59853 192.0.2.10 22"
    LOCAL, REMOTE = "0A0200C0:0016", "146433C6:E9CD"         # as /proc/net/tcp writes the two ends above

    def gone(self, rows, conn=CONN, name="tcp"):
        with tempfile.TemporaryDirectory() as d:
            head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            (Path(d) / name).write_text(head + "".join(
                "%4d: %s %s %s %08X:00000000 01:00000020 %08X     0        0 1 1 0 20 4 30 10 -1\n" % ((i,) + r)
                for i, r in enumerate(rows)))
            return sources.ssh_peer_gone(conn, tables=(str(Path(d) / "tcp"), str(Path(d) / "tcp6")))

    def test_asleep_only_with_data_in_flight_and_many_timeouts(self):
        mine = lambda st="01", tx=0x2A0, rt=8: (self.LOCAL, self.REMOTE, st, tx, rt)
        self.assertTrue(self.gone([mine()]))
        self.assertTrue(self.gone([("0100007F:0016", "0100007F:9999", "01", 0, 0), mine(rt=12)]))
        self.assertFalse(self.gone([mine(rt=7)]))                 # a few lost packets: not yet
        self.assertFalse(self.gone([mine(tx=0, rt=9)]))           # nothing waiting for an acknowledgement
        self.assertFalse(self.gone([mine(st="08")]))              # not established
        self.assertFalse(self.gone([("0A0200C0:0016", "146433C6:E9CE", "01", 1, 15)]))   # someone else's
        self.assertFalse(self.gone([mine()], conn=""))            # not over SSH
        self.assertFalse(self.gone([mine()], conn="nonsense 1 2"))
        self.assertFalse(self.gone([]))

    def test_ipv4_client_on_an_ipv6_listener(self):
        mapped = lambda w: "0000000000000000FFFF0000" + w      # ::ffff:a.b.c.d
        row = (mapped("0A0200C0") + ":0016", mapped("146433C6") + ":E9CD", "01", 5, 9)
        self.assertTrue(self.gone([row], name="tcp6"))

    def test_real_table_parses(self):
        if os.path.exists("/proc/net/tcp"):
            self.assertIn(sources.ssh_peer_gone("127.0.0.1 1 127.0.0.1 2"), (False,))


class StreamInboxTest(unittest.TestCase):
    def test_lines_in_one_packet_and_eof(self):
        import importlib.machinery
        import importlib.util
        path = str(Path(__file__).resolve().parent.parent / "bin" / "agent-board")
        spec = importlib.util.spec_from_loader("agent_board_cli", importlib.machinery.SourceFileLoader("agent_board_cli", path))
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        r, w = os.pipe()
        try:
            inbox = cli.Inbox(r)
            os.write(w, b'{"cmd":"act","rid":1}\n{"cmd":"refresh"}\n\n{"cmd":"pi')     # two lines and a half
            self.assertEqual(inbox.read(), ([b'{"cmd":"act","rid":1}', b'{"cmd":"refresh"}'], False))
            os.write(w, b'ng"}\n')
            self.assertEqual(inbox.read(), ([b'{"cmd":"ping"}'], False))
            inbox.MAX_LINE = 10                                         # a runaway line is dropped whole...
            os.write(w, b'{"cmd":"act","rid":2}\n' + b"x" * 40)
            self.assertEqual(inbox.read(), ([b'{"cmd":"act","rid":2}'], False))    # ...keeping the line before it
            os.write(w, b'xxxx\n{"cmd":"refresh"}\n')
            self.assertEqual(inbox.read(), ([b'{"cmd":"refresh"}'], False))        # and the one after
            os.close(w)
            w = None
            self.assertEqual(inbox.read(), ([], True))
        finally:
            os.close(r)
            if w is not None:
                os.close(w)


class CommandHelpersTest(unittest.TestCase):
    def test_parser_takes_its_description_and_help_from_the_docstring(self):
        ap = cli.parser("tool: does one thing.\n\n    tool run    runs it\n", "tool")
        self.assertEqual((ap.prog, ap.description), ("tool", "tool: does one thing."))
        self.assertIn("    tool run    runs it", ap.format_help())

    def test_out_prints_json_or_hands_the_result_to_the_human_printer(self):
        printed, shown = io.StringIO(), []
        with contextlib.redirect_stdout(printed):
            cli.out({"text": "café", "at": Path("/x")}, True, shown.append)
        self.assertEqual((json.loads(printed.getvalue()), shown), ({"text": "café", "at": "/x"}, []))
        self.assertIn("café", printed.getvalue())                     # the owner's text as written
        cli.out({"a": 1}, False, shown.append)
        self.assertEqual(shown, [{"a": 1}])


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
    con = fixtures.mailroom_db(path)
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

    def test_newest_review_per_tag_and_audience(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "mailroom.sqlite"
            sid_a, sid_b = mail_fixture(db)
            self.assertNotIn("reviews", sources.MailStore(db, owners=[OWNER]).read(NOW)["by_session"][("claude", sid_a)])
            con = sqlite3.connect(str(db))          # the mailroom's schema (agent-mailroom agentmail/store.py)
            con.executescript("""
              create table reviews(id integer primary key, tag text not null, created_at integer not null,
                                   round integer not null, verdict text not null, subject text, kind text,
                                   sent_message_id text, reasons text, fixes text, words integer, model text,
                                   seconds real, cost real, draft text, audience text);
              create index reviews_created_at on reviews(created_at);
            """)
            con.executemany("insert into reviews(tag, created_at, round, verdict, subject, reasons, draft, audience) "
                            "values(?,?,?,?,?,?,?,?)", [
                ("taga", NOW - 700, 1, "pass", "Alpha done", None, None, None),
                ("taga", NOW - 120, 1, "revise", "Alpha v2", "no summary first; plot attached, not inline", "{}", None),
                ("taga", NOW - 60, 1, "pass", "To Sam", None, None, "collaborator"),   # doesn't settle the owner's draft
                ("tagb", NOW - 400, 1, "revise", "Beta", "too long", None, "owner"),
                ("tagb", NOW - 300, 1, "withdrawn", "Beta", None, None, "owner"),
                ("tagb", NOW - 200, 1, "revise", "Key", "Contains what looks like a GitHub token ('ghp_ab...')", None,
                 "collaborator"),
                ("tagm", NOW - 60, 1, "revise", "Mac", "x", "{}", None)])
            con.execute("update reviews set kind = 'secret' where subject = 'Key'")
            con.commit()
            con.close()
            got = sources.MailStore(db, owners=[OWNER]).read(NOW)["by_session"]
            ra = {r["audience"]: r for r in got[("claude", sid_a)]["reviews"]}
            self.assertEqual((ra["owner"]["verdict"], ra["owner"]["has_draft"], ra["owner"]["subject"]), ("revise", 1, "Alpha v2"))
            self.assertEqual(ra["collaborator"]["verdict"], "pass")
            rb = {r["audience"]: r for r in got[("codex", sid_b)]["reviews"]}
            self.assertEqual(rb["owner"]["verdict"], "withdrawn")
            self.assertEqual((rb["collaborator"]["kind"], rb["collaborator"]["reasons"]), ("secret", None))  # never read
            b = got[("codex", sid_b)]
            self.assertEqual([(r["subject"], r["secret"], r["reasons"]) for r in classify.mail_state(
                b["sent"], b["inbound"], NOW, reviews=b["reviews"])["revise"]], [("Key", True, "")])
            self.assertNotIn(("claude", "c" * 36), got)                         # Mac-hosted session excluded
            a = got[("claude", sid_a)]
            self.assertEqual([r["subject"] for r in classify.mail_state(a["sent"], a["inbound"], NOW,
                                                                        reviews=a["reviews"])["revise"]], ["Alpha v2"])

    def test_missing_store_is_not_an_error(self):
        got = sources.MailStore(Path("/nonexistent/mailroom.sqlite"), owners=[OWNER]).read(NOW)
        self.assertFalse(got["ok"])
        self.assertEqual(got["by_session"], {})
        self.assertNotIn("error", got)
        self.assertFalse(Path("/nonexistent/mailroom.sqlite").exists())

    def test_a_reviews_query_failing_for_another_reason_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "mailroom.sqlite"
            mail_fixture(db)
            con = sqlite3.connect(str(db))
            con.execute("create table reviews(id integer primary key, tag text)")     # a mailroom with another schema
            con.commit()
            con.close()
            got = sources.MailStore(db, owners=[OWNER]).read(NOW)
            self.assertFalse(got["ok"])
            self.assertIn("no such column", got["error"])


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
            repo = d / "proj"                    # alpha works on branch feat with two files changed
            fixtures.git("init", "-q", "-b", "feat", str(repo))
            for f in ("a.txt", "b.txt"):
                (repo / f).write_text("x")
            fixtures.git("-C", str(repo), "add", "a.txt", "b.txt")
            fixtures.git("-C", str(repo), "commit", "-q", "-m", "first")
            for f in ("a.txt", "b.txt"):
                (repo / f).write_text("y")
            # Codex: garden and its subagent Beauvoir running (their lock files held), nova exited
            parent = "0190bbbb-0000-7000-8000-00000000b001"
            thread = {"cwd": str(d), "created_at": int(NOW - 900), "updated_at": int(NOW - 100)}
            locks = codex_home(d / "codex", [
                dict(thread, id=parent, name="garden"),
                dict(thread, id="0190cccc-0000-7000-8000-00000000c001", thread_source="subagent",
                     agent_nickname="Beauvoir", source=json.dumps({"subagent": {"thread_spawn": {
                         "parent_thread_id": parent, "depth": 1}}})),
                dict(thread, id="0190aaaa-0000-7000-8000-00000000a001", name="nova", updated_at=int(NOW - 3600))],
                held=(parent, "0190cccc-0000-7000-8000-00000000c001"))
            for fh in locks:
                self.addCleanup(fh.close)
            evs.append({"ts": NOW - 3600, "event": "note", "session_id": "0190aaaa-0000-7000-8000-00000000a001",
                        "text": "phase 1 done", "cli": "codex", "name": "nova"})
            evs.append({"ts": NOW - 30, "event": "idle", "session_id": "0190cccc-0000-7000-8000-00000000c001",
                        "cli": "codex"})          # a subagent's own hook event must not become a ghost tile
            evs.append({"ts": NOW - 20, "event": "failover", "session_id": sid_a, "cli": "claude",
                        "account": "work", "text": "default -> work (limit: five-hour limit, resets 17:29)"})
            log.write_text("".join(json.dumps(e) + "\n" for e in evs))
            col = fixtures.collector(
                d, claude=lambda: [fixtures.session(sid_a, "alpha", NOW - 590, cwd=str(repo), tmux="alpha:@1.%1",
                                                    started_at=NOW - 4000)],
                panes=lambda: [(4999991, "nova", str(d)), (4999992, "alpha", "/somewhere/else")])
            snap = col.snapshot(NOW)
            by = {a["display"]: a for a in snap["agents"]}
            self.assertEqual(sorted(by), ["alpha", "garden", "nova"])   # no orchestrator, probe or subagent tiles
            self.assertEqual([x["nickname"] for x in by["garden"]["subagents"]], ["Beauvoir"])
            self.assertEqual(by["nova"]["left_behind"], {"cwd_exists": True, "tmux": "nova"})
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
            reading = {"accounts": [{"target": "claude:default", "windows": [
                {"kind": "session", "name": "5-hour", "remaining_pct": 90.0}]}]}
            # agent-usage asks the providers over the network: its reading is given here
            usage = sources.Usage(d / "no-cache.json", runner=lambda force: reading, annotate=None, sync=True)
            cfg = d / "mail-config.json"         # the mailroom's routing: default first, then work
            cfg.write_text(json.dumps({"usage": {"claude_accounts": {"default": {}, "work": {}}}}))
            col = fixtures.collector(
                d, claude=lambda: [fixtures.session(sid, "fab", NOW - 3000, tmux="fab:@1.%1", account="work",
                                                    account_default=False)],
                usage=usage, routing=lambda: sources.claude_routing(cfg))
            col.models.memo[(sid, None)] = ("claude-opus-5-5", NOW, None)    # read before: no live transcript needed
            col.models.memo[("gone", 1)] = ("x", NOW, None)
            snap = col.snapshot(NOW)
            a = snap["agents"][0]
            self.assertEqual(a["model"], "claude-opus-5-5")
            self.assertIn("Running on work while default has room", a["login_note"])
            self.assertEqual(list(col.models.memo), [(sid, None)])     # agents no longer there are forgotten
            self.assertEqual(snap["problems"], [])

    def test_a_stuck_background_wait_reads_idle(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            sid = "11111111-2222-3333-4444-555555555555"
            log = d / "events.jsonl"
            log.write_text(json.dumps({"ts": NOW - 4000, "event": "start", "session_id": sid, "cli": "claude",
                                       "name": "todo", "cwd": "/x"}) + "\n")
            # the agent's processes, as the live /proc would list them: a loop waiting on a pgrep that finds itself
            loop = StuckWaitTest.SNAP % 'until ! pgrep -f "stage x"; do sleep 15; done'
            procs = {4242: (1, ["claude", "--resume", sid]), 4243: (4242, loop.split(" ", 2))}
            calls = []

            def waits(pids):
                calls.append(list(pids))
                return sources.stuck_waits(pids, procs)
            col = fixtures.collector(d, claude=lambda: [fixtures.session(sid, "todo", NOW - 3000, raw_status="shell",
                                                                         pid=4242, tmux="todo:@1.%1")], waits=waits)
            a = col.snapshot(NOW)["agents"][0]
            self.assertEqual(a["state"], "idle")
            self.assertIn("`stage x` can never end", a["reason"])
            col.snapshot(NOW + 5)
            self.assertEqual(calls, [[4242]])                        # /proc is read at most every 30 s
            col.snapshot(NOW + 40)
            self.assertEqual(len(calls), 2)

    def test_a_limit_is_checked_against_the_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            sid = "11111111-2222-3333-4444-555555555555"
            log = d / "events.jsonl"
            base = {"session_id": sid, "cli": "claude", "name": "interp", "cwd": "/x"}
            log.write_text("".join(json.dumps(dict(base, **e)) + "\n" for e in (
                {"ts": NOW - 4000, "event": "start"}, {"ts": NOW - 100, "event": "idle"},
                {"ts": NOW - 99, "event": "limit", "text": "rate_limit: 429 long context"})))
            claude = lambda: [fixtures.session(sid, "interp", NOW - 100, pid=4242)]
            # its transcript, in a login folder here rather than the live ~/.claude: the turn finished
            transcript = d / "login" / "projects" / "-x" / (sid + ".jsonl")
            transcript.parent.mkdir(parents=True)
            transcript.write_text("".join(json.dumps(e) + "\n" for e in (
                {"type": "user", "message": {"content": "go"}}, {"type": "assistant", "message": {"content": "done"}})))
            calls = []

            def stops(s):
                calls.append(s)
                return sources.last_turn_failed(s)
            real = sources.claude_logins
            sources.claude_logins = lambda: [("default", d / "login")]
            try:
                col = fixtures.collector(d, claude=claude, stops=stops)
                self.assertEqual(col.snapshot(NOW - 80)["agents"][0]["state"], "blocked")   # under 30 s: not read yet
                self.assertEqual(calls, [])
                a = col.snapshot(NOW)["agents"][0]
                self.assertEqual(a["state"], "idle")
                self.assertIn("a request after it failed (rate_limit: 429 long context)", a["reason"])
                col.snapshot(NOW + 5)
                self.assertEqual(calls, [sid])                                  # read once per event
                with transcript.open("a") as fh:                                # the turn itself failed
                    fh.write(json.dumps({"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit"}) + "\n")
                col2 = fixtures.collector(d, claude=claude)
                self.assertEqual(col2.snapshot(NOW)["agents"][0]["state"], "blocked")
            finally:
                sources.claude_logins = real
            calls[:] = []

            def unreadable(s):      # a transcript gone between listing and reading raises: a race no test can time
                calls.append(s)
                raise OSError("transcript unreadable")
            col3 = fixtures.collector(d, claude=claude, stops=unreadable)
            snap = col3.snapshot(NOW)
            self.assertEqual(snap["agents"][0]["state"], "blocked")                  # unreadable: still shown blocked
            self.assertEqual(snap["problems"],
                             [{"source": "last turn of interp", "error": "OSError: transcript unreadable"}])
            self.assertEqual(col3.snapshot(NOW + 60)["problems"], snap["problems"])  # still listed, not asked again
            col3.snapshot(NOW + 301)
            self.assertEqual(calls, [sid, sid])                                      # asked again after 5 minutes

    def test_failing_sources_are_listed_and_the_board_stays_up(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            sid = "11111111-2222-3333-4444-555555555555"
            log = d / "events.jsonl"
            base = {"session_id": sid, "cli": "claude", "name": "fab", "cwd": "/x"}
            log.write_text("".join(json.dumps(dict(base, **e)) + "\n" for e in (
                {"ts": NOW - 4000, "event": "start"}, {"ts": NOW - 100, "event": "idle"},
                {"ts": NOW - 99, "event": "limit", "text": "rate_limit: 429"})))
            repo = d / "proj"                   # fab's repo, on branch feat, with its origin on GitHub: o/r
            fixtures.git("init", "-q", "-b", "feat", str(repo))
            fixtures.git("-C", str(repo), "remote", "add", "origin", "https://github.com/o/r.git")
            claude = lambda: [fixtures.session(sid, "fab", NOW - 100, cwd=str(repo), pid=4242, account="work")]
            db = d / "mailroom.sqlite"
            mail_fixture(db)
            con = sqlite3.connect(str(db))
            con.execute("create table reviews(id integer primary key, tag text)")     # a mailroom with another schema
            con.commit()
            con.close()
            (d / "codex").mkdir()
            con = sqlite3.connect(str(d / "codex" / "state_5.sqlite"))
            con.execute("create table threads(id text)")
            con.commit()
            con.close()
            (d / "manifest.json").write_text("{half a file")       # the last restart's manifest, damaged
            (d / "codex-credits.json").write_text("not json")      # and the Codex credits state
            cfg = d / "mail-config.json"                          # a mailroom config whose threshold is no number
            cfg.write_text(json.dumps({"usage": {"claude_accounts": {"default": {}, "work": {}},
                                                 "min_remaining": {"session": "ten"}}}))

            def broken(why):
                def fail(*args, **kw):
                    raise RuntimeError(why)
                return fail
            reading = {"accounts": [{"target": "claude:default", "windows": [
                {"kind": "session", "name": "5-hour", "remaining_pct": 90.0}]},
                {"target": "codex", "credits": {"has": True, "balance": "12"}}]}
            # stand-ins for outside systems: agent-usage's reading, and failures a test cannot bring about in the
            # rest (gh logged out, apt-check and git hung, the agent's processes changing mid-read, its transcript in
            # the live ~/.claude unreadable)
            col = fixtures.collector(
                d, claude=claude,
                usage=sources.Usage(d / "no-cache.json", runner=lambda force: reading, sync=True,
                                    annotate=lambda accounts: credits.annotate(accounts, d / "codex-credits.json")),
                routing=lambda: sources.claude_routing(cfg),
                prs=sources.PullRequests(runner=broken("gh: not logged in"), sync=True),
                desktop=sources.Periodic(broken("apt-check hung"), 1800, {}, sync=True),
                worktrees=sources.Periodic(broken("git hung"), 600, [], sync=True),
                restored=restart.restored_activity, stops=broken("transcript unreadable"),
                stuck_mail=broken("/proc race"))
            real = (sources.model_effort, restart.MANIFEST)
            sources.model_effort = broken("no transcript")      # the model comes from /proc and the live ~/.claude
            restart.MANIFEST = d / "manifest.json"
            try:
                snap = col.snapshot(NOW)
            finally:
                sources.model_effort, restart.MANIFEST = real
            self.assertEqual(snap["agents"][0]["state"], "blocked")       # an unconfirmed limit still shows
            self.assertEqual((snap["desktop"], snap["worktrees"]), ({}, []))
            got = {p["source"]: p["error"] for p in snap["problems"]}
            self.assertEqual(got, {
                "restored agents": got["restored agents"],
                "mail": got["mail"], "Codex sessions": got["Codex sessions"],
                "last turn of fab": "RuntimeError: transcript unreadable",
                "model of fab": "RuntimeError: no transcript",
                "login note of fab": got["login note of fab"],
                "usage": got["usage"],
                "pull requests": "o/r: RuntimeError: gh: not logged in",
                "Desktop": "RuntimeError: apt-check hung", "worktrees": "RuntimeError: git hung",
                "stuck mail": "RuntimeError: /proc race"})
            self.assertIn("manifest.json is not valid JSON", got["restored agents"])
            self.assertIn("no such column", got["mail"])
            self.assertIn("no such column", got["Codex sessions"])
            self.assertIn("TypeError", got["login note of fab"])
            self.assertIn("codex-credits.json is not valid JSON", got["usage"])
            self.assertEqual(snap["usage"]["accounts"], reading["accounts"])   # the reading stands without the bar
            self.assertNotIn("credits_meter", snap["usage"]["accounts"][1])
            json.dumps(snap)

    def test_tmux_is_read_once_per_poll_for_both_views(self):
        rows = [(4999991, "nova", "/w/a"), (4999992, "nova", "/w/a/sub"), (4999993, "my session", "/w/b")]
        self.assertEqual(sources.tmux_panes(rows), {4999991: "nova", 4999992: "nova", 4999993: "my session"})
        self.assertEqual(sources.tmux_sessions(rows), {"nova": {"/w/a", "/w/a/sub"}, "my session": {"/w/b"}})
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "events.jsonl").write_text("")
            calls = []
            col = fixtures.collector(d, panes=lambda: calls.append(1) or rows)     # the live tmux server's panes
            col.agents(NOW, with_git=False)
            self.assertEqual((col.panes, col.tmux_names), (sources.tmux_panes(rows), sources.tmux_sessions(rows)))
            col.agents(NOW + 5, with_git=False)
            self.assertEqual(len(calls), 1)                              # one tmux call, both views from it
            col.refresh()
            col.agents(NOW + 6, with_git=False)
            self.assertEqual(len(calls), 2)                              # a manual refresh reads tmux again

    def test_last_turn_failed_reads_the_transcript_tail(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            sid = "11111111-2222-3333-4444-555555555555"
            (home / "projects/-x").mkdir(parents=True)
            path = home / ("projects/-x/%s.jsonl" % sid)
            ok = [{"type": "assistant", "message": {"content": "done"}}, {"type": "system", "subtype": "turn_duration"},
                  {"type": "custom-title"}]
            failed = [{"type": "assistant", "message": {"content": "done"}}, {"type": "user", "message": {"content": "go"}},
                      {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit"},
                      {"type": "user", "isMeta": True, "message": {"content": "caveat"}}]
            real = sources.claude_home_of
            sources.claude_home_of = lambda s: ("default", home)
            try:
                path.write_text("".join(json.dumps(e) + "\n" for e in ok))
                self.assertIs(sources.last_turn_failed(sid), False)
                path.write_text("".join(json.dumps(e) + "\n" for e in failed))
                self.assertIs(sources.last_turn_failed(sid), True)
                path.write_text("")
                self.assertIsNone(sources.last_turn_failed(sid))
                self.assertIsNone(sources.last_turn_failed("22222222-2222-3333-4444-555555555555"))
            finally:
                sources.claude_home_of = real


class SourceReadTest(unittest.TestCase):
    def test_periodic_reads_when_due_and_keeps_its_last_value_on_failure(self):
        answers = [1, RuntimeError("apt-check hung"), 3]

        def load():
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        p = sources.Periodic(load, 30, 0, sync=True)
        self.assertEqual((p.value, p.checked_at, p.error), (0, None, None))
        self.assertEqual(p.maybe_refresh(NOW), 1)
        self.assertEqual(p.maybe_refresh(NOW + 10), 1)                   # not due: no read
        self.assertEqual(p.maybe_refresh(NOW + 31), 1)                   # read, failed: the last value stays
        self.assertEqual(p.error, "RuntimeError: apt-check hung")
        p.invalidate()
        self.assertEqual((p.maybe_refresh(NOW + 32), p.error), (3, None))
        self.assertIsNotNone(p.checked_at)

        gate, reads = threading.Event(), []

        def slow():
            reads.append(1)
            gate.wait(5)
            return "new"
        bg = sources.Periodic(slow, 30, "old", sync=False)
        self.assertEqual(bg.maybe_refresh(NOW), "old")                   # the poll never waits for the read
        for _ in range(500):
            if reads:
                break
            time.sleep(0.01)
        bg.refresh()                                                     # one read at a time: returns at once
        gate.set()
        for _ in range(500):
            if bg.value == "new":
                break
            time.sleep(0.01)
        self.assertEqual((bg.value, len(reads)), ("new", 1))

    def test_ro_sqlite_never_creates_or_rewrites_a_database(self):
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "state_5.sqlite"
            with self.assertRaises(FileNotFoundError):
                sources.ro_sqlite(missing)
            self.assertFalse(missing.exists())                           # nothing made in another tool's folder
            junk = Path(d) / "junk.sqlite"
            junk.write_bytes(b"not a database " * 40)
            with self.assertRaises(sqlite3.DatabaseError):
                sources.ro_sqlite(junk).execute("select 1 from sqlite_master").fetchall()
            wal = Path(d) / "wal.sqlite"
            con = sqlite3.connect(str(wal))
            con.execute("pragma journal_mode=wal")
            con.execute("create table t(a)")
            con.execute("insert into t values(7)")
            con.commit()
            ro = sources.ro_sqlite(wal)
            self.assertEqual(ro.execute("select a from t").fetchone()["a"], 7)
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("insert into t values(8)")
            con.close()

    def test_gpu_readings_carry_what_the_mac_shows(self):
        m = sources.Metrics(gpu=False)

        class NvidiaSmi:        # nvidia-smi needs an NVIDIA GPU and driver: its output lines stand in for it
            stdout = ["NVIDIA Example GPU, 1024, 24576, 7, 41\n"]
        m.gpu_proc = NvidiaSmi()
        m._gpu_reader()
        self.assertEqual(m.gpus, [{"name": "NVIDIA Example GPU", "mem_used": 1024.0 * 2**20,
                                   "mem_total": 24576.0 * 2**20, "util": 7.0, "temp": 41.0}])

    def test_codex_reader_reports_a_threads_table_it_cannot_read(self):
        with tempfile.TemporaryDirectory() as d:
            con = sqlite3.connect(str(Path(d) / "state_5.sqlite"))
            con.execute("create table threads(id text)")                 # none of the columns the board reads
            con.commit()
            con.close()
            reader = sources.CodexReader(d)
            self.assertEqual(reader.sessions(now=NOW), [])
            self.assertIn("no such column", reader.error)
            self.assertIsNone(sources.CodexReader(Path(d) / "none").error)   # no Codex: nothing wrong


class LeakedEnvTest(unittest.TestCase):
    def test_an_sdk_cli_session_in_a_tmux_pane_is_an_agent(self):
        # a tmux server an orchestrator run started gives every agent CLAUDE_CODE_ENTRYPOINT=sdk-cli
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            log = d / "events.jsonl"
            log.write_text("")
            agent = fixtures.session("aaaaaaaa-2222-3333-4444-555555555555", "resumed-agent", NOW - 100, cwd=str(d),
                                     pid=4999998, entrypoint="sdk-cli")
            probe = fixtures.session("bbbbbbbb-2222-3333-4444-555555555555", "probe", NOW - 100, cwd=str(d),
                                     pid=4999999, entrypoint="sdk-cli")
            col = fixtures.collector(d, claude=lambda: [agent, probe], panes=lambda: [(4999990, "resumed-agent", str(d))])
            real = sources.ppid
            # the parent of each made-up pid, as the live /proc would give it
            sources.ppid = lambda pid: {4999998: 4999990, 4999999: 4999991}.get(pid, 0)
            try:
                rows, _, _ = col.agents(NOW, with_git=False)
            finally:
                sources.ppid = real
            # the agent's parent is the pane's shell; the probe's grandparent is (it runs from an agent's tool shell)
            self.assertEqual([a["name"] for a in rows], ["resumed-agent"])

    def test_launches_scrub_the_leaked_variables_and_tmux_forgets_them(self):
        self.assertIn("-u AGENT_ORCHESTRATOR", relaunch.SCRUB_ENV)
        self.assertIn("-u CLAUDE_CODE_ENTRYPOINT", relaunch.SCRUB_ENV)
        calls = []

        def run(cmd, stdin=None, timeout=60):       # tmux is a live server holding the agents: it answers here
            calls.append(cmd)
            if cmd[1] == "show-environment":
                return 0, "PATH=/usr/bin\nCLAUDE_CONFIG_DIR=/x\nAGENT_ORCHESTRATOR=1\nCLAUDE_CODE_ENTRYPOINT=sdk-cli\n" \
                          "CLAUDE_CODE_SOMETHING_NEW=1\n-DISPLAY\n"
            return 0, ""
        self.assertEqual(relaunch.scrub_tmux_env("tmux", run), "")
        self.assertEqual([c[-1] for c in calls if c[1] == "set-environment"],     # PATH, the login dir and a user's
                         ["AGENT_ORCHESTRATOR", "CLAUDE_CODE_ENTRYPOINT"])          # own CLAUDE_CODE_* stay
        self.assertEqual(relaunch.scrub_tmux_env("tmux", lambda cmd, timeout=0: (1, "no server")), "")

    def test_a_leaked_variable_tmux_keeps_stops_the_launch(self):
        # tmux is a live server: its answers are given here. Leaked before the session starts, or by the server it starts
        for leak_from_new_server in (False, True):
            calls = []

            def run(cmd, stdin=None, timeout=60):
                calls.append(cmd)
                started = any(c[1] == "new-session" for c in calls)
                if cmd[1] == "show-environment":
                    return 0, "PATH=/usr/bin\n" + ("AGENT_ORCHESTRATOR=1\n" if started or not leak_from_new_server else "")
                return (1, "access not allowed") if cmd[1] == "set-environment" else (0, "")
            # live and cap stand in for the session registry and the mailroom's memory cap on a live tmux pane
            up, memory, err, note = relaunch.launch_in_tmux({"id": "x", "cli": "claude", "cwd": "/w"}, "claude --resume x", "fab",
                                                      run=run, live=lambda a: True, wait=1, cap=lambda *a: "capped",
                                                      tmux="tmux")
            self.assertFalse(up)
            self.assertIn("AGENT_ORCHESTRATOR (access not allowed)", err)
            self.assertEqual([c for c in calls if c[1] == "send-keys"], [])             # the agent was never started
            self.assertEqual(any(c[1] == "new-session" for c in calls), leak_from_new_server)

    def launch_on(self, first_screen):
        """launch_in_tmux of a Codex resume whose pane shows first_screen until a key is sent, then Codex's prompt.
        tmux is a live server holding the machine's agents, and the session registry reads it: both answer here.
        Returns (launch_in_tmux's result, the keys sent after the launch line and its Enter)."""
        calls, screens = [], [first_screen]

        def run(cmd, stdin=None, timeout=60):
            calls.append(cmd)
            if cmd[1] == "capture-pane":
                return 0, screens[-1]
            if cmd[1] == "send-keys" and len([c for c in calls if c[1] == "send-keys"]) > 2:
                screens.append("› Ask Codex to do anything\n")
            return 0, ""
        got = relaunch.launch_in_tmux({"id": "x", "cli": "codex", "cwd": "/w"}, "codex resume x", "tutor", run=run,
                                      live=lambda a: "Ask Codex" in screens[-1], wait=5, cap=lambda *a: "capped",
                                      tmux="tmux")
        return got, [c[4:] for c in calls if c[1] == "send-keys"][2:]

    def test_codex_hook_review_is_answered_without_trusting_and_reported(self):
        review = fixtures.CODEX_HOOK_REVIEW
        for screen, key in ((review, "3"),                  # the choice is picked by its text, wherever it sits
                            (review.replace("3. Continue", "4. Continue").replace("  2. Trust", "  3. Trust"), "4")):
            (up, memory, err, note), keys = self.launch_on(screen)
            self.assertTrue(up)
            self.assertEqual(keys, [["-l", key]])           # one key: never Enter, which reviews, or Trust all
            self.assertEqual(note, "Codex: 1 hook is new or changed, left untrusted (review in Codex's /hooks)")

    def test_a_hook_review_already_on_the_untrusted_choice_gets_enter(self):
        # should Codex's number key only move the highlight, the next look finds it there and confirms
        on_it = fixtures.CODEX_HOOK_REVIEW.replace("› 1. Review", "  1. Review").replace("  3. Continue", "› 3. Continue")
        (up, memory, err, note), keys = self.launch_on(on_it)
        self.assertTrue(up)
        self.assertEqual(keys, [["Enter"]])

    def test_a_codex_hook_review_without_the_untrusted_choice_is_left_for_the_owner(self):
        (up, memory, err, note), keys = self.launch_on(fixtures.CODEX_HOOK_REVIEW.replace("Continue without trusting",
                                                                                          "Skip"))
        self.assertFalse(up)
        self.assertEqual(keys, [])                          # nothing is pressed for the owner
        self.assertEqual(note, "Codex asks to review its hooks and offers no 'Continue without trusting': answer it "
                               "in tmux 'tutor'")


def act(req, agents=(), worktrees=(), **seams):
    """One request through actions.dispatch, with these as the board's agent rows and the repos' worktrees."""
    return actions.dispatch(req, lambda: list(agents), lambda: list(worktrees), **seams)


class ActionsTest(unittest.TestCase):
    rows = [{"id": "11111111-2222-3333-4444-555555555555", "cli": "claude", "live": True, "display": "alpha"},
            {"id": "0190aaaa-0000-7000-8000-00000000a001", "cli": "codex", "live": False, "display": "nova"}]

    def run_act(self, req):
        calls = []

        def fake(cmd, stdin=None, timeout=0):       # agent-send and agent-close act on live agents: they answer here
            calls.append((cmd, stdin))
            return 0, "ok"
        real_find = sources.find_bin
        stand_ins.use(self)
        try:
            return act(req, self.rows, run=fake), calls
        finally:
            sources.find_bin = real_find

    def test_each_action_builds_only_what_it_needs(self):
        built = []
        agents = lambda: built.append("agents") or self.rows
        worktrees = lambda: built.append("worktrees") or []
        for req, needs in (({"action": "sent", "limit": 0}, []), ({"action": "groups", "groups": "?"}, ["agents"]),
                           ({"action": "restart_run", "update": "?"}, []), ({"action": "orchestrate", "text": " "}, []),
                           ({"action": "close", "id": "?"}, ["agents"]), ({"action": "resume", "id": "?"}, ["agents"]),
                           ({"action": "wt_check", "path": "?"}, ["agents", "worktrees"]),
                           ({"action": "wt_landed_cleanup", "paths": []}, ["agents", "worktrees"]),
                           ({"action": "shell"}, []), ({"action": ["close"]}, []), (["close"], [])):
            built.clear()
            self.assertFalse(actions.dispatch(req, agents, worktrees)["ok"], req)     # each one refused, or empty
            self.assertEqual(built, needs, req)

    def test_unknown_actions_are_refused(self):
        for action in ("not_an_action", "delete_everything"):
            self.assertEqual(actions.dispatch({"action": action}, lambda: self.rows, lambda: []),
                             {"ok": False, "error": "unknown action %r" % action})

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
        self.assertTrue(calls[0][1].startswith("[relayed from Alex via agent-board]\n--help"))
        self.assertIn("(Sent from the agent board as message ", calls[0][1])
        self.assertIn(", not by email. If you need to answer, use agent-mail send to Alex.)\n", calls[0][1])
        self.assertEqual(actions.send_footer(), "\n\n(Sent from the agent board, not by email. If you need to answer, "
                                                "use agent-mail send to Alex.)")

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
                    {"action": "force_close", "id": shared[1]["id"], "confirm": "nova"}):
            res = act(req, shared, run=lambda *a, **k: (0, "should not run"))     # agent-close: never reached
            self.assertFalse(res["ok"])
            self.assertIn("same directory", res["error"])

    def test_switch_validates_and_builds_argv(self):
        calls = []

        def fake(cmd, stdin=None, timeout=0):       # agent-switch restarts a live agent on another login: it answers here
            calls.append(cmd)
            if "--options" in cmd:
                return 0, json.dumps({"agent": {"busy": True}, "will_interrupt": True, "targets": []})
            return (3, json.dumps({"ok": False, "needs_force": True, "detail": "busy"})) if "--force" not in cmd \
                else (0, json.dumps({"ok": True, "detail": "default -> work"}))
        real_find = sources.find_bin
        stand_ins.use(self)
        try:
            sid = self.rows[0]["id"]
            res = act({"action": "switch_options", "id": sid}, self.rows, run=fake)
            self.assertTrue(res["options"]["will_interrupt"])
            self.assertEqual(calls[-1], ["/bin/agent-switch", sid, "--options", "--json"])
            res = act({"action": "switch", "id": sid, "to": "claude:work"}, self.rows, run=fake)
            self.assertTrue(res["needs_force"])                                   # busy: ask, then --force
            res = act({"action": "switch", "id": sid, "to": "claude:work", "model": "opus", "effort": "high",
                       "force": True}, self.rows, run=fake)
            self.assertTrue(res["ok"])
            self.assertEqual(calls[-1], ["/bin/agent-switch", sid, "--to", "claude:work", "--model", "opus",
                                         "--effort", "high", "--force", "--json"])
            for odd in ("[1, 2]", "null", '"text"', "not json"):
                res = act({"action": "switch_options", "id": sid}, self.rows, run=lambda cmd, **k: (1, odd))
                self.assertFalse(res["ok"])
                res = act({"action": "switch", "id": sid, "to": "codex"}, self.rows, run=lambda cmd, **k: (1, odd))
                self.assertFalse(res["ok"])
            refuse = lambda cmd, **k: (2, json.dumps({"ok": False, "refused": True, "detail": "not agent-spawn's tmux layout"}))
            res = act({"action": "switch_options", "id": sid}, self.rows, run=refuse)
            self.assertEqual((res["ok"], res["error"]), (False, "not agent-spawn's tmux layout"))
            for bad in ({"to": "claude:work; rm -rf ~"}, {"to": "gemini"}, {"model": "--help"}, {"effort": "x y"}, {},
                        {"to": "codex", "force": "yes"}):
                n = len(calls)
                self.assertFalse(act(dict(bad, action="switch", id=sid), self.rows, run=fake)["ok"], bad)
                self.assertEqual(len(calls), n, bad)
            self.assertFalse(act({"action": "switch", "id": self.rows[1]["id"], "to": "codex"}, self.rows, run=fake)["ok"])
        finally:
            sources.find_bin = real_find

    def test_force_close_needs_typed_name_and_works_offline(self):
        res, calls = self.run_act({"action": "force_close", "id": self.rows[1]["id"], "confirm": "nova"})
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

    def test_the_board_and_bulk_clean_up_select_the_same_worktrees(self):
        self.assertTrue(collect.is_subagent_tree(self.wts[1]["path"]))
        self.assertFalse(collect.is_subagent_tree(self.wts[0]["path"]))
        with tempfile.TemporaryDirectory() as d:
            fixtures.repo_with_worktree(d, "proj", "other")
            # a live agent in another checkout of proj guards proj's subagent tree; the board's rows carry the repo,
            # `act` reads the agents without git
            board = [{"id": "x", "live": True, "cwd": d + "/proj-other", "repo": "proj"}]
            act = [{"id": "x", "live": True, "cwd": d + "/proj-other"}]
            free = collect.free_worktrees(board, self.wts)
            self.assertEqual([w["path"] for w in free], ["/w/feed-fix"])
            self.assertEqual(collect.free_worktrees(act, self.wts), free)
            # agent-close would remove worktrees and fetch from GitHub: its verdict and the fetch are stood in for
            chk = actions.landed_check(act, self.wts, closer=lambda info, **k: (0, ["remove worktree"]),
                                       fetch=lambda mains: {})
            self.assertEqual([c["path"] for c in chk["candidates"]], ["/w/feed-fix"])

    def test_a_free_worktree_row_carries_what_the_mac_reads(self):
        with tempfile.TemporaryDirectory() as d:
            fixtures.repo_with_worktree(d, "feed", "fix")
            real = sources.REPOS_ROOT
            sources.REPOS_ROOT = Path(d)                 # the repos the scan finds: this folder's, not the machine's
            try:
                rows = fixtures.collector(d, worktrees=collect.worktree_scan(True)).worktree_section([], NOW)
            finally:
                sources.REPOS_ROOT = real
            self.assertEqual([w["path"] for w in rows], [d + "/feed-fix"])
            for w in rows:      # the fields FreeWorktree (Model.swift) decodes, and no others
                self.assertEqual(set(w), {"path", "name", "repo", "branch", "locked", "git", "last_commit", "tmux"})

    def test_the_orchestrator_folder_comes_from_the_mailroom_config(self):
        real = sources.MAIL_CONFIG
        d = Path(tempfile.mkdtemp())
        (d / "orch").mkdir()
        sources.MAIL_CONFIG = d / "config.json"
        try:
            sources.MAIL_CONFIG.write_text(json.dumps({"orchestrator_dir": str(d / "orch")}))
            orch = collect.orchestrator_dir()
            self.assertEqual(orch, os.path.realpath(d / "orch"))
            self.assertTrue(collect.is_orchestrator("x", "nova", str(d / "orch"), orch))
            self.assertFalse(collect.is_orchestrator("x", "nova", str(d), orch))
            self.assertTrue(collect.is_orchestrator("x", "orchestrator-7", str(d), orch))
            sources.MAIL_CONFIG.write_text(json.dumps({"repos_root": str(d)}))     # else <repos_root>/orchestrator
            self.assertEqual(collect.orchestrator_dir(), collect._realpath(str(d / "orchestrator")))
            sources.MAIL_CONFIG.write_text("{}")
            self.assertEqual(collect.orchestrator_dir(), collect._realpath(str(config.repos_root({}) / "orchestrator")))
        finally:
            sources.MAIL_CONFIG = real

    def test_real_paths_cached_for_one_poll(self):
        with tempfile.TemporaryDirectory() as d:
            real, link = Path(d) / "real", Path(d) / "link"
            real.mkdir()
            collect.forget_paths()
            self.assertEqual(collect._realpath(str(link)), str(link))     # not a link yet
            link.symlink_to(real)
            self.assertEqual(collect._realpath(str(link)), str(link))     # same poll: the remembered answer
            collect.forget_paths()                                       # what Collector.agents() does first
            self.assertEqual(collect._realpath(str(link)), os.path.realpath(str(real)))
            self.assertTrue(collect.inside(str(link / "sub"), str(real)))

    def test_actions_validate_and_route(self):
        calls = []
        # agent-close would remove the worktree and agent-spawn start an agent: they answer here
        closer = lambda info, force, dry_run, fetch: (calls.append((info["cwd"], force, dry_run)) or (0, ["ok"]))
        spawn = {"run": lambda cmd, **k: (calls.append(cmd) or (0, "{}")), "state": "clean"}
        ok = lambda req, agents=(): act(req, agents, self.wts,
                                        **(spawn if req["action"] == "wt_spawn" else {"closer": closer}))
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

    def test_bulk_landed_cleanup(self):
        wts = self.wts + [{"path": "/w/feed-old", "repo": "feed", "main": "/w/feed", "branch": "old", "detached": False,
                           "locked": False}]
        verdict = {"/w/feed-fix": (0, ["would end no tmux session and remove worktree /w/feed-fix"]),
                   "/w/feed-old": (2, ["not closing worktree feed-old: 1 uncommitted path"])}
        calls, fetched = [], []

        def closer(info, force, dry_run, fetch):    # agent-close removes worktrees; the fetches below ask GitHub
            calls.append((info["cwd"], force, dry_run, fetch))
            return verdict[info["cwd"]] if dry_run else (0, ["removed"])
        busy = [{"id": "x", "live": True, "cwd": "/w/proj"}]                      # guards proj's subagent tree
        chk = actions.landed_check(busy, wts, closer=closer, fetch=lambda mains: fetched.extend(mains) or {})
        got = {c["path"]: c["removable"] for c in chk["candidates"]}
        self.assertEqual(got, {"/w/feed-fix": True, "/w/feed-old": False})      # subagent tree not even offered
        self.assertTrue(all(not f and fe is False for _, f, _, fe in calls))     # never --force; one fetch per repo
        self.assertEqual(fetched, ["/w/feed", "/w/feed"])
        res = actions.landed_cleanup({"paths": ["/w/feed-fix", "/etc", "/w/feed-fix"]}, busy, wts, closer=closer,
                                     fetch=lambda m: {})
        self.assertEqual((res["removed"], res["kept"]), (1, 1))                  # a repeated path counts once
        self.assertEqual(calls[-1], ("/w/feed-fix", False, False, False))
        self.assertFalse(actions.landed_cleanup({"paths": "/w/feed-fix"}, busy, wts, closer=closer)["ok"])

    def test_a_repo_that_could_not_be_fetched_is_never_removable(self):
        stale = lambda mains: {"/w/feed": "fatal: unable to access origin"}     # the fetch failed for feed's repo
        # agent-close would remove the worktree: its verdict is given here
        closer = lambda info, force, dry_run, fetch: (0, ["would remove worktree " + info["cwd"]] if dry_run else ["removed"])
        chk = actions.landed_check([], self.wts[:1], closer=closer, fetch=stale)["candidates"][0]
        self.assertFalse(chk["removable"])
        self.assertIn("git fetch origin failed in /w/feed (fatal: unable to access origin)", chk["detail"])
        self.assertIn("would remove worktree /w/feed-fix", chk["detail"])          # agent-close's verdict still shown
        res = actions.landed_cleanup({"paths": ["/w/feed-fix"]}, [], self.wts[:1], closer=closer, fetch=stale)
        self.assertEqual((res["removed"], res["kept"]), (0, 1))
        self.assertIn("git fetch origin failed in /w/feed", res["results"][0]["detail"])

    def test_fetching_names_the_repos_it_could_not_fetch(self):
        with tempfile.TemporaryDirectory() as d:
            fixtures.git("init", "-q", "--bare", d + "/origin.git")
            for repo in ("fetched", "no-origin"):
                fixtures.git("init", "-q", "-b", "main", "%s/%s" % (d, repo))
            fixtures.git("-C", d + "/fetched", "remote", "add", "origin", d + "/origin.git")
            failed = actions._fetch_repos([d + "/fetched", d + "/no-origin", d + "/fetched"])
        self.assertEqual(list(failed), [d + "/no-origin"])
        self.assertIn("origin", failed[d + "/no-origin"])                        # git's own reason

    def test_list_worktrees_real_repo(self):
        with tempfile.TemporaryDirectory() as d:
            fixtures.repo_with_worktree(d, "repo", "feat")
            got = sources.list_worktrees([d])
            self.assertEqual([(w["repo"], w["branch"], w["path"]) for w in got], [("repo", "feat", d + "/repo-feat")])


class ResumeTest(unittest.TestCase):
    @staticmethod
    def fake_mailroom(**modules):
        """sources.mailroom over these modules, any other one missing as in a mailroom older than it: the mailroom is
        another repo's install, with its own tests."""
        def mailroom(module):
            if module not in modules:
                raise ModuleNotFoundError("No module named 'agentmail.%s'" % module, name="agentmail." + module)
            return modules[module]
        return mailroom

    @staticmethod
    def config(cfg):
        import types
        return types.SimpleNamespace(load=lambda: cfg)

    def test_resume_validation_and_tmux_flow(self):
        d = tempfile.mkdtemp()
        dead = {"id": "0190aaaa-0000-7000-8000-00000000a001", "cli": "codex", "live": False, "display": "nova",
                "name": "nova", "cwd": d, "last_activity": NOW}
        rows = [dead, dict(dead, id="11111111-2222-3333-4444-555555555555", live=True, display="alive"),
                dict(dead, id="22222222-2222-3333-4444-555555555555", cwd=d + "/gone", display="nodir")]
        self.assertFalse(act({"action": "resume", "id": rows[1]["id"]}, rows)["ok"])     # already running
        self.assertIn("directory is gone", act({"action": "resume", "id": rows[2]["id"]}, rows)["error"])
        calls, sent = [], []
        screens = [fixtures.CODEX_HOOK_REVIEW]        # Codex's first screen asks about a changed hook

        def run(cmd, stdin=None, timeout=0):          # tmux and agent-send: live processes
            calls.append(cmd)
            if stdin:
                sent.append(stdin)
            if "capture-pane" in cmd:
                return 0, screens.pop() if screens else ""
            return 0, ("1234" if "#{pane_pid}" in cmd else "")
        real = (sources.find_bin, sources.tmux_sessions, sources.codex_settings, actions.stranded_mail, sources.mailroom)
        stand_ins.use(self)
        sources.tmux_sessions = lambda: {"nova": {"/x"}}       # the live tmux server, nova taken: it must pick nova-2
        sources.codex_settings = lambda tid: ("gpt-6-astra", "xhigh")     # the thread's settings in the live ~/.codex
        actions.stranded_mail = lambda a: [311]                            # the live mailroom database
        sources.mailroom = self.fake_mailroom(config=self.config({}))
        try:
            # already running again, or a process already carries the id: refuse, start nothing. live, busy and cap
            # stand in for the live session registry, /proc and the mailroom's memory cap on a live tmux pane
            self.assertIn("already running", actions.resume({}, dead, run=run, live=lambda a: True, busy=lambda i: [])["error"])
            self.assertIn("already has", actions.resume({}, dead, run=run, live=lambda a: False, busy=lambda i: [42])["error"])
            self.assertEqual(calls, [])
            state = {"n": 0}

            def live(a):                     # the live session registry: dead at first, live once launched
                state["n"] += 1
                return state["n"] > 1
            res = actions.resume({"text": "carry on"}, dead, run=run, live=live, wait=6, cap=lambda *a: "capped",
                                 busy=lambda i: [], ref="k3f9")
        finally:
            sources.find_bin, sources.tmux_sessions, sources.codex_settings, actions.stranded_mail, sources.mailroom = real
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["tmux"], "nova-2")
        self.assertIn(["/bin/tmux", "send-keys", "-t", "=nova-2:", "-l", "3"], calls)    # continued without trusting
        self.assertIn("gpt-6-astra · xhigh; capped). Codex: 1 hook is new or changed, left untrusted (review in "
                      "Codex's /hooks). Told it why", res["output"])
        self.assertEqual(calls[0], ["/bin/tmux", "show-environment", "-g"])     # leaked variables dropped first
        self.assertEqual(calls[1][:4], ["/bin/tmux", "new-session", "-d", "-s"])
        line = next(c[-1] for c in calls if "-l" in c)
        self.assertIn("resume -s danger-full-access -a never -C %s -c check_for_update_on_startup=false -m gpt-6-astra "
                      "-c model_reasoning_effort=xhigh %s" % (d, dead["id"]), line)
        self.assertTrue(sent and "agent-mail show 311" in sent[0] and "carry on" in sent[0])
        self.assertTrue(res["note_sent"])
        self.assertIn("message k3f9 on the board", sent[0])
        self.assertIn("agent-board done k3f9 -m", sent[0])
        self.assertTrue(sent[0].startswith("[relayed from Alex via agent-board] resumed\n"))
        self.assertIn("Alex resumed it from the agent board, in tmux session 'nova-2'", sent[0])
        self.assertIn("Alex emailed you while you were down: agent-mail show 311", sent[0])

    def test_a_note_that_did_not_reach_the_agent_says_so(self):
        d = tempfile.mkdtemp()
        dead = {"id": "0190aaaa-0000-7000-8000-00000000a001", "cli": "codex", "live": False, "display": "nova",
                "name": "nova", "cwd": d, "last_activity": NOW}

        def run(cmd, stdin=None, timeout=0):          # tmux and agent-send: live processes
            if cmd[0].endswith("agent-send"):
                return 1, "no such session"
            return 0, ("1234" if "#{pane_pid}" in cmd else "")
        real = (sources.find_bin, sources.tmux_sessions, sources.codex_settings, actions.stranded_mail, sources.mailroom)
        stand_ins.use(self)
        sources.tmux_sessions = lambda: {}                                 # the live tmux server
        sources.codex_settings = lambda tid: ("gpt-6-astra", "xhigh")     # the thread's settings in the live ~/.codex
        actions.stranded_mail = lambda a: []                               # the live mailroom database
        sources.mailroom = self.fake_mailroom(config=self.config({}))
        try:
            state = {"n": 0}

            def live(a):                     # the live session registry: dead at first, live once launched
                state["n"] += 1
                return state["n"] > 1
            # busy and cap stand in for the live /proc and the mailroom's memory cap on a live tmux pane
            res = actions.resume({"text": "carry on"}, dead, run=run, live=live, wait=3, cap=lambda *a: "capped",
                                 busy=lambda i: [])
        finally:
            sources.find_bin, sources.tmux_sessions, sources.codex_settings, actions.stranded_mail, sources.mailroom = real
        self.assertTrue(res["ok"])                                         # it is up again
        self.assertFalse(res["note_sent"])
        self.assertIn("Could not tell it why (no such session): your note was not passed on.", res["output"])

    def test_newest_transcript_copy_wins(self):
        d = Path(tempfile.mkdtemp())
        sid = "c0c0c0c0-0000-4000-8000-000000000003"
        for login, age in (("work", 3600), ("default", 10)):
            f = d / login / "projects" / "p" / (sid + ".jsonl")
            f.parent.mkdir(parents=True)
            f.write_text("{}\n")
            os.utime(f, (time.time() - age, time.time() - age))
        real = sources.claude_logins
        sources.claude_logins = lambda: [("work", d / "work"), ("default", d / "default")]
        try:
            self.assertEqual(sources.claude_home_of(sid), ("default", d / "default"))   # not the first login listed
        finally:
            sources.claude_logins = real

    SID = "c0c0c0c0-0000-4000-8000-000000000003"

    def claude_login(self):
        """A Claude login in a temporary folder holding this session's transcript, and the agent row for it."""
        d = Path(tempfile.mkdtemp())
        (d / ".claude" / "projects" / "p").mkdir(parents=True)
        (d / ".claude" / "projects" / "p" / (self.SID + ".jsonl")).write_text("{}\n")
        return d / ".claude", {"id": self.SID, "cli": "claude", "live": False, "display": "fab", "name": "fab",
                               "cwd": str(d), "last_activity": NOW}

    @staticmethod
    def failover(home, recorded, place=None):
        """The mailroom's failover module: it is another repo's install, with its own tests. It places the resume on
        `home` (or runs `place`) and reads `recorded` (--model/--effort flags) from the transcript."""
        import types
        return types.SimpleNamespace(
            place_resume=place or (lambda sess, cfg, trust, dry_run: (str(home), "on login default: most left")),
            transcript=lambda sess: Path(sess["config_dir"]) / "projects" / "p" / (sess["id"] + ".jsonl"),
            read_tail=lambda path, nbytes: Path(path).read_text().splitlines(), settings=lambda cfg: {"tail_bytes": 1},
            transcript_flags=lambda lines: list(recorded))

    def resume_claude(self, home, agent, failover, cfg, taken=()):
        """actions.resume on an exited Claude agent, with this mailroom and these tmux sessions already there:
        (result, the line typed into tmux)."""
        typed = []

        def run(cmd, stdin=None, timeout=0):          # tmux and agent-send: live processes
            if "-l" in cmd:
                typed.append(cmd[-1])
            return 0, ""
        state = {"n": 0}

        def live(a):                     # the live session registry: dead at first, live once launched
            state["n"] += 1
            return state["n"] > 1
        real = (sources.claude_logins, sources.DEFAULT_CLAUDE_HOME, sources.find_bin, sources.tmux_sessions,
                sources.mailroom, relaunch.spawn_trust, actions.stranded_mail)
        sources.claude_logins = lambda: [("default", home)]               # not the live ~/.claude logins
        sources.DEFAULT_CLAUDE_HOME = home
        stand_ins.use(self)
        sources.tmux_sessions = lambda: {t: {"/x"} for t in taken}         # the live tmux server
        sources.mailroom = self.fake_mailroom(config=self.config(cfg), failover=failover)
        relaunch.spawn_trust = lambda: None                                # it would write a login's .claude.json
        actions.stranded_mail = lambda a: []                               # the live mailroom database
        try:                # busy and cap: the live /proc and the mailroom's memory cap on a live tmux pane
            res = actions.resume({}, agent, run=run, live=live, wait=3, cap=lambda *a: "capped", busy=lambda i: [])
        finally:
            (sources.claude_logins, sources.DEFAULT_CLAUDE_HOME, sources.find_bin, sources.tmux_sessions,
             sources.mailroom, relaunch.spawn_trust, actions.stranded_mail) = real
        return res, typed

    def test_claude_resumes_at_its_own_model_and_effort_and_name(self):
        home, agent = self.claude_login()
        res, typed = self.resume_claude(home, agent, self.failover(home, ["--model", "claude-fable-5-1", "--effort", "high"]),
                                        {"spawn_claude_model": "claude-opus-5-5", "spawn_claude_effort": "xhigh"},
                                        taken=["fab"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["tmux"], "fab-2")
        self.assertIn("--model claude-fable-5-1 --effort high --name fab", typed[0])    # its own name: agent-send's
        self.assertNotIn("fab-2", typed[0])
        self.assertTrue(typed[0].startswith("env %s -u CLAUDE_CONFIG_DIR " % relaunch.SCRUB_ENV))   # not as the orchestrator
        self.assertIn("claude · claude-fable-5-1 · high; on login default: most left", res["output"])

    def test_a_failed_placement_stops_the_resume_with_its_message(self):
        home, agent = self.claude_login()

        def place(sess, cfg, trust, dry_run):
            raise PermissionError("can't write /x/.claude.json")
        res, typed = self.resume_claude(home, agent, self.failover(home, [], place), {})
        self.assertFalse(res["ok"])
        self.assertIn("can't write /x/.claude.json", res["error"])
        self.assertEqual(typed, [])                                      # nothing was started

    def test_what_the_agent_did_not_record_comes_from_the_mailroom_defaults(self):
        import types
        home, agent = self.claude_login()
        cfg = {"spawn_claude_model": "claude-opus-5-5", "spawn_claude_effort": "xhigh", "spawn_codex_model": "gpt-6.1-sol",
               "spawn_codex_effort": None}                               # null: the CLI's own choice
        real = (sources.claude_logins, sources.mailroom, relaunch.spawn_trust, sources.codex_settings)
        stand_ins.use(self)                                                # a mailroom checkout is there
        sources.claude_logins = lambda: [("default", home)]               # not the live ~/.claude logins
        sources.mailroom = self.fake_mailroom(failover=self.failover(home, []))
        relaunch.spawn_trust = lambda: None                                # it would write a login's .claude.json
        sources.codex_settings = lambda tid: (None, None)                 # the live ~/.codex: no thread settings
        try:
            claude = relaunch.resume_settings(agent, cfg)
            codex = relaunch.resume_settings(dict(agent, cli="codex"), cfg)
            null = relaunch.resume_settings(agent, dict(cfg, spawn_claude_model=None))
            sources.mailroom = self.fake_mailroom(failover=types.SimpleNamespace())     # older than place_resume
            unplaced = relaunch.resume_settings(agent, cfg)
            sources.mailroom = self.fake_mailroom()                                       # older than failover
            self.assertEqual(relaunch.resume_settings(agent, cfg), unplaced)
        finally:
            sources.claude_logins, sources.mailroom, relaunch.spawn_trust, sources.codex_settings = real
        self.assertEqual(claude, (home, "claude-opus-5-5", "xhigh", "on login default: most left"))
        self.assertEqual(codex, (None, "gpt-6.1-sol", None, ""))
        self.assertEqual(null[1], None)
        self.assertEqual(unplaced[:3], (home, "claude-opus-5-5", "xhigh"))
        self.assertIn("on login default, where its newest copy is", unplaced[3])

    @staticmethod
    def mailroom_install(command, modules):
        """A mailroom checkout in a temporary folder, found as the kit finds it (AGENT_MAILROOM_DIR, config.mailroom_dir),
        with `command` in its bin/ and these agentmail modules ({name: source}); its root, and how to put the
        environment, sys.path and sys.modules back."""
        import sys
        root = Path(tempfile.mkdtemp())
        (root / "bin").mkdir()
        (root / "bin" / command).write_text("")
        (root / "agentmail").mkdir()
        (root / "agentmail" / "__init__.py").write_text("")
        for name, src in modules.items():
            (root / "agentmail" / (name + ".py")).write_text(src)
        real_dir, real_path = os.environ.get("AGENT_MAILROOM_DIR"), list(sys.path)

        def forget():                    # any agentmail imported before or by this test
            for m in [m for m in sys.modules if m == "agentmail" or m.startswith("agentmail.")]:
                del sys.modules[m]

        def undo():
            sys.path[:] = real_path
            if real_dir is None:
                os.environ.pop("AGENT_MAILROOM_DIR", None)
            else:
                os.environ["AGENT_MAILROOM_DIR"] = real_dir
            forget()
        forget()
        os.environ["AGENT_MAILROOM_DIR"] = str(root)
        return root, undo

    def test_the_mailroom_is_the_kits_checkout(self):
        root, undo = self.mailroom_install("agent-close", {"close": "def close(info, **kw):\n    return 0, ['here']\n"})
        try:
            self.assertEqual(sources.mailroom_root(), str(root))
            self.assertEqual(actions.mailroom_close()({}), (0, ["here"]))
            bare = Path(tempfile.mkdtemp())                                  # a folder with no agentmail package
            os.environ["AGENT_MAILROOM_DIR"] = str(bare)
            self.assertIsNone(sources.mailroom_root())
            self.assertIsNone(sources.optional_mailroom("reviewgate"))
            with self.assertRaisesRegex(RuntimeError, "the mailroom isn't installed"):
                sources.mailroom("close")
        finally:
            undo()

    def test_a_broken_agent_gate_is_not_skipped(self):
        for modules, want in (({}, ""),                                      # a mailroom older than agent-gate
                              ({"reviewgate": "def codex_hook_args(cwd):\n    return ['-c', 'x=' + cwd]\n"}, "-c x=/w")):
            root, undo = self.mailroom_install("agent-spawn", modules)
            try:
                self.assertEqual(relaunch.codex_hook_args("/w"), want)
            finally:
                undo()
        root, undo = self.mailroom_install("agent-spawn", {"reviewgate": "import agentboard_no_such_dependency\n"})
        try:
            with self.assertRaises(ModuleNotFoundError):                     # Codex would skip the repo's gate silently
                relaunch.codex_hook_args("/w")
        finally:
            undo()

    def test_running_with_scans_command_lines(self):
        d = tempfile.mkdtemp()
        for pid, cmd in (("101", b"/home/alex/.local/bin/codex\x00resume\x00abc-123"),
                         ("102", b"bash"),
                         ("103", b"tail\x00-f\x00/x/abc-123.jsonl"),                         # reading the transcript
                         ("104", b"python3\x00/bin/agent-mail\x00send\x00--session\x00abc-123"),
                         ("105", b"node\x00/opt/claude.js\x00--resume\x00abc-123"),
                         ("106", b"/usr/bin/claude\x00--resume\x00abc-123x"),                 # a different id
                         ("107", b"node\x00/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js\x00--resume\x00abc-123"),
                         ("108", b"python3\x00/home/alex/tools/agent-mail\x00--session\x00abc-123")):  # not a CLI
            os.makedirs(os.path.join(d, pid))
            with open(os.path.join(d, pid, "cmdline"), "wb") as fh:
                fh.write(cmd)
        self.assertEqual(sorted(actions.running_with("abc-123", proc=d)), [101, 105, 107])


class OrchestrateTest(unittest.TestCase):
    def test_runs_one_event_in_the_background_with_the_header(self):
        got = {}

        class FakeProc:     # agent-orchestrate would run a paid `claude -p` event: this Popen keeps what it was given
            def __init__(self, cmd, **kw):
                got["cmd"], got["kw"] = cmd, kw
                self.stdin = self

            def write(self, data):
                got["stdin"] = data

            def close(self):
                pass
        real_find, real_logs, real_config = sources.find_bin, actions.ORCH_LOGS, sources.MAIL_CONFIG
        stand_ins.use(self)
        actions.ORCH_LOGS = Path(tempfile.mkdtemp())
        sources.MAIL_CONFIG = actions.ORCH_LOGS / "config.json"
        sources.MAIL_CONFIG.write_text(json.dumps({"allowlist": ["owner@example.com"]}))
        try:
            res = act({"action": "orchestrate", "text": "spin up an agent on proj --help $(x)"}, popen=FakeProc)
            self.assertTrue(res["ok"])
            self.assertEqual(got["cmd"], ["/bin/agent-orchestrate", "--source", "board"])     # text never in argv
            self.assertTrue(got["kw"]["start_new_session"])
            body = got["stdin"].decode()
            self.assertTrue(body.startswith("[message from Alex via the agent board"))
            self.assertIn("`agent-mail send --to owner --subject", body)          # the mailroom's alias for the owner
            self.assertIn("\n\nspin up an agent on proj --help $(x)\n\nThis is message ", body)
            self.assertTrue(body.splitlines()[-1].strip().startswith(sent.DONE_CMD + " "))
            for bad in ({"text": "  "}, {"text": 5}, {}, {"text": "x" * 9000}):
                self.assertFalse(act(dict(bad, action="orchestrate"), popen=FakeProc)["ok"])
        finally:
            sources.find_bin, actions.ORCH_LOGS, sources.MAIL_CONFIG = real_find, real_logs, real_config


class SentRecordTest(unittest.TestCase):
    """The board's record of what the owner sent. Each `run` answers for agent-send, agent-spawn or agent-close, which act
    on live agents."""
    rows = ActionsTest.rows

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.real = (sent.PATH, sent.MARKS, sources.find_bin)
        sent.PATH, sent.MARKS = d / "sent.jsonl", d / "sent-marks.jsonl"
        stand_ins.use(self)

    def tearDown(self):
        sent.PATH, sent.MARKS, sources.find_bin = self.real

    def test_an_action_the_log_cannot_record_says_so(self):
        real = actions.AUDIT
        actions.AUDIT = Path(tempfile.mkdtemp())                       # a folder where the log file should be
        try:
            res = act({"action": "send", "id": self.rows[0]["id"], "text": "hello"}, self.rows,
                      run=lambda cmd, stdin=None, timeout=0: (0, "delivered"))
        finally:
            actions.AUDIT = real
        self.assertTrue(res["ok"])
        self.assertIn("the board could not record this action", res["error"])

    def test_a_record_that_cannot_be_written_is_reported_not_lost(self):
        sent.PATH.write_text("")
        sent.PATH.chmod(0o400)                                               # readable, but not writable
        with self.assertRaises(OSError):
            sent.record("send", "nova", "hello", True)
        res = act({"action": "send", "id": self.rows[0]["id"], "text": "hello"}, self.rows,
                  run=lambda cmd, stdin=None, timeout=0: (0, "delivered"))
        self.assertTrue(res["ok"])                                            # it went: the Mac says so, and why
        self.assertIn("could not keep its record of this message", res["error"])

    def test_messages_are_kept_in_full_failed_ones_too(self):
        sid, dead = self.rows[0]["id"], self.rows[1]["id"]
        bodies = []
        act({"action": "send", "id": sid, "text": "  look at the café ✓\nline two  "}, self.rows,
            run=lambda cmd, stdin=None, timeout=0: (bodies.append(stdin) or (0, "delivered")))
        act({"action": "send", "id": sid, "text": "second"}, self.rows,
            run=lambda cmd, stdin=None, timeout=0: (1, "no such session"))
        act({"action": "send", "id": dead, "text": "are you there?"}, self.rows)   # not running
        act({"action": "close", "id": sid}, self.rows, run=lambda cmd, stdin=None, timeout=0: (0, ""))
        act({"action": "send", "id": sid, "text": "   "}, self.rows)
        got = sent.messages()
        self.assertEqual([(r["kind"], r["to"], r["ok"], r["text"], r["state"]) for r in got],
                         [("send", "alpha", True, "look at the café ✓\nline two", "waiting"),
                          ("send", "alpha", False, "second", "failed"),
                          ("send", "nova", False, "are you there?", "failed")])   # a close or an empty box is no message
        self.assertEqual((got[0]["id"], got[0]["cli"]), (sid, "claude"))
        self.assertNotIn("error", got[0])
        self.assertIn("no such session", got[1]["error"])
        self.assertIn("not running", got[2]["error"])
        refs = [r["ref"] for r in got]
        self.assertTrue(all(sent.REF_RE.match(r) for r in refs) and len(set(refs)) == 3)
        self.assertIn("as message %s, not by email" % refs[0], bodies[0])   # its ref, and how to mark it done:
        self.assertEqual(bodies[0].splitlines()[-1].strip(),                # alone on the last line, ready to run
                         '%s %s -m "what you did, in one line"' % (sent.DONE_CMD, refs[0]))
        self.assertEqual(sent.PATH.stat().st_mode & 0o777, 0o600)
        self.assertIn("café ✓", sent.PATH.read_text(encoding="utf-8"))       # readable, not \u-escaped

    def test_orchestrator_spawn_and_resume_requests(self):
        class Proc:         # agent-orchestrate would run a paid `claude -p` event: this Popen takes it and does nothing
            def __init__(self, cmd, **kw):
                self.stdin = self

            def write(self, data):
                pass

            def close(self):
                pass
        real_logs = actions.ORCH_LOGS
        actions.ORCH_LOGS = Path(tempfile.mkdtemp())
        try:
            act({"action": "orchestrate", "text": "start a proj agent"}, popen=Proc)
            act({"action": "orchestrate", "text": "x" * 9000}, popen=Proc)
        finally:
            actions.ORCH_LOGS = real_logs
        wts = WorktreeTest.wts
        briefs = []
        act({"action": "wt_spawn", "path": "/w/feed-fix", "text": "tidy and close"}, [], wts,
            run=lambda cmd, **k: (briefs.append(cmd[4]) or (0, "{}")), state="clean")
        act({"action": "wt_spawn", "path": "/w/gone", "text": "and this"}, [], wts)
        act({"action": "wt_check", "path": "/w/feed-fix"}, [], wts, closer=lambda *a, **k: (0, []))
        act({"action": "resume", "id": self.rows[0]["id"], "text": "carry on"}, self.rows)   # already live
        got = sent.messages()
        self.assertEqual([(r["kind"], r["to"], r["ok"]) for r in got],
                         [("orchestrate", "the orchestrator", True), ("orchestrate", "the orchestrator", False),
                          ("spawn", "a new agent in feed-fix", True), ("spawn", "a new agent in gone", False),
                          ("resume", "alpha", False)])
        self.assertIn("/orchestrate-", got[0]["log"])                         # where its run's output went
        self.assertEqual(len(got[1]["text"]), 9000)                            # too long to send, still kept
        self.assertEqual(got[2]["path"], "/w/feed-fix")
        self.assertIn("message %s on the board" % got[2]["ref"], briefs[0])
        self.assertIn("already running", got[4]["error"])

    def test_done_by_the_agent_then_ticked_off_by_the_owner(self):
        act({"action": "send", "id": self.rows[0]["id"], "text": "fix the chip"}, self.rows,
            run=lambda cmd, stdin=None, timeout=0: (0, ""))
        act({"action": "send", "id": self.rows[0]["id"], "text": "and the menu"}, self.rows,
            run=lambda cmd, stdin=None, timeout=0: (0, ""))
        a, b = [r["ref"] for r in sent.messages()]
        self.assertEqual(sent.counts(), {"to_check": 0, "waiting": 2})
        self.assertEqual(sent.mark([a, "zzzz"], "done", note="  fixed it, PR #9  ", by="sess"), ([a], ["zzzz"]))
        m = {r["ref"]: r for r in sent.messages()}
        self.assertEqual((m[a]["state"], m[a]["done"]["note"], m[a]["done"]["by"]), ("done", "fixed it, PR #9", "sess"))
        self.assertEqual((m[b]["state"], sent.counts()), ("waiting", {"to_check": 1, "waiting": 1}))
        res = actions.sent_mark_action({"action": "sent_mark", "refs": [a, b], "checked": True})
        self.assertEqual((res["ok"], res["counts"]), (True, {"to_check": 0, "waiting": 0}))
        self.assertEqual([r["state"] for r in sent.messages()], ["checked", "checked"])   # the owner can tick a waiting one
        actions.sent_mark_action({"action": "sent_mark", "refs": [a], "checked": False})
        self.assertEqual({r["ref"]: r["state"] for r in sent.messages()}, {a: "done", b: "checked"})
        sent.mark([a], "done", note="x" * 900)
        self.assertEqual(len(next(r for r in sent.messages() if r["ref"] == a)["done"]["note"]), sent.MAX_NOTE)
        for bad in ({"refs": a, "checked": True}, {"refs": [], "checked": True}, {"refs": ["no way"], "checked": True},
                    {"refs": [a], "checked": "yes"}, {"refs": ["zzzz"], "checked": True}):
            self.assertFalse(actions.sent_mark_action(dict(bad, action="sent_mark"))["ok"], bad)
        self.assertEqual(sent.MARKS.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            sent.mark([a], "deleted")

    def test_the_done_command(self):
        import importlib.machinery
        import importlib.util
        import io
        from contextlib import redirect_stderr, redirect_stdout
        path = str(Path(__file__).resolve().parent.parent / "bin" / "agent-board")
        spec = importlib.util.spec_from_loader("agent_board_cli", importlib.machinery.SourceFileLoader("agent_board_cli", path))
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        sent.record("send", "alpha", "do the thing", True, ref="k3f9")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main(["done", "k3f9", "-m", "did the thing"]), 0)
            self.assertEqual(cli.main(["done", "zzzz"]), 1)                         # not on record
            self.assertEqual(cli.main(["done", "K3F9;rm"]), 2)                      # not a ref at all
            self.assertEqual(cli.main(["sent"]), 0)
        self.assertIn("marked k3f9 done", out.getvalue())
        self.assertIn("[done]", out.getvalue())
        self.assertIn("-- done ", out.getvalue())
        self.assertIn("did the thing", out.getvalue())
        self.assertEqual(sent.messages()[0]["state"], "done")

    def test_seeing_the_agents_email_ticks_a_done_message_off(self):
        t = time.time() - 3 * 3600
        for ref, sid, ago in (("aaaa", "s1", 0), ("bbbb", "s2", 0), ("cccc", "s3", 0), ("dddd", None, 0),
                              ("eeee", "s5", 0), ("ffff", "s6", 0), ("gggg", "s7", 8 * 86400)):
            sent.record("send", "x", "do " + ref, True, ts=t - ago, ref=ref, **({"id": sid} if sid else {}))
        for ref in ("aaaa", "bbbb", "cccc", "dddd", "ffff", "gggg"):        # eeee stays waiting
            sent.mark([ref], "done", note="did it", by="s4" if ref == "dddd" else None)
        mail = lambda th, at, mid, subj="Done": {"thread_id": th, "sent_at": int(at), "message_id": mid, "subject": subj}
        reply = lambda th, at: {"thread_id": th, "received_at": int(at)}
        box = {"s1": {"sent": [mail("t1", t + 60, "<m1@x>", "Fixed the chip")], "inbound": []},
               "s2": {"sent": [mail("t2", t + 60, "<m2@x>"), mail("t3", t + 120, "<m3@x>")],
                      "inbound": [reply("t2", t + 90), reply("t3", t + 100)]},            # before m3: not its reply
               "s3": {"sent": [mail("t4", t - 60, "<m4@x>"), mail("t5", time.time() + 7200, "<m5@x>")],
                      "inbound": []},                         # one before the message, one over an hour after done
               "s4": {"sent": [mail("t6", t + 60, "<m6@x>")], "inbound": []},          # dddd's done came from s4
               "s5": {"sent": [mail("t7", t + 60, "<m7@x>")], "inbound": []},
               "s7": {"sent": [mail("t8", t + 60, "<m8@x>")], "inbound": []}}
        read = {"<m1@x>": False, "<m6@x>": True, "<m7@x>": True, "<m8@x>": True}
        asked = []
        is_read = lambda m: asked.append(m) or read.get(m)        # GmailState.is_read: the owner's mailbox, over the network
        self.assertEqual(sent.seen_ticks(box, is_read), ["dddd"])        # m1 unread, m3 unanswered, nothing for ffff
        self.assertIn("<m3@x>", asked)                                 # asked about, so Gmail looks it up
        read["<m1@x>"] = True
        box["s2"]["inbound"].append(reply("t3", t + 130))
        self.assertEqual(sorted(sent.seen_ticks(box, is_read)), ["aaaa", "bbbb"])
        m = {r["ref"]: r for r in sent.messages()}
        self.assertEqual({r: m[r]["state"] for r in m}, {"aaaa": "checked", "bbbb": "checked", "cccc": "done",
                                                         "dddd": "checked", "eeee": "waiting", "ffff": "done",
                                                         "gggg": "done"})   # gggg: older than the mail view
        self.assertEqual((m["aaaa"]["tick"]["by"], m["aaaa"]["tick"]["note"]), ("mail", 'you read its email "Fixed the chip"'))
        self.assertIn("replied to", m["bbbb"]["tick"]["note"])
        self.assertEqual(sent.counts(), {"to_check": 3, "waiting": 1})
        self.assertEqual(sent.seen_ticks(box, is_read), [])                  # once only
        actions.sent_mark_action({"action": "sent_mark", "refs": ["aaaa"], "checked": False})
        self.assertEqual(sent.seen_ticks(box, is_read), [])                  # the owner's untick stands
        self.assertEqual(next(r for r in sent.messages() if r["ref"] == "aaaa")["state"], "done")
        self.assertEqual(sent.seen_ticks({"s3": {"sent": [mail("t9", t + 60, "<m9@x>")], "inbound": []}}), [])  # no Gmail
        self.assertEqual(sum(1 for ln in sent.MARKS.read_text().splitlines() if '"mail"' in ln), 3)

    def test_the_stream_ticks_off_what_he_replied_to_and_a_snapshot_never_does(self):
        sid = "11111111-2222-3333-4444-555555555555"
        sent.record("send", "fab", "fix it", True, ts=NOW - 600, ref="hhhh", id=sid)
        sent.MARKS.write_text(json.dumps({"ts": NOW - 300, "ref": "hhhh", "event": "done", "note": "fixed"}) + "\n")
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            log = d / "events.jsonl"
            log.write_text(json.dumps({"ts": NOW - 4000, "event": "start", "session_id": sid, "cli": "claude",
                                       "name": "fab", "cwd": "/x"}) + "\n")
            con = fixtures.mailroom_db(d / "mailroom.sqlite")     # the agent's mail after the message, and the owner's reply
            con.execute("insert into sessions(tag, cli, session_id, session_name, origin_host) values(?,?,?,?,?)",
                        ("abcd1234", "claude", sid, "fab", "desktop"))
            con.execute("insert into sent(tag, thread_id, message_id, to_addr, subject, sent_at) values(?,?,?,?,?,?)",
                        ("abcd1234", "t1", "<m1@x>", OWNER, "Fixed", int(NOW - 290)))
            con.execute("insert into inbound(thread_id, from_addr, subject, received_at, matched_tag, match_rule, action, "
                        "detail, released_to) values(?,?,?,?,?,?,?,?,?)",
                        ("t1", OWNER, "Re: Fixed", int(NOW - 100), "abcd1234", "thread", "delivered", "", ""))
            con.commit()
            con.close()
            col = fixtures.collector(d, claude=lambda: [fixtures.session(sid, "fab", NOW - 3000)], sent=sent)
            marks = sent.MARKS.read_text()
            snap = col.snapshot(NOW)
            self.assertEqual((snap["sent"], snap["problems"]), ({"to_check": 1, "waiting": 0}, []))
            self.assertEqual(sent.MARKS.read_text(), marks)                # a snapshot only reads
            self.assertEqual(col.tick_seen(NOW), ["hhhh"])                 # what the stream runs after each one
            self.assertEqual(sent.messages()[0]["tick"]["note"], 'you replied to its email "Fixed"')
            self.assertEqual(col.snapshot(NOW)["sent"], {"to_check": 0, "waiting": 0})
            with open(sent.MARKS, "a") as fh:
                fh.write("damaged\n{}\n")
            snap = col.snapshot(NOW)                                     # the board stays up and says what failed
            self.assertNotIn("sent", snap)
            self.assertEqual([p["source"] for p in snap["problems"]], ["sent"])
            self.assertIn("sent-marks.jsonl line", snap["problems"][0]["error"])
            with self.assertRaises(ValueError):
                col.tick_seen(NOW)

    def test_reading_back(self):
        sent.PATH.write_text("\n".join([json.dumps({"ts": 30, "kind": "send", "to": "b", "ok": True, "text": "3"}),
                                         json.dumps([1]), json.dumps({"ts": 5, "text": None}),
                                         json.dumps({"ts": 10, "kind": "send", "to": "a", "ok": True, "text": "1",
                                                     "recovered": True}),
                                         json.dumps({"ts": 20, "kind": "send", "to": "a", "ok": True, "text": "2"}),
                                         '{"ts": 40, "kind": "se']))          # a write still in progress
        got = sent.messages()
        self.assertEqual([r["text"] for r in got], ["1", "2", "3"])          # by time, non-messages left out
        self.assertEqual([r["state"] for r in got], ["checked", "waiting", "waiting"])   # recovered ones: history
        self.assertEqual([r["ref"] for r in sent.messages()], [r["ref"] for r in got])   # refs made up, but stable
        self.assertTrue(all(sent.REF_RE.match(r["ref"]) for r in got))
        self.assertNotIn(sent.new_ref(), {r["ref"] for r in got})
        self.assertEqual([r["text"] for r in sent.messages(2)], ["2", "3"])
        res = actions.sent_action({"action": "sent", "limit": 2})
        self.assertEqual([r["text"] for r in res["messages"]], ["3", "2"])
        self.assertEqual(res["counts"], {"to_check": 0, "waiting": 2})
        self.assertEqual(len(actions.sent_action({"action": "sent"})["messages"]), 3)
        for bad in (0, -1, 2001, "5", True, 2.5):
            self.assertFalse(actions.sent_action({"action": "sent", "limit": bad})["ok"], bad)
        sent.PATH = sent.PATH.parent / "missing.jsonl"
        self.assertEqual((sent.messages(), sent.counts()), ([], {"to_check": 0, "waiting": 0}))


class UsageTest(unittest.TestCase):
    """Each runner answers for agent-usage, which asks the providers over the network."""

    def test_stand_in_reads_cache_and_swaps_to_agent_usage(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "usage-cache.json"
            cache.write_text(json.dumps({"codex": {"plan": "pro", "windows": [], "observed_at": NOW - 60},
                                         "claude:default": {"plan": "team", "windows": [{"kind": "session", "remaining_pct": 40}],
                                                            "observed_at": NOW - 30}}))
            u = sources.Usage(cache, runner=lambda refresh: None, sync=True)     # agent-usage not installed
            u.maybe_refresh(NOW)
            self.assertEqual(u.data["source"], "cache")
            self.assertEqual([a["target"] for a in u.data["accounts"]], ["claude:default", "codex"])
            self.assertTrue(all(a["stale"] for a in u.data["accounts"]))
            u = sources.Usage(cache, runner=lambda refresh: {"generated_at": NOW, "accounts": [{"target": "codex", "label": "Codex"}]},
                              sync=True)
            u.maybe_refresh(NOW)
            self.assertEqual((u.data["source"], u.data["accounts"][0]["label"]), ("agent-usage", "Codex"))

            for junk in ("null", "[1, 2]", "{\"codex\": 3}", "not json",
                         json.dumps({"codex": {"observed_at": "yesterday"}, "claude:x": {"observed_at": 5}})):
                cache.write_text(junk)
                u = sources.Usage(cache, runner=lambda refresh: None)       # must not raise
                self.assertIsInstance(u.data["accounts"], list)
            cache.write_text(json.dumps({"codex": {"plan": "pro", "windows": [], "observed_at": NOW - 60}}))

            def boom(refresh):
                raise RuntimeError("probe failed")
            u = sources.Usage(cache, runner=boom, sync=True)
            u.maybe_refresh(NOW)
            self.assertEqual((u.data["source"], u.data["error"]), ("cache", "RuntimeError: probe failed"))
            self.assertEqual(u.error, "RuntimeError: probe failed")
            u = sources.Usage(cache, runner=lambda refresh: None, sync=True)
            self.assertEqual(u.maybe_refresh(NOW)["source"], "cache")          # sync: read in this thread
            self.assertIsNone(u.error)



class CreditsTest(unittest.TestCase):
    RESETS = NOW + 3 * 86400

    def codex(self, left, bal, t, resets=None, **credit):
        return {"target": "codex", "observed_at": t,
                "windows": [{"kind": "weekly", "remaining_pct": left, "used_pct": 100 - left,
                             "resets_at": resets or self.RESETS}],
                "credits": dict({"has": True, "unlimited": False, "balance": bal}, **credit)}

    def test_counts_down_from_the_balance_the_week_ran_out_at_and_refills_at_the_reset(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "codex-credits.json"

            def bar(acct):
                credits.annotate([acct], path)
                return acct.get("credits_meter")

            b = bar(self.codex(40.0, "6000.00", NOW - 3600))          # the week has room: the number, a full bar
            self.assertEqual((b["balance"], b["spending"], b["left_pct"]), (6000.0, False, 100.0))
            b = bar(self.codex(0.0, "5950.5", NOW))                    # used up; some went before this reading
            self.assertEqual((b["full"], b["left_pct"], b["since"], b["resets_at"]), (6000.0, 99.2, NOW, self.RESETS))
            b = bar(self.codex(0.0, "4000", NOW + 600, resets=self.RESETS + 1))   # resets_at jitters by a second
            self.assertEqual((b["full"], b["left_pct"], b["since"], b["spending"]), (6000.0, 66.7, NOW, True))
            b = bar(self.codex(0.0, "9000", NOW + 1200))               # credits bought: a new full bar
            self.assertEqual((b["full"], b["left_pct"]), (9000.0, 100.0))
            b = bar(self.codex(0.0, "8100", NOW + 1800))
            self.assertEqual((b["full"], b["left_pct"]), (9000.0, 90.0))
            b = bar(self.codex(100.0, "8100", self.RESETS + 60, resets=self.RESETS + 7 * 86400))   # the week reset
            self.assertEqual((b["spending"], b["left_pct"], b["balance"]), (False, 100.0, 8100.0))
            self.assertEqual(json.loads(path.read_text()), {"room": {"balance": 8100.0, "at": self.RESETS + 60}})
            b = bar(self.codex(0.0, "8000", self.RESETS + 86400, resets=self.RESETS + 7 * 86400))
            self.assertEqual((b["full"], b["since"]), (8100.0, self.RESETS + 86400))

    def test_a_reading_without_the_weekly_window_keeps_the_full_bar(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "codex-credits.json"
            credits.annotate([self.codex(40.0, "6000", NOW - 3600)], path)
            credits.annotate([self.codex(0.0, "5000", NOW)], path)                # used up: 6000 is the full bar
            kept = path.read_text()
            a = dict(self.codex(0.0, "4900", NOW + 300), windows=[])
            credits.annotate([a], path)
            self.assertEqual((a["credits_meter"]["balance"], a["credits_meter"]["spending"]), (4900.0, False))
            self.assertEqual(path.read_text(), kept)                              # not taken for a week with room
            a = self.codex(0.0, "4800", NOW + 600)
            credits.annotate([a], path)
            self.assertEqual((a["credits_meter"]["full"], a["credits_meter"]["left_pct"]), (6000.0, 80.0))

    def test_a_first_reading_or_a_new_week_already_used_up_starts_from_its_own_balance(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "codex-credits.json"
            a = self.codex(0.0, "45000.0000000000", NOW)
            credits.annotate([a], path)                                 # no state yet
            self.assertEqual((a["credits_meter"]["full"], a["credits_meter"]["left_pct"]), (45000.0, 100.0))
            later = self.RESETS + 7 * 86400                             # the next week, never seen with room
            a = self.codex(0.0, "30000", self.RESETS + 3600, resets=later)
            credits.annotate([a], path)
            self.assertEqual((a["credits_meter"]["full"], a["credits_meter"]["since"]), (30000.0, self.RESETS + 3600))
            path.write_text(json.dumps({"room": {"balance": 99999.0, "at": NOW - 9 * 86400}}))   # a week before
            a = self.codex(0.0, "500", NOW)
            credits.annotate([a], path)
            self.assertEqual(a["credits_meter"]["full"], 500.0)
            for junk in ("not json", "[1]"):
                path.write_text(junk)
                with self.assertRaises(ValueError):
                    credits.annotate([self.codex(0.0, "500", NOW)], path)
                self.assertEqual(path.read_text(), junk)                 # left as it is: its full bar is not lost
            path.write_text(json.dumps({"full": "lots", "resets_at": self.RESETS}))
            a = self.codex(0.0, "500", NOW)
            credits.annotate([a], path)
            self.assertEqual(a["credits_meter"]["full"], 500.0)

    def test_no_bar_without_a_balance_and_only_codex_gets_one(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "codex-credits.json"
            accts = [self.codex(0.0, None, NOW), self.codex(0.0, "12", NOW, unlimited=True),
                     self.codex(0.0, "12", NOW, has=False),
                     {"target": "codex", "credits": None}, {"target": "codex"},
                     dict(self.codex(0.0, "12", NOW), target="claude:default")]
            credits.annotate(accts, path)
            self.assertFalse(any("credits_meter" in a for a in accts))
            self.assertFalse(path.exists())
            a = {"target": "codex", "credits": {"has": True, "balance": "12.5"}}     # no weekly window: not spending
            credits.annotate([a], path, now=NOW)
            self.assertEqual(a["credits_meter"], {"balance": 12.5, "spending": False, "left_pct": 100.0})
            self.assertFalse(path.exists())                                       # nor a week with room
            for bad in (self.codex(0.0, "lots", NOW), {"target": "codex", "credits": 7}):
                with self.assertRaises(ValueError):
                    credits.annotate([bad], path)

    def test_usage_readings_carry_the_bar(self):
        with tempfile.TemporaryDirectory() as d:
            path, cache = Path(d) / "codex-credits.json", Path(d) / "usage-cache.json"
            path.write_text(json.dumps({"room": {"balance": 60000.0, "at": NOW - 86400}}))
            reading = {"generated_at": NOW, "accounts": [self.codex(0.0, "45000.0000000000", NOW),
                                                         {"target": "claude:default", "windows": []}]}
            bar = lambda accounts: credits.annotate(accounts, path)
            # agent-usage asks the providers over the network: its readings are given here
            u = sources.Usage(cache, runner=lambda refresh: reading, annotate=bar, sync=True)
            u.maybe_refresh(NOW)
            m = u.data["accounts"][0]["credits_meter"]
            self.assertEqual((m["full"], m["left_pct"], m["spending"]), (60000.0, 75.0, True))
            self.assertNotIn("credits_meter", u.data["accounts"][1])
            self.assertIsNone(u.error)
            path.write_text("not json")
            u = sources.Usage(cache, runner=lambda refresh: {"accounts": [self.codex(0.0, "47000", NOW)]}, annotate=bar,
                              sync=True)
            u.maybe_refresh(NOW)                                         # the reading stands without the bar
            self.assertEqual((u.data["source"], u.data["accounts"][0]["target"]), ("agent-usage", "codex"))
            self.assertNotIn("credits_meter", u.data["accounts"][0])
            self.assertIn("ValueError: %s is not valid JSON" % path, u.error)
            self.assertEqual(path.read_text(), "not json")


if __name__ == "__main__":
    unittest.main()
