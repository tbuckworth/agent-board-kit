// Offline tests for web/js/sync.js: a sync reports its failure to the caller rather than handling it.
// GitHub and IndexedDB are stubbed (an empty cache that accepts every write). Run with: node --test web/tests/

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';

import { getState } from '../js/state.js';
import * as api from '../js/api.js';

api.setRepo('example-user/tasks');
import { syncAllIssues } from '../js/sync.js';

if (!globalThis.crypto) globalThis.crypto = webcrypto;

function request(result) {
  const req = {};
  setTimeout(() => { req.result = result; req.onsuccess?.(); });
  return req;
}
const store = {
  getAll: () => request([]), get: () => request(undefined), put: () => request(), delete: () => request(),
  clear: () => request(),
};
globalThis.indexedDB = {
  open: () => request({
    objectStoreNames: { contains: () => true },
    transaction: () => {
      const t = { objectStore: () => store };
      setTimeout(() => t.oncomplete?.());
      return t;
    },
  }),
};

const empty = { pageInfo: { hasNextPage: false, endCursor: null }, nodes: [] };

test('a sync returns the issues it loaded', async () => {
  api.setToken('test-not-a-real-pat');
  globalThis.fetch = async (url) => {
    if (url.includes('/contents/')) return { ok: false, status: 404 };
    return { ok: true, status: 200, json: async () => ({ data: { repository: { issues: empty, labels: empty } } }) };
  };
  assert.deepEqual(await syncAllIssues(true), []);
  assert.equal(getState().syncState, 'success');
});

test('a rejected token is thrown to the caller, not handled here', async () => {
  api.setToken('test-not-a-real-pat');
  globalThis.fetch = async () => ({ ok: false, status: 401 });
  await assert.rejects(syncAllIssues(true), api.AuthError);
  assert.equal(getState().syncState, 'auth_error');
  assert.equal(api.getToken(), 'test-not-a-real-pat'); // forgetting it is the app's decision
});

test('any other failure is thrown too', async () => {
  api.setToken('test-not-a-real-pat');
  globalThis.fetch = async () => ({ ok: false, status: 502, text: async () => 'bad gateway' });
  await assert.rejects(syncAllIssues(true), /502/);
  assert.equal(getState().syncState, 'error');
});

test('a failed groups fetch fails the sync, after the issues are saved', async () => {
  api.setToken('test-not-a-real-pat');
  globalThis.fetch = async (url) => {
    if (url.includes('/contents/')) return { ok: false, status: 500, text: async () => 'config is down' };
    return { ok: true, status: 200, json: async () => ({ data: { repository: { issues: empty, labels: empty } } }) };
  };
  await assert.rejects(syncAllIssues(true), /500/);
  assert.equal(getState().syncState, 'error');
});
