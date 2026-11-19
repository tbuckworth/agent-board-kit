"""The Mail tab on the board (agentboard/mailbox.py, its actions and the collector's use of it):
python3 -m unittest discover -s tests

agent-mail is the mailroom's command, another component with its own tests of the mailbox; here it would read and
change the owner's real mailbox and deliver the owner's replies to real agents, so each test answers for it with a
stand-in that records the argv and stdin it was given, and the mailroom checkout the board finds (AGENT_MAILROOM_DIR)
is a temporary folder.
"""
import base64
import functools
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fixtures  # noqa: E402
import stand_ins  # noqa: E402
from agentboard import actions, mailbox, sent, sources  # noqa: E402

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
    """Each test runs with the board's records in a temporary folder and a mailroom install there that keeps a
    mailbox (its agentmail/mailbox.py), unless the test removes it from `self.root`."""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.real = (sent.PATH, sent.MARKS, actions.AUDIT, sources.find_bin)
        sent.PATH, sent.MARKS, actions.AUDIT = d / "sent.jsonl", d / "sent-marks.jsonl", d / "actions.jsonl"
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
        sent.PATH, sent.MARKS, actions.AUDIT, sources.find_bin = self.real

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

    def test_a_reply_goes_on_stdin_is_kept_by_the_mailroom_and_not_on_the_sent_record(self):
        run = AgentMail(answer({"ok": True, "key": "i31", "status": "sending"}))
        text = "--help; rm -rf ~ $(x)\nsecond line"
        res = self.act({"action": "mail_reply", "thread": THREAD, "key": KEY, "text": "  %s\n" % text}, run)
        self.assertEqual(res, {"ok": True, "key": "i31", "status": "sending"})
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
        self.assertEqual(res, {"ok": True, "key": "i32", "status": "sending"})
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
                    {"action": "mail_part", "key": "x3", "index": 1},
                    {"action": "mail_part", "key": "s3", "index": 0},
                    {"action": "mail_part", "key": "s3; x", "index": 1}):
            res = self.act(req, run)
            self.assertFalse(res["ok"], req)
            self.assertTrue(res["error"], req)
        self.assertEqual(run.calls, [])
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


BOX = {"g1": "<M1@mail.gmail.com>", "g2": "<m2@x>", "g3": "<m3@x>"}    # the owner's Gmail: Gmail id -> Message-ID


