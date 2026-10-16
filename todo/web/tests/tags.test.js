// Offline tests for tags and "waiting on". Run with: node --test web/tests/

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';

import {
  normaliseTag, extractTags, extractFollowUp, tagFields, splitWaitingLine, withWaitingLine, waitingState,
  waitingLast, waitingText, matchesTagFilter, nextTagMode, withTagMode, filterTags, loadTagFilter, saveTagFilter,
  withTags, withWaiting, labelColor, addDays,
} from '../js/tags.js';
import { parseIssue, buildLabels, buildPushBody, formatDue } from '../js/parser.js';
import { setState, matchesFilters } from '../js/state.js';
import { triagePool } from '../js/triage.js';

if (!globalThis.crypto) globalThis.crypto = webcrypto;

test('tag names are normalised', () => {
  assert.equal(normaliseTag('  At Home '), 'at-home');
  assert.equal(normaliseTag('tag:Home'), 'home');
  assert.equal(normaliseTag('café & shop!'), 'caf-shop');
  assert.equal(normaliseTag('--a  b--'), 'a-b');
  assert.equal(normaliseTag('  '), null);
  assert.equal(normaliseTag('!!!'), null);
});

test('tags, waiting and follow-up come from labels', () => {
  assert.deepEqual(extractTags(['tag:work', 'list:tasks', 'Tag:Home', 'tag:home']), ['home', 'work']);
  assert.equal(extractFollowUp(['follow-up:2026-10-23']), '2026-10-23');
  assert.equal(extractFollowUp(['follow-up:soon']), null);
  assert.deepEqual(tagFields(['status:waiting', 'tag:home']),
    { tags: ['home'], waiting: true, waitingCleared: false, followUp: null });
  assert.deepEqual(tagFields([]), { tags: [], waiting: false, waitingCleared: false, followUp: null });
  assert.deepEqual(tagFields(['status:waiting-cleared', 'follow-up:2026-10-23']),
    { tags: [], waiting: false, waitingCleared: true, followUp: '2026-10-23' });
});

test('the Waiting on line is split from the notes and put back', () => {
  assert.deepEqual(splitWaitingLine('Waiting on: Sam\n\nCall about the boiler'), { waitingOn: 'Sam', rest: 'Call about the boiler' });
  assert.deepEqual(splitWaitingLine('waiting on:   the landlord  '), { waitingOn: 'the landlord', rest: '' });
  assert.deepEqual(splitWaitingLine('Notes\nWaiting on: Sam'), { waitingOn: null, rest: 'Notes\nWaiting on: Sam' });
  assert.deepEqual(splitWaitingLine(''), { waitingOn: null, rest: '' });
  assert.equal(withWaitingLine('Call about the boiler', 'Sam'), 'Waiting on: Sam\n\nCall about the boiler');
  assert.equal(withWaitingLine('', 'Sam'), 'Waiting on: Sam');
  assert.equal(withWaitingLine('Waiting on: Sam\n\nNotes', null), 'Notes');
  assert.equal(withWaitingLine('Waiting on: Sam\n\nNotes', 'Alex'), 'Waiting on: Alex\n\nNotes');
});

test('parseIssue and buildPushBody round-trip who it is waiting on', () => {
  const body = 'Waiting on: Sam\n\nAsk about keys\n\n## Checklist\n\n- [ ] one\n\n## Claude Resolution Notes\n\nnote';
  const t = parseIssue({ number: 1, title: 'x', body, labels: ['list:tasks', 'status:waiting', 'follow-up:2026-10-23', 'tag:home'] });
  assert.equal(t.body, 'Ask about keys');
  assert.equal(t.waitingOn, 'Sam');
  assert.equal(t.waiting, true);
  assert.equal(t.followUp, '2026-10-23');
  assert.deepEqual(t.tags, ['home']);
  assert.equal(buildPushBody(t.body, t.checklist, t.claudeNotes, t.waitingOn), body);
  assert.equal(buildPushBody('plain', [], null), 'plain');
});

