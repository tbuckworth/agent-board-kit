"""Sub-group chats: each sub-group of the board's layout has a group chat of its agents.

The layout lives on the Mac, which sends it here whenever it changes (the "groups" action, groups.py); sync() then
makes each sub-group's chat match it, as the owner (chats.py). The sub-group's name is the chat's title, and its running
agents that have a session name are the chat's members, up to chats.MAX_MEMBERS; the chat lists the running agents it
leaves out for want of a name a chat can use. A chat is made once a sub-group holds MIN_AGENTS named agents. When its
sub-group leaves the layout (deleted, or made a group of its own) the chat is hidden, its posts kept, and it is shown
again if the sub-group comes back. Top-level groups have no chat. The sub-group decides who is in its chat, so the
board refuses member changes to it (actions.py).

The state file, beside groups.json, maps each sub-group's id (the Mac's UUID) to its chat and keeps what the last sync
could not do, which the snapshot lists as problems. The log has a line for each chat made, changed, hidden or shown.
A state file that cannot be read raises chats.ChatError; one that cannot be written stops a sync before it changes
any chat, so no chat is made that the file does not record.
"""
import time

from . import chats, groups, sources

MIN_AGENTS = 2


def state_path():
    return groups.beside("subgroup-chats.json")


def log(lines):
    for line in lines:
        sources.append_log(groups.beside("subgroup-chats.log"), line)


def state():
    """{"chats": {sub-group id: {"chat", "title", "members", "unnamed", "gone"}}, "problems": [text]}, both empty
    before the first sync. unnamed: the sub-group's running agents without a name a chat can use, as the board shows
    them; gone is true while the sub-group is out of the layout and its chat hidden. A file that cannot be read, or is
    damaged, raises chats.ChatError."""
    try:
        saved = sources.read_json(state_path(), {})
    except (OSError, ValueError) as e:
        raise chats.ChatError("the sub-group chats' state: %s" % sources.error_text(e)) from None
    return {"chats": saved.get("chats") or {}, "problems": saved.get("problems") or []}


def _names(tiles, agents, where):
    """(session names, unnamed, problems) for a sub-group's running agents: unnamed are those without a name a chat
    can use (chats.NAME_RE), as the board shows them; more than chats.MAX_MEMBERS named is a problem. An agent that has
    exited, or left the board, is left out: agent-group adds only a session that exists."""
    by_id = {a["id"]: a for a in agents}
    names, unnamed, problems = [], [], []
    for sid in tiles:
        a = by_id.get(sid)
        if a is None or not a.get("live"):
            continue
        if not chats.NAME_RE.match(a.get("name") or ""):
            unnamed.append(a["display"])
        elif a["name"] not in names:
            names.append(a["name"])
    if len(names) > chats.MAX_MEMBERS:
        problems.append("%s has %d named agents; its chat holds the first %d" % (where, len(names), chats.MAX_MEMBERS))
    return names[:chats.MAX_MEMBERS], unnamed, problems


def sync(layout, agents, run):
    """Make every sub-group's chat match `layout` (groups.clean_groups' form), given the board's agent rows, and
    return the problems, which the state file keeps for the snapshot. Nothing happens when agent-group is not
    installed. A sub-group sent without an id (an older Mac app) cannot be matched to its chat, so while one is in
    the layout no chat is hidden. Runs one at a time, under a lock. Raises chats.ChatError when the state file cannot
    be read, and OSError when it, its lock or the log cannot be written; the state is written back before any chat
    is changed, so a file that cannot be written stops the sync first."""
    if not chats.installed():
        return []
    with sources.file_lock(groups.beside("subgroup-chats.lock")):
        saved = state()
        sources.write_json(state_path(), dict(saved, updated=time.time()))
        before = saved["chats"]
        after, problems, notes, unidentified = {}, [], [], False
        for g in layout:
            for s in g["subgroups"]:
                where = 'sub-group "%s / %s"' % (g["name"], s["name"])
                if "id" not in s:
                    unidentified = True
                    problems.append("%s came without its id: relaunch the board app" % where)
                    continue
                names, unnamed, too_many = _names(s["members"], agents, where)
                problems += too_many
                had = before.get(s["id"])
                bad_title = chats.title_error(s["name"])
                if bad_title:
                    problems.append("%s: %s" % (where, bad_title))
                    if had:
                        after[s["id"]] = had
                    continue
                want = {"title": s["name"], "members": names, "unnamed": unnamed}
                try:
                    if had:
                        if had.get("gone"):
                            chats.hide(had["chat"], False, run)
                            notes.append("showed chat %s again: %s is back" % (had["chat"], where))
                        chats.set_chat(had["chat"], s["name"], names, run)
                        after[s["id"]] = dict(want, chat=had["chat"])
                        if (had["title"], had["members"]) != (s["name"], names):
                            notes.append("chat %s of %s: members %s" % (had["chat"], where, ", ".join(names) or "none"))
                    elif len(names) >= MIN_AGENTS:
                        chat = chats.new(s["name"], names, run)
                        after[s["id"]] = dict(want, chat=chat["id"])
                        notes.append("made chat %s for %s: %s" % (chat["id"], where, ", ".join(names)))
                except chats.ChatError as e:
                    problems.append("%s: %s" % (where, e))
                    if had:
                        after[s["id"]] = had
        for sid, had in before.items():
            if sid in after:                   # its sub-group is in the layout
                continue
            after[sid] = had
            if had.get("gone") or unidentified:
                continue
            try:
                chats.hide(had["chat"], True, run)
                after[sid] = dict(had, gone=True)
                notes.append('hid chat %s: its sub-group "%s" left the layout' % (had["chat"], had["title"]))
            except chats.ChatError as e:
                problems.append('hiding the chat of sub-group "%s", which left the layout: %s' % (had["title"], e))
        sources.write_json(state_path(), {"updated": time.time(), "chats": after, "problems": problems})
        log(notes)                             # after the state, which must not wait on the log
    return problems


def annotate(rows):
    """agent-group's chat objects (list or show), each with "subgroup", the name of the sub-group in the layout it
    belongs to, or None, and "unnamed", the sub-group's agents it leaves out for want of a name ([] for others)."""
    owner = {c["chat"]: c for c in state()["chats"].values() if not c.get("gone")}
    out = []
    for r in rows:
        c = owner.get(r.get("id"))
        # an entry from a sync before unnamed was kept has none listed until the next sync
        out.append(dict(r, subgroup=c["title"] if c else None, unnamed=c.get("unnamed", []) if c else []))
    return out


def summary(run=sources.run):
    """chats.summary with each chat's sub-group (annotate) and the last sync's problems: None when agent-group is not
    installed. Raises chats.ChatError."""
    out = chats.summary(run)
    if out is None:
        return None
    return {"chats": annotate(out["chats"]), "problems": state()["problems"]}
