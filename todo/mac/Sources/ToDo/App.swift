import AppKit
import Carbon
import Combine
import SwiftUI

/// A regular app (Dock, ⌘Tab) with a menu-bar item too: a caramel count of tasks agents are working on, and a
/// window toggled by clicking it or by the global hotkey ⌥⌘T (Carbon RegisterEventHotKey: no accessibility permission).
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let store = Store()
    var window: NSWindow!
    var statusItem: NSStatusItem!
    var hotKey: HotKey?
    var bag = Set<AnyCancellable>()
    let launched = Date()

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1220, height: 780),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "ToDo"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.contentView = NSHostingView(rootView: RootView(store: store).padding(.top, 18))
        window.setFrameAutosaveName("ToDoWindow")
        if !window.setFrameUsingName("ToDoWindow") { window.center() }
        window.delegate = self
        Dialogs.window = window

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = statusItem.button {
            b.image = NSImage(systemSymbolName: "checklist", accessibilityDescription: "ToDo")
            b.image?.isTemplate = true
            b.imagePosition = .imageLeading
            b.target = self
            b.action = #selector(statusClicked(_:))
            b.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        store.objectWillChange
            .debounce(for: .milliseconds(300), scheduler: RunLoop.main)
            .sink { [weak self] _ in self?.updateStatusTitle() }
            .store(in: &bag)

        hotKey = HotKey(keyCode: UInt32(kVK_ANSI_T), modifiers: UInt32(cmdKey | optionKey)) { [weak self] in
            Task { @MainActor in self?.toggleWindow() }
        }
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.store.reconnect() }
        }
        installKeyMonitor(window: window, store: store)
        buildMainMenu()
        store.connect()
        if !CommandLine.arguments.contains("--hidden") { showWindow() }
        watchForNewBuild()
        updateStatusTitle()
    }

    /// scripts/mac-install.sh replaces the binary under a running app; offer to relaunch into the new one.
    func watchForNewBuild() {
        let exe = Bundle.main.executablePath ?? ""
        let mtime = { (try? FileManager.default.attributesOfItem(atPath: exe)[.modificationDate] as? Date) ?? nil }
        let started = mtime()
        Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self = self, let now = mtime(), let then = started, now > then else { return }
                if !self.store.updateAvailable { self.store.updateAvailable = true }
            }
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return true
    }

    /// Switching to ToDo with ⌘Tab (or clicking it in the Dock) brings its window up, even after ⌘W closed it.
    /// Not in the first seconds after a hidden login launch, which also activates the app.
    func applicationDidBecomeActive(_ notification: Notification) {
        if CommandLine.arguments.contains("--hidden") && Date().timeIntervalSince(launched) < 3 { return }
        if !window.isVisible { window.makeKeyAndOrderFront(nil) }
    }

    @objc func statusClicked(_ sender: NSStatusBarButton) {
        if NSApp.currentEvent?.type == .rightMouseUp {
            let menu = NSMenu()
            menu.addItem(withTitle: "Open ToDo   ⌥⌘T", action: #selector(openFromMenu), keyEquivalent: "").target = self
            menu.addItem(withTitle: "Add a task…", action: #selector(addFromMenu), keyEquivalent: "").target = self
            menu.addItem(withTitle: "Refresh", action: #selector(refresh), keyEquivalent: "").target = self
            menu.addItem(withTitle: "Reconnect", action: #selector(reconnect), keyEquivalent: "").target = self
            menu.addItem(withTitle: "Desktop host (\(Settings.host))…", action: #selector(editHost), keyEquivalent: "").target = self
            if store.updateAvailable {
                menu.addItem(withTitle: "Relaunch into the new build", action: #selector(relaunch), keyEquivalent: "").target = self
            }
            menu.addItem(.separator())
            menu.addItem(withTitle: "Quit ToDo", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
            statusItem.menu = menu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
        } else {
            toggleWindow()
        }
    }

    @objc func openFromMenu() { showWindow() }
    @objc func addFromMenu() { showWindow(); store.requestAdd() }
    @objc func refresh() { store.refresh() }
    @objc func fullRefresh() { store.refresh(full: true) }
    @objc func reconnect() { store.reconnect() }
    @objc func relaunch() { store.relaunch() }
    @objc func editHost() { showWindow(); Dialogs.host(store: store) }
    @objc func newTask() { store.requestAdd() }
    @objc func find() { store.focusRequest = .search }
    @objc func zoomIn() { store.zoom(by: 1) }
    @objc func zoomOut() { store.zoom(by: -1) }
    @objc func zoomReset() { store.setZoom(1) }

    func toggleWindow() {
        if window.isVisible && window.isKeyWindow && NSApp.isActive {
            window.performClose(nil)
        } else {
            showWindow()
        }
    }

    func showWindow() {
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    func buildMainMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(), editItem = NSMenuItem(), taskItem = NSMenuItem(), viewItem = NSMenuItem(), windowItem = NSMenuItem()
        [appItem, editItem, taskItem, viewItem, windowItem].forEach { main.addItem($0) }
        let app = NSMenu()
        app.addItem(withTitle: "Hide ToDo", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(.separator())
        app.addItem(withTitle: "Quit ToDo", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = app
        // Text fields get ⌘C/⌘V/⌘X/⌘A/⌘Z from these; without an Edit menu they do nothing (and dictation that pastes fails).
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
        let task = NSMenu(title: "Task")
        task.addItem(withTitle: "New Task", action: #selector(newTask), keyEquivalent: "n").target = self
        task.addItem(withTitle: "Find", action: #selector(find), keyEquivalent: "f").target = self
        taskItem.submenu = task
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Refresh", action: #selector(refresh), keyEquivalent: "r").target = self
        let full = view.addItem(withTitle: "Fetch Every Task Again", action: #selector(fullRefresh), keyEquivalent: "r")
        full.keyEquivalentModifierMask = [.command, .shift]
        full.target = self
        view.addItem(.separator())
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

    func updateStatusTitle() {
        guard let button = statusItem?.button else { return }
        let title = NSMutableAttributedString()
        let font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .semibold)
        func add(_ text: String, _ color: NSColor) {
            title.append(NSAttributedString(string: text, attributes: [.foregroundColor: color, .font: font]))
        }
        let agents = store.activeAgents
        if agents > 0 { add(" \(agents)", Palette.caramelLift) }
        if !store.conflicts.isEmpty || !store.stuck.isEmpty { add(" !", NSColor(hex: 0xDA3633)) }
        if case .down = store.link, !store.pending.isEmpty { add(" ↑\(store.pending.count)", .secondaryLabelColor) }
        button.attributedTitle = title
        button.toolTip = "ToDo: \(store.live.filter { $0.isOpen }.count) open, \(agents) with an agent"
            + (store.pending.isEmpty ? "" : ", \(store.pending.count) edits waiting to save")
    }
}

/// Window keys. ⌘ shortcuts work everywhere; the plain keys (↑↓, space, return, esc, delete) only when no
/// text field is being typed in.
@MainActor
@discardableResult
func installKeyMonitor(window: NSWindow, store: Store) -> Any? {
    NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak window, weak store] e in
        guard let window = window, let store = store, e.window === window, window.attachedSheet == nil,
              store.assigning == nil else { return e }
        let mods = e.modifierFlags.intersection([.command, .option, .control, .shift])
        let typing = (window.firstResponder as? NSTextView)?.isEditable == true
        let chars = e.charactersIgnoringModifiers ?? ""
        if mods == .command {
            switch chars {
            case "=", "+": store.zoom(by: 1)
            case "-", "_": store.zoom(by: -1)
            case "0": store.setZoom(1)
            case "r": store.refresh()
            case "n": store.requestAdd()
            case "f": store.focusRequest = .search
            case "w": window.performClose(nil)
            case "q": NSApp.terminate(nil)
            case "z" where !typing: store.undo()
            case "e" where !typing: if let s = store.selection { store.editing = s }
            case "a" where !typing: store.markAll()
            case "g" where !typing:
                let ns = store.chosen
                if ns.count > 1 { Dialogs.groupName(store: store, numbers: ns) }
            case "\u{7F}" where !typing:
                if !store.marked.isEmpty { Dialogs.deleteMany(store.chosen, store: store) }
                else if let t = store.selected { Dialogs.deleteTask(t, store: store) }
            default: return e
            }
            return nil
        }
        if mods == [.command, .shift] && chars.lowercased() == "a" && !typing {
            let ns = store.chosen
            if ns.count > 1 {
                // One agent for all; tasks not yet on GitHub can't be named, so nothing happens until they are
                if let lead = ns.first, !ns.contains(where: { $0 < 0 }) {
                    store.assigning = AssignRequest(number: lead, others: Array(ns.dropFirst()))
                } else {
                    NSSound.beep()
                }
            } else if let t = store.selected, t.isOpen, !t.isLocal {
                store.assigning = AssignRequest(number: t.number)
            }
            return nil
        }
        if mods == [.command, .shift] && chars.lowercased() == "r" { store.refresh(full: true); return nil }
        guard !typing, mods.isEmpty || mods == .shift else { return e }
        switch e.keyCode {
        case UInt16(kVK_DownArrow): store.move(1)
        case UInt16(kVK_UpArrow): store.move(-1)
        case UInt16(kVK_Space):
            if !store.marked.isEmpty, store.editing == nil { store.completeMany(store.chosen) }
            else if let s = store.selection, store.editing == nil { store.toggleComplete(s) } else { return e }
        case UInt16(kVK_Return): if let s = store.selection, store.editing == nil { store.editing = s } else { return e }
        case UInt16(kVK_Escape):
            if store.editing != nil { store.editing = nil }
            else if !store.marked.isEmpty { store.clearMarks() }
            else if store.selection != nil { store.selection = nil } else { return e }
        default:
            switch chars {
            case "j": store.move(1)
            case "k": store.move(-1)
            default: return e
            }
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
        let id = EventHotKeyID(signature: OSType(0x544F_444F), id: 1)   // "TODO"
        RegisterEventHotKey(keyCode, modifiers, id, GetApplicationEventTarget(), 0, &ref)
    }

    deinit {
        if let r = ref { UnregisterEventHotKey(r) }
        if let h = handler { RemoveEventHandler(h) }
    }
}
