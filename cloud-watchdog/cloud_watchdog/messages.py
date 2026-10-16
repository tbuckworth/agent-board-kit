"""Text of every message the watchdog sends. Markdown; emails are rendered to HTML by notify.py.

Emails go to the human the watchdog works for (config owner_name / owner_email). Agent messages call
that person by name, so an agent knows who will be asked next.
"""
import json

from . import rules, util


def human(cfg=None):
    return (cfg or {}).get("owner_name") or "the owner"


def label(res):
    return "%s (%s)" % (res["name"] or res["native_id"], res["key"])


def facts(res, now):
    extra = json.loads(res["extra"] or "{}")
    cost = rules.streak_cost(res)
    lines = []
    up = (now - res["launched_at"]) if res["launched_at"] else None
    lines.append("Up since %s (%s)." % (util.iso(res["launched_at"]), util.age(up)) if up else "Launch time unknown.")
    if cost is not None:
        lines.append("Cost so far: %s (%s)." % (util.money(cost, est="estimate" in (res["cost_source"] or "")),
                                                 res["cost_source"] or "?"))
    if res["rate_usd_h"]:
        lines.append("Burning about %s/h (%s)." % (util.money(res["rate_usd_h"]), res["rate_source"] or "?"))
    if res["health"] == "crash_loop":
        d = json.loads(res["health_detail"] or "{}")
        lines.append("Crash loop: %s of %s containers failed in the last %s min (last failure %s): \"%s\"." % (
            d.get("failed"), d.get("seen"), d.get("window_min"), util.iso(d.get("last_failure_at")), d.get("sample", "")))
    if res["health"] == "idle":
        lines.append("GPU idle: %s." % extra.get("idle_detail"))
    if res["deployed_by"] or res["git_commit"]:
        lines.append("Deployed by %s, commit %s." % (res["deployed_by"] or "?", res["git_commit"] or "?"))
    elif "created_by" in res.keys() and res["created_by"]:
        lines.append("Started (`modal run`) by %s." % res["created_by"])
    if res["ssh_keys"]:
        lines.append("SSH keys: %s." % ", ".join(json.loads(res["ssh_keys"])))
    if extra.get("instance_type"):
        lines.append("Type: %s%s." % (extra["instance_type"], (" in " + extra["region"]) if extra.get("region") else ""))
    return lines


def wid(inc):
    return "wd-%d" % inc["id"]


def ping(inc, res, findings, now, wait_min, auto_stop=False, cfg=None):
    """auto_stop: policy would let the watchdog stop this crash loop itself if nobody answers."""
    w = wid(inc)
    head = "[cloud-watchdog %s] Is this yours? %s" % (w, label(res))
    why = "; ".join(f["text"] for f in findings)
    then = ("If nobody answers in %d min I will stop it (`modal app stop`; volumes, secrets and images are kept) "
            "and tell %s." % (wait_min, human(cfg))) if auto_stop else \
        "If nobody answers in %d min I will ask %s." % (wait_min, human(cfg))
    body = [head, "Flagged: %s." % why] + facts(res, now) + [
        "Why I think it is yours: %s" % (res["owner_evidence"] or "?"),
        "",
        "Please run exactly ONE of:",
        "  cloud-watch close %s --reason \"...\"               # stop it now (graceful; data kept)" % w,
        "  cloud-watch ack %s --keep 24h --reason \"...\"      # still needed; optional --expect-cost USD" % w,
        "  cloud-watch disown %s --reason \"...\"              # not yours" % w,
        then,
    ]
    return "\n".join(body)


def resume_turn(ping_text):
    """For an owner session that has exited: one resumed turn, answer only this."""
    return ("This session was resumed by cloud-watchdog for ONE question. Answer it, run the one command, "
            "and stop: do not continue any other work.\n\n" + ping_text)


def footer(inc_id=None, extra=None, cfg=None):
    w = "wd-%d" % inc_id if inc_id else "<id>"
    lines = ["", "", "---", "", "*How to answer:* reply in plain words (\"close it\", \"keep it 48h\", \"stay in shadow\").",
             "*For the orchestrator:* %s's reply maps to one command, run on the machine cloud-watchdog runs on:"
             % human(cfg),
             "`cloud-watch close %s --by human --reason \"<their words>\"` · "
             "`cloud-watch ack %s --by human --keep <duration> --reason \"<their words>\"` · "
             "`cloud-watch mode shadow --by human` (stay in shadow) · `cloud-watch mode off --by human`. "
             "Then confirm in-thread with the command's output." % (w, w)]
    if extra:
        lines.insert(2, extra)
    return "\n".join(lines)


