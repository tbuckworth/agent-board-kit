import AppKit
import Combine
import SwiftUI
import WebKit

// The Mail tab's thread document: the one HTML page the desktop makes of a thread (mailview.document, already
// sanitized there), shown in a WKWebView that runs no page script, loads nothing from anywhere and never navigates.
// Links leave it: web and mailto links open in the default browser or mail app, attachment links fetch the part over
// the board's connection (mail_part) and open the file from a private temporary folder. The app's own scripts run in
// it: the outline and paging below, the agents' widgets (MailWidgets.swift), code blocks' Copy buttons (MailCopy.swift),
// agent mail's Forward buttons (MailForward.swift) and comments on selected text (MailComments.swift), each in a
// content world of its own.

/// A link the owner clicked in a thread document.
enum MailLink: Equatable {
    case web(URL)                          // http, https or mailto
    case part(key: String, index: Int)     // mailbox-part:KEY/INDEX, attachment INDEX (from 1) of message KEY (MailLink.isKey)
    case other(URL)                        // anything else, which does not open

    init(_ url: URL) {
        switch url.scheme?.lowercased() {
        case "http", "https", "mailto":
            self = .web(url)
        case "mailbox-part":
            let spec = url.absoluteString.dropFirst("mailbox-part:".count)
            let bits = spec.split(separator: "/", omittingEmptySubsequences: false)
            if bits.count == 2, MailLink.isKey(bits[0]), !bits[1].isEmpty,
               bits[1].allSatisfy({ $0.isASCII && $0.isNumber }), let index = Int(bits[1]), index >= 1 {
                self = .part(key: String(bits[0]), index: index)
            } else {
                self = .other(url)
            }
        default:
            self = .other(url)
        }
    }

    /// A message key as the mailroom writes it: "s" (an agent's mail) or "i" (the owner's) and its id's digits.
    static func isKey<S: StringProtocol>(_ s: S) -> Bool {
        guard let first = s.first, first == "s" || first == "i", s.count > 1 else { return false }
        return s.dropFirst().allSatisfy { $0.isASCII && $0.isNumber }
    }
}

/// What the document view does with a navigation: only its own load of a document goes ahead; a clicked link is
/// stopped and followed outside the view; anything else is stopped.
enum MailNavigation: Equatable {
    case load                     // the view's own loadHTMLString, which it is `expecting`
    case follow(MailLink)
    case cancel

    static func decide(expecting: Bool, type: WKNavigationType, mainFrame: Bool, url: URL?) -> MailNavigation {
        if expecting, type == .other, mainFrame, url?.absoluteString == "about:blank" { return .load }
        if type == .linkActivated, let u = url { return .follow(MailLink(u)) }
        return .cancel
    }
}

/// The content rules every document view loads under: every load is blocked except data: URLs (the document's own
/// images, which the desktop inlines). Compiled once, on first use.
@MainActor
enum MailBlocker {
    /// The thread document's Content-Security-Policy (agentmail/mailview.py's CSP), for the app's own pages: the
    /// enlarged item's (MailEnlarge) and the self-tests'.
    nonisolated static let policy = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'"
    static let rules = """
    [{"trigger": {"url-filter": ".*"}, "action": {"type": "block"}},
     {"trigger": {"url-filter": "^data:"}, "action": {"type": "ignore-previous-rules"}}]
    """
    private static var compiled: Result<WKContentRuleList, Error>?
    private static var waiting: [(Result<WKContentRuleList, Error>) -> Void] = []

    /// `done` gets the compiled rules, or why they did not compile; at once when they already have.
    static func whenReady(_ done: @escaping (Result<WKContentRuleList, Error>) -> Void) {
        if let r = compiled { done(r); return }
        waiting.append(done)
        guard waiting.count == 1 else { return }
        WKContentRuleListStore.default().compileContentRuleList(forIdentifier: "AgentBoardMail", encodedContentRuleList: rules) { list, error in
            MainActor.assumeIsolated {
                let r: Result<WKContentRuleList, Error> = list.map { .success($0) }
                    ?? .failure(error ?? NSError(domain: "AgentBoard", code: 1, userInfo: [NSLocalizedDescriptionKey: "no rule list and no error"]))
                compiled = r
                let all = waiting
                waiting = []
                all.forEach { $0(r) }
            }
        }
    }
}

