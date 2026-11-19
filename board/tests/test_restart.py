"""The desktop restart engine with fakes: python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _kitenv  # noqa: E402,F401  (scratch config with an example owner, before agentboard reads any)
import fixtures  # noqa: E402
import stand_ins  # noqa: E402
from agentboard import actions, collect, relaunch, restart, sources  # noqa: E402

NOW = 1_800_000_000.0
# plan's inspection reads the live agents' transcripts and /proc: tests give it fixed answers instead
INSPECTED = lambda a: {"model": "m", "effort": "e", "waits": [], "processes": []}


def agent(**kw):
    a = {"id": "11111111-2222-3333-4444-555555555555", "cli": "claude", "name": "alpha", "display": "alpha",
         "state": "idle", "reason": "", "tmux": "alpha", "cwd": tempfile.gettempdir(), "kind": "interactive",
         "config_dir": "", "account": "default", "model": "claude-opus-5-5", "effort": "xhigh", "last_activity": NOW - 60,
         "last_note": {"ts": NOW - 100, "text": "tests pass"}, "waits": [], "processes": [], "choice": "quiet", "live": True}
    a.update(kw)
    return a


class StateDir:
    """Point the restart module at a temporary state dir."""
    def __enter__(self):
        self.d = Path(tempfile.mkdtemp())
        self.saved = (restart.STATE, restart.MANIFEST, restart.STATUS, restart.LOG, restart.CANCEL, restart.mail)
        restart.STATE, restart.MANIFEST, restart.STATUS = self.d, self.d / "manifest.json", self.d / "status.json"
        restart.LOG, restart.CANCEL = self.d / "run.log", self.d / "cancel"
        self.mails = []
        restart.mail = lambda subject, body: self.mails.append((subject, body))   # agent-mail would send real mail
        return self

    def __exit__(self, *exc):
        (restart.STATE, restart.MANIFEST, restart.STATUS, restart.LOG, restart.CANCEL, restart.mail) = self.saved


def running(name="alpha", status="idle", sid="11111111-2222-3333-4444-555555555555"):
    """An agent running now, as the ~/.claude session registry lists it."""
    return fixtures.session(sid, name, time.time() - 60, cwd=tempfile.gettempdir(), tmux=name, raw_status=status)


def board(registry):
    """A real collect.Collector on an empty folder. `registry` stands in for the live ~/.claude session registry,
    which lists the machine's running agents: it returns rows as running() builds them."""
    return fixtures.collector(tempfile.mkdtemp(), claude=registry)


