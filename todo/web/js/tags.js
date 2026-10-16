// Tags and "waiting on". Pure functions with no DOM, so they are testable with `node --test`.
// The Mac app follows the same scheme; keep the two in step:
// - `tag:<name>` labels, any number per task. Names are lowercase and hyphenated (`tag:home`).
// - `status:waiting` marks a task waiting on someone; `follow-up:YYYY-MM-DD` (one per task) says when to chase.
// - Who it is waiting on is the body's first line, `Waiting on: <who>`.
// A waiting task is greyed and sorted last until its follow-up date, then shown as "Follow up with <who>".
// - `status:waiting-cleared`: an agent thinks the wait is over (an email came, say). It swaps status:waiting for
//   this label, keeps the body line and any follow-up:, and writes its evidence in the Claude notes. The task goes
//   to the top as "Came through? <who>" until the owner says Still waiting or OK.

export const TAG_PREFIX = 'tag:';
export const WAITING_LABEL = 'status:waiting';
export const FOLLOW_UP_PREFIX = 'follow-up:';
export const WAITING_CLEARED_LABEL = 'status:waiting-cleared';
export const TAG_COLOR = '5319e7';
export const WAITING_COLOR = 'fbca04';

const DATE_RE = /^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$/;
const WAITING_LINE_RE = /^waiting on:[ \t]*(.*)$/i;

// "At Home " → "at-home"; null when nothing is left
export function normaliseTag(name) {
  const tag = String(name || '').trim().toLowerCase()
    .replace(/^tag:/, '')
    .replace(/\s+/g, '-')
    .replace(/[^a-z0-9-]/g, '')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
  return tag || null;
}

export function extractTags(labels) {
  const tags = new Set();
  for (const l of labels || []) {
    if (l.toLowerCase().startsWith(TAG_PREFIX)) {
      const t = normaliseTag(l.slice(TAG_PREFIX.length));
      if (t) tags.add(t);
    }
  }
  return [...tags].sort();
}

export function extractFollowUp(labels) {
  const l = (labels || []).find(x => x.toLowerCase().startsWith(FOLLOW_UP_PREFIX));
  const val = l ? l.slice(FOLLOW_UP_PREFIX.length) : null;
  return val && DATE_RE.test(val) ? val : null;
}

export function isWaitingLabelled(labels) {
  return (labels || []).some(l => l.toLowerCase() === WAITING_LABEL);
}

export function isWaitingCleared(labels) {
  return (labels || []).some(l => l.toLowerCase() === WAITING_CLEARED_LABEL);
}

// The label-derived fields. Also applied to cached issues parsed by older versions.
export function tagFields(labels) {
  return {
    tags: extractTags(labels),
    waiting: isWaitingLabelled(labels),
    waitingCleared: isWaitingCleared(labels),
    followUp: extractFollowUp(labels),
  };
}

// The notes text split into the "Waiting on:" first line (if any) and the rest
export function splitWaitingLine(text) {
  const body = text || '';
  const nl = body.indexOf('\n');
  const first = (nl === -1 ? body : body.slice(0, nl)).trim();
  const m = WAITING_LINE_RE.exec(first);
  if (!m) return { waitingOn: null, rest: body };
  return { waitingOn: m[1].trim() || null, rest: nl === -1 ? '' : body.slice(nl + 1).replace(/^\s*\n/, '') };
}

// Notes text with the "Waiting on:" line in front, or without one when `who` is empty
export function withWaitingLine(text, who) {
  const rest = splitWaitingLine(text).rest.trim();
  const name = (who || '').trim();
  if (!name) return rest;
  return rest ? `Waiting on: ${name}\n\n${rest}` : `Waiting on: ${name}`;
}

// 'none', 'waiting' (greyed, last), 'follow-up' (its follow-up date has come: shown normally, flagged) or
// 'cleared' (an agent thinks it came through: first, flagged). `today` is a local YYYY-MM-DD string.
export function waitingState(task, today) {
  if (task.state === 'CLOSED') return 'none';
  if (task.waitingCleared) return 'cleared';
  if (!task.waiting) return 'none';
  return task.followUp && task.followUp <= today ? 'follow-up' : 'waiting';
}

// Tasks in the same order, except that those an agent thinks came through go first and those still waiting last
export function waitingLast(tasks, today) {
  const first = [];
  const middle = [];
  const last = [];
  for (const t of tasks) {
    const state = waitingState(t, today);
    (state === 'cleared' ? first : state === 'waiting' ? last : middle).push(t);
  }
  return first.concat(middle, last);
}

