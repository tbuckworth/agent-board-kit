import AppKit
import Combine
import Foundation
import SwiftUI

struct TileGroup: Codable, Identifiable, Equatable {
    var id = UUID()
    var name: String
    var members: [String] = []
    var collapsed = false
    /// One level of nesting: a group's sub-groups sit side by side inside it. Sub-groups have none of their own.
    var subgroups: [TileGroup] = []

    init(name: String) { self.name = name }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = (try? c.decode(UUID.self, forKey: .id)) ?? UUID()
        name = (try? c.decode(String.self, forKey: .name)) ?? "Group"
        members = (try? c.decodeIfPresent([String].self, forKey: .members)) ?? []
        collapsed = (try? c.decodeIfPresent(Bool.self, forKey: .collapsed)) ?? false
        subgroups = (try? c.decodeIfPresent([TileGroup].self, forKey: .subgroups)) ?? []
    }

    var allMembers: [String] { members + subgroups.flatMap { $0.members } }
}

/// Tile arrangement, saved on the Mac only: named groups plus a manual order for everything ungrouped.
struct Layout: Codable, Equatable {
    var groups: [TileGroup] = []
    var order: [String] = []
    var offlineCollapsed = false
    var worktreesCollapsed = false

    init() {}

    /// Every field optional on read: a layout saved by an older build must never fail to load (and lose groups).
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        groups = (try? c.decodeIfPresent([TileGroup].self, forKey: .groups)) ?? []
        order = (try? c.decodeIfPresent([String].self, forKey: .order)) ?? []
        offlineCollapsed = (try? c.decodeIfPresent(Bool.self, forKey: .offlineCollapsed)) ?? false
        worktreesCollapsed = (try? c.decodeIfPresent(Bool.self, forKey: .worktreesCollapsed)) ?? false
    }

    static var url: URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("AgentBoard", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("layout.json")
    }

    static func load() -> Layout {
        guard let data = try? Data(contentsOf: url), let l = try? JSONDecoder().decode(Layout.self, from: data) else {
            return Layout()
        }
        return l
    }

    func save() {
        if let data = try? JSONEncoder().encode(self) { try? data.write(to: Layout.url, options: .atomic) }
    }

    mutating func remove(_ id: String) {
        order.removeAll { $0 == id }
        for i in groups.indices {
            groups[i].members.removeAll { $0 == id }
            for j in groups[i].subgroups.indices { groups[i].subgroups[j].members.removeAll { $0 == id } }
        }
    }

    /// The innermost group (or sub-group) holding a tile.
    func group(of id: String) -> UUID? {
        for g in groups {
            if let sub = g.subgroups.first(where: { $0.members.contains(id) }) { return sub.id }
            if g.members.contains(id) { return g.id }
        }
        return nil
    }

    /// (top-level index, sub-group index or nil) of a group id, wherever it is.
    func locate(_ gid: UUID) -> (Int, Int?)? {
        for (i, g) in groups.enumerated() {
            if g.id == gid { return (i, nil) }
            if let j = g.subgroups.firstIndex(where: { $0.id == gid }) { return (i, j) }
        }
        return nil
    }

    func parent(of gid: UUID) -> UUID? {
        guard let (i, j) = locate(gid), j != nil else { return nil }
        return groups[i].id
    }

    func find(_ gid: UUID) -> TileGroup? {
        guard let (i, j) = locate(gid) else { return nil }
        return j.map { groups[i].subgroups[$0] } ?? groups[i]
    }

    /// Change one group or sub-group in place.
    mutating func update(_ gid: UUID, _ change: (inout TileGroup) -> Void) {
        guard let (i, j) = locate(gid) else { return }
        if let j = j { change(&groups[i].subgroups[j]) } else { change(&groups[i]) }
    }

    /// Take a group or sub-group out of the layout (its tiles go with it).
    mutating func extract(_ gid: UUID) -> TileGroup? {
        guard let (i, j) = locate(gid) else { return nil }
        if let j = j { return groups[i].subgroups.remove(at: j) }
        return groups.remove(at: i)
    }
}

enum SectionKind { case group, subgroup, ungrouped, offline }

struct BoardSection: Identifiable {
    var id: String
    var title: String
    var kind: SectionKind
    var groupID: UUID?
    var agents: [Agent]
    var collapsed: Bool
    var subsections: [BoardSection] = []
}

/// An open "Change agent" / "Change model/effort" sheet.
struct SwitchSession: Identifiable {
    let id = UUID()
    let agent: Agent
    let modelOnly: Bool
}

enum Tab: String, CaseIterable, Identifiable {
    case board = "Board", overview = "Overview"
    var id: String { rawValue }
}

