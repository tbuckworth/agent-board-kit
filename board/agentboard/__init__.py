"""agent-board: a read-mostly status collector for the owner's Claude Code and Codex agents.

classify     pure rules: state, colour, display name, waiting-for-reply (unit-tested with fixtures)
sources      cheap readers for the session registries, status log, mailroom store, metrics, usage; shared helpers
config       where the mailroom and its config are (the owner's name and address); the board's own optional settings
cli          what the bin/ commands share: their argument parser, and printing a result as JSON or for people
collect      the cached collector that assembles one JSON snapshot
actions      the few validated commands the Mac app may run (close, force close, send, resume, switch, ...)
relaunch     starting an exited agent again: its login, model and effort, resume line, tmux session and memory cap;
             a claude's env
groups       the board's tile groups, as the Mac sends them, and each tile's latest group
colours      the groups' colours (colours.json): each group's, its sub-groups' shades and Ungrouped's
sent         what the owner sends from the board, and whether each message has been dealt with
chats        group chats between the owner and the agents, read and written as the owner through the mailroom's
             agent-group
subchats     a group chat for each sub-group of the board's layout, kept in step with it
gmailstate   the agents' mail in the owner's Gmail (optional): whether it has been read, and the drafts of the Mail
             tab's forwards
mailbox      the Mail tab: mail between the owner and the agents, read and changed as the owner through agent-mail
             mailbox
mailforward  forwarding an agent's mail from the Mail tab: a fresh mail of it, made a draft in the owner's Gmail
maildrafts   the owner's unsent replies, kept on the desktop and shared by the Mac app's windows and the phone, with
             recent versions
mailweb      the Mail tab on the owner's phone: a page served to the owner's tailnet alone (agent-board mail-web)
mailwebstate the phone page's settings, enrolled passkeys, sessions and enrolment code, and its tailnet address
webauthn     passkeys (WebAuthn) for a page: checking one when enrolled and each signature with it (needs cryptography)
lockdown     hardening the phone page's server, and readying the process that reads the owner's desktop password:
             hardened, and on the system's Python in isolated mode
keyring      the desktop's keyrings (optional): whether the browser's is locked, and unlocking them with the owner's
             password (keyring-status, keyring-unlock)
meetings     the owner's upcoming meetings on their Google calendars, for the Mac app's meeting alerts (optional)
google       a small Google API client for a refresh-token login, and where the owner's Google logins are
webapi       one JSON request to a web API: what the Google client sends its requests through
credits      Codex credits: what Codex spends once its plan's weekly allowance is used up
cliupdate    keeps the agents' CLIs current (agent-cli-update.timer), and has fast mode's keys tried on each new version
restart      restarts or updates the desktop without losing the agents
timesources  the Time page's sources: what the owner did, and when
timeuse      the Time page: active minutes per day and board group (agent-board time)
screen       the Mac's screen time, as the Mac app uploads it, for the Time page
allowance    where a plan window's allowance has gone since it last reset: each agent's share, and other; the usage
             readings it fits; a per-token login's weekly spend
transcripts  the archive of every agent transcript (optional), its catalog by board group and the by-group tree
voicedesk    the voice desk: agents asked through a read-only fork, and the agent behind the Mac's front window
voiceweb     the voice page (optional): Gemini Live in the owner's browser, with voicedesk's tools (mail-web's /voice)
gemini       Gemini Live's ephemeral tokens: the one request with the Gemini API key, read from where voice.json says
speech       the desktop's voice (optional): Kokoro's daemon on a Unix socket, its client, and an answer spoken as it is
             written
desk         the desk (optional): one always-running Claude the owner talks to, its own tools (an MCP server) and its
             send rule
"""
