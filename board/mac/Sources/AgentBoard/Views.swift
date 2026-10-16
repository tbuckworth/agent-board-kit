import AppKit
import SwiftUI

// MARK: - root

struct BoardView: View {
    @ObservedObject var model: BoardModel

    var body: some View {
        VStack(spacing: 0) {
            TopBar(model: model)
            Divider()
            HStack(spacing: 0) {
                Group {
                    if model.tab == .board { BoardGrid(model: model) } else { OverviewView(model: model) }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                if model.tab == .board, let a = model.selected {
                    Divider()
                    DetailView(agent: a, model: model).frame(width: 340 * model.zoom)
                } else if model.tab == .board, let w = model.selectedWorktree {
                    Divider()
                    WorktreeDetail(wt: w, model: model).frame(width: 340 * model.zoom)
                }
            }
            Divider()
            FooterBar(model: model)
        }
        .frame(minWidth: 780, minHeight: 480)
        .background(Color(nsColor: .windowBackgroundColor))
        .environment(\.zoom, model.zoom)
        .sheet(item: $model.switching) { s in SwitchSheet(session: s, model: model) }
        .sheet(isPresented: $model.showRestart) { RestartSheet(model: model) }
    }
}

// MARK: - top bar: link, tabs, mail, machine metrics, plan usage

struct TopBar: View {
    @Environment(\.zoom) private var z
    @ObservedObject var model: BoardModel
    @State private var showHeld = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                LinkBadge(model: model)
                Picker("", selection: $model.tab) {
                    ForEach(Tab.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 170)
                Button { model.refreshAll() } label: {
                    Image(systemName: "arrow.clockwise").font(.system(size: 12 * z, weight: .medium))
                }
                .buttonStyle(.borderless)
                .help("Refresh everything now (⌘R): agents, git, PRs, usage")
                Button { Dialogs.orchestrator(model: model) } label: {
                    Label("Orchestrator…", systemImage: "paperplane").font(.system(size: 11 * z))
                }
                .buttonStyle(.borderless)
                .help("Message the orchestrator, like an email: e.g. ask it to start a new agent")
                Spacer()
                if let s = model.snapshot {
                    let waiting = s.agents.filter { $0.agentState == .waiting }.count
                    if waiting > 0 {
                        Chip(text: "\(waiting) waiting on you", color: AgentState.waiting.color)
                    }
                    let blocked = s.agents.filter { $0.agentState == .blocked }.count
                    if blocked > 0 {
                        Chip(text: "\(blocked) blocked", color: AgentState.blocked.color)
                    }
                    let stranded = s.agents.filter { $0.stranded ?? false }.count
                    if stranded > 0 {
                        Chip(text: stranded == 1 ? "1 reply stranded" : "\(stranded) replies stranded", color: AgentState.blocked.color.opacity(0.75))
                            .help("You replied to these agents after they stopped running; nothing is reading those replies until they are resumed.")
                    }
                    if let g = s.gmail, g.ok == false {
                        Text("read-check off").font(.system(size: 10 * z)).foregroundStyle(.secondary)
                            .help("Can't see what you've read in Gmail (\(g.error ?? "unknown")). Tiles stay green until you reply.")
                    }
                    DesktopChip(model: model)
                    Button { showHeld.toggle() } label: {
                        Label("\(s.mail.heldCount) held", systemImage: "tray.full")
                            .font(.system(size: 11 * z, weight: (s.mail.heldOwner ?? 0) > 0 ? .bold : .regular))
                            .foregroundColor((s.mail.heldOwner ?? 0) > 0 ? Palette.attention : .secondary)
                    }
                    .buttonStyle(.borderless)
                    .help("Mail the mailroom is holding (last 7 days). Bold: some of it is from you.")
                    .popover(isPresented: $showHeld, arrowEdge: .bottom) { HeldList(model: model) }
                }
            }
            ScrollView(.horizontal, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 8) {
                    MachineMeters(metrics: model.metrics)
                    UsageBars(model: model)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .fixedSize(horizontal: false, vertical: true)
    }
}

struct LinkBadge: View {
    @Environment(\.zoom) private var z
    @ObservedObject var model: BoardModel

    var body: some View {
        let (color, text): (Color, String) = {
            switch model.link {
            case .live:
                return (Palette.ok, "\(Settings.host) · \(Fmt.ago(model.lastMessage?.timeIntervalSince1970, now: model.now))")
            case .connecting:
                return (AgentState.waiting.color, "connecting to \(Settings.host)…")
            case .down(let why):
                return (AgentState.blocked.color, why)
            }
        }()
        HStack(spacing: 5) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(text).font(.system(size: 11 * z)).foregroundStyle(.secondary).lineLimit(1)
        }
        .frame(maxWidth: 260, alignment: .leading)
        .help("Data from `\(Settings.collector) stream` over ssh \(Settings.host)")
        .onTapGesture { if case .down = model.link { model.reconnect() } }
    }
}

struct Chip: View {
    @Environment(\.zoom) private var z
    let text: String
    let color: Color
    var body: some View {
        Text(text)
            .font(.system(size: 11 * z, weight: .semibold))
            .foregroundColor(.white)
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(Capsule().fill(color))
    }
}

struct Meter: View {
    @Environment(\.zoom) private var z
    let label: String
    let fraction: Double
    let value: String
    var tint: Color? = nil
    var width: CGFloat = 112
    var dim = false
    var detail: String? = nil
    var onHoverDetail: ((String?) -> Void)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 4) {
                Text(label).font(.system(size: 10 * z, weight: .medium)).foregroundStyle(.secondary).lineLimit(1)
                Spacer(minLength: 2)
                Text(value).font(.system(size: 10 * z).monospacedDigit()).lineLimit(1)
            }
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.primary.opacity(0.1))
                    Capsule().fill(tint ?? meterTint(fraction))
                        .frame(width: max(2, g.size.width * CGFloat(min(1, max(0, fraction)))))
                }
            }
            .frame(height: 5 * z)
        }
        .frame(width: width * z)
        .opacity(dim ? 0.55 : 1)
        .contentShape(Rectangle())
        .help(detail ?? "")
        .onHover { inside in onHoverDetail?(inside ? detail : nil) }
    }
}

