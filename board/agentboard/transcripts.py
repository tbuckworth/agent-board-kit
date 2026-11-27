"""The transcript archive: every Claude Code and Codex transcript on the desktop (and the Mac, when one is configured),
kept for good in the archive folder the settings name, with a catalog of the sessions by board group and a tree of
links by group (`agent-board transcripts`; the README's "Transcript archive" says what is kept where and how groups are
decided). Off until the owner names the archive folder (CONFIG) and turns on its timer.

archive() runs once a day (agent-board-transcripts.timer). It copies what is new or changed in each source into
<archive>/<machine>/<source>/, mirroring the source's paths, then rebuilds the catalog (<archive>/catalog.sqlite) and
the tree of links <archive>/by-group/. Nothing in the archive is ever deleted or written over: a file that only grew
gets its new end appended, and one rewritten or shrunk is first moved to <archive>/versions/<run>/. A machine reached
over ssh (the Mac) is copied with rsync into <archive>/.incoming/<machine>/ and archived from there the same way; one
that cannot be reached (asleep or away) is skipped until the next run.

A session's group is the board's: the owner's placement on the Mac's layout (groups.py) while that group is on the
board,
else the Time page's grouping (timeuse.assign: the group of most grouped tiles in its repo, else the Time page's model
guess, timeuse.guess, cached in transcripts-guesses.json and asked for at most guess_per_run sessions a run). Once a
session has ended (not running, and quiet for end_after_hours) the group it has then is frozen in the catalog.

The settings file (CONFIG) names the archive folder, and may name the machines and their sources (default_machines
when it names none) and override the rest of DEFAULTS.
"""
import collections
import datetime
import glob
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from . import classify, config, groups, sources, timesources, timeuse

STATE = sources.BOARD_STATE
LOCK = STATE / "transcripts.lock"
LOG = STATE / "transcripts.log"
PARSED = STATE / "transcripts-cache.json"       # what each archived transcript says, read on from where it stopped
GUESSES = STATE / "transcripts-guesses.json"    # the model's guesses (timeuse.guess's cache)
CONFIG = Path(os.environ.get("AGENT_BOARD_TRANSCRIPTS_CONFIG")
              or sources.HOME / ".config/agent-board/transcripts.json")
DEFAULTS = {
    # the archive folder: no default, the settings file must name it, and it must exist (its disk mounted); it is never
    # made here, so an unmounted disk cannot fill another one instead
    "archive": None,
    # [{"name", "ssh"?, "sources": [{"cli": "claude"|"codex", "path"|"glob", "name"?}]}]; null (the default) is
    # default_machines(): this desktop's own sources, and the Mac's when one is configured. A list replaces them. A
    # machine with "ssh" is reached by that host alias; a source's "path" there is relative to its home ("~/" is
    # dropped). A "glob" (local machines only) gives a source per folder it matches, named after its login folder.
    "machines": None,
    # a session not running and quiet this long has ended, and its group is frozen; the board shows an offline tile
    # for at most 48 hours, so an ended session is off the board and the owner can no longer move it there
    "end_after_hours": 48,
    "guess_per_run": 240,     # sessions the model may be asked to group per run: two of the Time page's batches of 120
    "ssh_timeout": 15,        # seconds a machine reached over ssh has to answer before it counts as asleep
}
CATALOG = "catalog.sqlite"
BY_GROUP = "by-group"
VERSIONS = "versions"
INCOMING = ".incoming"
MANIFEST = "sources.json"     # in the archive: each archived source's machine, CLI, login and origin
RESERVED = {CATALOG, BY_GROUP, VERSIONS, INCOMING, MANIFEST}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
UUID_RE = sources.SESSION_ID_RE
UNSORTED = timeuse.UNSORTED   # the by-group folder of sessions no group fits
NO_SUBGROUP = "(none)"
TITLE_CHARS = 120
LINK_NAME_CHARS = 80
CHUNK = 1 << 20
# the start of a first message that is not what the session is about: a slash command's or a hook's text, Claude's
# caveat, a continued session's summary, Codex's AGENTS.md and environment
NOT_A_TITLE = ("<", "Caveat:", "This session is being continued", "# AGENTS.md instructions", "[Request interrupted")
BRIEF_MARK = "\n\nBrief:\n\n"                    # where agent-spawn's header ends and the owner's brief begins
BRIEF_NAME_RE = re.compile(r"Session name: (\S+?)\. ")
REMOTE_HOME_RE = re.compile(r"^/(?:Users|home)/[^/]+/")
# ssh's words when a machine cannot be reached: asleep, away or off the network, not misconfigured. A refused
# connection is not one of them: the machine is up but nothing answers ssh (Remote Login off), a problem to fix
UNREACHABLE = ("timed out", "No route to host", "Network is unreachable", "Host is down")
MAC = "mac"                   # the Mac's machine name in the default machines (its folder in the archive)
HOW_TO_SET = ('name a folder of your own for it, which must already exist (it is never made), in %s: '
              '{"archive": "~/transcript-archive"}; README, "Transcript archive"')
SCHEMA = """create table if not exists sessions (
    id text primary key, cli text, machine text, login text, logins text, files text, cwd text, project text,
    name text, title text, entrypoint text, started real, last_activity real, size integer,
    group_name text, subgroup text, group_source text, group_basis text, frozen_at real, updated real)"""
