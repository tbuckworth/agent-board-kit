// A list's open tasks with selection, groups and bulk actions.
//
// - Click opens a task. Shift-click selects a range, Cmd/Ctrl-click adds or removes one, and pressing on a task and
//   dragging up or down sweeps a selection. Cmd/Ctrl-A selects all, Esc clears.
// - Dragging a task by its grip onto another task groups the two (or the whole selection, if the dragged task is
//   in it); dropping onto a group adds to that group.
// - Groups show their tasks underneath, indented, and fold with the chevron.
// - The bar at the bottom acts on the selection. Every action can be undone from its toast or with Cmd/Ctrl-Z.

import { el, icon, clear } from '../render.js';
import { getState, getAllLists, getAllTags, groupForList } from '../state.js';
import { taskRow } from './task-row.js';
import { showToast } from './toast.js';
import { showModal, confirm } from './modal.js';
import { ratingControls } from './rating-controls.js';
import { isRatedList } from '../parser.js';
import { normaliseTag, addDays } from '../tags.js';
import { buildRows, loadCollapsed, saveCollapsed, groupCounts } from '../groups.js';
import * as bulk from '../bulk.js';

const DRAG_THRESHOLD = 6;

// Survives re-renders of the same list
let selection = { list: null, numbers: new Set(), anchor: null };
let keyHandler = null;

