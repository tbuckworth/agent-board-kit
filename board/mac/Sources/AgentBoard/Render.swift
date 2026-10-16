import AppKit
import SwiftUI

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
        let given = layoutFile.flatMap { FileManager.default.contents(atPath: $0) }.flatMap { try? JSONDecoder().decode(Layout.self, from: $0) }
        let model = BoardModel(layout: given ?? Layout(), persist: false)
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
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(origin: .zero, size: size)
        let win = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        win.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        win.contentView = host
        win.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        win.orderBack(nil)
        host.layoutSubtreeIfNeeded()
        RunLoop.current.run(until: Date().addingTimeInterval(0.6))
        host.layoutSubtreeIfNeeded()
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return 1 }
        host.cacheDisplay(in: host.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { return 1 }
        do {
            try png.write(to: URL(fileURLWithPath: out))
        } catch {
            FileHandle.standardError.write("cannot write \(out): \(error)\n".data(using: .utf8)!)
            return 1
        }
        print("wrote \(out) (\(Int(size.width))x\(Int(size.height)), \(snap.agents.count) agents)")
        return 0
    }

    /// The switch sheet with saved options (from --probe), for a screenshot.
    static func renderSwitch(options path: String, snapshot: String, to out: String) -> Int32 {
        guard let od = FileManager.default.contents(atPath: path), let sd = FileManager.default.contents(atPath: snapshot),
              let o = try? JSON.decoder.decode(SwitchOptions.self, from: od),
              let snap = try? JSON.decoder.decode(Snapshot.self, from: sd),
              let agent = snap.agents.first(where: { $0.live }) else { print("cannot read inputs"); return 1 }
        let model = BoardModel(layout: Layout(), persist: false)
        let view = SwitchSheet(session: SwitchSession(agent: agent, modelOnly: false), model: model, preloaded: o)
            .background(Color(nsColor: .windowBackgroundColor))
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(origin: .zero, size: host.fittingSize)
        let win = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        win.appearance = NSAppearance(named: .darkAqua)
        win.contentView = host
        win.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        win.orderBack(nil)
        RunLoop.current.run(until: Date().addingTimeInterval(0.5))
        host.frame = NSRect(origin: .zero, size: host.fittingSize)
        host.layoutSubtreeIfNeeded()
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return 1 }
        host.cacheDisplay(in: host.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { return 1 }
        try? png.write(to: URL(fileURLWithPath: out))
        print("wrote \(out)")
        return 0
    }

    static func renderRestart(plan path: String, to out: String) -> Int32 {
        guard let d = FileManager.default.contents(atPath: path), let p = try? JSON.decoder.decode(RestartPlan.self, from: d) else {
            print("cannot read plan"); return 1
        }
        let model = BoardModel(layout: Layout(), persist: false)
        let host = NSHostingView(rootView: RestartSheet(model: model, preloaded: p).background(Color(nsColor: .windowBackgroundColor)))
        host.frame = NSRect(x: 0, y: 0, width: 720, height: 620)
        let win = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        win.appearance = NSAppearance(named: .darkAqua)
        win.contentView = host
        win.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        win.orderBack(nil)
        RunLoop.current.run(until: Date().addingTimeInterval(0.6))
        host.layoutSubtreeIfNeeded()
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return 1 }
        host.cacheDisplay(in: host.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: out))
        print("wrote \(out)")
        return 0
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
        var done = false
        Actions.runJSON(["action": "switch_options", "id": target.id]) { o, _ in opts = o; done = true }
        while !done && Date() < deadline.addingTimeInterval(90) { RunLoop.current.run(until: Date().addingTimeInterval(0.2)) }
        if let o = decodeSwitchOptions(opts) {
            print("switch options for \(target.display): busy=\(o.agent.busy ?? false) targets=\(o.targets.map { $0.target }) "
                  + "models=\(o.models.mapValues { $0.count })")
            if let path = ProcessInfo.processInfo.environment["AGENTBOARD_SAVE_SWITCH"], let raw = opts?["options"],
               let data = try? JSONSerialization.data(withJSONObject: raw) {
                try? data.write(to: URL(fileURLWithPath: path))
            }
        } else {
            print("switch options NOT decoded: \(opts ?? [:])")
        }
        return 0
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
        let t0 = Date()
        for h in [3.2, 26.0, 100.0] { print("reset in \(h)h → " + Fmt.resetAt(t0.timeIntervalSince1970 + h * 3600, now: t0)) }
        let ok = results.map { $0.1 } == [1.1, 1.25, 1.1, 1.0] && submitted == 2 && box.string == "\n"
        print(ok ? "keys OK" : "keys FAILED")
        return ok ? 0 : 1
    }
}


/// Headless check of the group / sub-group operations, and that the saved layout still decodes.
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
        let old = #"{"groups":[{"id":"\#(UUID().uuidString)","name":"Old","members":["x"],"collapsed":false}],"order":["y"]}"#
        let decoded = try? JSONDecoder().decode(Layout.self, from: Data(old.utf8))
        check(decoded?.groups.first?.name == "Old" && decoded?.groups.first?.subgroups.isEmpty == true, "old layout decodes")
        let real = Layout.url
        if let data = try? Data(contentsOf: real) {
            if let l = try? JSONDecoder().decode(Layout.self, from: data) {
                print("saved layout: \(l.groups.count) groups (\(l.groups.map { "\($0.name):\($0.allMembers.count)" }.joined(separator: ", "))), \(l.order.count) ordered")
            } else { fails.append("the saved layout does not decode") }
        }
        print(fails.isEmpty ? "layout OK" : "layout FAILED: " + fails.joined(separator: "; "))
        return fails.isEmpty ? 0 : 1
    }
}
