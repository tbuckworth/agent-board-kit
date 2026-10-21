"""Address book: every Claude Code and Codex session on this machine, live or recent.

  Claude Code  ~/.claude/sessions/<pid>.json   (name, sessionId, cwd, status, inbox socket), and the same
               under every other login's config dir (config usage.claude_accounts, CLAUDE_CONFIG_DIR)
  Codex        ~/.codex/thread-writer-locks/    (live thread ids)
               ~/.codex/state_5.sqlite          (title, cwd, updated_at per thread)
               ~/.codex/session_index.jsonl     (thread names from /rename)
"""
import glob
import json
import os
import shutil
import socket
import sqlite3
import subprocess
from pathlib import Path

HOME = Path.home()
DEFAULT_CLAUDE_HOME = HOME / ".claude"
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", DEFAULT_CLAUDE_HOME))
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
HOST = socket.gethostname().split(".")[0]


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def same_process(pid, proc_start):
    """Claude records its process start time (procStart, /proc/PID/stat field 22). A pid reused by another
    process after a crash left the registry entry behind has a different start time."""
    if not proc_start:
        return True
    try:
        with open("/proc/%d/stat" % int(pid)) as f:
            return f.read().rsplit(")", 1)[1].split()[19] == str(proc_start)
    except (OSError, IndexError, ValueError):
        return not os.path.isdir("/proc")   # no /proc (the Mac): the pid check is all there is


