import AppKit
import Carbon
import SwiftUI

// The Triage page: one card at a time, drawn by Triage.pick (Triage.swift). The card's task also shows
// in the panel on the right, so it can be read in full, edited, ticked or assigned there as usual.

struct TriageColumn: View {
    @Environment(\.zoom) private var z
    @ObservedObject var store: Store
    @ObservedObject var deck = TriageDeck.shared

    static let keys = "→ or space next · ← or a archive · w this week · s snooze · d done · x delete · g agent · "
        + "1 2 3 or h m l importance · u urgent · return edit · ⌘Z undo"

    var body: some View {
        let card = deck.card(store)
        let count = deck.poolCount(store)
        let focus = Triage.focusTasks(store.live)
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    ColumnHeader(title: "Triage", subtitle: "\(count) to look at · \(focus.count)/\(Triage.focusCap) this week")
                    Text("One task at a time from the rated lists. Unrated, important and long-untouched tasks come up most. Snoozes stay on this Mac.")
                        .font(.system(size: 12 * z)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 6).padding(.bottom, 14)
                    if let t = card {
                        TriageCard(task: t, store: store, deck: deck)
                    } else {
                        VStack(alignment: .leading, spacing: 6) {
                            Image(systemName: "checkmark.circle").font(.system(size: 30 * z)).foregroundStyle(.tertiary)
                            Text(store.hasLoaded ? "Nothing to triage" : "Loading tasks…").font(.system(size: 15 * z, weight: .semibold))
                            if store.hasLoaded {
                                Text("Every open task in a rated list is snoozed or waiting on someone, or there are none.")
                                    .font(.system(size: 12.5 * z)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .padding(.vertical, 30)
                    }
                    if !focus.isEmpty { TriageThisWeek(tasks: focus, store: store).padding(.top, 28) }
                }
                .padding(.horizontal, 22).padding(.vertical, 16)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            // the actions stay in view under a long card
            if let t = card {
                Divider()
                VStack(alignment: .leading, spacing: 7) {
                    TriageActions(task: t, store: store, deck: deck)
                    Text("Keys: " + TriageColumn.keys).font(.system(size: 10.5 * z)).foregroundStyle(.tertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.horizontal, 22).padding(.vertical, 10)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .onAppear { deck.ensureCard(store) }
        // the card's task was closed or deleted (its panel, ⌘⌫, another device), or a first sync brought tasks
        .onChange(of: card == nil && count > 0) { need in if need { deck.ensureCard(store) } }
        .onChange(of: store.assigning == nil) { closed in if closed { deck.assignClosed(store) } }
    }
}

struct TriageCard: View {
    @Environment(\.zoom) private var z
    let task: TaskItem
    @ObservedObject var store: Store
    @ObservedObject var deck: TriageDeck

    var body: some View {
        let hint = deck.prefs.snoozes[task.number]?.hint
        let step = Triage.firstStep(task.claudeNotes)
        let notes = Triage.cardNotes(task.notes)
        let items = task.allChecklist
        let importance = task.importance
        let marks = Rating.marks(task, now: store.now)
        VStack(alignment: .leading, spacing: 10 * z) {
            FlowLayout(spacing: 6) {
                TriageChip(text: titleCase(task.list))
                if let i = importance {
                    TriageChip(text: "\(i) importance", color: i == "high" ? Palette.red : i == "low" ? Palette.green : nil)
                } else {
                    TriageChip(text: "unrated", color: Palette.attention, bold: true)
                }
                if marks.urgent { TriageChip(text: "urgent", color: Palette.red) }
                if let d = marks.due { TriageChip(text: d, color: marks.overdue ? Palette.red : nil) }
                if Triage.hasFocus(task.issue.labels) { TriageChip(text: "this week", color: Palette.blue, bold: true) }
                if task.cameThrough {
                    TriageChip(text: task.waitingOn.map { "came through? \($0)" } ?? "came through?", color: Palette.attention)
                } else if task.followUpDue(today: store.today) {
                    TriageChip(text: task.waitingOn.map { "follow up with \($0)" } ?? "follow up", color: Palette.attention)
                }
                ForEach(task.tags, id: \.self) { t in TriageChip(text: t, color: Palette.tag) }
                if task.assigned {
                    TriageChip(text: task.agentActive ? "\(task.agent?.name ?? "agent") working" : "agent " + (task.agent?.status ?? "assigned"),
                               color: Palette.caramel)
                        .onTapGesture(count: 2) { store.openAgentMail(task) }
                        .help("Double-click to open the agent's latest email")
                }
            }
            Text(task.title)
                .font(.system(size: 19 * z, weight: .semibold))
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            Text(facts).font(.system(size: 11.5 * z)).foregroundStyle(.secondary)
            if let h = hint {
                Label(h, systemImage: "moon.zzz.fill").font(.system(size: 12.5 * z, weight: .semibold)).foregroundColor(Palette.caramel)
            }
            if let s = step {
                (Text("First step: ").fontWeight(.semibold) + Text(RichText.attributed(s)))
                    .font(.system(size: 13 * z)).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
            if !notes.isEmpty {
                RichText(text: notes).font(.system(size: 13 * z)).foregroundStyle(.secondary)
            }
            if !items.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(Array(items.prefix(6))) { item in ChecklistRow(item: item, task: task, store: store) }
                    if items.count > 6 {
                        Text("and \(items.count - 6) more in the panel").font(.system(size: 11.5 * z)).foregroundStyle(.secondary)
                    }
                }
            }
            Divider().padding(.vertical, 2)
            TriageRating(task: task, store: store, deck: deck)
        }
        .padding(16 * z)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color.primary.opacity(0.045)))
        .overlay(RoundedRectangle(cornerRadius: 12)
            .strokeBorder(task.agentActive ? Palette.caramel : Color.primary.opacity(0.1), lineWidth: task.agentActive ? 1.5 : 1))
    }

    var facts: String {
        var parts = ["#\(task.number)", "updated " + Fmt.ago(task.issue.updatedAt, now: store.now),
                     "added " + Fmt.day(task.issue.createdAt)]
        if let s = task.section { parts.append(s) }
        return parts.joined(separator: " · ")
    }
}

struct TriageChip: View {
    @Environment(\.zoom) private var z
    let text: String
    var color: Color? = nil
    var bold = false

