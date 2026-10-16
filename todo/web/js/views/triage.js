// Triage: one open task at a time, to archive, focus on this week, rate, snooze, finish,
// delete or hand to an agent. Which task comes next is decided in ../triage.js.

import { el, icon, mount, linkify } from '../render.js';
import { getState, getIssueByNumber, isTaskRated, subscribe } from '../state.js';
import { navigate, getCurrentRoute } from '../router.js';
import { labelChip } from '../components/label-chip.js';
import { ratingMarkers } from '../components/task-row.js';
import { ratingControls } from '../components/rating-controls.js';
import { showToast } from '../components/toast.js';
import { confirm, showModal } from '../components/modal.js';
import { showAssignDialog } from '../components/assign-dialog.js';
import { titleCase } from '../parser.js';
import {
  pickTriage, triagePool, nextSnooze, snoozeDays, snoozeHint, pruneShown, firstStep,
  hasFocus, focusTasks, FOCUS_CAP, loadTriageStore, saveTriageStore,
} from '../triage.js';
import * as actions from '../actions.js';

const SWIPE_DISTANCE = 80; // px
const SWIPE_CANCEL_VERTICAL = 30;

let keyHandler = null;
let unsubscribe = null;

function storage() {
  try { return window.localStorage; } catch { return null; }
}

function snoozeLabel(days) {
  if (days % 7 === 0) return days === 7 ? '1 week' : `${days / 7} weeks`;
  return days === 1 ? '1 day' : `${days} days`;
}

