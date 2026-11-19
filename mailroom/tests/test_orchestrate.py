import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
from scripts import load_script  # noqa: E402
from agentmail import config, headless, orchestrate, registry  # noqa: E402

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
        self.work, self.orch, bws = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
        Path(self.work, ".credentials.json").write_text("{}")
        Path(bws, "token").write_text("tok\n")
        Path(bws, "project-id").write_text("proj-1\n")
        self.cfg = dict(config.load(), orchestrator_dir=self.orch, spawn_codex_model="gpt-x",
                        orchestrator_fallback_secret="AGENTS_ANTHROPIC_API_KEY",
                        bws_token_file=str(Path(bws, "token")), bws_project_file=str(Path(bws, "project-id")), usage={
                            "claude_accounts": {"personal": {"config_dir": None}, "work": {"config_dir": self.work}},
                            "order": ["claude:personal", "claude:work", "codex"]})

        def run(cmd, **kw):
            if cmd[0] == "git":                                             # statuslog's branch lookup
                return subprocess.CompletedProcess(cmd, 1, "", "")
            self.calls.append((cmd, kw.get("env"), kw.get("input")))
            out = self.outputs.pop(0)
            if isinstance(out, Exception):
                raise out
            failed = "is_error" in out or "turn.failed" in out
            return subprocess.CompletedProcess(cmd, 1 if failed else 0, out, "")
        find_bin = registry.find_bin
        # the CLIs: claude, codex and bws are never run here (run stands in for them), so none need be installed
        for target, attr, fake in ((registry, "claude_bin", lambda: "claude"), (registry, "codex_bin", lambda: "codex"),
                                   (registry, "find_bin", lambda name, extra: "/usr/bin/bws" if name == "bws"
                                    else find_bin(name, extra)),
                                   (subprocess, "run", run)):
            patcher = patch.object(target, attr, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def names(self):
        return [Path(c[0][0]).name for c in self.calls]

    def test_order_personal_work_codex_then_the_key(self):
        self.outputs = [LIMIT, LIMIT, CODEX_LIMIT, LIMIT]
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/elsewhere"}):
            orchestrate.run_event("event", "test", self.cfg)
        self.assertEqual(self.names(), ["claude", "claude", "codex", "bws"])
        envs = [c[1] for c in self.calls]
        self.assertFalse("CLAUDE_CONFIG_DIR" in envs[0])                    # the default login
        self.assertEqual(envs[1]["CLAUDE_CONFIG_DIR"], str(Path(self.work).resolve()))
        codex = self.calls[2][0]
        for flag in ("--json", "--ignore-user-config", "check_for_update_on_startup=false"):
            self.assertIn(flag, codex)
        self.assertEqual(codex[codex.index("-C") + 1], self.orch)
        self.assertEqual(codex[codex.index("-m") + 1], "gpt-x")
        self.assertIn('model_reasoning_effort="high"', codex)
        self.assertEqual(codex[-1], "-")
        self.assertEqual(self.calls[3][0], ["/usr/bin/bws", "run", "--project-id", "proj-1", "--",
                                            'ANTHROPIC_API_KEY="$AGENTS_ANTHROPIC_API_KEY" exec '
                                            + shlex.join(orchestrate.command(self.cfg))])
        self.assertEqual(envs[3]["BWS_ACCESS_TOKEN"], "tok")

    def test_no_codex_installed_goes_straight_to_the_key(self):
        self.outputs = [LIMIT, LIMIT, GOOD]
        with patch.object(registry, "codex_bin", return_value=None):
            info = orchestrate.run_event("event", "test", self.cfg)
        self.assertEqual((info["ok"], info["auth"], self.names()), (True, headless.API_KEY, ["claude", "claude", "bws"]))

    def test_the_first_login_runs_it(self):
        self.outputs = [GOOD]
        info = orchestrate.run_event("event", "test", self.cfg)
        self.assertEqual((info["ok"], info["auth"], info["result"], len(self.calls)), (True, "personal", "done", 1))
        self.assertNotIn("failed", info)

    def test_both_claude_logins_out_runs_on_codex(self):
        self.outputs = [LIMIT, LIMIT, CODEX_GOOD]
        inherited = {"CLAUDE_CODE_MESSAGING_SOCKET": "/run/s.sock", "CLAUDECODE": "1", "CLAUDE_CONFIG_DIR": "/elsewhere"}
        with patch.dict(os.environ, inherited):
            info = orchestrate.run_event("the event", "test", self.cfg)
        for cmd, run_env, _ in self.calls:                                  # neither claude nor codex gets them
            self.assertFalse({"CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDECODE"} & set(run_env), cmd[0])
            self.assertNotEqual(run_env.get("CLAUDE_CONFIG_DIR"), "/elsewhere", cmd[0])
        self.assertEqual((info["ok"], info["auth"], info["session_id"]), (True, "codex", "t-1"))
        self.assertEqual(info["result"], "Replied to mail 9.")
        self.assertEqual(info["failed"].count("session limit"), 2)
        self.assertEqual(self.names(), ["claude", "claude", "codex"])
        self.assertEqual(self.calls[2][2], "the event")                     # the event goes in on stdin
        self.assertEqual(self.calls[2][1]["AGENT_ORCHESTRATOR"], "1")       # agent-mail sends as the orchestrator

    def test_codex_out_too_uses_the_key(self):
        self.outputs = [LIMIT, LIMIT, CODEX_LIMIT, GOOD]
        info = orchestrate.run_event("event", "test", self.cfg)
        self.assertEqual((info["ok"], info["auth"]), (True, headless.API_KEY))
        self.assertIn("codex: You've hit your usage limit.", info["failed"])
        self.assertEqual(self.names()[3], "bws")

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
        self.calls, self.outputs = [], [subprocess.TimeoutExpired("claude", 900)]
        info = orchestrate.run_event("event", "test", self.cfg)
        self.assertEqual((info["ok"], len(self.calls)), (False, 1))
        self.assertIn("timed out", info["error"])

    def test_a_run_that_exits_0_without_a_result_fails(self):
        # personal prints nothing, work prints text that is not JSON, codex prints no JSON events,
        # the key's run prints a JSON list with no result object: each exits 0
        self.outputs = ["", "Loading...", "codex started\n", json.dumps([{"type": "system"}])]
        info = orchestrate.run_event("event", "test", self.cfg)
        self.assertFalse(info["ok"])
        self.assertEqual(len(self.calls), 4)                                # it returned nothing: the next login runs
        self.assertIn("exit 0 without a result", info["error"])
        for name, out in (("personal", "no output"), ("work", "Loading..."), ("codex", "codex started")):
            self.assertIn("%s: exit 0 without a result: %s" % (name, out), info["failed"])

    def test_a_run_log_that_cannot_be_written_fails_after_the_run(self):
        self.outputs = [GOOD]
        with patch.object(orchestrate, "RUNS_LOG", Path(tempfile.mkdtemp())):   # a directory: the append fails
            with self.assertRaisesRegex(RuntimeError, r"test · ok · \$0.1 · 4 turns · personal\).*IsADirectoryError"):
                orchestrate.run_event("event", "test", self.cfg)
            self.assertEqual(len(self.calls), 1)                            # after the run, which happened
            with self.assertRaisesRegex(RuntimeError, "orchestrator directory missing.*IsADirectoryError"):
                orchestrate.run_event("event", "test", dict(self.cfg, orchestrator_dir=self.orch + "/gone"))

    def test_a_status_log_that_cannot_be_written_fails_after_the_run(self):
        self.outputs = [GOOD]
        with patch.object(orchestrate.statuslog, "LOG", Path(tempfile.mkdtemp())):   # a directory: the append fails
            with self.assertRaisesRegex(RuntimeError, r"test · ok .* not recorded: IsADirectoryError"):
                orchestrate.run_event("event", "test", self.cfg)
        self.assertEqual(orchestrate.logged_runs()[-1]["result"], "done")   # the run log has it

    def test_codex_result_reads_the_last_message_and_ignores_warning_items(self):
        j = orchestrate.codex_result(CODEX_GOOD)
        self.assertEqual((j["result"], j["session_id"], j["num_turns"], j["is_error"]),
                         ("Replied to mail 9.", "t-1", 1, False))
        j = orchestrate.codex_result(CODEX_LIMIT)
        self.assertEqual((j["is_error"], j["result"], j["num_turns"]), (True, "You've hit your usage limit.", 0))


class RunsListing(unittest.TestCase):
    def test_runs_lists_the_last_ones_and_totals_every_run(self):
        log = Path(tempfile.mkdtemp(), "runs.jsonl")
        log.write_text("".join(json.dumps({"ts": 0, "source": "mail", "ok": True, "cost": cost, "result": "run %d" % i}) + "\n"
                               for i, cost in enumerate([0.5, 0.25, 1.0])))
        script = load_script("agent-orchestrate")
        out = io.StringIO()
        with patch.object(orchestrate, "RUNS_LOG", log), contextlib.redirect_stdout(out):
            self.assertEqual(script.main(["--runs", "--limit", "1"]), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("run 2", lines[0])
        self.assertEqual(lines[1], "total logged cost: $1.75 over 3 runs")

    def test_a_damaged_run_line_is_skipped_and_counted(self):
        log = Path(tempfile.mkdtemp(), "runs.jsonl")
        log.write_text('{"ts": 0, "ok": true, "cost": 0.5}\n{"ts": 1, "ok": tr\n')
        with patch.object(orchestrate, "RUNS_LOG", log), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(orchestrate.logged_runs(), [{"ts": 0, "ok": True, "cost": 0.5}])
        self.assertEqual(err.getvalue(), "%s: skipped 1 line(s) that are not JSON objects\n" % log)


if __name__ == "__main__":
    unittest.main()
