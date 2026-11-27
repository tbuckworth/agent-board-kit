import AppKit
import Combine
import SwiftUI
import WebKit

// Threads popped out of the Mail tab into windows of their own: a double-click on a row of the list, or the row's
// right-click Open in new window. The model keeps what each window shows (MailModel.popouts); this file makes the windows
// to match, gives each its own web view, and its keys.

/// The windows of the threads popped out of the Mail tab, one per thread, following MailModel.popouts: a window opens
/// for each thread popped out, comes to the front when the thread is popped out again (MailModel.raise), and closes when
/// the model lets the thread go (Unread); closed by the owner, its thread goes from the model (MailModel.popIn). Each is an
/// ordinary window titled with the thread's subject: resizable, full screen with the green button or ⌃⌘F (the View
/// menu), closed with ⌘W, with a web view of its own (MailWeb.make) that goes when it closes. Its keys are the Mail
/// tab's, for its thread, less the list's (MailKeys.command with no list).
@MainActor
final class MailWindows: NSObject, NSWindowDelegate {
    /// A window's size before one has been closed (whose frame the next one takes), and its smallest.
    static let size = NSSize(width: 900, height: 760)
    static let minSize = NSSize(width: 520, height: 360)
    /// The name the last closed window's frame is saved under, in the app's defaults.
    static let frameName = "AgentBoardMailThread"

    let mail: MailModel
    /// The windows open, by thread.
    private(set) var windows: [String: NSWindow] = [:]
    /// Told when a window has closed: the app goes back to the menu bar once no window of its is open.
    var closed: () -> Void = {}
    private let zoom: AnyPublisher<CGFloat, Never>
    private let onScreen: Bool
    private var bag = Set<AnyCancellable>()
    private var keys: Any?

    /// `zoom`: the board's zoom, which the windows follow. `onScreen`: the app's windows, which open where the last one
    /// was (cascaded from one still open, else at the last closed one's frame, else centred) and come to the front with
    /// the app (bringForward); else (self-tests and renders) far off every screen, ordered in without the app, and no
    /// frame saved.
    init(mail: MailModel, zoom: AnyPublisher<CGFloat, Never>, onScreen: Bool) {
        self.mail = mail
        self.zoom = zoom
        self.onScreen = onScreen
        super.init()
        // on the main queue's next turn, after the change: the model is not changed while it publishes
        mail.$popouts.receive(on: DispatchQueue.main).sink { [weak self] in self?.follow($0) }.store(in: &bag)
        mail.raise.receive(on: DispatchQueue.main).sink { [weak self] id in
            if let w = self?.windows[id] { self?.show(w) }
        }.store(in: &bag)
        keys = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in self?.key(e) ?? e }
    }

    deinit {
        if let k = keys { NSEvent.removeMonitor(k) }
    }

    /// The thread whose window `w` is.
    func thread(of w: NSWindow) -> String? { windows.first { $0.value === w }?.key }

    /// A window for each thread popped out, titled with its subject, and none for the rest.
    private func follow(_ popouts: [String: MailReading]) {
        for (id, w) in windows where popouts[id] == nil { w.close() }
        for id in popouts.keys.sorted() {
            let title = MailWindows.title(popouts[id]?.shown?.thread ?? mail.current(id))
            if let w = windows[id] {
                if w.title != title { w.title = title }
            } else {
                open(id, title: title)
            }
        }
    }

    /// A thread's window's title: its subject, as the list shows it (MailRow.texts); "Mail" until the thread is known.
    static func title(_ t: MailboxThread?) -> String { t.map { MailRow.texts($0).subject } ?? "Mail" }

    private func open(_ id: String, title: String) {
        let w = NSWindow(contentRect: NSRect(origin: .zero, size: MailWindows.size),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        w.isReleasedWhenClosed = false
        w.collectionBehavior.insert(.fullScreenPrimary)
        w.tabbingMode = .disallowed               // its own window, even where macOS is set to prefer tabs
        w.title = title
        w.contentMinSize = MailWindows.minSize
        w.contentView = NSHostingView(rootView: MailWindowView(mail: mail, id: id, web: MailWeb.make(),
                                                               host: { [weak w] in w }, zoom: zoom))
        w.delegate = self
        place(w)
        windows[id] = w
        show(w)
    }

    private func place(_ w: NSWindow) {
        guard onScreen else { return w.setFrameOrigin(NSPoint(x: -20000, y: -20000)) }
        if let last = windows.values.min(by: { $0.orderedIndex < $1.orderedIndex }), !last.styleMask.contains(.fullScreen) {
            var frame = last.frame
            frame.origin.x += 24
            frame.origin.y -= 24
            w.setFrame(w.constrainFrameRect(frame, to: last.screen), display: false)
        } else if !w.setFrameUsingName(MailWindows.frameName) {
            w.center()
        }
    }

    private func show(_ w: NSWindow) {
        if w.isMiniaturized { w.deminiaturize(nil) }
        if onScreen { bringForward(w) } else { w.orderFront(nil) }
    }

    func windowWillClose(_ n: Notification) {
        guard let w = n.object as? NSWindow, let id = thread(of: w) else { return }
        windows[id] = nil
        if onScreen && !w.styleMask.contains(.fullScreen) { w.saveFrame(usingName: MailWindows.frameName) }
        w.contentView = nil                       // its hosting view goes, and with it the web view
        mail.popIn(id)
        closed()
    }

    /// A key in a thread's window: the Mail tab's keys, less the list's, for its thread; nil when it was one of them.
    private func key(_ e: NSEvent) -> NSEvent? {
        guard let w = e.window, w.attachedSheet == nil, let id = thread(of: w),
              let key = MailKeys.command(e.charactersIgnoringModifiers ?? "", keyCode: e.keyCode, flags: e.modifierFlags,
                                         typing: w.firstResponder is NSText, list: false) else { return e }
        if key == .release { w.makeFirstResponder(nil) } else { mail.perform(key, in: id) }
        return nil
    }
}

