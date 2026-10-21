import AppKit
import SwiftUI

/// Offscreen render of the real views from saved data (a `todo-desk snapshot` or the app's own cache).
@MainActor
enum Renderer {
    static func loadData(_ path: String) -> (issues: [Issue], config: TodoConfig?, pending: [PendingOp])? {
        guard let data = FileManager.default.contents(atPath: path),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        var pending: [PendingOp] = []
        if let p = obj["pending"], let pd = try? JSONSerialization.data(withJSONObject: p) {
            pending = (try? JSONDecoder().decode([PendingOp].self, from: pd)) ?? []
        }
        return (decodeIssues(obj["issues"]), decodeConfig(obj["config"]), pending)
    }

    static func render(data path: String, to out: String, size: CGSize, dark: Bool, list: String?, select: Int?,
                       edit: Int?, assign: Int?, agents: Bool, search: String?, zoom: CGFloat,
                       running: Bool = false) -> Int32 {
        guard let d = loadData(path) else {
            FileHandle.standardError.write("cannot read \(path)\n".data(using: .utf8)!)
            return 1
        }
        let store = Store(persist: false)
        store.install(issues: d.issues, config: d.config, pending: d.pending)
        store.link = .live
        store.syncedAt = isoString(Date().addingTimeInterval(-4))
        store.zoom = zoom
        if let l = list { store.route = .list(l) }
        if agents { store.route = .agents }
        if let q = search { store.query = q; store.route = .search }
        store.selection = select ?? edit
        store.editing = edit
        if let g = store.items[select ?? 0]?.group { store.expanded.insert(g) }
        if let a = assign {
            guard let t = store.items[a] else { print("no task #\(a)"); return 1 }
            store.previewStub = "[message from the owner via the to-do app (the Mac app, over SSH), not by email …]\n\nThe owner assigned this to-do to an agent from the to-do app. Start one agent to do it.\n\nTask #\(t.number): \(t.title)\nIssue: \(t.issue.url)\nList: \(t.list)    Labels: \(t.issue.labels.joined(separator: ", "))"
            store.agentsStub = Renderer.sampleAgents
            let sheet = AssignSheet(task: t, store: store, startRunning: running,
                                    startChosen: running ? Renderer.sampleAgents[1].id : nil).environment(\.zoom, zoom)
                .background(Color(nsColor: .windowBackgroundColor))
            return snap(AnyView(sheet), size: nil, dark: dark, to: out, settle: 0.8, expandPreview: true)
        }
        let view = RootView(store: store).padding(.top, 18).frame(width: size.width, height: size.height)
        return snap(AnyView(view), size: size, dark: dark, to: out, settle: 0.8)
    }

    /// Running agents for the offscreen Assign sheet (--running)
    static let sampleAgents: [BoardAgent] = [
        BoardAgent(id: "01a11bf1-5b48-7651-b12c-140cb15c777e", cli: "codex", name: "docs-cleanup", state: "waiting",
                   folder: "docs-site", note: "Two style guides disagree on headings; asked the owner which to keep",
                   active: isoString(Date().addingTimeInterval(-600)), group: "Work / Docs"),
        BoardAgent(id: "8d8bd0a8-3b6e-484b-89fe-217b761a2481", cli: "claude", name: "theme-colours", state: "idle",
                   folder: "website", note: "Added 12 background presets and a background colour picker",
                   active: isoString(Date().addingTimeInterval(-3 * 3600)), group: "Side projects"),
        BoardAgent(id: "d21ff850-e1bb-46d1-8d0a-c5bc8842af30", cli: "claude", name: "importance-urgency reply", state: "busy",
                   folder: "to-do-todo-42-importance", note: "Assign to a running agent: helper, Mac and web",
                   active: isoString(Date().addingTimeInterval(-40)), group: "Admin"),
        BoardAgent(id: "dad1b5f2-9dd2-4947-9010-b3acaffc4ed9", cli: "claude", name: "agent-board-mac", state: "stale",
                   folder: "agent-board", note: "", active: isoString(Date().addingTimeInterval(-20 * 3600)), group: "Admin"),
    ]

