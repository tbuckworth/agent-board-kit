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
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentmail import config, failover, registry, usage  # noqa: E402

NOW = time.time()
WORK = tempfile.mkdtemp()
Path(WORK, ".credentials.json").write_text("{}")
CFG = {"usage": {"claude_accounts": {"personal": {"config_dir": None}, "work": {"config_dir": WORK}},
                 "order": ["claude:personal", "claude:work", "codex"]},
       "spawn_claude_model": "claude-opus-5-5", "failover": {"idle_seconds": 300, "pinned": ["nightly-*"]}}
SID = "3d3d3d3d-0000-4000-8000-000000000001"


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROOT / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def rejection(resets_at, window="five_hour", status="rejected"):
    """The entry Claude Code 2.1.291 writes when a plan limit stops a turn (shape seen in real transcripts)."""
    return json.dumps({"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit", "apiErrorStatus": 429,
                       "sessionId": SID, "timestamp": "2026-10-06T14:00:00Z",
                       "quotaLimits": {"status": status, "resetsAt": resets_at, "rateLimitType": window,
                                       "overageStatus": "rejected", "isUsingOverage": False},
                       "message": {"model": "<synthetic>", "content": [
                           {"type": "text", "text": "You've hit your session limit · resets 3pm (Etc/UTC)"}]}})


def entry(kind, text="x", **kw):
    return json.dumps(dict({"type": kind, "message": {"content": [{"type": "text", "text": text}]}}, **kw))


# The two wordings of Claude Code's "model bucket used up" stop, exactly as Claude Code writes them: a session's
# (Fable 5.1 on the personal login) and `claude -p`'s (as a hook's own headless run reports it).
SESSION_CREDITS = "You're out of usage credits. Run /usage-credits to keep using Fable 5.1 or /model to switch models."
CLI_CREDITS = ("You're out of usage credits. Switch to another model, or manage usage credits at "
               "https://claude.ai/settings/usage?from=cc_cli_limit_message, to continue.")


def credits_record(text=SESSION_CREDITS, api_error="model_requires_usage_credits", at="2026-10-07T18:30:26.112Z"):
    """The entry Claude Code 2.1.291 writes when a login's weekly Fable bucket runs out: no quotaLimits, no reset
    time."""
    d = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit", "apiErrorStatus": 429,
         "errorDetails": "429 {\"type\":\"error\",\"error\":{\"type\":\"rate_limit_error\",\"message\":\"This request "
                         "would exceed your account's rate limit. Please try again later.\"}}",
         "sessionId": SID, "sessionKind": "bg", "timestamp": at, "perTurnEffort": "high",
         "message": {"model": "<synthetic>", "role": "assistant", "content": [{"type": "text", "text": text}]}}
    if api_error:
        d["apiError"] = api_error
    return json.dumps(d)


def reply(model="claude-fable-5-1", effort="high"):
    return json.dumps({"type": "assistant", "perTurnEffort": effort, "message": {
        "model": model, "content": [{"type": "text", "text": "working"}]}})


def fable(remaining, resets_at=None):
    return {"kind": "model_weekly", "name": "weekly Fable", "used_pct": 100 - remaining, "remaining_pct": remaining,
            "resets_at": NOW + 3600 if resets_at is None else resets_at, "model": "fable"}


def fable_snaps(personal=0, work=25, personal_resets=None):
    s = snaps()
    s["claude:personal"]["windows"].append(fable(personal, personal_resets))
    s["claude:work"]["windows"].append(fable(work))
    return s


def win(kind, remaining):
    return {"kind": kind, "name": kind, "used_pct": 100 - remaining, "remaining_pct": remaining,
            "resets_at": NOW + 3600, "model": None}


def snaps(personal=90, work=90, work_extra=False):
    return {"claude:personal": {"windows": [win("session", personal)], "cli": "claude", "account": "personal"},
            "claude:work": {"windows": [win("session", work)], "cli": "claude", "account": "work",
                             "extra_usage": {"enabled": work_extra}}}


def sess(account="personal", status="busy", idle_for=0, sid=SID, name="agent-x"):
    return {"id": sid, "name": name, "account": account, "status": status, "live": True, "pid": 999,
            "tmux": "agent-x:@1.%5", "cwd": "/work", "updated_at": NOW - idle_for, "waiting_for": "",
            "config_dir": WORK if account == "work" else None}


class Detect(unittest.TestCase):
    def test_a_rejection_at_the_end_is_a_limit(self):
        hit = failover.limit_hit([entry("user"), entry("assistant"), rejection(NOW + 600),
                                  json.dumps({"type": "system", "subtype": "x"})], now=NOW)
        self.assertEqual((hit["window"], hit["resets_at"]), ("five_hour", NOW + 600))
        self.assertIn("session limit", hit["text"])

    def test_not_a_limit(self):
        self.assertIsNone(failover.limit_hit([rejection(NOW - 1)], now=NOW))                    # already reset
        self.assertIsNone(failover.limit_hit([rejection(NOW + 600), entry("user")], now=NOW))   # someone went on
        self.assertIsNone(failover.limit_hit([rejection(NOW + 600, status="allowed")], now=NOW))  # a transient 429
        self.assertIsNone(failover.limit_hit([entry("assistant")], now=NOW))
        self.assertIsNone(failover.limit_hit(["not json", ""], now=NOW))

    def test_relaunch_flags_keep_model_effort_permissions_not_the_prompt(self):
        argv = ["/home/t/.local/bin/claude", "--name", "agent-x", "--dangerously-skip-permissions", "--model",
                "claude-opus-5-5", "--effort", "xhigh", "--resume", "old", "You were started by agent-spawn..."]
        self.assertEqual(failover.relaunch_flags(argv), ["--dangerously-skip-permissions", "--model",
                                                         "claude-opus-5-5", "--effort", "xhigh"])
        self.assertEqual(failover.relaunch_flags(["claude", "--model=opus", "hi"]), ["--model=opus"])
        self.assertEqual(failover.pane_id({"tmux": "usage-aware-routing:@7.%7"}), "%7")
        self.assertIsNone(failover.pane_id({"tmux": None}))

    def test_launch_line(self):
        line = failover.launch_line("/bin/claude", SID, "a b", ["--model", "m"], "/h/.claude-work", "it's done")
        self.assertEqual(line, "stty sane; export CLAUDE_CONFIG_DIR=/h/.claude-work; /bin/claude --resume %s "
                               "--name 'a b' --model m 'it'\"'\"'s done'" % SID)
        self.assertTrue(failover.launch_line("c", SID, "n", [], None, None).startswith("stty sane; unset CLAUDE_CONFIG_DIR; "))
        self.assertFalse(failover.launch_line("c", SID, "n", [], None, None).endswith("'"))


