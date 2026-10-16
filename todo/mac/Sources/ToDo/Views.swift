import AppKit
import SwiftUI

// MARK: - root

struct RootView: View {
    @ObservedObject var store: Store

    var body: some View {
        VStack(spacing: 0) {
            TopBar(store: store)
            Divider()
            HStack(spacing: 0) {
                Sidebar(store: store).frame(width: 224 * store.zoom)
                Divider()
                MainColumn(store: store).frame(minWidth: 340 * store.zoom, maxWidth: .infinity, maxHeight: .infinity)
                if let t = store.selected {
                    Divider()
                    DetailPanel(task: t, store: store).frame(width: 384 * store.zoom)
                        .id(t.number)
                }
            }
            Divider()
            FooterBar(store: store)
        }
        .frame(minWidth: 860, minHeight: 520)
        .background(Color(nsColor: .windowBackgroundColor))
        .environment(\.zoom, store.zoom)
        .sheet(item: $store.assigning) { req in
            if let t = store.items[req.number] { AssignSheet(task: t, store: store, others: req.others) }
        }
    }
}

// MARK: - top bar

struct TopBar: View {
    @Environment(\.zoom) private var z
    @ObservedObject var store: Store
    @FocusState private var searchFocused: Bool

    var body: some View {
        HStack(spacing: 12) {
            LinkBadge(store: store).frame(width: 250 * z, alignment: .leading)
            Spacer(minLength: 8)
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary).font(.system(size: 12 * z))
                TextField("Search \(store.live.filter { $0.isOpen }.count) open tasks", text: $store.query)
                    .textFieldStyle(.plain)
                    .font(.system(size: 13 * z))
                    .focused($searchFocused)
                    .onSubmit { if let f = store.searchResults().first { store.selection = f.number } }
                    .onExitCommand { store.query = ""; searchFocused = false }
                if !store.query.isEmpty {
                    Button { store.query = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary) }
                        .buttonStyle(.borderless)
                } else {
                    Text("⌘F").font(.system(size: 11 * z)).foregroundStyle(.tertiary)
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 5 * z)
            .background(RoundedRectangle(cornerRadius: 7).fill(Color.primary.opacity(0.07)))
            .frame(maxWidth: 380 * z)
            Spacer(minLength: 8)
            Button { store.undo() } label: {
                Image(systemName: "arrow.uturn.backward").font(.system(size: 12 * z, weight: .medium))
            }
            .buttonStyle(.borderless)
            .disabled(store.undoStack.isEmpty)
            .help((store.undoTitle ?? "Nothing to undo") + " (⌘Z)")
            Button { store.refresh() } label: {
                Image(systemName: "arrow.clockwise").font(.system(size: 12 * z, weight: .medium))
            }
            .buttonStyle(.borderless)
            .help("Check GitHub now (⌘R). ⇧⌘R fetches every task again.")
            if store.activeAgents > 0 {
                Button { store.route = .agents } label: {
                    Text(store.activeAgents == 1 ? "1 with an agent" : "\(store.activeAgents) with agents")
                        .font(.system(size: 11 * z, weight: .semibold)).foregroundColor(.white)
                        .padding(.horizontal, 9).padding(.vertical, 3)
                        .background(Capsule().fill(Palette.caramel))
                }
                .buttonStyle(.plain)
                .help("Tasks an agent is working on")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .onChange(of: store.query) { q in
            if !q.isEmpty { if store.route != .search { store.route = .search } }
            else if store.route == .search { store.route = .list(Store.defaults.string(forKey: "list") ?? "tasks") }
        }
        .onChange(of: store.focusRequest) { f in
            if f == .search { searchFocused = true; store.focusRequest = nil }
        }
    }
}

struct LinkBadge: View {
    @Environment(\.zoom) private var z
    @ObservedObject var store: Store

    var body: some View {
        let waiting = store.pending.count
        let (color, text): (Color, String) = {
            switch store.link {
            case .live:
                let synced = (store.deskSyncedAt ?? store.syncedAt).map { Fmt.ago($0, now: store.now) } ?? "…"
                return (store.syncError == nil ? Palette.ok : Palette.attention,
                        store.syncError.map { "GitHub: \($0)" } ?? "\(Settings.host) · synced \(synced)"
                            + (waiting > 0 ? " · saving \(waiting)" : ""))
            case .connecting:
                return (Palette.attention, "connecting to \(Settings.host)…")
            case .down(let why):
                return (Palette.red, waiting > 0 ? "offline · \(waiting) edit\(waiting == 1 ? "" : "s") waiting" : "offline · \(why)")
            }
        }()
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 7 * z, height: 7 * z)
            Text(text).font(.system(size: 11 * z)).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
        }
        .help({
            if case .down(let why) = store.link { return "Not connected: \(why). Click to retry. Your tasks are cached; edits wait and are sent when the desktop is back." }
            return "Tasks come from GitHub through `\(Settings.helper)` over ssh \(Settings.host)."
        }())
        .onTapGesture { if case .down = store.link { store.reconnect() } }
    }
}

// MARK: - sidebar

