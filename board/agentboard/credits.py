"""Codex credits: what Codex spends once its plan's weekly allowance is used up, and the board's bar for them.

The server reports only the balance, so the bar counts down from the balance the weekly limit ran out at: noted
while the weekly window has room (nothing spends credits then), and kept in STATE as the full bar until the window
has room again. The README's Keys and layout section says what the bar shows.
"""
import time
from pathlib import Path

from . import sources

STATE = sources.BOARD_STATE / "codex-credits.json"
WEEK = 7 * 86400


def balance(acct):
    """The credit balance as a number; None without credits or a balance, or with unlimited ones. Raises ValueError
    when the account's "credits" is not an object or its balance is not a number."""
    c = acct.get("credits")
    if c is None:
        return None
    if not isinstance(c, dict):
        raise ValueError("Codex credits %r are not an object" % (c,))
    if not c.get("has") or c.get("unlimited") or c.get("balance") is None:
        return None
    try:
        return float(c["balance"])
    except (TypeError, ValueError):
        raise ValueError("Codex credit balance %r is not a number" % (c["balance"],)) from None


def weekly(acct):
    """(percent left, resets_at) of the plan's own weekly window (not a model's), or (None, None)."""
    for w in acct.get("windows") or []:
        if isinstance(w, dict) and w.get("kind") == "weekly":
            given = w.get("remaining_pct") is not None or w.get("used_pct") is not None
            return (sources.left_pct(w) if given else None), w.get("resets_at")
    return None, None


def step(acct, state, now):
    """(bar, state to keep) for one Codex reading. state is {} at first, {"room": {"balance", "at"}} while the weekly
    window has room, {"full", "since", "resets_at"} once it is used up; bar is None when there is no balance to show."""
    bal = balance(acct)
    if bal is None:
        return None, state
    t = acct.get("observed_at") if isinstance(acct.get("observed_at"), (int, float)) else (now or time.time())
    left, resets = weekly(acct)
    if left is None:        # a reading without the weekly window says nothing about the week: keep the full bar
        return {"balance": bal, "spending": False, "left_pct": 100.0}, state
    if left > 0:
        return {"balance": bal, "spending": False, "left_pct": 100.0}, {"room": {"balance": bal, "at": t}}
    kept = state.get("full")
    if not (isinstance(kept, (int, float)) and sources.same_reset(state.get("resets_at"), resets)):
        room = state.get("room") if isinstance(state.get("room"), dict) else {}
        before = room.get("balance") if isinstance(room.get("balance"), (int, float)) else None
        this_week = isinstance(room.get("at"), (int, float)) and (resets is None or room["at"] > resets - WEEK)
        state = {"full": max(bal, before) if before is not None and this_week else bal, "since": t, "resets_at": resets}
    elif bal > kept:
        state = dict(state, full=bal)
    full = state["full"]
    bar = {"balance": bal, "spending": True, "full": full, "left_pct": round(100.0 * bal / full, 1) if full > 0 else 0.0,
           "since": state["since"], "resets_at": resets}
    return bar, state


def annotate(accounts, path=None, now=None):
    """Put "credits_meter" on the Codex account of a usage reading (in place) and keep the state file (`path`,
    default STATE) current. Raises ValueError when the state file is not a JSON object (it is left as it is, so its
    full bar is not lost) or the balance cannot be read (balance), and OSError when the file cannot be read or
    written."""
    path = Path(path or STATE)
    for a in accounts:
        if a.get("target") != "codex":
            continue
        state = sources.load_config({}, path)      # {} before the first reading
        bar, new = step(a, state, now)
        if bar is None:
            continue
        a["credits_meter"] = bar
        if new != state:
            sources.write_json(path, new)