export function selectableTaskList({ tasks, listName, onOpen, onToggle, rerender }) {
  if (selection.list !== listName) selection = { list: listName, numbers: new Set(), anchor: null };
  const shownNumbers = new Set(tasks.map(t => t.number));
  for (const n of [...selection.numbers]) if (!shownNumbers.has(n)) selection.numbers.delete(n);

  const collapsed = loadCollapsed();
  const rows = buildRows(tasks, getState().issues, collapsed);
  const order = rows.map(r => r.task.number);

  const container = el('div', { className: 'task-list-container select-list' });
  const bar = el('div', { className: 'selection-bar', role: 'toolbar', 'aria-label': 'Selected tasks' });
  const wrapper = el('div', {}, container, bar);

  // ---------------------------------------------------------- rendering

  function paint() {
    clear(container);
    if (rows.length === 0) {
      container.appendChild(el('div', { className: 'empty-state', style: { padding: '2rem 1rem' } },
        el('div', { className: 'empty-state-title' }, 'No tasks'),
        el('div', { className: 'empty-state-text' }, 'Add a task to get started.'),
      ));
    }
    for (const r of rows) {
      container.appendChild(taskRow(r.task, {
        row: r,
        grip: true,
        selected: selection.numbers.has(r.task.number),
        onToggle: (task) => (r.childCount > 0 && task.state === 'OPEN') ? run(() => bulk.completeTasks([task.number]),
          `Group completed with its tasks`) : onToggle(task),
        onClick: (task, e) => clickRow(task, e),
        onFold: (task) => {
          if (collapsed.has(task.number)) collapsed.delete(task.number); else collapsed.add(task.number);
          saveCollapsed(collapsed);
          rerender();
        },
      }));
    }
    paintBar();
  }

  function refreshSelectionMarks() {
    for (const rowEl of container.querySelectorAll('.task-row')) {
      const on = selection.numbers.has(Number(rowEl.dataset.number));
      rowEl.classList.toggle('selected', on);
      rowEl.setAttribute('aria-selected', on ? 'true' : 'false');
    }
    paintBar();
  }

  function paintBar() {
    clear(bar);
    const n = selection.numbers.size;
    bar.classList.toggle('open', n > 0);
    if (n === 0) return;
    const nums = selectedInOrder();
    const counts = groupCounts(getState().issues);
    const hasGroup = nums.some(x => (counts.get(x)?.total || 0) > 0);
    const hasGrouped = nums.some(x => getState().issues.find(i => i.number === x)?.parentNumber != null);
    const btn = (label, iconName, onClick, title = label) =>
      el('button', { className: 'btn btn-ghost btn-sm', title, onClick }, iconName ? icon(iconName, 16) : null, label);
    const items = [
      el('span', { className: 'selection-count' }, `${n} selected`),
      btn('Complete', 'check', () => run(() => bulk.completeTasks(nums), `${nums.length} completed`)),
      btn('Move', 'folder', () => askMove(nums)),
      btn('Rate', null, () => askRate(nums)),
      btn('Tag', 'tag', () => askTag(nums)),
      btn('Waiting', 'clock', () => askWaiting(nums), 'Waiting on someone'),
      btn('Agent', 'sparkles', () => askAgent(nums), 'Assign to one agent'),
      n > 1 ? btn('Group', 'list', () => askGroup(nums)) : null,
      hasGroup ? btn('Ungroup', null, () => doUngroup(nums)) : null,
      hasGrouped ? btn('Take out', null, () => run(() => bulk.removeFromGroup(nums), 'Taken out of the group'),
        'Take out of its group') : null,
      btn('Delete', 'trash', () => askDelete(nums)),
      el('button', {
        className: 'btn btn-ghost btn-icon btn-sm', title: 'Clear selection (Esc)', 'aria-label': 'Clear selection',
        onClick: () => { selection.numbers.clear(); refreshSelectionMarks(); },
      }, icon('close', 16)),
    ];
    bar.append(...items.filter(Boolean));
  }

  const selectedInOrder = () => order.filter(x => selection.numbers.has(x));

  // ---------------------------------------------------------- clicking and sweeping

  let suppressClick = false;

  function clickRow(task, e) {
    if (suppressClick) { suppressClick = false; return; }
    const n = task.number;
    if (e?.shiftKey && selection.anchor != null && order.includes(selection.anchor)) {
      const [a, b] = [order.indexOf(selection.anchor), order.indexOf(n)].sort((x, y) => x - y);
      for (const x of order.slice(a, b + 1)) selection.numbers.add(x);
      refreshSelectionMarks();
      return;
    }
    if (e?.shiftKey || e?.metaKey || e?.ctrlKey) {
      if (selection.numbers.has(n)) selection.numbers.delete(n); else selection.numbers.add(n);
      selection.anchor = n;
      refreshSelectionMarks();
      return;
    }
    onOpen(task);
  }

  function rowAt(y) {
    for (const rowEl of container.querySelectorAll('.task-row')) {
      const r = rowEl.getBoundingClientRect();
      if (y >= r.top && y <= r.bottom) return rowEl;
    }
    return null;
  }

  container.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    const rowEl = e.target.closest('.task-row');
    if (!rowEl || e.target.closest('.task-checkbox, .group-fold')) return;
    if (e.target.closest('.task-grip')) startDrag(e, rowEl);
    else startSweep(e, rowEl);
  });

  // Press on a task and move up or down: every task between is selected
  function startSweep(e, startRow) {
    const startY = e.clientY;
    const startIndex = order.indexOf(Number(startRow.dataset.number));
    const base = (e.metaKey || e.ctrlKey || e.shiftKey) ? new Set(selection.numbers) : new Set();
    let sweeping = false;
    function move(ev) {
      if (!sweeping && Math.abs(ev.clientY - startY) < DRAG_THRESHOLD) return;
      if (!sweeping) {
        sweeping = true;
        container.classList.add('sweeping');
      }
      ev.preventDefault();
      const over = rowAt(ev.clientY);
      const rowsEls = [...container.querySelectorAll('.task-row')];
      let endIndex = over ? order.indexOf(Number(over.dataset.number))
        : (ev.clientY < container.getBoundingClientRect().top ? 0 : rowsEls.length - 1);
      const [a, b] = [startIndex, endIndex].sort((x, y) => x - y);
      selection.numbers = new Set([...base, ...order.slice(a, b + 1)]);
      selection.anchor = order[startIndex];
      refreshSelectionMarks();
    }
    function up() {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      container.classList.remove('sweeping');
      if (sweeping) suppressClick = true;
      setTimeout(() => { suppressClick = false; }, 0);
    }
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
  }

  // Drag a task by its grip onto another one to group them
  function startDrag(e, dragRow) {
    e.preventDefault();
    const dragged = Number(dragRow.dataset.number);
    const moving = selection.numbers.has(dragged) ? selectedInOrder() : [dragged];
    let ghost = null;
    let target = null;
    function move(ev) {
      if (!ghost) {
        ghost = el('div', { className: 'drag-ghost' },
          moving.length > 1 ? `${moving.length} tasks` : dragRow.querySelector('.task-title')?.textContent || '');
        document.body.appendChild(ghost);
        dragRow.classList.add('dragging');
      }
      ghost.style.left = `${ev.clientX + 12}px`;
      ghost.style.top = `${ev.clientY + 8}px`;
      const over = rowAt(ev.clientY);
      const n = over ? Number(over.dataset.number) : null;
      const next = (n != null && !moving.includes(n)) ? over : null;
      if (next !== target) {
        target?.classList.remove('drop-target');
        target = next;
        target?.classList.add('drop-target');
      }
    }
    function up() {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      ghost?.remove();
      dragRow.classList.remove('dragging');
      if (!target) return;
      target.classList.remove('drop-target');
      suppressClick = true;
      setTimeout(() => { suppressClick = false; }, 0);
      const onto = Number(target.dataset.number);
      const isGroup = (groupCounts(getState().issues).get(onto)?.total || 0) > 0;
      if (isGroup) {
        run(() => bulk.groupTasks([onto, ...moving], ''), `Added to the group`, true);
      } else {
        askGroup([onto, ...moving]);
      }
    }
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
  }

  // ---------------------------------------------------------- actions

  // Runs an action that returns an undo function (or { undo }), then offers Undo
  async function run(action, message, keepSelection = false) {
    showToast('Working…', { duration: 60000 });
    try {
      const result = await action();
      const undo = typeof result === 'function' ? result : result?.undo;
      if (undo) bulk.pushUndo(message, undo);
      if (!keepSelection) selection.numbers.clear();
      rerender();
      showToast(message, undo ? { action: () => undoNow(), actionLabel: 'Undo' } : {});
    } catch (err) {
      rerender();
      showToast(`Error: ${err.message}`);
    }
  }

  async function undoNow() {
    showToast('Undoing…', { duration: 60000 });
    try {
      const label = await bulk.undoLast();
      rerender();
      showToast(label ? `Undone: ${label}` : 'Nothing to undo');
    } catch (err) {
      rerender();
      showToast(`Undo failed: ${err.message}`);
    }
  }

  function modal(title, body, confirmLabel, onConfirm) {
    const go = el('button', { className: 'btn btn-primary' }, confirmLabel);
    const m = showModal({
      title, body,
      footer: [el('button', { className: 'btn btn-secondary', onClick: () => m.close() }, 'Cancel'), go],
    });
    go.addEventListener('click', () => { if (onConfirm() !== false) m.close(); });
    return m;
  }

  function askGroup(nums) {
    // One group among them: the others join it, no name needed
    const counts = groupCounts(getState().issues);
    if (nums.filter(x => (counts.get(x)?.total || 0) > 0).length === 1) {
      run(() => bulk.groupTasks(nums, ''), 'Added to the group');
      return;
    }
    const first = getState().issues.find(i => i.number === nums[0]);
    const input = el('input', { className: 'input', type: 'text', value: first?.title || '', placeholder: 'Group name' });
    const submit = () => {
      const name = input.value.trim();
      if (!name) { input.focus(); return false; }
      run(() => bulk.groupTasks(nums, name), `Grouped ${nums.length} tasks`);
      return true;
    };
    const m = modal(`Group ${nums.length} tasks`, [
      el('p', { className: 'form-hint' }, 'They stay in this list, under one task you can fold away. Ticking the group ticks them all.'),
      input,
    ], 'Group', submit);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); if (submit()) m.close(); } });
    input.select();
  }

  function askMove(nums) {
    const lists = getAllLists();
    const input = el('input', { className: 'input', type: 'text', placeholder: 'List name', list: 'move-lists-datalist' });
    const datalist = el('datalist', { id: 'move-lists-datalist' }, ...lists.map(l => el('option', { value: l })));
    const submit = () => {
      const target = input.value.trim().toLowerCase().replace(/\s+/g, '-');
      if (!target) { input.focus(); return false; }
      run(() => bulk.moveTasks(nums, target), `Moved to ${target}`);
      return true;
    };
    const m = modal(`Move ${nums.length} task${nums.length > 1 ? 's' : ''}`, [input, datalist], 'Move', submit);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); if (submit()) m.close(); } });
    input.focus();
  }

  function askTag(nums) {
    const input = el('input', { className: 'input', type: 'text', placeholder: 'Tag, e.g. home', list: 'bulk-tags-datalist' });
    const datalist = el('datalist', { id: 'bulk-tags-datalist' }, ...getAllTags().map(t => el('option', { value: t })));
    const apply = (add) => {
      const tag = normaliseTag(input.value);
      if (!tag) { input.focus(); return false; }
      run(() => bulk.tagTasks(nums, tag, add), add ? `Tagged #${tag}` : `Removed #${tag}`);
      return true;
    };
    const remove = el('button', { className: 'btn btn-secondary' }, 'Remove tag');
    const m = modal(`Tag ${nums.length} task${nums.length > 1 ? 's' : ''}`, [input, datalist, remove], 'Add tag', () => apply(true));
    remove.addEventListener('click', () => { if (apply(false)) m.close(); });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); if (apply(true)) m.close(); } });
    input.focus();
  }

  function askWaiting(nums) {
    const tasks = nums.map(x => getState().issues.find(i => i.number === x)).filter(Boolean);
    const who = el('input', { className: 'input', type: 'text', placeholder: 'Who? (optional)' });
    let followUp = addDays(7);
    const choices = el('div', { className: 'filter-bar follow-up-choices' });
    const paintChoices = () => {
      clear(choices);
      for (const [label, days] of [['3 days', 3], ['1 week', 7], ['2 weeks', 14], ['None', null]]) {
        const date = days == null ? null : addDays(days);
        choices.appendChild(el('button', {
          className: `filter-chip ${followUp === date ? 'active' : ''}`,
          onClick: () => { followUp = date; paintChoices(); },
        }, label));
      }
    };
    paintChoices();
    const stop = tasks.length && tasks.every(t => t.waiting)
      ? el('button', { className: 'btn btn-secondary' }, 'No longer waiting') : null;
    const m = modal(`Waiting: ${nums.length} task${nums.length > 1 ? 's' : ''}`, [
      el('div', { className: 'form-group' }, who),
      el('div', { className: 'form-label' }, 'Follow up'),
      choices,
      stop,
    ], 'Mark waiting', () => {
      run(() => bulk.waitTasks(nums, { waiting: true, waitingOn: who.value.trim() || null, followUp }),
        `${nums.length} waiting`);
      return true;
    });
    stop?.addEventListener('click', () => {
      run(() => bulk.waitTasks(nums, { waiting: false }), 'No longer waiting');
      m.close();
    });
    who.focus();
  }

  function askRate(nums) {
    let changed = {};
    const controls = ratingControls({}, (_value, delta) => { changed = { ...changed, ...delta }; }, 'bulk-rate');
    const rated = isRatedList(listName, groupForList(listName));
    modal(`Rate ${nums.length} task${nums.length > 1 ? 's' : ''}`, [
      rated ? null : el('p', { className: 'form-hint' }, 'This list is not normally rated.'),
      el('p', { className: 'form-hint' }, 'Only what you change here is applied. A group gets the rating itself; its tasks keep theirs.'),
      controls.element,
    ], 'Apply', () => {
      if (Object.keys(changed).length === 0) return true;
      run(() => bulk.rateTasks(nums, changed), 'Rating changed');
      return true;
    });
  }

  function askAgent(nums) {
    const textarea = el('textarea', { className: 'input textarea', placeholder: 'Instructions (optional)' });
    modal(nums.length > 1 ? `One agent for ${nums.length} tasks` : 'Assign to agent', [
      nums.length > 1 ? el('p', { className: 'form-hint' }, 'One agent gets them all as one job. The desktop picks it up within 2 minutes.') : null,
      textarea,
    ], 'Assign', () => {
      run(() => bulk.assignTasks(nums, textarea.value), 'Assigned to an agent');
      return true;
    });
    textarea.focus();
  }

  async function askDelete(nums) {
    const counts = groupCounts(getState().issues);
    const groupTasks = nums.reduce((s, x) => s + (counts.get(x)?.total || 0), 0);
    const ok = await confirm({
      title: `Delete ${nums.length} task${nums.length > 1 ? 's' : ''}?`,
      message: groupTasks ? `Groups are deleted with their tasks. You can undo this.` : 'You can undo this.',
      confirmLabel: 'Delete',
      danger: true,
    });
    if (ok) run(() => bulk.deleteTasks(nums), `${nums.length} deleted`);
  }

  function doUngroup(nums) {
    const counts = groupCounts(getState().issues);
    const groups = nums.filter(x => (counts.get(x)?.total || 0) > 0);
    run(async () => {
      const undos = [];
      for (const g of groups) undos.push(await bulk.ungroup(g));
      return async () => { for (const u of undos.reverse()) await u(); };
    }, groups.length > 1 ? `${groups.length} groups ungrouped` : 'Ungrouped');
  }

  // ---------------------------------------------------------- keys

  if (keyHandler) document.removeEventListener('keydown', keyHandler);
  keyHandler = (e) => {
    if (!container.isConnected) {
      document.removeEventListener('keydown', keyHandler);
      keyHandler = null;
      return;
    }
    const typing = e.target.closest?.('input, textarea, select, [contenteditable="true"]');
    if (typing || document.querySelector('.modal-backdrop')) return;
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === 'z' && !e.shiftKey) {
      e.preventDefault();
      undoNow();
    } else if (mod && e.key.toLowerCase() === 'a') {
      e.preventDefault();
      selection.numbers = new Set(order);
      refreshSelectionMarks();
    } else if (e.key === 'Escape' && selection.numbers.size) {
      selection.numbers.clear();
      refreshSelectionMarks();
    } else if ((e.key === 'Delete' || e.key === 'Backspace') && selection.numbers.size) {
      e.preventDefault();
      askDelete(selectedInOrder());
    }
  };
  document.addEventListener('keydown', keyHandler);

  paint();
  return wrapper;
}
