"""agent-board: a read-mostly status collector for the owner's Claude Code and Codex agents.

classify     pure rules: state, colour, display name, waiting-for-reply (unit-tested with fixtures)
sources      cheap readers for the session registries, status log, mailroom store, metrics, usage; shared helpers
config       where the mailroom and its config are (the owner's name and address); the board's own optional settings
cli          what the bin/ commands share: their argument parser, and printing a result as JSON or for people
collect      the cached collector that assembles one JSON snapshot
actions      the few validated commands the Mac app may run (close, force close, send, resume, switch, ...)
relaunch     starting an exited agent again: its login, model and effort, resume line, tmux session and memory cap
groups       the board's tile groups, as the Mac sends them, and each tile's latest group
sent         what the owner sends from the board, and whether each message has been dealt with
chats        group chats between the owner and the agents, read and written as the owner through the mailroom's
             agent-group
subchats     a group chat for each sub-group of the board's layout, kept in step with it
gmailstate   the agents' mail in the owner's Gmail (optional): whether it has been read, and the Mail tab's sync
             archiving or marking it read
mailbox      the Mail tab: mail between the owner and the agents, read and changed as the owner through agent-mail
             mailbox
meetings     the owner's upcoming meetings on their Google calendars, for the Mac app's meeting alerts (optional)
google       a small Google API client for a refresh-token login, and where the owner's Google logins are
webapi       one JSON request to a web API: what the Google client sends its requests through
credits      Codex credits: what Codex spends once its plan's weekly allowance is used up
cliupdate    keeps the agents' CLIs current (agent-cli-update.timer)
restart      restarts or updates the desktop without losing the agents
timesources  the Time page's sources: what the owner did, and when
timeuse      the Time page: active minutes per day and board group (agent-board time)
screen       the Mac's screen time, as the Mac app uploads it, for the Time page
allowance    where a plan window's allowance has gone since it last reset: each agent's share, and other; the usage
             readings it fits
"""
