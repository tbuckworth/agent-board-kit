import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import fakes  # noqa: E402
import homes  # noqa: E402
from scripts import load_script  # noqa: E402
from agentmail import config, failover, switch  # noqa: E402

HOST = config.DEFAULTS["host_id"]
WORK = tempfile.mkdtemp()
Path(WORK, ".credentials.json").write_text("{}")
CFG = dict(config.load(), usage={"claude_accounts": {"personal": {"config_dir": None}, "work": {"config_dir": WORK}},
                 "order": ["claude:personal", "claude:work", "codex"]},
           spawn_claude_model="claude-opus-5-5", spawn_claude_effort="xhigh",
           spawn_codex_model="gpt-6.1-sol", spawn_codex_effort="xhigh")
CLAUDE_MODELS = [{"value": "opus", "label": "Opus 5.5", "efforts": ["low", "medium", "high", "xhigh", "max"]},
                 {"value": "sonnet", "label": "Sonnet 5.5", "efforts": ["low", "medium", "high", "xhigh", "max"]},
                 {"value": "haiku", "label": "Haiku 4.5", "efforts": []}]
CODEX_MODELS = [{"value": "gpt-6.1-sol", "label": "GPT-6.1-Sol", "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"]}]


def write_jsonl(entries):
    f = Path(tempfile.mkdtemp()) / "t.jsonl"
    f.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return f


