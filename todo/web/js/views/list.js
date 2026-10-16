// List view — Tasks within a list

import { el, icon, mount, clear } from '../render.js';
import { getState, getFilteredIssues, getCompletedIssues, setState, getAllLists, groupForList } from '../state.js';
import { navigate } from '../router.js';
import { taskRow } from '../components/task-row.js';
import { selectableTaskList } from '../components/select-list.js';
import { filterBar } from '../components/filter-bar.js';
import { focusSection } from '../components/focus-section.js';
import { showToast } from '../components/toast.js';
import { confirm, showModal } from '../components/modal.js';
import { ratingControls } from '../components/rating-controls.js';
import * as api from '../api.js';
import * as db from '../db.js';
import { parseIssue, titleCase, buildLabels, isRatedList } from '../parser.js';

export function renderList(container, { name }) {
  const tasks = getFilteredIssues(name);
  const completedTasks = getCompletedIssues(name);
  const { filters } = getState();

  const header = el('div', { className: 'view-header' },
    el('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' } },
      el('h1', { className: 'view-title' }, titleCase(name)),
      el('button', {
        className: 'btn btn-ghost btn-sm',
        title: 'Tasks archived from this list',
        onClick: () => navigate(`#/archived/${encodeURIComponent(name)}`),
      }, 'Archived'),
      el('button', {
        className: 'btn btn-ghost btn-icon',
        title: 'Delete list',
        onClick: () => deleteList(container, name),
      }, icon('trash', 18)),
    ),
    el('p', { className: 'view-subtitle' },
      `${tasks.filter(t => t.state === 'OPEN').length} active task${tasks.filter(t => t.state === 'OPEN').length !== 1 ? 's' : ''}`,
    ),
  );

  // Add task button/form
  let addFormVisible = false;
  const addFormContainer = el('div');

  const addBtn = el('button', {
    className: 'btn btn-primary',
    onClick: () => showAddForm(),
  }, icon('plus', 16), 'New Task');

  function showAddForm() {
    const titleInput = el('input', {
      className: 'input',
      type: 'text',
      placeholder: 'Task title...',
      autofocus: true,
    });

    const bodyInput = el('textarea', {
      className: 'input textarea',
      placeholder: 'Notes (optional)',
    });

    const lists = getAllLists();
    const listInput = el('input', {
      className: 'input',
      type: 'text',
      value: name,
      placeholder: 'List name',
      list: 'add-task-lists-datalist',
    });
    const listDatalist = el('datalist', { id: 'add-task-lists-datalist' },
      ...lists.map(l => el('option', { value: l }))
    );

    const targetList = () => listInput.value.trim().toLowerCase().replace(/\s+/g, '-') || name;
    const targetRated = () => isRatedList(targetList(), groupForList(targetList()));

    // Rated lists need an importance; Urgent is off by default and writes urgency:low
    let ratingValue = { importance: null, urgency: 'low', due: null };
    const rating = ratingControls(ratingValue, (value) => { ratingValue = value; updateAddState(); }, 'add-task');
    const ratingGroup = el('div', { className: 'form-group' }, rating.element);

    const submitBtn = el('button', {
      className: 'btn btn-primary',
      onClick: () => submit(),
    }, 'Add Task');

    function updateAddState() {
      const rated = targetRated();
      ratingGroup.hidden = !rated;
      submitBtn.disabled = rated && !ratingValue.importance;
      submitBtn.title = submitBtn.disabled ? 'Choose an importance first' : '';
    }

    let submitting = false;
    async function submit() {
      const title = titleInput.value.trim();
      if (!title || submitting) return;
      const rated = targetRated();
      const value = rating.getValue();
      if (rated && !value.importance) {
        showToast('Choose an importance first');
        return;
      }
      submitting = true;
      submitBtn.disabled = true;
      const ok = await createTask(title, bodyInput.value.trim(), targetList(),
        rated ? { importance: value.importance, urgency: value.urgency || 'low', due: value.due } : {});
      submitting = false;
      if (!ok) updateAddState();
    }

    listInput.addEventListener('input', updateAddState);

    const form = el('div', { className: 'add-task-form' },
      el('div', { className: 'form-group' }, titleInput),
      el('div', { className: 'form-group' }, bodyInput),
      el('div', { className: 'form-group' },
        el('label', { className: 'form-label', style: { fontSize: 'var(--font-size-sm)' } }, 'List'),
        listInput, listDatalist,
      ),
      ratingGroup,
      el('div', { style: { display: 'flex', gap: '8px', justifyContent: 'flex-end' } },
        el('button', {
          className: 'btn btn-secondary',
          onClick: () => { clear(addFormContainer); addFormContainer.appendChild(addBtn); },
        }, 'Cancel'),
        submitBtn,
      ),
    );

    updateAddState();
    clear(addFormContainer);
    addFormContainer.appendChild(form);
    titleInput.focus();

    // Enter to submit from title field
    titleInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        submit();
      }
    });
  }

  addFormContainer.appendChild(addBtn);

  // Task rows: selectable, with groups (Shift/Cmd-click, sweep, drag by the grip onto another task)
  const openTasks = tasks.filter(t => t.state === 'OPEN');
  const taskListEl = selectableTaskList({
    tasks: openTasks,
    listName: name,
    onOpen: (task) => navigate(`#/task/${task.number}`),
    onToggle: (task) => toggleTask(task),
    rerender: () => renderList(container, { name }),
  });

  // Completed section
  let showCompleted = filters.showCompleted;
  const completedContainer = el('div');

  function renderCompletedSection() {
    clear(completedContainer);
    if (completedTasks.length === 0) return;

    const toggleBtn = el('button', {
      className: `completed-toggle ${showCompleted ? 'expanded' : ''}`,
      onClick: () => {
        showCompleted = !showCompleted;
        renderCompletedSection();
      },
    },
      el('span', { className: 'chevron' }, icon('chevronRight', 14)),
      `Completed (${completedTasks.length})`,
    );

    completedContainer.appendChild(toggleBtn);

    if (showCompleted) {
      const completedRows = completedTasks.map(t =>
        taskRow(t, {
          onToggle: (task) => toggleTask(task),
          onClick: (task) => navigate(`#/task/${task.number}`),
        })
      );
      completedContainer.appendChild(
        el('div', { className: 'task-list-container' }, ...completedRows)
      );
    }
  }

  renderCompletedSection();

  mount(container,
    header,
    // The Tasks list is the landing page
    name.toLowerCase() === 'tasks' ? focusSection(() => renderList(container, { name })) : null,
    filterBar({ tasks: getState().issues.filter(i => i.state === 'OPEN' && i.listName?.toLowerCase() === name.toLowerCase()) }),
    addFormContainer,
    el('div', { style: { marginTop: 'var(--space-4)' } }, taskListEl),
    completedContainer,
  );

  // rating: { importance, urgency, due } for rated lists, {} otherwise. Returns true when created.
  async function createTask(title, body, listName, rating = {}) {
    try {
      const labels = buildLabels({ labels: [], listName, ...rating });
      const raw = await api.createIssue(title, body || null, labels);
      const parsed = parseIssue(raw);
      const { issues } = getState();
      setState({ issues: [parsed, ...issues] });
      await db.issues.put(parsed);
      showToast('Task created');
      renderList(container, { name }); // Re-render
      return true;
    } catch (err) {
      showToast(`Error: ${err.message}`);
      return false;
    }
  }

  async function toggleTask(task) {
    const wasOpen = task.state === 'OPEN';
    // Optimistic update
    const { issues } = getState();
    const updated = issues.map(i =>
      i.number === task.number ? { ...i, state: wasOpen ? 'CLOSED' : 'OPEN' } : i
    );
    setState({ issues: updated });
    renderList(container, { name }); // Re-render

    const undoAction = () => {
      const current = getState().issues.map(i =>
        i.number === task.number ? { ...i, state: wasOpen ? 'OPEN' : 'CLOSED' } : i
      );
      setState({ issues: current });
      renderList(container, { name });
    };

    showToast(
      wasOpen ? 'Task completed' : 'Task reopened',
      { action: undoAction, actionLabel: 'Undo' }
    );

    try {
      if (wasOpen) {
        await api.closeIssue(task.number);
      } else {
        await api.reopenIssue(task.number);
      }
      // Update cache
      const final = getState().issues.find(i => i.number === task.number);
      if (final) await db.issues.put(final);
    } catch (err) {
      // Revert on error
      undoAction();
      showToast(`Error: ${err.message}`);
    }
  }
}

