// Task detail view — slide-over on desktop, full page on mobile

import { uuid } from '../uuid.js';
import { el, icon, mount, clear, linkify } from '../render.js';
import { getState, setState, getIssueByNumber, getAllLists, getAllSections, getAllTags, isTaskRated } from '../state.js';
import { navigate, getCurrentRoute } from '../router.js';
import { labelChip } from '../components/label-chip.js';
import { claudeCard } from '../components/claude-card.js';
import { ratingMarkers, waitingChip } from '../components/task-row.js';
import { ratingControls } from '../components/rating-controls.js';
import { showToast } from '../components/toast.js';
import { confirm, showModal } from '../components/modal.js';
import * as api from '../api.js';
import * as db from '../db.js';
import {
  buildPushBody, buildLabels, parseIssue, agentRequestBody, formatDue, localDateString,
  AGENT_REQUESTED_LABEL, AGENT_ASSIGNED_LABEL,
} from '../parser.js';
import { normaliseTag, addDays, waitingState } from '../tags.js';
import { setTags, setWaiting } from '../actions.js';
import { agentPicker } from '../components/agent-picker.js';
import { requestedToast } from '../components/assign-dialog.js';
import { latestAgentStatus } from '../agents.js';

const AGENT_KIND_WORDS = {
  assigned: 'Agent assigned', started: 'Agent', done: 'Agent done', blocked: 'Agent blocked', failed: 'Agent failed',
};
const agentStatusCache = new Map();     // "number@updatedAt" -> latestAgentStatus(...)

