"""Who may stop what. Enforced here, in the stop path itself, not in any prompt.

Actors:
  watchdog  the timer
  owner     the agent session recorded as the resource's owner (or the first agent to claim an unowned one)
  human     the person the watchdog works for, directly or relayed by the orchestrator (`--by human`)
  agent     any other session

Only providers with a stop can be stopped, and a stop that loses data (STOP_KEEPS_DATA false: Lambda's terminate
loses the instance's local disk) needs the owning agent or the human, and explicit confirmation.
"""
from . import messages, providers, rules, util


def can_stop(res, actor, cfg, now, fresh_crash=None, confirm_terminate=False):
    """(allowed, reason)."""
    provider = res["provider"]
    mod = providers.get(provider)
    if not hasattr(mod, "stop"):
        return False, "%s resources are not stopped by cloud-watch; use the provider's dashboard" % provider
    if actor == "watchdog":
        return _watchdog(res, cfg, now, fresh_crash)
    if res["ownership"] == "foreign" and actor != "human":
        return False, "not ours (started by %s); only %s can decide on someone else's resource" % (
            util.launched_by(res) or "someone else", messages.human(cfg))
    if actor == "agent":
        return False, ("owned by another session (%s): ask it to close it, or, if it is not its own, to run "
                       "`cloud-watch disown`, after which you can close it" % messages.owner_label(res))
    if not mod.STOP_KEEPS_DATA and not confirm_terminate:
        return False, "close would %s. Copy results off, then re-run with --confirm-terminate" % mod.STOP_EFFECT
    return True, "%s may stop it" % actor


def _watchdog(res, cfg, now, fresh_crash):
    acct = cfg["accounts"].get(res["account"]) or {}
    users = cfg["identity"].get("owner_modal_users") or []
    if res["provider"] != "modal":
        return False, "the watchdog only ever stops Modal apps"
    if not acct.get("auto_stop"):
        return False, "auto-stop is off for %s" % res["account"]
    # Provenance, not inference: Modal must name one of the configured owner_modal_users as the app's latest
    # deployer, or, for a `modal run` app (never deployed), as its creator. An empty list means never.
    if not users:
        return False, "identity.owner_modal_users is empty, so the watchdog never stops anything itself"
    who = util.launched_by(res)
    if who not in users:
        return False, "deployed or created by %s, not by %s" % (who or "an unknown user", "/".join(users))
    if res["health"] != "crash_loop":
        return False, "not in a confirmed crash loop"
    if not fresh_crash:
        return False, "a fresh log check does not confirm the crash loop"
    if rules.acked(res, now) and res["suppress_health"] == "crash_loop":
        return False, "the owning agent acked it while it was already crash-looping"
    return True, "confirmed crash loop in an app deployed by %s" % who
