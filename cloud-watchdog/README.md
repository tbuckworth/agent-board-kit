# cloud-watchdog (optional)

Notices paid cloud resources that agents left running, works out which agent launched them, asks
that agent, and escalates to you. It can stop crash-looping Modal apps deployed by a user you list,
and nothing else. Install it only if your agents launch paid compute (Modal, Lambda, a paid Slurm
partition, Hugging Face endpoints, DeepInfra deployments) or use metered APIs (OpenRouter).

It is part of the agent kit: it messages agents with `agent-send`, emails you through the
[mailroom](../mailroom/README.md) (`agent-mail send --unrouted`, so your replies reach the
[orchestrator](../orchestrator/AGENTS.md)), and reads Claude/Codex transcripts to find who launched
what. Enable it with `WITH_CLOUD_WATCHDOG="yes"` in `kit.env` and re-run `./install.sh`.

## What a check does

A `systemd --user` timer runs `cloud-watch check` every 10 minutes. One check:

1. **Collects**, read-only, from every configured account, each in its own child process that holds
   only that account's keys (`creds.py`, `childexec.py`). Fast pass every run; billing, Slurm and spend
   APIs on the slow pass (`slow_interval_min`, default 60).
2. **Applies the rules** (`rules.py`; thresholds in `cloud_watchdog/defaults.toml`).
3. **Finds the owner**: a registration first, then Modal deploy history (who deployed it; for a
   `modal run` app, who created it), then a search of Claude/Codex transcripts for the tool call
   that launched it (`owner.py`, `transcripts.py`). A capped `claude -p` investigator runs only if all
   three fail, and only in live mode.
4. **Walks the ladder** (`ladder.py`): message the owning agent, wait, ask again, then email you. It
   stops something itself only for a confirmed crash loop in a Modal app deployed by a user in
   `identity.owner_modal_users`, on an account with `auto_stop = true` (`policy.py`).
5. Sends the **daily digest** (first check after `digest_hour_local`, default 08:00).

## For agents

```
cloud-watch register modal:main:my-app-* --expect 6h --reason "eval sweep"     # right after launching
cloud-watch launch --expect 6h --reason "..." -- modal deploy app.py           # or launch through it
cloud-watch ack wd-17 --keep 24h --reason "still training" [--expect-cost 600]  # answer to a ping: keep it
cloud-watch close wd-17 --reason "done"                                         # answer: stop it now
cloud-watch disown wd-17 --reason "not mine"                                    # answer: not yours
cloud-watch list            # what of ours is billing now (--all: everything tracked)
cloud-watch show wd-17      # one resource: facts, findings, incident, history
cloud-watch accounts        # the configured account names
```

Patterns are `PROVIDER:ACCOUNT:NAME` with globs; account names are yours to choose in the config.
Inside Claude Code the session is detected; from Codex pass `--session <name>`. `close` is graceful
and never deletes anything:

| Provider | What `close` does |
|---|---|
| Modal | `modal app stop` (volumes, secrets and images are kept) |
| Slurm | `scancel` |
| HF | pause the endpoint |
| Lambda | terminate. The local disk is lost, so it needs `--confirm-terminate`. |

The kit adds the agents' part of this (register what you launch, answer `[cloud-watchdog wd-N]`
messages) to the global agent instructions when the watchdog is enabled
([docs/agents-snippet.md](docs/agents-snippet.md)), and the reply table below to the orchestrator's
brief ([docs/orchestrator-section.md](docs/orchestrator-section.md)).

## For you (or the orchestrator relaying your reply)

```
cloud-watch mode                          # shadow / shadow until <date> / live / off
cloud-watch mode shadow [--days 7]        # (re)start shadow; with --days it goes live by itself afterwards
cloud-watch mode live | off
cloud-watch ack wd-17 --by human --keep 48h --reason "keep it"
cloud-watch close wd-17 --by human --reason "close it"
cloud-watch status | digest --print
```

