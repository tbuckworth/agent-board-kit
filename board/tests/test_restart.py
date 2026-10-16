"""The desktop restart engine with fakes: python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _kitenv  # noqa: E402,F401  (scratch config with an example owner, before agentboard reads any)
from agentboard import actions, collect, config, restart, sources  # noqa: E402

NOW = 1_800_000_000.0


def agent(**kw):
    a = {"id": "11111111-2222-3333-4444-555555555555", "cli": "claude", "name": "alpha", "display": "alpha",
         "state": "idle", "reason": "", "tmux": "alpha", "cwd": tempfile.gettempdir(), "kind": "interactive",
         "config_dir": "", "account": "personal", "model": "claude-opus-5-5", "effort": "xhigh", "last_activity": NOW - 60,
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
        restart.mail = lambda subject, body: self.mails.append((subject, body))
        return self

    def __exit__(self, *exc):
        restart.STATE, restart.MANIFEST, restart.STATUS, restart.LOG, restart.CANCEL, restart.mail = self.saved


class FakeCollector:
    def __init__(self, rows):
        self.rows = rows

    def agents(self, with_git=True):
        return self.rows, {}, {}


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
            u = restart.updates_info(run)
            self.assertEqual((u["total"], u["security"], u["gpu_ok"], u["gpu_message"]), (3, 1, None, ""))
            self.assertNotIn("nvidia-smi", calls)
            sources.find_bin = lambda n: "/usr/bin/" + n
            broken = restart.updates_info(lambda cmd, timeout=0, env=None:
                                          (9, "", "NVIDIA-SMI has failed\n") if "nvidia-smi" in cmd[0] else (0, "", ""))
            self.assertEqual((broken["gpu_ok"], broken["gpu_message"]), (False, "NVIDIA-SMI has failed"))
        finally:
            sources.find_bin = real

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
        sources.find_bin = lambda n: "/bin/" + n
        try:
            line = restart.launch_line(agent(config_dir=str(sources.DEFAULT_CLAUDE_HOME)))
            self.assertTrue(line.startswith("env %s -u CLAUDE_CONFIG_DIR /bin/claude --resume" % actions.SCRUB_ENV))
            self.assertIn("--model claude-opus-5-5 --effort xhigh --name alpha", line)
            work = restart.launch_line(agent(config_dir="/home/t/.claude-work", name="weekly status report abcd1234"))
            self.assertTrue(work.startswith("env %s CLAUDE_CONFIG_DIR=/home/t/.claude-work " % actions.SCRUB_ENV))
            self.assertIn("--name 'weekly status report abcd1234'", work)
            cx = restart.launch_line(agent(cli="codex", id="0190aaaa-0000-7000-8000-00000000a001", model="gpt-6-astra"))
            self.assertTrue(cx.startswith("env %s /bin/codex resume -s danger-full-access -a never -C" % actions.SCRUB_ENV))
            self.assertTrue(cx.endswith("-m gpt-6-astra -c model_reasoning_effort=xhigh 0190aaaa-0000-7000-8000-00000000a001"))
        finally:
            sources.find_bin = real

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
        rows = [agent(), agent(id="22222222-2222-3333-4444-555555555555", name="beta", display="beta", state="busy")]
        real = (restart.model_effort, restart.background_waits, restart.agent_processes)
        restart.model_effort = lambda a: ("m", "e")
        restart.background_waits = lambda a: []
        restart.agent_processes = lambda a: []
        try:
            with StateDir():
                col = FakeCollector(rows)
                self.assertFalse(restart.start_run(True, 99, {}, collector=col)["ok"])
                gone = restart.start_run(True, 15, {"nobody": "quiet", "beta": "skip"}, dry_run=True, collector=col)
                self.assertTrue(gone["ok"])                                  # an agent that stopped since doesn't block it
                self.assertEqual(gone["gone"], ["nobody"])
                self.assertFalse(restart.start_run(True, 15, {"alpha": "maybe"}, collector=col)["ok"])
                dry = restart.start_run(True, 15, {"beta": "skip"}, dry_run=True, collector=col)
                self.assertEqual([a["choice"] for a in dry["manifest"]["agents"]], ["skip", "quiet"])   # busy first, by state
                self.assertFalse(restart.MANIFEST.exists())
                launched = []
                res = restart.start_run(False, 15, {}, launcher=lambda: launched.append(1), collector=col)
                self.assertTrue(res["ok"])
                self.assertEqual(launched, [1])
                self.assertEqual(json.loads(restart.MANIFEST.read_text())["status"], "pending")
                self.assertIn("already in progress", restart.start_run(False, 15, {}, collector=col)["error"])
        finally:
            restart.model_effort, restart.background_waits, restart.agent_processes = real

    def worker_manifest(self, update=True):
        m = {"version": 1, "created": NOW, "status": "pending", "update": update, "wait_minutes": 1,
             "agents": [agent(state="busy", choice="nudge"), agent(id="33333333-2222-3333-4444-555555555555", choice="quiet")]}
        restart.write_json(restart.MANIFEST, m)

    def test_worker_waits_updates_and_reboots(self):
        with StateDir() as sd:
            self.worker_manifest()
            polls = {"n": 0}

            class Col:
                def agents(self, with_git=True):
                    polls["n"] += 1
                    return [agent(state="busy" if polls["n"] < 3 else "idle")], {}, {}
            ran, reboots = [], []
            real = sources.find_bin
            sources.find_bin = lambda n: "/bin/true"
            try:
                code = restart.worker(reboot=lambda: reboots.append(1) or 0, run=lambda cmd, timeout=0, env=None: (ran.append(cmd) or (0, "", "")),
                                      sleep=lambda s: None, collector=Col())
            finally:
                sources.find_bin = real
            self.assertEqual(code, 0)
            self.assertEqual(reboots, [1])
            self.assertEqual([c[2] if c[0] == "sudo" else c for c in ran][:3], ["apt-get", "env", "snap"])
            self.assertEqual(json.loads(restart.MANIFEST.read_text())["status"], "rebooting")
            self.assertTrue(any("restarting now" in s.lower() for s, _ in sd.mails))

    def test_worker_aborts_on_apt_failure_and_on_cancel(self):
        with StateDir() as sd:
            self.worker_manifest()
            reboots = []
            code = restart.worker(reboot=lambda: reboots.append(1), run=lambda cmd, timeout=0, env=None: (100, "", "E: broken"),
                                  sleep=lambda s: None, collector=FakeCollector([agent()]))
            self.assertEqual((code, reboots), (1, []))
            self.assertEqual(restart.status()["status"], "failed")
            self.assertTrue(any("stopped" in s for s, _ in sd.mails))
        with StateDir():
            self.worker_manifest()
            restart.CANCEL.touch()
            reboots = []
            restart.worker(reboot=lambda: reboots.append(1), run=lambda cmd, timeout=0, env=None: (0, "", ""),
                           sleep=lambda s: None, collector=FakeCollector([agent()]))
            self.assertEqual(reboots, [])
            self.assertEqual(restart.status()["status"], "cancelled")


class MailTest(unittest.TestCase):
    def test_mail_goes_to_the_configured_owner_only(self):
        sent = []
        real = (sources.find_bin, restart.subprocess.run, restart.STATE, restart.LOG)
        d = Path(tempfile.mkdtemp())
        restart.STATE, restart.LOG = d, d / "run.log"
        sources.find_bin = lambda n: "/bin/" + n
        restart.subprocess.run = lambda cmd, **kw: sent.append((cmd, kw.get("input")))
        try:
            restart.mail("Desktop is back", "all fine")
            self.assertEqual(sent[0][0], ["/bin/agent-mail", "send", "--unrouted", "--to", "owner@example.com",
                                          "--subject", "Desktop is back"])
            real_cfg = config.MAIL_CONFIG
            config.MAIL_CONFIG = d / "missing.json"            # no owner configured: nothing is sent, it is logged
            try:
                restart.mail("x", "y")
            finally:
                config.MAIL_CONFIG = real_cfg
            self.assertEqual(len(sent), 1)
            self.assertIn("no owner_email", restart.LOG.read_text())
        finally:
            sources.find_bin, restart.subprocess.run, restart.STATE, restart.LOG = real


class RestoreTest(unittest.TestCase):
    def test_restore_once_codex_last_and_summary(self):
        with StateDir() as sd:
            m = {"status": "rebooting", "rebooting_at": NOW, "update": True, "agents": [
                agent(cli="codex", id="0190aaaa-0000-7000-8000-00000000a001", display="astra", choice="quiet"),
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
            self.assertEqual(order[-1], "astra")                                     # Codex after every Claude
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


class BoardTest(unittest.TestCase):
    def test_quiet_agents_keep_their_colour_after_a_restore(self):
        d = Path(tempfile.mkdtemp())
        log = d / "events.jsonl"
        sid = "11111111-2222-3333-4444-555555555555"
        at = NOW - 600
        log.write_text("".join(json.dumps(e) + "\n" for e in (
            {"ts": NOW - 30000, "event": "idle", "session_id": sid, "cli": "claude", "name": "alpha"},
            {"ts": at + 20, "event": "start", "session_id": sid, "cli": "claude"})))
        claude = lambda: [{"cli": "claude", "id": sid, "name": "alpha", "cwd": str(d), "raw_status": "idle", "kind": "interactive",
                           "live": True, "pid": None, "tmux": "alpha", "waiting_for": "", "started_at": at, "status_at": at + 20}]
        col = collect.Collector(events=sources.EventLog(log), codex=sources.CodexReader(d / "none"),
                                mail=sources.MailStore(d / "none.sqlite", owners=[]), git=sources.GitCache(fetch=lambda c: {}),
                                claude=claude, panes=lambda: {}, restored=lambda: {sid: (at, NOW - 30000)})
        col.sessions_fn = lambda: {}
        a = col.agents(NOW)[0][0]
        self.assertEqual(a["state"], "stale")              # resumed 10 min ago, but grey before: still grey

    def test_actions_validate_restart_requests(self):
        bad = ({"action": "restart_run", "update": "yes"}, {"action": "restart_run", "update": True, "wait_minutes": "15"},
               {"action": "restart_run", "update": True, "choices": {"alpha": "quiet"}},
               {"action": "restart_run", "update": True, "choices": {"11111111-2222-3333-4444-555555555555": "maybe"}})
        for req in bad:
            self.assertFalse(actions.restart_action(req)["ok"], req)


if __name__ == "__main__":
    unittest.main()
