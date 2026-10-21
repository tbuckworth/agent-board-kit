# agent-board-kit

Run a fleet of Claude Code and Codex agents on an always-on Linux machine and manage them from
your inbox, a Mac menu-bar board and a GitHub-Issues to-do list.

- **Email your agents.** Every agent mails you from one shared Gmail account. Your reply lands back
  in the exact session that wrote to you, even hours later. Mail that no agent owns goes to an
  **orchestrator** (one fresh `claude -p` run per email), which starts, relays to, and closes
  agents for you.
- **Mail you can act on.** Before an agent's mail leaves, a cheap model with no context reads it as
  you will. If it would leave you asking "what does this mean?" or "where's the plot?", it goes back to
  the agent once, with concrete fixes. Plots go inline, and a mail containing an API key or token is
  never sent. Mail to collaborators and other people is checked for clarity too.
- **One worktree per agent.** `agent-spawn` makes a branch off `origin/main`, a git worktree, a
  tmux session and a memory-capped systemd scope, and launches Claude Code or Codex in it with a
  brief. `agent-close` removes it, but only when its work is landed.
- **See everything at a glance.** The **agent board** (a Mac menu-bar app) shows every session as
  a tile coloured by state (busy, waiting on you, blocked, idle, offline), with git progress
  toward main, plan usage, and right-click actions (message, close, resume, switch model/login).
- **A to-do list agents can work on.** Your tasks are issues in a private GitHub repo. A web app
  and a Mac app edit them. **Assign to agent** starts an agent on a task, or hands it to one that is
  already running; it reports back on the issue and by email.
- **Free status notes.** Hooks log every turn; agents add one-line milestones with
  `agent-status note`. A status report email costs one small model call.

Everything personal (names, addresses, GitHub names, paths, hosts) lives in one file,
`~/.config/agent-kit/kit.env`. The kit contains no one's data or credentials.

```
 you (phone / laptop mail)                        Mac (optional)
   │  ▲                                            AgentBoard.app ── ssh ──┐   ToDo.app ── ssh ──┐
   │  │ replies                                                            │                     │
   ▼  │                                                                    ▼                     ▼
 Gmail: agents account ◄──── agent-mail send ───┐          Linux machine (always on)
   │                                            │  ┌─────────────────────────────────────────────────────┐
   └── polled every 30 s by agent-mailroom ─────┼─►│ routing slip: thread / tag / allowlist + DKIM        │
                                                │  │   ├─► agent-send ─► the session that sent the mail   │
                                                │  │   └─► agent-orchestrate ─► claude -p (orchestrator)  │
                                                │  │           └─► agent-spawn ─► worktree + tmux + agent │
                                                └──┤ agents: Claude Code / Codex in tmux, one per worktree │
                                                   │ hooks ─► agent-status events ─► board, reports       │
                                                   │ agent-board stream (read-only collector)             │
                                                   │ todo-desk (gh) ◄──► GitHub Issues (your tasks repo)   │
                                                   └─────────────────────────────────────────────────────┘
```

## What is in the box

| Directory | What | Status |
|---|---|---|
| [`mailroom/`](mailroom/README.md) | `agent-mail`, `agent-mailroom` (daemon), `agent-send`, `agent-status`, `agent-spawn`, `agent-close`, `agent-orchestrate`; optional `agent-usage`, `agent-failover`, `agent-switch` (several Claude logins), `agent-memwatch` + memory caps | core |
| [`orchestrator/`](orchestrator/AGENTS.md) | The orchestrator's brief, memory file and status-report skill (rendered into `ORCHESTRATOR_DIR`) | core |
| [`templates/`](templates/) | Global agent instructions, Claude Code and Codex hook settings | core |
| [`board/`](board/README.md) | The agent board: Linux collector + macOS menu-bar app; `agent-restart` (reboot and resume every agent) | core (the app needs a Mac) |
| [`todo/`](todo/README.md) | To-do on GitHub Issues: label setup, `todo-desk` helper, web app, Mac app, Assign to agent | core |
| [`cloud-watchdog/`](cloud-watchdog/README.md) | Watches paid cloud resources agents launch (Modal, Lambda, Slurm, HF...) and chases the owner | optional |
| [`bin/kit`](bin/kit), [`install.sh`](install.sh) | Installer (`kit install`), health check (`kit doctor`), `kit uninstall` | |
| [`docs/`](docs/) | Step-by-step setup, daily use, security, troubleshooting | |

