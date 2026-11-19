import AppKit
import SwiftUI
import WebKit

/// Offscreen render of the real board views from a saved snapshot. Used for screenshots and a smoke test.
@MainActor
enum Renderer {
    static func render(snapshot path: String, to out: String, size: CGSize, dark: Bool,
                       select: String?, overview: Bool, zoom: CGFloat = 1, layoutFile: String? = nil) -> Int32 {
        guard let data = FileManager.default.contents(atPath: path) else {
            FileHandle.standardError.write("cannot read \(path)\n".data(using: .utf8)!)
            return 1
        }
        let snap: Snapshot
        do {
            snap = try JSON.decoder.decode(Snapshot.self, from: data)
        } catch {
            FileHandle.standardError.write("snapshot not understood: \(error)\n".data(using: .utf8)!)
            return 1
        }
        var layout = Layout()
        if let f = layoutFile {
            guard FileManager.default.fileExists(atPath: f) else { return fail("cannot read \(f)") }
            let (l, problem) = Layout.load(URL(fileURLWithPath: f))
            if let p = problem { return fail(p) }
            layout = l
        }
        let model = BoardModel(layout: layout, persist: false)
        model.apply(snap)
        model.link = .live
        model.lastMessage = Date(timeIntervalSince1970: snap.generated)
        model.now = Date(timeIntervalSince1970: snap.generated + 2)
        if let sel = select {
            model.selectedID = snap.agents.first { $0.id.hasPrefix(sel) || $0.display == sel }?.id
                ?? snap.worktrees.first { $0.id == sel || $0.path == sel || $0.name == sel }?.id
        }
        if overview { model.tab = .overview }
        model.zoom = zoom

        let view = BoardView(model: model).frame(width: size.width, height: size.height)
        let code = png(view, size: size, dark: dark, to: out)
        if code == 0 { print("wrote \(out) (\(Int(size.width))x\(Int(size.height)), \(snap.agents.count) agents)") }
        return code
    }

    /// The switch sheet with saved options (from --probe), for a screenshot.
    static func renderSwitch(options path: String, snapshot: String, to out: String) -> Int32 {
        guard let sd = FileManager.default.contents(atPath: snapshot), let snap = try? JSON.decoder.decode(Snapshot.self, from: sd),
              let agent = snap.agents.first(where: { $0.live }) else { return fail("cannot read a live agent from \(snapshot)") }
        return renderSheet(path, as: SwitchOptions.self, to: out, size: nil, dark: true) { o, model in
            SwitchSheet(session: SwitchSession(agent: agent, modelOnly: false), model: model, preloaded: o)
        }
    }

    /// The restart sheet from a saved plan (`agent-restart plan --json`), for a screenshot.
    static func renderRestart(plan path: String, to out: String) -> Int32 {
        renderSheet(path, as: RestartPlan.self, to: out, size: CGSize(width: 720, height: 620), dark: true) { p, model in
            RestartSheet(model: model, preloaded: p)
        }
    }

    /// The Time sheet from a saved time.json (`agent-board time --json`), for a screenshot; GROUP drills in.
    static func renderTime(_ path: String, to out: String, focus: TimeFocus, dark: Bool) -> Int32 {
        let tall = focus.group != nil || focus.day != nil || focus.range == 1
        return renderSheet(path, as: TimeDoc.self, to: out, size: CGSize(width: 900, height: tall ? 720 : 520), dark: dark) { doc, model in
            TimeSheet(model: model, preloaded: (doc, focus))
        }
    }

    /// The usage breakdown sheet from a saved usage_split answer's "split", for a screenshot.
    static func renderUsage(_ path: String, to out: String, dark: Bool) -> Int32 {
        renderSheet(path, as: UsageSplit.self, to: out, size: CGSize(width: 680, height: 700), dark: dark) { s, model in
            UsageSplitSheet(model: model, target: s.target, window: s.window, title: "\(s.target) · \(s.window): where it went",
                            preloaded: s)
        }
    }

