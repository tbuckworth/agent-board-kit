// Service worker: caches static assets only, NOT API calls.
// index.html does not register it, so as shipped it does nothing (only the browser's HTTP cache applies). If you
// register it for offline use, bump CACHE_NAME on every deploy and keep STATIC_ASSETS in step with the files.

const CACHE_NAME = 'todo-v11';
const STATIC_ASSETS = [
  './',
  './index.html',
  './css/tokens.css',
  './css/base.css',
  './css/layout.css',
  './css/components.css',
  './css/views.css',
  './css/animations.css',
  './css/triage.css',
  './js/app.js',
  './js/state.js',
  './js/router.js',
  './js/render.js',
  './js/api.js',
  './js/db.js',
  './js/parser.js',
  './js/uuid.js',
  './js/triage.js',
  './js/tags.js',
  './js/bulk.js',
  './js/groups.js',
  './js/actions.js',
  './js/groups.js',
  './js/bulk.js',
  './js/views/home.js',
  './js/views/group.js',
  './js/views/list.js',
  './js/views/task.js',
  './js/views/search.js',
  './js/views/settings.js',
  './js/views/triage.js',
  './js/views/archived.js',
  './js/components/toast.js',
  './js/components/modal.js',
  './js/components/task-row.js',
  './js/components/label-chip.js',
  './js/components/claude-card.js',
  './js/components/filter-bar.js',
  './js/components/rating-controls.js',
  './js/components/skeleton.js',
  './js/components/focus-section.js',
  './js/components/select-list.js',
  './js/components/assign-dialog.js',
  './js/components/select-list.js',
  './fonts/inter-regular.woff2',
  './fonts/inter-medium.woff2',
  './fonts/inter-semibold.woff2',
  './fonts/inter-bold.woff2',
  './icons/favicon.svg',
  './manifest.json',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never cache API calls
  if (url.hostname === 'api.github.com') return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      // Network first for HTML, cache first for assets
      if (event.request.mode === 'navigate') {
        return fetch(event.request).catch(() => cached);
      }
      return cached || fetch(event.request);
    })
  );
});
