"""Paths and configuration. Nothing here holds a secret; the token lives in TOKEN_PATH.

Every personal value (the agents account, the owner's name and address, the GitHub owner, where
repos live) comes from ~/.config/agent-mail/config.json. DEFAULTS are neutral: `account` must be
set before anything sends or polls mail, and `allowlist` must name the owner before any mail is
delivered.
"""
import json
import os
from pathlib import Path

HOME = Path.home()
CONF_DIR = Path(os.environ.get("AGENT_MAIL_CONFIG_DIR", HOME / ".config" / "agent-mail"))
TOKEN_PATH = CONF_DIR / "token.json"
CLIENT_SECRETS_PATH = CONF_DIR / "credentials.json"
CONFIG_PATH = CONF_DIR / "config.json"
STATE_DIR = Path(os.environ.get("AGENT_MAIL_STATE_DIR", HOME / ".local" / "state" / "agent-mail"))
DB_PATH = STATE_DIR / "mailroom.sqlite"
LOCK_PATH = STATE_DIR / "mailroom.lock"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class ConfigError(Exception):
    """A required config value is missing."""


DEFAULTS = {
    # The dedicated agents Gmail account (required): every agent sends from it, the mailroom polls it.
    "account": None,
    # From display name on agent mail: "<from_name> (<session name>)".
    "from_name": "Agents",
    # Only mail from these senders is ever delivered into a session. Everything else is held.
    # Normally exactly one entry: the owner's personal address.
    "allowlist": [],
    # The human the agents work for: named in the text agents read ("email from Alex", "tell Alex").
    "owner_name": "the owner",
    # The owner's personal address, where agents send results. None: the first allowlist entry.
    "owner_email": None,
    # GitHub user or org that `agent-spawn --new-repo` creates repos under (required for --new-repo).
    "github_owner": None,
    # Where repos and worktrees live: `agent-spawn --repo NAME` and `--new-repo NAME` resolve here.
    "repos_root": "~/code",
    # Repo for tasks that belong to no project (read by the orchestrator brief and the to-do tools).
    "general_repo": "~/code/general",
    "poll_seconds": 30,
    # When the matched session is not live: resume it (claude --resume --bg / codex exec resume)?
    "resume_offline": True,
    # Resume with permissions bypassed / full-access sandbox, as the desktop sessions run anyway.
    "resume_dangerous": True,
    # The orchestrator: one fresh `claude -p` run per event (mode "exec"), in this directory.
    # Mode "session" instead delivers to a live session with this name (the old design).
    "orchestrator": "orchestrator",
    "orchestrator_mode": "exec",
    "orchestrator_dir": "~/code/orchestrator",
    # The alias on purpose: the orchestrator follows the latest Sonnet as each one ships.
    "orchestrator_model": "sonnet",
    "orchestrator_effort": "high",
    "orchestrator_timeout": 900,
    # agent-spawn's model and effort per CLI when --model/--effort are not given. Full ids, so a
    # new release does not change what spawned agents run. Set one to null in config.json to
    # leave that choice to the CLI's own default.
    "spawn_claude_model": "claude-opus-5-5",
    "spawn_claude_effort": "xhigh",
    "spawn_codex_model": "gpt-6.1-sol",
    "spawn_codex_effort": "xhigh",
    # "auto": with no --cli/--account/--model, agent-spawn picks the CLI and Claude login by remaining plan
    # usage (agent-usage). "manual" keeps the old default, Claude on the default login.
    "spawn_route": "manual",
    # agent-usage: Claude logins (one config dir each), preference order and thresholds. Every key and its
    # default is in agentmail/usage.py DEFAULTS, e.g. {"claude_accounts": {...}, "order": [...]}.
    "usage": {},
    # Every agent agent-spawn starts or agent-send resumes runs in its own systemd scope with these caps
    # (agentmail/memcap.py), so one runaway batch is killed in its own scope instead of exhausting the
    # desktop. Sizes like "32G"; "off" for no cap. agent-spawn --memory-max/--swap-max override per launch.
    "agent_memory_max": "32G",
    "agent_swap_max": "2G",
    # agent-memwatch: warn the agents holding the most memory at 80%/90% RAM in use. Overrides go here,
    # e.g. {"warn_pct": 85}; every key and its default is in agentmail/memwatch.py DEFAULTS.
    "memwatch": {},
    # agent-failover: move a Claude agent to another login when its own hits a usage limit. Overrides go
    # here, e.g. {"enabled": false} or {"pinned": ["long-job-*"]}; keys and defaults: agentmail/failover.py.
    "failover": {},
    # Optional: if the claude.ai login fails or is rate-limited, retry the orchestrator once with an
    # Anthropic API key held in Bitwarden Secrets Manager, injected by `bws run`. Set this to the
    # secret's name in your bws project (e.g. "ANTHROPIC_API_KEY"); None disables the fallback.
    "orchestrator_fallback_secret": None,
    "bws_token_file": "~/.config/bws/token",
    "bws_project_file": "~/.config/bws/project-id",
    # Collaborators' agents: second tier. Their mail is delivered marked as external-agent input,
    # never as the owner's instruction. {address: {"name": ..., "owner": ...}}
    "collaborators": {},
    # Honour an allowlisted or collaborator address only if Gmail's Authentication-Results shows
    # a DKIM pass for the sender's domain. A From header alone is trivially forged.
    "require_dkim": True,
    # Flood and loop guards for collaborator mail.
    "collaborator_hourly_cap": 10,
    "agent_thread_cap": 12,
    # One-line notice to the owner when a collaborator starts a new thread.
    "notify_owner_new_collab_thread": True,
    # Where mailroom alerts go (default: owner_email, else the first allowlist entry).
    "alert_to": None,
    "alerts_enabled": True,
    # False on machines whose sessions the mailroom cannot reach (the Mac): agent-mail send refuses.
    "send_enabled": True,
    # Stable machine identities. Mac commands proxy mail to the desktop over SSH.
    "host_id": "desktop",
    "mail_proxy": None,
    "remote_hosts": {},  # host_id -> SSH alias (only configured peers are contacted)
    "remote_timeout": 30,
    "recovery_hosts": [],  # searched after the local machine, in order
    "recovery_seconds": 20,
    "recovery_days": 14,
    "labels": {
        "delivered": "mailroom/delivered",
        "held": "mailroom/held",
        "skipped": "mailroom/skipped",
    },
}


