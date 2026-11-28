"""Where a plan window's allowance has gone since it last reset: each agent's share, by board group and sub-group,
and Other; and what a per-token login (a model on OpenRouter) has spent this week, against its weekly budget. The
README's sections "Plan usage: where it went" and "Spend of a per-token login" say how they are worked out; each
function says how it does its part.

compute() answers the usage_split action, and weekly_spend() the board's spend meters. ~/.config/agent-board/
allowance.json overrides DEFAULTS. The parsed transcripts are cached (private): CACHE for the split, SPEND_CACHE for
the spend, as one reads 21 days and the other a week, and a cache shared between them would be read afresh each time.
Each split adds a line to LOG. The fit reads the usage readings sample() records in SAMPLES: the board's usage refresh
(bin/agent-board's annotate_usage) calls it with each new reading, and it keeps at most one line per SAMPLE_EVERY
seconds.
"""
import bisect
import collections
import datetime
import glob
import json
import math
import os
import re
import time
import zoneinfo
from pathlib import Path

from . import collect, colours, groups, sources, timesources, timeuse

CONFIG = Path(os.environ.get("AGENT_BOARD_ALLOWANCE_CONFIG") or sources.HOME / ".config/agent-board/allowance.json")
CACHE = sources.BOARD_STATE / "allowance-cache.json"
LOCK = sources.BOARD_STATE / "allowance.lock"
LOG = sources.BOARD_STATE / "allowance.log"
SPEND_CACHE = sources.BOARD_STATE / "spend-cache.json"
SPEND_LOCK = sources.BOARD_STATE / "spend.lock"
SAMPLES = sources.BOARD_STATE / "usage-samples.jsonl"
SAMPLE_EVERY = 30 * 60      # seconds between two usage readings kept for the fit
SAMPLES_MAX, SAMPLES_TRIM = 1000, 700     # past SAMPLES_MAX lines (~20 days), keep the newest SAMPLES_TRIM
DEFAULTS = {
    # dollars per million tokens at API rates: [input, output, cache read], or [input, output, cache read, cache write]
    # for a model whose cache writes have a price of their own; without one, a cache write costs 1.25 times the input
    # price (5-minute) or 2 times (1-hour), as Anthropic charges. Codex's models have no published prices: theirs are
    # weights between token kinds, since the fitted factor sets the scale. A per-token login's model (OpenRouter's
    # maker/model name) needs its price here, at what OpenRouter charges, for its spend meter: add yours in
    # allowance.json (README, "Spend of a per-token login"). Gemini's cache writes cost nothing on OpenRouter.
    "prices": {
        "google/gemini-4-neon": [3, 15, 0.15, 0], "google/gemini-4-hydrogen": [1.5, 7.5, 0.075, 0],
        "claude-fable-5-2": [8, 40, 0.2], "claude-fable-5-1": [10, 50, 0.25], "claude-fable-5": [10, 50, 1],
        "claude-opus-5-5": [4, 20, 0.2], "claude-opus-5": [5, 25, 0.5], "claude-opus-4-8": [5, 25, 0.5],
        "claude-opus-4-7": [5, 25, 0.5], "claude-opus-4-6": [5, 25, 0.5],
        "claude-sonnet-5-5": [2, 10, 0.2], "claude-sonnet-5": [2, 10, 0.2], "claude-sonnet-4-6": [3, 15, 0.3],
        "claude-haiku-5-5": [0.1, 0.5, 0.01], "claude-haiku-4-5": [1, 5, 0.1],
        "gpt-6.1-sol": [1.25, 10, 0.125], "gpt-6-astra": [1.25, 10, 0.125], "gpt-5.6-sol": [1.25, 10, 0.125],
    },
    "fit_hours": 6,          # each point of the fit: the window's rise over this many hours of readings
    "fit_days": 21,          # how far back the fit reads (sample() keeps about 20 days of readings)
    "min_fit_points": 8,     # with fewer stretches of agent use than this there is no factor, so no percentages
    "week_zone": None,       # a weekly budget's week starts on Monday at midnight in this time zone (None: local time)
}
WINDOW_SECONDS = {"session": 5 * 3600, "weekly": 7 * 86400}     # a model's own window ("model:<name>") is weekly
CHART_STEPS = 168                                               # the chart's points across a window (hourly in a week)
CACHE_WRITE = {"5m": 1.25, "1h": 2.0}                          # times the input price
ORCHESTRATOR = "orchestrator"                                   # one row for all of the orchestrator's sessions