struct Sidebar: View {
    @Environment(\.zoom) private var z
    @ObservedObject var store: Store

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 1) {
                if !store.agentTasks.isEmpty {
                    SidebarRow(title: "With agents", count: store.agentTasks.count, icon: "person.crop.circle.badge.clock",
                               selected: store.route == .agents, tint: Palette.caramel) { store.route = .agents }
                        .padding(.bottom, 6)
                }
                let items = store.sidebar
                ForEach(items) { item in
                    switch item {
                    case .list(let name, let open):
                        listRow(name, open, group: nil)
                    case .group(let name, let open, let lists):
                        GroupRow(name: name, count: open, expanded: store.expanded.contains(name)) {
                            if store.expanded.contains(name) { store.expanded.remove(name) } else { store.expanded.insert(name) }
                        }
                        .draggable("group:" + name)
                        .modifier(DropTarget(onHover: { store.expanded.insert(name) }) { payload in dropOnGroup(payload, name) })
                        .contextMenu {
                            Button("New list in \(name)…") { Dialogs.newList(store: store, group: name) }
                        }
                        if store.expanded.contains(name) {
                            ForEach(lists, id: \.name) { l in listRow(l.name, l.open, group: name) }
                        }
                    }
                }
                Text("Drop here to move to the top level")
                    .font(.system(size: 10.5 * z)).foregroundStyle(.tertiary)
                    .frame(maxWidth: .infinity, minHeight: 26 * z)
                    .modifier(DropTarget { payload in dropAtEnd(payload) })
                    .padding(.top, 6)
                Menu {
                    Button("New list…") { Dialogs.newList(store: store, group: nil) }
                    Button("New group…") { Dialogs.newGroup(store: store) }
                } label: {
                    Label("New list", systemImage: "plus").font(.system(size: 12 * z))
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .padding(.top, 6).padding(.leading, 10)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 10)
        }
        .background(Color.primary.opacity(0.025))
    }

    // Drag a list onto a list to put it just before that one (in that list's group), onto a group to put it at
    // the end of that group, or onto the bottom zone for the top level. Drag a group onto anything at the top
    // level to put it before that. Every move is saved to .todo/config.json.
    func dropOnList(_ payload: String, _ target: String, group: String?) -> Bool {
        if payload.hasPrefix("task:"), let n = Int(payload.dropFirst(5)) {
            store.moveTask(n, toList: target)        // a to-do dragged from the task list into this list
        } else if payload.hasPrefix("list:") {
            store.moveList(String(payload.dropFirst(5)), toGroup: group, before: target)
        } else if payload.hasPrefix("group:") {
            store.moveGroup(String(payload.dropFirst(6)), before: group.map { .group($0) } ?? .list(target))
        } else { return false }
        return true
    }

    func dropOnGroup(_ payload: String, _ g: String) -> Bool {
        if payload.hasPrefix("task:") {
            store.expanded.insert(g)                  // a to-do goes into a list, not a group: open it to pick one
            return false
        } else if payload.hasPrefix("list:") {
            store.moveList(String(payload.dropFirst(5)), toGroup: g, before: nil)
        } else if payload.hasPrefix("group:") {
            store.moveGroup(String(payload.dropFirst(6)), before: .group(g))
        } else { return false }
        return true
    }

    func dropAtEnd(_ payload: String) -> Bool {
        if payload.hasPrefix("task:") {
            return false
        } else if payload.hasPrefix("list:") {
            store.moveList(String(payload.dropFirst(5)), toGroup: nil, before: nil)
        } else if payload.hasPrefix("group:") {
            store.moveGroup(String(payload.dropFirst(6)), before: nil)
        } else { return false }
        return true
    }

    @ViewBuilder func listRow(_ name: String, _ count: Int, group: String?) -> some View {
        SidebarRow(title: titleCase(name), count: count, icon: nil, selected: store.route == .list(name), indent: group != nil) {
            store.route = .list(name)
            store.query = ""
        }
        .draggable("list:" + name)
        .modifier(DropTarget { payload in dropOnList(payload, name, group: group) })
        .contextMenu {
            Menu("Move to group") {
                Button("No group") { store.setGroup(name, nil) }
                ForEach(store.groupNames, id: \.self) { g in Button(g) { store.setGroup(name, g) } }
            }
            if name != "tasks" {
                Button("Delete list…") { Dialogs.deleteList(name, store: store) }
            }
        }
    }
}

/// Highlights while something is dragged over it, and hands the dropped payload ("list:x" / "group:y") on.
struct DropTarget: ViewModifier {
    var line = true                                 // a blue line on top means "goes just before this"
    var onHover: (() -> Void)? = nil                // spring-loading: called after a short hover while dragging
    var perform: (String) -> Bool
    @State private var targeted = false
    @State private var hoverWork: DispatchWorkItem?
    func body(content: Content) -> some View {
        content
            .background(RoundedRectangle(cornerRadius: 6).fill(targeted ? Palette.blue.opacity(0.18) : Color.clear))
            .overlay(alignment: .top) {
                if targeted && line { Rectangle().fill(Palette.blue).frame(height: 2).padding(.horizontal, 6) }
            }
            .dropDestination(for: String.self) { items, _ in
                guard let p = items.first else { return false }
                return perform(p)
            } isTargeted: { t in
                targeted = t
                hoverWork?.cancel()
                if t, let h = onHover {
                    let w = DispatchWorkItem { h() }
                    hoverWork = w
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.6, execute: w)
                }
            }
    }
}

