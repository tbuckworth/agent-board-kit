"""Paths and configuration. Nothing here holds a secret: accounts name where their keys live, never values.

Layers, later ones win:
  1. cloud_watchdog/defaults.toml        (shipped; no accounts, neutral thresholds)
  2. ~/.config/cloud-watchdog/config.toml (yours: accounts, identity, overrides; tables merge key by key)
  3. the owner's name, email and repos root fall back to the mailroom's ~/.config/agent-mail/config.json
"""
import json
import os
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    try:
        import tomli as tomllib
    except ImportError:
        raise SystemExit("cloud-watchdog needs Python 3.11+ (tomllib) or the `tomli` package") from None

HOME = Path.home()
PKG_DIR = Path(__file__).resolve().parent
REPO_DIR = PKG_DIR.parent
DEFAULTS_PATH = PKG_DIR / "defaults.toml"
CONF_DIR = HOME / ".config" / "cloud-watchdog"
CONFIG_PATH = CONF_DIR / "config.toml"
STATE_DIR = HOME / ".local" / "state" / "cloud-watchdog"
DB_PATH = STATE_DIR / "state.sqlite"
LOCK_PATH = STATE_DIR / "check.lock"
MAILROOM_CONFIG = Path(os.environ.get("AGENT_MAIL_CONFIG_DIR", HOME / ".config" / "agent-mail")) / "config.json"
DEFAULT_OWNER_NAME = "the owner"
LOCAL_BIN = HOME / ".local" / "bin"   # agent-mail, agent-send and the cloud-watch link
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")

# Accepted at the top level of config.toml as a convenience; they belong in [identity].
IDENTITY_KEYS = ("owner_modal_users", "owner_lambda_ssh_keys", "modal_shared_apps")


def merge(base, over):
    """Deep-merge two dicts: tables merge key by key, everything else is replaced."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


# Rule keys of the first kit release, read from an existing config.toml under their current names.
LEGACY_LONG_RUNNING = {"modal_active_hours": ("modal",), "lambda_hours": ("lambda",), "hf_hours": ("hf", "deepinfra"),
                       "slurm_hours": ("slurm",)}


def legacy_rules(user):
    """`user` (a parsed config.toml) with [rules.long_running] *_hours keys moved into [rules.long_running.hours] and
    [rules.storage] modal_volumes_usd_month renamed volumes_usd_month; a current key, when also set, wins."""
    rules = user.get("rules") or {}
    lr = rules.get("long_running") or {}
    for old, providers in LEGACY_LONG_RUNNING.items():
        if old in lr:
            hours = lr.setdefault("hours", {})
            value = lr.pop(old)
            for p in providers:
                hours.setdefault(p, value)
    st = rules.get("storage") or {}
    if "modal_volumes_usd_month" in st:
        st.setdefault("volumes_usd_month", st.pop("modal_volumes_usd_month"))
    return user


def mailroom_config(path=None):
    """The mailroom's JSON config (owner_name, owner_email, allowlist, repos_root), or {}."""
    p = Path(path) if path else MAILROOM_CONFIG
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def load(path=None, mailroom_path=None):
    with open(DEFAULTS_PATH, "rb") as f:
        cfg = tomllib.load(f)
    path = Path(path) if path else CONFIG_PATH
    if path.exists():
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for k in IDENTITY_KEYS:
            if k in user:
                user.setdefault("identity", {})[k] = user.pop(k)
        cfg = merge(cfg, legacy_rules(user))
    # An account can be switched off locally with `enabled = false`.
    cfg["accounts"] = {k: resolve_account(k, v, cfg.get("secrets"))
                       for k, v in (cfg.get("accounts") or {}).items() if v.get("enabled", True)}
    # Who the human is, and where repos live: this file first, then the mailroom's config.
    mr = mailroom_config(mailroom_path)
    allow = mr.get("allowlist") or []
    cfg["owner_name"] = cfg.get("owner_name") or mr.get("owner_name") or DEFAULT_OWNER_NAME
    cfg["owner_email"] = cfg.get("owner_email") or mr.get("owner_email") or (allow[0] if allow else None)
    cfg["repos_root"] = cfg.get("repos_root") or mr.get("repos_root") or "~/code"
    cfg["repo_roots"] = cfg.get("repo_roots") or [cfg["repos_root"]]
    return cfg


def service_path(current=None):
    """The PATH the watchdog runs its tools under: LOCAL_BIN, then `current` (a PATH value; None: SYSTEM_PATH), each
    directory once. The systemd units set it with no `current`; creds.base_env passes the caller's PATH."""
    dirs = [str(LOCAL_BIN)] + (current.split(":") if current is not None else list(SYSTEM_PATH))
    return ":".join(dict.fromkeys(dirs))


def expand(p):
    return Path(os.path.expanduser(p))


def resolve_account(name, acct, secrets=None):
    """Fill in where an account's keys come from (paths and backend only, never values)."""
    s = secrets or {}
    out = dict(acct)
    out["secrets"] = acct.get("secrets") or s.get("backend") or "env-file"
    env_dir = expand(s.get("env_dir") or "~/.config/cloud-watchdog/accounts")
    out["env_file"] = str(expand(acct["env_file"]) if acct.get("env_file") else env_dir / ("%s.env" % name.replace(":", "-")))
    out["bws_token_file"] = str(expand(acct.get("bws_token_file") or s.get("bws_token_file") or "~/.config/bws/token"))
    out["bws_project_file"] = str(expand(acct.get("bws_project_file") or s.get("bws_project_file")
                                         or "~/.config/bws/project-id"))
    return out
