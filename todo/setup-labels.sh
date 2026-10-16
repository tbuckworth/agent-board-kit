#!/usr/bin/env bash
# Create, or update in place, the labels the to-do clients, todo-desk and agents use, in the tasks repo.
# Safe to rerun: `gh label create --force` updates the colour and description of a label that exists.
#
#   ./setup-labels.sh [--dry-run] [--list NAME]... OWNER/REPO
#
#   --dry-run    print the gh commands and change nothing (gh need not be installed)
#   --list NAME  also create list:NAME (repeatable). Lists need no setup: every client creates a list's label
#                the first time a task goes into it. This only gives them a colour and a description up front.
#
# Labels that carry a value (list:NAME, group:NAME, tag:NAME, due:YYYY-MM-DD, follow-up:YYYY-MM-DD) are created by
# the clients when first needed.
set -euo pipefail

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }

DRY=0
REPO=""
LISTS=(tasks)
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --list) [ $# -ge 2 ] || { usage >&2; exit 2; }; LISTS+=("$2"); shift ;;
    --list=*) LISTS+=("${1#--list=}") ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) [ -z "$REPO" ] || { echo "one repo only" >&2; exit 2; }; REPO="$1" ;;
  esac
  shift
done
if [ -z "$REPO" ]; then usage >&2; exit 2; fi
if ! [[ "$REPO" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$ ]]; then
  echo "the repo must be OWNER/NAME, e.g. example-user/tasks (got: $REPO)" >&2
  exit 2
fi
if [ "$DRY" = 0 ] && ! command -v gh >/dev/null 2>&1; then
  echo "gh (GitHub CLI) is not installed; install it and run 'gh auth login', or use --dry-run" >&2
  exit 1
fi

# name | colour (hex, no #) | description
LABELS=(
  # where a task lives (single-valued: a task is in one list, one section)
  "section:personal|C5DEF5|Section: personal"
  "section:work|C5DEF5|Section: work"
  "section:home|C5DEF5|Section: home"
  "section:health|C5DEF5|Section: health"
  "section:finance|C5DEF5|Section: finance"
  # what kind of action it is, for agents; set by hand or by agents (the apps keep it but no longer set it)
  "action:buy|F9D0C4|Something to buy (agents recommend; buying needs the approved label)"
  "action:email|F9D0C4|An email to send (agents draft; sending needs the approved label)"
  "action:look-into|F9D0C4|Research or find something out (agents may do it)"
  "action:call|F9D0C4|A phone call (agents may prepare notes)"
  "action:manual|EDEDED|Only a person can do this; agents leave it alone"
  # rating (single-valued); due:YYYY-MM-DD is created when a due date is set
  "importance:high|B60205|Importance: high"
  "importance:medium|D93F0B|Importance: medium"
  "importance:low|FBCA04|Importance: low"
  "urgency:high|E99695|Urgent (also: due within 14 days)"
  "urgency:low|FEF2C0|Not urgent"
  "priority:high|EDEDED|Legacy priority (the Mac app's editor still sets it; the web app replaces it with importance)"
  "priority:medium|EDEDED|Legacy priority"
  "priority:low|EDEDED|Legacy priority"
  # who made it
  "source:app|BFD4F2|Created in a to-do app"
  "source:claude|BFD4F2|Created by an agent"
  "source:import|BFD4F2|Imported from another to-do list"
  # agent workflow
  "status:agent-requested|D4C5F9|The web app asked for an agent; todo-desk dispatch starts it"
  "status:agent-assigned|AD7D48|An agent was started on this task from the to-do app"
  "status:claude-processed|0E8A16|An agent worked on this task; see Claude Resolution Notes"
  # waiting on someone: the body's first line says who ("Waiting on: <who>"); follow-up:YYYY-MM-DD says when to chase
  "status:waiting|FBCA04|Waiting on someone (first line of the body: Waiting on: <who>)"
  "status:waiting-cleared|0E8A16|An agent thinks the wait is over; confirm (OK) or keep waiting in the app"
  "approved|0E8A16|The owner approved the externally consequential step an agent prepared"
  # grouping and focus
  "subtask|EDEDED|In a group: a sub-issue of another task (lets other devices notice the link)"
  "focus:week|5C5CE0|In This week (at most five open tasks), set from Triage"
)
SEEN=" "
for l in "${LISTS[@]}"; do
  l=$(printf '%s' "$l" | tr '[:upper:]' '[:lower:]' | sed 's/^ *//; s/ *$//; s/  */-/g')
  [ -n "$l" ] || continue
  case "$SEEN" in *" $l "*) continue ;; esac
  SEEN="$SEEN$l "
  if [ "$l" = tasks ]; then
    LABELS+=("list:tasks|0075CA|The default list")
  else
    LABELS+=("list:$l|0075CA|List: $l")
  fi
done

n=0
for spec in "${LABELS[@]}"; do
  IFS='|' read -r name color desc <<<"$spec"
  cmd=(gh label create "$name" --repo "$REPO" --color "$color" --description "$desc" --force)
  if [ "$DRY" = 1 ]; then
    printf '%q ' "${cmd[@]}"; echo
  else
    "${cmd[@]}" >/dev/null
    echo "ok  $name"
  fi
  n=$((n + 1))
done
if [ "$DRY" = 1 ]; then
  echo "# dry run: $n labels for $REPO, nothing changed" >&2
else
  echo "$n labels created or updated in $REPO"
fi
