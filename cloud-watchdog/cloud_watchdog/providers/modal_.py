"""Modal: app list, live containers, system logs (crash loops), deploy history (owner), billing (cost),
and who created each billed app (spend attribution, see spend.py).

Runs inside the credentialed child. For a toml-profile account (`profile = ...`) the MODAL_TOKEN_* names are
absent from the environment by construction, and MODAL_PROFILE pins the profile, so Modal's silent
env-over-toml precedence cannot switch accounts under us.
"""
import datetime as dt
import json
import os
import re
import shutil
from pathlib import Path

from .. import util
from . import clean

# System-log lines that mean a container died. Preemption and normal scale-down are not failures.
FAILURE_RE = re.compile(r"Runner failed|exited with (?:exit )?code [1-9]|[Oo]ut of [Mm]emory|\bOOM\b|"
                        r"Runner terminated unexpectedly|crashed|Container failed", re.I)
NOT_FAILURE_RE = re.compile(r"preempt", re.I)
ACTIVE_STATES = {"ephemeral", "ephemeral (detached)", "initializing...", "stopping...", "deployed"}
API_HELPER = Path(__file__).resolve().parent / "modal_api.py"
MAX_LOOKUPS = 500          # creator lookups per pass; the rest wait for the next pass


def _env(acct):
    env = dict(os.environ)
    if acct.get("profile"):
        env.pop("MODAL_TOKEN_ID", None)
        env.pop("MODAL_TOKEN_SECRET", None)
        env["MODAL_PROFILE"] = acct["profile"]
    return env


def _modal(acct, args, timeout=120):
    code, out, err = util.run(["modal"] + args, env=_env(acct), timeout=timeout)
    if code != 0:
        raise RuntimeError("modal %s failed (%s): %s" % (args[0:2], code, (err or out).strip()[-300:]))
    return out


def _log_time(line):
    parts = line.split(" ", 3)
    if len(parts) < 3:
        return None
    return util.parse_time(parts[0] + "T" + parts[1])


def crash_check(acct, app_id, window_min, live_containers=(), at=None):
    """Count containers that failed in the window, against all containers seen in it.
    `at` (ISO time) evaluates a past window instead, for backtests."""
    if at:
        end = util.parse_time(at)
        rng = ["--since", util.iso(end - window_min * 60).replace(" UTC", "Z").replace(" ", "T"),
               "--until", util.iso(end).replace(" UTC", "Z").replace(" ", "T")]
    else:
        rng = ["--since", "%dm" % int(window_min)]
    sysout = _modal(acct, ["app", "logs", app_id] + rng + ["-s", "system", "--show-container-id",
                                                           "--timestamps"], timeout=120)
    failed, last_failure = set(), None
    for line in sysout.splitlines():
        m = re.search(r"\b(ta-[A-Za-z0-9]+)\b", line)
        if m and FAILURE_RE.search(line) and not NOT_FAILURE_RE.search(line):
            failed.add(m.group(1))
            t = _log_time(line)
            if t and (last_failure is None or t > last_failure):
                last_failure = t
    seen = set(failed) | set(live_containers)
    allout = _modal(acct, ["app", "logs", app_id] + rng + ["--show-container-id", "--tail", "5000"], timeout=120)
    seen |= set(re.findall(r"\b(ta-[A-Za-z0-9]+)\b", allout))
    sample = next((l.split(" ", 3)[-1][:160] for l in sysout.splitlines() if FAILURE_RE.search(l)), "")
    return {"failed": len(failed), "seen": len(seen), "window_min": window_min,
            "last_failure_at": last_failure, "sample": sample}


def _history(acct, app_id):
    rows = util.json_tail(_modal(acct, ["app", "history", app_id, "--json"], timeout=60))
    if not rows:
        return {}
    latest = max(rows, key=lambda r: int(str(r.get("version", "v0")).lstrip("v") or 0))
    return {"deployed_by": latest.get("deployed_by"), "git_commit": (latest.get("commit") or "").rstrip("*") or None,
            "deployed_at": util.parse_time(latest.get("time_deployed"))}


