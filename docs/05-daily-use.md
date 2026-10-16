# 5. Daily use

## Talking to your agents by email

| You want | Do |
|---|---|
| answer an agent | reply to its mail, from any device. It gets your reply in its session within ~30 s |
| start something | email the agents' address (new thread): "Start an agent on repo X to do Y". The orchestrator spawns it and replies with its name |
| pass a message on | "Tell the agent on X to also do Z" |
| status | "What are the agents up to?" (an HTML report: facts from the event log plus a short narrative) |
| stop an agent | reply "close" in its thread. It lands its work and closes, or tells you what is unlanded and asks |
| restart the machine | "Install the updates and restart" (asks you to confirm; agents resume after boot) |

Only mail from `OWNER_EMAIL` with a DKIM pass is acted on. Everything else is held for you
(`agent-mail inbox --held`).

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
agent-mail status                    # daemon lock, history cursor, counts

agent-close fix-login --dry-run      # what would be removed; refuses on unlanded work
agent-close fix-login                # end the session, remove the worktree and branch
agent-orchestrate --runs             # orchestrator runs: cost, turns, outcome
```

## The board and the to-do list

- The [agent board](../board/README.md) shows each session's state and git progress; right-click
  for message, close, clean up, resume, switch model or login, open in a terminal.
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
| `agent-restart-restore` | runs once after a reboot that `agent-restart` started, resumes agents |
| `todo-desk dispatch` | systemd timer every 2 min: turns web/phone "assign" requests into orchestrator events |
| `agent-board stream` | started by the Mac app over ssh, exits when the connection drops |
