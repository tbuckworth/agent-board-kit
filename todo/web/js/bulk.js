// Actions on several tasks at once, and on groups. Each returns an undo function, which the list view
// keeps on a stack for the toast's Undo and Cmd/Ctrl-Z. A group's tasks go with it when it is completed, deleted or
// moved; a rating or an agent applies to the group alone.

import { getState, setState, getIssueByNumber } from './state.js';
import * as api from './api.js';
import { parseIssue, buildLabels } from './parser.js';
import {
  completeTask, reopenTask, deleteTask, saveRating, requestAgent, replaceLocal, cache, setTags, setWaiting,
} from './actions.js';
import { normaliseTag } from './tags.js';
import { SUBTASK_LABEL, withGroupMembers, groupRating, planGroup, openDescendants } from './groups.js';
import { AGENT_REQUESTED_LABEL } from './parser.js';

// ---------------------------------------------------------------- undo stack

const undoStack = [];

export function pushUndo(label, fn) {
  undoStack.push({ label, fn });
  if (undoStack.length > 20) undoStack.shift();
}

// Undoes the latest action; resolves to its label, or null when there is nothing to undo
export async function undoLast() {
  const entry = undoStack.pop();
  if (!entry) return null;
  await entry.fn();
  return entry.label;
}

// Runs fn over numbers one at a time (GitHub's mutation pacing is in api.js); returns the numbers it did
async function each(numbers, fn) {
  const done = [];
  const errors = [];
  for (const n of numbers) {
    try {
      await fn(n);
      done.push(n);
    } catch (err) {
      errors.push(`#${n}: ${err.message}`);
    }
  }
  if (errors.length) {
    const e = new Error(`${errors.length} of ${numbers.length} failed (${errors[0]})`);
    e.done = done;
    throw e;
  }
  return done;
}

const all = () => getState().issues;

// ---------------------------------------------------------------- complete, delete, move

export async function completeTasks(numbers) {
  const targets = withGroupMembers(numbers, all()).filter(n => getIssueByNumber(n)?.state === 'OPEN');
  const done = await each(targets, n => completeTask(n));
  return () => each(done, n => reopenTask(n));
}

export async function deleteTasks(numbers) {
  const targets = withGroupMembers(numbers, all());
  const before = new Map(targets.map(n => [n, getIssueByNumber(n)]).filter(([, t]) => t));
  const done = await each([...before.keys()], n => deleteTask(n));
  return () => each(done, async n => {
    const t = before.get(n);
    const raw = await api.updateIssue(n, { state: t.state, labels: t.labels });
    const restored = { ...parseIssue(raw), parentNumber: t.parentNumber };
    setState({ issues: [restored, ...getState().issues.filter(i => i.number !== n)] });
    await cache(restored);
  });
}

async function setList(number, listName) {
  const current = await api.getIssueLabels(number);
  const labels = current.filter(l => !l.toLowerCase().startsWith('list:')).concat(`list:${listName}`);
  const parsed = parseIssue(await api.updateIssue(number, { labels }));
  replaceLocal(parsed);
  await cache(parsed);
}

export async function moveTasks(numbers, listName) {
  const targets = withGroupMembers(numbers, all());
  const from = new Map(targets.map(n => [n, getIssueByNumber(n)?.listName || 'tasks']));
  const done = await each(targets, n => setList(n, listName));
  return () => each(done, n => setList(n, from.get(n)));
}

// ---------------------------------------------------------------- tags and waiting

// Adds `tag` to (or, with add false, removes it from) each task. Tasks that already had it as asked are left alone.
export async function tagTasks(numbers, tag, add = true) {
  const name = normaliseTag(tag);
  if (!name) throw new Error('No tag given');
  const before = new Map(numbers.map(n => [n, getIssueByNumber(n)?.tags || []]));
  const targets = numbers.filter(n => before.get(n).includes(name) !== add);
  const done = await each(targets, n => {
    const tags = before.get(n);
    return setTags(n, add ? [...tags, name] : tags.filter(t => t !== name));
  });
  return () => each(done, n => setTags(n, before.get(n)));
}

// value: { waiting, waitingOn, followUp }, the same for each task
export async function waitTasks(numbers, value) {
  const before = new Map(numbers.map(n => {
    const t = getIssueByNumber(n);
    return [n, { waiting: !!t?.waiting, waitingOn: t?.waitingOn ?? null, followUp: t?.followUp ?? null }];
  }));
  const done = await each(numbers, n => setWaiting(n, value));
  return () => each(done, n => setWaiting(n, before.get(n)));
}

// ---------------------------------------------------------------- rating and agents

export async function rateTasks(numbers, change) {
  const before = new Map(numbers.map(n => {
    const t = getIssueByNumber(n);
    return [n, { importance: t?.importance ?? null, urgency: t?.urgency ?? null, due: t?.due ?? null }];
  }));
  const done = await each(numbers, n => saveRating(n, change));
  return () => each(done, n => saveRating(n, before.get(n)));
}

