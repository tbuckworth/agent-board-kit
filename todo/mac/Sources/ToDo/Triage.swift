import AppKit
import Carbon
import Combine
import Foundation

// Triage: one weighted-random task at a time, to archive, put in This week, rate, snooze, finish, delete or hand to an
// agent. The rules are the web app's (web/js/triage.js); keep the two in step.
//
// This file holds the pure rules (`Triage`), this Mac's memory of snoozes and showings (`TriagePrefs`), the deck that
// holds the card on screen (`TriageDeck`), the Store actions Triage adds, and the self-test. TriageView.swift draws it.

/// A task's snoozes on this Mac: how many so far, and when the current one ends (epoch milliseconds, as the web app
/// keeps it).
struct Snooze: Equatable {
    var count: Int
    var until: Double

    func isActive(_ now: Double) -> Bool { until > now }

    /// "Snoozed 4 times. Archive?" from the fourth snooze on.
    var hint: String? { count >= Triage.snoozeHintCount ? "Snoozed \(count) times. Archive?" : nil }
}

/// The pool is open tasks on GitHub in rated lists, not snoozed and not quietly waiting on someone. Weight = importance (unrated 6, high 3, medium 2, low 1)
/// × recency (shown in triage in the last day 0.1, in the last week 0.4, else 1) × age (1 + days since GitHub last
/// updated it ÷ 30, at most 4).
enum Triage {
    static let focusLabel = "focus:week"
    static let focusCap = 5
    /// 1 day, 3 days, 1 week, 3 weeks, then 3 weeks again.
    static let snoozeDays = [1, 3, 7, 21]
    static let snoozeHintCount = 4
    static let unratedWeight = 6.0
    static let importanceWeights: [String: Double] = ["high": 3, "medium": 2, "low": 1]
    static let recentDayFactor = 0.1
    static let recentWeekFactor = 0.4
    static let ageCapDays = 90.0
    static let shownKeepDays = 30.0
    static let day = 86_400_000.0                   // milliseconds

    static func ms(_ d: Date) -> Double { (d.timeIntervalSince1970 * 1000).rounded() }

    static func importanceWeight(_ importance: String?) -> Double {
        guard let i = importance else { return unratedWeight }
        return importanceWeights[i.lowercased()] ?? 1
    }

    static func snoozeDays(_ previousCount: Int) -> Int { snoozeDays[min(max(0, previousCount), snoozeDays.count - 1)] }

    /// The snooze after one more: the interval grows with the count.
    static func nextSnooze(_ current: Snooze?, now: Double) -> Snooze {
        let count = current?.count ?? 0
        return Snooze(count: count + 1, until: now + Double(snoozeDays(count)) * day)
    }

    static func recencyFactor(_ lastShown: Double?, now: Double) -> Double {
        guard let s = lastShown else { return 1 }
        let age = now - s
        if age < day { return recentDayFactor }
        if age < 7 * day { return recentWeekFactor }
        return 1
    }

    static func ageFactor(_ updatedAt: String?, now: Double) -> Double {
        guard let d = Fmt.date(updatedAt) else { return 1 }
        let days = max(0, (now - ms(d)) / day)
        return 1 + min(days, ageCapDays) / 30
    }

    static func weight(_ t: TaskItem, now: Double, lastShown: Double?) -> Double {
        importanceWeight(t.importance) * recencyFactor(lastShown, now: now) * ageFactor(t.issue.updatedAt, now: now)
    }

    struct Candidate {
        var task: TaskItem
        var weight: Double
    }

    /// Whether a task may be drawn: open, on GitHub, not waiting on someone before its follow-up day, in a rated list
    /// (`config`'s unratedGroups and unratedLists), and not snoozed.
    static func eligible(_ t: TaskItem, groups: [String: String?], config: TodoConfig?, snoozes: [Int: Snooze], now: Double,
                         today: String) -> Bool {
        guard t.isOpen, t.number > 0, !t.greyed(today: today) else { return false }
        guard Rating.isRatedList(t.list, group: Rating.group(of: t, in: groups), config: config) else { return false }
        return snoozes[t.number]?.isActive(now) != true
    }

    /// The pool in issue-number order, so a seeded draw always gives the same card.
    static func pool(_ tasks: [TaskItem], config: TodoConfig?, snoozes: [Int: Snooze], shown: [Int: Double],
                     now: Double, today: String) -> [Candidate] {
        let groups = Rating.groupsByList(config)
        return tasks.filter { eligible($0, groups: groups, config: config, snoozes: snoozes, now: now, today: today) }
            .sorted { $0.number < $1.number }
            .map { Candidate(task: $0, weight: weight($0, now: now, lastShown: shown[$0.number])) }
    }

