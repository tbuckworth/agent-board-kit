import contextlib
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

import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import email  # noqa: E402
import email.policy  # noqa: E402
import fakes  # noqa: E402
import homes  # noqa: E402
from scripts import load_script  # noqa: E402
from agentmail import alerts, claude_session, commands, failover, jsonfile, launch, outbound, procs, registry, statuslog, store, usage  # noqa: E402,E501

NOW = time.time()
WORK = tempfile.mkdtemp()
Path(WORK, ".credentials.json").write_text("{}")
CFG = {"usage": {"claude_accounts": {"personal": {"config_dir": None}, "work": {"config_dir": WORK}},
                 "order": ["claude:personal", "claude:work", "codex"]},
       "spawn_claude_model": "claude-opus-5-5", "failover": {"idle_seconds": 300, "pinned": ["nightly-*"]}}
SID = "3d3d3d3d-0000-4000-8000-000000000001"


def rejection(resets_at, window="five_hour", status="rejected"):
    """The entry Claude Code 2.1.291 writes when a plan limit stops a turn (shape seen in real transcripts)."""
    return json.dumps({"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit", "apiErrorStatus": 429,
                       "sessionId": SID, "timestamp": "2026-05-19T14:00:00Z",
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


def credits_record(text=SESSION_CREDITS, api_error="model_requires_usage_credits", at="2026-05-20T18:30:26.112Z"):
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


def win(kind, remaining, resets_at=NOW + 3600):
    return {"kind": kind, "name": kind, "used_pct": 100 - remaining, "remaining_pct": remaining,
            "resets_at": resets_at, "model": None}


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
        self.assertIsNone(failover.limit_hit([rejection(NOW + 600), entry("user"), reply()], now=NOW))  # it went on
        self.assertIsNone(failover.limit_hit([rejection(NOW + 600, status="allowed")], now=NOW))  # a transient 429
        self.assertIsNone(failover.limit_hit([entry("assistant")], now=NOW))
        self.assertIsNone(failover.limit_hit(["not json", ""], now=NOW))

    def test_relaunch_flags_keep_model_effort_permissions_not_the_prompt(self):
        argv = ["/home/alex/.local/bin/claude", "--name", "agent-x", "--dangerously-skip-permissions", "--model",
                "claude-opus-5-5", "--effort", "xhigh", "--resume", "old", "You were started by agent-spawn..."]
        self.assertEqual(failover.relaunch_flags(argv), ["--dangerously-skip-permissions", "--model",
                                                         "claude-opus-5-5", "--effort", "xhigh"])
        self.assertEqual(failover.relaunch_flags(["claude", "--model=opus", "hi"]), ["--model=opus"])
        self.assertEqual(claude_session.pane_id({"tmux": "usage-aware-routing:@7.%7"}), "%7")
        self.assertIsNone(claude_session.pane_id({"tmux": None}))

    def test_launch_line(self):
        line = launch.claude_line("/bin/claude", SID, "a b", ["--model", "m"], "/h/.claude-work", "it's done", None)
        self.assertEqual(line, "stty sane; export CLAUDE_CONFIG_DIR=/h/.claude-work; /bin/claude --resume %s "
                               "--name 'a b' --model m 'it'\"'\"'s done'" % SID)
        self.assertTrue(launch.claude_line("c", SID, "n", [], None, None, None).startswith("stty sane; unset CLAUDE_CONFIG_DIR; "))
        self.assertFalse(launch.claude_line("c", SID, "n", [], None, None, None).endswith("'"))


class Credits(unittest.TestCase):
    """A model's own weekly bucket (Fable) used up: the limit that is easy to miss, since it has no reset time."""

    def test_the_fable_credits_record_is_a_limit(self):
        hit = failover.limit_hit([reply(), entry("user", "carry on"), credits_record(),
                                  json.dumps({"type": "system", "subtype": "turn_duration"})], now=NOW)
        self.assertEqual((hit["window"], hit["resets_at"], hit["model"]), ("model_credits", None, "Fable 5.1"))
        self.assertEqual(hit["text"], SESSION_CREDITS)
        self.assertEqual(hit["at"], usage.iso_epoch("2026-05-20T18:30:26.112Z"))

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
        self.assertEqual(claude_session.session_model(lines), "claude-fable-5-1")   # not the error's "<synthetic>"
        self.assertEqual(claude_session.transcript_flags(lines), ["--model", "claude-fable-5-1", "--effort", "high",
                                                            "--permission-mode", "bypassPermissions"])
        self.assertIsNone(claude_session.session_model([credits_record()]))
        timed = json.dumps(dict(json.loads(reply("claude-opus-5-5")), timestamp="2026-06-23T09:42:13.471Z"))
        self.assertEqual(claude_session.last_reply([timed, credits_record()]),
                         ("claude-opus-5-5", "2026-06-23T09:42:13.471Z"))
        self.assertEqual(claude_session.last_reply([credits_record()]), (None, None))
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


LADDER = dict(CFG, spawn_claude_model="claude-fable-5-2", claude_fallback_models=["claude-opus-5-5"])
FABLE, OPUS = "claude-fable-5-2", "claude-opus-5-5"


class Ladder(unittest.TestCase):
    """The login and model order: personal Fable, work Fable, personal Opus 5.5, work Opus 5.5, then work credits
    on Opus 5.5, unless the owner changed the agent by hand."""
    LIMIT = {SID: {"window": "five_hour", "resets_at": NOW + 3600, "text": ""}}

    def place(self, agent, s, state=None, limits=None):
        moves, notes = failover.decide([agent], self.LIMIT if limits is None else limits, s, state or {}, LADDER, NOW)
        return [(m["to"], m["model"]) for m in moves], moves, notes

    def test_a_limited_fable_agent_steps_down_in_alex_order(self):
        fable_agent = dict(sess(), model=FABLE)
        got, moves, _ = self.place(fable_agent, fable_snaps(personal=0, work=25))
        self.assertEqual(got, [("work", None)])                       # work Fable: the same model
        got, moves, _ = self.place(fable_agent, fable_snaps(personal=0, work=0))
        self.assertEqual(got, [("personal", OPUS)])                    # personal Opus: the same login, restarted
        self.assertIn("moved to login 'personal' on model claude-opus-5-5 and resumed here", moves[0]["prompt"])
        self.assertIn("(model claude-opus-5-5)", moves[0]["reason"])
        s = fable_snaps(personal=0, work=0)
        s["claude:personal"]["windows"][0] = win("session", 0)         # personal used up for every model
        self.assertEqual(self.place(fable_agent, s)[0], [("work", OPUS)])
        s["claude:work"]["windows"][0] = win("session", 0)            # and work
        got, _, notes = self.place(fable_agent, s)
        self.assertEqual(got, [])
        self.assertIn("no other Claude login has headroom for claude-fable-5-2 or claude-opus-5-5", notes[0][2])
        self.assertIn("and none bills extra usage for claude-opus-5-5; it waits", notes[0][2])
        s["claude:work"]["extra_usage"] = {"enabled": True}
        self.assertEqual(self.place(fable_agent, s)[0], [("work", OPUS)])   # credits, on Opus only

    def test_an_opus_agent_never_moves_up_to_fable(self):
        s = fable_snaps(personal=50, work=50)
        s["claude:personal"]["windows"][0] = win("session", 0)
        self.assertEqual(self.place(dict(sess(), model=OPUS), s)[0], [("work", None)])

    def test_a_fable_agent_kept_on_fable_takes_the_other_login_then_bills_there(self):
        """A Fable agent switched to Fable by hand, or left on Fable when it is not on the model order, goes to the
        other login when Fable runs out, then bills extra usage there on Fable (spend_watch mails the owner, who can
        switch it); it never moves onto another model."""
        by_hand = {"sessions": {SID: {"switched_at": NOW - 3600}}}
        fable_agent = dict(sess(), model=FABLE)
        for cfg, state in ((LADDER, by_hand), (CFG, by_hand), (CFG, {})):
            def place(s):
                moves, notes = failover.decide([fable_agent], self.LIMIT, s, state, cfg, NOW)
                return [(m["to"], m["model"]) for m in moves], notes
            self.assertEqual(place(fable_snaps(personal=0, work=25))[0], [("work", None)])   # the other login
            got, notes = place(fable_snaps(personal=0, work=0))
            self.assertEqual(got, [])
            self.assertIn("headroom for claude-fable-5-2 (work: weekly Fable window used up", notes[0][2])
            self.assertIn("and none bills extra usage for claude-fable-5-2; it waits", notes[0][2])
            s = fable_snaps(personal=0, work=0)
            s["claude:work"]["extra_usage"] = {"enabled": True}
            self.assertEqual(place(s)[0], [("work", None)])                    # work's extra usage, on Fable

    def test_back_to_fable_on_its_first_login_once_that_has_reset(self):
        state = {"sessions": {SID: {"home": "personal", "home_model": FABLE, "current": "work", "model": OPUS,
                                    "resets_at": NOW - 60, "moves": []}}}
        idle = dict(sess("work", "idle", 600), model=OPUS)
        got, moves, _ = self.place(idle, fable_snaps(personal=96, work=0), state, limits={})
        self.assertEqual(got, [("personal", FABLE)])
        self.assertEqual(moves[0]["prompt"], None)
        self.assertEqual(self.place(idle, fable_snaps(personal=0, work=0), state, limits={})[0],
                         [("personal", None)])     # no Fable anywhere: up to personal on the Opus it runs
        busy = dict(sess("work", "busy", 600), model=OPUS)
        self.assertEqual(self.place(busy, fable_snaps(personal=96, work=0), state, limits={})[0], [])
        # the next pass, before it replies: its transcript still shows Opus, but it runs the Fable the move gave it
        moved = failover.record(state, moves[0], NOW)
        back = dict(sess("personal", "idle", 600), model=OPUS, model_at=NOW - 700)
        self.assertEqual(self.place(back, fable_snaps(personal=96, work=0), moved, limits={}), ([], [], []))
        # once it has replied since (on Opus, after a /model there), its transcript is right again
        replied = dict(back, model_at=NOW + 60)
        self.assertEqual(self.place(replied, fable_snaps(personal=96, work=0), moved, limits={})[0],
                         [("personal", FABLE)])

    def test_record_keeps_the_first_login_and_model_and_that_ones_reset(self):
        moves = [{"session": dict(sess(), model=FABLE), "to": "work", "model": None, "from_model": FABLE,
                  "reason": "limit", "resets_at": NOW + 60},
                 {"session": dict(sess("work"), model=FABLE), "to": "personal", "model": OPUS, "from_model": FABLE,
                  "reason": "limit", "resets_at": NOW + 600}]
        st = failover.record(failover.record({}, moves[0], NOW), moves[1], NOW + 1)
        e = st["sessions"][SID]
        self.assertEqual((e["home"], e["home_model"], e["current"], e["model"], e["resets_at"]),
                         ("personal", FABLE, "personal", OPUS, NOW + 60))     # the reset of personal's Fable
        back = {"session": dict(sess(), model=OPUS), "to": "personal", "model": FABLE, "from_model": OPUS,
                "reason": "switch back", "resets_at": None}
        self.assertNotIn("resets_at", failover.record(st, back, NOW + 2)["sessions"][SID])
        old = {"sessions": {SID: {"home": "personal", "current": "work", "moves": []}}}   # written before models
        e = failover.record(old, dict(moves[1], model=None, from_model=OPUS), NOW)["sessions"][SID]
        self.assertEqual((e["home"], e["home_model"]), ("personal", OPUS))


FIXTURES = ROOT / "tests" / "fixtures" / "usage-limit"
RESET_1630 = 1781281800   # 4:30pm UTC on 2026-06-12: the resetsAt of the fixtures' refusals


def fixture(name):
    """A transcript excerpt, trimmed and scrubbed: Claude Code's limit records and notes as it writes them, every
    other text a placeholder."""
    return (FIXTURES / name).read_text().splitlines()


def notice(text, at):
    """Claude Code's system line when a limit ends a turn (2.1.314)."""
    return json.dumps({"type": "system", "subtype": "informational", "isMeta": False, "level": "notice",
                       "timestamp": at, "content": text})


class UsageLimitReached(unittest.TestCase):
    """A login at its 5-hour limit, as the fixtures record it: three agents that agent-failover must each see as
    stopped."""

    def setUp(self):
        # The auto-continue time Claude Code writes is local; these transcripts were written at UTC.
        env = mock.patch.dict(os.environ, {"TZ": "Etc/UTC"})
        env.start()
        self.addCleanup(time.tzset)
        self.addCleanup(env.stop)
        time.tzset()

    def test_the_grace_allowance_ends_in_a_limit(self):
        """garden-planner: Claude Code's grace allowance, then "Usage limit reached · continuing automatically at
        4:30pm"; no API-error record at all."""
        lines = fixture("grace-allowance.jsonl")
        at = usage.iso_epoch("2026-06-12T14:40:48.931Z")
        hit = failover.limit_hit(lines, now=at + 60)
        self.assertEqual((hit["resets_at"], hit["at"]), (RESET_1630, at))
        self.assertEqual(hit["text"], "Usage limit reached · continuing automatically at 4:30pm · esc to cancel")
        self.assertIsNone(failover.limit_hit(lines[:-2], now=at))           # still wrapping up in its allowance
        self.assertIsNone(failover.limit_hit(lines, now=RESET_1630 + 1))    # past 4:30pm

    def test_a_task_notification_after_the_refusal_does_not_hide_it(self):
        """recipe-site: two refusals, then a background helper's task-notification as the last user line."""
        hit = failover.limit_hit(fixture("refusal-then-task-notification.jsonl"), now=RESET_1630 - 3600)
        self.assertEqual((hit["window"], hit["resets_at"], hit["at"]),
                         ("five_hour", RESET_1630, usage.iso_epoch("2026-06-12T14:46:01.341Z")))

    def test_a_limit_hit_again_stays_hit_until_a_good_reply(self):
        """agent-board: a queued message, "Usage limit reached again" and a refusal, then a task-notification;
        it went on at 14:47 once a person's message got a reply."""
        lines = fixture("refusal-again-then-task-notification.jsonl")
        hit = failover.limit_hit(lines, now=RESET_1630 - 3600)
        self.assertEqual((hit["window"], hit["at"]), ("five_hour", usage.iso_epoch("2026-06-12T14:46:17.787Z")))
        typed = lines + [entry("user", "you hit your limit keep going.", origin={"kind": "human"})]
        self.assertEqual(failover.limit_hit(typed, now=RESET_1630 - 3600)["at"], hit["at"])
        self.assertIsNone(failover.limit_hit(typed + [reply("claude-opus-5-5")], now=RESET_1630 - 3600))

    def test_the_auto_continue_time_is_the_next_one_after_the_line(self):
        """Lines from other transcripts, each beside the refusal that gave its reset."""
        for text, at, resets in (
                ("Usage limit reached · continuing automatically at 12am · esc to cancel",
                 "2026-05-26T21:36:24.448Z", 1779840000),                    # midnight: the next day
                ("Usage limit reached · continuing automatically at 3pm · esc or type to cancel",
                 "2026-04-30T12:06:57.517Z", 1777561200),
                ("Usage limit reached again · continuing automatically at 4:30pm · esc to cancel",
                 "2026-06-12T14:46:17.779Z", RESET_1630)):
            with self.subTest(text=text):
                hit = failover.limit_hit([reply(), notice(text, at)], now=usage.iso_epoch(at))
                self.assertEqual(hit["resets_at"], resets)

    def test_a_reset_it_cannot_read_comes_from_the_logins_used_up_window(self):
        hit = failover.limit_hit([reply(), notice("Usage limit reached · esc to cancel", "2026-06-12T14:40:48Z")],
                                 now=NOW)
        self.assertIsNone(hit["resets_at"])
        moves, _ = failover.decide([sess()], {SID: hit}, snaps(personal=0), {}, CFG, NOW)
        self.assertEqual([(m["to"], m["resets_at"]) for m in moves], [("work", NOW + 3600)])

    def test_a_line_with_no_time_from_before_the_logins_last_reset_is_over(self):
        """No window used up now: a line written before the latest of the counted windows' last resets is over (the
        window that ran out has reset since); one written after it is trusted, as is any while usage is unread."""
        s = snaps()   # personal's 5-hour window resets in an hour, so it last reset 4 hours ago
        s["claude:personal"]["windows"].append(dict(win("weekly", 40), resets_at=NOW + 3 * 86400))   # 4 days ago

        def limit(at):
            return {SID: failover.limit_hit([reply(), notice("Usage limit reached · esc to cancel", at)], now=NOW)}
        self.assertEqual(failover.decide([sess()], limit(NOW - 5 * 3600), s, {}, CFG, NOW), ([], []))
        moves, _ = failover.decide([sess()], limit(NOW - 3 * 3600), s, {}, CFG, NOW)   # after it, yet refused
        self.assertEqual([(m["to"], m["resets_at"]) for m in moves], [("work", None)])
        s["claude:personal"] = {"windows": [], "error": "claude usage read timed out after 30s", "cli": "claude",
                                "account": "personal"}
        moves, _ = failover.decide([sess()], limit(NOW - 5 * 3600), s, {}, CFG, NOW)
        self.assertEqual([(m["to"], m["resets_at"]) for m in moves], [("work", None)])

    def test_text_quoting_the_line_is_not_a_limit(self):
        quoted = "Usage limit reached · continuing automatically at 4:30pm · esc to cancel"
        self.assertIsNone(failover.limit_hit([reply(), entry("user", quoted)], now=NOW))
        self.assertIsNone(failover.limit_hit([reply(), json.dumps({"type": "system", "subtype": "stop_hook_summary",
                                                                   "content": quoted})], now=NOW))

    def test_a_record_from_before_its_last_move_is_the_login_it_left(self):
        """Moved off personal a minute ago; its resume prompt is the last line until the first reply is written,
        so the refusal that moved it is still its latest record. personal reads as having headroom (a stale usage
        reading would): without the move in mind, it would be killed mid-turn and moved back."""
        state = {"sessions": {SID: {"home": "personal", "current": "work",
                                    "moves": [[NOW - 60, "personal", "work", "limit: five-hour limit"]]}}}
        old = {SID: {"window": "five_hour", "resets_at": NOW + 3600, "at": NOW - 120, "text": ""}}
        self.assertEqual(failover.decide([sess("work")], old, snaps(), state, CFG, NOW), ([], []))
        new = {SID: dict(old[SID], at=NOW - 30)}
        moves, _ = failover.decide([sess("work")], new, snaps(), state, CFG, NOW)
        self.assertEqual([m["to"] for m in moves], ["personal"])


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

    def test_a_login_whose_usage_could_not_be_read_is_named_in_the_note(self):
        s = snaps(personal=0)
        s["claude:work"] = {"windows": [], "error": "claude usage read timed out after 30s", "cli": "claude",
                             "account": "work"}
        moves, notes = self.decide([sess()], {SID: {"window": "five_hour", "resets_at": NOW + 3600, "text": ""}}, s)
        self.assertEqual(moves, [])
        self.assertIn("no other Claude login has headroom for claude-opus-5-5 (work: usage unknown: claude usage read "
                      "timed out after 30s), and none bills extra usage for claude-opus-5-5; it waits for the reset",
                      notes[0][2])

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

    def test_an_idle_agent_started_lower_in_the_order_moves_up_once_there_is_headroom(self):
        """An agent started or resumed on work while personal had no headroom, so never moved off personal, goes
        there too once personal has headroom, the same way as one moved back."""
        moves, _ = self.decide([sess("work", "idle", 600)])
        self.assertEqual([(m["to"], m["prompt"]) for m in moves], [("personal", None)])
        self.assertEqual(moves[0]["reason"], "move up: personal has headroom")
        self.assertEqual(self.decide([sess("work", "idle", 600)], s=snaps(personal=0))[0], [])
        self.assertEqual(self.decide([sess("work", "busy", 600)])[0], [])
        moved_by_hand = {"sessions": {SID: {"home": "work", "current": "personal", "moves": []}}}
        self.assertEqual(self.decide([sess("personal", "idle", 600)], state=moved_by_hand)[0], [])   # at the top

    def test_a_move_goes_only_to_a_login_the_repos_owner_may_use(self):
        cfg = json.loads(json.dumps(CFG))
        cfg["usage"]["claude_accounts"]["personal"]["exclude_owners"] = ["Example-Org"]
        work = dict(sess("work", "idle", 600), owner="example-org")
        self.assertEqual(self.decide([work], cfg=cfg)[0], [])                                 # not up to personal
        self.assertEqual([m["to"] for m in self.decide([dict(work, owner="example-user")], cfg=cfg)[0]], ["personal"])
        limited = dict(work, status="busy")
        moves, notes = self.decide([limited], {SID: {"window": "five_hour", "resets_at": NOW + 60, "text": ""}},
                                   snaps(work=0), cfg=cfg)
        self.assertEqual(moves, [])                                                           # nor there on a limit
        self.assertIn("(personal: repos owned by example-org are excluded from this account)", notes[0][2])

    def test_an_agent_switched_by_hand_moves_up_only_to_where_a_limit_took_it_from(self):
        switched = {"sessions": {SID: {"switched_at": NOW - 3600}}}
        self.assertEqual(self.decide([sess("work", "idle", 600)], state=switched)[0], [])      # the owner's choice
        limited = {"sessions": {SID: {"switched_at": NOW - 3600, "home": "personal", "current": "work",
                                       "resets_at": NOW - 60, "moves": []}}}
        moves, _ = self.decide([sess("work", "idle", 600)], state=limited)
        self.assertEqual([(m["to"], m["reason"]) for m in moves], [("personal", "switch back: personal has reset")])

    def test_idle_agent_billing_extra_usage_is_rebalanced(self):
        billing = snaps(personal=0, work=80)
        billing["claude:personal"]["extra_usage"] = {"enabled": True}
        moves, _ = self.decide([sess("personal", "idle", 600)], s=billing)
        self.assertEqual([(m["to"], m["prompt"]) for m in moves], [("work", None)])   # down: no place above it
        self.assertIn("extra usage", moves[0]["reason"])
        moves, _ = self.decide([sess("personal", "idle", 600)], s=snaps(personal=0, work=80))   # extra usage off:
        self.assertTrue(moves[0]["reason"].startswith("spent: "), moves[0]["reason"])          # IdleOnUsedUpLogin
        self.assertEqual(self.decide([sess("personal", "busy", 600)], s=billing)[0], [])
        moves, _ = self.decide([sess("work", "idle", 600)], s=snaps(personal=80, work=0, work_extra=True))
        self.assertEqual([m["to"] for m in moves], ["personal"])                          # up, as any idle agent

    def test_ping_pong_guard(self):
        state = {"sessions": {SID: {"moves": [[NOW - 60 * i, "a", "b", "r"] for i in range(4)]}}}
        hit = {SID: {"window": "five_hour", "resets_at": NOW + 60, "text": ""}}
        moves, notes = self.decide([sess()], hit, snaps(personal=0), state)
        self.assertEqual(moves, [])
        self.assertEqual(notes[0][:2], (SID, NOW + 60))     # said once: the same marker as any other wait
        self.assertIn("agent-x (3d3d3d3d) hit its limit on personal (five-hour limit, resets ", notes[0][2])
        self.assertIn("moved 4 times in the last hour, the most failover.max_moves_per_hour allows; it waits",
                      notes[0][2])
        state["sessions"][SID]["stuck_until"] = NOW + 60
        self.assertEqual(self.decide([sess()], hit, snaps(personal=0), state), ([], []))
        self.assertEqual(self.decide([sess()], {}, snaps(personal=0), state), ([], []))   # no limit: nothing to say

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


WEEK_RESET = NOW + 3 * 86400   # when personal's used-up weekly window resets


def used_up(work=90, **kw):
    """snaps() with personal's weekly window used up (its 5-hour one has room)."""
    s = snaps(work=work, **kw)
    s["claude:personal"]["windows"].append(win("weekly", 0, WEEK_RESET))
    return s


class IdleOnUsedUpLogin(unittest.TestCase):
    """An idle agent, which has hit no limit itself, leaves a login whose fresh usage reading is used up for its model,
    as a limit would move it, with no prompt and only where there is headroom; with that login's extra usage on, the
    same move is a rebalance. It comes back after the window's reset and not before."""

    def decide(self, sessions, s=None, state=None, cfg=CFG, now=NOW, limits=None):
        return failover.decide(sessions, limits or {}, used_up() if s is None else s, state or {}, cfg, now)

    def test_it_moves_to_the_next_login_with_headroom_and_waits_there(self):
        moves, notes = self.decide([sess("personal", "idle", 600)])
        self.assertEqual([(m["to"], m["model"], m["prompt"], m["resets_at"], m["idle"]) for m in moves],
                         [("work", None, None, WEEK_RESET, True)])
        self.assertEqual(moves[0]["reason"], "spent: personal has no headroom (weekly window used up, resets %s)"
                         % usage.fmt_reset(WEEK_RESET, NOW))
        self.assertEqual(notes, [])
        e = failover.record({}, moves[0], NOW)["sessions"][SID]
        self.assertEqual((e["home"], e["current"], e["resets_at"]), ("personal", "work", WEEK_RESET))
        s = used_up()
        s["claude:personal"]["windows"][0] = win("session", 0)     # both used up: it waits for the later reset
        moves, _ = self.decide([sess("personal", "idle", 600)], s)
        self.assertIn("(session and weekly windows used up, resets ", moves[0]["reason"])
        self.assertEqual(moves[0]["resets_at"], WEEK_RESET)

    def test_a_fable_agent_steps_down_the_ladder_as_on_a_limit(self):
        agent = dict(sess("personal", "idle", 600), model=FABLE)
        s = fable_snaps(personal=0, work=0, personal_resets=WEEK_RESET)   # Fable used up on both, Opus has room
        moves, _ = failover.decide([agent], {}, s, {}, LADDER, NOW)
        self.assertEqual([(m["to"], m["model"], m["prompt"]) for m in moves], [("personal", OPUS, None)])
        self.assertIn("(weekly Fable window used up, resets ", moves[0]["reason"])
        self.assertTrue(moves[0]["reason"].endswith("(model claude-opus-5-5)"), moves[0]["reason"])
        e = failover.record({}, moves[0], NOW)["sessions"][SID]
        self.assertEqual((e["home"], e["home_model"], e["model"], e["resets_at"]),
                         ("personal", FABLE, OPUS, WEEK_RESET))

    def test_a_login_billing_extra_usage_is_left_by_the_same_rule(self):
        s = used_up(work=90)
        s["claude:personal"]["extra_usage"] = {"enabled": True}
        moves, _ = self.decide([sess("personal", "idle", 600)], s)
        self.assertEqual([(m["to"], m["prompt"], m["resets_at"]) for m in moves], [("work", None, WEEK_RESET)])
        self.assertEqual(moves[0]["reason"], "rebalance: personal bills extra usage (weekly window used up, resets %s)"
                         % usage.fmt_reset(WEEK_RESET, NOW))
        stale = json.loads(json.dumps(s))
        stale["claude:personal"].update(stale=True, error="timed out", observed_at=NOW - 3 * 3600)
        self.assertEqual(self.decide([sess("personal", "idle", 600)], stale), ([], []))   # an old reading: no move
        s["claude:work"]["windows"] = [win("session", 0)]
        moves, notes = self.decide([sess("personal", "idle", 600)], s)
        self.assertEqual(moves, [])
        self.assertIn("is idle on personal, which bills extra usage (weekly window used up, resets ", notes[0][2])
        self.assertTrue(notes[0][2].endswith("; it stays there, billing extra usage when it works"), notes[0][2])

    def test_a_busy_agent_or_one_not_idle_long_enough_or_in_a_dialog_stays(self):
        for agent in (sess("personal", "busy", 600), sess("personal", "idle", 60),
                      dict(sess("personal", "idle", 600), waiting_for="permission")):
            self.assertEqual(self.decide([agent]), ([], []))
        off = dict(CFG, failover={"switch_back": False})
        self.assertEqual(self.decide([sess("personal", "idle", 600)], cfg=off), ([], []))
        # a busy agent's limit, when it hits one, moves it as a limit always moves it, with its prompt
        hit = {SID: {"window": "seven_day", "resets_at": WEEK_RESET, "text": ""}}
        moves, _ = self.decide([sess("personal", "busy", 600)], limits=hit)
        self.assertEqual([(m["to"], m["idle"]) for m in moves], [("work", False)])
        self.assertIn("hit its usage limit", moves[0]["prompt"])

    def test_a_pinned_agent_stays_and_so_does_one_moved_too_often(self):
        self.assertEqual(self.decide([sess("personal", "idle", 600, name="nightly-research")]), ([], []))
        busy_hour = {"sessions": {SID: {"moves": [[NOW - 60 * i, "a", "b", "r"] for i in range(4)]}}}
        self.assertEqual(self.decide([sess("personal", "idle", 600)], state=busy_hour), ([], []))

    def test_a_login_whose_usage_is_unknown_or_stale_is_not_used_up(self):
        failed = used_up()
        failed["claude:personal"] = {"windows": [], "error": "claude usage read timed out after 30s", "cli": "claude",
                                     "account": "personal"}
        stale = used_up()
        stale["claude:personal"].update(stale=True, error="timed out", observed_at=NOW - 3 * 3600)
        missing = used_up()
        del missing["claude:personal"]
        for s in (failed, stale, missing):
            self.assertEqual(self.decide([sess("personal", "idle", 600)], s), ([], []))
        self.assertEqual(self.decide([sess("personal", "idle", 600)], snaps(personal=5)), ([], []))   # low, not out

    def test_nowhere_with_headroom_it_stays_noted_once_with_no_limit_wait(self):
        s = used_up(work=0, work_extra=True)    # work used up too: it would bill there, so it does not go
        moves, notes = self.decide([sess("personal", "idle", 600)], s)
        self.assertEqual(moves, [])
        [(sid, key, text, event)] = notes
        self.assertEqual((sid, event), (SID, None))   # no limit-wait: the board must not show it stopped
        self.assertIn("agent-x (3d3d3d3d) is idle on personal, which has no headroom (weekly window used up, resets ",
                      text)
        self.assertIn("and no other Claude login has headroom for claude-opus-5-5 (work: session window used up "
                      "until ", text)
        self.assertTrue(text.endswith("; it stays there"), text)
        self.assertEqual(self.decide([sess("personal", "idle", 600)], s, {"sessions": {SID: {"stuck_until": key}}}),
                         ([], []))

    def test_it_comes_back_after_the_reset_and_not_before(self):
        moves, _ = self.decide([sess("personal", "idle", 600)])
        state = failover.record({}, moves[0], NOW)
        there = sess("work", "idle", 600)
        self.assertEqual(self.decide([there], state=state, now=NOW + 1200), ([], []))   # personal still used up
        # personal reads as having headroom again before the recorded reset: still no move back
        self.assertEqual(self.decide([there], snaps(), state=state, now=NOW + 1200), ([], []))
        back, _ = self.decide([there], snaps(), state=state, now=WEEK_RESET + 60)
        self.assertEqual([(m["to"], m["reason"], m["prompt"]) for m in back],
                         [("personal", "switch back: personal has reset", None)])
        self.assertNotIn("resets_at", failover.record(state, back[0], WEEK_RESET + 60)["sessions"][SID])

    def test_an_agent_started_lower_down_never_bounces_up_onto_the_used_up_login(self):
        moves, _ = self.decide([sess("work", "idle", 600)])   # no record: the move up looks at once
        self.assertEqual(moves, [])

    def test_one_switched_by_hand_keeps_its_model_and_comes_back_only_home(self):
        by_hand = {"sessions": {SID: {"switched_at": NOW - 3600}}}
        agent = dict(sess("personal", "idle", 600), model=FABLE)
        s = fable_snaps(personal=0, work=25, personal_resets=WEEK_RESET)
        moves, _ = failover.decide([agent], {}, s, by_hand, LADDER, NOW)
        self.assertEqual([(m["to"], m["model"]) for m in moves], [("work", None)])   # Fable kept
        state = failover.record(by_hand, moves[0], NOW)
        self.assertEqual(state["sessions"][SID]["home"], "personal")
        moves, notes = failover.decide([agent], {}, fable_snaps(personal=0, work=0, personal_resets=WEEK_RESET),
                                       by_hand, LADDER, NOW)
        self.assertEqual(moves, [])                                                  # never onto Opus
        self.assertIn("no other Claude login has headroom for claude-fable-5-2 (", notes[0][2])
        back, _ = failover.decide([dict(sess("work", "idle", 600), model=FABLE)], {},
                                  fable_snaps(personal=96, work=25), state, LADDER, WEEK_RESET + 60)
        self.assertEqual([(m["to"], m["reason"]) for m in back], [("personal", "switch back: personal has reset")])

    def test_only_to_a_login_the_repos_owner_may_use(self):
        cfg = json.loads(json.dumps(CFG))
        cfg["usage"]["claude_accounts"]["work"]["only_owners"] = ["employer-org"]
        moves, notes = self.decide([dict(sess("personal", "idle", 600), owner="example-user")], cfg=cfg)
        self.assertEqual(moves, [])
        self.assertIn("(work: ", notes[0][2])
        self.assertEqual([m["to"] for m in self.decide([dict(sess("personal", "idle", 600), owner="employer-org")],
                                                       cfg=cfg)[0]], ["work"])

    def test_a_pass_makes_a_few_idle_moves_and_every_limit_move(self):
        agents = [sess("personal", "idle", 600, sid="s%d" % i, name="idle-%d" % i) for i in range(5)]
        limited = sess("personal", "busy", 0, sid="stuck", name="stuck")
        moves, _ = failover.decide(agents + [limited], {"stuck": {"window": "seven_day", "resets_at": WEEK_RESET,
                                                                   "text": ""}}, used_up(), {}, CFG, NOW)
        go, later = failover.pace(moves, failover.settings(CFG))
        self.assertEqual([m["session"]["name"] for m in go], ["idle-0", "idle-1", "idle-2", "stuck"])
        self.assertEqual([m["session"]["name"] for m in later], ["idle-3", "idle-4"])
        go, later = failover.pace(moves, failover.settings(dict(CFG, failover={"idle_moves_per_pass": 0})))
        self.assertEqual(([m["session"]["name"] for m in go], len(later)), (["stuck"], 5))


class Move(unittest.TestCase):
    def setUp(self):
        self.h = homes.Homes(self, logins=("personal", "work"))
        self.base, self.personal, self.work = self.h.base, self.h.homes["personal"], self.h.homes["work"]
        (self.work / "projects").mkdir()
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

    def test_copy_transcript_and_session_folder(self):
        dst = claude_session.copy_transcript(self.src, self.work, SID)
        self.assertEqual(dst, self.work / "projects" / "-work" / (SID + ".jsonl"))
        self.assertEqual(dst.read_text(), self.src.read_text())
        self.assertTrue((dst.parent / SID / "subagents" / "a.jsonl").exists())
        os.utime(dst, (time.time() + 100, time.time() + 100))
        with self.assertRaisesRegex(RuntimeError, "newer"):
            claude_session.copy_transcript(self.src, self.work, SID)

    def run_migrate(self, live_after, dry_run=False, copy_error=None, live_on="work", jobs=(), pid_reused=False,
                    tmux_error=None, to="work", argv=None, session=None, overrides=None):
        """tmux is the boundary (the agent's pane): its calls are recorded, and tmux_error fails every one. With
        live_after, the relaunched claude is live on login live_on. self.locked gets, for the SIGKILL and each tmux
        call, whether the keys lock was taken then. The move is of session (default: sess() on personal), whose
        claude runs argv, to login `to`, with overrides."""
        calls, killed, trusted = [], [], []
        self.locked = []

        def tmux(*args, check):
            calls.append(args)
            self.locked.append(fakes.keys_taken())
            if tmux_error and check:
                raise commands.CommandError(tmux_error)
            return subprocess.CompletedProcess(args, 0, "", "")

        def kill(pid, sig):
            if sig:
                killed.append((pid, sig))
                self.locked.append(fakes.keys_taken())
            elif (pid, failover.signal.SIGKILL) in killed and not pid_reused:
                raise ProcessLookupError(pid)
        s = dict(session or sess(), cwd=str(self.base))
        if live_after:
            self.h.claude(SID, "agent-x", login=live_on)
        # the agent's process is made up: its signals (os.kill), its argv and background jobs (/proc) and its pane
        patches = [mock.patch.object(claude_session, "movable", return_value=("%5", "")),
                   mock.patch.object(procs, "cmdline", return_value=argv or [
                       "claude", "--model", "m", "--effort", "high", "--dangerously-skip-permissions", "brief"]),
                   mock.patch.object(failover.os, "kill", side_effect=kill),
                   mock.patch.object(claude_session, "wait_dead", return_value=True),
                   mock.patch.object(claude_session.commands, "tmux", side_effect=tmux),
                   mock.patch.object(failover.time, "sleep"),
                   mock.patch.object(claude_session, "background_jobs", return_value=list(jobs))]
        if copy_error:
            patches.append(mock.patch.object(claude_session, "copy_transcript", side_effect=copy_error))
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            ok, detail = failover.migrate(s, to, "carry on", self.cfg, trust=lambda c, d: trusted.append((c, d)),
                                          claude="/bin/claude", dry_run=dry_run, overrides=overrides)
        return ok, detail, calls, killed, trusted

    def test_migrate_kills_copies_trusts_and_relaunches_in_the_pane(self):
        memory = self.personal / "projects" / failover.accounts.project_slug(self.base.resolve()) / "memory"
        memory.mkdir(parents=True)
        ok, detail, calls, killed, trusted = self.run_migrate(live_after=True)
        self.assertTrue(ok, detail)
        self.assertEqual(killed, [(999, failover.signal.SIGKILL)])   # SIGKILL: background jobs survive
        self.assertTrue((self.work / "projects" / "-work" / (SID + ".jsonl")).exists())
        self.assertEqual(trusted, [(str(self.base), str(self.work.resolve()))])
        self.assertEqual(os.readlink(self.work / "projects" / memory.parent.name / "memory"), str(memory))
        self.assertEqual(calls[0], ("set-environment", "-t", "%5", "CLAUDE_CONFIG_DIR", str(self.work.resolve())))
        self.assertEqual(calls[1], ("send-keys", "-t", "%5", "C-u"))   # a stray key typed meanwhile is cleared
        typed = calls[2][-1]
        self.assertTrue(typed.startswith("stty sane; export CLAUDE_CONFIG_DIR=%s; /bin/claude --resume %s --name agent-x"
                                         % (self.work.resolve(), SID)), typed)
        self.assertIn("--model m --effort high --dangerously-skip-permissions 'carry on'", typed)
        self.assertNotIn("brief", typed)
        self.assertEqual(calls[3], ("send-keys", "-t", "%5", "Enter"))
        self.assertEqual(self.locked, [True] * 5)   # the keys lock from the kill until the line's Enter
        self.assertFalse(fakes.keys_taken())

    def registry_dir(self, *entries):
        d = self.personal / "sessions"
        d.mkdir(parents=True)
        for pid, sid in entries:
            (d / ("%d.json" % pid)).write_text(json.dumps({"pid": pid, "sessionId": sid, "name": "agent-x"}))
        (d / ("999.%s.key" % ("0" * 64))).write_text("k")
        return d

    def test_the_killed_claudes_registry_entry_is_removed(self):
        # it cannot remove its own; left there it is a dead entry beside the relaunched one (agent-send would exit 3)
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

    def test_a_keys_lock_not_had_in_time_stops_nothing_and_is_reported(self):
        jobs = [{"pid": 77, "command": "sleep 900", "output": ""}]
        self.cfg["failover"]["background"] = "kill"
        with fakes.keys_held_elsewhere(), mock.patch.object(commands, "LOCK_WAIT_SECONDS", 0.2), \
                mock.patch.object(failover, "stop_jobs") as stop:
            ok, detail, calls, killed, _ = self.run_migrate(live_after=True, jobs=jobs)
        self.assertFalse(ok)
        self.assertIn("not moved, nothing stopped: ", detail)
        self.assertIn("held by another program after 0.2 s of waiting", detail)
        self.assertEqual((killed, calls), ([], []))   # the agent and its jobs left running, nothing typed
        stop.assert_not_called()

    def test_background_jobs_are_named_in_the_note(self):
        jobs = [{"pid": 77, "command": "python train.py", "output": "/t/a.output"}]
        ok, detail, calls, killed, _ = self.run_migrate(live_after=True, jobs=jobs)
        typed = calls[2][-1]
        self.assertIn("still running", typed)
        self.assertIn("pid 77: python train.py (output: /t/a.output)", typed)
        self.assertEqual(killed, [(999, failover.signal.SIGKILL)])   # only claude: the job keeps running
        self.assertTrue(detail.endswith("; its background jobs left running: pid 77"), detail)

    def test_kill_mode_stops_the_jobs_and_says_so(self):
        self.cfg["failover"]["background"] = "kill"
        jobs = [{"pid": 77, "command": "sleep 900", "output": ""}]
        with mock.patch.object(failover, "stop_jobs") as stop:
            ok, detail, calls, killed, _ = self.run_migrate(live_after=True, jobs=jobs)
        stop.assert_called_once_with(jobs)
        self.assertIn("were stopped", calls[2][-1])
        self.assertTrue(detail.endswith("; its background jobs stopped: pid 77"), detail)

    def test_dry_run_touches_nothing(self):
        ok, detail, calls, killed, _ = self.run_migrate(live_after=False, dry_run=True)
        self.assertTrue(ok)
        self.assertIn("would stop pid 999", detail)
        self.assertEqual((calls, killed), ([], []))
        self.assertFalse((self.work / "projects" / "-work").exists())

    def test_a_relaunch_tmux_refuses_is_reported_at_once(self):
        """A keystroke tmux refuses fails the move at once, naming the pane and tmux's error, instead of waiting
        out launch_wait_seconds."""
        ok, detail, calls, killed, _ = self.run_migrate(live_after=False, tmux_error="tmux failed: no such pane: %5")
        self.assertFalse(ok)
        self.assertEqual(killed, [(999, failover.signal.SIGKILL)])
        self.assertIn("relaunching it in pane %5 failed: tmux failed: no such pane", detail)

    def test_not_live_after_relaunch_is_reported(self):
        ok, detail, *_ = self.run_migrate(live_after=False)
        self.assertFalse(ok)
        self.assertIn("not live after 2s", detail)

    def test_a_failed_copy_relaunches_where_it_was_with_its_flags(self):
        ok, detail, calls, killed, _ = self.run_migrate(live_after=True, live_on="personal", copy_error=OSError("disk full"))
        self.assertFalse(ok)   # it runs again, but on the login that is out: still a failed move
        self.assertIn("moving it to work failed (OSError: disk full); relaunched on personal instead", detail)
        typed = [c for c in calls if c[0] == "send-keys" and c[-1] not in ("C-u", "Enter")][0][-1]
        self.assertIn("unset CLAUDE_CONFIG_DIR", typed)
        self.assertIn("--model m --effort high", typed)
        self.assertIn(("set-environment", "-t", "%5", "-r", "CLAUDE_CONFIG_DIR"), calls)

    def test_onto_a_login_with_its_own_settings_and_model(self):
        g = add_gemini(self, self.cfg, self.h)
        gdir, settings = str(Path(g["config_dir"]).resolve()), str(Path(g["settings"]).resolve())
        argv = ["claude", "--model", "claude-opus-5-5", "--settings", "/elsewhere.json", "--effort", "high",
                "--dangerously-skip-permissions"]
        ok, detail, calls, *_ = self.run_migrate(live_after=True, live_on="gemini", to="gemini", argv=argv,
                                                 overrides={"--model": None, "--effort": None})
        self.assertTrue(ok, detail)
        typed = calls[2][-1]
        self.assertTrue(typed.startswith("stty sane; export CLAUDE_CONFIG_DIR=%s; /bin/claude --resume %s"
                                         % (gdir, SID)), typed)
        self.assertIn("--effort high --dangerously-skip-permissions --settings %s --model %s 'carry on'"
                      % (settings, homes.NEON), typed)   # its own model, not the one carried
        self.assertEqual(typed.count("--model"), 1)
        self.assertNotIn("elsewhere", typed)
        # another model asked for: refused before anything is stopped (as agent-switch and agent-spawn refuse it)
        ok, detail, calls, killed, _ = self.run_migrate(live_after=False, to="gemini", argv=argv,
                                                        overrides={"--model": "claude-opus-5-5"})
        self.assertEqual((ok, detail), (False, "not moved: Claude login gemini runs only %s, not claude-opus-5-5: "
                                               "leave out the model" % homes.NEON))
        self.assertEqual((calls, killed), ([], []))

    def test_off_it_the_agent_runs_what_a_new_agent_gets_there(self):
        g = add_gemini(self, self.cfg, self.h)
        gdir = Path(g["config_dir"]).resolve()
        proj = gdir / "projects" / "-work"
        proj.mkdir(parents=True)
        (proj / (SID + ".jsonl")).write_text(entry("user") + "\n")
        argv = ["claude", "--settings", str(g["settings"]), "--model", homes.NEON, "--effort", "high",
                "--dangerously-skip-permissions"]
        on = on_gemini(g)
        for overrides, model in ((None, "--model claude-opus-5-5"),   # what a new agent gets on work
                                 ({"--model": "claude-sonnet-5-5"}, "--model claude-sonnet-5-5")):   # asked for
            ok, detail, calls, *_ = self.run_migrate(live_after=True, session=on, argv=argv, overrides=overrides)
            self.assertTrue(ok, detail)
            typed = calls[2][-1]
            self.assertIn(model, typed)
            self.assertIn("--effort high --dangerously-skip-permissions", typed)
            self.assertEqual(typed.count("--model"), 1)
            self.assertNotIn("--settings", typed)
            self.assertNotIn(homes.NEON, typed)
        # asked for gemini's own model, which work cannot run: refused before anything is stopped (as agent-switch)
        ok, detail, calls, killed, _ = self.run_migrate(live_after=False, session=on, argv=argv,
                                                        overrides={"--model": homes.NEON})
        self.assertFalse(ok)
        self.assertEqual(detail, "not moved: Claude login work cannot run %s, the model of a login that runs no "
                                 "other: leave out the model" % homes.NEON)
        self.assertEqual((calls, killed), ([], []))
        self.cfg["spawn_claude_model"] = None   # the CLI's own model: no --model at all
        ok, detail, calls, *_ = self.run_migrate(live_after=True, session=on, argv=argv)
        self.assertIn("--effort high --dangerously-skip-permissions 'carry on'", calls[2][-1])
        self.assertNotIn("--model", calls[2][-1])

    def test_a_failed_move_onto_it_relaunches_where_it_was_on_its_own_model(self):
        """gemini's model must not reach the Claude login it stays on: every request there would fail."""
        g = add_gemini(self, self.cfg, self.h)
        ok, detail, calls, *_ = self.run_migrate(live_after=True, live_on="personal", to="gemini",
                                                 copy_error=OSError("disk full"),
                                                 overrides={"--model": homes.NEON, "--effort": "low"})
        self.assertFalse(ok)
        self.assertIn("moving it to gemini failed (OSError: disk full); relaunched on personal instead", detail)
        typed = [c for c in calls if c[0] == "send-keys" and c[-1] not in ("C-u", "Enter")][0][-1]
        self.assertIn("unset CLAUDE_CONFIG_DIR", typed)
        self.assertIn("--model m --effort low --dangerously-skip-permissions 'carry on'", typed)   # the effort asked
        self.assertNotIn(homes.NEON, typed)
        self.assertNotIn(str(Path(g["settings"]).resolve()), typed)

    def test_a_background_session_onto_it_resumes_with_its_settings_and_model(self):
        g = add_gemini(self, self.cfg, self.h)
        ok, detail, runs, *_ = self.run_bg_migrate(to="gemini")
        self.assertTrue(ok, detail)
        launch = runs[1][0]
        self.assertEqual(launch[4:12], ["--name", "nightly report r7k2m9xq", "--effort", "high",
                                        "--permission-mode", "bypassPermissions", "--settings",
                                        str(Path(g["settings"]).resolve())])
        self.assertEqual(launch[12:14], ["--model", homes.NEON])   # not the Fable 5.1 its transcript recorded
        self.assertEqual(runs[1][1]["env"]["CLAUDE_CONFIG_DIR"], str(Path(g["config_dir"]).resolve()))
        ok, detail, runs, *_ = self.run_bg_migrate(to="gemini", dry_run=True, live_after=False)
        self.assertEqual((ok, runs), (True, []))
        self.assertIn("--settings %s --model %s 'carry on'" % (Path(g["settings"]).resolve(), homes.NEON), detail)

    def test_refuses_what_it_cannot_move(self):
        with mock.patch.object(claude_session, "pane_pid", return_value=None), \
                mock.patch.object(failover.os, "kill", side_effect=AssertionError("must not stop anything")):
            ok, detail = failover.migrate(sess(), "work", None, self.cfg)
        self.assertFalse(ok)
        self.assertIn("not moved: pane or process gone", detail)
        cfg = json.loads(json.dumps(self.cfg))
        cfg["usage"]["claude_accounts"]["work"]["config_dir"] = "/nonexistent/x"
        self.assertIn("not ready", failover.migrate(sess(), "work", None, cfg)[1])

    def run_bg_migrate(self, stop_rc=0, launch_rc=0, live_after=True, dry_run=False, jobs=(), dead=True,
                       launch_out=(), overrides=None, copy_rc=None, to="work"):
        """A Claude Code background session on personal, out of Fable credits, moved to login `to`.
        The claude CLI is the boundary: stop_rc answers `claude stop` of the session, copy_rc {"stop" or "rm": exit
        code} the same for a copy, launch_rc and launch_out (one per resume) `claude --resume --bg`."""
        self.src.write_text("\n".join([json.dumps({"type": "permission-mode", "permissionMode": "bypassPermissions"}),
                                       reply("claude-fable-5-1", "high"), credits_record()]) + "\n")
        runs, stopped, capped = [], [], []
        s = dict(sess(), kind="bg", tmux=None, cwd=str(self.base), name="nightly report r7k2m9xq")
        if live_after:   # the session the login's daemon resumed
            self.h.claude(SID, s["name"], login=to, kind="bg")

        outs = list(launch_out)
        real_run = subprocess.run

        def kill(pid, sig):
            if sig:   # signal 0, the registry asking whether a pid is alive, is no kill
                raise AssertionError("a bg session is stopped, not killed")

        def run(cmd, **kw):
            if cmd[0] == "git":   # repo_root's, on the temp folder: run for real
                return real_run(cmd, **kw)
            runs.append((cmd, kw))
            if cmd[1] in ("stop", "rm"):
                rc = stop_rc if cmd[2] == SID[:8] else (copy_rc or {}).get(cmd[1], 0)
            else:
                rc = launch_rc
            said = outs.pop(0) if cmd[1] == "--resume" and outs else "backgrounded · %s" % SID[:8]
            return subprocess.CompletedProcess(cmd, rc, said if rc == 0 else "", "boom" if rc else "")
        patches = [mock.patch.object(procs, "cmdline", return_value=["claude", "bg-spare", "--bg-spare", "/x.sock"]),
                   mock.patch.object(claude_session.subprocess, "run", side_effect=run),
                   mock.patch.object(failover.os, "kill", side_effect=kill),
                   mock.patch.object(claude_session, "wait_dead", return_value=dead),
                   mock.patch.object(claude_session.commands, "tmux", side_effect=AssertionError("no tmux for a bg session")),
                   mock.patch.object(failover.time, "sleep"),
                   mock.patch.object(claude_session, "background_jobs", return_value=list(jobs)),
                   mock.patch.object(failover, "stop_jobs", side_effect=stopped.append),
                   mock.patch("agentmail.memcap.cap_bg_session",
                              side_effect=lambda sid, name, props, session_pid=None:
                              capped.append((sid, name, session_pid())) or "agent-x.scope")]
        # also its argv and jobs (/proc), and the memory cap's systemd scope
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            ok, detail = failover.migrate(s, to, "carry on", self.cfg, trust=lambda c, d: None,
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
        note = ("backgrounded · 1d4e45b2 · axx\nnote: background session %s keeps its own saved options, so the flags "
                "you passed started a copy as 1d4e45b2. Without flags, the same command continues %s itself."
                % (SID[:8], SID[:8]))
        ok, detail, runs, *_ = self.run_bg_migrate(launch_out=[note, "backgrounded · %s" % SID[:8]])
        self.assertTrue(ok, detail)
        self.assertEqual([r[0][1:3] for r in runs], [["stop", SID[:8]], ["--resume", SID], ["stop", "1d4e45b2"],
                                                     ["rm", "1d4e45b2"], ["--resume", SID]])
        self.assertEqual(runs[4][0], ["/bin/claude", "--resume", SID, "--bg", "carry on"])
        self.assertIn("claude started a copy 1d4e45b2, which was stopped and removed", detail)
        # a copy that will not stop is a failed move naming it, and the session is not resumed beside it
        ok, detail, runs, *_ = self.run_bg_migrate(launch_out=[note], copy_rc={"stop": 1})
        self.assertFalse(ok)
        self.assertIn("claude started a copy 1d4e45b2 and `claude stop 1d4e45b2` failed: boom; the copy may still "
                      "be running", detail)
        self.assertEqual([r[0][1] for r in runs], ["stop", "--resume", "stop"])
        # one that stops but is not removed: the session is resumed, and the leftover said
        ok, detail, runs, *_ = self.run_bg_migrate(launch_out=[note], copy_rc={"rm": 1})
        self.assertTrue(ok, detail)
        self.assertIn("claude started a copy 1d4e45b2, which was stopped but not removed: boom", detail)

    def test_an_invalid_memory_cap_is_said_not_just_left_off(self):
        self.cfg["agent_memory_max"] = "lots"
        ok, detail, runs, stopped, capped = self.run_bg_migrate()
        self.assertTrue(ok, detail)
        self.assertIn("memory: uncapped: bad size 'lots' (use e.g. 32G, 512M or off); agent-x.scope", detail)
        self.assertEqual(len(capped), 1)   # still in a scope of its own, its daemon out of this service

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
        self.assertEqual(capped, [(SID, "nightly report r7k2m9xq", os.getpid())])   # its registry entry's pid
        self.assertTrue(detail.endswith("; its background jobs stopped: pid 77"), detail)

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
        with mock.patch.object(claude_session, "pane_pid", return_value=100), \
                mock.patch.object(procs, "ppid", return_value=100):
            self.assertEqual(claude_session.movable(sess()), ("%5", ""))
        with mock.patch.object(claude_session, "pane_pid", return_value=100), \
                mock.patch.object(procs, "ppid", return_value=7):
            self.assertIn("not agent-spawn's layout", claude_session.movable(sess())[1])
        self.assertEqual(claude_session.movable(dict(sess(), tmux=None))[1], "not in tmux")
        self.assertEqual(claude_session.movable(dict(sess(), tmux=None, kind="bg")), (claude_session.BG, ""))
        self.assertEqual(claude_session.movable(dict(sess(), kind="bg", live=False))[1], "not live")


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
        cfg["usage"]["claude_accounts"]["personal"]["exclude_owners"] = ["Example-Org"]
        self.assertEqual(self.pick(snaps(), owner="Example-Org", cfg=cfg), "work")
        self.assertEqual(self.pick(snaps(), owner="example-user", cfg=cfg), "personal")

    def test_with_every_login_low_the_most_left_and_with_none_known_it_stays(self):
        self.assertEqual(self.pick(snaps(personal=5, work=8)), "work")
        self.assertEqual(failover.resume_login("personal", None, None, snaps(personal=0, work=0), CFG, NOW)[0],
                         "personal")                                              # all used up: no point moving it
        self.assertEqual(self.pick({}), "work")                                  # usage unknown


class PlaceResume(unittest.TestCase):
    def setUp(self):
        h = homes.Homes(self, logins=("personal", "work"))
        self.base, self.personal, self.work = h.base, h.homes["personal"], h.homes["work"]
        Path(self.work, ".credentials.json").write_text("{}")
        self.cfg = json.loads(json.dumps(CFG))
        self.cfg["usage"]["claude_accounts"]["work"]["config_dir"] = str(self.work)
        self.trusted = []
        self.memory = self.personal / "projects" / failover.accounts.project_slug(self.base) / "memory"
        self.memory.mkdir(parents=True)                                 # the default login's auto memory
        self.link = self.work / "projects" / self.memory.parent.name / "memory"

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
        self.assertFalse(self.link.exists())                            # the default login has the memory itself

    def test_to_a_second_login_the_memory_is_linked(self):
        self.copy(self.personal, "newest", 0)
        config_dir, note = self.place(snaps(personal=0))
        self.assertEqual(config_dir, str(self.work))
        self.assertIn("newest", (self.work / "projects" / "-work" / (SID + ".jsonl")).read_text())
        self.assertTrue((self.work / "projects" / "-work" / SID / "subagents").is_dir())
        self.assertEqual(os.readlink(self.link), str(self.memory))

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

    def test_a_failed_copy_or_a_damaged_usage_cache_stops_the_resume(self):
        """It is not resumed where it was instead: agent-send fails, and the mailroom holds the mail and alerts."""
        self.copy(self.work, "after", 0)
        (self.personal / "projects" / "-work").write_text("")   # a file where the copy's folder would go
        with self.assertRaises(FileExistsError):
            self.place(snaps())
        self.assertEqual(self.trusted, [])
        damaged = failover.config.STATE_DIR / "damaged-usage-cache.json"
        damaged.write_text("{not json")
        with mock.patch.object(failover.usage, "CACHE_NAME", damaged.name), \
                self.assertRaisesRegex(ValueError, "damaged-usage-cache.json is not JSON"):
            self.place(None)

    def test_an_owner_git_cannot_read_stops_the_choice(self):
        """Owner rules need the repo's owner: when git cannot say, no login is chosen and nothing is resumed."""
        self.copy(self.work, "after", 0)
        with mock.patch.object(failover.accounts, "repo_owner",
                               side_effect=commands.CommandError("git ls-remote --get-url origin failed")), \
                self.assertRaisesRegex(commands.CommandError, "git ls-remote"):
            self.place(snaps())
        self.assertEqual(self.trusted, [])

    def test_a_pinned_session_resumes_where_its_newest_copy_is(self):
        self.copy(self.personal, "the planner", 0)
        sess = {"id": SID, "name": "nightly-report", "cwd": str(self.base), "config_dir": None}
        config_dir, note = failover.place_resume(sess, self.cfg, snaps=snaps(personal=0), now=NOW)
        self.assertIsNone(config_dir)                                   # personal, though work has headroom
        self.assertIn("pinned (failover.pinned)", note)
        self.assertFalse((self.work / "projects" / "-work" / (SID + ".jsonl")).exists())

    def test_no_transcript_resumes_where_it_was(self):
        self.assertEqual(self.place(snaps()), (str(self.work), ""))

    def test_a_session_on_a_manual_login_resumes_there_and_none_is_placed_on_one(self):
        h = homes.Homes(self, logins=("personal", "work"))
        self.cfg["usage"]["claude_accounts"]["work"]["config_dir"] = str(h.homes["work"])
        Path(h.homes["work"], ".credentials.json").write_text("{}")
        g = add_gemini(self, self.cfg, h)
        gdir = str(Path(g["config_dir"]).resolve())
        self.copy(h.homes["personal"], "before", 3600)
        self.copy(Path(gdir), "on gemini", 0)
        sess = {"id": SID, "cwd": str(self.base), "config_dir": gdir}
        config_dir, note = failover.place_resume(sess, self.cfg, snaps=snaps(), now=NOW)
        self.assertEqual(config_dir, gdir)            # though personal has headroom
        self.assertIn("on login gemini, where its newest copy is: a manual login, which nothing moves it off", note)
        os.utime(self.copy(h.homes["personal"], "newest", 0), (NOW + 60, NOW + 60))
        gem = dict(usage.per_token(g), cli="claude", account="gemini")
        config_dir, note = failover.place_resume(dict(sess, config_dir=None), self.cfg,
                                                 snaps=dict(snaps(personal=0, work=0), **{"claude:gemini": gem}),
                                                 now=NOW)
        self.assertIsNone(config_dir)                 # every plan used up: where its newest copy is, never gemini
        self.assertIn("no login with known headroom", note)


class BackgroundJobs(unittest.TestCase):
    def test_lists_bash_tool_commands_with_their_output_files(self):
        argv = {11: ["/bin/bash", "-c", "source snap.sh 2>/dev/null || true && eval 'python train.py --x '\"'\"'a b'\"'\"'' "
                                              "< /dev/null && pwd -P >| /tmp/cwd"],
                12: ["npm", "exec", "@gongrzhe/server-gmail-autoauth-mcp"],
                13: ["/bin/bash", "-c", "eval 'sleep 900' < /dev/null"]}
        outs = {11: "/t/tasks/a.output", 13: "pipe:[123]"}
        with mock.patch.object(procs, "cmdline", side_effect=lambda p: argv.get(p, [])), \
                mock.patch.object(failover.os, "readlink", side_effect=lambda path: outs[int(path.split("/")[2])]):
            jobs = claude_session.background_jobs(10, {11: 10, 12: 10, 13: 10, 14: 99})
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

    def test_unreadable_entries_are_skipped_and_said(self):
        """A session file Claude Code is writing this moment, or a damaged line in Codex's name index, is skipped
        with a line on stderr naming the file; a file that is gone since the listing is a session that ended."""
        home = Path(tempfile.mkdtemp())
        (home / "sessions").mkdir()
        (home / "sessions" / "1.json").write_text(json.dumps({"pid": 1, "sessionId": "a", "name": "a"}))
        (home / "sessions" / "2.json").write_text('{"pid": 2, "sessi')
        with contextlib.redirect_stderr(io.StringIO()) as err:
            found = registry._claude_sessions_in(home / "sessions", "personal", None)
        self.assertEqual([s["id"] for s in found], ["a"])
        self.assertIn("%s is not JSON" % (home / "sessions" / "2.json"), err.getvalue())
        (home / "session_index.jsonl").write_text('{"id": "t1", "thread_name": "loop"}\n{"id": "t2", "thr\n')
        with mock.patch.object(registry, "CODEX_HOME", home), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(registry.codex_names(), {"t1": "loop"})
        self.assertEqual(err.getvalue(), "%s: skipped 1 line(s) that are not JSON objects\n" % (home / "session_index.jsonl"))
        (home / "session_index.jsonl").write_text('{"id": "t1", "thread_name": "loop"}\n{"thread_name": "no id"}\n')
        with mock.patch.object(registry, "CODEX_HOME", home), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(registry.codex_names(), {"t1": "loop"})
        self.assertEqual(err.getvalue(), "%s: skipped 1 line(s) with no thread id\n" % (home / "session_index.jsonl"))

    def test_an_exited_session_resumes_from_its_newest_copy(self):
        h = homes.Homes(self, logins=("personal", "work"))
        a, b = h.homes["personal"], h.homes["work"]
        for home, age in ((a, 100), (b, 0)):
            p = home / "projects" / "-work" / (SID + ".jsonl")
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({"cwd": str(h.base)}) + "\n")
            os.utime(p, (time.time() - age, time.time() - age))
        self.assertEqual(registry.claude_transcript_session(SID)["config_dir"], str(b))
        os.utime(a / "projects" / "-work" / (SID + ".jsonl"), (time.time() + 50, time.time() + 50))
        self.assertIsNone(registry.claude_transcript_session(SID)["config_dir"])
        # a line that is JSON but no object carries no folder; the next one that does is found
        p = a / "projects" / "-work" / (SID + ".jsonl")
        p.write_text("3\n" + json.dumps({"cwd": "/w2"}) + "\n")
        os.utime(p, (time.time() + 50, time.time() + 50))
        self.assertEqual(registry.claude_transcript_session(SID)["cwd"], "/w2")

    @unittest.skipIf(os.geteuid() == 0, "root reads a file whatever its mode")
    def test_a_transcript_that_cannot_be_read_is_an_error_not_a_session_with_no_folder(self):
        """Its folder is what lets agent-send refuse to resume it in the wrong one."""
        home = homes.Homes(self, logins=("personal",)).default
        p = home / "projects" / "-work" / (SID + ".jsonl")
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"cwd": "/work"}) + "\n")
        p.chmod(0)
        self.addCleanup(p.chmod, 0o600)
        with self.assertRaisesRegex(PermissionError, str(p)):
            registry.claude_transcript_session(SID)


