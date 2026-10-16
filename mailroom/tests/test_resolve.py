"""Session lookup by name: `agent-send orchestrator` must reach the orchestrator, never a spawned agent
whose brief mentions it. No real sessions, no orchestrator runs."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.TemporaryDirectory()
os.environ["AGENT_MAIL_STATE_DIR"] = TMP.name
os.environ["AGENT_MAIL_CONFIG_DIR"] = TMP.name          # defaults only: the orchestrator is "orchestrator"
os.environ["AGENT_STATUS_DIR"] = TMP.name
from agentmail import config, orchestrate, registry, switch  # noqa: E402

BRIEF = ("You were started by agent-spawn on Alex's machine, on their behalf (usually relayed by the "
         "orchestrator from an email they sent).")


def sess(name, sid, live=True, cli="codex", title=""):
    return {"cli": cli, "name": name, "id": sid, "cwd": "/x", "status": "", "kind": "", "live": live,
            "socket": None, "pid": None, "tmux": None, "updated_at": 0, "title": title}


SPAWNED = sess("garden-shed-plan", "0190bbbb-0000-7000-8000-00000000b001", title=BRIEF)
UNNAMED = sess("", "0190cccc-0000-7000-8000-000000000000", title="Confirm Sam's paper sweep status")
NAMED_LIKE = sess("orchestrator-routing-fix", "4e4e4e4e-0000-4000-8000-000000000002", cli="claude")
UNNAMED_ORCH = sess("", "01a10ce0-0000-7000-8000-000000000000", title="orchestrator: check the queue")


def script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROOT / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Resolve(unittest.TestCase):
    def resolve(self, target, sessions):
        with patch.object(registry, "all_sessions", return_value=sessions), \
                patch.object(registry, "claude_transcript_session", return_value=None):
            return registry.resolve(target)

    def test_orchestrator_never_fuzzy_matches(self):
        for target in ("orchestrator", "Orchestrator"):
            s, code, _ = self.resolve(target, [SPAWNED, NAMED_LIKE, UNNAMED_ORCH, UNNAMED])
            self.assertEqual((s, code), (None, 4))

    def test_session_named_orchestrator_still_wins(self):
        exact = sess("orchestrator", "aaaaaaaa-0000-4000-8000-000000000000", cli="claude")
        s, code, _ = self.resolve("orchestrator", [SPAWNED, exact, NAMED_LIKE])
        self.assertEqual((s["id"], code), (exact["id"], 0))

    def test_configured_orchestrator_name_is_reserved(self):
        cfg = dict(config.DEFAULTS, orchestrator="orc")
        with patch.object(config, "load", return_value=cfg):
            self.assertEqual(self.resolve("orc", [sess("orca-review", "bbbbbbbb-0000-4000-8000-000000000000")])[1], 4)
            # "orchestrator" is an ordinary target again: name prefix applies
            self.assertEqual(self.resolve("orchestrator", [NAMED_LIKE])[0]["id"], NAMED_LIKE["id"])

    def test_title_text_in_the_middle_does_not_match(self):
        self.assertEqual(self.resolve("relayed by the orchestrator", [SPAWNED])[1], 4)
        self.assertEqual(self.resolve("paper sweep", [UNNAMED])[1], 4)

    def test_named_session_not_reachable_by_its_title(self):
        self.assertEqual(self.resolve("You were started", [SPAWNED])[1], 4)

    def test_unnamed_session_by_start_of_title(self):
        s, code, _ = self.resolve("confirm sam", [SPAWNED, UNNAMED])
        self.assertEqual((s["id"], code), (UNNAMED["id"], 0))
        self.assertEqual(self.resolve("Con", [UNNAMED])[1], 4)          # too short to be a title

    def test_names_and_ids_unchanged(self):
        sessions = [SPAWNED, UNNAMED, NAMED_LIKE]
        self.assertEqual(self.resolve("garden-shed-plan", sessions)[0]["id"], SPAWNED["id"])
        self.assertEqual(self.resolve("garden", sessions)[0]["id"], SPAWNED["id"])
        self.assertEqual(self.resolve("0190cccc", sessions)[0]["id"], UNNAMED["id"])
        self.assertEqual(self.resolve(NAMED_LIKE["id"], sessions)[0]["id"], NAMED_LIKE["id"])

    def test_agent_switch_cannot_pick_a_spawned_agent_for_the_orchestrator(self):
        with patch.object(registry, "all_sessions", return_value=[SPAWNED]), \
                patch.object(registry, "claude_transcript_session", return_value=None):
            with self.assertRaises(switch.SwitchError):
                switch.find("orchestrator", config.DEFAULTS)


class AgentSendOrchestrator(unittest.TestCase):
    def run_send(self, argv, sessions):
        sender = script("agent-send")
        out, err = io.StringIO(), io.StringIO()
        with patch.object(registry, "all_sessions", return_value=sessions), \
                patch.object(sender, "all_sessions", return_value=sessions), \
                patch.object(registry, "claude_transcript_session", return_value=None), \
                patch.object(sender, "queue_codex") as queue, \
                patch.object(sender, "send_claude_socket") as sock, \
                patch.object(orchestrate, "run_event", return_value={
                    "ok": True, "cost": 0, "turns": 1, "duration_s": 1, "result": "done"}) as run, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = sender.main(argv)
        return rc, out.getvalue(), queue, sock, run

    def test_dry_run_reports_the_orchestrator(self):
        for target in ("orchestrator", "Orchestrator"):
            rc, out, queue, sock, run = self.run_send(["--dry-run", "--from", "t", target, "test"],
                                                      [SPAWNED, NAMED_LIKE])
            self.assertEqual(rc, 0, target)
            self.assertIn("would run the orchestrator once", out)
            self.assertNotIn("garden-shed-plan", out)
            run.assert_not_called()

    def test_message_runs_the_orchestrator_not_a_spawned_agent(self):
        rc, out, queue, sock, run = self.run_send(["--from", "t", "orchestrator", "hello"], [SPAWNED, NAMED_LIKE])
        self.assertEqual(rc, 0)
        run.assert_called_once()
        self.assertIn("hello", run.call_args.args[0])
        queue.assert_not_called()
        sock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
