"""Fold one collector result into state: resources, health streaks, ownership, registrations, gone."""
import fnmatch
import json

from . import util


def key_for(account, native_id):
    return "%s:%s" % (account, native_id)


def billing_now(provider, r):
    if provider == "modal":
        return bool((r.get("tasks") or 0) > 0 or (r.get("containers") or 0) > 0 or (r.get("rate_usd_h") or 0) > 0.01)
    if provider == "lambda":
        return r.get("state") in ("active", "booting", "unhealthy")
    if provider == "slurm":
        return r.get("state") in ("RUNNING", "CONFIGURING", "COMPLETING")
    return True  # hf / deepinfra collectors only return billing resources


def modal_users(cfg, db=None, account=None):
    """Your Modal user names: identity.owner_modal_users, plus the user behind the watchdog's own token for
    this workspace once a billing pass has seen it."""
    users = set(cfg["identity"].get("owner_modal_users") or [])
    if db is not None and account:
        token_user = (db.meta("spend:" + account) or {}).get("token_user")
        if token_user:
            users.add(token_user)
    return users


def ownership(cfg, provider, r, prev, users=None):
    """ours | foreign | unknown. With no identity configured for a provider, ownership stays unknown, which the
    rules treat like ours: the owner search, not the provider's user field, decides who is asked."""
    ident = cfg["identity"]
    if provider == "modal" and (r.get("deployed_by") or r.get("created_by")):
        users = users if users is not None else modal_users(cfg)
        if users:
            return "ours" if util.modal_user(r) in users else "foreign"
    keys = ident.get("owner_lambda_ssh_keys") or []
    if provider == "lambda" and r.get("ssh_keys") and keys:
        return "ours" if set(r["ssh_keys"]) & set(keys) else "foreign"
    if provider in ("slurm", "hf", "deepinfra"):
        return "ours"
    return prev or "unknown"


def crash_candidate(detail, rule):
    if not detail or "failed" not in detail:
        return False
    seen = max(detail.get("seen") or 0, detail["failed"])
    return detail["failed"] >= rule["min_failures"] and seen and detail["failed"] / seen >= rule["min_failure_ratio"]


def match_registration(db, key, name, now):
    for reg in db.q("select * from registrations where matched_key is null or matched_key=? order by id desc", key):
        pat = reg["pattern"]
        provider_acct = key.rsplit(":", 1)[0] if key.count(":") >= 2 else ""
        if fnmatch.fnmatch(key, pat) or (name and fnmatch.fnmatch("%s:%s" % (provider_acct, name), pat)):
            if reg["matched_key"] is None and "*" not in pat and "?" not in pat:
                db.x("update registrations set matched_key=?, matched_at=? where id=?", key, now, reg["id"])
            return reg
    return None


def apply_registration(db, res, reg, now):
    start = res["launched_at"] or res["first_seen"] or now
    upd = {"owner_session": reg["session_id"], "owner_cli": reg["cli"], "owner_name": reg["session_name"],
           "owner_source": "registered", "owner_confidence": "high", "owner_checked_at": now,
           "owner_evidence": "registered: %s" % (reg["reason"] or reg["pattern"])}
    if res["ownership"] == "unknown":
        upd["ownership"] = "ours"
    if reg["expect_secs"]:
        cost = max(0.0, (res["cost_usd"] or 0.0) - (res["cost_basis"] or 0.0))
        upd.update(suppress_until=start + reg["expect_secs"], suppress_by=reg["session_id"],
                   suppress_reason="registered: %s" % (reg["reason"] or ""), suppress_health=res["health"],
                   ack_x_usd=reg["expect_cost"] if reg["expect_cost"] is not None else cost)
    db.update_resource(res["key"], **upd)
    inc = db.open_incident(res["key"])
    if inc is not None and reg["expect_secs"]:
        db.close_incident(inc["id"], "registered", now)


