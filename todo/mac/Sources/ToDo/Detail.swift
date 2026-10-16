import AppKit
import SwiftUI

// MARK: - the detail panel

struct DetailPanel: View {
    let task: TaskItem
    @ObservedObject var store: Store

    var body: some View {
        if store.editing == task.number {
            EditForm(task: task, store: store)
        } else {
            TaskDetail(task: task, store: store)
        }
    }
}

struct DetailSection<Content: View>: View {
    @Environment(\.zoom) private var z
    let title: String
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title.uppercased()).font(.system(size: 10 * z, weight: .semibold)).tracking(0.6).foregroundStyle(.secondary)
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct Fact: View {
    @Environment(\.zoom) private var z
    let label: String
    let value: String
    var link: String? = nil
    var body: some View {
        HStack(alignment: .top) {
            Text(label).frame(width: 70 * z, alignment: .leading).foregroundStyle(.secondary)
            if let l = link, let u = URL(string: l) {
                Link(value, destination: u)
            } else {
                Text(value).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
        }
        .font(.system(size: 12 * z))
    }
}

struct CaramelButton: ButtonStyle {
    @Environment(\.zoom) private var z
    @Environment(\.isEnabled) private var enabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12.5 * z, weight: .semibold))
            .foregroundColor(.white)
            .padding(.horizontal, 12).padding(.vertical, 5 * z)
            .background(RoundedRectangle(cornerRadius: 6).fill(Palette.caramel.opacity(configuration.isPressed ? 0.75 : enabled ? 1 : 0.45)))
    }
}

struct TaskDetail: View {
    @Environment(\.zoom) private var z
    let task: TaskItem
    @ObservedObject var store: Store

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                banners
                HStack(spacing: 8) {
                    if task.isOpen && !task.isLocal {
                        Button(task.assigned ? "Assign again…" : "Assign to agent…") { store.assigning = AssignRequest(number: task.number) }
                            .buttonStyle(CaramelButton())
                            .help("Start an agent on this task through the orchestrator (⇧⌘A)")
                    }
                    Button(task.isOpen ? "Complete" : "Reopen") { store.toggleComplete(task.number) }
                    Button("Edit") { store.editing = task.number }.help("⌘E")
                    Button("Delete") { Dialogs.deleteTask(task, store: store) }.buttonStyle(.borderless).foregroundStyle(.secondary)
                }
                .font(.system(size: 12.5 * z))
                if task.assigned { AgentSection(task: task, store: store) }
                if task.cameThrough {
                    Banner(color: Palette.attention, text: "An agent thinks this came through (see notes).") {
                        Button("Still waiting") { store.answerCleared(task.number, stillWaiting: true) }
                        Button("OK") { store.answerCleared(task.number, stillWaiting: false) }
                    }
                } else if task.waiting && task.isOpen { WaitingSummary(task: task, store: store) }
                DetailSection(title: "Tags") { TagEditor(task: task, store: store) }
                if !task.notes.isEmpty {
                    DetailSection(title: "Notes") { NotesView(task: task, store: store) }
                }
                ChecklistSection(task: task, store: store)
                if let c = task.claudeNotes {
                    DetailSection(title: "Claude resolution notes") {
                        RichText(text: c).font(.system(size: 12.5 * z))
                            .padding(12)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(RoundedRectangle(cornerRadius: 8).fill(Palette.caramel.opacity(0.12)))
                    }
                }
                DetailSection(title: "Facts") {
                    if task.isLocal {
                        Fact(label: "Issue", value: "not on GitHub yet")
                    } else {
                        Fact(label: "Issue", value: task.issue.shortRef, link: task.issue.url)
                    }
                    Fact(label: "Labels", value: task.issue.labels.isEmpty ? "none" : task.issue.labels.joined(separator: ", "))
                    Fact(label: "Created", value: Fmt.day(task.issue.createdAt))
                    Fact(label: "Updated", value: Fmt.ago(task.issue.updatedAt, now: store.now))
                }
            }
            .padding(18)
        }
    }

    var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 10) {
                Button { store.toggleComplete(task.number) } label: { CheckCircle(done: !task.isOpen, priority: task.priority, size: 20) }
                    .buttonStyle(.plain).padding(.top, 2 * z)
                Text(task.title).font(.system(size: 17 * z, weight: .semibold))
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                Spacer(minLength: 4)
                Button { store.selection = nil } label: { Image(systemName: "xmark") }.buttonStyle(.borderless).help("Close (esc)")
            }
            HStack(spacing: 4) {
                Button(titleCase(task.list)) { store.route = .list(task.list); store.query = "" }.buttonStyle(.borderless)
                Text(crumb).foregroundStyle(.secondary)
            }
            .font(.system(size: 12 * z))
            .padding(.leading, 30 * z)
        }
    }

    var crumb: String {
        var parts = ["\(task.priority) priority"]
        if let sec = task.section { parts.append(sec) }
        if !task.isLocal { parts.append("#\(task.number)") }
        return "· " + parts.joined(separator: " · ")
    }

    @ViewBuilder var banners: some View {
        if let fields = task.conflict {
            Banner(color: Palette.attention, text: "This task's \(fields.joined(separator: " and ")) changed on GitHub while your edit was waiting. Which version should it keep?") {
                Button("Keep mine") { store.keepMine(task.number) }
                Button("Keep GitHub's") { store.keepTheirs(task.number) }
            }
        } else if let err = task.failed {
            Banner(color: Palette.red, text: "Couldn't save this edit: \(err)") {
                Button("Try again") { store.retry(task.number) }
                Button("Discard my edit") { store.discardPending(task.number) }
            }
        } else if task.pending && !store.link.isLive {
            Banner(color: Palette.ok, text: "Saved here. It goes to GitHub when the desktop is back.") { EmptyView() }
        }
    }
}

