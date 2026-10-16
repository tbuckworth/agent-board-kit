// Home view — Groups + ungrouped lists

import { el, icon, mount } from '../render.js';
import { getState, setState, getTopLevelItems, getActiveCountByList, getAllSections } from '../state.js';
import { navigate } from '../router.js';
import { filterBar } from '../components/filter-bar.js';
import { focusSection } from '../components/focus-section.js';
import { showModal } from '../components/modal.js';
import { showToast } from '../components/toast.js';
import { titleCase } from '../parser.js';
import * as db from '../db.js';

export function renderHome(container) {
  const items = getTopLevelItems();
  const counts = getActiveCountByList();

  const header = el('div', { className: 'view-header' },
    el('h1', { className: 'view-title' }, 'Tasks'),
    el('p', { className: 'view-subtitle' },
      `${Object.values(counts).reduce((a, b) => a + b, 0)} active tasks`
    ),
  );

  // Section chips (a cross-cutting filter)
  const sections = getAllSections();

  if (items.length === 0) {
    mount(container,
      header,
      ...(sections.length > 0 ? [filterBar()] : []),
      el('div', { className: 'empty-state' },
        el('div', { className: 'empty-state-icon' }, icon('list', 48)),
        el('div', { className: 'empty-state-title' }, 'No lists yet'),
        el('div', { className: 'empty-state-text' }, 'Tasks will appear once synced from GitHub Issues.'),
      ),
    );
    return;
  }

  const cards = items.map(item => {
    const isGroup = item.type === 'group';
    return el('button', {
      className: 'nav-card',
      onClick: () => navigate(isGroup ? `#/group/${encodeURIComponent(item.name)}` : `#/list/${encodeURIComponent(item.name)}`),
    },
      el('div', { className: `nav-card-icon ${isGroup ? 'group' : 'list'}` },
        icon(isGroup ? 'folder' : 'list', 18),
      ),
      el('div', { className: 'nav-card-content' },
        el('div', { className: 'nav-card-name' }, isGroup ? item.name : titleCase(item.name)),
        el('div', { className: 'nav-card-count' },
          `${item.taskCount} task${item.taskCount !== 1 ? 's' : ''}`,
        ),
      ),
      el('span', { className: 'nav-card-chevron' }, icon('chevronRight', 16)),
    );
  });

  const newListBtn = el('button', {
    className: 'btn btn-secondary',
    style: { marginTop: 'var(--space-3)' },
    onClick: () => showNewListModal(),
  }, icon('plus', 16), 'New List');

  mount(container,
    header,
    focusSection(() => renderHome(container)),
    filterBar(),
    el('div', { className: 'task-list-container stagger-children' }, ...cards),
    newListBtn,
  );
}

function showNewListModal() {
  const nameInput = el('input', {
    className: 'input',
    type: 'text',
    placeholder: 'List name',
    autofocus: true,
  });

  async function create() {
    const raw = nameInput.value.trim();
    if (!raw) return;
    const name = raw.toLowerCase().replace(/\s+/g, '-');
    await db.listMeta.put({ listName: name, groupName: null, sortOrder: 0 });
    const allMeta = await db.listMeta.getAll();
    setState({ listMetadata: allMeta });
    showToast(`List "${name}" created`);
    modal.close();
    navigate(`#/list/${encodeURIComponent(name)}`);
  }

  const modal = showModal({
    title: 'New List',
    body: el('div', { className: 'form-group' },
      el('label', { className: 'form-label' }, 'Name'),
      nameInput,
    ),
    footer: [
      el('button', {
        className: 'btn btn-secondary',
        onClick: () => modal.close(),
      }, 'Cancel'),
      el('button', {
        className: 'btn btn-primary',
        onClick: create,
      }, 'Create'),
    ],
  });

  nameInput.focus();
  nameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      create();
    }
  });
}
