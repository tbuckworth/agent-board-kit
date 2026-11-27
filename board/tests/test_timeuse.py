"""The Time page: the owner's activity from each source, grouping (layout history, repo, guesses), 5-minute blocks, and
the refresh that writes time.json, and the sources that are off until configured. Every file goes to a temporary
folder."""
import datetime
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fixtures import git, iso, parse_file, write_jsonl  # noqa: E402
from agentboard import actions, google, groups, screen, sent, sources, timesources, timeuse  # noqa: E402

T0 = 1793000000.0          # a fixed moment, Monday 26 Oct 2026 07:33 UTC (02:33 in Chicago); blocks are 300 s
B0 = (T0 // 300) * 300     # the start of T0's block
HOURS = timeuse.DEFAULTS["work_hours"]
A, B, C = (str(uuid.UUID(int=n)) for n in (1, 2, 3))      # session ids
PAGE_2 = "EXAMPLE_PAGE_2"  # a fake continuation marker for a second page of results
ALL_HOURS = {"weekdays": [1, 2, 3, 4, 5, 6, 7], "start": "00:00", "end": "24:00"}


class LocalTime(unittest.TestCase):
    """Tests of wall-clock rules, run in one fixed zone, Chicago (where T0 is early on a Monday and 1 Nov 2026 has 25
    hours), whatever the machine's zone."""

    def setUp(self):
        self.saved_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/Chicago"
        time.tzset()

    def tearDown(self):
        if self.saved_tz is None:
            os.environ.pop("TZ")
        else:
            os.environ["TZ"] = self.saved_tz
        time.tzset()


class Sandbox:
    """Every path the Time page and the groups store write, moved to a temporary folder, and the Time page's config
    read from there too (DEFAULTS until a test writes one), never this machine's own."""
    NAMES = ("STATE", "OUT", "GUESSES", "TRANSCRIPTS", "MAIL_CACHE", "LOCK", "LOG")

    def __enter__(self):
        self.d = Path(tempfile.mkdtemp())
        self.saved = {k: getattr(timeuse, k) for k in self.NAMES + ("CONFIG",)}
        self.saved_groups, self.saved_audit, self.saved_sent, self.saved_screen = groups.GROUPS, actions.AUDIT, sent.PATH, screen.PATH
        for k in self.NAMES:
            setattr(timeuse, k, self.d if k == "STATE" else self.d / Path(str(self.saved[k])).name)
        timeuse.CONFIG = self.d / "time-config.json"          # its own name, time.json, is OUT's
        groups.GROUPS, actions.AUDIT, sent.PATH = self.d / "groups.json", self.d / "actions.jsonl", self.d / "sent.jsonl"
        screen.PATH = self.d / "screen.json"
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(timeuse, k, v)
        groups.GROUPS, actions.AUDIT, sent.PATH, screen.PATH = self.saved_groups, self.saved_audit, self.saved_sent, self.saved_screen


def google_client(routes):
    """A real google.Client whose HTTP layer, the network boundary, answers from `routes`: [(url prefix, answer)],
    an answer being a dict or a function of (url, params). The token request gets a stand-in token."""
    creds = Path(tempfile.mkdtemp()) / "creds.json"
    creds.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
    calls = []

    def http(method, url, data=None, headers=None, timeout=20):
        if url == google.TOKEN_URL:
            return {"access_token": "EXAMPLE", "expires_in": 3600}
        base, _, query = url.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        calls.append((base, params))
        for prefix, answer in routes:
            if base.startswith(prefix):
                return answer(base, params) if callable(answer) else answer
        raise AssertionError("unexpected URL " + base)
    client = google.Client(creds, http)
    client.calls = calls
    return client


# ------------------------------------------------------------------ transcripts

class Transcripts(unittest.TestCase):
    def test_claude_counts_only_what_the_owner_typed(self):
        d = Path(tempfile.mkdtemp())
        p = write_jsonl(d / "sid-1.jsonl", [
            {"type": "user", "entrypoint": "cli", "cwd": "/w/admin-x", "timestamp": iso(T0),
             "message": {"content": "You were started by agent-spawn on Alex's desktop ... Brief: buy a hat"}},
            {"type": "user", "timestamp": iso(T0 + 60), "message": {"content": "make it a red one"}},
            {"type": "user", "timestamp": iso(T0 + 120), "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            {"type": "user", "timestamp": iso(T0 + 180),
             "message": {"content": "Another Claude session sent a message:\n[relayed from Alex by email] also socks"}},
            {"type": "user", "timestamp": iso(T0 + 240), "isMeta": True, "message": {"content": "meta"}},
            {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(T0 + 300),
             "content": "[relayed from Alex by email] and a scarf"},
            {"type": "user", "timestamp": iso(T0 + 360), "message": {"content": "<command-name>/clear</command-name>"}},
        ])
        t = parse_file(timesources.parse_claude, p)
        self.assertTrue(t["interactive"])
        self.assertEqual([x[1] for x in t["typed"]], ["make it a red one"])
        self.assertEqual((t["spawn"], t["relay"]), (T0, [T0 + 180, T0 + 300]))

    def test_claude_print_runs_are_not_interactive(self):
        d = Path(tempfile.mkdtemp())
        p = write_jsonl(d / "orch.jsonl", [{"type": "user", "entrypoint": "sdk-cli", "cwd": "/w/orchestrator",
                                            "timestamp": iso(T0), "message": {"content": "an event"}}])
        self.assertFalse(parse_file(timesources.parse_claude, p)["interactive"])

    def test_codex_skips_context_by_its_prefix_and_subagents(self):
        d = Path(tempfile.mkdtemp())
        meta = {"type": "session_meta", "payload": {"id": "cx-1", "cwd": "/w/web", "source": "cli"}}
        msg = lambda ts, text: {"timestamp": iso(ts), "type": "response_item",
                                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}
        p = write_jsonl(d / "rollout-a.jsonl", [meta, msg(T0, "# AGENTS.md instructions for /w/web ..."),
                                                 msg(T0 + 30, "<environment_context>x</environment_context>"),
                                                 msg(T0 + 60, "plot the losses"),
                                                 msg(T0 + 90, "please update AGENTS.md with the new rule")])
        self.assertEqual([x[1] for x in parse_file(timesources.parse_codex, p)["typed"]],
                         ["plot the losses", "please update AGENTS.md with the new rule"])
        sub = write_jsonl(d / "rollout-b.jsonl", [{"type": "session_meta", "payload": {"id": "cx-2", "source": {"subagent": {}}}},
                                                   msg(T0, "review this")])
        self.assertEqual(parse_file(timesources.parse_codex, sub)["typed"], [])

    def test_transcripts_reads_each_file_on_from_where_it_stopped(self):
        home, codex, cache = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "c.json"
        proj = home / "projects" / "p"
        proj.mkdir(parents=True)
        f = write_jsonl(proj / "s1.jsonl", [{"type": "user", "entrypoint": "cli", "cwd": "/w", "timestamp": iso(T0),
                                             "message": {"content": "first"}}])
        said = []
        self.assertEqual(timesources.transcripts(0, cache, [home], codex, said.append)[0]["typed"][0][1], "first")
        doc = json.loads(cache.read_text())
        doc[f]["state"]["typed"] = [[T0, "from the cache"]]           # nothing new in the file: the cache answers
        cache.write_text(json.dumps(doc))
        self.assertEqual(timesources.transcripts(0, cache, [home], codex, said.append)[0]["typed"][0][1], "from the cache")
        with open(f, "a") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": iso(T0 + 60), "message": {"content": "second"}}) + "\n")
        self.assertEqual([x[1] for x in timesources.transcripts(0, cache, [home], codex, said.append)[0]["typed"]],
                         ["from the cache", "second"])                   # only the new line was read
        write_jsonl(f + ".new", [{"type": "user", "entrypoint": "cli", "cwd": "/w", "timestamp": iso(T0),
                                  "message": {"content": "first"}},
                                 {"type": "user", "timestamp": iso(T0 + 60), "message": {"content": "second"}}])
        os.replace(f + ".new", f)                                        # replaced (another inode): read afresh
        self.assertEqual([x[1] for x in timesources.transcripts(0, cache, [home], codex, said.append)[0]["typed"]],
                         ["first", "second"])
        self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)                # prompt snippets are private
        self.assertEqual(said, [])
        cache.write_text("{half a file")                                           # damaged: parsed afresh, and said
        self.assertEqual([x[1] for x in timesources.transcripts(0, cache, [home], codex, said.append)[0]["typed"]],
                         ["first", "second"])
        self.assertIn(f, json.loads(cache.read_text()))
        self.assertEqual(len(said), 1)
        self.assertIn("c.json is not valid JSON", said[0])

    def test_handoffs(self):
        parsed = [{"sid": "s1", "relay": [T0 + 100], "spawn": None}, {"sid": "s2", "relay": [], "spawn": T0 + 50}]
        hand = timesources.handoffs(parsed)
        self.assertEqual(timesources.handed_to(T0, hand, 900), "s2")
        self.assertEqual(timesources.handed_to(T0 + 60, hand, 900), "s1")
        self.assertIsNone(timesources.handed_to(T0 + 200, hand, 900))