    var body: some View {
        Text(text).font(.system(size: 11 * z, weight: bold ? .semibold : .regular))
            .foregroundColor(color ?? .secondary)
            .padding(.horizontal, 8).padding(.vertical, 2 * z)
            .background(Capsule().fill((color ?? Color.primary).opacity(color == nil ? 0.05 : 0.1)))
            .overlay(Capsule().strokeBorder((color ?? Color.primary).opacity(color == nil ? 0.16 : 0.45)))
    }
}

/// Importance, urgency and a due date, each saved at once (and undoable), as on the web.
struct TriageRating: View {
    @Environment(\.zoom) private var z
    let task: TaskItem
    @ObservedObject var store: Store
    @ObservedObject var deck: TriageDeck

    var body: some View {
        let due = Rating.due(task)
        FlowLayout(spacing: 14) {
            HStack(spacing: 5) {
                Text("Importance").font(.system(size: 11.5 * z)).foregroundStyle(.secondary)
                TriageChoice(title: "High", key: "1", on: task.importance == "high", color: Palette.red) { deck.rate(store, importance: "high") }
                TriageChoice(title: "Medium", key: "2", on: task.importance == "medium", color: Palette.slate) { deck.rate(store, importance: "medium") }
                TriageChoice(title: "Low", key: "3", on: task.importance == "low", color: Palette.green) { deck.rate(store, importance: "low") }
            }
            .fixedSize()
            TriageChoice(title: "Urgent", key: "u", on: Rating.urgent(task), color: Palette.red) { deck.toggleUrgent(store) }
            Menu {
                ForEach(Rating.duePresets, id: \.1) { label, days in
                    Button(label) { deck.setDue(store, Waiting.day(in: days, from: store.now)) }
                }
                Button("Pick a day…") { TriageDialogs.pickDue(store: store, task: task) }
                if due != nil {
                    Divider()
                    Button("No due date") { deck.setDue(store, nil) }
                }
            } label: {
                Label(due.map { "Due " + Waiting.short($0, now: store.now) } ?? "Due date", systemImage: "calendar")
                    .font(.system(size: 12 * z))
            }
            .menuStyle(.borderlessButton).fixedSize()
            .help("A due date (due:YYYY-MM-DD)")
        }
    }
}

/// One choice in the rating row: filled in its colour when it is the task's.
struct TriageChoice: View {
    @Environment(\.zoom) private var z
    let title: String
    let key: String
    let on: Bool
    let color: Color
    let tap: () -> Void

