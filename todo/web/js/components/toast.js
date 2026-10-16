// Toast / snackbar with undo support

import { el } from '../render.js';

let container = null;
let toastTimeout = null;

function ensureContainer() {
  if (container) return container;
  container = el('div', {
    className: 'toast-container',
    role: 'status',
    'aria-live': 'polite',
    'aria-atomic': 'true',
  });
  document.body.appendChild(container);
  return container;
}

export function showToast(message, options = {}) {
  const { action, actionLabel = 'Undo', duration = 5000 } = options;
  const c = ensureContainer();

  // Remove existing toast
  if (c.firstChild) {
    c.firstChild.classList.add('leaving');
    setTimeout(() => {
      if (c.firstChild) c.removeChild(c.firstChild);
    }, 200);
  }
  clearTimeout(toastTimeout);

  const children = [message];
  if (action) {
    children.push(
      el('button', {
        className: 'toast-action',
        onClick: () => {
          action();
          dismiss();
        },
      }, actionLabel)
    );
  }

  const toast = el('div', { className: 'toast' }, ...children);
  // Wait a tick to allow the leaving animation to start
  setTimeout(() => c.appendChild(toast), 50);

  function dismiss() {
    toast.classList.add('leaving');
    clearTimeout(toastTimeout);
    setTimeout(() => {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 200);
  }

  toastTimeout = setTimeout(dismiss, duration);
  return dismiss;
}
