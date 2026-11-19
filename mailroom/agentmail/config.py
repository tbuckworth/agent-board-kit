"""Paths and configuration. Nothing here holds a secret; the token lives in TOKEN_PATH.

Every personal value (the agents account, the owner's name and address, the GitHub owner, where
repos live) comes from ~/.config/agent-mail/config.json. DEFAULTS are neutral: `account` must be
set before anything sends or polls mail, and `allowlist` must name the owner before any mail is
delivered.
"""
import copy
import json
import os
from pathlib import Path

HOME = Path.home()
CONF_DIR = Path(os.environ.get("AGENT_MAIL_CONFIG_DIR", HOME / ".config" / "agent-mail"))
TOKEN_PATH = CONF_DIR / "token.json"
# The OAuth client agent-mail-auth uses when config oauth_client_file is not set.
CLIENT_SECRETS_PATH = CONF_DIR / "credentials.json"
CONFIG_PATH = CONF_DIR / "config.json"
STATE_DIR = Path(os.environ.get("AGENT_MAIL_STATE_DIR", HOME / ".local" / "state" / "agent-mail"))
DB_PATH = STATE_DIR / "mailroom.sqlite"
# Every mail an agent sends, as the MIME bytes handed to Gmail, one file per sha256 (agentmail/messages.py).
MESSAGES_DIR = STATE_DIR / "messages"
LOCK_PATH = STATE_DIR / "mailroom.lock"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
# The coding-agent CLIs the tools start and talk to, and the DEFAULTS keys holding the model and effort agent-spawn
# gives a new agent of each.
CLIS = ("claude", "codex")
SPAWN_KEYS = {"claude": {"model": "spawn_claude_model", "effort": "spawn_claude_effort"},
              "codex": {"model": "spawn_codex_model", "effort": "spawn_codex_effort"}}
# What an agent may type for the owner where an address or a name is wanted (agent-mail --to owner, agent-group
# --as owner), whatever config owner_name says; the owner's name itself works too.
OWNER_ALIAS = "owner"


class ConfigError(Exception):
    """A required config value is missing."""


