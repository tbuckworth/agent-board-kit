"""Rules. Each is independent (any one firing is enough); inside a rule every condition must hold.

A finding says who should hear about it:
  owner  -> the owning agent gets a message (the ladder handles waits and escalation)
  human  -> the human gets an email (cost tiers; these are not silenced by an ack)
  foreign_crash -> somebody else's crash-looping app: email the human, never stop
"""
from . import util


def streak_cost(res):
    if res["cost_usd"] is None:
        return None
    return max(0.0, res["cost_usd"] - (res["cost_basis"] or 0.0))


def acked(res, now):
    return bool(res["suppress_until"] and res["suppress_until"] > now)


def next_tier(start, current_tier):
    """The first email threshold at or above `start`, doubling past what was already emailed."""
    t = start
    while t <= (current_tier or 0):
        t *= 2
    return t


def cost_email_threshold(res, cfg, now):
    c = cfg["rules"]["cost"]
    if acked(res, now) and res["ack_x_usd"] is not None:
        start = max(c["acked_floor_usd"], c["acked_multiple"] * res["ack_x_usd"])
    else:
        start = c["email_owner_usd"]
    return next_tier(start, res["cost_email_tier"])


def evaluate(res, cfg, now):
    if not res["billing_now"] or res["gone_at"]:
        return []
    R = cfg["rules"]
    out = []
    provider = res["provider"]
    age_active = (now - res["active_since"]) if res["active_since"] else 0
    age_launch = (now - res["launched_at"]) if res["launched_at"] else age_active
    cost = streak_cost(res)

    if res["ownership"] == "foreign":
        if res["health"] == "crash_loop":
            out.append({"rule": "crash_loop", "audience": "foreign_crash", "severity": "urgent",
                        "text": "crash-looping (someone else's app)"})
        return out

    if res["health"] == "crash_loop":
        out.append({"rule": "crash_loop", "audience": "owner", "severity": "urgent", "text": "crash-looping"})
    rate = res["rate_usd_h"] or 0
    if rate >= R["high_burn"]["usd_per_hour"]:
        out.append({"rule": "high_burn", "audience": "owner", "severity": "high",
                    "text": "burning %s/h (threshold %s/h)" % (util.money(rate), util.money(R["high_burn"]["usd_per_hour"]))})
    lr = R["long_running"]
    limit_h = {"lambda": lr["lambda_hours"], "hf": lr["hf_hours"], "slurm": lr["slurm_hours"],
               "modal": lr["modal_active_hours"], "deepinfra": lr["hf_hours"]}.get(provider)
    running_for = age_launch if provider in ("lambda", "slurm", "hf", "deepinfra") else age_active
    if limit_h and running_for >= limit_h * 3600:
        out.append({"rule": "long_running", "audience": "owner", "severity": "normal",
                    "text": "running for %s (threshold %dh)" % (util.age(running_for), limit_h)})
    if res["health"] == "idle":
        out.append({"rule": "idle_gpu", "audience": "owner", "severity": "high",
                    "text": "GPU idle with no work process for %d checks" % R["idle_gpu"]["consecutive_checks"]})
    if cost is not None and not acked(res, now) and cost >= R["cost"]["ask_owner_usd"]:
        out.append({"rule": "cost", "audience": "owner", "severity": "normal",
                    "text": "%s spent (threshold %s)" % (util.money(cost), util.money(R["cost"]["ask_owner_usd"]))})
    if cost is not None:
        threshold = cost_email_threshold(res, cfg, now)
        if cost >= threshold:
            out.append({"rule": "cost_email", "audience": "human", "severity": "high", "tier": threshold,
                        "text": "%s spent, past the %s line%s" % (util.money(cost), util.money(threshold),
                                                                  " (owner acked it)" if acked(res, now) else "")})
    # An ack silences owner messages until it expires, except a crash loop that began after the ack.
    if acked(res, now):
        new_crash = res["health"] == "crash_loop" and res["suppress_health"] != "crash_loop"
        out = [f for f in out if f["audience"] != "owner" or (f["rule"] == "crash_loop" and new_crash)]
    return out


def account_findings(db, cfg, account, now):
    """Daily-spend lines for the digest (rolling 24 h, from the latest metric)."""
    limit = (cfg["rules"].get("account_spend") or {}).get(account)
    if limit is None:
        return []
    row = db.one("select value from metrics where account=? and name='day_spend_usd' order by ts desc limit 1", account)
    if row and row["value"] >= limit:
        return [{"kind": "account_spend", "key": account,
                 "text": "%s spent %s in the last 24 h (threshold %s)" % (account, util.money(row["value"]), util.money(limit))}]
    return []


def storage_findings(db, cfg, account, now):
    row = db.one("select value from metrics where account=? and name='month_volumes_usd' order by ts desc limit 1", account)
    limit = cfg["rules"]["storage"]["modal_volumes_usd_month"]
    if row and row["value"] >= limit:
        return [{"kind": "storage", "key": account,
                 "text": "%s volume storage %s so far this month (threshold %s/month)" % (account, util.money(row["value"]), util.money(limit))}]
    return []
