"""shadow -> live switch. Shadow: everything is decided and logged, nothing is sent or stopped.

meta 'mode' = {"mode": "shadow"|"live"|"off", "live_at": epoch|None, "set_by": str, "set_at": epoch}
A shadow mode with live_at goes live by itself at that time, unless the mode was changed first.
With no mode set at all, the watchdog is in shadow with no go-live date: it never acts until told to.
"""
from . import util


def get(db):
    return db.meta("mode") or {"mode": "shadow", "live_at": None, "set_by": "default", "set_at": None}


def set_mode(db, mode, now, live_at=None, by="cli"):
    db.set_meta("mode", {"mode": mode, "live_at": live_at, "set_by": by, "set_at": now})


def effective(db, now):
    """Returns (mode, switched_now)."""
    m = get(db)
    if m["mode"] == "shadow" and m.get("live_at") and now >= m["live_at"]:
        set_mode(db, "live", now, by="automatic after shadow period")
        # Incidents advanced in shadow never really messaged anyone: start live with a clean slate.
        for inc in db.q("select id, key from incidents where closed_at is null"):
            db.close_incident(inc["id"], "shadow-reset", now)
        db.log("mode", "shadow period ended: now live", now=now)
        return "live", True
    return m["mode"], False


def describe(db, now):
    m = get(db)
    if m["mode"] == "shadow":
        if m.get("live_at"):
            return "shadow until %s (%s from now), then live automatically" % (
                util.iso(m["live_at"], local=True), util.age(m["live_at"] - now))
        return "shadow (no automatic go-live date)"
    return m["mode"]
