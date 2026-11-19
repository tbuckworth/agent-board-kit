"""Provider modules. Their operations run *inside* a credentialed child (see creds.py); the parent reads their facts
through get(name).

    deepinfra    DeepInfra dedicated deployments (no stop)
    hf           Hugging Face Inference Endpoints
    lambda_      Lambda Cloud instances, and the SSH idle probe of our boxes
    modal_       Modal apps: health, owner, cost, and the data for spend.py
    modal_api    a helper script, not a provider: modal_ runs it under the modal CLI's Python for lifecycle lookups
    openrouter   OpenRouter key spend (metrics only)
    slurm        Slurm jobs on paid partitions, over SSH

Each provider module exposes:

    collect(acct, opts) -> {"resources": [...], "metrics": {...}}    # Modal adds "creators", "warnings", "spend"
        opts["inventory_only"]: list the resources and skip the per-resource health probes and lookups
    HAS_RESOURCES                       # False: collect() returns metrics only, nothing that can be left running

With HAS_RESOURCES, the facts the parent applies to each resource:

    billing_now(r) -> bool              # r: the resource's state row merged with this collection's fields
    ownership(r, identity) -> "ours" | "foreign" | None    # r: a collected resource; None: its fields don't say
    COUNTS_FROM_LAUNCH                  # age and cost count from launch (True) or from the current active stretch
    ID_TERM                             # how a launch command prints the native id ("%s": as is), for the owner search

Optional, parent side: collect_options(db, cfg, account, now, slow) -> the opts its collect() reads besides "now",
"slow" and "inventory_only" (check.collect_opts).

Optional operations:

    stop(acct, native_id) -> (ok, message), with STOP_EFFECT (what it does, in words) and STOP_KEEPS_DATA
    crash_check(acct, native_id, window_min, live_containers, at) -> dict
    live_container_ids(acct, native_id) -> [container id]                # with crash_check

Resource dicts carry only allowlisted, non-secret fields (see FIELDS), plus Modal's "dormant" flag (a deployed app
scaled to zero: the parent tracks it only if it already knows it). ENV names the variables each provider's tools
read its keys from (creds.py puts the account's keys there).
"""
import json
import urllib.error
import urllib.request

from .. import util

ENV = {
    "modal": ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"),   # none when the account uses a ~/.modal.toml profile
    "lambda": ("LAMBDA_API_KEY",),
    "hf": ("HF_TOKEN",),
    "deepinfra": ("DEEPINFRA_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "slurm": (),                                          # SSH, with your own SSH config and agent
}
SSH_CONNECT_TIMEOUT_S = 15
UNNAMED = " (unnamed)"   # ends a name a collector made up for an unnamed resource; the owner search skips such names
FIELDS = {
    "native_id", "name", "kind", "state", "launched_at", "tasks", "containers", "max_container_age_s",
    "rate_usd_h", "rate_source", "cost_usd", "cost_source", "deployed_by", "created_by", "git_commit", "deployed_at",
    "ssh_keys", "instance_type", "region", "ip", "partition", "health_detail", "idle", "idle_detail",
}


def clean(resource):
    """Drop anything not explicitly allowed (e.g. Lambda's jupyter_token)."""
    return {k: v for k, v in resource.items() if k in FIELDS}


def http_json(url, method="GET", body=None, timeout=30, auth=None, bearer=None):
    """Call a JSON API and return the parsed answer. `auth` is a (user, password) pair for Basic auth; `bearer` a token
    sent as `Authorization: Bearer`. An HTTP error raises RuntimeError naming only the status and the URL."""
    data = json.dumps(body).encode() if body is not None else None
    # Lambda's API sits behind a bot filter that answers Python's default User-Agent with 403.
    hdrs = {"User-Agent": "cloud-watchdog/0.1", "Accept": "application/json"}
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(("%s:%s" % auth).encode()).decode())
    if bearer is not None:
        req.add_header("Authorization", "Bearer " + bearer)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        # The body may echo request details; keep only the status and a short reason.
        raise RuntimeError("HTTP %s from %s" % (e.code, url.split("?")[0])) from None


def ssh(host, command, timeout, options=()):
    """Run `command` on `host` over ssh, never prompting for a password (BatchMode) and giving up on a connection
    after SSH_CONNECT_TIMEOUT_S; `options` are extra `-o` settings. Returns util.run's (code, stdout, stderr): code
    255 when ssh itself failed, otherwise the remote command's exit status."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % SSH_CONNECT_TIMEOUT_S]
    for opt in options:
        argv += ["-o", opt]
    return util.run(argv + [host, command], timeout=timeout)


def get(name):
    """The module of a provider name (an account's `provider`)."""
    from . import deepinfra, hf, lambda_, modal_, openrouter, slurm
    mods = {"modal": modal_, "lambda": lambda_, "slurm": slurm, "hf": hf, "deepinfra": deepinfra,
            "openrouter": openrouter}
    if name not in mods:
        raise ValueError("unknown provider %r (known: %s)" % (name, ", ".join(sorted(mods))))
    return mods[name]