struct MachineMeters: View {
    @Environment(\.zoom) private var z
    let metrics: Metrics?

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            if let m = metrics {
                if let mem = m.mem, mem.total > 0 {
                    Meter(label: "RAM", fraction: mem.used / mem.total,
                          value: "\(Fmt.bytes(mem.used)) / \(Fmt.bytes(mem.total))")
                        .help("Used = total − available. Swap used \(Fmt.bytes(mem.swapUsed)) of \(Fmt.bytes(mem.swapTotal)).")
                }
                if let c = m.cpu, let pct = c.pct {
                    Meter(label: "CPU", fraction: pct / 100, value: String(format: "%.0f%% · %.1f", pct, c.load1 ?? 0), width: 84)
                        .help("CPU busy across \(c.ncpu ?? 0) threads; load average \(c.load1 ?? 0)")
                }
                ForEach(Array(m.gpus.enumerated()), id: \.offset) { _, g in
                    if let used = g.memUsed, let total = g.memTotal, total > 0 {
                        Meter(label: "GPU", fraction: used / total,
                              value: "\(Fmt.bytes(used)) · \(Int(g.util ?? 0))%")
                            .help("\(g.name): \(Fmt.bytes(used)) of \(Fmt.bytes(total)) memory, \(Int(g.util ?? 0))% util, \(Int(g.temp ?? 0))°C")
                    }
                }
                ForEach(m.disks) { d in
                    Meter(label: "\(d.kind) \(d.label)", fraction: d.total > 0 ? d.used / d.total : 0,
                          value: "\(Fmt.bytes(d.free)) free")
                        .help("\(d.mount) on \(d.device) (\(d.model)): \(Fmt.bytes(d.free)) free of \(Fmt.bytes(d.total))")
                }
            } else {
                Text("waiting for metrics…").font(.system(size: 11 * z)).foregroundStyle(.secondary)
            }
        }
    }
}

struct UsageBars: View {
    @Environment(\.zoom) private var z
    @ObservedObject var model: BoardModel

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            if let u = model.usage, !u.accounts.isEmpty {
                ForEach(u.accounts) { acct in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(accountTitle(acct))
                            .font(.system(size: 10 * z, weight: .semibold)).lineLimit(1)
                        if (acct.windows ?? []).isEmpty, let err = acct.error {
                            Text(err.hasPrefix("no login") ? "not logged in yet" : err)
                                .font(.system(size: 10 * z)).foregroundStyle(.secondary).lineLimit(1)
                                .frame(maxWidth: 200 * z, alignment: .leading)
                        }
                        HStack(spacing: 8) {
                            ForEach(acct.windows ?? []) { w in
                                let left = w.remainingPct ?? (100 - (w.usedPct ?? 0))
                                Meter(label: shortWindow(w), fraction: (100 - left) / 100,
                                      value: String(format: "%.0f%% left", left),
                                      tint: left <= 10 ? AgentState.blocked.color : (left <= 25 ? Palette.attention : Palette.ok),
                                      width: 96,
                                      detail: "\(accountTitle(acct)) · \(w.name ?? w.kind ?? "window"): \(Int(left))% left · resets \(Fmt.resetAt(w.resetsAt, now: model.now))",
                                      onHoverDetail: { model.hoverInfo = $0 })
                            }
                        }
                    }
                    .contentShape(Rectangle())
                    .onHover { inside in
                        model.hoverInfo = inside ? (acct.error ?? accountTitle(acct) + " · " + (acct.windows ?? []).map { w in
                            "\(w.name ?? w.kind ?? "window") resets \(Fmt.resetAt(w.resetsAt, now: model.now))"
                        }.joined(separator: " · ")) : nil
                    }
                }
            } else {
                Text(model.usage?.error ?? "no usage data yet").font(.system(size: 11 * z)).foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 2) {
                Button { model.refreshUsage() } label: {
                    Image(systemName: "arrow.clockwise").font(.system(size: 11 * z))
                        .rotationEffect(.degrees(model.usageRefreshing ? 180 : 0))
                }
                .buttonStyle(.borderless)
                .help("Refresh plan usage now")
                if let rec = model.usage?.recommendation, let use = rec.use, !use.isEmpty {
                    Text("next agent → \(use.replacingOccurrences(of: "claude:", with: ""))")
                        .font(.system(size: 9 * z, weight: .semibold)).foregroundStyle(.secondary)
                        .help((rec.line ?? "") + "\nagent-spawn routes each NEW agent by usage.order; running agents keep their login.")
                }
                if let u = model.usage {
                    Text(u.source == "agent-usage" ? Fmt.ago(u.observedAt, now: model.now) : "cache \(Fmt.ago(u.observedAt, now: model.now))")
                        .font(.system(size: 9 * z)).foregroundStyle(.secondary)
                        .help(u.source == "agent-usage" ? "From agent-usage --json" :
                              "Stand-in: agent-usage is not installed yet, so this is its cache file as last written.")
                }
            }
        }
    }

    func accountTitle(_ a: UsageAccount) -> String {
        // "Personal Claude (Max plan; the default login, ~/.claude)" -> "Personal Claude"
        var label = a.label ?? a.target ?? "?"
        if let cut = label.firstIndex(of: "(") { label = String(label[..<cut]).trimmingCharacters(in: .whitespaces) }
        return a.plan.map { "\(label) · \($0)" } ?? label
    }

    func shortWindow(_ w: UsageWindow) -> String {
        switch w.kind ?? "" {
        case "session": return "5-hour"
        case "weekly": return "weekly"
        case "model_weekly": return "\(w.model?.capitalized ?? "model") wk"
        default: return w.name ?? w.kind ?? "?"
        }
    }
}

struct HeldList: View {
    @Environment(\.zoom) private var z
    @ObservedObject var model: BoardModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Held mail, last 7 days").font(.headline)
            Text("Release one on the desktop with  agent-mail release N SESSION").font(.system(size: 11 * z)).foregroundStyle(.secondary)
            ScrollView {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(model.snapshot?.mail.held ?? []) { h in
                        VStack(alignment: .leading, spacing: 1) {
                            HStack {
                                Text("#\(h.id)").font(.system(size: 11 * z).monospacedDigit()).foregroundStyle(.secondary)
                                Text(h.subject).font(.system(size: 12 * z, weight: h.owner ? .semibold : .regular)).lineLimit(1)
                                Spacer()
                                Text(Fmt.ago(h.ts, now: model.now)).font(.system(size: 11 * z).monospacedDigit()).foregroundStyle(.secondary)
                            }
                            Text("\(h.from) · \(h.reason)").font(.system(size: 10 * z)).foregroundStyle(.secondary).lineLimit(2)
                        }
                        .padding(6)
                        .background(RoundedRectangle(cornerRadius: 5).fill(h.owner ? Palette.attention.opacity(0.15) : Color.primary.opacity(0.04)))
                    }
                }
            }
        }
        .padding(12)
        .frame(width: 460, height: 340)
    }
}

// MARK: - board

func tileColumns(_ z: CGFloat) -> [GridItem] { [GridItem(.adaptive(minimum: 172 * z, maximum: 240 * z), spacing: 8 * z)] }