class Activities(unittest.TestCase):
    def test_typed_prompts_are_counted_once_inside_the_window_with_a_label(self):
        p = {"interactive": True, "sid": A, "cwd": "/w/admin-hat", "typed": [[T0 - 99, "old"], [T0, "buy a hat"]]}
        copy = dict(p)                                     # the same session, copied to a second login
        bare = {"interactive": True, "sid": B, "cwd": "", "typed": [[T0, "x"]]}
        auto = {"interactive": False, "sid": C, "cwd": "/w", "typed": [[T0, "a print run"]]}
        acts = timesources.agent_activities([p, copy, bare, auto], {}, T0)
        self.assertEqual([(a["key"], a["label"], a["hint"]) for a in acts],
                         [("s:" + A, "admin-hat", "old"), ("s:" + B, B[:8], "x")])
        self.assertEqual(timesources.agent_activities([p], {A: "hat agent"}, T0)[0]["label"], "hat agent")

    def test_session_names_from_the_mailroom_then_live_sessions(self):
        db = Path(tempfile.mkdtemp()) / "mailroom.sqlite"
        con = sqlite3.connect(db)
        con.execute("create table sessions (tag text, session_id text, session_name text)")
        con.executemany("insert into sessions values (?,?,?)", [("t1", A, "old name"), ("t2", B, None)])
        con.commit()
        real = sources.claude_sessions_all
        sources.claude_sessions_all = lambda: [{"id": A, "name": "renamed"}, {"id": C, "name": "live one"}]   # this
        try:                                                                       # machine's live registry
            self.assertEqual(timesources.session_names(db), {A: "renamed", C: "live one"})
            self.assertEqual(timesources.session_names(db.with_name("none.sqlite")), {A: "renamed", C: "live one"})
        finally:
            sources.claude_sessions_all = real


# ------------------------------------------------------------------ board messages and mail to agents

class BoardAndMail(unittest.TestCase):
    def test_board_messages_with_and_without_a_session(self):
        with Sandbox():
            write_jsonl(sent.PATH, [{"ts": T0, "kind": "send", "to": "newsletter", "id": A, "text": "hi", "ok": True},
                                    {"ts": T0 + 10, "kind": "orchestrate", "text": "start a hat agent", "ok": True},
                                    {"ts": T0 + 20, "kind": "orchestrate", "text": "add milk", "ok": True},
                                    {"ts": T0 - 999, "kind": "send", "to": "old", "id": A, "text": "too old", "ok": True}])
            acts = timesources.board_messages(T0 - 1, {A: "newsletter"}, {}, [(T0 + 15, B)], 5)
        self.assertEqual([(a["key"], a["label"]) for a in acts][:2], [("s:" + A, "newsletter"), ("s:" + B, B[:8])])
        self.assertTrue(acts[2]["key"].startswith("orch:board:"))
        self.assertEqual(acts[2]["label"], "add milk")

    def test_orchestrator_mail_goes_to_the_agent_it_started(self):
        db = Path(tempfile.mkdtemp()) / "mailroom.sqlite"
        con = sqlite3.connect(db)
        con.executescript("""create table sessions (tag text, session_id text, session_name text);
            create table inbound (id integer, from_addr text, subject text, received_at integer, matched_tag text,
                                  match_rule text, delivered_session text);""")
        con.execute("insert into sessions values ('t1', 'sid-a', 'newsletter')")
        con.executemany("insert into inbound values (?,?,?,?,?,?,?)", [
            (1, "owner@example.com", "Re: plots", T0, "t1", "in-reply-to", None),
            (2, "owner@example.com", "start a hat agent", T0 + 600, None, "orchestrator", None),
            (3, "owner@example.com", "add milk to my to-do", T0 + 1200, None, "orchestrator", None),
            (4, "someone@else.com", "spam", T0, None, "orchestrator", None)])
        con.commit()
        acts = timesources.agent_emails(T0 - 10, {"sid-a": "newsletter"}, {}, [(T0 + 700, "sid-hat")], 900,
                                        ["owner@example.com"], db)
        self.assertEqual({a["label"] if a["key"].startswith("orch:") else a["key"] for a in acts},
                         {"s:sid-a", "s:sid-hat", "add milk to my to-do"})

    def test_an_unreadable_mailroom_is_a_source_error(self):
        db = Path(tempfile.mkdtemp()) / "mailroom.sqlite"
        db.write_text("not a database")
        with self.assertRaises(timesources.SourceError):
            timesources.agent_emails(0, {}, {}, [], 900, [], db)


# ------------------------------------------------------------------ Google and to-dos

