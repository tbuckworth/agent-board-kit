"""todo-desk: the desktop side of the Mac to-do app. Python stdlib only.

The Mac app keeps one `ssh desktop todo-desk serve` open, like AgentBoard's stream. Requests and replies
are JSON lines. GitHub is reached with the desktop's own `gh` login (`gh auth token`), so the Mac holds no
token and nothing listens on a port.

Settings: ~/.config/todo-desk/config.json (or $TODO_DESK_CONFIG_DIR/config.json), keys "repo" ("owner/name" of
the tasks repo; $TODO_REPO when the file has none), "owner_name", "owner_email", "repos_root", "general_repo".
The last four fall back to the mailroom's ~/.config/agent-mail/config.json.

    todo-desk serve                         JSON lines on stdin and stdout (the protocol is below)
    todo-desk snapshot [--full] [--pretty]  sync the cache and print it
    todo-desk message N [--text T]          print the orchestrator message for task N (sends nothing)
    todo-desk assign N [--text T] [--again] [--to SESSION]
                                            assign task N to a new agent, or send it to a running one
    todo-desk dispatch [--dry-run]          start agents the web app asked for (cron, every 2 min),
                                            then publish-agents
    todo-desk agents [--pretty]             the agent board's running agents, as the to-do apps list them
    todo-desk publish-agents [--force]      write them to .todo/agents.json for the web app, if changed
    todo-desk agent-mail N                  the newest mail task N's agent sent the owner (what the Mac opens)
    todo-desk config                        print the settings in effect (no GitHub call); exit 2 if "repo" is unset

Requests carry an "id" that the reply repeats; the helper answers them one at a time, in order:

    {"cmd": "hello", "since": ISO|null}     sync, then the issues updated since then (all if null),
                                            the config, the label names, "repo" ("owner/name"), and "known": every
                                            issue number cached, so the Mac drops issues deleted on GitHub whenever
                                            it was away
    {"cmd": "refresh", "full": bool}        sync now; changes arrive as an "issues" event
    {"cmd": "create", "title", "body", "labels"}
    {"cmd": "update", "number", "set": {"title"?, "body"?, "state"?, "stateReason"?}, "base": {"title"?, "body"?},
                      "add": [labels], "remove": [labels], "force": bool}
                                            a field changed on GitHub since "base" is a conflict, unless the
                                            body's sections merge cleanly; "force" overwrites. "stateReason"
                                            (COMPLETED or NOT_PLANNED) goes with a close: Triage's Archive closes
                                            as not planned
    {"cmd": "config", "ops": [{"op": "add_list"|"remove_list"|"set_group"|"add_group"|"remove_group", ...}]}
    {"cmd": "delete_list", "list"}          its tasks move to "tasks", and it leaves the config
    {"cmd": "comments", "number"}
    {"cmd": "assign_preview", "number", "text", "to"?: SESSION}
                                            the orchestrator message, or with "to" the message that agent is sent
    {"cmd": "assign", "number", "text", "again": bool, "to"?: SESSION}
                                            with "to", the task goes straight into that running agent (agent-send)
                                            and the issue gets a "started" marker; without it the orchestrator
                                            starts a new agent
    {"cmd": "agents"}                       {"agents": [{id, cli, name, state, folder, note, active, group}],
                                            "generated"}: the board's live agents, the ones wanting the owner first
    {"cmd": "agent_mail", "number"}         {"name", "mail": {message_id, subject, sent_at, thread} | null, "owner"}:
                                            the newest mail the task's agent sent the owner, from the mailroom's
                                            store (read-only), with its thread id in the owner's Gmail when the
                                            agent board's optional Gmail read-check finds it (else null: the Mac
                                            searches for the Message-ID)
    {"cmd": "set_parent", "number", "parent": N|null}
                                            put the task in group N (a GitHub sub-issue of N, replacing any other
                                            parent) or take it out of its group (null), adding or removing the
                                            `subtask` label too: that is what makes other devices' incremental
                                            syncs notice, as linking alone doesn't change updatedAt. Idempotent.
    {"cmd": "comment", "number", "body"}    a plain comment (the other tasks of a several-task agent request)

Each issue carries "parent": the number of the group it is in, or null. Queued edits (create,
update, config, delete_list, set_parent) carry an "op" id, so one that already happened is not done twice.

Events the helper sends by itself: {"type": "issues", "issues": [...], "removed": [...]}, {"type": "config"},
{"type": "ping"} every 20 s, and {"type": "error"} when a background sync fails.
"""
import argparse
import base64
import datetime as dt
import fcntl
import ipaddress
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from issue_body import CHECKLIST, join_sections, split_sections

API = "https://api.github.com"
HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("TODO_DESK_CONFIG_DIR", HOME / ".config/todo-desk"))
MAIL_CONFIG_DIR = Path(os.environ.get("AGENT_MAIL_CONFIG_DIR", HOME / ".config/agent-mail"))
DEFAULT_REPOS_ROOT = "~/code"
REPO_RE = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}\Z")
CACHE_PATH = Path(os.environ.get("TODO_DESK_CACHE", HOME / ".cache/todo-desk/cache.json"))
STATE_DIR = Path(os.environ.get("TODO_DESK_STATE", HOME / ".local/state/todo-desk"))
VERSION = 1
FULL_SYNC_EVERY = 6 * 3600     # an incremental sync never sees an issue deleted on GitHub; a full one drops it
PRUNED_KEEP_DAYS = 30          # how long a dropped issue number is remembered, for clients that were away
QUIET_SAVE_SECONDS = 1800      # a sync that changed nothing rewrites the 1.3 MB cache (for synced_at) at most this often

POLL_SECONDS = 60
PING_SECONDS = 20
STALL_SECONDS = 120
MAX_SECONDS = 12 * 3600
GONE_TIMEOUTS = 8              # consecutive retransmission timeouts: ~50 s of a Mac acknowledging nothing
MAX_TEXT = 4000
MAX_NOTES = 6000

ASSIGNED_LABEL = "status:agent-assigned"
ASSIGNED_COLOR = "AD7D48"          # AgentBoard's busy caramel
SINGLE_PREFIXES = ("list:", "section:", "group:", "action:", "priority:", "importance:", "urgency:", "due:",
                   "follow-up:")
# Colours for labels the apps make on first use: tags, and when to chase a task waiting on someone
NEW_LABEL_COLORS = (("tag:", "5319E7"), ("follow-up:", "FBCA04"), ("status:waiting-cleared", "0E8A16"),
                    ("status:waiting", "FBCA04"))
MARK = "<!-- todo-agent:%s -->"
MARK_RE = re.compile(r"<!--\s*todo-agent:(assigned|started|done|blocked|failed)\s*-->")
# The web app (in a browser, on a phone or a laptop) can't start agents, so it asks for one: this label plus a
# comment starting with the request marker. `todo-desk dispatch` turns each request into a normal assign.
# The marker is not a MARK_RE kind.
REQUESTED_LABEL = "status:agent-requested"
# `to=<session id>` sends the task to that running agent instead of starting a new one
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
REQUEST_RE = re.compile(r"\A\s*<!--\s*todo-agent-request(?:\s+to=(?P<to>%s))?\s*-->" % UUID)
UUID_RE = re.compile(r"\A%s\Z" % UUID)
NO_INSTRUCTIONS = "No extra instructions."
# Why an issue is closed: done, or archived (Triage's Archive: closed as not planned, the list label kept)
CLOSE_REASONS = ("COMPLETED", "NOT_PLANNED")


class GitHubError(Exception):
    pass


class ConfigError(Exception):
    pass


def _read_json(path):
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def load_config():
    """The helper's settings. "repo" comes from ~/.config/todo-desk/config.json, else $TODO_REPO. The owner keys and
    the repo locations come from that file, else from the mailroom's config (the kit installer writes both)."""
    own = _read_json(CONFIG_DIR / "config.json")
    mail = _read_json(MAIL_CONFIG_DIR / "config.json")
    cfg = {"repo": (own.get("repo") or os.environ.get("TODO_REPO") or "").strip() or None}
    for k in ("owner_name", "owner_email", "repos_root", "general_repo"):
        v = own.get(k) or mail.get(k)
        cfg[k] = v.strip() if isinstance(v, str) and v.strip() else None
    if not cfg["owner_email"]:                      # the mailroom's only allowlisted sender is the owner
        allow = mail.get("allowlist")
        if isinstance(allow, list) and allow and isinstance(allow[0], str):
            cfg["owner_email"] = allow[0]
    return cfg


def repo_parts(repo):
    """("owner", "name") from "owner/name", or a ConfigError that says how to set it."""
    r = (repo or "").strip()
    if not r:
        raise ConfigError('no tasks repo configured: set "repo": "owner/name" in %s (or export TODO_REPO=owner/name)'
                          % (CONFIG_DIR / "config.json"))
    if not REPO_RE.match(r):
        raise ConfigError('the tasks repo %r is not of the form "owner/name"' % r)
    owner, name = r.split("/", 1)
    return owner, name


class LaunchFailed(ValueError):
    """The agent did not start, or the task did not reach the running agent; the issue already says so."""


