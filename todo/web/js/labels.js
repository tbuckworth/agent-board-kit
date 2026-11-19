// Labels and issue state shared by every view: This week focus, list and rating labels, the agent's first step,
// archived tasks.

// "This week": Covey's weekly big rocks, a single label, at most five open tasks
export const FOCUS_LABEL = 'focus:week';
export const FOCUS_CAP = 5;

// This week
export function hasFocus(task) {
  return (task.labels || []).some(l => l.toLowerCase() === FOCUS_LABEL);
}

export function focusTasks(tasks) {
  return tasks.filter(t => t.state === 'OPEN' && hasFocus(t));
}

export function withFocus(labels, on) {
  const rest = (labels || []).filter(l => l.toLowerCase() !== FOCUS_LABEL);
  return on ? [...rest, FOCUS_LABEL] : rest;
}

// A task's list is its single list: label
export const LIST_PREFIX = 'list:';

// The rating's labels: importance:, urgency: and due:. The retired priority: is rewritten with them, so an edit
// drops an old priority label.
export const IMPORTANCE_PREFIX = 'importance:';
export const URGENCY_PREFIX = 'urgency:';
export const DUE_PREFIX = 'due:';
export const RATING_PREFIXES = [IMPORTANCE_PREFIX, URGENCY_PREFIX, DUE_PREFIX, 'priority:'];

export function ratingLabels({ importance, urgency, due }) {
  const labels = [];
  if (importance) labels.push(IMPORTANCE_PREFIX + importance);
  if (urgency) labels.push(URGENCY_PREFIX + urgency);
  if (due) labels.push(DUE_PREFIX + due);
  return labels;
}

// Labels with the rating replaced, every other label kept as it is
export function withRating(labels, rating) {
  const rest = (labels || []).filter(l => !RATING_PREFIXES.some(p => l.toLowerCase().startsWith(p)));
  return [...rest, ...ratingLabels(rating)];
}

// The agent's "**First step:** …" line from the Claude notes (as nudges use it), or null
const FIRST_STEP_RE = /^\W*\*\*First step:?\*\*:?\s*(.+)$/im;

export function firstStep(claudeNotes) {
  if (!claudeNotes) return null;
  const m = FIRST_STEP_RE.exec(claudeNotes);
  return m ? m[1].trim() : null;
}

// Archived: closed as "not planned", still in a list (the apps' delete removes the list: label)
export function isArchived(task) {
  return task.state === 'CLOSED' && task.stateReason === 'NOT_PLANNED' &&
    (task.labels || []).some(l => l.toLowerCase().startsWith(LIST_PREFIX));
}

// Closed issues cached before stateReason was fetched; a full sync fills it in
export function needsStateReason(tasks) {
  return tasks.some(t => t.state === 'CLOSED' && t.stateReason === undefined);
}
