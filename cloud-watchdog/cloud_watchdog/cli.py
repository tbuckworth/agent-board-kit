"""cloud-watch: the command agents (and the human, via the orchestrator) use, and the timer's entry point.

    cloud-watch register PROVIDER:ACCOUNT:NAME_OR_PATTERN [--expect 6h] [--reason "..."] [--expect-cost USD]
    cloud-watch launch --expect 6h [--reason "..."] [--expect-cost USD] [--provider modal] -- <command ...>
    cloud-watch ack ID --keep 24h --reason "..." [--expect-cost USD]
    cloud-watch close ID [--reason "..."] [--confirm-terminate]
    cloud-watch disown ID [--reason "..."]
    cloud-watch list [--all] | show ID | status | accounts
    cloud-watch mode [shadow [--days N|never] | live | off] [--by WHO]
    cloud-watch check [--dry-run] [--slow] [--account A ...] [--json]
    cloud-watch digest [--send]
    cloud-watch whois ID                      # run owner resolution and print the evidence
    cloud-watch crashcheck ACCOUNT APP_ID [--window MIN] [--at ISO_TIME]
    cloud-watch heartbeat | alert-failure UNIT | install [--shadow-days N] | uninstall

register, launch, ack, close and disown also take [--session NAME_OR_ID] [--cli claude|codex] [--by human].
ID is an incident (wd-17), a resource key (modal:main:ap-...), or a unique resource name.
Inside Claude Code the caller's session comes from CLAUDE_CODE_SESSION_ID; Codex passes --session. --by human marks
a command as the human's decision (the orchestrator relaying an email reply).
"""
import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from . import (check, config, creds, digest, gather, messages, modes, notify, observe, owner, policy, providers,
               rules, spend, state, util)

CHECK_EVERY_MIN = 10     # the timer runs a check this often
CHECK_TIMEOUT_MIN = 8    # systemd stops a check that runs longer
CHECK_PEAK_MB = 160      # a check's peak memory
NOT_RUNNING = messages.subject("Not running")   # the heartbeat's email


def die(msg, code=1):
    print("cloud-watch: " + msg, file=sys.stderr)
    sys.exit(code)


# ----------------------------------------------------------------------- identity
def caller(args):
    """Who runs this command: actor (human | agent), session, cli, name, label (the one name the audit log and
    resource fields record: the session's name, else its id) and, for an agent, cwd.

    The session is --session (a name or id from `agent-send --list`) or else CLAUDE_CODE_SESSION_ID. Exits with an
    error when there is no session, or when --session matches no session or several, or cannot be looked up because
    `agent-send --list` fails. A session from CLAUDE_CODE_SESSION_ID then goes on by its id, with a warning on stderr."""
    sid = getattr(args, "session", None) or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if getattr(args, "by", None) == "human":
        name = messages.human(config.load())
        return {"actor": "human", "session": sid, "cli": None, "name": name, "label": name}
    if not sid:
        die("cannot tell which session you are: run inside Claude Code, or pass --session NAME_OR_ID (Codex)")
    cli = "claude" if os.environ.get("CLAUDE_CODE_SESSION_ID") == sid else (getattr(args, "cli", None) or "codex")
    try:
        book = owner.sessions()
    except owner.AddressBookError as e:
        if getattr(args, "session", None):
            die("cannot look up --session %s: %s" % (sid, e))
        # The id from CLAUDE_CODE_SESSION_ID names the session by itself; the address book only adds its name and cwd.
        print("cloud-watch: warning: %s; going on as session %s, without its name" % (e, sid), file=sys.stderr)
        book = {}
    entry = book.get(sid)
    if entry is None:  # not an id in the address book: try it as a session name
        hits = [e for e in book.values() if e.get("name") == sid]
        if len(hits) > 1:
            die("--session %s matches %d sessions (%s): pass the session id instead" % (
                sid, len(hits), ", ".join(e["id"] for e in hits)))
        if hits:
            entry, sid = hits[0], hits[0]["id"]
        elif getattr(args, "session", None):
            die("no session in `agent-send --list` has the id or name %s: pass your session's name or id as "
                "listed there" % sid)
    name = (entry or {}).get("name")
    return {"actor": "agent", "session": sid, "cli": (entry or {}).get("cli") or cli, "name": name,
            "label": name or sid, "cwd": (entry or {}).get("cwd") or os.getcwd()}