struct Banner<Buttons: View>: View {
    @Environment(\.zoom) private var z
    var color: Color
    var text: String
    @ViewBuilder var buttons: Buttons
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(text).font(.system(size: 12 * z)).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) { buttons }.controlSize(.small)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(color.opacity(0.14)))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(color.opacity(0.5)))
    }
}

struct ChecklistRow: View {
    @Environment(\.zoom) private var z
    let item: ChecklistItem
    let task: TaskItem
    @ObservedObject var store: Store
    @State private var hover = false
    @State private var editing = false
    @State private var draft = ""
    @FocusState private var focused: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Button { store.toggleItem(task.number, item) } label: {
                ZStack {
                    RoundedRectangle(cornerRadius: 4).fill(item.checked ? Palette.green : Color.clear)
                    RoundedRectangle(cornerRadius: 4).strokeBorder(item.checked ? Palette.green : Color.secondary.opacity(0.7), lineWidth: 1.4)
                    if item.checked { Image(systemName: "checkmark").font(.system(size: 9 * z, weight: .bold)).foregroundColor(.white) }
                }
                .frame(width: 15 * z, height: 15 * z)
                .frame(width: 24 * z, height: 22 * z)       // the whole box (and a margin) is the click target,
                .contentShape(Rectangle())                  // not just its 1.4 pt border
            }
            .buttonStyle(.plain)
            .padding(.leading, -4 * z)
            .help(item.checked ? "Untick" : "Tick")
            if editing {
                TextField("Item", text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.system(size: 13 * z))
                    .focused($focused)
                    .onSubmit { commit() }
                    .onExitCommand { editing = false }
                    .onChange(of: focused) { f in if !f && editing { commit() } }
                    .padding(.top, 2 * z)
            } else {
                RichText(text: item.text, selectable: false)
                    .font(.system(size: 13 * z))
                    .strikethrough(item.checked, color: .secondary)
                    .foregroundStyle(item.checked ? .secondary : .primary)
                    .padding(.top, 2 * z)
                    .onTapGesture(count: 2) { start() }
                    .help("Double-click to edit")
            }
            Spacer(minLength: 4)
            if !editing {
                Button { start() } label: { Image(systemName: "pencil").font(.system(size: 10 * z)) }
                    .buttonStyle(.borderless).opacity(hover ? 1 : 0).help("Edit")
                    .padding(.top, 3 * z)
                if item.header == IssueBody.checklistHeader {
                    Button { store.removeItem(task.number, item) } label: { Image(systemName: "xmark").font(.system(size: 10 * z)) }
                        .buttonStyle(.borderless).opacity(hover ? 1 : 0).help("Remove")
                        .padding(.top, 3 * z)
                }
            }
        }
        .onHover { hover = $0 }
    }

    func start() {
        draft = item.text
        editing = true
        DispatchQueue.main.async { focused = true }
    }

    func commit() {
        editing = false
        store.editItem(task.number, item, to: draft)
    }
}