/// The thread document in `web`, a web view of MailWeb's: the tab's, or a popped-out thread's window's own. A document
/// that is the same as the one showing is not loaded again, so its scroll position and open messages stay. The message
/// `current` (the one being read) is outlined, opened and brought to the top, once the document has loaded and whenever
/// it changes; each new `page` scrolls a screen down or up. Its `widgets` are drawn once it has loaded, showing the
/// answers in its `draft`, and the text its comments quote is highlighted; both follow the draft, so the tab and a
/// window showing the same thread show the same answers and highlights. Files dropped on it go to `take`, for the reply.
struct MailDocument: NSViewRepresentable {
    let html: String
    let thread: String                    // whose document it is: its Forward buttons forward this thread's mail
    let agentMail: Set<String>            // the keys of the agents' mails in it: a comment quotes only these
    let widgets: [MailWidget]
    let draft: Draft                      // the thread's reply: options picked and text commented on go into it
    let current: String?
    let page: MailPage?
    let zoom: CGFloat
    let web: MailWebView                  // the web view it shows in, with its own loader
    let follow: (MailLink) -> Void        // a link was clicked
    let refuse: (String) -> Void          // the document can't be shown safely, and why
    let problem: (String) -> Void         // a widget was not drawn, a block not copied, a comment not taken, and why
    let take: (NSPasteboard) -> Bool      // files dropped on it: whether they were taken
    let forward: MailDocumentLoader.Forward   // a Forward button was clicked

    func makeNSView(context: Context) -> MailWebView { web }

    func updateNSView(_ web: MailWebView, context: Context) {
        let loader = web.loader
        loader.follow = follow
        loader.refuse = refuse
        loader.problem = problem
        loader.forward = forward
        web.take = take
        if web.pageZoom != zoom { web.pageZoom = zoom }
        loader.show(html, thread: thread, agentMail: agentMail, widgets: widgets, draft: draft, current: current,
                    in: web)
        loader.turn(page, in: web)
    }
}

/// The web views thread documents show in. The tab's (`view`) is made on first use and kept while the app runs: a new
/// WKWebView starts a WebKit process, a third of a second or more before its first document shows, so it is not made
/// anew each time the tab comes back, and `warm` starts it when the tab appears, before a thread is opened. Each
/// popped-out thread's window (MailWindows) has one of its own, from `make`, which goes with the window.
@MainActor
enum MailWeb {
    static let view = make()
    /// The tab's web view's loader.
    static var loader: MailDocumentLoader { view.loader }
    static let blank = "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body></body></html>"
    /// The smallest font a thread document is drawn in, whatever its mail asks (its CSS, or a <font size=> attribute):
    /// no text is too small to read.
    static let minimumFontSize: CGFloat = 9

    /// A new web view with a loader of its own, locked down and with the app's scripts in their content worlds.
    static func make() -> MailWebView {
        let loader = MailDocumentLoader()
        let config = WKWebViewConfiguration()
        config.defaultWebpagePreferences.allowsContentJavaScript = false
        config.preferences.minimumFontSize = minimumFontSize
        config.websiteDataStore = .nonPersistent()
        if case .success(let source) = MailWidgets.script {      // when it is missing, drawing says so (MailDocumentLoader)
            for script in [source, MailWidgets.enlarge] {
                config.userContentController.addUserScript(WKUserScript(source: script, injectionTime: .atDocumentEnd,
                                                                        forMainFrameOnly: true, in: MailWidgets.world))
            }
        }
        config.userContentController.add(loader, contentWorld: MailWidgets.world, name: MailWidgets.handler)
        config.userContentController.addUserScript(WKUserScript(source: MailCopy.script, injectionTime: .atDocumentEnd,
                                                                forMainFrameOnly: true, in: MailCopy.world))
        config.userContentController.addScriptMessageHandler(loader, contentWorld: MailCopy.world, name: MailCopy.handler)
        config.userContentController.addUserScript(WKUserScript(source: MailForward.script, injectionTime: .atDocumentEnd,
                                                                forMainFrameOnly: true, in: MailForward.world))
        config.userContentController.addScriptMessageHandler(loader, contentWorld: MailForward.world, name: MailForward.handler)
        config.userContentController.addUserScript(WKUserScript(source: MailComments.script, injectionTime: .atDocumentEnd,
                                                                forMainFrameOnly: true, in: MailComments.world))
        config.userContentController.add(loader, contentWorld: MailComments.world, name: MailComments.handler)
        let web = MailWebView(loader: loader, configuration: config)
        web.comment = { [weak web] in if let web = web { web.loader.commentOnSelection(in: web) } }
        web.navigationDelegate = loader
        web.allowsBackForwardNavigationGestures = false
        web.setValue(false, forKey: "drawsBackground")      // the board's own background shows behind the document
        return web
    }

