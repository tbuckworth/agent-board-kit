"""Where a plan window's allowance went (agentboard/allowance.py): replies read from Claude and Codex transcripts,
the moves between logins, each reply counted once on its login, the fitted factor, and the split by group with
Other, and the usage readings the board keeps for the fit. Every file goes to a temporary folder."""
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fixtures import iso, parse_file, repo_with_worktree, write_jsonl  # noqa: E402
from agentboard import actions, allowance  # noqa: E402

T0 = 1793000000.0
WEEK = 7 * 86400
A, B, C, D = (str(uuid.UUID(int=n)) for n in (1, 2, 3, 4))      # session ids
CFG = dict(allowance.DEFAULTS, prices={"claude-opus-5-5": [4, 20, 0.2], "claude-fable-5-1": [10, 50, 0.25],
                                       "gpt-6.1-sol": [1, 8, 0.1]}, min_fit_points=3)


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
                               T0 + 48 * 3600, self.HISTORY, self.LAYOUT, {A: "todo-1", B: "board"}, "/w/orchestrator")

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
        self.assertEqual(s["layout"], self.LAYOUT)

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


class Action(unittest.TestCase):
    def test_a_request_without_a_pool_and_window_is_refused(self):
        for req in ({"action": "usage_split"}, {"action": "usage_split", "target": "codex", "window": 7}):
            res = actions.dispatch(req, lambda: [], lambda: [])
            self.assertEqual(res, {"ok": False, "error": "target and window must be strings"})


if __name__ == "__main__":
    unittest.main()