def resolve_target(db, ident):
    ident = ident.strip()
    inc_id = messages.parse_wid(ident)
    if inc_id is not None:
        inc = db.incident(inc_id)
        if inc is None:
            die("no incident %s" % ident)
        return db.resource(inc["key"]), inc
    res = db.resource(ident)
    if res is None:
        rows = db.q("select * from resources where (name=? or native_id=?) and gone_at is null", ident, ident)
        if len(rows) != 1:
            die("%s matches %d resources; use the key from `cloud-watch list`" % (ident, len(rows)))
        res = rows[0]
    return res, db.open_incident(res["key"])


def role(res, who):
    """owner | human | claim (unowned or only inferred) | agent (someone else's)."""
    if who["actor"] == "human":
        return "human"
    if res["owner_session"] == who["session"]:
        return "owner"
    if not res["owner_session"] or res["owner_source"] not in owner.AUTHORITATIVE_SOURCES:
        return "claim"
    return "agent"


def claim(db, res, who, now):
    db.update_resource(res["key"], **owner.assignment(session=who["session"], cli=who["cli"], name=who["name"],
                                                      source="claimed", confidence="high",
                                                      evidence="claimed by the session itself", now=now),
                       ownership="ours" if res["ownership"] != "foreign" else "foreign")


def refuse(db, command, res, inc, who, now, why):
    """Log `command` on `res` as a failed action of the caller, then exit with the reason it was refused."""
    db.log(command, "refused: " + why, key=res["key"], incident_id=inc["id"] if inc else None, actor=who["label"],
           ok=False, shadow=False, now=now)
    die("refused: " + why)


def notifier(db, cfg, now):
    """A live notifier for a command: what it sends really goes out."""
    return notify.Notifier(db, cfg, shadow=False, now=now, run=util.run, op=gather.op)


# ----------------------------------------------------------------------- commands
# Each takes the parsed arguments and main()'s context: the state database, the merged config and the time it started.
def cmd_register(args, db, cfg, now):
    if args.pattern.count(":") < 2:
        die("pattern must look like PROVIDER:ACCOUNT:NAME (e.g. modal:main:my-judge-*, lambda:main:<instance id>)")
    who = caller(args)
    reg = db.add_registration(args.pattern, who, args.expect, args.expect_cost, args.reason, now)
    # Apply this registration at once to anything already known.
    # Applied even to a resource this session disowned before: registering it again is a deliberate claim.
    for res in db.q("select key, name from resources where gone_at is null"):
        hit = observe.match_registration(db, res["key"], res["name"], now, (), reg_id=reg["id"])
        if hit is not None:
            observe.apply_registration(db, db.resource(res["key"]), hit, now)
            print("registered %s (already running) to this session" % res["key"])
    print("registered %s for %s%s" % (args.pattern, args.expect or "no fixed time",
                                      "" if args.reason else " (add --reason next time)"))


def cmd_ack(args, db, cfg, now):
    res, inc = resolve_target(db, args.id)
    who = caller(args)
    r = role(res, who)
    if r == "agent":
        refuse(db, "ack", res, inc, who, now, "%s is attributed to session %s (%s). If it is yours, ask %s, or "
               "have that session run `cloud-watch disown %s`." % (res["key"], messages.owner_label(res),
                                                                   res["owner_source"], messages.human(cfg), args.id))
    if r == "claim":
        claim(db, res, who, now)
    keep = util.parse_duration(args.keep)
    x, _ = observe.ack(db, res, now + keep, args.reason, who["label"], args.expect_cost, "acked", now)
    db.log("ack", "keep %s, X=%s: %s" % (args.keep, util.money(x), args.reason), key=res["key"],
           incident_id=inc["id"] if inc else None, actor=who["label"], shadow=False, now=now)
    print("acked %s until %s. %s is emailed if it reaches %s." % (
        res["key"], util.iso(now + keep, local=True), messages.human(cfg, capital=True),
        util.money(rules.cost_email_threshold(db.resource(res["key"]), cfg, now))))


