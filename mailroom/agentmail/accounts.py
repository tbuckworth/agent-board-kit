"""Claude logins, one config dir each (config usage.claude_accounts): looking one up, whether it is ready and
allowed for a repo, the environment claude runs with on it, and setting up a second login that shares the default
login's customisations and auto memory."""
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

from . import commands, jsonfile

# Never hand these to a child claude (fnmatch patterns): the first eight would make Claude Code read some other
# credential than the account's own; the rest tie it to the session or background job this process runs inside.
CLAUDE_ENV_DROP = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                   "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                   "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
                   "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_KIND",
                   "CLAUDE_BG_*")


def expand(p):
    return str(Path(os.path.expanduser(p)).resolve()) if p else None


# ------------------------------------------------------------------ accounts
def account(s, name):
    return dict(s["claude_accounts"].get(name) or {})


def default_account(s):
    """The account whose config dir is the default ~/.claude, i.e. what `claude` uses with nothing set."""
    for n, a in s["claude_accounts"].items():
        if not (a or {}).get("config_dir"):
            return n
    return None


def account_ready(acct, platform=None):
    """(ok, why). Existence checks only; no credential file is opened. macOS keeps the login in the Keychain,
    so there only the config dir is checked."""
    d = expand(acct.get("config_dir"))
    if not d:
        return True, ""
    if not os.path.isdir(d):
        return False, "config dir %s does not exist (set it up with agent-usage init-account)" % d
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


def claude_env(acct, base=None):
    """The environment a child claude runs with on `acct`: `base` (None: this process's) without CLAUDE_ENV_DROP,
    plus the account's config dir (none: the default login)."""
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if not any(fnmatch.fnmatchcase(k, p) for p in CLAUDE_ENV_DROP)}
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
