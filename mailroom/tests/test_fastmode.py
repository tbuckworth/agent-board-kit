"""Fast mode on a Mail-tab thread: the effort keys in an agent's pane (Claude Code's and Codex's, through a fake tmux
that draws their prompt and steps a level as they do), and fast mode end to end through `agent-mail mailbox fast` and
the daemon: the effort lowered and put back, once per agent whatever its number of fast threads, the line on the owner's
messages, the review skipped for the owning session alone and for mail to the owner alone, the end by switch and by time,
a restore tried again and then reported, the agents whose effort it leaves alone, and the refusals. The boundaries:
tmux (fakes.install), agent-send into a session (deliver.deliver, deliver.deliver_session), Gmail (fakes.fake_gmail),
the reviewer's model call (review.ask_model), and the process tree agent-mail runs in, which is an agent's when an
agent runs these tests (cli.agent_ancestor, tested on its own in AgentAncestor); the registry, store, routing and
review run for real."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
from test_remote import CFG, SID  # noqa: E402
from test_mailbox import HOST, OWNER, Mailbox, no_agent_env  # noqa: E402
from test_groups import A, Base as GroupBase  # noqa: E402
import fakes  # noqa: E402
import homes  # noqa: E402
from scripts import load_script  # noqa: E402
from agentmail import (alerts, cli, claude_session, commands, config, deliver, fastmode,  # noqa: E402
                       groupmail, launch, mailbox, mailroom, outbound, paneeffort, registry, review)
from agentmail.store import Store  # noqa: E402

PANE = "%7"
CODEX_TID = "019a0000-0000-7000-8000-00000000c0de"
OTHER_SID = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
# long enough for the reviewer's model call, and laid out so that no model-free check holds it
LONG = "**Results.**\n\n" + "\n".join("- run %d finished with no errors in its logs" % i for i in range(30)) + "\n"
PASS = ({"kind": "other", "blockers": [], "polish": []}, {})


def codex_holder(test, folder, tid, version="0.171.0"):
    """A process named codex that holds Codex thread tid's lock file open, as Codex does, run from a copy of `sleep`
    laid out as Codex's standalone install lays out a release (its version in the path); stopped when the test ends.
    Returns it."""
    exe = folder / "releases" / ("%s-x86_64-unknown-linux-musl" % version) / "bin" / "codex"
    exe.parent.mkdir(parents=True)
    shutil.copy(shutil.which("sleep"), exe)
    lock = registry.CODEX_HOME / "thread-writer-locks" / (tid + ".lock")
    lock.parent.mkdir(exist_ok=True)
    lock.write_text("")
    with open(lock) as fh:
        holder = subprocess.Popen(["codex", "3600"], executable=str(exe), stdin=fh)
    test.addCleanup(holder.wait)
    test.addCleanup(holder.kill)
    return holder


class Keys(unittest.TestCase):
    """paneeffort against a fake tmux whose pane draws the agent's prompt and steps its effort on shift+down and
    shift+up, a live Claude Code session of a tested version in it."""

    def setUp(self):
        homes.patch(self, paneeffort, "READ_STEP", 0.01)
        homes.patch(self, paneeffort, "READ_SECONDS", 0.3)
        self.homes = homes.Homes(self)
        self.homes.claude(SID, name="agent", tmux="agent:@1.%s" % PANE, version="2.1.329", status="idle")
        self.sess = registry.claude_sessions()[0]
        self.pane = paneeffort.Pane(PANE, os.getpid())
        self.counted = 0

    def install(self, cli, level, **env):
        """A fresh fake tmux with an agent of cli at level in the pane, every knob at rest but those env sets."""
        return fakes.install(self, **{**dict(FAKE_TMUX_EFFORT=level, FAKE_TMUX_EFFORT_CLI=cli, FAKE_TMUX_COMMAND=cli,
                                             FAKE_TMUX_PANE_PID=str(os.getppid()), FAKE_TMUX_IN_MODE="0",
                                             FAKE_TMUX_CLIENTS="", FAKE_TMUX_DIALOG_AFTER="", FAKE_TMUX_OUTPUT="",
                                             FAKE_TMUX_EFFORT_STUCK="", FAKE_TMUX_EFFORT_LATE="0"), **env})

    def keys(self, bins):
        return [c[-1] for c in bins.calls("tmux") if c[0] == "send-keys"]

    def sent(self):
        """set_level's count of each key before it goes."""
        self.counted += 1

    def test_claude_code_steps_down_to_low_and_back_reading_its_line_after_each_key(self):
        bins = self.install("claude", "xhigh")
        self.assertEqual(paneeffort.set_level(self.sess, self.pane, "low", "xhigh", self.sent), 3)
        self.assertEqual(self.keys(bins), ["S-Down"] * 3)
        self.assertEqual(paneeffort.set_level(self.sess, self.pane, "high", "low", self.sent), 2)
        self.assertEqual(self.keys(bins), ["S-Down"] * 3 + ["S-Up"] * 2)
        self.assertEqual(self.counted, 5)   # each key counted before it went

    def test_a_slow_redraw_is_waited_for(self):
        bins = self.install("claude", "xhigh", FAKE_TMUX_EFFORT_LATE="3")   # each level shows from the 4th read on
        self.assertEqual(paneeffort.set_level(self.sess, self.pane, "low", "xhigh", self.sent), 3)
        self.assertEqual(self.keys(bins), ["S-Down"] * 3)

    def test_only_the_line_over_the_prompt_says_the_level_not_the_agents_output(self):
        bins = self.install("claude", "xhigh", FAKE_TMUX_OUTPUT="Docs: the line reads ○ low · /effort")
        paneeffort.set_level(self.sess, self.pane, "low", "xhigh", self.sent)
        self.assertEqual(self.keys(bins), ["S-Down"] * 3)   # not taken for low already
        bins = self.install("codex", "xhigh", FAKE_TMUX_OUTPUT="  GPT-6.1-Sol low · a line of its output")
        paneeffort.set_level(dict(self.sess, cli="codex"), self.pane, "low", "high", self.sent)
        self.assertEqual(self.keys(bins), ["S-Down"] * 3)

    def test_keys_that_change_nothing_stop_with_what_was_pressed(self):
        self.install("claude", "xhigh", FAKE_TMUX_EFFORT_STUCK="1")
        with self.assertRaises(paneeffort.EffortError) as e:
            paneeffort.set_level(self.sess, self.pane, "low", "xhigh", self.sent)
        self.assertEqual((e.exception.pressed, e.exception.level), (1, None))
        self.assertIn("no new effort level showed in its pane within", str(e.exception))

    def test_a_dialog_that_opens_between_keys_stops_them(self):
        bins = self.install("claude", "xhigh", FAKE_TMUX_DIALOG_AFTER="1")
        with self.assertRaises(paneeffort.Wait) as e:
            paneeffort.set_level(self.sess, self.pane, "low", "xhigh", self.sent)
        self.assertEqual((e.exception.pressed, e.exception.risk, self.keys(bins)), (1, True, ["S-Down"]))
        self.assertIn("its pane does not show the agent's prompt", str(e.exception))
        bins = self.install("codex", "xhigh", FAKE_TMUX_DIALOG_AFTER="1")
        with self.assertRaises(paneeffort.Wait):
            paneeffort.set_level(dict(self.sess, cli="codex"), self.pane, "low", "xhigh", self.sent)
        self.assertEqual(self.keys(bins), ["S-Down"])

    def test_what_makes_the_keys_wait(self):
        for env, entry, said in (
                ({"FAKE_TMUX_IN_MODE": "1"}, {}, "its pane is in tmux's copy mode"),
                ({"FAKE_TMUX_COMMAND": "bash"}, {}, "its pane runs bash, not the agent"),
                ({}, {"status": "shell"}, "its session is shell, not idle"),
                ({}, {"status": "busy"}, "its session is busy, not idle"),   # a dialog may open mid-turn
                ({}, {"waitingFor": "permission"}, "a dialog is open in its pane (permission)"),
                ({"FAKE_TMUX_PANE_PID": "1"}, {}, "the pane's shell is not the agent's parent"),
                ({"FAKE_TMUX_CLIENTS": "%d" % time.time()}, {}, "someone typed in its window in the last 5 s"),
                ({"FAKE_TMUX_DIALOG_AFTER": "0"}, {},
                 "its pane does not show the agent's prompt (a dialog or menu may be open)")):
            with self.subTest(said=said):
                bins = self.install("claude", "xhigh", **env)
                path = next((self.homes.default / "sessions").glob("*.json"))
                path.write_text(json.dumps({**json.loads(path.read_text()), "status": "idle", "waitingFor": "",
                                            **entry}))
                self.assertEqual(paneeffort.ready(self.sess, self.pane), said)
                with self.assertRaises(paneeffort.Wait) as e:
                    paneeffort.set_level(self.sess, self.pane, "low", "xhigh", self.sent)
                self.assertEqual((self.keys(bins), e.exception.risk), ([], False))
        self.install("claude", "xhigh", FAKE_TMUX_CLIENTS="%d" % (time.time() - 60))
        self.assertEqual(paneeffort.ready(self.sess, self.pane), "")   # typed a minute ago: not now

    def test_keys_never_wait_for_another_program_typing_in_a_pane(self):
        bins = self.install("claude", "xhigh")
        held, done = threading.Event(), threading.Event()

        def hold():   # a relaunch, say, from its kill to its line's Enter
            with commands.keys_lock(True):
                held.set()
                done.wait(5)
        t = threading.Thread(target=hold)
        t.start()
        self.assertTrue(held.wait(5))
        try:
            with self.assertRaises(paneeffort.Wait) as e:
                paneeffort.set_level(self.sess, self.pane, "low", "xhigh", self.sent)
        finally:
            done.set()
            t.join()
        self.assertEqual((self.keys(bins), e.exception.pressed, e.exception.risk), ([], 0, False))
        self.assertIn("another program is typing in an agent's pane", str(e.exception))

    def test_crafted_screens_that_are_not_the_idle_prompt(self):
        r, c, x = "\u2500" * 60, "\u276f", "\u203a"
        notice = " " * 40 + "\u25d0 medium \u00b7 /effort"   # drawn flush right, as Claude Code draws its notices
        for cli, lines, want in (
                ("claude", ["\u25cf done", "", r, c + " ", r, "  footer"], (True, None)),
                ("claude", ["\u25cf done", notice, r, c + " ", r, "  footer"], (True, "medium")),
                ("claude", ["\u25cf Docs say the line reads \u25d0 low \u00b7 /effort", r, c + " ", r, "  footer"],
                 None),                                                        # the agent's output flush on the rule
                ("claude", ["  \u23bf \u2610 check \u25d0 low \u00b7 /effort", r, c + " ", r], None),   # a todo item
                ("claude", ["", r, c + " list:", "  1. item", r], None),        # a draft that looks like a choice
                ("claude", ["", r, c + " session from yesterday", "  session from last week", r,
                            "  Enter to select \u00b7 Esc to cancel"], None),   # a picker in a ruled box
                ("claude", [" Do you want to proceed?", " " + c + " 1. Yes", "   2. Yes, and don't ask again",
                            "   3. No"], None),                                   # a dialog in the box's place
                ("codex", [x + " Ask Codex to do anything", "  GPT-6.1-Sol medium \u00b7 ~/repo", "  ? for shortcuts"],
                 (True, "medium")),
                ("codex", ["", x + " Ask Codex to do anything", "", "  GPT-6.1-Sol xhigh \u00b7 /home/alex/tmp/a/",
                           "  b/probe-work", "  \u2190 for agents \u00b7 ? for shortcuts" + " " * 40 + "2 warnings"],
                 (True, "xhigh")),                                       # as Codex 0.169.0 draws it, its folder wrapped
                ("codex", [x + " Ask Codex to do anything", "", "", "  GPT-6.1-Sol high \u00b7 ~/repo"], None),
                ("codex", [x + " what does the status line say?", "", "\u2022 It reads:",
                           "  GPT-6.1-Sol low \u00b7 ~/repo, on the last line", "",
                           "  q to quit \u00b7 \u2191/\u2193 to scroll"], None),   # a pager over a history echo
                ("codex", [x + " Ask Codex", "  Approve? (y) yes  (n) no", "  GPT-6.1-Sol high \u00b7 ~/repo"], None),
                ("codex", ["  Would you like to run the following command?", x + " 1. Yes, proceed (y)",
                           "  2. No (esc)"], None)):
            with self.subTest(cli=cli, lines=lines):
                status = paneeffort.view(cli, "\n".join(lines))
                self.assertEqual(None if status is None else (True, paneeffort.shown(cli, status)), want)

    def test_a_dialog_drawn_over_a_box_claude_code_keeps_is_told_by_its_registry_entry(self):
        r, c = "\u2500" * 60, "\u276f"
        dialog = [" Do you want to proceed?", "   1. Yes", "   2. No", "", r, c + " ", r]
        self.assertIsNotNone(paneeffort.view("claude", "\n".join(dialog)))   # the screen alone cannot tell
        self.install("claude", "xhigh")
        path = next((self.homes.default / "sessions").glob("*.json"))
        path.write_text(json.dumps({**json.loads(path.read_text()), "status": "waiting"}))
        self.assertEqual(paneeffort.ready(self.sess, self.pane), "its session is waiting, not idle")

    def test_only_tested_versions_get_keys(self):
        self.assertEqual(paneeffort.untested(self.sess, self.pane), "")
        self.assertIn("Claude Code 2.1.300 isn't on fast mode's tested list yet",
                      paneeffort.untested(dict(self.sess, version="2.1.300"), self.pane))
        holder = codex_holder(self, homes.temp_dir(self), CODEX_TID, "0.171.0")
        codex = {"cli": "codex", "id": CODEX_TID}
        self.assertEqual(paneeffort.untested(codex, paneeffort.Pane(PANE, holder.pid)), "")
        old = codex_holder(self, homes.temp_dir(self), CODEX_TID + "x", "0.150.0")
        self.assertIn("Codex 0.150.0 isn't on fast mode's tested list yet",
                      paneeffort.untested(codex, paneeffort.Pane(PANE, old.pid)))
        self.assertIn("which Codex version runs it could not be told",
                      paneeffort.untested(codex, paneeffort.Pane(PANE, os.getpid())))

    def test_a_codex_pane_is_the_one_whose_codex_holds_the_thread_s_lock(self):
        holder = codex_holder(self, homes.temp_dir(self), CODEX_TID)
        fakes.install(self, FAKE_TMUX_PANES="%%3 1\n%%9 %d\n%%4 2" % os.getpid())
        pane, why = paneeffort.place({"cli": "codex", "id": CODEX_TID, "name": "elsewhere"})
        self.assertEqual((pane, why), (paneeffort.Pane("%9", holder.pid), ""))
        fakes.install(self, FAKE_TMUX_PANES="%%9 %d\n%%8 %d" % (os.getpid(), os.getpid()))
        self.assertEqual(paneeffort.place({"cli": "codex", "id": CODEX_TID, "name": "x"}),
                         (None, "2 tmux panes run the codex that holds its thread's lock"))
        codex_holder(self, homes.temp_dir(self), CODEX_TID)   # a second codex holds the same lock
        fakes.install(self, FAKE_TMUX_PANES="%%9 %d" % os.getpid())
        self.assertEqual(paneeffort.place({"cli": "codex", "id": CODEX_TID, "name": "x"}),
                         (None, "2 codex processes hold its thread's lock"))

    def test_claude_s_level_comes_from_its_newest_reply_else_its_command_line(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--effort", "high"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        sess = dict(self.sess, pid=proc.pid)
        self.assertEqual(paneeffort.current(sess, self.pane), "high")
        folder = self.homes.default / "projects" / "-w"
        folder.mkdir(parents=True)
        (folder / (SID + ".jsonl")).write_text("".join(json.dumps(d) + "\n" for d in (
            {"type": "assistant", "effort": "xhigh", "message": {"model": "claude-fable-5-2"}},
            {"type": "assistant", "effort": "medium", "perTurnEffort": "xhigh", "message": {"model": "claude-fable-5-2"}},
            {"type": "user", "message": {"content": "next"}})))
        self.assertEqual(paneeffort.current(sess, self.pane), "medium")   # the session's level, not an ultrathink turn's


class Shared(unittest.TestCase):
    """The shared code fast mode relies on: every tmux command goes to one server, named with -S, without $TMUX; one
    lock for whatever presses keys in a pane; reading one CLI's sessions reads nothing of the other's; a transcript's
    effort is the session's; a notice never resumes a session."""

    def test_every_tmux_call_names_its_socket_and_drops_tmux_from_the_environment(self):
        bins = fakes.install(self, TMUX="/tmp/tmux-1000/other,1,0", TMUX_TMPDIR="/run/sockets")
        commands.tmux("has-session", "-t", "=x", check=False)
        self.assertEqual(bins.calls("tmux", raw=True), [["-S", "/run/sockets/tmux-%d/default" % os.getuid(),
                                                         "has-session", "-t", "=x"]])
        self.assertEqual(bins.tmux_env(), [])
        homes.config_file(self, {"tmux_socket": "/srv/tmux.sock"})
        commands.tmux("list-panes", "-a", check=False)
        self.assertEqual(bins.calls("tmux", raw=True)[-1][:2], ["-S", "/srv/tmux.sock"])

    def test_a_config_that_does_not_say_which_server_stops_tmux_and_says_why(self):
        bins = fakes.install(self)
        homes.config_file(self, {}).write_text("{not json")
        r = commands.tmux("list-panes", "-a", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("tmux not run: ", r.stderr)
        with self.assertRaisesRegex(commands.CommandError, "tmux not run: "):
            commands.tmux("list-panes", "-a", check=True)
        homes.config_file(self, {"tmux_socket": 3})
        with self.assertRaisesRegex(commands.CommandError, "tmux_socket must be null or a socket's path, not 3"):
            commands.tmux("list-panes", "-a", check=True)
        self.assertEqual(bins.calls("tmux"), [])   # never run, on any server

    def test_a_lock_holder_takes_it_again_another_holder_waits_or_is_told_it_is_busy(self):
        with commands.keys_lock(True):
            with commands.keys_lock(False):   # launch.send_line inside type_line inside a relaunch: no wait
                self.assertTrue(fakes.keys_taken())
            self.assertTrue(fakes.keys_taken())   # held until the outermost hold ends
        self.assertFalse(fakes.keys_taken())

    def test_a_lock_waited_for_too_long_fails_loudly(self):
        """A holder that runs a command which takes the same lock would wait for itself for ever: the command fails."""
        with commands.keys_lock(True):
            child = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r)\n"
                                    "from agentmail import commands\ncommands.LOCK_WAIT_SECONDS = 0.5\n"
                                    "with commands.keys_lock(True): pass" % str(ROOT)],
                                   capture_output=True, text=True, timeout=60)
        self.assertNotEqual(child.returncode, 0)
        self.assertIn("LockBusy: %s is held by another program after 0.5 s of waiting for it (does its holder wait for "
                      "itself?)" % config.KEYS_LOCK, child.stderr)

    def test_a_lock_may_be_waited_for_a_given_time(self):
        with fakes.keys_held_elsewhere():
            t0 = time.monotonic()
            with self.assertRaisesRegex(commands.LockBusy, "is held by another program after 0.3 s of waiting for it$"):
                with commands.keys_lock(0.3):
                    pass
            self.assertGreaterEqual(time.monotonic() - t0, 0.3)
        held, done = threading.Event(), threading.Event()

        def hold_a_moment():
            with commands.keys_lock(True):
                held.set()
                time.sleep(0.2)
        t = threading.Thread(target=hold_a_moment)
        t.start()
        self.assertTrue(held.wait(5))
        with commands.keys_lock(5):   # had once its holder lets go, well within the time given
            self.assertTrue(fakes.keys_taken())
        t.join()

    def test_a_lock_not_had_in_time_is_a_command_that_failed(self):
        held, done = threading.Event(), threading.Event()

        def hold():
            with commands.keys_lock(True):
                held.set()
                done.wait(5)
        t = threading.Thread(target=hold)
        t.start()
        self.assertTrue(held.wait(5))
        homes.patch(self, commands, "LOCK_WAIT_SECONDS", 0.2)
        try:
            with self.assertRaisesRegex(commands.CommandError, "held by another program after 0.2 s of waiting"):
                launch.send_line("%1", "echo hi")   # as agent-spawn types its line: its CommandError says why
        finally:
            done.set()
            t.join()

    def test_key_pressers_share_one_lock(self):
        held, done = threading.Event(), threading.Event()

        def hold():
            with commands.keys_lock(True):
                held.set()
                done.wait(5)
        bins = fakes.install(self)
        t = threading.Thread(target=hold)
        t.start()
        self.assertTrue(held.wait(5), "the lock was not taken")
        threading.Timer(1.0, done.set).start()
        started = time.time()
        launch.send_line("%1", "echo hi")   # waits for the holder
        t.join()
        self.assertGreaterEqual(time.time() - started, 0.9)   # send_line's own pause is 0.3 s
        self.assertEqual([c[:3] for c in bins.calls("tmux")], [["send-keys", "-t", "%1"]] * 2)

    def test_reading_one_cli_s_sessions_reads_nothing_of_the_other(self):
        h = homes.Homes(self)
        h.claude(SID, name="agent")
        (h.codex_home / "state_5.sqlite").write_text("not a database")
        self.assertEqual([s["name"] for s in registry.all_sessions("claude")], ["agent"])

    def test_a_transcript_s_effort_is_the_session_s(self):
        lines = [json.dumps({"type": "assistant", "effort": "medium", "perTurnEffort": "xhigh",
                             "message": {"model": "claude-fable-5-2"}})]
        self.assertEqual(claude_session.transcript_flags(lines)[2:4], ["--effort", "medium"])
        self.assertEqual(claude_session.session_effort(lines), "medium")
        older = [json.dumps({"type": "assistant", "perTurnEffort": "high", "message": {"model": "m"}})]
        self.assertEqual(claude_session.session_effort(older), "high")   # older lines record only perTurnEffort

    def test_a_notice_never_resumes_a_session(self):
        cfg = dict(CFG, resume_offline=True)
        with mock.patch.object(deliver, "deliver", return_value=(0, "sent")) as send:
            deliver.deliver_session({"origin_host": CFG["host_id"], "cli": "claude", "session_id": SID}, "t", cfg,
                                    resume=False)
        self.assertIs(send.call_args.args[3], False)


