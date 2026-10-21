// GitHub GraphQL API client for the tasks repo (plus the REST contents API for .todo/config.json)
import { labelColor } from './tags.js';

const GRAPHQL_URL = 'https://api.github.com/graphql';

// The tasks repo, "owner/name", entered in Settings next to the PAT and kept in localStorage.
// GitHub owners are 1-39 letters, digits or dashes; repo names are letters, digits, '.', '_' or '-'.
const REPO_RE = /^([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))\/([A-Za-z0-9._-]{1,100})$/;
let OWNER = null;
let REPO = null;

let _token = null;
let _repoId = null;
let _labelMap = null; // name → id
let _lastMutationTime = 0;
const MUTATION_DELAY = 1000; // 1s between mutations

export function setToken(token) {
  _token = token;
  _repoId = null;
  _labelMap = null;
}

// "owner/name" as typed (spaces, a github.com URL or a trailing .git are tolerated), or null when it isn't one
export function parseRepo(text) {
  let t = (text || '').trim().replace(/^https?:\/\/github\.com\//i, '').replace(/\.git$/i, '').replace(/\/+$/, '');
  const m = REPO_RE.exec(t);
  if (!m || m[2] === '.' || m[2] === '..') return null;
  return `${m[1]}/${m[2]}`;
}

// Point every call at this repo. Returns the normalised "owner/name"; throws on anything else.
export function setRepo(text) {
  const full = parseRepo(text);
  if (!full) throw new Error('The repository must look like owner/name');
  [OWNER, REPO] = full.split('/');
  _repoId = null;
  _labelMap = null;
  return full;
}

export function getRepo() {
  return OWNER && REPO ? `${OWNER}/${REPO}` : null;
}

export function clearRepo() {
  OWNER = null;
  REPO = null;
  _repoId = null;
  _labelMap = null;
}

// What the PAT needs, shown on the setup screen and in Settings
export const PAT_HINT = 'A fine-grained PAT (github.com/settings/personal-access-tokens) with access to only the ' +
  'tasks repository: Issues read and write, and Contents read and write (read is enough unless you delete lists). ' +
  'It is kept in this browser only.';

// A token and a repo: what every call needs
export function isConfigured() {
  return !!_token && !!OWNER && !!REPO;
}

function requireRepo() {
  if (!OWNER || !REPO) throw new RepoError('No tasks repository configured; enter it in Settings');
}

export function getToken() {
  return _token;
}

export function clearToken() {
  _token = null;
  _repoId = null;
  _labelMap = null;
}

async function gql(query, variables = {}) {
  if (!_token) throw new AuthError('No token configured');
  requireRepo();

  const res = await fetch(GRAPHQL_URL, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${_token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ query, variables }),
  });

  if (res.status === 401) {
    throw new AuthError('Invalid or expired token');
  }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`GitHub API error: ${res.status} - ${text}`);
  }

  const json = await res.json();
  if (json.errors) {
    throw new Error(`GraphQL errors: ${json.errors.map(e => e.message).join(', ')}`);
  }
  return json.data;
}

async function waitForMutationSlot() {
  const now = Date.now();
  const elapsed = now - _lastMutationTime;
  if (elapsed < MUTATION_DELAY) {
    await new Promise(r => setTimeout(r, MUTATION_DELAY - elapsed));
  }
  _lastMutationTime = Date.now();
}

// Test connection
export async function testConnection() {
  const data = await gql(`
    query {
      repository(owner: "${OWNER}", name: "${REPO}") {
        name
      }
    }
  `);
  return !!data?.repository?.name;
}

// Fetch issues with pagination
export async function fetchIssues(since = null, cursor = null) {
  const sinceFilter = since ? `, filterBy: {since: "${since}"}` : '';
  const afterCursor = cursor ? `, after: "${cursor}"` : '';

  const data = await gql(`
    query {
      repository(owner: "${OWNER}", name: "${REPO}") {
        issues(first: 100, orderBy: {field: UPDATED_AT, direction: DESC}${sinceFilter}${afterCursor}) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            number
            id
            title
            body
            state
            stateReason
            updatedAt
            createdAt
            parent { number }
            labels(first: 20) {
              nodes {
                name
              }
            }
          }
        }
      }
    }
  `);

  const issues = data.repository.issues;
  const nodes = issues.nodes;
  const pageInfo = issues.pageInfo;
  const nextCursor = pageInfo.hasNextPage ? pageInfo.endCursor : null;

  return { issues: nodes, nextCursor };
}

