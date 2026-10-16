# mailroom

One shared Gmail account for every Claude Code and Codex session on a machine, with replies
routed back to the exact session that sent the original mail. Around that core sit the tools the
rest of the kit relies on to start, address, watch and close agents.

- Agents send with `agent-mail send` / `agent-mail reply`. Before a mail leaves, the sending
  session gets a stable routing tag, recorded in a local SQLite map.
- The `agent-mailroom` daemon polls the agents mailbox, works out which session a reply belongs to,
  and delivers it into that session with `agent-send` (resuming the session if it has exited).
- Mail that no session claims goes to the **orchestrator**: one fresh `claude -p` run per event in
  the orchestrator directory (the kit's `orchestrator/` template), never a long-lived session.
- `agent-status` hooks record a text-free line per turn, start and end; agents add milestone notes.
  `agent-status report` turns that into a "what are my agents doing" report with no model call.
- `agent-spawn` starts an agent in a fresh git worktree and tmux session with a brief;
  `agent-close` ends it and removes the worktree once its work is landed.

The mailroom never runs a model itself. Only the orchestrator does, and only per event.

How it fits the kit: the installer writes `~/.config/agent-mail/config.json` from `kit.env`, puts a
wrapper for each tool in `~/.local/bin`, and installs the systemd units below. The agent board
reads this config (`owner_name`, `owner_email`, `repos_root`) and calls `agent-send`,
`agent-switch` and `agent-close`; the to-do tools hand tasks to agents through `agent-spawn` or the
orchestrator. The global agent instructions (kit `templates/`) tell every agent how to use
`agent-mail` and `agent-status note`.

## Contents

| Path | What |
|---|---|
| `bin/agent-mail` | Agents **send** mail with it: `send`, `reply N`. People use `inbox [--held]`, `show N`, `release N TARGET`, `rerun N`, `sent`, `status`, `recover N`, `receipt UUID`. |
| `bin/agent-mailroom` | The daemon. Polls Gmail history every `poll_seconds`, walks the routing slip, delivers or holds. `--once`, `--dry-run`, `--baseline`. |
| `bin/agent-send` | Deliver a message to any Claude Code or Codex session on this machine by name or id; `--list` is the address book. |
| `bin/agent-status` | `note "..."` (agents), `report [--format text\|md\|html\|json] [--email TO]`, `log`, and `hook --cli claude\|codex` (the hook entry point). |
| `bin/agent-orchestrate` | Run the orchestrator once on one event; `--runs` lists cost and outcome per run. |
| `bin/agent-spawn` | Start an agent: `--repo R --branch B` (new worktree), `--new-repo NAME`, or `--dir PATH`; brief via `--brief FILE`, `--brief-text`, or stdin. |
| `bin/agent-close` | End an agent's session and remove its worktree: `agent-close NAME` or, from inside, `agent-close --self`. Refuses on unlanded work unless `--force`. |
| `bin/agent-switch` | Switch a live agent to another CLI, Claude login, model or effort (the board's right-click menu). Optional. |
| `bin/agent-usage` | Plan usage left on each Claude login and on Codex; `--pick`; `init-account` prepares a second Claude login. Optional. |
| `bin/agent-failover` | Daemon: moves a Claude agent to another login when its own hits a usage limit, and back after the reset. Optional. |
| `bin/agent-memwatch` | Daemon: warns the agents holding the most memory before the machine runs out. Optional. |
| `agent_mail_auth.py` | One-time OAuth consent for the agents account; installed as the `agent-mail-auth` wrapper. |
| `agentmail/` | Shared code: `config`, `gmail` (API layer), `routing` (tags, matching, quote stripping, message headers), `store` (SQLite routing map), `deliver`, `alerts`, `recovery`, `remote` (Mac proxy transport), `orchestrate`, `compose` (text to HTML), `registry` (both CLIs' session registries), `statuslog`, `report`, `close`, `usage`, `failover`, `switch`, `memcap`, `memwatch`. |
| `systemd/*.service.in` | `systemd --user` unit templates for `agent-mailroom`, `agent-failover`, `agent-memwatch`. |
| `tests/` | Unit tests (see [Tests](#tests)). |
| `requirements.txt` | The Google client libraries. Everything else is Python 3.9+ standard library. |

## How a reply finds its session

### The tag

Each session that sends mail gets a stable 8-character tag (e.g. `k7mq2xbe`), stored with its
identity `(host, cli, session id)` before the mail leaves. The mail carries the tag three ways:

- `Reply-To: <from_name> <agents+k7mq2xbe@example.com>` (a Gmail plus-address of `account`);
- `[#k7mq2xbe]` at the end of the subject;
- an `X-Agent-Mailroom: k7mq2xbe` header.

The From line is `<from_name> (<session name>)`, so the owner sees which agent wrote. Inside Claude
Code the sending session comes from `CLAUDE_CODE_SESSION_ID`. Codex has no such variable: a Codex
agent names itself with `/rename NAME` and passes `--session NAME` to `agent-mail`.

### The routing slip

For each new message in the inbox, in decreasing trust, the first hit wins:

1. A copy of mail the agents account sent itself: skipped.
2. Sender not in `allowlist` or `collaborators`: **held**, never delivered anywhere.
3. Sender DKIM check fails (see [Sender tiers](#sender-tiers-and-dkim)): held, owner alerted.
4. A reply or forward to a `[mailroom]` notice (or in a saved alert thread): the orchestrator,
   ahead of every session hint, since notices quote the original `[#TAG]`.
5. `In-Reply-To` / `References` names a Message-ID the mailroom sent: that session.
6. The Gmail thread is one a session sent in: that session.
7. `agents+TAG@` in To / Cc / Delivered-To: the session that owns TAG.
8. `[#TAG]` in the subject: the session that owns TAG.
9. Nothing matched, sender known: the orchestrator.

A reply in a thread that only ever held `agent-mail send --unrouted` mail (status reports, memory
warnings, cloud-watchdog) belongs to the orchestrator. A reply that looks like it answers a
session's mail but cannot be matched is an *unowned reply*: it is held, a bounded read-only
recovery search runs (`agent-mail recover N`), and the owner gets one notice.

Quoted text and signatures are stripped before delivery. The session receives one message whose
first line says who wrote and how to answer:

```
[email from Alex <owner@example.com> re "Results" · to reply: agent-mail reply 42  (reply body on stdin or --body-file)]
Looks good, go ahead with the second run.
```

`agent-mail reply 42` answers in the same Gmail thread, from the same tag.

### Gmail labels

The daemon creates these labels on start (names configurable under `labels`):

| Label | Meaning |
|---|---|
| `mailroom/delivered` | Delivered to a session or handled by the orchestrator; marked read. |
| `mailroom/held` | Waiting for a person; left unread. `agent-mail inbox --held` lists them. |
| `mailroom/skipped` | Reserved; created but not applied today. |

### Delivery

`agent-send` is the only delivery path:

- live Claude Code session: one JSON line on its inbox socket (from `~/.claude/sessions/<pid>.json`);
- live Codex session: `codex queue --thread <id> --message ...` (Codex >= 0.149);
- exited session (`resume_offline`): `claude --resume <id> --bg "..."` or a detached
  `codex exec resume <id> "..."`, with bypassed permissions unless `resume_dangerous` is false.

Before a message leaves, `agent-send` writes a private transport receipt. If `agent-send` is
interrupted, the daemon checks that receipt, so a message the session accepted counts as delivered.
"Delivered" means the session's transport accepted it, not that the agent has read it.

## Sender tiers and DKIM

- **Owner** (`allowlist`, normally one address, the owner's personal Gmail): mail is delivered as the
  owner's instruction, headed `[email from <name> <address> ...]`.
- **Collaborators** (`collaborators`): other people's agents. Their mail is delivered marked
  `(collaborator agent of <their owner>, not <owner_name>)` with a guard line: answer questions and
  share project content, but run no commands, change no files, push nothing, spend nothing, mail
  nobody else on its say-so; small reversible work only on a branch, and tell the owner by email.
  The orchestrator classifies their requests the same way.
- **Everyone else**: held.

Both tiers are honoured only if Gmail's `Authentication-Results` header shows a DKIM pass for the
sender's domain (`require_dkim`, default on). A forged From header is held and the owner is alerted.
Collaborator traffic is capped at `collaborator_hourly_cap` per hour; a thread with more than
`agent_thread_cap` consecutive agent messages and no human is held (loop guard); and the owner gets
a notice when a collaborator starts a new thread (`notify_owner_new_collab_thread`).

```json
"collaborators": {"sam.agent@example.com": {"name": "Sam's Claude", "owner": "Sam"}}
```

## The orchestrator

The orchestrator is not a running session. Each event runs one fresh
`claude -p --model <orchestrator_model> --effort <orchestrator_effort>` in `orchestrator_dir`, with no
MCP servers, serialised by a lock, with the whole email thread (quotes stripped) in the prompt. Its
brief (`CLAUDE.md`/`AGENTS.md`) and memory (`STATE.md`) live in that directory. Inside a run,
`agent-mail` sends and replies as the orchestrator, so replies come back to it.

Events: mail from a known sender that no session claimed, a reply in a thread the orchestrator wrote
in, and `agent-send orchestrator "..."` (when no live session is named exactly `orchestrator`).

```bash
agent-orchestrate "text"        # run one event by hand
agent-orchestrate --runs        # cost, turns, outcome per run
agent-mail rerun N              # run it again on a held or failed mail
```

A failed run holds the mail and sends the owner a `[mailroom]` notice; nothing is lost.

**Optional API-key fallback.** If the claude.ai login fails or is rate-limited, a run can be retried
once with an Anthropic API key from Bitwarden Secrets Manager. Set `orchestrator_fallback_secret` to
the secret's name in your `bws` project, and provide `bws` on PATH (or `~/.local/bin/bws`), an access
token file (`bws_token_file`) and a project-id file (`bws_project_file`). `bws run` injects the
project's secrets into a child shell, which maps that name onto `ANTHROPIC_API_KEY` and execs
`claude`; the key never passes through the mailroom, a file or a log. Unset (the default), there is
no fallback.

## Status log, hooks and the report

Hooks write one line per event, with no message text, to
`~/.local/state/agent-status/events.jsonl`: `idle` (Stop), `start` (SessionStart), `end`
(SessionEnd), `limit` / `api-error` (StopFailure, Claude only) and `quota` (the Notification types
`quota_auto_resume_*`, Claude only). Agents add milestones themselves with
`agent-status note "one line"`. The daemon's watchdog alerts the owner on `quota` events.

**Claude Code**, in `~/.claude/settings.json` (merge into existing `hooks`):

```json
{
  "crossSessionInbound": "accept",
  "hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20, "async": true}]}],
    "SessionStart": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20, "async": true}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20}]}],
    "StopFailure": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20, "async": true}]}],
    "Notification": [{"matcher": "quota_auto_resume_fired|quota_auto_resume_stale|quota_auto_resume_disabled",
                      "hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20, "async": true}]}]
  }
}
```

`"crossSessionInbound": "accept"` is required: without it, a Claude session running with bypassed
permissions holds every message from outside (the mailroom, `agent-send`) for approval and drops it
after five minutes. Sessions read it at launch, so restart sessions that were already running.

**Codex**, in `~/.codex/hooks.json` (Codex has no StopFailure or quota Notification events):

```json
{
  "hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli codex", "timeout": 20}]}],
    "SessionStart": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli codex", "timeout": 20}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli codex", "timeout": 3}]}]
  }
}
```

Codex may ask once to trust a new hooks file. The hook never fails and never prints: a broken status
log cannot disturb an agent.

```bash
agent-status note "E2 finished: 0.81 accuracy, results in results/e2.json"
agent-status report                                  # text, last 24 h
agent-status report --format html --email owner@example.com
agent-status log --since 2h --event note
```

## Sending mail from a session

```bash
printf 'Results below...\n' | agent-mail send --to owner@example.com --subject "E6 results"
agent-mail send --to X --subject S --body-file body.txt --html body.html   # your own HTML part
agent-mail send --to X --subject S --session my-codex-name --body-file b.txt   # from Codex
agent-mail reply 7 < answer.txt                                           # answer inbound #7 in-thread
agent-mail send --unrouted --to X --subject S < report.txt                # no session; replies go to the orchestrator
```

Every mail is multipart/alternative: the text as given, plus an HTML part rendered from it
(paragraphs, bullet and numbered lists, nested items, fenced code, `**bold**`, `` `code` ``; all
escaped first) unless `--html` supplies one. Threading and routing never depend on the HTML.

## agent-send

```bash
agent-send --list                              # address book: both CLIs, live or not
agent-send my-agent "run the tests"            # by name, id, or unambiguous prefix
agent-send orchestrator "..."                  # runs one orchestrator event
agent-send --from mailroom TARGET < body.txt   # message on stdin
agent-send --offline resume TARGET "..."       # wake a session that is not live
agent-send --dry-run TARGET "..."              # resolve and show, send nothing
```

A target is matched in order: exact name, exact id, id prefix (6+ characters), name prefix, then
the start of an unnamed session's title. The orchestrator's name only ever matches exactly. The
body starts with a `[from ... via agent-send; ...]` line saying whether a reply address exists.
Exit codes: 0 sent, 1 error, 2 offline, 3 ambiguous, 4 not found.

## agent-spawn and agent-close

```bash
agent-spawn --repo myproject --branch fix-login --brief-text "Fix the login redirect; open a PR."
agent-spawn --new-repo scratch-idea --brief brief.md          # private repo under github_owner
agent-spawn --dir ~/code/notes --cli codex < brief.md
agent-spawn --repo myproject --branch x --dry-run --brief-text "..."   # print the plan only
```

- `--repo NAME` is looked up in `repos_root` (or give a path). The worktree goes next to the repo at
  `<parent>/<repo>-<branch>`, branched off `origin/main`; the tmux session is named after the branch.
- `--new-repo NAME` runs `gh repo create <github_owner>/NAME --private --clone` in `repos_root`.
  `--org OWNER` overrides `github_owner`; with neither, it refuses.
- `AGENT_SPAWN_ROOT` in the environment overrides `repos_root`.
- Defaults: Claude `spawn_claude_model` / `spawn_claude_effort`, Codex `spawn_codex_model` /
  `spawn_codex_effort`; `--model` / `--effort` per task. Claude runs with
  `--dangerously-skip-permissions`, Codex with `-s danger-full-access -a never`; the repo is
  pre-trusted for both CLIs.
- The brief is prefixed with a protocol preamble: who the agent works for (`owner_name`), to record
  milestones with `agent-status note`, to email `owner_email` only when done or blocked, and how to
  close itself. `--no-preamble` sends the brief as given.
- Attach with `tmux attach -t NAME`.

When the owner replies "close" (in any words) in an agent's thread, the agent runs
`agent-close --self --dry-run`, reports unlanded work if any, and otherwise ends with
`agent-close --self`. It refuses (exit 2) while the worktree has uncommitted or untracked files or
commits not on origin's default branch; `--force` overrides. Only a linked worktree is removed.

## Optional: a second Claude login, usage routing and failover

**Second login.** A second Claude login is its own `CLAUDE_CONFIG_DIR`.
`agent-usage init-account work --config-dir ~/.claude-work` creates it, sharing `CLAUDE.md`,
settings, skills, hooks, plugins and commands with `~/.claude` by symlink while keeping
credentials, sessions and transcripts separate. Then log in once yourself
(`CLAUDE_CONFIG_DIR=~/.claude-work claude`, then `/login`) and add to the config:

```json
{"usage": {"claude_accounts": {"personal": {"config_dir": null, "label": "Claude (personal)"},
                               "work": {"config_dir": "~/.claude-work", "label": "Claude (work)",
                                        "only_owners": ["employer-org"]}},
           "order": ["claude:personal", "claude:work", "codex"],
           "min_remaining": {"session": 10, "weekly": 5}},
 "spawn_route": "auto"}
```

`config_dir: null` is the default login (`~/.claude`). `exclude_owners` / `only_owners` restrict a
login by the repo's GitHub owner (e.g. keep employer repos off a personal plan).

**agent-usage** reads plan usage left (percent remaining per window) from each provider's server
through the CLIs themselves: Claude Code's `get_usage` control request and Codex's app-server
`account/rateLimits/read`. No prompt is sent, no quota spent; readings are cached for 2 minutes.
`agent-usage --pick` prints the recommended target. With `spawn_route: "auto"` (or
`agent-spawn --account auto`), agent-spawn picks the first target in `usage.order` with headroom;
`--account NAME|codex` picks explicitly. Usage keys and defaults: `agentmail/usage.py` `DEFAULTS`.

**agent-failover** (service, one pass every 20 s) watches live Claude agents' transcripts. When a
plan limit or a used-up per-model bucket stops a turn, it moves the agent to the first login in
`usage.order` with headroom for the model it runs: SIGKILL (background jobs keep running), copy the
transcript, trust the folder, resume the same session id in the same tmux pane with a note. Mail
replies still reach it. Once the original login resets and the agent is idle, it moves back.
Only agent-spawn's tmux layout and `claude --bg` sessions are moved; at most 4 moves per agent per
hour; Codex agents are never moved. `failover.pinned` (shell globs of session names) keeps agents
on their login. `agent-failover move NAME --to work`, `agent-failover status`,
`agent-failover --once --dry-run`. Settings: config key `failover`
(`agentmail/failover.py` `DEFAULTS`); `{"enabled": false}` turns it off.

**agent-switch** moves a live agent to another login, model or effort (same conversation), or hands
it over between Claude Code and Codex (a handoff record in `~/.local/state/agent-switch/`, mail
routing moved to the new session). A busy agent needs `--force`.

```bash
agent-switch NAME --options --json
agent-switch NAME --to claude:work --model opus --effort high
agent-switch NAME --to codex --force
```

## Optional: memory caps and agent-memwatch

One agent's batch of parallel workers can fill RAM and swap, after which the kernel kills services
one by one and finally the user systemd manager, ending every agent. Two defences:

- **Caps.** Every agent `agent-spawn` starts or `agent-send` resumes runs in its own systemd scope,
  `agent-NAME.scope`, with `MemoryMax=agent_memory_max` (default `32G`) and
  `MemorySwapMax=agent_swap_max` (default `2G`); `"off"` disables either. A runaway is OOM-killed
  inside its own scope. `agent-spawn --memory-max 48G --swap-max off` overrides per launch; raise a
  running agent's cap with `systemctl --user set-property --runtime agent-NAME.scope MemoryMax=48G`.
  The tmux server and Claude's background daemon are moved out of any service's cgroup (into
  `tmux-server.scope` / `claude-bg-daemon.scope`), so restarting the mailroom cannot kill agents.
  A cap that cannot be applied never blocks a launch. No-op without a systemd user bus (macOS).
  Not covered: sessions started by hand, Docker containers (charged to `dockerd`), many agents that
  each stay under their cap.
- **agent-memwatch** (service, every 15 s) charges each process's memory to the agent it descends
  from. At 80% RAM in use it messages the top agents (via `agent-send`) to cut workers; at 90% or
  swap half full it messages them again and emails the owner; an agent near its own scope cap is
  warned first. `agent-memwatch --dry-run` shows who holds what. Settings: config key `memwatch`
  (`agentmail/memwatch.py` `DEFAULTS`, e.g. `{"warn_pct": 85}`).

## Optional: a Mac that proxies through the Linux box

There is one polling daemon, on the always-on Linux box. A Mac with its own Claude/Codex sessions
can still send and receive: its `agent-mail send` / `reply` proxy over SSH to the Linux box, which
records the tag against `(mac, cli, session id)` and sends; replies go back over SSH into the exact
live Mac session (never resumed automatically). Install the kit on both machines (the Mac needs no
Gmail token, but `agent-mail` imports the Google libraries, so install `requirements.txt` there too).

Linux box `~/.config/agent-mail/config.json`:

```json
{"host_id": "desktop", "remote_hosts": {"mac": "mac"}, "recovery_hosts": ["mac"]}
```

Mac `~/.config/agent-mail/config.json`:

```json
{"host_id": "mac", "mail_proxy": "desktop", "remote_hosts": {"desktop": "desktop"}, "send_enabled": false}
```

`remote_hosts` maps a host id to an SSH alias; both aliases must connect without prompts and with
known host keys (`BatchMode=yes`, `StrictHostKeyChecking=yes`). Payloads are JSON on stdin to
`~/.local/bin/agent-mail rpc`, never shell-interpolated. A proxy send prints a request UUID; after a
timeout, run `agent-mail receipt UUID` before retrying anything. If the Mac is offline or the
session is gone, the reply stays held and the owner gets one notice; then:

```bash
agent-mail show 123
agent-mail release 123 origin                                      # retry the recorded origin
agent-mail release 123 FULL_SESSION_UUID --host mac --cli codex    # explicit handoff
agent-mail recover 123 --query 'exact document title' --refresh    # read-only search, this machine then recovery_hosts
```

Recovery searches session metadata, status notes, transcripts and text docs in recent worktrees,
within fixed time, byte and file budgets, and returns only paths, identities and matching queries.
Matches are leads, never permission to route.

## Configuration

`~/.config/agent-mail/config.json` (no secrets; override the directory with `AGENT_MAIL_CONFIG_DIR`).
Keys left out take these defaults (`agentmail/config.py`):

| Key | Default | Meaning |
|---|---|---|
| `account` | `null` (**required**) | The dedicated agents Gmail address. Sending and polling refuse until it is set. |
| `from_name` | `"Agents"` | From display name: `<from_name> (<session name>)`. |
| `allowlist` | `[]` | Owner addresses whose mail is delivered as instructions. Empty: every mail is held. |
| `owner_name` | `"the owner"` | The owner's first name, used in text agents read. |
| `owner_email` | `null` | Where agents send results and alerts go; `null`: first `allowlist` entry. |
| `github_owner` | `null` | GitHub user/org for `agent-spawn --new-repo` (required for it). |
| `repos_root` | `"~/code"` | Where repos and new repos live (`AGENT_SPAWN_ROOT` overrides). |
| `general_repo` | `"~/code/general"` | Repo for tasks that belong to no project (read by the orchestrator brief and to-do tools). |
| `poll_seconds` | `30` | Gmail poll interval. |
| `resume_offline` | `true` | Resume an exited session to deliver a reply. |
| `resume_dangerous` | `true` | Resume with bypassed permissions / full-access sandbox. |
| `orchestrator` | `"orchestrator"` | The orchestrator's name for `agent-send` and in the report. |
| `orchestrator_mode` | `"exec"` | `exec`: one `claude -p` per event. `session`: deliver to a live session of that name. |
| `orchestrator_dir` | `"~/code/orchestrator"` | Working directory of orchestrator runs (brief, `STATE.md`). |
| `orchestrator_model` | `"sonnet"` | Model alias for orchestrator runs. |
| `orchestrator_effort` | `"high"` | Effort for orchestrator runs. |
| `orchestrator_timeout` | `900` | Seconds per orchestrator attempt (and lock wait). |
| `orchestrator_fallback_secret` | `null` | Name of a `bws` secret holding an Anthropic API key; `null` disables the fallback. |
| `bws_token_file` | `"~/.config/bws/token"` | Bitwarden Secrets Manager access token file (fallback only). |
| `bws_project_file` | `"~/.config/bws/project-id"` | File with the `bws` project id (fallback only). |
| `spawn_claude_model` | `"claude-opus-5-5"` | agent-spawn's Claude model (`null`: CLI default). |
| `spawn_claude_effort` | `"xhigh"` | agent-spawn's Claude effort. |
| `spawn_codex_model` | `"gpt-6.1-sol"` | agent-spawn's Codex model. |
| `spawn_codex_effort` | `"xhigh"` | agent-spawn's Codex effort. |
| `spawn_route` | `"manual"` | `auto`: with no `--cli/--account/--model`, pick CLI and login by usage. |
| `usage` | `{}` | Claude logins, order and thresholds for agent-usage (`agentmail/usage.py`). |
| `failover` | `{}` | agent-failover overrides (`agentmail/failover.py`), e.g. `{"enabled": false}`. |
| `memwatch` | `{}` | agent-memwatch overrides (`agentmail/memwatch.py`). |
| `agent_memory_max` | `"32G"` | Per-agent MemoryMax; `"off"` for none. |
| `agent_swap_max` | `"2G"` | Per-agent MemorySwapMax; `"off"` for none. |
| `collaborators` | `{}` | `{address: {"name", "owner"}}` of collaborators' agents. |
| `require_dkim` | `true` | Honour owner/collaborator mail only with a DKIM pass for the sender's domain. |
| `collaborator_hourly_cap` | `10` | Collaborator mails per hour before holding. |
| `agent_thread_cap` | `12` | Consecutive agent messages in a thread before the loop guard holds it. |
| `notify_owner_new_collab_thread` | `true` | Notice to the owner when a collaborator starts a thread. |
| `alert_to` | `null` | Where `[mailroom]` alerts go; `null`: `owner_email`. |
| `alerts_enabled` | `true` | Send `[mailroom]` alerts at all. |
| `send_enabled` | `true` | `false` on a machine that must not send directly (the Mac). |
| `host_id` | `"desktop"` | This machine's identity in routing. |
| `mail_proxy` | `null` | Host id of the central mailroom (set on the Mac). |
| `remote_hosts` | `{}` | Host id to SSH alias for peers. |
| `remote_timeout` | `30` | Seconds per SSH operation. |
| `recovery_hosts` | `[]` | Hosts searched after this one by `agent-mail recover`. |
| `recovery_seconds` | `20` | Time budget per recovery search (max 20). |
| `recovery_days` | `14` | How far back recovery looks (max 30). |
| `labels` | `{"delivered": "mailroom/delivered", "held": "mailroom/held", "skipped": "mailroom/skipped"}` | Gmail label names (replace the whole object to change one). |

A minimal config:

```json
{"account": "agents@example.com", "from_name": "Agents", "allowlist": ["owner@example.com"],
 "owner_name": "Alex", "owner_email": "owner@example.com", "github_owner": "example-user",
 "repos_root": "~/code", "orchestrator_dir": "~/code/orchestrator"}
```

### Files

| Path | What |
|---|---|
| `~/.config/agent-mail/credentials.json` | OAuth client (a Google Cloud "Desktop app" client JSON). Read only by `agent-mail-auth`. |
| `~/.config/agent-mail/token.json` | Refresh token for the agents account, mode 600, written by `agent-mail-auth`. Scope `gmail.modify` only. |
| `~/.config/agent-mail/config.json` | The config above. |
| `~/.local/state/agent-mail/mailroom.sqlite` | Routing map, inbound/outbound records (`AGENT_MAIL_STATE_DIR`). |
| `~/.local/state/agent-mail/orchestrator-runs.jsonl` | One line per orchestrator run. |
| `~/.local/state/agent-mail/{usage-cache,failover,memwatch}.json` | Optional services' state. |
| `~/.local/state/agent-status/events.jsonl` | The status log (`AGENT_STATUS_DIR`). |
| `~/.local/state/agent-spawn/`, `agent-send/`, `agent-switch/` | Briefs, resume logs, handoff records (mode 700). |

## Manual install

The kit's `install.sh` does all of this; by hand:

1. Python environment: `python3 -m venv ~/.local/share/agent-kit/venv` and
   `~/.local/share/agent-kit/venv/bin/pip install -r mailroom/requirements.txt`.
2. Wrappers in `~/.local/bin`, one per tool in `bin/` (`agent-mail`, `agent-mailroom`, `agent-send`,
   `agent-status`, `agent-orchestrate`, `agent-spawn`, `agent-close`, `agent-switch`, `agent-usage`,
   `agent-failover`, `agent-memwatch`) plus `agent-mail-auth` for `agent_mail_auth.py`:

   ```sh
   #!/bin/sh
   exec "$HOME/.local/share/agent-kit/venv/bin/python" "/path/to/agent-board-kit/mailroom/bin/agent-mail" "$@"
   ```

   Tools call their siblings with the same interpreter, and the SSH proxy calls
   `~/.local/bin/agent-mail` on the peer, so the wrappers must be at exactly these paths.
3. `~/.config/agent-mail/config.json` with at least `account`, `allowlist`, `owner_name`,
   `owner_email` (see above); `mkdir -m 700 ~/.config/agent-mail`.
4. Put the OAuth client JSON at `~/.config/agent-mail/credentials.json`, then run
   `agent-mail-auth` and sign in as the **agents** account. It refuses any other account. On a
   headless machine: `agent-mail-auth --port 8765 --no-browser`, forward the port from your laptop
   with `ssh -L 8765:localhost:8765 <host>`, and open the printed URL there.
5. Hooks and `crossSessionInbound` as in [Status log, hooks and the report](#status-log-hooks-and-the-report).
6. The orchestrator directory (`orchestrator_dir`) from the kit's `orchestrator/` template.
7. Services: render each `systemd/*.service.in`, replacing `{{PYTHON}}` with the venv's python and
   `{{KIT_DIR}}` with the kit checkout, into `~/.config/systemd/user/`, then:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now agent-mailroom.service
   systemctl --user enable --now agent-memwatch.service     # optional
   systemctl --user enable --now agent-failover.service     # optional, needs a second login
   loginctl enable-linger "$USER"                           # keep user services running without a login
   ```

8. Check: `agent-mail status`, then `agent-mailroom --once --dry-run` (run it while the service is
   stopped; only one instance can hold the lock).

### The unit files

- `agent-mailroom.service`: `KillMode=mixed` sends SIGTERM to the daemon only. Its handler finishes
  the current message (including a running `agent-send` or orchestrator child), records the outcome,
  and exits without starting another; unprocessed messages stay behind the saved history cursor for
  the next poll. After the daemon exits (or the deadline passes) systemd kills what remains.
  `TimeoutStopSec=1h` covers the worst case: an event waiting for the orchestrator lock, then two
  15-minute attempts. If you raise `orchestrator_timeout`, raise this too. Agents themselves are not
  in this cgroup (see memory caps), so a restart does not touch them.
- `agent-failover.service`: `KillMode=process`, so a restart never takes the agents it relaunched
  with it; `MemoryMax=256M`.
- `agent-memwatch.service`: a separate unit, so it outlives a mailroom crash; `MemoryMax=256M`.
- All three set `Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin`, because
  `systemd --user` does not read shell profiles and `claude`, `codex` and the wrappers live in
  `~/.local/bin`.

After changing a unit, `systemctl --user daemon-reload` **before** restarting, and check
`systemctl --user show agent-mailroom.service -p KillMode -p TimeoutStopUSec`.

## Requirements

- Linux with `systemd --user` for the services and memory caps (the tools themselves also run on
  macOS); Python 3.9+; `git`, `tmux`; `gh` for `--new-repo`.
- Claude Code and/or Codex. Codex >= 0.149 for `codex queue`.
- `"crossSessionInbound": "accept"` in `~/.claude/settings.json`.
- Neither CLI is on PATH in cron, systemd or a non-login SSH shell; `agent-send` and friends look in
  `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, then ask a login shell.

## Tests

The suites mutate module globals, so run each separately. Point every state and config directory at
a scratch location; no test needs Gmail, the network, a real session, tmux or systemd:

```bash
cd mailroom
export HOME=$(mktemp -d) AGENT_MAIL_CONFIG_DIR=$(mktemp -d) AGENT_MAIL_STATE_DIR=$(mktemp -d) AGENT_STATUS_DIR=$(mktemp -d)
for t in tests/test_*.py; do echo "== $t"; python3 "$t" || break; done
```

`test_memcap.py`'s `RealScope` class (2 tests) starts real scopes in the systemd user manager and is
skipped unless `MEMCAP_REAL_TESTS=1`. The Gmail-facing tests (`test_collab`, `test_compose`,
`test_remote`, `test_failure_notices`) import the Google libraries, so use the kit venv's python.

## Troubleshooting

| Symptom | Check |
|---|---|
| Error: config `account` is not set | Add `account` to `~/.config/agent-mail/config.json`. |
| `no Gmail token at ...` | Run `agent-mail-auth` once, signed in as the agents account. |
| `invalid_grant` about a week after setup | An OAuth consent screen left in "Testing" status expires refresh tokens after 7 days. Set it to "In production" (the unverified-app warning remains; fine for your own accounts) and rerun `agent-mail-auth`. |
| Every mail is held: `sender not on allowlist` | Add the owner's address to `allowlist`; then `agent-mail inbox --held` and `agent-mail release N TARGET`. |
| `DKIM check failed` | The sender's domain did not DKIM-sign, or a forwarding hop broke the signature. Such mail is never released (`agent-mail release` refuses it); send it again directly from your provider. Persistent for a real sender: `require_dkim: false` (weakens forgery protection). |
| A Claude session never receives messages | `crossSessionInbound: "accept"` missing, or the session started before it was set. |
| A queued Codex message never appears | Codex older than 0.149, or a dialog (e.g. a rate-limit prompt) is open in the session. Try `codex app-server daemon start` and relaunch the session. |
| `agent-send` exit 3 (ambiguous) | Two live sessions share a name or prefix; use the full id from `agent-send --list`. |
| Orchestrator mail held, "orchestrator failed" | `agent-orchestrate --runs`; does `orchestrator_dir` exist? Is `claude` logged in? Then `agent-mail rerun N`. |
| Daemon down | `systemctl --user status agent-mailroom`, `journalctl --user -u agent-mailroom -f -o cat`, `agent-mail status`. |
| Old mail suddenly processed / history gap | The daemon falls back to a 2-day search when Gmail's history id expires. `agent-mailroom --baseline` (service stopped) resets the cursor to now. |
| A reply was not delivered to a Mac session | `agent-mail show N`; Mac reachable with `ssh mac true`? Then `agent-mail release N origin`. |
| Agent killed at its memory cap | Relaunch with a bigger `--memory-max`, or raise `agent_memory_max`. |
