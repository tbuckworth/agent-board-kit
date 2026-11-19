"""The library the commands in bin/ share. README.md documents the commands.

Modules:
  accounts: Claude logins (config `usage.claude_accounts`): lookup, readiness, owner rules, env, a new login's setup
  alerts: Mailroom alerts to the owner: plain, untagged, deduplicated mail
  backup: agent-mail backup: the message store and the database copied to config backup.targets
  claude_session: A Claude Code session's transcript, process, pane and background jobs; moving it to another login
  cli: Stopping a script with its name and a message; refusing to act as the owner inside an agent session
  close: agent-close: ending an agent's session and removing its worktree once its work is landed
  commands: Running a command, the tmux helper, a repo's main checkout, an option's value in a command line
  compose: Plain text to HTML for outgoing mail, and HTML back to text (a mail's plain part, or its words at a glance)
  config: Paths, `DEFAULTS`, and `config.json` merged over them
  deliver: Handing a text to a session through agent-send, and the delivery receipt agent-send writes
  failover: agent-failover: spotting a limit, deciding where an agent goes, moving it; the extra-usage spend mail
  gmail: The Gmail API layer
  groupcmd: agent-group's commands on the central mailroom, the Mac's proxy to them, delivering a post, the notices
  groupmail: Groups and email: the mail to the owner for each new group, their reply as their post, their mail asking for one
  groups: Groups as append-only envelope records: ids, envelopes, the fold, which posts alert whom, the store, notice flags
  headless: A `claude -p` run (orchestrator, reviewer) and the logins to run it on
  jsonfile: Atomic JSON, text and byte writes, durable on request; JSON-lines logs and their tails
  launch: Starting Claude Code or Codex in a tmux pane: the launch line, Codex's start, the brief's preamble
  listing: What agent-mail prints of sent mail, inbound mail and a proxied send's receipt
  mailbox: The owner's mailbox for the board's Mail tab: threads, their flags and the rules, list, mark, sync, answer, show, part, summary, backfill
  mailroom: One inbound mail, for the daemon and agent-mail: holds, its session, orchestrator context and thread, delivery, state and labels
  mailview: The Mail tab's view of a stored mail: its parts, its HTML sanitized, a whole thread as one HTML page
  memcap: Per-agent memory caps in systemd scopes
  memwatch: Who holds the memory, and when to warn
  messages: The message store: the MIME bytes of every agent mail and of the owner's Mail-tab replies with files, by sha256
  orchestrate: One orchestrator event: the lock, the run on each login in turn, the run log
  outbound: agent-mail's sending: who --to and --cc name, the MIME message, kept then sent, send and reply after the review, held drafts
  piped: Reading piped or redirected input without waiting on one that never ends
  procs: The process tree from /proc
  recovery: The bounded, read-only ownership search for an unowned reply
  registry: The address book: both CLIs' session registries, and finding a session by name or id
  remote: The authenticated SSH transport between machines
  replyfiles: The files in the owner's Mail-tab replies: safe names, read back from the store, written where the agent opens them
  report: Assembling and rendering the status report
  review: The pre-send review
  reviewgate: Codex and a repo's own .codex/hooks.json: a worktree whose hooks its main checkout lacks
  routing: Reply tags, quote stripping, matching a mail to a session, and the ids of a reply written in the Mail tab
  rpc: agent-mail between machines: the Mac's proxied commands and the desktop's answers
  send: agent-send's delivery: the `[from …]` line, the inbox socket, `codex queue`, resuming an offline session
  statuslog: The status log: one JSON line per event
  store: The SQLite routing map and the review records
  switch: agent-switch: moving a live agent to another CLI, login, model or effort
  trust: Pre-trusting a folder for both CLIs
  usage: Plan usage left on each account, and which one a new agent should use
  watch: The loop, options and log line agent-failover and agent-memwatch share
  widgets: Clickable options and interactive plots in agent mail: the blocks checked, and what Gmail gets instead
  worktree: A git worktree for a new agent: fetch, base, add, remove

Data files:
  email_rules.md: The owner's mail rules, which `agent-mail guide` prints and the reviewer applies
  review_prompt.md: The reviewer's prompt for mail to the owner
  review_prompt_outside.md: The reviewer's prompt for mail to anyone else
"""
__version__ = "0.1.0"