    static func snap(_ view: AnyView, size: CGSize?, dark: Bool, to out: String, settle: Double, expandPreview: Bool = false) -> Int32 {
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(origin: .zero, size: size ?? host.fittingSize)
        let win = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        win.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        win.contentView = host
        win.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        win.orderBack(nil)
        host.layoutSubtreeIfNeeded()
        RunLoop.current.run(until: Date().addingTimeInterval(settle))
        if size == nil { host.frame = NSRect(origin: .zero, size: host.fittingSize) }
        host.layoutSubtreeIfNeeded()
        RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return 1 }
        host.cacheDisplay(in: host.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { return 1 }
        do { try png.write(to: URL(fileURLWithPath: out)) } catch {
            FileHandle.standardError.write("cannot write \(out): \(error)\n".data(using: .utf8)!)
            return 1
        }
        print("wrote \(out) (\(Int(host.bounds.width))x\(Int(host.bounds.height)))")
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

/// The icon: AgentBoard's dark squircle, with three task rows; the first is ticked in the board's caramel.
struct AppIcon: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 230, style: .continuous)
                .fill(LinearGradient(colors: [Color(hex: 0x2A2F37), Color(hex: 0x14171C)], startPoint: .top, endPoint: .bottom))
                .frame(width: 824, height: 824)
            VStack(alignment: .leading, spacing: 70) {
                row(done: true, width: 300)
                row(done: false, width: 360)
                row(done: false, width: 250)
            }
        }
        .frame(width: 1024, height: 1024)
    }

    func row(done: Bool, width: CGFloat) -> some View {
        HStack(spacing: 56) {
            ZStack {
                if done {
                    Circle().fill(Color(hex: 0xAD7D48))
                    Image(systemName: "checkmark").font(.system(size: 64, weight: .heavy)).foregroundColor(.white)
                } else {
                    Circle().strokeBorder(Color(hex: 0xEAD3BF), lineWidth: 18)
                }
            }
            .frame(width: 124, height: 124)
            RoundedRectangle(cornerRadius: 22).fill(Color(hex: 0xEAD3BF).opacity(done ? 0.45 : 0.9)).frame(width: width, height: 44)
        }
    }
}

@MainActor
func spin(_ seconds: Double, until done: () -> Bool) -> Bool {
    let deadline = Date().addingTimeInterval(seconds)
    while !done() && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.1)) }
    return done()
}

/// Headless smoke test of the real transport.
@MainActor
enum Probe {
    static func run() -> Int32 {
        let store = Store(persist: false)
        print("connecting: ssh \(Settings.host) \(Settings.remoteCommand)")
        store.connect()
        let t0 = Date()
        guard spin(200, until: { store.hasLoaded }) else {
            print("no tasks within 200 s; link: \(store.link) \(store.syncError ?? "")")
            return 1
        }
        let open = store.live.filter { $0.isOpen }
        print(String(format: "hello in %.1f s: %d tasks (%d open), %d lists, %d groups, synced %@",
                     Date().timeIntervalSince(t0), store.items.count, open.count, store.allLists.count,
                     store.config?.groups.count ?? 0, store.syncedAt ?? "?"))
        for (l, n) in store.openCounts().sorted(by: { $0.value > $1.value }).prefix(5) { print("  \(n)\t\(l)") }
        if let t = store.open(in: "tasks").first {
            var msg: String?
            store.assignPreview(t.number, text: "(probe)") { m, e in msg = m ?? "error: \(e ?? "?")" }
            _ = spin(30) { msg != nil }
            print("assign preview for #\(t.number): " + (msg?.components(separatedBy: "\n").dropFirst(2).prefix(3).joined(separator: " | ") ?? "timeout"))
        }
        store.channel.stop()
        return 0
    }
}

