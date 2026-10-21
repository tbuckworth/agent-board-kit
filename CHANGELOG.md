# Changelog

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
