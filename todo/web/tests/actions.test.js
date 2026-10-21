// Offline tests for web/js/actions.js, with GitHub's GraphQL endpoint stubbed (no network, no IndexedDB:
// the cache write fails, as it may in a browser, and must not turn a success into a failure).
// Run with: node --test web/tests/

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';

import { setState, getIssueByNumber } from '../js/state.js';
import * as api from '../js/api.js';
import { archiveTask, saveRating, setFocus } from '../js/actions.js';

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const calls = [];
let respond = () => ({});

globalThis.fetch = async (url, init) => {
  const { query, variables } = JSON.parse(init.body);
  calls.push({ query, variables });
  const data = await respond(query, variables);
  if (data instanceof Error) return { ok: false, status: 502, text: async () => data.message };
  return { ok: true, status: 200, json: async () => ({ data }) };
};

api.setToken('test-not-a-real-pat');
api.setRepo('example-user/tasks');

function issue(labels, extra = {}) {
  return {
    number: 7, nodeId: 'I_7', title: 'Book dentist', rawBody: '', body: '', checklist: [], claudeNotes: null,
    state: 'OPEN', stateReason: null, listName: 'tasks', labels, importance: null, urgency: null, due: null,
    updatedAt: '2026-10-01T00:00:00Z', ...extra,
  };
}

function rawIssue(labels, extra = {}) {
  return {
    number: 7, id: 'I_7', title: 'Book dentist', body: '', state: 'OPEN', stateReason: null,
    createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-10-12T00:00:00Z',
    labels: { nodes: labels.map(name => ({ name })) }, ...extra,
  };
}

const labelMap = { repository: { id: 'R_1', labels: { pageInfo: { hasNextPage: false }, nodes: [
  { id: 'L_list', name: 'list:tasks' }, { id: 'L_focus', name: 'focus:week' },
  { id: 'L_high', name: 'importance:high' }, { id: 'L_ulow', name: 'urgency:low' },
  { id: 'L_uhigh', name: 'urgency:high' }, { id: 'L_agent', name: 'status:agent-assigned' },
] } } };

test('a failed archive puts back only what it changed', async () => {
  setState({ issues: [issue(['list:tasks'])] });
  respond = (query) => {
    if (query.includes('issue(number: 7) { id }')) return { repository: { issue: { id: 'I_7' } } };
    return new Error('boom');
  };
  const pending = archiveTask(7);
  assert.equal(getIssueByNumber(7).state, 'CLOSED'); // shown at once
  // Meanwhile another change to the same task lands
  setState({ issues: [{ ...getIssueByNumber(7), labels: ['list:tasks', 'focus:week'] }] });
  await assert.rejects(pending, /boom/);
  const after = getIssueByNumber(7);
  assert.equal(after.state, 'OPEN');
  assert.equal(after.stateReason, null);
  assert.deepEqual(after.labels, ['list:tasks', 'focus:week']);
});

test('a rating is written on top of the labels GitHub has now', async () => {
  calls.length = 0;
  setState({ issues: [issue(['list:tasks'])] }); // stale: no focus:week here
  respond = (query, variables) => {
    if (query.includes('labels(first: 100) { nodes { name } }')) {
      return { repository: { issue: { labels: { nodes: [{ name: 'list:tasks' }, { name: 'focus:week' }] } } } };
    }
    if (query.includes('labels(first: 100') && query.includes('pageInfo')) return labelMap;
    if (query.includes('repository(owner') && query.includes(' id')) {
      return { repository: { id: 'R_1', issue: { id: 'I_7' }, labels: labelMap.repository.labels } };
    }
    if (query.includes('updateIssue')) {
      return { updateIssue: { issue: rawIssue(['list:tasks', 'focus:week', 'importance:high', 'urgency:low']) } };
    }
    return {};
  };
  const result = await saveRating(7, { importance: 'high' });
  const update = calls.find(c => c.query.includes('updateIssue'));
  assert.deepEqual(update.variables.input.labelIds.sort(), ['L_focus', 'L_high', 'L_list', 'L_ulow']);
  assert.equal(result.importance, 'high');
  assert.equal(result.urgency, 'low'); // defaulted: urgency was not part of the change
  assert.ok(getIssueByNumber(7).labels.includes('focus:week'));
});

test('an explicit urgency change is not overridden', async () => {
  calls.length = 0;
  setState({ issues: [issue(['list:tasks', 'importance:high', 'urgency:high'], { importance: 'high', urgency: 'high' })] });
  respond = (query) => {
    if (query.includes('labels(first: 100) { nodes { name } }')) {
      return { repository: { issue: { labels: { nodes: [{ name: 'list:tasks' }, { name: 'importance:high' }, { name: 'urgency:high' }] } } } };
    }
    if (query.includes('updateIssue')) return { updateIssue: { issue: rawIssue(['list:tasks', 'importance:high']) } };
    return { repository: { id: 'R_1', issue: { id: 'I_7' }, labels: labelMap.repository.labels } };
  };
  await saveRating(7, { urgency: null });
  const update = calls.find(c => c.query.includes('updateIssue'));
  assert.deepEqual(update.variables.input.labelIds.sort(), ['L_high', 'L_list']);
});

test('focus succeeds even though the cache write fails', async () => {
  setState({ issues: [issue(['list:tasks'])] });
  respond = (query) => {
    if (query.includes('addLabelsToLabelable')) {
      return { addLabelsToLabelable: { labelable: rawIssue(['list:tasks', 'focus:week']) } };
    }
    return labelMap;
  };
  const result = await setFocus(7, true);
  assert.deepEqual(result.labels, ['list:tasks', 'focus:week']);
  assert.deepEqual(getIssueByNumber(7).labels, ['list:tasks', 'focus:week']);
});
