# Agent board

A Mac menu-bar app that shows every Claude Code and Codex session running on an always-on Linux desktop as a
coloured tile, with the desktop's RAM, CPU, GPU, disks and plan usage. From a tile you can message an agent,
close it, resume it, switch its login or model, attach to its tmux session, or clean up its git worktree. The
**Desktop** chip restarts (or updates and restarts) the desktop and brings every agent back afterwards.

It is one component of the agent kit. It reads what the [mailroom](../mailroom/README.md) records (the status log,
the mail routing store, the mailroom config) and runs the mailroom's own tools for anything it changes
(`agent-send`, `agent-close`, `agent-switch`, `agent-spawn`, `agent-orchestrate`, `agent-mail`). Without the
mailroom the board still shows sessions and metrics, but tiles never turn green and most actions are unavailable.

```
Mac                                                    desktop (Linux; ssh alias `desktop` by default)
AgentBoard.app ── ssh desktop agent-board stream ──►  board/bin/agent-board stream   (read-only, stdlib Python)
               ◄── one JSON line per change / 30 s ──  session registries, status log, mailroom store, /proc, nvidia-smi
               ── ssh desktop agent-board act ──────►  one validated action (JSON on stdin)
```

Nothing listens on a port. The app holds one SSH session open. The collector behind it exits when the connection
drops (EOF on stdin, a broken pipe, or a write stalled for 2 minutes) and after 12 hours; the app reconnects.

## Contents

| Path | What it is |
|---|---|
| `bin/agent-board` | The desktop side: `snapshot`, `stream`, `act`. |
| `bin/agent-restart` | Restart or update-and-restart the desktop, and resume the agents after boot. |
| `bin/agent-cli-update` | Update Codex and Claude Code (`codex update`, `claude update`); run by a timer. |
| `agentboard/classify.py` | Pure rules: tile state, display name, git stage, "waiting on you". |
| `agentboard/sources.py` | Read-only readers: session registries, status log, mailroom store, git, PRs, metrics, usage. |
| `agentboard/collect.py` | Assembles one snapshot from the readers. |
| `agentboard/actions.py` | The only things the app may make the desktop do, each validated. |
| `agentboard/restart.py` | The restart engine (plan, worker, boot-time restore). |
| `agentboard/config.py` | Where the mailroom and its config are; the board's own settings. |
| `agentboard/gmailread.py` | Optional: has the owner read an agent's mail? |
| `agentboard/cliupdate.py` | What `agent-cli-update` runs, and its log. |
| `systemd/agent-restart-restore.service.in` | User unit that resumes the agents after an `agent-restart` reboot. |
| `systemd/agent-cli-update.service.in`, `systemd/agent-cli-update.timer` | User units that run `agent-cli-update` every 6 hours. |
| `mac/` | The SwiftPM app (`Sources/AgentBoard`), `build-app.sh`, `install.sh`, `Info.plist`. |
| `scripts/mac-build.sh`, `scripts/mac-install.sh` | Build and install on the Mac from the desktop over SSH. |
| `tests/` | Unit tests for the collector, actions, restart, config and CLI updates. |

## Tile states