/// The body, label and queue logic, offline.
@MainActor
enum SelfTest {
    static func run() -> Int32 {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append(what) } }

        let body = "Notes here\n\n## Checklist\n- [ ] one\n- [ ] two\n\n## Claude Resolution Notes\nold notes"
        check(IssueBody.join(IssueBody.split(body)) == body, "body round trip")
        let items = IssueBody.checklist(body)
        check(items.map { $0.text } == ["one", "two"], "checklist parse")
        let toggled = IssueBody.toggling(body, items[1])
        check(toggled.contains("- [x] two") && toggled.contains("- [ ] one") && toggled.contains("old notes"), "toggle in place")
        check(!IssueBody.removing(body, items[0]).contains("one"), "remove item")
        check(IssueBody.adding("Just notes", item: "new").hasSuffix("## Checklist\n- [ ] new"), "add creates section")
        let added = IssueBody.adding(body, item: "three")
        check(added.contains("- [ ] two\n- [ ] three\n\n## Claude"), "add appends in section: \(added)")
        check(IssueBody.withNotes(body, "New notes").hasPrefix("New notes\n\n## Checklist"), "replace notes keeps sections")
        let loose = "- [ ] Good plasters\n- [x] Aloe vera"
        check(IssueBody.checklist(loose, in: "").count == 2, "loose checkboxes found")
        check(IssueBody.toggling(loose, IssueBody.checklist(loose, in: "")[0]).hasPrefix("- [x] Good plasters"), "loose toggle")
        check(IssueBody.build(notes: "n", checklist: ["a", " "]) == "n\n\n## Checklist\n- [ ] a", "build body")

        check(Labels.apply(["list:books", "source:app"], add: ["list:tasks"], remove: []).sorted() == ["list:tasks", "source:app"], "single-valued add")
        check(Labels.apply(["List:Books"], add: [], remove: ["list:books"]).isEmpty, "case-insensitive remove")

        let cfg = TodoConfig(groups: [GroupMeta(name: "To Do", sortOrder: 4)],
                             listMetadata: [ListMeta(listName: "to-buy", groupName: "To Do", sortOrder: 0),
                                            ListMeta(listName: "Tasks", groupName: nil, sortOrder: 0)])
        let issue = Issue(number: 5, title: "Buy milk", body: body, state: "OPEN", url: "u", createdAt: "2026-10-01T00:00:00Z",
                          updatedAt: "2026-10-02T00:00:00Z", labels: ["list:tasks", "source:app", "status:waiting"], agent: nil)
        let t = TaskItem(issue, config: cfg)
        check(t.list == "tasks" && t.priority == "medium" && t.action == "manual", "defaults")
        let linked = Issue(number: 9, title: "x", body: "", state: "OPEN", url: "https://github.com/example-user/tasks/issues/9",
                           createdAt: "2026-10-01T00:00:00Z", updatedAt: "2026-10-02T00:00:00Z", labels: [], agent: nil)
        check(linked.shortRef == "example-user/tasks #9" && issue.shortRef == "#5", "repo read from the issue URL: \(linked.shortRef)")
        let d = labelDiff(from: t, list: "to-buy", section: "home", priority: "high", config: cfg)
        check(Set(d.add) == ["list:to-buy", "group:To Do", "section:home", "priority:high"], "label diff adds \(d.add)")
        check(d.remove == ["list:tasks"], "label diff removes \(d.remove)")
        check(labelDiff(from: t, list: "tasks", section: nil, priority: "medium", config: cfg).add.isEmpty, "no-op diff")

        // tags and waiting on someone
        check(Tags.normalise("  Only At Home! ") == "only-at-home" && Tags.normalise("tag:Home") == "home"
              && Tags.normalise("a -- b") == "a-b" && Tags.normalise("!!").isEmpty, "tag names normalised")
        check(TagMode.next(nil) == .only && TagMode.next(.only) == .hide && TagMode.next(.hide) == nil, "tag chip cycles")
        check(Waiting.who("Waiting on: Sam Jones\n\nnotes") == "Sam Jones" && Waiting.who("waiting ON:  the landlord ") == "the landlord"
              && Waiting.who("notes\nWaiting on: Sam") == nil && Waiting.who("Waiting on:") == nil, "who is read from the first line only")
        check(Waiting.withWho("notes\n\n## Checklist\n- [ ] a", "Sam") == "Waiting on: Sam\n\nnotes\n\n## Checklist\n- [ ] a", "who line added")
        check(Waiting.withWho("Waiting on: Sam\n\nnotes", "Alex") == "Waiting on: Alex\n\nnotes", "who line replaced")
        check(Waiting.withWho("Waiting on: Sam\n\nnotes", nil) == "notes" && Waiting.withWho("", "Sam") == "Waiting on: Sam", "who line removed, or alone")
        let wi = Issue(number: 9, title: "Hear from Sam", body: "Waiting on: Sam\n\nAsked about the lease", state: "OPEN", url: "",
                       createdAt: "2026-10-01T00:00:00Z", updatedAt: "2026-10-03T00:00:00Z",
                       labels: ["list:tasks", "status:waiting", "follow-up:2026-10-20", "tag:Home", "action:email"], agent: nil)
        let wt = TaskItem(wi, config: nil)
        check(wt.waiting && wt.waitingOn == "Sam" && wt.followUp == "2026-10-20" && wt.notes == "Asked about the lease" && wt.tags == ["home"],
              "a waiting task is read: \(wt.notes) \(wt.tags)")
        check(wt.greyed(today: "2026-10-19") && !wt.followUpDue(today: "2026-10-19") && wt.followUpDue(today: "2026-10-20")
              && !wt.greyed(today: "2026-10-21"), "greyed until the follow-up day")
        let off = waitingDiff(from: wt, waiting: false, followUp: nil)
        check(off.add.isEmpty && Set(off.remove) == ["status:waiting", "follow-up:2026-10-20"], "waiting off takes both labels: \(off)")
        let moved = waitingDiff(from: wt, waiting: true, followUp: "2026-10-27")
        check(moved.add == ["follow-up:2026-10-27"] && moved.remove == ["follow-up:2026-10-20"], "a new follow-up day replaces the old: \(moved)")
        let on = waitingDiff(from: t, waiting: true, followUp: nil)
        check(on.add.isEmpty && on.remove.isEmpty, "already waiting (the test issue has the label): \(on)")
        let td = tagDiff(from: wt, to: ["home", "Phone Call"])
        check(td.add == ["tag:phone-call"] && td.remove.isEmpty, "add a tag: \(td)")
        check(tagDiff(from: wt, to: []).remove == ["tag:Home"], "remove a tag by GitHub's spelling")
        check(Labels.apply(["follow-up:2026-10-20"], add: ["follow-up:2026-10-27"], remove: []) == ["follow-up:2026-10-27"], "one follow-up day")
        let today = "2026-10-16"
        let plain = TaskItem(Issue(number: 10, title: "p", body: "", state: "OPEN", url: "", createdAt: "2026-10-01T00:00:00Z",
                                   updatedAt: "2026-10-01T00:00:00Z", labels: ["list:tasks"], agent: nil), config: nil)
        let due = TaskItem(Issue(number: 11, title: "d", body: "", state: "OPEN", url: "", createdAt: "2026-10-01T00:00:00Z",
                                 updatedAt: "2026-10-02T00:00:00Z", labels: ["list:tasks", "status:waiting", "follow-up:2026-10-10"], agent: nil), config: nil)
        check(Store.waitingLast([wt, plain, due], today: today).map { $0.number } == [11, 10, 9],
              "waiting tasks sort last, a due follow-up doesn't")
        var f = Filters()
        f.cycle("home")
        check(f.acceptsTags(wt) && !f.acceptsTags(plain) && f.active, "only home")
        f.cycle("home")
        check(!f.acceptsTags(wt) && f.acceptsTags(plain), "hide home")
        f.cycle("home")
        check(f.tags.isEmpty && !f.active, "filter off again")
        let ws = Store(persist: false)
        ws.install(issues: [wi], config: nil)
        ws.link = .down("test")
        ws.saveEdit(9, title: wi.title, notes: "Asked about the lease, again", list: "tasks", section: nil, priority: "medium")
        check(ws.items[9]?.issue.body == "Waiting on: Sam\n\nAsked about the lease, again", "editing notes keeps the who line: \(ws.items[9]?.issue.body ?? "")")
        ws.saveEdit(9, title: wi.title, notes: "Asked about the lease, again", list: "tasks", section: nil, priority: "medium",
                    tags: ["home", "admin"], waiting: WaitingEdit(on: true, who: "Sam and Jo", followUp: "2026-10-30"))
        let we = ws.items[9]
        check(we?.waitingOn == "Sam and Jo" && we?.followUp == "2026-10-30" && we?.tags == ["admin", "home"]
              && we?.issue.labels.filter({ $0.hasPrefix("follow-up:") }).count == 1, "edit who, follow-up and tags: \(we?.issue.labels ?? [])")
        check(we?.issue.labels.contains("action:email") == true, "an old action label is kept")
        ws.waitMany([9], on: false)
        check(ws.items[9]?.waiting == false && ws.items[9]?.followUp == nil && ws.items[9]?.issue.body == "Asked about the lease, again",
              "no longer waiting: labels and the who line go")
        ws.undo()
        check(ws.items[9]?.waitingOn == "Sam and Jo" && ws.items[9]?.waiting == true, "undo brings them back")
        ws.tagMany([9], "Phone", on: true)
        check(ws.items[9]?.tags.contains("phone") == true, "tag from the menu")
        check(ws.pending.count == 1 && ws.pending[0].add.contains("tag:phone"), "all folded into one request")
        // an agent thinks the wait is over: on top, then Still waiting or OK
        let ci = Issue(number: 12, title: "Hear from Jo", body: "Waiting on: Jo\n\nnote\n\n## Claude Resolution Notes\nJo replied on 15 Oct",
                       state: "OPEN", url: "", createdAt: "2026-10-01T00:00:00Z", updatedAt: "2026-10-01T00:00:00Z",
                       labels: ["list:tasks", "status:waiting-cleared", "follow-up:2026-10-20"], agent: nil)
        let ct = TaskItem(ci, config: nil)
        check(ct.cameThrough && !ct.greyed(today: today) && ct.waitingOn == "Jo", "a cleared task is read")
        check(Store.waitingLast([plain, wt, ct, due], today: today).map { $0.number } == [12, 11, 10, 9], "a cleared task goes to the top")
        let cs = Store(persist: false)
        cs.install(issues: [ci], config: nil)
        cs.link = .down("test")
        cs.answerCleared(12, stillWaiting: true)
        check(cs.items[12]?.waiting == true && cs.items[12]?.cleared == false && cs.items[12]?.followUp == "2026-10-20"
              && cs.items[12]?.waitingOn == "Jo", "still waiting: back to waiting, who and follow-up kept")
        cs.undo()
        cs.answerCleared(12, stillWaiting: false)
        let ok = cs.items[12]
        check(ok?.cleared == false && ok?.waiting == false && ok?.followUp == nil && ok?.waitingOn == nil
              && ok?.claudeNotes == "Jo replied on 15 Oct", "OK: cleared, who and follow-up go; notes stay: \(ok?.issue.labels ?? [])")
        let again = waitingDiff(from: ct, waiting: true, followUp: "2026-10-20")
        check(again.add == ["status:waiting"] && again.remove == ["status:waiting-cleared"], "waiting on again removes the cleared label: \(again)")
        check(ws.addTask(title: "New", list: "tasks").map { ws.items[$0]?.issue.labels.contains { $0.hasPrefix("action:") } } == false,
              "a new task gets no action label")

        // the queue, with no desktop
        let s = Store(persist: false)
        s.install(issues: [issue], config: cfg)
        s.link = .down("test")
        s.toggleItem(5, s.items[5]!.checklist[0])
        s.toggleComplete(5)
        s.saveEdit(5, title: "Buy oat milk", notes: "Notes here", list: "to-buy", section: nil, priority: "high")
        check(s.pending.count == 1, "edits to one task fold into one request (\(s.pending.count))")
        let op = s.pending.first
        check(op?.base["body"] == body && op?.base["title"] == "Buy milk", "base is what it was before the first edit")
        check(op?.set["state"] == "CLOSED", "state carried")
        let shown = s.items[5]
        check(shown?.title == "Buy oat milk" && shown?.isOpen == false && shown?.list == "to-buy" && shown?.priority == "high"
              && shown?.checklist.first?.checked == true && shown?.issue.labels.contains("status:waiting") == true,
              "overlay shows every waiting edit and keeps other labels")
        check(shown?.pending == true, "marked as waiting")
        let n = s.addTask(title: "New thing", list: "to-buy", checklist: ["x"])!
        check(n < 0 && s.items[n]?.issue.labels.contains("group:To Do") == true, "local task with its group")
        s.update(n, set: ["title": "New thing!"], what: "t")
        check(s.pending.count == 2 && s.items[n]?.title == "New thing!", "edit folds into an unsent create")
        s.toggleComplete(n)
        check(s.pending.count == 3, "completing an unsent task is its own step")
        s.keepTheirs(5)
        check(s.items[5]?.title == "Buy milk" && s.items[5]?.pending == false, "keep GitHub's drops my edit")
        s.newList("Gift Ideas", group: "To Do")
        check(s.config?.group(of: "gift-ideas") == "To Do" && s.allLists.contains("gift-ideas"), "new list shows at once")
        s.deleteList("to-buy")
        check(s.items[n]?.list == "tasks", "deleting a list moves its tasks to Tasks")
        let sb = s.sidebar
        check(sb.contains { if case .group(let g, _, let ls) = $0 { return g == "To Do" && ls.contains { $0.name == "gift-ideas" } }; return false },
              "sidebar groups lists")

        // inline checklist edit, and undo
        let u = Store(persist: false)
        u.install(issues: [issue], config: cfg)
        u.link = .down("test")
        u.editItem(5, u.items[5]!.checklist[0], to: "one, see https://example.com")
        check(u.items[5]!.checklist[0].text == "one, see https://example.com" && !u.items[5]!.checklist[0].checked, "edit an item in place")
        u.toggleItem(5, u.items[5]!.checklist[1])
        u.toggleComplete(5)
        check(u.items[5]?.isOpen == false && u.undoStack.count == 3, "three undoable steps")
        u.undo()
        check(u.items[5]?.isOpen == true, "undo completing")
        u.undo()
        check(u.items[5]?.checklist[1].checked == false, "undo ticking")
        u.undo()
        check(u.items[5]?.checklist[0].text == "one" && u.undoStack.isEmpty, "undo the edit")
        let n2 = u.addTask(title: "Temp", list: "tasks")!
        u.undo()
        check(u.items[n2] == nil, "undo adding removes the new task")

        // sidebar moves, and the stale "To Sell"
        func iss(_ n: Int, _ list: String) -> Issue {
            Issue(number: n, title: "t\(n)", body: "", state: "OPEN", url: "", createdAt: "2026-10-01T00:00:00Z",
                  updatedAt: "2026-10-02T00:00:00Z", labels: ["list:" + list], agent: nil)
        }
        let mcfg = TodoConfig(groups: [GroupMeta(name: "To Do", sortOrder: 4), GroupMeta(name: "Digest", sortOrder: 5)],
                              listMetadata: [ListMeta(listName: "Tasks", groupName: nil, sortOrder: 0),
                                             ListMeta(listName: "To Sell", groupName: nil, sortOrder: 0),
                                             ListMeta(listName: "to-sell", groupName: "To Do", sortOrder: 0),
                                             ListMeta(listName: "to-buy", groupName: "To Do", sortOrder: 1),
                                             ListMeta(listName: "books", groupName: "Digest", sortOrder: 0)])
        let m = Store(persist: false)
        m.install(issues: [iss(1, "tasks"), iss(2, "to-buy"), iss(3, "books"), iss(4, "to-sell")], config: mcfg)
        m.link = .down("test")
        check(!m.allLists.contains("to sell") && m.allLists.contains("to-sell"), "the stale “To Sell” is hidden")
        check(m.topOrder == [.list("tasks"), .group("To Do"), .group("Digest")], "starting order \(m.topOrder)")
        m.moveGroup("Digest", before: .group("To Do"))
        check(m.topOrder == [.list("tasks"), .group("Digest"), .group("To Do")], "drag a group up: \(m.topOrder)")
        m.moveList("books", toGroup: "To Do", before: "to-buy")
        check(m.lists(inGroup: "To Do") == ["to-sell", "books", "to-buy"] && m.lists(inGroup: "Digest").isEmpty,
              "drag a list into another group, before a list: \(m.lists(inGroup: "To Do"))")
        m.moveList("to-buy", toGroup: nil, before: "tasks")
        check(m.topOrder.first == .list("to-buy") && m.config?.group(of: "to-buy") == nil, "drag a list out to the top: \(m.topOrder)")
        m.moveList("tasks", toGroup: "Digest", before: nil)
        check(m.lists(inGroup: "Digest") == ["tasks"], "drag a list into a group")
        let ops = m.pending.filter { $0.kind == .config }.flatMap { $0.configOps }
        check(ops.contains { $0.op == "set_sort" && $0.group == "Digest" } && ops.contains { $0.op == "set_group" && $0.list == "books" },
              "moves are saved as config operations")

        // a to-do dragged onto another list
        m.moveTask(2, toList: "books")
        check(m.items[2]?.list == "books" && m.items[2]?.issue.labels.contains("group:" + (m.config?.group(of: "books") ?? "?")) == true,
              "drag a to-do to another list (and its group): \(m.items[2]?.issue.labels ?? [])")
        m.undo()
        check(m.items[2]?.list == "to-buy", "undo the move")

        // groups of tasks: sub-issues + the subtask label, shown nested, undoable
        func gi(_ n: Int, parent: Int? = nil, state: String = "OPEN", labels: [String] = []) -> Issue {
            var i = Issue(number: n, title: "g\(n)", body: "", state: state, url: "", createdAt: "2026-10-01T00:00:00Z",
                          updatedAt: "2026-10-0\(min(9, n))T00:00:00Z", labels: ["list:tasks"] + labels, agent: nil)
            i.parent = parent
            return i
        }
        let g = Store(persist: false)
        g.folded = []
        g.install(issues: [gi(1), gi(2, parent: 1), gi(3, parent: 1, state: "CLOSED"), gi(4, labels: ["importance:low"]),
                           gi(5, labels: ["importance:high", "urgency:high", "due:2026-11-02"])], config: nil)
        g.link = .down("test")
        let rows = g.groupedRows(g.open(in: "tasks"))
        check(rows.map { $0.task.number } == [5, 4, 1, 2] && rows.first { $0.task.number == 2 }?.depth == 1,
              "a group's task sits under it: \(rows.map { "\($0.task.number)@\($0.depth)" })")
        let r1 = rows.first { $0.task.number == 1 }
        check(r1?.childCount == 2 && r1?.doneCount == 1, "group counts 1/2")
        g.toggleFold(1)
        check(g.groupedRows(g.open(in: "tasks")).map { $0.task.number } == [5, 4, 1], "folded group hides its tasks")
        g.toggleFold(1)

        // ⇧-click a range, ⌘-click one more
        g.click(5, shift: false, command: false)
        g.click(1, shift: true, command: false)
        check(g.marked == [5, 4, 1], "⇧-click selects the range: \(g.marked.sorted())")
        g.click(4, shift: false, command: true)
        check(g.marked == [5, 1], "⌘-click takes one out")
        g.click(2, shift: false, command: false)
        check(g.marked.isEmpty && g.selection == 2, "a plain click selects only that one")

        // a new group of 4 and 5: created here (negative number), rated from its tasks, both moved into it
        let grp = g.groupMany([4, 5], name: "Pair")!
        check(grp < 0 && g.items[grp]?.title == "Pair", "new group made locally")
        let glabels = Set(g.items[grp]?.issue.labels ?? [])
        check(glabels.isSuperset(of: ["list:tasks", "importance:high", "urgency:high", "due:2026-11-02"]), "group rated from its tasks: \(glabels.sorted())")
        check(g.items[4]?.parent == grp && g.items[5]?.issue.labels.contains(subtaskLabel) == true, "tasks in the group, with the subtask label")
        let sp = g.pending.filter { $0.kind == .setParent }
        check(sp.count == 2 && sp.allSatisfy { $0.parent == grp }, "two set_parent requests waiting for the group")
        check(sp.first?.request["cmd"] as? String == "set_parent", "set_parent request")
        g.undo()
        check(g.items[grp] == nil && g.items[4]?.parent == nil && g.items[5]?.issue.labels.contains(subtaskLabel) == false,
              "undo grouping: the group goes and its tasks come out")
        check(!g.pending.contains { $0.kind == .setParent && ($0.parent ?? 0) < 0 }, "no request left waiting on the vanished group")

        // drop onto a group: join it, no question; complete the group: its open tasks too; undo both
        check(g.drop("task:4", onto: 1), "drop a task onto a group")
        check(g.items[4]?.parent == 1, "joined the group")
        g.completeMany([1])
        check(g.items[1]?.isOpen == false && g.items[2]?.isOpen == false && g.items[4]?.isOpen == false, "the group's tasks completed with it")
        g.undo()
        check(g.items[1]?.isOpen == true && g.items[2]?.isOpen == true && g.items[4]?.isOpen == true, "undo completes none")
        g.undo()
        check(g.items[4]?.parent == nil, "undo the join")
        g.moveMany([1], toList: "books")
        check(g.items[2]?.list == "books" && g.items[1]?.list == "books" && g.items[5]?.list == "tasks", "moving a group takes its tasks")
        g.undo()
        g.ungroupMany([1])
        check(g.items[2]?.parent == nil && g.items[1]?.isOpen == false && g.isDeleted(g.items[1]!), "ungroup: tasks out, group deleted")
        g.undo()
        check(g.items[2]?.parent == 1 && g.items[1]?.isOpen == true, "undo the ungroup")
        g.rateMany([1], importance: "high")
        check(g.items[1]?.issue.labels.contains("importance:high") == true && g.items[2]?.issue.labels.contains("importance:high") == false,
              "a group is rated itself")
        check(g.assignText(lead: 1, others: [5], text: "go").contains("#2") && g.assignText(lead: 1, others: [5], text: "go").contains("#5"),
              "one agent is told of the group's tasks and the others")

        // Assign to a running agent: todo-desk's `agents` records, search, and the board's colours
        let agentRows: [Any] = [
            ["id": "01a11bf1-5b48-7651-b12c-140cb15c777e", "cli": "codex", "name": "docs-cleanup", "state": "waiting",
             "folder": "docs", "note": "asked about headings", "active": "2026-10-16T14:00:00Z", "group": "Work / Docs"],
            ["id": "8d8bd0a8-3b6e-484b-89fe-217b761a2481", "cli": "claude", "name": "theme-colours", "state": "idle",
             "folder": "website", "note": "", "active": NSNull(), "group": NSNull()],
            ["id": "broken"],
        ]
        let ba = BoardAgent.decode(agentRows)
        check(ba.count == 2 && ba[0].cli == "codex" && ba[1].active == nil && ba[1].group == nil, "agents decode, a bad row skipped")
        check(ba[0].matches("docs headings") && !ba[0].matches("theme") && ba[1].matches("THEME"),
              "agent search: every word, any field, any case")
        check(ba[0].stateLabel == "waiting on you" && ba[1].stateLabel == "idle", "agent state words")
        check(Set(["busy", "background", "waiting", "blocked", "idle", "stale"].map { "\(Palette.agentState($0))" }).count == 6,
              "each agent state has its own colour")

        print(fails.isEmpty ? "selftest OK" : "selftest FAILED:\n  " + fails.joined(separator: "\n  "))
        return fails.isEmpty ? 0 : 1
    }
}