// Fetch all issues (paginated)
export async function fetchAllIssues(since = null, onPage = null) {
  let cursor = null;
  const allIssues = [];

  do {
    const result = await fetchIssues(since, cursor);
    allIssues.push(...result.issues);
    if (onPage) onPage(allIssues.length, result.issues);
    cursor = result.nextCursor;
  } while (cursor);

  return allIssues;
}

// Fetch labels
export async function fetchLabels() {
  let cursor = null;
  const allLabels = [];

  do {
    const afterCursor = cursor ? `, after: "${cursor}"` : '';
    const data = await gql(`
      query {
        repository(owner: "${OWNER}", name: "${REPO}") {
          labels(first: 100${afterCursor}) {
            pageInfo { hasNextPage endCursor }
            nodes { id name color }
          }
        }
      }
    `);
    const labels = data.repository.labels;
    allLabels.push(...labels.nodes);
    cursor = labels.pageInfo.hasNextPage ? labels.pageInfo.endCursor : null;
  } while (cursor);

  return allLabels;
}

// Get repo ID + label map (cached)
async function ensureRepoMeta() {
  if (_repoId && _labelMap) return;

  const data = await gql(`
    query {
      repository(owner: "${OWNER}", name: "${REPO}") {
        id
      }
    }
  `);

  // Every label, not just the first page: a label missing from the map is
  // re-created, the create fails, and the label is silently dropped.
  const labels = await fetchLabels();
  _repoId = data.repository.id;
  _labelMap = new Map(labels.map(l => [l.name, l.id]));
}

// Issue node ID by number
export async function getIssueNodeId(issueNumber) {
  const data = await gql(`
    query {
      repository(owner: "${OWNER}", name: "${REPO}") {
        issue(number: ${issueNumber}) { id }
      }
    }
  `);
  return data.repository.issue.id;
}

// An issue's labels as GitHub has them now (to edit some without dropping others set elsewhere)
export async function getIssueLabels(issueNumber) {
  const data = await gql(`
    query {
      repository(owner: "${OWNER}", name: "${REPO}") {
        issue(number: ${issueNumber}) {
          labels(first: 100) { nodes { name } }
        }
      }
    }
  `);
  return data.repository.issue.labels.nodes.map(l => l.name);
}

// An issue's labels and raw body as GitHub has them now, so a partial edit (the "Waiting on:" line)
// keeps changes made elsewhere since the page loaded, such as an agent's notes
export async function getIssueLabelsAndBody(issueNumber) {
  const data = await gql(`
    query {
      repository(owner: "${OWNER}", name: "${REPO}") {
        issue(number: ${issueNumber}) {
          body
          labels(first: 100) { nodes { name } }
        }
      }
    }
  `);
  const issue = data.repository.issue;
  return { labels: issue.labels.nodes.map(l => l.name), body: issue.body || '' };
}

// Create issue
export async function createIssue(title, body, labels) {
  await waitForMutationSlot();
  await ensureRepoMeta();

  // Auto-create missing labels
  for (const l of labels) {
    if (!_labelMap.has(l)) await ensureLabel(l);
  }
  const labelIds = labels.map(l => _labelMap.get(l)).filter(Boolean);
  const input = { repositoryId: _repoId, title };
  if (body) input.body = body;
  if (labelIds.length > 0) input.labelIds = labelIds;

  const data = await gql(`
    mutation CreateIssue($input: CreateIssueInput!) {
      createIssue(input: $input) {
        issue {
          number
          id
          title
          body
          state
          stateReason
          createdAt
          updatedAt
          parent { number }
          labels(first: 20) {
            nodes { name }
          }
        }
      }
    }
  `, { input });

  return data.createIssue.issue;
}

