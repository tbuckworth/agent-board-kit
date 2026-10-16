// Group view — Lists within a group

import { el, icon, mount } from '../render.js';
import { getState, getActiveCountByList } from '../state.js';
import { navigate } from '../router.js';
import { titleCase } from '../parser.js';
import { filterBar } from '../components/filter-bar.js';

export function renderGroup(container, { name }) {
  const { listMetadata } = getState();
  const counts = getActiveCountByList();

  // Find lists in this group
  const listsInGroup = listMetadata
    .filter(m => m.groupName === name)
    .map(m => m.listName)
    .sort();

  // Also include lists that have tasks assigned but no metadata
  const { issues } = getState();
  for (const issue of issues) {
    if (issue.listName) {
      const meta = listMetadata.find(m => m.listName === issue.listName);
      if (meta?.groupName === name && !listsInGroup.includes(issue.listName)) {
        listsInGroup.push(issue.listName);
      }
    }
  }

  const totalTasks = listsInGroup.reduce((sum, l) => sum + (counts[l] || 0), 0);

  const header = el('div', { className: 'view-header' },
    el('h1', { className: 'view-title' }, name),
    el('p', { className: 'view-subtitle' },
      `${listsInGroup.length} list${listsInGroup.length !== 1 ? 's' : ''}, ${totalTasks} active task${totalTasks !== 1 ? 's' : ''}`,
    ),
  );

  if (listsInGroup.length === 0) {
    mount(container,
      header,
      el('div', { className: 'empty-state' },
        el('div', { className: 'empty-state-icon' }, icon('folder', 48)),
        el('div', { className: 'empty-state-title' }, 'Empty group'),
        el('div', { className: 'empty-state-text' }, 'Assign lists to this group in Settings.'),
      ),
    );
    return;
  }

  const cards = listsInGroup.map(listName =>
    el('button', {
      className: 'nav-card',
      onClick: () => navigate(`#/list/${encodeURIComponent(listName)}`),
    },
      el('div', { className: 'nav-card-icon list' }, icon('list', 18)),
      el('div', { className: 'nav-card-content' },
        el('div', { className: 'nav-card-name' }, titleCase(listName)),
        el('div', { className: 'nav-card-count' },
          `${counts[listName] || 0} task${(counts[listName] || 0) !== 1 ? 's' : ''}`,
        ),
      ),
      el('span', { className: 'nav-card-chevron' }, icon('chevronRight', 16)),
    )
  );

  const inGroup = new Set(listsInGroup.map(l => l.toLowerCase()));
  mount(container,
    header,
    filterBar({ tasks: issues.filter(i => i.state === 'OPEN' && inGroup.has(i.listName?.toLowerCase())) }),
    el('div', { className: 'task-list-container stagger-children' }, ...cards),
  );
}
