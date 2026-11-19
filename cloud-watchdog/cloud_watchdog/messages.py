"""Text of the owner messages and the emails about resources and accounts. Markdown; emails are rendered to HTML by
notify.py, which also adds the footer (how to answer) to each.

Emails go to the human the watchdog works for (config owner_name / owner_email). Agent messages call
that person by name, so an agent knows who will be asked next.
"""
import json

from . import providers, rules, util

PREFIX = "[cloud-watchdog"   # starts every subject and owner message; transcripts.py skips the lines that hold it
WID_PREFIX = "wd-"


def human(cfg=None, capital=False):
    """The name of the person the watchdog works for (config owner_name), else "the owner"."""
    name = (cfg or {}).get("owner_name") or "the owner"
    return name[:1].upper() + name[1:] if capital else name


def subject(text, inc_id=None):
    """A subject line: PREFIX, the incident's id when there is one, then `text`."""
    return "%s%s] %s" % (PREFIX, " " + wid(inc_id) if inc_id else "", text)


def wid(inc_id):
    """An incident's id as commands and messages write it (wd-17)."""
    return "%s%d" % (WID_PREFIX, inc_id)


def parse_wid(text):
    """The incident id that `text` names (wd-17, in any case), or None when it is not an incident id."""
    text = text.strip().lower()
    number = text[len(WID_PREFIX):]
    return int(number) if text.startswith(WID_PREFIX) and number.isdigit() else None


def display_name(res):
    """What a person knows the resource as: its name, else its provider id."""
    return res["name"] or res["native_id"]


def owner_label(res, short=False):
    """Who owns the resource, for a person: the owner session's name, else its id (the first 8 characters with
    `short`), else "unknown"."""
    session = res["owner_session"] or ""
    return res["owner_name"] or (session[:8] if short else session) or "unknown"


def cost_text(res):
    """What the resource has cost in its active stretch, marked ≈ when the provider's figure is an estimate."""
    return util.money(rules.streak_cost(res), est="estimate" in (res["cost_source"] or ""))


def label(res):
    return "%s (%s)" % (display_name(res), res["key"])


def facts(res, now):
    extra = json.loads(res["extra"] or "{}")
    lines = []
    up = (now - res["launched_at"]) if res["launched_at"] else None
    lines.append("Up since %s (%s)." % (util.iso(res["launched_at"]), util.age(up)) if up else "Launch time unknown.")
    if rules.streak_cost(res) is not None:
        lines.append("Cost so far: %s (%s)." % (cost_text(res), res["cost_source"] or "?"))
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


def ping(inc, res, findings, now, wait_min, will_stop, cfg=None):
    """The owner message. `will_stop`: whether the watchdog will stop the resource if nobody answers in time. `cfg`
    names the human the watchdog asks next."""
    w = wid(inc["id"])
    mod = providers.get(res["provider"])
    head = subject("Is this yours? %s" % label(res), inc["id"])
    why = "; ".join(f["text"] for f in findings)
    then = ("If nobody answers in %d min I will stop it (%s) and tell %s." % (wait_min, mod.STOP_EFFECT, human(cfg))
            if will_stop else "If nobody answers in %d min I will ask %s." % (wait_min, human(cfg)))
    close = ("  cloud-watch close %s --reason \"...\"               # stop it now: %s" % (w, mod.STOP_EFFECT)
             if hasattr(mod, "stop") else
             "  (cloud-watch cannot close %s resources: stop it in the provider's dashboard)" % res["provider"])
    body = [head, "Flagged: %s." % why] + facts(res, now) + [
        "Why I think it is yours: %s" % (res["owner_evidence"] or "?"),
        "",
        "Please run exactly ONE of:",
        close,
        "  cloud-watch ack %s --keep 24h --reason \"...\"      # still needed; optional --expect-cost USD" % w,
        "  cloud-watch disown %s --reason \"...\"              # not yours" % w,
        then,
    ]
    return "\n".join(body)


def resume_turn(inc, res, findings, now, wait_min, will_stop, cfg=None):
    """For an owner session that has exited: one resumed turn, answer only this."""
    return ("This session was resumed by cloud-watchdog for ONE question. Answer it, run the one command, "
            "and stop: do not continue any other work.\n\n" + ping(inc, res, findings, now, wait_min, will_stop, cfg))


