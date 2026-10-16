"""One timer tick: collect -> ingest -> rules/ladder -> digest -> heartbeat."""
import concurrent.futures as cf
import fcntl
import json
import shutil
import tempfile

from . import config, digest, gather, ladder, messages, modes, notify, observe, owner, rules, spend, state, util


def collect_opts(db, cfg, account, now, slow):
    opts = {"now": now, "slow": slow, "crash_window_min": cfg["rules"]["crash_loop"]["window_min"],
            "our_ssh_keys": cfg["identity"].get("owner_lambda_ssh_keys") or [], "idle_pattern": cfg["rules"]["idle_gpu"]["work_pattern"],
            "idle_gpu_max": cfg["rules"]["idle_gpu"]["gpu_util_max_pct"]}
    if cfg["accounts"][account]["provider"] == "modal":
        rows = db.q("select r.native_id, r.crash_streak, r.health, r.deployed_by, r.git_commit, r.extra, "
                    "(select count(*) from incidents i where i.key=r.key and i.closed_at is null) open_inc "
                    "from resources r where r.account=? and r.gone_at is null", account)
        opts["watch"] = [r["native_id"] for r in rows if r["crash_streak"] or r["health"] == "crash_loop" or r["open_inc"]]
        opts["known_history"] = {r["native_id"]: {"deployed_by": r["deployed_by"], "git_commit": r["git_commit"],
                                                  "deployed_at": json.loads(r["extra"] or "{}").get("deployed_at")}
                                 for r in rows if r["deployed_by"]}
        creators = {r["app_id"]: r["created_by"] for r in db.q(
            "select app_id, created_by from modal_app_owner where account=? and created_by is not null", account)}
        creators.update({r["native_id"]: r["created_by"] for r in db.q(
            "select native_id, created_by from resources where account=? and created_by is not null", account)})
        opts["known_creator_of"] = creators
        if cfg.get("modal_python"):
            opts["modal_python"] = cfg["modal_python"]
        if slow:
            opts["known_creators"] = spend.known_ids(db, account, now)
    return opts


def run(cfg=None, dry_run=False, force_slow=False, only=None, quiet=False, now=None):
    cfg = cfg or config.load()
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
            return _run(state.DB(db_path), cfg, dry_run, force_slow, only, now or util.now())
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _run(db, cfg, dry_run, force_slow, only, now):
    mode, switched = modes.effective(db, now)
    summary = {"mode": mode, "dry_run": dry_run, "accounts": {}, "switched_live": switched}
    if mode == "off" and not dry_run:
        db.set_meta("last_ok_at", now)
        return summary
    live = mode == "live" and not dry_run
    notifier = notify.Notifier(db, cfg, shadow=not live, now=now)
    if switched and not dry_run:
        users = cfg["identity"].get("owner_modal_users") or []
        stops = (" and stops crash-looping Modal apps deployed by %s (on accounts with auto_stop = true)"
                 % "/".join(users)) if users else "; it never stops anything itself (identity.owner_modal_users is empty)"
        notifier.send("[cloud-watchdog] Now live",
                      "The shadow period is over: from now on the watchdog messages the agents that own resources, "
                      "emails you%s. Reply \"back to shadow\" or \"off\" to change that." % stops
                      + messages.footer(None, cfg=cfg))

    # 1. collect, in parallel, one credentialed child per account
    jobs = {}
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        for account, acct in cfg["accounts"].items():
            if only and account not in only:
                continue
            h = db.one("select * from collector_health where account=?", account)
            slow = force_slow or not h or not h["last_slow_at"] or now - h["last_slow_at"] >= cfg["slow_interval_min"] * 60
            if acct.get("slow_only") and not slow:
                continue
            jobs[pool.submit(gather.op, account, acct, "collect", collect_opts(db, cfg, account, now, slow))] = (account, slow)
        results = [(jobs[f], f.result()) for f in cf.as_completed(jobs)]

    # 2. ingest
    for (account, slow), (ok, res) in sorted(results, key=lambda x: x[0][0]):
        summary["accounts"][account] = ingest_account(db, cfg, account, slow, ok, res, now)

    # 3. blind collectors
    for h in db.q("select * from collector_health where consecutive_failures>=? and alerted=0",
                  cfg["rules"]["blind"]["consecutive_failures"]):
        subject, md = messages.blind(h["account"], h, now, cfg=cfg)
        if live:
            notifier.email(subject, md, key=h["account"])
        else:
            db.add_digest("blind", h["account"], "%s: check failing (%s)" % (h["account"], (h["last_error"] or "")[:120]), now)
        db.x("update collector_health set alerted=1 where account=?", h["account"])

    # 4. account-level digest lines
    for account in cfg["accounts"]:
        for f in rules.account_findings(db, cfg, account, now):
            db.add_digest(f["kind"], f["key"], f["text"], now)
        if cfg["accounts"][account]["provider"] == "modal" and digest.digest_weekday(cfg, now) == 0:
            for f in rules.storage_findings(db, cfg, account, now):
                db.add_digest(f["kind"], f["key"], f["text"], now)

    # 5. quiet attribution of anything of ours that is billing, so `cloud-watch list` shows owners
    book = None
    pending = db.q("select * from resources where gone_at is null and billing_now=1 and ownership!='foreign' "
                   "and owner_session is null and (owner_checked_at is null or owner_checked_at<?) limit 5",
                   now - 6 * 3600)
    for res in pending:
        book = book if book is not None else owner.sessions()
        upd = owner.resolve(db, cfg, res, now, book=book)
        upd.pop("_candidates", None)
        if upd:
            db.update_resource(res["key"], **upd)

    # 6. the ladder
    ladder.Ladder(db, cfg, now, notifier, live=live).process()

    # 7. digest
    if dry_run:
        if digest.due(db, cfg, now):
            summary["digest"] = "due (not sent: dry run)"
    else:
        summary["digest_sent"] = digest.send_if_due(db, cfg, now, notifier)

    if int(now) % 86400 < 700:
        observe.prune(db, now)
    db.set_meta("last_ok_at", now)
    summary["would"] = [dict(a) for a in db.q("select ts, key, action, detail, shadow from actions where ts>=? "
                                                "order by ts", now)]
    return summary


def ingest_account(db, cfg, account, slow, ok, res, now):
    """Record one account's collection. One transaction: a check killed mid-ingest leaves the account exactly as
    the last complete check saw it, and the next tick collects it again."""
    if not ok:
        db.x("insert into collector_health(account, last_error, last_error_at, consecutive_failures) values(?,?,?,1) "
             "on conflict(account) do update set last_error=excluded.last_error, last_error_at=excluded.last_error_at, "
             "consecutive_failures=collector_health.consecutive_failures+1", account, str(res)[:500], now)
        return {"ok": False, "error": str(res)[:200]}
    with db.tx():
        gone = observe.ingest(db, cfg, account, res, now, slow)
        if res.get("spend"):
            spend.ingest(db, cfg, account, res["spend"], res.get("metrics") or {}, now)
        db.x("insert into collector_health(account, last_ok_at, last_slow_at, consecutive_failures, alerted) "
             "values(?,?,?,0,0) on conflict(account) do update set last_ok_at=excluded.last_ok_at, "
             "last_slow_at=coalesce(?, collector_health.last_slow_at), consecutive_failures=0, alerted=0",
             account, now, now if slow else None, now if slow else None)
    return {"ok": True, "slow": slow, "n": len([r for r in res["resources"] if not r.get("dormant")]), "gone": gone}
