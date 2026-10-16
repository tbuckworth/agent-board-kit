// Reusable modal with focus trap

import { el, clear, icon } from '../render.js';

let activeModal = null;

export function showModal({ title, body, footer, onClose }) {
  closeModal(); // Close any existing modal

  const backdrop = el('div', { className: 'modal-backdrop' });
  const modal = el('div', {
    className: 'modal',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-label': title,
  },
    el('div', { className: 'modal-header' },
      el('h2', { className: 'modal-title' }, title),
      el('button', {
        className: 'btn btn-ghost btn-icon',
        'aria-label': 'Close',
        onClick: () => close(),
      }, icon('close', 18)),
    ),
    el('div', { className: 'modal-body' }, ...(Array.isArray(body) ? body : [body])),
    footer ? el('div', { className: 'modal-footer' }, ...(Array.isArray(footer) ? footer : [footer])) : null,
  );

  backdrop.appendChild(modal);
  document.body.appendChild(backdrop);

  // Animate in
  requestAnimationFrame(() => backdrop.classList.add('open'));

  function close() {
    document.removeEventListener('keydown', onKeydown);
    backdrop.classList.remove('open');
    setTimeout(() => {
      if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
      activeModal = null;
      if (onClose) onClose();
    }, 200);
  }

  // Close on backdrop click
  backdrop.addEventListener('click', (e) => {
    if (e.target === backdrop) close();
  });

  // Close on Escape
  function onKeydown(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
    }
  }
  document.addEventListener('keydown', onKeydown);

  // Focus trap
  const focusable = modal.querySelectorAll('button, input, textarea, select, [tabindex]:not([tabindex="-1"])');
  if (focusable.length > 0) focusable[0].focus();

  activeModal = { close, backdrop, modal };
  return { close };
}

export function closeModal() {
  if (activeModal) {
    activeModal.close();
    activeModal = null;
  }
}

// Confirm dialog helper
export function confirm({ title, message, confirmLabel = 'Confirm', danger = false }) {
  return new Promise((resolve) => {
    const body = el('p', {}, message);
    const footer = [
      el('button', {
        className: 'btn btn-secondary',
        onClick: () => { modal.close(); resolve(false); },
      }, 'Cancel'),
      el('button', {
        className: `btn ${danger ? 'btn-danger' : 'btn-primary'}`,
        onClick: () => { modal.close(); resolve(true); },
      }, confirmLabel),
    ];
    const modal = showModal({ title, body, footer, onClose: () => resolve(false) });
  });
}
