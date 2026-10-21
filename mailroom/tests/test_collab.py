import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["AGENT_MAIL_STATE_DIR"] = tempfile.mkdtemp()
os.environ["AGENT_STATUS_DIR"] = tempfile.mkdtemp()
from agentmail import config, gmail, orchestrate, routing  # noqa: E402

ACCT = "agents@example.com"
COLLAB = "sam.agent@example.com"
CFG = dict(config.DEFAULTS, allowlist=["owner@example.com"],
           collaborators={COLLAB: {"name": "Sam's Claude", "owner": "Sam"}})

AUTH_PASS = ("mx.google.com; dkim=pass header.i=@gmail.com header.s=20230601 header.b=abc; "
             "spf=pass (google.com: domain of x@gmail.com designates 1.2.3.4 as permitted sender) "
             "smtp.mailfrom=x@gmail.com; dmarc=pass (p=NONE sp=QUARANTINE dis=NONE) header.from=gmail.com")
AUTH_FAIL = "mx.google.com; dkim=fail header.i=@gmail.com; spf=softfail smtp.mailfrom=x@gmail.com; dmarc=fail header.from=gmail.com"


def msg(frm, body, ts):
    return ({"from": frm}, body, ts, "g%d" % ts)


class Tiers(unittest.TestCase):
    def test_sender_tier(self):
        self.assertEqual(routing.sender_tier("Owner@example.com", CFG), "owner")
        self.assertEqual(routing.sender_tier(COLLAB, CFG), "collaborator")
        self.assertIsNone(routing.sender_tier("someone@example.com", CFG))

    def test_auth_results_and_dkim(self):
        a = gmail.auth_results({"authentication-results": AUTH_PASS})
        self.assertEqual(a["dkim"], ["gmail.com"])
        self.assertTrue(a["spf"])
        self.assertEqual(a["dmarc"], "pass")
        self.assertTrue(routing.dkim_ok("x@gmail.com", a))
        self.assertFalse(routing.dkim_ok("x@example.com", a))
        b = gmail.auth_results({"authentication-results": AUTH_FAIL})
        self.assertEqual(b["dkim"], [])
        self.assertFalse(routing.dkim_ok("x@gmail.com", b))
        self.assertFalse(routing.dkim_ok("x@gmail.com", gmail.auth_results({})))

    def test_header_d_form_and_subdomain(self):
        a = gmail.auth_results({"authentication-results": "mx; dkim=pass header.d=mail.example.com header.s=k1"})
        self.assertTrue(routing.dkim_ok("x@example.com", a))


class Threads(unittest.TestCase):
    def setUp(self):
        self.msgs = [
            msg("Alex <owner@example.com>", "Please look at X.\n\nOn Thu wrote:\n> old", 1000),
            msg("Agents (w) <%s>" % ACCT, "Done, see branch.", 2000),
            msg("Sam's Claude <%s>" % COLLAB, "Could you also add Y?", 3000),
            msg("Agents (w) <%s>" % ACCT, "Added Y.", 4000),
        ]

    def test_thread_context_excludes_last_and_tags(self):
        txt = routing.thread_context(self.msgs, ACCT, CFG["collaborators"])
        self.assertIn("Please look at X.", txt)
        self.assertNotIn("> old", txt)
        self.assertIn("agent (ours)", txt)
        self.assertIn("Sam's Claude (collaborator agent)", txt)
        self.assertNotIn("Added Y.", txt)   # the latest message is delivered separately

    def test_trailing_agent_run(self):
        self.assertEqual(routing.trailing_agent_run(self.msgs, ACCT, CFG["collaborators"]), 3)
        self.assertEqual(routing.trailing_agent_run(self.msgs[:1], ACCT, CFG["collaborators"]), 0)

    def test_texts(self):
        t = routing.collaborator_text(7, "Sam's Claude", COLLAB, "Sam", "Re: Y", "body", "THREAD", owner_name="Alex")
        self.assertIn("collaborator agent of Sam, not Alex", t)
        self.assertIn("agent-mail reply 7", t)
        self.assertIn("THREAD", t)
        self.assertIn(routing.collab_guard("Alex"), t)
        self.assertIn("not from Alex", routing.collab_guard("Alex"))
        self.assertIn("tell Alex by email", routing.collab_guard("Alex"))
        o = routing.orchestrator_event(8, "owner", "Alex", "owner@example.com", None, "S", "b", owner_name="Alex")
        self.assertIn("(Alex)", o)
        self.assertNotIn(routing.collab_guard("Alex"), o)
        o2 = routing.orchestrator_event(9, "collaborator", "Sam's Claude", COLLAB, "Sam", "S", "b", owner_name="Alex")
        self.assertIn("NOT Alex", o2)

    def test_texts_default_to_the_owner(self):
        o = routing.orchestrator_event(9, "collaborator", "Sam's Claude", COLLAB, "Sam", "S", "b")
        self.assertIn("NOT the owner", o)
        self.assertIn("tell the owner by email", o)
        self.assertEqual(config.owner_name({}), "the owner")
        self.assertEqual(config.owner_name({"owner_name": "Alex"}), "Alex")
        self.assertEqual(config.owner_email({"allowlist": ["owner@example.com"]}), "owner@example.com")
        self.assertEqual(config.owner_email({"owner_email": "Alex@Example.com", "allowlist": []}), "alex@example.com")
        self.assertIsNone(config.owner_email({"allowlist": []}))

    def test_account_is_required(self):
        self.assertIsNone(config.DEFAULTS["account"])
        with self.assertRaises(config.ConfigError):
            config.require_account(dict(config.DEFAULTS))
        self.assertEqual(config.require_account({"account": ACCT}), ACCT)