def config():
    """DEFAULTS with the config file's overrides. Raises ValueError for prices that are not
    {model: [input, output, cache read] or [input, output, cache read, cache write]} in dollars per million tokens,
    and for a week_zone that names no time zone."""
    cfg = sources.load_config(DEFAULTS, CONFIG)
    p = cfg["prices"]
    if not (isinstance(p, dict) and all(isinstance(v, list) and len(v) in (3, 4) and
                                        all(isinstance(x, (int, float)) for x in v) for v in p.values())):
        raise ValueError("%s: prices must map each model to [input, output, cache read] or [input, output, cache read, "
                         "cache write] dollars per million tokens" % CONFIG)
    if cfg["week_zone"] is not None:
        try:
            zoneinfo.ZoneInfo(cfg["week_zone"])
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValueError("%s: no time zone %r (week_zone)" % (CONFIG, cfg["week_zone"])) from None
    return cfg


def log(msg):
    sources.append_log(LOG, msg)


# ------------------------------------------------------------------ usage readings

def sample(accounts, t=None, path=None):
    """Keep one usage reading for the fit: what each pool had used of each window, and when that window resets, as
    {"ts", "active", "pools": {target: {window key (sources.windows): {"used", "resets"}}}, "busy"} ("active" and
    "busy" are always false and empty: the board starts no runs of its own whose use is not organic). accounts: a
    usage reading's; an account read from agent-usage's cache ("stale") is left out, since its numbers are older
    than `t`. At most one line per SAMPLE_EVERY seconds, checked under the file's lock, so several collectors may
    call it; past SAMPLES_MAX lines the file keeps the newest SAMPLES_TRIM. The line written, or None (too soon, or
    no pool with a window). Raises OSError when the file cannot be written, ValueError when it is damaged."""
    t = t or time.time()
    path = Path(path or SAMPLES)
    pools = {}
    for a in accounts or []:
        if a.get("stale") or not a.get("target"):
            continue
        ws = {k: {"used": 100 - sources.left_pct(w), "resets": w.get("resets_at")}
              for k, w in sources.windows(a).items()}
        if ws:
            pools[a["target"]] = ws
    if not pools:
        return None
    rec = {"ts": t, "active": False, "pools": pools, "busy": {}}
    too_soon = lambda rows: any(isinstance(r, dict) and abs(t - (r.get("ts") or 0)) < SAMPLE_EVERY for r in rows[-5:])
    if not sources.append_jsonl(path, rec, unless=too_soon):
        return None
    with sources.file_lock(path.with_name(path.name + ".lock")):
        rows = sources.read_jsonl(path)
        if len(rows) > SAMPLES_MAX:
            sources.write_text_atomic(path, "".join(json.dumps(r) + "\n" for r in rows[-SAMPLES_TRIM:]))
    return rec


# ------------------------------------------------------------------ transcripts

