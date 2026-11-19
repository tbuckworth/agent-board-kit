"""Lambda Cloud: instance list (cost = list price x uptime, an estimate), SSH idle check for our boxes.

The instance JSON includes `jupyter_token` and `jupyter_url`; providers.clean() drops them.
"""
import os
import re
import shlex

from .. import util
from . import UNNAMED, clean, http_json, ssh

API = "https://cloud.lambdalabs.com/api/v1"
HAS_RESOURCES = True
COUNTS_FROM_LAUNCH = True
ID_TERM = "%s"
STOP_EFFECT = "terminate the instance; its local disk is lost"
STOP_KEEPS_DATA = False


def billing_now(r):
    return r.get("state") in ("active", "booting", "unhealthy")


def ownership(r, identity):
    """Ours when the box was launched with one of your SSH keys (identity.owner_lambda_ssh_keys). None when the box
    lists no keys, or none are configured."""
    keys = identity.get("owner_lambda_ssh_keys") or []
    if not keys or not r.get("ssh_keys"):
        return None
    return "ours" if set(r["ssh_keys"]) & set(keys) else "foreign"


def collect_options(db, cfg, account, now, slow):
    """Parent side: who we are (our boxes get the idle probe), and the idle rule's work pattern and GPU threshold."""
    return {"identity": cfg["identity"], "idle_pattern": cfg["rules"]["idle_gpu"]["work_pattern"],
            "idle_gpu_max": cfg["rules"]["idle_gpu"]["gpu_util_max_pct"]}


def _auth():
    return (os.environ["LAMBDA_API_KEY"], "")


def idle_check(ip, work_pattern, timeout=25):
    """One SSH probe of the box. Returns (ok, detail): on success (True, {"reachable": True, "work_procs",
    "gpu_util_sum"}); on failure (False, {"reachable", "error"}), where reachable is False when ssh itself failed and
    True when the remote command did."""
    # Lines containing "grep" are dropped: that removes grep itself and the remote shell whose own
    # command line contains the pattern (a naive `ps | grep` would always count itself).
    remote = ("ps -eo args | grep -E %s | grep -v grep | wc -l ; nvidia-smi --query-gpu=utilization.gpu "
              "--format=csv,noheader | tr -d ' %%' | paste -sd+ - | bc") % shlex.quote(work_pattern)
    code, out, err = ssh("ubuntu@%s" % ip, remote, timeout, options=["StrictHostKeyChecking=accept-new"])
    if code == 255:
        return False, {"reachable": False, "error": "ssh failed (exit 255): %s" % err.strip()[-200:]}
    nums = re.findall(r"\d+", out)
    if code not in (0, 1) or len(nums) < 2:
        return False, {"reachable": True, "error": "remote command failed (exit %s): %s" % (code, err.strip()[-200:])}
    return True, {"reachable": True, "work_procs": int(nums[0]), "gpu_util_sum": int(nums[1])}


def collect(acct, opts):
    now = opts["now"]
    data = http_json(API + "/instances", auth=_auth())["data"]
    out = []
    for i in data:
        t = i["instance_type"]
        rate = t["price_cents_per_hour"] / 100.0
        launched = util.parse_time(i.get("first_healthy"))
        r = {"native_id": i.get("id"), "name": i.get("name") or (t.get("name") or "lambda") + UNNAMED,
             "kind": "instance", "state": i.get("status"), "launched_at": launched,
             "rate_usd_h": rate, "rate_source": "list price",
             "cost_usd": round(rate * (now - launched) / 3600, 2) if launched else 0.0,
             "cost_source": "estimate: price x uptime", "ssh_keys": i["ssh_key_names"],
             "instance_type": t.get("name"), "region": (i.get("region") or {}).get("name"), "ip": i.get("ip")}
        ours = ownership(r, opts["identity"]) == "ours"
        if ours and r["ip"] and r["state"] == "active" and opts.get("idle_pattern") and not opts["inventory_only"]:
            ok, detail = idle_check(r["ip"], opts["idle_pattern"])
            r["idle"] = (detail["work_procs"] == 0 and detail["gpu_util_sum"] <= opts["idle_gpu_max"]) if ok else None
            r["idle_detail"] = detail
        out.append(clean(r))
    return {"resources": out, "metrics": {}}


def stop(acct, instance_id):
    """Terminate. The instance's local disk is lost; only the owning agent or the human may ask for this."""
    res = http_json(API + "/instance-operations/terminate", auth=_auth(), method="POST",
                    body={"instance_ids": [instance_id]})
    ok = bool(res and (res.get("data") or {}).get("terminated_instances"))
    return ok, "terminated" if ok else "terminate returned no instances"