    /// A weighted random task, avoiding `exclude` (the card just shown) unless it is the only one left.
    /// `random` returns a number in [0, 1).
    static func pick(_ tasks: [TaskItem], config: TodoConfig?, snoozes: [Int: Snooze], shown: [Int: Double],
                     now: Double, today: String, exclude: Int?, random: () -> Double) -> TaskItem? {
        let all = pool(tasks, config: config, snoozes: snoozes, shown: shown, now: now, today: today)
        let others = all.filter { $0.task.number != exclude }
        let candidates = others.isEmpty ? all : others
        guard !candidates.isEmpty else { return nil }
        var ticket = random() * candidates.reduce(0) { $0 + $1.weight }
        for c in candidates {
            if ticket < c.weight { return c.task }
            ticket -= c.weight
        }
        return candidates.last?.task
    }

    static func pruneShown(_ shown: [Int: Double], now: Double) -> [Int: Double] {
        shown.filter { now - $0.value < shownKeepDays * day }
    }

    // MARK: This week: one label, at most five open tasks (Covey's weekly big rocks)

    static func hasFocus(_ labels: [String]) -> Bool { labels.contains { $0.lowercased() == focusLabel } }

    static func focusTasks(_ tasks: [TaskItem]) -> [TaskItem] {
        tasks.filter { $0.isOpen && hasFocus($0.issue.labels) }.sorted { $0.issue.updatedAt > $1.issue.updatedAt }
    }

    // MARK: what the card shows

    private static let firstStepPattern = try! NSRegularExpression(pattern: #"^\W*\*\*First step:?\*\*:?\s*(.+)$"#,
                                                                    options: [.caseInsensitive, .anchorsMatchLines])

    /// The agent's "**First step:** …" line in the Claude notes (as nudges and the widget read it), or nil.
    static func firstStep(_ claudeNotes: String?) -> String? {
        guard let s = claudeNotes else { return nil }
        let ns = s as NSString
        guard let m = firstStepPattern.firstMatch(in: s, range: NSRange(location: 0, length: ns.length)) else { return nil }
        let step = ns.substring(with: m.range(at: 1)).trimmingCharacters(in: .whitespaces)
        return step.isEmpty ? nil : step
    }