    /// Start the tab's web view's process with an empty page, unless it has had a document already.
    static func warm() {
        guard !loader.started else { return }
        loader.show(blank, thread: "", agentMail: [], widgets: [], draft: nil, current: nil, in: view)
    }
}

/// The document's web view, which hands files dropped on it (Attachments) to `take` instead of opening them, and whose
/// right-click menu starts with Comment… while the selection is text a comment can quote (MailComments). Its `loader`
/// loads its documents and answers its scripts; the view keeps it.
final class MailWebView: WKWebView {
    let loader: MailDocumentLoader
    var take: ((NSPasteboard) -> Bool)?
    /// Whether the selection is text of an agent's mail that a comment can quote, as the comments script last said.
    var commentable = false
    /// What Comment… does.
    var comment: (() -> Void)?

    init(loader: MailDocumentLoader, configuration: WKWebViewConfiguration) {
        self.loader = loader
        super.init(frame: .zero, configuration: configuration)
    }

    /// Never made from an archive.
    required init?(coder: NSCoder) { nil }

    override func willOpenMenu(_ menu: NSMenu, with event: NSEvent) {
        super.willOpenMenu(menu, with: event)
        guard commentable else { return }
        let item = NSMenuItem(title: "Comment…", action: #selector(commentOnSelection(_:)), keyEquivalent: "")
        item.target = self
        menu.insertItem(item, at: 0)
        menu.insertItem(.separator(), at: 1)
    }

    @objc func commentOnSelection(_ sender: Any?) { comment?() }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        Attachments.holds(sender.draggingPasteboard) ? .copy : super.draggingEntered(sender)
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        Attachments.holds(sender.draggingPasteboard) ? .copy : super.draggingUpdated(sender)
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        if Attachments.holds(sender.draggingPasteboard), take?(sender.draggingPasteboard) == true { return true }
        return super.performDragOperation(sender)
    }
}

/// Loads documents into one web view, decides every navigation in it (MailNavigation): a clicked link goes to
/// `follow`, and runs the app's own scripts in it (the page runs none of its own): the outline on the message being
/// read, paging, the widgets, whose answers go into the thread's draft (WKScriptMessageHandler), the comments on
/// selected text, which do too, the code blocks' Copy buttons, each answered with whether the pasteboard took the text,
/// and agent mail's Forward buttons, each answered once its draft has opened or why not
/// (WKScriptMessageHandlerWithReply). The cards and highlights follow the draft's answers and comments, so an × in
/// the reply box or Discard clears them.
@MainActor
final class MailDocumentLoader: NSObject, WKNavigationDelegate, WKScriptMessageHandler, WKScriptMessageHandlerWithReply {
    /// Forward agent mail (second) of thread (first), then call back with nil once its draft has opened, else why not.
    typealias Forward = (String, String, @escaping @MainActor (String?) -> Void) -> Void