/// The checklist, with tick, inline edit, remove and add: shown in the task panel and in the edit pane.
struct ChecklistSection: View {
    @Environment(\.zoom) private var z
    let task: TaskItem
    @ObservedObject var store: Store
    @State private var newItem = ""

    var body: some View {
        DetailSection(title: task.checklist.isEmpty ? "Checklist" : "Checklist \(task.checklist.filter { $0.checked }.count)/\(task.checklist.count)") {
            ForEach(task.checklist) { item in ChecklistRow(item: item, task: task, store: store) }
            HStack(spacing: 8) {
                Image(systemName: "plus").font(.system(size: 11 * z)).foregroundStyle(.secondary)
                TextField("Add an item", text: $newItem)
                    .textFieldStyle(.plain).font(.system(size: 13 * z))
                    .onSubmit { store.addItem(task.number, newItem); newItem = "" }
            }
            .padding(.top, 2)
        }
    }
}

/// The notes, with links clickable and loose `- [ ]` lines (from older imports) tickable in place.
struct NotesView: View {
    @Environment(\.zoom) private var z
    let task: TaskItem
    @ObservedObject var store: Store

    var body: some View {
        let lines = NotesView.noteLines(task)
        let blocks = NotesView.blocks(lines, items: task.looseChecklist)
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, b in
                if let item = b.item {
                    ChecklistRow(item: item, task: task, store: store)
                } else {
                    RichText(text: b.text).font(.system(size: 13 * z))
                }
            }
        }
    }

    struct Block { var text: String; var item: ChecklistItem? }

    /// The notes' lines, with the "Waiting on:" line blanked (it shows above, not as a note) so line numbers match.
    static func noteLines(_ task: TaskItem) -> [String] {
        var lines = IssueBody.split(task.issue.body).first?.text.components(separatedBy: "\n") ?? []
        if task.waitingOn != nil, !lines.isEmpty { lines[0] = "" }
        return lines
    }

    static func blocks(_ lines: [String], items: [ChecklistItem]) -> [Block] {
        var out: [Block] = []
        var para: [String] = []
        func flush() {
            let t = para.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty { out.append(Block(text: t, item: nil)) }
            para = []
        }
        for (i, l) in lines.enumerated() {
            if let item = items.first(where: { $0.line == i }) {
                flush()
                out.append(Block(text: "", item: item))
            } else if l.trimmingCharacters(in: .whitespaces).isEmpty {
                flush()
            } else {
                para.append(l)
            }
        }
        flush()
        return out
    }
}

/// Inline Markdown (bold, italics, code, links) plus bare URLs made clickable.
struct RichText: View {
    let text: String
    var selectable = true                             // off where double-click means "edit" (checklist items)
    var body: some View {
        if selectable {
            Text(RichText.attributed(text)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        } else {
            Text(RichText.attributed(text)).fixedSize(horizontal: false, vertical: true)
        }
    }

    static let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue)

    static func attributed(_ s: String) -> AttributedString {
        var a = (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(s)
        let plain = String(a.characters)
        let ns = plain as NSString
        for m in detector?.matches(in: plain, range: NSRange(location: 0, length: ns.length)) ?? [] {
            guard let url = m.url, let r = Range(m.range, in: plain) else { continue }
            let lower = plain.distance(from: plain.startIndex, to: r.lowerBound)
            let len = plain.distance(from: r.lowerBound, to: r.upperBound)
            let start = a.index(a.startIndex, offsetByCharacters: lower)
            let end = a.index(start, offsetByCharacters: len)
            if a[start..<end].link == nil { a[start..<end].link = url }
        }
        return a
    }
}

/// The task's tags as chips you can take off, and a menu to add one (an existing tag or a new one). Saved at once.
struct TagEditor: View {
    @Environment(\.zoom) private var z
    let task: TaskItem
    @ObservedObject var store: Store
    /// The edit pane passes its own list here, saved with the rest of the edit; the task panel saves at once.
    var draft: Binding<[String]>? = nil

