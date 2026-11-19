"""Address book: every Claude Code and Codex session on this machine, live or recent.

  Claude Code  ~/.claude/sessions/<pid>.json   (name, sessionId, cwd, status, inbox socket), and the same
               under every other login's config dir (config usage.claude_accounts, CLAUDE_CONFIG_DIR)
  Codex        ~/.codex/thread-writer-locks/    (live thread ids)
               ~/.codex/state_5.sqlite          (title, cwd, updated_at, rollout log per thread)
               ~/.codex/session_index.jsonl     (thread names from /rename)
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import accounts, config, jsonfile

DEFAULT_CLAUDE_HOME = config.HOME / ".claude"
CLAUDE_JSON = config.HOME / ".claude.json"   # the default login's .claude.json sits beside ~/.claude, not in it
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", DEFAULT_CLAUDE_HOME))
CODEX_HOME = Path(os.environ.get("CODEX_HOME", config.HOME / ".codex"))
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
# before it loads the thread. Unattended launches skip that check.
CODEX_NO_UPDATE_PROMPT = ["-c", "check_for_update_on_startup=false"]


def short(s, n=8):
    return (s or "")[:n]


def claude_homes():
    """[(account, home, config_dir)] for every Claude login here: this process's own (CLAUDE_HOME), the default
    ~/.claude, and each account in config usage.claude_accounts. A session's registry entry and transcript live in
    the home it was started with; config_dir is the CLAUDE_CONFIG_DIR that reaches it again (None: the default).
    Reads config.json; raises when it does not parse."""
    from . import usage   # usage imports registry: imported here, when called
    named = {}
    for name, a in usage.settings(config.load())["claude_accounts"].items():
        d = accounts.expand((a or {}).get("config_dir")) or str(DEFAULT_CLAUDE_HOME.resolve())
        named.setdefault(d, name)
    out, seen = [], set()
    for home in [CLAUDE_HOME, DEFAULT_CLAUDE_HOME] + [Path(d) for d in named]:
        key = str(Path(home).resolve())
        if key in seen:
            continue
        seen.add(key)
        default = key == str(DEFAULT_CLAUDE_HOME.resolve())
        out.append((named.get(key) or ("default" if default else Path(key).name), Path(home), None if default else key))
    return out


def claude_home(config_dir):
    """A login's Claude home: its config dir, or ~/.claude for the default login (None)."""
    return Path(config_dir) if config_dir else DEFAULT_CLAUDE_HOME


def transcripts(home, sid):
    """Session sid's transcripts under one Claude home (projects/<folder>/<sid>.jsonl), newest first."""
    return sorted(Path(home).glob("projects/*/%s.jsonl" % sid), key=lambda p: p.stat().st_mtime, reverse=True)


def transcript_copies(sid):
    """The session's transcript under each login, newest first: [(mtime, account, config_dir, path)]. A move leaves
    a copy on every login the session ran on; only the newest holds the whole conversation."""
    found = [(p.stat().st_mtime, acct, cdir, p) for acct, home, cdir in claude_homes() for p in transcripts(home, sid)]
    return sorted(found, key=lambda c: c[0], reverse=True)


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
    """The sessions in one login's registry folder. A file that is not JSON (Claude Code writing it this moment, or
    damage) is skipped and said on stderr; one gone since the listing is a session that ended."""
    out = []
    for f in sorted(sessions_dir.glob("*.json")):
        try:
            d = jsonfile.read(f)
        except ValueError as e:
            print("%s; skipped" % e, file=sys.stderr)
            continue
        if not d:
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
    """{thread id: its /rename name} from Codex's session index. A damaged line (jsonfile.read_lines), or one with
    no thread id, is skipped and said on stderr."""
    idx = CODEX_HOME / "session_index.jsonl"
    rows = jsonfile.read_lines(idx)
    named = [d for d in rows if d.get("id")]
    if len(named) < len(rows):
        print("%s: skipped %d line(s) with no thread id" % (idx, len(rows) - len(named)), file=sys.stderr)
    return {d["id"]: d.get("thread_name") or "" for d in named}


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


def codex_query(sql, args):
    """The rows of one query on Codex's state database (its threads: title, folder, rollout log), or [] when
    Codex has none. It is opened plainly with query_only, which never writes: mode=ro fails on macOS for a WAL
    database. The existence check keeps the open from creating one. Raises sqlite3.Error, of the type sqlite
    raised, naming the database: a lock Codex holds past 2 s, a damaged file, or a schema this query no longer
    fits. Every session lookup fails until it can be read; the operator checks Codex, or updates the query."""
    db = CODEX_HOME / "state_5.sqlite"
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(str(db), timeout=2)
        try:
            con.execute("pragma query_only=1")
            con.row_factory = sqlite3.Row
            return con.execute(sql, args).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        raise type(e)("%s: %s" % (db, e)) from e


def codex_threads(limit=400):
    """{thread id: its row} for Codex's newest unarchived threads. Raises sqlite3.Error (codex_query)."""
    rows = codex_query("select id, title, cwd, archived, updated_at from threads "
                       "where archived = 0 order by updated_at desc limit ?", (limit,))
    return {r["id"]: dict(r) for r in rows}