export function renderTriage(container) {
  const store = loadTriageStore(storage());
  let current = null;

  function onTriage() {
    return getCurrentRoute()?.view === 'triage';
  }

  function save() {
    saveTriageStore(storage(), store);
  }

  function options(exclude) {
    return { now: Date.now(), snoozes: store.snoozes, shown: store.shown, isRated: isTaskRated, exclude };
  }

  // Draw the next card, never the one just shown unless it is the only one left
  function next() {
    const task = pickTriage(getState().issues, options(current));
    current = task ? task.number : null;
    if (task) {
      const now = Date.now();
      store.shown = pruneShown({ ...store.shown, [task.number]: now }, now);
      save();
    }
    render();
  }

  function currentTask() {
    const task = current != null ? getIssueByNumber(current) : null;
    return task && task.state === 'OPEN' ? task : null;
  }

  function render() {
    if (!onTriage()) return;
    const task = currentTask();
    const poolSize = triagePool(getState().issues, options(null)).length;

    const header = el('div', { className: 'view-header' },
      el('h1', { className: 'view-title' }, 'Triage'),
      el('p', { className: 'view-subtitle' },
        `${poolSize} task${poolSize !== 1 ? 's' : ''} in rated lists · ${focusTasks(getState().issues).length}/${FOCUS_CAP} this week`),
    );

    if (!task) {
      mount(container, header,
        el('div', { className: 'empty-state' },
          el('div', { className: 'empty-state-icon' }, icon('checkCircle', 48)),
          el('div', { className: 'empty-state-title' }, 'Nothing to triage'),
          el('div', { className: 'empty-state-text' }, 'Every open task in a rated list is snoozed, or there are none.'),
        ),
      );
      return;
    }

    mount(container, header, card(task), actionBar(task), hints());
  }

  function card(task) {
    const chips = [labelChip(titleCase(task.listName), 'list')];
    chips.push(el('span', {
      className: `label-chip importance ${task.importance || 'unrated'}`,
      title: 'Importance',
    }, task.importance ? `${task.importance} importance` : 'unrated'));
    chips.push(...ratingMarkers(task));
    if (hasFocus(task)) chips.push(el('span', { className: 'label-chip focus' }, 'this week'));

    const step = firstStep(task.claudeNotes);
    const hint = snoozeHint(store.snoozes[task.number]);

    const checklist = task.checklist.length > 0
      ? el('ul', { className: 'triage-checklist' },
          ...task.checklist.map(item => el('li', { className: item.isChecked ? 'checked' : '' },
            el('span', { className: 'triage-check', 'aria-hidden': 'true' }, item.isChecked ? '☑' : '☐'),
            el('span', {}, linkify(item.text)),
          )))
      : null;

    const rating = ratingControls(task, (value, changed) => rate(task.number, changed), `triage-${task.number}`);

    const cardEl = el('article', { className: 'triage-card', 'aria-label': task.title },
      el('div', { className: 'triage-chips' }, ...chips),
      el('h2', { className: 'triage-title' },
        el('a', {
          href: `#/task/${task.number}`,
          className: 'triage-title-link',
          title: 'Open the task',
        }, task.title),
      ),
      hint ? el('p', { className: 'triage-hint' }, hint) : null,
      step ? el('p', { className: 'triage-first-step' }, el('strong', {}, 'First step: '), linkify(step)) : null,
      task.body ? el('div', { className: 'triage-notes' }, linkify(task.body)) : null,
      checklist,
      el('div', { className: 'triage-rating' }, rating.element),
      el('div', { className: 'triage-swipe-message', 'aria-hidden': 'true' }),
    );
    attachSwipe(cardEl, task);
    return cardEl;
  }

  function actionBar(task) {
    const button = (label, iconName, onClick, extra = '', title = label) => el('button', {
      className: `btn ${extra || 'btn-secondary'}`,
      title,
      onClick,
    }, iconName ? icon(iconName, 16) : null, label);

    const focusOn = hasFocus(task);
    return el('div', { className: 'triage-actions' },
      button('Archive', 'folder', () => archive(task), 'btn-secondary', 'Archive (a): close as not planned'),
      button(focusOn ? 'Not this week' : 'This week', 'check', () => toggleFocus(task), focusOn ? 'btn-secondary active' : 'btn-secondary', 'This week (w)'),
      button(`Snooze ${snoozeLabel(snoozeDays(store.snoozes[task.number]?.count))}`, 'moon', () => snooze(task), 'btn-secondary', 'Snooze (s)'),
      button('Done', 'checkCircle', () => done(task), 'btn-secondary', 'Done (d)'),
      button('Agent', 'sparkles', () => handToAgent(task), 'btn-secondary', 'Hand to an agent (g)'),
      button('Delete', 'trash', () => remove(task), 'btn-danger', 'Delete (x)'),
      button('Next', 'chevronRight', () => next(), 'btn-primary', 'Keep it and show another (→)'),
    );
  }

  function hints() {
    return el('p', { className: 'triage-keys form-hint' },
      'Swipe right to keep and move on, left to archive. Keys: a archive · w this week · s snooze · d done · x delete · h/m/l importance · u urgent · g agent · → next');
  }

  // Pointer swipe: at least 80px across, cancelled by more than 30px up or down
  function attachSwipe(cardEl, task) {
    let start = null;
    const message = cardEl.querySelector('.triage-swipe-message');
    const reset = () => {
      start = null;
      cardEl.style.transform = '';
      message.textContent = '';
    };
    cardEl.addEventListener('pointerdown', (e) => {
      if (e.isPrimary && e.pointerType !== 'mouse') start = { x: e.clientX, y: e.clientY };
    });
    cardEl.addEventListener('pointercancel', reset);
    cardEl.addEventListener('pointermove', (e) => {
      if (!start) return;
      const x = e.clientX - start.x;
      const y = e.clientY - start.y;
      if (Math.abs(y) > SWIPE_CANCEL_VERTICAL && Math.abs(y) > Math.abs(x)) { reset(); return; }
      cardEl.style.transform = `translateX(${x}px) rotate(${x / 40}deg)`;
      message.textContent = Math.abs(x) > 45 ? (x > 0 ? 'Keep · next' : 'Archive') : '';
    });
    cardEl.addEventListener('pointerup', (e) => {
      if (!start) return;
      const x = e.clientX - start.x;
      const y = e.clientY - start.y;
      reset();
      if (Math.abs(x) < SWIPE_DISTANCE || Math.abs(x) < 1.4 * Math.abs(y)) return;
      if (x > 0) next();
      else archive(task);
    });
  }

  // --- Actions ---

  function failed(err) {
    showToast(`Error: ${err.message}`);
    render();
  }

  function archive(task) {
    actions.archiveTask(task.number).catch(failed);
    showToast('Archived', {
      action: () => actions.reopenTask(task.number)
        .then(() => showToast('Back in its list'))
        .catch(failed),
    });
    next();
  }

  function done(task) {
    actions.completeTask(task.number).catch(failed);
    showToast('Done', {
      action: () => actions.reopenTask(task.number).catch(failed),
    });
    next();
  }

  function snooze(task) {
    const entry = nextSnooze(store.snoozes[task.number], Date.now());
    store.snoozes = { ...store.snoozes, [task.number]: entry };
    save();
    showToast(`Snoozed for ${snoozeLabel(snoozeDays(entry.count - 1))}`);
    next();
  }

  async function remove(task) {
    const ok = await confirm({
      title: 'Delete this task?',
      message: 'This will close the issue and remove it from your lists. This cannot be undone.',
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    try {
      await actions.deleteTask(task.number);
      showToast('Task deleted');
      next();
    } catch (err) {
      failed(err);
    }
  }

  async function handToAgent(task) {
    if (task.labels.includes('status:agent-requested')) {
      showToast('An agent is already requested for this task');
      return;
    }
    if (await showAssignDialog(task)) next();
  }

  function rate(number, changed) {
    const task = getIssueByNumber(number);
    if (task && Object.entries(changed).every(([k, v]) => task[k] === v)) return;
    actions.saveRating(number, changed).then(() => render()).catch(failed);
  }

  function toggleFocus(task) {
    if (hasFocus(task)) {
      actions.setFocus(task.number, false)
        .then(() => { showToast('Taken off this week'); render(); })
        .catch(failed);
      return;
    }
    const focused = focusTasks(getState().issues);
    if (focused.length >= FOCUS_CAP) {
      showReplaceDialog(task, focused);
      return;
    }
    // Stay on this card: moving on silently made the next key or tap act on a task not yet seen
    actions.setFocus(task.number, true)
      .then(() => { showToast(`This week: ${focused.length + 1}/${FOCUS_CAP}`); render(); })
      .catch(failed);
  }

  // This week is full: say so, and offer to swap one out
  function showReplaceDialog(task, focused) {
    const rows = focused.map(f => el('div', { className: 'focus-replace-row' },
      el('span', { className: 'focus-replace-title' }, f.title),
      el('button', {
        className: 'btn btn-secondary btn-sm',
        onClick: async () => {
          modal.close();
          try {
            await actions.setFocus(f.number, false);
            await actions.setFocus(task.number, true);
            showToast(`Swapped into this week: ${task.title}`);
            render();
          } catch (err) {
            failed(err);
          }
        },
      }, 'Replace'),
    ));
    const modal = showModal({
      title: `This week already has ${FOCUS_CAP}`,
      body: [
        el('p', { className: 'form-hint', style: { marginTop: '0' } }, `Replace one with "${task.title}"?`),
        ...rows,
      ],
      footer: [el('button', { className: 'btn btn-secondary', onClick: () => modal.close() }, 'Keep them')],
    });
  }

  // --- Keyboard ---

  if (keyHandler) document.removeEventListener('keydown', keyHandler);
  keyHandler = (e) => {
    if (!onTriage()) {
      document.removeEventListener('keydown', keyHandler);
      keyHandler = null;
      return;
    }
    const tag = e.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (document.querySelector('.modal-backdrop')) return;
    const task = currentTask();
    if (!task) return;
    const handlers = {
      a: () => archive(task),
      w: () => toggleFocus(task),
      s: () => snooze(task),
      d: () => done(task),
      x: () => remove(task),
      g: () => handToAgent(task),
      h: () => rate(task.number, { importance: 'high' }),
      m: () => rate(task.number, { importance: 'medium' }),
      l: () => rate(task.number, { importance: 'low' }),
      u: () => rate(task.number, { urgency: task.urgency === 'high' ? 'low' : 'high' }),
      ArrowRight: () => next(),
    };
    const handler = handlers[e.key];
    if (handler) {
      e.preventDefault();
      handler();
    }
  };
  document.addEventListener('keydown', keyHandler);

  // A first sync can bring the tasks after the page opened empty
  if (unsubscribe) unsubscribe();
  unsubscribe = subscribe((state, prev) => {
    if (!onTriage()) {
      unsubscribe();
      unsubscribe = null;
      return;
    }
    if (state.syncState === 'success' && prev.syncState !== 'success' && !currentTask()) next();
  });

  next();
}