class AgentAncestor(unittest.TestCase):
    """Whether a Claude Code or Codex agent is among a process's ancestors, read from /proc: a chain of processes
    started here, one of them named claude."""

    def chain(self, name):
        """The pid of a python process whose parent is a shell started as `name`, child of this process."""
        out = homes.temp_dir(self) / "pid"
        script = out.parent / "child.py"
        script.write_text("import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(30)\n")
        # bash started as `name`, which keeps running (the `; true`) with python as its child
        sh = subprocess.Popen(["bash", "-c", 'exec -a %s bash -c "%s %s %s; true"' % (name, sys.executable, script, out)])
        self.addCleanup(sh.wait)
        self.addCleanup(sh.kill)
        for _ in range(100):
            if out.exists() and out.read_text():
                break
            time.sleep(0.05)
        pid = int(out.read_text())
        self.addCleanup(lambda: subprocess.run(["kill", str(pid)]))
        return pid

    def test_an_agent_among_its_ancestors_is_found(self):
        pid = self.chain("claude")
        self.assertEqual(cli.agent_ancestor(pid, stop=os.getpid()), "claude")
        self.assertIsNone(cli.agent_ancestor(self.chain("bash"), stop=os.getpid()))


class FastMode(Mailbox):
    """A desktop mailroom whose session mailtab is a live Claude Code agent (2.1.329) in tmux pane %7 (the pane's shell
    being this process's parent, as agent-spawn's layout has it), at xhigh effort by its transcript and its pane."""

    def setUp(self):
        super().setUp()
        homes.patch(self, paneeffort, "READ_STEP", 0.01)
        homes.patch(self, paneeffort, "READ_SECONDS", 0.3)
        homes.patch(self, cli, "agent_ancestor", lambda pid, stop=None: None)   # see the module docstring
        self.entry = self.homes.claude(SID, name="mailtab", tmux="mailtab:@1.%s" % PANE, version="2.1.329",
                                       status="idle")
        folder = self.homes.default / "projects" / "-w"
        folder.mkdir(parents=True)
        (folder / (SID + ".jsonl")).write_text(json.dumps({"type": "assistant", "effort": "xhigh",
                                                           "message": {"model": "claude-fable-5-2"}}) + "\n")
        self.bins = fakes.install(self, FAKE_TMUX_EFFORT="xhigh", FAKE_TMUX_EFFORT_CLI="claude",
                                  FAKE_TMUX_COMMAND="claude", FAKE_TMUX_PANE_PID=str(os.getppid()))
        self.thread = self.agent_mail()["thread_id"]
        self.told, self.resumed = [], []

        def deliver_to(cli_, sid, text, resume, dangerous, timeout=None):
            self.told.append(text)
            self.resumed.append(resume)
            return 0, "sent"
        homes.patch(self, deliver, "deliver", deliver_to)

    def fast(self, state, thread=None, env=None):
        """(exit code, the JSON printed or None, stderr) of `agent-mail mailbox fast -- THREAD state`, as the board
        runs it."""
        code, out, err = self.main(["mailbox", "fast", "--", thread or self.thread, state], env=env)
        return code, json.loads(out) if out else None, err

    def switch(self, state, thread=None, env=None):
        """fast, then the daemon's next poll, which does the work: (exit code, {"thread": the thread as listed after the
        poll} or the refusal's None, stderr)."""
        code, out, err = self.fast(state, thread, env)
        if code == 0:
            self.poll()
            out = {"ok": True, "thread": self.listed()[thread or self.thread]}
        return code, out, err

    def entry_set(self, **fields):
        self.entry.write_text(json.dumps(dict(json.loads(self.entry.read_text()), **fields)))

    def level(self):
        return (self.bins.dir / "effort").read_text()

    def keys(self):
        return [c[-1] for c in self.bins.calls("tmux") if c[0] == "send-keys"]

    def poll(self, cfg=CFG):
        self.fake.history_since.return_value = ([], "after")
        self.st.set_meta("history_id", "before")
        with mock.patch("sys.stdout"):
            self.room.poll(self.fake, self.st, cfg, False)

    def tab_reply(self, text, key="k" * 20, thread=None):
        """The owner's reply in the thread written in the tab, routed by the daemon. Returns the text the agent got."""
        with mock.patch.object(deliver, "deliver_session", return_value=(0, "sent")) as send:
            self.answer(thread or self.thread, text, key=key)
            self.poll()
        return send.call_args.args[1]

    def reply(self, text, env, args=()):
        """(exit code, stdout) of the agent's `agent-mail reply` to the owner's newest mail in the thread, in env."""
        newest = self.st.con.execute("select max(id) from inbound").fetchone()[0]
        body = homes.temp_dir(self) / "reply.txt"
        body.write_text(text)
        code, out, _ = self.main(["reply", str(newest), "--body-file", str(body)] + list(args), env=env)
        return code, out

    def test_on_is_recorded_at_once_and_the_daemon_lowers_the_effort(self):
        before = time.time()
        code, out, _ = self.fast("on")
        fast = out["thread"]["fast"]
        self.assertEqual(code, 0)
        self.assertEqual((self.level(), self.keys()), ("xhigh", []))   # no key from the board's command
        self.assertGreaterEqual(fast["since"], before)
        self.assertEqual(fast, {"since": fast["since"], "until": fast["since"] + 300, "minutes": 5,
                                "agent": "mailtab (claude)", "effort": "lowering", "was": "xhigh",
                                "note": "Back to xhigh when fast mode ends."})
        self.poll()
        self.assertEqual((self.level(), self.keys()), ("low", ["S-Down"] * 3))
        lowered = dict(fast, effort="low", note="Effort low until fast mode ends, then xhigh again.")
        self.assertEqual(self.listed()[self.thread]["fast"], lowered)
        self.assertEqual(self.fast("on")[1]["thread"]["fast"], lowered)   # on again: nothing changes
        self.poll()
        self.assertEqual(self.keys(), ["S-Down"] * 3)

    def test_a_busy_claude_agent_waits_for_its_turn_to_end(self):
        self.entry_set(status="busy")
        fast = self.switch("on")[1]["thread"]["fast"]
        self.assertEqual((fast["effort"], fast["note"], self.keys()),
                         ("lowering", "Waiting: its session is busy, not idle.", []))
        self.entry_set(status="idle")
        self.poll()
        self.assertEqual((self.level(), self.listed()[self.thread]["fast"]["effort"]), ("low", "low"))

    def test_keys_counted_before_they_go_are_put_back_after_a_pass_cut_off(self):
        os.environ["FAKE_TMUX_SEND_FAIL_AFTER"] = "1"   # the second key fails, as if the pass had been killed there
        fast = self.switch("on")[1]["thread"]["fast"]
        self.assertEqual((fast["effort"], fast["was"], self.level()), ("low", "xhigh", "high"))
        self.assertEqual(self.st.fast_agent(self.sess["tag"])["pressed"], 2)
        del os.environ["FAKE_TMUX_SEND_FAIL_AFTER"]
        self.switch("off")
        self.assertEqual(self.level(), "xhigh")

    def between_polls(self, seconds=1):
        """The daemon's wait of `seconds` between polls, its sleeps skipped, routing what tab replies wait."""
        with mock.patch.object(self.room.time, "sleep"), mock.patch("sys.stdout"):
            self.room.between_polls(self.fake, self.st, CFG, seconds, False)

    def local_thread(self):
        """A thread of agent mail to the owner kept for the Mail tab alone (config mailbox.owner_delivery "local"), which
        Gmail holds no mail of: the daemon routes its tab replies between polls."""
        self.ctx = outbound.Context(dict(CFG, mailbox={"owner_delivery": "local"}))
        return self.agent_mail()["thread_id"]

    def test_a_tab_reply_routed_between_polls_finds_its_agent_s_effort_already_lowered(self):
        thread = self.local_thread()
        code, _, _ = self.fast("on", thread)   # recorded; the keys wait for the daemon's next pass
        self.assertEqual((code, self.level()), (0, "xhigh"))
        levels = []

        def sent(*args, **kwargs):
            levels.append(self.level())
            return 0, "sent"
        with mock.patch.object(deliver, "deliver_session", side_effect=sent):
            self.answer(thread, "Quick one: did it pass?", key="k" * 20)
            self.between_polls()
        self.assertEqual(levels, ["low"])

    def test_a_restore_that_fails_between_polls_uses_up_no_try(self):
        thread = self.local_thread()
        self.switch("on", thread)
        os.environ["FAKE_TMUX_EFFORT_STUCK"] = "1"
        self.switch("off", thread)
        tries = self.st.fast_agent(self.sess["tag"])["tries"]
        routed = []

        def sent(*args, **kwargs):
            routed.append(args)
            return 0, "sent"
        with mock.patch.object(deliver, "deliver_session", side_effect=sent):
            for i in range(fastmode.RESTORE_TRIES):
                self.answer(thread, "Reply %d" % i, key="k%019d" % i)
                self.between_polls()
        self.assertEqual(len(routed), fastmode.RESTORE_TRIES)   # each routed between polls, after a pass
        self.assertEqual(self.st.fast_agent(self.sess["tag"])["tries"], tries)
        self.poll()
        self.assertEqual(self.st.fast_agent(self.sess["tag"])["tries"], tries + 1)

    def test_the_owner_s_messages_carry_the_fast_line_and_keep_it_on(self):
        self.switch("on")
        since = self.st.fast_row(self.thread)["since"]
        self.st.con.execute("update mailbox_fast set since=? where thread_id=?", (since - 250, self.thread))
        text = self.tab_reply("Which run? And when?")
        self.assertIn(fastmode.REMIND % {"owner": "Alex"}, text)
        self.assertNotIn("question mark", text)   # the usual reminder gives way to it
        last = self.st.newest_from(self.thread, [OWNER])
        self.assertEqual(self.listed()[self.thread]["fast"]["until"], last + 300)

    def test_the_owning_session_s_reply_to_the_owner_skips_the_review_and_no_one_else_s(self):
        self.switch("on")
        self.tab_reply("Quick one: did it pass?")
        model = mock.Mock(return_value=PASS)
        with mock.patch.object(review, "ask_model", model):
            code, out = self.reply(LONG, {"CLAUDE_CODE_SESSION_ID": SID})
            self.assertEqual(code, 0)
            self.assertIn("| fast mode: not reviewed", out)
            model.assert_not_called()
            self.assertEqual(self.reply(LONG, {})[0], 0)   # run by no known session: reviewed
            self.assertEqual(model.call_count, 1)
            self.assertEqual(self.reply(LONG, {"CLAUDE_CODE_SESSION_ID": OTHER_SID})[0], 0)
            self.assertEqual(model.call_count, 2)
            # inside the owning session, --session naming another one does not agree with it: reviewed
            self.homes.claude(OTHER_SID, name="other")
            self.assertEqual(self.reply(LONG, {"CLAUDE_CODE_SESSION_ID": SID}, ["--session", "other"])[0], 0)
            self.assertEqual(model.call_count, 3)
        rows = [(r["verdict"], r["kind"]) for r in self.st.con.execute("select * from reviews order by id")]
        self.assertEqual(rows[0], ("skipped", "fast"))
        self.assertNotIn(("skipped", "fast"), rows[1:])

    def test_a_reply_to_anyone_but_the_owner_in_a_fast_thread_is_reviewed(self):
        self.switch("on")
        sam = self.st.record_inbound(gmail_id="j1", thread_id=self.thread, message_id="<j1@x>",
                                       from_addr="sam@example.com", from_name="Sam", subject="Re: Run A results",
                                       matched_tag=self.sess["tag"], action="delivered", body="And the data?")
        inb, row = outbound.reply_target(self.st, sam)
        self.assertFalse(fastmode.skips_review(self.st, CFG, row, inb, (HOST, "claude", SID), time.time()))

    def test_off_is_recorded_at_once_and_the_daemon_tells_the_agent_and_puts_the_effort_back(self):
        self.switch("on")
        code, out, err = self.fast("off")
        self.assertEqual((code, out["thread"]["fast"], self.told), (0, None, []))
        self.assertEqual(self.level(), "low")
        self.assertIsNone(fastmode.active(self.st, CFG, self.thread, time.time()))   # no line, no skip, at once
        self.poll()
        self.assertEqual((self.level(), self.keys()), ("xhigh", ["S-Down"] * 3 + ["S-Up"] * 3))
        self.assertIsNone(self.st.fast_row(self.thread))
        self.assertIsNone(self.st.fast_agent(self.sess["tag"]))
        self.assertEqual(self.told, ['[mailroom: fast mode is over in the thread "Run A results": Alex switched it off. '
                                     'Your effort is going back to xhigh. Write mail as usual again (agent-mail '
                                     'guide); your agent-mail reply goes through the pre-send review again.]'])
        self.assertEqual(self.resumed, [False])
        self.assertEqual(self.fast("off")[0], 0)   # off again: nothing to do
        self.poll()
        self.assertEqual(len(self.told), 1)

    def test_a_notice_a_codex_agent_holds_in_its_queue_is_told_held_not_failed(self):
        homes.patch(self, deliver, "deliver",
                    lambda *a, **k: (deliver.HELD, "held in Codex's queue for x\nwhy it waits"))
        lines = fastmode._tell(types.SimpleNamespace(st=self.st, cfg=CFG), self.sess["tag"], "a notice")
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^fast   %s: told .*, held in Codex's queue: why it waits$" % self.sess["tag"])

    def test_on_again_before_the_daemon_ends_it_keeps_it_on_and_tells_no_one(self):
        self.switch("on")
        self.fast("off")
        self.fast("on")
        self.poll()
        self.assertEqual((self.told, self.listed()[self.thread]["fast"]["effort"], self.level()), ([], "low", "low"))

    def test_it_ends_after_its_minutes_with_no_message_from_the_owner(self):
        self.switch("on")
        self.poll()
        self.assertIsNotNone(self.st.fast_row(self.thread))   # not yet
        self.st.con.execute("update mailbox_fast set since=? where thread_id=?", (time.time() - 301, self.thread))
        self.poll()
        self.assertIsNone(self.st.fast_row(self.thread))
        self.assertEqual(self.level(), "xhigh")
        self.assertIn("Alex wrote nothing in it for 5 minutes. Your effort is going back to xhigh.", self.told[0])

    def test_an_agent_whose_effort_is_still_to_be_lowered_is_not_told_it_is_low(self):
        second = self.agent_mail(subject="Second question")["thread_id"]
        self.entry_set(waitingFor="permission")   # the keys wait
        self.switch("on")
        self.switch("on", second)
        self.switch("off")
        self.assertNotIn("stays low", self.told[0])
        self.assertIn("Fast mode is still on in 1 other thread, so your effort is still to go to low.", self.told[0])

    def test_one_agent_s_effort_is_lowered_once_for_all_its_fast_threads(self):
        second = self.agent_mail(subject="Second question")["thread_id"]
        self.switch("on")
        self.switch("on", second)
        self.assertEqual(self.keys(), ["S-Down"] * 3)
        self.assertEqual(self.listed()[second]["fast"]["effort"], "low")
        self.switch("off")
        self.assertEqual((self.level(), self.keys()), ("low", ["S-Down"] * 3))   # still fast in the second
        self.assertIn("Fast mode is still on in 1 other thread, so your effort stays low.", self.told[0])
        self.switch("off", second)
        self.assertEqual((self.level(), self.keys()), ("xhigh", ["S-Down"] * 3 + ["S-Up"] * 3))

    def test_a_dialog_in_the_pane_makes_the_keys_wait_for_the_daemon(self):
        self.entry_set(waitingFor="permission")
        fast = self.switch("on")[1]["thread"]["fast"]
        self.assertEqual((fast["effort"], fast["note"], self.keys()),
                         ("lowering", "Waiting: a dialog is open in its pane (permission).", []))
        self.entry_set(waitingFor="")
        self.poll()
        self.assertEqual((self.st.fast_agent(self.sess["tag"])["effort"], self.level()), ("low", "low"))

    def test_a_dialog_that_opens_as_a_key_goes_alerts_the_owner_and_the_daemon_finishes_later(self):
        homes.config_file(self, dict(json.loads(self.homes.config.read_text()),
                                     mailbox={"owner_delivery": "local"}))
        os.environ["FAKE_TMUX_DIALOG_AFTER"] = "1"
        fast = self.switch("on")[1]["thread"]["fast"]
        self.assertEqual((fast["effort"], fast["was"], self.keys(), self.level()),
                         ("lowering", "xhigh", ["S-Down"], "high"))
        self.assertIn(fastmode.RISK_NOTE, fast["note"])
        alert, = [m for m, _ in self.sent if "as a dialog opened" in m["Subject"]]   # through Gmail
        self.assertIn("may have moved that dialog's highlight", alert.get_body(("plain",)).get_content())
        del os.environ["FAKE_TMUX_DIALOG_AFTER"]
        self.poll()
        self.assertEqual((self.level(), self.keys()), ("low", ["S-Down"] * 3))
        self.assertEqual(len([m for m, _ in self.sent if "as a dialog opened" in m["Subject"]]), 1)

    def test_a_key_alert_that_cannot_go_out_is_kept_on_the_thread_and_tried_again(self):
        self.fake.send_raw.side_effect = RuntimeError("Gmail is down")
        os.environ["FAKE_TMUX_DIALOG_AFTER"] = "1"
        self.switch("on")
        self.assertEqual([a["tag"] for a in self.st.fast_alerts().values()], [self.sess["tag"]])
        del os.environ["FAKE_TMUX_DIALOG_AFTER"]
        self.poll()   # the effort is lowered and its note rewritten; Gmail is still down
        fast = self.listed()[self.thread]["fast"]
        self.assertEqual((fast["effort"], fast["note"]), ("low", "Effort low until fast mode ends, then xhigh again. "
                                                          + fastmode.RISK_NOTE))
        self.assertFalse([m for m, _ in self.sent if "as a dialog opened" in m["Subject"]])
        self.fake.send_raw.side_effect = self.send
        self.poll()
        alert, = [m for m, _ in self.sent if "as a dialog opened" in m["Subject"]]
        self.assertIn("mailtab", alert["Subject"])
        self.assertEqual((self.st.fast_alerts(), self.listed()[self.thread]["fast"]["note"]),
                         ({}, "Effort low until fast mode ends, then xhigh again."))

    def test_each_key_a_dialog_may_have_taken_is_alerted_on_its_own(self):
        os.environ["FAKE_TMUX_DIALOG_AFTER"] = "1"   # a dialog opens as the first key goes
        self.switch("on")
        del os.environ["FAKE_TMUX_DIALOG_AFTER"]
        self.poll()
        self.assertEqual(self.level(), "low")
        os.environ["FAKE_TMUX_DIALOG_AFTER"] = "4"   # and again, later, as the first key putting it back goes
        self.switch("off")
        self.assertEqual(len([m for m, _ in self.sent if "as a dialog opened" in m["Subject"]]), 2)
        self.assertEqual(self.st.fast_alerts(), {})

    def test_keys_that_wait_before_any_goes_alert_no_one(self):
        self.entry_set(waitingFor="permission")
        self.switch("on")
        self.assertFalse([m for m, _ in self.sent if "as a dialog opened" in m["Subject"]])

    def test_a_restore_that_waits_is_finished_by_the_daemon_and_on_again_cancels_it(self):
        self.switch("on")
        self.entry_set(waitingFor="permission")
        self.switch("off")
        self.assertEqual(self.st.fast_agent(self.sess["tag"])["effort"], "raising")
        self.assertIn("Your effort is going back to xhigh.", self.told[0])
        self.switch("on")   # on again for the same agent before its effort went back: it stays low, no keys
        self.assertEqual((self.st.fast_agent(self.sess["tag"])["effort"], self.keys()), ("low", ["S-Down"] * 3))
        self.switch("off")
        self.entry_set(waitingFor="")
        self.poll()
        self.assertEqual((self.level(), self.st.fast_agent(self.sess["tag"])), ("xhigh", None))

    def test_keys_that_may_have_changed_it_keep_the_level_to_put_back(self):
        os.environ["FAKE_TMUX_EFFORT_LATE"] = "1000"   # the keys take, but no level ever shows
        fast = self.switch("on")[1]["thread"]["fast"]
        self.assertEqual((fast["effort"], fast["was"], self.level()), ("low", "xhigh", "high"))
        self.assertIn("Effort may be partly lowered", fast["note"])
        del os.environ["FAKE_TMUX_EFFORT_LATE"]
        self.switch("off")
        self.assertEqual(self.level(), "xhigh")   # put back from where it was

    def test_a_restore_that_fails_is_tried_again_then_the_owner_is_alerted_through_gmail(self):
        homes.config_file(self, dict(json.loads(self.homes.config.read_text()),
                                     mailbox={"owner_delivery": "local"}))
        self.switch("on")
        os.environ["FAKE_TMUX_EFFORT_STUCK"] = "1"
        self.switch("off")
        self.assertEqual(self.st.fast_agent(self.sess["tag"])["effort"], "raising")
        for _ in range(fastmode.RESTORE_TRIES - 2):   # `off` tried once
            self.poll()
        self.assertIsNotNone(self.st.fast_agent(self.sess["tag"]))
        self.assertFalse([m for m, _ in self.sent if "fast mode could not put" in m["Subject"]])
        self.poll()
        alert, = [m for m, _ in self.sent if "fast mode could not put" in m["Subject"]]   # through Gmail
        self.assertIn("could not be put back to xhigh", alert.get_body(("plain",)).get_content())
        self.assertIn("your effort could not be put back to xhigh", self.told[-1])
        self.assertIn("Alex has been told.", self.told[-1])
        self.assertIsNone(self.st.fast_agent(self.sess["tag"]))

    def give_up(self, cfg=CFG):
        """Fast mode on and off with keys that do nothing, polled (with cfg) until the restore is given up."""
        self.switch("on")
        os.environ["FAKE_TMUX_EFFORT_STUCK"] = "1"
        self.switch("off")
        for _ in range(fastmode.RESTORE_TRIES - 1):
            self.poll(cfg)

    def test_each_restore_given_up_is_alerted_and_told_on_its_own(self):
        self.give_up()
        for name in ("effort", "drawn"):   # set back to xhigh by hand, and the keys work again
            (self.bins.dir / name).write_text("xhigh")
        del os.environ["FAKE_TMUX_EFFORT_STUCK"]
        self.give_up()   # lowered, then the restore fails again
        self.assertEqual(len([m for m, _ in self.sent if "fast mode could not put" in m["Subject"]]), 2)
        self.assertEqual([t.endswith("Alex has been told.]") for t in self.told if "could not be put back" in t],
                         [True, True])
        self.assertIsNone(self.st.fast_agent(self.sess["tag"]))

    def test_an_alert_that_cannot_go_out_is_tried_again_and_nobody_is_told_it_went(self):
        self.fake.send_raw.side_effect = RuntimeError("Gmail is down")
        self.give_up()
        self.assertEqual(self.st.fast_agent(self.sess["tag"])["effort"], "unreported")   # kept for the next poll
        self.assertFalse(any("has been told" in t for t in self.told))
        self.fake.send_raw.side_effect = self.send
        self.poll()
        self.assertTrue([m for m, _ in self.sent if "fast mode could not put" in m["Subject"]])
        self.assertIn("Alex has been told.", self.told[-1])
        self.assertIsNone(self.st.fast_agent(self.sess["tag"]))

    def test_alerts_turned_off_are_said_and_not_claimed(self):
        homes.config_file(self, dict(json.loads(self.homes.config.read_text()), alerts_enabled=False))
        self.give_up(dict(CFG, alerts_enabled=False))
        self.assertFalse([m for m, _ in self.sent if "fast mode could not put" in m["Subject"]])
        self.assertIn("Alex could not be told: alerts are off (config alerts_enabled).", self.told[-1])
        self.assertIsNone(self.st.fast_agent(self.sess["tag"]))

    def test_a_row_that_fails_neither_stops_the_others_nor_runs_forever(self):
        self.switch("on")
        self.st.con.execute("insert into fast_agents(tag, effort, was, note, pressed, tries) "
                            "values('gone', 'raising', 'high', '', 1, 0)")   # an agent with no sessions row
        self.st.con.execute("update mailbox_fast set since=? where thread_id=?", (time.time() - 301, self.thread))
        self.poll()
        self.assertIsNone(self.st.fast_row(self.thread))   # ended all the same
        for _ in range(fastmode.RESTORE_TRIES):
            self.poll()
        self.assertIsNone(self.st.fast_agent("gone"))
        self.assertTrue([m for m, _ in self.sent if "fast mode could not put" in m["Subject"]])

    def test_the_agents_whose_effort_it_leaves_alone(self):
        transcript = self.homes.default.joinpath("projects", "-w", SID + ".jsonl")
        entry, said = self.entry.read_text(), transcript.read_text()
        for case, change, note in (
                ("untested", lambda: self.entry_set(version="2.1.300"),
                 "Effort unchanged: Claude Code 2.1.300 isn't on fast mode's tested list yet: `agent-mail fast-trial "
                 "claude --version 2.1.300` tries its keys and adds it."),
                ("max", lambda: transcript.write_text(json.dumps({"type": "assistant", "effort": "max"}) + "\n"),
                 "Effort unchanged: it runs at max, which its keys cannot put back."),
                ("low", lambda: transcript.write_text(json.dumps({"type": "assistant", "effort": "low"}) + "\n"),
                 "Effort unchanged: it runs at low already."),
                ("background", lambda: self.entry_set(kind="bg"),
                 "Effort unchanged: it is a background session (claude --bg), with no pane to press keys in."),
                ("gone", lambda: self.entry.unlink(), "Effort unchanged: the agent is not running.")):
            with self.subTest(case=case):
                self.entry.write_text(entry)
                transcript.write_text(said)
                change()
                fast = self.switch("on")[1]["thread"]["fast"]
                self.assertEqual((fast["effort"], fast["was"], fast["note"]), ("unchanged", None, note))
                self.switch("off")
        self.assertEqual(self.keys(), [])

    def test_a_mac_agent_gets_the_line_and_the_skip_but_no_keys(self):
        mac = self.st.session_for("claude", SID, "macagent", origin_host="mac")
        thread = self.agent_mail(subject="Mac run", sess=mac)["thread_id"]
        fast = self.switch("on", thread)[1]["thread"]["fast"]
        self.assertEqual((fast["agent"], fast["effort"], fast["note"]),
                         ("macagent (claude@mac)", "unchanged",
                          "Effort unchanged: the agent runs on mac, where the mailroom presses no keys."))
        inb = dict(self.his_mail(thread, text="Done?", at=time.time()), matched_tag=mac["tag"])
        self.assertTrue(fastmode.skips_review(self.st, CFG, mac, inb, ("mac", "claude", SID), time.time()))
        self.assertFalse(fastmode.skips_review(self.st, CFG, mac, inb, (HOST, "claude", SID), time.time()))
        with mock.patch.object(deliver, "_remote_deliver", return_value=(0, "queued")) as send:
            self.switch("off", thread)
        self.assertIn("fast mode is over", send.call_args.args[2]["text"])
        self.assertEqual(self.keys(), [])

    def test_a_codex_agent_s_effort_is_stepped_by_its_own_keys(self):
        holder = codex_holder(self, homes.temp_dir(self), CODEX_TID, "0.169.0")
        codex = self.st.session_for("codex", CODEX_TID, "codexer", origin_host=HOST)
        con = __import__("sqlite3").connect(str(self.homes.codex_home / "state_5.sqlite"))
        with con:
            con.execute("create table if not exists threads (id text, title text, cwd text, archived int, "
                        "updated_at int, rollout_path text)")
            con.execute("insert into threads values (?, '', '/w', 0, 0, null)", (CODEX_TID,))
        con.close()
        thread = self.agent_mail(subject="Codex run", sess=codex)["thread_id"]
        os.environ.update(FAKE_TMUX_EFFORT_CLI="codex", FAKE_TMUX_COMMAND="codex", FAKE_TMUX_PANE_PID=str(os.getpid()),
                          FAKE_TMUX_PANES="%%9 %d" % os.getpid())
        (self.bins.dir / "effort").write_text("high")
        fast = self.switch("on", thread)[1]["thread"]["fast"]
        self.assertEqual((fast["effort"], fast["was"], self.level(), self.keys()), ("low", "high", "low",
                                                                                     ["S-Down"] * 2))
        self.switch("off", thread)
        self.assertEqual(self.level(), "high")
        self.assertTrue(holder.pid)

    def test_what_it_refuses(self):
        code, _, err = self.fast("on", env={"CLAUDE_CODE_SESSION_ID": SID})
        self.assertEqual(code, 2)
        self.assertIn("refusing mailbox fast inside an agent session", err)
        with mock.patch.object(cli, "agent_ancestor", return_value="claude"):
            code, _, err = self.fast("on")
        self.assertEqual(code, 2)
        self.assertIn("refusing mailbox fast under a claude process", err)
        self.assertEqual(self.fast("on", "nosuchthread")[0], 4)
        unrouted = self.agent_mail(subject="[cloud-watchdog] Daily", sess=None)["thread_id"]
        code, _, err = self.fast("on", unrouted)
        self.assertEqual(code, 1)
        self.assertIn("fast mode is for a thread with one agent", err)
        self.assertIsNone(self.st.fast_row(unrouted))
        with self.assertRaisesRegex(ValueError, "fast_minutes must be a whole number"):
            fastmode.minutes(dict(CFG, mailbox={"fast_minutes": "10"}))

    def test_the_length_comes_from_config(self):
        homes.config_file(self, dict(json.loads(self.homes.config.read_text()), mailbox={"fast_minutes": 3}))
        fast = self.fast("on")[1]["thread"]["fast"]
        self.assertEqual((fast["minutes"], fast["until"] - fast["since"]), (3, 180))


class GroupThread(GroupBase):
    def setUp(self):
        super().setUp()
        self.st = Store(homes.temp_dir(self) / "mailroom.sqlite")
        self.addCleanup(self.st.con.close)
        self.room = load_script("agent-mailroom")
        fakes.sending_gmail(self, outbound, self.room, mailroom, alerts)
        homes.patch(self, outbound, "Store", lambda: self.st)
        homes.patch(self, config, "MESSAGES_DIR", homes.temp_dir(self) / "messages")

    def test_a_group_s_thread_is_refused(self):
        self.group(A, "Release", "bob")
        groupmail.announce(self.room.gmail, self.st, self.cfg)
        with no_agent_env(), self.assertRaisesRegex(outbound.MailError, "goes to group 'Release': fast mode is for a "
                                                                         "thread with one agent") as refused:
            mailbox.fast(outbound.Context.opened(self.cfg, self.st, None), "t1", True, time.time())
        self.assertEqual(refused.exception.exit_code, 1)
        self.assertIsNone(self.st.fast_row("t1"))


if __name__ == "__main__":
    unittest.main()
