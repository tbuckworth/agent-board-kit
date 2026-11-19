// The PAT outside api.js: forgetting it, and what a failed sync means for it

import * as api from './api.js';
import { navigate } from './router.js';
import { showToast } from './components/toast.js';

// Forget the PAT here and in the browser's storage
export function forgetToken() {
  api.clearToken();
  localStorage.removeItem('todo_pat');
  sessionStorage.removeItem('todo_pat');
}

// A rejected token is forgotten and the user sent to Settings to reconnect; a missing tasks repo sends them there
// too (the token stays); any other failure is shown
export function reportSyncError(err) {
  if (err instanceof api.RepoError) {
    showToast(err.message);
    navigate('#/settings');
  } else if (err instanceof api.AuthError) {
    forgetToken();
    showToast('Authentication failed. Please reconnect.');
    navigate('#/settings');
  } else {
    showToast(`Sync error: ${err.message}`);
  }
}