COLUMNS = ("id", "cli", "machine", "login", "logins", "files", "cwd", "project", "name", "title", "entrypoint",
           "started", "last_activity", "size", "group_name", "subgroup", "group_source", "group_basis", "frozen_at",
           "updated")


class ArchiveError(RuntimeError):
    """The archive cannot run at all: its folder is missing, or the settings are wrong."""


def log(msg):
    sources.append_log(LOG, msg)


# ------------------------------------------------------------------ settings and sources

def default_machines(host_id=None, mac=None):
    """The machines whose transcripts are kept when the settings name none: this desktop, named by the mailroom
    config's host_id (`host_id`; "desktop" when it has none, or one that is not a machine name), with every Claude
    login's projects folder (~/.claude*/projects) and Codex's sessions and archived sessions; and, when a Mac is
    configured (`mac`, its ssh alias: config.mac_host(), the mailroom config's remote_hosts["mac"]), the Mac as MAC,
    reached over ssh, with its default Claude login's projects and Codex's sessions. No Mac configured: nothing is
    reached over ssh."""
    host_id = sources.mail_host() if host_id is None else host_id
    name = host_id if isinstance(host_id, str) and NAME_RE.match(host_id) and host_id not in RESERVED else "desktop"
    out = [{"name": name, "sources": [
        {"cli": "claude", "glob": "~/.claude*/projects"},
        {"cli": "codex", "name": "codex-sessions", "path": "~/.codex/sessions"},
        {"cli": "codex", "name": "codex-archived", "path": "~/.codex/archived_sessions"}]}]
    if mac:
        out.append({"name": MAC if name != MAC else MAC + "-1", "ssh": mac, "sources": [
            {"cli": "claude", "name": "claude-projects", "path": "~/.claude/projects"},
            {"cli": "codex", "name": "codex-sessions", "path": "~/.codex/sessions"}]})
    return out


def settings(path=None, host_id=None, mac=None):
    """DEFAULTS overridden by the settings file at `path` (default CONFIG), with "machines" made concrete: the file's
    list, or default_machines(host_id, mac) when it names none (null or left out; `mac` None reads config.mac_host()).
    Raises ArchiveError naming what is missing or wrong: the archive folder (HOW_TO_SET says how to name one), the
    machines and their sources."""
    path = path or CONFIG
    try:
        cfg = sources.load_config(DEFAULTS, path)
    except (OSError, ValueError) as e:
        raise ArchiveError(sources.error_text(e)) from None
    if not isinstance(cfg["archive"], str) or not cfg["archive"]:
        raise ArchiveError('%s must name the archive folder ("archive"): %s' % (path, HOW_TO_SET % path))
    if cfg["machines"] is None:
        try:
            cfg["machines"] = default_machines(host_id, config.mac_host() if mac is None else mac)
        except (OSError, ValueError) as e:            # a damaged mailroom config
            raise ArchiveError("the default machines could not be worked out: %s" % sources.error_text(e)) from None
    if not isinstance(cfg["machines"], list) or not cfg["machines"]:
        raise ArchiveError('%s: "machines" must list the machines whose transcripts are kept, or be left out for the '
                           "default ones (this desktop, and the Mac when one is configured)" % path)
    seen = set()
    for m in cfg["machines"]:
        name = m.get("name") if isinstance(m, dict) else None
        if not isinstance(name, str) or not NAME_RE.match(name) or name in RESERVED or name in seen:
            raise ArchiveError("each machine needs a name of its own (letters, digits, '.', '_' and '-'), not one of "
                               "the archive's own names: %r" % (name,))
        seen.add(name)
        if "ssh" in m and not (isinstance(m["ssh"], str) and m["ssh"]):
            raise ArchiveError("machine %s: ssh must be a host alias" % name)
        if not isinstance(m.get("sources"), list) or not m["sources"]:
            raise ArchiveError("machine %s lists no sources" % name)
        for s in m["sources"]:
            if not isinstance(s, dict) or s.get("cli") not in ("claude", "codex"):
                raise ArchiveError("machine %s: each source needs a cli, claude or codex" % name)
            if ("path" in s) == ("glob" in s):
                raise ArchiveError("machine %s: each source needs a path or a glob, not both" % name)
            if "glob" in s and m.get("ssh"):
                raise ArchiveError("machine %s: a glob works only on this machine; name each source's path" % name)
            if "path" in s and not (isinstance(s.get("name"), str) and NAME_RE.match(s["name"])):
                raise ArchiveError("machine %s: a source with a path needs a name (letters, digits, '.', '_' and '-')"
                                   % name)
    return cfg


def _login_of(folder, logins):
    """The login a Claude projects folder belongs to: its mailroom login name, else its login folder's name."""
    return logins.get(Path(folder).parent.resolve()) or Path(folder).parent.name.lstrip(".")


