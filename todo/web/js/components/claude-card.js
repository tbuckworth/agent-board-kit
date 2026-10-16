// Claude Resolution Notes card

import { el, icon, linkify } from '../render.js';

export function claudeCard(notes) {
  if (!notes) return null;
  return el('div', { className: 'claude-card' },
    el('div', { className: 'claude-card-header' },
      icon('sparkles', 16),
      'Claude Resolution Notes',
    ),
    el('div', { className: 'claude-card-body' }, linkify(notes)),
  );
}