class CommandsTest(unittest.TestCase):
    def test_show_cmd_unwraps_claude_tool_shells(self):
        bg = "/bin/bash -c source /x/snap.sh 2>/dev/null || true && eval 'for i in $(seq 1 9); do sleep 30; done'"
        fg = "/bin/bash -c source /x/snap.sh && eval 'grep -q '\"'\"'DONE'\"'\"' log' && pwd -P >| /tmp/cwd"
        self.assertEqual(restart.show_cmd(bg), "for i in $(seq 1 9); do sleep 30; done")
        self.assertEqual(restart.show_cmd(fg), "grep -q 'DONE' log")
        self.assertEqual(restart.show_cmd("python3 train.py --x 1"), "python3 train.py --x 1")

    def test_processes_skip_the_turn_in_progress_and_helpers(self):
        d = tempfile.mkdtemp()
        sid = "11111111-2222-3333-4444-555555555555"
        procs = {100: (1, b"claude\x00--resume\x00" + sid.encode(), b""),
                 101: (100, b"/bin/bash\x00-c\x00source s.sh && eval 'ls' && pwd -P >| /tmp/x", b""),     # foreground tool
                 102: (100, b"/bin/bash\x00-c\x00source s.sh && eval 'for i in 1 2; do sleep 30; done'", b""),
                 103: (100, b"npx\x00-y\x00some-mcp-server", b""),
                 104: (1, b"python3\x00train.py", b"CLAUDE_CODE_SESSION_ID=" + sid.encode()),             # detached job
                 105: (1, b"python3\x00other.py", b""),
                 106: (101, b"python3\x00bin/agent-restart\x00plan", b""),                                  # inside the tool call
                 107: (100, b"/opt/claude/vendor/ugrep\x00-G\x00--ignore-files\x00foo", b""),           # the Grep tool
                 108: (1, b"bws\x00run\x00--\x00timeout\x003600\x00bash\x00queue.sh", b"CLAUDE_CODE_SESSION_ID=" + sid.encode()),
                 109: (108, b"timeout\x003600\x00bash\x00queue.sh", b"CLAUDE_CODE_SESSION_ID=" + sid.encode()),
                 110: (109, b"bash\x00queue.sh", b"CLAUDE_CODE_SESSION_ID=" + sid.encode()),
                 111: (110, b"sleep\x0060", b"CLAUDE_CODE_SESSION_ID=" + sid.encode())}
        for pid, (ppid, cmd, env) in procs.items():
            os.makedirs(os.path.join(d, str(pid)))
            open(os.path.join(d, str(pid), "stat"), "w").write("%d (x) S %d 0 0" % (pid, ppid))
            open(os.path.join(d, str(pid), "cmdline"), "wb").write(cmd)
            open(os.path.join(d, str(pid), "environ"), "wb").write(env)
        got = restart.agent_processes(agent(pid=100, state="busy"), proc=d)
        self.assertEqual([p["cmd"] for p in got], ["for i in 1 2; do sleep 30; done", "python3 train.py", "bash queue.sh"])

    def test_background_commands_end_like_foreground_ones_now(self):
        # newer Claude Code versions end every Bash tool call with `< /dev/null && pwd -P >| ...`, background or
        # not; the restart plan must still list background jobs
        d = tempfile.mkdtemp()
        tail = b"' < /dev/null && pwd -P >| /tmp/claude-ab-cwd"
        procs = {100: (1, b"claude\x00--resume\x00x", 1000),
                 101: (100, b"/bin/bash\x00-c\x00source s.sh && eval 'python3 train.py --long" + tail, 2000),
                 102: (101, b"python3\x00train.py\x00--long", 2001),
                 103: (100, b"/bin/bash\x00-c\x00source s.sh && eval 'git status" + tail, 3000)}
        for pid, (ppid, cmd, start) in procs.items():
            os.makedirs(os.path.join(d, str(pid)))
            fields = ["S", str(ppid)] + ["0"] * 17 + [str(start)]
            open(os.path.join(d, str(pid), "stat"), "w").write("%d (x) %s" % (pid, " ".join(fields)))
            open(os.path.join(d, str(pid), "cmdline"), "wb").write(cmd)
            open(os.path.join(d, str(pid), "environ"), "wb").write(b"")
        idle = [p["cmd"] for p in restart.agent_processes(agent(pid=100, state="background"), proc=d)]
        self.assertEqual(idle, ["python3 train.py --long", "git status"])      # no turn running: both are jobs
        busy = [p["cmd"] for p in restart.agent_processes(agent(pid=100, state="busy"), proc=d)]
        self.assertEqual(busy, ["python3 train.py --long"])                    # the newest is the turn's command

    def test_updates_info_without_an_nvidia_gpu_is_not_a_fault(self):
        calls = []

        def run(cmd, timeout=0, env=None):
            calls.append(cmd[0])
            if cmd[0].endswith("apt-check"):
                return 0, "", "3;1"
            return 0, "", ""
        real = sources.find_bin
        try:
            sources.find_bin = lambda n: None
            u = collect.updates_info(run)
            self.assertEqual((u["total"], u["security"], u["gpu_ok"], u["gpu_message"]), (3, 1, None, ""))
            self.assertNotIn("nvidia-smi", calls)
            sources.find_bin = lambda n: "/usr/bin/" + n
            broken = collect.updates_info(lambda cmd, timeout=0, env=None:
                                          (9, "", "NVIDIA-SMI has failed\n") if "nvidia-smi" in cmd[0] else (0, "", ""))
            self.assertEqual((broken["gpu_ok"], broken["gpu_message"]), (False, "NVIDIA-SMI has failed"))
        finally:
            sources.find_bin = real

    def test_background_waits_leave_out_finished_and_older_commands(self):
        home = Path(tempfile.mkdtemp())
        sid = "11111111-2222-3333-4444-555555555555"
        (home / "projects" / "-x").mkdir(parents=True)

        def iso(t):
            return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))

        def bg(tid, t, what):
            return {"type": "assistant", "timestamp": iso(t), "message": {"content": [
                {"type": "tool_use", "id": tid, "name": "Bash",
                 "input": {"command": "sleep 1", "description": what, "run_in_background": True}}]}}

        def done(tid, t, status="completed"):
            return {"type": "user", "timestamp": iso(t), "message": {"content": (
                "<task-notification>\n<task-id>b1</task-id>\n<tool-use-id>%s</tool-use-id>\n<status>%s</status>\n"
                "<summary>Background command completed</summary>\n</task-notification>" % (tid, status))}}

        def started(tid, task, t):
            return {"type": "user", "timestamp": iso(t), "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "Command running in background with ID: %s. "
                                                                        "Output is being written to: /x" % task}]}}

        def stop(task, t):
            return {"type": "assistant", "timestamp": iso(t), "message": {"content": [
                {"type": "tool_use", "id": "toolu_stop", "name": "TaskStop", "input": {"task_id": task}}]}}

        rows = [bg("toolu_old", NOW - 7200, "from before this process"), bg("toolu_a", NOW - 600, "finished"),
                done("toolu_a", NOW - 500), bg("toolu_b", NOW - 400, "failed"), done("toolu_b", NOW - 300, "failed"),
                bg("toolu_s", NOW - 350, "stopped by the agent"), started("toolu_s", "bstop1", NOW - 349),
                stop("bstop1", NOW - 250), bg("toolu_c", NOW - 200, "still running"),
                bg("toolu_q", NOW - 190, "ended mid-turn"),
                {"type": "attachment", "timestamp": iso(NOW - 100), "attachment": {
                    "type": "queued_command", "prompt": done("toolu_q", NOW - 100)["message"]["content"]}},
                # a request record repeats earlier messages, with no timestamp: not a new command
                dict(bg("toolu_a", NOW, "finished"), type="api-request-blob", timestamp=None)]
        (home / "projects" / "-x" / ("%s.jsonl" % sid)).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        real = sources.claude_home_of
        sources.claude_home_of = lambda s: ("default", str(home))
        try:
            waits = restart.background_waits({"id": sid, "cli": "claude", "started_at": NOW - 3600}, now=NOW)
        finally:
            sources.claude_home_of = real
        self.assertEqual([w["what"] for w in waits], ["still running"])
        self.assertEqual(set(waits[0]), {"ts", "what", "command"})

    def test_default_choices_and_steps(self):
        self.assertEqual(restart.default_choice(agent(state="busy")), "nudge")
        self.assertEqual(restart.default_choice(agent(state="background")), "nudge")
        for st in ("waiting", "idle", "stale", "blocked"):
            self.assertEqual(restart.default_choice(agent(state=st)), "quiet")
        m = {"wait_minutes": 15, "update": True, "agents": [agent(state="busy", choice="nudge"), agent(choice="quiet"),
                                                            agent(choice="skip")]}
        self.assertEqual(restart.steps(m), ["give 1 busy agent(s) up to 15 min to stop", "apt-get update + full-upgrade, snap refresh",
                                            "reboot", "resume 2 agent(s) (1 nudged, 1 quietly), 1 not resumed"])

    def test_launch_lines_keep_login_model_effort_and_name(self):
        real = sources.find_bin
        stand_ins.use(self)
        try:
            line = restart.launch_line(agent(config_dir=str(sources.DEFAULT_CLAUDE_HOME)))
            self.assertTrue(line.startswith("env %s -u CLAUDE_CONFIG_DIR /bin/claude --resume" % relaunch.SCRUB_ENV))
            self.assertIn("--model claude-opus-5-5 --effort xhigh --name alpha", line)
            work = restart.launch_line(agent(config_dir="/home/alex/.claude-work", name="weekly status report abcd1234"))
            self.assertTrue(work.startswith("env %s CLAUDE_CONFIG_DIR=/home/alex/.claude-work " % relaunch.SCRUB_ENV))
            self.assertIn("--name 'weekly status report abcd1234'", work)
            cx = restart.launch_line(agent(cli="codex", id="0190aaaa-0000-7000-8000-00000000a001", model="gpt-6-astra"))
            self.assertTrue(cx.startswith("env %s /bin/codex resume -s danger-full-access -a never -C" % relaunch.SCRUB_ENV))
            self.assertIn(" -c check_for_update_on_startup=false ", cx)
            self.assertTrue(cx.endswith("-m gpt-6-astra -c model_reasoning_effort=xhigh 0190aaaa-0000-7000-8000-00000000a001"))
        finally:
            sources.find_bin = real

    def test_codex_keeps_its_gate_only_where_agent_gate_trusts_the_hooks(self):
        """Codex skips a repo's unapproved hooks silently: a resume in a gated repo passes agent-gate's flag."""
        class Gate:
            @staticmethod
            def codex_hook_args(cwd):
                return ["--dangerously-bypass-hook-trust"] if cwd == "/w/gated" else []
        real_bin, real_mailroom = sources.find_bin, sources.mailroom
        stand_ins.use(self)
        sources.mailroom = lambda module: Gate
        try:
            gated = restart.launch_line(agent(cli="codex", id="01a0", cwd="/w/gated", model="gpt-6.1-sol"))
            self.assertIn(" -c check_for_update_on_startup=false --dangerously-bypass-hook-trust -m gpt-6.1-sol", gated)
            plain = restart.launch_line(agent(cli="codex", id="01a0", cwd="/w/plain", model="gpt-6.1-sol"))
            self.assertNotIn("bypass", plain)
            os.environ["AGENT_MAILROOM_DIR"] = "/nonexistent/mailroom"          # no mailroom checkout: no gate
            self.assertNotIn("bypass", restart.launch_line(agent(cli="codex", id="01a0", cwd="/w/gated")))
        finally:
            sources.find_bin, sources.mailroom = real_bin, real_mailroom

    def test_nudge_says_what_it_was_doing(self):
        t = restart.nudge_text(agent(state="background", waits=[{"what": "Wait for the pod", "command": "x"}],
                                     processes=[{"pid": 1, "cmd": "python3 train.py"}]), NOW, "alpha", " for system updates")
        self.assertIn("for system updates", t)
        self.assertIn("waiting on background work", t)
        self.assertIn("your last note: tests pass", t)
        self.assertIn("'Wait for the pod'", t)
        self.assertIn("python3 train.py", t)
        self.assertTrue(t.startswith("[relayed from Alex via agent-board] the desktop was restarted\n"))