struct BoardGrid: View {
    @ObservedObject var model: BoardModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if model.snapshot == nil {
                    Text(model.link == .connecting ? "Connecting to \(Settings.host)…" : "No data yet.")
                        .foregroundStyle(.secondary).padding(.top, 40).frame(maxWidth: .infinity)
                }
                ForEach(model.sections) { section in
                    SectionView(section: section, model: model)
                }
                if model.snapshot != nil { NewGroupZone(model: model) }
                if !model.freeWorktrees.isEmpty { WorktreeSection(model: model) }
            }
            .padding(12)
        }
        .onTapGesture { model.selectedID = nil }
    }
}

struct SectionView: View {
    @Environment(\.zoom) private var z
    let section: BoardSection
    @ObservedObject var model: BoardModel
    @State private var targeted = false

    var isGroupLike: Bool { section.kind == .group || section.kind == .subgroup }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            header
            if !section.collapsed {
                if section.agents.isEmpty && section.subsections.isEmpty && isGroupLike {
                    Text("Drag tiles here")
                        .font(.system(size: 11 * z)).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, minHeight: 50)
                        .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4])).foregroundStyle(.tertiary))
                } else if !section.agents.isEmpty {
                    LazyVGrid(columns: tileColumns(z), alignment: .leading, spacing: 8 * z) {
                        ForEach(section.agents) { a in
                            DraggableTile(agent: a, model: model)
                        }
                    }
                }
                if section.kind == .group, let gid = section.groupID, !section.subsections.isEmpty {
                    // sub-groups side by side; they wrap onto a new row when there are more than fit
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 230 * z), spacing: 10 * z, alignment: .top)],
                              alignment: .leading, spacing: 10 * z) {
                        ForEach(section.subsections) { sub in
                            SectionView(section: sub, model: model)
                                .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.035)))
                        }
                        NewSubgroupZone(parent: gid, model: model)
                    }
                }
            }
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 9).fill(targeted ? Color.accentColor.opacity(0.12) : Color.clear))
        .dropDestination(for: String.self) { ids, _ in
            guard let id = ids.first else { return false }
            if let gid = BoardModel.groupPayload(id) {
                // onto a group or sub-group: just before it, at its level; anywhere else: last top-level group
                model.moveGroup(gid, before: isGroupLike ? section.groupID : nil)
                return true
            }
            guard section.kind != .offline else { return false }
            model.move(id, toGroup: section.groupID)
            return true
        } isTargeted: { targeted = $0 }
    }

    var header: some View {
        HStack(spacing: 6) {
            if section.kind != .ungrouped {
                Button { model.toggleCollapsed(section) } label: {
                    Image(systemName: section.collapsed ? "chevron.right" : "chevron.down")
                        .font(.system(size: 9 * z, weight: .bold)).frame(width: 10)
                }
                .buttonStyle(.borderless)
            }
            Group {
                if isGroupLike, let gid = section.groupID {
                    Text(section.title.uppercased())
                        .draggable("group:" + gid.uuidString) {
                            Text(section.title.uppercased()).font(.system(size: 11 * z, weight: .semibold))
                                .padding(6).background(RoundedRectangle(cornerRadius: 5).fill(Color.accentColor.opacity(0.3)))
                        }
                        .help(section.kind == .subgroup ? "Drag to reorder, or onto another group" : "Drag to reorder groups")
                } else {
                    Text(section.title.uppercased())
                }
            }
            .font(.system(size: (section.kind == .subgroup ? 9.5 : 10) * z, weight: .semibold)).tracking(0.6).foregroundStyle(.secondary)
            Text("\(section.agents.count + section.subsections.reduce(0) { $0 + $1.agents.count })")
                .font(.system(size: 10 * z).monospacedDigit()).foregroundStyle(.tertiary)
            Spacer()
            if isGroupLike, let gid = section.groupID, let group = model.layout.find(gid) {
                Menu {
                    Button("Rename…") { Dialogs.rename(group: group, model: model) }
                    if section.kind == .group {
                        Button("New sub-group…") { Dialogs.newGroup(model: model, parent: gid) }
                        Button("Move up") { model.moveGroup(gid, by: -1) }
                        Button("Move down") { model.moveGroup(gid, by: 1) }
                        Divider()
                        Button("Delete group (tiles stay)") { model.deleteGroup(gid) }
                    } else {
                        Button("Move left") { model.moveGroup(gid, by: -1) }
                        Button("Move right") { model.moveGroup(gid, by: 1) }
                        Button("Make it a group of its own") { model.promoteGroup(gid) }
                        Divider()
                        Button("Delete sub-group (tiles stay in the group)") { model.deleteGroup(gid) }
                    }
                } label: { Image(systemName: "ellipsis") }
                .menuStyle(.borderlessButton).menuIndicator(.hidden).frame(width: 22)
            }
        }
    }
}

struct NewSubgroupZone: View {
    let parent: UUID
    @ObservedObject var model: BoardModel
    @Environment(\.zoom) private var z
    @State private var targeted = false

    var body: some View {
        Button { Dialogs.newGroup(model: model, parent: parent) } label: {
            Label("sub-group", systemImage: "plus")
                .font(.system(size: 10 * z)).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, minHeight: 40 * z)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4]))
            .foregroundStyle(targeted ? Color.accentColor : Color.secondary.opacity(0.4)))
        .help("New sub-group: click, or drop a tile or a group's name here")
        .dropDestination(for: String.self) { ids, _ in
            guard let id = ids.first else { return false }
            if let gid = BoardModel.groupPayload(id) { model.moveGroup(gid, into: parent); return true }
            model.newGroup(with: id, parent: parent)
            return true
        } isTargeted: { targeted = $0 }
    }
}

// MARK: - worktrees with no agent

struct WorktreeSection: View {
    @ObservedObject var model: BoardModel
    @Environment(\.zoom) private var z