    var body: some View {
        let tags = draft?.wrappedValue ?? task.tags
        FlowLayout(spacing: 6) {
            ForEach(tags, id: \.self) { t in
                HStack(spacing: 4) {
                    Text(t)
                    Button { remove(t) } label: { Image(systemName: "xmark").font(.system(size: 8 * z, weight: .bold)) }
                        .buttonStyle(.plain).help("Take the \(t) tag off")
                }
                .font(.system(size: 11.5 * z))
                .foregroundColor(Palette.tag)
                .padding(.horizontal, 8).padding(.vertical, 2.5 * z)
                .overlay(Capsule().strokeBorder(Palette.tag.opacity(0.5)))
            }
            Menu {
                ForEach(store.allTags.filter { !tags.contains($0) }, id: \.self) { t in Button(t) { add(t) } }
                if store.allTags.contains(where: { !tags.contains($0) }) { Divider() }
                Button("New tag…") {
                    Dialogs.askTag(store: store) { name in add(name) }
                }
            } label: {
                Label(tags.isEmpty ? "Add a tag" : "Add", systemImage: "plus").font(.system(size: 11.5 * z))
            }
            .menuStyle(.borderlessButton).fixedSize()
        }
    }

    func add(_ t: String) {
        let n = Tags.normalise(t)
        guard !n.isEmpty else { return }
        if let d = draft { if !d.wrappedValue.contains(n) { d.wrappedValue.append(n) } }
        else { store.tagMany([task.number], n, on: true) }
    }

    func remove(_ t: String) {
        if let d = draft { d.wrappedValue.removeAll { $0 == t } } else { store.tagMany([task.number], t, on: false) }
    }
}

/// Waiting on someone: who, when to chase, and a button for when the answer has come.
struct WaitingSummary: View {
    @Environment(\.zoom) private var z
    let task: TaskItem
    @ObservedObject var store: Store

    var body: some View {
        let due = task.followUpDue(today: store.today)
        let color = due ? Palette.attention : Palette.slate
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 7) {
                Image(systemName: due ? "bell.fill" : "hourglass").foregroundColor(color)
                Text(due ? (task.waitingOn.map { "Follow up with \($0)" } ?? "Time to follow up")
                         : (task.waitingOn.map { "Waiting on \($0)" } ?? "Waiting on someone"))
                    .font(.system(size: 13 * z, weight: .semibold))
            }
            Text(task.followUp.map { (due ? "The follow-up day was " : "Follow up on ") + Waiting.short($0, now: store.now) + "." }
                 ?? "No follow-up day. It stays greyed at the bottom of the list until you say it's done.")
                .font(.system(size: 12 * z)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button("No longer waiting") { store.waitMany([task.number], on: false) }
                Button("Change…") { store.editing = task.number; store.focusRequest = .waiting }
            }
            .controlSize(.small)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(color.opacity(0.12)))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(color.opacity(0.4)))
    }
}

struct AgentSection: View {
    @Environment(\.zoom) private var z
    let task: TaskItem
    @ObservedObject var store: Store

    static func describe(_ info: AgentInfo?) -> (Color, String) {
        switch info?.status ?? "assigned" {
        case "started": return (Palette.caramel, "\(info?.name ?? "An agent") is working on it.")
        case "done": return (Palette.green, "The agent reported back. Tick the task off when you're happy with it.")
        case "blocked": return (Palette.red, "The agent is blocked and needs you; it will have emailed you.")
        case "failed": return (Palette.red, "The agent didn't start. Assign again to retry.")
        default: return (Palette.caramel, "Waiting for the orchestrator to start an agent. It emails you which one.")
        }
    }

    var body: some View {
        let (color, line) = AgentSection.describe(task.agent)
        let events: [AgentEvent] = (task.agent?.events ?? []).reversed()
        DetailSection(title: "Agent") {
            HStack(alignment: .top, spacing: 8) {
                Circle().fill(color).frame(width: 8 * z, height: 8 * z).padding(.top, 5 * z)
                Text(line).font(.system(size: 13 * z)).fixedSize(horizontal: false, vertical: true)
            }
            ForEach(0..<events.count, id: \.self) { i in AgentEventRow(event: events[i]) }
        }
    }
}

struct AgentEventRow: View {
    @Environment(\.zoom) private var z
    let event: AgentEvent

