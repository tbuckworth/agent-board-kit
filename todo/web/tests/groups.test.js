import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildRows, groupCounts, withGroupMembers, groupRating, planGroup, loadCollapsed, saveCollapsed } from '../js/groups.js';

const t = (number, extra = {}) => ({ number, title: `t${number}`, state: 'OPEN', parentNumber: null, ...extra });

test('a group shows its tasks under it, indented, and folds', () => {
  const tasks = [t(1), t(2, { parentNumber: 1 }), t(3), t(4, { parentNumber: 1 })];
  const all = [...tasks, t(5, { parentNumber: 1, state: 'CLOSED' })];
  const rows = buildRows(tasks, all);
  assert.deepEqual(rows.map(r => [r.task.number, r.depth]), [[1, 0], [2, 1], [4, 1], [3, 0]]);
  assert.equal(rows[0].childCount, 3);
  assert.equal(rows[0].doneCount, 1);
  assert.equal(rows[0].expanded, true);
  const folded = buildRows(tasks, all, new Set([1]));
  assert.deepEqual(folded.map(r => r.task.number), [1, 3]);
  assert.equal(folded[0].expanded, false);
});

test('a task whose group is not shown stands on its own; loops are shown once', () => {
  const rows = buildRows([t(2, { parentNumber: 99 }), t(3)], []);
  assert.deepEqual(rows.map(r => [r.task.number, r.depth]), [[2, 0], [3, 0]]);
  const loop = buildRows([t(1, { parentNumber: 2 }), t(2, { parentNumber: 1 })], []);
  assert.deepEqual(loop.map(r => r.task.number), [1, 2]);   // a loop (GitHub forbids them) still shows both
  assert.deepEqual(buildRows([t(1, { parentNumber: 1 })], []).map(r => r.task.number), [1]);
});

test('groups take their open tasks along, nested ones too', () => {
  const all = [t(1), t(2, { parentNumber: 1 }), t(3, { parentNumber: 2 }), t(4, { parentNumber: 1, state: 'CLOSED' }), t(5)];
  assert.deepEqual(withGroupMembers([1, 5], all), [1, 5, 2, 3]);
  assert.deepEqual(withGroupMembers([5], all), [5]);
  assert.deepEqual(groupCounts(all).get(1), { total: 2, done: 1 });
});

test('a new group is rated from its tasks', () => {
  assert.deepEqual(groupRating([{ importance: 'low', urgency: 'low' }, { importance: 'high', urgency: 'low', due: '2026-11-02' },
    { importance: 'medium', urgency: 'high', due: '2026-10-20' }]), { importance: 'high', urgency: 'high', due: '2026-10-20' });
  assert.deepEqual(groupRating([{}, {}]), { importance: null, urgency: null, due: null });
});

test('grouping joins the one group among the tasks, otherwise makes a new one', () => {
  const all = [t(1), t(2, { parentNumber: 1 }), t(3), t(4), t(6), t(7, { parentNumber: 6 })];
  assert.deepEqual(planGroup([3, 1, 4], all), { join: 1, members: [3, 4] });
  assert.deepEqual(planGroup([3, 4], all), { join: null, members: [3, 4] });
  assert.deepEqual(planGroup([1, 6], all), { join: null, members: [1, 6] });   // two groups: a group of groups
});

test('folded groups are remembered, and storage failures are harmless', () => {
  const store = new Map();
  const storage = { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  saveCollapsed(new Set([3, 4]), storage);
  assert.deepEqual([...loadCollapsed(storage)], [3, 4]);
  const broken = { getItem: () => { throw new Error('no'); }, setItem: () => { throw new Error('no'); } };
  assert.deepEqual([...loadCollapsed(broken)], []);
  saveCollapsed(new Set([1]), broken);
});
