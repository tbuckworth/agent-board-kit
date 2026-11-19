import AppKit
import SwiftUI
import WebKit

// The Mail tab's thread document: the one HTML page the desktop makes of a thread (mailview.document, already
// sanitized there), shown in a WKWebView that runs no page script, loads nothing from anywhere and never navigates.
// Links leave it: web and mailto links open in the default browser or mail app, attachment links fetch the part over
// the board's connection (mail_part) and open the file from a private temporary folder. The app's own scripts run in
// it: the outline and paging below, and the agents' widgets (MailWidgets.swift) in their own content world.

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

/// The thread document in MailWeb's web view. A document that is the same as the one showing is not loaded again, so
/// its scroll position and open messages stay. The message `current` (the one being read) is outlined, opened and
/// brought to the top, once the document has loaded and whenever it changes; each new `page` scrolls a screen down or
/// up. Its `widgets` are drawn once it has loaded. Files dropped on it go to `take`, for the reply.
struct MailDocument: NSViewRepresentable {
    let html: String
    let thread: String                    // whose document it is: a choice is for this thread's reply
    let widgets: [MailWidget]
    let current: String?
    let page: MailPage?
    let zoom: CGFloat
    let follow: (MailLink) -> Void        // a link was clicked
    let refuse: (String) -> Void          // the document can't be shown safely, and why
    let choose: (String, String) -> Void  // an option was picked in thread (first): the reply's first line (second)
    let problem: (String) -> Void         // a widget was not drawn, and why
    let take: (NSPasteboard) -> Bool      // files dropped on it: whether they were taken

    func makeCoordinator() -> MailDocumentLoader { MailWeb.loader }

    func makeNSView(context: Context) -> MailWebView { MailWeb.view }

    func updateNSView(_ web: MailWebView, context: Context) {
        let loader = context.coordinator
        loader.follow = follow
        loader.refuse = refuse
        loader.choose = choose
        loader.problem = problem
        web.take = take
        if web.pageZoom != zoom { web.pageZoom = zoom }
        loader.show(html, thread: thread, widgets: widgets, current: current, in: web)
        loader.turn(page, in: web)
    }
}

/// The one web view thread documents show in, with its loader, made on first use and kept while the app runs. A new
/// WKWebView starts a WebKit process, a third of a second or more before its first document shows, so the view is
/// not made anew each time the tab comes back, and `warm` starts it when the tab appears, before a thread is opened.
@MainActor
enum MailWeb {
    static let loader = MailDocumentLoader()
    static let view: MailWebView = {
        let config = WKWebViewConfiguration()
        config.defaultWebpagePreferences.allowsContentJavaScript = false
        config.websiteDataStore = .nonPersistent()
        if case .success(let source) = MailWidgets.script {      // when it is missing, drawing says so (MailDocumentLoader)
            config.userContentController.addUserScript(WKUserScript(source: source, injectionTime: .atDocumentEnd,
                                                                    forMainFrameOnly: true, in: MailWidgets.world))
        }
        config.userContentController.add(loader, contentWorld: MailWidgets.world, name: MailWidgets.handler)
        let web = MailWebView(frame: .zero, configuration: config)
        web.navigationDelegate = loader
        web.allowsBackForwardNavigationGestures = false
        web.setValue(false, forKey: "drawsBackground")      // the board's own background shows behind the document
        return web
    }()
    static let blank = "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body></body></html>"

    /// Start the web view's process with an empty page, unless it has had a document already.
    static func warm() {
        if !loader.started { loader.show(blank, thread: "", widgets: [], current: nil, in: view) }
    }
}

/// The document's web view, which hands files dropped on it (Attachments) to `take` instead of opening them.
final class MailWebView: WKWebView {
    var take: ((NSPasteboard) -> Bool)?

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
/// read, paging, and the widgets, whose choices come back to `choose` (WKScriptMessageHandler).
@MainActor
final class MailDocumentLoader: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
    var follow: (MailLink) -> Void = { _ in }
    var refuse: (String) -> Void = { _ in }
    var choose: (String, String) -> Void = { _, _ in }
    var problem: (String) -> Void = { _ in }
    private var html: String?            // the document asked for last
    private var thread = ""              // whose it is
    private var widgets: [MailWidget] = []   // its widgets
    private(set) var ready = false       // it has loaded
    private var current: String?         // the message to outline, a key (MailLink.isKey, so safe in a script)
    private var marked: String?          // the message outlined in the document as it is
    private var paged: Int?              // the last MailPage done
    private var loading = false          // the next navigation is our loadHTMLString
    private var blocking = false         // the content rules are on this web view

    /// Whether a document has been asked for.
    var started: Bool { html != nil }

    func show(_ html: String, thread: String, widgets: [MailWidget], current: String?, in web: WKWebView) {
        self.current = current.flatMap { MailLink.isKey($0) ? $0 : nil }
        guard html != self.html else {
            if ready { mark(in: web) }
            return
        }
        self.html = html
        self.thread = thread
        self.widgets = widgets
        ready = false
        marked = nil
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
                web.loadHTMLString(html, baseURL: nil)
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

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        ready = true
        mark(in: webView)
        draw(in: webView)
    }

    /// Draw the document's widgets with widgets.js, in its content world. A widget it cannot draw keeps the plain
    /// text the mailroom put in its place, and `problem` says why.
    private func draw(in web: WKWebView) {
        guard !widgets.isEmpty else { return }
        let json: String
        do {
            json = try MailWidgets.json(widgets)
        } catch {
            problem("The options and plots in this thread are shown as plain text: they could not be encoded (\(error.localizedDescription))")
            return
        }
        switch MailWidgets.script {
        case .failure(let error):
            problem("The options and plots in this thread are shown as plain text: \(error.localizedDescription)")
        case .success:
            web.callAsyncJavaScript(MailWidgets.draw, arguments: ["widgets": json], in: nil, in: MailWidgets.world) { [weak self] result in
                if case .failure(let error) = result {
                    self?.problem("The options and plots in this thread are shown as plain text: \(error.localizedDescription)")
                }
            }
        }
    }

    /// A choice or a problem from widgets.js. Only the main frame's, in the widgets' world, and only a choice of a
    /// reasonable length counts; it is for the thread whose document is loaded.
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, message.world == MailWidgets.world, ready,
              let body = message.body as? [String: Any] else { return }
        if let text = body["choice"] as? String, text.count <= MailWidgets.maxChoice, !thread.isEmpty {
            choose(thread, text)
        } else if let why = body["problem"] as? String {
            problem("Shown as plain text: " + String(why.prefix(300)))
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