struct SidebarRow: View {
    @Environment(\.zoom) private var z
    var title: String
    var count: Int
    var icon: String?
    var selected: Bool
    var indent = false
    var tint: Color? = nil
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 7) {
                if let i = icon {
                    Image(systemName: i).font(.system(size: 11 * z)).foregroundColor(tint ?? .secondary).frame(width: 14 * z)
                }
                Text(title).font(.system(size: 13 * z)).lineLimit(1)
                Spacer(minLength: 4)
                Text("\(count)").font(.system(size: 11 * z).monospacedDigit()).foregroundStyle(selected ? .primary : .tertiary)
            }
            .padding(.leading, indent ? 26 * z : 10)
            .padding(.trailing, 10)
            .padding(.vertical, 4.5 * z)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 6).fill(selected ? Palette.blue.opacity(0.3) : Color.clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

struct GroupRow: View {
    @Environment(\.zoom) private var z
    var name: String
    var count: Int
    var expanded: Bool
    var toggle: () -> Void

    var body: some View {
        Button(action: toggle) {
            HStack(spacing: 6) {
                Image(systemName: "chevron.right").font(.system(size: 9 * z, weight: .bold)).foregroundStyle(.secondary)
                    .rotationEffect(.degrees(expanded ? 90 : 0)).frame(width: 12 * z)
                Text(name).font(.system(size: 13 * z, weight: .semibold)).lineLimit(1)
                Spacer(minLength: 4)
                Text("\(count)").font(.system(size: 11 * z).monospacedDigit()).foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 8).padding(.vertical, 4.5 * z)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.top, 2)
    }
}

// MARK: - the middle column: a list, the agents' tasks, or search results

struct MainColumn: View {
    @Environment(\.zoom) private var z
    @ObservedObject var store: Store

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            switch store.route {
            case .list(let name): ListColumn(store: store, name: name)
            case .agents: AgentsColumn(store: store)
            case .search: SearchColumn(store: store)
            }
            if !store.marked.isEmpty {
                Divider()
                SelectionBar(store: store)
            }
        }
    }
}

/// What can be done to the tasks picked with ⇧- or ⌘-click.
struct SelectionBar: View {
    @Environment(\.zoom) private var z
    @ObservedObject var store: Store

    var body: some View {
        let ns = store.chosen
        let anyGroup = ns.contains { store.isGroup($0) }
        let anyGrouped = ns.contains { store.items[$0]?.parent != nil }
        HStack(spacing: 10) {
            Text("\(ns.count) selected").font(.system(size: 12 * z, weight: .semibold))
            Button { store.completeMany(ns) } label: { Label("Complete", systemImage: "checkmark") }
            Menu {
                ForEach(store.allLists, id: \.self) { l in Button(titleCase(l)) { store.moveMany(ns, toList: l) } }
            } label: { Label("Move", systemImage: "folder") }
                .menuStyle(.borderlessButton).fixedSize()
            RateMenu(store: store, numbers: ns)
            TagMenu(store: store, numbers: ns, icon: true)
            WaitingButtons(store: store, numbers: ns, icon: true)
            Button { if let lead = ns.first { store.assigning = AssignRequest(number: lead, others: Array(ns.dropFirst())) } } label: {
                Label("Agent", systemImage: "person.crop.circle.badge.plus")
            }
            .disabled(ns.contains { $0 < 0 } || !store.link.isLive)
            .help("One agent for all of them (⇧⌘A)")
            if ns.count > 1 {
                Button { Dialogs.groupName(store: store, numbers: ns) } label: { Label("Group", systemImage: "rectangle.stack") }
                    .help("Put them under one task you can fold (⌘G)")
            }
            if anyGroup { Button("Ungroup") { store.ungroupMany(ns) } }
            if anyGrouped { Button("Take out") { store.takeOut(ns) }.help("Take out of their group") }
            Button { Dialogs.deleteMany(ns, store: store) } label: { Label("Delete", systemImage: "trash") }
            Spacer()
            Button { store.clearMarks() } label: { Image(systemName: "xmark") }.help("Clear the selection (esc)")
        }
        .buttonStyle(.borderless)
        .font(.system(size: 12 * z))
        .padding(.horizontal, 16).padding(.vertical, 8)
        .background(Palette.blue.opacity(0.08))
    }
}

/// Importance and urgency for one or several tasks (a group is rated itself; its tasks keep theirs).
struct RateMenu: View {
    @ObservedObject var store: Store
    let numbers: [Int]
    var body: some View {
        Menu {
            Section("Importance") {
                ForEach(["high", "medium", "low"], id: \.self) { i in Button(i.capitalized) { store.rateMany(numbers, importance: i) } }
            }
            Section("Urgency") {
                Button("Urgent") { store.rateMany(numbers, urgency: "high") }
                Button("Not urgent") { store.rateMany(numbers, urgency: "low") }
            }
        } label: { Label("Rate", systemImage: "star") }
            .menuStyle(.borderlessButton).fixedSize()
    }
}

