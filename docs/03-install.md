# 3. Install and check

## Before you start

- The [prerequisites](01-prerequisites.md) are installed on the Linux machine, `gh` and Claude Code
  are logged in.
- The agents' Gmail account and its OAuth client file exist: [02-agent-gmail.md](02-agent-gmail.md),
  steps 1-3 (`~/.config/agent-mail/credentials.json` is in place).
- Your to-do repo exists: `gh repo create <you>/tasks --private` (details in [06-todo.md](06-todo.md)).

## Clone and describe yourself

```bash
mkdir -p ~/code && git clone <this repo> ~/code/agent-board-kit
cd ~/code/agent-board-kit
./install.sh            # first run: creates ~/.config/agent-kit/kit.env and stops
$EDITOR ~/.config/agent-kit/kit.env
```

Every key is explained in the file ([kit.example.env](../kit.example.env)). The ones that matter:

| Key | Example | Used for |
|---|---|---|
| `OWNER_NAME` | `Alex` | how agents refer to you; the header on every relayed message |
| `OWNER_EMAIL` | `alex@example.com` | the only sender whose mail is taken as an instruction; where agents report |
| `AGENT_EMAIL` | `yourname.agents@gmail.com` | the agents' mailbox ([02-agent-gmail.md](02-agent-gmail.md)) |
| `GITHUB_OWNER` | `alex-gh` | owner of repos made by `agent-spawn --new-repo` |
| `TASKS_REPO` | `alex-gh/tasks` | the to-do list's private repo |
| `REPOS_ROOT` | `~/code` | where `agent-spawn --repo NAME` looks, and where worktrees go |
| `GENERAL_REPO` | `~/code/general` | home for tasks that belong to no project (created if missing) |
| `ORCHESTRATOR_DIR` | `~/code/orchestrator` | the orchestrator's brief and memory (created) |
| `HOST_ID`, `DESKTOP_HOST`, `MAC_HOST` | `desktop`, `desktop`, `mac` or empty | routing name of the Linux machine, the Mac's ssh alias for it, its ssh alias for the Mac ([07-mac.md](07-mac.md)) |
| `WITH_*` | `yes`/`no` | optional parts: memory watch, agent-restart, cloud-watchdog, to-do dispatch |

## Install

```bash
./install.sh --dry-run   # optional: see every file it would write
./install.sh
export PATH="$HOME/.local/bin:$PATH"   # if `kit` is not found; new login shells get it from ~/.profile
```

What it does, step by step (each step is idempotent; `--only STEP` or `--skip STEP` to choose):

| Step | Effect |
|---|---|
| `venv` | `~/.local/share/agent-kit/venv` with the Google client libraries |
| `wrappers` | one small script per command in `~/.local/bin` (`agent-mail`, `agent-send`, `agent-spawn`, ..., `kit`). An existing file that is not a kit wrapper is left alone unless `--force` (then backed up) |
| `configs` | `~/.config/agent-mail/config.json` (identity keys; your other keys are kept), `~/.config/todo-desk/config.json`, `~/.config/agent-board/config.json` |
| `instructions` | renders the global agent instructions to `~/.config/agent-kit/AGENTS.agents.md`; adds one `@` include line to `~/.claude/CLAUDE.md`; adds a marked block to `~/.codex/AGENTS.md` |
| `hooks` | merges the `agent-status` hooks and `"crossSessionInbound": "accept"` into `~/.claude/settings.json`, and hooks into `~/.codex/hooks.json` (backups kept; existing hooks untouched) |
| `orchestrator` | renders [orchestrator/](../orchestrator) into `ORCHESTRATOR_DIR` and makes it a git repo. Never overwrites its `STATE.md`; other changed files only with `--force` |
| `general-repo` | creates `GENERAL_REPO` as a local git repo if it does not exist |
| `systemd` | renders the user units into `~/.config/systemd/user/` (not started without `--start`) |
| `cron` | only where no `todo-dispatch.timer` is installed: with `--start`, a crontab line for `todo-desk dispatch` |

## Give Gmail consent

```bash
agent-mail-auth
```

A browser opens. Sign in **as the agents' account**, click through "Google hasn't verified this
app" (**Advanced → Go to <your app> (unsafe)**: it is your own app), then **Allow**. The token is
saved to `~/.config/agent-mail/token.json` (mode 600). The command refuses to save a token for any
account other than `AGENT_EMAIL`, so picking the wrong Google login cannot do harm.

**On a headless machine** (you are logged in over ssh):

```bash
# on your laptop:
ssh -L 8765:localhost:8765 <linux-machine>
# then, in that ssh session:
agent-mail-auth --port 8765 --no-browser
```

Open the printed URL in your laptop's browser; the redirect comes back through the tunnel.

## Start

```bash
./install.sh --start --labels
loginctl enable-linger "$USER"
```

`--start` enables and starts `agent-mailroom` (only once the Gmail token exists), the optional
`agent-memwatch`, the after-boot `agent-restart-restore` unit and the `todo-dispatch.timer`.
`--labels` creates the to-do labels in `TASKS_REPO` (the repo must exist). Lingering keeps user
services running while you are logged out.

Restart any Claude Code or Codex sessions that were already running: they read hooks and
instructions at launch.

## Check

```bash
kit doctor            # local checks
kit doctor --online   # also: the Gmail token works for AGENT_EMAIL, the tasks repo has its labels
```

Each failure prints the fix. Common ones are in [troubleshooting](09-troubleshooting.md).

## First test, end to end

1. From any directory on the Linux machine:
   `agent-spawn --repo general --branch hello --brief-text "Say hello: email me one line with agent-mail send, then wait."`
   (`general` is the last part of `GENERAL_REPO`; any repo name under `REPOS_ROOT` works.)
   It prints the tmux session name; `tmux attach -t hello` to watch it (detach with Ctrl-b d).
2. The mail arrives in your own inbox from `AGENT_FROM_NAME (hello)`. Reply "thanks, now close".
3. Within about 30 seconds the reply appears in the `hello` session; it closes itself.
4. Now from your phone, email the agents' address (a fresh mail, not a reply): "What agents are
   running?". No session owns it, so the orchestrator answers.

## Updating

```bash
cd ~/code/agent-board-kit && git pull && ./install.sh
systemctl --user restart agent-mailroom     # after mailroom code changes; not mid-orchestrator-run
```

## Uninstalling

`kit uninstall` removes the wrappers, the kit's systemd units and the instruction blocks, and tells
you what it left (configs, token, hooks in settings files, state, venv).