    var follow: (MailLink) -> Void = { _ in }
    var refuse: (String) -> Void = { _ in }
    var problem: (String) -> Void = { _ in }
    /// Where a code block's Copy button puts its text: the general pasteboard (a test's own, or a refusal, in its test).
    var copyText: (String) -> String? = { MailCopy.write($0, to: .general) }
    /// What a Forward button asks for: MailModel.forward, which MailDocument sets (a test sets its own).
    var forward: Forward = { _, _, done in done("the Mail tab has not wired its Forward buttons") }
    private var html: String?            // the document asked for last
    private var thread = ""              // whose it is
    private var agentMail: Set<String> = []  // its agents' mails' keys
    private var widgets: [MailWidget] = []   // its widgets
    private var navigation: WKNavigation?    // its load: only that one's end makes it ready
    private var draft: Draft?            // its thread's reply
    private var watching: [AnyCancellable] = []   // on the draft's answers and comments
    private(set) var ready = false       // it has loaded
    private var current: String?         // the message to outline, a key (MailLink.isKey, so safe in a script)
    private var marked: String?          // the message outlined in the document as it is
    private var paged: Int?              // the last MailPage done
    private var loading = false          // the next navigation is our loadHTMLString
    private var blocking = false         // the content rules are on this web view

    /// Whether a document has been asked for.
    var started: Bool { html != nil }

    func show(_ html: String, thread: String, agentMail: Set<String>, widgets: [MailWidget], draft: Draft?,
              current: String?, in web: WKWebView) {
        self.current = current.flatMap { MailLink.isKey($0) ? $0 : nil }
        guard html != self.html else {
            if ready { mark(in: web) }
            return
        }
        self.html = html
        self.thread = thread
        self.agentMail = agentMail
        self.widgets = widgets
        navigation = nil
        ready = false
        marked = nil
        follow(draft, in: web)
        (web as? MailWebView)?.commentable = false
        MailBlocker.whenReady { [weak self, weak web] result in
            guard let self = self, let web = web, self.html == html else { return }
            switch result {
            case .failure(let error):
                self.refuse("The thread is not shown: WebKit did not compile the rules that block its loads (\(error.localizedDescription)).")
            case .success(let rules):
                if !self.blocking {
                    web.configuration.userContentController.add(rules)
                    self.blocking = true
                }
                self.loading = true
                self.navigation = web.loadHTMLString(html, baseURL: nil)
            }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 preferences: WKWebpagePreferences) async -> (WKNavigationActionPolicy, WKWebpagePreferences) {
        preferences.allowsContentJavaScript = false
        switch MailNavigation.decide(expecting: loading, type: action.navigationType,
                                     mainFrame: action.targetFrame?.isMainFrame == true, url: action.request.url) {
        case .load:
            loading = false
            return (.allow, preferences)
        case .follow(let link):
            follow(link)
            return (.cancel, preferences)
        case .cancel:
            return (.cancel, preferences)
        }
    }

    /// The document asked for last has loaded; an earlier one's load, ending late, is not it, so its scripts' posts
    /// do not reach this thread's draft.
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard let mine = self.navigation, navigation === mine else { return }
        ready = true
        mark(in: webView)
        draw(in: webView)
        if let comments = draft?.comments, !comments.isEmpty { highlight(comments.map(CommentAnchor.init), in: webView) }
    }

    /// Keep the document's cards and highlights with `draft`'s answers and comments as they change (a comment's own
    /// text aside, which the document does not show).
    private func follow(_ draft: Draft?, in web: WKWebView) {
        self.draft = draft
        watching = []
        guard let d = draft else { return }
        watching = [
            d.$picks.removeDuplicates().sink { [weak self, weak web] picks in
                guard let self = self, let web = web, self.ready, !self.widgets.isEmpty else { return }
                self.showPicks(picks, in: web)
            },
            d.$comments.map { $0.map(CommentAnchor.init) }.removeDuplicates().sink { [weak self, weak web] anchors in
                guard let self = self, let web = web, self.ready else { return }
                self.highlight(anchors, in: web)
            },
        ]
    }

    /// Run `source` in `world` with `arguments` (JSON text); when they cannot be made or the script fails, `problem`
    /// says `failure` and why.
    private func run(_ source: String, _ arguments: () throws -> [String: String], in web: WKWebView,
                     world: WKContentWorld, failure: String) {
        let args: [String: String]
        do { args = try arguments() } catch {
            return problem("\(failure): \(error.localizedDescription)")
        }
        web.callAsyncJavaScript(source, arguments: args, in: nil, in: world) { [weak self] result in
            if case .failure(let error) = result { self?.problem("\(failure): \(error.localizedDescription)") }
        }
    }

