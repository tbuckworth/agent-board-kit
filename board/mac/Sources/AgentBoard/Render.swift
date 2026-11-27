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

    /// The Chats sheet from saved chats, one chat and its posts (ChatsPreload), for a screenshot.
    static func renderChats(_ path: String, to out: String, dark: Bool) -> Int32 {
        renderSheet(path, as: ChatsPreload.self, to: out, size: CGSize(width: 1000, height: 660), dark: dark) { p, model in
            ChatsSheet(model: model, selected: nil, preloaded: p)
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

    /// The ask box on the desk with a sample exchange (one answer in, with what the desk sent for the owner under it,
    /// and the next being answered, what the desk is doing said and its first sentence spoken by the desktop's voice;
    /// stand-ins answer for the desktop and the speaker), beside one with no agent found behind the window, on the desk
    /// still, and one asking an agent's copy, whose answer this Mac's own voice read, the desktop's voice not answering.
    static func renderAsk(to out: String, dark: Bool) -> Int32 {
        func agent(_ n: Int, _ name: String, _ status: String, _ folder: String) -> AskAgent {
            AskAgent(id: String(repeating: String(n), count: 8) + "-0000-4000-8000-000000000000", name: name,
                     status: status, folder: folder)
        }
        let loader = agent(1, "loader-fix", "busy", "agent-board-loader")
        let agents = [loader, agent(2, "sweep-runner", "idle", "experiments"), agent(3, "docs-tidy", "idle", "admin"),
                      agent(4, "eval-sweep", "busy", "evals"), agent(5, "web-fix", "waiting", "website")]
        var asked = 0
        let found = AskBox(look: AskLook(agent: loader, said: "You're looking at Google Chrome: Re: Loader cache: two "
                                         + "choices before I merge. Behind it: loader-fix (matched by its mail tag "
                                         + "[#ab12cd34]).", agents: agents),
                           send: { _, onPart, done in
                               asked += 1
                               if asked == 1 {
                                   done(["ok": true, "text": "Done: I told loader-fix to keep the cache on disk and "
                                         + "merge once the eviction test passes.", "timings": ["end": 2.3], "parts": 1,
                                         "sends": [["agent": "loader-fix", "text": "Keep the cache on disk, and merge "
                                                    + "once the eviction test passes.", "ok": true, "ref": "k3f9"]]],
                                        nil)
                               } else {
                                   onPart?(AskTest.part(0, "Asking sweep-runner."))
                                   onPart?(["status": "Asking sweep-runner…"])
                               }
                           }, speaker: AskTest.Speaker(), timers: false)
        for q in ["Tell loader-fix to keep the cache on disk, and to merge once the eviction test passes.",
                  "And how far along is sweep-runner's sweep?"] {
            found.text = q
            found.submit()
        }
        let none = AskBox(look: AskLook(agent: nil, said: "You're looking at Finder: Downloads. No agent found behind it.",
                                        agents: agents),
                          send: { _, _, _ in }, speaker: AskTest.Speaker(), timers: false)
        let down = "the desktop's voice (agent-speech) is not answering on ~/.local/state/agent-board/voice-desk/"
            + "speech.sock: Connection refused"
        let mac = AskBox(look: AskLook(agent: agents[1], said: "You're looking at kitty: sweep-runner. Behind it: "
                                       + "sweep-runner (matched by its tmux session sweep-runner, the one last typed "
                                       + "in).",
                                       agents: agents),
                         send: { _, onPart, done in
                             onPart?(AskTest.part(0, "The sweep is half done, twelve of twenty-four runs.",
                                                  error: down))
                             done(["ok": true, "agent": "sweep-runner", "seconds": 6.1, "parts": 1,
                                   "answer": "The sweep is half done, twelve of twenty-four runs."], nil)
                         }, speaker: AskTest.Speaker(), timers: false)
        mac.choose(.agent(agents[1]))
        mac.text = "How far along is the sweep?"
        mac.submit()
        let boxes = [found, none, mac]
        let view = HStack(alignment: .top, spacing: 20) {
            ForEach(boxes.indices, id: \.self) { i in
                AskBoxView(box: boxes[i], close: {})
                    .background(RoundedRectangle(cornerRadius: 12).fill(Color(nsColor: .windowBackgroundColor)))
            }
        }
        .padding(20)
        .background(Color.gray)
        return wrote(out, png(view, size: nil, dark: dark, to: out))
    }

    /// The top bar with Chrome's keyring on the desktop locked (KeyringSample), above four unlock sheets: a password
    /// typed, then the answers to a wrong one, to login's alone (partial) and to the right one (stand-ins answer for
    /// the desktop), for a screenshot.
    static func renderKeyring(to out: String, dark: Bool) -> Int32 {
        let snap: Snapshot
        do { snap = try KeyringSample.locked(bootId: "render") } catch {
            return fail("the render's snapshot is not understood: \(describeDecodeError(error))")
        }
        let model = BoardModel(layout: Layout(), persist: false)
        model.apply(snap)
        model.link = .live
        func answering(_ line: String) -> KeyringUnlock { KeyringUnlock(send: { _ in (Data(line.utf8), "") }) }
        let cols = #""collections": {"default": {"path": "/c/Default_5fkeyring", "label": "Default keyring"}, "#
            + #""login": {"path": "/c/login", "label": "Login"}}"#
        let typed = answering("")
        typed.password = "a password"
        let wrong = answering(#"{"result": "wrong", "locked": true, "tried": {"default": "wrong"}, "# + cols + "}")
        let partial = answering(#"{"result": "partial", "locked": true, "tried": {"default": "wrong", "#
                                + #""login": "unlocked"}, "# + cols + "}")
        let right = answering(#"{"result": "unlocked", "locked": false}"#)
        let answered = [wrong, partial, right]
        for u in answered {
            u.password = "a password"
            u.submit()
        }
        let end = Date().addingTimeInterval(5)
        while answered.contains(where: { $0.said == nil }) && Date() < end {
            RunLoop.current.run(until: Date().addingTimeInterval(0.02))
        }
        let sheets = [typed] + answered
        let view = VStack(alignment: .leading, spacing: 16) {
            TopBar(model: model).frame(width: 1420)
            HStack(alignment: .top, spacing: 16) {
                ForEach(sheets.indices, id: \.self) { i in
                    KeyringSheet(model: model, unlock: sheets[i])
                        .background(RoundedRectangle(cornerRadius: 10).stroke(Color.secondary.opacity(0.4)))
                }
            }
        }
        .padding(16)
        .background(Color(nsColor: .windowBackgroundColor))
        return wrote(out, png(view, size: nil, dark: dark, to: out))
    }

    /// A sheet drawn offscreen into a PNG at `out`: the file at `path` decoded as T and handed to `sheet` with a board
    /// that saves nothing (the shared body of --render-switch, --render-restart, --render-time, --render-usage and
    /// --render-chats). 0, or 1 with the
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

    /// What `host` draws, as a PNG at `out`. 0, or 1 with the reason on stderr.
    static func write(_ host: NSView, to out: String) -> Int32 {
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return fail("cannot draw the view") }
        host.cacheDisplay(in: host.bounds, to: rep)
        return write(rep, to: out)
    }

    /// `rep` as a PNG at `out`. 0, or 1 with the reason on stderr.
    static func write(_ rep: NSBitmapImageRep, to out: String) -> Int32 {
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
    /// desktop (MailStandIn). It prints how long each thread took from being opened to its document in WebKit's page
    /// (its first message there), how many of its widgets were drawn and of its draft's comments highlighted, and draws
    /// the tab as it ends; WebKit draws the document in its own process, so it is captured with takeSnapshot and drawn
    /// over the rest (picture). `snapshot`: a saved
    /// snapshot whose agents (and their states) the board has, for the threads' busy dots; without one the board has
    /// no agents. `drafts`: a saved drafts.json, copied into the render's own drafts folder, so the threads have those
    /// replies (their answers shown on the cards, their comments highlighted); without one there are none.
    /// `archive`: the open thread is then archived, as e does, so the Undo bar shows, with no thread open (the tab says
    /// so where the thread was). `popout`: the last thread given is then popped out into a
    /// window of its own (MailWindows, off screen), drawn with its title bar beside the tab; it prints how long that
    /// thread took to reach the window's page, in the window's new web view.
    static func renderMail(list listPath: String, shows showPaths: [String], to out: String, dark: Bool,
                           snapshot snapshotPath: String? = nil, drafts draftsPath: String? = nil,
                           archive: Bool = false, popout: Bool = false) -> Int32 {
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
        let draftsFolder = MailDrafts.scratchFolder()       // the render's own; saved drafts given are copied into it
        if let path = draftsPath {
            do {
                try FileManager.default.createDirectory(at: draftsFolder, withIntermediateDirectories: true)
                try FileManager.default.copyItem(at: URL(fileURLWithPath: path),
                                                 to: draftsFolder.appendingPathComponent("drafts.json"))
            } catch {
                return fail("the saved drafts were not copied: \(error.localizedDescription)")
            }
        }
        let desktop = MailStandIn(list: listAnswer, shows: saved, name: "--render-mail")
        let mail = MailModel(call: desktop.call, upload: { _, done in
            MainActor.assumeIsolated { done(nil, "--render-mail copies nothing to the desktop") }
        }, ahead: MailModel.appAhead, cacheBytes: MailModel.appCacheBytes, draftsFolder: draftsFolder)
        if let why = mail.drafts.loadProblem { return fail("the saved drafts were not read: \(why)") }
        var summary: [String: Any] = ["generated": Date().timeIntervalSince1970, "agents": [Any](), "timeline": [Any](),
                                      "mail": ["held": [Any](), "held_count": 0]]
        if let path = snapshotPath {
            guard let d = FileManager.default.contents(atPath: path),
                  let saved = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else {
                return fail("cannot read a JSON object from \(path)")
            }
            summary = saved
        }
        if summary["mailbox"] == nil {
            summary["mailbox"] = ["unread": list.counts?.unread ?? 0, "inbox": list.counts?.inbox ?? 0]
        }
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
            /// The web view once thread `s` is shown and its document in the page, or why not.
            @MainActor func reached(_ s: MailShow) -> (WKWebView?, String?) {
                // a thread kept already shows at once, before SwiftUI has put the web view in the window: wait for both
                var shown: WKWebView?
                guard wait(15, step: 0.002, until: {
                    shown = find(WKWebView.self, in: host)
                    return mail.shown?.thread.id == s.thread.id && shown != nil
                }), let web = shown else {
                    return (nil, "thread \(s.thread.id) was not shown in 15 s" + (mail.threadError.map { ": " + $0 } ?? ""))
                }
                guard inPage(web, id: s.messages[0].key, within: 15) else {
                    return (nil, "thread \(s.thread.id)'s document did not reach the page in 15 s" + (mail.notice.map { ": " + $0 } ?? ""))
                }
                return (web, nil)
            }
            var page: WKWebView?
            for s in shows + [firstShow] {
                let asked = Date()
                mail.open(s.thread.id)
                let (reach, why) = reached(s)
                guard let web = reach else { return fail(why ?? "") }
                print(String(format: "thread %@ (%d characters) in the page %.3f s after it was opened",
                             s.thread.id, s.document.utf16.count, Date().timeIntervalSince(asked)))
                if !s.widgets.isEmpty {
                    let drawn = widgetsDrawn(web, want: s.widgets.count, within: 10)
                    print("thread \(s.thread.id): \(drawn) of \(s.widgets.count) widgets drawn"
                          + (drawn < s.widgets.count ? " (" + (mail.notice ?? "no reason given") + ")" : ""))
                }
                let comments = Set(mail.draft(for: s.thread.id).comments.map(\.id))
                if !comments.isEmpty {
                    var lit = 0
                    _ = wait(5, step: 0.01) {
                        lit = evaluate("new Set(Array.prototype.map.call(document.querySelectorAll('mark[data-abk-comment]'), "
                                       + "function (m) { return m.getAttribute('data-abk-comment'); })).size",
                                       in: web, within: 5) as? Int ?? 0
                        return lit >= comments.count
                    }
                    print("thread \(s.thread.id): \(lit) of \(comments.count) comments highlighted")
                }
                page = web
            }
            if archive {
                mail.archiveOrRestore()
                guard wait(5, step: 0.05, until: { !mail.undos.isEmpty && mail.tab == nil }) else {
                    return fail("thread \(firstShow.thread.id)'s archive was not offered to Undo" + (mail.notice.map { ": " + $0 } ?? ""))
                }
                page = nil                                          // no thread open: the tab shows no document
            }
            let webs = page.map { [$0] } ?? []
            guard let popped = popout ? shows.last : nil else {
                RunLoop.current.run(until: Date().addingTimeInterval(0.5))  // its layout, and the scroll to the open message
                let (tab, why) = picture(host, webs: webs)
                guard let image = tab else { return fail(why ?? "") }
                let code = write(image, to: out)
                if code == 0 { print("wrote \(out) (\(list.threads.count) threads, thread \(mail.selected ?? "none") open)") }
                return code
            }
            // the last thread given, popped out into a window of its own, drawn beside the tab
            let windows = MailWindows(mail: mail, zoom: model.$zoom.eraseToAnyPublisher(), onScreen: false)
            let id = popped.thread.id
            let asked = Date()
            mail.popOut(id)
            var popWeb: MailWebView?
            guard wait(15, step: 0.002, until: {
                popWeb = windows.windows[id]?.contentView.flatMap { find(MailWebView.self, in: $0) }
                return mail.popouts[id]?.shown != nil && popWeb != nil
            }), let other = popWeb, let window = windows.windows[id], let frame = window.contentView?.superview else {
                return fail("thread \(id) was not popped out in 15 s" + (mail.popouts[id]?.error.map { ": " + $0 } ?? ""))
            }
            window.appearance = win.appearance               // the render's light or dark, not this Mac's
            guard inPage(other, id: popped.messages[0].key, within: 15) else {
                return fail("thread \(id)'s document did not reach its window's page in 15 s")
            }
            print(String(format: "thread %@ in its own window's page %.3f s after it was popped out (a new web view)",
                         id, Date().timeIntervalSince(asked)))
            if !popped.widgets.isEmpty {
                print("thread \(id) in its window: \(widgetsDrawn(other, want: popped.widgets.count, within: 10)) of "
                      + "\(popped.widgets.count) widgets drawn")
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.5))
            let (tab, tabWhy) = picture(host, webs: webs)
            let (pop, popWhy) = picture(frame, webs: [other])
            guard let left = tab, let right = pop else { return fail(tabWhy ?? popWhy ?? "") }
            guard let both = side(left, right) else { return fail("cannot make the picture of both") }
            let code = withExtendedLifetime(windows) { write(both, to: out) }
            if code == 0 {
                print("wrote \(out) (\(list.threads.count) threads, thread \(mail.selected ?? "none") open in the tab, "
                      + "thread \(id) in its window, titled “\(window.title)”)")
            }
            return code
        }
    }

    /// What `view` draws, with what each of `webs` shows drawn over it: WebKit draws in its own process, so each is
    /// captured with takeSnapshot and drawn in its place (window coordinates, which are `view`'s: the window's content
    /// view, or the frame view round it). nil, and why, when one could not be captured.
    static func picture(_ view: NSView, webs: [WKWebView]) -> (NSBitmapImageRep?, String?) {
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return (nil, "cannot draw the view") }
        view.cacheDisplay(in: view.bounds, to: rep)
        final class Taken { var image: NSImage?; var error: Error?; var done = false }
        var over: [(NSImage, NSRect)] = []
        for web in webs {
            let taken = Taken()
            web.takeSnapshot(with: nil) { image, error in taken.image = image; taken.error = error; taken.done = true }
            guard wait(10, step: 0.05, until: { taken.done }), let image = taken.image else {
                return (nil, "the document could not be captured: \(taken.error?.localizedDescription ?? "no picture in 10 s")")
            }
            over.append((image, web.convert(web.bounds, to: nil)))
        }
        guard let context = NSGraphicsContext(bitmapImageRep: rep) else { return (nil, "cannot draw over the view") }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = context
        for (image, rect) in over { image.draw(in: rect) }
        NSGraphicsContext.restoreGraphicsState()
        return (rep, nil)
    }

    /// `left` and `right` side by side, tops aligned, on a grey ground with room round them; nil when no bitmap can be
    /// made for them.
    static func side(_ left: NSBitmapImageRep, _ right: NSBitmapImageRep) -> NSBitmapImageRep? {
        let room: CGFloat = 28
        let size = NSSize(width: left.size.width + right.size.width + 3 * room,
                          height: max(left.size.height, right.size.height) + 2 * room)
        let scale = CGFloat(left.pixelsWide) / left.size.width
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * scale),
                                         pixelsHigh: Int(size.height * scale), bitsPerSample: 8, samplesPerPixel: 4,
                                         hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0,
                                         bitsPerPixel: 0), let context = NSGraphicsContext(bitmapImageRep: rep) else { return nil }
        rep.size = size
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = context
        NSColor(white: 0.5, alpha: 1).setFill()
        NSRect(origin: .zero, size: size).fill()
        left.draw(in: NSRect(x: room, y: size.height - room - left.size.height, width: left.size.width, height: left.size.height))
        right.draw(in: NSRect(x: 2 * room + left.size.width, y: size.height - room - right.size.height,
                              width: right.size.width, height: right.size.height))
        NSGraphicsContext.restoreGraphicsState()
        return rep
    }

    /// What `source` evaluates to in `web`'s page, once its answer comes or `seconds` pass (nil without one).
    static func evaluate(_ source: String, in web: WKWebView, within seconds: Double) -> Any? {
        final class Got { var value: Any?; var done = false }
        let got = Got()
        web.evaluateJavaScript(source) { v, _ in got.value = v; got.done = true }
        _ = wait(seconds, step: 0.002) { got.done }
        return got.value
    }

    /// What the function body `source` returns, called with `arguments` in content world `world` of `web`'s page, once
    /// its answer comes or `seconds` pass (nil without one, or when it throws).
    static func evaluate(_ source: String, arguments: [String: Any], in web: WKWebView, world: WKContentWorld,
                         within seconds: Double) -> Any? {
        final class Got { var value: Any?; var done = false }
        let got = Got()
        web.callAsyncJavaScript(source, arguments: arguments, in: nil, in: world) { result in
            if case .success(let v) = result { got.value = v }
            got.done = true
        }
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
    static func find<T: NSView>(_ type: T.Type, in view: NSView) -> T? { all(type, in: view).first }

    /// Every view of type T in `view`'s tree, `view` included, depth first.
    static func all<T: NSView>(_ type: T.Type, in view: NSView) -> [T] {
        ((view as? T).map { [$0] } ?? []) + view.subviews.flatMap { all(type, in: $0) }
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

/// A stand-in desktop for the Mail tab in renders and self-tests: mail_list answered from a saved list, mail_show from
/// saved answers by thread, mail_mark by changing the thread's row as the tab shows a change (MailRules.changed), which
/// later lists then give (less the threads a mark has moved out of the box asked for), and mail_fast by giving the row
/// a fast mode of fastMinutes, its effort still to be lowered (as the mailroom answers), or none. Each answer comes on
/// the main queue's next turn, as the desktop's do over the stream; anything else is refused, in `name`'s words. Either
/// kind of saved answer may be the board action's or `agent-mail mailbox`'s own output, which has no "ok". It keeps
/// every request, in order.
@MainActor
final class MailStandIn {
    private(set) var asked: [[String: Any]] = []
    private let list: [String: Any]
    private let shows: [String: [String: Any]]
    private let name: String
    private var rows: [String: [String: Any]]       // each thread's row: the list's, else its SHOW's, as marked
    private var order: [String] = []                // the list's threads, in its order
    private var moved: Set<String> = []             // threads a mark has moved to another box

    init(list: [String: Any], shows: [String: [String: Any]], name: String) {
        self.list = list
        self.shows = shows
        self.name = name
        rows = shows.compactMapValues { $0["thread"] as? [String: Any] }
        for case let t as [String: Any] in list["threads"] as? [Any] ?? [] {
            if let id = t["id"] as? String { rows[id] = t; order.append(id) }
        }
    }

    /// The requests asked for `action`.
    func asked(_ action: String) -> [[String: Any]] { asked.filter { $0["action"] as? String == action } }

    /// How the model reaches it (MailModel.Call), which keeps it.
    var call: MailModel.Call {
        { request, done in
            MainActor.assumeIsolated {
                self.asked.append(request)
                let action = request["action"] as? String ?? ""
                let thread = request["thread"] as? String ?? (request["threads"] as? [String])?.first
                var reply: [String: Any]?
                switch action {
                case "mail_show": reply = thread.flatMap(self.show)
                case "mail_list": reply = self.listing(request)
                case "mail_mark": reply = self.mark(request)
                case "mail_fast": reply = self.fast(request)
                case "mail_researcher": reply = self.researcher(request)
                default: reply = nil
                }
                let why = reply == nil ? "\(self.name) has no answer for \(action) \(thread ?? "")" : nil
                DispatchQueue.main.async { MainActor.assumeIsolated { done(reply, why) } }
            }
        }
    }

    private func show(_ id: String) -> [String: Any]? {
        guard var answer = shows[id], var thread = answer["thread"] as? [String: Any], let row = rows[id] else {
            return nil
        }
        for field in ["box", "unread", "snooze_until", "fast", "researcher"] {
            thread[field] = row[field]
        }
        answer["thread"] = thread
        answer["ok"] = true
        return answer
    }

    private func mark(_ request: [String: Any]) -> [String: Any]? {
        guard let id = (request["threads"] as? [String])?.first, var row = rows[id],
              let op = (request["op"] as? String).flatMap(MailOp.init(rawValue:)),
              let data = try? JSONSerialization.data(withJSONObject: row),
              let t = try? JSON.decoder.decode(MailboxThread.self, from: data) else { return nil }
        let n = MailRules.changed(t, op, until: request["until"] as? Double)
        if n.box != t.box { moved.insert(id) }
        row["unread"] = n.unread
        row["box"] = n.box
        row["snooze_until"] = n.snoozeUntil ?? NSNull()
        rows[id] = row
        return ["ok": true, "threads": [row]]
    }

    /// The minutes a fast mode switched on here lasts: the mailroom's default (its mailbox.fast_minutes).
    static let fastMinutes = 5.0

    private func fast(_ request: [String: Any]) -> [String: Any]? {
        guard let id = request["thread"] as? String, var row = rows[id], let on = request["on"] as? Bool else { return nil }
        let now = Date().timeIntervalSince1970
        row["fast"] = on ? ["since": now, "until": now + 60 * MailStandIn.fastMinutes, "minutes": MailStandIn.fastMinutes,
                            "agent": "alpha", "effort": "lowering", "was": "xhigh",
                            "note": "Back to xhigh when fast mode ends."] as [String: Any] : NSNull()
        rows[id] = row
        return ["ok": true, "thread": row]
    }

    private func researcher(_ request: [String: Any]) -> [String: Any]? {
        guard let id = request["thread"] as? String, var row = rows[id], let on = request["on"] as? Bool,
              let tag = request["expected_tag"] as? String, var state = row["researcher"] as? [String: Any],
              state["tag"] as? String == tag else { return nil }
        state["on"] = on
        state["delivery"] = "pending"
        row["researcher"] = state
        rows[id] = row
        return ["ok": true, "thread": row]
    }

    private func listing(_ request: [String: Any]) -> [String: Any] {
        let box = request["box"] as? String
        var answer = list
        answer["ok"] = true
        answer["threads"] = order.compactMap { rows[$0] }.filter {
            box == MailBox.all.rawValue || !moved.contains($0["id"] as? String ?? "") || $0["box"] as? String == box
        }
        return answer
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
        box.submit = { submitted += 1 }
        func key(_ code: UInt16, _ flags: NSEvent.ModifierFlags) {
            if let e = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags, timestamp: 0, windowNumber: 0,
                                        context: nil, characters: "\r", charactersIgnoringModifiers: "\r", isARepeat: false, keyCode: code) {
                box.keyDown(with: e)
            }
        }
        key(36, [.command]); key(76, [.command]); key(36, [])
        print("⌘↩ submits: \(submitted == 2) · Return types a newline: \(box.string == "\n")")
        let fails = dialogFiles()
        fails.forEach { print("FAILED: dialog files: " + $0) }
        let ok = results.map { $0.1 } == [1.1, 1.25, 1.1, 1.0] && submitted == 2 && box.string == "\n" && fails.isEmpty
        print(ok ? "keys OK" : "keys FAILED")
        return ok ? 0 : 1
    }

    /// A dialog's files (Dialogs.filesRow, MessageFiles): an image or a file on a pasteboard is taken, text is left to
    /// be typed; each is named and sent as base64, within a reply's limits, a refusal said under the text in red; Remove
    /// shows only with files, and takes them all out. What failed, else [].
    static func dialogFiles() -> [String] {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append(what) } }
        let files = MessageFiles()
        let (scroll, text) = textArea(width: 300, height: 80)
        let (row, take) = Dialogs.filesRow(scroll, files, width: 300)
        // Paste (⌘V) is offered only when the clipboard holds a type the box reads: a screenshot copied to it is an
        // image alone, so a box that takes files reads images too; one that does not, only what a text box reads
        check(!textArea(width: 300, height: 80).1.readablePasteboardTypes.contains(.png), "a text-only box reads no image")
        text.take = take
        check([.png, .tiff].allSatisfy(text.readablePasteboardTypes.contains),
              "a box that takes files reads images, so Paste is offered for a screenshot: "
              + text.readablePasteboardTypes.map(\.rawValue).joined(separator: ", "))
        let label = row.subviews.compactMap { $0 as? NSTextField }.first
        let remove = row.subviews.compactMap { $0 as? NSButton }.first { $0.title == "Remove" }
        check(remove?.isHidden == true && label?.stringValue == files.summary,
              "no files: Remove hidden, the label says what to do: \(label?.stringValue ?? "no label")")
        let pb = NSPasteboard(name: NSPasteboard.Name("agentboard-keytest-\(UUID().uuidString)"))   // never the owner's clipboard
        defer { pb.releaseGlobally() }
        pb.clearContents()
        pb.setString("words", forType: .string)
        check(!take(pb) && files.files.isEmpty, "text on the pasteboard is left to be typed")
        pb.clearContents()
        pb.setData(Data([0x89, 0x50]), forType: .png)
        check(take(pb) && files.files.map(\.data) == [Data([0x89, 0x50])]
              && files.files.first?.name.hasPrefix("Pasted image ") == true, "an image is taken: \(files.files.map(\.name))")
        check(remove?.isHidden == false && label?.stringValue.hasPrefix("1 file, ") == true,
              "and named under the text: \(label?.stringValue ?? "")")
        check(files.request == [["name": files.files.first?.name ?? "", "data": "iVA="]], "sent as base64: \(files.request)")
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("agentboard-keytest-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }           // this test's own folder
        let csv = folder.appendingPathComponent("data.csv")
        try? Data("a,b\n".utf8).write(to: csv)
        check(files.add([.file(csv)], at: Date()) == nil && files.files.last?.name == "data.csv"
              && files.files.last?.data == Data("a,b\n".utf8), "a file is read, under its own name")
        check(files.add([.file(folder)], at: Date())?.contains("is not a file (a folder?)") == true && files.files.count == 2,
              "a folder is refused, and nothing added")
        check(files.add(Array(repeating: .image(Data([0])), count: MailModel.maxFiles - 1), at: Date())?
                .contains("at most \(MailModel.maxFiles) files") == true && files.files.count == 2, "too many files")
        check(files.add([.image(Data(count: MailModel.maxFileBytes))], at: Date())?.contains("a message carries at most")
              == true && files.files.count == 2, "too many bytes")
        files.removeAll()
        check(files.files.isEmpty && remove?.isHidden == true, "Remove takes them all out")
        pb.clearContents()
        pb.writeObjects([folder as NSURL])
        check(take(pb) && files.files.isEmpty && label?.textColor == .systemRed
              && label?.stringValue.contains("is not a file") == true, "a refusal is said under the text, in red: "
              + (label?.stringValue ?? ""))
        return fails
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
