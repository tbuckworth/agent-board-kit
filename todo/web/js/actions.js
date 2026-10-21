// Online task mutations used by Triage, Archived and the "This week" section. Each shows its change at once,
// keeps GitHub's answer, and on failure undoes only the fields it changed, so a change to the same task that
// succeeded meanwhile is kept.

import { getState, setState, getIssueByNumber } from './state.js';
import * as api from './api.js';
import * as db from './db.js';
import {
  parseIssue, agentRequestBody, AGENT_REQUESTED_LABEL, buildPushBody, bodyFields, extractChecklist, extractClaudeNotes,
} from './parser.js';
import { withTags, withWaiting, normaliseTag } from './tags.js';
import { FOCUS_LABEL, withFocus, withRating } from './triage.js';

// A mutation's answer doesn't say which group the task is in; the number already known stays
export function replaceLocal(task) {
  setState({
    issues: getState().issues.map(i => i.number !== task.number ? i
      : (task.parentNumber === undefined ? { ...task, parentNumber: i.parentNumber } : task)),
  });
}

// The cache is a convenience: a failed write must not turn a GitHub success into a reported failure
export async function cache(task) {
  try {
    await db.issues.put(task);
  } catch (err) {
    console.warn('Cache write failed:', err);
  }
}

// Puts back the fields `change` set, where they still hold the values this action wrote
function revert(number, before, change) {
  const latest = getIssueByNumber(number);
  if (!latest) return;
  const restored = { ...latest };
  for (const key of Object.keys(change)) {
    if (latest[key] === change[key]) restored[key] = before[key];
  }
  replaceLocal(restored);
}

// Shows `change` at once; GitHub's answer (when the request returns one) replaces it.
async function optimistic(number, change, request) {
  const before = getIssueByNumber(number);
  if (!before) throw new Error(`Task #${number} is not loaded`);
  replaceLocal({ ...before, ...change });
  let raw;
  try {
    raw = await request(before);
  } catch (err) {
    revert(number, before, change);
    throw err;
  }
  // GitHub accepted the change: from here on nothing is reported as a failure
  let result;
  try {
    result = raw ? parseIssue(raw) : getIssueByNumber(number);
  } catch (err) {
    console.warn('Could not read the updated task; keeping the local change:', err);
    return getIssueByNumber(number);
  }
  if (result) {
    if (raw) replaceLocal(result);
    await cache(result);
  }
  return result;
}

// Archive: close as "not planned"; the list: label stays, so the task is still found under its list
export function archiveTask(number) {
  return optimistic(number, { state: 'CLOSED', stateReason: 'NOT_PLANNED' },
    () => api.closeIssue(number, 'NOT_PLANNED'));
}

export function completeTask(number) {
  return optimistic(number, { state: 'CLOSED', stateReason: 'COMPLETED' },
    () => api.closeIssue(number, 'COMPLETED'));
}

export function reopenTask(number) {
  return optimistic(number, { state: 'OPEN', stateReason: 'REOPENED' },
    () => api.reopenIssue(number));
}

// The apps' delete: close and remove the list: label (keeping every other label as GitHub has it now),
// then forget the task locally. Nothing changes locally until GitHub has accepted it.
export async function deleteTask(number) {
  const task = getIssueByNumber(number);
  if (!task) return;
  const current = await api.getIssueLabels(number);
  const labels = current.filter(l => !l.toLowerCase().startsWith('list:'));
  // Always sent: the cached state may be stale (the issue reopened elsewhere), and closing twice is harmless
  await api.updateIssue(number, { state: 'CLOSED', labels });
  setState({ issues: getState().issues.filter(i => i.number !== number) });
  try {
    await db.issues.delete(number);
  } catch (err) {
    console.warn('Cache delete failed:', err);
  }
}

// Add or remove focus:week, leaving every other label alone
export function setFocus(number, on) {
  const before = getIssueByNumber(number);
  if (!before) return Promise.reject(new Error(`Task #${number} is not loaded`));
  return optimistic(number, { labels: withFocus(before.labels, on) }, async () => {
    const issueId = before.nodeId || await api.getIssueNodeId(number);
    return on
      ? api.addLabels(issueId, [FOCUS_LABEL])
      : api.removeLabels(issueId, [FOCUS_LABEL]); // null when the repo has no such label
  });
}