class GoogleSources(unittest.TestCase):
    def test_calendar_skips_declined_and_all_day_pages_and_clips(self):
        pages = {None: {"items": [
            {"id": "e1", "summary": "Standup", "start": {"dateTime": iso(T0)}, "end": {"dateTime": iso(T0 + 1800)}},
            {"id": "e2", "summary": "Declined", "start": {"dateTime": iso(T0)}, "end": {"dateTime": iso(T0 + 600)},
             "attendees": [{"self": True, "responseStatus": "declined"}]},
            {"id": "e3", "summary": "Holiday", "start": {"date": "2026-10-30"}, "end": {"date": "2026-10-31"}}],
            "nextPageToken": PAGE_2},
            PAGE_2: {"items": [{"id": "e4", "summary": "Began earlier", "start": {"dateTime": iso(T0 - 3600)},
                                "end": {"dateTime": iso(T0 + 600)}},
                               {"id": "e5", "summary": "Runs on", "start": {"dateTime": iso(T0 + 3000)},
                                "end": {"dateTime": iso(T0 + 9000)}}]}}
        g = google_client([("https://www.googleapis.com/calendar/", lambda url, p: pages[p.get("pageToken")])])
        acts = timesources.calendar(T0, T0 + 3600, g, ALL_HOURS)
        self.assertEqual([(a["label"], a["ts"], a["end"]) for a in acts],
                         [("Standup", T0, T0 + 1800), ("Began earlier", T0, T0 + 600), ("Runs on", T0 + 3000, T0 + 3600)])

    def test_documents_by_revision_with_a_refused_file_and_folders_left_out(self):
        files = {"files": [{"id": "d1", "name": "Plan", "modifiedByMeTime": iso(T0 + 50)},
                           {"id": "d2", "name": "Shared", "modifiedByMeTime": iso(T0 + 90)}]}
        revs = {"revisions": [{"modifiedTime": iso(T0 + 10), "lastModifyingUser": {"me": True}},
                              {"modifiedTime": iso(T0 + 20), "lastModifyingUser": {"me": False}}]}

        def revisions(url, p):
            if "/d2/" in url:
                raise google.GoogleError("Google API answered HTTP 403", 403)
            return revs
        g = google_client([("https://www.googleapis.com/drive/v3/files/", revisions),
                        ("https://www.googleapis.com/drive/v3/files", files)])
        acts = timesources.documents(T0, g)
        self.assertEqual([(a["key"], a["ts"], a.get("approx", False)) for a in acts],
                         [("doc:d1", T0 + 10, False), ("doc:d2", T0 + 90, True)])
        q = g.calls[0][1]
        self.assertNotIn("folder", q["q"])
        self.assertIn("application/vnd.google-apps.document", q["q"])
        self.assertEqual((q["corpora"], q["supportsAllDrives"]), ("allDrives", "true"))

    def test_pages_follows_page_tokens_up_to_its_limit(self):
        endless = google_client([("https://example.invalid/items", lambda url, p: {"items": [p.get("pageToken", "first")],
                                                                                  "nextPageToken": PAGE_2})])
        self.assertEqual(list(endless.pages("https://example.invalid/items", {}, "items", max_pages=2)), ["first", PAGE_2])
        self.assertEqual(len(endless.calls), 2)
        last = google_client([("https://example.invalid/items", {"items": [1, 2]})])
        self.assertEqual(list(last.pages("https://example.invalid/items", {}, "items")), [1, 2])

    def test_documents_outage_is_not_mistaken_for_a_refusal(self):
        def down(url, p):
            raise google.GoogleError("Google API answered HTTP 503", 503)
        g = google_client([("https://www.googleapis.com/drive/v3/files/", down),
                        ("https://www.googleapis.com/drive/v3/files", {"files": [{"id": "d1", "name": "x", "modifiedByMeTime": iso(T0)}]})])
        with self.assertRaises(google.GoogleError):
            timesources.documents(T0 - 1, g)

    def test_own_email_leaves_out_the_agents_and_caches_inside_the_window(self):
        meta = {"m1": {"internalDate": str(int((T0 + 5) * 1000)), "threadId": "t1", "payload": {"headers": [{"name": "Subject", "value": "Dinner"}]}},
                "old": {"internalDate": str(int((T0 - 9999) * 1000)), "threadId": "t0", "payload": {"headers": []}}}
        g = google_client([("https://gmail.googleapis.com/gmail/v1/users/me/messages/", lambda url, p: meta[url.rsplit("/", 1)[1]]),
                        ("https://gmail.googleapis.com/gmail/v1/users/me/messages", {"messages": [{"id": "m1"}, {"id": "old"}]})])
        cache = Path(tempfile.mkdtemp()) / "mail.json"
        cache.write_text(json.dumps({"old": [T0 - 9999, "t0", "Long ago"]}))
        said = []
        acts = timesources.own_email(T0, g, "agents@example.com", cache, said.append)
        self.assertEqual([(a["key"], a["label"]) for a in acts], [("mail:t1", "Dinner")])
        self.assertIn("-to:agents@example.com", g.calls[0][1]["q"])
        self.assertEqual(set(json.loads(cache.read_text())), {"m1"})
        cache.write_text("{half a file")                                           # damaged: asked again, and said
        acts = timesources.own_email(T0, g, "agents@example.com", cache, said.append)
        self.assertEqual([(a["key"], a["label"]) for a in acts], [("mail:t1", "Dinner")])
        self.assertEqual(set(json.loads(cache.read_text())), {"m1"})
        self.assertEqual(len(said), 1)
        self.assertIn("mail.json is not valid JSON", said[0])

    def test_todos_and_a_failing_gh(self):
        out = json.dumps([{"number": 7, "title": "Buy milk", "createdAt": iso(T0 + 1), "closedAt": iso(T0 + 99)},
                          {"number": 8, "title": "Old", "createdAt": iso(T0 - 999), "closedAt": None}])
        ran = []

        def run(cmd, **kw):        # gh talks to GitHub: the boundary
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, out, "")
        acts = timesources.todos(T0, "me/tasks", run)
        self.assertEqual([(a["key"], a["ts"]) for a in acts], [("todo:7", T0 + 1), ("todo:7", T0 + 99)])
        self.assertIn("me/tasks", ran[0])
        with self.assertRaises(timesources.SourceError):
            timesources.todos(T0, "me/tasks", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no auth"))


class WorkHours(LocalTime):
    def test_calendar_counts_only_work_hours(self):
        day = datetime.datetime.fromtimestamp(T0).replace(hour=0, minute=0, second=0)          # Monday
        at = lambda d, h, m=0: (day + datetime.timedelta(days=d, hours=h, minutes=m)).timestamp()
        events = [("early", at(0, 8), at(0, 9, 30)), ("dinner", at(0, 19), at(0, 21)), ("late", at(0, 17), at(0, 19)),
                  ("saturday", at(5, 10), at(5, 11)), ("overnight", at(1, 17, 30), at(2, 9, 15))]
        g = google_client([("https://www.googleapis.com/calendar/", {"items": [
            {"id": n, "summary": n, "start": {"dateTime": iso(a)}, "end": {"dateTime": iso(b)}} for n, a, b in events]})])
        hours = {"weekdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}
        acts = timesources.calendar(at(0, 0), at(7, 0), g, hours)
        self.assertEqual([(a["label"], a["ts"], a["end"]) for a in acts],
                         [("early", at(0, 9), at(0, 9, 30)), ("late", at(0, 17), at(0, 18)),
                          ("overnight", at(1, 17, 30), at(1, 18)), ("overnight", at(2, 9), at(2, 9, 15))])

    def test_within_hours_across_the_clock_change(self):
        # 1 Nov 2026, a Sunday, is 25 hours long in Chicago: the hours are wall-clock times on each day
        sunday = datetime.datetime(2026, 11, 1)
        got = timesources.within_hours(sunday.timestamp(), (sunday + datetime.timedelta(days=2)).timestamp(),
                                       {"weekdays": [7, 1], "start": "09:00", "end": "24:00"})
        self.assertEqual([datetime.datetime.fromtimestamp(t).strftime("%a %H:%M") for pair in got for t in pair],
                         ["Sun 09:00", "Mon 00:00", "Mon 09:00", "Tue 00:00"])


# ------------------------------------------------------------------ the layout and its history

class Groups(unittest.TestCase):
    def test_history_keeps_closed_tiles_and_drops_ungrouped_live_ones(self):
        with Sandbox():
            layout = [{"name": "Work", "members": [A], "subgroups": [{"name": "Proj", "members": [B], "subgroups": []}]}]
            groups.save_groups(layout, live_ids={A, B})
            self.assertEqual(groups.history(), {A: {"group": "Work", "subgroup": None}, B: {"group": "Work", "subgroup": "Proj"}})
            groups.save_groups([{"name": "Admin", "members": [C], "subgroups": []}], live_ids={B, C})
            self.assertEqual(set(groups.history()), {A, C})        # A closed: kept; B running but ungrouped: gone
            self.assertEqual(groups.layout(), [{"name": "Admin", "subgroups": []}])

    def test_a_corrupt_history_raises_instead_of_being_replaced(self):
        with Sandbox():
            groups.history_path().write_text("{not json")
            with self.assertRaises(ValueError):
                groups.save_groups([{"name": "Work", "members": [A], "subgroups": []}], live_ids=set())
            self.assertEqual(groups.history_path().read_text(), "{not json")
            self.assertFalse(groups.GROUPS.exists())             # nor was the layout saved without its history

    def test_the_groups_action_stores_the_history_in_the_sandbox(self):
        with Sandbox() as sb:
            self.assertTrue(actions.groups_action({"groups": [{"name": "Work", "members": [A]}]}, [])["ok"])
            self.assertEqual(groups.history()[A]["group"], "Work")
            self.assertEqual(groups.history_path().parent, sb.d)


    def test_no_layout_yet_and_a_corrupt_one(self):
        with Sandbox():
            self.assertEqual((groups.stored(), groups.layout(), groups.history()), ({}, [], {}))
            for text in ("{not json", "null", "[]"):
                groups.GROUPS.write_text(text)
                with self.assertRaises(ValueError, msg=text):
                    groups.layout()


class Config(unittest.TestCase):
    def test_load_config_missing_malformed_and_not_an_object(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(sources.load_config({"a": 1}, d / "none.json"), {"a": 1})
        (d / "c.json").write_text('{"a": 2, "b": 3}')
        self.assertEqual(sources.load_config({"a": 1}, d / "c.json"), {"a": 2, "b": 3})
        for text in ("{not json", "[1, 2]"):
            (d / "c.json").write_text(text)
            with self.assertRaises(ValueError, msg=text):
                sources.load_config({"a": 1}, d / "c.json")

    def test_the_time_page_refuses_a_malformed_config(self):
        """A malformed config is not read as no config at all, to run on the defaults without a word."""
        bad = Path(tempfile.mkdtemp()) / "bad.json"
        bad.write_text("{not json")
        for module in (timeuse,):
            real = module.CONFIG
            module.CONFIG = bad
            try:
                with self.assertRaises(ValueError, msg=module.__name__):
                    module.config()
            finally:
                module.CONFIG = real


class RepoName(unittest.TestCase):
    def test_a_removed_folder_is_matched_past_entries_it_cannot_read(self):
        root = Path(tempfile.mkdtemp())
        git("init", "-q", "-b", "main", str(root / "admin"))
        (root / "locked").mkdir()
        os.chmod(root / "locked", 0)                     # its .git cannot even be looked for
        try:
            self.assertEqual(sources.repo_name(str(root / "admin-todo-99")), "admin")
        finally:
            os.chmod(root / "locked", 0o700)

    def test_worktrees_removed_worktrees_and_none(self):
        root = Path(tempfile.mkdtemp())
        git("init", "-q", "-b", "main", str(root / "admin"))
        git("-C", str(root / "admin"), "commit", "-q", "--allow-empty", "-m", "x")
        git("-C", str(root / "admin"), "worktree", "add", "-q", "-b", "t1", str(root / "admin-todo-1"))
        git("init", "-q", "-b", "main", str(root / "admin-tools"))
        (root / "inbox-worktrees" / "7-x").mkdir(parents=True)
        git("init", "-q", "-b", "main", str(root / "inbox"))
        self.assertEqual(sources.repo_name(str(root / "admin-todo-1")), "admin")             # a live worktree
        self.assertEqual(sources.repo_name(str(root / "admin-todo-99")), "admin")            # removed
        self.assertEqual(sources.repo_name(str(root / "admin-tools-fix")), "admin-tools")    # the longest match
        self.assertEqual(sources.repo_name(str(root / "inbox-worktrees" / "7-x" / "inbox")), "inbox")
        self.assertEqual(sources.repo_name(str(root / "elsewhere")), "")
        self.assertEqual(sources.repo_name(""), "")


# ------------------------------------------------------------------ grouping, guesses, blocks

class Grouping(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        for r in ("proj", "lone"):
            git("init", "-q", "-b", "main", str(self.root / r))

    def act(self, key, repo=None, kind="prompt", ts=T0):
        return {"ts": ts, "kind": kind, "key": key, "label": key, "repo": str(self.root / repo) if repo else ""}

    def test_history_then_repo_then_guess(self):
        layout = [{"name": "Work", "subgroups": ["Proj"]}, {"name": "Personal", "subgroups": []}]
        acts = [self.act("s:" + A, "proj"), self.act("s:" + B, "proj"), self.act("s:" + C, "lone"),
                self.act("cal:1", kind="meeting"), self.act("mail:2", kind="mail")]
        seen = {}

        def guesser(items, layout_, history, labels):       # the model is the boundary (a paid call)
            seen.update(items)
            return {"s:" + C: ["Personal", None], "cal:1": ["Work", None], "mail:2": None}, {"asked": 3, "batches": 1}, None
        out, stats, warning = timeuse.assign(acts, {A: {"group": "Work", "subgroup": "Proj"}}, layout, {}, {}, guesser)
        self.assertEqual(out["s:" + A], ("Work", "Proj", False))
        self.assertEqual(out["s:" + B], ("Work", None, True))        # its repo's grouped tile is in Work
        self.assertEqual(out["s:" + C], ("Personal", None, True))
        self.assertEqual(out["cal:1"], ("Work", None, True))
        self.assertEqual(out["mail:2"], (timeuse.UNSORTED, None, True))
        self.assertEqual(set(seen), {"s:" + C, "cal:1", "mail:2"})
        self.assertEqual((stats["asked"], warning), (3, None))

    def test_a_group_no_longer_on_the_board_is_not_used(self):
        layout = [{"name": "Work", "subgroups": []}]
        # the model's guesses, a paid call: everything in Work
        guesser = lambda items, *a: ({k: ["Work", None] for k in items}, {"asked": 1, "batches": 1}, None)
        out, _, _ = timeuse.assign([self.act("s:" + A, "lone")], {A: {"group": "Old name", "subgroup": None}}, layout, {},
                                   {}, guesser)
        self.assertEqual(out["s:" + A], ("Work", None, True))

    def test_the_repo_vote_counts_tiles_not_prompts(self):
        layout = [{"name": "Work", "subgroups": []}, {"name": "Personal", "subgroups": []}]
        history = {A: {"group": "Personal", "subgroup": None}, B: {"group": "Work", "subgroup": None},
                   C: {"group": "Work", "subgroup": None}}
        acts = [self.act("s:" + A, "proj", ts=T0 + i) for i in range(20)] + [self.act("s:" + B, "proj"), self.act("s:" + C, "proj"),
                                                                             self.act("s:new", "proj")]
        guesser = lambda *a: ({}, {"asked": 0, "batches": 0}, None)        # the model, a paid call: no answer
        out, _, _ = timeuse.assign(acts, history, layout, {}, {}, guesser)
        self.assertEqual(out["s:new"], ("Work", None, True))

    def test_a_fixed_item_counts_in_its_group_and_is_never_guessed(self):
        layout = [{"name": "Work", "subgroups": []}, {"name": "Personal", "subgroups": []}]
        asked = {}

        def guesser(items, *a):                             # the model is the boundary (a paid call)
            asked.update(items)
            return {k: ["Work", None] for k in items}, {"asked": len(items), "batches": 1}, None
        acts = [self.act("todo:7", kind="todo"), self.act("todo:8", kind="todo")]
        out, _, _ = timeuse.assign(acts, {}, layout, {}, {"todo:7": "Personal"}, guesser)
        self.assertEqual(out["todo:7"], ("Personal", None, False))
        self.assertEqual(out["todo:8"], ("Work", None, True))
        self.assertEqual(set(asked), {"todo:8"})

    def test_fixed_groups_off_the_board_are_named_and_a_malformed_setting_raises(self):
        layout = [{"name": "Personal", "subgroups": []}]
        warnings = []
        cfg = dict(timeuse.DEFAULTS, fixed_groups={"todo:1": "Personal", "todo:2": "Gardening"})
        self.assertEqual(timeuse.fixed_groups(cfg, layout, warnings), {"todo:1": "Personal"})
        self.assertEqual(warnings, ["fixed_groups: todo:2 names Gardening, which is not a group on the board; "
                                    "it is grouped as usual"])
        for bad in (["todo:1"], {"todo:1": ["Personal"]}):
            with self.assertRaises(ValueError):
                timeuse.fixed_groups(dict(timeuse.DEFAULTS, fixed_groups=bad), layout, [])

    def test_parse_guesses_keeps_only_known_names(self):
        layout = [{"name": "Work", "subgroups": ["Proj"]}, {"name": "Admin", "subgroups": []}]
        got = timeuse.parse_guesses('Here: {"x": "Work: Proj", "y": "Work: Nope", "z": "Gardening", "w": null, "v": "Admin"}', layout)
        self.assertEqual(got, {"x": ["Work", "Proj"], "y": ["Work", None], "z": None, "w": None, "v": ["Admin", None]})
        with self.assertRaises(ValueError):
            timeuse.parse_guesses("no json here", layout)

    def test_parse_guesses_with_colons_in_group_names(self):
        layout = [{"name": "Work: AI", "subgroups": ["Proj"]}, {"name": "Work", "subgroups": []}]
        got = timeuse.parse_guesses('{"x": "Work: AI", "y": "Work: AI: Proj", "z": "Work"}', layout)
        self.assertEqual(got, {"x": ["Work: AI", None], "y": ["Work: AI", "Proj"], "z": ["Work", None]})


class Guess(unittest.TestCase):
    def test_prompt_on_stdin_cache_per_batch_and_unanswered_asked_again(self):
        d = Path(tempfile.mkdtemp())
        calls = []

        def run(cmd, **kw):          # `claude -p` makes a paid model call: the boundary
            calls.append((cmd, kw["input"]))
            self.assertEqual(cmd[cmd.index("--tools") + 1], "")
            answer = {"cal:1": "Work"} if "cal:1" in kw["input"] else {}
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps(answer)}), "")
        real = sources.find_bin
        sources.find_bin = lambda n: "/bin/" + n          # no claude CLI is needed where the boundary is faked
        try:
            cfg = dict(timeuse.DEFAULTS, guess_batch=1)
            layout = [{"name": "Work", "subgroups": []}]
            items = {"cal:1": {"kind": "meeting", "label": "Private standup"}, "mail:2": {"kind": "mail", "label": "x"}}
            got, stats, warning = timeuse.guess(items, layout, {}, {}, cfg, d / "g.json", run)
            self.assertEqual((got, stats, warning), ({"cal:1": ["Work", None], "mail:2": None}, {"asked": 2, "batches": 2}, None))
            self.assertNotIn("Private standup", " ".join(calls[0][0]))
            self.assertIn("Private standup", calls[0][1])
            timeuse.guess(items, layout, {}, {}, cfg, d / "g.json", run)
            self.assertEqual(len(calls), 3)                    # only mail:2, unanswered before, is asked again
            timeuse.guess(items, layout + [{"name": "Admin", "subgroups": []}], {}, {}, cfg, d / "g.json", run)
            self.assertEqual(len(calls), 5)                    # a new layout asks everything again
            self.assertEqual(stat.S_IMODE((d / "g.json").stat().st_mode), 0o600)
            timeuse.guess(items, layout, {}, {}, dict(cfg, guess_model="opus"), d / "g.json", run)
            self.assertEqual(len(calls), 7)                    # so does another model
            with Sandbox():
                (d / "g.json").write_text("{half a file")
                timeuse.guess(items, layout, {}, {}, cfg, d / "g.json", run)
                self.assertEqual(len(calls), 9)                # a damaged cache: everything is asked again, and said
                self.assertIn("g.json is not valid JSON", timeuse.LOG.read_text())
        finally:
            sources.find_bin = real


class GuessFailures(unittest.TestCase):
    """Each way guessing can fail leaves the items unguessed (Unsorted) and says why. Each `run` answers for
    `claude -p`, a paid model call."""
    ITEMS = {"cal:1": {"kind": "meeting", "label": "Standup"}}
    LAYOUT = [{"name": "Work", "subgroups": []}]

    def run_guess(self, run, layout=LAYOUT, claude="/bin/claude"):
        real = sources.find_bin
        sources.find_bin = lambda n: claude          # where the claude CLI is: whatever this machine has
        try:
            return timeuse.guess(self.ITEMS, layout, {}, {}, timeuse.DEFAULTS, Path(tempfile.mkdtemp()) / "g.json", run)
        finally:
            sources.find_bin = real

    def test_claude_fails(self):
        got, stats, warning = self.run_guess(lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "rate limited"))
        self.assertEqual((got, stats["batches"]), ({"cal:1": None}, 1))
        self.assertIn("claude exited 1: rate limited", warning)

    def test_a_bad_answer(self):
        got, _, warning = self.run_guess(lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "no idea"}), ""))
        self.assertEqual(got, {"cal:1": None})
        self.assertIn("no JSON object", warning)

    def test_no_claude_and_no_layout(self):
        never = lambda cmd, **kw: self.fail("no model call should be made")
        self.assertEqual(self.run_guess(never, claude=None)[2], "no claude CLI to guess groups")
        self.assertEqual(self.run_guess(never, layout=[])[2], "no board groups to guess into")


