"""Whose Modal spend is it: split a workspace's month-to-date bill into yours, shared and others.

Modal's billing report is per app (object_id) with no user field, and a workspace may be shared by
several people. Each app's lifecycle does name a user, though: `created_by` (who started it: `modal run`,
`modal deploy`, or an `App.lookup(create_if_missing=True)`) and `deployed_by` (its latest deploy). So:

- yours:  every person on the app is in identity.owner_modal_users or is the user behind the watchdog's own
          token for that workspace. Any token of that user counts, so you and all your agents, on any machine;
- shared: an app on the identity.modal_shared_apps list, or one created and deployed by different people.
          Modal bills the whole app to that app; it records no user per container or sandbox, so this
          part cannot be split (e.g. `inspect_modal_sandbox` holds every Inspect sandbox in the workspace);
- others: apps whose people are all someone else;
- unknown: not looked up yet, the lookup failed, or Modal no longer has the app's record.

Volume storage is billed per workspace, never per volume, so it is outside the split.
Creators are cached in `modal_app_owner`: they never change, so each app is looked up once (a deployed
app again after a day, in case someone else redeployed it).
"""
import datetime as dt

from . import util

FINAL_STATES = {"stopped", "ephemeral", "detached", "stopping", "not-found"}   # deployed_by can't change
RECHECK_S = 86400


def known_ids(db, account, now):
    """App ids whose creator the child need not look up again."""
    return [r["app_id"] for r in db.q("select app_id, state, checked_at from modal_app_owner where account=?", account)
            if r["state"] in FINAL_STATES or r["checked_at"] >= now - RECHECK_S]


def ingest(db, cfg, account, spend, metrics, now):
    """Store the creators the child found, compute the split, keep it for the digest. Inside ingest's tx."""
    for app_id, c in (spend.get("creators") or {}).items():
        name = (spend["month"].get(app_id) or [None])[0]
        db.x("insert into modal_app_owner(account, app_id, name, created_by, deployed_by, state, checked_at) "
             "values(?,?,?,?,?,?,?) on conflict(account, app_id) do update set name=excluded.name, "
             "created_by=excluded.created_by, deployed_by=excluded.deployed_by, state=excluded.state, "
             "checked_at=excluded.checked_at", account, app_id, name, c.get("created_by"), c.get("deployed_by"),
             c.get("state"), now)
    owners = {r["app_id"]: dict(r) for r in db.q("select * from modal_app_owner where account=?", account)}
    token_user = spend.get("token_user") or (db.meta("spend:" + account) or {}).get("token_user")
    users = your_users(cfg["identity"], token_user)
    s = split(spend["month"], owners, users, cfg["identity"], now)
    s.update(through=spend.get("through"), users=sorted(users), token_user=token_user, error=spend.get("error"),
             lookup_errors=spend.get("lookup_errors") or 0, month_total_usd=metrics.get("month_metered_usd"),
             volumes_usd=metrics.get("month_volumes_usd"))
    vols = spend.get("volumes_by")
    if vols is not None:
        s["volumes"] = {"yours": sum(n for who, n in vols.items() if who in users), "total": sum(vols.values())}
    db.set_meta("spend:" + account, s)
    for k in ("apps_usd", "yours_usd", "shared_usd", "others_usd", "unknown_usd"):
        db.x("insert into metrics(account, ts, name, value) values(?,?,?,?)", account, now, "month_" + k, s[k])
    return s


def your_users(identity, token_user=None):
    """Your Modal user names: the configured ones plus whoever owns the token the watchdog reads with."""
    return set(identity.get("owner_modal_users") or []) | ({token_user} if token_user else set())


def family(name):
    """Group look-alike app names: `exp-sweep-models-fixed10-001-large` -> `exp-sweep-models-*`.
    Cuts at the first dash-part (from the third on) that holds a digit: run ids, dates, model sizes."""
    parts = (name or "?").split("-")
    for i, p in enumerate(parts):
        if i >= 2 and any(ch.isdigit() for ch in p):
            return "-".join(parts[:i]) + "-*"
    return name or "?"