/// Tags for one or several tasks: a tick on tags they all have; choosing one adds it to all, or takes it off all
/// if they all have it. "New tag…" names a new one.
struct TagMenu: View {
    @ObservedObject var store: Store
    let numbers: [Int]
    var icon = false

    var body: some View {
        let tasks = numbers.compactMap { store.items[$0] }
        Menu {
            ForEach(store.allTags, id: \.self) { tag in
                let all = !tasks.isEmpty && tasks.allSatisfy { $0.tags.contains(tag) }
                Button { store.tagMany(numbers, tag, on: !all) } label: {
                    if all { Label(tag, systemImage: "checkmark") } else { Text(tag) }
                }
            }
            if !store.allTags.isEmpty { Divider() }
            Button("New tag…") { Dialogs.newTag(store: store, numbers: numbers) }
        } label: {
            if icon { Label("Tag", systemImage: "tag") } else { Text("Tags") }
        }
        .menuStyle(.borderlessButton).fixedSize()
    }
}

/// Mark tasks as waiting on someone, or not any more (who and when to chase are set in the edit pane).
struct WaitingButtons: View {
    @ObservedObject var store: Store
    let numbers: [Int]
    var icon = false

    var body: some View {
        let tasks = numbers.compactMap { store.items[$0] }.filter { $0.isOpen }
        if tasks.contains(where: { !$0.waiting }) {
            Button { store.waitMany(numbers, on: true) } label: {
                if icon { Label("Waiting", systemImage: "hourglass") } else { Text("Mark waiting") }
            }
            .help("Waiting on someone: greyed at the bottom of the list")
        }
        if tasks.contains(where: { $0.waiting }) {
            Button(icon ? "Not waiting" : "No longer waiting") { store.waitMany(numbers, on: false) }
        }
    }
}

struct ColumnHeader: View {
    @Environment(\.zoom) private var z
    var title: String
    var subtitle: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(title).font(.system(size: 21 * z, weight: .bold)).lineLimit(1)
            Text(subtitle).font(.system(size: 13 * z)).foregroundStyle(.secondary).lineLimit(1)
            Spacer()
        }
    }
}

struct ListColumn: View {
    @Environment(\.zoom) private var z
    @ObservedObject var store: Store
    let name: String
    @State private var draft = ""
    @FocusState private var addFocused: Bool

    var body: some View {
        let open = store.open(in: name)
        let done = store.completed(in: name)
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    HStack {
                        ColumnHeader(title: titleCase(name), subtitle: "\(open.count) open" + (store.filters.active ? " (filtered)" : ""))
                        if name != "tasks" {
                            Menu {
                                Menu("Move to group") {
                                    Button("No group") { store.setGroup(name, nil) }
                                    ForEach(store.groupNames, id: \.self) { g in Button(g) { store.setGroup(name, g) } }
                                }
                                Button("Delete list…") { Dialogs.deleteList(name, store: store) }
                            } label: { Image(systemName: "ellipsis.circle") }
                                .menuStyle(.borderlessButton).fixedSize()
                        }
                    }
                    HStack(spacing: 8) {
                        Image(systemName: "plus").foregroundStyle(.secondary).font(.system(size: 12 * z))
                        TextField("Add a task to \(titleCase(name))", text: $draft)
                            .textFieldStyle(.plain).font(.system(size: 13 * z))
                            .focused($addFocused)
                            .onSubmit {
                                if store.addTask(title: draft, list: name) != nil { draft = "" }
                                addFocused = true
                            }
                            .onExitCommand { draft = ""; addFocused = false }
                        Text(addFocused ? "⏎ add" : "⌘N").font(.system(size: 11 * z)).foregroundStyle(.tertiary)
                    }
                    .padding(.horizontal, 12).padding(.vertical, 8 * z)
                    .background(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.primary.opacity(addFocused ? 0.35 : 0.12)))
                    .padding(.top, 12)
                    FilterChips(store: store, list: name).padding(.vertical, 10)
                    if open.isEmpty {
                        Text(store.filters.active ? "Nothing open matches these filters." : "Nothing open here. Add a task above.")
                            .font(.system(size: 13 * z)).foregroundStyle(.secondary).padding(.vertical, 18)
                    }
                    TaskRows(store: store, entries: store.groupedRows(open))
                    if !done.isEmpty {
                        Button { store.showCompleted.toggle() } label: {
                            HStack(spacing: 6) {
                                Image(systemName: "chevron.right").font(.system(size: 9 * z, weight: .bold))
                                    .rotationEffect(.degrees(store.showCompleted ? 90 : 0))
                                Text("Completed \(done.count)").font(.system(size: 12 * z))
                            }
                            .foregroundStyle(.secondary).contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .padding(.top, 14).padding(.bottom, 4)
                        if store.showCompleted { TaskRows(store: store, tasks: done) }
                    }
                }
                .padding(.horizontal, 22).padding(.vertical, 16)
            }
            .onChange(of: store.selection) { s in if let s = s { withAnimation(.easeOut(duration: 0.15)) { proxy.scrollTo(s) } } }
        }
        .onChange(of: store.focusRequest) { f in
            if f == .add { addFocused = true; store.focusRequest = nil }
        }
        .onAppear { if store.focusRequest == .add { addFocused = true; store.focusRequest = nil } }
    }
}

