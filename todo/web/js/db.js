// IndexedDB wrapper for issue cache + group metadata

const DB_NAME = 'todo-web';
const DB_VERSION = 1;
let dbPromise = null;

function openDB() {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = (e) => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains('issues')) {
        db.createObjectStore('issues', { keyPath: 'number' });
      }
      if (!db.objectStoreNames.contains('labels')) {
        db.createObjectStore('labels', { keyPath: 'name' });
      }
      if (!db.objectStoreNames.contains('groups')) {
        db.createObjectStore('groups', { keyPath: 'name' });
      }
      if (!db.objectStoreNames.contains('listMetadata')) {
        db.createObjectStore('listMetadata', { keyPath: 'listName' });
      }
      if (!db.objectStoreNames.contains('meta')) {
        db.createObjectStore('meta', { keyPath: 'key' });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  return dbPromise;
}

function tx(storeName, mode = 'readonly') {
  return openDB().then(db => {
    const transaction = db.transaction(storeName, mode);
    return transaction.objectStore(storeName);
  });
}

// Generic helpers
async function getAll(storeName) {
  const store = await tx(storeName);
  return new Promise((resolve, reject) => {
    const req = store.getAll();
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function put(storeName, value) {
  const store = await tx(storeName, 'readwrite');
  return new Promise((resolve, reject) => {
    const req = store.put(value);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function putAll(storeName, values) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(storeName, 'readwrite');
    const store = transaction.objectStore(storeName);
    for (const val of values) {
      store.put(val);
    }
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error);
  });
}

async function del(storeName, key) {
  const store = await tx(storeName, 'readwrite');
  return new Promise((resolve, reject) => {
    const req = store.delete(key);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}

async function clearStore(storeName) {
  const store = await tx(storeName, 'readwrite');
  return new Promise((resolve, reject) => {
    const req = store.clear();
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}

// Issues
export const issues = {
  getAll: () => getAll('issues'),
  put: (issue) => put('issues', issue),
  putAll: (items) => putAll('issues', items),
  delete: (number) => del('issues', number),
  clear: () => clearStore('issues'),
};

// Labels
export const labels = {
  getAll: () => getAll('labels'),
  putAll: (items) => putAll('labels', items),
  clear: () => clearStore('labels'),
};

// Groups
export const groups = {
  getAll: () => getAll('groups'),
  put: (group) => put('groups', group),
  putAll: (items) => putAll('groups', items),
  delete: (name) => del('groups', name),
  clear: () => clearStore('groups'),
};

// List metadata
export const listMeta = {
  getAll: () => getAll('listMetadata'),
  put: (meta) => put('listMetadata', meta),
  putAll: (items) => putAll('listMetadata', items),
  delete: (listName) => del('listMetadata', listName),
  clear: () => clearStore('listMetadata'),
};

// Meta (sync timestamps, etc.)
export const meta = {
  async get(key) {
    const store = await tx('meta');
    return new Promise((resolve, reject) => {
      const req = store.get(key);
      req.onsuccess = () => resolve(req.result?.value);
      req.onerror = () => reject(req.error);
    });
  },
  set: (key, value) => put('meta', { key, value }),
};

// The cache belongs to one tasks repo. When the repo in Settings changes (or a cache predates the setting),
// drop the cached tasks, labels, groups and sync times so two repos' issues never mix. Returns true if it cleared.
export async function useRepo(repo) {
  if (!repo) return false;
  const current = await meta.get('repo');
  if (current && current.toLowerCase() === repo.toLowerCase()) return false;
  await Promise.all([issues.clear(), labels.clear(), groups.clear(), listMeta.clear()]);
  await meta.set('lastSync', null);
  await meta.set('lastFullSync', null);
  await meta.set('unrated', null);
  await meta.set('repo', repo);
  return true;
}
