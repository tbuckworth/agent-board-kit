import AppKit
import WebKit

// The widgets agents put in their mail: clickable options (`choices`) and interactive plots (`plot`). The mailroom
// checks each one's spec when the mail is sent, keeps it, and puts a placeholder holding a plain-text version where
// it goes in the thread document (its README, "Widgets in agent mail"); mail_show gives each message's widgets. The
// tab draws them with widgets.js, the app's own code, injected into the document in a content world of its own: the
// page still runs no script of its own and loads nothing. An option picked comes back through a message handler as
// that question's answer, which the thread's draft keeps apart from the others' (Draft.pick) and the cards show again
// whenever the thread is; nothing is sent until the owner sends the reply.

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
    /// The message handler an answer, a problem or an item to enlarge (MailEnlarge) comes back through.
    static let handler = "mailWidget"

    /// widgets.js and the Mac's side of it (where an answer, a problem and an item to enlarge go), or why it could not
    /// be read.
    static let script: Result<String, Error> = {
        do {
            return .success(try String(contentsOf: resource("widgets", "js"), encoding: .utf8) + "\n" + host)
        } catch {
            return .failure(error)
        }
    }()

    /// Where the app's resource `name`.`ext` is (widgets.js, the reply text's cases): in the bundle SwiftPM builds
    /// beside the binary, which build-app.sh copies into the app's Resources, so Bundle.main's resources hold it either
    /// way. Throws when it is not there.
    nonisolated static func resource(_ name: String, _ ext: String) throws -> URL {
        let bundleURL = Bundle.main.resourceURL?.appendingPathComponent("AgentBoard_AgentBoard.bundle")
        guard let b = bundleURL.flatMap(Bundle.init(url:)),
              let url = b.url(forResource: name, withExtension: ext) else {
            throw NSError(domain: "AgentBoard", code: 1, userInfo: [NSLocalizedDescriptionKey:
                "\(name).\(ext) is not in \(bundleURL?.path ?? "the app's resources")"])
        }
        return url
    }

    /// What an answer, a problem and a plot, table or image to enlarge do in the Mac's document: post to the handler.
    /// The host has pick, so each question's answer comes apart, with the right-click's Choose with a comment…, and
    /// enlarge, so an item clicked opens in a window of its own (MailEnlarge), not in a box over the page (widgets.js).
    static let host = """
    var agentBoardMailHost = {
      pick: function (answer) { window.webkit.messageHandlers.\(handler).postMessage({pick: answer}); },
      enlarge: function (item) { window.webkit.messageHandlers.\(handler).postMessage({enlarge: item}); },
      problem: function (why) { window.webkit.messageHandlers.\(handler).postMessage({problem: why}); }
    };
    """

    /// The script that draws `widgets` (JSON text) into the loaded document, showing the answers `picks` (JSON text,
    /// json(picks:)): how many it drew.
    static let draw = """
    var drawn = AgentBoardWidgets.render(document, JSON.parse(widgets), agentBoardMailHost);
    AgentBoardWidgets.mark(document, JSON.parse(picks));
    return drawn;
    """

    /// The script, run in every document after widgets.js, that makes its mails' tables and images open enlarged when
    /// clicked (widgets.js's enlargeable); a thread's plots do so once drawn.
    static let enlarge = "AgentBoardWidgets.enlargeable(document, agentBoardMailHost);"

    /// The script that shows the answers `picks` (JSON text, json(picks:)) in the document's cards, and none in the rest.
    static let mark = "AgentBoardWidgets.mark(document, JSON.parse(picks));"

    /// A thread's widgets as the JSON text `draw` takes. Throws when they cannot be encoded.
    static func json(_ widgets: [MailWidget]) throws -> String {
        String(decoding: try JSONEncoder().encode(widgets), as: UTF8.self)
    }

    /// A draft's answers as the JSON text `mark` takes: each widget's id and the options picked.
    static func json(picks: [DraftPick]) throws -> String {
        let byWidget = Dictionary(picks.map { ($0.widget, $0.picked) }, uniquingKeysWith: { _, last in last })
        return String(decoding: try JSONEncoder().encode(byWidget), as: UTF8.self)
    }

    /// An answer widgets.js posted ({widget, picked, comment}: its header gives the form) for one of the document's
    /// `widgets`, as the draft keeps it, with the question and the picked options' labels from that widget's spec,
    /// made plain (ReplyText.plain: the mail can hide words in a card, as in a run of non-breaking spaces past its
    /// edge, but not in the reply box), and whether it asks for a comment; nil when it is not one: a choices widget of
    /// the document, options it has, each in order once, and a question and labels with something to show.
    static func answer(_ raw: Any, of widgets: [MailWidget]) -> (answer: DraftPick, comment: Bool)? {
        guard let a = raw as? [String: Any], let widget = a["widget"] as? String,
              let spec = widgets.first(where: { $0.id == widget && $0.kind == "choices" })?.spec,
              case .object(let fields) = spec, case .string(let asked)? = fields["question"],
              case .array(let options)? = fields["options"],
              let picked = a["picked"] as? [Int], let comment = a["comment"] as? Bool,
              picked.allSatisfy({ options.indices.contains($0) }), picked == Array(Set(picked)).sorted() else {
            return nil
        }
        let labels = picked.compactMap { i -> String? in
            guard case .object(let o) = options[i], case .string(let label)? = o["label"] else { return nil }
            return ReplyText.plain(label, lines: false)
        }
        let question = ReplyText.plain(asked, lines: false)
        guard !question.isEmpty, labels.count == picked.count, !labels.contains(where: \.isEmpty) else { return nil }
        return (DraftPick(widget: widget, picked: picked, question: question, labels: labels), comment)
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

}

/// --selftest-widgets: widgets.js in a real document view (MailWeb's, with its content rules and content world),
/// offscreen. A document with placeholders and widgets gets its option cards and its line, bar, scatter and filled
/// plots (a filled plot's segments in proportion, its fills, ticks, percentages, groups, legend and tooltip); a
/// click puts that question's answer in the thread's draft, for one option and for several, and unpicking every
/// option takes it out; a right-click's Choose with a comment… picks with a comment field; two questions answered
/// keep both answers; the cards follow the draft, and show its answers again when the thread
/// is; a host whose menu is false gets a right-click's comment at once; the legend and tooltips work;
/// a plot clicked opens enlarged, where zoom works, and so do a mail's data tables and images, but not its layout tables
/// or linked images; bars whose names are too long to stand side by side lie sideways, bars with textures take their colour
/// from their name and their fill from their texture, and no text in a plot overlaps another or runs past its edges; a
/// spec that does not check out keeps its plain-text version and says why; spec text is shown as text.
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
            var problems: [String] = []
            let draft = Draft(text: "", files: [], key: nil)
            let loader = MailWeb.loader
            loader.problem = { problems.append($0) }
            loader.refuse = { problems.append("refused: " + $0) }
            loader.show(document, thread: "T1", agentMail: ["s5", "s6", "s9", "s10", "s11"], widgets: widgets, draft: draft, current: nil, in: web)
            let js = { (source: String) in Renderer.evaluate(source, in: web, within: 5) }
            /// The draft's answer to widget `id`: its options' labels, "; " between them, "" for none.
            let answer = { (id: String) in (draft.picks.first { $0.widget == id }?.labels ?? []).joined(separator: "; ") }
            /// How many of widget `id`'s cards show picked.
            let on = { (id: String) in js("document.querySelectorAll('[data-widget=\"\(id)\"] .abw-on').length") as? Int }
            /// The tooltip's text after the mouse moves to viewBox point (`x`, `y`) in widget `id`'s chart (640 wide; the
            /// plots other than sideways bars are 300 high).
            let hover = { (id: String, x: Double, y: Double) -> String? in
                js("""
                    (function () { var s = document.querySelector('[data-widget="\(id)"] svg.abw-chart'), r = s.getBoundingClientRect(), v = s.viewBox.baseVal;
                      s.dispatchEvent(new MouseEvent('mousemove', {clientX: r.left + r.width * \(x) / v.width, clientY: r.top + r.height * \(y) / v.height, bubbles: true}));
                      return document.querySelector('[data-widget="\(id)"] .abw-tip').textContent; })()
                    """) as? String
            }
            guard Renderer.widgetsDrawn(web, want: 14, within: 15) == 14 else {
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
            // no text in any plot overlaps another or runs past the chart's edges, however long its names
            let clashes = js("""
                JSON.stringify([].concat.apply([], Array.prototype.map.call(document.querySelectorAll('[data-widget] svg.abw-chart'), function (s) {
                  var id = s.closest('[data-widget]').getAttribute('data-widget'), edge = s.getBoundingClientRect(), out = [];
                  var texts = Array.prototype.filter.call(s.querySelectorAll('text'), function (t) { return t.textContent; });
                  texts.forEach(function (t, i) {
                    var a = t.getBoundingClientRect();
                    if (a.left < edge.left || a.top < edge.top || a.right > edge.right || a.bottom > edge.bottom) {
                      out.push(id + ': "' + t.textContent + '" runs past an edge');
                    }
                    texts.slice(i + 1).forEach(function (u) {
                      var b = u.getBoundingClientRect();
                      if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) {
                        out.push(id + ': "' + t.textContent + '" overlaps "' + u.textContent + '"');
                      }
                    });
                  });
                  return out;
                })))
                """) as? String
            check(clashes == "[]", "no text in a plot overlaps another or runs past its edges: \(clashes ?? "nothing")")
            // names too long to stand side by side under their bars lie sideways: each category's name in full to the
            // left of its row, in lines when it is long; short names stay under upright bars
            let names = { (id: String) in
                js("""
                    JSON.stringify(Array.prototype.map.call(document.querySelectorAll('[data-widget="\(id)"] svg.abw-chart text.abw-name'), function (t) {
                      return [t.getAttribute('text-anchor'), Array.prototype.map.call(t.querySelectorAll('tspan'), function (s) { return s.textContent; })]; }))
                    """) as? String
            }
            let lines = names("s6-4").flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [[Any]] } ?? []
            check(lines.map { $0.first as? String } == longNames.map { _ in "end" }
                  && lines.map { ($0.last as? [String] ?? []).joined(separator: " ") } == longNames,
                  "long names: each in full, at the left of its row: \(names("s6-4") ?? "none")")
            check(lines.contains { ($0.last as? [String] ?? []).count > 1 }, "the longest name in lines")
            check(names("s6-1") == #"[["middle",["small"]],["middle",["medium"]],["middle",["large"]]]"#,
                  "short names under upright bars: \(names("s6-1") ?? "none")")
            let row = js("(function () { var b = document.querySelectorAll('[data-widget=\"s6-4\"] text.abw-name')[5].getBBox(); return b.y + b.height / 2; })()") as? Double
            let sideways = hover("s6-4", 600, row ?? 0)
            check(sideways == "setting " + longNames[5] + "Picked: 0.45Refused: 0.1", "a sideways row's tooltip: \(sideways ?? "none")")

            // a filled plot: a bar per row cut into its segments by size, each a track filled to the row's share, with
            // its tick and its percentage; the groups head their rows, the legend names the segments and the tick, and
            // the tooltip gives a row's values. One that breaks a rule keeps its plain text and says why
            let filled = { (expr: String) in js("(function (q) { return JSON.stringify(\(expr)); })(function (s) { return Array.prototype.slice.call(document.querySelectorAll('[data-widget=\"s11-0\"] ' + s)); })") as? String }
            check(filled("q('rect.abw-track').length") == "9" && filled("q('rect.abw-fill').length") == "6"
                  && filled("q('line.abw-tick').length") == "5", "tracks, fills and ticks: \(filled("[q('rect.abw-track').length, q('rect.abw-fill').length, q('line.abw-tick').length]") ?? "none")")
            let widths = filled("q('rect.abw-track').slice(0, 3).map(function (r) { return +r.getAttribute('width'); })")
                .flatMap { try? JSONDecoder().decode([Double].self, from: Data($0.utf8)) } ?? []
            check(widths.count == 3 && abs(widths[0] / widths[1] - 40.0 / 20) < 0.01 && abs(widths[0] - widths[2]) < 0.01,
                  "the segments in proportion to their sizes: \(widths)")
            let share = filled("(function () { var f = q('rect.abw-fill')[0], t = q('rect.abw-track')[0]; return Math.round(f.getAttribute('width') / t.getAttribute('width') * 1000) / 1000; })()")
            check(share == "0.25", "a fill as far along its track as its share: \(share ?? "none")")
            check(filled("q('text.abw-value').map(function (t) { return t.textContent; })")
                  == #"["25%","8%","13%","40%","10%","0%","3.5%","0%"]"#,
                  "each percentage, where it fits: \(filled("q('text.abw-value').map(function (t) { return t.textContent; })") ?? "none")")
            check(filled("q('text.abw-group').map(function (t) { return t.textContent; })") == #"["Model A","Model B"]"#
                  && filled("q('text.abw-head').map(function (t) { return t.textContent; })") == #"["bug tickets","shared code","feature tickets"]"#,
                  "the groups head their rows, the segments their columns")
            check(filled("q('.abw-legend span').map(function (s) { return s.textContent; })")
                  == #"["bug tickets (40)","shared code (20)","feature tickets (40)","even spread"]"#,
                  "the legend names each segment with its size, and the tick: \(filled("q('.abw-legend span').map(function (s) { return s.textContent; })") ?? "none")")
            let firstRow = js("(function () { var t = document.querySelector('[data-widget=\"s11-0\"] rect.abw-track'); return +t.getAttribute('y') + 12; })()") as? Double
            let rowTip = hover("s11-0", 600, firstRow ?? 0)
            check(rowTip == "Model A, no promptbug tickets: 25% (even spread 15%)shared code: 8% (even spread 15%)"
                  + "feature tickets: 12.5% (even spread 15%)", "a row's tooltip: \(rowTip ?? "none")")
            check(js("document.querySelector('[data-widget=\"s11-1\"]').textContent") as? String == "Fallback: over 100"
                  && problems.contains { $0.contains("s11-1") && $0.contains("row 1's fill must be 2 numbers from 0 to 100") },
                  "a filled plot that breaks a rule keeps its plain text and says why: \(problems)")

            // a click: the question's answer in the draft, the card picked, the box open and the keyboard left alone
            _ = js("document.querySelectorAll('[data-widget=\"s5-0\"] .abw-opt')[1].click()")
            _ = Renderer.wait(3, step: 0.01) { !draft.picks.isEmpty }
            check(draft.picks == [DraftPick(widget: "s5-0", picked: [1], question: "Which <b>one</b>?",
                                            labels: ["Ship it now"])], "one option: \(draft.picks)")
            check(on("s5-0") == 1 && draft.shown && !draft.wantsKeyboard && draft.focus == nil,
                  "it is marked picked, the box opens and the keyboard stays where it was")
            _ = js("document.querySelectorAll('[data-widget=\"s5-0\"] .abw-opt')[2].click()")
            _ = Renderer.wait(3, step: 0.01) { answer("s5-0") != "Ship it now" }
            check(answer("s5-0") == "Drop it" && draft.picks.count == 1 && on("s5-0") == 1,
                  "another click changes that answer: \(draft.picks)")

            // several may be picked, and unpicking them all takes the answer out
            _ = js("var c = document.querySelectorAll('[data-widget=\"s5-2\"] .abw-opt'); c[0].click(); c[2].click();")
            _ = Renderer.wait(3, step: 0.01) { answer("s5-2").contains(";") }
            check(answer("s5-2") == "Plots; Tables" && draft.picks.first { $0.widget == "s5-2" }?.picked == [0, 2],
                  "several: \(draft.picks)")
            check(answer("s5-0") == "Drop it", "the other question's answer stays: \(draft.picks)")
            _ = js("var c = document.querySelectorAll('[data-widget=\"s5-2\"] .abw-opt'); c[0].click(); c[2].click();")
            _ = Renderer.wait(3, step: 0.01) { answer("s5-2").isEmpty }
            check(draft.picks.map(\.widget) == ["s5-0"] && on("s5-2") == 0, "none picked: no answer: \(draft.picks)")

            // a right-click: a menu whose one item picks the option with a comment field that takes the keyboard; a
            // click elsewhere closes it, picking nothing
            let rightClick = { (id: String, i: Int) in
                _ = js("""
                    (function () { var c = document.querySelectorAll('[data-widget="\(id)"] .abw-opt')[\(i)], r = c.getBoundingClientRect();
                      c.dispatchEvent(new MouseEvent('contextmenu', {clientX: r.left + 20, clientY: r.top + 10, bubbles: true, cancelable: true})); })()
                    """)
            }
            rightClick("s5-2", 1)
            check(js("document.querySelector('[data-abw].abw-menu').textContent") as? String == "Choose with a comment…", "the menu: one item")
            _ = js("document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}))")
            check(js("document.querySelector('[data-abw].abw-menu') === null") as? Bool == true && on("s5-2") == 0, "a click elsewhere closes it")
            rightClick("s5-2", 1)
            _ = js("document.querySelector('[data-abw].abw-menu button').click()")
            _ = Renderer.wait(3, step: 0.01) { !answer("s5-2").isEmpty }
            let commented = draft.picks.first { $0.widget == "s5-2" }
            check(commented?.labels == ["Sliders"] && commented?.comment == "" && draft.focus == "s5-2" && on("s5-2") == 1,
                  "Choose with a comment…: picked, with an empty comment field that asks for the keyboard: \(draft.picks)")
            check(js("document.querySelector('[data-abw].abw-menu') === null") as? Bool == true, "and the menu goes")
            draft.note("only if cheap", on: "s5-2")
            draft.focus = nil
            rightClick("s5-2", 1)
            _ = js("document.querySelector('[data-abw].abw-menu button').click()")
            _ = Renderer.wait(3, step: 0.01) { draft.focus != nil }
            check(draft.picks.first { $0.widget == "s5-2" }?.picked == [1] && draft.picks.first { $0.widget == "s5-2" }?.comment == "only if cheap",
                  "again on a picked option: it stays picked, the comment kept: \(draft.picks)")

            // the cards follow the draft: an × in the reply box (drop) or Discard clears them
            draft.drop("s5-2")
            _ = Renderer.wait(3, step: 0.01) { on("s5-2") == 0 }
            check(on("s5-2") == 0 && on("s5-0") == 1, "an answer taken out of the draft: its card shows nothing picked")

            // text with a control character falls back and says why; a line separator in a label is drawn, and the
            // choice line stays one line
            for (id, fallback) in [("s9-1", "Fallback: a line break"), ("s9-3", "Fallback: a tab")] {
                check(js("document.querySelector('[data-widget=\"\(id)\"]').textContent") as? String == fallback,
                      "\(id): text with a control character keeps the plain text")
                check(problems.contains { $0.contains(id) && $0.contains("control character") }, "\(id): and says why: \(problems)")
            }
            _ = js("document.querySelectorAll('[data-widget=\"s9-2\"] .abw-opt')[0].click()")
            _ = Renderer.wait(3, step: 0.01) { !answer("s9-2").isEmpty }
            check(answer("s9-2") == "Two lines" && draft.picks.first { $0.widget == "s9-2" }?.question == "Which? Twice",
                  "the question and label are one line each: \(draft.picks)")

            // a label the mail hides past its card's edge (a run of non-breaking spaces), and format characters in the
            // question: the answer the draft takes is plain, so the reply box shows all of it
            _ = js("document.querySelectorAll('[data-widget=\"s10-0\"] .abw-opt')[0].click()")
            _ = Renderer.wait(3, step: 0.01) { !answer("s10-0").isEmpty }
            check(answer("s10-0") == "Keep the old results and delete the bucket"
                  && draft.picks.first { $0.widget == "s10-0" }?.question == "Which one?", "the answer made plain: \(draft.picks)")
            // a place the mail lays out right to left: its options still read left to right, in the order the reply
            // sends them
            check(js("getComputedStyle(document.querySelector('[data-widget=\"s10-1\"] .abw-t')).direction") as? String == "ltr",
                  "options read left to right whatever their place's direction")
            // mail cannot pass for the app's own: its "picked card" has none of a card's look, and its "menu" stays when
            // the app's menu opens and closes
            check(js("getComputedStyle(document.querySelector('#s10 .mail > .abw-choices .abw-opt')).borderTopColor") as? String
                  != js("getComputedStyle(document.querySelector('[data-widget=\"s10-0\"] .abw-on')).borderTopColor") as? String,
                  "the mail's own picked card is not drawn as one")
            rightClick("s10-0", 1)
            check(js("document.querySelectorAll('.abw-menu').length") as? Int == 2, "the app's menu beside the mail's")
            _ = js("document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}))")
            check(js("Array.prototype.map.call(document.querySelectorAll('.abw-menu'), function (m) { return m.textContent; })"
                     + ".join('|')") as? String == "fake menu", "the app's menu closes, the mail's stays")

            draft.clear()
            _ = Renderer.wait(3, step: 0.01) { on("s5-0") == 0 && on("s9-2") == 0 && on("s10-0") == 0 }
            check(js("document.querySelectorAll('[data-widget] .abw-on').length") as? Int == 0, "Discard clears every card")
            // an axis narrower than the numbers' spacing there (1e17 to 1e17 + 16) is drawn, not looped on
            check(js("document.querySelectorAll('[data-widget=\"s9-0\"] svg.abw-chart').length") as? Int == 1,
                  "a plot on a range of a few ulps is drawn")

            // the legend hides a line; hovering shows the values at the nearest x
            _ = js("document.querySelectorAll('[data-widget=\"s5-1\"] .abw-legend button')[1].click()")
            check(js("document.querySelectorAll('[data-widget=\"s5-1\"] svg.abw-chart path.abw-line').length") as? Int == 1, "a name hides its line")
            let hovered = hover("s5-1", 320, 150)
            check(hovered == "step 1000Muon: 0.52", "the tooltip: \(hovered ?? "none")")
            // a click opens the plot enlarged, in a window of its own (MailEnlarge, offscreen and behind here), drawn
            // again there larger; dragging across it zooms on x and a double-click resets it; Escape closes the window
            let realPlace = MailEnlarge.place
            MailEnlarge.place = { win in
                win.setFrame(NSRect(x: -20000, y: -20000, width: 1400, height: 900), display: true)
                win.orderBack(nil)
            }
            defer { MailEnlarge.place = realPlace; MailEnlarge.window?.close() }
            /// What `source` gives in the enlarged window's page, once the window has one.
            let big = { (source: String) in MailEnlarge.web.flatMap { Renderer.evaluate(source, in: $0, within: 5) } }
            let opened = { (selector: String) in
                Renderer.wait(5, step: 0.02) { big("document.querySelector('\(selector)') !== null") as? Bool == true }
            }
            check((js("document.querySelector('[data-widget=\"s5-1\"] .abw-hint').textContent") as? String ?? "")
                    .contains("click the plot to enlarge it"), "the plot says a click enlarges it")
            _ = js("document.querySelector('[data-widget=\"s5-1\"] svg.abw-chart').dispatchEvent(new MouseEvent('click', {bubbles: true}))")
            let card = js("document.querySelector('[data-widget=\"s5-1\"] svg.abw-chart').getBoundingClientRect().width") as? Double ?? 0
            check(opened("svg.abw-chart") && MailEnlarge.window?.title == js("document.querySelector('[data-widget=\"s5-1\"] .abw-title').textContent") as? String,
                  "a click opens the plot in a window titled with it")
            let wide = big("document.querySelector('svg.abw-chart').getBoundingClientRect().width") as? Double ?? 0
            check(wide > card * 1.4, "the window's plot is larger than the mail's: \(wide) against \(card)")
            let before = big("document.querySelector('svg.abw-chart g').textContent") as? String
            _ = big("""
                (function () { var s = document.querySelector('svg.abw-chart'), r = s.getBoundingClientRect();
                  function at(type, f) { s.dispatchEvent(new MouseEvent(type, {clientX: r.left + r.width * f, clientY: r.top + r.height * 0.5, bubbles: true})); }
                  at('mousedown', 0.3); at('mousemove', 0.6); at('mouseup', 0.6); })()
                """)
            let zoomed = big("document.querySelector('svg.abw-chart g').textContent") as? String
            check(zoomed != nil && zoomed != before, "a drag in the enlarged plot zooms in (the x ticks change)")
            _ = big("document.querySelector('svg.abw-chart').dispatchEvent(new MouseEvent('dblclick', {bubbles: true}))")
            check(big("document.querySelector('svg.abw-chart g').textContent") as? String == before, "a double-click resets the zoom")
            let shown = MailEnlarge.window
            shown?.cancelOperation(nil)
            check(shown?.isVisible == false && MailEnlarge.window == nil, "Escape closes the window, which is then forgotten")
            // a filled plot opens enlarged too, drawn again there whole
            _ = js("document.querySelector('[data-widget=\"s11-0\"] svg.abw-chart').dispatchEvent(new MouseEvent('click', {bubbles: true}))")
            check(opened("svg.abw-chart rect.abw-track") && big("document.querySelectorAll('rect.abw-track').length") as? Int == 9
                  && MailEnlarge.window?.title == "The long prompt moves the fixes to the bug tickets",
                  "a filled plot opens enlarged, whole")
            MailEnlarge.window?.close()

            // the mails' tables and images open enlarged too: a data table, not a layout table; an image, not a link's.
            // A click on a link in a table goes to the link
            check(js("JSON.stringify(Array.prototype.map.call(document.querySelectorAll('[data-abw-big]'), function (e) { return e.tagName; }))")
                  as? String == #"["TABLE","IMG"]"#, "the data table and the image are enlargeable, the layout tables and the linked image not")
            let shownBefore = MailEnlarge.window
            _ = js("""
                (function () { var a = document.querySelector('#results a');     // cancelled first: nothing is opened
                  a.addEventListener('click', function (e) { e.preventDefault(); }, {once: true});
                  a.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true})); })()
                """)
            check(!Renderer.wait(1, step: 0.02) { MailEnlarge.window !== shownBefore }, "a click on a link in a table opens nothing")
            _ = js("document.querySelectorAll('#results td')[1].click()")
            check(opened("body > table") && big("document.querySelector('body > table').textContent") as? String
                  == js("document.getElementById('results').textContent") as? String && MailEnlarge.window?.title == "Table",
                  "a click on the table opens it in the window")
            check(big("document.querySelectorAll('[id]').length") as? Int == 0, "the copy takes no id of the mail's")
            _ = js("document.querySelector('[data-abw-big][alt=\"a plot\"]').click()")
            check(opened("body > img") && (big("document.querySelector('body > img').getBoundingClientRect().width") as? Double ?? 0) > 1000
                  && MailEnlarge.window?.title == "a plot", "a click on an image opens it filling the window")
            // what the window refuses: an image from outside, a plot not in the thread, markup not a table, any other kind
            let refusals: [([String: Any], String)] = [
                (["kind": "image", "src": "https://example.com/x.png"], "the image is not the mail's own"),
                (["kind": "plot", "widget": "s9-9"], "the plot is not one of this thread's"),
                (["kind": "table", "html": "<div>not a table</div>"], "the table's markup was not understood"),
                (["kind": "script"], "it is not a plot, a table or an image")]
            for (item, why) in refusals {
                do {
                    _ = try MailEnlarge.content(item, widgets: widgets)
                    check(false, "not refused: \(why)")
                } catch {
                    check((error as? MailEnlarge.Refused)?.why == why, "refused, as \(why): \(error)")
                }
            }

            // a host without enlarge gets the plot enlarged in a box over the page, which Escape closes
            let boxed = Renderer.evaluate("""
                var d = document.createElement('div'), place = document.createElement('div');
                place.setAttribute('data-widget', 's5-1');
                d.appendChild(place);
                document.body.appendChild(d);
                AgentBoardWidgets.render(d, JSON.parse(widgets).filter(function (w) { return w.id === 's5-1'; }),
                                         {pick: function () {}, problem: function () {}});
                d.querySelector('svg.abw-chart').dispatchEvent(new MouseEvent('click', {bubbles: true}));
                var opened = document.querySelector('.abw-big .abw-big-plot svg.abw-chart') !== null;
                document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                var closed = document.querySelector('.abw-big') === null;
                d.remove();
                return [opened, closed];
                """, arguments: ["widgets": (try? MailWidgets.json(widgets)) ?? "[]"], in: web, world: MailWidgets.world,
                within: 5) as? [Bool]
            check(boxed == [true, true], "without enlarge, a box over the page that Escape closes: \(String(describing: boxed))")

            // a bar plot: a bar per category and series; the tooltip has each series' value; a name hides its bars
            check(js("document.querySelectorAll('[data-widget=\"s6-1\"] svg.abw-chart rect.abw-mark-0').length") as? Int == 3
                  && js("document.querySelectorAll('[data-widget=\"s6-1\"] svg.abw-chart rect.abw-mark-1').length") as? Int == 3,
                  "a bar per category and series")
            let bars = hover("s6-1", 58 + 564.0 / 6, 150)                     // the first category's middle
            check(bars == "size smallDone: 5Failed: 1", "the bar tooltip: \(bars ?? "none")")
            _ = js("document.querySelectorAll('[data-widget=\"s6-1\"] .abw-legend button')[1].click()")
            check(js("document.querySelectorAll('[data-widget=\"s6-1\"] svg.abw-chart rect.abw-mark-1').length") as? Int == 0,
                  "a name hides its bars")
            check(hover("s6-1", 58 + 564.0 / 6, 150) == "size smallDone: 5", "and its value leaves the tooltip")

            // a scatter plot: a marker per point, its own shape for each series; the tooltip at the nearest x
            check(js("document.querySelectorAll('[data-widget=\"s6-2\"] svg.abw-chart circle.abw-mark-0').length") as? Int == 3
                  && js("document.querySelectorAll('[data-widget=\"s6-2\"] svg.abw-chart rect.abw-mark-1').length") as? Int == 2,
                  "circles for one series, squares for the other")
            let dots = hover("s6-2", 58 + 564.0 / 2, 150)                      // x = 2, the middle of 1 to 3
            check(dots == "params 2A: 2B: –", "the scatter tooltip, – where a series has no point there: \(dots ?? "none")")
            _ = js("document.querySelectorAll('[data-widget=\"s6-2\"] .abw-legend button')[0].click()")
            check(js("document.querySelectorAll('[data-widget=\"s6-2\"] svg.abw-chart .abw-mark-0').length") as? Int == 0,
                  "a name hides its markers")

            // bars with textures: a colour per name and a fill per texture (solid, hatched, cross-hatched), a legend row
            // of each; the tooltip gives both; a name or a texture hides its bars, shown again once neither is off
            let tex = { (expr: String) in "(function (q) { return JSON.stringify(\(expr)); })(function (s) { return document.querySelectorAll('[data-widget=\"s6-5\"] ' + s); })" }
            check(js(tex("Array.prototype.map.call(q('.abw-legend'), function (row) { return Array.prototype.map.call(row.querySelectorAll('button'), function (b) { return b.textContent; }); })")) as? String
                  == #"[["long prompt","short prompt"],["feature-heavy list","bug-heavy list","mixed list"]]"#,
                  "a legend row of names over one of textures")
            let paints = js(tex("[0, 1, 2, 3, 4].map(function (i) { var f = q('rect.abw-mark-' + i)[0].getAttribute('fill'), m = /^url\\(#(.+)\\)$/.exec(f), p = m && document.getElementById(m[1]); return p ? [p.children.length, p.lastChild.getAttribute('fill')] : f; })")) as? String
            check(paints == ##"["#0072B2",[2,"#0072B2"],[3,"#0072B2"],"#E69F00",[2,"#E69F00"]]"##,
                  "a colour per name; solid, hatched and cross-hatched by texture: \(paints ?? "none")")
            check(js(tex("q('svg.abw-chart pattern').length")) as? String == "3"
                  && js("(function () { var ids = Array.prototype.map.call(document.querySelectorAll('[id]'), function (e) { return e.id; }); return ids.length === new Set(ids).size; })()") as? Bool == true,
                  "a pattern per textured series, and no id twice in the document")
            let both = hover("s6-5", 58 + 564.0 / 4, 150)                     // the first category's middle
            check(both == "model Model Along prompt, feature-heavy list: 15long prompt, bug-heavy list: 33"
                  + "long prompt, mixed list: 24short prompt, feature-heavy list: 29"
                  + "short prompt, bug-heavy list: 35", "the tooltip names both: \(both ?? "none")")
            let showing = { js(tex("[0, 1, 2, 3, 4].map(function (i) { return q('rect.abw-mark-' + i).length; })")) as? String }
            let click = { (row: Int, n: Int) in _ = js(tex("q('.abw-legend')[\(row)].querySelectorAll('button')[\(n)].click()")) }
            click(1, 1)
            check(showing() == "[2,0,2,2,0]", "a texture hides its bars: \(showing() ?? "none")")
            click(0, 0)
            check(showing() == "[0,0,0,2,0]", "and a name its own: \(showing() ?? "none")")
            click(1, 1)
            check(showing() == "[0,0,0,2,2]", "bars come back once neither their name nor their texture is off: \(showing() ?? "none")")

            // plots whose series are all empty, as the mailroom allows: the axes and a note, no marks, an empty tooltip
            for id in ["s8-0", "s8-1"] {
                check(js("document.querySelector('[data-widget=\"\(id)\"] svg.abw-chart .abw-empty').textContent") as? String
                      == "Nothing to show", "\(id): an empty plot says so")
                check(js("document.querySelectorAll('[data-widget=\"\(id)\"] svg.abw-chart [class^=\"abw-mark\"], "
                         + "[data-widget=\"\(id)\"] svg.abw-chart path.abw-line').length") as? Int == 0, "\(id): no marks")
                check(hover(id, 320, 150) == "", "\(id): no tooltip over an empty plot")
            }

            // a widget that does not check out keeps its plain-text version and says why; one with no place says so
            check(js("document.querySelector('[data-widget=\"s6-0\"]').textContent") as? String == "Fallback: pick 1 or 2",
                  "a bad spec keeps the plain text")
            check(problems.contains { $0.contains("s6-0") && $0.contains("options must be a list of 2 to 8") },
                  "and says why: \(problems)")
            check(problems.contains { $0.contains("s7-0") && $0.contains("no place") }, "a widget with no place: \(problems)")
            check(js("document.querySelector('[data-widget=\"s6-3\"]').textContent") as? String == "Fallback: a pie",
                  "a plot that does not check out keeps the plain text")
            check(problems.contains { $0.contains("s6-3") && $0.contains("kind must be line, scatter, bar or filled") },
                  "and says why: \(problems)")
            check(problems.contains { $0.contains("s6-6") && $0.contains("texture is for bar plots only") }
                  && problems.contains { $0.contains("s6-7") && $0.contains("every series has a texture, or none does") }
                  && problems.contains { $0.contains("s6-8") && $0.contains("at most 3 textures") }
                  && problems.contains { $0.contains("s6-9") && $0.contains("no two series have the same name and texture") }
                  && js("['s6-6', 's6-7', 's6-8', 's6-9'].map(function (id) { return document.querySelector('[data-widget=\"' + id + '\"]').textContent; }).join('|')") as? String
                  == "Fallback: a textured line|Fallback: one texture|Fallback: four textures|Fallback: twins",
                  "textures that break the rules: plain text, and why: \(problems)")
            // widgets that are not a list: said, nothing drawn
            _ = Renderer.evaluate("return AgentBoardWidgets.render(document, {}, agentBoardMailHost);", arguments: [:],
                                  in: web, world: MailWidgets.world, within: 5)
            _ = Renderer.wait(5, step: 0.01) { problems.contains { $0.contains("not a list") } }
            check(problems.contains { $0.contains("the widgets are not a list") }, "widgets not in a list: said: \(problems)")
            check(!problems.contains { $0.hasPrefix("refused") }, "the document was shown: \(problems)")

            // a host whose menu is false, as a touch screen's: a right-click (there, a long press) picks with a comment
            // at once, and no menu opens
            let touch = Renderer.evaluate("""
                var wrap = document.createElement('div'), place = document.createElement('div'), picked = [];
                place.setAttribute('data-widget', 's5-0');
                wrap.appendChild(place);
                document.body.appendChild(wrap);
                var host = {pick: function (a) { picked.push(a); }, menu: false, problem: function () {}};
                AgentBoardWidgets.render(wrap, JSON.parse(widgets), host);
                var cards = wrap.querySelectorAll('.abw-opt');
                var left = cards[1].dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true}));
                wrap.parentNode.removeChild(wrap);
                return JSON.stringify([picked, left, document.querySelector('[data-abw].abw-menu') === null]);
                """, arguments: ["widgets": try! MailWidgets.json([widgets[0]])], in: web, world: MailWidgets.world,
                within: 5) as? String
            check(touch == #"[[{"widget":"s5-0","picked":[1],"comment":true}],false,true]"#,
                  "a host with no menu: \(touch ?? "nothing")")

            // two questions in one mail and an option picked in each: the draft keeps both answers, and the reply
            // sends both
            let two = Draft(text: "", files: [], key: nil)
            let drawn = { (want: Int) in
                Renderer.wait(15, step: 0.01) { loader.ready } && Renderer.widgetsDrawn(web, want: want, within: 15) == want
            }
            loader.show(twoQuestions, thread: "T2", agentMail: ["s21"], widgets: twoWidgets, draft: two, current: nil, in: web)
            check(drawn(2), "the two questions are drawn")
            _ = js("document.querySelectorAll('[data-widget=\"s21-0\"] .abw-opt')[1].click()")
            _ = js("document.querySelectorAll('[data-widget=\"s21-1\"] .abw-opt')[0].click()")
            _ = Renderer.wait(3, step: 0.01) { two.picks.count == 2 }
            check(two.reply == "> Which cache policy should the loader use?\n> 2. LFU with ageing, 512 MB\nMy answer: option 2\n\n"
                  + "> When should it ship?\n> 1. Today\nMy answer: option 1", "both answers are sent: \(two.reply.debugDescription)")
            // shown again (another thread, then this one): the cards show the draft's answers
            loader.show(document, thread: "T1", agentMail: ["s5", "s6", "s9", "s10", "s11"], widgets: widgets, draft: draft, current: nil, in: web)
            check(drawn(14), "another thread")
            loader.show(twoQuestions, thread: "T2", agentMail: ["s21"], widgets: twoWidgets, draft: two, current: nil, in: web)
            check(drawn(2), "this thread again")
            let picked = js("""
                JSON.stringify(Array.prototype.map.call(document.querySelectorAll('.abw-opt'), function (c) {
                  return c.getAttribute('aria-checked'); }))
                """) as? String
            check(picked == #"["false","true","true","false"]"#, "the draft's answers are shown again: \(picked ?? "nothing")")
            if fails.isEmpty { print("widgets selftest: ok"); return 0 }
            fails.forEach { print("FAIL: " + $0) }
            return 1
        }
    }

    static let document = """
    <!DOCTYPE html><html><head><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="\(MailBlocker.policy)">
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
    <div class="widget" data-widget="s6-4">Fallback: long names</div>
    <div class="widget" data-widget="s6-5">Fallback: textures</div>
    <div class="widget" data-widget="s6-6">Fallback: a textured line</div>
    <div class="widget" data-widget="s6-7">Fallback: one texture</div>
    <div class="widget" data-widget="s6-8">Fallback: four textures</div>
    <div class="widget" data-widget="s6-9">Fallback: twins</div>
    <div class="widget" data-widget="s8-0">Fallback: no bars</div>
    <div class="widget" data-widget="s8-1">Fallback: no lines</div>
    </div></details>
    <details class="msg" id="s9" open><summary>agent</summary><div class="card">
    <div class="widget" data-widget="s9-0">Fallback: nanoseconds</div>
    <div class="widget" data-widget="s9-1">Fallback: a line break</div>
    <div class="widget" data-widget="s9-2">Fallback: a separator</div>
    <div class="widget" data-widget="s9-3">Fallback: a tab</div>
    </div></details>
    <details class="msg" id="s10" open><summary>agent</summary><div class="card"><div class="mail">
    <style>#s10 .mail { .abw-opt .abw-t { white-space: nowrap; overflow: hidden; max-width: 16em; } }</style>
    <div class="widget" data-widget="s10-0">Fallback: keep or delete</div>
    <div class="widget" data-widget="s10-1" dir="rtl" align="left">Fallback: right to left</div>
    <div class="abw-menu">fake menu</div><div class="abw-choices"><div class="abw-opt abw-on">Fake picked card</div></div>
    <table id="results"><tr><th>run</th><th>loss</th></tr><tr><td><a href="https://example.com/r1">r1</a></td><td>1.9</td></tr></table>
    <table><tr><td><table><tr><th>inner</th></tr></table></td><td>a layout table</td></tr></table>
    <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAQAAAACCAIAAADwyuo0AAAAEUlEQVR4nGP4z8AARwxI7P8AZ7IH+SY4448AAAAASUVORK5CYII=" alt="a plot" width="40" height="20">
    <a href="https://example.com/big"><img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAQAAAACCAIAAADwyuo0AAAAEUlEQVR4nGP4z8AARwxI7P8AZ7IH+SY4448AAAAASUVORK5CYII=" alt="a linked image"></a>
    </div></div></details>
    <details class="msg" id="s11" open><summary>agent</summary><div class="card"><div class="mail">
    <div class="widget" data-widget="s11-0">Fallback: filled bars</div>
    <div class="widget" data-widget="s11-1">Fallback: over 100</div>
    </div></div></details>
    </body></html>
    """

    /// A mail with two questions.
    static let twoQuestions = """
    <!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
    <details class="msg" id="s21" open><summary>agent</summary><div class="card"><div class="mail">
    <p>Two things to settle:</p>
    <div class="widget" data-widget="s21-0">Fallback: the cache</div>
    <div class="widget" data-widget="s21-1">Fallback: the day</div>
    </div></div></details></body></html>
    """

    static let twoWidgets: [MailWidget] = {
        let raw = """
        [{"id": "s21-0", "kind": "choices", "spec": {"question": "Which cache policy should the loader use?",
          "options": [{"label": "LRU, 256 MB"}, {"label": "LFU with ageing, 512 MB"}]}},
         {"id": "s21-1", "kind": "choices", "spec": {"question": "When should it ship?",
          "options": [{"label": "Today"}, {"label": "Monday"}]}}]
        """
        return try! JSONDecoder().decode([MailWidget].self, from: Data(raw.utf8))
    }()

    /// The categories of a bar plot whose names cannot stand side by side under their bars, the last 80 characters long.
    static let longNames = ["Model A, feature-heavy task list", "Model A, bug-heavy task list",
                            "Model B, feature-heavy task list", "Model B, bug-heavy task list",
                            "Model C at low reasoning effort, mixed list",
                            "Model C at high reasoning effort, mixed list, with the task brief repeated twice"]

    static let widgets: [MailWidget] = {
        let long = String(data: try! JSONEncoder().encode(longNames), encoding: .utf8)!
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
         {"id": "s6-4", "kind": "plot", "spec": {"title": "Option picked, by setting", "kind": "bar",
          "x": {"label": "setting"}, "y": {"label": "share of answers", "min": 0},
          "series": [{"name": "Picked", "x": \(long), "y": [0.62, 0.41, 0.58, 0.37, 0.5, 0.45]},
                     {"name": "Refused", "x": \(long), "y": [0.05, 0.12, 0.04, 0.15, 0.08, 0.1]}]}},
         {"id": "s6-5", "kind": "plot", "spec": {"title": "Fixes made, by prompt and task list", "kind": "bar",
          "x": {"label": "model"}, "y": {"label": "fixes made"},
          "series": [{"name": "long prompt", "texture": "feature-heavy list", "x": ["Model A", "Model B"], "y": [15, 16]},
                     {"name": "long prompt", "texture": "bug-heavy list", "x": ["Model A", "Model B"], "y": [33, 32]},
                     {"name": "long prompt", "texture": "mixed list", "x": ["Model A", "Model B"], "y": [24, 25]},
                     {"name": "short prompt", "texture": "feature-heavy list", "x": ["Model A", "Model B"], "y": [29, 31]},
                     {"name": "short prompt", "texture": "bug-heavy list", "x": ["Model A", "Model B"], "y": [35, 30]}]}},
         {"id": "s6-6", "kind": "plot", "spec": {"title": "Textured line", "kind": "line", "x": {}, "y": {},
          "series": [{"name": "a", "texture": "hatched", "x": [1], "y": [1]}]}},
         {"id": "s6-7", "kind": "plot", "spec": {"title": "One texture", "kind": "bar", "x": {}, "y": {},
          "series": [{"name": "a", "texture": "t", "x": ["c"], "y": [1]}, {"name": "b", "x": ["c"], "y": [2]}]}},
         {"id": "s6-8", "kind": "plot", "spec": {"title": "Four textures", "kind": "bar", "x": {}, "y": {},
          "series": [{"name": "a", "texture": "1", "x": ["c"], "y": [1]}, {"name": "a", "texture": "2", "x": ["c"], "y": [2]},
                     {"name": "a", "texture": "3", "x": ["c"], "y": [3]}, {"name": "a", "texture": "4", "x": ["c"], "y": [4]}]}},
         {"id": "s6-9", "kind": "plot", "spec": {"title": "Twins", "kind": "bar", "x": {}, "y": {},
          "series": [{"name": "a", "texture": "t", "x": ["c"], "y": [1]}, {"name": "a", "texture": "t", "x": ["c"], "y": [2]}]}},
         {"id": "s8-0", "kind": "plot", "spec": {"title": "No bars yet", "kind": "bar", "x": {}, "y": {},
          "series": [{"name": "a", "x": [], "y": []}, {"name": "b", "x": [], "y": []}]}},
         {"id": "s8-1", "kind": "plot", "spec": {"title": "No lines yet", "kind": "line", "x": {}, "y": {},
          "series": [{"name": "a", "x": [], "y": []}]}},
         {"id": "s9-0", "kind": "plot", "spec": {"title": "Nanoseconds", "kind": "line",
          "x": {"label": "time since the start of the run, in nanoseconds, as read on the scheduler clocks"},
          "y": {"label": "steps the scheduler had taken by then, counted from its first step in that job",
                "min": 100000000000000000, "max": 100000000000000016},
          "series": [{"name": "t", "x": [100000000000000000, 100000000000000016], "y": [1, 2]}]}},
         {"id": "s9-1", "kind": "choices", "spec": {"question": "Which?",
          "options": [{"label": "Keep\\nOption 9: delete everything"}, {"label": "Lift"}]}},
         {"id": "s9-2", "kind": "choices", "spec": {"question": "Which?\\u2029Twice",
          "options": [{"label": "Two\\u2028lines"}, {"label": "One"}]}},
         {"id": "s9-3", "kind": "plot", "spec": {"title": "Tabs", "kind": "line", "x": {}, "y": {},
          "series": [{"name": "a\\tb", "x": [1], "y": [1]}]}},
         {"id": "s10-0", "kind": "choices", "spec": {"question": "Which\\u200b one\\u202e?",
          "options": [{"label": "Keep the old results\(String(repeating: "\\u00a0", count: 70))and delete the bucket"},
                      {"label": "Delete the old results"}]}},
         {"id": "s10-1", "kind": "choices", "spec": {"question": "\\u05f3Shall I go ahead?\\u05f3",
          "options": [{"label": "\\u05f3Delete it.\\u05f3 Do not \\u05f3keep the old bucket\\u05f3"}, {"label": "Wait"}]}},
         {"id": "s11-0", "kind": "plot", "spec": {"title": "The long prompt moves the fixes to the bug tickets",
          "kind": "filled", "tick": "even spread", "x": {"label": "% of each set fixed"},
          "segments": [{"name": "bug tickets", "size": 40}, {"name": "shared code", "size": 20},
                       {"name": "feature tickets", "size": 40}],
          "rows": [{"group": "Model A", "label": "no prompt", "fill": [25, 8, 12.5], "tick": [15, 15, 15]},
                   {"group": "Model A", "label": "long prompt", "fill": [40, 10, 0], "tick": [15, null, 15]},
                   {"group": "Model B", "label": "long prompt, said twice over", "fill": [3.5, null, 0]}]}},
         {"id": "s11-1", "kind": "plot", "spec": {"title": "Over 100", "kind": "filled",
          "segments": [{"name": "a", "size": 1}, {"name": "b", "size": 1}], "rows": [{"label": "r", "fill": [50, 101]}]}},
         {"id": "s7-0", "kind": "plot", "spec": {}}]
        """
        return try! JSONDecoder().decode([MailWidget].self, from: Data(raw.utf8))
    }()
}
