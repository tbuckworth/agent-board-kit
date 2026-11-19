"""Real agentboard objects on temporary files, which several test modules build the same way. Importing it first
points every config and state path at a scratch folder with an example owner (_kitenv)."""
import datetime
import json
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _kitenv  # noqa: E402,F401  (scratch config with an example owner, before agentboard reads any)
from _kitenv import AGENTS, OWNER  # noqa: E402
from agentboard import collect, gmailstate, google, meetings, sources  # noqa: E402

OWN = "me@example.com"         # the calendar login's own account (its primary calendar's id)
WORK = "work@example.org"      # a work calendar shared with that login
# Codex's start-up screen when a hook it runs is new or changed since it was trusted (Codex 0.167)
CODEX_HOOK_REVIEW = ("  Hooks need review\n  1 hook is new or changed.\n"
                     "  Hooks can run outside the sandbox after you trust them.\n"
                     "› 1. Review hooks\n  2. Trust all and continue\n  3. Continue without trusting (hooks won't run)\n"
                     "  enter confirm · esc skip\n")


def iso(ts):
    """An epoch time as Claude and Codex write it in their transcripts: ISO 8601 in UTC, ending in Z."""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def write_jsonl(path, rows):
    """`rows` as a JSON-lines file at `path`, its folder made if missing; the path, as a string."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


def parse_file(parse, path):
    """A whole transcript through one of the parsers sources.read_appended runs."""
    with open(path, "rb") as fh:
        return parse(fh, None, path)


def git(*args):
    """Run git with a fixed author, so a commit works wherever the machine's git identity is unset; its output.
    Raises CalledProcessError when git fails."""
    return subprocess.run(["git", "-c", "user.email=t@e", "-c", "user.name=t"] + list(args), capture_output=True,
                          text=True, check=True).stdout


def repo_with_worktree(d, repo, branch):
    """A real git repo d/repo with one commit, and its linked worktree d/repo-branch on a new branch."""
    git("init", "-q", "-b", "main", "%s/%s" % (d, repo))
    git("-C", "%s/%s" % (d, repo), "commit", "-q", "--allow-empty", "-m", "x")
    git("-C", "%s/%s" % (d, repo), "worktree", "add", "-q", "-b", branch, "%s/%s-%s" % (d, repo, branch))


def session(sid, name, at, **kw):
    """A row as the ~/.claude session registry gives it (sources.claude_sessions): a running interactive Claude agent
    in /x, its status last updated at `at`, started an hour before. kw: the fields that differ."""
    return dict({"cli": "claude", "id": sid, "name": name, "cwd": "/x", "raw_status": "idle", "kind": "interactive",
                 "live": True, "pid": None, "tmux": "", "waiting_for": "", "started_at": at - 3600, "status_at": at},
                **kw)


def collector(d, claude=lambda: [], panes=lambda: [], **kw):
    """A real collect.Collector on the files in folder `d`, any of them missing: the status log events.jsonl, the Codex
    home codex/ and the mailroom database mailroom.sqlite, with git read for real. Two readers of live state stand in:
    `claude` for the ~/.claude session registry (rows as session() builds them) and `panes` for tmux_list_panes, since
    the real ones hold the machine's running agents; and the agents' stuck waits and mail are looked for in an empty process
    table, not the live /proc. kw: the Collector's other arguments, waits and stuck_mail included."""
    d = Path(d)
    kw.setdefault("waits", lambda pids: sources.stuck_waits(pids, {}))
    kw.setdefault("stuck_mail", lambda pids, now=None: sources.stuck_mail(pids, {}, now))
    return collect.Collector(events=sources.EventLog(d / "events.jsonl"), codex=sources.CodexReader(d / "codex"),
                             mail=sources.MailStore(d / "mailroom.sqlite", owners=[OWNER], host_id="desktop"),
                             git=sources.GitCache(), claude=claude, panes=panes, **kw)


def mailroom_db(path):
    """A mailroom database at `path` with the routing tables the board reads, empty; an open connection to it."""
    con = sqlite3.connect(str(path))
    con.executescript("""
      create table sessions(tag text primary key, cli text, session_id text, session_name text, cwd text,
                            created_at integer, origin_host text);
      create table sent(id integer primary key, tag text, gmail_id text, thread_id text, message_id text,
                        to_addr text, subject text, sent_at integer, in_reply_to_inbound integer);
      create table inbound(id integer primary key, gmail_id text, thread_id text, message_id text, from_addr text,
                           from_name text, subject text, received_at integer, matched_tag text, match_rule text,
                           action text, detail text, body text, released_to text, released_at integer);
    """)
    return con