    var body: some View {
        let items = model.freeWorktrees
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Button { model.toggleWorktreesCollapsed() } label: {
                    Image(systemName: model.layout.worktreesCollapsed ? "chevron.right" : "chevron.down")
                        .font(.system(size: 9 * z, weight: .bold)).frame(width: 10)
                }
                .buttonStyle(.borderless)
                Text("WORKTREES WITH NO AGENT").font(.system(size: 10 * z, weight: .semibold)).tracking(0.6).foregroundStyle(.secondary)
                Text("\(items.count)").font(.system(size: 10 * z).monospacedDigit()).foregroundStyle(.tertiary)
                let merged = items.filter { $0.git?.stage == "merged" || $0.git?.stage == "clean" }.count
                if merged > 0 {
                    Text("· \(merged) merged").font(.system(size: 10 * z)).foregroundStyle(.tertiary)
                        .help("PR merged into main, or nothing beyond main. agent-close has the final word on what is removable.")
                }
                Spacer()
                Button { Dialogs.cleanupLanded(model: model) } label: {
                    Label("Clean up all fully landed…", systemImage: "trash").font(.system(size: 11 * z))
                }
                .buttonStyle(.borderless)
                .help("Runs agent-close's dry run on every worktree here, shows what would go, and on confirm removes only the fully landed ones (never forced)")
            }
            .help("Linked git worktrees of the repos under the repos root that no agent is working in. Right-click one to clean it up, force remove it, or start an agent in it.")
            if !model.layout.worktreesCollapsed {
                LazyVGrid(columns: tileColumns(z), alignment: .leading, spacing: 8 * z) {
                    ForEach(items) { w in
                        WorktreeTile(wt: w, selected: model.selectedID == w.id, now: model.now)
                            .onTapGesture { model.selectedID = model.selectedID == w.id ? nil : w.id }
                            .contextMenu { WorktreeMenu(wt: w, model: model) }
                    }
                }
            }
        }
        .padding(6)
    }
}

struct WorktreeMenu: View {
    let wt: FreeWorktree
    @ObservedObject var model: BoardModel

    var body: some View {
        Button("Start agent here…") { Dialogs.wtSpawn(wt, model: model) }
        Button("Clean up…") { Dialogs.wtCleanup(wt, model: model) }
        Button("Force remove…", role: .destructive) { Dialogs.wtForce(wt, model: model) }
        Divider()
        Button("Copy path") { copyToPasteboard(wt.path) }
    }
}

struct WorktreeTile: View {
    let wt: FreeWorktree
    let selected: Bool
    let now: Date
    @Environment(\.zoom) private var z

    var body: some View {
        VStack(alignment: .leading, spacing: 3 * z) {
            HStack(spacing: 5) {
                Image(systemName: "folder").font(.system(size: 10 * z))
                Text(wt.name).font(.system(size: 12 * z, weight: .semibold)).lineLimit(1)
                Spacer(minLength: 2)
                if wt.locked == true { Image(systemName: "lock.fill").font(.system(size: 9 * z)).help("git worktree is locked") }
            }
            Text(wt.repoBranch).font(.system(size: 10 * z)).lineLimit(1).opacity(0.85)
            HStack(spacing: 5) {
                Text(wt.lastCommit?.ts.map { Fmt.ago($0, now: now) } ?? "-").font(.system(size: 10 * z).monospacedDigit())
                    .help("last commit")
                ForEach(wt.git?.chips ?? [], id: \.self) { chip in
                    Text(chip)
                        .font(.system(size: 9 * z, weight: .semibold).monospacedDigit())
                        .padding(.horizontal, 4).padding(.vertical, 1)
                        .background(Capsule().strokeBorder(lineWidth: 1).opacity(0.45))
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
                Text(landedLabel).font(.system(size: 9 * z, weight: .medium)).opacity(0.8).lineLimit(1)
            }
        }
        .foregroundColor(Color.primary.opacity(0.8))
        .padding(.horizontal, 9 * z).padding(.vertical, 7 * z)
        .frame(maxWidth: .infinity, minHeight: 64 * z, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: 7).fill(Color.primary.opacity(0.03)))
        .overlay(RoundedRectangle(cornerRadius: 7)
            .strokeBorder(style: StrokeStyle(lineWidth: selected ? 2 : 1, dash: selected ? [] : [1, 3]))
            .foregroundColor(selected ? Color.primary : Color.secondary.opacity(0.6)))
        .contentShape(RoundedRectangle(cornerRadius: 7))
        .help(wt.path + ((wt.git?.summary).map { "\n" + $0 } ?? ""))
    }

    var landedLabel: String {
        switch wt.git?.stage ?? "" {
        case "merged", "clean": return "landed"
        case "uncommitted": return "uncommitted"
        case "unpushed": return "unpushed"
        case "pr": return "PR open"
        case "pushed": return wt.git?.pr?.state?.uppercased() == "MERGED" ? "new since merge" : "no PR"
        default: return ""
        }
    }
}

struct WorktreeDetail: View {
    let wt: FreeWorktree
    @ObservedObject var model: BoardModel
    @Environment(\.zoom) private var z

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 5) {
                    HStack(spacing: 7) {
                        Image(systemName: "folder").font(.system(size: 14 * z))
                        Text(wt.name).font(.system(size: 15 * z, weight: .semibold)).lineLimit(2)
                        Spacer()
                        Button { model.selectedID = nil } label: { Image(systemName: "xmark") }.buttonStyle(.borderless)
                    }
                    Text("A worktree with no agent in it").font(.system(size: 12 * z)).foregroundStyle(.secondary)
                }
                HStack(spacing: 8) {
                    Button("Start agent…") { Dialogs.wtSpawn(wt, model: model) }
                    Button("Clean up…") { Dialogs.wtCleanup(wt, model: model) }
                    Button("Force remove…") { Dialogs.wtForce(wt, model: model) }
                }
                .controlSize(.small)
                DetailSection(title: "Facts") {
                    Fact("Repo", wt.repoBranch)
                    HStack(alignment: .top) {
                        Text("Path").frame(width: 62 * z, alignment: .leading).foregroundStyle(.secondary)
                        Text(wt.path).font(.system(size: 10 * z, design: .monospaced)).textSelection(.enabled)
                        Button { copyToPasteboard(wt.path); model.show("Copied path.") } label: { Image(systemName: "doc.on.doc") }
                            .buttonStyle(.borderless)
                    }.font(.system(size: 11 * z))
                    if let c = wt.lastCommit, let h = c.hash {
                        Fact("Commit", "\(h) \(c.subject ?? "")" + (c.ts.map { " (\(Fmt.ago($0, now: model.now)) ago)" } ?? ""))
                    }
                    if let t = wt.tmux, !t.isEmpty { Fact("tmux", "'\(t)' still open, with no agent in it") }
                    if wt.locked == true { Fact("Locked", "git worktree lock is set (git worktree unlock to clear)") }
                }
                if let g = wt.git {
                    DetailSection(title: "Git") {
                        GitPipeline(stage: g.stage)
                        Fact("State", g.summary)
                        if let pr = g.pr, let n = pr.number, let url = pr.url, let link = URL(string: url) {
                            HStack(alignment: .top) {
                                Text("PR").frame(width: 62 * z, alignment: .leading).foregroundStyle(.secondary)
                                Link("#\(n) \(pr.title ?? "")", destination: link).lineLimit(2)
                            }.font(.system(size: 11 * z))
                        }
                    }
                }
                Text("Clean up… removes it only if everything is landed (agent-close's own checks); otherwise nothing changes. Start agent… is for when it needs some work first, e.g. \"land what's worth keeping, then clean this up\".")
                    .font(.system(size: 10 * z)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            .padding(12)
        }
    }
}

