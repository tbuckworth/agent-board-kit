# tasks

This private repository is a to-do list. Each open issue is a task; a closed issue is a finished task (or an
archived one, if it was closed as "not planned"). The to-do clients from agent-board-kit (the web app, the Mac
app and the `todo-desk` helper) and the agents read and write it. Nothing in the repository's files is needed
except `.todo/config.json`; the desktop also writes `.todo/agents.json` (below).

## An issue

- **Title**: the task.
- **Body**: free notes first (a task waiting on someone starts with a `Waiting on: <who>` line), then optional
  sections, each starting with a `## ` heading:

  ```markdown
  Call before Friday; the number is in the last email.

  ## Checklist
  - [ ] find the reference number
  - [x] check the opening hours

  ## Claude Resolution Notes
  What an agent did and found, with links. Agents append here; they keep the rest of the body as it is.
  ```

  Any other `## ` section is kept as it is by every client.
- **Comments**: the agent workflow posts comments whose first line is a marker:
  `<!-- todo-agent-request -->` (the web app asking for a new agent) or
  `<!-- todo-agent-request to=SESSION_ID -->` (asking for the task to go to that running agent), then
  `<!-- todo-agent:assigned -->`, `<!-- todo-agent:started -->`, `<!-- todo-agent:done -->`,
  `<!-- todo-agent:blocked -->` or `<!-- todo-agent:failed -->`. The apps show the agent's progress from them.

## Labels

| Label | Meaning |
|---|---|
| `list:NAME` | the list the task is in (one per task; `list:tasks` is the default list) |
| `section:NAME` | a cross-cutting section such as `section:home` (one per task) |
| `group:NAME` | the list's group, copied from the config by the clients when a task is saved (one per task) |
| `action:buy`, `action:email`, `action:look-into`, `action:call`, `action:manual` | what kind of action it is, for agents; no `action:` label means manual (set on GitHub or by agents) |
| `tag:NAME` | free-form tags, any number per task, such as `tag:home` |
| `importance:high`, `importance:medium`, `importance:low` | how much it matters (one per task) |
| `urgency:high`, `urgency:low` | how soon it matters (one per task) |
| `due:YYYY-MM-DD` | due date (one per task; a date within 14 days counts as urgent) |
| `priority:high`, `priority:medium`, `priority:low` | legacy priority, still set by the Mac app's editor |
| `source:app`, `source:claude`, `source:import` | where the task came from |
| `status:agent-requested` | the web app asked for an agent; the desktop's `todo-desk dispatch` picks it up |
| `status:agent-assigned` | an agent was started on it from a to-do app, or it was sent to a running agent |
| `status:claude-processed` | an agent has worked on it; read `## Claude Resolution Notes` |
| `status:waiting` | waiting on someone; the body's first line says who: `Waiting on: <who>` |
| `follow-up:YYYY-MM-DD` | when to chase a waiting task (one per task) |
| `status:waiting-cleared` | an agent thinks the wait is over; the apps ask you to confirm |
| `approved` | the owner approved the outside-world step an agent prepared (sending, buying, booking) |
| `subtask` | the task is in a group: a GitHub sub-issue of another task |
| `focus:week` | one of at most five tasks picked for This week in Triage |

Create the fixed ones with `setup-labels.sh OWNER/tasks` from the kit's `todo/` folder. Clients create `list:`,
`group:`, `tag:`, `due:` and `follow-up:` labels themselves when they need a new one, and keep any label they do
not manage.

## `.todo/config.json`

Optional. It groups lists in the sidebars and orders them, and names the lists that are not rated:

```json
{
  "groups": [{ "name": "Home", "sortOrder": 1 }],
  "listMetadata": [
    { "listName": "tasks", "sortOrder": 0 },
    { "listName": "to-buy", "groupName": "Home", "sortOrder": 0 }
  ],
  "unratedGroups": ["Reading"],
  "unratedLists": []
}
```

- `groups`: the groups, in `sortOrder`.
- `listMetadata`: one entry per list that has a group or a place in the order. A list with no entry is shown
  at the top level.
- `unratedGroups`, `unratedLists`: lists (by group or by name, any case) that hold catalogues or journals rather
  than things to do. They get no importance or urgency and stay out of Triage.

The Mac app writes this file through `todo-desk` when lists or groups are added, moved or deleted; the web app
writes it when a list is deleted. Edit it by hand at any time; the clients pick it up on their next sync.

## `.todo/agents.json`

Written by the desktop (`todo-desk dispatch`, commit message "Agents list (todo-desk)"), not by hand: the agents
running on the desktop, so the web app can offer "A running agent" in Assign to agent. It changes when an agent
starts or stops, and at most every 5 minutes otherwise. It holds agent names, folders and last status notes.
Deleting it is harmless; the next dispatch run writes it again.
