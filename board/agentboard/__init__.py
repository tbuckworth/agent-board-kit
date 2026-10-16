"""agent-board: a read-mostly status collector for the owner's Claude Code and Codex agents.

classify  pure rules: state, colour, display name, waiting-for-reply (unit-tested with fixtures)
sources   cheap readers for the session registries, status log, mailroom store, metrics, usage
collect   the cached collector that assembles one JSON snapshot
actions   the few validated commands the Mac app may run (close, force close, send, resume, switch, ...)
restart   restart or update-and-restart the desktop and resume the agents after boot
config    where the mailroom and its config are; the board's own optional settings
gmailread the optional "has the owner read the agent's mail" check
"""
SNAPSHOT_VERSION = 1