    var body: some View {
        let text = event.text.components(separatedBy: "\n").prefix(6).joined(separator: "\n")
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(event.kind.capitalized).font(.system(size: 11 * z, weight: .semibold))
                Text(Fmt.clock(event.at)).font(.system(size: 11 * z)).foregroundStyle(.secondary)
                Spacer()
                if let s = event.url, let u = URL(string: s) {
                    Link(destination: u) { Image(systemName: "arrow.up.right.square") }.help("Open the comment on GitHub")
                }
            }
            RichText(text: text).font(.system(size: 12 * z)).foregroundStyle(.secondary)
        }
        .padding(9)
        .background(RoundedRectangle(cornerRadius: 7).fill(Color.primary.opacity(0.04)))
    }
}

// MARK: - edit

struct EditForm: View {
    @Environment(\.zoom) private var z
    let task: TaskItem
    @ObservedObject var store: Store
    @State private var title: String
    @State private var notes: String
    @State private var list: String
    @State private var section: String
    @State private var priority: String
    @State private var tags: [String]
    @State private var waiting: Bool
    @State private var who: String
    @State private var followUp: String?
    @FocusState private var titleFocused: Bool
    @FocusState private var whoFocused: Bool

    init(task: TaskItem, store: Store) {
        self.task = task
        self.store = store
        _title = State(initialValue: task.title)
        _notes = State(initialValue: task.notes)
        _list = State(initialValue: task.list)
        _section = State(initialValue: task.section ?? "")
        _priority = State(initialValue: task.priority)
        _tags = State(initialValue: task.tags)
        _waiting = State(initialValue: task.waiting || store.focusRequest == .waiting)
        _who = State(initialValue: task.waitingOn ?? "")
        _followUp = State(initialValue: task.followUp)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    Text("Edit task").font(.system(size: 15 * z, weight: .semibold))
                    Spacer()
                    Button("Cancel") { store.editing = nil }.keyboardShortcut(.cancelAction)
                    Button("Save") { save() }.keyboardShortcut(.return, modifiers: .command).buttonStyle(.borderedProminent)
                }
                field("Title") {
                    TextField("Title", text: $title, axis: .vertical).textFieldStyle(.roundedBorder).focused($titleFocused)
                }
                field("Notes") {
                    TextEditor(text: $notes)
                        .font(.system(size: 13 * z))
                        .frame(minHeight: 140 * z)
                        .padding(4)
                        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.primary.opacity(0.15)))
                }
                if let live = store.items[task.number] {
                    ChecklistSection(task: live, store: store)          // saved as you go, like the task panel
                }
                field("Priority") {
                    Picker("", selection: $priority) { ForEach(priorities, id: \.self) { Text($0).tag($0) } }
                        .pickerStyle(.segmented).labelsHidden()
                }
                field("Tags") { TagEditor(task: task, store: store, draft: $tags) }
                field("Waiting") { waitingFields }
                field("List") {
                    HStack {
                        TextField("List", text: $list).textFieldStyle(.roundedBorder)
                        Menu("Lists") { ForEach(store.allLists, id: \.self) { l in Button(titleCase(l)) { list = l } } }.fixedSize()
                    }
                }
                field("Section") {
                    HStack {
                        TextField("none", text: $section).textFieldStyle(.roundedBorder)
                        Menu("Sections") {
                            Button("None") { section = "" }
                            ForEach(Array(Set(sections + store.sectionsInUse)).sorted(), id: \.self) { s in Button(s) { section = s } }
                        }.fixedSize()
                    }
                }
                Text("Save changes only what you edited in the fields above. Checklist edits are saved as you make them; other labels and Claude's notes stay as they are.")
                    .font(.system(size: 11 * z)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            .font(.system(size: 13 * z))
            .padding(18)
        }
        .onAppear {
            if store.focusRequest == .waiting {
                store.focusRequest = nil
                whoFocused = true
            } else {
                titleFocused = true
            }
        }
        .onChange(of: store.focusRequest) { f in
            if f == .waiting { waiting = true; whoFocused = true; store.focusRequest = nil }
        }
    }

    /// On or off; who; and the day to follow up, picked from a few or on a calendar.
    @ViewBuilder var waitingFields: some View {
        VStack(alignment: .leading, spacing: 8) {
            Toggle("Waiting on someone (greyed at the bottom of the list until the follow-up day)", isOn: $waiting)
                .toggleStyle(.checkbox)
            if waiting {
                TextField("Who? e.g. Sam, the landlord", text: $who).textFieldStyle(.roundedBorder).focused($whoFocused)
                HStack(spacing: 6) {
                    Text("Follow up").foregroundStyle(.secondary)
                    ForEach([(3, "3 days"), (7, "1 week"), (14, "2 weeks")], id: \.0) { days, label in
                        let day = Waiting.day(in: days, from: store.now)
                        Button(label) { followUp = day }
                            .buttonStyle(.bordered).controlSize(.small)
                            .tint(followUp == day ? Palette.attention : nil)
                    }
                    Button("None") { followUp = nil }.buttonStyle(.bordered).controlSize(.small)
                        .tint(followUp == nil ? Palette.attention : nil)
                }
                HStack(spacing: 6) {
                    if followUp != nil {
                        DatePicker("", selection: Binding(
                            get: { followUp.flatMap(Waiting.date) ?? store.now },
                            set: { followUp = Waiting.day($0) }), displayedComponents: .date)
                            .datePickerStyle(.field).labelsHidden().fixedSize()
                        Text(followUp.map { "on " + Waiting.short($0, now: store.now) } ?? "")
                            .foregroundStyle(.secondary)
                    } else {
                        Button("Pick a day…") { followUp = Waiting.day(in: 7, from: store.now) }
                            .buttonStyle(.borderless)
                    }
                }
            }
        }
    }

    func field<C: View>(_ label: String, @ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label).font(.system(size: 11 * z, weight: .medium)).foregroundStyle(.secondary)
            content()
        }
    }

    func save() {
        // Only what changed in the pane is sent, so a tag or waiting change made meanwhile elsewhere is kept
        let tagsChanged = Set(tags) != Set(task.tags)
        let w = WaitingEdit(on: waiting, who: who, followUp: waiting ? followUp : nil)
        let waitChanged = w.on != task.waiting || (w.on && (w.who.trimmingCharacters(in: .whitespaces) != (task.waitingOn ?? "")
                                                             || w.followUp != task.followUp))
        store.saveEdit(task.number, title: title, notes: notes, list: list, section: section.isEmpty ? nil : section,
                       priority: priority, tags: tagsChanged ? tags : nil, waiting: waitChanged ? w : nil)
        store.editing = nil
    }
}