def find_bin(name, extra):
    found = shutil.which(name)
    if found:
        return found
    for cand in extra:
        cand = Path(os.path.expanduser(cand))
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    try:  # cron and non-login SSH shells lack the user's PATH
        out = subprocess.run(["bash", "-lc", "command -v " + name],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        if out:
            return out.splitlines()[-1]
    except Exception:
        pass
    return None


def claude_bin():
    return find_bin("claude", ["~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"])


def codex_bin():
    return find_bin("codex", ["~/.local/bin/codex", "/opt/homebrew/bin/codex", "/usr/local/bin/codex"])


# Once Codex knows of a newer release, an interactive start opens on an "Update available" menu and waits for a key
# before it loads the thread (so every Codex agent resumed after a restart would sit there). Unattended
# launches skip that check.
CODEX_NO_UPDATE_PROMPT = ["-c", "check_for_update_on_startup=false"]


def short(s, n=8):
    return (s or "")[:n]


def claude_homes():
    """[(account, home, config_dir)] for every Claude login here: this process's own (CLAUDE_HOME), the default
    ~/.claude, and each account in config usage.claude_accounts. A session's registry entry and transcript live in
    the home it was started with; config_dir is the CLAUDE_CONFIG_DIR that reaches it again (None: the default)."""
    try:
        from . import config, usage
        accts = usage.settings(config.load())["claude_accounts"]
        named = {}
        for name, a in accts.items():
            d = usage.expand((a or {}).get("config_dir")) or str(DEFAULT_CLAUDE_HOME.resolve())
            named.setdefault(d, name)
    except Exception:
        named = {}
    out, seen = [], set()
    for home in [CLAUDE_HOME, DEFAULT_CLAUDE_HOME] + [Path(d) for d in named]:
        key = str(Path(home).resolve())
        if key in seen:
            continue
        seen.add(key)
        default = key == str(DEFAULT_CLAUDE_HOME.resolve())
        out.append((named.get(key) or ("default" if default else Path(key).name), Path(home), None if default else key))
    return out


def claude_sessions():
    out = []
    for account, home, config_dir in claude_homes():
        out += _claude_sessions_in(home / "sessions", account, config_dir)
    return one_per_id(out)


def one_per_id(sessions):
    """A killed claude leaves its registry entry behind (agent-failover and agent-switch SIGKILL it on purpose), so
    a session id can have dead entries beside its live one, often under another login. Keep an id's live entries, or
    with none live, its latest entry."""
    live = {s["id"] for s in sessions if s["live"]}
    latest = {}
    for s in sessions:
        sid = s["id"]
        if sid and sid not in live and (sid not in latest or s["updated_at"] > latest[sid]["updated_at"]):
            latest[sid] = s
    return [s for s in sessions if s["live"] or not s["id"] or latest.get(s["id"]) is s]


def _claude_sessions_in(sessions_dir, account, config_dir):
    out = []
    for f in sorted(sessions_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        pid = d.get("pid")
        sock = d.get("messagingSocketPath")
        live = pid_alive(pid) and bool(sock) and os.path.exists(sock) and same_process(pid, d.get("procStart"))
        out.append({
            "cli": "claude",
            "name": d.get("name") or "",
            "id": d.get("sessionId") or "",
            "cwd": d.get("cwd") or "",
            "status": d.get("status") or "",
            "kind": d.get("kind") or "",
            "live": live,
            "socket": sock,
            "pid": pid,
            "tmux": d.get("tmux"),
            "updated_at": (d.get("statusUpdatedAt") or d.get("updatedAt") or 0) / 1000.0,
            "waiting_for": d.get("waitingFor") or "",
            "account": account,
            "config_dir": config_dir,
        })
    return out


def codex_names():
    names = {}
    idx = CODEX_HOME / "session_index.jsonl"
    if idx.exists():
        for line in idx.read_text(errors="replace").splitlines():
            try:
                d = json.loads(line)
                names[d["id"]] = d.get("thread_name") or ""
            except Exception:
                pass
    return names


def codex_held_locks(lock_dir, proc="/proc"):
    """Thread ids whose lock file a codex process has open, or None where that cannot be seen (no /proc).
    A codex that was killed leaves its lock file behind; only an open one means the thread is live."""
    if not os.path.isdir(proc):
        return None
    held, prefix = set(), str(lock_dir) + os.sep
    for d in os.listdir(proc):
        if not d.isdigit():
            continue
        try:
            with open(os.path.join(proc, d, "cmdline"), "rb") as f:
                argv0 = f.read().split(b"\0", 1)[0].decode(errors="replace")
            if "codex" not in os.path.basename(argv0):
                continue
            for fd in os.listdir(os.path.join(proc, d, "fd")):
                target = os.readlink(os.path.join(proc, d, "fd", fd))
                if target.startswith(prefix) and target.endswith(".lock"):
                    held.add(os.path.basename(target)[:-len(".lock")])
        except OSError:
            continue
    return held


def codex_live_ids():
    live = set()
    lock_dir = CODEX_HOME / "thread-writer-locks"
    if lock_dir.is_dir():
        for p in lock_dir.glob("*.lock"):
            if not p.name.startswith("."):
                live.add(p.stem)
        held = codex_held_locks(lock_dir.resolve())
        if held is not None:
            live &= held
    return live


def codex_threads(limit=400):
    meta = {}
    db = CODEX_HOME / "state_5.sqlite"
    if not db.exists():
        return meta
    try:
        # mode=ro fails on macOS for a WAL database; a plain open with query_only never writes.
        con = sqlite3.connect(str(db), timeout=2)
        con.execute("pragma query_only=1")
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "select id, title, cwd, archived, updated_at from threads "
            "where archived = 0 order by updated_at desc limit ?", (limit,)).fetchall()
        for r in rows:
            meta[r["id"]] = dict(r)
        con.close()
    except sqlite3.Error:
        pass
    return meta


def codex_sessions():
    names = codex_names()
    live_ids = codex_live_ids()
    meta = codex_threads()
    ids = list(live_ids) + [i for i in meta if i not in live_ids]
    out = []
    for tid in ids:
        m = meta.get(tid, {})
        out.append({
            "cli": "codex",
            "name": names.get(tid, ""),
            "id": tid,
            "cwd": m.get("cwd", ""),
            "status": "",
            "kind": "interactive",
            "live": tid in live_ids,
            "title": m.get("title", ""),
            "updated_at": (m.get("updated_at") or 0),
        })
    return out


def all_sessions(cli=None):
    s = claude_sessions() + codex_sessions()
    if cli:
        s = [x for x in s if x["cli"] == cli]
    return s


def live_sessions(cli=None):
    return [s for s in all_sessions(cli) if s["live"]]


def claude_transcript_session(sid):
    """An exited Claude session leaves no registry entry, only its transcript. Given a
    full session id, find the transcript so a resume can still reach it."""
    if len(sid) != 36 or sid.count("-") != 4:
        return None
    # agent-failover leaves a copy under each login a session ran on: resume from the newest one
    found = [(os.path.getmtime(h), h, acct, cdir) for acct, home, cdir in claude_homes()
             for h in glob.glob(str(home / "projects" / "*" / (sid + ".jsonl")))]
    if not found:
        return None
    _, hit, account, config_dir = max(found, key=lambda x: x[0])
    hits = [hit]
    cwd = ""
    try:  # Keep even a removed cwd, so agent-send can refuse to resume in the wrong folder.
        with open(hits[0], errors="replace") as fh:
            for _ in range(200):
                line = fh.readline()
                if not line:
                    break
                try:
                    c = json.loads(line).get("cwd")
                except Exception:
                    continue
                if c:
                    cwd = c
                    break
    except OSError:
        pass
    return {"cli": "claude", "name": "", "id": sid, "cwd": cwd, "status": "exited",
            "kind": "", "live": False, "socket": None, "pid": None, "tmux": None,
            "transcript": hits[0], "updated_at": 0, "account": account, "config_dir": config_dir}


def by_id(sessions, sid):
    """(session, code, candidates) for one full session id among sessions, never a name or a prefix: 0 found,
    3 ambiguous, 4 none. The live entry is the session, whatever dead entries share its id (see one_per_id); with
    none live, they are one offline session and the latest stands for it. Only two live entries are ambiguous."""
    cands = [s for s in sessions if s["id"] == sid]
    live = [s for s in cands if s["live"]]
    if len(live) > 1:
        return None, 3, live
    if live:
        return live[0], 0, live
    if cands:
        return max(cands, key=lambda s: s.get("updated_at") or 0), 0, cands
    return None, 4, []


def remove_dead_entry(sess):
    """Remove the registry entry a killed claude left: <its login>/sessions/<pid>.json and <pid>.*.key, which claude
    removes itself on a normal exit. Only while the file still names this pid and session id, and never while the
    pid is alive. Returns the paths removed."""
    try:
        pid = int(sess.get("pid") or 0)
    except (TypeError, ValueError):
        return []
    if not pid or not sess.get("id"):
        return []
    sessions_dir = (Path(sess["config_dir"]) if sess.get("config_dir") else DEFAULT_CLAUDE_HOME) / "sessions"
    entry = sessions_dir / ("%d.json" % pid)
    try:
        d = json.loads(entry.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(d, dict) or str(d.get("pid")) != str(pid) or d.get("sessionId") != sess["id"] or pid_alive(pid):
        return []
    removed = []
    for f in [entry] + sorted(sessions_dir.glob("%d.*.key" % pid)):
        try:
            f.unlink()
            removed.append(str(f))
        except OSError:
            pass
    return removed


def orchestrator_name():
    """The orchestrator's address. It has no long-lived session, so the name must never fuzzy-match one."""
    try:
        from . import config
        return config.load().get("orchestrator") or "orchestrator"
    except Exception:
        return "orchestrator"


def resolve(target, cli=None):
    """Return (session, error_code, candidates). 0 ok, 3 ambiguous, 4 not found.

    Exact name, exact id, id prefix (6+), name prefix, then the start of an unnamed session's
    title (what --list shows for it). The orchestrator's name matches only a session of exactly
    that name: every spawned agent's brief mentions the orchestrator."""
    sessions = all_sessions(cli)
    live_first = sorted(sessions, key=lambda s: not s["live"])
    picks = [lambda s: s["name"] == target, lambda s: s["id"] == target]
    if target.lower() != orchestrator_name().lower():
        picks += [
            lambda s: len(target) >= 6 and s["id"].startswith(target),
            lambda s: s["name"].startswith(target),
            lambda s: (len(target) >= 4 and not s["name"]
                       and (s.get("title") or "").strip().lower().startswith(target.lower())),
        ]
    for pick in picks:
        hits = [s for s in live_first if pick(s)]
        if not hits:
            continue
        live = [s for s in hits if s["live"]]
        chosen = live if live else hits
        if len(chosen) == 1 or len({s["id"] for s in chosen}) == 1:
            return chosen[0], 0, chosen
        return None, 3, chosen
    if cli in (None, "claude"):
        gone = claude_transcript_session(target)
        if gone:
            return gone, 0, [gone]
    return None, 4, []


def current_session(cwd=None):
    """The session this process runs inside: CLAUDE_CODE_SESSION_ID, else the one live
    session whose cwd matches. Returns a dict with at least cli/id/name, or None."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        for s in claude_sessions():
            if s["id"] == sid:
                return s
        return {"cli": "claude", "id": sid, "name": "", "cwd": cwd or os.getcwd(), "live": True}
    cwd = os.path.realpath(cwd or os.getcwd())
    cands = [s for s in live_sessions() if s.get("cwd") and os.path.realpath(s["cwd"]) == cwd]
    if not cands:
        return None
    cands.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return cands[0]
