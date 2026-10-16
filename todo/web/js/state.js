// Simple pub/sub state management
import { isEffectivelyUrgent, isRatedList, localDateString } from './parser.js';
import { matchesTagFilter, loadTagFilter, saveTagFilter, waitingLast } from './tags.js';

const listeners = new Map();
let state = {
  issues: [],        // All issues from IndexedDB/API
  labels: [],        // All labels from API
  groups: [],        // { name, sortOrder } from IndexedDB
  listMetadata: [],  // { listName, groupName, sortOrder } from IndexedDB
  syncState: 'idle', // idle | syncing | success | error | auth_error
  lastError: null,
  filters: {
    section: null,
    importance: null,  // high | medium | low | unrated
    urgent: false,     // effectively urgent only
    tags: loadTagFilter(), // { tag: 'only' | 'hide' }, remembered on this device
    showCompleted: false,
  },
  currentView: 'home',
  viewParams: {},
};

export function getState() {
  return state;
}

export function setState(partial) {
  const prev = state;
  if (partial.issues) partial = { ...partial, issues: keepParents(partial.issues, prev.issues) };
  state = { ...state, ...partial };
  // Notify all listeners
  for (const [, cb] of listeners) {
    cb(state, prev);
  }
}

// A task parsed from an answer that didn't ask for its group (parentNumber undefined) keeps the one known
function keepParents(issues, previous) {
  if (!issues.some(i => i.parentNumber === undefined)) return issues;
  const known = new Map(previous.map(i => [i.number, i.parentNumber]));
  return issues.map(i => (i.parentNumber === undefined && known.get(i.number) !== undefined)
    ? { ...i, parentNumber: known.get(i.number) } : i);
}

export function updateFilters(partial) {
  if (partial.tags) saveTagFilter(partial.tags);
  setState({ filters: { ...state.filters, ...partial } });
}

let nextId = 0;
export function subscribe(cb) {
  const id = nextId++;
  listeners.set(id, cb);
  return () => listeners.delete(id);
}

// Derived data helpers

export function getIssueByNumber(num) {
  return state.issues.find(i => i.number === num);
}

// The section/importance/urgent/tag chips, shared by lists, counts and search
export function matchesFilters(issue, filters = state.filters, today = new Date()) {
  if (filters.section && issue.section !== filters.section) return false;
  if (filters.importance && (issue.importance || 'unrated') !== filters.importance) return false;
  if (filters.urgent && !isEffectivelyUrgent(issue, today)) return false;
  if (!matchesTagFilter(issue, filters.tags)) return false;
  return true;
}

// A list's group comes from .todo/config.json listMetadata; an issue's group: label is the fallback
export function groupForList(listName, fallback = null) {
  if (!listName) return fallback;
  const meta = state.listMetadata.find(m => m.listName?.toLowerCase() === listName.toLowerCase());
  return meta ? meta.groupName || null : fallback;
}

export function isTaskRated(task) {
  return isRatedList(task.listName, groupForList(task.listName, task.groupName));
}

// Newest first, with tasks still waiting on someone last
export function getFilteredIssues(listName) {
  const { filters, issues } = state;
  return waitingLast(issues.filter(issue => {
    if (listName && issue.listName?.toLowerCase() !== listName.toLowerCase()) return false;
    if (!filters.showCompleted && issue.state === 'CLOSED') return false;
    return matchesFilters(issue, filters);
  }).sort((a, b) => (b.updatedAt || '').localeCompare(a.updatedAt || '')), localDateString());
}

// Every tag in use, for the tag editor's suggestions
export function getAllTags() {
  const tags = new Set();
  for (const issue of state.issues) for (const t of issue.tags || []) tags.add(t);
  for (const l of state.labels || []) {
    const name = l.name || l;
    if (typeof name === 'string' && name.toLowerCase().startsWith('tag:')) tags.add(name.slice(4).toLowerCase());
  }
  return [...tags].sort();
}

export function getCompletedIssues(listName) {
  return state.issues.filter(issue => {
    if (listName && issue.listName?.toLowerCase() !== listName.toLowerCase()) return false;
    return issue.state === 'CLOSED';
  }).sort((a, b) => (b.updatedAt || '').localeCompare(a.updatedAt || ''));
}

export function getAllLists() {
  const listSet = new Map(); // lowercase → canonical name
  for (const issue of state.issues) {
    if (issue.listName) listSet.set(issue.listName.toLowerCase(), issue.listName.toLowerCase());
  }
  // Also include lists from metadata that might have no issues
  for (const meta of state.listMetadata) {
    listSet.set(meta.listName.toLowerCase(), meta.listName.toLowerCase());
  }
  return [...listSet.values()].sort((a, b) => a.localeCompare(b));
}

export function getAllSections() {
  const sectionSet = new Set();
  for (const issue of state.issues) {
    if (issue.section) sectionSet.add(issue.section);
  }
  return [...sectionSet].sort();
}

export function getActiveCountByList() {
  const { filters } = state;
  const counts = {};
  for (const issue of state.issues) {
    if (issue.state !== 'OPEN') continue;
    if (!matchesFilters(issue, filters)) continue;
    const list = (issue.listName || 'Inbox').toLowerCase();
    counts[list] = (counts[list] || 0) + 1;
  }
  return counts;
}

export function getTopLevelItems() {
  const { groups, listMetadata, issues } = state;
  const metadataMap = new Map(listMetadata.map(m => [m.listName.toLowerCase(), m]));
  const counts = getActiveCountByList();
  const allLists = getAllLists();

  // Build group → lists mapping
  const groupListsMap = new Map();
  const ungroupedLists = [];

  for (const list of allLists) {
    const meta = metadataMap.get(list.toLowerCase());
    const groupName = meta?.groupName;
    if (groupName) {
      if (!groupListsMap.has(groupName)) groupListsMap.set(groupName, []);
      groupListsMap.get(groupName).push(list);
    } else {
      ungroupedLists.push(list);
    }
  }

  // Add groups that exist in the groups table but may have no lists yet
  for (const g of groups) {
    if (!groupListsMap.has(g.name)) groupListsMap.set(g.name, []);
  }

  const items = [];

  // Groups
  for (const [groupName, lists] of groupListsMap) {
    const group = groups.find(g => g.name === groupName);
    const totalTasks = lists.reduce((sum, l) => sum + (counts[l] || 0), 0);
    items.push({
      type: 'group',
      name: groupName,
      taskCount: totalTasks,
      sortOrder: group?.sortOrder ?? 0,
      lists,
    });
  }

  // Ungrouped lists
  for (const list of ungroupedLists) {
    const meta = metadataMap.get(list.toLowerCase());
    items.push({
      type: 'list',
      name: list,
      taskCount: counts[list] || 0,
      sortOrder: meta?.sortOrder ?? 0,
    });
  }

  items.sort((a, b) => a.sortOrder - b.sortOrder);
  return items;
}