def parse_claude(lines, out, path):
    """A Claude transcript's replies (sources.read_appended's parse): {"sid", "cwd", "replies": {message id: [ts,
    model, input, 5-minute cache writes, 1-hour cache writes, cache reads, output]}}. A reply's content blocks are
    lines of their own that repeat its usage; the output count is complete on the last. sid is the session the lines
    name: a subagent's transcript names its parent's, and a transcript copied by a tool names the session it copies.
    "unread": the lines that might have held a reply but are not JSON."""
    out = out or {"sid": "", "cwd": "", "replies": {}, "unread": 0}
    for line in lines:
        if b'"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            out["unread"] += 1
            continue
        m = d.get("message")
        if d.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict) \
                or not d.get("timestamp"):
            continue
        if not out["sid"]:
            out.update(sid=d.get("sessionId") or Path(path).stem, cwd=d.get("cwd") or "")
        u, cw = m["usage"], m["usage"].get("cache_creation") or {}
        five, hour = cw.get("ephemeral_5m_input_tokens"), cw.get("ephemeral_1h_input_tokens")
        if five is None and hour is None:            # no breakdown: Claude Code's writes last an hour
            five, hour = 0, u.get("cache_creation_input_tokens") or 0
        row = [sources.iso_ts(d["timestamp"]), m.get("model") or "", u.get("input_tokens") or 0, five or 0, hour or 0,
               u.get("cache_read_input_tokens") or 0, u.get("output_tokens") or 0]
        key = m.get("id") or d.get("requestId") or d.get("uuid")
        had = out["replies"].get(key)
        if had is None or row[6] > had[6]:
            out["replies"][key] = row if had is None else had[:6] + row[6:]
    return out


def parse_codex(lines, out, path):
    """A Codex rollout's turns, as parse_claude's replies: each token_count event's growth of the thread's totals,
    uncached input as input and cached input as cache reads, on the model of the turn it is in. A thread spawned or
    forked from another first replays that one's events, so those before the thread started only set where its own
    totals start. "parent": the thread that spawned it, or asked for it (a review thread). Totals that fall start
    again from there. "unread": the lines that might have held a turn but are not JSON; a first line that is not
    JSON, or names no thread, leaves the thread without an id ("sid" empty), so the ledger leaves it out and says so."""
    if out is None:
        first = next(lines, None)
        if first is None:
            return None
        try:
            meta = json.loads(first).get("payload") or {}
        except ValueError:
            meta = {}
        out = {"sid": meta.get("id") or "", "cwd": meta.get("cwd") or "", "replies": {},
               "parent": meta.get("parent_thread_id") or sources.subagent_parent(json.dumps(meta.get("source")), ""),
               "start": sources.iso_ts(meta["timestamp"]) if meta.get("timestamp") else 0, "model": "",
               "total": [0, 0, 0], "unread": 0}
    for line in lines:
        if b'"token_count"' not in line and b'"turn_context"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            out["unread"] += 1
            continue
        p = d.get("payload") or {}
        if d.get("type") == "turn_context":
            out["model"] = p.get("model") or out["model"]
            continue
        tot = ((p.get("info") or {}).get("total_token_usage")) if p.get("type") == "token_count" else None
        if not tot or not d.get("timestamp"):
            continue
        now = [tot.get("input_tokens") or 0, tot.get("cached_input_tokens") or 0, tot.get("output_tokens") or 0]
        grew = [a - b for a, b in zip(now, out["total"])]
        out["total"] = now
        ts = sources.iso_ts(d["timestamp"])
        if ts < out["start"] or min(grew) < 0 or not any(grew):
            continue
        out["replies"]["%s:%d" % (out["sid"], len(out["replies"]))] = [ts, out["model"], grew[0] - grew[1], 0, 0,
                                                                       grew[1], grew[2]]
    return out


def moves(path):
    """({session id: [(ts, login before, login after)]}, oldest first, and how many move events could not be read):
    each move between Claude logins in the status log at `path`, agent-failover's ("failover", "A -> B (why)") and
    agent-switch's ("switch", "... (A login) ... -> ... (B login) ..."). A move that failed, or a switch to or from
    Codex (no login on one side), is not a move between logins."""
    fail_re = re.compile(r"^(\S+) -> (\S+) \(")
    switch_re = re.compile(r"\((\S+) login\).* -> .*\((\S+) login\)")
    out, unread = collections.defaultdict(list), 0
    if not Path(path).exists():
        return {}, 0
    with open(path, errors="replace") as fh:
        for line in fh:
            if '"failover"' not in line and '"switch"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ev, text = e.get("event"), e.get("text") or ""
            if ev not in ("failover", "switch") or "FAILED" in text or e.get("cli") != "claude":
                continue
            if ev == "switch" and " login)" not in text.split(" -> ", 1)[0]:
                continue                       # from Codex: a new session, not a move between logins
            m = (fail_re if ev == "failover" else switch_re).search(text)
            if not m or (ev == "failover" and m.group(2) != e.get("account")):
                unread += 1
                continue
            if m.group(1) != m.group(2):
                out[e.get("session_id") or ""].append((float(e.get("ts") or 0), m.group(1), m.group(2)))
    return {sid: sorted(ms) for sid, ms in out.items()}, unread