class Reading(unittest.TestCase):
    def test_flags(self):
        argv = ["claude", "--name", "a", "--model", "claude-opus-5-5", "--effort=high", "brief"]
        self.assertEqual(switch.commands.flag_value(argv, "--model"), "claude-opus-5-5")
        self.assertEqual(switch.commands.flag_value(argv, "--effort"), "high")
        self.assertIsNone(switch.commands.flag_value(argv, "-m"))
        cx = ["codex", "-s", "danger-full-access", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=xhigh"]
        self.assertEqual(switch.commands.flag_value(cx, "-m", "--model"), "gpt-6.1-sol")
        self.assertEqual(switch.effort_in_codex_argv(cx), "xhigh")
        self.assertEqual(switch.effort_in_codex_argv(["codex", "-c", "model_reasoning_effort='low'"]), "low")

    def test_condense_claude(self):
        path = write_jsonl([
            {"type": "user", "message": {"content": "Do the thing. Report by email."}},
            {"type": "user", "isMeta": True, "message": {"content": "<system reminder>"}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"},
                                                          {"type": "text", "text": "Starting."},
                                                          {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "huge output"}]}},
            {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "subagent"}]}},
            {"type": "assistant", "isApiErrorMessage": True, "message": {"content": [{"type": "text", "text": "You've hit your limit"}]}},
            {"type": "user", "message": {"content": [{"type": "text", "text": "carry on"}]}}])
        brief, lines = switch.condense_claude(path)
        self.assertEqual(brief, "Do the thing. Report by email.")
        self.assertEqual(lines, ["[assistant] Starting.", "[tool] Bash: git status", "[limit] You've hit your limit",
                                 "[user] carry on"])

    def test_condense_codex(self):
        path = write_jsonl([
            {"type": "session_meta", "payload": {"id": "t"}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions\n..."}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "The real brief"}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": []}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": "{\"cmd\": \"pytest -q\"}"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "wait", "arguments": "{\"cell_id\": \"2\"}"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Tests pass."}]}}])
        brief, lines = switch.condense_codex(path)
        self.assertEqual(brief, "The real brief")
        self.assertEqual(lines, ["[tool] exec: pytest -q", '[tool] wait: {"cell_id": "2"}', "[assistant] Tests pass."])

    def test_handoff_keeps_the_latest_part(self):
        st = {"name": "a", "id": "x" * 36, "cwd": "/w"}
        lines = ["[assistant] line %d %s" % (i, "y" * 1000) for i in range(400)]
        doc = switch.handoff_text(st, "Codex", "Claude", "/t.jsonl", "BRIEF", lines,
                                  [{"pid": 5, "command": "sleep 9", "output": "/o"}], CFG["owner_name"])
        self.assertIn("## Original brief\n\nBRIEF", doc)
        self.assertIn("earlier entries left out", doc)
        self.assertIn("line 399", doc)
        self.assertNotIn("line 0 ", doc)
        self.assertIn("- pid 5: sleep 9 (output: /o)", doc)
        self.assertLess(len(doc), switch.HANDOFF_BYTES + 5000)

    def test_codex_models_lists_visible_ones_by_priority(self):
        f = Path(tempfile.mkdtemp()) / "models_cache.json"
        f.write_text(json.dumps({"models": [
            {"slug": "b", "display_name": "B", "visibility": "list", "priority": 2, "supported_reasoning_levels": [{"effort": "low"}]},
            {"slug": "hidden", "visibility": "hide", "priority": 0},
            {"slug": "a", "display_name": "A", "visibility": "list", "priority": 1,
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "ultra"}]}]}))
        self.assertEqual(switch.codex_models(f), [{"value": "a", "label": "A", "efforts": ["low", "ultra"]},
                                                  {"value": "b", "label": "B", "efforts": ["low"]}])
        self.assertEqual(switch.codex_models(Path("/nonexistent")), [])   # no Codex here: no catalog
        f.write_text('{"models": [')
        with self.assertRaisesRegex(ValueError, "%s is not JSON" % f):        # never "no catalog", which skips checks
            switch.codex_models(f)


def agent(cli="claude", account="personal", model="claude-opus-5-5", effort="xhigh", busy=False, movable=True):
    return {"name": "agent-x", "id": "11111111-1111-1111-1111-111111111111", "cli": cli, "account": account,
            "cwd": "/w", "status": "busy" if busy else "idle", "live": True, "model": model, "effort": effort,
            "pane": "%5" if movable else None, "pid": 999, "movable": movable,
            "why_not": "" if movable else "not in tmux", "busy": busy, "background_jobs": 0}


CONVERSATION = write_jsonl([{"type": "user", "message": {"content": "Do the thing."}}])


class Switching(unittest.TestCase):
    def run_switch(self, st, calls=None, transcript=CONVERSATION, rollout=CONVERSATION, **kw):
        """transcript, rollout: what the Claude transcript and the Codex rollout lookups give (a path, None, or an
        exception to raise)."""
        calls = {} if calls is None else calls
        sess = kw.pop("sess", None) or {"id": st["id"], "name": st["name"], "cli": st["cli"], "account": st["account"],
                                        "live": True}
        lookup = (lambda found: {"side_effect": found} if isinstance(found, Exception) else {"return_value": found})
        stopped = kw.pop("stopped", True)   # whether the SIGKILLed agent exits
        # switch() is the decision under test: each step that would reach the agent or a CLI is a stand-in (its
        # lookup, the CLIs' model catalogs, stopping it, the move and relaunches in its pane, its files), each
        # driven for real elsewhere (test_resolve, ClaudeModels, StartCodex, test_failover's Move)
        patches = [mock.patch.object(switch, "find", return_value=sess),
                   mock.patch.object(switch, "state", return_value=st),
                   mock.patch.object(switch, "claude_models", return_value=CLAUDE_MODELS),
                   mock.patch.object(switch, "codex_models", return_value=CODEX_MODELS),
                   mock.patch.object(switch.failover, "migrate",
                                     side_effect=lambda *a, **k: calls.setdefault("migrate", (a, k)) and (True, "moved")),
                   mock.patch.object(switch, "relaunch_codex",
                                     side_effect=lambda *a: calls.setdefault("codex", a) and (True, "resumed")),
                   mock.patch.object(switch, "stop", side_effect=lambda pid: bool(calls.setdefault("stop", pid)) and stopped),
                   mock.patch.object(switch.claude_session, "transcript", **lookup(transcript)),
                   mock.patch.object(switch.registry, "codex_rollout", **lookup(rollout)),
                   mock.patch.object(switch.claude_session, "background_jobs", return_value=[]),
                   mock.patch.object(switch, "write_handoff", return_value=Path("/h/agent-x-handoff.md")),
                   mock.patch.object(switch, "start_codex", side_effect=kw.pop("start_error", None) or (
                       lambda st_, m, e, brief, codex: calls.setdefault("start_codex", (m, e, brief))
                       and (kw.pop("new_id", "thread-2"), kw.pop("queue_error", ""), kw.pop("notes", [])))),
                   mock.patch.object(switch, "start_claude",
                                     side_effect=lambda st_, dst_dir, m, e, brief, claude:
                                     calls.setdefault("start_claude", (dst_dir, m, e, brief)) and kw.pop("new_id", "sess-2"))]
        rehome = mock.Mock(return_value=1)
        trust = kw.pop("trust", lambda cwd, config_dir: calls.setdefault("trust", (cwd, config_dir)))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            res = switch.switch("agent-x", CFG, rehome=rehome, preamble=lambda st_, cli: "PREAMBLE ",
                                claude="/bin/claude", codex="/bin/codex", trust=trust,
                                **{k: v for k, v in kw.items() if k not in ("new_id", "queue_error", "notes")})
        return res, calls, rehome

    def test_busy_needs_force_and_touches_nothing(self):
        res, calls, _ = self.run_switch(agent(busy=True), to="claude:work")
        self.assertTrue(res["needs_force"])
        self.assertFalse(res["ok"])
        self.assertIn("will interrupt", res["detail"])
        self.assertEqual(calls, {})

    def test_idle_claude_to_other_login_and_model_keeps_the_conversation(self):
        res, calls, _ = self.run_switch(agent(), to="claude:work", model="sonnet", effort="high")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["id"], agent()["id"])   # the session switched, for agent-switch's status log entry
        args, kw = calls["migrate"]
        self.assertEqual((args[1], args[2]), ("work", None))   # idle: no prompt, it waits for input
        self.assertEqual(kw["overrides"], {"--model": "sonnet", "--effort": "high"})
        self.assertEqual(res["to"], "Claude Code (work login), sonnet at effort high")

    def test_forced_switch_tells_the_agent_it_was_interrupted(self):
        res, calls, _ = self.run_switch(agent(busy=True), model="sonnet", force=True)
        args, kw = calls["migrate"]
        self.assertEqual(args[1], "personal")
        self.assertIn("interrupting your turn", args[2])

    def test_nothing_to_do(self):
        res, calls, _ = self.run_switch(agent(), to="claude:personal")
        self.assertTrue(res["ok"])
        self.assertTrue(res["detail"].startswith("already"))
        self.assertEqual(calls, {})

    def test_an_agent_whose_model_is_unknown_keeps_it_and_says_so(self):
        res, calls, _ = self.run_switch(agent(model=None, effort=None), to="claude:work")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["from"], "Claude Code (personal login), model unknown at effort unknown")
        self.assertEqual(calls["migrate"][1]["overrides"], {"--model": None, "--effort": None})   # left as it runs
        res, calls, _ = self.run_switch(agent("codex", None, None, None), effort="high")
        self.assertEqual(calls["codex"][1:3], (None, "high"))

    def test_haiku_takes_no_effort(self):
        res, calls, _ = self.run_switch(agent(), model="haiku")
        self.assertEqual(calls["migrate"][1]["overrides"], {"--model": "haiku", "--effort": None})

    def test_refusals(self):
        for st, kw, msg in ((agent(movable=False), {"to": "claude:work"}, "not in tmux"),
                            (agent(), {"to": "claude:max"}, "unknown account"),
                            (agent(), {"model": "gpt-6.1-sol"}, "unknown claude model"),
                            (agent(), {"model": "opus", "effort": "ultra"}, "does not take effort")):
            with self.assertRaisesRegex(switch.SwitchError, msg):
                self.run_switch(st, **kw)
        cfg = json.loads(json.dumps(CFG))
        cfg["usage"]["claude_accounts"]["work"]["config_dir"] = "/nonexistent"
        with mock.patch.object(switch, "find", return_value={"id": "x", "cli": "claude"}), \
                mock.patch.object(switch, "state", return_value=agent()), \
                self.assertRaisesRegex(switch.SwitchError, "not set up"):
            switch.switch("agent-x", cfg, to="claude:work")

    def test_a_background_session_changes_login_but_is_not_handed_to_codex(self):
        bg = dict(agent(model="claude-fable-5-1", effort="high"), pane=switch.claude_session.BG)
        res, calls, _ = self.run_switch(bg, to="claude:work")
        self.assertTrue(res["ok"], res)
        self.assertEqual(calls["migrate"][1]["overrides"], {"--model": "claude-fable-5-1", "--effort": "high"})
        with self.assertRaisesRegex(switch.SwitchError, "background session"):
            self.run_switch(bg, to="codex")

    def test_codex_model_change_resumes_the_thread(self):
        res, calls, _ = self.run_switch(agent("codex", None, "gpt-6.1-sol", "xhigh"), effort="ultra")
        self.assertTrue(res["ok"])
        self.assertEqual(calls["codex"][1:4], ("gpt-6.1-sol", "ultra", None))

    def test_claude_to_codex_is_a_handoff_with_mail_rerouted(self):
        res, calls, rehome = self.run_switch(agent(), to="codex")
        self.assertTrue(res["ok"], res)
        self.assertEqual(calls["stop"], 999)
        model, effort, brief = calls["start_codex"]
        self.assertEqual((model, effort), ("gpt-6.1-sol", "xhigh"))   # the spawn defaults for the new CLI
        self.assertTrue(brief.startswith("PREAMBLE You are taking over agent agent-x"))
        self.assertIn("/h/agent-x-handoff.md", brief)
        rehome.assert_called_once_with("claude", "11111111-1111-1111-1111-111111111111", "codex", "thread-2",
                                       "agent-x", "/w")
        self.assertIn("mail routing moved", res["detail"])

    def test_a_handoff_removes_the_killed_claudes_registry_entry(self):
        home = Path(tempfile.mkdtemp())
        (home / "sessions").mkdir()
        killed = subprocess.Popen(["true"])
        killed.wait()
        st = dict(agent(account="work"), pid=killed.pid)
        entry = home / "sessions" / ("%d.json" % killed.pid)
        entry.write_text(json.dumps({"pid": killed.pid, "sessionId": st["id"], "name": "agent-x"}))
        sess = {"id": st["id"], "name": "agent-x", "cli": "claude", "account": "work", "live": True,
                "pid": killed.pid, "config_dir": str(home)}
        res, calls, _ = self.run_switch(st, to="codex", sess=sess)
        self.assertTrue(res["ok"], res)
        self.assertEqual(calls["stop"], killed.pid)
        self.assertFalse(entry.exists())

    def test_codex_to_claude_handoff_on_a_chosen_login(self):
        res, calls, rehome = self.run_switch(agent("codex", None, "gpt-6.1-sol", "xhigh"), to="claude:work",
                                             model="opus", effort="max")
        dst_dir, model, effort, brief = calls["start_claude"]
        self.assertEqual((dst_dir, model, effort), (str(Path(WORK).resolve()), "opus", "max"))
        self.assertEqual(calls["trust"], ("/w", str(Path(WORK).resolve())))   # the folder, on the login it goes to
        self.assertEqual(rehome.call_args[0][2:4], ("claude", "sess-2"))

    def test_a_folder_that_cannot_be_trusted_is_refused_before_stopping(self):
        def unwritable(cwd, config_dir):
            raise OSError("Read-only file system: '/h/.claude.json'")
        calls = {}
        with self.assertRaisesRegex(switch.SwitchError, "cannot trust /w for Codex.*Read-only file system"):
            self.run_switch(agent(), calls=calls, to="codex", trust=unwritable)
        self.assertEqual(calls, {})

    def test_a_handoff_whose_new_session_never_shows_is_reported(self):
        res, calls, rehome = self.run_switch(agent(), to="codex", new_id=None)
        self.assertFalse(res["ok"])
        self.assertIn("did not show up", res["detail"])
        rehome.assert_not_called()

    def test_an_agent_that_outlives_sigkill_is_a_failed_switch_not_a_refusal(self):
        """It was stopped, so it is exit 1 (attempted and failed), and nothing else is started."""
        res, calls, rehome = self.run_switch(agent(), to="codex", stopped=False)
        self.assertFalse(res["ok"])
        self.assertIn("pid 999 did not exit after SIGKILL; nothing started", res["detail"])
        self.assertNotIn("start_codex", calls)
        rehome.assert_not_called()

    def test_a_handoff_tmux_refuses_is_a_failure(self):
        res, calls, rehome = self.run_switch(agent(), to="codex",
                                             start_error=switch.commands.CommandError("tmux failed: no such pane"))
        self.assertFalse(res["ok"])
        self.assertIn("starting Codex, gpt-6.1-sol at effort xhigh in pane %5 failed: tmux failed: no such pane",
                      res["detail"])
        rehome.assert_not_called()

    def test_a_handoff_whose_brief_was_not_queued_is_a_failure(self):
        """A handoff whose `codex queue` fails is ok=False with the queue's error, and the mail routing still moves
        to the new thread."""
        res, calls, rehome = self.run_switch(agent(), to="codex", queue_error="codex queue failed: no such thread")
        self.assertFalse(res["ok"])
        self.assertIn("as session thread-2", res["detail"])
        self.assertIn("codex queue failed: no such thread", res["detail"])
        rehome.assert_called_once()   # the new thread is the agent now: replies must reach it

    def test_a_handoff_says_what_codex_left_at_its_start(self):
        res, _, _ = self.run_switch(agent(), to="codex", notes=["Codex: 1 hook is new or changed, left untrusted"])
        self.assertTrue(res["ok"])
        self.assertIn("mail routing moved; Codex: 1 hook is new or changed, left untrusted)", res["detail"])

    def test_a_handoff_with_no_conversation_to_hand_over_stops_nothing(self):
        """With no transcript or rollout, or an unreadable Codex database, the handoff is refused before anything
        is stopped, dry runs too."""
        for st, kw, msg in ((agent(), {"to": "codex", "transcript": None}, "no transcript"),
                            (agent("codex", None, "gpt-6.1-sol", "xhigh"), {"to": "claude:work", "rollout": None},
                             "no rollout log"),
                            (agent("codex", None, "gpt-6.1-sol", "xhigh"),
                             {"to": "claude:work", "rollout": sqlite3.OperationalError("database disk image is malformed")},
                             "database disk image is malformed")):
            for dry_run in (False, True):
                calls = {}
                with self.subTest(msg=msg, dry_run=dry_run):
                    with self.assertRaisesRegex(switch.SwitchError, msg):
                        self.run_switch(st, calls=calls, dry_run=dry_run, **kw)
                    self.assertEqual(calls, {})

    def test_no_handoff_to_codex_where_it_would_run_without_the_worktrees_review_gate(self):
        """Codex runs the main checkout's .codex/hooks.json even in a linked worktree; refused before anything is
        stopped, dry runs too. A Claude login switch in the same folder goes ahead."""
        repo, wt = homes.gated_worktree(self)
        st = dict(agent(), cwd=str(wt))
        for dry_run in (False, True):
            calls = {}
            with self.subTest(dry_run=dry_run), self.assertRaisesRegex(
                    switch.SwitchError, "cannot hand agent-x over to Codex in %s: Codex runs the main checkout's "
                                        ".codex/hooks.json even in a linked worktree, and %s has none" % (wt, repo)):
                self.run_switch(st, calls=calls, dry_run=dry_run, to="codex")
            self.assertEqual(calls, {})
        res, calls, _ = self.run_switch(st, to="claude:work")
        self.assertTrue(res["ok"], res)

    def test_dry_run_stops_nothing(self):
        for kw in ({"to": "codex"}, {"to": "claude:work"}, {"model": "sonnet"}):
            res, calls, _ = self.run_switch(agent(), dry_run=True, **kw)
            self.assertTrue(res["ok"])
            self.assertTrue(res["detail"].startswith("would"), res["detail"])
            self.assertEqual(calls, {})


