"""What the owner did, and when, for the Time page (agentboard/timeuse.py). The README's Time section says what each
source counts; each source's function says how.

Each source returns activities: {"ts", "end"?, "kind", "key", "label", "repo"?, "hint"?, "approx"?}. The kinds are
prompt, board, email, meeting, doc, mail, todo and screen; the Mac app's Time.swift (TimeSheet.kinds) names them.
`key` names what the time belongs to: "s:<session id>" for an agent's tile, else an item that timeuse guesses a group
for ("cal:", "doc:", "mail:", "todo:", "orch:", "screen:").

Reads only, except two caches it keeps up to date: parsed transcripts (sources.read_appended reads each on
from where it last stopped) and the subjects of the owner's sent mail; a damaged one is reported through the caller's
log and starts afresh.
Calendar and Drive use the owner's Google login (google.docs_login), the owner's own email the owner's Gmail login
(google.gmail_login); both are optional, and timeuse leaves their sources out while they are off. todos runs `gh`.
"""
import datetime
import glob
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

from . import classify, config, gmailstate, google, screen, sent, sources

# the orchestrator relaying the owner's email to an agent (the kit's orchestrator/AGENTS.md writes it so)
RELAY_MARK = "[relayed from %s by email]" % config.owner_name()
SPAWN_MARK = classify.SPAWN_MARK
# a user message that starts like this was delivered or generated, not typed by the owner
INJECTED = ("Another Claude session", "[", "<", "Caveat:", "This session is being continued", "You review git diffs",
            SPAWN_MARK, "# AGENTS.md instructions")
GOOGLE_REFUSED = (403, 404)          # Drive answers these for a file whose history the login may not read
# an unread count in a window title, which changes while the window stays the same thing: "(3)", "(99+)" or
# "(1,204)" at the start, as in "(3) Home / X", or before a " - " separator, as in "Inbox (4) - Gmail". Not a
# file's "report (2).pdf", nor a year: "Paper (2023) - arXiv" keeps it.
COUNTER_RE = re.compile(r"^\(\d{1,3}(?:,\d{3})*\+?\)\s+|\s\(\d{1,3}(?:,\d{3})*\+?\)(?=\s-\s)")
EDITOR_KINDS = ("document", "spreadsheet", "presentation")   # Docs, Sheets, Slides: not folders, whose times move
                                                             # whenever a file lands in them


class SourceError(RuntimeError):
    """A source could not be read; the Time page leaves it out and says so."""


class NotUploaded(SourceError):
    """A device (the Mac) has uploaded nothing yet for this source. The page names it as waiting, but nothing is
    broken (timeuse.default_sources leaves screen time out until the Mac has uploaded, so it is not even waiting)."""


# ------------------------------------------------------------------ transcripts

def _note(out, ts, text):
    """Sort one user message: a relayed email, an agent-spawn brief, or something the owner typed."""
    if RELAY_MARK in text[:400]:
        out["relay"].append(ts)
    if text.startswith(SPAWN_MARK) and out["spawn"] is None:
        out["spawn"] = ts
    if text and not text.startswith(INJECTED):
        out["typed"].append([ts, text[:200]])


def _blank(cli):
    return {"cli": cli, "sid": "", "cwd": "", "interactive": None, "typed": [], "relay": [], "spawn": None}


def parse_claude(lines, out, path):
    """{"cli", "sid", "cwd", "interactive", "typed": [[ts, first 200 chars]], "relay": [ts], "spawn": ts or None}, from
    a transcript's lines (sources.read_appended's parse: `out` is what its earlier lines gave, None at the start)."""
    out = out or dict(_blank("claude"), sid=Path(path).stem)
    for line in lines:
        if b'"user"' not in line and b'"queue-operation"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "queue-operation" and d.get("operation") == "enqueue":   # delivered mid-turn
            if RELAY_MARK in str(d.get("content", "")) and d.get("timestamp"):
                out["relay"].append(sources.iso_ts(d["timestamp"]))
            continue
        if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain") or not d.get("timestamp"):
            continue
        if out["interactive"] is None:
            out["interactive"] = d.get("entrypoint") == "cli"
            out["cwd"] = d.get("cwd", "")
        _note(out, sources.iso_ts(d["timestamp"]), sources.message_text(d.get("message", {}).get("content")).lstrip())
    return out