class Hook(unittest.TestCase):
    def test_stop_failure_is_logged_as_a_limit(self):
        st = load_script("agent-status")
        homes.status_log(self)
        homes.Homes(self).claude(SID, "agent-x", cwd="/work")   # the session the hook runs in
        payload = {"hook_event_name": "StopFailure", "session_id": SID, "cwd": "/work", "error": "rate_limit",
                   "error_details": "You've hit your session limit"}
        for error in ("rate_limit", "server_error"):
            with mock.patch.object(st.sys, "stdin", io.StringIO(json.dumps(dict(payload, error=error)))):
                st.cmd_hook(mock.Mock(cli="claude"))
        limit, other = statuslog.read()
        self.assertEqual((limit["event"], limit["session_id"], limit["name"]), ("limit", SID, "agent-x"))
        self.assertEqual(limit["text"], "rate_limit: You've hit your session limit")
        self.assertEqual(other["event"], "api-error")


class MoveByHand(unittest.TestCase):
    def test_a_failed_move_by_hand_is_in_the_status_log(self):
        """Not only successes: a move that failed after it touched the agent leaves a trace."""
        af = load_script("agent-failover")
        homes.Homes(self, logins=("personal", "work"), cfg=CFG).claude(SID, "agent-x", cwd="/work")
        homes.status_log(self)
        with mock.patch.object(af.failover, "migrate", return_value=(False, "pid 999 did not exit after SIGKILL")), \
                contextlib.redirect_stdout(io.StringIO()):   # migrate would stop a real agent
            self.assertEqual(af.main(["move", SID, "--to", "work"]), 1)
            [e] = af.statuslog.read(event="failover")
        self.assertEqual((e["session_id"], e["account"], e["text"]),
                         (SID, "work", "personal -> work (by hand): FAILED pid 999 did not exit after SIGKILL"))

    def test_status_lists_the_agents_it_moved(self):
        """Not an agent switched by hand (agent-switch) or one that only waited: neither has moves to show."""
        af = load_script("agent-failover")
        moved = failover.record({}, {"session": sess(), "to": "work", "reason": "limit: five-hour limit"}, NOW)
        state = failover.switched_by_hand(moved, "by-hand", NOW)
        state["sessions"]["waited"] = {"stuck_until": NOW + 60}
        with mock.patch.object(af.failover.STATE, "path", Path(tempfile.mkdtemp()) / "failover.json"):
            af.failover.STATE.write(state)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(af.main(["status"]), 0)
            [line] = out.getvalue().splitlines()
            self.assertRegex(line, r"^agent-x +3d3d3d3d  first login personal +now work +last move .*: limit: five")
            moved = failover.record({}, {"session": sess(), "to": "personal", "model": "claude-opus-5-5",
                                         "from_model": "claude-fable-5-2", "reason": "limit: weekly Fable"}, NOW)
            af.failover.STATE.write(moved)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                af.main(["status"])
            self.assertRegex(out.getvalue(), r"first login personal \(claude-fable-5-2\) +now personal "
                                             r"\(claude-opus-5-5\) +last move")
            af.failover.STATE.write(failover.switched_by_hand({}, "by-hand", NOW))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                af.main(["status"])
            self.assertEqual(out.getvalue(), "no agent has been moved\n")


