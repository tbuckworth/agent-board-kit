import AppKit
import WebKit

// Copy buttons on the code blocks of a thread document: agents put the commands they ask the owner to run in fenced
// blocks, which the mailroom's document shows as <pre>. A small script of the app's own, run in a content world of
// its own (as widgets.js runs in its), puts a Copy button at the top right of every <pre>; a click posts the block's
// exact text to the app through a message handler of its own, which writes it to the pasteboard (WebKit's
// navigator.clipboard is not dependable in a WKWebView) and answers whether it did; the button says Copied, or Not
// copied and the tab says why. The page itself still runs no script.

@MainActor
enum MailCopy {
    /// The content world the copy script runs in, apart from the page (which runs nothing) and from widgets.js.
    static let world = WKContentWorld.world(name: "AgentBoardMailCopy")
    /// The message handler a block's text comes back through; it answers the script (WKScriptMessageHandlerWithReply).
    static let handler = "mailCopy"
    /// The longest block a click copies, in UTF-16 code units (as the script counts text): a longer one is not
    /// copied, and the button and the tab say so.
    static let maxText = 1 << 20

    /// The script, at the document's end, in its own world. Each <pre> goes into a box of its own with the button
    /// beside it, so the block's text stays exactly its own; the block is padded at the top so the button covers none
    /// of it. The button's colours are its own, so it shows on a light or a dark block alike. A click copies the
    /// block's text without its last new line and says Copied for 1.5 s; the phone's page has the same rule (codeBlock
    /// in web/mail/app.js): change both together.
    static let script = """
    (function () {
      'use strict';
      var SVG = 'http://www.w3.org/2000/svg';
      var STYLE = [
        '.abc-pre { position: relative; }',
        '.abc-pre > pre { padding-top: 30px !important; }',
        '.abc-copy { position: absolute; top: 4px; right: 4px; display: inline-flex; align-items: center; gap: 4px;',
        '  font: 12px -apple-system, system-ui, sans-serif; line-height: 18px; color: #1f2328; background: #ffffff;',
        '  border: 1px solid #c9d1d9; border-radius: 6px; padding: 1px 8px; cursor: pointer; opacity: 0.92; }',
        '.abc-copy:hover { background: #f3f4f6; opacity: 1; }',
        '.abc-copy.abc-done { color: #1a7f37; border-color: #1a7f37; }',
        '.abc-copy svg { width: 13px; height: 13px; }'
      ].join('\\n');
      function icon() {
        var s = document.createElementNS(SVG, 'svg');
        s.setAttribute('viewBox', '0 0 16 16');
        s.setAttribute('aria-hidden', 'true');
        [['rect', {x: 5, y: 1.5, width: 6, height: 3, rx: 1}],
         ['path', {d: 'M5 3H3.5A1.5 1.5 0 0 0 2 4.5v9A1.5 1.5 0 0 0 3.5 15h9a1.5 1.5 0 0 0 1.5-1.5v-9A1.5 1.5 0 0 0 12.5 3H11'}]]
          .forEach(function (p) {
            var e = document.createElementNS(SVG, p[0]);
            for (var k in p[1]) e.setAttribute(k, String(p[1][k]));
            e.setAttribute('fill', 'none');
            e.setAttribute('stroke', 'currentColor');
            e.setAttribute('stroke-width', '1.4');
            s.appendChild(e);
          });
        return s;
      }
      if (!document.getElementById('abc-style')) {
        var style = document.createElement('style');
        style.id = 'abc-style';
        style.textContent = STYLE;
        (document.head || document.documentElement).appendChild(style);
      }
      Array.prototype.forEach.call(document.querySelectorAll('pre'), function (pre) {
        if (pre.parentNode.classList && pre.parentNode.classList.contains('abc-pre')) return;
        var box = document.createElement('div');
        box.className = 'abc-pre';
        pre.parentNode.insertBefore(box, pre);
        box.appendChild(pre);
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'abc-copy';
        button.title = 'Copy to the clipboard';
        var label = document.createElement('span');
        label.textContent = 'Copy';
        button.appendChild(icon());
        button.appendChild(label);
        var timer = null;
        function say(text, done) {
          label.textContent = text;
          button.classList.toggle('abc-done', done);
          clearTimeout(timer);
          timer = setTimeout(function () { label.textContent = 'Copy'; button.classList.remove('abc-done'); }, 1500);
        }
        button.addEventListener('click', function (e) {
          e.preventDefault();
          e.stopPropagation();
          window.webkit.messageHandlers.\(handler).postMessage({copy: pre.textContent.replace(/\\n$/, '')})
            .then(function () { say('Copied', true); }, function () { say('Not copied', false); });
        });
        box.appendChild(button);
      });
    })();
    """

    /// Put `text` on `pasteboard` as plain text, in place of what was there: nil when it is there, else why not.
    static func write(_ text: String, to pasteboard: NSPasteboard) -> String? {
        pasteboard.clearContents()
        return pasteboard.setString(text, forType: .string) ? nil : "the pasteboard did not take it"
    }

    /// A block's text from a Copy button, put on the pasteboard by `write` (MailDocumentLoader.copyText): nil when it
    /// was, else why not (it is longer than maxText, or the write failed).
    static func copy(_ text: String, with write: (String) -> String?) -> String? {
        let n = text.utf16.count
        guard n <= maxText else { return "the block is \(n) characters, more than the \(maxText) a copy takes" }
        return write(text)
    }
}

