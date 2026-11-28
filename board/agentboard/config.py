"""Where the board finds the mailroom and its configuration, and the few settings of its own.

Identity (owner_name, owner_email, repos_root, orchestrator_dir) comes from the mailroom's config file,
~/.config/agent-mail/config.json, which the kit installer writes. The board only reads it.

Board-only settings live in ~/.config/agent-board/config.json; every key is optional, and everything that reaches
one of the owner's own accounts is off until named:

  gmail_credentials   path to an "authorized_user" OAuth file for the owner's own Gmail: the "has the owner read the
                      agent's mail" check and the Mail tab's Forward, which makes a Gmail draft (agentboard/gmailstate.py;
                      the read-check needs the gmail.readonly scope, Forward gmail.compose or gmail.modify as well: a
                      read-only login is refused there and the answer says so), and the Time page's "own email"
                      source. null = off.
  google_token        path to an "authorized_user" OAuth file for the owner's Google account with Calendar read access
                      (and Drive metadata read, for the Time page's documents): the Time page's meetings and documents
                      sources, and the meeting alerts when meeting_alerts is on. null = off.
  meeting_alerts      true turns on the Mac's meeting alerts (agentboard/meetings.py), read with google_token.
                      Default false: a google_token for the Time page alone alerts nothing.
  tasks_repo          "OWNER/REPO" of the owner's to-do list, for the Time page's to-do source. Default: the "repo" of
                      the to-do component's config (~/.config/todo-desk/config.json); neither = that source off.
  keyring_unlock      true turns on the desktop keyring's lock mark and its unlock from the Mac and the phone
                      (agentboard/keyring.py; README "Unlock the desktop"), for a desktop that logs in by itself and
                      leaves gnome-keyring locked. Default false: no keyring state is read, and keyring-unlock refuses
                      before it reads a password.

Per-feature files beside it, each optional: time.json (the Time page), meetings.json (the meeting alerts'
calendars), allowance.json (the usage breakdown's prices and the spend meter's week), colours.json (the groups'
colours, agentboard/colours.py: it replaces the kit's agentboard/colours.json, which names no group), mailweb.json (the
phone page and the voice page's devices), voice.json (the voice page), desk.json (the desk), speech.json (the
desktop's voice), transcripts.json (the transcript archive; it has no default folder), keyring.json (the keyring
daemon's program).

Environment overrides: AGENT_MAILROOM_DIR (the mailroom checkout, default: the kit's sibling `mailroom/`),
AGENT_MAIL_CONFIG_DIR / AGENT_MAIL_STATE_DIR (as the mailroom itself honours them), AGENT_BOARD_CONFIG
(the board's own config file), AGENT_BOARD_REPOS (the worktree scan root), AGENT_BOARD_GMAIL_CREDS,
AGENT_BOARD_GOOGLE_TOKEN, AGENT_BOARD_MEETING_ALERTS ("yes"/"no"), AGENT_BOARD_KEYRING_UNLOCK ("yes"/"no"),
AGENT_BOARD_COLOURS (the groups' colours file), TODO_DESK_CONFIG_DIR (the to-do component's config folder),
AGENT_BOARD_MAC (the Mac's ssh alias, for the voice desk's `looking` and the transcript archive; mac_host).
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
TODO_CONFIG_DIR = Path(os.environ.get("TODO_DESK_CONFIG_DIR") or HOME / ".config/todo-desk")

DEFAULT_REPOS_ROOT = "~/code"
BOARD_DEFAULTS = {
    "gmail_credentials": None,
    "google_token": None,
    "meeting_alerts": False,
    "tasks_repo": None,
    "keyring_unlock": False,
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


def _path_setting(key, env, cfg):
    p = os.environ.get(env) or (cfg if cfg is not None else board_config()).get(key)
    return Path(os.path.expanduser(str(p))) if p else None


def gmail_credentials(cfg=None):
    """The OAuth file of the owner's own Gmail (read-check, the Mail tab's Forward, the Time page's own email), or None
    when that is off. AGENT_BOARD_GMAIL_CREDS overrides the board config."""
    return _path_setting("gmail_credentials", "AGENT_BOARD_GMAIL_CREDS", cfg)


def google_token(cfg=None):
    """The OAuth file of the owner's Google login with Calendar (and Drive metadata) read access (meeting alerts, the
    Time page's meetings and documents), or None when that is off. AGENT_BOARD_GOOGLE_TOKEN overrides the board
    config."""
    return _path_setting("google_token", "AGENT_BOARD_GOOGLE_TOKEN", cfg)


def meeting_alerts(cfg=None):
    """Whether the owner turned the meeting alerts on: AGENT_BOARD_MEETING_ALERTS ("yes", "true", "1" or "on"; anything
    else is off) when set, else the board config's meeting_alerts (true only). They also need google_token."""
    env = os.environ.get("AGENT_BOARD_MEETING_ALERTS")
    if env is not None:
        return env.strip().lower() in ("yes", "true", "1", "on")
    return (cfg if cfg is not None else board_config()).get("meeting_alerts") is True


def tasks_repo(cfg=None, todo_config=None):
    """"OWNER/REPO" of the owner's to-do list (the Time page's to-do source): the board config's tasks_repo, else the
    to-do component's "repo" (TODO_CONFIG_DIR/config.json), else None (that source off)."""
    repo = (cfg if cfg is not None else board_config()).get("tasks_repo")
    if not (isinstance(repo, str) and repo.strip()):
        repo = _read(todo_config or TODO_CONFIG_DIR / "config.json").get("repo")
    return repo.strip() if isinstance(repo, str) and repo.strip() else None


def keyring_unlock(cfg=None):
    """Whether the owner turned the desktop keyring's lock mark and unlock on: AGENT_BOARD_KEYRING_UNLOCK ("yes",
    "true", "1" or "on"; anything else is off) when set, else the board config's keyring_unlock (true only)."""
    env = os.environ.get("AGENT_BOARD_KEYRING_UNLOCK")
    if env is not None:
        return env.strip().lower() in ("yes", "true", "1", "on")
    return (cfg if cfg is not None else board_config()).get("keyring_unlock") is True


def mac_host(cfg=None):
    """The ssh alias this desktop reaches the owner's Mac by, for what reads the Mac (the voice desk's `looking`, the
    transcript archive's Mac sources): AGENT_BOARD_MAC when set (empty: no Mac), else the mailroom config's
    remote_hosts["mac"] (the kit installer writes it from MAC_HOST), else None: no Mac is configured, and nothing
    reaches for one."""
    env = os.environ.get("AGENT_BOARD_MAC")
    if env is not None:
        return env.strip() or None
    remote = (cfg if cfg is not None else mail_config()).get("remote_hosts") or {}
    host = remote.get("mac") if isinstance(remote, dict) else None
    return host.strip() if isinstance(host, str) and host.strip() else None