def _billing(acct, now):
    """Per-app cost: daily rows for 31 days (before today) + hourly rows since today 00:00 UTC.
    Also month-to-date cost per app ({app_id: [name, usd]}) and the end of the last full hour billed."""
    today0 = dt.datetime.fromtimestamp(now, util.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = today0.replace(day=1).timestamp()
    start_d = (today0 - dt.timedelta(days=31)).strftime("%Y-%m-%d")
    start_h = (dt.datetime.fromtimestamp(now - 48 * 3600, util.UTC)).strftime("%Y-%m-%dT%H:00:00")
    daily = util.json_tail(_modal(acct, ["billing", "report", "--start", start_d, "-r", "d", "--json"], timeout=180))
    hourly = util.json_tail(_modal(acct, ["billing", "report", "--start", start_h, "-r", "h", "--json"], timeout=180))
    per_app, month, through = {}, {}, None
    t0 = today0.timestamp()

    def add_month(r, c):
        m = month.setdefault(r["object_id"], [r.get("description") or r["object_id"], 0.0])
        m[1] += c

    for r in daily:
        t = util.parse_time(r["interval_start"])
        if t is not None and t < t0:
            per_app.setdefault(r["object_id"], []).append((t, float(r["cost"])))
            if t >= month0:
                add_month(r, float(r["cost"]))
    latest, day_spend = {}, 0.0
    for r in hourly:
        t = util.parse_time(r["interval_start"])
        c = float(r["cost"])
        if t is None:
            continue
        if t >= t0:
            per_app.setdefault(r["object_id"], []).append((t, c))
            add_month(r, c)
        if t >= now - 24 * 3600 - 3600:
            day_spend += c
        if r["object_id"] not in latest or t > latest[r["object_id"]][0]:
            latest[r["object_id"]] = (t, c)
        through = max(through or 0, t + 3600)
    summary = util.json_tail(_modal(acct, ["billing", "summary", "--json"], timeout=60), "{")
    volumes = float((summary.get("metered_cost_breakdown") or {}).get("volumes") or 0)
    metrics = {"day_spend_usd": round(day_spend, 2), "month_volumes_usd": round(volumes, 2),
               "month_metered_usd": round(float(summary.get("metered_cost") or 0), 2)}
    spend = {"month": {a: [n, round(c, 4)] for a, (n, c) in month.items()}, "through": through}
    return per_app, latest, metrics, spend


def modal_python(override=None):
    """The interpreter the `modal` CLI runs on (from its shebang): it can import `modal`, ours may not."""
    if override:
        return override
    path = shutil.which("modal")
    if not path:
        return None
    with open(path, "rb") as f:
        first = f.readline().decode(errors="replace").strip()
    parts = first[2:].split() if first.startswith("#!") else []
    if len(parts) > 1 and parts[0].endswith("/env"):
        return shutil.which(parts[1])
    return parts[0] if parts else None


def _creators(acct, app_ids, python):
    """Who created / last deployed each app (Modal's AppGetLifecycle), and whose token this is."""
    if not python:
        raise RuntimeError("cannot find the Python the modal CLI runs on")
    code, out, err = util.run([python, "-I", str(API_HELPER)], env=_env(acct), timeout=300,
                              input_text=json.dumps({"app_ids": list(app_ids)}))
    if code != 0:
        raise RuntimeError("modal_api failed (%s): %s" % (code, (err or out).strip()[-200:]))
    return util.json_tail(out, "{")


def _volume_creators(acct):
    rows = util.json_tail(_modal(acct, ["volume", "list", "--json"], timeout=120))
    by = {}
    for v in rows:
        who = v.get("created_by") or "?"
        by[who] = by.get(who, 0) + 1
    return by


def _attribution(acct, opts, spend):
    """Month-to-date cost per app, plus creators of the billed apps the parent does not know yet.
    Read-only. A failure here never fails the collection: the digest then says attribution is unavailable."""
    known = set(opts.get("known_creators") or [])
    todo = [a for a, (_, usd) in sorted(spend["month"].items(), key=lambda kv: -kv[1][1])
            if usd > 0 and a not in known][:MAX_LOOKUPS]
    try:
        res = _creators(acct, todo, modal_python(opts.get("modal_python")))
        spend["creators"] = res.get("apps") or {}
        spend["token_user"] = res.get("token_user")
        spend["lookup_errors"] = len(res.get("errors") or {})
    except (RuntimeError, ValueError, OSError) as e:
        spend["error"] = str(e)[:200]
    try:
        spend["volumes_by"] = _volume_creators(acct)
    except (RuntimeError, ValueError):
        pass
    return spend


def collect(acct, opts):
    now = opts["now"]
    apps = util.json_tail(_modal(acct, ["app", "list", "--json"]))
    conts = util.json_tail(_modal(acct, ["container", "list", "--json"]))
    by_app = {}
    for c in conts:
        by_app.setdefault(c.get("app_id"), []).append(c)
    billing, latest, metrics, spend = ({}, {}, {}, None)
    if opts.get("slow"):
        billing, latest, metrics, spend = _billing(acct, now)
        spend = _attribution(acct, opts, spend)
    watch = set(opts.get("watch") or [])
    known_history = opts.get("known_history") or {}
    out = []
    for a in apps:
        app_id, state = a.get("app_id"), (a.get("state") or "").strip()
        if state in ("stopped",):
            continue
        tasks = int(a.get("tasks") or 0)
        live = by_app.get(app_id, [])
        launched = util.parse_time(a.get("created_at"))
        r = {"native_id": app_id, "name": a.get("description"), "kind": "app", "state": state,
             "launched_at": launched, "tasks": tasks, "containers": len(live)}
        starts = [util.parse_time(c.get("start_time")) for c in live]
        starts = [s for s in starts if s]
        r["max_container_age_s"] = (now - min(starts)) if starts else None
        rows = billing.get(app_id)
        if rows is not None:
            since = (launched or 0) - 86400
            r["cost_usd"] = round(sum(c for t, c in rows if t >= since), 2)
            r["cost_source"] = "billing (31 d max)"
        if app_id in latest:
            t, c = latest[app_id]
            r["rate_usd_h"] = round(c, 2) if t >= now - 4 * 3600 else 0.0
            r["rate_source"] = "billing, last full hour"
        interesting = tasks > 0 or live or app_id in watch or state != "deployed" or (r.get("cost_usd") or 0) > 0
        if not interesting:
            out.append({"native_id": app_id, "name": r["name"], "kind": "app", "state": state, "tasks": 0,
                        "containers": 0, "launched_at": launched, "dormant": True})
            continue
        if tasks > 0 or live or app_id in watch:
            try:
                r["health_detail"] = crash_check(acct, app_id, opts["crash_window_min"],
                                                 [c.get("container_id") for c in live])
            except RuntimeError as e:
                r["health_detail"] = {"error": str(e)[:200]}
        if app_id in known_history:
            r.update(known_history[app_id])
        else:
            try:
                r.update(_history(acct, app_id))
            except RuntimeError:
                pass
        out.append(clean(r) | ({"dormant": False}))
    _fill_creators(acct, opts, out)
    return {"resources": out, "metrics": metrics} | ({"spend": spend} if spend else {})


def _fill_creators(acct, opts, resources):
    """Give each tracked app that has no deployer (a `modal run` app) its creator, for ownership.
    From the parent's cache first; the rest in one AppGetLifecycle batch. A failed lookup leaves them unknown."""
    known = opts.get("known_creator_of") or {}
    todo = []
    for r in resources:
        if r.get("dormant") or r.get("deployed_by"):
            continue
        if known.get(r["native_id"]):
            r["created_by"] = known[r["native_id"]]
        else:
            todo.append(r)
    if not todo:
        return
    try:
        found = _creators(acct, [r["native_id"] for r in todo], modal_python(opts.get("modal_python"))).get("apps") or {}
    except (RuntimeError, ValueError, OSError):
        return
    for r in todo:
        who = (found.get(r["native_id"]) or {}).get("created_by")
        if who:
            r["created_by"] = who


def stop(acct, app_id):
    """`modal app stop`: containers stop; the deployment can be redeployed; volumes, secrets and images stay."""
    code, out, err = util.run(["modal", "app", "stop", app_id], env=_env(acct), timeout=120)
    return code == 0, (out + err).strip()[-300:]
