// Archived tasks (closed as "not planned"), across every list or in one. Tick a row to bring it back.

import { el, icon, mount, clear } from '../render.js';
import { getState } from '../state.js';
import { navigate, getCurrentRoute } from '../router.js';
import { taskRow } from '../components/task-row.js';
import { showToast } from '../components/toast.js';
import { titleCase } from '../parser.js';
import { isArchived, needsStateReason } from '../triage.js';
import * as actions from '../actions.js';

let refreshedThisSession = false;

// refresh(): a full sync, which fills in stateReason for issues cached before it was fetched
export function renderArchived(container, { name = null } = {}, { refresh } = {}) {
  let query = '';

  function stillHere() {
    const route = getCurrentRoute();
    return route?.view === 'archived' && (route.params.name || null) === name;
  }

  function archivedTasks() {
    const q = query.trim().toLowerCase();
    return getState().issues
      .filter(isArchived)
      .filter(t => !name || t.listName?.toLowerCase() === name.toLowerCase())
      .filter(t => !q || t.title.toLowerCase().includes(q) || (t.body || '').toLowerCase().includes(q))
      .sort((a, b) => (b.updatedAt || '').localeCompare(a.updatedAt || ''));
  }

  const results = el('div');
  const subtitle = el('p', { className: 'view-subtitle' });
  const loading = el('p', { className: 'form-hint' });

  function row(task) {
    return taskRow(task, {
      onToggle: async (t) => {
        try {
          await actions.reopenTask(t.number);
          showToast(`Back in ${titleCase(t.listName)}`);
        } catch (err) {
          showToast(`Error: ${err.message}`);
        }
        renderResults();
      },
      onClick: (t) => navigate(`#/task/${t.number}`),
    });
  }

  function renderResults() {
    clear(results);
    const tasks = archivedTasks();
    subtitle.textContent = `${tasks.length} archived task${tasks.length !== 1 ? 's' : ''} · tick one to bring it back`;
    if (tasks.length === 0) {
      results.appendChild(el('div', { className: 'empty-state', style: { padding: '2rem 1rem' } },
        el('div', { className: 'empty-state-title' }, 'Nothing archived'),
      ));
      return;
    }
    if (name) {
      results.appendChild(el('div', { className: 'task-list-container' }, ...tasks.map(row)));
      return;
    }
    const byList = new Map();
    for (const t of tasks) {
      const list = t.listName || 'tasks';
      if (!byList.has(list)) byList.set(list, []);
      byList.get(list).push(t);
    }
    for (const list of [...byList.keys()].sort()) {
      results.appendChild(el('h2', { className: 'archived-list-title' },
        el('button', {
          className: 'btn btn-ghost btn-sm',
          onClick: () => navigate(`#/archived/${encodeURIComponent(list)}`),
        }, `${titleCase(list)} (${byList.get(list).length})`),
      ));
      results.appendChild(el('div', { className: 'task-list-container' }, ...byList.get(list).map(row)));
    }
  }

  const search = el('input', {
    className: 'input',
    type: 'search',
    placeholder: 'Filter archived tasks...',
    'aria-label': 'Filter archived tasks',
    onInput: (e) => { query = e.target.value; renderResults(); },
  });

  mount(container,
    el('div', { className: 'view-header' },
      el('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' } },
        el('h1', { className: 'view-title' }, name ? `Archived · ${titleCase(name)}` : 'Archived'),
        name
          ? el('button', { className: 'btn btn-ghost btn-sm', onClick: () => navigate('#/archived') }, 'All lists')
          : null,
      ),
      subtitle,
    ),
    el('div', { className: 'form-group' }, search),
    loading,
    results,
  );
  renderResults();

  // Issues cached before the archive reason was fetched need one full sync
  if (refresh && !refreshedThisSession && needsStateReason(getState().issues)) {
    refreshedThisSession = true;
    loading.textContent = 'Fetching archive reasons from GitHub…';
    loading.prepend(icon('refresh', 14), ' ');
    Promise.resolve(refresh()).finally(() => {
      if (stillHere()) {
        loading.textContent = '';
        renderResults();
      }
    });
  }
}
