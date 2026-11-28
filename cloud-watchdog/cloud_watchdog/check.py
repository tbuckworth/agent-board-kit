"""One timer tick: collect -> ingest -> blind alerts and digest lines -> quiet owner search -> ladder -> digest."""
import concurrent.futures as cf
import fcntl
import hashlib
import json
import shutil
import tempfile

from . import (config, digest, gather, investigator, ladder, messages, modes, notify, observe, owner, providers, rules,
               spend, state, util)

QUIET_OWNER_MAX_AGE_S = 6 * 3600   # quiet attribution searches again for an owner it could not find after this long
LAST_OK = "last_ok_at"   # meta key: when the last check finished (`cloud-watch status` and the heartbeat read it)


def collect_opts(db, cfg, account, now, slow, inventory_only):
    """The options of one account's collect: `now`, `slow`, `inventory_only` (list the resources, without health
    probes or lookups), and what the provider's collect_options adds."""
    opts = {"now": now, "slow": slow, "inventory_only": inventory_only}
    mod = providers.get(cfg["accounts"][account]["provider"])
    if hasattr(mod, "collect_options"):
        opts.update(mod.collect_options(db, cfg, account, now, slow))
    return opts


def run(cfg, dry_run=False, force_slow=False, only=None):
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(config.LOCK_PATH, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return {"skipped": "another check is running"}
    try:
        db_path, tmp = config.DB_PATH, None
        if dry_run:
            # A dry run works on a copy, so it can never advance a real incident. We hold the lock, so any
            # copy still here is from a dry run that was killed before its cleanup ran.
            for old in config.STATE_DIR.glob("cw-dry-*"):
                shutil.rmtree(old, ignore_errors=True)
            tmp = tempfile.mkdtemp(prefix="cw-dry-", dir=str(config.STATE_DIR))
            db_path = "%s/state.sqlite" % tmp
            if config.DB_PATH.exists():
                state.DB().c.execute("vacuum into ?", (db_path,))
        try:
            return _run(state.DB(db_path), cfg, dry_run, force_slow, only, util.now())
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def record_config(db, cfg, now):
    """Log the merged config as a `config` action when it differs from the one the last check logged."""
    config_hash = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
    if db.meta("config_hash") != config_hash:
        db.log("config", cfg, shadow=False, now=now)
        db.set_meta("config_hash", config_hash)


def _run(db, cfg, dry_run, force_slow, only, now):
    if not dry_run:
        record_config(db, cfg, now)
    mode, switched = modes.effective(db, now)
    summary = {"mode": mode, "dry_run": dry_run, "accounts": {}, "switched_live": switched}
    if mode == "off" and not dry_run:
        db.set_meta(LAST_OK, now)
        return summary
    live = mode == "live" and not dry_run
    notifier = notify.Notifier(db, cfg, shadow=not live, now=now, run=util.run, op=gather.op)
    if switched and not dry_run:
        users = cfg["identity"].get("owner_modal_users") or []
        stops = (" and stops crash-looping Modal apps that %s deployed (or, for `modal run` apps, created), on "
                 "accounts with auto_stop = true" % "/".join(users)) if users else \
            "; it never stops anything itself (identity.owner_modal_users is empty)"
        notifier.send(messages.subject("Now live"),
                      "The shadow period is over: from now on the watchdog messages the agents that own resources, "
                      "emails you%s. Reply \"stay in shadow\" or \"turn it off\" to change that." % stops, alert=None)

    # 1. collect, in parallel, one credentialed child per account
    jobs = {}
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        for account, acct in cfg["accounts"].items():
            if only and account not in only:
                continue
            h = db.collector_health(account)
            slow = force_slow or not h or not h["last_slow_at"] or now - h["last_slow_at"] >= cfg["slow_interval_min"] * 60
            if acct.get("slow_only") and not slow:
                continue
            opts = collect_opts(db, cfg, account, now, slow, inventory_only=False)
            jobs[pool.submit(gather.op, acct, "collect", opts)] = (account, slow)
        results = [(jobs[f], f.result()) for f in cf.as_completed(jobs)]

    # 2. ingest
    for (account, slow), (ok, res) in sorted(results, key=lambda x: x[0][0]):
        summary["accounts"][account] = ingest_account(db, cfg, account, slow, ok, res, now)

    # 3. blind collectors
    alert_blind(db, cfg, notifier, live, now)

    # 4. account-level digest lines: daily ones on every check; weekly ones only in the check that sends the Monday
    #    digest, so each goes out with that digest and no copy waits for the next Monday.
    weekly = digest.due(db, cfg, now) and util.local_time(now).weekday() == 0
    for account in cfg["accounts"]:
        for f in rules.metric_findings(db, cfg, account, weekly):
            db.add_digest(f["kind"], f["key"], f["text"], now)

    # 5. quiet attribution of anything of ours that is billing, so `cloud-watch list` shows owners
    pending = [r for r in db.ours_billing() if not r["owner_session"]
               and (r["owner_checked_at"] is None or r["owner_checked_at"] < now - QUIET_OWNER_MAX_AGE_S)][:5]
    if pending:
        try:
            book = owner.sessions()
        except owner.AddressBookError as e:
            db.log("error", "quiet attribution skipped: %s" % e, ok=False, shadow=False, now=now)
        else:
            for res in pending:
                owner.refresh(db, cfg, res, now, book, QUIET_OWNER_MAX_AGE_S, owner.resolve)

    # 6. the ladder
    ladder.Ladder(db, cfg, now, notifier, live=live, sessions=owner.sessions, resolve_owner=owner.resolve,
                  run_investigator=investigator.run, op=gather.op).process()

    # 7. digest
    if dry_run:
        if digest.due(db, cfg, now):
            summary["digest"] = "due (not sent: dry run)"
    else:
        summary["digest"] = digest.send_if_due(db, cfg, now, notifier)

    if now - db.meta("pruned_at", 0) >= 86400:   # once a day, whatever the timer's interval
        db.prune(now)
        db.set_meta("pruned_at", now)
    db.set_meta(LAST_OK, now)
    summary["would"] = [dict(a) for a in db.q("select ts, key, action, detail, shadow from actions where ts>=? "
                                                "order by ts", now)]
    return summary


def alert_blind(db, cfg, notifier, live, now):
    """Tell the human once about each account whose last rules.blind.consecutive_failures collections all failed: by
    email when live (an email that fails to send is retried next check), else in the digest."""
    for h in db.collectors(cfg["rules"]["blind"]["consecutive_failures"]):
        if h["alerted"]:
            continue
        subject, md = messages.blind(h["account"], h)
        if live:
            if notifier.email(subject, md, alert="cloud-blind", key=h["account"]) == "failed":
                continue   # not marked alerted: the next check sends it again
        else:
            text = "%s: check failing (%s)" % (h["account"], (h["last_error"] or "")[:120])
            db.add_digest("blind", h["account"], text, now)
        db.collector_alerted(h["account"])


def ingest_account(db, cfg, account, slow, ok, res, now):
    """Record one account's collection. One transaction: a check killed mid-ingest leaves the account exactly as
    the last complete check saw it, and the next tick collects it again."""
    if not ok:
        db.collector_failed(account, str(res)[:500], now)
        return {"ok": False, "error": str(res)[:200]}
    with db.tx():
        if "creators" in res:   # the creator lookups the collector made (Modal)
            spend.cache_creators(db, account, res["creators"], now)
        gone = observe.ingest(db, cfg, account, res, now)
        if res.get("spend"):
            spend.ingest(db, cfg, account, res["spend"], res.get("metrics") or {}, now)
        warnings = res.get("warnings", [])   # lookups that failed while the collection as a whole worked (Modal)
        for w in warnings:
            # One line per account and lookup a day.
            db.add_digest("collector_warning", "%s %s" % (account, w["lookup"]), "%s: %s" % (account, w["text"]), now)
        db.collector_ok(account, now, slow)
    return {"ok": True, "slow": slow, "n": len([r for r in res["resources"] if not r.get("dormant")]), "gone": gone,
            "warnings": [w["text"] for w in warnings]}