def machine_sources(machine, logins=None):
    """[{"name", "cli", "login", "origin", "path"}] of a machine's sources. A glob gives one source per folder it
    matches (each folder once), named after its login folder: ~/.claude-work/projects is claude-work-projects.
    `login` is a Claude login's mailroom name where `logins` ({resolved login folder: name}, read from the mailroom's
    config on this machine when None) knows it, else its folder's name, and codex for Codex. `path` is the folder on
    this machine, or, for a machine reached over ssh, relative to its home. Raises ArchiveError when two sources of
    the machine would share a name."""
    remote = bool(machine.get("ssh"))
    if logins is None:
        logins = {} if remote else {Path(d).resolve(): n for n, d in sources.claude_logins()}
    out = []
    for s in machine["sources"]:
        if "glob" in s:
            seen = set()
            for hit in sorted(glob.glob(os.path.expanduser(s["glob"]))):
                real = Path(hit).resolve()
                if not Path(hit).is_dir() or real in seen:
                    continue
                seen.add(real)
                login = _login_of(hit, logins) if s["cli"] == "claude" else "codex"
                out.append({"name": "%s-%s" % (Path(hit).parent.name.lstrip("."), Path(hit).name), "cli": s["cli"],
                            "login": login, "origin": sources.tilde(hit), "path": hit})
        else:
            p = s["path"]
            path = (p[2:] if p.startswith("~/") else p) if remote else os.path.expanduser(p)
            login = ("codex" if s["cli"] == "codex" else
                     _login_of(path, logins) if not remote else Path(path).parent.name.lstrip(".") or "claude")
            out.append({"name": s["name"], "cli": s["cli"], "login": login, "origin": p, "path": path})
    names = [s["name"] for s in out]
    if len(names) != len(set(names)):
        raise ArchiveError("machine %s: two sources share a name: %s" % (machine["name"], ", ".join(names)))
    return out


# ------------------------------------------------------------------ copying: append, or keep the old version

def private_dirs(path):
    """Make the folder `path` and any missing folder above it, each private (0700): mkdir's own `parents` would make
    those above with the default mode."""
    missing, p = [], Path(path)
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        d.mkdir(mode=0o700, exist_ok=True)


def _pump(src_fh, dst_fh, n):
    """Copy up to n bytes from one open file to another; how many were copied."""
    done = 0
    while done < n:
        buf = src_fh.read(min(CHUNK, n - done))
        if not buf:
            break
        dst_fh.write(buf)
        done += len(buf)
    return done


def _is_prefix(old, new, n):
    """Whether file `old` (n bytes) holds exactly the first n bytes of file `new`."""
    with open(old, "rb") as a, open(new, "rb") as b:
        left = n
        while left:
            k = min(CHUNK, left)
            x, y = a.read(k), b.read(k)
            if x != y or len(x) != k:
                return False
            left -= k
    return True


def _copy_whole(src, st, dst):
    """Copy the first st.st_size bytes of src to dst through a temporary file beside it (dst does not exist), with
    src's modification time. A copy cut short leaves the temporary file (.NAME.XXXX.partial), never a part of dst."""
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix="." + dst.name + ".", suffix=".partial")
    with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
        _pump(inp, out, st.st_size)
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.rename(tmp, dst)


def archive_file(src, st, dst, old_versions):
    """Bring the archived copy `dst` of the file `src` (os.lstat `st`) up to date without losing anything; what was
    done: "new" (copied), "same" (unchanged), "touched" (the same bytes with another time: the time copied), "grown"
    (the new end appended) or "rewritten" (the copy moved to `old_versions`, the path it is kept at, then src copied
    in its place)."""
    try:
        dt = dst.stat()
    except FileNotFoundError:
        private_dirs(dst.parent)
        _copy_whole(src, st, dst)
        return "new"
    if dt.st_size == st.st_size and dt.st_mtime_ns == st.st_mtime_ns:
        return "same"
    if dt.st_size <= st.st_size and _is_prefix(dst, src, dt.st_size):
        if dt.st_size < st.st_size:
            with open(src, "rb") as inp, open(dst, "ab") as out:
                inp.seek(dt.st_size)
                _pump(inp, out, st.st_size - dt.st_size)
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
        return "grown" if dt.st_size < st.st_size else "touched"
    keep = Path(old_versions)
    while keep.exists():               # the same file kept twice in one run: never write over the first
        keep = keep.with_name(keep.name + ".again")
    private_dirs(keep.parent)
    os.rename(dst, keep)
    _copy_whole(src, st, dst)
    return "rewritten"


def sync_tree(src_root, dst_root, versions_root):
    """archive_file for every regular file under `src_root` (symbolic links not followed), into the same path under
    `dst_root`, an old version going to the same path under `versions_root`. Counts of each outcome, and "failed"
    with the first few failures in "errors" (a file that vanished mid-run, or one that cannot be read)."""
    counts = collections.Counter()
    errors = []
    src_root, dst_root, versions_root = Path(src_root), Path(dst_root), Path(versions_root)
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames.sort()
        rel = Path(dirpath).relative_to(src_root)
        for fn in sorted(filenames):
            src = Path(dirpath) / fn
            try:
                st = os.lstat(src)
                if not stat.S_ISREG(st.st_mode):
                    continue
                counts[archive_file(src, st, dst_root / rel / fn, versions_root / rel / fn)] += 1
            except OSError as e:
                counts["failed"] += 1
                if len(errors) < 5:
                    errors.append(sources.error_text(e))
    return dict(counts, **({"errors": errors} if errors else {}))