class RunTest(unittest.TestCase):
    def test_start_run_validates_writes_and_launches(self):
        with StateDir():
            col = board(lambda: [running(), running("beta", "busy", "22222222-2222-3333-4444-555555555555")])
            self.assertFalse(restart.start_run(True, 99, {}, collector=col, inspect=INSPECTED)["ok"])
            gone = restart.start_run(True, 15, {"nobody": "quiet", "beta": "skip"}, dry_run=True, collector=col,
                                     inspect=INSPECTED)
            self.assertTrue(gone["ok"])                                  # an agent that stopped since doesn't block it
            self.assertEqual(gone["gone"], ["nobody"])
            self.assertFalse(restart.start_run(True, 15, {"alpha": "maybe"}, collector=col, inspect=INSPECTED)["ok"])
            dry = restart.start_run(True, 15, {"beta": "skip"}, dry_run=True, collector=col, inspect=INSPECTED)
            self.assertEqual([a["choice"] for a in dry["manifest"]["agents"]], ["skip", "quiet"])   # busy first, by state
            self.assertEqual(dry["manifest"]["agents"][0]["model"], "m")
            self.assertFalse(restart.MANIFEST.exists())
            launched = []
            res = restart.start_run(False, 15, {}, launcher=lambda: launched.append(1), collector=col, inspect=INSPECTED)
            self.assertTrue(res["ok"])
            self.assertEqual(launched, [1])
            self.assertEqual(json.loads(restart.MANIFEST.read_text())["status"], "pending")
            self.assertIn("already in progress", restart.start_run(False, 15, {}, collector=col, inspect=INSPECTED)["error"])

    def test_status_says_whether_a_restart_is_active_and_cancellable(self):
        with StateDir():
            self.assertEqual(restart.status(), {})
            restart.set_status("waiting", "waiting for 1 busy agent(s) to stop")
            self.assertEqual((restart.status()["active"], restart.status()["cancellable"]), (True, True))
            restart.set_status("updating", "apt-get update")
            self.assertEqual((restart.status()["active"], restart.status()["cancellable"]), (True, False))
            self.assertFalse(restart.cancel()["ok"])
            restart.write_json(restart.STATUS, {"status": "waiting", "message": "x", "updated": restart.now() - 7 * 3600})
            self.assertEqual((restart.status()["active"], restart.status()["cancellable"]), (False, False))   # it died
            self.assertTrue(restart.start_run(False, 0, {}, dry_run=True, collector=board(lambda: []), inspect=INSPECTED)["ok"])
            restart.set_status("done", "restored 1 of 1 agents")
            self.assertEqual((restart.status()["active"], restart.status()["cancellable"]), (False, False))

    def worker_manifest(self, update=True):
        m = {"version": 1, "created": NOW, "status": "pending", "update": update, "wait_minutes": 1,
             "agents": [agent(state="busy", choice="nudge"), agent(id="33333333-2222-3333-4444-555555555555", choice="quiet")]}
        restart.write_json(restart.MANIFEST, m)

    def test_worker_waits_updates_and_reboots(self):
        with StateDir() as sd:
            self.worker_manifest()
            polls = {"n": 0}

            def registry():                 # alpha's turn ends after the worker's second look
                polls["n"] += 1
                return [running(status="busy" if polls["n"] < 3 else "idle")]
            ran = []
            real = sources.find_bin
            sources.find_bin = lambda n: "/bin/true"     # agent-send would type into a live agent: true runs instead
            try:
                # run stands in for sudo apt-get, snap and systemctl reboot
                code = restart.worker(run=lambda cmd, timeout=0, env=None: (ran.append(cmd) or (0, "", "")),
                                      sleep=lambda s: None, collector=board(registry))
            finally:
                sources.find_bin = real
            self.assertEqual(code, 0)
            self.assertEqual([c[2] for c in ran], ["apt-get", "env", "snap", "systemctl"])
            self.assertEqual(ran[-1], ["sudo", "-n", "systemctl", "reboot"])
            self.assertEqual(json.loads(restart.MANIFEST.read_text())["status"], "rebooting")
            self.assertTrue(any("restarting now" in s.lower() for s, _ in sd.mails))

    def test_a_heads_up_that_cannot_be_sent_is_logged_and_the_restart_goes_on(self):
        with StateDir():
            self.worker_manifest()
            polls = {"n": 0}

            def registry():                 # alpha's turn ends after the worker's second look
                polls["n"] += 1
                return [running(status="busy" if polls["n"] < 3 else "idle")]
            real = sources.find_bin
            sources.find_bin = lambda n: None if n == "agent-send" else "/bin/true"      # agent-send not installed
            try:
                # run stands in for sudo apt-get, snap and systemctl reboot
                code = restart.worker(run=lambda cmd, timeout=0, env=None: (0, "", ""),
                                      sleep=lambda s: None, collector=board(registry))
            finally:
                sources.find_bin = real
            self.assertEqual(code, 0)
            self.assertIn("heads-up to alpha: FAILED (127): agent-send is not installed", restart.LOG.read_text())

    def test_a_restored_agent_whose_nudge_failed_says_so(self):
        d = Path(tempfile.mkdtemp())
        # stand-ins: the real tmux server runs the machine's agents, and agent-send would type into one of them
        for name, script in (("tmux", "exit 0"),             # no pane pids: the memory cap is never asked to move one
                             ("agent-send", 'echo "no such session"; exit 1')):
            (d / name).write_text("#!/bin/sh\n" + script + "\n")
            (d / name).chmod(0o755)
        checks = {"n": 0}

        def live(a):            # the live session registry: not running yet, then up once launched
            checks["n"] += 1
            return checks["n"] > 1
        saved = (sources.find_bin, sources.is_live)
        sources.find_bin = lambda name: str(d / name) if (d / name).exists() else None
        sources.is_live = live
        try:
            res = restart.restore_one(agent(cwd=str(d), choice="nudge"), "an update")
        finally:
            sources.find_bin, sources.is_live = saved
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"], "resumed; nudge FAILED (no such session)")

    def test_a_restored_codex_agent_says_its_hooks_were_left_untrusted(self):
        d = Path(tempfile.mkdtemp())
        # a stand-in for tmux: the real server runs the machine's agents. Its pane shows Codex's hook review until a key is
        # typed into it (the launch line is typed before that), then Codex's prompt
        (d / "screen").write_text(fixtures.CODEX_HOOK_REVIEW)
        (d / "tmux").write_text('#!/bin/sh\ncd "$(dirname "$0")"\ncase "$1" in\n'
                                'send-keys) [ -e typed ] && [ "$4" = "-l" ] && touch answered; [ "$4" = "-l" ] && touch typed;;\n'
                                'capture-pane) if [ -e answered ]; then echo "› Ask Codex"; else cat screen; fi;;\n'
                                'esac\nexit 0\n')
        (d / "tmux").chmod(0o755)
        saved = (sources.find_bin, sources.is_live)
        sources.find_bin = lambda name: str(d / name) if (d / name).exists() else None
        sources.is_live = lambda a: (d / "answered").exists()   # the live session registry: up once answered
        try:
            res = restart.restore_one(agent(cli="codex", cwd=str(d)), "")
        finally:
            sources.find_bin, sources.is_live = saved
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"], "resumed quietly; Codex: 1 hook is new or changed, left untrusted (review in "
                                        "Codex's /hooks)")

    def test_worker_aborts_on_apt_failure_and_on_cancel(self):
        with StateDir() as sd:
            self.worker_manifest()
            ran = []
            # run stands in for sudo apt-get, snap and systemctl reboot: here apt-get fails
            code = restart.worker(run=lambda cmd, timeout=0, env=None: (ran.append(cmd) or (100, "", "E: broken")),
                                  sleep=lambda s: None, collector=board(lambda: [running()]))
            self.assertEqual((code, len(ran)), (1, 1))                       # apt-get update only: no reboot
            self.assertEqual(restart.status()["status"], "failed")
            self.assertTrue(any("stopped" in s for s, _ in sd.mails))
        with StateDir():
            self.worker_manifest()
            restart.CANCEL.touch()
            ran = []
            restart.worker(run=lambda cmd, timeout=0, env=None: (ran.append(cmd) or (0, "", "")),     # apt, snap, reboot
                           sleep=lambda s: None, collector=board(lambda: [running()]))
            self.assertEqual(ran, [])
            self.assertEqual(restart.status()["status"], "cancelled")

    def test_a_failed_snap_refresh_is_reported_and_the_reboot_goes_ahead(self):
        with StateDir() as sd:
            self.worker_manifest()
            ran = []

            def run(cmd, timeout=0, env=None):      # sudo apt-get, snap and systemctl reboot: snap fails
                ran.append(cmd)
                return (1, "", 'error: cannot refresh "firefox": snap "firefox" has running apps') if "snap" in cmd \
                    else (0, "", "")
            self.assertEqual(restart.worker(run=run, sleep=lambda s: None, collector=board(lambda: [running()])), 0)
            self.assertEqual(ran[-1], ["sudo", "-n", "systemctl", "reboot"])
            self.assertIn("snap refresh failed (exit 1)", restart.status()["message"])
            body = dict(sd.mails)["Desktop restarting now"]
            self.assertIn('snap refresh failed (exit 1): error: cannot refresh "firefox"', body)

    def test_a_failed_reboot_command_fails_the_restart_and_mails(self):
        with StateDir() as sd:
            self.worker_manifest(update=False)

            def run(cmd, timeout=0, env=None):      # sudo apt-get, snap and systemctl reboot: the reboot fails
                return (1, "", "sudo: a password is required") if "reboot" in cmd else (0, "", "")
            self.assertEqual(restart.worker(run=run, sleep=lambda s: None, collector=board(lambda: [running()])), 1)
            st = restart.status()
            self.assertEqual(st["status"], "failed")
            self.assertIn("reboot command failed (exit 1): sudo: a password is required", st["message"])
            self.assertEqual(json.loads(restart.MANIFEST.read_text())["status"], "failed")   # no later boot acts on it
            subject, body = sd.mails[-1]
            self.assertIn("failed", subject)
            self.assertIn("sudo: a password is required", body)

    def test_cancel_is_logged_and_audited(self):
        saved = actions.AUDIT
        with StateDir() as sd:
            actions.AUDIT = sd.d / "actions.jsonl"
            try:
                self.assertFalse(actions.restart_action({"action": "restart_cancel"})["ok"])     # nothing to cancel
                restart.set_status("waiting", "waiting for 1 busy agent(s) to stop")
                self.assertTrue(actions.restart_action({"action": "restart_cancel"})["ok"])
                self.assertTrue(restart.CANCEL.exists())
                self.assertIn("cancel requested", restart.LOG.read_text())
                self.assertEqual([(e["action"], e["ok"]) for e in sources.read_jsonl(actions.AUDIT)],
                                 [("restart_cancel", False), ("restart_cancel", True)])
            finally:
                actions.AUDIT = saved