/// Create, edit, complete and delete a dummy task on GitHub through the real link, including edits made while
/// disconnected. The task is titled as a self-test and ends deleted (closed, with no list), as the app deletes.
@MainActor
enum RemoteTest {
    static func run() -> Int32 {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { print((ok ? "ok    " : "FAIL  ") + what); if !ok { fails.append(what) } }
        let s = Store(persist: false)
        s.connect()
        guard spin(200, until: { s.hasLoaded && s.link.isLive }) else { print("could not connect: \(s.link)"); return 1 }
        check(true, "connected; \(s.items.count) tasks")
        let stamp = isoString(Date())
        let title = "ToDo Mac app self-test \(stamp) (safe to ignore)"
        s.addTask(title: title, notes: "Made by ToDo --selftest-remote; it deletes itself.", list: "tasks", checklist: ["first item", "second item"])
        check(spin(60) { s.pending.isEmpty }, "create reached GitHub")
        guard let n = s.items.values.first(where: { $0.title == title && $0.number > 0 })?.number else {
            print("FAIL  the new task never got an issue number"); return 1
        }
        check(s.server[n]?.body.contains("## Checklist\n- [ ] first item") == true, "#\(n) has its checklist")
        s.toggleItem(n, s.items[n]!.checklist[0])
        s.saveEdit(n, title: title + " edited", notes: "Edited notes.", list: "tasks", section: nil, priority: "high",
                   tags: ["home"], waiting: WaitingEdit(on: true, who: "the self-test", followUp: nil))
        check(spin(60) { s.pending.isEmpty }, "edits reached GitHub")
        let e = s.server[n]
        check(e?.title == title + " edited", "title saved")
        check(e?.body.contains("- [x] first item") == true && e?.body.hasPrefix("Waiting on: the self-test\n\nEdited notes.") == true,
              "checklist tick, who and notes saved")
        check(e?.labels.contains("priority:high") == true && e?.labels.contains("tag:home") == true
              && e?.labels.contains("status:waiting") == true && e?.labels.contains("source:app") == true, "labels saved: \(e?.labels ?? [])")

        s.channel.disconnect()
        check(!s.link.isLive, "disconnected")
        s.addItem(n, "added while offline")
        s.toggleComplete(n)
        check(s.pending.count == 1 && s.items[n]?.isOpen == false, "offline edits wait, folded into one, and show at once")
        s.channel.start()
        check(spin(90) { s.link.isLive && s.pending.isEmpty }, "offline edits sent on reconnect")
        check(s.server[n]?.state == "CLOSED" && s.server[n]?.body.contains("- [ ] added while offline") == true, "offline edits on GitHub")

        var preview: String?
        s.assignPreview(n, text: "self-test, do not start anything") { m, err in preview = m ?? "error " + (err ?? "") }
        check(spin(30) { preview != nil } && preview?.contains("Task #\(n):") == true, "assign preview built (nothing launched)")

        s.deleteTask(n)
        check(spin(60) { s.pending.isEmpty }, "delete reached GitHub")
        let after = s.server[n]
        check(after?.state == "CLOSED" && after?.labels.contains(where: { $0.hasPrefix("list:") }) == false, "deleted: closed, no list")
        s.channel.stop()
        print(fails.isEmpty ? "remote selftest OK (#\(n))" : "remote selftest FAILED")
        return fails.isEmpty ? 0 : 1
    }
}