    /// The meeting overlay as it covers one 1440x900 screen, for a meeting a minute away: one with a title and a video
    /// link, or with `untitled` one from a calendar seen as free/busy (no title, no link). Mid grey stands in for the
    /// screen behind it.
    static func renderMeeting(to out: String, dark: Bool, untitled: Bool) -> Int32 {
        let start = Date().timeIntervalSince1970 + 60
        let meeting = untitled
            ? Meeting(calendar: "Work", id: "render", title: nil, start: start, end: start + 1800, joinUrl: nil,
                      openUrl: "https://calendar.google.com/calendar/r/day", account: "work@example.com")
            : Meeting(calendar: "Personal", id: "render", title: "Weekly sync with the project team", start: start,
                      end: start + 1800, joinUrl: "https://meet.google.com/abc-defg-hij",
                      openUrl: "https://calendar.google.com/calendar/r/day", account: "owner@example.com")
        let view = MeetingOverlayView(meeting: meeting, onPrimary: {}, onDismiss: {}).background(Color.gray)
        return wrote(out, png(view, size: CGSize(width: 1440, height: 900), dark: dark, to: out))
    }

    /// A sheet drawn offscreen into a PNG at `out`: the file at `path` decoded as T and handed to `sheet` with a board
    /// that saves nothing (the shared body of --render-switch, --render-restart, --render-time and --render-usage). 0, or 1 with the
    /// reason on stderr.
    static func renderSheet<T: Decodable, V: View>(_ path: String, as type: T.Type, to out: String, size: CGSize?, dark: Bool,
                                                   sheet: (T, BoardModel) -> V) -> Int32 {
        guard let d = FileManager.default.contents(atPath: path) else { return fail("cannot read \(path)") }
        let value: T
        do { value = try JSON.decoder.decode(T.self, from: d) } catch { return fail("\(path) not understood: \(describeDecodeError(error))") }
        let model = BoardModel(layout: Layout(), persist: false)
        let view = sheet(value, model).background(Color(nsColor: .windowBackgroundColor))
        return wrote(out, png(view, size: size, dark: dark, to: out))
    }

    /// Draw `view` offscreen into a PNG at `out`: the shared body of the --render modes but --render-mail. A nil size
    /// takes the view's own fitting size. 0, or 1 with the reason on stderr.
    static func png<V: View>(_ view: V, size: CGSize?, dark: Bool, to out: String) -> Int32 {
        let (win, host) = offscreen(view, size: size, dark: dark)
        RunLoop.current.run(until: Date().addingTimeInterval(0.6))
        if size == nil { host.frame = NSRect(origin: .zero, size: host.fittingSize) }
        host.layoutSubtreeIfNeeded()
        return withExtendedLifetime(win) { write(host, to: out) }
    }

    /// `view` in a borderless window far off every screen, laid out once.
    static func offscreen<V: View>(_ view: V, size: CGSize?, dark: Bool) -> (NSWindow, NSHostingView<V>) {
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(origin: .zero, size: size ?? host.fittingSize)
        let win = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        win.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        win.contentView = host
        win.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        win.orderBack(nil)
        host.layoutSubtreeIfNeeded()
        return (win, host)
    }

