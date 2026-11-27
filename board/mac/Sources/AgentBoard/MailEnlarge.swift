import AppKit
import WebKit

// A plot, table or image of a thread document, enlarged in a window of its own nearly the size of the screen: a click
// on one in the document (widgets.js's enlarge, through the widgets' message handler, MailDocumentLoader) opens it. The
// window's web view runs no page script and loads nothing from outside (its document's CSP and MailBlocker's rules), as
// the thread's does, and its document is the app's own: a plot drawn again by widgets.js, in its content world, at the
// window's size (drawEnlarged), from the thread's own widget of that id, never from a spec the script sent; a table,
// the markup the script copied from the thread's document, which the mailroom sanitized; an image, its data: URL. It
// loads only its own page (MailNavigation), so links in it go nowhere. There is one such window: a new click shows its
// item in a new one in its place. Escape or ⌘W closes it.

@MainActor
enum MailEnlarge {
    /// How much of the screen's visible frame the window takes, each way.
    static let share: CGFloat = 0.92
    /// The most markup of a table it takes, in UTF-16 code units, as the script copied it.
    static let maxTable = 4 << 20
    /// The page around what it shows: the thread document's content rules (agentmail/mailview.py's CSP), a white
    /// ground, a table a quarter larger than in the mail, and an image as large as fits.
    static let page = """
    <!DOCTYPE html><html><head><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="\(MailBlocker.policy)">
    <style>
    body { margin: 0; padding: 24px; background: #ffffff; color: #1f2328;
      font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    body > table { border-collapse: collapse; zoom: 1.25; }
    body > img { display: block; width: 100%; height: calc(100vh - 48px); object-fit: contain; }
    </style></head><body>BODY</body></html>
    """

