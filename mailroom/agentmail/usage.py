"""Plan usage left on each account, read from the providers' servers, and which account a new agent should use.

Claude Code  One `claude -p --safe-mode --no-session-persistence --input-format stream-json` per account, answering
             a single `get_usage` control request: the claude.ai usage endpoint that `/usage` shows. No prompt is
             sent, so no model runs and no quota is spent (about 2 s). Claude Code reads its own credentials;
             nothing here opens a token file. An account is a config dir (CLAUDE_CONFIG_DIR); None is the
             default ~/.claude, run with CLAUDE_CONFIG_DIR unset (setting it to ~/.claude would move .claude.json).
Codex        `codex app-server` over stdio, JSON-RPC `account/rateLimits/read`: what Codex's /status shows, live
             from the server, no model turn (about 2 s). If that fails, the last `rate_limits` a Codex session
             wrote to its rollout log, labelled "session-log" with its age.

Every percentage is server-reported. None is estimated from local token counts: plan limits are not published
in tokens, so a local count cannot be turned into a percentage honestly. Readings are cached for
`cache_seconds` in STATE_DIR/usage-cache.json (percentages, reset times, plan names; no identities, no tokens).
When a read fails, the last good reading is used, marked stale with its age.

Routing: places are tried in `places` order, each Claude login in `order` on each model of
config.claude_model_order in turn; the first whose counted windows all keep at least `min_remaining` percent wins (recommend says what comes
next). Counted windows are the 5-hour/session and weekly ones, plus a per-model weekly window only when it is for
the model the agent would run there. Settings live under config key "usage"; DEFAULTS below.
"""
import datetime
import json
import os
import queue
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import accounts, claude_session, config, jsonfile, registry

DEFAULTS = {
    # Claude accounts, one config dir each. config_dir None is the default login (~/.claude, CLAUDE_CONFIG_DIR
    # unset). exclude_owners / only_owners: GitHub owners (from the repo's origin) this account must not / may
    # only be used for, e.g. keep employer repos off a personal plan. min_remaining overrides per account.
    "claude_accounts": {"default": {"config_dir": None, "label": "Claude (default login)"}},
    "codex_label": "Codex",   # how the report names Codex
    # Preference order: the first target with headroom wins. None means every Claude account, then codex.
    "order": None,
    # Percent that must REMAIN in each counted window; below it the account is "low" and skipped.
    "min_remaining": {"session": 10, "weekly": 5},
    "cache_seconds": 120,        # a reading younger than this is reused
    "stale_seconds": 6 * 3600,   # a failed read falls back to the last good reading up to this old
    "timeout": 30,               # seconds for one account's read
}
CODEX = "codex"   # Codex's target; a Claude login's is CLAUDE_PREFIX and the login's name (claude_target)
CLAUDE_PREFIX = "claude:"
CACHE_NAME = "usage-cache.json"


class UsageError(Exception):
    pass


def settings(cfg):
    s = dict(DEFAULTS, **(cfg.get("usage") or {}))
    s["min_remaining"] = dict(DEFAULTS["min_remaining"], **(s.get("min_remaining") or {}))
    if not s.get("claude_accounts"):
        s["claude_accounts"] = dict(DEFAULTS["claude_accounts"])
    if not s.get("order"):
        s["order"] = [claude_target(n) for n in s["claude_accounts"]] + [CODEX]
    return s


# ------------------------------------------------------------------ targets
def claude_target(name):
    """The target of Claude login `name`: "claude:<name>"."""
    return CLAUDE_PREFIX + name


def account_of(target):
    """The Claude login a target names ("claude:<name>" -> name), or None for CODEX."""
    return target[len(CLAUDE_PREFIX):] if target.startswith(CLAUDE_PREFIX) else None


def ranked(s):
    """The targets routing may pick, in preference order (unknown names in `order` are ignored)."""
    return [t for t in s["order"] if t == CODEX or account_of(t) in s["claude_accounts"]]


