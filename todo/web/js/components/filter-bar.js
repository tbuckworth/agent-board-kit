// Filter bar with section/importance/urgent chips, and a row of tag chips

import { el } from '../render.js';
import { getState, updateFilters } from '../state.js';
import { filterTags, nextTagMode, withTagMode, isTagFilterActive } from '../tags.js';

const SECTIONS = ['personal', 'work', 'home', 'health', 'finance'];
const IMPORTANCES = ['high', 'medium', 'low', 'unrated'];

// `tasks`: the tasks whose tags are offered (the list's, or every open task)
export function filterBar({ tasks } = {}) {
  const { filters, issues } = getState();

  function chip(label, isActive, onClick, title) {
    return el('button', {
      className: `filter-chip ${isActive ? 'active' : ''}`,
      'aria-pressed': isActive ? 'true' : 'false',
      title: title || label,
      onClick,
    }, label);
  }

  const sectionChips = SECTIONS.map(s =>
    chip(s, filters.section === s, () =>
      updateFilters({ section: filters.section === s ? null : s })
    )
  );

  const importanceChips = IMPORTANCES.map(i =>
    chip(i, filters.importance === i, () =>
      updateFilters({ importance: filters.importance === i ? null : i }),
      i === 'unrated' ? 'No importance rating' : `Importance: ${i}`,
    )
  );

  const urgentChip = chip('urgent', filters.urgent, () =>
    updateFilters({ urgent: !filters.urgent }),
    'Urgent, or due within 14 days',
  );

  return el('div', {},
    el('div', { className: 'filter-bar' },
      ...sectionChips,
      el('span', { className: 'filter-bar-divider' }),
      ...importanceChips,
      urgentChip,
    ),
    tagFilterBar(tasks || issues.filter(i => i.state === 'OPEN')),
  );
}

// One chip per tag: click cycles all → only these → hide these → all
function tagFilterBar(tasks) {
  const tagFilter = getState().filters.tags || {};
  const tags = filterTags(tasks, tagFilter);
  if (tags.length === 0) return null;
  const chips = tags.map(tag => {
    const mode = tagFilter[tag] || null;
    const title = mode === 'only' ? `Only tasks tagged ${tag}. Click to hide them instead`
      : mode === 'hide' ? `Hiding tasks tagged ${tag}. Click to show all`
      : `Click to show only tasks tagged ${tag}`;
    return el('button', {
      className: `filter-chip tag-filter ${mode || ''}`,
      'aria-label': `Tag ${tag}: ${mode === 'only' ? 'only these' : mode === 'hide' ? 'hidden' : 'all'}`,
      title,
      onClick: () => updateFilters({ tags: withTagMode(tagFilter, tag, nextTagMode(mode)) }),
    }, mode === 'hide' ? el('span', { className: 'tag-filter-mark', 'aria-hidden': 'true' }, 'not ') : null, `#${tag}`);
  });
  const active = isTagFilterActive(tagFilter);
  return el('div', { className: 'filter-bar tag-filter-bar', role: 'group', 'aria-label': 'Tags' },
    el('span', { className: 'tag-filter-label' }, active ? 'Filtered' : 'Tags'),
    ...chips,
    active ? el('button', {
      className: 'btn btn-ghost btn-sm tag-filter-clear',
      onClick: () => updateFilters({ tags: {} }),
    }, 'Clear') : null,
  );
}
