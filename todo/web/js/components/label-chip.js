// Label chip component

import { el } from '../render.js';

export function labelChip(text, type = '') {
  return el('span', { className: `label-chip ${type}` }, text);
}
