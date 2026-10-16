import AppKit
import Carbon
import Combine
import SwiftUI

/// Menu-bar app: a status item whose title counts the agents that need you, a window toggled by
/// clicking it or by the global hotkey (⌥⌘A, Carbon RegisterEventHotKey: no accessibility permission).
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let model = BoardModel()
    var window: NSWindow!
    var statusItem: NSStatusItem!
    var hotKey: HotKey?
    var bag = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1060, height: 680),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Agent Board"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.contentView = NSHostingView(rootView: BoardView(model: model).padding(.top, 18))
        window.setFrameAutosaveName("AgentBoardWindow")
        if !window.setFrameUsingName("AgentBoardWindow") { window.center() }
        window.delegate = self
        Dialogs.window = window

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
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.model.reconnect() }
        }
        installKeyMonitor(window: window, model: model)
        buildMainMenu()
        model.connect()
        if !CommandLine.arguments.contains("--hidden") { showWindow() }   // the login item starts in the menu bar only
        watchForNewBuild()
    }

    /// scripts/mac-build.sh replaces the binary under a running app; offer to relaunch into the new one.
    func watchForNewBuild() {
        let exe = Bundle.main.executablePath ?? ""
        let mtime = { (try? FileManager.default.attributesOfItem(atPath: exe)[.modificationDate] as? Date) ?? nil }
        let started = mtime()
        Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self = self, let now = mtime(), let then = started, now > then else { return }
                if !self.model.updateAvailable { self.model.updateAvailable = true }
            }
        }
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
    @objc func reconnect() { model.reconnect() }
    @objc func editHost() { showWindow(); Dialogs.host(model: model) }

    func toggleWindow() {
        if window.isVisible && window.isKeyWindow && NSApp.isActive {
            window.performClose(nil)
        } else {
            showWindow()
        }
    }

    /// While the window is open the board is a regular app (Dock icon, ⌘Tab, a menu bar); closed, it goes
    /// back to living in the menu bar only.
    func showWindow() {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ notification: Notification) {
        DispatchQueue.main.async { NSApp.setActivationPolicy(.accessory) }
    }

    func buildMainMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(), editItem = NSMenuItem(), viewItem = NSMenuItem(), windowItem = NSMenuItem()
        main.addItem(appItem)
        main.addItem(editItem)
        main.addItem(viewItem)
        main.addItem(windowItem)
        // Text fields get ⌘C/⌘V/⌘X/⌘A/⌘Z from these key equivalents; without an Edit menu they do nothing,
        // and dictation tools that insert by synthesising ⌘V (Wispr Flow) fail too.
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

/// A global hotkey through Carbon's RegisterEventHotKey: works without accessibility permission.
final class HotKey {
    private var ref: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private static var action: (() -> Void)?

    init(keyCode: UInt32, modifiers: UInt32, action: @escaping () -> Void) {
        HotKey.action = action
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
            HotKey.action?()
            return noErr
        }, 1, &spec, nil, &handler)
        let id = EventHotKeyID(signature: OSType(0x4142_4F44), id: 1)   // "ABOD"
        RegisterEventHotKey(keyCode, modifiers, id, GetApplicationEventTarget(), 0, &ref)
    }

    deinit {
        if let r = ref { UnregisterEventHotKey(r) }
        if let h = handler { RemoveEventHandler(h) }
    }
}
