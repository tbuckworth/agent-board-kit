// Groups of tasks: a group is an issue whose tasks are its GitHub sub-issues. Pure functions here;
// the GitHub side is in bulk.js.

export const SUBTASK_LABEL = 'subtask';

const IMPORTANCE_ORDER = { high: 3, medium: 2, low: 1 };

// Rows to show for `tasks` (already filtered and ordered): a task whose group is also shown sits under it, indented.
// `allIssues` gives each group's counts, done tasks included. `collapsed` is a Set of group numbers folded away.
// Returns [{ task, depth, childCount, doneCount, expanded }] in display order.
export function buildRows(tasks, allIssues, collapsed = new Set()) {
  const shown = new Set(tasks.map(t => t.number));
  const childrenOf = new Map();
  for (const t of tasks) {
    const p = t.parentNumber;
    if (p != null && p !== t.number && shown.has(p)) {
      if (!childrenOf.has(p)) childrenOf.set(p, []);
      childrenOf.get(p).push(t);
    }
  }
  const counts = groupCounts(allIssues);
  const rows = [];
  const placed = new Set();
  function add(task, depth) {
    if (placed.has(task.number)) return;     // a loop in parent links is shown once, not for ever
    placed.add(task.number);
    const c = counts.get(task.number) || { total: 0, done: 0 };
    const expanded = c.total > 0 && !collapsed.has(task.number);
    rows.push({ task, depth, childCount: c.total, doneCount: c.done, expanded });
    if (expanded) for (const child of childrenOf.get(task.number) || []) add(child, depth + 1);
  }
  for (const t of tasks) {
    const p = t.parentNumber;
    if (p == null || p === t.number || !shown.has(p)) add(t, 0);
  }
  // A task not reached (through a parent loop GitHub forbids, not by folding) still shows, at the top level
  for (const t of tasks) {
    if (!placed.has(t.number) && !isFolded(t, collapsed, tasks)) add(t, 0);
  }
  return rows;
}

// Inside a folded group (at any depth), as opposed to unreachable through a parent loop
function isFolded(task, collapsed, tasks) {
  const byNumber = new Map(tasks.map(t => [t.number, t]));
  const seen = new Set();
  let p = task.parentNumber;
  while (p != null && byNumber.has(p) && !seen.has(p)) {
    if (collapsed.has(p)) return true;
    seen.add(p);
    p = byNumber.get(p).parentNumber;
  }
  return false;
}

// group number -> { total, done } over every task that names it as its group
export function groupCounts(allIssues) {
  const counts = new Map();
  for (const i of allIssues) {
    if (i.parentNumber == null) continue;
    const c = counts.get(i.parentNumber) || { total: 0, done: 0 };
    c.total += 1;
    if (i.state === 'CLOSED') c.done += 1;
    counts.set(i.parentNumber, c);
  }
  return counts;
}

// The open tasks in a group, and in groups within it
export function openDescendants(number, allIssues) {
  const out = [];
  const seen = new Set([number]);
  const queue = [number];
  while (queue.length) {
    const n = queue.shift();
    for (const i of allIssues) {
      if (i.parentNumber === n && !seen.has(i.number)) {
        seen.add(i.number);
        queue.push(i.number);
        if (i.state === 'OPEN') out.push(i);
      }
    }
  }
  return out;
}

// The selected numbers plus the open tasks of any selected group, each once, groups first
export function withGroupMembers(numbers, allIssues) {
  const out = [...numbers];
  const seen = new Set(numbers);
  for (const n of numbers) {
    for (const d of openDescendants(n, allIssues)) {
      if (!seen.has(d.number)) { seen.add(d.number); out.push(d.number); }
    }
  }
  return out;
}

// A new group's rating: the highest importance among its tasks, urgent if any is, the earliest due date
export function groupRating(tasks) {
  let importance = null;
  for (const t of tasks) {
    if (t.importance && (!importance || IMPORTANCE_ORDER[t.importance] > IMPORTANCE_ORDER[importance])) {
      importance = t.importance;
    }
  }
  const dues = tasks.map(t => t.due).filter(Boolean).sort();
  return {
    importance,
    urgency: importance ? (tasks.some(t => t.urgency === 'high') ? 'high' : 'low') : null,
    due: dues[0] || null,
  };
}

// What grouping `numbers` means: join the one group among them, or make a new group of them all.
// Returns { join: groupNumber, members: [...] } or { join: null, members: [...] }.
export function planGroup(numbers, allIssues) {
  const counts = groupCounts(allIssues);
  const groups = numbers.filter(n => (counts.get(n)?.total || 0) > 0);
  if (groups.length === 1) {
    return { join: groups[0], members: numbers.filter(n => n !== groups[0]) };
  }
  return { join: null, members: [...numbers] };
}

// Folded groups, remembered in this browser
const COLLAPSED_KEY = 'todo_collapsed_groups';

export function loadCollapsed(storage = globalThis.localStorage) {
  try {
    return new Set(JSON.parse(storage?.getItem(COLLAPSED_KEY) || '[]'));
  } catch {
    return new Set();
  }
}

export function saveCollapsed(set, storage = globalThis.localStorage) {
  try {
    storage?.setItem(COLLAPSED_KEY, JSON.stringify([...set]));
  } catch {
    // Private mode or storage full: folding just isn't remembered
  }
}