    /// What `host` draws, as a PNG at `out`, with `over` drawn on top: pictures that another process draws (WebKit's),
    /// each in its rectangle in window coordinates. 0, or 1 with the reason on stderr.
    static func write(_ host: NSView, to out: String, over: [(NSImage, NSRect)] = []) -> Int32 {
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return fail("cannot draw the view") }
        host.cacheDisplay(in: host.bounds, to: rep)
        if !over.isEmpty {
            guard let context = NSGraphicsContext(bitmapImageRep: rep) else { return fail("cannot draw over the view") }
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = context
            for (image, rect) in over { image.draw(in: rect) }
            NSGraphicsContext.restoreGraphicsState()
        }
        guard let data = rep.representation(using: .png, properties: [:]) else { return fail("cannot encode the PNG") }
        do {
            try data.write(to: URL(fileURLWithPath: out))
        } catch {
            return fail("cannot write \(out): \(error)")
        }
        return 0
    }

    /// The Mail tab over a saved mail_list answer, with the threads of saved mail_show answers opened one after
    /// another and then the first again, once the tab has listed them and started its web view (MailWeb.warm): the
    /// real views and model (reading ahead and keeping threads as the app does), with the files answering for the
    /// desktop (each mail_show by its thread, every mark "ok"). Either kind of
    /// file may be the board action's answer or `agent-mail mailbox`'s own output, which has no "ok". It prints how
    /// long each thread took from being opened to its document in WebKit's page (its first message there), and draws
    /// the tab as it ends; WebKit draws the document in its own process, so it is captured with takeSnapshot and drawn
    /// over the rest.
    static func renderMail(list listPath: String, shows showPaths: [String], to out: String, dark: Bool) -> Int32 {
        func answer(_ path: String) -> [String: Any]? {
            guard let d = FileManager.default.contents(atPath: path),
                  var obj = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else { return nil }
            obj["ok"] = true
            return obj
        }
        guard let listAnswer = answer(listPath) else { return fail("cannot read a JSON object from \(listPath)") }
        let (list, listWhy) = Actions.decodeAnswer(listAnswer, nil, key: nil, as: MailList.self)
        guard let list = list else { return fail("\(listPath) not understood: \(listWhy ?? "")") }
        var shows: [MailShow] = []
        var saved: [String: [String: Any]] = [:]
        for path in showPaths {
            guard let a = answer(path) else { return fail("cannot read a JSON object from \(path)") }
            let (shown, why) = MailShow.decode(a, nil)
            guard let s = shown else { return fail("\(path) not understood: \(why ?? "")") }
            guard !s.messages.isEmpty else { return fail("\(path) has no message to see the document by") }
            shows.append(s)
            saved[s.thread.id] = a
        }
        guard let firstShow = shows.first else { return fail("--render-mail needs a mail_show answer") }
        let marked: [String: Any] = ["ok": true, "threads": [Any]()]
        let mail = MailModel(call: { request, done in
            let action = request["action"] as? String ?? ""
            let thread = request["thread"] as? String
            let reply: [String: Any]? = action == "mail_show" ? thread.flatMap { saved[$0] }
                : action == "mail_list" ? listAnswer : action == "mail_mark" ? marked : nil
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    done(reply, reply == nil ? "--render-mail has no answer for \(action) \(thread ?? "")" : nil)
                }
            }
        }, upload: { _, done in MainActor.assumeIsolated { done(nil, "--render-mail copies nothing to the desktop") } },
           ahead: MailModel.appAhead, cacheBytes: MailModel.appCacheBytes, draftsFolder: MailDrafts.scratchFolder())
        let summary: [String: Any] = ["generated": Date().timeIntervalSince1970, "agents": [Any](), "timeline": [Any](),
                                      "mail": ["held": [Any](), "held_count": 0],
                                      "mailbox": ["unread": list.counts?.unread ?? 0, "inbox": list.counts?.inbox ?? 0]]
        let snap: Snapshot
        do {
            snap = try JSON.decoder.decode(Snapshot.self, from: JSONSerialization.data(withJSONObject: summary))
        } catch {
            return fail("the stand-in snapshot did not decode: \(describeDecodeError(error))")
        }
        let model = BoardModel(layout: Layout(), persist: false, mail: mail)
        model.apply(snap)
        model.link = .live
        model.lastMessage = Date()
        model.tab = .mail
        model.zoom = 1                       // not the zoom saved on this Mac, as --render
        let size = CGSize(width: 1180, height: 760)
        let (win, host) = offscreen(BoardView(model: model).frame(width: size.width, height: size.height), size: size, dark: dark)
        return withExtendedLifetime(win) {
            guard wait(5, step: 0.05, until: { mail.threads != nil }) else { return fail("the Mail tab did not list the saved threads") }
            guard wait(15, step: 0.05, until: { MailWeb.loader.ready }) else { return fail("the web view did not start in 15 s") }
            var page: WKWebView?
            for s in shows + [firstShow] {
                let asked = Date()
                mail.open(s.thread.id)
                // a thread kept already shows at once, before SwiftUI has put the web view in the window: wait for both
                var shown: WKWebView?
                guard wait(15, step: 0.002, until: {
                    shown = find(WKWebView.self, in: host)
                    return mail.shown?.thread.id == s.thread.id && shown != nil
                }), let web = shown else {
                    return fail("thread \(s.thread.id) was not shown in 15 s" + (mail.threadError.map { ": " + $0 } ?? ""))
                }
                guard inPage(web, id: s.messages[0].key, within: 15) else {
                    return fail("thread \(s.thread.id)'s document did not reach the page in 15 s" + (mail.notice.map { ": " + $0 } ?? ""))
                }
                print(String(format: "thread %@ (%d characters) in the page %.3f s after it was opened",
                             s.thread.id, s.document.utf16.count, Date().timeIntervalSince(asked)))
                if !s.widgets.isEmpty {
                    let drawn = widgetsDrawn(web, want: s.widgets.count, within: 10)
                    print("thread \(s.thread.id): \(drawn) of \(s.widgets.count) widgets drawn"
                          + (drawn < s.widgets.count ? " (" + (mail.notice ?? "no reason given") + ")" : ""))
                }
                page = web
            }
            guard let web = page else { return fail("no document was shown") }
            RunLoop.current.run(until: Date().addingTimeInterval(0.5))     // its layout, and the scroll to the open message
            final class Taken { var image: NSImage?; var error: Error?; var done = false }
            let taken = Taken()
            web.takeSnapshot(with: nil) { image, error in taken.image = image; taken.error = error; taken.done = true }
            guard wait(10, step: 0.05, until: { taken.done }), let image = taken.image else {
                return fail("the document could not be captured: \(taken.error?.localizedDescription ?? "no picture in 10 s")")
            }
            let code = write(host, to: out, over: [(image, web.convert(web.bounds, to: nil))])
            if code == 0 { print("wrote \(out) (\(list.threads.count) threads, thread \(firstShow.thread.id) open)") }
            return code
        }
    }

    /// What `source` evaluates to in `web`'s page, once its answer comes or `seconds` pass (nil without one).
    static func evaluate(_ source: String, in web: WKWebView, within seconds: Double) -> Any? {
        final class Got { var value: Any?; var done = false }
        let got = Got()
        web.evaluateJavaScript(source) { v, _ in got.value = v; got.done = true }
        _ = wait(seconds, step: 0.002) { got.done }
        return got.value
    }

    /// Whether the element `id` (a message key) is in `web`'s page within `seconds`, asking again as each answer comes.
    static func inPage(_ web: WKWebView, id: String, within seconds: Double) -> Bool {
        guard MailLink.isKey(id) else { return false }
        return wait(seconds, step: 0.002) {
            evaluate("document.getElementById('\(id)') !== null", in: web, within: seconds) as? Bool == true
        }
    }

    /// How many of the document's widgets widgets.js has drawn, once `want` are or `seconds` pass.
    static func widgetsDrawn(_ web: WKWebView, want: Int, within seconds: Double) -> Int {
        var drawn = 0
        _ = wait(seconds, step: 0.01) {
            drawn = evaluate("document.querySelectorAll('.abw-drawn').length", in: web, within: seconds) as? Int ?? 0
            return drawn >= want
        }
        return drawn
    }

    /// Run the main run loop until `done` says so or `seconds` pass, asking `done` every `step` seconds; whether it did.
    static func wait(_ seconds: Double, step: Double, until done: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while !done() {
            if Date() > deadline { return false }
            RunLoop.current.run(until: Date().addingTimeInterval(step))
        }
        return true
    }

    /// The first view of type T in `view`'s tree, `view` included.
    static func find<T: NSView>(_ type: T.Type, in view: NSView) -> T? {
        if let v = view as? T { return v }
        for sub in view.subviews { if let v = find(type, in: sub) { return v } }
        return nil
    }

    static func fail(_ message: String) -> Int32 {
        FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
        return 1
    }

    static func wrote(_ out: String, _ code: Int32) -> Int32 {
        if code == 0 { print("wrote \(out)") }
        return code
    }

    static func icon(into dir: String) -> Int32 {
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        for (px, name) in [(16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"), (128, "128x128"),
                           (256, "128x128@2x"), (256, "256x256"), (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x")] {
            let host = NSHostingView(rootView: AppIcon().frame(width: 1024, height: 1024))
            host.frame = NSRect(x: 0, y: 0, width: 1024, height: 1024)
            guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                                             samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                             bytesPerRow: 0, bitsPerPixel: 0) else { return 1 }
            rep.size = NSSize(width: 1024, height: 1024)
            host.cacheDisplay(in: host.bounds, to: rep)
            guard let png = rep.representation(using: .png, properties: [:]) else { return 1 }
            try? png.write(to: URL(fileURLWithPath: "\(dir)/icon_\(name).png"))
        }
        return 0
    }
}

