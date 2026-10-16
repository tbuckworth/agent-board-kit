// App entry point — auth gate, router, initial fetch, keyboard shortcuts

import { getState, setState, subscribe, getTopLevelItems, getAllLists, getActiveCountByList } from './state.js';
import { initRouter, navigate, getCurrentRoute } from './router.js';
import { el, icon, mount, clear } from './render.js';
import { parseIssue, titleCase, ratingFields, bodyFields, setUnrated, unratedFromConfig } from './parser.js';
import { tagFields } from './tags.js';
import * as api from './api.js';
import * as db from './db.js';
import { showToast } from './components/toast.js';
import { renderHome } from './views/home.js';
import { renderGroup } from './views/group.js';
import { renderList } from './views/list.js';
import { renderTask } from './views/task.js';
import { renderSearch } from './views/search.js';
import { renderSettings } from './views/settings.js';
import { renderTriage } from './views/triage.js';
import { renderArchived } from './views/archived.js';
import { skeletonList } from './components/skeleton.js';

// Theme
const savedTheme = localStorage.getItem('todo_theme') ||
  (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
document.documentElement.setAttribute('data-theme', savedTheme);

// Auth
const token = localStorage.getItem('todo_pat') || sessionStorage.getItem('todo_pat');
if (token) api.setToken(token);
// The tasks repo, "owner/name", entered in Settings next to the PAT
const savedRepo = localStorage.getItem('todo_repo');
if (savedRepo) {
  try { api.setRepo(savedRepo); } catch { localStorage.removeItem('todo_repo'); }
}

// DOM refs
const appShell = document.getElementById('app');

// Build the app shell
function buildShell() {
  const breadcrumb = el('nav', { className: 'topbar-breadcrumb', 'aria-label': 'Breadcrumb' });
  const syncBtn = el('button', {
    title: 'Sync',
    'aria-label': 'Sync',
    onClick: async () => {
      syncBtn.classList.add('spinning');
      await syncAllIssues(true);
      syncBtn.classList.remove('spinning');
    },
  }, icon('refresh', 20));

  const topbarActions = el('div', { className: 'topbar-actions' },
    syncBtn,
    el('button', {
      title: 'Search',
      'aria-label': 'Search',
      onClick: () => navigate('#/search'),
    }, icon('search', 20)),
    el('button', {
      title: 'Toggle theme',
      'aria-label': 'Toggle theme',
      onClick: () => {
        const current = document.documentElement.getAttribute('data-theme');
        const next = current === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        localStorage.setItem('todo_theme', next);
      },
    }, icon('moon', 20)),
    el('button', {
      title: 'Settings',
      'aria-label': 'Settings',
      onClick: () => navigate('#/settings'),
    }, icon('settings', 20)),
  );

  const topbar = el('header', { className: 'topbar' }, breadcrumb, topbarActions);
  const sidebar = el('aside', { className: 'sidebar', role: 'navigation', 'aria-label': 'Lists' });
  const mainContent = el('main', { className: 'main-content', id: 'main-content' },
    el('div', { className: 'main-content-inner' }),
  );

  const bottomNav = el('nav', { className: 'bottom-nav', 'aria-label': 'Navigation' },
    el('div', { className: 'bottom-nav-items' },
      el('button', {
        className: 'bottom-nav-item',
        dataset: { nav: 'home' },
        onClick: () => navigate('#/'),
      }, el('span', { className: 'nav-icon' }, icon('home', 20)), 'Tasks'),
      el('button', {
        className: 'bottom-nav-item',
        dataset: { nav: 'triage' },
        onClick: () => navigate('#/triage'),
      }, el('span', { className: 'nav-icon' }, icon('cards', 20)), 'Triage'),
      el('button', {
        className: 'bottom-nav-item',
        dataset: { nav: 'search' },
        onClick: () => navigate('#/search'),
      }, el('span', { className: 'nav-icon' }, icon('search', 20)), 'Search'),
      el('button', {
        className: 'bottom-nav-item',
        dataset: { nav: 'settings' },
        onClick: () => navigate('#/settings'),
      }, el('span', { className: 'nav-icon' }, icon('settings', 20)), 'Settings'),
    ),
  );

  const appBody = el('div', { className: 'app-body' }, sidebar, mainContent);
  const skipLink = el('a', { className: 'skip-link', href: '#main-content' }, 'Skip to main content');

  mount(appShell,
    skipLink,
    el('div', { className: 'app-shell' }, topbar, appBody, bottomNav),
  );

  return { breadcrumb, sidebar, mainContent: mainContent.firstChild, topbar, bottomNav };
}

let shell = null;

function ensureShell() {
  if (!shell) shell = buildShell();
  return shell;
}

// Sidebar rendering
function renderSidebar() {
  const { sidebar } = ensureShell();
  clear(sidebar);

  const items = getTopLevelItems();
  const route = getCurrentRoute();

  // Triage and the archive
  const reviewSection = el('div', { className: 'sidebar-section' });
  for (const [view, label, iconName, hash] of [
    ['triage', 'Triage', 'cards', '#/triage'],
    ['archived', 'Archived', 'archive', '#/archived'],
  ]) {
    reviewSection.appendChild(el('button', {
      className: `sidebar-item ${route?.view === view ? 'active' : ''}`,
      onClick: () => navigate(hash),
    }, el('span', { className: 'icon' }, icon(iconName, 16)), label));
  }
  sidebar.appendChild(reviewSection);

  // Groups section
  const groupItems = items.filter(i => i.type === 'group');
  if (groupItems.length > 0) {
    const section = el('div', { className: 'sidebar-section' },
      el('div', { className: 'sidebar-section-title' }, 'Groups'),
    );
    for (const item of groupItems) {
      const isActive = route?.view === 'group' && route?.params?.name === item.name;
      section.appendChild(
        el('button', {
          className: `sidebar-item ${isActive ? 'active' : ''}`,
          onClick: () => navigate(`#/group/${encodeURIComponent(item.name)}`),
        },
          el('span', { className: 'icon' }, icon('folder', 16)),
          item.name,
          el('span', { className: 'count' }, String(item.taskCount)),
        )
      );
    }
    sidebar.appendChild(section);
  }

  // Lists section
  const listItems = items.filter(i => i.type === 'list');
  if (listItems.length > 0) {
    const section = el('div', { className: 'sidebar-section' },
      el('div', { className: 'sidebar-section-title' }, 'Lists'),
    );
    for (const item of listItems) {
      const isActive = route?.view === 'list' && route?.params?.name === item.name;
      section.appendChild(
        el('button', {
          className: `sidebar-item ${isActive ? 'active' : ''}`,
          onClick: () => navigate(`#/list/${encodeURIComponent(item.name)}`),
        },
          el('span', { className: 'icon' }, icon('list', 16)),
          titleCase(item.name),
          el('span', { className: 'count' }, String(item.taskCount)),
        )
      );
    }
    sidebar.appendChild(section);
  }

  // Also list groups' child lists
  for (const group of groupItems) {
    if (route?.view === 'group' && route?.params?.name === group.name) {
      const childSection = el('div', { className: 'sidebar-section' },
        el('div', { className: 'sidebar-section-title' }, group.name),
      );
      const counts = getActiveCountByList();
      for (const listName of group.lists || []) {
        const isActive = route?.view === 'list' && route?.params?.name === listName;
        childSection.appendChild(
          el('button', {
            className: `sidebar-item ${isActive ? 'active' : ''}`,
            onClick: () => navigate(`#/list/${encodeURIComponent(listName)}`),
          },
            el('span', { className: 'icon' }, icon('list', 16)),
            titleCase(listName),
            el('span', { className: 'count' }, String(counts[listName] || 0)),
          )
        );
      }
      sidebar.appendChild(childSection);
    }
  }
}

// Breadcrumb
function updateBreadcrumb(route) {
  const { breadcrumb } = ensureShell();
  clear(breadcrumb);

  const homeBtn = el('button', { onClick: () => navigate('#/') }, 'Tasks');
  breadcrumb.appendChild(homeBtn);

  if (route.view === 'group') {
    breadcrumb.appendChild(el('span', { className: 'breadcrumb-sep' }, '/'));
    breadcrumb.appendChild(el('span', { className: 'breadcrumb-current' }, route.params.name));
  } else if (route.view === 'list') {
    // Check if list belongs to a group
    const { listMetadata } = getState();
    const meta = listMetadata.find(m => m.listName === route.params.name);
    if (meta?.groupName) {
      breadcrumb.appendChild(el('span', { className: 'breadcrumb-sep' }, '/'));
      breadcrumb.appendChild(
        el('button', { onClick: () => navigate(`#/group/${encodeURIComponent(meta.groupName)}`) },
          meta.groupName,
        )
      );
    }
    breadcrumb.appendChild(el('span', { className: 'breadcrumb-sep' }, '/'));
    breadcrumb.appendChild(el('span', { className: 'breadcrumb-current' }, titleCase(route.params.name)));
  } else if (route.view === 'task') {
    breadcrumb.appendChild(el('span', { className: 'breadcrumb-sep' }, '/'));
    const task = getState().issues.find(i => i.number === route.params.number);
    if (task?.listName) {
      breadcrumb.appendChild(
        el('button', { onClick: () => navigate(`#/list/${encodeURIComponent(task.listName)}`) },
          titleCase(task.listName),
        )
      );
      breadcrumb.appendChild(el('span', { className: 'breadcrumb-sep' }, '/'));
    }
    breadcrumb.appendChild(el('span', { className: 'breadcrumb-current' },
      task ? task.title : `#${route.params.number}`,
    ));
  } else if (route.view === 'search') {
    breadcrumb.appendChild(el('span', { className: 'breadcrumb-sep' }, '/'));
    breadcrumb.appendChild(el('span', { className: 'breadcrumb-current' }, 'Search'));
  } else if (route.view === 'settings') {
    breadcrumb.appendChild(el('span', { className: 'breadcrumb-sep' }, '/'));
    breadcrumb.appendChild(el('span', { className: 'breadcrumb-current' }, 'Settings'));
  } else if (route.view === 'triage' || route.view === 'archived') {
    breadcrumb.appendChild(el('span', { className: 'breadcrumb-sep' }, '/'));
    breadcrumb.appendChild(el('span', { className: 'breadcrumb-current' }, route.view === 'triage' ? 'Triage' : 'Archived'));
  }
}

// Bottom nav active state
function updateBottomNav(route) {
  const { bottomNav } = ensureShell();
  const items = bottomNav.querySelectorAll('.bottom-nav-item');
  items.forEach(item => {
    const nav = item.dataset.nav;
    const isActive =
      (nav === 'home' && ['home', 'group', 'list', 'task'].includes(route.view)) ||
      (nav === 'triage' && route.view === 'triage') ||
      (nav === 'search' && route.view === 'search') ||
      (nav === 'settings' && route.view === 'settings');
    item.classList.toggle('active', isActive);
  });
}

// Route handler
function onRoute(route) {
  if (!api.isConfigured() && route.view !== 'settings') {
    // Show setup screen
    showSetup();
    return;
  }

  ensureShell();
  const { mainContent } = shell;
  clear(mainContent);

  updateBreadcrumb(route);
  updateBottomNav(route);
  renderSidebar();

  switch (route.view) {
    case 'home':
      renderHome(mainContent);
      break;
    case 'group':
      renderGroup(mainContent, route.params);
      break;
    case 'list':
      renderList(mainContent, route.params);
      break;
    case 'task':
      renderTask(mainContent, route.params);
      break;
    case 'search':
      renderSearch(mainContent);
      break;
    case 'settings':
      renderSettings(mainContent);
      break;
    case 'triage':
      renderTriage(mainContent);
      break;
    case 'archived':
      renderArchived(mainContent, route.params, { refresh: () => syncAllIssues(true) });
      break;
    default:
      renderHome(mainContent);
  }
}

// Setup screen (no PAT or no repo yet)
function showSetup() {
  const repoInput = el('input', {
    className: 'input',
    type: 'text',
    placeholder: 'your-user/tasks',
    id: 'repo-input',
    autocomplete: 'off',
    autocapitalize: 'off',
    spellcheck: 'false',
    value: api.getRepo() || '',
  });

  const tokenInput = el('input', {
    className: 'input',
    type: 'password',
    placeholder: 'Fine-grained personal access token',
    id: 'pat-input',
    value: api.getToken() || '',
  });

  const statusEl = el('div', { className: 'setup-status', style: { display: 'none' } });

  const connectBtn = el('button', {
    className: 'btn btn-primary btn-lg',
    style: { width: '100%', marginTop: 'var(--space-4)' },
    onClick: async () => {
      const token = tokenInput.value.trim();
      const repo = api.parseRepo(repoInput.value);
      statusEl.style.display = 'flex';
      if (!repo) {
        statusEl.className = 'setup-status error';
        statusEl.textContent = 'Enter the tasks repository as owner/name, e.g. your-user/tasks.';
        repoInput.focus();
        return;
      }
      if (!token) {
        statusEl.className = 'setup-status error';
        statusEl.textContent = 'Enter a personal access token.';
        tokenInput.focus();
        return;
      }
      connectBtn.textContent = 'Connecting...';
      connectBtn.disabled = true;
      statusEl.className = 'setup-status loading';
      statusEl.textContent = `Testing connection to ${repo}...`;
      api.setToken(token);
      api.setRepo(repo);
      try {
        const ok = await api.testConnection();
        if (ok) {
          localStorage.setItem('todo_pat', token);
          localStorage.setItem('todo_repo', repo);
          await db.useRepo(repo);
          statusEl.className = 'setup-status success';
          statusEl.textContent = 'Connected! Loading tasks...';
          await syncAllIssues(true);
          navigate('#/');
          return;
        }
        statusEl.className = 'setup-status error';
        statusEl.textContent = 'Could not access the repository. Check its name and the PAT\'s repository access.';
        api.clearToken();
      } catch (err) {
        statusEl.className = 'setup-status error';
        statusEl.textContent = `Error: ${err.message}`;
        api.clearToken();
      }
      connectBtn.textContent = 'Connect';
      connectBtn.disabled = false;
    },
  }, 'Connect');

  mount(appShell,
    el('div', { className: 'setup-screen' },
      el('div', { className: 'setup-card' },
        el('div', { className: 'setup-logo' },
          el('h1', {}, 'Tasks'),
          el('p', {}, 'Connect to your GitHub Issues backend'),
        ),
        el('div', { className: 'form-group' },
          el('label', { className: 'form-label', htmlFor: 'repo-input' }, 'Tasks repository'),
          repoInput,
          el('p', { className: 'form-hint' }, 'The GitHub repo whose issues are your tasks, as owner/name.'),
        ),
        el('div', { className: 'form-group' },
          el('label', { className: 'form-label', htmlFor: 'pat-input' }, 'Personal Access Token'),
          tokenInput,
          el('p', { className: 'form-hint' }, api.PAT_HINT),
        ),
        connectBtn,
        statusEl,
      ),
    ),
  );

  (api.getRepo() ? tokenInput : repoInput).focus();
}

// Cached issues, with the label-derived fields that older versions did not store
async function loadCachedIssues() {
  const cached = await db.issues.getAll();
  return cached.map(issue => ({
    ...issue,
    ...ratingFields(issue.labels || []),
    ...tagFields(issue.labels || []),
    ...(issue.rawBody !== undefined ? bodyFields(issue.rawBody) : {}),
  }));
}

// Sync all issues
const FULL_SYNC_EVERY_MS = 24 * 60 * 60 * 1000;

export async function syncAllIssues(fullSync = false) {
  setState({ syncState: 'syncing' });

  try {
    // Load from IndexedDB cache first
    const cachedIssues = await loadCachedIssues();
    const cachedGroups = await db.groups.getAll();
    const cachedMeta = await db.listMeta.getAll();

    if (cachedIssues.length > 0) {
      setState({
        issues: cachedIssues,
        groups: cachedGroups,
        listMetadata: cachedMeta,
      });
    }

    // A sync since lastSync never sees an issue deleted on GitHub, so once a day it reloads everything
    const lastFull = await db.meta.get('lastFullSync');
    if (!lastFull || Date.now() - Date.parse(lastFull) > FULL_SYNC_EVERY_MS) fullSync = true;

    // Fetch from API
    const lastSync = fullSync ? null : await db.meta.get('lastSync');
    const rawIssues = await api.fetchAllIssues(lastSync, (count) => {
      setState({ syncState: 'syncing' });
    });

    const parsedIssues = rawIssues.map(parseIssue);

    if (lastSync && !fullSync) {
      // Merge with cache
      const { issues } = getState();
      const issueMap = new Map(issues.map(i => [i.number, i]));
      for (const issue of parsedIssues) {
        issueMap.set(issue.number, issue);
      }
      const merged = [...issueMap.values()];
      setState({ issues: merged });
      await db.issues.putAll(parsedIssues);
    } else {
      // Full replace
      setState({ issues: parsedIssues });
      await db.issues.clear();
      await db.issues.putAll(parsedIssues);
    }

    // Save sync timestamp
    await db.meta.set('lastSync', new Date().toISOString());
    if (fullSync) await db.meta.set('lastFullSync', new Date().toISOString());

    // Fetch config (groups/list metadata) from .todo/config.json
    try {
      const configResult = await api.fetchConfig();
      // Which lists are not rated: from the config, remembered for the next start
      const unrated = unratedFromConfig(configResult?.config);
      setUnrated(unrated);
      await db.meta.set('unrated', unrated);
      if (configResult) {
        const { config } = configResult;
        const groups = (config.groups || []).map(g => ({ name: g.name, sortOrder: g.sortOrder }));
        const listMetadata = (config.listMetadata || []).map(m => ({
          listName: m.listName, groupName: m.groupName, sortOrder: m.sortOrder,
        }));
        await db.groups.clear();
        if (groups.length > 0) await db.groups.putAll(groups);
        await db.listMeta.clear();
        if (listMetadata.length > 0) await db.listMeta.putAll(listMetadata);
        setState({ groups, listMetadata });
      }
    } catch (e) {
      console.warn('Config sync failed:', e);
    }

    // Also fetch labels
    try {
      const labels = await api.fetchLabels();
      await db.labels.clear();
      await db.labels.putAll(labels);
      setState({ labels, syncState: 'success' });
    } catch {
      setState({ syncState: 'success' });
    }
  } catch (err) {
    if (err instanceof api.RepoError) {
      setState({ syncState: 'error', lastError: err.message });
      showToast(err.message);
      navigate('#/settings');
    } else if (err instanceof api.AuthError) {
      setState({ syncState: 'auth_error', lastError: err.message });
      api.clearToken();
      localStorage.removeItem('todo_pat');
      sessionStorage.removeItem('todo_pat');
      showToast('Authentication failed. Please reconnect.');
      navigate('#/settings');
    } else {
      setState({ syncState: 'error', lastError: err.message });
      showToast(`Sync error: ${err.message}`);
    }
  }
}

// Re-render on state change (debounced)
let renderTimer = null;
subscribe((next, prev) => {
  // Filter chips change what the home, group and list views show
  if (next.filters !== prev.filters) {
    const route = getCurrentRoute();
    if (route && shell && ['home', 'group', 'list'].includes(route.view)) onRoute(route);
  }
  clearTimeout(renderTimer);
  renderTimer = setTimeout(() => {
    const route = getCurrentRoute();
    if (route && shell) {
      renderSidebar();
    }
  }, 50);
});

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
  // Ignore when typing in inputs
  const tag = e.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
    if (e.key === 'Escape') e.target.blur();
    return;
  }

  switch (e.key) {
    case 'n':
      e.preventDefault();
      // Navigate to the current list view to add a task, or home
      const route = getCurrentRoute();
      if (route?.view === 'list') {
        // The list view will handle it — just need to make the add form visible
      }
      break;
    case '/':
      e.preventDefault();
      navigate('#/search');
      break;
    case 'Escape':
      // The open dialog handles its own Escape
      if (document.querySelector('.modal-backdrop')) break;
      // With tasks selected, Escape clears the selection (the list handles that) instead of going back
      if (document.querySelector('.selection-bar.open')) break;
      if (getCurrentRoute()?.view !== 'home') {
        window.history.back();
      }
      break;
  }
});

// Init
async function init() {
  // The cache is for one repo: drop it if the repo in Settings changed since it was written
  if (api.getRepo()) await db.useRepo(api.getRepo()).catch(console.warn);

  // Load cached data from IndexedDB
  setUnrated((await db.meta.get('unrated').catch(() => null)) || {});
  const cachedIssues = await loadCachedIssues();
  const cachedGroups = await db.groups.getAll();
  const cachedMeta = await db.listMeta.getAll();

  setState({
    issues: cachedIssues,
    groups: cachedGroups,
    listMetadata: cachedMeta,
  });

  // Init router
  initRouter(onRoute);

  // With a token and a repo, sync in the background
  if (api.isConfigured()) {
    syncAllIssues().catch(console.error);
  }

  // Sync on tab focus
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && api.isConfigured()) {
      syncAllIssues().catch(console.error);
    }
  });

  // Periodic sync every 5 minutes
  setInterval(() => {
    if (!document.hidden && api.isConfigured()) {
      syncAllIssues().catch(console.error);
    }
  }, 5 * 60 * 1000);
}

init();