Every watchdog email ends with the exact command a reply maps to; reply "close it", "keep it 48h",
"go live", "turn it off" and the orchestrator runs it.

## Modes: shadow first

A fresh install is in **shadow** mode with no go-live date: every check decides and logs what it
would do, and the daily digest lists it, but nothing is messaged, emailed (except the digest) or
stopped. Read a few digests, then `cloud-watch mode live`. `shadow_days` in the config (or
`cloud-watch install --shadow-days 7`) sets an automatic go-live date instead. When it goes live,
open incidents are reset and you get a "Now live" email. `off` stops everything but the timers.

## Rules (defaults; override in `~/.config/cloud-watchdog/config.toml`)

Each rule is independent: any one firing is enough. Within a rule every condition must hold. Rules
apply only to things billing right now. Resources that belong to someone else (another Modal user,
a Lambda box without your SSH keys) are ignored, except crash loops, which are emailed to you.

| Rule | Fires when | Then |
|---|---|---|
| crash_loop (Modal) | ≥ 3 containers failed in the last 120 min, and ≥ 50 % of the containers seen in that window failed, on 2 checks in a row | urgent message to the owner; after 15 min with no answer, stop it if it is yours and `auto_stop` is on, and email you |
| high_burn | ≥ $25/h | message to the owner |
| long_running | Lambda or HF/DeepInfra ≥ 24 h; Slurm (paid partitions) ≥ 12 h; a Modal app with a container up ≥ 12 h | message to the owner |
| cost (not acked) | ≥ $150 → message to the owner; ≥ $500 → email you, then again at each doubling | |
| cost (acked) | email you at max($500, 2 × X), then at each doubling (X = `--expect-cost`, or the cost at the ack) | |
| idle_gpu (your Lambda boxes) | no work process and GPU ≤ 5 % on 3 checks in a row | message to the owner; never terminated |
| account_spend | per-account 24 h spend over a limit you set (`[rules.account_spend]`, none by default) | digest line |
| storage | Modal volumes ≥ $100/month | Monday digest line |
| blind | a check fails 3 times in a row | email you |

**Owner messages.** The owner gets a message, then another after 30 min (up to 3), then you are
emailed. If the owner session has exited, the watchdog resumes it for one turn and emails you too.
**Dedupe.** One incident per resource; an ack silences it until it expires (a new crash loop still
counts). At most `max_emails_per_day` (6) emails a day; the rest go into the digest.

## Configuration

`~/.config/cloud-watchdog/config.toml` overrides `cloud_watchdog/defaults.toml` key by key. The
defaults list **no accounts**: a provider with no configured account is not watched. Owner name,
email and repos root default to the mailroom's config.

```toml
[identity]
owner_modal_users = ["alex-1234"]      # your Modal user name(s): their apps are "yours"; enables auto-stop
owner_lambda_ssh_keys = ["alex-laptop"] # SSH key names you launch Lambda boxes with

[accounts."modal:main"]
provider = "modal"                     # keys: MODAL_TOKEN_ID, MODAL_TOKEN_SECRET
auto_stop = true

[accounts."lambda:main"]
provider = "lambda"                    # key: LAMBDA_API_KEY

[accounts."slurm:cluster"]
provider = "slurm"
secrets = "none"                       # uses your SSH config
ssh_host = "cluster-login"             # alias of a node where squeue/scancel work
paid_partitions = ["gpu-paid-*"]       # other partitions are ignored

[rules.account_spend]
"modal:main" = 200                     # digest line when 24 h spend exceeds $200
```

The full commented example (HF, DeepInfra, OpenRouter, Modal profiles, Slurm partition rates) is at
the end of [defaults.toml](cloud_watchdog/defaults.toml).

### Credentials

Each account's collector runs in a child process that receives only that account's keys. Where the
keys come from (`[secrets] backend`, or `secrets = "..."` per account):