class GuessesOff(unittest.TestCase):
    """"guess_model": null in time.json switches the guesses off: no model call at all, what nothing else groups is
    Unsorted, and that is no warning."""

    def test_no_model_call_and_no_warning(self):
        never = lambda cmd, **kw: self.fail("no model call should be made")
        real = sources.find_bin
        sources.find_bin = lambda n: self.fail("the claude CLI is not even looked for")
        try:
            got, stats, warning = timeuse.guess({"cal:1": {"kind": "meeting", "label": "Standup"}},
                                                [{"name": "Work", "subgroups": []}], {}, {},
                                                dict(timeuse.DEFAULTS, guess_model=None), Path(tempfile.mkdtemp()) / "g.json",
                                                never)
        finally:
            sources.find_bin = real
        self.assertEqual((got, stats, warning), ({"cal:1": None}, {"asked": 0, "batches": 0}, None))

    def test_a_refresh_with_guesses_off_puts_the_rest_in_unsorted(self):
        with Sandbox():
            now = T0
            timeuse.CONFIG.write_text(json.dumps({"days": 2, "work_hours": ALL_HOURS, "guess_model": None}))
            groups.save_groups([{"name": "Work", "members": [], "subgroups": []}], live_ids=set())
            table = lambda since, until, cfg, warnings: [
                ("calendar", lambda: [{"ts": now - 600, "end": now - 300, "kind": "meeting", "key": "cal:1",
                                       "label": "Standup"}])]
            real = subprocess.run
            subprocess.run = lambda *a, **kw: self.fail("no model call should be made")
            try:
                doc = timeuse.refresh(timeuse.config(), now=now, table=table)
            finally:
                subprocess.run = real
        self.assertEqual(doc["config"]["guess_model"], None)
        self.assertEqual((doc["warnings"], doc["summary"]["guessed"]), ([], {"asked": 0, "batches": 0}))
        self.assertEqual([r["group"] for r in doc["daily"][-1]["rows"]], [timeuse.UNSORTED])


