"""Where a plan window's allowance went (agentboard/allowance.py): replies read from Claude and Codex transcripts,
the moves between logins, each reply counted once on its login, the fitted factor, and the split by group with
Other, and the usage readings the board keeps for the fit. Every file goes to a temporary folder."""
import datetime
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fixtures import iso, parse_file, repo_with_worktree, write_jsonl  # noqa: E402
from agentboard import actions, allowance, sources  # noqa: E402

T0 = 1793000000.0
WEEK = 7 * 86400
A, B, C, D = (str(uuid.UUID(int=n)) for n in (1, 2, 3, 4))      # session ids
CFG = dict(allowance.DEFAULTS, prices={"claude-opus-5-5": [4, 20, 0.2], "claude-fable-5-1": [10, 50, 0.25],
                                       "gpt-6.1-sol": [1, 8, 0.1]}, min_fit_points=3)
PALETTE = {"groups": {"Work": "#0E8A84"}, "reserve": ["#6A5ACD"], "ungrouped": "#8C877F",      # colours.json's form
           "shades": {"lightness": 0.1, "chroma": 0.92}}


def reply(sid, mid, ts, out=1000, model="claude-opus-5-5", cw=0, cr=0, cwd="/w/repo", breakdown=True):
    usage = {"input_tokens": 0, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr, "output_tokens": out}
    if breakdown:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": cw}
    return {"type": "assistant", "sessionId": sid, "cwd": cwd, "timestamp": iso(ts),
            "message": {"id": mid, "model": model, "usage": usage}}


def codex_meta(tid, ts, parent=None, cwd="/w/web"):
    payload = {"id": tid, "cwd": cwd, "timestamp": iso(ts), "source": "cli" if parent is None else
               {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}}}
    return {"type": "session_meta", "payload": payload}


