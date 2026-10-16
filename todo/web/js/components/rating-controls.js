// Importance chips, an Urgent toggle and an optional due date

import { el } from '../render.js';
import { titleCase } from '../parser.js';

const IMPORTANCES = ['high', 'medium', 'low'];
const DUE_COMMIT_DELAY = 1000;

// A typed date passes through values like 0002-10-12 and 2026-01-12 on its way
// to 2026-11-12, so only plausible years count, and typing is debounced.
function plausibleDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const year = Number(value.slice(0, 4));
  return year >= 2000 && year < 2200;
}

// value: { importance, urgency, due }. onChange(value, changed) gets the whole value and the changed field.
// The controls update their own look, so a caller need not re-render.
export function ratingControls(value, onChange, idPrefix = 'rating') {
  const current = {
    importance: value.importance || null,
    urgency: value.urgency || null,
    due: value.due || null,
  };

  function set(changed) {
    Object.assign(current, changed);
    sync();
    onChange({ ...current }, changed);
  }

  const importanceChips = IMPORTANCES.map(level => el('button', {
    type: 'button',
    className: `filter-chip importance-chip ${level}`,
    dataset: { level },
    onClick: () => { if (current.importance !== level) set({ importance: level }); },
  }, titleCase(level)));

  const urgentInput = el('input', {
    type: 'checkbox',
    id: `${idPrefix}-urgent`,
    onChange: (e) => set({ urgency: e.target.checked ? 'high' : 'low' }),
  });

  let dueTimer = null;
  function commitDue() {
    clearTimeout(dueTimer);
    const next = dueInput.value || null;
    if (next && !plausibleDate(next)) return;
    if (next !== current.due) set({ due: next });
  }

  const dueInput = el('input', {
    className: 'input rating-due-input',
    type: 'date',
    id: `${idPrefix}-due`,
    onChange: () => {
      clearTimeout(dueTimer);
      dueTimer = setTimeout(commitDue, DUE_COMMIT_DELAY);
    },
    onBlur: commitDue,
  });

  const clearDueBtn = el('button', {
    type: 'button',
    className: 'btn btn-ghost btn-sm',
    onClick: () => { dueInput.value = ''; commitDue(); },
  }, 'Clear');

  function sync() {
    for (const chip of importanceChips) {
      const active = chip.dataset.level === current.importance;
      chip.classList.toggle('active', active);
      chip.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
    urgentInput.checked = current.urgency === 'high';
    if (document.activeElement !== dueInput) dueInput.value = current.due || '';
    clearDueBtn.hidden = !current.due;
  }

  const element = el('div', { className: 'rating-controls' },
    el('div', { className: 'rating-row' },
      el('span', { className: 'rating-label' }, 'Importance'),
      el('div', { className: 'rating-chips', role: 'group', 'aria-label': 'Importance' }, ...importanceChips),
    ),
    el('div', { className: 'rating-row' },
      el('label', { className: 'rating-label', htmlFor: `${idPrefix}-urgent` }, 'Urgent'),
      el('label', { className: 'toggle' },
        urgentInput,
        el('span', { className: 'toggle-track' }),
      ),
    ),
    el('div', { className: 'rating-row' },
      el('label', { className: 'rating-label', htmlFor: `${idPrefix}-due` }, 'Due'),
      dueInput,
      clearDueBtn,
    ),
  );

  sync();

  // getValue() reads a typed but uncommitted date too, for forms that submit on click
  return {
    element,
    getValue: () => {
      commitDue();
      return { ...current };
    },
  };
}
