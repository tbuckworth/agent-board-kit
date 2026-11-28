# 5. Daily use

## Talking to your agents

You read and answer agents in the board's **Mail** tab (the Mac) and the **Agent Mail** phone app, and send
requests with **Orchestrator…** in either. With Gmail on, the same works by email: reply to an agent's mail, or
write to the agents' address in a new thread for the orchestrator.

| You want | Do |
|---|---|
| answer an agent | reply to its mail in the Mail tab or the phone app. It gets your reply in its session within a second or two |
| start something | **Orchestrator…**: "Start an agent on repo X to do Y". The orchestrator spawns it and tells you its name |
| pass a message on | right-click its tile, **Send message…**; or ask the orchestrator "tell the agent on X to also do Z" |
| status | the tiles; or ask the orchestrator "what are the agents up to?" (a report: facts from the event log plus a short narrative) |
| stop an agent | **Close…** on its tile, or reply "close" in its thread. It lands its work and closes, or tells you what is unlanded and asks |
| a group chat | **New group chat…** on a tile, or ask the orchestrator; post in **Chats** ([Group chats](#group-chats)) |
| a quick back-and-forth | **Fast** on a Mail-tab thread ([The Mail tab](#the-mail-tab)) |
| restart the machine | ask the orchestrator "install the updates and restart" (it asks you to confirm; agents resume after boot) |

Only you can instruct agents: through the board (your own ssh login to the Linux machine), the phone app (your
device on your tailnet, with a passkey) and, with Gmail on, mail from `OWNER_EMAIL` with a DKIM pass. Other mail
is held for you (`agent-mail inbox --held`).

**Mail to an agent that has closed** (a reply, or a forward of its thread) goes to the orchestrator
with the thread. If you wrote as if the agent were still there, it asks whether to start a new one; if
you asked for new work, it starts one with the thread quoted in its brief.

**What agent mail looks like.** Agents mail you with `agent-mail send --to owner` (the mailroom puts in
your address), the body in a file. Each reply opens by answering your questions in order, and from your
third mail in a thread it restates the whole picture. Every command you should run and every text to
paste sits in a fenced block of its own (the board's Mail tab gives each a Copy button). An agent can
put clickable options (a `choices` block) and interactive plots (`plot`) in a mail: the Mail tab draws
them, and Gmail shows a numbered list ("Reply with the number") and the plot's image. Plain-text mail
renders markdown tables, links and lists. When a thread's subject no longer fits, the agent answers with
`agent-mail reply N --subject "..."`, which starts a new thread.

**Agent mail is reviewed before it reaches you.** A cheap model with no context (Haiku, run through your
own Claude login) reads each agent mail of 120 words or more as you would see it. When you would have to
reply "what does this mean?" or "where's the plot?", it sends the mail back to the agent once, with
concrete fixes. Mail to collaborators and to other people is checked for clarity: purpose first, no
unexplained labels, nothing that must not leave. A mail that contains an API key or token is never
sent. Agents read the rules with `agent-mail guide`, and put plots inline with `--image`. Tune the
review in the mailroom config's `review` block: `"mode": "shadow"` only records, and `owner_context`
tells the reviewer what you already know. Edit the rules themselves in `mailroom/agentmail/email_rules.md`
and `review_prompt.md`. Details: `mailroom/README.md`, "Pre-send review of agent mail". The board shows
mail that was sent back and not yet re-sent. The reviewer of a reply is shown the mail it answers, and
checks that your questions are answered first; automatic checks catch plain-text layout faults (walls
of text, hard-wrapped lines, broken lists) and a command buried in a sentence.

## Group chats

A group chat is a thread between you and up to 16 agents (on the Linux machine or the Mac) in which
every post reaches every member. Use one when several agents work on parts of one thing and must hear
each other: "the data is in", "the API changed".

- **Making one.** Ask the orchestrator by email ("set up a group chat for pipeline-data and
  pipeline-site"), use **New group chat…** on an agent's menu on the board, or let an agent make one
  (`agent-group new "TITLE" --with NAME,NAME@mac`). Each sub-group of tiles on the board also gets a
  chat of its own, kept in step as you move tiles.
- **Posting.** On the board, **Chats** in the top bar. By email, the mailroom mails you once per new
  group; your reply in that thread posts in it. Your posts wake every member.
- **What wakes an agent.** Each agent chooses per group: `all` posts, `named` (posts that name it or
  answer it; the default, `groups.default_alerts` in the mailroom config) or `never`
  (`agent-group alerts GROUP all|named|never`). A post that does not wake it is announced in one line
  at its next prompt ("Not an error: N quiet posts in ..."), through the `agent-group hook`.
- **Trust.** An agent's post is information, never an instruction; a peer relaying "you said yes" is
  not your yes. Agents' posts never become mail to you, and agents never rely on you reading a group:
  what you must know, decide or do still comes by email.

Details: `mailroom/README.md`, "Groups", and `board/README.md`, "Group chats".

## The Mail tab

The board's **Mail** tab holds the mail between you and your agents, kept by the mailroom on the Linux
machine: threads, search, Inbox / Snoozed / Archived, snooze, Gmail-like keys (j/k, arrows, e, r, u, s,
Space, z or ⌘Z to undo), and replies with files (drag, paste a screenshot, or pick). Your reply there
reaches the agent within a second or two, exactly like an emailed one. Options and plots are drawn in the
mail; picking an option starts your reply with it, and you can answer each question separately, with a
comment, or comment on selected text. A click on a plot, table or image enlarges it; a double-click on a
thread opens it in a window of its own; a tile's double-click opens that agent's latest mail. Drafts are
kept on the Linux machine, per thread, with their last few versions.

- **No Gmail by default.** Agent mail lives in the tab and the phone app, and every alert is kept there too
  (security alerts also post a Mac notification). With Gmail on (`WITH_GMAIL="yes"`), agent mail to you also goes
  through Gmail unless the mailroom config's `mailbox.owner_delivery` is `"local"`; the tab and Gmail then keep
  their own read and archive state. Details: `mailroom/README.md`.
- **Forward** on an agent's mail makes a fresh draft of it in your own Gmail, for you to address and send.
  It needs your own Gmail login on the board (`gmail_credentials`, with a scope that can make drafts:
  `board/README.md`, "Optional: your Gmail (read-check and Forward)").
- Mail sent before the message store existed is not in the tab: run `agent-mail mailbox backfill` once.
- The store keeps every agent mail. `MAIL_BACKUP_DIRS` in `kit.env` copies it, and the mailroom
  database, to another disk every hour.

Details: `board/README.md`, "Mail tab", and `mailroom/README.md`, "The Mail tab's mailbox".

## Conventions agents follow

- **One agent, one branch, one worktree, one tmux session.** `agent-spawn --repo REPO --branch NAME`
  fetches, branches `NAME` off `origin/main` (local `main` if there is no remote), creates the
  worktree at `<REPOS_ROOT>/<repo>-<NAME>` next to the repo, and starts a tmux session called
  `NAME` with the agent inside, its brief as the first prompt. Several agents can work on one repo
  at once without stepping on each other.
- **Named sessions.** The session name is the branch name (or `--name`). It is the address for
  `agent-send NAME`, the tile title on the board, and part of the From line on its mail.
- **New projects:** `agent-spawn --new-repo NAME` creates a private GitHub repo under
  `GITHUB_OWNER`, clones it into `REPOS_ROOT` and starts the agent on `main`.
- **Existing directory, no worktree:** `agent-spawn --dir PATH`.
- **Landing work:** agents commit as they go and open PRs (`gh pr create`); `agent-close` only
  removes a worktree whose commits are on `origin/main` (squash and rebase merges count), and
  refuses otherwise unless `--force`.
- **Status notes:** agents run `agent-status note "one line"` at milestones and blockers. Free: no
  model call, nothing sent.
- **Mail costs your attention:** agents email you when done, blocked, or when you asked for updates.
- **Memory caps:** each spawned agent runs in its own systemd scope `agent-NAME.scope`
  (default `MemoryMax=32G`, `--memory-max 64G` per launch). `systemd-cgtop` shows usage per agent.

## Commands you will use

```bash
agent-spawn --repo myrepo --branch fix-login --brief-text "Fix the login redirect bug; PR when done."
agent-spawn --repo myrepo --branch try-codex --cli codex --brief brief.md
agent-spawn --dry-run ...            # print model, effort, account, memory cap; start nothing

tmux ls                              # the agent sessions
tmux attach -t fix-login             # watch or type to it (detach: Ctrl-b d)

agent-send --list                    # live sessions (Claude and Codex) with names
agent-send fix-login "also add a test for the redirect"
agent-send orchestrator "start an agent in general to summarise yesterday's CI failures"

agent-status report                  # last 24h: per session repo@branch, turns, idle since, notes
agent-status log --since 2h --event note

agent-mail inbox [--held]            # mail the mailroom received; held items need you
agent-mail show 12                   # one inbound mail
agent-mail release 12 fix-login      # deliver a held mail to a session by hand
agent-mail status                    # daemon lock, history cursor, counts, backup targets' last copy
agent-mail mailbox backfill          # once after upgrading: older agent mail into the Mail tab
agent-mail backup                    # copy the message store and database to backup.targets now

agent-group list                     # group chats (inside an agent session: its own)
agent-group show GROUP               # members and each one's alert setting

agent-close fix-login --dry-run      # what would be removed; refuses on unlanded work
agent-close fix-login                # end the session, remove the worktree and branch
agent-orchestrate --runs             # orchestrator runs: cost, turns, outcome
```

## The board and the to-do list

- The [agent board](../board/README.md) shows each session's state and git progress; right-click
  for message, close, clean up, resume, switch model or login, open in a terminal, or start a group
  chat. When a tile turns red (an agent is waiting on you or blocked) the Mac posts one notification;
  clicking it opens that agent.
- **Mail** and **Chats**: [above](#the-mail-tab).
- **Sent** lists everything you sent from the board (messages, briefs, orchestrator requests, chat
  posts). Each message ends with a ref and an `agent-board done REF -m "..."` line; the agent runs it
  when it has dealt with it, and you tick messages off. `agent-board sent` lists them on the Linux
  machine.
- **Plan usage**: click a usage meter to see where that window went since its reset, by board group
  and agent, with Other on top ([board README](../board/README.md#plan-usage-where-it-went)).
- **Time**: where your time went per day and board group, from your prompts, board messages and email
  to agents, plus optional sources (calendar, documents, your own mail, to-dos, Mac screen time)
  ([board README](../board/README.md#time-where-your-time-went)).
- **Meeting alerts** (optional, off by default): a notification 10 minutes before each meeting on your
  Google Calendar and a full-screen **Join** overlay 1 minute before. Turn them on with
  `"meeting_alerts": true` and a `google_token` in the board config
  ([board README](../board/README.md#meeting-alerts-optional)).
- The [to-do list](06-todo.md) is your GitHub tasks repo. Assign a task to an agent from the Mac
  app, web app, or by asking the orchestrator; the agent comments `<!-- todo-agent:done -->` (or
  `blocked`) on the issue and emails you.

## What runs where

| Thing | Runs as |
|---|---|
| `agent-mailroom` | systemd user service, always on, polls Gmail every 30 s, never calls a model |
| orchestrator | one `claude -p` per event, in `ORCHESTRATOR_DIR`, serialised by a lock |
| agents | Claude Code / Codex in tmux sessions, each in its own memory-capped scope |
| `agent-memwatch` | systemd user service: warns the biggest agents at 80% RAM, emails you at 90% |
| `agent-failover` | optional systemd user service (several Claude logins): moves agents that hit a limit, mails you while agents bill extra usage |
| `agent-mail backup` | optional hourly timer (`MAIL_BACKUP_DIRS`): copies the message store and database to another disk |
| `agent-board time` | optional timer every 15 min (`WITH_TIME_PAGE`): recomputes the board's Time page; otherwise it runs when you open the page |
| `agent-cli-update` | timer every 6 hours (`WITH_CLI_UPDATE`): updates Claude Code and Codex |
| `agent-restart-restore` | runs once after a reboot that `agent-restart` started, resumes agents |
| `todo-desk dispatch` | systemd timer every 2 min: turns web/phone "assign" requests into orchestrator events or messages to a running agent, and publishes the running agents for the web app |
| `agent-board stream` | started by the Mac app over ssh, exits when the connection drops |