def places(s, claude_models, codex_model):
    """The (target, model) places routing tries, best first (the README's "login and model order"): each Claude
    login in `order` on claude_models[0], then each on claude_models[1], and so on; Codex, on codex_model, before
    them if `order` lists it before every Claude login, else after them."""
    order = ranked(s)
    claude = [t for t in order if t != CODEX]
    rungs = [(t, m) for m in claude_models for t in claude]
    if CODEX not in order:
        return rungs
    codex_first = not claude or order.index(CODEX) < order.index(claude[0])
    return [(CODEX, codex_model)] + rungs if codex_first else rungs + [(CODEX, codex_model)]


def targets(s):
    """Every target agent-usage reads and shows: the ranked ones, then any account left out of `order`."""
    out = ranked(s)
    out += [claude_target(n) for n in s["claude_accounts"] if claude_target(n) not in out]
    return out + ([CODEX] if CODEX not in out else [])


def parse_target(text, s):
    """'work', 'claude:work', 'codex' -> ('claude', 'work') / ('codex', None). ValueError if unknown."""
    t = (text or "").strip()
    if t in (CODEX, "codex:default"):
        return CODEX, None
    name = account_of(t)
    name = t if name is None else name
    if name in s["claude_accounts"]:
        return "claude", name
    raise ValueError("unknown account %r (Claude accounts: %s; or codex)"
                     % (text, ", ".join(sorted(s["claude_accounts"])) or "none"))


# ------------------------------------------------------------------ parsing (pure)
def iso_epoch(s):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _window(kind, name, used, resets_at, model=None):
    used = float(used)
    return {"kind": kind, "name": name, "used_pct": round(used, 1), "remaining_pct": round(max(0.0, 100.0 - used), 1),
            "resets_at": resets_at, "model": model}


def parse_claude(resp):
    """The get_usage control response -> windows, plan, extra usage. UsageError if the plan has no limits."""
    rl = resp.get("rate_limits")
    if not resp.get("rate_limits_available") or not isinstance(rl, dict):
        raise UsageError("no plan usage available (not logged in to a claude.ai plan, or a token without "
                         "the profile scope, e.g. from setup-token)")
    windows = []

    def add(kind, name, w, model=None):
        if isinstance(w, dict) and w.get("utilization") is not None:
            windows.append(_window(kind, name, w["utilization"], iso_epoch(w.get("resets_at")), model))

    add("session", "5-hour", rl.get("five_hour"))
    add("weekly", "weekly", rl.get("seven_day"))
    add("model_weekly", "weekly Opus", rl.get("seven_day_opus"), "opus")
    add("model_weekly", "weekly Sonnet", rl.get("seven_day_sonnet"), "sonnet")
    for m in rl.get("model_scoped") or []:
        dn = (m.get("display_name") or "model").strip()
        if not any(w["model"] == dn.lower() for w in windows):
            add("model_weekly", "weekly " + dn, m, dn.lower())
    if not windows:  # only the newer limits[] shape
        for lim in rl.get("limits") or []:
            kind = {"session": "session", "weekly_all": "weekly", "weekly_scoped": "model_weekly"}.get(lim.get("kind"), "other")
            model = (((lim.get("scope") or {}).get("model") or {}).get("display_name") or "").strip()
            name = {"session": "5-hour", "weekly": "weekly"}.get(kind) or ("weekly " + model if model else lim.get("kind", "?"))
            add(kind, name, {"utilization": lim.get("percent"), "resets_at": lim.get("resets_at")}, model.lower() or None)
    extra = None
    eu = rl.get("extra_usage")
    if isinstance(eu, dict) and eu.get("is_enabled") is not None:
        scale = 10 ** int(eu.get("decimal_places") or 2)
        extra = {"enabled": bool(eu["is_enabled"]),
                 "used": eu["used_credits"] / scale if eu.get("used_credits") is not None else None,
                 "monthly_limit": eu["monthly_limit"] / scale if eu.get("monthly_limit") is not None else None,
                 "currency": eu.get("currency") or "USD",
                 "limit_reached": bool(eu.get("spend_limit_reached"))}
    return {"plan": resp.get("subscription_type"), "windows": windows, "extra_usage": extra}