class Blocks(unittest.TestCase):
    def test_a_block_is_shared_never_double_counted_and_every_day_listed(self):
        acts = [{"ts": B0 + 10, "kind": "prompt", "key": "s:a", "label": "A"},
                {"ts": B0 + 20, "kind": "prompt", "key": "s:a", "label": "A"},
                {"ts": B0 + 30, "kind": "email", "key": "s:b", "label": "B"},
                {"ts": B0 + 400, "kind": "prompt", "key": "s:a", "label": "A"}]
        day = datetime.date.fromtimestamp(B0).isoformat()
        before = (datetime.date.fromtimestamp(B0) - datetime.timedelta(1)).isoformat()
        daily, items = timeuse.aggregate(acts, {"s:a": ("Work", None, False), "s:b": ("Admin", None, False)}, [before, day],
                                         HOURS)
        self.assertEqual([d["day"] for d in daily], [before, day])
        self.assertEqual(daily[0]["rows"], [])
        self.assertAlmostEqual(sum(r["minutes"] for r in daily[1]["rows"]), 10.0)
        per = {i["key"]: i["minutes"] for i in items}
        self.assertEqual((per["s:a"], per["s:b"]), (7.5, 2.5))
        a = next(i for i in items if i["key"] == "s:a")       # how its 7.5 minutes came about
        self.assertEqual((a["blocks"], a["kinds"]), ([B0, B0 + 300], {"prompt": 3}))

    def test_a_meeting_fills_its_blocks(self):
        acts = [{"ts": B0, "end": B0 + 1800, "kind": "meeting", "key": "cal:1", "label": "Standup"}]
        daily, _ = timeuse.aggregate(acts, {"cal:1": ("Work", None, True)}, [datetime.date.fromtimestamp(B0).isoformat()],
                                     HOURS)
        self.assertAlmostEqual(sum(r["minutes"] for r in daily[0]["rows"]), 30.0)

    def test_work_time_is_split_out_by_the_work_hours(self):
        monday = datetime.datetime(2026, 11, 2)                              # local time
        at = lambda h, m: (monday + datetime.timedelta(hours=h, minutes=m)).timestamp()
        acts = [{"ts": at(8, 57), "kind": "prompt", "key": "s:a", "label": "A"},       # the block before 09:00
                {"ts": at(9, 2), "kind": "prompt", "key": "s:a", "label": "A"},
                {"ts": at(17, 58), "kind": "prompt", "key": "s:a", "label": "A"},      # the last block before 18:00
                {"ts": at(18, 0), "kind": "prompt", "key": "s:a", "label": "A"},
                {"ts": at(5 * 24 + 10, 0), "kind": "prompt", "key": "s:a", "label": "A"}]   # Saturday morning
        days = [(monday.date() + datetime.timedelta(n)).isoformat() for n in (0, 5)]
        daily, items = timeuse.aggregate(acts, {"s:a": ("Work", None, False)}, days, HOURS)
        self.assertEqual([(d["day"], d["workday"]) for d in daily], [(days[0], True), (days[1], False)])
        self.assertEqual(daily[0]["rows"], [{"group": "Work", "subgroup": None, "inferred": False, "minutes": 20.0,
                                             "work_minutes": 10.0}])
        self.assertEqual(daily[1]["rows"][0]["work_minutes"], 0.0)
        mon = next(i for i in items if i["day"] == days[0])
        self.assertEqual((mon["minutes"], mon["work_minutes"], mon["work_blocks"]), (20.0, 10.0, [at(9, 0), at(17, 55)]))
        self.assertEqual(mon["blocks"], [at(8, 55), at(9, 0), at(17, 55), at(18, 0)])