async function deleteList(container, name) {
  // Include ALL tasks (open and closed) so nothing lingers in cache
  const tasksInList = getState().issues.filter(
    i => i.listName?.toLowerCase() === name.toLowerCase()
  );
  const openCount = tasksInList.filter(i => i.state === 'OPEN').length;

  const message = openCount > 0
    ? `This will delete the list "${name}" and move ${openCount} active task${openCount !== 1 ? 's' : ''} to "tasks".`
    : `Delete the empty list "${name}"?`;

  const ok = await confirm({
    title: `Delete list "${name}"?`,
    message,
    confirmLabel: 'Delete',
    danger: true,
  });
  if (!ok) return;

  try {
    // Move tasks to "tasks" list (update on GitHub + local cache)
    for (const task of tasksInList) {
      const newLabels = task.labels
        .filter(l => !l.toLowerCase().startsWith('list:'))
        .concat('list:tasks');
      await api.updateIssue(task.number, { labels: newLabels });
    }

    // Update all matching issues in local state + IndexedDB
    const { issues } = getState();
    const updated = issues.map(i =>
      i.listName?.toLowerCase() === name.toLowerCase()
        ? { ...i, listName: 'tasks', labels: i.labels.filter(l => !l.toLowerCase().startsWith('list:')).concat('list:tasks') }
        : i
    );
    setState({ issues: updated });
    for (const i of updated.filter(i => i.listName === 'tasks')) {
      await db.issues.put(i);
    }

    // Remove list metadata locally (delete all case variants)
    const allMetaBefore = await db.listMeta.getAll();
    for (const m of allMetaBefore) {
      if (m.listName.toLowerCase() === name.toLowerCase()) {
        await db.listMeta.delete(m.listName);
      }
    }
    const allMeta = await db.listMeta.getAll();
    setState({ listMetadata: allMeta });

    // Push updated config to GitHub so the entry doesn't come back on sync
    try {
      const configResult = await api.fetchConfig();
      if (configResult) {
        const { config, sha } = configResult;
        config.listMetadata = (config.listMetadata || []).filter(
          m => m.listName.toLowerCase() !== name.toLowerCase()
        );
        await api.pushConfig(config, sha);
      }
    } catch (e) {
      console.warn('Config push after delete failed:', e);
    }

    showToast(`List "${name}" deleted`);
    navigate('#/');
  } catch (err) {
    showToast(`Error: ${err.message}`);
  }
}