test('buildLabels manages tags and waiting, and keeps action labels', () => {
  const task = parseIssue({ number: 2, title: 'x', body: '', labels: ['list:tasks', 'action:buy', 'tag:old', 'status:waiting', 'follow-up:2026-10-23', 'focus:week'] });
  const edited = buildLabels({ ...task, tags: ['home'], waiting: false, followUp: null });
  assert.ok(edited.includes('action:buy'));
  assert.ok(edited.includes('focus:week'));
  assert.ok(edited.includes('tag:home'));
  assert.ok(!edited.includes('tag:old'));
  assert.ok(!edited.includes('status:waiting'));
  assert.ok(!edited.some(l => l.startsWith('follow-up:')));
  assert.ok(!buildLabels({ labels: [], listName: 'tasks', actionType: 'manual' }).some(l => l.startsWith('action:')));
  // A task cached before these fields existed keeps its tags and waiting labels
  const old = { labels: ['list:tasks', 'tag:home', 'status:waiting', 'follow-up:2026-10-23'], listName: 'tasks' };
  assert.deepEqual(buildLabels(old).filter(l => /^(tag|status|follow-up):/.test(l)).sort(),
    ['follow-up:2026-10-23', 'status:waiting', 'tag:home']);
});

test('withTags and withWaiting rewrite only their own labels', () => {
  assert.deepEqual(withTags(['list:tasks', 'tag:a', 'focus:week'], ['Home', 'b', 'home']),
    ['list:tasks', 'focus:week', 'tag:b', 'tag:home']);
  assert.deepEqual(withWaiting(['list:tasks', 'status:waiting', 'follow-up:2026-01-01'], { waiting: true, followUp: '2026-10-23' }),
    ['list:tasks', 'status:waiting', 'follow-up:2026-10-23']);
  assert.deepEqual(withWaiting(['list:tasks', 'status:waiting', 'follow-up:2026-01-01', 'status:agent-assigned'], { waiting: false }),
    ['list:tasks', 'status:agent-assigned']);
  assert.deepEqual(withWaiting(['list:tasks'], { waiting: true, followUp: null }), ['list:tasks', 'status:waiting']);
});

test('waiting tasks grey out and go last until their follow-up date', () => {
  const today = '2026-10-16';
  const plain = { number: 1, state: 'OPEN' };
  const waiting = { number: 2, state: 'OPEN', waiting: true, followUp: '2026-10-20', waitingOn: 'Sam' };
  const due = { number: 3, state: 'OPEN', waiting: true, followUp: '2026-10-16', waitingOn: 'Sam' };
  const noDate = { number: 4, state: 'OPEN', waiting: true };
  const closed = { number: 5, state: 'CLOSED', waiting: true };
  assert.equal(waitingState(plain, today), 'none');
  assert.equal(waitingState(waiting, today), 'waiting');
  assert.equal(waitingState(due, today), 'follow-up');
  assert.equal(waitingState(noDate, today), 'waiting');
  assert.equal(waitingState(closed, today), 'none');
  assert.deepEqual(waitingLast([waiting, plain, noDate, due], today).map(t => t.number), [1, 3, 2, 4]);
  assert.equal(waitingText(waiting, today, formatDue), 'Waiting on Sam · follow up 20 Oct');
  assert.equal(waitingText(due, today, formatDue), 'Follow up with Sam');
  assert.equal(waitingText(noDate, today, formatDue), 'Waiting');
});

test('waiting-cleared: an agent thinks it came through', () => {
  const today = '2026-10-16';
  const plain = { number: 1, state: 'OPEN' };
  const waiting = { number: 2, state: 'OPEN', waiting: true, followUp: '2026-10-20' };
  const cleared = { number: 3, state: 'OPEN', waitingCleared: true, waitingOn: 'Sam', followUp: '2026-10-20' };
  const clearedNoWho = { number: 4, state: 'OPEN', waitingCleared: true };
  assert.equal(waitingState(cleared, today), 'cleared');
  assert.equal(waitingState({ ...cleared, state: 'CLOSED' }, today), 'none');
  // First in its list, ahead of everything; waiting still last
  assert.deepEqual(waitingLast([plain, waiting, cleared, clearedNoWho], today).map(t => t.number), [3, 4, 1, 2]);
  assert.equal(waitingText(cleared, today, formatDue), 'Came through? Sam');
  assert.equal(waitingText(clearedNoWho, today, formatDue), 'Came through?');
  // An unrelated edit keeps the agent's label, the follow-up and the body line
  const body = 'Waiting on: Sam\n\nNotes';
  const t = parseIssue({ number: 9, title: 'x', body, labels: ['list:tasks', 'status:waiting-cleared', 'follow-up:2026-10-20'] });
  assert.equal(t.waitingCleared, true);
  assert.equal(t.waitingOn, 'Sam');
  const kept = buildLabels({ ...t, importance: 'high' });
  assert.ok(kept.includes('status:waiting-cleared') && kept.includes('follow-up:2026-10-20'));
  assert.equal(buildPushBody(t.body, t.checklist, t.claudeNotes, t.waitingOn), body);
  // Still waiting: status:waiting back, cleared gone, follow-up kept
  const labels = ['list:tasks', 'status:waiting-cleared', 'follow-up:2026-10-20'];
  assert.deepEqual(withWaiting(labels, { waiting: true, followUp: '2026-10-20' }),
    ['list:tasks', 'status:waiting', 'follow-up:2026-10-20']);
  // OK: cleared and follow-up gone (the body line goes with waitingOn null)
  assert.deepEqual(withWaiting(labels, { waiting: false }), ['list:tasks']);
  assert.equal(labelColor('status:waiting-cleared'), '0e8a16');
});