class SyncTest(MailTest):
    def sync(self, gmail, run):
        return actions.dispatch({"action": "mail_sync"}, lambda: [], lambda: [], run=run, gmail=gmail)

    @staticmethod
    def changes(calls):
        """The label changes asked of Gmail: batchModify's bodies."""
        return [body for method, url, _, body in calls if method == "POST" and url.endswith("/batchModify")]

    def test_the_tab_and_gmail_are_brought_into_step(self):
        inbox, unread = {"g1", "g2"}, {"g2", "g3"}
        gmail, calls = fixtures.gmail_of(fixtures.OWNER, BOX, inbox=inbox, unread=unread)
        run = AgentMail(answer({"archived": 5, "read": 6,
                                "gmail": {"archive": ["m1@mail.gmail.com"], "read": ["m2@x", "m3@x"]}}))
        res = self.sync(gmail, run)
        self.assertEqual(res, {"ok": True, "archived": 5, "read": 6, "gmail_archived": 1, "gmail_read": 2})
        (argv, stdin), = run.calls
        self.assertEqual(argv, [self.exe, "mailbox", "sync"])
        self.assertEqual(json.loads(stdin), {"inbox": ["m1@mail.gmail.com", "m2@x"], "unread": ["m2@x", "m3@x"]})
        self.assertEqual(self.changes(calls), [{"ids": ["g1"], "removeLabelIds": ["INBOX"]},
                                               {"ids": ["g2", "g3"], "removeLabelIds": ["UNREAD"]}])
        self.assertEqual((inbox, unread), ({"g2"}, set()))                    # Gmail as the tab has it now
        self.assertTrue(any("from%3Aagents%40example.com+in%3Ainbox" in u for _, u, _, _ in calls), calls)
        self.assertTrue(all("AT-secret" not in u and "AT-secret" not in json.dumps(b) for _, u, _, b in calls))
        rec, = self.audited()
        self.assertEqual({k: rec[k] for k in ("action", "archived", "read", "gmail_archived", "gmail_read")},
                         {"action": "mail_sync", "archived": 5, "read": 6, "gmail_archived": 1, "gmail_read": 2})

    def test_nothing_to_change_in_gmail_asks_nothing_of_it(self):
        gmail, calls = fixtures.gmail_of(fixtures.OWNER, BOX, inbox={"g1"}, unread=set())
        run = AgentMail(answer({"archived": 0, "read": 0, "gmail": {"archive": [], "read": []}}))
        self.assertEqual(self.sync(gmail, run), {"ok": True, "archived": 0, "read": 0, "gmail_archived": 0,
                                                 "gmail_read": 0})
        self.assertEqual(self.changes(calls), [])

    def test_a_thousand_ids_to_a_request(self):
        unread = {"g%d" % n for n in range(1500)}
        gmail, calls = fixtures.gmail_of(fixtures.OWNER, BOX, inbox=set(), unread=unread)
        gmail.remove_label(sorted(unread), "UNREAD")
        self.assertEqual([len(c["ids"]) for c in self.changes(calls)], [1000, 500])
        self.assertEqual(unread, set())

    def test_every_page_of_gmail_s_answer_counts(self):
        unread = {"g%d" % n for n in range(4, 1205)}               # more pages than the read check ever lists
        gmail, _ = fixtures.gmail_of(fixtures.OWNER, BOX, inbox={"g1"}, unread=unread)
        self.assertEqual(gmail.state(), ({"m1@mail.gmail.com": ["g1"]},
                                         {"g%d@x" % n: ["g%d" % n] for n in range(4, 1205)}))

    def test_gmail_failing_the_changes_says_the_tab_changed_and_is_recorded(self):
        gmail, _ = fixtures.gmail_of(fixtures.OWNER, BOX, inbox={"g1"}, unread=set(), fail="batchModify")
        run = AgentMail(answer({"archived": 2, "read": 1, "gmail": {"archive": ["m1@mail.gmail.com"], "read": []}}))
        self.assertEqual(self.sync(gmail, run),
                         {"ok": False, "error": "the tab is in step with Gmail (2 threads archived, 1 read), but changing "
                                                "Gmail failed: Google API answered HTTP 503; sync again to finish"})
        rec, = self.audited()
        self.assertEqual({k: rec[k] for k in ("action", "archived", "read", "gmail_error")},
                         {"action": "mail_sync", "archived": 2, "read": 1,
                          "gmail_error": "Google API answered HTTP 503"})

    def test_gmail_failing_to_answer_changes_nothing(self):
        gmail, _ = fixtures.gmail_of(fixtures.OWNER, BOX, inbox={"g1"}, unread=set(), fail="is%3Aunread")
        run = AgentMail()
        self.assertEqual(self.sync(gmail, run), {"ok": False, "error": "Google API answered HTTP 503"})
        self.assertEqual((run.calls, self.audited()), ([], []))

    def test_no_gmail_login_on_the_desktop_is_the_answer_s_error(self):
        run = AgentMail()
        no_login = {"error": "the tab cannot sync with Gmail: the board config names no Gmail login of the owner's "
                             "(gmail_credentials), or the mailroom's config names no agents' account", "ok": False}
        res = actions.dispatch({"action": "mail_sync"}, lambda: [], lambda: [], run=run)    # off: the kit's default
        self.assertEqual(res, no_login)
        with mock.patch.dict(os.environ, {"AGENT_BOARD_GMAIL_CREDS": str(self.root / "no-login.json")}):
            res = actions.dispatch({"action": "mail_sync"}, lambda: [], lambda: [], run=run)     # named, but missing
        self.assertEqual(res, no_login)
        self.assertEqual((run.calls, self.audited()), ([], []))

    def test_someone_else_s_gmail_is_never_used(self):
        gmail, calls = fixtures.gmail_of(fixtures.AGENTS, BOX, inbox={"g1"}, unread=set())
        run = AgentMail()
        res = self.sync(gmail, run)
        self.assertFalse(res["ok"])
        self.assertIn("not the owner's", res["error"])
        self.assertEqual((run.calls, self.audited(), self.changes(calls)), ([], [], []))


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


