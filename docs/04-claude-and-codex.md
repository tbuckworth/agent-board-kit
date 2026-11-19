# 4. Claude Code and Codex

## Logins

- **Claude Code**: run `claude` once on the Linux machine and `/login` with your Claude plan
  (or set `ANTHROPIC_API_KEY` for API billing). The orchestrator runs `claude -p` under the same
  login.
- **Codex** (optional): `codex login`. Agents can run on either CLI: `agent-spawn --cli codex`.

## What the installer changes in their settings

`~/.claude/settings.json` gets (merged, with a backup):

```json
{
  "crossSessionInbound": "accept",
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20, "async": true}]}],
    "Stop":         [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20, "async": true}]}],
    "StopFailure":  [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20, "async": true}]}],
    "SessionEnd":   [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20}]}],
    "Notification": [{"matcher": "quota_auto_resume_fired|quota_auto_resume_stale|quota_auto_resume_disabled",
                      "hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20, "async": true}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-group hook --cli claude", "timeout": 20}]}]
  }
}
```

- **`crossSessionInbound: "accept"`** lets an outside process (the mailroom, a peer agent, the
  board) put a message into a running session. Without it, a session in bypass-permissions mode
  holds such messages for approval and drops them after five minutes. Sessions read this at launch.
- **The hooks** write one text-free line per event to `~/.local/state/agent-status/events.jsonl`
  (no model call, no message content). The board and the status report are built from it.
  `StopFailure` records usage-limit stops.
- **`agent-group hook`** (on `UserPromptSubmit`) tells a session, at its next prompt, about group-chat
  posts that did not wake it ("Not an error: N quiet posts in ..."). It prints nothing otherwise and
  never fails. See [group chats](05-daily-use.md#group-chats).

`~/.codex/hooks.json` gets the same `agent-status hook --cli codex` on `SessionStart`, `Stop` and
`SessionEnd`, and `agent-group hook --cli codex` on `UserPromptSubmit`. Running sessions load hooks at
launch: restart them after an update that adds one.

**Codex asks you to review hooks** ("Hooks need review") whenever its hooks file is new or changed,
until you trust them in Codex's `/hooks` screen. Do that once, in a session you start yourself.
`agent-spawn`, `agent-switch`, the board's Resume and the post-restart restore start Codex unattended:
when that question appears they answer "Continue without trusting", so the agent never stalls on it (its hooks
then stay off for that session, and the board says how many). `agent-spawn` also refuses to start
Codex in a worktree whose `.codex/hooks.json` is missing from the repo's main checkout, since Codex
would read different hooks there.

`~/.claude/CLAUDE.md` gets one line, `@~/.config/agent-kit/AGENTS.agents.md`, and
`~/.codex/AGENTS.md` a marked block with the same text: the [global agent
instructions](../templates/AGENTS.agents.md) that teach every session how to mail you, take
replies, write status notes, use the to-do list and close itself.

## How agents are launched

`agent-spawn` starts Claude Code with `--name NAME --dangerously-skip-permissions` (Codex with its
full-access equivalent) inside tmux, because an agent working unattended cannot answer permission
prompts. The orchestrator also runs `claude -p --dangerously-skip-permissions`. Read
[08-security.md](08-security.md) for what that means and how the kit limits who can instruct them.

Defaults (in `~/.config/agent-mail/config.json`, override per launch with `--model`/`--effort`):

| key | default |
|---|---|
| `spawn_claude_model`, `spawn_claude_effort` | `null` (Claude Code's own default model), `xhigh` |
| `claude_fallback_models` | `[]` (no models to step down to) |
| `spawn_codex_model`, `spawn_codex_effort` | `gpt-6.1-sol`, `xhigh` |
| `orchestrator_model`, `orchestrator_effort` | `sonnet` (follows the latest Sonnet), `high` |

A model key set to `null` lets the CLI choose. Effort `xhigh` is expensive on a small plan; `high` or
`medium` is a sensible start.

**To pin models, and step down when one runs out**, name them with full ids, best first. For example,
Fable 5.2 first and Opus 5.5 when a login's Fable allowance is used up:

```json
{"spawn_claude_model": "claude-fable-5-2", "claude_fallback_models": ["claude-opus-5-5"]}
```

Full ids keep a new release from changing what spawned agents run. With several models listed,
routing and failover follow the [login and model order](#the-login-and-model-order) below. Restart
`agent-failover` after changing them (`systemctl --user restart agent-failover`, if you run it).

**Your own house rules** (for example "write a failing test before each fix") go in your own
`~/.claude/CLAUDE.md` or `~/.codex/AGENTS.md`, outside the kit's block: the installer re-renders the
kit's instructions on every run.

## Optional: a second Claude login, and routing by remaining usage

If you have two Claude plans (say personal and work), each login is its own `CLAUDE_CONFIG_DIR`:

```bash
agent-usage init-account work --config-dir ~/.claude-work   # shares CLAUDE.md, settings, skills by symlink
CLAUDE_CONFIG_DIR=~/.claude-work claude                      # then /login with the other plan
```

Then add to `~/.config/agent-mail/config.json`:

```json
{"usage": {"claude_accounts": {"personal": {"config_dir": null, "label": "Claude (personal)"},
                               "work": {"config_dir": "~/.claude-work", "label": "Claude (work)",
                                        "only_owners": ["your-employer-org"]}},
           "order": ["claude:personal", "claude:work", "codex"],
           "min_remaining": {"session": 10, "weekly": 5}},
 "spawn_route": "auto"}
```

- `agent-usage` shows the percent left in each window of each login and recommends where the next
  agent should go (it asks the CLIs; no prompt is sent, no quota used).
- `spawn_route: "auto"` makes `agent-spawn` pick by usage when no `--cli`/`--account`/`--model`
  is given. `only_owners` / `exclude_owners` keep a GitHub owner's repos on (or off) a login.
- `agent-failover` (systemd unit `agent-failover.service`) moves a Claude agent that hits a usage
  limit mid-task to a login with headroom, in the same tmux pane and conversation, and back once
  the first login resets. It also sees a limit hidden behind later queued messages and the end of
  Claude Code's short grace allowance. Enable it only if you configured two logins:
  `systemctl --user enable --now agent-failover`.
- `agent-switch NAME --to claude:work|codex --model M --effort E` moves an agent by hand (the
  board's right-click menu uses it).

### The login and model order

One order places Claude agents, built from `usage.order` and your models (`spawn_claude_model`, then
`claude_fallback_models`):

1. each Claude login in `usage.order` on the first model;
2. each again on the next model, and so on;
3. Codex, before all of them if `usage.order` lists it before every Claude login, else after them;
4. only when no place has headroom: the first Claude login whose extra usage is on (and under its
   spend limit), on the last model, where the agent bills extra usage.

`agent-spawn` (routing by usage), `agent-failover` and the orchestrator all use it. A failover never
moves an agent to a better model than the one it started on, and moves it back once its first login
and model have reset. With the default single model (Claude Code's own) the order is simply each login,
then Codex. `agent-usage` shows the recommendation with its reason; the orchestrator passes the model it
names to `agent-spawn --model`.

**Extra usage costs money, so you are told.** While agents bill a login's extra usage, `agent-failover`
mails you once at the start, again each time the spend grows by `failover.spend_step` (default 100, in the
login's currency), and at the end. `"failover": {"spend_mail": false}` turns that off.

A usage limit is not news: an agent that hits one writes a status note and does not email you, and
with more than one login `agent-failover` moves it.

Details: [mailroom/README.md](../mailroom/README.md), "The login and model order".
