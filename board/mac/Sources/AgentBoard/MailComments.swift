import AppKit
import WebKit

// Comments on text in an agent's mail, as in Google Docs: the owner selects text in an agent's mail (not their own)
// and a Comment button comes beside the selection, and Comment… heads the right-click menu (MailWebView). Either one adds a
// comment to the thread's reply, quoting the text (Draft.addComment), and highlights the text in the mail; clicking
// the highlight gives the comment's field the keyboard. The draft keeps each comment's quote, mail and place, and the
// highlights follow the draft: an × in the reply box or Discard takes them away, and each time the thread is shown
// they are drawn again by finding each quote in its mail. A small script of the app's own does the document's side,
// in a content world of its own, as widgets.js and the Copy buttons run in theirs; the page itself still runs none.

/// A comment as the document needs it to highlight its text: its id, mail, place and quote, not what it says.
struct CommentAnchor: Codable, Equatable {
    var id: String
    var message: String
    var at: Int
    var quote: String

    init(_ c: DraftComment) {
        id = c.id
        message = c.message
        at = c.at
        quote = c.quote
    }
}

@MainActor
enum MailComments {
    /// The content world the comments script runs in, apart from the page (which runs nothing) and the app's other
    /// scripts.
    static let world = WKContentWorld.world(name: "AgentBoardMailComments")
    /// The message handler a comment, a highlight clicked, the selection's state or a problem comes back through.
    static let handler = "mailComment"
    /// The highlight's colour, in the mail and beside each quote in the reply box.
    static let highlight: UInt32 = 0xFFE08A

    /// The script that highlights the text the comments `comments` (JSON text, json(anchors)) quote, and nothing else:
    /// how many it found.
    static let draw = "return AgentBoardComments.draw(JSON.parse(comments));"
    /// The script behind Comment… in the right-click menu: a comment on the selection (null), or why there is none.
    static let fromSelection = "return AgentBoardComments.fromSelection();"

    /// Comments' anchors as the JSON text `draw` takes.
    static func json(_ anchors: [CommentAnchor]) throws -> String {
        String(decoding: try JSONEncoder().encode(anchors), as: UTF8.self)
    }

    /// ReplyText.blanks as a JavaScript character class's escapes.
    static let blankClass = ReplyText.blanks.sorted { $0.value < $1.value }
        .map { String(format: "\\u%04X", $0.value) }.joined()

    /// Right-to-left script, by code point: Hebrew, Arabic and the other right-to-left scripts, their presentation
    /// forms and Arabic digits (Unicode's bidi classes R, AL and AN; the mailroom refuses them in widgets' text).
    /// Beside such a character, words and numbers can show in another order than they come in the text, and so in the
    /// quote.
    static let rightToLeft: [ClosedRange<UInt32>] = [0x0590...0x08FF, 0xFB1D...0xFDFF, 0xFE70...0xFEFC,
                                                     0x10800...0x10FFF, 0x1E800...0x1EFFF]
    /// rightToLeft as a JavaScript character class's ranges.
    static let rightToLeftClass = rightToLeft
        .map { String(format: "\\u{%X}-\\u{%X}", $0.lowerBound, $0.upperBound) }.joined()
    static let rightToLeftWhy = "this text holds right-to-left characters, so the reply could not quote it in the order "
        + "it shows; select less"

    /// `s` as a JavaScript string literal.
    nonisolated static func literal(_ s: String) -> String {
        String(decoding: try! JSONEncoder().encode(s), as: UTF8.self)
    }

    /// The most characters a quote holds, made plain: what fits whole, with its comment field, in the room the reply
    /// box gives its answers and quotes at its narrowest (about 900 characters of prose; --selftest-mail checks it).
    static let maxQuote = 800

    /// A comment the script posted ({message, at, quote}): one of `mail`, the agents' mails of the thread shown, the
    /// place, and a quote with something to show and no right-to-left script (rightToLeft), made plain
    /// (ReplyText.plain), of at most maxQuote characters; or why it is not taken.
    static func comment(_ raw: Any, of mail: Set<String>)
        -> (comment: (message: String, at: Int, quote: String)?, why: String?) {
        guard let c = raw as? [String: Any], let message = c["message"] as? String, mail.contains(message),
              let at = c["at"] as? Int, at >= 0, let shown = c["quote"] as? String,
              case let quote = ReplyText.plain(shown, lines: true), !quote.isEmpty else {
            return (nil, "the comments script posted a comment not understood")
        }
        guard !shown.unicodeScalars.contains(where: { s in rightToLeft.contains { $0.contains(s.value) } }) else {
            return (nil, rightToLeftWhy)
        }
        guard quote.count <= maxQuote else {
            return (nil, "select less: at most \(Fmt.count(Double(maxQuote))) characters")
        }
        return ((message, at, quote), nil)
    }