/// Assign to agent, end to end, on a harmless dummy task: the same Store.assign the button calls. It starts a real
/// (tiny) agent through the orchestrator, which emails the owner as it always does.
@MainActor
enum AssignTest {
    static func run() -> Int32 {
        let s = Store(persist: false)
        s.connect()
        guard spin(200, until: { s.hasLoaded && s.link.isLive }) else { print("could not connect: \(s.link)"); return 1 }
        let title = "Agent test from the Mac to-do app \(isoString(Date())) (safe to ignore)"
        s.addTask(title: title, notes: """
            A harmless test of the Mac app's Assign to agent button. The agent should change nothing: no files, no \
            branches, no PRs. It only reports back on this issue as its brief says (a done comment, one line under \
            Claude Resolution Notes, the status:claude-processed label), then closes itself with agent-close --self.
            """, list: "tasks")
        guard spin(60, until: { s.pending.isEmpty }),
              let n = s.items.values.first(where: { $0.title == title && $0.number > 0 })?.number else {
            print("FAIL could not create the dummy task"); return 1
        }
        print("created #\(n)")
        var result: String??
        s.assign(n, text: "Only a test of the assign flow. Start it in the general repo this message names, with --effort low. No email is needed from the agent itself; it should report on the issue and then run agent-close --self.", again: false) { err in result = .some(err) }
        func cleanUp() {                                    // a failed test leaves no dummy task behind
            s.deleteTask(n)
            _ = spin(60, until: { s.pending.isEmpty })
            print("deleted the dummy task #\(n)")
        }
        guard spin(150, until: { result != nil }) else { print("FAIL assign got no answer"); cleanUp(); return 1 }
        if case .some(.some(let e)) = result { print("FAIL assign refused: \(e)"); cleanUp(); return 1 }
        print("assigned #\(n): labels \(s.server[n]?.labels ?? []), agent \(s.server[n]?.agent?.status ?? "-")")
        var again: String??
        s.assign(n, text: "", again: false) { err in again = .some(err) }
        _ = spin(60, until: { again != nil })
        print("second press without Assign again: " + ((again ?? nil).map { "refused (\($0))" } ?? "ACCEPTED (bad)"))
        s.channel.stop()
        return 0
    }
}

