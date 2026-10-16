// Search view — full-text search + filter chips

import { el, icon, mount, clear } from '../render.js';
import { getState, setState, matchesFilters } from '../state.js';
import { navigate } from '../router.js';
import { taskRow } from '../components/task-row.js';
import { filterBar } from '../components/filter-bar.js';
import { showToast } from '../components/toast.js';
import * as api from '../api.js';
import * as db from '../db.js';

export function renderSearch(container) {
  let query = '';
  let results = [];
  let debounceTimer = null;

  const searchIcon = el('span', { className: 'search-icon' }, icon('search', 16));

  const searchInput = el('input', {
    className: 'input search-input',
    type: 'search',
    placeholder: 'Search tasks...',
    'aria-label': 'Search tasks',
    autofocus: true,
    onInput: (e) => {
      query = e.target.value;
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => doSearch(query), 200);
    },
  });

  const searchWrapper = el('div', { className: 'search-input-wrapper' },
    searchIcon, searchInput,
  );

  const resultsCount = el('div', { className: 'search-results-count' });
  const resultsContainer = el('div');

  mount(container,
    el('div', { className: 'view-header' },
      el('h1', { className: 'view-title' }, 'Search'),
    ),
    searchWrapper,
    resultsCount,
    resultsContainer,
  );

  searchInput.focus();

  function doSearch(q) {
    const trimmed = q.trim().toLowerCase();
    if (!trimmed) {
      results = [];
      clear(resultsContainer);
      resultsCount.textContent = '';
      return;
    }

    const { issues, filters } = getState();
    results = issues.filter(issue => {
      // Text match
      const textMatch =
        issue.title.toLowerCase().includes(trimmed) ||
        (issue.body && issue.body.toLowerCase().includes(trimmed)) ||
        (issue.claudeNotes && issue.claudeNotes.toLowerCase().includes(trimmed));
      if (!textMatch) return false;

      // Apply active filters
      if (!filters.showCompleted && issue.state === 'CLOSED') return false;
      return matchesFilters(issue, filters);
    });

    resultsCount.textContent = `${results.length} result${results.length !== 1 ? 's' : ''}`;

    clear(resultsContainer);
    if (results.length === 0) {
      resultsContainer.appendChild(
        el('div', { className: 'empty-state', style: { padding: '2rem 1rem' } },
          el('div', { className: 'empty-state-title' }, 'No results'),
          el('div', { className: 'empty-state-text' }, `No tasks matching "${q}"`),
        )
      );
      return;
    }

    const rows = results.map(t =>
      taskRow(t, {
        onToggle: async (task) => {
          const wasOpen = task.state === 'OPEN';
          const { issues } = getState();
          const updated = issues.map(i =>
            i.number === task.number ? { ...i, state: wasOpen ? 'CLOSED' : 'OPEN' } : i
          );
          setState({ issues: updated });
          doSearch(query); // Re-render results

          try {
            if (wasOpen) await api.closeIssue(task.number);
            else await api.reopenIssue(task.number);
            const final = getState().issues.find(i => i.number === task.number);
            if (final) await db.issues.put(final);
          } catch (err) {
            showToast(`Error: ${err.message}`);
          }
        },
        onClick: (task) => navigate(`#/task/${task.number}`),
      })
    );

    resultsContainer.appendChild(
      el('div', { className: 'task-list-container' }, ...rows)
    );
  }
}