class StartCodex(unittest.TestCase):
    def start(self, live_ids, **env):
        """switch.start_codex in pane %5 with the fake tmux and codex (tests/fakes.py), and Codex's live threads
        (read from its lock files) answering live_ids, one set per look, the last one from then on."""
        bins = fakes.install(self, **env)
        ids = [set(x) for x in live_ids]
        st = dict(agent("claude"), cwd=tempfile.mkdtemp())
        # Codex's live threads on each look (its lock files): the fake codex starts no thread, and the new one must
        # appear only after the launch
        with mock.patch.object(switch.registry, "codex_live_ids", side_effect=lambda: ids.pop(0) if len(ids) > 1 else ids[0]), \
                mock.patch.object(switch.launch, "CODEX_THREAD_SECONDS", 0.2), mock.patch.object(switch.launch.time, "sleep"):
            return switch.start_codex(st, "gpt-6.1-sol", "xhigh", "the brief", "codex"), bins

    def test_the_brief_is_queued_and_checked(self):
        (tid, error, notes), bins = self.start([(), ("thread-2",)])
        self.assertEqual((tid, error, notes), ("thread-2", "", []))
        self.assertIn(["queue", "--thread", "thread-2", "--message", "the brief"], bins.calls("codex"))
        (tid, error, notes), _ = self.start([(), ("thread-2",)], FAKE_CODEX_QUEUE="error: no such thread")
        self.assertEqual(tid, "thread-2")
        self.assertIn("codex queue failed: error: no such thread", error)
        (tid, error, notes), bins = self.start([()])
        self.assertIsNone(tid)
        self.assertEqual([c for c in bins.calls("codex") if c[:1] == ["queue"]], [])


