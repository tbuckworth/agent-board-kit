import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentmail import switch  # noqa: E402

WORK = tempfile.mkdtemp()
Path(WORK, ".credentials.json").write_text("{}")
CFG = {"usage": {"claude_accounts": {"personal": {"config_dir": None}, "work": {"config_dir": WORK}},
                 "order": ["claude:personal", "claude:work", "codex"]},
       "spawn_claude_model": "claude-opus-5-5", "spawn_claude_effort": "xhigh",
       "spawn_codex_model": "gpt-6.1-sol", "spawn_codex_effort": "xhigh"}
CLAUDE_MODELS = [{"value": "opus", "label": "Opus 5.5", "efforts": ["low", "medium", "high", "xhigh", "max"]},
                 {"value": "sonnet", "label": "Sonnet 5.5", "efforts": ["low", "medium", "high", "xhigh", "max"]},
                 {"value": "haiku", "label": "Haiku 4.5", "efforts": []}]
CODEX_MODELS = [{"value": "gpt-6.1-sol", "label": "GPT-6.1-Sol", "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"]}]


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROOT / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def write_jsonl(entries):
    f = Path(tempfile.mkdtemp()) / "t.jsonl"
    f.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return f


class Reading(unittest.TestCase):
    def test_flags(self):
        argv = ["claude", "--name", "a", "--model", "claude-opus-5-5", "--effort=high", "brief"]
        self.assertEqual(switch.flag_value(argv, "--model"), "claude-opus-5-5")
        self.assertEqual(switch.flag_value(argv, "--effort"), "high")
        self.assertIsNone(switch.flag_value(argv, "-m"))
        cx = ["codex", "-s", "danger-full-access", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=xhigh"]
        self.assertEqual(switch.flag_value(cx, "-m", "--model"), "gpt-6.1-sol")
        self.assertEqual(switch.codex_effort(cx), "xhigh")
        self.assertEqual(switch.codex_effort(["codex", "-c", "model_reasoning_effort='low'"]), "low")

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
                                  [{"pid": 5, "command": "sleep 9", "output": "/o"}])
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
        self.assertEqual(switch.codex_models(Path("/nonexistent")), [])


def agent(cli="claude", account="personal", model="claude-opus-5-5", effort="xhigh", busy=False, movable=True):
    return {"name": "agent-x", "id": "11111111-1111-1111-1111-111111111111", "cli": cli, "account": account,
            "cwd": "/w", "status": "busy" if busy else "idle", "live": True, "model": model, "effort": effort,
            "pane": "%5" if movable else None, "pid": 999, "movable": movable,
            "why_not": "" if movable else "not in tmux", "busy": busy, "background_jobs": 0}


class Switching(unittest.TestCase):
    def run_switch(self, st, **kw):
        calls = {}
        sess = kw.pop("sess", None) or {"id": st["id"], "name": st["name"], "cli": st["cli"], "account": st["account"],
                                        "live": True}
        patches = [mock.patch.object(switch, "find", return_value=sess),
                   mock.patch.object(switch, "state", return_value=st),
                   mock.patch.object(switch, "claude_models", return_value=CLAUDE_MODELS),
                   mock.patch.object(switch, "codex_models", return_value=CODEX_MODELS),
                   mock.patch.object(switch.failover, "migrate",
                                     side_effect=lambda *a, **k: calls.setdefault("migrate", (a, k)) and (True, "moved")),
                   mock.patch.object(switch, "relaunch_codex",
                                     side_effect=lambda *a: calls.setdefault("codex", a) and (True, "resumed")),
                   mock.patch.object(switch, "stop", side_effect=lambda pid: calls.setdefault("stop", pid) or True),
                   mock.patch.object(switch.failover, "transcript", return_value=None),
                   mock.patch.object(switch, "codex_rollout", return_value=None),
                   mock.patch.object(switch.failover, "background_jobs", return_value=[]),
                   mock.patch.object(switch, "write_handoff", return_value=Path("/h/agent-x-handoff.md")),
                   mock.patch.object(switch, "start_codex",
                                     side_effect=lambda st_, m, e, brief, codex, trust=None:
                                     calls.setdefault("start_codex", (m, e, brief)) and kw.pop("new_id", "thread-2")),
                   mock.patch.object(switch, "start_claude",
                                     side_effect=lambda st_, acct, m, e, brief, cfg, claude, trust=None:
                                     calls.setdefault("start_claude", (acct, m, e, brief)) and kw.pop("new_id", "sess-2"))]
        rehome = mock.Mock(return_value=1)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            res = switch.switch("agent-x", CFG, rehome=rehome, preamble=lambda st_, cli: "PREAMBLE ",
                                claude="/bin/claude", codex="/bin/codex",
                                **{k: v for k, v in kw.items() if k != "new_id"})
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
        bg = dict(agent(model="claude-fable-5-1", effort="high"), pane=switch.failover.BG)
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
        acct, model, effort, brief = calls["start_claude"]
        self.assertEqual((acct, model, effort), ("work", "opus", "max"))
        self.assertEqual(rehome.call_args[0][2:4], ("claude", "sess-2"))

    def test_a_handoff_whose_new_session_never_shows_is_reported(self):
        res, calls, rehome = self.run_switch(agent(), to="codex", new_id=None)
        self.assertFalse(res["ok"])
        self.assertIn("did not show up", res["detail"])
        rehome.assert_not_called()

    def test_dry_run_stops_nothing(self):
        for kw in ({"to": "codex"}, {"to": "claude:work"}, {"model": "sonnet"}):
            res, calls, _ = self.run_switch(agent(), dry_run=True, **kw)
            self.assertTrue(res["ok"])
            self.assertTrue(res["detail"].startswith("would"), res["detail"])
            self.assertEqual(calls, {})


