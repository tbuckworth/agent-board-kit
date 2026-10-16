"""Parent side of the collectors: run one provider op in a credentialed child and parse its answer."""
import json
import sys

from . import creds

TIMEOUTS = {"collect": 600, "stop": 180, "crash_check": 300}


def op(account, acct, name, opts=None, native_id=None):
    """Returns (ok, result_or_error)."""
    req = {"op": name, "account": account, "acct": acct, "opts": opts or {}, "native_id": native_id}
    code, out, err = creds.run_as(acct, [sys.executable, "-m", "cloud_watchdog.collect", json.dumps(req)],
                                  timeout=TIMEOUTS.get(name, 300))
    line = next((l for l in reversed(out.strip().splitlines()) if l.startswith("{")), None)
    if line is None:
        return False, "child exit %s: %s" % (code, (err or out).strip()[-300:])
    try:
        res = json.loads(line)
    except ValueError:
        return False, "unparseable child output"
    return (True, res["result"]) if res.get("ok") else (False, res.get("error", "unknown error"))
