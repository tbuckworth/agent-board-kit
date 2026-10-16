"""Lambda Cloud: instance list (cost = list price x uptime, an estimate), SSH idle check for our boxes.

The instance JSON includes `jupyter_token` and `jupyter_url`; providers.clean() drops them.
"""
import os
import re

from .. import util
from . import clean, http_json

API = "https://cloud.lambdalabs.com/api/v1"


def _auth():
    return (os.environ["LAMBDA_API_KEY"], "")


def idle_check(ip, work_pattern, timeout=25):
    """(reachable, detail) from one SSH probe: count of work processes, and the summed GPU utilisation."""
    # Lines containing "grep" are dropped: that removes grep itself and the remote shell whose own
    # command line contains the pattern (a naive `ps | grep` would always count itself).
    remote = ("ps -eo args | grep -E %s | grep -v grep | wc -l ; nvidia-smi --query-gpu=utilization.gpu "
              "--format=csv,noheader | tr -d ' %%' | paste -sd+ - | bc") % _shq(work_pattern)
    code, out, _ = util.run(["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
                             "-o", "BatchMode=yes", "ubuntu@%s" % ip, remote], timeout=timeout)
    nums = re.findall(r"\d+", out)
    if code not in (0, 1) or len(nums) < 2:
        return None, {"reachable": False}
    return True, {"reachable": True, "work_procs": int(nums[0]), "gpu_util_sum": int(nums[1])}


def _shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def collect(acct, opts):
    now = opts["now"]
    data = http_json(API + "/instances", auth=_auth()).get("data", [])
    out = []
    for i in data:
        t = i.get("instance_type") or {}
        rate = (t.get("price_cents_per_hour") or 0) / 100.0
        launched = util.parse_time(i.get("first_healthy"))
        r = {"native_id": i.get("id"), "name": i.get("name") or (t.get("name") or "lambda") + " (unnamed)",
             "kind": "instance", "state": i.get("status"), "launched_at": launched,
             "rate_usd_h": rate, "rate_source": "list price",
             "cost_usd": round(rate * (now - launched) / 3600, 2) if launched else 0.0,
             "cost_source": "estimate: price x uptime", "ssh_keys": i.get("ssh_key_names") or [],
             "instance_type": t.get("name"), "region": (i.get("region") or {}).get("name"), "ip": i.get("ip")}
        ours = set(r["ssh_keys"]) & set(opts.get("our_ssh_keys") or [])
        if ours and r["ip"] and r["state"] == "active" and opts.get("idle_pattern"):
            reachable, detail = idle_check(r["ip"], opts["idle_pattern"])
            r["idle"] = (detail["work_procs"] == 0 and detail["gpu_util_sum"] <= opts.get("idle_gpu_max", 5)
                         ) if reachable else None
            r["idle_detail"] = detail
        out.append(clean(r))
    return {"resources": out, "metrics": {}}


def stop(acct, instance_id):
    """Terminate. The instance's local disk is lost; only the owning agent or the human may ask for this."""
    res = http_json(API + "/instance-operations/terminate", auth=_auth(), method="POST",
                    body={"instance_ids": [instance_id]})
    ok = bool(res and (res.get("data") or {}).get("terminated_instances"))
    return ok, "terminated" if ok else "terminate returned no instances"