def cmd_close(args, db, cfg, now):
    res, inc = resolve_target(db, args.id)
    who = caller(args)
    r = role(res, who)
    actor = {"human": "human", "owner": "owner", "claim": "owner", "agent": "agent"}[r]
    ok, why = policy.can_stop(res, actor, cfg, now, confirm_terminate=args.confirm_terminate)
    if not ok:
        refuse(db, "close", res, inc, who, now, why)
    if r == "claim":
        claim(db, res, who, now)
    stopped, detail = notifier(db, cfg, now).stop(db.resource(res["key"]), who["label"], inc["id"] if inc else None)
    if not stopped:
        die("stop failed: %s" % detail)
    if inc is not None:
        db.close_incident(inc["id"], "closed by %s: %s" % (who["label"], args.reason or ""), now)
    db.update_resource(res["key"], notes=(res["notes"] or "") + "\nclosed %s by %s: %s" % (
        util.iso(now), who["label"], args.reason or ""))
    print("stopped %s: %s. The next check confirms it is gone." % (res["key"], detail))


def cmd_disown(args, db, cfg, now):
    res, inc = resolve_target(db, args.id)
    who = caller(args)
    dis = set(json.loads(res["disowned"] or "[]"))
    if who["session"]:
        dis.add(who["session"])
    upd = {"disowned": json.dumps(sorted(dis)), "owner_checked_at": None}
    if res["owner_session"] in dis:
        upd.update(owner_session=None, owner_name=None, owner_source=None, owner_confidence=None,
                   owner_evidence="disowned by %s: %s" % (who["label"], args.reason))
    db.update_resource(res["key"], **upd)
    if inc is not None:
        db.update_incident(inc["id"], now, state="open", ping_count=0, deadline_at=None, investigated_at=None,
                           resumed_at=None)
    db.log("disown", args.reason or "", key=res["key"], incident_id=inc["id"] if inc else None,
           actor=who["label"], shadow=False, now=now)
    print("noted: %s is not yours. The owner search continues without you." % res["key"])


def cmd_launch(args, db, cfg, now):
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        die("usage: cloud-watch launch --expect 6h --reason ... -- <command>")
    who = caller(args)
    provider = args.provider or ("modal" if "modal" in command[0] or "modal" in " ".join(command[:3]) else
                                 "slurm" if "sbatch" in " ".join(command) else None)
    accounts = {k: v for k, v in cfg["accounts"].items() if (provider is None or v["provider"] == provider)
                and providers.get(v["provider"]).HAS_RESOURCES}
    if not accounts:
        # Still run the command: an agent told to launch through cloud-watch must not be blocked by a missing account.
        print("cloud-watch: no configured account%s has resources to watch, so nothing is registered; see "
              "`cloud-watch accounts`" % (" of provider %s" % provider if provider else ""), file=sys.stderr)
        sys.exit(subprocess.call(command))

    def snapshot(names):
        """{account: its resource keys} for each of these accounts, from a fast-pass collect with the check's own
        options, inventory only: the providers skip their health probes and lookups. A collection that failed is
        printed to stderr and left out."""
        keys, own_db = {}, state.DB()   # its own connection: the poll thread snapshots too
        try:
            for name in names:
                opts = check.collect_opts(own_db, cfg, name, util.now(), slow=False, inventory_only=True)
                ok, res = gather.op(accounts[name], "collect", opts)
                if ok:
                    keys[name] = {observe.key_for(name, r["native_id"]) for r in res["resources"]}
                else:
                    print("cloud-watch: could not list %s: %s" % (name, res), file=sys.stderr)
        finally:
            own_db.c.close()
        return keys

    # Only accounts listed before the command can show what it started.
    before = snapshot(accounts)
    found, stop = set(), threading.Event()

    def compare():
        """List the accounts again and add to `found` the keys that were not there before; returns the listing."""
        after = snapshot(before)
        for name, keys in after.items():
            found.update(keys - before[name])
        return after

    def poll():
        while not stop.wait(30):
            compare()

    t = threading.Thread(target=poll, daemon=True)
    t.start()
    code = subprocess.call(command)
    stop.set()
    unlisted = sorted(set(accounts) - set(compare()))   # failed before or after the command
    now = util.now()   # the command may have run for hours: register its resources at its end
    for key in sorted(found):
        db.add_registration(key, who, args.expect, args.expect_cost, args.reason, now)
        print("cloud-watch: registered %s (expect %s)" % (key, args.expect), file=sys.stderr)
    if not found and unlisted:
        print("cloud-watch: no new resource appeared, but %s could not be listed: register by hand anything the "
              "command started there" % ", ".join(unlisted), file=sys.stderr)
    elif not found:
        print("cloud-watch: no new resource appeared; register it by hand if it did start", file=sys.stderr)
    sys.exit(code)


