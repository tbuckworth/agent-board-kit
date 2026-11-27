"""Claude logins, one config dir each (config usage.claude_accounts): looking one up, whether it is ready and
allowed for a repo, the options and environment claude runs with on it, and setting up a second login that shares the
default login's customisations and auto memory.

A login may name a Claude Code settings file ("settings", passed as --settings) and a model of its own ("model", the
only one it runs: passed as --model, in place of any other); one marked "manual" is used only when a person names it
(agent-spawn --account, agent-switch --to), never by routing, failover or a resume's placement."""
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

from . import commands, config, jsonfile

# Never hand these to a child claude (fnmatch patterns): the first six would make Claude Code read some other
# credential, endpoint or model than the account's own; the rest tie it to the session or background job this process
# runs inside. claude_env drops the variables of every login's settings file too (settings_env).
CLAUDE_ENV_DROP = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_*", "CLAUDE_CODE_USE_BEDROCK",
                   "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SESSION_ID",
                   "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDECODE",
                   "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_KIND", "CLAUDE_BG_*")


def expand(p):
    return str(Path(os.path.expanduser(p)).resolve()) if p else None


# ------------------------------------------------------------------ accounts
def account(s, name):
    return dict(s["claude_accounts"].get(name) or {})


def current_settings():
    """usage.settings of config.json as it is now (config.load). Raises when config.json does not parse."""
    from . import usage   # usage imports this module: imported here, when called
    return usage.settings(config.load())


def manual(acct):
    """Whether this login is used only when a person names it (its "manual")."""
    return bool((acct or {}).get("manual"))


def fixed_model(acct):
    """The one model this login runs (its "model"), or None: any model."""
    return (acct or {}).get("model") or None


def account_by_dir(s, config_dir):
    """(name, account) of the login whose config dir is config_dir, both expanded; (None, {}) for None (the default
    login) or a dir no account names."""
    d = expand(config_dir)
    if d:
        for n, a in s["claude_accounts"].items():
            if expand((a or {}).get("config_dir")) == d:
                return n, dict(a)
    return None, {}


def account_args(acct):
    """The options claude runs with on this login: --settings with its settings file, then --model with its own model
    (only what it sets)."""
    acct = acct or {}
    return (["--settings", expand(acct["settings"])] if acct.get("settings") else []) + \
        (["--model", acct["model"]] if acct.get("model") else [])


def launch_args(s, config_dir):
    """account_args of the login whose config dir is config_dir (s: usage.settings): [] for the default login and for
    a login that sets neither. Every claude command line for an agent carries these, after its other options
    (login_flags adds them); `agent-usage login` prints them for the researcher's runner."""
    return account_args(account_by_dir(s, config_dir)[1])


def move_model(s, src, dst, asked, current, fresh):
    """The model an agent runs once on Claude login dst (s: usage.settings), coming from Claude login src (None: from
    Codex, or a new agent), when `asked` is asked for (None: no model asked). current: the model it runs now (a new
    agent: the one it would get). fresh: what a new agent gets on a login without a model of its own (config
    spawn_claude_model; None: the CLI's own). On a login with its own model, that model; any other asked is refused.
    On any other login, never a model only a one-model login runs: one asked is refused, and one it ran on such a
    login gives way to fresh; else asked, else current. Raises ValueError, naming the login and the model, for a
    model refused."""
    own = fixed_model(account(s, dst))
    if own:
        if asked and asked != own:
            raise ValueError("Claude login %s runs only %s, not %s: leave out the model" % (dst, own, asked))
        return own
    theirs = {fixed_model(a) for a in s["claude_accounts"].values()} - {None}
    if asked in theirs:
        raise ValueError("Claude login %s cannot run %s, the model of a login that runs no other: leave out the "
                         "model" % (dst, asked))
    if asked:
        return asked
    return fresh if (src and fixed_model(account(s, src))) or current in theirs else current


def login_flags(s, config_dir, flags):
    """flags (a claude command line's options) for a claude started on the login config_dir: never a --settings they
    carry (the login decides), no --model of theirs on a login with its own model, then launch_args. Doing it twice
    changes nothing."""
    acct = account_by_dir(s, config_dir)[1]
    drop = ("--settings", "--model") if fixed_model(acct) else ("--settings",)
    return commands.without_flags(list(flags), *drop) + account_args(acct)


def default_account(s):
    """The account whose config dir is the default ~/.claude, i.e. what `claude` uses with nothing set."""
    for n, a in s["claude_accounts"].items():
        if not (a or {}).get("config_dir"):
            return n
    return None


def account_ready(acct, platform=None):
    """(ok, why). Existence checks only; no credential file is opened. A login with a settings file authenticates by
    its apiKeyHelper, so the config dir and that file are checked; macOS keeps a login in the Keychain, so there only
    the config dir is checked."""
    d = expand(acct.get("config_dir"))
    if d and not os.path.isdir(d):
        return False, "config dir %s does not exist (set it up with agent-usage init-account)" % d
    if acct.get("settings"):
        f = expand(acct["settings"])
        return (True, "") if os.path.isfile(f) else (False, "settings file %s does not exist" % f)
    if not d:
        return True, ""
    if (platform or sys.platform) != "darwin" and not os.path.exists(os.path.join(d, ".credentials.json")):
        return False, "no login in %s yet: run CLAUDE_CONFIG_DIR=%s claude, then /login" % (d, d)
    return True, ""


