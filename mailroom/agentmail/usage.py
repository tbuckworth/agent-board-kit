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

Routing: accounts are tried in `order`; the first whose counted windows all keep at least `min_remaining` percent
wins. Counted windows are the 5-hour/session and weekly ones, plus a per-model weekly window only when it is for
the model the agent would run. Settings live under config key "usage"; DEFAULTS below.
"""
import datetime
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULTS = {
    # Claude accounts, one config dir each. config_dir None is the default login (~/.claude, CLAUDE_CONFIG_DIR
    # unset). exclude_owners / only_owners: GitHub owners (from the repo's origin) this account must not / may
    # only be used for, e.g. keep employer repos off a personal plan. min_remaining overrides per account.
    "claude_accounts": {"default": {"config_dir": None, "label": "Claude (default login)"}},
    "codex_label": "Codex",
    # Preference order: the first target with headroom wins. None means every Claude account, then codex.
    "order": None,
    # Percent that must REMAIN in each counted window; below it the account is "low" and skipped.
    "min_remaining": {"session": 10, "weekly": 5},
    "cache_seconds": 120,
    "stale_seconds": 6 * 3600,   # a failed read falls back to the last good reading up to this old
    "timeout": 30,
}
CODEX = "codex"
CACHE_NAME = "usage-cache.json"
# Never hand these to the probe: each would make Claude Code read some other credential than the account's own.
CLAUDE_ENV_DROP = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                   "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                   "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SESSION_ID")


class UsageError(Exception):
    pass


def settings(cfg):
    s = dict(DEFAULTS, **(cfg.get("usage") or {}))
    s["min_remaining"] = dict(DEFAULTS["min_remaining"], **(s.get("min_remaining") or {}))
    if not s.get("claude_accounts"):
        s["claude_accounts"] = dict(DEFAULTS["claude_accounts"])
    if not s.get("order"):
        s["order"] = ["claude:" + n for n in s["claude_accounts"]] + [CODEX]
    return s


def expand(p):
    return str(Path(os.path.expanduser(p)).resolve()) if p else None


# ------------------------------------------------------------------ targets and accounts
def ranked(s):
    """The targets routing may pick, in preference order (unknown names in `order` are ignored)."""
    return [t for t in s["order"] if t == CODEX or (t.startswith("claude:") and t[7:] in s["claude_accounts"])]


def targets(s):
    """Every target agent-usage reads and shows: the ranked ones, then any account left out of `order`."""
    out = ranked(s)
    out += ["claude:" + n for n in s["claude_accounts"] if "claude:" + n not in out]
    return out + ([CODEX] if CODEX not in out else [])


def parse_target(text, s):
    """'work', 'claude:work', 'codex' -> ('claude', 'work') / ('codex', None). ValueError if unknown."""
    t = (text or "").strip()
    if t in (CODEX, "codex:default"):
        return CODEX, None
    name = t[len("claude:"):] if t.startswith("claude:") else t
    if name in s["claude_accounts"]:
        return "claude", name
    raise ValueError("unknown account %r (Claude accounts: %s; or codex)"
                     % (text, ", ".join(sorted(s["claude_accounts"])) or "none"))


def account(s, name):
    return dict(s["claude_accounts"].get(name) or {})


def default_account(s):
    """The account whose config dir is the default ~/.claude, i.e. what `claude` uses with nothing set."""
    for n, a in s["claude_accounts"].items():
        if not (a or {}).get("config_dir"):
            return n
    return None


def account_ready(acct, platform=None):
    """(ok, why). Existence checks only; no credential file is opened. macOS keeps the login in the Keychain,
    so there only the config dir is checked."""
    d = expand(acct.get("config_dir"))
    if not d:
        return True, ""
    if not os.path.isdir(d):
        return False, "config dir %s does not exist (set it up with agent-usage init-account)" % d
    if (platform or sys.platform) != "darwin" and not os.path.exists(os.path.join(d, ".credentials.json")):
        return False, "no login in %s yet: run CLAUDE_CONFIG_DIR=%s claude, then /login" % (d, d)
    return True, ""


def owner_allowed(acct, owner):
    if not owner:
        return True, ""
    low = owner.lower()
    if low in [o.lower() for o in acct.get("exclude_owners") or []]:
        return False, "repos owned by %s are excluded from this account" % owner
    only = [o.lower() for o in acct.get("only_owners") or []]
    if only and low not in only:
        return False, "this account is only for repos owned by %s" % ", ".join(acct["only_owners"])
    return True, ""


def repo_owner(path):
    """GitHub owner of the repo's origin remote, or None."""
    try:
        url = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"github\.com[:/]+([^/]+)/", url)
    return m.group(1) if m else None


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
def claude_env(acct, base=None):
    env = dict(os.environ if base is None else base)
    for k in CLAUDE_ENV_DROP:
        env.pop(k, None)
    d = expand(acct.get("config_dir"))
    if d:
        env["CLAUDE_CONFIG_DIR"] = d
    return env


