// Triage: which task to show next, snoozes, the "This week" focus set, and archived tasks.
// Pure functions with no DOM, so they are testable with `node --test`. Any other client that offers
// Triage should follow the same rules, so a task is picked, snoozed and archived alike everywhere.

import { waitingState, addDays } from './tags.js';

const DAY = 86400000;

// "This week": Covey's weekly big rocks, a single label, at most five open tasks
export const FOCUS_LABEL = 'focus:week';
export const FOCUS_CAP = 5;

// Snoozes grow: 1 day, 3 days, 1 week, 3 weeks, then 3 weeks again
export const SNOOZE_DAYS = [1, 3, 7, 21];
export const SNOOZE_HINT_COUNT = 4;

// Weights. Unrated tasks come first, so they get rated; then importance.
export const UNRATED_WEIGHT = 6;
export const IMPORTANCE_WEIGHT = { high: 3, medium: 2, low: 1 };
// A task shown in triage within the last day counts a tenth, within a week 0.4
export const RECENT_DAY_FACTOR = 0.1;
export const RECENT_WEEK_FACTOR = 0.4;
// Older tasks (by last update) count more: 1 + days/30, capped at 90 days (x4)
export const AGE_CAP_DAYS = 90;
// Shown-history older than this is forgotten
export const SHOWN_KEEP_DAYS = 30;

export function snoozeDays(previousCount) {
  const n = Math.max(0, previousCount || 0);
  return SNOOZE_DAYS[Math.min(n, SNOOZE_DAYS.length - 1)];
}

// The snooze entry after one more snooze: { count, until } with until in epoch milliseconds
export function nextSnooze(entry, now) {
  const count = entry?.count || 0;
  return { count: count + 1, until: now + snoozeDays(count) * DAY };
}

export function isSnoozed(entry, now) {
  return !!entry && typeof entry.until === 'number' && entry.until > now;
}

export function snoozeHint(entry) {
  const count = entry?.count || 0;
  return count >= SNOOZE_HINT_COUNT ? `Snoozed ${count} times — archive?` : null;
}

export function recencyFactor(lastShown, now) {
  if (!lastShown) return 1;
  const age = now - lastShown;
  if (age < DAY) return RECENT_DAY_FACTOR;
  if (age < 7 * DAY) return RECENT_WEEK_FACTOR;
  return 1;
}

export function ageFactor(updatedAt, now) {
  const t = updatedAt ? Date.parse(updatedAt) : NaN;
  if (Number.isNaN(t)) return 1;
  const days = Math.max(0, (now - t) / DAY);
  return 1 + Math.min(days, AGE_CAP_DAYS) / 30;
}

export function triageWeight(task, now, lastShown) {
  const importance = task.importance ? (IMPORTANCE_WEIGHT[task.importance] || 1) : UNRATED_WEIGHT;
  return importance * recencyFactor(lastShown, now) * ageFactor(task.updatedAt, now);
}

// Open tasks on GitHub in rated lists, not snoozed. isRated(task) decides the list rule (state.js isTaskRated).
// Work is allowed here; it is only left out of nudges and the widget. A task waiting on someone is left out until
// its follow-up date.
export function triagePool(tasks, { now, snoozes = {}, shown = {}, isRated }) {
  const pool = [];
  const today = addDays(0, new Date(now));
  for (const task of tasks) {
    if (task.state !== 'OPEN' || !task.number) continue;
    if (isRated && !isRated(task)) continue;
    if (waitingState(task, today) === 'waiting') continue;
    if (isSnoozed(snoozes[task.number], now)) continue;
    pool.push({ task, weight: triageWeight(task, now, shown[task.number]) });
  }
  return pool;
}

// A weighted random task, avoiding `exclude` (the current card's number) unless it is the only one left
export function pickTriage(tasks, opts, random = Math.random) {
  const pool = triagePool(tasks, opts);
  const others = opts.exclude != null ? pool.filter(c => c.task.number !== opts.exclude) : pool;
  const candidates = others.length > 0 ? others : pool;
  if (candidates.length === 0) return null;
  const total = candidates.reduce((sum, c) => sum + c.weight, 0);
  let ticket = random() * total;
  for (const c of candidates) {
    if (ticket < c.weight) return c.task;
    ticket -= c.weight;
  }
  return candidates[candidates.length - 1].task;
}

export function pruneShown(shown, now) {
  const kept = {};
  for (const [k, v] of Object.entries(shown || {})) {
    if (now - v < SHOWN_KEEP_DAYS * DAY) kept[k] = v;
  }
  return kept;
}

// Expired snoozes keep their count (it drives the next interval and the hint)
export function pruneSnoozes(snoozes, openNumbers) {
  const kept = {};
  for (const [k, v] of Object.entries(snoozes || {})) {
    if (!openNumbers || openNumbers.has(Number(k))) kept[k] = v;
  }
  return kept;
}

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

// Labels with the rating replaced: importance:, urgency: and due: (and the retired priority:) are rewritten,
// every other label is kept as it is
const RATING_PREFIXES = ['importance:', 'urgency:', 'due:', 'priority:'];

export function withRating(labels, { importance, urgency, due }) {
  const rest = (labels || []).filter(l => !RATING_PREFIXES.some(p => l.toLowerCase().startsWith(p)));
  if (importance) rest.push(`importance:${importance}`);
  if (urgency) rest.push(`urgency:${urgency}`);
  if (due) rest.push(`due:${due}`);
  return rest;
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
    (task.labels || []).some(l => l.toLowerCase().startsWith('list:'));
}

// Closed issues cached before stateReason was fetched; a full sync fills it in
export function needsStateReason(tasks) {
  return tasks.some(t => t.state === 'CLOSED' && t.stateReason === undefined);
}

// Per-device triage memory: { snoozes: { [number]: { count, until } }, shown: { [number]: epochMs } }
export const STORE_KEY = 'todo_triage_v1';

export function loadTriageStore(storage) {
  try {
    const raw = storage?.getItem(STORE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return {
      snoozes: parsed && typeof parsed.snoozes === 'object' && parsed.snoozes ? parsed.snoozes : {},
      shown: parsed && typeof parsed.shown === 'object' && parsed.shown ? parsed.shown : {},
    };
  } catch {
    return { snoozes: {}, shown: {} };
  }
}

export function saveTriageStore(storage, store) {
  try {
    storage?.setItem(STORE_KEY, JSON.stringify(store));
  } catch {
    // Private mode or full storage: triage still works, it just forgets
  }
}
