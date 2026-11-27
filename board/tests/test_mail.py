"""The Mail tab on the board (agentboard/mailbox.py, its actions and the collector's use of it):
python3 -m unittest discover -s tests

agent-mail is the mailroom's command, another component with its own tests of the mailbox; here it would read and
change the owner's real mailbox and deliver the owner's replies to real agents, so each test answers for it with a
stand-in that records the argv and stdin it was given, and the mailroom checkout the board finds (AGENT_MAILROOM_DIR)
is a temporary folder.
"""
import base64
import email
import email.policy
import functools
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from email.message import EmailMessage
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fixtures  # noqa: E402
import stand_ins  # noqa: E402
from agentboard import (actions, colours, gmailstate, groups, mailbox, maildrafts, mailforward, sent,  # noqa: E402
                        sources)

THREAD = "18f2a9c0d1e2f3a4"
KEY = "Zt0pZt0pZt0pZt0pZt0pZt"
NOW = 1_800_000_000.0
LISTED = {"threads": [{"id": THREAD, "subject": "Weekly results", "agents": ["alpha (claude)"], "snippet": "done",
                       "last": NOW - 60, "count": 2, "unread": True, "box": "inbox", "snooze_until": None,
                       "newest": 7, "attachments": False}],
          "counts": {"inbox": 1, "unread": 1, "snoozed": 0}}


AgentMail = stand_ins.Command          # agent-mail


def answer(obj):
    return 0, json.dumps(obj), ""


class MailTest(unittest.TestCase):
    """Each test runs with the board's records in a temporary folder, with no layout there yet, and a mailroom
    install there that keeps a mailbox (its agentmail/mailbox.py), unless the test removes it from `self.root`."""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.real = (sent.PATH, sent.MARKS, actions.AUDIT, sources.find_bin, groups.GROUPS, maildrafts.ROOT,
                     maildrafts.CONFIG)
        sent.PATH, sent.MARKS, actions.AUDIT = d / "sent.jsonl", d / "sent-marks.jsonl", d / "actions.jsonl"
        maildrafts.ROOT, maildrafts.CONFIG = d / "mail-drafts", d / "drafts.json"     # the drafts a reply clears
        groups.GROUPS = d / "groups.json"
        self.root = d / "mailroom"
        (self.root / "bin").mkdir(parents=True)
        (self.root / "agentmail").mkdir()
        (self.root / "bin" / "agent-spawn").write_text("")
        (self.root / "agentmail" / "mailbox.py").write_text("")
        self.exe = str(self.root / "bin" / "agent-mail")
        sources.find_bin = lambda name: str(self.root / "bin" / name)
        mock.patch.dict(os.environ, {"AGENT_MAILROOM_DIR": str(self.root)}).start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        (sent.PATH, sent.MARKS, actions.AUDIT, sources.find_bin, groups.GROUPS, maildrafts.ROOT,
         maildrafts.CONFIG) = self.real

    def act(self, req, run):
        return actions.dispatch(req, lambda: [], lambda: [], run=run)

    def audited(self):
        return [json.loads(ln) for ln in actions.AUDIT.read_text().splitlines()] if actions.AUDIT.exists() else []


