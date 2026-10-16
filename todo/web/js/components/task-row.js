// Task row component

import { el, icon } from '../render.js';
import { labelChip } from './label-chip.js';
import { isEffectivelyUrgent, formatDue, daysUntil, localDateString } from '../parser.js';
import { waitingState, waitingText } from '../tags.js';

// Optional, for list views: `row` from groups.buildRows ({ depth, childCount, doneCount, expanded }) shows the
// task as a group with a fold chevron (onFold); `selected` marks it; `grip` adds a drag handle.
export function taskRow(task, { onToggle, onClick, row = null, onFold = null, selected = false, grip = false }) {
  const isCompleted = task.state === 'CLOSED';

  const checkbox = el('label', { className: 'task-checkbox' },
    el('input', {
      type: 'checkbox',
      checked: isCompleted,
      'aria-label': `Mark "${task.title}" as ${isCompleted ? 'incomplete' : 'complete'}`,
      onClick: (e) => {
        e.stopPropagation();
        onToggle(task);
      },
    }),
    el('span', { className: 'checkmark' }),
  );

  const metaChips = [];
  if (task.importance === 'high' || task.importance === 'low') {
    metaChips.push(el('span', {
      className: `importance-dot ${task.importance}`,
      title: `Importance: ${task.importance}`,
    }));
  }
  metaChips.push(...ratingMarkers(task));
  if (task.section) {
    metaChips.push(labelChip(task.section, 'section'));
  }
  for (const tag of task.tags || []) metaChips.push(labelChip(`#${tag}`, 'tag'));

  // Waiting on someone: greyed until the follow-up date, then flagged
  const waiting = waitingState(task, localDateString());
  if (waiting !== 'none') metaChips.unshift(waitingChip(task));

  const isGroup = row && row.childCount > 0;
  const content = el('div', { className: 'task-content' },
    el('div', { className: 'task-title' },
      task.title,
      isGroup ? el('span', { className: 'group-count', title: `${row.doneCount} of ${row.childCount} done` },
        ` ${row.doneCount}/${row.childCount}`) : null,
    ),
    metaChips.length > 0 ? el('div', { className: 'task-meta' }, ...metaChips) : null,
  );

  const chevron = el('span', { className: 'task-chevron' }, icon('chevronRight', 16));

  const fold = isGroup ? el('button', {
    className: `group-fold ${row.expanded ? 'expanded' : ''}`,
    'aria-label': `${row.expanded ? 'Collapse' : 'Expand'} group "${task.title}"`,
    'aria-expanded': String(row.expanded),
    onClick: (e) => { e.stopPropagation(); onFold?.(task); },
  }, icon('chevronRight', 14)) : null;

  const handle = grip ? el('span', {
    className: 'task-grip',
    title: 'Drag onto another task to group them',
    'aria-hidden': 'true',
  }, icon('grip', 14)) : null;

  const element = el('div', {
    className: `task-row ${isCompleted ? 'completed' : ''} ${isGroup ? 'is-group' : ''} ${selected ? 'selected' : ''} ${waiting === 'waiting' ? 'waiting' : ''}`,
    onClick: (e) => onClick(task, e),
    role: 'button',
    tabindex: '0',
    'aria-label': task.title,
    'aria-selected': selected ? 'true' : 'false',
    dataset: { number: String(task.number) },
    style: row?.depth ? { paddingLeft: `calc(var(--space-4) + ${row.depth * 28}px)` } : {},
  }, handle, fold, checkbox, content, chevron);

  // Keyboard activation
  element.addEventListener('keydown', (e) => {
    if (e.target !== element) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onClick(task, e);
    }
  });

  return element;
}

// "Waiting on Sam · follow up 23 Oct"; "Follow up with Sam" once that date has come; "Came through? Sam" when an
// agent thinks the wait is over
export function waitingChip(task) {
  const today = localDateString();
  const state = waitingState(task, today);
  const flagged = state === 'follow-up' || state === 'cleared';
  return el('span', {
    className: `label-chip waiting ${flagged ? 'follow-up' : ''}`,
    title: state === 'cleared' ? 'An agent thinks this came through; open the task to confirm'
      : task.followUp ? `Follow up ${task.followUp}` : 'Waiting on someone',
  }, waitingText(task, today, formatDue));
}

// Urgent marker and "due 12 Oct" text, shared with the task detail view
export function ratingMarkers(task) {
  const markers = [];
  if (task.state !== 'CLOSED' && isEffectivelyUrgent(task)) {
    markers.push(el('span', { className: 'label-chip urgent', title: 'Urgent, or due within 14 days' }, 'urgent'));
  }
  if (task.due) {
    const overdue = task.state !== 'CLOSED' && daysUntil(task.due) < 0;
    markers.push(el('span', {
      className: `label-chip due ${overdue ? 'overdue' : ''}`,
      title: overdue ? `Overdue: ${task.due}` : `Due ${task.due}`,
    }, `due ${formatDue(task.due)}`));
  }
  return markers;
}
