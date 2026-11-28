import AppKit
import WebKit

// A Forward button on each agent's mail in a thread document, in the mail's header (its <summary>) beside its time, as
// Gmail puts its own: a click asks the desktop for a draft in the owner's Gmail of that one mail, a fresh mail of its
// content, images and attachments with no recipient yet (mail_forward), and opens the draft in that Gmail, for the
// owner to address and send. A small script of the app's own, run in a content world of its own (as MailCopy's and widgets.js
// run in theirs), puts the buttons in and posts a click's message key to the app through a message handler of its own,
// which answers once the draft has opened, or why not; the button says Forwarding… until then. The owner's own mail has
// no button. The page itself still runs no script.

@MainActor
enum MailForward {
    /// The content world the forward script runs in, apart from the page (which runs nothing) and the other scripts.
    static let world = WKContentWorld.world(name: "AgentBoardMailForward")
    /// The message handler a click's message key comes back through; it answers the script
    /// (WKScriptMessageHandlerWithReply).
    static let handler = "mailForward"

    /// The script, at the document's end, in its own world. Each agent's mail (body > .msg, not .mine; the mailroom's
    /// mailview.document) gets a button after its time, in the page's own colours (its CSS variables), so it suits the
    /// light and the dark page. A click neither opens nor folds the mail; while one is under way the button says
    /// Forwarding… and takes no other click; then it says Opened, or Not forwarded (the tab says why), for 2 s.
    static let script = """
    (function () {
      'use strict';
      var SVG = 'http://www.w3.org/2000/svg';
      var STYLE = [
        '.abf-forward { display: inline-flex; align-items: center; gap: 4px; align-self: center;',
        '  font: 12px -apple-system, system-ui, sans-serif; line-height: 18px; color: var(--muted, #5f6368);',
        '  background: transparent; border: 1px solid var(--line, #e0e3e7); border-radius: 6px; padding: 1px 8px;',
        '  cursor: pointer; }',
        '.abf-forward:hover { color: var(--fg, #1f1f1f); border-color: var(--muted, #5f6368); }',
        '.abf-forward.abf-busy { cursor: progress; opacity: 0.75; }',
        '.abf-forward.abf-done { color: #1a7f37; border-color: #1a7f37; }',
        '.abf-forward.abf-failed { color: #c5221f; border-color: #c5221f; }',
        '.abf-forward svg { width: 13px; height: 13px; }'
      ].join('\\n');
      function icon() {
        var s = document.createElementNS(SVG, 'svg');
        s.setAttribute('viewBox', '0 0 16 16');
        s.setAttribute('aria-hidden', 'true');
        ['M9.5 3.5 13.5 7.5 9.5 11.5', 'M13.5 7.5H6.5A4 4 0 0 0 2.5 11.5V13'].forEach(function (d) {
          var p = document.createElementNS(SVG, 'path');
          p.setAttribute('d', d);
          p.setAttribute('fill', 'none');
          p.setAttribute('stroke', 'currentColor');
          p.setAttribute('stroke-width', '1.4');
          p.setAttribute('stroke-linecap', 'round');
          p.setAttribute('stroke-linejoin', 'round');
          s.appendChild(p);
        });
        return s;
      }
      if (!document.getElementById('abf-style')) {
        var style = document.createElement('style');
        style.id = 'abf-style';
        style.textContent = STYLE;
        (document.head || document.documentElement).appendChild(style);
      }
      Array.prototype.forEach.call(document.querySelectorAll('body > .msg:not(.mine) > summary'), function (summary) {
        if (summary.querySelector('.abf-forward')) return;
        var key = summary.parentNode.id;
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'abf-forward';
        button.title = 'Forward: a new mail of this one, its images and files, as a draft in your Gmail';
        var label = document.createElement('span');
        label.textContent = 'Forward';
        button.appendChild(icon());
        button.appendChild(label);
        var busy = false;
        function end(text, mark) {
          label.textContent = text;
          button.classList.remove('abf-busy');
          button.classList.add(mark);
          setTimeout(function () {
            label.textContent = 'Forward';
            button.classList.remove(mark);
            busy = false;
          }, 2000);
        }
        button.addEventListener('click', function (e) {
          e.preventDefault();
          e.stopPropagation();
          if (busy) return;
          busy = true;
          label.textContent = 'Forwarding…';
          button.classList.add('abf-busy');
          window.webkit.messageHandlers.\(handler).postMessage({forward: key})
            .then(function () { end('Opened', 'abf-done'); }, function () { end('Not forwarded', 'abf-failed'); });
        });
        var when = summary.querySelector('.when');
        if (when) when.insertAdjacentElement('afterend', button); else summary.appendChild(button);
      });
    })();
    """

