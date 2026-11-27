#!/usr/bin/env python3
"""The Mac's front window as its ActivityWatch has it, for the voice desk (board/agentboard/voicedesk.py): the
desktop's `agent-board looking`, and the voice page's what_am_i_looking_at, pipe this file to
`ssh <the Mac's alias> python3 - --aw`, which prints one JSON object and exits:

  {"ok": true, "windows": the window watcher's latest events, newest first, "web": the browser watcher's, "afk":
   "afk" / "not-afk" / null, "buckets": the bucket ids, "errors": the buckets whose events could not be read, and
   why}, or {"ok": false, "error"}

A few events of each come, not only the latest, so that the desktop can pass over the voice page's own window, which
is in front while the owner talks to it. Standard library only, Python 3.9 (the Mac's /usr/bin/python3); nothing to
install. VOICE_DESK_AW sets ActivityWatch's server (default http://localhost:5600).
"""
import json
import os
import sys
import urllib.request

AW = os.environ.get("VOICE_DESK_AW", "http://localhost:5600").rstrip("/")
EVENTS = 5                     # events of each watcher


def aw_get(path, base=None, timeout=4):
    with urllib.request.urlopen((base or AW) + "/api/0/" + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def aw_front(base=None, n=EVENTS):
    """The latest `n` events of the window, browser and AFK watchers (the module's docstring); {"ok": False, "error"}
    when the server does not answer or no window watcher's events can be read."""
    try:
        buckets = aw_get("buckets/", base)
    except Exception as e:                       # not running, or the Mac's firewall: say so, never raise
        return {"ok": False, "error": "ActivityWatch is not answering at %s: %s" % (base or AW, e)}
    ids, errors = sorted(buckets), []

    def latest(prefix, n):
        """The newest bucket's latest events among those whose ids start with `prefix`; a bucket that cannot be read
        is passed over and its error kept."""
        for b in sorted((b for b in ids if b.startswith(prefix)),
                        key=lambda b: (buckets[b].get("last_updated") or ""), reverse=True):
            try:
                evs = aw_get("buckets/%s/events?limit=%d" % (b, n), base)
            except Exception as e:               # this bucket only: another may answer
                errors.append("%s: %s" % (b, e))
                continue
            if evs:
                return [dict(e.get("data") or {}, timestamp=e.get("timestamp"), duration=e.get("duration"), bucket=b)
                        for e in evs]
        return []

    windows = latest("aw-watcher-window", n)
    if not windows:
        return {"ok": False, "error": "ActivityWatch has no window watcher events (buckets: %s%s)" % (
            ", ".join(ids), "; " + "; ".join(errors) if errors else ""), "buckets": ids}
    afk = latest("aw-watcher-afk", 1)
    return {"ok": True, "windows": windows, "web": latest("aw-watcher-web", n),
            "afk": afk[0].get("status") if afk else None, "buckets": ids, "errors": errors}


if __name__ == "__main__":
    if sys.argv[1:] != ["--aw"]:
        sys.exit("usage: aw_front.py --aw")
    print(json.dumps(aw_front()))