/// The icon: the board itself, four state-coloured tiles on a dark squircle.
struct AppIcon: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 230, style: .continuous)
                .fill(LinearGradient(colors: [Color(hex: 0x2A2F37), Color(hex: 0x14171C)], startPoint: .top, endPoint: .bottom))
                .frame(width: 824, height: 824)
            VStack(spacing: 44) {
                HStack(spacing: 44) { tile(AgentState.busy.color); tile(AgentState.waiting.color) }
                HStack(spacing: 44) { tile(AgentState.idle.color); tile(Color(hex: 0xEAD3BF)) }
            }
        }
        .frame(width: 1024, height: 1024)
    }

    func tile(_ c: Color) -> some View {
        RoundedRectangle(cornerRadius: 56, style: .continuous).fill(c).frame(width: 270, height: 270)
    }
}

/// Headless smoke test of the real transport: the StreamLink and Actions the app uses.
@MainActor
enum Probe {
    static func run() -> Int32 {
        let model = BoardModel(layout: Layout(), persist: false)
        model.connect()
        let deadline = Date().addingTimeInterval(25)
        while model.snapshot == nil && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
            if case .down(let why) = model.link { print("link: \(why)") }
        }
        guard let s = model.snapshot else { print("no snapshot within 25 s"); return 1 }
        print("snapshot from \(s.host ?? "?"): \(s.agents.count) agents, held \(s.mail.heldCount)")
        for a in s.agents { print("  \(a.state.padding(toLength: 8, withPad: " ", startingAt: 0)) \(a.cli) \(a.display)  \(a.repoBranch)") }
        while model.metrics == nil && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.2)) }
        if let m = model.metrics { print("metrics: ram \(Fmt.bytes(m.mem?.used))/\(Fmt.bytes(m.mem?.total)), \(m.disks.count) disks, \(m.gpus.count) gpu") }
        print("usage: \(model.usage?.source ?? "none"), \(model.usage?.accounts.count ?? 0) accounts")
        probeChats()
        probeMail(s)
        if let target = s.agents.first(where: { $0.live }) {     // the click path: over the open stream
            var streamed: (Data, String)??
            let t0 = Date()
            Actions.viaStream(["action": "check_close", "id": target.id]) { streamed = .some($0) }
            while streamed == nil && Date() < deadline.addingTimeInterval(30) { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
            if let r = streamed ?? nil, let obj = try? JSONSerialization.jsonObject(with: r.0) as? [String: Any] {
                print(String(format: "check_close over the stream: ok=%@ in %.2f s", String(describing: obj["ok"] ?? "?"), Date().timeIntervalSince(t0)))
            } else {
                print("check_close over the stream: NOT answered (\(streamed == nil ? "timeout" : "the collector does not take actions"))")
            }
        }
        model.stream.stop()
        guard let target = s.agents.first(where: { $0.live }) else { return 0 }
        var result: ActionResult?
        Actions.run(["action": "check_close", "id": target.id]) { result = $0 }
        while result == nil && Date() < deadline.addingTimeInterval(30) { RunLoop.current.run(until: Date().addingTimeInterval(0.2)) }
        print("check_close \(target.display): ok=\(result?.ok ?? false) \(result?.message.prefix(300) ?? "timeout")")
        var bad: ActionResult?
        Actions.run(["action": "close", "id": "not-a-session"]) { bad = $0 }
        while bad == nil && Date() < deadline.addingTimeInterval(40) { RunLoop.current.run(until: Date().addingTimeInterval(0.2)) }
        print("invalid id rejected: \(!(bad?.ok ?? true)) (\(bad?.message ?? ""))")
        var opts: [String: Any]?
        var optsErr: String?
        var done = false
        Actions.runJSON(["action": "switch_options", "id": target.id]) { o, e in opts = o; optsErr = e; done = true }
        while !done && Date() < deadline.addingTimeInterval(90) { RunLoop.current.run(until: Date().addingTimeInterval(0.2)) }
        let (decoded, why) = Actions.decodeAnswer(opts, optsErr, key: "options", as: SwitchOptions.self)
        if let o = decoded {
            print("switch options for \(target.display): busy=\(o.agent.busy ?? false) targets=\(o.targets.map { $0.target }) "
                  + "models=\(o.models.mapValues { $0.count })")
            if let path = ProcessInfo.processInfo.environment["AGENTBOARD_SAVE_SWITCH"], let raw = opts?["options"] {
                do {
                    try JSONSerialization.data(withJSONObject: raw).write(to: URL(fileURLWithPath: path))
                    print("switch options saved to \(path)")
                } catch {
                    print("switch options NOT saved to \(path): \(error.localizedDescription)")
                }
            }
        } else {
            print("switch options NOT decoded: \(why ?? "")")
        }
        return 0
    }

    /// One read through Actions.fetch, waited for (60 s at most).
    static func fetched<T: Decodable>(_ req: [String: Any], key: String?, as type: T.Type) -> (T?, String?) {
        var got: (T?, String?)?
        Actions.fetch(req, key: key, as: type) { value, why in got = (value, why) }
        _ = Renderer.wait(60, step: 0.05) { got != nil }
        return got ?? (nil, "no answer in 60 s")
    }

    /// The Chats sheet's reads, as the sheet makes them: the list, then the first chat and its posts.
    static func probeChats() {
        let (list, why) = fetched(["action": "chat_list"], key: "chats", as: [ChatSummary].self)
        guard let list = list else { print("chat_list NOT decoded: \(why ?? "")"); return }
        print("chat_list: \(list.count) chats")
        guard let first = list.first else { return }
        let (chat, showWhy) = fetched(["action": "chat_show", "group": first.id], key: "chat", as: Chat.self)
        print(chat.map { "chat_show \($0.title): \($0.members.count) members, \($0.unnamed?.count ?? 0) left out" }
              ?? "chat_show NOT decoded: \(showWhy ?? "")")
        let (posts, postsWhy) = fetched(["action": "chat_posts", "group": first.id], key: "posts", as: [ChatPost].self)
        print(posts.map { "chat_posts \(first.title): \($0.count) posts" } ?? "chat_posts NOT decoded: \(postsWhy ?? "")")
    }

    /// The Mail tab's reads, which change nothing (showing a thread does not mark it read): the inbox, then its newest
    /// thread. The snapshot's mailbox section first: without it the tab asks for nothing.
    static func probeMail(_ s: Snapshot) {
        guard let box = s.mailbox else { print("mailbox: none in the snapshot, so the Mail tab reads nothing"); return }
        print("mailbox: \(box.unread) unread, \(box.inbox) in the inbox")
        let (list, why) = fetched(["action": "mail_list", "box": MailBox.inbox.rawValue], key: nil, as: MailList.self)
        guard let list = list else { print("mail_list NOT decoded: \(why ?? "")"); return }
        print("mail_list: \(list.threads.count) threads, counts \(list.counts.map { "\($0)" } ?? "none")"
              + (list.dropped.isEmpty ? "" : ", \(list.dropped.count) not understood: \(list.dropped[0])"))
        guard let first = list.threads.first else { return }
        let (shown, showWhy) = fetched(["action": "mail_show", "thread": first.id], key: nil, as: MailShow.self)
        print(shown.map { "mail_show \(first.subject): \($0.messages.count) messages, a document of \($0.document.count) characters" }
              ?? "mail_show NOT decoded: \(showWhy ?? "")")
    }
}