// Update issue
export async function updateIssue(issueNumber, updates) {
  await waitForMutationSlot();
  await ensureRepoMeta();

  const issueId = await getIssueNodeId(issueNumber);
  const input = { id: issueId };

  if (updates.title !== undefined) input.title = updates.title;
  if (updates.body !== undefined) input.body = updates.body;
  if (updates.state !== undefined) input.state = updates.state;
  if (updates.labels !== undefined) {
    // Auto-create missing labels
    for (const l of updates.labels) {
      if (!_labelMap.has(l)) await ensureLabel(l);
    }
    input.labelIds = updates.labels.map(l => _labelMap.get(l)).filter(Boolean);
  }

  const data = await gql(`
    mutation UpdateIssue($input: UpdateIssueInput!) {
      updateIssue(input: $input) {
        issue {
          number
          id
          title
          body
          state
          stateReason
          createdAt
          updatedAt
          parent { number }
          labels(first: 20) {
            nodes { name }
          }
        }
      }
    }
  `, { input });

  return data.updateIssue.issue;
}

// Add labels without replacing the others (creates missing labels)
export async function addLabels(issueId, names) {
  await waitForMutationSlot();
  await ensureRepoMeta();

  const labelIds = [];
  for (const name of names) {
    const id = _labelMap.get(name) || await ensureLabel(name);
    if (!id) throw new Error(`Could not create label ${name}`);
    labelIds.push(id);
  }

  const data = await gql(`
    mutation AddLabels($input: AddLabelsToLabelableInput!) {
      addLabelsToLabelable(input: $input) {
        labelable {
          ... on Issue {
            number
            id
            title
            body
            state
            stateReason
            createdAt
            updatedAt
            parent { number }
            labels(first: 20) {
              nodes { name }
            }
          }
        }
      }
    }
  `, { input: { labelableId: issueId, labelIds } });

  return data.addLabelsToLabelable.labelable;
}

// Remove labels without touching the others. Labels the repo doesn't have are skipped.
export async function removeLabels(issueId, names) {
  await waitForMutationSlot();
  await ensureRepoMeta();

  const labelIds = names.map(n => _labelMap.get(n)).filter(Boolean);
  if (labelIds.length === 0) return null;

  const data = await gql(`
    mutation RemoveLabels($input: RemoveLabelsFromLabelableInput!) {
      removeLabelsFromLabelable(input: $input) {
        labelable {
          ... on Issue {
            number
            id
            title
            body
            state
            stateReason
            createdAt
            updatedAt
            parent { number }
            labels(first: 20) {
              nodes { name }
            }
          }
        }
      }
    }
  `, { input: { labelableId: issueId, labelIds } });

  return data.removeLabelsFromLabelable.labelable;
}

// Comment on an issue
export async function addComment(issueId, body) {
  await waitForMutationSlot();

  const data = await gql(`
    mutation AddComment($input: AddCommentInput!) {
      addComment(input: $input) {
        commentEdge {
          node { id url }
        }
      }
    }
  `, { input: { subjectId: issueId, body } });

  return data.addComment.commentEdge.node;
}

// Close issue. stateReason: COMPLETED (done) or NOT_PLANNED (archive)
// Groups are GitHub sub-issues: the group is the parent issue, its tasks are sub-issues
export async function addSubIssue(parentId, childId) {
  await waitForMutationSlot();
  await gql(`
    mutation AddSubIssue($input: AddSubIssueInput!) {
      addSubIssue(input: $input) { issue { number } }
    }
  `, { input: { issueId: parentId, subIssueId: childId, replaceParent: true } });
}

export async function removeSubIssue(parentId, childId) {
  await waitForMutationSlot();
  await gql(`
    mutation RemoveSubIssue($input: RemoveSubIssueInput!) {
      removeSubIssue(input: $input) { issue { number } }
    }
  `, { input: { issueId: parentId, subIssueId: childId } });
}