LIST_ROW = "%-44s %-9s %-8s %8s %9s %10s  %-24s %s"   # `cloud-watch list`: its header and each resource


def fmt_res(r, cfg, now):
    up = now - r["launched_at"] if r["launched_at"] else None
    flags = ", ".join(f["rule"] for f in rules.evaluate(r, cfg, now)) if r["billing_now"] else ""
    return LIST_ROW % (
        messages.display_name(r)[:44], r["ownership"], "billing" if r["billing_now"] else "idle", util.age(up),
        util.money(r["rate_usd_h"]) + "/h" if r["rate_usd_h"] else "-",
        messages.cost_text(r) if r["cost_usd"] is not None else "-", messages.owner_label(r, short=True)[:24], flags)


def cmd_list(args, db, cfg, now):
    rows = db.live_resources() if args.all else db.ours_billing()
    print(LIST_ROW % ("RESOURCE", "WHOSE", "STATE", "UP", "RATE", "COST", "OWNER", "FLAGS"))
    for r in rows:
        inc = db.open_incident(r["key"])
        print(fmt_res(r, cfg, now) + ("  [%s %s]" % (messages.wid(inc["id"]), inc["state"]) if inc else ""))
    if not rows:
        print("(nothing of ours is billing; --all shows everything tracked)")


def cmd_show(args, db, cfg, now):
    res, inc = resolve_target(db, args.id)
    d = dict(res)
    for k in ("extra", "health_detail", "ssh_keys", "disowned"):
        if d.get(k):
            d[k] = json.loads(d[k])
    for k in ("first_seen", "last_seen", "gone_at", "launched_at", "active_since", "suppress_until", "owner_checked_at"):
        if d.get(k):
            d[k] = util.iso(d[k])
    print(json.dumps(d, indent=1, default=str))
    print("findings:", json.dumps(rules.evaluate(res, cfg, now), indent=1))
    if inc:
        print("incident:", json.dumps(dict(inc), indent=1, default=str))
    for a in db.q("select * from actions where key=? order by ts desc limit 15", res["key"]):
        print("  %s %-8s %-14s %s%s" % (util.iso(a["ts"]), a["actor"], a["action"], "(shadow) " if a["shadow"] else "",
                                        (a["detail"] or "")[:160]))