class Conflict(Exception):
    def __init__(self, fields, issue):
        super().__init__("changed on GitHub: " + ", ".join(fields))
        self.fields, self.issue = fields, issue


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def iso_minus(s, seconds):
    return (parse_iso(s) - dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ GitHub
def gh_token():
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    # `ssh desktop todo-desk serve` runs with a short non-interactive PATH, so look in the usual places as well
    gh = shutil.which("gh") or next((str(c) for c in (HOME / ".local/bin/gh", Path("/usr/local/bin/gh"),
                                                      Path("/home/linuxbrew/.linuxbrew/bin/gh"), Path("/snap/bin/gh"))
                                     if c.exists()), "/usr/bin/gh")
    try:
        out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitHubError("gh is not available on the desktop: %s" % e)
    if out.returncode != 0 or not out.stdout.strip():
        raise GitHubError("gh is not logged in on the desktop (gh auth token: %s)" % out.stderr.strip()[:200])
    return out.stdout.strip()


class GitHub:
    """Just enough of GitHub's GraphQL and REST APIs, with the desktop's gh token held in memory. `opener` is
    urllib's urlopen, swapped for a fake in tests."""

    def __init__(self, token=None, opener=None):
        self.token = token or gh_token()
        self.opener = opener or urllib.request.urlopen

    def _request(self, method, url, body=None, headers=None, accept="application/vnd.github+json", raw=False, ok=()):
        """(status, body, headers): the body parsed as JSON, or the bytes when `raw`. A 304, and any error status in
        `ok`, comes back as a status; other errors raise GitHubError (with .status), after up to two retries of a
        502, 503 or 504 or a network failure."""
        data = None if body is None else json.dumps(body).encode()
        h = {"Authorization": "Bearer " + self.token, "Accept": accept,
             "User-Agent": "todo-desk", "X-GitHub-Api-Version": "2022-11-28"}
        if data is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        decode = (lambda b: b) if raw else (lambda b: json.loads(b) if b else None)   # noqa: E731
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, method=method, headers=h)
            try:
                with self.opener(req, timeout=60) as r:
                    return r.status, decode(r.read()), dict(r.headers)
            except urllib.error.HTTPError as e:
                content = e.read() or b""
                if e.code == 304:
                    return 304, None, dict(e.headers or {})
                if e.code in ok:
                    return e.code, decode(content), dict(e.headers or {})
                if e.code in (502, 503, 504) and attempt < 2:
                    time.sleep(1 + attempt * 2)
                    continue
                try:
                    msg = json.loads(content).get("message", "")
                except ValueError:
                    msg = content[:200].decode(errors="replace")
                if e.code == 401:
                    raise GitHubError("GitHub rejected the desktop's gh token (401); run `gh auth login` on the desktop")
                err = GitHubError("GitHub %s %s: %s %s" % (method, url.replace(API, ""), e.code, msg))
                err.status = e.code
                raise err
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < 2:
                    time.sleep(1 + attempt * 2)
                    continue
                raise GitHubError("GitHub unreachable: %s" % e)
        raise GitHubError("GitHub unreachable")

    def rest(self, method, path, body=None, headers=None):
        status, obj, hdrs = self._request(method, API + path, body, headers)
        return obj if headers is None else (status, obj, hdrs)

    def graphql(self, query, variables=None):
        _, obj, _ = self._request("POST", API + "/graphql", {"query": query, "variables": variables or {}})
        if obj.get("errors"):
            raise GitHubError("GraphQL: " + "; ".join(e.get("message", "?") for e in obj["errors"]))
        return obj["data"]


ISSUE_FIELDS = "number title body state url createdAt updatedAt parent { number } labels(first: 40) { nodes { name } }"
SUBTASK_LABEL = "subtask"         # on every task in a group (a sub-issue), so incremental syncs notice the link
ISSUES_QUERY = """
query($owner: String!, $repo: String!, $after: String, $since: DateTime) {
  repository(owner: $owner, name: $repo) {
    issues(first: 100, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}, filterBy: {since: $since}) {
      pageInfo { hasNextPage endCursor }
      nodes { %s }
    }
  }
}""" % ISSUE_FIELDS


def from_graphql(n):
    return {"number": n["number"], "title": n["title"], "body": n.get("body") or "", "state": n["state"],
            "url": n["url"], "createdAt": n["createdAt"], "updatedAt": n["updatedAt"],
            "labels": [l["name"] for l in (n.get("labels") or {}).get("nodes", [])],
            "parent": (n.get("parent") or {}).get("number")}


def from_rest(i):
    """REST answers don't say which group a task is in: no "parent" key, and put() keeps the one cached."""
    return {"number": i["number"], "title": i["title"], "body": i.get("body") or "", "state": i["state"].upper(),
            "url": i["html_url"], "createdAt": i["created_at"], "updatedAt": i["updated_at"],
            "labels": [l["name"] if isinstance(l, dict) else l for l in i.get("labels", [])]}


# ------------------------------------------------------------------ issue bodies
def merge_body(base, mine, theirs):
    """Three-way merge by section. Returns the merged body, or None when the same section changed both ways."""
    norm = lambda s: (s or "").strip()                       # noqa: E731
    secs = [split_sections(x) for x in (base, mine, theirs)]
    if any(len({h for h, _ in x}) != len(x) for x in secs):   # a heading used twice: merge whole bodies only
        if norm(mine) == norm(base) or norm(mine) == norm(theirs):
            return theirs
        return mine if norm(theirs) == norm(base) else None
    b, m, t = (dict(x) for x in secs)
    order = [h for h, _ in split_sections(theirs)]
    order += [h for h, _ in split_sections(mine) if h not in order]
    out = []
    for h in order:
        bv, mv, tv = b.get(h), m.get(h), t.get(h)
        if norm(mv) == norm(tv):
            v = tv if tv is not None else mv
        elif norm(mv) == norm(bv):
            v = tv                                          # only GitHub changed it (None: they removed it)
        elif norm(tv) == norm(bv):
            v = mv                                          # only the Mac changed it (None: it removed it)
        else:
            return None
        if v is not None:
            out.append((h, v))
    return join_sections(out)


def notes_of(body):
    return split_sections(body)[0][1].strip()


def checklist_of(body):
    for h, text in split_sections(body):
        if h == CHECKLIST:
            return [(m.group(1).lower() == "x", m.group(2).strip())
                    for m in (re.match(r"^\s*- \[([ xX])\]\s*(.+)", l) for l in text.split("\n")) if m]
    # issues imported before the app put loose checkboxes in the notes
    return [(m.group(1).lower() == "x", m.group(2).strip())
            for m in (re.match(r"^\s*- \[([ xX])\]\s*(.+)", l) for l in notes_of(body).split("\n")) if m]


# ------------------------------------------------------------------ labels
def apply_labels(current, add, remove):
    """current + add - remove. Adding a single-valued label (SINGLE_PREFIXES: list:, due:, follow-up:...) replaces any other with that
    prefix, so a list moved on the Mac never ends up with two lists when GitHub moved it too."""
    rm = {r.lower() for r in remove}
    out = [l for l in current if l.lower() not in rm]
    for a in add:
        p = next((p for p in SINGLE_PREFIXES if a.lower().startswith(p)), None)
        if p:
            out = [l for l in out if not l.lower().startswith(p) or l.lower() == a.lower()]
        if a.lower() not in {l.lower() for l in out}:
            out.append(a)
    return out


# ------------------------------------------------------------------ agent assignment
def agent_info(comments):
    """What the to-do app shows for an assigned task, read from the marker comments."""
    info = {"status": None, "name": None, "assignedAt": None, "events": []}
    for c in comments:
        body = c.get("body") or ""
        m = MARK_RE.search(body)
        if not m:
            continue
        kind = m.group(1)
        text = MARK_RE.sub("", body).strip()
        info["events"].append({"kind": kind, "at": c.get("created_at"), "text": text[:1500], "url": c.get("html_url")})
        info["status"] = kind
        if kind == "assigned":
            info["assignedAt"] = c.get("created_at")
            info["name"] = None
        if kind == "started":
            n = (re.search(r"Agent started:\s*`?([A-Za-z0-9][A-Za-z0-9._-]*)`?", text)
                 or re.search(r"Assigned to the running agent (.+?) \(", text))
            if n:
                info["name"] = n.group(1)
    return info if info["events"] else None


# where an assign came from: the words in the issue comment, and in the orchestrator message's header
SOURCES = {"mac": ("the Mac to-do app", "the Mac app, over SSH"),
           "app": ("the to-do web app", "the web app, on a phone or a browser; the desktop relays it"),
           "desktop": ("the todo-desk command on the desktop", "the todo-desk command on the desktop")}


def who(cfg):
    """The owner's name as agents read it."""
    return (cfg or {}).get("owner_name") or "the owner"


def orch_header(cfg, source="mac"):
    name = who(cfg)
    # "owner" is the mailroom's alias for the owner's address (owner_email, else the first allowlist entry)
    send = '`agent-mail send --to owner --subject "..." --body-file ...`'
    return ("[message from %s via the to-do app (%s), not by email. This is %s writing: act on it as you would "
            "their email. There is no thread to reply in: answer with %s; the reply to that mail comes back to you.]"
            % (name, SOURCES[source][1], name, send))