    /// Notes on the card: heading and bold marks dropped, blank runs closed up, cut at a word near
    /// `max` characters. The task's panel has the rest.
    static func cardNotes(_ notes: String, max: Int = 400) -> String {
        let plain = notes.components(separatedBy: "\n")
            .map { $0.replacingOccurrences(of: #"^#{1,6}\s+"#, with: "", options: .regularExpression)
                .replacingOccurrences(of: "**", with: "").replacingOccurrences(of: "`", with: "") }
            .joined(separator: "\n")
            .replacingOccurrences(of: #"\n{3,}"#, with: "\n\n", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard plain.count > max else { return plain }
        let cut = String(plain.prefix(max))
        let word = cut.range(of: " ", options: .backwards).map { String(cut[..<$0.lowerBound]) } ?? cut
        return word + " …"
    }
}

// MARK: - this Mac's memory

/// Triage's memory on this Mac only, as each browser keeps its own: snoozes and when each task was last shown. Kept
/// in `defaults` for org.agentkit.todo as `triageSnoozes` ({"N": {"count", "until"}}) and
/// `triageShown` ({"N": epoch ms}). Without `defaults` (the offscreen renderer, the self-test) nothing is saved.
final class TriagePrefs {
    static let snoozesKey = "triageSnoozes"
    static let shownKey = "triageShown"
    private let defaults: UserDefaults?
    private(set) var snoozes: [Int: Snooze] = [:]
    private(set) var shown: [Int: Double] = [:]

    init(defaults: UserDefaults?) {
        self.defaults = defaults
        guard let d = defaults else { return }
        for (k, v) in d.dictionary(forKey: TriagePrefs.snoozesKey) ?? [:] {
            guard let n = Int(k), let e = v as? [String: Any], let c = (e["count"] as? NSNumber)?.intValue,
                  let u = (e["until"] as? NSNumber)?.doubleValue else { continue }
            snoozes[n] = Snooze(count: c, until: u)
        }
        for (k, v) in d.dictionary(forKey: TriagePrefs.shownKey) ?? [:] {
            if let n = Int(k), let t = (v as? NSNumber)?.doubleValue { shown[n] = t }
        }
    }

    /// Snooze task `n` once more; returns the new snooze.
    @discardableResult
    func snooze(_ n: Int, now: Double) -> Snooze {
        let next = Triage.nextSnooze(snoozes[n], now: now)
        snoozes[n] = next
        save()
        return next
    }

    /// Set a snooze outright (the renderer's --snoozed, the self-test).
    func setSnooze(_ n: Int, _ s: Snooze?) {
        snoozes[n] = s
        save()
    }

    func recordShown(_ n: Int, now: Double) {
        shown[n] = now
        shown = Triage.pruneShown(shown, now: now)
        save()
    }

    private func save() {
        guard let d = defaults else { return }
        var s: [String: Any] = [:]
        for (n, e) in snoozes { s[String(n)] = ["count": e.count, "until": e.until] }
        d.set(s, forKey: TriagePrefs.snoozesKey)
        var h: [String: Any] = [:]
        for (n, t) in shown { h[String(n)] = t }
        d.set(h, forKey: TriagePrefs.shownKey)
    }
}

/// SplitMix64: a seeded draw for the offscreen renderer, so `--seed N` always shows the same card.
final class SeededRandom {
    private var state: UInt64
    init(_ seed: UInt64) { state = seed }

    func next() -> Double {
        state &+= 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        z ^= z >> 31
        return Double(z >> 11) / Double(UInt64(1) << 53)
    }
}

// MARK: - the card on screen, and what Triage's buttons and keys do

/// The card on screen and Triage's actions on it. The app has one (`shared`); the renderer and the self-test make
/// their own, with a fixed draw and nothing saved. Every change to a task goes through the Store, so it is queued,
/// conflict-checked and undoable like any other edit.
@MainActor
final class TriageDeck: ObservableObject {
    static var shared = TriageDeck(prefs: TriagePrefs(defaults: Store.defaults))

    @Published private(set) var current: Int?
    @Published private(set) var revision = 0          // bumped when snoozes change, which no Store change reports
    let prefs: TriagePrefs
    var random: () -> Double
    var clock: () -> Date
    /// The card's task as the Assign sheet opened on it: a changed issue when the sheet closes means it was assigned.
    private var assignWatch: (number: Int, issue: Issue)?

    init(prefs: TriagePrefs, random: @escaping () -> Double = { Double.random(in: 0..<1) },
         clock: @escaping () -> Date = { Date() }) {
        self.prefs = prefs
        self.random = random
        self.clock = clock
    }

    /// The card's task, while it is still open and not deleted.
    func card(_ store: Store) -> TaskItem? {
        guard let n = current, let t = store.items[n], t.isOpen, !store.isDeleted(t) else { return nil }
        return t
    }

    func poolCount(_ store: Store) -> Int {
        let now = clock()
        let groups = Rating.groupsByList(store.config)
        let today = Waiting.day(now), ms = Triage.ms(now)
        return store.live.filter { Triage.eligible($0, groups: groups, config: store.config, snoozes: prefs.snoozes, now: ms,
                                                   today: today) }.count
    }

    /// Draw the next card (never the one just shown unless it is the only one) and show its task in the panel.
    func next(_ store: Store) {
        let now = clock()
        let previous = current
        let t = Triage.pick(store.live, config: store.config, snoozes: prefs.snoozes, shown: prefs.shown,
                            now: Triage.ms(now), today: Waiting.day(now), exclude: previous, random: random)
        current = t?.number
        if let n = current { prefs.recordShown(n, now: Triage.ms(now)) }
        if previous != nil, store.editing == previous { store.editing = nil }
        if let n = current { store.selection = n } else if store.selection == previous { store.selection = nil }
    }

    /// A card if there should be one: on opening Triage, after a first sync, or once the card's task is closed or
    /// deleted, here (its panel's Complete, ⌘⌫) or elsewhere. Back on Triage, the panel shows the card's task again.
    func ensureCard(_ store: Store) {
        if let n = current, card(store) != nil {
            store.selection = n
            return
        }
        next(store)
    }

    // Archive and Done draw the next card first, so the closing task never leaves the page empty

    /// Close as "not planned"; it stays in its list (a, ←).
    func archive(_ store: Store) {
        guard let t = card(store) else { return }
        next(store)
        store.archive(t.number)
    }

    /// Close as done (d). A group's open tasks go with it, as everywhere on the Mac.
    func done(_ store: Store) {
        guard let t = card(store) else { return }
        next(store)
        store.toggleComplete(t.number)
    }

    /// The app's delete, after asking (x). The card moves on when the task closes.
    func delete(_ store: Store) {
        guard let t = card(store) else { return }
        Dialogs.deleteTask(t, store: store)
    }

    /// On this Mac only, for 1 day, 3 days, 1 week, then 3 weeks (s).
    func snooze(_ store: Store) {
        guard let t = card(store) else { return }
        let s = prefs.snooze(t.number, now: Triage.ms(clock()))
        revision += 1
        store.show("Snoozed “\(t.title)” for \(Fmt.formatDayCount(Triage.snoozeDays(s.count - 1)))")
        next(store)
    }

    /// In or out of This week (w). At five, ask which one to swap out. Stays on the card either way: moving on
    /// silently made the next key act on a task not yet seen (the web app's lesson).
    func toggleFocus(_ store: Store) {
        guard let t = card(store) else { return }
        if Triage.hasFocus(t.issue.labels) { store.setFocus(on: [], off: [t.number]); return }
        let focused = Triage.focusTasks(store.live)
        if focused.count >= Triage.focusCap {
            TriageDialogs.replaceFocus(store: store, task: t, focused: focused)
        } else {
            store.setFocus(on: [t.number], off: [])
        }
    }

    /// Importance, saved at once (h/m/l or 1/2/3). A rated task with no urgency gets "not urgent", as new tasks do.
    func rate(_ store: Store, importance: String) {
        guard let t = card(store), t.importance != importance else { return }
        store.rateMany([t.number], importance: importance)
    }

    func toggleUrgent(_ store: Store) {
        guard let t = card(store) else { return }
        store.rateMany([t.number], urgency: Rating.urgent(t) ? "low" : "high")
    }

    func setDue(_ store: Store, _ day: String?) {
        guard let t = card(store) else { return }
        store.setDue(t.number, day)
    }

    /// The Assign sheet (g). Once it has assigned the task, the next card comes up, as on the web.
    func assign(_ store: Store) {
        guard let t = card(store), !t.isLocal else { return }
        assignWatch = (t.number, t.issue)
        store.assigning = AssignRequest(number: t.number)
    }

    func assignClosed(_ store: Store) {
        guard let w = assignWatch else { return }
        assignWatch = nil
        if w.number == current, let now = store.items[w.number]?.issue, now != w.issue { next(store) }
    }
}

// MARK: - the Store actions Triage adds (queued, conflict-checked and undoable like every other edit)

extension Store {
    /// Archive: close as "not planned". The list label stays, so the task is still found under its list (the web
    /// app's Archived view). ⌘Z reopens it.
    func archive(_ number: Int) {
        guard let t = items[number], t.isOpen, number > 0 else { return }
        undoable(number, "archiving “\(t.title)”") {
            update(number, set: ["state": "CLOSED", "stateReason": "NOT_PLANNED"], what: "Archive “\(t.title)”")
        }
        show("Archived “\(t.title)”", undo: { [weak self] in self?.undo() })
    }

    /// Put tasks in This week (`focus:week`) or take them out, as one undoable step (a swap is both).
    func setFocus(on: [Int], off: [Int]) {
        let adds = on.filter { items[$0].map { $0.isOpen && !Triage.hasFocus($0.issue.labels) } ?? false }
        let removes = off.filter { items[$0].map { Triage.hasFocus($0.issue.labels) } ?? false }
        guard !adds.isEmpty || !removes.isEmpty else { return }
        let first = items[(adds + removes)[0]]?.title ?? "a task"
        let what = adds.isEmpty ? "taking “\(first)” off this week" : removes.isEmpty ? "adding “\(first)” to this week"
            : "swapping “\(first)” into this week"
        undoableMany(removes + adds, what) {
            for n in removes {
                guard let t = items[n] else { continue }
                update(n, remove: t.issue.labels.filter { $0.lowercased() == Triage.focusLabel }, what: "Not this week: “\(t.title)”")
            }
            for n in adds {
                guard let t = items[n] else { continue }
                update(n, add: [Triage.focusLabel], what: "This week: “\(t.title)”")
            }
            return []
        }
        let count = Triage.focusTasks(live).count
        show(adds.isEmpty ? "Taken off this week: “\(first)”" : removes.isEmpty ? "This week: \(count)/\(Triage.focusCap)"
             : "Swapped into this week: “\(first)”", undo: { [weak self] in self?.undo() })
    }

    /// A due date (`due:YYYY-MM-DD`, single-valued), or none.
    func setDue(_ number: Int, _ day: String?) {
        guard let t = items[number], Rating.due(t) != day else { return }
        var r = Rating.Value(t)
        r.due = day
        let d = ratingDiff(from: t, to: r)
        undoable(number, (day == nil ? "clearing the due date of “" : "setting the due date of “") + t.title + "”") {
            update(number, add: d.add, remove: d.remove, what: "Due date: “\(t.title)”")
        }
        show(day.map { "Due \(Waiting.short($0, now: now)): “\(t.title)”" } ?? "No due date: “\(t.title)”",
             undo: { [weak self] in self?.undo() })
    }
}

// MARK: - self-test (part of --selftest): the same rules and numbers as the web app's triage.test.js

@MainActor
enum TriageSelfTest {
    static func failures() -> [String] {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append("triage: " + what) } }
        func near(_ a: Double, _ b: Double) -> Bool { abs(a - b) < 1e-9 }

        let nowDate = Fmt.date("2026-10-12T12:00:00Z")!
        let now = Triage.ms(nowDate)
        let day = Triage.day
        let today = "2026-10-12"
        let cfg = TodoConfig(groups: [GroupMeta(name: "Digest", sortOrder: 5)],
                             listMetadata: [ListMeta(listName: "books", groupName: "Digest", sortOrder: 0),
                                            ListMeta(listName: "tasks", groupName: nil, sortOrder: 0),
                                            ListMeta(listName: "work", groupName: nil, sortOrder: 1)],
                             unratedGroups: ["Digest", "journal"], unratedLists: ["someday-ideas"])
        func issue(_ n: Int, list: String = "tasks", importance: String? = nil, state: String = "OPEN",
                   updated: Double? = nil, labels: [String] = []) -> Issue {
            Issue(number: n, title: "Task \(n)", body: "", state: state, url: "", createdAt: "2026-01-01T00:00:00Z",
                  updatedAt: isoString(Date(timeIntervalSince1970: (updated ?? now) / 1000)),
                  labels: ["list:" + list] + (importance.map { ["importance:" + $0] } ?? []) + labels, agent: nil)
        }
        func task(_ n: Int, list: String = "tasks", importance: String? = nil, state: String = "OPEN",
                  updated: Double? = nil, labels: [String] = []) -> TaskItem {
            TaskItem(issue(n, list: list, importance: importance, state: state, updated: updated, labels: labels), config: cfg)
        }
        func numbers(_ c: [Triage.Candidate]) -> [Int] { c.map { $0.task.number } }

        // snoozes grow 1 day, 3 days, 1 week, 3 weeks, then stay at 3 weeks
        check([0, 1, 2, 3, 4, 9].map { Triage.snoozeDays($0) } == [1, 3, 7, 21, 21, 21], "snooze lengths")
        var snooze: Snooze?
        var lengths: [Double] = []
        for _ in 1...5 {
            snooze = Triage.nextSnooze(snooze, now: now)
            lengths.append((snooze!.until - now) / day)
        }
        check(lengths == [1, 3, 7, 21, 21] && snooze?.count == 5, "snoozes grow: \(lengths)")

        // a snoozed task stays out until its snooze ends
        let once = Triage.nextSnooze(nil, now: now)
        check(once.isActive(now + day - 1) && !once.isActive(now + day), "a snooze ends after a day")
        let two = [task(1), task(2)]
        check(numbers(Triage.pool(two, config: cfg, snoozes: [1: once], shown: [:], now: now, today: today)) == [2], "snoozed is out")
        check(numbers(Triage.pool(two, config: cfg, snoozes: [1: once], shown: [:], now: now + 2 * day, today: today)) == [1, 2],
              "back once the snooze ends")

        // the archive hint comes after the fourth snooze
        check(Snooze(count: 3, until: 0).hint == nil && Snooze(count: 4, until: 0).hint == "Snoozed 4 times. Archive?", "archive hint")

        // weights: unrated first, then importance, less if shown lately, more if old
        check(Triage.weight(task(1), now: now, lastShown: nil) == 6, "unrated weighs 6")
        check(Triage.weight(task(1, importance: "high"), now: now, lastShown: nil) == 3
              && Triage.weight(task(1, importance: "medium"), now: now, lastShown: nil) == 2
              && Triage.weight(task(1, importance: "low"), now: now, lastShown: nil) == 1, "high 3, medium 2, low 1")
        check(Triage.recencyFactor(now - 2 * 3_600_000, now: now) == 0.1 && Triage.recencyFactor(now - 3 * day, now: now) == 0.4
              && Triage.recencyFactor(now - 8 * day, now: now) == 1 && Triage.recencyFactor(nil, now: now) == 1, "recency")
        check(near(Triage.ageFactor(isoString(Date(timeIntervalSince1970: (now - 30 * day) / 1000)), now: now), 2)
              && near(Triage.ageFactor(isoString(Date(timeIntervalSince1970: (now - 400 * day) / 1000)), now: now), 4)
              && Triage.ageFactor("not a date", now: now) == 1 && Triage.ageFactor(isoString(nowDate), now: now) == 1,
              "age: 1 + days/30, at most 4")
        check(near(Triage.weight(task(1, importance: "high", updated: now - 60 * day), now: now, lastShown: now - 3 * day), 3 * 0.4 * 3),
              "weight is the product")

        // the pool: open tasks on GitHub in rated lists, work included
        let mixed = [task(1), task(2, state: "CLOSED"), task(3, list: "books"), task(-4), task(5, list: "work"),
                     task(6, list: "someday-ideas"), task(7, list: "anything", labels: ["group:Journal"])]
        check(numbers(Triage.pool(mixed, config: cfg, snoozes: [:], shown: [:], now: now, today: today)) == [1, 5],
              "pool is open, on GitHub, rated: \(numbers(Triage.pool(mixed, config: cfg, snoozes: [:], shown: [:], now: now, today: today)))")
        // the config's group wins over a stale group: label; a list it doesn't know uses the label
        check(numbers(Triage.pool([task(8, list: "books", labels: ["group:To Do"]), task(9, list: "new-list", labels: ["group:Digest"])],
                                  config: cfg, snoozes: [:], shown: [:], now: now, today: today)).isEmpty, "groups from the config, else the label")
        // waiting on someone: out until the follow-up day; an agent's "came through?" is in
        let waiting = [task(10, labels: ["status:waiting", "follow-up:2026-10-20"]), task(11, labels: ["status:waiting", "follow-up:2026-10-01"]),
                       task(12, labels: ["status:waiting"]), task(13, labels: ["status:waiting-cleared"])]
        check(numbers(Triage.pool(waiting, config: cfg, snoozes: [:], shown: [:], now: now, today: today)) == [11, 13],
              "waiting tasks out until their follow-up day")

        // the draw is weighted and avoids the current card unless it is alone
        let pair = [task(1, importance: "low"), task(2, importance: "high")]          // weights 1 and 3
        func pick(_ r: Double, _ exclude: Int? = nil, _ tasks: [TaskItem]? = nil) -> Int? {
            Triage.pick(tasks ?? pair, config: cfg, snoozes: [:], shown: [:], now: now, today: today, exclude: exclude, random: { r })?.number
        }
        check(pick(0) == 1 && pick(0.24) == 1 && pick(0.26) == 2 && pick(0.999) == 2, "weighted draw")
        check(pick(0.99, 2) == 1, "avoids the card just shown")
        check(pick(0.5, 7, [task(7)]) == 7, "unless it is the only one")
        check(pick(0.5, nil, []) == nil, "nothing to draw")
        var seed: Int64 = 1
        let lcg: () -> Double = { seed = (seed * 16807) % 2_147_483_647; return Double(seed) / 2_147_483_647 }
        let highAndUnrated = [task(1, importance: "high"), task(2)]
        var unrated = 0
        for _ in 0..<3000 where Triage.pick(highAndUnrated, config: cfg, snoozes: [:], shown: [:], now: now, today: today,
                                            exclude: nil, random: lcg)?.number == 2 { unrated += 1 }
        check(unrated > 1800 && unrated < 2200, "an unrated task comes up about twice as often as a high one: \(unrated) of 3000")

        // shown history older than 30 days is forgotten
        check(Triage.pruneShown([1: now - 31 * day, 2: now - day], now: now) == [2: now - day], "shown history pruned")

        // this week is one focus label on open tasks
        let f1 = task(1, labels: ["focus:week"]), f2 = task(2, state: "CLOSED", labels: ["Focus:Week"])
        check(Triage.hasFocus(f1.issue.labels) && Triage.hasFocus(["FOCUS:week"]) && !Triage.hasFocus(["list:tasks"]), "focus label, any case")
        check(Triage.focusTasks([f1, f2, task(3)]).map { $0.number } == [1], "closed tasks don't count for this week")

        check([1, 3, 7, 21].map { Fmt.formatDayCount($0) } == ["1 day", "3 days", "1 week", "3 weeks"], "snooze labels")
        check(Triage.firstStep("Found it.\n\n**First step:** open https://example.org/form (~10 min)") == "open https://example.org/form (~10 min)"
              && Triage.firstStep("- **First step**: call the doctor") == "call the doctor"
              && Triage.firstStep("nothing") == nil && Triage.firstStep(nil) == nil, "first step line")
        check(Triage.cardNotes("## Plan\n**Ring** the `doctor`\n\n\n\nthen book") == "Plan\nRing the doctor\n\nthen book", "card notes cleaned")
        let long = Triage.cardNotes(String(repeating: "word ", count: 120))
        check(long.hasSuffix(" …") && long.count <= 402 && !long.contains("wor …"), "long notes cut at a word: \(long.suffix(12))")
        let unratedCfg = TodoConfig(unratedGroups: [" Journal"], unratedLists: ["not-to-buy"])
        check(Rating.isRatedList("Not-To-Buy", group: nil, config: unratedCfg) == false
              && Rating.isRatedList("tasks", group: " journal ", config: unratedCfg) == false
              && Rating.isRatedList("tasks", group: nil, config: unratedCfg) && Rating.isRatedList("not-to-buy", group: nil, config: nil),
              "rated lists come from the config")
        let decoded = try? decodeConfig(["unratedGroups": ["Archive", 7, ["x"], "Journal"], "unratedLists": "not a list"])
        check(decoded?.unratedGroups == ["Archive", "Journal"] && decoded?.unratedLists == [],
              "unrated names: strings kept, anything else left out: \(String(describing: decoded))")

        // Store actions, offline: archive is a close as not planned, undone by reopening
        let s = Store(channel: FakeDesk(), persist: false)
        s.install(issues: [issue(1), issue(2, importance: "low"), issue(3, labels: ["focus:week"]), issue(4, labels: ["focus:week"]),
                           issue(5, labels: ["focus:week"]), issue(6, labels: ["focus:week"]), issue(7, labels: ["focus:week"]),
                           issue(8, list: "books")], config: cfg)
        s.link = .down("test")
        s.archive(1)
        check(s.items[1]?.isOpen == false && s.items[1]?.issue.labels.contains("list:tasks") == true && !s.isDeleted(s.items[1]!),
              "archived: closed, still in its list")
        check(s.pending.count == 1 && s.pending[0].set["stateReason"] == "NOT_PLANNED"
              && (s.pending[0].request["set"] as? [String: String])?["stateReason"] == "NOT_PLANNED", "the request says not planned")
        s.undo()
        check(s.items[1]?.isOpen == true && s.pending.count == 1 && s.pending[0].set["stateReason"] == nil,
              "undo reopens, and the folded request forgets the reason: \(s.pending.first?.set ?? [:])")
        s.toggleComplete(1)
        check(s.pending.count == 1 && s.pending[0].set == ["state": "CLOSED"], "a later Done closes as completed: \(s.pending[0].set)")
        s.undo()

        // this week: on, off, and a swap, each one undo
        s.setFocus(on: [2], off: [])
        check(s.items[2]?.issue.labels.contains(Triage.focusLabel) == true, "this week on")
        s.undo()
        check(s.items[2].map { Triage.hasFocus($0.issue.labels) } == false, "undo this week")
        check(Triage.focusTasks(s.live).count == Triage.focusCap, "five this week already")
        let undos = s.undoStack.count
        s.setFocus(on: [2], off: [3])
        check(s.items[2].map { Triage.hasFocus($0.issue.labels) } == true && s.items[3].map { Triage.hasFocus($0.issue.labels) } == false
              && s.undoStack.count == undos + 1, "swap one out, as one step")
        s.undo()
        check(s.items[2].map { Triage.hasFocus($0.issue.labels) } == false && s.items[3].map { Triage.hasFocus($0.issue.labels) } == true,
              "undo the swap")

        // due dates: one at a time, and none
        s.setDue(2, "2026-11-03")
        s.setDue(2, "2026-11-10")
        check(s.items[2]?.issue.labels.filter { $0.hasPrefix("due:") } == ["due:2026-11-10"], "one due date")
        s.setDue(2, nil)
        check(s.items[2].flatMap { Rating.due($0) } == nil, "no due date")
        s.undo()
        check(s.items[2].flatMap { Rating.due($0) } == "2026-11-10", "undo clearing it")

        // the deck: a fixed draw, never the same card twice running, snoozes and archive move on
        let deck = TriageDeck(prefs: TriagePrefs(defaults: nil), random: { 0 }, clock: { nowDate })
        deck.next(s)
        check(deck.current == 1 && s.selection == 1 && deck.prefs.shown[1] == now, "first card, shown in the panel and recorded")
        deck.next(s)
        check(deck.current == 2 && s.selection == 2, "next card")
        check(deck.poolCount(s) == 7, "pool count: \(deck.poolCount(s))")
        deck.snooze(s)
        check(deck.prefs.snoozes[2]?.count == 1 && deck.current != 2 && deck.poolCount(s) == 6, "snooze: out of the pool, next card")
        let archived = deck.current!
        deck.archive(s)
        check(s.items[archived]?.isOpen == false && deck.current != archived && deck.card(s) != nil, "archive moves on first")
        deck.rate(s, importance: "high")
        let rated = s.undoStack.count
        deck.rate(s, importance: "high")
        check(s.items[deck.current!]?.importance == "high" && Rating.urgent(s.items[deck.current!]!) == false
              && s.items[deck.current!]?.issue.labels.contains("urgency:low") == true && s.undoStack.count == rated,
              "rated high (not urgent, said so); the same again changes nothing")
        deck.toggleUrgent(s)
        check(s.items[deck.current!].map { Rating.urgent($0) } == true, "urgent")
        let shownCard = deck.current!
        s.toggleComplete(shownCard)                       // closed from its panel: the page draws again
        check(deck.card(s) == nil, "a closed card is no card")
        deck.ensureCard(s)
        check(deck.current != shownCard && deck.card(s) != nil, "ensureCard draws a new one")

        // this Mac's memory survives a restart and bad data
        let suite = "org.agentkit.todo.triage-selftest"
        if let d = UserDefaults(suiteName: suite) {
            d.removePersistentDomain(forName: suite)
            let p = TriagePrefs(defaults: d)
            p.snooze(42, now: now)
            p.recordShown(42, now: now)
            let again = TriagePrefs(defaults: d)
            check(again.snoozes[42] == Snooze(count: 1, until: now + day) && again.shown[42] == now, "prefs saved and read back")
            d.set(["x": "junk", "43": ["count": "two"]], forKey: TriagePrefs.snoozesKey)
            d.set("not a dictionary", forKey: TriagePrefs.shownKey)
            let bad = TriagePrefs(defaults: d)
            check(bad.snoozes.isEmpty && bad.shown.isEmpty, "bad prefs are ignored")
            d.removePersistentDomain(forName: suite)
        } else {
            check(false, "a scratch defaults suite")
        }

        // keys: Triage's own, only on its page and only without modifiers
        let ks = Store(channel: FakeDesk(), persist: false)
        ks.install(issues: [issue(1), issue(2), issue(3)], config: cfg)
        ks.link = .down("test")
        let saved = TriageDeck.shared
        let kd = TriageDeck(prefs: TriagePrefs(defaults: nil), random: { 0 }, clock: { nowDate })
        TriageDeck.shared = kd
        func key(_ chars: String, _ code: Int, _ mods: NSEvent.ModifierFlags = []) -> Bool {
            guard let e = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: mods, timestamp: 0, windowNumber: 0,
                                           context: nil, characters: chars, charactersIgnoringModifiers: chars, isARepeat: false,
                                           keyCode: UInt16(code)) else { return false }
            return TriageKeys.handle(e, store: ks)
        }
        kd.next(ks)
        check(kd.current == 1 && !key("a", kVK_ANSI_A) && ks.items[1]?.isOpen == true, "keys do nothing off the Triage page")
        ks.route = .triage
        check(key("\u{F703}", kVK_RightArrow) && kd.current == 2, "→ shows the next card")
        check(key(" ", kVK_Space) && kd.current == 1 && key("\u{F703}", kVK_RightArrow) && kd.current == 2, "space does too")
        check(key("1", kVK_ANSI_1) && ks.items[2]?.importance == "high", "1 rates high")
        check(key("l", kVK_ANSI_L) && ks.items[2]?.importance == "low", "l rates low")
        check(key("u", kVK_ANSI_U) && ks.items[2].map { Rating.urgent($0) } == true, "u makes it urgent")
        check(key("w", kVK_ANSI_W) && ks.items[2].map { Triage.hasFocus($0.issue.labels) } == true, "w puts it in this week")
        check(!key("a", kVK_ANSI_A, .command) && !key("a", kVK_ANSI_A, .shift) && ks.items[2]?.isOpen == true, "⌘A and ⇧A are not archive")
        check(!key("q", kVK_ANSI_Q) && !key("\r", kVK_Return), "other keys are the app's")
        check(key("a", kVK_ANSI_A) && ks.items[2]?.isOpen == false && kd.current != 2 && kd.card(ks) != nil, "a archives and moves on")
        let beforeDone = kd.current!
        check(key("d", kVK_ANSI_D) && ks.items[beforeDone]?.isOpen == false, "d completes")
        TriageDeck.shared = saved

        let r1 = SeededRandom(7), r2 = SeededRandom(7)
        let a = (0..<5).map { _ in r1.next() }, b = (0..<5).map { _ in r2.next() }
        check(a == b && a.allSatisfy { $0 >= 0 && $0 < 1 } && Set(a).count == 5, "seeded draw repeats")
        return fails
    }
}
