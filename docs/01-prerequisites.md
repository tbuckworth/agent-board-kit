# 1. Prerequisites

## The machine

One always-on Linux machine runs everything that matters: the agents, the mailroom daemon, the
orchestrator and the board's collector. A desktop under your desk, a home server or a cloud VM all
work. It was built on Ubuntu with systemd; anything with a systemd **user** manager should do.

- **Ubuntu 22.04 or 24.04** (or similar), with `systemd --user` available.
- **RAM**: agents and their test runs are memory-hungry. 32 GB is comfortable for a handful of
  agents; each agent gets its own memory cap (default 32G, configurable) so one runaway cannot take
  the machine down.
- **Always on, user services kept alive**: `loginctl enable-linger $USER`, so the mailroom keeps
  running while you are logged out.
- A dedicated user account for the agents is a good idea (see [security](08-security.md)): agents
  run with permission prompts off.

A **Mac** is optional. It runs the two menu-bar apps (agent board, to-do) and can host its own
agent sessions whose mail goes through the Linux machine. See [07-mac.md](07-mac.md).

## Packages on the Linux machine

```bash
sudo apt update
sudo apt install -y git tmux python3 python3-venv python3-pip curl jq
# GitHub CLI (https://github.com/cli/cli/blob/trunk/docs/install_linux.md)
sudo apt install -y gh            # or follow the link for the newest version
gh auth login                     # as the GitHub account that owns your repos and the tasks repo
```

- **Python 3.9+** (Ubuntu's python3 is fine). The kit's tools are stdlib Python; only the Gmail
  code needs the Google client libraries, which the installer puts in a private venv
  (`~/.local/share/agent-kit/venv`).
- **Claude Code** ([docs](https://docs.anthropic.com/en/docs/claude-code)):
  `curl -fsSL https://claude.ai/install.sh | bash`, then run `claude` once and log in.
- **Codex** (optional, [docs](https://github.com/openai/codex)): `npm install -g @openai/codex` (needs
  Node.js 18+, e.g. from nodejs.org or `nvm`; or download a release binary from the Codex GitHub page),
  then `codex login`. Version 0.149 or newer (the mailroom delivers to Codex with `codex queue`).
- **nvidia-smi** (optional): the board shows GPU use if it is present.
- **Tailscale** (optional, recommended with a Mac): a private network so the Mac can reach the
  Linux machine by ssh from anywhere, with nothing exposed to the internet.

Neither CLI is on `PATH` in cron or a non-login ssh shell. The tools look in `~/.local/bin` and
`/opt/homebrew/bin`, then ask a login shell, so the standard installers' locations work.

## Accounts you need

| Account | Why | Guide |
|---|---|---|
| A **new Gmail account** for your agents (e.g. `yourname.agents@gmail.com`) | the shared mailbox every agent sends from and the mailroom reads | [02-agent-gmail.md](02-agent-gmail.md) |
| A **Google Cloud project** (free) in that or your own Google account | the OAuth client that lets the mailroom use the Gmail API | [02-agent-gmail.md](02-agent-gmail.md) |
| Your **own email address** | the only sender agents take instructions from. Its provider must sign mail with DKIM (Gmail and most providers do) | |
| **GitHub** (`gh` logged in) | repos, worktrees, PRs, and the private `tasks` repo behind the to-do list | [06-todo.md](06-todo.md) |
| **Claude** plan (Pro/Max/Team/Enterprise) or an API key | Claude Code agents and the orchestrator | [04-claude-and-codex.md](04-claude-and-codex.md) |
| **ChatGPT/OpenAI** plan (optional) | Codex agents | [04-claude-and-codex.md](04-claude-and-codex.md) |

## Where things go

| Path | What |
|---|---|
| the kit checkout (anywhere, e.g. `~/code/agent-board-kit`) | code; the installed commands point here, so keep it |
| `~/.config/agent-kit/kit.env` | your values (the only personal file the installer reads) |
| `~/.config/agent-mail/` | mailroom config, the OAuth client file and the Gmail token (mode 600) |
| `~/.local/bin/` | the commands (small wrappers into the kit) |
| `~/.local/state/agent-mail/`, `agent-status/`, `agent-board/` | routing database, event log, board state |
| `REPOS_ROOT` (e.g. `~/code`) | your repos; agent worktrees are created next to their repo |
| `ORCHESTRATOR_DIR` (e.g. `~/code/orchestrator`) | the orchestrator's brief and memory |