def tokens(ts, inp, cached, out):
    return {"timestamp": iso(ts), "type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}}}}


def turn(model):
    return {"type": "turn_context", "payload": {"model": model}}


def status_event(sid, event, text, ts, account="", cli="claude"):
    return {"cli": cli, "session_id": sid, "event": event, "text": text, "ts": ts, "account": account}


class Parsing(unittest.TestCase):
    def test_a_reply_counts_once_with_its_complete_output(self):
        d = Path(tempfile.mkdtemp())
        p = write_jsonl(d / "p" / (A + ".jsonl"), [
            {"type": "user", "sessionId": A, "timestamp": iso(T0), "message": {"content": "go"}},
            reply(A, "m1", T0 + 1, out=5, cw=100, cr=2000),     # a reply's first block: its output not counted yet
            reply(A, "m1", T0 + 2, out=50, cw=100, cr=2000),    # its last block
            reply(A, "m2", T0 + 60, out=7)])
        st = parse_file(allowance.parse_claude, p)
        self.assertEqual((st["sid"], st["cwd"]), (A, "/w/repo"))
        self.assertEqual(st["replies"]["m1"], [T0 + 1, "claude-opus-5-5", 0, 0, 100, 2000, 50])
        self.assertEqual(len(st["replies"]), 2)
        # $4 in, $20 out, $0.20 cache reads per million; an hour-long cache write costs twice the input price
        self.assertAlmostEqual(allowance.price(st["replies"]["m1"], CFG["prices"]),
                               (100 * 4 * 2 + 2000 * 0.2 + 50 * 20) / 1e6)
        self.assertIsNone(allowance.price([T0, "claude-unknown", 0, 0, 0, 0, 1], CFG["prices"]))

    def test_the_default_prices_match_fable_5_2s_published_rates(self):
        # Anthropic's pricing page: $8 input, $10 five-minute and $16 one-hour cache writes, $0.20 cache reads and $40
        # output per million tokens
        row = [T0, "claude-fable-5-2"] + [10 ** 6] * 5
        self.assertAlmostEqual(allowance.price(row, allowance.DEFAULTS["prices"]), 8 + 10 + 16 + 0.2 + 40)

    def test_a_subagent_transcript_counts_on_its_parent_and_old_writes_last_an_hour(self):
        d = Path(tempfile.mkdtemp())
        p = write_jsonl(d / "p" / A / "subagents" / "agent-x.jsonl",
                        [reply(A, "s1", T0, out=3, cw=40, breakdown=False)])
        st = parse_file(allowance.parse_claude, p)
        self.assertEqual(st["sid"], A)
        self.assertEqual(st["replies"]["s1"][3:5], [0, 40])

    def test_codex_counts_each_threads_own_growth_on_its_turns_model(self):
        d = Path(tempfile.mkdtemp())
        p = write_jsonl(d / "rollout-y.jsonl", [
            codex_meta(B, T0 + 100, parent=A),
            tokens(T0 + 10, 1000, 800, 50),         # replayed from the thread it was forked from: sets the start
            turn("gpt-6.1-sol"),
            tokens(T0 + 200, 1500, 1100, 80),
            tokens(T0 + 210, 1500, 1100, 80),       # the same totals again: nothing new
            tokens(T0 + 300, 2500, 2000, 100)])
        st = parse_file(allowance.parse_codex, p)
        self.assertEqual((st["sid"], st["parent"], st["cwd"]), (B, A, "/w/web"))
        self.assertEqual(list(st["replies"].values()), [
            [T0 + 200, "gpt-6.1-sol", 200, 0, 0, 300, 30],          # uncached input, cached input as reads, output
            [T0 + 300, "gpt-6.1-sol", 100, 0, 0, 900, 20]])

    def test_codex_totals_that_fall_start_again_from_there(self):
        d = Path(tempfile.mkdtemp())
        p = write_jsonl(d / "rollout-r.jsonl", [codex_meta(A, T0), turn("gpt-6.1-sol"), tokens(T0 + 10, 1000, 0, 100),
                                                tokens(T0 + 20, 400, 0, 40),          # a fall: no turn, a new start
                                                tokens(T0 + 30, 600, 100, 50)])
        self.assertEqual(list(parse_file(allowance.parse_codex, p)["replies"].values()),
                         [[T0 + 10, "gpt-6.1-sol", 1000, 0, 0, 0, 100], [T0 + 30, "gpt-6.1-sol", 100, 0, 0, 100, 10]])

    def test_a_malformed_price_list_is_refused(self):
        saved = allowance.CONFIG
        allowance.CONFIG = Path(tempfile.mkdtemp()) / "allowance.json"
        try:
            for prices in ({"claude-opus-5-5": [4, 20]}, {"claude-opus-5-5": [4, "x", 0.2]}, ["claude-opus-5-5"]):
                allowance.CONFIG.write_text(json.dumps({"prices": prices}))
                with self.assertRaisesRegex(ValueError, "prices must map each model"):
                    allowance.config()
            allowance.CONFIG.write_text(json.dumps({"fit_days": 14}))
            self.assertEqual((allowance.config()["fit_days"], allowance.config()["prices"]),
                             (14, allowance.DEFAULTS["prices"]))
        finally:
            allowance.CONFIG = saved


class Moves(unittest.TestCase):
    def test_moves_from_failover_and_switch_events(self):
        log = write_jsonl(Path(tempfile.mkdtemp()) / "events.jsonl", [
            status_event(A, "failover", "default -> work (limit: five-hour limit)", T0 + 100, "work"),
            status_event(A, "failover", "work -> default (by hand): FAILED no room", T0 + 150, "default"),
            status_event(A, "switch", "Claude Code (work login), claude-opus-5-5 at effort high -> Claude Code "
                                      "(default login), claude-opus-5-5 at effort high: moved x to default", T0 + 200),
            status_event(B, "switch", "Claude Code (default login), opus -> Codex, gpt-6.1-sol: moved", T0, cli="codex"),
            status_event(C, "switch", "Codex, gpt-6.1-sol -> Claude Code (default login), opus: handed over to Claude "
                                      "Code (default login), opus as session x", T0),
            status_event(D, "failover", "garbled", T0, "work"),
            {"event": "idle", "session_id": A, "ts": T0}])
        moved, unread = allowance.moves(log)
        self.assertEqual(moved, {A: [(T0 + 100, "default", "work"), (T0 + 200, "work", "default")]})
        self.assertEqual(unread, 1)
        self.assertEqual([allowance.login_at(moved[A], t, "x") for t in (T0, T0 + 100, T0 + 250)],
                         ["default", "work", "default"])
        self.assertEqual(allowance.login_at(None, T0, "work"), "work")
        self.assertEqual(allowance.moves(Path(tempfile.mkdtemp()) / "none.jsonl"), ({}, 0))


class Ledger(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.default, self.work, self.codex = root / "claude", root / "claude-work", root / "codex"
        self.codex.mkdir()
        self.log = root / "events.jsonl"
        self.log.write_text("")
        self.cache = root / "cache.json"

    def ledger(self, since=0):
        return allowance.ledger(CFG, since, [("default", self.default), ("work", self.work)], self.codex,
                                self.log, self.cache)

    def test_a_copied_conversation_counts_once_on_the_login_it_was_on(self):
        lines = [reply(A, "m1", T0 + 50), reply(A, "m2", T0 + 150)]
        write_jsonl(self.default / "projects" / "p" / (A + ".jsonl"), lines)          # copied back when it returned
        write_jsonl(self.work / "projects" / "p" / (A + ".jsonl"), lines[:1] + lines[1:] + [reply(A, "m3", T0 + 250)])
        write_jsonl(self.log, [status_event(A, "failover", "default -> work (limit)", T0 + 100, "work"),
                               status_event(A, "failover", "work -> default (switch back)", T0 + 200, "default")])
        rows, cwd, notes = self.ledger()
        self.assertEqual(sorted((r[2], r[0]) for r in rows),
                         [(T0 + 50, "claude:default"), (T0 + 150, "claude:work"), (T0 + 250, "claude:default")])
        self.assertEqual((cwd, notes), ({A: "/w/repo"}, []))

    def test_a_forks_copied_history_counts_once(self):
        write_jsonl(self.default / "projects" / "p" / (A + ".jsonl"), [reply(A, "m1", T0), reply(A, "m2", T0 + 60)])
        # a copy made by a tool keeps the session ids its lines name; Claude Code's own fork names itself
        write_jsonl(self.default / "projects" / "p" / (B + ".jsonl"), [reply(A, "m1", T0), reply(A, "m2", T0 + 60)])
        write_jsonl(self.default / "projects" / "p" / (C + ".jsonl"),
                    [reply(C, "m1", T0), reply(C, "m2", T0 + 60), reply(C, "m3", T0 + 120)])
        rows, _, _ = self.ledger()
        self.assertEqual(sorted(r[2] for r in rows), [T0, T0 + 60, T0 + 120])
        self.assertEqual({r[1] for r in rows if r[2] == T0 + 120}, {C})

    def test_codex_sub_threads_count_on_the_thread_that_started_them(self):
        day = self.codex / "sessions" / "2026" / "10" / "26"
        # the files are read in name order, so the sub-threads come first: the root keeps its own folder all the same
        write_jsonl(day / "rollout-z.jsonl", [codex_meta(A, T0), turn("gpt-6.1-sol"), tokens(T0 + 10, 100, 0, 10)])
        write_jsonl(day / "rollout-a.jsonl", [codex_meta(B, T0 + 20, parent=A, cwd="/w/sub"), turn("gpt-6.1-sol"),
                                              tokens(T0 + 30, 100, 0, 10)])
        write_jsonl(day / "rollout-b.jsonl", [codex_meta(C, T0 + 40, parent=B), turn("gpt-6.1-sol"),
                                              tokens(T0 + 50, 100, 0, 10)])
        rows, cwd, _ = self.ledger()
        self.assertEqual(sorted((r[0], r[1], r[2]) for r in rows),
                         [("codex", A, T0 + 10), ("codex", A, T0 + 30), ("codex", A, T0 + 50)])
        self.assertEqual(cwd[A], "/w/web")

    def test_a_codex_review_thread_counts_on_the_thread_that_asked_for_it(self):
        # a review thread names its kind as text, {"subagent": "review"}, and its parent at the top of its meta
        day = self.codex / "sessions" / "2026" / "11" / "25"
        write_jsonl(day / "rollout-a.jsonl", [codex_meta(A, T0), turn("gpt-6.1-sol"), tokens(T0 + 10, 100, 0, 10)])
        review = codex_meta(B, T0 + 20)
        review["payload"].update(source={"subagent": "review"}, parent_thread_id=A)
        write_jsonl(day / "rollout-b.jsonl", [review, turn("gpt-6.1-sol"), tokens(T0 + 30, 100, 0, 10)])
        rows, _, notes = self.ledger()
        self.assertEqual(sorted((r[0], r[1], r[2]) for r in rows), [("codex", A, T0 + 10), ("codex", A, T0 + 30)])
        self.assertEqual(notes, [])

    def test_lines_and_threads_that_cannot_be_read_are_named(self):
        p = write_jsonl(self.default / "projects" / "p" / (A + ".jsonl"), [reply(A, "m1", T0)])
        with open(p, "a") as fh:
            fh.write('{"type": "assistant", "message": {"usage": {"output_tokens": 9}\n')     # damaged
        day = self.codex / "sessions" / "2026" / "10" / "26"
        day.mkdir(parents=True)
        (day / "rollout-q.jsonl").write_text('{"payload": \n' + json.dumps(tokens(T0, 1, 0, 1)) + "\n")
        rows, _, notes = self.ledger()
        self.assertEqual([r[2] for r in rows], [T0])
        self.assertEqual(notes, ["1 transcript line that might have held a reply could not be read and is left out",
                                 "1 Codex thread left out: the first line does not name the thread"])

    def test_a_model_without_a_price_is_named_and_old_replies_left_out(self):
        write_jsonl(self.default / "projects" / "p" / (A + ".jsonl"),
                    [reply(A, "m0", T0 - 10), reply(A, "m1", T0, model="claude-mystery", out=77), reply(A, "m2", T0 + 5)])
        rows, _, notes = self.ledger(since=T0)
        self.assertEqual([r[2] for r in rows], [T0 + 5])
        self.assertEqual(len(notes), 1)
        self.assertIn("claude-mystery (77 tokens)", notes[0])


def sample(ts, used, resets, target="claude:default", key="weekly"):
    return {"ts": ts, "pools": {target: {key: {"used": used, "resets": resets}}}}


class Fit(unittest.TestCase):
    def test_the_factor_is_recovered_from_the_readings(self):
        # $10 an hour of agent use, and the window rises 0.3 points an hour: 3% of the window per $100
        rows = [("claude:default", A, T0 + h * 3600 + 60, "claude-opus-5-5", 10.0) for h in range(48)]
        samples = [sample(T0 + i * 1800, 0.15 * i, T0 + WEEK) for i in range(97)]
        k = allowance.fit(rows, samples, "claude:default", "weekly", CFG, T0 + 48 * 3600)
        self.assertAlmostEqual(k["pct_per_dollar"], 0.03)
        self.assertEqual(k["points"], 8)
        self.assertAlmostEqual(k["rms"], 0)
        self.assertIsNone(allowance.fit(rows, samples[:30], "claude:default", "weekly", CFG, T0 + 48 * 3600))

    def test_a_new_window_starts_new_stretches(self):
        rows = [("claude:default", A, T0 + h * 3600 + 60, "claude-opus-5-5", 10.0) for h in range(48)]
        samples = [sample(T0 + i * 1800, 0.15 * i if i < 48 else 0.15 * (i - 48), T0 + (WEEK if i < 48 else 2 * WEEK))
                   for i in range(97)]
        k = allowance.fit(rows, samples, "claude:default", "weekly", CFG, T0 + 48 * 3600)
        self.assertAlmostEqual(k["pct_per_dollar"], 0.03)         # the fall at the reset is not a stretch
        self.assertEqual(k["points"], 7)


class Split(unittest.TestCase):
    LAYOUT = [{"name": "Work", "subgroups": ["Proj"]}, {"name": "Admin", "subgroups": []}]
    HISTORY = {A: {"group": "Work", "subgroup": "Proj"}, B: {"group": "Admin", "subgroup": None}}

    def accounts(self, used=13.0):
        return [{"target": "claude:default", "windows": [
            {"kind": "weekly", "used_pct": used, "resets_at": T0 + WEEK},
            {"kind": "model_weekly", "model": "fable", "used_pct": 2.0, "resets_at": T0 + WEEK}]}]

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        repo_with_worktree(self.d, "proj", "todo-1")         # A works in the repo, C (never placed) in its worktree

    def split(self, rows, samples, key="weekly", accounts=None):
        cwd = {A: str(self.d / "proj"), B: "/w/admin", C: str(self.d / "proj-todo-1"), D: "/w/orchestrator"}
        return allowance.split("claude:default", key, accounts or self.accounts(), rows, cwd, samples, CFG,
                               T0 + 48 * 3600, self.HISTORY, self.LAYOUT, {A: "todo-1", B: "board"}, "/w/orchestrator",
                               PALETTE)

    def rows(self):
        # the fit's stretches: $10 an hour across all agents, at 3% of the window per $100
        out = [("claude:default", A, T0 + h * 3600 + 60, "claude-opus-5-5", 5.0) for h in range(48)]
        out += [("claude:default", B, T0 + h * 3600 + 120, "claude-opus-5-5", 3.0) for h in range(48)]
        out += [("claude:default", C, T0 + h * 3600 + 180, "claude-opus-5-5", 1.0) for h in range(48)]
        out += [("claude:default", D, T0 + h * 3600 + 240, "claude-opus-5-5", 1.0) for h in range(48)]
        out += [("claude:default", A, T0 - 600, "claude-opus-5-5", 99.0)]               # before the reset
        out += [("codex", A, T0 + 60, "gpt-6.1-sol", 50.0)]                              # another pool
        return out

    def samples(self):
        return [sample(T0 + i * 1800, 0.15 * i + (0.5 if i >= 40 else 0), T0 + WEEK) for i in range(97)]

    def test_groups_sub_groups_agents_and_other(self):
        s = self.split(self.rows(), self.samples())
        self.assertAlmostEqual(s["scale"]["pct_per_dollar"], 0.03, delta=0.002)     # other use lifts it a little
        self.assertEqual((s["since"], s["used"], s["dollars"]), (T0, 13.0, 480.0))
        k = s["scale"]["pct_per_dollar"]
        self.assertAlmostEqual(s["agents_pct"], round(480 * k, 2))
        self.assertAlmostEqual(s["other_pct"], round(13.0 - 480 * k, 2))
        work, admin, unsorted = s["groups"]
        self.assertEqual((work["name"], work["dollars"]), ("Work", 288.0))
        self.assertEqual([(x["name"], x["dollars"]) for x in work["subgroups"]], [("Proj", 240.0)])
        self.assertEqual([(x["name"], x["inferred"]) for x in work["subgroups"][0]["agents"]], [("todo-1", False)])
        self.assertEqual([(x["name"], x["inferred"]) for x in work["agents"]], [("proj-todo-1", True)])  # C: by repo
        self.assertEqual([x["name"] for x in admin["agents"]], ["board"])
        self.assertEqual((unsorted["name"], [x["name"] for x in unsorted["agents"]]),
                         (allowance.timeuse.UNSORTED, [allowance.ORCHESTRATOR]))
        self.assertEqual(s["colours"], {"groups": [
            {"name": "Work", "colour": "#0E8A84", "shades": [{"name": "Proj", "colour": "#006A65"}]},  # the darker end
            {"name": "Admin", "colour": "#6A5ACD", "shades": []}],                    # Admin: the reserve's first
            "ungrouped": "#8C877F"})

    def test_the_chart_stacks_the_groups_and_other_on_top(self):
        s = self.split(self.rows(), self.samples())
        c = s["chart"]
        self.assertEqual((c["start"], c["step"], c["unit"]), (T0, 900, "%"))       # 48 hours in 192 steps
        self.assertEqual([g["name"] for g in c["groups"]], ["Work", "Admin", allowance.timeuse.UNSORTED])
        k = s["scale"]["pct_per_dollar"]
        work = c["groups"][0]["values"]
        self.assertEqual(len(work), 192)
        self.assertAlmostEqual(work[-1], 288 * k, places=2)
        self.assertEqual(work, sorted(work))                                         # cumulative
        self.assertTrue(all(o >= 0 for o in c["other"]))
        self.assertAlmostEqual(c["other"][-1], max(0, 0.15 * 96 + 0.5 - 480 * k), places=2)

    def test_a_models_own_window_counts_only_that_model(self):
        rows = self.rows() + [("claude:default", B, T0 + h * 3600 + 300, "claude-fable-5-1", 2.0) for h in range(48)]
        samples = [dict(s, pools={"claude:default": dict(s["pools"]["claude:default"],
                                                          **{"model:fable": {"used": 0.03 * i, "resets": T0 + WEEK}})})
                   for i, s in enumerate(self.samples())]
        s = self.split(rows, samples, key="model:fable")
        self.assertEqual((s["dollars"], s["used"]), (96.0, 2.0))
        self.assertEqual([g["name"] for g in s["groups"]], ["Admin"])
        self.assertAlmostEqual(s["scale"]["pct_per_dollar"], 0.03)

    def test_without_a_factor_the_shares_are_in_dollars(self):
        s = self.split(self.rows(), [])
        self.assertIsNone(s["scale"])
        self.assertEqual((s["agents_pct"], s["other_pct"], s["chart"]["unit"], s["chart"]["other"]),
                         (None, None, "$", None))
        self.assertIsNone(s["groups"][0]["pct"])
        self.assertEqual(s["groups"][0]["dollars"], 288.0)

    def test_a_pool_or_window_the_reading_lacks_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no session window"):
            self.split(self.rows(), [], key="session")
        with self.assertRaisesRegex(ValueError, "no usage reading for codex"):
            allowance.window_of(self.accounts(), "codex", "weekly")


class Samples(unittest.TestCase):
    """The readings the fit reads: the board keeps one per half hour from its usage refresh (allowance.sample)."""

    def reading(self, used=13.0, **kw):
        return [dict({"target": "claude:default", "windows": [
            {"kind": "weekly", "used_pct": used, "resets_at": T0 + WEEK},
            {"kind": "session", "remaining_pct": 70.0, "resets_at": T0 + 3600}]}, **kw),
            {"target": "codex", "windows": []}]

    def test_one_line_per_half_hour_in_the_shape_the_fit_reads(self):
        path = Path(tempfile.mkdtemp()) / "usage-samples.jsonl"
        rec = allowance.sample(self.reading(), T0, path)
        self.assertEqual(rec, {"ts": T0, "active": False, "busy": {}, "pools": {"claude:default": {
            "weekly": {"used": 13.0, "resets": T0 + WEEK}, "session": {"used": 30.0, "resets": T0 + 3600}}}})
        self.assertIsNone(allowance.sample(self.reading(14.0), T0 + 600, path))      # too soon: kept out
        self.assertIsNotNone(allowance.sample(self.reading(15.0), T0 + allowance.SAMPLE_EVERY, path))
        rows = allowance.sources.read_jsonl(path)
        self.assertEqual([r["pools"]["claude:default"]["weekly"]["used"] for r in rows], [13.0, 15.0])
        self.assertEqual(allowance.fit([], rows, "claude:default", "weekly", CFG, T0 + 3600), None)   # reads them
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_a_cached_reading_or_one_without_windows_is_not_kept(self):
        path = Path(tempfile.mkdtemp()) / "usage-samples.jsonl"
        self.assertIsNone(allowance.sample(self.reading(stale=True), T0, path))
        self.assertIsNone(allowance.sample([], T0, path))
        self.assertFalse(path.exists())

    def test_the_file_is_trimmed_to_its_newest_lines(self):
        path = Path(tempfile.mkdtemp()) / "usage-samples.jsonl"
        write_jsonl(path, [{"ts": T0 + i * allowance.SAMPLE_EVERY, "pools": {}} for i in range(allowance.SAMPLES_MAX)])
        allowance.sample(self.reading(), T0 + allowance.SAMPLES_MAX * allowance.SAMPLE_EVERY, path)
        rows = allowance.sources.read_jsonl(path)
        self.assertEqual(len(rows), allowance.SAMPLES_TRIM)
        self.assertEqual(rows[-1]["pools"]["claude:default"]["weekly"]["used"], 13.0)

    def test_the_usage_refresh_records_a_reading(self):
        saved = allowance.SAMPLES
        allowance.SAMPLES = Path(tempfile.mkdtemp()) / "usage-samples.jsonl"
        try:
            board = load_agent_board()
            board.annotate_usage(self.reading())
            self.assertEqual(list(allowance.sources.read_jsonl(allowance.SAMPLES)[0]["pools"]), ["claude:default"])
        finally:
            allowance.SAMPLES = saved


def load_agent_board():
    """bin/agent-board as a module, for its annotate_usage."""
    import importlib.machinery
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "bin" / "agent-board"
    loader = importlib.machinery.SourceFileLoader("agent_board_cmd", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def gemini_reply(sid, mid, ts, inp, cached, out, model="google/gemini-4-neon"):
    """A reply as Claude Code writes one from OpenRouter: no cache writes, and their breakdown at zero."""
    usage = {"input_tokens": inp, "cache_creation_input_tokens": 0, "cache_read_input_tokens": cached,
             "output_tokens": out, "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
    return {"type": "assistant", "sessionId": sid, "cwd": "/w/repo", "timestamp": iso(ts),
            "message": {"id": mid, "model": model, "usage": usage}}


def utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc).timestamp()


GEMINI = {"config_dir": None, "label": "Gemini Neon (OpenRouter)", "settings": "~/openrouter-claude-settings.json",
          "model": "google/gemini-4-neon", "manual": True, "weekly_budget_usd": 500}


class Spend(unittest.TestCase):
    """A per-token login's dollars this week, against its budget (allowance.weekly_spend)."""

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.home, self.default = root / "claude-gemini", root / "claude"
        self.log, self.cache = root / "events.jsonl", root / "spend-cache.json"
        self.log.write_text("")
        self.accounts = {"default": {"config_dir": str(self.default)},
                         "gemini": dict(GEMINI, config_dir=str(self.home))}
        saved = allowance.CONFIG
        allowance.CONFIG = root / "allowance.json"           # the week in UTC, whatever this machine's zone
        allowance.CONFIG.write_text(json.dumps({"week_zone": "UTC"}))
        self.addCleanup(setattr, allowance, "CONFIG", saved)

    def spend(self, now):
        return allowance.weekly_spend(now, self.accounts, self.log, self.cache)

    def test_gemini_prices_charge_nothing_for_cache_writes(self):
        prices = allowance.DEFAULTS["prices"]
        row = [T0, "google/gemini-4-neon", 10 ** 6, 10 ** 6, 10 ** 6, 10 ** 6, 10 ** 6]
        self.assertAlmostEqual(allowance.price(row, prices), 3 + 0 + 0 + 0.15 + 15)
        self.assertAlmostEqual(allowance.price([T0, "google/gemini-4-hydrogen"] + [10 ** 6] * 5, prices),
                               1.5 + 0.075 + 7.5)
        saved = allowance.CONFIG
        allowance.CONFIG = Path(tempfile.mkdtemp()) / "allowance.json"
        try:
            allowance.CONFIG.write_text(json.dumps({"prices": {"x/y": [1, 2, 0.1, 0.5]}}))
            self.assertEqual(allowance.config()["prices"], {"x/y": [1, 2, 0.1, 0.5]})
            allowance.CONFIG.write_text(json.dumps({"prices": {"x/y": [1, 2, 0.1, 0.5, 9]}}))
            with self.assertRaisesRegex(ValueError, "prices must map each model"):
                allowance.config()
        finally:
            allowance.CONFIG = saved

    def test_a_sessions_spend_is_priced_as_openrouter_charges(self):
        # one per-token session's replies, (input, cache read, output), and its sub-agent's, at the model's prices
        # ([3, 15, 0.15] dollars per million): 34,000 input, 40,000 cache read and 1,300 output tokens, $0.1275.
        # Claude Code writes each reply's usage on every line of its content, so ids repeat
        t = utc(2026, 11, 27, 10, 52)
        main = [(10000, 0, 300), (2000, 9000, 100), (2000, 10000, 200), (2000, 10000, 200), (2000, 11000, 200)]
        lines = []
        for i, (inp, cached, out) in enumerate(main):
            lines += [gemini_reply(A, "msg_%d" % i, t + 10 * i, inp, cached, out)] * (2 if i % 2 else 3)
        write_jsonl(self.home / "projects" / "-w-repo" / (A + ".jsonl"), lines)
        write_jsonl(self.home / "projects" / "-w-repo" / A / "subagents" / "agent-a1.jsonl",
                    [gemini_reply(A, "msg_s1", t + 3, 8000, 0, 200)] * 2
                    + [gemini_reply(A, "msg_s2", t + 6, 8000, 0, 100)])
        got = self.spend(t + 3600)["gemini"]
        self.assertAlmostEqual(got["dollars"], 0.1275, places=10)
        self.assertEqual((got["budget"], got["since"], got["notes"]), (500.0, utc(2026, 11, 23), []))
        self.assertAlmostEqual(self.spend(t + 3600)["gemini"]["dollars"], 0.1275, places=10)      # from its cache
        self.assertEqual(allowance.weekly_spend(t, {"default": {}}, self.log, self.cache), {})     # no budget, no spend

    def test_the_week_starts_on_monday_at_midnight_in_its_zone_across_a_clock_change(self):
        # Central European clocks went forward on Sunday 29 March 2026 and back on Sunday 25 October 2026
        paris = "Europe/Paris"
        self.assertEqual(allowance.week_start(utc(2026, 3, 29, 21, 30), paris), utc(2026, 3, 22, 23))   # Monday 00:00 CET
        self.assertEqual(allowance.week_start(utc(2026, 3, 29, 22, 30), paris), utc(2026, 3, 29, 22))   # Monday 00:00 CEST
        self.assertEqual(utc(2026, 3, 29, 22) - utc(2026, 3, 22, 23), WEEK - 3600)               # an hour short
        self.assertEqual(utc(2026, 10, 25, 23) - utc(2026, 10, 18, 22), WEEK + 3600)             # an hour long
        self.assertEqual(allowance.week_start(utc(2026, 10, 25, 22, 30), paris), utc(2026, 10, 18, 22))  # Sunday 23:30
        self.assertEqual(allowance.week_start(utc(2026, 10, 25, 23, 30), paris), utc(2026, 10, 25, 23))  # Monday 00:30
        allowance.CONFIG.write_text(json.dumps({"week_zone": paris}))
        write_jsonl(self.home / "projects" / "p" / (A + ".jsonl"), [
            gemini_reply(A, "m1", utc(2026, 3, 29, 21, 30), 10 ** 6, 0, 0),      # Sunday 23:30 CEST: last week's
            gemini_reply(A, "m2", utc(2026, 3, 29, 22, 30), 0, 0, 10 ** 6)])     # Monday 00:30 CEST: this week's
        got = self.spend(utc(2026, 3, 30, 12))["gemini"]
        self.assertEqual((got["dollars"], got["since"]), (15.0, utc(2026, 3, 29, 22)))

    def test_with_no_week_zone_the_week_is_this_machine_s_local_one(self):
        saved = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        try:
            self.assertEqual(allowance.week_start(utc(2026, 11, 24), None), utc(2026, 11, 23, 5))    # 00:00 EST
            self.assertIsNone(allowance.DEFAULTS["week_zone"])
        finally:
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()

    def test_a_budget_or_model_that_is_not_one_is_refused_and_the_week_zone_is_a_setting(self):
        for bad in ("500", 0, -5, None, True):
            with self.assertRaisesRegex(ValueError, r"usage.claude_accounts.gemini.weekly_budget_usd is .*, not a "
                                                    r"positive number of dollars"):
                allowance.budgets({"gemini": dict(GEMINI, weekly_budget_usd=bad)})
        for bad in (7, "", ["google/gemini-4-neon"]):
            with self.assertRaisesRegex(ValueError, "usage.claude_accounts.gemini.model is .*, not a model's name"):
                allowance.budgets({"gemini": dict(GEMINI, model=bad)})
        self.assertEqual([b[0] for b in allowance.budgets(self.accounts)], ["gemini"])     # default: no budget key
        allowance.CONFIG.write_text(json.dumps({"week_zone": "America/New_York"}))
        self.assertEqual(self.spend(utc(2026, 11, 24))["gemini"]["since"], utc(2026, 11, 23, 5))   # 00:00 EST
        allowance.CONFIG.write_text(json.dumps({"week_zone": "Mars/Olympus"}))
        with self.assertRaisesRegex(ValueError, "no time zone 'Mars/Olympus' \\(week_zone\\)"):
            allowance.config()

    def test_only_what_ran_on_the_login_counts_and_the_rest_is_named(self):
        moved = utc(2026, 11, 24, 12)
        # switched from the default login: its copied history ran there, on Opus; then Gemini, and a sub-agent's Hydrogen
        write_jsonl(self.home / "projects" / "p" / (A + ".jsonl"), [
            gemini_reply(A, "m1", moved - 60, 10 ** 6, 0, 0, model="claude-opus-5-5"),
            gemini_reply(A, "m2", moved + 60, 10 ** 6, 0, 0),
            gemini_reply(A, "m3", moved + 120, 10 ** 6, 0, 0, model="google/gemini-4-hydrogen"),
            gemini_reply(A, "m4", moved + 180, 0, 0, 1234, model="google/gemini-9")])
        # never moved, yet a Claude model in the Gemini login's folder: credited to it by mistake
        write_jsonl(self.home / "projects" / "p" / (B + ".jsonl"), [
            gemini_reply(B, "b1", moved, 10 ** 6, 0, 0, model="claude-opus-5-5")])
        write_jsonl(self.log, [status_event(A, "switch", "Claude Code (default login), claude-opus-5-5 at effort high "
                                                         "-> Claude Code (gemini login), google/gemini-4-neon: moved",
                                            moved)])
        got = self.spend(moved + 3600)["gemini"]
        self.assertAlmostEqual(got["dollars"], 3 + 1.5)
        self.assertEqual(len(got["notes"]), 2)
        self.assertIn("google/gemini-9 (1234 tokens)", got["notes"][0])
        self.assertEqual(got["notes"][1], "left out, not a model this login runs: claude-opus-5-5 (1 reply, $4.00 at "
                                          "its price)")

    def test_the_usage_reading_carries_it_on_the_logins_account(self):
        """bin/agent-board's annotate_usage, as the collector's usage reading runs it: the spend on the per-token
        login's account alone."""
        board = load_agent_board()
        d = Path(tempfile.mkdtemp())
        (d / "config.json").write_text(json.dumps({"usage": {"claude_accounts": self.accounts}}))
        write_jsonl(self.home / "projects" / "p" / (A + ".jsonl"),
                    [gemini_reply(A, "m1", utc(2026, 11, 26), 0, 0, 10 ** 6)])
        reading = {"generated_at": T0, "accounts": [
            {"target": "claude:default", "windows": [{"kind": "weekly", "remaining_pct": 40, "resets_at": T0}]},
            {"target": "claude:gemini", "billing": "per_token", "windows": [], "weekly_budget_usd": 500}]}
        saved = (sources.MAIL_CONFIG, sources.STATUS_LOG, sources.claude_logins, allowance.SPEND_CACHE,
                 allowance.SPEND_LOCK, allowance.time.time, allowance.SAMPLES)
        sources.MAIL_CONFIG, sources.STATUS_LOG = d / "config.json", self.log
        sources.claude_logins = lambda path=None: []         # mark_shared_logins: the live logins' .claude.json
        allowance.SPEND_CACHE, allowance.SPEND_LOCK = d / "spend-cache.json", d / "spend.lock"
        allowance.SAMPLES = d / "usage-samples.jsonl"
        allowance.time.time = lambda: utc(2026, 11, 27, 12)
        try:
            usage = sources.Usage(cache_path=d / "usage-cache.json", runner=lambda force: reading,
                                  annotate=board.annotate_usage, sync=True)
            accounts = usage.maybe_refresh(force=True)["accounts"]
        finally:
            (sources.MAIL_CONFIG, sources.STATUS_LOG, sources.claude_logins, allowance.SPEND_CACHE,
             allowance.SPEND_LOCK, allowance.time.time, allowance.SAMPLES) = saved
        self.assertIsNone(usage.error)
        self.assertNotIn("spend", accounts[0])
        self.assertEqual(accounts[1]["spend"], {"dollars": 15.0, "budget": 500.0, "since": utc(2026, 11, 23),
                                                "notes": []})
        self.assertTrue((d / "spend-cache.json").exists())


class Action(unittest.TestCase):
    def test_a_request_without_a_pool_and_window_is_refused(self):
        for req in ({"action": "usage_split"}, {"action": "usage_split", "target": "codex", "window": 7}):
            res = actions.dispatch(req, lambda: [], lambda: [])
            self.assertEqual(res, {"ok": False, "error": "target and window must be strings"})


if __name__ == "__main__":
    unittest.main()