def fetch(machine, srcs, incoming, cfg, run):
    """Copy each of a machine's sources over rsync into incoming/<source>/ (no --delete: what the machine has dropped
    stays). (warning, problems). The machine is first asked over ssh: when it cannot be reached (ssh's connection
    timed out, or found no route, its network unreachable or the host down: asleep or away) that is a warning and
    nothing else is tried, until the next run; when ssh fails for any other reason (a refused connection, a refused
    key, a changed host key, an unknown alias) that is a problem, with ssh's words. Each source rsync could not copy is
    a problem too."""
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % cfg["ssh_timeout"]]
    host = machine["ssh"]
    try:
        r = run(ssh + [host, "true"], capture_output=True, text=True, timeout=cfg["ssh_timeout"] + 30)
        code, said = r.returncode, (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        code, said = None, "no answer in %d seconds" % (cfg["ssh_timeout"] + 30)
    if code is None or (code == 255 and any(w in said for w in UNREACHABLE)):
        return ("%s could not be reached (%s): it is asleep or away, and its transcripts are copied at the next run"
                % (machine["name"], said[-200:]), [])
    if code != 0:
        return None, ["%s: ssh %s failed (exit %d): %s" % (machine["name"], host, code, said[-300:] or "no message")]
    problems = []
    for s in srcs:
        dest = Path(incoming) / s["name"]
        private_dirs(dest)
        try:
            r = run(["rsync", "-a", "--timeout=120", "-e", " ".join(ssh), "%s:%s/" % (host, s["path"]),
                     str(dest) + "/"], capture_output=True, text=True, timeout=6 * 3600)
            if r.returncode != 0:
                problems.append("%s/%s: rsync exited %d: %s" % (machine["name"], s["name"], r.returncode,
                                                                (r.stderr or "").strip()[-200:]))
        except subprocess.TimeoutExpired:
            problems.append("%s/%s: rsync took over 6 hours" % (machine["name"], s["name"]))
    return None, problems


# ------------------------------------------------------------------ what each transcript says

def first_words(text):
    """A title from a first message: its words, up to TITLE_CHARS; an agent-spawn brief's own text after its header;
    "" for a message that is not what the session is about (NOT_A_TITLE)."""
    t = (text or "").strip()
    if t.startswith(classify.SPAWN_MARK) and BRIEF_MARK in t:
        t = t.split(BRIEF_MARK, 1)[1].strip()
    if t.startswith(NOT_A_TITLE):
        return ""
    return " ".join(t.split())[:TITLE_CHARS]


def brief_name(text):
    """The session name an agent-spawn brief gives ("Session name: X."), or "" for any other message."""
    m = BRIEF_NAME_RE.search(text[:600]) if text.lstrip().startswith(classify.SPAWN_MARK) else None
    return m.group(1) if m else ""


def _blank(cli):
    return {"cli": cli, "sid": "", "parent": "", "cwd": "", "entrypoint": "", "start": None, "first": "",
            "summary": "", "name": ""}


def _ts(d):
    try:
        return sources.iso_ts(d["timestamp"]) if d.get("timestamp") else None
    except (TypeError, ValueError):
        return None


def parse_claude(lines, out, path):
    """What a Claude Code transcript says (sources.read_appended's parse): {"sid", "cwd", "entrypoint", "start", "first"
    (its first message's words, first_words), "summary" (Claude's own title for it), "name" (its /rename name, or an
    agent-spawn brief's session name)}. Lines are checked for what they hold before any is decoded, by words that
    do not depend on the writer's spacing."""
    out = out or _blank("claude")
    for line in lines:
        want_first = not out["first"] and b'"user"' in line
        want_start = out["start"] is None and b'"timestamp"' in line
        titled = (b'"custom-title"' in line or b'"agent-name"' in line or b'"ai-title"' in line
                  or (b'"summary"' in line and b'"leafUuid"' in line))
        if not (want_first or want_start or titled):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if want_start:
            out["start"] = _ts(d)
        out["sid"] = out["sid"] or d.get("sessionId") or ""
        kind = d.get("type")
        if kind == "custom-title" and d.get("customTitle"):
            out["name"] = d["customTitle"]
        elif kind == "agent-name" and d.get("agentName") and not out["name"]:
            out["name"] = d["agentName"]
        elif kind == "ai-title" and d.get("aiTitle"):
            out["summary"] = d["aiTitle"]
        elif kind == "summary" and d.get("summary") and not out["summary"]:
            out["summary"] = d["summary"]
        elif kind == "user" and want_first and not d.get("isMeta") and not d.get("isSidechain"):
            if not out["cwd"]:
                out["cwd"], out["entrypoint"] = d.get("cwd") or "", d.get("entrypoint") or ""
            text = sources.message_text(d.get("message", {}).get("content"))
            words = first_words(text)
            if words:
                out["first"] = words
                out["name"] = out["name"] or brief_name(text)
    return out


def parse_codex(lines, out, path):
    """As parse_claude, for a Codex rollout: its first line says the thread, its folder, how it was started and, for a
    sub-thread, its parent ("parent"); then its first message from the owner is read, and the rest is passed over."""
    if out is None:
        first = next(lines, None)
        if first is None:                     # nothing written yet
            return None
        out = _blank("codex")
        try:
            d = json.loads(first)
        except ValueError:
            return out
        meta = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        src = meta.get("source")
        sub = isinstance(src, dict) and "subagent" in src
        out.update(sid=meta.get("id") or "", cwd=meta.get("cwd") or "", start=_ts(meta) or _ts(d),
                   entrypoint=src if isinstance(src, str) else "subagent" if sub else "",
                   parent=meta.get("parent_thread_id") or (sources.subagent_parent(json.dumps(src), "subagent")
                                                           if sub else ""))
    for line in lines:
        if out["first"] or b'"user"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if p.get("type") == "message" and p.get("role") == "user":
            text = sources.message_text(p.get("content"))
            out["first"] = first_words(text)
            if out["first"]:
                out["name"] = out["name"] or brief_name(text)
    return out


PARSERS = {"claude": parse_claude, "codex": parse_codex}


# ------------------------------------------------------------------ the sessions in the archive

def scan(root, manifest):
    """Every archived file a session owns, as {"path" (under `root`), "key" (machine/source), "size", "mtime", "sid",
    "main", "transcript"}. A Claude source's <project>/<id>.jsonl is a session's main transcript, and every file in its
    <project>/<id>/ folder (subagents, tool results) is that session's; another .jsonl at a project's top is read for
    its session (sid None). Every Codex rollout is read for its thread (sid None). A project's memory/ is no
    session's, and is archived but not catalogued."""
    out = []

    def add(p, key, sid, main, transcript):
        st = p.stat()
        out.append({"path": str(p.relative_to(root)), "key": key, "size": st.st_size, "mtime": st.st_mtime,
                    "sid": sid, "main": main, "transcript": transcript})
    for key, src in sorted(manifest.items()):
        base = Path(root) / key
        if not base.is_dir():
            continue
        if src["cli"] == "claude":
            for proj in sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink()):
                for e in sorted(proj.iterdir()):
                    if e.is_file() and e.name.endswith(".jsonl") and not e.name.startswith("."):
                        stem = e.name[:-len(".jsonl")]
                        add(e, key, stem if UUID_RE.match(stem) else None, True, True)
                    elif e.is_dir() and not e.is_symlink() and UUID_RE.match(e.name):
                        for dirpath, dirnames, files in os.walk(e):
                            dirnames.sort()
                            for f in sorted(files):
                                if not f.startswith("."):
                                    add(Path(dirpath) / f, key, e.name, False, f.endswith(".jsonl"))
        else:
            for dirpath, dirnames, files in os.walk(base):
                dirnames.sort()
                for f in sorted(files):
                    if f.startswith("rollout-") and f.endswith(".jsonl"):
                        add(Path(dirpath) / f, key, None, True, True)
    return out