class Chained(unittest.TestCase):
    def test_history_survives_a_second_switch(self):
        with mock.patch.object(switch, "HANDOFF_DIR", Path(tempfile.mkdtemp()) / "agent-switch"):
            first = switch.write_handoff("agent-x", switch.handoff_text(
                {"name": "agent-x", "id": "x" * 36, "cwd": "/w"}, "Claude", "Codex", "/t.jsonl",
                "Remember KESTREL. Do the task.", ["[assistant] step one", "[tool] Bash: make"], [], CFG["owner_name"]))
        handoff_brief = switch.handoff_brief({"name": "agent-x", "id": "x" * 36, "cwd": "/w"}, first, "Claude", "Codex", CFG["owner_name"])
        brief, lines = switch.carry_forward(handoff_brief, ["[assistant] step two"])
        self.assertEqual(brief, "Remember KESTREL. Do the task.")
        self.assertEqual(lines[:2], ["[assistant] step one", "[tool] Bash: make"])
        self.assertIn("switched here", lines[2])
        self.assertEqual(lines[3], "[assistant] step two")
        self.assertEqual(switch.carry_forward("an ordinary brief", ["x"]), ("an ordinary brief", ["x"]))


class Options(unittest.TestCase):
    def test_targets_and_interrupt_warning(self):
        st = agent(busy=True)
        snaps = {"claude:personal": {"windows": [{"kind": "session", "name": "5-hour", "remaining_pct": 50,
                                                  "used_pct": 50, "resets_at": None, "model": None}]}}
        with mock.patch.object(switch, "find", return_value={"id": st["id"]}), \
                mock.patch.object(switch, "state", return_value=st):
            opts = switch.options("agent-x", CFG, snaps=snaps, models={"claude": CLAUDE_MODELS, "codex": CODEX_MODELS})
        self.assertTrue(opts["will_interrupt"])
        kinds = {t["target"]: (t["kind"], t["status"]) for t in opts["targets"]}
        self.assertEqual(kinds, {"claude:personal": ("current", "ok"), "claude:work": ("resume", "unknown"),
                                 "codex": ("handoff", "unknown")})
        self.assertEqual(opts["defaults"]["codex"], {"model": "gpt-6.1-sol", "effort": "xhigh"})
        self.assertEqual(opts["agent"]["model"], "claude-opus-5-5")

    def test_a_fable_agent_sees_which_logins_have_fable_left(self):
        st = agent(model="claude-fable-5-1")
        fable = {"kind": "model_weekly", "name": "weekly Fable", "remaining_pct": 0, "used_pct": 100,
                 "resets_at": None, "model": "fable"}
        snaps = {"claude:personal": {"windows": [{"kind": "session", "name": "5-hour", "remaining_pct": 50,
                                                  "used_pct": 50, "resets_at": None, "model": None}, fable]}}
        with mock.patch.object(switch, "find", return_value={"id": st["id"]}), \
                mock.patch.object(switch, "state", return_value=st):
            opts = switch.options("agent-x", CFG, snaps=snaps, models={"claude": CLAUDE_MODELS, "codex": CODEX_MODELS})
        status = {t["target"]: t["status"] for t in opts["targets"]}
        self.assertEqual(status["claude:personal"], "out")   # 5-hour is fine, but this agent runs Fable
        self.assertIn("weekly Fable 0% left", [t for t in opts["targets"] if t["current"]][0]["usage"])

    def test_a_background_session_reads_its_model_from_the_transcript(self):
        """A background session: a daemon's spare runs it, so its command line names no model; it ran Fable 5.1."""
        path = write_jsonl([{"type": "permission-mode", "permissionMode": "bypassPermissions"},
                            {"type": "assistant", "perTurnEffort": "high",
                             "message": {"model": "claude-fable-5-1", "content": []}},
                            {"type": "assistant", "isApiErrorMessage": True, "apiError": "model_requires_usage_credits",
                             "message": {"model": "<synthetic>", "content": []}}])
        sess = {"id": "c0c0c0c0-0000-4000-8000-000000000003", "name": "axx", "cli": "claude", "account": "personal",
                "live": True, "pid": 999, "kind": "bg", "tmux": None, "status": "idle", "cwd": "/w"}
        with mock.patch.object(switch.procs, "cmdline", return_value=["claude", "bg-spare", "--bg-spare", "/s"]), \
                mock.patch.object(switch.claude_session, "transcript", return_value=path), \
                mock.patch.object(switch.claude_session, "background_jobs", return_value=[]), \
                mock.patch.object(switch, "pane_busy", side_effect=AssertionError("a bg session has no pane")):
            st = switch.state(sess)
        self.assertEqual((st["model"], st["effort"], st["movable"], st["pane"], st["busy"]),
                         ("claude-fable-5-1", "high", True, switch.claude_session.BG, False))

    def test_a_model_it_cannot_read_is_unknown_not_the_config_default(self):
        sess = {"id": "c0c0c0c0-0000-4000-8000-000000000003", "name": "axx", "cli": "claude", "account": "personal",
                "live": True, "pid": 999, "kind": "bg", "tmux": None, "status": "idle", "cwd": "/w"}
        with mock.patch.object(switch.procs, "cmdline", return_value=["claude", "bg-spare", "--bg-spare", "/s"]), \
                mock.patch.object(switch.claude_session, "transcript", return_value=None), \
                mock.patch.object(switch.claude_session, "background_jobs", return_value=[]):
            st = switch.state(sess)
        self.assertEqual((st["model"], st["effort"]), (None, None))
        sw = load_script("agent-switch")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            sw.show_options({"agent": dict(st, busy=False, movable=True), "targets": [], "models": {}})
        self.assertIn("model unknown, effort unknown", out.getvalue())