def parse_codex(lines, out, path):
    """As parse_claude, for a Codex rollout; subagent threads and `codex exec` runs are not interactive."""
    if out is None:
        first = next(lines, None)
        if first is None:                     # nothing written yet
            return None
        out = dict(_blank("codex"), interactive=False)
        try:
            meta = json.loads(first).get("payload", {})
        except ValueError:
            return out
        out.update(sid=meta.get("id", ""), cwd=meta.get("cwd", ""), interactive=meta.get("source") in ("cli", "vscode"))
    if not out["interactive"]:
        return out
    for line in lines:
        if b'"user"' not in line:         # a cheap filter before parsing; spacing differs between writers
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        p = d.get("payload", {})
        if p.get("type") == "message" and p.get("role") == "user" and d.get("timestamp"):
            _note(out, sources.iso_ts(d["timestamp"]), sources.message_text(p.get("content")).lstrip())
    return out


def transcripts(since, cache_path, claude_homes, codex_home, log):
    """Parsed transcripts modified since `since`: every Claude login's, and Codex's, read through the cache at
    `cache_path` (sources.read_appended; a damaged cache is reported with log(message))."""
    paths = [(p, parse_claude) for h in claude_homes for p in glob.glob(os.path.join(str(h), "projects", "*", "*.jsonl"))]
    paths += [(p, parse_codex) for p in glob.glob(os.path.join(str(codex_home), "sessions", "*", "*", "*", "rollout-*.jsonl"))]
    return list(sources.read_appended(paths, since, cache_path, log).values())


def handoffs(parsed):
    """[(ts, session id)], sorted: each time the orchestrator relayed the owner's email to a session or started one."""
    return sorted([(ts, t["sid"]) for t in parsed for ts in t["relay"]] + [(t["spawn"], t["sid"]) for t in parsed if t["spawn"]])


def handed_to(ts, hand, window):
    """The session the orchestrator relayed to or started within `window` seconds after `ts`, or None."""
    return next((sid for t, sid in hand if ts <= t <= ts + window), None)


def session_names(db):
    """{session id: name}: the mailroom's session table (agents that ever mailed), then the live sessions."""
    names = {}
    if Path(db).exists():           # without a mailroom, names fall back to the session's folder
        for r in sources.ro_sqlite(db).execute("select session_id, session_name from sessions where session_name is not null"):
            names[r["session_id"]] = r["session_name"]
    for s in sources.claude_sessions_all():
        if s.get("name"):
            names[s["id"]] = s["name"]
    return names


def agent_activities(parsed, names, since):
    """The owner's typed prompts, each on its session's tile; a session copied between logins counts once."""
    out, seen = [], set()
    for t in parsed:
        if not t["interactive"] or not t["sid"]:
            continue
        label = names.get(t["sid"]) or os.path.basename((t["cwd"] or "").rstrip("/")) or t["sid"][:8]
        first = t["typed"][0][1] if t["typed"] else ""
        for ts, _ in t["typed"]:
            if ts >= since and (t["sid"], ts) not in seen:
                seen.add((t["sid"], ts))
                out.append({"ts": ts, "kind": "prompt", "key": "s:" + t["sid"], "label": label, "repo": t["cwd"], "hint": first})
    return out


def _on_tile(ts, kind, sid, names, cwd_of, fallback_label):
    return {"ts": ts, "kind": kind, "key": "s:" + sid, "label": names.get(sid) or fallback_label, "repo": cwd_of.get(sid, "")}


# ------------------------------------------------------------------ board messages and email to agents