test('the tag filter: only, hide, and cycling', () => {
  const home = { tags: ['home'] };
  const both = { tags: ['home', 'work'] };
  const none = { tags: [] };
  assert.equal(matchesTagFilter(none, {}), true);
  assert.equal(matchesTagFilter(none, { home: 'only' }), false);
  assert.equal(matchesTagFilter(home, { home: 'only' }), true);
  assert.equal(matchesTagFilter(home, { home: 'only', work: 'only' }), false);
  assert.equal(matchesTagFilter(both, { work: 'hide' }), false);
  assert.equal(matchesTagFilter(home, { work: 'hide' }), true);
  assert.equal(matchesTagFilter({}, { home: 'hide' }), true);
  assert.equal(nextTagMode(null), 'only');
  assert.equal(nextTagMode('only'), 'hide');
  assert.equal(nextTagMode('hide'), null);
  assert.deepEqual(withTagMode({ home: 'only' }, 'home', null), {});
  assert.deepEqual(filterTags([home, both], { garden: 'hide' }), ['garden', 'home', 'work']);
});

test('matchesFilters applies the tag filter', () => {
  setState({ issues: [] });
  const base = { section: null, importance: null, urgent: false, showCompleted: false };
  assert.equal(matchesFilters({ tags: ['home'] }, { ...base, tags: { home: 'hide' } }), false);
  assert.equal(matchesFilters({ tags: [] }, { ...base, tags: { home: 'hide' } }), true);
  assert.equal(matchesFilters({ tags: [] }, { ...base }), true);
});

test('the tag filter is remembered, and survives broken storage', () => {
  const store = new Map();
  const storage = { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  saveTagFilter({ home: 'hide', Work: 'only', x: 'bogus' }, storage);
  assert.deepEqual(loadTagFilter(storage), { home: 'hide', work: 'only' });
  const broken = { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('blocked'); } };
  assert.deepEqual(loadTagFilter(broken), {});
  assert.doesNotThrow(() => saveTagFilter({ home: 'only' }, broken));
  assert.deepEqual(loadTagFilter(undefined), {});
});

test('triage leaves out tasks still waiting', () => {
  const now = new Date(2026, 9, 16, 12).getTime();
  const tasks = [
    { number: 1, state: 'OPEN', importance: 'high' },
    { number: 2, state: 'OPEN', importance: 'high', waiting: true, followUp: '2026-10-20' },
    { number: 3, state: 'OPEN', importance: 'high', waiting: true, followUp: '2026-10-16' },
    { number: 4, state: 'OPEN', importance: 'high', waitingCleared: true, followUp: '2026-10-20' },
  ];
  assert.deepEqual(triagePool(tasks, { now }).map(c => c.task.number), [1, 3, 4]);
});

test('label colours and dates', () => {
  assert.equal(labelColor('tag:home'), '5319e7');
  assert.equal(labelColor('follow-up:2026-10-23'), 'fbca04');
  assert.equal(labelColor('status:waiting'), 'fbca04');
  assert.equal(labelColor('due:2026-10-23'), 'ededed');
  assert.equal(addDays(7, new Date(2026, 9, 28)), '2026-11-04');
  assert.equal(addDays(0, new Date(2026, 9, 16, 23, 59)), '2026-10-16');
});