// MARK: - assign to an agent

struct AssignSheet: View {
    @Environment(\.zoom) private var z
    let task: TaskItem
    @ObservedObject var store: Store
    var others: [Int] = []                          // several selected: this one agent handles them as well
    @State private var text = ""
    @State private var preview: String?
    @State private var previewError: String?
    @State private var showPreview = false
    @State private var sending = false
    @State private var error: String?
    @State private var previewTask: DispatchWorkItem?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Assign to an agent").font(.system(size: 15 * z, weight: .semibold))
            Text("The orchestrator starts an agent for this task and emails you which one. The task gets the status:agent-assigned label and a comment, so it isn't assigned twice. The agent comments on the task when it starts and when it's done.")
                .font(.system(size: 12 * z)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 3) {
                Text(task.title).font(.system(size: 13 * z, weight: .semibold)).lineLimit(2)
                Text(summary).font(.system(size: 11 * z)).foregroundStyle(.secondary)
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.05)))
            if !others.isEmpty {
                Text(othersNote).font(.system(size: 12 * z)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if task.assigned {
                Text("Already assigned \(Fmt.ago(task.agent?.assignedAt ?? task.issue.updatedAt, now: store.now)). Assigning again starts a second agent.")
                    .font(.system(size: 12 * z, weight: .medium)).foregroundColor(Palette.attention)
            }
            Text("Anything the agent should know? (optional)").font(.system(size: 12 * z)).foregroundStyle(.secondary)
            TextEditor(text: $text)
                .font(.system(size: 13 * z))
                .frame(height: 110 * z)
                .padding(4)
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.primary.opacity(0.18)))
            DisclosureGroup(isExpanded: $showPreview) {
                ScrollView {
                    Text(preview ?? previewError ?? "Loading…")
                        .font(.system(size: 11 * z, design: .monospaced)).foregroundStyle(.secondary)
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(height: 170 * z)
                .padding(6)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.04)))
            } label: {
                Text("Message to the orchestrator").font(.system(size: 12 * z))
            }
            if !store.link.isLive {
                Text("The desktop isn't connected, and assigning needs it. Try again once the link is back.")
                    .font(.system(size: 12 * z)).foregroundColor(Palette.red)
            }
            if let e = error {
                Text(e).font(.system(size: 12 * z)).foregroundColor(Palette.red).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Text("⌘⏎ to assign").font(.system(size: 11 * z)).foregroundStyle(.tertiary)
                Spacer()
                if sending { ProgressView().controlSize(.small) }
                Button("Cancel") { store.assigning = nil }.keyboardShortcut(.cancelAction)
                Button(task.assigned ? "Assign again" : "Assign to agent") { send() }
                    .buttonStyle(CaramelButton())
                    .keyboardShortcut(.return, modifiers: .command)
                    .disabled(sending || !store.link.isLive)
            }
        }
        .padding(22)
        .frame(width: 560 * z)
        .onAppear { loadPreview() }
        .onChange(of: text) { _ in
            previewTask?.cancel()
            let w = DispatchWorkItem { loadPreview() }
            previewTask = w
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6, execute: w)
        }
    }

    var othersNote: String {
        let titles: [String] = others.compactMap { store.items[$0]?.title }.prefix(4).map { "“\($0)”" }
        let more: String = others.count > 4 ? "…" : ""
        return "One agent does all \(others.count + 1) as one job. It is started on this task and told of the others, "
            + "which each get a comment pointing here: \(titles.joined(separator: ", "))\(more)"
    }

    var summary: String {
        var parts = [titleCase(task.list), "#\(task.number)"]
        if !task.notes.isEmpty { parts.append("notes") }
        if !task.allChecklist.isEmpty { parts.append("\(task.allChecklist.count) checklist items") }
        return parts.joined(separator: " · ") + " are included"
    }

    func loadPreview() {
        guard store.link.isLive || store.previewStub != nil else { previewError = "The desktop isn't connected."; return }
        store.assignPreview(task.number, text: store.assignText(lead: task.number, others: others, text: text)) { msg, err in
            preview = msg
            previewError = err.map { "Couldn't build the message: \($0)" }
        }
    }

    func send() {
        sending = true
        error = nil
        let lead = task.number, rest = others
        store.assign(lead, text: store.assignText(lead: lead, others: rest, text: text), again: task.assigned) { err in
            sending = false
            if let e = err { error = e } else {
                store.noteOthers(rest, lead: lead)
                store.marked = []
                store.assigning = nil
            }
        }
    }
}