def board_messages(since, names, cwd_of, hand, window):
    out = []
    for r in sent.records():
        ts = float(r.get("ts") or 0)
        if ts < since:
            continue
        if r.get("kind") == "chat":         # a group chat spans agents: it is an item of its own, grouped like one
            out.append({"ts": ts, "kind": "board", "key": "chat:%s" % r.get("group"), "label": r.get("to") or "chat"})
            continue
        sid = r.get("id") or handed_to(ts, hand, window)
        out.append(_on_tile(ts, "board", sid, names, cwd_of, r.get("to") or sid[:8]) if sid else
                   {"ts": ts, "kind": "board", "key": "orch:board:" + r["ref"], "label": r["text"][:120]})
    return out


def agent_emails(since, names, cwd_of, hand, window, owners, db):
    """The owner's emails to agents. One the orchestrator handled goes to the agent it relayed to or started, if any."""
    try:
        con = sources.ro_sqlite(db)
        tags = {r["tag"]: r["session_id"] for r in con.execute("select tag, session_id from sessions")}
        rows = list(con.execute("select id, from_addr, subject, received_at, matched_tag, match_rule, delivered_session "
                                "from inbound where received_at >= ?", (int(since),)))
    except sqlite3.Error as e:
        raise SourceError("mailroom store unreadable: %s" % e) from None
    out = []
    for r in rows:
        if (r["from_addr"] or "").lower() not in owners:
            continue
        ts, subject = float(r["received_at"]), r["subject"] or "(no subject)"
        if r["match_rule"] == "orchestrator":
            sid = handed_to(ts, hand, window)
            if not sid:
                out.append({"ts": ts, "kind": "email", "key": "orch:mail:%s" % r["id"], "label": subject})
                continue
        else:
            sid = tags.get(r["matched_tag"]) or r["delivered_session"]
        if sid:
            out.append(_on_tile(ts, "email", sid, names, cwd_of, subject))
    return out


# ------------------------------------------------------------------ Google

def within_hours(start, end, hours):
    """The parts of [start, end) inside `hours`, as (start, end) pairs: {"weekdays": [ISO numbers, Monday 1],
    "start": "HH:MM", "end": "HH:MM"} in local time, the end up to "24:00"."""
    (sh, sm), (eh, em) = (map(int, hours[k].split(":")) for k in ("start", "end"))
    out = []
    day = datetime.date.fromtimestamp(start)
    while datetime.datetime.combine(day, datetime.time()).timestamp() < end:
        if day.isoweekday() in hours["weekdays"]:
            midnight = datetime.datetime.combine(day, datetime.time())
            lo = (midnight + datetime.timedelta(hours=sh, minutes=sm)).timestamp()
            hi = (midnight + datetime.timedelta(hours=eh, minutes=em)).timestamp()
            if min(end, hi) > max(start, lo):
                out.append((max(start, lo), min(end, hi)))
        day += datetime.timedelta(days=1)
    return out


def calendar(since, until, client, hours):
    """Timed events the owner has not declined, on the login's primary calendar, clipped to [since, until] and to
    `hours` (within_hours): one activity per part that is left."""
    out = []
    params = {"timeMin": sources.rfc3339(since), "timeMax": sources.rfc3339(until), "singleEvents": "true", "maxResults": 250,
              "fields": "nextPageToken,items(id,summary,start,end,eventType,attendees(self,responseStatus))"}
    for e in client.pages(google.calendar_events_url("primary"), params, "items"):
        if not google.attending(e, ("default", "focusTime")):
            continue
        start, end = max(sources.iso_ts(e["start"]["dateTime"]), since), min(sources.iso_ts(e["end"]["dateTime"]), until)
        for s, t in within_hours(start, end, hours):
            out.append({"ts": s, "end": t, "kind": "meeting", "key": "cal:" + e["id"], "label": e.get("summary") or "(no title)"})
    return out