export async function closeIssue(issueNumber, stateReason = 'COMPLETED') {
  await waitForMutationSlot();
  const issueId = await getIssueNodeId(issueNumber);
  const data = await gql(`
    mutation CloseIssue($input: CloseIssueInput!) {
      closeIssue(input: $input) {
        issue {
          number
          id
          title
          body
          state
          stateReason
          createdAt
          updatedAt
          parent { number }
          labels(first: 20) {
            nodes { name }
          }
        }
      }
    }
  `, { input: { issueId, stateReason } });
  return data.closeIssue.issue;
}

// Reopen issue
export async function reopenIssue(issueNumber) {
  return updateIssue(issueNumber, { state: 'OPEN' });
}

// Create a label if it doesn't exist
export async function ensureLabel(name, color = labelColor(name)) {
  await ensureRepoMeta();
  if (_labelMap.has(name)) return _labelMap.get(name);

  try {
    const data = await gql(`
      mutation CreateLabel($input: CreateLabelInput!) {
        createLabel(input: $input) {
          label { id name }
        }
      }
    `, {
      input: { repositoryId: _repoId, name, color },
    });
    const id = data.createLabel.label.id;
    _labelMap.set(name, id);
    return id;
  } catch {
    // Label may already exist, e.g. a due: label another client created since the map loaded
    try {
      const data = await gql(`
        query FindLabel($name: String!) {
          repository(owner: "${OWNER}", name: "${REPO}") {
            label(name: $name) { id }
          }
        }
      `, { name });
      const id = data.repository.label?.id || null;
      if (id) _labelMap.set(name, id);
      return id;
    } catch {
      return null;
    }
  }
}

// Fetch config file from .todo/config.json
export async function fetchConfig() {
  if (!_token) throw new AuthError('No token configured');
  requireRepo();

  const res = await fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/contents/.todo/config.json`,
    {
      headers: {
        'Authorization': `Bearer ${_token}`,
        'Accept': 'application/vnd.github.v3+json',
      },
    }
  );
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Config fetch failed: ${res.status}`);

  const data = await res.json();
  const content = JSON.parse(atob(data.content));
  return { config: content, sha: data.sha };
}

// The running agents the desktop publishes for Assign to agent (.todo/agents.json); null until it has.
// Read through the contents API, not the raw file, so the private repo's token works and it isn't cached.
export async function fetchAgents() {
  if (!_token) throw new AuthError('No token configured');

  const res = await fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/contents/.todo/agents.json`,
    {
      headers: {
        'Authorization': `Bearer ${_token}`,
        'Accept': 'application/vnd.github.v3+json',
      },
      cache: 'no-store',
    }
  );
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Agent list fetch failed: ${res.status}`);

  const data = await res.json();
  const bytes = Uint8Array.from(atob((data.content || '').replace(/\s/g, '')), c => c.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

// An issue's comments, oldest first (the first 100), for the agent status line
export async function fetchIssueComments(issueNumber) {
  if (!_token) throw new AuthError('No token configured');

  const res = await fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/issues/${issueNumber}/comments?per_page=100`,
    {
      headers: {
        'Authorization': `Bearer ${_token}`,
        'Accept': 'application/vnd.github.v3+json',
      },
      cache: 'no-store',
    }
  );
  if (!res.ok) throw new Error(`Comments fetch failed: ${res.status}`);
  return res.json();
}

// Push config file to .todo/config.json
export async function pushConfig(config, sha) {
  if (!_token) throw new AuthError('No token configured');
  requireRepo();

  const content = btoa(unescape(encodeURIComponent(JSON.stringify(config, null, 2))));
  const res = await fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/contents/.todo/config.json`,
    {
      method: 'PUT',
      headers: {
        'Authorization': `Bearer ${_token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        message: 'Update config (web app)',
        content,
        sha,
      }),
    }
  );
  if (!res.ok) throw new Error(`Config push failed: ${res.status}`);
  return res.json();
}

// Auth error class
export class AuthError extends Error {
  constructor(message) {
    super(message);
    this.name = 'AuthError';
  }
}

// No tasks repo set yet (not an auth failure: the saved PAT stays)
export class RepoError extends Error {
  constructor(message) {
    super(message);
    this.name = 'RepoError';
  }
}
