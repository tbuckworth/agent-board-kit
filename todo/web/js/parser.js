// Issue body parser: notes, the ## Checklist section and ## Claude Resolution Notes
import { uuid } from './uuid.js';
import {
  splitWaitingLine, withWaitingLine, tagFields, TAG_PREFIX, WAITING_LABEL, WAITING_CLEARED_LABEL, FOLLOW_UP_PREFIX,
} from './tags.js';

// Title case for list names ("to-buy" -> "To Buy"), as every client shows them
export function titleCase(str) {
  if (!str) return '';
  return str.split(/[\s\-_]+/).map(w =>
    w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()
  ).join(' ');
}

const CHECKLIST_MARKER = '## Checklist';
const CLAUDE_MARKER = '## Claude Resolution Notes';
const CHECKBOX_RE = /^- \[([ xX])\]\s*(.+)/;

export function extractBody(body) {
  if (!body) return '';
  const checklistIdx = body.indexOf(CHECKLIST_MARKER);
  const claudeIdx = body.indexOf(CLAUDE_MARKER);

  let firstMarker = Infinity;
  if (checklistIdx !== -1) firstMarker = Math.min(firstMarker, checklistIdx);
  if (claudeIdx !== -1) firstMarker = Math.min(firstMarker, claudeIdx);

  if (firstMarker !== Infinity) {
    return body.substring(0, firstMarker).trim();
  }
  return body.trim();
}

export function extractChecklist(body) {
  if (!body) return [];
  const idx = body.indexOf(CHECKLIST_MARKER);
  if (idx === -1) return [];

  const afterMarker = body.substring(idx + CHECKLIST_MARKER.length);
  const nextSection = afterMarker.indexOf('\n## ');
  const checklistText = nextSection !== -1 ? afterMarker.substring(0, nextSection) : afterMarker;

  const items = [];
  for (const line of checklistText.split('\n')) {
    const match = CHECKBOX_RE.exec(line.trim());
    if (match) {
      items.push({
        id: uuid(),
        text: match[2].trim(),
        isChecked: match[1].toLowerCase() === 'x',
      });
    }
  }
  return items;
}

export function extractClaudeNotes(body) {
  if (!body) return null;
  const idx = body.indexOf(CLAUDE_MARKER);
  if (idx === -1) return null;
  const afterMarker = body.substring(idx + CLAUDE_MARKER.length);
  const nextSection = afterMarker.indexOf('\n## ');
  const text = nextSection !== -1 ? afterMarker.substring(0, nextSection) : afterMarker;
  return text.trim() || null;
}

// `waitingOn` goes first, as a "Waiting on: <who>" line
export function buildPushBody(bodyText, checklist, claudeNotes, waitingOn = null) {
  const parts = [];
  const notes = withWaitingLine(bodyText, waitingOn);
  if (notes) parts.push(notes);

  if (checklist && checklist.length > 0) {
    parts.push(CHECKLIST_MARKER);
    const items = checklist.map(ci => {
      const checkbox = ci.isChecked ? '- [x]' : '- [ ]';
      return `${checkbox} ${ci.text}`;
    }).join('\n');
    parts.push(items);
  }

  if (claudeNotes) {
    parts.push(CLAUDE_MARKER);
    parts.push(claudeNotes);
  }

  const result = parts.join('\n\n');
  return result || null;
}

// Label extraction

export function extractLabel(labels, prefix) {
  const label = labels.find(l => l.startsWith(prefix));
  return label ? label.slice(prefix.length) : null;
}

export function extractActionType(labels) {
  const label = labels.find(l => l.startsWith('action:'));
  if (!label) return 'manual';
  const val = label.slice(7);
  return val || 'manual';
}

// Importance, urgency and due date. Unknown values read as unset.
const IMPORTANCES = ['high', 'medium', 'low'];
const URGENCIES = ['high', 'low'];
const DUE_RE = /^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$/;

function extractChoice(labels, prefix, allowed) {
  const val = extractLabel(labels.map(l => l.toLowerCase()), prefix);
  return allowed.includes(val) ? val : null;
}

export function extractImportance(labels) {
  return extractChoice(labels, 'importance:', IMPORTANCES);
}

export function extractUrgency(labels) {
  return extractChoice(labels, 'urgency:', URGENCIES);
}

export function extractDue(labels) {
  const val = extractLabel(labels, 'due:');
  return val && DUE_RE.test(val) ? val : null;
}

// The label-derived rating fields. Also applied to cached issues parsed by older versions.
export function ratingFields(labels) {
  return {
    importance: extractImportance(labels),
    urgency: extractUrgency(labels),
    due: extractDue(labels),
  };
}