class Credits(unittest.TestCase):
    """A model's own weekly bucket (Fable) used up: the limit that is easy to miss, since it has no reset time."""

    def test_the_fable_credits_record_is_a_limit(self):
        hit = failover.limit_hit([reply(), entry("user", "carry on"), credits_record(),
                                  json.dumps({"type": "system", "subtype": "turn_duration"})], now=NOW)
        self.assertEqual((hit["window"], hit["resets_at"], hit["model"]), ("model_credits", None, "Fable 5.1"))
        self.assertEqual(hit["text"], SESSION_CREDITS)
        self.assertEqual(hit["at"], usage.iso_epoch("2026-10-07T18:30:26.112Z"))

    def test_the_cli_wording_is_a_limit_too(self):
        hit = failover.limit_hit([credits_record(CLI_CREDITS, api_error=None)], now=NOW)
        self.assertEqual(hit["window"], "model_credits")
        self.assertEqual(hit["text"], CLI_CREDITS[:160])
        self.assertIsNone(hit["model"])

    def test_a_hook_quoting_the_message_is_not_a_limit(self):
        """A review hook's own `claude -p` ran out of Fable and the hook passed its output on as a
        systemMessage. The agent's login was fine; its next reply went through."""
        hook = json.dumps({"type": "attachment", "attachment": {
            "type": "async_hook_response", "hookName": "PostToolUse:Bash", "hookEvent": "PostToolUse",
            "response": {"systemMessage": CLI_CREDITS + "\n"}, "stdout": json.dumps({"systemMessage": CLI_CREDITS})}})
        stopped = json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [
            {"type": "text", "text": "I've stopped partway through the refactor because this session ran out "
                                     "of usage credits."}]}})
        self.assertIsNone(failover.limit_hit([reply("claude-opus-5-5"), hook, stopped], now=NOW))
        self.assertIsNone(failover.limit_hit([reply("claude-opus-5-5"), hook], now=NOW))
        self.assertIsNone(failover.limit_hit([entry("user", CLI_CREDITS)], now=NOW))   # quoted by a person

    def test_session_model_and_recorded_flags(self):
        lines = [json.dumps({"type": "permission-mode", "permissionMode": "bypassPermissions"}),
                 reply("claude-fable-5-1", "high"), credits_record()]
        self.assertEqual(failover.session_model(lines), "claude-fable-5-1")   # not the error's "<synthetic>"
        self.assertEqual(failover.transcript_flags(lines), ["--model", "claude-fable-5-1", "--effort", "high",
                                                            "--permission-mode", "bypassPermissions"])
        self.assertIsNone(failover.session_model([credits_record()]))
        self.assertEqual(failover.fill_flags(["--dangerously-skip-permissions", "--model", "m"],
                                             ["--model", "x", "--effort", "high", "--permission-mode", "plan"]),
                         ["--dangerously-skip-permissions", "--model", "m", "--effort", "high"])

    def hit(self, **kw):
        return {SID: dict(failover.limit_hit([credits_record(**kw)], now=NOW))}

    def fable_agent(self, *a, **kw):
        return dict(sess(*a, **kw), model="claude-fable-5-1", kind="bg", tmux=None)

    def test_a_fable_agent_moves_to_a_login_with_fable_left(self):
        moves, notes = failover.decide([self.fable_agent()], self.hit(), fable_snaps(), {}, CFG, NOW)
        self.assertEqual([(m["to"], m["resets_at"]) for m in moves], [("work", NOW + 3600)])
        self.assertIn("out of Fable 5.1 usage credits, weekly Fable resets", moves[0]["prompt"])
        self.assertIn("moved to login 'work'", moves[0]["prompt"])
        self.assertEqual(notes, [])

    def test_no_login_with_fable_left_waits_and_is_noted_once(self):
        hit = self.hit()
        moves, notes = failover.decide([self.fable_agent()], hit, fable_snaps(work=0), {}, CFG, NOW)
        self.assertEqual(moves, [])
        self.assertIn("no other Claude login has headroom for claude-fable-5-1", notes[0][2])
        state = {"sessions": {SID: {"stuck_until": notes[0][1]}}}
        self.assertEqual(failover.decide([self.fable_agent()], hit, fable_snaps(work=0), state, CFG, NOW), ([], []))

    def test_an_opus_agent_does_not_care_about_fable(self):
        plan = {SID: {"window": "five_hour", "resets_at": NOW + 3600, "text": ""}}
        s = fable_snaps(personal=0, work=0)
        s["claude:personal"]["windows"][0] = win("session", 0)
        moves, _ = failover.decide([dict(sess(), model="claude-opus-5-5")], plan, s, {}, CFG, NOW)
        self.assertEqual([m["to"] for m in moves], ["work"])

    def test_a_record_from_before_the_weekly_reset_is_stale(self):
        """Stuck at 18:30 on Fable 0%; the bucket reset at 06:00 and has headroom: nothing to move."""
        s = fable_snaps(personal=96, personal_resets=NOW + 7 * 86400 - 3600)   # last reset an hour ago
        moves, notes = failover.decide([self.fable_agent()], {SID: dict(self.hit()[SID], at=NOW - 7200)}, s, {},
                                       CFG, NOW)
        self.assertEqual((moves, notes), ([], []))
        moves, _ = failover.decide([self.fable_agent()], {SID: dict(self.hit()[SID], at=NOW - 600)}, s, {}, CFG, NOW)
        self.assertEqual([m["to"] for m in moves], ["work"])   # after the reset, yet refused: trust the record

    def test_no_switch_back_while_home_has_no_fable(self):
        state = {"sessions": {SID: {"home": "personal", "current": "work", "moves": []}}}
        idle = self.fable_agent("work", "idle", 600)
        self.assertEqual(failover.decide([idle], {}, fable_snaps(personal=0), state, CFG, NOW)[0], [])
        moves, _ = failover.decide([idle], {}, fable_snaps(personal=96), state, CFG, NOW)
        self.assertEqual([(m["to"], m["prompt"]) for m in moves], [("personal", None)])


