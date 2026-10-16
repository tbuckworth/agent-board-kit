# orchestrator

The brief and memory for {{OWNER_NAME}}'s agent orchestrator. It is **not a running session**: the
mailroom starts one fresh `claude -p --model sonnet --effort high` run in this directory per event
(an email no agent session claimed, a reply in a thread the orchestrator wrote, a message from the
agent board or the to-do app), and `agent-send orchestrator "..."` does the same for a peer message.
`AGENTS.md` is the job description, `STATE.md` the memory, and `.claude/skills/agent-report` the
status-report procedure.

```bash
agent-orchestrate "test: reply with the word ready"   # run one event by hand
agent-orchestrate --runs                              # recent runs: cost, turns, outcome
agent-mail rerun N                                    # run again on a held or failed mail
```

Every run's cost and outcome is logged in `~/.local/state/agent-mail/orchestrator-runs.jsonl`.
This directory is a local git repo so each run can commit `STATE.md`; it needs no remote.