/// Clicks the middle of a checklist box (not its border) in an offscreen window, through AppKit's event path,
/// and checks the item gets ticked: it guards against a box that only took clicks on its 1.4 pt outline.
@MainActor
enum ClickTest {
    static func run() -> Int32 {
        let issue = Issue(number: 5, title: "Click test", body: "## Checklist\n- [ ] first\n- [ ] second", state: "OPEN", url: "",
                          createdAt: "2026-10-01T00:00:00Z", updatedAt: "2026-10-02T00:00:00Z", labels: ["list:tasks"], agent: nil)
        let s = Store(persist: false)
        s.install(issues: [issue], config: nil)
        s.link = .down("test")
        let t = s.items[5]!
        let view = ChecklistRow(item: t.checklist[0], task: t, store: s).frame(width: 360, height: 30, alignment: .leading)
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(x: 0, y: 0, width: 360, height: 30)
        let win = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: 360, height: 30), styleMask: [.titled],
                           backing: .buffered, defer: false)
        win.contentView = host
        win.orderFront(nil)
        host.layoutSubtreeIfNeeded()
        RunLoop.current.run(until: Date().addingTimeInterval(0.4))
        // the box is 15 pt inside a 24 pt target starting 4 pt left of the row: its centre is about x = 8
        let point = NSPoint(x: 8, y: 15)
        for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
            if let e = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                          windowNumber: win.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1) {
                win.sendEvent(e)
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.15))
        }
        RunLoop.current.run(until: Date().addingTimeInterval(0.3))
        let ok = s.items[5]?.checklist.first?.checked == true
        print(ok ? "click OK: a click in the middle of the box ticks the item" : "click FAILED: the item is still unticked")
        return ok ? 0 : 1
    }
}
