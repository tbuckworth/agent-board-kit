// The tasks repo is entered in Settings as "owner/name"; every API call uses it.
// Run with: node --test web/tests/

import { test } from 'node:test';
import assert from 'node:assert/strict';

import * as api from '../js/api.js';

test('the repo setting accepts owner/name and close variants, and nothing else', () => {
  assert.equal(api.parseRepo('example-user/tasks'), 'example-user/tasks');
  assert.equal(api.parseRepo('  example-user/my.tasks_2  '), 'example-user/my.tasks_2');
  assert.equal(api.parseRepo('https://github.com/example-user/tasks'), 'example-user/tasks');
  assert.equal(api.parseRepo('https://github.com/example-user/tasks.git'), 'example-user/tasks');
  assert.equal(api.parseRepo('example-user/tasks/'), 'example-user/tasks');
  for (const bad of ['', 'tasks', 'a/b/c', '/tasks', 'owner/', '-owner/tasks', 'own er/tasks', 'owner/..',
    'owner/ta"sks', 'owner/tasks") { x }', null, undefined]) {
    assert.equal(api.parseRepo(bad), null, String(bad));
  }
});

test('every call needs a token and a repo; the repo goes into the requests', async () => {
  api.clearToken();
  api.clearRepo();
  assert.equal(api.isConfigured(), false);
  await assert.rejects(api.testConnection(), api.AuthError);
  api.setToken('test-not-a-real-pat');
  await assert.rejects(api.testConnection(), api.RepoError);       // not an auth failure: the PAT is kept
  assert.equal(api.getToken(), 'test-not-a-real-pat');
  assert.throws(() => api.setRepo('nope'), /owner\/name/);
  assert.equal(api.setRepo('https://github.com/example-user/tasks'), 'example-user/tasks');
  assert.equal(api.getRepo(), 'example-user/tasks');
  assert.equal(api.isConfigured(), true);

  const seen = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen.push({ url, body: init?.body || '' });
    if (url.endsWith('/graphql')) return { ok: true, status: 200, json: async () => ({ data: { repository: { name: 'tasks' } } }) };
    return { ok: false, status: 404, json: async () => ({}) };
  };
  try {
    assert.equal(await api.testConnection(), true);
    assert.equal(await api.fetchConfig(), null);
  } finally {
    globalThis.fetch = realFetch;
  }
  assert.match(JSON.parse(seen[0].body).query, /repository\(owner: "example-user", name: "tasks"\)/);
  assert.equal(seen[1].url, 'https://api.github.com/repos/example-user/tasks/contents/.todo/config.json');
  api.clearRepo();
  api.clearToken();
});