class CollectorTest(MailTest):
    SID = "11111111-2222-3333-4444-555555555555"

    def board(self, d, read, **kw):
        """A real collector on a status log and mailroom store in `d`: one agent whose latest mail to the owner
        (Message-ID <m1@x>) is unanswered, and a mailbox whose summary counts the Message-IDs in `read` as read in the tab. kw: the
        collector's other arguments (gmail, sent)."""
        (d / "events.jsonl").write_text(json.dumps({"ts": NOW - 4000, "event": "start", "session_id": self.SID,
                                                    "cli": "claude", "name": "alpha", "cwd": "/x"}) + "\n")
        con = fixtures.mailroom_db(d / "mailroom.sqlite")
        con.execute("insert into sessions(tag, cli, session_id, session_name, origin_host) values(?,?,?,?,?)",
                    ("abcd1234", "claude", self.SID, "alpha", "desktop"))
        con.execute("insert into sent(tag, thread_id, message_id, to_addr, subject, sent_at) values(?,?,?,?,?,?)",
                    ("abcd1234", "t1", "<m1@x>", fixtures.OWNER, "Fixed", int(NOW - 290)))
        con.commit()
        con.close()
        run = AgentMail(answer({"unread": 1, "inbox": 4, "read": read}))
        box = sources.Periodic(functools.partial(mailbox.summary, run=run), mailbox.REFRESH, None, sync=True)
        return fixtures.collector(d, claude=lambda: [fixtures.session(self.SID, "alpha", NOW - 3000)], mailbox=box, **kw)

    def test_mail_read_in_the_tab_turns_the_tile_from_waiting_and_the_snapshot_carries_the_counts(self):
        with tempfile.TemporaryDirectory() as d:
            snap = self.board(Path(d), ["<M1@x>"]).snapshot(NOW)
            a, = snap["agents"]
            self.assertTrue(a["mail"]["read"])
            self.assertNotEqual(a["state"], "waiting")
            self.assertIn("read its mail", a["reason"])
            self.assertEqual((snap["mailbox"], snap["problems"]), ({"unread": 1, "inbox": 4}, []))
        with tempfile.TemporaryDirectory() as d:
            a, = self.board(Path(d), ["<other@x>"]).snapshot(NOW)["agents"]
            self.assertIsNone(a["mail"]["read"])                 # no Gmail on this board: not known, still waiting
            self.assertEqual(a["state"], "waiting")

    def test_the_tab_counting_a_mail_read_wins_over_gmail_and_gmail_answers_the_rest(self):
        gmail, _ = fixtures.gmail_of(fixtures.OWNER, BOX, inbox=set(), unread={"g1", "g2"})
        with tempfile.TemporaryDirectory() as d:
            col = self.board(Path(d), ["<M1@mail.gmail.com>"], gmail=gmail)
            col.agents(NOW)                                     # reads the mailbox's summary
            self.assertIsNone(col.is_read("<m2@x>"))            # Gmail not looked at yet: unknown, and now asked
            gmail.is_read("<M1@mail.gmail.com>")                # asked too, so Gmail's answer for it would be known
            gmail.maybe_refresh(force=True)                     # sync: Gmail's state, read in this thread
            self.assertIs(gmail.is_read("<M1@mail.gmail.com>"), False)
            self.assertIs(col.is_read("<m1@MAIL.gmail.com>"), True)    # unread in Gmail, read in the tab
            self.assertIs(col.is_read("<m2@x>"), False)                # not read in the tab: Gmail's answer

    def test_a_done_message_is_ticked_off_once_its_email_is_read_in_the_tab_alone(self):
        sent.record("send", "alpha", "fix it", True, ts=NOW - 600, ref="hhhh", id=self.SID)
        sent.MARKS.write_text(json.dumps({"ts": NOW - 300, "ref": "hhhh", "event": "done", "note": "fixed"}) + "\n")
        with tempfile.TemporaryDirectory() as d:
            col = self.board(Path(d), ["<m1@x>"], sent=sent)     # no Gmail on this board
            col.snapshot(NOW)
            self.assertEqual(col.tick_seen(NOW), ["hhhh"])
            self.assertEqual(sent.messages()[0]["tick"]["note"], 'you read its email "Fixed"')

    def test_a_failing_summary_is_a_problem_and_the_board_carries_on(self):
        with tempfile.TemporaryDirectory() as d:
            col = self.board(Path(d), [])
            col.mailbox.load = functools.partial(mailbox.summary, run=AgentMail((1, "", "agent-mail: locked")))
            snap = col.snapshot(NOW)
            self.assertNotIn("mailbox", snap)                  # no value yet: the Mac's tab says why there is none
            self.assertEqual(snap["problems"], [{"source": "mailbox", "error": "CommandError: agent-mail: locked"}])
            self.assertEqual(snap["agents"][0]["state"], "waiting")


if __name__ == "__main__":
    unittest.main()
