// The agents running on the desktop, for Assign to agent's "A running agent". The desktop publishes the
// agent board's list as `.todo/agents.json` in the tasks repo. Pure functions; the picker is in
// components/agent-picker.js.

export const AGENT_STATES = ['busy', 'background', 'waiting', 'blocked', 'idle', 'stale'];

const STATE_WORDS = {
  busy: 'working',
  background: 'waiting on a command',
  waiting: 'waiting on you',
  blocked: 'blocked',
  idle: 'idle',
  stale: 'idle for hours',
};

const ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function str(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function isoOrNull(value) {
  return str(value) && !Number.isNaN(Date.parse(value)) ? value : null;
}

// The file's agents, each checked, in the file's order; null if the file is missing or not a list of agents
export function parseAgentsFile(data) {
  if (!data || typeof data !== 'object' || !Array.isArray(data.agents)) return null;
  const agents = [];
  for (const a of data.agents) {
    if (!a || typeof a !== 'object' || !ID.test(str(a.id))) continue;
    agents.push({
      id: str(a.id).toLowerCase(),
      cli: a.cli === 'codex' ? 'codex' : 'claude',
      name: str(a.name) || str(a.folder) || str(a.id).slice(0, 8),
      state: AGENT_STATES.includes(a.state) ? a.state : 'idle',
      folder: str(a.folder),
      note: str(a.note),
      active: isoOrNull(a.active),
      group: str(a.group) || null,
    });
  }
  return { updated: isoOrNull(data.updated), agents };
}

export function stateWord(state) {
  return STATE_WORDS[state] || state;
}

// Agents whose name, folder, group, note or CLI contains every word of the query
export function filterAgents(agents, query) {
  const words = (query || '').toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return agents;
  return agents.filter(a => {
    const hay = [a.name, a.folder, a.group, a.note, a.cli].filter(Boolean).join(' ').toLowerCase();
    return words.every(w => hay.includes(w));
  });
}

// "just now", "5 min ago", "3 h ago", "2 days ago"
export function ago(iso, now = new Date()) {
  if (!iso) return '';
  const minutes = Math.round((now.getTime() - Date.parse(iso)) / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

// The time ("14:05"), with the day ("16 Oct 14:05") when not today, in the viewer's own locale
export function asOf(iso, now = new Date()) {
  if (!iso) return '';
  const d = new Date(iso);
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return time;
  return `${d.toLocaleDateString([], { day: 'numeric', month: 'short' })} ${time}`;
}

const STATUS_MARK = /^\s*<!--\s*todo-agent:(assigned|started|done|blocked|failed)\s*-->\s*/;

// The newest agent status comment (`<!-- todo-agent:KIND -->`, from the desktop or the agent) as
// { kind, text, at }, text being its first line; null if there is none. Comments come oldest first.
export function latestAgentStatus(comments) {
  let found = null;
  for (const c of comments || []) {
    const m = STATUS_MARK.exec(c?.body || '');
    if (!m) continue;
    const text = c.body.slice(m[0].length).split('\n').map(l => l.trim()).find(Boolean) || '';
    found = { kind: m[1], text, at: c.created_at || c.createdAt || null };
  }
  return found;
}