// Local calendar date as YYYY-MM-DD
export function localDateString(date = new Date()) {
  const pad = n => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

// Whole days from today's local date to a YYYY-MM-DD date; negative when past
export function daysUntil(due, today = new Date()) {
  const [y, m, d] = due.split('-').map(Number);
  const target = Date.UTC(y, m - 1, d);
  const start = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
  return Math.round((target - start) / 86400000);
}

export const DUE_SOON_DAYS = 14;

// Effectively urgent: urgency:high, or due within 14 days or already past
export function isEffectivelyUrgent(task, today = new Date()) {
  if (task.urgency === 'high') return true;
  return !!task.due && daysUntil(task.due, today) <= DUE_SOON_DAYS;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// "2026-10-12" → "12 Oct"
export function formatDue(due) {
  const [, m, d] = due.split('-').map(Number);
  return `${d} ${MONTHS[m - 1]}`;
}

// Lists that hold catalogues or journals rather than things to do are not rated (no importance or urgency,
// left out of Triage). .todo/config.json in the tasks repo names them, in any case:
//   "unratedGroups": ["Archive"], "unratedLists": ["reading-log"]
let UNRATED_GROUPS = [];
let UNRATED_LISTS = [];

const lowerNames = (xs) => (Array.isArray(xs) ? xs : [])
  .filter(x => typeof x === 'string' && x.trim())
  .map(x => x.trim().toLowerCase());

export function setUnrated({ groups = [], lists = [] } = {}) {
  UNRATED_GROUPS = lowerNames(groups);
  UNRATED_LISTS = lowerNames(lists);
}

// The unrated settings from a .todo/config.json object (or null when the repo has none)
export function unratedFromConfig(config) {
  return { groups: lowerNames(config?.unratedGroups), lists: lowerNames(config?.unratedLists) };
}

export function isRatedList(listName, groupName) {
  if (listName && UNRATED_LISTS.includes(listName.toLowerCase())) return false;
  if (groupName && UNRATED_GROUPS.includes(groupName.toLowerCase())) return false;
  return true;
}

// Assign to agent from the web: the desktop's `todo-desk dispatch` turns the label and
// the request comment into a normal assign. The marker must be the comment's first line.
export const AGENT_REQUESTED_LABEL = 'status:agent-requested';
export const AGENT_ASSIGNED_LABEL = 'status:agent-assigned';
export const AGENT_REQUEST_MARKER = '<!-- todo-agent-request -->';
const SESSION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// `to` (a running agent's session id) asks the desktop to send the task to that agent instead of starting
// a new one: `<!-- todo-agent-request to=<id> -->`
export function agentRequestBody(instructions, to = null) {
  const text = (instructions || '').trim();
  if (to != null && !SESSION_ID.test(to)) throw new Error('Not an agent session id');
  const marker = to ? `<!-- todo-agent-request to=${to.toLowerCase()} -->` : AGENT_REQUEST_MARKER;
  return `${marker}\n\n${text || 'No extra instructions.'}`;
}

// The notes (without the checklist, Claude's notes or the "Waiting on:" line) and who the task waits on.
// Also applied to cached issues parsed by older versions.
export function bodyFields(rawBody) {
  const { waitingOn, rest } = splitWaitingLine(extractBody(rawBody));
  return { body: rest.trim(), waitingOn };
}

// Parse a raw GitHub issue into our app model
export function parseIssue(raw) {
  const labelNames = raw.labels?.nodes?.map(l => l.name) || raw.labels || [];
  return {
    number: raw.number,
    nodeId: raw.id || raw.nodeId,
    title: raw.title,
    rawBody: raw.body || '',
    ...bodyFields(raw.body),
    state: raw.state,
    // COMPLETED, NOT_PLANNED (archived), REOPENED or null; undefined in caches from before it was fetched
    stateReason: raw.stateReason,
    checklist: extractChecklist(raw.body),
    claudeNotes: extractClaudeNotes(raw.body),
    section: extractLabel(labelNames, 'section:'),
    listName: extractLabel(labelNames, 'list:') || 'tasks',
    groupName: extractLabel(labelNames, 'group:'),
    actionType: extractActionType(labelNames),
    ...ratingFields(labelNames),
    // tags, waiting, waitingCleared, followUp
    ...tagFields(labelNames),
    labels: labelNames,
    // The group this task is in (its parent issue's number), null when none. Undefined when the answer it came
    // from didn't ask, e.g. a mutation's: keep the number already known then.
    parentNumber: raw.parent === undefined ? raw.parentNumber : (raw.parent?.number ?? null),
    createdAt: raw.createdAt,
    updatedAt: raw.updatedAt,
  };
}

// Build labels for a task edit while preserving labels the app does not manage.
// action: is no longer written; an existing action label is kept as it is.
export function buildLabels(issue, groupName = undefined) {
  // priority: is retired but stays managed, so an edit drops any old priority label
  const managedPrefixes = ['section:', 'list:', 'group:', 'priority:', 'importance:', 'urgency:', 'due:',
    TAG_PREFIX, FOLLOW_UP_PREFIX];
  const labels = (issue.labels || []).filter(label =>
    !managedPrefixes.some(prefix => label.toLowerCase().startsWith(prefix))
    && ![WAITING_LABEL, WAITING_CLEARED_LABEL].includes(label.toLowerCase())
  );

  if (issue.section) labels.push(`section:${issue.section}`);
  if (issue.listName) labels.push(`list:${issue.listName}`);
  const gn = groupName === undefined ? issue.groupName : groupName;
  if (gn) labels.push(`group:${gn}`);
  if (issue.importance) labels.push(`importance:${issue.importance}`);
  if (issue.urgency) labels.push(`urgency:${issue.urgency}`);
  if (issue.due) labels.push(`due:${issue.due}`);
  // Fields not set on `issue` (a task cached by an older version) come from its labels
  const fromLabels = tagFields(issue.labels || []);
  for (const tag of issue.tags ?? fromLabels.tags) labels.push(TAG_PREFIX + tag);
  const waiting = issue.waiting ?? fromLabels.waiting;
  const cleared = issue.waitingCleared ?? fromLabels.waitingCleared;
  if (waiting) labels.push(WAITING_LABEL);
  if (cleared) labels.push(WAITING_CLEARED_LABEL);
  const followUp = issue.followUp !== undefined ? issue.followUp : fromLabels.followUp;
  if ((waiting || cleared) && followUp) labels.push(FOLLOW_UP_PREFIX + followUp);
  if (!labels.some(label => label.toLowerCase().startsWith('source:'))) {
    labels.push('source:app');
  }
  return labels;
}