def _main_copy(e, sid):
    """Whether an entry is a copy of session `sid`'s own transcript: a Claude <id>.jsonl, or a Codex thread's rollout
    (a sub-thread's, moved under its parent, is not main any more)."""
    name = e["path"].rsplit("/", 1)[-1]
    return e["main"] and (name == sid + ".jsonl" or name.startswith("rollout-"))


def _repo_path(cwd, remote, repos_root=None):
    """The folder to find a session's repo by. A remote machine's (the Mac's) folder is first taken at the same place
    under this machine's home, for repos kept at the same place on both; when that folder is not here, the folder under
    the repos root (`repos_root`, sources.REPOS_ROOT: config.repos_root()) that the remote folder's path ends with,
    the longest such ending first (the Mac's ~/src/site/docs is <repos root>/site/docs when that is here); else the
    first, which sources.repo_name still reads by agent-spawn's worktree naming."""
    if not (remote and cwd):
        return cwd
    m = REMOTE_HOME_RE.match(cwd)
    if not m:
        return cwd
    here = str(sources.HOME) + "/" + cwd[m.end():]
    if os.path.isdir(here):
        return here
    root = Path(repos_root or sources.REPOS_ROOT)
    parts = [p for p in cwd[m.end():].split("/") if p]
    for i in range(len(parts)):
        p = root.joinpath(*parts[i:])
        if p.is_dir():
            return str(p)
    return here


def assemble(entries, parsed, manifest, remote, names):
    """{session id: record} from scan()'s entries and each main transcript's parse ({path under the archive: state}).
    A session copied to several logins or sources (failover, Codex archiving a thread) is one record, read from its
    newest copy first; a Claude subagent's files and a Codex sub-thread's rollout are its parent's. `remote`: the
    machines reached over ssh. `names`: {session id: name} for sessions whose transcripts carry none (the mailroom's).
    A record: id, cli, machine, login, logins, files (its transcripts' paths under the archive, the newest main copy
    first), cwd, project, name, title, entrypoint, started, last_activity, size and repo (the folder its repo is
    found by)."""
    own = collections.defaultdict(list)          # session id -> entries
    for e in entries:
        st = parsed.get(e["path"]) if e["main"] else None
        sid = e["sid"] or (st or {}).get("sid")
        if sid:
            own[sid].append(dict(e, state=st))
    for sid in list(own):                        # Codex sub-threads under a parent the archive holds
        states = [e["state"] for e in own[sid] if e["state"]]
        parent = next((s["parent"] for s in states if s.get("parent")), "")
        if parent and parent in own and parent != sid:
            own[parent] += [dict(e, main=False, state=None) for e in own.pop(sid)]
    repos, out = {}, {}
    for sid, es in own.items():
        es.sort(key=lambda e: (-e["mtime"], e["path"]))
        mains = [e for e in es if _main_copy(e, sid)]
        main_paths = {e["path"] for e in mains}
        states = [e["state"] for e in mains if e["state"]] or [e["state"] for e in es if e["state"]]
        pick = lambda k: next((s[k] for s in states if s.get(k)), "")
        head = (mains or es)[0]
        src = manifest[head["key"]]
        cwd = pick("cwd")
        rp = _repo_path(cwd, src["machine"] in remote)
        if rp not in repos:
            repos[rp] = sources.repo_name(rp) or os.path.basename(cwd.rstrip("/"))
        starts = [s["start"] for s in states if s.get("start")]
        others = sorted(e["path"] for e in es if e["transcript"] and e["path"] not in main_paths)
        out[sid] = {
            "id": sid, "cli": src["cli"], "machine": src["machine"], "login": src["login"],
            "logins": sorted({manifest[e["key"]]["login"] for e in mains}) or [src["login"]],
            "files": [e["path"] for e in mains] + others, "cwd": cwd, "project": repos[rp],
            "name": pick("name") or names.get(sid, ""), "title": pick("summary") or pick("first"),
            "entrypoint": pick("entrypoint"), "started": min(starts) if starts else min(e["mtime"] for e in es),
            "last_activity": max(e["mtime"] for e in es), "size": sum(e["size"] for e in es), "repo": rp}
    return out