def split(month, owners, ours, identity, now, top=6):
    """month: {app_id: [name, usd]}; owners: {app_id: {created_by, deployed_by}}; ours: your user names. Pure."""
    shared_names = set(identity.get("modal_shared_apps") or [])
    out = {"as_of": now, "month": dt.datetime.fromtimestamp(now, util.UTC).strftime("%Y-%m"),
           "apps_usd": 0.0, "yours_usd": 0.0, "shared_usd": 0.0, "others_usd": 0.0, "unknown_usd": 0.0,
           "yours_apps": 0}
    others, shared, fam = {}, [], {}
    for app_id, (name, usd) in month.items():
        out["apps_usd"] += usd
        o = owners.get(app_id)
        people = {p for p in ((o or {}).get("created_by"), (o or {}).get("deployed_by")) if p}
        if name in shared_names or (len(people) > 1 and not people <= ours):
            out["shared_usd"] += usd
            shared.append([name, usd, (o or {}).get("created_by"), (o or {}).get("deployed_by")])
        elif not people:
            out["unknown_usd"] += usd
        elif people <= ours:
            out["yours_usd"] += usd
            out["yours_apps"] += 1
            f = fam.setdefault(family(name), [0.0, 0, name])
            f[0] += usd
            f[1] += 1
        else:
            who = next(iter(people))
            others[who] = others.get(who, 0.0) + usd
            out["others_usd"] += usd
    for k in ("apps_usd", "yours_usd", "shared_usd", "others_usd", "unknown_usd"):
        out[k] = round(out[k], 2)
    out["others_by"] = [[w, round(c, 2)] for w, c in sorted(others.items(), key=lambda kv: -kv[1])]
    out["shared"] = [[n, round(c, 2), cb, db] for n, c, cb, db in sorted(shared, key=lambda x: -x[1]) if c >= 0.005]
    fams = sorted(([f if n > 1 else name, round(c, 2), n] for f, (c, n, name) in fam.items()), key=lambda x: -x[1])
    out["yours_top"] = fams[:top]
    rest = fams[top:]
    out["yours_rest"] = [round(sum(c for _, c, _ in rest), 2), sum(n for _, _, n in rest)]
    return out


def _pct(part, whole):
    return " (%d%% of app spend)" % round(100 * part / whole) if whole else ""


def lines(account, s):
    """Markdown sub-bullets for one Modal account's split (indented under its Spend line)."""
    if not s:
        return []
    users = s.get("users") or []
    who = " or ".join("`%s`" % u for u in users) or "you"
    if s.get("error") and not s.get("yours_apps") and not s.get("yours_usd"):
        return ["    - Yours: unavailable (creator lookup failed: `%s`)." % s["error"][:120]]
    total = s.get("month_total_usd")
    out = ["    - **This month: total %s, yours %s**%s. Basis: apps created or deployed by %s, i.e. any token of "
           "%s: you and all your agents, on any machine.%s" % (
               util.money(total if total is not None else s["apps_usd"]), util.money(s["yours_usd"]),
               _pct(s["yours_usd"], s["apps_usd"]), who, "those Modal users" if len(users) > 1 else "that Modal user",
               " Billing to %s." % util.iso(s["through"]) if s.get("through") else "")]
    bits = ["yours %s (%d apps)" % (util.money(s["yours_usd"]), s["yours_apps"])]
    for name, usd, created, deployed in s.get("shared") or []:
        by = "created by %s" % (created or "?") + (", deployed by %s" % deployed if deployed and deployed != created else "")
        bits.append("shared `%s` %s (%s; holds several people's work, can't be split)" % (name, util.money(usd), by))
    if s["others_usd"]:
        bits.append("others %s (%s)" % (util.money(s["others_usd"]), ", ".join(
            "%s %s" % (w, util.money(c)) for w, c in s["others_by"][:5])))
    if s["unknown_usd"] >= 0.01:
        bits.append("creator unknown %s" % util.money(s["unknown_usd"]))
    out.append("    - Split of the %s app spend: %s." % (util.money(s["apps_usd"]), " · ".join(bits)))
    if s.get("yours_top"):
        top = ["%s%s %s" % (n, " (%d apps)" % k if k > 1 else "", util.money(c)) for n, c, k in s["yours_top"]]
        rest_usd, rest_n = s.get("yours_rest") or (0, 0)
        if rest_n:
            top.append("%d more %s" % (rest_n, util.money(rest_usd)))
        out.append("    - Yours by app: " + " · ".join(top) + ".")
    vol = s.get("volumes")
    out.append("    - Not in \"yours\": volume storage%s (billed per workspace, not per volume%s); your containers "
               "or sandboxes inside an app someone else started%s (they bill to that app); model servers others "
               "deployed that your runs call." % (
                   " " + util.money(s["volumes_usd"]) if s.get("volumes_usd") is not None else "",
                   "; you created %d of %d volumes" % (vol["yours"], vol["total"]) if vol else "",
                   ", such as the shared app above" if s.get("shared") else ""))
    return out


def plain(line):
    """A digest sub-bullet as terminal text."""
    return line.strip()[2:].replace("**", "").replace("`", "")