def cmd_status(args, db, cfg, now):
    print("mode:        %s" % modes.describe(db, now))
    last = db.meta(check.LAST_OK)
    print("last check:  %s (%s ago)" % (util.iso(last, local=True), util.age(now - last) if last else "-"))
    for h in db.collectors(0):
        print("  %-22s ok %-6s ago  slow %-6s ago  %s" % (
            h["account"], util.age(now - h["last_ok_at"]) if h["last_ok_at"] else "never",
            util.age(now - h["last_slow_at"]) if h["last_slow_at"] else "never",
            ("FAILING x%d: %s" % (h["consecutive_failures"], (h["last_error"] or "")[:80])) if h["consecutive_failures"] else ""))
    print("open incidents: %d" % db.one("select count(*) n from incidents where closed_at is null")["n"])
    print("emails 24h: %d   investigator 24h: %s" % (db.emails_today(now), util.money(db.model_spend_today(now))))
    for account in cfg["accounts"]:
        split = db.meta(spend.meta_key(account))   # the split of a Modal workspace's month
        if split:
            print("%s spend:" % account)
            for line in spend.lines(split):
                print("  " + spend.plain(line))


def cmd_accounts(args, db, cfg, now):
    """Each configured account, its provider, and where its keys come from (never the values)."""
    print("config: %s%s" % (config.CONFIG_PATH, "" if config.CONFIG_PATH.exists() else " (not created yet)"))
    print("owner:  %s <%s>" % (cfg["owner_name"], cfg["owner_email"] or "NO owner_email: emails cannot be sent"))
    ident = cfg["identity"]
    print("identity: modal users %s, lambda ssh keys %s" % (ident.get("owner_modal_users") or "(none: never auto-stops)",
                                                             ident.get("owner_lambda_ssh_keys") or "(none)"))
    if not cfg["accounts"]:
        print("no accounts: nothing is watched. Add [accounts.\"PROVIDER:NAME\"] tables to the config "
              "(example at the end of %s)." % config.DEFAULTS_PATH)
        return
    for name, acct in cfg["accounts"].items():
        flags = [f for f in ("auto_stop", "slow_only") if acct.get(f)]
        print("  %-22s %-10s %-10s %s%s" % (name, acct.get("provider") or "?", acct["secrets"], creds.describe(acct),
                                            ("  [%s]" % ", ".join(flags)) if flags else ""))


def cmd_mode(args, db, cfg, now):
    if not args.mode:
        print(modes.describe(db, now))
        return
    live_at = None
    if args.mode == "shadow":
        live_at = modes.live_at(now, None if args.days in ("never", None) else args.days)
    actor = args.by or os.environ.get("CLAUDE_CODE_SESSION_ID") or "cli"
    modes.set_mode(db, args.mode, now, live_at=live_at, by=actor)
    db.log("mode", "%s%s" % (args.mode, " until %s" % util.iso(live_at) if live_at else ""), actor=actor, shadow=False,
           now=now)
    print(modes.describe(db, now))


def cmd_check(args, db, cfg, now):
    s = check.run(cfg, dry_run=args.dry_run, force_slow=args.slow, only=args.account or None)
    if args.json:
        print(json.dumps(s, indent=1, default=str))
        return
    if "skipped" in s:
        print(s["skipped"])
        return
    print("mode %s%s" % (s["mode"], " (dry run)" if s["dry_run"] else ""))
    for a, v in sorted(s["accounts"].items()):
        print("  %-22s %s" % (a, ("ok%s, %d tracked%s" % (" +billing" if v["slow"] else "", v["n"],
                                                       ", gone: %s" % v["gone"] if v["gone"] else ""))
                              if v["ok"] else "FAILED: " + v["error"]))
        for text in v.get("warnings", []):
            print("    warning: %s" % text)
    for w in s.get("would", []):
        print("  %s %-12s %s" % ("would" if w["shadow"] else "did  ", w["action"], (w["detail"] or "")[:150]))


def cmd_digest(args, db, cfg, now):
    subject, md, _, _ = digest.build(db, cfg, now)
    if args.send:
        if not notifier(db, cfg, now).send(subject, md):
            die("digest not sent: agent-mail failed (its output is in the audit log)")
        print("sent")
    else:
        print(subject + "\n\n" + md)