    var body: some View {
        Button(action: tap) {
            HStack(spacing: 4) {
                Text(title)
                Text(key).font(.system(size: 9.5 * z, weight: .medium, design: .monospaced)).opacity(0.6)
            }
            .font(.system(size: 12 * z, weight: on ? .semibold : .regular))
            .foregroundColor(on ? .white : .secondary)
            .padding(.horizontal, 10).padding(.vertical, 3.5 * z)
            .background(Capsule().fill(on ? color : Color.clear))
            .overlay(Capsule().strokeBorder(on ? Color.clear : Color.primary.opacity(0.18)))
            .contentShape(Capsule())
            .fixedSize()
        }
        .buttonStyle(.plain)
        .help("\(title) (\(key))")
    }
}

struct TriageActions: View {
    @Environment(\.zoom) private var z
    let task: TaskItem
    @ObservedObject var store: Store
    @ObservedObject var deck: TriageDeck

    var body: some View {
        let focus = Triage.hasFocus(task.issue.labels)
        let length = Fmt.formatDayCount(Triage.snoozeDays(deck.prefs.snoozes[task.number]?.count ?? 0))
        FlowLayout(spacing: 8) {
            button("Archive", "a", "archivebox",
                   help: "Close as not planned. It stays in its list, so it can still be found (a or ←; ⌘Z undoes it)") { deck.archive(store) }
            button(focus ? "Not this week" : "This week", "w", focus ? "star.slash" : "star", tint: focus ? Palette.blue : nil,
                   help: "This week: at most \(Triage.focusCap) tasks, the focus:week label (w)") { deck.toggleFocus(store) }
            button("Snooze \(length)", "s", "moon.zzz", help: "Hide it from Triage on this Mac for \(length) (s)") { deck.snooze(store) }
            button("Done", "d", "checkmark", tint: Palette.green, help: "Complete it (d; ⌘Z undoes it)") { deck.done(store) }
            button("Agent", "g", "person.crop.circle.badge.plus", fill: Palette.caramel,
                   help: "Assign it to an agent (g)") { deck.assign(store) }
                .disabled(task.isLocal)
            button("Delete", "x", "trash", tint: Palette.red, help: "Delete it, after asking (x)") { deck.delete(store) }
            button("Next", "→", nil, fill: Palette.blue, help: "Keep it as it is and show another (→ or space)") { deck.next(store) }
        }
    }

    func button(_ title: String, _ key: String, _ icon: String?, tint: Color? = nil, fill: Color? = nil, help: String,
                _ run: @escaping () -> Void) -> some View {
        Button(action: run) {
            HStack(spacing: 5) {
                if let i = icon { Image(systemName: i).font(.system(size: 11 * z, weight: .semibold)) }
                Text(title)
                Text(key).font(.system(size: 10 * z, weight: .medium, design: .monospaced)).opacity(0.6)
            }
            .fixedSize()
        }
        .buttonStyle(TriageButtonStyle(fill: fill, tint: tint))
        .help(help)
    }
}

/// Triage's buttons: a quiet outline, or filled (Next in blue, Agent in AgentBoard's caramel).
struct TriageButtonStyle: ButtonStyle {
    var fill: Color? = nil
    var tint: Color? = nil
    @Environment(\.zoom) private var z
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12.5 * z, weight: fill == nil ? .medium : .semibold))
            .foregroundColor(fill != nil ? .white : (tint ?? .primary))
            .padding(.horizontal, 11).padding(.vertical, 5.5 * z)
            .background(RoundedRectangle(cornerRadius: 7)
                .fill(fill.map { $0.opacity(configuration.isPressed ? 0.75 : 1) } ?? Color.primary.opacity(configuration.isPressed ? 0.14 : 0.06)))
            .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(fill == nil ? Color.primary.opacity(0.12) : Color.clear))
            .opacity(enabled ? 1 : 0.45)
            .contentShape(RoundedRectangle(cornerRadius: 7))
    }
}

/// The tasks in This week, each a click from its panel, and one more from leaving it.
struct TriageThisWeek: View {
    @Environment(\.zoom) private var z
    let tasks: [TaskItem]
    @ObservedObject var store: Store

