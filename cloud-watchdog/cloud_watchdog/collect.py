"""Child entry point: runs inside creds.run_as with one account's credentials.

    python -m cloud_watchdog.collect '<json request>'

Request: {"op": "collect"|"stop"|"crash_check", "account": name, "acct": {...cfg}, "opts": {...},
"native_id": ...}. Prints one JSON object on stdout. Errors are reported by exception *type and a
trimmed message* only, so a provider's error body can never carry a credential into the logs.
"""
import json
import sys
import traceback

from . import providers


def main():
    req = json.loads(sys.argv[1])
    try:
        mod = providers.get(req["acct"]["provider"])
        if req["op"] == "collect":
            res = mod.collect(req["acct"], req["opts"])
        elif req["op"] == "stop":
            ok, msg = mod.stop(req["acct"], req["native_id"])
            res = {"ok": ok, "message": msg}
        elif req["op"] == "crash_check":
            res = mod.crash_check(req["acct"], req["native_id"], req["opts"]["window_min"], at=req["opts"].get("at"))
        else:
            raise ValueError("unknown op %r" % req["op"])
        print(json.dumps({"ok": True, "result": res}))
    except Exception as e:  # noqa: BLE001 - report, never crash the parent's parse
        tb = traceback.extract_tb(e.__traceback__)[-1]
        print(json.dumps({"ok": False, "error": "%s: %s (%s:%d)" % (type(e).__name__, str(e)[:300],
                                                                   tb.filename.rsplit("/", 1)[-1], tb.lineno)}))


if __name__ == "__main__":
    main()