| Backend | Keys come from |
|---|---|
| `env-file` (default) | `~/.config/cloud-watchdog/accounts/<provider>-<account>.env`, `KEY=value` lines, mode 600 (e.g. `modal-main.env` with `MODAL_TOKEN_ID=...` and `MODAL_TOKEN_SECRET=...`) |
| `bws` | Bitwarden Secrets Manager: `bws run` with the token and project-id files in `[secrets]`; map each key to a secret name with `env = { LAMBDA_API_KEY = "MY_LAMBDA_KEY" }` |
| `environ` | the service's own environment (e.g. a systemd drop-in with `EnvironmentFile=`) |
| `none` | no keys (Slurm over SSH; Modal with a `~/.modal.toml` profile) |

Secrets are never stored in the watchdog's state or logs. `cloud-watch accounts` shows what is
configured and where each account's keys come from.

## Install

The kit's installer does it when `WITH_CLOUD_WATCHDOG="yes"`: the `cloud-watch` wrapper, the units
below rendered into `~/.config/systemd/user/`, and (with `--start`) the two timers enabled. Then
add your accounts and check:

```bash
$EDITOR ~/.config/cloud-watchdog/config.toml
cloud-watch accounts
cloud-watch check --dry-run --slow       # a full check on a throwaway copy of the state: nothing sent or kept
systemctl --user list-timers 'cloud-watchdog*'
```

Without the kit installer, `cloud-watch install [--shadow-days N]` writes the units, links
`~/.local/bin/cloud-watch` (if nothing is there) and enables the timers; `cloud-watch uninstall`
disables them (state is kept).

**Units:**
- `cloud-watchdog.timer` → `cloud-watchdog.service`: every 10 minutes. If a run fails,
  `OnFailure=cloud-watchdog-alert@%n.service` emails you (at most once every 6 h), naming systemd's
  result and, for an out-of-memory kill, the kernel's recent kills. `OOMPolicy=continue` lets a check
  finish without one killed collector; `MemoryMax=1G` caps a runaway (a check peaks at ~160 MB).
- `cloud-watchdog-heartbeat.timer`: hourly; emails you if no check has succeeded for an hour.
- Optional, root: `host/user-manager-revive@.{service,timer}` restarts your systemd user manager if a
  global out-of-memory kill takes it down (lingering does not restart it). That keeps every user
  service of the kit alive, not just this one. Install instructions are in the unit file's header.

**State:** `~/.local/state/cloud-watchdog/state.sqlite` (resources, incidents, observations,
registrations, actions, metrics, Modal app owners), `events.jsonl` (append-only audit log),
`outbox/` (copies of sent emails).

## Notes

- **Shared Modal workspaces.** Modal's billing is per app with no user field. The watchdog joins it
  with each app's lifecycle (`created_by`, `deployed_by`) to split the month into yours, shared
  (`identity.modal_shared_apps`, or apps whose creator and deployer differ) and others'. The lookup
  runs `providers/modal_api.py` under the Python the `modal` CLI uses (`modal_python` to override).
- **Cost of long-lived Modal deployments** counts from when the current run began, so an app that
  scales to zero between uses does not trip the cost rule on old spend.
- **Crash-loop window is 120 minutes**: an app failing every ~30 min still shows several failures
  in that window, and "≥ 50 % of containers failed" catches containers that hang in startup.

## Tests

`python3 -m unittest discover -s tests` (stdlib only; no network), or `scripts/run-tests.sh watchdog`
from the kit root.

## Troubleshooting

| Symptom | Check |
|---|---|
| Nothing is ever reported | `cloud-watch mode` (shadow sends only the digest); `cloud-watch accounts` (no accounts = nothing watched) |
| "blind" emails | `journalctl --user -u cloud-watchdog -n 50`; a provider CLI missing from the service's PATH, or keys not found (`cloud-watch accounts`) |
| A resource is always "unknown owner" | register launches (`cloud-watch register`), set `identity.owner_modal_users` / `owner_lambda_ssh_keys`; `cloud-watch whois wd-N` shows the evidence |
| Crash loops are never auto-stopped | only in live mode, only on accounts with `auto_stop = true`, only for apps deployed by `owner_modal_users` |