def documents(since, client):
    """One activity per revision the owner made to a Google Doc, Sheet or Slides file since `since`, shared drives
    included. Where Drive refuses a file's history, the owner's last edit of it stands in, marked "approx"."""
    out = []
    kinds = " or ".join("mimeType = 'application/vnd.google-apps.%s'" % k for k in EDITOR_KINDS)
    params = {"q": "modifiedTime > '%s' and trashed = false and (%s)" % (sources.rfc3339(since), kinds),
              "fields": "nextPageToken,files(id,name,modifiedByMeTime)", "pageSize": 200, "corpora": "allDrives",
              "includeItemsFromAllDrives": "true", "supportsAllDrives": "true"}
    for f in client.pages("https://www.googleapis.com/drive/v3/files", params, "files"):
        if not f.get("modifiedByMeTime") or sources.iso_ts(f["modifiedByMeTime"]) < since:
            continue
        label = f.get("name") or "(untitled)"
        try:
            revs = client.pages("https://www.googleapis.com/drive/v3/files/%s/revisions" % f["id"],
                          {"fields": "nextPageToken,revisions(modifiedTime,lastModifyingUser(me))", "pageSize": 1000}, "revisions")
            times = [sources.iso_ts(r["modifiedTime"]) for r in revs if (r.get("lastModifyingUser") or {}).get("me")]
            approx = False
        except google.GoogleError as e:
            if e.status not in GOOGLE_REFUSED:
                raise
            times, approx = [sources.iso_ts(f["modifiedByMeTime"])], True
        out += [dict({"ts": ts, "kind": "doc", "key": "doc:" + f["id"], "label": label}, **({"approx": True} if approx else {}))
                for ts in times if ts >= since]
    return out


def own_email(since, client, agents_address, cache_path, log):
    """Mail the owner sent from their Gmail, except to the agents' address; one activity per message. Each message's
    time, thread and subject are cached, and the cache keeps only messages inside the window. A damaged cache is
    reported with log(message) and every message is looked up again."""
    seen = sources.read_cache(cache_path, {}, log) or {}       # Gmail still has every message it lists
    api = gmailstate.API + "/messages"
    q = "in:sent after:%d" % int(since) + (" -to:%s" % agents_address if agents_address else "")
    out = []
    for m in client.pages(api, {"q": q, "maxResults": 500}, "messages"):
        if m["id"] not in seen:
            meta = client.get(api + "/" + m["id"], {"format": "metadata", "metadataHeaders": "Subject",
                                                    "fields": "internalDate,threadId,payload/headers"})
            subject = google.header(meta, "Subject")
            seen[m["id"]] = [int(meta["internalDate"]) / 1000, meta.get("threadId", m["id"]), subject]
        ts, thread, subject = seen[m["id"]]
        if ts >= since:
            out.append({"ts": ts, "kind": "mail", "key": "mail:" + thread, "label": subject or "(no subject)"})
    sources.write_json(cache_path, {k: v for k, v in seen.items() if v[0] >= since}, mode=0o600)
    return out


# ------------------------------------------------------------------ to-dos

def todos(since, repo, run):
    """Tasks the owner made in the to-do app (label source:app) in `repo`, and the moments they were closed."""
    gh = sources.find_bin("gh") or "gh"
    day = datetime.date.fromtimestamp(since).isoformat()
    r = run([gh, "issue", "list", "--repo", repo, "--state", "all", "--limit", "1000", "--label", "source:app",
             "--search", "updated:>=%s" % day, "--json", "number,title,createdAt,closedAt"],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise SourceError("gh issue list failed: %s" % (r.stderr or "").strip()[:200])
    out = []
    for i in json.loads(r.stdout or "[]"):
        for field in ("createdAt", "closedAt"):
            if i.get(field) and sources.iso_ts(i[field]) >= since:
                out.append({"ts": sources.iso_ts(i[field]), "kind": "todo", "key": "todo:%d" % i["number"], "label": i["title"]})
    return out


# ------------------------------------------------------------------ screen time

def screen_time(since, path):
    """The screen-time blocks stored at `path` that start at or after `since`, one activity each, the title without
    its unread count (COUNTER_RE). None stored at all means the Mac app has never uploaded any, which the page names."""
    if not path.exists():
        raise NotUploaded("nothing uploaded yet from the Mac app, which reads ActivityWatch")
    out = []
    for start, app, title in screen.held(path):
        title = COUNTER_RE.sub("", title).strip()
        if start >= since:
            out.append({"ts": start, "kind": "screen", "key": "screen:%s|%s" % (app, title[:80]),
                        "label": "%s: %s" % (app, title) if title else app})
    return out