def login_at(ms, ts, folder):
    """The login a session was on at `ts`, given its moves (moves()'s list, or none): `folder`'s, the login whose
    folder holds its transcript, when it never moved."""
    if not ms:
        return folder
    login = ms[0][1]
    for t, _, to in ms:
        if ts >= t:
            login = to
    return login


def price(row, prices):
    """A reply's dollars at API rates, None when its model has no price. Cache writes cost the model's own price for
    them when it has one, else CACHE_WRITE's multiples of the input price."""
    p = prices.get(row[1])
    if p is None:
        return None
    inp, out, read = p[:3]
    five, hour = (p[3], p[3]) if len(p) > 3 else (inp * CACHE_WRITE["5m"], inp * CACHE_WRITE["1h"])
    return (row[2] * inp + row[3] * five + row[4] * hour + row[5] * read + row[6] * out) / 1e6


def ledger(cfg, since, logins, codex_home, status_log, cache_path):
    """Every reply since `since`, once: ([(target, session id, ts, model, dollars)], {session id: cwd}, notes).
    logins: [(login name, its config folder)] (sources.claude_logins); codex_home None reads no Codex threads; a Codex
    sub-thread's replies count on the thread that spawned it. A reply whose model has no price is left out and named
    in the notes, with its tokens. A reply in several Claude transcripts counts on the first of them by their first
    reply (then session id): the copies failover makes are of one session, and a fork's copied history counts on one
    of the two."""
    paths, folder = [], {}
    for name, home in logins:
        for pat in (("projects", "*", "*.jsonl"), ("projects", "*", "*", "subagents", "*.jsonl")):
            for p in sorted(glob.glob(os.path.join(str(home), *pat))):
                paths.append((p, parse_claude))
                folder[p] = name
    if codex_home is not None:
        paths += [(p, parse_codex) for p in sorted(glob.glob(os.path.join(str(codex_home), "sessions", "*", "*", "*",
                                                                         "rollout-*.jsonl")))]
    parsed = sources.read_appended(paths, since, cache_path, log)
    moved, unread = moves(status_log)
    rows, cwd, unpriced, notes = [], {}, collections.Counter(), []

    def add(target, sid, r):
        if r[0] < since or not any(r[2:]):
            return
        d = price(r, cfg["prices"])
        if d is None:
            unpriced[r[1] or "(no model)"] += sum(r[2:])
        else:
            rows.append((target, sid, r[0], r[1], d))

    claude = sorted(((st, p) for p, st in parsed.items() if p in folder and st["replies"]),
                    key=lambda x: (min(r[0] for r in x[0]["replies"].values()), x[0]["sid"], x[1]))
    seen = set()
    for st, p in claude:
        cwd.setdefault(st["sid"], st["cwd"])
        for key, r in st["replies"].items():
            if key not in seen:
                seen.add(key)
                login = login_at(moved.get(st["sid"]), r[0], folder[p])
                add(sources.usage_target({"cli": "claude", "account": login}), st["sid"], r)
    threads = [st for p, st in parsed.items() if p not in folder and st["sid"]]
    no_id = sum(1 for p, st in parsed.items() if p not in folder and not st["sid"])
    unread_lines = sum(st.get("unread", 0) for st in parsed.values())
    parent = {st["sid"]: st["parent"] for st in threads}

    def root(sid):
        hops = 0
        while parent.get(sid) in parent and hops < 20:          # a sub-thread may spawn sub-threads of its own
            sid, hops = parent[sid], hops + 1
        return sid

    for st in sorted(threads, key=lambda st: st["sid"] != root(st["sid"])):     # a thread's own folder first
        cwd.setdefault(root(st["sid"]), st["cwd"])
        for r in st["replies"].values():
            add("codex", root(st["sid"]), r)
    if unread_lines:
        notes.append("%d transcript line%s that might have held a reply could not be read and %s left out" % (
            unread_lines, "" if unread_lines == 1 else "s", "is" if unread_lines == 1 else "are"))
    if no_id:
        notes.append("%d Codex thread%s left out: the first line does not name the thread" % (
            no_id, "" if no_id == 1 else "s"))
    if unread:
        notes.append("%d login move%s in the status log could not be read: those agents' replies count on the login "
                     "whose folder holds them" % (unread, "" if unread == 1 else "s"))
    if unpriced:
        notes.append("left out, no price for the model (add it to %s): %s" % (
            CONFIG, ", ".join("%s (%s tokens)" % (m, n) for m, n in unpriced.most_common())))
    return rows, cwd, notes


