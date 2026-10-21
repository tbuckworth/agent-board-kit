import AppKit
import Combine
import SwiftUI

enum Route: Hashable {
    case list(String)
    case agents
    case search
}

struct Filters: Equatable {
    var section: String?
    var priority: String?
    /// Tags: every "only" tag must be on a task, and no "hide" tag may be. Kept across launches.
    var tags: [String: TagMode] = [:]

    var active: Bool { section != nil || priority != nil || !tags.isEmpty }

    func accepts(_ t: TaskItem) -> Bool {
        if let s = section, t.section?.lowercased() != s { return false }
        if let p = priority, t.priority != p { return false }
        return acceptsTags(t)
    }

    func acceptsTags(_ t: TaskItem) -> Bool {
        for (tag, mode) in tags {
            if (mode == .only) != t.tags.contains(tag) { return false }
        }
        return true
    }

    /// Click a tag's chip: neutral → only → hide → neutral.
    mutating func cycle(_ tag: String) {
        tags[tag] = TagMode.next(tags[tag])
    }
}

/// The waiting fields of the edit pane: on or off, who, and the follow-up day (`YYYY-MM-DD`) if any.
struct WaitingEdit: Equatable {
    var on: Bool
    var who: String = ""
    var followUp: String?
}

struct Toast: Identifiable {
    let id = UUID()
    var text: String
    var isError = false
    var undo: (() -> Void)?
}

/// One task in an undoable action: the task as it was before and just after it (before nil: it was created).
struct UndoPart {
    var number: Int
    var before: Issue?
    var after: Issue
}

/// One undoable action, on one task or several (a group and its tasks, a selection).
struct UndoEntry {
    var what: String
    var parts: [UndoPart]

    init(what: String, parts: [UndoPart]) { self.what = what; self.parts = parts }
    init(what: String, number: Int, before: Issue?, after: Issue) {
        self.init(what: what, parts: [UndoPart(number: number, before: before, after: after)])
    }
}

/// A top-level place in the sidebar: a group, or a list that is in no group.
enum TopRef: Equatable {
    case group(String)
    case list(String)
}

struct AssignRequest: Identifiable {
    let id = UUID()
    var number: Int
    var others: [Int] = []            // several selected: one agent, on `number`, handles these as well
}

enum FocusTarget { case add, search, waiting }

struct ListCount: Hashable {
    var name: String
    var open: Int
}

enum SidebarItem: Identifiable {
    case list(name: String, open: Int)
    case group(name: String, open: Int, lists: [ListCount])

    var id: String {
        switch self {
        case .list(let n, _): return "l:" + n
        case .group(let n, _, _): return "g:" + n
        }
    }
}

/// Everything the window shows. GitHub's copy of each task (`server`) plus the edits still waiting to reach it
/// (`pending`) make `items`, which is what the views read. Waiting edits survive quitting and being offline.
@MainActor
final class Store: ObservableObject {
    @Published private(set) var server: [Int: Issue] = [:]
    @Published private(set) var pending: [PendingOp] = []
    @Published private(set) var serverConfig: TodoConfig?
    @Published private(set) var items: [Int: TaskItem] = [:]
    @Published private(set) var config: TodoConfig?
    @Published private(set) var labelNames: [String] = []
    @Published var syncedAt: String?          // the desktop's sync time of the newest data this Mac has
    @Published var deskSyncedAt: String?      // when the desktop last checked GitHub (from its pings)
    @Published var link: LinkState = .down("not connected yet")
    @Published var lastContact: Date?
    @Published var syncError: String?
    @Published var toast: Toast?
    @Published private(set) var undoStack: [UndoEntry] = []

    @Published var route: Route = .list("tasks") {
        didSet {
            if route != oldValue { marked = [] }
            guard case .list(let l) = route else { return }
            Store.defaults.set(l, forKey: "list")
            if let g = config?.group(of: l), !expanded.contains(g) { expanded.insert(g) }   // show where it is
        }
    }
    @Published var selection: Int?
    /// Tasks picked with ⇧- or ⌘-click (or ⌘A) to act on together; empty when only `selection` is.
    @Published var marked: Set<Int> = []
    /// Groups of tasks folded away in the list (remembered across launches)
    @Published var folded: Set<Int> { didSet { Store.defaults.set(Array(folded), forKey: "foldedTaskGroups") } }
    @Published var filters = Filters() {
        didSet {
            guard persist, filters.tags != oldValue.tags else { return }
            Store.defaults.set(filters.tags.mapValues { $0.rawValue }, forKey: "tagFilter")
        }
    }
    @Published var query = ""
    @Published var showCompleted = false
    @Published var editing: Int?
    @Published var assigning: AssignRequest?
    @Published var focusRequest: FocusTarget?
    @Published var zoom: CGFloat
    @Published var expanded: Set<String> { didSet { Store.defaults.set(Array(expanded), forKey: "expandedGroups") } }
    @Published var updateAvailable = false
    @Published var now = Date()
    @Published var hasLoaded = false
    var previewStub: String?          // the offscreen renderer's stand-in for the orchestrator message
    var agentsStub: [BoardAgent]?     // ...and for the board's running agents

    static let defaults = UserDefaults.standard
    let channel = DeskLink()
    let persist: Bool
    private var inFlight: UUID?
    private var retryAt: [UUID: Date] = [:]
    private var nextLocal = -1
    private var saveWork: DispatchWorkItem?
    private var timers: [Timer] = []

    init(persist: Bool = true) {
        self.persist = persist
        let z = Store.defaults.double(forKey: "zoom")
        zoom = z > 0 ? CGFloat(z) : 1
        expanded = Set(Store.defaults.stringArray(forKey: "expandedGroups") ?? [])
        folded = Set((Store.defaults.array(forKey: "foldedTaskGroups") as? [Int]) ?? [])
        if let l = Store.defaults.string(forKey: "list") { route = .list(l) }
        if persist {
            let saved = (Store.defaults.dictionary(forKey: "tagFilter") as? [String: String]) ?? [:]
            filters.tags = saved.compactMapValues { TagMode(rawValue: $0) }
            load()
        }
    }

    // MARK: cache file