def cmd_whois(args, db, cfg, now):
    res, _ = resolve_target(db, args.id)
    try:
        book = owner.sessions()
    except owner.AddressBookError as e:
        print("cloud-watch: warning: %s; candidates get no live tie-break and no names" % e, file=sys.stderr)
        book = None
    upd = owner.resolve(cfg, res, now, book)
    for c in upd.pop("_candidates", []) or []:
        print("  candidate %s %-6s score %4.1f launch=%s %s  %s" % (c["session_id"][:8], c["cli"], c["score"],
                                                                   c["launch_hit"], util.iso(c["launch_ts"]), c["cwd"]))
    print(json.dumps(upd, indent=1, default=str))


def cmd_crashcheck(args, db, cfg, now):
    window = args.window if args.window is not None else cfg["rules"]["crash_loop"]["window_min"]
    ok, res = gather.op(cfg["accounts"][args.account], "crash_check", {"window_min": window, "at": args.at},
                        args.app_id)
    print(json.dumps(res, indent=1, default=str))
    if ok:
        print("crash candidate:", observe.crash_candidate(res, cfg["rules"]["crash_loop"]))


def cmd_heartbeat(args, db, cfg, now):
    if modes.get(db)["mode"] == "off":
        return
    last = db.meta(check.LAST_OK) or 0
    stale = now - last > cfg["heartbeat_stale_min"] * 60
    if stale and not db.meta("heartbeat_alerted"):
        sent = notifier(db, cfg, now).send(
            NOT_RUNNING, "The last successful check was %s (%s ago). Check "
            "`systemctl --user status cloud-watchdog.timer` and `journalctl --user -u cloud-watchdog`." % (
                util.iso(last, local=True), util.age(now - last)))
        if not sent:
            die("\"Not running\" email not sent: agent-mail failed (its output is in the audit log); the next "
                "heartbeat tries again")
        db.set_meta("heartbeat_alerted", True)
    elif not stale:
        db.set_meta("heartbeat_alerted", False)


FAILURE_HINTS = {
    "oom-kill": "The kernel's OOM killer killed a process of this run. A check peaks at ~%d MB, so this almost "
                "always means the whole machine ran out of memory and the watchdog was a bystander: see the kernel's "
                "kills and the biggest processes below." % CHECK_PEAK_MB,
    "signal": "The check was killed by a signal. If it was SIGKILL, it was most likely the kernel's OOM killer after "
              "the whole machine ran out of memory: see the kernel's kills and the biggest processes below.",
    "timeout": "The check ran past its %d-minute limit: a provider CLI hung, or the machine was thrashing."
               % CHECK_TIMEOUT_MIN,
    "exit-code": "The check exited with an error; the traceback is in the log lines below.",
}


def meminfo():
    try:
        with open("/proc/meminfo") as f:
            kb = {line.split(":")[0]: int(line.split()[1]) for line in f}
        return "Memory now: %.1f of %.1f GB available, swap %.1f of %.1f GB free." % (
            kb["MemAvailable"] / 2**20, kb["MemTotal"] / 2**20, kb["SwapFree"] / 2**20, kb["SwapTotal"] / 2**20)
    except (OSError, KeyError, ValueError, IndexError):
        return ""


def failure_report(unit, cfg, run=util.run, mem=meminfo):
    """Subject and markdown body for a failed check: what systemd says happened, and, if memory ran out, who used it.
    The body says when the human hears again (failure_alert_every_hours, heartbeat_stale_min)."""
    _, result, _ = run(["systemctl", "--user", "show", "-p", "Result", "--value", unit], timeout=30)
    result = result.strip() or "unknown"
    _, log, _ = run(["journalctl", "--user", "-u", unit, "-n", "15", "--no-pager"], timeout=30)
    parts = [("`%s` failed (systemd result: `%s`). %s" % (unit, result, FAILURE_HINTS.get(result, ""))).strip(),
             "Last log lines:\n\n```\n%s\n```" % log[-2500:]]
    if result in ("oom-kill", "signal"):
        _, kern, _ = run(["journalctl", "-k", "--since", "-30min", "--no-pager", "-o", "short",
                          "--grep", "Out of memory: Killed process"], timeout=30)
        kills = [line for line in kern.splitlines() if "Killed process" in line]
        if kills:
            parts.append("Kernel OOM kills in the last 30 min (%d in total; the latest %d shown):\n\n```\n%s\n```" % (
                len(kills), min(len(kills), 8), "\n".join(kills[-8:])))
        _, top, _ = run(["ps", "-eo", "pid,rss,etime,comm", "--sort=-rss"], timeout=30)
        if top.strip():
            parts.append("Biggest processes now (RSS in KB):\n\n```\n%s\n```" % "\n".join(top.splitlines()[:9]))
        parts.append(mem())
    parts.append("The timer keeps running and retries every %d minutes. Further failures in the next %g h are not "
                 "emailed; if no check succeeds for %g min you get \"%s\"." % (
                     CHECK_EVERY_MIN, cfg["failure_alert_every_hours"], cfg["heartbeat_stale_min"], NOT_RUNNING))
    return messages.subject("Check failed (%s)" % result), "\n\n".join(p for p in parts if p)


