"""The daily digest email: what is running, what the watchdog did (or would have done) since the last digest, spend."""
import json

from . import messages, modes, rules, spend, util

# Each action the digest lists, as it reads when done and when shadow mode only logged it. A failed one (ok=0) reads
# "FAILED: " and the done form. Actions logged as `error` have their own section.
ACTIONS = {"open": ("flagged", "flagged"),
           "ping": ("messaged owner", "would have messaged owner"),
           "resume": ("resumed exited owner", "would have resumed exited owner"),
           "email": ("emailed you", "would have emailed you"),
           "stop": ("stopped", "would have stopped"),
           "investigate": ("ran investigator", "would have run investigator"),
           "auto-stop-skip": ("did not auto-stop", "did not auto-stop"),
           "resolve": ("resolved", "resolved"),
           "reappear": ("reappeared", "reappeared"),
           "failure-alert-skip": ("held back a check-failure email", "held back a check-failure email")}
SPEND_METRICS = (("day_spend_usd", "last 24 h"), ("month_metered_usd", "month metered"),
                 ("month_volumes_usd", "month volumes"), ("month_spend_usd", "month"),
                 ("month_estimated_usd", "month estimated"))
SENT_AT = "digest_sent_at"   # meta key: when the last digest went out


def due(db, cfg, now):
    t = util.local_time(now)
    return t.hour >= cfg["digest_hour_local"] and db.meta("digest_last_date") != t.date().isoformat()


def action_line(a, text):
    """A digest line for the logged action `a`: its local time, its resource and `text`."""
    return "- %s · %s · %s" % (util.iso(a["ts"], local=True)[11:16], a["key"] or "-", text)


def build(db, cfg, now):
    """(subject, markdown body, ids of the digest items it holds, whether it has anything to report). It lists the
    actions logged since the last digest went out (the first digest: the last 24 h)."""
    mode = modes.get(db)
    shadow = mode["mode"] == "shadow"
    since = db.meta(SENT_AT) or now - 86400
    lines = []
    if shadow:
        lines += ["**Shadow mode.** Nothing below was sent to any agent and nothing was stopped; "
                  "\"would\" marks what the live watchdog would have done.",
                  ("Goes live: **%s**. Reply \"stay in shadow\" or \"turn it off\" to change that."
                   % util.iso(mode["live_at"], local=True)) if mode.get("live_at") else
                  "It stays in shadow until you run `cloud-watch mode live` (or reply \"go live now\").", ""]
    running = db.ours_billing()
    lines.append("### Billing now (%d)" % len(running))
    if running:
        lines += ["| Resource | Account | Owner | Up | $/h | Cost | Flags |", "|---|---|---|---|---|---|---|"]
        for r in running:
            f = ", ".join(x["rule"] for x in rules.evaluate(r, cfg, now)) or "-"
            if rules.acked(r, now):
                f += " (acked until %s)" % util.iso(r["suppress_until"], local=True)
            up = now - r["launched_at"] if r["launched_at"] else None
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                messages.display_name(r), r["account"], messages.owner_label(r, short=True), util.age(up),
                util.money(r["rate_usd_h"]) if r["rate_usd_h"] else "-", messages.cost_text(r), f))
    else:
        lines.append("Nothing of ours is billing by the hour.")
    acts = db.q("select * from actions where ts>=? and action in (%s) order by ts" % ",".join("?" * len(ACTIONS)),
                since, *ACTIONS)
    lines += ["", "### %s (since %s)" % ("Would have done" if shadow else "Done", util.iso(since, local=True))]
    for a in acts:
        done, would = ACTIONS[a["action"]]
        word = would if a["shadow"] else done if a["ok"] else "FAILED: " + done
        detail = a["detail"]
        if detail and detail.startswith("{"):
            detail = (json.loads(detail).get("verdict") or {}).get("summary") or detail[:120]
        lines.append(action_line(a, "%s: %s" % (word, (detail or "")[:220])))
    if not acts:
        lines.append("Nothing.")
    errors = db.q("select * from actions where ts>=? and action='error' order by ts", since)
    if errors:
        lines += ["", "### Errors"] + [action_line(a, (a["detail"] or "")[:220]) for a in errors]
    lines += ["", "### Spend"]
    for acct in cfg["accounts"]:
        over = {f["metric"]: f["limit"] for f in rules.metric_findings(db, cfg, acct, weekly=False)}
        bits = []
        for name, label in SPEND_METRICS:
            value = db.latest_metric(acct, name)
            if value is not None:
                bits.append("%s %s%s" % (label, util.money(value),
                                         " ⚠ over %s" % util.money(over[name]) if name in over else ""))
        if bits:
            lines.append("- `%s`: %s" % (acct, ", ".join(bits)))
            lines += spend.lines(db.meta(spend.meta_key(acct)))   # the split of a Modal workspace's month
    items = db.q("select * from digest_items where sent_at is null order by ts")
    if items:
        lines += ["", "### Also"] + ["- " + i["text"] for i in items]
    bad = db.collectors(1)
    if bad:
        lines += ["", "### Checks failing"] + ["- `%s`: %d in a row, last error `%s`" % (
            b["account"], b["consecutive_failures"], (b["last_error"] or "")[:160]) for b in bad]
    model_spend = db.model_spend_today(now)
    if model_spend:
        lines += ["", "Investigator spend, last 24 h: %s" % util.money(model_spend)]
    head = messages.subject("Daily%s: %d billing, %d flagged" % (
        " (shadow)" if shadow else "", len(running), sum(1 for r in running if rules.evaluate(r, cfg, now))))
    return head, "\n".join(lines), [i["id"] for i in items], bool(acts or errors or running or items or bad)


def send_if_due(db, cfg, now, notifier):
    """Send the digest once a day, on the first check at or after digest_hour_local (local time), when there is
    something to report, in shadow mode too (it is how shadow mode reports what it would have done). A day with
    nothing to report sends none and logs a `digest` action saying so: a fresh install stays in shadow with no
    go-live date, and would otherwise mail an empty digest every day. Returns "not due", "sent", "skipped" or
    "failed" (agent-mail failed: the next check tries again)."""
    if not due(db, cfg, now):
        return "not due"
    head, md, item_ids, has_content = build(db, cfg, now)
    if has_content:
        if not notifier.send(head, md):  # really sent, also in shadow mode
            return "failed"
        db.set_meta(SENT_AT, now)
        for i in item_ids:
            db.x("update digest_items set sent_at=? where id=?", now, i)
        outcome = "sent"
    else:
        db.log("digest", "skipped: nothing to report", shadow=False, now=now)
        outcome = "skipped"
    db.set_meta("digest_last_date", util.local_time(now).date().isoformat())
    return outcome