class Decide(unittest.TestCase):
    def decide(self, sessions, limits=None, s=None, state=None, cfg=CFG):
        return failover.decide(sessions, limits or {}, s or snaps(), state or {}, cfg, NOW)

    def test_a_limited_agent_moves_to_the_next_login_with_headroom(self):
        moves, notes = self.decide([sess()], {SID: {"window": "five_hour", "resets_at": NOW + 3600, "text": ""}},
                                   snaps(personal=0))
        self.assertEqual([(m["to"], m["resets_at"]) for m in moves], [("work", NOW + 3600)])
        self.assertIn("'personal' hit its usage limit (five-hour limit", moves[0]["prompt"])
        self.assertIn("moved to login 'work'", moves[0]["prompt"])
        self.assertEqual(notes, [])

    def test_nowhere_to_go_is_noted_once(self):
        hit = {SID: {"window": "seven_day", "resets_at": NOW + 3600, "text": ""}}
        moves, notes = self.decide([sess()], hit, snaps(personal=0, work=0))
        self.assertEqual(moves, [])
        self.assertIn("no other Claude login has headroom", notes[0][2])
        moves, notes = self.decide([sess()], hit, snaps(personal=0, work=0),
                                   {"sessions": {SID: {"stuck_until": NOW + 3600}}})
        self.assertEqual(notes, [])

    def test_nothing_is_pinned_by_default(self):
        self.assertEqual(failover.DEFAULTS["pinned"], [])
        self.assertFalse(failover.pinned({"name": "nightly-report"}, failover.settings({})))
        self.assertTrue(failover.pinned({"name": "nightly-report"}, failover.settings(CFG)))

    def test_a_pinned_agent_waits_on_its_own_login(self):
        hit = {SID: {"window": "five_hour", "resets_at": NOW + 3600, "text": ""}}
        moves, notes = self.decide([sess(name="nightly-report")], hit, snaps(personal=0))
        self.assertEqual(moves, [])
        self.assertIn("pinned there (failover.pinned), so it waits for the reset", notes[0][2])
        moves, notes = self.decide([sess(name="nightly-report")], hit, snaps(personal=0),
                                   {"sessions": {SID: {"stuck_until": NOW + 3600}}})
        self.assertEqual((moves, notes), ([], []))                     # noted once
        cfg = dict(CFG, failover={"pinned": ["keep-*"]})
        self.assertEqual(self.decide([sess(name="keep-me")], hit, snaps(personal=0), cfg=cfg)[0], [])
        self.assertEqual(len(self.decide([sess(name="nightly-report")], hit, snaps(personal=0), cfg=cfg)[0]), 1)
        # nor is one moved back or rebalanced
        idle = sess(account="work", status="idle", idle_for=3600, name="nightly-report")
        state = {"sessions": {SID: {"home": "personal", "resets_at": NOW - 60}}}
        self.assertEqual(self.decide([idle], {}, snaps(), state), ([], []))

    def test_a_login_not_set_up_is_never_a_target(self):
        cfg = json.loads(json.dumps(CFG))
        cfg["usage"]["claude_accounts"]["work"]["config_dir"] = "/nonexistent/claude-work"
        moves, notes = self.decide([sess()], {SID: {"window": "five_hour", "resets_at": NOW + 60, "text": ""}},
                                   snaps(personal=0), cfg=cfg)
        self.assertEqual(moves, [])
        self.assertEqual(len(notes), 1)

    def test_switch_back_after_the_reset_when_idle(self):
        state = {"sessions": {SID: {"home": "personal", "current": "work", "resets_at": NOW - 60, "moves": []}}}
        moves, _ = self.decide([sess("work", "idle", 600)], state=state)
        self.assertEqual([(m["to"], m["prompt"]) for m in moves], [("personal", None)])   # no prompt: it just waits
        for s, st in ((sess("work", "busy", 600), state),                                # never interrupt a turn
                      (sess("work", "idle", 60), state),                                 # not idle long enough
                      (sess("work", "idle", 600), {"sessions": {SID: dict(state["sessions"][SID], resets_at=NOW + 60)}})):
            self.assertEqual(self.decide([s], state=st)[0], [])
        self.assertEqual(self.decide([sess("work", "idle", 600)], s=snaps(personal=0), state=state)[0], [])
        waiting = dict(sess("work", "idle", 600), waiting_for="permission")
        self.assertEqual(self.decide([waiting], state=state)[0], [])

    def test_no_switch_back_to_a_login_ranked_lower(self):
        state = {"sessions": {SID: {"home": "work", "current": "personal", "moves": []}}}
        self.assertEqual(self.decide([sess("personal", "idle", 600)], state=state)[0], [])

    def test_idle_agent_billing_extra_usage_is_rebalanced(self):
        moves, _ = self.decide([sess("work", "idle", 600)], s=snaps(personal=80, work=0, work_extra=True))
        self.assertEqual([(m["to"], m["prompt"]) for m in moves], [("personal", None)])
        self.assertIn("extra usage", moves[0]["reason"])
        self.assertEqual(self.decide([sess("work", "idle", 600)], s=snaps(personal=80, work=0))[0], [])
        self.assertEqual(self.decide([sess("work", "busy", 600)], s=snaps(personal=80, work=0, work_extra=True))[0], [])

    def test_ping_pong_guard(self):
        state = {"sessions": {SID: {"moves": [[NOW - 60 * i, "a", "b", "r"] for i in range(4)]}}}
        moves, _ = self.decide([sess()], {SID: {"window": "five_hour", "resets_at": NOW + 60, "text": ""}},
                               snaps(personal=0), state)
        self.assertEqual(moves, [])

    def test_record_keeps_the_first_login(self):
        m1 = {"session": sess(), "to": "work", "reason": "limit", "resets_at": NOW + 60}
        st = failover.record({}, m1, NOW)
        self.assertEqual(st["sessions"][SID]["home"], "personal")
        self.assertEqual(st["sessions"][SID]["resets_at"], NOW + 60)
        m2 = {"session": sess("work"), "to": "personal", "reason": "back", "resets_at": None}
        st = failover.record(st, m2, NOW + 120)
        e = st["sessions"][SID]
        self.assertEqual((e["home"], e["current"], len(e["moves"])), ("personal", "personal", 2))
        self.assertNotIn("resets_at", e)


