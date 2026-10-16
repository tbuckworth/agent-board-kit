# To-do: a GitHub Issues task list with web and Mac clients and "Assign to agent"

A personal to-do list whose backend is GitHub Issues in a private `tasks` repo. Each open issue is a task;
labels say which list, section and tags it has, how important and urgent it is, whether it is waiting on
someone, and where an agent is with it. Three things read and write it:

- **The web app** (`web/`): a dependency-free static site (no build step) that talks to GitHub's GraphQL API
  from the browser with a fine-grained personal access token (PAT) kept in that browser. It works on a phone.
- **The Mac app** (`mac/`): a SwiftUI menu-bar app. It holds no token: it keeps one `ssh desktop todo-desk serve`
  open and the desktop's helper talks to GitHub with the desktop's `gh` login. It caches every task and queues
  edits made offline.
- **`todo-desk`** (`bin/todo-desk`, `desk/todo_desk.py`, Python stdlib): the desktop helper. It serves the Mac app,
  starts agents for tasks ("Assign to agent"), and every 2 minutes turns agent requests from the web app into
  assignments (`todo-desk dispatch`).

"Assign to agent" hands a task to the kit's orchestrator (`agent-orchestrate`, from `mailroom/`), which starts
one agent for it with `agent-spawn`. The agent reports back on the issue, so the apps show its progress, and
by email.

```text
 browser / phone ── HTTPS + PAT ──────────────────────────────►  GitHub Issues: OWNER/tasks
                                                                  ▲   (labels, comments,
 Mac: ToDo.app ── ssh desktop ~/.local/bin/todo-desk serve ──┐    │    .todo/config.json)
                                                              ▼    │
                                      desktop: todo-desk ── gh auth token
                                         │  assign / dispatch (timer, every 2 min)
                                         ▼
                                agent-orchestrate ──► agent-spawn ──► an agent (comments on the issue,
                                                                       emails the owner)
```

## What is in this folder

| Path | What |
|---|---|
| `bin/todo-desk` | the desktop helper's entry point (the installer puts a wrapper in `~/.local/bin`) |
| `desk/todo_desk.py`, `desk/tests/` | the helper and its unit tests |
| `web/` | the web app (`index.html`, `js/`, `css/`, Inter fonts under the SIL OFL, `tests/`) |
| `mac/` | the Mac app (SwiftPM; `build-app.sh`, `install.sh`, `Info.plist`) |
| `scripts/mac-build.sh`, `scripts/mac-install.sh` | build and install the Mac app on a Mac from the Linux desktop over ssh |
| `setup-labels.sh` | creates the label set in the tasks repo |
| `tasks-repo-template/` | a README and an example `.todo/config.json` to seed the tasks repo with |
| `systemd/todo-dispatch.service.in`, `systemd/todo-dispatch.timer` | runs `todo-desk dispatch` every 2 minutes |
| `config.example.json` | an example `~/.config/todo-desk/config.json` |

## The tasks repo

### An issue

- **Title**: the task text.
- **Body**: optional notes, then optional sections, each with a `## ` heading. A task waiting on someone has
  `Waiting on: <who>` as its first line. Two sections are special:
  - `## Checklist`: `- [ ] item` / `- [x] item` lines. Every client shows and ticks them.
  - `## Claude Resolution Notes`: what an agent did and found. Agents append here and keep the rest of the body.

  Any other `## ` section survives edits from every client. The Mac app merges concurrent edits section by
  section and only reports a conflict when the same section changed on both sides.
- **State**: open is active; closed as "completed" is done; closed as "not planned" is archived (the list label
  stays, so it is still found under its list). Deleting from an app closes the issue and removes its `list:`
  label; the issue itself stays on GitHub.
