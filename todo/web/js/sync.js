// Loading the tasks, groups and labels from GitHub into the state and the IndexedDB cache. A failure sets
// syncState and is thrown; what the user is shown, and whether the token is forgotten, is the caller's choice.

import { getState, setState } from './state.js';
import { parseIssue, ratingFields, bodyFields, setUnrated, unratedFromConfig } from './parser.js';
import { tagFields } from './tags.js';
import * as api from './api.js';
import * as db from './db.js';

// Cached issues, with the label-derived fields that older versions did not store
export async function loadCachedIssues() {
  const cached = await db.issues.getAll();
  return cached.map(issue => ({
    ...issue,
    ...ratingFields(issue.labels || []),
    ...tagFields(issue.labels || []),
    ...(issue.rawBody !== undefined ? bodyFields(issue.rawBody) : {}),
  }));
}

// A sync since lastSync never sees an issue deleted on GitHub, so once a day it reloads everything
const FULL_SYNC_EVERY_MS = 24 * 60 * 60 * 1000;

// Returns the issues now in the state
export async function syncAllIssues(fullSync = false) {
  setState({ syncState: 'syncing' });
  try {
    await sync(fullSync);
  } catch (err) {
    setState({ syncState: err instanceof api.AuthError ? 'auth_error' : 'error', lastError: err.message });
    throw err;
  }
  setState({ syncState: 'success' });
  return getState().issues;
}

async function sync(fullSync) {
  // Load from IndexedDB cache first
  const cachedIssues = await loadCachedIssues();
  const cachedGroups = await db.groups.getAll();
  const cachedMeta = await db.listMeta.getAll();

  if (cachedIssues.length > 0) {
    setState({
      issues: cachedIssues,
      groups: cachedGroups,
      listMetadata: cachedMeta,
    });
  }

  const lastFull = await db.meta.get('lastFullSync');
  if (!lastFull || Date.now() - Date.parse(lastFull) > FULL_SYNC_EVERY_MS) fullSync = true;

  // Fetch from API
  const lastSync = fullSync ? null : await db.meta.get('lastSync');
  const rawIssues = await api.fetchAllIssues(lastSync);
  const parsedIssues = rawIssues.map(parseIssue);

  if (lastSync && !fullSync) {
    // Merge with cache
    const issueMap = new Map(getState().issues.map(i => [i.number, i]));
    for (const issue of parsedIssues) {
      issueMap.set(issue.number, issue);
    }
    setState({ issues: [...issueMap.values()] });
    await db.issues.putAll(parsedIssues);
  } else {
    // Full replace
    setState({ issues: parsedIssues });
    await db.issues.clear();
    await db.issues.putAll(parsedIssues);
  }

  // Save sync timestamp
  await db.meta.set('lastSync', new Date().toISOString());
  if (fullSync) await db.meta.set('lastFullSync', new Date().toISOString());

  // Groups and list order from .todo/config.json, then the labels. A failure here fails the sync like any other
  // (the issues above are already saved).
  const configResult = await api.fetchConfig();
  // Which lists are not rated: from the config, remembered for the next start
  const unrated = unratedFromConfig(configResult?.config);
  setUnrated(unrated);
  await db.meta.set('unrated', unrated);
  if (configResult) {
    const { config } = configResult;
    const groups = (config.groups || []).map(g => ({ name: g.name, sortOrder: g.sortOrder }));
    const listMetadata = (config.listMetadata || []).map(m => ({
      listName: m.listName, groupName: m.groupName, sortOrder: m.sortOrder,
    }));
    await db.groups.clear();
    if (groups.length > 0) await db.groups.putAll(groups);
    await db.listMeta.clear();
    if (listMetadata.length > 0) await db.listMeta.putAll(listMetadata);
    setState({ groups, listMetadata });
  }

  const labels = await api.fetchLabels();
  await db.labels.clear();
  await db.labels.putAll(labels);
  setState({ labels });
}
