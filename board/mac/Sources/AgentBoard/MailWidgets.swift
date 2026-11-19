import AppKit
import WebKit

// The widgets agents put in their mail: clickable options (`choices`) and interactive plots (`plot`). The mailroom
// checks each one's spec when the mail is sent, keeps it, and puts a placeholder holding a plain-text version where
// it goes in the thread document (its README, "Widgets in agent mail"); mail_show gives each message's widgets. The
// tab draws them with widgets.js, the app's own code, injected into the document in a content world of its own: the
// page still runs no script of its own and loads nothing. A choice comes back through a message handler as the
// reply's first line (Draft.choose); nothing is sent until the owner sends the reply.

/// One widget of an agent's mail, as mail_show gives it: its id ("s<sent id>-<n>", also its placeholder's
/// data-widget in the document), its kind and its spec.
struct MailWidget: Codable, Equatable {
    var id: String
    var kind: String
    var spec: JSONValue
}

@MainActor
enum MailWidgets {
    /// The content world widgets.js runs in, apart from the page (which runs nothing) and from the app's other scripts.
    static let world = WKContentWorld.world(name: "AgentBoardMailWidgets")
    /// The message handler a choice or a problem comes back through.
    static let handler = "mailWidget"

    /// widgets.js and the Mac's side of it (where a choice and a problem go), or why it could not be read. It is a
    /// resource of the app: in the bundle SwiftPM builds beside the binary, which build-app.sh copies into the app's
    /// Resources, so Bundle.main's resources hold it either way.
    static let script: Result<String, Error> = {
        let bundleURL = Bundle.main.resourceURL?.appendingPathComponent("AgentBoard_AgentBoard.bundle")
        do {
            guard let b = bundleURL.flatMap(Bundle.init(url:)), let url = b.url(forResource: "widgets", withExtension: "js") else {
                throw NSError(domain: "AgentBoard", code: 1, userInfo: [NSLocalizedDescriptionKey:
                    "widgets.js is not in \(bundleURL?.path ?? "the app's resources")"])
            }
            return .success(try String(contentsOf: url, encoding: .utf8) + "\n" + host)
        } catch {
            return .failure(error)
        }
    }()

    /// What a choice and a problem do in the Mac's document: post to the handler.
    static let host = """
    var agentBoardMailHost = {
      choose: function (text) { window.webkit.messageHandlers.\(handler).postMessage({choice: text}); },
      problem: function (why) { window.webkit.messageHandlers.\(handler).postMessage({problem: why}); }
    };
    """

    /// The script that draws `widgets` (JSON text) into the loaded document: how many it drew.
    static let draw = "return AgentBoardWidgets.render(document, JSON.parse(widgets), agentBoardMailHost);"

    /// A thread's widgets as the JSON text `draw` takes. Throws when they cannot be encoded.
    static func json(_ widgets: [MailWidget]) throws -> String {
        String(decoding: try JSONEncoder().encode(widgets), as: UTF8.self)
    }

    /// mail_show's messages' widgets, from its answer `obj` (key -> widgets), decoded with a plain JSONDecoder so that
    /// each spec reaches widgets.js with its keys as the agent wrote them; with "KEY: why" for each message whose
    /// widgets do not decode.
    nonisolated static func decode(_ obj: [String: Any]?) -> (widgets: [String: [MailWidget]], problems: [String]) {
        var widgets: [String: [MailWidget]] = [:]
        var problems: [String] = []
        for m in obj?["messages"] as? [[String: Any]] ?? [] {
            guard let key = m["key"] as? String, let raw = m["widgets"], !(raw is NSNull) else { continue }
            do {
                widgets[key] = try JSONDecoder().decode([MailWidget].self, from: JSONSerialization.data(withJSONObject: raw))
            } catch {
                problems.append("\(key): \(describeDecodeError(error))")
            }
        }
        return (widgets, problems)
    }

    /// The longest choice line taken from a document: past it, the post is not a choice the renderer made.
    static let maxChoice = 4000
}

/// --selftest-widgets: widgets.js in a real document view (MailWeb's, with its content rules and content world),
/// offscreen. A document with placeholders and widgets gets its option cards and its line, bar and scatter plots; a
/// click posts the choice line, for one option and for several; the legend, tooltips and zoom work; a spec that does
/// not check out keeps its plain-text version and says why; spec text is shown as text.
@MainActor
enum WidgetTest {
    static func run() -> Int32 {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append(what) } }
        guard case .success = MailWidgets.script else {
            if case .failure(let e) = MailWidgets.script { print("FAIL: widgets.js not loaded: \(e.localizedDescription)") }
            return 1
        }
        let web = MailWeb.view
        web.frame = NSRect(x: 0, y: 0, width: 760, height: 900)
        let win = NSWindow(contentRect: web.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        win.contentView = web
        win.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        win.orderBack(nil)
        return withExtendedLifetime(win) {
            var choices: [String] = []
            var problems: [String] = []
            let loader = MailWeb.loader
            loader.choose = { thread, text in choices.append(thread + ": " + text) }
            loader.problem = { problems.append($0) }
            loader.refuse = { problems.append("refused: " + $0) }
            loader.show(document, thread: "T1", widgets: widgets, current: nil, in: web)
            let js = { (source: String) in Renderer.evaluate(source, in: web, within: 5) }
            /// The tooltip's text after the mouse moves to viewBox x `x` (of 640) in widget `id`'s chart, halfway down.
            let hover = { (id: String, x: Double) -> String? in
                js("""
                    (function () { var s = document.querySelector('[data-widget="\(id)"] svg.abw-chart'), r = s.getBoundingClientRect();
                      s.dispatchEvent(new MouseEvent('mousemove', {clientX: r.left + r.width * \(x) / 640, clientY: r.top + r.height * 0.5, bubbles: true}));
                      return document.querySelector('[data-widget="\(id)"] .abw-tip').textContent; })()
                    """) as? String
            }
            guard Renderer.widgetsDrawn(web, want: 9, within: 15) == 9 else {
                print("FAIL: the widgets were not drawn in 15 s; problems: \(problems)")
                return 1
            }
            check(js("document.querySelectorAll('[data-widget=\"s5-0\"] .abw-opt').length") as? Int == 3, "three option cards")
            check(js("document.querySelector('[data-widget=\"s5-0\"] .abw-badge').parentNode.textContent") as? String
                  == "2. Ship it nowRecommended", "the recommended badge on option 2")
            check(js("document.querySelector('[data-widget=\"s5-0\"] .abw-q').textContent") as? String == "Which <b>one</b>?",
                  "spec text is text, not markup")
            check(js("document.querySelector('[data-widget=\"s5-0\"] b') === null") as? Bool == true, "no element made from spec text")
            check(js("document.querySelector('[data-widget=\"s5-0\"] .abw-default').textContent") as? String
                  == "If you don't answer: Option 2 tonight", "the default line")
            check(js("document.querySelectorAll('[data-widget=\"s5-1\"] svg.abw-chart path.abw-line').length") as? Int == 2, "two lines")
            check(js("document.querySelectorAll('[data-widget=\"s5-1\"] .abw-legend button').length") as? Int == 2, "a legend")
            check(js("document.querySelector('[data-widget=\"s5-1\"]').textContent.indexOf('Fallback plot') < 0") as? Bool == true,
                  "the plot replaces its plain-text version")

            // a click: the choice line, for the open thread
            _ = js("document.querySelectorAll('[data-widget=\"s5-0\"] .abw-opt')[1].click()")
            _ = Renderer.wait(3, step: 0.01) { !choices.isEmpty }
            check(choices == ["T1: Option 2: Ship it now"], "one option: \(choices)")
            check(js("document.querySelectorAll('[data-widget=\"s5-0\"] .abw-on').length") as? Int == 1, "it is marked picked")
            _ = js("document.querySelectorAll('[data-widget=\"s5-0\"] .abw-opt')[2].click()")
            _ = Renderer.wait(3, step: 0.01) { choices.count == 2 }
            check(choices.last == "T1: Option 3: Drop it", "another click replaces it: \(choices)")

            // several may be picked: "Options 1 and 3: ...", and none again is ""
            _ = js("var c = document.querySelectorAll('[data-widget=\"s5-2\"] .abw-opt'); c[0].click(); c[2].click();")
            _ = Renderer.wait(3, step: 0.01) { choices.count == 4 }
            check(Array(choices.suffix(2)) == ["T1: Option 1: Plots", "T1: Options 1 and 3: Plots; Tables"],
                  "several: \(choices.suffix(2))")
            _ = js("var c = document.querySelectorAll('[data-widget=\"s5-2\"] .abw-opt'); c[0].click(); c[2].click();")
            _ = Renderer.wait(3, step: 0.01) { choices.count == 6 }
            check(choices.last == "T1: ", "none picked: an empty line: \(choices.last ?? "none")")

            // text with a control character falls back and says why; a line separator in a label is drawn, and the
            // choice line stays one line
            for (id, fallback) in [("s9-1", "Fallback: a line break"), ("s9-3", "Fallback: a tab")] {
                check(js("document.querySelector('[data-widget=\"\(id)\"]').textContent") as? String == fallback,
                      "\(id): text with a control character keeps the plain text")
                check(problems.contains { $0.contains(id) && $0.contains("control character") }, "\(id): and says why: \(problems)")
            }
            _ = js("document.querySelectorAll('[data-widget=\"s9-2\"] .abw-opt')[0].click()")
            _ = Renderer.wait(3, step: 0.01) { choices.count == 7 }
            check(choices.last == "T1: Option 1: Two lines", "the choice line is one line: \(choices.last.debugDescription)")
            // an axis narrower than the numbers' spacing there (1e17 to 1e17 + 16) is drawn, not looped on
            check(js("document.querySelectorAll('[data-widget=\"s9-0\"] svg.abw-chart').length") as? Int == 1,
                  "a plot on a range of a few ulps is drawn")

            // the legend hides a line; hovering shows the values at the nearest x
            _ = js("document.querySelectorAll('[data-widget=\"s5-1\"] .abw-legend button')[1].click()")
            check(js("document.querySelectorAll('[data-widget=\"s5-1\"] svg.abw-chart path.abw-line').length") as? Int == 1, "a name hides its line")
            let hovered = hover("s5-1", 320)
            check(hovered == "step 1000Muon: 0.52", "the tooltip: \(hovered ?? "none")")
            // dragging zooms on x; a double-click resets it
            let before = js("document.querySelector('[data-widget=\"s5-1\"] svg.abw-chart g').textContent") as? String
            _ = js("""
                (function () { var s = document.querySelector('[data-widget="s5-1"] svg.abw-chart'), r = s.getBoundingClientRect();
                  function at(type, f) { s.dispatchEvent(new MouseEvent(type, {clientX: r.left + r.width * f, clientY: r.top + r.height * 0.5, bubbles: true})); }
                  at('mousedown', 0.3); at('mousemove', 0.6); at('mouseup', 0.6); })()
                """)
            let zoomed = js("document.querySelector('[data-widget=\"s5-1\"] svg.abw-chart g').textContent") as? String
            check(zoomed != nil && zoomed != before, "a drag zooms in (the x ticks change)")
            _ = js("document.querySelector('[data-widget=\"s5-1\"] svg.abw-chart').dispatchEvent(new MouseEvent('dblclick', {bubbles: true}))")
            check(js("document.querySelector('[data-widget=\"s5-1\"] svg.abw-chart g').textContent") as? String == before,
                  "a double-click resets the zoom")

            // a bar plot: a bar per category and series; the tooltip has each series' value; a name hides its bars
            check(js("document.querySelectorAll('[data-widget=\"s6-1\"] svg.abw-chart rect.abw-mark-0').length") as? Int == 3
                  && js("document.querySelectorAll('[data-widget=\"s6-1\"] svg.abw-chart rect.abw-mark-1').length") as? Int == 3,
                  "a bar per category and series")
            let bars = hover("s6-1", 58 + 564.0 / 6)                     // the first category's middle
            check(bars == "size smallDone: 5Failed: 1", "the bar tooltip: \(bars ?? "none")")
            _ = js("document.querySelectorAll('[data-widget=\"s6-1\"] .abw-legend button')[1].click()")
            check(js("document.querySelectorAll('[data-widget=\"s6-1\"] svg.abw-chart rect.abw-mark-1').length") as? Int == 0,
                  "a name hides its bars")
            check(hover("s6-1", 58 + 564.0 / 6) == "size smallDone: 5", "and its value leaves the tooltip")

            // a scatter plot: a marker per point, its own shape for each series; the tooltip at the nearest x
            check(js("document.querySelectorAll('[data-widget=\"s6-2\"] svg.abw-chart circle.abw-mark-0').length") as? Int == 3
                  && js("document.querySelectorAll('[data-widget=\"s6-2\"] svg.abw-chart rect.abw-mark-1').length") as? Int == 2,
                  "circles for one series, squares for the other")
            let dots = hover("s6-2", 58 + 564.0 / 2)                      // x = 2, the middle of 1 to 3
            check(dots == "params 2A: 2B: –", "the scatter tooltip, – where a series has no point there: \(dots ?? "none")")
            _ = js("document.querySelectorAll('[data-widget=\"s6-2\"] .abw-legend button')[0].click()")
            check(js("document.querySelectorAll('[data-widget=\"s6-2\"] svg.abw-chart .abw-mark-0').length") as? Int == 0,
                  "a name hides its markers")

            // plots whose series are all empty, as the mailroom allows: the axes and a note, no marks, an empty tooltip
            for id in ["s8-0", "s8-1"] {
                check(js("document.querySelector('[data-widget=\"\(id)\"] svg.abw-chart .abw-empty').textContent") as? String
                      == "Nothing to show", "\(id): an empty plot says so")
                check(js("document.querySelectorAll('[data-widget=\"\(id)\"] svg.abw-chart [class^=\"abw-mark\"], "
                         + "[data-widget=\"\(id)\"] svg.abw-chart path.abw-line').length") as? Int == 0, "\(id): no marks")
                check(hover(id, 320) == "", "\(id): no tooltip over an empty plot")
            }

            // a widget that does not check out keeps its plain-text version and says why; one with no place says so
            check(js("document.querySelector('[data-widget=\"s6-0\"]').textContent") as? String == "Fallback: pick 1 or 2",
                  "a bad spec keeps the plain text")
            check(problems.contains { $0.contains("s6-0") && $0.contains("options must be a list of 2 to 8") },
                  "and says why: \(problems)")
            check(problems.contains { $0.contains("s7-0") && $0.contains("no place") }, "a widget with no place: \(problems)")
            check(js("document.querySelector('[data-widget=\"s6-3\"]').textContent") as? String == "Fallback: a pie",
                  "a plot that does not check out keeps the plain text")
            check(problems.contains { $0.contains("s6-3") && $0.contains("kind must be line, scatter or bar") },
                  "and says why: \(problems)")
            // widgets that are not a list: said, nothing drawn
            final class Ran { var done = false }
            let ran = Ran()
            web.callAsyncJavaScript("return AgentBoardWidgets.render(document, {}, agentBoardMailHost);", arguments: [:], in: nil,
                                    in: MailWidgets.world) { _ in ran.done = true }
            _ = Renderer.wait(5, step: 0.01) { ran.done && problems.contains { $0.contains("not a list") } }
            check(problems.contains { $0.contains("the widgets are not a list") }, "widgets not in a list: said: \(problems)")
            check(!problems.contains { $0.hasPrefix("refused") }, "the document was shown: \(problems)")
            if fails.isEmpty { print("widgets selftest: ok"); return 0 }
            fails.forEach { print("FAIL: " + $0) }
            return 1
        }
    }

    static let document = """
    <!DOCTYPE html><html><head><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'">
    </head><body>
    <details class="msg" id="s5" open><summary>agent</summary><div class="card">
    <p>Three ways to go:</p>
    <div class="widget" data-widget="s5-0"><p><b>Which?</b></p><ol><li>Keep</li><li>Lift</li><li>Week</li></ol></div>
    <div class="widget" data-widget="s5-1"><p>Fallback plot</p></div>
    <div class="widget" data-widget="s5-2"><p>Fallback several</p></div>
    </div></details>
    <details class="msg" id="s6" open><summary>agent</summary><div class="card">
    <div class="widget" data-widget="s6-0">Fallback: pick 1 or 2</div>
    <div class="widget" data-widget="s6-1"><p>Fallback bars</p></div>
    <div class="widget" data-widget="s6-2"><p>Fallback scatter</p></div>
    <div class="widget" data-widget="s6-3">Fallback: a pie</div>
    <div class="widget" data-widget="s8-0">Fallback: no bars</div>
    <div class="widget" data-widget="s8-1">Fallback: no lines</div>
    </div></details>
    <details class="msg" id="s9" open><summary>agent</summary><div class="card">
    <div class="widget" data-widget="s9-0">Fallback: nanoseconds</div>
    <div class="widget" data-widget="s9-1">Fallback: a line break</div>
    <div class="widget" data-widget="s9-2">Fallback: a separator</div>
    <div class="widget" data-widget="s9-3">Fallback: a tab</div>
    </div></details>
    </body></html>
    """

    static let widgets: [MailWidget] = {
        let raw = """
        [{"id": "s5-0", "kind": "choices", "spec": {"question": "Which <b>one</b>?",
          "options": [{"label": "Wait for review", "detail": "Merge once the review passes."},
                      {"label": "Ship it now", "detail": "The release goes out tonight.", "recommended": true},
                      {"label": "Drop it"}],
          "default": "Option 2 tonight"}},
         {"id": "s5-1", "kind": "plot", "spec": {"title": "Accuracy by step", "kind": "line",
          "x": {"label": "step"}, "y": {"label": "accuracy", "min": 0, "max": 1},
          "series": [{"name": "Muon", "x": [0, 1000, 2000], "y": [0.1, 0.52, 0.7]},
                     {"name": "Lion", "x": [0, 1000, 2000], "y": [0.1, 0.3, null]}]}},
         {"id": "s5-2", "kind": "choices", "spec": {"question": "Which to build?", "multiple": true,
          "options": [{"label": "Plots"}, {"label": "Sliders"}, {"label": "Tables"}]}},
         {"id": "s6-0", "kind": "choices", "spec": {"question": "Only one?", "options": [{"label": "One"}]}},
         {"id": "s6-1", "kind": "plot", "spec": {"title": "Runs by size", "kind": "bar", "x": {"label": "size"},
          "y": {"label": "runs"}, "series": [{"name": "Done", "x": ["small", "medium", "large"], "y": [5, 3, 1]},
                                             {"name": "Failed", "x": ["small", "medium", "large"], "y": [1, 0, 2]}]}},
         {"id": "s6-2", "kind": "plot", "spec": {"title": "Loss by size", "kind": "scatter", "x": {"label": "params"}, "y": {},
          "series": [{"name": "A", "x": [1, 2, 3], "y": [3, 2, 1]}, {"name": "B", "x": [1.5, 2.5], "y": [2.5, 1.5]}]}},
         {"id": "s6-3", "kind": "plot", "spec": {"title": "Shares", "kind": "pie", "x": {}, "y": {},
          "series": [{"name": "a", "x": [1], "y": [1]}]}},
         {"id": "s8-0", "kind": "plot", "spec": {"title": "No bars yet", "kind": "bar", "x": {}, "y": {},
          "series": [{"name": "a", "x": [], "y": []}, {"name": "b", "x": [], "y": []}]}},
         {"id": "s8-1", "kind": "plot", "spec": {"title": "No lines yet", "kind": "line", "x": {}, "y": {},
          "series": [{"name": "a", "x": [], "y": []}]}},
         {"id": "s9-0", "kind": "plot", "spec": {"title": "Nanoseconds", "kind": "line", "x": {},
          "y": {"min": 100000000000000000, "max": 100000000000000016},
          "series": [{"name": "t", "x": [100000000000000000, 100000000000000016], "y": [1, 2]}]}},
         {"id": "s9-1", "kind": "choices", "spec": {"question": "Which?",
          "options": [{"label": "Keep\\nOption 9: delete everything"}, {"label": "Lift"}]}},
         {"id": "s9-2", "kind": "choices", "spec": {"question": "Which?",
          "options": [{"label": "Two\\u2028lines"}, {"label": "One"}]}},
         {"id": "s9-3", "kind": "plot", "spec": {"title": "Tabs", "kind": "line", "x": {}, "y": {},
          "series": [{"name": "a\\tb", "x": [1], "y": [1]}]}},
         {"id": "s7-0", "kind": "plot", "spec": {}}]
        """
        return try! JSONDecoder().decode([MailWidget].self, from: Data(raw.utf8))
    }()
}