## Quick start

Read [the security model](docs/08-security.md) first: agents run without permission prompts, and
email can start them.

**Before you install** (about 30 minutes, once):

1. On the Linux machine (Ubuntu 22.04 or 24.04): `git`, `tmux`, `python3-venv`, the GitHub CLI
   (`gh auth login`) and Claude Code (logged in). See [prerequisites](docs/01-prerequisites.md).
2. A new Gmail account for the agents, a Google Cloud project with the Gmail API, and its OAuth client
   saved as `~/.config/agent-mail/credentials.json` ([agents' Gmail, steps 1-3](docs/02-agent-gmail.md)).
3. A private GitHub repo for your to-do list: `gh repo create <you>/tasks --private`.

**Install** on the Linux machine:

```bash
git clone <this repo> ~/code/agent-board-kit && cd ~/code/agent-board-kit
./install.sh                          # 1. creates ~/.config/agent-kit/kit.env
$EDITOR ~/.config/agent-kit/kit.env   # 2. your name, your email, the agents' Gmail, GitHub names, paths
./install.sh                          # 3. installs: venv, commands in ~/.local/bin, configs, hooks,
                                      #    agent instructions, orchestrator, systemd units (not started)
export PATH="$HOME/.local/bin:$PATH"  #    (new shells get this from ~/.profile)
agent-mail-auth                       # 4. one-time Gmail consent, signed in as the agents' account
./install.sh --start --labels         # 5. start the mailroom and timers, create the to-do labels
loginctl enable-linger "$USER"        # 6. keep the services running while you are logged out
kit doctor --online                   # 7. everything green?
```

Restart any Claude Code or Codex session that was already running, so it picks up the hooks and
instructions. Then send an email from your own address to the agents' address: "start an agent
in my general repo that lists the five largest files in it and emails me the result". The
orchestrator starts it; the agent mails you; reply to that mail and the agent gets your reply.

## Setup guides

1. [Prerequisites](docs/01-prerequisites.md): machine, packages, accounts
2. [The agents' Gmail account](docs/02-agent-gmail.md): account, Google Cloud project, OAuth client, consent
3. [Install and check](docs/03-install.md): `kit.env`, `install.sh`, services, `kit doctor`
4. [Claude Code and Codex](docs/04-claude-and-codex.md): logins, hooks, permissions, a second login
5. [Daily use](docs/05-daily-use.md): email commands, `agent-spawn`, tmux and worktree conventions, closing agents
6. [To-do list](docs/06-todo.md): tasks repo, labels, web app, Mac app, Assign to agent
7. [The Mac](docs/07-mac.md): ssh alias, agent board and to-do apps, mail from Mac sessions
8. [Security model](docs/08-security.md): read this before you switch anything on
9. [Troubleshooting](docs/09-troubleshooting.md)

## Platform

The server side targets **Linux with systemd** (user services, memory caps via systemd scopes,
`/proc` metrics). It was built and used on Ubuntu. The **Mac** is a client: the menu-bar apps,
and Claude/Codex sessions whose mail is proxied through the Linux machine over SSH. Running the
mailroom on macOS itself is untested; [docs/07-mac.md](docs/07-mac.md) says what would differ.

## Updating

`git pull && ./install.sh` (add `--start` to also enable anything new; running services are not restarted).
What changed in each update: [CHANGELOG.md](CHANGELOG.md). Your
`kit.env`, configs, Gmail token, orchestrator memory and state survive updates. After a change to
the mailroom code, restart the daemon: `systemctl --user restart agent-mailroom` (check that no
orchestrator run is in flight: `agent-orchestrate --runs`).

## Tests

`scripts/run-tests.sh` runs every component's unit tests in a throwaway HOME (nothing touches your
real config, state, tmux, systemd or mail). Use the kit's venv for the mailroom suites:
`PYTHON=~/.local/share/agent-kit/venv/bin/python scripts/run-tests.sh`.

## License

MIT, see [LICENSE](LICENSE). The bundled Inter font files in `todo/web/fonts/` are under the SIL Open
Font License.
