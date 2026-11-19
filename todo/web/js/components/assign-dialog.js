// Assign to agent dialog for the task page and Triage. Resolves true once the request is made.
// `again` words it for a task an agent already has; `idPrefix` keeps its element ids unique per page.

import { el } from '../render.js';
import { showModal } from './modal.js';
import { showToast } from './toast.js';
import { requestAgent } from '../actions.js';
import { agentPicker } from './agent-picker.js';

export function requestedToast(target) {
  return target
    ? `Requested. The desktop sends it to ${target.name} within a couple of minutes`
    : 'Requested. The desktop starts the agent within a couple of minutes';
}

export function showAssignDialog(task, { again, idPrefix }) {
  return new Promise((resolve) => {
    let done = false;
    const instructions = el('textarea', {
      className: 'input textarea',
      id: `${idPrefix}-instructions`,
      placeholder: 'What should the agent do? (optional)',
    });
    const hint = el('p', { className: 'form-hint', style: { marginTop: '0', marginBottom: 'var(--space-3)' } });
    const picker = agentPicker(idPrefix, () => { hint.textContent = picker.describe(); });
    hint.textContent = picker.describe();

    const assignBtn = el('button', {
      className: 'btn btn-primary',
      onClick: async () => {
        const problem = picker.check();
        if (problem) { showToast(problem); return; }
        assignBtn.disabled = true;
        const target = picker.target();
        try {
          await requestAgent(task.number, instructions.value, target?.id || null);
          done = true;
          showToast(requestedToast(target));
          modal.close();
        } catch (err) {
          showToast(`Error: ${err.message}`);
          assignBtn.disabled = false;
        }
      },
    }, again ? 'Assign again' : 'Assign');

    const modal = showModal({
      title: again ? 'Assign again' : 'Hand to an agent',
      body: [
        picker.element,
        hint,
        el('div', { className: 'form-group' },
          el('label', { className: 'form-label', htmlFor: `${idPrefix}-instructions` }, 'Instructions'),
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