class Move(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.personal = self.base / ".claude"
        self.work = self.base / ".claude-work"
        (self.work / "projects").mkdir(parents=True)
        Path(self.work, ".credentials.json").write_text("{}")
        self.proj = self.personal / "projects" / "-work"
        self.proj.mkdir(parents=True)
        self.src = self.proj / (SID + ".jsonl")
        self.src.write_text(entry("user") + "\n" + rejection(NOW + 600) + "\n")
        (self.proj / SID / "subagents").mkdir(parents=True)
        (self.proj / SID / "subagents" / "a.jsonl").write_text("{}\n")
        self.cfg = json.loads(json.dumps(CFG))
        self.cfg["usage"]["claude_accounts"]["work"]["config_dir"] = str(self.work)
        self.cfg["failover"]["launch_wait_seconds"] = 2
        self.patches = [mock.patch.object(registry, "DEFAULT_CLAUDE_HOME", self.personal)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_copy_transcript_and_session_folder(self):
        dst = failover.copy_transcript(self.src, self.work, SID)
        self.assertEqual(dst, self.work / "projects" / "-work" / (SID + ".jsonl"))
        self.assertEqual(dst.read_text(), self.src.read_text())
        self.assertTrue((dst.parent / SID / "subagents" / "a.jsonl").exists())
        os.utime(dst, (time.time() + 100, time.time() + 100))
        with self.assertRaisesRegex(RuntimeError, "newer"):
            failover.copy_transcript(self.src, self.work, SID)

    def run_migrate(self, live_after, dry_run=False, copy_error=None, live_on=None, jobs=(), pid_reused=False):
        calls, killed, trusted = [], [], []

        def kill(pid, sig):
            if sig:
                killed.append((pid, sig))
            elif (pid, failover.signal.SIGKILL) in killed and not pid_reused:
                raise ProcessLookupError(pid)
        s = dict(sess(), cwd=str(self.base))
        live = [{"id": SID, "live": True, "config_dir": str(self.work.resolve()) if live_on is None else live_on}] \
            if live_after else []
        patches = [mock.patch.object(failover, "movable", return_value=("%5", "")),
                   mock.patch.object(failover, "cmdline", return_value=["claude", "--model", "m", "--effort", "high",
                                                                        "--dangerously-skip-permissions", "brief"]),
                   mock.patch.object(failover.os, "kill", side_effect=kill),
                   mock.patch.object(failover, "wait_dead", return_value=True),
                   mock.patch.object(failover, "tmux", side_effect=lambda *a, **k: calls.append(a) or
                                     subprocess.CompletedProcess(a, 0, "", "")),
                   mock.patch.object(failover.registry, "claude_sessions", return_value=live),
                   mock.patch.object(failover.time, "sleep"),
                   mock.patch.object(failover, "repo_root", return_value=str(self.base)),
                   mock.patch.object(failover, "background_jobs", return_value=list(jobs))]
        if copy_error:
            patches.append(mock.patch.object(failover, "copy_transcript", side_effect=copy_error))
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            ok, detail = failover.migrate(s, "work", "carry on", self.cfg, trust=lambda c, d: trusted.append((c, d)),
                                          claude="/bin/claude", dry_run=dry_run)
        return ok, detail, calls, killed, trusted

    def test_migrate_kills_copies_trusts_and_relaunches_in_the_pane(self):
        ok, detail, calls, killed, trusted = self.run_migrate(live_after=True)
        self.assertTrue(ok, detail)
        self.assertEqual(killed, [(999, failover.signal.SIGKILL)])   # SIGKILL: background jobs survive
        self.assertTrue((self.work / "projects" / "-work" / (SID + ".jsonl")).exists())
        self.assertEqual(trusted, [(str(self.base), str(self.work.resolve()))])
        self.assertEqual(calls[0], ("set-environment", "-t", "agent-x", "CLAUDE_CONFIG_DIR", str(self.work.resolve())))
        typed = calls[1][-1]
        self.assertTrue(typed.startswith("stty sane; export CLAUDE_CONFIG_DIR=%s; /bin/claude --resume %s --name agent-x"
                                         % (self.work.resolve(), SID)), typed)
        self.assertIn("--model m --effort high --dangerously-skip-permissions 'carry on'", typed)
        self.assertNotIn("brief", typed)
        self.assertEqual(calls[2], ("send-keys", "-t", "%5", "Enter"))

    def registry_dir(self, *entries):
        d = self.personal / "sessions"
        d.mkdir(parents=True)
        for pid, sid in entries:
            (d / ("%d.json" % pid)).write_text(json.dumps({"pid": pid, "sessionId": sid, "name": "agent-x"}))
        (d / ("999.%s.key" % ("0" * 64))).write_text("k")
        return d

    def test_the_killed_claudes_registry_entry_is_removed(self):
        # it cannot remove its own; left there it is a dead entry beside the relaunched one (agent-send exit 3)
        d = self.registry_dir((999, SID), (998, SID))
        ok, detail, *_ = self.run_migrate(live_after=True)
        self.assertTrue(ok, detail)
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["998.json"])   # only the killed process's files

    def test_an_entry_naming_another_session_or_a_live_pid_is_kept(self):
        d = self.registry_dir((999, "another-session"))
        self.assertTrue(self.run_migrate(live_after=True)[0])
        self.assertEqual(len(list(d.iterdir())), 2)
        (d / "999.json").write_text(json.dumps({"pid": 999, "sessionId": SID}))
        self.assertTrue(self.run_migrate(live_after=True, pid_reused=True)[0])
        self.assertEqual(len(list(d.iterdir())), 2)

    def test_background_jobs_are_named_in_the_note(self):
        jobs = [{"pid": 77, "command": "python train.py", "output": "/t/a.output"}]
        ok, detail, calls, killed, _ = self.run_migrate(live_after=True, jobs=jobs)
        typed = calls[1][-1]
        self.assertIn("still running", typed)
        self.assertIn("pid 77: python train.py (output: /t/a.output)", typed)
        self.assertEqual(killed, [(999, failover.signal.SIGKILL)])   # only claude: the job keeps running

    def test_kill_mode_stops_the_jobs_and_says_so(self):
        self.cfg["failover"]["background"] = "kill"
        jobs = [{"pid": 77, "command": "sleep 900", "output": ""}]
        with mock.patch.object(failover, "stop_jobs") as stop:
            ok, detail, calls, killed, _ = self.run_migrate(live_after=True, jobs=jobs)
        stop.assert_called_once_with(jobs)
        self.assertIn("were stopped", calls[1][-1])

    def test_dry_run_touches_nothing(self):
        ok, detail, calls, killed, _ = self.run_migrate(live_after=False, dry_run=True)
        self.assertTrue(ok)
        self.assertIn("would stop pid 999", detail)
        self.assertEqual((calls, killed), ([], []))
        self.assertFalse((self.work / "projects" / "-work").exists())

    def test_not_live_after_relaunch_is_reported(self):
        ok, detail, *_ = self.run_migrate(live_after=False)
        self.assertFalse(ok)
        self.assertIn("not live after 2s", detail)

    def test_a_failed_copy_relaunches_where_it_was_with_its_flags(self):
        ok, detail, calls, killed, _ = self.run_migrate(live_after=True, live_on="", copy_error=OSError("disk full"))
        self.assertFalse(ok)   # it runs again, but on the login that is out: still a failed move
        self.assertIn("copy failed (OSError: disk full); relaunched on personal instead", detail)
        typed = [c for c in calls if c[0] == "send-keys" and c[-1] != "Enter"][0][-1]
        self.assertIn("unset CLAUDE_CONFIG_DIR", typed)
        self.assertIn("--model m --effort high", typed)
        self.assertIn(("set-environment", "-t", "agent-x", "-r", "CLAUDE_CONFIG_DIR"), calls)

    def test_refuses_what_it_cannot_move(self):
        with mock.patch.object(failover, "pane_pid", return_value=None), \
                mock.patch.object(failover.os, "kill", side_effect=AssertionError("must not stop anything")):
            ok, detail = failover.migrate(sess(), "work", None, self.cfg)
        self.assertFalse(ok)
        self.assertIn("not moved: pane or process gone", detail)
        cfg = json.loads(json.dumps(self.cfg))
        cfg["usage"]["claude_accounts"]["work"]["config_dir"] = "/nonexistent/x"
        self.assertIn("not ready", failover.migrate(sess(), "work", None, cfg)[1])

    def run_bg_migrate(self, stop_rc=0, launch_rc=0, live_after=True, dry_run=False, jobs=(), dead=True,
                       launch_out=(), overrides=None):
        """A Claude Code background session on personal, out of Fable credits."""
        self.src.write_text("\n".join([json.dumps({"type": "permission-mode", "permissionMode": "bypassPermissions"}),
                                       reply("claude-fable-5-1", "high"), credits_record()]) + "\n")
        runs, stopped, capped = [], [], []
        s = dict(sess(), kind="bg", tmux=None, cwd=str(self.base), name="nightly report r7k2m9xq")
        live = [{"id": SID, "live": True, "pid": 1234, "config_dir": str(self.work.resolve())}] if live_after else []

        outs = list(launch_out)

        def run(cmd, **kw):
            runs.append((cmd, kw))
            rc = stop_rc if cmd[1] == "stop" else launch_rc
            said = outs.pop(0) if cmd[1] != "stop" and outs else "backgrounded · %s" % SID[:8]
            return subprocess.CompletedProcess(cmd, rc, said if rc == 0 else "", "boom" if rc else "")
        patches = [mock.patch.object(failover, "cmdline", return_value=["claude", "bg-spare", "--bg-spare", "/x.sock"]),
                   mock.patch.object(failover.subprocess, "run", side_effect=run),
                   mock.patch.object(failover.os, "kill", side_effect=AssertionError("a bg session is stopped, not killed")),
                   mock.patch.object(failover, "wait_dead", return_value=dead),
                   mock.patch.object(failover, "tmux", side_effect=AssertionError("no tmux for a bg session")),
                   mock.patch.object(failover.registry, "claude_sessions", return_value=live),
                   mock.patch.object(failover.time, "sleep"),
                   mock.patch.object(failover, "repo_root", return_value=str(self.base)),
                   mock.patch.object(failover, "background_jobs", return_value=list(jobs)),
                   mock.patch.object(failover, "stop_jobs", side_effect=stopped.append),
                   mock.patch("agentmail.memcap.cap_bg_session",
                              side_effect=lambda sid, name, props, session_pid=None:
                              capped.append((sid, name, session_pid())) or "agent-x.scope")]
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            ok, detail = failover.migrate(s, "work", "carry on", self.cfg, trust=lambda c, d: None,
                                          claude="/bin/claude", dry_run=dry_run, overrides=overrides)
        return ok, detail, runs, stopped, capped

    def known_on_work(self, flags=("--name", "nightly report r7k2m9xq", "--model", "claude-fable-5-1")):
        """work's daemon has run this session before: it keeps saved options for it (jobs/<short id>)."""
        d = self.work / "jobs" / SID[:8]
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"state": "done", "template": "bg", "respawnFlags": list(flags)}))

    def test_a_login_that_ran_the_session_before_resumes_it_with_no_flags(self):
        """Flags (even --name) would make that daemon start a copy under a new id: mail would stop reaching it."""
        self.known_on_work()
        ok, detail, runs, *_ = self.run_bg_migrate()
        self.assertTrue(ok, detail)
        self.assertEqual(runs[1][0], ["/bin/claude", "--resume", SID, "--bg", "carry on"])

    def test_a_copy_started_anyway_is_stopped_and_the_session_resumed_itself(self):
        note = ("backgrounded · 1d4e45b2 · bgx\nnote: background session %s keeps its own saved options, so the flags "
                "you passed started a copy as 1d4e45b2. Without flags, the same command continues %s itself."
                % (SID[:8], SID[:8]))
        ok, detail, runs, *_ = self.run_bg_migrate(launch_out=[note, "backgrounded · %s" % SID[:8]])
        self.assertTrue(ok, detail)
        self.assertEqual([r[0][1:3] for r in runs], [["stop", SID[:8]], ["--resume", SID], ["stop", "1d4e45b2"],
                                                     ["rm", "1d4e45b2"], ["--resume", SID]])
        self.assertEqual(runs[4][0], ["/bin/claude", "--resume", SID, "--bg", "carry on"])

    def test_a_model_change_a_daemon_would_ignore_is_refused_before_stopping(self):
        self.known_on_work()
        ok, detail, runs, *_ = self.run_bg_migrate(overrides={"--model": "claude-opus-5-5", "--effort": None})
        self.assertFalse(ok)
        self.assertIn("resumes it there with its saved options", detail)
        self.assertEqual(runs, [])
        ok, detail, runs, *_ = self.run_bg_migrate(overrides={"--model": "claude-fable-5-1", "--effort": "high"})
        self.assertTrue(ok, detail)   # what it runs already: nothing to change

    def test_a_background_session_is_stopped_and_resumed_by_the_daemon(self):
        jobs = [{"pid": 77, "command": "until grep -q exit smoke.log; do sleep 5; done", "output": "/t/b.output"}]
        ok, detail, runs, stopped, capped = self.run_bg_migrate(jobs=jobs)
        self.assertTrue(ok, detail)
        (stop, stop_kw), (launch, launch_kw) = runs
        self.assertEqual(stop, ["/bin/claude", "stop", SID[:8]])   # the short id: a full one is "No job matching"
        self.assertNotIn("CLAUDE_CONFIG_DIR", stop_kw["env"])          # stopped under its own (default) login
        self.assertEqual(launch[:12], ["/bin/claude", "--resume", SID, "--bg", "--name", "nightly report r7k2m9xq",
                                       "--model", "claude-fable-5-1", "--effort", "high",
                                       "--permission-mode", "bypassPermissions"])   # from the transcript
        self.assertTrue(launch[12].startswith("carry on"))
        self.assertIn("were stopped", launch[12])                       # `claude stop` ends its jobs: say so
        self.assertIn("pid 77: until grep", launch[12])
        self.assertEqual(launch_kw["env"]["CLAUDE_CONFIG_DIR"], str(self.work.resolve()))
        self.assertNotIn("CLAUDECODE", launch_kw["env"])
        self.assertEqual(launch_kw["cwd"], str(self.base))
        self.assertTrue((self.work / "projects" / "-work" / (SID + ".jsonl")).exists())
        self.assertEqual(stopped, [jobs])
        self.assertEqual(capped, [(SID, "nightly report r7k2m9xq", 1234)])   # daemon out of our cgroup

    def test_a_background_dry_run_says_what_it_would_run(self):
        ok, detail, runs, *_ = self.run_bg_migrate(dry_run=True)
        self.assertTrue(ok)
        self.assertEqual(runs, [])
        self.assertIn("would claude stop %s" % SID[:8], detail)
        self.assertIn("CLAUDE_CONFIG_DIR=%s claude --resume %s --bg" % (self.work.resolve(), SID), detail)

    def test_a_failed_background_stop_leaves_it_running(self):
        ok, detail, runs, *_ = self.run_bg_migrate(stop_rc=1, dead=False)
        self.assertFalse(ok)
        self.assertIn("claude stop failed", detail)
        self.assertFalse((self.work / "projects" / "-work").exists())

    def test_a_failed_background_resume_is_reported(self):
        ok, detail, runs, *_ = self.run_bg_migrate(launch_rc=1, live_after=False)
        self.assertFalse(ok)
        self.assertIn("claude --resume --bg on work failed: boom", detail)

    def test_movable_needs_the_pane_shell_to_be_claudes_parent(self):
        with mock.patch.object(failover, "pane_pid", return_value=100), \
                mock.patch.object(failover, "parent_pid", return_value=100):
            self.assertEqual(failover.movable(sess()), ("%5", ""))
        with mock.patch.object(failover, "pane_pid", return_value=100), \
                mock.patch.object(failover, "parent_pid", return_value=7):
            self.assertIn("not agent-spawn's layout", failover.movable(sess())[1])
        self.assertEqual(failover.movable(dict(sess(), tmux=None))[1], "not in tmux")
        self.assertEqual(failover.movable(dict(sess(), tmux=None, kind="bg")), (failover.BG, ""))
        self.assertEqual(failover.movable(dict(sess(), kind="bg", live=False))[1], "not live")