/// --selftest-copy: the copy script in a real document view (MailWeb's, with its content rules and content worlds): a
/// button on every <pre>, clear of its first line; a click puts the block's exact text on the pasteboard (a pasteboard
/// of the test's own, not the general one) and says "Copied" for a moment; a block too long, or a write that fails,
/// says "Not copied" and the tab is told why.
enum CopyTest {
    static let multiLine = "rsync -a --partial \\\n  results/ \"$HOME/backups/run 7/\"\n"
        + "echo \"<tag> & 'quotes' `ticks` \\\\back\" \t tab — ünïcode 🙂"
    /// Two blocks as an agent's mail has them, and a third longer than a copy takes.
    @MainActor
    static var document: String {
        """
        <!DOCTYPE html><html><head><meta charset="utf-8">
        <meta http-equiv="Content-Security-Policy" content="\(MailBlocker.policy)">
        </head><body>
        <details class="msg" id="s5" open><summary>agent</summary><div class="card"><div class="mail">
        <p>Run this:</p>
        <pre><code>rsync -a --partial \\
          results/ "$HOME/backups/run 7/"
        echo "&lt;tag&gt; &amp; 'quotes' `ticks` \\\\back" \t tab — ünïcode 🙂
        </code></pre>
        <p>and then:</p>
        <pre style="background:#1f2328;color:#e6edf3">agent-mail inbox</pre>
        <pre>\(String(repeating: "x", count: MailCopy.maxText + 1))</pre>
        </div></div></details>
        </body></html>
        """
    }

    @MainActor
    static func run() -> Int32 {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append(what) } }
        let web = MailWeb.view
        web.frame = NSRect(x: 0, y: 0, width: 760, height: 600)
        let win = NSWindow(contentRect: web.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        win.contentView = web
        win.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        win.orderBack(nil)
        let board = NSPasteboard(name: NSPasteboard.Name("agentboard-copytest-\(UUID().uuidString)"))
        defer { board.releaseGlobally() }
        board.clearContents()
        return withExtendedLifetime(win) {
            let loader = MailWeb.loader
            loader.copyText = { MailCopy.write($0, to: board) }
            var problems: [String] = []
            loader.problem = { problems.append($0) }
            loader.refuse = { problems.append("refused: " + $0) }
            loader.show(document, thread: "T1", agentMail: [], widgets: [], draft: nil, current: nil, in: web)
            let js = { (source: String) in Renderer.evaluate(source, in: web, within: 5) }
            let label = { (i: Int) in js("document.querySelectorAll('.abc-copy')[\(i)].textContent") as? String }
            let click = { (i: Int) in _ = js("document.querySelectorAll('.abc-copy')[\(i)].click()") }
            _ = Renderer.wait(15, step: 0.05) {
                (Renderer.evaluate("document.querySelectorAll('.abc-copy').length", in: web, within: 1) as? Int ?? 0) == 3
            }
            check(js("document.querySelectorAll('.abc-copy').length") as? Int == 3, "a button on each of the three blocks")
            check(js("document.querySelectorAll('.abc-pre > pre + .abc-copy').length") as? Int == 3,
                  "each button beside its block, not in it")
            check(js("""
                Array.prototype.every.call(document.querySelectorAll('.abc-pre'), function (box) {
                  var pre = box.querySelector('pre'), b = box.querySelector('.abc-copy');
                  return pre.getBoundingClientRect().top + parseFloat(getComputedStyle(pre).paddingTop) >= b.getBoundingClientRect().bottom;
                })
                """) as? Bool == true, "no button covers its block's first line")
            // a click: the block's exact text on the pasteboard, its trailing new line left off, and "Copied" a moment
            click(0)
            _ = Renderer.wait(3, step: 0.01) { label(0) == "Copied" }
            check(board.string(forType: .string) == multiLine,
                  "the first block, exactly: \(board.string(forType: .string).debugDescription)")
            check(label(0) == "Copied", "it says Copied once the app has it: \(label(0) ?? "nothing")")
            click(1)
            _ = Renderer.wait(3, step: 0.01) { board.string(forType: .string) == "agent-mail inbox" }
            check(board.string(forType: .string) == "agent-mail inbox", "the second block replaces it")
            _ = Renderer.wait(3, step: 0.05) { label(0) == "Copy" }
            check(label(0) == "Copy", "and says Copy again")
            check(js("document.querySelector('pre').textContent.indexOf('Copy') < 0") as? Bool == true,
                  "the block's own text holds no button text")
            // a block too long for a copy: not copied, and said
            click(2)
            _ = Renderer.wait(5, step: 0.02) { label(2) == "Not copied" }
            check(label(2) == "Not copied", "a block too long says Not copied: \(label(2) ?? "nothing")")
            check(board.string(forType: .string) == "agent-mail inbox", "and leaves the pasteboard as it was")
            check(problems.contains { $0.hasPrefix("Not copied: the block is \(MailCopy.maxText + 1) characters") },
                  "the tab says why: \(problems)")
            // a write that fails (a real pasteboard does not refuse on demand, so a writer that says no stands in)
            loader.copyText = { _ in "the pasteboard did not take it" }
            click(1)
            _ = Renderer.wait(3, step: 0.01) { label(1) == "Not copied" }
            check(label(1) == "Not copied", "a failed write says Not copied: \(label(1) ?? "nothing")")
            check(problems.last == "Not copied: the pasteboard did not take it", "and the tab says why: \(problems)")
            // the page's own world (where the document's script would run, were it allowed) has neither handler
            check(js("typeof (window.webkit && window.webkit.messageHandlers && (window.webkit.messageHandlers.\(MailCopy.handler) || window.webkit.messageHandlers.\(MailWidgets.handler)))")
                  as? String == "undefined", "the page's own world cannot post a copy")
            check(!problems.contains { $0.hasPrefix("refused") }, "the document was shown: \(problems)")
            if fails.isEmpty { print("copy selftest: ok"); return 0 }
            fails.forEach { print("FAIL: " + $0) }
            return 1
        }
    }
}