# How long each kind of window parse_claude reads runs, so it last reset that long before its resets_at. A window of
# kind "other" has no known length.
CLAUDE_WINDOW_SECONDS = {"session": 5 * 3600, "weekly": 7 * 86400, "model_weekly": 7 * 86400}


def claude_last_reset(w):
    """When a Claude login's window (parse_claude) last reset, as an epoch; None when its length or reset is unknown."""
    secs = CLAUDE_WINDOW_SECONDS.get(w["kind"])
    return w["resets_at"] - secs if secs and w.get("resets_at") else None


def window_name(mins):
    if not mins:
        return "window"
    if mins == 300:
        return "5-hour"
    if mins == 10080:
        return "weekly"
    if mins == 1440:
        return "daily"
    return "%dh" % (mins // 60) if mins % 1440 else "%dd" % (mins // 1440)


def parse_codex(result):
    """account/rateLimits/read result (camelCase) -> windows, plan, credits, blocked."""
    buckets = result.get("rateLimitsByLimitId") or {}
    main = buckets.get("codex") or result.get("rateLimits") or {}
    windows, blocked = [], []
    for bid, b in [("codex", main)] + [(k, v) for k, v in sorted(buckets.items()) if k != "codex"]:
        model = None if bid == "codex" else (b.get("normalModelSlug") or b.get("limitName") or bid).lower()
        for slot in ("primary", "secondary"):
            w = b.get(slot)
            if not isinstance(w, dict) or w.get("usedPercent") is None:
                continue
            mins = w.get("windowDurationMins")
            kind = "weekly" if mins and mins > 1440 else "session"   # unknown length: the stricter threshold
            name = window_name(mins)
            if model:
                kind, name = "model_" + kind, "%s %s" % (name, b.get("limitName") or model)
            windows.append(_window(kind, name, w["usedPercent"], w.get("resetsAt"), model))
        if bid == "codex":
            if b.get("rateLimitReachedType"):
                blocked.append("server reports %s" % b["rateLimitReachedType"].replace("_", " "))
            if b.get("spendControlReached"):
                blocked.append("server reports its spend control reached")
    if result.get("ordinaryUsageAllowed") is False:
        blocked.append("server reports ordinary usage not allowed")
    c = main.get("credits") or None
    credits = {"has": bool(c.get("hasCredits")), "unlimited": bool(c.get("unlimited")),
               "balance": c.get("balance")} if isinstance(c, dict) else None
    resets = (result.get("rateLimitResetCredits") or {}).get("availableCount")
    return {"plan": main.get("planType"), "windows": windows, "credits": credits,
            "reset_credits": resets, "blocked": blocked}


def codex_rollout_to_result(rl):
    """A rollout log's snake_case rate_limits -> the app-server's camelCase result shape."""
    def win(w):
        if not isinstance(w, dict):
            return None
        return {"usedPercent": w.get("used_percent"), "windowDurationMins": w.get("window_minutes"),
                "resetsAt": w.get("resets_at")}
    c = rl.get("credits")
    snap = {"primary": win(rl.get("primary")), "secondary": win(rl.get("secondary")), "planType": rl.get("plan_type"),
            "rateLimitReachedType": rl.get("rate_limit_reached_type"),
            "spendControlReached": rl.get("spend_control_reached"),
            "credits": {"hasCredits": c.get("has_credits"), "unlimited": c.get("unlimited"),
                        "balance": c.get("balance")} if isinstance(c, dict) else None}
    return {"rateLimits": snap}


# ------------------------------------------------------------------ reading (subprocesses)

def fetch_claude(acct, timeout=30, claude=None, cwd=None, run=subprocess.run):
    ok, why = accounts.account_ready(acct)
    if not ok:
        raise UsageError(why)
    if claude is None:
        claude = registry.claude_bin()
    if not claude:
        raise UsageError("claude binary not found")
    try:
        resp = claude_session.claude_control(claude, acct, {"subtype": "get_usage", "skip_behaviors": True}, timeout, cwd, run)
    except subprocess.TimeoutExpired:
        raise UsageError("claude usage read timed out after %ds" % timeout)
    except claude_session.ControlError as e:
        raise UsageError(str(e))
    return parse_claude(resp)


def fetch_codex_live(timeout=30, codex=None):
    if codex is None:
        codex = registry.codex_bin()
    if not codex:
        raise UsageError("codex binary not found")
    p = subprocess.Popen([codex, "app-server", "--listen", "stdio://"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
    lines = queue.Queue()

    def pump():  # a thread, not select(): select cannot see lines already sitting in the pipe's read buffer
        for line in p.stdout:
            lines.put(line)
        lines.put(None)
    threading.Thread(target=pump, daemon=True).start()
    try:
        def send(o):
            p.stdin.write(json.dumps(o) + "\n")
            p.stdin.flush()
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agent-usage", "version": "1"}}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = lines.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if line is None:
                break
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("id") == 1:
                if "error" in d:
                    raise UsageError("codex initialize: %s" % str(d["error"])[:200])
                send({"method": "initialized"})
                send({"id": 2, "method": "account/rateLimits/read"})
            elif d.get("id") == 2:
                if "error" in d:
                    raise UsageError("codex: %s" % str((d["error"] or {}).get("message") or d["error"])[:200])
                return parse_codex(d.get("result") or {})
        raise UsageError("codex app-server gave no rate limits within %ds" % timeout)
    finally:
        try:
            p.stdin.close()
            p.terminate()
            p.wait(5)
        except Exception:
            p.kill()
        try:
            p.stdout.close()
        except Exception:
            pass


def codex_from_rollouts(home=None, max_files=40):
    """(parsed, event_ts): the newest rate_limits any Codex session logged. Reads only the tails of recent logs."""
    root = Path(home or registry.CODEX_HOME) / "sessions"
    files = []
    for dirpath, dirs, names in os.walk(str(root)):
        files += [os.path.join(dirpath, n) for n in names if n.startswith("rollout-") and n.endswith(".jsonl")]
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    for f in files[:max_files]:
        try:
            lines = jsonfile.read_tail(f, 512 << 10)
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            rl = (d.get("payload") or {}).get("rate_limits")
            if isinstance(rl, dict):
                return parse_codex(codex_rollout_to_result(rl)), iso_epoch(d.get("timestamp")) or os.path.getmtime(f)
    raise UsageError("no rate_limits in recent Codex session logs")


def fetch_codex(timeout=30):
    try:
        return dict(fetch_codex_live(timeout), source="server")
    except (UsageError, OSError) as live_err:
        try:
            parsed, ts = codex_from_rollouts()
        except (UsageError, OSError):
            raise UsageError(str(live_err))
        return dict(parsed, source="session-log", observed_at=ts, note="live read failed: %s" % live_err)


# ------------------------------------------------------------------ snapshots + cache
def label_for(target, s):
    if target == CODEX:
        return s["codex_label"]
    return accounts.account(s, account_of(target)).get("label") or target


def read_one(target, s):
    t0 = time.time()
    if target == CODEX:
        data = fetch_codex(s["timeout"])
    else:
        data = dict(fetch_claude(accounts.account(s, account_of(target)), s["timeout"], cwd=str(config.STATE_DIR)
                                 if config.STATE_DIR.is_dir() else None), source="server")
    data.setdefault("observed_at", time.time())
    data["read_seconds"] = round(time.time() - t0, 1)
    return data


def snapshots(cfg, refresh=False, reader=None, cache_file=None, now=None):
    """{target: snapshot}. Fresh cache entries are reused; the rest are read in parallel and written to the cache
    file. A cache file that is not JSON raises (jsonfile.read); it is only a cache, so the operator deletes it. One
    that cannot be written raises OSError."""
    s = settings(cfg)
    reader = reader or read_one
    now = time.time() if now is None else now
    cache_file = cache_file or config.STATE_DIR / CACHE_NAME
    cache = jsonfile.read(cache_file)
    out, todo = {}, []
    for t in targets(s):
        c = cache.get(t)
        if c and not refresh and now - c.get("observed_at", 0) < s["cache_seconds"]:
            out[t] = dict(c, cached=True)
        else:
            todo.append(t)

    def one(t):
        try:
            return t, reader(t, s), None
        except Exception as e:  # one account failing must not hide the others
            return t, None, str(e) or e.__class__.__name__

    if todo:
        with ThreadPoolExecutor(max_workers=len(todo)) as ex:
            results = list(ex.map(one, todo))
        changed = False
        for t, data, err in results:
            if data is not None:
                cache[t] = data
                changed = True
                out[t] = dict(data, cached=False)
            else:
                last = cache.get(t)
                if last and now - last.get("observed_at", 0) < s["stale_seconds"]:
                    out[t] = dict(last, cached=True, stale=True, error=err)
                else:
                    out[t] = {"windows": [], "error": err}
        if changed:
            jsonfile.write(cache_file, cache, jsonfile.PRIVATE, 1)
    for t in out:
        cli = CODEX if t == CODEX else "claude"
        out[t].update(target=t, cli=cli, account=account_of(t), label=label_for(t, s))
    return out


# ------------------------------------------------------------------ deciding
def fmt_reset(ts, now=None):
    """A time as its local time of day, with the weekday when it is 20 hours or more from now; "unknown" for none."""
    if not ts:
        return "unknown"
    now = time.time() if now is None else now
    lt = time.localtime(ts)
    if abs(ts - now) < 20 * 3600:
        return time.strftime("%H:%M", lt)
    return time.strftime("%a %H:%M", lt)


def counted(w, model):
    if w["kind"] in ("session", "weekly"):
        return True
    return bool(model and w.get("model") and w["model"] in model.lower())


def used_up(snap, model):
    """The windows of this snapshot counted for `model` (counted) with nothing left."""
    return [w for w in snap.get("windows") or [] if counted(w, model) and w["remaining_pct"] <= 0]


def assess(snap, s, model=None, now=None):
    """-> {"status": ok|low|out|unknown, "headroom": %, "why": [...]}; why says what keeps it from ok."""
    now = time.time() if now is None else now
    if not snap.get("windows"):
        return {"status": "unknown", "headroom": None, "why": ["usage unknown: %s" % (snap.get("error") or "no data")]}
    acct = accounts.account(s, snap.get("account")) if snap.get("cli") == "claude" else {}
    thr = dict(s["min_remaining"], **(acct.get("min_remaining") or {}))
    status, why, rems, gone = "ok", [], [], used_up(snap, model)
    for w in snap["windows"]:
        if not counted(w, model):
            continue
        rem = w["remaining_pct"]
        rems.append(rem)
        base = "session" if w["kind"].endswith("session") else "weekly"
        if w in gone:
            status = "out"
            why.append("%s window used up until %s" % (w["name"], fmt_reset(w.get("resets_at"), now)))
        elif rem < thr.get(base, 0):
            status = "low" if status == "ok" else status
            why.append("%s %d%% left (under %d%%) until %s" % (w["name"], rem, thr[base], fmt_reset(w.get("resets_at"), now)))
    for b in snap.get("blocked") or []:
        status = "out"
        why.append(b)
    eu = snap.get("extra_usage") or {}
    if status == "out" and eu.get("enabled"):
        why.append("extra usage is on, so new work bills credits" + (" (spend limit reached)" if eu.get("limit_reached") else ""))
    if snap.get("stale"):
        why.append("reading is %d min old (%s)" % ((now - snap.get("observed_at", now)) / 60, snap.get("error")))
    elif snap.get("source") == "session-log":
        why.append("from a Codex session log %d min old" % ((now - snap.get("observed_at", now)) / 60))
    return {"status": status, "headroom": min(rems) if rems else None, "why": why}


def summary(snap, model=None):
    parts = ["%s %d%% left" % (w["name"], w["remaining_pct"]) for w in snap.get("windows", []) if counted(w, model)]
    return ", ".join(parts) or "no counted windows"


def last_resort(snap, model, claude_models):
    """Whether this Claude login may take an agent on `model` once no place has headroom, billing its extra usage:
    `model` is the last of claude_models (the models the agent may run, best first), and extra usage is on there
    and under its spend limit."""
    eu = snap.get("extra_usage") or {}
    return model == claude_models[-1] and bool(eu.get("enabled")) and not eu.get("limit_reached")


def recommend(snaps, cfg, models=None, owner=None, only_cli=None, now=None):
    """Pick a target and the model the agent runs there. models: {"claude": model id, "codex": model id} the agent
    would run (for per-model windows); None: config's, where a Claude agent may run each of
    config.claude_model_order in turn. Places are tried in `places` order: the first with headroom wins; with none,
    the low one with most left; then the first whose usage is unknown; then the first last_resort allows; then the
    first one used up."""
    s = settings(cfg)
    claude_models = config.claude_model_order(cfg) if models is None else [models.get("claude")]
    codex_model = (config.spawn_models(cfg) if models is None else models).get("codex")
    cands = []
    for t, model in places(s, claude_models, codex_model):
        cli = CODEX if t == CODEX else "claude"
        if only_cli and cli != only_cli:
            continue
        snap = snaps.get(t) or {"target": t, "cli": cli, "windows": [], "error": "not read"}
        a = assess(snap, s, model, now)
        if cli == "claude":
            acct = accounts.account(s, account_of(t))
            allowed, reason = accounts.owner_allowed(acct, owner)
            ready, why_not = accounts.account_ready(acct)
            if not allowed:
                a = {"status": "excluded", "headroom": a["headroom"], "why": [reason]}
            elif not ready:   # never route to a login that does not exist yet
                a = {"status": "unavailable", "headroom": None, "why": [why_not]}
        cands.append(dict(a, target=t, cli=cli, account=account_of(t), model=model,
                          bills=cli == "claude" and last_resort(snap, model, claude_models),
                          summary=summary(snap, model)))
    use, why = None, ""
    by = {st: [c for c in cands if c["status"] == st] for st in ("ok", "low", "unknown", "out")}
    billing = [c for c in by["out"] if c["bills"]]
    if by["ok"]:
        use = by["ok"][0]
        why = use["summary"]
    elif by["low"]:
        use = max(by["low"], key=lambda c: c["headroom"] or 0)
        why = "every account is low; this one has the most left (%s)" % use["summary"]
    elif by["unknown"]:
        use = by["unknown"][0]
        why = "no account has known headroom; usage of this one is unknown (%s)" % "; ".join(use["why"])
    elif billing:
        use = billing[0]
        why = "no account has headroom; this one bills its extra usage (%s)" % "; ".join(use["why"])
    elif by["out"]:
        use = by["out"][0]
        why = "every account is used up; nothing better to pick (%s)" % "; ".join(use["why"])

    def place(c):   # a Claude place names its model when there are several to choose from
        return "%s on %s" % (c["target"], c["model"]) if c["cli"] == "claude" and len(claude_models) > 1 else c["target"]
    avoid = [{"target": c["target"], "model": c["model"], "place": place(c), "status": c["status"],
              "why": "; ".join(c["why"])}
             for c in cands if c is not use and c["status"] in ("out", "low", "excluded", "unavailable")]
    rec = {"use": use["target"] if use else None, "cli": use["cli"] if use else None,
           "account": use["account"] if use else None, "model": use["model"] if use else None,
           "place": place(use) if use else None, "why": why, "avoid": avoid, "candidates": cands, "owner": owner}
    rec["line"] = recommendation_line(rec)
    return rec


def recommendation_line(rec):
    if not rec["use"] and rec["avoid"]:
        return "No usable account: %s." % "; ".join("%s (%s)" % (a["place"], a["why"]) for a in rec["avoid"])
    if not rec["use"]:
        return "No account to recommend (nothing configured for this CLI)."
    line = "Use %s: %s." % (rec["place"], rec["why"])
    if rec["avoid"]:
        line += " Avoid " + "; ".join("%s (%s)" % (a["place"], a["why"]) for a in rec["avoid"]) + "."
    return line


# ------------------------------------------------------------------ text report
def money(v, cur):
    return ("$%.2f" % v) if cur == "USD" else "%.2f %s" % (v, cur)


def extra_spend(eu):
    """A login's extra-usage spend this month, from its snapshot's extra_usage: "$211.41 used, no monthly cap" or
    "$211.41 used of a $500.00 monthly cap"."""
    used = money(eu["used"], eu["currency"]) if eu.get("used") is not None else "?"
    if eu.get("monthly_limit"):
        return "%s used of a %s monthly cap" % (used, money(eu["monthly_limit"], eu["currency"]))
    return "%s used, no monthly cap" % used


def render_text(snaps, rec, cfg, now=None):
    now = time.time() if now is None else now
    s = settings(cfg)
    models = config.spawn_models(cfg)
    lines = ["Plan usage, percent REMAINING (server-reported; read by each CLI itself, cached up to %d s)" % s["cache_seconds"], ""]
    for t in targets(s):
        snap = snaps.get(t)
        if not snap:
            continue
        src = snap.get("source") or "-"
        if snap.get("stale"):
            src = "STALE %s from %s" % (src, time.strftime("%H:%M", time.localtime(snap.get("observed_at", now))))
        elif src == "session-log":
            src = "Codex session log from %s" % time.strftime("%H:%M", time.localtime(snap.get("observed_at", now)))
        elif snap.get("cached"):
            src = "server, cached %ds ago" % (now - snap.get("observed_at", now))
        plan = snap.get("plan")
        lines.append("%-18s %s%s  [%s]" % (t, snap.get("label") or "", (" (%s plan)" % plan) if plan else "", src))
        if not snap.get("windows"):
            lines.append("    usage unknown: %s" % (snap.get("error") or "no data"))
        for w in snap.get("windows", []):
            note = "" if counted(w, models.get(snap["cli"])) else "   (not counted: agents run %s)" % (models.get(snap["cli"]) or "another model")
            lines.append("    %-16s %3d%% left   resets %s%s" % (w["name"], w["remaining_pct"], fmt_reset(w.get("resets_at"), now), note))
        eu = snap.get("extra_usage")
        if eu:
            if eu["enabled"]:
                lines.append("    extra usage ON: %s%s" % (extra_spend(eu), " (LIMIT REACHED)" if eu.get("limit_reached") else ""))
            else:
                lines.append("    extra usage off")
        cr = snap.get("credits")
        if cr and (cr.get("has") or cr.get("unlimited")):
            lines.append("    credits: %s" % ("unlimited" if cr.get("unlimited") else "balance %s" % cr.get("balance")))
        if snap.get("reset_credits"):
            lines.append("    %d free limit reset(s) available" % snap["reset_credits"])
        if snap.get("error") and snap.get("windows"):
            lines.append("    last read failed: %s" % snap["error"])
        lines.append("")
    lines.append("Recommendation: " + rec["line"])
    return "\n".join(lines)