struct AgentsColumn: View {
    @ObservedObject var store: Store
    var body: some View {
        let tasks = store.agentTasks
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                ColumnHeader(title: "With agents", subtitle: "\(tasks.filter { $0.agentActive }.count) working · \(tasks.count) assigned")
                Text("Open tasks you assigned to an agent. Caramel ones have an agent on them now; the rest have reported back.")
                    .font(.system(size: 12)).foregroundStyle(.secondary).padding(.top, 6).padding(.bottom, 12)
                TaskRows(store: store, tasks: tasks, showList: true)
            }
            .padding(.horizontal, 22).padding(.vertical, 16)
        }
    }
}

struct SearchColumn: View {
    @Environment(\.zoom) private var z
    @ObservedObject var store: Store
    var body: some View {
        let results = store.searchResults()
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                ColumnHeader(title: "Search", subtitle: "\(results.count) match\(results.count == 1 ? "" : "es") for “\(store.query)”")
                HStack(spacing: 12) {
                    Toggle("Include completed", isOn: $store.showCompleted).toggleStyle(.checkbox)
                    if !store.filters.tags.isEmpty {
                        Button { store.filters.tags = [:] } label: {
                            Text("Tag filter on · Clear").font(.system(size: 11 * z, weight: .semibold)).foregroundColor(Palette.attention)
                        }
                        .buttonStyle(.borderless)
                        .help(store.filters.tags.sorted { $0.key < $1.key }.map { ($0.value == .only ? "only " : "hiding ") + $0.key }.joined(separator: ", "))
                    }
                }
                .font(.system(size: 12 * z)).padding(.vertical, 10)
                TaskRows(store: store, tasks: Array(results.prefix(300)), showList: true)
                if results.count > 300 {
                    Text("Showing the 300 most recent. Add a word to narrow it down.").font(.system(size: 12 * z)).foregroundStyle(.secondary).padding(.top, 8)
                }
            }
            .padding(.horizontal, 22).padding(.vertical, 16)
        }
    }
}

struct FilterChips: View {
    @Environment(\.zoom) private var z
    @ObservedObject var store: Store
    let list: String

    var body: some View {
        let secs = Array(Set(sections + store.sectionsInUse)).sorted { (sections.firstIndex(of: $0) ?? 99, $0) < (sections.firstIndex(of: $1) ?? 99, $1) }
        let tags = store.tagsShown(in: list)
        FlowLayout(spacing: 6) {
            if !tags.isEmpty {
                ForEach(tags, id: \.self) { t in TagFilterChip(tag: t, mode: store.filters.tags[t]) { store.filters.cycle(t) } }
                divider
            }
            ForEach(secs, id: \.self) { s in chip(s, store.filters.section == s) { store.filters.section = store.filters.section == s ? nil : s } }
            divider
            ForEach(priorities, id: \.self) { p in chip(p, store.filters.priority == p) { store.filters.priority = store.filters.priority == p ? nil : p } }
            if store.filters.active {
                Button { store.filters = Filters() } label: {
                    Text("Filtered · Clear").font(.system(size: 11 * z, weight: .semibold)).foregroundColor(Palette.attention)
                }
                .buttonStyle(.borderless)
                .help("Show every task again")
            }
        }
    }

    var divider: some View { Rectangle().fill(Color.primary.opacity(0.15)).frame(width: 1, height: 14 * z) }

    func chip(_ label: String, _ on: Bool, _ tap: @escaping () -> Void) -> some View {
        Button(action: tap) {
            Text(label).font(.system(size: 11 * z, weight: on ? .semibold : .regular))
                .foregroundColor(on ? Color(nsColor: .windowBackgroundColor) : .secondary)
                .padding(.horizontal, 9).padding(.vertical, 2.5 * z)
                .background(Capsule().fill(on ? Color.primary : Color.clear))
                .overlay(Capsule().strokeBorder(Color.primary.opacity(on ? 0 : 0.16)))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }
}

/// A tag in the list filter. Click: only tasks with it (filled), then none with it (struck through), then off.
struct TagFilterChip: View {
    @Environment(\.zoom) private var z
    let tag: String
    let mode: TagMode?
    let tap: () -> Void

