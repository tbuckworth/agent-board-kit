import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp()
os.environ["AGENT_MAIL_STATE_DIR"] = TMP
os.environ["AGENT_MAIL_CONFIG_DIR"] = TMP          # defaults only: no real token, no real config
os.environ["AGENT_STATUS_DIR"] = TMP
from agentmail import config, orchestrate  # noqa: E402

LIMIT = json.dumps({"is_error": True, "num_turns": 1, "result": "You've hit your session limit · resets 3pm"})
GOOD = json.dumps({"result": "done", "num_turns": 4, "session_id": "s-1", "total_cost_usd": 0.1})


def codex_out(*events):
    return "\n".join(json.dumps(e) for e in events)


CODEX_GOOD = codex_out(
    {"type": "thread.started", "thread_id": "t-1"},
    {"type": "item.completed", "item": {"type": "error", "message": "failed to parse plugin hooks config"}},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Checking."}},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "agent-mail reply 9"}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Replied to mail 9."}},
    {"type": "turn.completed", "usage": {"input_tokens": 10}})
CODEX_LIMIT = codex_out(
    {"type": "thread.started", "thread_id": "t-2"}, {"type": "turn.started"},
    {"type": "error", "message": "You've hit your usage limit."},
    {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})


class OrchestratorLoginsTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.outputs = [], []
        self.work_login, self.work = tempfile.mkdtemp(), tempfile.mkdtemp()
        Path(self.work_login, ".credentials.json").write_text("{}")
        self.cfg = dict(config.load(), orchestrator_dir=self.work, spawn_codex_model="gpt-x", usage={
            "claude_accounts": {"personal": {"config_dir": None}, "work": {"config_dir": self.work_login}},
            "order": ["claude:personal", "claude:work", "codex"]})
        self.saved = (orchestrate.subprocess.run, orchestrate.registry.claude_bin, orchestrate.registry.codex_bin,
                      orchestrate.fallback_available, orchestrate.fallback_command, orchestrate._fallback_env)
        orchestrate.registry.claude_bin = lambda: "claude"
        orchestrate.registry.codex_bin = lambda: "codex"
        orchestrate.fallback_available = lambda cfg: True
        orchestrate.fallback_command = lambda cfg, cmd: ["bws", "run", "--"] + cmd
        orchestrate._fallback_env = lambda cfg, env: {"BWS_ACCESS_TOKEN": "t"}

        def run(cmd, **kw):
            if cmd[0] == "git":                                             # statuslog's branch lookup
                return orchestrate.subprocess.CompletedProcess(cmd, 1, "", "")
            self.calls.append((cmd, kw.get("env"), kw.get("input")))
            out = self.outputs.pop(0)
            if isinstance(out, Exception):
                raise out
            failed = "is_error" in out or "turn.failed" in out
            return orchestrate.subprocess.CompletedProcess(cmd, 1 if failed else 0, out, "")
        orchestrate.subprocess.run = run

    def tearDown(self):
        (orchestrate.subprocess.run, orchestrate.registry.claude_bin, orchestrate.registry.codex_bin,
         orchestrate.fallback_available, orchestrate.fallback_command, orchestrate._fallback_env) = self.saved

    def names(self):
        return [c[0][0] if c[0][0] != "bws" else "bws" for c in self.calls]

    def test_order_personal_work_codex_then_the_key(self):
        got = orchestrate.logins(self.cfg, {"CLAUDE_CONFIG_DIR": "/elsewhere", "PATH": "/bin"}, self.work)
        self.assertEqual([g[0] for g in got], ["personal", "work", "codex", "api-key"])
        self.assertNotIn("CLAUDE_CONFIG_DIR", got[0][2])
        self.assertEqual(got[1][2]["CLAUDE_CONFIG_DIR"], str(Path(self.work_login).resolve()))
        codex = got[2][1]
        self.assertEqual(codex[:2], ["codex", "exec"])
        for flag in ("--json", "--ignore-user-config", "check_for_update_on_startup=false"):
            self.assertIn(flag, codex)
        self.assertEqual(codex[codex.index("-C") + 1], self.work)
        self.assertEqual(codex[codex.index("-m") + 1], "gpt-x")
        self.assertIn('model_reasoning_effort="high"', codex)
        self.assertEqual(codex[-1], "-")
        self.assertEqual(got[3][1][:3], ["bws", "run", "--"])

    def test_no_codex_installed_goes_straight_to_the_key(self):
        orchestrate.registry.codex_bin = lambda: None
        self.assertEqual([g[0] for g in orchestrate.logins(self.cfg, {}, self.work)],
                         ["personal", "work", "api-key"])

    def test_the_first_login_runs_it(self):
        self.outputs = [GOOD]
        info = orchestrate.run_event("event", "test", self.cfg)
        self.assertEqual((info["ok"], info["auth"], info["result"], len(self.calls)), (True, "personal", "done", 1))
        self.assertNotIn("failed", info)

    def test_both_claude_logins_out_runs_on_codex(self):
        self.outputs = [LIMIT, LIMIT, CODEX_GOOD]
        info = orchestrate.run_event("the event", "test", self.cfg)
        self.assertEqual((info["ok"], info["auth"], info["session_id"]), (True, "codex", "t-1"))
        self.assertEqual(info["result"], "Replied to mail 9.")
        self.assertEqual(info["failed"].count("session limit"), 2)
        self.assertEqual(self.names(), ["claude", "claude", "codex"])
        self.assertEqual(self.calls[2][2], "the event")                     # the event goes in on stdin
        self.assertEqual(self.calls[2][1]["AGENT_ORCHESTRATOR"], "1")       # agent-mail sends as the orchestrator

    def test_codex_out_too_uses_the_key(self):
        self.outputs = [LIMIT, LIMIT, CODEX_LIMIT, GOOD]
        info = orchestrate.run_event("event", "test", self.cfg)
        self.assertEqual((info["ok"], info["auth"]), (True, "api-key"))
        self.assertIn("codex: You've hit your usage limit.", info["failed"])
        self.assertEqual(self.calls[3][:2], (["bws", "run", "--"] + orchestrate.command(self.cfg),
                                             {"BWS_ACCESS_TOKEN": "t"}))

    def test_everything_out_fails(self):
        self.outputs = [LIMIT, LIMIT, CODEX_LIMIT, LIMIT]
        info = orchestrate.run_event("event", "test", self.cfg)
        self.assertFalse(info["ok"])
        self.assertIn("session limit", info["error"])
        self.assertEqual(len(self.calls), 4)

    def test_a_failure_part_way_or_a_timeout_is_not_rerun(self):
        self.outputs = [json.dumps({"is_error": True, "num_turns": 6, "result": "You've hit your session limit"})]
        info = orchestrate.run_event("event", "test", self.cfg)
        self.assertEqual((info["ok"], len(self.calls)), (False, 1))        # it may have acted already
        self.calls, self.outputs = [], [orchestrate.subprocess.TimeoutExpired("claude", 900)]
        info = orchestrate.run_event("event", "test", self.cfg)
        self.assertEqual((info["ok"], len(self.calls)), (False, 1))
        self.assertIn("timed out", info["error"])

    def test_codex_result_reads_the_last_message_and_ignores_warning_items(self):
        j = orchestrate.codex_result(CODEX_GOOD)
        self.assertEqual((j["result"], j["session_id"], j["num_turns"], j["is_error"]),
                         ("Replied to mail 9.", "t-1", 1, False))
        j = orchestrate.codex_result(CODEX_LIMIT)
        self.assertEqual((j["is_error"], j["result"], j["num_turns"]), (True, "You've hit your usage limit.", 0))


if __name__ == "__main__":
    unittest.main()
