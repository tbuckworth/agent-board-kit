# 6. The to-do list

Your to-do list is the issues of one private GitHub repo (`TASKS_REPO`). Labels carry the list,
importance, urgency, due date and agent status; the body carries notes, a `## Checklist` and
agents' `## Claude Resolution Notes`. Three things use it:

- **You**, through the web app (any browser, phone included) or the Mac menu-bar app.
- **Agents**, through `gh issue ...`: the global instructions tell every agent how to add a task when
  you ask ("add to my to-do: renew passport") and how to report on one it was given.
- **Assign to agent**: from either app, a task becomes an orchestrator event that starts one agent on
  it. The orchestrator comments `<!-- todo-agent:started -->` with the agent's name; the agent comments
  `<!-- todo-agent:done -->` (or `blocked`) when it finishes, and emails you. Or pick an agent that is
  already running (the picker lists the board's live agents): the task goes straight into its session
  with `agent-send`, as a message from you, and it answers by email.

Full reference (schema, every label, the web app's token and hosting, the Mac app, the assign flow end
to end, agent rules): [todo/README.md](../todo/README.md).

## Setup in five steps

1. **Create the repo** and seed it with the template (`.todo/config.json` defines groups of lists):

   ```bash
   gh repo create alex-gh/tasks --private --description "My to-do list"
   git clone https://github.com/alex-gh/tasks ~/code/tasks && cd ~/code/tasks
   cp -r ~/code/agent-board-kit/todo/tasks-repo-template/. . && git add -A && git commit -m seed && git push
   ```

2. **Create the labels** (30 of them; safe to re-run): `./install.sh --labels`, or
   `todo/setup-labels.sh [--dry-run] [--list NAME]... alex-gh/tasks`.

3. **The desktop helper** `todo-desk` is installed and configured by `./install.sh`
   (`~/.config/todo-desk/config.json` names the repo). Check: `todo-desk config`,
   `todo-desk snapshot --pretty | head`.

4. **The web app** is static files in `todo/web/`. Serve them anywhere you can reach from your phone:
   `python3 -m http.server 8080 --bind <tailnet IP> --directory ~/code/agent-board-kit/todo/web` on a private network
   (Tailscale), Caddy, or GitHub Pages from a repo of its own. The page has no secrets: you paste a
   **fine-grained personal access token** limited to the tasks repo (Issues read/write, Contents
   read/write) into the page, and it stays in that browser. Make the token from the same GitHub
   account the desktop's `gh` uses: dispatch only trusts requests written by that account.

5. **Dispatch** (web "Assign to agent" requests): `./install.sh --start` enables the
   `todo-dispatch.timer` (every 2 minutes). The Mac app assigns directly and does not need it.
   Dispatch also publishes the running agents to `.todo/agents.json` in the tasks repo, so the web
   app can offer them: keep the tasks repo private.

The Mac app: see [07-mac.md](07-mac.md).

## Using it with agents

- "Add buy printer ink to my to-do" in any agent session creates an issue with `source:claude`.
- Email the orchestrator "work on to-do #42" or press **Assign to agent**; add instructions in the
  sheet. The orchestrator names the agent for the task, as it names agents it starts from email (2 to
  4 words, such as `invoice-export-fix`), and writes the name in its "started" comment on the issue,
  which is how the board and the apps match agent to task.
- Double-click an agent's name on a task (its row, the task panel, the Triage card) in the Mac app to
  open the newest mail that agent sent you, in Gmail: the thread itself when the board's Gmail
  read-check is on (`gmail_credentials`), otherwise a Gmail search for that message.
  `todo-desk agent-mail N` shows what it finds.
- Agents never close an assigned task; you tick it off. They draft, but do not send, buy or book,
  until you say yes (by email or the `approved` label).

## Triage, importance and urgency

**Triage**, in the web app and at the top of the Mac app's sidebar, shows one task at a time from your
rated lists (unrated, important and long-untouched tasks come up most), with one key each for Archive
(closed as "not planned", still found under its list), This week (at most five), Snooze, Done, Delete,
Agent, importance, Urgent and a due date; every action can be undone. Lists in `unratedGroups` /
`unratedLists` of the tasks repo's `.todo/config.json` (catalogues: books, films) get no rating and
stay out of Triage, in both apps.

Tasks are rated by **importance** (High, Medium, Low, None), **urgent** and **due**. The old
`priority:` label is retired: no app writes it and any edit removes it; a task rated only with
`priority:` shows as unrated until you rate it. `setup-labels.sh` still creates the `priority:` labels
so old tasks keep a described label. Details: [todo/README.md](../todo/README.md), "The Mac app" and
"`.todo/config.json`".
