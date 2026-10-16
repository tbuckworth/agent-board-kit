"""DeepInfra: only dedicated (hourly) deployments. `legacy` entries are public models billed per use,
so they are skipped."""
import os

from .. import util
from . import clean, http_json


def collect(acct, opts):
    rows = http_json("https://api.deepinfra.com/deploy/list",
                     headers={"Authorization": "Bearer " + os.environ["DEEPINFRA_API_KEY"]}) or []
    out = []
    for d in rows:
        if d.get("type") == "legacy" or d.get("status") in ("deleted", "failed", "stopped"):
            continue
        out.append(clean({"native_id": d.get("deploy_id"), "name": d.get("model_name"), "kind": "deployment",
                          "state": d.get("status"), "launched_at": util.parse_time(d.get("created_at"))}))
    return {"resources": out, "metrics": {}}


def stop(acct, native_id):
    return False, "not automated: stop it in the DeepInfra dashboard (deleting a deployment is irreversible)"
