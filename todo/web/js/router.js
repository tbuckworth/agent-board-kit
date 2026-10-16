// Hash-based SPA router

let currentRoute = null;
let routeHandler = null;

const routes = [
  { pattern: /^#?\/?$/, view: 'list', params: () => ({ name: 'tasks' }) },
  { pattern: /^#\/group\/(.+)$/, view: 'group', params: (m) => ({ name: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/list\/(.+)$/, view: 'list', params: (m) => ({ name: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/task\/(\d+)$/, view: 'task', params: (m) => ({ number: parseInt(m[1], 10) }) },
  { pattern: /^#\/search$/, view: 'search', params: () => ({}) },
  { pattern: /^#\/settings$/, view: 'settings', params: () => ({}) },
  { pattern: /^#\/triage$/, view: 'triage', params: () => ({}) },
  { pattern: /^#\/archived$/, view: 'archived', params: () => ({ name: null }) },
  { pattern: /^#\/archived\/(.+)$/, view: 'archived', params: (m) => ({ name: decodeURIComponent(m[1]) }) },
];

function parseHash(hash) {
  for (const route of routes) {
    const match = route.pattern.exec(hash);
    if (match) {
      return { view: route.view, params: route.params(match) };
    }
  }
  return { view: 'home', params: {} };
}

export function navigate(path) {
  window.location.hash = path;
}

export function getCurrentRoute() {
  return currentRoute;
}

export function initRouter(handler) {
  routeHandler = handler;

  function onHashChange() {
    const hash = window.location.hash || '#/';
    const route = parseHash(hash);
    currentRoute = route;
    if (routeHandler) routeHandler(route);
  }

  window.addEventListener('hashchange', onHashChange);
  onHashChange(); // Handle initial load
}