    /// The script, at the document's end, in its own world. A quote is found by the characters of its mail's text that
    /// are neither white space, blank (ReplyText.blanks) nor default-ignorable, as a selection's text has line breaks
    /// and spaces where the document has tags, and the other way round; the place counts those characters too
    /// (DraftComment.at). A mail's text is its text nodes outside its widgets, styles, scripts and buttons (a code
    /// block's Copy). A selection is quoted whole or not at all. It is refused when it holds text the page adds that is
    /// not in the mail's text (generated: ::before and ::after content, a list marker of words, on any element it takes
    /// in), text laid out right to left or holding right-to-left characters (rightToLeft), text off screen, or text
    /// this script finds hidden (refusal): not visible, of a font under 4 points, a colour it cannot read, all but
    /// transparent or too near the colour behind it (a border's too, or over a background image), under an element with
    /// no display, all but no opacity, a filter, clip path or mask, its content skipped or flattened, in a closed
    /// <details>, under another element, or not wholly inside every element that clips it, the card too. The first line
    /// of defence is the mailroom's sanitizer, which keeps only an allowlist of CSS (its README, "The Mail tab's
    /// mailbox"): nothing that positions, transforms, filters, blends, fades or clips text, and no generated text; and
    /// the web view draws no font under MailWeb.minimumFontSize. What those let through, these checks are for: text
    /// near its ground's colour, sizes nested small in relative units, long <pre> lines, HTML attributes such as
    /// color=, bgcolor= and width=. For whatever else hides text, the guard is the reply box, which shows every quote
    /// whole, left to right, and holds Send until it has all been scrolled through (ComposeBox). Its own elements are
    /// known by data- attributes, which mail cannot carry (the mailroom keeps no data- attribute but its own
    /// data-widget), so a mail cannot borrow their look.
    static let script = #"""
    var AgentBoardComments = (function () {
      'use strict';
      var STYLE = [
        'mark[data-abk-comment] { background: \#(String(format: "#%06X", highlight)); color: inherit;',
        '  border-radius: 2px; cursor: pointer; }',
        'mark[data-abk-comment]:hover { box-shadow: 0 0 0 1px #d4a72c; }',
        'button[data-abk-add] { position: absolute; z-index: 20; display: none; padding: 3px 10px; line-height: 18px;',
        '  font: 12px -apple-system, system-ui, sans-serif; color: #4d3800; background: #fff6d6;',
        '  border: 1px solid #d4a72c; border-radius: 6px; box-shadow: 0 2px 8px rgba(16, 24, 40, 0.18);',
        '  cursor: pointer; }',
        'button[data-abk-add]:hover { background: #ffe9a8; }'
      ].join('\n');
      var KEY = /^s[0-9]+$/;            // an agent's mail; the owner's own are "i<id>"
      // what a quote is found without (white space, blanks and default-ignorable characters: what ReplyText.plain
      // makes a space or drops), and what by
      var BARE = /[\s\u0085\#(blankClass)\p{Default_Ignorable_Code_Point}]+/gu;
      var KEPT = /[^\s\u0085\#(blankClass)\p{Default_Ignorable_Code_Point}]/gu;
      var MAX = 300000;                 // the most characters of a mail a comment can quote or be found in
      var RIGHT_TO_LEFT = /[\#(rightToLeftClass)]/u;     // right-to-left script (MailComments.rightToLeft)

      function post(m) { window.webkit.messageHandlers.\#(handler).postMessage(m); }

      function bare(text) { return text.replace(BARE, ''); }

      // The card holding agent mail `key`'s text, or null.
      function cardOf(key) {
        var m = KEY.test(key) ? document.getElementById(key) : null;
        return m && m.matches('details.msg:not(.mine)') ? m.querySelector(':scope > .card') : null;
      }

      // The card of the agent's mail that `node` is in, or null.
      function cardAround(node) {
        var e = node.nodeType === 1 ? node : node.parentNode;
        var card = e && e.closest ? e.closest('details.msg > .card') : null;
        return card && card === cardOf(card.parentNode.id) ? card : null;
      }

      // Whether text under element `e`, in `card`, is not the mail's text: in a widget, a style, a script or a button
      // (a code block's Copy). `memo` keeps each element's answer for one reading of the card.
      function aside(e, card, memo) {
        if (!e || e === card) return false;
        if (!memo.has(e)) memo.set(e, e.matches('.widget, style, script, button') || aside(e.parentElement, card, memo));
        return memo.get(e);
      }

      // A computed colour as [r, g, b (0 to 255), alpha (0 to 1)]: "transparent", rgb(), rgba() or color() (its
      // space's channels read as sRGB's, near enough to tell text from its ground); null for any other.
      function rgba(colour) {
        if (colour === 'transparent') return [0, 0, 0, 0];
        var m = /^rgba?\(([^)]*)\)$/.exec(colour), n = /^color\(\s*[\w-]+\s+([^)]*)\)$/.exec(colour);
        if (!m && !n) return null;
        var v = (m || n)[1].trim().split(/[\s,\/]+/).map(function (x) {
          return /%$/.test(x) ? parseFloat(x) / 100 * (m ? 255 : 1) : parseFloat(x);
        });
        if (v.length < 3 || v.length > 4 || v.some(isNaN)) return null;
        var scale = m ? 1 : 255;
        return [v[0] * scale, v[1] * scale, v[2] * scale, v.length > 3 ? v[3] : 1];
      }

      // The colour behind point (x, y) of element `e`'s text: from `e` up, of each element whose box holds the point,
      // its border's colour where the point is on a border, else its background's; the first solid one. Null when a
      // background image comes first, or a colour that cannot be read.
      function groundAt(e, x, y) {
        for (; e; e = e.parentElement) {
          var s = getComputedStyle(e), b = e.getBoundingClientRect();
          if (s.backgroundImage !== 'none') return null;
          if (x < b.left || x >= b.right || y < b.top || y >= b.bottom) continue;
          var side = y < b.top + parseFloat(s.borderTopWidth) ? 'Top'
            : y >= b.bottom - parseFloat(s.borderBottomWidth) ? 'Bottom'
            : x < b.left + parseFloat(s.borderLeftWidth) ? 'Left'
            : x >= b.right - parseFloat(s.borderRightWidth) ? 'Right' : null;
          var c = rgba(side && !/^(none|hidden)$/.test(s['border' + side + 'Style']) ? s['border' + side + 'Color']
                                                                                       : s.backgroundColor);
          if (c === null) return null;
          if (c[3] >= 0.5) return c;
        }
        return [255, 255, 255, 1];
      }

      // Whether element `a` hides what is in it, whatever its descendants say: no display, all but no opacity, a
      // filter, a clip path or a mask, its content skipped, or a transform that flattens it.
      function hidesWithin(a, memo) {
        if (memo.has(a)) return memo.get(a);
        var s = getComputedStyle(a), out = s.display === 'none' || parseFloat(s.opacity) < 0.1 || s.filter !== 'none'
          || s.clipPath !== 'none' || (s.webkitMaskImage || 'none') !== 'none' || (s.maskImage || 'none') !== 'none'
          || s.contentVisibility === 'hidden';
        var m = /^matrix(3d)?\(([^)]*)\)$/.exec(s.transform);
        if (!out && m) {
          var v = m[2].split(',').map(parseFloat), d = m[1] ? v[0] * v[5] - v[1] * v[4] : v[0] * v[3] - v[1] * v[2];
          out = Math.abs(d) < 0.01;
        }
        memo.set(a, out);
        return out;
      }

      // The element the page shows at point (x, y) for text in element `e`: the topmost there but the app's own Comment
      // button and its highlights, unless the highlight holds `e`.
      function shownAt(x, y, e) {
        var stack = document.elementsFromPoint(x, y);
        for (var i = 0; i < stack.length; i++) {
          if (!add.contains(stack[i]) && !(stack[i].matches('mark[data-abk-comment]') && !stack[i].contains(e))) return stack[i];
        }
        return null;
      }

      var HIDDEN = 'this selection includes text the mail hides; select less';
      var RTL = 'this text is laid out right to left, so the reply could not quote it in the order it shows; select less';
      var OFF = 'scroll so that all of the selection shows, then comment';

      // Why the text of `range` in text node `n` (in `card`) cannot be quoted as the owner sees it, or null. It is laid out
      // right to left (the reply box shows the agent's words left to right); or part of it is off screen; or the mail
      // hides it, or may (a colour that cannot be read counts as hiding it). What text inherits (its visibility, size
      // and colours) is read from its parent's computed style, which has resolved it; what hides all within it
      // (hidesWithin), or a closed <details> it is in outside the summary, is looked for in every element up to the
      // card; and every piece of the text must take room on the page, lie wholly inside every element that clips what
      // overflows it and inside the card, which paints only within itself (contain: content), be on screen, stand off
      // the colour behind it there, borders included (groundAt), and be what the page shows there (shownAt).
      function refusal(n, range, card, memo) {
        var e = n.parentElement, s = getComputedStyle(e), c = rgba(s.color), f = rgba(s.webkitTextFillColor);
        if (s.direction === 'rtl') return RTL;
        if (c === null || f === null || s.visibility !== 'visible' || parseFloat(s.fontSize) < 4 || c[3] < 0.1 || f[3] < 0.1) {
          return HIDDEN;
        }
        var summary = e.closest('summary');
        for (var a = e; a && a !== card; a = a.parentElement) {
          if (hidesWithin(a, memo) || (a.nodeName === 'DETAILS' && !a.open && !(summary && summary.parentElement === a))) {
            return HIDDEN;
          }
        }
        var part = document.createRange();
        part.selectNodeContents(n);
        if (n === range.startContainer) part.setStart(n, range.startOffset);
        if (n === range.endContainer) part.setEnd(n, range.endOffset);
        var rects = Array.prototype.filter.call(part.getClientRects(),
                                                function (r) { return r.width >= 1 && r.height >= 2; });
        if (!rects.length) return HIDDEN;
        for (a = e; a; a = a === card ? null : a.parentElement) {
          var o = getComputedStyle(a);
          if (a !== card && o.overflowX === 'visible' && o.overflowY === 'visible') continue;   // the card paints within itself
          var box = a.getBoundingClientRect();
          if (!rects.every(function (r) { return r.left >= box.left - 1 && r.right <= box.right + 1
                                                 && r.top >= box.top - 1 && r.bottom <= box.bottom + 1; })) {
            return HIDDEN;
          }
        }
        var view = document.documentElement;
        for (var i = 0; i < rects.length; i++) {
          var r = rects[i], x = r.left + r.width / 2, y = r.top + r.height / 2;
          if (x < 0 || y < 0 || x >= view.clientWidth || y >= view.clientHeight) return OFF;
          var g = groundAt(e, x, y), over = shownAt(x, y, e);
          if (g === null || Math.max(Math.abs(f[0] - g[0]), Math.abs(f[1] - g[1]), Math.abs(f[2] - g[2])) < 40) return HIDDEN;
          if (!over || !e.contains(over)) return HIDDEN;
        }
        return null;
      }

      // Whether text the page adds that is not in the mail's text is in or around the selection: ::before or ::after
      // content or a list item's marker of words, on any element the range takes in any of, the card and the elements
      // holding its ends included. Such text may give the words selected a meaning the quote would not have.
      function generated(range, card) {
        var walk = document.createTreeWalker(card, NodeFilter.SHOW_ELEMENT), e = card;
        function added(el, pseudo) {
          var c = getComputedStyle(el, pseudo).content;
          return c !== 'none' && c !== 'normal';
        }
        for (; e; e = walk.nextNode()) {
          if (e !== card && !range.intersectsNode(e)) continue;
          if (added(e, '::before') || added(e, '::after')) return true;
          if (e.nodeName === 'LI' && (/^["']/.test(getComputedStyle(e).listStyleType) || added(e, '::marker'))) return true;
        }
        return false;
      }

      // The card's text a comment can quote: `text`, the characters it is found by, run together (up to MAX), and
      // `segs`, each text node with where its characters start in `text`.
      function textOf(card) {
        var out = {segs: [], text: '', memo: new Map()}, walk = document.createTreeWalker(card, NodeFilter.SHOW_TEXT), n;
        while ((n = walk.nextNode()) && out.text.length < MAX) {
          if (aside(n.parentElement, card, out.memo)) continue;
          var kept = bare(n.data);
          if (!kept) continue;
          out.segs.push({node: n, from: out.text.length, length: kept.length});
          out.text += kept;
        }
        return out;
      }

      // Where character `i` of `t.text` is: its segment's index, node and offset there.
      function locate(t, i) {
        var lo = 0, hi = t.segs.length - 1;
        while (lo < hi) {
          var mid = (lo + hi + 1) >> 1;
          if (t.segs[mid].from <= i) lo = mid; else hi = mid - 1;
        }
        var seg = t.segs[lo], want = i - seg.from, k = 0, m;
        KEPT.lastIndex = 0;
        while ((m = KEPT.exec(seg.node.data))) {
          if (want < k + m[0].length) return {seg: lo, node: seg.node, at: m.index + want - k};
          k += m[0].length;
        }
        return null;
      }

      // Where `q` (a quote's characters) comes in `t.text`: at `at` when it is still there, else the place nearest it.
      function find(t, q, at) {
        if (t.text.substr(at, q.length) === q) return at;
        var after = t.text.indexOf(q, at), before = at > 0 ? t.text.lastIndexOf(q, at - 1) : -1;
        if (after < 0 || before < 0) return Math.max(after, before);
        return after - at <= at - before ? after : before;
      }

      // The card the range is in: an agent's mail's, with nothing but white space of the range outside it (a
      // triple-click's selection ends at the next block, which may be past the mail); else null.
      function within(range) {
        var card = cardAround(range.startContainer) || cardAround(range.endContainer), out = document.createRange();
        if (!card) return null;
        if (!card.contains(range.startContainer)) {
          out.setStart(range.startContainer, range.startOffset);
          out.setEndBefore(card);
          if (out.toString().trim()) return null;
        }
        if (!card.contains(range.endContainer)) {
          out.setStartAfter(card);
          out.setEnd(range.endContainer, range.endOffset);
          if (out.toString().trim()) return null;
        }
        return card;
      }

      // The block element `e` is laid out in, within `card` (memo: as in aside).
      function blockOf(e, card, memo) {
        for (; e && e !== card; e = e.parentElement) {
          if (!memo.has(e)) memo.set(e, !/^(inline|contents|ruby)/.test(getComputedStyle(e).display));
          if (memo.get(e)) return e;
        }
        return card;
      }

      // The text of `range` in `card` (`t`, textOf(card)): {text}, its text nodes cut to the range, a new line where a
      // block or a line break comes between them; or {why} the first piece of it that cannot be quoted cannot be
      // (refusal).
      function rangeText(range, card, t) {
        var walk = document.createTreeWalker(card, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT), n, out = '';
        var blocks = new Map(), hiders = new Map(), block = null;
        while ((n = walk.nextNode())) {
          if (!range.intersectsNode(n)) continue;
          if (n.nodeType === 1) {
            if (n.nodeName === 'BR' && !aside(n, card, t.memo)) out += '\n';
            continue;
          }
          if (aside(n.parentElement, card, t.memo)) continue;
          var part = n.data.slice(n === range.startContainer ? range.startOffset : 0,
                                  n === range.endContainer ? range.endOffset : n.data.length);
          var why = bare(part) ? refusal(n, range, card, hiders) : null;
          if (why) return {why: why};
          var b = blockOf(n.parentElement, card, blocks);
          if (block !== null && b !== block) out += '\n';
          block = b;
          out += part;
        }
        return {text: out};
      }

      // The selection as a comment quotes it, {message, at, quote}; or {why} it cannot be quoted.
      function selected() {
        var sel = window.getSelection();
        if (!sel || sel.rangeCount !== 1 || sel.isCollapsed) return {why: "select some text in an agent's mail first"};
        var range = sel.getRangeAt(0), card = within(range);
        if (!card) return {why: "select text in one agent's mail, not your own or across mails"};
        var widgets = card.querySelectorAll('.widget');
        for (var w = 0; w < widgets.length; w++) {
          if (range.intersectsNode(widgets[w])) {
            return {why: 'select text outside the options and plots (right-click an option to choose it with a comment)'};
          }
        }
        if (generated(range, card)) {
          return {why: 'this selection holds text the page adds that is not in the mail (::before or ::after content, a '
                       + 'list marker); select less'};
        }
        var t = textOf(card), shown = rangeText(range, card, t);
        if (shown.why) return {why: shown.why};
        if (RIGHT_TO_LEFT.test(shown.text)) return {why: \#(literal(rightToLeftWhy))};
        var quote = shown.text.trim(), q = bare(quote), at = t.text.length;
        if (!q) return {why: 'select some text, not only space'};
        // its place: where the first of its characters comes, the first text shown at or after the selection's start
        for (var i = 0; i < t.segs.length; i++) {
          var seg = t.segs[i];
          if (seg.node === range.startContainer) {
            at = seg.from + bare(seg.node.data.slice(0, range.startOffset)).length;
            break;
          }
          if (range.comparePoint(seg.node, seg.node.data.length) >= 0) {
            at = seg.from;
            break;
          }
        }
        if (t.text.substr(at, q.length) !== q) {
          return {why: 'the selection is past the most of a mail a comment can quote (' + MAX + ' characters); '
                       + 'select less'};
        }
        return {message: card.parentNode.id, at: at, quote: quote};
      }

      // A comment on the selection (the app adds it to the reply and has its text highlighted): null, or why not.
      function fromSelection() {
        var s = selected();
        if (s.why) return s.why;
        post({comment: {message: s.message, at: s.at, quote: s.quote}});
        window.getSelection().removeAllRanges();
        return null;
      }

      function unwrap() {
        Array.prototype.forEach.call(document.querySelectorAll('mark[data-abk-comment]'), function (m) {
          var p = m.parentNode;
          while (m.firstChild) p.insertBefore(m.firstChild, m);
          p.removeChild(m);
          p.normalize();
        });
      }

      // Highlight characters `from` to `to` of `t.text` for comment `id`: the text nodes they are in, from the first
      // character's place to the end of the last.
      function wrap(t, from, to, id) {
        var a = locate(t, from), b = locate(t, to);
        for (var k = a.seg; k <= b.seg; k++) {
          var n = t.segs[k].node, start = k === a.seg ? a.at : 0, end = k === b.seg ? b.at + 1 : n.data.length;
          if (start > 0) n = n.splitText(start);
          if (end - start < n.data.length) n.splitText(end - start);
          var m = document.createElement('mark');
          m.setAttribute('data-abk-comment', id);
          n.parentNode.insertBefore(m, n);
          m.appendChild(n);
        }
      }

      // Highlight the text `comments` ([{id, message, at, quote}]) quote, and nothing else: how many were found. Each
      // is looked for in its mail at its place, else where it comes nearest. One the mail no longer holds (the mail
      // changed) is not an error: its comment stays in the reply box, with no highlight.
      function draw(comments) {
        unwrap();
        var found = 0;
        comments.forEach(function (c) {
          var card = cardOf(c.message), q = bare(c.quote);
          if (!card || !q) return;
          var t = textOf(card), i = find(t, q, c.at);
          if (i < 0) return;
          wrap(t, i, i + q.length - 1, c.id);
          found += 1;
        });
        return found;
      }

      var style = document.createElement('style');
      style.textContent = STYLE;
      (document.head || document.documentElement).appendChild(style);
      // the Comment button, in the body (a mail's card contains its paint, which would clip it)
      var add = document.createElement('button');
      add.type = 'button';
      add.setAttribute('data-abk-add', '');
      add.textContent = 'Comment';
      add.title = 'Comment on this text in your reply';
      document.body.appendChild(add);
      add.addEventListener('mousedown', function (e) { e.preventDefault(); });      // the selection stays
      add.addEventListener('click', function (e) {
        e.preventDefault();
        var why = fromSelection();
        if (why) post({problem: why});
      });

      // As the selection settles: the button beside its end while it can be commented on, and the app told when that
      // changes (for Comment… in the right-click menu).
      var said = null, timer = null;
      function update() {
        var s = selected(), can = !s.why;
        add.style.display = can ? 'block' : 'none';
        if (can) {
          var range = window.getSelection().getRangeAt(0), rects = range.getClientRects();
          var r = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect();
          var x = Math.max(0, Math.min(r.right + 6, document.documentElement.clientWidth - add.offsetWidth - 6));
          add.style.left = (x + window.scrollX) + 'px';
          add.style.top = (r.bottom + 4 + window.scrollY) + 'px';
        }
        if (can !== said) {
          said = can;
          post({selection: can});
        }
      }
      document.addEventListener('selectionchange', function () {
        clearTimeout(timer);
        timer = setTimeout(update, 100);
      });
      window.addEventListener('resize', update);
      // A right-click may itself select the word under it, just before this event: say at once, not once it settles,
      // so that the app knows before the menu opens (this message reaches it first) and Comment… is in that menu.
      document.addEventListener('contextmenu', function () {
        clearTimeout(timer);
        update();
      }, true);
      // a click on a highlight (not the end of a selection) gives its comment's field the keyboard
      document.addEventListener('click', function (e) {
        var m = e.target.closest && e.target.closest('mark[data-abk-comment]');
        if (m && window.getSelection().isCollapsed) post({focus: m.getAttribute('data-abk-comment')});
      });

      return {draw: draw, fromSelection: fromSelection};
    })();
    """#
}

/// --selftest-comments: the comments script in a real document view (MailWeb's, with its content rules and content
/// worlds), offscreen, driven by script. Text selected in an agent's mail gets the Comment button and Comment… in the
/// right-click menu, at once when the right-click selects it; either one adds a comment to the thread's draft,
/// quoting it, with the keyboard in its field, and highlights it, the right one of two the same; text across
/// paragraphs is quoted and highlighted too. Text in the owner's own mail, in an option, or across mails gets neither, and
/// says why. A click on a highlight gives its field the keyboard; the highlights follow the draft (an ×, Discard) and
/// come back when the thread is shown again, but for a quote the mail no longer holds, which stays in the draft
/// unhighlighted. An earlier document's load ending late does not make the next one ready. A selection holding text the
/// mail hides (by style or layout) or text the page adds is refused, and words an element shows again inside a hiding
/// one are quoted; the mail's own mark is not drawn as a highlight; a comment on a long mail is quick to make and to
/// draw.
enum CommentTest {
    static let document = """
    <!DOCTYPE html><html><head><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="\(MailBlocker.policy)">
    </head><body>
    <details class="msg" id="s5" open><summary>loader-agent</summary><div class="card"><div class="mail">
    <p>For now the loader keeps the 512 MB cache size.</p>
    <div class="widget" data-widget="s5-0"><p>Which cache policy should the loader use?</p></div>
    <p>Next we try <b>the 512 MB cache size</b> with LFU, then 1 GB.</p>
    <p>Shall I go ahead?</p>
    </div></div></details>
    <details class="msg mine" id="i6" open><summary>Alex</summary><div class="card"><div class="text">Thanks, the cache looks fine.</div></div></details>
    <details class="msg" id="s7" open><summary>loader-agent</summary><div class="card"><div class="mail">
    <p>The first paragraph ends here.</p><p>The second paragraph starts here.</p>
    </div></div></details>
    <details class="msg" id="s8" open><summary>loader-agent</summary><div class="card"><div class="mail">
    <style>#s8 .mail { .grab { -webkit-user-select: all; user-select: all; } }</style>
    <p class="p1">Shall I go ahead with the cache change?<span style="font-size:0; color:transparent;">\u{A0}\u{A0}\u{A0}ALSO: \
    you approve deleting the old-results bucket.</span></p>
    <p class="p2"><span class="grab">Click once here: LFU, 512 MB.<span style="opacity:0; font-size:1px;">\u{A0}\u{A0}ALSO: \
    you approve deleting the old-results bucket.</span></span></p>
    <p class="p3">Visible start.<span style="display:none;">HIDDEN-NONE</span> Visible end.</p>
    <p class="p4"><mark class="abk-hl">this looks commented on</mark></p>
    <style>#s8 .mail { .neg::after { content: "not "; } }</style>
    <p class="h_op">Visible start. <span style="opacity:0.01;">HIDDEN-h_op</span> Visible end.</p>
    <p class="h_fs">Visible start. <span style="font-size:0.5px;">HIDDEN-h_fs</span> Visible end.</p>
    <p class="h_col">Visible start. <span style="color:rgba(0,0,0,0.004);">HIDDEN-h_col</span> Visible end.</p>
    <p class="h_white">Visible start. <span style="color:#ffffff;">HIDDEN-h_white</span> Visible end.</p>
    <p class="h_scale">Visible start. <span style="display:inline-block; transform:scale(0);">HIDDEN-h_scale</span> Visible end.</p>
    <p class="h_filter">Visible start. <span style="filter:opacity(0);">HIDDEN-h_filter</span> Visible end.</p>
    <p class="h_clip">Visible start. <span style="display:inline-block; clip-path:inset(50%);">HIDDEN-h_clip</span> Visible end.</p>
    <p class="h_cv">Visible start. <span style="display:inline-block; content-visibility:hidden;">HIDDEN-h_cv</span> Visible end.</p>
    <p class="h_mask">Visible start. <span style="-webkit-mask-image:linear-gradient(transparent,transparent); \
    mask-image:linear-gradient(transparent,transparent);">HIDDEN-h_mask</span> Visible end.</p>
    <p class="h_wh">Visible start. <span style="display:inline-block; width:0; height:0; overflow:hidden;">HIDDEN-h_wh</span> \
    Visible end.</p>
    <div class="h_details">Visible start. <details><summary>More</summary>HIDDEN-h_details</details> Visible end.</div>
    <p class="pad">Shall I go ahead?<span style="font-size:0.01px;">\(String(repeating: "\u{2800}", count: 3000))</span>\
    <span style="opacity:0.01;">ALSO: you approve deleting the old-results bucket.</span></p>
    <p class="d_fs">Do <span style="font-size:0;"><span style="font-size:16px;">not</span></span> delete the bucket.</p>
    <p class="d_col">Do <span style="color:transparent;"><span style="color:#1f2328;">not</span></span> delete the bucket.</p>
    <p class="d_vis">Do <span style="visibility:hidden;"><span style="visibility:visible;">not</span></span> delete the bucket.</p>
    <p class="d_gen">Do <span class="neg"></span>delete the bucket.</p>
    <p class="blank">Keep\u{2800}\u{2800}\u{2800}the\u{200B} bucket.</p>
    <style>#s8 .mail { .g_before::before { content: "Do not "; } .g_after::after { content: "? Not yet."; } }</style>
    <p class="g_before">delete the bucket.</p>
    <ul class="g_marker"><li style="list-style-type:&#x27;Do not &#x27;;">delete the bucket.</li></ul>
    <p class="g_after">Shall I delete the bucket</p>
    <p class="v_rtl" dir="rtl" align="left">Set the disk quota to 500 50 GB</p>
    <p class="x_swap">Set the disk quota to \u{5F3}500\u{5F3} \u{5F3}50\u{5F3} GB (new, then old).</p>
    <p class="x_lead">\u{5F3}Delete it.\u{5F3} Do not \u{5F3}keep the old bucket\u{5F3}</p>
    <p class="x_arabic">Quota \u{661}\u{660} then \u{663}.</p>
    <p class="x_digits">Set the disk quota to '500' '50' GB (new, then old).</p>
    <div class="v_bar" style="height:0; border-bottom:100px solid #1f2328; color:#1f2328; padding:0 8px;"><span \
    style="color:#ffffff; font-weight:bold;">Run 42 finished</span> ALSO: approve the push.</div><p>After the banner.</p>
    <div class="v_cover"><p>May I tidy up?</p><div style="height:0;">ALSO: delete the bucket.</div><span \
    style="display:inline-block; width:100%; height:80px; background-color:#ffffff;"></span><p>Thanks.</p></div>
    <details open class="v_open"><summary>More detail</summary><p>Inside the details.</p></details>
    <div class="v_under"><div style="height:0;">ALSO: sell the box.</div><p style="margin:0;">Highlight this \
    line, which lies over the words before it.</p></div>
    <div class="v_closed"><p>Before the details.</p><details><summary>Less detail</summary><p>ALSO: drop the table.</p>\
    </details><p>After the details.</p></div>
    <div class="v_scroll" style="overflow-x:auto; width:300px;"><pre style="margin:0;">START \
    \(String(repeating: "python -m loader.bench ", count: 8))TAIL</pre></div>
    <p class="c_p3">Visible start. <span style="color:color(display-p3 0 0 0 / 0);">ALSO: delete the bucket.</span> Visible end.</p>
    <p class="c_p3w">Visible start. <span style="color:color(display-p3 1 1 1);">ALSO: delete the bucket.</span> Visible end.</p>
    <p class="b_bgimg">Visible start. <span style="color:#ffffff; background-color:#000000; \
    background-image:linear-gradient(#ffffff,#ffffff);">ALSO: delete the bucket.</span> Visible end.</p>
    <p class="b_off">Visible start. <span style="position:relative; left:-5000px;">ALSO: delete the bucket.</span> Visible end.</p>
    <p class="b_tail"><span style="display:inline-block; max-width:12em; overflow:hidden; white-space:nowrap; \
    vertical-align:top;">Visible start, then ALSO: delete the bucket.</span></p>
    <p class="plain_p3">Seen in <span style="color:color(display-p3 0.1 0.1 0.1);">a wide-gamut colour</span>.</p>
    <p class="tiny">Fine <span style="font-size:1px;">print</span> here.</p>
    <table class="bar" style="border-collapse:collapse;"><tr><td style="font-size:1px; line-height:1px; width:40px; \
    background-color:#2f6fde;">\u{A0}</td><td style="font-size:1px; line-height:1px; width:60px; \
    background-color:#d0d7de;">\u{A0}</td></tr></table>
    </div></div></details>
    <details class="msg" id="s9" open><summary>loader-agent</summary><div class="card"><div class="mail">
    <p>\(String(repeating: "word ", count: 40000))</p>
    </div></div></details>
    </body></html>
    """

    /// The script that selects, in mail `key`'s card, from the `nth` (from 0) text node holding `from` to the end of
    /// `to` after it; it returns the selection's text.
    static func select(_ key: String, from: String, to: String, nth: Int = 0) -> String {
        """
        (function () {
          var card = document.querySelector('#\(key) > .card'), walk = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
          var n, seen = 0, start = null, end = null, from = \(MailComments.literal(from)), to = \(MailComments.literal(to));
          while ((n = walk.nextNode())) {
            if (start === null && n.data.indexOf(from) >= 0) { if (seen === \(nth)) start = [n, n.data.indexOf(from)]; seen += 1; }
            if (start !== null) {
              var j = n.data.indexOf(to, n === start[0] ? start[1] : 0);
              if (j >= 0) { end = [n, j + to.length]; break; }
            }
          }
          var r = document.createRange(), s = window.getSelection();
          r.setStart(start[0], start[1]);
          r.setEnd(end[0], end[1]);
          s.removeAllRanges();
          s.addRange(r);
          window.scrollBy(0, r.getBoundingClientRect().top - 40);
          return s.toString();
        })()
        """
    }

    @MainActor
    static func run() -> Int32 {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append(what) } }
        let web = MailWeb.view
        web.frame = NSRect(x: 0, y: 0, width: 760, height: 700)
        let win = NSWindow(contentRect: web.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        win.contentView = web
        win.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        win.orderBack(nil)
        return withExtendedLifetime(win) {
            var problems: [String] = []
            let draft = Draft(text: "", files: [], key: nil)
            let loader = MailWeb.loader
            loader.problem = { problems.append($0) }
            loader.refuse = { problems.append("refused: " + $0) }
            let js = { (source: String) in Renderer.evaluate(source, in: web, within: 5) }
            let show = {
                loader.show(document, thread: "T1", agentMail: ["s5", "s7", "s8", "s9"], widgets: [], draft: draft, current: nil, in: web)
                return Renderer.wait(15, step: 0.01) { loader.ready }
                    && Renderer.inPage(web, id: "s7", within: 15)
                    && Renderer.wait(5, step: 0.01) { js("document.querySelector('button[data-abk-add]') !== null") as? Bool == true }
            }
            guard show() else {
                print("FAIL: the document was not shown in 15 s: \(problems)")
                return 1
            }
            /// The button is showing, and the app has been told the selection can be commented on (or neither).
            let offered = { (yes: Bool) -> Bool in
                Renderer.wait(3, step: 0.01) {
                    web.commentable == yes
                        && js("getComputedStyle(document.querySelector('button[data-abk-add]')).display") as? String == (yes ? "block" : "none")
                }
            }
            /// The text of the highlights of comment `id`, each one's.
            let marks = { (id: String) in
                js("JSON.stringify(Array.prototype.map.call(document.querySelectorAll('mark[data-abk-comment=\"\(id)\"]'), "
                   + "function (m) { return m.textContent; }))") as? String
            }
            /// What Comment… would say, in the comments' world: nil when it commented.
            let fromSelection = {
                Renderer.evaluate(MailComments.fromSelection, arguments: [:], in: web, world: MailComments.world, within: 5)
                    as? String
            }

            // text in an agent's mail, the second of two the same: the button; a click comments on that one
            _ = js(select("s5", from: "the 512 MB cache size", to: "cache size", nth: 1))
            check(offered(true), "a selection in an agent's mail: the Comment button, and the app is told")
            _ = js("document.querySelector('button[data-abk-add]').click()")
            _ = Renderer.wait(3, step: 0.01) { !draft.comments.isEmpty }
            let first = draft.comments.first
            let at = "For now the loader keeps the 512 MB cache size.Next we try ".filter { !$0.isWhitespace }.count
            check(first?.message == "s5" && first?.quote == "the 512 MB cache size" && first?.at == at && first?.comment == "",
                  "a comment quoting it, at its place: \(draft.comments)")
            check(draft.focus == first?.id && draft.shown, "the box opens, the keyboard in its field")
            _ = Renderer.wait(3, step: 0.01) { marks(first?.id ?? "") != "[]" }
            check(marks(first?.id ?? "") == #"["the 512 MB cache size"]"#
                  && js("document.querySelector('mark[data-abk-comment]').parentNode.nodeName") as? String == "B",
                  "the second one is highlighted: \(marks(first?.id ?? "") ?? "nothing")")
            check(offered(false) && js("window.getSelection().isCollapsed") as? Bool == true, "and the selection goes, with the button")

            // the owner's own mail, an option, across mails: no button, nothing in the menu, and Comment… says why
            for (key, from, to, why) in [("i6", "the cache", "fine", "not your own"), ("s5", "cache size.", "Next", "outside the options")] {
                _ = js(select(key, from: from, to: to))
                check(offered(false), "\(key) \(from)…: no button")
                let said = fromSelection()
                check(said?.contains(why) == true, "\(key) \(from)…: Comment… says why: \(said ?? "nothing")")
            }
            _ = js("""
                (function () { var r = document.createRange(), s = window.getSelection();
                  r.setStart(document.querySelector('#s5 p:last-child').firstChild, 0);
                  r.setEnd(document.querySelector('#s7 p').firstChild, 5);
                  s.removeAllRanges(); s.addRange(r); })()
                """)
            check(offered(false) && fromSelection()?.contains("across mails") == true, "across two mails: none")
            check(draft.comments.count == 1, "none of them commented: \(draft.comments)")

            // across two paragraphs: quoted with a line break between them, highlighted in each
            _ = js(select("s7", from: "ends here.", to: "The second"))
            check(offered(true), "across paragraphs: the button")
            _ = js("document.querySelector('button[data-abk-add]').click()")
            _ = Renderer.wait(3, step: 0.01) { draft.comments.count == 2 }
            let second = draft.comments.last
            check(second?.message == "s7" && second?.quote == "ends here.\nThe second",
                  "the quote as selected: \(second?.quote.debugDescription ?? "none")")
            _ = Renderer.wait(3, step: 0.01) { marks(second?.id ?? "") != "[]" }
            check(marks(second?.id ?? "") == #"["ends here.","The second"]"#, "highlighted in each: \(marks(second?.id ?? "") ?? "")")

            // Comment… in the right-click menu, while the selection can be commented on; a right-click that itself
            // selects the text has the app told at once, before its menu opens, not once the selection settles
            check(offered(false), "no selection yet")
            _ = js(select("s5", from: "Shall I", to: "ahead?") + "; document.querySelector('#s5 .mail > p:last-child')"
                   + ".dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true}))")
            check(web.commentable, "a right-click: the app knows at once that the selection can be commented on")
            check(offered(true), "a third selection")
            let event = NSEvent.mouseEvent(with: .rightMouseDown, location: .zero, modifierFlags: [], timestamp: 0,
                                           windowNumber: win.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1)
            let menu = NSMenu()
            menu.addItem(withTitle: "Copy", action: nil, keyEquivalent: "")
            if let e = event { web.willOpenMenu(menu, with: e) }
            check(menu.items.first?.title == "Comment…" && menu.items.count == 3, "the menu starts with Comment…: \(menu.items.map(\.title))")
            menu.performActionForItem(at: 0)
            _ = Renderer.wait(3, step: 0.01) { draft.comments.count == 3 }
            check(draft.comments.map(\.quote) == ["the 512 MB cache size", "Shall I go ahead?", second?.quote ?? ""],
                  "Comment… comments on it, in the order of the text: \(draft.comments.map(\.quote))")
            let plain = NSMenu()
            check(offered(false), "the selection gone")
            if let e = event { web.willOpenMenu(plain, with: e) }
            check(plain.items.isEmpty, "with nothing to comment on, the menu is WebKit's own")

            // a click on a highlight: its field takes the keyboard
            draft.focus = nil
            _ = js("document.querySelector('mark[data-abk-comment=\"\(second?.id ?? "")\"]').click()")
            _ = Renderer.wait(3, step: 0.01) { draft.focus != nil }
            check(draft.focus == second?.id, "a click on a highlight: its comment's field: \(draft.focus ?? "none")")

            // an × takes its highlight out
            draft.uncomment(first?.id ?? "")
            _ = Renderer.wait(3, step: 0.01) { marks(first?.id ?? "") == "[]" }
            check(marks(first?.id ?? "") == "[]" && marks(second?.id ?? "") == #"["ends here.","The second"]"#,
                  "an ×: its highlight goes, the others stay")

            // shown again (another document, then this one): the highlights come back; a quote the mail no longer holds
            // stays in the draft, unhighlighted, and nothing is said
            draft.addComment(message: "s5", at: 0, quote: "a sentence the agent took out")
            let gone = draft.comments.first { $0.quote == "a sentence the agent took out" }
            loader.show(WidgetTest.twoQuestions, thread: "T2", agentMail: ["s21"], widgets: [], draft: Draft(text: "", files: [], key: nil),
                        current: nil, in: web)
            loader.webView(web, didFinish: nil)                // an earlier document's load, ending late
            check(!loader.ready, "a load that is not the new document's does not make it ready")
            check(Renderer.wait(15, step: 0.01) { loader.ready } && Renderer.inPage(web, id: "s21", within: 15), "another document")
            check(show(), "this one again")
            _ = Renderer.wait(5, step: 0.01) { (js("document.querySelectorAll('mark[data-abk-comment]').length") as? Int ?? 0) >= 3 }
            let third = draft.comments.first { $0.quote == "Shall I go ahead?" }
            check(marks(second?.id ?? "") == #"["ends here.","The second"]"#
                  && marks(third?.id ?? "") == #"["Shall I go ahead?"]"#, "the highlights come back")
            check(marks(gone?.id ?? "") == "[]" && draft.comments.count == 3, "a quote not found: kept, not highlighted")

            // Discard: no highlight left
            draft.clear()
            _ = Renderer.wait(3, step: 0.01) { js("document.querySelectorAll('mark[data-abk-comment]').length") as? Int == 0 }
            check(js("document.querySelectorAll('mark[data-abk-comment]').length") as? Int == 0, "Discard takes every highlight away")
            check(js("document.querySelector('#s5').textContent.indexOf('the 512 MB cache size with LFU') >= 0") as? Bool == true,
                  "and the mail's text is as it was")
            /// What Comment… says for the whole of element `selector`'s text, selected: nil when it commented.
            let commentOn = { (selector: String) -> String? in
                _ = js("(function () { var r = document.createRange(), s = window.getSelection(); "
                       + "r.selectNodeContents(document.querySelector('\(selector)')); s.removeAllRanges(); s.addRange(r); "
                       + "window.scrollBy(0, r.getBoundingClientRect().top - 40); })()")
                return fromSelection()
            }
            // a selection holding text the mail hides, by its style or by layout, is refused, never quoted without it: a
            // font of no size, no or all but no opacity or colour, the colour of its ground, no display, flattened,
            // filtered, clipped, masked, its content skipped, of no size, folded away; a run of blanks before hidden
            // words
            for cls in ["p1", "p2", "p3", "h_op", "h_col", "h_white", "h_scale", "h_filter", "h_clip", "h_cv", "h_mask",
                        "h_wh", "h_details", "pad"] {
                let said = commentOn("#s8 .\(cls)")
                check(said == "this selection includes text the mail hides; select less", "\(cls): refused: \(said ?? "commented")")
            }
            // text the page adds that is not the mail's (::after content) is refused too
            for cls in ["d_gen"] {
                let said = commentOn("#s8 .\(cls)")
                check(said?.hasPrefix("this selection holds text the page adds") == true, "\(cls): refused: \(said ?? "commented")")
            }
            check(draft.comments.isEmpty, "none of them commented: \(draft.comments.map(\.quote))")
            // a font of half a pixel is drawn at the web view's minimum, so it shows, and is quoted
            check(commentOn("#s8 .h_fs") == nil, "a font made too small to read is drawn readable: commented")
            _ = Renderer.wait(3, step: 0.01) { !draft.comments.isEmpty }
            check(draft.comments.map(\.quote) == ["Visible start. HIDDEN-h_fs Visible end."], "and quoted: \(draft.comments.map(\.quote))")
            // a word an element shows again inside one that hides its text (its size, colour or visibility set back) is
            // shown, so it is quoted
            for cls in ["d_fs", "d_col", "d_vis"] {
                let before = draft.comments.count
                let said = commentOn("#s8 .\(cls)")
                _ = Renderer.wait(3, step: 0.01) { draft.comments.count > before }
                check(said == nil && draft.comments.last?.quote == "Do not delete the bucket.",
                      "\(cls): quoted whole: \(said ?? draft.comments.last?.quote.debugDescription ?? "none")")
            }
            /// What Comment… says for element `selector`'s text, selected as a drag or a triple-click selects it: from its
            /// first text to the end of its last, inside it.
            let commentOnText = { (selector: String) -> String? in
                _ = js("""
                    (function () { var el = document.querySelector('\(selector)'), w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT),
                      n, t = []; while ((n = w.nextNode())) if (n.data.trim()) t.push(n);
                      var r = document.createRange(), s = window.getSelection(), l = t[t.length - 1];
                      r.setStart(t[0], 0); r.setEnd(l, l.data.length); s.removeAllRanges(); s.addRange(r);
                      window.scrollBy(0, r.getBoundingClientRect().top - 40); })()
                    """)
                return fromSelection()
            }
            // text the page adds on the element that holds the selection, or one around it (::before, ::after, a list
            // marker of words), a selection made inside it: refused
            for cls in ["g_before", "g_marker", "g_after"] {
                let said = commentOnText("#s8 .\(cls)")
                check(said?.hasPrefix("this selection holds text the page adds") == true, "\(cls): refused: \(said ?? "commented")")
            }
            // text laid out right to left, or holding right-to-left characters: refused, as its words and numbers could
            // show in another order than the reply sends them
            let rtl = commentOnText("#s8 .v_rtl")
            check(rtl?.hasPrefix("this text is laid out right to left") == true, "right to left: refused: \(rtl ?? "commented")")
            for cls in ["x_swap", "x_lead", "x_arabic"] {
                let said = commentOnText("#s8 .\(cls)")
                check(said == MailComments.rightToLeftWhy, "\(cls): refused: \(said ?? "commented")")
            }
            // the same words and numbers without them: quoted
            let beforeDigits = draft.comments.count
            check(commentOnText("#s8 .x_digits") == nil, "numbers in plain text: commented")
            _ = Renderer.wait(3, step: 0.01) { draft.comments.count > beforeDigits }
            check(draft.comments.contains { $0.quote == "Set the disk quota to '500' '50' GB (new, then old)." },
                  "and quoted: \(draft.comments.map(\.quote))")
            // text a later element covers, text on a border of its own colour, and a closed <details> selected across:
            // refused
            for (cls, from, to) in [("v_cover", "May I tidy up?", "Thanks."), ("v_bar", "ALSO: approve", "push."),
                                    ("v_closed", "Before the details", "After the details.")] {
                _ = js(select("s8", from: from, to: to))
                let said = fromSelection()
                check(said == "this selection includes text the mail hides; select less", "\(cls): refused: \(said ?? "commented")")
            }
            // text under the app's own highlight of other text: refused
            let beforeUnder = draft.comments.count
            _ = js(select("s8", from: "Highlight this", to: "before it."))
            check(fromSelection() == nil, "the line over hidden words: commented")
            _ = Renderer.wait(3, step: 0.01) { draft.comments.count > beforeUnder }
            let over = draft.comments.first { $0.quote.hasPrefix("Highlight this") }?.id ?? ""
            check(Renderer.wait(3, step: 0.01) { marks(over) != "[]" }, "and highlighted")
            _ = js(select("s8", from: "ALSO: sell", to: "box."))
            let under = fromSelection()
            check(under == "this selection includes text the mail hides; select less", "under a highlight: refused: \(under ?? "commented")")
            // the summary of a closed <details>, and the white words on the banner: quoted
            let beforeSummary = draft.comments.count
            for (from, to) in [("Run 42", "finished"), ("Less detail", "Less detail")] {
                _ = js(select("s8", from: from, to: to))
                check(fromSelection() == nil, "\(from): commented")
            }
            _ = Renderer.wait(3, step: 0.01) { draft.comments.count >= beforeSummary + 2 }
            check(Set(draft.comments.map(\.quote)).isSuperset(of: ["Run 42 finished", "Less detail"]),
                  "and quoted: \(draft.comments.map(\.quote))")
            // an open <details>: quoted
            let beforeOpen = draft.comments.count
            check(commentOn("#s8 .v_open p") == nil, "an open <details>: commented")
            _ = Renderer.wait(3, step: 0.01) { draft.comments.count > beforeOpen }
            check(draft.comments.contains { $0.quote == "Inside the details." }, "and quoted: \(draft.comments.map(\.quote))")
            // a scrolling box (a wide <pre>): its end scrolled into view is quoted, its start scrolled out is refused
            _ = js("(function () { var b = document.querySelector('#s8 .v_scroll'); b.scrollLeft = b.scrollWidth; })()")
            let beforeTail = draft.comments.count
            _ = js(select("s8", from: "TAIL", to: "TAIL"))
            check(fromSelection() == nil, "the end of a wide <pre> scrolled into view: commented")
            _ = Renderer.wait(3, step: 0.01) { draft.comments.count > beforeTail }
            check(draft.comments.contains { $0.quote == "TAIL" }, "and quoted: \(draft.comments.map(\.quote))")
            _ = js(select("s8", from: "START", to: "START"))
            let start = fromSelection()
            check(start == "this selection includes text the mail hides; select less", "its start scrolled out: refused: \(start ?? "commented")")
            // a selection off screen: scroll to it first
            _ = js(select("s8", from: "After the banner", to: "banner.") + "; window.scrollTo(0, 0)")
            let off = fromSelection()
            check(off == "scroll so that all of the selection shows, then comment", "off screen: \(off ?? "commented")")
            // a colour this script cannot read as visible (a wide-gamut one fully transparent, or white on white), text
            // over a background image, text moved out of the card, text whose end an element clips: refused
            for cls in ["c_p3", "c_p3w", "b_bgimg", "b_off", "b_tail"] {
                let said = commentOnText("#s8 .\(cls)")
                check(said == "this selection includes text the mail hides; select less", "\(cls): refused: \(said ?? "commented")")
            }
            // a wide-gamut colour that shows is read, and quoted
            let beforeP3 = draft.comments.count
            check(commentOnText("#s8 .plain_p3") == nil, "a wide-gamut colour that shows: commented")
            _ = Renderer.wait(3, step: 0.01) { draft.comments.count > beforeP3 }
            check(draft.comments.contains { $0.quote == "Seen in a wide-gamut colour." }, "and quoted: \(draft.comments.map(\.quote))")
            // no text is drawn under the web view's minimum font size, so a font of 1 pixel shows; a bar of 1-pixel cells
            // keeps its widths and stays a thin bar
            let tiny = js("document.querySelector('#s8 .tiny span').getBoundingClientRect().height") as? Double ?? 0
            let bar = js("JSON.stringify(Array.prototype.map.call(document.querySelectorAll('#s8 .bar td'), function (c) { "
                         + "var r = c.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; }))") as? String
            check(tiny >= Double(MailWeb.minimumFontSize), "a 1-pixel font is drawn at the minimum: \(tiny) points high")
            let cells = (try? JSONDecoder().decode([[Int]].self, from: Data((bar ?? "").utf8))) ?? []
            check(cells.map { $0[0] } == [42, 62] && cells.allSatisfy { $0[1] <= 12 },
                  "a bar of 1-pixel cells keeps its widths (and a pixel of padding each side) and stays thin: \(bar ?? "none")")
            // blanks and ignorable characters: the quote has a space for a blank and none for the rest, and its
            // highlight is still found (the script's BARE and ReplyText.plain agree)
            let beforeBlank = draft.comments.count
            check(commentOn("#s8 .blank") == nil, "blanks: commented")
            _ = Renderer.wait(3, step: 0.01) { draft.comments.count > beforeBlank }
            let blank = draft.comments.first { $0.quote == "Keep the bucket." }
            _ = Renderer.wait(3, step: 0.01) { marks(blank?.id ?? "") != "[]" }
            check(marks(blank?.id ?? "") == "[\"Keep\u{2800}\u{2800}\u{2800}the\u{200B} bucket.\"]",
                  "blanks: quoted plain and highlighted: \(draft.comments.map(\.quote)) \(marks(blank?.id ?? "") ?? "")")
            // the mail's own <mark class="abk-hl"> is not drawn as a highlight
            let yellow = "rgb(\(MailComments.highlight >> 16), \((MailComments.highlight >> 8) & 0xFF), \(MailComments.highlight & 0xFF))"
            check(js("getComputedStyle(document.querySelector('#s8 .p4 mark')).backgroundColor") as? String != yellow,
                  "the mail's own mark is not a highlight")

            // a long mail (200,000 characters): a comment on it is quick to make and to draw again
            _ = js("(function () { var t = document.querySelector('#s9 p').firstChild, r = document.createRange(), "
                   + "s = window.getSelection(); r.setStart(t, 30000); r.setEnd(t, 30800); s.removeAllRanges(); s.addRange(r); "
                   + "window.scrollBy(0, r.getBoundingClientRect().top - 40); })()")
            var started = Date()
            let made = fromSelection()
            let making = Date().timeIntervalSince(started)
            _ = Renderer.wait(3, step: 0.01) { draft.comments.contains { $0.message == "s9" } }
            check(made == nil && making < 0.5 && draft.comments.last?.quote.count == 799 && draft.comments.last?.at == 24000,
                  String(format: "a comment on a long mail made in %.2f s: ", making) + (made ?? "made"))
            started = Date()
            let found = Renderer.evaluate(MailComments.draw, arguments: ["comments": try! MailComments.json(draft.comments.map(CommentAnchor.init))],
                                          in: web, world: MailComments.world, within: 10) as? Int
            let drawing = Date().timeIntervalSince(started)
            check(found == draft.comments.count && drawing < 0.5,
                  String(format: "its highlights drawn again in %.2f s: %d of %d", drawing, found ?? -1, draft.comments.count))

            // the page's own world (where the document's script would run, were it allowed) cannot post a comment
            check(js("typeof (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.\(MailComments.handler))")
                  as? String == "undefined", "the page's own world cannot post a comment")
            check(problems.isEmpty, "nothing went wrong: \(problems)")
            if fails.isEmpty { print("comments selftest: ok"); return 0 }
            fails.forEach { print("FAIL: " + $0) }
            return 1
        }
    }
}