// MARK: - confirmations (NSAlert, as AgentBoard does)

@MainActor
enum Dialogs {
    static weak var window: NSWindow?

    private static func present(_ alert: NSAlert, _ done: @escaping (NSApplication.ModalResponse) -> Void) {
        if let w = window, w.isVisible {
            alert.beginSheetModal(for: w, completionHandler: done)
        } else {
            done(alert.runModal())
        }
    }

    static func deleteTask(_ t: TaskItem, store: Store) {
        let alert = NSAlert()
        alert.messageText = "Delete “\(t.title)”?"
        alert.informativeText = "It is closed and taken out of its list, as the web app deletes. You can undo it from the bottom bar for a few seconds."
        let b = alert.addButton(withTitle: "Delete")
        b.hasDestructiveAction = true
        alert.addButton(withTitle: "Cancel")
        present(alert) { r in if r == .alertFirstButtonReturn { store.deleteTask(t.number) } }
    }

    /// Several tasks (or a group): deleted together, a group with its open tasks.
    static func deleteMany(_ numbers: [Int], store: Store) {
        let withTasks = Grouping.withMembers(numbers, all: store.live).count
        let alert = NSAlert()
        alert.messageText = numbers.count == 1 ? "Delete “\(store.items[numbers[0]]?.title ?? "this task")”?" : "Delete \(numbers.count) tasks?"
        alert.informativeText = (withTasks > numbers.count ? "Groups go with their tasks (\(withTasks) in all). " : "")
            + "They are closed and taken out of their lists. ⌘Z undoes it."
        let b = alert.addButton(withTitle: "Delete")
        b.hasDestructiveAction = true
        alert.addButton(withTitle: "Cancel")
        present(alert) { r in if r == .alertFirstButtonReturn { store.deleteMany(numbers) } }
    }