@MainActor
final class BoardModel: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var metrics: Metrics?
    @Published var usage: UsageInfo?
    @Published var link: LinkState = .connecting
    @Published var lastMessage: Date?
    @Published var collectorError: String?
    @Published var selectedID: String?
    @Published var tab: Tab = .board
    @Published var layout: Layout
    @Published var now = Date()
    @Published var toast: String?
    @Published var usageRefreshing = false
    @Published var updateAvailable = false
    /// What the pointer is over in the top bar (usage window, meter), shown in the footer: tooltips don't
    /// reliably appear inside the top bar's scroll view.
    @Published var hoverInfo: String?
    @Published var switching: SwitchSession?
    @Published var showRestart = false
    @Published var zoom: CGFloat = {
        let z = UserDefaults.standard.double(forKey: "zoom")
        return z > 0 ? CGFloat(z) : 1
    }()

    let stream = StreamLink()
    private var clock: Timer?
    private var toastTimer: Timer?

    /// false for offscreen renders, which must never overwrite the saved layout
    let persist: Bool

    init(layout: Layout = Layout.load(), persist: Bool = true) {
        self.layout = layout
        self.persist = persist
    }

    func connect() {
        stream.onLine = { [weak self] line in self?.handle(line) }
        stream.onState = { [weak self] s in self?.link = s }
        stream.start()
        clock = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.now = Date() }
        }
    }

    func handle(_ line: Data) {
        guard let env = try? JSON.decoder.decode(Envelope.self, from: line) else { return }
        lastMessage = Date()
        link = .live
        switch env.type {
        case "snapshot":
            do {
                let snap = try JSON.decoder.decode(Snapshot.self, from: line)
                apply(snap)
                collectorError = nil
            } catch {
                collectorError = "snapshot not understood: \(error)"
            }
        case "metrics":
            if let m = try? JSON.decoder.decode(MetricsMessage.self, from: line) {
                if let mm = m.metrics { metrics = mm }
                if let u = m.usage { usage = u; usageRefreshing = false }
            }
        case "error":
            collectorError = (try? JSON.decoder.decode(ErrorMessage.self, from: line))?.error
        default:
            break
        }
    }

    func apply(_ snap: Snapshot) {
        snapshot = snap
        if let m = snap.metrics { metrics = m }
        if let u = snap.usage { usage = u; usageRefreshing = false }
        var l = layout
        for a in snap.agents where a.agentState != .offline && !l.order.contains(a.id) && l.group(of: a.id) == nil {
            l.order.append(a.id)
        }
        if l.order.count > 400 { l.order = Array(l.order.suffix(300)) }
        if l != layout { commit(l) }
        if let sel = selectedID, !snap.agents.contains(where: { $0.id == sel }),
           !snap.worktrees.contains(where: { $0.id == sel }) { selectedID = nil }
    }

    // MARK: derived

    var agents: [Agent] { snapshot?.agents ?? [] }
    var selected: Agent? { agents.first { $0.id == selectedID } }
    var freeWorktrees: [FreeWorktree] { snapshot?.worktrees ?? [] }
    var selectedWorktree: FreeWorktree? { freeWorktrees.first { $0.id == selectedID } }

    func toggleWorktreesCollapsed() {
        var l = layout
        l.worktreesCollapsed.toggle()
        commit(l)
    }

    func count(_ s: AgentState) -> Int { agents.filter { $0.agentState == s }.count }

    /// summary counts; JSONDecoder's snake-case strategy may or may not have touched dictionary keys
    func summary(_ key: String) -> Int {
        let s = snapshot?.summary ?? [:]
        return s[key] ?? s[key.replacingOccurrences(of: "_", with: "")] ?? 0
    }

    var sections: [BoardSection] {
        let byID = Dictionary(agents.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        var placed = Set<String>()
        var out: [BoardSection] = []
        for g in layout.groups {
            placed.formUnion(g.allMembers)
            let subs = g.subgroups.map {
                BoardSection(id: $0.id.uuidString, title: $0.name, kind: .subgroup, groupID: $0.id,
                             agents: $0.members.compactMap { byID[$0] }, collapsed: $0.collapsed)
            }
            out.append(BoardSection(id: g.id.uuidString, title: g.name, kind: .group, groupID: g.id,
                                    agents: g.members.compactMap { byID[$0] }, collapsed: g.collapsed, subsections: subs))
        }
        let rest = agents.filter { !placed.contains($0.id) }
        let rank = Dictionary(layout.order.enumerated().map { ($1, $0) }, uniquingKeysWith: { a, _ in a })
        let live = rest.filter { $0.agentState != .offline }
            .enumerated()
            .sorted { (rank[$0.element.id] ?? Int.max, $0.offset) < (rank[$1.element.id] ?? Int.max, $1.offset) }
            .map { $0.element }
        out.append(BoardSection(id: "ungrouped", title: layout.groups.isEmpty ? "Agents" : "Ungrouped",
                                kind: .ungrouped, groupID: nil, agents: live, collapsed: false))
        let offline = rest.filter { $0.agentState == .offline }
        if !offline.isEmpty {
            out.append(BoardSection(id: "offline", title: "Offline", kind: .offline, groupID: nil,
                                    agents: offline, collapsed: layout.offlineCollapsed))
        }
        return out
    }

    // MARK: layout edits (drag and drop, groups)

    func move(_ id: String, before target: String) {
        guard id != target else { return }
        var l = layout
        l.remove(id)
        if let gid = l.group(of: target) {
            l.update(gid) { g in g.members.insert(id, at: g.members.firstIndex(of: target) ?? g.members.count) }
        } else {
            let idx = l.order.firstIndex(of: target) ?? l.order.count
            l.order.insert(id, at: idx)
        }
        commit(l)
    }

    func move(_ id: String, toGroup gid: UUID?) {
        var l = layout
        l.remove(id)
        if let gid = gid, l.locate(gid) != nil {
            l.update(gid) { $0.members.append(id) }
        } else {
            l.order.append(id)
        }
        commit(l)
    }

    /// A new top-level group, or (with `parent`) a sub-group inside that group; optionally with a tile in it.
    @discardableResult
    func newGroup(name: String? = nil, with id: String? = nil, parent: UUID? = nil) -> UUID {
        var l = layout
        let host = parent.flatMap { l.find($0) }
        var g = TileGroup(name: name ?? (host.map { "Sub-group \($0.subgroups.count + 1)" } ?? "Group \(l.groups.count + 1)"))
        if let id = id { l.remove(id); g.members = [id] }
        if let p = parent, let (i, j) = l.locate(p), j == nil {
            l.groups[i].subgroups.append(g)
        } else {
            l.groups.append(g)
        }
        commit(l)
        return g.id
    }

    func renameGroup(_ gid: UUID, to name: String) {
        guard !name.isEmpty else { return }
        var l = layout
        l.update(gid) { $0.name = name }
        commit(l)
    }

    /// Delete a group: its tiles (and a top-level group's sub-groups' tiles) go to the parent, or to Ungrouped.
    func deleteGroup(_ gid: UUID) {
        var l = layout
        let parent = l.parent(of: gid)
        guard let g = l.extract(gid) else { return }
        if let p = parent {
            l.update(p) { $0.members.append(contentsOf: g.members) }
        } else {
            l.order.append(contentsOf: g.allMembers)
        }
        commit(l)
    }

    func toggleCollapsed(_ section: BoardSection) {
        var l = layout
        if section.kind == .offline {
            l.offlineCollapsed.toggle()
        } else if let gid = section.groupID {
            l.update(gid) { $0.collapsed.toggle() }
        }
        commit(l)
    }

    /// Drag a group's or sub-group's name onto another: it goes just before that one, at that one's level.
    /// A group with sub-groups of its own can't become a sub-group (one level only).
    func moveGroup(_ gid: UUID, before target: UUID?) {
        guard gid != target else { return }
        var l = layout
        let targetParent = target.flatMap { l.parent(of: $0) }
        if targetParent != nil, let g = l.find(gid), !g.subgroups.isEmpty {
            show("A group with sub-groups can't become a sub-group.")
            return
        }
        if targetParent == gid { return }
        guard let g = l.extract(gid) else { return }
        if let p = targetParent, let (pi, _) = l.locate(p) {
            let ti = l.groups[pi].subgroups.firstIndex { $0.id == target } ?? l.groups[pi].subgroups.count
            l.groups[pi].subgroups.insert(g, at: ti)
        } else {
            let ti = target.flatMap { t in l.groups.firstIndex { $0.id == t } } ?? l.groups.count
            l.groups.insert(g, at: ti)
        }
        commit(l)
    }

    /// Make a group (or another group's sub-group) a sub-group of `parent`, at the end.
    func moveGroup(_ gid: UUID, into parent: UUID) {
        guard gid != parent, isTopLevel(parent) else { return }
        var l = layout
        if let g = l.find(gid), !g.subgroups.isEmpty {
            show("A group with sub-groups can't become a sub-group.")
            return
        }
        guard let g = l.extract(gid), let (pi, _) = l.locate(parent) else { return }
        l.groups[pi].subgroups.append(g)
        commit(l)
    }

    /// A sub-group becomes a top-level group, just after its parent.
    func promoteGroup(_ gid: UUID) {
        var l = layout
        guard let p = l.parent(of: gid), let g = l.extract(gid), let (pi, _) = l.locate(p) else { return }
        l.groups.insert(g, at: pi + 1)
        commit(l)
    }

    private func isTopLevel(_ gid: UUID) -> Bool {
        if let (_, j) = layout.locate(gid) { return j == nil }
        return false
    }

    /// Payloads are tile ids, or "group:UUID" for a group or sub-group name.
    static func groupPayload(_ s: String) -> UUID? {
        s.hasPrefix("group:") ? UUID(uuidString: String(s.dropFirst(6))) : nil
    }

    /// Move a group up/down (or a sub-group left/right) within its own level.
    func moveGroup(_ gid: UUID, by delta: Int) {
        var l = layout
        guard let (i, j) = l.locate(gid) else { return }
        if let j = j {
            let nj = max(0, min(l.groups[i].subgroups.count - 1, j + delta))
            let g = l.groups[i].subgroups.remove(at: j)
            l.groups[i].subgroups.insert(g, at: nj)
        } else {
            let ni = max(0, min(l.groups.count - 1, i + delta))
            let g = l.groups.remove(at: i)
            l.groups.insert(g, at: ni)
        }
        commit(l)
    }

    private func commit(_ l: Layout) {
        layout = l
        if persist { l.save() }
    }

    // MARK: zoom

    static let zoomSteps: [CGFloat] = [0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.4, 1.6, 1.8, 2.0]

    func zoom(by step: Int) {
        let steps = BoardModel.zoomSteps
        let i = steps.firstIndex { $0 >= zoom - 0.001 } ?? 3
        setZoom(steps[max(0, min(steps.count - 1, i + step))])
    }

    func setZoom(_ z: CGFloat) {
        zoom = z
        if persist { UserDefaults.standard.set(Double(z), forKey: "zoom") }
        show("Zoom \(Int((z * 100).rounded()))%")
    }

    // MARK: actions

    /// ⌘R: reconnect if the link is down, otherwise ask for everything fresh (git, PRs, tmux, usage).
    func refreshAll() {
        if case .live = link {
            stream.send(["cmd": "refresh"])
            show("Refreshing…")
        } else {
            reconnect()
            show("Reconnecting…")
        }
    }

    func refreshUsage() {
        usageRefreshing = true
        stream.send(["cmd": "refresh_usage"])
    }

    func reconnect() { stream.start() }

    /// Start the newly built copy of this app and quit this one (`open` alone would just re-show this one).
    func relaunch() {
        let bundle = Bundle.main.bundlePath
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        p.arguments = ["-c", "sleep 0.7; /usr/bin/open \"$0\"", bundle]
        try? p.run()
        NSApplication.shared.terminate(nil)
    }

    func show(_ message: String) {
        toast = message
        toastTimer?.invalidate()
        toastTimer = Timer.scheduledTimer(withTimeInterval: 6, repeats: false) { [weak self] _ in
            Task { @MainActor in self?.toast = nil }
        }
    }

    /// The agent's latest mail to the owner, in the owner's Gmail: the thread itself when the read-check has found it,
    /// else a search for that exact message. Gmail does the rest (images, replying, unread).
    func latestMailURL(for a: Agent) -> URL? {
        // ?authuser=EMAIL picks the account (Gmail redirects to its /u/N/); a literal /u/EMAIL/ path gives
        // "Temporary Error (404): your account is temporarily unavailable"
        let owner = (snapshot?.owner).flatMap { $0.isEmpty ? nil : $0 }
        let safe = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "@.-_"))   // `+` would read as a space
        let base = "https://mail.google.com/mail/" + (owner.flatMap {
            $0.addingPercentEncoding(withAllowedCharacters: safe).map { "?authuser=" + $0 } } ?? "u/0/")
        if let t = a.mail.gmailThread, !t.isEmpty { return URL(string: base + "#all/" + t) }
        guard let mid = a.mail.lastOutId, !mid.isEmpty else { return nil }
        let q = ("rfc822msgid:" + mid.trimmingCharacters(in: CharacterSet(charactersIn: "<> ")))
            .addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
        return URL(string: base + "#search/" + q)
    }

    func openLatestMail(_ a: Agent) {
        guard let url = latestMailURL(for: a) else { show("\(a.display) hasn't emailed you in the last week."); return }
        Browser.open(url, signedInAs: snapshot?.owner)
    }

    func attachCommand(for a: Agent) -> String? {
        let host = Settings.host
        if let t = a.tmux, !t.isEmpty {
            return "ssh -t \(host) " + shellQuote("tmux attach -t " + shellQuote("=" + t))
        }
        if a.kind == "bg" && a.cli == "claude", let remote = Kitty.remoteCommand(for: a) {
            return "ssh -t \(host) " + shellQuote(remote)
        }
        return nil
    }
}