struct NewGroupZone: View {
    @Environment(\.zoom) private var z
    @ObservedObject var model: BoardModel
    @State private var targeted = false

    var body: some View {
        Button { Dialogs.newGroup(model: model) } label: {
            Label("New group — or drop a tile here", systemImage: "plus")
                .font(.system(size: 11 * z)).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, minHeight: 34)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4]))
            .foregroundStyle(targeted ? Color.accentColor : Color.secondary.opacity(0.4)))
        .dropDestination(for: String.self) { ids, _ in
            guard let id = ids.first else { return false }
            if let gid = BoardModel.groupPayload(id) { model.moveGroup(gid, before: nil); return true }
            model.newGroup(with: id)
            return true
        } isTargeted: { targeted = $0 }
    }
}

struct DraggableTile: View {
    let agent: Agent
    @ObservedObject var model: BoardModel
    @State private var targeted = false

    var body: some View {
        TileView(agent: agent, selected: model.selectedID == agent.id, now: model.now)
            .overlay(alignment: .leading) {
                if targeted { Capsule().fill(Color.accentColor).frame(width: 3).offset(x: -5) }
            }
            .onTapGesture(count: 2) { model.selectedID = agent.id; model.openLatestMail(agent) }
            .onTapGesture { model.selectedID = model.selectedID == agent.id ? nil : agent.id }
            .draggable(agent.id) {
                TileView(agent: agent, selected: false, now: model.now).frame(width: 180)
            }
            .dropDestination(for: String.self) { ids, _ in
                guard let id = ids.first else { return false }
                if let gid = BoardModel.groupPayload(id) {        // a group dropped on a tile: before that tile's group
                    model.moveGroup(gid, before: model.layout.group(of: agent.id))
                    return true
                }
                model.move(id, before: agent.id)
                return true
            } isTargeted: { targeted = $0 }
            .contextMenu { TileMenu(agent: agent, model: model) }
    }
}

struct TileMenu: View {
    let agent: Agent
    @ObservedObject var model: BoardModel

    var body: some View {
        if agent.live {
            Button("Close…") { Dialogs.close(agent, model: model) }
        } else {
            Button("Resume…") { Dialogs.resume(agent, model: model) }
            Button("Clean up…") { Dialogs.cleanup(agent, model: model) }
        }
        Button("Force close…", role: .destructive) { Dialogs.forceClose(agent, model: model) }
        Divider()
        Button("Send message…") { Dialogs.send(agent, model: model) }.disabled(!agent.live)
        if agent.live {
            Button("Change agent…") { model.switching = SwitchSession(agent: agent, modelOnly: false) }
            Button("Change model/effort…") { model.switching = SwitchSession(agent: agent, modelOnly: true) }
        }
        Button("Open latest mail in Gmail") { model.openLatestMail(agent) }.disabled(model.latestMailURL(for: agent) == nil)
        Button("Open in Kitty") { Kitty.open(agent, model: model) }.disabled(!Kitty.canOpen(agent))
        let attach = model.attachCommand(for: agent)
        Button("Copy attach command") { copyToPasteboard(attach ?? "") }.disabled(attach == nil)
        Button("Copy session id") { copyToPasteboard(agent.id) }
        Divider()
        Menu("Move to group") {
            ForEach(model.layout.groups) { g in
                Button(g.name) { model.move(agent.id, toGroup: g.id) }
                ForEach(g.subgroups) { sub in
                    Button("    ↳ " + sub.name) { model.move(agent.id, toGroup: sub.id) }
                }
            }
            if !model.layout.groups.isEmpty { Divider() }
            Button("Ungrouped") { model.move(agent.id, toGroup: nil) }
            Button("New group…") { Dialogs.newGroup(model: model, with: agent.id) }
        }
    }
}

func copyToPasteboard(_ s: String) {
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(s, forType: .string)
}

func tileColor(_ a: Agent) -> Color {
    let st = a.agentState
    guard st == .idle, let fresh = a.fresh, fresh < 1 else { return st.color }
    // idle fades toward stale grey as it ages (fresh runs 1 → 0.45)
    let t = min(1, max(0, (1 - fresh) / 0.55))
    let from = AgentState.idle.hex, to = AgentState.stale.hex
    func ch(_ v: UInt32, _ s: UInt32) -> Double { Double((v >> s) & 0xFF) / 255 }
    let mix = { (s: UInt32) in ch(from, s) * (1 - t) + ch(to, s) * t }
    return Color(.sRGB, red: mix(16), green: mix(8), blue: mix(0), opacity: 1)
}

struct TileView: View {
    @Environment(\.zoom) private var z
    let agent: Agent
    let selected: Bool
    let now: Date

