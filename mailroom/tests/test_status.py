import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp()
os.environ["AGENT_STATUS_DIR"] = TMP
os.environ["AGENT_MAIL_STATE_DIR"] = TMP
from agentmail import registry, report, statuslog  # noqa: E402


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROOT / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def fresh_log():
    statuslog.LOG = Path(tempfile.mkdtemp()) / "events.jsonl"


class StatusLog(unittest.TestCase):
    def setUp(self):
        fresh_log()

    def test_append_read_filter(self):
        statuslog.append({"cli": "claude", "session_id": "s1", "name": "a", "cwd": "/x", "event": "idle"})
        statuslog.append({"cli": "codex", "session_id": "s2", "name": "b", "cwd": "/y", "event": "note", "text": "hi"})
        self.assertEqual(len(statuslog.read()), 2)
        self.assertEqual(statuslog.read(event="note")[0]["text"], "hi")
        self.assertEqual(statuslog.read(session_id="s1")[0]["cli"], "claude")
        self.assertEqual(statuslog.read(since=time.time() + 10), [])
        self.assertEqual(oct(os.stat(statuslog.LOG).st_mode & 0o777), "0o600")

    def test_parse_since(self):
        now = 1000000.0
        self.assertAlmostEqual(statuslog.parse_since("2h", now), now - 7200)
        self.assertAlmostEqual(statuslog.parse_since("3d", now), now - 3 * 86400)
        self.assertIsNone(statuslog.parse_since(None))
        with self.assertRaises(ValueError):
            statuslog.parse_since("soon")

    def test_git_facts(self):
        d = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", "-b", "main", d], check=True)
        subprocess.run(["git", "-C", d, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "first"], check=True)
        f = statuslog.git_facts(d)
        self.assertEqual(f["branch"], "main")
        self.assertEqual(f["dirty"], 0)
        self.assertIn("first", f["last_commit"])
        self.assertEqual(statuslog.git_facts("/nonexistent")["branch"], "")


class Report(unittest.TestCase):
    def setUp(self):
        fresh_log()
        self._orig = registry.all_sessions
        registry.all_sessions = lambda cli=None: [
            {"cli": "claude", "name": "worker-a", "id": "s1", "cwd": "/x", "live": True, "status": "idle", "kind": "interactive", "updated_at": time.time()},
            {"cli": "codex", "name": "", "id": "s9", "cwd": "/z", "live": False, "status": "", "kind": "interactive", "updated_at": 0, "title": "old"},
        ]
        statuslog.append({"cli": "claude", "session_id": "s1", "name": "worker-a", "cwd": "/x", "event": "idle"})
        statuslog.append({"cli": "claude", "session_id": "s1", "name": "worker-a", "cwd": "/x", "event": "note", "text": "tests pass"})
        statuslog.append({"cli": "codex", "session_id": "s3", "name": "gone", "cwd": "/g", "event": "idle"})

    def tearDown(self):
        registry.all_sessions = self._orig

    def test_gather_and_render(self):
        rep = report.gather(since_seconds=3600)
        names = [r["name"] for r in rep["rows"]]
        self.assertIn("worker-a", names)          # live
        self.assertIn("gone", names)              # exited but had events
        self.assertNotIn("", names)               # stale codex thread with no events is left out
        a = next(r for r in rep["rows"] if r["name"] == "worker-a")
        self.assertEqual(a["turns"], 1)
        self.assertEqual(a["notes"][0]["text"], "tests pass")
        for fn in (report.render_text, report.render_markdown, report.render_html):
            out = fn(rep)
            self.assertIn("worker-a", out)
            self.assertIn("tests pass", out)
        self.assertIn("narr", report.render_html(rep, "narr"))


class StaleEntry(unittest.TestCase):
    """A claude SIGKILLed by a login move leaves its registry entry. Read after the live one (personal, then work),
    it must not show the agent as exited in the report, nor lend it its old name."""
    SID = "c0c0c0c0-0000-4000-8000-000000000003"

    def setUp(self):
        fresh_log()
        base = Path(tempfile.mkdtemp())
        sock = base / "sock"
        sock.write_text("")
        killed = subprocess.Popen(["true"])
        killed.wait()
        homes = []
        for acct, pid, status, name, at in (("personal", os.getpid(), "busy", "renamed", 1),
                                            ("work", killed.pid, "idle", "old-name", 2)):
            home = base / acct
            (home / "sessions").mkdir(parents=True)
            (home / "sessions" / ("%d.json" % pid)).write_text(json.dumps(
                {"pid": pid, "sessionId": self.SID, "name": name, "cwd": "/w", "status": status,
                 "messagingSocketPath": str(sock), "statusUpdatedAt": at * 1000}))
            homes.append((acct, home, None if acct == "personal" else str(home)))
        self._orig = registry.claude_homes, registry.CODEX_HOME
        registry.claude_homes, registry.CODEX_HOME = (lambda: homes), base / ".codex"

    def tearDown(self):
        registry.claude_homes, registry.CODEX_HOME = self._orig

    def test_report_and_identity_use_the_live_entry(self):
        self.assertEqual([(s["account"], s["live"]) for s in registry.claude_sessions()], [("personal", True)])
        statuslog.append({"cli": "claude", "session_id": self.SID, "name": "renamed", "cwd": "/w", "event": "idle"})
        row = next(r for r in report.gather(since_seconds=3600)["rows"] if r["id"] == self.SID)
        self.assertEqual((row["live"], row["status"], row["name"]), (True, "busy", "renamed"))
        self.assertEqual(statuslog.identity(cli="claude", session_id=self.SID)["name"], "renamed")


