// Offline tests for Triage's selection rule, snoozes, This week and the archive.
// Run with: node --test web/tests/

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';

import {
  snoozeDays, nextSnooze, isSnoozed, snoozeHint, recencyFactor, ageFactor, triageWeight, triagePool,
  pickTriage, pruneShown, hasFocus, focusTasks, withFocus, withRating, firstStep, isArchived, needsStateReason,
  loadTriageStore, saveTriageStore, STORE_KEY, FOCUS_LABEL, UNRATED_WEIGHT,
} from '../js/triage.js';
import { parseIssue } from '../js/parser.js';

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const DAY = 86400000;
const NOW = Date.UTC(2026, 9, 12, 12, 0);
const iso = (ms) => new Date(ms).toISOString();

function task(number, extra = {}) {
  return {
    number, state: 'OPEN', title: `Task ${number}`, listName: 'tasks', labels: ['list:tasks'],
    importance: null, updatedAt: iso(NOW), ...extra,
  };
}

test('snoozes grow 1 day, 3 days, 1 week, 3 weeks, then stay at 3 weeks', () => {
  assert.deepEqual([0, 1, 2, 3, 4, 9].map(snoozeDays), [1, 3, 7, 21, 21, 21]);
  let entry;
  const untils = [];
  for (let i = 0; i < 5; i++) {
    entry = nextSnooze(entry, NOW);
    untils.push((entry.until - NOW) / DAY);
  }
  assert.deepEqual(untils, [1, 3, 7, 21, 21]);
  assert.equal(entry.count, 5);
});

test('a snoozed task is hidden until its snooze ends', () => {
  const entry = nextSnooze(undefined, NOW);
  assert.equal(isSnoozed(entry, NOW), true);
  assert.equal(isSnoozed(entry, NOW + DAY - 1), true);
  assert.equal(isSnoozed(entry, NOW + DAY), false);
  assert.equal(isSnoozed(undefined, NOW), false);

  const tasks = [task(1), task(2)];
  const pool = triagePool(tasks, { now: NOW, snoozes: { 1: entry }, isRated: () => true });
  assert.deepEqual(pool.map(c => c.task.number), [2]);
  const later = triagePool(tasks, { now: NOW + 2 * DAY, snoozes: { 1: entry }, isRated: () => true });
  assert.deepEqual(later.map(c => c.task.number), [1, 2]);
});

test('the hint appears after the fourth snooze', () => {
  assert.equal(snoozeHint({ count: 3 }), null);
  assert.equal(snoozeHint({ count: 4 }), 'Snoozed 4 times — archive?');
  assert.equal(snoozeHint(undefined), null);
});

test('weights: unrated first, then importance; recent showings and fresh tasks count less', () => {
  const fresh = iso(NOW);
  assert.equal(triageWeight({ importance: null, updatedAt: fresh }, NOW, undefined), UNRATED_WEIGHT);
  assert.equal(triageWeight({ importance: 'high', updatedAt: fresh }, NOW, undefined), 3);
  assert.equal(triageWeight({ importance: 'medium', updatedAt: fresh }, NOW, undefined), 2);
  assert.equal(triageWeight({ importance: 'low', updatedAt: fresh }, NOW, undefined), 1);

  assert.equal(recencyFactor(undefined, NOW), 1);
  assert.equal(recencyFactor(NOW - 2 * 3600000, NOW), 0.1);
  assert.equal(recencyFactor(NOW - 3 * DAY, NOW), 0.4);
  assert.equal(recencyFactor(NOW - 8 * DAY, NOW), 1);

  assert.equal(ageFactor(iso(NOW), NOW), 1);
  assert.equal(ageFactor(iso(NOW - 30 * DAY), NOW), 2);
  assert.equal(ageFactor(iso(NOW - 400 * DAY), NOW), 4); // capped at 90 days
  assert.equal(ageFactor(undefined, NOW), 1);
});

test('the pool: open tasks on GitHub in rated lists; work is allowed', () => {
  const tasks = [
    task(1),
    task(2, { state: 'CLOSED' }),
    task(3, { listName: 'books' }),
    task(null),
    task(5, { listName: 'work' }),
  ];
  const pool = triagePool(tasks, { now: NOW, isRated: t => t.listName !== 'books' });
  assert.deepEqual(pool.map(c => c.task.number), [1, 5]);
});

