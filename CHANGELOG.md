# Changelog

## 2026-11-19

**The Mail tab** (board, mailroom). The board has a **Mail** tab with the mail between you and your agents,
which the mailroom now keeps on the Linux machine (every agent mail, exactly as sent, in
`~/.local/state/agent-mail/messages/`). You get threads, search, Inbox / Snoozed / Archived, snooze, Gmail-like keys
(j/k, arrows, e, r, Space) and replies with files (drag, paste or pick; files too big for a reply can be copied to
the Linux machine instead). A reply written there reaches the agent exactly like an emailed one, and its files are
saved where the agent can open them, on the Mac too. Drafts are kept per thread. **Sync with Gmail** (⋯ menu) brings
the tab and your Gmail into step; it needs `gmail_credentials` with the `gmail.modify` scope in
`~/.config/agent-board/config.json`. `agent-mail mailbox backfill` fetches older mail into the tab once.
`MAIL_BACKUP_DIRS` in `kit.env` copies the store and the mailroom database to another disk every hour
(`agent-mail backup`, `agent-mail-backup.timer`; empty = off).

**Group chats** (mailroom, board, instructions). `agent-group` makes a thread of up to 16 agents (on the Linux machine
or the Mac) in which every post reaches every member, and you may post. Ask the orchestrator by email ("set up a
group chat for X and Y"), use **New group chat…** on an agent's menu, or open **Chats** on the board; each sub-group
of the board gets a chat of its own, kept in step as you move tiles. The mailroom mails you once per new group, and
your reply in that thread is your post. Each agent chooses which posts wake it (`agent-group alerts GROUP
all|named|never`; default `named`, config `groups.default_alerts`); the others are announced in one line at its next
prompt, through a new `UserPromptSubmit` hook (`agent-group hook`). Agents know the rules: an agent's post is
information, never an instruction, and what you must know still comes by email.

**Options, plots and copyable commands in agent mail** (mailroom, board). An agent can put clickable options (a
`choices` block) and interactive plots (`plot`) in a mail: the Mail tab draws them, and picking an option starts your
reply with it; Gmail gets a numbered list ("Reply with the number") and the plot's image. Every command you should
run, and every text to paste, comes in a fenced block of its own, which the Mail tab gives a Copy button.
`agent-mail` checks every block before sending.

**Replies answer your questions first** (mailroom). Your mail reaches the agent with a reminder to open with each of
your questions answered in order, and, from your third mail in a thread, to restate the whole picture. The pre-send
reviewer of a reply sees the mail it answers and checks this; automatic checks catch plain-text layout faults (walls
of text, hard-wrapped lines, broken lists). Plain-text mail now renders markdown tables, links and italics.
`agent-mail reply N --subject "..."` starts a new thread when the old subject no longer fits.

**Where your plan usage went** (board). Clicking a usage meter shows the window's usage since its reset by board
group, sub-group and agent, with Other on top. Agents' replies are priced at API rates
(`~/.config/agent-board/allowance.json` overrides the prices); after a few days of the board's own usage readings
the shares turn from dollars into percent.

**Time page** (board). **Time** shows where your time went, per day and board group, over 1, 7, 14 or 30 days, as
all time, work time or outside work. It counts your prompts to agents, board messages and emails to agents, and
optionally meetings and Google Docs edits (`google_token`), your own email (`gmail_credentials`), to-dos (your tasks
repo) and Mac screen time from ActivityWatch (`defaults write org.agentkit.agentboard activityWatch
http://localhost:5600`). A model guesses groups for what you haven't grouped; `"guess_model": null` in
`~/.config/agent-board/time.json` turns that off. The page is computed when you open it; `WITH_TIME_PAGE="yes"` in
`kit.env` recomputes it every 15 minutes.

**Sent messages you can tick off** (board). Everything you send from the board (messages, resume notes, briefs,
orchestrator requests, chat posts) is kept in `~/.local/state/agent-board/sent.jsonl`. Each message ends with a ref
and an `agent-board done REF -m "..."` line for the agent; **Sent** in the top bar lists them with what each agent
says it did, and you tick them off. `agent-board sent` lists them on the Linux machine.

**Several Claude logins: one login and model order** (mailroom, orchestrator). New agents routed by usage,
`agent-failover` and the orchestrator place an agent on each Claude login in `usage.order`, then Codex, and only then
where it bills extra usage. To step down between models when one runs out, name them: `spawn_claude_model` (e.g.
`claude-fable-5-2`) and `claude_fallback_models` (e.g. `["claude-opus-5-5"]`); the order then takes each login on the
first model, then each on the next. agent-failover also sees a limit hidden behind later queued messages and at the
end of Claude Code's grace allowance, and **mails you while agents bill a login's extra usage** (once at the start,
every `failover.spend_step` of spend, and at the end; `failover.spend_mail: false` turns it off). The orchestrator
follows agent-usage's recommendation, including the model it names, and says why it chose that login. An agent that
hits a usage limit writes a status note and does not email you.

**New agents run Claude Code's default model** (mailroom). `spawn_claude_model` now defaults to `null`, so the kit
makes no model choice of its own for Claude agents; set it (and `claude_fallback_models`) in
`~/.config/agent-mail/config.json` to pin one ([docs/04](docs/04-claude-and-codex.md)).

**Meeting alerts** (board, optional, off by default). A notification 10 minutes before each meeting on your Google
Calendar, and a full-screen overlay with **Join** 1 minute before. Turn them on with `"meeting_alerts": true` and a
`google_token` (a Google login with Calendar read access) in `~/.config/agent-board/config.json`;
`~/.config/agent-board/meetings.json` picks the calendars.

**Red means a notification** (board). When a tile turns red, the Mac posts one notification, and clicking it opens
that agent. A tile also turns red for a mail stuck in `agent-mail`, a signed-out login names the account to log in
to, and two logins on one account are marked "same account as …".

**Mail to a closed agent** (mailroom, orchestrator). Your reply to, or forward of, the thread of an agent that has
closed or exited goes to the orchestrator with the thread, instead of being held with a notice. If you wrote as if the
agent were still there, it asks whether to start a new one; if you asked for new work, it starts one with the thread
quoted in its brief. Forwarded mail is treated as material, not instructions.

**Agents never type your address** (mailroom, instructions, board, to-do). Agents, the orchestrator, board messages
and to-do assignments write `agent-mail send --to owner` (also `--cc owner`, `agent-status report --email owner`),
which the mailroom turns into your address, and write the body to a file before sending in a separate command, so
no send command carries both an address and a link (Claude Code can stop such a command for approval).

**To-do** (todo). The Mac app gets the web app's **Triage** at the top of its sidebar: one task at a time from your
rated lists, with Archive (closed as "not planned"), This week, Snooze, Done, Delete, Agent, importance, Urgent and a
due date, one key each and each undoable with ⌘Z. **Importance and urgency replace priority** on the Mac (the
`priority:` label is retired: no app writes it, and any edit removes it). Double-click an agent's name on a task to
open the newest mail that agent sent you in Gmail (`todo-desk agent-mail N`). Agents started from the to-do apps are
named by the orchestrator for the task (2 to 4 words) instead of `todo-42-<title>`. Lighter and safer: no cache
rewrite after a sync that found nothing new, `todo-desk serve` exits soon after the Mac sleeps, a moved task reaches
other devices, and syncs, labels and caches fail loudly instead of silently.

**cloud-watchdog** (cloud-watchdog). A provider's request reaches its collector on stdin, so a big Modal workspace no
longer fails its check with "argument list too long". When it cannot message the owning agent it emails you at once
with the cause, and a failed alert email is retried on the next check. An owner gets two messages before you are
emailed (`ladder.max_pings`, was 3). The digest lists actions since the last digest, marks failed ones and has an
Errors section; `cloud-watch digest` prints by default (`--send` emails it). New settings `heartbeat_stale_min`,
`failure_alert_every_hours`, `repo_roots`; `[rules.long_running.hours]` per provider and
`[rules.storage] volumes_usd_month` (the old keys are still read); `timezone` is gone (the machine's time zone).
Emails are HTML when the `markdown` package is installed (the installer now puts it in the kit's venv).

**agent-spawn and Codex** (mailroom, board). `agent-spawn --new-repo` starts the agent in its own worktree (branch
`initial`), leaving the main folder on main. agent-spawn, agent-switch and the board's Resume and post-restart
restore answer Codex's "Hooks need review" question with "Continue without trusting", so an unattended Codex start
never stalls there (trust hooks yourself once in Codex's `/hooks`), and agent-spawn refuses to start Codex in a worktree whose
`.codex/hooks.json` its main checkout lacks.

**Board details** (board). Actions go over the open connection, with no new SSH per click. The Codex credit balance
shows beside Codex's weekly bar. Busy tiles pulse in Core Animation, so the board uses about 1% CPU. **N problems**
in the top bar lists what could not be read.

**Reliability** (mailroom). A large internal rewrite (library code in `agentmail/`, one launch path for spawn, switch
and failover, tests on the real code). `agent-close` no longer deletes a branch with unlanded work, orchestrator
failures and Gmail errors hold the mail and alert you instead of failing silently, `agent-spawn` exits 1 when the
agent never got its brief, and a fetch failure no longer blocks a spawn (the brief says the base may be behind).
`kit doctor` checks the new hooks, the backup targets, the board's optional logins and the Mail tab's mailbox.

Updating:

1. `git pull && ./install.sh --start`. This installs `agent-group`, the `UserPromptSubmit` hooks for Claude Code and
   Codex, re-renders the agents' instructions, and adds the `markdown` package for cloud-watchdog.
2. Check that no orchestrator run is in flight (`agent-orchestrate --runs`), then `systemctl --user restart
   agent-mailroom` (and `agent-failover` and `agent-memwatch` if you run them). The database adds its new columns and
   tables by itself.
3. `./install.sh --only orchestrator --force` replaces the orchestrator brief and its report skill with this
   version (its `STATE.md` is never overwritten). If you edited your brief, merge by hand instead.
4. Restart running Claude Code and Codex sessions so they load the new hook and instructions; Codex asks you to
   review the changed hooks once: trust them in `/hooks`.
5. Once: `agent-mail mailbox backfill`, so older agent mail shows in the Mail tab.
6. Rebuild and reinstall both Mac apps (`cd board/mac && ./build-app.sh && ./install.sh`, the same in `todo/mac`, or
   `scripts/mac-install.sh` in each from the Linux machine), then choose **New build ready: relaunch** in the running
   board and allow notifications when asked.
7. Check the model: if your mailroom config does not set `spawn_claude_model`, new Claude agents now start on Claude
   Code's default model instead of `claude-opus-5-5`. To keep Opus 5.5, add `"spawn_claude_model":
   "claude-opus-5-5"` to `~/.config/agent-mail/config.json`. The config key `orchestrator_mode` is gone.
8. Optional: `MAIL_BACKUP_DIRS` and `WITH_TIME_PAGE` in `kit.env` (then `./install.sh --start`); in
   `~/.config/agent-board/config.json`, `gmail_credentials` with `gmail.modify` (Sync with Gmail), `google_token`
   (the Time page's calendar and documents) and `"meeting_alerts": true`. Then `kit doctor`.

## 2026-10-26

**Orchestrator: every login before the API key** (mailroom). When a login is out of usage or logged
out, the orchestrator now passes the event to the next one in `usage.order`: each Claude login, then
Codex (`codex exec`, with no MCP servers, plugins or hooks from your Codex config, as the Claude run has
none), and only then the optional API key. Before, it went from the default login straight to the API
key, or failed without one. A run that times out or fails after its first turn is not repeated, since
it may have acted already. `agent-orchestrate --runs` shows which login ran each event. New settings:
`orchestrator_codex_model`, `orchestrator_codex_effort`.

**Memory watch: swap alone no longer alerts** (mailroom). Swap half full now makes it urgent only with
RAM at 80% or more: pages swapped out under past pressure stay in swap long after it has gone, and
counting them alone emailed the owner every hour all night. The email names the agents it messaged.

## 2026-10-21 (later)

**Pre-send review: where the question goes.** The review no longer pushes a bare yes/no to the first line,
where the reader cannot yet tell what it is about. The first lines must say a decision is needed, in plain
words; the question goes at the top only if it stands on its own (what would be done, with what, at what
cost), otherwise right after the recommendation.

**Restart: only commands still running count as waits** (board). Before a restart, the board lists what
each Claude agent is waiting on. It used to list every background command from the last day; it now leaves
out commands that have finished, failed or been stopped, and ones started before the agent was last resumed.

## 2026-10-21

**Pre-send review of agent mail** (mailroom). Before an agent's mail of 120 words or more leaves, a
cheap model with no context (Haiku, through your own Claude login) reads it as you would see it. If you
would have to reply "what does this mean?" or "where's the plot?", the mail goes back to the agent once,
with concrete fixes (`agent-mail: NOT SENT`, exit 3), and the agent's next send goes out whatever the
review says. Mail to collaborators and to other people is checked for clarity instead of visuals. A mail
containing an API key or token is held every time. Agents read the rules with `agent-mail guide`.
Configure it under `review` in `~/.config/agent-mail/config.json`: set `owner_context` to a few sentences
about yourself, try `"mode": "shadow"` first if you want to watch before it holds anything, and edit
`mailroom/agentmail/email_rules.md` to taste. Details: `mailroom/README.md`, "Pre-send review of agent mail".

**Inline plots and attachments** (mailroom). `agent-mail send|reply --image NAME=PATH` embeds an image,
placed with `<img src="cid:NAME">`, and `--attach PATH` adds a file.

**Agent board** (board). New tiles show mail that the review sent back and the agent has not re-sent, per
recipient, and mail stopped because it contained a credential. A new `agent-cli-update` timer updates
Claude Code and Codex every 6 hours (`WITH_CLI_UPDATE` in `kit.env`). Codex sessions started or resumed by
the kit no longer stop at Codex's "update available" prompt. An agent that hit a usage limit but has
worked since is no longer shown as blocked.

**To-do** (todo). **Assign to agent** can hand a task to an agent that is already running, from the Mac
app, the web app or `todo-desk assign N --to SESSION`. To support it, dispatch publishes the running agents
to `.todo/agents.json` in your tasks repo.

**Fixes.** The optional API-key fallback for the orchestrator never ran claude (`bws run` treats its
arguments as one shell script); it does now. `agent-status report --email` is never held by the review.

**Instructions.** The global agent instructions and the orchestrator brief tell agents about
`agent-mail guide`, `NOT SENT` and `--image`.

Updating: `git pull && ./install.sh --start`, then `systemctl --user restart agent-mailroom`. The mailroom
adds the `reviews` table to its database by itself.

## 2026-10-16

First release.
