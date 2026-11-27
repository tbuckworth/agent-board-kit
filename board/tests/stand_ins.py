"""Stand-ins the test modules share for the commands the board finds on PATH (sources.find_bin), for the mailroom
checkout it borrows modules from (sources.mailroom_root: config.mailroom_dir, AGENT_MAILROOM_DIR), for the mailroom's
modules (sources.mailroom), and for the desktop's keyring where the phone page reaches it."""
import contextlib
import fcntl
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _kitenv  # noqa: E402,F401  (scratch config with an example owner, before agentboard reads any)
from agentboard import sources  # noqa: E402

MAILROOM = Path(tempfile.mkdtemp())          # a mailroom checkout: bin/agent-spawn beside an (empty) agentmail package
(MAILROOM / "bin").mkdir()
(MAILROOM / "agentmail").mkdir()
(MAILROOM / "bin" / "agent-spawn").write_text("")
(MAILROOM / "bin" / "agent-spawn").chmod(0o755)       # found on PATH too, by a test's `agent-board` (test_askbox)


def find_bin(name):
    """Every command is "/bin/<name>", except agent-spawn, which sits in MAILROOM; the tests stand in for the
    mailroom's modules (another component, with its own tests) themselves."""
    return str(MAILROOM / "bin" / name) if name == "agent-spawn" else "/bin/" + name


def use(test):
    """For one test: find_bin above, and MAILROOM as the mailroom checkout (AGENT_MAILROOM_DIR), both put back by the
    test's cleanup."""
    saved_find, saved_dir = sources.find_bin, os.environ.get("AGENT_MAILROOM_DIR")
    sources.find_bin = find_bin
    os.environ["AGENT_MAILROOM_DIR"] = str(MAILROOM)

    def undo():
        sources.find_bin = saved_find
        if saved_dir is None:
            os.environ.pop("AGENT_MAILROOM_DIR", None)
        else:
            os.environ["AGENT_MAILROOM_DIR"] = saved_dir
    test.addCleanup(undo)


class Command:
    """Stands in for a command the board runs through sources.run (agent-group, agent-mail): answers each call with
    the next (code, stdout, stderr), the last one again once the others are used, and keeps (argv, stdin) of each."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers) or [(0, "", "")], []

    def __call__(self, cmd, timeout=60, env=None, input=None, cwd=None):
        self.calls.append((cmd, input))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class Popen:
    """Stands in for subprocess.Popen where the board starts agent-orchestrate (actions.orchestrate), which would run a
    paid `claude -p` event: nothing runs, and each call is kept as {"cmd", "kw", "stdin"}: its argv, its keyword
    arguments and the bytes written to its stdin. Each process it starts is still running when waited for, or, given
    `exits`, has ended at once with that exit code after writing `says` to its stdout (as systemd-run does when it
    cannot reach the user bus). With `closes_input`, its stdin is closed already, so a write to it raises
    BrokenPipeError and nothing is kept."""

    def __init__(self, exits=None, says=b"", closes_input=False):
        self.calls, self.exits, self.says, self.closes_input = [], exits, says, closes_input

    def __call__(self, cmd, **kw):
        call = {"cmd": cmd, "kw": kw, "stdin": b""}
        self.calls.append(call)
        if self.exits is not None:
            kw["stdout"].write(self.says)
        return _Process(call, self.exits, self.closes_input)


class _Process:
    """A process Popen started: its stdin keeps what is written to it in its call (or refuses it, with
    `closes_input`), and wait answers `exits`, or, with None, times out at once, as a process still running does."""

    def __init__(self, call, exits, closes_input):
        self.stdin, self.call, self.exits, self.closes_input = self, call, exits, closes_input

    def write(self, data):
        if self.closes_input:
            raise BrokenPipeError(32, "Broken pipe")
        self.call["stdin"] += data

    def close(self):
        pass

    def wait(self, timeout):
        if self.exits is None:
            raise subprocess.TimeoutExpired(self.call["cmd"], timeout)
        return self.exits


# keyring-unlock's stand-in (Keyring), run as `python3 -I` on this text with FOLDER filled in. As the core does, it
# makes itself undumpable before it reads (unless told not to), after waiting to be let go when it is held, and it
# keeps in its folder, for each run: its process id as it starts, its arguments, the names in its environment (never
# their values, which hold the tests' own credentials), whether its stdin held anything before it was undumpable and
# whether it is a socket, what came on its stdin, and whether that is anywhere in its environment. Then it answers.
UNLOCK_SCRIPT = """import ctypes, json, os, select, stat, sys, time
d = FOLDER


def put(name, data):
    with open(os.path.join(d, name), "wb") as f:
        f.write(data)