def gmail_of(me, box, inbox, unread, fail=None):
    """A real GmailState (the board's view of the agents' mail in the owner's Gmail) whose HTTP layer, Gmail's API over
    the network, which a test cannot reach, answers as the mailbox of `me` would. `box` maps Gmail ids to their
    Message-IDs (any other g<N> asked for is <gN@x>); the ids in `inbox` are in the inbox and those in `unread` unread,
    sets a test may change between checks, and a batchModify taking INBOX or UNREAD off takes ids out of them; a list
    is paged as the request's maxResults asks. `fail`: a URL fragment Gmail answers with an HTTP error. Also the calls
    made, (method, url, headers, body) each."""
    calls = []

    def http(method, url, data=None, headers=None, body=None):
        calls.append((method, url, headers, body))
        if fail and fail in url:
            raise google.GoogleError("Google API answered HTTP 503", 503)
        if url == google.TOKEN_URL:
            return {"access_token": "AT-secret", "expires_in": 3600}
        path, query = urllib.parse.urlsplit(url)[2:4]
        if path.endswith("/messages/batchModify"):
            for label in body["removeLabelIds"]:
                {"INBOX": inbox, "UNREAD": unread}[label].difference_update(body["ids"])
            return {}
        if path.endswith("/profile"):
            return {"emailAddress": me}
        if path.endswith("/messages"):
            args = urllib.parse.parse_qs(query)
            q = args["q"][0]
            if q.startswith("rfc822msgid:"):                            # the read check finding one mail
                want = sources.norm_id(q.split(":", 1)[1])
                return {"messages": [{"id": g, "threadId": "t-" + g} for g, m in box.items()
                                     if sources.norm_id(m) == want]}
            ids = sorted(inbox if "in:inbox" in q else unread)
            start, page = int(args.get("pageToken", ["0"])[0]), int(args["maxResults"][0])
            more = {"nextPageToken": str(start + page)} if start + page < len(ids) else {}
            return dict({"messages": [{"id": g} for g in ids[start:start + page]]}, **more)
        gid = path.rsplit("/", 1)[1]
        return {"payload": {"headers": [{"name": "Message-Id", "value": box.get(gid, "<%s@x>" % gid)}]}}
    creds = Path(tempfile.mkdtemp()) / "credentials.json"
    creds.write_text(json.dumps({"type": "authorized_user", "client_id": "c", "client_secret": "s",
                                 "refresh_token": "r"}))
    return gmailstate.GmailState(AGENTS, [OWNER], creds_path=creds, http=http, sync=True), calls


def event(eid, start, minutes=30, **kw):
    """An event as Calendar's events.list gives it with meetings.FIELDS; kw: the fields that differ or are added."""
    return dict({"id": eid, "iCalUID": eid + "@google.com", "status": "confirmed", "start": {"dateTime": iso(start)},
                 "end": {"dateTime": iso(start + minutes * 60)}}, **kw)


class FakeCalendar:
    """Google Calendar's API at the network boundary: the `http` that google.Client takes answers from the events held
    here, so the real client, its token handling and paging, and the real requests run. The real API needs the
    owner's login and reads live calendars. `calendars`: calendar id -> its events, or the GoogleError asking for
    them raises. The board only reads: any other method fails the test."""

    def __init__(self, calendars):
        self.calendars = calendars
        self.calls = []                  # (url without its query, query params, headers) of each API request

    def __call__(self, method, url, data=None, headers=None, timeout=20, body=None):
        if url == google.TOKEN_URL:
            return {"access_token": "EXAMPLE-TOKEN", "expires_in": 3600}
        base, _, query = url.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        self.calls.append((base, params, headers))
        assert method == "GET", (method, base)
        path = base[len(google.CALENDAR_API):]
        if path == "primary":
            return {"id": OWN}
        cal, _, eid = path.partition("/events")
        got = self.calendars[urllib.parse.unquote(cal)]
        if isinstance(got, Exception):
            raise got
        return {"items": got}


def config_file(**cfg):
    """A meetings config file holding `cfg`; its path."""
    path = Path(tempfile.mkdtemp()) / "meetings.json"
    path.write_text(json.dumps(cfg))
    return path


def meetings_reader(fake, config=None, creds=True):
    """A real meetings.Meetings that reads in the caller's thread, over `fake` (a FakeCalendar), with the config file
    `config` (none: the defaults) and a login file unless `creds` is false."""
    d = Path(tempfile.mkdtemp())
    if creds:
        (d / "token.json").write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
    return meetings.Meetings(config_path=config or d / "none.json", creds_path=d / "token.json", http=fake, sync=True)

