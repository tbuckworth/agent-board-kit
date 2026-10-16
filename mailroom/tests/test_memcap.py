import contextlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentmail import memcap  # noqa: E402

G = 1 << 30


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROOT / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Sizes(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(memcap.parse_bytes("32G"), 32 * G)
        self.assertEqual(memcap.parse_bytes("512M"), 512 << 20)
        self.assertEqual(memcap.parse_bytes("1.5GiB"), int(1.5 * G))
        self.assertEqual(memcap.parse_bytes("2048"), 2048)
        self.assertEqual(memcap.parse_bytes("0"), 0)
        for off in (None, "", "off", "infinity", "None"):
            self.assertIsNone(memcap.parse_bytes(off))
        with self.assertRaises(ValueError):
            memcap.parse_bytes("lots")

    def test_limits_and_overrides(self):
        cfg = {"agent_memory_max": "32G", "agent_swap_max": "2G"}
        self.assertEqual(memcap.limits(cfg), {"MemoryMax": 32 * G, "MemorySwapMax": 2 * G})
        self.assertEqual(memcap.limits(cfg, "48G"), {"MemoryMax": 48 * G, "MemorySwapMax": 2 * G})
        self.assertEqual(memcap.limits(cfg, "off", "0"), {"MemorySwapMax": 0})
        self.assertEqual(memcap.limits({}), {})
        with self.assertRaises(ValueError):
            memcap.limits(cfg, "0")  # would kill every agent at once
        self.assertEqual(memcap.describe(memcap.limits(cfg)), "MemoryMax=32G, MemorySwapMax=2G")
        self.assertEqual(memcap.describe({}), "no cap")

    def test_unit_names_are_valid(self):
        self.assertEqual(memcap.unit_name("agent", "rm-280 slate/star"), "agent-rm-280-slate-star.scope")
        self.assertTrue(len(memcap.unit_name("agent", "x" * 500)) < 200)


class Scopes(unittest.TestCase):
    def test_start_scope_call_and_name_collision(self):
        calls = []

        def fake(*args):
            calls.append(args)
            return (len(calls) > 1, "" if len(calls) > 1 else "Unit agent-a.scope was already loaded")

        unit, msg = memcap.start_scope("agent-a.scope", [11, 12], {"MemoryMax": 32 * G}, "agent a", call=fake)
        self.assertRegex(unit, r"^agent-a-\d+\.scope$")
        self.assertIn("MemoryMax=32G", msg)
        args = [str(a) for a in calls[0]]
        self.assertEqual(args[:5], ["StartTransientUnit", "ssa(sv)a(sa(sv))", "agent-a.scope", "fail", "5"])
        self.assertIn("PIDs au 2 11 12", " ".join(args))
        self.assertIn("MemoryMax t %d" % (32 * G), " ".join(args))
        self.assertIn("Delegate b true", " ".join(args))

    def test_start_scope_failure_is_reported_not_raised(self):
        unit, msg = memcap.start_scope("agent-a.scope", [1], {}, "a", call=lambda *a: (False, "Access denied"))
        self.assertIsNone(unit)
        self.assertIn("uncapped", msg)
        self.assertIn("Access denied", msg)

    def test_adopt_without_bus_is_a_noop(self):
        with mock.patch.object(memcap, "available", return_value=False):
            self.assertEqual(memcap.adopt(os.getpid(), "x", {}, "x")[0], None)

    def test_free_from_service_only_moves_service_members(self):
        svc = "/u/app.slice/agent-mailroom.service"
        groups = {10: svc, 11: svc, 12: "/u/app.slice/tmux-spawn-1.scope", 13: svc}
        started, attached = [], []
        with mock.patch.object(memcap, "available", return_value=True), \
                mock.patch.object(memcap, "cgroup", side_effect=groups.get), \
                mock.patch.object(memcap, "tree", return_value=[10, 11, 12, 13]), \
                mock.patch.object(memcap, "wait_moved", return_value=True), \
                mock.patch.object(memcap, "attach", side_effect=lambda u, p: attached.append((u, p)) or (True, "")), \
                mock.patch.object(memcap, "start_scope",
                                  side_effect=lambda u, p, *a: started.append((u, p)) or (u, "")):
            unit, how = memcap.free_from_service(10, "tmux-server", "tmux server", exclude=[13])
            # only the server in the first call; then its service-cgroup descendants, never the agent being adopted
            self.assertEqual(started, [("tmux-server.scope", [10])])
            self.assertEqual(attached, [("tmux-server.scope", [11])])
            self.assertIn("moved out of agent-mailroom.service", how)
            groups[10] = "/u/app.slice/tmux-server.scope"
            self.assertIsNone(memcap.free_from_service(10, "tmux-server", "tmux server")[0])
            self.assertEqual(len(started), 1)

    def test_a_shared_process_started_inside_an_agent_scope_is_freed(self):
        agent = "/u/app.slice/agent-x.scope"
        groups = {10: agent, 11: agent, 12: "/u/app.slice/agent-y.scope"}
        started = []
        with mock.patch.object(memcap, "available", return_value=True), \
                mock.patch.object(memcap, "cgroup", side_effect=groups.get), \
                mock.patch.object(memcap, "tree", return_value=[10, 11, 12]), \
                mock.patch.object(memcap, "wait_moved", return_value=True), \
                mock.patch.object(memcap, "attach", return_value=(True, "")), \
                mock.patch.object(memcap, "start_scope", side_effect=lambda u, p, *a: started.append((u, p)) or (u, "")):
            unit, how = memcap.free_from_service(10, "tmux-server", "tmux server")
        self.assertEqual(started, [("tmux-server.scope", [10])])
        self.assertIn("moved out of agent-x.scope", how)
        self.assertTrue(memcap.borrowed_home("/u/app.slice/agent-mailroom.service"))
        self.assertTrue(memcap.borrowed_home("/u/app.slice/agent-rm-280.scope"))
        self.assertFalse(memcap.borrowed_home("/u/app.slice/tmux-server.scope"))
        self.assertFalse(memcap.borrowed_home("/u/app.slice/tmux-spawn-1.scope"))
        self.assertFalse(memcap.borrowed_home(None))

    def test_adopt_starts_with_the_root_only_then_sweeps(self):
        # systemd rejects StartTransientUnit outright if any listed PID has exited, so only the root goes in it
        groups = {20: "/u/tmux-spawn-1.scope", 21: "/u/tmux-spawn-1.scope", 22: "/u/tmux-spawn-1.scope"}
        started, swept = [], []
        def start(u, pids, *a):
            started.append(pids)
            groups[20] = "/u/" + u
            return u, u
        with mock.patch.object(memcap, "available", return_value=True), \
                mock.patch.object(memcap, "cgroup", side_effect=groups.get), \
                mock.patch.object(memcap, "tree", return_value=[20, 21, 22]), \
                mock.patch.object(memcap, "start_scope", side_effect=start), \
                mock.patch.object(memcap, "attach", side_effect=lambda u, p, **k: swept.append(p) or (True, "")):
            unit, msg = memcap.adopt(20, "a", {}, "a", settle=0)
        self.assertEqual(unit, "agent-a.scope")
        self.assertEqual(started, [[20]])
        self.assertEqual(swept, [[21, 22]])

    def test_attach_survives_a_pid_that_exited(self):
        visible = {31, 32, 33}            # 34 is already gone: filtered out before any call
        calls = []

        def fake(*args):
            calls.append(args[5:])
            if len(calls) == 1:
                visible.discard(32)       # 32 exits between the filter and the call: systemd refuses the batch
            return all(p in visible for p in args[5:]), "No such process"

        with mock.patch.object(memcap, "cgroup", side_effect=lambda p: "/u/x.scope" if p in visible else None):
            ok, msg = memcap.attach("agent-a.scope", [31, 32, 33, 34], call=fake)
        self.assertEqual(calls, [(31, 32, 33), (31,), (32,), (33,)])   # batch, then each alone
        self.assertTrue(ok, msg)                                       # 32 is gone: no loss

    def test_attach_reports_a_live_pid_it_could_not_move(self):
        with mock.patch.object(memcap, "cgroup", return_value="/u/x.scope"):
            ok, msg = memcap.attach("agent-a.scope", [41, 42],
                                    call=lambda *a: (a[5:] == (41,), "Access denied"))
        self.assertFalse(ok)
        self.assertIn("42: Access denied", msg)

class BackgroundSession(unittest.TestCase):
    def test_finds_the_top_process_of_a_resumed_session(self):
        sid = str(uuid.uuid4())
        code = ("import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', "
                "'--resume', %r]); time.sleep(30)" % sid)
        p = subprocess.Popen([sys.executable, "-c", code, "--resume", sid])
        try:
            self.assertEqual(memcap.find_bg_session(sid, timeout=5), p.pid)
            self.assertIsNone(memcap.find_bg_session(str(uuid.uuid4()), timeout=0))
        finally:
            for pid in memcap.tree(p.pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, 9)
            p.wait()


    def test_finds_a_session_that_claimed_a_spare(self):
        # The daemon hands resumes to pre-started spares: no --resume ID anywhere, only the registry's pid.
        code = ("import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', "
                "'bg-spare']); time.sleep(30)")
        host = subprocess.Popen([sys.executable, "-c", code, "bg-pty-host", "--bg-pty-host", "/x/spare/1.pty.sock"])
        try:
            for _ in range(100):
                kids = [p for p in memcap.tree(host.pid) if p != host.pid]
                if kids:
                    break
                time.sleep(0.05)
            sid = str(uuid.uuid4())
            self.assertEqual(memcap.find_bg_session(sid, timeout=2, session_pid=lambda: kids[0]), host.pid)
            self.assertIsNone(memcap.find_bg_session(sid, timeout=0, session_pid=lambda: None))
        finally:
            for pid in memcap.tree(host.pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, 9)
            host.wait()

    def test_bg_daemon_is_freed_before_the_session_is_adopted(self):
        events = []
        with mock.patch.object(memcap, "available", return_value=True), \
                mock.patch.object(memcap, "find_bg_session", return_value=50), \
                mock.patch.object(memcap, "parents", return_value={50: 40}), \
                mock.patch.object(memcap, "is_claude_daemon", return_value=True), \
                mock.patch.object(memcap, "tree", return_value=[50, 51]), \
                mock.patch.object(memcap, "free_from_service",
                                  side_effect=lambda pid, *a, **k: events.append(("free", pid, k["exclude"]))
                                  or ("claude-bg-daemon.scope", "moved out of x.service")), \
                mock.patch.object(memcap, "adopt", side_effect=lambda pid, *a, **k: events.append(("adopt", pid))
                                  or ("agent-s.scope", "agent-s.scope (MemoryMax=1G)")):
            msg = memcap.cap_bg_session("sid", "s", {"MemoryMax": G})
        self.assertEqual(events, [("free", 40, [50, 51]), ("adopt", 50)])
        self.assertEqual(msg, "agent-s.scope (MemoryMax=1G); daemon moved out of x.service")


@unittest.skipUnless(os.environ.get("MEMCAP_REAL_TESTS") == "1" and memcap.available(),
                     "opt-in: set MEMCAP_REAL_TESTS=1 to run against the real systemd user manager")
class RealScope(unittest.TestCase):
    """Against the real user manager: the cap is applied and enforced. Opt-in, since it starts real scopes."""

    def test_runaway_is_killed_inside_its_scope(self):
        hog = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.readline(); b = bytearray(400 << 20); "
                                "b[::4096] = b'x' * len(b[::4096]); print('survived', flush=True)"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        name = "memcap-test-%d" % os.getpid()
        try:
            unit, msg = memcap.adopt(hog.pid, name, {"MemoryMax": 100 << 20, "MemorySwapMax": 0}, "memcap test",
                                     settle=0)
            self.assertTrue(unit, msg)
            self.assertTrue(memcap.cgroup(hog.pid).endswith("/" + unit))
            out, _ = hog.communicate("go\n", timeout=60)
            self.assertEqual(hog.returncode, -9, out)  # OOM-killed in its scope; this test process lives on
        finally:
            if hog.poll() is None:
                hog.kill()
                hog.wait()

    def test_children_are_swept_in_and_a_dead_pid_cannot_spoil_the_start(self):
        # What the root-only start avoids: one exited PID in the list fails the whole StartTransientUnit.
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            unit, msg = memcap.start_scope(memcap.unit_name("agent", "memcap-dead-%d" % os.getpid()),
                                           [live.pid, dead.pid], {}, "memcap test (must fail)")
            self.assertIsNone(unit, "systemd accepted a dead PID; the root-only start is no longer needed")
        finally:
            live.kill()
            live.wait()
        # A parent with a child: the scope starts with the parent alone and the child is swept in.
        parent = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time; "
                                   "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                                   "time.sleep(60)"])
        try:
            for _ in range(100):
                kids = [p for p in memcap.tree(parent.pid) if p != parent.pid]
                if kids:
                    break
                time.sleep(0.05)
            unit, msg = memcap.adopt(parent.pid, "memcap-sweep-%d" % os.getpid(), {"MemoryMax": 256 << 20},
                                     "memcap test", settle=0)
            self.assertTrue(unit, msg)
            for pid in [parent.pid] + kids:
                self.assertTrue(memcap.cgroup(pid).endswith("/" + unit), (pid, memcap.cgroup(pid)))
        finally:
            for pid in memcap.tree(parent.pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, 9)
            parent.wait()


class SpawnIntegration(unittest.TestCase):
    def plan(self, *args):
        cdir = tempfile.mkdtemp()
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "agent-spawn"), "--dir", tempfile.mkdtemp(),
                            "--brief-text", "x", "--dry-run"] + list(args),
                           capture_output=True, text=True, env=dict(os.environ, AGENT_MAIL_CONFIG_DIR=cdir))
        return r

    def test_dry_run_shows_the_cap(self):
        r = self.plan()
        self.assertEqual(json.loads(r.stdout)["memory_cap"], "MemoryMax=32G, MemorySwapMax=2G")
        self.assertEqual(json.loads(self.plan("--memory-max", "48G").stdout)["memory_cap"],
                         "MemoryMax=48G, MemorySwapMax=2G")
        self.assertNotEqual(self.plan("--memory-max", "lots").returncode, 0)

    def test_pane_is_capped_before_the_agent_is_typed(self):
        sp = load_script("agent-spawn")
        events = []

        def fake_tmux(*args, check=True):
            events.append(args[0])
            return subprocess.CompletedProcess(args, 0, "123 456\n" if args[0] == "display-message" else "", "")

        sp.tmux = fake_tmux
        sp.time.sleep = lambda s: None
        sp.registry.claude_bin = lambda: "claude"
        with mock.patch.object(sp.memcap, "adopt", side_effect=lambda pid, *a, **k: events.append(("adopt", pid))
                               or ("agent-n.scope", "agent-n.scope (MemoryMax=32G)")), \
                mock.patch.object(sp.memcap, "tree", return_value=[123, 124]), \
                mock.patch.object(sp.memcap, "free_from_service",
                                  side_effect=lambda pid, *a, **k: events.append(("free", pid, k.get("exclude")))
                                  or (None, "")):
            out = sp.launch_claude("n", "/p", "/b", None, None, {"MemoryMax": 32 * G})
        # set-environment: the default login keeps CLAUDE_CONFIG_DIR out of the session (agent-spawn launch_claude).
        # The server is freed first, excluding the pane's tree, so the pane's own move is the last one.
        self.assertEqual(events, ["new-session", "set-environment", "display-message", ("free", 456, [123, 124]),
                                  ("adopt", 123), "send-keys", "send-keys"])
        self.assertEqual(out["memory"], "agent-n.scope (MemoryMax=32G)")


class SendIntegration(unittest.TestCase):
    def test_resume_claude_reports_the_cap(self):
        snd = load_script("agent-send")
        sess = {"id": str(uuid.uuid4()), "name": "rm-1", "cwd": None}
        with mock.patch.object(snd, "claude_bin", return_value="claude"), \
                mock.patch.object(snd.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")), \
                mock.patch.object(snd, "agent_caps", return_value=({"MemoryMax": G}, None)), \
                mock.patch.object(snd.memcap, "cap_bg_session", return_value="agent-rm-1.scope (MemoryMax=1G)") as cap:
            ok, out = snd.resume_claude(sess, "hi", True)
        self.assertTrue(ok)
        self.assertEqual(cap.call_args[0], (sess["id"], "rm-1", {"MemoryMax": G}))
        self.assertTrue(callable(cap.call_args[1]["session_pid"]))
        self.assertIn("memory: agent-rm-1.scope", out)

    def test_bad_config_size_does_not_block_a_resume(self):
        snd = load_script("agent-send")
        with mock.patch.object(snd.config, "load", return_value={"agent_memory_max": "lots"}):
            props, why = snd.agent_caps()
        self.assertIsNone(props)
        self.assertIn("uncapped", why)


if __name__ == "__main__":
    unittest.main()