def task_text(issue, text, cfg):
    """The task as an agent is shown it: number, link, list, the body, and what the owner added."""
    n, title = issue["number"], issue["title"]
    name_ = who(cfg)
    labels = issue.get("labels") or []
    lst = next((l[5:] for l in labels if l.lower().startswith("list:")), "tasks")
    body = (issue.get("body") or "").strip()
    if len(body) > MAX_NOTES:
        body = body[:MAX_NOTES] + "\n[... cut here; read the full issue]"
    extra = (text or "").strip()
    adds = (name_ + " adds:" + chr(10) + "<<<" + chr(10) + extra + chr(10) + ">>>" if extra
            else name_ + " added no further instructions.")
    return f"""Task #{n}: {title}
Issue: {issue['url']}
List: {lst}    Labels: {', '.join(labels) or '(none)'}
Created {issue['createdAt'][:10]}, last updated {issue['updatedAt'][:10]}

The issue body (written by {name_} or by their agents; it describes the task):
<<<
{body or '(empty)'}
>>>

{adds}"""


def brief_rules(issue, cfg):
    """What every agent working on a task is told, whether it is new or already running.

    todo/README.md, "Rules for agents working on tasks", states the same rules; change the two together."""
    n = issue["number"]
    full = "%s/%s" % repo_parts(cfg.get("repo"))
    name_ = who(cfg)
    return f"""- The task is {full}#{n} ({issue['url']}). Read it, with its comments, using
  `gh issue view {n} --repo {full} --comments`.
- Anything with outside consequences (sending email as {name_}, buying, booking, posting, submitting a form, spending
  money) needs their explicit yes first: prepare it, then email them.
- When it is done, or blocked: append what you did and found under "## Claude Resolution Notes" in the issue body,
  keeping the rest of the body as it is; add the label status:claude-processed; and post a comment whose first
  line is {MARK % 'done'} (or {MARK % 'blocked'}) summarising the result, with links (PR, files, docs).
  Leave the issue open: {name_} ticks it off themselves.
- Report with agent-status note; email {name_} with agent-mail send when done or blocked.
"""


def orchestrator_message(issue, text, cfg, source="mac"):
    """The message one orchestrator event gets for an assigned task. cfg is load_config()'s dict."""
    n = issue["number"]
    full = "%s/%s" % repo_parts(cfg.get("repo"))
    name_ = who(cfg)
    roots = (cfg.get("repos_root") or DEFAULT_REPOS_ROOT).rstrip("/")
    general = cfg.get("general_repo") or roots + "/general"
    return f"""{orch_header(cfg, source)}

{name_} assigned this to-do to an agent from the to-do app. Start one agent to do it.

{task_text(issue, text, cfg)}

How to start it:
- Repo: if the task clearly belongs to an existing repo under {roots}, start the agent there on a new branch off
  origin/main. If it clearly needs a repo of its own, make one (agent-spawn --new-repo). Otherwise start it in
  {general} on a new branch.
- Name it for what the task is, as you name the agents you start from email:
  2 to 4 lowercase words joined by hyphens (invoice-export-fix, dentist-booking), without the issue
  number and not starting with todo-. It must not clash with a name in agent-send --list. Pass it to
  agent-spawn --name.
- Pick the account as usual (agent-usage --repo REPO, if several logins are set up).
- Once it is running, comment on the issue in exactly this form (the to-do app reads it):
    gh issue comment {n} --repo {full} --body "{MARK % 'started'}
    Agent started: NAME (REPO@BRANCH, ACCOUNT)"
- Answer {name_} with agent-mail send: one to three lines with the agent's name, repo and branch, and account.
- If you cannot start it, comment on the issue with a body starting {MARK % 'failed'} that says why, and email {name_}.

Put this in the agent's brief, after the task itself:
{brief_rules(issue, cfg)}"""


def running_header(cfg, source="mac"):
    """The first line of a task sent straight into a running agent: who it is from, and through what."""
    return "[relayed from %s via the to-do app (%s)]" % (who(cfg), SOURCES[source][1])


def running_agent_message(issue, text, cfg, source="mac"):
    """The task, sent straight into an agent that is already running (Assign to a running agent)."""
    name_ = who(cfg)
    answer = "agent-mail send --to owner"          # the mailroom's alias for the owner's address
    return f"""{running_header(cfg, source)}

{name_} assigned you a to-do from the to-do app.

{task_text(issue, text, cfg)}

{name_} chose you for this task, alongside what you are doing. Fit it in sensibly: finish or checkpoint your current
step first unless they say it's urgent.

{brief_rules(issue, cfg)}
(Sent from the to-do app, not by email: there is no thread to reply in. Answer {name_} with {answer} if needed.)
"""


# ------------------------------------------------------------------ the agent board's running agents
# The Mac board's tile groups, if the board stores them on the desktop (the kit's board does not yet): with them,
# each running agent in the to-do apps' picker shows its group; without them, "group" is null.
BOARD_GROUPS = HOME / ".local/state/agent-board/groups.json"
STATE_ORDER = ("waiting", "blocked", "idle", "background", "busy", "stale")
PUBLISH_EVERY = 300                # seconds between agents.json writes when only states or notes changed
AGENTS_PATH = ".todo/agents.json"


def find_agent_board():
    """The board's command: on PATH, else the installer's wrapper in ~/.local/bin."""
    exe = shutil.which("agent-board") or str(HOME / ".local/bin/agent-board")
    return exe if os.path.exists(exe) else None


# The board's own agent rows, without git, PRs or metrics: a few seconds, where `agent-board snapshot` takes much
# longer, too slow for the dispatch timer every 2 minutes. The owner's read state is kept as `agent-board snapshot`
# gets it, from the board's Mail tab (the mailroom's mailbox) and, when the board has it turned on, Gmail: without it,
# an agent whose mail the owner has read would show as waiting on them. Run in a child process (with the board's own
# python), so the board's code never loads into this one.
BOARD_ROWS = """import json, sys
sys.path.insert(0, sys.argv[1])
from agentboard import collect, gmailstate, mailbox, sources
gmail = gmailstate.for_owner(sync=True)
box = sources.Periodic(mailbox.summary, mailbox.REFRESH, None, sync=True)
box.maybe_refresh()
col = collect.Collector(gmail=gmail, mailbox=box)
if gmail is not None:
    col.agents(with_git=False)     # tells Gmail which mails the board cares about
    gmail.maybe_refresh()          # then one look-up, so the rows show read state
print(json.dumps({"agents": col.agents(with_git=False)[0]}, default=str))
"""


KIT_BOARD = Path(__file__).resolve().parent.parent.parent / "board"     # todo/desk/ -> the kit's board/ component


def board_root(exe=None):
    """The directory holding the board's `agentboard` package: the kit's board/ next to todo/, else the checkout the
    agent-board command lives in (when it is a symlink to the script rather than the installer's wrapper)."""
    cands = [KIT_BOARD] + ([Path(os.path.realpath(exe)).parent.parent] if exe else [])
    return next((str(c) for c in cands if (c / "agentboard" / "__init__.py").exists()), None)


def board_snapshot():
    """{"agents": [the board's rows]}: the same list and states the agent board shows."""
    exe = find_agent_board()
    root = board_root(exe)
    cmds = ([[sys.executable, "-c", BOARD_ROWS, root]] if root else []) + ([[exe, "snapshot"]] if exe else [])
    if not cmds:                                      # the full snapshot is the fallback
        raise ValueError("agent-board is not installed on the desktop")
    errors = []
    # BOARD_ROWS reads the board's Python API directly, which breaks when the board changes its internals; the board's
    # own `snapshot` command is its stable interface, only slower. It stays as a second way in, with the first failure
    # on stderr, rather than lose the agent list whenever the board changes.
    for cmd in cmds:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append(str(e))
            continue
        if out.returncode == 0:
            try:
                snap = json.loads(out.stdout)
            except ValueError:
                errors.append("not JSON")
                continue
            if errors:
                print("todo-desk: warning: reading the agent board's rows failed (%s); used `agent-board snapshot`"
                      % errors[0], file=sys.stderr)
            return snap
        errors.append((out.stderr or out.stdout).strip()[-300:])
    raise ValueError("could not read the agent board: %s" % "; then `agent-board snapshot`: ".join(errors))


# An agent's latest mail to the owner, for double-clicking the agent on a task (as on the agent board). The mailroom
# records every mail an agent session sends; the thread id in the owner's own Gmail comes from the agent board's
# optional Gmail read-check (its "gmail_credentials" login), as the board does for its "open in Gmail". Without it the
# Mac opens a Gmail search for the Message-ID instead. Run in a child process, like BOARD_ROWS.
MAIL_STATE_DIR = Path(os.environ.get("AGENT_MAIL_STATE_DIR", HOME / ".local/state/agent-mail"))
MAILROOM_DB = Path(os.environ.get("TODO_DESK_MAILROOM", MAIL_STATE_DIR / "mailroom.sqlite"))
BOARD_THREAD = """import json, sys
sys.path.insert(0, sys.argv[1])
thread = None
try:
    from agentboard import gmailstate
except ImportError:                # a board without the read-check: no thread, the Mac searches instead
    gmailstate = None
g = gmailstate.for_owner(sync=True) if gmailstate else None
if g is not None:
    g.is_read(sys.argv[2])         # asks for it: the poll then looks it up
    g.maybe_refresh(force=True)
    thread = g.thread_of(sys.argv[2])
print(json.dumps(thread))
"""


def mail_owners(cfg=None, path=None):
    """The owner's addresses, lower case: todo-desk's owner_email (load_config's, which falls back to the mailroom's),
    then the mailroom's allowlist. [] when neither is set."""
    cfg = cfg if cfg is not None else load_config()
    path = Path(path or MAIL_CONFIG_DIR / "config.json")
    try:
        config = json.loads(path.read_text())
    except FileNotFoundError:
        config = {}
    allow = config.get("allowlist") if isinstance(config, dict) else None
    if not isinstance(config, dict) or not isinstance(allow or [], list) or not all(isinstance(a, str) for a in allow or []):
        raise ValueError("%s: expected an object whose allowlist is a list of addresses" % path)
    out = []
    for a in [cfg.get("owner_email")] + list(allow or []):
        if a and a.strip().lower() not in out:
            out.append(a.strip().lower())
    return out


