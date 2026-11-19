"""Cheap readers for everything the board shows, and the helpers every tool shares (running a command, the board's
own logs and state files, mailing the owner). Nothing here writes to another tool's state.

  Claude sessions  ~/.claude/sessions/<pid>.json        live = pid alive, same process start, socket present
  Codex sessions   ~/.codex/state_5.sqlite (threads)     live = its writer lock is actually HELD (/proc/locks);
                   ~/.codex/thread-writer-locks/*.lock    a lock file alone outlives its process
  Status log       ~/.local/state/agent-status/events.jsonl, tailed incrementally
  Mailroom store   ~/.local/state/agent-mail/mailroom.sqlite, opened read-only
  Metrics          /proc/meminfo, /proc/stat, statvfs, one long-lived `nvidia-smi -l`
  Usage            `agent-usage --json`, or its cache file (read only) when agent-usage is missing or fails
"""
import contextlib
import datetime
import fcntl
import ipaddress
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import config

HOME = Path.home()
BOARD_STATE = HOME / ".local/state/agent-board"      # the board's own state: sent messages, groups, Time page
SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")   # a Claude or Codex id
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude"))
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
STATUS_LOG = Path(os.environ.get("AGENT_STATUS_DIR", HOME / ".local/state/agent-status")) / "events.jsonl"
MAIL_STATE = config.MAIL_STATE
MAIL_DB = MAIL_STATE / "mailroom.sqlite"
MAIL_CONFIG = config.MAIL_CONFIG
USAGE_CACHE = MAIL_STATE / "usage-cache.json"
BIN_DIRS = [HOME / ".local/bin", Path("/usr/local/bin"), Path("/usr/bin")]
# The PATH of a login shell: a systemd job or a detached worker needs it to find the agents' tools.
LOGIN_PATH = ":".join([str(HOME / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin", "/snap/bin"])
BOARD_BIN = Path(__file__).resolve().parent.parent / "bin"     # this checkout's commands


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


def run(cmd, timeout=60, env=None, input=None, cwd=None):
    """Run a command, in the folder `cwd` if given, and capture its text output: (exit code, stdout, stderr). Never
    raises: a command that cannot start gives 127 and the reason, one that runs over `timeout` seconds is stopped
    and gives 124."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, input=input, cwd=cwd)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %gs" % timeout
    except OSError as e:
        return 127, "", str(e)


def run_merged(cmd, stdin=None, timeout=60):
    """`run` with stdout and stderr joined and stripped: (exit code, text)."""
    code, out, err = run(cmd, timeout=timeout, input=stdin)
    return code, (out + err).strip()


class CommandError(Exception):
    """A command the board runs for the owner (the mailroom's agent-group or agent-mail) is not installed, failed, or
    printed something that is not JSON; the message says which, for the Mac to show."""


def call(name, args, run, stdin=None, parse=True):
    """The command `name` (find_bin) with `args`, `stdin` on its input: its output parsed as JSON when `parse`, else
    as stripped text. `run` is `run` or a stand-in for it. Raises CommandError when the command is not installed,
    exits non-zero (with the end of what it printed) or, when `parse`, prints something that is not JSON."""
    exe = find_bin(name)
    if not exe:
        raise CommandError("%s is not installed" % name)
    code, out, err = run([exe] + list(args), timeout=60, input=stdin)
    if code != 0:
        raise CommandError((err or out).strip()[-500:] or "%s exited %d" % (name, code))
    if not parse:
        return out.strip()
    try:
        return json.loads(out)
    except ValueError:
        raise CommandError("%s printed something that is not JSON: %s" % (name, out.strip()[:200])) from None


def agent_send_argv(cli, sid):
    """The command that types text (given on its stdin) into a live agent. By its full id, but not --exact-id:
    that refuses when a dead login-move leftover shares the id (agent-send treats them as ambiguous), while plain
    resolution picks the live one; a UUID cannot match a name. Raises RuntimeError without agent-send."""
    exe = find_bin("agent-send")
    if not exe:
        raise RuntimeError("agent-send is not installed")
    return [exe, "--raw", "--cli", cli, sid]


def detached_argv(unit, command, env, properties):
    """systemd-run --user for `command` as the transient unit `unit`: it lives in the user manager, not in the SSH
    session or timer that asked, with the login PATH plus `env` ({name: value}) and the unit `properties`."""
    return (["systemd-run", "--user", "--collect", "--unit", unit, "--setenv=PATH=%s" % LOGIN_PATH]
            + ["--setenv=%s=%s" % kv for kv in env.items()] + ["--property=%s" % p for p in properties] + list(command))


def ro_sqlite(path):
    """A read-only connection, with sqlite3.Row rows, to another tool's existing SQLite database. Raises
    FileNotFoundError when there is no file (nothing is created) and sqlite3.Error when it cannot be read."""
    if not Path(path).exists():
        raise FileNotFoundError("no database at %s" % path)
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2, check_same_thread=False)
        con.execute("select 1 from sqlite_master limit 1")
    except sqlite3.OperationalError:
        # a WAL database that SQLite before 3.22 cannot read on a read-only connection: open it normally and refuse
        # writes (query_only), though SQLite may then still checkpoint its WAL file
        if not _is_wal(path):
            raise
        con = sqlite3.connect(str(path), timeout=2, check_same_thread=False)
        con.execute("pragma query_only=1")
    con.row_factory = sqlite3.Row
    return con


def _is_wal(path):
    """Whether a SQLite file is in WAL mode: its header's write and read versions (bytes 18 and 19) are 2."""
    with open(path, "rb") as fh:
        return fh.read(20)[18:20] == b"\x02\x02"


def error_text(e):
    """An exception as one line for a board field or the snapshot's problems: "Type: message", at most 300 long."""
    return ("%s: %s" % (type(e).__name__, e))[:300]


class Periodic:
    """A value that load() reads, read again when maybe_refresh() finds the last read started over `interval`
    seconds ago, or is forced. A read runs in a background thread, so a poll never waits for it, or in the caller's
    thread when `sync` is set (a one-shot snapshot); one runs at a time. Until the first read ends, the value is
    `initial`. A read that raises leaves the value as it was and puts error_text() in `error`, which the next good
    read clears; `checked_at` is when the last read ended (None before)."""

    def __init__(self, load, interval, initial, sync):
        self.load, self.interval, self.sync = load, interval, sync
        self.value, self.error, self.checked_at = initial, None, None
        self.at, self.busy = 0, False          # when the last read started; whether one is running
        self.lock = threading.Lock()

    def refresh(self, *args):
        """Read now with load(*args), in this thread, unless a read is running already."""
        with self.lock:
            if self.busy:
                return
            self.busy = True
        try:
            self.value, self.error = self.load(*args), None
        except Exception as e:      # the board stays up on the last value; `error` says why it is not newer
            self.error = error_text(e)
        finally:
            self.checked_at, self.busy = time.time(), False

    def maybe_refresh(self, now=None, force=False, args=()):
        """The current value, after starting a read with load(*args) when one is due or `force` is set."""
        now = now or time.time()
        if force or now - self.at > self.interval:
            self.at = now
            if self.sync:
                self.refresh(*args)
            else:
                threading.Thread(target=self.refresh, args=args, daemon=True).start()
        return self.value

    def invalidate(self):
        """The next maybe_refresh() reads (a manual refresh)."""
        self.at = 0


# ------------------------------------------------------------------ processes
def proc_start(pid):
    try:
        with open("/proc/%d/stat" % int(pid)) as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def boot_time():
    """Epoch seconds when this machine booted (/proc/stat btime), or None where there is no /proc."""
    try:
        with open("/proc/stat") as fh:
            return next(float(line.split()[1]) for line in fh if line.startswith("btime "))
    except (OSError, ValueError, StopIteration):
        return None


def process_started_at(pid):
    """Epoch seconds when a live process started, or None. A resumed Codex thread keeps its creation time, so
    this is the only way to tell that its process began after something."""
    ticks = proc_start(pid) if pid else None
    btime = boot_time()
    return btime + int(ticks) / os.sysconf("SC_CLK_TCK") if ticks and btime is not None else None


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


# ------------------------------------------------------------------ the SSH connection we serve
GONE_TIMEOUTS = 8       # consecutive retransmission timeouts: ~50 s of a peer acknowledging nothing


def _proc_tcp_addr(ip, port):
    """How /proc/net/tcp{,6} writes an address: each 32-bit word of the address in host (little-endian) order."""
    b = ip.packed
    return "".join("%08X" % int.from_bytes(b[i:i + 4], "little") for i in range(0, len(b), 4)) + ":%04X" % port


def ssh_peer_gone(conn=None, tables=("/proc/net/tcp", "/proc/net/tcp6")):
    """Has the SSH client this process serves (SSH_CONNECTION) stopped acknowledging data? A Mac that sleeps or
    drops off the network never closes its connection, and with the kernel's 15 retries the stream would keep
    writing into it for ~16 minutes. Here: the connection still has data in flight and the kernel has already
    timed out GONE_TIMEOUTS retransmissions in a row (an awake Mac acknowledges within a round trip). False when
    unsure: not over SSH, or the socket is not in the kernel's tables (e.g. an SSH server that runs in user space)."""
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
                        return int(f[4].split(":")[0], 16) > 0 and int(f[6], 16) >= GONE_TIMEOUTS
        except (OSError, ValueError):
            continue
    return False


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


MAIL_STUCK_AFTER = 120          # an agent-mail send/reply blocked on its input this long never gets its text
MAIL_SLOW_AFTER = 900           # ...and one still running this long, whatever it waits on, is not going out


def _mail_subject(argv):
    for i, x in enumerate(argv):
        if x == "--subject" and i + 1 < len(argv):
            return argv[i + 1]
        if x.startswith("--subject="):
            return x.split("=", 1)[1]
    return "reply %s" % argv[3] if len(argv) > 3 and argv[2] == "reply" else ""


def stuck_mail(pids, procs=None, now=None, proc="/proc", stuck_after=MAIL_STUCK_AFTER):
    """{agent pid: [{"pid", "age", "subject", "why"}]}: an `agent-mail send|reply` run by an agent (anywhere below
    its process) that is not going out. why "input": blocked reading a socket on its input for `stuck_after` s.
    A Claude Code command holding a heredoc gets the Bash tool's socket as its input, which never ends: an
    agent-mail waiting there for its body never sends, while the tile would show only "background". why "slow":
    still running after MAIL_SLOW_AFTER s."""
    procs = proc_table(proc) if procs is None else procs
    now = now or time.time()
    want = {int(p) for p in pids if p}
    out = {}
    for q, (_, argv) in procs.items():
        if len(argv) < 3 or os.path.basename(argv[1]) != "agent-mail" or argv[2] not in ("send", "reply"):
            continue
        agent, up = None, q
        for _ in range(12):                      # its agent: the nearest ancestor that is one
            up = procs.get(up, (None,))[0]
            if up in want:
                agent = up
                break
            if not up:
                break
        if agent is None:
            continue
        started = process_started_at(q)
        age = now - started if started else 0
        try:
            with open("%s/%d/wchan" % (proc, q)) as fh:
                wchan = fh.read().strip()
            stdin = os.readlink("%s/%d/fd/0" % (proc, q))
        except OSError:
            wchan, stdin = "", ""
        if age >= stuck_after and wchan == "unix_stream_data_wait" and stdin.startswith("socket:"):
            why = "input"
        elif age >= MAIL_SLOW_AFTER:
            why = "slow"
        else:
            continue
        out.setdefault(agent, []).append({"pid": q, "age": round(age), "subject": _mail_subject(argv)[:120], "why": why})
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


def mail_config(path=None):
    """The mailroom's config object (at `path`, default MAIL_CONFIG, which is config.MAIL_CONFIG); {} when the file
    does not exist. A file that is not a JSON object raises ValueError: a damaged config must not quietly become the
    defaults. The owner's name and addresses are read from it through config.py (owner_addresses, owner)."""
    return load_config({}, path or MAIL_CONFIG)


def claude_accounts(path=None):
    """The mailroom's usage.claude_accounts: {Claude login name: its settings}, in the config's order; {} when the
    config has none."""
    return (mail_config(path).get("usage") or {}).get("claude_accounts") or {}


def claude_routing(path=None):
    """(Claude login names in the mailroom's usage.order, min_remaining) with the mailroom's defaults: without an
    order, the configured logins in config order (default: the one login named "default")."""
    u = mail_config(path).get("usage") or {}
    accts = list(claude_accounts(path) or {"default": None})
    order = [t[7:] for t in (u.get("order") or ["claude:" + n for n in accts]) if isinstance(t, str)
             and t.startswith("claude:") and t[7:] in accts]
    return order, dict(ROUTING_MIN_REMAINING, **(u.get("min_remaining") or {}))


def claude_logins(path=None):
    """[(account name, config dir)] for every Claude login in the mailroom's usage config; the default
    login (config_dir null) is always ~/.claude, even when this process runs under another login's
    CLAUDE_CONFIG_DIR. Without config, just the default."""
    accts = claude_accounts(path)
    out, seen = [], set()
    for name, a in accts.items():
        d = Path(os.path.expanduser(a.get("config_dir"))) if isinstance(a, dict) and a.get("config_dir") else DEFAULT_CLAUDE_HOME
        if d.resolve() not in seen:
            seen.add(d.resolve())
            out.append((name, d))
    if DEFAULT_CLAUDE_HOME.resolve() not in seen:
        out.insert(0, ("default", DEFAULT_CLAUDE_HOME))
    return out


def login_labels(path=None):
    """{Claude login name: its label in the mailroom's usage config, without the part in brackets}, for the logins
    that have one ("Work seat (~/.claude-work)" gives "Work seat")."""
    accts = claude_accounts(path)
    return {name: a["label"].split("(")[0].strip() for name, a in accts.items()
            if isinstance(a, dict) and isinstance(a.get("label"), str) and a["label"].split("(")[0].strip()}


def login_config(home):
    """The .claude.json of the Claude login whose folder is `home`: the default login's sits beside ~/.claude, in the
    home folder; another login's is inside its folder."""
    return HOME / ".claude.json" if Path(home).resolve() == DEFAULT_CLAUDE_HOME.resolve() else Path(home) / ".claude.json"


def login_account(home):
    """(account id, organisation id) the Claude login whose folder is `home` is signed in to, or None when it is
    signed out. Raises OSError or ValueError when its .claude.json cannot be read."""
    o = read_json(login_config(home), {}).get("oauthAccount") or {}
    return (o["accountUuid"], o.get("organizationUuid")) if o.get("accountUuid") else None


def mark_shared_logins(accounts, logins=None):
    """Put "same_account_as" on each Claude account of a usage reading (in place) whose login is signed in to the same
    account, in the same organisation, as an earlier login: those logins' targets; and "login_dir", its folder, with ~
    for the home folder. Such logins spend one allowance, which the reading shows as two. The default login comes
    first, so it is never the one marked (its .claude.json is not in its folder), then the others as the mailroom's
    usage.claude_accounts lists them (logins: claude_logins', read from the mailroom's config when None). Reads each
    login's .claude.json (login_account), and raises as that does."""
    logins = claude_logins() if logins is None else logins
    seen = {}
    for name, home in sorted(logins, key=lambda nh: Path(nh[1]).resolve() != DEFAULT_CLAUDE_HOME.resolve()):
        who = login_account(home)
        if who is None:
            continue
        target = usage_target({"cli": "claude", "account": name})
        acct = next((a for a in accounts if a.get("target") == target), None)
        if who in seen and acct is not None:
            acct["same_account_as"] = list(seen[who])
            acct["login_dir"] = "~/" + str(Path(home).relative_to(HOME)) if Path(home).is_relative_to(HOME) else str(home)
        seen.setdefault(who, []).append(target)


def append_log(path, msg):
    """Append one timestamped line to a run log, creating its folder. Returns the line, without its newline."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with open(path, "a") as fh:
        fh.write(line + "\n")
    return line


@contextlib.contextmanager
def file_lock(path):
    """Hold an exclusive lock on the file at `path` (created, with its folder, if missing) for the with-block, so
    callers that share the path run it one at a time; a second caller waits for the first."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield                                # closing the file releases the lock


def append_jsonl(path, obj, unless=None):
    """Append `obj` as one JSON line: a single write under an exclusive lock on the file, so concurrent writers
    never interleave. With `unless`, a check of the lines already there (read_jsonl's list) made under the same
    lock: when it is true nothing is written. Whether the line was written. A new file is private (0600). Raises
    OSError when the file cannot be written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if unless is not None and unless(read_jsonl(path)):
            return False
        os.write(fd, (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
        return True
    finally:
        os.close(fd)                         # releases the lock


def read_jsonl(path):
    """The JSON values of a file's lines, oldest first; [] when the file does not exist. A line that is not JSON
    raises ValueError naming the file and line, except a last line without its newline: a writer may be appending
    it right now. An unreadable file raises OSError."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    lines = text.split("\n")
    out = []
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            if i == len(lines) - 1:
                continue
            raise ValueError("%s line %d is not JSON" % (path, i + 1)) from None
    return out


def read_json(path, default=None):
    """A JSON file's content, or `default` when the file does not exist. A damaged file raises: OSError when it cannot
    be read, ValueError naming it when it is not JSON, so a damaged state file never passes for a missing one. A
    cache the caller can rebuild is read with read_cache."""
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
    except ValueError as e:         # not JSON, or not even UTF-8 text
        raise ValueError("%s is not valid JSON: %s" % (path, e)) from None


def read_cache(path, default, log):
    """read_json for a cache the caller can rebuild from its sources: a damaged file is reported with log(message)
    and gives `default`, as a missing one does, so the caller starts it afresh."""
    try:
        return read_json(path, default)
    except (OSError, ValueError) as e:
        log("starting a damaged cache afresh: %s" % error_text(e))
        return default


class _Lines:
    """The complete lines of a binary file from its position on, as bytes (json.loads reads them; a parser looks for
    what it wants before decoding a line); `offset` is the byte after the last one given. A last line without its
    newline is still being written, so it ends the lines without being given."""

    def __init__(self, fh, offset):
        self.fh, self.offset = fh, offset

    def __iter__(self):
        return self

    def __next__(self):
        raw = self.fh.readline()
        if not raw.endswith(b"\n"):
            raise StopIteration
        self.offset += len(raw)
        return raw


def read_appended(paths, since, cache_path, log):
    """{path: parse's result} for each (path, parse) in `paths` whose file was modified since `since`, in that order
    (a file parse has nothing for yet, None, is left out). parse(lines, state, path) reads the lines into `state`, None
    at the start of a file, and returns it. The files are logs that only grow: one that has grown since the cache's
    last read is read on from where that read stopped, and one replaced or shrunk (another inode, or smaller) from
    its start. The cache at `cache_path` is rewritten (private) with what was read this time; a damaged one is
    reported with log(message), and every file is read again."""
    cache = read_cache(cache_path, {}, log) or {}
    out, fresh = {}, {}
    for p, parse in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_mtime < since:
            continue
        hit = cache.get(p)
        if not (isinstance(hit, dict) and hit.get("ino") == st.st_ino and 0 <= hit.get("offset", -1) <= st.st_size):
            hit = {"ino": st.st_ino, "offset": 0, "state": None}
        if hit["offset"] < st.st_size:
            with open(p, "rb") as fh:
                fh.seek(hit["offset"])
                lines = _Lines(fh, hit["offset"])
                hit = dict(hit, state=parse(lines, hit["state"], p), offset=lines.offset)
        fresh[p] = hit
        if hit["state"] is not None:
            out[p] = hit["state"]
    write_json(cache_path, fresh, mode=0o600)
    return out


def iso_ts(s):
    """Epoch seconds from an ISO 8601 time, including the "Z" that Claude, Codex and Google end theirs with (Python
    before 3.11 does not read it). Raises ValueError on anything else."""
    return datetime.datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s).timestamp()


def rfc3339(ts):
    """Epoch seconds as the RFC 3339 time in UTC that Google's APIs take ("2026-11-13T09:30:00Z"), to the second."""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config(defaults, path):
    """`defaults` overridden by the JSON object at `path`. A missing file means no overrides; one that is not a
    JSON object raises ValueError (read_json's errors otherwise), so a typo in a config never quietly turns into the
    defaults."""
    given = read_json(path, {})
    if not isinstance(given, dict):
        raise ValueError("%s must hold a JSON object" % path)
    return dict(defaults, **given)


def write_json(path, obj, mode=None):
    """Write `obj` as JSON with write_text_atomic."""
    write_text_atomic(path, json.dumps(obj, indent=1, default=str), mode)


def write_text_atomic(path, text, mode=None):
    """Write `text` atomically: a unique temporary file beside `path`, then a rename, so concurrent writers never
    collide. The file keeps its permissions; a new one is private (0600) unless `mode` says otherwise."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    if mode is None and path.exists():
        mode = path.stat().st_mode & 0o777
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def live_session_ids():
    """The ids of every Claude Code and Codex session running now."""
    return ({s["id"] for s in claude_sessions_all() if s["live"]}
            | {s["id"] for s in CodexReader().sessions() if s["live"]})


def claude_sessions_all():
    out, labels = [], login_labels()
    for account, home in claude_logins():
        for s in claude_sessions(home):
            s["account"] = account
            s["account_label"] = labels.get(account)
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
    """Codex threads with real liveness. Busy = its rollout file was written in the last BUSY_SECONDS. A threads
    table that cannot be read gives no threads for that poll, with the reason in `error`."""
    BUSY_SECONDS = 45
    RECENT_SECONDS = 86400      # a thread not live and not asked about is listed if updated this recently

    def __init__(self, home=CODEX_HOME):
        self.home = Path(home)
        self.con = None
        self.error = None

    def _db(self):
        if self.con is None:
            db = self.home / "state_5.sqlite"
            if not db.exists():
                return None
            self.con = ro_sqlite(db)
        return self.con

    def sessions(self, ids_of_interest=(), now=None):
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
        rows = {}
        try:
            con = self._db()
            if con is not None:
                want = set(live) | set(ids_of_interest)
                ph = ",".join("?" * len(want))
                where = "where archived = 0 and (updated_at >= ? %s)" % ("or id in (%s)" % ph if want else "")
                base = "id, name, title, cwd, git_branch, rollout_path, updated_at, model, reasoning_effort, created_at"
                for cols in (base + ", source, thread_source, agent_nickname", base):
                    try:
                        for r in con.execute("select %s from threads %s" % (cols, where),
                                             [int(now - self.RECENT_SECONDS)] + list(want)):
                            rows[r["id"]] = dict(r)
                        break
                    except sqlite3.OperationalError as e:
                        if "no such column" not in str(e) or cols == base:
                            raise
                        # an older Codex: retry without the subagent columns
            self.error = None
        except sqlite3.Error as e:          # locked, I/O, a stale handle: reopened next poll; the board lists it
            self.con, self.error = None, error_text(e)
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


TMUX_NO_SERVER = ("no server running", "error connecting to")      # tmux's words when no server is up


def tmux_list_panes():
    """[(pane pid, session name, pane's current directory)] for every tmux pane, from one `tmux list-panes -a`;
    [] without tmux or with no tmux server running. Raises RuntimeError when tmux fails otherwise or does not answer
    in 3 s."""
    tmux = find_bin("tmux")
    if not tmux:
        return []
    code, out, err = run([tmux, "list-panes", "-a", "-F", "#{pane_pid}\t#{session_name}\t#{pane_current_path}"],
                         timeout=3)
    if code != 0:
        if any(w in err for w in TMUX_NO_SERVER):
            return []
        raise RuntimeError("tmux list-panes exit %d: %s" % (code, err.strip()[-300:]))
    rows = []
    for line in out.splitlines():
        pid, _, rest = line.partition("\t")
        name, _, path = rest.partition("\t")
        if pid.isdigit() and name:
            rows.append((int(pid), name, path))
    return rows


def tmux_sessions(rows=None):
    """{session name: set of its panes' current directories}; rows: tmux_list_panes()'s, read now when None."""
    out = {}
    for _, name, path in tmux_list_panes() if rows is None else rows:
        out.setdefault(name, set()).add(path)
    return out


def tmux_panes(rows):
    """{pane pid: session name} for tmux_list_panes()'s rows."""
    return {pid: name for pid, name, _ in rows}


def tmux_session_for(pid, panes):
    depth = 12                  # the pane's process is at most this many ancestors up
    while pid and depth:
        if pid in panes:
            return panes[pid]
        pid = ppid(pid)
        depth -= 1
    return ""


# ------------------------------------------------------------------ status log
class EventLog:
    """Incremental tail of the agent-status log; keeps the last KEEP_SECONDS of events in memory."""
    KEEP_SECONDS = 48 * 3600

    def __init__(self, path=STATUS_LOG):
        self.path = Path(path)
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
        cutoff = now - self.KEEP_SECONDS
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
def norm_id(message_id):
    """A Message-ID as two copies of it compare: without spaces or <>, in lower case."""
    return (message_id or "").strip().strip("<>").strip().lower()


def owner_addresses(path=None):
    """The owner's addresses (mailroom config owner_email, then allowlist), lower case; [] when unconfigured."""
    return config.owner_addresses(mail_config(path))


def owner(path=None):
    """The address the board and its tools mail the owner at (config.owner_email: the mailroom config's owner_email,
    else its first allowlisted sender), in lower case; None when the config names neither."""
    return (config.owner_email(mail_config(path)) or "").lower() or None


def mail_account(path=None):
    """The agents' Gmail address (mailroom config "account"), or ""."""
    return (mail_config(path).get("account") or "").lower()


def mail_host(path=None):
    """This machine's name in the mailroom (config "host_id")."""
    return mail_config(path).get("host_id") or "desktop"


def mail_owner(subject, body):
    """Email the owner from the agents' account, unrouted (a detached job has no session for the reply to reach).
    (sent?, what went wrong)."""
    exe = find_bin("agent-mail")
    if not exe:
        return False, "agent-mail is not installed"
    to = owner()
    if not to:
        return False, "no owner address in the mailroom config (owner_email)"
    env = {k: v for k, v in os.environ.items() if not k.startswith("SSH_")}
    env["PATH"] = LOGIN_PATH
    code, out, err = run([exe, "send", "--unrouted", "--to", to, "--subject", subject], timeout=120, env=env,
                         input=body if body.endswith("\n") else body + "\n")
    return code == 0, "" if code == 0 else "agent-mail exit %d: %s" % (code, (err or out).strip()[-300:])


class MailStore:
    """Read-only view of the mailroom's routing map. Never opened for writing."""

    def __init__(self, path=MAIL_DB, owners=None, host_id=None):
        self.path = Path(path)
        self.owners = owners
        # this machine's identity in mail routing (mailroom config host_id); sessions hosted elsewhere are skipped
        self.host_id = host_id or mail_host()
        self.con = None

    def _db(self):
        if self.con is None and self.path.exists():
            self.con = ro_sqlite(self.path)
        return self.con

    def read(self, now=None, window=7 * 86400):
        """{"by_session": {(cli, sid): {"sent": [...], "inbound": [...], "reviews": [...]?}}, "held": [...], "ok"};
        "ok" is False without a store, and also when it could not be read, with the sqlite error in "error"."""
        now = now or time.time()
        owners = self.owners if self.owners is not None else owner_addresses()
        con = self._db()
        out = {"by_session": {}, "held": [], "ok": con is not None}
        if con is None:
            return out
        since = int(now - window)
        try:
            tags, names = {}, {}
            for r in con.execute("select tag, cli, session_id, session_name, origin_host from sessions"):
                if (r["origin_host"] or self.host_id) != self.host_id:     # no origin: an older, local row
                    continue
                tags[r["tag"]] = (r["cli"], r["session_id"])
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
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e):
                    raise
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
    # --no-optional-locks: `git status` must not refresh the agents' own index, or their commits can hit index.lock
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", cwd] + list(args), capture_output=True, text=True,
                           timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def repo_of_common(common, toplevel=""):
    """A repo's name from its git common dir: the main checkout's folder, so a linked worktree such as
    admin-todo-12 is named admin."""
    return os.path.basename(os.path.dirname(os.path.abspath(common))) or os.path.basename(toplevel)


_names_beside = {}         # folder -> the names in it, read once per process


def repo_name(cwd):
    """The repo a folder belongs to, or "" when it belongs to none. A folder that exists asks git. One that no
    longer does (a removed worktree) is matched by agent-spawn's naming, <repo>-<branch>: the longest git repo
    beside it, at the nearest level where one matches, so .../admin-todo-12 is admin and
    .../recipe-site-worktrees/7-x/site is recipe-site."""
    if not cwd:
        return ""
    if os.path.isdir(cwd):
        common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common:
            return repo_of_common(common)
    p = Path(cwd)
    for comp in [p] + list(p.parents)[:-1]:
        parent = comp.parent
        if parent not in _names_beside:
            try:
                _names_beside[parent] = os.listdir(parent)
            except OSError:
                _names_beside[parent] = []
        # only the names it could be named after are looked at, so a folder of many entries costs one listing
        hits = [r for r in _names_beside[parent] if (comp.name == r or comp.name.startswith(r + "-"))
                and _has_git(parent / r)]
        if hits:
            return max(hits, key=len)
    return ""


def _has_git(folder):
    """Whether `folder` holds a .git; False when that cannot be looked for (no permission to enter it)."""
    try:
        return (folder / ".git").exists()
    except OSError:
        return False


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
    repo = repo_of_common(common, toplevel)
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
    """Every PR per GitHub repo, from one `gh pr list` per repo every `ttl` seconds, in the background (in the
    caller's thread when `sync` is set: a one-shot snapshot). Without gh there is no PR column. A failed gh keeps
    the repo's last table and is tried again a minute later; until it succeeds, `error` says why."""

    RETRY = 60

    def __init__(self, ttl=300, runner=None, sync=False):
        self.ttl = ttl
        self.runner = runner or self._gh
        self.sync = sync
        self.by_slug = {}      # slug -> (ts, {branch: pr})
        self.errors = {}       # slug -> why its last gh call failed
        self.pending = set()
        self.lock = threading.Lock()

    @staticmethod
    def _gh(slug):
        """The repo's PRs as `gh pr list --json` lists them; None when gh is not installed. Raises RuntimeError when
        gh fails (logged out, rate limited, no answer in 30 s) and ValueError when its output is not JSON."""
        gh = find_bin("gh")
        if not gh:
            return None
        code, out, err = run([gh, "pr", "list", "--repo", slug, "--state", "all", "--limit", "100", "--json",
                              "number,state,headRefName,headRefOid,baseRefName,url,isDraft,mergedAt,title"], timeout=30)
        if code != 0:
            raise RuntimeError("gh pr list exit %d: %s" % (code, (err or out).strip()[-300:]))
        return json.loads(out)

    @property
    def error(self):
        """Each repo's gh failure as "slug: error", joined by "; ", or None."""
        with self.lock:
            return "; ".join("%s: %s" % kv for kv in sorted(self.errors.items())) or None

    def _fetch(self, slug, now=None):
        err = None
        try:
            rows = self.runner(slug)
        except Exception as e:      # the repo keeps its last table; `error` shows why it is not newer
            rows, err = None, error_text(e)
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
            if err:
                self.errors[slug] = err
            else:
                self.errors.pop(slug, None)
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


REPOS_ROOT = config.repos_root()        # where repos and worktrees live (AGENT_BOARD_REPOS, else the mailroom config)


def list_worktrees(roots=None):
    """Every linked worktree of every main repo directly under the roots (default: the configured repos_root),
    wherever the worktree itself lives: [{path, repo, main, branch, detached, locked}]. Missing (prunable) ones
    are skipped."""
    out = []
    for root in roots or [REPOS_ROOT]:
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
        self.burst = False         # the next poll fetches every stale cwd, not just `budget` of them

    def invalidate(self):
        """Next poll refetches every cwd (a manual refresh)."""
        self.cache = {c: (0, f) for c, (_, f) in self.cache.items()}
        self.burst = True

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
                [smi, "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
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
                 "util": _num(f[3]), "temp": _num(f[4])}
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
    """Plan usage: `agent-usage --json`, or its cache file when agent-usage is missing or fails (the failure in
    the reading's "error"). Nothing here writes the cache or runs a probe. A reading is taken every `interval`
    seconds, as a Periodic (in the background unless `sync`). `annotate`, when given, is called with each reading's
    accounts and adds to them in place (the board passes bin/agent-board's annotate_usage, which says what it adds);
    what it raises goes in the reading's "error"."""

    def __init__(self, cache_path=USAGE_CACHE, interval=300, runner=None, annotate=None, sync=False):
        self.cache_path = Path(cache_path)
        self.runner = runner or agent_usage
        self.annotate = annotate
        # show the last cached reading at once; the first real read happens in the background.
        # The cache is another tool's file: nothing in it may stop the collector from starting.
        try:
            cached = self.from_cache()
        except Exception:
            cached = None
        first = self._annotated(cached or {"source": "none", "accounts": [], "observed_at": None, "error": None})
        self.timer = Periodic(self._read, interval, first, sync)

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

    def _read(self, force):
        """One reading: agent-usage's (re-probed with `force`), else the cache's with agent-usage's failure in
        "error"; then annotated."""
        data, err = None, None
        try:
            raw = self.runner(force)
            if raw is not None:
                data = {"source": "agent-usage", "accounts": raw.get("accounts") or [],
                        "observed_at": raw.get("generated_at"), "recommendation": raw.get("recommendation"),
                        "error": None}
        except Exception as e:   # a broken probe must never take the board down
            err = error_text(e)
        if data is None:
            data = self.from_cache() or {"source": "none", "accounts": [], "observed_at": None, "error": None}
            if err:
                data["error"] = err
        return self._annotated(data)

    def _annotated(self, data):
        if self.annotate is not None:
            try:
                self.annotate(data.get("accounts") or [])
            except Exception as e:      # costs only what annotate adds; the reading's "error" says why
                data["error"] = "; ".join(x for x in (data.get("error"), error_text(e)) if x)
        return data

    @property
    def data(self):
        """The current reading: {"source", "accounts", "observed_at", "error", ...}."""
        return self.timer.value

    @property
    def error(self):
        """Why the current reading is not a fresh, complete one, or None."""
        return self.timer.error or self.data.get("error")

    def maybe_refresh(self, now=None, force=False):
        """The current reading, after starting a new one when `interval` has passed or `force` is set (`force` also
        makes agent-usage probe again)."""
        return self.timer.maybe_refresh(now, force, args=(force,))

    def invalidate(self):
        """The next maybe_refresh() takes a reading, through agent-usage's own cache (no new probes)."""
        self.timer.invalidate()


def claude_home_of(sid):
    """(account, config dir) of the login holding the NEWEST copy of this session's transcript, or (None, None).
    After a login move both logins have a copy and the old one stops at the move, so first-found is wrong."""
    best = None
    for account, home in claude_logins():
        for path in Path(home).glob("projects/*/%s.jsonl" % sid):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, account, Path(home))
    return (best[1], best[2]) if best else (None, None)


def transcript_tail(path, nbytes):
    """The lines of a transcript's last `nbytes` bytes (the first may be cut). Raises OSError when unreadable."""
    with open(path, "rb") as fh:
        fh.seek(max(0, os.path.getsize(path) - nbytes))
        return fh.read().decode("utf-8", "replace").splitlines()


def last_claude_model(home, sid):
    """The model of the session's last assistant turn, from its transcript tail."""
    for path in Path(home).glob("projects/*/%s.jsonl" % sid):
        try:
            lines = transcript_tail(path, 2_000_000)
        except OSError:
            continue
        for line in reversed(lines):
            if '"model"' in line and '"assistant"' in line:
                try:
                    m = (json.loads(line).get("message") or {}).get("model")
                except ValueError:
                    continue
                if m and not m.startswith("<"):
                    return m
    return None


def last_turn_failed(sid):
    """Whether the session's conversation ends on Claude Code's own record of a failed request (an assistant entry
    with isApiErrorMessage), from its newest transcript's tail; None when that can't be read. A StopFailure hook
    also fires when a side request fails after a turn finished normally (such as a 429 "Usage credits are required
    for long context requests" a second after an agent's final answer), and that leaves no such record."""
    _, home = claude_home_of(sid)
    paths = sorted(Path(home).glob("projects/*/%s.jsonl" % sid), key=lambda p: p.stat().st_mtime) if home else []
    if not paths:
        return None
    try:
        lines = transcript_tail(paths[-1], 2_000_000)
    except OSError:
        return None
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") in ("assistant", "user") and not d.get("isMeta"):
            return d["type"] == "assistant" and bool(d.get("isApiErrorMessage"))
    return None


def codex_settings(tid):
    con = CodexReader()._db()
    if con is None:
        return None, None
    r = con.execute("select model, reasoning_effort from threads where id = ?", (tid,)).fetchone()
    return (r["model"], r["reasoning_effort"]) if r else (None, None)


def is_live(agent):
    if agent["cli"] == "claude":
        return any(s["id"] == agent["id"] and s["live"] for s in claude_sessions_all())
    return any(s["id"] == agent["id"] and s["live"] for s in CodexReader().sessions(ids_of_interest=[agent["id"]]))


def cmdline(pid, proc="/proc"):
    try:
        with open("%s/%d/cmdline" % (proc, int(pid)), "rb") as fh:
            return [a.decode("utf-8", "replace") for a in fh.read().split(b"\x00") if a]
    except (OSError, ValueError, TypeError):
        return []


def model_effort(agent):
    """(model, effort) as the agent runs now: its process arguments first, else its transcript / thread."""
    argv = cmdline(agent.get("pid")) if agent.get("pid") else []
    model = effort = None
    for i, a in enumerate(argv):
        if a in ("--model", "-m") and i + 1 < len(argv):
            model = argv[i + 1]
        elif a == "--effort" and i + 1 < len(argv):
            effort = argv[i + 1]
        elif a.startswith("model_reasoning_effort="):
            effort = a.split("=", 1)[1].strip("'\"")
    if agent["cli"] == "codex" and not (model and effort):
        m, e = codex_settings(agent["id"])
        model, effort = model or m, effort or e
    if agent["cli"] == "claude" and not model:
        _, home = claude_home_of(agent["id"])
        model = last_claude_model(home, agent["id"]) if home else None
    return model, effort


def agent_usage(refresh, run=run):
    """`agent-usage --json` (with --refresh: re-probe now; else it answers from its 2-minute cache and never spends
    quota), parsed; None when agent-usage is not installed. Raises RuntimeError when it fails."""
    b = find_bin("agent-usage")
    if not b:
        return None
    code, out, err = run([b, "--json"] + (["--refresh"] if refresh else []), timeout=90)
    if code != 0:
        raise RuntimeError((err or out).strip()[-300:] or "agent-usage exit %d" % code)
    return json.loads(out)


def read_usage(run=run):
    """The accounts agent-usage reports; RuntimeError when it is not installed or fails."""
    got = agent_usage(False, run)
    if got is None:
        raise RuntimeError("agent-usage is not installed")
    return got.get("accounts") or []


SAME_RESET = 3600      # one window's resets_at moves by a second between readings; a new window's by hours or days


def same_reset(a, b):
    """Whether two usage readings' reset times (resets_at, or None) name the same window."""
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= SAME_RESET


def model_window(model):
    """The windows() key of a model's own weekly allowance: "model:<name>", in lower case. mark_window_keys sends
    these keys to the Mac with each reading."""
    return "model:%s" % (model or "").lower()


def windows(acct):
    """{"session"|"weekly"|model_window(name): window} for one account."""
    out = {}
    for w in acct.get("windows") or []:
        k = model_window(w.get("model")) if w.get("kind") == "model_weekly" else w.get("kind")
        if k:
            out[k] = w
    return out


def mark_window_keys(accounts):
    """Put "key", its windows() key, on each window of a usage reading's accounts (in place): the name the Mac
    asks the usage_split action for (agentboard/allowance.py)."""
    for a in accounts:
        for key, w in windows(a).items():
            w["key"] = key


def left_pct(w):
    """Percent left in a usage window: its remaining_pct, else 100 minus used_pct (none given: all of it)."""
    if w.get("remaining_pct") is not None:
        return float(w["remaining_pct"])
    return 100.0 - float(w.get("used_pct") or 0)


def usage_target(agent):
    """The usage account an agent spends: "codex", or "claude:<login>" (no login named: the mailroom's default login,
    "default")."""
    if agent.get("cli") == "codex":
        return "codex"
    return "claude:%s" % (agent.get("account") or "default")


def mailroom_root():
    """The mailroom checkout whose agentmail package and bin/ the board borrows (config.mailroom_dir: the kit's
    sibling mailroom/, or AGENT_MAILROOM_DIR), as a string; None when there is none with an agentmail package. The
    kit's commands on PATH are wrappers, so the checkout is not found through them."""
    d = config.mailroom_dir()
    return str(d) if d else None


def mailroom(module):
    """A module of the mailroom package (read-only use, as agent-close's own functions are used). Raises
    RuntimeError when the mailroom isn't installed, and whatever importing the module raises."""
    import importlib
    import sys
    root = mailroom_root()
    if not root:
        raise RuntimeError("the mailroom isn't installed (no agentmail/ in the kit's mailroom/ or AGENT_MAILROOM_DIR)")
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("agentmail." + module)


def optional_mailroom(module):
    """mailroom(module), or None when the mailroom isn't installed (mailroom_root) or is older than that module. A
    module that is there but fails to import raises."""
    if mailroom_root() is None:
        return None
    try:
        return mailroom(module)
    except ModuleNotFoundError as e:
        if e.name != "agentmail." + module:
            raise
        return None