    var body: some View {
        let st = agent.agentState
        let offline = st == .offline
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 5) {
                if st == .busy { PulseDot() }
                if st == .background { Image(systemName: "hourglass").font(.system(size: 9 * z)).help(agent.reason) }
                Text(agent.display).font(.system(size: 12 * z, weight: .semibold)).lineLimit(1)
                Spacer(minLength: 2)
                if agent.mail.unanswered {
                    if agent.mail.read == true {
                        Image(systemName: "envelope.open").font(.system(size: 9 * z))
                            .help("You've read its last mail; no reply yet")
                    } else {
                        Image(systemName: "envelope.fill").font(.system(size: 9 * z))
                            .help(agent.mail.read == false ? "Its last mail to you is unread" : "Its last mail to you has no reply yet")
                    }
                }
                if (agent.mail.held ?? 0) > 0 {
                    Image(systemName: "tray.full.fill").font(.system(size: 9 * z)).help("A reply to it is held")
                }
                if let subs = agent.subagents, !subs.isEmpty {
                    Text("+\(subs.count)").font(.system(size: 9 * z, weight: .bold))
                        .help("\(subs.count) Codex subagent\(subs.count == 1 ? "" : "s") in this session: "
                              + subs.map { $0.nickname }.joined(separator: ", ") + ". Click the tile to see them.")
                }
            }
            Text(agent.repoBranch).font(.system(size: 10 * z)).lineLimit(1).opacity(0.85)
            HStack(spacing: 5) {
                Text(agent.cli + ((agent.accountDefault ?? true) || (agent.account ?? "").isEmpty ? "" : " · \(agent.account ?? "")"))
                    .font(.system(size: 9 * z, weight: .bold))
                    .padding(.horizontal, 4).padding(.vertical, 1)
                    .background(Capsule().fill(Color.black.opacity(offline ? 0.08 : 0.22)))
                Text(Fmt.ago(agent.lastActivity, now: now)).font(.system(size: 10 * z).monospacedDigit())
                ForEach(agent.git?.chips ?? [], id: \.self) { chip in
                    Text(chip)
                        .font(.system(size: 9 * z, weight: .semibold).monospacedDigit())
                        .padding(.horizontal, 4).padding(.vertical, 1)
                        .background(Capsule().strokeBorder(lineWidth: 1).opacity(0.45))
                        .lineLimit(1)
                }
                .help(agent.git?.summary ?? "")
                Spacer(minLength: 0)
                Text(tileStateLabel).font(.system(size: 9 * z, weight: .medium)).opacity(0.8).lineLimit(1)
            }
        }
        .foregroundColor(offline ? Color.primary.opacity(0.75) : .white)
        .padding(.horizontal, 9 * z).padding(.vertical, 7 * z)
        .frame(maxWidth: .infinity, minHeight: 64 * z, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: 7).fill(offline ? Color.primary.opacity(0.06) : tileColor(agent)))
        .overlay(
            RoundedRectangle(cornerRadius: 7)
                .strokeBorder(style: StrokeStyle(lineWidth: selected ? 2 : 1, dash: offline && !selected ? [4, 3] : []))
                .foregroundColor(selected ? Color.primary : ((agent.stranded ?? false) ? AgentState.blocked.color
                                 : (offline ? Color.secondary.opacity(0.5) : Color.white.opacity(0.10))))
        )
        .overlay(alignment: .topTrailing) {
            if let note = agent.loginNote, !note.isEmpty {
                // on a later login in usage.order while the first has room for its model
                Image(systemName: "star.fill")
                    .font(.system(size: 9 * z, weight: .bold))
                    .foregroundColor(Color(red: 0.86, green: 0.12, blue: 0.12))
                    .shadow(color: .black.opacity(0.35), radius: 0.5)
                    .offset(x: 3 * z, y: -3 * z)
                    .help(note)
            }
        }
        .contentShape(RoundedRectangle(cornerRadius: 7))
        .help(tooltip)
    }

    var tileStateLabel: String {
        if agent.stranded ?? false { return "reply stranded" }
        if agent.agentState == .offline {
            if agent.closed ?? false { return "closed" }
            if let t = agent.leftBehind?.tmux, !t.isEmpty { return "exited · tmux open" }
            return "exited"
        }
        switch agent.agentState {
        case .waiting: return "you"
        case .busy: return "busy"
        case .background: return "bg wait"
        default: return agent.agentState.label
        }
    }

    var tooltip: String {
        var lines = ["\(agent.agentState.label): \(agent.reason)"]
        if let g = agent.git, !g.summary.isEmpty { lines.append("git: \(g.summary)") }
        if let n = agent.lastNote {
            lines.append("Last note, \(Fmt.ago(n.ts, now: now)) ago: \(n.text)")
        }
        lines.append(agent.cwd)
        return lines.joined(separator: "\n")
    }
}

struct PulseDot: View {
    @Environment(\.zoom) private var z
    @State private var on = false
    var body: some View {
        Circle().fill(Color.white).frame(width: 6 * z, height: 6 * z)
            .opacity(on ? 1 : 0.3)
            .onAppear { withAnimation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true)) { on = true } }
    }
}

// MARK: - detail panel