// One agent for the lot: the first task carries the request, naming the others; each other task gets a note.
// A group's agent is asked to handle the group's tasks as well.
export async function assignTasks(numbers, instructions = '', to = null) {
  const [lead, ...rest] = numbers;
  const members = openDescendants(lead, all()).map(t => t.number);
  const lines = [];
  if (members.length) lines.push(`This is a group; its tasks are its sub-issues: ${members.map(n => `#${n}`).join(', ')}.`);
  if (rest.length) lines.push(`Handle these tasks together with this one, as one job: ${rest.map(n => `#${n}`).join(', ')}.`);
  const text = [instructions.trim(), ...lines].filter(Boolean).join('\n\n');
  await requestAgent(lead, text, to);
  for (const n of rest) {
    const t = getIssueByNumber(n);
    const id = t?.nodeId || await api.getIssueNodeId(n);
    await api.addComment(id, `Assigned to an agent together with #${lead}, which carries the request.`);
  }
  // Undo works until the desktop picks the request up (every 2 minutes): it removes the request label
  return async () => {
    const t = getIssueByNumber(lead);
    const id = t?.nodeId || await api.getIssueNodeId(lead);
    const raw = await api.removeLabels(id, [AGENT_REQUESTED_LABEL]);
    if (raw) {
      const parsed = parseIssue(raw);
      replaceLocal(parsed);
      await cache(parsed);
    }
  };
}

// ---------------------------------------------------------------- groups

async function nodeId(n) {
  return getIssueByNumber(n)?.nodeId || api.getIssueNodeId(n);
}

function setParentLocal(n, parentNumber) {
  const t = getIssueByNumber(n);
  if (!t) return;
  const labels = parentNumber == null
    ? t.labels.filter(l => l !== SUBTASK_LABEL)
    : (t.labels.includes(SUBTASK_LABEL) ? t.labels : [...t.labels, SUBTASK_LABEL]);
  const updated = { ...t, parentNumber, labels };
  replaceLocal(updated);
  cache(updated);
}

// Links a task into a group. The subtask label is what makes other devices notice: linking alone doesn't change
// a task's updatedAt, and they only fetch tasks updated since their last sync.
async function link(parentNumber, childNumber) {
  const [pid, cid] = await Promise.all([nodeId(parentNumber), nodeId(childNumber)]);
  await api.addSubIssue(pid, cid);
  await api.addLabels(cid, [SUBTASK_LABEL]);
  setParentLocal(childNumber, parentNumber);
}

async function unlink(parentNumber, childNumber) {
  const [pid, cid] = await Promise.all([nodeId(parentNumber), nodeId(childNumber)]);
  await api.removeSubIssue(pid, cid);
  await api.removeLabels(cid, [SUBTASK_LABEL]);
  setParentLocal(childNumber, null);
}

// Puts each task back in the group it was in before (or none)
async function restoreParents(previous) {
  await each([...previous.keys()], async n => {
    const was = previous.get(n);
    const now = getIssueByNumber(n)?.parentNumber ?? null;
    if (was === now) return;
    if (was == null) await unlink(now, n);
    else await link(was, n);
  });
}

// Groups `numbers`: into the one group among them if there is one, otherwise into a new group named `name`,
// in the list of the first task. Returns { undo, group }.
export async function groupTasks(numbers, name) {
  const plan = planGroup(numbers, all());
  const previous = new Map(plan.members.map(n => [n, getIssueByNumber(n)?.parentNumber ?? null]));
  let group = plan.join;
  let created = false;
  if (group == null) {
    const tasks = numbers.map(getIssueByNumber).filter(Boolean);
    const first = tasks[0];
    const rating = groupRating(tasks);
    const labels = buildLabels({ labels: [], listName: first?.listName || 'tasks', ...rating });
    const parsed = parseIssue(await api.createIssue(name.trim(), null, labels));
    parsed.parentNumber = null;
    setState({ issues: [parsed, ...getState().issues] });
    await cache(parsed);
    group = parsed.number;
    created = true;
  }
  try {
    await each(plan.members, n => link(group, n));
  } catch (err) {
    err.group = group;
    throw err;
  }
  const undo = async () => {
    await restoreParents(previous);
    if (created) await deleteTask(group);
  };
  return { undo, group };
}

// Takes a group's tasks out of it and deletes the group itself
export async function ungroup(groupNumber) {
  const group = getIssueByNumber(groupNumber);
  const members = all().filter(i => i.parentNumber === groupNumber).map(i => i.number);
  await each(members, n => unlink(groupNumber, n));
  const before = group ? { state: group.state, labels: group.labels } : null;
  await deleteTask(groupNumber);
  return async () => {
    if (before) {
      const raw = await api.updateIssue(groupNumber, before);
      const restored = { ...parseIssue(raw), parentNumber: group.parentNumber ?? null };
      setState({ issues: [restored, ...getState().issues.filter(i => i.number !== groupNumber)] });
      await cache(restored);
    }
    await each(members, n => link(groupNumber, n));
  };
}

// Takes some tasks out of whatever group they are in
export async function removeFromGroup(numbers) {
  const previous = new Map(numbers.map(n => [n, getIssueByNumber(n)?.parentNumber ?? null]));
  await each(numbers.filter(n => previous.get(n) != null), n => unlink(previous.get(n), n));
  return () => restoreParents(previous);
}