def latest_mail(name, folder=None, db=None, owners=None):
    """{"message_id", "subject", "sent_at"} of the newest mail an agent session called `name`, or working in a
    directory called `folder`, sent the owner, or None. Every session of that name counts: a restarted or moved agent
    keeps its name and gets a new tag. The folder finds an agent renamed after it started."""
    db = Path(db or MAILROOM_DB)
    if not (name or folder) or not db.exists():
        return None
    owners = owners if owners is not None else mail_owners()
    tail = "/" + folder if folder else None
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
    try:
        rows = con.execute("select s.message_id, s.subject, s.sent_at, s.to_addr from sent s join sessions x "
                           "on x.tag = s.tag where (x.session_name = ? or substr(x.cwd, -length(?)) = ?) "
                           "and s.message_id is not null order by s.sent_at desc", (name, tail, tail)).fetchall()
    finally:
        con.close()
    for mid, subject, at, to in rows:
        if any(o in (to or "").lower() for o in owners):
            return {"message_id": mid, "subject": subject or "", "sent_at": iso_from_ts(at)}
    return None


def agent_folder(info):
    """The agent's working directory's name, from the orchestrator's "Agent started: NAME (repo@worktree, ...)"
    comment: the worktree of repo `to-do` called `todo-1-x` is the directory `to-do-todo-1-x`."""
    for e in reversed((info or {}).get("events") or []):
        if e.get("kind") == "started":
            m = re.search(r"\(([A-Za-z0-9._-]+)@([A-Za-z0-9._-]+)[,)]", e.get("text") or "")
            return "%s-%s" % m.groups() if m else None
    return None


