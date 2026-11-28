"""Headless claude -p runs: the command, reading the CLI's output, the run, and the logins to run it on."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp()
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import homes  # noqa: E402
from agentmail import config, headless, registry  # noqa: E402


class Command(unittest.TestCase):
    def test_skeleton_with_the_callers_model_effort_and_options(self):
        with patch.object(registry, "claude_bin", return_value="/bin/claude"):   # the installed CLI
            cmd = headless.claude_command("haiku", "low", ["--tools", ""])
            bare = headless.claude_command(None, None, [])
        self.assertEqual(cmd, ["/bin/claude", "-p", "--output-format", "json", "--model", "haiku", "--effort", "low",
                               "--mcp-config", str(headless.EMPTY_MCP), "--strict-mcp-config", "--tools", ""])
        self.assertEqual(json.loads(headless.EMPTY_MCP.read_text()), {"mcpServers": {}})
        self.assertEqual(bare, ["/bin/claude", "-p", "--output-format", "json", "--mcp-config", str(headless.EMPTY_MCP),
                                "--strict-mcp-config"])                     # None: the CLI's own default
        with patch.object(registry, "claude_bin", return_value=None), \
                self.assertRaisesRegex(RuntimeError, "claude binary not found"):
            headless.claude_command("haiku", None, [])


class Output(unittest.TestCase):
    def test_result_from_an_object_or_a_list(self):
        r = {"type": "result", "result": "done"}
        self.assertEqual(headless.result(json.dumps(r)), r)
        self.assertEqual(headless.result(json.dumps([{"type": "system"}, r])), r)
        for out in ("", "Loading...", json.dumps([{"type": "system"}]), "3"):
            self.assertEqual(headless.result(out), {}, out)

    def run_cli(self, code, stdout, stderr=""):
        # the claude CLI's exit status and output
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, stdout, stderr)) as run:
            got = headless.run(["claude", "-p"], "prompt", TMP, {"PATH": "/bin"}, 30, headless.result)
        self.assertEqual({k: run.call_args.kwargs[k] for k in ("input", "cwd", "env", "timeout")},
                         {"input": "prompt", "cwd": TMP, "env": {"PATH": "/bin"}, "timeout": 30})
        return got

    def test_run_says_what_went_wrong(self):
        self.assertEqual(self.run_cli(0, json.dumps({"result": "done"})), ({"result": "done"}, ""))
        self.assertEqual(self.run_cli(1, json.dumps({"is_error": True, "result": "You've hit your limit"}))[1],
                         "You've hit your limit")
        self.assertEqual(self.run_cli(2, "", "boom\n")[1], "boom")
        self.assertEqual(self.run_cli(0, "Loading...")[1], "exit 0 without a result: Loading...")
        self.assertEqual(self.run_cli(0, "")[1], "exit 0 without a result: no output")


class Logins(unittest.TestCase):
    def setUp(self):
        self.work, bws = tempfile.mkdtemp(), tempfile.mkdtemp()
        Path(self.work, ".credentials.json").write_text("{}")
        Path(bws, "token").write_text("tok\n")
        Path(bws, "project-id").write_text("proj-123\n")
        # the API-key fallback is off by default (orchestrator_fallback_secret None): these tests turn it on
        self.cfg = dict(config.load(), orchestrator_fallback_secret="AGENTS_ANTHROPIC_API_KEY",
                        bws_token_file=str(Path(bws, "token")),
                        bws_project_file=str(Path(bws, "project-id")), usage={
                            "claude_accounts": {"personal": {"config_dir": None}, "work": {"config_dir": self.work}},
                            "order": ["claude:personal", "codex", "claude:work"]})
        find_bin = registry.find_bin
        # bws: named in the command, never run, so it need not be installed
        patcher = patch.object(registry, "find_bin",
                               lambda name, extra: "/usr/bin/bws" if name == "bws" else find_bin(name, extra))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_usage_order_codex_where_the_caller_has_it_then_the_key(self):
        base = {"PATH": "/bin", "AGENT_ORCHESTRATOR": "1", "CLAUDE_CONFIG_DIR": "/elsewhere",
                "CLAUDE_CODE_MESSAGING_TOKEN": "t", "CLAUDE_BG_JOB": "j"}
        got = headless.logins(self.cfg, ["claude", "-p"], base, ["codex", "exec"])
        self.assertEqual([g[0] for g in got], ["personal", "codex", "work", headless.API_KEY])
        self.assertEqual([g[0] for g in headless.logins(self.cfg, ["claude", "-p"], base, None)],
                         ["personal", "work", headless.API_KEY])
        for name, cmd, env in got:   # base, without the session or the login this process runs in
            self.assertEqual((env["PATH"], env["AGENT_ORCHESTRATOR"]), ("/bin", "1"), name)
            self.assertFalse({"CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_BG_JOB"} & set(env), name)
            self.assertNotEqual(env.get("CLAUDE_CONFIG_DIR"), "/elsewhere", name)
        self.assertEqual(got[2][2]["CLAUDE_CONFIG_DIR"], str(Path(self.work).resolve()))
        self.assertEqual((got[3][1][:5], got[3][2]["BWS_ACCESS_TOKEN"]),
                         (["/usr/bin/bws", "run", "--project-id", "proj-123", "--"], "tok"))

    def test_a_login_not_set_up_and_no_api_key_are_left_out(self):
        Path(self.work, ".credentials.json").unlink()
        for off in ("", None):   # None: the default, no API-key fallback
            got = headless.logins(dict(self.cfg, orchestrator_fallback_secret=off), ["claude", "-p"], {}, None)
            self.assertEqual([g[0] for g in got], ["personal"], off)

    def test_a_manual_login_is_never_run_and_a_login_s_own_options_are(self):
        self.cfg["usage"]["claude_accounts"]["gemini"] = homes.per_token_login(self)
        self.cfg["usage"]["order"].insert(0, "claude:gemini")          # even listed first
        got = headless.logins(dict(self.cfg, orchestrator_fallback_secret=""), ["claude", "-p", "--model", "haiku"],
                              {}, None)
        self.assertEqual([g[0] for g in got], ["personal", "work"])
        settings = Path(self.work, "s.json")
        settings.write_text('{"env": {"OWN_ENDPOINT": "x"}}')
        self.cfg["usage"]["claude_accounts"]["work"].update(settings=str(settings), model="work-model")
        got = {g[0]: g for g in headless.logins(dict(self.cfg, orchestrator_fallback_secret=""),
                                                ["claude", "-p", "--model", "haiku"], {"OWN_ENDPOINT": "x"}, None)}
        self.assertEqual(got["work"][1],
                         ["claude", "-p", "--settings", str(settings.resolve()), "--model", "work-model"])
        self.assertEqual(got["personal"][1], ["claude", "-p", "--model", "haiku"])
        self.assertNotIn("OWN_ENDPOINT", got["personal"][2])           # a settings file's variables, for no login

    def test_api_key_wrapping(self):
        cmd = headless.api_key_command(self.cfg, ["claude", "-p"])
        self.assertEqual(cmd[:4], ["/usr/bin/bws", "run", "--project-id", "proj-123"])
        # bws joins its arguments into one sh -c script, so the whole command is one quoted argument
        self.assertEqual(cmd[4:], ["--", 'ANTHROPIC_API_KEY="$AGENTS_ANTHROPIC_API_KEY" exec claude -p'])
        with self.assertRaisesRegex(ValueError, "variable name"):
            headless.api_key_command(dict(self.cfg, orchestrator_fallback_secret="A; rm"), ["claude"])

    def test_api_key_script_runs_the_command_with_its_arguments(self):
        cmd = headless.api_key_command(self.cfg, ["printf", "[%s]", "a b", "x (y)\nz", "it's"])
        # what bws does with it: sh -c "<arguments joined by spaces>", the secret in the environment
        r = subprocess.run(["sh", "-c", " ".join(cmd[5:])], capture_output=True, text=True,
                           env=dict(os.environ, AGENTS_ANTHROPIC_API_KEY="k"))
        self.assertEqual(r.stdout, "[a b][x (y)\nz][it's]")


if __name__ == "__main__":
    unittest.main()