def owner_allowed(acct, owner):
    if not owner:
        return True, ""
    low = owner.lower()
    if low in [o.lower() for o in acct.get("exclude_owners") or []]:
        return False, "repos owned by %s are excluded from this account" % owner
    only = [o.lower() for o in acct.get("only_owners") or []]
    if only and low not in only:
        return False, "this account is only for repos owned by %s" % ", ".join(acct["only_owners"])
    return True, ""


def repo_owner(path):
    """GitHub owner of the origin remote of the repo at path, from `git ls-remote --get-url origin` (which expands
    url.*.insteadOf); None when there is no origin remote (or no repo) or origin is not on GitHub. Raises
    commands.CommandError when git fails, so an owner rule is never skipped for want of an answer."""
    r = commands.checked(commands.run(["git", "-C", path, "ls-remote", "--get-url", "origin"], 10),
                         "git ls-remote --get-url origin in %s" % path, commands.CommandError)
    m = re.search(r"github\.com[:/]+([^/]+)/", r.stdout.strip())
    return m.group(1) if m else None


def folder_owner(path):
    """repo_owner of the folder `path` an agent works in; None when there is none, or it is gone."""
    return repo_owner(path) if path and os.path.isdir(path) else None


def settings_env(s):
    """The names of the variables the logins' settings files set (their "env"; s: usage.settings). Claude Code puts
    them in every command its agent runs too, so a child claude started from such an agent for another login would
    take that login's endpoint and models. A file that is not there names none: no agent runs on that login. Raises
    ValueError naming a file that is there but cannot be read, is not JSON, or whose "env" is not an object, so that
    no child claude starts with variables that may be that login's."""
    names = set()
    for n, a in s["claude_accounts"].items():
        path = expand((a or {}).get("settings"))
        if not path or not os.path.exists(path):
            continue
        try:
            d = json.loads(Path(path).read_text())
        except (OSError, ValueError) as e:
            raise ValueError("login %s's settings file %s cannot be read: %s" % (n, path, e)) from e
        env = d.get("env", {}) if isinstance(d, dict) else None
        if not isinstance(env, dict):
            raise ValueError("login %s's settings file %s is not an object with an \"env\" object" % (n, path))
        names.update(env)
    return names


def claude_env(acct, base=None, s=None):
    """The environment a child claude runs with on `acct`: `base` (None: this process's) without CLAUDE_ENV_DROP and
    the logins' settings variables (settings_env of s, usage.settings; None: config.json's, current_settings), plus
    the account's config dir (none: the default login). Raises ValueError for a settings file settings_env cannot
    read."""
    drop = CLAUDE_ENV_DROP + tuple(sorted(settings_env(current_settings() if s is None else s)))
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if not any(fnmatch.fnmatchcase(k, p) for p in drop)}
    d = expand(acct.get("config_dir"))
    if d:
        env["CLAUDE_CONFIG_DIR"] = d
    return env


# ------------------------------------------------------------------ project memory for a second account
def project_slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def link_memory(repo_root, config_dir, default_home):
    """Give an agent on another account the same auto memory as the default login (whose Claude home is
    default_home): symlink <config_dir>/projects/<slug>/memory to <default_home>/projects/<slug>/memory. Returns
    the link, or None when there is nothing to link (no memory on the default login) or something is there
    already. Raises OSError if the folder or the link cannot be made."""
    slug = project_slug(Path(repo_root).resolve())
    src = Path(default_home) / "projects" / slug / "memory"
    dst = Path(config_dir) / "projects" / slug / "memory"
    if not src.is_dir() or dst.exists() or dst.is_symlink():
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(src), str(dst))
    return str(dst)


# ------------------------------------------------------------------ setting up a second account
# User-level customisations shared with the default login by symlink. Everything else (credentials, .claude.json,
# projects, sessions, history, caches) stays per account. Claude Code refuses a symlinked .credentials.json anyway.
SHARED = ("CLAUDE.md", "settings.json", "commands", "agents", "skills", "hooks", "plugins", "output-styles",
          "keybindings.json", "tools")


def init_account(config_dir, default_home, default_json, copy_mcp=(), dry_run=False):
    """Create a second account's config dir that shares the customisations of the default login, whose Claude
    home is default_home and whose .claude.json is default_json. Never touches the default login's files except
    to read them. Returns a list of what it did (or would do)."""
    home = Path(default_home)
    d = Path(os.path.expanduser(config_dir)).resolve()
    if d == home.resolve():
        raise ValueError("that is the default login's own config dir")
    done = []
    if not d.exists():
        done.append("create %s (mode 700)" % d)
        if not dry_run:
            d.mkdir(mode=0o700, parents=True)
    for item in SHARED:
        src, dst = home / item, d / item
        if src.exists() and not (dst.exists() or dst.is_symlink()):
            done.append("link %s -> %s" % (dst, src))
            if not dry_run:
                os.symlink(str(src), str(dst))
    if copy_mcp:
        src_json = Path(default_json)
        dst_json = d / ".claude.json"
        servers = jsonfile.read(src_json).get("mcpServers") or {}
        missing = [n for n in copy_mcp if n not in servers]
        if missing:
            raise ValueError("no user MCP server named %s in %s" % (", ".join(missing), src_json))
        if not dst_json.exists():
            raise ValueError("%s does not exist yet: log in to the new account first, then copy MCP servers" % dst_json)
        done.append("copy user MCP servers %s into %s" % (", ".join(copy_mcp), dst_json))
        if not dry_run:
            data = json.loads(dst_json.read_text())
            data.setdefault("mcpServers", {}).update({n: servers[n] for n in copy_mcp})
            jsonfile.write(dst_json, data, jsonfile.PRIVATE, 2)
    return done