/// A popped-out thread's window: the thread's buttons (ThreadActions), where it is when out of the inbox, the Undo bar,
/// then the thread as the tab shows it (MailThreadView), in the window's own web view, at the board's zoom.
struct MailWindowView: View {
    @ObservedObject var mail: MailModel
    let id: String
    let web: MailWebView
    let host: () -> NSWindow?
    let zoom: AnyPublisher<CGFloat, Never>
    @State private var z: CGFloat = 1

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10 * z) {
                ThreadActions(mail: mail, window: id)
                if let line = MailWindowView.place(mail.current(id), now: Date()) {
                    Text(line).font(.system(size: 11.5 * z, weight: .medium)).foregroundStyle(.secondary).lineLimit(1)
                }
                UndoBar(mail: mail)
            }
            .padding(.horizontal, 12 * z).padding(.vertical, 8 * z)
            Divider()
            if let r = mail.popouts[id] {
                MailThreadView(mail: mail, reading: r, window: id, web: web, host: host)
            } else {
                Spacer()                          // let go by the model: the window is closing
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .windowBackgroundColor))
        .environment(\.zoom, z)
        .onReceive(zoom) { z = $0 }
    }

    /// Where thread `t` is when out of the inbox, in the Undo bar's words: "Archived", "Snoozed until Sat 08:00".
    static func place(_ t: MailboxThread?, now: Date) -> String? {
        switch t.flatMap({ MailBox(rawValue: $0.box) }) {
        case .archived?: return MailOp.archive.done(until: nil, now: now)
        case .snoozed?: return MailOp.snooze.done(until: t?.snoozeUntil, now: now)
        case .inbox?, .all?, nil: return nil
        }
    }
}

// MARK: - --selftest-popout