# Every config key and its default; config.json overrides them (load).
DEFAULTS = {
    # The dedicated agents Gmail account (required): every agent sends from it, the mailroom polls it.
    "account": None,
    # From display name on agent mail: "<from_name> (<session name>)".
    "from_name": "Agents",
    # The registered Google app (its "Desktop app" OAuth client JSON) that agent-mail-auth asks for the account's
    # consent through. None: CLIENT_SECRETS_PATH (~/.config/agent-mail/credentials.json).
    "oauth_client_file": None,
    # Only mail from these senders is ever delivered into a session. Everything else is held.
    # Normally exactly one entry: the owner's personal address.
    "allowlist": [],
    # The human the agents work for: named in the text agents, the orchestrator and the pre-send reviewer read
    # ("email from Alex", "tell Alex").
    "owner_name": "the owner",
    # The owner's personal address, where agents send results. None: the first allowlist entry.
    "owner_email": None,
    # GitHub user or org that `agent-spawn --new-repo` creates repos under (its --org default; required for
    # --new-repo).
    "github_owner": None,
    # Where repos and worktrees live: agent-spawn finds --repo NAME and makes --new-repo here (env
    # AGENT_SPAWN_ROOT overrides).
    "repos_root": "~/code",
    # Repo for tasks that belong to no project (read by the orchestrator brief and the to-do tools).
    "general_repo": "~/code/general",
    "poll_seconds": 30,   # agent-mailroom's wait between Gmail polls
    # When the matched session is not live: resume it (claude --resume --bg / codex exec resume)?
    "resume_offline": True,
    # Resume with permissions bypassed / full-access sandbox, as the agents' sessions run anyway; also what
    # agent-send --offline resume does when given neither --dangerous nor --no-dangerous.
    "resume_dangerous": True,
    # The orchestrator: one fresh headless run per event, in orchestrator_dir. A message that
    # agent-send addresses to this name runs one.
    "orchestrator": "orchestrator",
    "orchestrator_dir": "~/code/orchestrator",
    # The model and effort of its Claude runs; the alias on purpose: it follows the latest Sonnet as each one ships.
    "orchestrator_model": "sonnet",
    "orchestrator_effort": "high",
    # Seconds an event may wait for the orchestrator lock, and then each run on a login (headless.logins) may take.
    # Keep TimeoutStopSec in systemd/agent-mailroom.service above twice this.
    "orchestrator_timeout": 900,
    # When the Claude logins before Codex in usage.order are out, the event runs on Codex: this model, or
    # spawn_codex_model when null, at this reasoning effort.
    "orchestrator_codex_model": None,
    "orchestrator_codex_effort": "high",
    # agent-spawn's model and effort per CLI when --model/--effort are not given. null leaves that choice to the
    # CLI's own default (the kit's default for Claude). Set full ids in config.json, e.g. "claude-fable-5-2", so a new
    # release does not change what spawned agents run.
    "spawn_claude_model": None,
    "spawn_claude_effort": "xhigh",
    # The models a Claude agent steps down to, in order, after spawn_claude_model (claude_model_order), e.g.
    # ["claude-opus-5-5"]. A new agent routed by usage, or one agent-failover moves on a limit, takes each Claude
    # login in usage.order on the first model, then each on the next, and so on; only once none has headroom does it
    # go where it bills extra usage, and only on the last model. agent-failover moves it back once the login and model
    # it started on have reset. [] (the default) keeps every agent on spawn_claude_model; it is ignored while
    # spawn_claude_model is null.
    "claude_fallback_models": [],
    "spawn_codex_model": "gpt-6.1-sol",
    "spawn_codex_effort": "xhigh",
    # "auto": with no --cli/--account/--model, agent-spawn picks the CLI and Claude login by remaining plan
    # usage (agent-usage). "manual": Claude on the default login unless --cli or --account says otherwise.
    "spawn_route": "manual",
    # agent-usage: Claude logins (one config dir each), preference order and thresholds. Every key and its
    # default is in agentmail/usage.py DEFAULTS, e.g. {"claude_accounts": {...}, "order": [...]}.
    "usage": {},
    # The pre-send review of agent mail; every key and its default is in agentmail/review.py DEFAULTS.
    "review": {},
    # Group chat (agent-group); every key and its default is in agentmail/groups.py DEFAULTS.
    "groups": {},
    # agent-failover: move a Claude agent to another login when its own hits a usage limit. Overrides go
    # here, e.g. {"enabled": false} or {"pinned": ["long-job-*"]}; every key and its default is in
    # agentmail/failover.py DEFAULTS.
    "failover": {},
    # Every agent agent-spawn starts or agent-send resumes runs in its own systemd scope with these caps
    # (agentmail/memcap.py), so one runaway batch is killed in its own scope instead of exhausting the
    # machine. Sizes like "32G"; "off" for no cap. agent-spawn --memory-max/--swap-max override per launch.
    "agent_memory_max": "32G",
    "agent_swap_max": "2G",
    # agent-memwatch: warn the agents holding the most memory at 80%/90% RAM in use. Overrides go here,
    # e.g. {"warn_pct": 85}; every key and its default is in agentmail/memwatch.py DEFAULTS.
    "memwatch": {},
    # Optional last resort for the orchestrator and the reviewer, after every login in usage.order fails or is
    # out of usage: an Anthropic API key held in Bitwarden Secrets Manager, injected by `bws run`. Set this to
    # the secret's name in your bws project (e.g. "ANTHROPIC_API_KEY"); None disables it. The other two keys are
    # the files holding bws's access token and project id.
    "orchestrator_fallback_secret": None,
    "bws_token_file": "~/.config/bws/token",
    "bws_project_file": "~/.config/bws/project-id",
    # Collaborators' agents: second tier. Their mail is delivered marked as external-agent input,
    # never as the owner's instruction. {address: {"name": ..., "owner": ...}}; the pre-send reviewer of mail to
    # the owner is told each entry's owner (else its address) is a collaborator the owner knows (review.read_prompt).
    "collaborators": {},
    # Honour an allowlisted or collaborator address only if Gmail's Authentication-Results shows
    # a DKIM pass for the sender's domain. A From header alone is trivially forged.
    "require_dkim": True,
    # Flood and loop guards: collaborator mail is held past this many mails an hour from one sender, or past this
    # many agents' messages in a row in its thread.
    "collaborator_hourly_cap": 10,
    "agent_thread_cap": 12,
    # One-line notice to the owner when a collaborator starts a new thread.
    "notify_owner_new_collab_thread": True,
    # Where mailroom alerts go (default: the owner's address, owner_address), and whether they are sent at all.
    "alert_to": None,
    "alerts_enabled": True,
    # False on machines whose sessions the mailroom cannot reach (the Mac): agent-mail send refuses.
    "send_enabled": True,
    # This machine's stable name, and the host_id whose mailroom sends its mail (the Mac's is "desktop"; null:
    # this one sends).
    "host_id": "desktop",
    "mail_proxy": None,
    "remote_hosts": {},  # host_id -> SSH alias (only configured peers are contacted)
    "remote_timeout": 30,   # seconds for one SSH request to a peer
    "remote_files_timeout": 120,   # seconds to copy a Mail-tab reply's files (up to 25 MiB) to a peer's session
    "remote_command": "~/.local/bin/agent-mail rpc",   # what a peer runs over SSH to answer a request
    "recovery_hosts": [],  # searched after the local machine, in order
    # agent-mail recover: seconds per search (at most 20) and days of sessions searched (at most 30).
    "recovery_seconds": 20,
    "recovery_days": 14,
    # agent-mail backup (agentmail/backup.py): the directories it copies the message store and the database to, each
    # on a disk other than the state's. Empty: the command refuses.
    "backup": {"targets": []},
    # The Gmail labels the mailroom puts on the mail it delivered, held or skipped.
    "labels": {
        "delivered": "mailroom/delivered",
        "held": "mailroom/held",
        "skipped": "mailroom/skipped",
    },
}