- **Comments**: the agent markers below.
- **Groups**: a task can be a group of tasks. Its tasks are GitHub sub-issues of it and also carry the
  `subtask` label (linking alone does not change a task's `updatedAt`, which every client syncs by).

### Labels

`setup-labels.sh` creates the fixed ones (30 labels, with `list:tasks`); the clients create `list:NAME`,
`group:NAME`, `tag:NAME`, `due:YYYY-MM-DD` and `follow-up:YYYY-MM-DD` labels themselves when a new value is needed.

| Labels | Meaning | Read / written by |
|---|---|---|
| `list:tasks`, `list:NAME` | the list (one per task); `tasks` is the default and cannot be deleted | all clients, agents |
| `section:personal`, `section:work`, `section:home`, `section:health`, `section:finance` (or any `section:NAME`) | a cross-cutting filter (one per task) | all clients |
| `group:NAME` | the list's group from `.todo/config.json`, copied onto the task when it is saved | all clients |
| `action:buy`, `action:email`, `action:look-into`, `action:call`, `action:manual` | the kind of action, for agents; no `action:` label means manual. Neither app shows or sets it any more; edits keep it | you (on GitHub), agents |
| `tag:NAME` | free-form tags, any number per task (`tag:home` for what can only be done at home); lower case, hyphenated | web, Mac |
| `importance:high`, `importance:medium`, `importance:low` | how much it matters (one per task) | web, Mac, agents |
| `urgency:high`, `urgency:low` | how soon (one per task); a due date within 14 days also counts as urgent | web, Mac, agents |
| `due:YYYY-MM-DD` | due date (one per task) | web, Mac |
| `priority:high`, `priority:medium`, `priority:low` | legacy priority; the Mac app's editor sets it, the web app's editor removes it | Mac |
| `source:app`, `source:claude`, `source:import` | where the task came from (`source:app` is added to tasks made in an app) | all clients, agents |
| `status:agent-requested` | the web app asked for an agent | web writes, `todo-desk dispatch` removes |
| `status:agent-assigned` | an agent was started on it | `todo-desk` |
| `status:claude-processed` | an agent has worked on it | agents |
| `status:waiting` | waiting on someone; the body's first line says who (`Waiting on: <who>`). Greyed and sorted last until its follow-up date | web, Mac, agents |
| `follow-up:YYYY-MM-DD` | when to chase a waiting task (one per task); from that day it shows as "Follow up with <who>" | web, Mac |
| `status:waiting-cleared` | an agent thinks the wait is over (it swaps `status:waiting` for this and writes its evidence in the Claude notes); the task shows "Came through?" until you answer OK or Still waiting | agents set, web and Mac clear |
| `approved` | you approved the outside-world step an agent prepared | you |
| `subtask` | it is in a group (a sub-issue) | web, Mac (`todo-desk`) |
| `focus:week` | one of at most five tasks in "This week" (web Triage) | web |

`list:`, `section:`, `group:`, `action:`, `priority:`, `importance:`, `urgency:`, `due:` and `follow-up:` are single-valued:
setting one replaces any other with the same prefix. Every client keeps labels it does not manage.

### `.todo/config.json` (optional)

Groups the lists in the sidebars, orders them, and names the lists that are not rated. See
`tasks-repo-template/.todo/config.json` and `tasks-repo-template/README.md`:

```json
{
  "groups": [{ "name": "Home", "sortOrder": 1 }],
  "listMetadata": [{ "listName": "to-buy", "groupName": "Home", "sortOrder": 0 }],
  "unratedGroups": ["Reading"],
  "unratedLists": []
}
```

The Mac app changes it through `todo-desk` (as operations on a fresh copy, so concurrent edits survive); the web
app rewrites it when a list is deleted. Lists in `unratedGroups` / `unratedLists` get no rating controls and stay
out of Triage. Without the file every list is ungrouped and rated.

## Install

The kit's top-level `install.sh` does the desktop part (wrapper, config file, timer). The steps below are what it
does, for a manual install or for checking it.

### 1. Create the tasks repo and its labels

```bash
gh repo create alex-gh/tasks --private --description "My to-do list"
git clone git@github.com:alex-gh/tasks.git && cd tasks
cp -r ~/code/agent-board-kit/todo/tasks-repo-template/. .
git add -A && git commit -m "Seed the to-do repo" && git push
cd ~/code/agent-board-kit/todo
./setup-labels.sh --dry-run alex-gh/tasks      # look first
./setup-labels.sh alex-gh/tasks                 # create or update the 30 labels; safe to rerun
./setup-labels.sh --list to-buy --list chores alex-gh/tasks   # optional: more lists up front
```

Keep the repo private: issues are your tasks.

### 2. The desktop helper

On the always-on Linux machine ("the desktop"):

1. `gh auth login` (with the `repo` scope, the default). The helper uses `gh auth token`; `GH_TOKEN` or
   `GITHUB_TOKEN` in its environment take precedence.
2. Write `~/.config/todo-desk/config.json` (see `config.example.json`):

   | Key | Default | Meaning |
   |---|---|---|
   | `repo` | none (required); `$TODO_REPO` if the file has none | the tasks repo, `owner/name` |
   | `owner_name` | the mailroom config's `owner_name`, else "the owner" | your name, in the orchestrator message and issue comments |
   | `owner_email` | the mailroom config's `owner_email`, else its `allowlist[0]` | where agents email results (`agent-mail send --to`) |
   | `repos_root` | the mailroom config's `repos_root`, else `~/code` | where the orchestrator looks for a task's repo |
   | `general_repo` | the mailroom config's `general_repo`, else `<repos_root>/general` | where an agent starts for a task with no repo of its own |

   The mailroom config is `~/.config/agent-mail/config.json` (or `$AGENT_MAIL_CONFIG_DIR/config.json`).
3. Put a wrapper on `PATH`: `~/.local/bin/todo-desk` that runs `exec python3 /path/to/agent-board-kit/todo/bin/todo-desk "$@"`
   (the installer writes it with the kit's venv python).
4. Check it:

   ```bash
   todo-desk config                        # the settings in effect; exit 2 if "repo" is unset
   todo-desk snapshot --pretty | head      # syncs and prints what the Mac app would receive
   todo-desk message 12 --text "test"      # the orchestrator message for task #12; sends nothing
   todo-desk dispatch --dry-run            # which web requests would start an agent; changes nothing
   ```

5. Start `todo-desk dispatch` every 2 minutes, either as a systemd user timer (the installer renders
   `systemd/todo-dispatch.service.in` with `{{PYTHON}}` and `{{KIT_DIR}}`):

   ```bash
   # todo-dispatch.service is todo-dispatch.service.in with {{PYTHON}} and {{KIT_DIR}} filled in
   sed -e "s|{{PYTHON}}|$(command -v python3)|" -e "s|{{KIT_DIR}}|$HOME/code/agent-board-kit|" \
       systemd/todo-dispatch.service.in > todo-dispatch.service
   cp todo-dispatch.service systemd/todo-dispatch.timer ~/.config/systemd/user/
   systemctl --user daemon-reload && systemctl --user enable --now todo-dispatch.timer
   ```

   or as a user crontab line (`crontab -e`, after `mkdir -p ~/.local/state/todo-desk`). Cron's PATH is short,
   so set it (cron does not expand `~` or `$HOME` there):

   ```cron
   PATH=/home/alex/.local/bin:/usr/local/bin:/usr/bin:/bin
   */2 * * * * todo-desk dispatch >> $HOME/.local/state/todo-desk/dispatch.log 2>&1
   ```

   Only needed for the web app's "Assign to agent"; the Mac app assigns directly.

Environment variables the helper reads: `TODO_REPO`, `TODO_DESK_CONFIG_DIR` (default `~/.config/todo-desk`),
`AGENT_MAIL_CONFIG_DIR` (default `~/.config/agent-mail`), `TODO_DESK_CACHE` (default
`~/.cache/todo-desk/cache.json`), `TODO_DESK_STATE` (default `~/.local/state/todo-desk`), `GH_TOKEN`/`GITHUB_TOKEN`.

### 3. The web app

**Token.** Create a fine-grained PAT at GitHub > Settings > Developer settings > Personal access tokens >
Fine-grained tokens:

- Resource owner: your account. Repository access: **Only select repositories**, just the tasks repo.
- Repository permissions: **Issues: Read and write**; **Contents: Read and write** (Read-only works, except that
  deleting a list then cannot remove it from `.todo/config.json`). Metadata: Read is added automatically.
- Pick an expiry you are happy to renew.

Create it from the same GitHub account the desktop's `gh` is logged in as: `todo-desk dispatch` only acts on
agent requests written by that account.

**Hosting.** The app is plain static files with no secrets in them; serve `todo/web/` with any static server
over http(s) (not `file://`: it uses ES modules):

- On a private network such as a tailnet: `python3 -m http.server 8080 --bind <the machine's tailnet IP> --directory ~/code/agent-board-kit/todo/web`
  (run it as a systemd user service or in tmux to keep it up).
- Caddy:

  ```caddyfile
  :8080 {
      root * /home/alex/code/agent-board-kit/todo/web
      file_server
  }
  ```

- GitHub Pages: publish a copy of `todo/web/` from a repository of its own (Pages serves a branch's root or
  `/docs`). Anyone can then load the page, but it shows nothing without a PAT; the PAT is entered in the browser
  and stays in that browser's `localStorage` (or `sessionStorage` with "Remember me" off). It never reaches the
  host. The page's Content-Security-Policy only allows connections to `api.github.com`.

**First run.** Open the page, enter the tasks repo (`owner/name`) and the PAT, and press Connect. Both can be
changed later in Settings > GitHub Connection; changing the repo clears the browser's cache of the old repo's
tasks. Browser storage: `localStorage` keys `todo_repo`, `todo_pat`, `todo_remember`, `todo_theme` and a few view
settings (folded groups, the tag filter, Triage snoozes), and the IndexedDB database `todo-web` (the task cache).

**What it does.** Lists and groups, search, add/edit/complete/archive/delete, checklists, Claude notes,
importance/urgency/due dates, tags (with an only/hide filter), waiting on someone (with a follow-up date),
multi-select with bulk actions and groups of tasks, Triage (one task at a time,
with snoozes and a "This week" set of at most five), and Assign to agent (as a request; see below). Reads are
cached in IndexedDB; writes need the network.

`web/sw.js` (a service worker) is shipped but not registered by `index.html`. If you register it for offline
use, bump its `CACHE_NAME` on every change; `web/tests/sw.test.js` checks its file list.

### 4. The Mac app

Needs macOS 13+, the Xcode Command Line Tools (`xcode-select --install`; Xcode itself is not needed), and an ssh
alias for the desktop that logs in without a prompt (key-based, e.g. `Host desktop` in `~/.ssh/config`).

On the Mac, from a copy of the kit:

```bash
cd agent-board-kit/todo/mac
./build-app.sh                          # swift build, then build/ToDo.app (ad-hoc signed)
./install.sh                            # /Applications/ToDo.app and a login item; safe to rerun
TODO_HOST=my-linux-box ./install.sh     # also set the desktop's ssh alias (default: desktop)
```

Or from the desktop, over ssh to the Mac (it rsyncs `mac/` into a scratch folder on the Mac and builds there):

```bash
MAC_HOST=mac scripts/mac-install.sh                     # build + install
MAC_HOST=mac MAC_BUILD_DIR=todo-mac-build scripts/mac-build.sh   # build only
TODO_HELPER=/path/to/todo-desk scripts/mac-install.sh   # point the app at another helper (e.g. a worktree)
```

`MAC_BUILD_DIR` (default `~/todo-mac-build` on the Mac) is overwritten with `rsync --delete`: never point it at
a folder you edit.

Settings live in `defaults` for `org.agentkit.todo`: `host` (default `desktop`; also in the menu-bar item's
right-click menu), `helper` (default `~/.local/bin/todo-desk`, run on the desktop), and window state (`zoom`,
`list`, `expandedGroups`, `foldedTaskGroups`, `tagFilter`). The cache is `~/Library/Application Support/ToDo/cache.json`.

Keys: ⌥⌘T (anywhere) shows or hides the window; ⌘N add; ⌘F search; ↑↓ or j/k move; space complete or reopen;
return or ⌘E edit; ⌘⌫ delete; ⇧⌘A assign to an agent; ⌘Z undo; ⌘R check GitHub now; ⇧⌘R fetch everything again;
⌘+ / ⌘- / ⌘0 zoom; ⇧-click / ⌘-click / ⌘A select several (then ⌘G groups them); esc clears the selection.

Checks from a terminal on the Mac:

```bash
/Applications/ToDo.app/Contents/MacOS/ToDo --selftest          # offline logic checks
/Applications/ToDo.app/Contents/MacOS/ToDo --probe             # connect as the app does and print what arrives
/Applications/ToDo.app/Contents/MacOS/ToDo --selftest-remote   # creates, edits and deletes a dummy task on GitHub
```

`--assign-test` presses Assign to agent on a new dummy task and so starts a real (small) agent; use it only when
that is the point. Every mode accepts `-helper PATH` to try another helper without changing the settings.

Uninstall: `launchctl bootout gui/$(id -u)/org.agentkit.todo; rm ~/Library/LaunchAgents/org.agentkit.todo.plist;
rm -rf /Applications/ToDo.app ~/Library/Application\ Support/ToDo; defaults delete org.agentkit.todo`.

## Assign to agent, end to end

From the **Mac app** (⇧⌘A, or Agent in the selection bar):

1. The sheet takes optional instructions and previews the exact orchestrator message
   (`assign_preview`).
2. `todo-desk assign` re-reads the issue and refuses if it is closed, or already has `status:agent-assigned`
   (unless "Assign again"). It adds `status:agent-assigned` and posts a comment starting
   `<!-- todo-agent:assigned -->` with your instructions quoted.
3. It runs `agent-orchestrate --source todo` in the background with the message on stdin; the output goes to
   `~/.local/state/todo-desk/orchestrate-N-<time>.log`. If that fails at once, it removes the label and comments
   `<!-- todo-agent:failed -->` with the reason.

From the **web app** (Assign to agent on a task, or `g` in Triage):

1. The browser posts a comment whose first line is `<!-- todo-agent-request -->` (then your instructions, or
   "No extra instructions.") and adds `status:agent-requested`.
2. Within 2 minutes `todo-desk dispatch` on the desktop finds open issues with that label and removes the label
   (so a request is dispatched once). It takes the newest request comment written by the desktop's own `gh`
   account; request comments from anyone else are ignored, and if there is none from that account it starts
   nothing and comments a `failed` marker. Then it runs the same assign as the Mac.

The **orchestrator message** asks for one agent and tells the orchestrator to:

- start it in the task's repo under `repos_root` on a new branch off `origin/main`, or a new repo
  (`agent-spawn --new-repo`) if the task plainly needs one, or else in `general_repo`;
- name it `todo-N-<slug of the title>` (so the board and the apps can match it to the task);
- comment on the issue `<!-- todo-agent:started -->` then `Agent started: NAME (REPO@BRANCH, ACCOUNT)`;
- email you (`owner_email`) the agent's name, repo and account; on failure comment `<!-- todo-agent:failed -->`.

The **agent's brief** (written by the orchestrator from the message) says: read the issue with its comments
(`gh issue view N --repo OWNER/tasks --comments`); get your explicit yes before anything with outside
consequences; when done or blocked, append under `## Claude Resolution Notes`, add `status:claude-processed`, and
comment `<!-- todo-agent:done -->` (or `<!-- todo-agent:blocked -->`) with a summary and links; leave the issue
open for you to close; report with `agent-status note` and email you.

The **Mac app** reads the marker comments: an assigned task shows the agent's status (assigned, started, done,
blocked, failed) and name, and it lists them under "With agents". The web app shows only the label state
(`status:agent-requested` / `status:agent-assigned`). Keep the marker format exactly:
`<!-- todo-agent:assigned|started|done|blocked|failed -->` and `<!-- todo-agent-request -->`, on the comment's
first line.

Assign to agent needs the desktop reachable at that moment (the Mac never queues it), `agent-orchestrate` on the
desktop (`~/.local/bin` or `PATH`), and `gh` on the desktop able to comment on the tasks repo (the orchestrator
and the agent use it too).

## Rules for agents working on tasks

These belong in the global agent instructions the kit installs, so every agent follows them:

1. Agents act on a task when it is assigned to them (through the orchestrator) or when you ask directly. No agent
   sweeps the list on a schedule.
2. Never act on an `action:manual` task beyond what you asked for.
3. Safe work (research, summaries, drafts, code on a branch) can be done directly.
4. `action:email` and `action:buy` (and anything else with outside consequences: sending as you, buying, booking,
   posting, submitting a form, spending money) end in a draft or a recommendation. The step itself waits for your
   explicit yes, by email or the `approved` label.
5. Results go under `## Claude Resolution Notes` in the body, appended, with the rest of the body unchanged.
6. Add `status:claude-processed` after working on a task, and comment with the `done` or `blocked` marker if it
   was assigned.
7. Leave assigned tasks open; you close them. A task an agent fully completed on its own initiative (for example
   one it created) may be closed by it.
8. Keep labels the agent does not manage; replace single-valued ones (`list:`, `importance:`...) rather than
   adding a second.
9. A task an agent adds goes in with `gh issue create --repo OWNER/tasks --title "..." --label "list:tasks,source:claude"`
   (plus `action:*`, `importance:*`, `urgency:*` when known).
10. A task waiting on someone (`status:waiting`) whose wait an agent finds is over (the reply came, the event was
    booked): swap `status:waiting` for `status:waiting-cleared`, keep the `Waiting on:` line and any `follow-up:`
    label, and write the evidence under `## Claude Resolution Notes`. The apps then ask you to confirm. The kit
    ships no scheduled checker for this; ask an agent, or schedule one yourself.

## Tests

```bash
cd todo
HOME=$(mktemp -d) python3 -m unittest discover -s desk/tests    # the helper: 51 tests, no network, fake GitHub
node --test web/tests/*.test.js                                 # the web app: 49 tests (Node 18+, no npm install)
./setup-labels.sh --dry-run alex-gh/tasks                  # prints the gh commands only
```

The Mac app has no tests that run off a Mac; `ToDo --selftest` covers its model logic on the Mac.

## Troubleshooting

- **`todo-desk: no tasks repo configured`**: set `"repo": "owner/name"` in `~/.config/todo-desk/config.json`
  (`todo-desk config` shows what it reads).
- **`gh is not logged in on the desktop` / 401**: run `gh auth login` on the desktop as the account that owns or
  can write the tasks repo. Under ssh the helper looks for `gh` on `PATH`, then `~/.local/bin`, `/usr/local/bin`,
  Linuxbrew, `/snap/bin` and `/usr/bin`.
- **Mac app says "todo-desk not found on the desktop"**: the `helper` path is wrong or the wrapper is missing;
  `ssh desktop '~/.local/bin/todo-desk config'` must work without a password prompt.
  `defaults write org.agentkit.todo helper /path/to/todo-desk` changes it.
- **Mac app keeps reconnecting**: `ToDo --probe` on the Mac prints what arrives. The helper pings every 20 s and
  exits after 12 hours (the app reconnects).
- **Web app: "Could not resolve to a Repository"**: the repo name is wrong, or the PAT was not given access to it.
- **Web app: list deletion comes back**: the PAT has Contents read-only; give it Contents read and write.
- **Web app shows old code after an update**: hard-refresh (Shift-reload); browsers may hold JS and CSS for a while.
- **A web "Assign to agent" never starts**: check the timer (`systemctl --user list-timers todo-dispatch.timer`,
  `journalctl --user -u todo-dispatch`) or the cron log, then `todo-desk dispatch --dry-run`. "no request comment"
  means the request was written by a different GitHub account from the desktop's `gh` login.
- **Assign fails with "agent-orchestrate exited"**: read the log named in the error
  (`~/.local/state/todo-desk/orchestrate-N-*.log`); the mailroom's README covers `agent-orchestrate`.
- **Changed the tasks repo**: the helper ignores a cache written for another repo and does a full sync. On the
  Mac, quit ToDo and delete `~/Library/Application Support/ToDo/cache.json`; the web app clears its cache itself.
- **Edit conflicts in the Mac app**: a banner offers Keep mine (overwrite) or Keep GitHub's (drop the edit).

State on the desktop: `~/.cache/todo-desk/cache.json` (the issue cache), `~/.cache/todo-desk/done-ops.json`
(queued Mac edits already applied, so a retry is not applied twice), `~/.local/state/todo-desk/actions.jsonl`
(assign and dispatch audit log) and `orchestrate-*.log`. All can be deleted; the next sync rebuilds the cache.