/// --selftest-popout: the Mail tab and threads popped out of it, offscreen, against a stand-in desktop (MailStandIn,
/// which answers as the views ask, as --render-mail's does): the real board, list, windows and web views, clicked and
/// typed into as the mouse and keyboard do. A click on a row opens the thread in the tab and a double-click pops it out
/// into one window, titled with its subject, with a web view of its own, the thread marked read once, after its second
/// on screen; another thread has a window of its own, and a double-click on a thread popped out already brings its
/// window forward. The window and the tab share the thread's draft: text typed in either box is in the other, and an
/// option picked or text commented on in the window shows in the tab's document. The window's keys: r replies there,
/// Esc leaves the box, Space pages, ↓ is not the list's, e archives (the window stays, saying so, and the tab, which
/// had it open, has none open) and z undoes; u (Unread) closes the window and frees it, and Undo opens it again; s pops
/// up the Snooze menu at that window's Snooze button (kept off the screen here), whose items snooze it. A window the owner
/// closes is freed too; a window can go full screen.
@MainActor
enum PopoutTest {
    static func run() -> Int32 {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append(what) } }
        /// The checks failed so far and why the test cannot go on.
        func stop(_ why: String) -> Int32 {
            (fails + [why]).forEach { print("FAIL: " + $0) }
            return 1
        }
        guard case .success = MailWidgets.script, let rawWidgets = try? MailWidgets.json(WidgetTest.twoWidgets),
              let widgets = try? JSONSerialization.jsonObject(with: Data(rawWidgets.utf8)) else {
            return stop("widgets.js or the stand-in widgets not loaded")
        }
        var t = MailTest.thread("T", unread: true, newest: 21, last: 210, subject: "Loader cache: two choices before I merge")
        t["researcher"] = ["on": false, "agent": "alpha", "tag": "alpha001"]
        let u = MailTest.thread("U", newest: 30, last: 300, subject: "E7 sweep finished")
        let message = MailTest.Desk.message("s21", unread: true).merging(["widgets": widgets]) { $1 }
        let sweep = "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body><details class=\"msg\" id=\"s30\" open>"
            + "<summary>alpha</summary><div class=\"card\"><div class=\"mail\"><p>All six runs are done.</p></div></div>"
            + "</details></body></html>"
        let desktop = MailStandIn(
            list: ["threads": [u, t], "counts": ["inbox": 2, "unread": 1, "snoozed": 0]],
            shows: ["T": MailTest.Desk.show(t, [message], document: WidgetTest.twoQuestions),
                    "U": MailTest.Desk.show(u, [MailTest.Desk.message("s30", ts: 2)], document: sweep)],
            name: "--selftest-popout")
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        let mail = MailModel(call: desktop.call, upload: { _, done in
            MainActor.assumeIsolated { done(nil, "--selftest-popout copies nothing to the desktop") }
        }, ahead: 0, cacheBytes: 1 << 20, draftsFolder: folder, undoSeconds: 60)
        let board = BoardModel(layout: Layout(), persist: false, mail: mail)
        guard let snap = MailTest.snapshot([], check) else { return stop("the stand-in snapshot did not decode") }
        board.apply(snap)
        board.link = .live
        board.tab = .mail
        board.zoom = 1
        let (tab, host) = Renderer.offscreen(BoardView(model: board).frame(width: 1180, height: 760),
                                             size: CGSize(width: 1180, height: 760), dark: false)
        let windows = MailWindows(mail: mail, zoom: board.$zoom.eraseToAnyPublisher(), onScreen: false)
        return withExtendedLifetime((tab, windows)) {
            guard Renderer.wait(5, step: 0.05, until: { mail.threads?.count == 2 }),
                  Renderer.wait(15, step: 0.05, until: { MailWeb.loader.ready }) else {
                return stop("the tab did not list the threads and start its web view: \(mail.listError ?? "")")
            }
            let js = { (source: String, web: WKWebView) in Renderer.evaluate(source, in: web, within: 5) }
            /// The marks asked for thread `id`, as "op upto".
            let marks = { (id: String) in
                desktop.asked("mail_mark").filter { ($0["threads"] as? [String]) == [id] }
                    .map { "\($0["op"] ?? "") \($0["upto"] ?? "-")" }
            }
            /// Window `id`'s web view, once its document is in the page.
            let shown = { (id: String, key: String) -> MailWebView? in
                var web: MailWebView?
                guard Renderer.wait(15, step: 0.01, until: {
                    web = windows.windows[id]?.contentView.flatMap { Renderer.find(MailWebView.self, in: $0) }
                    return web != nil && mail.popouts[id]?.shown != nil
                }), let w = web, Renderer.inPage(w, id: key, within: 15) else { return nil }
                return w
            }

            // a click on T's row opens it in the tab; a double-click pops it out, into a window of its own
            guard let spot = rowBelow("U", mail: mail, host: host) else {
                return stop("the rows were not found in the list")
            }
            click(spot, in: tab, count: 1)
            check(Renderer.wait(3, step: 0.01, until: { mail.selected == "T" }), "a click opens T in the tab: \(mail.selected ?? "none")")
            click(spot, in: tab, count: 2)
            weak var freedWindow: NSWindow?
            weak var freedWeb: MailWebView?
            // each part that reaches into a window drains its own autorelease pool, as the app's run loop does each
            // turn: AppKit's arrays of subviews and its events, autoreleased, hold the window and its web view
            let stopped = autoreleasepool { () -> Int32? in
                guard let tWeb = shown("T", "s21"), let tWin = windows.windows["T"] else {
                    return stop("T was not shown in a window of its own: \(mail.popouts["T"]?.error ?? "none")")
                }
                check(windows.windows.count == 1 && mail.popouts.keys.sorted() == ["T"], "a double-click: one window, T's")
                check(tWin.title == Fmt.leftToRight("Loader cache: two choices before I merge"),
                      "titled with the subject, as the list shows it: \(tWin.title)")
                check(tWeb !== MailWeb.view, "with a web view of its own")
                check(Renderer.widgetsDrawn(tWeb, want: 2, within: 10) == 2, "its options drawn")
                check(Renderer.wait(3, step: 0.01, until: { marks("T") == ["read 21"] }),
                      "T marked read once, after its second on screen, up to its newest: \(marks("T"))")

                // U, popped out from its right-click menu (not opened in the tab): a second window; then a double-click
                // on T's row brings T's window forward, and opens no other
                mail.popOut("U")
                guard shown("U", "s30") != nil, let uWin = windows.windows["U"] else {
                    return stop("U was not shown in a window of its own")
                }
                check(mail.selected == "T" && windows.windows.count == 2, "U in a window of its own, the tab's T still open")
                check(uWin.orderedIndex < tWin.orderedIndex, "U's window in front of T's")
                click(spot, in: tab, count: 1)
                click(spot, in: tab, count: 2)
                check(Renderer.wait(3, step: 0.01, until: { tWin.orderedIndex < uWin.orderedIndex }),
                      "a double-click on T again: its window comes to the front")
                check(windows.windows.count == 2 && windows.windows["T"] === tWin, "and no other window opens for it")

                // the Fast switch atop T in its window (MailModeBar, in MailThreadView as in the tab): one state for the
                // list, the tab's reading of T and its window's, on and off again
                mail.setFast("T", on: true)
                check(Renderer.wait(3, step: 0.01, until: { mail.fastAsked == nil })
                      && mail.current("T")?.fast?.effort == "lowering" && mail.shown?.thread.fast?.effort == "lowering"
                      && mail.popouts["T"]?.shown?.thread.fast?.effort == "lowering",
                      "fast mode on from T's window: in the list, the tab and the window alike")
                check(MailModeBar.texts(mail.current("T")?.fast, now: mail.now, switching: false).label == "Fast · 5 min",
                      "the switch in both says its minutes left")
                mail.setFast("T", on: false)
                check(Renderer.wait(3, step: 0.01, until: { mail.fastAsked == nil }) && mail.current("T")?.fast == nil
                      && mail.shown?.thread.fast == nil && mail.popouts["T"]?.shown?.thread.fast == nil,
                      "and off again in all three")
                check(desktop.asked("mail_fast").map { "\($0["thread"] ?? "") \($0["on"] ?? "")" } == ["T true", "T false"],
                      "mail_fast asked once each way: \(desktop.asked("mail_fast"))")

                // Researcher is the rightmost capsule, immediately below the popout's thread actions.
                let layout = tWin.contentLayoutRect
                click(NSPoint(x: layout.maxX - 48, y: layout.maxY - 47), in: tWin, count: 1)
                check(Renderer.wait(3, step: 0.01, until: { mail.current("T")?.researcher?.on == true })
                      && mail.shown?.thread.researcher?.on == true && mail.popouts["T"]?.shown?.thread.researcher?.on == true,
                      "pressing Researcher in the popout switches it on in both readers")
                let switches = desktop.asked("mail_researcher")
                check(switches.count == 1 && switches.first?["expected_tag"] as? String == "alpha001"
                      && mail.current("T")?.fast == nil, "the popout click binds its routing tag and preserves Fast")

                // r in the window: the shared draft's box, in the window and in the tab (T is open in both)
                press("r", 15, in: tWin)
                let draft = mail.draft(for: "T")
                var boxes: (FileTakingTextView, FileTakingTextView)?
                _ = Renderer.wait(5, step: 0.01) {
                    if let a = tWin.contentView.flatMap({ Renderer.find(FileTakingTextView.self, in: $0) }),
                       let b = Renderer.find(FileTakingTextView.self, in: host) { boxes = (a, b) }
                    return boxes != nil
                }
                guard let pair = boxes else {
                    return stop("r in the window did not show the reply box in both: \(draft.shown)")
                }
                let (inWindow, inTab) = pair
                check(draft.shown && Renderer.wait(3, step: 0.01, until: { !draft.wantsKeyboard }),
                      "r in the window: the reply box, in both, and the keyboard asked for is taken")
                // offscreen no window is key, so either box may take it; in the app the window r was pressed in is key
                check(ComposeText.takesKeyboard(tWin, key: tWin) && !ComposeText.takesKeyboard(tab, key: tWin)
                      && ComposeText.takesKeyboard(tab, key: nil), "only the key window's box takes the keyboard, any with none key")
                inWindow.insertText("Merge it", replacementRange: NSRange(location: 0, length: 0))
                check(Renderer.wait(3, step: 0.01, until: { inTab.string == "Merge it" }) && draft.text == "Merge it",
                      "typed in the window, it is in the tab's box: \(inTab.string.debugDescription)")
                inTab.insertText(" today", replacementRange: NSRange(location: (inTab.string as NSString).length, length: 0))
                check(Renderer.wait(3, step: 0.01, until: { inWindow.string == "Merge it today" }),
                      "typed in the tab, it is in the window's box: \(inWindow.string.debugDescription)")

                // an option picked in the window is picked in the tab's document too
                _ = js("document.querySelectorAll('[data-widget=\"s21-0\"] .abw-opt')[1].click()", tWeb)
                check(Renderer.wait(3, step: 0.01, until: { draft.picks.map(\.widget) == ["s21-0"] }), "picked: \(draft.picks)")
                check(Renderer.wait(5, step: 0.02, until: {
                    js("document.querySelectorAll('[data-widget=\"s21-0\"] .abw-on').length", MailWeb.view) as? Int == 1
                }), "the option picked in the window shows picked in the tab")

                // text commented on in the window is highlighted in the tab's document
                _ = js(CommentTest.select("s21", from: "Two things", to: "settle:"), tWeb)
                _ = Renderer.wait(3, step: 0.01) {
                    js("getComputedStyle(document.querySelector('button[data-abk-add]')).display", tWeb) as? String == "block"
                }
                _ = js("document.querySelector('button[data-abk-add]').click()", tWeb)
                check(Renderer.wait(3, step: 0.01, until: { draft.comments.count == 1 }), "commented: \(draft.comments)")
                let id = draft.comments.first?.id ?? ""
                check(Renderer.wait(5, step: 0.02, until: {
                    js("Array.prototype.map.call(document.querySelectorAll('mark[data-abk-comment=\"\(id)\"]'), "
                       + "function (m) { return m.textContent; }).join('')", MailWeb.view) as? String == "Two things to settle:"
                }), "the text commented on in the window is highlighted in the tab")

                // the window's keys: in the box they type (e archives nothing) and Esc leaves it; Space pages, ↓ is not
                // the list's, e archives and z undoes
                tWin.makeFirstResponder(inWindow)
                press("e", 14, in: tWin)
                check(marks("T") == ["read 21"], "e in the reply box does not archive: \(marks("T"))")
                press("\u{1b}", 53, in: tWin)
                check(!(tWin.firstResponder is NSText), "Esc: the keyboard leaves the box")
                press(" ", 49, in: tWin)
                check(mail.popouts["T"]?.page == MailPage(n: 1, by: 1) && mail.page == nil, "Space pages the window, not the tab")
                press(MailKeys.down, 125, in: tWin, flags: [.numericPad, .function])
                check(mail.selected == "T", "↓ in the window does not step the tab's list")
                press("e", 14, in: tWin)
                check(Renderer.wait(3, step: 0.01, until: { !mail.undos.isEmpty }) && marks("T").last == "archive 21",
                      "e archives T: \(marks("T"))")
                check(windows.windows["T"] === tWin && mail.current("T")?.box == MailBox.archived.rawValue
                      && MailWindowView.place(mail.current("T"), now: Date()) == "Archived",
                      "its window stays open, saying it is archived")
                check(mail.tab == nil, "the tab, which had T open, has none open, as after any archive: \(mail.selected ?? "none")")
                press("z", 6, in: tWin)
                check(Renderer.wait(3, step: 0.01, until: { marks("T").last == "inbox -" })
                      && mail.current("T")?.box == MailBox.inbox.rawValue && mail.selected == "T",
                      "z in the window: back in the inbox, and open in the tab again: \(marks("T"))")

                // the window can go full screen: the green button and ⌃⌘F (toggleFullScreen) work on such a window
                check(tWin.styleMask.isSuperset(of: [.titled, .closable, .miniaturizable, .resizable])
                      && tWin.collectionBehavior.contains(.fullScreenPrimary) && tWin.tabbingMode == .disallowed,
                      "an ordinary window that can go full screen: \(tWin.styleMask) \(tWin.collectionBehavior)")
                _ = Renderer.wait(3, step: 0.01) { mail.undoWaiting == 0 }

                // u, as Unread, closes it (in the tab too)
                freedWindow = tWin
                freedWeb = tWeb
                press("u", 32, in: tWin)
                check(Renderer.wait(3, step: 0.01, until: { windows.windows["T"] == nil }) && mail.popouts["T"] == nil
                      && mail.selected == nil && marks("T").last == "unread -",
                      "u in the window marks T unread and closes the window, and the tab's thread: \(marks("T"))")
                return nil
            }
            if let code = stopped { return code }
            check(freed { freedWindow == nil }, "and the window is freed")
            check(freed { freedWeb == nil }, "and its web view")
            check(Renderer.wait(3, step: 0.01, until: { !mail.undos.isEmpty && mail.undoWaiting == 0 }), "Unread offered to Undo")
            mail.undo()
            check(shown("T", "s21") != nil && marks("T").last == "read 21", "Undo opens it again in a window, read: \(marks("T"))")

            // s in U's window: the Snooze menu, its items the Snooze button's, pops up at that window's Snooze button
            // (mail.popUp, kept off the screen here); its first item snoozes U, and the window stays, saying so
            var popped: [(menu: NSMenu, at: NSView)] = []
            mail.popUp = { popped.append(($0, $1)) }
            if let uWin = windows.windows["U"], let bar = uWin.contentView {
                press("s", 1, in: uWin)
                check(Renderer.wait(3, step: 0.01, until: { !popped.isEmpty }) && popped.count == 1,
                      "s in U's window: one menu: \(popped.count)")
                if let first = popped.first {
                    let (menu, view) = (first.menu, first.at)
                    let place = view.convert(view.bounds, to: nil)
                    check(view.window === uWin && place.width > 30 && place.height > 10
                          && place.minY > bar.bounds.height - 60, "at the Snooze button atop U's window: \(place)")
                    let titles = menu.items.filter { !$0.isSeparatorItem }.map(\.title)
                    check(titles == Snooze.items(now: Date()).map(\.title), "the Snooze button's items: \(titles)")
                    menu.performActionForItem(at: 0)
                    check(Renderer.wait(3, step: 0.01, until: { marks("U").last == "snooze 30" })
                          && windows.windows["U"] === uWin && mail.current("U")?.box == MailBox.snoozed.rawValue,
                          "its first item snoozes U; the window stays: \(marks("U"))")
                }
            } else {
                check(false, "U's window was not open for s")
            }

            // closed by the owner: the thread is let go, and its window and web view freed
            autoreleasepool {
                let uWin = windows.windows["U"]
                freedWindow = uWin
                freedWeb = uWin?.contentView.flatMap { Renderer.find(MailWebView.self, in: $0) }
                uWin?.performClose(nil)
            }
            check(mail.popouts["U"] == nil && windows.windows["U"] == nil, "closed: the model lets U go")
            check(freed { freedWindow == nil }, "U's window is freed")
            check(freed { freedWeb == nil }, "and its web view")
            windows.windows["T"]?.close()
            check(Renderer.wait(3, step: 0.01, until: { mail.popouts.isEmpty }), "every window closed")
            if fails.isEmpty { print("popout selftest: ok"); return 0 }
            fails.forEach { print("FAIL: " + $0) }
            return 1
        }
    }

    /// Whether `gone` comes true within 5 s, autorelease pools drained as the run loop turns.
    static func freed(_ gone: () -> Bool) -> Bool {
        for _ in 0..<100 {
            autoreleasepool { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
            if gone() { return true }
        }
        return false
    }

    /// Where the row below thread `above`'s is in the tab's list, in `host`'s window's coordinates: `above`'s row is
    /// the tallest band of the list column that changes when it is open (its ground takes the open thread's colour; the
    /// buttons above the list change too, and lines here and there), and the next row, of the same lines, is its height
    /// further down. It opens `above` and closes it again; nil when nothing changes.
    static func rowBelow(_ above: String, mail: MailModel, host: NSView) -> NSPoint? {
        func picture() -> NSBitmapImageRep? {
            _ = Renderer.wait(0.5, step: 0.05) { false }
            return Renderer.picture(host, webs: []).0
        }
        guard let closed = picture() else { return nil }
        mail.open(above)
        _ = Renderer.wait(3, step: 0.01) { mail.shown?.thread.id == above }
        guard let open = picture() else { return nil }
        mail.close()
        guard open.bytesPerRow == closed.bytesPerRow, open.pixelsHigh == closed.pixelsHigh,
              let a = open.bitmapData, let b = closed.bitmapData else { return nil }
        let scale = CGFloat(open.pixelsWide) / host.bounds.width
        let from = Int(12 * scale) * open.bitsPerPixel / 8, width = Int((MailListColumn.width - 24) * scale) * open.bitsPerPixel / 8
        let changed = (0..<open.pixelsHigh).filter { y in
            memcmp(a + y * open.bytesPerRow + from, b + y * open.bytesPerRow + from, width) != 0
        }
        // the runs of rows of pixels that changed: the tallest is the open row's ground
        var runs: [ClosedRange<Int>] = []
        for y in changed {
            if let last = runs.last, last.upperBound == y - 1 { runs[runs.count - 1] = last.lowerBound...y } else { runs.append(y...y) }
        }
        guard let band = runs.max(by: { $0.count < $1.count }) else { return nil }
        let height = CGFloat(band.count) / scale
        let middle = CGFloat(band.lowerBound + band.upperBound) / 2 / scale + height
        return host.convert(NSPoint(x: MailListColumn.width / 2, y: middle), to: nil)
    }

    /// A click of `count` (2: the second of a double-click) at `p` in `window`, as the mouse makes it: through the
    /// app's event queue, so each is the app's current event as it is handled.
    static func click(_ p: NSPoint, in window: NSWindow, count: Int) {
        autoreleasepool { clicked(p, in: window, count: count) }
    }

    private static func clicked(_ p: NSPoint, in window: NSWindow, count: Int) {
        for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
            if let e = NSEvent.mouseEvent(with: type, location: p, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                          windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: count,
                                          pressure: type == .leftMouseDown ? 1 : 0) {
                NSApp.postEvent(e, atStart: false)
            }
        }
        let until = Date().addingTimeInterval(0.1)
        while let e = NSApp.nextEvent(matching: .any, until: until, inMode: .default, dequeue: true) { NSApp.sendEvent(e) }
    }

    /// A key press in `window`, as the keyboard makes it.
    static func press(_ chars: String, _ code: UInt16, in window: NSWindow, flags: NSEvent.ModifierFlags = []) {
        autoreleasepool { pressed(chars, code, in: window, flags: flags) }
    }

    private static func pressed(_ chars: String, _ code: UInt16, in window: NSWindow, flags: NSEvent.ModifierFlags) {
        if let e = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags, timestamp: ProcessInfo.processInfo.systemUptime,
                                    windowNumber: window.windowNumber, context: nil, characters: chars,
                                    charactersIgnoringModifiers: chars, isARepeat: false, keyCode: code) {
            NSApp.sendEvent(e)
        }
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    }
}