# ------------------------------------------------------------------ groups

GROUP_FIELDS = ("group_name", "subgroup", "group_source", "group_basis", "frozen_at")


def decide(records, prior, live, history, layout, guesser, now, end_after, cap):
    """({session id: {group_name, subgroup, group_source, group_basis, frozen_at}}, stats) for every record.

    A frozen session (prior's frozen_at set) keeps its group, unless it is running again or has written since it was
    frozen: then it is grouped afresh, and frozen again once it ends. The rest are grouped as the Time page groups a
    tile (timeuse.assign): the owner's placement (`history`, groups.history) while its group is on the board ("placed",
    basis "board"), else the group of most placed sessions of its repo ("guessed", basis "repo"), else the model's
    guess through guesser(items, layout, history, labels) ("guessed", basis "model"), asked for at most `cap`
    sessions, the most recently active first. A session no group fits, or not asked yet, is "unknown" with no group.
    A session not in `live` and quiet for `end_after` seconds has ended: its group is frozen (frozen_at = now),
    unless it is unknown, which stays open for a later guess."""
    out, acts = {}, []
    on_board = {g["name"] for g in layout}
    for sid, r in sorted(records.items(), key=lambda kv: (-kv[1]["last_activity"], kv[0])):
        p = prior.get(sid) or {}
        if p.get("frozen_at") and sid not in live and r["last_activity"] <= p["frozen_at"]:
            out[sid] = {k: p.get(k) for k in GROUP_FIELDS}
            if (history.get(sid) or {}).get("group") not in on_board:
                continue
        # a frozen session placed in a group still on the board counts toward its repo's vote, and is never guessed
        acts.append({"key": "s:" + sid, "kind": "session", "label": r["name"] or r["title"] or sid[:8],
                     "repo": r["repo"], "hint": r["title"]})
    passed, asked = set(), set()

    def capped(items, layout_, history_, labels):
        passed.update(items)
        keep = dict(list(items.items())[:cap])
        asked.update(keep)
        return guesser(keep, layout_, history_, labels) if keep else ({}, {"asked": 0, "batches": 0}, None)
    labels = {sid: r["name"] or r["title"] for sid, r in records.items() if r["name"] or r["title"]}
    assigned, stats, warning = timeuse.assign(acts, history, layout, labels, {}, capped)
    frozen_now = 0
    for a in acts:
        sid = a["key"][2:]
        if sid in out:
            continue
        g, s, inferred = assigned[a["key"]]
        if g == UNSORTED:
            row = {"group_name": None, "subgroup": None, "group_source": "unknown", "group_basis": None}
        else:
            basis = "board" if not inferred else "model" if a["key"] in passed else "repo"
            row = {"group_name": g, "subgroup": s, "group_source": "placed" if basis == "board" else "guessed",
                   "group_basis": basis}
        ended = sid not in live and records[sid]["last_activity"] < now - end_after
        row["frozen_at"] = now if ended and row["group_source"] != "unknown" else None
        frozen_now += bool(row["frozen_at"])
        out[sid] = row
    return out, dict(stats, waiting=len(passed - asked), frozen=frozen_now, warning=warning)


def model_guesser(time_cfg):
    """The Time page's model guess (timeuse.guess, `claude -p`) with the Time page's model and batch size, cached in
    GUESSES."""
    return lambda items, layout, history, labels: timeuse.guess(items, layout, history, labels, time_cfg, GUESSES,
                                                                subprocess.run)


# ------------------------------------------------------------------ the catalog

def open_catalog(path):
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    os.chmod(path, 0o600)
    return con


def write_catalog(con, records, grouped, now):
    """Store every record with its group (insert or replace by id); a session the archive no longer finds stays."""
    with con:
        for sid, r in records.items():
            row = dict(r, **grouped[sid], updated=now, logins=json.dumps(r["logins"]),
                       files=json.dumps(r["files"]))
            con.execute("insert or replace into sessions (%s) values (%s)"
                        % (", ".join(COLUMNS), ", ".join("?" * len(COLUMNS))), [row.get(c) for c in COLUMNS])


def rows(con):
    """Every session in the catalog, as dicts with logins and files decoded, oldest start first."""
    out = []
    for r in con.execute("select * from sessions order by started, id"):
        d = dict(r)
        d["logins"], d["files"] = json.loads(d["logins"] or "[]"), json.loads(d["files"] or "[]")
        out.append(d)
    return out


# ------------------------------------------------------------------ the by-group tree