| colour | state | rule |
|---|---|---|
| caramel | busy | a turn is running: Claude `busy`, or a Codex rollout written in the last 45 s |
| darker caramel | background | Claude `shell`: the turn ended but a background command it started is still running, so it is only waiting on that and wakes by itself. The tile says `bg wait`. Exception: when every such command is a `pgrep -f` wait loop that can only find itself (the Bash tool runs it as `bash -c '... pgrep -f PATTERN ...'`, so the loop always matches its own command line) and nothing else matches its pattern, nothing will wake the agent, so it shows as idle and the reason says why |
| green | waiting on you | idle, and its latest mail to you is newer than anything you have sent it, with no turn ending more than 15 min after that mail. The reason ends "(sent as-is …)" when the mailroom's pre-send review had sent that mail back and its fallback sent the draft unchanged |
| red | blocked | stuck at a terminal prompt (Claude `waiting`), a plan-limit or API-error event after its last turn, or its last note starts with blocked / stuck / waiting on you / needs you (or your configured `owner_name`) and it has done nothing since. For Claude, a limit or API error counts only when the session's transcript also ends on the failed request: a `StopFailure` hook from a side request that failed after a finished turn shows as idle, with the reason "its turn finished; a request after it failed (…)" |
| blue → grey | idle | live and idle; fades over 6 h, then **stale**. The reason starts with any mail the pre-send review is holding (see [Held mail](#held-mail-from-the-pre-send-review)) |
| dashed | offline | the process is not running; every other state is a live process. Shown for 12 h after real work (notes or mail), 1 h after a deliberate `agent-close`, and 48 h while mail is in flight. A **red** dashed tile is a *stranded reply*: you answered after it stopped, so nothing reads your reply until it is resumed |

**Red star** (top-right of a tile): a Claude agent running on a later login in the mailroom's `usage.order` while
the first login has room for its model, by the thresholds routing uses (`usage.min_remaining`, default 10% of the
5-hour window and 5% of each weekly window it counts). Hover for the numbers and whether `agent-failover` will move
it back (only agents it moved, after 5 minutes idle). Only relevant with more than one Claude login configured.

**Git chips** show how far the agent's work has got toward main: `±N` uncommitted (tracked + untracked), `↑N`
unpushed commits, `no PR` (pushed, not in main), `PR #N` / `draft #N`, `merged #N`, `closed #N`. The detail panel
shows the pipeline uncommitted → committed → pushed → PR → merged. PRs come from one `gh pr list` per GitHub repo
every 5 minutes, in the background; a squash merge counts as merged when the PR's head is the agent's HEAD.

Not shown: `claude -p` and SDK runs (`entrypoint: sdk-cli`: usage probes, orchestrator runs, preflights), the
orchestrator's own sessions (named `orchestrator*` or running in `orchestrator_dir`), and Codex subagents, which
appear as `+N` on their parent's tile. A Codex session counts as live only while its writer lock is actually held
(`/proc/locks`); the lock file alone outlives its process.

## Data sources

All local and read-only; no model calls.

| What | Where |
|---|---|
| sessions | `~/.claude/sessions/*.json` for every Claude login in the mailroom's `usage.claude_accounts` (pid, process start, socket); `~/.codex/state_5.sqlite` + `~/.codex/thread-writer-locks` |
| turns, notes, limits | `~/.local/state/agent-status/events.jsonl` (written by the mailroom's `agent-status` hooks), tailed incrementally |
| waiting on you, held mail | `~/.local/state/agent-mail/mailroom.sqlite`, opened `mode=ro`: mail `sent` to the owner vs `inbound` from the owner, per session (sessions hosted on this machine only, by the mailroom's `host_id`); and each session's newest `reviews` row per `audience` (the mailroom's pre-send review; skipped when the table does not exist). The `reasons` of a credential stop are never read |
| limit vs side request | for a live Claude agent whose last event is a limit or API error, the tail of its transcript (`<login>/projects/*/<session id>.jsonl`), read once per event, 30 s after it |
| git | `git status --porcelain=v2 --branch`, `rev-list --count origin/main..HEAD`, last commit; cached 60 s, at most 4 refreshes per poll |
| PRs | `gh pr list --repo OWNER/REPO --state all` per repo, every 5 min, in a background thread (needs `gh auth login`) |
| metrics | Linux `/proc/meminfo`, `/proc/stat`, `/proc/mounts`, `statvfs` per real block device (kind and model from sysfs), and one long-lived `nvidia-smi -l 5` when `nvidia-smi` exists. GPU metrics are optional: without an NVIDIA GPU the GPU meter is simply absent |
| plan usage | `agent-usage --json` (mailroom) when on PATH; otherwise its cache file `usage-cache.json`, never written here |
| pending updates | `/usr/lib/update-notifier/apt-check`, `/var/run/reboot-required`, `snap refresh --list`, `apt list --upgradable` (Ubuntu), every 30 min in the background |
| read in Gmail (optional) | see [Optional: Gmail read-check](#optional-gmail-read-check) |

Cost: a 60 s run of `agent-board stream` uses well under a second of CPU, about 1% of one core. A snapshot is
tens of KB; it is sent on change and at least every 30 s, and metrics every 5 s.

## Held mail from the pre-send review

The mailroom can review an agent's mail before it leaves (see the [mailroom README](../mailroom/README.md)). A mail
the review sends back is not sent: the agent is expected to revise and re-send it, but it may carry on as if the
mail went. The board reads the mailroom's `reviews` table and, once the agent's turn has ended, starts its tile's
reason with what is held:

- **Sent back for revision**: "its mail to you was sent back for revision 10m ago and hasn't gone out: SUBJECT
  (REASONS)". It shows while the mailroom still holds the draft: until the agent re-sends to that reader, withdraws
  the mail (`agent-mail withdraw`), or the mailroom's fallback sends the draft as it was. A mail the fallback sent
  unchanged then marks the green tile "(sent as-is: it didn't revise it after the pre-send review)".
- **Stopped for a credential**: "its mail to you was stopped 5m ago (it looked like it had a credential in it) and
  won't go out unless re-sent: SUBJECT". The mailroom keeps no draft and never sends it. The board shows it until the
  agent re-sends to that reader or a turn ends more than 15 minutes after the stop (the agent has moved on). The
  review's reasons for such a stop quote the start of what matched, so the board never reads them: only the subject
  is shown.

Each reader is tracked on its own, as the mailroom holds one draft per reader: `you` (mail to the owner),
`a collaborator` (an address in the mailroom's `collaborators`) or `someone else`. With more than one held, the
newest is named and the rest counted ("(and 1 more held)"). Nothing needs configuring: a mailroom without the
review has no `reviews` table and the board simply skips it.

## Keys and layout

- ⌥⌘A (anywhere) toggles the window; ⌘W closes it. While the window is open the board is a regular app with a Dock
  icon and a menu bar (View: Refresh, Zoom); closed, it lives in the menu bar only. The menu-bar title counts the
  agents that need you (waiting and blocked).
- ⌘R refreshes everything: agents, git (ignoring the 60 s cache), PRs, tmux, worktrees, and usage (through
  agent-usage's own cache). It reconnects if the link is down. The ↻ button next to the tabs does the same.
- ⌘+ / ⌘- / ⌘0 zoom the board, 70–200%. The zoom is remembered.
- In the text dialogs (Message, Orchestrator, Start agent here), ⌘↩ sends; Return is a new line.
- Drag a tile onto another tile to put it just before that tile, or onto a group to add it to that group. Drop it
  on "New group" to start one. Drag a group's name to reorder groups. Groups can be renamed, collapsed and deleted;
  deleting a group keeps its tiles.
- **Sub-groups** (one level) sit side by side inside a group as columns. Make one with ⋯ → "New sub-group…" or the
  "+ sub-group" cell; drag tiles and sub-group names to move them; its ⋯ menu offers rename, move left/right, "make
  it a group of its own", and delete (which keeps its tiles).
- Claude sessions are read from every login in the mailroom's `usage.claude_accounts`; a tile on a non-default
  login shows it, e.g. `claude · work`. Under the usage bars, `next agent → X` is agent-usage's pick for the next
  spawn; running agents keep their login.
- A selected offline tile says what is left: the process (exited, killed, closed), the tmux session (gone, or open
  with no agent in it), the directory (linked worktree, main checkout, or gone) and any unlanded work.
- The layout (groups and order, keyed by session id) is saved in
  `~/Library/Application Support/AgentBoard/layout.json`.

## Worktrees with no agent

The bottom section lists every linked git worktree of every repo directly under `repos_root` (`git worktree
list`, wherever the worktree itself lives) that no agent is working in. Worktrees an Offline tile already shows
are left out. A Claude Code subagent worktree (`<repo>/.claude/worktrees/…`) counts as busy while any live agent
works anywhere in that repo. The scan runs in the background every 10 minutes and on ⌘R.

Right-click a worktree tile, or use the panel:

- **Clean up…** runs `agent-close`'s own `close()` on the path (squash and rebase merges count as landed). It
  removes the worktree and its branch only if everything is landed; otherwise it refuses and nothing changes. The
  confirmation shows the dry run first.
- **Force remove…** removes it anyway, after you type its name.
- **Start agent here…** runs `agent-spawn --dir PATH` with your instructions plus the path, branch and git state.
- **Clean up all fully landed…** (section header) fetches each repo once, runs the dry run on every worktree in
  the section, lists what would go and what stays, and on confirm cleans up each one with the same checks, never
  forced.

The desktop re-checks every request: the path must be a current linked worktree, with no live agent in it.

## Actions

Right-click a tile:

- **Close…** asks first, then sends the agent a fixed close message (`[relayed from <owner_name> via agent-board]
  close`, through `agent-send`). The agent lands its work and closes itself, or emails you if work is unlanded.
- **Clean up…** (offline agents) is plain `agent-close ID`: it removes a linked worktree and its branch only if
  every change is landed on main, and never removes a main checkout. The confirmation shows the dry run first.
- **Force close…** shows what `agent-close ID --dry-run` says would be discarded, makes you type the agent's name,
  then runs `agent-close ID --force`.
- **Send message…** delivers your text to the agent, headed `[relayed from <owner_name> via agent-board]`.
- **Orchestrator…** (top bar and menu-bar menu) messages the orchestrator like an email: the desktop runs one
  `agent-orchestrate --source board` event in the background with your text after a header saying it came from
  you via the board, not by email, and that the answer goes to `owner_email` by `agent-mail send`. Like any local
  `agent-orchestrate`, it cannot be verified the way an email is (DKIM); it is as trustworthy as the desktop
  account. Logs: `~/.local/state/agent-board/orchestrate-*.log`.
- **Change agent…** / **Change model/effort…** use the mailroom's `agent-switch`: the sheet lists each target
  (each Claude login, or Codex) with its usage status, whether it keeps the conversation or hands off to a new
  session, and the models and efforts valid for that CLI. An idle agent switches at once; a busy one only after
  "Interrupt and switch".
- **Resume…** (offline agents) starts it again the way it was started: a new tmux session in its folder, in its
  own memory-capped scope, continuing the same conversation (`claude --resume ID` on the login the mailroom picks
  for offline resumes, or `codex resume ID` with the thread's model and effort). It is then told it was resumed,
  given any note you typed, and told about mail you sent while it was down. Refused if its folder is gone. Codex is
  started with `-c check_for_update_on_startup=false`, so a pending Codex update cannot leave it waiting on the
  "Update available" menu (the same holds for agents `agent-restart` brings back).
- **Open in Kitty** opens a new window in the running kitty (via its remote-control socket, kitty.conf
  `listen_on unix:/tmp/kitty-{pid}`) running `kitten ssh -t <host> tmux attach -t =SESSION`; with no kitty running
  it starts one. A background Claude session gets `claude attach ID` under its own login instead.
- **Copy attach command** copies `ssh -t <host> 'tmux attach -t =NAME'` (or `claude attach` for a background
  session).

Clean up and Force close refuse when another live agent works in the same directory: agent-close checks for
unlanded work, not for other sessions, so it would pull a shared worktree out from under the live one.

The desktop re-validates every request against the current board: a full session id (never a name), live for
close and send, and the typed name for force close. Text goes on stdin, never into argv or a shell. Actions are
logged to `~/.local/state/agent-board/actions.jsonl`.

## Restarting the desktop (`agent-restart`, optional)

The **Desktop** chip shows pending updates, whether a reboot is needed, and whether the GPU driver answers. Click it
for the restart panel: every live agent with its CLI, model and what it is doing, plus background waits and local
processes a reboot would kill. Each agent gets a choice:

- **Nudge**: resumed, then told the desktop restarted and asked to carry on. Default for busy and background agents.
- **Quiet**: resumed in the same conversation with no message; it keeps its colour. Default for the rest.
- **Don't resume**: left offline (`skip` on the command line: `--choice NAME=skip`).

**Restart** reboots. **Update & Restart** first runs `apt-get update`, `apt-get full-upgrade` and `snap refresh`; if
apt fails, nothing reboots and you get an email. Busy agents are told the restart is coming and get up to 15 minutes
to reach a stopping point. You can cancel until the update starts. After boot, the user unit
`agent-restart-restore.service` resumes each agent in its old tmux name, folder, login, model and effort, in its own
memory scope, Claude agents first, then Codex, and emails you a summary. It acts only on the manifest of a restart
it was asked for, and only once.

From a shell (an agent or the orchestrator should run this only after you say yes):

```bash
agent-restart plan                                        # read-only: what would happen
agent-restart run --update --choice NAME=quiet --yes      # or --dry-run
agent-restart status | cancel
agent-restart test-cycle --only A,B --choice A=nudge      # the restore path on scratch agents, no reboot
```

Requirements: Ubuntu (apt, optionally snap), a systemd user manager with lingering enabled
(`loginctl enable-linger $USER`, so the restore unit runs at boot without a login), and passwordless `sudo -n` for
the commands it runs: `systemctl reboot` for Restart, plus `apt-get`, `env` and `snap` for Update & Restart. Note
that passwordless `apt-get` or `env` is effectively root; grant only `systemctl reboot` if you only want Restart:

```
# /etc/sudoers.d/agent-restart  (edit with: sudo visudo -f /etc/sudoers.d/agent-restart)
alex ALL=(root) NOPASSWD: /usr/bin/systemctl reboot
```

Mail goes to `owner_email` from the mailroom config through `agent-mail send --unrouted`; without it nothing is
mailed (the log says so). State and the log: `~/.local/state/agent-board/restart/`.

Resumed agents, from Resume or the restore, never inherit `AGENT_ORCHESTRATOR` or other per-process Claude
variables: if an orchestrator run started the tmux server, those sit in tmux's global environment, and the board
removes them before starting a session.

## Keeping Codex and Claude Code updated (`agent-cli-update`)

`agent-cli-update` runs `codex update` and `claude update`, and `agent-cli-update.timer` runs it every 6 hours
(03:40, 09:40, 15:40 and 21:40, give or take 10 minutes, and 15 minutes after boot; a run missed while the machine
was off happens at the next boot). Both CLIs update themselves too, but late, and until Codex installs an update it
knows of, every interactive Codex start opens on its "Update available" menu and waits for a key. Each CLI installs
a new version beside the old one and switches a symlink, so running agents keep their version and the next launch
gets the new one.

```bash
agent-cli-update               # update both now, one log line each
agent-cli-update log [N]       # the last N lines of ~/.local/state/agent-board/cli-update.log (default 20)
systemctl --user status agent-cli-update.service     # the last timer run
```

A CLI that is not installed is logged as skipped (Codex is optional). An installed CLI whose update fails (a
non-zero exit, a timeout, or no `--version` afterwards) makes the run, and so the unit, fail; so does finding
neither CLI. Updates run non-interactively (stdin closed, `CODEX_NON_INTERACTIVE=1`). To stop automatic updates,
disable the timer: `systemctl --user disable --now agent-cli-update.timer`.

## Configuration

The board has no required settings of its own. It reads:

**Mailroom config** `~/.config/agent-mail/config.json` (written by the kit installer; `AGENT_MAIL_CONFIG_DIR`
overrides the directory):

| Key | Default if absent | Used for |
|---|---|---|
| `owner_name` | `the owner` | the name in every text an agent reads (`[relayed from Alex via agent-board]`), and in the blocked-note rule |
| `owner_email` | first `allowlist` entry, else none | where `agent-restart` mails; the orchestrator header's `--to`; the Gmail link and read-check |
| `allowlist` | `[]` | which inbound and sent mail counts as the owner's |
| `account` | none | the agents' Gmail address (only for the optional read-check) |
| `repos_root` | `~/code` | the worktree scan root (`AGENT_BOARD_REPOS` overrides) |
| `orchestrator_dir` | `<repos_root>/orchestrator` | sessions there are the orchestrator's, not tiles |
| `host_id` | `desktop` | which mailroom sessions are hosted on this machine |
| `usage` | one login, `default` | Claude logins, `order`, `min_remaining` (the mailroom's agent-usage settings) |
| `spawn_*` | as the mailroom's | model and effort for Resume when a session's own are unknown |

**Mailroom checkout**: the kit's sibling directory `../mailroom` (relative to `board/`), or `AGENT_MAILROOM_DIR`.
The board imports `agentmail.close`, `agentmail.failover`, `agentmail.memcap` and `agentmail.config` from it, and
loads `bin/agent-spawn` for its trust helper. State is read from `~/.local/state/agent-mail`
(`AGENT_MAIL_STATE_DIR` overrides) and `~/.local/state/agent-status` (`AGENT_STATUS_DIR`).

**Board config** `~/.config/agent-board/config.json` (optional; `AGENT_BOARD_CONFIG` overrides the path):

| Key | Default | Meaning |
|---|---|---|
| `gmail_credentials` | `null` (off) | path to the owner's Gmail OAuth file for the read-check below (`AGENT_BOARD_GMAIL_CREDS` overrides) |

**Mac app** settings live in `defaults` for the `org.agentkit.agentboard` domain:

| Key | Default | Meaning |
|---|---|---|
| `host` | `desktop` | the ssh alias of the desktop; also editable from the menu-bar menu ("Desktop host…") |
| `collector` | `~/.local/bin/agent-board` | the collector's path on the desktop (expanded by the remote shell) |

## Optional: Gmail read-check

When on, a green tile turns blue ("you've read its mail") once you have read the agent's latest mail in Gmail, and
green again if you mark it unread. It is off by default. To turn it on, point `gmail_credentials` at an
"authorized_user" OAuth JSON (`client_id`, `client_secret`, `refresh_token`) for your own Gmail with the
`gmail.readonly` scope or broader (for example the file a Gmail MCP server keeps, `~/.gmail-mcp/credentials.json`):

```bash
mkdir -p ~/.config/agent-board
echo '{"gmail_credentials": "~/.gmail-mcp/credentials.json"}' > ~/.config/agent-board/config.json
```

The login must belong to an owner address (`owner_email` or `allowlist`), or it is not used. Once a minute it makes
one `messages.list` of unread mail from the agents' address in the last 8 days, plus one cached `rfc822msgid:`
lookup per mail the board newly asks about. Metadata only, no bodies; the token never appears in a URL or a log.
If it fails, "read-check off" shows in the top bar and tiles behave as without it.

## Install

The kit's top-level `install.sh` does the desktop part. By hand:

### On the desktop

1. Python 3.9+ (stdlib only), `git`, `tmux`; `gh` (logged in) for PR chips; `nvidia-smi` optional.
2. The mailroom installed and configured (`../mailroom/README.md`), including its `agent-status` hooks.
3. Wrappers on PATH (the Mac app runs `~/.local/bin/agent-board` over a non-login ssh shell):

   ```bash
   KIT=~/code/agent-board-kit          # wherever the kit is checked out
   for t in agent-board agent-restart agent-cli-update; do
     printf '#!/bin/sh\nexec python3 %s/board/bin/%s "$@"\n' "$KIT" "$t" > ~/.local/bin/$t
     chmod +x ~/.local/bin/$t
   done
   agent-board snapshot --pretty | head -40        # check: JSON with your agents
   ```

4. Optional, for restarts: render and enable the restore unit (`{{PYTHON}}` is the Python to use, `{{KIT_DIR}}`
   the kit checkout), enable lingering, and set up sudo as described above:

   ```bash
   sed -e "s|{{PYTHON}}|$(command -v python3)|" -e "s|{{KIT_DIR}}|$KIT|" \
       $KIT/board/systemd/agent-restart-restore.service.in > ~/.config/systemd/user/agent-restart-restore.service
   systemctl --user daemon-reload && systemctl --user enable agent-restart-restore.service
   loginctl enable-linger "$USER"
   ```

5. Optional, to keep Codex and Claude Code updated: render the service the same way, copy the timer, and enable it:

   ```bash
   sed -e "s|{{PYTHON}}|$(command -v python3)|" -e "s|{{KIT_DIR}}|$KIT|" \
       $KIT/board/systemd/agent-cli-update.service.in > ~/.config/systemd/user/agent-cli-update.service
   cp $KIT/board/systemd/agent-cli-update.timer ~/.config/systemd/user/
   systemctl --user daemon-reload && systemctl --user enable --now agent-cli-update.timer
   ```

### On the Mac

Requirements: macOS 13 or later, the Command Line Tools (`xcode-select --install`; SwiftPM, no Xcode needed), and
an ssh alias for the desktop that logs in without a prompt (key auth, `BatchMode` works), reachable over the LAN or
Tailscale. Check with `ssh desktop true`.

Simplest: build on the Mac itself.

```bash
git clone <your copy of the kit> ~/agent-board-kit
cd ~/agent-board-kit/board/mac
./build-app.sh            # swift build, assemble and ad-hoc sign mac/build/AgentBoard.app
./install.sh              # copy to /Applications, add the login item, start it
defaults write org.agentkit.agentboard host my-alias    # only if your ssh alias is not `desktop`
```

Or from the desktop, over SSH (rsyncs `board/` to the Mac, builds and installs there):

```bash
MAC_HOST=mac scripts/mac-install.sh        # MAC_HOST: the Mac's ssh alias (default `mac`)
                                           # MAC_BUILD_DIR: build dir on the Mac (default ~/agent-board-build)
```

`install.sh` copies the app to `/Applications/AgentBoard.app`; the first time only it writes the login item
`~/Library/LaunchAgents/org.agentkit.agentboard.plist` (starts the app at login, menu bar only); it stops any copy
running from elsewhere, so there is one board and one hotkey; and it starts the installed copy unless it is
already running, in which case the running copy offers "New build ready: relaunch". Rerun after every change. The
app needs no permissions: the hotkey uses Carbon `RegisterEventHotKey`. Built locally, it carries no quarantine
flag; a copy from elsewhere may need right-click → **Open** once.

## Uninstall

On the Mac:

```bash
launchctl bootout gui/$(id -u)/org.agentkit.agentboard; rm ~/Library/LaunchAgents/org.agentkit.agentboard.plist
rm -rf /Applications/AgentBoard.app ~/Library/Application\ Support/AgentBoard
defaults delete org.agentkit.agentboard
```

On the desktop:

```bash
systemctl --user disable agent-restart-restore.service; rm ~/.config/systemd/user/agent-restart-restore.service
systemctl --user disable --now agent-cli-update.timer
rm ~/.config/systemd/user/agent-cli-update.service ~/.config/systemd/user/agent-cli-update.timer
rm ~/.local/bin/agent-board ~/.local/bin/agent-restart ~/.local/bin/agent-cli-update
rm -rf ~/.local/state/agent-board ~/.config/agent-board
```

## Developing

```bash
python3 -m unittest discover -s tests          # 97 tests; fixtures only, no real tmux, ssh, systemd or network
bin/agent-board snapshot --pretty | less       # what the app sees
echo '{"action": "restart_plan"}' | bin/agent-board act | python3 -m json.tool    # a read-only action
```

The tests point every config and state path at a scratch directory (`tests/_kitenv.py`) with an example owner,
so they never read your real configuration. On the Mac:

```bash
AgentBoard.app/Contents/MacOS/AgentBoard --probe              # real ssh stream + a read-only action, headless
AgentBoard.app/Contents/MacOS/AgentBoard --render snap.json out.png 1060 680 [--light] [--overview] [--select ID]
AgentBoard.app/Contents/MacOS/AgentBoard --selftest-keys      # zoom shortcuts reach the board
AgentBoard.app/Contents/MacOS/AgentBoard --selftest-layout    # groups and sub-groups, and the saved layout decodes
```

`--render` draws the real views offscreen from a saved snapshot (`agent-board snapshot > snap.json` on the
desktop), so screenshots need no screen-recording permission.

## Troubleshooting

- **The board says it cannot connect.** Run `ssh -o BatchMode=yes desktop ~/.local/bin/agent-board snapshot | head
  -c 300` on the Mac. A password prompt or host-key question means the alias is not passwordless yet; "not found"
  means the wrapper is missing; a Python error is the collector's. Fix the alias with "Desktop host…" in the
  menu-bar menu or `defaults write org.agentkit.agentboard host <alias>`.
- **No agents, but they are running.** Check `~/.claude/sessions/*.json` exist (Claude Code writes them) and, for
  other logins, that the mailroom's `usage.claude_accounts` lists their `config_dir`.
- **Tiles never turn green.** The mail state needs the mailroom store (`~/.local/state/agent-mail/mailroom.sqlite`)
  and `owner_email` / `allowlist` in the mailroom config; agents must send their mail with `agent-mail send`.
- **No notes or turn counts.** The mailroom's `agent-status` hooks are not installed; see its README.
- **Clean up / Resume say the mailroom isn't installed.** The board looks for `../mailroom/agentmail` next to
  `board/`; set `AGENT_MAILROOM_DIR` if the mailroom lives elsewhere.
- **No PR chips.** `gh auth status` on the desktop; the repo's `origin` must be on GitHub.
- **No GPU meter.** Expected without `nvidia-smi`; the Desktop chip then skips the GPU check too.
- **The Desktop chip says "checking…" forever.** `/usr/lib/update-notifier/apt-check` is missing (it ships with
  Ubuntu's `update-notifier-common`); the rest of the board is unaffected.
- **"read-check off" in the top bar.** The optional Gmail read-check is configured but failing; hover for why.
- **Restart refuses to update or reboot.** `sudo -n true` must work for the commands listed above; the log is
  `~/.local/state/agent-board/restart/run.log`.
- **A tile says a mail "was sent back for revision" but the agent says it sent it.** The mailroom's pre-send review
  is holding it; the agent must revise and re-send (or `agent-mail withdraw`). See
  [Held mail](#held-mail-from-the-pre-send-review).
- **`agent-cli-update.service` failed.** `agent-cli-update log` shows which CLI and why; run `codex update` or
  `claude update` by hand to see the full output.
- **Agents did not come back after a restart.** `systemctl --user status agent-restart-restore.service` and
  `agent-restart status`; lingering must be on for the unit to run before you log in.
