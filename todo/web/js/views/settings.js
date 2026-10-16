// Settings view — PAT config, group management, theme toggle

import { el, icon, mount, clear } from '../render.js';
import { getState, setState, getAllLists } from '../state.js';
import { navigate } from '../router.js';
import { showToast } from '../components/toast.js';
import { confirm, showModal } from '../components/modal.js';
import * as api from '../api.js';
import * as db from '../db.js';

export function renderSettings(container) {
  const { groups, listMetadata } = getState();

  // --- Auth section ---
  const currentToken = api.getToken();
  const currentRepo = api.getRepo();
  const repoInput = el('input', {
    className: 'input',
    type: 'text',
    placeholder: 'your-user/tasks',
    id: 'repo-input',
    autocomplete: 'off',
    autocapitalize: 'off',
    spellcheck: 'false',
    value: currentRepo || '',
  });
  const tokenInput = el('input', {
    className: 'input',
    type: 'password',
    placeholder: 'Fine-grained personal access token',
    id: 'pat-input',
    value: currentToken || '',
  });

  const rememberCheck = el('input', {
    type: 'checkbox',
    checked: localStorage.getItem('todo_remember') !== 'false',
    id: 'remember-me',
  });

  const testBtn = el('button', {
    className: 'btn btn-primary',
    onClick: async () => {
      const token = tokenInput.value.trim();
      const repo = api.parseRepo(repoInput.value);
      if (!repo) {
        showToast('Enter the tasks repository as owner/name');
        repoInput.focus();
        return;
      }
      if (!token) {
        showToast('Please enter a PAT');
        return;
      }
      testBtn.textContent = 'Testing...';
      testBtn.disabled = true;
      const previousRepo = api.getRepo();
      api.setToken(token);
      api.setRepo(repo);
      try {
        const ok = await api.testConnection();
        if (ok) {
          localStorage.setItem('todo_repo', repo);
          repoInput.value = repo;
          // Another repo: the cached tasks are the old repo's, so drop them and load the new one's
          if (await db.useRepo(repo)) {
            setState({ issues: [], labels: [], groups: [], listMetadata: [] });
            import('../app.js').then(({ syncAllIssues }) => syncAllIssues(true)).catch(console.error);
          }
          const remember = rememberCheck.checked;
          if (remember) {
            localStorage.setItem('todo_pat', token);
            localStorage.setItem('todo_remember', 'true');
          } else {
            sessionStorage.setItem('todo_pat', token);
            localStorage.setItem('todo_remember', 'false');
            localStorage.removeItem('todo_pat');
          }
          showToast('Connected successfully!');
          statusEl.className = 'setup-status success';
          statusEl.textContent = `Connected to ${repo}`;
        } else {
          statusEl.className = 'setup-status error';
          statusEl.textContent = 'Could not access the repository';
          if (previousRepo) api.setRepo(previousRepo); else api.clearRepo();
        }
      } catch (err) {
        statusEl.className = 'setup-status error';
        statusEl.textContent = `Error: ${err.message}`;
        api.clearToken();
        if (previousRepo) api.setRepo(previousRepo); else api.clearRepo();
      }
      statusEl.style.display = 'flex';
      testBtn.textContent = 'Test Connection';
      testBtn.disabled = false;
    },
  }, 'Test Connection');

  const disconnectBtn = currentToken ? el('button', {
    className: 'btn btn-danger',
    onClick: () => {
      api.clearToken();
      localStorage.removeItem('todo_pat');
      sessionStorage.removeItem('todo_pat');
      showToast('Disconnected');
      renderSettings(container);
    },
  }, 'Disconnect') : null;

  const connected = api.isConfigured();
  const statusEl = el('div', { className: 'setup-status', style: { display: connected ? 'flex' : 'none' } },
    connected ? `Connected to ${currentRepo}` : ''
  );
  if (connected) statusEl.className = 'setup-status success';

  const authSection = el('div', { className: 'settings-section' },
    el('h3', { className: 'settings-section-title' }, 'GitHub Connection'),
    el('div', { className: 'form-group' },
      el('label', { className: 'form-label', htmlFor: 'repo-input' }, 'Tasks repository'),
      repoInput,
      el('p', { className: 'form-hint' },
        'The GitHub repo whose issues are your tasks, as owner/name. Changing it reloads the tasks from that repo.'),
    ),
    el('div', { className: 'form-group' },
      el('label', { className: 'form-label', htmlFor: 'pat-input' }, 'Personal Access Token'),
      tokenInput,
      el('p', { className: 'form-hint' }, api.PAT_HINT),
    ),
    el('div', { className: 'settings-row' },
      el('div', {},
        el('label', { className: 'settings-row-label', htmlFor: 'remember-me' }, 'Remember me'),
        el('div', { className: 'settings-row-desc' }, 'Store PAT in localStorage (persists across sessions)'),
      ),
      el('label', { className: 'toggle' },
        rememberCheck,
        el('span', { className: 'toggle-track' }),
      ),
    ),
    el('div', { style: { display: 'flex', gap: '8px', marginTop: 'var(--space-4)' } },
      testBtn,
      disconnectBtn,
    ),
    statusEl,
  );

  // --- Theme section ---
  const currentTheme = document.documentElement.getAttribute('data-theme') || 'light';
  const themeSection = el('div', { className: 'settings-section' },
    el('h3', { className: 'settings-section-title' }, 'Appearance'),
    el('div', { className: 'settings-row' },
      el('div', {},
        el('div', { className: 'settings-row-label' }, 'Dark mode'),
        el('div', { className: 'settings-row-desc' }, 'Toggle dark/light theme'),
      ),
      el('label', { className: 'toggle' },
        el('input', {
          type: 'checkbox',
          checked: currentTheme === 'dark',
          onChange: (e) => {
            const theme = e.target.checked ? 'dark' : 'light';
            document.documentElement.setAttribute('data-theme', theme);
            localStorage.setItem('todo_theme', theme);
          },
        }),
        el('span', { className: 'toggle-track' }),
      ),
    ),
  );

  // --- Groups section ---
  const groupsSection = el('div', { className: 'settings-section' },
    el('h3', { className: 'settings-section-title' }, 'Groups'),
    el('p', { style: { fontSize: 'var(--font-size-sm)', color: 'var(--color-text-secondary)', marginBottom: 'var(--space-4)' } },
      'Organize lists into groups. Groups are stored locally.',
    ),
  );

  const groupList = el('div', { className: 'group-list' });
  renderGroupList(groupList, groups, listMetadata);
  groupsSection.appendChild(groupList);

  const addGroupBtn = el('button', {
    className: 'btn btn-secondary',
    style: { marginTop: 'var(--space-3)' },
    onClick: () => showAddGroupModal(groupList),
  }, icon('plus', 16), 'Add Group');
  groupsSection.appendChild(addGroupBtn);

  // --- Assign lists section ---
  const assignSection = el('div', { className: 'settings-section' },
    el('h3', { className: 'settings-section-title' }, 'Assign Lists to Groups'),
  );

  const allLists = getAllLists();
  const metaMap = new Map(listMetadata.map(m => [m.listName, m]));

  for (const listName of allLists) {
    const meta = metaMap.get(listName);
    const currentGroup = meta?.groupName || '';

    const select = el('select', {
      className: 'input select',
      style: { width: '160px' },
      onChange: async (e) => {
        const groupName = e.target.value || null;
        const updatedMeta = { listName, groupName, sortOrder: meta?.sortOrder || 0 };
        await db.listMeta.put(updatedMeta);
        const allMeta = await db.listMeta.getAll();
        setState({ listMetadata: allMeta });
        showToast(`${listName} ${groupName ? `assigned to ${groupName}` : 'ungrouped'}`);
      },
    },
      el('option', { value: '' }, '(ungrouped)'),
      ...groups.map(g => {
        const opt = el('option', { value: g.name }, g.name);
        if (currentGroup === g.name) opt.selected = true;
        return opt;
      }),
    );

    assignSection.appendChild(
      el('div', { className: 'settings-row' },
        el('div', { className: 'settings-row-label' }, listName),
        select,
      )
    );
  }

  // --- Sync section ---
  const syncSection = el('div', { className: 'settings-section' },
    el('h3', { className: 'settings-section-title' }, 'Data'),
    el('div', { style: { display: 'flex', gap: '8px' } },
      el('button', {
        className: 'btn btn-secondary',
        onClick: async () => {
          showToast('Syncing...');
          try {
            const { syncAllIssues } = await import('../app.js');
            await syncAllIssues(true);
            showToast('Sync complete');
          } catch (err) {
            showToast(`Sync error: ${err.message}`);
          }
        },
      }, icon('refresh', 16), 'Force Sync'),
      el('button', {
        className: 'btn btn-danger',
        onClick: async () => {
          const ok = await confirm({
            title: 'Clear local cache?',
            message: 'This will delete all cached data. You will need to re-sync from GitHub.',
            confirmLabel: 'Clear',
            danger: true,
          });
          if (!ok) return;
          await db.issues.clear();
          await db.labels.clear();
          await db.meta.set('lastSync', null);
          setState({ issues: [], labels: [] });
          showToast('Cache cleared');
        },
      }, 'Clear Cache'),
    ),
  );

  // --- Keyboard shortcuts ---
  const shortcutsSection = el('div', { className: 'settings-section' },
    el('h3', { className: 'settings-section-title' }, 'Keyboard Shortcuts'),
    ...[
      ['n', 'New task'],
      ['/', 'Search'],
      ['Esc', 'Go back / Close'],
      ['j / k', 'Navigate tasks'],
      ['?', 'Show shortcuts'],
    ].map(([key, desc]) =>
      el('div', { className: 'settings-row' },
        el('div', { className: 'settings-row-label' }, desc),
        el('kbd', { className: 'kbd' }, key),
      )
    ),
  );

  mount(container,
    el('div', { className: 'view-header' },
      el('h1', { className: 'view-title' }, 'Settings'),
    ),
    authSection,
    themeSection,
    groupsSection,
    assignSection,
    syncSection,
    shortcutsSection,
  );
}