how = json.load(open(os.path.join(d, "answer.json")))
runs = os.path.join(d, "runs")
n = (int(open(runs).read()) if os.path.exists(runs) else 0) + 1
put("runs", str(n).encode())
put("pid-%d" % n, str(os.getpid()).encode())
ends = time.monotonic() + 60
while how["hold"] and not os.path.exists(os.path.join(d, "go-%d" % n)) and time.monotonic() < ends:
    time.sleep(0.01)
early = bool(select.select([0], [], [], 0)[0])
if how["hardens"]:
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)              # PR_SET_DUMPABLE 0, as lockdown.harden does
put("run-%d.json" % n, json.dumps({"argv": sys.argv[1:], "env": sorted(os.environ), "early": early,
                                    "stdin_socket": stat.S_ISSOCK(os.fstat(0).st_mode)}).encode())
got = sys.stdin.buffer.read()
put("stdin-%d" % n, got)
put("in-env-%d" % n, b"yes" if got and any(got in v.encode() for v in os.environ.values()) else b"no")
if how["hangs"]:
    time.sleep(60)
sys.stdout.write(how["line"] + "\\n")
sys.exit(how["code"])
"""


DEFAULT_KEYRING = "/org/freedesktop/secrets/collection/Default_5fkeyring"     # the browser's, on an example desktop
LOGIN_KEYRING = "/org/freedesktop/secrets/collection/login"


def collections(default, login):
    """The watched keyrings as keyring.status and keyring-unlock give them on an example desktop: the browser's
    ("default", the "Default keyring") and "Login", each locked as given."""
    return {"default": {"path": DEFAULT_KEYRING, "label": "Default keyring", "locked": default, "error": None},
            "login": {"path": LOGIN_KEYRING, "label": "Login", "locked": login, "error": None}}


class Keyring:
    """Stands in for the desktop's login keyring where the phone page reaches it (mailweb.Guard's read_keyring and
    unlock_command), as the real one is the owner's own. read() answers what keyring.status would, `state`, which the test
    sets (unlocked until it does), and counts each read in `reads`. `command` runs a Python script in `folder`
    (UNLOCK_SCRIPT) in place of `agent-board keyring-unlock`, which keeps what each run was given there (runs()), then
    answers as answer() last set."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.script = self.folder / "keyring-unlock.py"
        self.script.write_text(UNLOCK_SCRIPT.replace("FOLDER", repr(str(self.folder))))
        self.command = [sys.executable, "-I", str(self.script)]
        self.lock("unlocked")
        self.reads = 0
        self.answer(3, {"result": "error", "locked": None, "error": "the test did not say what keyring-unlock answers"})

    def read(self):
        self.reads += 1
        return dict(self.state)

    def lock(self, state="locked", error=None, login=None):
        """The keyrings as status() would read them next, as on an example desktop: the browser's ("default", the
        "Default keyring") `state`, "locked", "unlocked", or "unknown" with `error`, and "Login" locked as `login` says
        (by default as the browser's is)."""
        locked = {"locked": True, "unlocked": False}.get(state)
        cols = collections(locked, locked if login is None else login)
        if state == "unknown":
            cols["default"] = {"path": None, "label": None, "locked": None, "error": error}
        self.state = {"state": state, "locked": locked, "error": error, "boot_id": "test-boot", "collections": cols}

    def answer(self, code, said, hangs=False, hardens=True, hold=False):
        """What the next runs do: with `hold`, wait as they start, before they harden, until release() lets them go;
        make themselves undumpable when `hardens`, read their stdin, then print `said` (an object as one JSON line, or
        text as it is) and exit `code`; with `hangs`, wait a minute instead, answering nothing."""
        line = said if isinstance(said, str) else json.dumps(said)
        (self.folder / "answer.json").write_text(json.dumps({"code": code, "line": line, "hangs": hangs,
                                                             "hardens": hardens, "hold": hold}))

    def release(self, run):
        """Let run `run` (from 1), held as it started (answer's `hold`), go on."""
        (self.folder / ("go-%d" % run)).write_text("")

    def pid(self, run):
        """The process id of run `run` (from 1), once it has started; else None."""
        path = self.folder / ("pid-%d" % run)
        return int(path.read_text()) if path.exists() else None

    def runs(self):
        """Each run so far, in order: {"argv": its arguments, "env": the names in its environment, "early": whether its
        stdin held anything before it was undumpable, "stdin_socket": whether its stdin is a socket, "stdin": the bytes
        it was given (None when it was stopped before it read them), "in_env": whether they are in its environment}."""
        def read(name):
            path = self.folder / name
            return path.read_bytes() if path.exists() else None
        n = int(read("runs") or 0)
        return [dict(json.loads(read("run-%d.json" % i) or "{}"), stdin=read("stdin-%d" % i),
                     in_env=read("in-env-%d" % i) == b"yes") for i in range(1, n + 1)]