def claude_probe_cmd(claude):
    return [claude, "-p", "--safe-mode", "--no-session-persistence", "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose"]


def fetch_claude(acct, timeout=30, claude=None, cwd=None, run=subprocess.run):
    ok, why = account_ready(acct)
    if not ok:
        raise UsageError(why)
    if claude is None:
        from . import registry
        claude = registry.claude_bin()
    if not claude:
        raise UsageError("claude binary not found")
    req = {"type": "control_request", "request_id": "agent-usage",
           "request": {"subtype": "get_usage", "skip_behaviors": True}}
    try:
        r = run(claude_probe_cmd(claude), input=json.dumps(req) + "\n", capture_output=True, text=True,
                timeout=timeout, env=claude_env(acct), cwd=cwd)
    except subprocess.TimeoutExpired:
        raise UsageError("claude usage read timed out after %ds" % timeout)
    for line in (r.stdout or "").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "control_response":
            continue
        resp = d.get("response") or {}
        if resp.get("subtype") == "error":
            raise UsageError("claude: %s" % str(resp.get("error"))[:200])
        return parse_claude(resp.get("response") or {})
    raise UsageError("claude gave no usage answer (exit %s): %s" % (r.returncode, (r.stderr or r.stdout or "").strip()[:200]))


def fetch_codex_live(timeout=30, codex=None, popen=subprocess.Popen):
    if codex is None:
        from . import registry
        codex = registry.codex_bin()
    if not codex:
        raise UsageError("codex binary not found")
    p = popen([codex, "app-server", "--listen", "stdio://"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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


def codex_home():
    from . import registry
    return registry.CODEX_HOME


def codex_from_rollouts(home=None, max_files=40):
    """(parsed, event_ts): the newest rate_limits any Codex session logged. Reads only the tails of recent logs."""
    root = Path(home or codex_home()) / "sessions"
    files = []
    for dirpath, dirs, names in os.walk(str(root)):
        files += [os.path.join(dirpath, n) for n in names if n.startswith("rollout-") and n.endswith(".jsonl")]
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    for f in files[:max_files]:
        try:
            with open(f, "rb") as fh:
                fh.seek(max(0, os.path.getsize(f) - (512 << 10)))
                lines = fh.read().decode("utf-8", "replace").splitlines()
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
def cache_path():
    from . import config
    return config.STATE_DIR / CACHE_NAME


def load_cache(path=None):
    try:
        return json.loads(Path(path or cache_path()).read_text())
    except (OSError, ValueError):
        return {}


def save_cache(data, path=None):
    path = Path(path or cache_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.%d" % os.getpid())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def label_for(target, s):
    if target == CODEX:
        return s.get("codex_label") or "Codex"
    return account(s, target.split(":", 1)[1]).get("label") or target


def read_one(target, s):
    t0 = time.time()
    if target == CODEX:
        data = fetch_codex(s["timeout"])
    else:
        from . import config
        data = dict(fetch_claude(account(s, target.split(":", 1)[1]), s["timeout"], cwd=str(config.STATE_DIR)
                                 if config.STATE_DIR.is_dir() else None), source="server")
    data.setdefault("observed_at", time.time())
    data["read_seconds"] = round(time.time() - t0, 1)
    return data


def snapshots(cfg, refresh=False, only=None, reader=None, cache_file=None, now=None):
    """{target: snapshot}. Fresh cache entries are reused; the rest are read in parallel."""
    s = settings(cfg)
    reader = reader or read_one
    now = time.time() if now is None else now
    wanted = [t for t in targets(s) if not only or t == only or t.startswith(only + ":")]
    cache = load_cache(cache_file)
    out, todo = {}, []
    for t in wanted:
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
            try:
                save_cache(cache, cache_file)
            except OSError:
                pass
    for t in out:
        cli = CODEX if t == CODEX else "claude"
        out[t].update(target=t, cli=cli, account=None if cli == CODEX else t.split(":", 1)[1], label=label_for(t, s))
    return out


# ------------------------------------------------------------------ deciding
def fmt_reset(ts, now=None):
    if not ts:
        return "unknown"
    now = time.time() if now is None else now
    lt = time.localtime(ts)
    if ts - now < 20 * 3600:
        return time.strftime("%H:%M", lt)
    return time.strftime("%a %H:%M", lt)


def counted(w, model):
    if w["kind"] in ("session", "weekly"):
        return True
    return bool(model and w.get("model") and w["model"] in model.lower())


def assess(snap, s, model=None, now=None):
    """-> {"status": ok|low|out|unknown, "headroom": %, "why": [...]}; why says what keeps it from ok."""
    now = time.time() if now is None else now
    if not snap.get("windows"):
        return {"status": "unknown", "headroom": None, "why": ["usage unknown: %s" % (snap.get("error") or "no data")]}
    acct = account(s, snap.get("account")) if snap.get("cli") == "claude" else {}
    thr = dict(s["min_remaining"], **(acct.get("min_remaining") or {}))
    status, why, rems = "ok", [], []
    for w in snap["windows"]:
        if not counted(w, model):
            continue
        rem = w["remaining_pct"]
        rems.append(rem)
        base = "session" if w["kind"].endswith("session") else "weekly"
        if rem <= 0:
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


def recommend(snaps, cfg, models=None, owner=None, only_cli=None, now=None):
    """Pick a target. models: {"claude": model id, "codex": model id} the agent would run (for per-model windows)."""
    s = settings(cfg)
    models = models or {"claude": cfg.get("spawn_claude_model"), "codex": cfg.get("spawn_codex_model")}
    cands = []
    for t in ranked(s):
        cli = CODEX if t == CODEX else "claude"
        if only_cli and cli != only_cli:
            continue
        snap = snaps.get(t) or {"target": t, "cli": cli, "windows": [], "error": "not read"}
        a = assess(snap, s, models.get(cli), now)
        if cli == "claude":
            acct = account(s, t.split(":", 1)[1])
            allowed, reason = owner_allowed(acct, owner)
            ready, why_not = account_ready(acct)
            if not allowed:
                a = {"status": "excluded", "headroom": a["headroom"], "why": [reason]}
            elif not ready:   # never route to a login that does not exist yet
                a = {"status": "unavailable", "headroom": None, "why": [why_not]}
        cands.append(dict(a, target=t, cli=cli, account=None if cli == CODEX else t.split(":", 1)[1],
                          summary=summary(snap, models.get(cli))))
    use, why = None, ""
    by = {st: [c for c in cands if c["status"] == st] for st in ("ok", "low", "unknown", "out")}
    if by["ok"]:
        use = by["ok"][0]
        why = use["summary"]
    elif by["low"]:
        use = max(by["low"], key=lambda c: c["headroom"] or 0)
        why = "every account is low; this one has the most left (%s)" % use["summary"]
    elif by["unknown"]:
        use = by["unknown"][0]
        why = "no account has known headroom; usage of this one is unknown (%s)" % "; ".join(use["why"])
    elif by["out"]:
        use = by["out"][0]
        why = "every account is used up; nothing better to pick (%s)" % "; ".join(use["why"])
    avoid = [{"target": c["target"], "status": c["status"], "why": "; ".join(c["why"])}
             for c in cands if c is not use and c["status"] in ("out", "low", "excluded", "unavailable")]
    rec = {"use": use["target"] if use else None, "cli": use["cli"] if use else None,
           "account": use["account"] if use else None, "why": why, "avoid": avoid, "candidates": cands,
           "owner": owner}
    rec["line"] = recommendation_line(rec)
    return rec


def recommendation_line(rec):
    if not rec["use"]:
        return "No account to recommend (nothing configured for this CLI)."
    line = "Use %s: %s." % (rec["use"], rec["why"])
    if rec["avoid"]:
        line += " Avoid " + "; ".join("%s (%s)" % (a["target"], a["why"]) for a in rec["avoid"]) + "."
    return line


# ------------------------------------------------------------------ text report
def money(v, cur):
    return ("$%.2f" % v) if cur == "USD" else "%.2f %s" % (v, cur)


def render_text(snaps, rec, cfg, now=None):
    now = time.time() if now is None else now
    s = settings(cfg)
    models = {"claude": cfg.get("spawn_claude_model"), "codex": cfg.get("spawn_codex_model")}
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
                cap = ("of %s monthly cap" % money(eu["monthly_limit"], eu["currency"])) if eu.get("monthly_limit") else "no monthly cap"
                used = money(eu["used"], eu["currency"]) if eu.get("used") is not None else "?"
                lines.append("    extra usage ON: %s used, %s%s" % (used, cap, " (LIMIT REACHED)" if eu.get("limit_reached") else ""))
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


# ------------------------------------------------------------------ project memory for a second account
def project_slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def link_memory(repo_root, config_dir, default_home=None):
    """Give an agent on another account the same auto memory as the default login: symlink
    <config_dir>/projects/<slug>/memory to ~/.claude/projects/<slug>/memory. Best effort; returns the link or None."""
    slug = project_slug(Path(repo_root).resolve())
    src = Path(default_home or Path.home() / ".claude") / "projects" / slug / "memory"
    dst = Path(config_dir) / "projects" / slug / "memory"
    if not src.is_dir() or dst.exists() or dst.is_symlink():
        return None
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(src), str(dst))
        return str(dst)
    except OSError:
        return None


# ------------------------------------------------------------------ setting up a second account
# User-level customisations shared with the default login by symlink. Everything else (credentials, .claude.json,
# projects, sessions, history, caches) stays per account. Claude Code refuses a symlinked .credentials.json anyway.
SHARED = ("CLAUDE.md", "settings.json", "commands", "agents", "skills", "hooks", "plugins", "output-styles",
          "keybindings.json", "tools")


def init_account(config_dir, default_home=None, copy_mcp=(), default_json=None, dry_run=False):
    """Create a second account's config dir that shares the default login's customisations. Never touches the
    default login's files except to read them. Returns a list of what it did (or would do)."""
    home = Path(default_home or Path.home() / ".claude")
    d = Path(os.path.expanduser(config_dir)).resolve()
    if d == home.resolve():
        raise ValueError("that is the default login's own config dir")
    done = []
    if not d.exists():
        done.append("create %s (mode 700)" % d)
        if not dry_run:
            d.mkdir(mode=0o700, parents=True)
    for item in SHARED:
        src, dst = home / item, d / item
        if src.exists() and not (dst.exists() or dst.is_symlink()):
            done.append("link %s -> %s" % (dst, src))
            if not dry_run:
                os.symlink(str(src), str(dst))
    if copy_mcp:
        src_json = Path(default_json or Path.home() / ".claude.json")
        dst_json = d / ".claude.json"
        servers = (json.loads(src_json.read_text()).get("mcpServers") or {}) if src_json.exists() else {}
        missing = [n for n in copy_mcp if n not in servers]
        if missing:
            raise ValueError("no user MCP server named %s in %s" % (", ".join(missing), src_json))
        if not dst_json.exists():
            raise ValueError("%s does not exist yet: log in to the new account first, then copy MCP servers" % dst_json)
        done.append("copy user MCP servers %s into %s" % (", ".join(copy_mcp), dst_json))
        if not dry_run:
            data = json.loads(dst_json.read_text())
            data.setdefault("mcpServers", {}).update({n: servers[n] for n in copy_mcp})
            tmp = dst_json.with_suffix(".json.agent-usage.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, dst_json)
    return done