/// Headless check that the window shortcuts reach the model: posts ⌘= ⌘= ⌘- ⌘0 into this process's own event
/// queue for an offscreen board window and reads the zoom back. Nothing is shown and nothing is saved.
@MainActor
enum KeyTest {
    static func run() -> Int32 {
        let model = BoardModel(layout: Layout(), persist: false)
        model.zoom = 1
        let win = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: 600, height: 400), styleMask: [.titled],
                           backing: .buffered, defer: false)
        win.contentView = NSHostingView(rootView: BoardView(model: model))
        win.orderBack(nil)
        installKeyMonitor(window: win, model: model)
        func press(_ chars: String, shift: Bool = false) -> CGFloat {
            let flags: NSEvent.ModifierFlags = shift ? [.command, .shift] : [.command]
            if let e = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags, timestamp: 0,
                                        windowNumber: win.windowNumber, context: nil, characters: chars,
                                        charactersIgnoringModifiers: chars, isARepeat: false, keyCode: 0) {
                NSApp.postEvent(e, atStart: false)
            }
            let until = Date().addingTimeInterval(0.3)
            while Date() < until, let ev = NSApp.nextEvent(matching: .any, until: until, inMode: .default, dequeue: true) {
                NSApp.sendEvent(ev)
            }
            return model.zoom
        }
        let results = [("⌘=", press("=")), ("⌘+ (shift)", press("+", shift: true)), ("⌘-", press("-")), ("⌘0", press("0"))]
        for (k, z) in results { print("\(k) -> zoom \(z)") }
        // ⌘↩ in a dialog's text box presses its main button; plain Return still types a newline
        let (_, box) = textArea(width: 200, height: 80)
        var submitted = 0
        box.onSubmit = { submitted += 1 }
        func key(_ code: UInt16, _ flags: NSEvent.ModifierFlags) {
            if let e = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags, timestamp: 0, windowNumber: 0,
                                        context: nil, characters: "\r", charactersIgnoringModifiers: "\r", isARepeat: false, keyCode: code) {
                box.keyDown(with: e)
            }
        }
        key(36, [.command]); key(76, [.command]); key(36, [])
        print("⌘↩ submits: \(submitted == 2) · Return types a newline: \(box.string == "\n")")
        let ok = results.map { $0.1 } == [1.1, 1.25, 1.1, 1.0] && submitted == 2 && box.string == "\n"
        print(ok ? "keys OK" : "keys FAILED")
        return ok ? 0 : 1
    }
}


