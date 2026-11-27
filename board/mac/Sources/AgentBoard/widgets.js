// The Mail tab's widgets: the clickable options (`choices`) and interactive plots (`plot`) that agents put in their
// mail as data (the mailroom's README, "Widgets in agent mail"). This file is the board's own code, the Mail tab's
// renderer, injected into the thread document in its own content world (MailWidgets.swift); the phone page loads the
// same file (agentboard/mailweb.py serves it, web/mail/app.js draws with it). Agents send only the JSON specs; every
// node here is made with createElement and textContent, never from markup. It checks each spec by the mailroom's
// rules (agentmail/widgets.py), lengths in characters (code points), as they are.
//
// AgentBoardWidgets.render(root, widgets, host) fills each `div[data-widget=ID]` under `root` (a document or an
// element) with the widget of that id from `widgets` ([{id, kind, spec}], mail_show's messages' widgets), replacing
// the fallback the mailroom put there. A widget it cannot draw keeps its fallback, and host.problem(why) is told.
// It returns how many it drew.
//
// An option clicked shows picked in its card (a radio button, or a checkbox when several may be picked), and the host
// is told the question's answer with host.pick(answer), keeping each question's answer apart: answer is {widget (its
// id), picked (the options' indexes from 0, ascending; none once the last is unpicked), comment}, and the host reads
// the question and labels from the widget's spec itself. An option's right-click picks it as a click does (never
// unpicking it) with comment true: from a menu at the pointer, "Choose with a comment…", or at once for a host whose
// `menu` is false (the phone page, where the right-click is a long press and a menu under the finger would be one tap
// more). AgentBoardWidgets.mark(root, picks) shows the answers the host keeps ({widget id: [indexes]}) in the cards
// under `root`, none where a widget has none, and tells the host nothing.
//
// A plot clicked opens enlarged: a host with `enlarge` is given the item ({kind: 'plot', widget: its id}), and the Mac
// opens it in a window of its own (MailEnlarge.swift), where AgentBoardWidgets.drawEnlarged(document, widget) draws it
// at the window's size; any other host (the phone page's) gets a copy in a box over the page, which Escape, a click
// outside it, or its × closes. A mouse dragged across an enlarged line or scatter plot zooms it, and a double-click
// resets.
// AgentBoardWidgets.enlargeable(root, host) makes the tables and images of the mails under `root` (the Mac's thread
// document; the phone's mail is in a frame that runs no script) give `host.enlarge` {kind: 'table', html: a copy's
// markup} or {kind: 'image', src, alt} when clicked: a table with a header cell and no table in it (not a layout
// table), and an image that is not a link. It marks each with a data-abw-big attribute, which mail cannot carry either.
//
// Every element made here carries a data-abw attribute, and every style rule asks for it: mail cannot carry it (the
// mailroom keeps no data- attribute but its own data-widget), so an element of the mail's own does not take the look
// of a picked card or of the menu.
var AgentBoardWidgets = (function () {
  'use strict';

  var SVG = 'http://www.w3.org/2000/svg';
  var ID = /^s[0-9]+-[0-9]+$/;
  // Okabe and Ito's colour-blind-safe set, with a dash pattern and a marker shape for each, so no series is told
  // apart by colour alone.
  var COLOURS = ['#0072B2', '#E69F00', '#009E73', '#CC79A7', '#56B4E9', '#D55E00', '#000000', '#999999', '#882255',
                 '#44AA99'];
  var DASHES = ['', '7 4', '2 3', '9 3 2 3', '4 4', '1 3', '12 4', '6 2 2 2', '3 6', '10 2 2 2 2 2'];
  var MARKERS = ['circle', 'square', 'triangle', 'diamond', 'cross', 'circle', 'square', 'triangle', 'diamond',
                 'cross'];
  var PLOT_KINDS = ['line', 'scatter', 'bar', 'filled'];
  // A bar plot's textures at most (a series' second grouping): solid, hatched and cross-hatched (paint).
  var TEXTURES = 3;
  // A filled plot's segments and rows at most; its bars' height, the room between two of its segments and between two
  // rows, the opacity of a segment's track (its colour, pale, where it is not filled), and a percentage's padding.
  var SEGMENTS = 6, ROWS = 50, BAR = 24, PART_GAP = 3, ROW_GAP = 8, TRACK = 0.18, PAD = 4;
  var INK = '#1d2330';
  var TICK = {stroke: INK, 'stroke-width': 2, 'stroke-opacity': 0.75};   // a filled plot's tick, in its chart and legend
  var WIDTH = 640, HEIGHT = 300, MARGIN = {left: 58, right: 18, top: 12, bottom: 46};
  var MAX_TICKS = 50;               // an axis's ticks, at most, whatever its range
  var CHROME = 120;                 // px an enlarged plot's title, legend and hint take beside its chart, at most
  // A plot's text: its font (set on the chart, so that a canvas measures what is drawn), size and line height, in
  // viewBox units; the space kept between two names side by side, and between a name and its bar; and the share of
  // the width a sideways bar plot's names may take, past which they go on in more lines.
  var FONT = '-apple-system, system-ui, sans-serif', TEXT = 11, LINE = 13, GAP = 8, NAMES = 0.4;
  // the mailroom's rule (agentmail/widgets.py): no control character (C0, DEL, C1) in any text of a widget
  var CONTROL = /[\u0000-\u001f\u007f-\u009f]/;

  var STYLE = [
    '.abw-choices { margin: 12px 0; }',
    '.abw-q { font-weight: 600; margin: 0 0 8px; }',
    '.abw-opt { display: flex; gap: 12px; align-items: flex-start; border: 1.5px solid #d8dde6; border-radius: 9px;',
    '  padding: 10px 14px; margin: 8px 0; cursor: pointer; background: #ffffff; outline: none; }',
    '.abw-opt:hover, .abw-opt:focus-visible { border-color: #9aa4b5; }',
    '.abw-opt.abw-on { border-color: #2f6fde; background: #e8f0fd; }',
    '.abw-mark { width: 18px; height: 18px; border-radius: 50%; border: 2px solid #9aa4b5; flex: none; margin-top: 2px;',
    '  box-sizing: border-box; }',
    '.abw-multi .abw-mark { border-radius: 4px; }',
    '.abw-opt.abw-on .abw-mark { border-color: #2f6fde; background: #2f6fde; box-shadow: inset 0 0 0 3px #e8f0fd; }',
    '.abw-t { font-weight: 600; }',
    '.abw-d { color: #667085; font-size: 0.92em; }',
    '.abw-badge { font-size: 0.76em; font-weight: 700; color: #16794a; background: #e3f5ec; border-radius: 6px;',
    '  padding: 1px 7px; margin-left: 8px; white-space: nowrap; }',
    '.abw-default { color: #667085; font-size: 0.88em; margin-top: 6px; }',
    '.abw-menu { position: fixed; z-index: 20; padding: 4px; background: #ffffff; border: 1px solid #d0d5dd;',
    '  border-radius: 7px; box-shadow: 0 6px 20px rgba(16, 24, 40, 0.18);',
    '  font: 13px ' + FONT + '; }',
    '.abw-menu button { all: unset; display: block; padding: 4px 12px; border-radius: 4px; color: #1d2330;',
    '  cursor: default; }',
    '.abw-menu button:hover, .abw-menu button:focus { background: #2f6fde; color: #ffffff; }',
    '.abw-plot { position: relative; margin: 12px 0; }',
    '.abw-title { font-weight: 600; margin: 0 0 4px; }',
    '.abw-legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 2px 0 4px; font-size: 0.88em; }',
    '.abw-legend button { all: unset; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }',
    '.abw-legend button.abw-off { color: #b0b7c3; text-decoration: line-through; }',
    '.abw-legend span { display: inline-flex; align-items: center; gap: 6px; }',
    '.abw-plot svg.abw-chart { display: block; width: 100%; height: auto; user-select: none; -webkit-user-select: none; }',
    '.abw-tip { position: absolute; pointer-events: none; background: #1d2330; color: #ffffff; font-size: 12px;',
    '  line-height: 1.35; border-radius: 6px; padding: 6px 9px; white-space: nowrap; display: none; }',
    '.abw-hint { color: #667085; font-size: 0.8em; margin-top: 2px; }',
    '.abw-plot.abw-small svg.abw-chart { cursor: zoom-in; }',
    '.abw-big { position: fixed; inset: 0; z-index: 30; display: flex; align-items: center; justify-content: center;',
    '  background: rgba(16, 24, 40, 0.55); }',
    '.abw-big-panel { position: relative; box-sizing: border-box; max-width: 94vw; max-height: 92vh; overflow: auto;',
    '  padding: 34px 20px 16px; background: #ffffff; color: #1f2328; border-radius: 10px;',
    '  box-shadow: 0 10px 40px rgba(16, 24, 40, 0.35); font: 14px/1.45 ' + FONT + '; }',
    '.abw-big-close { all: unset; position: absolute; top: 4px; right: 12px; padding: 2px 6px; font-size: 24px;',
    '  line-height: 1; color: #667085; cursor: pointer; }',
    '.abw-big-close:hover { color: #1d2330; }'
  ].join('\n').replace(/\.abw-/g, '[data-abw].abw-') + '\n[data-abw-big] { cursor: zoom-in; }';

  function el(doc, tag, cls, text) {
    var e = doc.createElement(tag);
    e.setAttribute('data-abw', '');
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function svg(doc, tag, attrs) {
    var e = doc.createElementNS(SVG, tag);
    e.setAttribute('data-abw', '');
    for (var k in attrs) if (Object.prototype.hasOwnProperty.call(attrs, k)) e.setAttribute(k, String(attrs[k]));
    return e;
  }

  function fail(why) { throw new Error(why); }

  // obj[key] as the mailroom checks text: a string of at most `max` characters with no control character, a required
  // one not blank; an optional one may be missing.
  function text(obj, key, what, max, required) {
    var v = obj[key];
    if (v === undefined && !required) return;
    if (typeof v !== 'string' || (required && !v.trim())) fail(what + ' must be ' + (required ? 'non-blank ' : '') + 'text');
    if (Array.from(v).length > max) fail(what + ' must be at most ' + max + ' characters');
    if (CONTROL.test(v)) fail(what + ' must not hold a control character (a line break, a tab...)');
  }

  function keys(obj, allowed, what) {
    if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) fail(what + ' must be an object');
    Object.keys(obj).forEach(function (k) { if (allowed.indexOf(k) < 0) fail(what + ' has an unknown key "' + k + '"'); });
  }

  // The indexes of the cards shown picked, ascending.
  function pickedIn(cards) {
    var out = [];
    cards.forEach(function (c, i) { if (c.classList.contains('abw-on')) out.push(i); });
    return out;
  }

  function showPicked(cards, picked) {
    cards.forEach(function (c, i) {
      var on = picked.indexOf(i) >= 0;
      c.classList.toggle('abw-on', on);
      c.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }

  var openMenu = null;               // the option's right-click menu, while it is open
  var openBig = null;                // the box an enlarged plot, table or image is in, while it is open

  function closeMenu() {
    if (openMenu && openMenu.parentNode) openMenu.parentNode.removeChild(openMenu);
    openMenu = null;
  }

  function closeBig() {
    if (openBig && openBig.parentNode) openBig.parentNode.removeChild(openBig);
    openBig = null;
  }

  // Open what make(room) returns in a box over the page, room being the box's inside ({w, h} in px, its CSS's size
  // less its padding): the phone's enlarged plot. The box goes in the body, so no card clips it. Escape (setup), a
  // click outside the box or its × closes it; only one is open at a time.
  function openBox(doc, make) {
    closeBig();
    var shade = el(doc, 'div', 'abw-big'), panel = el(doc, 'div', 'abw-big-panel');
    var close = el(doc, 'button', 'abw-big-close', '\u00d7');
    close.type = 'button';
    close.setAttribute('aria-label', 'Close');
    close.addEventListener('click', closeBig);
    shade.addEventListener('click', function (e) { if (e.target === shade) closeBig(); });
    panel.appendChild(close);
    shade.appendChild(panel);
    doc.body.appendChild(shade);
    openBig = shade;
    var css = (doc.defaultView || window).getComputedStyle(panel), px = function (v) { return parseFloat(css[v]); };
    var inside = el(doc, 'div', 'abw-big-plot');
    inside.appendChild(make({w: px('maxWidth') - px('paddingLeft') - px('paddingRight'),
                             h: px('maxHeight') - px('paddingTop') - px('paddingBottom')}));
    panel.appendChild(inside);
  }

  // The document's style and its listeners, once: a click elsewhere, Escape, a scroll or the window losing focus closes
  // an option's menu, and Escape closes the enlarged box.
  function setup(doc) {
    if (doc.getElementById('abw-style')) return;
    var style = el(doc, 'style', '', STYLE);
    style.id = 'abw-style';
    (doc.head || doc.documentElement).appendChild(style);
    doc.addEventListener('mousedown', function (e) { if (!(openMenu && openMenu.contains(e.target))) closeMenu(); }, true);
    doc.addEventListener('keydown', function (e) { if (e.key === 'Escape') { closeMenu(); closeBig(); } }, true);
    doc.addEventListener('scroll', closeMenu, true);
    (doc.defaultView || window).addEventListener('blur', closeMenu);
  }

  // The markup of a copy of a mail's table, for its enlarged window: without ids, which would name the mail's.
  function tableMarkup(node) {
    var copy = node.cloneNode(true);
    copy.removeAttribute('id');
    copy.removeAttribute('data-abw-big');
    Array.prototype.forEach.call(copy.querySelectorAll('[id]'), function (e) { e.removeAttribute('id'); });
    return copy.outerHTML;
  }

  function enlargeable(root, host) {
    if (!host || typeof host.enlarge !== 'function') fail('enlargeable needs a host that enlarges');
    var doc = root.ownerDocument || root;
    setup(doc);
    Array.prototype.forEach.call(root.querySelectorAll('.mail table, .mail img'), function (node) {
      var table = node.tagName === 'TABLE';
      if (node.closest('[data-abw], [data-widget]') || node.hasAttribute('data-abw-big')) return;
      if (table ? !node.querySelector('th') || node.querySelector('table') || node.parentElement.closest('table')
        : node.closest('a')) return;
      node.setAttribute('data-abw-big', '');
      var pending = null;
      node.addEventListener('click', function (e) {
        clearTimeout(pending);
        if (e.detail > 1 || e.target.closest('a')) return;           // a double-click selects a word; a link goes
        pending = setTimeout(function () {
          var picked = doc.getSelection && doc.getSelection();
          if (picked && !picked.isCollapsed && String(picked)) return;   // a click that ends a selection
          host.enlarge(table ? {kind: 'table', html: tableMarkup(node)}
            : {kind: 'image', src: node.getAttribute('src') || '', alt: node.getAttribute('alt') || ''});
        }, 250);
      });
    });
  }

  // An option's right-click menu at the pointer, its one item `act`. It goes in the body, not the card: a mail's card
  // contains its paint, which would clip it. A click elsewhere, Escape, a scroll or the window losing focus closes it
  // (setup listens for those).
  function menu(doc, e, act) {
    closeMenu();
    var m = el(doc, 'div', 'abw-menu');
    openMenu = m;
    m.setAttribute('role', 'menu');
    var item = el(doc, 'button', '', 'Choose with a comment\u2026');
    item.type = 'button';
    item.setAttribute('role', 'menuitem');
    item.addEventListener('click', function (ev) {
      ev.preventDefault();
      closeMenu();
      act();
    });
    m.appendChild(item);
    m.style.left = e.clientX + 'px';
    m.style.top = e.clientY + 'px';
    doc.body.appendChild(m);
    var r = m.getBoundingClientRect(), view = doc.documentElement;
    if (r.right > view.clientWidth) m.style.left = Math.max(0, view.clientWidth - r.width - 4) + 'px';
    if (r.bottom > view.clientHeight) m.style.top = Math.max(0, e.clientY - r.height) + 'px';
    item.focus();
  }

  function checkChoices(spec) {
    keys(spec, ['question', 'options', 'multiple', 'default'], 'choices');
    text(spec, 'question', 'question', 200, true);
    if (!Array.isArray(spec.options) || spec.options.length < 2 || spec.options.length > 8) {
      fail('options must be a list of 2 to 8');
    }
    if (spec.multiple !== undefined && typeof spec.multiple !== 'boolean') fail('multiple must be true or false');
    text(spec, 'default', 'default', 200, false);
    var recommended = 0;
    spec.options.forEach(function (o, i) {
      var what = 'option ' + (i + 1);
      keys(o, ['label', 'detail', 'recommended'], what);
      text(o, 'label', what + "'s label", 120, true);
      text(o, 'detail', what + "'s detail", 300, false);
      if (o.recommended !== undefined && typeof o.recommended !== 'boolean') fail(what + "'s recommended must be true or false");
      if (o.recommended) recommended += 1;
    });
    if (recommended > 1 || (recommended && spec.multiple)) fail('at most one option is recommended, and none when several may be picked');
  }

  function drawChoices(doc, spec, host, id) {
    checkChoices(spec);
    var multiple = spec.multiple === true;
    var box = el(doc, 'div', 'abw-choices' + (multiple ? ' abw-multi' : ''));
    box.setAttribute('dir', 'ltr');      // laid out left to right, as the reply quotes it, whatever its place says
    box.setAttribute('role', multiple ? 'group' : 'radiogroup');
    box.appendChild(el(doc, 'p', 'abw-q', spec.question));
    var cards = spec.options.map(function (o, i) {
      var card = el(doc, 'div', 'abw-opt');
      card.setAttribute('role', multiple ? 'checkbox' : 'radio');
      card.setAttribute('aria-checked', 'false');
      card.setAttribute('tabindex', '0');
      card.appendChild(el(doc, 'span', 'abw-mark'));
      var words = el(doc, 'div');
      var title = el(doc, 'div', 'abw-t', (i + 1) + '. ' + o.label);
      if (o.recommended) title.appendChild(el(doc, 'span', 'abw-badge', 'Recommended'));
      words.appendChild(title);
      if (o.detail) words.appendChild(el(doc, 'div', 'abw-d', o.detail));
      card.appendChild(words);
      // a click picks the option (in a question of several, picks or unpicks it); with a comment it is picked
      function pick(comment) {
        var picked = pickedIn(cards), at = picked.indexOf(i);
        if (!multiple) picked = [i];
        else if (at < 0) picked.push(i);
        else if (!comment) picked.splice(at, 1);
        picked.sort(function (a, b) { return a - b; });
        showPicked(cards, picked);
        host.pick({widget: id, picked: picked, comment: comment});
      }
      card.addEventListener('click', function () { pick(false); });
      card.addEventListener('keydown', function (e) {
        if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); pick(false); }
      });
      card.addEventListener('contextmenu', function (e) {
        e.preventDefault();
        if (host.menu === false) pick(true);
        else menu(doc, e, function () { pick(true); });
      });
      box.appendChild(card);
      return card;
    });
    if (spec['default']) box.appendChild(el(doc, 'div', 'abw-default', "If you don't answer: " + spec['default']));
    return box;
  }

  function finite(v) { return typeof v === 'number' && isFinite(v); }

  // A plot's checks (the mailroom's check_plot): its kind, keys, title and image, then its kind's own.
  function checkPlot(spec) {
    if (spec === null || typeof spec !== 'object' || Array.isArray(spec)) fail('plot must be an object');
    if (PLOT_KINDS.indexOf(spec.kind) < 0) {
      fail('kind must be ' + PLOT_KINDS.slice(0, -1).join(', ') + ' or ' + PLOT_KINDS[PLOT_KINDS.length - 1]);
    }
    var filled = spec.kind === 'filled';
    keys(spec, ['title', 'kind', 'image'].concat(filled ? ['segments', 'rows', 'tick', 'x'] : ['x', 'y', 'series']),
         'plot');
    text(spec, 'title', 'title', 200, true);
    if (spec.image !== undefined) text(spec, 'image', 'image', 64, true);
    (filled ? checkFilled : checkSeries)(spec);
  }

  // A filled plot's own checks (the mailroom's _check_filled): each row a bar cut into the segments by their sizes, each
  // segment filled to a percentage, with an optional tick in each.
  function checkFilled(spec) {
    if (spec.x !== undefined) {
      keys(spec.x, ['label'], 'x');
      text(spec.x, 'label', "x's label", 80, false);
    }
    var segs = spec.segments;
    if (!Array.isArray(segs) || segs.length < 1 || segs.length > SEGMENTS) {
      fail('segments must be a list of 1 to ' + SEGMENTS);
    }
    segs.forEach(function (s, i) {
      var what = 'segment ' + (i + 1);
      keys(s, ['name', 'size'], what);
      text(s, 'name', what + "'s name", 80, true);
      if (!finite(s.size) || s.size <= 0) fail(what + "'s size must be a finite number above 0");
    });
    if (unique(segs.map(function (s) { return s.name; })).length < segs.length) fail('no two segments have the same name');
    if (spec.tick !== undefined) text(spec, 'tick', 'tick', 80, true);
    if (!Array.isArray(spec.rows) || spec.rows.length < 1 || spec.rows.length > ROWS) {
      fail('rows must be a list of 1 to ' + ROWS);
    }
    var percents = function (v) {
      return Array.isArray(v) && v.length === segs.length
        && v.every(function (p) { return p === null || (finite(p) && p >= 0 && p <= 100); });
    };
    var each = segs.length + ' numbers from 0 to 100, or null';
    spec.rows.forEach(function (r, i) {
      var what = 'row ' + (i + 1);
      keys(r, ['label', 'fill', 'group', 'tick'], what);
      text(r, 'label', what + "'s label", 80, true);
      if (r.group !== undefined) text(r, 'group', what + "'s group", 80, true);
      if (!percents(r.fill)) fail(what + "'s fill must be " + each);
      if (r.tick === undefined) return;
      if (!percents(r.tick)) fail(what + "'s tick must be " + each);
      if (spec.tick === undefined) fail(what + "'s ticks need the plot's tick, their name in the legend");
    });
    if (spec.tick !== undefined && !spec.rows.some(function (r) { return r.tick !== undefined; })) {
      fail("tick names the rows' ticks, and no row has any");
    }
    var groups = spec.rows.map(function (r) { return r.group; });
    if (groups.some(function (g) { return g !== undefined; })) {
      if (groups.indexOf(undefined) >= 0) fail('every row has a group, or none does');
      groups.forEach(function (g, i) {
        if (i && g !== groups[i - 1] && groups.slice(0, i).indexOf(g) >= 0) fail("a group's rows come one after another");
      });
    }
  }

  // A line, scatter or bar plot's own checks.
  function checkSeries(spec) {
    keys(spec.x, ['label'], 'x');
    keys(spec.y, ['label', 'min', 'max'], 'y');
    text(spec.x, 'label', "x's label", 80, false);
    text(spec.y, 'label', "y's label", 80, false);
    ['min', 'max'].forEach(function (k) {
      if (spec.y[k] !== undefined && !finite(spec.y[k])) fail("y's " + k + ' must be a finite number');
    });
    if (finite(spec.y.min) && finite(spec.y.max) && spec.y.min >= spec.y.max) fail("y's min must be below its max");
    if (!Array.isArray(spec.series) || spec.series.length < 1 || spec.series.length > 10) {
      fail('series must be a list of 1 to 10');
    }
    var points = 0, categories = null;
    spec.series.forEach(function (s, i) {
      var what = 'series ' + (i + 1);
      keys(s, ['name', 'x', 'y', 'texture'], what);
      text(s, 'name', what + "'s name", 80, true);
      if (s.texture !== undefined) {
        if (spec.kind !== 'bar') fail(what + "'s texture is for bar plots only");
        text(s, 'texture', what + "'s texture", 80, true);
      }
      if (!Array.isArray(s.x) || !Array.isArray(s.y) || s.x.length !== s.y.length) {
        fail(what + ' needs x and y lists of the same length');
      }
      points += s.x.length;
      s.y.forEach(function (v) { if (v !== null && !finite(v)) fail(what + "'s y values must be finite numbers or null"); });
      if (spec.kind === 'bar') {
        s.x.forEach(function (c) {
          if (typeof c !== 'string' || !c.length || Array.from(c).length > 80 || CONTROL.test(c)) {
            fail(what + "'s categories must be text of 1 to 80 characters, with no control character");
          }
        });
        if (categories === null) categories = s.x;
        else if (categories.join('\u0000') !== s.x.join('\u0000')) fail('every bar series must have the same categories');
      } else {
        s.x.forEach(function (v) { if (!finite(v)) fail(what + "'s x values must be finite numbers"); });
      }
    });
    if (points > 5000) fail('at most 5000 points in all');
    if (categories && categories.length > 50) fail('at most 50 categories');
    var textured = spec.series.filter(function (s) { return s.texture !== undefined; });
    if (textured.length && textured.length < spec.series.length) fail('every series has a texture, or none does');
    var pairs = spec.series.map(function (s) { return JSON.stringify([s.name, s.texture]); });
    if (textured.length && unique(pairs).length < pairs.length) fail('no two series have the same name and texture');
    if (unique(textured.map(function (s) { return s.texture; })).length > TEXTURES) {
      fail('at most ' + TEXTURES + ' textures');
    }
  }

  // The values of `list` once each, in the order first met.
  function unique(list) { return list.filter(function (v, i) { return list.indexOf(v) === i; }); }

  // The indexes in `list` that hold `v`.
  function indexes(list, v) {
    var out = [];
    list.forEach(function (w, i) { if (w === v) out.push(i); });
    return out;
  }

  // About `n` round tick values from lo to hi, at most MAX_TICKS. A range too wide for a finite step gets its two
  // ends; one narrower than the numbers' own spacing there (1e17 to 1e17 + 16) stops where a step no longer moves.
  function ticks(lo, hi, n) {
    if (hi === lo) return [lo];
    var raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw))), r = raw / mag;
    var step = (r >= 5 ? 10 : r >= 2 ? 5 : r >= 1 ? 2 : 1) * mag;
    if (!isFinite(step) || step <= 0) return [lo, hi];
    var out = [];
    for (var v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9 && out.length < MAX_TICKS; v += step) {
      out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
      if (v + step === v) break;
    }
    return out;
  }

  function fmt(v) {
    if (v === null || v === undefined) return '–';
    var a = Math.abs(v);
    if (a !== 0 && (a >= 1e6 || a < 1e-3)) return v.toExponential(2);
    return String(Number(v.toPrecision(4)));
  }

  // A function giving a text's width in viewBox units, as a plot draws it, in `weight` (undefined: the normal one).
  function measurer(doc, weight) {
    var c = doc.createElement('canvas').getContext('2d');
    c.font = (weight ? weight + ' ' : '') + TEXT + 'px ' + FONT;
    return function (s) { return c.measureText(s).width; };
  }

  // Text `s` in lines no wider than `room` by `width`, broken between words, and within a word too wide alone.
  function wrap(s, room, width) {
    var lines = [], line = '';
    s.split(' ').forEach(function (word) {
      if (line && width(line + ' ' + word) <= room) { line += ' ' + word; return; }
      if (line) lines.push(line);
      line = '';
      Array.from(word).forEach(function (ch) {
        if (line && width(line + ch) > room) { lines.push(line); line = ''; }
        line += ch;
      });
    });
    lines.push(line);
    return lines;
  }

  function marker(doc, kind, x, y, colour) {
    var r = 3.6;
    if (kind === 'square') return svg(doc, 'rect', {x: x - r, y: y - r, width: 2 * r, height: 2 * r, fill: colour});
    if (kind === 'triangle') {
      return svg(doc, 'polygon', {points: [x, y - r - 1, x - r - 0.5, y + r, x + r + 0.5, y + r].join(' '), fill: colour});
    }
    if (kind === 'diamond') {
      return svg(doc, 'polygon', {points: [x, y - r - 1, x + r + 1, y, x, y + r + 1, x - r - 1, y].join(' '), fill: colour});
    }
    if (kind === 'cross') {
      return svg(doc, 'path', {d: 'M' + (x - r) + ',' + (y - r) + 'L' + (x + r) + ',' + (y + r) + 'M' + (x + r) + ',' +
                                   (y - r) + 'L' + (x - r) + ',' + (y + r), stroke: colour, 'stroke-width': 2});
    }
    return svg(doc, 'circle', {cx: x, cy: y, r: r, fill: colour});
  }

  // The paint of a bar of `colour` in texture t (0 to TEXTURES - 1): t = 0, solid, is the colour itself; t = 1,
  // hatched, stripes of the colour at 45 degrees over a pale ground of it, and t = 2, cross-hatched, stripes both
  // ways, each a pattern put in `defs` under the id `pid`.
  function paint(doc, defs, pid, colour, t) {
    if (!t) return colour;
    var p = svg(doc, 'pattern', {id: pid, patternUnits: 'userSpaceOnUse', width: 6, height: 6,
                                 patternTransform: 'rotate(45)'});
    p.appendChild(svg(doc, 'rect', {width: 6, height: 6, fill: colour, 'fill-opacity': 0.25}));
    p.appendChild(svg(doc, 'rect', {width: 2.5, height: 6, fill: colour}));
    if (t === 2) p.appendChild(svg(doc, 'rect', {width: 6, height: 2.5, fill: colour}));
    defs.appendChild(p);
    return 'url(#' + pid + ')';
  }

  // Plot `spec` (widget `id`): in its mail when `room` is null, where a click enlarges it (enlarges); else enlarged, as
  // wide as fits room ({w, h} in px) whole, its ids apart from the mail's copy's.
  function drawPlot(doc, spec, id, room, host) {
    checkPlot(spec);
    return (spec.kind === 'filled' ? drawFilled : drawSeries)(doc, spec, id, room, host);
  }

  // A plot's frame: its box (as wide as fits `room`, when enlarged), title, legend, chart (an SVG WIDTH by `height` in
  // viewBox units), tooltip and hint (`hints`, joined).
  function frame(doc, spec, room, height, hints) {
    var box = el(doc, 'div', room ? 'abw-plot' : 'abw-plot abw-small');
    if (room) box.style.width = Math.max(320, Math.min(room.w, (room.h - CHROME) * WIDTH / height)) + 'px';
    box.appendChild(el(doc, 'p', 'abw-title', spec.title));
    var legend = el(doc, 'div', 'abw-legend');
    box.appendChild(legend);
    var chart = svg(doc, 'svg', {'class': 'abw-chart', viewBox: '0 0 ' + WIDTH + ' ' + height, role: 'img',
                                 'font-family': FONT});
    chart.setAttribute('aria-label', spec.title);
    box.appendChild(chart);
    var tip = el(doc, 'div', 'abw-tip');
    box.appendChild(tip);
    box.appendChild(el(doc, 'div', 'abw-hint', hints.join(' · ')));
    return {box: box, legend: legend, chart: chart, tip: tip};
  }

  // A click on the chart of plot `spec` (widget `id`) in its mail opens it enlarged: through `host`, else in a box over
  // the page (openBox).
  function enlarges(doc, chart, spec, id, host) {
    chart.addEventListener('click', function () {
      if (host && typeof host.enlarge === 'function') host.enlarge({kind: 'plot', widget: id});
      else openBox(doc, function (inside) { return drawPlot(doc, spec, id, inside, null); });
    });
  }

  // The point in viewBox units under mouse event `e` on `chart`, `height` high, and the chart's rectangle on screen.
  function pointIn(chart, height, e) {
    var r = chart.getBoundingClientRect();
    return {x: (e.clientX - r.left) * WIDTH / r.width, y: (e.clientY - r.top) * height / r.height, r: r};
  }

  // Show tooltip `tip` of plot box `box` beside viewBox point (`x`, `y`) of the chart at screen rectangle `r`: to its
  // right, or to its left where the right has no room.
  function showTip(box, tip, r, x, y) {
    tip.style.display = 'block';
    var scale = r.width / WIDTH, at = box.getBoundingClientRect();
    var tx = r.left - at.left + x * scale + 12;
    if (tx + tip.offsetWidth > box.clientWidth) tx = r.left - at.left + x * scale - tip.offsetWidth - 12;
    tip.style.left = Math.max(0, tx) + 'px';
    tip.style.top = r.top - at.top + y * scale + 'px';
  }

  // The name of what a plot's axis along the bottom measures (`name`), under the chart's middle from `left` to `right`
  // as far as the width allows, on its last line (the chart `height` high); `width` measures text.
  function nameAlong(doc, name, width, left, right, height) {
    var half = width(name) / 2;
    var t = svg(doc, 'text', {x: Math.max(GAP + half, Math.min(WIDTH - GAP - half, (left + right) / 2)), y: height - 6,
                              'text-anchor': 'middle', fill: '#344054'});
    t.textContent = name;
    return t;
  }

  // Lines `lines` of text as SVG text at `x` (anchored at `anchor`), the first on baseline `y`, in class `cls` (each a
  // tspan).
  function lineText(doc, lines, x, y, anchor, cls) {
    var t = svg(doc, 'text', {'class': cls, x: x, 'text-anchor': anchor, y: y});
    lines.forEach(function (line, n) {
      var span = svg(doc, 'tspan', {x: x, dy: n ? LINE : 0});
      span.textContent = line;
      t.appendChild(span);
    });
    return t;
  }

  // The first baseline of `lines` of text centred on `mid`.
  function centred(lines, mid) { return mid - (lines.length - 1) * LINE / 2 + 4; }

  // A line, scatter or bar plot (spec checked; drawPlot), where, enlarged, dragging zooms on x and a double-click resets.
  function drawSeries(doc, spec, id, room, host) {
    var ids = room ? id + '-big' : id;      // the plot's element ids (its clip and fills): one per copy in the document
    var bar = spec.kind === 'bar';
    var shown = spec.series.map(function () { return true; });
    // Each series' colour (an index into COLOURS, DASHES and MARKERS), fill (a texture, for paint) and full name, as a tooltip
    // gives it. With textures, series of one name share a colour and series of one texture a fill, each in the order
    // first named, and the full name is both; without, each series has a colour of its own, solid.
    var textured = spec.series[0].texture !== undefined;
    var hues = unique(spec.series.map(function (s) { return s.name; }));
    var fills = textured ? unique(spec.series.map(function (s) { return s.texture; })) : [];
    var hue = spec.series.map(function (s, i) { return textured ? hues.indexOf(s.name) : i; });
    var fill = spec.series.map(function (s) { return textured ? fills.indexOf(s.texture) : 0; });
    var fullName = spec.series.map(function (s) { return textured ? s.name + ', ' + s.texture : s.name; });
    var full = null, zoom = null;          // the x range of all the data, and the zoomed one
    if (!bar) {
      var xs = [];
      spec.series.forEach(function (s) { xs = xs.concat(s.x); });
      full = xs.length ? [Math.min.apply(null, xs), Math.max.apply(null, xs)] : [0, 1];
      if (full[0] === full[1]) full = [full[0] - 1, full[1] + 1];
    }
    var left = MARGIN.left, right = WIDTH - MARGIN.right, categories = bar ? spec.series[0].x : null;
    var slots = bar ? Math.max(categories.length, 1) : 0;      // a bar plot's bands: one per category
    var band = bar ? (right - left) / slots : 0;                // and each band's breadth
    // Bars whose names do not all fit side by side under them, one line each, lie sideways: a row per category, its
    // name to its left in as many lines as it takes (NAMES of the width at most), the values along the bottom.
    var width = measurer(doc);
    var sideways = bar && categories.some(function (c) { return width(c) + GAP > band; });
    var names = null;                                           // sideways: each category's name in lines
    if (sideways) {
      var room = Math.min(Math.max.apply(null, categories.map(width)), WIDTH * NAMES);
      names = categories.map(function (c) { return wrap(c, room, width); });
      var most = Math.max.apply(null, names.map(function (lines) { return lines.length; }));
      left = GAP + room + GAP;
      band = Math.max(most * LINE + 10, spec.series.length * 8 + 10, 24);
    }
    // The axes' names, written across: the one along the bottom under the plot, the one up its left side over it.
    var upName = sideways ? spec.x.label : spec.y.label, alongName = sideways ? spec.y.label : spec.x.label;
    var top = MARGIN.top + (upName ? LINE + GAP : 0);
    var bottom = top + (sideways ? band * slots : HEIGHT - MARGIN.top - MARGIN.bottom), height = bottom + MARGIN.bottom;

    var framed = frame(doc, spec, room, height, ['Hover for values', 'click a name to hide it']
      .concat(!room ? ['click the plot to enlarge it' + (bar ? '' : ', and drag across it there to zoom')]
        : bar ? [] : ['drag to zoom', 'double-click to reset']));
    var box = framed.box, legend = framed.legend, plot = framed.chart, tip = framed.tip;

    // The legend: a name per series or, with textures, a row of colours (each a series name) over a row of fills
    // (each a texture). A name clicked hides its series, which show again once none of their names is off.
    var entries = textured
      ? hues.map(function (n, h) { return {label: n, hue: h, series: indexes(hue, h)}; })
          .concat(fills.map(function (t, f) { return {label: t, fill: f, series: indexes(fill, f)}; }))
      : spec.series.map(function (s, i) { return {label: s.name, hue: i, series: [i]}; });
    var fillRow = textured ? el(doc, 'div', 'abw-legend') : null;
    if (fillRow) box.insertBefore(fillRow, legend.nextSibling);
    entries.forEach(function (key) {
      var b = el(doc, 'button');
      b.type = 'button';
      var swatch = svg(doc, 'svg', {width: 22, height: 10});
      if (key.fill === undefined) {
        swatch.appendChild(svg(doc, 'line', {x1: 1, y1: 5, x2: 21, y2: 5, stroke: COLOURS[key.hue], 'stroke-width': 3,
                                              'stroke-dasharray': DASHES[key.hue]}));
      } else {
        var defs = svg(doc, 'defs', {});
        swatch.appendChild(defs);
        swatch.appendChild(svg(doc, 'rect', {x: 1, y: 1, width: 20, height: 8, stroke: '#1d2330', 'stroke-width': 0.6,
                                              fill: paint(doc, defs, 'abw-fill-' + ids + '-key' + key.fill, '#667085',
                                                          key.fill)}));
      }
      b.appendChild(swatch);
      b.appendChild(doc.createTextNode(key.label));
      b.addEventListener('click', function () {
        key.off = !key.off;
        b.classList.toggle('abw-off', key.off);
        shown = spec.series.map(function (s, i) {
          return !entries.some(function (k) { return k.off && k.series.indexOf(i) >= 0; });
        });
        draw();
      });
      (key.fill === undefined ? legend : fillRow).appendChild(b);
    });

    // atKey: where an x value, or a bar plot's category (its index), comes along its axis; atValue: where a y value
    // comes along its axis, up the plot or, for sideways bars, across it
    var atKey, atValue, xr, yr;

    function yRange() {
      var vals = [];
      spec.series.forEach(function (s, i) {
        if (!shown[i]) return;
        s.y.forEach(function (v, j) { if (v !== null && (bar || (s.x[j] >= xr[0] && s.x[j] <= xr[1]))) vals.push(v); });
      });
      var lo = finite(spec.y.min) ? spec.y.min : Math.min.apply(null, vals.length ? vals : [0]);
      var hi = finite(spec.y.max) ? spec.y.max : Math.max.apply(null, vals.length ? vals : [1]);
      if (bar && !finite(spec.y.min)) lo = Math.min(lo, 0);
      if (lo === hi) { lo -= 1; hi += 1; }
      if (!finite(spec.y.min) || !finite(spec.y.max)) {
        var pad = (hi - lo) * 0.05;
        if (!finite(spec.y.min) && !(bar && lo === 0)) lo -= pad;
        if (!finite(spec.y.max)) hi += pad;
      }
      return [lo, hi];
    }

    function draw() {
      while (plot.firstChild) plot.removeChild(plot.firstChild);
      xr = zoom || full;
      yr = yRange();
      var from = sideways ? left : bottom, to = sideways ? right : top;
      atValue = function (v) { return from + (v - yr[0]) / (yr[1] - yr[0]) * (to - from); };
      if (bar) {
        atKey = function (k) { return (sideways ? top : left) + band * (k + 0.5); };
      } else {
        atKey = function (v) { return left + (v - xr[0]) / (xr[1] - xr[0]) * (right - left); };
      }
      var under = bottom + 20;                                  // the baseline of the text under the plot
      var grid = svg(doc, 'g', {'font-size': TEXT, fill: '#667085'});
      ticks(yr[0], yr[1], 5).forEach(function (v) {
        var at = atValue(v), t;
        if (sideways) {
          grid.appendChild(svg(doc, 'line', {x1: at, x2: at, y1: top, y2: bottom, stroke: '#e6e9ef'}));
          t = svg(doc, 'text', {x: at, y: under, 'text-anchor': 'middle'});
        } else {
          grid.appendChild(svg(doc, 'line', {x1: left, x2: right, y1: at, y2: at, stroke: '#e6e9ef'}));
          t = svg(doc, 'text', {x: left - 6, y: at + 4, 'text-anchor': 'end'});
        }
        t.textContent = fmt(v);
        grid.appendChild(t);
      });
      if (sideways) {
        names.forEach(function (lines, k) {
          grid.appendChild(lineText(doc, lines, left - GAP, centred(lines, atKey(k)), 'end', 'abw-name'));
        });
      } else if (bar) {
        categories.forEach(function (c, k) {
          var t = svg(doc, 'text', {'class': 'abw-name', x: atKey(k), y: under, 'text-anchor': 'middle'});
          t.appendChild(svg(doc, 'tspan', {})).textContent = c;
          grid.appendChild(t);
        });
      } else {
        ticks(xr[0], xr[1], 6).forEach(function (v) {
          var t = svg(doc, 'text', {x: atKey(v), y: under, 'text-anchor': 'middle'});
          t.textContent = fmt(v);
          grid.appendChild(t);
        });
      }
      grid.appendChild(sideways ? svg(doc, 'line', {x1: left, x2: left, y1: top, y2: bottom, stroke: '#9aa4b5'})
                                : svg(doc, 'line', {x1: left, x2: right, y1: bottom, y2: bottom, stroke: '#9aa4b5'}));
      if (upName) {
        var up = svg(doc, 'text', {x: GAP, y: MARGIN.top + TEXT, fill: '#344054'});
        up.textContent = upName;
        grid.appendChild(up);
      }
      if (alongName) grid.appendChild(nameAlong(doc, alongName, width, left, right, height));
      plot.appendChild(grid);

      var clip = 'abw-clip-' + ids;
      var defs = svg(doc, 'defs', {});
      var cp = svg(doc, 'clipPath', {id: clip});
      cp.appendChild(svg(doc, 'rect', {x: left, y: top - 4, width: right - left, height: bottom - top + 8}));
      defs.appendChild(cp);
      plot.appendChild(defs);
      var marks = svg(doc, 'g', {'clip-path': 'url(#' + clip + ')'});
      var visible = shown.filter(Boolean).length;
      var slot = 0;
      spec.series.forEach(function (s, i) {
        if (!shown[i]) return;
        var colour = COLOURS[hue[i]];
        if (bar) {
          var w = band * 0.8 / visible, look = paint(doc, defs, 'abw-fill-' + ids + '-' + i, colour, fill[i]);
          s.y.forEach(function (v, k) {
            if (v === null) return;
            // the bar's start and breadth along its category's band, and its ends along the values
            var at = (sideways ? top : left) + band * k + band * 0.1 + w * slot, breadth = Math.max(w - 1, 1);
            var a = atValue(Math.max(v, 0)), b = atValue(Math.min(v, 0));
            var lo = Math.min(a, b), length = Math.max(Math.abs(a - b), 0.5);
            marks.appendChild(svg(doc, 'rect', {x: sideways ? lo : at, y: sideways ? at : lo,
                                                width: sideways ? length : breadth, height: sideways ? breadth : length,
                                                fill: look,
                                                stroke: '#1d2330', 'stroke-width': 0.6,
                                                'stroke-dasharray': DASHES[hue[i]], 'class': 'abw-mark-' + i}));
          });
          slot += 1;
          return;
        }
        if (spec.kind === 'line') {
          var d = '', pen = false;
          s.x.forEach(function (x, j) {
            var v = s.y[j];
            if (v === null) { pen = false; return; }
            d += (pen ? 'L' : 'M') + atKey(x).toFixed(1) + ',' + atValue(v).toFixed(1);
            pen = true;
          });
          if (d) {
            marks.appendChild(svg(doc, 'path', {d: d, fill: 'none', stroke: colour, 'stroke-width': 2.2,
                                                'stroke-dasharray': DASHES[hue[i]], 'stroke-linejoin': 'round',
                                                'class': 'abw-line abw-mark-' + i}));
          }
        } else {
          s.x.forEach(function (x, j) {
            if (s.y[j] === null) return;
            var m = marker(doc, MARKERS[hue[i]], atKey(x), atValue(s.y[j]), colour);
            m.setAttribute('class', 'abw-mark-' + i);
            marks.appendChild(m);
          });
        }
      });
      plot.appendChild(marks);
      var any = spec.series.some(function (s, i) { return shown[i] && s.y.some(function (v) { return v !== null; }); });
      if (!any) {                          // no series with a value, or every one hidden: the axes and a note
        var empty = svg(doc, 'text', {'class': 'abw-empty', x: (left + right) / 2, y: (top + bottom) / 2,
                                      'text-anchor': 'middle', fill: '#667085', 'font-size': 13});
        empty.textContent = 'Nothing to show';
        plot.appendChild(empty);
      }
      plot.appendChild(svg(doc, 'line', {'class': 'abw-guide', x1: 0, x2: 0, y1: top, y2: bottom, stroke: '#9aa4b5',
                                         'stroke-dasharray': '3 3', visibility: 'hidden'}));
      plot.appendChild(svg(doc, 'g', {'class': 'abw-dots'}));
      plot.appendChild(svg(doc, 'rect', {'class': 'abw-band', x: 0, y: top, width: 0, height: bottom - top,
                                         fill: '#2f6fde', 'fill-opacity': 0.12, visibility: 'hidden'}));
      plot.appendChild(svg(doc, 'rect', {'class': 'abw-touch', x: left, y: top, width: right - left,
                                         height: bottom - top, fill: 'transparent'}));
    }

    function at(e) { return pointIn(plot, height, e); }

    // The nearest x to viewBox point `p` among the shown series, or its bar plot's category: {key: the x value or the
    // category's index, label, rows}.
    function nearest(p) {
      if (bar) {
        if (!categories.length) return null;
        var k = Math.floor((sideways ? p.y - top : p.x - left) / band);
        k = Math.max(0, Math.min(categories.length - 1, k));
        return {key: k, label: categories[k], rows: spec.series.map(function (s, i) {
          return shown[i] ? {i: i, name: fullName[i], y: s.y[k]} : null; }).filter(Boolean)};
      }
      var target = xr[0] + (p.x - left) / (right - left) * (xr[1] - xr[0]);
      var best = null;
      spec.series.forEach(function (s, i) {
        if (!shown[i]) return;
        s.x.forEach(function (x) {
          if (x < xr[0] || x > xr[1]) return;
          if (best === null || Math.abs(x - target) < Math.abs(best - target)) best = x;
        });
      });
      if (best === null) return null;
      return {key: best, label: fmt(best), rows: spec.series.map(function (s, i) {
        if (!shown[i]) return null;
        var j = -1;
        s.x.forEach(function (x, n) { if (j < 0 || Math.abs(x - best) < Math.abs(s.x[j] - best)) j = n; });
        return {i: i, name: fullName[i], y: s.x[j] === best ? s.y[j] : null, x: s.x[j]};
      }).filter(Boolean)};
    }

    function hover(e) {
      var p = at(e), guide = plot.querySelector('.abw-guide'), dots = plot.querySelector('.abw-dots');
      while (dots.firstChild) dots.removeChild(dots.firstChild);
      var near = p.x >= left && p.x <= right && p.y >= top && p.y <= bottom ? nearest(p) : null;
      if (!near) { guide.setAttribute('visibility', 'hidden'); tip.style.display = 'none'; return; }
      var g = atKey(near.key);                                  // the guide: across a sideways bar's row, else up at x
      [['x1', sideways ? left : g], ['x2', sideways ? right : g], ['y1', sideways ? g : top], ['y2', sideways ? g : bottom]]
        .forEach(function (kv) { guide.setAttribute(kv[0], kv[1]); });
      guide.setAttribute('visibility', 'visible');
      while (tip.firstChild) tip.removeChild(tip.firstChild);
      tip.appendChild(el(doc, 'div', '', (spec.x.label ? spec.x.label + ' ' : '') + near.label));
      near.rows.forEach(function (row) {
        tip.appendChild(el(doc, 'div', '', row.name + ': ' + fmt(row.y)));
        if (row.y !== null && !bar) dots.appendChild(svg(doc, 'circle', {cx: g, cy: atValue(row.y), r: 4.5, fill: COLOURS[hue[row.i]],
                                                                          stroke: '#ffffff', 'stroke-width': 1.5}));
      });
      // the tip goes beside the guide at the plot's top, or beside the pointer at the top of a sideways bar's row
      showTip(box, tip, p.r, sideways ? p.x : g, sideways ? g - band / 2 : top);
    }

    var dragFrom = null;
    plot.addEventListener('mousemove', function (e) {
      hover(e);
      if (dragFrom === null) return;
      var p = at(e), band = plot.querySelector('.abw-band');
      var a = Math.max(left, Math.min(dragFrom, p.x)), b = Math.min(right, Math.max(dragFrom, p.x));
      band.setAttribute('x', a);
      band.setAttribute('width', b - a);
      band.setAttribute('visibility', 'visible');
    });
    plot.addEventListener('mouseleave', function () {
      plot.querySelector('.abw-guide').setAttribute('visibility', 'hidden');
      tip.style.display = 'none';
    });
    if (!room) {
      enlarges(doc, plot, spec, id, host);
    } else if (!bar) {
      plot.addEventListener('mousedown', function (e) {
        var p = at(e);
        if (p.x >= left && p.x <= right) { dragFrom = p.x; e.preventDefault(); }
      });
      plot.addEventListener('mouseup', function (e) {
        if (dragFrom === null) return;
        var p = at(e), a = Math.min(dragFrom, p.x), b = Math.max(dragFrom, p.x);
        dragFrom = null;
        plot.querySelector('.abw-band').setAttribute('visibility', 'hidden');
        if (b - a < 6) return;
        var v = function (px) { return xr[0] + (Math.max(left, Math.min(right, px)) - left) / (right - left) * (xr[1] - xr[0]); };
        zoom = [v(a), v(b)];
        draw();
      });
      plot.addEventListener('dblclick', function () { zoom = null; draw(); });
    }
    draw();
    return box;
  }

  // Relative luminance (WCAG's) of colour `hex` (#rrggbb).
  function luminance(hex) {
    var n = parseInt(hex.slice(1), 16);
    var c = [n >> 16, (n >> 8) & 255, n & 255].map(function (v) {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }

  // The text colour that reads best on `hex`: white or INK, whichever contrasts more.
  function onColour(hex) {
    var l = luminance(hex) + 0.05;
    return 1.05 / l > l / (luminance(INK) + 0.05) ? '#ffffff' : INK;
  }

  // A percentage as a filled plot's label gives it: whole from 10, else to a tenth.
  function percent(v) { return (v >= 10 ? Math.round(v) : Math.round(v * 10) / 10) + '%'; }

  // Where a segment's percentage label goes, `w` wide, in a segment `whole` wide filled to `f`, its tick at `t` (null:
  // none), each from the segment's left end: inside the fill's end, else just past it, else past the tick, else before
  // the tick inside the fill; the first that fits in the segment clear of the tick. Null where none does.
  function labelAt(whole, f, t, w) {
    var clear = function (x) { return t === null || !(t > x - 2 && t < x + w + 2); };
    var past = t === null ? f : Math.max(f, t + 3);
    var tries = [[f - w, f >= w + 4], [f, f + w <= whole], [past, past + w <= whole],
                 [t === null ? 0 : t - w - 3, t !== null && t - w - 3 >= 0 && t <= f]];
    for (var i = 0; i < tries.length; i++) if (tries[i][1] && clear(tries[i][0])) return tries[i][0];
    return null;
  }

  // A filled plot (spec checked; drawPlot): a bar per row, cut into the segments in proportion to their sizes (PART_GAP
  // apart), each a pale track of its colour (TRACK) filled from its left end to the row's percentage, with the row's
  // tick across it and the percentage where it fits clear of the tick (labelAt: in the fill's colour's best text colour
  // inside it, in ink beside it). The segments' names head their columns, each row's label is at its left in as many
  // lines as it takes (NAMES of the width at most), and each group's name heads its rows. The legend names each segment,
  // with its size, and the tick; the tooltip gives the row's values.
  function drawFilled(doc, spec, id, room, host) {
    var segs = spec.segments, rows = spec.rows, width = measurer(doc), bold = measurer(doc, 600);
    var labelRoom = Math.min(Math.max.apply(null, rows.map(function (r) { return width(r.label); })), WIDTH * NAMES);
    var left = GAP + labelRoom + GAP, right = WIDTH - MARGIN.right;
    var total = segs.reduce(function (a, s) { return a + s.size; }, 0);
    var span = right - left - PART_GAP * (segs.length - 1);
    var parts = [], start = left;                               // each segment's left end and breadth
    segs.forEach(function (s) {
      var w = span * s.size / total;
      parts.push({x: start, w: w});
      start += w + PART_GAP;
    });
    var heads = segs.map(function (s, k) { return wrap(s.name, Math.max(parts[k].w - 4, 1), width); });
    var headLines = Math.max.apply(null, heads.map(function (h) { return h.length; }));
    // The rows' places, top to bottom: a group's name over its first row, then each row as high as its bar or its
    // label's lines, whichever is higher.
    var y = MARGIN.top + headLines * LINE + 6, layout = [];
    rows.forEach(function (r, i) {
      var lines = wrap(r.label, labelRoom, width), group = null;
      if (r.group !== undefined && (i === 0 || r.group !== rows[i - 1].group)) {
        if (i) y += ROW_GAP;
        group = {lines: wrap(r.group, WIDTH - 2 * GAP, bold), y: y};
        y += group.lines.length * LINE + 4;
      }
      var high = Math.max(BAR, lines.length * LINE);
      layout.push({row: r, lines: lines, group: group, y: y, high: high, bar: y + (high - BAR) / 2});
      y += high + ROW_GAP;
    });
    var name = spec.x && spec.x.label;
    var height = y + (name ? LINE + 8 : 0);
    var hints = ['Hover for values'].concat(room ? [] : ['click the plot to enlarge it']);
    var framed = frame(doc, spec, room, height, hints);
    var box = framed.box, chart = framed.chart, tip = framed.tip;

    segs.forEach(function (s, k) {
      var key = el(doc, 'span'), swatch = svg(doc, 'svg', {width: 22, height: 12});
      swatch.appendChild(svg(doc, 'rect', {x: 1, y: 1, width: 20, height: 10, rx: 2, fill: COLOURS[k],
                                           'fill-opacity': TRACK}));
      swatch.appendChild(svg(doc, 'rect', {x: 1, y: 1, width: 10, height: 10, rx: 2, fill: COLOURS[k]}));
      key.appendChild(swatch);
      key.appendChild(doc.createTextNode(s.name + ' (' + fmt(s.size) + ')'));
      framed.legend.appendChild(key);
    });
    if (spec.tick !== undefined) {
      var key = el(doc, 'span'), swatch = svg(doc, 'svg', {width: 6, height: 14});
      swatch.appendChild(svg(doc, 'line', Object.assign({x1: 3, x2: 3, y1: 0, y2: 14}, TICK)));
      key.appendChild(swatch);
      key.appendChild(doc.createTextNode(spec.tick));
      framed.legend.appendChild(key);
    }

    var g = svg(doc, 'g', {'font-size': TEXT, fill: '#667085'});
    heads.forEach(function (lines, k) {
      g.appendChild(lineText(doc, lines, parts[k].x + 2, MARGIN.top + TEXT, 'start', 'abw-head'));
    });
    layout.forEach(function (l) {
      if (l.group) {                                           // in the weight it was measured in
        var heading = lineText(doc, l.group.lines, GAP, l.group.y + TEXT, 'start', 'abw-group');
        heading.setAttribute('font-weight', 600);
        heading.setAttribute('fill', '#344054');
        g.appendChild(heading);
      }
      g.appendChild(lineText(doc, l.lines, left - GAP, centred(l.lines, l.y + l.high / 2), 'end', 'abw-name'));
      parts.forEach(function (p, k) {
        var colour = COLOURS[k], v = l.row.fill[k], t = l.row.tick ? l.row.tick[k] : null;
        g.appendChild(svg(doc, 'rect', {'class': 'abw-track', x: p.x, y: l.bar, width: p.w, height: BAR, rx: 3,
                                        fill: colour, 'fill-opacity': TRACK}));
        if (v) {
          g.appendChild(svg(doc, 'rect', {'class': 'abw-fill abw-mark-' + k, x: p.x, y: l.bar, height: BAR, rx: 3,
                                          width: Math.max(p.w * v / 100, 1), fill: colour}));
        }
        var tx = t === null ? null : p.w * t / 100;
        if (tx !== null) {
          g.appendChild(svg(doc, 'line', Object.assign({'class': 'abw-tick', x1: p.x + tx, x2: p.x + tx, y1: l.bar - 3,
                                                        y2: l.bar + BAR + 3}, TICK)));
        }
        if (v === null) return;
        var words = percent(v), w = width(words) + 2 * PAD, f = p.w * v / 100, x = labelAt(p.w, f, tx, w);
        if (x === null) return;
        var label = svg(doc, 'text', {'class': 'abw-value', x: p.x + x + PAD, y: l.bar + BAR / 2 + 4,
                                      fill: x + w <= f + 1 ? onColour(colour) : INK});
        label.textContent = words;
        g.appendChild(label);
      });
    });
    if (name) g.appendChild(nameAlong(doc, name, width, left, right, height));
    chart.appendChild(g);

    chart.addEventListener('mousemove', function (e) {
      var p = pointIn(chart, height, e);
      var under = layout.filter(function (l) { return p.y >= l.y - ROW_GAP / 2 && p.y < l.y + l.high + ROW_GAP / 2; })[0];
      if (!under) { tip.style.display = 'none'; return; }
      var r = under.row;
      while (tip.firstChild) tip.removeChild(tip.firstChild);
      tip.appendChild(el(doc, 'div', '', (r.group !== undefined ? r.group + ', ' : '') + r.label));
      segs.forEach(function (s, k) {
        var v = r.fill[k], t = r.tick ? r.tick[k] : null;
        tip.appendChild(el(doc, 'div', '', s.name + ': ' + (v === null ? '–' : fmt(v) + '%')
                                          + (t === null ? '' : ' (' + spec.tick + ' ' + fmt(t) + '%)')));
      });
      showTip(box, tip, p.r, p.x, under.y);
    });
    chart.addEventListener('mouseleave', function () { tip.style.display = 'none'; });
    if (!room) enlarges(doc, chart, spec, id, host);
    return box;
  }

  function render(root, widgets, host) {
    var doc = root.ownerDocument || root;
    setup(doc);
    if (!Array.isArray(widgets)) { host.problem('the widgets are not a list'); return 0; }
    var drawn = 0;
    widgets.forEach(function (w) {
      var id = w && w.id;
      if (typeof id !== 'string' || !ID.test(id)) { host.problem('a widget with an id that is not s<n>-<n> was left out'); return; }
      var place = root.querySelector('div[data-widget="' + id + '"]');
      if (!place) { host.problem('widget ' + id + ' has no place in the mail'); return; }
      try {
        var made = w.kind === 'choices' ? drawChoices(doc, w.spec, host, id)
          : w.kind === 'plot' ? drawPlot(doc, w.spec, id, null, host)
          : fail('its kind is neither choices nor plot');
        while (place.firstChild) place.removeChild(place.firstChild);
        place.appendChild(made);
        place.classList.add('abw-drawn');
        drawn += 1;
      } catch (e) {
        host.problem('widget ' + id + ' is shown as plain text: ' + e.message);
      }
    });
    return drawn;
  }

  // Plot widget `widget` ({id, kind, spec}) in `doc`'s body, enlarged to fill the window inside the body's padding (the
  // Mac's MailEnlarge window). Returns why not, or null.
  function drawEnlarged(doc, widget) {
    setup(doc);
    var css = (doc.defaultView || window).getComputedStyle(doc.body), px = function (v) { return parseFloat(css[v]); };
    try {
      if (!widget || widget.kind !== 'plot' || typeof widget.id !== 'string' || !ID.test(widget.id)) fail('it is not a plot');
      doc.body.appendChild(drawPlot(doc, widget.spec, widget.id,
        {w: doc.body.clientWidth - px('paddingLeft') - px('paddingRight'),
         h: doc.documentElement.clientHeight - px('paddingTop') - px('paddingBottom')}, null));
      return null;
    } catch (e) {
      return e.message;
    }
  }

  function mark(root, picks) {
    Array.prototype.forEach.call(root.querySelectorAll('div[data-widget].abw-drawn'), function (place) {
      var box = place.querySelector('.abw-choices'), id = place.getAttribute('data-widget');
      if (!box) return;
      var picked = Object.prototype.hasOwnProperty.call(picks, id) ? picks[id] : [];
      showPicked(Array.prototype.slice.call(box.querySelectorAll('.abw-opt')), picked);
    });
  }

  return {render: render, mark: mark, enlargeable: enlargeable, drawEnlarged: drawEnlarged};
})();