# ------------------------------------------------------------------ screen time from the Mac

class ScreenTime(unittest.TestCase):
    def test_clean_checks_every_block_and_shortens_long_names(self):
        start = int(B0)
        got, err = screen.clean([[start, "Safari", "arXiv"], [start + 300, "x" * 500, "y" * 500]], 300)
        self.assertIsNone(err)
        self.assertEqual(got[0], [start, "Safari", "arXiv"])
        self.assertEqual((len(got[1][1]), len(got[1][2])), (screen.APP_CHARS, screen.TITLE_CHARS))
        for bad in ("blocks", [[start, "Safari"]], [[start + 1, "Safari", "t"]], [[float(start), "Safari", "t"]],
                    [[True, "Safari", "t"]], [[start, 3, "t"]], [[start, "Safari", None]], [(start, "Safari", "t")]):
            self.assertIsNone(screen.clean(bad, 300)[0], bad)
        self.assertEqual(screen.clean([], 300), ([], None))

    def test_store_replaces_a_block_sent_again_and_drops_blocks_before_the_span(self):
        path = Path(tempfile.mkdtemp()) / "state" / "screen.json"
        start = int(B0)
        screen.store([[start - 300, "Mail", "Inbox"], [start, "Safari", "arX"]], start - 300, path)
        held = screen.store([[start, "Safari", "arXiv"], [start + 300, "Zoom", "Standup"]], start, path)
        self.assertEqual(held, [[start, "Safari", "arXiv"], [start + 300, "Zoom", "Standup"]])
        self.assertEqual(json.loads(path.read_text()), held)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_damaged_store_raises_instead_of_being_replaced(self):
        path = Path(tempfile.mkdtemp()) / "screen.json"
        path.write_text("[[1793000100, \"Saf")
        with self.assertRaises(ValueError):
            screen.store([[int(B0), "Safari", "arXiv"]], B0, path)
        with self.assertRaises(ValueError):            # a ValueError leaves the source out of the page, by name
            timesources.screen_time(B0, path)
        self.assertEqual(path.read_text(), "[[1793000100, \"Saf")

    def test_the_action_answers_where_the_next_upload_starts(self):
        with Sandbox():
            _, since = timeuse.span(timeuse.config(), time.time())
            first = actions.screen_action({"blocks": []})
            self.assertEqual((first["ok"], first["held"], first["next"], first["block"]), (True, 0, int(since), 300))
            now = int(time.time() // 300 * 300)
            got = actions.screen_action({"blocks": [[now - 300, "Safari", "arXiv"], [now, "Zoom", "Standup"]]})
            self.assertEqual((got["held"], got["next"]), (2, now))
            self.assertFalse(actions.screen_action({"blocks": [[now + 7, "Safari", "arXiv"]]})["ok"])
            self.assertFalse(actions.screen_action({})["ok"])
            self.assertEqual(len(json.loads(screen.PATH.read_text())), 2)

    def test_the_board_command_takes_an_upload(self):
        home = Path(tempfile.mkdtemp())       # the command runs as on the desktop, with this folder as its home
        start = int(time.time() // 300 * 300)
        r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent.parent / "bin" / "agent-board"), "act"],
                           input=json.dumps({"action": "screen", "blocks": [[start, "Safari", "arXiv"]]}),
                           env=dict(os.environ, HOME=str(home)), capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, json.loads(r.stdout)["next"]), (0, start), r.stderr)
        self.assertEqual(json.loads((home / ".local/state/agent-board/screen.json").read_text()), [[start, "Safari", "arXiv"]])

    def test_an_upload_from_the_stream_keeps_non_ascii_titles_within_the_request_limit(self):
        # The stream hands each action to `agent-board act` as JSON again. Escaped (an em dash would be six
        # characters), a part the Mac sized well under the limit would pass it and arrive cut off.
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("agent_board_cli", str(Path(__file__).resolve().parent.parent / "bin" / "agent-board"))
        cli = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(cli)
        start = int(time.time() // 300 * 300)
        blocks = [[start - 300 * n, "Safari", "\u2014" * 190] for n in range(120)]      # ~23 000 characters
        self.assertLess(len(json.dumps({"action": "screen", "blocks": blocks}, ensure_ascii=False)), cli.MAX_REQUEST // 2)
        home, saved = tempfile.mkdtemp(), os.environ.get("HOME")
        os.environ["HOME"] = home              # the action runs as on the desktop, with this folder as its home
        try:
            out, err = cli.run_act({"action": "screen", "blocks": blocks})
        finally:
            os.environ["HOME"] = saved
        self.assertTrue(json.loads(out)["ok"], err)
        self.assertEqual(len(json.loads((Path(home) / ".local/state/agent-board/screen.json").read_text())), 120)

    def test_the_source_reads_the_span_and_names_a_missing_upload(self):
        path = Path(tempfile.mkdtemp()) / "screen.json"
        with self.assertRaisesRegex(timesources.SourceError, "nothing uploaded yet"):
            timesources.screen_time(B0, path)
        path.write_text(json.dumps([[int(B0) - 300, "Mail", "Inbox"], [int(B0), "Safari", "arXiv"], [int(B0) + 300, "Finder", ""],
                                    [int(B0) + 600, "Chrome", "Inbox (4) - Gmail"], [int(B0) + 900, "Chrome", "(99+) Inbox - Gmail"],
                                    [int(B0) + 1200, "Preview", "report (2).pdf"], [int(B0) + 1500, "Chrome", "Paper (2023) - arXiv"],
                                    [int(B0) + 1800, "Chrome", "Inbox (1,204) - Gmail"]]))
        self.assertEqual(timesources.screen_time(B0, path),
                         [{"ts": B0, "kind": "screen", "key": "screen:Safari|arXiv", "label": "Safari: arXiv"},
                          {"ts": B0 + 300, "kind": "screen", "key": "screen:Finder|", "label": "Finder"},
                          {"ts": B0 + 600, "kind": "screen", "key": "screen:Chrome|Inbox - Gmail", "label": "Chrome: Inbox - Gmail"},
                          {"ts": B0 + 900, "kind": "screen", "key": "screen:Chrome|Inbox - Gmail", "label": "Chrome: Inbox - Gmail"},
                          {"ts": B0 + 1200, "kind": "screen", "key": "screen:Preview|report (2).pdf", "label": "Preview: report (2).pdf"},
                          {"ts": B0 + 1500, "kind": "screen", "key": "screen:Chrome|Paper (2023) - arXiv", "label": "Chrome: Paper (2023) - arXiv"},
                          {"ts": B0 + 1800, "kind": "screen", "key": "screen:Chrome|Inbox - Gmail", "label": "Chrome: Inbox - Gmail"}])

    def test_screen_time_counts_only_where_nothing_else_does(self):
        acts = [{"ts": B0 + 10, "kind": "prompt", "key": "s:a", "label": "A"},
                {"ts": B0, "kind": "screen", "key": "screen:Terminal|claude", "label": "Terminal: claude"},
                {"ts": B0 + 300, "kind": "screen", "key": "screen:Safari|arXiv", "label": "Safari: arXiv"},
                {"ts": B0 + 600, "end": B0 + 1200, "kind": "meeting", "key": "cal:1", "label": "Standup"},
                {"ts": B0 + 900, "kind": "screen", "key": "screen:Zoom|Standup", "label": "Zoom: Standup"}]
        kept = timeuse.fill_gaps(acts)
        self.assertEqual(sorted(a["key"] for a in kept), ["cal:1", "s:a", "screen:Safari|arXiv"])
        day = datetime.date.fromtimestamp(B0).isoformat()
        daily, _ = timeuse.aggregate(kept, {"s:a": ("Work", None, False), "cal:1": ("Work", None, True),
                                            "screen:Safari|arXiv": ("Research", None, True)}, [day], HOURS)
        self.assertAlmostEqual(sum(r["minutes"] for r in daily[0]["rows"]), 20.0)


# ------------------------------------------------------------------ refresh and the action

class Refresh(unittest.TestCase):
    def test_refresh_writes_every_day_privately_and_the_action_reads_it(self):
        with Sandbox():
            # a fixed moment and fixed work hours, which the action's recompute reads from the config file: the
            # figures never depend on when or where the test runs (in Chicago, T0 is before the default hours)
            now = T0
            timeuse.CONFIG.write_text(json.dumps({"days": 3, "work_hours": ALL_HOURS}))
            groups.save_groups([{"name": "Work", "members": [A], "subgroups": []}], live_ids=set())
            # the real sources read this machine's transcripts, mailroom and Google: one source stands in for them
            table = lambda since, until, cfg, warnings: [
                ("prompts", lambda: [{"ts": now - 60, "kind": "prompt", "key": "s:" + A, "label": "proj", "repo": ""}])]
            cfg = timeuse.config()
            self.assertEqual(cfg, dict(timeuse.DEFAULTS, days=3, work_hours=ALL_HOURS))
            doc = timeuse.refresh(cfg, now=now, table=table)
            self.assertEqual((len(doc["daily"]), doc["block"]), (3, 300))
            self.assertEqual(doc["unsorted"], timeuse.UNSORTED)                 # the Mac reads the name from here
            self.assertEqual(doc["colours"], {"groups": [{"name": "Work", "colour": "#0E8A84", "shades": []}],
                                              "ungrouped": "#8C877F"})           # the Mail tab's colours too
            self.assertEqual(json.loads(timeuse.OUT.read_text())["config"], cfg)
            self.assertEqual(doc["daily"][-1]["rows"], [{"group": "Work", "subgroup": None, "inferred": False, "minutes": 5.0,
                                                         "work_minutes": 5.0}])
            self.assertEqual(doc["format"], timeuse.FORMAT)
            self.assertEqual(stat.S_IMODE(timeuse.OUT.stat().st_mode), 0o600)
            got = actions.time_action({})
            self.assertEqual(got["time"]["daily"], doc["daily"])
            self.assertFalse(actions.time_action({"refresh": "yes"})["ok"])
            actions.time_action({"refresh": True}, table=table, now=now)
            self.assertIn('"action": "time"', actions.AUDIT.read_text())     # a recompute is on the record
            timeuse.OUT.write_text("{half a file")                           # damaged: recomputed, and said
            self.assertEqual(actions.time_action({}, table=table, now=now)["time"]["daily"][-1]["rows"],
                             doc["daily"][-1]["rows"])
            self.assertEqual(json.loads(timeuse.OUT.read_text())["block"], 300)
            self.assertIn("time.json is not valid JSON", timeuse.LOG.read_text())
            timeuse.OUT.write_text(json.dumps(dict(doc, format=1)))         # an older refresh's: recomputed, and said
            self.assertEqual(actions.time_action({}, table=table, now=now)["time"]["format"], timeuse.FORMAT)
            self.assertIn("time.json is of format 1, not %d: recomputing" % timeuse.FORMAT, timeuse.LOG.read_text())

    def test_refresh_counts_a_fixed_item_in_its_group_without_a_guess(self):
        with Sandbox():
            now = T0                 # a fixed moment: now - 60 is on the same day wherever the test runs
            groups.save_groups([{"name": "Personal", "members": [], "subgroups": []}], live_ids=set())
            # the to-dos are read from GitHub, a network service: one source stands in for it
            table = lambda since, until, cfg, warnings: [
                ("to-dos", lambda: [{"ts": now - 60, "kind": "todo", "key": "todo:7", "label": "Book the dentist"}])]
            cfg = dict(timeuse.DEFAULTS, days=3, fixed_groups={"todo:7": "Personal"}, work_hours=ALL_HOURS)
            doc = timeuse.refresh(cfg, now=now, table=table)
        self.assertEqual(doc["daily"][-1]["rows"], [{"group": "Personal", "subgroup": None, "inferred": False, "minutes": 5.0,
                                                     "work_minutes": 5.0}])
        self.assertEqual((doc["summary"]["guessed"]["asked"], doc["warnings"]), (0, []))

    def test_a_failing_source_is_left_out_by_name(self):
        # gh asks GitHub, a network service: its answer stands in. No auth for the to-dos
        no_auth = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no auth")
        with Sandbox():
            write_jsonl(sent.PATH, [{"ts": T0, "kind": "send", "to": "x", "id": A, "text": "hi", "ok": True}])
            table = [("board messages", lambda: timesources.board_messages(T0 - 1, {}, {}, [], 900)),
                     ("to-dos", lambda: timesources.todos(T0 - 1, "o/r", no_auth)),
                     ("screen time", lambda: timesources.screen_time(T0 - 1, screen.PATH))]
            warnings, waiting = [], []
            acts, labels = timeuse.collect(table, warnings, waiting)
        self.assertEqual(len(acts), 1)
        self.assertEqual(labels, {A: "x"})
        self.assertEqual(warnings, ["to-dos: gh issue list failed: no auth"])
        # a device that has uploaded nothing yet is waiting, not a failure (default_sources does not even list it)
        self.assertEqual(waiting, ["screen time: nothing uploaded yet from the Mac app, which reads ActivityWatch"])

    def default_table(self, sb):
        """default_sources' names and warnings, with no transcripts, sessions or mailroom (this machine's own stay out
        of it)."""
        names = ("DEFAULT_CLAUDE_HOME", "CODEX_HOME", "MAIL_CONFIG", "MAIL_DB")
        saved = {k: getattr(sources, k) for k in names}
        for k in names:
            setattr(sources, k, sb.d / "none")
        try:
            warnings = []
            table = timeuse.default_sources(T0 - 1, T0 + 1, timeuse.config(), warnings)
        finally:
            for k, v in saved.items():
                setattr(sources, k, v)
        return [name for name, _ in table], warnings

    def test_a_source_that_is_off_is_left_out_silently_and_each_comes_on_with_its_setting(self):
        local = ["prompts", "board messages", "emails to agents"]
        with Sandbox() as sb:
            self.assertEqual(self.default_table(sb), (local, []))      # the kit's defaults: only the local three
            creds = sb.d / "login.json"
            creds.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
            saved = {k: os.environ.get(k) for k in ("AGENT_BOARD_GOOGLE_TOKEN", "AGENT_BOARD_GMAIL_CREDS")}
            try:
                os.environ["AGENT_BOARD_GOOGLE_TOKEN"] = str(sb.d / "missing.json")
                self.assertEqual(self.default_table(sb), (local, []))  # named, but no file there: still off
                timeuse.CONFIG.write_text(json.dumps({"tasks_repo": "example/tasks"}))
                screen.PATH.write_text("[]")                               # the Mac app has uploaded
                os.environ["AGENT_BOARD_GOOGLE_TOKEN"] = os.environ["AGENT_BOARD_GMAIL_CREDS"] = str(creds)
                self.assertEqual(self.default_table(sb), (local + ["to-dos", "screen time", "calendar", "Google Docs",
                                                                   "your email"], []))
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_the_tasks_repo_comes_from_the_board_or_the_to_do_config(self):
        with Sandbox() as sb:
            self.assertIsNone(timeuse.config()["tasks_repo"])
            todo = sb.d / "todo"
            todo.mkdir()
            (todo / "config.json").write_text(json.dumps({"repo": "example/todo"}))
            real = timeuse.configured_tasks_repo
            timeuse.configured_tasks_repo = lambda: real(todo_config=todo / "config.json")
            try:
                self.assertEqual(timeuse.config()["tasks_repo"], "example/todo")
                timeuse.CONFIG.write_text(json.dumps({"tasks_repo": "example/other"}))   # time.json's own wins
                self.assertEqual(timeuse.config()["tasks_repo"], "example/other")
            finally:
                timeuse.configured_tasks_repo = real


class TimeCommand(unittest.TestCase):
    def test_exit_status_says_whether_a_source_failed_and_names_waiting_ones(self):
        import contextlib
        import importlib.machinery
        import importlib.util
        import io
        loader = importlib.machinery.SourceFileLoader("agent_board_cli", str(Path(__file__).resolve().parent.parent / "bin" / "agent-board"))
        cli = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(cli)
        doc = {"daily": [{"day": "2026-10-30", "rows": [{"group": "Work", "minutes": 90.0}]}], "warnings": [], "waiting": [],
               "summary": {"activities": {"prompt": 3}, "guessed": {"asked": 0, "batches": 0}, "approximate documents": 0}}
        real, real_config = cli.timeuse.refresh, cli.timeuse.CONFIG
        cli.timeuse.CONFIG = Path(tempfile.mkdtemp()) / "none.json"          # never this machine's own config
        try:                     # refresh reads this machine's logs and services; the command's own job is the report
            for warnings, waiting, code in (([], [], 0), (["calendar: Google API answered HTTP 401"], [], 1),
                                            ([], ["screen time: nothing uploaded yet from the Mac app"], 0)):
                cli.timeuse.refresh = lambda cfg: dict(doc, warnings=warnings, waiting=waiting)
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(cli.cmd_time(type("A", (), {"json": False})()), code)
                self.assertIn("Work 1.5 h", out.getvalue())
                self.assertEqual("calendar" in err.getvalue(), bool(warnings))
                self.assertEqual("waiting: screen time" in err.getvalue(), bool(waiting))
        finally:
            cli.timeuse.refresh, cli.timeuse.CONFIG = real, real_config


if __name__ == "__main__":
    unittest.main()