/// Headless check of the group / sub-group operations, of reading and saving a layout file (a temporary one), and
/// that the saved layout still loads.
@MainActor
enum LayoutTest {
    static func run() -> Int32 {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append(what) } }
        let m = BoardModel(layout: Layout(), persist: false)
        m.layout.order = ["a", "b", "c", "d", "e"]
        let g1 = m.newGroup(name: "Projects", with: "a")
        let s1 = m.newGroup(name: "Alpha", with: "b", parent: g1)
        let s2 = m.newGroup(name: "Beta", with: "c", parent: g1)
        check(m.layout.groups.count == 1 && m.layout.groups[0].subgroups.map { $0.name } == ["Alpha", "Beta"], "sub-groups created")
        check(m.layout.group(of: "b") == s1 && m.layout.group(of: "a") == g1, "innermost group of a tile")
        m.move("d", toGroup: s2)
        m.move("e", before: "b")
        check(m.layout.find(s1)?.members == ["e", "b"] && m.layout.find(s2)?.members == ["c", "d"], "tiles into sub-groups")
        m.moveGroup(s2, before: s1)
        check(m.layout.groups[0].subgroups.map { $0.name } == ["Beta", "Alpha"], "reorder sub-groups")
        let g2 = m.newGroup(name: "Tools")
        m.moveGroup(s1, before: g2)                      // drag a sub-group onto a top-level group: it becomes one
        check(m.layout.groups.map { $0.name } == ["Projects", "Alpha", "Tools"], "promote by drag")
        m.moveGroup(s1, into: g1)
        check(m.layout.groups.map { $0.name } == ["Projects", "Tools"] && m.layout.find(g1)?.subgroups.count == 2, "demote into a group")
        m.moveGroup(g1, into: g2)                        // has sub-groups: refused (one level)
        check(m.layout.groups.count == 2, "one level only")
        m.promoteGroup(s2)
        check(m.layout.groups.map { $0.name } == ["Projects", "Beta", "Tools"], "promote from menu")
        m.deleteGroup(s1)                                 // a sub-group's tiles go to its group
        check(m.layout.find(g1)?.members == ["a", "e", "b"], "delete sub-group keeps tiles in the group")
        m.deleteGroup(g1)
        check(m.layout.order.contains("a") && m.layout.order.contains("b"), "delete group sends tiles to ungrouped")
        m.toggle(\.worktreesCollapsed)
        check(m.layout.worktreesCollapsed && !m.layout.offlineCollapsed, "a fixed section folds by itself")
        let old = #"{"groups":[{"id":"\#(UUID().uuidString)","name":"Old","members":["x"],"collapsed":false}],"order":["y"]}"#
        let decoded = try? JSONDecoder().decode(Layout.self, from: Data(old.utf8))
        check(decoded?.groups.first?.name == "Old" && decoded?.groups.first?.subgroups.isEmpty == true, "old layout decodes")
        // the layout file, in a temporary directory: missing, unreadable, with groups that do not decode, saved
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("agentboard-layout-\(UUID().uuidString)")
        let file = dir.appendingPathComponent("layout.json")
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let (empty, emptyProblem) = Layout.load(file)
            check(empty == Layout() && emptyProblem == nil, "no file: an empty layout, nothing wrong")
            try Data("not json".utf8).write(to: file)
            let (bad, badProblem) = Layout.load(file)
            check(bad == Layout() && badProblem != nil, "an unreadable file: an empty layout and the error")
            let mixed = #"{"groups":[{"name":"Good","members":["x"]},7,{"name":"Bad","members":"x"}],"order":["y"]}"#
            try Data(mixed.utf8).write(to: file)
            let (partial, partialProblem) = Layout.load(file)
            check(partial.groups.map { $0.name } == ["Good"] && partial.order == ["y"], "a group that does not decode loses only itself")
            check((partialProblem ?? "").hasPrefix("2 groups in "), "the groups left out are named")
            var kept = partial
            kept.worktreesCollapsed = true
            try kept.save(to: file)
            let (again, againProblem) = Layout.load(file)
            check(again.groups.map { $0.name } == ["Good"] && againProblem == nil, "saved, it loads cleanly")
            check(again.worktreesCollapsed && !again.offlineCollapsed, "and keeps which fixed sections are folded")
        } catch {
            fails.append("the layout file test could not write \(dir.path): \(error.localizedDescription)")
        }
        try? FileManager.default.removeItem(at: dir)          // a temporary directory: the system clears it anyway
        let (saved, savedProblem) = Layout.load(Layout.url)
        if let p = savedProblem {
            fails.append("the saved layout: \(p)")
        } else {
            print("saved layout: \(saved.groups.count) groups (\(saved.groups.map { "\($0.name):\($0.allMembers.count)" }.joined(separator: ", "))), \(saved.order.count) ordered")
        }
        print(fails.isEmpty ? "layout OK" : "layout FAILED: " + fails.joined(separator: "; "))
        return fails.isEmpty ? 0 : 1
    }
}
