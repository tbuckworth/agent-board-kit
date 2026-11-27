"""The board's tile groups: the layout the Mac app sends, and the group each tile was last in.

The layout lives on the Mac; the app sends it here (the "groups" action) whenever it changes, and the desktop keeps
the latest copy in groups.json for the Time page, the usage breakdown, the voice page's excluded groups, the
transcript archive, sub-group chats (subchats.py) and the group colours of the Mail tab's threads
(mailbox.add_groups). Saving a layout also updates groups-history.json, which keeps each tile's group after the tile
closes and the Mac stops sending it. A tile that is running but in no group has been ungrouped, and leaves the
history.
"""
import re
import time
from pathlib import Path

from . import sources

GROUPS = sources.BOARD_STATE / "groups.json"
NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")
ID_RE = sources.SESSION_ID_RE
# the Mac's id for a group
UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


def beside(name):
    """A file beside groups.json, so whatever stands in for GROUPS (a test's folder) holds it too."""
    return Path(GROUPS).with_name(name)


def history_path():
    return beside("groups-history.json")


def clean_groups(groups):
    """The Mac's groups as stored here, or (None, error): [{name, members, subgroups: [{name, members, id}]}]. id is
    the Mac's UUID for a group, which subchats.py keys a sub-group's chat by; an app older than sub-group chats sends
    none, and the group is stored without it."""
    if not isinstance(groups, list) or len(groups) > 60:
        return None, "groups must be a list of at most 60"
    out = []
    for g in groups:
        if not isinstance(g, dict) or not isinstance(g.get("name"), str) or not NAME_RE.match(g["name"]):
            return None, "each group needs a name of 1-80 printable characters"
        if "id" in g and not (isinstance(g["id"], str) and UUID_RE.match(g["id"])):
            return None, "a group's id must be a UUID"
        subs, err = clean_groups(g.get("subgroups") or []) if g.get("subgroups") else ([], None)
        if err:
            return None, err
        if any(s["subgroups"] for s in subs):
            return None, "sub-groups have no sub-groups of their own"
        members = g.get("members") or []
        if not isinstance(members, list) or len(members) > 500 or not all(isinstance(m, str) and ID_RE.match(m)
                                                                          for m in members):
            return None, "members must be full session ids"
        out.append(dict({"name": g["name"], "members": list(members), "subgroups": subs},
                        **({"id": g["id"]} if "id" in g else {})))
    return out, None


def placements(groups):
    """{session id: {"group", "subgroup"}} for a layout; subgroup is None for a group's direct members."""
    out = {}
    for g in groups:
        for m in g.get("members") or []:
            out[m] = {"group": g["name"], "subgroup": None}
        for s in g.get("subgroups") or []:
            for m in s.get("members") or []:
                out[m] = {"group": g["name"], "subgroup": s["name"]}
    return out


def placed(history, layout):
    """{session id: (group, sub-group or None)} of the tiles the owner placed (`history`) whose group is on the board
    (`layout`, layout()'s form); a sub-group no longer in its group is dropped. The Time page, the transcript archive
    and the Mail tab's colours place a session by this."""
    subs = {g["name"]: set(g["subgroups"]) for g in layout}
    return {sid: (h["group"], h["subgroup"] if h["subgroup"] in subs[h["group"]] else None)
            for sid, h in history.items() if h["group"] in subs}


def stored():
    """The saved layout, {"updated", "groups"}; {} before the Mac has sent one. A file that exists but does not hold
    a layout raises (ValueError, or read_json's OSError), rather than being taken for an empty one."""
    saved = sources.read_json(GROUPS, {})
    if not isinstance(saved, dict):
        raise ValueError("%s does not hold a layout" % GROUPS)
    return saved


def history():
    """{session id: {"group", "subgroup"}}: every tile's latest group, closed tiles included. A file that exists but
    cannot be read raises (read_json), rather than being taken for an empty history and overwritten."""
    return sources.read_json(history_path(), {})


def save_groups(groups, live_ids, t=None):
    """Store a cleaned layout and fold it into the history. `live_ids` are the sessions running now: one of them that
    the layout leaves out was ungrouped, and leaves the history; a tile that is not running keeps its group."""
    hist = history()               # first: an unreadable history must stop the save before either file changes
    sources.write_json(GROUPS, {"updated": t or time.time(), "groups": groups})
    placed = placements(groups)
    for sid in [s for s in hist if s in live_ids and s not in placed]:
        del hist[sid]
    hist.update(placed)
    sources.write_json(history_path(), hist)


def layout():
    """[{"name", "subgroups": [names]}] of the saved layout (stored()), in the board's order."""
    return [{"name": g["name"], "subgroups": [s["name"] for s in g.get("subgroups") or []]}
            for g in stored().get("groups") or []]


def members(names=None, saved=None):
    """{session id: group path ("Work" or "Work / Sub")} for the named groups (every group when `names` is None),
    sub-groups included. Raises as stored() does."""
    saved = stored() if saved is None else saved
    return {sid: p["group"] + (" / " + p["subgroup"] if p["subgroup"] else "")
            for sid, p in placements(saved.get("groups") or []).items() if names is None or p["group"] in names}
