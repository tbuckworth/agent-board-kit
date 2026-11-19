"""Fold one collector result into state: resources, health streaks, ownership, registrations, gone."""
import fnmatch
import json

from . import owner, providers, rules, spend, util


def key_for(account, native_id):
    return "%s:%s" % (account, native_id)


def identity_for(cfg, db, account):
    """cfg's [identity], with the user behind the watchdog's own token for this Modal workspace added to
    owner_modal_users once a billing pass has seen it (spend.py records it). Only when owner_modal_users is set:
    an empty list keeps Modal ownership unknown."""
    ident = dict(cfg["identity"])
    users = list(ident.get("owner_modal_users") or [])
    if users and cfg["accounts"][account]["provider"] == "modal":
        token_user = (db.meta(spend.meta_key(account)) or {}).get("token_user")
        if token_user and token_user not in users:
            ident["owner_modal_users"] = users + [token_user]
    return ident


def crash_candidate(detail, rule):
    if not detail or "failed" not in detail:
        return False
    seen = max(detail.get("seen") or 0, detail["failed"])
    return detail["failed"] >= rule["min_failures"] and seen and detail["failed"] / seen >= rule["min_failure_ratio"]


def match_registration(db, key, name, now, skip_sessions, reg_id=None):
    """The newest registration whose pattern names this resource, or None; with `reg_id`, only that registration.
    Registrations made by a session in `skip_sessions` (those that disowned the resource) are passed over.
    A pattern without wildcards is bound to the first resource it matches."""
    for reg in db.q("select * from registrations where (matched_key is null or matched_key=?) and (? is null or id=?) "
                    "order by id desc", key, reg_id, reg_id):
        if reg["session_id"] in skip_sessions:
            continue
        pat = reg["pattern"]
        provider_acct = key.rsplit(":", 1)[0] if key.count(":") >= 2 else ""
        if fnmatch.fnmatch(key, pat) or (name and fnmatch.fnmatch("%s:%s" % (provider_acct, name), pat)):
            if reg["matched_key"] is None and "*" not in pat and "?" not in pat:
                db.x("update registrations set matched_key=?, matched_at=? where id=?", key, now, reg["id"])
            return reg
    return None


def ack(db, res, until, reason, by, expect_cost, resolution, now):
    """Ack `res` (its resources row) until `until`: owner messages about it stop, except for a new crash loop, and
    its cost emails count from X, which is `expect_cost` or else its cost so far (rules.cost_email_threshold). Closes
    its open incident with `resolution`. Returns (X, the incident it closed or None)."""
    x = expect_cost if expect_cost is not None else (rules.streak_cost(res) or 0.0)
    db.update_resource(res["key"], suppress_until=until, suppress_reason=reason, suppress_by=by,
                       suppress_health=res["health"], ack_x_usd=x)
    inc = db.open_incident(res["key"])
    if inc is not None:
        db.close_incident(inc["id"], resolution, now)
    return x, inc


def apply_registration(db, res, reg, now):
    """Make the session that made registration `reg` the owner of `res`, a resource it names. A registration with an
    expected duration also acks it until launch plus that duration, which resolves its open incident (logged)."""
    upd = owner.assignment(session=reg["session_id"], cli=reg["cli"], name=reg["session_name"], source="registered",
                           confidence="high", evidence="registered: %s" % (reg["reason"] or reg["pattern"]), now=now)
    if res["ownership"] == "unknown":
        upd["ownership"] = "ours"
    db.update_resource(res["key"], **upd)
    if reg["expect_secs"]:
        start = res["launched_at"] or res["first_seen"] or now
        _, inc = ack(db, res, start + reg["expect_secs"], "registered: %s" % (reg["reason"] or ""), reg["session_id"],
                     reg["expect_cost"], "registered", now)
        if inc is not None:
            db.log("resolve", "registered", key=res["key"], incident_id=inc["id"], shadow=False, now=now)


def probe_health(prev, column, hit, rule, label):
    """One health probe's step on a streak of checks: a hit (True) extends the streak in the resource's previous row
    `prev` (None: new), a miss (False) resets it, and a failed probe (None) keeps it. Returns (streak, health): health
    is `label` once the streak reaches the rule's consecutive_checks, else "ok"."""
    streak = prev[column] if prev is not None else 0
    if hit is not None:
        streak = streak + 1 if hit else 0
    return streak, (label if streak >= rule["consecutive_checks"] else "ok")