    /// Draw the document's widgets with widgets.js, in its content world, showing the draft's answers. A widget it
    /// cannot draw keeps the plain text the mailroom put in its place, and `problem` says why.
    private func draw(in web: WKWebView) {
        guard !widgets.isEmpty else { return }
        let failure = "The options and plots in this thread are shown as plain text"
        switch MailWidgets.script {
        case .failure(let error):
            problem("\(failure): \(error.localizedDescription)")
        case .success:
            let picks = draft?.picks ?? []
            run(MailWidgets.draw, { ["widgets": try MailWidgets.json(self.widgets), "picks": try MailWidgets.json(picks: picks)] },
                in: web, world: MailWidgets.world, failure: failure)
        }
    }

    /// Show the answers `picks` in the document's cards, and none in the rest (widgets.js's mark).
    private func showPicks(_ picks: [DraftPick], in web: WKWebView) {
        run(MailWidgets.mark, { ["picks": try MailWidgets.json(picks: picks)] }, in: web, world: MailWidgets.world,
            failure: "The options picked are not shown in the mail")
    }

    /// Highlight the text the comments `anchors` quote, in the comments script's world.
    private func highlight(_ anchors: [CommentAnchor], in web: WKWebView) {
        run(MailComments.draw, { ["comments": try MailComments.json(anchors)] }, in: web, world: MailComments.world,
            failure: "The text commented on is not highlighted")
    }

    /// Comment… in the right-click menu: a comment on the selection, or `problem` says why there is none.
    func commentOnSelection(in web: WKWebView) {
        web.callAsyncJavaScript(MailComments.fromSelection, arguments: [:], in: nil,
                                in: MailComments.world) { [weak self] result in
            switch result {
            case .success(let why as String): self?.problem("Not commented: " + why)
            case .success: break
            case .failure(let error): self?.problem("Not commented: \(error.localizedDescription)")
            }
        }
    }

