"""Session lookup by name: `agent-send orchestrator` must reach the orchestrator, never a spawned agent
whose brief mentions it. No real sessions, no orchestrator runs."""
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import homes  # noqa: E402
from scripts import load_script  # noqa: E402
from agentmail import config, orchestrate, registry, switch  # noqa: E402

BRIEF = ("You were started by agent-spawn on Alex's desktop, on his behalf (usually relayed by the "
         "orchestrator from an email he sent).")


def sess(name, sid, live=True, cli="codex", title=""):
    """A session for registry_with: a Claude one has no title (its brief is not in the registry)."""
    return {"cli": cli, "name": name, "id": sid, "live": live, "title": title}


SPAWNED = sess("garden-planner", "01a10cee-2dc4-7000-8000-000000000002", title=BRIEF)
UNNAMED = sess("", "01a10cef-0000-7000-8000-000000000000", title="Check the nightly backup status")
NAMED_LIKE = sess("orchestrator-routing-fix", "4e4e4e4e-0000-4000-8000-000000000003", cli="claude")
UNNAMED_ORCH = sess("", "01a10ce0-0000-7000-8000-000000000000", title="orchestrator: check the queue")


def registry_with(test, sessions, cfg=None):
    """A registry holding these sessions (sess) until the test ends: a Claude entry or a Codex thread for each, live
    or exited, under temporary homes, with cfg in config.json (homes.Homes)."""
    h = homes.Homes(test, cfg=cfg)
    for s in sessions:
        if s["cli"] == "claude":
            h.claude(s["id"], s["name"], live=s["live"], cwd="/x")
        else:
            h.codex(s["id"], s["name"], s["title"], live=s["live"], cwd="/x")
    return h


class Resolve(unittest.TestCase):
    def resolve(self, target, sessions, cfg=None):
        registry_with(self, sessions, cfg)
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
        cfg = {"orchestrator": "orc"}
        self.assertEqual(self.resolve("orc", [sess("orca-review", "bbbbbbbb-0000-4000-8000-000000000000")], cfg)[1], 4)
        # "orchestrator" is an ordinary target again: name prefix applies
        self.assertEqual(self.resolve("orchestrator", [NAMED_LIKE], cfg)[0]["id"], NAMED_LIKE["id"])

    def test_title_text_in_the_middle_does_not_match(self):
        self.assertEqual(self.resolve("relayed by the orchestrator", [SPAWNED])[1], 4)
        self.assertEqual(self.resolve("nightly backup", [UNNAMED])[1], 4)

    def test_named_session_not_reachable_by_its_title(self):
        self.assertEqual(self.resolve("You were started", [SPAWNED])[1], 4)

    def test_unnamed_session_by_start_of_title(self):
        s, code, _ = self.resolve("check the nightly", [SPAWNED, UNNAMED])
        self.assertEqual((s["id"], code), (UNNAMED["id"], 0))
        self.assertEqual(self.resolve("Che", [UNNAMED])[1], 4)          # too short to be a title

    def test_names_and_ids_unchanged(self):
        sessions = [SPAWNED, UNNAMED, NAMED_LIKE]
        self.assertEqual(self.resolve("garden-planner", sessions)[0]["id"], SPAWNED["id"])
        self.assertEqual(self.resolve("garden", sessions)[0]["id"], SPAWNED["id"])
        self.assertEqual(self.resolve("01a10cef", sessions)[0]["id"], UNNAMED["id"])
        self.assertEqual(self.resolve(NAMED_LIKE["id"], sessions)[0]["id"], NAMED_LIKE["id"])

    def test_agent_switch_cannot_pick_a_spawned_agent_for_the_orchestrator(self):
        registry_with(self, [SPAWNED])
        with self.assertRaises(switch.SwitchError):
            switch.find("orchestrator", config.load())


class ResolveExact(unittest.TestCase):
    """agent-mail's matching: exact name, id or id prefix (6+), never a name prefix, a title or a transcript."""

    def resolve(self, target, sessions):
        registry_with(self, sessions)
        with patch.object(registry, "claude_transcript_session", side_effect=AssertionError("no transcript lookup")):
            return registry.resolve_exact(target, None)

    def test_names_ids_and_id_prefixes(self):
        sessions = [SPAWNED, UNNAMED, NAMED_LIKE]
        self.assertEqual(self.resolve("garden-planner", sessions)[0]["id"], SPAWNED["id"])
        self.assertEqual(self.resolve(NAMED_LIKE["id"], sessions)[0]["id"], NAMED_LIKE["id"])
        self.assertEqual(self.resolve("01a10cef", sessions)[0]["id"], UNNAMED["id"])
        self.assertEqual(self.resolve("01a10ce", sessions)[1], 3)                 # both 01a10ce... ids

    def test_no_guesses(self):
        sessions = [SPAWNED, UNNAMED, NAMED_LIKE]
        for target in ("garden", "Check the nightly", "orchestrator-routing", "4e245"):
            self.assertEqual(self.resolve(target, sessions)[1], 4, target)