def load():
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    cfg["account"] = (cfg.get("account") or "").strip().lower() or None
    cfg["allowlist"] = [a.lower() for a in (cfg.get("allowlist") or [])]
    cfg["collaborators"] = {k.lower(): (v or {}) for k, v in (cfg.get("collaborators") or {}).items()}
    return cfg


def owner_name(cfg=None):
    """The owner's first name for text agents read; "the owner" when unset."""
    cfg = load() if cfg is None else cfg
    return (cfg.get("owner_name") or "").strip() or "the owner"


def owner_possessive(cfg=None):
    """"Alex's", or "the owner's"."""
    return owner_name(cfg) + "'s"


def owner_email(cfg=None):
    """The owner's personal address: owner_email, else the first allowlist entry, else None."""
    cfg = load() if cfg is None else cfg
    if cfg.get("owner_email"):
        return cfg["owner_email"].strip().lower()
    return cfg["allowlist"][0] if cfg.get("allowlist") else None


def require_account(cfg):
    """The agents account, or ConfigError with what to set."""
    if not cfg.get("account"):
        raise ConfigError("config `account` is not set: put the agents Gmail address in %s, "
                          'e.g. {"account": "agents@example.com", "allowlist": ["owner@example.com"]}' % CONFIG_PATH)
    return cfg["account"]

