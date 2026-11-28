# Agent board

A Mac menu-bar app that shows every Claude Code and Codex session running on an always-on Linux desktop as a
coloured tile, with the desktop's RAM, CPU, GPU, disks and plan usage. From a tile you can message an agent,
close it, resume it, switch its login or model, attach to its tmux session, or clean up its git worktree. The
**Desktop** chip restarts (or updates and restarts) the desktop and brings every agent back afterwards.

Beside the tiles it has:

- a **Mail** tab with the mail between you and your agents, kept by the mailroom: threads, replies with files,
  answers to the options agents offer, comments on their text, Undo, threads in windows of their own, fast mode for
  quick back-and-forths, drafts kept on the desktop, and (with your own Gmail login) a Gmail draft of any agent mail
  to forward ([Mail tab](#mail-tab), [Drafts](#drafts));
- **Agent Mail**, the same mail on your phone: a page the desktop serves to your own Tailscale network alone, which
  Chrome installs as an app, signed in with a passkey ([Mail on the phone](#mail-on-the-phone-agent-mail));
- a **notification on the Mac** for each security alert the mailroom keeps off Gmail, such as a phone enrolled for
  Agent Mail ([Security alerts on the Mac](#security-alerts-on-the-mac));
- **group chats** between you and several agents, one for each sub-group of the board, with topics
  ([Group chats](#group-chats));
- a record of everything you **sent** from the board, which agents tick off when they have dealt with it
  ([Sent messages](#sent-messages-what-you-sent-and-what-is-dealt-with));
- where a **plan window's usage** went, by group and agent ([Plan usage](#plan-usage-where-it-went));
- a **Time** page: where your time went, per day and board group ([Time](#time-where-your-time-went));
- one colour per board group, the same in the Mail tab, the Time page and the usage breakdown
  ([Group colours](#group-colours));
- optional **meeting alerts** from your Google Calendar ([Meeting alerts](#meeting-alerts-optional));
- an optional **Researcher** switch per agent, in the Mail tab and the phone app, for agents that use the public
  `researcher` Claude Code plugin ([Researcher](#researcher-optional));
- an optional **per-token login** (Claude Code on a model through OpenRouter, billed per token), which an agent is
  started on only when you name it, with a weekly **spend meter**
  ([A per-token login](#a-per-token-login-and-its-spend-meter-optional));
- optional **voice**: a voice page on your tailnet where Gemini Live asks your agents for you
  ([Voice page](#voice-page-the-front-desk-optional)), the Mac's **ask box**, and **the desk**, one always-running
  agent you talk to ([The ask box on the Mac](#the-ask-box-on-the-mac), [The desk](#the-desk-optional));
- an optional **keyring unlock**, for a desktop that logs in by itself and leaves its keyring locked
  ([Unlock the desktop](#unlock-the-desktop-optional));
- an optional **transcript archive** of every agent conversation, by board group
  ([Transcript archive](#transcript-archive-optional)).

It is one component of the agent kit. It reads what the [mailroom](../mailroom/README.md) records (the status log, the
mail routing store, the mailbox, the security alerts, the mailroom config) and runs the mailroom's own tools for
anything it changes (`agent-send`, `agent-close`, `agent-switch`, `agent-spawn`, `agent-orchestrate`, `agent-mail`,
`agent-group`). Without the mailroom the board still shows sessions and metrics, but tiles never turn green and most
actions are unavailable.

```
Mac                                                    desktop (Linux; ssh alias `desktop` by default)
AgentBoard.app ── ssh desktop agent-board stream ──►  board/bin/agent-board stream   (stdlib Python)
               ◄── one JSON line per change / 30 s ──  session registries, status log, mailroom store, /proc, nvidia-smi
               ── {"cmd":"act"} on the same stream ─►  one validated action, run as `agent-board act`
```

Nothing listens on a port. The app holds one SSH session open. (The optional phone app's server, and the voice page
on it, listen on a Unix socket that `tailscale serve` publishes to your tailnet alone:
[Mail on the phone](#mail-on-the-phone-agent-mail). The optional desk and desktop voice listen on Unix sockets of their
own, for the desktop alone.) The collector behind it exits when the connection drops (EOF on stdin, a broken pipe, or a
write stalled for 2 minutes) and after 12 hours; the app reconnects.

A click goes over the stream that is already open, which saves an SSH handshake per action. The collector's first
line (its hello line) says whether it takes actions (`acts`); with no live stream, or a collector that doesn't, the
app runs `ssh desktop agent-board act` with the JSON on stdin. If the stream drops while an action is waiting, the
app reports that it may or may not have run, and does not retry. A request too big for the stream (a mail reply with
files) goes over an SSH connection of its own.

## Contents

| Path | What it is |
|---|---|
| `bin/agent-board` | The desktop side: `snapshot`, `stream`, `act`; `done` and `sent` (sent messages), `time` (the Time page), `receive` (files from the Mail tab), `mail-web` (the phone app's server and the voice page); optional parts: `agents`, `ask`, `looking` and `voice-enrol` (the voice desk), `desk` (the desk), `keyring-status` and `keyring-unlock`, `transcripts` (the archive). |
| `bin/agent-keyring-unlock` | `agent-board keyring-unlock` on the system's Python in isolated mode, as it must run, for a shell ([Unlock the desktop](#unlock-the-desktop-optional)). |
| `bin/agent-speech` | The desktop's voice: Kokoro text-to-speech on a Unix socket, on a venv of its own ([The ask box on the Mac](#the-ask-box-on-the-mac)). |
| `bin/agent-restart` | Restart or update-and-restart the desktop, and resume the agents after boot. |
| `bin/agent-cli-update` | Update Codex and Claude Code (`codex update`, `claude update`), then try fast mode's keys on a new version; run by a timer. |
| `agentboard/__init__.py` | Lists every module of the package with one line on each. |
| `agentboard/classify.py` | Pure rules: tile state, display name, git stage, "waiting on you". |
| `agentboard/sources.py` | Read-only readers (session registries, status log, mailroom store, git, PRs, metrics, usage) and shared helpers. |
| `agentboard/collect.py` | Assembles one snapshot from the readers. |
| `agentboard/actions.py` | The only things the app may make the desktop do, each validated; `ACTIONS` is the table. |
| `agentboard/relaunch.py` | Starting an exited agent again: its login, model, effort, tmux session and memory cap. |
| `agentboard/sent.py`, `groups.py` | What you sent from the board and its state; the board's tile groups as the Mac sends them. |
| `agentboard/colours.py`, `colours.json` | The groups' colours: the kit's palette, and how each group and sub-group gets its colour. |
| `agentboard/chats.py`, `subchats.py` | Group chats through the mailroom's `agent-group`; a chat per sub-group. |
| `agentboard/mailbox.py`, `maildrafts.py` | The Mail tab over `agent-mail mailbox`; the reply drafts kept on the desktop. |
| `agentboard/mailforward.py`, `gmailstate.py` | Forwarding an agent's mail as a Gmail draft; your Gmail's read state (both optional). |
| `agentboard/mailweb.py`, `mailwebstate.py`, `webauthn.py`, `lockdown.py` | The phone app's server: its checks, its settings and stored state (passkeys, sessions, the enrolment code), passkey signatures (with `cryptography`), and the hardening of it and of the keyring unlock. |
| `web/mail/`, `web/passkeys.js` | The phone app's page: its HTML, CSS, scripts, manifest and icons, served by `mailweb.py`; the passkey calls of both pages. |
| `agentboard/voicedesk.py`, `voiceweb.py`, `gemini.py`, `voice.json`, `web/voice/`, `mac/voice-desk/aw_front.py` | The voice desk: read-only copies that answer for an agent, and what is on the Mac's screen (`aw_front.py`, piped to the Mac's ActivityWatch over ssh); the voice page, its Gemini token mint and its settings. |
| `agentboard/desk.py`, `desk.json`, `desk-prompt.md` | The desk: one always-running Claude, its tools and its send rule. |
| `agentboard/speech.py`, `speech.json` | The desktop's voice: the socket between the board and `agent-speech`, and speaking an answer as it is written. |
| `agentboard/keyring.py` | The desktop keyring's state and its unlock. |
| `agentboard/transcripts.py` | The transcript archive and its catalog. |
| `agentboard/allowance.py`, `credits.py` | Where a plan window's usage went; the Codex credit balance. |
| `agentboard/timeuse.py`, `timesources.py`, `screen.py` | The Time page, its sources, and the Mac's screen time. |
| `agentboard/meetings.py`, `google.py`, `webapi.py` | Optional meeting alerts; a small Google API client for your own login. |
| `agentboard/restart.py` | The restart engine (plan, worker, boot-time restore). |
| `agentboard/config.py` | Where the mailroom and its config are; the board's own settings. |
| `agentboard/cliupdate.py` | What `agent-cli-update` runs, and its log. |
| `systemd/agent-restart-restore.service.in` | User unit that resumes the agents after an `agent-restart` reboot. |
| `systemd/agent-cli-update.service.in`, `systemd/agent-cli-update.timer` | User units that run `agent-cli-update` every 6 hours. |
| `systemd/agent-board-time.service.in`, `systemd/agent-board-time.timer` | Optional user units that recompute the Time page every 15 minutes. |
| `systemd/agent-board-mail-web.service.in` | Optional user unit that runs the phone app's server (`agent-board mail-web`), and the voice page on it. |
| `systemd/agent-board-desk.service.in` | Optional user unit that runs the desk (`agent-board desk serve`). |
| `systemd/agent-speech.service.in` | Optional user unit that runs the desktop's voice (`agent-speech serve`, on the speech venv). |
| `systemd/agent-board-transcripts.service.in`, `systemd/agent-board-transcripts.timer` | Optional user units that archive the transcripts once a day. |
| `requirements.txt` | `cryptography`, which only the phone app's server needs; the rest of the board is standard library. |
| `requirements-speech.txt` | Kokoro and what it needs, pinned, for the speech venv (optional, heavy: PyTorch is installed beside it). |
| `mac/` | The SwiftPM app (`Sources/AgentBoard`; `main.swift` lists every flag and source file), `build-app.sh`, `install.sh`, `Info.plist`. |
| `scripts/mac-build.sh`, `scripts/mac-install.sh` | Build and install on the Mac from the desktop over SSH. |
| `tests/` | Unit tests for the collector, actions, restart, chats, mail, drafts, colours, usage and spend, Time page, meetings, config, CLI updates, the phone app (its page in headless Chrome, `tests/chrome.py`), the keyring (on a private D-Bus), the voice desk and voice page, the ask box, the desk (`tests/desk_claude.py` stands in for Claude), the desktop's voice and the transcript archive. |

## Tile states

| colour | state | rule |
|---|---|---|
| caramel | busy | a turn is running: Claude `busy`, or a Codex rollout written in the last 45 s |
| darker caramel | background | Claude `shell`: the turn ended but a background command it started is still running, so it is only waiting on that and wakes by itself. The tile says `bg wait`. Exception: when every such command is a `pgrep -f` wait loop that can only find itself (the Bash tool runs it as `bash -c '... pgrep -f PATTERN ...'`, so the loop always matches its own command line) and nothing else matches its pattern, nothing will wake the agent, so it shows as idle and the reason says why |
| green | waiting on you | idle, and its latest mail to you is newer than anything you have sent it, with no turn ending more than 15 min after that mail. The reason ends "(sent as-is …)" when the mailroom's pre-send review had sent that mail back and its fallback sent the draft unchanged |
| red | blocked | its mail to you is stuck in `agent-mail` (see below; this outranks busy), stuck at a terminal prompt (Claude `waiting`), a plan-limit or API-error event after its last turn, or its last note starts with blocked / stuck / waiting on you / needs you (or your configured `owner_name`) and it has done nothing since. For Claude, a limit or API error counts only when the session's transcript also ends on the failed request: a `StopFailure` hook from a side request that failed after a finished turn shows as idle, with the reason "its turn finished; a request after it failed (…)" |
| blue → grey | idle | live and idle; fades over 6 h, then **stale**. The reason starts with any mail the pre-send review is holding (see [Held mail](#held-mail-from-the-pre-send-review)) |
| dashed | offline | the process is not running; every other state is a live process. Shown for 12 h after real work (notes or mail), 1 h after a deliberate `agent-close`, and 48 h while mail is in flight. A **red** dashed tile is a *stranded reply*: you answered after it stopped, so nothing reads your reply until it is resumed |

**A stuck mail** turns the tile red, even while the agent is busy with something else: an `agent-mail send` or
`reply` anywhere below the agent's process that has spent 2 minutes blocked reading a socket on its input, or
15 minutes running for any reason (checked every 30 s). The reason names the mail's subject and the pid.

**A signed-out login.** When a Claude agent's last turn ended because its login is signed out
(`authentication_failed`), the reason names the login and the account it is for (its label in the mailroom's usage
config), so `/login` goes to that account and not one another login already uses. Two logins on one account spend
one allowance; if that happens anyway, the board marks the later login under its usage meters as "same account as
…", in red. Later means after the default login, then in the order the mailroom's `usage.claude_accounts` lists the
logins. The board reads each login's `.claude.json` at every usage reading, every 5 minutes.

**Notifications.** The Mac app posts a macOS notification once when anything turns red (`RedRules` in
`mac/Sources/AgentBoard/Alerts.swift` says what counts), and again only after it has been not-red. More than 3 at
once come as one notification. Clicking one opens the board on that agent. What has been notified is kept in the
app's defaults (`redNotified`), so a relaunch doesn't repeat it. The first launch asks macOS for notification
permission; without it the app posts through `osascript`.

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
appear as `+N` on their parent's tile (hover for their names; click the tile for the list). A Codex session counts
as live only while its writer lock is actually held (`/proc/locks`); the lock file alone outlives its process.

## Data sources

These feed the live board: all local and read-only, no model calls, except the optional Google readers at the end.
The [Time page](#time-where-your-time-went) has its own sources.

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
| Mail tab | the mailroom's mailbox, through `agent-mail mailbox summary` every 30 s: the unread and inbox counts, and which agent mails the tab counts as read |
| group chats | the mailroom's `agent-group`, read as you (the Chats button shows once the desktop has it) |
| security alerts | the mailroom's `security-alerts.jsonl` (`~/.local/state/agent-mail/`), read with each snapshot: the entries of the last 7 days ([Security alerts on the Mac](#security-alerts-on-the-mac)) |
| read in Gmail (optional) | your own Gmail login, metadata only; see [Optional: your Gmail](#optional-your-gmail-read-check-and-forward) |
| meetings (optional) | your Google calendars, every 5 minutes, when `meeting_alerts` is on; see [Meeting alerts](#meeting-alerts-optional) |
| spend (optional) | a per-token login's transcripts since Monday, with each usage reading, for its spend meter ([A per-token login](#a-per-token-login-and-its-spend-meter-optional)) |
| logins (optional) | the mailroom's manual logins (`accounts.manual`), with each snapshot, for the start dialog's Login menu (`spawn_logins`) |
| keyring (optional) | the `default` alias (ReadAlias), and its keyring's and login's Label and Locked properties, through `busctl` every minute, when `keyring_unlock` is on ([Unlock the desktop](#unlock-the-desktop-optional)) |

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

- ⌥⌘A (anywhere) toggles the window; ⌘W closes it. While the window, or a Mail thread's own window, is open the board
  is a regular app with a Dock icon, a place in ⌘Tab and a menu bar (View: Refresh, Zoom, Enter Full Screen); with
  all closed, it lives in the menu bar only. The menu-bar title counts the agents that need you (waiting and blocked).
- ⌘R refreshes everything: agents, git (ignoring the 60 s cache), PRs, tmux, worktrees, and usage (through
  agent-usage's own cache). It reconnects if the link is down. The ↻ button next to the tabs does the same.
- ⌘+ / ⌘- / ⌘0 zoom the board, 70–200%. The zoom is remembered.
- In the text dialogs (Message, Orchestrator, Start agent here), ⌘↩ sends; Return is a new line. Images and files
  pasted or dropped on the text, or picked with **Attach…**, go with a message or an orchestrator request
  ([Actions](#actions)).
- The Mail tab has keys of its own: ↑ ↓ ← →, Space, **e**, **u**, **s**, **z** or ⌘Z, **r** and ⌘V
  ([Mail tab](#mail-tab), "Keys").
- **Double-click a tile** for its agent's latest mail to you in the Mail tab: the tab opens the thread at that mail
  (at the thread's newest when the agent's mail is not found in it) and lists the box it is in, Inbox, Snoozed or
  Archived. An agent with no mail to you in the last week, or a desktop with no mailbox, gets a short note saying so
  and the board stays. A tile's right-click menu has **Open latest mail in the Mail tab** and **Open latest mail in
  Gmail**; the Gmail item is left out when the mailroom kept that mail for the tab alone (always, without Gmail), as
  Gmail has no copy.
- While a dialog is open the board holds its updates, so typing in it isn't slowed by redraws of every tile. The
  newest snapshot and metrics are applied once it closes.
- Drag a tile onto another tile to put it just before that tile, or onto a group to add it to that group. Drop it
  on "New group" to start one. Drag a group's name to reorder groups. Groups can be renamed, collapsed and deleted;
  deleting a group keeps its tiles.
- **Sub-groups** (one level) sit side by side inside a group as columns, and wrap onto a new row when there are too
  many to fit. Make one with ⋯ → "New sub-group…" or the "+ sub-group" cell. Drag a tile into a column to move it
  there; drag a sub-group's name onto another sub-group to reorder it (or move it into that group), onto a top-level
  group to make it a group of its own, or onto "+ sub-group" to move it into that group. Its ⋯ menu offers rename,
  move left/right, "make it a group of its own", and delete (which keeps its tiles). A group that has sub-groups
  can't itself become a sub-group. Each sub-group has a group chat of its agents ([Group chats](#group-chats)).
- **Layout not saved** in the top bar means the layout file can't be read as it is. Until you choose "Read the file
  again" or "Save the board's layout over it", group changes are not saved and the desktop keeps the groups it has.
- **N problems** in the top bar counts what could not be read this time, on the desktop or in decoding on the Mac,
  while the rest of the board carried on. Hover over it, or click it, for each source and its error.
- Claude sessions are read from every login in the mailroom's `usage.claude_accounts`; a tile on a non-default
  login shows it, e.g. `claude · work`. Under the usage bars, `next agent → X` is agent-usage's pick for the next
  spawn; running agents keep their login.
- **Credits** (Codex, right of its weekly bar): the Codex credit balance, which Codex spends once its weekly
  allowance is used up. While the week has room the bar is full and shows the number. Once the week runs out, the
  balance it ran out at is the full bar, and the bar shows how much of that is left (`47,751 · 77% left`). It is full
  again when the week resets; credits bought meanwhile make a new full bar. State:
  `~/.local/state/agent-board/codex-credits.json`.
- Clicking a usage meter opens where that window's usage went ([Plan usage](#plan-usage-where-it-went)).
- A selected offline tile says what is left: the process (exited, killed, closed), the tmux session (gone, or open
  with no agent in it), the directory (linked worktree, main checkout, or gone) and any unlanded work.
- The layout (groups and order, keyed by session id) is saved in
  `~/Library/Application Support/AgentBoard/layout.json`.

## Group colours

Each board group has a colour, and the Mail tab, the Time page and the usage breakdown all use the same ones. The
desktop works them out for the layout (`agentboard/colours.py`) and sends them; the Mac draws what it is sent.

- **One file.** A `colours.json` holds every colour and number below:
  - `groups`: the colours of the groups it names, `{"Work": "#0E8A84", ...}`;
  - `reserve`: a list of colours for the other groups;
  - `ungrouped`: the grey of what is in no group;
  - `shades`: the shade rule's lightness step and chroma factor.

  The kit's own, `agentboard/colours.json`, names no group, so every group takes the reserve's colours in layout
  order. To give your groups colours of your own, copy it to `~/.config/agent-board/colours.json` (or the path in
  `AGENT_BOARD_COLOURS`) and name them there: that file, when it exists, replaces the kit's whole, and a kit update
  never touches it. Colours are `#RRGGBB`. A file that does not load is an error the Time page, the usage breakdown
  and the Mail list say, never a silent fallback.
- **A group the file does not name** (one added or renamed later) takes the next colour of the reserve, in layout
  order: the first such group takes the first colour, the next the second. Once every reserve colour is taken, the
  list starts again from its first.
- **Sub-groups** are shades of their group's colour, darker to lighter in layout order, and none is the group's colour
  itself: that stays the colour of whatever is in no sub-group. The first half of the sub-groups (rounded up) are
  darker and the rest lighter. Each side is spread evenly out to its end, one lightness step from the group's colour
  in OKLCH lightness, with the chroma lowered by the chroma factor at the ends. With three, the first is a step
  darker, the second half a step darker and the third a step lighter; a lone sub-group is a step darker.
- **Ungrouped** is the grey of a thread whose agent is in no group: the orchestrator, mailroom notices, a session on
  another machine, a tile never grouped. The Time page's Unsorted time has it too.

**In the Mail list.** A thread takes the group of the agent of its newest agent mail, placed as the Time page places
a tile: its group on the board, kept after the tile closes (`groups-history.json`), while that group is still on the
board. A sub-group no longer on the board is dropped. A strip down the row's left edge has its sub-group's shade, or
its group's colour; hovering over the row names its group and sub-group. When the desktop cannot say the threads'
groups, the list says why ("No group colours: …") and the rows have no strip.

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
- **Start agent here…** runs `agent-spawn --dir PATH` with your instructions plus the path, branch and git state. Its
  **Login** menu is Automatic (the login usage picks, as for any spawn) or one of the mailroom's manual logins, which
  start an agent only when named (`--account NAME`; [A per-token
  login](#a-per-token-login-and-its-spend-meter-optional)).
- **Clean up all fully landed…** (section header) fetches each repo once, runs the dry run on every worktree in
  the section, lists what would go and what stays, and on confirm cleans up each one with the same checks, never
  forced. A worktree whose repo could not be fetched stays, and says so.

The desktop re-checks every request: the path must be a current linked worktree, with no live agent in it.

## Actions

Right-click a tile:

- **Close…** asks first, then sends the agent a fixed close message (`[relayed from <owner_name> via agent-board]
  close`, through `agent-send`). The agent lands its work and closes itself, or emails you if work is unlanded.
- **Clean up…** (offline agents) is plain `agent-close ID`: it removes a linked worktree and its branch only if
  every change is landed on main, and never removes a main checkout. The confirmation shows the dry run first.
- **Force close…** shows what `agent-close ID --dry-run` says would be discarded, makes you type the agent's name,
  then runs `agent-close ID --force`.
- **Send message…** delivers your text to the agent (`agent-send --raw --cli CLI ID`), headed
  `[relayed from <owner_name> via agent-board]` and ending with how the agent marks it done
  ([Sent messages](#sent-messages-what-you-sent-and-what-is-dealt-with)). Images and files pasted or dropped on its
  text, or picked with **Attach…**, go with it, within a Mail-tab reply's limits ("Files", under
  [Mail tab](#mail-tab)): `agent-send --input-json` saves them on the desktop for the agent and ends the message with
  their paths. A message may be files alone. The records keep their names and sizes, never their contents. A
  message a Codex agent takes into its queue but holds there (its last turn was interrupted, say) is shown as not
  delivered, with why: it arrives once the agent starts another turn, so sending it again would deliver it twice.
- **New group chat…** makes a chat with this agent ticked ([Group chats](#group-chats)).
- **Change agent…** / **Change model/effort…** use the mailroom's `agent-switch`: the sheet lists each target (each
  Claude login, or Codex) with its usage status, whether it keeps the conversation or hands off to a new session, and
  the models and efforts valid for that CLI (for a [per-token login](#a-per-token-login-and-its-spend-meter-optional),
  its one model; a model may be named `maker/model`). An idle agent switches at once; a busy one only after "Interrupt
  and switch".
- **Resume…** (offline agents) starts it again the way it was started: a new tmux session in its folder, in its
  own memory-capped scope, continuing the same conversation (`claude --resume ID` on the login the mailroom picks
  for offline resumes, or `codex resume ID` with the thread's model and effort). An agent on a per-token login resumes
  there with that login's settings and model, and every claude the board starts has the mailroom's scrubbed
  environment ([A per-token login](#a-per-token-login-and-its-spend-meter-optional)). The line and the keys that answer
  start-up dialogs are typed under the mailroom's keys lock, so they never mix with keys the mailroom presses in a
  pane (fast mode, a relaunch). It is then told it was resumed, given any note you typed, and told about mail you
  sent while it was down. Refused if its folder is gone. Codex is started with
  `-c check_for_update_on_startup=false`, so a pending Codex update cannot leave it waiting on the "Update
  available" menu (the same holds for agents `agent-restart` brings back). When Codex stops a start because a hook
  it runs is new or changed since it was trusted (a plugin update changes its hook's path, for instance), Resume and
  the restore choose "Continue without trusting": those hooks stay off, and Resume's result and the restore email
  say how many. To trust them, use `/hooks` in any Codex session.
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

**Orchestrator…** (top bar and menu-bar menu) messages the orchestrator like an email, e.g. to start a new agent.
The desktop runs one `agent-orchestrate --source board` event in the background, with your text on stdin after a
header saying it came from you via the board (the Mac app, over SSH), not by email, and that the answer goes to you by
`agent-mail send --to owner`. Files go with it as Send message… takes them (`--input-json`). The run is in a systemd
scope of its own (`systemd-run --user --scope`, unit `agent-orchestrate-<time>-<random>`, no memory cap), so it
outlives the SSH session that started it. A run that fails at once, such as systemd-run finding no user bus, is
refused with the reason; one that stops reading the message before its end is stopped (`systemctl --user stop`), so
it cannot act on part of it, or, if it cannot be stopped, named so you can stop it. Each message carries a key the
Mac makes, so a message sent again after its answer was lost starts no second run. A message holding invisible
characters (Unicode format characters but the joiner inside emoji such as ❤️‍🔥, or tag characters, which text pasted
from agent mail can carry, hiding words from you that the orchestrator reads) is refused, not changed. Like any local
`agent-orchestrate`, it cannot be verified the way an email is (DKIM); it is as trustworthy as the desktop account.
Logs: `~/.local/state/agent-board/orchestrate-*.log`.

## Sent messages: what you sent, and what is dealt with

Everything you type into the board, except your replies in the [Mail tab](#mail-tab) (the mailroom keeps those as
your mail), is kept in full in `~/.local/state/agent-board/sent.jsonl` (mode 600): messages, resume notes, briefs
for new agents, orchestrator requests and group-chat posts, with the names of any files that went with them. A
failed send is kept too, so the text is never lost. Each one has a short ref such as `k3f9`. The delivered text ends
with its ref and one command for the agent to run once it has dealt with all of it (the board's own
`bin/agent-board`, by its full path):

    /path/to/agent-board-kit/board/bin/agent-board done k3f9 -m "what you did, in one line"

Each message is then:

- **waiting**: delivered, not marked done;
- **done**: the agent marked it done. It shows in bold with the agent's note until you tick it off;
- **ticked off**: you ticked it. You can tick any message, and untick it again. Messages recovered from the agents'
  transcripts are marked `recovered` and start ticked off;
- **not delivered**: the send failed, or a Codex agent holds the message in its queue (its note says "Not
  delivered: waiting in Codex's queue: " and why).

A done message from the last 7 days is also ticked off by itself once you have seen the agent's emails about it:
every email that agent sent you from the message until an hour after it marked it done, each one either read (in
the Mail tab or the phone app, or in your Gmail with the [read-check](#optional-your-gmail-read-check-and-forward) on)
or replied to.
The tick's note says so, e.g. `you read its email "Fixed the chip"`. A done message with no such email stays bold,
and a message you have ticked or unticked yourself is never ticked again by mail.

On the Mac, the **Sent** button in the top bar (it reads "2 to check" in bold when agents have marked messages
done), or **Sent messages…** in the menu-bar menu, opens the list. The list can hide ticked-off messages, and "Tick
off all done" clears the bold ones at once. On the desktop, `agent-board sent` prints the last 20 with their states
(`-n 0` for all, `--json` for the records).

## Group chats

A group chat is a thread between you and some of your agents in which every post reaches every member. The mailroom
keeps them (`agent-group`; its README's "Groups" says how posts reach agents and which wake them); the board reads
and writes them as you (`agentboard/chats.py`, the `chat_*` actions). The **Chats** button in the top bar, or
**Group chats…** in the menu-bar menu, opens them; it shows once the desktop has `agent-group`, and reads "2 new" in
bold when chats have posts you haven't had open (kept on the Mac as `chatSeen`). The sheet lists the chats, newest
post first, and shows one: its members with the posts that wake each, its posts, and a box where ⌘↩ posts to every
member. Each post is named by agent-group's label for its sender, and your own by "You". Members are added and
removed from its Members menu, and a chat is hidden or shown from its right-click menu. **New chat…**, or **New
group chat…** on an agent's menu, makes one with the agents ticked; only a named session can be a member.

**Topics.** A chat's posts can be in topics. Beside the posts, the sheet lists **All**, then one row per topic as
agent-group lists them, the most recently active first, then **New topic…**. All shows every post, each with its
topic; a topic shows its own. Your post goes in the topic picked, or in no topic under All; under New topic… you give
a title, and your post opens that topic. The reply button on a post (↩) answers it, and the reply stays in that post's
topic, whichever is picked. The line under the box says where the post will go. A member's own alert setting for a
topic shows beside its setting for the chat, as "named; Release: all". Titles are one line of at most 200 bytes,
with no tabs or other control characters, as agent-group takes them.

Each sub-group of the board has a chat of its own, named after it (`agentboard/subchats.py`). When the Mac sends the
layout, the desktop makes the chat once the sub-group holds two running agents with a session name, and from then
on keeps its title and members in step with the sub-group: moving a tile in or out adds or removes that agent, and
an agent that has exited is left out until it runs again and the layout next changes. The chat is hidden, its posts
kept, when the sub-group is deleted or made a group of its own, and shown again if it comes back. Its members are
the sub-group's to decide, so its Members menu is replaced by a note, and its member line names the sub-group's
agents left out for want of a name a chat can use (letters, digits, `.`, `_` and `-` only, up to 64; `/rename`
gives one). A chat agent-group made but whose answer the board could not read is kept by its id, so it is changed
next time, never made twice. A chat change that fails is listed under the top bar's problems. Top-level groups have
no chat.

Your post is on the record of what you sent (kind `chat`, under the chat's title) and ends like a board message, with
its ref and the command that marks it done; whichever member deals with it runs it, and the Sent list says who. The
board shows your posts without that last line. Each post carries an id the Mac makes, which agent-group keeps as its
idempotency key, so sending the same draft again cannot post it twice. Every id goes to agent-group after `--` or
as `--opt=ID`, so one starting with `-` is never read as an option.

## Mail tab

The **Mail** tab shows the mail between you and your agents, which the mailroom keeps on the desktop: its README
("The Mail tab's mailbox") says what it keeps, how the read, archive and snooze state works, and how it is backed up.
Without Gmail (the kit's default: the mailroom's config `"gmail": false`, its README's "Without Gmail") the Mail tab
and the [phone app](#mail-on-the-phone-agent-mail) are where you read and answer agent mail, and nothing reaches an
inbox anywhere else. With Gmail on, your Gmail receives the same mail, unless you have the mailroom keep agent mail
to you for the tab alone (its README, "Mail to the owner kept off Gmail"; `mailbox.owner_delivery`). The tab's label
counts the unread
threads (`Mail 3`); with no `mailbox` section in the snapshot (a mailroom without the mailbox), the tab says the
desktop has no mailbox yet and asks it nothing. The menu-bar menu has **Mail** too. On this tab the top bar leaves
out the machine meters and plan usage, to give the mail the room.

- **The list** (left): a search over every box (all its words in a thread's subjects, agents' names or text), Inbox /
  Snoozed / Archived / All, the open thread's buttons, and the box's newest 100 threads. A row shows the thread's
  agents and, in a badge, its message count; its time (for a snoozed thread, when it comes back, in amber), its
  subject, the start of its newest message, a paperclip for attachments, **Draft** in red when a reply to it is
  started, and **Fast · 7 min** in violet while it is in fast mode (below). As in Gmail, an unread thread is bold,
  with a blue dot; a read one is plain and greyed; the open one is on blue. Each row has a strip of its agent's
  group colour down its left edge ([Group colours](#group-colours)). A **brown dot** marks a thread one of whose
  agents is at work now (its tile busy, or waiting on a background command): the list carries each thread's agents by
  session id (`mail_list` with `sessions`), matched to the board's tiles, so the dot comes and goes with the tiles.
  Right-click opens a thread in a window of its own (below), marks it read or unread, or archives it or moves it to
  the inbox.
- **The buttons**, above the list, act on the open thread: **Reply**; **Archive** (on an archived or snoozed thread,
  **Inbox**); **Snooze**, until later today (three hours on, to the nearest hour), tomorrow, Saturday or next Monday
  at 08:00, or a date and time; and **Unread**, which also closes it. After Archive, Inbox or Snooze no thread is
  open: **↓** or **j** then opens the thread that was below it in the list, and **↑** or **k** the one above.
- **Undo**: after Archive, Inbox, Snooze, Unread, or a right-click mark, a bar at the foot of the list says what was
  done ("Archived", "Snoozed until Sat 08:00") with **Undo**, for 10 seconds. Undo, **z** or **⌘Z** puts the thread
  back as it was: in its box, read or unread, in its place in the list, and, if it was open, open again at the mail
  being read. Changes in a row stack: each Undo puts back the newest, and the bar then offers the one before. A
  refused Undo is said on the bar, and the list is read again. The read mark that comes with reading a thread offers
  no Undo.
- **The thread** (right) is the document the desktop makes of it, headed by its subject: read messages folded,
  unread ones and the newest open. In a thread of several mails, the one being read is outlined in blue and named in
  a bar above the document ("Mail 2 of 5", its sender and time, with ‹ › to step); a thread opens at its first unread
  mail, else its newest. A thread is marked read once it has been open for a second, up to the newest message on
  screen then, so mail that arrives after is still unread; Archive and Snooze reach only that far too. Left sooner,
  it stays unread.
- **Options and plots**: where an agent's mail asks you to choose or shows numbers, it may carry widgets (the
  mailroom's README, "Widgets in agent mail"), which the tab draws where the agent put them. Options are cards to
  click, checkboxes when several may be picked, with the recommended one marked and what the agent does with no
  answer. A click marks the card picked and opens the reply box; above the box's text each answered question is shown
  whole, as it is sent (the question, the options picked, and "My answer: option 2"), and its × takes it out.
  Right-click an option for **Choose with a comment…**. Nothing is sent until you send it. A plot (line, scatter or
  bar) shows values on hover and hides a series when its name is clicked; bar names that do not fit lie sideways. A
  filled plot draws a bar per row, cut into its segments, each a pale track filled to the row's percentage. A widget
  the tab cannot draw keeps the plain-text version the mailroom put in its place, and the tab says why.
- **Enlarging**: click a plot, a table or an image in a mail to open it in a window of its own, most of the screen
  (`MailEnlarge.swift`): a line or scatter plot there zooms on a dragged range of x (double-click resets). Escape or
  ⌘W closes it. A layout table and a linked image stay as they are.
- **Comments**: select text in an agent's mail and click the **Comment** button beside it, or right-click and
  **Comment…**: the reply box gets the quote, whole, with a comment field, and the text is highlighted. A selection is
  quoted whole or not at all; it is refused, and the tab says why, when it is over 800 characters, partly off screen,
  right to left, or holds text the mail hides by style or layout. The box shows every quote whole and holds Send
  until it has all been scrolled through (`MailComments.swift`).
- **What a reply sends**: the agent's words quoted as Markdown quote lines (`> `), yours never. Each answered
  question, then each comment on text in the order of the text, then what you typed, a blank line between them
  (`ReplyText` in `Compose.swift`, checked against `mac/Sources/AgentBoard/reply-text-cases.json` by
  `--selftest-mail`).
- **Code blocks** (an agent puts a command for you to run in one) have a **Copy** button at the top right, which
  puts the block's exact text on the clipboard (`MailCopy.swift`).
- **Forward** (optional; it needs [your Gmail login](#optional-your-gmail-read-check-and-forward)), in the header of
  each agent's mail beside its time, makes a draft in your Gmail of that one mail and opens it there, for you to
  address and send: a new mail, not a forward, under the thread's subject (no reply tag or Re:), with the mail's text
  and HTML, its inline images and its attachments, and no recipient. The mailroom's own lines in it are left out (the
  note that the mail continues another thread, the review's note on a mail it sent unchanged, and how to answer
  options by mail). A failure, such as a draft over Gmail's 35 MB, is said above the thread (`MailForward.swift`).
- **Keys**, when no text box has the keyboard: **↓** or **j** and **↑** or **k** open the thread below or above;
  **→** and **←** move to the newer or older mail in the thread; **Space** and **⇧Space** scroll the document a screen
  down or up; **e** archives the thread (or moves it back to the inbox); **u** marks it unread and closes it; **s**
  opens the Snooze menu (**↑ ↓** choose, **Enter** snoozes, **Esc** closes); **z** or **⌘Z** undoes the last change;
  **r** opens the reply box; **⌘V** puts files or an image from the clipboard in the reply. In a text box the keys
  type, and ⌘Z is the box's own undo; **Esc** gives the keyboard back, and so does running a search.
- **Windows of their own**: double-click a thread in the list, or right-click it and **Open in new window**, and it
  opens in an ordinary window titled with its subject: resizable, movable to another screen, full screen with ⌃⌘F,
  closed with ⌘W. It shows the whole thread as the tab does, with its own reply box and the thread's buttons. One
  window per thread; several threads can be out at once. The window and the tab share the thread's draft. The tab's
  keys work there for its thread, except the list's (**↓ j ↑ k** scroll the document instead); **e** archives and the
  window stays open, so **z** puts it back (`MailWindows.swift`).
- **Replying**: **Reply** or **r** opens a box under the thread, which answers its newest agent mail; ⌘↩ or **Send**
  sends (in fast mode Return too). The reply carries a key made on the Mac until the desktop has it, so sending again
  after a dropped connection never sends it twice; a reply that fails keeps its text and files and says why. Unless
  the desktop refused it before the mailroom kept anything (`mail_reply`'s `refused`), the mailroom may have kept it,
  so the reply cannot be changed until Send sends it again (which the mailroom keeps once) or it is discarded. Under
  the box, your last three replies in the thread show their state (sending, queued, delivered, with the orchestrator,
  held).
- **Fast mode**, for quick back-and-forths with a thread's agent: the **Fast** switch above the thread runs
  `mail_fast`. While it is on, the mailroom puts the agent at low effort, tells it with each of your messages in the
  thread to answer in a few plain lines, and lets its replies there skip the pre-send review; in the box Return alone
  sends (⇧↩ or ⌥↩ for a new line), and the box stays open after each reply. It ends when you switch it off, or after
  the mailroom's `mailbox.fast_minutes` (5 by default) with no message from you in the thread; the agent's effort
  then goes back. Beside the switch is the mailroom's note on the agent's effort. A thread fast mode cannot be used on
  (a group's, one no single agent owns) is refused, and the tab says why. The mailroom's README, "Fast mode", says
  how it presses the effort keys, and why only on a CLI version they were tried on
  ([CLI updates](#keeping-codex-and-claude-code-updated-agent-cli-update)).
- **Researcher** (optional) appears beside Fast, in the tab and in a thread's own window, when the mailroom reports an
  agent it can route the setting to. It shows the setting you asked for, which holds for that agent across its threads
  and across restarts, with the mailroom's note on its delivery under the bar ([Researcher](#researcher-optional)).
- **Files**: drag files onto the box or the mail, paste them (files copied in the Finder, or an image on its own such
  as a screenshot) or pick them with the paperclip. A reply carries at most 20 files and 25 MB of them together (the
  mailroom's limits), and may be files alone. The mailroom keeps them with the reply and puts them on the agent's own
  machine, naming each file's path in the message the agent receives.
- **Files too big for a reply**: the box asks whether to copy them to the desktop instead. Each goes over SSH to
  `agent-board receive`, into `~/.local/state/agent-board/mail-files/<day>/` on the desktop (readable by you only,
  outside the mailroom and its backup), and a line with its path there joins the reply, which waits until every copy
  has arrived. More than 20 files, a folder, or a pasted image too big are refused, and the box says why.
- **Drafts**: each thread keeps its own draft (its text, answers, comments and files) on the desktop, with a copy on
  the Mac; the box's **Versions** menu (a clock beside Discard) brings back an earlier one, and Discard goes on the
  Undo bar ([Drafts](#drafts)).
- **Updates**: while the tab is on screen, or a thread is out in a window of its own, the list is read every 15
  seconds, and ⌘R reads it at once. The open thread is read again when the list shows it changed, or while a reply in
  it is sending or queued. Threads the tab has read are kept on the Mac (up to 64 MB of documents), and the two
  threads on each side of the open one are read before they are asked for.
- **Safety**: the desktop's document allows only formatting, http, https and mailto links, and `data:` images, under
  a strict Content-Security-Policy. The Mac shows it in a WKWebView that runs no JavaScript from the page, blocks every
  load but `data:` URLs with content rules, keeps no cookies or cache, and never navigates: a clicked http, https or
  mailto link opens in the browser or mail app, and any other link does nothing. Widgets, comments, Copy and Forward
  are the app's own scripts (`mac/Sources/AgentBoard/widgets.js` and the `Mail*.swift` files), each in a content world
  of its own, making every node from the spec's text, never from markup: agents send only data, and a click can only
  put the line it shows in the reply or open its item enlarged.
- **Attachments**, an agent's or the files of your own replies, are fetched from the desktop on click (`mail_part`)
  into `AgentBoard-mail/` in the Mac's temporary folder, readable by you only and quarantined (macOS asks before
  running anything in it), and opened with their default app.

The desktop side is `agentboard/mailbox.py` and the `mail_*` actions. Each but `mail_forward` runs `agent-mail mailbox
...` as you and hands its JSON answer to the Mac unchanged, except that `mail_list` asked with `sessions` adds each
thread's board agents (`sessions`, from the mailroom's store, read-only) and its group colours (`group`, `subgroup`,
`colour`, `shade`), or `sessions_error` / `groups_error` saying why not. agent-mail refuses to change the mailbox from
inside an agent session. A reply is the mailroom's record of it (an inbound mail from you), routed within a second or
two exactly as your emailed replies are: to the agent's session, to the group chat for a group's mail, or to the
orchestrator for system mail or an agent that has gone. The actions record (`actions.jsonl`) has every reply's thread,
key, length and its files' names and sizes, every mark except a read one, every switch of fast mode with its agent and
effort, and every Researcher switch with the agent the mailroom confirmed. `mail_forward` reads the mail as the mailroom
sent it from its message store, read-only (`agentboard/mailforward.py`), and makes the draft with your Gmail login
(Gmail's `drafts.create`, through its upload endpoint); the record has the draft, its size and its files' names.

## Drafts

A reply started in the Mail tab or the phone app is a draft, one per thread, kept on the desktop
(`agentboard/maildrafts.py`, and the `draft_*` actions `agentboard/actions.py` lists), so the tab, a thread's own
window and the phone always show the same one and a draft outlives either app. Each change says which device made it
(`mac` or `phone`): a version's line names it ("Not taken 14:02, phone: ..."), and a draft that changed on the other
device since it was read says so ("This draft changed on your phone since; your version is under Versions.").

- **What it holds**: what you typed, your answers to an agent's options with their comments, your comments on its
  text, its files, and, while a reply may be on its way, that reply's key. The desktop keeps them in
  `~/.local/state/agent-board/mail-drafts/` (readable by you only): `threads/<thread>.json` for each thread's draft
  and versions, and each file once, under the SHA-256 of its bytes, in `files/<thread>/`.
- **Saving**: the Mac reads a thread's draft from the desktop when the thread opens, and again when the desktop's list
  of drafts, read with each list of threads, shows it changed. It saves it there about a second after typing stops
  and at once on leaving the thread. A file goes to the desktop before the draft that names it. Each save names the
  rev of the draft it was made on; the rev goes up with every change. A read or save that fails is said under the box
  until one succeeds.
- **A save on a stale draft**: a save made on a draft that has changed since (in another window, or on the phone) is
  not taken. The desktop keeps it as a
  version, and the box shows the draft as the desktop has it, with a note that your version is under Versions.
- **Versions**: the desktop keeps a version of a draft when it is discarded, when a save replaces content last kept
  more than 2 minutes before, when a save empties it, when a save is not taken (above), and when an older version is
  brought back over it. It keeps at most 20 a thread, none for more than 7 days, and one of any content. The
  **Versions** menu, a clock beside Discard, lists them newest first: why each was kept, when it was written, and its
  first words. Choosing one brings it back, the draft it replaces kept as a version.
- **Discard** empties the draft, kept as a version, without asking first: the Undo bar ("Draft discarded") brings it
  back.
- **Sending**: Send saves the draft with the reply's key first, then sends the reply, its files named from the
  desktop's copies. Once the mailroom has the reply, the desktop empties the draft holding that key, keeping no
  version (the mail holds it); when it cannot read the drafts, the reply has still gone and its answer says the draft
  was not emptied.
- **The phone** keeps no copy: what is typed there goes to the desktop a second after typing stops, and at once when
  the thread or the reply is left or the page is hidden, so a draft outlives the page; until the desktop has it, it is
  in the page's memory only.
- **The Mac's copy**: `~/Library/Application Support/AgentBoard/mail-drafts/` holds `drafts.json`, each draft as it is
  on the Mac with the rev the desktop had when the Mac last read or saved it, and a copy of each file the desktop has
  not taken yet. It is written ahead of the desktop, so a save that fails (the desktop asleep, the connection
  dropped) loses nothing: what the desktop has not taken goes there at the next list of threads it answers.
- **Settings**: `~/.config/agent-board/drafts.json` (`AGENT_BOARD_DRAFTS_CONFIG` overrides the path) may set
  `versions` (20), `version_days` (7), `version_minutes` (2) and `upload_hours` (24: how long a file uploaded but
  named by no draft or version is kept).
- **The record** (`actions.jsonl`) has each change's thread, rev, device and sizes (characters, answers, comments,
  files), never its words.

## Researcher (optional)

**Researcher** is a per-agent switch in the [Mail tab](#mail-tab) and the [phone app](#mail-on-the-phone-agent-mail)
for agents that do research with the public `researcher` Claude Code plugin (an 11-step research workflow: literature
search, novelty check, a challenged plan, experiments, an independent audit of the results, a write-up). While it is on
for an agent, that agent must run the plugin's interactive workflow and get your approval of its methods, by Agent Mail,
before it runs experiments. The mailroom keeps the setting and does the work (its README, "Researcher mode"); the
board only shows the switch and passes your choice on.

**Install the plugin** on each machine whose agents should use it (the desktop, and the Mac if agents run there). The
kit's installer does it on the desktop when `kit.env` has `WITH_RESEARCHER="yes"`, from the kit's own plugin
marketplace, which names the plugin's public repository:

```bash
claude plugin marketplace add ~/code/agent-board-kit/mailroom/config/researcher-marketplace   # your kit checkout
claude plugin install researcher@agent-kit
```

Check that `/researcher:researcher` is offered in a new session. The mailroom checks, on the agent's own machine, that
the plugin is installed and new enough (0.7 or later) and that its helper runs, before it confirms the switch on, and
the switch's note says when it is missing (the mailroom's README, "Researcher mode").

**What you see.** The switch appears beside **Fast** above a thread, and in a thread's own window, only when the
mailroom reports an agent it can route the setting to (the thread's `researcher`, below); otherwise it is hidden. It
shows the setting you asked for, which holds for that agent across all its threads and across restarts. Under the
thread bar a note says where the switch stands: "Delivery pending", "Delivery queued", "Awaiting agent confirmation",
"Delivery failed", with the mailroom's reason when the agent is unavailable (the plugin missing, say), or, once it is
on, that the agent will ask you to approve its experiment plan in Mail. A switch the mailroom refuses (the thread's
agent changed since you opened it) is said at the foot of the tab, and the thread is read again.

**The desktop side** is the `mail_researcher` action: `{"thread": T, "on": B, "expected_tag": TAG}`, TAG being the
routing tag the thread showed (`researcher.tag`). It runs `agent-mail mailbox researcher --expect-tag TAG -- THREAD
on|off`, and answers with the thread as the mailroom then lists it. The mailroom refuses the switch when the thread's
route has changed since (another agent took over), so a switch never lands on an agent you did not see. The board
counts the switch done only when the mailroom's answer confirms that exact state for that tag, and puts the agent it
confirmed (or the problem) on the record (`actions.jsonl`). Every thread the mailroom lists or shows may carry
`researcher`: `{"on", "agent" (its display name), "tag", "delivery"?: "pending" | "queued" | "accepted" | "applied" |
"unavailable" | "error", "delivery_detail"?}`, or null when no agent can take it. With a mailroom that has no
Researcher, threads carry no `researcher` and the switch never shows. On the phone, a switch needs the passkey again,
as a reply does ([How it is protected](#how-it-is-protected)).


## Mail on the phone (Agent Mail)

`agent-board mail-web` serves the Mail tab to your phone as an app, **Agent Mail**, which Chrome installs on the home
screen and opens full screen: the list, search, a thread, archive, snooze, mark unread, Fast, Researcher, replies with
files, options and plots, drafts shared with the Mac ([Drafts](#drafts)), and messages to the orchestrator, through the
same `mail_*`, `draft_*` and `orchestrate` actions as the Mac, and (when you turn it on) unlocking the desktop's keyring
with the same command as the Mac ([Unlock the desktop](#unlock-the-desktop-optional)). It is light or dark as the phone
is. Files too big for a reply stay on the Mac. It is optional and off until you set it up (below); the Mac app is needed
only to show the enrolment code, and even that can be done without it.

The page is built and tested for Chrome on Android (passkeys, the back gesture, installing without a service worker);
another browser with passkeys may work, but is not tested.

- **The list** is the Mac's: a search over every box, Inbox (with its unread count) / Snoozed / Archived / All, the
  counts, and the box's newest 100 threads. Unread is bold with the blue dot, read greyed; each row has its group's
  strip, as on the Mac ([Group colours](#group-colours)), and "No group colours: …" over the list says when the desktop
  could not send them. A row has the thread's agents, its message count, **Draft** in red when a reply is started,
  **Fast · 7 min** in violet while it is in fast mode, a paperclip, and its time (for a snoozed thread, when it comes
  back, in amber). The Mac's brown busy dot is left out: the page's server has no cheap source of the agents' states.
  Swipe a row left to archive it (or, out of the inbox, to move it back), with **Undo**; pull the list down to read it
  again.
- **A thread** slides over the list: the desktop's document, headed by its subject. Archive (or Inbox), Snooze (later
  today, tomorrow, this weekend, next week, or a date and time) and Unread go back to the list with Undo. **Fast**
  switches fast mode on or off as on the Mac: while it is on it is violet and reads **Fast · 7 min**, with the
  mailroom's note on the agent's effort under the bar; a refusal is said at the foot of the screen. **Researcher**
  sits beside it as on the Mac, when the mailroom reports an agent for it, with its note under the bar; a switch needs
  the fingerprint, as a reply does. A floating **Reply** button (**Draft** while a reply is started) opens the reply
  sheet.
- **Options, plots, code and files.** A button at the bottom left names what the document's frame cannot show itself
  (such as **Options · Plots · Code · File**) and opens it in a sheet: an agent's options and plots, drawn by the Mac
  tab's own renderer (`mac/Sources/AgentBoard/widgets.js`, served from that one copy), each mail's under its sender and
  time. A plot tapped opens larger in a box over the page. A tap on an option marks it picked and keeps that question's
  answer in the thread's draft, and the sheet stays up for the other questions; a long press picks with a comment and
  opens the reply on the comment's field. The reply shows each answer above your text as it is sent, each with an ×
  to take it out, and sends what the Mac would ("What a reply sends", under [Mail tab](#mail-tab); both copies of that
  rule, `ReplyText` and `web/mail/replytext.js`, are checked on `mac/Sources/AgentBoard/reply-text-cases.json`); when
  the answers do not all fit, Send waits until they have been scrolled to the end. Each mail's code blocks are listed
  there too, each with a **Copy** button, and its attachments, each downloaded at a tap.
- **A reply that fails** holds still as on the Mac, its answers too, with the Mac's words saying why; one that never
  left the phone (the fingerprint not given) can still change. Discard lets it go.
- **The orchestrator.** The list's floating **Orchestrator** button opens **Message the orchestrator**: a text box,
  then **Send**, which needs the fingerprint as a reply does. Its answer, or why not, is said at the foot of the
  screen; the header the orchestrator reads says the message came from the phone app. It goes on the Sent list like
  the Mac's own ([Sent messages](#sent-messages-what-you-sent-and-what-is-dealt-with)).
- **Unlock the desktop** (only with `keyring_unlock` on): while the desktop's keyring is locked, a red **Desktop
  browser locked** mark sits over the list (grey **Desktop keyring unknown** when its state can't be read), from the
  session's status, read again with the list. A tap opens **Unlock the desktop**: type the keyring's password and tap
  **Unlock**, then touch the fingerprint sensor. The sheet says the answer as the Mac does (unlocked, wrong, partial,
  too many tries). The field is emptied as it is sent, on closing the sheet, on signing out and whenever the page is
  hidden, and Chrome is never offered the password to save (`autocomplete="one-time-code"`).
- **Back and reload.** Opening the app, or reloading it, always shows the list. Android's back gesture closes a sheet,
  else leaves a thread for the list where it was, else leaves the app. The history holds only which view is showing,
  never a thread.
- **Speed.** The list and the session's status are asked for together, over a skeleton of the list drawn at once. The
  top 4 threads are read before they are asked for (2 at a time), and the 2 below a thread opened, so most open without
  asking the desktop; each box's last list shows at once when it is picked again; a change shows at once and is sent
  behind it. All of it is in the page's memory only (up to 24 million characters of documents).

### Set it up

On the desktop, once (the kit installer does step 2 always, and step 3 with `WITH_PHONE_APP="yes"` in `kit.env`):

1. **Tailscale** on the desktop and on the phone, both logged in to your tailnet with your own login. In the
   Tailscale admin console, turn on **MagicDNS** and **HTTPS certificates** (`tailscale serve --https` needs both).
   Never turn on **Funnel** for the page's port: the page refuses to run while it is reachable from the public.
2. **The Python package**: `cryptography` in the Python the unit runs on (`board/requirements.txt`; the kit installer
   puts it in its venv, `~/.local/share/agent-kit/venv`; on Ubuntu, `python3-cryptography` also does).
3. **The unit**: `systemd/agent-board-mail-web.service.in`, rendered with `{{PYTHON}}` (the kit venv's Python) and
   `{{KIT_DIR}}` (the kit checkout), runs `{{PYTHON}} -I {{KIT_DIR}}/board/bin/agent-board mail-web`. By hand:

   ```bash
   KIT=~/code/agent-board-kit; PY=~/.local/share/agent-kit/venv/bin/python
   sed -e "s|{{PYTHON}}|$PY|" -e "s|{{KIT_DIR}}|$KIT|" \
       $KIT/board/systemd/agent-board-mail-web.service.in > ~/.config/systemd/user/agent-board-mail-web.service
   systemctl --user daemon-reload
   ```

4. **The settings**: the phone's Tailscale node id and tailscaled's uid, in `~/.config/agent-board/mailweb.json`. The
   node id is the phone's `ID` in `tailscale status --json` (not its name):

   ```bash
   tailscale status --json | python3 -c 'import json, sys
   for p in json.load(sys.stdin)["Peer"].values(): print(p["ID"], p["DNSName"])'
   mkdir -p ~/.config/agent-board
   echo '{"devices": ["nAbC123CNTRL"], "proxy_uids": [0]}' > ~/.config/agent-board/mailweb.json
   ```

   The page answers the tailnet logins in `logins`, by default your addresses in the mailroom's config
   (`owner_email`, `allowlist`). If you log in to Tailscale with another identity (a GitHub login is `name@github`;
   `tailscale status --json` lists it under `User`), name it: `"logins": ["name@github"]`.
5. **Start it**: `systemctl --user enable --now agent-board-mail-web.service`. While the settings or Tailscale are not
   ready, the server stops with the reason in its journal (`journalctl --user -u agent-board-mail-web`), and the unit
   tries again every 10 seconds.
6. **Publish it on your tailnet** (once the unit has made its socket; `--bg` keeps it across reboots):

   ```bash
   sudo tailscale serve --bg --https=8466 unix:$HOME/.local/state/agent-board/mail-web/page.sock
   ```

   It needs `sudo` unless Tailscale has you as its operator (`sudo tailscale set --operator=$USER`, once). Use the
   port in `mailweb.json` if you changed it. Never run `tailscale serve reset`, which also removes any other served
   ports, or `tailscale funnel` for this port.
7. **Enrol the phone**: on the Mac, right-click the menu-bar icon, **Phone…**, **Show an enrolment code**. Scan it
   with the phone's camera, open the link, tap **Enrol this phone** and touch the fingerprint sensor. Without the Mac
   app, `echo '{"action": "mail_web_enrol"}' | agent-board act` on the desktop prints the same one-time link (`url`)
   to open on the phone. Each enrolment is a [security alert](#security-alerts-on-the-mac).
8. **Install it**: in Chrome's ⋮ menu, **Add to home screen**, then **Install**. **Agent Mail** opens full screen from
   the home screen, already signed in (the app and Chrome share the session).

The **Phone…** sheet also signs the phone out (every session ends) or forgets it (its passkeys too, so it must be
enrolled again). `sudo tailscale serve --https=8466 off` takes the page off the tailnet, and
`systemctl --user disable --now agent-board-mail-web.service` stops it.

`~/.config/agent-board/mailweb.json` (`AGENT_BOARD_MAILWEB_CONFIG` names another file):

| Key | Default | Meaning |
|---|---|---|
| `devices` | `[]` (required) | the Tailscale stable node ids of the devices the page answers: your phone's `ID` |
| `proxy_uids` | `[]` (required) | the uids whose connections the server takes: tailscaled's, root's `0` |
| `logins` | `[]`: the mailroom config's `owner_email` and `allowlist` | the tailnet logins (Tailscale-User-Login) the page answers |
| `port` | `8466` | the tailnet port `tailscale serve` publishes the page on: 1024 to 65535, never one Funnel can open (443, 8443, 10000) |
| `session_hours` | `12` | how long a sign-in lasts (1 to 24) |
| `enrol_minutes` | `10` | how long an enrolment code shown on the Mac lasts (1 to 10) |

The passkeys, sessions, the waiting code and the socket are in `~/.local/state/agent-board/mail-web/` (mode 700, each
file 600; `agentboard/mailwebstate.py`).

### How it is protected

Each layer stands on its own (`agentboard/mailweb.py` says how each check is made):

- **Encrypted from the phone to the desktop.** The server listens only on a Unix socket in its state folder, readable
  by you alone. `tailscale serve` publishes it to the tailnet over HTTPS at `https://<the desktop's tailnet name>:8466/`,
  so the traffic is WireGuard-encrypted between your own devices, with TLS inside it.
- **Only tailscaled's connections.** A connection is taken only when the process at its other end runs as a uid in
  `proxy_uids`, as the kernel says of the socket (SO_PEERCRED) before a byte is read; at most 16 at once, and at most
  8 requests from one device at once. A request's line and headers must arrive within 10 seconds, and its body within
  10 seconds and a second per 64 KiB (at most 10 minutes).
- **Never public.** The page answers nothing while any port open to the public through Tailscale Funnel is its port,
  or has a target on its port or on its socket (by any path to the same file), in the background config or a
  foreground `tailscale funnel`; it checks every 30 seconds, and when the check fails, or meets a status it cannot read,
  it answers nothing and logs why. It refuses to start in that state, and refuses any request that came through Funnel.
  The settings refuse the ports Funnel can open.
- **Only your phone.** A request is answered only when its tailnet login is one the page answers and `tailscale
  whois` on its address names a device in `devices` logged in as that same login; and only when it asked over HTTPS
  for exactly the desktop's tailnet name at the page's port (as tailscaled reports it in X-Forwarded-Proto and
  X-Forwarded-Host).
- **A passkey with your fingerprint.** Mail needs a session, which a passkey sign-in gives (WebAuthn, with the user
  verified: fingerprint or screen lock). The passkey is discoverable, so signing in names no credential: nothing on
  the phone can learn its id to send bad signatures for it. Sessions last 12 hours, are kept on the desktop (the
  cookie is HttpOnly, Secure, SameSite=Strict) and stay on the device that signed in. Signatures of an enrolled
  passkey that fail to check are limited to 5 in 15 minutes. Enrolling, signing in and approving an action each keep
  their own challenges. The signatures are checked with the `cryptography` package (`agentboard/webauthn.py`), never
  by hand.
- **Each reply, Researcher switch, orchestrator message and unlock approved again.** A reply carries words to an
  agent, Researcher changes how an agent runs, the orchestrator can act as you, and an unlock carries your desktop
  password, so each needs the session's passkey again, over a challenge bound to that action and its exact request
  (its SHA-256). For Researcher this binds the approval to the thread, the routing tag shown and the state asked for.
  Fast mode carries no words to an agent, so it needs only the session, as archiving does.
- **The desktop password, approved each time and kept nowhere** (only with `keyring_unlock` on; off, the server
  refuses an unlock before it looks at it). What the passkey approves for an unlock is a random nonce the page makes
  for that one submission (`{"nonce": …}`), never the password: a digest of a password can be guessed back offline, so
  none is made. The password leaves the phone once, after the fingerprint, in the unlock's own request: its JSON line
  (the nonce and the signed approval), then the password on the next line, outside what the approval covers. The
  server takes it only with a valid approval of that action and that nonce, from the session's passkey, used once.
  Before running anything it refuses a password the unlock command's own rule refuses (1 to 1024 bytes on one line, no
  NUL, no carriage return at the end), so a hand-made client cannot use up tries. It reads the request's line and
  headers a line at a time, peeking for each line's end, so that its body stays in the kernel until it is read straight
  into one buffer; it copies only the JSON line out of that buffer, and zeroes it once the request is answered. The
  password goes only to `agent-board keyring-unlock`'s stdin, as a view of that buffer: a Unix socket, which no other
  process can open through `/proc`, written only once the command has made itself undumpable; the command gets only
  the environment it needs, and its outcome alone goes on the record.
- **Enrolment only from the Mac's code.** A passkey is enrolled only with a one-time code the Mac shows as a QR code,
  valid for 10 minutes; failed tries (a wrong code, or a passkey that does not check out) are limited to 5 in 15
  minutes. Each enrolment has a user handle of its own. You are told of each enrolment by a security alert
  (`agent-mail send --unrouted --alert phone-enrolment`), and an enrolment you cannot be told of does not happen: a
  mailroom that cannot send or keep the alert, or one older than `--alert`, refuses it, and no passkey is enrolled.
  With Gmail on, the mailroom sends it through Gmail even when it keeps your other mail for the Mail tab; without
  Gmail it is kept for the Mail tab and also becomes a [notification on the Mac](#security-alerts-on-the-mac), which the
  phone cannot dismiss.
- **Mail in a sandbox, and what the phone keeps.** The page runs only its own scripts (`web/mail/`, `web/passkeys.js`
  and the Mac tab's widget renderer) under a policy that loads nothing from elsewhere. A thread is the document the
  desktop has already made safe (the mailroom's README, "The Mail tab's mailbox"), shown in a sandboxed frame that runs
  no script; its links open in a new tab. Its options and plots are drawn in the sheet over it by the page's own
  script, every node made from the agent's data, never from markup. Its code blocks are read from the document with
  DOMParser, which runs and loads nothing, and listed as text only. Its attachments are downloaded by the page's own
  script, never shown in the page. Every answer, errors included, says no-store and may not be framed, so the phone's
  browser keeps no mail. The page has no service worker and uses no storage (no localStorage, IndexedDB or Cache
  Storage); the tests check both. Its lists, threads and drafts are in the page's memory, forgotten on signing out; a
  draft is kept on the desktop. What the phone does keep: the session cookie, until it lapses; Android's snapshot of
  the last screen, in Recents; attachments you download; text you copy; the installed app (its name, address and
  icons); and Chrome's own record of visits (the address and the title Agent Mail).
- **Installed without a service worker.** Chrome on Android installs a page from its menu with only a manifest
  (`web/mail/manifest.webmanifest`: Agent Mail, standalone, start and scope `/`), served as the page is (only to your
  phone, no-store, nosniff). Installing has Chrome make a WebAPK through Google's service: it receives the manifest,
  so the name Agent Mail, the icons and the start address with the desktop's tailnet name and the port, never a
  cookie, a passkey or mail. The tailnet name is public already: Tailscale's HTTPS certificates are in the
  certificate transparency logs.
- **Hardened, and on the record.** The unit runs the server in Python's isolated mode (`-I`), so nothing from a user
  site, `PYTHONPATH` or the current folder loads in it (the kit venv's own packages still do), and the server makes
  itself undumpable with no core files as it starts (`agentboard/lockdown.py`). Each request is logged in the unit's
  journal with its login and device, and so is each sign-in, enrolment, refusal and failed action; what the page
  changes goes on the board's record (`actions.jsonl`, `sent.jsonl`) as the Mac's actions do.

**What it does not cover.** The page guards against the network: the internet, your other devices, anyone on your
tailnet, and a session used from anywhere but your phone. It does not guard against anything running on the desktop as
you or as root: any program running as you can read the mail, run agent-mail as you, and rewrite the page's settings,
state and code (`web/mail/` and `widgets.js` are read from the kit checkout on each request).

- The page shares its host name with anything else you serve from the desktop through Tailscale, on other ports.
  Cookies are not kept apart by port, so a page on another of those ports could see or overwrite the session cookie
  while the phone visits it; it cannot use the cookie, which works only from your phone through tailscaled.
- On Android, Chrome keeps passkeys in Google Password Manager by default: synced to your signed-in Google devices and
  end-to-end encrypted, unlocked with your screen lock or fingerprint. A copy elsewhere can still sign in to this page
  only from your phone's tailnet node.

## Security alerts on the Mac

A few of the mailroom's alerts are about who may reach your agents or your mail, or what fast mode may have left wrong
in an agent's pane (the mailroom's `SECURITY_ALERTS`: `phone-enrolment` from the phone app, `fast-mode` and `fast-keys`
from fast mode, and with Gmail also `dkim`, `collab-thread` and `flood`). With Gmail on, the mailroom always sends
them through Gmail. Without Gmail it keeps them for the Mail tab like any alert, where whoever caused one could read
and archive it from the phone before you saw it, so it also appends each one to
`~/.local/state/agent-mail/security-alerts.jsonl` (the mailroom's README, "Security alerts without Gmail").

The collector reads that file with each snapshot (`sources.security_alerts`: the same path rule, under
`AGENT_MAIL_STATE_DIR` when that is set; no file, no alerts) and passes on the entries of the last 7 days as the
snapshot's `security_alerts` (`[{"at", "kind", "subject", "thread_id"}, ...]`, oldest first); a line that is not an
alert is passed over and counted under the top bar's problems. The Mac app (`SecurityAlerts.swift`) posts one macOS
notification for each entry newer than the newest it has notified, titled by its kind ("Security: a phone was
enrolled for Agent Mail"), with the alert's subject, and keeps that time in its defaults (`securityAlertNewest`), so
neither a relaunch nor a reconnect notifies one twice. A click opens the alert's thread in the Mail tab. Nothing on the
phone app can hide or clear them.

**The trade-off.** Without Gmail you learn of these on the Mac, from the notification and the Mail tab, not on a
separate channel outside the desktop and the Mac: anything that can write to the desktop's state as you can change the
file, as it could the mailbox. The Mac must allow AgentBoard's notifications (System Settings → Notifications);
without that permission the app falls back to a plain Notification Center message, which opens nothing.

## Plan usage: where it went

Clicking a usage meter in the top bar opens where that window (5-hour, weekly, or a model's own weekly) has gone
since it last reset: a chart of the window stacked by board group in the groups' colours, with **Other** on top, and
below it a list. Click a group in the list to open its sub-groups, and a sub-group to open its agents. The desktop
works it out on request (the `usage_split` action, `agentboard/allowance.py`):

- **Agents' use.** Every reply in the agents' transcripts is priced at API rates. A Claude agent's subagents count as
  its own, and so do a Codex thread's sub-threads and review threads; the orchestrator's runs make one row. A reply
  counts once, though failover copies a conversation between logins and a fork copies its history, and it counts on
  the login its agent was on when it was written (read from the moves in the status log).
- **Percent of the window.** One factor per window turns the agents' dollars into percent. It is fitted, through
  zero, on 6-hour stretches of the board's own usage readings from the last 21 days: each stretch's rise in the
  window against the agents' dollars in it. The collector records a reading at most every 30 minutes while the Mac
  app is connected (`~/.local/state/agent-board/usage-samples.jsonl`). The sheet says how many stretches it used and
  how far one typically misses. With too few stretches (in the first days, or with the Mac rarely connected), the
  shares are in dollars.
- **Other** is the reading less the agents' estimate: the claude.ai and phone apps, and sessions the board does not
  see.
- **Groups.** Agents are grouped as the Time page groups their tiles, without its model guesses. A session never
  placed on the board goes with the tiles of its repo ("grouped by its repo"), else in Unsorted.

The parsed transcripts are cached in `~/.local/state/agent-board/allowance-cache.json` (private); each file is read
on from where the last read stopped, so the first request after a while takes longest. Each answer adds a line to
`allowance.log`. `allowance.DEFAULTS` lists the settings: the **prices per model** (dollars per million tokens:
input, output, cache read), the stretch length, how far back the fit reads, and how many stretches it needs.
`~/.config/agent-board/allowance.json` overrides them, e.g. to price a model released after this kit:

```json
{"prices": {"claude-new-model-1": [5, 25, 0.5]}}
```

The sheet names what it leaves out: replies from a model with no price, transcript lines that cannot be read, and
Codex threads whose first line does not name the thread.

## A per-token login and its spend meter (optional)

Besides plan logins, the mailroom can hold a Claude Code login whose requests go to a model on OpenRouter and are billed
per token: Claude Code itself, on a login folder of its own (such as `~/.claude-openrouter`), with a settings file that
points it at OpenRouter's endpoint and one model. Such a login is **manual**: nothing routes to it, fails over to it or
moves an agent onto it; an agent runs there only when you name it. The mailroom's README says how to set one up: its
`usage.claude_accounts` entry (`config_dir`, `settings`, `model`, `manual: true`, `weekly_budget_usd`, `label`), and the
OpenRouter API key, which comes from a mode-600 file or an environment variable you name, never from the kit.

On the board:

- **Starting an agent on it.** The worktree tile's **Start agent here…** has a **Login** menu: **Automatic** (the login
  usage picks, as for any spawn) or one of the mailroom's manual logins, which the collector sends in the snapshot as
  `spawn_logins` (`[{"name", "label"}]`, from the mailroom's `accounts.manual`). The desktop takes only a name from that
  list (`wt_spawn`'s `account`) and passes it to `agent-spawn --account NAME`; any other is refused before anything
  runs, and the record keeps the login.
- **Switching to it.** **Change agent…** lists it with the plan logins; it runs only its own model, which the sheet
  offers (and `agent-switch` uses unless you pick another model the login takes: a model name may be OpenRouter's
  `maker/model`).
- **Resuming on it.** Resume brings an agent back on the login the mailroom places it on, and an agent last on a
  per-token login resumes there with that login's `--settings` and model, never the Claude model its transcript names
  (which would fail on OpenRouter's endpoint). An agent moved off it takes the new login's default model. Every claude
  the board starts (Resume, the restart's restore, the voice desk's copies) gets the mailroom's scrubbed environment
  (`accounts.claude_env`): no other login's `CLAUDE_CONFIG_DIR`, `ANTHROPIC_*`, or the variables any login's settings
  file sets, which Claude Code passes to every command an agent runs, so a claude started from a per-token agent's shell
  does not inherit its endpoint. A resume's shell line unsets those names too, among its own and the tmux server's.
  Without a mailroom that knows per-token logins, none of this applies and claude runs as before.
- **The spend meter.** A login with a `weekly_budget_usd` has no plan windows, so its place under the usage meters is
  one meter, "$X of $B this week", filled by the share of the budget spent: blue up to the budget, red past it, where
  the number keeps counting. Hover over it for since when and what was left out. The week starts on Monday at 00:00 in
  `allowance.json`'s `week_zone` (the desktop's local time when unset). The dollars are its agents' replies since then,
  priced at OpenRouter's rates (`allowance.DEFAULTS` has some; add yours to `allowance.json`'s `prices` as
  `[input, output, cache read, cache write]` dollars per million tokens), by the same reading of the transcripts as
  [Plan usage](#plan-usage-where-it-went): each reply once, on the login it ran on by the status log's moves. Only that
  login's folder is read, through its own cache, `spend-cache.json`. Replies on a model the login does not run (a Claude
  model id fails there) are left out and named, as are models with no price. The collector works it out with each
  usage reading, every 5 minutes, off the loop that builds snapshots (`allowance.weekly_spend`), and puts it on that
  login's usage account as `spend`: `{"dollars", "budget", "since", "notes"}`.


## Time: where your time went

**Time** in the top bar, or in the menu-bar right-click menu, opens a chart: one bar per day over 7, 14 or 30 days,
stacked by board group, with each group's sub-groups in shades of its colour ([Group colours](#group-colours)).
Hovering over a segment names it under the chart. Clicking narrows the view: a group (in the legend or a bar) to its
sub-groups, then a sub-group, and a day's date under its bar to what counted that day. **1 day** shows one day across
its hours instead: a lane per group and sub-group, each 5-minute block it was active in filled at its clock time,
hatched where the group is a guess. The menu beside the range chooses which time is counted: **All time**, **Work
time** or **Outside work** (the `work_hours` setting, Monday to Friday 09:00 to 18:00 by default). Below the chart is
a list of the tiles and items behind what is shown; clicking one shows how its time adds up.

**What counts.** Time is counted in 5-minute blocks. A block counts when you did any of the following in it, and it
is shared evenly between the groups active in it, so a day's total never exceeds your active time. Only the first
three are on by default; each of the others is on only when its source is set up, and a source that is off is simply
left out (not an error).

| Activity | Read from | On when |
|---|---|---|
| prompts you typed to an agent | transcripts of interactive Claude Code and Codex sessions. Briefs, messages that agent-send or the mailroom delivered, and `claude -p` runs do not count | always |
| board messages | `sent.jsonl`. A request to the orchestrator counts for the agent it relayed to or started soon after; a group-chat post is an item of its own | always |
| emails to agents | the mailroom's inbound mail from your addresses (Mail-tab replies included, counted once), with the same rule for mail the orchestrator handled | always |
| meetings | timed events on your primary Google Calendar that you have not declined, inside `work_hours` | `google_token` is set |
| documents | your revisions to Google Docs, Sheets and Slides | `google_token` is set (with Drive read access) |
| your own email | mail you sent from your Gmail, except to the agents | `gmail_credentials` is set |
| to-dos | tasks you made in the to-do list, and the moments they were closed (`gh`) | a tasks repo is known (`tasks_repo`, or the to-do component's config) |
| screen time | your Mac, from [ActivityWatch](https://activitywatch.net): the app and window title on screen longest in each block you were active for a minute or more. It counts only in blocks none of the above accounts for | the Mac app's `activityWatch` setting is set and it has uploaded once |

Screen time comes from the Mac app. With `defaults write org.agentkit.agentboard activityWatch http://localhost:5600`
(ActivityWatch installed on the Mac and given the Accessibility permission), the app uploads the blocks with the
`screen` action at launch and every 15 minutes; nothing else leaves the Mac. The desktop keeps the blocks of the
page's span in `screen.json`, private. Agents that edit a Google document with your login count as your edits: Drive
cannot tell them apart.

**Groups.** The page follows your board layout as it is now, for every past day.
- An item the `fixed_groups` setting lists always counts in the group it names there.
- A tile you grouped keeps that group after it closes (`groups-history.json`).
- A tile you haven't grouped, or whose group is no longer on the board, takes the group of most of the grouped tiles
  in its repo.
- Everything else is guessed by a model (`claude -p --model sonnet` with no tools; `guess_model`): tiles in repos
  with no grouped tile, meetings, documents, your email, to-dos, screen time, and orchestrator requests that started
  no agent. Each guess is cached per item until the layout changes. Set `"guess_model": null` to make no model calls;
  such time is then "Unsorted".

Guessed time is hatched; grouping the tile makes it solid. Time that no group fits, or not guessed yet, is
"Unsorted", in Ungrouped's grey.

**Running it.** `agent-board time` recomputes `time.json` in `~/.local/state/agent-board/` and prints what it
counted; it exits 1 when a source that is on failed and was left out, naming it. The Mac app reads the result with
the `time` action, and computes it then if there is none yet; **Refresh** on the sheet recomputes it. To keep it
fresh, install the optional `agent-board-time.timer` (every 15 minutes; see [Install](#install)). `time.json` and its
caches are private (0600); a damaged one is noted in `time.log` and rebuilt. `timeuse.DEFAULTS` lists the settings
with their defaults and meanings; `~/.config/agent-board/time.json` overrides them, for example:

```json
{"days": 30, "guess_model": "sonnet",
 "work_hours": {"weekdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}}
```

## Meeting alerts (optional)

Off by default. When on, the Mac app alerts you before each meeting, twice: a macOS notification 10 minutes before it
starts, then a full-screen overlay 1 minute before. The overlay covers every screen, above every window and the menu
bar, on every Space and over full-screen apps. It shows the title, the time and how long to go. **Join** opens the
meeting's video link; a meeting without one gets **Open calendar** instead. **Dismiss** or Esc closes the overlay.
Each link opens in the Chrome profile signed in to the account that owns the meeting's calendar (`Browser.swift`);
with no such profile it opens in the default browser.

Each alert comes once per meeting and start time, so a meeting moved to another time is alerted for again. The app
keeps the last meetings the desktop sent, so a short disconnect loses nothing, and checks them every 15 seconds and
when the Mac wakes. A meeting that started more than 2 minutes ago gets no alert. What has been alerted is kept for a
day in the app's defaults (`meetingAlertsFired`).

The desktop lists the meetings in the snapshot (`agentboard/meetings.py`). It reads each calendar's events from now
to 24 hours ahead and keeps those with a start time, not cancelled and not declined by you: default events and those
Gmail adds (flights, bookings), not working location, out of office, focus time or birthdays. A meeting on two
calendars is listed once. A meeting with no title (one on a calendar shared as free/busy only) is called after its
calendar's label ("Work meeting"). A calendar that cannot be read keeps its last meetings and is listed in the top
bar's problems.

**To turn it on**, set `"meeting_alerts": true` in the board config and give the board a Google login that may
read your calendar: `google_token` (see [Configuration](#configuration)), an "authorized_user" OAuth JSON
(`client_id`, `client_secret`, `refresh_token`) with the `calendar.readonly` scope. Your primary calendar is then
read. A `google_token` alone, for the Time page, alerts nothing. To read others, write
`~/.config/agent-board/meetings.json` (read at every refresh, so a change needs no restart):

```json
{"calendars": [{"id": "primary", "label": "Personal"},
               {"id": "you@work.example", "label": "Work", "account": "you@work.example"}],
 "lookahead_hours": 24, "notify_minutes": 10, "fullscreen_minutes": 1, "refresh_minutes": 5}
```

A person's main calendar has their address as its id; another calendar must be shared with the login. A calendar's
`account` is the Google account its meetings open in; without one, the login's own.

## Voice page: the front desk (optional)

You can talk to your agents by voice from a page, `/voice` on the [phone app's](#mail-on-the-phone-agent-mail) server:
Google's Gemini Live listens and answers in your browser, and asks the desktop what it needs through three read-only
tools. The page is made for Safari and Chrome on the Mac and for Safari on a phone; it is tested in Chrome. It is off
until you set it up (below): it needs the phone app's server running, a Gemini API key of your own, and the device
listed for it.

- **Talk** asks the desktop for a token for one conversation, opens Gemini Live's socket with it and streams the
  microphone there. Gemini's voice plays as it comes, and both sides are written down as Gemini transcribes them.
  **Stop** ends it (it stays in a bar at the bottom of the window while a conversation goes); Google closes a
  conversation after about 10 minutes, and Talk starts another.
- **Transcripts.** Each conversation's transcript is saved on the desktop, a turn at a time, in
  `~/.local/state/agent-board/voice-desk/transcripts/<YYYYMMDD-HHMMSS-xxxxxx>.txt` (one file per conversation, private
  to you, named by its start's time on your device; one line per entry: `14:15:02 Alex: ...`, with your first name
  from the mailroom config's `owner_name`, then `Front desk:`, `Tool:`, `Note:`). The page sends them with its own API
  call, `transcript` (signed in like the others; at most 50 entries a call, 200 calls in 10 minutes, 512 KB a
  conversation).
- **The voice.** Gemini's prebuilt voice `Puck` with no accent asked for, by default: `voice`, `accent` and
  `language_code` in `voice.json` change it (below).
- **The tools.** `list_agents` names the live agents; `what_am_i_looking_at` says what is in front of you on your Mac
  and which agent is behind it (the window before the voice page's own, when that is in front); `ask_agent(agent,
  question)` asks an agent through a READ-ONLY copy of its conversation, without interrupting it. While a copy is
  answering (5 seconds to 2 minutes; longer when a new copy must be made first) the page says "Asking loader-fix…"
  and Gemini says a short filler line. Gemini is told to keep answers short, to look at the screen when you say
  "this", to ask the agent that owns the work, and never to invent an agent's answer.

```
agent-board agents [--json]                        list_agents: the live Claude and Codex agents
agent-board ask AGENT QUESTION... [--stdin] [--json] [--model M] [--effort E] [--timeout S]
                                                   ask_agent: answered by a READ-ONLY copy of the agent's conversation
agent-board looking [--json]                       what_am_i_looking_at: the Mac's front window (ActivityWatch) and
                                                   the agent behind it
```

These commands do from a shell what the tools do (`agentboard/voicedesk.py`), and work without the page or a Gemini
key: nothing they say goes to Google. A Claude agent's copy is `claude -p --resume ID --fork-session` in the agent's
folder, under its login, on its model, at low effort. Only Read, Grep and Glob exist in it, and they read only in the
agent's folder, never a secret-looking file there (`.env`, keys, `*secret*`, `*credential*`, tokens): a read anywhere
else is refused, so a question cannot have a copy read the desktop's keys for an answer that goes to Google. An agent
running in the home folder or above it, or in a folder that holds another live agent's, is not asked. The copy's init
event, before any event but its session title, lists its tools, MCP servers and permission mode, and a copy with
anything more is stopped there, before it can act, whatever a newer Claude Code does with the flags. It runs no hooks,
refuses cross-session messages, loads no MCP server and is named `voice-copy-of-<agent>`, so mail, agent-send and
group posts for the agent never reach it. The original's transcript is never written. A copy is kept (its transcript
beside the original's) that was asked nothing but to reply OK, and each question within 50 minutes forks it, so the
conversation is read from the prompt cache and no question stays in the copy for the next to meet; a new copy is made
once the agent has moved on. Each ask is logged in `~/.local/state/agent-board/voice-desk/asks.jsonl`; `copies.json`
there lists the kept copies. Why each flag is there: the docstring of `agentboard/voicedesk.py`.

An agent on a per-token login (a model through OpenRouter; [A per-token
login](#a-per-token-login-and-its-spend-meter-optional)) is asked too, on the login's one model, with the login's
settings under the copy's own; the docstring of `agentboard/voicedesk.py` says how.

When the agent's login is at its usage limit (the copy's reply is "You've hit your weekly limit · ..."), the question
goes to the mailroom's next Claude login, in the order its own headless runs fall through (`headless.logins`: each
set-up login in `usage.order`, never a manual one): the agent's transcript is copied there first, as the mailroom
copies one for a resume on another login (`claude_session.copy_transcript`), and the copy is made and kept there.
`asks.jsonl` says the login that answered and those at their limit. An agent on a manual login (Gemini, billed per
token) is asked on its own login alone.

A Codex agent's copy is `codex exec fork --ephemeral` in the agent's folder, under its `CODEX_HOME`, on its model at
low effort. It has no tool that can act or read: it loads none of `config.toml` (`--ignore-user-config`: no MCP
servers, plugins, hooks or the folder's own `.codex` settings), has the shell, apps, plugins, image tools, goals,
sub-agents, web search and Codex's start-up login shell turned off, and runs on a model catalog of the voice desk's
(`codex-models/` in the same folder) without code mode, apply_patch or the shell; so it answers from the conversation
alone. Everything it sends goes to a gate on 127.0.0.1 (it connects to nothing else), which turns away all but its
model requests and sends one on only when the copy offers no tool but `request_user_input` (refused outside Plan mode)
and any tool result in it is Codex's refusal, so nothing a tool gave could reach the model; else the copy is stopped.
After a compaction Codex re-sends the tool list the agent's conversation recorded, which goes as recorded: the copy
refuses those tools. A copy writes no rollout, thread, lock or log entry, so neither the board nor agent-send sees it,
and runs no hooks. There is no kept copy: each question forks the live conversation, often read from the cache an
earlier copy left. How each of these was checked (on Codex 0.171.0): the same docstring.

`looking` reads the Mac's ActivityWatch over ssh (`mac/voice-desk/aw_front.py`, piped there; nothing is installed on the
Mac but ActivityWatch itself, with its window watcher and, for browser tabs, its web watcher). The Mac is the ssh alias
the kit's `MAC_HOST` set (the mailroom config's `remote_hosts.mac`; `AGENT_BOARD_MAC` or `VOICE_DESK_MAC` override it);
with no Mac configured, `looking` says so and reaches for nothing. It maps a mail tag `[#xxxxxxxx]` in the title or
address (Gmail) to the session that owns it in the mailroom, a subject alone (a Mail-tab window) to the session that
sent it, a GitHub pull request to the live session with its head branch checked out, the board's own window to the agent
it shows, a kitty window to the desktop tmux session last typed in, and else a live agent's name in the title; anything
else is "no agent found". The board's main window carries what it shows in its (hidden) title, `Agent Board — NAME
[session ID]` for the selected tile and `Agent Board — Mail: SUBJECT [session ID]` for the Mail tab's open thread (the
agent that joined it last), so `looking` names that agent. A kitty window attaches to the desktop's tmux (Open in
Kitty), so for one `looking` asks tmux (`tmux list-clients`, read-only) which client was typed in last and names the
live agent in that pane, or alone in that tmux session. Gmail's inbox, with no thread open, has no agent behind it.
Windows titled `Agent Front Desk…` (this page, and the Mac's [ask box](#the-ask-box-on-the-mac)) are passed over.

**How it is protected** (`agentboard/voiceweb.py` says how each check is made):

- **Its own devices.** `/voice` answers only the devices in `voice_devices` in `~/.config/agent-board/mailweb.json`
  (your Mac), with the phone app's other checks (on your tailnet, nothing open to the public). With `voice_devices`
  empty, the default, it answers no device at all. A device listed there alone is refused all of the Mail page: its
  page, its mail calls, its step-ups, the orchestrator and the keyring unlock. The phone, in `devices`, reaches
  `/voice` only when it is listed in `voice_devices` too.
- **The Mail page's passkeys and sessions.** Every call but the page's status and signing in needs a passkey session,
  from the same store and cookie as the Mail page's; a session holds only on the device that signed in, so the Mac's
  opens nothing of the Mail page. The voice page signs in, and enrols a device's passkey, through the Mail page's own
  handlers at its own address, from the one-time link `agent-board voice-enrol` prints (on the record as the Mac's
  enrolment codes are). You are told of each enrolment as the phone's are (a `phone-enrolment` security alert, in the
  Mail page's words, "a passkey for the agent-mail page"). The browser's passkey calls are one file for both pages,
  `web/passkeys.js`.
- **Its own policy.** Every answer under `/voice` has its own Content-Security-Policy: scripts, styles and calls from
  the page itself, plus the one Gemini Live socket (`wss://generativelanguage.googleapis.com/ws/...
  BidiGenerateContentConstrained`) and nothing else; and a Permissions-Policy that allows the microphone. The Mail
  page keeps its own policy and refuses the microphone (a browser grants it to the whole origin).
- **The Gemini key stays on the desktop.** For each conversation the desktop runs `agentboard/gemini.py` on its own,
  on the system's Python in isolated mode. It reads the key from where `voice.json` says (below): by default the file
  `~/.config/agent-board/gemini-key`, which it refuses unless it is yours, a regular file (not a link) and readable by
  you alone (mode 600); or an environment variable you name. It clears the rest of its environment, sends the key in
  one request's header to Google's `auth_tokens` endpoint, and prints the token. The key is never in an argument, a
  log or the repo, and never reaches the browser; the token goes to the page and is logged nowhere. A token starts one
  session (`uses: 1`) within 30 seconds, lasts 10 minutes, and carries the whole session's setup, so the model, voice,
  instruction and tools are the desktop's whatever the browser sends.
- **Calls checked and bounded.** A tool call names one of the three tools with exactly its arguments: the agent must be
  a live agent's name, and a question is at most 1000 characters; a call is at most 16 KiB. At most one question is
  asked at a time, 10 in 10 minutes and 60 a day (each copy reads the agent's whole conversation); tokens, 1 at a time,
  6 in 10 minutes and 40 a day; the other tools, and the transcript's saving, have limits of their own. A call refused
  for its arguments counts toward no limit.
- **Groups kept from Google.** The agents the board places in any of `exclude_groups` (sub-groups included: the layout
  the Mac sends and each tile's latest group) are never listed or asked by voice ("that's an agent kept from voice;
  it isn't available by voice"), so their work never goes to Google: use it for work under an employer's or a client's
  rules, for instance. When one is behind the window, what_am_i_looking_at says only that it is such an agent's work,
  with neither the agent's name nor the window's title; so too when the board's window shows one, live or not. While a
  group is excluded and the Mac has not yet sent the board's layout, no agent is offered at all. None is excluded by
  default. The desktop commands (`agent-board ask`, `looking`) and the Mac's ask box are not limited so: nothing they
  say goes to Google.
- **Read-only, and agents' words are data.** Nothing that acts as you (a reply, a send, the orchestrator, an unlock)
  is offered. What agents answer and what windows are titled goes to Gemini bounded and marked as data, not
  instructions, and Gemini is told never to act on it.
- **What goes to Google.** What you say, your first name (from the mailroom config, in Gemini's instruction), the
  names, states and folders of your live agents (but excluded ones), your Mac's front window title, and agents'
  answers. The socket's handshake also gives Google the page's address (the desktop's tailnet name, which is public
  already in the certificate logs).

**Settings**: `agentboard/voice.json` holds the defaults, overridden key by key by `~/.config/agent-board/voice.json`
(`AGENT_BOARD_VOICE_CONFIG` names another file). A key it does not know is an error, never ignored.

| key | default | meaning |
|---|---|---|
| `model` | `gemini-3.8-live` | the Gemini Live model. Google renames Live models often: change it here, and the page's status shows the model in use |
| `api_version` | `v1beta` | the Live API's version; ephemeral tokens work with v1beta only |
| `voice` | `Puck` | Gemini's prebuilt voice (Puck is Gemini's default; Kore, Charon, Aoede and others) |
| `accent` | `""` | a language and accent Gemini is told to speak in, such as `"American English"`; `""` or `null` for none. Native-audio Live models pick their language themselves and take no language code, so this instruction is what sets it |
| `language_code` | `""` | speechConfig's languageCode (BCP-47, such as `en-US`), sent only when set: for a half-cascade Live model, which takes one (a native-audio model may refuse it) |
| `new_session_seconds` | `30` | how soon after the mint the conversation must start (10 to 120) |
| `conversation_minutes` | `10` | how long a token's conversation lasts (1 to 30; Google closes a connection after about 10) |
| `question_chars` | `1000` | the longest question Gemini may put to an agent |
| `answer_chars` | `2000` | how much of an agent's answer goes to Gemini |
| `exclude_groups` | `[]` | board groups whose agents are never offered by voice (sub-groups included) |
| `excluded_as` | `"an agent kept from voice"` | what such an agent is called when one is refused or behind the screen |
| `gemini_key_file` | `"~/.config/agent-board/gemini-key"` | the file holding the Gemini API key, mode 600, used when `gemini_key_env` is null |
| `gemini_key_env` | `null` | the name of an environment variable that holds the key instead: in `agent-board-mail-web.service`'s environment (a systemd drop-in with `EnvironmentFile=` a private file), or put there by `gemini_key_command` |
| `gemini_key_command` | `null` | a command, as a list of its arguments, that runs the mint with the key in its environment, such as a secrets manager's runner; the key is then read from `gemini_key_env`, or `GEMINI_API_KEY` when that is null |
| `limits` | see the file | for `start` (a token), `transcript` and each tool: `in_flight`, `per_window`, `window_seconds`, `per_day`. An override of `limits` gives all four fields of each kind it names; the kinds it leaves out keep their defaults |

A bad setting is said on the page, which then reaches no socket and makes no call. `mailweb.json`'s `voice_devices`
(default `[]`) lists the devices the page answers.

**Setting it up**, on the desktop, with the phone app's server already running ([Mail on the
phone](#mail-on-the-phone-agent-mail)):

1. A Gemini API key from Google AI Studio (aistudio.google.com, "Get API key"), in its file, readable by you alone:

   ```bash
   install -m 600 /dev/null ~/.config/agent-board/gemini-key
   nano ~/.config/agent-board/gemini-key        # paste the key on one line; never in a command line
   ```

   Or keep it in a secrets manager and set `gemini_key_env` (and `gemini_key_command`) in
   `~/.config/agent-board/voice.json` instead.
2. Your Mac's Tailscale node id (`tailscale status --json`: the Mac's peer `ID`) in `voice_devices` (not `devices`) in
   `~/.config/agent-board/mailweb.json`, then `systemctl --user restart agent-board-mail-web.service`.
3. For `what_am_i_looking_at`: ActivityWatch running on the Mac, and `MAC_HOST` set in kit.env (the desktop must reach
   the Mac by `ssh <MAC_HOST>` without a password).
4. `agent-board voice-enrol` on the desktop; open the link it prints on the Mac within 10 minutes and press **Enrol
   this device**, which makes its passkey.
5. Open `https://<the desktop's tailnet name>:8466/voice`, **Sign in with your passkey** when asked, and tap Talk.

To turn it off again, empty `voice_devices` and restart the unit; the key file can then go.

### The ask box on the Mac

The ask box is optional and off until you turn it on (on the Mac: the menu bar item's **Ask box**, or `defaults write
org.agentkit.agentboard askBox -bool true`). Its hot key, ⌃⌥A, opens it anywhere on the Mac to talk to [the
desk](#the-desk-optional), or to ask one agent, by voice or by typing, with nothing going to Google. Before the box
appears, the board asks the desktop who is behind the window in front (the `voice_looking` action, which does what
`agent-board looking` does), and the box says what it found. It opens on the desk ("Ask the desk"), which is told that
window and its agent with each question, so "what is this one doing?" works; **Change** picks one agent instead, whose
read-only copy then answers, or the desk again.

- **Speak or type.** The box brings the board forward and takes the keyboard, so whatever dictation app you use (one
  that types or pastes into the focused field) types into it: one change of 10 or more characters counts as a
  dictation, and the box asks about 0.8 s later, unless you edit the text first. ↩ asks at once, and ⌥↩ is a new line.
- **Talking over it interrupts it.** The voice stops the moment an app starts to record from a microphone (your
  dictation app, as you start to speak), and when a new question is asked; nothing more of the answers before is
  spoken, though they still come and are shown. **Stop** silences the voice at once and drops what was still to be
  spoken and the questions not yet answered; Esc does that too, closes the box, and gives the keyboard back.
- **What goes where.** The question goes to the desktop over the board's own link (the `agent-board stream` the Mac
  keeps open). To the desk (the `desk_say` action, `{"text", "looking"?}`) it goes with what `voice_looking` said of the
  window, and no history: the desk remembers the conversation itself, until **New conversation** (`desk_new`). While
  the desk works, the box says what it is doing ("Asking loader-fix…", one status line per tool:
  `actions.DESK_STATUS`), and under its answer what it sent an agent for you ("Sent to loader-fix as message k3f9: …"),
  or why a send failed. To one agent (the `voice_ask` action, `{"agent", "question", "history"?}`), a read-only copy of
  its conversation answers, as `agent-board ask` does (the voice page's section has how a copy is held): on the agent's
  own CLI, Claude Code or Codex, under its own login. A follow-up carries the box's last 3 questions and answers to
  that agent (at most 3 pairs, each text at most 16,000 characters), so "and the second one?" works.
- **Heard as it is written.** The desktop's voice (below) speaks the desk's answer, or a Claude copy's, a sentence at
  a time while it is still being written (what the desk says before a tool, such as "Asking loader-fix.", at once).
  Each sentence's audio comes to the Mac over the board's link as a part of the answer: a stream message
  `{"type": "act_part", "rid", "part": {"seq", "text", "audio"}}` (the WAV in base64), or `{"seq", "text", "error"}`
  when the desktop's voice could not speak it, and the desk's tool lines as `{"status"}` parts; the answer itself comes
  last, as every action's does. Over an `ssh desktop agent-board act` of its own, the parts are listed in the answer's
  `parts` instead (`agent-board act --parts` prints each as a line `{"act_part": {...}}` first). A Codex copy's answer
  comes whole (`codex exec --json` streams none of it), then is spoken the same way. A sentence the desktop's voice
  could not speak comes with why, and the Mac reads it with its own voice, saying so.
- **Agents the voice page leaves out may be asked here.** The voice page refuses the agents of its `exclude_groups` so
  that their work never reaches Google; here the question and the answer go nowhere but the desktop, the model the
  agent already works on, and the Mac. Neither web page (the phone's Mail page, the voice page) offers these actions.
- **How long.** A question to an agent takes 5 seconds to 2 minutes (longer when a new copy must be made first); the
  desk's own takes as long as its turn ([the desk](#the-desk-optional)). The board's link runs at most 2 of them at
  once, apart from its other actions (4 at once), so a slow one never holds those up. Neither Stop nor an interruption
  stops a copy, or a desk's turn, already answering on the desktop: it finishes unheard, and a question asked meanwhile
  waits.

The box's window is titled "Agent Front Desk — Ask the desk" (or "— Ask NAME"), so `looking` passes over it, as it does
over the voice page.

**The desktop's voice** is Kokoro (hexgrad/Kokoro-82M, Apache-2.0) in a daemon of its own, `agent-speech serve` (the
user unit `agent-speech.service`), on a Unix socket in the board's state folder (mode 0600, your uid only): text in,
audio out, a sentence at a time, with no TCP port, so the text never leaves the desktop and the Mac
(`agentboard/speech.py` has the socket's protocol). Kokoro is PyTorch, so it runs on the python of a venv of its own,
`~/.local/share/agent-kit/speech-venv` (or the python `AGENT_SPEECH_PYTHON` names); the rest of the board stays the
standard library only. Words Kokoro's lexicon lacks (agent names, file names) go to espeak-ng, which the
`espeakng-loader` wheel carries: nothing is installed outside the venv. Without the daemon, the ask box still works:
each sentence comes with why it was not spoken, and the Mac reads it with its own voice.

Settings: `agentboard/speech.json`, overridden key by key by `~/.config/agent-board/speech.json`
(`AGENT_BOARD_SPEECH_CONFIG` names another file); restart the unit after a change.

| key | default | meaning |
|---|---|---|
| `voice` | `"af_heart"` | Kokoro's English voice: first letter the accent (`a` American, `b` British), second the voice (`f` female, `m` male), such as `am_michael`, `bf_emma`, `bm_lewis` |
| `speed` | `1.0` | how fast it speaks, 0.5 to 2.0 |
| `device` | `"auto"` | where Kokoro runs: `auto` (the GPU when PyTorch sees one, else the CPU), `cuda`, `cuda:N` or `cpu` |
| `socket` | `~/.local/state/agent-board/voice-desk/speech.sock` | the daemon's Unix socket |

On a GPU a sentence takes about 0.1 s and the daemon holds about 1 GB of its memory; on a desktop CPU it takes 1 to 4 s
(about 25 ms a character), so the first sentence is heard seconds later and the answer's end waits for its last
sentence. `speech.log` in `~/.local/state/agent-board/voice-desk/` says how long each answer and sentence took, and
each time the daemon could not be reached.

To set it up (the installer does these steps when `WITH_SPEECH="yes"` in `kit.env`), from the kit folder, once:

```bash
python3 -m venv ~/.local/share/agent-kit/speech-venv
~/.local/share/agent-kit/speech-venv/bin/pip install torch      # the default Linux wheel has CUDA; CPU only:
                                                                # add --index-url https://download.pytorch.org/whl/cpu
~/.local/share/agent-kit/speech-venv/bin/pip install --no-deps -r board/requirements-speech.txt
agent-speech fetch                       # Kokoro's model and voice, about 330 MB, from Hugging Face; once
systemctl --user enable --now agent-speech.service
agent-speech say "The desktop's voice is ready." -o ready.wav      # each sentence's timing, and the audio
```

`agent-speech serve` and `fetch`, run on a python that cannot import Kokoro and PyTorch (the kit's wrapper in
`~/.local/bin` runs the kit's venv), start again on the speech venv's python; with no speech venv they say how to make
one and exit 2, which the unit does not restart. The unit loads everything from Hugging Face's cache
(`HF_HUB_OFFLINE=1`): the daemon never goes online. A venv made with `--system-site-packages` reuses a PyTorch you have
installed already; then add `--ignore-installed` to the last pip, so each pin still lands in the venv.

## The desk (optional)

The desk is one always-running agent you talk to: a single long-lived `claude -p` process (stream-json; by default
`claude-sonnet-5-5` at low effort, chosen for speed: a spoken answer should start within seconds), so nothing starts
up per question and it remembers the conversation. `agent-board desk serve` holds it and answers on a Unix socket on
the desktop (mode 0600, your uid only, in a folder of its own made 0700; nothing on any network). It is optional and
off until its unit runs (`WITH_DESK="yes"` in `kit.env`). The [ask box](#the-ask-box-on-the-mac) talks to it; so can a
shell:

```
agent-board desk say TEXT... [--looking] [--json]   one turn: the reply as it streams, then its timings, tools and
                                                    sends (--looking: the desk reads the Mac's front window first)
agent-board desk new                                a fresh conversation
```

A request is one JSON line, `{"text", "looking"?}` (`looking`: what the box's `voice_looking` gave, or `true`), or
`{"op": "new"}`, and its answer JSON lines: `{"type": "waiting"}` while it waits behind another turn, text deltas
(`{"type": "delta", "text"}`), a line per tool call (`{"type": "tool", "name", "input"}`, so a caller can say "Asking
loader-fix…"), then a final line, `{"type": "final", "ok", "text", "error"?, "screen_error"?, "timings", "tools",
"refused", "failed", "sends", "context_tokens"}`: the whole reply, its timings, the tools used, those refused or
failed, anything sent, and whether your screen could not be read. One turn runs at a time; another waits.
`turns.jsonl` beside the socket keeps each turn, `mcp.log` there is the tools' own log, and the unit's journal has a
line per turn and a warning for each restart and each refused or failed tool.

- **Its tools**, an MCP server of its own (`agent-board desk mcp`) and no other: `agents` (each live agent's state as
  the board shows it, group, folder and last status note), `recent(agent, n)` (its latest messages, its own and those it
  was sent, read at once from its transcript or Codex rollout, without tool output), `ask(agent, question)` (a read-only
  copy, as the [ask box](#the-ask-box-on-the-mac) asks), `looking`, `send(agent, text)`, and `mail_list` and
  `mail_show` (through `agent-mail inbox`, `sent` and `show`; it never sends mail). Besides these, only Read, Grep and
  Glob over `read_dirs` (your repos folder by default; secret-looking files refused, as for the copies), and WebSearch.
  No WebFetch: what the desk reads could leave in a URL to any host. Everything the desk reads goes to Anthropic, under
  its login; none of it to Google.
- **Its login.** The mailroom's Claude logins, in the order its own headless runs (the orchestrator, the pre-send
  review) fall through them (`headless.logins`: each set-up login in `usage.order`, never a manual one such as a
  per-token login). When a primer or a turn gets its login's usage-limit reply ("You've hit your weekly limit · ..."),
  the desk starts again on the next login (a new conversation) and asks that turn again there, so the answer still
  comes; the limit's reply is never streamed or given as the answer. A turn that finds no login with room left fails,
  its error saying each login's limit. A turn that had already used a tool or said something is not asked again (it
  may have sent a message): it fails, saying the desk has moved. The log says which login it moved to and why, and
  `turns.jsonl` each turn's login. It goes back to an earlier login only when it starts afresh for another reason (New
  conversation, a long context, a process that died), so it does not flap. The ask box's questions to an agent's
  copy fall through the logins the same way: the agent's transcript is copied into the next login's folder.
- **How it is held.** `--restricted` (none of the login's settings files, so none of its plugins, hooks, allow rules
  or CLAUDE.md), permission mode dontAsk with nobody to ask, hooks off, cross-session messages refused, no session
  persisted, and named `desk` (no tile, not offered to ask). Each turn's init event must list exactly these tools, its
  one MCP server and dontAsk before the model answers; a process holding more is stopped there and the daemon exits 3,
  which the unit does not restart. A process that dies, or whose context passes `context_tokens`, starts again with a
  new conversation. The token its `send` tool uses to reach the daemon is in a private file beside the socket (0600),
  which Claude may not read, never on a command line.
- **The send rule.** The desk reads untrusted text (transcripts, mail, web pages), so its `send` is checked by the
  daemon, not left to the model: only in a turn that came from your own request, whose words ask for a send (tell, ask,
  send, message, remind, ...), or are a short yes ("Yes please") to the desk's answer before when that ended by asking
  whether to send ("Shall I tell loader-fix?"), to an agent your words name (in this request or your last two), the
  offer you said yes to names, or the one behind your screen, and once per agent per turn. The message goes through the
  board's own send path, with your words to the desk quoted under it (and the offer, after a yes), so the agent sees
  what you actually said and agreed to; it is on the record in `actions.jsonl` and `sent.jsonl` (`"via": "desk"`) and
  comes back in the turn's final line for the caller to show. No confirmation is asked.
- **Its prompt names no one.** `agentboard/desk-prompt.md` has `{owner}` and `{read_dirs}` in it; the daemon fills in
  your first name (the mailroom config's `owner_name`) and the folders as it starts Claude, into `prompt.md` beside the
  socket (0600).

**Settings**: `agentboard/desk.json`, overridden key by key by `~/.config/agent-board/desk.json`
(`AGENT_BOARD_DESK_CONFIG` names another file); restart the unit after a change. An unknown key or a bad value stops
the daemon with why.

| key | default | meaning |
|---|---|---|
| `model` | `"claude-sonnet-5-5"` | the desk's model |
| `effort` | `"low"` | its effort: low, medium, high, xhigh or max |
| `claude_config_dir` | `null` | the login to try first: `null` follows the mailroom's order of logins; a login's folder such as `"~/.claude-work"` must be one of those logins, and a limit still moves the desk on |
| `socket` | `~/.local/state/agent-board/desk/desk.sock` | its Unix socket (at most 107 bytes) |
| `context_tokens` | `100000` | past this much context, it starts a new conversation between turns |
| `turn_seconds` | `600` | a turn that takes longer is stopped |
| `system_prompt` | `"desk-prompt.md"` | its prompt template, relative to `agentboard/` |
| `read_dirs` | `null` | the folders Read, Grep and Glob may read: `null` is your repos folder (the mailroom config's `repos_root`), or a list of at most 10 absolute folders |

**The unit**: the installer installs `agent-board-desk.service` from `board/systemd/agent-board-desk.service.in` when
`WITH_DESK="yes"`; by hand, render its `{{PYTHON}}` and `{{KIT_DIR}}`, put it in `~/.config/systemd/user/`, then
`systemctl --user daemon-reload` and `systemctl --user enable --now agent-board-desk.service`. Its log is the unit's
journal (`journalctl --user -u agent-board-desk`).

## Unlock the desktop (optional)

For a Linux desktop that logs in by itself (GDM's automatic login): its keyrings (gnome-keyring) then stay locked
after a restart, and a Chromium-based browser there (Chrome) waits at its "Authentication required" dialog, so agents
cannot use the desktop browser. Chrome's keyring is the one the Secret Service's `default` alias names, which need not
be the login keyring (it is often "Default keyring"). Off unless you turn it on:

```bash
mkdir -p ~/.config/agent-board
# add "keyring_unlock": true to ~/.config/agent-board/config.json (or set WITH_KEYRING_UNLOCK="yes" in kit.env)
```

With it on, the board reads, every minute, which keyring the alias names and whether it and the login keyring are
locked (the snapshot's `keyring`). While Chrome's is locked, the top bar shows a red **Desktop browser locked** mark (a
grey **Desktop keyring unknown** when its state can't be read, or the alias names none), and the Mac posts one
notification a boot. Click the mark, type the keyring's password and press **Unlock**: it is tried on each of the two
that is locked, Chrome's first. The app runs the unlock command below over ssh with the password on that ssh process's
stdin: never in its arguments, and never over the board's stream, whose actions are recorded. The field is emptied as
it is sent; the password is not saved, logged or put on the pasteboard. After an unlock the app asks the stream for the
keyring's state at once (`{"cmd": "refresh_keyring"}`).

On the phone, Agent Mail shows the same mark over the list and unlocks the same way, after the fingerprint each time
([Mail on the phone](#mail-on-the-phone-agent-mail)): the page's server runs the unlock command below with the password
on its stdin. With the unlock off, the phone shows no mark and its server refuses an unlock outright.

On the desktop, the password goes to the running daemon's `UnlockWithMasterPassword`, encrypted in a Diffie-Hellman
session on the session bus, in calls that never start a daemon, and only once the bus name's owner is found to be
running `/usr/bin/gnome-keyring-daemon` (the `daemon` setting in `~/.config/agent-board/keyring.json`). That method
only unlocks: a missing keyring is an error, never created, and a wrong password changes nothing.
`agentboard/keyring.py`'s docstring says why `gnome-keyring-daemon --unlock` and the control socket are not used, and
which copies of the password Python cannot zero. Anything running as you can reach the daemon itself, past the limit
below, pose as it once stopped, or change this code; the unlock keeps the password out of argv, the environment, files,
logs, core files and the bus in plain text, not out of reach of a hostile process of yours.

Agents check, or wait, with:

```bash
agent-board keyring-status [--json]                 # Chrome's keyring: exit 0 unlocked, 1 locked, 3 unknown (or off)
agent-board keyring-status --wait [--timeout 600]   # checks every 10 s until unlocked; at the timeout exits 1 or 3
```

`--json` prints `{"state": "locked" | "unlocked" | "unknown", "locked": true | false | null, "error": why it is
unknown, or null, "boot_id": this boot's id, "collections": {"default": ..., "login": ...}}`: the state is Chrome's
keyring's, and each collection is `{"path", "label", "locked", "error"}`, `path` null when the alias names none. It
reads (ReadAlias and the properties, with busctl) and writes and logs nothing. With the unlock off it prints why and
exits 3.

The unlock command is the interface for any caller; the Mac app and the phone page's server call it:

- **Command**: `/usr/bin/python3 -I <kit>/board/bin/agent-board keyring-unlock`, or, from a shell, `agent-keyring-unlock`
  (the kit's wrapper in `~/.local/bin` for `board/bin/agent-keyring-unlock`, a shell script that runs exactly that). It
  refuses (result `error`) to run on another Python or without `-I`, so nothing from a virtualenv, a user site or
  PYTHONPATH loads beside the password, and it makes itself undumpable before it reads. The kit's wrapper
  `~/.local/bin/agent-board` runs the kit venv's Python, which it refuses, so the Mac app reads which script that
  wrapper runs (or takes its `keyringScript` setting) and runs the system's Python on it over ssh. With the board
  config's `keyring_unlock` off it answers `error` before it reads anything.
- **Input**: the password on stdin only, as UTF-8: one line of 1 to 1024 bytes, which may end in one newline, taken
  off. Input that is longer, holds another newline or a NUL, ends in a carriage return (a client ending its lines
  CRLF), is empty, is a terminal, or does not end within 30 s is refused with the result `error`, and the refusal never
  repeats the input or its length.
- **Environment**: it needs only `HOME`, the session bus's address (`DBUS_SESSION_BUS_ADDRESS` or `XDG_RUNTIME_DIR`)
  and `PATH` (`keyring.ENVIRONMENT`); the phone page's server gives it those alone.
- **Output**: one JSON line, `{"result": "unlocked" | "partial" | "wrong" | "rate_limited" | "error", "locked": true
  | false | null, "collections": ...}`, with `"tried"` (`{"default" | "login": "unlocked" | "wrong" | "error"}`, each
  keyring the password went to; `{}` for none) unless the keyrings could not be read or the attempt was held back,
  `"error"` (why) when there is one, and `"retry_after"` (seconds) for `rate_limited`. `locked` is Chrome's keyring's
  state and `collections` both keyrings', read afterwards. `unlocked`: everything tried opened and Chrome's keyring
  reads as unlocked; with nothing locked, the password is not sent. `partial`: it opened one keyring, and the other
  refused it (they have different passwords) or could not be unlocked (`error` says why); `tried` says which. `wrong`:
  every keyring tried refused it.
- **Exit codes**: 0 unlocked, 1 wrong, 2 rate-limited, 3 error, 4 partial.
- **Limit**: at most 5 wrong passwords in 15 minutes, from every caller together; the next is refused, unsent, until
  the oldest of them is 15 minutes old. One password counts once, however many keyrings it is tried on, and a partial
  one counts too. A password that went out with no clear answer, or whose caller was stopped before the answer was
  recorded, counts as wrong.

Each attempt's outcome, and who asked (the ssh client's address, or the local parent process: the phone page's server,
which puts the outcome in its own journal and `actions.jsonl` too), goes to
`~/.local/state/agent-board/keyring/keyring.log`, and its time and result to `attempts.json` there; never the password
or its length. It needs the distribution's `python3-gi` and `python3-cryptography` for `/usr/bin/python3` (Ubuntu:
`sudo apt install python3-gi python3-cryptography`), and `busctl` (systemd's). The phone page's server runs on the kit
venv's Python in isolated mode, so the password passes through a process that loads the venv's own packages (which you
installed) but nothing from a user site or PYTHONPATH.

`~/.config/agent-board/keyring.json` (optional):

| Key | Default | Meaning |
|---|---|---|
| `daemon` | `/usr/bin/gnome-keyring-daemon` | the only program the password is sent to: the owner of `org.freedesktop.secrets` must be running it |

## Transcript archive (optional)

The archive keeps every transcript you have with your agents, Claude Code's and Codex's, from the desktop (and the
Mac, when one is configured), for good, in one folder you choose on the desktop, with a catalog of the sessions by
board group (`agentboard/transcripts.py`). It is off until you name that folder and turn on its timer.

**The archive has one copy, in the folder you name.** Nothing is copied anywhere else: if that disk fails, the archive
is lost. Put it on a disk with room to grow (transcripts add up to gigabytes), and back it up yourself if you need to.

**What is kept where.** Each source is mirrored under `<archive>/<machine>/<source>/`, with the source's own paths.
With no `machines` in the settings, the sources are:

| machine | source | from |
|---|---|---|
| this desktop (the mailroom config's `host_id`, else `desktop`) | `claude-projects`, `claude-work-projects`, … | every `~/.claude*/projects` (each Claude login), found afresh each run |
| this desktop | `codex-sessions`, `codex-archived` | `~/.codex/sessions`, `~/.codex/archived_sessions` |
| `mac`, only when a Mac is configured | `claude-projects`, `codex-sessions` | the Mac's `~/.claude/projects` and `~/.codex/sessions`, over rsync |

A Mac is configured when the mailroom config's `remote_hosts` has a `mac` entry (the kit installer writes it from
`MAC_HOST` in `kit.env`), or `AGENT_BOARD_MAC` names its ssh alias; with neither, nothing is reached over ssh.

A session's subagents, tool results and memory files come with it, as they sit in its source. Nothing in the archive is
ever deleted or written over. A transcript that has grown gets only its new end appended. One that was rewritten or
has shrunk is first moved, as it was, to `versions/<time of the run>/<machine>/<source>/…`. A transcript the source
has dropped stays in the archive. The Mac's folders come over rsync (never with `--delete`) into
`.incoming/<machine>/` and are archived from there the same way. When the Mac cannot be reached (ssh's connection
times out, or finds no route, its network unreachable or the host down: it is asleep or away), the run says so with
ssh's words and leaves the Mac for the next day; the Mac keeps its transcripts until then. When ssh is refused (the
connection, with Remote Login off, or the key or host key), that is a problem to fix, not sleep.

Claude Code deletes old sessions after `cleanupPeriodDays` (30 by default): set it high (for example `36500`) in
`~/.claude/settings.json` on each machine if the archive should hold every session, since a session deleted before
the next run is not archived.

**When.** `agent-board-transcripts.timer` runs `agent-board transcripts archive` once a day at 10:30, give or take
10 minutes: mid-morning, when a Mac is usually awake. It is persistent: a run missed while the desktop was off happens
at the next boot. The run is niced, with idle I/O priority, and refuses to start when the archive folder is missing
(the disk not mounted): the folder is never made by the archive, so it never fills another disk instead.

**The catalog** is `catalog.sqlite` in the archive (private to you), table `sessions`, one row per session:

- `id`, `cli` (claude or codex), `machine`, `login` (the newest copy's) and `logins` (every login holding a copy),
  `files` (its transcripts' paths in the archive, the newest copy of its own first, then its subagents');
- `cwd`, `project` (its repo), `name` (its `/rename` or agent-spawn name, else the mailroom's), `title` (the session's
  own summary when its transcript has one, else its first message, trimmed), `entrypoint` (`cli`, `sdk-cli`, `exec`…);
- `started`, `last_activity`, `size` (bytes of all its files), `updated` (epoch seconds);
- `group_name`, `subgroup`, `group_source` (`placed`, `guessed` or `unknown`), `group_basis` (`board`, `repo` or
  `model`) and `frozen_at`.

A session copied between logins (agent-failover) or archived by Codex is one row. A Claude subagent's transcripts are
its parent session's, and so is a Codex sub-thread's rollout. `sources.json` beside the catalog records each archived
source's machine, CLI, login and origin.

**How a session's group is decided.** Each run groups every session that is not frozen, as the board and the
[Time page](#time-where-your-time-went) group a tile:

1. **Placed**: the group and sub-group you put its tile in on the board (the layout the Mac sends, kept in
   `groups-history.json` after the tile closes), while that group is still on the board. Your placement always wins.
2. **Guessed from its repo**: the group of most of the placed sessions of the same repo. A Mac session's repo is the
   desktop folder at the same place under the home folder, else the folder under the repos root (`repos_root`) that
   the Mac's folder path ends with.
3. **Guessed by the model**: the Time page's own guess (`timeuse.assign` and `timeuse.guess`, with its model, `claude
   -p` on your plan), not a second classifier. Each guess is cached in `transcripts-guesses.json` until the layout
   changes. A run asks about at most `guess_per_run` sessions (240: two calls), the most recently active first; the
   rest wait for the next run. With no board groups yet, no model is asked.
4. **Unknown**: no group fits, or not asked yet. These sessions are in `Unsorted` in the tree.

**Frozen when it ends.** A session has ended when it is not running and has written nothing for `end_after_hours`
(48: the board shows an offline tile for 48 hours at most, so it is off the board by then). The group it has at that
moment is frozen in the catalog (`frozen_at`): your placement if you made one, otherwise the guess. Later runs do not
change it, even if the layout changes or you move another tile. The archive freezes the group itself, rather than
having each agent label its own transcript as it closes, because sessions often just stop, with no step at which an
agent could do so, and Codex has no hook for it. An unknown session is not frozen; it is asked about again when the
layout changes. A frozen session that runs again, or writes to its transcript after it was frozen (resumed), is
grouped afresh and frozen again when it next ends. On the first run, every session that has already ended is frozen
with the group it has then.

**By group.** `by-group/<group>/<sub-group or (none)>/<start date> <name or title> (<cli>).jsonl` is a tree of
relative symbolic links to each session's newest transcript; two sessions that would share a name there get their
ids. It is rebuilt from the catalog every run, so a regrouped session's link moves. Only links are removed there (and
folders left empty); a file of any other kind is left alone.

**Commands.**

```bash
agent-board transcripts archive [--json]       # what the timer runs: copy, catalog, tree; prints what it did
agent-board transcripts list --group Research --subgroup Evals --since 2026-11-01 --query "flaky test"
agent-board transcripts list --group Unsorted -n 20 --json
```

`list` prints each matching session, oldest first: its start, group and sub-group with how it was decided, CLI,
machine, name and title, then the full path of its newest transcript and how many more it has (other logins' copies,
subagents); `--json` lists them all. `--group` and `--subgroup` ignore case; `Unsorted` and `(none)` find sessions with
none. `--since` keeps sessions active on or after a day. `--query` needs every word in the name, title, folder,
project or id. To search what was said, grep the files `list` names, or the archive. For other questions, query the
catalog with `sqlite3` (times are epoch seconds: `datetime(started, 'unixepoch', 'localtime')`).

`archive` exits 2 when it cannot run at all (no archive folder named, the folder missing, or bad settings), and 1 when
it ran but something failed: a file not copied, an rsync that failed, ssh refused, or the mailroom's session names not
read (each session then keeps the name the catalog has). A Mac that cannot be reached is only a warning. Each run adds
a line to `~/.local/state/agent-board/transcripts.log`. Transcripts already parsed are cached in
`transcripts-cache.json` beside it, and each is read on from where the last run stopped.

**Settings.** `~/.config/agent-board/transcripts.json` (`AGENT_BOARD_TRANSCRIPTS_CONFIG` points elsewhere). Only
`archive` is required:

| key | default | meaning |
|---|---|---|
| `archive` | none (required) | the archive folder; it must already exist (it is never made); `~` is your home |
| `machines` | `null`: this desktop, and the Mac when one is configured (above) | the machines and their sources; a list replaces the defaults |
| `end_after_hours` | `48` | a session not running and quiet this long has ended, and its group is frozen |
| `guess_per_run` | `240` | sessions the model may be asked to group per run |
| `ssh_timeout` | `15` | seconds a machine reached over ssh has to answer before it counts as asleep |

A `machines` list names each machine and its sources, for example:

```json
{"archive": "/mnt/archive/transcripts",
 "machines": [
  {"name": "desktop", "sources": [
    {"cli": "claude", "glob": "~/.claude*/projects"},
    {"cli": "codex", "name": "codex-sessions", "path": "~/.codex/sessions"},
    {"cli": "codex", "name": "codex-archived", "path": "~/.codex/archived_sessions"}]},
  {"name": "mac", "ssh": "mac", "sources": [
    {"cli": "claude", "name": "claude-projects", "path": "~/.claude/projects"},
    {"cli": "codex", "name": "codex-sessions", "path": "~/.codex/sessions"}]}]}
```

A machine name is letters, digits, `.`, `_` and `-`, and not one of the archive's own names (`catalog.sqlite`,
`by-group`, `versions`, `.incoming`, `sources.json`). A `glob` (on this desktop only) gives one source per folder it
matches, named after its login folder (`~/.claude-work/projects` is `claude-work-projects`); a `path` needs a `name`.
A machine with `ssh` is reached by that host alias (non-interactive: its key must work without a prompt), and its
paths are relative to its home there; it needs `rsync` on both machines.

**Setting it up.**

1. Make the archive folder, on a disk with room: `mkdir -p /mnt/archive/transcripts && chmod 700
   /mnt/archive/transcripts` (any folder of yours).
2. Name it: `{"archive": "/mnt/archive/transcripts"}` in `~/.config/agent-board/transcripts.json`.
3. For the Mac's transcripts: Remote Login on the Mac, the desktop's ssh key accepted there, and `MAC_HOST` set in
   `kit.env` (or a `mac` machine in `machines`). Check with `ssh -o BatchMode=yes <mac alias> true`.
4. Try it once: `agent-board transcripts archive`, then `agent-board transcripts list -n 5`.
5. Turn on the daily run. The kit's installer does this when `kit.env` has `WITH_TRANSCRIPTS="yes"` (and writes the
   folder from `TRANSCRIPTS_DIR` into the settings); by hand, render
   `systemd/agent-board-transcripts.service.in` into `~/.config/systemd/user/agent-board-transcripts.service`, copy
   `systemd/agent-board-transcripts.timer` beside it, then `systemctl --user daemon-reload` and
   `systemctl --user enable --now agent-board-transcripts.timer`.

`systemctl --user start agent-board-transcripts.service` runs it at once, and `journalctl --user -u
agent-board-transcripts` shows what each run printed.

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

Mail goes to `owner_email` from the mailroom config through `agent-mail send --unrouted` (an ordinary notice, no
alert kind: without Gmail, or with your agent mail kept off Gmail, it is kept for the Mail tab); without
`owner_email` nothing is mailed (the log says so). State and the log: `~/.local/state/agent-board/restart/`.

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

Then, for each installed CLI, it runs `agent-mail fast-trial CLI --if-untested` (the mailroom's README, "Fast mode"):
the mailroom's fast mode presses an agent's effort keys only on a CLI version they were tried on, so a version not
tried yet gets them tried in a throwaway tmux session, and is added to fast mode's tested list when it passes; a
version tried already costs nothing. A trial runs even after a failed update (the version that runs may be untested),
and a failed trial undoes no update: the new version runs without fast mode until a trial passes.

```bash
agent-cli-update               # update both and try them now, one log line each
agent-cli-update log [N]       # the last N lines of ~/.local/state/agent-board/cli-update.log (default 20)
systemctl --user status agent-cli-update.service     # the last timer run
```

A CLI that is not installed is logged as skipped and gets no trial (Codex is optional). An installed CLI whose update
fails (a non-zero exit, a timeout, or no `--version` afterwards), or whose trial fails, makes the run, and so the
unit, fail; so does finding neither CLI. Updates and trials run non-interactively (stdin closed,
`CODEX_NON_INTERACTIVE=1`); a trial past 10 minutes is stopped. To stop automatic updates, disable the timer:
`systemctl --user disable --now agent-cli-update.timer`.

## Configuration

The board has no required settings of its own. It reads:

**Mailroom config** `~/.config/agent-mail/config.json` (written by the kit installer; `AGENT_MAIL_CONFIG_DIR`
overrides the directory):

| Key | Default if absent | Used for |
|---|---|---|
| `owner_name` | `the owner` | the name in every text an agent reads (`[relayed from Alex via agent-board]`), and in the blocked-note rule |
| `owner_email` | first `allowlist` entry, else none | where `agent-restart` and the phone app's enrolment alert mail; the phone app's tailnet login unless `mailweb.json` names `logins`; the Gmail link, read-check and Forward |
| `allowlist` | `[]` | which inbound and sent mail counts as the owner's; also tailnet logins the phone app answers, unless `mailweb.json` names `logins` |
| `account` | none | the agents' Gmail address (only for the optional read-check and Forward) |
| `repos_root` | `~/code` | the worktree scan root (`AGENT_BOARD_REPOS` overrides) |
| `orchestrator_dir` | `<repos_root>/orchestrator` | sessions there are the orchestrator's, not tiles |
| `host_id` | `desktop` | which mailroom sessions are hosted on this machine; the transcript archive's name for this machine |
| `remote_hosts.mac` | none | the Mac's ssh alias (the installer writes `MAC_HOST`), for the voice desk's `looking` and the transcript archive's Mac sources; none: nothing reaches for a Mac (`AGENT_BOARD_MAC` overrides) |
| `usage` | one login, `default` | Claude logins, `order`, `min_remaining` (the mailroom's agent-usage settings); a login's `manual`, `model`, `settings` and `weekly_budget_usd` make it a [per-token login](#a-per-token-login-and-its-spend-meter-optional) |
| `spawn_*` | as the mailroom's | model and effort for Resume when a session's own are unknown |

**Mailroom checkout**: the kit's sibling directory `../mailroom` (relative to `board/`), or `AGENT_MAILROOM_DIR`.
The board imports `agentmail.close`, `agentmail.failover`, `agentmail.memcap`, `agentmail.config`,
`agentmail.commands` (the keys lock), and, when the mailroom has them, `agentmail.accounts` and `agentmail.usage` (the
logins' options, the manual logins and the scrub a claude gets) from it, and loads `bin/agent-spawn` for its trust
helper. The Mail tab's Researcher runs `agent-mail mailbox researcher`. State is read from
`~/.local/state/agent-mail` (`AGENT_MAIL_STATE_DIR` overrides) and `~/.local/state/agent-status`
(`AGENT_STATUS_DIR`).

**Board config** `~/.config/agent-board/config.json` (optional; `AGENT_BOARD_CONFIG` overrides the path):

| Key | Default | Meaning |
|---|---|---|
| `gmail_credentials` | `null` (off) | path to your own Gmail's OAuth file: the read-check, the Mail tab's Forward, the Time page's "your own email" ([below](#optional-your-gmail-read-check-and-forward); `AGENT_BOARD_GMAIL_CREDS` overrides) |
| `google_token` | `null` (off) | path to an OAuth file for your Google account with Calendar read access (and Drive metadata read access for documents): the Time page's meetings and documents, and [meeting alerts](#meeting-alerts-optional) when they are on (`AGENT_BOARD_GOOGLE_TOKEN` overrides) |
| `meeting_alerts` | `false` | `true` turns on [meeting alerts](#meeting-alerts-optional), read with `google_token` (`AGENT_BOARD_MEETING_ALERTS=yes` overrides) |
| `tasks_repo` | the to-do component's `repo` (`~/.config/todo-desk/config.json`), else none | `OWNER/REPO` of your to-do list, for the Time page's to-dos |
| `keyring_unlock` | `false` | `true` turns on the desktop keyring's lock mark and its unlock from the Mac and the phone ([Unlock the desktop](#unlock-the-desktop-optional); `AGENT_BOARD_KEYRING_UNLOCK=yes` overrides) |

Each feature with settings of its own reads them from a file beside it, read at each use, so a change needs no
restart; every key is optional and the module's `DEFAULTS` lists them with their meanings:

| File | Env override | Settings for |
|---|---|---|
| `~/.config/agent-board/time.json` | `AGENT_BOARD_TIME_CONFIG` | the [Time page](#time-where-your-time-went) (`timeuse.DEFAULTS`) |
| `~/.config/agent-board/allowance.json` | `AGENT_BOARD_ALLOWANCE_CONFIG` | [plan usage](#plan-usage-where-it-went), its prices per model, and the spend meter's `week_zone` (`allowance.DEFAULTS`) |
| `~/.config/agent-board/meetings.json` | `AGENT_BOARD_MEETINGS_CONFIG` | [meeting alerts](#meeting-alerts-optional) (`meetings.DEFAULTS`) |
| `~/.config/agent-board/drafts.json` | `AGENT_BOARD_DRAFTS_CONFIG` | the Mail tab's [drafts](#drafts) on the desktop (`maildrafts.DEFAULTS`) |
| `~/.config/agent-board/colours.json` | `AGENT_BOARD_COLOURS` | the [group colours](#group-colours): when it exists, it replaces the kit's `agentboard/colours.json` whole |
| `~/.config/agent-board/mailweb.json` | `AGENT_BOARD_MAILWEB_CONFIG` | the [phone app](#mail-on-the-phone-agent-mail) and the voice page's devices, `voice_devices` (`mailwebstate.DEFAULTS`); read as its server starts and as the Mac asks for an enrolment code |
| `~/.config/agent-board/voice.json` | `AGENT_BOARD_VOICE_CONFIG` | the [voice page](#voice-page-the-front-desk-optional): its model, voice, Gemini key's place, excluded groups, limits (`agentboard/voice.json`) |
| `~/.config/agent-board/desk.json` | `AGENT_BOARD_DESK_CONFIG` | [the desk](#the-desk-optional) (`agentboard/desk.json`); read as the daemon starts |
| `~/.config/agent-board/speech.json` | `AGENT_BOARD_SPEECH_CONFIG` | the desktop's voice ([The ask box on the Mac](#the-ask-box-on-the-mac); `agentboard/speech.json`); read as the daemon starts |
| `~/.config/agent-board/keyring.json` | | the [keyring unlock](#unlock-the-desktop-optional)'s `daemon` (`keyring.DEFAULTS`) |
| `~/.config/agent-board/transcripts.json` | `AGENT_BOARD_TRANSCRIPTS_CONFIG` | the [transcript archive](#transcript-archive-optional): its folder (required) and sources (`transcripts.DEFAULTS`) |

The two Google files are "authorized_user" OAuth JSONs (`client_id`, `client_secret`, `refresh_token`), the kind
`google-auth` writes and many MCP servers keep. Make one with your own Google Cloud OAuth client (desktop app) and the
scopes you want; it may be one file for both keys if it has all of them. Each must belong to one of your addresses.

**Mac app** settings live in `defaults` for the `org.agentkit.agentboard` domain:

| Key | Default | Meaning |
|---|---|---|
| `host` | `desktop` | the ssh alias of the desktop; also editable from the menu-bar menu ("Desktop host…") |
| `collector` | `~/.local/bin/agent-board` | the collector's path on the desktop (expanded by the remote shell) |
| `activityWatch` | unset (off) | ActivityWatch's server on the Mac, e.g. `http://localhost:5600`, for the Time page's screen time |
| `askBox` | `false` | the [ask box](#the-ask-box-on-the-mac) and its hot key ⌃⌥A (also from the menu-bar menu) |
| `askVoice` | unset | the Apple voice that reads a sentence the desktop's voice could not speak; unset, the best one for the Mac's language |
| `keyringScript` | unset: read from the collector's kit wrapper | the desktop's `board/bin/agent-board`, which the [keyring unlock](#unlock-the-desktop-optional) runs on the system's Python |

The Mac also keeps the layout (`~/Library/Application Support/AgentBoard/layout.json`), its copy of the Mail-tab
drafts (`mail-drafts/` beside it), and in its defaults what it has notified (`redNotified`, `meetingAlertsFired`, and
`securityAlertNewest`, the time of the newest security alert notified) and which chat posts you have seen
(`chatSeen`).

## Optional: your Gmail (read-check and Forward)

Off by default, and nothing else in the board needs it: without it, the Mail tab and the phone app are where you read
agent mail. With your own Gmail login, three things turn on:

- **The read-check.** A green tile turns blue ("you've read its mail") once you have read the agent's latest mail,
  in Gmail or in the Mail tab, and green again if you mark it unread in Gmail and the tab has not counted it read.
  Mail the mailroom kept for the Mail tab alone is never looked up in Gmail: the tab's read state alone counts for it.
  Without the login, only the Mail tab's read state counts.
- **Forward** in the Mail tab: a draft in your Gmail of an agent's mail, to send on (it needs the `gmail.compose` or
  `gmail.modify` scope; see [Mail tab](#mail-tab)).
- **Your own email** on the Time page: the mail you sent, except to the agents (subjects only).

To turn it on, point `gmail_credentials` at an "authorized_user" OAuth JSON (`client_id`, `client_secret`,
`refresh_token`) for your own Gmail with the `gmail.readonly` scope (read-check and Time page), plus `gmail.compose`
or `gmail.modify` for Forward, for example the file a Gmail MCP server keeps, `~/.gmail-mcp/credentials.json`:

```bash
mkdir -p ~/.config/agent-board
echo '{"gmail_credentials": "~/.gmail-mcp/credentials.json"}' > ~/.config/agent-board/config.json
```

The login must belong to an owner address (`owner_email` or `allowlist`), or it is not used. Once a minute it makes
one `messages.list` of unread mail from the agents' address in the last 8 days, plus one cached `rfc822msgid:`
lookup per mail the board newly asks about. Metadata only, no bodies; the token never appears in a URL or a log.
It changes nothing in your Gmail but the drafts Forward makes. If the login fails, "read-check off" shows in the top
bar and tiles behave as without it.

## Install

The kit's top-level `install.sh` does the desktop part. By hand:

### On the desktop

1. Python 3.9+ (stdlib only, but for the phone app's `cryptography`: `board/requirements.txt`, which the kit
   installer puts in its venv), `git`, `tmux`; `gh` (logged in) for PR chips; `nvidia-smi` optional; `systemd-run`
   (systemd's, with a user manager) for the Orchestrator button.
2. The mailroom installed and configured (`../mailroom/README.md`), including its `agent-status` hooks. Group chats
   need its `agent-group`, and the Mail tab its mailbox (`agent-mail mailbox`); with an older mailroom the Chats
   button stays hidden, the Mail tab says the desktop has no mailbox yet, and the rest works.
3. Wrappers on PATH (the Mac app runs `~/.local/bin/agent-board` over a non-login ssh shell):

   ```bash
   KIT=~/code/agent-board-kit          # wherever the kit is checked out
   for t in agent-board agent-restart agent-cli-update agent-speech; do
     printf '#!/bin/sh\nexec python3 %s/board/bin/%s "$@"\n' "$KIT" "$t" > ~/.local/bin/$t
     chmod +x ~/.local/bin/$t
   done
   printf '#!/bin/sh\nexec %s/board/bin/agent-keyring-unlock "$@"\n' "$KIT" > ~/.local/bin/agent-keyring-unlock
   chmod +x ~/.local/bin/agent-keyring-unlock      # a shell script: it runs the system's Python itself
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

6. Optional, to keep the [Time page](#time-where-your-time-went) fresh (otherwise it is computed when you open it):
   the same for `agent-board-time` (the kit installer does this with `WITH_TIME_PAGE="yes"` in kit.env):

   ```bash
   sed -e "s|{{PYTHON}}|$(command -v python3)|" -e "s|{{KIT_DIR}}|$KIT|" \
       $KIT/board/systemd/agent-board-time.service.in > ~/.config/systemd/user/agent-board-time.service
   cp $KIT/board/systemd/agent-board-time.timer ~/.config/systemd/user/
   systemctl --user daemon-reload && systemctl --user enable --now agent-board-time.timer
   ```

7. Optional: the phone app, set up as [Mail on the phone](#mail-on-the-phone-agent-mail) says.
8. Optional: your Gmail and Google logins in `~/.config/agent-board/config.json` ([Configuration](#configuration)),
   and your own group colours in `~/.config/agent-board/colours.json` ([Group colours](#group-colours)).
9. Optional, each set up as its section says, and each off until you do: [Researcher](#researcher-optional) (the plugin;
   `WITH_RESEARCHER`), [a per-token login](#a-per-token-login-and-its-spend-meter-optional) (the mailroom's
   side; the board needs nothing more), the [voice page](#voice-page-the-front-desk-optional) (needs the phone app),
   the desktop's voice (`agent-speech.service` and its venv, [The ask box on the Mac](#the-ask-box-on-the-mac)),
   [the desk](#the-desk-optional) (`agent-board-desk.service`), the [keyring unlock](#unlock-the-desktop-optional)
   (`keyring_unlock`, and `python3-gi` and `python3-cryptography` for the system's Python) and the [transcript
   archive](#transcript-archive-optional) (`transcripts.json` and its timer). The kit installer does the units and
   settings from kit.env: `WITH_SPEECH`, `WITH_DESK`, `WITH_KEYRING_UNLOCK`, `WITH_TRANSCRIPTS` and `TRANSCRIPTS_DIR`,
   `WITH_VOICE_PAGE`. Units are rendered as the restore unit is in step 4.

### On the Mac

Requirements: macOS 13 or later, the Command Line Tools (`xcode-select --install`; SwiftPM, no Xcode needed), and
an ssh alias for the desktop that logs in without a prompt (key auth, `BatchMode` works), reachable over the LAN or
a VPN. Check with `ssh desktop true`.

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
systemctl --user disable --now agent-cli-update.timer agent-board-time.timer agent-board-mail-web.service
rm ~/.config/systemd/user/agent-cli-update.service ~/.config/systemd/user/agent-cli-update.timer
rm -f ~/.config/systemd/user/agent-board-time.service ~/.config/systemd/user/agent-board-time.timer
rm -f ~/.config/systemd/user/agent-board-mail-web.service
systemctl --user disable --now agent-board-desk.service agent-speech.service agent-board-transcripts.timer
rm -f ~/.config/systemd/user/agent-board-desk.service ~/.config/systemd/user/agent-speech.service
rm -f ~/.config/systemd/user/agent-board-transcripts.service ~/.config/systemd/user/agent-board-transcripts.timer
rm -rf ~/.local/share/agent-kit/speech-venv     # the desktop's voice (Kokoro's model stays in ~/.cache/huggingface)
sudo tailscale serve --https=8466 off      # only if you published the phone app (8466: its port in mailweb.json)
rm ~/.local/bin/agent-board ~/.local/bin/agent-restart ~/.local/bin/agent-cli-update
rm -f ~/.local/bin/agent-speech ~/.local/bin/agent-keyring-unlock
rm -rf ~/.local/state/agent-board ~/.config/agent-board      # also removes the drafts, the phone's passkeys, the
                                                             # voice transcripts and the Gemini key file
# the transcript archive's folder is yours: remove it by hand if you want it gone
```

## Developing

```bash
python3 -m unittest discover -s tests          # fixtures only, no real tmux, ssh, systemd, Gmail, Tailscale or network
bin/agent-board snapshot --pretty | less       # what the app sees
echo '{"action": "restart_plan"}' | bin/agent-board act | python3 -m json.tool    # a read-only action
bin/agent-board time --help                    # the Time page, by hand
```

The tests point every config and state path at a scratch directory (`tests/_kitenv.py`) with an example owner,
so they never read your real configuration; the kit's `scripts/run-tests.sh board` also runs them in a throwaway
HOME. The phone app's tests (`tests/test_mailweb.py`) run its real server on a socket of their own with tailscale and
agent-mail stood in for and a test passkey that signs with a real key; they skip without `cryptography`. The keyring
tests (`tests/test_keyring.py`) run a private `dbus-daemon` of their own with a fake keyring on it, never your
session bus; they need `dbus-daemon`, `busctl`, and `python3-gi` and `python3-cryptography` for `/usr/bin/python3`.
The voice desk's, the desk's and the ask box's tests stand in for `claude` and `codex` (`tests/desk_claude.py`, and
fakes in `tests/test_voicedesk.py`) and for Kokoro (`fixtures.Voice`), so nothing reaches a model; the transcript
archive's stand in for ssh and rsync. Its page is
tested in headless Chrome (`google-chrome` or `chromium` on PATH, else those tests skip), driven over the DevTools
protocol on a pipe (`tests/chrome.py`, a profile of its own in the temporary folder, removed after), and its scripts'
shared rules under `node` (skipped without it). On the Mac:

```bash
AgentBoard.app/Contents/MacOS/AgentBoard --probe              # real ssh stream + a read-only action, headless
AgentBoard.app/Contents/MacOS/AgentBoard --render snap.json out.png 1060 680 [--light] [--overview] [--select ID]
AgentBoard.app/Contents/MacOS/AgentBoard --selftest-keys      # zoom shortcuts reach the board
AgentBoard.app/Contents/MacOS/AgentBoard --selftest-layout    # groups and sub-groups, and the saved layout decodes
AgentBoard.app/Contents/MacOS/AgentBoard --selftest-mail      # the Mail tab's rules, offline (also -decode, -chats,
                                                              # -widgets, -comments, -popout, -copy, -forward,
                                                              # -meetings, -usage, -time, -screen, -phone,
                                                              # -security)
```

`--render` draws the real views offscreen from a saved snapshot (`agent-board snapshot > snap.json` on the
desktop), so screenshots need no screen-recording permission; `--render-time`, `--render-usage`, `--render-mail`
(with `--popout`, a thread in its own window), `--render-chats` and `--render-meeting` draw the other sheets. The
header of `mac/Sources/AgentBoard/main.swift` lists every flag. Keep animations in Core Animation (`Pulse.swift`): a
SwiftUI `repeatForever` animation re-runs the board's view update every frame; `--selftest-cpu SNAPSHOT.json`
measures the CPU the board uses offscreen.

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
- **Resume says the mailroom's keys lock is not there.** The mailroom checkout is older than its keys lock
  (`agentmail/commands.py`'s `keys_lock`); update it.
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
- **`agent-cli-update.service` failed.** `agent-cli-update log` shows which CLI, update or trial, and why; run
  `codex update` or `claude update` by hand to see the full output, and the mailroom's README, "Fast mode", for a
  failed trial.
- **No Chats button.** The desktop has no `agent-group` on PATH: update the mailroom (`git pull && ./install.sh`).
- **The Mail tab says the desktop has no mailbox yet.** The mailroom is too old for `agent-mail mailbox`; update it.
  A mail action that fails shows agent-mail's own error.
- **Forward fails.** Without `gmail_credentials` it is off and says so; with a read-only login (`gmail.readonly`)
  Gmail refuses the draft: use a login with `gmail.compose` or `gmail.modify`. A mail sent before the mailroom kept
  copies of agent mail cannot be forwarded, and the error says so.
- **The phone app's unit keeps restarting.** `journalctl --user -u agent-board-mail-web -n 20` says why: no
  `mailweb.json` with `devices` and `proxy_uids`, no `cryptography` in the Python it runs on (`board/requirements.txt`),
  Tailscale not logged in, or the page's port open to the public through Funnel. Mend it and it starts within 10
  seconds.
- **The phone gets "Forbidden" or nothing.** The journal names the check that refused it: a login not in `logins` or
  the owner's addresses (`tailscale status --json` shows your login), a device not in `devices` (the phone's `ID`, not
  its name), the page asked for at another host or port than `tailscale serve` publishes, or a connection from a uid
  not in `proxy_uids` (tailscaled's: `ps -o uid= -C tailscaled`).
- **"No code" in the Mac's Phone… sheet.** The desktop says why: `mailweb.json` does not load, or Tailscale gives no
  tailnet name (not logged in).
- **Enrolment fails with "the owner could not be told of it".** The enrolment's security alert could not be sent or
  kept (`agent-mail` missing, no `owner_email`, or a mailroom too old for `--alert phone-enrolment`): no passkey is
  enrolled without it. Update the mailroom and try again.
- **No notification for a security alert.** The Mac must allow AgentBoard's notifications (System Settings →
  Notifications); without that permission it falls back to a plain Notification Center message that opens nothing.
  A security alert whose line in `security-alerts.jsonl` does not read is listed under the top bar's problems.
- **The Orchestrator button says systemd-run has no user bus.** The desktop's user manager is not running for the
  SSH login: enable lingering (`loginctl enable-linger $USER`).
- **"No group colours" over the Mail list, or the Time page or usage sheet fails on colours.json.** Your
  `~/.config/agent-board/colours.json` does not load; the error says what is wrong. Delete it to go back to the
  kit's.
- **No meeting alerts.** They need `"meeting_alerts": true` and `google_token` with Calendar read access (`kit
  doctor` checks both); a calendar that cannot be read is listed under the top bar's problems. The Mac must allow
  AgentBoard's notifications (System Settings → Notifications) for the 10-minute one; the overlay needs no permission.
- **The Time page names a source as failed.** `agent-board time` on the desktop prints why; `time.log` in
  `~/.local/state/agent-board/` keeps the history. A source that is not set up is left out silently.
- **Plan usage shows dollars, not percent.** Too few usage readings yet: the collector records one every 30 minutes
  while the Mac app is connected, and the fit needs 8 six-hour stretches with agent use in the last 21 days.
- **No spend meter for a per-token login.** Its `usage.claude_accounts` entry needs `weekly_budget_usd`; a model
  with no price in `allowance.json` is left out and named on hover. A bad `week_zone` is listed under the top bar's
  problems.
- **No Login menu in Start agent here….** The mailroom names no manual login (`manual: true`), or is older than
  manual logins.
- **No Researcher switch.** The mailroom reports no agent for the thread (its `researcher` is null or missing): the
  mailroom is older than Researcher, or the thread has no single routed agent. "Delivery failed" with a reason names
  what the mailroom found on the agent's machine (the plugin missing or too old, say).
- **No lock mark, or `keyring-status` exits 3 saying the unlock is off.** `keyring_unlock` is not `true` in
  `~/.config/agent-board/config.json`.
- **`agent-keyring-unlock` says the process could not be read or must run on the system's Python.** Run it through
  its wrapper (`agent-keyring-unlock`), never as `agent-board keyring-unlock`; the system's Python needs `python3-gi`
  and `python3-cryptography` (`sudo apt install python3-gi python3-cryptography`).
- **The voice page says it has no key.** Put the Gemini key in `~/.config/agent-board/gemini-key`, mode 600, one line
  (or set `gemini_key_env`); the page names what is wrong with the file. "Forbidden" on `/voice` means the device is
  not in `voice_devices`.
- **`looking` says no Mac is configured.** Set `MAC_HOST` in kit.env (the mailroom config's `remote_hosts.mac`), and
  check `ssh -o BatchMode=yes <alias> true` and ActivityWatch on the Mac.
- **The ask box says the desk could not be reached.** `systemctl --user status agent-board-desk`; exit 3 means Claude
  held more than the desk's tools, and the journal says which (it is not restarted).
- **Answers are shown but not spoken.** The desktop's voice is not running: `systemctl --user status agent-speech`,
  `agent-speech say test`; `speech.log` in `~/.local/state/agent-board/voice-desk/` says why each time.
- **The transcript archive exits 2.** No `archive` folder in `transcripts.json`, or the folder is missing (a disk not
  mounted): it never makes the folder.
- **Agents did not come back after a restart.** `systemctl --user status agent-restart-restore.service` and
  `agent-restart status`; lingering must be on for the unit to run before you log in.