class AgentMailSession(unittest.TestCase):
    """agent-mail's --session and release target (resolve_session)."""

    def setUp(self):
        self.am = load_script("agent-mail")

    def resolve(self, spec, sessions):
        registry_with(self, sessions)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                return self.am.resolve_session(config.load(), spec, None), None
            except SystemExit as e:
                return err.getvalue(), e.code

    def test_exact_matches_only(self):
        self.assertEqual(self.resolve("garden-planner", [SPAWNED, NAMED_LIKE])[0]["id"], SPAWNED["id"])
        err, code = self.resolve("garden", [SPAWNED])
        self.assertEqual(code, 4)
        self.assertIn("no session matches --session 'garden'", err)
        err, code = self.resolve("01a10ce", [SPAWNED, UNNAMED])
        self.assertEqual(code, 3)
        self.assertIn("garden-planner", err)

    def test_orchestrator_by_name_unless_a_live_session_has_it(self):
        self.assertEqual(self.resolve("orchestrator", [SPAWNED, NAMED_LIKE])[0]["cli"], "orchestrator")
        named = sess("orchestrator", "aaaaaaaa-0000-4000-8000-000000000000", cli="claude")
        self.assertEqual(self.resolve("orchestrator", [named])[0]["id"], named["id"])
        self.assertEqual(self.resolve("orchestrator", [dict(named, live=False)])[0]["cli"], "orchestrator")
        self.assertEqual(self.resolve("Orchestrator", [SPAWNED])[0]["cli"], "orchestrator")   # as agent-send matches it

    def test_without_session_this_claude_session(self):
        sid = "bbbbbbbb-0000-4000-8000-000000000000"
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": sid}):
            self.assertEqual(self.resolve(None, [sess("mine", sid, cli="claude")])[0]["name"], "mine")
            self.assertEqual(self.resolve(None, [])[0]["id"], sid)                  # not in the registry yet
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            err, code = self.resolve(None, [])
        self.assertEqual(code, 1)
        self.assertIn("not inside a Claude session", err)


def run_send(argv):
    """agent-send's main on the registry there is (registry_with); the inbox socket, codex queue and the orchestrator
    run are stand-ins (they would reach real agents or run a model)."""
    sender = load_script("agent-send")
    out, err = io.StringIO(), io.StringIO()
    with patch.object(sender, "queue_codex") as queue, \
            patch.object(sender, "send_claude_socket") as sock, \
            patch.object(orchestrate, "run_event", return_value={
                "ok": True, "cost": 0, "turns": 1, "duration_s": 1, "result": "done"}) as run, \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = sender.main(argv)
    return rc, out.getvalue(), queue, sock, run


class AgentSendOrchestrator(unittest.TestCase):
    def test_dry_run_reports_the_orchestrator(self):
        registry_with(self, [SPAWNED, NAMED_LIKE])
        for target in ("orchestrator", "Orchestrator"):
            rc, out, queue, sock, run = run_send(["--dry-run", "--from", "t", target, "test"])
            self.assertEqual(rc, 0, target)
            self.assertIn("would run the orchestrator once", out)
            self.assertNotIn("garden-planner", out)
            run.assert_not_called()

    def test_message_runs_the_orchestrator_not_a_spawned_agent(self):
        registry_with(self, [SPAWNED, NAMED_LIKE])
        rc, out, queue, sock, run = run_send(["--from", "t", "orchestrator", "hello"])
        self.assertEqual(rc, 0)
        run.assert_called_once()
        self.assertIn("hello", run.call_args.args[0])
        queue.assert_not_called()
        sock.assert_not_called()


class BrokenConfig(unittest.TestCase):
    """A config.json that does not parse stops a lookup with its error. It must not read as defaults: the Claude
    logins it names would drop out of the address book, and mail to the orchestrator would report "no session
    matches"."""

    def setUp(self):
        registry_with(self, [SPAWNED]).config.write_text('{"orchestrator": "orc",')

    def test_claude_homes_raises(self):
        with self.assertRaises(ValueError):
            registry.claude_homes()

    def test_orchestrator_name_raises(self):
        with self.assertRaises(ValueError):
            registry.orchestrator_name()

    def test_agent_send_to_the_orchestrator_raises(self):
        with self.assertRaises(ValueError):
            run_send(["--from", "t", "orchestrator", "hello"])


if __name__ == "__main__":
    unittest.main()