struct DetailView: View {
    @Environment(\.zoom) private var z
    let agent: Agent
    @ObservedObject var model: BoardModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 5) {
                    HStack(spacing: 7) {
                        RoundedRectangle(cornerRadius: 3).fill(agent.agentState == .offline ? Color.secondary : tileColor(agent))
                            .frame(width: 12, height: 12)
                        Text(agent.display).font(.system(size: 15 * z, weight: .semibold)).lineLimit(2)
                        Spacer()
                        Button { model.selectedID = nil } label: { Image(systemName: "xmark") }.buttonStyle(.borderless)
                    }
                    Text("\(agent.agentState.label) — \(agent.reason)")
                        .font(.system(size: 12 * z)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 8) {
                    Button("Message…") { Dialogs.send(agent, model: model) }.disabled(!agent.live)
                    if agent.live {
                        Button("Switch…") { model.switching = SwitchSession(agent: agent, modelOnly: false) }
                            .help("Change its CLI, Claude login, model or effort")
                    }
                    if agent.live {
                        Button("Close…") { Dialogs.close(agent, model: model) }
                    } else {
                        Button("Resume…") { Dialogs.resume(agent, model: model) }
                            .help("Start it again in tmux: same conversation, model and login")
                        Button("Clean up…") { Dialogs.cleanup(agent, model: model) }
                            .help("agent-close without --force: removes the worktree only if all work is landed")
                    }
                    Button("Force close…") { Dialogs.forceClose(agent, model: model) }
                }
                .controlSize(.small)

                if !agent.live {
                    DetailSection(title: "Offline") {
                        Fact("Process", (agent.closed ?? false) ? "closed with agent-close" : "not running (exited or killed)")
                        let left = agent.leftBehind
                        Fact("tmux", (left?.tmux ?? "").isEmpty ? "no tmux session left" : "'\(left?.tmux ?? "")' still open, with no agent in it")
                        let unlanded = (agent.git?.chips ?? []).isEmpty ? "" : "; " + (agent.git?.summary ?? "")
                        Fact("Worktree", !(left?.cwdExists ?? false) ? "gone"
                             : agent.git?.linked == true ? "linked worktree, still there" + unlanded
                             : agent.git?.linked == false ? "main checkout, still there (agent-close never removes one)" + unlanded
                             : "directory still there")
                        Text("Nothing is cleaned up when an agent dies. Clean up… runs agent-close, which removes a linked worktree only once all its work is landed (never a main checkout) and otherwise refuses. Force close… discards unlanded work. Leaving it is fine too: offline tiles drop off the board by themselves.")
                            .font(.system(size: 10 * z)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                }

                DetailSection(title: "Facts") {
                    Fact("CLI", agent.cli + (agent.model.map { $0.isEmpty ? "" : " · \($0)" } ?? "") + (agent.kind == "bg" ? " · background" : ""))
                    if let acct = agent.account, !acct.isEmpty { Fact("Login", acct + ((agent.accountDefault ?? true) ? " (default)" : "")) }
                    Fact("Repo", agent.repoBranch)
                    Fact("Active", Fmt.ago(agent.lastActivity, now: model.now) + " ago" + ((agent.turns24h ?? 0) > 0 ? " · \(agent.turns24h ?? 0) turns in 24h" : ""))
                    if let s = agent.startedAt, s > 0 { Fact("Started", Fmt.clock(s)) }
                    Fact("Dir", agent.cwd)
                    if let cmd = model.attachCommand(for: agent) {
                        HStack(alignment: .top) {
                            Text("Attach").frame(width: 62 * z, alignment: .leading).foregroundStyle(.secondary)
                            Text(cmd).font(.system(size: 10 * z, design: .monospaced)).textSelection(.enabled)
                            Button { copyToPasteboard(cmd); model.show("Copied attach command.") } label: {
                                Image(systemName: "doc.on.doc")
                            }.buttonStyle(.borderless)
                            if Kitty.canOpen(agent) {
                                Button { Kitty.open(agent, model: model) } label: { Image(systemName: "terminal") }
                                    .buttonStyle(.borderless).help("Open in Kitty")
                            }
                        }.font(.system(size: 11 * z))
                    }
                    HStack(alignment: .top) {
                        Text("Session").frame(width: 62 * z, alignment: .leading).foregroundStyle(.secondary)
                        Text(agent.id).font(.system(size: 10 * z, design: .monospaced)).textSelection(.enabled)
                    }.font(.system(size: 11 * z))
                }

                if let subs = agent.subagents, !subs.isEmpty {
                    DetailSection(title: "Subagents (\(subs.count))") {
                        ForEach(subs, id: \.self) { sub in
                            HStack(alignment: .top, spacing: 6) {
                                Circle().fill(sub.busy ? AgentState.busy.color : AgentState.idle.color).frame(width: 7 * z, height: 7 * z)
                                    .padding(.top, 4 * z)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(sub.nickname).font(.system(size: 12 * z, weight: .semibold))
                                    Text((sub.path.isEmpty ? "" : sub.path + " · ") + (sub.busy ? "working" : "idle")
                                         + (sub.lastActivity > 0 ? " · active \(Fmt.ago(sub.lastActivity, now: model.now)) ago" : ""))
                                        .font(.system(size: 10 * z)).foregroundStyle(.secondary)
                                }
                            }
                        }
                        Text("Codex subagents run inside this agent's process and end with it. Messages go to the parent.")
                            .font(.system(size: 10 * z)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                }

                if let g = agent.git {
                    DetailSection(title: "Git") {
                        GitPipeline(stage: g.stage)
                        Fact("State", g.summary)
                        if let up = g.upstream, !up.isEmpty { Fact("Upstream", up) }
                        if let c = agent.lastCommit, let h = c.hash {
                            Fact("Commit", "\(h) \(c.subject ?? "")" + (c.ts.map { " (\(Fmt.ago($0, now: model.now)) ago)" } ?? ""))
                        }
                        if let pr = g.pr, let n = pr.number, let url = pr.url, let link = URL(string: url) {
                            HStack(alignment: .top) {
                                Text("PR").frame(width: 62 * z, alignment: .leading).foregroundStyle(.secondary)
                                Link("#\(n) \(pr.title ?? "")", destination: link).lineLimit(2)
                            }.font(.system(size: 11 * z))
                        }
                    }
                }

                if let threads = agent.mail.threads, !threads.isEmpty {
                    DetailSection(title: "Mail with you") {
                        ForEach(threads, id: \.self) { t in
                            HStack(alignment: .top, spacing: 6) {
                                Image(systemName: (t.unanswered ?? false) ? "envelope.badge" : "envelope.open")
                                    .foregroundColor((t.unanswered ?? false) ? AgentState.waiting.color : .secondary)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(t.subject).font(.system(size: 11 * z)).lineLimit(2)
                                    Text(mailLine(t)).font(.system(size: 10 * z)).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }

                DetailSection(title: "Notes") {
                    if agent.notes.isEmpty {
                        Text("No agent-status notes in the last 48h.").font(.system(size: 11 * z)).foregroundStyle(.secondary)
                    }
                    ForEach(agent.notes.reversed(), id: \.self) { n in
                        VStack(alignment: .leading, spacing: 1) {
                            Text(Fmt.clock(n.ts)).font(.system(size: 10 * z).monospacedDigit()).foregroundStyle(.secondary)
                            Text(n.text).font(.system(size: 12 * z)).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                        }
                    }
                }

                DetailSection(title: "Recent events") {
                    ForEach(agent.events.reversed(), id: \.self) { e in
                        HStack(alignment: .top, spacing: 6) {
                            Text(Fmt.clock(e.ts)).font(.system(size: 10 * z).monospacedDigit()).foregroundStyle(.secondary).frame(width: 64 * z, alignment: .leading)
                            Text(eventLabel(e.event)).font(.system(size: 10 * z, weight: .medium)).frame(width: 52 * z, alignment: .leading)
                            Text(e.text ?? "").font(.system(size: 10 * z)).foregroundStyle(.secondary).lineLimit(2)
                        }
                    }
                }
            }
            .padding(12)
        }
    }

    func mailLine(_ t: MailThread) -> String {
        var parts: [String] = []
        if t.lastOut > 0 { parts.append("it wrote \(Fmt.ago(t.lastOut, now: model.now)) ago") }
        if t.lastIn > 0 { parts.append("you wrote \(Fmt.ago(t.lastIn, now: model.now)) ago") }
        return parts.joined(separator: " · ")
    }

    func eventLabel(_ e: String) -> String {
        ["idle": "turn end", "start": "start", "end": "exit", "note": "note", "spawn": "spawned",
         "closed": "closed", "quota": "quota"][e] ?? e
    }
}

/// uncommitted → committed → pushed → PR → merged, with the agent's current step lit.
struct GitPipeline: View {
    @Environment(\.zoom) private var z
    let stage: String
    static let steps = ["uncommitted", "committed", "pushed", "PR", "merged"]

    var current: Int {
        switch stage {
        case "uncommitted": return 0
        case "unpushed": return 1
        case "pushed": return 2
        case "pr", "pr_closed": return 3
        case "merged", "clean": return 4
        default: return -1
        }
    }

    var body: some View {
        HStack(spacing: 3) {
            ForEach(Array(GitPipeline.steps.enumerated()), id: \.offset) { i, step in
                Text(step)
                    .font(.system(size: 9 * z, weight: i == current ? .bold : .regular))
                    .padding(.horizontal, 5).padding(.vertical, 2)
                    .background(Capsule().fill(i == current ? Palette.ok.opacity(0.85) : Color.primary.opacity(i < current ? 0.14 : 0.05)))
                    .foregroundColor(i == current ? .white : (i < current ? .primary : .secondary))
                if i < GitPipeline.steps.count - 1 { Image(systemName: "chevron.right").font(.system(size: 7 * z)).foregroundStyle(.tertiary) }
            }
        }
        .help(stage == "clean" ? "Nothing beyond main" : stage)
    }
}

struct DetailSection<Content: View>: View {
    @Environment(\.zoom) private var z
    let title: String
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title.uppercased()).font(.system(size: 10 * z, weight: .semibold)).tracking(0.6).foregroundStyle(.secondary)
            content
        }
    }
}