    var body: some View {
        DetailSection(title: "This week \(tasks.count)/\(Triage.focusCap)") {
            ForEach(tasks) { t in
                HStack(spacing: 8) {
                    Image(systemName: "star.fill").font(.system(size: 10 * z)).foregroundColor(Palette.blue)
                    Button { store.selection = t.number } label: {
                        Text(t.title).font(.system(size: 13 * z)).lineLimit(1).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Show it in the panel")
                    Spacer(minLength: 6)
                    Text(titleCase(t.list)).font(.system(size: 11 * z)).foregroundStyle(.secondary)
                    Button { store.setFocus(on: [], off: [t.number]) } label: {
                        Image(systemName: "xmark").font(.system(size: 9 * z, weight: .bold))
                    }
                    .buttonStyle(.borderless)
                    .help("Take it off this week")
                }
                .padding(.vertical, 2 * z)
            }
        }
    }
}

// MARK: - keys

@MainActor
enum TriageKeys {
    /// A plain key on the Triage page. App.swift's key monitor asks this first, when no text field is being typed in;
    /// true means Triage used it. Return, esc and the ⌘ keys stay the app's (return edits the card's task).
    static func handle(_ e: NSEvent, store: Store) -> Bool {
        guard store.route == .triage, e.modifierFlags.intersection([.command, .option, .control, .shift]).isEmpty else { return false }
        let deck = TriageDeck.shared
        guard deck.card(store) != nil else { return false }
        switch e.keyCode {
        case UInt16(kVK_RightArrow), UInt16(kVK_Space): deck.next(store); return true
        case UInt16(kVK_LeftArrow): deck.archive(store); return true
        default: break
        }
        switch e.charactersIgnoringModifiers ?? "" {
        case "a": deck.archive(store)
        case "w": deck.toggleFocus(store)
        case "s": deck.snooze(store)
        case "d": deck.done(store)
        case "x": deck.delete(store)
        case "g": deck.assign(store)
        case "h", "1": deck.rate(store, importance: "high")
        case "m", "2": deck.rate(store, importance: "medium")
        case "l", "3": deck.rate(store, importance: "low")
        case "u": deck.toggleUrgent(store)
        default: return false
        }
        return true
    }
}

// MARK: - questions (NSAlert, as the app's other dialogs)

@MainActor
enum TriageDialogs {
    private static func present(_ alert: NSAlert, _ done: @escaping (NSApplication.ModalResponse) -> Void) {
        if let w = Dialogs.window, w.isVisible {
            alert.beginSheetModal(for: w, completionHandler: done)
        } else {
            done(alert.runModal())
        }
    }

    /// This week is full: say so, and offer to swap one of them out for this task.
    static func replaceFocus(store: Store, task: TaskItem, focused: [TaskItem]) {
        let alert = NSAlert()
        alert.messageText = "This week already has \(Triage.focusCap)"
        alert.informativeText = "Replace one with “\(task.title)”?"
        let popup = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 340, height: 26), pullsDown: false)
        for f in focused {                              // menu items, not addItem(withTitle:), which drops repeated titles
            let title = f.title.count > 60 ? String(f.title.prefix(58)) + "…" : f.title
            popup.menu?.addItem(NSMenuItem(title: title, action: nil, keyEquivalent: ""))
        }
        alert.accessoryView = popup
        alert.addButton(withTitle: "Replace")
        alert.addButton(withTitle: "Keep them")
        present(alert) { r in
            let i = popup.indexOfSelectedItem
            guard r == .alertFirstButtonReturn, focused.indices.contains(i) else { return }
            store.setFocus(on: [task.number], off: [focused[i].number])
        }
    }

    /// Any day, on a calendar.
    static func pickDue(store: Store, task: TaskItem) {
        let alert = NSAlert()
        alert.messageText = "Due date"
        alert.informativeText = "For “\(task.title)”."
        let picker = NSDatePicker()
        picker.datePickerStyle = .clockAndCalendar
        picker.datePickerElements = .yearMonthDay
        picker.dateValue = Rating.due(task).flatMap(Waiting.date) ?? Calendar.current.date(byAdding: .day, value: 7, to: Date()) ?? Date()
        picker.sizeToFit()
        alert.accessoryView = picker
        alert.addButton(withTitle: "Set")
        alert.addButton(withTitle: "Cancel")
        present(alert) { r in
            guard r == .alertFirstButtonReturn else { return }
            store.setDue(task.number, Waiting.day(picker.dateValue))
        }
    }
}

// MARK: - the offscreen renderer

extension TriageDeck {
    /// `--render … --route triage`: the Triage page, its card drawn with a fixed seed (`--seed N`, default 1) and
    /// nothing read from or saved to `defaults`. `--snoozed` gives the card four past snoozes (the archive hint).
    /// The panel shows the card's task, as in the app.
    static func prepareRender(_ store: Store, args: [String] = CommandLine.arguments) {
        func value(_ name: String) -> String? {
            guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
            return args[i + 1]
        }
        guard value("--route") == "triage" else { return }
        let rng = SeededRandom(value("--seed").flatMap { UInt64($0) } ?? 1)
        let deck = TriageDeck(prefs: TriagePrefs(defaults: nil), random: { rng.next() })
        shared = deck
        store.route = .triage
        deck.next(store)
        if args.contains("--snoozed"), let n = deck.current { deck.prefs.setSnooze(n, Snooze(count: 4, until: 0)) }
        print("triage card: #\(deck.current.map(String.init) ?? "none") of \(deck.poolCount(store))")
    }
}