def escalation(inc, res, findings, now, reason, recommendation, crash, stop_at=None, cfg=None):
    w = wid(inc)
    subject = "[cloud-watchdog %s] %s: %s, owner not reached" % (w, res["name"] or res["native_id"],
                                                                   ", ".join(sorted({f["rule"] for f in findings})))
    body = ["**%s** in `%s`" % (res["name"] or res["native_id"], res["account"]), ""]
    body += ["- " + f["text"] for f in findings]
    body += ["- " + l for l in facts(res, now)]
    body += ["", "**Owning agent:** %s" % reason]
    if res["owner_evidence"]:
        body += ["", "Evidence: %s" % res["owner_evidence"]]
    body += ["", "**Recommendation:** %s" % recommendation]
    if stop_at:
        body += ["", "**Unless you say keep, I will stop it at %s** (`modal app stop`; data kept)." % util.iso(stop_at, local=True)]
    return subject, "\n".join(body) + footer(inc["id"], cfg=cfg)


def auto_stopped(inc, res, findings, now, ok, detail, cfg=None):
    w = wid(inc)
    subject = "[cloud-watchdog %s] %s %s" % (w, "Stopped crash-looping" if ok else "FAILED to stop",
                                              res["name"] or res["native_id"])
    body = ["%s `%s` in `%s`." % ("Stopped" if ok else "Tried and failed to stop", res["name"], res["account"]), ""]
    body += ["- " + l for l in facts(res, now)]
    body += ["", "Owning agent: %s" % (res["owner_name"] or res["owner_session"] or "not found"),
             "Action: `modal app stop %s`: %s" % (res["native_id"], detail),
             "", "Nothing was deleted: the deployment can be redeployed with `modal deploy`; volumes, secrets and "
             "images are untouched."]
    return subject, "\n".join(body) + footer(inc["id"], cfg=cfg)


def cost_email(inc_id, res, finding, now, cfg=None):
    subject = "[cloud-watchdog%s] %s has cost %s" % (" wd-%d" % inc_id if inc_id else "", res["name"] or res["native_id"],
                                                       util.money(rules.streak_cost(res)))
    body = ["**%s** in `%s`: %s." % (res["name"], res["account"], finding["text"]), ""]
    body += ["- " + l for l in facts(res, now)]
    body += ["", "Owning agent: %s%s" % (res["owner_name"] or res["owner_session"] or "unknown",
                                  ("; acked until %s: %s" % (util.iso(res["suppress_until"]), res["suppress_reason"]))
                                  if res["suppress_until"] and res["suppress_until"] > now else "")]
    body += ["", "Next email at %s unless it stops." % util.money(finding["tier"] * 2)]
    return subject, "\n".join(body) + footer(inc_id, cfg=cfg)


def foreign_crash(res, now, cfg=None):
    who = util.modal_user(res)
    subject = "[cloud-watchdog] Someone else's app crash-looping: %s (%s)" % (res["name"], who or "unknown")
    body = ["`%s` in `%s` is crash-looping. It was started by **%s**, not by you, so I will not touch it." % (
        res["name"], res["account"], who or "someone else"), ""]
    body += ["- " + l for l in facts(res, now)]
    body += ["", "The watchdog never contacts other people, so they have not been told. Forward this if you like."]
    return subject, "\n".join(body) + footer(None, cfg=cfg)


def blind(account, row, now, cfg=None):
    subject = "[cloud-watchdog] Can't see %s" % account
    body = ["The `%s` check has failed %d times in a row since %s." % (account, row["consecutive_failures"],
                                                                       util.iso(row["last_ok_at"]) if row["last_ok_at"] else "install"),
            "", "Last error: `%s`" % (row["last_error"] or "?"),
            "", "Until it recovers I cannot see what is running there. Common causes: an expired or rotated key, a missing "
                "or unreadable credentials file (`cloud-watch accounts` shows where each account's keys come from), "
                "or an API change."]
    return subject, "\n".join(body) + footer(None, cfg=cfg)
