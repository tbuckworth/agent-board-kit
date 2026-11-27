"""A message to a live Codex thread (agentmail/codexqueue.py): typed at the agent's idle prompt when nothing is in
the way (paste), else put in its queue by `codex queue`: the thread's last turn read from its rollout log, the queue
read and never written, and what agent-send reports: taken (delivered), waiting for the turn under way (queued), or
held, with a message held after an interrupted turn released by a note typed at the agent's idle prompt, only when
that is safe. The boundaries: tmux (fakes.install, whose pane draws Codex's idle prompt), Codex itself (a thread that
writes what Codex would to the rollout and the queue: codex_answers, codex_takes_paste), and `codex queue`
(agent-send's queue_codex). Codex's queue and state databases and its rollout are real files in a temporary home."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import fakes  # noqa: E402
import homes  # noqa: E402
from scripts import load_script  # noqa: E402
from test_fastmode import codex_holder  # noqa: E402
from agentmail import codexqueue, commands, config, deliver, registry  # noqa: E402

TID = "01a20000-0000-7000-8000-0000000000c1"
OTHER = "01a20000-0000-7000-8000-0000000000c2"
ITEM = "01a20000-1111-7000-8000-00000000aaaa"
QUEUED = "Queued message %s for thread %s." % (ITEM, TID)
# Codex's idle prompt as tmux shows it: its composer with the placeholder of an empty one, a blank line, its status line
IDLE = "\n".join(["• Done.", "", "› Ask Codex to do anything", "", "  GPT-6.1-Sol xhigh · ~/repo"])
DRAFT = IDLE.replace("Ask Codex to do anything", "half-typed draft")
DIALOG = "\n".join(["  Switch to a cheaper model?", "› 1. Yes", "  2. No", "", "  press enter to confirm"])
# The schema of Codex's queue database (codex-rs/state/queue_migrations, rust-v0.171.0)
SCHEMA = """
create table queued_items (id text primary key not null, thread_id text not null, payload_json text not null,
                           queue_order integer not null, created_at_ms integer not null,
                           updated_at_ms integer not null);
