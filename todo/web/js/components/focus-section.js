// "This week": the open focus:week tasks (at most five), shown at the top of the home page and the Tasks list

import { el, icon } from '../render.js';
import { getState } from '../state.js';
import { navigate } from '../router.js';
import { taskRow } from './task-row.js';
import { showToast } from './toast.js';
import { focusTasks, FOCUS_CAP } from '../triage.js';
import * as actions from '../actions.js';

// onChange re-renders the page after a task is ticked off
export function focusSection(onChange) {
  const tasks = focusTasks(getState().issues);
  if (tasks.length === 0) return null;

  const rows = tasks.map(t => taskRow(t, {
    onToggle: async (task) => {
      try {
        await actions.completeTask(task.number);
        showToast('Task completed', {
          action: () => actions.reopenTask(task.number).then(onChange).catch(err => showToast(`Error: ${err.message}`)),
        });
      } catch (err) {
        showToast(`Error: ${err.message}`);
      }
      onChange();
    },
    onClick: (task) => navigate(`#/task/${task.number}`),
  }));

  return el('section', { className: 'focus-section', 'aria-label': 'This week' },
    el('div', { className: 'focus-section-header' },
      el('h2', { className: 'focus-section-title' }, icon('check', 16), 'This week'),
      el('span', { className: 'focus-section-count' }, `${tasks.length}/${FOCUS_CAP}`),
      el('button', {
        className: 'btn btn-ghost btn-sm',
        onClick: () => navigate('#/triage'),
        title: 'Pick tasks for this week in Triage',
      }, 'Triage'),
    ),
    el('div', { className: 'task-list-container' }, ...rows),
  );
}