# ------------------------------------------------------------------ the factor, and the split

def window_rows(rows, target, key):
    """The rows of pool `target` that window `key` counts: all of them, or for a model's own window
    ("model:<name>") that model's."""
    model = key[6:] if key.startswith("model:") else None
    return [r for r in rows if r[0] == target and (model is None or model in r[3].lower())]


def fit(rows, samples, target, key, cfg, now):
    """{"pct_per_dollar", "points", "rms"}: the factor that best turns the agents' dollars into the rise of window
    `key` of pool `target`, over stretches of cfg["fit_hours"] in the last cfg["fit_days"] of usage readings
    (sample()'s lines), fitted through the origin; rms is the typical miss of one stretch, in points of the
    window. None with fewer than cfg["min_fit_points"] stretches in which the agents used any."""
    rows = sorted(rows, key=lambda r: r[2])
    ts = [r[2] for r in rows]
    cum = [0.0]
    for r in rows:
        cum.append(cum[-1] + r[4])

    def spent(a, b):
        return cum[bisect.bisect_right(ts, b)] - cum[bisect.bisect_right(ts, a)]

    pts = sorted((s["ts"], w["used"], float(w["resets"])) for s in samples if s.get("ts", 0) >= now - cfg["fit_days"] * 86400
                 for w in [((s.get("pools") or {}).get(target) or {}).get(key)] if w and w.get("resets"))
    xy, start = [], pts[0] if pts else None
    for p in pts[1:]:
        if not sources.same_reset(p[2], start[2]):             # a new window
            start = p
        elif p[0] - start[0] >= cfg["fit_hours"] * 3600:
            xy.append((spent(start[0], p[0]), p[1] - start[1]))
            start = p
    xy = [(x, y) for x, y in xy if x > 0]
    if len(xy) < cfg["min_fit_points"]:
        return None
    k = sum(x * y for x, y in xy) / sum(x * x for x, _ in xy)
    return {"pct_per_dollar": k, "points": len(xy), "rms": math.sqrt(sum((y - k * x) ** 2 for x, y in xy) / len(xy))}


def window_of(accounts, target, key):
    """(account, window) of pool `target`'s window `key` (sources.windows' keys) in a usage reading's accounts.
    Raises ValueError when the reading has no such pool or window."""
    acct = next((a for a in accounts if a.get("target") == target), None)
    if acct is None:
        raise ValueError("no usage reading for %s" % target)
    w = sources.windows(acct).get(key)
    if w is None or not w.get("resets_at"):
        raise ValueError("%s has no %s window with a reset time" % (target, key))
    return acct, w


