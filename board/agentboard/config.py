"""Where the board finds the mailroom and its configuration, and the few settings of its own.

Identity (owner_name, owner_email, repos_root, orchestrator_dir) comes from the mailroom's config file,
~/.config/agent-mail/config.json, which the kit installer writes. The board only reads it.

Board-only settings live in ~/.config/agent-board/config.json; every key is optional:

  gmail_credentials   path to an "authorized_user" OAuth file for the owner's own Gmail, for the optional
                      "has the owner read the agent's mail" check (agentboard/gmailread.py). null = off.

Environment overrides: AGENT_MAILROOM_DIR (the mailroom checkout, default: the kit's sibling `mailroom/`),
AGENT_MAIL_CONFIG_DIR / AGENT_MAIL_STATE_DIR (as the mailroom itself honours them), AGENT_BOARD_CONFIG
(the board's own config file), AGENT_BOARD_REPOS (the worktree scan root), AGENT_BOARD_GMAIL_CREDS.
"""
import json
import os
from pathlib import Path

HOME = Path.home()
KIT_DIR = Path(__file__).resolve().parents[2]          # board/agentboard/config.py -> the kit checkout
MAIL_CONFIG_DIR = Path(os.environ.get("AGENT_MAIL_CONFIG_DIR") or HOME / ".config/agent-mail")
MAIL_CONFIG = MAIL_CONFIG_DIR / "config.json"
MAIL_STATE = Path(os.environ.get("AGENT_MAIL_STATE_DIR") or HOME / ".local/state/agent-mail")
BOARD_CONFIG = Path(os.environ.get("AGENT_BOARD_CONFIG") or HOME / ".config/agent-board/config.json")

DEFAULT_REPOS_ROOT = "~/code"
BOARD_DEFAULTS = {
    "gmail_credentials": None,
}


def _read(path):
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def mail_config(path=None):
    """The mailroom's config.json as written (no defaults merged), or {}."""
    return _read(path or MAIL_CONFIG)


def board_config(path=None):
    return dict(BOARD_DEFAULTS, **_read(path or BOARD_CONFIG))


def mailroom_dir():
    """The mailroom checkout whose agentmail package and bin/ the board borrows functions from, or None."""
    env = os.environ.get("AGENT_MAILROOM_DIR")
    d = Path(os.path.expanduser(env)) if env else KIT_DIR / "mailroom"
    return d if (d / "agentmail").is_dir() else None


def owner_name(default="the owner", cfg=None):
    """The owner's first name, for text agents read ("[relayed from Alex via agent-board]")."""
    name = ((cfg if cfg is not None else mail_config()).get("owner_name") or "").strip()
    return name or default


def owner_email(cfg=None):
    """The owner's own address (results go there), else the first allowlisted sender, else None."""
    cfg = cfg if cfg is not None else mail_config()
    allow = [a for a in cfg.get("allowlist") or [] if isinstance(a, str) and a.strip()]
    return (cfg.get("owner_email") or "").strip() or (allow[0].strip() if allow else None)


def owner_addresses(cfg=None):
    """Every address that counts as the owner: owner_email first, then the mailroom allowlist. Lower case."""
    cfg = cfg if cfg is not None else mail_config()
    out = []
    for a in [cfg.get("owner_email")] + list(cfg.get("allowlist") or []):
        if isinstance(a, str) and a.strip() and a.strip().lower() not in out:
            out.append(a.strip().lower())
    return out


def repos_root(cfg=None):
    """Where repos and worktrees live: AGENT_BOARD_REPOS, else the mailroom config's repos_root, else ~/code."""
    env = os.environ.get("AGENT_BOARD_REPOS")
    root = env or (cfg if cfg is not None else mail_config()).get("repos_root") or DEFAULT_REPOS_ROOT
    return Path(os.path.expanduser(str(root)))


def orchestrator_dir(cfg=None):
    """The orchestrator's working directory (its sessions are not tiles): orchestrator_dir, else
    <repos_root>/orchestrator."""
    cfg = cfg if cfg is not None else mail_config()
    d = cfg.get("orchestrator_dir")
    return Path(os.path.expanduser(str(d))) if d else repos_root(cfg) / "orchestrator"


def gmail_credentials(cfg=None):
    """The OAuth file for the optional Gmail read-check, or None when it is off."""
    env = os.environ.get("AGENT_BOARD_GMAIL_CREDS")
    p = env or (cfg if cfg is not None else board_config()).get("gmail_credentials")
    return Path(os.path.expanduser(str(p))) if p else None