class Spawn(unittest.TestCase):
    def test_helpers(self):
        sp = load_script("agent-spawn")
        self.assertEqual(sp.slug("Fix: the thing!"), "Fix-the-thing")
        self.assertEqual(sp.tmux_name("feat/a.b:c"), "feat/a-b-c")
        d = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", "-b", "main", d], check=True)
        subprocess.run(["git", "-C", d, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "first"], check=True)
        self.assertEqual(sp.pick_base(d, None), "main")
        self.assertEqual(sp.pick_base(d, "origin/dev"), "origin/dev")
        self.assertTrue(sp.branch_exists(d, "main"))
        self.assertFalse(sp.branch_exists(d, "nope"))
        pre = sp.preamble("n", "/p", "b", "codex")
        self.assertIn("--session n", pre)
        self.assertIn("agent-close --self", pre)

    def test_preamble_names_the_owner(self):
        sp = load_script("agent-spawn")
        pre = sp.preamble("n", "/p", "b", "claude", {"owner_name": "Alex", "owner_email": "owner@example.com"})
        self.assertIn("on Alex's machine", pre)
        self.assertIn("Email Alex only when", pre)
        self.assertIn("agent-mail send --to owner@example.com", pre)
        pre = sp.preamble("n", "/p", "b", "claude", {"allowlist": ["first@example.com"]})
        self.assertIn("Email the owner only when", pre)
        self.assertIn("--to first@example.com", pre)

    def test_new_repo_needs_a_github_owner_and_lands_in_repos_root(self):
        cdir, root = tempfile.mkdtemp(), tempfile.mkdtemp()
        env = dict(os.environ, AGENT_MAIL_CONFIG_DIR=cdir)
        env.pop("AGENT_SPAWN_ROOT", None)
        cmd = [sys.executable, str(ROOT / "bin" / "agent-spawn"), "--new-repo", "demo", "--brief-text", "x", "--dry-run"]
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("github_owner", r.stderr)
        Path(cdir, "config.json").write_text(json.dumps({"github_owner": "example-user", "repos_root": root}))
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.loads(r.stdout)
        self.assertEqual((plan["github"], plan["path"]), ("example-user/demo", str(Path(root) / "demo")))
        r = subprocess.run(cmd + ["--org", "other-org"], capture_output=True, text=True,
                           env=dict(env, AGENT_SPAWN_ROOT=cdir))
        plan = json.loads(r.stdout)
        self.assertEqual((plan["github"], plan["path"]), ("other-org/demo", str(Path(cdir) / "demo")))

    def test_dry_run_dir(self):
        d = tempfile.mkdtemp()
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "agent-spawn"), "--dir", d, "--brief-text", "x", "--dry-run"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"mode": "dir"', r.stdout)

    def spawn_plan(self, *args, conf=None):
        cdir = tempfile.mkdtemp()
        if conf is not None:
            Path(cdir, "config.json").write_text(json.dumps(conf))
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "agent-spawn"), "--dir", tempfile.mkdtemp(),
                            "--brief-text", "x", "--dry-run"] + list(args),
                           capture_output=True, text=True, env=dict(os.environ, AGENT_MAIL_CONFIG_DIR=cdir))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_default_models(self):
        p = self.spawn_plan()
        self.assertEqual((p["cli"], p["model"], p["effort"]), ("claude", "claude-opus-5-5", "xhigh"))
        p = self.spawn_plan("--cli", "codex")
        self.assertEqual((p["model"], p["effort"]), ("gpt-6.1-sol", "xhigh"))
        p = self.spawn_plan("--cli", "codex", "--model", "gpt-6-astra", "--effort", "medium")
        self.assertEqual((p["model"], p["effort"]), ("gpt-6-astra", "medium"))
        self.assertEqual(self.spawn_plan("--effort", "max")["effort"], "max")
        p = self.spawn_plan("--cli", "codex", conf={"spawn_codex_model": "gpt-6-astra", "spawn_codex_effort": None})
        self.assertEqual((p["model"], p["effort"]), ("gpt-6-astra", None))

    def test_launch_passes_model_and_effort(self):
        sp = load_script("agent-spawn")
        sp.tmux = lambda *a, **k: None
        orig_bin, orig_sleep = sp.registry.claude_bin, sp.time.sleep
        sp.registry.claude_bin, sp.time.sleep = (lambda: "claude"), (lambda s: None)
        try:
            line = sp.launch_claude("n", "/p", "/b", "claude-opus-5-5", "xhigh")["launch"]
            self.assertIn("--model claude-opus-5-5 --effort xhigh", line)
            self.assertNotIn("--model", sp.launch_claude("n", "/p", "/b", None, None)["launch"])
        finally:
            sp.registry.claude_bin, sp.time.sleep = orig_bin, orig_sleep

    def test_existing_session_does_not_claim_model(self):
        sp = load_script("agent-spawn")
        sp.BRIEF_DIR = Path(tempfile.mkdtemp())
        sp.tmux_has = lambda name: True
        sp.ensure_trust = lambda path, claude_json=None: {}
        sp.run = lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "sent", "")
        fresh_log()
        for extra in (["--json"], []):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                sp.main(["--dir", tempfile.mkdtemp(), "--brief-text", "x", "--reuse"] + extra)
            if extra:
                plan = json.loads(out.getvalue())
                self.assertEqual((plan["tmux"], plan["model"], plan["effort"]), ("existing", None, None))
            else:
                self.assertIn("model and effort unchanged", out.getvalue())
                self.assertNotIn("claude-opus-5-5", out.getvalue())


if __name__ == "__main__":
    unittest.main()