class Chained(unittest.TestCase):
    def test_history_survives_a_second_switch(self):
        d = Path(tempfile.mkdtemp()) / "agent-switch"
        d.mkdir()
        first = d / "agent-x-1-handoff.md"
        first.write_text(switch.handoff_text({"name": "agent-x", "id": "x" * 36, "cwd": "/w"}, "Claude", "Codex",
                                             "/t.jsonl", "Remember KESTREL. Do the task.",
                                             ["[assistant] step one", "[tool] Bash: make"], []))
        handoff_brief = switch.handoff_brief({"name": "agent-x", "id": "x" * 36, "cwd": "/w"}, first, "Claude", "Codex")
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
        sess = {"id": "c0c0c0c0-0000-4000-8000-000000000003", "name": "bgx", "cli": "claude", "account": "personal",
                "live": True, "pid": 999, "kind": "bg", "tmux": None, "status": "idle", "cwd": "/w"}
        with mock.patch.object(switch.failover, "cmdline", return_value=["claude", "bg-spare", "--bg-spare", "/s"]), \
                mock.patch.object(switch.failover, "transcript", return_value=path), \
                mock.patch.object(switch.failover, "background_jobs", return_value=[]), \
                mock.patch.object(switch, "pane_busy", side_effect=AssertionError("a bg session has no pane")):
            st = switch.state(sess, CFG)
        self.assertEqual((st["model"], st["effort"], st["movable"], st["pane"], st["busy"]),
                         ("claude-fable-5-1", "high", True, switch.failover.BG, False))


class Relaunch(unittest.TestCase):
    def test_codex_resume_line_keeps_sandbox_and_folder(self):
        st = agent("codex", None, "gpt-6.1-sol", "xhigh")
        typed = []
        with mock.patch.object(switch.failover, "cmdline", return_value=[
                "codex", "-s", "danger-full-access", "-a", "never", "-C", "/w", "-m", "gpt-6.1-sol",
                "-c", "model_reasoning_effort=xhigh"]), \
                mock.patch.object(switch, "stop", return_value=True), \
                mock.patch.object(switch, "type_line", side_effect=lambda pane, sess, line, *a, **k: typed.append(line)), \
                mock.patch.object(switch, "codex_pane", return_value=("%5", 1234)):
            ok, detail = switch.relaunch_codex(st, "gpt-6.1-sol", "ultra", None, "/bin/codex")
        self.assertTrue(ok)
        self.assertEqual(typed[0], "stty sane; unset CLAUDE_CONFIG_DIR; /bin/codex resume %s -s danger-full-access "
                                   "-a never -C /w -c check_for_update_on_startup=false -m gpt-6.1-sol "
                                   "-c model_reasoning_effort=ultra" % st["id"])


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


class Store(unittest.TestCase):
    def test_rehome_moves_the_tags(self):
        from agentmail.store import Store as S
        st = S(Path(tempfile.mkdtemp()) / "m.sqlite")
        row = st.session_for("claude", "old-sid", "agent-x", "/w")
        self.assertEqual(st.rehome("claude", "old-sid", "codex", "new-thread", "agent-x", "/w"), 1)
        moved = st.session_by_tag(row["tag"])
        self.assertEqual((moved["cli"], moved["session_id"]), ("codex", "new-thread"))
        self.assertEqual(st.session_for("codex", "new-thread")["tag"], row["tag"])   # its next mail reuses the tag
        st.session_for("claude", "other", "b", "/w")
        self.assertEqual(st.rehome("claude", "other", "codex", "new-thread"), 0)   # never clobber an existing one
        self.assertEqual(st.rehome("claude", "never-sent-mail", "codex", "t3"), 0)


class Cli(unittest.TestCase):
    def test_exit_codes_and_failover_state(self):
        sw = load_script("agent-switch")
        state = Path(tempfile.mkdtemp()) / "failover.json"
        state.write_text(json.dumps({"sessions": {"sid-1": {"home": "personal"}, "sid-2": {}}}))
        sw.FAILOVER_STATE = state
        with mock.patch.object(sw, "helpers", return_value=(None, None, None)), \
                mock.patch.object(sw.statuslog, "append"), \
                mock.patch.object(sw.switch, "find", return_value={"id": "sid-1"}):
            for res, code in (({"ok": False, "needs_force": True, "detail": "busy", "from": "a", "to": "b"}, 3),
                              ({"ok": True, "needs_force": False, "detail": "moved", "from": "a", "to": "b"}, 0),
                              ({"ok": False, "needs_force": False, "detail": "failed", "from": "a", "to": "b"}, 1)):
                with mock.patch.object(sw.switch, "switch", return_value=res), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(sw.main(["agent-x", "--to", "claude:work"]), code)
            with mock.patch.object(sw.switch, "switch", side_effect=sw.switch.SwitchError("no live agent")), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(sw.main(["agent-x", "--to", "codex", "--json"]), 2)
            self.assertEqual(json.loads(out.getvalue())["refused"], True)
        self.assertEqual(json.loads(state.read_text())["sessions"], {"sid-2": {}})   # moved by hand: forgotten


if __name__ == "__main__":
    unittest.main()