create unique index queued_items_thread_order_idx on queued_items(thread_id, queue_order);
create table queued_thread_revisions (revision integer primary key autoincrement, thread_id text not null unique);
"""


def event(kind, **payload):
    """One line of a rollout log: an event_msg of type kind, as Codex writes it."""
    return json.dumps({"timestamp": "2026-11-23T15:19:20.894Z", "type": "event_msg",
                       "payload": dict(payload, type=kind)}) + "\n"


def other(n=1, size=40):
    """n rollout lines that are no turn event (a tool call's output), each about size bytes."""
    return "".join(json.dumps({"timestamp": "2026-11-23T15:19:21Z", "type": "response_item",
                               "payload": {"type": "custom_tool_call_output", "output": "x" * size}}) + "\n"
                   for _ in range(n))


def append(path, text):
    with open(path, "a") as fh:
        fh.write(text)


def said(text):
    """The rollout line of a user message, as Codex 0.172 writes one: a response item of role user."""
    return json.dumps({"timestamp": "2026-11-24T12:00:00Z", "type": "response_item",
                       "payload": {"type": "message", "role": "user", "id": "u1",
                                   "content": [{"type": "input_text", "text": text}]}}) + "\n"


def typing_now():
    """The fake tmux's list-clients line of someone who typed in the agent's window this second."""
    return {"FAKE_TMUX_CLIENTS": "%d" % time.time()}


def make_queue(path, items=((ITEM, TID),)):
    """Codex's queue database at path, in WAL mode as Codex keeps it, holding items [(item id, thread id)]."""
    con = sqlite3.connect(str(path))
    con.execute("pragma journal_mode=wal")
    con.executescript(SCHEMA)
    with con:
        for i, (item, tid) in enumerate(items):
            con.execute("insert into queued_items values (?, ?, '{}', ?, 0, 0)", (item, tid, i))
    con.close()


def take(path, item=ITEM):
    """Codex starting the queued item: its row deleted, as Codex deletes it once the item's turn has started."""
    con = sqlite3.connect(str(path), timeout=5)
    with con:
        con.execute("delete from queued_items where id = ?", (item,))
    con.close()


class Rollouts(unittest.TestCase):
    """last_turn: what the rollout's last turn event leaves the thread in, as Codex derives its status
    (core/src/agent/status.rs), and where that line starts."""

    def setUp(self):
        self.path = homes.temp_dir(self) / "rollout.jsonl"
        self.path.write_text("")

    def test_the_last_turn_event_says_whether_a_turn_is_under_way(self):
        for lines, state in (([event("task_started")], codexqueue.RUNNING),
                             ([event("task_started"), other(3), event("task_complete")], codexqueue.IDLE),
                             ([event("task_complete"), other(2), event("task_started"), other(5)], codexqueue.RUNNING),
                             ([event("turn_started")], codexqueue.RUNNING),        # the older names Codex still reads
                             ([event("task_started"), event("turn_complete")], codexqueue.IDLE)):
            with self.subTest(lines=lines):
                self.path.write_text("".join(lines))
                self.assertEqual(codexqueue.last_turn(self.path)[0], state)

    def test_an_interrupt_or_a_budget_limit_holds_the_queue_other_aborts_do_not(self):
        for reason, state in (("interrupted", codexqueue.INTERRUPTED), ("budget_limited", codexqueue.INTERRUPTED),
                              ("replaced", codexqueue.IDLE), ("review_ended", codexqueue.IDLE)):
            with self.subTest(reason=reason):
                self.path.write_text(event("task_started") + other(2) + event("turn_aborted", reason=reason))
                self.assertEqual(codexqueue.last_turn(self.path)[0], state)

    def test_a_turn_event_far_back_is_found_with_the_offset_of_its_line(self):
        head = other(3) + event("task_complete") + other(2)
        self.path.write_text(head + event("task_started", turn_id="t9") + other(4000, size=200))   # some 1 MB later
        self.assertGreater(self.path.stat().st_size, 4 * codexqueue.CHUNK)
        self.assertEqual(codexqueue.last_turn(self.path), (codexqueue.RUNNING, len(head.encode())))
        with open(self.path, "rb") as fh:
            fh.seek(len(head.encode()))
            self.assertIn(b'"task_started"', fh.readline())

    def test_a_line_cut_short_or_a_rollout_with_no_turn_yet(self):
        self.path.write_text(event("turn_aborted", reason="interrupted") + event("task_started")[:30])
        self.assertEqual(codexqueue.last_turn(self.path)[0], codexqueue.INTERRUPTED)   # one being written is skipped
        self.path.write_text(other(5))
        self.assertEqual(codexqueue.last_turn(self.path), (codexqueue.IDLE, -1))
        self.path.write_text("")
        self.assertEqual(codexqueue.last_turn(self.path), (codexqueue.IDLE, -1))


class Queue(unittest.TestCase):
    """waiting reads Codex's queue database read-only (mode=ro), where Codex's own settings put it (registry.codex_db,
    which the state database follows too)."""

    def setUp(self):
        self.home = homes.temp_dir(self)
        homes.patch(self, registry, "CODEX_HOME", self.home)
        self.db = self.home / "queue_1.sqlite"
        make_queue(self.db, [(ITEM, TID), ("other-item", OTHER)])

    def test_an_item_waits_until_its_row_is_gone(self):
        self.assertTrue(codexqueue.waiting(TID, ITEM))
        self.assertFalse(codexqueue.waiting(OTHER, ITEM))   # an id is looked for in its own thread
        take(self.db)
        self.assertFalse(codexqueue.waiting(TID, ITEM))
        self.assertTrue(codexqueue.waiting(OTHER, "other-item"))

    def test_the_queue_is_read_and_never_written(self):
        """Read as a live codex keeps it, open in WAL mode with an item added since its last checkpoint: neither the
        database nor its log changes. (Readers of a WAL database mark the shared-memory file as they read.)"""
        codex = sqlite3.connect(str(self.db))
        self.addCleanup(codex.close)
        with codex:
            codex.execute("insert into queued_items values ('later', ?, '{}', 9, 0, 0)", (TID,))
        files = [self.db, self.db.with_name(self.db.name + "-wal")]
        before = [p.read_bytes() for p in files]
        self.assertTrue(codexqueue.waiting(TID, "later"))
        self.assertEqual([p.read_bytes() for p in files], before)
        with mock.patch.object(registry.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            codexqueue.waiting(TID, ITEM)
        uri = connect.call_args.args[0]
        self.assertTrue(uri.startswith("file:") and uri.endswith("?mode=ro"))
        self.assertTrue(connect.call_args.kwargs["uri"])
        gone = self.home / "gone"
        with mock.patch.dict(os.environ, {"CODEX_SQLITE_HOME": str(gone)}), \
                self.assertRaisesRegex(sqlite3.Error, "gone/queue_1.sqlite"):
            codexqueue.waiting(TID, ITEM)
        self.assertFalse(gone.exists())   # never made

    def test_codex_s_databases_are_where_codex_sqlite_home_puts_them(self):
        elsewhere = homes.temp_dir(self)
        make_queue(elsewhere / "queue_1.sqlite", [])
        con = sqlite3.connect(str(elsewhere / "state_5.sqlite"))
        with con:
            con.execute("create table threads (id text, rollout_path text)")
            con.execute("insert into threads values (?, '/r.jsonl')", (TID,))
        con.close()
        with mock.patch.dict(os.environ, {"CODEX_SQLITE_HOME": ""}):
            self.assertEqual(registry.codex_db("queue_1.sqlite"), self.home / "queue_1.sqlite")
            self.assertTrue(codexqueue.waiting(TID, ITEM))
            self.assertIsNone(registry.codex_rollout(TID))   # no state database in CODEX_HOME
        with mock.patch.dict(os.environ, {"CODEX_SQLITE_HOME": str(elsewhere)}):
            self.assertFalse(codexqueue.waiting(TID, ITEM))
            self.assertEqual(registry.codex_rollout(TID), Path("/r.jsonl"))

    def test_the_item_is_codex_s_answer_s(self):
        self.assertEqual(codexqueue.item_id(QUEUED), ITEM)
        self.assertIsNone(codexqueue.item_id("Queued message"))

    def test_the_numbers_come_from_config(self):
        self.assertEqual(codexqueue.settings(dict(config.DEFAULTS)), codexqueue.DEFAULTS)
        got = codexqueue.settings(dict(config.DEFAULTS, codex_queue={"check_seconds": 3}))
        self.assertEqual(got["check_seconds"], 3)
        for bad in ({"check_seconds": -1}, {"release_seconds": "20"}, {"lock_seconds": True}, {"wait": 1}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "codex_queue"):
                codexqueue.settings(dict(config.DEFAULTS, codex_queue=bad))


class LiveThread(unittest.TestCase):
    """A live Codex thread whose codex holds its lock in a pane of the fake tmux (paneeffort.place finds it), with
    its rollout and queue (one item, ITEM, queued) as real files, and Codex's part played by codex_answers. Its
    waits are long (LONG), as an outcome that comes before them returns at once and Codex's delete of a row can
    wait on sqlite's locks; a test of what a wait running out says shortens it (short)."""

    LONG, SHORT = 5, 0.6

    def setUp(self):
        homes.patch(self, codexqueue, "STEP", 0.02)
        self.homes = homes.Homes(self, cfg={"codex_queue": {"check_seconds": self.LONG,
                                                            "release_seconds": self.LONG, "lock_seconds": 0.2}})
        self.rollout = self.homes.codex_home / ("rollout-%s.jsonl" % TID)
        self.rollout.write_text(other(2))
        con = sqlite3.connect(str(self.homes.codex_home / "state_5.sqlite"))
        with con:
            con.execute("create table threads (id text, title text, cwd text, archived int, updated_at int, "
                        "rollout_path text)")
            con.execute("insert into threads values (?, '', '/w', 0, 0, ?)", (TID, str(self.rollout)))
        con.close()
        self.db = self.homes.codex_home / "queue_1.sqlite"
        make_queue(self.db)
        codex_holder(self, homes.temp_dir(self), TID)
        self.sess = {"cli": "codex", "id": TID, "name": "coder", "live": True}

    def tmux(self, screen=IDLE, **env):
        """The fake tmux, its one pane %9 running the thread's codex at screen, nobody typing; env changes that."""
        return fakes.install(self, **dict(dict(FAKE_TMUX_SCREEN=screen, FAKE_TMUX_PANES="%%9 %d" % os.getpid(),
                                               FAKE_TMUX_COMMAND="codex", FAKE_TMUX_PANE_PID=str(os.getpid()),
                                               FAKE_TMUX_IN_MODE="0", FAKE_TMUX_CLIENTS=""), **env))

    def keys(self, bins):
        return [c[1:] for c in bins.calls("tmux") if c[0] == "send-keys"]

    def short(self, wait):
        """config codex_queue's wait (check_seconds or release_seconds) SHORT, for a test of its running out."""
        cfg = json.loads(self.homes.config.read_text())
        cfg["codex_queue"][wait] = self.SHORT
        self.homes.config.write_text(json.dumps(cfg))

    def later(self, seconds, fn, *args):
        """fn(*args) in seconds, on a timer the test waits for before its files go."""
        timer = threading.Timer(seconds, fn, args)
        timer.start()
        self.addCleanup(timer.join)

    def codex_answers(self, bins, start=True, finish=True):
        """Codex in the pane, for the note: once Enter follows text typed in the pane, its turn starts (task_started
        in the rollout) and, with finish, ends; then Codex starts the queued message, deleting its row."""
        def run():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                keys = self.keys(bins)
                if len(keys) >= 2 and keys[-1][-1] == "Enter":
                    break
                time.sleep(0.01)
            else:
                return
            if start:
                append(self.rollout, event("task_started", turn_id="note"))
            if finish:
                time.sleep(0.05)
                append(self.rollout, other(1) + event("task_complete", turn_id="note"))
                take(self.db)
                append(self.rollout, event("task_started", turn_id="message"))
        t = threading.Thread(target=run)
        t.start()
        self.addCleanup(t.join)


class Pasting(LiveThread):
    def codex_takes_paste(self, bins, start=True, record=True):
        """Codex in the pane, for a paste: once Enter follows the paste, its turn starts (task_started) with, when
        record, the pasted text as its user message."""
        def run():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                calls = bins.calls("tmux")
                if any(c[0] == "paste-buffer" for c in calls) and calls[-1] == ["send-keys", "-t", "%9", "Enter"]:
                    break
                time.sleep(0.01)
            else:
                return
            if start:
                append(self.rollout, event("task_started", turn_id="pasted"))
            if record:
                append(self.rollout, said((bins.dir / "buffer").read_text()))
        t = threading.Thread(target=run)
        t.start()
        self.addCleanup(t.join)

    def pasted(self, bins):
        """The tmux calls that typed: load-buffer's, paste-buffer's and send-keys's."""
        return [c for c in bins.calls("tmux") if c[0] in ("load-buffer", "paste-buffer", "send-keys")]


class Paste(Pasting):
    """paste on the live thread."""

    TEXT = "[from claude:peer via agent-send]\nLine two, with a tab\there.\n/review stays text\n@README.md too"

    def paste(self):
        before = codexqueue.before(TID)
        t0 = time.monotonic()
        got = codexqueue.paste(self.sess, before, self.TEXT)
        return got, time.monotonic() - t0

    def test_an_idle_agent_gets_it_as_one_bracketed_paste_and_enter_and_codex_takes_it(self):
        append(self.rollout, event("task_complete"))
        bins = self.tmux()
        self.codex_takes_paste(bins)
        (got, not_typed), took = self.paste()
        self.assertEqual((got, not_typed), (codexqueue.Outcome(codexqueue.DELIVERED, codexqueue.TYPED), None))
        buffer = codexqueue.BUFFER % os.getpid()
        self.assertEqual(self.pasted(bins), [["load-buffer", "-b", buffer, "-"],
                                             ["paste-buffer", "-p", "-d", "-b", buffer, "-t", "%9"],
                                             ["send-keys", "-t", "%9", "Enter"]])
        self.assertEqual((bins.dir / "buffer").read_text(), self.TEXT)   # exactly the message, on stdin
        self.assertLess(took, 2)

    def test_an_agent_whose_last_turn_was_interrupted_gets_it_typed_too(self):
        append(self.rollout, event("task_started") + event("turn_aborted", reason="interrupted"))
        bins = self.tmux()
        self.codex_takes_paste(bins)
        got, not_typed = self.paste()[0]
        self.assertEqual((got.state, not_typed), (codexqueue.DELIVERED, None))

    def test_a_thread_with_no_rollout_yet_is_followed_to_the_one_codex_starts(self):
        con = sqlite3.connect(str(self.homes.codex_home / "state_5.sqlite"))
        with con:
            con.execute("update threads set rollout_path = ''")
        con.close()
        self.rollout.unlink()
        bins = self.tmux()

        def codex():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and ["send-keys", "-t", "%9", "Enter"] not in bins.calls("tmux"):
                time.sleep(0.01)
            self.rollout.write_text(event("task_started") + said(self.TEXT))
            con = sqlite3.connect(str(self.homes.codex_home / "state_5.sqlite"))
            with con:
                con.execute("update threads set rollout_path = ?", (str(self.rollout),))
            con.close()
        t = threading.Thread(target=codex)
        t.start()
        self.addCleanup(t.join)
        got, not_typed = self.paste()[0]
        self.assertEqual((got.state, not_typed), (codexqueue.DELIVERED, None))

    def test_nothing_is_typed_where_something_is_in_the_way(self):
        append(self.rollout, event("task_complete"))
        for env, why in ((typing_now(), "someone typed in its window in the last 5 s"),
                         ({"FAKE_TMUX_SCREEN": DRAFT}, "a draft is in its composer"),
                         ({"FAKE_TMUX_SCREEN": DIALOG}, "its pane does not show the agent's prompt"),
                         ({"FAKE_TMUX_IN_MODE": "1"}, "its pane is in tmux's copy mode"),
                         ({"FAKE_TMUX_COMMAND": "bash"}, "its pane runs bash, not the agent"),
                         ({"FAKE_TMUX_PANES": ""}, "no tmux pane's shell started the codex")):
            with self.subTest(why=why):
                bins = self.tmux(**env)
                got, not_typed = self.paste()[0]
                self.assertIsNone(got)
                self.assertIn(why, not_typed)
                self.assertEqual(self.pasted(bins), [])
        bins = self.tmux()
        with fakes.keys_held_elsewhere():
            got, not_typed = self.paste()[0]
        self.assertEqual((got, not_typed), (None, "another program is typing in an agent's pane"))
        append(self.rollout, event("task_started"))
        got, not_typed = self.paste()[0]
        self.assertEqual((got, not_typed), (None, "the agent is mid-turn"))
        self.assertEqual(self.pasted(bins), [])

    def test_a_paste_that_starts_no_turn_is_unconfirmed_never_held_and_never_typed_again(self):
        self.short("check_seconds")
        append(self.rollout, event("task_complete"))
        bins = self.tmux()
        self.codex_takes_paste(bins, start=False, record=False)
        got, not_typed = self.paste()[0]
        self.assertEqual((got, not_typed), (codexqueue.Outcome(
            codexqueue.UNCHECKED, "it was pasted at its idle prompt and Enter pressed, but no turn started in 0.6 s: "
                                  "it may be in its composer, unsent"), None))
        self.assertEqual(len(self.pasted(bins)), 3)

    def test_a_turn_whose_message_is_not_found_to_be_it_is_unchecked(self):
        self.short("check_seconds")
        append(self.rollout, event("task_complete"))
        bins = self.tmux()
        self.codex_takes_paste(bins, record=False)
        got = self.paste()[0][0]
        self.assertEqual(got, codexqueue.Outcome(codexqueue.UNCHECKED, codexqueue.TYPED + "; a turn started, but its "
                                                 "rollout does not show this message in 0.6 s"))

    def test_an_enter_that_fails_after_the_paste_is_unconfirmed_not_queued(self):
        append(self.rollout, event("task_complete"))
        self.tmux()
        real = commands.tmux

        def tmux(*args, check, input=None):   # tmux's send-keys failing after the paste went in
            if args[0] == "send-keys":
                raise commands.CommandError("tmux send-keys failed: exit 1")
            return real(*args, check=check, input=input)
        with mock.patch.object(commands, "tmux", side_effect=tmux):
            got, not_typed = self.paste()[0]
        self.assertIsNone(not_typed)
        self.assertEqual(got.state, codexqueue.UNCHECKED)
        self.assertTrue(got.why.startswith("it was pasted at its idle prompt, but Enter could not be pressed"), got.why)

    def test_a_rollout_that_cannot_be_read_before_means_nothing_typed_after_means_unchecked(self):
        bins = self.tmux()
        got, not_typed = codexqueue.paste(self.sess, codexqueue.Before(None, 0, "disk I/O error"), "hello")
        self.assertEqual((got, not_typed), (None, "its rollout cannot be read (disk I/O error)"))
        self.assertEqual(self.pasted(bins), [])
        append(self.rollout, event("task_complete"))

        def gone():   # the rollout gone between the paste's Enter and the look at it
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and ["send-keys", "-t", "%9", "Enter"] not in bins.calls("tmux"):
                time.sleep(0.01)
            self.rollout.unlink()
        t = threading.Thread(target=gone)
        t.start()
        self.addCleanup(t.join)
        got, not_typed = codexqueue.paste(self.sess, codexqueue.before(TID), "hello")
        self.assertIsNone(not_typed)
        self.assertEqual(got.state, codexqueue.UNCHECKED)
        self.assertTrue(got.why.startswith(codexqueue.TYPED + ", but its rollout could not be read ("), got.why)


class SaidSince(unittest.TestCase):
    def test_user_messages_in_either_shape_after_the_offset_and_a_line_cut_short_skipped(self):
        path = homes.temp_dir(self) / "rollout.jsonl"
        path.write_text(said("before the mark"))
        mark = path.stat().st_size
        append(path, said("as a response item") + event("task_started")
               + event("user_message", message="as an event") + '{"type": "response_item", "payload": {"ty')
        self.assertEqual(codexqueue.said_since(path, mark), ["as a response item", "as an event"])


class Follow(LiveThread):
    """follow on the live thread."""

    def follow(self, before=None, out=QUEUED):
        """codexqueue.follow on the thread, before taken now unless given. Returns (its outcome, seconds taken)."""
        before = before or codexqueue.before(TID)
        t0 = time.monotonic()
        got = codexqueue.follow(self.sess, before, out)
        return got, time.monotonic() - t0

    def test_a_message_codex_takes_is_delivered(self):
        bins = self.tmux()
        append(self.rollout, event("task_started") + event("task_complete"))
        before = codexqueue.before(TID)
        self.later(0.1, take, self.db)
        got, took = self.follow(before)
        self.assertEqual(got.state, codexqueue.DELIVERED)
        self.assertLess(took, self.LONG)        # once the row has gone, not at the end of the wait
        self.assertEqual(self.keys(bins), [])

    def test_a_message_to_an_agent_mid_turn_is_queued_at_once(self):
        bins = self.tmux()
        append(self.rollout, event("task_started") + other(3))
        got, took = self.follow()
        self.assertEqual(got, codexqueue.Outcome(codexqueue.QUEUED, "the agent is mid-turn: it arrives when that "
                                                                    "turn ends"))
        self.assertLess(took, 0.3)
        self.assertEqual(self.keys(bins), [])

    def test_a_turn_that_starts_after_it_was_queued_is_given_time_to_be_its_own(self):
        """Codex writes the item's task_started before it deletes its row: a turn begun since the message was queued
        is not taken for one it waits behind."""
        self.tmux()
        append(self.rollout, event("task_complete"))
        before = codexqueue.before(TID)
        append(self.rollout, event("task_started", turn_id="message"))
        self.later(0.15, take, self.db)
        self.assertEqual(self.follow(before)[0].state, codexqueue.DELIVERED)
        append(self.rollout, event("task_complete") + event("task_started", turn_id="typed by someone"))
        self.short("check_seconds")
        again = homes.temp_dir(self)
        make_queue(again / "queue_1.sqlite")   # the message queued again
        with mock.patch.dict(os.environ, {"CODEX_SQLITE_HOME": str(again)}):
            got, took = self.follow(before)   # a turn begun since, and the row stays: it waits for that turn
        self.assertEqual(got, codexqueue.Outcome(codexqueue.QUEUED, "a turn started after it was queued: it "
                                                                    "arrives when that turn ends"))
        self.assertGreater(took, self.SHORT)

    def test_an_idle_agent_that_does_not_take_it_holds_it_and_says_what_its_screen_shows(self):
        self.short("check_seconds")
        append(self.rollout, event("task_started") + event("task_complete"))
        for screen, why in ((IDLE, "nothing on its screen says why"),
                            (DIALOG, "its pane shows a dialog or a menu, not its idle prompt")):
            with self.subTest(why=why):
                bins = self.tmux(screen)
                got, took = self.follow()
                self.assertEqual(got.state, codexqueue.HELD)
                self.assertIn("Codex has not taken it in 0.6 s, though the agent is not mid-turn", got.why)
                self.assertIn(why, got.why)
                self.assertGreater(took, self.SHORT)
                self.assertEqual(self.keys(bins), [])

    def test_a_message_held_after_an_interrupt_is_released_by_a_note_at_its_prompt(self):
        bins = self.tmux()
        append(self.rollout, event("task_started") + other(2) + event("turn_aborted", reason="interrupted"))
        self.codex_answers(bins)
        got, _ = self.follow()
        self.assertEqual(got, codexqueue.Outcome(codexqueue.DELIVERED, "its last turn was interrupted, so Codex held "
                                                 "it; agent-send typed a note at its idle prompt, and Codex took it "
                                                 "once the note's turn ended"))
        self.assertEqual(self.keys(bins), [["-t", "%9", "-l", codexqueue.NOTE], ["-t", "%9", "Enter"]])

    def test_a_released_message_waits_for_the_note_s_turn(self):
        self.short("release_seconds")
        bins = self.tmux()
        append(self.rollout, event("turn_aborted", reason="interrupted"))
        self.codex_answers(bins, finish=False)
        got, _ = self.follow()
        self.assertEqual(got, codexqueue.Outcome(codexqueue.QUEUED, "its last turn was interrupted, so Codex held "
                                                 "it; agent-send typed a note at its idle prompt to release it, and "
                                                 "it arrives when the note's turn ends"))
        self.assertEqual(len(self.keys(bins)), 2)

    def test_a_note_that_starts_no_turn_leaves_it_held(self):
        self.short("release_seconds")
        bins = self.tmux()
        append(self.rollout, event("turn_aborted", reason="interrupted"))
        self.codex_answers(bins, start=False, finish=False)
        got, _ = self.follow()
        self.assertEqual(got.state, codexqueue.HELD)
        self.assertIn("a note typed at its idle prompt to release it started no turn in 0.6 s", got.why)
        self.assertEqual(len(self.keys(bins)), 2)   # typed once, never again

    def test_it_never_types_into_a_pane_that_is_not_safe(self):
        append(self.rollout, event("turn_aborted", reason="interrupted"))
        for env, why in (({"FAKE_TMUX_CLIENTS": "%d" % time.time()}, "someone typed in its window in the last 5 s"),
                         ({"FAKE_TMUX_SCREEN": DRAFT}, "a draft is in its composer"),
                         ({"FAKE_TMUX_SCREEN": DIALOG}, "its pane does not show the agent's prompt"),
                         ({"FAKE_TMUX_IN_MODE": "1"}, "its pane is in tmux's copy mode"),
                         ({"FAKE_TMUX_COMMAND": "bash"}, "its pane runs bash, not the agent"),
                         ({"FAKE_TMUX_PANES": ""}, "no tmux pane's shell started the codex")):
            with self.subTest(why=why):
                bins = self.tmux(**env)
                got, _ = self.follow()
                self.assertEqual(got.state, codexqueue.HELD)
                self.assertTrue(got.why.startswith("its last turn was interrupted, so Codex holds it until another "
                                                   "turn starts; agent-send did not type a note to release it"),
                                got.why)
                self.assertIn(why, got.why)
                self.assertEqual(self.keys(bins), [])
        bins = self.tmux()
        with fakes.keys_held_elsewhere():
            got, _ = self.follow()
        self.assertEqual(got.state, codexqueue.HELD)
        self.assertIn("another program is typing in an agent's pane", got.why)
        self.assertEqual(self.keys(bins), [])

    def test_an_answer_naming_no_item_or_a_queue_that_cannot_be_read_is_unchecked(self):
        bins = self.tmux()
        append(self.rollout, event("turn_aborted", reason="interrupted"))
        got, _ = self.follow(out="Queued message")
        self.assertEqual(got.state, codexqueue.UNCHECKED)
        self.assertIn("Codex's answer named no queued item", got.why)
        self.db.rename(self.db.with_suffix(".moved"))
        got, _ = self.follow()
        self.assertEqual(got.state, codexqueue.UNCHECKED)
        self.assertIn("its queue or rollout could not be read", got.why)
        self.assertEqual(self.keys(bins), [])

    def test_a_rollout_or_state_database_that_cannot_be_read_is_unchecked_never_idle(self):
        """An agent mid-turn whose rollout cannot be read is not taken for an idle one (which would be reported
        held, and not mid-turn): whether the message arrived is not checked, and no note is typed."""
        bins = self.tmux()
        append(self.rollout, event("task_started"))
        self.rollout.rename(self.rollout.with_suffix(".moved"))   # where Codex records it, nothing
        got, _ = self.follow()
        self.assertEqual(got.state, codexqueue.UNCHECKED)
        self.assertIn("its rollout could not be read", got.why)
        (self.homes.codex_home / "state_5.sqlite").write_bytes(b"not a database " * 300)
        got, _ = self.follow()
        self.assertEqual(got.state, codexqueue.UNCHECKED)
        self.assertIn("its rollout could not be read", got.why)
        self.assertIn("state_5.sqlite", got.why)
        self.assertEqual(self.keys(bins), [])

    def test_a_config_that_cannot_be_read_is_unchecked_not_an_error(self):
        self.tmux()
        self.homes.config.write_text('{"codex_queue": ')
        got, _ = self.follow()
        self.assertEqual(got.state, codexqueue.UNCHECKED)
        self.assertIn("config", got.why)


class AgentSend(Pasting):
    """agent-send's output and exit code for each outcome, paste and follow running for real on the live thread;
    `codex queue` is the boundary: its stand-in answers as Codex does, the item already in the queue. A test of the
    queue's route has someone typing in the agent's window, or a turn under way, so nothing is typed."""

    def send(self, answer=QUEUED, then=None, receipt=None):
        """agent-send's main to the thread (then, a function: what Codex does as the message goes in). Returns (exit
        code, stdout, stderr)."""
        sender = load_script("agent-send")
        out, err = io.StringIO(), io.StringIO()

        def queue(sess, text):
            self.queued.append(text)
            if then:
                then()
            return True, answer
        self.queued = []
        with mock.patch.object(sender, "queue_codex", side_effect=queue), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = sender.main(["--raw", "--exact-id", "--cli", "codex"] +
                             (["--receipt", str(receipt)] if receipt else []) + [TID, "hello"])
        return rc, out.getvalue(), err.getvalue()

    def test_each_outcome_is_said_and_only_a_held_message_exits_held(self):
        typing = "someone typed in its window in the last 5 s"
        # each case's tmux environment is made as it runs (typing_now, not typing_now()): a time taken when the table
        # was built is more than 5 s old by the later cases, and no longer counts as someone typing
        cases = (("delivered", event("task_complete"), typing_now, lambda: take(self.db), QUEUED, 0,
                  "delivered to codex", typing, "Codex took it from its queue"),
                 ("queued", event("task_started"), dict, None, QUEUED, 0, "queued for codex", "the agent is mid-turn",
                  "the agent is mid-turn: it arrives when that turn ends"),
                 ("held", event("turn_aborted", reason="interrupted"), typing_now, None, QUEUED, deliver.HELD,
                  "held in Codex's queue for codex", typing,
                  "its last turn was interrupted, so Codex holds it until another turn starts; agent-send did not "
                  "type a note to release it, since someone typed in its window in the last 5 s"),
                 ("unchecked", event("task_complete"), typing_now, None, "Queued message", 0, "queued for codex",
                  typing, "Codex's answer named no queued item, so whether it arrived was not checked"))
        for name, last, env, then, answer, code, first, not_typed, why in cases:
            with self.subTest(name):
                make_queue(self.db.with_name("fresh"))
                self.db.with_name("fresh").replace(self.db)        # the item queued again
                append(self.rollout, last)
                bins = self.tmux(**env())
                rc, out, err = self.send(answer, then)
                self.assertEqual((rc, err), (code, ""))
                self.assertTrue(out.startswith(first), out)
                self.assertIn("(%s; not typed at its prompt: %s" % (answer, not_typed), out)
                self.assertEqual(out.strip().splitlines()[-1], why)
                self.assertEqual(self.keys(bins), [])

    def test_an_idle_agent_s_message_is_typed_and_never_queued(self):
        for name, last, start, code, first, why in (
                ("delivered", event("task_complete"), True, 0, "delivered to codex", codexqueue.TYPED),
                ("unconfirmed", event("task_complete"), False, 0, "typed for codex",
                 "it was pasted at its idle prompt and Enter pressed, but no turn started in 0.6 s: it may be in its "
                 "composer, unsent")):
            with self.subTest(name):
                self.short("check_seconds")
                append(self.rollout, last)
                bins = self.tmux()
                self.codex_takes_paste(bins, start=start, record=start)
                rc, out, err = self.send()
                self.assertEqual((rc, err), (code, ""))
                self.assertTrue(out.startswith(first), out)
                self.assertEqual(out.strip().splitlines()[-1], why)
                self.assertEqual(self.queued, [])
                self.assertEqual((bins.dir / "buffer").read_text(), "hello")

    def test_a_paste_tmux_refuses_is_queued_once(self):
        for refused in ("load-buffer", "paste-buffer"):
            with self.subTest(refused):
                append(self.rollout, event("task_complete"))
                bins = self.tmux()
                real = commands.tmux

                def tmux(*args, check, input=None):   # tmux failing that command: the pane gone, say
                    if args[0] == refused:
                        raise commands.CommandError("tmux %s failed: exit 1: can't find pane" % refused)
                    return real(*args, check=check, input=input)
                with mock.patch.object(commands, "tmux", side_effect=tmux):
                    rc, out, _ = self.send(then=lambda: take(self.db))
                self.assertEqual(rc, 0)
                self.assertEqual(self.queued, ["hello"])
                self.assertIn("not typed at its prompt: its pane or rollout could not be read, or tmux failed (tmux "
                              "%s failed" % refused, out.splitlines()[0])
                self.assertNotIn(["send-keys", "-t", "%9", "Enter"], bins.calls("tmux"))
                make_queue(self.db.with_name("fresh"))
                self.db.with_name("fresh").replace(self.db)

    def test_the_rollout_s_end_is_marked_before_the_message_goes_in(self):
        """A turn that starts as the message goes in is one started after it was queued (perhaps its own), not one it
        waits behind: so before() is taken before `codex queue`."""
        append(self.rollout, event("task_complete"))
        self.tmux(**typing_now())
        self.short("check_seconds")
        rc, out, _ = self.send(then=lambda: append(self.rollout, event("task_started")))
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip().splitlines()[-1], "a turn started after it was queued: it arrives when that turn "
                                                         "ends")

    def test_an_error_after_codex_took_it_is_no_failed_send(self):
        """The message is in Codex's queue once `codex queue` said so: an error while following it must not read as
        a failed delivery (exit 1), which the mailroom would hold and send again. follow returns an outcome for what
        it cannot read or do, so the error is a stand-in raised from it."""
        self.tmux(**typing_now())
        with mock.patch.object(codexqueue, "follow", side_effect=commands.CommandError("tmux lost its server")):
            rc, out, _ = self.send()
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("queued for codex"))
        self.assertEqual(out.strip().splitlines()[-1], "whether it arrived could not be checked: tmux lost its server")

    def test_a_held_message_was_accepted(self):
        receipt = homes.temp_dir(self) / "accepted.json"
        append(self.rollout, event("turn_aborted", reason="interrupted"))
        self.tmux(FAKE_TMUX_SCREEN=DRAFT)
        rc, _, _ = self.send(receipt=receipt)
        self.assertEqual(rc, deliver.HELD)
        self.assertTrue(deliver.receipt_written(receipt, TID, "hello"))


if __name__ == "__main__":
    unittest.main()