def cmd_alert_failure(args, db, cfg, now):
    last = db.meta("failure_alerted_at") or 0
    if now - last < cfg["failure_alert_every_hours"] * 3600:
        db.log("failure-alert-skip", "%s failed again; not emailed, as the last failure email went at %s (one every "
               "%g h at most)" % (args.unit, util.iso(last, local=True), cfg["failure_alert_every_hours"]),
               shadow=False, now=now)
        return
    subject, md = failure_report(args.unit, cfg)
    if not notifier(db, cfg, now).send(subject, md):
        die("failure email not sent: agent-mail failed (its output is in the audit log); the next failure tries again")
    db.set_meta("failure_alerted_at", now)


UNIT_DIR = config.REPO_DIR / "systemd"
TIMERS = ["cloud-watchdog.timer", "cloud-watchdog-heartbeat.timer"]


def units(python, repo):
    """{name: text} of the systemd units `install` writes, from systemd/*.in (and plain unit files) with {{PYTHON}}
    and {{KIT_DIR}} filled in, which run `python` on the checkout at `repo` (the kit's cloud-watchdog folder). The
    kit installer renders the same files."""
    repo = Path(repo)
    out = {}
    for src in sorted(UNIT_DIR.iterdir()):
        if src.suffix not in (".in", ".service", ".timer"):
            continue
        text = src.read_text().replace("{{KIT_DIR}}/cloud-watchdog", str(repo))
        text = text.replace("{{KIT_DIR}}", str(repo.parent)).replace("{{PYTHON}}", python)
        out[src.name[:-3] if src.suffix == ".in" else src.name] = text
    return out


def cmd_install(args, db, cfg, now):
    """Manual install, for use without the kit installer: units, a `cloud-watch` link if none exists, mode."""
    unit_dir = config.HOME / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name, text in units(sys.executable, config.REPO_DIR).items():
        (unit_dir / name).write_text(text)
    link = config.LOCAL_BIN / "cloud-watch"
    if not (link.is_symlink() or link.exists()):   # never replace the kit installer's wrapper
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(config.REPO_DIR / "bin" / "cloud-watch")
    m = modes.get(db)
    if m["set_by"] == "default":
        days = args.shadow_days if args.shadow_days is not None else cfg["shadow_days"]
        modes.set_mode(db, "shadow", now, live_at=modes.live_at(now, days if days and days > 0 else None),
                       by="install")
    for cmd in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now"] + TIMERS):
        code, out, err = util.run(cmd, timeout=60)
        if code:
            die("%s failed: %s" % (" ".join(cmd), err.strip()))
    print("installed: units in %s, mode: %s" % (unit_dir, modes.describe(db, now)))
    if not cfg["accounts"]:
        print("no accounts configured yet: add them to %s (see `cloud-watch accounts`)" % config.CONFIG_PATH)


def cmd_uninstall(args, db, cfg, now):
    cmd = ["systemctl", "--user", "disable", "--now"] + TIMERS
    code, _, err = util.run(cmd)
    if code:
        die("%s failed: %s" % (" ".join(cmd), err.strip()))
    print("timers disabled; state kept in %s" % config.STATE_DIR)