    /// Whether a Forward button's message key may be forwarded: an agent's mail, "s" and its id's digits
    /// (MailLink.isKey).
    static func forwards(_ key: String) -> Bool { key.hasPrefix("s") && MailLink.isKey(key) }

    /// Gmail's page that opens the draft whose message is `message` in its compose window, in the Google account
    /// `account`; nil for a message id that is not one (Gmail's are letters and digits).
    static func draftURL(_ message: String, account: String?) -> URL? {
        guard !message.isEmpty, message.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber) }) else { return nil }
        return Browser.gmail("drafts?compose=" + message, account: account)
    }
}

/// --selftest-forward: the Forward buttons in a real document view (MailWeb's, with its content rules and content
/// worlds), wired to a real MailModel whose desktop is the test: a button on each agent's mail, in its header after its
/// time, and none on the owner's; a click asks for mail_forward of that mail in that thread, leaves the mail folded, says
/// Forwarding… and takes no second click until the answer, then opens the draft in the owner's Gmail account and says
/// Opened; a refusal opens nothing, says Not forwarded, and the tab says why.
enum ForwardTest {
    static let account = "owner@example.com"
    /// A thread as the mailroom's document has it: an agent's mail folded, the owner's reply, the agent's answer open.
    static let document = """
    <!DOCTYPE html><html><head><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="\(MailBlocker.policy)">
    </head><body>
    <h1 class="subject">Run 6 results</h1>
    <details id="s5" class="msg"><summary><b class="from">alpha (claude)</b><span class="to">to Alex</span><span class="when">Fri 20 Nov, 09:12</span><span class="snip">Run 6 finished: accuracy 0.91</span></summary>
    <div class="card"><div class="mail"><p>Run 6 finished: accuracy 0.91.</p></div></div></details>
    <details id="i6" class="msg mine"><summary><b class="from">Alex</b><span class="to">to alpha (claude)</span><span class="when">Fri 20 Nov, 09:30</span><span class="snip">Start run 7</span></summary>
    <div class="card"><div class="text">Start run 7</div></div></details>
    <details id="s7" class="msg" open><summary><b class="from">alpha (claude)</b><span class="to">to Alex</span><span class="when">Fri 20 Nov, 11:02</span><span class="snip">Run 7 is running</span></summary>
    <div class="card"><div class="mail"><p>Run 7 is running.</p></div></div></details>
    </body></html>
    """

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
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        var asked: [(request: [String: Any], done: @MainActor ([String: Any]?, String?) -> Void)] = []
        let mail = MailModel(call: { request, done in asked.append((request, done)) },
                             upload: { _, done in done(nil, "this test copies nothing to the desktop") },
                             ahead: 0, cacheBytes: 0, draftsFolder: folder)
        mail.account = account
        var opened: [(URL, String?)] = []
        mail.openLink = { url, account in opened.append((url, account)); return nil }
        return withExtendedLifetime(win) {
            let loader = MailWeb.loader
            loader.forward = { mail.forward($1, in: $0, done: $2) }
            var problems: [String] = []
            loader.problem = { problems.append($0) }
            loader.refuse = { problems.append("refused: " + $0) }
            loader.show(document, thread: "T1", agentMail: [], widgets: [], draft: nil, current: nil, in: web)
            let js = { (source: String) in Renderer.evaluate(source, in: web, within: 5) }
            let button = { (key: String) in "document.querySelector('#\(key) > summary > .abf-forward')" }
            let label = { (key: String) in js(button(key) + ".textContent") as? String }
            let click = { (key: String) in _ = js(button(key) + ".click()") }
            _ = Renderer.wait(15, step: 0.05) {
                (Renderer.evaluate("document.querySelectorAll('.abf-forward').length", in: web, within: 1) as? Int ?? 0) == 2
            }
            check(js("Array.prototype.map.call(document.querySelectorAll('.abf-forward'), function (b) { return b.parentNode.parentNode.id; }).join(' ')")
                  as? String == "s5 s7", "a button on each agent's mail and none on the owner's")
            check(js("document.querySelectorAll('.when + .abf-forward').length") as? Int == 2, "each after its mail's time")
            check(label("s5") == "Forward", "it says Forward: \(label("s5") ?? "nothing")")

            // a click on the folded mail's button: mail_forward of it in this thread, the mail still folded
            click("s5")
            _ = Renderer.wait(3, step: 0.01) { asked.count == 1 }
            check(asked.count == 1 && asked[0].request["action"] as? String == "mail_forward"
                  && asked[0].request["thread"] as? String == "T1" && asked[0].request["key"] as? String == "s5"
                  && asked[0].request.count == 3, "the click asks for mail_forward of s5 in T1: \(asked.map(\.request))")
            check(label("s5") == "Forwarding…", "and says Forwarding… meanwhile: \(label("s5") ?? "nothing")")
            check(js("document.getElementById('s5').open") as? Bool == false, "the mail stays folded")
            click("s5")
            RunLoop.current.run(until: Date().addingTimeInterval(0.3))
            check(asked.count == 1, "a second click meanwhile asks nothing: \(asked.count) asked")
            if !asked.isEmpty {
                asked.removeFirst().done(["ok": true, "draft": "r-1", "message": "18c0ffee"], nil)
            }
            _ = Renderer.wait(3, step: 0.01) { label("s5") == "Opened" }
            check(label("s5") == "Opened", "the draft opened: \(label("s5") ?? "nothing")")
            check(opened.map(\.0.absoluteString) == ["https://mail.google.com/mail/?authuser=owner@example.com#drafts?compose=18c0ffee"]
                  && opened.first?.1 == account, "in the owner's Gmail, in that account: \(opened)")
            check(mail.notice == nil, "nothing said: \(mail.notice ?? "")")

            // a refusal: nothing opened, the button and the tab say so
            click("s7")
            _ = Renderer.wait(3, step: 0.01) { asked.count == 1 }
            let why = "the draft would be 36.2 MB, more than the 35 MB Gmail takes in one, so none was made"
            if !asked.isEmpty { asked.removeFirst().done(["ok": false, "error": why], nil) }
            _ = Renderer.wait(3, step: 0.01) { label("s7") == "Not forwarded" }
            check(label("s7") == "Not forwarded", "a refusal says Not forwarded: \(label("s7") ?? "nothing")")
            check(mail.notice == "Not forwarded: " + why, "and the tab says why: \(mail.notice ?? "nothing")")
            check(opened.count == 1, "and nothing opens")
            check(js("document.getElementById('s7').open") as? Bool == true, "the open mail stays open")
            _ = Renderer.wait(4, step: 0.05) { label("s5") == "Forward" }
            check(label("s5") == "Forward", "the button says Forward again: \(label("s5") ?? "nothing")")

            // a message id that is not one is not opened
            check(MailForward.draftURL("18c0ffee", account: nil)?.absoluteString
                  == "https://mail.google.com/mail/u/0/#drafts?compose=18c0ffee", "no account: the first signed in")
            check(MailForward.draftURL("x#y", account: account) == nil && MailForward.draftURL("", account: account) == nil,
                  "a message id that is not one gives no page")
            check(MailForward.forwards("s12") && !MailForward.forwards("i6") && !MailForward.forwards("s1x"),
                  "only an agent's mail is forwarded")
            // the page's own world (where the document's script would run, were it allowed) cannot post a forward
            check(js("typeof (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.\(MailForward.handler))")
                  as? String == "undefined", "the page's own world cannot post a forward")
            check(problems.isEmpty, "the document was shown: \(problems)")
            if fails.isEmpty { print("forward selftest: ok"); return 0 }
            fails.forEach { print("FAIL: " + $0) }
            return 1
        }
    }
}