    var body: some View {
        Button(action: tap) {
            HStack(spacing: 3) {
                Image(systemName: mode == .only ? "checkmark" : mode == .hide ? "eye.slash" : "tag")
                    .font(.system(size: 9 * z, weight: .semibold))
                Text(tag).strikethrough(mode == .hide)
            }
            .font(.system(size: 11 * z, weight: mode == nil ? .regular : .semibold))
            .foregroundColor(mode == .only ? .white : mode == .hide ? Palette.red : Palette.tag)
            .padding(.horizontal, 9).padding(.vertical, 2.5 * z)
            .background(Capsule().fill(mode == .only ? Palette.tag : mode == .hide ? Palette.red.opacity(0.1) : Color.clear))
            .overlay(Capsule().strokeBorder(mode == .only ? Color.clear : mode == .hide ? Palette.red.opacity(0.6) : Palette.tag.opacity(0.45)))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help(mode == nil ? "Show only tasks tagged \(tag)" : mode == .only ? "Showing only \(tag): click to hide \(tag) instead"
              : "Hiding \(tag): click to stop filtering by it")
    }
}

/// Wraps its children onto new lines (the filter chips).
struct FlowLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 600
        var x: CGFloat = 0, y: CGFloat = 0, line: CGFloat = 0, maxX: CGFloat = 0
        for s in subviews {
            let size = s.sizeThatFits(.unspecified)
            if x > 0 && x + size.width > width { x = 0; y += line + spacing; line = 0 }
            x += size.width + spacing
            maxX = max(maxX, x)
            line = max(line, size.height)
        }
        return CGSize(width: min(width, maxX), height: y + line)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, line: CGFloat = 0
        for s in subviews {
            let size = s.sizeThatFits(.unspecified)
            if x > bounds.minX && x + size.width > bounds.maxX { x = bounds.minX; y += line + spacing; line = 0 }
            s.place(at: CGPoint(x: x, y: y + (line > 0 ? 0 : 0)), proposal: ProposedViewSize(size))
            x += size.width + spacing
            line = max(line, size.height)
        }
    }
}

// MARK: - rows

struct TaskRows: View {
    @ObservedObject var store: Store
    let entries: [RowEntry]
    var showList = false

    init(store: Store, entries: [RowEntry], showList: Bool = false) {
        self.store = store
        self.entries = entries
        self.showList = showList
    }

    /// Flat rows, no grouping (completed tasks, the agents' tasks, search results)
    init(store: Store, tasks: [TaskItem], showList: Bool = false) {
        self.init(store: store, entries: tasks.map { RowEntry(task: $0) }, showList: showList)
    }

    var body: some View {
        LazyVStack(spacing: 0) {
            ForEach(entries) { e in
                let t = e.task
                let picked = store.marked.contains(t.number)
                TaskRow(task: t, selected: store.selection == t.number || picked, showList: showList, now: store.now,
                        entry: e,
                        toggle: { store.toggleComplete(t.number) },
                        select: {
                            let mods = NSApp.currentEvent?.modifierFlags ?? []
                            store.click(t.number, shift: mods.contains(.shift), command: mods.contains(.command))
                        },
                        edit: { store.selection = t.number; store.editing = t.number },
                        fold: { store.toggleFold(t.number) })
                    .id(t.number)
                    .draggable("task:\(t.number)") {
                        TaskDragPreview(task: t, count: picked ? store.marked.count : 1)
                    }
                    .modifier(DropTarget(line: false) { payload in store.drop(payload, onto: t.number) })
                    .contextMenu {
                        if picked && store.marked.count > 1 { BulkMenu(store: store) } else { TaskMenu(task: t, store: store) }
                    }
            }
        }
    }
}

/// The right-click menu on a row that is part of a selection of several.
struct BulkMenu: View {
    @ObservedObject var store: Store
    var body: some View {
        let ns = store.chosen
        Button("Complete \(ns.count) tasks") { store.completeMany(ns) }
        Menu("Move to list") {
            ForEach(store.allLists, id: \.self) { l in Button(titleCase(l)) { store.moveMany(ns, toList: l) } }
        }
        RateMenu(store: store, numbers: ns)
        TagMenu(store: store, numbers: ns)
        WaitingButtons(store: store, numbers: ns)
        Button("Assign one agent to all…") { if let lead = ns.first { store.assigning = AssignRequest(number: lead, others: Array(ns.dropFirst())) } }
            .disabled(ns.contains { $0 < 0 })
        Button("Group…") { Dialogs.groupName(store: store, numbers: ns) }
        if ns.contains(where: { store.isGroup($0) }) { Button("Ungroup") { store.ungroupMany(ns) } }
        if ns.contains(where: { store.items[$0]?.parent != nil }) { Button("Take out of group") { store.takeOut(ns) } }
        Divider()
        Button("Delete \(ns.count) tasks…") { Dialogs.deleteMany(ns, store: store) }
    }
}

struct TaskMenu: View {
    let task: TaskItem
    @ObservedObject var store: Store
    var body: some View {
        Button(task.isOpen ? "Complete" : "Reopen") { store.toggleComplete(task.number) }
        if store.isGroup(task.number) { Button("Ungroup") { store.ungroupMany([task.number]) } }
        if task.parent != nil { Button("Take out of group") { store.takeOut([task.number]) } }
        Button("Edit") { store.selection = task.number; store.editing = task.number }
        if task.isOpen && !task.isLocal {
            Button(task.assigned ? "Assign to agent again…" : "Assign to agent…") { store.assigning = AssignRequest(number: task.number) }
        }
        Divider()
        if !task.issue.url.isEmpty {
            Button("Open on GitHub") { if let u = URL(string: task.issue.url) { NSWorkspace.shared.open(u) } }
            Button("Copy link") { copyToPasteboard(task.issue.url) }
        }
        Menu("Move to list") {
            ForEach(store.allLists, id: \.self) { l in
                Button(titleCase(l)) {
                    store.saveEdit(task.number, title: task.title, notes: task.notes, list: l, section: task.section,
                                   priority: task.priority)
                }
            }
        }
        TagMenu(store: store, numbers: [task.number])
        if task.isOpen {
            if task.waiting {
                Button("No longer waiting") { store.waitMany([task.number], on: false) }
            } else {
                Button("Waiting on someone…") { store.selection = task.number; store.editing = task.number; store.focusRequest = .waiting }
            }
        }
        Divider()
        Button("Delete…") { Dialogs.deleteTask(task, store: store) }
    }
}

