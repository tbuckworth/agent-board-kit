"""Parent side of the collectors: run one provider op in a credentialed child and parse its answer."""
import json
import sys

from . import creds

TIMEOUTS = {"collect": 600, "stop": 180, "crash_check": 300}


def op(acct, name, opts=None, native_id=None):
    """Returns (ok, result_or_error); the child reads the request on stdin (why: creds.py)."""
    req = {"op": name, "acct": acct, "opts": opts or {}, "native_id": native_id}
    code, out, err = creds.run_as(acct, [sys.executable, "-m", "cloud_watchdog.collect"],
                                  timeout=TIMEOUTS.get(name, 300), input_text=json.dumps(req))
    line = next((l for l in reversed(out.strip().splitlines()) if l.startswith("{")), None)
    if line is None:
        return False, "child exit %s: %s" % (code, (err or out).strip()[-300:])
    try:
        res = json.loads(line)
    except ValueError:
        # stderr only: the cut-off stdout line may carry provider data.
        return False, "unparseable child output (exit %s): %s" % (code, err.strip()[-300:])
    return (True, res["result"]) if res.get("ok") else (False, res.get("error", "unknown error"))
