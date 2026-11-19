"""DeepInfra: only dedicated (hourly) deployments. `legacy` entries are public models billed per use,
so they are skipped. No stop: the API's only way is deleting the deployment, which cannot be undone."""
import os

from .. import util
from . import clean, http_json

HAS_RESOURCES = True
COUNTS_FROM_LAUNCH = True
ID_TERM = "%s"


def billing_now(r):
    return True   # collect() returns only deployments that bill


def ownership(r, identity):
    return "ours"   # the account holds only your deployments


def collect(acct, opts):
    rows = http_json("https://api.deepinfra.com/deploy/list", bearer=os.environ["DEEPINFRA_API_KEY"]) or []
    out = []
    for d in rows:
        if d.get("type") == "legacy" or d.get("status") in ("deleted", "failed", "stopped"):
            continue
        out.append(clean({"native_id": d.get("deploy_id"), "name": d.get("model_name"), "kind": "deployment",
                          "state": d.get("status"), "launched_at": util.parse_time(d.get("created_at"))}))
    return {"resources": out, "metrics": {}}