def _safe(name, limit=LINK_NAME_CHARS):
    """A name as one folder or file name: no slashes or control characters, not hidden, at most `limit` characters."""
    s = re.sub(r"[\x00-\x1f\x7f/]+", "-", " ".join(str(name).split()))[:limit].strip()
    return ("_" + s[1:] if s.startswith(".") else s) or "_"


def link_plan(sessions):
    """{link path under by-group/: archive path it points to} for the catalog's sessions:
    <group or Unsorted>/<sub-group or (none)>/<start date> <name or title> (<cli>).jsonl, pointing at the session's
    newest main transcript. Names that would clash in one folder get the session's whole id: Codex's ids begin with
    the time, so threads started together share their first characters."""
    plan = {}
    for r in sessions:
        if not r["files"]:
            continue
        folder = "%s/%s" % (_safe(r["group_name"] or UNSORTED), _safe(r["subgroup"]) if r["subgroup"] else NO_SUBGROUP)
        day = datetime.date.fromtimestamp(r["started"] or 0).isoformat()
        stem = "%s %s" % (day, _safe(r["name"] or r["title"] or r["id"][:8]))
        plan.setdefault((folder, stem), []).append(r)
    out = {}
    for (folder, stem), rs in plan.items():
        for r in rs:
            tag = " %s" % r["id"] if len(rs) > 1 else ""
            out["%s/%s%s (%s).jsonl" % (folder, stem, tag, r["cli"])] = r["files"][0]
    return out


def link_tree(root, sessions):
    """Bring <root>/by-group/ in line with link_plan(sessions): relative symbolic links, so the tree still works with
    the disk mounted elsewhere. Only links are ever removed (one no session wants any more, or pointing elsewhere), and
    folders left empty; anything else found there is left as it is and counted. {"made", "removed", "kept", "other"}."""
    base = Path(root) / BY_GROUP
    want = {base / k: v for k, v in link_plan(sessions).items()}
    counts = collections.Counter()
    if base.is_dir():
        for dirpath, dirnames, files in os.walk(base):
            for f in files + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
                p = Path(dirpath) / f
                if not p.is_symlink():
                    counts["other"] += 1
                    continue
                target = os.path.relpath(Path(root) / want[p], p.parent) if p in want else None
                if target is not None and os.readlink(p) == target:
                    counts["kept"] += 1
                    want.pop(p)
                else:
                    os.unlink(p)            # a link of this tree, never a file it points at
                    counts["removed"] += 1
    for p, rel in want.items():
        private_dirs(p.parent)
        os.symlink(os.path.relpath(Path(root) / rel, p.parent), p)
        counts["made"] += 1
    if base.is_dir():
        for dirpath, dirnames, files in sorted(os.walk(base), key=lambda t: -len(t[0])):
            if Path(dirpath) != base and not os.listdir(dirpath):
                os.rmdir(dirpath)
    return {k: counts[k] for k in ("made", "removed", "kept", "other")}


# ------------------------------------------------------------------ the whole run

def archive(cfg, now=None, run=subprocess.run, live=None, guesser=None, logins=None, names=None):
    """Copy every source into the archive, then rebuild the catalog and the by-group tree; a report of what it did:
    "copied" ({machine/source: archive_file's counts}), "sessions", "groups", "guessed", "links", "warnings" (a
    machine that could not be reached, a source folder not there) and "problems" (what failed: the command exits 1).

    Besides the archive's own files (each source's copy, versions/, .incoming/, catalog.sqlite, sources.json and
    by-group/), it runs ssh and rsync to each machine with "ssh" (fetch), may call the model through `guesser`, reads
    and writes the caches PARSED and GUESSES (the latter through the default guesser), takes LOCK, so runs go one at
    a time, and appends a line to LOG. `live`: the running sessions' ids (sources.live_session_ids when None).
    `guesser`: the model's guess (model_guesser with the Time page's settings when None). `logins`: machine_sources'.
    `names`: catalog's. Raises ArchiveError when the archive folder does not exist: it is never made here, so an
    unmounted disk cannot fill another disk instead."""
    now = now or time.time()
    root = Path(os.path.expanduser(cfg["archive"]))
    if not root.is_dir():
        raise ArchiveError("the archive folder %s does not exist: is its disk mounted? It is never made here; make it "
                           "yourself, or name another in %s" % (root, CONFIG))
    stamp = time.strftime("%Y-%m-%dT%H%M%S", time.localtime(now))
    report = {"archive": str(root), "copied": {}, "warnings": [], "problems": []}
    with sources.file_lock(LOCK):
        manifest = sources.read_json(root / MANIFEST, {})
        remote = {m["name"] for m in cfg["machines"] if m.get("ssh")}
        for m in cfg["machines"]:
            srcs = machine_sources(m, logins)
            if m.get("ssh"):
                incoming = root / INCOMING / m["name"]
                folders = [(s, incoming / s["name"]) for s in srcs]
                # first whatever a run that stopped part-way left in incoming/, so rsync never writes over a version
                # the archive has not kept yet
                _copy_sources(m, [(s, f) for s, f in folders if f.is_dir()], root, stamp, manifest, report)
                warning, problems = fetch(m, srcs, incoming, cfg, run)
                report["problems"] += problems
                if warning:
                    report["warnings"].append(warning)
                    continue
            else:
                folders = [(s, Path(s["path"])) for s in srcs]
            _copy_sources(m, folders, root, stamp, manifest, report)
        sources.write_json(root / MANIFEST, manifest, mode=0o600)
        sessions, stats = catalog(root, manifest, remote, now, cfg, report, live, guesser, names)
        report["links"] = link_tree(root, sessions)
    report["sessions"] = len(sessions)
    report["groups"] = dict(collections.Counter(
        "%s / %s [%s]" % (r["group_name"] or UNSORTED, r["subgroup"] or NO_SUBGROUP, r["group_source"])
        for r in sessions))
    report["guessed"] = stats
    log("archived: %s | %d sessions, %d frozen this run, %d asked of the model | links %s | %s" % (
        ", ".join("%s %s" % (k, counts_text(v)) for k, v in report["copied"].items()), len(sessions), stats["frozen"],
        stats.get("asked", 0), report["links"], "; ".join(report["warnings"] + report["problems"]) or "no problems"))
    return report