class ActionTest(MailTest):
    def test_each_action_runs_agent_mail_mailbox_and_passes_its_answer_on(self):
        run = AgentMail(answer(LISTED))
        res = self.act({"action": "mail_list", "box": "all", "query": "  run results ", "limit": 50}, run)
        self.assertEqual(res, dict(LISTED, ok=True))
        self.assertEqual(run.calls, [([self.exe, "mailbox", "list", "--box", "all", "--limit", "50",
                                       "--query", "run results"], None)])
        shown = {"thread": LISTED["threads"][0], "messages": [], "document": "<p>hi</p>"}
        run = AgentMail(answer(LISTED), answer(shown))
        self.assertEqual(self.act({"action": "mail_list"}, run)["threads"][0]["id"], THREAD)   # inbox, 100, no search
        self.assertEqual(run.calls[0][0], [self.exe, "mailbox", "list", "--box", "inbox", "--limit", "100"])
        self.assertEqual(self.act({"action": "mail_show", "thread": THREAD}, run)["document"], "<p>hi</p>")
        self.assertEqual(run.calls[1][0], [self.exe, "mailbox", "show", "--", THREAD])
        run = AgentMail(answer({"name": "r.csv", "type": "text/csv", "data": "YSxi"}), answer({"name": "p.png",
                                                                                              "type": "image/png", "data": "eA=="}))
        self.assertEqual(self.act({"action": "mail_part", "key": "s7", "index": 2}, run)["data"], "YSxi")
        self.assertEqual(run.calls[0][0], [self.exe, "mailbox", "part", "--", "s7", "2"])
        self.assertEqual(self.act({"action": "mail_part", "key": "i31", "index": 1}, run)["name"], "p.png")  # the owner's own
        self.assertEqual(run.calls[1][0], [self.exe, "mailbox", "part", "--", "i31", "1"])
        self.assertEqual(self.audited(), [])                                   # reading is not on the record

    def test_a_list_asked_with_sessions_names_each_thread_s_agents_from_the_store(self):
        other = dict(LISTED["threads"][0], id="18f2a9c0d1e2f3a5", agents=["mailroom"])
        listed = dict(LISTED, threads=LISTED["threads"] + [other])
        d = Path(tempfile.mkdtemp())
        con = fixtures.mailroom_db(d / "mailroom.sqlite")       # the mailroom's store, as the collector reads it
        con.executemany("insert into sessions values(?,?,?,?,?,?,?)", [
            ("tagf", "claude", "s1", "fab", "/x", 0, "desktop"),
            ("tagb", "codex", "s2", "beta", "/y", 0, "desktop"),
            ("tagm", "claude", "s3", "mac", "/z", 0, "mac")])
        con.executemany("insert into sent(tag, thread_id, to_addr, subject, sent_at) values(?,?,?,?,?)", [
            ("tagf", THREAD, "owner@example.com", "Run results", NOW - 120),
            ("tagb", THREAD, "owner@example.com", "Re: Run results", NOW - 90),
            ("tagm", THREAD, "owner@example.com", "Re: Run results", NOW - 60),
            ("tagf", "another-thread", "owner@example.com", "Other", NOW - 30)])
        con.commit()
        con.close()

        def listing(req, store):
            return actions.dispatch(req, lambda: [], lambda: [], run=AgentMail(answer(listed)), mail_store=store)

        res = listing({"action": "mail_list", "sessions": True},
                      sources.MailStore(d / "mailroom.sqlite", host_id="desktop"))
        self.assertTrue(res["ok"], res)
        self.assertEqual([t["sessions"] for t in res["threads"]],
                         [[{"cli": "claude", "id": "s1"}, {"cli": "codex", "id": "s2"}],   # the Mac's session left out
                          []])                                                            # unrouted mail: none
        self.assertEqual(res["threads"][0]["agents"], ["alpha (claude)"])                 # the rest as it came
        self.assertNotIn("sessions_error", res)
        # not asked for: the answer as it came, the store not read (a missing one would have said so)
        self.assertEqual(listing({"action": "mail_list"}, sources.MailStore(d / "none.sqlite", host_id="desktop")),
                         dict(listed, ok=True))
        res = listing({"action": "mail_list", "sessions": True}, sources.MailStore(d / "none.sqlite", host_id="desktop"))
        missing = "FileNotFoundError: no database at %s" % (d / "none.sqlite")
        self.assertEqual(res, dict(listed, ok=True, sessions_error=missing, groups_error=missing))
        (d / "broken.sqlite").write_text("not a database")
        res = listing({"action": "mail_list", "sessions": True}, sources.MailStore(d / "broken.sqlite", host_id="desktop"))
        self.assertEqual(res, dict(listed, ok=True, sessions_error="DatabaseError: file is not a database",
                                   groups_error="DatabaseError: file is not a database"))

    def test_a_list_asked_with_sessions_gives_each_thread_its_newest_agent_s_group_and_colours(self):
        p, s, r, w, g, n, m = ("%08d-0000-4000-8000-000000000000" % i for i in range(1, 8))   # session ids
        own = Path(tempfile.mkdtemp()) / "colours.json"       # the owner's own colours.json, naming four groups
        own.write_text(json.dumps({"groups": {"Work": "#0E8A84", "Side": "#3E9B4F", "Admin": "#6A5ACD",
                                              "Personal": "#CF4F7E"}, "reserve": ["#2E5C8A", "#A85551"],
                                   "ungrouped": "#8C877F", "shades": {"lightness": 0.10, "chroma": 0.92}}))
        saved = colours.OWN
        colours.OWN = own
        self.addCleanup(setattr, colours, "OWN", saved)
        sources.write_json(groups.GROUPS, {"updated": NOW, "groups": [
            {"name": "Work", "members": [], "subgroups": [
                {"name": "Parser", "members": [p]}, {"name": "Release", "members": []},
                {"name": "Docs", "members": []}]},
            {"name": "Side", "members": [], "subgroups": [{"name": "Evals", "members": [s]},
                                                                {"name": "Reading", "members": []}]},
            {"name": "Research", "members": [r], "subgroups": []},          # a group colours.json does not name
            {"name": "Admin", "members": [], "subgroups": []}, {"name": "Personal", "members": [], "subgroups": []}]})
        sources.write_json(groups.history_path(), {
            p: {"group": "Work", "subgroup": "Parser"}, s: {"group": "Side", "subgroup": "Evals"},
            r: {"group": "Research", "subgroup": None},
            w: {"group": "Work", "subgroup": "Old"},              # a closed tile whose sub-group has gone since
            g: {"group": "Gone", "subgroup": None},               # a closed tile whose group has gone
            m: {"group": "Admin", "subgroup": None}})             # a Mac session's id: never a tile on this machine
        d = Path(tempfile.mkdtemp())
        con = fixtures.mailroom_db(d / "mailroom.sqlite")
        con.executemany("insert into sessions values(?,?,?,?,?,?,?)", [
            ("tag" + k, "claude", sid, k, "/x", 0, "mac" if sid == m else "desktop")
            for k, sid in zip("psrwgnm", (p, s, r, w, g, n, m))])
        sent_rows = [(1, "p", "t1"), (2, "s", "t2"), (3, "r", "t3"), (4, "w", "t4"), (5, "g", "t5"), (6, "n", "t6"),
                     (7, "p", "t7"), (8, "m", "t7"), (9, "s", "t9"), (10, "p", "t9")]
        con.executemany("insert into sent(id, tag, thread_id, to_addr, subject, sent_at) values(?,?,?,?,?,?)",
                        [(i, "tag" + k, t, "owner@example.com", "x", NOW) for i, k, t in sent_rows])
        con.commit()
        con.close()
        newest = {"t1": 1, "t2": 2, "t3": 3, "t4": 4, "t5": 5, "t6": 6, "t7": 8, "t8": 0, "t9": 10}   # t8: the owner's own only
        listed = dict(LISTED, threads=[dict(LISTED["threads"][0], id=t, newest=i) for t, i in newest.items()])
        store = sources.MailStore(d / "mailroom.sqlite", host_id="desktop")
        res = actions.dispatch({"action": "mail_list", "sessions": True}, lambda: [], lambda: [],
                               run=AgentMail(answer(listed)), mail_store=store)
        self.assertTrue(res["ok"], res)
        self.assertNotIn("groups_error", res)
        got = {t["id"]: (t["group"], t["subgroup"], t["colour"], t["shade"]) for t in res["threads"]}
        ungrouped = (None, None, "#8C877F", "#8C877F")
        self.assertEqual(got, {
            "t1": ("Work", "Parser", "#0E8A84", "#006A65"),   # its sub-group's shade, the darkest
            "t2": ("Side", "Evals", "#3E9B4F", "#257B37"),
            "t3": ("Research", None, "#2E5C8A", "#2E5C8A"),                   # the reserve's first colour
            "t4": ("Work", None, "#0E8A84", "#0E8A84"),                       # the group's colour, no sub-group now
            "t5": ungrouped, "t6": ungrouped,                                 # a group gone; never grouped
            "t7": ungrouped,                                                  # its newest from the Mac's session
            "t8": ungrouped,                                                  # no agent mail
            "t9": ("Work", "Parser", "#0E8A84", "#006A65")})  # the newest agent's, not the first's
        self.assertEqual(res["threads"][0]["sessions"], [{"cli": "claude", "id": p}])   # the sessions still there
        # a layout that cannot be read: the sessions, and why there are no colours
        groups.GROUPS.write_text("not a layout")
        res = actions.dispatch({"action": "mail_list", "sessions": True}, lambda: [], lambda: [],
                               run=AgentMail(answer(listed)), mail_store=store)
        self.assertEqual(res["groups_error"][:11], "ValueError:")
        self.assertNotIn("colour", res["threads"][0])
        self.assertEqual(res["threads"][0]["sessions"], [{"cli": "claude", "id": p}])

    def test_a_read_mark_is_not_on_the_record_and_the_other_marks_are(self):
        run = AgentMail(answer({"ok": True, "threads": LISTED["threads"]}))
        res = self.act({"action": "mail_mark", "threads": [THREAD], "op": "read", "upto": 7}, run)
        self.assertTrue(res["ok"], res)
        until = time.time() + 3600
        self.assertTrue(self.act({"action": "mail_mark", "threads": [THREAD, "abc"], "op": "snooze", "upto": 7,
                                  "until": until}, run)["ok"])
        self.assertTrue(self.act({"action": "mail_mark", "threads": [THREAD], "op": "inbox"}, run)["ok"])
        self.assertEqual([c for c, _ in run.calls], [
            [self.exe, "mailbox", "mark", "--op", "read", "--upto", "7", "--", THREAD],
            [self.exe, "mailbox", "mark", "--op", "snooze", "--upto", "7", "--until", repr(float(until)), "--", THREAD,
             "abc"],
            [self.exe, "mailbox", "mark", "--op", "inbox", "--", THREAD]])
        self.assertEqual([(r["op"], r["threads"]) for r in self.audited()], [("snooze", [THREAD, "abc"]),
                                                                             ("inbox", [THREAD])])

    def test_fast_mode_is_switched_by_agent_mail_and_each_switch_is_on_the_record(self):
        fast = {"since": NOW, "until": NOW + 300, "minutes": 5, "agent": "fab", "effort": "low", "was": "xhigh",
                "note": "Effort low until fast mode ends, then xhigh again."}
        on, off = dict(LISTED["threads"][0], fast=fast), dict(LISTED["threads"][0], fast=None)
        run = AgentMail(answer({"ok": True, "thread": on}), answer({"ok": True, "thread": off}))
        self.assertEqual(self.act({"action": "mail_fast", "thread": THREAD, "on": True}, run), {"ok": True, "thread": on})
        self.assertEqual(self.act({"action": "mail_fast", "thread": THREAD, "on": False}, run), {"ok": True, "thread": off})
        self.assertEqual(run.calls, [([self.exe, "mailbox", "fast", "--", THREAD, "on"], None),
                                     ([self.exe, "mailbox", "fast", "--", THREAD, "off"], None)])
        self.assertEqual([{k: v for k, v in r.items() if k != "ts"} for r in self.audited()], [
            {"action": "mail_fast", "thread": THREAD, "on": True, "agent": "fab", "effort": "low", "until": NOW + 300},
            {"action": "mail_fast", "thread": THREAD, "on": False, "agent": None, "effort": None, "until": None}])

    def test_an_answer_with_no_thread_is_on_the_record_as_such(self):
        res = self.act({"action": "mail_fast", "thread": THREAD, "on": True}, AgentMail(answer({"ok": True})))
        self.assertEqual(res, {"ok": True})
        self.assertEqual([{k: v for k, v in r.items() if k != "ts"} for r in self.audited()], [
            {"action": "mail_fast", "thread": THREAD, "on": True, "agent": None, "effort": None, "until": None,
             "problem": "agent-mail's answer had no thread"}])

    def test_fast_mode_refused_by_agent_mail_says_why_and_is_not_on_the_record(self):
        why = "agent-mail: fast mode is for a thread one agent owns; this one is a group's"
        res = self.act({"action": "mail_fast", "thread": THREAD, "on": True}, AgentMail((1, "", why + "\n")))
        self.assertEqual(res, {"ok": False, "error": why})
        res = self.act({"action": "mail_fast", "thread": THREAD, "on": False},
                       AgentMail((4, "", "agent-mail: no thread %s in the mailbox\n" % THREAD)))
        self.assertEqual(res, {"ok": False, "error": "agent-mail: no thread %s in the mailbox" % THREAD})
        self.assertEqual(self.audited(), [])

    def test_researcher_switch_uses_the_mailroom_thread_and_records_confirmed_state(self):
        rows = [dict(LISTED["threads"][0], researcher={"on": on, "agent": "fab", "tag": "fab00001"})
                for on in (True, False)]
        run = AgentMail(*(answer({"ok": True, "thread": t}) for t in rows))
        for t in rows:
            on = t["researcher"]["on"]
            self.assertEqual(self.act({"action": "mail_researcher", "thread": THREAD, "on": on,
                                       "expected_tag": "fab00001"}, run),
                             {"ok": True, "thread": t})
        self.assertEqual(run.calls, [([self.exe, "mailbox", "researcher", "--expect-tag", "fab00001", "--", THREAD,
                                      value], None)
                                     for value in ("on", "off")])
        self.assertEqual([(r["action"], r["thread"], r["on"], r["agent"]) for r in self.audited()],
                         [("mail_researcher", THREAD, True, "fab"), ("mail_researcher", THREAD, False, "fab")])

    def test_researcher_refuses_invalid_input_and_requires_an_authoritative_answer(self):
        req = {"action": "mail_researcher", "thread": THREAD, "on": True, "expected_tag": "fab00001"}
        run = AgentMail()
        for invalid in (dict(req, on=1), dict(req, on="on"), dict(req, on=None), dict(req, thread="--help")):
            self.assertEqual(self.act(invalid, run),
                             {"ok": False, "error": "thread must be a thread id and on true or false"})
        for invalid in (None, "--help", True, "fab (claude)", "a b c d"):
            res = self.act(dict(req, expected_tag=invalid), run)
            self.assertFalse(res["ok"], res)
            self.assertIn("expected_tag", res["error"])
        self.assertFalse(self.act({k: v for k, v in req.items() if k != "expected_tag"}, run)["ok"])
        self.assertEqual(run.calls, [])
        row = dict(LISTED["threads"][0], researcher={"on": True, "agent": "fab", "tag": "fab00001"})
        for item in (None, dict(row, id="other"), dict(row, researcher=None),
                     dict(row, researcher={"on": 1, "agent": "fab", "tag": "fab00001"}),
                     dict(row, researcher={"on": False, "agent": "fab", "tag": "fab00001"}),
                     dict(row, researcher={"on": True, "agent": "fab", "tag": "fab00002"})):
            res = self.act(req, AgentMail(answer({"ok": True, "thread": item})))
            self.assertFalse(res["ok"], res)
            self.assertIn("confirm", res["error"])
        for failed in ((2, "", "unknown command researcher"), answer({"ok": False, "error": "route changed"})):
            res = self.act(req, AgentMail(failed))
            self.assertFalse(res["ok"], res)
            self.assertTrue("researcher" in res["error"] or "route changed" in res["error"], res)

    def test_researcher_crosses_the_command_process_boundary(self):
        row = dict(LISTED["threads"][0], researcher={"on": True, "agent": "fab", "tag": "fab00001"})
        # Use the board's real process runner with a stand-in executable for the live mailroom.
        command = Path(self.exe)
        command.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"assert sys.argv[1:] == ['mailbox', 'researcher', '--expect-tag', 'fab00001', '--', {THREAD!r}, 'on']\n"
            f"print({json.dumps({'ok': True, 'thread': row})!r})\n"
        )
        command.chmod(0o700)
        res = self.act({"action": "mail_researcher", "thread": THREAD, "on": True, "expected_tag": "fab00001"},
                       sources.run)
        self.assertEqual(res, {"ok": True, "thread": row})

    def test_a_reply_goes_on_stdin_is_kept_by_the_mailroom_and_not_on_the_sent_record(self):
        run = AgentMail(answer({"ok": True, "key": "i31", "status": "sending"}))
        text = "--help; rm -rf ~ $(x)\nsecond line"
        res = self.act({"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "  %s\n" % text}, run)
        self.assertEqual(res, {"ok": True, "key": "i31", "status": "sending",
                               "draft": {"cleared": False, "rev": None}})      # no draft held its key
        self.assertEqual(run.calls, [([self.exe, "mailbox", "answer", "--key", KEY, "--", THREAD], text)])
        self.assertFalse(sent.PATH.exists())          # the mailroom keeps it as the owner's mail; the Time page counts it once
        rec, = self.audited()
        self.assertEqual((rec["action"], rec["thread"], rec["key"], rec["chars"]), ("mail_reply", THREAD, KEY,
                                                                                    len(text) + 3))
        self.assertNotIn("text", rec)

    def test_a_reply_with_files_goes_as_json_and_its_record_names_them_without_their_data(self):
        run = AgentMail(answer({"ok": True, "key": "i32", "status": "sending"}),
                        answer({"ok": True, "key": "i33", "status": "sending"}))
        files = [{"name": "plot.png", "data": base64.b64encode(b"\x89PNG...").decode()},
                 {"name": "notes.txt", "data": base64.b64encode(b"two\n").decode()}]
        res = self.act({"action": "mail_reply", "thread": THREAD, "key": KEY, "text": " see these \n", "files": files}, run)
        self.assertEqual(res, {"ok": True, "key": "i32", "status": "sending", "draft": {"cleared": False, "rev": None}})
        argv, stdin = run.calls[0]
        self.assertEqual(argv, [self.exe, "mailbox", "answer", "--key", KEY, "--json", "--", THREAD])
        self.assertEqual(json.loads(stdin), {"text": "see these", "files": files})
        res = self.act({"action": "mail_reply", "thread": THREAD, "key": KEY[::-1], "files": files[:1]}, run)  # no text
        self.assertTrue(res["ok"], res)
        self.assertEqual(json.loads(run.calls[1][1]), {"text": "", "files": files[:1]})
        first, second = self.audited()
        self.assertEqual((first["chars"], first["files"]), (12, [{"name": "plot.png", "bytes": 7},
                                                                 {"name": "notes.txt", "bytes": 4}]))
        self.assertEqual((second["chars"], second["files"]), (0, [{"name": "plot.png", "bytes": 7}]))
        self.assertNotIn("data", json.dumps(first))

    def test_the_largest_reply_the_mac_may_send_fits_what_act_reads(self):
        """bin/agent-board's MAX_REQUEST, from mailbox's limits: the most files, base64, beside the longest text."""
        spec = importlib.util.spec_from_loader("agent_board_bin", SourceFileLoader(
            "agent_board_bin", str(Path(__file__).resolve().parent.parent / "bin" / "agent-board")))
        board = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(board)
        size = mailbox.MAX_FILE_BYTES // mailbox.MAX_FILES
        files = [{"name": "f%d" % i, "data": base64.b64encode(b"x" * size).decode()} for i in range(mailbox.MAX_FILES)]
        req = {"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "\u2603" * mailbox.MAX_REPLY, "files": files}
        wire = json.dumps(req, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(wire), board.MAX_REQUEST)
        self.assertTrue(self.act(json.loads(wire), AgentMail(answer({"ok": True, "key": "i1", "status": "sending"})))["ok"])

    def test_bad_requests_never_reach_agent_mail(self):
        run = AgentMail()
        future = time.time() + 3600
        for req in ({"action": "mail_list", "box": "spam"},
                    {"action": "mail_list", "limit": 0},
                    {"action": "mail_list", "limit": True},
                    {"action": "mail_list", "query": "x" * 201},
                    {"action": "mail_list", "query": 3},
                    {"action": "mail_list", "query": "a\x00b"},
                    {"action": "mail_list", "sessions": "yes"},
                    {"action": "mail_show", "thread": "--help"},
                    {"action": "mail_show", "thread": "a/b"},
                    {"action": "mail_show", "thread": THREAD + "\n"},                         # the whole id only
                    {"action": "mail_mark", "threads": [], "op": "read", "upto": 1},
                    {"action": "mail_mark", "threads": THREAD, "op": "read", "upto": 1},
                    {"action": "mail_mark", "threads": ["-x"], "op": "inbox"},
                    {"action": "mail_mark", "threads": [THREAD], "op": "delete"},
                    {"action": "mail_mark", "threads": [THREAD], "op": "read"},                  # no upto
                    {"action": "mail_mark", "threads": [THREAD], "op": "read", "upto": "7"},
                    {"action": "mail_mark", "threads": [THREAD], "op": "inbox", "upto": 7},      # upto not for inbox
                    {"action": "mail_mark", "threads": [THREAD], "op": "snooze", "upto": 7},     # no until
                    {"action": "mail_mark", "threads": [THREAD], "op": "snooze", "upto": 7, "until": NOW - 1e9},
                    {"action": "mail_mark", "threads": [THREAD], "op": "snooze", "upto": 7, "until": float("inf")},
                    {"action": "mail_mark", "threads": [THREAD], "op": "snooze", "upto": 7, "until": float("nan")},
                    {"action": "mail_mark", "threads": [THREAD], "op": "read", "upto": 7, "until": future},
                    {"action": "mail_mark", "threads": [THREAD] * 201, "op": "inbox"},
                    {"action": "mail_reply", "thread": THREAD, "key": "short", "text": "hi"},
                    {"action": "mail_reply", "thread": THREAD, "key": KEY + "/", "text": "hi"},
                    {"action": "mail_reply", "thread": THREAD, "key": KEY + "\n", "text": "hi"},
                    {"action": "mail_reply", "thread": THREAD, "key": "-" + KEY, "text": "hi"},  # an option to it
                    {"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "   "},
                    {"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "a\x00b"},
                    {"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "x" * 20001},
                    {"action": "mail_reply", "thread": None, "key": KEY, "text": "hi"},
                    {"action": "mail_reply", "thread": THREAD, "key": KEY, "files": []},          # nothing to send
                    {"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "hi", "files": {"name": "a"}},
                    {"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "hi", "files": [{"name": "a"}]},
                    {"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "hi",
                     "files": [{"name": "", "data": "eA=="}]},
                    {"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "hi",
                     "files": [{"name": "a", "data": "not base64!"}]},
                    {"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "hi",
                     "files": [{"name": "a", "data": "eA==", "path": "/etc/passwd"}]},
                    {"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "hi",
                     "files": [{"name": "a", "data": "eA=="}] * 21},
                    {"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "hi",
                     "files": [{"name": "big", "data": base64.b64encode(b"x" * (25 << 20 | 1)).decode()}]},
                    {"action": "mail_fast", "thread": THREAD},                                   # no on
                    {"action": "mail_fast", "thread": THREAD, "on": "on"},
                    {"action": "mail_fast", "thread": THREAD, "on": 1},
                    {"action": "mail_fast", "thread": "--help", "on": True},
                    {"action": "mail_fast", "thread": [THREAD], "on": True},
                    {"action": "mail_part", "key": "x3", "index": 1},
                    {"action": "mail_part", "key": "s3", "index": 0},
                    {"action": "mail_part", "key": "s3; x", "index": 1}):
            res = self.act(req, run)
            self.assertFalse(res["ok"], req)
            self.assertTrue(res["error"], req)
            if req["action"] == "mail_reply":
                self.assertIs(res.get("refused"), True, req)      # never kept: the Mac may let it be changed
        self.assertEqual(run.calls, [])
        self.assertEqual(self.audited(), [])

    def test_a_reply_agent_mail_did_not_answer_may_have_been_kept(self):
        """agent-mail timing out (or failing) after the mailroom kept the reply: the answer is not a refusal, so the Mac
        keeps the reply as it was, under its key, for Send to send once."""
        res = self.act({"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "Ship it."},
                       AgentMail((124, "", "timed out after 60s")))
        self.assertEqual(res, {"ok": False, "error": "timed out after 60s"})
        res = self.act({"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "Ship it."},
                       AgentMail((1, "", "agent-mail: refused: thread is not in the mailbox\n")))    # the words do not count
        self.assertEqual(res, {"ok": False, "error": "agent-mail: refused: thread is not in the mailbox"})

    def test_a_reply_agent_mail_refused_was_not_kept(self):
        """agent-mail's exit code for a reply it refused before recording anything (the mailroom's
        mailbox.ANSWER_REFUSED): the answer is a refusal, so the Mac lets the reply be changed."""
        res = self.act({"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "Ship it."},
                       AgentMail((mailbox.ANSWER_REFUSED, "", "agent-mail: thread %s is not in the mailbox\n" % THREAD)))
        self.assertEqual(res, {"ok": False, "error": "agent-mail: thread %s is not in the mailbox" % THREAD, "refused": True})
        self.assertEqual(self.audited(), [])

    def test_agent_mail_failing_or_missing_is_the_answer_s_error(self):
        res = self.act({"action": "mail_show", "thread": THREAD}, AgentMail((4, "", "agent-mail: no thread x\n")))
        self.assertEqual(res, {"ok": False, "error": "agent-mail: no thread x"})
        res = self.act({"action": "mail_show", "thread": THREAD}, AgentMail((0, "Traceback", "")))
        self.assertFalse(res["ok"])
        self.assertIn("not JSON", res["error"])
        sources.find_bin = lambda name: None
        res = self.act({"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "hi"}, AgentMail())
        self.assertEqual(res, {"ok": False, "error": "agent-mail is not installed"})
        self.assertEqual(self.audited(), [])                                  # nothing happened, nothing recorded


class ReceiveTest(unittest.TestCase):
    """agent-board receive: a file too big for a reply, kept in a temporary BIG_FILES."""

    def setUp(self):
        self.real = mailbox.BIG_FILES
        mailbox.BIG_FILES = Path(tempfile.mkdtemp()) / "mail-files"

    def tearDown(self):
        mailbox.BIG_FILES = self.real

    def send(self, name, data, size=None):
        head = json.dumps({"name": name, "size": len(data) if size is None else size}).encode() + b"\n"
        return mailbox.receive(io.BytesIO(head + data), NOW)

    def test_a_file_is_kept_under_its_day_with_a_safe_name_and_never_over_another(self):
        got = self.send("../film.mov", b"x" * 3000000)
        path = Path(got["path"])
        self.assertEqual((path.parent.parent, path.name, got["bytes"]), (mailbox.BIG_FILES, "_film.mov", 3000000))
        self.assertEqual(path.read_bytes(), b"x" * 3000000)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        again = Path(self.send("../film.mov", b"y")["path"])
        self.assertEqual((again.name, again.read_bytes(), path.read_bytes()[:1]), ("_film (2).mov", b"y", b"x"))
        self.assertEqual(Path(self.send(".", b"")["path"]).name, "file")

    def test_a_bad_header_too_big_a_file_or_a_short_one_leaves_nothing(self):
        for head in (b"not json\n", b'{"name": "a"}\n', b'{"name": "a", "size": true}\n', b'{"name": 3, "size": 1}\n',
                     json.dumps({"name": "a", "size": mailbox.MAX_BIG_FILE + 1}).encode() + b"\n"):
            with self.assertRaises(ValueError, msg=head):
                mailbox.receive(io.BytesIO(head + b"x"), NOW)
        with self.assertRaisesRegex(ValueError, "2 bytes short"):
            self.send("cut.bin", b"abc", size=5)

        class Failing(io.BytesIO):
            def read(self, n=-1):
                raise OSError("disk read error")
        with self.assertRaisesRegex(OSError, "disk read error"):
            mailbox.receive(Failing(b'{"name": "a", "size": 3}\nabc'), NOW)
        self.assertEqual([p.name for p in mailbox.BIG_FILES.rglob("*") if p.is_file()], [])

    def test_another_upload_s_partial_file_is_neither_used_nor_removed(self):
        day = mailbox.BIG_FILES / time.strftime("%Y-%m-%d", time.localtime(NOW))
        day.mkdir(parents=True)
        (day / "film.mov.part").write_bytes(b"half")
        self.assertEqual(Path(self.send("film.mov", b"whole")["path"]).name, "film (2).mov")
        self.assertEqual((day / "film.mov.part").read_bytes(), b"half")
        with mock.patch.object(Path, "exists", return_value=False):        # as if it appeared after the check
            with self.assertRaises(FileExistsError):
                self.send("film.mov", b"whole")
        self.assertEqual((day / "film.mov.part").read_bytes(), b"half")


BOX = {"g1": "<M1@mail.gmail.com>", "g2": "<m2@x>"}    # the owner's Gmail: Gmail id -> Message-ID


class SummaryTest(MailTest):
    def test_summary_normalises_the_read_list_and_checks_its_shape(self):
        run = AgentMail(answer({"unread": 2, "inbox": 5, "read": ["<A@mail.gmail.com>", "b@x", "b@x"]}))
        self.assertEqual(mailbox.summary(run), {"unread": 2, "inbox": 5, "read": ["a@mail.gmail.com", "b@x"]})
        self.assertEqual(run.calls[0][0], [self.exe, "mailbox", "summary"])
        with self.assertRaisesRegex(ValueError, "not {unread, inbox, read}"):
            mailbox.summary(AgentMail(answer({"unread": "2", "inbox": 5, "read": []})))

    def test_a_mailroom_without_a_mailbox_gives_no_summary(self):
        (self.root / "agentmail" / "mailbox.py").rename(self.root / "agentmail" / "other.py")
        run = AgentMail()
        self.assertIsNone(mailbox.summary(run))
        self.assertEqual(run.calls, [])


TAG = "abcd2345"
AGENTS = fixtures.AGENTS
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4         # an inline plot's bytes, made up
CSV = b"run,accuracy\nE6,0.91\n"
# An agent's mail as the mailroom sends it (its HTML from the agent's markdown, a widget's fallback, a plot inline and a
# file attached), answering in a new thread, so that it ends with the line naming the old one
HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family: sans-serif">
<p>E6 finished: <b>accuracy 0.91</b>, up from 0.87.</p>
<p><img src="cid:curve" alt="curve" style="max-width:100%;height:auto"></p>
<div data-agentmail-widget="0">
<p><b>Run E7 next?</b></p>
<ol><li><b>Yes</b> (recommended)</li><li><b>No</b></li></ol>
<p>If you don't answer: Yes</p>
<p>Reply with the number.</p>
</div>
<hr><p style="color:#6e7781;font-size:13px">Continues the thread &quot;E5 plan&quot;. <a href="https://mail.google.com/mail/?authuser=owner%40example.com#search/rfc822msgid%3Ax">Open the mail this answers in Gmail</a></p>
</body></html>
"""
FORWARDED_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family: sans-serif">
<p>E6 finished: <b>accuracy 0.91</b>, up from 0.87.</p>
<p><img src="cid:curve" alt="curve" style="max-width:100%;height:auto"></p>
<div>
<p><b>Run E7 next?</b></p>
<ol><li><b>Yes</b> (recommended)</li><li><b>No</b></li></ol>
<p>If you don't answer: Yes</p>

</div>
</body></html>
"""
TEXT = """E6 finished: **accuracy 0.91**, up from 0.87.

**Run E7 next?**

1. Yes (recommended)
2. No

If you don't answer: Yes
Reply with the number.

--
Continues the thread "E5 plan". The mail this answers: https://mail.google.com/mail/?authuser=owner%40example.com#x
"""
FORWARDED_TEXT = """E6 finished: **accuracy 0.91**, up from 0.87.

**Run E7 next?**

1. Yes (recommended)
2. No

If you don't answer: Yes
"""


def sent_mail(subject, text=TEXT, html=HTML, images=(("curve", PNG),), files=(("e6.csv", CSV),)):
    """A mail's bytes as the mailroom's outbound.compose writes an agent's (another repo, whose message store a test
    cannot share): from the agents' account in the agent's name, to the owner, answering the owner's mail, the reply tag in
    Reply-To and its own header; its text, and its HTML (None: none) with `images` related to it; `files` attached."""
    msg = EmailMessage()
    msg["From"] = "Agent (fab (claude)) <%s>" % AGENTS
    msg["To"] = fixtures.OWNER
    msg["Subject"] = subject
    msg["Reply-To"] = "Agent <agents+%s@example.com>" % TAG
    msg["X-Agent-Mailroom"] = TAG
    msg["In-Reply-To"] = msg["References"] = "<owner-1@mail.gmail.com>"
    msg.set_content(text)
    if html is not None:
        msg.add_alternative(html, subtype="html")
        for name, data in images:
            msg.get_payload()[1].add_related(data, maintype="image", subtype="png", cid="<%s>" % name,
                                             filename=name + ".png", disposition="inline")
    for name, data in files:
        msg.add_attachment(data, maintype="text", subtype="csv", filename=name)
    return msg.as_bytes()


def parsed(raw):
    return email.message_from_bytes(raw, policy=email.policy.default)


class ForwardTest(MailTest):
    def store(self, *mails):
        """A mailroom store in a temporary folder with a sent row for each (sent id, thread, subject, the mail's bytes
        or None), the bytes kept in its message store as the mailroom files them; a MailStore reading it."""
        d = Path(tempfile.mkdtemp())
        con = fixtures.mailroom_db(d / "mailroom.sqlite")
        for sid, thread, subject, raw in mails:
            sha = raw and hashlib.sha256(raw).hexdigest()
            if raw:
                (d / "messages" / sha[:2]).mkdir(parents=True, exist_ok=True)
                (d / "messages" / sha[:2] / (sha + ".eml")).write_bytes(raw)
            con.execute("insert into sent(id, tag, thread_id, to_addr, subject, sent_at, eml) values(?,?,?,?,?,?,?)",
                        (sid, TAG, thread, fixtures.OWNER, subject, int(NOW), sha))
        con.commit()
        con.close()
        return sources.MailStore(d / "mailroom.sqlite", host_id="desktop", messages=d / "messages")

    def forward(self, req, store, gmail):
        return actions.dispatch(dict(req, action="mail_forward"), lambda: [], lambda: [], gmail=gmail, mail_store=store)

    @staticmethod
    def uploads(calls):
        """The drafts uploaded: (url, headers, bytes) each."""
        return [(url, headers, sent_bytes) for method, url, headers, sent_bytes in calls if "/upload/" in url]

    def test_a_forward_is_a_fresh_mail_of_that_message_with_its_images_and_files(self):
        subject = "Re: Run results [#%s]" % TAG
        store = self.store((7, THREAD, subject, sent_mail(subject)), (8, THREAD, subject, sent_mail(subject, "later")))
        gmail, calls = fixtures.gmail_of(fixtures.OWNER, BOX, unread=set())
        res = self.forward({"thread": THREAD, "key": "s7"}, store, gmail)
        self.assertEqual(res, {"ok": True, "draft": "r-1", "message": "d1"})
        (url, headers, raw), = self.uploads(calls)
        self.assertEqual(url, "https://gmail.googleapis.com/upload/gmail/v1/users/me/drafts?uploadType=media")
        self.assertEqual(headers, {"Authorization": "Bearer AT-secret", "Content-Type": "message/rfc822"})
        self.assertTrue(all("AT-secret" not in u for _, u, _, _ in calls))
        msg = parsed(raw)
        self.assertEqual(list(msg.keys()), ["Subject", "MIME-Version", "Content-Type"])   # no sender, recipient, reply
        self.assertEqual(msg["Subject"], "Run results")
        self.assertEqual([p.get_content_type() for p in msg.walk()],
                         ["multipart/mixed", "multipart/alternative", "text/plain", "multipart/related", "text/html",
                          "image/png", "text/csv"])
        self.assertEqual(msg.get_body(("plain",)).get_content(), FORWARDED_TEXT)
        self.assertEqual(msg.get_body(("html",)).get_content(), FORWARDED_HTML)
        image, csv = [p for p in msg.walk() if not p.is_multipart()][2:]
        self.assertEqual((image["Content-ID"], image.get_content_disposition(), image.get_content()),
                         ("<curve>", "inline", PNG))
        self.assertEqual((csv.get_filename(), csv.get_content_disposition(), csv.get_content().encode()),
                         ("e6.csv", "attachment", CSV))
        rec, = self.audited()
        self.assertEqual({k: v for k, v in rec.items() if k != "ts"},
                         {"action": "mail_forward", "thread": THREAD, "key": "s7", "draft": "r-1", "message": "d1",
                          "bytes": len(raw), "images": 1, "files": ["e6.csv"]})

    def test_the_subject_loses_its_reply_tag_and_re_as_the_tab_heads_the_thread(self):
        self.assertEqual(mailforward.subject("Re: Run results [#%s]" % TAG), "Run results")
        self.assertEqual(mailforward.subject("RE: re:Fwd: Run [#%s] results " % TAG), "Run results")
        self.assertEqual(mailforward.subject("Run results"), "Run results")
        self.assertEqual(mailforward.subject("Are: the [#notatag!] stays"), "Are: the [#notatag!] stays")
        self.assertEqual(mailforward.subject("[mailroom] Re: held mail"), "[mailroom] Re: held mail")

    def test_the_review_s_note_on_a_mail_sent_unchanged_is_left_out(self):
        note = ("[Sent unchanged by the mailroom: the pre-send review asked the agent for changes and it did not revise "
                "or withdraw this mail within 30 minutes.]")
        own = sent_mail("E6", text=note + "\n\nE6 is done.\n", images=(), files=(),
                        html='<html><body style="margin:0"><p style="color:#6e7781;font-size:13px">%s</p>\n'
                             '<p>E6 is done.</p></body></html>\n' % note)
        rendered = sent_mail("E6", text=note + "\n\nE6 is done.\n", images=(), files=(),
                             html='<body style="margin:0">\n<p>%s</p>\n<p>E6 is done.</p>\n</body>\n' % note)
        for raw, html in ((own, '<html><body style="margin:0"><p>E6 is done.</p></body></html>\n'),
                          (rendered, '<body style="margin:0">\n<p>E6 is done.</p>\n</body>\n')):
            msg = parsed(mailforward.draft(raw, "E6").raw)
            self.assertEqual(msg.get_body(("plain",)).get_content(), "E6 is done.\n")
            self.assertEqual(msg.get_body(("html",)).get_content(), html)

    def test_a_mail_with_only_text_or_only_html_keeps_that_alone(self):
        old = EmailMessage()
        old["Subject"] = "E6"
        old.set_content("E6 is done.\n")
        old.add_attachment(CSV, maintype="text", subtype="csv", filename="e6.csv")
        msg = parsed(mailforward.draft(old.as_bytes(), "E6").raw)
        self.assertEqual([p.get_content_type() for p in msg.walk()], ["multipart/mixed", "text/plain", "text/csv"])
        old = EmailMessage()
        old.set_content("<p>E6 <img src=\"cid:curve\"></p>\n", subtype="html")
        old.add_related(PNG, maintype="image", subtype="png", cid="<curve>")
        fwd = mailforward.draft(old.as_bytes(), "E6")
        self.assertEqual([p.get_content_type() for p in parsed(fwd.raw).walk()],
                         ["multipart/related", "text/html", "image/png"])
        self.assertEqual((fwd.images, fwd.files), (1, []))
        old = EmailMessage()
        old.add_attachment(CSV, maintype="text", subtype="csv", filename="e6.csv")
        with self.assertRaisesRegex(ValueError, "neither a text nor an HTML body"):
            mailforward.draft(old.as_bytes(), "E6")

    def test_a_draft_over_gmail_s_limit_is_refused_before_anything_is_sent(self):
        gmail, calls = fixtures.gmail_of(fixtures.OWNER, BOX, unread=set())
        with self.assertRaisesRegex(ValueError, r"^the draft would be 35\.0 MB, more than the 35 MB Gmail takes in "
                                                r"one, so none was made$"):
            gmail.create_draft(b"x" * (gmailstate.MAX_DRAFT_BYTES + 1))
        self.assertEqual(calls, [])
        self.assertEqual(gmail.create_draft(b"x" * gmailstate.MAX_DRAFT_BYTES)["message"]["id"], "d1")

    def test_what_cannot_be_forwarded_says_why_and_makes_no_draft(self):
        subject = "Re: Run results [#%s]" % TAG
        raw = sent_mail(subject)
        store = self.store((7, THREAD, subject, raw), (9, "another-thread", subject, raw), (10, THREAD, subject, None))
        gmail, calls = fixtures.gmail_of(fixtures.OWNER, BOX, unread=set())
        for req, error in (
                ({"thread": THREAD, "key": "i5"}, "thread must be a thread id and key an agent's mail (s<id>)"),
                ({"thread": "-x", "key": "s7"}, "thread must be a thread id and key an agent's mail (s<id>)"),
                ({"thread": THREAD, "key": "s9"}, "agent mail s9 is not in thread %s" % THREAD),
                ({"thread": THREAD, "key": "s11"}, "the mailroom has no agent mail s11"),
                ({"thread": THREAD, "key": "s10"}, "the mailroom kept no copy of agent mail s10: it was sent before the "
                                                   "mailroom kept copies, and not copied from Gmail since")):
            self.assertEqual(self.forward(req, store, gmail), {"ok": False, "error": error})
        sha = hashlib.sha256(raw).hexdigest()
        (store.messages / sha[:2] / (sha + ".eml")).write_bytes(raw + b"x")
        self.assertEqual(self.forward({"thread": THREAD, "key": "s7"}, store, gmail),
                         {"ok": False, "error": "the mailroom's copy of agent mail s7 is damaged: it does not hash to "
                                                "its name"})
        with sqlite3.connect(store.path) as con:                   # a copy named by something not a sha256
            con.execute("update sent set eml = ? where id = 7", ("../../config.json",))
        self.assertEqual(self.forward({"thread": THREAD, "key": "s7"}, store, gmail),
                         {"ok": False, "error": "agent mail s7 names its copy '../../config.json', which is not a "
                                                "sha256"})
        res = self.forward({"thread": THREAD, "key": "s7"}, sources.MailStore(store.path.parent / "none.sqlite"), gmail)
        self.assertEqual(res, {"ok": False, "error": "no database at %s" % (store.path.parent / "none.sqlite")})
        self.assertEqual((self.uploads(calls), self.audited()), ([], []))

    def test_a_draft_gmail_names_no_message_of_is_said_and_on_the_record(self):
        subject = "Re: Run results [#%s]" % TAG
        store = self.store((7, THREAD, subject, sent_mail(subject)))
        gmail, calls = fixtures.gmail_of(fixtures.OWNER, BOX, unread=set(), draft={"id": "r-9"})
        self.assertEqual(self.forward({"thread": THREAD, "key": "s7"}, store, gmail),
                         {"ok": False, "error": "Gmail made the draft but named no message to open; it is in Gmail's "
                                                "Drafts"})
        (_, _, raw), = self.uploads(calls)
        rec, = self.audited()
        self.assertEqual({k: v for k, v in rec.items() if k != "ts"},
                         {"action": "mail_forward", "thread": THREAD, "key": "s7", "draft": "r-9", "message": None,
                          "bytes": len(raw), "images": 1, "files": ["e6.csv"]})

    def test_no_gmail_another_s_gmail_or_gmail_failing_makes_no_draft_and_no_record(self):
        subject = "Re: Run results [#%s]" % TAG
        store = self.store((7, THREAD, subject, sent_mail(subject)))
        no_login = {"ok": False, "error": "the tab cannot make a Gmail draft: the board config names no Gmail login of "
                                          "the owner's (gmail_credentials), or the mailroom's config names no agents' "
                                          "account"}
        res = actions.dispatch({"action": "mail_forward", "thread": THREAD, "key": "s7"}, lambda: [], lambda: [],
                               mail_store=store)                                     # off: the kit's default
        self.assertEqual(res, no_login)
        with mock.patch.dict(os.environ, {"AGENT_BOARD_GMAIL_CREDS": str(self.root / "no-login.json")}):
            res = actions.dispatch({"action": "mail_forward", "thread": THREAD, "key": "s7"}, lambda: [], lambda: [],
                                   mail_store=store)                                 # named, but missing
        self.assertEqual(res, no_login)
        gmail, calls = fixtures.gmail_of(AGENTS, BOX, unread=set())
        res = self.forward({"thread": THREAD, "key": "s7"}, store, gmail)
        self.assertEqual(res, {"ok": False, "error": "the Gmail login there is not the owner's (it is %s); not used" % AGENTS})
        self.assertEqual(self.uploads(calls), [])
        gmail, calls = fixtures.gmail_of(fixtures.OWNER, BOX, unread=set(), fail="/drafts")
        res = self.forward({"thread": THREAD, "key": "s7"}, store, gmail)
        self.assertEqual(res, {"ok": False, "error": "Google API answered HTTP 503"})
        self.assertEqual(self.audited(), [])


class CollectorTest(MailTest):
    SID = "11111111-2222-3333-4444-555555555555"

    def board(self, d, read, mid, **kw):
        """A real collector on a status log and mailroom store in `d`: one agent whose latest mail to the owner
        (Message-ID `mid`) is unanswered, and a mailbox whose summary counts the Message-IDs in `read` as read in the
        tab. kw: the collector's other arguments (gmail, sent)."""
        (d / "events.jsonl").write_text(json.dumps({"ts": NOW - 4000, "event": "start", "session_id": self.SID,
                                                    "cli": "claude", "name": "alpha", "cwd": "/x"}) + "\n")
        con = fixtures.mailroom_db(d / "mailroom.sqlite")
        con.execute("insert into sessions(tag, cli, session_id, session_name, origin_host) values(?,?,?,?,?)",
                    ("abcd1234", "claude", self.SID, "alpha", "desktop"))
        con.execute("insert into sent(tag, thread_id, message_id, to_addr, subject, sent_at) values(?,?,?,?,?,?)",
                    ("abcd1234", "t1", mid, fixtures.OWNER, "Fixed", int(NOW - 290)))
        con.commit()
        con.close()
        run = AgentMail(answer({"unread": 1, "inbox": 4, "read": read}))
        box = sources.Periodic(functools.partial(mailbox.summary, run=run), mailbox.REFRESH, None, sync=True)
        return fixtures.collector(d, claude=lambda: [fixtures.session(self.SID, "alpha", NOW - 3000)], mailbox=box, **kw)

    def test_mail_read_in_the_tab_turns_the_tile_from_waiting_and_the_snapshot_carries_the_counts(self):
        with tempfile.TemporaryDirectory() as d:
            snap = self.board(Path(d), ["<M1@x>"], "<m1@x>").snapshot(NOW)
            a, = snap["agents"]
            self.assertTrue(a["mail"]["read"])
            self.assertNotEqual(a["state"], "waiting")
            self.assertIn("read its mail", a["reason"])
            self.assertEqual((snap["mailbox"], snap["problems"]), ({"unread": 1, "inbox": 4}, []))
        with tempfile.TemporaryDirectory() as d:
            a, = self.board(Path(d), ["<other@x>"], "<m1@x>").snapshot(NOW)["agents"]
            self.assertIsNone(a["mail"]["read"])                 # no Gmail on this board: not known, still waiting
            self.assertEqual(a["state"], "waiting")

    def test_the_tab_counting_a_mail_read_wins_over_gmail_and_gmail_answers_the_rest(self):
        gmail, _ = fixtures.gmail_of(fixtures.OWNER, BOX, unread={"g1", "g2"})
        with tempfile.TemporaryDirectory() as d:
            col = self.board(Path(d), ["<M1@mail.gmail.com>"], "<m1@x>", gmail=gmail)
            col.agents(NOW)                                     # reads the mailbox's summary
            self.assertIsNone(col.is_read("<m2@x>"))            # Gmail not looked at yet: unknown, and now asked
            gmail.is_read("<M1@mail.gmail.com>")                # asked too, so Gmail's answer for it would be known
            gmail.maybe_refresh(force=True)                     # sync: Gmail's state, read in this thread
            self.assertIs(gmail.is_read("<M1@mail.gmail.com>"), False)
            self.assertIs(col.is_read("<m1@MAIL.gmail.com>"), True)    # unread in Gmail, read in the tab
            self.assertIs(col.is_read("<m2@x>"), False)                # not read in the tab: Gmail's answer

    def test_mail_kept_for_the_tab_alone_is_unread_until_the_tab_reads_it_and_gmail_is_never_searched_for_it(self):
        kept = "<177.4.5@agent-mailroom.local>"                 # the mailroom kept it: Gmail never had it
        gmail, calls = fixtures.gmail_of(fixtures.OWNER, BOX, unread=set())
        with tempfile.TemporaryDirectory() as d:
            col = self.board(Path(d), ["<m2@x>"], kept, gmail=gmail)
            for _ in range(2):                                  # the snapshot asks, Gmail checks, and again
                a, = col.snapshot(NOW)["agents"]
                gmail.maybe_refresh(force=True)
            self.assertTrue(gmail.status["ok"], gmail.status)
            self.assertEqual([u for _, u, _, _ in calls if "rfc822msgid" in u], [])
            self.assertEqual((a["mail"]["read"], a["mail"]["last_out_kept"], a["mail"]["gmail_thread"]),
                             (False, True, None))              # unread in the tab, not "no reply yet"
            self.assertEqual(a["state"], "waiting")
        with tempfile.TemporaryDirectory() as d:
            a, = self.board(Path(d), [kept.upper()], kept, gmail=gmail).snapshot(NOW)["agents"]
            self.assertIs(a["mail"]["read"], True)                  # read in the tab: the tile stops waiting
            self.assertIn("read its mail", a["reason"])
        with tempfile.TemporaryDirectory() as d:
            col = self.board(Path(d), [], kept)
            col.mailbox.load = functools.partial(mailbox.summary, run=AgentMail((1, "", "agent-mail: locked")))
            a, = col.snapshot(NOW)["agents"]
            self.assertIsNone(a["mail"]["read"])                    # no summary yet: the tab cannot say
        with tempfile.TemporaryDirectory() as d:
            a, = self.board(Path(d), [], "<m1@x>").snapshot(NOW)["agents"]
            self.assertIs(a["mail"]["last_out_kept"], False)       # Gmail's: its link stays

    def test_a_done_message_is_ticked_off_once_its_email_is_read_in_the_tab_alone(self):
        sent.record("send", "alpha", "fix it", True, ts=NOW - 600, ref="hhhh", id=self.SID)
        sent.MARKS.write_text(json.dumps({"ts": NOW - 300, "ref": "hhhh", "event": "done", "note": "fixed"}) + "\n")
        with tempfile.TemporaryDirectory() as d:
            col = self.board(Path(d), ["<m1@x>"], "<m1@x>", sent=sent)     # no Gmail on this board
            col.snapshot(NOW)
            self.assertEqual(col.tick_seen(NOW), ["hhhh"])
            self.assertEqual(sent.messages()[0]["tick"]["note"], 'you read its email "Fixed"')

    def test_a_failing_summary_is_a_problem_and_the_board_carries_on(self):
        with tempfile.TemporaryDirectory() as d:
            col = self.board(Path(d), [], "<m1@x>")
            col.mailbox.load = functools.partial(mailbox.summary, run=AgentMail((1, "", "agent-mail: locked")))
            snap = col.snapshot(NOW)
            self.assertNotIn("mailbox", snap)                  # no value yet: the Mac's tab says why there is none
            self.assertEqual(snap["problems"], [{"source": "mailbox", "error": "CommandError: agent-mail: locked"}])
            self.assertEqual(snap["agents"][0]["state"], "waiting")


if __name__ == "__main__":
    unittest.main()