class ResumeLogin(unittest.TestCase):
    """Where an offline agent resumes (agent-send --offline resume, the mailroom's wake-up): agent-spawn's rule."""

    def pick(self, snaps_, model="claude-opus-5-5", owner=None, cfg=CFG):
        return failover.resume_login("work", model, owner, snaps_, cfg, NOW)[0]

    def test_the_first_login_with_headroom_even_if_the_newest_copy_is_elsewhere(self):
        self.assertEqual(self.pick(snaps()), "personal")
        self.assertEqual(self.pick(snaps(personal=0)), "work")

    def test_headroom_is_judged_for_its_model(self):
        self.assertEqual(self.pick(fable_snaps(personal=0, work=25), model="claude-fable-5-1"), "work")
        self.assertEqual(self.pick(fable_snaps(personal=0, work=25)), "personal")   # an Opus agent

    def test_a_login_excluded_for_its_repo_is_skipped(self):
        cfg = json.loads(json.dumps(CFG))
        cfg["usage"]["claude_accounts"]["personal"]["exclude_owners"] = ["Employer-Org"]
        self.assertEqual(self.pick(snaps(), owner="Employer-Org", cfg=cfg), "work")
        self.assertEqual(self.pick(snaps(), owner="example-user", cfg=cfg), "personal")

    def test_with_every_login_low_the_most_left_and_with_none_known_it_stays(self):
        self.assertEqual(self.pick(snaps(personal=5, work=8)), "work")
        self.assertEqual(failover.resume_login("personal", None, None, snaps(personal=0, work=0), CFG, NOW)[0],
                         "personal")                                              # all used up: no point moving it
        self.assertEqual(self.pick({}), "work")                                  # usage unknown