function renderGroupList(container, groups, listMetadata) {
  clear(container);
  for (const group of groups) {
    const listsInGroup = listMetadata.filter(m => m.groupName === group.name);
    const item = el('div', { className: 'group-item' },
      el('span', { className: 'group-item-name' }, group.name),
      el('span', { className: 'group-item-lists' },
        listsInGroup.length > 0
          ? `${listsInGroup.length} list${listsInGroup.length !== 1 ? 's' : ''}`
          : 'empty'
      ),
      el('button', {
        className: 'btn btn-ghost btn-sm',
        onClick: async () => {
          const ok = await confirm({
            title: `Delete group "${group.name}"?`,
            message: 'Lists in this group will become ungrouped.',
            confirmLabel: 'Delete',
            danger: true,
          });
          if (!ok) return;
          await db.groups.delete(group.name);
          // Ungroup lists
          for (const meta of listsInGroup) {
            await db.listMeta.put({ ...meta, groupName: null });
          }
          const allGroups = await db.groups.getAll();
          const allMeta = await db.listMeta.getAll();
          setState({ groups: allGroups, listMetadata: allMeta });
          showToast(`Group "${group.name}" deleted`);
          renderGroupList(container, allGroups, allMeta);
        },
      }, icon('trash', 14)),
    );
    container.appendChild(item);
  }

  if (groups.length === 0) {
    container.appendChild(
      el('p', { style: { color: 'var(--color-text-tertiary)', fontSize: 'var(--font-size-sm)' } },
        'No groups yet. Create one to organize your lists.'
      )
    );
  }
}