class Relaunch(unittest.TestCase):
    """Relaunching in the agent's pane: tmux, the agent's process (its argv in /proc, stopping it) are made up."""

    def test_codex_resume_line_keeps_sandbox_and_folder(self):
        st = agent("codex", None, "gpt-6.1-sol", "xhigh")
        typed = []
        with mock.patch.object(switch.procs, "cmdline", return_value=[
                "codex", "-s", "danger-full-access", "-a", "never", "-C", "/w", "-m", "gpt-6.1-sol",
                "-c", "model_reasoning_effort=xhigh"]), \
                mock.patch.object(switch, "stop", return_value=True), \
                mock.patch.object(switch.commands, "tmux", side_effect=lambda *a, **k: typed.append(a)), \
                mock.patch.object(switch.launch.time, "sleep"), \
                mock.patch.object(switch, "codex_pane", return_value=("%5", 1234)):   # tmux: the boundary
            ok, detail = switch.relaunch_codex(st, "gpt-6.1-sol", "ultra", None, "/bin/codex")
        self.assertTrue(ok)
        self.assertEqual(typed[0], ("set-environment", "-t", "%5", "-r", "CLAUDE_CONFIG_DIR"))
        self.assertEqual(typed[1][-1], "stty sane; unset CLAUDE_CONFIG_DIR; /bin/codex resume %s -s danger-full-access "
                                   "-a never -C /w -c check_for_update_on_startup=false -m gpt-6.1-sol "
                                   "-c model_reasoning_effort=ultra" % st["id"])
        typed.clear()
        with mock.patch.object(switch.procs, "cmdline", return_value=["codex", "-C", "/w"]), \
                mock.patch.object(switch, "stop", return_value=True), \
                mock.patch.object(switch.commands, "tmux", side_effect=lambda *a, **k: typed.append(a)), \
                mock.patch.object(switch.launch.time, "sleep"), \
                mock.patch.object(switch, "codex_pane", return_value=("%5", 1234)):   # tmux: the boundary
            self.assertTrue(switch.relaunch_codex(st, None, "high", None, "/bin/codex")[0])
        self.assertTrue(typed[1][-1].endswith("-c check_for_update_on_startup=false -c model_reasoning_effort=high"),
                        typed[1][-1])   # a model it cannot read: none given, so it keeps the CLI's


    def test_a_codex_that_outlives_sigkill_is_not_relaunched(self):
        st = agent("codex", None, "gpt-6.1-sol", "xhigh")
        with mock.patch.object(switch.procs, "cmdline", return_value=["codex", "-C", "/w"]), \
                mock.patch.object(switch, "stop", return_value=False), \
                mock.patch.object(switch.commands, "tmux", side_effect=AssertionError("nothing is typed")):
            self.assertEqual(switch.relaunch_codex(st, "gpt-6.1-sol", "ultra", None, "/bin/codex"),
                             (False, "codex pid 999 did not exit after SIGKILL; nothing relaunched"))

    def test_a_resume_tmux_refuses_after_codex_was_stopped_is_reported(self):
        st = agent("codex", None, "gpt-6.1-sol", "xhigh")

        def refuses(*args, check):   # tmux is the boundary: the agent's pane is gone
            raise switch.commands.CommandError("tmux set-environment -t %5 failed (exit 1): can't find pane: %5")
        with mock.patch.object(switch.procs, "cmdline", return_value=["codex", "-C", "/w"]), \
                mock.patch.object(switch, "stop", return_value=True) as stop, \
                mock.patch.object(switch.commands, "tmux", side_effect=refuses):
            ok, detail = switch.relaunch_codex(st, "gpt-6.1-sol", "ultra", None, "/bin/codex")
        stop.assert_called_once_with(999)
        self.assertFalse(ok)
        self.assertEqual(detail, "stopped codex pid 999, but relaunching it in pane %5 failed: tmux set-environment "
                                 "-t %5 failed (exit 1): can't find pane: %5")