class Watch(unittest.TestCase):
    """The watch's passes, with the usage probes (they ask Anthropic), the move (failover.migrate, driven by Move)
    and the folder trust in the user's files made up."""

    @staticmethod
    def put(h, sid, name, lines, status, updated_at):
        """A live background agent in the temporary Claude homes h, in status since updated_at, its transcript these
        lines: a registry entry and a transcript, which the watch's sample() reads. Returns the entry's path."""
        entry_path = h.claude(sid, name, cwd="/work", updated_at=updated_at, kind="bg", status=status)
        t = h.default / "projects" / "-work" / (sid + ".jsonl")
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text("\n".join(lines) + "\n")
        return entry_path

    def agent(self, *lines, login="personal", name="agent-x"):
        """A live background agent on login, busy, its transcript these lines (put)."""
        self.put(homes.Homes(self, logins=(login,)), SID, name, lines, "busy", NOW)

    def test_one_pass_moves_a_limited_agent_and_saves_state(self):
        af = load_script("agent-failover")
        homes.status_log(self)
        self.agent(entry("user"), rejection(NOW + 600))
        moved = []
        with mock.patch.object(af.usage, "snapshots", return_value=snaps(personal=0)), \
                mock.patch.object(af.failover, "migrate", side_effect=lambda s, to, prompt, cfg, **k:
                                  moved.append((s["id"], to, prompt)) or (True, "moved")), \
                mock.patch.object(af, "trust", return_value=None), contextlib.redirect_stdout(io.StringIO()):
            state, moves, _ = af.one_pass(CFG, {}, False, {"idle": 0, "spend": NOW}, NOW)
        self.assertEqual([(m[0], m[1]) for m in moved], [(SID, "work")])
        self.assertEqual(state["sessions"][SID]["home"], "personal")
        [e] = statuslog.read(event="failover")
        self.assertEqual((e["session_id"], e["account"]), (SID, "work"))
        self.assertTrue(e["text"].startswith("personal -> work ("), e["text"])

    def test_a_pass_reads_each_agents_repo_owner_for_the_logins_owner_rules(self):
        af = load_script("agent-failover")
        repo = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:Example-Org/x.git"],
                       check=True)
        broken = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(broken)], check=True)
        with open(broken / ".git" / "config", "a") as fh:
            fh.write("[broken\n")                                       # git cannot tell its owner
        with contextlib.redirect_stdout(io.StringIO()) as out:
            got = af.with_owners([dict(sess(), cwd=str(repo)), dict(sess(sid="other"), cwd="/no/such/folder"),
                                  dict(sess(sid="broken-one", name="agent-broken"), cwd=str(broken))])
        self.assertEqual([(a["id"], a["owner"]) for a in got], [(SID, "Example-Org"), ("other", None)])
        self.assertIn("agent-broken (broken-o) not moved in this pass: git ls-remote --get-url origin in",
                      out.getvalue())

    def test_a_fable_agent_out_of_credits_end_to_end(self):
        """A background agent on personal, Fable 5.1, out of Fable credits (weekly Fable 0%, extra usage off). The
        watch must see it and move it to work, which has Fable left."""
        af = load_script("agent-failover")
        homes.status_log(self)
        self.agent(reply(), entry("user", "[email from Alex] carry on"), credits_record(),
                   name="nightly report r7k2m9xq")
        moved = []
        with mock.patch.object(af.usage, "snapshots", return_value=fable_snaps(personal=0, work=25)), \
                mock.patch.object(af.failover, "migrate", side_effect=lambda s, to, prompt, cfg, **k:
                                  moved.append((s["id"], s["model"], to, prompt)) or (True, "moved")), \
                mock.patch.object(af, "trust", return_value=None), contextlib.redirect_stdout(io.StringIO()) as out:
            state, moves, _ = af.one_pass(CFG, {}, False, {"idle": NOW, "spend": NOW}, NOW)
        self.assertEqual([m[:3] for m in moved], [(SID, "claude-fable-5-1", "work")])
        self.assertIn("out of Fable 5.1 usage credits", moved[0][3])
        self.assertIn("moved nightly report r7k2m9xq", out.getvalue())
        self.assertEqual(state["sessions"][SID]["home"], "personal")
        self.assertEqual(state["sessions"][SID]["resets_at"], NOW + 3600)   # personal's weekly Fable reset
        self.assertEqual([e["account"] for e in statuslog.read(event="failover")], ["work"])

    def test_with_no_fable_left_anywhere_the_pass_restarts_it_on_opus_on_its_login(self):
        af = load_script("agent-failover")
        homes.status_log(self)
        self.agent(reply("claude-fable-5-2"), credits_record(), name="agent-x")
        moved = []
        with mock.patch.object(af.usage, "snapshots", return_value=fable_snaps(personal=0, work=0)), \
                mock.patch.object(af.failover, "migrate", side_effect=lambda s, to, prompt, cfg, **k:
                                  moved.append((to, k["overrides"], prompt)) or (True, "moved")), \
                mock.patch.object(af, "trust", return_value=None), contextlib.redirect_stdout(io.StringIO()):
            state, _, _ = af.one_pass(LADDER, {}, False, {"idle": NOW, "spend": NOW}, NOW)
        self.assertEqual([m[:2] for m in moved], [("personal", {"--model": "claude-opus-5-5"})])
        self.assertIn("moved to login 'personal' on model claude-opus-5-5", moved[0][2])
        e = state["sessions"][SID]
        self.assertEqual((e["home"], e["home_model"], e["model"]), ("personal", "claude-fable-5-2", "claude-opus-5-5"))
        [log] = statuslog.read(event="failover")
        self.assertIn("(model claude-opus-5-5)", log["text"])

    def test_a_wait_is_noted_once_across_passes(self):
        """The daemon's passes: the first notes that the agent waits for its reset and saves that; the next does not
        note it again."""
        af = load_script("agent-failover")
        homes.status_log(self)
        self.agent(entry("user"), rejection(NOW + 600), login="default")
        state = jsonfile.JsonFile(Path(tempfile.mkdtemp()) / "failover.json", jsonfile.PLAIN, 1)
        with mock.patch.object(af.usage, "snapshots", return_value={}), \
                mock.patch.object(af.failover, "STATE", state), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual((af.main(["--once"]), af.main(["--once"])), (0, 0))
        self.assertEqual(out.getvalue().count("waiting: "), 1, out.getvalue())
        self.assertEqual(state.read()["sessions"][SID]["stuck_until"], NOW + 600)
        self.assertEqual(len(statuslog.read(event="limit-wait")), 1)

    def idle_agents(self, n):
        """n live background agents idle for 10 minutes on personal, with work as a second login (put). Returns (the
        homes, {session id: its registry entry's path})."""
        h = homes.Homes(self, logins=("personal", "work"))
        return h, {sid: self.put(h, sid, "agent-x" if n == 1 else "idle-%d" % i, [reply("claude-opus-5-5")], "idle",
                                 NOW - 600)
                   for i, sid in enumerate([SID] if n == 1 else ["s%d" % i for i in range(n)])}

    def test_idle_agents_leave_a_used_up_login_a_few_each_pass(self):
        """Agents idle on a used-up login: each pass moves idle_moves_per_pass of them, one after another, and with
        some left looks again at the next pass rather than after the usual wait."""
        af = load_script("agent-failover")
        homes.status_log(self)
        h, entries = self.idle_agents(5)
        moved = []

        def move(s, to, prompt, cfg, **k):
            """As a move leaves the registry: the session's entry under the login it went to."""
            entries[s["id"]].rename(h.homes[to] / "sessions" / entries[s["id"]].name)
            moved.append(s["id"])
            return True, "moved"
        (h.homes["work"] / "sessions").mkdir()
        with mock.patch.object(af.usage, "snapshots", return_value=used_up()), \
                mock.patch.object(af.failover, "migrate", side_effect=move), \
                mock.patch.object(af, "trust", return_value=None), contextlib.redirect_stdout(io.StringIO()) as out:
            state, moves, last = af.one_pass(CFG, {}, False, {"idle": 0, "spend": NOW}, NOW)
            self.assertEqual(len(moved), 3)                                      # in the registry's order
            self.assertEqual(last["idle"], NOW - af.IDLE_LOOK_SECONDS)         # due again at the next pass
            said = out.getvalue().split("2 idle move(s) left for the next pass (failover.idle_moves_per_pass: 3): ")
            self.assertEqual(sorted(said[1].splitlines()[0].split(", ")),
                             ["idle-%s -> work" % sid[1:] for sid in sorted(set(entries) - set(moved))])
            state, moves, last = af.one_pass(CFG, state, False, last, NOW + 20)
            self.assertEqual(sorted(moved), ["s0", "s1", "s2", "s3", "s4"])
            self.assertEqual(last["idle"], NOW + 20)                            # nothing left: the usual wait
            state, moves, last = af.one_pass(CFG, state, False, {"idle": 0, "spend": NOW}, NOW + 40)
        self.assertEqual(moves, [])                                              # all on work now: nothing to do
        self.assertEqual({e["resets_at"] for e in state["sessions"].values()}, {WEEK_RESET})
        self.assertTrue(all(e["text"].startswith("personal -> work (spent: personal has no headroom")
                            for e in statuslog.read(event="failover")))
        self.assertEqual(statuslog.read(event="limit-wait"), [])

    def test_a_pass_whose_every_idle_move_fails_waits_the_usual_time(self):
        """So that a move that keeps failing is not tried again every pass."""
        af = load_script("agent-failover")
        homes.status_log(self)
        self.idle_agents(5)
        with mock.patch.object(af.usage, "snapshots", return_value=used_up()), \
                mock.patch.object(af.failover, "migrate", return_value=(False, "not moved: no transcript")), \
                mock.patch.object(af, "trust", return_value=None), contextlib.redirect_stdout(io.StringIO()):
            _, moves, last = af.one_pass(CFG, {}, False, {"idle": 0, "spend": NOW}, NOW)
        self.assertEqual((len(moves), last["idle"]), (3, NOW))

    def test_an_idle_agent_with_nowhere_to_go_is_journaled_once_and_not_shown_stopped(self):
        af = load_script("agent-failover")
        homes.status_log(self)
        self.idle_agents(1)
        with mock.patch.object(af.usage, "snapshots", return_value=used_up(work=0)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            state, moves, _ = af.one_pass(CFG, {}, False, {"idle": 0, "spend": NOW}, NOW)
            af.one_pass(CFG, state, False, {"idle": 0, "spend": NOW}, NOW + 20)
        self.assertEqual(moves, [])
        self.assertEqual(out.getvalue().count("waiting: agent-x (3d3d3d3d) is idle on personal"), 1, out.getvalue())
        self.assertEqual(statuslog.read(event="limit-wait"), [])

    def test_no_usage_read_when_nothing_is_limited_or_idle_and_no_spend_check_is_due(self):
        af = load_script("agent-failover")
        self.agent(entry("user"), reply())
        last = {"idle": NOW, "spend": NOW - 299}
        with mock.patch.object(af.usage, "snapshots", side_effect=AssertionError("no usage read needed")):
            self.assertEqual(af.one_pass(CFG, {}, False, last, NOW), ({}, [], last))
        with mock.patch.object(af.usage, "snapshots", return_value=snaps()) as read:
            state, moves, done = af.one_pass(CFG, {}, False, last, NOW + 1)
        self.assertEqual((read.call_count, moves, done), (1, [], {"idle": NOW, "spend": NOW + 1}))


def billing(work=0, extra=True, used=211.41, limit=None, personal=80):
    """snaps() with work's extra usage on (or off), `used` spent this month, and personal's too."""
    s = snaps(personal=personal, work=work, work_extra=extra)
    for t in s:
        s[t]["extra_usage"] = {"enabled": extra if t == "claude:work" else False, "used": used,
                               "monthly_limit": limit, "currency": "USD", "limit_reached": False}
    return s


def on(account, name, model="claude-opus-5-5", sid=None):
    return dict(sess(account, name=name, sid=sid or name), model=model)


def add_gemini(test, cfg, h=None):
    """The per-token login gemini (homes.per_token_login) beside the others: in cfg and, with h (a homes.Homes), in
    its homes and config.json, so the registry reads it. Returns its account entry."""
    g = homes.per_token_login(test, h.base if h else None)
    cfg["usage"]["claude_accounts"]["gemini"] = g
    if h:
        h.homes["gemini"] = Path(g["config_dir"])
        d = json.loads(h.config.read_text())
        d["usage"]["claude_accounts"]["gemini"] = g
        h.config.write_text(json.dumps(d))
    return g


def on_gemini(g, **kw):
    return dict(sess(**kw), account="gemini", config_dir=str(Path(g["config_dir"]).resolve()))


class PerTokenLogin(unittest.TestCase):
    """A manual login (gemini: billed per token, with a settings file and a model of its own) is a person's choice:
    nothing moves an agent onto it or off it, and a move onto or off it runs the right model."""

    def setUp(self):
        self.cfg = json.loads(json.dumps(CFG))
        self.g = add_gemini(self, self.cfg)
        self.snaps = dict(snaps(), **{"claude:gemini": dict(usage.per_token(self.g), cli="claude", account="gemini")})

    def decide(self, sessions, limits=None, s=None, state=None):
        return failover.decide(sessions, limits or {}, s or self.snaps, state or {}, self.cfg, NOW)

    def test_an_agent_on_it_is_never_moved_off(self):
        hit = {SID: {"window": "five_hour", "resets_at": NOW + 3600, "text": ""}}
        moves, notes = self.decide([on_gemini(self.g)], hit)
        self.assertEqual(moves, [])
        self.assertIn("hit its limit on gemini (five-hour limit, resets %s); that is a manual login "
                      "(usage.claude_accounts), which nothing moves it off, so it waits for the reset"
                      % usage.fmt_reset(NOW + 3600, NOW), notes[0][2])
        self.assertEqual(self.decide([on_gemini(self.g, status="idle", idle_for=3600)]), ([], []))   # no move up
        moved_once = {"sessions": {SID: {"home": "personal", "current": "gemini", "resets_at": NOW - 60, "moves": []}}}
        self.assertEqual(self.decide([on_gemini(self.g, status="idle", idle_for=3600)], state=moved_once), ([], []))

    def test_nothing_moves_an_agent_onto_it(self):
        hit = {SID: {"window": "five_hour", "resets_at": NOW + 3600, "text": ""}}
        moves, notes = self.decide([sess()], hit, dict(self.snaps, **snaps(personal=0, work=0)))
        self.assertEqual(moves, [])                      # though it is the one login with room
        self.assertIn("no other Claude login has headroom", notes[0][2])
        self.assertNotIn("gemini", notes[0][2])
        # nor back to it, after a move by hand off it (agent-failover move records it as the first login)
        state = {"sessions": {SID: {"switched_at": NOW - 7200, "home": "gemini", "home_model": homes.NEON,
                                    "current": "work", "resets_at": NOW - 60, "moves": []}}}
        self.assertEqual(self.decide([sess("work", "idle", 3600)], s=dict(self.snaps, **snaps(personal=0)),
                                     state=state)[0], [])

    def test_an_idle_agent_leaving_a_used_up_login_never_goes_to_it(self):
        s = dict(self.snaps, **used_up(work=0))
        moves, notes = self.decide([sess("personal", "idle", 600)], s=s)
        self.assertEqual(moves, [])                      # though it is the one login with room
        self.assertIn("is idle on personal, which has no headroom", notes[0][2])
        self.assertNotIn("gemini", notes[0][2])
        self.assertEqual(self.decide([on_gemini(self.g, status="idle", idle_for=3600)], s=s), ([], []))

    def test_no_extra_usage_mail_for_it(self):
        s = dict(billing(), **{"claude:gemini": self.snaps["claude:gemini"]})
        out, unreadable = failover.spend_watch([dict(on_gemini(self.g), model=homes.NEON)], s, {}, self.cfg, NOW)
        self.assertEqual((out, unreadable), ([], {}))


class Spend(unittest.TestCase):
    """failover.spend_watch: the owner's mails while agents bill a login's extra usage."""

    def watch(self, sessions, s, state=None, now=NOW, cfg=CFG):
        return failover.spend_watch(sessions, s, state or {}, cfg, now)[0]

    def run_stretch(self, readings, sessions):
        """spend_watch over (now, snaps) readings, each result's stretch kept as agent-failover keeps it; the mails."""
        state, mails = {}, []
        for now, s in readings:
            for login, stretch, mail in self.watch(sessions, s, state, now):
                state = failover.keep_stretch(state, login, stretch)
                if mail:
                    mails.append(mail)
        return state, mails

    def test_agents_starting_to_bill_get_one_mail_naming_them_the_login_and_the_month(self):
        [(login, st, mail)] = self.watch([on("work", "board-sync"), on("work", "eval-3"), on("personal", "mail")],
                                         billing())
        self.assertEqual((login, st["since"], st["spent"], st["agents"]),
                         ("work", NOW, 0.0, ["board-sync (claude-opus-5-5)", "eval-3 (claude-opus-5-5)"]))
        kind, key, subject, body = mail
        self.assertEqual((kind, subject), ("extra-usage-spend", "2 agents now bill extra usage on Claude login work"))
        self.assertIn("2 agents are running on the Claude login work, whose session window is used up until %s. Its "
                      "extra usage is on" % usage.fmt_reset(NOW + 3600, NOW), body)
        self.assertIn("Extra usage on work this month: $211.41 used, no monthly cap.", body)
        self.assertIn("Agents on it: board-sync (claude-opus-5-5), eval-3 (claude-opus-5-5).", body)
        self.assertIn("To stop one billing, switch it to another model or login: right-click it on the agent board, "
                      "or run agent-switch NAME --model MODEL or --to claude:LOGIN.", body)
        self.assertIn("another mail at each further $100.00 they spend", body)

    def test_no_mail_without_extra_usage_billed(self):
        self.assertEqual(self.watch([on("work", "a")], billing(extra=False)), [])        # extra usage off
        self.assertEqual(self.watch([on("work", "a")], billing(extra=False, used=None)), [])   # and no spend shown
        self.assertEqual(self.watch([on("work", "a")], billing(work=40)), [])           # window not used up
        self.assertEqual(self.watch([on("personal", "a")], billing()), [])                # no agent on work

    def test_a_model_window_bills_only_the_agents_running_that_model(self):
        s = billing(work=50)
        s["claude:work"]["windows"].append(fable(0))
        [(_, st, mail)] = self.watch([on("work", "fab", model="claude-fable-5-1"), on("work", "op")], s)
        self.assertEqual(st["agents"], ["fab (claude-fable-5-1)"])
        self.assertIn("1 agent is running on the Claude login work, whose weekly Fable window is used up", mail[3])

    def test_a_mail_each_further_step_and_one_for_two_steps_at_once(self):
        a = [on("work", "a")]
        state, mails = self.run_stretch([(NOW, billing(used=211.41)), (NOW + 300, billing(used=290)),
                                         (NOW + 600, billing(used=320)), (NOW + 900, billing(used=330)),
                                         (NOW + 1200, billing(used=525))], a)
        self.assertEqual([m[2] for m in mails[1:]],
                         ["$108.59 of extra usage spent on Claude login work since %s" % usage.fmt_reset(NOW, NOW),
                          "$313.59 of extra usage spent on Claude login work since %s" % usage.fmt_reset(NOW, NOW)])
        self.assertIn("Still on it: a (claude-opus-5-5).", mails[1][3])
        self.assertEqual((state["spend"]["work"]["spent"], state["spend"]["work"]["mailed"]), (313.59, 300))
        self.assertEqual(len({m[1] for m in mails}), 3)        # each its own alert key, so none is deduplicated
        self.assertEqual({m[0] for m in mails}, {"extra-usage-spend"})

    def test_the_step_comes_from_config(self):
        cfg = dict(CFG, failover=dict(CFG["failover"], spend_step=50))
        _, _, mail = self.watch([on("work", "a")], billing(used=270),
                                {"spend": {"work": {"since": NOW - 60, "last_used": 211.41, "spent": 0.0, "mailed": 0.0,
                                                     "currency": "USD", "agents": ["a"]}}}, cfg=cfg)[0]
        self.assertIn("$58.59 of extra usage spent", mail[2])
        self.assertIn("each further $50.00", mail[3])

    def test_a_new_month_counts_on_from_zero(self):
        state, _ = self.run_stretch([(NOW, billing(used=880)), (NOW + 300, billing(used=950)),
                                     (NOW + 600, billing(used=12.5))], [on("work", "a")])
        self.assertEqual(state["spend"]["work"]["spent"], 82.5)

    def test_the_stretch_ends_once_with_its_total(self):
        for sessions, end, why in (([], billing(used=353.51), "no agent runs on it any more"),
                                   ([on("work", "a")], billing(work=100, used=353.51), "its used-up window has reset"),
                                   ([on("work", "a")], billing(extra=False, used=353.51), "its extra usage was turned off")):
            state, _ = self.run_stretch([(NOW, billing(used=211.41))], [on("work", "a")])
            [(login, st, mail)] = self.watch(sessions, end, state, NOW + 3600)
            self.assertEqual((login, st), ("work", None))
            self.assertEqual(mail[:3:2], ("extra-usage-end",
                                          "Agents stopped billing extra usage on Claude login work: $142.10 spent"))
            self.assertIn("any more: %s. From %s to %s they spent $142.10 of extra usage." % (
                why, usage.fmt_reset(NOW, NOW), usage.fmt_reset(NOW + 3600, NOW)), mail[3])
            self.assertIn("Extra usage on work this month: $353.51 used, no monthly cap.", mail[3])

    def test_extra_usage_turned_off_ends_it_though_the_reading_then_has_no_spend(self):
        state, _ = self.run_stretch([(NOW, billing(used=211.41)), (NOW + 300, billing(used=260))], [on("work", "a")])
        [(_, st, mail)] = self.watch([on("work", "a")], billing(extra=False, used=None), state, NOW + 600)
        self.assertIsNone(st)
        self.assertEqual(mail[2], "Agents stopped billing extra usage on Claude login work: $48.59 spent")
        self.assertIn("any more: its extra usage was turned off.", mail[3])
        self.assertIn("Extra usage on work this month: $260.00 used, no monthly cap.", mail[3])   # its last reading

    def test_the_billing_agents_leaving_ends_it_while_others_stay(self):
        s = billing(work=50)
        s["claude:work"]["windows"].append(fable(0))
        state, _ = self.run_stretch([(NOW, s)], [on("work", "fab", model="claude-fable-5-1"), on("work", "op")])
        [(_, st, mail)] = self.watch([on("work", "op")], s, state, NOW + 600)
        self.assertIsNone(st)
        self.assertIn("any more: the agents that billed it have left.", mail[3])

    def test_an_unreadable_or_stale_reading_changes_nothing(self):
        state, _ = self.run_stretch([(NOW, billing())], [on("work", "a")])
        stale = billing(work=100)
        stale["claude:work"].update(stale=True, error="claude usage read timed out after 30s")
        no_spend = billing(used=None)
        for s in (stale, no_spend, {}, {"claude:work": {"windows": [], "error": "timed out"}}):
            self.assertEqual(self.watch([], s, state, NOW + 600), [])
            self.assertEqual(self.watch([on("work", "b")], s, {}, NOW + 600), [])
        reasons = [failover.spend_watch([on("work", "b")], s, {}, CFG, NOW)[1]
                   for s in (stale, no_spend, {}, {"claude:work": {"windows": [], "error": "timed out"}})]
        self.assertEqual(reasons, [
            {"work": "its usage read failed (claude usage read timed out after 30s), leaving only an older reading"},
            {"work": "its reading has no extra-usage spend"}, {"work": "its usage was not read"},
            {"work": "its usage read failed (timed out)"}])


class SpendWatch(unittest.TestCase):
    """agent-failover --once with a live agent on work: the usage read (it asks Anthropic) and Gmail stood in, and in
    the test with a rebalance the move (failover.migrate, driven by Move), which would stop a live agent; the rest
    real: the registry and transcript, the usage parse, the decision, the alert and its store, the state file."""

    RESP = {"subscription_type": "team", "rate_limits_available": True, "rate_limits": {
        "five_hour": {"utilization": 100, "resets_at": "2026-06-15T16:30:00+00:00"},
        "seven_day": {"utilization": 57, "resets_at": "2026-06-19T02:59:59+00:00"},
        "extra_usage": {"is_enabled": True, "monthly_limit": None, "used_credits": 21141, "utilization": None,
                        "currency": "USD", "decimal_places": 2, "spend_limit_reached": False}}}

    def setUp(self):
        self.h = homes.Homes(self, logins=("personal", "work"), cfg=dict(CFG, usage={"cache_seconds": 0}))
        (self.h.homes["work"] / ".credentials.json").write_text("{}")
        self.h.claude(SID, "board-sync", login="work", cwd="/work", updated_at=NOW, kind="bg", status="busy")
        t = self.h.homes["work"] / "projects" / "-work" / (SID + ".jsonl")
        t.parent.mkdir(parents=True)
        t.write_text(reply("claude-opus-5-5") + "\n")
        self.state = jsonfile.JsonFile(homes.temp_dir(self) / "failover.json", jsonfile.PLAIN, 1)
        homes.patch(self, failover, "STATE", self.state)
        db = homes.temp_dir(self) / "mailroom.sqlite"
        homes.patch(self, outbound, "Store", lambda: store.Store(db))
        self.sent = fakes.sending_gmail(self, outbound, alerts)
        self.work = json.loads(json.dumps(self.RESP))
        self.personal = None

    def fetch(self, acct, timeout, cwd=None):
        if acct.get("config_dir") == str(self.h.homes["work"]):
            return usage.parse_claude(self.work)
        if self.personal:
            return usage.parse_claude(self.personal)
        raise usage.UsageError("only work is read here")

    def run_once(self):
        af = load_script("agent-failover")
        with mock.patch.object(usage, "fetch_claude", self.fetch), \
                mock.patch.object(usage, "fetch_codex", side_effect=usage.UsageError("no codex here")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(af.main(["--once"]), 0, out.getvalue())
        return out.getvalue()

    def mails(self):
        return [email.message_from_bytes(raw, policy=email.policy.default) for raw, _ in self.sent]

    def test_start_step_and_end_are_mailed_once_each(self):
        out = self.run_once()
        self.assertIn("mailed Alex: 1 agent now bills extra usage on Claude login work", out)
        self.run_once()                                                          # nothing new to say
        self.work["rate_limits"]["extra_usage"]["used_credits"] = 33141        # $120 later
        self.run_once()
        self.work["rate_limits"]["five_hour"]["utilization"] = 20               # the window reset
        self.run_once()
        self.run_once()
        subjects = [m["Subject"].strip() for m in self.mails()]   # the header folder may start a long one on a new line
        self.assertEqual(subjects[0], "[mailroom] 1 agent now bills extra usage on Claude login work")
        self.assertRegex(subjects[1], r"^\[mailroom\] \$120\.00 of extra usage spent on Claude login work since ")
        self.assertEqual(subjects[2], "[mailroom] Agents stopped billing extra usage on Claude login work: $120.00 spent")
        self.assertEqual(len(subjects), 3)
        first = self.mails()[0]
        self.assertEqual(first["To"], "owner@example.com")
        self.assertIn("Extra usage on work this month: $211.41 used, no monthly cap.",
                      first.get_body(("plain",)).get_content())
        self.assertEqual(self.state.read()["spend"], {})                       # the stretch is over

    def test_a_failed_mail_is_tried_again_at_the_next_check(self):
        with mock.patch.object(alerts.gmail, "send_raw", side_effect=OSError("network down")):
            out = self.run_once()
        self.assertIn("error: extra-usage mail about work not sent, tried again at the next check: OSError: network "
                      "down", out)
        self.assertNotIn("spend", self.state.read())
        self.run_once()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("work", self.state.read()["spend"])

    def test_an_agent_moved_off_the_login_in_the_same_pass_is_not_reported_there(self):
        """The rebalance moves an idle agent off a login that bills extra usage; the start mail names only the busy
        agent left there."""
        self.h.claude("idle-agent-0000", "idle-one", login="work", cwd="/work", updated_at=NOW - 3600, kind="bg",
                      status="idle")
        self.personal = json.loads(json.dumps(self.RESP))
        self.personal["rate_limits"]["five_hour"]["utilization"] = 10
        self.personal["rate_limits"]["extra_usage"]["is_enabled"] = False
        with mock.patch.object(failover, "migrate", return_value=(True, "moved")) as migrate:   # it would stop an agent
            out = self.run_once()
        self.assertEqual([(c.args[0]["id"], c.args[1]) for c in migrate.call_args_list],
                         [("idle-agent-0000", "personal")])
        self.assertIn("mailed Alex: 1 agent now bills extra usage on Claude login work", out)
        self.assertIn("Agents on it: board-sync (claude-opus-5-5).", self.mails()[0].get_body(("plain",)).get_content())
        self.assertEqual(self.state.read()["spend"]["work"]["ids"], [SID])

    def test_a_dry_run_says_what_it_would_mail_and_sends_nothing(self):
        af = load_script("agent-failover")
        with mock.patch.object(usage, "fetch_claude", self.fetch), \
                mock.patch.object(usage, "fetch_codex", side_effect=usage.UsageError("no codex here")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(af.main(["--once", "--dry-run"]), 0)
        self.assertIn("would mail Alex: 1 agent now bills extra usage on Claude login work", out.getvalue())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state.read(), {})

    def test_a_login_that_cannot_be_checked_is_logged_once(self):
        def fail(acct, timeout, cwd=None):
            raise usage.UsageError("claude usage read timed out after 30s")
        self.fetch = fail
        outs = [self.run_once(), self.run_once()]
        self.assertEqual(sum(o.count("extra-usage spend on work not checked: its usage read failed") for o in outs), 1)
        self.assertEqual(self.sent, [])

    def test_off_in_config(self):
        self.h.config.write_text(json.dumps(dict(json.loads(self.h.config.read_text()),
                                                 failover={"spend_mail": False})))
        self.run_once()
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