def main(argv=None):
    p = argparse.ArgumentParser(prog="cloud-watch", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def who_args(sp):
        sp.add_argument("--session", help="your session name or id (Codex; Claude is detected)")
        sp.add_argument("--cli", choices=["claude", "codex"])
        sp.add_argument("--by", choices=["human"], help="the human's decision (the orchestrator relaying a reply)")

    sp = sub.add_parser("register"); sp.add_argument("pattern"); sp.add_argument("--expect")
    sp.add_argument("--expect-cost", type=float); sp.add_argument("--reason", default=""); who_args(sp)
    sp.set_defaults(fn=cmd_register)
    sp = sub.add_parser("launch"); sp.add_argument("--expect", required=True); sp.add_argument("--expect-cost", type=float)
    sp.add_argument("--reason", default=""); sp.add_argument("--provider"); who_args(sp)
    sp.add_argument("command", nargs=argparse.REMAINDER); sp.set_defaults(fn=cmd_launch)
    sp = sub.add_parser("ack"); sp.add_argument("id"); sp.add_argument("--keep", required=True)
    sp.add_argument("--expect-cost", type=float); sp.add_argument("--reason", required=True); who_args(sp)
    sp.set_defaults(fn=cmd_ack)
    sp = sub.add_parser("close"); sp.add_argument("id"); sp.add_argument("--reason", default="")
    sp.add_argument("--confirm-terminate", action="store_true"); who_args(sp); sp.set_defaults(fn=cmd_close)
    sp = sub.add_parser("disown"); sp.add_argument("id"); sp.add_argument("--reason", default=""); who_args(sp)
    sp.set_defaults(fn=cmd_disown)
    sp = sub.add_parser("list"); sp.add_argument("--all", action="store_true"); sp.set_defaults(fn=cmd_list)
    sp = sub.add_parser("show"); sp.add_argument("id"); sp.set_defaults(fn=cmd_show)
    sp = sub.add_parser("status"); sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("accounts"); sp.set_defaults(fn=cmd_accounts)
    sp = sub.add_parser("mode"); sp.add_argument("mode", nargs="?", choices=["shadow", "live", "off"])
    sp.add_argument("--days", help="with shadow: days until automatic go-live, or 'never'", default="never")
    sp.add_argument("--by", help="who changed it (free text, e.g. human)"); sp.set_defaults(fn=cmd_mode)
    sp = sub.add_parser("check"); sp.add_argument("--dry-run", action="store_true"); sp.add_argument("--slow", action="store_true")
    sp.add_argument("--account", action="append"); sp.add_argument("--json", action="store_true"); sp.set_defaults(fn=cmd_check)
    sp = sub.add_parser("digest"); sp.add_argument("--send", action="store_true")
    sp.add_argument("--print", action="store_true", help="print it without sending (the default)")
    sp.set_defaults(fn=cmd_digest)
    sp = sub.add_parser("whois"); sp.add_argument("id"); sp.set_defaults(fn=cmd_whois)
    sp = sub.add_parser("crashcheck"); sp.add_argument("account"); sp.add_argument("app_id")
    sp.add_argument("--window", type=int, help="minutes (default: rules.crash_loop.window_min)")
    sp.add_argument("--at", help="ISO time: backtest a past window")
    sp.set_defaults(fn=cmd_crashcheck)
    sp = sub.add_parser("heartbeat"); sp.set_defaults(fn=cmd_heartbeat)
    sp = sub.add_parser("alert-failure"); sp.add_argument("unit"); sp.set_defaults(fn=cmd_alert_failure)
    sp = sub.add_parser("install"); sp.add_argument("--shadow-days", type=float); sp.set_defaults(fn=cmd_install)
    sp = sub.add_parser("uninstall"); sp.set_defaults(fn=cmd_uninstall)
    args = p.parse_args(argv)
    args.fn(args, state.DB(), config.load(), util.now())
