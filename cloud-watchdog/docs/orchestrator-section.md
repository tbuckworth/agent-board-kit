## cloud-watchdog threads

cloud-watchdog emails {{OWNER_NAME}} with `agent-mail send --unrouted`, so replies to those threads come to you.
Their subjects start `[cloud-watchdog`. Turn the reply into exactly one command, run it, and reply in-thread
with its output:

| {{OWNER_NAME}} says | Run |
|---|---|
| "close it", "stop it", "kill it" | `cloud-watch close wd-N --by human --reason "{{OWNER_NAME}}: <their words>"` |
| "keep it", "leave it 48h", "it's fine until Friday" | `cloud-watch ack wd-N --by human --keep <duration> --reason "{{OWNER_NAME}}: <their words>"` (default 24h) |
| "stay in shadow", "not yet" | `cloud-watch mode shadow --by human` (add `--days 7` to go live by itself after 7 days) |
| "turn it off", "stop emailing" | `cloud-watch mode off --by human` |
| "go live now" | `cloud-watch mode live --by human` |

- `wd-N` is in the subject. A digest has no `wd-N`: use `cloud-watch list` to find the resource.
- For a question instead, `cloud-watch show wd-N` and `cloud-watch status` have the facts.
- Do not stop or delete anything by other means.
- For a Lambda instance, `close` also needs `--confirm-terminate`, because the local disk is lost. Only add it if
  {{OWNER_NAME}} clearly said to terminate.
