// Offline tests for the pure helpers behind importance, urgency, links and Assign to agent.
// Run with: node --test web/tests/

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';

import {
  extractImportance, extractUrgency, extractDue, ratingFields, parseIssue, buildLabels,
  isEffectivelyUrgent, daysUntil, localDateString, formatDue, isRatedList,
  agentRequestBody, AGENT_REQUEST_MARKER, setUnrated, unratedFromConfig,
} from '../js/parser.js';
import { linkSegments } from '../js/render.js';
import { setState, matchesFilters, groupForList, isTaskRated } from '../js/state.js';

// uuid.js needs Web Crypto, which Node 18 does not expose as a global
if (!globalThis.crypto) globalThis.crypto = webcrypto;

// Local dates, so the tests hold in any time zone
const TODAY = new Date(2026, 9, 9, 23, 30); // 9 Oct 2026, late evening

test('label extraction', () => {
  assert.equal(extractImportance(['list:tasks', 'importance:high']), 'high');
  assert.equal(extractImportance(['Importance:Low']), 'low');
  assert.equal(extractImportance(['importance:urgent']), null);
  assert.equal(extractImportance(['list:tasks']), null);
  assert.equal(extractUrgency(['urgency:high']), 'high');
  assert.equal(extractUrgency(['urgency:low']), 'low');
  assert.equal(extractUrgency(['urgency:medium']), null);
  assert.equal(extractDue(['due:2026-10-12']), '2026-10-12');
  assert.equal(extractDue(['due:2026-13-01']), null);
  assert.equal(extractDue(['due:soon']), null);
  assert.deepEqual(ratingFields([]), { importance: null, urgency: null, due: null });
});

test('parseIssue reads ratings and no longer carries priority', () => {
  const issue = parseIssue({
    number: 7,
    id: 'I_7',
    title: 'Book dentist',
    body: 'Notes\n\n## Checklist\n\n- [ ] call\n- [x] find number',
    state: 'OPEN',
    labels: { nodes: [{ name: 'list:tasks' }, { name: 'priority:high' }, { name: 'importance:medium' },
      { name: 'urgency:low' }, { name: 'due:2026-10-20' }] },
  });
  assert.equal(issue.importance, 'medium');
  assert.equal(issue.urgency, 'low');
  assert.equal(issue.due, '2026-10-20');
  assert.equal('priority' in issue, false);
  assert.equal(issue.checklist.length, 2);
});

test('effective urgency uses the local date and a 14-day window', () => {
  assert.equal(localDateString(TODAY), '2026-10-09');
  assert.equal(daysUntil('2026-10-09', TODAY), 0);
  assert.equal(daysUntil('2026-10-23', TODAY), 14);
  assert.equal(daysUntil('2026-10-01', TODAY), -8);

  assert.equal(isEffectivelyUrgent({ urgency: 'high' }, TODAY), true);
  assert.equal(isEffectivelyUrgent({ urgency: 'low' }, TODAY), false);
  assert.equal(isEffectivelyUrgent({}, TODAY), false);
  assert.equal(isEffectivelyUrgent({ urgency: 'low', due: '2026-10-23' }, TODAY), true);
  assert.equal(isEffectivelyUrgent({ urgency: 'low', due: '2026-10-24' }, TODAY), false);
  assert.equal(isEffectivelyUrgent({ due: '2026-09-01' }, TODAY), true); // overdue

  // Across a daylight-saving change (late March in Europe)
  const spring = new Date(2026, 2, 20, 0, 15);
  assert.equal(daysUntil('2026-04-03', spring), 14);
  assert.equal(isEffectivelyUrgent({ due: '2026-04-03' }, spring), true);
  assert.equal(isEffectivelyUrgent({ due: '2026-04-04' }, spring), false);
});

test('due dates display as "12 Oct"', () => {
  assert.equal(formatDue('2026-10-12'), '12 Oct');
  assert.equal(formatDue('2027-01-05'), '5 Jan');
});

test('rated and unrated lists', () => {
  // Nothing configured: every list is rated
  setUnrated({});
  assert.equal(isRatedList('reading-log', 'Archive'), true);
  // As .todo/config.json names them, in any case
  setUnrated(unratedFromConfig({ unratedGroups: ['Archive', 'Journal', '', 7], unratedLists: ['Reading-Log', 'not-to-buy'] }));
  assert.equal(isRatedList('tasks', null), true);
  assert.equal(isRatedList('to-buy', 'Home'), true);
  assert.equal(isRatedList('recipes', 'archive'), false);
  assert.equal(isRatedList('dreams', 'JOURNAL'), false);
  assert.equal(isRatedList('reading-log', 'Home'), false);
  assert.equal(isRatedList('Not-To-Buy', null), false);
  assert.deepEqual(unratedFromConfig(null), { groups: [], lists: [] });
  setUnrated({});
});

