// Offline tests for web/js/api.js with fetch stubbed: label creation, closing and the contents-API reads.
// Run with: node --test web/tests/

import { test } from 'node:test';
import assert from 'node:assert/strict';

import * as api from '../js/api.js';

api.setRepo('example-user/tasks');

const calls = [];
let respond = () => ({});

// GraphQL requests get respond(query, variables); an Error is returned as GitHub's GraphQL error
function stubGraphQL() {
  globalThis.fetch = async (url, init) => {
    const { query, variables } = JSON.parse(init.body);
    calls.push({ query, variables });
    const data = await respond(query, variables);
    const json = data instanceof Error ? { errors: [{ message: data.message }] } : { data };
    return { ok: true, status: 200, json: async () => json };
  };
}

const repoMeta = { repository: { id: 'R_1', labels: { pageInfo: { hasNextPage: false }, nodes: [
  { id: 'L_list', name: 'list:tasks' },
] } } };

function rawIssue(labels) {
  return {
    number: 7, id: 'I_7', title: 'Book dentist', body: '', state: 'OPEN', stateReason: null,
    createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-10-12T00:00:00Z',
    labels: { nodes: labels.map(name => ({ name })) },
  };
}

function reset(createLabel) {
  calls.length = 0;
  api.setToken('test-not-a-real-pat'); // also forgets the label map
  stubGraphQL();
  respond = (query) => {
    if (query.includes('createLabel')) return createLabel();
    if (query.includes('label(name: $name)')) return { repository: { label: { id: 'L_due' } } };
    if (query.includes('createIssue')) return { createIssue: { issue: rawIssue(['list:tasks', 'due:2026-11-01']) } };
    if (query.includes('updateIssue')) return { updateIssue: { issue: rawIssue(['list:tasks', 'due:2026-11-01']) } };
    if (query.includes('issue(number: 7) { id }')) return { repository: { issue: { id: 'I_7' } } };
    return repoMeta;
  };
}

test('a label another client just created is looked up, not dropped', async () => {
  reset(() => new Error('Name has already been taken'));
  await api.createIssue('Book dentist', '', ['list:tasks', 'due:2026-11-01']);
  const create = calls.find(c => c.query.includes('createIssue'));
  assert.deepEqual(create.variables.input.labelIds, ['L_list', 'L_due']);
});

test('a label that cannot be created fails the create instead of being dropped', async () => {
  reset(() => new Error('Resource not accessible by integration'));
  await assert.rejects(api.createIssue('Book dentist', '', ['list:tasks', 'due:2026-11-01']), /not accessible/);
  assert.equal(calls.some(c => c.query.includes('createIssue')), false);
});

test('a label that cannot be created fails the update instead of being dropped', async () => {
  reset(() => new Error('Resource not accessible by integration'));
  await assert.rejects(api.updateIssue(7, { labels: ['list:tasks', 'due:2026-11-01'] }), /not accessible/);
  assert.equal(calls.some(c => c.query.includes('updateIssue')), false);
});

test('closing needs an explicit reason', async () => {
  reset(() => ({}));
  await assert.rejects(api.closeIssue(7), /stateReason/);
  assert.equal(calls.some(c => c.query.includes('closeIssue')), false);
});

test('the config is decoded as UTF-8 and read uncached', async () => {
  api.setToken('test-not-a-real-pat');
  const json = JSON.stringify({ groups: [{ name: 'Café', sortOrder: 0 }] });
  const b64 = Buffer.from(json, 'utf8').toString('base64').replace(/(.{60})/g, '$1\n');
  let url, init;
  globalThis.fetch = async (u, i) => {
    url = u; init = i;
    return { ok: true, status: 200, json: async () => ({ content: b64, sha: 'abc' }) };
  };
  const result = await api.fetchConfig();
  assert.match(url, /\/contents\/\.todo\/config\.json$/);
  assert.equal(init.cache, 'no-store');
  assert.equal(result.config.groups[0].name, 'Café');
  assert.equal(result.sha, 'abc');
  globalThis.fetch = async () => ({ ok: false, status: 404 });
  assert.equal(await api.fetchConfig(), null);
});