def footer(inc_id, shadow_days, cfg=None):
    """How to answer an email about incident `inc_id` (None: no incident): the commands the human's reply maps to.
    `shadow_days` is how long the shadow period that "stay in shadow" starts lasts (0: no end)."""
    # The same reply -> command table is in docs/orchestrator-section.md, "cloud-watchdog threads": change both.
    w = wid(inc_id) if inc_id else "<id>"
    name = human(cfg)
    shadow = ("`cloud-watch mode shadow --days %g --by human`" % shadow_days if shadow_days
              else "`cloud-watch mode shadow --by human`")
    lines = ["", "", "---", "",
             "*How to answer:* reply in plain words (\"close it\", \"keep it 48h\", \"stay in shadow\", "
             "\"turn it off\", \"go live now\").",
             "*For the orchestrator:* %s's reply maps to one command, run on the machine cloud-watchdog runs on:" % name,
             "`cloud-watch close %s --by human --reason \"%s: <their words>\"` · "
             "`cloud-watch ack %s --by human --keep <duration> --reason \"%s: <their words>\"` · "
             "%s (stay in shadow) · `cloud-watch mode off --by human` · "
             "`cloud-watch mode live --by human`. Then confirm in-thread with the command's output."
             % (w, name, w, name, shadow)]
    return "\n".join(lines)


def escalation(inc, res, findings, now, reason, recommendation, stop_at=None):
    head = subject("%s: %s, owner not reached" % (display_name(res), ", ".join(sorted({f["rule"] for f in findings}))),
                   inc["id"])
    body = ["**%s** in `%s`" % (display_name(res), res["account"]), ""]
    body += ["- " + f["text"] for f in findings]
    body += ["- " + l for l in facts(res, now)]
    body += ["", "**Owning agent:** %s" % reason]
    if res["owner_evidence"]:
        body += ["", "Evidence: %s" % res["owner_evidence"]]
    body += ["", "**Recommendation:** %s" % recommendation]
    if stop_at:
        body += ["", "**Unless you say keep, I will stop it at %s** (%s)." % (
            util.iso(stop_at, local=True), providers.get(res["provider"]).STOP_EFFECT)]
    return head, "\n".join(body)


def auto_stopped(inc, res, now, ok, detail):
    head = subject("%s %s" % ("Stopped crash-looping" if ok else "FAILED to stop", display_name(res)), inc["id"])
    body = ["%s `%s` in `%s`." % ("Stopped" if ok else "Tried and failed to stop", display_name(res), res["account"]),
            ""]
    body += ["- " + l for l in facts(res, now)]
    body += ["", "Owning agent: %s" % owner_label(res),
             "Action: `modal app stop %s`: %s" % (res["native_id"], detail),
             "", "Nothing was deleted: the app can be started again with `modal deploy` or `modal run`; volumes, "
             "secrets and images are untouched."]
    return head, "\n".join(body)


def cost_email(inc_id, res, finding, now):
    head = subject("%s has cost %s" % (display_name(res), cost_text(res)), inc_id)
    body = ["**%s** in `%s`: %s." % (display_name(res), res["account"], finding["text"]), ""]
    body += ["- " + l for l in facts(res, now)]
    body += ["", "Owning agent: %s%s" % (owner_label(res),
                                  ("; acked until %s: %s" % (util.iso(res["suppress_until"]), res["suppress_reason"]))
                                  if res["suppress_until"] and res["suppress_until"] > now else "")]
    # The threshold after this tier: next_tier(start, already emailed) with both set to this tier.
    body += ["", "Next email at %s unless it stops." % util.money(rules.next_tier(finding["tier"], finding["tier"]))]
    return head, "\n".join(body)


def foreign_crash(res, now):
    who = util.launched_by(res)
    head = subject("Someone else's app crash-looping: %s (%s)" % (display_name(res), who or "unknown"))
    body = ["`%s` in `%s` is crash-looping. It was started by **%s**, not by you, so I will not touch it." % (
        display_name(res), res["account"], who or "someone else"), ""]
    body += ["- " + l for l in facts(res, now)]
    body += ["", "The watchdog never contacts other people, so they have not been told. Forward this if you like."]
    return head, "\n".join(body)


def blind(account, row):
    head = subject("Can't see %s" % account)
    body = ["The `%s` check has failed %d times in a row since %s." % (account, row["consecutive_failures"],
                                                                       util.iso(row["last_ok_at"]) if row["last_ok_at"] else "install"),
            "", "Last error: `%s`" % (row["last_error"] or "?"),
            "", "Until it recovers I cannot see what is running there. Common causes: an expired or rotated key, a missing "
                "or unreadable credentials file (`cloud-watch accounts` shows where each account's keys come from), "
                "or an API change."]
    return head, "\n".join(body)
