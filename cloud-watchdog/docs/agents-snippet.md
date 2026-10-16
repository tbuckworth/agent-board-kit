## Paid compute you launch (cloud-watchdog)

Anything that bills while it runs must be registered straight after you launch it. That covers `modal deploy`,
`modal run --detach`, a Lambda instance, a job on a paid Slurm partition and an HF endpoint. Run
`cloud-watch register <provider:account:name-or-glob> --expect 6h --reason "..."`, or launch through
`cloud-watch launch --expect 6h --reason "..." -- <command>`. `cloud-watch accounts` lists the account names.
Stop it when you are done.

A message starting `[cloud-watchdog wd-N]` is about something you may own. Answer it with exactly one of:
- `cloud-watch close wd-N --reason ...`
- `cloud-watch ack wd-N --keep 24h --reason ...`
- `cloud-watch disown wd-N --reason ...`

Never ignore it, and never delete volumes or data because of it. Codex: add `--session <your name>`.
