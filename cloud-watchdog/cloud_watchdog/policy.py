"""Who may stop what. Enforced here, in the stop path itself, not in any prompt.

Actors:
  watchdog  the timer (or the investigator acting through it)
  owner     the agent session recorded as the resource's owner (or the first agent to claim an unowned one)
  human     the person the watchdog works for, directly or relayed by the orchestrator (`--by human`)
  agent     any other session

Nothing here ever deletes data: Modal `app stop`, Slurm `scancel`, HF `pause`, and Lambda terminate
(owning agent or human only, explicit confirmation, because the instance's local disk is lost).
"""
from . import rules, util

STOPPABLE = {"modal", "lambda", "slurm", "hf"}


def can_stop(res, actor, cfg, now, fresh_crash=None, confirm_terminate=False):
    """(allowed, reason)."""
    provider = res["provider"]
    if provider not in STOPPABLE:
        return False, "%s resources are not stopped by cloud-watch; use the provider's dashboard" % provider
    if actor == "watchdog":
        return _watchdog(res, cfg, now, fresh_crash)
    if res["ownership"] == "foreign" and actor != "human":
        return False, "not ours (deployed by %s); only %s can decide on someone else's resource" % (
            util.modal_user(res) or "someone else", cfg.get("owner_name") or "the owner")
    if actor == "agent":
        return False, "owned by another session (%s); ask it, or `disown` flow" % (res["owner_name"] or res["owner_session"])
    if provider == "lambda" and not confirm_terminate:
        return False, "terminating a Lambda instance destroys its local disk; copy results off, then re-run with --confirm-terminate"
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
    who = util.modal_user(res)
    if who not in users:
        return False, "deployed or created by %s, not by %s" % (who or "an unknown user", "/".join(users))
    if res["health"] != "crash_loop":
        return False, "not in a confirmed crash loop"
    if not fresh_crash:
        return False, "a fresh log check does not confirm the crash loop"
    if rules.acked(res, now) and res["suppress_health"] == "crash_loop":
        return False, "the owning agent acked it while it was already crash-looping"
    return True, "confirmed crash loop in an app deployed by %s" % who