// One rating change (importance, urgency or due). Only the rating labels are rewritten, on top of the labels
// GitHub has now, so a label set elsewhere meanwhile (focus:week, an agent's) is kept. As on new tasks, a task
// given an importance with no urgency yet says it is not urgent.
export function saveRating(number, changed) {
  const before = getIssueByNumber(number);
  if (!before) return Promise.reject(new Error(`Task #${number} is not loaded`));
  const rating = {
    importance: before.importance, urgency: before.urgency, due: before.due, ...changed,
  };
  if (rating.importance && !rating.urgency && !('urgency' in changed)) rating.urgency = 'low';
  const change = { ...rating, labels: withRating(before.labels, rating) };
  return optimistic(number, change, async () => {
    const current = await api.getIssueLabels(number);
    return api.updateIssue(number, { labels: withRating(current, rating) });
  });
}

// Assign to agent: comment first, then label; the desktop's `todo-desk dispatch` acts on the label.
// `to` (a running agent's session id) hands the task to that agent instead of starting a new one.
export async function requestAgent(number, instructions, to = null) {
  if (typeof navigator !== 'undefined' && navigator.onLine === false) {
    throw new Error('Assigning needs the network');
  }
  const task = getIssueByNumber(number);
  if (!task) throw new Error(`Task #${number} is not loaded`);
  const issueId = task.nodeId || await api.getIssueNodeId(number);
  await api.addComment(issueId, agentRequestBody(instructions, to));
  const parsed = parseIssue(await api.addLabels(issueId, [AGENT_REQUESTED_LABEL]));
  replaceLocal(parsed);
  await cache(parsed);
  return parsed;
}

// Requests for one task, one after another, so quick successive changes reach GitHub in the order they were made
const queues = new Map();
function inTurn(number, request) {
  const run = (queues.get(number) || Promise.resolve()).catch(() => {}).then(request);
  queues.set(number, run);
  run.finally(() => { if (queues.get(number) === run) queues.delete(number); }).catch(() => {});
  return run;
}

// Replace a task's tags. Only the tag labels are rewritten, on top of the labels GitHub has now.
export function setTags(number, tags) {
  const before = getIssueByNumber(number);
  if (!before) return Promise.reject(new Error(`Task #${number} is not loaded`));
  const names = [...new Set(tags.map(normaliseTag).filter(Boolean))].sort();
  return optimistic(number, { tags: names, labels: withTags(before.labels, names) }, () => inTurn(number, async () => {
    const current = await api.getIssueLabels(number);
    return api.updateIssue(number, { labels: withTags(current, names) });
  }));
}

// Waiting on someone: { waiting, waitingOn, followUp }. status:waiting and follow-up: are rewritten on
// top of the labels GitHub has now; who goes in the body's first line. Turning waiting off clears all three.
// Either way status:waiting-cleared goes: this is also the answer to an agent that thinks the wait is over
// (Still waiting: waiting with the same who and follow-up; OK: not waiting).
export function setWaiting(number, { waiting, waitingOn = null, followUp = null }) {
  const before = getIssueByNumber(number);
  if (!before) return Promise.reject(new Error(`Task #${number} is not loaded`));
  const who = waiting ? ((waitingOn || '').trim() || null) : null;
  const due = waiting ? (followUp || null) : null;
  const change = { waiting: !!waiting, waitingCleared: false, waitingOn: who, followUp: due, labels: withWaiting(before.labels, { waiting, followUp: due }) };
  return optimistic(number, change, () => inTurn(number, async () => {
    // GitHub's body, not the cached one: only its "Waiting on:" line changes, so notes added elsewhere stay
    const current = await api.getIssueLabelsAndBody(number);
    const updates = { labels: withWaiting(current.labels, { waiting, followUp: due }) };
    const fields = bodyFields(current.body);
    if ((fields.waitingOn || null) !== who) {
      updates.body = buildPushBody(fields.body, extractChecklist(current.body), extractClaudeNotes(current.body), who) || '';
    }
    return api.updateIssue(number, updates);
  }));
}