    nonisolated static var cacheURL: URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("ToDo")
        return dir.appendingPathComponent("cache.json")
    }

    struct CacheFile: Codable {
        var issues: [Issue]
        var config: TodoConfig?
        var syncedAt: String?
        var pending: [PendingOp]
        var labels: [String]?
    }

    func load(from url: URL = Store.cacheURL) {
        guard let data = try? Data(contentsOf: url), let c = try? JSONDecoder().decode(CacheFile.self, from: data) else { return }
        server = Dictionary(c.issues.map { ($0.number, $0) }, uniquingKeysWith: { a, _ in a })
        serverConfig = c.config
        syncedAt = c.syncedAt
        pending = c.pending
        labelNames = c.labels ?? []
        nextLocal = min(-1, (pending.map { $0.number }.min() ?? 0) - 1)
        hasLoaded = !server.isEmpty
        rebuildAll()
    }

    func scheduleSave() {
        guard persist else { return }
        saveWork?.cancel()
        let c = CacheFile(issues: Array(server.values), config: serverConfig, syncedAt: syncedAt, pending: pending, labels: labelNames)
        let url = Store.cacheURL
        let w = DispatchWorkItem {
            guard let data = try? JSONEncoder().encode(c) else { return }
            try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? data.write(to: url, options: .atomic)
        }
        saveWork = w
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 0.8, execute: w)
    }

    // MARK: derived

    private func rebuildAll() {
        config = Overlay.config(serverConfig, pending)
        var out: [Int: TaskItem] = [:]
        var numbers = Set(server.keys)
        for op in pending where op.number < 0 { numbers.insert(op.number) }
        for n in numbers { if let t = makeItem(n) { out[n] = t } }
        items = out
    }

    private func rebuild(_ numbers: [Int]) {
        if pending.contains(where: { $0.kind == .deleteList || $0.kind == .config }) || numbers.isEmpty {
            rebuildAll()
            return
        }
        for n in numbers { items[n] = makeItem(n) }
    }

    private func makeItem(_ n: Int) -> TaskItem? {
        let ops = pending.filter { $0.number == n || $0.kind == .deleteList }
        guard let issue = Overlay.apply(server[n], ops, number: n, now: isoString(Date())) else { return nil }
        var t = TaskItem(issue, config: config)
        let mine = ops.filter { $0.number == n }
        t.pending = !mine.isEmpty
        t.conflict = mine.first(where: { $0.conflict != nil })?.conflict
        t.failed = mine.first(where: { $0.conflict == nil && $0.attempts >= PendingOp.maxAttempts })?.error
        return t
    }

    /// A deleted task is closed with no list (the web app deletes that way); it is hidden everywhere.
    func isDeleted(_ t: TaskItem) -> Bool { !t.isOpen && Labels.value(t.issue.labels, "list:") == nil }

    var live: [TaskItem] { items.values.filter { !isDeleted($0) } }

    func open(in list: String) -> [TaskItem] {
        Store.waitingLast(live.filter { $0.isOpen && $0.list == list && filters.accepts($0) }, today: today)
    }

    /// Today as `YYYY-MM-DD`, for follow-up days.
    var today: String { Waiting.day(now) }

    /// Newest first, with tasks an agent thinks came through on top, and tasks waiting on someone (until their
    /// follow-up day) after the rest.
    static func waitingLast(_ tasks: [TaskItem], today: String) -> [TaskItem] {
        func rank(_ t: TaskItem) -> Int { t.cameThrough ? 0 : t.greyed(today: today) ? 2 : 1 }
        return tasks.sorted { a, b in
            let ra = rank(a), rb = rank(b)
            return ra != rb ? ra < rb : newest(a, b)
        }
    }

    /// Every tag on a task or in the repo's labels, for the tag menus.
    var allTags: [String] {
        Array(Set(live.flatMap { $0.tags } + Tags.of(labelNames))).sorted()
    }

    /// The tag chips over a list: tags on the tasks in it, plus any in the filter.
    func tagsShown(in list: String) -> [String] {
        Array(Set(live.filter { $0.isOpen && $0.list == list }.flatMap { $0.tags } + filters.tags.keys)).sorted()
    }

    func completed(in list: String) -> [TaskItem] {
        live.filter { !$0.isOpen && $0.list == list }.sorted(by: Store.newest)
    }

    var agentTasks: [TaskItem] {
        live.filter { $0.assigned && $0.isOpen }.sorted(by: Store.newest)
    }

    func searchResults() -> [TaskItem] {
        let words = query.lowercased().split(separator: " ").map(String.init).filter { !$0.isEmpty }
        guard !words.isEmpty else { return [] }
        return live.filter { $0.matches(words: words) && ($0.isOpen || showCompleted) && filters.acceptsTags($0) }
            .sorted { ($0.isOpen ? 1 : 0, $0.issue.updatedAt) > ($1.isOpen ? 1 : 0, $1.issue.updatedAt) }
    }

    static func newest(_ a: TaskItem, _ b: TaskItem) -> Bool { a.issue.updatedAt > b.issue.updatedAt }

    /// A list's open tasks as shown: groups with their tasks underneath (unless folded)
    func groupedRows(_ tasks: [TaskItem]) -> [RowEntry] { Grouping.rows(tasks, all: live, folded: folded) }

    func isGroup(_ n: Int) -> Bool { live.contains { $0.parent == n } }

    func toggleFold(_ n: Int) {
        if folded.contains(n) { folded.remove(n) } else { folded.insert(n) }
    }

    /// The rows of the current view, in order: what ↑ and ↓ move through.
    var rows: [Int] {
        switch route {
        case .list(let l): return groupedRows(open(in: l)).map { $0.task.number } + (showCompleted ? completed(in: l).map { $0.number } : [])
        case .agents: return agentTasks.map { $0.number }
        case .search: return searchResults().map { $0.number }
        }
    }

    var allLists: [String] {
        var s = Set(live.map { $0.list })
        let configNames = (config?.listMetadata ?? []).map { $0.listName.lowercased() }
        for n in configNames where listSlug(n) == n || !(s.contains(listSlug(n)) || configNames.contains(listSlug(n))) {
            s.insert(n)                                     // "To Sell" is dropped when "to-sell" exists: a stale spelling
        }
        return s.sorted()
    }

    var groupNames: [String] { (config?.groups ?? []).sorted { ($0.sortOrder ?? 0, $0.name) < ($1.sortOrder ?? 0, $1.name) }.map { $0.name } }

    var sectionsInUse: [String] { Array(Set(live.compactMap { $0.section?.lowercased() })).sorted() }

    func openCounts() -> [String: Int] {
        var c: [String: Int] = [:]
        for t in live where t.isOpen && filters.accepts(t) { c[t.list, default: 0] += 1 }
        return c
    }

    /// Lists and groups in the config's order, as the web app's home and sidebar show them.
    var sidebar: [SidebarItem] {
        let counts = openCounts()
        let cfg = config ?? TodoConfig()
        var grouped: [String: [(String, Int, Int)]] = [:]
        var loose: [(String, Int, Int)] = []
        for l in allLists {
            let meta = cfg.meta(l)
            let entry = (l, counts[l] ?? 0, meta?.sortOrder ?? 0)
            if let g = meta?.groupName { grouped[g, default: []].append(entry) } else { loose.append(entry) }
        }
        for g in cfg.groups where grouped[g.name] == nil { grouped[g.name] = [] }
        var top: [(Int, String, SidebarItem)] = loose.map { ($0.2, $0.0, .list(name: $0.0, open: $0.1)) }
        for (g, ls) in grouped {
            let order = cfg.groups.first { $0.name == g }?.sortOrder ?? 0
            let lists = ls.sorted { ($0.2, $0.0) < ($1.2, $1.0) }.map { ListCount(name: $0.0, open: $0.1) }
            top.append((order, g, .group(name: g, open: lists.reduce(0) { $0 + $1.open }, lists: lists)))
        }
        return top.sorted { ($0.0, $0.1) < ($1.0, $1.1) }.map { $0.2 }
    }

    var selected: TaskItem? { selection.flatMap { items[$0] } }

    var conflicts: [PendingOp] { pending.filter { $0.conflict != nil } }
    var stuck: [PendingOp] { pending.filter { $0.conflict == nil && $0.attempts >= PendingOp.maxAttempts } }
    var activeAgents: Int { live.filter { $0.agentActive }.count }

    // MARK: the link

    func connect() {
        channel.onState = { [weak self] s in self?.link = s }
        channel.onConnect = { [weak self] in self?.hello() }
        channel.onEvent = { [weak self] e in self?.handle(event: e) }
        channel.start()
        timers.append(Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.now = Date() }
        })
        timers.append(Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.pump() }
        })
    }

    func reconnect() { channel.start() }

    private func hello() {
        lastContact = Date()
        let since: Any = syncedAt.map { $0 as Any } ?? NSNull()
        channel.request(["cmd": "hello", "since": since], timeout: 180) { [weak self] r in
            guard let self = self else { return }
            switch r {
            case .success(let obj):
                self.syncError = nil
                self.absorb(obj, full: obj["full"] as? Bool ?? false)
                self.hasLoaded = true
                self.pump()
            case .failure(let e):
                self.syncError = e.message
            }
        }
    }

    private func absorb(_ obj: [String: Any], full: Bool) {
        let issues = decodeIssues(obj["issues"])
        if full { server = [:] }
        for i in issues { server[i.number] = i }
        for n in (obj["removed"] as? [Int]) ?? [] { server.removeValue(forKey: n) }
        // Every issue the desktop has: anything else was deleted on GitHub, however long ago
        if let known = obj["known"] as? [Int], !known.isEmpty {
            let keep = Set(known)
            server = server.filter { keep.contains($0.key) }
        }
        if let c = decodeConfig(obj["config"]) { serverConfig = c } else if obj["config"] is NSNull { serverConfig = nil }
        if let l = obj["labels"] as? [String] { labelNames = l }
        if let s = obj["synced_at"] as? String { syncedAt = s; deskSyncedAt = s }
        rebuildAll()
        scheduleSave()
    }

    private func handle(event e: [String: Any]) {
        lastContact = Date()
        switch e["type"] as? String {
        case "issues":
            let issues = decodeIssues(e["issues"])
            for i in issues { server[i.number] = i }
            let removed = (e["removed"] as? [Int]) ?? []
            for n in removed { server.removeValue(forKey: n) }
            if let s = e["synced_at"] as? String { syncedAt = s; deskSyncedAt = s }
            syncError = nil
            rebuild(issues.map { $0.number } + removed)
            scheduleSave()
        case "config":
            if let c = decodeConfig(e["config"]) { serverConfig = c } else if e["config"] is NSNull { serverConfig = nil } else { break }
            rebuildAll()                                    // null: the config file was deleted on GitHub
            scheduleSave()
        case "ping", "ready":
            if let s = e["synced_at"] as? String { deskSyncedAt = s }   // display only: never the cursor for "since"
        case "error":
            syncError = e["error"] as? String
        default:
            break
        }
    }

    func refresh(full: Bool = false) {
        guard link.isLive else { reconnect(); return }
        show(full ? "Fetching every task from GitHub…" : "Checking GitHub…")
        channel.request(["cmd": "refresh", "full": full], timeout: 180) { [weak self] r in
            switch r {
            case .success(let o):
                let n = o["changed"] as? Int ?? 0
                self?.show(n == 0 ? "Up to date" : "\(n) task\(n == 1 ? "" : "s") updated")
            case .failure(let e): self?.show("Refresh failed: \(e.message)", error: true)
            }
        }
    }

    // MARK: the queue

    private func enqueue(_ op: PendingOp) {
        var op = op
        if op.kind == .setParent {
            // Only the last group wanted counts: an unsent move into a group is replaced, not sent twice
            pending.removeAll { $0.kind == .setParent && $0.number == op.number && $0.id != inFlight && !$0.sent }
            pending.append(op)
        } else if op.kind == .update,
           let j = pending.lastIndex(where: { $0.number == op.number && ($0.kind == .update || $0.kind == .create) }),
           pending[j].id != inFlight, !pending[j].blocked {
            if pending[j].kind == .create, op.set["state"] == nil, !pending[j].sent {
                pending[j].absorbIntoCreate(op)
                op = pending[j]
            } else if pending[j].kind == .update, !(pending[j].sent && pending[j].number < 0) {
                pending[j].absorb(op)
                op = pending[j]
            } else {
                pending.append(op)
            }
        } else {
            pending.append(op)
        }
        rebuild([op.number])
        scheduleSave()
        pump()
    }

    func pump() {
        guard link.isLive, inFlight == nil else { return }
        let nowD = Date()
        var blockedNumbers = Set<Int>()
        var next: PendingOp?
        for op in pending {
            if op.blocked { blockedNumbers.insert(op.number); continue }
            if op.kind == .update && (op.number < 0 || blockedNumbers.contains(op.number)) { continue }
            // a move into a group waits until both the task and the group are on GitHub
            if op.kind == .setParent && (op.number < 0 || (op.parent ?? 0) < 0 || blockedNumbers.contains(op.number)) { continue }
            if let t = retryAt[op.id], t > nowD { blockedNumbers.insert(op.number); continue }
            next = op
            break
        }
        guard let op = next else { return }
        inFlight = op.id
        if let k = pending.firstIndex(where: { $0.id == op.id }) { pending[k].sent = true }
        channel.request(op.request, timeout: 120) { [weak self] r in self?.finished(op, r) }
    }

    private func finished(_ op: PendingOp, _ r: Result<[String: Any], LinkError>) {
        inFlight = nil
        guard let i = pending.firstIndex(where: { $0.id == op.id }) else { pump(); return }
        switch r {
        case .success(let obj):
            pending.remove(at: i)
            retryAt.removeValue(forKey: op.id)
            if let issue = decodeIssue(obj["issue"]) {
                server[issue.number] = issue
                if op.kind == .create { remap(op.number, to: issue.number) }
            }
            for issue in decodeIssues(obj["issues"]) { server[issue.number] = issue }
            if let c = decodeConfig(obj["config"]) { serverConfig = c }
            rebuildAll()
        case .failure(let e):
            if e.offline {
                return                                      // waits for the desktop; the 5 s timer or hello retries it
            } else if e.reply?["conflict"] as? Bool == true {
                pending[i].conflict = (e.reply?["fields"] as? [String]) ?? ["task"]
                if let cur = decodeIssue(e.reply?["issue"]) { server[cur.number] = cur }
                rebuildAll()
                show("“\(items[op.number]?.title ?? "A task")” changed elsewhere too. Pick which version to keep.", error: true)
            } else {
                pending[i].attempts += 1
                pending[i].error = e.message
                retryAt[op.id] = Date().addingTimeInterval(Double(5 * pending[i].attempts))
                if pending[i].attempts >= PendingOp.maxAttempts {
                    show("Couldn't save: \(e.message)", error: true)
                }
                rebuild([op.number])
            }
        }
        scheduleSave()
        pump()
    }

    private func remap(_ local: Int, to real: Int) {
        for k in pending.indices where pending[k].number == local { pending[k].number = real }
        for k in pending.indices where pending[k].parent == local { pending[k].parent = real }
        for k in undoStack.indices {
            for j in undoStack[k].parts.indices where undoStack[k].parts[j].number == local { undoStack[k].parts[j].number = real }
        }
        if folded.contains(local) { folded.remove(local); folded.insert(real) }
        if marked.contains(local) { marked.remove(local); marked.insert(real) }
        if selection == local { selection = real }
        if editing == local { editing = real }
        items.removeValue(forKey: local)
    }

    func keepMine(_ number: Int) {
        for k in pending.indices where pending[k].number == number && pending[k].conflict != nil {
            pending[k].conflict = nil
            pending[k].force = true
            pending[k].attempts = 0
        }
        rebuild([number]); scheduleSave(); pump()
    }

    func keepTheirs(_ number: Int) {
        pending.removeAll { $0.number == number && $0.id != inFlight }
        rebuild([number]); scheduleSave()
    }

    func retry(_ number: Int) {
        for k in pending.indices where pending[k].number == number {
            pending[k].attempts = 0
            retryAt.removeValue(forKey: pending[k].id)
        }
        rebuild([number]); scheduleSave(); pump()
    }

    func discardPending(_ number: Int) {
        pending.removeAll { $0.number == number && $0.id != inFlight }
        // a group that never reached GitHub: moves into it would wait for ever
        if number < 0 { pending.removeAll { $0.kind == .setParent && $0.parent == number && $0.id != inFlight } }
        if number < 0 { items.removeValue(forKey: number); if selection == number { selection = nil } }
        rebuildAll(); scheduleSave()
    }

    // MARK: edits

    @discardableResult
    func addTask(title: String, notes: String = "", list: String, checklist: [String] = []) -> Int? {
        let t = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return nil }
        let l = listSlug(list).isEmpty ? "tasks" : listSlug(list)
        var labels = ["list:" + l, "source:app"]
        if let g = config?.group(of: l) { labels.append("group:" + g) }
        let n = nextLocal
        nextLocal -= 1
        enqueue(PendingOp(kind: .create, number: n, title: t, body: IssueBody.build(notes: notes, checklist: checklist),
                          labels: labels, what: "Add “\(t)”"))
        if let after = items[n]?.issue { pushUndo(UndoEntry(what: "adding “\(t)”", number: n, before: nil, after: after)) }
        return n
    }

    func update(_ number: Int, set: [String: String] = [:], add: [String] = [], remove: [String] = [], what: String,
                base given: [String: String]? = nil) {
        guard let t = items[number] else { return }
        var base: [String: String] = given ?? [:]
        if set["title"] != nil, base["title"] == nil { base["title"] = t.issue.title }
        if set["body"] != nil, base["body"] == nil { base["body"] = t.issue.body }
        enqueue(PendingOp(kind: .update, number: number, set: set, base: base, add: add, remove: remove, what: what))
    }

    func toggleComplete(_ number: Int) {
        guard let t = items[number] else { return }
        let closing = t.isOpen
        if closing, !Grouping.openDescendants(number, all: live).isEmpty {
            if selection == number, case .list = route, !showCompleted { selectNeighbour(of: number) }
            completeMany([number])                          // a group: its tasks are ticked too
            return
        }
        undoable(number, (closing ? "completing" : "reopening") + " “\(t.title)”") {
            update(number, set: ["state": closing ? "CLOSED" : "OPEN"], what: (closing ? "Complete" : "Reopen") + " “\(t.title)”")
        }
        show(closing ? "Completed “\(t.title)”" : "Reopened “\(t.title)”", undo: { [weak self] in self?.undo() })
        if closing, selection == number, case .list = route, !showCompleted { selectNeighbour(of: number) }
    }

    func deleteTask(_ number: Int, recordUndo: Bool = true) {
        guard let t = items[number] else { return }
        if !recordUndo {
            let lists = t.issue.labels.filter { $0.lowercased().hasPrefix("list:") }
            if number < 0 { discardPending(number); return }
            update(number, set: ["state": "CLOSED"], remove: lists, what: "Delete “\(t.title)”")
            return
        }
        let lists = t.issue.labels.filter { $0.lowercased().hasPrefix("list:") }
        if number < 0 { discardPending(number); return }
        if selection == number { selectNeighbour(of: number) }
        if !Grouping.openDescendants(number, all: live).isEmpty { deleteMany([number]); return }   // with its tasks
        undoable(number, "deleting “\(t.title)”") {
            update(number, set: ["state": "CLOSED"], remove: lists, what: "Delete “\(t.title)”")
        }
        show("Deleted “\(t.title)”", undo: { [weak self] in self?.undo() })
    }

    func toggleItem(_ number: Int, _ item: ChecklistItem) {
        guard let t = items[number] else { return }
        undoable(number, (item.checked ? "unticking" : "ticking") + " “\(item.text)”") {
            update(number, set: ["body": IssueBody.toggling(t.issue.body, item)], what: "Tick “\(item.text)”")
        }
    }

    func removeItem(_ number: Int, _ item: ChecklistItem) {
        guard let t = items[number] else { return }
        undoable(number, "removing “\(item.text)”") {
            update(number, set: ["body": IssueBody.removing(t.issue.body, item)], what: "Remove “\(item.text)”")
        }
    }

    func addItem(_ number: Int, _ text: String) {
        guard let t = items[number], !text.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        undoable(number, "adding “\(text)”") {
            update(number, set: ["body": IssueBody.adding(t.issue.body, item: text)], what: "Add “\(text)”")
        }
    }

    func editItem(_ number: Int, _ item: ChecklistItem, to text: String) {
        let new = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let t = items[number], !new.isEmpty, new != item.text else { return }
        undoable(number, "editing “\(item.text)”") {
            update(number, set: ["body": IssueBody.editing(t.issue.body, item, to: new)], what: "Edit “\(new)”")
        }
    }

    /// Save the edit pane. `tags` and `waiting` are left as they are when nil.
    func saveEdit(_ number: Int, title: String, notes: String, list: String, section: String?, priority: String,
                  tags: [String]? = nil, waiting: WaitingEdit? = nil) {
        guard let t = items[number] else { return }
        var set: [String: String] = [:]
        let newTitle = title.trimmingCharacters(in: .whitespacesAndNewlines)
        if !newTitle.isEmpty, newTitle != t.title { set["title"] = newTitle }
        // The "Waiting on:" line belongs to the waiting fields, not the notes: it is put back first
        let who = waiting.map { $0.on ? $0.who : "" } ?? t.waitingOn
        let whoChanged = (who ?? "").trimmingCharacters(in: .whitespaces) != (t.waitingOn ?? "")
        let body = Waiting.withWho(IssueBody.withNotes(t.issue.body, notes), who)
        if body != t.issue.body, notes.trimmingCharacters(in: .whitespacesAndNewlines) != t.notes || whoChanged { set["body"] = body }
        var diff = labelDiff(from: t, list: list, section: section, priority: priority, config: config)
        if let tags = tags {
            let d = tagDiff(from: t, to: tags)
            diff.add += d.add
            diff.remove += d.remove
        }
        if let w = waiting {
            let d = waitingDiff(from: t, waiting: w.on, followUp: w.followUp)
            diff.add += d.add
            diff.remove += d.remove
        }
        if set.isEmpty && diff.add.isEmpty && diff.remove.isEmpty { return }
        let newList = listSlug(list)
        if !newList.isEmpty, config?.meta(newList) == nil, !allLists.contains(newList) {
            enqueue(PendingOp(kind: .config, configOps: [ConfigOp(op: "add_list", list: newList)], what: "New list \(newList)"))
        }
        undoable(number, "editing “\(t.title)”") {
            update(number, set: set, add: diff.add, remove: diff.remove, what: "Edit “\(newTitle.isEmpty ? t.title : newTitle)”")
        }
    }

    // MARK: tags and waiting

    /// Add or take off one tag, on one task or several. A new tag's label is made on GitHub by the desktop.
    func tagMany(_ numbers: [Int], _ tag: String, on: Bool) {
        let name = Tags.normalise(tag)
        guard !name.isEmpty else { return }
        let targets = numbers.filter { items[$0].map { $0.tags.contains(name) != on } ?? false }
        guard !targets.isEmpty else { return }
        undoableMany(targets, (on ? "tagging " : "untagging ") + label(numbers)) {
            for n in targets {
                guard let t = items[n] else { continue }
                let d = tagDiff(from: t, to: on ? t.tags + [name] : t.tags.filter { $0 != name })
                update(n, add: d.add, remove: d.remove, what: (on ? "Tag “" : "Untag “") + t.title + "” " + name)
            }
            return []
        }
        show((on ? "Tagged " : "Took \(name) off ") + label(numbers) + (on ? " \(name)" : ""), undo: { [weak self] in self?.undo() })
    }

    /// Waiting on someone, or not any more. On keeps who and the follow-up day as they are; off takes them away
    /// (the follow-up label and the "Waiting on:" line).
    func waitMany(_ numbers: [Int], on: Bool) {
        let targets = numbers.filter { items[$0].map { $0.waiting != on && $0.isOpen } ?? false }
        guard !targets.isEmpty else { return }
        undoableMany(targets, (on ? "marking " : "unmarking ") + label(numbers) + " waiting") {
            for n in targets {
                guard let t = items[n] else { continue }
                let d = waitingDiff(from: t, waiting: on, followUp: t.followUp)
                var set: [String: String] = [:]
                if !on, t.waitingOn != nil { set["body"] = Waiting.withWho(t.issue.body, nil) }
                update(n, set: set, add: d.add, remove: d.remove, what: (on ? "Waiting: “" : "Not waiting: “") + t.title + "”")
            }
            return []
        }
        show(on ? "Waiting on someone: \(label(numbers))" : "No longer waiting: \(label(numbers))", undo: { [weak self] in self?.undo() })
    }

    /// An agent thought the wait was over. Still waiting: back to waiting, who and the follow-up as they were.
    /// OK: done waiting, so who and the follow-up go too.
    func answerCleared(_ number: Int, stillWaiting: Bool) {
        guard let t = items[number], t.cleared else { return }
        let cleared = t.issue.labels.filter { $0.lowercased() == Waiting.cleared }
        undoable(number, stillWaiting ? "still waiting on “\(t.title)”" : "clearing the wait on “\(t.title)”") {
            if stillWaiting {
                update(number, add: t.waiting ? [] : [Waiting.label], remove: cleared, what: "Still waiting: “\(t.title)”")
            } else {
                let follow = t.issue.labels.filter { $0.lowercased().hasPrefix(Waiting.followUpPrefix) || $0.lowercased() == Waiting.label }
                let set: [String: String] = t.waitingOn == nil ? [:] : ["body": Waiting.withWho(t.issue.body, nil)]
                update(number, set: set, remove: cleared + follow, what: "Done waiting: “\(t.title)”")
            }
        }
        show(stillWaiting ? "Still waiting: “\(t.title)”" : "Done waiting: “\(t.title)”", undo: { [weak self] in self?.undo() })
    }

    func newList(_ name: String, group: String?) {
        let l = listSlug(name)
        guard !l.isEmpty else { return }
        var ops = [ConfigOp(op: "add_list", list: l)]
        if let g = group, !g.isEmpty { ops.append(ConfigOp(op: "set_group", list: l, group: g)) }
        enqueue(PendingOp(kind: .config, configOps: ops, what: "New list \(l)"))
        route = .list(l)
    }

    func setGroup(_ list: String, _ group: String?) {
        enqueue(PendingOp(kind: .config, configOps: [ConfigOp(op: "set_group", list: list, group: group)],
                          what: "Move \(list) to \(group ?? "no group")"))
    }

    func newGroup(_ name: String) {
        let g = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !g.isEmpty else { return }
        enqueue(PendingOp(kind: .config, configOps: [ConfigOp(op: "add_group", group: g)], what: "New group \(g)"))
        expanded.insert(g)
    }

    func deleteList(_ name: String) {
        guard name != "tasks" else { return }
        enqueue(PendingOp(kind: .deleteList, list: name, what: "Delete list \(name)"))
        route = .list("tasks")
        show("Deleted list \(titleCase(name)); its tasks moved to Tasks")
    }

    /// A to-do dragged onto a list in the sidebar: its list (and group) label changes, nothing else.
    func moveTask(_ number: Int, toList list: String) {
        guard let t = items[number], listSlug(list) != t.list else { return }
        if selection == number, case .list(let here) = route, here != listSlug(list) { selectNeighbour(of: number) }
        if marked.contains(number) || !Grouping.openDescendants(number, all: live).isEmpty {
            moveMany(marked.contains(number) ? chosen : [number], toList: list)   // the selection, or a group with its tasks
            return
        }
        let diff = labelDiff(from: t, list: list, section: t.section, priority: t.priority, config: config)
        undoable(number, "moving “\(t.title)” to \(titleCase(list))") {
            update(number, add: diff.add, remove: diff.remove, what: "Move “\(t.title)” to \(titleCase(list))")
        }
        show("Moved “\(t.title)” to \(titleCase(list))", undo: { [weak self] in self?.undo() })
    }

    // MARK: several tasks at once, and groups of tasks (the web app's bulk.js)

    /// The tasks an action applies to: those picked (in the order shown), else the selected one
    var chosen: [Int] {
        if marked.isEmpty { return selection.map { [$0] } ?? [] }
        let r = rows
        return r.filter { marked.contains($0) } + marked.filter { !r.contains($0) }.sorted()
    }

    /// A click on a row. ⇧: everything from the selected task to this one; ⌘: add or remove this one; plain:
    /// select only this one.
    func click(_ n: Int, shift: Bool, command: Bool) {
        if shift, let anchor = selection, anchor != n {
            let r = rows
            if let a = r.firstIndex(of: anchor), let b = r.firstIndex(of: n) {
                marked.formUnion(r[min(a, b)...max(a, b)])
                return
            }
        }
        if command || shift {
            if marked.isEmpty, let s = selection { marked.insert(s) }
            if marked.contains(n) { marked.remove(n) } else { marked.insert(n) }
            if selection == nil || command { selection = n }
            return
        }
        marked = []
        selection = n
    }

    func markAll() { marked = Set(rows) }

    func clearMarks() { marked = [] }

    func setParent(_ n: Int, _ parent: Int?, what: String) {
        enqueue(PendingOp(kind: .setParent, number: n, parent: parent, what: what))
    }

    private func label(_ ns: [Int]) -> String {
        ns.count == 1 ? "“\(items[ns[0]]?.title ?? "a task")”" : "\(ns.count) tasks"
    }

    /// Complete (or reopen) tasks; a group takes its open tasks with it.
    func completeMany(_ numbers: [Int]) {
        let targets = Grouping.withMembers(numbers, all: live).filter { items[$0]?.isOpen == true }
        guard !targets.isEmpty else { return }
        undoableMany(targets, "completing \(label(numbers))") {
            for n in targets { update(n, set: ["state": "CLOSED"], what: "Complete “\(items[n]?.title ?? "")”") }
            return []
        }
        marked.subtract(targets)
        show("Completed \(label(numbers))" + (targets.count > numbers.count ? " with their tasks" : ""), undo: { [weak self] in self?.undo() })
    }

    /// Delete tasks the apps' way (closed, out of their list); a group goes with its open tasks.
    func deleteMany(_ numbers: [Int]) {
        let targets = Grouping.withMembers(numbers, all: live)
        for n in targets where n < 0 { discardPending(n) }
        let real = targets.filter { $0 > 0 }
        if let s = selection, targets.contains(s) { selection = nil }
        undoableMany(real, "deleting \(label(numbers))") {
            for n in real {
                guard let t = items[n] else { continue }
                let lists = t.issue.labels.filter { $0.lowercased().hasPrefix("list:") }
                update(n, set: ["state": "CLOSED"], remove: lists, what: "Delete “\(t.title)”")
            }
            return []
        }
        marked = []
        show("Deleted \(label(numbers))", undo: { [weak self] in self?.undo() })
    }

    /// Move tasks to a list; a group takes its open tasks with it.
    func moveMany(_ numbers: [Int], toList list: String) {
        let l = listSlug(list)
        guard !l.isEmpty else { return }
        let targets = Grouping.withMembers(numbers, all: live).filter { items[$0]?.list != l }
        guard !targets.isEmpty else { return }
        undoableMany(targets, "moving \(label(numbers)) to \(titleCase(l))") {
            for n in targets {
                guard let t = items[n] else { continue }
                let diff = labelDiff(from: t, list: l, section: t.section, priority: t.priority, config: config)
                update(n, add: diff.add, remove: diff.remove, what: "Move “\(t.title)” to \(titleCase(l))")
            }
            return []
        }
        marked = []
        show("Moved \(label(numbers)) to \(titleCase(l))", undo: { [weak self] in self?.undo() })
    }

    /// Set importance (high, medium, low) and/or urgency (high, low). A group is rated itself; its tasks keep theirs.
    func rateMany(_ numbers: [Int], importance: String? = nil, urgency: String? = nil) {
        undoableMany(numbers, "rating \(label(numbers))") {
            for n in numbers {
                guard let t = items[n] else { continue }
                var add: [String] = []
                if let i = importance { add.append("importance:" + i) }
                if let u = urgency { add.append("urgency:" + u) }
                else if importance != nil, Labels.value(t.issue.labels, "urgency:") == nil { add.append("urgency:low") }
                let remove = t.issue.labels.filter { $0.lowercased().hasPrefix("priority:") }   // retired
                update(n, add: add, remove: remove, what: "Rate “\(t.title)”")
            }
            return []
        }
        show("Rated \(label(numbers))", undo: { [weak self] in self?.undo() })
    }

    /// Group tasks: into the one group among them, if there is exactly one; otherwise into a new group called
    /// `name`, in the first task's list, rated from its tasks. Returns the group's number.
    @discardableResult
    func groupMany(_ numbers: [Int], name: String) -> Int? {
        let groups = numbers.filter { isGroup($0) }
        if groups.count == 1, let g = groups.first {
            let members = numbers.filter { $0 != g && items[$0]?.parent != g }
            guard !members.isEmpty else { return g }
            undoableMany(members, "adding \(label(members)) to “\(items[g]?.title ?? "the group")”") {
                for n in members { setParent(n, g, what: "Group “\(items[n]?.title ?? "")”") }
                return []
            }
            marked = []
            show("Added \(label(members)) to “\(items[g]?.title ?? "the group")”", undo: { [weak self] in self?.undo() })
            return g
        }
        let title = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let tasks = numbers.compactMap { items[$0] }
        guard !title.isEmpty, let first = tasks.first else { return nil }
        var g: Int?
        undoableMany(numbers, "grouping \(label(numbers))") {
            var labels = ["list:" + first.list, "source:app"] + Grouping.ratingLabels(tasks)
            if let grp = config?.group(of: first.list) { labels.append("group:" + grp) }
            let n = nextLocal
            nextLocal -= 1
            enqueue(PendingOp(kind: .create, number: n, title: title, body: "", labels: labels, what: "Group “\(title)”"))
            for t in tasks { setParent(t.number, n, what: "Group “\(t.title)”") }
            g = n
            return [n]
        }
        marked = []
        if let n = g { selection = n }
        show("Grouped \(tasks.count) tasks as “\(title)”", undo: { [weak self] in self?.undo() })
        return g
    }

    /// Take groups apart: their tasks leave, and each group itself is deleted.
    func ungroupMany(_ numbers: [Int]) {
        let groups = numbers.filter { isGroup($0) }
        guard !groups.isEmpty else { return }
        let members = live.filter { t in t.parent.map { groups.contains($0) } ?? false }.map { $0.number }
        undoableMany(members + groups, "ungrouping \(label(groups))") {
            for n in members { setParent(n, nil, what: "Ungroup “\(items[n]?.title ?? "")”") }
            for g in groups {
                guard let t = items[g] else { continue }
                let lists = t.issue.labels.filter { $0.lowercased().hasPrefix("list:") }
                update(g, set: ["state": "CLOSED"], remove: lists, what: "Delete group “\(t.title)”")
            }
            return []
        }
        marked = []
        show("Ungrouped \(label(groups))", undo: { [weak self] in self?.undo() })
    }

    /// Take tasks out of whatever group they are in.
    func takeOut(_ numbers: [Int]) {
        let grouped = numbers.filter { items[$0]?.parent != nil }
        guard !grouped.isEmpty else { return }
        undoableMany(grouped, "taking \(label(grouped)) out of the group") {
            for n in grouped { setParent(n, nil, what: "Take “\(items[n]?.title ?? "")” out of its group") }
            return []
        }
        show("Took \(label(grouped)) out of the group", undo: { [weak self] in self?.undo() })
    }

    /// A task dragged onto another: onto a group it joins it; onto a task, the two (or the whole selection, if
    /// the dragged task is in it) become a new group, after asking for its name.
    func drop(_ payload: String, onto target: Int) -> Bool {
        guard payload.hasPrefix("task:"), let n = Int(payload.dropFirst(5)), n != target, items[target] != nil else { return false }
        let moving = marked.contains(n) ? chosen.filter { $0 != target } : [n]
        guard !moving.isEmpty else { return false }
        if isGroup(target) {
            groupMany([target] + moving, name: "")
        } else {
            Dialogs.groupName(store: self, numbers: [target] + moving)
        }
        return true
    }

    /// The instructions sent with an agent request for several tasks, or for a group: the agent is told of the rest.
    func assignText(lead: Int, others: [Int], text: String) -> String {
        var lines: [String] = []
        let members = Grouping.openDescendants(lead, all: live)
        if !members.isEmpty {
            lines.append("This is a group; its tasks are its sub-issues: " + members.map { "#\($0)" }.joined(separator: ", ") + ".")
        }
        if !others.isEmpty {
            lines.append("Handle these tasks together with this one, as one job: " + others.map { "#\($0)" }.joined(separator: ", ") + ".")
        }
        return ([text.trimmingCharacters(in: .whitespacesAndNewlines)] + lines).filter { !$0.isEmpty }.joined(separator: "\n\n")
    }

    /// After one agent was asked for several tasks: a note on each of the others.
    func noteOthers(_ others: [Int], lead: Int) {
        for n in others where n > 0 {
            channel.request(["cmd": "comment", "number": n,
                             "body": "Assigned to an agent together with #\(lead), which carries the request."], timeout: 60) { _ in }
        }
    }

    // MARK: undo (⌘Z, the top-bar button, the toast's Undo)

    private func pushUndo(_ e: UndoEntry) {
        undoStack.append(e)
        if undoStack.count > 60 { undoStack.removeFirst(undoStack.count - 60) }
    }

    private func undoable(_ number: Int, _ what: String, _ action: () -> Void) {
        let before = items[number]?.issue
        action()
        guard let b = before, let after = items[number]?.issue, after != b else { return }
        pushUndo(UndoEntry(what: what, number: number, before: b, after: after))
    }

    /// Several tasks changed by one action, undone together. `action` returns any tasks it created (a new group),
    /// which undo deletes. Tasks it left unchanged aren't recorded.
    private func undoableMany(_ numbers: [Int], _ what: String, _ action: () -> [Int]) {
        var before: [Int: Issue] = [:]
        for n in numbers { if let i = items[n]?.issue { before[n] = i } }
        let created = action()
        var parts: [UndoPart] = []
        for n in created { if let a = items[n]?.issue { parts.append(UndoPart(number: n, before: nil, after: a)) } }
        for n in numbers {
            guard let b = before[n], let a = items[n]?.issue, a != b else { continue }
            parts.append(UndoPart(number: n, before: b, after: a))
        }
        if !parts.isEmpty { pushUndo(UndoEntry(what: what, parts: parts)) }
    }

    var undoTitle: String? { undoStack.last.map { "Undo " + $0.what } }

    /// Reverse the last action: only what it changed, checked against each task as it left it, so anything changed
    /// elsewhere since (an agent's notes, say) merges rather than being overwritten. Parts are undone last first:
    /// a group's tasks leave it before the group itself is deleted.
    func undo() {
        guard let e = undoStack.popLast() else { show("Nothing to undo"); return }
        toast = nil
        var gone = 0
        for p in e.parts.reversed() {
            guard items[p.number] != nil else { gone += 1; continue }
            guard let before = p.before else {
                deleteTask(p.number, recordUndo: false)
                continue
            }
            var set: [String: String] = [:], base: [String: String] = [:]
            if p.after.title != before.title { set["title"] = before.title; base["title"] = p.after.title }
            if p.after.body != before.body { set["body"] = before.body; base["body"] = p.after.body }
            if p.after.state != before.state { set["state"] = before.state }
            let regroup = p.after.parent != before.parent
            // the subtask label goes with the group link, in the set_parent request
            let had = Set(before.labels.map { $0.lowercased() }), has = Set(p.after.labels.map { $0.lowercased() })
            let add = before.labels.filter { !has.contains($0.lowercased()) && !(regroup && $0 == subtaskLabel) }
            let remove = p.after.labels.filter { !had.contains($0.lowercased()) && !(regroup && $0 == subtaskLabel) }
            if !set.isEmpty || !add.isEmpty || !remove.isEmpty {
                update(p.number, set: set, add: add, remove: remove, what: "Undo " + e.what, base: base)
            }
            if regroup { setParent(p.number, before.parent, what: "Undo " + e.what) }
        }
        if gone == e.parts.count { show("Can't undo \(e.what): the task is gone", error: true); return }
        show("Undid \(e.what)")
    }

    // MARK: moving lists and groups in the sidebar (saved to .todo/config.json, so the web app sees the same order)

    var topOrder: [TopRef] {
        sidebar.map {
            switch $0 {
            case .list(let n, _): return .list(n)
            case .group(let g, _, _): return .group(g)
            }
        }
    }

    func lists(inGroup g: String) -> [String] {
        for item in sidebar { if case .group(let name, _, let ls) = item, name == g { return ls.map { $0.name } } }
        return []
    }

    private func sortOps(_ order: [TopRef]) -> [ConfigOp] {
        var ops: [ConfigOp] = []
        for (n, ref) in order.enumerated() {
            switch ref {
            case .group(let g):
                if config?.groups.first(where: { $0.name == g })?.sortOrder != n { ops.append(ConfigOp(op: "set_sort", group: g, order: n)) }
            case .list(let l):
                if config?.meta(l)?.sortOrder != n { ops.append(ConfigOp(op: "set_sort", list: l, order: n)) }
            }
        }
        return ops
    }

    /// Put a group just before another top-level item (nil: at the end).
    func moveGroup(_ g: String, before target: TopRef?) {
        guard target != .group(g) else { return }
        var order = topOrder.filter { $0 != .group(g) }
        let i = target.flatMap { order.firstIndex(of: $0) } ?? order.count
        order.insert(.group(g), at: i)
        let ops = sortOps(order)
        if !ops.isEmpty { enqueue(PendingOp(kind: .config, configOps: ops, what: "Move group \(g)")) }
    }

    /// Put a list into a group (nil: the top level), just before another list there (nil: at the end).
    func moveList(_ l: String, toGroup g: String?, before target: String?) {
        guard target != l else { return }
        var ops: [ConfigOp] = []
        if config?.group(of: l) != g { ops.append(ConfigOp(op: "set_group", list: l, group: g)) }
        if let g = g {
            var ls = lists(inGroup: g).filter { $0 != l }
            let i = target.flatMap { ls.firstIndex(of: $0) } ?? ls.count
            ls.insert(l, at: i)
            for (n, name) in ls.enumerated() where config?.meta(name)?.sortOrder != n || name == l {
                ops.append(ConfigOp(op: "set_sort", list: name, order: n))
            }
        } else {
            var order = topOrder.filter { $0 != .list(l) }
            let i = target.flatMap { order.firstIndex(of: .list($0)) } ?? order.count
            order.insert(.list(l), at: i)
            ops += sortOps(order)
        }
        if !ops.isEmpty { enqueue(PendingOp(kind: .config, configOps: ops, what: "Move list \(l)")) }
        if let g = g { expanded.insert(g) }
    }

    // MARK: assign to an agent (needs the desktop now; never queued)

    /// `to`: a running agent's session id; the preview is then the message that agent is sent.
    func assignPreview(_ number: Int, text: String, to: String? = nil, done: @escaping (String?, String?) -> Void) {
        if let stub = previewStub { done(stub, nil); return }
        var req: [String: Any] = ["cmd": "assign_preview", "number": number, "text": text]
        if let to = to { req["to"] = to }
        channel.request(req, timeout: 60) { r in
            switch r {
            case .success(let o): done(o["message"] as? String, nil)
            case .failure(let e): done(nil, e.message)
            }
        }
    }

    /// The agent board's running agents, from the desktop. A helper too old to know `agents` answers with an error,
    /// so the sheet can't pick one, and never sends a `to` that helper would ignore by starting a new agent.
    func runningAgents(done: @escaping ([BoardAgent]?, String?) -> Void) {
        if let stub = agentsStub { done(stub, nil); return }
        channel.request(["cmd": "agents"], timeout: 60) { r in
            switch r {
            case .success(let o): done(BoardAgent.decode(o["agents"]), nil)
            case .failure(let e): done(nil, e.message)
            }
        }
    }

    /// `to`: send the task into that running agent instead of having the orchestrator start one.
    func assign(_ number: Int, text: String, again: Bool, to: BoardAgent? = nil, done: @escaping (String?) -> Void) {
        var req: [String: Any] = ["cmd": "assign", "number": number, "text": text, "again": again]
        if let a = to { req["to"] = a.id }
        channel.request(req, timeout: 120) { [weak self] r in
            guard let self = self else { return }
            switch r {
            case .success(let o):
                if let i = decodeIssue(o["issue"]) { self.server[i.number] = i; self.rebuild([i.number]); self.scheduleSave() }
                self.show(to.map { "Sent to \($0.name). It fits the task in around what it's doing." }
                          ?? "Assigned. The orchestrator is starting an agent and will email you which one.")
                done(nil)
            case .failure(let e):
                done(e.message)
            }
        }
    }

    func comments(_ number: Int, done: @escaping ([[String: Any]]?, String?) -> Void) {
        channel.request(["cmd": "comments", "number": number], timeout: 60) { r in
            switch r {
            case .success(let o): done(o["comments"] as? [[String: Any]], nil)
            case .failure(let e): done(nil, e.message)
            }
        }
    }

    // MARK: selection, zoom, messages

    /// ⌘N: back to the current list if searching, then into its add field.
    func requestAdd() {
        if case .list = route {} else {
            query = ""
            route = .list(Store.defaults.string(forKey: "list") ?? "tasks")
        }
        focusRequest = .add
    }

    func move(_ delta: Int) {
        let r = rows
        guard !r.isEmpty else { return }
        if let s = selection, let i = r.firstIndex(of: s) {
            selection = r[max(0, min(r.count - 1, i + delta))]
        } else {
            selection = delta > 0 ? r.first : r.last
        }
    }

    private func selectNeighbour(of number: Int) {
        let r = rows
        guard let i = r.firstIndex(of: number) else { selection = nil; return }
        selection = i + 1 < r.count ? r[i + 1] : (i > 0 ? r[i - 1] : nil)
    }

    func zoom(by step: Int) {
        let steps: [CGFloat] = [0.7, 0.8, 0.9, 1, 1.1, 1.25, 1.4, 1.6, 1.8, 2]
        let i = steps.firstIndex { $0 >= zoom - 0.001 } ?? 3
        setZoom(steps[max(0, min(steps.count - 1, i + step))])
    }

    func setZoom(_ z: CGFloat) {
        zoom = z
        Store.defaults.set(Double(z), forKey: "zoom")
    }

    func show(_ text: String, error: Bool = false, undo: (() -> Void)? = nil) {
        let t = Toast(text: text, isError: error, undo: undo)
        toast = t
        DispatchQueue.main.asyncAfter(deadline: .now() + (undo != nil || error ? 7 : 4)) { [weak self] in
            if self?.toast?.id == t.id { self?.toast = nil }
        }
    }

    func relaunch() {
        let path = Bundle.main.bundlePath
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        p.arguments = ["-c", "sleep 1; open \(shellQuote(path))"]
        try? p.run()
        NSApp.terminate(nil)
    }

    /// For the offscreen renderer and self-tests: replace everything with a saved cache.
    func install(issues: [Issue], config: TodoConfig?, pending: [PendingOp] = []) {
        server = Dictionary(issues.map { ($0.number, $0) }, uniquingKeysWith: { a, _ in a })
        serverConfig = config
        self.pending = pending
        hasLoaded = true
        rebuildAll()
    }
}

// MARK: - decoding todo-desk's JSON

func decodeIssue(_ any: Any?) -> Issue? {
    guard let d = any as? [String: Any], JSONSerialization.isValidJSONObject(d),
          let data = try? JSONSerialization.data(withJSONObject: d) else { return nil }
    return try? JSONDecoder().decode(Issue.self, from: data)
}

func decodeIssues(_ any: Any?) -> [Issue] {
    ((any as? [Any]) ?? []).compactMap { decodeIssue($0) }
}

func decodeConfig(_ any: Any?) -> TodoConfig? {
    guard let d = any as? [String: Any], let data = try? JSONSerialization.data(withJSONObject: d) else { return nil }
    return try? JSONDecoder().decode(TodoConfig.self, from: data)
}
