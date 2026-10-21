"""Cheap, read-only readers for everything the board shows. Nothing here writes to another tool's state.

  Claude sessions  ~/.claude/sessions/<pid>.json        live = pid alive, same process start, socket present
  Codex sessions   ~/.codex/state_5.sqlite (threads)     live = its writer lock is actually HELD (/proc/locks);
                   ~/.codex/thread-writer-locks/*.lock    a lock file alone outlives its process
  Status log       ~/.local/state/agent-status/events.jsonl, tailed incrementally
  Mailroom store   ~/.local/state/agent-mail/mailroom.sqlite, opened read-only
  Metrics          /proc/meminfo, /proc/stat, statvfs, one long-lived `nvidia-smi -l`
  Usage            `agent-usage --json` when installed, else its cache file (read-only stand-in)
"""
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from . import config

HOME = Path.home()
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude"))
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
STATUS_LOG = Path(os.environ.get("AGENT_STATUS_DIR", HOME / ".local/state/agent-status")) / "events.jsonl"
MAIL_STATE = config.MAIL_STATE
MAIL_DB = MAIL_STATE / "mailroom.sqlite"
MAIL_CONFIG = config.MAIL_CONFIG
USAGE_CACHE = MAIL_STATE / "usage-cache.json"
BIN_DIRS = [HOME / ".local/bin", Path("/usr/local/bin"), Path("/usr/bin")]


