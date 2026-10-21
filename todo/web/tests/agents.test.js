// Offline tests for assigning to a running agent: the request marker, `.todo/agents.json`, and the status
// line's comment reading. Run with: node --test web/tests/

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { agentRequestBody, AGENT_REQUEST_MARKER } from '../js/parser.js';
import { parseAgentsFile, filterAgents, ago, latestAgentStatus, stateWord } from '../js/agents.js';
import * as api from '../js/api.js';

const ID = '7680A777-1111-4222-8333-444455556666';

test('a new agent keeps the plain marker', () => {
  assert.equal(agentRequestBody('  do it  '), `${AGENT_REQUEST_MARKER}\n\ndo it`);
  assert.equal(agentRequestBody(''), `${AGENT_REQUEST_MARKER}\n\nNo extra instructions.`);
});

test('a running agent is named by its session id on the marker line', () => {
  const body = agentRequestBody('pick this up', ID);
  assert.equal(body, `<!-- todo-agent-request to=${ID.toLowerCase()} -->\n\npick this up`);
  assert.match(body.split('\n')[0], /^<!--\s*todo-agent-request to=[0-9a-f-]{36}\s*-->$/);
});

test('anything but a session id is refused, so nothing can be smuggled into the marker', () => {
  assert.throws(() => agentRequestBody('x', 'abc'));
  assert.throws(() => agentRequestBody('x', `${ID} --> <!-- todo-agent-request`));
});

const FILE = {
  v: 1,
  updated: '2026-10-16T09:30:00Z',
  agents: [
    { id: ID, cli: 'claude', name: 'todo-12-export', state: 'busy', folder: 'agent-board-kit',
      note: 'Exporting the board', active: '2026-10-16T09:25:00Z', group: 'To-do' },
    { id: '01a140ca-0000-4000-8000-000000000001', cli: 'codex', name: '', state: 'weird', folder: 'home-budget',
      note: null, active: 'not a date', group: null },
    { id: 'not-an-id', name: 'dropped' },
    null,
  ],
};

test('agents.json is checked: bad ids dropped, unknown states idle, names fall back to the folder', () => {
  const parsed = parseAgentsFile(FILE);
  assert.equal(parsed.updated, '2026-10-16T09:30:00Z');
  assert.equal(parsed.agents.length, 2);
  assert.deepEqual(parsed.agents[0], {
    id: ID.toLowerCase(), cli: 'claude', name: 'todo-12-export', state: 'busy', folder: 'agent-board-kit',
    note: 'Exporting the board', active: '2026-10-16T09:25:00Z', group: 'To-do',
  });
  assert.equal(parsed.agents[1].name, 'home-budget');
  assert.equal(parsed.agents[1].state, 'idle');
  assert.equal(parsed.agents[1].cli, 'codex');
  assert.equal(parsed.agents[1].active, null);
  assert.equal(parsed.agents[1].note, '');
});

test('a missing or malformed file is null', () => {
  assert.equal(parseAgentsFile(null), null);
  assert.equal(parseAgentsFile({ v: 1 }), null);
  assert.equal(parseAgentsFile('[]'), null);
  assert.deepEqual(parseAgentsFile({ agents: [] }), { updated: null, agents: [] });
});

test('search matches every word across name, folder, group and note', () => {
  const { agents } = parseAgentsFile(FILE);
  assert.equal(filterAgents(agents, '').length, 2);
  assert.deepEqual(filterAgents(agents, 'board export').map(a => a.folder), ['agent-board-kit']);
  assert.deepEqual(filterAgents(agents, 'CODEX home').map(a => a.folder), ['home-budget']);
  assert.deepEqual(filterAgents(agents, 'to-do'), [filterAgents(agents, 'export')[0]]);
  assert.equal(filterAgents(agents, 'nothing-like-this').length, 0);
});

test('activity reads as time ago', () => {
  const now = new Date('2026-10-16T10:00:00Z');
  assert.equal(ago('2026-10-16T09:59:40Z', now), 'just now');
  assert.equal(ago('2026-10-16T09:55:00Z', now), '5 min ago');
  assert.equal(ago('2026-10-16T07:00:00Z', now), '3 h ago');
  assert.equal(ago('2026-10-13T10:00:00Z', now), '3 days ago');
  assert.equal(ago(null, now), '');
  assert.equal(stateWord('waiting'), 'waiting on you');
});

test('the status line takes the newest todo-agent comment and its first line', () => {
  const comments = [
    { body: '<!-- todo-agent:assigned -->\nAssigned to an agent from the web app.', created_at: '2026-10-16T09:00:00Z' },
    { body: 'An ordinary comment', created_at: '2026-10-16T09:01:00Z' },
    { body: '<!-- todo-agent-request to=x -->\n\ndo it', created_at: '2026-10-16T09:02:00Z' },
    { body: '<!-- todo-agent:started -->\nAssigned to the running agent todo-12-export.\nMore detail.',
      created_at: '2026-10-16T09:03:00Z' },
  ];
  assert.deepEqual(latestAgentStatus(comments),
    { kind: 'started', text: 'Assigned to the running agent todo-12-export.', at: '2026-10-16T09:03:00Z' });
  assert.equal(latestAgentStatus([{ body: 'nothing' }]), null);
  assert.equal(latestAgentStatus([{ body: '<!-- todo-agent:failed -->' }]).text, '');
});

test('fetchAgents reads the contents API, decodes UTF-8, and is null on 404', async () => {
  api.setToken('test-not-a-real-pat');
  const json = JSON.stringify({ v: 1, updated: '2026-10-16T09:30:00Z', agents: [{ id: ID, name: 'Café agent' }] });
  const b64 = Buffer.from(json, 'utf8').toString('base64').replace(/(.{60})/g, '$1\n');
  let url;
  globalThis.fetch = async (u) => { url = u; return { ok: true, status: 200, json: async () => ({ content: b64 }) }; };
  const data = await api.fetchAgents();
  assert.match(url, /\/contents\/\.todo\/agents\.json$/);
  assert.equal(data.agents[0].name, 'Café agent');
  globalThis.fetch = async () => ({ ok: false, status: 404 });
  assert.equal(await api.fetchAgents(), null);
  globalThis.fetch = async () => ({ ok: false, status: 500 });
  await assert.rejects(api.fetchAgents());
});