class PlaceResume(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.personal, self.work = self.base / ".claude", self.base / ".claude-work"
        Path(self.work, ".credentials.json").parent.mkdir(parents=True)
        Path(self.work, ".credentials.json").write_text("{}")
        self.cfg = json.loads(json.dumps(CFG))
        self.cfg["usage"]["claude_accounts"]["work"]["config_dir"] = str(self.work)
        self.homes = [("personal", self.personal, None), ("work", self.work, str(self.work))]
        self.trusted, self.linked = [], []
        self.patches = [mock.patch.object(registry, "claude_homes", return_value=self.homes),
                        mock.patch.object(registry, "DEFAULT_CLAUDE_HOME", self.personal),
                        mock.patch.object(failover.usage, "repo_owner", return_value=None),
                        mock.patch.object(failover, "repo_root", side_effect=lambda p: p),
                        mock.patch.object(failover.usage, "link_memory",
                                          side_effect=lambda root, d: self.linked.append((root, d)))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def copy(self, home, text, age):
        p = home / "projects" / "-work" / (SID + ".jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(entry("user", text) + "\n" + reply() + "\n")
        (p.parent / SID / "subagents").mkdir(parents=True, exist_ok=True)
        os.utime(p, (NOW - age, NOW - age))
        return p

    def place(self, snaps_, **kw):
        sess = {"id": SID, "cwd": str(self.base), "config_dir": str(self.work)}
        return failover.place_resume(sess, self.cfg, trust=lambda c, d: self.trusted.append((c, d)), snaps=snaps_,
                                     now=NOW, **kw)

    def test_the_newest_conversation_goes_to_the_best_login(self):
        old, new = self.copy(self.personal, "before the move", 3600), self.copy(self.work, "after the move", 0)
        config_dir, note = self.place(snaps())
        self.assertIsNone(config_dir)                                   # personal: the default login
        self.assertEqual(old.read_text(), new.read_text())
        self.assertIn("on login personal", note)
        self.assertIn("conversation copied from work", note)
        self.assertEqual(self.trusted, [(str(self.base), None)])
        self.assertEqual(self.linked, [])                               # the default login has the memory itself

    def test_to_a_second_login_the_memory_is_linked(self):
        self.copy(self.personal, "newest", 0)
        config_dir, note = self.place(snaps(personal=0))
        self.assertEqual(config_dir, str(self.work))
        self.assertIn("newest", (self.work / "projects" / "-work" / (SID + ".jsonl")).read_text())
        self.assertTrue((self.work / "projects" / "-work" / SID / "subagents").is_dir())
        self.assertEqual(self.linked, [(str(self.base), str(self.work))])

    def test_already_there_nothing_is_copied(self):
        old, new = self.copy(self.personal, "before", 3600), self.copy(self.work, "after", 0)
        config_dir, note = self.place(snaps(personal=0))
        self.assertEqual(config_dir, str(self.work))
        self.assertIn("before", old.read_text())
        self.assertEqual(self.trusted, [])

    def test_dry_run_copies_nothing(self):
        old, _ = self.copy(self.personal, "before", 3600), self.copy(self.work, "after", 0)
        config_dir, note = self.place(snaps(), dry_run=True)
        self.assertIsNone(config_dir)
        self.assertIn("would copy its conversation from work", note)
        self.assertIn("before", old.read_text())

    def test_failures_resume_where_the_newest_copy_is(self):
        self.copy(self.work, "after", 0)
        with mock.patch.object(failover, "copy_transcript", side_effect=OSError("disk full")):
            config_dir, note = self.place(snaps())
        self.assertEqual(config_dir, str(self.work))
        self.assertIn("copying it to personal failed (OSError: disk full)", note)
        with mock.patch.object(failover.usage, "snapshots", side_effect=RuntimeError("probe hung")):
            config_dir, note = self.place(None)
        self.assertEqual(config_dir, str(self.work))
        self.assertIn("usage unread (RuntimeError: probe hung)", note)

    def test_a_pinned_session_resumes_where_its_newest_copy_is(self):
        self.copy(self.personal, "the planner", 0)
        sess = {"id": SID, "name": "nightly-report", "cwd": str(self.base), "config_dir": None}
        config_dir, note = failover.place_resume(sess, self.cfg, snaps=snaps(personal=0), now=NOW)
        self.assertIsNone(config_dir)                                   # personal, though work has headroom
        self.assertIn("pinned (failover.pinned)", note)
        self.assertFalse((self.work / "projects" / "-work" / (SID + ".jsonl")).exists())

    def test_no_transcript_resumes_where_it_was(self):
        self.assertEqual(self.place(snaps()), (str(self.work), ""))


class BackgroundJobs(unittest.TestCase):
    def test_lists_bash_tool_commands_with_their_output_files(self):
        argv = {11: ["/bin/bash", "-c", "source snap.sh 2>/dev/null || true && eval 'python train.py --x '\"'\"'a b'\"'\"'' "
                                              "< /dev/null && pwd -P >| /tmp/cwd"],
                12: ["npm", "exec", "@gongrzhe/server-gmail-autoauth-mcp"],
                13: ["/bin/bash", "-c", "eval 'sleep 900' < /dev/null"]}
        outs = {11: "/t/tasks/a.output", 13: "pipe:[123]"}
        with mock.patch.object(failover, "cmdline", side_effect=lambda p: argv.get(p, [])), \
                mock.patch.object(failover.os, "readlink", side_effect=lambda path: outs[int(path.split("/")[2])]):
            jobs = failover.background_jobs(10, {11: 10, 12: 10, 13: 10, 14: 99})
        self.assertEqual(jobs, [{"pid": 11, "command": "python train.py --x 'a b'", "output": "/t/tasks/a.output"},
                                {"pid": 13, "command": "sleep 900", "output": ""}])
        note = failover.jobs_note(jobs, killed=False)
        self.assertIn("still running", note)
        self.assertIn("pid 11: python train.py --x 'a b' (output: /t/tasks/a.output)", note)
        self.assertIn("were stopped", failover.jobs_note(jobs, killed=True))
        self.assertEqual(failover.jobs_note([], killed=False), "")


class Registry(unittest.TestCase):
    def test_a_reused_pid_is_not_a_live_session(self):
        start = open("/proc/self/stat").read().rsplit(")", 1)[1].split()[19]
        self.assertTrue(registry.same_process(os.getpid(), start))
        self.assertFalse(registry.same_process(os.getpid(), str(int(start) + 1)))
        self.assertTrue(registry.same_process(os.getpid(), None))   # older Claude versions record none
        home = Path(tempfile.mkdtemp())
        sock = home / "sock"
        sock.write_text("")
        (home / "sessions").mkdir()
        for name, ps in (("real", start), ("reused", str(int(start) + 1))):
            (home / "sessions" / ("%s.json" % name)).write_text(json.dumps(
                {"pid": os.getpid(), "sessionId": name, "name": name, "messagingSocketPath": str(sock), "procStart": ps}))
        live = {s["name"]: s["live"] for s in registry._claude_sessions_in(home / "sessions", "a", None)}
        self.assertEqual(live, {"real": True, "reused": False})

    def test_an_exited_session_resumes_from_its_newest_copy(self):
        base = Path(tempfile.mkdtemp())
        a, b = base / ".claude", base / ".claude-work"
        for home, age in ((a, 100), (b, 0)):
            p = home / "projects" / "-work" / (SID + ".jsonl")
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({"cwd": str(base)}) + "\n")
            os.utime(p, (time.time() - age, time.time() - age))
        with mock.patch.object(registry, "claude_homes", return_value=[("personal", a, None), ("work", b, str(b))]):
            self.assertEqual(registry.claude_transcript_session(SID)["config_dir"], str(b))
            os.utime(a / "projects" / "-work" / (SID + ".jsonl"), (time.time() + 50, time.time() + 50))
            self.assertIsNone(registry.claude_transcript_session(SID)["config_dir"])


class Hook(unittest.TestCase):
    def test_stop_failure_is_logged_as_a_limit(self):
        st = load_script("agent-status")
        logged = []
        payload = {"hook_event_name": "StopFailure", "session_id": SID, "cwd": "/work", "error": "rate_limit",
                   "error_details": "You've hit your session limit"}
        with mock.patch.object(st.statuslog, "append", side_effect=logged.append), \
                mock.patch.object(st.statuslog, "identity", return_value={"cli": "claude", "session_id": SID}), \
                mock.patch.object(st.sys, "stdin", io.StringIO(json.dumps(payload))):
            st.cmd_hook(mock.Mock(cli="claude"))
        self.assertEqual(logged[0]["event"], "limit")
        self.assertEqual(logged[0]["text"], "rate_limit: You've hit your session limit")
        with mock.patch.object(st.statuslog, "append", side_effect=logged.append), \
                mock.patch.object(st.statuslog, "identity", return_value={"cli": "claude", "session_id": SID}), \
                mock.patch.object(st.sys, "stdin", io.StringIO(json.dumps(dict(payload, error="server_error")))):
            st.cmd_hook(mock.Mock(cli="claude"))
        self.assertEqual(logged[1]["event"], "api-error")


class Watch(unittest.TestCase):
    def test_one_pass_moves_a_limited_agent_and_saves_state(self):
        af = load_script("agent-failover")
        agent = sess()
        moved = []
        with mock.patch.object(af, "sample", return_value=([agent], {SID: {"window": "five_hour",
                                                                          "resets_at": NOW + 600, "text": ""}}, {})), \
                mock.patch.object(af.usage, "snapshots", return_value=snaps(personal=0)), \
                mock.patch.object(af.failover, "migrate", side_effect=lambda s, to, prompt, cfg, **k:
                                  moved.append((s["id"], to, prompt)) or (True, "moved")), \
                mock.patch.object(af.statuslog, "append"), mock.patch.object(af, "trust", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            state, moves, _ = af.one_pass(CFG, {}, False, 0, NOW)
        self.assertEqual([(m[0], m[1]) for m in moved], [(SID, "work")])
        self.assertEqual(state["sessions"][SID]["home"], "personal")

    def test_out_of_fable_credits_end_to_end(self):
        """A background agent on personal, Fable 5.1, out of Fable credits (weekly Fable 0%,
        extra usage off). The watch must see it and move it to work, which has Fable left."""
        af = load_script("agent-failover")
        home = Path(tempfile.mkdtemp())
        t = home / "projects" / "-work" / (SID + ".jsonl")
        t.parent.mkdir(parents=True)
        t.write_text("\n".join([reply(), entry("user", "[email from Alex] brilliant work"), credits_record()]) + "\n")
        agent = dict(sess(name="nightly report r7k2m9xq"), kind="bg", tmux=None)
        moved = []
        with mock.patch.object(af.registry, "claude_sessions", return_value=[agent]), \
                mock.patch.object(af.registry, "DEFAULT_CLAUDE_HOME", home), \
                mock.patch.object(af.usage, "snapshots", return_value=fable_snaps(personal=0, work=25)), \
                mock.patch.object(af.failover, "migrate", side_effect=lambda s, to, prompt, cfg, **k:
                                  moved.append((s["id"], s["model"], to, prompt)) or (True, "moved")), \
                mock.patch.object(af.statuslog, "append"), mock.patch.object(af, "trust", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            state, moves, _ = af.one_pass(CFG, {}, False, NOW, NOW)
        self.assertEqual([m[:3] for m in moved], [(SID, "claude-fable-5-1", "work")])
        self.assertIn("out of Fable 5.1 usage credits", moved[0][3])
        self.assertIn("moved nightly report r7k2m9xq", out.getvalue())
        self.assertEqual(state["sessions"][SID]["home"], "personal")
        self.assertEqual(state["sessions"][SID]["resets_at"], NOW + 3600)   # personal's weekly Fable reset

    def test_no_usage_read_when_nothing_is_limited_or_idle(self):
        af = load_script("agent-failover")
        with mock.patch.object(af, "sample", return_value=([sess()], {}, {})), \
                mock.patch.object(af.usage, "snapshots", side_effect=AssertionError("no usage read needed")):
            state, moves, _ = af.one_pass(CFG, {}, False, NOW, NOW)
        self.assertEqual(moves, [])


if __name__ == "__main__":
    unittest.main()