test('a list\'s group comes from listMetadata, then the issue\'s group: label', () => {
  setUnrated({ groups: ['Reading', 'Journal'] });
  setState({
    listMetadata: [
      { listName: 'books', groupName: 'Reading', sortOrder: 0 },
      { listName: 'Tasks', groupName: null, sortOrder: 0 },
    ],
  });
  assert.equal(groupForList('BOOKS'), 'Reading');
  assert.equal(groupForList('tasks', 'Archive'), null); // metadata says ungrouped
  assert.equal(groupForList('unknown', 'Journal'), 'Journal');
  assert.equal(isTaskRated({ listName: 'books' }), false);
  assert.equal(isTaskRated({ listName: 'tasks' }), true);
  assert.equal(isTaskRated({ listName: 'poems', groupName: 'Journal' }), false);
  setState({ listMetadata: [] });
  setUnrated({});
});

test('filters: importance (with unrated) and urgent', () => {
  const none = { section: null, importance: null, urgent: false, action: null };
  const high = { importance: 'high', urgency: 'low', actionType: 'manual' };
  const unrated = { importance: null, urgency: null, actionType: 'manual' };
  const dueSoon = { importance: 'low', urgency: 'low', due: '2026-10-12', actionType: 'manual' };

  assert.equal(matchesFilters(high, none, TODAY), true);
  assert.equal(matchesFilters(high, { ...none, importance: 'high' }, TODAY), true);
  assert.equal(matchesFilters(high, { ...none, importance: 'low' }, TODAY), false);
  assert.equal(matchesFilters(unrated, { ...none, importance: 'unrated' }, TODAY), true);
  assert.equal(matchesFilters(high, { ...none, importance: 'unrated' }, TODAY), false);
  assert.equal(matchesFilters(high, { ...none, urgent: true }, TODAY), false);
  assert.equal(matchesFilters(dueSoon, { ...none, urgent: true }, TODAY), true);
});

test('buildLabels writes ratings, drops priority and keeps unmanaged labels', () => {
  const labels = buildLabels({
    labels: ['list:tasks', 'Priority:High', 'priority:medium', 'importance:low', 'urgency:high',
      'due:2026-01-01', 'source:import', 'status:agent-assigned', 'type:follow-up'],
    listName: 'tasks',
    actionType: 'manual',
    importance: 'high',
    urgency: 'low',
    due: '2026-10-20',
  });
  assert.deepEqual([...labels].sort(), [
    'due:2026-10-20', 'importance:high', 'list:tasks', 'source:import',
    'status:agent-assigned', 'type:follow-up', 'urgency:low',
  ]);

  // Cleared due date and no source label yet
  const fresh = buildLabels({ labels: [], listName: 'to-buy', importance: 'medium', urgency: 'low', due: null });
  assert.deepEqual(fresh, ['list:to-buy', 'importance:medium', 'urgency:low', 'source:app']);

  // An unrated task gets no rating labels, and an old priority still goes
  const plain = buildLabels({ labels: ['list:books', 'priority:medium', 'source:app'], listName: 'books' });
  assert.deepEqual(plain, ['source:app', 'list:books']);
});

test('agent request comment starts with the marker line', () => {
  const body = agentRequestBody('  Find the form and fill it in  ');
  assert.equal(body.split('\n')[0], AGENT_REQUEST_MARKER);
  assert.equal(body, '<!-- todo-agent-request -->\n\nFind the form and fill it in');
  assert.equal(agentRequestBody(''), '<!-- todo-agent-request -->\n\nNo extra instructions.');
  assert.equal(agentRequestBody(null), '<!-- todo-agent-request -->\n\nNo extra instructions.');
});

test('linkSegments finds bare URLs and markdown links', () => {
  assert.deepEqual(linkSegments('plain text'), [{ text: 'plain text' }]);
  assert.deepEqual(linkSegments(''), []);
  assert.deepEqual(linkSegments('see https://example.com/a.'), [
    { text: 'see ' },
    { text: 'https://example.com/a', href: 'https://example.com/a' },
    { text: '.' },
  ]);
  assert.deepEqual(linkSegments('[the form](https://gov.uk/form) then http://x.org/p?q=1'), [
    { text: 'the form', href: 'https://gov.uk/form' },
    { text: ' then ' },
    { text: 'http://x.org/p?q=1', href: 'http://x.org/p?q=1' },
  ]);
  // Balanced parentheses stay in the URL; an enclosing one does not
  assert.deepEqual(linkSegments('(https://en.wikipedia.org/wiki/Foo_(bar))'), [
    { text: '(' },
    { text: 'https://en.wikipedia.org/wiki/Foo_(bar)', href: 'https://en.wikipedia.org/wiki/Foo_(bar)' },
    { text: ')' },
  ]);
  assert.deepEqual(linkSegments('[w](https://en.wikipedia.org/wiki/A_(b))'), [
    { text: 'w', href: 'https://en.wikipedia.org/wiki/A_(b)' },
  ]);
  // Only http(s) becomes a link
  assert.deepEqual(linkSegments('[x](javascript:alert(1)) ftp://a.b'), [{ text: '[x](javascript:alert(1)) ftp://a.b' }]);
  assert.deepEqual(linkSegments('**First step:** open https://a.io/x**'), [
    { text: '**First step:** open ' },
    { text: 'https://a.io/x', href: 'https://a.io/x' },
    { text: '**' },
  ]);
});