def timeline(rows, since, until, group_of, k, readings):
    """The chart of a window: {"start", "step", "unit", "groups": [{"name", "values"}], "other"}. values: each
    group's use from `since` to the end of each step, in percent of the window with a factor `k` (unit "%"), else in
    dollars ("$"); other: the readings ([(ts, percent used)], oldest first) less all the groups' use at the end of
    each step, never below 0, or None without a factor. group_of: {session id: group name}, in the order to stack."""
    step = max(300, int((until - since) // CHART_STEPS) // 300 * 300)
    n = max(1, math.ceil((until - since) / step))
    per = {g: [0.0] * n for g in dict.fromkeys(group_of.values())}
    for r in rows:
        if since <= r[2] < until:
            per[group_of[r[1]]][min(n - 1, int((r[2] - since) // step))] += r[4]
    scale = 1.0 if k is None else k
    groups_out = []
    for g, vals in per.items():
        run, cum = 0.0, []
        for v in vals:
            run += v * scale
            cum.append(round(run, 3))
        groups_out.append({"name": g, "values": cum})
    other = None
    if k is not None:
        other, i = [], 0
        for j in range(n):
            end = since + (j + 1) * step
            while i < len(readings) and readings[i][0] <= end:
                i += 1
            seen = readings[i - 1][1] if i else 0.0
            other.append(round(max(0.0, seen - sum(g["values"][j] for g in groups_out)), 3))
    return {"start": since, "step": step, "unit": "$" if k is None else "%", "groups": groups_out, "other": other}


def split(target, key, accounts, rows, cwd, samples, cfg, now, history, layout, names, orch_dir, palette):
    """Where window `key` of pool `target` has gone since it last reset: {"target", "window", "since", "resets_at",
    "used", "dollars", "scale" (fit's, or None), "agents_pct", "other_pct", "groups", "chart" (timeline's), "colours"
    (the layout's, colours.for_layout's of `palette`, so the Mac colours each group as the Time page does)}. groups:
    [{"name", "dollars", "pct", "agents", "subgroups": [{"name", "dollars", "pct", "agents"}]}], biggest first, and
    each agent {"id", "name", "dollars", "pct", "inferred"} (inferred: grouped by its repo, not placed by the owner).
    The percentages are None without a factor. rows, cwd: ledger's; samples: sample()'s lines; history, layout:
    groups'; names: {session id: name}. Raises ValueError for a pool or window the reading lacks."""
    _, w = window_of(accounts, target, key)
    since = float(w["resets_at"]) - WINDOW_SECONDS.get(key, WINDOW_SECONDS["weekly"])
    used = 100 - sources.left_pct(w)
    mine = window_rows(rows, target, key)
    scale = fit(mine, samples, target, key, cfg, now)
    k = scale["pct_per_dollar"] if scale else None
    per = collections.Counter()
    for r in mine:
        if r[2] >= since:
            per[r[1]] += r[4]
    orch = {sid for sid in per if collect.is_orchestrator(sid, names.get(sid), cwd.get(sid), orch_dir)}
    if orch:
        per[ORCHESTRATOR] = sum(per.pop(sid) for sid in orch)
    session_of = {sid: ORCHESTRATOR if sid in orch else sid for sid in {r[1] for r in mine if r[2] >= since}}
    acts = [{"key": "s:" + sid, "repo": cwd.get(sid, "")} for sid in per]
    no_guesses = lambda *a: ({}, {"asked": 0, "batches": 0}, None)
    assigned, _, _ = timeuse.assign(acts, history, layout, {}, {}, no_guesses)
    pct = lambda d: None if k is None else round(d * k, 2)
    tree = collections.OrderedDict()
    for sid, d in per.most_common():
        g, sub, inferred = assigned["s:" + sid]
        label = ORCHESTRATOR if sid == ORCHESTRATOR else (
            names.get(sid) or os.path.basename((cwd.get(sid) or "").rstrip("/")) or sid[:8])
        node = tree.setdefault(g, {"name": g, "dollars": 0.0, "agents": [], "subgroups": collections.OrderedDict()})
        node["dollars"] += d
        if sub:
            node = node["subgroups"].setdefault(sub, {"name": sub, "dollars": 0.0, "agents": []})
            node["dollars"] += d
        node["agents"].append({"id": sid, "name": label, "dollars": round(d, 2), "pct": pct(d), "inferred": inferred})
    out_groups = []
    for g in sorted(tree.values(), key=lambda g: -g["dollars"]):
        subs = [dict(s, dollars=round(s["dollars"], 2), pct=pct(s["dollars"])) for s in g["subgroups"].values()]
        out_groups.append(dict(g, dollars=round(g["dollars"], 2), pct=pct(g["dollars"]),
                               subgroups=sorted(subs, key=lambda s: -s["dollars"])))
    total = sum(per.values())
    stack = {g["name"]: i for i, g in enumerate(out_groups)}
    group_of = dict(sorted(((sid, assigned["s:" + s][0]) for sid, s in session_of.items()),
                           key=lambda x: stack[x[1]]))
    readings = sorted((s["ts"], v["used"]) for s in samples if since <= s.get("ts", 0) <= now
                      for v in [((s.get("pools") or {}).get(target) or {}).get(key)]
                      if v and v.get("resets") and sources.same_reset(v["resets"], w["resets_at"]))
    return {"target": target, "window": key, "since": since, "resets_at": float(w["resets_at"]), "used": used,
            "dollars": round(total, 2), "scale": scale, "agents_pct": pct(total),
            "other_pct": None if k is None else round(used - total * k, 2), "groups": out_groups,
            "chart": timeline(mine, since, min(now, float(w["resets_at"])), group_of, k, readings),
            "colours": colours.for_layout(layout, palette)}


def compute(target, key, now=None):
    """split() for window `key` of pool `target` from this machine's sources: the usage reading (agent-usage), the
    agents' transcripts (through CACHE, under LOCK), the status log, the usage readings (SAMPLES), the board's groups
    and the mailroom's session names; with "notes" on what was left out. Raises ValueError for a pool or window the
    reading lacks or a damaged config or colours.json, and RuntimeError when agent-usage fails."""
    now = now or time.time()
    cfg = config()
    palette = colours.load()
    accounts = sources.read_usage()
    window_of(accounts, target, key)                 # an unknown pool or window fails before any transcript is read
    with sources.file_lock(LOCK):
        rows, cwd, notes = ledger(cfg, now - cfg["fit_days"] * 86400, sources.claude_logins(), sources.CODEX_HOME,
                                  sources.STATUS_LOG, CACHE)
    out = split(target, key, accounts, rows, cwd, sources.read_jsonl(SAMPLES), cfg, now, groups.history(),
                groups.layout(), timesources.session_names(sources.MAIL_DB), collect.orchestrator_dir(), palette)
    if out["scale"] is None:
        notes.append("too few usage readings with agent use to turn dollars into percent of the window (needs %d "
                     "stretches of %d hours in the last %d days)" % (cfg["min_fit_points"], cfg["fit_hours"],
                                                                     cfg["fit_days"]))
    out["notes"] = notes
    log("%s %s: %.0f%% used, agents $%.0f%s" % (
        target, key, out["used"], out["dollars"], "" if out["scale"] is None else " = %.1f%%, other %.1f%% (%.2f%% per "
        "$100 from %d stretches, rms %.1f)" % (out["agents_pct"], out["other_pct"], 100 * out["scale"]["pct_per_dollar"],
                                               out["scale"]["points"], out["scale"]["rms"])))
    return out


# ------------------------------------------------------------------ a per-token login's weekly spend

def week_start(now, zone):
    """The start of the week holding `now` (epoch seconds): Monday 00:00 in time zone `zone` (None: this machine's local
    time), as epoch seconds. Across a clock change the week is an hour longer or shorter, as the clocks there say."""
    if zone is None:
        day = datetime.date.fromtimestamp(now)
        monday = day - datetime.timedelta(days=day.weekday())
        return time.mktime(datetime.datetime.combine(monday, datetime.time(0)).timetuple())
    tz = zoneinfo.ZoneInfo(zone)
    day = datetime.datetime.fromtimestamp(now, tz).date()
    monday = day - datetime.timedelta(days=day.weekday())
    return datetime.datetime.combine(monday, datetime.time(0), tzinfo=tz).timestamp()


def budgets(accounts=None):
    """[(login name, config folder, weekly budget in dollars, model)] for each Claude login of the mailroom's
    usage.claude_accounts (`accounts`, read from its config when None) with a weekly_budget_usd: a per-token login
    (a model through OpenRouter), its model the one it runs (its "model", or None: any), its folder sources.login_home's.
    Raises ValueError for a weekly_budget_usd that is not a positive number of dollars (a login with no budget leaves
    the key out), or a "model" that is not a model's name."""
    accounts = sources.claude_accounts() if accounts is None else accounts
    out = []
    for name, a in accounts.items():
        if not isinstance(a, dict) or "weekly_budget_usd" not in a:
            continue
        budget, model = a["weekly_budget_usd"], a.get("model")
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
            raise ValueError("usage.claude_accounts.%s.weekly_budget_usd is %r, not a positive number of dollars"
                             % (name, budget))
        if model is not None and not (isinstance(model, str) and model):
            raise ValueError("usage.claude_accounts.%s.model is %r, not a model's name" % (name, model))
        out.append((name, sources.login_home(a), float(budget), model))
    return out


def own_model(model, login_model):
    """Whether a reply's model is one the login runs: one from the provider of its model, the part before "/"
    ("google/" for a google/... model), so its sub-agents' other models from that provider count; any model when the
    login names none."""
    if not login_model:
        return True
    return model.startswith(login_model.split("/", 1)[0] + "/") if "/" in login_model else model == login_model


def spend_of(rows, target, since, login_model):
    """(dollars, notes): what pool `target` (claude:<login>) spent since `since`, from ledger's rows. A reply on a
    model the login does not run (own_model) is left out and named in the notes with what it would cost: such a model
    cannot run there (a Claude model id fails on OpenRouter's endpoint), so its replies are ones credited to the login
    by mistake, as when a move between logins is missing from the status log."""
    dollars, other = 0.0, collections.defaultdict(lambda: [0, 0.0])
    for t, _, ts, model, d in rows:
        if t != target or ts < since:
            continue
        if own_model(model, login_model):
            dollars += d
        else:
            other[model or "(no model)"][0] += 1
            other[model or "(no model)"][1] += d
    notes = ["left out, not a model this login runs: %s" % ", ".join(
        "%s (%d repl%s, $%.2f at its price)" % (m, n, "y" if n == 1 else "ies", d)
        for m, (n, d) in sorted(other.items(), key=lambda x: -x[1][1]))] if other else []
    return dollars, notes


def weekly_spend(now=None, accounts=None, status_log=None, cache_path=None):
    """{login name: {"dollars", "budget", "since", "notes"}} for each per-token login (budgets(accounts)): the dollars
    its agents spent since Monday 00:00 in the config's week_zone (week_start, "since"), at its prices (OpenRouter's,
    from allowance.json) from their transcripts, through the split's own ledger (each reply once, credited to the login it ran on
    by the status log's moves); "notes" say what was left out. Reads only those logins' folders (a session's replies
    on a login are in its copy there), through `cache_path` (SPEND_CACHE) under SPEND_LOCK. {} with no such login.
    Raises ValueError for a damaged config (the mailroom's, budgets', or allowance.json)."""
    now = now or time.time()
    found = budgets(accounts)
    if not found:
        return {}
    cfg = config()
    since = week_start(now, cfg["week_zone"])
    with sources.file_lock(SPEND_LOCK if cache_path is None else Path(str(cache_path) + ".lock")):
        rows, _, notes = ledger(cfg, since, [(name, home) for name, home, _, _ in found], None,
                                sources.STATUS_LOG if status_log is None else status_log,
                                SPEND_CACHE if cache_path is None else cache_path)
    out = {}
    for name, _, budget, model in found:
        dollars, own = spend_of(rows, sources.usage_target({"cli": "claude", "account": name}), since, model)
        out[name] = {"dollars": dollars, "budget": budget, "since": since, "notes": notes + own}
    return out


def annotate(accounts, spend=None):
    """Put "spend" (weekly_spend's, or `spend`) on the account of each per-token login in a usage reading's accounts
    (in place), for the Mac's spend meter. Raises as weekly_spend does."""
    spend = weekly_spend() if spend is None else spend
    for a in accounts:
        target = a.get("target") or ""
        if target.startswith("claude:") and target[7:] in spend:
            a["spend"] = spend[target[7:]]