async function showAddGroupModal(groupListContainer) {
  const nameInput = el('input', {
    className: 'input',
    type: 'text',
    placeholder: 'Group name',
    autofocus: true,
  });

  const modal = showModal({
    title: 'Create Group',
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
        onClick: async () => {
          const name = nameInput.value.trim();
          if (!name) return;
          const { groups } = getState();
          const maxSort = groups.reduce((max, g) => Math.max(max, g.sortOrder || 0), 0);
          await db.groups.put({ name, sortOrder: maxSort + 1 });
          const allGroups = await db.groups.getAll();
          const allMeta = await db.listMeta.getAll();
          setState({ groups: allGroups });
          showToast(`Group "${name}" created`);
          renderGroupList(groupListContainer, allGroups, allMeta);
          modal.close();
        },
      }, 'Create'),
    ],
  });

  nameInput.focus();
  nameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      modal.close();
      const name = nameInput.value.trim();
      if (name) {
        (async () => {
          const { groups } = getState();
          const maxSort = groups.reduce((max, g) => Math.max(max, g.sortOrder || 0), 0);
          await db.groups.put({ name, sortOrder: maxSort + 1 });
          const allGroups = await db.groups.getAll();
          const allMeta = await db.listMeta.getAll();
          setState({ groups: allGroups });
          showToast(`Group "${name}" created`);
          renderGroupList(groupListContainer, allGroups, allMeta);
        })();
      }
    }
  });
}