class MailTest(unittest.TestCase):
    def test_mail_goes_to_the_configured_owner_only(self):
        sent = []
        real = (sources.find_bin, sources.run, sources.MAIL_CONFIG, restart.STATE, restart.LOG)
        d = Path(tempfile.mkdtemp())
        restart.STATE, restart.LOG = d, d / "run.log"
        sources.find_bin = lambda n: "/bin/" + n
        # agent-mail would send real mail: a stand-in records what it was given
        sources.run = lambda cmd, **kw: (sent.append((cmd, kw.get("input"))), (0, "", ""))[1]
        try:
            restart.mail("Desktop is back", "all fine")
            self.assertEqual(sent[0][0], ["/bin/agent-mail", "send", "--unrouted", "--to", "owner@example.com",
                                          "--subject", "Desktop is back"])
            sources.MAIL_CONFIG = d / "missing.json"            # no owner configured: nothing is sent, it is logged
            restart.mail("x", "y")
            self.assertEqual(len(sent), 1)
            self.assertIn("mail 'x': NOT SENT: no owner address in the mailroom config", restart.LOG.read_text())
        finally:
            sources.find_bin, sources.run, sources.MAIL_CONFIG, restart.STATE, restart.LOG = real


class RestoreTest(unittest.TestCase):
    """restore_one starts real tmux sessions and wait_for_network resolves a real host: each test's `resume` and
    `network` answer for them."""

    def test_restore_once_codex_last_and_summary(self):
        with StateDir() as sd:
            m = {"status": "rebooting", "rebooting_at": NOW, "update": True, "agents": [
                agent(cli="codex", id="0190aaaa-0000-7000-8000-00000000a001", display="nova", choice="quiet"),
                agent(display="alpha", choice="nudge"),
                agent(id="44444444-2222-3333-4444-555555555555", display="gamma", choice="skip")]}
            restart.write_json(restart.MANIFEST, m)
            order = []

            def resume(a):
                order.append(a["display"])
                if a["choice"] == "skip":
                    return {"id": a["id"], "name": a["display"], "result": "not resumed (your choice)"}
                return {"id": a["id"], "name": a["display"], "ok": True, "choice": a["choice"], "result": "resumed"}
            self.assertEqual(restart.restore(force=True, resume=resume, parallel=1), 0)
            self.assertEqual(order[-1], "nova")                                     # Codex after every Claude
            done = json.loads(restart.MANIFEST.read_text())
            self.assertEqual(done["status"], "done")
            self.assertIn("2 of 3", sd.mails[-1][0])
            self.assertEqual(restart.restore(force=True, resume=resume), 0)          # once only
            self.assertEqual(len(order), 3)
            act = restart.restored_activity()
            self.assertEqual(list(act), ["0190aaaa-0000-7000-8000-00000000a001"])    # quiet ones keep their colour

    def test_a_test_cycle_sends_no_mail_and_shows_no_chip(self):
        with StateDir() as sd:
            restart.write_json(restart.MANIFEST, {"status": "rebooting", "rebooting_at": NOW, "test": True,
                                                  "agents": [agent(display="alpha", choice="quiet")]})
            ok = lambda a: {"id": a["id"], "name": a["display"], "ok": True, "choice": "quiet", "result": "resumed"}
            restart.restore(force=True, resume=ok)
            self.assertEqual(sd.mails, [])
            self.assertTrue(restart.status()["test"])

    def test_restore_waits_for_its_own_boot(self):
        with StateDir():
            restart.write_json(restart.MANIFEST, {"status": "rebooting", "rebooting_at": restart.now() + 3600, "agents": []})
            called = []
            restart.restore(resume=lambda a: called.append(a))
            self.assertEqual(json.loads(restart.MANIFEST.read_text())["status"], "rebooting")
            self.assertIn("predates the restart request", restart.LOG.read_text())

    def test_a_damaged_manifest_stops_the_restore_loudly(self):
        with StateDir():
            restart.MANIFEST.write_text("{half a file")
            with self.assertRaisesRegex(ValueError, "manifest.json is not valid JSON"):
                restart.restore(force=True, resume=lambda a: None)
            self.assertEqual(restart.MANIFEST.read_text(), "{half a file")

    def test_restore_without_a_boot_time_says_so_and_fails(self):
        with StateDir():
            restart.write_json(restart.MANIFEST, {"status": "rebooting", "rebooting_at": NOW, "agents": []})
            real = sources.boot_time
            sources.boot_time = lambda: None      # this machine's /proc/stat has btime: one without it stands in
            try:
                self.assertEqual(restart.restore(resume=lambda a: None, network=lambda timeout, sleep: True), 1)
            finally:
                sources.boot_time = real
            self.assertEqual(json.loads(restart.MANIFEST.read_text())["status"], "rebooting")
            self.assertIn("this boot's time is unknown", restart.LOG.read_text())

    def test_a_damaged_status_is_a_problem_on_the_board(self):
        with StateDir():
            restart.STATUS.write_text("{half a file")
            with self.assertRaisesRegex(ValueError, "status.json is not valid JSON"):
                restart.status()
            snap = fixtures.collector(tempfile.mkdtemp(), restart=restart).snapshot(NOW)
            self.assertNotIn("restart", snap)
            self.assertIn("status.json is not valid JSON",
                          next(p["error"] for p in snap["problems"] if p["source"] == "restart"))

    def test_restore_says_when_the_network_never_came_up(self):
        with StateDir() as sd:
            # rebooting_at 0: any boot comes after the request, so restore acts without force
            restart.write_json(restart.MANIFEST, {"status": "rebooting", "rebooting_at": 0,
                                                  "agents": [agent(display="alpha", choice="quiet")]})
            ok = lambda a: {"id": a["id"], "name": a["display"], "ok": True, "choice": "quiet", "result": "resumed"}
            # restore_one starts real tmux sessions and wait_for_network resolves a real host: both stand in here
            restart.restore(resume=ok, network=lambda timeout, sleep: False)
            self.assertIn("network not up after 180 s; restoring anyway", restart.LOG.read_text())
            self.assertIn("network not up after 180 s; restoring anyway", sd.mails[-1][1])