class Orchestrate(unittest.TestCase):
    def setUp(self):
        self._claude_bin = orchestrate.registry.claude_bin
        orchestrate.registry.claude_bin = lambda: "/bin/claude"   # never depend on (or run) a real claude

    def tearDown(self):
        orchestrate.registry.claude_bin = self._claude_bin

    def test_command(self):
        cmd = orchestrate.command(dict(CFG, orchestrator_effort="high"))
        self.assertIn("-p", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertIn("--strict-mcp-config", cmd)
        cmd = orchestrate.command(dict(CFG, orchestrator_model="claude-sonnet-5-5"))
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-5-5")
        cmd = orchestrate.command({k: v for k, v in CFG.items() if k != "orchestrator_model"})
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")

    def test_default_models(self):
        # the orchestrator follows the latest Sonnet; spawned agents are pinned to full ids
        self.assertEqual(config.DEFAULTS["orchestrator_model"], "sonnet")
        for key in ("spawn_claude_model", "spawn_codex_model"):
            self.assertNotIn(config.DEFAULTS[key], ("sonnet", "opus", "haiku", "fable"), key)
        self.assertEqual((config.DEFAULTS["spawn_claude_model"], config.DEFAULTS["spawn_claude_effort"]),
                         ("claude-opus-5-5", "xhigh"))
        self.assertEqual((config.DEFAULTS["spawn_codex_model"], config.DEFAULTS["spawn_codex_effort"]),
                         ("gpt-6.1-sol", "xhigh"))

    def test_fallback_wrapping(self):
        d = tempfile.mkdtemp()
        Path(d, "project-id").write_text("proj-123\n")
        self.assertFalse(orchestrate.fallback_available(CFG))    # off by default
        cfg = dict(CFG, orchestrator_fallback_secret="AGENTS_ANTHROPIC_API_KEY", bws_project_file=str(Path(d, "project-id")))
        orig = orchestrate.registry.find_bin
        orchestrate.registry.find_bin = lambda name, extra: "/usr/bin/bws" if name == "bws" else orig(name, extra)
        try:
            cmd = orchestrate.fallback_command(cfg, ["claude", "-p"])
        finally:
            orchestrate.registry.find_bin = orig
        self.assertEqual(cmd[:4], ["/usr/bin/bws", "run", "--project-id", "proj-123"])
        # bws joins its arguments into one sh -c script, so the whole command is one quoted argument
        self.assertEqual(cmd[4:], ["--", 'ANTHROPIC_API_KEY="$AGENTS_ANTHROPIC_API_KEY" exec claude -p'])
        self.assertFalse(orchestrate.fallback_available(dict(cfg, orchestrator_fallback_secret="")))

    def test_fallback_script_runs_the_command_with_its_arguments(self):
        d = tempfile.mkdtemp()
        Path(d, "project-id").write_text("proj-123\n")
        cfg = dict(CFG, orchestrator_fallback_secret="AGENTS_ANTHROPIC_API_KEY", bws_project_file=str(Path(d, "project-id")))
        orig = orchestrate.registry.find_bin
        orchestrate.registry.find_bin = lambda name, extra: "/usr/bin/bws" if name == "bws" else orig(name, extra)
        try:
            cmd = orchestrate.fallback_command(cfg, ["printf", "[%s]", "a b", "x (y)\nz", "it's"])
        finally:
            orchestrate.registry.find_bin = orig
        # what bws does with it: sh -c "<arguments joined by spaces>", the secret in the environment
        r = subprocess.run(["sh", "-c", " ".join(cmd[5:])], capture_output=True, text=True,
                           env=dict(os.environ, AGENTS_ANTHROPIC_API_KEY="k"))
        self.assertEqual(r.stdout, "[a b][x (y)\nz][it's]")

    def test_looks_like_auth_or_limit(self):
        self.assertTrue(orchestrate.looks_like_auth_or_limit("You've hit your usage limit"))
        self.assertTrue(orchestrate.looks_like_auth_or_limit("401 Unauthorized"))
        self.assertFalse(orchestrate.looks_like_auth_or_limit("tool failed: no such file"))


if __name__ == "__main__":
    unittest.main()
