"""The daily digest email: what is running, what the watchdog did (or would have done), spend."""
import datetime as dt
import json
from zoneinfo import ZoneInfo

from . import messages, modes, rules, spend, util

ACTION_WORDS = {"ping": "messaged owner", "resume": "resumed exited owner", "email": "emailed you",
                "stop": "stopped", "investigate": "ran investigator", "open": "flagged",
                "auto-stop-skip": "did not auto-stop", "resolve": "resolved"}


def local_now(cfg, now):
    """In the configured time zone, or the machine's own when none is set."""
    tz = cfg.get("timezone")
    return dt.datetime.fromtimestamp(now, ZoneInfo(tz)) if tz else dt.datetime.fromtimestamp(now).astimezone()


def due(db, cfg, now):
    t = local_now(cfg, now)
    return t.hour >= cfg["digest_hour_local"] and db.meta("digest_last_date") != t.date().isoformat()


def build(db, cfg, now):
    mode = modes.get(db)
    shadow = mode["mode"] == "shadow"
    lines = []
    if shadow:
        lines += ["**Shadow mode.** Nothing below was sent to any agent and nothing was stopped; "
                  "\"would\" marks what the live watchdog would have done.",
                  ("Goes live: **%s**. Reply \"stay in shadow\" (or \"off\") to change that." % util.iso(mode["live_at"], local=True))
                  if mode.get("live_at") else
                  "It stays in shadow until you run `cloud-watch mode live` (or reply \"go live\").", ""]
    running = [r for r in db.live_resources() if r["billing_now"] and r["ownership"] != "foreign"]
    lines.append("### Billing now (%d)" % len(running))
    if running:
        lines += ["| Resource | Account | Owner | Up | $/h | Cost | Flags |", "|---|---|---|---|---|---|---|"]
        for r in running:
            f = ", ".join(x["rule"] for x in rules.evaluate(r, cfg, now)) or "-"
            if rules.acked(r, now):
                f += " (acked until %s)" % util.iso(r["suppress_until"], local=True)
            up = now - r["launched_at"] if r["launched_at"] else None
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                r["name"], r["account"], r["owner_name"] or (r["owner_session"] or "")[:8] or "unknown",
                util.age(up), util.money(r["rate_usd_h"]) if r["rate_usd_h"] else "-",
                util.money(rules.streak_cost(r), est="estimate" in (r["cost_source"] or "")), f))
    else:
        lines.append("Nothing of ours is billing by the hour.")
    acts = db.q("select * from actions where ts>=? and action in ('open','ping','resume','email','stop','investigate',"
                "'auto-stop-skip','resolve') order by ts", now - 86400)
    lines += ["", "### %s (last 24 h)" % ("Would have done" if shadow else "Done")]
    if acts:
        for a in acts:
            word = ACTION_WORDS.get(a["action"], a["action"])
            if a["shadow"]:
                word = "would have " + word if not word.startswith("did not") else word
            detail = a["detail"]
            if detail and detail.startswith("{"):
                detail = (json.loads(detail).get("verdict") or {}).get("summary") or detail[:120]
            lines.append("- %s · %s · %s: %s" % (util.iso(a["ts"], local=True)[11:16], a["key"] or "-", word,
                                                 (detail or "")[:220]))
    else:
        lines.append("Nothing.")
    lines += ["", "### Spend"]
    for acct in cfg["accounts"]:
        rows = {m["name"]: m["value"] for m in db.q(
            "select name, value from metrics where account=? and ts=(select max(ts) from metrics where account=?)",
            acct, acct)}
        if rows:
            bits = []
            if "day_spend_usd" in rows:
                limit = (cfg["rules"].get("account_spend") or {}).get(acct)
                flag = " ⚠ over %s" % util.money(limit) if limit is not None and rows["day_spend_usd"] >= limit else ""
                bits.append("last 24 h %s%s" % (util.money(rows["day_spend_usd"]), flag))
            for k, lab in (("month_metered_usd", "month metered"), ("month_volumes_usd", "month volumes"),
                           ("month_spend_usd", "month"), ("month_estimated_usd", "month elastic")):
                if k in rows:
                    bits.append("%s %s" % (lab, util.money(rows[k])))
            lines.append("- `%s`: %s" % (acct, ", ".join(bits)))
            if cfg["accounts"][acct]["provider"] == "modal":
                lines += spend.lines(acct, db.meta("spend:" + acct))
    items = db.q("select * from digest_items where sent_at is null order by ts")
    weekday = digest_weekday(cfg, now)
    items = [i for i in items if i["kind"] != "storage" or weekday == 0]
    if items:
        lines += ["", "### Also"] + ["- " + i["text"] for i in items]
    bad = db.q("select * from collector_health where consecutive_failures > 0")
    if bad:
        lines += ["", "### Checks failing"] + ["- `%s`: %d in a row, last error `%s`" % (
            b["account"], b["consecutive_failures"], (b["last_error"] or "")[:160]) for b in bad]
    model_spend = db.model_spend_today(now)
    if model_spend:
        lines += ["", "Investigator spend, last 24 h: %s" % util.money(model_spend)]
    subject = "[cloud-watchdog] Daily%s: %d billing, %d flagged" % (
        " (shadow)" if shadow else "", len(running),
        sum(1 for r in running if rules.evaluate(r, cfg, now)))
    return subject, "\n".join(lines) + messages.footer(None, cfg=cfg), [i["id"] for i in items], bool(acts or running or items or bad)


def digest_weekday(cfg, now):
    return local_now(cfg, now).weekday()


def send_if_due(db, cfg, now, notifier, force=False):
    if not force and not due(db, cfg, now):
        return False
    subject, md, item_ids, has_content = build(db, cfg, now)
    # Sent whenever there is something to report, in shadow mode too (it is how shadow mode reports what it
    # would have done). A day with nothing billing and nothing done sends nothing.
    if has_content or force:
        ok = notifier.send(subject, md)
    else:
        ok = True
    if ok:
        db.set_meta("digest_last_date", local_now(cfg, now).date().isoformat())
        for i in item_ids:
            db.x("update digest_items set sent_at=? where id=?", now, i)
    return ok
