"""cloud-watchdog: notice paid cloud resources left running, find the agent that owns them, escalate.

Each provider op runs in a child process that holds only one account's credentials (gather, creds, childexec,
collect); the parent folds the results into the state database and acts on them.

Modules:
    check         one timer tick: collect every account, ingest, alert, run the ladder, send the digest when due
    childexec     run by creds.run_as (under `bws run` for a bws account): keeps the allowlisted environment, adds the
                  account's keys, execs the child
    cli           the `cloud-watch` command and the systemd units it installs
    collect       the child's entry point: runs one provider op and prints its result as JSON
    config        paths, and defaults.toml merged with the local config
    creds         runs a command with exactly one account's credentials
    defaults.toml every config key and its default
    digest        the daily digest email
    gather        the parent side of a provider op: runs it in the child and parses the answer
    investigator  the capped `claude -p` owner search, for when the deterministic one finds no owner
    ladder        the escalation ladder: one incident per resource, advanced on every check
    messages      text of the owner messages and incident emails, and the reply footer
    modes         shadow, live and off, and the automatic go-live
    notify        the side effects: agent messages, emails to the human, provider stops
    observe       folds one collection into state: resources, health streaks, ownership, registrations
    owner         owner resolution by transcript search, and the agent-send address book
    policy        who may stop what
    providers     one module per provider; its docstring lists them and their contract
    rules         the rules: findings per resource and digest lines per account
    spend         splits a Modal workspace's month into yours, shared and others
    state         the SQLite state and the audit log
    transcripts   searches Claude and Codex transcripts for the session that launched a resource
    util          time, durations, money and subprocess helpers
"""