test('pickTriage is weighted, and avoids the current card unless it is the only one', () => {
  const tasks = [task(1, { importance: 'low' }), task(2, { importance: 'high' })]; // weights 1 and 3
  const opts = { now: NOW, isRated: () => true };
  assert.equal(pickTriage(tasks, opts, () => 0.0).number, 1);
  assert.equal(pickTriage(tasks, opts, () => 0.24).number, 1);
  assert.equal(pickTriage(tasks, opts, () => 0.26).number, 2);
  assert.equal(pickTriage(tasks, opts, () => 0.999).number, 2);

  assert.equal(pickTriage(tasks, { ...opts, exclude: 2 }, () => 0.99).number, 1);
  assert.equal(pickTriage([task(7)], { ...opts, exclude: 7 }, () => 0.5).number, 7);
  assert.equal(pickTriage([], opts), null);

  // Over many draws an unrated task comes up about twice as often as a high one
  const mixed = [task(1, { importance: 'high' }), task(2)];
  let seed = 1;
  const rand = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
  let unrated = 0;
  for (let i = 0; i < 3000; i++) if (pickTriage(mixed, opts, rand).number === 2) unrated++;
  assert.ok(unrated > 1800 && unrated < 2200, `unrated drawn ${unrated} of 3000`);
});

test('shown history older than 30 days is forgotten', () => {
  const pruned = pruneShown({ 1: NOW - 31 * DAY, 2: NOW - DAY }, NOW);
  assert.deepEqual(pruned, { 2: NOW - DAY });
});

test('This week is one focus:week label', () => {
  const a = task(1, { labels: ['list:tasks', 'focus:week'] });
  const b = task(2, { labels: ['list:tasks', 'Focus:Week'], state: 'CLOSED' });
  const c = task(3);
  assert.equal(hasFocus(a), true);
  assert.equal(hasFocus(c), false);
  assert.deepEqual(focusTasks([a, b, c]).map(t => t.number), [1]); // closed ones don't count
  assert.deepEqual(withFocus(['list:tasks'], true), ['list:tasks', FOCUS_LABEL]);
  assert.deepEqual(withFocus(['list:tasks', 'focus:week', 'focus:week'], true), ['list:tasks', FOCUS_LABEL]);
  assert.deepEqual(withFocus(['list:tasks', 'Focus:week'], false), ['list:tasks']);
});

test('withRating rewrites only the rating labels', () => {
  const labels = ['list:tasks', 'focus:week', 'Importance:low', 'urgency:high', 'due:2026-01-01',
    'priority:medium', 'status:agent-assigned'];
  assert.deepEqual(withRating(labels, { importance: 'high', urgency: 'low', due: null }),
    ['list:tasks', 'focus:week', 'status:agent-assigned', 'importance:high', 'urgency:low']);
  assert.deepEqual(withRating(['list:x'], { importance: null, urgency: null, due: '2026-10-20' }),
    ['list:x', 'due:2026-10-20']);
});

test('firstStep reads the agent\'s line from the Claude notes', () => {
  assert.equal(firstStep('Found it.\n\n**First step:** open https://gov.uk/form (~10 min)'),
    'open https://gov.uk/form (~10 min)');
  assert.equal(firstStep('- **First step**: call the GP'), 'call the GP');
  assert.equal(firstStep('No step here'), null);
  assert.equal(firstStep(null), null);
});

test('archived: closed as not planned and still in a list', () => {
  const raw = (stateReason, labels = ['list:tasks']) => parseIssue({
    number: 9, id: 'I_9', title: 'x', body: '', state: 'CLOSED', stateReason,
    labels: { nodes: labels.map(name => ({ name })) },
  });
  assert.equal(isArchived(raw('NOT_PLANNED')), true);
  assert.equal(isArchived(raw('COMPLETED')), false);
  assert.equal(isArchived(raw('NOT_PLANNED', ['source:app'])), false); // the apps' delete
  assert.equal(isArchived({ ...raw('NOT_PLANNED'), state: 'OPEN' }), false);

  assert.equal(needsStateReason([{ state: 'CLOSED' }]), true);
  assert.equal(needsStateReason([{ state: 'CLOSED', stateReason: null }, { state: 'OPEN' }]), false);
});

test('the triage store survives bad or missing storage', () => {
  const mem = new Map();
  const storage = { getItem: k => mem.get(k) ?? null, setItem: (k, v) => mem.set(k, v) };
  assert.deepEqual(loadTriageStore(storage), { snoozes: {}, shown: {} });
  saveTriageStore(storage, { snoozes: { 4: { count: 1, until: NOW } }, shown: { 4: NOW } });
  assert.deepEqual(loadTriageStore(storage).snoozes, { 4: { count: 1, until: NOW } });
  mem.set(STORE_KEY, '{not json');
  assert.deepEqual(loadTriageStore(storage), { snoozes: {}, shown: {} });
  const throwing = { getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('full'); } };
  assert.deepEqual(loadTriageStore(throwing), { snoozes: {}, shown: {} });
  saveTriageStore(throwing, { snoozes: {}, shown: {} }); // does not throw
  assert.deepEqual(loadTriageStore(null), { snoozes: {}, shown: {} });
});

test('the new views and actions load and their imports resolve', async () => {
  await import('../js/actions.js');
  await import('../js/views/triage.js');
  await import('../js/views/archived.js');
  await import('../js/components/focus-section.js');
});
