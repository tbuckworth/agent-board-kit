// Assign to agent's choice: a new agent (the orchestrator starts one) or one already running on the desktop,
// picked from the list the desktop publishes (.todo/agents.json). Used by every Assign to agent dialog.

import { el, clear, icon } from '../render.js';
import * as api from '../api.js';
import { parseAgentsFile, filterAgents, stateWord, ago, asOf } from '../agents.js';

const FRESH_MS = 60 * 1000;
let cached = null;          // { at, file } where file is parseAgentsFile's result, or null for "none published"

async function loadAgents(force) {
  if (!force && cached && Date.now() - cached.at < FRESH_MS) return cached.file;
  const file = parseAgentsFile(await api.fetchAgents());
  cached = { at: Date.now(), file };
  return file;
}

// Returns { element, target(), check(), describe() }:
//   target()   the chosen agent { id, name } or null for a new agent
//   check()    an error message if "A running agent" is chosen with none selected, else null
//   describe() the hint under the choice, for the dialog to show
// onChange is called whenever the choice changes.
export function agentPicker(idPrefix = 'assign', onChange = () => {}) {
  let mode = 'new';
  let selected = null;
  let file;                 // undefined: not loaded yet
  let error = null;
  let loading = false;
  let query = '';

  const newChip = el('button', { type: 'button', className: 'filter-chip', role: 'radio', onClick: () => setMode('new') }, 'New agent');
  const runningChip = el('button', { type: 'button', className: 'filter-chip', role: 'radio', onClick: () => setMode('running') }, 'A running agent');
  const choice = el('div', { className: 'agent-choice', role: 'radiogroup', 'aria-label': 'Which agent' }, newChip, runningChip);

  const search = el('input', {
    className: 'input agent-search',
    type: 'search',
    id: `${idPrefix}-agent-search`,
    placeholder: 'Search agents',
    'aria-label': 'Search running agents',
    onInput: (e) => { query = e.target.value; renderList(); },
  });
  const refreshBtn = el('button', {
    type: 'button',
    className: 'btn btn-ghost btn-icon btn-sm',
    title: 'Refresh the list',
    'aria-label': 'Refresh the list',
    onClick: () => load(true),
  }, icon('refresh', 16));
  const asOfLine = el('span', { className: 'agent-asof' });
  const list = el('div', { className: 'agent-list', role: 'listbox', 'aria-label': 'Running agents' });
  const runningPane = el('div', { className: 'agent-pane', hidden: true },
    el('div', { className: 'agent-pane-head' }, search, refreshBtn),
    list,
    asOfLine,
  );

  const element = el('div', { className: 'form-group agent-picker' },
    el('label', { className: 'form-label' }, 'Agent'),
    choice,
    runningPane,
  );

  function setMode(next) {
    mode = next;
    sync();
    if (mode === 'running' && file === undefined) load(false);
    onChange();
  }

  async function load(force) {
    loading = true;
    error = null;
    renderList();
    try {
      file = await loadAgents(force);
    } catch (err) {
      error = err.message;
    }
    loading = false;
    if (selected && !(file?.agents || []).some(a => a.id === selected.id)) selected = null;
    renderList();
    onChange();
  }

  function sync() {
    newChip.classList.toggle('active', mode === 'new');
    runningChip.classList.toggle('active', mode === 'running');
    newChip.setAttribute('aria-checked', String(mode === 'new'));
    runningChip.setAttribute('aria-checked', String(mode === 'running'));
    runningPane.hidden = mode !== 'running';
  }

  function note(textContent) {
    return el('p', { className: 'form-hint agent-empty' }, textContent);
  }

  function row(agent) {
    const isSelected = selected?.id === agent.id;
    const meta = [agent.folder, stateWord(agent.state), agent.active ? `active ${ago(agent.active)}` : null]
      .filter(Boolean).join(' · ');
    return el('button', {
      type: 'button',
      className: `agent-row${isSelected ? ' selected' : ''}`,
      role: 'option',
      'aria-selected': String(isSelected),
      dataset: { id: agent.id },
      onClick: () => { selected = { id: agent.id, name: agent.name }; renderList(); onChange(); },
    },
      el('span', { className: `agent-dot ${agent.state}`, title: stateWord(agent.state) }),
      el('span', { className: 'agent-row-text' },
        el('span', { className: 'agent-row-name' }, agent.name,
          agent.cli === 'codex' ? el('span', { className: 'agent-cli' }, 'codex') : null),
        el('span', { className: 'agent-row-meta' }, meta),
        agent.note ? el('span', { className: 'agent-row-note' }, agent.note) : null,
      ),
      isSelected ? icon('check', 16) : null,
    );
  }

  function renderList() {
    clear(list);
    refreshBtn.disabled = loading;
    asOfLine.textContent = file?.updated ? `List as of ${asOf(file.updated)}` : '';
    if (loading && file === undefined) { list.appendChild(note('Loading the running agents…')); return; }
    if (error) { list.appendChild(note(`Couldn't load the running agents: ${error}`)); return; }
    if (file === null) { list.appendChild(note('No list of running agents yet. Choose New agent.')); return; }
    if (!file) return;
    if (!file.agents.length) { list.appendChild(note('No agents are running.')); return; }
    const shown = filterAgents(file.agents, query);
    if (!shown.length) { list.appendChild(note('No running agent matches.')); return; }
    let group;
    for (const agent of shown) {
      if (agent.group && agent.group !== group) list.appendChild(el('div', { className: 'agent-group' }, agent.group));
      group = agent.group;
      list.appendChild(row(agent));
    }
  }

  sync();

  return {
    element,
    target: () => (mode === 'running' ? selected : null),
    check: () => (mode === 'running' && !selected ? 'Choose a running agent, or New agent' : null),
    describe: () => (mode === 'running'
      ? (selected
        ? `The desktop sends this task to ${selected.name} within a couple of minutes. This needs the network.`
        : 'Pick the agent to hand this task to.')
      : 'The desktop starts an agent on this task within a couple of minutes. This needs the network.'),
  };
}
