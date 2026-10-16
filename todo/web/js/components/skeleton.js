// Loading skeleton components

import { el } from '../render.js';

export function skeletonRow() {
  return el('div', { className: 'skeleton-row' },
    el('div', { className: 'skeleton skeleton-circle' }),
    el('div', { style: { flex: '1' } },
      el('div', { className: 'skeleton skeleton-text md' }),
      el('div', { className: 'skeleton skeleton-text sm' }),
    ),
  );
}

export function skeletonList(count = 5) {
  const rows = [];
  for (let i = 0; i < count; i++) {
    rows.push(skeletonRow());
  }
  return el('div', { className: 'task-list-container' }, ...rows);
}
