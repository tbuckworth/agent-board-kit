import AppKit
import Carbon
import Combine
import SwiftUI

/// Menu-bar app: a status item whose title counts the agents that need you, a window toggled by
/// clicking it or by the global hotkey (⌥⌘A, Carbon RegisterEventHotKey: no accessibility permission), and, once
/// turned on (Settings.askBox, the menu's Ask box item), the ask box (⌃⌥A, AskBox.swift).
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let model = BoardModel.saved()
    var window: NSWindow!
    /// The Mail tab's threads popped out into windows of their own.
    var mailWindows: MailWindows!
    var statusItem: NSStatusItem!
    var hotKey: HotKey?
    var askKey: HotKey?
    let ask = AskDesk()
    var meetings: MeetingAlerts?
    var security: SecurityAlerts?
    var bag = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1060, height: 680),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = BoardTitle.base
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.contentView = NSHostingView(rootView: BoardView(model: model).padding(.top, 18))
        window.setFrameAutosaveName("AgentBoardWindow")
        if !window.setFrameUsingName("AgentBoardWindow") { window.center() }
        window.delegate = self
        Dialogs.window = window
        mailWindows = MailWindows(mail: model.mail, zoom: model.$zoom.eraseToAnyPublisher(), onScreen: true)
        mailWindows.closed = { [weak self] in DispatchQueue.main.async { self?.backToMenuBar() } }
        model.windowVisible = false             // until it is shown; then its occlusion state says
        NotificationCenter.default.addObserver(forName: NSWindow.didChangeOcclusionStateNotification, object: window,
                                               queue: .main) { [weak self] _ in
            Task { @MainActor in
                guard let self = self else { return }
                let visible = self.window.occlusionState.contains(.visible)
                if self.model.windowVisible != visible { self.model.windowVisible = visible }
            }
        }

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = statusItem.button {
            b.image = NSImage(systemSymbolName: "square.grid.2x2.fill", accessibilityDescription: "Agent Board")
            b.image?.isTemplate = true
            b.imagePosition = .imageLeading
            b.target = self
            b.action = #selector(statusClicked(_:))
            b.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        model.$snapshot.combineLatest(model.$link)
            .receive(on: RunLoop.main)
            .sink { [weak self] _, _ in self?.updateStatusTitle() }
            .store(in: &bag)

        hotKey = HotKey(keyCode: UInt32(kVK_ANSI_A), modifiers: UInt32(cmdKey | optionKey)) { [weak self] in
            Task { @MainActor in self?.toggleWindow() }
        }
        setAskKey()
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.model.reconnect() }
        }
        installKeyMonitor(window: window, model: model)
        buildMainMenu()
        let alerts = Alerts()
        alerts.onOpen = { [weak self] id in
            guard let self = self else { return }
            self.showWindow()
            if let id = id {
                self.model.tab = .board
                self.model.selectedID = id
            }
        }
        alerts.onLink = { [weak self] link in self?.model.openMeeting(link) }
        alerts.onThread = { [weak self] thread in
            guard let self = self else { return }
            self.showWindow()
            self.model.openSecurityAlert(thread)
        }
        model.alerts = alerts
        alerts.start()
        let security = SecurityAlerts()
        model.$snapshot.compactMap { $0?.securityAlerts }.removeDuplicates()   // a snapshot without them changes nothing
            .sink { security.take($0) }
            .store(in: &bag)
        self.security = security
        let overlay = MeetingOverlay(open: { [weak self] link in self?.model.openMeeting(link) })
        let meetings = MeetingAlerts(post: { Alerts.notify([$0]) }, show: { overlay.show($0) })
        model.$snapshot.compactMap { $0?.meetings }.removeDuplicates()       // a snapshot without them keeps the last
            .sink { meetings.take($0, now: Date()) }
            .store(in: &bag)
        meetings.start()
        self.meetings = meetings
        // the window's title (hidden: the bar is transparent) names what it shows, for the desktop's voice desk, which
        // reads it from ActivityWatch (agentboard/voicedesk.py, BOARD_SESSION)
        model.objectWillChange.map { _ in () }.merge(with: model.mail.objectWillChange.map { _ in () })
            .receive(on: RunLoop.main)
            .compactMap { [weak self] _ in self.map { BoardTitle.make($0.model) } }
            .removeDuplicates()
            .sink { [weak self] title in if self?.window.title != title { self?.window.title = title } }
            .store(in: &bag)
        model.connect()
        ScreenTime.start { [weak self] problem in Task { @MainActor in self?.model.screenError = problem } }
        if !CommandLine.arguments.contains("--hidden") { showWindow() }   // the login item starts in the menu bar only
        watchForNewBuild()
    }

    /// mac/install.sh replaces /Applications/AgentBoard.app under the running app; offer to relaunch into the new one.
    func watchForNewBuild() {
        let exe = Bundle.main.executablePath ?? ""
        let mtime = { (try? FileManager.default.attributesOfItem(atPath: exe)[.modificationDate] as? Date) ?? nil }
        let started = mtime()
        let check = Timer(timeInterval: 20, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self = self, let now = mtime(), let then = started, now > then else { return }
                if !self.model.updateAvailable { self.model.updateAvailable = true }
            }
        }
        check.tolerance = 5
        RunLoop.main.add(check, forMode: .default)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return true
    }

    @objc func statusClicked(_ sender: NSStatusBarButton) {
        if NSApp.currentEvent?.type == .rightMouseUp {
            let menu = NSMenu()
            menu.addItem(withTitle: "Open Board   ⌥⌘A", action: #selector(openFromMenu), keyEquivalent: "").target = self
            menu.addItem(withTitle: "Message orchestrator…", action: #selector(messageOrchestrator), keyEquivalent: "").target = self
            let toCheck = model.snapshot?.sent?.toCheck ?? 0
            menu.addItem(withTitle: toCheck > 0 ? "Sent messages (\(toCheck) to check)…" : "Sent messages…",
                         action: #selector(showSent), keyEquivalent: "").target = self
            menu.addItem(withTitle: "Time…", action: #selector(showTime), keyEquivalent: "").target = self
            menu.addItem(withTitle: "Phone…", action: #selector(showPhone), keyEquivalent: "").target = self
            let askItem = menu.addItem(withTitle: "Ask box   ⌃⌥A", action: #selector(toggleAskBox), keyEquivalent: "")
            askItem.target = self
            askItem.state = Settings.askBox ? .on : .off
            askItem.toolTip = "Ask the desk or one agent by typing or dictating, and hear the answer (needs the desk "
                + "and the voice turned on on the desktop; README, \"The ask box on the Mac\")."
            if model.snapshot?.chats != nil {
                menu.addItem(withTitle: "Group chats…", action: #selector(showChats), keyEquivalent: "").target = self
            }
            if let box = model.snapshot?.mailbox {
                menu.addItem(withTitle: box.unread > 0 ? "Mail (\(box.unread) unread)" : "Mail",
                             action: #selector(showMail), keyEquivalent: "").target = self
            }
            menu.addItem(withTitle: "Refresh   ⌘R", action: #selector(refreshAll), keyEquivalent: "").target = self
            menu.addItem(withTitle: "Refresh usage", action: #selector(refreshUsage), keyEquivalent: "").target = self
            menu.addItem(withTitle: "Reconnect", action: #selector(reconnect), keyEquivalent: "").target = self
            menu.addItem(withTitle: "Desktop host (\(Settings.host))…", action: #selector(editHost), keyEquivalent: "").target = self
            if model.updateAvailable {
                menu.addItem(withTitle: "Relaunch into the new build", action: #selector(relaunch), keyEquivalent: "").target = self
            }
            menu.addItem(.separator())
            menu.addItem(withTitle: "Quit Agent Board", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
            statusItem.menu = menu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
        } else {
            toggleWindow()
        }
    }

    @objc func openFromMenu() { showWindow() }
    @objc func refreshUsage() { model.refreshUsage() }
    @objc func refreshAll() { model.refreshAll() }
    @objc func relaunch() { model.relaunch() }
    @objc func messageOrchestrator() { showWindow(); Dialogs.orchestrator(model: model) }
    @objc func showSent() { showWindow(); model.sheet = .sent }
    @objc func showTime() { showWindow(); model.sheet = .time }
    @objc func showChats() { showWindow(); model.sheet = .chats(nil) }
    @objc func showMail() { showWindow(); model.tab = .mail }
    @objc func showPhone() { showWindow(); model.sheet = .phone }
    @objc func toggleAskBox() {
        Settings.askBox.toggle()
        setAskKey()
        if !Settings.askBox { ask.close() }
    }
    @objc func reconnect() { model.reconnect() }
    @objc func editHost() { showWindow(); Dialogs.host(model: model) }

    /// The ask box's hot key, ⌃⌥A, held while the ask box is on (Settings.askBox) and let go while it is off, so that
    /// the key stays free for other apps until the owner turns the box on.
    func setAskKey() {
        if !Settings.askBox {
            askKey = nil
        } else if askKey == nil {
            askKey = HotKey(keyCode: UInt32(kVK_ANSI_A), modifiers: UInt32(controlKey | optionKey)) { [weak self] in
                Task { @MainActor in self?.ask.toggle() }
            }
        }
    }

    func toggleWindow() {
        if window.isVisible && window.isKeyWindow && NSApp.isActive {
            window.performClose(nil)
        } else {
            showWindow()
        }
    }

    func showWindow() { bringForward(window) }

    func windowWillClose(_ notification: Notification) {
        DispatchQueue.main.async { self.backToMenuBar() }
    }

    /// Once the board's window and every popped-out thread's window have closed, the app lives in the menu bar only.
    func backToMenuBar() {
        if !window.isVisible && mailWindows.windows.isEmpty { NSApp.setActivationPolicy(.accessory) }
    }

    func buildMainMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(), editItem = NSMenuItem(), viewItem = NSMenuItem(), windowItem = NSMenuItem()
        main.addItem(appItem)
        main.addItem(editItem)
        main.addItem(viewItem)
        main.addItem(windowItem)
        // Text fields get ⌘C/⌘V/⌘X/⌘A/⌘Z from these key equivalents; without an Edit menu they do nothing,
        // and dictation tools that insert by synthesising ⌘V fail too.
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        let app = NSMenu()
        app.addItem(withTitle: "Hide Agent Board", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(.separator())
        app.addItem(withTitle: "Quit Agent Board", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = app
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Refresh", action: #selector(refreshAll), keyEquivalent: "r").target = self
        view.addItem(withTitle: "Zoom In", action: #selector(zoomIn), keyEquivalent: "+").target = self
        view.addItem(withTitle: "Zoom Out", action: #selector(zoomOut), keyEquivalent: "-").target = self
        view.addItem(withTitle: "Actual Size", action: #selector(zoomReset), keyEquivalent: "0").target = self
        view.addItem(.separator())
        // the key window's: AppKit names it Exit Full Screen while that window is full screen
        let fullScreen = view.addItem(withTitle: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)),
                                      keyEquivalent: "f")
        fullScreen.keyEquivalentModifierMask = [.control, .command]
        viewItem.submenu = view
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowItem.submenu = win
        NSApp.mainMenu = main
    }

    @objc func zoomIn() { model.zoom(by: 1) }
    @objc func zoomOut() { model.zoom(by: -1) }
    @objc func zoomReset() { model.setZoom(1) }

    func updateStatusTitle() {
        guard let button = statusItem.button else { return }
        let title = NSMutableAttributedString()
        let font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .semibold)
        func add(_ text: String, _ color: NSColor) {
            title.append(NSAttributedString(string: text, attributes: [.foregroundColor: color, .font: font]))
        }
        switch model.link {
        case .live:
            let w = model.count(.waiting), b = model.count(.blocked), busy = model.count(.busy)
            let bg = model.count(.background)
            if w > 0 { add(" \(w)", AgentState.waiting.nsColor) }
            if b > 0 { add(" \(b)", AgentState.blocked.nsColor) }
            add(" \(busy)", NSColor(hex: 0xC99A66))      // the caramel, lifted for the menu bar
            if bg > 0 { add(" \(bg)", NSColor(hex: 0x9C7448)) }   // background-only, the darker caramel
        case .connecting:
            add(" …", .secondaryLabelColor)
        case .down:
            add(" ×", .secondaryLabelColor)
        }
        button.attributedTitle = title
        button.toolTip = "Agent Board: \(model.count(.waiting)) waiting on you, \(model.count(.blocked)) blocked, \(model.count(.busy)) busy, \(model.count(.background)) waiting on background work"
    }
}

/// Bring `w`, the board's window or a popped-out thread's (MailWindows), to the front with the app. While any of its
/// windows is open the board is a regular app (Dock icon, ⌘Tab, a menu bar); once all have closed, it goes back to
/// living in the menu bar only (AppDelegate.backToMenuBar).
@MainActor
func bringForward(_ w: NSWindow) {
    NSApp.setActivationPolicy(.regular)
    NSApp.activate(ignoringOtherApps: true)
    w.makeKeyAndOrderFront(nil)
}

/// Window shortcuts: ⌘+ ⌘- ⌘0 zoom, ⌘R refresh, ⌘W hide, ⌘Q quit. A local monitor sees the key before any
/// view does, so it works whatever has focus (except while a dialog sheet is up).
@MainActor
@discardableResult
func installKeyMonitor(window: NSWindow, model: BoardModel) -> Any? {
    NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak window, weak model] e in
        guard let window = window, let model = model, e.window === window, window.attachedSheet == nil,
              e.modifierFlags.intersection([.command, .option, .control]) == .command else { return e }
        switch e.charactersIgnoringModifiers ?? "" {
        case "=", "+": model.zoom(by: 1)
        case "-", "_": model.zoom(by: -1)
        case "0": model.setZoom(1)
        case "r": model.refreshAll()
        case "w": window.performClose(nil)          // the delegate drops back to menu-bar-only
        case "q": NSApp.terminate(nil)
        default: return e
        }
        return nil
    }
}

/// A global hotkey through Carbon's RegisterEventHotKey: works without accessibility permission. Each has an id of its
/// own; one handler, installed with the first, runs the action of the one pressed.
final class HotKey {
    private var ref: EventHotKeyRef?
    private let id: UInt32
    private static var actions: [UInt32: () -> Void] = [:]
    private static var handler: EventHandlerRef?
    private static var lastID: UInt32 = 0

    init(keyCode: UInt32, modifiers: UInt32, action: @escaping () -> Void) {
        HotKey.lastID += 1
        id = HotKey.lastID
        HotKey.actions[id] = action
        if HotKey.handler == nil {
            var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
            InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
                var pressed = EventHotKeyID()
                let got = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                                            nil, MemoryLayout<EventHotKeyID>.size, nil, &pressed)
                if got == noErr { HotKey.actions[pressed.id]?() }
                return noErr
            }, 1, &spec, nil, &HotKey.handler)
        }
        let status = RegisterEventHotKey(keyCode, modifiers, EventHotKeyID(signature: OSType(0x4142_4F44), id: id),
                                         GetApplicationEventTarget(), 0, &ref)              // "ABOD"
        if status != noErr {          // another app holds the key: say so in the log, as the key will do nothing
            NSLog("AgentBoard: hotkey %u (modifiers %u) not registered: OSStatus %d", keyCode, modifiers, status)
        }
    }

    deinit {
        if let r = ref { UnregisterEventHotKey(r) }
        HotKey.actions[id] = nil
    }
}

/// The board window's title: "Agent Board", then what it shows, so that the desktop's voice desk can tell which agent
/// is behind it (agentboard/voicedesk.py reads "[session ID]"): in the Mail tab, the open thread's subject and the
/// session of the agent that joined it last (its sessions come in the order of their first mail); else the selected
/// tile's agent. The title bar is hidden, so the owner never sees it. --selftest-title checks it.
enum BoardTitle {
    static let base = "Agent Board"

    @MainActor
    static func make(_ model: BoardModel) -> String {
        if model.tab == .mail {
            guard let id = model.mail.selected, let t = model.mail.current(id) else { return base + " — Mail" }
            return make(mail: t.subject, session: t.sessions?.last?.id)
        }
        guard let a = model.selected else { return base }
        return make(agent: a.name.isEmpty ? a.display : a.name, session: a.id)
    }

    static func make(mail subject: String, session: String?) -> String {
        base + " — Mail: " + subject + (session.map { " [session \($0)]" } ?? "")
    }

    static func make(agent name: String, session: String) -> String {
        base + " — " + name + " [session \(session)]"
    }

    /// --selftest-title: the titles the desktop's tests read (tests/test_voicedesk.py, the board's window), offline.
    @MainActor
    static func selftest() -> Int32 {
        var fails: [String] = []
        func check(_ got: String, _ want: String) {
            if got != want { fails.append("\(got.debugDescription) != \(want.debugDescription)") }
        }
        let a = "aaaaaaaa-0000-0000-0000-000000000001", c = "cccccccc-0000-0000-0000-000000000003"
        check(make(agent: "loader-fix", session: a), "Agent Board — loader-fix [session \(a)]")
        check(make(mail: "Results are in [#ab12cd34]", session: c),
              "Agent Board — Mail: Results are in [#ab12cd34] [session \(c)]")
        check(make(mail: "Loader cache: two choices before I merge", session: nil),
              "Agent Board — Mail: Loader cache: two choices before I merge")
        let model = BoardModel(layout: Layout(), persist: false)
        check(make(model), "Agent Board")                         // nothing selected
        model.tab = .mail
        check(make(model), "Agent Board — Mail")                  // no thread open
        print(fails.isEmpty ? "title: all checks pass" : fails.map { "FAIL " + $0 }.joined(separator: "\n"))
        return fails.isEmpty ? 0 : 1
    }
}