def derive(cfg, account, r, prev, now, identity=None):
    """The fields of the resources row for `r`, one resource the account's collector returned, given its row as it
    stands (`prev`, None for a new one). Pure. A field the collection does not carry keeps its stored value; billing
    starts or ends the active stretch and its cost basis; health follows the probes the collector ran; ownership is
    the provider's rule applied to `identity` (default cfg's [identity]; see identity_for), else the stored value. Returns (fields, failures): a (digest kind, text) for each probe that
    failed, which leaves its streak as it was."""
    provider = cfg["accounts"][account]["provider"]
    mod = providers.get(provider)
    fields = {"provider": provider, "account": account, "native_id": r["native_id"], "kind": r.get("kind"),
              "name": r.get("name"), "state": r.get("state"), "last_seen": now, "gone_at": None}
    if prev is None:
        fields["first_seen"] = now
    for f in ("launched_at", "tasks", "containers", "deployed_by", "created_by", "git_commit"):
        if r.get(f) is not None:
            fields[f] = r[f]
    # A pass without billing data (Modal's comes on the slow pass only) keeps the last known values.
    if r.get("rate_usd_h") is not None:
        fields["rate_usd_h"], fields["rate_source"] = r["rate_usd_h"], r.get("rate_source")
    if r.get("cost_usd") is not None:
        fields["cost_usd"], fields["cost_source"] = r["cost_usd"], r.get("cost_source")
    if r.get("ssh_keys") is not None:
        fields["ssh_keys"] = json.dumps(r["ssh_keys"])
    extra = {k: r[k] for k in ("instance_type", "region", "ip", "partition", "max_container_age_s",
                               "deployed_at", "idle_detail") if r.get(k) is not None}
    fields["extra"] = json.dumps(extra)
    merged = dict(prev) if prev is not None else {}
    merged.update(fields)
    bill = mod.billing_now(merged)
    fields["billing_now"] = int(bill)
    if bill and not (prev is not None and prev["billing_now"] and prev["active_since"]):
        launched = merged.get("launched_at")
        # A provider that counts from launch starts the stretch there. Otherwise (Modal) the reported cost is the
        # total since launch, so the stretch starts now with that total as its basis, and the cost rules count only
        # the spend since; a resource launched under 3 h ago counts from launch.
        fresh = mod.COUNTS_FROM_LAUNCH or launched and (now - launched) < 3 * 3600
        fields["active_since"] = launched if fresh and launched else now
        fields["cost_basis"] = 0.0 if fresh else (merged.get("cost_usd") or 0.0)
    elif not bill:
        fields["active_since"] = None
    # Health, from the probes the collector ran (Modal's crash check, the Lambda idle probe).
    failures, name = [], r.get("name") or key_for(account, r["native_id"])
    if "health_detail" in r:
        d = r["health_detail"]
        fields["health_detail"] = json.dumps(d)
        hit = None if "error" in d else bool(crash_candidate(d, cfg["rules"]["crash_loop"]))
        streak, health = probe_health(prev, "crash_streak", hit, cfg["rules"]["crash_loop"], "crash_loop")
        if hit is None:
            # Health and streak keep their last values, so crash-loop detection is paused for this app: say so.
            failures.append(("crash_check_error", "crash check failing for %s: %s" % (name, d["error"])))
        else:
            fields["crash_streak"], fields["health"] = streak, health
    if "idle" in r:
        streak, health = probe_health(prev, "idle_streak", r["idle"], cfg["rules"]["idle_gpu"], "idle")
        if r["idle"] is None:
            # The idle streak keeps its value, so idle detection is paused for this box: say so. Health follows the
            # kept streak.
            failures.append(("idle_check_error", "idle check failing for %s: %s" % (name, r["idle_detail"]["error"])))
        else:
            fields["idle_streak"] = streak
        fields["health"] = health
    # From the collector's own values (ssh_keys is a list there; in `merged` it is already JSON text). When they
    # name nobody, the resource keeps the ownership it had.
    fields["ownership"] = (mod.ownership(r, identity or cfg["identity"])
                           or (prev["ownership"] if prev is not None else None) or "unknown")
    return fields, failures


def ingest(db, cfg, account, result, now):
    """Fold one collection into state: each resource's row (see derive), its registration and an observation, a
    digest line for each probe that failed, `gone_at` on the account's resources no longer collected (and a `reappear`
    action for one collected again), and the metrics. Returns the keys that went."""
    seen_keys = set()
    identity = identity_for(cfg, db, account)
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
            db.log("reappear", "gone since %s" % util.iso(prev["gone_at"]), key=key, shadow=False, now=now)
            prev = db.resource(key)
        fields, failures = derive(cfg, account, r, prev, now, identity)
        for kind, text in failures:
            db.add_digest(kind, key, text, now)
        db.upsert_resource(key, fields)
        # Registration = owner known up front, plus an ack for the expected duration.
        cur = db.resource(key)
        if cur["owner_source"] != "registered":
            reg = match_registration(db, key, cur["name"], now, set(json.loads(cur["disowned"] or "[]")))
            if reg is not None:
                apply_registration(db, cur, reg, now)
                cur = db.resource(key)
        db.x("insert into observations(key, ts, state, tasks, rate, cost, health, billing) values(?,?,?,?,?,?,?,?)",
             key, now, cur["state"], cur["tasks"], cur["rate_usd_h"], cur["cost_usd"], cur["health"],
             fields["billing_now"])
    # Anything of this account we knew and no longer see is gone (stopped / terminated / finished).
    gone = []
    for row in db.q("select key from resources where account=? and gone_at is null", account):
        if row["key"] not in seen_keys:
            db.update_resource(row["key"], gone_at=now, billing_now=0, active_since=None)
            gone.append(row["key"])
    for k, v in (result.get("metrics") or {}).items():
        db.add_metric(account, k, v, now)
    return gone