export function renderTask(container, { number }) {
  const task = getIssueByNumber(number);
  if (!task) {
    mount(container,
      el('div', { className: 'empty-state' },
        el('div', { className: 'empty-state-title' }, 'Task not found'),
        el('button', { className: 'btn btn-primary', onClick: () => navigate('#/') }, 'Go home'),
      ),
    );
    return;
  }

  let editing = false;
  let editState = {};

  function render() {
    // A late save must not draw over the view the user has moved to
    const route = getCurrentRoute();
    if (route && (route.view !== 'task' || route.params.number !== number)) return;
    if (editing) renderEditMode();
    else renderViewMode();
  }

  function renderViewMode() {
    const currentTask = getIssueByNumber(number);
    if (!currentTask) return;
    const isCompleted = currentTask.state === 'CLOSED';

    // Actions
    const actions = el('div', { className: 'task-detail-actions' },
      el('button', {
        className: 'btn btn-ghost',
        onClick: () => startEditing(currentTask),
        title: 'Edit',
      }, icon('edit', 16), 'Edit'),
      isCompleted
        ? el('button', {
            className: 'btn btn-ghost',
            onClick: () => toggleComplete(currentTask),
            title: 'Reopen',
          }, icon('refresh', 16), 'Reopen')
        : el('button', {
            className: 'btn btn-ghost',
            onClick: () => toggleComplete(currentTask),
            title: 'Complete',
          }, icon('checkCircle', 16), 'Complete'),
      assignButton(currentTask),
      el('button', {
        className: 'btn btn-danger',
        onClick: () => deleteTask(currentTask),
        title: 'Delete',
      }, icon('trash', 16), 'Delete'),
    );

    // Title
    const title = el('div', {
      className: `task-detail-title ${isCompleted ? 'completed' : ''}`,
    },
      el('span', { className: 'status-icon' },
        icon(isCompleted ? 'checkCircle' : 'circle', 24),
      ),
      currentTask.title,
    );

    // Labels
    const labelChips = [];
    if (currentTask.section) labelChips.push(labelChip(currentTask.section, 'section'));
    if (currentTask.listName) labelChips.push(labelChip(currentTask.listName, 'list'));
    if (waitingState(currentTask, localDateString()) !== 'none') labelChips.push(waitingChip(currentTask));
    labelChips.push(...ratingMarkers(currentTask));

    const labelsEl = el('div', { className: 'task-detail-labels' }, ...labelChips);

    // Importance, urgency and due date, for tasks in rated lists
    const ratingEl = renderRating(currentTask);

    // Body
    const bodyEl = currentTask.body
      ? el('div', { className: 'task-detail-body' }, linkify(currentTask.body))
      : null;

    // Checklist
    const checklistEl = renderChecklist(currentTask);

    // Claude notes
    const claudeEl = claudeCard(currentTask.claudeNotes);

    // Metadata
    const metaEl = el('div', { className: 'task-detail-meta' },
      `Created ${formatRelative(currentTask.createdAt)}`,
      currentTask.updatedAt ? ` · Updated ${formatRelative(currentTask.updatedAt)}` : '',
    );

    mount(container, actions, title, labelsEl, renderAgentStatus(currentTask), ratingEl, renderTags(currentTask),
      renderWaiting(currentTask), bodyEl, renderGroup(currentTask), checklistEl, claudeEl, metaEl);
  }

  // What the desktop or the agent last said on the issue (`<!-- todo-agent:KIND -->` comments), for a task
  // handed to an agent. Comments aren't cached, so it is fetched once per version of the task.
  function renderAgentStatus(currentTask) {
    const requested = currentTask.labels.includes(AGENT_REQUESTED_LABEL);
    if (!requested && !currentTask.labels.includes(AGENT_ASSIGNED_LABEL)) return null;
    const line = el('div', { className: 'agent-status' },
      el('span', { className: 'agent-status-kind' }, requested ? 'Agent requested' : 'Agent assigned'),
      el('span', { className: 'agent-status-text' },
        requested ? 'The desktop picks it up within a couple of minutes.' : ''));
    // While requested, an older status would describe the previous agent; the desktop's answer removes the label
    if (requested) return line;
    const key = `${currentTask.number}@${currentTask.updatedAt}`;
    const show = (status) => {
      if (!status) return;
      line.dataset.kind = status.kind;
      line.firstChild.textContent = AGENT_KIND_WORDS[status.kind];
      line.lastChild.textContent = status.text;
    };
    if (agentStatusCache.has(key)) {
      show(agentStatusCache.get(key));
    } else if (navigator.onLine !== false) {
      api.fetchIssueComments(currentTask.number)
        .then(comments => {
          const status = latestAgentStatus(comments);
          agentStatusCache.set(key, status);
          show(status);
        })
        .catch(() => {});
    }
    return line;
  }

  // Tags: chips with a remove button, and a field to add one, existing or new. Saved at once.
  function renderTags(currentTask) {
    const tags = currentTask.tags || [];
    const input = el('input', {
      className: 'input tag-input',
      type: 'text',
      placeholder: tags.length ? 'Add a tag' : 'Add a tag, e.g. home',
      list: 'tags-datalist',
      'aria-label': 'Add a tag',
    });
    const suggestions = el('datalist', { id: 'tags-datalist' },
      ...getAllTags().filter(t => !tags.includes(t)).map(t => el('option', { value: t })));
    const add = () => {
      const tag = normaliseTag(input.value);
      // Emptied first: redrawing removes the field, and a field removed with a changed value fires change again
      input.value = '';
      if (tag && !tags.includes(tag)) changeTags([...tags, tag]);
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); add(); }
    });
    input.addEventListener('change', () => {
      // Picking a suggestion from the list adds it straight away
      if (getAllTags().includes(normaliseTag(input.value))) add();
    });
    return el('div', { className: 'task-detail-section' },
      el('div', { className: 'task-detail-section-title' }, 'Tags'),
      el('div', { className: 'tag-editor' },
        ...tags.map(tag => el('span', { className: 'label-chip tag tag-removable' },
          `#${tag}`,
          el('button', {
            className: 'tag-remove',
            'aria-label': `Remove tag ${tag}`,
            title: `Remove tag ${tag}`,
            onClick: () => changeTags(tags.filter(t => t !== tag)),
          }, icon('close', 12)),
        )),
        el('div', { className: 'tag-add' },
          input, suggestions,
          el('button', { className: 'btn btn-ghost btn-icon btn-sm', 'aria-label': 'Add tag', onClick: add }, icon('plus', 16)),
        ),
      ),
    );
  }

  async function changeTags(tags) {
    const pending = setTags(number, tags);
    render();
    try {
      await pending;
      render();
    } catch (err) {
      render();
      showToast(`Error: ${err.message}`);
    }
  }

  // Waiting on someone: who, and when to follow up. Saved at once; the task greys out in its list.
  function renderWaiting(currentTask) {
    const section = el('div', { className: 'task-detail-section waiting-editor' });
    const titleEl = el('div', { className: 'task-detail-section-title' }, 'Waiting on');
    // An agent thinks the wait is over: the owner confirms or keeps waiting
    if (currentTask.waitingCleared && currentTask.state !== 'CLOSED') {
      section.append(titleEl, el('div', { className: 'waiting-cleared-banner', role: 'status' },
        el('p', {}, icon('checkCircle', 18), 'An agent thinks this came through (see notes)'),
        el('div', { className: 'waiting-cleared-actions' },
          el('button', {
            className: 'btn btn-secondary btn-sm',
            onClick: () => changeWaiting({ waiting: true, waitingOn: currentTask.waitingOn, followUp: currentTask.followUp }),
          }, icon('clock', 16), 'Still waiting'),
          el('button', {
            className: 'btn btn-primary btn-sm',
            onClick: () => changeWaiting({ waiting: false }),
          }, icon('check', 16), 'OK'),
        ),
      ));
      return section;
    }
    if (!currentTask.waiting) {
      section.append(titleEl, el('button', {
        className: 'btn btn-secondary btn-sm',
        disabled: currentTask.state === 'CLOSED',
        onClick: () => startWaiting(),
      }, icon('clock', 16), 'Waiting on someone…'));
      return section;
    }

    const whoInput = el('input', {
      className: 'input',
      type: 'text',
      value: currentTask.waitingOn || '',
      placeholder: 'Who? (e.g. Sam, the landlord)',
      'aria-label': 'Waiting on whom',
    });
    // Once only: redrawing removes the field, which blurs it again. Leaving the field (say, for a follow-up
    // button) saves without redrawing at once, so that button's click still lands.
    let saved = false;
    const saveWho = (redraw) => {
      const who = whoInput.value.trim() || null;
      if (saved || who === (currentTask.waitingOn || null)) return;
      saved = true;
      changeWaiting({ waiting: true, waitingOn: who, followUp: currentTask.followUp }, { redraw });
    };
    whoInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); saveWho(true); }
    });
    whoInput.addEventListener('blur', () => saveWho(false));

    const setFollowUp = (date) => changeWaiting({
      waiting: true, waitingOn: whoInput.value.trim() || currentTask.waitingOn || null, followUp: date,
    });
    const quick = [['3 days', 3], ['1 week', 7], ['2 weeks', 14]].map(([label, days]) => {
      const date = addDays(days);
      return el('button', {
        className: `filter-chip ${currentTask.followUp === date ? 'active' : ''}`,
        onClick: () => setFollowUp(date),
      }, label);
    });
    const dateInput = el('input', {
      className: 'input follow-up-date',
      type: 'date',
      value: currentTask.followUp || '',
      'aria-label': 'Follow-up date',
      onChange: (e) => setFollowUp(e.target.value || null),
    });

    section.append(
      titleEl,
      el('div', { className: 'form-group' }, whoInput),
      el('div', { className: 'form-label' }, 'Follow up'),
      el('div', { className: 'filter-bar follow-up-choices' },
        ...quick,
        dateInput,
        el('button', {
          className: `filter-chip ${currentTask.followUp ? '' : 'active'}`,
          onClick: () => setFollowUp(null),
        }, 'None'),
      ),
      el('button', {
        className: 'btn btn-ghost btn-sm',
        onClick: () => changeWaiting({ waiting: false }),
      }, icon('check', 16), 'No longer waiting'),
    );
    return section;
  }

  // Marks the task waiting (a week's follow-up by default) and puts the cursor in "Who?"
  async function startWaiting() {
    await changeWaiting({ waiting: true, waitingOn: null, followUp: addDays(7) }, { focusWho: true });
  }

  async function changeWaiting(value, { focusWho = false, redraw = true } = {}) {
    const pending = setWaiting(number, value);
    if (redraw) render();
    if (focusWho) container.querySelector('.waiting-editor input[type=text]')?.focus();
    try {
      await pending;
      // Don't redraw under someone typing who it is
      if (!container.querySelector('.waiting-editor input[type=text]:focus')) render();
    } catch (err) {
      render();
      showToast(`Error: ${err.message}`);
    }
  }

  // The group this task is in, and the tasks in it if it is a group
  function renderGroup(currentTask) {
    const all = getState().issues;
    const parent = currentTask.parentNumber != null ? getIssueByNumber(currentTask.parentNumber) : null;
    const members = all.filter(i => i.parentNumber === currentTask.number)
      .sort((a, b) => (a.state === b.state ? 0 : a.state === 'OPEN' ? -1 : 1));
    if (!parent && currentTask.parentNumber == null && members.length === 0) return null;
    const link = (t) => el('a', { href: `#/task/${t.number}`, className: t.state === 'CLOSED' ? 'done-link' : '' }, t.title);
    return el('div', { className: 'task-detail-section' },
      currentTask.parentNumber != null ? el('p', {},
        'In the group ', parent ? link(parent) : `#${currentTask.parentNumber}`) : null,
      members.length ? el('div', { className: 'task-detail-section-title' },
        `Tasks in this group (${members.filter(m => m.state === 'CLOSED').length}/${members.length} done)`) : null,
      members.length ? el('ul', { className: 'group-members' },
        ...members.map(m => el('li', {}, link(m)))) : null,
    );
  }

  function renderRating(currentTask) {
    if (!isTaskRated(currentTask)) return null;
    const controls = ratingControls(currentTask, (value, changed) => saveRating(changed), `task-${number}`);
    return el('div', { className: 'task-detail-section' },
      el('div', { className: 'task-detail-section-title' }, 'Rating'),
      currentTask.importance ? null : el('p', { className: 'form-hint rating-hint' }, 'Not rated yet'),
      controls.element,
    );
  }

  // Saves one rating change at once. Builds on the latest task, so quick successive changes all land.
  async function saveRating(changed) {
    const before = getIssueByNumber(number);
    if (!before) return;
    const next = { ...before, ...changed };
    // As on new tasks, a rated task that is not urgent says so
    if (next.importance && !next.urgency) next.urgency = 'low';
    const labels = buildLabels(next);
    setState({
      issues: getState().issues.map(i => i.number === number ? { ...next, labels } : i),
    });

    try {
      const raw = await api.updateIssue(number, { labels });
      const parsed = parseIssue(raw);
      setState({ issues: getState().issues.map(i => i.number === number ? parsed : i) });
      await db.issues.put(parsed);
      render();
    } catch (err) {
      setState({ issues: getState().issues.map(i => i.number === number ? before : i) });
      render();
      showToast(`Error: ${err.message}`);
    }
  }

  // Assign to agent: the desktop dispatches requests every couple of minutes
  function assignButton(currentTask) {
    const requested = currentTask.labels.includes(AGENT_REQUESTED_LABEL);
    const assigned = currentTask.labels.includes(AGENT_ASSIGNED_LABEL);
    const closed = currentTask.state === 'CLOSED';
    let label = 'Assign to agent';
    let title = 'Hand this task to an agent';
    if (requested) {
      label = 'Agent requested';
      title = 'Waiting for the desktop to start the agent';
    } else if (closed) {
      title = 'Reopen the task to assign it';
    } else if (assigned) {
      label = 'Assign again';
    }
    return el('button', {
      className: 'btn btn-ghost',
      disabled: closed || requested,
      title,
      onClick: () => showAssignDialog(currentTask, assigned),
    }, icon('sparkles', 16), label);
  }

  function showAssignDialog(currentTask, again) {
    const instructions = el('textarea', {
      className: 'input textarea',
      id: 'assign-instructions',
      placeholder: 'What should the agent do? (optional)',
    });

    const hint = el('p', { className: 'form-hint', style: { marginTop: '0', marginBottom: 'var(--space-3)' } });
    const picker = agentPicker('assign', () => { hint.textContent = picker.describe(); });
    hint.textContent = picker.describe();

    const assignBtn = el('button', {
      className: 'btn btn-primary',
      onClick: async () => {
        const problem = picker.check();
        if (problem) { showToast(problem); return; }
        assignBtn.disabled = true;
        const ok = await requestAgent(currentTask, instructions.value, picker.target());
        if (ok) modal.close();
        else assignBtn.disabled = false;
      },
    }, again ? 'Assign again' : 'Assign');

    const modal = showModal({
      title: again ? 'Assign again' : 'Assign to agent',
      body: [
        picker.element,
        hint,
        el('div', { className: 'form-group' },
          el('label', { className: 'form-label', htmlFor: 'assign-instructions' }, 'Instructions'),
          instructions,
        ),
      ],
      footer: [
        el('button', { className: 'btn btn-secondary', onClick: () => modal.close() }, 'Cancel'),
        assignBtn,
      ],
    });

    instructions.focus();
  }

  // Comment first, then label: the desktop only acts on the label, and reads the newest request comment
  async function requestAgent(currentTask, instructions, target = null) {
    if (navigator.onLine === false) {
      showToast('Assigning needs the network');
      return false;
    }
    try {
      const issueId = currentTask.nodeId || await api.getIssueNodeId(number);
      await api.addComment(issueId, agentRequestBody(instructions, target?.id || null));
      const raw = await api.addLabels(issueId, [AGENT_REQUESTED_LABEL]);
      const parsed = parseIssue(raw);
      setState({ issues: getState().issues.map(i => i.number === number ? parsed : i) });
      await db.issues.put(parsed);
      render();
      showToast(requestedToast(target));
      return true;
    } catch (err) {
      showToast(`Error: ${err.message}`);
      return false;
    }
  }

  function renderChecklist(currentTask) {
    const section = el('div', { className: 'task-detail-section' });

    const sectionTitle = el('div', { className: 'task-detail-section-title' }, 'Checklist');
    section.appendChild(sectionTitle);

    // Existing items
    for (const item of currentTask.checklist) {
      const row = el('div', {
        className: `checklist-item ${item.isChecked ? 'checked' : ''}`,
      },
        el('label', { className: 'task-checkbox' },
          el('input', {
            type: 'checkbox',
            checked: item.isChecked,
            'aria-label': item.text,
            onClick: () => toggleChecklistItem(currentTask, item.id),
          }),
          el('span', { className: 'checkmark' }),
        ),
        checklistText(currentTask, item),
        el('button', {
          className: 'checklist-remove btn btn-ghost btn-icon btn-sm',
          'aria-label': `Remove "${item.text}"`,
          onClick: () => removeChecklistItem(currentTask, item.id),
        }, icon('close', 14)),
      );
      section.appendChild(row);
    }

    // Add item
    const addInput = el('input', {
      className: 'input',
      type: 'text',
      placeholder: 'Add item...',
    });

    const addRow = el('div', { className: 'checklist-add' },
      addInput,
      el('button', {
        className: 'btn btn-ghost btn-icon',
        'aria-label': 'Add checklist item',
        onClick: () => addChecklistItem(currentTask, addInput),
      }, icon('plus', 16)),
    );

    addInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        addChecklistItem(currentTask, addInput);
      }
    });

    section.appendChild(addRow);
    return section;
  }

  // Item text with clickable links. Click (or Enter when focused) edits it in place;
  // linkify stops link clicks from reaching this handler.
  function checklistText(currentTask, item) {
    const textEl = el('span', {
      className: 'checklist-text editable',
      tabindex: '0',
      title: 'Click to edit',
      onClick: () => editChecklistItem(currentTask, item, textEl),
      onKeydown: (e) => {
        if (e.key === 'Enter' && e.target === textEl) {
          e.preventDefault();
          editChecklistItem(currentTask, item, textEl);
        }
      },
    }, linkify(item.text));
    return textEl;
  }

  // Enter (or leaving the field) saves; Escape, no change or an empty field cancels
  function editChecklistItem(currentTask, item, textEl) {
    const input = el('input', {
      className: 'input checklist-edit-input',
      type: 'text',
      value: item.text,
      'aria-label': 'Edit checklist item',
    });

    let finished = false;
    function finish(save) {
      if (finished) return;
      finished = true;
      const text = input.value.trim();
      if (save && text && text !== item.text) {
        const updatedChecklist = currentTask.checklist.map(ci =>
          ci.id === item.id ? { ...ci, text } : ci
        );
        updateTaskChecklist(currentTask, updatedChecklist);
      } else if (input.parentNode) {
        input.replaceWith(textEl);
      }
    }

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        finish(true);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        finish(false);
      }
    });
    input.addEventListener('blur', () => finish(true));

    textEl.replaceWith(input);
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }

  function startEditing(currentTask) {
    editing = true;
    editState = {
      title: currentTask.title,
      body: currentTask.body,
      section: currentTask.section || '',
      listName: currentTask.listName || '',
    };
    render();
  }

  function renderEditMode() {
    const titleInput = el('input', {
      className: 'input',
      type: 'text',
      value: editState.title,
      onInput: (e) => { editState.title = e.target.value; },
    });

    const bodyInput = el('textarea', {
      className: 'input textarea',
      onInput: (e) => { editState.body = e.target.value; },
    }, editState.body);

    // Section select
    const sections = getAllSections();
    const sectionInput = el('input', {
      className: 'input',
      type: 'text',
      value: editState.section,
      placeholder: 'Section (e.g. personal, work)',
      list: 'sections-datalist',
      onInput: (e) => { editState.section = e.target.value; },
    });
    const sectionDatalist = el('datalist', { id: 'sections-datalist' },
      ...sections.map(s => el('option', { value: s }))
    );

    // List select
    const lists = getAllLists();
    const listInput = el('input', {
      className: 'input',
      type: 'text',
      value: editState.listName,
      placeholder: 'List name',
      list: 'lists-datalist',
      onInput: (e) => { editState.listName = e.target.value; },
    });
    const listDatalist = el('datalist', { id: 'lists-datalist' },
      ...lists.map(l => el('option', { value: l }))
    );

    const actions = el('div', { style: { display: 'flex', gap: '8px', marginBottom: 'var(--space-4)' } },
      el('button', {
        className: 'btn btn-secondary',
        onClick: () => { editing = false; render(); },
      }, 'Cancel'),
      el('button', {
        className: 'btn btn-primary',
        onClick: () => saveEdits(),
      }, icon('check', 16), 'Save'),
    );

    mount(container,
      actions,
      el('div', { className: 'form-group' },
        el('label', { className: 'form-label' }, 'Title'),
        titleInput,
      ),
      el('div', { className: 'form-group' },
        el('label', { className: 'form-label' }, 'Notes'),
        bodyInput,
      ),
      el('div', { className: 'form-group' },
        el('label', { className: 'form-label' }, 'Section'),
        sectionInput, sectionDatalist,
      ),
      el('div', { className: 'form-group' },
        el('label', { className: 'form-label' }, 'List'),
        listInput, listDatalist,
      ),
    );

    titleInput.focus();
  }

  async function saveEdits() {
    const currentTask = getIssueByNumber(number);
    if (!currentTask) return;

    const updatedFields = {
      title: editState.title.trim(),
      body: buildPushBody(editState.body, currentTask.checklist, currentTask.claudeNotes, currentTask.waitingOn),
      labels: buildLabelsFromEdit(currentTask),
    };

    try {
      const raw = await api.updateIssue(number, updatedFields);
      const parsed = parseIssue(raw);
      const { issues } = getState();
      setState({ issues: issues.map(i => i.number === number ? parsed : i) });
      await db.issues.put(parsed);
      editing = false;
      showToast('Task updated');
      render();
    } catch (err) {
      showToast(`Error: ${err.message}`);
    }
  }

  function buildLabelsFromEdit(currentTask) {
    const editedList = editState.listName.trim();
    const listMeta = getState().listMetadata.find(meta =>
      meta.listName.toLowerCase() === editedList.toLowerCase()
    );
    const listUnchanged = currentTask.listName?.toLowerCase() === editedList.toLowerCase();
    const groupName = listMeta?.groupName ?? (listUnchanged ? currentTask.groupName : null);

    return buildLabels({
      ...currentTask,
      section: editState.section.trim() || null,
      listName: editedList || null,
    }, groupName);
  }

  async function toggleComplete(currentTask) {
    const wasOpen = currentTask.state === 'OPEN';
    const { issues } = getState();
    setState({
      issues: issues.map(i =>
        i.number === number ? { ...i, state: wasOpen ? 'CLOSED' : 'OPEN' } : i
      ),
    });
    render();

    showToast(wasOpen ? 'Task completed' : 'Task reopened', {
      action: () => {
        setState({
          issues: getState().issues.map(i =>
            i.number === number ? { ...i, state: wasOpen ? 'OPEN' : 'CLOSED' } : i
          ),
        });
        render();
      },
    });

    try {
      if (wasOpen) await api.closeIssue(number);
      else await api.reopenIssue(number);
      const updated = getState().issues.find(i => i.number === number);
      if (updated) await db.issues.put(updated);
    } catch (err) {
      setState({
        issues: getState().issues.map(i =>
          i.number === number ? { ...i, state: wasOpen ? 'OPEN' : 'CLOSED' } : i
        ),
      });
      render();
      showToast(`Error: ${err.message}`);
    }
  }

  async function deleteTask(currentTask) {
    const confirmed = await confirm({
      title: 'Delete this task?',
      message: 'This will close the issue and remove it from your lists. This cannot be undone.',
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!confirmed) return;

    try {
      // Close and remove list label
      const labelsWithoutList = currentTask.labels.filter(l => !l.startsWith('list:'));
      await api.updateIssue(number, { state: 'CLOSED', labels: labelsWithoutList });
      const { issues } = getState();
      setState({ issues: issues.filter(i => i.number !== number) });
      await db.issues.delete(number);
      showToast('Task deleted');
      navigate('#/');
    } catch (err) {
      showToast(`Error: ${err.message}`);
    }
  }

  async function toggleChecklistItem(currentTask, itemId) {
    const updatedChecklist = currentTask.checklist.map(ci =>
      ci.id === itemId ? { ...ci, isChecked: !ci.isChecked } : ci
    );
    await updateTaskChecklist(currentTask, updatedChecklist);
  }

  async function removeChecklistItem(currentTask, itemId) {
    const updatedChecklist = currentTask.checklist.filter(ci => ci.id !== itemId);
    await updateTaskChecklist(currentTask, updatedChecklist);
  }

  async function addChecklistItem(currentTask, input) {
    const text = input.value.trim();
    if (!text) return;
    const newItem = { id: uuid(), text, isChecked: false };
    const updatedChecklist = [...currentTask.checklist, newItem];
    input.value = '';
    await updateTaskChecklist(currentTask, updatedChecklist);
  }

  async function updateTaskChecklist(currentTask, checklist) {
    // Optimistic update
    const { issues } = getState();
    const updated = issues.map(i =>
      i.number === number ? { ...i, checklist } : i
    );
    setState({ issues: updated });
    render();

    try {
      const body = buildPushBody(currentTask.body, checklist, currentTask.claudeNotes, currentTask.waitingOn);
      await api.updateIssue(number, { body });
      const finalTask = getState().issues.find(i => i.number === number);
      if (finalTask) await db.issues.put(finalTask);
    } catch (err) {
      // Revert
      setState({
        issues: getState().issues.map(i =>
          i.number === number ? { ...i, checklist: currentTask.checklist } : i
        ),
      });
      render();
      showToast(`Error: ${err.message}`);
    }
  }

  render();
}

function formatRelative(dateStr) {
  if (!dateStr) return '';
  const date = new Date(dateStr);
  const now = new Date();
  const diff = now - date;
  const mins = Math.floor(diff / 60000);
  const hours = Math.floor(diff / 3600000);
  const days = Math.floor(diff / 86400000);

  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  if (hours < 24) return `${hours}h ago`;
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString();
}