def find_bin(name):
    """PATH first, then the usual dirs: a non-login SSH shell lacks ~/.local/bin."""
    found = shutil.which(name)
    if found:
        return found
    for d in BIN_DIRS:
        p = d / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def ro_sqlite(path):
    """Read-only connection that never checkpoints or writes; falls back to query_only if ro open fails."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2, check_same_thread=False)
        con.execute("select 1 from sqlite_master limit 1")
    except sqlite3.Error:
        con = sqlite3.connect(str(path), timeout=2, check_same_thread=False)
        con.execute("pragma query_only=1")
    con.row_factory = sqlite3.Row
    return con


# ------------------------------------------------------------------ processes
def proc_start(pid):
    try:
        with open("/proc/%d/stat" % int(pid)) as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def process_started_at(pid):
    """Epoch seconds when a live process started, or None. A resumed Codex thread keeps its creation time, so
    this is the only way to tell that its process began after something."""
    ticks = proc_start(pid) if pid else None
    try:
        with open("/proc/stat") as fh:
            btime = next(float(line.split()[1]) for line in fh if line.startswith("btime "))
        return btime + int(ticks) / os.sysconf("SC_CLK_TCK") if ticks else None
    except (OSError, ValueError, StopIteration):
        return None


def pid_alive(pid, start=None):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except (ValueError, OSError):
        return False
    if start and os.path.isdir("/proc"):   # guard against pid reuse
        actual = proc_start(pid)
        return actual is None or str(actual) == str(start)
    return True


def ppid(pid):
    try:
        with open("/proc/%d/stat" % int(pid)) as fh:
            return int(fh.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def tool_command(cmd):
    """What a command really runs: Claude's Bash tool wraps it as `bash -c source <snapshot> ... && eval '<cmd>'`,
    with `[< /dev/null] && pwd -P >| ...` after it (older versions put nothing after a background command)."""
    i = cmd.find("&& eval '")
    if i < 0:
        return cmd
    rest = cmd[i + len("&& eval '"):]
    j = max(rest.rfind("' && pwd -P"), rest.rfind("' < /dev/null && pwd -P"))
    rest = rest[:j] if j >= 0 else (rest[:-1] if rest.endswith("'") else rest)
    return rest.replace("'\"'\"'", "'")


# ------------------------------------------------------------------ background waits that can never end
TOOL_SHELL = re.compile(r"^(\S*/)?(ba|z)?sh -c source \S*shell-snapshots/snapshot-")
WAIT_LOOP = re.compile(r"\b(?:until\s+!\s*|while\s+)(?=pgrep\s)")
PGREP_VALUE = set("dgGPstuUFrO")          # pgrep's short options that take a value
PGREP_LONG_VALUE = {"--delimiter", "--pgroup", "--group", "--parent", "--session", "--terminal", "--euid", "--uid",
                    "--pidfile", "--ns", "--nslist", "--signal", "--older", "--runstates", "--env", "--cgroup"}


def pgrep_wait(cmd):
    """(pattern, exact, ignore_case) when `cmd` loops until `pgrep -f PATTERN` finds nothing (`until ! pgrep -f P`
    or `while pgrep -f P`), else None. A pattern built from a variable or command is unknowable: None."""
    m = WAIT_LOOP.search(cmd)
    if not m:
        return None
    lex = shlex.shlex(cmd[m.end():], posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    flags, skip = "", False
    try:
        toks = iter(lex)
        next(toks)                                    # pgrep
        for t in toks:
            if skip:
                skip = False
            elif t[:1] in tuple(";|&<>()"):
                return None
            elif t.startswith("--"):
                flags += {"--full": "f", "--exact": "x", "--ignore-case": "i", "--inverse": "v"}.get(t, "")
                skip = t in PGREP_LONG_VALUE
            elif t.startswith("-") and len(t) > 1:
                k = next((k for k, ch in enumerate(t[1:]) if ch in PGREP_VALUE), None)
                flags += t[1:] if k is None else t[1:k + 1]
                skip = k == len(t) - 2
            else:
                if "f" not in flags or "v" in flags or re.search(r"\$[\w{(]|`", t):
                    return None
                return t, "x" in flags, "i" in flags
    except (ValueError, StopIteration):
        return None
    return None


def proc_table(proc="/proc"):
    """{pid: (ppid, argv)} for every process whose stat and cmdline can be read."""
    out = {}
    try:
        pids = [int(p) for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return out
    for p in pids:
        try:
            with open("%s/%d/stat" % (proc, p)) as fh:
                pp = int(fh.read().rsplit(")", 1)[1].split()[1])
            with open("%s/%d/cmdline" % (proc, p), "rb") as fh:
                argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\x00") if a]
        except (OSError, ValueError, IndexError):
            continue
        out[p] = (pp, argv)
    return out


def stuck_waits(pids, procs=None):
    """{agent pid: {"shells": n, "stuck": [{"pid", "pattern", "target"}]}} for Claude agents with background shells.

    Claude's Bash tool runs a command as `bash -c '... <command>'`, and `pgrep -f` reads whole command lines, so a
    loop like `until ! pgrep -f "stage x"; do sleep 15; done` always finds its own shell and never ends (pgrep leaves
    out only itself). "stuck" lists those loops; "target" says whether anything besides such loops (and pgrep) still
    matches, i.e. whether what it meant to wait for is still running."""
    procs = proc_table() if procs is None else procs
    want = {int(p) for p in pids if p}
    joined = {q: " ".join(argv) for q, (_, argv) in procs.items()}
    out = {}
    for q, (pp, _) in procs.items():
        if pp in want and TOOL_SHELL.match(joined[q]):
            out.setdefault(pp, {"shells": 0, "stuck": []})["shells"] += 1
            w = pgrep_wait(tool_command(joined[q]))
            if not w:
                continue
            pattern, exact, icase = w
            try:
                rx = re.compile(pattern, re.I if icase else 0)
            except re.error:
                continue
            hit = rx.fullmatch if exact else rx.search
            if not hit(joined[q]):
                continue                     # it doesn't find itself, so it ends when what it waits for does
            target = any(hit(text) for r, text in joined.items() if r != q and text
                         and os.path.basename(procs[r][1][0]) != "pgrep"
                         and not (TOOL_SHELL.match(text) and pgrep_wait(tool_command(text)) == w))
            out[pp]["stuck"].append({"pid": q, "pattern": pattern, "target": target})
    return out


def held_locks():
    """{"MAJ:MIN:INODE": pid} for every lock the kernel holds (Linux), or None where unknowable."""
    try:
        text = Path("/proc/locks").read_text()
    except OSError:
        return None
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if "->" in parts:          # a waiter, not a holder
            continue
        for i, p in enumerate(parts):
            if p.count(":") == 2 and i >= 4:
                try:
                    out[p] = int(parts[i - 1])
                except ValueError:
                    pass
                break
    return out


def lock_key(st):
    return "%02x:%02x:%d" % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)


# ------------------------------------------------------------------ session registries
DEFAULT_CLAUDE_HOME = HOME / ".claude"    # the mailroom's "config_dir: null", whatever CLAUDE_CONFIG_DIR says


ROUTING_MIN_REMAINING = {"session": 10, "weekly": 5}   # the mailroom's usage.min_remaining defaults


def claude_routing(path=MAIL_CONFIG):
    """(Claude login names in the mailroom's usage.order, min_remaining) with the mailroom's defaults: without an
    order, the configured logins in config order (default: the one login named "default")."""
    try:
        u = json.loads(Path(path).read_text()).get("usage") or {}
    except (OSError, ValueError, AttributeError):
        u = {}
    accts = list((u.get("claude_accounts") or {"default": None}).keys())
    order = [t[7:] for t in (u.get("order") or ["claude:" + n for n in accts]) if isinstance(t, str)
             and t.startswith("claude:") and t[7:] in accts]
    return order, dict(ROUTING_MIN_REMAINING, **(u.get("min_remaining") or {}))


def claude_logins(path=MAIL_CONFIG):
    """[(account name, config dir)] for every Claude login in the mailroom's usage config; the default
    login (config_dir null) is always ~/.claude, even when this process runs under another login's
    CLAUDE_CONFIG_DIR. Without config, just the default."""
    try:
        accts = (json.loads(Path(path).read_text()).get("usage") or {}).get("claude_accounts") or {}
    except (OSError, ValueError, AttributeError):
        accts = {}
    out, seen = [], set()
    for name, a in accts.items():
        d = Path(os.path.expanduser(a.get("config_dir"))) if isinstance(a, dict) and a.get("config_dir") else DEFAULT_CLAUDE_HOME
        if d.resolve() not in seen:
            seen.add(d.resolve())
            out.append((name, d))
    if DEFAULT_CLAUDE_HOME.resolve() not in seen:
        out.insert(0, ("default", DEFAULT_CLAUDE_HOME))
    return out


def claude_sessions_all(logins=None):
    out = []
    for account, home in (logins or claude_logins()):
        for s in claude_sessions(home):
            s["account"] = account
            s["account_default"] = Path(home).resolve() == DEFAULT_CLAUDE_HOME.resolve()
            s["config_dir"] = str(home)     # `claude attach` for a --bg session must run under its own login
            out.append(s)
    return out


def claude_sessions(home=CLAUDE_HOME):
    out = []
    for f in sorted((Path(home) / "sessions").glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        sock = d.get("messagingSocketPath")
        live = pid_alive(d.get("pid"), d.get("procStart")) and bool(sock) and os.path.exists(sock)
        out.append({
            "cli": "claude", "id": d.get("sessionId") or "", "name": d.get("name") or "",
            "cwd": d.get("cwd") or "", "raw_status": d.get("status") or "", "kind": d.get("kind") or "",
            "live": live, "pid": d.get("pid"), "tmux": d.get("tmux") or "",
            "waiting_for": d.get("waitingFor") or "",
            "started_at": (d.get("startedAt") or 0) / 1000.0,
            "status_at": (d.get("statusUpdatedAt") or d.get("updatedAt") or 0) / 1000.0,
            "version": d.get("version") or "", "entrypoint": d.get("entrypoint") or "",
        })
    return out


class CodexReader:
    """Codex threads with real liveness. Busy = its rollout file was written in the last BUSY_SECONDS."""
    BUSY_SECONDS = 45

    def __init__(self, home=CODEX_HOME):
        self.home = Path(home)
        self.con = None

    def _db(self):
        if self.con is None:
            db = self.home / "state_5.sqlite"
            if not db.exists():
                return None
            self.con = ro_sqlite(db)
        return self.con

    def sessions(self, ids_of_interest=(), recent_seconds=86400, now=None):
        now = now or time.time()
        lock_dir = self.home / "thread-writer-locks"
        locks = {}
        if lock_dir.is_dir():
            for p in lock_dir.glob("*.lock"):
                if not p.name.startswith("."):
                    try:
                        locks[p.stem] = p.stat()
                    except OSError:
                        pass
        held = held_locks()
        live = {}
        for tid, st in locks.items():
            if held is None:
                live[tid] = None            # cannot tell: trust the file
            elif lock_key(st) in held:
                live[tid] = held[lock_key(st)]
        con = self._db()
        rows = {}
        if con is not None:
            want = set(live) | set(ids_of_interest)
            ph = ",".join("?" * len(want))
            where = "where archived = 0 and (updated_at >= ? %s)" % ("or id in (%s)" % ph if want else "")
            base = "id, name, title, cwd, git_branch, rollout_path, updated_at, model, reasoning_effort, created_at"
            for cols in (base + ", source, thread_source, agent_nickname", base):   # older Codex: no subagent columns
                try:
                    for r in con.execute("select %s from threads %s" % (cols, where), [int(now - recent_seconds)] + list(want)):
                        rows[r["id"]] = dict(r)
                    break
                except sqlite3.OperationalError as e:
                    if "no such column" in str(e) and cols != base:
                        continue                 # an older Codex: retry without the subagent columns
                    self.con = None              # locked / I/O / stale handle: reopen next poll
                    break
                except sqlite3.Error:
                    self.con = None
                    break
        out = []
        for tid, r in rows.items():
            is_live = tid in live
            busy, activity = False, 0.0
            rp = r.get("rollout_path")
            if is_live and rp:
                try:
                    activity = os.stat(rp).st_mtime
                    busy = now - activity < self.BUSY_SECONDS
                except OSError:
                    pass
            out.append({
                "cli": "codex", "id": tid, "name": r.get("name") or "", "title": r.get("title") or "",
                "cwd": r.get("cwd") or "", "raw_status": ("busy" if busy else "idle") if is_live else "",
                "kind": "interactive", "live": is_live, "pid": live.get(tid), "tmux": "",
                "waiting_for": "", "started_at": float(r.get("created_at") or 0),
                "status_at": max(float(r.get("updated_at") or 0), activity),
                "model": r.get("model") or "", "branch_hint": r.get("git_branch") or "",
                "parent": subagent_parent(r.get("source"), r.get("thread_source")),
                "is_subagent": r.get("thread_source") == "subagent" or "subagent" in (r.get("source") or ""),
                "nickname": r.get("agent_nickname") or "", "agent_path": _agent_path(r.get("source")),
                "activity_at": activity,
            })
        # a subagent without a recorded parent runs in its parent's process: the same pid holds both locks
        by_pid = {}
        for t in out:
            if t["live"] and t["pid"] and not t["is_subagent"]:
                by_pid.setdefault(t["pid"], []).append(t["id"])
        for t in out:
            if t["is_subagent"] and not t["parent"]:
                owners = by_pid.get(t["pid"]) if t["live"] and t["pid"] else None
                t["parent"] = owners[0] if owners and len(owners) == 1 else ""
            if not t["is_subagent"]:
                t["parent"] = ""
        return out


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], str):
            return obj[key]
        for v in obj.values():
            hit = _find_key(v, key)
            if hit:
                return hit
    return ""


def subagent_parent(source, thread_source):
    """A Codex subagent thread (spawned by another thread, sharing its process) -> its parent thread id,
    wherever the source JSON records it; "" if it is not a subagent or no parent is recorded."""
    if thread_source != "subagent" and "subagent" not in (source or ""):
        return ""
    try:
        return _find_key(json.loads(source), "parent_thread_id")
    except (ValueError, TypeError):
        return ""


def _agent_path(source):
    try:
        return _find_key(json.loads(source or ""), "agent_path")
    except (ValueError, TypeError):
        return ""


def tmux_sessions():
    """{session name: set of its panes' current directories}, or {} without tmux."""
    tmux = find_bin("tmux")
    if not tmux:
        return {}
    try:
        r = subprocess.run([tmux, "list-panes", "-a", "-F", "#{session_name}\t#{pane_current_path}"],
                           capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in r.stdout.splitlines():
        name, _, path = line.partition("\t")
        if name:
            out.setdefault(name, set()).add(path)
    return out


def tmux_panes():
    """{pane_pid: session_name} for every tmux pane, or {} without tmux."""
    tmux = find_bin("tmux")
    if not tmux:
        return {}
    try:
        r = subprocess.run([tmux, "list-panes", "-a", "-F", "#{pane_pid} #{session_name}"],
                           capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in r.stdout.splitlines():
        pid, _, name = line.partition(" ")
        if pid.isdigit():
            out[int(pid)] = name
    return out


def tmux_session_for(pid, panes, depth=12):
    while pid and depth:
        if pid in panes:
            return panes[pid]
        pid = ppid(pid)
        depth -= 1
    return ""


# ------------------------------------------------------------------ status log
class EventLog:
    """Incremental tail of the agent-status log; keeps the last `keep_seconds` of events in memory."""

    def __init__(self, path=STATUS_LOG, keep_seconds=48 * 3600):
        self.path = Path(path)
        self.keep = keep_seconds
        self.events = []
        self.ino = None
        self.pos = 0

    def _read_from(self, path, pos):
        out = []
        try:
            with open(path, "rb") as fh:
                fh.seek(pos)
                data = fh.read()
        except OSError:
            return out, pos
        end = data.rfind(b"\n") + 1      # leave a half-written last line for next time
        for line in data[:end].splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
        return out, pos + end

    def poll(self, now=None):
        now = now or time.time()
        try:
            st = self.path.stat()
        except OSError:
            return self.events
        new = []
        if self.ino is None:                                          # first read
            new, self.pos = self._read_from(self.path, 0)
        elif st.st_ino != self.ino or st.st_size < self.pos:          # rotated to events.jsonl.1
            old = self.path.with_suffix(".jsonl.1")
            if self._same_file(old):
                new, _ = self._read_from(old, self.pos)
            more, self.pos = self._read_from(self.path, 0)
            new.extend(more)
        elif st.st_size > self.pos:
            new, self.pos = self._read_from(self.path, self.pos)
        self.ino = st.st_ino
        cutoff = now - self.keep
        self.events.extend(e for e in new if (e.get("ts") or 0) >= cutoff)
        if self.events and (self.events[0].get("ts") or 0) < cutoff:
            self.events = [e for e in self.events if (e.get("ts") or 0) >= cutoff]
        return self.events

    def _same_file(self, path):
        try:
            return path.stat().st_ino == self.ino
        except OSError:
            return False


# ------------------------------------------------------------------ mailroom store
def owner_addresses(path=MAIL_CONFIG):
    """The owner's addresses (mailroom config owner_email, then allowlist), lower case; [] when unconfigured."""
    return config.owner_addresses(config.mail_config(path))


def mail_account(path=MAIL_CONFIG):
    """The agents' Gmail address (mailroom config "account"), or ""."""
    try:
        return (json.loads(Path(path).read_text()).get("account") or "").lower()
    except (OSError, ValueError, AttributeError):
        return ""


class MailStore:
    """Read-only view of the mailroom's routing map. Never opened for writing."""

    def __init__(self, path=MAIL_DB, owners=None, host_id=None):
        self.path = Path(path)
        self.owners = owners
        # this machine's identity in mail routing (mailroom config host_id); sessions hosted elsewhere are skipped
        self.host_id = host_id or config.mail_config().get("host_id") or "desktop"
        self.con = None

    def _db(self):
        if self.con is None and self.path.exists():
            self.con = ro_sqlite(self.path)
        return self.con

    def read(self, now=None, window=7 * 86400):
        """{"by_session": {(cli, sid): {"sent": [...], "inbound": [...], "reviews": [...]?}}, "held": [...]}"""
        now = now or time.time()
        owners = self.owners if self.owners is not None else owner_addresses()
        con = self._db()
        out = {"by_session": {}, "held": [], "ok": con is not None}
        if con is None:
            return out
        since = int(now - window)
        try:
            tags = {}
            for r in con.execute("select tag, cli, session_id, session_name, origin_host from sessions"):
                if (r["origin_host"] or self.host_id) == self.host_id:     # no origin: an older, local row
                    tags[r["tag"]] = (r["cli"], r["session_id"])
            names = {}
            for r in con.execute("select session_id, session_name from sessions where origin_host = ?", (self.host_id,)):
                if r["session_name"]:
                    names[r["session_name"]] = r["session_id"]
            for r in con.execute("select tag, thread_id, to_addr, subject, sent_at, message_id from sent where sent_at >= ?", (since,)):
                key = tags.get(r["tag"])
                if not key or not _addressed_to(r["to_addr"], owners):
                    continue
                out["by_session"].setdefault(key, {"sent": [], "inbound": []})["sent"].append(dict(r))
            for r in con.execute(
                    "select id, thread_id, from_addr, subject, received_at, matched_tag, match_rule, action, detail, "
                    "released_to from inbound where received_at >= ?", (since,)):
                d = dict(r)
                if d["action"] == "held":
                    out["held"].append({"id": d["id"], "ts": d["received_at"], "from": d["from_addr"] or "",
                                        "subject": d["subject"] or "", "reason": d["detail"] or "",
                                        "owner": (d["from_addr"] or "").lower() in owners})
                if (d["from_addr"] or "").lower() not in owners:
                    continue
                key = tags.get(d["matched_tag"])
                rel = d.get("released_to") or ""
                if not key and rel:
                    sid = names.get(rel, rel)
                    key = next((k for k in tags.values() if k[1] == sid), None)
                if key:
                    out["by_session"].setdefault(key, {"sent": [], "inbound": []})["inbound"].append(d)
            try:     # each tag's newest pre-send review per audience (the mailroom holds a draft per audience:
                     # owner, collaborator, other; null is owner); a mailroom without the review has no table
                for r in con.execute(
                        # a credential stop's reasons quote the start of what matched: never read them
                        "select r.tag, r.created_at, r.verdict, r.subject, r.kind, r.sent_message_id, "
                        "case when r.kind = 'secret' then null else r.reasons end as reasons, "
                        "coalesce(r.audience, 'owner') as audience, r.draft is not null as has_draft "
                        "from reviews r where r.created_at >= ? and r.id = (select max(id) from reviews x "
                        "where x.tag = r.tag and coalesce(x.audience, 'owner') = coalesce(r.audience, 'owner'))",
                        (since,)):
                    key = tags.get(r["tag"])
                    if key:
                        out["by_session"].setdefault(key, {"sent": [], "inbound": []}).setdefault(
                            "reviews", []).append(dict(r))
            except sqlite3.OperationalError:
                pass
        except sqlite3.Error as e:
            self.con = None
            out["ok"] = False
            out["error"] = str(e)
        out["held"].sort(key=lambda h: -h["ts"])
        return out


def _addressed_to(to_addr, owners):
    t = (to_addr or "").lower()
    return any(o in t for o in owners)


# ------------------------------------------------------------------ git
def _git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def parse_status_v2(text):
    """`git status --porcelain=v2 --branch` -> branch, upstream, ahead, behind, changed (tracked), untracked."""
    out = {"oid": "", "branch": "", "upstream": "", "ahead": None, "behind": None, "changed": 0, "untracked": 0}
    for line in (text or "").splitlines():
        if line.startswith("# branch.oid "):
            out["oid"] = line.split()[-1]
        elif line.startswith("# branch.head "):
            out["branch"] = line.split(" ", 2)[2]
        elif line.startswith("# branch.upstream "):
            out["upstream"] = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            a, b = line.split()[2:4]
            out["ahead"], out["behind"] = int(a.lstrip("+")), int(b.lstrip("-"))
        elif line[:2] in ("1 ", "2 ", "u "):
            out["changed"] += 1
        elif line.startswith("? "):
            out["untracked"] += 1
    return out


def github_slug(url):
    """owner/repo from a GitHub remote URL, or ""."""
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url or "")
    return "%s/%s" % (m.group(1), m.group(2)) if m else ""


_repo_meta = {}   # toplevel -> (ts, {"slug", "default"}): remote URL and default branch change rarely


def _repo_info(cwd, toplevel, now):
    hit = _repo_meta.get(toplevel)
    if hit and now - hit[0] < 3600:
        return hit[1]
    url = _git(cwd, "config", "--get", "remote.origin.url") or ""
    default = _git(cwd, "rev-parse", "--abbrev-ref", "origin/HEAD") or ""
    if not default:
        for cand in ("origin/main", "origin/master"):
            if _git(cwd, "rev-parse", "--verify", "--quiet", cand):
                default = cand
                break
    info = {"slug": github_slug(url), "default": default}
    _repo_meta[toplevel] = (now, info)
    return info


def git_facts(cwd, now=None):
    if not cwd or not os.path.isdir(cwd):
        return {}
    now = now or time.time()
    top = _git(cwd, "rev-parse", "--show-toplevel", "--git-common-dir")
    if not top or len(top.splitlines()) < 2:
        return {}
    toplevel, common = top.splitlines()[:2]
    common = common if os.path.isabs(common) else os.path.join(cwd, common)
    repo = os.path.basename(os.path.dirname(os.path.abspath(common))) or os.path.basename(toplevel)
    raw_status = _git(cwd, "status", "--porcelain=v2", "--branch", "--untracked-files=normal")
    st = parse_status_v2(raw_status)
    # branch: status knows it even on an unborn branch; if status timed out, ask rev-parse (fast)
    head = st["branch"] if raw_status is not None else (_git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "")
    if head == "(detached)":
        head = "HEAD"
    log = _git(cwd, "log", "-1", "--format=%h%x00%s%x00%ct")
    last = {}
    if log and log.count("\x00") == 2:
        h, subj, ct = log.split("\x00")
        last = {"hash": h, "subject": subj[:100], "ts": int(ct) if ct.isdigit() else 0}
    info = _repo_info(cwd, toplevel, now)
    not_in_default = None
    if info["default"] and st["oid"] and not st["oid"].startswith("("):     # detached heads too: commits are commits
        n = _git(cwd, "rev-list", "--count", "%s..HEAD" % info["default"])
        not_in_default = int(n) if n and n.isdigit() else None
    branch = head or "HEAD"                      # from rev-parse: fast, and right even if status times out
    known = raw_status is not None               # a timed-out status must read as unknown, never as clean
    linked = os.path.realpath(common) != os.path.realpath(os.path.join(toplevel, ".git"))   # a `git worktree add` tree
    return {"repo": repo, "branch": branch, "toplevel": toplevel, "oid": st["oid"], "status_ok": known, "linked": linked,
            "dirty": st["changed"] if known else None, "untracked": st["untracked"] if known else None,
            "upstream": st["upstream"],
            "ahead": st["ahead"], "behind": st["behind"], "default": info["default"], "slug": info["slug"],
            "not_in_default": not_in_default, "last_commit": last}


class PullRequests:
    """Every PR per GitHub repo, from one `gh pr list` per repo every `ttl` seconds, in the background.
    A failed or missing gh just means no PR column; it never blocks a poll."""

    RETRY = 60

    def __init__(self, ttl=300, runner=None, sync=False):
        self.ttl = ttl
        self.runner = runner or self._gh
        self.sync = sync          # one-shot snapshots wait for the answer; the stream never does
        self.by_slug = {}      # slug -> (ts, {branch: pr})
        self.pending = set()
        self.lock = threading.Lock()

    @staticmethod
    def _gh(slug):
        gh = find_bin("gh")
        if not gh:
            return None
        r = subprocess.run([gh, "pr", "list", "--repo", slug, "--state", "all", "--limit", "100", "--json",
                            "number,state,headRefName,headRefOid,baseRefName,url,isDraft,mergedAt,title"],
                           capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout) if r.returncode == 0 else None

    def _fetch(self, slug, now=None):
        try:
            rows = self.runner(slug)
        except Exception:
            rows = None
        table = {}
        for pr in rows or []:
            b = pr.get("headRefName")
            if b and (b not in table or pr.get("number", 0) > table[b].get("number", 0)):
                table[b] = {k: pr.get(k) for k in ("number", "state", "url", "isDraft", "mergedAt", "headRefOid", "baseRefName", "title")}
        with self.lock:
            when = now or time.time()
            if rows is not None:
                self.by_slug[slug] = (when, table)
            else:   # keep what we had; retry in a minute, not on every poll
                old = self.by_slug.get(slug, (0, {}))[1]
                self.by_slug[slug] = (when - self.ttl + self.RETRY, old)
            self.pending.discard(slug)

    def invalidate(self):
        with self.lock:
            self.by_slug = {k: (0, t) for k, (_, t) in self.by_slug.items()}

    def lookup(self, slug, branch, now=None):
        if not slug or not branch:
            return None
        now = now or time.time()
        with self.lock:
            hit = self.by_slug.get(slug)
            due = (hit is None or now - hit[0] > self.ttl) and slug not in self.pending
            if due:
                self.pending.add(slug)
        if due:
            if self.sync:
                self._fetch(slug, now)
            else:
                threading.Thread(target=self._fetch, args=(slug, now), daemon=True).start()
        hit = self.by_slug.get(slug)
        return (hit[1] if hit else {}).get(branch)


def list_worktrees(roots=None):
    """Every linked worktree of every main repo directly under the roots (default: the configured repos_root),
    wherever the worktree itself lives: [{path, repo, main, branch, detached, locked}]. Missing (prunable) ones
    are skipped."""
    out = []
    for root in roots or [config.repos_root()]:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for d in entries:
            main = os.path.join(root, d)
            if not os.path.isdir(os.path.join(main, ".git")):
                continue
            for block in (_git(main, "worktree", "list", "--porcelain") or "").split("\n\n")[1:]:
                f = {}
                for line in block.splitlines():
                    k, _, v = line.partition(" ")
                    f[k] = v
                path = f.get("worktree")
                if not path or "prunable" in f or not os.path.isdir(path):
                    continue
                out.append({"path": path, "repo": d, "main": main,
                            "branch": f.get("branch", "").replace("refs/heads/", ""),
                            "detached": "detached" in f, "locked": "locked" in f})
    return out


class GitCache:
    """git facts per cwd, refreshed at most every `ttl` seconds and at most `budget` cwds per poll."""

    def __init__(self, ttl=60, budget=4, fetch=git_facts):
        self.ttl, self.budget, self.fetch = ttl, budget, fetch
        self.cache = {}

    def invalidate(self):
        """Next poll refetches every cwd (a manual refresh)."""
        self.cache = {c: (0, f) for c, (_, f) in self.cache.items()}
        self.burst = True

    burst = False

    def get_many(self, cwds, now=None):
        now = now or time.time()
        budget, self.burst = (10 ** 6 if self.burst else self.budget), False
        stale = sorted((c for c in set(cwds) if c and now - self.cache.get(c, (0, {}))[0] > self.ttl),
                       key=lambda c: self.cache.get(c, (0, {}))[0])
        for c in stale[:budget]:
            self.cache[c] = (now, self.fetch(c))
        return {c: self.cache.get(c, (0, {}))[1] for c in cwds}


# ------------------------------------------------------------------ machine metrics
class Metrics:
    def __init__(self, gpu=True):
        self.prev_cpu = None
        self.disks = []
        self.disks_at = 0
        self.gpus = []
        self.gpu_at = 0
        self.gpu_proc = None
        if gpu:
            self._start_gpu()

    def _start_gpu(self):
        smi = find_bin("nvidia-smi")
        if not smi:
            return
        try:
            self.gpu_proc = subprocess.Popen(
                [smi, "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw",
                 "--format=csv,noheader,nounits", "-l", "5"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError:
            self.gpu_proc = None
            return
        threading.Thread(target=self._gpu_reader, daemon=True).start()

    def _gpu_reader(self):
        batch = []
        for line in self.gpu_proc.stdout:
            f = [x.strip() for x in line.split(",")]
            if len(f) < 5:
                continue
            g = {"name": f[0], "mem_used": _num(f[1]) * 2**20, "mem_total": _num(f[2]) * 2**20,
                 "util": _num(f[3]), "temp": _num(f[4]), "power": _num(f[5]) if len(f) > 5 else None}
            batch.append(g)
            # nvidia-smi prints one line per GPU per tick; a repeated name starts a new tick
            if len(batch) > 1 and batch[-1]["name"] == batch[0]["name"]:
                batch = [batch[-1]]
            self.gpus = list(batch)
            self.gpu_at = time.time()

    def close(self):
        if self.gpu_proc:
            self.gpu_proc.terminate()

    def memory(self):
        info = {}
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            return {}
        total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
        return {"total": total, "available": avail, "used": total - avail,
                "swap_total": info.get("SwapTotal", 0), "swap_used": info.get("SwapTotal", 0) - info.get("SwapFree", 0)}

    def cpu(self):
        try:
            first = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
            vals = [int(x) for x in first]
        except (OSError, ValueError, IndexError):
            return {}
        idle, total = vals[3] + vals[4], sum(vals[:8])
        pct = None
        if self.prev_cpu and total > self.prev_cpu[1]:
            pct = round(100.0 * (1 - (idle - self.prev_cpu[0]) / (total - self.prev_cpu[1])), 1)
        self.prev_cpu = (idle, total)
        load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
        return {"pct": pct, "load1": round(load[0], 2), "ncpu": os.cpu_count() or 1}

    def discover_disks(self):
        """One entry per real block device: the shortest mount point wins, /boot* and snap binds skipped."""
        best = {}
        try:
            mounts = Path("/proc/mounts").read_text().splitlines()
        except OSError:
            mounts = []
        for line in mounts:
            f = line.split()
            if len(f) < 3 or not f[0].startswith("/dev/") or f[1].startswith(("/boot", "/snap", "/var/snap")):
                continue
            mnt = f[1].replace("\\040", " ")
            if f[0] not in best or len(mnt) < len(best[f[0]]):
                best[f[0]] = mnt
        disks = []
        for dev, mnt in best.items():
            kind, model = _disk_kind(dev)
            label = "root" if mnt == "/" else os.path.basename(mnt.rstrip("/"))
            disks.append({"device": dev, "mount": mnt, "kind": kind, "model": model, "label": label})
        disks.sort(key=lambda d: (d["mount"] != "/", d["mount"]))
        return disks

    def disk_usage(self, now):
        if now - self.disks_at > 600:
            self.disks, self.disks_at = self.discover_disks(), now
        out = []
        for d in self.disks:
            try:
                st = os.statvfs(d["mount"])
            except OSError:
                continue
            total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
            out.append(dict(d, total=total, free=free, used=total - st.f_bfree * st.f_frsize))
        return out

    def read(self, now=None):
        now = now or time.time()
        gpus = self.gpus if now - self.gpu_at < 30 else []
        return {"ts": now, "mem": self.memory(), "cpu": self.cpu(), "disks": self.disk_usage(now), "gpus": gpus}


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _disk_kind(dev):
    """("SSD"|"HDD"|"disk", model) from sysfs, following a partition to its parent device."""
    name = os.path.basename(os.path.realpath(dev))
    sys_block = Path("/sys/class/block") / name
    parent = sys_block
    if (sys_block / "partition").exists():
        parent = sys_block.resolve().parent
    try:
        rota = (parent / "queue/rotational").read_text().strip()
        kind = "HDD" if rota == "1" else "SSD"
    except OSError:
        kind = "disk"
    try:
        model = (parent / "device/model").read_text().strip()
    except OSError:
        model = ""
    return kind, model


# ------------------------------------------------------------------ plan usage
class Usage:
    """Plan usage behind one interface: `agent-usage --json` when installed; until then its cache file.

    The stand-in never writes the cache and never runs the probes itself, so swapping to agent-usage
    later needs no change here beyond it appearing on PATH."""

    def __init__(self, cache_path=USAGE_CACHE, interval=300, runner=None):
        self.cache_path = Path(cache_path)
        self.interval = interval
        self.runner = runner or self._run_agent_usage
        # show the last cached reading at once; the first real read happens in the background.
        # The cache is another tool's file: nothing in it may stop the collector from starting.
        try:
            cached = self.from_cache()
        except Exception:
            cached = None
        self.data = cached or {"source": "none", "accounts": [], "observed_at": None, "error": None}
        self.at = 0
        self.lock = threading.Lock()
        self.busy = False

    @staticmethod
    def _run_agent_usage(refresh):
        exe = find_bin("agent-usage")
        if not exe:
            return None
        cmd = [exe, "--json"] + (["--refresh"] if refresh else [])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or "agent-usage exit %d" % r.returncode)
        return json.loads(r.stdout)

    def from_cache(self):
        try:
            raw = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        accounts = []
        for target, snap in raw.items():
            if not isinstance(snap, dict):
                continue
            a = dict(snap, target=target)
            a.setdefault("label", "Codex" if target == "codex" else "Claude (%s)" % target.split(":", 1)[-1])
            a["stale"] = True
            accounts.append(a)
        accounts.sort(key=lambda a: (a["target"] == "codex", a["target"]))
        seen = [a["observed_at"] for a in accounts if isinstance(a.get("observed_at"), (int, float))]
        return {"source": "cache", "accounts": accounts, "observed_at": max(seen) if seen else None, "error": None}

    def refresh(self, force=False):
        with self.lock:
            if self.busy:
                return
            self.busy = True
        try:
            data, err = None, None
            try:
                raw = self.runner(force)
                if raw is not None:
                    data = {"source": "agent-usage", "accounts": raw.get("accounts") or [],
                            "observed_at": raw.get("generated_at"), "recommendation": raw.get("recommendation"),
                            "error": None}
            except Exception as e:   # a broken probe must never take the board down
                err = str(e)[:300]
            if data is None:
                try:
                    data = self.from_cache()
                except Exception:
                    data = None
                data = data or {"source": "none", "accounts": [], "observed_at": None, "error": None}
                if err:
                    data["error"] = err
            self.data, self.at = data, time.time()
        finally:
            self.busy = False

    def kick(self):
        """Re-read now, through agent-usage's own cache (a manual board refresh; no new probes)."""
        self.at = time.time()
        threading.Thread(target=self.refresh, args=(False,), daemon=True).start()

    def maybe_refresh(self, now=None, force=False):
        now = now or time.time()
        if force or now - self.at > self.interval:
            self.at = now
            threading.Thread(target=self.refresh, args=(force,), daemon=True).start()
        return self.data
