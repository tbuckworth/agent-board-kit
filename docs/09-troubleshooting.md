# 9. Troubleshooting

Start with `kit doctor --online`: it checks most of what follows and prints the fix.

## Mail

**My reply never reached the agent.**
1. `agent-mail inbox --held`: is it held? `agent-mail show N` says why (unknown sender, DKIM fail,
   no matching session, delivery failed).
2. Was it sent from `OWNER_EMAIL`? Mail from another address of yours is held. Add it to
   `allowlist` in `~/.config/agent-mail/config.json` if you want it trusted.
3. Is the daemon running? `systemctl --user status agent-mailroom`,
   `journalctl --user -u agent-mailroom -f -o cat`, `agent-mail status`.
4. Is the session alive? `agent-send --list`. If it exited, the mailroom resumes it
   (`resume_offline: true`) in the background; the reply is then in that resumed session
   (`claude --resume <id>` to look, or the board's Resume).
5. Delivered but ignored: the session was started before `"crossSessionInbound": "accept"` was set.
   Restart it.

**`invalid_grant` / "Token has been expired or revoked".** The OAuth app is still in "Testing" (7-day
tokens), or access was revoked. Publish the app ([02-agent-gmail.md](02-agent-gmail.md), step 2.5),
then run `agent-mail-auth` again and `systemctl --user restart agent-mailroom`.

**"Refusing to save: you authorised X".** You signed in with the wrong Google account during
consent. Run `agent-mail-auth` again and pick the agents' account (use a private window if the
browser keeps choosing another login).

**Agent mail lands in my spam.** Add a filter in your own mailbox for the agents' address with
"Never send it to Spam", and mark one as "Not spam".

**DKIM failures for my own mail.** Your provider does not sign with DKIM, or a forwarding hop breaks
it. Send from the provider directly. `require_dkim: false` turns the check off: then anyone who can
forge your `From:` can instruct your agents. Not recommended.

**A reply went to the orchestrator instead of the agent.** It was a new thread or the subject's
`[#tag]` was removed. Reply in the agent's own thread. The orchestrator relays it if the
instruction is clear.

## Agents

**`agent-spawn` fails with "tmux: command not found" or "claude not found" from cron or the
orchestrator.** The CLIs are found in `~/.local/bin`, `/opt/homebrew/bin`, then via a login shell.
Install them with their standard installers or symlink them into `~/.local/bin`.

**A Codex session never shows a queued message.** Update Codex (0.149+ for `codex queue`). If it
still does not render, start the shared daemon (`codex app-server daemon start`) and relaunch the
session. A Codex rate-limit dialog also blocks queued messages until dismissed.

**An agent was killed with no message.** Its memory cap (`agent_memory_max`, default 32G) was hit.
`journalctl --user -u 'agent-*.scope'` shows the OOM kill. Relaunch with
`agent-spawn ... --memory-max 64G`, or raise a running agent's cap:
`systemctl --user set-property --runtime agent-NAME.scope MemoryMax=64G`.

**Everything died when I restarted the mailroom.** The tmux server was started from inside the
mailroom's cgroup (e.g. the first spawn came from the orchestrator) and was killed with it. The
kit moves tmux to `tmux-server.scope` on the next spawn or resume. To be safe, restart the
mailroom only when `agent-orchestrate --runs` shows nothing in flight.

**User services stop when I log out.** `loginctl enable-linger $USER`.

**`agent-close` refuses.** It found uncommitted, untracked or unpushed work. `agent-close NAME --dry-run`
lists it. Land it, or `--force` to discard.

## Orchestrator

**It never replies.** `agent-orchestrate --runs` shows each run's outcome and cost;
`~/.local/state/agent-mail/orchestrator-runs.jsonl` has the detail. Run one by hand:
`agent-orchestrate "test: reply with the word ready"`. Common causes: Claude Code not logged in for
this user, usage limit reached, `ORCHESTRATOR_DIR` missing (`./install.sh --only orchestrator`).

**It keeps doing something wrong.** Edit `ORCHESTRATOR_DIR/AGENTS.md` (it is your copy; the
installer will not overwrite it without `--force`) and commit.

## Board and to-do (Mac)

**The board says "no answer from the desktop".** From the Mac, `ssh <alias> ~/.local/bin/agent-board
snapshot --pretty | head` must work without a password prompt. Check the alias in `~/.ssh/config`,
your key, and that `~/.local/bin/agent-board` exists on the Linux machine.

**The apps were built elsewhere and macOS refuses to open them.** Build them on the Mac itself
(see [07-mac.md](07-mac.md)), or right-click → Open once.

**The to-do web app says "Bad credentials" or 404.** The fine-grained token must have access to the
tasks repo with Issues read/write and Contents read/write. Re-create it and paste it in Settings.

## Getting more detail

| What | Where |
|---|---|
| mailroom log | `journalctl --user -u agent-mailroom -o cat` |
| routing database | `~/.local/state/agent-mail/mailroom.sqlite` (`agent-mail status`, `inbox`, `sent`) |
| status events | `~/.local/state/agent-status/events.jsonl` (`agent-status log`) |
| orchestrator runs | `~/.local/state/agent-mail/orchestrator-runs.jsonl` |
| board actions | `~/.local/state/agent-board/actions.jsonl` |
| to-do helper | `~/.local/state/todo-desk/` |