    /// The window open now, if any.
    private(set) static var window: EnlargedWindow?
    /// Where a new window goes and how it shows: `share` of the screen's visible frame, in its middle, in front. A
    /// self-test puts it offscreen, behind, so that it takes neither the screen nor the keyboard.
    static var place: (NSWindow) -> Void = { win in
        let screen = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1280, height: 800)
        let size = NSSize(width: screen.width * share, height: screen.height * share)
        win.setFrame(NSRect(x: screen.midX - size.width / 2, y: screen.midY - size.height / 2, width: size.width,
                            height: size.height), display: true)
        win.makeKeyAndOrderFront(nil)
    }
    /// Its web view, where it draws (for the self-tests).
    static var web: WKWebView? { window?.contentView as? WKWebView }

    /// What `item` shows: its window's title, the page's body, and the plot drawEnlarged draws into it (nil for a
    /// table or an image). Throws Refused for an item that is not a plot of `widgets`, a table's markup, or an image's
    /// data: URL.
    static func content(_ item: [String: Any], widgets: [MailWidget]) throws -> (title: String, body: String, plot: MailWidget?) {
        switch item["kind"] as? String {
        case "plot":
            guard let id = item["widget"] as? String, let w = widgets.first(where: { $0.id == id && $0.kind == "plot" }) else {
                throw Refused(why: "the plot is not one of this thread's")
            }
            guard case .object(let spec) = w.spec, case .string(let title)? = spec["title"] else {
                throw Refused(why: "the plot has no title")
            }
            return (title, "", w)
        case "table":
            guard let html = item["html"] as? String, html.utf16.count <= maxTable,
                  html.lowercased().hasPrefix("<table"), html.lowercased().hasSuffix("</table>") else {
                throw Refused(why: "the table's markup was not understood")
            }
            return ("Table", html, nil)
        case "image":
            guard let src = item["src"] as? String, src.hasPrefix("data:image/") else {
                throw Refused(why: "the image is not the mail's own")
            }
            let alt = item["alt"] as? String ?? ""
            return (alt.isEmpty ? "Image" : alt, "<img src=\"\(escaped(src))\" alt=\"\(escaped(alt))\">", nil)
        default:
            throw Refused(why: "it is not a plot, a table or an image")
        }
    }

    struct Refused: Error {
        let why: String
    }

    /// Open `item` (content) in the window, nearly the size of the screen, in place of what it showed. Returns why
    /// not, or nil; a plot that widgets.js cannot draw there is told to `problem`.
    static func open(_ item: [String: Any], widgets: [MailWidget], problem: @escaping (String) -> Void) -> String? {
        let shown: (title: String, body: String, plot: MailWidget?)
        do { shown = try content(item, widgets: widgets) } catch {
            return (error as? Refused)?.why ?? error.localizedDescription
        }
        guard case .success(let script) = MailWidgets.script else { return "widgets.js could not be read" }
        let config = WKWebViewConfiguration()
        config.defaultWebpagePreferences.allowsContentJavaScript = false
        config.websiteDataStore = .nonPersistent()
        config.userContentController.addUserScript(WKUserScript(source: script, injectionTime: .atDocumentEnd,
                                                                forMainFrameOnly: true, in: MailWidgets.world))
        let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 800, height: 600), configuration: config)
        let loader = Loader(plot: shown.plot, problem: problem)
        web.navigationDelegate = loader
        let win = EnlargedWindow(contentRect: web.frame, styleMask: [.titled, .closable, .resizable], backing: .buffered,
                                 defer: false)
        win.isReleasedWhenClosed = false
        win.title = shown.title
        win.contentView = web
        win.loader = loader
        window?.close()
        window = win
        place(win)                                  // sized before its page loads: a plot is drawn to fit it
        MailBlocker.whenReady { [weak win, weak web] result in
            guard let win = win, let web = web, window === win else { return }
            switch result {
            case .failure(let error):
                win.close()
                problem("Not enlarged: WebKit did not compile the rules that block its loads (\(error.localizedDescription))")
            case .success(let rules):
                web.configuration.userContentController.add(rules)
                loader.expecting = true
                web.loadHTMLString(page.replacingOccurrences(of: "BODY", with: shown.body), baseURL: nil)
            }
        }
        return nil
    }

    /// Forget `win` once it has closed, unless another has taken its place.
    static func closed(_ win: EnlargedWindow) {
        if window === win { window = nil }
    }

    /// `s` with the characters that end an attribute or start markup written as entities.
    static func escaped(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "\"", with: "&quot;")
            .replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;")
    }

    /// The window's navigation: its own page loads, nothing else does (MailNavigation: a link followed in the thread's
    /// view does nothing here); once loaded, a plot is drawn into it.
    final class Loader: NSObject, WKNavigationDelegate {
        let plot: MailWidget?
        let problem: (String) -> Void
        var expecting = false                       // its own page is on its way

        init(plot: MailWidget?, problem: @escaping (String) -> Void) {
            self.plot = plot
            self.problem = problem
        }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
            let go = MailNavigation.decide(expecting: expecting, type: action.navigationType,
                                           mainFrame: action.targetFrame?.isMainFrame ?? false, url: action.request.url)
            if go == .load { expecting = false }
            decisionHandler(go == .load ? .allow : .cancel)
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            guard let plot = plot else { return }
            guard let json = try? MailWidgets.json([plot]) else { return problem("Not enlarged: the plot could not be encoded") }
            webView.callAsyncJavaScript("return AgentBoardWidgets.drawEnlarged(document, JSON.parse(widgets)[0]);",
                                        arguments: ["widgets": json], in: nil, in: MailWidgets.world) { [problem] result in
                switch result {
                case .success(let why as String): problem("Not enlarged: " + why)
                case .failure(let error): problem("Not enlarged: " + error.localizedDescription)
                default: break
                }
            }
        }
    }
}

/// The enlarged item's window: Escape closes it, as ⌘W does, and once closed MailEnlarge forgets it. It keeps its web
/// view's navigation delegate, which the web view holds weakly.
final class EnlargedWindow: NSWindow {
    var loader: MailEnlarge.Loader?

    override func cancelOperation(_ sender: Any?) {
        close()
    }

    override func close() {
        super.close()
        MailEnlarge.closed(self)
    }
}
