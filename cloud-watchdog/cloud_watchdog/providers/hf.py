"""Hugging Face Inference Endpoints (hourly-billed). Spaces on paid hardware are not watched."""
import os

from .. import util
from . import clean, http_json

API = "https://api.endpoints.huggingface.cloud/v2/endpoint"
NOT_BILLING = {"paused", "scaledToZero"}
HAS_RESOURCES = True
COUNTS_FROM_LAUNCH = True
ID_TERM = "%s"
STOP_EFFECT = "pause the endpoint; it and its config are kept"
STOP_KEEPS_DATA = True


def billing_now(r):
    return True   # collect() returns only endpoints that bill


def ownership(r, identity):
    return "ours"   # the account holds only your endpoints


def _token():
    return os.environ["HF_TOKEN"]


def _namespace():
    return http_json("https://huggingface.co/api/whoami-v2", bearer=_token())["name"]


def collect(acct, opts):
    ns = _namespace()
    items = http_json("%s/%s" % (API, ns), bearer=_token())["items"]
    out = []
    for e in items:
        st = (e.get("status") or {})
        if st.get("state") in NOT_BILLING:
            continue
        comp = e.get("compute") or {}
        out.append(clean({"native_id": "%s/%s" % (ns, e.get("name")), "name": e.get("name"), "kind": "endpoint",
                          "state": st.get("state"), "launched_at": util.parse_time(st.get("createdAt")),
                          "instance_type": "%s %s" % (comp.get("instanceType", ""), comp.get("instanceSize", ""))}))
    return {"resources": out, "metrics": {}}


def stop(acct, native_id):
    """Pause: billing stops, the endpoint and its config stay."""
    ns, name = native_id.split("/", 1)
    http_json("%s/%s/%s/pause" % (API, ns, name), bearer=_token(), method="POST", body={})
    return True, "paused"