class BoardTest(unittest.TestCase):
    def test_quiet_agents_keep_their_colour_after_a_restore(self):
        d = Path(tempfile.mkdtemp())
        log = d / "events.jsonl"
        sid = "11111111-2222-3333-4444-555555555555"
        at = NOW - 600
        log.write_text("".join(json.dumps(e) + "\n" for e in (
            {"ts": NOW - 30000, "event": "idle", "session_id": sid, "cli": "claude", "name": "alpha"},
            {"ts": at + 20, "event": "start", "session_id": sid, "cli": "claude"})))
        col = fixtures.collector(d, claude=lambda: [fixtures.session(sid, "alpha", at + 20, cwd=str(d), tmux="alpha",
                                                                     started_at=at)],
                                 restored=restart.restored_activity)
        with StateDir():           # the restore resumed alpha quietly 10 minutes ago, idle since NOW - 30000 before it
            restart.write_json(restart.MANIFEST, {"status": "done", "restored_at": at,
                                                  "agents": [agent(last_activity=NOW - 30000)],
                                                  "results": [{"id": sid, "ok": True, "choice": "quiet"}]})
            a = col.agents(NOW)[0][0]
            self.assertEqual(a["state"], "stale")              # resumed 10 min ago, but grey before: still grey
            # what it does in the first minutes after the restore is activity (a reply came in; it acted and closed)
            with open(log, "a") as fh:
                fh.write(json.dumps({"ts": at + 60, "event": "note", "session_id": sid, "cli": "claude",
                                     "text": "landed"}) + "\n")
            self.assertEqual(col.agents(NOW)[0][0]["last_activity"], at + 60)

    def test_actions_validate_restart_requests(self):
        bad = ({"action": "restart_run", "update": "yes"}, {"action": "restart_run", "update": True, "wait_minutes": "15"},
               {"action": "restart_run", "update": True, "choices": {"alpha": "quiet"}},
               {"action": "restart_run", "update": True, "choices": {"11111111-2222-3333-4444-555555555555": "maybe"}})
        for req in bad:
            self.assertFalse(actions.restart_action(req)["ok"], req)


if __name__ == "__main__":
    unittest.main()