def gmail_thread(message_id):
    """The mail's thread id in the owner's Gmail, or None when the board's Gmail read-check has none for it (turned
    off, or not delivered yet). A lookup that fails raises ValueError."""
    if not message_id:
        return None
    root = board_root(find_agent_board())
    if not root:
        raise ValueError("agent-board is not installed on the desktop")
    out = subprocess.run([sys.executable, "-c", BOARD_THREAD, root, message_id],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise ValueError("could not look up the Gmail thread: %s" % (out.stderr or out.stdout).strip()[-300:])
    try:
        return json.loads(out.stdout)
    except ValueError:
        raise ValueError("the Gmail thread lookup printed %r, not JSON" % out.stdout[:200])


def board_groups(stored):
    """{session id: "Group" or "Group / Sub-group"} from the board's stored groups (see BOARD_GROUPS)."""
    out = {}
    for g in (stored or {}).get("groups") or []:
        for m in g.get("members") or []:
            out[m] = g.get("name")
        for sub in g.get("subgroups") or []:
            for m in sub.get("members") or []:
                out[m] = "%s / %s" % (g.get("name"), sub.get("name"))
    return out


def read_board_groups():
    try:
        return board_groups(json.loads(BOARD_GROUPS.read_text()))
    except (OSError, ValueError):
        return {}


def iso_from_ts(ts):
    if not ts:
        return None
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def agent_records(snapshot, groups=None):
    """The board's running agents as the to-do apps show them: the ones that most want the owner first (waiting on
    them, blocked, idle...), then the most recently active."""
    groups = groups or {}
    out = []
    for a in (snapshot or {}).get("agents") or []:
        if not a.get("live") or a.get("state") not in STATE_ORDER or not UUID_RE.match(a.get("id") or ""):
            continue
        folder = os.path.basename((a.get("cwd") or "").rstrip("/"))
        note = re.sub(r"\s+", " ", (a.get("last_note") or {}).get("text") or "").strip()
        out.append({"id": a["id"], "cli": a.get("cli") or "claude",
                    "name": a.get("display") or a.get("name") or folder or a["id"][:8],
                    "state": a["state"], "folder": folder,
                    "note": note[:159] + "…" if len(note) > 160 else note,
                    "active": iso_from_ts(a.get("last_activity")), "group": groups.get(a["id"])})
    out.sort(key=lambda r: r["active"] or "", reverse=True)
    out.sort(key=lambda r: STATE_ORDER.index(r["state"]))
    return out


def should_publish(records, last, now):
    """Write agents.json? Not when nothing but `active` changed. When only states or notes did, at most every
    PUBLISH_EVERY seconds; at once when an agent came or went. `last` is what was kept after the previous write."""
    key = [{k: v for k, v in r.items() if k != "active"} for r in records]
    if last and key == last.get("key"):
        return False
    ids = sorted(r["id"] for r in records)
    return not (last and ids == last.get("ids") and now - last.get("at", 0) < PUBLISH_EVERY)


def send_to_agent(cli, sid, text):
    """agent-send with the full id and the raw message. Not --exact-id: that refuses when a dead login-move leftover
    shares the id, while plain resolution picks the live one (agent-board's actions.py does the same)."""
    exe = shutil.which("agent-send") or str(HOME / ".local/bin/agent-send")
    if not os.path.exists(exe):
        return 127, "agent-send not found"
    try:
        out = subprocess.run([exe, "--raw", "--cli", cli, sid], input=text, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return out.returncode, (out.stdout + out.stderr).strip()


def find_orchestrate():
    exe = shutil.which("agent-orchestrate") or str(HOME / ".local/bin/agent-orchestrate")
    return exe if os.path.exists(exe) else None


def audit(entry):
    """Append to the actions log. A failed write is reported, but doesn't undo the action it records."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / "actions.jsonl", "a") as fh:
            fh.write(json.dumps(dict(entry, ts=time.time())) + "\n")
    except OSError as e:
        print("todo-desk: could not write the audit log %s: %s (entry %s)"
              % (STATE_DIR / "actions.jsonl", e, json.dumps(entry)), file=sys.stderr)


# ------------------------------------------------------------------ the cache and every GitHub operation
class Desk:
    def __init__(self, gh=None, cache_path=CACHE_PATH, popen=None, config=None, board=None, send=None, mail=None,
                 thread=None):
        self.cfg = config if config is not None else load_config()
        self.owner, self.repo = repo_parts(self.cfg.get("repo"))     # before gh: a missing repo is the clearer error
        self.full_name = "%s/%s" % (self.owner, self.repo)
        self.gh = gh or GitHub()
        self.path = Path(cache_path)
        self.popen = popen or subprocess.Popen
        self.board = board or board_snapshot          # () -> the agent board's snapshot
        self.send = send or send_to_agent             # (cli, session id, text) -> (exit code, output)
        self.find_mail = mail or latest_mail          # (session name, folder) -> the newest mail it sent the owner, or None
        self.find_thread = thread or gmail_thread     # (Message-ID) -> its thread id in the owner's Gmail, or None
        self.labels = None              # name.lower() -> name
        self.data = {"version": VERSION, "repo": self.full_name, "synced_at": None, "issues": {}, "config": None,
                     "config_sha": None, "config_etag": None, "agents": {}, "full_synced_at": None, "pruned": {}}
        self.disk_synced_at = None      # synced_at as the cache file holds it
        self.load()

    # -- cache file
    def load(self):
        """A cache written for another tasks repo is ignored (the next sync is a full one and replaces it)."""
        try:
            d = json.loads(self.path.read_text())
            if d.get("version") == VERSION and (d.get("repo") or "").lower() == self.full_name.lower():
                self.data.update(d)
                self.disk_synced_at = d.get("synced_at")
        except (OSError, ValueError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp.%d" % os.getpid())
        tmp.write_text(json.dumps(self.data, separators=(",", ":")))
        os.replace(tmp, self.path)
        self.disk_synced_at = self.data.get("synced_at")

    def lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path.with_suffix(".lock"), "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    def issue_out(self, issue):
        out = dict(issue)
        a = self.data["agents"].get(str(issue["number"]))
        if a:
            out["agent"] = a
        return out

    def put(self, issue):
        key = str(issue["number"])
        if "parent" not in issue:                   # from a REST answer: keep the group already known
            issue = dict(issue, parent=(self.data["issues"].get(key) or {}).get("parent"))
        self.data["issues"][key] = issue

    # -- sync
    def sync(self, full=False):
        """Fetch what changed on GitHub since the last sync (everything if full). Returns (changed, removed).
        Every FULL_SYNC_EVERY seconds a sync is a full one anyway, so issues deleted on GitHub leave the cache."""
        with self.lock():
            self.load()
            last_full = self.data.get("full_synced_at")
            if not full and (not last_full or parse_iso(now_iso()) - parse_iso(last_full) > dt.timedelta(seconds=FULL_SYNC_EVERY)):
                full = True
            since = None if full or not self.data["synced_at"] else iso_minus(self.data["synced_at"], 120)
            started = now_iso()
            fetched, after = [], None
            while True:
                d = self.gh.graphql(ISSUES_QUERY, {"owner": self.owner, "repo": self.repo, "after": after,
                                                   "since": since})
                page = d["repository"]["issues"]
                fetched += [from_graphql(n) for n in page["nodes"]]
                if not page["pageInfo"]["hasNextPage"]:
                    break
                after = page["pageInfo"]["endCursor"]
            changed = []
            for issue in fetched:
                if self.data["issues"].get(str(issue["number"])) != issue:
                    changed.append(issue)
                    self.put(issue)
            removed = []
            if full:
                seen = {str(i["number"]) for i in fetched}
                removed = [int(k) for k in self.data["issues"] if k not in seen]
                for k in removed:
                    self.data["issues"].pop(str(k), None)
                    self.data["agents"].pop(str(k), None)
                pruned = self.data.setdefault("pruned", {})
                pruned.update({str(k): started for k in removed})
                cutoff = iso_minus(started, PRUNED_KEEP_DAYS * 86400)
                self.data["pruned"] = {k: t for k, t in pruned.items() if t >= cutoff}
                self.data["full_synced_at"] = started
            for issue in changed:
                self.refresh_agent(issue)
            self.data["synced_at"] = started
            config_changed = self.sync_config()
            # a quiet minute changes only synced_at: an older one on disk just widens the next sync's window
            if changed or removed or config_changed or full or not self.disk_synced_at or (
                    parse_iso(started) - parse_iso(self.disk_synced_at) > dt.timedelta(seconds=QUIET_SAVE_SECONDS)):
                self.save()
        return [self.issue_out(i) for i in changed], removed, config_changed

    def refresh_agent(self, issue):
        """Agent status for a task that has (or had) the assigned label, from its marker comments."""
        key = str(issue["number"])
        if ASSIGNED_LABEL not in issue["labels"] and key not in self.data["agents"]:
            return
        comments = self.gh.rest("GET", "/repos/%s/%s/issues/%d/comments?per_page=100" % (self.owner, self.repo, issue["number"]))
        info = agent_info(comments or [])
        if info:
            self.data["agents"][key] = info
        else:
            self.data["agents"].pop(key, None)

    def sync_config(self):
        etag = self.data.get("config_etag")
        try:
            status, obj, hdrs = self.gh.rest("GET", "/repos/%s/%s/contents/.todo/config.json" % (self.owner, self.repo),
                                             headers={"If-None-Match": etag} if etag else {})
        except GitHubError as e:
            if getattr(e, "status", None) != 404:
                raise
            changed = self.data.get("config") is not None
            self.data.update(config=None, config_sha=None, config_etag=None)   # no config file: lists, no groups
            return changed
        if status == 304:
            return False
        self.data["config"] = json.loads(base64.b64decode(obj["content"]).decode())
        self.data["config_sha"] = obj["sha"]
        self.data["config_etag"] = hdrs.get("ETag") or hdrs.get("Etag")
        return True

    def pruned_since(self, since):
        """Issue numbers dropped as deleted on GitHub since `since` (with the usual 2-minute overlap)."""
        if not since:
            return []
        cut = iso_minus(since, 120)
        return sorted(int(k) for k, t in self.data.get("pruned", {}).items() if t >= cut)

    def snapshot(self, since=None):
        issues = list(self.data["issues"].values())
        if since:
            cut = iso_minus(since, 120)
            issues = [i for i in issues if i["updatedAt"] >= cut]
        return {"full": not since, "issues": [self.issue_out(i) for i in issues], "config": self.data["config"],
                "labels": sorted(self.label_map().values()), "synced_at": self.data["synced_at"],
                "count": len(self.data["issues"]), "version": VERSION, "repo": self.full_name}

    # -- queued edits already applied (so a retry after a lost reply is not applied twice)
    def _ops_path(self):
        return self.path.with_name("done-ops.json")

    def done_op(self, op):
        try:
            return json.loads(self._ops_path().read_text()).get(op, {}).get("reply")
        except FileNotFoundError:                       # no queued edit has been done yet
            return None

    def record_op(self, op, reply):
        with self.lock():
            try:
                ops = json.loads(self._ops_path().read_text())
            except FileNotFoundError:
                ops = {}
            ops[op] = {"t": time.time(), "reply": reply}
            if len(ops) > 400:
                ops = dict(sorted(ops.items(), key=lambda kv: kv[1]["t"])[-300:])
            tmp = self._ops_path().with_suffix(".tmp")
            tmp.write_text(json.dumps(ops))
            os.replace(tmp, self._ops_path())

    # -- labels
    def label_map(self, refresh=False):
        if self.labels is None or refresh:
            names, page = [], 1
            while True:
                got = self.gh.rest("GET", "/repos/%s/%s/labels?per_page=100&page=%d" % (self.owner, self.repo, page))
                names += [l["name"] for l in got]
                if len(got) < 100:
                    break
                page += 1
            self.labels = {n.lower(): n for n in names}
        return self.labels

    def ensure_labels(self, names):
        """Use GitHub's spelling of a label that exists in another case; create the rest."""
        known = self.label_map()
        out = []
        for n in names:
            if n.lower() in known:
                out.append(known[n.lower()])
                continue
            prefixed = next((c for p, c in NEW_LABEL_COLORS if n.lower().startswith(p)), "ededed")
            body = {"name": n, "color": ASSIGNED_COLOR if n == ASSIGNED_LABEL else prefixed}
            if n == ASSIGNED_LABEL:
                body["description"] = "An agent was started on this task from the to-do app"
            try:
                self.gh.rest("POST", "/repos/%s/%s/labels" % (self.owner, self.repo), body)
            except GitHubError as e:
                if getattr(e, "status", None) != 422:
                    raise
                known = self.label_map(refresh=True)        # 422: created meanwhile, or a name GitHub refuses
                if n.lower() not in known:
                    raise ValueError("GitHub won't accept the label %r" % n)
                out.append(known[n.lower()])
                continue
            known[n.lower()] = n
            out.append(n)
        return out

    # -- mutations
    def fresh(self, number):
        issue = from_rest(self.gh.rest("GET", "/repos/%s/%s/issues/%d" % (self.owner, self.repo, number)))
        return issue

    def store(self, issue):
        with self.lock():
            self.load()
            self.put(issue)
            self.refresh_agent(issue)
            self.save()
        return self.issue_out(issue)

    def create(self, title, body, labels):
        if not isinstance(title, str) or not title.strip():
            raise ValueError("a task needs a title")
        payload = {"title": title.strip(), "labels": self.ensure_labels([l for l in labels or [] if l])}
        if body:
            payload["body"] = body
        issue = from_rest(self.gh.rest("POST", "/repos/%s/%s/issues" % (self.owner, self.repo), payload))
        return self.store(issue)

    def update(self, number, set_=None, base=None, add=None, remove=None, force=False):
        set_, base = set_ or {}, base or {}
        cur = self.fresh(number)
        patch, conflicts = {}, []
        if "title" in set_ and set_["title"] != cur["title"]:
            if force or "title" not in base or cur["title"] == base["title"]:
                patch["title"] = set_["title"]
            else:
                conflicts.append("title")
        if "body" in set_ and (set_["body"] or "") != cur["body"]:
            if force or "body" not in base or cur["body"] == (base["body"] or ""):
                patch["body"] = set_["body"] or ""
            else:
                merged = merge_body(base["body"] or "", set_["body"] or "", cur["body"])
                if merged is None:
                    conflicts.append("body")
                elif merged.strip() not in (cur["body"].strip(), join_sections(split_sections(cur["body"])).strip()):
                    patch["body"] = merged                  # not when it differs from GitHub's only in blank lines
        if conflicts:
            raise Conflict(conflicts, self.store(cur))
        if set_.get("state") in ("OPEN", "CLOSED") and set_["state"] != cur["state"]:
            patch["state"] = set_["state"].lower()
            if set_["state"] == "CLOSED" and set_.get("stateReason") in CLOSE_REASONS:
                patch["state_reason"] = set_["stateReason"].lower()
        labels = apply_labels(cur["labels"], self.ensure_labels(add or []), remove or [])
        if sorted(labels) != sorted(cur["labels"]):
            patch["labels"] = labels
        if not patch:
            return self.store(cur)
        issue = from_rest(self.gh.rest("PATCH", "/repos/%s/%s/issues/%d" % (self.owner, self.repo, number), patch))
        return self.store(issue)

    # -- groups: a group is an issue, its tasks are GitHub sub-issues carrying the subtask label
    PARENT_QUERY = """
query($owner: String!, $repo: String!, $n: Int!) {
  repository(owner: $owner, name: $repo) { issue(number: $n) { id parent { number id } } }
}"""

    def _node(self, number):
        d = self.gh.graphql(self.PARENT_QUERY, {"owner": self.owner, "repo": self.repo, "n": number})
        issue = (d.get("repository") or {}).get("issue")
        if not issue:
            raise ValueError("no such task #%d" % number)
        return issue

    def set_parent(self, number, parent):
        """Put task `number` in group `parent` (None: out of any group). Safe to repeat."""
        if parent is not None and (not isinstance(parent, int) or parent <= 0 or parent == number):
            raise ValueError("a task can't be in group %r" % (parent,))
        child = self._node(number)
        current = (child.get("parent") or {}).get("number")
        if parent is None:
            if current is not None:
                self.gh.graphql("mutation($p: ID!, $c: ID!) { removeSubIssue(input: {issueId: $p, subIssueId: $c}) "
                                "{ issue { number } } }", {"p": child["parent"]["id"], "c": child["id"]})
            issue = self.update(number, remove=[SUBTASK_LABEL])
        else:
            if current != parent:
                group = self._node(parent)
                self.gh.graphql("mutation($p: ID!, $c: ID!) { addSubIssue(input: {issueId: $p, subIssueId: $c, "
                                "replaceParent: true}) { issue { number } } }", {"p": group["id"], "c": child["id"]})
                if current is not None:
                    # From one group to another: it has the label already, so re-adding it changes nothing and the
                    # other devices never see the move. Taking it off first marks the task updated.
                    self.update(number, remove=[SUBTASK_LABEL])
            issue = self.update(number, add=[SUBTASK_LABEL])
        with self.lock():
            self.load()
            cached = self.data["issues"].get(str(number))
            if cached is not None:
                cached["parent"] = parent
                self.save()
        return dict(issue, parent=parent)

    def comment(self, number, body):
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_TEXT:
            raise ValueError("a comment needs some text")
        got = self.gh.rest("POST", "/repos/%s/%s/issues/%d/comments" % (self.owner, self.repo, number), {"body": body})
        return (got or {}).get("html_url")

    def comments(self, number):
        got = self.gh.rest("GET", "/repos/%s/%s/issues/%d/comments?per_page=100" % (self.owner, self.repo, number)) or []
        return [{"author": (c.get("user") or {}).get("login"), "at": c.get("created_at"), "body": c.get("body") or "",
                 "url": c.get("html_url")} for c in got[-30:]]

    # -- the config file (.todo/config.json), changed by operation so concurrent edits from other clients survive
    def config_ops(self, ops):
        for attempt in range(3):
            try:
                obj = self.gh.rest("GET", "/repos/%s/%s/contents/.todo/config.json" % (self.owner, self.repo))
                cfg = json.loads(base64.b64decode(obj["content"]).decode())
            except GitHubError as e:
                if getattr(e, "status", None) != 404:
                    raise
                obj, cfg = {}, {"groups": [], "listMetadata": []}
            new = apply_config_ops(json.loads(json.dumps(cfg)), ops)
            if new == cfg:
                break
            content = base64.b64encode((json.dumps(new, indent=2) + "\n").encode()).decode()
            put = {"message": "Update config (Mac app)", "content": content}
            if obj.get("sha"):
                put["sha"] = obj["sha"]
            try:
                self.gh.rest("PUT", "/repos/%s/%s/contents/.todo/config.json" % (self.owner, self.repo), put)
                break
            except GitHubError as e:
                if getattr(e, "status", None) not in (409, 422) or attempt == 2:
                    raise
        with self.lock():
            self.load()
            self.data["config_etag"] = None
            self.sync_config()
            self.save()
        return self.data["config"]

    def delete_list(self, name):
        name = (name or "").strip().lower()
        if not name or name == "tasks":
            raise ValueError("the tasks list can't be deleted")
        self.sync()
        moved = []
        for issue in list(self.data["issues"].values()):
            if any(l.lower() == "list:" + name for l in issue["labels"]):
                remove = [l for l in issue["labels"] if l.lower().startswith(("list:", "group:"))]
                moved.append(self.update(issue["number"], add=["list:tasks"], remove=remove))
        cfg = self.config_ops([{"op": "remove_list", "list": name}])
        return moved, cfg

    # -- assign to an agent
    def assign_preview(self, number, text, to=None, source="mac"):
        issue = self.data["issues"].get(str(number)) or self.fresh(number)
        if to:
            return running_agent_message(issue, text, self.cfg, source)
        return orchestrator_message(issue, text, self.cfg, source)

    # -- the agent board's running agents (Assign to a running agent)
    def agents(self):
        return {"agents": agent_records(self.board(), read_board_groups()), "generated": now_iso()}

    def agent_mail(self, number):
        """The newest mail the task's agent sent the owner, for opening it in their Gmail."""
        info = self.data["agents"].get(str(number)) or {}
        name = info.get("name")
        mail = self.find_mail(name, agent_folder(info)) if name else None
        if mail:
            try:
                mail = dict(mail, thread=self.find_thread(mail["message_id"]))
            except ValueError as e:
                # The Mac still opens the mail, by a search for its Message-ID; the reply and the log say why
                print("agent-mail: %s; opening by search instead" % e, file=sys.stderr)
                mail = dict(mail, thread=None, thread_error=str(e))
        owners = mail_owners(self.cfg)
        return {"name": name, "mail": mail, "owner": owners[0] if owners else None}

    def running_agent(self, sid):
        """The board's record for a running agent, or ValueError naming it."""
        if not isinstance(sid, str) or not UUID_RE.match(sid):
            raise ValueError("not an agent's session id")
        snap = self.board()
        rec = next((r for r in agent_records(snap, read_board_groups()) if r["id"] == sid), None)
        if rec is None:
            known = next((a for a in (snap or {}).get("agents") or [] if a.get("id") == sid), None)
            name = (known or {}).get("display") or (known or {}).get("name") or "that agent (%s)" % sid[:8]
            raise ValueError("%s is no longer running" % name)
        return rec

    def publish_agents(self, force=False, now=None):
        """Write the running agents to .todo/agents.json for the web app, which can't reach the board.
        Only when something changed (see should_publish). Returns True if it wrote."""
        now = time.time() if now is None else now
        records = agent_records(self.board(), read_board_groups())
        path = STATE_DIR / "agents-published.json"
        try:
            last = json.loads(path.read_text())
        except (OSError, ValueError):
            last = None
        if last and (last.get("repo") or self.full_name).lower() != self.full_name.lower():
            last = None                                 # written for another tasks repo
        if not force and not should_publish(records, last, now):
            return False
        where = "/repos/%s/%s/contents/%s" % (self.owner, self.repo, AGENTS_PATH)
        try:
            sha = (self.gh.rest("GET", where) or {}).get("sha")
        except GitHubError as e:
            if getattr(e, "status", None) != 404:
                raise
            sha = None
        doc = {"v": 1, "updated": now_iso(), "agents": records}
        put = {"message": "Agents list (todo-desk)",
               "content": base64.b64encode((json.dumps(doc, indent=2) + "\n").encode()).decode()}
        if sha:
            put["sha"] = sha
        self.gh.rest("PUT", where, put)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": now, "repo": self.full_name, "ids": sorted(r["id"] for r in records),
                                    "key": [{k: v for k, v in r.items() if k != "active"} for r in records]}))
        return True

    def assign(self, number, text, source, again=False, to=None):
        """Start an agent on the task (or, with `to`, send it to a running one). `source` is a SOURCES key: where
        the owner asked, which the issue comment and the orchestrator's message say."""
        if not isinstance(number, int) or number <= 0:
            raise ValueError("no such task")
        text = text or ""
        if not isinstance(text, str) or len(text) > MAX_TEXT or "\x00" in text:
            raise ValueError("the instructions are too long or not text")
        if to:
            return self.assign_running(number, text, again, source, to)
        exe = find_orchestrate()
        if not exe:
            raise ValueError("agent-orchestrate is not installed on the desktop")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / "assign.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            cur = self.fresh(number)
            if cur["state"] != "OPEN":
                raise ValueError("#%d is completed; reopen it first" % number)
            if ASSIGNED_LABEL in cur["labels"] and not again:
                raise ValueError("#%d is already assigned to an agent; use Assign again to start another" % number)
            self.ensure_labels([ASSIGNED_LABEL])
            had_label = ASSIGNED_LABEL in cur["labels"]
            self.gh.rest("POST", "/repos/%s/%s/issues/%d/labels" % (self.owner, self.repo, number), {"labels": [ASSIGNED_LABEL]})

            def unlabel():
                if not had_label:
                    try:
                        self.gh.rest("DELETE", "/repos/%s/%s/issues/%d/labels/%s"
                                     % (self.owner, self.repo, number, urllib.parse.quote(ASSIGNED_LABEL, safe="")))
                    except GitHubError:
                        pass
            where = SOURCES[source][0]
            when = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
            said = "\n".join("> " + l for l in text.strip().split("\n")) if text.strip() else "No extra instructions."
            try:
                comment = self.gh.rest("POST", "/repos/%s/%s/issues/%d/comments" % (self.owner, self.repo, number), {
                    "body": "%s\nAssigned to an agent from %s at %s%s. The orchestrator starts it and "
                            "comments here when it is running.\n\n%s's instructions:\n%s"
                            % (MARK % "assigned", where, when, " (again)" if had_label else "",
                               (self.cfg.get("owner_name") or "The owner"), said)})
            except GitHubError:
                unlabel()                                   # nothing was launched: leave the task as it was
                raise
            message = orchestrator_message(cur, text, self.cfg, source)
            log = STATE_DIR / ("orchestrate-%d-%d.log" % (number, int(time.time())))
            err = None
            try:
                with open(log, "ab") as fh:
                    p = self.popen([exe, "--source", "todo"], stdin=subprocess.PIPE, stdout=fh, stderr=subprocess.STDOUT,
                                   start_new_session=True)
                    p.stdin.write(message.encode())
                    p.stdin.close()
                time.sleep(1.5)
                code = p.poll()
                if code not in (None, 0):
                    err = "agent-orchestrate exited %s at once; see %s" % (code, log)
                elif code is None:
                    threading.Thread(target=p.wait, daemon=True).start()   # reap it when it finishes
            except OSError as e:
                err = "could not start agent-orchestrate: %s" % e
            if err:
                self.gh.rest("POST", "/repos/%s/%s/issues/%d/comments" % (self.owner, self.repo, number),
                             {"body": "%s\nThe orchestrator did not start: %s" % (MARK % "failed", err)})
                unlabel()
                audit({"action": "assign", "number": number, "ok": False, "error": err})
                raise LaunchFailed(err)
            audit({"action": "assign", "number": number, "ok": True, "chars": len(text), "log": str(log),
                   "again": bool(again), "source": source})
            issue = self.store(self.fresh(number))
            return issue, comment.get("html_url"), str(log)

    def assign_running(self, number, text, again, source, to):
        """Send the task into an agent that is already running on the board, instead of starting one."""
        agent = self.running_agent(to)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / "assign.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            cur = self.fresh(number)
            if cur["state"] != "OPEN":
                raise ValueError("#%d is completed; reopen it first" % number)
            if ASSIGNED_LABEL in cur["labels"] and not again:
                raise ValueError("#%d is already assigned to an agent; use Assign again to start another" % number)
            self.ensure_labels([ASSIGNED_LABEL])
            had_label = ASSIGNED_LABEL in cur["labels"]
            self.gh.rest("POST", "/repos/%s/%s/issues/%d/labels" % (self.owner, self.repo, number), {"labels": [ASSIGNED_LABEL]})

            def unlabel():
                if not had_label:
                    try:
                        self.gh.rest("DELETE", "/repos/%s/%s/issues/%d/labels/%s"
                                     % (self.owner, self.repo, number, urllib.parse.quote(ASSIGNED_LABEL, safe="")))
                    except GitHubError:
                        pass
            where = SOURCES[source][0]
            when = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
            said = "\n".join("> " + l for l in text.strip().split("\n")) if text.strip() else NO_INSTRUCTIONS
            try:
                comment = self.gh.rest("POST", "/repos/%s/%s/issues/%d/comments" % (self.owner, self.repo, number), {
                    "body": "%s\nAssigned to the running agent %s (%s) from %s at %s.\n\n%s's instructions:\n%s"
                            % (MARK % "started", agent["name"], agent["folder"] or "-", where, when,
                               self.cfg.get("owner_name") or "The owner", said)})
            except GitHubError:
                unlabel()
                raise
            code, out = self.send(agent["cli"], agent["id"], running_agent_message(cur, text, self.cfg, source))
            if code != 0:
                err = "agent-send failed (%s): %s" % (code, out[-300:] or "no output")
                self.gh.rest("POST", "/repos/%s/%s/issues/%d/comments" % (self.owner, self.repo, number),
                             {"body": "%s\nThe task was not sent to %s: %s" % (MARK % "failed", agent["name"], err)})
                unlabel()
                audit({"action": "assign", "number": number, "ok": False, "to": agent["id"], "error": err})
                raise LaunchFailed(err)
            audit({"action": "assign", "number": number, "ok": True, "chars": len(text), "to": agent["id"],
                   "name": agent["name"], "again": bool(again), "source": source})
            issue = self.store(self.fresh(number))
            return issue, comment.get("html_url"), ""

    def dispatch(self, dry_run=False):
        """Turn each open `status:agent-requested` issue into an assign. Only a request comment written by the
        token's own account (the owner's) counts. The label comes off before the agent starts, so a request is
        dispatched once even if two runs overlap. Returns [(number, outcome)], skips included with their reason;
        [(None, why)] when another run holds the lock."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / "dispatch.lock", "w") as lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return [(None, "skipped: another dispatch run holds the lock")]
            found = self.gh.rest("GET", "/repos/%s/%s/issues?state=open&per_page=100&labels=%s"
                                 % (self.owner, self.repo, urllib.parse.quote(REQUESTED_LABEL, safe=""))) or []
            if not found:
                return []
            me = (self.gh.rest("GET", "/user") or {}).get("login", "").lower()
            out = []
            for raw in found:
                n = raw["number"]
                if raw.get("pull_request"):
                    out.append((n, "skipped: a pull request"))
                    continue
                labels = [l["name"] if isinstance(l, dict) else l for l in raw.get("labels", [])]
                got = self.gh.rest("GET", "/repos/%s/%s/issues/%d/comments?per_page=100" % (self.owner, self.repo, n)) or []
                asks = [c for c in got if REQUEST_RE.match(c.get("body") or "")
                        and ((c.get("user") or {}).get("login") or "").lower() == me]
                text = REQUEST_RE.sub("", asks[-1]["body"]).strip() if asks else None
                to = REQUEST_RE.match(asks[-1]["body"]).group("to") if asks else None
                if text == NO_INSTRUCTIONS:
                    text = ""
                if dry_run:
                    out.append((n, "would assign" if asks else "no request comment"))
                    continue
                try:
                    self.gh.rest("DELETE", "/repos/%s/%s/issues/%d/labels/%s"
                                 % (self.owner, self.repo, n, urllib.parse.quote(REQUESTED_LABEL, safe="")))
                except GitHubError as e:
                    if getattr(e, "status", None) == 404:   # someone else took it
                        out.append((n, "skipped: another run took the request"))
                        continue
                    raise
                if not asks:
                    self.gh.rest("POST", "/repos/%s/%s/issues/%d/comments" % (self.owner, self.repo, n), {
                        "body": "%s\nThe agent request had no request comment from %s, so nothing was started."
                                % (MARK % "failed", me or "the desktop's account")})
                    audit({"action": "dispatch", "number": n, "ok": False, "error": "no request comment"})
                    out.append((n, "no request comment"))
                    continue
                try:
                    self.assign(n, text[:MAX_TEXT], "app", again=ASSIGNED_LABEL in labels, to=to)
                    out.append((n, "sent to a running agent" if to else "assigned"))
                except LaunchFailed as e:                   # assign has already said why on the issue
                    audit({"action": "dispatch", "number": n, "ok": False, "error": str(e)})
                    out.append((n, "failed: %s" % e))
                except ValueError as e:
                    self.gh.rest("POST", "/repos/%s/%s/issues/%d/comments" % (self.owner, self.repo, n), {
                        "body": "%s\n%s: %s" % (MARK % "failed", "The task was not sent" if to
                                                 else "The agent was not started", e)})
                    audit({"action": "dispatch", "number": n, "ok": False, "error": str(e)})
                    out.append((n, "failed: %s" % e))
            return out


def apply_config_ops(cfg, ops):
    cfg.setdefault("groups", [])
    cfg.setdefault("listMetadata", [])
    metas = cfg["listMetadata"]
    find = lambda name: [m for m in metas if (m.get("listName") or "").lower() == name.lower()]   # noqa: E731
    for op in ops:
        kind = op.get("op")
        name = (op.get("list") or "").strip().lower()
        group = op.get("group")
        if kind == "add_list" and name:
            if not find(name):
                entry = {"listName": name, "sortOrder": 0}
                if group:
                    entry["groupName"] = group
                metas.append(entry)
        elif kind == "remove_list" and name:
            cfg["listMetadata"] = metas = [m for m in metas if (m.get("listName") or "").lower() != name]
        elif kind == "set_group" and name:
            found = find(name)
            if not found:
                found = [{"listName": name, "sortOrder": 0}]
                metas.append(found[0])
            for m in found:
                if group:
                    m["groupName"] = group
                else:
                    m.pop("groupName", None)
        elif kind == "add_group" and group:
            if not any(g.get("name") == group for g in cfg["groups"]):
                top = max([g.get("sortOrder", 0) for g in cfg["groups"]] + [0])
                cfg["groups"].append({"name": group, "sortOrder": top + 1})
        elif kind == "set_sort" and type(op.get("order")) is int and (name or group):   # not a bool
            if name:                                           # a list's place (in its group, or at the top level)
                found = find(name) or [metas.append({"listName": name, "sortOrder": 0}) or metas[-1]]
                for m in found:
                    m["sortOrder"] = op["order"]
            else:                                              # a group's place at the top level
                for g in cfg["groups"]:
                    if g.get("name") == group:
                        g["sortOrder"] = op["order"]
        elif kind == "remove_group" and group:
            cfg["groups"] = [g for g in cfg["groups"] if g.get("name") != group]
            for m in metas:
                if m.get("groupName") == group:
                    m.pop("groupName", None)
        else:
            raise ValueError("unknown config change %r" % (op,))
    return cfg


# ------------------------------------------------------------------ serve
def _proc_tcp_addr(ip, port):
    """How /proc/net/tcp{,6} writes an address: each 32-bit word of the address in host (little-endian) order."""
    b = ip.packed
    return "".join("%08X" % int.from_bytes(b[i:i + 4], "little") for i in range(0, len(b), 4)) + ":%04X" % port


def ssh_peer_gone(conn=None, tables=("/proc/net/tcp", "/proc/net/tcp6"), timeouts=GONE_TIMEOUTS):
    """Has the Mac this process serves over SSH (SSH_CONNECTION) stopped acknowledging data? A Mac that sleeps or
    leaves the network never closes its connection, and with the kernel's 15 retries serve would keep polling
    GitHub for ~16 minutes. Here: data still in flight and `timeouts` retransmissions in a row timed out (an awake
    Mac acknowledges within a round trip). False when unsure. The agent board's stream does the same
    (agentboard/sources.py ssh_peer_gone)."""
    parts = (os.environ.get("SSH_CONNECTION", "") if conn is None else conn).split()
    try:
        cip, cport, sip, sport = ipaddress.ip_address(parts[0]), int(parts[1]), ipaddress.ip_address(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        return False
    pairs = [(_proc_tcp_addr(sip, sport), _proc_tcp_addr(cip, cport))]
    if cip.version == 4 and sip.version == 4:     # an IPv4 client on an IPv6 listener: ::ffff:a.b.c.d
        pairs.append((_proc_tcp_addr(ipaddress.ip_address("::ffff:" + str(sip)), sport),
                      _proc_tcp_addr(ipaddress.ip_address("::ffff:" + str(cip)), cport)))
    for table in tables:
        try:
            with open(table) as fh:
                for line in fh:
                    f = line.split()
                    if len(f) > 6 and (f[1], f[2]) in pairs and f[3] == "01":       # 01: established
                        return int(f[4].split(":")[0], 16) > 0 and int(f[6], 16) >= timeouts
        except (OSError, ValueError):
            continue
    return False


class Server:
    def __init__(self, desk, out=sys.stdout):
        self.desk, self.out = desk, out
        self.q = queue.Queue()
        self.wlock = threading.Lock()
        self.writing_since = None
        self.started = time.time()
        self.stop = threading.Event()
        self.busy = False

    def emit(self, obj):
        line = json.dumps(obj, separators=(",", ":"), default=str) + "\n"
        with self.wlock:
            self.writing_since = time.time()
            try:
                self.out.write(line)
                self.out.flush()
            except (BrokenPipeError, OSError):
                self.stop.set()
            self.writing_since = None

    def handle(self, req):
        """Queued edits carry the Mac's op id: one that already happened (its reply was lost) is not done twice."""
        op = req.get("op") if req.get("cmd") in ("create", "update", "config", "delete_list", "set_parent") else None
        if isinstance(op, str) and op:
            done = self.desk.done_op(op)
            if done is not None:
                return done
            res = self.dispatch(req)
            self.desk.record_op(op, res)
            return res
        return self.dispatch(req)

    def dispatch(self, req):
        cmd = req.get("cmd")
        d = self.desk
        if cmd == "hello":
            changed, removed, _ = d.sync()
            snap = d.snapshot(req.get("since"))
            # Also what a full sync dropped while this client was away
            return dict(snap, removed=sorted(set(removed) | set(d.pruned_since(req.get("since")))),
                        known=sorted(int(k) for k in d.data["issues"]))
        if cmd == "refresh":
            changed, removed, cfg = d.sync(full=bool(req.get("full")))
            if changed or removed:
                self.emit({"type": "issues", "issues": changed, "removed": removed, "synced_at": d.data["synced_at"]})
            if cfg:
                self.emit({"type": "config", "config": d.data["config"]})
            return {"changed": len(changed), "removed": len(removed), "synced_at": d.data["synced_at"]}
        if cmd == "create":
            return {"issue": d.create(req.get("title"), req.get("body"), req.get("labels") or [])}
        if cmd == "update":
            return {"issue": d.update(int(req["number"]), req.get("set"), req.get("base"), req.get("add"),
                                      req.get("remove"), bool(req.get("force")))}
        if cmd == "config":
            return {"config": d.config_ops(req.get("ops") or [])}
        if cmd == "delete_list":
            moved, cfg = d.delete_list(req.get("list"))
            return {"issues": moved, "config": cfg}
        if cmd == "comments":
            return {"comments": d.comments(int(req["number"]))}
        if cmd == "set_parent":
            parent = req.get("parent")
            return {"issue": d.set_parent(int(req["number"]), None if parent is None else int(parent))}
        if cmd == "comment":
            return {"url": d.comment(int(req["number"]), req.get("body"))}
        if cmd == "assign_preview":
            return {"message": d.assign_preview(int(req["number"]), req.get("text") or "", req.get("to") or None)}
        if cmd == "assign":
            issue, url, log = d.assign(int(req["number"]), req.get("text") or "", "mac", bool(req.get("again")),
                                       to=req.get("to") or None)
            return {"issue": issue, "comment": url, "log": log}
        if cmd == "agents":
            return d.agents()
        if cmd == "agent_mail":
            return d.agent_mail(int(req["number"]))
        if cmd == "ping":
            return {"pong": True}
        raise ValueError("unknown command %r" % (cmd,))

    def worker(self):
        while not self.stop.is_set():
            try:
                req = self.q.get(timeout=1)
            except queue.Empty:
                continue
            rid = req.get("id")
            self.busy = True
            try:
                res = self.handle(req)
                if rid is not None:
                    self.emit(dict(res, id=rid, ok=True))
            except Conflict as c:
                self.emit({"id": rid, "ok": False, "conflict": True, "fields": c.fields, "issue": c.issue,
                           "error": str(c)})
            except (GitHubError, ValueError, KeyError, TypeError) as e:
                if rid is not None:
                    self.emit({"id": rid, "ok": False, "error": str(e) or type(e).__name__})
                else:
                    self.emit({"type": "error", "error": str(e)})
            except Exception as e:                       # noqa: BLE001 - report, never die silently
                msg = "%s: %s" % (type(e).__name__, e)
                self.emit({"id": rid, "ok": False, "error": msg} if rid is not None else {"type": "error", "error": msg})
            finally:
                self.busy = False

    def ticker(self, gone=ssh_peer_gone):
        last_poll, last_ping, last_peer = time.time(), 0, 0
        while not self.stop.is_set():
            time.sleep(1)
            t = time.time()
            if self.writing_since and t - self.writing_since > STALL_SECONDS:
                os._exit(3)                              # the Mac went away mid-write
            if t - self.started > MAX_SECONDS and self.q.empty() and not self.busy:
                self.emit({"type": "bye", "error": "todo-desk restarts after %d h" % (MAX_SECONDS // 3600)})
                os._exit(0)                               # EOF for the Mac, which reconnects; nothing half-done
            if t - last_peer >= 5:
                last_peer = t
                if self.q.empty() and not self.busy and gone():
                    os._exit(0)                           # the Mac slept or left; it reconnects when it wakes
            if t - last_ping >= PING_SECONDS:
                last_ping = t
                self.emit({"type": "ping", "t": t, "synced_at": self.desk.data.get("synced_at")})
            if t - last_poll >= POLL_SECONDS and self.q.empty():
                last_poll = t
                self.q.put({"cmd": "refresh"})

    def run(self, inp=sys.stdin):
        self.emit({"type": "ready", "version": VERSION, "synced_at": self.desk.data.get("synced_at")})
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.ticker, daemon=True).start()
        while not self.stop.is_set():
            line = inp.readline()
            if not line:
                break                                    # EOF: the Mac closed the connection
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                self.emit({"type": "error", "error": "not JSON"})
                continue
            if isinstance(req, dict):
                self.q.put(req)
        self.stop.set()
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="todo-desk", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    p = sub.add_parser("snapshot")
    p.add_argument("--full", action="store_true")
    p.add_argument("--pretty", action="store_true")
    p = sub.add_parser("message")
    p.add_argument("number", type=int)
    p.add_argument("--text", default="")
    p = sub.add_parser("assign")
    p.add_argument("number", type=int)
    p.add_argument("--text", default="")
    p.add_argument("--again", action="store_true")
    p.add_argument("--to", help="a running agent's session id: send it the task instead of starting an agent")
    p = sub.add_parser("dispatch")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("agents")
    p.add_argument("--pretty", action="store_true")
    p = sub.add_parser("agent-mail")
    p.add_argument("number", type=int)
    p = sub.add_parser("publish-agents")
    p.add_argument("--force", action="store_true", help="write even if nothing changed")
    sub.add_parser("config")
    a = ap.parse_args(argv)
    if a.cmd == "config":
        cfg = load_config()
        print(json.dumps(dict(cfg, config_file=str(CONFIG_DIR / "config.json"),
                              mail_config_file=str(MAIL_CONFIG_DIR / "config.json")), indent=2))
        try:
            repo_parts(cfg["repo"])
        except ConfigError as e:
            print("todo-desk: %s" % e, file=sys.stderr)
            return 2
        return 0
    try:
        desk = Desk()
    except (ConfigError, GitHubError) as e:
        if a.cmd == "serve":
            print(json.dumps({"type": "fatal", "error": str(e)}), flush=True)
        else:
            print("todo-desk: %s" % e, file=sys.stderr)
        return 2
    if a.cmd == "serve":
        return Server(desk).run()
    if a.cmd == "snapshot":
        desk.sync(full=a.full)
        snap = desk.snapshot()
        print(json.dumps(snap, indent=2 if a.pretty else None))
        return 0
    if a.cmd == "message":
        desk.sync()
        print(desk.assign_preview(a.number, a.text))
        return 0
    if a.cmd == "assign":
        issue, url, log = desk.assign(a.number, a.text, "desktop", a.again, to=a.to)
        print("assigned #%d: %s%s" % (a.number, url, " (log %s)" % log if log else ""))
        return 0
    if a.cmd == "dispatch":
        for n, outcome in desk.dispatch(dry_run=a.dry_run):
            print("%s %s%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), "" if n is None else "#%d: " % n, outcome))
        if not a.dry_run:
            try:                                          # the web app's list of agents; never fails dispatch
                if desk.publish_agents():
                    print("%s published the agents list" % time.strftime("%Y-%m-%d %H:%M:%S"))
            except Exception as e:                        # noqa: BLE001
                print("%s agents list not published: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), e))
        return 0
    if a.cmd == "agents":
        print(json.dumps(desk.agents(), indent=2 if a.pretty else None))
        return 0
    if a.cmd == "agent-mail":
        print(json.dumps(desk.agent_mail(a.number), indent=2))
        return 0
    if a.cmd == "publish-agents":
        print("published" if desk.publish_agents(force=a.force) else "unchanged; nothing written")
        return 0
    return 1