struct CheckCircle: View {
    @Environment(\.zoom) private var z
    let done: Bool
    let priority: String
    var onCaramel = false
    var size: CGFloat = 18

    var body: some View {
        let ring: Color = onCaramel ? .white.opacity(0.85) : priority == "high" ? Palette.red : Color.secondary.opacity(priority == "low" ? 0.45 : 0.8)
        ZStack {
            if done {
                Circle().fill(Palette.green)
                Image(systemName: "checkmark").font(.system(size: size * 0.5 * z, weight: .bold)).foregroundColor(.white)
            } else {
                Circle().strokeBorder(ring, style: StrokeStyle(lineWidth: priority == "high" ? 2 : 1.5, dash: priority == "low" ? [2.5, 2] : []))
            }
        }
        .frame(width: size * z, height: size * z)
        .contentShape(Circle())
    }
}

struct TaskRow: View {
    @Environment(\.zoom) private var z
    let task: TaskItem
    let selected: Bool
    var showList = false
    var now: Date
    var entry: RowEntry? = nil                      // its place under groups, and its counts if it is a group
    var toggle: () -> Void
    var select: () -> Void
    var edit: () -> Void
    var fold: (() -> Void)? = nil
    @State private var ticking = false              // ticked on screen at once; leaves the list a moment later

    var body: some View {
        let caramel = task.agentActive
        let done = !task.isOpen || ticking
        let group = entry?.isGroup == true
        let greyed = task.greyed(today: Waiting.day(now)) && !selected
        HStack(spacing: 11) {
            if group, let f = fold {
                Button(action: f) {
                    Image(systemName: "chevron.right").font(.system(size: 9 * z, weight: .bold))
                        .rotationEffect(.degrees(entry?.expanded == true ? 90 : 0))
                        .frame(width: 12 * z, height: 18 * z).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .foregroundColor(caramel ? .white : .secondary)
                .padding(.trailing, -6)
                .help(entry?.expanded == true ? "Fold the group" : "Show the group's tasks")
            }
            Button(action: tick) { CheckCircle(done: done, priority: task.priority, onCaramel: caramel) }
                .buttonStyle(.plain)
                .help(task.isOpen ? (group ? "Complete the group and its tasks (space)" : "Complete (space)") : "Reopen")
            Text(task.title)
                .font(.system(size: 13 * z, weight: group ? .semibold : .regular))
                .strikethrough(done, color: .secondary)
                .foregroundColor(caramel ? .white : done ? .secondary : .primary)
                .lineLimit(1)
            if group, let e = entry {
                Text("\(e.doneCount)/\(e.childCount)").font(.system(size: 11 * z).monospacedDigit())
                    .foregroundColor(caramel ? .white.opacity(0.85) : .secondary)
                    .help("\(e.doneCount) of its \(e.childCount) tasks done")
            }
            Spacer(minLength: 6)
            meta(caramel)
        }
        .opacity(greyed ? 0.45 : 1)                     // waiting on someone: greyed until the follow-up day
        .padding(.horizontal, 10)
        .padding(.vertical, 7.5 * z)
        .background(RoundedRectangle(cornerRadius: 8).fill(caramel ? Palette.caramel : selected ? Palette.blue.opacity(0.3) : Color.clear))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(caramel && selected ? Color.white.opacity(0.9) : Color.clear, lineWidth: 2))
        .overlay(alignment: .bottom) {
            if !caramel && !selected { Rectangle().fill(Color.primary.opacity(0.06)).frame(height: 1).padding(.leading, 40 * z) }
        }
        .padding(.horizontal, -10)
        .padding(.leading, CGFloat(entry?.depth ?? 0) * 26 * z)    // a group's tasks sit under it, indented
        .padding(.vertical, caramel ? 2 : 0)
        .contentShape(Rectangle())
        // one gesture, reading the click count: a double-tap recogniser would hold every single click back
        .simultaneousGesture(TapGesture().onEnded {
            if (NSApp.currentEvent?.clickCount ?? 1) >= 2 { edit() } else { select() }
        })
    }

