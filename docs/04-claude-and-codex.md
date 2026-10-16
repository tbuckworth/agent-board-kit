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
                      "hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20, "async": true}]}]
  }
}
```

- **`crossSessionInbound: "accept"`** lets an outside process (the mailroom, a peer agent, the
  board) put a message into a running session. Without it, a session in bypass-permissions mode
  holds such messages for approval and drops them after five minutes. Sessions read this at launch.
- **The hooks** write one text-free line per event to `~/.local/state/agent-status/events.jsonl`
  (no model call, no message content). The board and the status report are built from it.
  `StopFailure` records usage-limit stops.

`~/.codex/hooks.json` gets the same `agent-status hook --cli codex` on `SessionStart`, `Stop` and
`SessionEnd`. Codex asks you to review and trust new hooks the next time it starts: accept them.

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
| `spawn_claude_model`, `spawn_claude_effort` | `claude-opus-5-5`, `xhigh` |
| `spawn_codex_model`, `spawn_codex_effort` | `gpt-6.1-sol`, `xhigh` |
| `orchestrator_model`, `orchestrator_effort` | `sonnet` (follows the latest Sonnet), `high` |

Set a model key to `null` to let the CLI choose. Effort `xhigh` is expensive on a small plan;
`high` or `medium` is a sensible start.

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
  the first login resets. Enable it only if you configured two logins:
  `systemctl --user enable --now agent-failover`.
- `agent-switch NAME --to claude:work|codex --model M --effort E` moves an agent by hand (the
  board's right-click menu uses it).

Details: [mailroom/README.md](../mailroom/README.md).