def counts_text(c):
    """archive_file's counts for one source as "grown=3 new=1", "empty" for none."""
    return " ".join("%s=%s" % (k, v) for k, v in sorted(c.items()) if k != "errors") or "empty"


def _copy_sources(machine, folders, root, stamp, manifest, report):
    for s, folder in folders:
        key = "%s/%s" % (machine["name"], s["name"])
        if not folder.is_dir():
            report["warnings"].append("%s: %s is not there" % (key, s["origin"]))
            continue
        counts = sync_tree(folder, root / key, root / VERSIONS / stamp / key)
        if counts.get("failed"):
            report["problems"].append("%s: not copied: %d (%s)" % (key, counts["failed"], "; ".join(counts["errors"])))
        prev = report["copied"].get(key, {})
        report["copied"][key] = {k: prev.get(k, 0) + v for k, v in counts.items() if k != "errors"}
        manifest[key] = {"machine": machine["name"], "cli": s["cli"], "login": s["login"], "origin": s["origin"]}


def catalog(root, manifest, remote, now, cfg, report, live=None, guesser=None, names=None):
    """Read every archived session (the parse cache PARSED is read on from where it stopped), group it (decide), and
    store it in <root>/catalog.sqlite. (the catalog's sessions, decide's stats); what goes wrong is added to
    archive()'s `report`. `names`: {session id: name} for sessions whose transcripts name none; when None, the
    mailroom's and the running sessions' (timesources.session_names). When those cannot be read, that is a problem,
    and each session whose transcript names none keeps the name the catalog has for it."""
    entries = scan(root, manifest)
    mains = [(str(Path(root) / e["path"]), PARSERS[manifest[e["key"]]["cli"]]) for e in entries if e["main"]]
    parsed = {str(Path(p).relative_to(root)): s for p, s in sources.read_appended(mains, 0, PARSED, log).items()}
    names_read = True
    if names is None:
        try:
            names = timesources.session_names(sources.MAIL_DB)
        except (sqlite3.Error, OSError) as e:
            names, names_read = {}, False
            report["problems"].append("session names could not be read from the mailroom, so each session keeps the "
                                      "name the catalog has: %s" % sources.error_text(e))
    records = assemble(entries, parsed, manifest, remote, names)
    con = open_catalog(Path(root) / CATALOG)
    try:
        prior = {r["id"]: dict(r) for r in con.execute("select id, name, %s from sessions" % ", ".join(GROUP_FIELDS))}
        if not names_read:
            for sid, r in records.items():
                r["name"] = r["name"] or (prior.get(sid) or {}).get("name") or ""
        live = sources.live_session_ids() if live is None else live
        guesser = guesser or model_guesser(timeuse.config())
        grouped, stats = decide(records, prior, live, groups.history(), groups.layout(), guesser, now,
                                cfg["end_after_hours"] * 3600, cfg["guess_per_run"])
        if stats.get("warning"):
            report["warnings"].append(stats["warning"])
        write_catalog(con, records, grouped, now)
        return rows(con), stats
    finally:
        con.close()


# ------------------------------------------------------------------ finding sessions

def matching(sessions, group=None, subgroup=None, since=None, query=None):
    """The sessions in `group` (Unsorted: those with none) and `subgroup` ((none): those with none), names compared
    without case; active on or after `since` (epoch seconds); and whose id, name, title, folder or project hold every
    word of `query`, without case."""
    words = (query or "").lower().split()
    out = []
    for r in sessions:
        if group and (r["group_name"] or UNSORTED).lower() != group.lower():
            continue
        if subgroup and (r["subgroup"] or NO_SUBGROUP).lower() != subgroup.lower():
            continue
        if since is not None and (r["last_activity"] or 0) < since:
            continue
        hay = " ".join(str(r.get(k) or "") for k in ("id", "name", "title", "cwd", "project")).lower()
        if any(w not in hay for w in words):
            continue
        out.append(r)
    return out


def listing(cfg, **filters):
    """The catalog's sessions that match `filters` (matching), each with "paths": its transcripts' absolute paths.
    Raises ArchiveError when the archive has no catalog yet."""
    root = Path(os.path.expanduser(cfg["archive"]))
    path = root / CATALOG
    if not path.exists():
        raise ArchiveError("no catalog at %s yet: run agent-board transcripts archive" % path)
    con = sources.ro_sqlite(path)
    try:
        found = matching(rows(con), **filters)
    finally:
        con.close()
    return [dict(r, paths=[str(root / f) for f in r["files"]]) for r in found]