def ingest(db, cfg, account, result, now, slow):
    provider = cfg["accounts"][account]["provider"]
    users = modal_users(cfg, db, account) if provider == "modal" else None
    crash_rule = cfg["rules"]["crash_loop"]
    idle_rule = cfg["rules"]["idle_gpu"]
    seen_keys = set()
    for r in result["resources"]:
        key = key_for(account, r["native_id"])
        seen_keys.add(key)
        prev = db.resource(key)
        if r.get("dormant"):
            # A deployed Modal app scaled to zero bills nothing; track it only if we already know it.
            if prev is not None and prev["gone_at"] is None:
                db.update_resource(key, last_seen=now, state=r.get("state"), tasks=0, containers=0,
                                   billing_now=0, active_since=None, crash_streak=0,
                                   health="ok" if prev["health"] == "crash_loop" else prev["health"])
            continue
        if prev is not None and prev["gone_at"] is not None:
            db.update_resource(key, gone_at=None)
            prev = db.resource(key)
        fields = {"provider": provider, "account": account, "native_id": r["native_id"], "kind": r.get("kind"),
                  "name": r.get("name"), "state": r.get("state"), "last_seen": now, "gone_at": None}
        if prev is None:
            fields["first_seen"] = now
        for f in ("launched_at", "tasks", "containers", "deployed_by", "created_by", "git_commit"):
            if r.get(f) is not None:
                fields[f] = r[f]
        # Billing data arrives on the slow pass only; keep the last known values in between.
        if r.get("rate_usd_h") is not None:
            fields["rate_usd_h"], fields["rate_source"] = r["rate_usd_h"], r.get("rate_source")
        elif provider == "modal" and prev is not None and slow:
            fields["rate_usd_h"] = 0.0
        if r.get("cost_usd") is not None:
            fields["cost_usd"], fields["cost_source"] = r["cost_usd"], r.get("cost_source")
        if r.get("ssh_keys") is not None:
            fields["ssh_keys"] = json.dumps(r["ssh_keys"])
        extra = {k: r[k] for k in ("instance_type", "region", "ip", "partition", "max_container_age_s",
                                   "deployed_at", "idle_detail") if r.get(k) is not None}
        fields["extra"] = json.dumps(extra)
        merged = dict(prev) if prev is not None else {}
        merged.update(fields)
        bill = billing_now(provider, merged)
        fields["billing_now"] = int(bill)
        if bill and not (prev is not None and prev["billing_now"] and prev["active_since"]):
            launched = merged.get("launched_at")
            fresh = launched and (now - launched) < 3 * 3600 or provider in ("lambda", "slurm", "hf", "deepinfra")
            fields["active_since"] = launched if fresh and launched else now
            # Cost rules count spend in this active stretch; Modal deployments accrue across stretches.
            fields["cost_basis"] = 0.0 if (fresh or provider != "modal") else (merged.get("cost_usd") or 0.0)
        elif not bill:
            fields["active_since"] = None
        # Health.
        if provider == "modal" and "health_detail" in r:
            d = r["health_detail"]
            fields["health_detail"] = json.dumps(d)
            if "error" not in d:
                streak = (prev["crash_streak"] if prev is not None else 0) + 1 if crash_candidate(d, crash_rule) else 0
                fields["crash_streak"] = streak
                fields["health"] = "crash_loop" if streak >= crash_rule["consecutive_checks"] else "ok"
        if provider == "lambda" and "idle" in r:
            if r["idle"] is True:
                fields["idle_streak"] = (prev["idle_streak"] if prev is not None else 0) + 1
            elif r["idle"] is False:
                fields["idle_streak"] = 0
            streak = fields.get("idle_streak", prev["idle_streak"] if prev is not None else 0)
            fields["health"] = "idle" if streak >= idle_rule["consecutive_checks"] else "ok"
        # From the collector's own values (ssh_keys is a list there; in `merged` it is already JSON text).
        fields["ownership"] = ownership(cfg, provider, r, prev["ownership"] if prev is not None else None, users)
        db.upsert_resource(key, fields)
        # Registration = owner known up front, plus an ack for the expected duration.
        cur = db.resource(key)
        if cur["owner_source"] != "registered":
            reg = match_registration(db, key, cur["name"], now)
            if reg is not None:
                apply_registration(db, cur, reg, now)
                cur = db.resource(key)
        db.x("insert into observations(key, ts, state, tasks, rate, cost, health, billing) values(?,?,?,?,?,?,?,?)",
             key, now, cur["state"], cur["tasks"], cur["rate_usd_h"], cur["cost_usd"], cur["health"], int(bill))
    # Anything of this account we knew and no longer see is gone (stopped / terminated / finished).
    gone = []
    for row in db.q("select key from resources where account=? and gone_at is null", account):
        if row["key"] not in seen_keys:
            db.update_resource(row["key"], gone_at=now, billing_now=0, active_since=None)
            gone.append(row["key"])
    for k, v in (result.get("metrics") or {}).items():
        db.x("insert into metrics(account, ts, name, value) values(?,?,?,?)", account, now, k, v)
    return gone


def prune(db, now, days=30):
    db.x("delete from observations where ts < ?", now - days * 86400)
    db.x("delete from metrics where ts < ?", now - days * 86400)
    db.x("delete from modal_app_owner where checked_at < ?", now - 62 * 86400)   # still billed? looked up again