def owner_email(cfg=None):
    """The owner's personal address: owner_email, else the first allowlist entry, else None."""
    cfg = load() if cfg is None else cfg
    if cfg.get("owner_email"):
        return cfg["owner_email"].strip().lower()
    return cfg["allowlist"][0] if cfg.get("allowlist") else None


def owner_address(cfg):
    """The owner's address, where agents' mail to the owner goes (owner_email); None when none is set. Alerts go to
    alert_to first (alert_address)."""
    return owner_email(cfg)


def alert_address(cfg):
    """Where mailroom alerts go: alert_to, else the owner's address; None when neither is set."""
    return cfg.get("alert_to") or owner_address(cfg)


def is_owner_alias(cfg, value):
    """Whether value (any case, outer spaces ignored) names the owner: OWNER_ALIAS or config owner_name."""
    v = (value or "").strip().lower()
    return bool(v) and v in (OWNER_ALIAS, cfg["owner_name"].lower())


def spawn_models(cfg):
    """{cli: the model agent-spawn gives a new agent of that CLI (SPAWN_KEYS; None: the CLI's own default)}."""
    return {cli: cfg[keys["model"]] for cli, keys in SPAWN_KEYS.items()}


def claude_model_order(cfg):
    """The models a Claude agent may be placed on, best first: spawn_claude_model, then claude_fallback_models
    (none when spawn_claude_model is null: the CLI's default is not a model to step down from). Raises ValueError
    when claude_fallback_models is not a list of model ids."""
    first, rest = cfg.get("spawn_claude_model"), cfg.get("claude_fallback_models") or []
    if not (isinstance(rest, list) and all(isinstance(m, str) and m for m in rest)):
        raise ValueError("config claude_fallback_models must be a list of model ids, not %r" % (rest,))
    return [first] + ([m for m in rest if m != first] if first else [])


def merged(default, user):
    """user's value over default's: a dict merges into a dict key by key, at every depth; any other value replaces."""
    if not (isinstance(default, dict) and isinstance(user, dict)):
        return user
    return dict(default, **{k: merged(default.get(k), v) for k, v in user.items()})


def load():
    """DEFAULTS with config.json (if any) merged over them (merged): a partial dict keeps the other defaults."""
    cfg = copy.deepcopy(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg = merged(cfg, json.loads(CONFIG_PATH.read_text()))
    cfg["account"] = (cfg.get("account") or "").strip().lower() or None
    cfg["allowlist"] = [a.lower() for a in (cfg.get("allowlist") or [])]
    cfg["owner_name"] = (cfg.get("owner_name") or "").strip() or DEFAULTS["owner_name"]
    cfg["collaborators"] = {k.lower(): (v or {}) for k, v in (cfg.get("collaborators") or {}).items()}
    return cfg


def owner_name(cfg=None):
    """The owner's first name for text agents read; "the owner" when unset."""
    cfg = load() if cfg is None else cfg
    return (cfg.get("owner_name") or "").strip() or DEFAULTS["owner_name"]


def owner_possessive(cfg=None):
    """"Alex's", or "the owner's"."""
    return owner_name(cfg) + "'s"


def oauth_client_path(cfg):
    """The OAuth client file agent-mail-auth uses: config oauth_client_file, else CLIENT_SECRETS_PATH."""
    return Path(os.path.expanduser(cfg.get("oauth_client_file") or str(CLIENT_SECRETS_PATH)))


def require_account(cfg):
    """The agents account, or ConfigError with what to set."""
    if not cfg.get("account"):
        raise ConfigError("config `account` is not set: put the agents Gmail address in %s, "
                          'e.g. {"account": "agents@example.com", "allowlist": ["owner@example.com"]}' % CONFIG_PATH)
    return cfg["account"]