class CodexLiveness(unittest.TestCase):
    def test_only_a_held_lock_is_a_live_thread(self):
        from agentmail import registry
        base = Path(tempfile.mkdtemp())
        locks = base / "thread-writer-locks"
        locks.mkdir()
        for tid in ("held", "stale", "other"):
            (locks / (tid + ".lock")).write_text("")
        proc = base / "proc"
        for pid, argv0, held in (("100", "/x/bin/codex", ["held"]), ("200", "/usr/bin/vim", ["other"])):
            (proc / pid / "fd").mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(argv0.encode() + b"\0--flag\0")
            for i, tid in enumerate(held):
                os.symlink(str(locks / (tid + ".lock")), str(proc / pid / "fd" / str(i)))
        (proc / "self").mkdir()
        self.assertEqual(registry.codex_held_locks(locks, str(proc)), {"held"})   # vim holding a lock is not codex
        self.assertIsNone(registry.codex_held_locks(locks, str(base / "no-proc")))
        with mock.patch.object(registry, "CODEX_HOME", base), \
                mock.patch.object(registry, "codex_held_locks", return_value={"held"}):
            self.assertEqual(registry.codex_live_ids(), {"held"})
        with mock.patch.object(registry, "CODEX_HOME", base), \
                mock.patch.object(registry, "codex_held_locks", return_value=None):   # the Mac: files are all we have
            self.assertEqual(registry.codex_live_ids(), {"held", "stale", "other"})