// "Waiting on Sam · follow up 23 Oct" / "Follow up with Sam" / "Came through? Sam"
export function waitingText(task, today, formatDate) {
  const who = task.waitingOn;
  const state = waitingState(task, today);
  if (state === 'cleared') return who ? `Came through? ${who}` : 'Came through?';
  if (state === 'follow-up') return who ? `Follow up with ${who}` : 'Follow up';
  const head = who ? `Waiting on ${who}` : 'Waiting';
  return task.followUp ? `${head} · follow up ${formatDate(task.followUp)}` : head;
}

// ---------------------------------------------------------------- the tag filter

// { tagName: 'only' | 'hide' }. Every "only" tag must be on the task; any "hide" tag keeps it out.
export function matchesTagFilter(task, tagFilter) {
  if (!tagFilter) return true;
  const tags = task.tags || [];
  for (const [tag, mode] of Object.entries(tagFilter)) {
    if (mode === 'only' && !tags.includes(tag)) return false;
    if (mode === 'hide' && tags.includes(tag)) return false;
  }
  return true;
}

// Neutral → only → hide → neutral
export function nextTagMode(mode) {
  return mode === 'only' ? 'hide' : mode === 'hide' ? null : 'only';
}

export function withTagMode(tagFilter, tag, mode) {
  const next = { ...(tagFilter || {}) };
  if (mode) next[tag] = mode; else delete next[tag];
  return next;
}

export function isTagFilterActive(tagFilter) {
  return !!tagFilter && Object.keys(tagFilter).length > 0;
}

// The tags to offer as filter chips: those on `tasks`, plus any in the filter, alphabetical
export function filterTags(tasks, tagFilter) {
  const tags = new Set(Object.keys(tagFilter || {}));
  for (const t of tasks) for (const tag of t.tags || []) tags.add(tag);
  return [...tags].sort();
}

const FILTER_KEY = 'todo.tagFilter';

export function loadTagFilter(storage = globalThis.localStorage) {
  try {
    const parsed = JSON.parse(storage.getItem(FILTER_KEY) || '{}');
    const out = {};
    for (const [tag, mode] of Object.entries(parsed || {})) {
      const t = normaliseTag(tag);
      if (t && (mode === 'only' || mode === 'hide')) out[t] = mode;
    }
    return out;
  } catch {
    return {};
  }
}

export function saveTagFilter(tagFilter, storage = globalThis.localStorage) {
  try {
    storage.setItem(FILTER_KEY, JSON.stringify(tagFilter || {}));
  } catch {
    // Private windows and blocked storage: the filter lasts for this visit only
  }
}

// ---------------------------------------------------------------- labels

const isTagLabel = l => l.toLowerCase().startsWith(TAG_PREFIX);
const isWaitingPart = l => [WAITING_LABEL, WAITING_CLEARED_LABEL].includes(l.toLowerCase())
  || l.toLowerCase().startsWith(FOLLOW_UP_PREFIX);

// `labels` with the tag labels replaced by `tags`; every other label kept
export function withTags(labels, tags) {
  const kept = (labels || []).filter(l => !isTagLabel(l));
  const names = [...new Set((tags || []).map(normaliseTag).filter(Boolean))].sort();
  return kept.concat(names.map(t => TAG_PREFIX + t));
}

// `labels` with status:waiting and follow-up: set from { waiting, followUp }; every other label kept.
// status:waiting-cleared always goes: setting the wait either way is the owner's answer to the agent.
export function withWaiting(labels, { waiting, followUp }) {
  const kept = (labels || []).filter(l => !isWaitingPart(l));
  if (!waiting) return kept;
  kept.push(WAITING_LABEL);
  if (followUp && DATE_RE.test(followUp)) kept.push(FOLLOW_UP_PREFIX + followUp);
  return kept;
}

// The colour a label gets when an app creates it
export function labelColor(name) {
  const l = String(name).toLowerCase();
  if (l.startsWith(TAG_PREFIX)) return TAG_COLOR;
  if (l === WAITING_CLEARED_LABEL) return '0e8a16';
  if (l === WAITING_LABEL || l.startsWith(FOLLOW_UP_PREFIX)) return WAITING_COLOR;
  return 'ededed';
}

// YYYY-MM-DD `days` after the local date `from`
export function addDays(days, from = new Date()) {
  const d = new Date(from.getFullYear(), from.getMonth(), from.getDate() + days);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
