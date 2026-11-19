# mailroom

One shared Gmail account for every Claude Code and Codex session on a machine, with replies
routed back to the exact session that sent the original mail. Around that core sit the tools the
rest of the kit relies on to start, address, watch and close agents.

- Agents send with `agent-mail send` / `agent-mail reply`. Before a mail leaves, the sending
  session gets a stable routing tag, recorded in a local SQLite map, and the mail is kept in a
  local message store.
- The `agent-mailroom` daemon polls the agents mailbox, works out which session a reply belongs to,
  and delivers it into that session with `agent-send` (resuming the session if it has exited).
- Mail that no session claims goes to the **orchestrator**: one fresh `claude -p` run per event in
  the orchestrator directory (the kit's `orchestrator/` template), never a long-lived session.
- The agent board's **Mail tab** reads and answers the owner's agent mail through
  `agent-mail mailbox`, without Gmail; a reply written there is routed like an emailed one.
- `agent-group` gives agents **group chats**, in which the owner may post too.
- `agent-status` hooks record a text-free line per turn, start and end; agents add milestone notes.
  `agent-status report` turns that into a "what are my agents doing" report with no model call.
- `agent-spawn` starts an agent in a fresh git worktree and tmux session with a brief;
  `agent-close` ends it and removes the worktree once its work is landed.

The mailroom never runs a model itself. Only the orchestrator and the pre-send reviewer do, and only
per event or per mail.

How it fits the kit: the installer writes `~/.config/agent-mail/config.json` from `kit.env`, puts a
wrapper for each tool in `~/.local/bin`, and installs the systemd units below. The agent board
reads this config (`owner_name`, `owner_email`, `repos_root`) and calls `agent-send`,
`agent-switch`, `agent-close`, `agent-mail mailbox` and `agent-group`; the to-do tools hand tasks to
agents through `agent-spawn` or the orchestrator. The global agent instructions (kit `templates/`)
tell every agent how to use `agent-mail`, `agent-group` and `agent-status note`.

## Contents

| Path | What |
|---|---|
| `bin/agent-mail` | Agents **send** mail with it: `send`, `reply N`, `guide`, `withdraw`. People use `inbox [--held]`, `show N`, `release N TARGET`, `rerun N`, `sent`, `status`, `recover N`, `receipt UUID`, `backup`. The board uses `mailbox ...`. |
| `bin/agent-mailroom` | The daemon. Polls Gmail history every `poll_seconds`, walks the routing slip, delivers or holds; routes replies written in the Mail tab. `--once`, `--dry-run`, `--baseline`. |
| `bin/agent-send` | Deliver a message to any Claude Code or Codex session on this machine by name or id; `--list` is the address book. |
| `bin/agent-group` | Group chat between agents, in which the owner may post; every post reaches every member. |
| `bin/agent-status` | `note "..."` (agents), `report [--format text\|md\|html\|json] [--email TO]`, `log`, and `hook --cli claude\|codex` (the hook entry point). |
| `bin/agent-orchestrate` | Run the orchestrator once on one event; `--runs` lists cost and outcome per run. |
| `bin/agent-spawn` | Start an agent: `--repo R --branch B` (new worktree), `--new-repo NAME`, or `--dir PATH`; brief via `--brief FILE`, `--brief-text`, or stdin. |
| `bin/agent-close` | End an agent's session and remove its worktree: `agent-close NAME` or, from inside, `agent-close --self`. Refuses on unlanded work unless `--force`. |
| `bin/agent-switch` | Switch a live agent to another CLI, Claude login, model or effort (the board's right-click menu). Optional. |
| `bin/agent-usage` | Plan usage left on each Claude login and on Codex; `--pick`; `init-account` prepares a second Claude login. Optional. |
| `bin/agent-failover` | Daemon: moves a Claude agent to another login, or onto the next model, when its own hits a usage limit, and back after the reset; mails the owner while agents bill extra usage. Optional. |
| `bin/agent-memwatch` | Daemon: warns the agents holding the most memory before the machine runs out. Optional. |
| `agent_mail_auth.py` | One-time OAuth consent for the agents account; installed as the `agent-mail-auth` wrapper. |
| `agentmail/` | The library the commands share; `agentmail/__init__.py` lists its modules. The review's rules and prompts are `email_rules.md`, `review_prompt.md` and `review_prompt_outside.md`. |
| `systemd/` | `systemd --user` unit templates for `agent-mailroom`, `agent-failover`, `agent-memwatch` and `agent-mail-backup` (with its hourly timer). |
| `tests/` | Unit tests (see [Tests](#tests)). |
| `requirements.txt` | The Google client libraries. Everything else is Python 3.9+ standard library. |

## The mailroom

```bash
agent-mail status                        # daemon lock, history cursor, counts, backup targets
agent-mail inbox --held                  # what is waiting for a person
agent-mail show 3                        # one inbound mail in full
agent-mail release 3 some-session-name   # deliver a held mail by hand
```

With the service stopped (one daemon at a time holds `mailroom.lock`), `agent-mailroom --once --dry-run`
polls once, deciding and logging but delivering nothing and changing no labels, and
`agent-mailroom --once --baseline` moves the history cursor to now, so older mail is ignored.

### The tag

Each session that sends mail gets a stable 8-character tag (e.g. `k7mq2xbe`), stored with its
identity `(host, cli, session id)` before the mail leaves. The mail carries the tag three ways:

- `Reply-To: <from_name> <agents+k7mq2xbe@example.com>` (a Gmail plus-address of `account`);
- `[#k7mq2xbe]` at the end of the subject;
- an `X-Agent-Mailroom: k7mq2xbe` header.

The From line is `<from_name> (<session name>)`, so the owner sees which agent wrote. Inside Claude
Code the sending session comes from `CLAUDE_CODE_SESSION_ID`. Codex has no such variable: a Codex
agent names itself with `/rename NAME` and passes `--session NAME` to `agent-mail`.

### The routing slip

For each new message in the inbox, in decreasing trust, the first hit wins:

1. A copy of mail the agents account sent itself: skipped.
2. Sender not in `allowlist` or `collaborators`: **held**, never delivered anywhere.
3. Sender DKIM check fails (see [Sender tiers](#sender-tiers-dkim-collaborators)): held, owner alerted.
4. A reply or forward to a `[mailroom]` notice (or in a saved alert thread): the orchestrator,
   ahead of every session hint, since notices quote the original `[#TAG]`.
5. `In-Reply-To` / `References` names a Message-ID the mailroom sent: that session (or group).
6. The Gmail thread is one a session sent in: that session.
7. `agents+TAG@` in To / Cc / Delivered-To: the session that owns TAG.
8. `[#TAG]` in the subject: the session that owns TAG.
9. Nothing matched, sender known: the orchestrator.

After the Gmail mail, each poll routes the replies the owner wrote in the board's Mail tab the same
way ([The Mail tab's mailbox](#the-mail-tabs-mailbox)). A reply to a group's mail becomes the owner's
post in that group ([Groups](#groups)).

A reply in a thread that only ever held `agent-mail send --unrouted` mail (status reports, memory
warnings, cloud-watchdog) belongs to the orchestrator. A reply that looks like it answers a
session's mail but cannot be matched is an *unowned reply*: it is held, a bounded read-only
recovery search runs (`agent-mail recover N`), and the owner gets one notice.

**Orphan fallback.** When the owner replies in, or forwards, the thread of a session that has closed
or exited, the mail goes to the orchestrator instead, with no notice. The session counts as gone when
`agent-send` exits 2 (not live and not resumed), 4 (no such session) or 5 (its folder was removed, as
`agent-close` does), or when the Mac answers a delivery with one of those codes. The orchestrator's
event names the closed session in its first line and carries the thread. A delivery that fails for
another reason (an unreachable Mac, say), and a collaborator's mail to a gone session, stay held with
the notice.

Quoted text and signatures are stripped before delivery; a forwarded message (Gmail's
`---------- Forwarded message ---------` line or Apple Mail's `Begin forwarded message:`) is kept, up
to 20,000 characters. The session receives one message whose first line says who wrote and how to
answer:

```
[email from Alex <owner@example.com> re "Results" · to reply: agent-mail reply 42  (reply body on stdin or --body-file)]
Looks good, go ahead with the second run.
```

Under that line the owner's mail carries a reminder of how to write the answer
(`routing.reply_reminder`): the owner's questions first, answered in order, when the mail holds a
question mark; and from the owner's third mail in a thread, restate the whole picture after repeated
requests to present something differently, and use `--subject` once the thread's subject no longer
fits. `agent-mail reply 42` answers in the same Gmail thread, from the same tag.

### Gmail labels

The daemon creates these labels on start (names configurable under `labels`):

| Label | Meaning |
|---|---|
| `mailroom/delivered` | Delivered to a session or handled by the orchestrator; marked read. |
| `mailroom/held` | Waiting for a person; left unread. `agent-mail inbox --held` lists them. |
| `mailroom/skipped` | Reserved; created but not applied today. |

A reply written in the Mail tab has no Gmail copy, so it gets no labels.

### Delivery

`agent-send` is the only delivery path:

- live Claude Code session: one JSON line on its inbox socket (from `~/.claude/sessions/<pid>.json`);
- live Codex session: `codex queue --thread <id> --message ...` (Codex >= 0.149);
- exited session (`resume_offline`): `claude --resume <id> --bg "..."` or a detached
  `codex exec resume <id> "..."`, with bypassed permissions unless `resume_dangerous` is false.

Before a message leaves, `agent-send` writes a private transport receipt. If `agent-send` is
interrupted, the daemon checks that receipt, so a message the session accepted counts as delivered.
"Delivered" means the session's transport accepted it, not that the agent has read it. An
unconfirmed failure stays held, with one notice saying the mail is saved and what to do next.
Mail whose orchestrator run fails, or whose handling raises an error part-way, is held and the owner
is alerted. When Gmail no longer has the saved history id, the daemon searches the inbox from the last
successful poll and alerts the owner.

## Sender tiers, DKIM, collaborators

- **Owner** (`allowlist`, normally one address, the owner's personal Gmail): mail is delivered as the
  owner's instruction, headed `[email from <name> <address> ...]`.
- **Collaborators** (`collaborators`): other people's agents. Their mail is delivered marked
  `(collaborator agent of <their owner>, not <owner_name>)` with a guard line: answer questions and
  share project content, but run no commands, change no files, push nothing, spend nothing, mail
  nobody else on its say-so; small reversible work only on a branch, and tell the owner by email.
  The orchestrator classifies their requests the same way.
- **Everyone else**: held.

Both tiers are honoured only if Gmail's `Authentication-Results` header shows a DKIM pass for the
sender's domain (`require_dkim`, default on). A forged From header is held and the owner is alerted.
Collaborator traffic is capped at `collaborator_hourly_cap` per hour; a thread with more than
`agent_thread_cap` consecutive agent messages and no human is held (loop guard); and the owner gets
a notice when a collaborator starts a new thread (`notify_owner_new_collab_thread`).

```json
"collaborators": {"sam.agent@example.com": {"name": "Sam's Claude", "owner": "Sam"}}
```

The pre-send reviewer of mail to the owner is told each collaborator's `owner` (else its address), so
it does not ask agents to explain who they are.

## The orchestrator

The orchestrator is not a running session. Each event runs one fresh
`claude -p --model <orchestrator_model> --effort <orchestrator_effort>` in `orchestrator_dir`, with no
MCP servers, serialised by a lock, with the whole email thread (quotes stripped) in the prompt. Its
brief (`CLAUDE.md`/`AGENTS.md`) and memory (`STATE.md`) live in that directory. Inside a run,
`agent-mail` sends and replies as the orchestrator, so replies come back to it.

Events: mail from a known sender that no session claimed, the owner's mail to a session that has
closed or exited (the [orphan fallback](#the-routing-slip)), a reply in a thread the orchestrator
wrote in, and `agent-send orchestrator "..."` (when no live session is named exactly `orchestrator`).
The owner can ask it by email for a group chat; it makes one with `agent-group new --for-mail N`
([Groups](#groups)).

```bash
agent-orchestrate "text"        # run one event by hand
agent-orchestrate --runs        # cost, turns, outcome per run
agent-mail rerun N              # run it again on a held or failed mail
```

A failed run holds the mail and sends the owner a `[mailroom]` notice; nothing is lost.

**When a login is out.** A login that fails on usage or login (or returns nothing) passes the event to
the next (`headless.logins`):

1. each Claude login in `usage.order` that is set up (see [a second Claude login](#usage-aware-routing-and-a-second-claude-login));
2. Codex, where `usage.order` puts it (the default order lists it after every Claude login):
   `codex exec --json --ignore-user-config`, so no MCP server, plugin or hook from
   `~/.codex/config.toml` loads, as the Claude run has no MCP. The global and the directory's
   `AGENTS.md` are still read. Model `orchestrator_codex_model` (`null`: `spawn_codex_model`), effort
   `orchestrator_codex_effort`;
3. optionally, an Anthropic API key from Bitwarden Secrets Manager. Set `orchestrator_fallback_secret`
   to the secret's name in your `bws` project, and provide `bws` on PATH (or `~/.local/bin/bws`), an
   access token file (`bws_token_file`) and a project-id file (`bws_project_file`). `bws run` injects
   the project's secrets into a child shell, which maps that name onto `ANTHROPIC_API_KEY` and execs
   `claude`; the key never passes through the mailroom, a file or a log. Unset (the default), there is
   no API-key step.

A timeout, or a failure after the run's first turn, ends the event there: that run may have acted
already. The run log's `auth` says which one ran it (`agent-orchestrate --runs` shows it), and
`failed` what the earlier ones said.

## The status log and report

Hooks write one line per event, with no message text, to
`~/.local/state/agent-status/events.jsonl`: `idle` (Stop), `start` (SessionStart), `end`
(SessionEnd), `limit` / `api-error` (StopFailure, Claude only) and `quota` (the Notification types
`quota_auto_resume_*`, Claude only). Agents add milestones themselves with
`agent-status note "one line"`. The daemon's watchdog alerts the owner on `quota` events. A
`UserPromptSubmit` hook runs `agent-group hook`, which tells a session about group posts that did not
wake it ([Groups](#groups)).

**Claude Code**, in `~/.claude/settings.json` (merge into existing `hooks`):

```json
{
  "crossSessionInbound": "accept",
  "hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20, "async": true}]}],
    "SessionStart": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20, "async": true}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20}]}],
    "StopFailure": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20, "async": true}]}],
    "Notification": [{"matcher": "quota_auto_resume_fired|quota_auto_resume_stale|quota_auto_resume_disabled",
                      "hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli claude", "timeout": 20, "async": true}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-group hook --cli claude", "timeout": 20}]}]
  }
}
```

`"crossSessionInbound": "accept"` is required: without it, a Claude session running with bypassed
permissions holds every message from outside (the mailroom, `agent-send`) for approval and drops it
after five minutes. Sessions read it at launch, so restart sessions that were already running.

**Codex**, in `~/.codex/hooks.json` (Codex has no StopFailure or quota Notification events):

```json
{
  "hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli codex", "timeout": 20}]}],
    "SessionStart": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli codex", "timeout": 20}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-status hook --cli codex", "timeout": 3}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "$HOME/.local/bin/agent-group hook --cli codex", "timeout": 20}]}]
  }
}
```

Codex asks to review new or changed hooks ("Hooks need review") until someone trusts them in its
`/hooks` screen; see [Starting an agent](#starting-an-agent) for what an unattended start does. The
hooks never fail and never print: a broken status log cannot disturb an agent.

```bash
agent-status note "E2 finished: 0.81 accuracy, results in results/e2.json"
agent-status report                                  # text, last 24 h
agent-status report --format html --email owner
agent-status log --since 2h --event note
```

## Sending mail from a session

```bash
printf 'Results below...\n' | agent-mail send --to owner --subject "Run results"  # owner: the owner's address
agent-mail send --to X --subject S --body-file body.txt --html body.html   # your own HTML part
agent-mail send --to X --subject S --session my-codex-name --body-file b.txt   # from Codex
agent-mail reply 7 < answer.txt                                           # answer inbound #7 in-thread
agent-mail reply 7 --subject "Run fixed: results" --body-file answer.txt  # the thread's subject no longer fits
agent-mail send --to X --subject S --html body.html                      # HTML alone: the text is made from it
agent-mail send --unrouted --to X --subject S < report.txt                # no session; replies go to the orchestrator
```

`--to` and `--cc` take an address, or `owner` (or the owner's name, config `owner_name`; any case)
for the owner's address (`owner_email`, else the first `allowlist` entry). Agents mail the owner as
`--to owner`, and write the body file in one command and send in the next, so that no send command
holds the owner's address or a link: Claude Code asks for approval of a command that holds the
signed-in account's address together with a link or a network tool's name, even when permissions are
bypassed.

The plain text comes from `--body-file`, else from a pipe or redirect, else, with `--html` alone, from
the HTML. Input that was not piped or redirected in is never read, and a pipe silent for 60 s gives up
(`agentmail/piped.py`): in Claude Code, a command that holds a heredoc runs with the tool's socket as
its input, which never ends. `agent-send`, `agent-status note`, `agent-orchestrate` and `agent-spawn`
read piped input the same way.

Every mail is multipart/alternative: the text as given, plus an HTML part rendered from it unless
`--html` supplies one (`agentmail/compose.py`, everything escaped first): blank-line paragraphs, single
newlines as soft breaks, `-`/`*` bullets and `1.` items as real lists (items indented two columns or
more nest), fenced code, `**bold**`, `*italic*` and `` `code` `` spans, `[text](https://...)` links
(http, https and mailto only), and pipe tables as real tables (a GitHub `|---|:--:|` row second makes
the first row the header and sets alignment). Threading and routing never depend on the HTML. Each
mail is kept in the message store before it is sent, and is not sent if it cannot be kept ([The Mail
tab's mailbox](#the-mail-tabs-mailbox)).

`reply --subject S` answers under a new subject, for a thread whose subject no longer says what the
mail is about. Gmail threads only mail whose subjects match, so the answer starts a new thread; it is
still recorded as the answer to that mail, and its last line links to the mail it answers.

### Plots, images and files

```bash
agent-mail send --to X --subject S --body-file body.txt --html body.html \
    --image acc=plots/accuracy.png --image demo=demo.gif --attach results.csv
```

`--image NAME=PATH` (PNG, JPEG, GIF or WebP, repeatable) embeds the image as a `multipart/related` part
with `Content-ID: <NAME>`; place it in the HTML with `<img src="cid:NAME" alt="..." style="max-width:100%">`.
An image the HTML does not reference is shown at the end, so `--image` also works without `--html`.
`--attach PATH` adds an ordinary attachment. Both work through the Mac proxy. Images and attachments
together are capped at 20 MiB.

### Widgets in agent mail

A mail's plain-text body may carry clickable options and interactive plots, which the board's Mail tab
draws in place: a fenced block whose info string is `choices` or `plot`, holding one JSON object
(`agentmail/widgets.py`).

````text
```choices
{"question": "Which do you want?", "default": "Option 2 tonight",
 "options": [{"label": "Wait for review", "detail": "Merge once the review passes."},
             {"label": "Ship it now", "recommended": true}]}
```

```plot
{"title": "Accuracy by step", "kind": "line", "x": {"label": "step"}, "y": {"label": "accuracy", "min": 0, "max": 1},
 "series": [{"name": "Baseline", "x": [0, 1000, 2000], "y": [0.1, 0.52, 0.7]}], "image": "acc"}
```
````

- **choices:** `question` (1-200 characters) and `options`, 2 to 8 of them, each a `label` (1-120) with an optional
  `detail` (at most 300) and an optional `recommended: true` (one at most). Optionally `multiple: true`, for
  checkboxes, which takes no recommended option, and `default` (at most 200): what the agent does with no answer.
- **plot:** `title` (1-200), `kind` (`line`, `scatter` or `bar`), `x` and `y` (each an optional `label` of at most 80;
  `y` may also give finite `min` and `max`), and `series`, 1 to 10 of them, each a `name` (1-80) with `x` and `y`
  lists of one length. Numbers are finite, and `y` may hold null for a gap. A bar plot's `x` lists category names
  (1-80 characters each), at most 50, the same in every series. At most 5,000 points in all. `image` (1-64
  characters) is the NAME of an `--image` sent with the mail.
- No text holds a control character (a line break, a tab or another). At most 5 blocks a mail, each at most 64 KiB
  of JSON; no other keys. A mail with blocks takes no `--html`. agent-mail refuses a mail that breaks any of this
  before sending or keeping it, naming the block, the key and the rule; the Linux box checks a proxied mail's
  widgets again.
- **What Gmail gets** in each block's place: the question and the options as a numbered list, then the default and
  "Reply with the number"; for a plot, its `--image` there, or a line naming the plot. The `sent` row keeps the
  widgets, and `mailbox show` hands them to the tab with their places marked.

### Pre-send review of agent mail

Mail is reviewed before it leaves (`agentmail/review.py`), so that what reaches the owner can be acted on
without a round of "what does this mean?". Mail to an owner address (the `allowlist`) is held to the
owner's rules in `agentmail/email_rules.md`: bottom line first, no unexplained labels, scannable, plots
inline, a clear ask, every command the owner should run in a fenced block of its own (the Mail tab gives
it a Copy button), and in a reply, the owner's questions answered first. `agent-mail guide` prints
them, and `agent-spawn`'s preamble tells agents to read them before a results mail. Mail to anyone else
(collaborators, or a person the owner asked an agent to write to) is held to clarity rules in
`agentmail/review_prompt_outside.md`: purpose first, no unexplained labels, self-contained asks and
handovers, nothing that must not leave. Plots are not asked for there; the test is whether the owner
and the collaborator could follow the thread later if asked about it.

The rules were mined from several hundred replies in which one owner had to ask agents for clearer mail
(jargon, structure, plots, length, the ask). Edit `email_rules.md` and `review_prompt.md` to match your
own taste (they are Python format strings: `{owner_name}`, `{owner_context}` and `{collaborators}` are
filled in, and a literal brace is written twice), and put a few sentences about yourself in
`review.owner_context` (your field, your projects, the people you work with, your own apps) so the
reviewer does not ask agents to define what you already know.

0. A credential in the mail (an API key or token pattern, `review.SECRET_PATTERNS`) holds it on every
   round and in every mode, and no draft is kept for the watchdog; only `--no-review` sends it.
1. Model-free checks: an image with a `data:` URI or a local path, or `cid:NAME` with no `--image NAME`.
   Plain-text mail to the owner of 120 words or more (no `--html`) also gets layout checks on the HTML
   agent-mail renders from it (`review.layout`), which the reviewer cannot see because it reads that HTML
   flattened: a wall of bare paragraphs, a list item whose sentence goes on flush left, and hard-wrapped
   text. These hold the mail even if the model is down.
2. Mail under 120 words with nothing from step 1 is passed without a model call.
3. A fresh-context reviewer reads only what Gmail will show (`review.render`: the HTML flattened to
   text with `[IMAGE ...]`, `[TABLE]` and `[CODE]` markers, or the HTML agent-mail renders from the text), plus
   hints (local paths, pipe tables, text a shell may have eaten, the third mail within an hour). Before a
   reply to the owner it is also shown the message being answered (its first 6,000 characters), and
   checks that each question is answered near the top, that a change of presentation the owner asked
   for was made, and that the thread's subject still fits. It is `claude -p` on Haiku with the prompt
   for its reader and a JSON schema, and no tools, settings, CLAUDE.md or hooks, so it logs nothing to
   the status log. It runs on each ready Claude login in `usage.order` in turn (see [a second Claude
   login](#usage-aware-routing-and-a-second-claude-login)) and last on the optional API-key fallback
   (`orchestrator_fallback_secret`), moving on when a login fails (usage limit, logged out); the row's
   `model` reads `haiku`, `haiku (NAME login)` for a login after the first, or `haiku (api key)`. Mail
   proxied from the Mac is reviewed on the Linux box.
4. `pass`: the mail goes. `revise`: nothing is sent; agent-mail prints `agent-mail: NOT SENT` and the
   fixes, and exits 3 (a proxied send gets the same text as a completed receipt). If the reviewer could
   not run and only the automatic checks of step 1 held the mail, the message says so. The **next** send
   or reply from that session to the same kind of reader (`audience`) goes out whatever the review says
   (one pushback per mail); it is still reviewed and recorded (`review_round2`), to measure whether the
   revision fixed it. A held mail to a collaborator does not let the agent's next mail to the owner
   through, nor is it settled by it.
5. `--no-review "why"` sends at once (recorded as `override`); `agent-status report --email` passes it,
   since the report has a fixed format, and `--unrouted` system mail is never reviewed.
   `agent-mail withdraw` drops the held draft. A draft nobody revised or withdrew is sent unchanged by
   the mailroom's watchdog after `fallback_minutes` (`expired`); mail to the owner gets a one-line note
   at the top saying so, mail to anyone else does not. A draft whose session or replied-to mail is no
   longer in the store is not sent (`dropped`, with the reason).
6. Any other failure (no claude binary, timeout, every login failing, bad JSON) sends the mail (`error`).

Every decision is a row in the `reviews` table: `tag`, `created_at`, `round`, `verdict` (pass, revise,
advised, skipped, override, error, withdrawn, expired, dropped), `subject`, `kind`, `sent_message_id`
(set once that mail went out), `reasons`, `fixes` (JSON), `words`, `model`, `seconds`, `cost`, `draft`
(the held mail, cleared once settled) and `audience` (`owner`, `collaborator` or `other`; null means
owner). The agent board reads it, and relies on two shapes:

- **A held draft** is the newest row per tag and audience with `verdict` revise and a `draft`: the board
  shows "sent back for revision and not re-sent".
- **A credential stop** is `verdict` revise, `kind` secret and no `draft`: nothing will go out unless the
  agent sends again, so the board shows a tile until it does. Its `reasons` quote the start of the
  credential, and the board never shows them.

Config, under `"review"` in `config.json` (defaults shown):

```json
{"review": {"enabled": true, "model": "haiku", "timeout": 40, "min_words": 120,
            "fallback_minutes": 30, "mode": "enforce", "review_round2": true,
            "hold_kinds": "all", "hold_min_words": 300, "outside": true, "hold_outside": true,
            "owner_context": ""}}
```

`"mode": "shadow"` reviews and records every mail but always sends: use it to measure first.
The reviewer grades each issue `blocker` (the reader would have to ask before acting) or `polish`; only
a blocker holds a mail, and polish is printed after sending as tips. `hold_kinds` narrows which mail a
blocker holds: a list such as `["results_report", "literature_review", "recommendation",
"plan_or_proposal"]` holds those kinds and any mail of `hold_min_words` or more, and sends the rest with
the blockers as tips (`advised`). In the evaluation the reviewer was tuned on, that lighter setting held
59% of the mails that had drawn a complaint instead of 93%, and about 30% of all agent mail instead of
about 55%. `"outside": false` stops reviewing mail to anyone but the owner; `"hold_outside": false` sends
it at once and returns its blockers to the agent as tips (`advised`).

## The Mail tab's mailbox

The agent board's Mail tab shows the owner's mail with the agents and takes the owner's replies,
without Gmail. The mailroom keeps the mail and the owner's flags on each thread (read, archived,
snoozed), and the tab reads and changes them through `agent-mail mailbox`, which runs on the Linux box
(the board calls it over its SSH stream). The mailroom side needs no credentials beyond the agents
account; whatever reads the owner's own Gmail for `mailbox sync` belongs to the board.

**The message store.** Every agent mail (a send, a reply, a held draft the watchdog sends, `--unrouted`
mail, a group's mail to the owner) is written, exactly as it is about to be handed to Gmail, to
`~/.local/state/agent-mail/messages/<first 2 hex digits>/<sha256>.eml` (mode 600, folders 700; a temp
file, synced, then renamed), and then those bytes are sent. A mail that cannot be written is not sent,
and agent-mail fails. The `sent` row records the file's sha256 (`eml`), the Cc, the plain text, the
number of attachments and the mail's [widgets](#widgets-in-agent-mail). Mailroom alerts are not kept.

`agent-mail mailbox backfill [--limit N]` fills in mail sent before the store existed: for each `sent`
row without a file, newest first, it fetches the raw message from the agents account's Gmail
(read-only) and keeps it. A mail it cannot fetch is reported and left for the next run, and it then
exits 1. Run it once after upgrading from a kit without the message store.

**Threads and flags.** A thread is a Gmail thread id. It is in the mailbox when agent mail in it went to
or cc an owner address (config `allowlist`), or mail in it came from one; its messages are all its agent
mail and the owner's mail, never anyone else's. The owner's flags on a thread hold `sent` ids, so "read
up to agent mail 123" never races a mail that arrives while the tab shows the thread. The rules are
Gmail's (`mailbox.state`): `read` up to N (newer agent mail makes it unread again); `unread`; `archive`
up to N (out of the inbox until newer agent mail comes); `snooze` up to N until T (back in the inbox,
unread, at T, or at once when newer agent mail comes); `inbox` (ends an archive or a snooze). The
owner's own mail changes none of these.

**Commands.** Each prints one JSON object, or on refusal prints why on stderr and exits non-zero; the
keys are in `agentmail/mailbox.py`.

```bash
agent-mail mailbox list [--box inbox|snoozed|archived|all] [--query TEXT] [--limit N]   # inbox, 100 (at most 500)
agent-mail mailbox show THREAD               # its messages, and the thread as one sanitized HTML page
agent-mail mailbox mark THREAD... --op read|unread|archive|inbox|snooze [--upto ID] [--until EPOCH]
agent-mail mailbox sync < gmail.json         # the tab and the owner's Gmail into step: {"inbox": [...], "unread": [...]}
printf 'Ship it.\n' | agent-mail mailbox answer THREAD --key KEY
agent-mail mailbox answer THREAD --key KEY --json < reply.json   # {"text": ..., "files": [{"name": ..., "data": BASE64}]}
agent-mail mailbox part s101 1               # attachment 1 of stored agent mail 101 (i45: of the owner's reply 45), base64
agent-mail mailbox summary                   # the counts, and agent mail to the owner read in the tab in 8 days
```

- `list` sorts newest first; its snippet is the newest message's words as the reader sees them.
  `--query` wants every word somewhere in the thread's subjects, its agents' names or its text.
- `mark --upto` is the newest agent mail id the tab showed; `read`, `archive` and `snooze` need it, and
  `snooze` needs `--until` in the future.
- `sync` is given the Message-IDs of the agent mail in the owner's Gmail inbox and of that unread there.
  What is done in either counts as done in both: each thread not snoozed in the tab stays in the inbox
  only when it is there in the tab and some of its agent mail is in the Gmail inbox, and stays unread
  only when it is unread in both. It prints what it changed and the Message-IDs Gmail must archive and
  mark read to match (`gmail`), which the board then changes there.
- `show`'s page (`mailview.document`) is shown with scripts off and every load blocked: a strict content
  security policy, each message's HTML through an allowlist sanitizer (formatting only; links only http,
  https and mailto; images only from the mail's own parts), and attachments as links `part` serves.
- `mark`, `sync` and `answer` refuse to run inside an agent session, as `agent-group --as owner` does
  (the same known limit, [Groups](#groups)).

**Replies written in the tab.** `mailbox answer THREAD --key KEY` (the text piped in, at most 20,000
characters) records the owner's reply to the thread's newest agent mail as an inbound row (Gmail id
`mailbox:KEY`, from the owner's address, the subject `Re: ` and the answered mail's) with the session
the routing slip finds for a reply to that mail. KEY, 16 to 64 letters, digits, `_` and `-` made by the
tab, makes a retry give back the first reply instead of a second. On every poll the daemon routes each
such reply, oldest first, with the code that routes the owner's email: to its session, into a group as
the owner's post, to the orchestrator, or held with the usual notice when delivery fails. It gets no
DKIM check, since it came through no mail server, and no Gmail labels. An agent's `agent-mail reply N`
to it goes out in the Gmail thread of the mail the owner answered. The reply itself reaches only the
session: it is not in the owner's Gmail.

**Files in a reply.** With `--json`, `answer` takes up to 20 files, 25 MiB in all; the text may be empty
when there are files. The reply, text and files, is kept as one message in the store. Before the reply
is handed on, its files are written to `~/.local/state/agent-mail/files/i<inbound id>/` on the machine
of the session that gets it (to the Mac over SSH, within `remote_files_timeout` seconds), and the text
the agent gets ends with a line per file: its path there, type and size. A reply to a group's mail
takes no files.

**Backup.** `agent-mail backup` copies the mailroom to each directory in config `backup.targets` (none
by default, and then it refuses), each on another disk than the state: `messages/` gets every stored
message it lacks, checked by its sha256 once copied; `latest.sqlite` is a consistent copy of
`mailroom.sqlite`, replaced whole; `daily/mailroom-YYYY-MM-DD.sqlite.gz` is the day's first copy,
gzipped. It never deletes anything in a target. It makes a missing target folder but never a folder
above it, so a disk that is not mounted fails the run instead of filling the system disk. It goes on
past a target that fails, exits 1 if any failed, and records each target's last whole copy.
`systemd/agent-mail-backup.timer` runs it hourly; enable the timer only once `backup.targets` is set:

```json
{"backup": {"targets": ["/mnt/backup-disk/agent-mail"]}}
```

With the kit's installer, set `MAIL_BACKUP_DIRS` in `kit.env` instead: it writes `backup.targets` and
installs the timer (empty: no timer).

## Groups

A group is a thread of up to 16 agents, in which every post reaches every member, and the owner may
post. The owner can read a group on the board but does not follow groups, so agents never rely on a post
reaching the owner: what the owner must know, decide or do goes by email. A group has a title, its
members (each a session's name on a machine, `NAME@HOST`), its maker (the owner or an agent) and an
optional link to a task on the owner's board. The owner is in every group and is never listed as a
member. Groups are never deleted; the owner can hide one on the board. Every group lives on the Linux
box, and on the Mac `agent-group` runs each command there over SSH, as `agent-mail` does.

```bash
agent-group new "Data pipeline" --with pipeline-data,pipeline-site@mac   # you, them and the owner
agent-group post GROUP "pipeline-site: the data is in."   # or pipe the text in; --reply-to POST answers one
agent-group inbox [GROUP]          # your unread posts, oldest first, which are then marked read
agent-group inbox --recent         # the last 20 posts, read or not; they are then marked read
agent-group show GROUP             # members, maker, your alert setting and unread count
agent-group alerts GROUP named     # all | named | never
agent-group add GROUP NAME[@HOST],...     # remove the same way
agent-group set GROUP --title "New title" --with NAME[@HOST],...   # either or both; nothing if unchanged
agent-group leave GROUP
agent-group list
```

- **Who you are.** The Claude or Codex session the command runs in (`CLAUDE_CODE_SESSION_ID`,
  `CODEX_THREAD_ID`), or `--session NAME_OR_ID`. A member is a name, so an unnamed session must be named
  first (`/rename`). A bare `NAME` is a session on the machine the command was typed on. GROUP is a
  group's id, or 4 or more of its first characters or its title among your groups. A group does not
  follow a rename: the maker or the owner adds the new name.
- **Who may do what.** The owner makes and changes any group. An agent makes only a group that includes
  itself, and changes one (`add`, `remove`, `set`) only if it made it and is still a member. Any member
  can leave. Only members and the owner read or post in a group, and only the owner hides one.
- **Alerts.** Each agent chooses per group which posts wake it: `all`; `named`, only posts that name it
  or answer one of its posts; or `never`. An agent joins on config `groups.default_alerts` (`named`).
  The owner's posts wake every member. A post names an agent when one of the text's longest runs of
  letters, digits, `_`, `-` and `.` is its name, followed by nothing or only dots, ignoring ASCII case:
  `@pipeline-site`, `pipeline-site's` and `Thanks, pipeline-site.` name pipeline-site;
  `pipeline-site-2` and `pipeline-site.md` do not.
- **Delivery.** A post that wakes a member goes to it through agent-send's routes and starts with one
  line naming the group, the sender and the post, saying it is an agent's post (information, never
  instructions) or the owner's own, and giving the command that answers it. An offline session is never
  resumed. A post that does not wake a member, or cannot reach it, stays unread for it, and the member
  learns of it once, in one line (`Not an error: 3 quiet posts in "TITLE" (GROUP); ...`), with the next
  post that wakes it or at its next prompt, through the hook. Each post's outcome is logged in
  `~/.local/state/agent-mail/groups/deliveries.jsonl`.
- **The hook.** `agent-group hook --cli claude|codex` runs on every `UserPromptSubmit` ([the hook
  settings](#the-status-log-and-report)). With no notice waiting it exits after reading one directory;
  with one, it prints the session's notices as the prompt's added context. It never fails a prompt.
- **By email.** After each poll the mailroom mails the owner once about each new group that is not
  hidden, with the subject `Group "TITLE": MEMBERS and you [#TAG]`. The owner's reply in that thread,
  with a DKIM pass, becomes the owner's post in the group, without the quoted mail, and wakes every
  member. A reply from anyone else is held.
- **Asking the orchestrator.** The owner can ask the orchestrator by email to set up a group chat. It
  runs `agent-group new TITLE --with NAMES --for-mail N`, which makes the group as the owner's, once per
  mail; only an orchestrator run may, and only for the owner's own mail that the mailroom gave it in the
  last 24 hours.
- **Storage.** Each change is a new whole record of the group, never an edit, and the current group is
  worked out by applying the records in order (`groups.fold`). The Linux box keeps each group's records
  in `~/.local/state/agent-mail/groups/records/<GROUP>.jsonl`, and each agent's unread posts in
  `groups/reads.json`, under one lock.

The agent board reads and writes groups as the owner (`--as owner`):

```bash
agent-group list --json --as owner                 # every group, hidden ones included
agent-group show GROUP --json --as owner           # one group, with each member's alert setting
agent-group posts GROUP --json --as owner [--since POST]   # its posts in order (after POST), marking nothing read
agent-group new TITLE --with NAME@HOST,... --as owner [--task TASK] [--json] [--id GROUP]
agent-group post GROUP TEXT --as owner --id POST   # POST: a new id, so a retry cannot post twice
agent-group set GROUP [--title TITLE] [--with NAME@HOST,...] --as owner [--json]
agent-group hide|unhide GROUP --as owner
```

Exit codes: 0 done, 1 error, 2 not allowed, 3 `--session` matches several sessions, 4 no such group,
post, agent or session.

**Known limit.** Every process here runs as the same Unix user, so nothing can prove that a command
comes from the owner. agent-group refuses `--as owner` inside an agent session (when any of
`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`, `CODEX_SESSION_ID` or `AGENT_ORCHESTRATOR` is
set). A process that clears them can still claim to be the owner. Agents' posts never become mail to
the owner.

## agent-send

```bash
agent-send --list                              # address book: both CLIs, live or not
agent-send my-agent "run the tests"            # by name, id, or unambiguous prefix
agent-send orchestrator "..."                  # runs one orchestrator event
agent-send --from mailroom TARGET < body.txt   # message on stdin
agent-send --offline resume TARGET "..."       # wake a session that is not live
agent-send --dry-run TARGET "..."              # resolve and show, send nothing
```

A target is matched in order: exact name, exact id, id prefix (6+ characters), name prefix, the
start of an unnamed session's title, then, for a full id, an exited Claude session (its transcript
gives the folder to resume it in). The orchestrator's name only ever matches exactly. The body starts
with a `[from ... via agent-send; ...]` line saying whether a reply address exists. A Claude session
resumes on the best login by usage ([Usage-aware routing](#usage-aware-routing-and-a-second-claude-login)),
with the newest copy of its conversation copied there. A session whose recorded folder is gone is not
resumed. Exit codes: 0 sent or queued, 1 error, 2 offline, 3 ambiguous, 4 not found, 5 offline with
`--offline resume` and its folder gone.

`--exact-id` (what the mailroom uses) matches the full session id only. If dead registry entries share
it with a live one (a claude killed by a login move leaves its entry), the live one is the session;
with none live, they are one offline session.

## Starting an agent

```bash
agent-spawn --repo myproject --branch fix-login --brief-text "Fix the login redirect; open a PR."
agent-spawn --new-repo scratch-idea --brief brief.md          # private repo under github_owner
agent-spawn --dir ~/code/notes --cli codex < brief.md
agent-spawn --repo myproject --branch x --dry-run --brief-text "..."   # print the plan only
```

- `--repo NAME` is looked up in `repos_root` (or give a path). The worktree goes next to the repo at
  `<parent>/<repo>-<branch>`, branched off `origin/main`; the tmux session is named after the branch.
  If the fetch fails, the branch is made from origin's refs as the last fetch left them, and the output
  and the brief say so.
- `--new-repo NAME` runs `gh repo create <github_owner>/NAME --private --clone` in `repos_root`
  (`--org OWNER` overrides `github_owner`; with neither, it refuses; `--public` for a public one). The
  agent works in a worktree at `<repos_root>/<NAME>-<branch>`, on branch `initial` unless `--branch`
  names one, and the main folder stays on main.
- `AGENT_SPAWN_ROOT` in the environment overrides `repos_root`.
- **Model and effort.** Claude `spawn_claude_model` / `spawn_claude_effort` (default `null`, Claude Code's
  own default model, and `xhigh`), Codex `spawn_codex_model` / `spawn_codex_effort`; `--model` / `--effort` per task. Routed by
  usage, a Claude agent with no `--model` may start on a later model of `claude_fallback_models`
  instead ([the login and model order](#the-login-and-model-order)). Claude runs with
  `--dangerously-skip-permissions`, Codex with `-s danger-full-access -a never`; the folder is
  pre-trusted for both CLIs.
- **Login.** `--account NAME|codex|auto` picks the Claude login or routes by remaining usage.
- **Memory.** Each agent runs in its own memory-capped systemd scope ([Memory caps](#memory-caps)).
- The brief is prefixed with a protocol preamble: who the agent works for (`owner_name`), to record
  milestones with `agent-status note`, to email the owner (`--to owner`) only when done or blocked, to
  read `agent-mail guide` first, and how to close itself. `--no-preamble` sends the brief as given.
- **Codex's start-up dialogs.** agent-spawn answers Codex's update and folder-trust dialogs, and its
  "Hooks need review" question (asked while a hook it runs is new or changed since it was trusted) with
  "Continue without trusting", which leaves those hooks off for that session and says so in the output.
  Trust hooks yourself in Codex's `/hooks` screen to have them run.
- Codex runs the main checkout's `.codex/hooks.json` even in a linked worktree, so `agent-spawn --cli
  codex` (and an `agent-switch` handoff to Codex) refuses to start Codex in a worktree whose
  `.codex/hooks.json` the main checkout lacks or has in another version.
- If the agent does not get its brief, agent-spawn exits 1 with the reason. Attach with
  `tmux attach -t NAME`.

## Closing an agent

When the owner replies "close" (in any words) in an agent's thread, the agent runs
`agent-close --self --dry-run`, reports unlanded work if any, and otherwise ends with
`agent-close --self`. For an agent that is stuck or gone, `agent-close NAME`, or ask the orchestrator.

- **Safety checks.** It refuses (exit 2) while the worktree has uncommitted or untracked files or
  commits not on origin's default branch, or when the session's working directory is unknown;
  `--force` overrides.
- **Cleanup.** It ends the tmux session, then removes the worktree and its branch. Only a linked
  worktree is ever removed; for a main checkout or a plain directory, only the session ends.
- **`--self`** removes first and ends its own session two seconds later, so the agent sees the result.
  It writes a `closed` status event.

## Usage-aware routing and a second Claude login

Optional: with one Claude login and no Codex, none of this is needed.

**Second login.** A second Claude login is its own `CLAUDE_CONFIG_DIR`.
`agent-usage init-account work --config-dir ~/.claude-work` creates it, sharing `CLAUDE.md`,
settings, skills, hooks, plugins and commands with `~/.claude` by symlink while keeping
credentials, sessions and transcripts separate. Then log in once yourself
(`CLAUDE_CONFIG_DIR=~/.claude-work claude`, then `/login`) and add to the config:

```json
{"usage": {"claude_accounts": {"personal": {"config_dir": null, "label": "Claude (personal)"},
                               "work": {"config_dir": "~/.claude-work", "label": "Claude (work)",
                                        "only_owners": ["employer-org"]}},
           "order": ["claude:personal", "claude:work", "codex"],
           "min_remaining": {"session": 10, "weekly": 5}},
 "spawn_route": "auto"}
```

`config_dir: null` is the default login (`~/.claude`). `exclude_owners` / `only_owners` restrict a
login by the repo's GitHub owner (e.g. keep employer repos off a personal plan).

**agent-usage** reads plan usage left (percent remaining per window) from each provider's server
through the CLIs themselves: Claude Code's `get_usage` control request (the 5-hour window, the weekly
window, per-model weekly windows, and extra usage) and Codex's app-server `account/rateLimits/read`.
No prompt is sent, no quota spent; readings are cached for 2 minutes. `agent-usage --pick` prints the
recommended target. With `spawn_route: "auto"` (or `agent-spawn --account auto`), agent-spawn picks the
first place with headroom in the order below; `--account NAME|codex` picks explicitly. Usage keys and
defaults: `agentmail/usage.py` `DEFAULTS`.

**Sessions on two logins.** The registry scans every login's `sessions/` and `projects/`, so
`agent-send`, mail replies, `agent-close`, recovery and the status report reach agents on either
login. Resuming an exited session picks the login for the model it runs: the first in `usage.order`
with headroom for that model, allowed for its repo's owner; the newest copy of its conversation is
copied there first.

### The login and model order

One order places Claude agents, built from `usage.order` and the models `spawn_claude_model`, then
`claude_fallback_models` (config). By default there is one model, Claude Code's own default
(`spawn_claude_model: null`, `claude_fallback_models: []`); to step down between models, name them, for
example `"spawn_claude_model": "claude-fable-5-2", "claude_fallback_models": ["claude-opus-5-5"]`:

1. each Claude login in `usage.order` on the first model;
2. each again on the next model, and so on;
3. Codex, before all of them if `usage.order` lists it before every Claude login, else after them;
4. only when no place has headroom: the first Claude login whose extra usage is on (and under its
   spend limit), on the last model, where the agent bills extra usage.

- **agent-spawn**, routing by usage with no `--model`, starts the agent at the first place with
  headroom, on that place's model. An agent started with `--account` runs `spawn_claude_model`, and
  failover moves it if that is used up.
- **agent-failover** moves an agent that hits a limit to the next place with headroom, from the model
  it started on downwards (a Fable agent may go to Opus, an Opus agent never to Fable), and back once
  the login and model it started on have reset.
- **By hand.** An agent switched with `agent-switch`, or running a model not on the list, keeps its
  model: failover moves it only to another login with headroom for that model, and never where it
  would bill extra usage.

`claude_fallback_models: []` keeps every agent on `spawn_claude_model`; with `spawn_claude_model: null`
(the default) the list is ignored and the model is Claude Code's own default.

## Login failover when an agent hits its limit

`agent-failover` (optional service, one pass every 20 s) watches live Claude agents' transcripts:

- **Detecting it.** When a limit stops a turn, Claude Code records it in the transcript: a plan limit
  (`rate_limit`, `quotaLimits.status: "rejected"`, the window and its reset time); a model's own weekly
  bucket used up with extra usage off (`apiError: "model_requires_usage_credits"`, with no reset time:
  that comes from the login's per-model window in `agent-usage`); or the end of Claude Code's grace
  allowance (only the system line "Usage limit reached · continuing automatically at 4:30pm"; the reset
  is the next such local time). A live agent whose latest such record is newer than its latest good
  reply, with the reset still ahead, is stuck, whatever background-task notices or queued messages came
  after. A hook's message that merely quotes the text is not a limit.
- **Moving it.** It goes to the next place with headroom in [the login and model
  order](#the-login-and-model-order). Its claude is stopped with SIGKILL (background jobs keep running),
  its registry entry removed, its transcript copied to the other login, the folder trusted there and its
  memory linked, and the same session resumed in the same tmux pane with its name, effort and
  permissions (and `--model` when the move changes it) and a note listing its running background
  commands. The session id does not change, so mail replies still reach it. Background sessions
  (`claude --bg`) move with the claude daemon's own commands.
- **Moving it back.** Once the limit that moved it has reset, the agent returns while it is idle (5
  minutes with no turn); a busy agent is never interrupted. An idle agent on a login billing extra usage
  moves to a place with headroom the same way.
- **When nothing has headroom** and no login bills extra usage for it, the agent stays put and Claude
  Code's own auto-continue resumes it at the reset.
- **Limits.** Only agent-spawn's tmux layout and `claude --bg` sessions are moved; at most 4 moves per
  agent per hour; Codex agents are never moved. `failover.pinned` (shell globs of session names,
  default none) keeps agents on their login: a limit leaves them waiting for its reset.
- **Extra-usage mail.** While agents bill a login's extra usage (a window counted for their model is
  used up and extra usage is on), the owner gets a mailroom alert: once when they start, naming the
  agents, the login, the used-up window and the month's extra-usage spend; again each time the
  stretch's spend grows by `failover.spend_step` (100, in the login's currency); and once with the
  total when none bills any more. `failover.spend_mail: false` turns these off.
- **By hand.** `agent-failover move NAME --to work`, `agent-failover status`,
  `agent-failover --once --dry-run`. Settings: config key `failover` (`agentmail/failover.py`
  `DEFAULTS`); `{"enabled": false}` turns it off.

## Switching an agent (CLI, login, model, effort)

`agent-switch` is what the agent board's right-click menu calls:

```bash
agent-switch NAME --options --json     # the agent (cli, login, model, effort, busy), every target, models + efforts
agent-switch NAME --to claude:work --model opus --effort high      # or claude:personal, or codex
agent-switch NAME --model sonnet       # same CLI and login: new model and/or effort
agent-switch NAME --to codex --force   # a busy agent is only switched with --force (its turn is cut off)
```

- **Claude Code to Claude Code** (another login, model or effort): the same conversation continues,
  using agent-failover's move with the new flags, in the same session and tmux pane.
- **Codex to Codex** (model or effort): `codex resume THREAD -m MODEL -c model_reasoning_effort=E` in
  the same pane.
- **Claude Code <-> Codex**: a handoff, because one CLI cannot continue the other's conversation. The
  new CLI starts in the same pane and folder with a brief pointing at a handoff record in
  `~/.local/state/agent-switch/` (the original brief, a condensed record of the conversation and any
  background commands still running). The agent's mail routing tags move to the new session.
- **Models and efforts** come from the CLIs themselves (Claude Code's answer per login, cached for a
  day, and Codex's model cache).
- A switch by hand clears agent-failover's record for the agent, so it is never moved back over the
  owner's choice. Exit codes: 0 switched or nothing to do, 3 busy (needs `--force`), 2 refused before
  anything was stopped, 1 attempted and failed.

## Memory caps

One agent's batch of parallel workers can fill RAM and swap, after which the kernel kills services
one by one and finally the user systemd manager, ending every agent. So every agent `agent-spawn`
starts or `agent-send` resumes runs in its own systemd scope, `agent-NAME.scope`, with
`MemoryMax=agent_memory_max` (default `32G`) and `MemorySwapMax=agent_swap_max` (default `2G`); `"off"`
disables either. A runaway is OOM-killed inside its own scope. `agent-spawn --memory-max 48G
--swap-max off` overrides per launch; raise a running agent's cap with
`systemctl --user set-property --runtime agent-NAME.scope MemoryMax=48G` (the launch output's `memory:`
line names the unit). The tmux server and Claude's background daemon are moved out of any service's
cgroup (into `tmux-server.scope` / `claude-bg-daemon.scope`), so restarting the mailroom cannot kill
agents. A cap that cannot be applied never blocks a launch. No-op without a systemd user bus (macOS).
Not covered: sessions started by hand, Docker containers (charged to `dockerd`), many agents that each
stay under their cap.

## Memory watch

`agent-memwatch` (optional service, every 15 s) charges each process's memory to the agent it descends
from (its nearest Claude or Codex ancestor, the `CLAUDE_CODE_SESSION_ID` in its environment, or a
session sharing its cgroup). At 80% RAM in use it messages the top agents (via `agent-send`) to cut
workers; at 90%, or 80% with swap half full, it messages them again and emails the owner (swap alone
never alerts: pages swapped out under past pressure stay there long after it has gone); an agent near
its own scope cap is warned first. At most one message per agent and level every 30 minutes, one email
an hour. `agent-memwatch --once --dry-run` shows who holds what. Settings: config key `memwatch`
(`agentmail/memwatch.py` `DEFAULTS`, e.g. `{"warn_pct": 85}`).

## Mac requests and replies

There is one polling daemon, on the always-on Linux box. A Mac with its own Claude/Codex sessions
can still send and receive: its `agent-mail send` / `reply` proxy over SSH to the Linux box, which
records the tag against `(mac, cli, session id)` and sends; replies go back over SSH into the exact
live Mac session (never resumed automatically). `agent-group` and `agent-mail inbox`, `show`, `sent`
and `receipt` on the Mac run against the Linux box's records too. Install the kit on both machines (the
Mac needs no Gmail token, but `agent-mail` imports the Google libraries, so install `requirements.txt`
there too).

Linux box `~/.config/agent-mail/config.json`:

```json
{"host_id": "desktop", "remote_hosts": {"mac": "mac"}, "recovery_hosts": ["mac"]}
```

Mac `~/.config/agent-mail/config.json`:

```json
{"host_id": "mac", "mail_proxy": "desktop", "remote_hosts": {"desktop": "desktop"}, "send_enabled": false}
```

`remote_hosts` maps a host id to an SSH alias; both aliases must connect without prompts and with
known host keys (`BatchMode=yes`, `StrictHostKeyChecking=yes`). Payloads are JSON on stdin to
`remote_command` (`~/.local/bin/agent-mail rpc`), never shell-interpolated; one request carries at most
40 MiB. Files the owner attaches to a Mail-tab reply to a Mac session go to the Mac first, in a request
of their own. A proxy send prints a request UUID; after a timeout, run `agent-mail receipt UUID` before
retrying anything (rerun only with `--request-id UUID`). The owner's reply to a Mac session that has
exited goes to the orchestrator ([orphan fallback](#the-routing-slip)). If the Mac is offline or
delivery fails, the reply stays held and the owner gets one notice; then:

```bash
agent-mail show 123
agent-mail release 123 origin                                      # retry the recorded origin
agent-mail release 123 FULL_SESSION_UUID --host mac --cli codex    # explicit handoff
agent-mail recover 123 --query 'exact document title' --refresh    # read-only search, this machine then recovery_hosts
```

Recovery searches session metadata, status notes, transcripts and text docs in recent worktrees,
within fixed time, byte and file budgets, and returns only paths, identities and matching queries.
Matches are leads, never permission to route.

## Configuration

`~/.config/agent-mail/config.json` (no secrets; override the directory with `AGENT_MAIL_CONFIG_DIR`).
Keys left out take these defaults (`agentmail/config.py`); a partial object (e.g. `labels`) keeps the
other keys' defaults:

| Key | Default | Meaning |
|---|---|---|
| `account` | `null` (**required**) | The dedicated agents Gmail address. Sending and polling refuse until it is set. |
| `from_name` | `"Agents"` | From display name: `<from_name> (<session name>)`. |
| `oauth_client_file` | `null` | The OAuth client JSON `agent-mail-auth` uses; `null`: `~/.config/agent-mail/credentials.json`. |
| `allowlist` | `[]` | Owner addresses whose mail is delivered as instructions. Empty: every mail is held. |
| `owner_name` | `"the owner"` | The owner's first name, used in text agents and the reviewer read. Agents may also write `owner` for it. |
| `owner_email` | `null` | Where agents send results (`--to owner`) and alerts go; `null`: first `allowlist` entry. |
| `github_owner` | `null` | GitHub user/org for `agent-spawn --new-repo` (required for it). |
| `repos_root` | `"~/code"` | Where repos and new repos live (`AGENT_SPAWN_ROOT` overrides). |
| `general_repo` | `"~/code/general"` | Repo for tasks that belong to no project (read by the orchestrator brief and to-do tools). |
| `poll_seconds` | `30` | Gmail poll interval. |
| `resume_offline` | `true` | Resume an exited session to deliver a reply. |
| `resume_dangerous` | `true` | Resume with bypassed permissions / full-access sandbox. |
| `orchestrator` | `"orchestrator"` | The orchestrator's name for `agent-send` and in the report. |
| `orchestrator_dir` | `"~/code/orchestrator"` | Working directory of orchestrator runs (brief, `STATE.md`). |
| `orchestrator_model` | `"sonnet"` | Model alias for orchestrator runs. |
| `orchestrator_effort` | `"high"` | Effort for orchestrator runs. |
| `orchestrator_timeout` | `900` | Seconds per orchestrator attempt (and lock wait). |
| `orchestrator_codex_model` | `null` | Codex model when the orchestrator runs on Codex (`null`: `spawn_codex_model`). |
| `orchestrator_codex_effort` | `"high"` | Codex reasoning effort for those runs. |
| `orchestrator_fallback_secret` | `null` | Name of a `bws` secret holding an Anthropic API key, the last resort of the orchestrator and the reviewer; `null` disables it. |
| `bws_token_file` | `"~/.config/bws/token"` | Bitwarden Secrets Manager access token file (fallback only). |
| `bws_project_file` | `"~/.config/bws/project-id"` | File with the `bws` project id (fallback only). |
| `spawn_claude_model` | `null` | agent-spawn's Claude model (`null`: Claude Code's default; e.g. `"claude-fable-5-2"`). |
| `spawn_claude_effort` | `"xhigh"` | agent-spawn's Claude effort. |
| `claude_fallback_models` | `[]` | Models a Claude agent steps down to, in order, e.g. `["claude-opus-5-5"]` ([the login and model order](#the-login-and-model-order)); ignored while `spawn_claude_model` is `null`. |
| `spawn_codex_model` | `"gpt-6.1-sol"` | agent-spawn's Codex model. |
| `spawn_codex_effort` | `"xhigh"` | agent-spawn's Codex effort. |
| `spawn_route` | `"manual"` | `auto`: with no `--cli/--account/--model`, pick CLI and login by usage. |
| `usage` | `{}` | Claude logins, order and thresholds for agent-usage (`agentmail/usage.py`). |
| `review` | see [Pre-send review](#pre-send-review-of-agent-mail) | The pre-send review of agent mail: `enabled`, `mode`, `model`, `owner_context` and the hold settings. |
| `groups` | `{}` | Group chat settings (`agentmail/groups.py`): `default_alerts` (`"named"`). |
| `failover` | `{}` | agent-failover overrides (`agentmail/failover.py`), e.g. `{"enabled": false}`, `pinned`, `spend_mail`. |
| `memwatch` | `{}` | agent-memwatch overrides (`agentmail/memwatch.py`). |
| `agent_memory_max` | `"32G"` | Per-agent MemoryMax; `"off"` for none. |
| `agent_swap_max` | `"2G"` | Per-agent MemorySwapMax; `"off"` for none. |
| `collaborators` | `{}` | `{address: {"name", "owner"}}` of collaborators' agents; `share_data_with` lists the rest of that collaborator's team (their own address, say), so mail to them is reviewed as collaborator mail. |
| `require_dkim` | `true` | Honour owner/collaborator mail only with a DKIM pass for the sender's domain. |
| `collaborator_hourly_cap` | `10` | Collaborator mails per hour before holding. |
| `agent_thread_cap` | `12` | Consecutive agent messages in a thread before the loop guard holds it. |
| `notify_owner_new_collab_thread` | `true` | Notice to the owner when a collaborator starts a thread. |
| `alert_to` | `null` | Where `[mailroom]` alerts and memory emails go; `null`: the owner's address. |
| `alerts_enabled` | `true` | Send `[mailroom]` alerts at all. |
| `send_enabled` | `true` | `false` on a machine that must not send directly (the Mac). |
| `host_id` | `"desktop"` | This machine's identity in routing. |
| `mail_proxy` | `null` | Host id of the central mailroom (set on the Mac). |
| `remote_hosts` | `{}` | Host id to SSH alias for peers. |
| `remote_timeout` | `30` | Seconds per SSH request. |
| `remote_files_timeout` | `120` | Seconds to copy a Mail-tab reply's files (up to 25 MiB) to a peer's session. |
| `remote_command` | `"~/.local/bin/agent-mail rpc"` | What a peer runs over SSH to answer a request. |
| `recovery_hosts` | `[]` | Hosts searched after this one by `agent-mail recover`. |
| `recovery_seconds` | `20` | Time budget per recovery search (max 20). |
| `recovery_days` | `14` | How far back recovery looks (max 30). |
| `backup` | `{"targets": []}` | Directories `agent-mail backup` copies the message store and database to, each on another disk. |
| `labels` | `{"delivered": "mailroom/delivered", "held": "mailroom/held", "skipped": "mailroom/skipped"}` | Gmail label names. |

A minimal config:

```json
{"account": "agents@example.com", "from_name": "Agents", "allowlist": ["owner@example.com"],
 "owner_name": "Alex", "owner_email": "owner@example.com", "github_owner": "example-user",
 "repos_root": "~/code", "orchestrator_dir": "~/code/orchestrator"}
```

### Files

| Path | What |
|---|---|
| `~/.config/agent-mail/credentials.json` | OAuth client (a Google Cloud "Desktop app" client JSON). Read only by `agent-mail-auth`. |
| `~/.config/agent-mail/token.json` | Refresh token for the agents account, mode 600, written by `agent-mail-auth`. Scope `gmail.modify` only. |
| `~/.config/agent-mail/config.json` | The config above. |
| `~/.local/state/agent-mail/mailroom.sqlite` | Routing map, inbound/outbound records, reviews, Mail-tab flags (`AGENT_MAIL_STATE_DIR`). |
| `~/.local/state/agent-mail/messages/` | The message store: every agent mail and Mail-tab reply with files, by sha256. |
| `~/.local/state/agent-mail/files/` | Files of Mail-tab replies, where the agent opens them. |
| `~/.local/state/agent-mail/groups/`, `group-flags/` | Group records, unread posts, delivery log; notice flags for the hook. |
| `~/.local/state/agent-mail/orchestrator-runs.jsonl` | One line per orchestrator run. |
| `~/.local/state/agent-mail/{usage-cache,failover,memwatch}.json` | Optional services' state. |
| `~/.local/state/agent-status/events.jsonl` | The status log (`AGENT_STATUS_DIR`). |
| `~/.local/state/agent-spawn/`, `agent-send/`, `agent-switch/` | Briefs, resume logs, handoff records (mode 700). |

## Manual install

The kit's `install.sh` does all of this; by hand:

1. Python environment: `python3 -m venv ~/.local/share/agent-kit/venv` and
   `~/.local/share/agent-kit/venv/bin/pip install -r mailroom/requirements.txt`.
2. Wrappers in `~/.local/bin`, one per tool in `bin/` (`agent-mail`, `agent-mailroom`, `agent-send`,
   `agent-group`, `agent-status`, `agent-orchestrate`, `agent-spawn`, `agent-close`, `agent-switch`,
   `agent-usage`, `agent-failover`, `agent-memwatch`) plus `agent-mail-auth` for `agent_mail_auth.py`:

   ```sh
   #!/bin/sh
   exec "$HOME/.local/share/agent-kit/venv/bin/python" "/path/to/agent-board-kit/mailroom/bin/agent-mail" "$@"
   ```

   The SSH proxy calls `~/.local/bin/agent-mail` on the peer, and hooks call `~/.local/bin/agent-status`
   and `agent-group`, so the wrappers must be at exactly these paths. Tools that send mail call
   `agent-mail` with their own interpreter (the venv, which holds the Google libraries); the rest are
   standard library only and run on any Python 3.9+.
3. `~/.config/agent-mail/config.json` with at least `account`, `allowlist`, `owner_name`,
   `owner_email` (see above); `mkdir -m 700 ~/.config/agent-mail`.
4. Put the OAuth client JSON at `~/.config/agent-mail/credentials.json`, then run
   `agent-mail-auth` and sign in as the **agents** account. It refuses any other account. On a
   headless machine: `agent-mail-auth --port 8765 --no-browser`, forward the port from your laptop
   with `ssh -L 8765:localhost:8765 <host>`, and open the printed URL there.
5. Hooks and `crossSessionInbound` as in [The status log and report](#the-status-log-and-report),
   including the `UserPromptSubmit` hook for `agent-group`.
6. The orchestrator directory (`orchestrator_dir`) from the kit's `orchestrator/` template.
7. Services: render each `systemd/*.service.in`, replacing `{{PYTHON}}` with the venv's python and
   `{{KIT_DIR}}` with the kit checkout, into `~/.config/systemd/user/` (copy the `.timer` as it is), then:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now agent-mailroom.service
   systemctl --user enable --now agent-memwatch.service     # optional
   systemctl --user enable --now agent-failover.service     # optional, needs a second login
   systemctl --user enable --now agent-mail-backup.timer    # optional, once backup.targets is set
   loginctl enable-linger "$USER"                           # keep user services running without a login
   ```

8. Check: `agent-mail status`, then `agent-mailroom --once --dry-run` (run it while the service is
   stopped; only one instance can hold the lock).

### The unit files

- `agent-mailroom.service`: `KillMode=mixed` sends SIGTERM to the daemon only. Its handler finishes
  the current message (including a running `agent-send` or orchestrator child), records the outcome,
  and exits without starting another; unprocessed messages stay behind the saved history cursor for
  the next poll. After the daemon exits (or the deadline passes) systemd kills what remains.
  `TimeoutStopSec=1h` covers an event waiting for the orchestrator lock, then one run, each bounded by
  `orchestrator_timeout`; if you raise that, keep `TimeoutStopSec` above twice its value. Agents
  themselves are not in this cgroup (see memory caps), so a restart does not touch them.
- `agent-failover.service`: `KillMode=process`, so a restart never takes the agents it relaunched
  with it; `MemoryMax=256M`.
- `agent-memwatch.service`: a separate unit, so it outlives a mailroom crash; `MemoryMax=256M`.
- `agent-mail-backup.service` and `.timer`: a oneshot run of `agent-mail backup`, hourly, and at the
  next boot for a run missed while the machine was off.
- The long-running units set `Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin`, because
  `systemd --user` does not read shell profiles and `claude`, `codex` and the wrappers live in
  `~/.local/bin`.

After changing a unit, `systemctl --user daemon-reload` **before** restarting, and check
`systemctl --user show agent-mailroom.service -p KillMode -p TimeoutStopUSec`. Before restarting the
mailroom, check that no orchestrator event is in flight:
`flock -n ~/.local/state/agent-mail/orchestrator.lock true && echo "no orchestrator event in flight"`.

## Requirements

- Linux with `systemd --user` for the services and memory caps (the tools themselves also run on
  macOS); Python 3.9+; `git`, `tmux`; `gh` for `--new-repo`.
- Claude Code and/or Codex. Codex >= 0.149 for `codex queue`.
- `"crossSessionInbound": "accept"` in `~/.claude/settings.json`.
- Neither CLI is on PATH in cron, systemd or a non-login SSH shell; `agent-send` and friends look in
  `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, then ask a login shell.
- Once Codex knows of a newer release, an interactive start opens on an "Update available" menu
  and waits for a key before loading the thread. agent-spawn and agent-switch pass
  `-c check_for_update_on_startup=false` (`registry.CODEX_NO_UPDATE_PROMPT`) so their launches never
  wait there; the board's `agent-cli-update` timer keeps both CLIs current instead.

## Tests

Every test module imports `tests/isolate_state.py` first, which points the state, config and status
directories at fresh temporary ones and writes a test identity (owner Alex, `agents@example.com`) into
the temporary `config.json`; no test needs Gmail, the network, a real session, the default tmux server
or systemd. From the kit's root, with the kit venv's python (the Gmail-facing suites import the Google
libraries):

```bash
PYTHON=~/.local/share/agent-kit/venv/bin/python scripts/run-tests.sh mailroom
```

or one module: `cd mailroom && python3 tests/test_review.py`. `test_memcap.py`'s `RealScope` class
starts real scopes in the systemd user manager and is skipped unless `MEMCAP_REAL_TESTS=1`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Error: config `account` is not set | Add `account` to `~/.config/agent-mail/config.json`. |
| `no Gmail token at ...` | Run `agent-mail-auth` once, signed in as the agents account. |
| `invalid_grant` about a week after setup | An OAuth consent screen left in "Testing" status expires refresh tokens after 7 days. Set it to "In production" (the unverified-app warning remains; fine for your own accounts) and rerun `agent-mail-auth`. |
| Every mail is held: `sender not on allowlist` | Add the owner's address to `allowlist`; then `agent-mail inbox --held` and `agent-mail release N TARGET`. |
| `DKIM check failed` | The sender's domain did not DKIM-sign, or a forwarding hop broke the signature. Such mail is never released (`agent-mail release` refuses it); send it again directly from your provider. Persistent for a real sender: `require_dkim: false` (weakens forgery protection). |
| A Claude session never receives messages | `crossSessionInbound: "accept"` missing, or the session started before it was set. |
| A queued Codex message never appears | Codex older than 0.149, or a dialog (e.g. a rate-limit prompt) is open in the session. Try `codex app-server daemon start` and relaunch the session. |
| A Codex agent's hooks do not run | Codex left them untrusted ("Continue without trusting"); trust them in Codex's `/hooks` screen. |
| `agent-send` exit 3 (ambiguous) | Two live sessions share a name or prefix; use the full id from `agent-send --list`. |
| Orchestrator mail held, "orchestrator failed" | `agent-orchestrate --runs`; does `orchestrator_dir` exist? Is `claude` logged in? Then `agent-mail rerun N`. |
| Daemon down | `systemctl --user status agent-mailroom`, `journalctl --user -u agent-mailroom -f -o cat`, `agent-mail status`. |
| Old mail suddenly processed / history gap | When Gmail's history id expires, the daemon searches the inbox from the last successful poll and alerts the owner. `agent-mailroom --once --baseline` (service stopped) resets the cursor to now. |
| A reply was not delivered to a Mac session | `agent-mail show N`; Mac reachable with `ssh mac true`? Then `agent-mail release N origin`. |
| A group post never reached an agent | `agent-group show GROUP` (its alert setting), `~/.local/state/agent-mail/groups/deliveries.jsonl`; is the `UserPromptSubmit` hook installed? |
| `agent-mail backup` refuses | Set `backup.targets` to directories on another disk. |
| The Mail tab shows old mail as text only | Run `agent-mail mailbox backfill` once. |
| Agent killed at its memory cap | Relaunch with a bigger `--memory-max`, or raise `agent_memory_max`. |