def codex_rollout(thread_id):
    """The rollout log of a Codex thread (its conversation), or None if Codex records none. Raises sqlite3.Error when
    its state database cannot be read, which is not the same as a thread with no conversation."""
    rows = codex_query("select rollout_path from threads where id=?", (thread_id,))
    return Path(rows[0]["rollout_path"]) if rows and rows[0]["rollout_path"] else None


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


def live_sessions():
    return [s for s in all_sessions() if s["live"]]


def live_on(config_dir):
    """The live Claude sessions on one login: the one with this config dir (as config usage.claude_accounts
    writes it), or the default login for None."""
    want = accounts.expand(config_dir) if config_dir else None
    return [s for s in claude_sessions() if s["live"] and (s.get("config_dir") or None) == want]


def claude_transcript_session(sid):
    """An exited Claude session leaves no registry entry, only its transcript. Given a full session id, find the
    transcript so a resume can still reach it, with the folder it ran in. Raises OSError if the transcript cannot
    be read: without its folder, agent-send could not refuse to resume it in the wrong one."""
    if len(sid) != 36 or sid.count("-") != 4:
        return None
    # agent-failover leaves a copy under each login a session ran on: resume from the newest one
    found = transcript_copies(sid)
    if not found:
        return None
    _, account, config_dir, path = found[0]
    cwd = ""
    with open(path, errors="replace") as fh:   # keep even a removed cwd, so agent-send can refuse to resume there
        for _ in range(200):
            line = fh.readline()
            if not line:
                break
            try:
                d = json.loads(line)
            except json.JSONDecodeError:   # a last line Claude is still writing
                continue
            if isinstance(d, dict) and d.get("cwd"):
                cwd = d["cwd"]
                break
    return {"cli": "claude", "name": "", "id": sid, "cwd": cwd, "status": "exited",
            "kind": "", "live": False, "socket": None, "pid": None, "tmux": None,
            "transcript": str(path), "updated_at": 0, "account": account, "config_dir": config_dir}


def by_id(sessions, sid):
    """(session, code, candidates) for one full session id among sessions, never a name or a prefix: 0 found,
    3 ambiguous, 4 none. The live entry is the session, whatever dead entries share its id (see one_per_id); with
    none live, they are one offline session and the latest stands for it. Only two live entries are ambiguous."""
    return _one([s for s in sessions if s["id"] == sid])


def by_name(sessions, name):
    """by_id for a session's exact name, never a prefix or a title: a group member is a name on a machine."""
    return _one([s for s in sessions if s["name"] == name])


def _one(cands):
    """(session, code, candidates) for the entries of what should be one session (by_id, by_name)."""
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
    sessions_dir = claude_home(sess.get("config_dir")) / "sessions"
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
    """The orchestrator's address. It has no long-lived session, so the name must never fuzzy-match one. Reads
    config.json; raises when it does not parse."""
    return config.load()["orchestrator"]


def resolve(target, cli=None):
    """Return (session, error_code, candidates). 0 ok, 3 ambiguous, 4 not found.

    Exact name, exact id, id prefix (6+), name prefix, then the start of an unnamed session's
    title (what --list shows for it), then an exited Claude session by the transcript its full id
    names. The orchestrator's name matches only a session of exactly that name: every spawned
    agent's brief mentions the orchestrator."""
    return _resolve(target, cli, exact=False)


def resolve_exact(target, cli):
    """resolve without its guesses: a session's exact name, its id, or a prefix of 6+ of its id, among the sessions
    in the registry. No name prefix, no title, no exited session found by its transcript."""
    return _resolve(target, cli, exact=True)


def _resolve(target, cli, exact):
    sessions = all_sessions(cli)
    live_first = sorted(sessions, key=lambda s: not s["live"])
    picks = [lambda s: s["name"] == target, lambda s: s["id"] == target]
    if target.lower() != orchestrator_name().lower():
        picks.append(lambda s: len(target) >= 6 and s["id"].startswith(target))
        if not exact:
            picks += [
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
    if not exact and cli in (None, "claude"):
        gone = claude_transcript_session(target)
        if gone:
            return gone, 0, [gone]
    return None, 4, []


def session_from_env(cwd):
    """The Claude session this process runs inside, named by CLAUDE_CODE_SESSION_ID (Claude Code sets it for the
    commands it runs), or None outside Claude. One not in the registry yet is {cli, id, name "", cwd, live}."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return None
    for s in claude_sessions():
        if s["id"] == sid:
            return s
    return {"cli": "claude", "id": sid, "name": "", "cwd": cwd, "live": True}


def codex_session_from_env():
    """The Codex session this process runs inside, named by CODEX_THREAD_ID (Codex sets it for the commands it
    runs), or None outside Codex or when the registry does not hold exactly that thread."""
    tid = os.environ.get("CODEX_THREAD_ID")
    if not tid:
        return None
    sess, code, _ = by_id(codex_sessions(), tid)
    return sess if code == 0 else None


def current_session(cwd=None):
    """The session this process runs inside: CLAUDE_CODE_SESSION_ID (session_from_env), else the live session
    working in cwd that was updated last. Returns a dict with at least cli/id/name, or None."""
    sess = session_from_env(cwd or os.getcwd())
    if sess:
        return sess
    cwd = os.path.realpath(cwd or os.getcwd())
    cands = [s for s in live_sessions() if s.get("cwd") and os.path.realpath(s["cwd"]) == cwd]
    if not cands:
        return None
    cands.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return cands[0]
