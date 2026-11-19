"""Modal: app list, live containers, system logs (crash loops), deploy history (owner), billing (cost),
and who created each billed or `modal run` app (spend attribution, see spend.py; owner).

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
from ..spend import known_ids
from . import clean

HAS_RESOURCES = True
COUNTS_FROM_LAUNCH = False   # a deployment scales to zero and back: its age and cost count from the current stretch
ID_TERM = "%s"
STOP_EFFECT = "`modal app stop`; volumes, secrets and images are kept"
STOP_KEEPS_DATA = True

# System-log lines that mean a container died. Preemption and normal scale-down are not failures.
FAILURE_RE = re.compile(r"Runner failed|exited with (?:exit )?code [1-9]|[Oo]ut of [Mm]emory|\bOOM\b|"
                        r"Runner terminated unexpectedly|crashed|Container failed", re.I)
NOT_FAILURE_RE = re.compile(r"preempt", re.I)
API_HELPER = Path(__file__).resolve().parent / "modal_api.py"
MAX_LOOKUPS = 500          # billed apps looked up per pass; the rest wait for the next pass
BILLING_DAYS = 31          # an app's cost counts its daily billing rows over this many days before today


def billing_now(r):
    """Running containers or tasks, or a billed rate in the last full hour (as of the last slow pass)."""
    return bool((r.get("tasks") or 0) > 0 or (r.get("containers") or 0) > 0 or (r.get("rate_usd_h") or 0) > 0.01)


def ownership(r, identity):
    """Ours when Modal names one of your users (identity.owner_modal_users) as the app's latest deployer, or, never
    deployed, its creator. None when the app names nobody, or no Modal user is configured: then the owner search,
    not Modal's user field, decides whom to ask."""
    users = identity.get("owner_modal_users") or []
    if not users or not (r.get("deployed_by") or r.get("created_by")):
        return None
    return "ours" if util.launched_by(r) in users else "foreign"


def collect_options(db, cfg, account, now, slow):
    """Parent side: the crash-check window, and what the state already holds, so that collect() probes and looks up
    only what it must: the apps to crash-check even when idle ("watch": a crash streak, a crash loop or an open
    incident), the deploy history and creators already recorded, and on a slow pass the billed apps whose creator
    need not be looked up again (spend.known_ids)."""
    rows = db.q("select r.native_id, r.crash_streak, r.health, r.deployed_by, r.git_commit, r.extra, "
                "(select count(*) from incidents i where i.key=r.key and i.closed_at is null) open_inc "
                "from resources r where r.account=? and r.gone_at is null", account)
    opts = {"crash_window_min": cfg["rules"]["crash_loop"]["window_min"],
            "watch": [r["native_id"] for r in rows
                      if r["crash_streak"] or r["health"] == "crash_loop" or r["open_inc"]],
            "known_history": {r["native_id"]: {"deployed_by": r["deployed_by"], "git_commit": r["git_commit"],
                                               "deployed_at": json.loads(r["extra"] or "{}").get("deployed_at")}
                              for r in rows if r["deployed_by"]},
            "known_creator_of": {r["app_id"]: r["created_by"] for r in db.q(
                "select app_id, created_by from modal_app_owner where account=? and created_by is not null", account)}}
    if slow:
        opts["known_creators"] = known_ids(db, account, now)
    return opts


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
    """The time a `--timestamps` log line starts with, or None for a line that has none."""
    parts = line.split(" ", 3)
    if len(parts) < 3:
        return None
    try:
        return util.parse_time(parts[0] + "T" + parts[1])
    except ValueError:   # Modal prints no timestamp for a log entry that has none; the line starts with its text
        return None


def _containers_by_app(acct):
    """Live containers (`modal container list`), grouped by app id."""
    by_app = {}
    for c in util.json_tail(_modal(acct, ["container", "list", "--json"])):
        by_app.setdefault(c.get("app_id"), []).append(c)
    return by_app


def live_container_ids(acct, app_id):
    """Ids of the app's containers running now (`modal container list`)."""
    return [c.get("container_id") for c in _containers_by_app(acct).get(app_id, [])]