class ClaudeModels(unittest.TestCase):
    def test_catalog_from_the_initialize_answer_cached_per_login(self):
        answer = {"type": "control_response", "response": {"subtype": "success", "response": {"models": [
            {"value": "default", "displayName": "Default"},
            {"value": "opus", "displayName": "Opus 5.5", "supportedEffortLevels": ["low", "max"]},
            {"value": "haiku", "displayName": "Haiku 4.5"}]}}}
        asked = []

        def claude(said):   # the claude CLI is the boundary: it answers one control request on stdin
            def run(cmd, **kw):
                asked.append(json.loads(kw["input"])["request"])
                return subprocess.CompletedProcess(cmd, 0, json.dumps(said) + "\n", "")
            return run
        with mock.patch.object(switch.config, "STATE_DIR", Path(tempfile.mkdtemp())):
            models = switch.claude_models("work", CFG, run=claude(answer))
            self.assertEqual(switch.claude_models("work", CFG, run=claude(answer)), models)   # cached
        self.assertEqual(models, [{"value": "opus", "label": "Opus 5.5", "efforts": ["low", "max"]},
                                  {"value": "haiku", "label": "Haiku 4.5", "efforts": []}])
        self.assertEqual(asked, [{"subtype": "initialize"}])

    def test_a_failed_probe_is_an_error_not_a_made_up_catalog(self):
        """A probe that fails or lists no models is an error, so the board never shows a made-up catalog."""
        refused = {"type": "control_response", "response": {"subtype": "error", "error": "not supported"}}
        empty = {"type": "control_response", "response": {"subtype": "success", "response": {"models": []}}}

        def claude(said=None, error=None):   # the claude CLI is the boundary: it answers one control request
            def run(cmd, **kw):
                if error:
                    raise error
                return subprocess.CompletedProcess(cmd, 0, json.dumps(said) + "\n", "")
            return run
        with mock.patch.object(switch.config, "STATE_DIR", Path(tempfile.mkdtemp())):
            for run, msg in ((claude(refused), "claude: not supported"),
                             (claude(error=subprocess.TimeoutExpired("claude", 60)), "TimeoutExpired"),
                             (claude(empty), "listed no models")):
                with self.subTest(msg=msg), self.assertRaisesRegex(switch.SwitchError,
                                                                   "model catalog of Claude login personal.*" + msg):
                    switch.claude_models("personal", CFG, run=run)
            self.assertFalse((switch.config.STATE_DIR / "models-cache.json").exists())   # nothing cached


class ModelCache(unittest.TestCase):
    @unittest.skipIf(os.geteuid() == 0, "root writes into a folder whatever its mode")
    def test_a_model_cache_that_cannot_be_written_raises(self):
        answer = {"type": "control_response", "response": {"subtype": "success", "response": {"models": [
            {"value": "opus", "displayName": "Opus 5.5", "supportedEffortLevels": ["low"]}]}}}

        def claude(cmd, **kw):   # the claude CLI is the boundary: it answers one control request on stdin
            return subprocess.CompletedProcess(cmd, 0, json.dumps(answer) + "\n", "")
        state = Path(tempfile.mkdtemp())
        state.chmod(0o500)
        self.addCleanup(state.chmod, 0o700)
        with mock.patch.object(switch.config, "STATE_DIR", state), self.assertRaises(PermissionError):
            switch.claude_models("work", CFG, run=claude)


class CodexState(unittest.TestCase):
    def test_threads_and_rollout_from_a_wal_database_codex_has_open(self):
        from agentmail import registry
        home = Path(tempfile.mkdtemp())
        con = sqlite3.connect(str(home / "state_5.sqlite"))   # Codex itself, holding its database open
        con.execute("pragma journal_mode=wal")
        con.execute("create table threads (id text, title text, cwd text, archived int, updated_at int, rollout_path text)")
        con.executemany("insert into threads values (?, ?, ?, ?, ?, ?)",
                        [("t1", "fix it", "/w", 0, 2, "/r/t1.jsonl"), ("t2", "old", "/w", 1, 1, "/r/t2.jsonl"),
                         ("t3", "new", "/w", 0, 3, None)])
        con.commit()
        self.addCleanup(con.close)
        with mock.patch.object(registry, "CODEX_HOME", home):
            self.assertEqual(list(registry.codex_threads()), ["t3", "t1"])   # newest first, archived left out
            self.assertEqual(registry.codex_rollout("t1"), Path("/r/t1.jsonl"))
            self.assertIsNone(registry.codex_rollout("t3"))
            self.assertIsNone(registry.codex_rollout("nope"))
        empty = Path(tempfile.mkdtemp())
        with mock.patch.object(registry, "CODEX_HOME", empty):
            self.assertIsNone(registry.codex_rollout("t1"))
            self.assertEqual(registry.codex_threads(), {})
        self.assertEqual(list(empty.iterdir()), [])   # no database is created where Codex has none

    def test_a_database_that_cannot_be_read_is_an_error_not_no_conversation(self):
        from agentmail import registry
        home = Path(tempfile.mkdtemp())
        (home / "state_5.sqlite").write_bytes(b"not a database" * 100)
        with mock.patch.object(registry, "CODEX_HOME", home), self.assertRaises(sqlite3.DatabaseError):
            registry.codex_rollout("t1")
        # nor is it "no threads": every offline Codex session would vanish from lookups
        with mock.patch.object(registry, "CODEX_HOME", home), \
                self.assertRaisesRegex(sqlite3.DatabaseError, "%s: file is not a database" % (home / "state_5.sqlite")):
            registry.codex_threads()