struct Fact: View {
    @Environment(\.zoom) private var z
    let label: String
    let value: String
    init(_ label: String, _ value: String) { self.label = label; self.value = value }
    var body: some View {
        HStack(alignment: .top) {
            Text(label).frame(width: 62 * z, alignment: .leading).foregroundStyle(.secondary)
            Text(value).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
        }
        .font(.system(size: 11 * z))
    }
}

// MARK: - overview: what have they been doing

struct OverviewView: View {
    @Environment(\.zoom) private var z
    @ObservedObject var model: BoardModel

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text(summaryLine).font(.system(size: 13 * z)).fixedSize(horizontal: false, vertical: true)
                    ForEach(digestAgents) { a in
                        AgentDigest(agent: a, now: model.now)
                            .contentShape(Rectangle())
                            .onTapGesture { model.selectedID = a.id; model.tab = .board }
                    }
                }
                .padding(14)
            }
            .frame(maxWidth: .infinity)
            Divider()
            VStack(alignment: .leading, spacing: 6) {
                Text("LAST 24 HOURS").font(.system(size: 10 * z, weight: .semibold)).tracking(0.6).foregroundStyle(.secondary)
                ScrollView {
                    VStack(alignment: .leading, spacing: 7) {
                        ForEach(model.snapshot?.timeline ?? [], id: \.self) { t in
                            VStack(alignment: .leading, spacing: 1) {
                                HStack(spacing: 5) {
                                    Text(Fmt.clock(t.ts)).font(.system(size: 10 * z).monospacedDigit()).foregroundStyle(.secondary)
                                    Text(t.name).font(.system(size: 10 * z, weight: .semibold)).lineLimit(1)
                                    if t.event != "note" { Text(t.event).font(.system(size: 9 * z)).foregroundStyle(.secondary) }
                                }
                                if let text = t.text, !text.isEmpty, t.event != "spawn" {
                                    Text(text).font(.system(size: 11 * z)).fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                    }
                }
            }
            .padding(12)
            .frame(width: 360 * z)
        }
    }

    var digestAgents: [Agent] {
        model.agents.sorted {
            ($0.agentState == .offline ? 1 : 0, -($0.lastActivity ?? 0)) < ($1.agentState == .offline ? 1 : 0, -($1.lastActivity ?? 0))
        }
    }

    var summaryLine: String {
        guard let s = model.snapshot else { return "No data yet." }
        let c = model.summary
        var parts = ["\(c("live")) live agents: \(c("busy")) busy, \(c("background")) waiting on background work, \(c("waiting")) waiting on you, \(c("blocked")) blocked, \(c("idle") + c("stale")) idle."]
        parts.append("\(c("turns_24h")) turns and \(c("notes_24h")) notes in the last 24h.")
        if s.mail.heldCount > 0 { parts.append("\(s.mail.heldCount) held mails (\(s.mail.heldOwner ?? 0) from you).") }
        return parts.joined(separator: " ")
    }
}

struct AgentDigest: View {
    @Environment(\.zoom) private var z
    let agent: Agent
    let now: Date

    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            RoundedRectangle(cornerRadius: 3)
                .fill(agent.agentState == .offline ? Color.secondary.opacity(0.5) : tileColor(agent))
                .frame(width: 5)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(agent.display).font(.system(size: 12 * z, weight: .semibold))
                    Text(agent.repoBranch).font(.system(size: 11 * z)).foregroundStyle(.secondary).lineLimit(1)
                    Spacer()
                    Text(Fmt.ago(agent.lastActivity, now: now)).font(.system(size: 10 * z).monospacedDigit()).foregroundStyle(.secondary)
                }
                Text("\(agent.agentState.label) — \(agent.reason)").font(.system(size: 11 * z)).foregroundStyle(.secondary).lineLimit(2)
                ForEach(agent.notes.suffix(3).reversed(), id: \.self) { n in
                    HStack(alignment: .top, spacing: 6) {
                        Text(Fmt.clock(n.ts)).font(.system(size: 10 * z).monospacedDigit()).foregroundStyle(.secondary).frame(width: 64 * z, alignment: .leading)
                        Text(n.text).font(.system(size: 11 * z)).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .padding(.vertical, 2)
    }
}

// MARK: - footer: legend, toast

struct FooterBar: View {
    @Environment(\.zoom) private var z
    @ObservedObject var model: BoardModel

    var body: some View {
        HStack(spacing: 12) {
            ForEach(AgentState.allCases) { s in
                HStack(spacing: 4) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(s == .offline ? Color.secondary.opacity(0.3) : s.color)
                        .frame(width: 10, height: 10)
                    Text("\(s.label) \(model.count(s))").font(.system(size: 10 * z)).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if model.updateAvailable {
                Button("New build ready: relaunch") { model.relaunch() }
                    .controlSize(.small)
                    .help("This app was rebuilt while it was running; relaunch to load the new version")
            }
            HStack(spacing: 2) {
                Button { model.zoom(by: -1) } label: { Image(systemName: "minus.magnifyingglass") }
                    .keyboardShortcut("-", modifiers: .command)
                Button { model.setZoom(1) } label: {
                    Text("\(Int((model.zoom * 100).rounded()))%").font(.system(size: 10 * z).monospacedDigit())
                }
                .keyboardShortcut("0", modifiers: .command)
                Button { model.zoom(by: 1) } label: { Image(systemName: "plus.magnifyingglass") }
                    .keyboardShortcut("=", modifiers: .command)
            }
            .buttonStyle(.borderless)
            .help("Zoom: ⌘+ / ⌘- / ⌘0")
            if let h = model.hoverInfo {
                Text(h).font(.system(size: 11 * z, weight: .medium)).lineLimit(1)
            } else if let t = model.toast {
                Text(t).font(.system(size: 11 * z, weight: .medium)).lineLimit(1)
            } else if let e = model.collectorError {
                Text(e).font(.system(size: 11 * z)).foregroundColor(AgentState.blocked.color).lineLimit(1)
            } else {
                Text("double-click: its latest mail · drag to arrange · right-click for actions · ⌘R · ⌘+/⌘- · ⌥⌘A").font(.system(size: 10 * z)).foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }
}