def crash_check(acct, app_id, window_min, live_containers, at=None):
    """Count containers that failed in the window, against all containers seen in it (`modal app logs`), the app's
    `live_containers` included. `at` (ISO time) evaluates a past window instead, for backtests."""
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
    """Per-app cost: daily rows for BILLING_DAYS days (before today) + hourly rows since today 00:00 UTC.
    Also month-to-date cost per app ({app_id: [name, usd]}) and the end of the last full hour billed."""
    today0 = dt.datetime.fromtimestamp(now, util.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = today0.replace(day=1).timestamp()
    start_d = (today0 - dt.timedelta(days=BILLING_DAYS)).strftime("%Y-%m-%d")
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
        if t < t0:
            per_app.setdefault(r["object_id"], []).append((t, float(r["cost"])))
            if t >= month0:
                add_month(r, float(r["cost"]))
    latest, day_spend = {}, 0.0
    for r in hourly:
        t = util.parse_time(r["interval_start"])
        c = float(r["cost"])
        if t >= t0:
            per_app.setdefault(r["object_id"], []).append((t, c))
            add_month(r, c)
        if t >= now - 24 * 3600 - 3600:
            day_spend += c
        if r["object_id"] not in latest or t > latest[r["object_id"]][0]:
            latest[r["object_id"]] = (t, c)
        through = max(through or 0, t + 3600)
    summary = util.json_tail(_modal(acct, ["billing", "summary", "--json"], timeout=60), "{")
    volumes = float(summary["metered_cost_breakdown"].get("volumes") or 0)
    metrics = {"day_spend_usd": round(day_spend, 2), "month_volumes_usd": round(volumes, 2),
               "month_metered_usd": round(float(summary["metered_cost"]), 2)}
    spend = {"month": {a: [n, round(c, 4)] for a, (n, c) in month.items()}, "through": through}
    return per_app, latest, metrics, spend


def modal_python():
    """The interpreter the `modal` CLI runs on (from its shebang): it can import `modal`, ours may not."""
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


def _lookup_failed(lookup, resources, error):
    """The warning for a "history" or "creators" lookup that failed for these tracked apps (up to five named)."""
    what, unknown = {"history": ("deploy history", "deployer"), "creators": ("creator", "owner")}[lookup]
    names = ", ".join(r["native_id"] if not r["name"] else "%s (%s)" % (r["name"], r["native_id"]) for r in resources[:5])
    names += " and %d more" % (len(resources) - 5) if len(resources) > 5 else ""
    return {"lookup": lookup, "text": "%s lookup failed for %s: %s; the %s stays unknown until a lookup works." % (
        what, names, error, unknown)}


def app_resource(app, live, billed, latest, watched, slow, now):
    """One row of `modal app list` as a resource, with `live` its running containers, `billed` its (hour, usd) billing
    rows (None: this pass read no billing for it) and `latest` its last billed (hour, usd) or None. Pure.

    The cost counts the billing from a day before the app launched. The rate is the last billed hour's if that hour
    began within 4 h, else 0; a slow pass that billed no hour for it gives 0 too. None for a stopped app. A deployed
    app with no tasks, containers, cost or watch (`watched`) is dormant: only its identity comes back, with
    "dormant": True. Otherwise "dormant" is False."""
    state = (app.get("state") or "").strip()
    if state == "stopped":
        return None
    tasks = int(app.get("tasks") or 0)
    launched = util.parse_time(app.get("created_at"))
    r = {"native_id": app.get("app_id"), "name": app.get("description"), "kind": "app", "state": state,
         "launched_at": launched, "tasks": tasks, "containers": len(live), "dormant": False}
    starts = [s for s in (util.parse_time(c.get("start_time")) for c in live) if s]
    r["max_container_age_s"] = (now - min(starts)) if starts else None
    if billed is not None:
        since = (launched or 0) - 86400
        r["cost_usd"] = round(sum(c for t, c in billed if t >= since), 2)
        r["cost_source"] = "billing (%d d max)" % BILLING_DAYS
    if latest is not None:
        t, c = latest
        r["rate_usd_h"] = round(c, 2) if t >= now - 4 * 3600 else 0.0
        r["rate_source"] = "billing, last full hour"
    elif slow:
        r["rate_usd_h"], r["rate_source"] = 0.0, "billing, last full hour"
    if not (tasks > 0 or live or watched or state != "deployed" or (r.get("cost_usd") or 0) > 0):
        return {"native_id": r["native_id"], "name": r["name"], "kind": "app", "state": state, "tasks": 0,
                "containers": 0, "launched_at": launched, "dormant": True}
    return r


def collect(acct, opts):
    """The workspace's apps, with health, cost and the people behind them, from the modal CLI (`modal app list`,
    `container list`, `app logs`, `app history`; on a slow pass `billing` and `volume list`) and one run of the
    modal_api.py lookup helper (see _lookup_creators). Read-only. Besides resources and metrics it returns
    "creators" (the AppGetLifecycle lookups made this pass, for the parent's modal_app_owner cache), "warnings"
    (lookups that failed, each {"lookup", "text"}) and, on a slow pass, "spend" (see spend.py). On a slow pass an app
    with no billed hour in the last 48 h has rate 0. With opts["inventory_only"] it runs only the app and container
    listings: no crash checks, deploy history or creator lookups."""
    now = opts["now"]
    apps = util.json_tail(_modal(acct, ["app", "list", "--json"]))
    by_app = _containers_by_app(acct)
    billing, latest, metrics, spend = ({}, {}, {}, None)
    if opts.get("slow"):
        billing, latest, metrics, spend = _billing(acct, now)
        try:
            spend["volumes_by"] = _volume_creators(acct)
        except (RuntimeError, ValueError) as e:
            spend["volumes_error"] = str(e)[:200]
    watch = set(opts.get("watch") or [])
    known_history = opts.get("known_history") or {}
    out, history_failed, warnings = [], [], []
    for a in apps:
        app_id = a.get("app_id")
        live = by_app.get(app_id, [])
        r = app_resource(a, live, billing.get(app_id), latest.get(app_id), app_id in watch, opts["slow"], now)
        if r is None:
            continue
        if r["dormant"]:
            out.append(r)
            continue
        if not opts["inventory_only"]:
            if r["tasks"] > 0 or live or app_id in watch:
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
                except RuntimeError as e:
                    history_failed.append((r, str(e)[:200]))
        out.append(clean(r) | {"dormant": False})
    if history_failed:
        warnings.append(_lookup_failed("history", [r for r, _ in history_failed], history_failed[0][1]))
    creators = {} if opts["inventory_only"] else _lookup_creators(acct, opts, out, spend, warnings)
    return {"resources": out, "metrics": metrics, "creators": creators, "warnings": warnings} | (
        {"spend": spend} if spend else {})


def _lookup_creators(acct, opts, resources, spend, warnings):
    """One AppGetLifecycle batch for this pass, for two uses. Ownership: the creator of each tracked app that has no
    deployer (a `modal run` app). Spend, on a slow pass (`spend` given): the people on each billed app that
    opts["known_creators"] does not name, costliest first, up to MAX_LOOKUPS. Creators the parent already holds
    (opts["known_creator_of"]) are not looked up again; a slow pass runs the helper even with nothing to look up, for
    the token's user.

    Sets created_by on those resources; on a slow pass sets spend's token_user and lookup_errors (failed billed
    lookups), or its error when the helper failed. Appends a warning when an ownership lookup failed. Returns
    {app_id: {name, created_by, deployed_by, state}} for each app looked up, for the parent to cache."""
    known = opts["known_creator_of"]
    owners = []
    for r in resources:
        if r.get("dormant") or r.get("deployed_by"):
            continue
        if known.get(r["native_id"]):
            r["created_by"] = known[r["native_id"]]
        else:
            owners.append(r)
    billed = []
    if spend is not None:
        recent = set(opts["known_creators"])
        billed = [a for a, (_, usd) in sorted(spend["month"].items(), key=lambda kv: -kv[1][1])
                  if usd > 0 and a not in recent][:MAX_LOOKUPS]
    names = {r["native_id"]: r["name"] for r in owners} | {a: spend["month"][a][0] for a in billed}
    if not names and spend is None:
        return {}
    try:
        res = _creators(acct, list(names), modal_python())
    except (RuntimeError, ValueError, OSError) as e:
        if spend is not None:
            spend["error"] = str(e)[:200]
        if owners:
            warnings.append(_lookup_failed("creators", owners, str(e)[:200]))
        return {}
    found, errors = res["apps"], res["errors"]
    for r in owners:
        who = (found.get(r["native_id"]) or {}).get("created_by")
        if who:
            r["created_by"] = who
    if spend is not None:
        spend["token_user"] = res.get("token_user")   # absent when TokenInfoGet failed; spend.ingest keeps the last one
        spend["lookup_errors"] = sum(1 for a in billed if a in errors)
    failed = [r for r in owners if r["native_id"] in errors]
    if failed:
        warnings.append(_lookup_failed("creators", failed, ", ".join(sorted({errors[r["native_id"]] for r in failed}))))
    return {a: dict(c, name=names[a]) for a, c in found.items()}


def stop(acct, app_id):
    """`modal app stop --yes`: containers stop; the app can be started again; volumes, secrets and images stay.
    --yes because the CLI asks for confirmation and aborts when, as here, no terminal is attached."""
    code, out, err = util.run(["modal", "app", "stop", "--yes", app_id], env=_env(acct), timeout=120)
    return code == 0, (out + err).strip()[-300:]