KEYS_LOCK = MAILROOM / "keys.lock"


class MailroomCommands:
    """Stands in for the mailroom's agentmail.commands (another repo's, with its own tests) where the board takes its
    keys lock (relaunch.mailroom_keys_lock): a real lock on KEYS_LOCK, which keys_taken reads."""

    @staticmethod
    @contextlib.contextmanager
    def keys_lock(wait):
        with open(KEYS_LOCK, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def keys_taken():
    """Whether MailroomCommands' keys lock is held now (a lock on the file through another open of it)."""
    with open(KEYS_LOCK, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


class MailroomAccounts:
    """Stands in for the mailroom's agentmail.accounts (another component's, with its own tests) where the board asks it
    what a claude it starts must not inherit (claude_env, CLAUDE_ENV_DROP, settings_env) and what options it takes on
    each login (login_flags, account_by_dir, fixed_model, move_model, manual), as the mailroom has them: the scrub drops
    CLAUDE_ENV_DROP's patterns and the names the logins' settings files set (SETTINGS_ENV); a login with a "settings"
    file takes --settings with it, and with a "model" runs only that one. Its settings are the config's
    (current_settings: the usage and config modules the test gives sources.mailroom)."""
    CLAUDE_ENV_DROP = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_*", "CLAUDE_CODE_SESSION_ID",
                       "CLAUDECODE", "CLAUDE_BG_*")
    SETTINGS_ENV = ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS")

    @staticmethod
    def current_settings():
        from agentboard import sources
        return sources.mailroom("usage").settings(sources.mailroom("config").load())

    @classmethod
    def settings_env(cls, s):
        return set(cls.SETTINGS_ENV)

    @classmethod
    def claude_env(cls, acct, base=None, s=None):
        drop = cls.CLAUDE_ENV_DROP + cls.SETTINGS_ENV
        env = {k: v for k, v in (os.environ if base is None else base).items()
               if not any(fnmatch.fnmatchcase(k, p) for p in drop)}
        if acct.get("config_dir"):
            env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(acct["config_dir"])
        return env

    @staticmethod
    def expand(p):
        return os.path.expanduser(p) if p else None

    @staticmethod
    def manual(acct):
        return bool((acct or {}).get("manual"))

    @staticmethod
    def fixed_model(acct):
        return (acct or {}).get("model") or None

    @classmethod
    def account_by_dir(cls, s, config_dir):
        for n, a in s["claude_accounts"].items():
            if config_dir and (a or {}).get("config_dir") and cls.expand(a["config_dir"]) == config_dir:
                return n, dict(a)
        return None, {}

    @classmethod
    def account_args(cls, acct):
        return (["--settings", cls.expand(acct["settings"])] if acct.get("settings") else []) + \
            (["--model", acct["model"]] if acct.get("model") else [])

    @classmethod
    def login_flags(cls, s, config_dir, flags):
        acct = cls.account_by_dir(s, config_dir)[1]
        drop = ("--settings", "--model") if cls.fixed_model(acct) else ("--settings",)
        out, i = [], 0
        while i < len(flags):
            if flags[i] in drop:
                i += 2
                continue
            out.append(flags[i])
            i += 1
        return out + cls.account_args(acct)

    @classmethod
    def move_model(cls, s, src, dst, asked, current, fresh):
        own = cls.fixed_model(s["claude_accounts"].get(dst))
        if own:
            return own
        theirs = {cls.fixed_model(a) for a in s["claude_accounts"].values()} - {None}
        return asked or (fresh if (src and cls.fixed_model(s["claude_accounts"].get(src))) or current in theirs
                         else current)


class MailroomUsage:
    """Stands in for the mailroom's agentmail.usage where the board reads its settings (usage.settings): the config's
    "usage" section."""

    @staticmethod
    def settings(cfg):
        u = cfg.get("usage") or {}
        return dict(u, claude_accounts=u.get("claude_accounts") or {})


class MailroomHeadless:
    """Stands in for the mailroom's agentmail.headless where the board lists the logins a claude it starts falls back
    through (logins), as the mailroom has it: each Claude login in usage.order ("claude:NAME") that is not manual and
    is set up (its folder is there), `cmd` as it takes it (accounts.login_flags) and `base` scrubbed for it
    (accounts.claude_env); Codex where the order puts it, given a command; then the API key, when the config names its
    secret, with its token in the environment. Its accounts and usage are those the test gives sources.mailroom."""
    API_KEY = "api-key"

    @classmethod
    def logins(cls, cfg, cmd, base, codex):
        from agentboard import sources
        accounts, s, out = sources.mailroom("accounts"), sources.mailroom("usage").settings(cfg), []
        for target in s.get("order") or []:
            if target == "codex":
                if codex:
                    out.append(("codex", codex, accounts.claude_env({}, base, s)))
                continue
            acct = s["claude_accounts"].get(target.split(":", 1)[-1])
            d = accounts.expand((acct or {}).get("config_dir"))
            if acct is None or accounts.manual(acct) or (d and not os.path.isdir(d)):
                continue
            out.append((target.split(":", 1)[-1], cmd[:1] + accounts.login_flags(s, d, cmd[1:]),
                        accounts.claude_env(acct, base, s)))
        if cfg.get("orchestrator_fallback_secret"):
            out.append((cls.API_KEY, ["bws", "run", "--"] + list(cmd), dict(base, BWS_ACCESS_TOKEN="the-token")))
        return out


class MailroomFailover:
    """Stands in for the mailroom's agentmail.failover where the board reads a usage limit's reply (limit_record), as
    the mailroom reads one from a transcript entry: a plan limit (error rate_limit, quotaLimits rejected), or a model's
    own allowance used up (apiError model_requires_usage_credits); None for any other entry."""

    @staticmethod
    def limit_record(d):
        if not (d.get("type") == "assistant" and d.get("isApiErrorMessage")):
            return None
        content = (d.get("message") or {}).get("content")
        text = " ".join(c.get("text", "") for c in content if isinstance(c, dict)) if isinstance(content, list) \
            else str(content or "")
        q = d.get("quotaLimits") or {}
        if d.get("error") == "rate_limit" and q.get("status") == "rejected":
            return {"window": q.get("rateLimitType") or "limit", "resets_at": q.get("resetsAt"), "at": None,
                    "text": text[:160]}
        if d.get("apiError") == "model_requires_usage_credits" or text.startswith("You're out of usage credits"):
            return {"window": "model_credits", "resets_at": None, "at": None, "text": text[:160]}
        return None


class MailroomClaudeSession:
    """Stands in for the mailroom's agentmail.claude_session where the board copies a transcript to another login
    (copy_transcript), as the mailroom does: to the same project folder under dst_home, with its session folder,
    never over a newer copy."""

    @staticmethod
    def copy_transcript(src, dst_home, sid):
        dst = Path(dst_home) / "projects" / src.parent.name / src.name
        if dst.exists() and dst.stat().st_mtime > src.stat().st_mtime + 1:
            raise RuntimeError("%s is newer than the transcript being moved; not overwriting it" % dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        if (src.parent / sid).is_dir():
            shutil.copytree(str(src.parent / sid), str(dst.parent / sid), dirs_exist_ok=True)
        return dst


class MailroomConfig:
    """Stands in for the mailroom's agentmail.config where the board loads it (config.load): `cfg`."""

    def __init__(self, cfg):
        self.cfg = cfg

    def load(self):
        return self.cfg


def mailroom(**modules):
    """sources.mailroom over these modules and the keys lock's (commands: MailroomCommands, unless given), any other
    one missing as in a mailroom older than it: the mailroom is another repo's install, with its own tests."""
    modules = dict({"commands": MailroomCommands}, **modules)

    def load(module):
        if module not in modules:
            raise ModuleNotFoundError("No module named 'agentmail.%s'" % module, name="agentmail." + module)
        return modules[module]
    return load


def mailroom_with_logins(folder, settings=()):
    """(sources.mailroom over the stand-ins of every mailroom module the board reaches the Claude logins through, {login
    name: its folder}): three logins, each a folder made in `folder`: personal and work, in usage.order with Codex
    after them, and gemini, a manual login with a model of its own; and the API key's secret named, so a test sees the
    board leave the key out. Each login `settings` names has a settings file of its own, <folder>/<name>.json."""
    homes = {n: Path(folder) / n for n in ("personal", "work", "gemini")}
    for home in homes.values():
        home.mkdir(exist_ok=True)
    config = {"orchestrator_fallback_secret": "WORK_ANTHROPIC_API_KEY", "usage": {
        "order": ["claude:personal", "claude:work", "codex", "claude:gemini"], "claude_accounts": {
            "personal": {"config_dir": str(homes["personal"])}, "work": {"config_dir": str(homes["work"])},
            "gemini": {"config_dir": str(homes["gemini"]), "model": "google/gemini-4-neon", "manual": True}}}}
    for name in settings:
        path = Path(folder) / ("%s.json" % name)
        path.write_text(json.dumps({"apiKeyHelper": "/bin/true"}))
        config["usage"]["claude_accounts"][name]["settings"] = str(path)
    return mailroom(accounts=MailroomAccounts, usage=MailroomUsage, config=MailroomConfig(config),
                    headless=MailroomHeadless, failover=MailroomFailover, claude_session=MailroomClaudeSession), homes