    func tick() {
        guard task.isOpen else { toggle(); return }
        withAnimation(.easeOut(duration: 0.12)) { ticking = true }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
            toggle()
            ticking = false
        }
    }

    @ViewBuilder func meta(_ caramel: Bool) -> some View {
        HStack(spacing: 7) {
            if task.conflict != nil {
                Image(systemName: "exclamationmark.triangle.fill").foregroundColor(Palette.attention).help("Changed elsewhere too: open it to choose a version")
            } else if task.failed != nil {
                Image(systemName: "exclamationmark.circle.fill").foregroundColor(Palette.red).help("Couldn't save: \(task.failed ?? "")")
            } else if task.pending {
                Image(systemName: "icloud.and.arrow.up").help("Waiting to reach GitHub")
            }
            if showList { Text(titleCase(task.list)) }
            if !task.allChecklist.isEmpty {
                HStack(spacing: 3) {
                    Image(systemName: "checklist")
                    Text("\(task.checklistDone)/\(task.allChecklist.count)")
                }
            }
            if task.cameThrough {
                HStack(spacing: 3) {
                    Image(systemName: "envelope.badge")
                    Text(task.waitingOn.map { "Came through? \($0)" } ?? "Came through?")
                }
                .font(.system(size: 11 * z, weight: .semibold))
                .foregroundColor(caramel ? .white : Palette.attention)
                .help("An agent thinks the wait is over: open it to see why")
            } else if task.waiting && task.isOpen { waitingNote(caramel) }
            ForEach(task.tags, id: \.self) { t in tag(t, caramel, color: caramel ? nil : Palette.tag) }
            if let s = task.section { tag(s, caramel) }
            if task.assigned {
                if caramel {
                    PulseDot()
                    tag(task.agent?.name ?? "agent", true, bold: true)
                    Text(task.agent?.status == "started" ? "working" : "starting")
                } else {
                    tag("agent " + (task.agent?.status ?? "assigned"), false, color: Palette.caramel)
                }
            }
            Text(Fmt.age(task.issue.updatedAt, now: now)).monospacedDigit()
        }
        .font(.system(size: 11 * z))
        .foregroundColor(caramel ? .white.opacity(0.92) : .secondary)
        .lineLimit(1)
    }

    /// "Waiting on Sam · follow up 23 Oct", or, from the follow-up day, "Follow up with Sam" in the attention colour.
    @ViewBuilder func waitingNote(_ caramel: Bool) -> some View {
        let who = task.waitingOn
        if task.followUpDue(today: Waiting.day(now)) {
            HStack(spacing: 3) {
                Image(systemName: "bell.fill")
                Text(who.map { "Follow up with \($0)" } ?? "Follow up")
            }
            .font(.system(size: 11 * z, weight: .semibold))
            .foregroundColor(caramel ? .white : Palette.attention)
            .help("The follow-up day was \(Waiting.short(task.followUp ?? "", now: now)): time to chase")
        } else {
            HStack(spacing: 3) {
                Image(systemName: "hourglass")
                Text((who.map { "Waiting on \($0)" } ?? "Waiting")
                     + (task.followUp.map { " · follow up \(Waiting.short($0, now: now))" } ?? ""))
            }
        }
    }

    func tag(_ s: String, _ caramel: Bool, bold: Bool = false, color: Color? = nil) -> some View {
        Text(s).font(.system(size: 10.5 * z, weight: bold ? .semibold : .regular))
            .foregroundColor(color ?? (caramel ? .white : .secondary))
            .padding(.horizontal, 6).padding(.vertical, 1)
            .overlay(Capsule().strokeBorder(color ?? (caramel ? Color.white.opacity(0.6) : Color.primary.opacity(0.18))))
    }
}

/// What follows the pointer while a to-do is dragged to another list.
struct TaskDragPreview: View {
    let task: TaskItem
    var count = 1
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: count > 1 ? "square.stack" : "circle").font(.system(size: 12))
            Text(count > 1 ? "\(count) tasks" : task.title).font(.system(size: 13)).lineLimit(1)
        }
        .padding(.horizontal, 10).padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: 8).fill(task.agentActive ? Palette.caramel : Palette.blue.opacity(0.85)))
        .foregroundColor(.white)
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

// MARK: - footer

struct FooterBar: View {
    @Environment(\.zoom) private var z
    @ObservedObject var store: Store

    var body: some View {
        HStack(spacing: 14) {
            legend(Palette.red, "high priority", store.live.filter { $0.isOpen && $0.priority == "high" }.count)
            legend(Palette.caramel, "with an agent", store.activeAgents)
            if !store.pending.isEmpty { legend(Palette.ok, "waiting to save", store.pending.count) }
            if !store.conflicts.isEmpty { legend(Palette.attention, "to resolve", store.conflicts.count) }
            Spacer()
            if store.updateAvailable {
                Button("New build ready: relaunch") { store.relaunch() }.controlSize(.small)
            }
            if let t = store.toast {
                HStack(spacing: 8) {
                    Text(t.text).font(.system(size: 11 * z, weight: .medium)).foregroundColor(t.isError ? Palette.red : .primary).lineLimit(1)
                    if let u = t.undo {
                        Button("Undo") { u(); store.toast = nil }.buttonStyle(.borderless).font(.system(size: 11 * z, weight: .semibold))
                    }
                }
            } else {
                Text("⌘N add · space complete · ⌘E edit · ⌘Z undo · ⇧/⌘-click select · ⌘G group · drag a task onto another to group · ⇧⌘A assign · ⌘R refresh · ⌥⌘T")
                    .font(.system(size: 10 * z)).foregroundStyle(.tertiary).lineLimit(1)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
    }

    func legend(_ c: Color, _ label: String, _ n: Int) -> some View {
        HStack(spacing: 5) {
            RoundedRectangle(cornerRadius: 2).fill(c).frame(width: 10 * z, height: 10 * z)
            Text("\(label) \(n)").font(.system(size: 10 * z)).foregroundStyle(.secondary)
        }
    }
}
