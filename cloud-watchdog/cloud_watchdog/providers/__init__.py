"""Provider modules. Each runs *inside* a credentialed child (see creds.py) and exposes:

    collect(acct, opts) -> {"resources": [...], "metrics": {...}}
    stop(acct, native_id) -> (ok, message)            # graceful stop; never deletes data
    crash_check(acct, native_id, window_min) -> dict  # optional (Modal only)

Resource dicts carry only allowlisted, non-secret fields (see FIELDS). ENV names the variables each
provider's tools read its keys from (creds.py puts the account's keys there).
"""
import json
import urllib.error
import urllib.request

ENV = {
    "modal": ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"),   # none when the account uses a ~/.modal.toml profile
    "lambda": ("LAMBDA_API_KEY",),
    "hf": ("HF_TOKEN",),
    "deepinfra": ("DEEPINFRA_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "slurm": (),                                          # SSH, with your own SSH config and agent
}

FIELDS = {
    "native_id", "name", "kind", "state", "launched_at", "tasks", "containers", "max_container_age_s",
    "rate_usd_h", "rate_source", "cost_usd", "cost_source", "deployed_by", "created_by", "git_commit", "deployed_at",
    "ssh_keys", "instance_type", "region", "ip", "partition", "health", "health_detail", "idle", "idle_detail",
}


def clean(resource):
    """Drop anything not explicitly allowed (e.g. Lambda's jupyter_token)."""
    return {k: v for k, v in resource.items() if k in FIELDS}


def http_json(url, headers=None, method="GET", body=None, timeout=30, auth=None):
    data = json.dumps(body).encode() if body is not None else None
    # Lambda's API sits behind a bot filter that answers Python's default User-Agent with 403.
    hdrs = {"User-Agent": "cloud-watchdog/0.1", "Accept": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(("%s:%s" % auth).encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        # The body may echo request details; keep only the status and a short reason.
        raise RuntimeError("HTTP %s from %s" % (e.code, url.split("?")[0])) from None


def get(name):
    from . import deepinfra, hf, lambda_, modal_, openrouter, slurm
    mods = {"modal": modal_, "lambda": lambda_, "slurm": slurm, "hf": hf, "deepinfra": deepinfra,
            "openrouter": openrouter}
    if name not in mods:
        raise ValueError("unknown provider %r (known: %s)" % (name, ", ".join(sorted(mods))))
    return mods[name]