    /// Name a new group of tasks (prefilled with the first task's title); joining an existing group asks nothing.
    static func groupName(store: Store, numbers: [Int]) {
        if numbers.filter({ store.isGroup($0) }).count == 1 { store.groupMany(numbers, name: ""); return }
        let alert = NSAlert()
        alert.messageText = "Group \(numbers.count) tasks"
        alert.informativeText = "They stay in this list, under one task you can fold away. Ticking the group ticks them all."
        let field = NSTextField(string: store.items[numbers.first ?? 0]?.title ?? "")
        field.frame = NSRect(x: 0, y: 0, width: 300, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: "Group")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        present(alert) { r in
            guard r == .alertFirstButtonReturn else { return }
            store.groupMany(numbers, name: field.stringValue)
        }
    }

    /// Name a new tag. `done` gets the name as typed; callers normalise it.
    static func askTag(store: Store, done: @escaping (String) -> Void) {
        let alert = NSAlert()
        alert.messageText = "New tag"
        alert.informativeText = "For example “home”, for what you can only do at home. Lower case, with dashes for spaces. Filter the list by it with the tag chips above the tasks."
        let field = NSTextField(string: "")
        field.placeholderString = "e.g. home"
        field.frame = NSRect(x: 0, y: 0, width: 260, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: "Add tag")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        present(alert) { r in
            guard r == .alertFirstButtonReturn, !Tags.normalise(field.stringValue).isEmpty else { return }
            done(field.stringValue)
        }
    }

    static func newTag(store: Store, numbers: [Int]) {
        askTag(store: store) { name in store.tagMany(numbers, name, on: true) }
    }

    static func deleteList(_ name: String, store: Store) {
        let n = store.open(in: name).count + store.completed(in: name).count
        let alert = NSAlert()
        alert.messageText = "Delete the list “\(titleCase(name))”?"
        alert.informativeText = n == 0 ? "It has no tasks." : "Its \(n) task\(n == 1 ? "" : "s") move to Tasks. Nothing is deleted from GitHub."
        let b = alert.addButton(withTitle: "Delete list")
        b.hasDestructiveAction = true
        alert.addButton(withTitle: "Cancel")
        present(alert) { r in if r == .alertFirstButtonReturn { store.deleteList(name) } }
    }

    static func newList(store: Store, group: String?) {
        let alert = NSAlert()
        alert.messageText = group.map { "New list in \($0)" } ?? "New list"
        let field = NSTextField(string: "")
        field.placeholderString = "e.g. Gifts"
        let popup = NSPopUpButton(frame: .zero, pullsDown: false)
        popup.addItem(withTitle: "No group")
        popup.addItems(withTitles: store.groupNames)
        if let g = group { popup.selectItem(withTitle: g) }
        let stack = NSStackView(views: [field, popup])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.frame = NSRect(x: 0, y: 0, width: 280, height: 56)
        field.frame = NSRect(x: 0, y: 0, width: 280, height: 24)
        field.widthAnchor.constraint(equalToConstant: 280).isActive = true
        alert.accessoryView = stack
        alert.addButton(withTitle: "Create")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        present(alert) { r in
            guard r == .alertFirstButtonReturn else { return }
            let g = popup.indexOfSelectedItem > 0 ? popup.titleOfSelectedItem : nil
            store.newList(field.stringValue, group: g)
        }
    }

    static func newGroup(store: Store) {
        let alert = NSAlert()
        alert.messageText = "New group"
        alert.informativeText = "Groups hold lists. Move a list into it from the list's right-click menu."
        let field = NSTextField(string: "")
        field.frame = NSRect(x: 0, y: 0, width: 260, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: "Create")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        present(alert) { r in if r == .alertFirstButtonReturn { store.newGroup(field.stringValue) } }
    }

    static func host(store: Store) {
        let alert = NSAlert()
        alert.messageText = "Desktop SSH host"
        alert.informativeText = "An alias from ~/.ssh/config that logs in without a prompt. Helper on the desktop: \(Settings.helper)"
        let field = NSTextField(string: Settings.host)
        field.frame = NSRect(x: 0, y: 0, width: 260, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        present(alert) { r in
            let h = field.stringValue.trimmingCharacters(in: .whitespaces)
            guard r == .alertFirstButtonReturn, !h.isEmpty, !h.hasPrefix("-") else { return }
            Settings.host = h
            store.reconnect()
        }
    }
}
