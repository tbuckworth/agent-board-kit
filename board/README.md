# Agent board

A Mac menu-bar app that shows every Claude Code and Codex session running on an always-on Linux desktop as a
coloured tile, with the desktop's RAM, CPU, GPU, disks and plan usage. From a tile you can message an agent,
close it, resume it, switch its login or model, attach to its tmux session, or clean up its git worktree. The
**Desktop** chip restarts (or updates and restarts) the desktop and brings every agent back afterwards.

Beside the tiles it has:

- a **Mail** tab with the mail between you and your agents, kept by the mailroom: threads, replies with files,
  drafts, snooze, and the options and plots agents put in their mail ([Mail tab](#mail-tab));
- **group chats** between you and several agents, one for each sub-group of the board ([Group chats](#group-chats));
- a record of everything you **sent** from the board, which agents tick off when they have dealt with it
  ([Sent messages](#sent-messages-what-you-sent-and-what-is-dealt-with));
- where a **plan window's usage** went, by group and agent ([Plan usage](#plan-usage-where-it-went));
- a **Time** page: where your time went, per day and board group ([Time](#time-where-your-time-went));
- optional **meeting alerts** from your Google Calendar ([Meeting alerts](#meeting-alerts-optional)).

It is one component of the agent kit. It reads what the [mailroom](../mailroom/README.md) records (the status log,
the mail routing store, the mailbox, the mailroom config) and runs the mailroom's own tools for anything it changes
(`agent-send`, `agent-close`, `agent-switch`, `agent-spawn`, `agent-orchestrate`, `agent-mail`, `agent-group`).
Without the mailroom the board still shows sessions and metrics, but tiles never turn green and most actions are
unavailable.

```
Mac                                                    desktop (Linux; ssh alias `desktop` by default)
AgentBoard.app ── ssh desktop agent-board stream ──►  board/bin/agent-board stream   (stdlib Python)
               ◄── one JSON line per change / 30 s ──  session registries, status log, mailroom store, /proc, nvidia-smi
               ── {"cmd":"act"} on the same stream ─►  one validated action, run as `agent-board act`
```

Nothing listens on a port. The app holds one SSH session open. The collector behind it exits when the connection
drops (EOF on stdin, a broken pipe, or a write stalled for 2 minutes) and after 12 hours; the app reconnects.

A click goes over the stream that is already open, which saves an SSH handshake per action. The collector's first
line (its hello line) says whether it takes actions (`acts`); with no live stream, or a collector that doesn't, the
app runs `ssh desktop agent-board act` with the JSON on stdin. If the stream drops while an action is waiting, the
app reports that it may or may not have run, and does not retry. A request too big for the stream (a mail reply with
files) goes over an SSH connection of its own.

## Contents

| Path | What it is |
|---|---|
| `bin/agent-board` | The desktop side: `snapshot`, `stream`, `act`; `done` and `sent` (sent messages), `time` (the Time page), `receive` (files from the Mail tab). |
| `bin/agent-restart` | Restart or update-and-restart the desktop, and resume the agents after boot. |
| `bin/agent-cli-update` | Update Codex and Claude Code (`codex update`, `claude update`); run by a timer. |
| `agentboard/__init__.py` | Lists every module of the package with one line on each. |
| `agentboard/classify.py` | Pure rules: tile state, display name, git stage, "waiting on you". |
| `agentboard/sources.py` | Read-only readers (session registries, status log, mailroom store, git, PRs, metrics, usage) and shared helpers. |
| `agentboard/collect.py` | Assembles one snapshot from the readers. |
| `agentboard/actions.py` | The only things the app may make the desktop do, each validated; `ACTIONS` is the table. |
| `agentboard/relaunch.py` | Starting an exited agent again: its login, model, effort, tmux session and memory cap. |
| `agentboard/sent.py`, `groups.py` | What you sent from the board and its state; the board's tile groups as the Mac sends them. |
| `agentboard/chats.py`, `subchats.py` | Group chats through the mailroom's `agent-group`; a chat per sub-group. |
| `agentboard/mailbox.py`, `gmailstate.py` | The Mail tab over `agent-mail mailbox`; your Gmail's read state and the tab's Sync with Gmail (optional). |
| `agentboard/allowance.py`, `credits.py` | Where a plan window's usage went; the Codex credit balance. |
| `agentboard/timeuse.py`, `timesources.py`, `screen.py` | The Time page, its sources, and the Mac's screen time. |
| `agentboard/meetings.py`, `google.py`, `webapi.py` | Optional meeting alerts; a small Google API client for your own login. |
| `agentboard/restart.py` | The restart engine (plan, worker, boot-time restore). |
| `agentboard/config.py` | Where the mailroom and its config are; the board's own settings. |
| `agentboard/cliupdate.py` | What `agent-cli-update` runs, and its log. |
| `systemd/agent-restart-restore.service.in` | User unit that resumes the agents after an `agent-restart` reboot. |
| `systemd/agent-cli-update.service.in`, `systemd/agent-cli-update.timer` | User units that run `agent-cli-update` every 6 hours. |
| `systemd/agent-board-time.service.in`, `systemd/agent-board-time.timer` | Optional user units that recompute the Time page every 15 minutes. |
| `mac/` | The SwiftPM app (`Sources/AgentBoard`; `main.swift` lists every flag and source file), `build-app.sh`, `install.sh`, `Info.plist`. |
| `scripts/mac-build.sh`, `scripts/mac-install.sh` | Build and install on the Mac from the desktop over SSH. |
| `tests/` | Unit tests for the collector, actions, restart, chats, mail, usage, Time page, meetings, config and CLI updates. |

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
appear as `+N` on their parent's tile. A Codex session counts as live only while its writer lock is actually held
(`/proc/locks`); the lock file alone outlives its process.

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
| read in Gmail (optional) | your own Gmail login, metadata only; see [Optional: your Gmail](#optional-your-gmail-read-check-and-sync) |
| meetings (optional) | your Google calendars, every 5 minutes, when `meeting_alerts` is on; see [Meeting alerts](#meeting-alerts-optional) |

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
- The Mail tab has keys of its own: the arrows, Space, **e**, **r** and ⌘V ([Mail tab](#mail-tab), "Keys"). A
  tile's right-click menu has **Open latest mail in the Mail tab** beside the Gmail one.
- While a dialog is open the board holds its updates, so typing in it isn't slowed by redraws of every tile. The
  newest snapshot and metrics are applied once it closes.
- Drag a tile onto another tile to put it just before that tile, or onto a group to add it to that group. Drop it
  on "New group" to start one. Drag a group's name to reorder groups. Groups can be renamed, collapsed and deleted;
  deleting a group keeps its tiles.
- **Sub-groups** (one level) sit side by side inside a group as columns. Make one with ⋯ → "New sub-group…" or the
  "+ sub-group" cell; drag tiles and sub-group names to move them; its ⋯ menu offers rename, move left/right, "make
  it a group of its own", and delete (which keeps its tiles). Each sub-group has a group chat of its agents
  ([Group chats](#group-chats)).
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
- **Send message…** delivers your text to the agent (`agent-send --raw --cli CLI ID`), headed
  `[relayed from <owner_name> via agent-board]` and ending with how the agent marks it done
  ([Sent messages](#sent-messages-what-you-sent-and-what-is-dealt-with)).
- **New group chat…** makes a chat with this agent ticked ([Group chats](#group-chats)).
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
  "Update available" menu (the same holds for agents `agent-restart` brings back). When Codex stops a start
  because a hook it runs is new or changed since it was trusted (a plugin update changes its hook's path, for
  instance), Resume and the restore choose "Continue without trusting": those hooks stay off, and Resume's result
  and the restore email say how many. To trust them, use `/hooks` in any Codex session.
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

## Sent messages: what you sent, and what is dealt with

Everything you type into the board, except your replies in the [Mail tab](#mail-tab) (the mailroom keeps those as
your mail), is kept in full in `~/.local/state/agent-board/sent.jsonl` (mode 600): messages, resume notes, briefs
for new agents, orchestrator requests and group-chat posts. A failed send is kept too, so the text is never lost.
Each one has a short ref such as `k3f9`. The delivered text ends with its ref and one command for the agent to run
once it has dealt with all of it (the board's own `bin/agent-board`, by its full path):

    /path/to/agent-board-kit/board/bin/agent-board done k3f9 -m "what you did, in one line"

Each message is then:

- **waiting**: delivered, not marked done;
- **done**: the agent marked it done. It shows in bold with the agent's note until you tick it off;
- **ticked off**: you ticked it. You can tick any message, and untick it again. Messages recovered from the agents'
  transcripts are marked `recovered` and start ticked off;
- **not delivered**: the send failed.

A done message from the last 7 days is also ticked off by itself once you have seen the agent's emails about it:
every email that agent sent you from the message until an hour after it marked it done, each one either read (in
the Mail tab, or in your Gmail with the [read-check](#optional-your-gmail-read-check-and-sync) on) or replied to.
The tick's note says so, e.g. `you read its email "Fixed the chip"`. A done message with no such email stays bold,
and a message you have ticked or unticked yourself is never ticked again by mail.

On the Mac, the **Sent** button in the top bar (it reads "2 to check" in bold when agents have marked messages
done), or **Sent messages…** in the menu-bar menu, opens the list. The list can hide ticked-off messages, and "Tick
off all done" clears the bold ones at once. On the desktop, `agent-board sent` prints the last 20 with their states
(`-n 0` for all, `--json` for the records).

## Group chats

A group chat is a thread between you and some of your agents in which every post reaches every member. The mailroom
keeps them (`agent-group`; its README says how posts reach agents and which wake them); the board reads and writes
them as you (`agentboard/chats.py`, the `chat_*` actions). The **Chats** button in the top bar, or **Group chats…**
in the menu-bar menu, opens them; it shows once the desktop has `agent-group`, and reads "2 new" in bold when chats
have posts you haven't had open (kept on the Mac as `chatSeen`). The sheet lists the chats, newest post first, and
shows one: its members with the posts that wake each, its posts, and a box where ⌘↩ posts to every member. Members
are added and removed from its Members menu, and a chat is hidden or shown from its right-click menu. **New
chat…**, or **New group chat…** on an agent's menu, makes one with the agents ticked; only a named session can be a
member.

Each sub-group of the board has a chat of its own, named after it (`agentboard/subchats.py`). When the Mac sends the
layout, the desktop makes the chat once the sub-group holds two running agents with a session name, and from then
on keeps its title and members in step with the sub-group: moving a tile in or out adds or removes that agent, and
an agent that has exited is left out until it runs again and the layout next changes. The chat is hidden, its posts
kept, when the sub-group is deleted or made a group of its own, and shown again if it comes back. Its members are
the sub-group's to decide, so its Members menu is replaced by a note, and its member line names the sub-group's
agents left out for want of a name a chat can use (letters, digits, `.`, `_` and `-` only, up to 64; `/rename`
gives one). A chat change that fails is listed under the top bar's problems. Top-level groups have no chat.

Your post is on the record of what you sent (kind `chat`, under the chat's title) and ends like a board message, with
its ref and the command that marks it done; whichever member deals with it runs it, and the Sent list says who. The
board shows your posts without that last line. Each post carries an id the Mac makes, which agent-group keeps as its
idempotency key, so sending the same draft again cannot post it twice.

## Mail tab

The **Mail** tab shows the mail between you and your agents, which the mailroom keeps on the desktop: its README
("The Mail tab's mailbox") says what it keeps, how the read, archive and snooze state works, and how it is backed up.
Your Gmail receives the same mail. The tab's label counts the unread threads (`Mail 3`); with no `mailbox` section
in the snapshot (a mailroom without the mailbox), the tab says the desktop has no mailbox yet and asks it nothing.
The menu-bar menu has **Mail** too. On this tab the top bar leaves out the machine meters and plan usage, to give the
mail the room.

- **The list** (left): a search over every box (all its words in a thread's subjects, agents' names or text), Inbox /
  Snoozed / Archived / All, the open thread's buttons, and the box's newest 100 threads. A row shows the thread's
  agents and, in a badge, its message count; its time (for a snoozed thread, when it comes back, in amber), its
  subject, the start of its newest message, a paperclip for attachments, and **Draft** in red when a reply to it is
  started. As in Gmail, an unread thread is bold, with a dot; a read one is plain and greyed. Right-click marks a
  thread read or unread, or archives it or moves it to the inbox. **Sync with Gmail**, in the ⋯ menu, runs
  `mail_sync` (below) after asking, and says what it changed.
- **The buttons**, above the list, act on the open thread: **Reply**; **Archive** (on an archived or snoozed thread,
  **Inbox**); **Snooze**, until later today (three hours on, to the nearest hour), tomorrow, Saturday or next Monday
  at 08:00, or a date and time; and **Unread**, which also closes it. After Archive, Inbox or Snooze, the next thread
  opens.
- **The thread** (right) is the document the desktop makes of it, headed by its subject: read messages folded,
  unread ones and the newest open. In a thread of several mails, the one being read is outlined in blue and named in
  a bar above the document ("Mail 2 of 5", its sender and time, with ‹ › to step); a thread opens at its first unread
  mail, else its newest. Opening a thread marks it read up to the newest message on screen, so mail that arrives
  after is still unread; Archive and Snooze reach only that far too.
- **Options and plots**: where an agent's mail asks you to choose or shows numbers, it may carry widgets (the
  mailroom's README, "Widgets in agent mail"), which the tab draws where the agent put them. Options are cards to
  click, checkboxes when several may be picked, with the recommended one marked and what the agent does with no
  answer; a click opens the reply with the choice as its first line ("Option 2: …"), replacing an earlier choice's
  line and keeping what you typed, and nothing is sent until you send it. A plot (line, scatter or bar) shows values
  on hover, hides a series when its name is clicked, and zooms on a dragged range of x (double-click resets). A
  widget the tab cannot draw keeps the plain-text version the mailroom put in its place, and the tab says why.
- **Keys**, when no text box has the keyboard: **↓** or **j** and **↑** or **k** open the thread below or above;
  **→** and **←** move to the newer or older mail in the thread; **Space** and **⇧Space** scroll the document a
  screen down or up; **e** archives the thread (or moves it back to the inbox); **r** opens the reply box; **⌘V** puts
  files or an image from the clipboard in the reply. In a text box the keys type; **Esc** gives the keyboard back, and
  so does running a search.
- **Replying**: **Reply** or **r** opens a box under the thread, which answers its newest agent mail; ⌘↩ or **Send**
  sends. The reply carries a key made on the Mac until the desktop has it, so sending again after a dropped
  connection never sends it twice; a reply that fails keeps its text and files and says why. Under the box, your
  last three replies in the thread show their state (sending, queued, delivered, with the orchestrator, held).
- **Files**: drag files onto the box or the mail, paste them, or pick them with the paperclip. A reply carries at
  most 20 files and 25 MB of them together (the mailroom's limits), and may be files alone. The mailroom keeps them
  with the reply and puts them on the agent's own machine, naming each file's path in the message the agent receives.
- **Files too big for a reply**: the box asks whether to copy them to the desktop instead. Each goes over SSH to
  `agent-board receive`, into `~/.local/state/agent-board/mail-files/<day>/` on the desktop (readable by you only,
  outside the mailroom and its backup), and a line with its path there joins the reply, which waits until every copy
  has arrived. More than 20 files, a folder, or a pasted image too big are refused, and the box says why.
- **Drafts**: each thread keeps its own draft, its text and files, when another thread opens and when the app quits:
  `~/Library/Application Support/AgentBoard/mail-drafts/` holds `drafts.json` and a copy of each file, which goes
  when it is sent, taken out or the draft discarded (the trash button, after asking).
- **Updates**: while the tab is on screen the list is read every 15 seconds, and ⌘R reads it at once. The open thread
  is read again when the list shows it changed, or while a reply in it is sending or queued. Threads the tab has read
  are kept on the Mac (up to 64 MB of documents), and the two threads on each side of the open one are read before
  they are asked for.
- **Safety**: the desktop's document allows only formatting, http, https and mailto links, and `data:` images, under
  a strict Content-Security-Policy. The Mac shows it in a WKWebView that runs no JavaScript from the page, blocks every
  load but `data:` URLs with content rules, keeps no cookies or cache, and never navigates: a clicked http, https or
  mailto link opens in the browser or mail app, and any other link does nothing. Widgets are drawn by the app's own
  code (`mac/Sources/AgentBoard/widgets.js`, in a content world of its own, making every node from the spec's text,
  never from markup): agents send only data, and a click can only put the line it shows in the reply.
- **Attachments**, an agent's or the files of your own replies, are fetched from the desktop on click (`mail_part`)
  into `AgentBoard-mail/` in the Mac's temporary folder, readable by you only and quarantined (macOS asks before
  running anything in it), and opened with their default app.

The desktop side is `agentboard/mailbox.py` and the `mail_*` actions. Each runs `agent-mail mailbox ...` as you and
hands its JSON answer to the Mac unchanged; agent-mail refuses to change the mailbox from inside an agent session.
A reply is the mailroom's record of it (an inbound mail from you), routed on the mailroom's next poll exactly as your
emailed replies are: to the agent's session, to the group chat for a group's mail, or to the orchestrator for system
mail or an agent that has gone. The actions record (`actions.jsonl`) has every reply's thread, key, length and its
files' names and sizes, and every mark except a read one.

`mail_sync` (optional; it needs [your Gmail login](#optional-your-gmail-read-check-and-sync) with the `gmail.modify`
scope) brings the tab and your Gmail into step, what is done in either counting as done in both (the mailroom's
`mailbox sync` gives the rule): a thread archived or read in Gmail is archived or read in the tab, and the agents'
mail of a thread archived or read in the tab is archived or marked read in Gmail. Nothing comes back to the inbox or
goes unread, and snoozed threads are left alone. It changes nothing in Gmail but the Inbox and Unread labels of the
agents' mail. Without the login it says so and changes nothing; with a login allowed only to read, Gmail refuses
the change (HTTP 403), the answer says so and that the tab's side was done.

## Plan usage: where it went

Clicking a usage meter in the top bar opens where that window (5-hour, weekly, or a model's own weekly) has gone
since it last reset: a chart of the window stacked by board group, with **Other** on top, and below it a list. Click
a group in the list to open its sub-groups, and a sub-group to open its agents. The desktop works it out on request
(the `usage_split` action, `agentboard/allowance.py`):

- **Agents' use.** Every reply in the agents' transcripts is priced at API rates. A Claude agent's subagents count as
  its own, and so do a Codex thread's sub-threads; the orchestrator's runs make one row. A reply counts once, though
  failover copies a conversation between logins and a fork copies its history, and it counts on the login its agent
  was on when it was written (read from the moves in the status log).
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

## Time: where your time went

**Time** in the top bar, or in the menu-bar right-click menu, opens a chart: one bar per day over 7, 14 or 30 days,
stacked by board group, with each group's sub-groups in shades of its colour. Hovering over a segment names it under
the chart. Clicking narrows the view: a group (in the legend or a bar) to its sub-groups, then a sub-group, and a
day's date under its bar to what counted that day. **1 day** shows one day across its hours instead: a lane per group
and sub-group, each 5-minute block it was active in filled at its clock time, hatched where the group is a guess. The
menu beside the range chooses which time is counted: **All time**, **Work time** or **Outside work** (the
`work_hours` setting, Monday to Friday 09:00 to 18:00 by default). Below the chart is a list of the tiles and items
behind what is shown; clicking one shows how its time adds up.

**What counts.** Time is counted in 5-minute blocks. A block counts when you did any of the following in it, and it
is shared evenly between the groups active in it, so a day's total never exceeds your active time. Only the first
three are on by default; each of the others is on only when its source is set up, and a source that is off is simply
left out (not an error).

| Activity | Read from | On when |
|---|---|---|
| prompts you typed to an agent | transcripts of interactive Claude Code and Codex sessions. Briefs, messages that agent-send or the mailroom delivered, and `claude -p` runs do not count | always |
| board messages | `sent.jsonl`. A request to the orchestrator counts for the agent it relayed to or started soon after; a group-chat post is an item of its own | always |
| emails to agents | the mailroom's inbound mail from your addresses, with the same rule for mail the orchestrator handled | always |
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
"Unsorted".

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
| `gmail_credentials` | `null` (off) | path to your own Gmail's OAuth file: the read-check, the Mail tab's Sync with Gmail, the Time page's "your own email" ([below](#optional-your-gmail-read-check-and-sync); `AGENT_BOARD_GMAIL_CREDS` overrides) |
| `google_token` | `null` (off) | path to an OAuth file for your Google account with Calendar read access (and Drive metadata read access for documents): the Time page's meetings and documents, and [meeting alerts](#meeting-alerts-optional) when they are on (`AGENT_BOARD_GOOGLE_TOKEN` overrides) |
| `meeting_alerts` | `false` | `true` turns on [meeting alerts](#meeting-alerts-optional), read with `google_token` (`AGENT_BOARD_MEETING_ALERTS=yes` overrides) |
| `tasks_repo` | the to-do component's `repo` (`~/.config/todo-desk/config.json`), else none | `OWNER/REPO` of your to-do list, for the Time page's to-dos |

Each feature with settings of its own reads them from a file beside it, read at each use, so a change needs no
restart; every key is optional and the module's `DEFAULTS` lists them with their meanings:

| File | Env override | Settings for |
|---|---|---|
| `~/.config/agent-board/time.json` | `AGENT_BOARD_TIME_CONFIG` | the [Time page](#time-where-your-time-went) (`timeuse.DEFAULTS`) |
| `~/.config/agent-board/allowance.json` | `AGENT_BOARD_ALLOWANCE_CONFIG` | [plan usage](#plan-usage-where-it-went), its prices per model (`allowance.DEFAULTS`) |
| `~/.config/agent-board/meetings.json` | `AGENT_BOARD_MEETINGS_CONFIG` | [meeting alerts](#meeting-alerts-optional) (`meetings.DEFAULTS`) |

The two Google files are "authorized_user" OAuth JSONs (`client_id`, `client_secret`, `refresh_token`), the kind
`google-auth` writes and many MCP servers keep. Make one with your own Google Cloud OAuth client (desktop app) and the
scopes you want; it may be one file for both keys if it has all of them. Each must belong to one of your addresses.

**Mac app** settings live in `defaults` for the `org.agentkit.agentboard` domain:

| Key | Default | Meaning |
|---|---|---|
| `host` | `desktop` | the ssh alias of the desktop; also editable from the menu-bar menu ("Desktop host…") |
| `collector` | `~/.local/bin/agent-board` | the collector's path on the desktop (expanded by the remote shell) |
| `activityWatch` | unset (off) | ActivityWatch's server on the Mac, e.g. `http://localhost:5600`, for the Time page's screen time |

The Mac also keeps the layout (`~/Library/Application Support/AgentBoard/layout.json`), Mail-tab drafts
(`mail-drafts/` beside it), and in its defaults what it has notified (`redNotified`, `meetingAlertsFired`) and which
chat posts you have seen (`chatSeen`).

## Optional: your Gmail (read-check and sync)

Off by default. With your own Gmail login, three things turn on:

- **The read-check.** A green tile turns blue ("you've read its mail") once you have read the agent's latest mail,
  in Gmail or in the Mail tab, and green again if you mark it unread in Gmail and the tab has not counted it read.
  Without it, only the Mail tab's read state counts.
- **Sync with Gmail** in the Mail tab (it needs the `gmail.modify` scope; see [Mail tab](#mail-tab)).
- **Your own email** on the Time page: the mail you sent, except to the agents (subjects only).

To turn it on, point `gmail_credentials` at an "authorized_user" OAuth JSON (`client_id`, `client_secret`,
`refresh_token`) for your own Gmail with the `gmail.readonly` scope (read-check and Time page) or `gmail.modify`
(also the sync), for example the file a Gmail MCP server keeps, `~/.gmail-mcp/credentials.json`:

```bash
mkdir -p ~/.config/agent-board
echo '{"gmail_credentials": "~/.gmail-mcp/credentials.json"}' > ~/.config/agent-board/config.json
```

The login must belong to an owner address (`owner_email` or `allowlist`), or it is not used. Once a minute it makes
one `messages.list` of unread mail from the agents' address in the last 8 days, plus one cached `rfc822msgid:`
lookup per mail the board newly asks about. Metadata only, no bodies; the token never appears in a URL or a log.
The sync changes nothing but the Inbox and Unread labels of the agents' mail. If the login fails, "read-check off"
shows in the top bar and tiles behave as without it.

## Install

The kit's top-level `install.sh` does the desktop part. By hand:

### On the desktop

1. Python 3.9+ (stdlib only), `git`, `tmux`; `gh` (logged in) for PR chips; `nvidia-smi` optional.
2. The mailroom installed and configured (`../mailroom/README.md`), including its `agent-status` hooks. Group chats
   need its `agent-group`, and the Mail tab its mailbox (`agent-mail mailbox`); with an older mailroom the Chats
   button stays hidden, the Mail tab says the desktop has no mailbox yet, and the rest works.
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

6. Optional, to keep the [Time page](#time-where-your-time-went) fresh (otherwise it is computed when you open it):
   the same for `agent-board-time` (the kit installer does this with `WITH_TIME_PAGE="yes"` in kit.env):

   ```bash
   sed -e "s|{{PYTHON}}|$(command -v python3)|" -e "s|{{KIT_DIR}}|$KIT|" \
       $KIT/board/systemd/agent-board-time.service.in > ~/.config/systemd/user/agent-board-time.service
   cp $KIT/board/systemd/agent-board-time.timer ~/.config/systemd/user/
   systemctl --user daemon-reload && systemctl --user enable --now agent-board-time.timer
   ```

7. Optional: your Gmail and Google logins in `~/.config/agent-board/config.json` ([Configuration](#configuration)).

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
systemctl --user disable --now agent-cli-update.timer agent-board-time.timer
rm ~/.config/systemd/user/agent-cli-update.service ~/.config/systemd/user/agent-cli-update.timer
rm -f ~/.config/systemd/user/agent-board-time.service ~/.config/systemd/user/agent-board-time.timer
rm ~/.local/bin/agent-board ~/.local/bin/agent-restart ~/.local/bin/agent-cli-update
rm -rf ~/.local/state/agent-board ~/.config/agent-board
```

## Developing

```bash
python3 -m unittest discover -s tests          # 333 tests; fixtures only, no real tmux, ssh, systemd or network
bin/agent-board snapshot --pretty | less       # what the app sees
echo '{"action": "restart_plan"}' | bin/agent-board act | python3 -m json.tool    # a read-only action
bin/agent-board time --help                    # the Time page, by hand
```

The tests point every config and state path at a scratch directory (`tests/_kitenv.py`) with an example owner,
so they never read your real configuration. On the Mac:

```bash
AgentBoard.app/Contents/MacOS/AgentBoard --probe              # real ssh stream + a read-only action, headless
AgentBoard.app/Contents/MacOS/AgentBoard --render snap.json out.png 1060 680 [--light] [--overview] [--select ID]
AgentBoard.app/Contents/MacOS/AgentBoard --selftest-keys      # zoom shortcuts reach the board
AgentBoard.app/Contents/MacOS/AgentBoard --selftest-layout    # groups and sub-groups, and the saved layout decodes
AgentBoard.app/Contents/MacOS/AgentBoard --selftest-mail      # the Mail tab's rules, offline (also -decode, -chats,
                                                              # -widgets, -meetings, -usage, -time, -screen)
```

`--render` draws the real views offscreen from a saved snapshot (`agent-board snapshot > snap.json` on the
desktop), so screenshots need no screen-recording permission; `--render-time`, `--render-usage`, `--render-mail`
and `--render-meeting` draw the other sheets. The header of `mac/Sources/AgentBoard/main.swift` lists every flag.
Keep animations in Core Animation (`Pulse.swift`): a SwiftUI `repeatForever` animation re-runs the board's view update
every frame; `--selftest-cpu SNAPSHOT.json` measures the CPU the board uses offscreen.

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
- **No Chats button.** The desktop has no `agent-group` on PATH: update the mailroom (`git pull && ./install.sh`).
- **The Mail tab says the desktop has no mailbox yet.** The mailroom is too old for `agent-mail mailbox`; update it.
  A mail action that fails shows agent-mail's own error.
- **Sync with Gmail fails.** Without `gmail_credentials` it is off; with a read-only login (`gmail.readonly`) Gmail
  refuses the change (HTTP 403) and only the tab's side is done: use a login with `gmail.modify`.
- **No meeting alerts.** They need `"meeting_alerts": true` and `google_token` with Calendar read access (`kit
  doctor` checks both); a calendar that cannot be read is listed under the top bar's problems. The Mac must allow
  AgentBoard's notifications (System Settings → Notifications) for the 10-minute one; the overlay needs no permission.
- **The Time page names a source as failed.** `agent-board time` on the desktop prints why; `time.log` in
  `~/.local/state/agent-board/` keeps the history. A source that is not set up is left out silently.
- **Plan usage shows dollars, not percent.** Too few usage readings yet: the collector records one every 30 minutes
  while the Mac app is connected, and the fit needs 8 six-hour stretches with agent use in the last 21 days.
- **Agents did not come back after a restart.** `systemctl --user status agent-restart-restore.service` and
  `agent-restart status`; lingering must be on for the unit to run before you log in.