class Store(unittest.TestCase):
    def test_rehome_moves_the_tags(self):
        from agentmail.store import Store as S
        st = S(Path(tempfile.mkdtemp()) / "m.sqlite")
        row = st.session_for("claude", "old-sid", "agent-x", "/w", origin_host=HOST)
        self.assertEqual(st.rehome("claude", "old-sid", "codex", "new-thread", "agent-x", "/w", origin_host=HOST), 1)
        moved = st.session_by_tag(row["tag"])
        self.assertEqual((moved["cli"], moved["session_id"]), ("codex", "new-thread"))
        self.assertEqual(st.session_for("codex", "new-thread", origin_host=HOST)["tag"], row["tag"])   # its next mail reuses the tag
        st.session_for("claude", "other", "b", "/w", origin_host=HOST)
        self.assertEqual(st.rehome("claude", "other", "codex", "new-thread", origin_host=HOST), 0)   # never clobber an existing one
        self.assertEqual(st.rehome("claude", "never-sent-mail", "codex", "t3", origin_host=HOST), 0)


class Cli(unittest.TestCase):
    def test_exit_codes_failover_state_and_status_log(self):
        """Every switch that touched the agent is in the status log, a failed one marked FAILED; a busy agent or a
        refusal touched nothing and is not."""
        sw = load_script("agent-switch")
        state = Path(tempfile.mkdtemp()) / "failover.json"
        state.write_text(json.dumps({"sessions": {"sid-1": {"home": "personal"}, "sid-2": {}}}))
        homes.status_log(self)
        # switch.switch would stop a live agent (Switching drives it); helpers would write trust and the store
        with mock.patch.object(sw.failover.STATE, "path", state), \
                mock.patch.object(sw, "helpers", return_value=(None, None, None)):
            for res, code in (({"ok": False, "needs_force": True, "detail": "busy", "from": "a", "to": "b"}, 3),
                              ({"ok": False, "needs_force": False, "detail": "pid 9 did not exit after SIGKILL",
                                "from": "a", "to": "b"}, 1),
                              ({"ok": True, "needs_force": False, "detail": "moved", "from": "a", "to": "b"}, 0)):
                with mock.patch.object(sw.switch, "switch", return_value=dict(res, id="sid-1")), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(sw.main(["agent-x", "--to", "claude:work"]), code)
            with mock.patch.object(sw.switch, "switch", side_effect=sw.switch.SwitchError("no live agent")), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(sw.main(["agent-x", "--to", "codex", "--json"]), 2)
            self.assertEqual(json.loads(out.getvalue())["refused"], True)
        self.assertEqual([(e["event"], e["session_id"], e["text"]) for e in sw.statuslog.read()],
                         [("switch", "sid-1", "a -> b: FAILED pid 9 did not exit after SIGKILL"),
                          ("switch", "sid-1", "a -> b: moved")])
        sessions = json.loads(state.read_text())["sessions"]
        self.assertEqual((sessions["sid-2"], list(sessions["sid-1"])), ({}, ["switched_at"]))   # by hand: forgotten

    def test_failover_takes_a_switch_by_hand_as_the_agents_last_move(self):
        """agent-failover moved the agent off personal and back; then personal hit its five-hour limit with work
        out too, and the owner switched it to work by hand. Until the resumed agent's first reply, personal's
        limit is its transcript's latest record, and personal reads as having headroom (as a stale reading
        would): failover must take that record as belonging to the login it left, not move it off the owner's
        choice. It forgets the first login (no move back), and the switch does not count toward
        failover.max_moves_per_hour."""
        sw = load_script("agent-switch")
        sid, before = "3d3d3d3d-0000-4000-8000-000000000001", time.time()
        state = Path(tempfile.mkdtemp()) / "failover.json"
        state.write_text(json.dumps({"sessions": {sid: {"home": "personal", "current": "personal", "moves": [
            [before - 3000, "personal", "work", "limit: five-hour limit"],
            [before - 600, "work", "personal", "switch back: personal has reset"]]}}}))
        homes.status_log(self)
        res = {"ok": True, "needs_force": False, "detail": "moved", "from": "personal", "to": "work", "id": sid}
        # switch.switch would stop a live agent (Switching drives it); helpers would write trust and the store
        with mock.patch.object(failover.STATE, "path", state), \
                mock.patch.object(sw, "helpers", return_value=(None, None, None)), \
                mock.patch.object(sw.switch, "switch", return_value=res), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sw.main(["agent-x", "--to", "claude:work"]), 0)
            after = time.time()
            st = failover.STATE.read()
        self.assertNotIn("home", st["sessions"].get(sid, {}))
        usage = {"windows": [{"kind": "session", "name": "5-hour", "remaining_pct": 90, "resets_at": after + 3600,
                              "model": None}], "cli": "claude"}
        snaps = {"claude:personal": dict(usage, account="personal"), "claude:work": dict(usage, account="work")}
        agent = {"id": sid, "name": "agent-x", "account": "work", "status": "busy", "model": "claude-opus-5-5"}
        limit = {"window": "five_hour", "resets_at": after + 3600, "text": ""}
        self.assertEqual(failover.decide([agent], {sid: dict(limit, at=before - 60)}, snaps, st, CFG, after),
                         ([], []))
        idle = dict(agent, status="idle", updated_at=after - 3600, waiting_for="")
        self.assertEqual(failover.decide([idle], {}, snaps, st, CFG, after + 3600), ([], []))   # never moved back
        once = dict(CFG, failover={"max_moves_per_hour": 1})
        moves, _ = failover.decide([agent], {sid: dict(limit, at=after + 1)}, snaps, st, once, after + 2)
        self.assertEqual([m["to"] for m in moves], ["personal"])   # a limit on work itself still moves it


if __name__ == "__main__":
    unittest.main()
