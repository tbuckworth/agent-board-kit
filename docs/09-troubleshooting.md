# 9. Troubleshooting

Start with `kit doctor --online`: it checks most of what follows and prints the fix.

## Without Gmail (the default)

**A reply in the Mail tab or the phone app never reached the agent.** `agent-mail status` on the Linux machine says
whether the mailroom is running and what is waiting. The agent's session must have started after the install (it
reads hooks and instructions at launch): restart it. If the agent has closed, the reply went to the orchestrator.

**An agent says `agent-mail` refused a mail (exit 5).** With Gmail off, agent mail goes only to you. The agent
should tell you what it would have sent; turn Gmail on ([02-agent-gmail.md](02-agent-gmail.md)) if you want agents
to mail other people.

**`agent-mail status` lists settings under `ignored`.** They only work with Gmail on (`nudge_minutes`,
`gmail_alerts`, `collaborators`, ...): remove them from `~/.config/agent-mail/config.json`, or turn Gmail on.

**The phone app does not load.** `kit doctor` names the step that is missing: the phone not listed in
`~/.config/agent-board/mailweb.json`, the service not running (`journalctl --user -u agent-board-mail-web -n 30`),
or `tailscale serve` not publishing its socket ([07-mac.md](07-mac.md), "The phone app"). It refuses to answer
while Tailscale Funnel exposes its port (`tailscale funnel status`).

**No Mac notification for a security alert.** System Settings → Notifications → AgentBoard: allow. The alert is in
the Mail tab either way.

## Mail (with Gmail on)

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
`[#tag]` was removed, or the agent has closed (the orchestrator then offers a new one). Reply in the
agent's own thread. The orchestrator relays it if the instruction is clear.

**`agent-mail backup` refuses: "backup.targets is empty".** Set `MAIL_BACKUP_DIRS` in `kit.env` and
run `./install.sh --start`. A target whose parent folder is missing (a disk not mounted) fails that
run; `agent-mail status` shows each target's last copy.

**The Mail tab shows older mail as text only, or not at all.** Mail sent before the message store
existed is fetched once with `agent-mail mailbox backfill`.

**A group post never reached an agent.** `agent-group show GROUP` gives each member's alert setting
(`named` agents wake only for posts that name them). Quiet posts are announced at the agent's next
prompt by the `UserPromptSubmit` hook: `kit doctor` checks it is installed, and a session started
before it was must be restarted. Each delivery is logged in
`~/.local/state/agent-mail/groups/deliveries.jsonl`.

## Agents

**`agent-spawn` fails with "tmux: command not found" or "claude not found" from cron or the
orchestrator.** The CLIs are found in `~/.local/bin`, `/opt/homebrew/bin`, then via a login shell.
Install them with their standard installers or symlink them into `~/.local/bin`.

**A Codex session never shows a queued message.** Update Codex (0.149+ for `codex queue`). If it
still does not render, start the shared daemon (`codex app-server daemon start`) and relaunch the
session. A Codex rate-limit dialog also blocks queued messages until dismissed.

**A Codex agent started by the kit runs without its hooks.** Codex asked "Hooks need review" and the
unattended start answered "Continue without trusting". Trust the hooks once in a Codex session you
start yourself (`/hooks`), then resume the agent.

**`agent-spawn` refuses Codex in a worktree.** The worktree has a `.codex/hooks.json` that the repo's
main checkout lacks, so Codex would run different hooks there. Commit it to main first, or start the
agent on Claude.

**Spawned agents stop at once with a model error.** `spawn_claude_model` (or `claude_fallback_models`)
names a model your plan does not have. Set it to `null` (Claude Code's default) or another model in `~/.config/agent-mail/config.json`
([04-claude-and-codex.md](04-claude-and-codex.md)).

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

**No Chats button, the Mail tab says there is no mailbox, Forward fails, no meeting alerts, a
Time source is marked failed, or usage shows in dollars.** Each has an entry in the board's own
[Troubleshooting](../board/README.md#troubleshooting). Usage shows in dollars until the board has a few
days of its own usage readings.

## Getting more detail

| What | Where |
|---|---|
| mailroom log | `journalctl --user -u agent-mailroom -o cat` |
| routing database | `~/.local/state/agent-mail/mailroom.sqlite` (`agent-mail status`, `inbox`, `sent`) |
| status events | `~/.local/state/agent-status/events.jsonl` (`agent-status log`) |
| orchestrator runs | `~/.local/state/agent-mail/orchestrator-runs.jsonl` |
| board actions | `~/.local/state/agent-board/actions.jsonl` |
| what you sent from the board | `~/.local/state/agent-board/sent.jsonl` (`agent-board sent`) |
| Time page, usage breakdown | `~/.local/state/agent-board/time.log`, `allowance.log`, `usage-samples.jsonl` |
| agent mail kept for the Mail tab | `~/.local/state/agent-mail/messages/` |
| group chats | `~/.local/state/agent-mail/groups/` (`deliveries.jsonl`: each post's delivery) |
| to-do helper | `~/.local/state/todo-desk/` |