    /// What the widgets' and the comments' scripts post, only the main frame's, in their own worlds, for the document
    /// loaded: an answer (MailWidgets.answer) or a comment (MailComments.comment) for the draft, a plot, table or image
    /// to open enlarged in a window of its own (MailEnlarge), a highlight clicked (its comment's field takes the
    /// keyboard), whether the selection can be commented on, or a problem. One that does
    /// not check out, or that a frozen draft does not take, is not taken, and `problem` says so; the cards then show
    /// the draft's answers again.
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, ready, let body = message.body as? [String: Any] else { return }
        if message.world == MailWidgets.world {
            if let raw = body["pick"] {
                guard let got = MailWidgets.answer(raw, of: widgets) else {
                    return problem("An option picked was not taken: widgets.js posted an answer not understood")
                }
                if let d = draft, !d.pick(got.answer, comment: got.comment), let web = message.webView {
                    problem("An option picked was not taken. " + Draft.frozenWhy)
                    showPicks(d.picks, in: web)
                }
            } else if let item = body["enlarge"] as? [String: Any] {
                if let why = MailEnlarge.open(item, widgets: widgets, problem: { [weak self] in self?.problem($0) }) {
                    problem("Not enlarged: " + why)
                }
            } else if let why = body["problem"] as? String {
                problem("Shown as plain text: " + String(why.prefix(300)))
            }
        } else if message.world == MailComments.world {
            if let raw = body["comment"] {
                let (got, why) = MailComments.comment(raw, of: agentMail)
                if let c = got, let d = draft, !d.addComment(message: c.message, at: c.at, quote: c.quote) {
                    problem("Not commented. " + Draft.frozenWhy)
                }
                if let w = why { problem("Not commented: " + w) }
            } else if let id = body["focus"] as? String, let d = draft, d.comments.contains(where: { $0.id == id }) {
                d.focus = id
            } else if let on = body["selection"] as? Bool {
                (message.webView as? MailWebView)?.commentable = on
            } else if let why = body["problem"] as? String {
                problem("Not commented: " + String(why.prefix(300)))
            }
        }
    }

    /// A click on a button of the app's own, only the main frame's, each in its own script's world, answering the
    /// script with whether it was done, or why not. A code block's text from its Copy button (MailCopy) is put on the
    /// pasteboard (copyText); a refusal is also said through `problem`. An agent mail's key from its Forward button
    /// (MailForward) is forwarded from the thread loaded (`forward`), which says itself why it was not.
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        let body = message.body as? [String: Any]
        guard message.frameInfo.isMainFrame, ready else { return replyHandler(nil, "not from this thread's document") }
        if message.world == MailCopy.world, let text = body?["copy"] as? String {
            if let why = MailCopy.copy(text, with: copyText) {
                problem("Not copied: " + why)
                return replyHandler(nil, why)
            }
            replyHandler(true, nil)
        } else if message.world == MailForward.world, let key = body?["forward"] as? String, MailForward.forwards(key),
                  !thread.isEmpty {
            forward(thread, key) { why in replyHandler(why == nil ? true : nil, why) }
        } else {
            replyHandler(nil, "not a copy or a forward from this thread's document")
        }
    }

    /// Outline the message being read, open it and bring it to the top, unless it is outlined already.
    private func mark(in web: WKWebView) {
        guard let key = current, key != marked else { return }
        marked = key
        web.evaluateJavaScript(MailDocumentLoader.markScript(key))
    }

    /// The script that makes `key` (a message key, MailLink.isKey) the message being read: the class "current" (the
    /// document's outline, mailview.py) on it alone, and only when the thread has more than one message; brought to
    /// the top, or for the first message the page's top, so that the subject heading shows.
    static func markScript(_ key: String) -> String {
        """
        (function () {
          var all = document.querySelectorAll('body > .msg'), m = document.getElementById('\(key)');
          for (var i = 0; i < all.length; i++) all[i].classList.remove('current');
          if (!m) return;
          if (all.length > 1) m.classList.add('current');
          m.open = true;
          if (m === all[0]) window.scrollTo(0, 0); else m.scrollIntoView({block: 'start'});
        })();
        """
    }

    /// Scroll a screen (less a little, so a line carries over) the way `page` says, once per request.
    func turn(_ page: MailPage?, in web: WKWebView) {
        guard let p = page, p.n != paged else { return }
        paged = p.n
        web.evaluateJavaScript("window.scrollBy(0, \(p.by) * window.innerHeight * 0.85);")
    }
}

/// Attachments the owner opens, written where only they can read them.
enum MailFiles {
    /// AgentBoard-mail in this user's temporary folder, which macOS empties of old files by itself.
    static var folder: URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("AgentBoard-mail", isDirectory: true)
    }

    /// A name macOS can use for an attachment's file: folder separators, colons and control characters become "_",
    /// leading dots go, and a name over 120 characters is cut, keeping its extension; "attachment" when nothing is left.
    static func fileName(_ name: String) -> String {
        let bad = CharacterSet(charactersIn: "/\\:").union(.controlCharacters).union(.newlines)
        var s = String(String.UnicodeScalarView(name.unicodeScalars.map { bad.contains($0) ? "_" : $0 }))
        s = s.trimmingCharacters(in: .whitespaces)
        while s.hasPrefix(".") { s.removeFirst() }
        if s.count > 120 {
            let ext = (s as NSString).pathExtension
            let keep = ext.isEmpty || ext.count > 10 ? "" : "." + ext
            s = String(s.prefix(120 - keep.count)) + keep
        }
        return s.isEmpty ? "attachment" : s
    }

    /// Write a part to FOLDER/KEY-INDEX/NAME: the folders readable by this user only, the file too, and marked as
    /// downloaded (quarantined) so macOS asks before running anything it holds. Throws when any step fails.
    static func save(_ data: Data, name: String, key: String, index: Int, in root: URL = folder) throws -> URL {
        let dir = root.appendingPathComponent("\(key)-\(index)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        var url = dir.appendingPathComponent(fileName(name))
        try data.write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        var values = URLResourceValues()
        values.quarantineProperties = [kLSQuarantineTypeKey as String: kLSQuarantineTypeOtherDownload as String,
                                       kLSQuarantineAgentNameKey as String: "Agent Board"]
        try url.setResourceValues(values)
        return url
    }
}
