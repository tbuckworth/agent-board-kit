// Assign to agent dialog for Triage (the task page has its own). Resolves true once the request is made.

import { el } from '../render.js';
import { showModal } from './modal.js';
import { showToast } from './toast.js';
import { requestAgent } from '../actions.js';

export function showAssignDialog(task) {
  return new Promise((resolve) => {
    let done = false;
    const instructions = el('textarea', {
      className: 'input textarea',
      id: 'triage-assign-instructions',
      placeholder: 'What should the agent do? (optional)',
    });

    const assignBtn = el('button', {
      className: 'btn btn-primary',
      onClick: async () => {
        assignBtn.disabled = true;
        try {
          await requestAgent(task.number, instructions.value);
          done = true;
          showToast('Requested — the desktop starts the agent within a couple of minutes');
          modal.close();
        } catch (err) {
          showToast(`Error: ${err.message}`);
          assignBtn.disabled = false;
        }
      },
    }, 'Assign');

    const modal = showModal({
      title: 'Hand to an agent',
      body: [
        el('p', { className: 'form-hint', style: { marginTop: '0', marginBottom: 'var(--space-3)' } },
          'The desktop starts an agent on this task within a couple of minutes. This needs the network.'),
        el('div', { className: 'form-group' },
          el('label', { className: 'form-label', htmlFor: 'triage-assign-instructions' }, 'Instructions'),
          instructions,
        ),
      ],
      footer: [
        el('button', { className: 'btn btn-secondary', onClick: () => modal.close() }, 'Cancel'),
        assignBtn,
      ],
      onClose: () => resolve(done),
    });

    instructions.focus();
  });
}
