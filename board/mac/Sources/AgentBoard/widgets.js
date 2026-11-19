// The Mail tab's widgets: the clickable options (`choices`) and interactive plots (`plot`) that agents put in their
// mail as data (the mailroom's README, "Widgets in agent mail"). This file is the board's own code, the Mail tab's
// renderer, injected into the thread document in its own content world (MailWidgets.swift). Agents send only the
// JSON specs; every node here is made with createElement and textContent, never from markup. It checks each spec by
// the mailroom's rules (agentmail/widgets.py), lengths in characters (code points), as they are.
//
// AgentBoardWidgets.render(root, widgets, host) fills each `div[data-widget=ID]` under `root` (a document or an
// element) with the widget of that id from `widgets` ([{id, kind, spec}], mail_show's messages' widgets), replacing
// the fallback the mailroom put there. A widget it cannot draw keeps its fallback, and host.problem(why) is told.
// A choice calls host.choose(text): the reply's first line ("Option 2: ...", "" when nothing is picked).
// It returns how many it drew.
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
  var WIDTH = 640, HEIGHT = 300, MARGIN = {left: 58, right: 18, top: 12, bottom: 46};
  var MAX_TICKS = 50;               // an axis's ticks, at most, whatever its range
  // the mailroom's rule (agentmail/widgets.py): no control character (C0, DEL, C1) in any text of a widget
  var CONTROL = /[\u0000-\u001f\u007f-\u009f]/;
  // and a choice's line is one line, whatever its labels hold: these become spaces in it
  var LINE_BREAKS = /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g;

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
    '.abw-plot { position: relative; margin: 12px 0; }',
    '.abw-title { font-weight: 600; margin: 0 0 4px; }',
    '.abw-legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 2px 0 4px; font-size: 0.88em; }',
    '.abw-legend button { all: unset; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }',
    '.abw-legend button.abw-off { color: #b0b7c3; text-decoration: line-through; }',
    '.abw-plot svg.abw-chart { display: block; width: 100%; height: auto; user-select: none; -webkit-user-select: none; }',
    '.abw-tip { position: absolute; pointer-events: none; background: #1d2330; color: #ffffff; font-size: 12px;',
    '  line-height: 1.35; border-radius: 6px; padding: 6px 9px; white-space: nowrap; display: none; }',
    '.abw-hint { color: #667085; font-size: 0.8em; margin-top: 2px; }'
  ].join('\n');

  function el(doc, tag, cls, text) {
    var e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function svg(doc, tag, attrs) {
    var e = doc.createElementNS(SVG, tag);
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

  // "1", "1 and 3", "1, 2 and 3"
  function joined(items) {
    return items.length < 2 ? items.join('') : items.slice(0, -1).join(', ') + ' and ' + items[items.length - 1];
  }

  /// The reply's first line for the options picked (their indexes, from 0): "" for none, "Option 2: LABEL" for one,
  /// "Options 1 and 3: LABEL; LABEL" for several. One line, always: a line break in a label becomes a space.
  function choiceText(spec, picked) {
    var sorted = picked.slice().sort(function (a, b) { return a - b; });
    if (!sorted.length) return '';
    var nums = sorted.map(function (i) { return String(i + 1); });
    var labels = sorted.map(function (i) { return spec.options[i].label.replace(LINE_BREAKS, ' '); });
    return (sorted.length === 1 ? 'Option ' : 'Options ') + joined(nums) + ': ' + labels.join('; ');
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

  function drawChoices(doc, spec, host) {
    checkChoices(spec);
    var multiple = spec.multiple === true;
    var picked = [];
    var box = el(doc, 'div', 'abw-choices' + (multiple ? ' abw-multi' : ''));
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
      function pick() {
        if (multiple) {
          var at = picked.indexOf(i);
          if (at < 0) picked.push(i); else picked.splice(at, 1);
        } else {
          picked = [i];
        }
        cards.forEach(function (c, j) {
          var on = picked.indexOf(j) >= 0;
          c.classList.toggle('abw-on', on);
          c.setAttribute('aria-checked', on ? 'true' : 'false');
        });
        host.choose(choiceText(spec, picked));
      }
      card.addEventListener('click', pick);
      card.addEventListener('keydown', function (e) {
        if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); pick(); }
      });
      box.appendChild(card);
      return card;
    });
    if (spec['default']) box.appendChild(el(doc, 'div', 'abw-default', "If you don't answer: " + spec['default']));
    return box;
  }

  function finite(v) { return typeof v === 'number' && isFinite(v); }

  function checkPlot(spec) {
    keys(spec, ['title', 'kind', 'x', 'y', 'series', 'image'], 'plot');
    text(spec, 'title', 'title', 200, true);
    if (['line', 'scatter', 'bar'].indexOf(spec.kind) < 0) fail('kind must be line, scatter or bar');
    keys(spec.x, ['label'], 'x');
    keys(spec.y, ['label', 'min', 'max'], 'y');
    text(spec.x, 'label', "x's label", 80, false);
    text(spec.y, 'label', "y's label", 80, false);
    ['min', 'max'].forEach(function (k) {
      if (spec.y[k] !== undefined && !finite(spec.y[k])) fail("y's " + k + ' must be a finite number');
    });
    if (finite(spec.y.min) && finite(spec.y.max) && spec.y.min >= spec.y.max) fail("y's min must be below its max");
    if (spec.image !== undefined) text(spec, 'image', 'image', 64, true);
    if (!Array.isArray(spec.series) || spec.series.length < 1 || spec.series.length > 10) {
      fail('series must be a list of 1 to 10');
    }
    var points = 0, categories = null;
    spec.series.forEach(function (s, i) {
      var what = 'series ' + (i + 1);
      keys(s, ['name', 'x', 'y'], what);
      text(s, 'name', what + "'s name", 80, true);
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

  function drawPlot(doc, spec, id) {
    checkPlot(spec);
    var bar = spec.kind === 'bar';
    var shown = spec.series.map(function () { return true; });
    var full = null, zoom = null;          // the x range of all the data, and the zoomed one
    if (!bar) {
      var xs = [];
      spec.series.forEach(function (s) { xs = xs.concat(s.x); });
      full = xs.length ? [Math.min.apply(null, xs), Math.max.apply(null, xs)] : [0, 1];
      if (full[0] === full[1]) full = [full[0] - 1, full[1] + 1];
    }
    var box = el(doc, 'div', 'abw-plot');
    box.appendChild(el(doc, 'p', 'abw-title', spec.title));
    var legend = el(doc, 'div', 'abw-legend');
    box.appendChild(legend);
    var plot = svg(doc, 'svg', {'class': 'abw-chart', viewBox: '0 0 ' + WIDTH + ' ' + HEIGHT, role: 'img'});
    plot.setAttribute('aria-label', spec.title);
    box.appendChild(plot);
    var tip = el(doc, 'div', 'abw-tip');
    box.appendChild(tip);
    box.appendChild(el(doc, 'div', 'abw-hint', bar ? 'Hover for values · click a name to hide it'
      : 'Hover for values · click a name to hide it · drag to zoom · double-click to reset'));

    spec.series.forEach(function (s, i) {
      var b = doc.createElement('button');
      b.type = 'button';
      var swatch = svg(doc, 'svg', {width: 22, height: 10});
      swatch.appendChild(svg(doc, 'line', {x1: 1, y1: 5, x2: 21, y2: 5, stroke: COLOURS[i], 'stroke-width': 3,
                                            'stroke-dasharray': DASHES[i]}));
      b.appendChild(swatch);
      b.appendChild(doc.createTextNode(s.name));
      b.addEventListener('click', function () {
        shown[i] = !shown[i];
        b.classList.toggle('abw-off', !shown[i]);
        draw();
      });
      legend.appendChild(b);
    });

    var left = MARGIN.left, right = WIDTH - MARGIN.right, top = MARGIN.top, bottom = HEIGHT - MARGIN.bottom;
    var sx, sy, xr, yr, categories = bar ? spec.series[0].x : null;
    var slots = bar ? Math.max(categories.length, 1) : 0;      // a bar plot's bands: one per category

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
      sy = function (v) { return bottom - (v - yr[0]) / (yr[1] - yr[0]) * (bottom - top); };
      if (bar) {
        var band = (right - left) / slots;
        sx = function (k) { return left + band * (k + 0.5); };
      } else {
        sx = function (v) { return left + (v - xr[0]) / (xr[1] - xr[0]) * (right - left); };
      }
      var grid = svg(doc, 'g', {'font-size': 11, fill: '#667085'});
      ticks(yr[0], yr[1], 5).forEach(function (v) {
        grid.appendChild(svg(doc, 'line', {x1: left, x2: right, y1: sy(v), y2: sy(v), stroke: '#e6e9ef'}));
        var t = svg(doc, 'text', {x: left - 6, y: sy(v) + 4, 'text-anchor': 'end'});
        t.textContent = fmt(v);
        grid.appendChild(t);
      });
      if (bar) {
        categories.forEach(function (c, k) {
          var t = svg(doc, 'text', {x: sx(k), y: bottom + 16, 'text-anchor': 'middle'});
          t.textContent = c.length > 14 && categories.length > 6 ? c.slice(0, 13) + '…' : c;
          grid.appendChild(t);
        });
      } else {
        ticks(xr[0], xr[1], 6).forEach(function (v) {
          var t = svg(doc, 'text', {x: sx(v), y: bottom + 16, 'text-anchor': 'middle'});
          t.textContent = fmt(v);
          grid.appendChild(t);
        });
      }
      grid.appendChild(svg(doc, 'line', {x1: left, x2: right, y1: bottom, y2: bottom, stroke: '#9aa4b5'}));
      if (spec.x.label) {
        var xl = svg(doc, 'text', {x: (left + right) / 2, y: HEIGHT - 6, 'text-anchor': 'middle', fill: '#344054'});
        xl.textContent = spec.x.label;
        grid.appendChild(xl);
      }
      if (spec.y.label) {
        var yl = svg(doc, 'text', {x: 0, y: 0, 'text-anchor': 'middle', fill: '#344054',
                                   transform: 'translate(13,' + ((top + bottom) / 2) + ') rotate(-90)'});
        yl.textContent = spec.y.label;
        grid.appendChild(yl);
      }
      plot.appendChild(grid);

      var clip = 'abw-clip-' + id;
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
        var colour = COLOURS[i];
        if (bar) {
          var band = (right - left) / slots, w = band * 0.8 / visible;
          s.y.forEach(function (v, k) {
            if (v === null) return;
            var x0 = left + band * k + band * 0.1 + w * slot, y0 = sy(Math.max(v, 0)), y1 = sy(Math.min(v, 0));
            marks.appendChild(svg(doc, 'rect', {x: x0, y: y0, width: Math.max(w - 1, 1), height: Math.max(y1 - y0, 0.5),
                                                fill: colour, stroke: '#1d2330', 'stroke-width': 0.6,
                                                'stroke-dasharray': DASHES[i], 'class': 'abw-mark-' + i}));
          });
          slot += 1;
          return;
        }
        if (spec.kind === 'line') {
          var d = '', pen = false;
          s.x.forEach(function (x, j) {
            var v = s.y[j];
            if (v === null) { pen = false; return; }
            d += (pen ? 'L' : 'M') + sx(x).toFixed(1) + ',' + sy(v).toFixed(1);
            pen = true;
          });
          if (d) {
            marks.appendChild(svg(doc, 'path', {d: d, fill: 'none', stroke: colour, 'stroke-width': 2.2,
                                                'stroke-dasharray': DASHES[i], 'stroke-linejoin': 'round',
                                                'class': 'abw-line abw-mark-' + i}));
          }
        } else {
          s.x.forEach(function (x, j) {
            if (s.y[j] === null) return;
            var m = marker(doc, MARKERS[i], sx(x), sy(s.y[j]), colour);
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

    // The point in viewBox units under a mouse event.
    function at(e) {
      var r = plot.getBoundingClientRect();
      return {x: (e.clientX - r.left) * WIDTH / r.width, y: (e.clientY - r.top) * HEIGHT / r.height, r: r};
    }

    // The nearest x to viewBox x `px` among the shown series: [x value (or category index), its rows].
    function nearest(px) {
      if (bar) {
        if (!categories.length) return null;
        var k = Math.floor((px - left) / ((right - left) / slots));
        k = Math.max(0, Math.min(categories.length - 1, k));
        return {key: k, label: categories[k], rows: spec.series.map(function (s, i) {
          return shown[i] ? {i: i, name: s.name, y: s.y[k]} : null; }).filter(Boolean)};
      }
      var target = xr[0] + (px - left) / (right - left) * (xr[1] - xr[0]);
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
        return {i: i, name: s.name, y: s.x[j] === best ? s.y[j] : null, x: s.x[j]};
      }).filter(Boolean)};
    }

    function hover(e) {
      var p = at(e), guide = plot.querySelector('.abw-guide'), dots = plot.querySelector('.abw-dots');
      while (dots.firstChild) dots.removeChild(dots.firstChild);
      var near = p.x >= left && p.x <= right && p.y >= top && p.y <= bottom ? nearest(p.x) : null;
      if (!near) { guide.setAttribute('visibility', 'hidden'); tip.style.display = 'none'; return; }
      var gx = sx(near.key);
      guide.setAttribute('x1', gx);
      guide.setAttribute('x2', gx);
      guide.setAttribute('visibility', 'visible');
      while (tip.firstChild) tip.removeChild(tip.firstChild);
      tip.appendChild(el(doc, 'div', '', (spec.x.label ? spec.x.label + ' ' : '') + near.label));
      near.rows.forEach(function (row) {
        tip.appendChild(el(doc, 'div', '', row.name + ': ' + fmt(row.y)));
        if (row.y !== null && !bar) dots.appendChild(svg(doc, 'circle', {cx: gx, cy: sy(row.y), r: 4.5, fill: COLOURS[row.i],
                                                                          stroke: '#ffffff', 'stroke-width': 1.5}));
      });
      tip.style.display = 'block';
      var scale = p.r.width / WIDTH, boxRect = box.getBoundingClientRect();
      var tx = p.r.left - boxRect.left + gx * scale + 12, ty = p.r.top - boxRect.top + top * scale;
      if (tx + tip.offsetWidth > box.clientWidth) tx = p.r.left - boxRect.left + gx * scale - tip.offsetWidth - 12;
      tip.style.left = Math.max(0, tx) + 'px';
      tip.style.top = ty + 'px';
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
    if (!bar) {
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

  function render(root, widgets, host) {
    var doc = root.ownerDocument || root;
    if (!doc.getElementById('abw-style')) {
      var style = el(doc, 'style', '', STYLE);
      style.id = 'abw-style';
      (doc.head || doc.documentElement).appendChild(style);
    }
    if (!Array.isArray(widgets)) { host.problem('the widgets are not a list'); return 0; }
    var drawn = 0;
    widgets.forEach(function (w) {
      var id = w && w.id;
      if (typeof id !== 'string' || !ID.test(id)) { host.problem('a widget with an id that is not s<n>-<n> was left out'); return; }
      var place = root.querySelector('div[data-widget="' + id + '"]');
      if (!place) { host.problem('widget ' + id + ' has no place in the mail'); return; }
      try {
        var made = w.kind === 'choices' ? drawChoices(doc, w.spec, host)
          : w.kind === 'plot' ? drawPlot(doc, w.spec, id)
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

  return {render: render};
})();
