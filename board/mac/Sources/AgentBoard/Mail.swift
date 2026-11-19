import AppKit
import Carbon
import SwiftUI
import WebKit

// The Mail tab: mail between the owner and their agents, read from the mailroom on the desktop, which keeps the
// messages and the owner's flags (read, archived, snoozed) as a mail server would (`agent-mail mailbox`, through the
// board's mail_* actions). The tab lists a box or a search, shows one thread as the HTML document the desktop makes of
// it (MailHTML.swift), and sends the owner's marks and replies back, with files. Only the owner's unsent replies are
// kept on the Mac (MailDrafts.swift).

// MARK: - the desktop's answers

/// The snapshot's mailbox section, for the tab's label. Absent while the desktop's mailroom has no mailbox.
struct MailboxSummary: Decodable, Equatable {
    var unread: Int
    var inbox: Int
}

enum MailBox: String, CaseIterable, Identifiable {
    case inbox, snoozed, archived, all
    var id: String { rawValue }
    var label: String { rawValue.capitalized }
}

/// One thread, as mail_list lists it and mail_show heads it.
struct MailboxThread: Decodable, Identifiable, Equatable {
    var id: String
    var subject: String
    var agents: [String]          // who wrote in it, in order of their first message
    var snippet: String           // the start of its newest message
    var last: Double              // its newest message, or when it woke from a snooze: the list's order
    var count: Int                // messages, both ways
    var unread: Bool
    var box: String               // inbox, snoozed or archived
    var snoozeUntil: Double?
    var newest: Int               // its newest agent message's id: how far a read, archive or snooze reaches
    var attachments: Bool
}

struct MailCounts: Decodable, Equatable {
    var inbox: Int
    var unread: Int
    var snoozed: Int
}

/// mail_list's answer. A thread that does not decode is left out, and why is kept in `dropped`.
struct MailList: Decodable {
    var threads: [MailboxThread]
    var counts: MailCounts?
    var dropped: [String]

    enum CodingKeys: String, CodingKey { case threads, counts }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard c.contains(.threads) else {
            throw DecodingError.keyNotFound(CodingKeys.threads, .init(codingPath: c.codingPath, debugDescription: "no threads"))
        }
        var why: [String] = []
        threads = c.lossyArray(MailboxThread.self, forKey: .threads, failures: &why)
        counts = c.lossyValue(MailCounts.self, forKey: .counts, failures: &why)
        dropped = why
    }
}

/// One message of a thread, as far as the tab uses it beside the document: who wrote it, and how the owner's replies
/// are getting on.
struct MailboxMessage: Decodable, Identifiable, Equatable {
    var key: String               // "s<id>" an agent's mail, "i<id>" the owner's
    var from: String
    var mine: Bool
    var ts: Double
    var unread: Bool
    var status: String?           // the owner's: sending, delivered, queued, with the orchestrator, or held: <why>
    var widgets: [MailWidget]?    // an agent's mail: its options and plots, filled by MailShow.decode; none when missing
    var id: String { key }

    enum CodingKeys: String, CodingKey { case key, from, mine, ts, unread, status }   // widgets: MailShow.decode
}

/// mail_show's answer: the thread, its messages, and the whole thread as one sanitized HTML page. A message that does
/// not decode is left out of `messages` (the document still has it), and why is kept in `dropped`. Read it with
/// MailShow.decode, which adds the messages' widgets, and in `widgetProblems` why some could not be.
struct MailShow: Decodable {
    var thread: MailboxThread
    var messages: [MailboxMessage]
    var document: String
    var dropped: [String]
    /// For each message whose widgets did not decode, "KEY: why" (MailShow.decode); those show as plain text.
    var widgetProblems: [String] = []
    /// Its messages' widgets, in order.
    var widgets: [MailWidget] { messages.flatMap { $0.widgets ?? [] } }

    /// mail_show's answer `obj` (or `err`) as the tab reads it: Actions.decodeAnswer for the thread and messages, and
    /// each agent message's widgets decoded apart (MailWidgets.decode), since JSON.decoder would change a spec's keys.
    /// Widgets that do not decode leave their message plain, and `widgetProblems` says why.
    static func decode(_ obj: [String: Any]?, _ err: String?) -> (MailShow?, String?) {
        let (got, why) = Actions.decodeAnswer(obj, err, key: nil, as: MailShow.self)
        guard var show = got else { return (nil, why) }
        let (widgets, problems) = MailWidgets.decode(obj)
        for i in show.messages.indices { show.messages[i].widgets = widgets[show.messages[i].key] }
        show.widgetProblems = problems
        return (show, nil)
    }

    enum CodingKeys: String, CodingKey { case thread, messages, document }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        thread = try c.decode(MailboxThread.self, forKey: .thread)
        document = try c.decode(String.self, forKey: .document)
        var why: [String] = []
        messages = c.lossyArray(MailboxMessage.self, forKey: .messages, failures: &why)
        dropped = why
    }
}

/// mail_part's answer: one attachment of a stored message.
struct MailPart: Decodable {
    var name: String
    var data: String              // base64
}

/// mail_reply's answer: the key of the owner's reply, now a message of the thread.
struct MailReply: Decodable {
    var key: String
}

/// mail_sync's answer: the threads it archived and marked read in the tab, and the mails in Gmail.
struct MailSync: Decodable, Equatable {
    var archived: Int
    var read: Int
    var gmailArchived: Int
    var gmailRead: Int

    /// "Synced with Gmail: here 5 archived, 6 read; in Gmail 2 mails archived", the counts that are not 0.
    var line: String {
        func part(_ place: String, _ counts: [(Int, String)]) -> String? {
            let said = counts.filter { $0.0 > 0 }.map { "\($0.0) \($0.1)" }
            return said.isEmpty ? nil : place + " " + said.joined(separator: ", ")
        }
        let parts = [part("here", [(archived, "archived"), (read, "read")]),
                     part("in Gmail", [(gmailArchived, gmailArchived == 1 ? "mail archived" : "mails archived"),
                                       (gmailRead, "marked read")])].compactMap { $0 }
        return "Synced with Gmail: " + (parts.isEmpty ? "already in step" : parts.joined(separator: "; "))
    }
}

enum MailOp: String {
    case read, unread, archive, inbox, snooze

    /// What a refusal says.
    var failure: String {
        switch self {
        case .read: return "Not marked read"
        case .unread: return "Not marked unread"
        case .archive: return "Not archived"
        case .inbox: return "Not moved to the inbox"
        case .snooze: return "Not snoozed"
        }
    }
}

// MARK: - rules

/// What the tab shows before the desktop answers, and when it asks again.
enum MailRules {
    /// A thread as it will be after `op`.
    static func changed(_ t: MailboxThread, _ op: MailOp, until: Double?) -> MailboxThread {
        var t = t
        switch op {
        case .read: t.unread = false
        case .unread: t.unread = true
        case .archive: t.box = MailBox.archived.rawValue; t.snoozeUntil = nil
        case .inbox: t.box = MailBox.inbox.rawValue; t.snoozeUntil = nil
        case .snooze: t.box = MailBox.snoozed.rawValue; t.snoozeUntil = until
        }
        return t
    }

    /// The list after `op` on the thread `id`: changed, and gone when it no longer belongs to the box listed. `box` nil
    /// is a search, which lists every box, as All does.
    static func applying(_ op: MailOp, to id: String, in list: [MailboxThread], box: MailBox?, until: Double?) -> [MailboxThread] {
        list.compactMap { t in
            guard t.id == id else { return t }
            let n = changed(t, op, until: until)
            guard let box = box, box != .all else { return n }
            return n.box == box.rawValue ? n : nil
        }
    }

    /// A reply of the owner's still on its way: the open thread is read again on each poll until it has arrived.
    static func pending(_ m: MailboxMessage) -> Bool { m.mine && (m.status == "sending" || m.status == "queued") }

    /// Whether the list's row for a thread is newer than an answer about it: a message more, or woken from a snooze.
    static func behind(_ shown: MailShow, row: MailboxThread?) -> Bool {
        guard let r = row else { return false }
        return r.last != shown.thread.last || r.count != shown.thread.count
    }

    /// Whether the open thread must be read again: the list says it has changed, or a reply of the owner's is on its way.
    static func stale(_ shown: MailShow, row: MailboxThread?) -> Bool {
        behind(shown, row: row) || shown.messages.contains(where: pending)
    }

    /// The message a thread opens at: in an unread thread its first unread message, else its newest.
    static func focus(_ messages: [MailboxMessage], unread: Bool) -> String? {
        (unread ? messages.first(where: { $0.unread })?.key : nil) ?? messages.last?.key
    }

    /// The message `by` places on from `key` in the thread (-1 the older one, 1 the newer), or nil past either end.
    static func step(from key: String?, by: Int, in messages: [MailboxMessage]) -> String? {
        guard let i = messages.firstIndex(where: { $0.key == key }), messages.indices.contains(i + by) else { return nil }
        return messages[i + by].key
    }
}

/// Threads read from the desktop, kept so that one opened again, or one fetched ahead, shows at once. Once their
/// documents pass `budget` bytes together, the least recently used go first. An entry is only as good as the list
/// row it is checked against (MailRules.stale): the model shows it while the list vouches for it.
struct MailCache {
    let budget: Int
    private(set) var entries: [String: MailShow] = [:]
    private var asked: [String: Int] = [:]   // the number of the request each entry answered
    private var order: [String] = []         // least recently used first
    private var bytes = 0

    init(budget: Int) { self.budget = budget }

    /// The thread kept for `id`, now the most recently used.
    mutating func take(_ id: String) -> MailShow? {
        guard let s = entries[id] else { return nil }
        touch(id)
        return s
    }

    /// Keep `s`, the answer to request number `n`, in place of what was kept for its thread, unless that answered a
    /// later request (answers can come out of order). Then drop the least recently used past the budget, never `s`.
    mutating func put(_ s: MailShow, asked n: Int) {
        let id = s.thread.id
        if let had = asked[id], had > n { return }
        if let old = entries[id] { bytes -= old.document.utf8.count }
        entries[id] = s
        asked[id] = n
        bytes += s.document.utf8.count
        touch(id)
        while bytes > budget, let oldest = order.first, oldest != id {
            order.removeFirst()
            asked[oldest] = nil
            if let gone = entries.removeValue(forKey: oldest) { bytes -= gone.document.utf8.count }
        }
    }

    private mutating func touch(_ id: String) {
        order.removeAll { $0 == id }
        order.append(id)
    }
}

/// What a key does in the Mail tab (MailKeys.command).
enum MailKey: Equatable {
    case thread(Int)          // the thread below (1) or above (-1) in the list
    case message(Int)         // the newer (1) or older (-1) mail in the open thread
    case page(Int)            // the document a screen down (1) or up (-1)
    case archive              // archive the open thread, or move it back to the inbox
    case reply                // open the reply box, with the keyboard in it
    case paste                // files or an image on the clipboard into the reply
    case release              // stop typing: the arrows go back to the list
}

enum MailKeys {
    static let up = String(UnicodeScalar(UInt16(NSUpArrowFunctionKey))!)
    static let down = String(UnicodeScalar(UInt16(NSDownArrowFunctionKey))!)
    static let left = String(UnicodeScalar(UInt16(NSLeftArrowFunctionKey))!)
    static let right = String(UnicodeScalar(UInt16(NSRightArrowFunctionKey))!)

    /// A key press in the Mail tab: ↓ or j, ↑ or k the thread below or above; → and ← the newer and older mail in the
    /// thread; Space and ⇧Space a screen down and up; e archive; r reply; ⌘V files or an image into the reply; and
    /// while the owner types (`typing`, a text box has the keyboard), only Esc, which gives the keyboard back (the box
    /// takes its own ⌘V). nil: the key is not the tab's (no other ⌘, nor ⌥ or ⌃).
    static func command(_ chars: String, keyCode: UInt16, flags: NSEvent.ModifierFlags, typing: Bool) -> MailKey? {
        let mods = flags.intersection([.command, .option, .control, .shift])
        if typing { return keyCode == UInt16(kVK_Escape) && mods.isEmpty ? .release : nil }
        if mods == .shift { return chars == " " ? .page(-1) : nil }
        if mods == .command { return keyCode == UInt16(kVK_ANSI_V) ? .paste : nil }
        guard mods.isEmpty else { return nil }
        switch chars {
        case down, "j": return .thread(1)
        case up, "k": return .thread(-1)
        case right: return .message(1)
        case left: return .message(-1)
        case " ": return .page(1)
        case "e": return .archive
        case "r": return .reply
        default: return nil
        }
    }
}

/// A request to scroll the document a screen down (`by` 1) or up (-1); `n` tells one request from the next.
struct MailPage: Equatable {
    var n: Int
    var by: Int
}

struct SnoozeChoice: Equatable {
    var label: String
    var date: Date
}

enum Snooze {
    /// Gmail's choices: later today (three hours on, to the nearest hour, while that is still today), tomorrow 08:00,
    /// this weekend (Saturday 08:00, offered Monday to Friday) and next week (Monday 08:00 of the coming week). A choice
    /// at the same moment as an earlier one is left out.
    static func presets(now: Date, calendar cal: Calendar = .current) -> [SnoozeChoice] {
        var out: [SnoozeChoice] = []
        func add(_ label: String, _ date: Date?) {
            if let d = date, d > now, !out.contains(where: { $0.date == d }) { out.append(SnoozeChoice(label: label, date: d)) }
        }
        let ahead = now.addingTimeInterval(3 * 3600)
        if let hour = cal.dateInterval(of: .hour, for: ahead)?.start {
            let later = ahead.timeIntervalSince(hour) >= 1800 ? cal.date(byAdding: .hour, value: 1, to: hour) : hour
            if let l = later, cal.isDate(l, inSameDayAs: now) { add("Later today", l) }
        }
        let tomorrow = cal.date(byAdding: .day, value: 1, to: cal.startOfDay(for: now))
        add("Tomorrow", morning(after: now, calendar: cal))
        let weekday = cal.component(.weekday, from: now)              // 1 Sunday … 7 Saturday
        if weekday != 1 && weekday != 7 {
            add("This weekend", cal.nextDate(after: now, matching: DateComponents(hour: 8, minute: 0, weekday: 7),
                                             matchingPolicy: .nextTime))
        }
        if let t = tomorrow {
            add("Next week", cal.nextDate(after: t.addingTimeInterval(-1), matching: DateComponents(hour: 8, minute: 0, weekday: 2),
                                          matchingPolicy: .nextTime))
        }
        return out
    }

    /// Tomorrow 08:00: the preset, and where Pick date & time starts.
    static func morning(after now: Date, calendar cal: Calendar = .current) -> Date? {
        cal.date(byAdding: .day, value: 1, to: cal.startOfDay(for: now))
            .flatMap { cal.date(bySettingHour: 8, minute: 0, second: 0, of: $0) }
    }
}

enum MailFmt {
    /// The list's time: "14:02" today, "Yesterday", a weekday within the last week, "13 Nov" this year, "13 Nov 2025"
    /// before.
    static func listTime(_ ts: Double, now: Date, calendar cal: Calendar = .current) -> String {
        let date = Date(timeIntervalSince1970: ts)
        if cal.isDate(date, inSameDayAs: now) { return formatter("HH:mm", cal).string(from: date) }
        if date < now {
            if let y = cal.date(byAdding: .day, value: -1, to: now), cal.isDate(date, inSameDayAs: y) { return "Yesterday" }
            if now.timeIntervalSince(date) < 6 * 86400 { return formatter("EEE", cal).string(from: date) }
        }
        let sameYear = cal.component(.year, from: date) == cal.component(.year, from: now)
        return formatter(sameYear ? "d MMM" : "d MMM yyyy", cal).string(from: date)
    }

    /// A time ahead, such as a snooze's: "17:00" today, "Sat 08:00" within six days, else "Mon 30 Nov 08:00".
    static func at(_ ts: Double, now: Date, calendar cal: Calendar = .current) -> String {
        let date = Date(timeIntervalSince1970: ts)
        if cal.isDate(date, inSameDayAs: now) { return formatter("HH:mm", cal).string(from: date) }
        let pattern = date.timeIntervalSince(now) < 6 * 86400 ? "EEE HH:mm" : "EEE d MMM HH:mm"
        return formatter(pattern, cal).string(from: date)
    }

    static func formatter(_ pattern: String, _ cal: Calendar) -> DateFormatter {
        let f = DateFormatter()
        f.calendar = cal
        f.timeZone = cal.timeZone
        f.locale = cal.locale ?? .current
        f.dateFormat = pattern
        return f
    }

    /// The owner's reply's state in the colour it is shown in: held in red, on its way in amber, the rest plain.
    static func statusColor(_ status: String) -> Color {
        if status.hasPrefix("held") { return AgentState.blocked.color }
        return status == "sending" || status == "queued" ? Palette.attention : .secondary
    }
}

// MARK: - the model

/// The Mail tab's state, kept while the tab is away so a draft or the open thread survives a look at the board. It
/// polls the list every 15 s while the tab and the window are on screen, and reads the open thread again only when the
/// list shows that it changed or a reply of the owner's in it is still on its way. Threads it has read are kept
/// (MailCache), and the `ahead` threads on each side of the open one (or the top ones, with none open) are read
/// before they are asked for, so stepping through the list shows each at once.
@MainActor
final class MailModel: ObservableObject {
    /// How a request reaches the desktop: the board's actions (`live`), or saved answers in --render-mail and
    /// --selftest-mail.
    typealias Call = (_ request: [String: Any], _ done: @escaping @MainActor ([String: Any]?, String?) -> Void) -> Void
    typealias Shown = @MainActor (MailShow?, String?) -> Void
    /// How a file too big for a reply reaches the desktop: its path there, or why not (Actions.upload in the app).
    typealias Upload = (_ file: URL, _ done: @escaping @MainActor (String?, String?) -> Void) -> Void

    static let live: Call = { request, done in
        Actions.runJSON(request) { obj, err in MainActor.assumeIsolated { done(obj, err) } }   // runJSON answers on main
    }
    static let liveUpload: Upload = { file, done in
        Actions.upload(file) { path, err in MainActor.assumeIsolated { done(path, err) } }      // answers on main
    }

    /// The mailroom refuses a reply longer than this (characters, as Python counts them: Unicode scalars).
    static let maxReply = 20000
    /// The files one reply carries, at most: how many, and their bytes together (the mailroom's MAX_ANSWER_FILES and
    /// MAX_ANSWER_FILE_BYTES, and the board's MAX_FILES and MAX_FILE_BYTES).
    static let maxFiles = 20
    static let maxFileBytes = 25 << 20
    /// How many threads a list asks for, newest first (the mailroom's default; it allows up to 500).
    static let listLimit = 100
    /// The app's: threads read ahead on each side of the open one, and the bytes of documents kept (a long thread
    /// with plots is up to 10 MB).
    static let appAhead = 2
    static let appCacheBytes = 64 << 20

    @Published private(set) var box = MailBox.inbox
    @Published private(set) var search = ""                    // the words searched for; "" lists the box
    @Published private(set) var threads: [MailboxThread]?      // nil until the list has come
    @Published private(set) var counts: MailCounts?
    @Published private(set) var listError: String?
    @Published private(set) var selected: String?               // the open thread
    @Published private(set) var shown: MailShow?                // its answer; while another loads, the one before
    @Published private(set) var current: String?                // the message being read: outlined, and ← → step from it
    @Published private(set) var page: MailPage?                 // the last Space or ⇧Space
    @Published private(set) var threadError: String?
    @Published var notice: String?                              // the last thing that failed: a mark, a link, a file
    @Published private(set) var syncing = false                 // mail_sync is running
    @Published private(set) var synced: String?                 // what the last sync changed, in a line
    @Published private(set) var now = Date()                    // the list's times count from it; set with each list
    private var revealing: String?                              // a thread opened from the board: list its box once it loads

    /// The owner's unsent replies, per thread; only its reply box watches each, so typing redraws nothing else.
    let drafts: MailDrafts

    private let call: Call
    private let upload: Upload
    private let ahead: Int
    private var cache: MailCache
    private var fetching: [String: (n: Int, waiting: [Shown])] = [:]   // mail_show asked and not answered, per thread
    private var timer: Timer?
    private var keyMonitor: Any?
    private var asked = 0            // requests numbered as they go
    private var listAsked = 0        // the newest list request: an older one's answer is out of date
    private var changedAt = 0        // the newest change (a mark, a sync): a list asked before it is out of date
    private var changing = 0         // changes not answered yet: a list answered meanwhile may predate them

    /// Whether the tab is on screen (MailView) and whether the board's window is (BoardModel.windowVisible). The list
    /// is polled while both are; the tab's keys (MailKeys) work while the tab is.
    var tabShown = false { didSet { if tabShown != oldValue { update() } } }
    var windowVisible = true { didSet { if windowVisible != oldValue { update() } } }

    /// `ahead` threads on each side of the open one are read before they are asked for (0: none); `cacheBytes` of
    /// documents are kept; drafts are kept in `draftsFolder` (MailDrafts).
    init(call: @escaping Call, upload: @escaping Upload, ahead: Int, cacheBytes: Int, draftsFolder: URL) {
        self.call = call
        self.upload = upload
        self.ahead = ahead
        self.cache = MailCache(budget: cacheBytes)
        self.drafts = MailDrafts(folder: draftsFolder)
        notice = drafts.loadProblem
        drafts.problem = { [weak self] in self?.notice = $0 }
    }

    /// The app's: requests go to the desktop as board actions; drafts are kept in `draftsFolder`.
    convenience init(draftsFolder: URL) {
        self.init(call: MailModel.live, upload: MailModel.liveUpload, ahead: MailModel.appAhead,
                  cacheBytes: MailModel.appCacheBytes, draftsFolder: draftsFolder)
    }

    /// The box listed, or nil while searching (a search covers every box).
    var listing: MailBox? { search.isEmpty ? box : nil }

    /// The open thread's row: the list's, which polls keep fresh, else the one in its answer.
    func current(_ id: String) -> MailboxThread? {
        threads?.first { $0.id == id } ?? (shown?.thread.id == id ? shown?.thread : nil)
    }

    func draft(for id: String) -> Draft { drafts.draft(id) }

    private func update() {
        let on = tabShown && windowVisible
        if on && timer == nil {
            let t = Timer(timeInterval: 15, repeats: true) { [weak self] _ in Task { @MainActor in self?.refresh() } }
            t.tolerance = 2
            RunLoop.main.add(t, forMode: .default)
            timer = t
            refresh()
        } else if !on, let t = timer {
            t.invalidate()
            timer = nil
        }
        if tabShown && keyMonitor == nil {
            keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
                guard let self = self, let w = e.window, w === Dialogs.window, w.attachedSheet == nil,
                      let key = MailKeys.command(e.charactersIgnoringModifiers ?? "", keyCode: e.keyCode,
                                                 flags: e.modifierFlags, typing: w.firstResponder is NSText) else { return e }
                if key == .release { w.makeFirstResponder(nil) } else { self.perform(key) }
                return nil
            }
        } else if !tabShown, let m = keyMonitor {
            NSEvent.removeMonitor(m)
            keyMonitor = nil
        }
    }

    // MARK: reading

    /// The list again (each poll, ⌘R, and after every change).
    func refresh() {
        asked += 1
        let n = asked
        listAsked = n
        var req: [String: Any] = ["action": "mail_list", "box": (listing ?? .all).rawValue, "limit": MailModel.listLimit]
        if !search.isEmpty { req["query"] = search }
        call(req) { [weak self] obj, err in
            guard let self = self, n == self.listAsked, n > self.changedAt, self.changing == 0 else { return }
            self.now = Date()
            let (got, why) = Actions.decodeAnswer(obj, err, key: nil, as: MailList.self)
            guard let list = got else { self.listError = why; return }
            self.threads = list.threads
            self.counts = list.counts
            self.listError = list.dropped.first.map {
                "\(list.dropped.count) part\(list.dropped.count == 1 ? "" : "s") of the list not understood: \($0)"
            }
            if let s = self.shown, s.thread.id == self.selected,
               MailRules.stale(s, row: list.threads.first { $0.id == s.thread.id }) {
                self.load(s.thread.id, focus: nil, fresh: true)
            }
            self.readAhead()
        }
    }

    func show(_ b: MailBox) {
        guard b != box || !search.isEmpty else { return }
        box = b
        search = ""
        threads = nil
        synced = nil
        refresh()
    }

    /// Search every box for threads with all of `words` (in the subject, an agent's name or a message); "" goes back to
    /// the box.
    func find(_ words: String) {
        let w = words.trimmingCharacters(in: .whitespacesAndNewlines)
        guard w != search else { return }
        search = w
        threads = nil
        synced = nil
        refresh()
    }

    /// Open a thread: at once when it was read before and the list shows it unchanged since, else once the desktop
    /// answers. The one open already is read again only if it failed to load.
    func open(_ id: String) {
        guard id != selected || shown?.thread.id != id else { return }
        if let old = selected, old != id { draft(for: old).leave() }
        selected = id
        threadError = nil
        if let row = threads?.first(where: { $0.id == id }), let kept = cache.take(id), !MailRules.stale(kept, row: row) {
            var s = kept
            s.thread = row                      // the list's flags are the newer
            display(s, focus: nil)
        } else {
            load(id, focus: nil, fresh: false)
        }
        readAhead()
    }

    /// Open a thread from the board (an agent's latest mail) and list the box it is in, so it shows in the list.
    func reveal(_ id: String) {
        revealing = id
        if selected == id, let s = shown, s.thread.id == id { listBox(of: s.thread) } else { open(id) }
    }

    private func listBox(of t: MailboxThread) {
        revealing = nil
        if let b = MailBox(rawValue: t.box), listing != b { show(b) }
    }

    func close() {
        if let old = selected { draft(for: old).leave() }
        selected = nil
        shown = nil
        current = nil
        threadError = nil
    }

    /// Read a thread from the desktop and show it if it is still the one open, at the message `focus` (nil: as
    /// display decides). `fresh`: ask anew even when a request for it is on its way already, which may predate a
    /// change; else wait for that one.
    /// A request already on its way that it waited for may have been asked before the list showed the thread changed;
    /// then it shows that answer and asks once more, afresh.
    private func load(_ id: String, focus key: String?, fresh: Bool) {
        fetch(id, fresh: fresh) { [weak self] got, why in
            guard let self = self, self.selected == id else { return }
            guard let s = got else { self.threadError = why; return }
            self.display(s, focus: key)
            if !fresh, MailRules.behind(s, row: self.threads?.first { $0.id == id }) { self.load(id, focus: nil, fresh: true) }
        }
    }

    /// Show `s`, the open thread, at the message `key`; with none given, the same thread read again stays at the
    /// message being read while it is there and nothing in it is unread, and otherwise it goes to MailRules.focus. An
    /// unread thread is then marked read up to its newest message, the one now on screen.
    private func display(_ s: MailShow, focus key: String?) {
        threadError = s.dropped.first.map {
            "\(s.dropped.count) message\(s.dropped.count == 1 ? "" : "s") not understood (the document still has them): \($0)"
        }
        if let first = s.widgetProblems.first {
            let n = s.widgetProblems.count
            notice = "Options or plots in \(n) mail\(n == 1 ? "" : "s") could not be drawn, shown as plain text: \(first)"
        }
        let stay = shown?.thread.id == s.thread.id && !s.thread.unread && s.messages.contains { $0.key == current }
        current = key ?? (stay ? current : MailRules.focus(s.messages, unread: s.thread.unread))
        shown = s
        if revealing == s.thread.id { listBox(of: s.thread) }
        if s.thread.unread { mark(s.thread.id, .read) }
    }

    /// mail_show for `id`, kept (MailCache) when it comes. A request on its way for the thread already is waited for,
    /// unless `fresh`; then nobody waits for that one any more (only load asks afresh, and its own `done` shows the
    /// thread), and its answer is kept only if nothing newer is.
    private func fetch(_ id: String, fresh: Bool, _ done: @escaping Shown) {
        if !fresh, var f = fetching[id] {
            f.waiting.append(done)
            fetching[id] = f
            return
        }
        asked += 1
        let n = asked
        fetching[id] = (n, [done])
        call(["action": "mail_show", "thread": id]) { [weak self] obj, err in
            guard let self = self else { return }
            let (got, why) = MailShow.decode(obj, err)
            if let s = got { self.cache.put(s, asked: n) }
            guard self.fetching[id]?.n == n, let f = self.fetching.removeValue(forKey: id) else { return }
            f.waiting.forEach { $0(got, why) }
        }
    }

    /// Read the threads within `ahead` places of the open one in the list (of the first, with none open) that are
    /// neither kept as the list has them nor on their way.
    private func readAhead() {
        guard ahead > 0, let list = threads, !list.isEmpty else { return }
        let i = selected.flatMap { id in list.firstIndex { $0.id == id } } ?? 0
        for t in list[max(0, i - ahead)...min(list.count - 1, i + ahead)] where fetching[t.id] == nil {
            if let kept = cache.entries[t.id], !MailRules.stale(kept, row: t) { continue }
            fetch(t.id, fresh: false) { _, _ in }
        }
    }

    // MARK: changing

    /// Change a thread's flags on the desktop (mail_mark), showing the change at once and the list as the desktop has
    /// it once it answers. read, archive and snooze reach up to the newest message the owner has been shown: the open
    /// thread's, else the list's.
    func mark(_ id: String, _ op: MailOp, until: Double? = nil) {
        guard let t = (shown?.thread.id == id ? shown?.thread : nil) ?? threads?.first(where: { $0.id == id }) else { return }
        asked += 1
        changedAt = asked
        changing += 1
        threads = threads.map { MailRules.applying(op, to: id, in: $0, box: listing, until: until) }
        if shown?.thread.id == id { shown?.thread = MailRules.changed(t, op, until: until) }
        var req: [String: Any] = ["action": "mail_mark", "threads": [id], "op": op.rawValue]
        if op == .read || op == .archive || op == .snooze { req["upto"] = t.newest }
        if let u = until { req["until"] = u }
        call(req) { [weak self] obj, err in
            guard let self = self else { return }
            self.changing -= 1
            if let why = Actions.decodeAnswer(obj, err, key: "threads", as: [MailboxThread].self).1 {
                self.notice = "\(op.failure): \(why)"
            }
            self.refresh()
        }
    }

    /// Bring the mailbox and the owner's Gmail into step (mail_sync): what is archived or read in either becomes so in
    /// both; nothing comes back to the inbox or goes unread, and snoozed threads stay. Then the list again.
    func sync() {
        guard !syncing else { return }
        syncing = true
        synced = nil
        asked += 1
        changedAt = asked
        changing += 1
        call(["action": "mail_sync"]) { [weak self] obj, err in
            guard let self = self else { return }
            self.syncing = false
            self.changing -= 1
            let (got, why) = Actions.decodeAnswer(obj, err, key: nil, as: MailSync.self)
            if let counts = got { self.synced = counts.line } else { self.notice = "Not synced with Gmail: " + (why ?? "no answer") }
            self.refresh()
        }
    }

    /// The first button, and e: archive the thread, or move it back to the inbox when it is archived or snoozed. The
    /// open thread makes way for the next one down the list (else the one above).
    func archiveOrRestore(_ id: String? = nil) {
        guard let id = id ?? selected, let t = current(id) else { return }
        let next = id == selected ? neighbour(of: id) : nil
        mark(id, t.box == MailBox.inbox.rawValue ? .archive : .inbox)
        if id == selected { if let n = next { open(n) } else { close() } }
    }

    /// Snooze the open thread until `date`; the next one opens as after Archive.
    func snooze(until date: Date) {
        guard let id = selected else { return }
        let next = neighbour(of: id)
        mark(id, .snooze, until: date.timeIntervalSince1970)
        if let n = next { open(n) } else { close() }
    }

    /// Mark a thread unread; the open one closes, as in Gmail, so it isn't marked read again when it next loads.
    func markUnread(_ id: String? = nil) {
        guard let id = id ?? selected else { return }
        mark(id, .unread)
        if id == selected { close() }
    }

    /// ↓ or j (1) and ↑ or k (-1): the next thread down the list or up it; the first one when none is open.
    func step(_ by: Int) {
        guard let list = threads, !list.isEmpty else { return }
        guard let id = selected, let i = list.firstIndex(where: { $0.id == id }) else { open(list[0].id); return }
        if list.indices.contains(i + by) { open(list[i + by].id) }
    }

    /// → (1) and ← (-1): the newer or older mail in the open thread becomes the one being read.
    func stepMessage(_ by: Int) {
        guard let s = shown, s.thread.id == selected, let key = MailRules.step(from: current, by: by, in: s.messages) else { return }
        current = key
    }

    private func neighbour(of id: String) -> String? {
        guard let list = threads, let i = list.firstIndex(where: { $0.id == id }) else { return nil }
        return list.indices.contains(i + 1) ? list[i + 1].id : (i > 0 ? list[i - 1].id : nil)
    }

    /// What a key does (MailKeys.command). `release` takes the window, so the key monitor does it.
    func perform(_ key: MailKey) {
        switch key {
        case .thread(let by): step(by)
        case .message(let by): stepMessage(by)
        case .page(let by): page = MailPage(n: (page?.n ?? 0) + 1, by: by)
        case .archive: archiveOrRestore()
        case .reply: reply()
        case .paste: _ = attach(.general)
        case .release: break
        }
    }

    /// Reply (the button, r): the open thread's reply box, with the keyboard in it.
    func reply() {
        guard let id = selected else { return }
        draft(for: id).ask()
    }

    /// An option picked in thread `id`'s mail (MailWidgets): `line` becomes its reply's first line (Draft.choose), and
    /// the box opens with the keyboard in it. Nothing is sent until the owner sends it.
    func choose(_ line: String, for id: String) {
        draft(for: id).choose(line)
    }

    /// Files or an image on `pb` (a paste, a drop) into the open thread's reply, which opens; whether there were any.
    /// Too many or too big: none, and the box says why.
    func attach(_ pb: NSPasteboard) -> Bool {
        let items = Attachments.items(pb)
        guard let id = selected, !items.isEmpty else { return false }
        add(items, to: id)
        return true
    }

    /// The paperclip: files chosen in a panel into thread `id`'s reply.
    func pickFiles(for id: String) {
        guard let w = Dialogs.window else { return }
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.message = "Files to send with your reply"
        panel.beginSheetModal(for: w) { [weak self] r in
            guard r == .OK else { return }
            MainActor.assumeIsolated { self?.add(panel.urls.map { .file($0) }, to: id) }
        }
    }

    private func add(_ items: [Attachments.Item], to id: String) {
        let d = draft(for: id)
        d.ask()
        switch drafts.add(items, to: id) {
        case .added: break
        case .refused(let why): d.error = why
        case .tooBig(let files): d.oversize = files
        }
    }

    /// What the reply box asks before copying `files`, too big for a reply, to the desktop.
    static func desktopOffer(_ files: [URL]) -> String {
        "\(files.map(\.lastPathComponent).joined(separator: ", ")): a reply carries at most "
            + "\(Fmt.bytes(Double(maxFileBytes))) of files. Copy \(files.count == 1 ? "it" : "them") to the desktop instead? "
            + "Your reply then says where on the desktop, and the files stay on your machines, out of mail and its backup."
    }

    /// Files too big for a reply, copied to the desktop instead (the box's prompt says so first): each one's path
    /// there joins thread `id`'s reply once it has arrived, and the reply waits for them.
    func copyToDesktop(_ files: [URL], for id: String) {
        let d = draft(for: id)
        for file in files {
            let name = file.lastPathComponent
            let size = (try? file.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? nil
            d.copyStarted(name)
            upload(file) { path, why in
                d.copyEnded(name, line: path.map { "Too big to attach, so copied to the desktop: \($0) (\(Fmt.bytes(size.map { Double($0) })))" },
                            failure: why.map { "\(name) not copied to the desktop: \($0)" })
            }
        }
    }

    /// Send a thread's draft, its text and files, as the owner's reply to its newest agent mail (mail_reply), then
    /// read the thread again, brought to that reply. Once the desktop has the files, their copies in the drafts go.
    func send(_ id: String) {
        let d = draft(for: id)
        let n = d.text.trimmingCharacters(in: .whitespacesAndNewlines).unicodeScalars.count
        guard n <= MailModel.maxReply else {
            d.error = "Too long to send: \(Fmt.count(Double(n))) characters; the mailroom takes at most \(Fmt.count(Double(MailModel.maxReply)))."
            return
        }
        guard let reply = d.begin() else { return }
        var req: [String: Any] = ["action": "mail_reply", "thread": id, "key": reply.key, "text": reply.text]
        do {
            if !reply.files.isEmpty {
                req["files"] = try reply.files.map { ["name": $0.name, "data": try drafts.data($0).base64EncodedString()] }
            }
        } catch {
            d.finish((reply.text, reply.files), failure: "Not sent: a file could not be read: \(error.localizedDescription)")
            return
        }
        call(req) { [weak self] obj, err in
            let (got, why) = Actions.decodeAnswer(obj, err, key: nil, as: MailReply.self)
            d.finish((reply.text, reply.files), failure: got == nil ? "Not sent: " + (why ?? "no answer") : nil)
            guard let sent = got, let self = self else { return }
            self.drafts.sent(reply.files)
            if self.selected == id { self.load(id, focus: sent.key, fresh: true) }
            self.refresh()
        }
    }

    /// A link clicked in the document.
    func follow(_ link: MailLink) {
        switch link {
        case .web(let url):
            if !NSWorkspace.shared.open(url) { notice = "macOS could not open \(url.absoluteString)" }
        case .part(let key, let index):
            openPart(key: key, index: index)
        case .other(let url):
            notice = "Not opened: \(url.absoluteString). Only web, mailto and attachment links open from mail."
        }
    }

    /// Fetch an attachment over the board's connection (mail_part), write it to MailFiles.folder and open it.
    private func openPart(key: String, index: Int) {
        call(["action": "mail_part", "key": key, "index": index]) { [weak self] obj, err in
            guard let self = self else { return }
            let (got, why) = Actions.decodeAnswer(obj, err, key: nil, as: MailPart.self)
            guard let part = got else { self.notice = "Attachment not opened: " + (why ?? "no answer"); return }
            guard let data = Data(base64Encoded: part.data) else {
                self.notice = "Attachment not opened: its data is not base64"
                return
            }
            do {
                let url = try MailFiles.save(data, name: part.name, key: key, index: index)
                if !NSWorkspace.shared.open(url) { self.notice = "Saved as \(url.path), but macOS could not open it." }
            } catch {
                self.notice = "Attachment not saved: \(error.localizedDescription)"
            }
        }
    }
}

// MARK: - views

/// The tab: the list on the left, the open thread on the right. Before the first snapshot it waits; with a snapshot
/// that has no mailbox section it says so and asks the desktop nothing.
struct MailView: View {
    let mail: MailModel           // the columns watch it; this view only places them
    let connected: Bool           // a snapshot has come
    let available: Bool           // it has a mailbox section
    @Environment(\.zoom) private var z

    var body: some View {
        if !connected {
            Text("Connecting to \(Settings.host)…").foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if !available {
            VStack(spacing: 8 * z) {
                Image(systemName: "tray").font(.system(size: 30 * z)).foregroundStyle(.secondary)
                Text("No mailbox on the desktop yet").font(.system(size: 15 * z, weight: .semibold))
                Text("The desktop's snapshot has no mailbox section: its mailroom has no `agent-mail mailbox` yet, or the board could not read it (the top bar's problems say). Mail between you and your agents shows here once it has.")
                    .font(.system(size: 12 * z)).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    .frame(maxWidth: 440 * z)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            HStack(spacing: 0) {
                MailListColumn(mail: mail, drafts: mail.drafts).frame(width: 300 * z)
                Divider()
                MailThreadPane(mail: mail)
            }
            .onAppear {
                mail.tabShown = true
                MailWeb.warm()
            }
            .onDisappear { mail.tabShown = false }
        }
    }
}

/// The left pane: search, the box, what the open thread's buttons do (ThreadActions), and the list.
struct MailListColumn: View {
    @ObservedObject var mail: MailModel
    @ObservedObject var drafts: MailDrafts
    @Environment(\.zoom) private var z
    @State private var query = ""
    @State private var confirmSync = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 8 * z) {
                HStack(spacing: 6 * z) {
                    HStack(spacing: 6 * z) {
                        Image(systemName: "magnifyingglass").font(.system(size: 12 * z)).foregroundStyle(.secondary)
                        TextField("Search all mail", text: $query)
                            .textFieldStyle(.plain).font(.system(size: 12.5 * z))
                            .onSubmit {
                                mail.find(query)
                                Dialogs.window?.makeFirstResponder(nil)      // the arrows go to the results
                            }
                        if !query.isEmpty || !mail.search.isEmpty {
                            Button { query = ""; mail.find("") } label: { Image(systemName: "xmark.circle.fill") }
                                .buttonStyle(.borderless).foregroundStyle(.secondary).help("Back to the box")
                        }
                    }
                    .padding(.horizontal, 8 * z).padding(.vertical, 6 * z)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.06)))
                    if mail.syncing {
                        ProgressView().controlSize(.small).help("Syncing with Gmail…")
                    } else {
                        Menu {
                            Button("Sync with Gmail…") { confirmSync = true }
                        } label: {
                            Image(systemName: "ellipsis.circle").font(.system(size: 14 * z))
                        }
                        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                        .help("Sync with Gmail")
                    }
                }
                .alert("Sync the Mail tab and Gmail?", isPresented: $confirmSync) {
                    Button("Cancel", role: .cancel) {}
                    Button("Sync") { mail.sync() }
                } message: {
                    Text("What is done in either counts as done in both: threads you archived or read in Gmail are archived or marked read here, and agent mail you archived or read here is archived or marked read in Gmail. Nothing comes back to the inbox or goes unread, and snoozed threads are left alone. It needs your Gmail login set up on the desktop (gmail_credentials in the board's config); without one the desktop says so here.")
                }
                if mail.search.isEmpty {
                    Picker("", selection: Binding(get: { mail.box }, set: { mail.show($0) })) {
                        ForEach(MailBox.allCases) { Text($0.label).tag($0) }
                    }
                    .pickerStyle(.segmented).labelsHidden()
                } else {
                    Text("Search: “\(mail.search)”, in every box")
                        .font(.system(size: 11.5 * z, weight: .medium)).lineLimit(2)
                }
                if let line = summary {
                    Text(line).font(.system(size: 10.5 * z)).foregroundStyle(.secondary)
                }
                if mail.syncing {
                    Text("Syncing with Gmail…").font(.system(size: 10.5 * z)).foregroundStyle(.secondary)
                } else if let line = mail.synced {
                    Text(line).font(.system(size: 10.5 * z, weight: .medium)).fixedSize(horizontal: false, vertical: true)
                }
                if let e = mail.listError {
                    Text(e).font(.system(size: 11 * z)).foregroundColor(AgentState.blocked.color).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ThreadActions(mail: mail)
            }
            .padding(10 * z)
            Divider()
            list
        }
        .onAppear { query = mail.search }
    }

    var summary: String? {
        if !mail.search.isEmpty { return mail.threads.map { "\($0.count) thread\($0.count == 1 ? "" : "s")" } }
        guard let c = mail.counts else { return nil }
        return "\(c.inbox) in the inbox · \(c.unread) unread · \(c.snoozed) snoozed"
    }

    var empty: String {
        if !mail.search.isEmpty { return "No thread has all of these words." }
        switch mail.box {
        case .inbox: return "Nothing in the inbox."
        case .snoozed: return "Nothing snoozed."
        case .archived: return "Nothing archived."
        case .all: return "No mail yet."
        }
    }

    var list: some View {
        ScrollViewReader { reader in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if let threads = mail.threads {
                        if threads.isEmpty {
                            Text(empty).font(.system(size: 12 * z)).foregroundStyle(.secondary).padding(14 * z)
                        }
                        ForEach(threads) { t in
                            MailRow(thread: t, selected: t.id == mail.selected, now: mail.now,
                                    mixed: mail.listing == .all || mail.listing == nil, draft: drafts.held.contains(t.id))
                                .contentShape(Rectangle())
                                .onTapGesture { mail.open(t.id) }
                                .contextMenu {
                                    Button(t.unread ? "Mark as read" : "Mark as unread") {
                                        if t.unread { mail.mark(t.id, .read) } else { mail.markUnread(t.id) }
                                    }
                                    Button(t.box == MailBox.inbox.rawValue ? "Archive" : "Move to Inbox") { mail.archiveOrRestore(t.id) }
                                }
                                .id(t.id)
                            Divider().padding(.leading, 24 * z)
                        }
                        if threads.count >= MailModel.listLimit {
                            Text("The newest \(threads.count); search to find older mail.")
                                .font(.system(size: 11 * z)).foregroundStyle(.secondary).padding(12 * z)
                        }
                    } else if mail.listError == nil {
                        ProgressView().controlSize(.small).frame(maxWidth: .infinity).padding(.top, 20 * z)
                    }
                }
            }
            .onChange(of: mail.selected) { id in if let id = id { reader.scrollTo(id) } }
        }
    }
}

/// One thread in the list, Gmail's way: who, how many, when; the subject; the start of the newest message. Unread is
/// bold and dark with a dot; read is plain and greyed, on a grey ground. "Draft" marks a reply the owner has started; a
/// snoozed thread shows when it comes back instead of its time.
struct MailRow: View {
    let thread: MailboxThread
    let selected: Bool
    let now: Date
    let mixed: Bool               // the list holds every box (All, or a search): mark the archived ones
    let draft: Bool               // an unsent reply of the owner's is kept for it
    @Environment(\.zoom) private var z

    var body: some View {
        let t = thread
        let ink: Color = t.unread ? .primary : .secondary
        HStack(alignment: .top, spacing: 7 * z) {
            Circle().fill(t.unread ? Palette.ok : Color.clear).frame(width: 7 * z, height: 7 * z).padding(.top, 5 * z)
            VStack(alignment: .leading, spacing: 2 * z) {
                HStack(alignment: .firstTextBaseline, spacing: 5 * z) {
                    Text(t.agents.isEmpty ? "(no agent)" : t.agents.joined(separator: ", "))
                        .font(.system(size: 12.5 * z, weight: t.unread ? .bold : .regular)).foregroundColor(ink).lineLimit(1)
                    if t.count > 1 {
                        Text("\(t.count)").font(.system(size: 10.5 * z, weight: .semibold).monospacedDigit()).foregroundColor(ink)
                            .padding(.horizontal, 5 * z).padding(.vertical, 1)
                            .background(Capsule().fill(Color.primary.opacity(0.1)))
                            .help("\(t.count) mails in this thread")
                    }
                    if draft {
                        Text("Draft").font(.system(size: 11.5 * z, weight: .semibold)).foregroundColor(AgentState.blocked.color)
                    }
                    Spacer(minLength: 4 * z)
                    if t.attachments {
                        Image(systemName: "paperclip").font(.system(size: 10.5 * z)).foregroundStyle(.secondary)
                    }
                    when
                }
                Text(t.subject.isEmpty ? "(no subject)" : t.subject)
                    .font(.system(size: 12 * z, weight: t.unread ? .bold : .regular)).foregroundColor(ink).lineLimit(1)
                Text(t.snippet).font(.system(size: 11.5 * z)).foregroundStyle(.secondary).lineLimit(2)
                if mixed && t.box == MailBox.archived.rawValue {
                    Text("Archived").font(.system(size: 10 * z, weight: .medium)).foregroundStyle(.secondary)
                        .padding(.horizontal, 5 * z).padding(.vertical, 1)
                        .background(Capsule().fill(Color.primary.opacity(0.08)))
                }
            }
        }
        .padding(.vertical, 8 * z).padding(.horizontal, 10 * z)
        .background(selected ? Palette.ok.opacity(0.16) : t.unread ? Color.clear : Color.primary.opacity(0.045))
    }

    @ViewBuilder var when: some View {
        if thread.box == MailBox.snoozed.rawValue, let wake = thread.snoozeUntil {
            HStack(spacing: 2 * z) {
                Image(systemName: "clock")
                Text(MailFmt.at(wake, now: now))
            }
            .font(.system(size: 11 * z, weight: .medium).monospacedDigit())
            .foregroundColor(Palette.attention)
            .help("Snoozed: back in the inbox, unread, at this time")
        } else {
            Text(MailFmt.listTime(thread.last, now: now))
                .font(.system(size: 11 * z, weight: thread.unread ? .semibold : .regular).monospacedDigit())
                .foregroundStyle(thread.unread ? HierarchicalShapeStyle.primary : .secondary)
        }
    }
}

/// Above the list: Reply, Archive (Inbox for a thread out of the inbox), Snooze and Unread, for the open thread.
struct ThreadActions: View {
    @ObservedObject var mail: MailModel
    @Environment(\.zoom) private var z
    @State private var picking = false
    @State private var picked = Date()

    var body: some View {
        let t = mail.selected.flatMap { mail.current($0) }
        let inbox = t?.box == MailBox.inbox.rawValue
        HStack(spacing: 8 * z) {
            Button { mail.reply() } label: { Label("Reply", systemImage: "arrowshape.turn.up.left") }
                .help("Reply (r); drag files onto the mail or paste them to send them with it").fixedSize()
            Button { mail.archiveOrRestore() } label: {
                Label(inbox ? "Archive" : "Inbox", systemImage: inbox ? "archivebox" : "tray.and.arrow.down")
            }
            .help(inbox ? "Archive (e): out of the inbox until an agent writes in it again" : "Back to the inbox (e)")
            .fixedSize()
            snoozeMenu
            Button { mail.markUnread() } label: { Label("Unread", systemImage: "envelope.badge") }
                .help("Mark unread, and close").fixedSize()
            Spacer(minLength: 0)
        }
        .buttonStyle(.borderless)
        .labelStyle(.titleAndIcon)
        .font(.system(size: 11.5 * z))
        .disabled(t == nil)
        .sheet(isPresented: $picking) {
            SnoozePicker(date: $picked) { date in
                picking = false
                if let d = date { mail.snooze(until: d) }
            }
        }
    }

    var snoozeMenu: some View {
        let now = Date()
        return Menu {
            ForEach(Snooze.presets(now: now), id: \.label) { p in
                Button("\(p.label) · \(MailFmt.at(p.date.timeIntervalSince1970, now: now))") { mail.snooze(until: p.date) }
            }
            Divider()
            Button("Pick date & time…") {
                picked = Snooze.morning(after: Date()) ?? Date().addingTimeInterval(86400)
                picking = true
            }
        } label: {
            Label("Snooze", systemImage: "clock")
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .help("Out of the inbox until then; back unread")
    }
}

/// The right pane: the open thread's document (its subject heads it, so scrolls with it), the bar naming the mail
/// being read in a thread of several, and the reply box while there is one (ReplyArea).
struct MailThreadPane: View {
    @ObservedObject var mail: MailModel
    @Environment(\.zoom) private var z

    var body: some View {
        if let id = mail.selected {
            VStack(alignment: .leading, spacing: 0) {
                notices
                reading(id)
                document(id)
                let mine = mail.shown?.thread.id == id ? mail.shown : nil
                let to = mine?.messages.last(where: { !$0.mine })?.from ?? mail.current(id)?.agents.last
                ReplyArea(mail: mail, id: id, draft: mail.draft(for: id), to: to,
                          replies: mine?.messages.filter { $0.mine && $0.status != nil } ?? [])
            }
        } else {
            VStack(spacing: 10 * z) {
                notices
                Spacer()
                Image(systemName: "envelope.open").font(.system(size: 30 * z)).foregroundStyle(.tertiary)
                Text("No thread open").font(.system(size: 14 * z, weight: .medium)).foregroundStyle(.secondary)
                Text("Click one on the left, or press ↓.").font(.system(size: 12 * z)).foregroundStyle(.secondary)
                Spacer()
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    @ViewBuilder var notices: some View {
        if let n = mail.notice {
            HStack(alignment: .top, spacing: 8 * z) {
                Text(n).font(.system(size: 11.5 * z)).foregroundColor(AgentState.blocked.color).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer()
                Button { mail.notice = nil } label: { Image(systemName: "xmark") }.buttonStyle(.borderless)
            }
            .padding(.horizontal, 14 * z).padding(.vertical, 8 * z)
        }
    }

    /// In a thread of several mails, the one being read (outlined in the document): "Mail 2 of 5 · its sender · its
    /// time", and the buttons that do ← and →.
    @ViewBuilder func reading(_ id: String) -> some View {
        if let s = mail.shown, s.thread.id == id, s.messages.count > 1,
           let i = s.messages.firstIndex(where: { $0.key == mail.current }) {
            let m = s.messages[i]
            HStack(spacing: 8 * z) {
                Text("Mail \(i + 1) of \(s.messages.count)").font(.system(size: 12 * z, weight: .semibold).monospacedDigit())
                Text("\(m.mine ? "You" : m.from) · \(MailFmt.at(m.ts, now: Date()))")
                    .font(.system(size: 12 * z)).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                HStack(spacing: 8 * z) {
                    Button { mail.stepMessage(-1) } label: { Image(systemName: "chevron.left") }
                        .help("Older mail (←)").disabled(i == 0)
                    Button { mail.stepMessage(1) } label: { Image(systemName: "chevron.right") }
                        .help("Newer mail (→)").disabled(i == s.messages.count - 1)
                }
                .buttonStyle(.borderless)
            }
            .padding(.horizontal, 8 * z).padding(.vertical, 4 * z)
            .background(RoundedRectangle(cornerRadius: 6).fill(Palette.ok.opacity(0.12)))
            .padding(.horizontal, 10 * z).padding(.vertical, 6 * z)
        }
    }

    @ViewBuilder func document(_ id: String) -> some View {
        if let s = mail.shown {
            MailDocument(html: s.document, thread: s.thread.id, widgets: s.widgets, current: mail.current,
                         page: mail.page, zoom: z, follow: { mail.follow($0) }, refuse: { mail.notice = $0 },
                         choose: { mail.choose($1, for: $0) }, problem: { mail.notice = $0 }, take: { mail.attach($0) })
                .overlay {
                    if s.thread.id != id {
                        ZStack {
                            Color(nsColor: .windowBackgroundColor).opacity(0.85)
                            if let e = mail.threadError { error(e) } else { ProgressView() }
                        }
                    }
                }
        } else if let e = mail.threadError {
            error(e)
        } else {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    func error(_ e: String) -> some View {
        Text("Not shown: " + e).font(.system(size: 12 * z)).foregroundColor(AgentState.blocked.color).textSelection(.enabled)
            .padding(20 * z).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
    }
}

/// Under the thread: its reply box, while the owner has asked for it (Reply) or it holds a draft; else nothing, so the
/// document has the pane.
struct ReplyArea: View {
    let mail: MailModel
    let id: String
    @ObservedObject var draft: Draft
    let to: String?                // whose mail a reply answers
    let replies: [MailboxMessage]  // the owner's replies in the thread that have a state
    @Environment(\.zoom) private var z

    var body: some View {
        if draft.shown {
            VStack(spacing: 0) {
                Divider()
                ComposeBox(draft: draft, placeholder: to.map { "Reply to \($0)…" } ?? "Reply…", height: 110,
                           files: ComposeFiles(take: { mail.attach($0) }, pick: { mail.pickFiles(for: id) },
                                               remove: { mail.drafts.remove($0, from: id) }, offer: MailModel.desktopOffer,
                                               action: "Copy to the desktop", instead: { mail.copyToDesktop($0, for: id) }),
                           send: { mail.send(id) }, discard: { mail.drafts.discard(id) }) {
                    ReplyStates(to: to, replies: replies)
                }
                .padding(12 * z)
            }
        }
    }
}

/// Beside a thread's Send button: how the owner's last three replies in it got on, or, before any reply, what ⌘↩ does.
struct ReplyStates: View {
    let to: String?               // whose mail a reply answers: the thread's newest agent message's sender
    let replies: [MailboxMessage] // the owner's replies in the thread that have a state, oldest first
    @Environment(\.zoom) private var z

    var body: some View {
        if replies.isEmpty {
            Text("⌘↩ sends" + (to.map { ", in answer to \($0)'s last mail" } ?? ""))
                .font(.system(size: 11 * z)).foregroundStyle(.secondary)
        } else {
            HStack(spacing: 5 * z) {
                Text("Your replies:").foregroundStyle(.secondary)
                ForEach(replies.suffix(3)) { r in
                    Text(Fmt.clock(r.ts)).foregroundStyle(.secondary)
                    Text(r.status ?? "").foregroundColor(MailFmt.statusColor(r.status ?? ""))
                }
            }
            .font(.system(size: 11 * z).monospacedDigit()).lineLimit(1)
        }
    }
}

/// Snooze until a chosen moment, from the Snooze menu's last item.
struct SnoozePicker: View {
    @Binding var date: Date
    let done: (Date?) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Snooze until").font(.headline)
            DatePicker("", selection: $date, in: Date()..., displayedComponents: [.date, .hourAndMinute])
                .datePickerStyle(.graphical).labelsHidden()
            HStack {
                Text(MailFmt.at(date.timeIntervalSince1970, now: Date())).foregroundStyle(.secondary)
                Spacer()
                Button("Cancel") { done(nil) }.keyboardShortcut(.cancelAction)
                Button("Snooze") { done(date) }.keyboardShortcut(.defaultAction).disabled(date <= Date())
            }
        }
        .padding(18)
        .frame(width: 360)
    }
}

// MARK: - --selftest-mail

/// --selftest-mail: the tab's rules, then the requests a MailModel makes as a thread is opened, stepped through,
/// archived, snoozed, marked and answered, against a stand-in desktop whose requests wait until the test answers them
/// (so it picks the order they come back in). Offline; it writes one attachment to a temporary folder and removes it.
@MainActor
enum MailTest {
    static func run() -> Int32 {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append(what) } }
        rules(check)
        model(check)
        ahead(check)
        drafts(check)
        choices(check)
        if fails.isEmpty { print("mail selftest: ok"); return 0 }
        fails.forEach { print("FAIL: " + $0) }
        return 1
    }

    static func rules(_ check: (Bool, String) -> Void) {
        // reply keys: `mailbox answer --key` takes 16-64 of [A-Za-z0-9_-]
        let keyRE = try! NSRegularExpression(pattern: "^[A-Za-z0-9_-]{16,64}$")
        let keys = (0..<1000).map { _ in newPostID() }
        check(keys.allSatisfy { keyRE.firstMatch(in: $0, range: NSRange($0.startIndex..., in: $0)) != nil },
              "a reply key the mailroom refuses")
        check(Set(keys).count == keys.count, "a reply key came up twice in \(keys.count)")

        // snooze choices, on a fixed test calendar (UTC, a neutral locale) so the expected text is the same on every
        // Mac; the app itself uses the Mac's own time zone and locale (Calendar.current)
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        cal.locale = Locale(identifier: "en_US_POSIX")
        let iso = ISO8601DateFormatter()
        func at(_ s: String) -> Date { iso.date(from: s)! }
        func presets(_ s: String) -> [String] {
            Snooze.presets(now: at(s), calendar: cal).map { "\($0.label) " + MailFmt.formatter("EEE d HH:mm", cal).string(from: $0.date) }
        }
        let wed = presets("2026-11-18T14:20:00Z")
        check(wed == ["Later today Wed 18 17:00", "Tomorrow Thu 19 08:00", "This weekend Sat 21 08:00", "Next week Mon 23 08:00"],
              "a Wednesday afternoon's choices: \(wed)")
        check(presets("2026-11-18T14:40:00Z").first == "Later today Wed 18 18:00", "17:40 rounds to 18:00")
        check(presets("2026-11-18T12:30:00Z").first == "Later today Wed 18 16:00", "15:30 rounds to 16:00")
        check(presets("2026-11-18T21:40:00Z").first == "Tomorrow Thu 19 08:00", "late evening: no later today (it would be tomorrow)")
        let fri = presets("2026-11-20T23:00:00Z")
        check(fri == ["Tomorrow Sat 21 08:00", "Next week Mon 23 08:00"], "Friday night: the weekend is tomorrow, said once: \(fri)")
        let sat = presets("2026-11-21T10:00:00Z")
        check(sat == ["Later today Sat 21 13:00", "Tomorrow Sun 22 08:00", "Next week Mon 23 08:00"], "Saturday: no weekend: \(sat)")
        let sun = presets("2026-11-22T10:00:00Z")
        check(sun == ["Later today Sun 22 13:00", "Tomorrow Mon 23 08:00"], "Sunday: next week is tomorrow, said once: \(sun)")
        let mon = presets("2026-11-23T06:00:00Z")
        check(mon.last == "Next week Mon 30 08:00", "early Monday: next week is the coming Monday: \(mon)")

        // the list's times, and times ahead
        let now = at("2026-11-18T14:20:00Z")
        func listed(_ s: String) -> String { MailFmt.listTime(at(s).timeIntervalSince1970, now: now, calendar: cal) }
        let times = ["2026-11-18T09:05:00Z", "2026-11-17T22:00:00Z", "2026-11-16T10:00:00Z", "2026-11-12T15:00:00Z",
                     "2026-11-12T14:00:00Z", "2025-12-01T10:00:00Z"].map(listed)
        check(times == ["09:05", "Yesterday", "Mon", "Thu", "12 Nov", "1 Dec 2025"], "the list's times: \(times)")
        let ahead = ["2026-11-18T17:00:00Z", "2026-11-21T08:00:00Z", "2026-11-30T08:00:00Z"]
            .map { MailFmt.at(at($0).timeIntervalSince1970, now: now, calendar: cal) }
        check(ahead == ["17:00", "Sat 08:00", "Mon 30 Nov 08:00"], "times ahead: \(ahead)")

        // what the list shows before the desktop answers
        func row(_ id: String, _ box: MailBox = .inbox) -> MailboxThread {
            MailboxThread(id: id, subject: id, agents: ["a"], snippet: "", last: 1, count: 1, unread: true, box: box.rawValue,
                          snoozeUntil: box == .snoozed ? 5 : nil, newest: 1, attachments: false)
        }
        let inbox = [row("a"), row("b")]
        func ids(_ l: [MailboxThread]) -> [String] { l.map(\.id) }
        check(ids(MailRules.applying(.archive, to: "a", in: inbox, box: .inbox, until: nil)) == ["b"], "archived: out of the inbox")
        check(ids(MailRules.applying(.snooze, to: "a", in: inbox, box: .inbox, until: 9)) == ["b"], "snoozed: out of the inbox")
        let read = MailRules.applying(.read, to: "a", in: inbox, box: .inbox, until: nil)
        check(ids(read) == ["a", "b"] && read[0].unread == false && read[1].unread, "read: still listed, not bold")
        check(MailRules.applying(.inbox, to: "s", in: [row("s", .snoozed)], box: .snoozed, until: nil).isEmpty, "back to the inbox: out of Snoozed")
        let all = MailRules.applying(.archive, to: "a", in: inbox, box: .all, until: nil)
        check(ids(all) == ["a", "b"] && all[0].box == "archived", "All keeps an archived thread, marked archived")
        let found = MailRules.applying(.snooze, to: "a", in: inbox, box: nil, until: 9)
        check(ids(found) == ["a", "b"] && found[0].box == "snoozed" && found[0].snoozeUntil == 9, "a search keeps a snoozed thread")
        check(MailRules.changed(row("s", .snoozed), .inbox, until: nil).snoozeUntil == nil, "back in the inbox: no wake time")

        // links in the document
        func link(_ s: String) -> MailLink { MailLink(URL(string: s)!) }
        check(link("mailbox-part:s101/1") == .part(key: "s101", index: 1), "an attachment link")
        check(link("mailbox-part:i12/1") == .part(key: "i12", index: 1), "a file of your own reply")
        for bad in ["mailbox-part:s12/0", "mailbox-part:x1/1", "mailbox-part:s101/1/2", "mailbox-part:s/1", "mailbox-part:s1/-1", "mailbox-part:s1/+1",
                    "mailbox-part:s1/", "file:///etc/passwd", "javascript:alert(1)", "about:blank#x"] {
            if case .other = link(bad) {} else { check(false, "\(bad) is not a link that opens") }
        }
        check(link("https://example.com/x") == .web(URL(string: "https://example.com/x")!), "a web link opens")
        check(link("mailto:a@b.c") == .web(URL(string: "mailto:a@b.c")!), "a mailto link opens")

        // the document view's navigations: only its own load goes ahead, once; a click is followed outside it
        let blank = URL(string: "about:blank")
        let web = URL(string: "https://example.com/doc")
        func nav(_ expecting: Bool, _ type: WKNavigationType, main: Bool = true, _ url: URL?) -> MailNavigation {
            MailNavigation.decide(expecting: expecting, type: type, mainFrame: main, url: url)
        }
        check(nav(true, .other, blank) == .load, "its own load of the document goes ahead")
        check(nav(false, .other, blank) == .cancel, "a load of about:blank it did not start is stopped")
        check(nav(true, .other, main: false, blank) == .cancel, "a frame inside the document loads nothing")
        check(nav(true, .other, web) == .cancel && nav(false, .other, web) == .cancel, "a redirect or refresh elsewhere is stopped")
        check(nav(true, .linkActivated, blank) == .follow(.other(blank!)), "a link to about:blank is not mistaken for its load")
        check(nav(false, .linkActivated, web) == .follow(.web(web!)), "a clicked web link is followed outside the view")
        check(nav(false, .linkActivated, URL(string: "mailbox-part:s1/2")) == .follow(.part(key: "s1", index: 2)),
              "a clicked attachment is followed")
        for type in [WKNavigationType.formSubmitted, .backForward, .reload, .formResubmitted] {
            check(nav(true, type, blank) == .cancel && nav(false, type, web) == .cancel, "a \(type.rawValue) navigation is stopped")
        }

        // attachment files
        check(MailFiles.fileName("results.csv") == "results.csv", "a plain name is kept")
        check(MailFiles.fileName("../../etc/passwd") == "_.._etc_passwd", "no folders: \(MailFiles.fileName("../../etc/passwd"))")
        check(MailFiles.fileName("a\nb:c.txt") == "a_b_c.txt", "no line breaks or colons")
        check(MailFiles.fileName(" ... ") == "attachment", "nothing left: a name of its own")
        let long = MailFiles.fileName(String(repeating: "x", count: 300) + ".pdf")
        check(long.count == 120 && long.hasSuffix(".pdf"), "a long name is cut, keeping its extension")
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("agentboard-mailtest-\(UUID().uuidString)")
        do {
            let url = try MailFiles.save(Data("a,b\n".utf8), name: "results.csv", key: "s102", index: 1, in: root)
            let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
            let dirAttrs = try FileManager.default.attributesOfItem(atPath: url.deletingLastPathComponent().path)
            check(url.lastPathComponent == "results.csv" && url.deletingLastPathComponent().lastPathComponent == "s102-1",
                  "written as KEY-INDEX/NAME: \(url.path)")
            check((attrs[.posixPermissions] as? Int) == 0o600 && (dirAttrs[.posixPermissions] as? Int) == 0o700,
                  "readable by this user only")
            check(try url.resourceValues(forKeys: [.quarantinePropertiesKey]).quarantineProperties != nil, "quarantined")
            check(try Data(contentsOf: url) == Data("a,b\n".utf8), "the bytes written")
        } catch {
            check(false, "an attachment could not be saved: \(error.localizedDescription)")
        }
        try? FileManager.default.removeItem(at: root)        // a temporary folder: the system clears it anyway

        // the tab's keys, and only Esc while the owner types
        func key(_ chars: String, _ code: UInt16, _ flags: NSEvent.ModifierFlags, typing: Bool) -> MailKey? {
            MailKeys.command(chars, keyCode: code, flags: flags, typing: typing)
        }
        let arrow: NSEvent.ModifierFlags = [.numericPad, .function]          // what an arrow key carries by itself
        check(key(MailKeys.down, 125, arrow, typing: false) == .thread(1) && key("j", 38, [], typing: false) == .thread(1),
              "↓ and j: the thread below")
        check(key(MailKeys.up, 126, arrow, typing: false) == .thread(-1) && key("k", 40, [], typing: false) == .thread(-1),
              "↑ and k: the thread above")
        check(key(MailKeys.right, 124, arrow, typing: false) == .message(1) && key(MailKeys.left, 123, arrow, typing: false) == .message(-1),
              "→ and ←: the newer and older mail")
        check(key(" ", 49, [], typing: false) == .page(1) && key(" ", 49, .shift, typing: false) == .page(-1)
              && key("e", 14, [], typing: false) == .archive, "Space, ⇧Space and e")
        check(key("j", 38, .command, typing: false) == nil && key(MailKeys.left, 123, arrow.union(.option), typing: false) == nil
              && key("E", 14, .shift, typing: false) == nil && key(MailKeys.down, 125, arrow.union(.shift), typing: false) == nil
              && key("x", 7, [], typing: false) == nil, "⌘j, ⌥←, ⇧e, ⇧↓ and x are not the tab's")
        check(key(MailKeys.down, 125, arrow, typing: true) == nil && key("e", 14, [], typing: true) == nil
              && key(" ", 49, [], typing: true) == nil, "while typing, the keys type")
        let esc = UInt16(kVK_Escape)
        check(key("\u{1b}", esc, [], typing: true) == .release && key("\u{1b}", esc, [], typing: false) == nil,
              "Esc stops the typing, and is left alone otherwise")
        let v = UInt16(kVK_ANSI_V)
        check(key("r", 15, [], typing: false) == .reply && key("r", 15, [], typing: true) == nil, "r replies, unless typing")
        check(key("v", v, .command, typing: false) == .paste, "⌘V with no text box: files into the reply")
        check(key("v", v, .command, typing: true) == nil && key("v", v, [.command, .shift], typing: false) == nil
              && key("c", 8, .command, typing: false) == nil, "⌘V in a text box is its own; ⇧⌘V and ⌘C are not the tab's")

        // the message a thread opens at, and ← → through it
        let msgs = [MailboxMessage(key: "s1", from: "a", mine: false, ts: 1, unread: false, status: nil),
                    MailboxMessage(key: "s2", from: "a", mine: false, ts: 2, unread: true, status: nil),
                    MailboxMessage(key: "i3", from: "Alex", mine: true, ts: 3, unread: false, status: "delivered")]
        check(MailRules.focus(msgs, unread: true) == "s2", "an unread thread opens at its first unread mail")
        check(MailRules.focus(msgs, unread: false) == "i3", "a read one at its newest, whatever its mails were kept saying")
        check(MailRules.step(from: "s2", by: -1, in: msgs) == "s1" && MailRules.step(from: "s2", by: 1, in: msgs) == "i3",
              "one older, one newer")
        check(MailRules.step(from: "s1", by: -1, in: msgs) == nil && MailRules.step(from: "i3", by: 1, in: msgs) == nil
              && MailRules.step(from: "s9", by: 1, in: msgs) == nil, "nothing past either end, or from a mail not there")

        // the cache: the least recently used goes past the budget; an older request's answer does not replace a newer one
        func kept(_ id: String, _ doc: String) -> MailShow {
            Actions.decodeAnswer(["ok": true, "thread": thread(id, newest: 1, last: 1), "messages": [Any](), "document": doc],
                                 nil, key: nil, as: MailShow.self).0!
        }
        var cache = MailCache(budget: 10)
        cache.put(kept("a", "aaaa"), asked: 1)
        cache.put(kept("b", "bbbb"), asked: 2)
        check(cache.take("a") != nil, "a kept thread is taken")
        cache.put(kept("c", "cccc"), asked: 3)
        check(cache.entries.keys.sorted() == ["a", "c"], "past the budget, the least recently used goes: \(cache.entries.keys.sorted())")
        cache.put(kept("a", "old"), asked: 0)
        check(cache.entries["a"]?.document == "aaaa", "an answer to an older request is not kept over a newer one")
        cache.put(kept("a", "new!"), asked: 4)
        check(cache.entries["a"]?.document == "new!", "a newer one is")
        cache.put(kept("big", String(repeating: "x", count: 20)), asked: 5)
        check(cache.entries.keys.sorted() == ["big"], "a document over the budget by itself is still kept, alone")

        // a list with a thread that does not decode keeps the rest; one with no threads is not a list
        let mixed: [String: Any] = ["ok": true, "threads": [thread("a", newest: 1, last: 1), ["id": 7]]]
        let (list, _) = Actions.decodeAnswer(mixed, nil, key: nil, as: MailList.self)
        check(list?.threads.map(\.id) == ["a"] && list?.dropped.count == 1, "the thread that decodes is kept, the other named")
        let (none, why) = Actions.decodeAnswer(["ok": true, "counts": ["inbox": 0, "unread": 0, "snoozed": 0]], nil,
                                               key: nil, as: MailList.self)
        check(none == nil && why != nil, "an answer without threads is not an empty list")
    }

    /// A thread as mail_list and mail_show give it.
    static func thread(_ id: String, unread: Bool = false, newest: Int, count: Int = 1, last: Double,
                       box: MailBox = .inbox) -> [String: Any] {
        ["id": id, "subject": "about " + id, "agents": ["mailtab (claude)"], "snippet": "…", "last": last, "count": count,
         "unread": unread, "box": box.rawValue, "snooze_until": NSNull(), "newest": newest, "attachments": false]
    }

    static func model(_ check: (Bool, String) -> Void) {
        var asked: [(request: [String: Any], done: @MainActor ([String: Any]?, String?) -> Void)] = []
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        var copied: [URL] = []                                             // nothing is copied to the desktop here
        let mail = MailModel(call: { request, done in asked.append((request, done)) },
                             upload: { file, _ in copied.append(file) },
                             ahead: 0, cacheBytes: 1 << 20, draftsFolder: folder)
        func waiting() -> [String] { asked.map { $0.request["action"] as? String ?? "?" } }
        /// Answer the oldest request for `action`; that request.
        @discardableResult func answer(_ action: String, _ obj: [String: Any]?, err: String? = nil) -> [String: Any] {
            guard let i = asked.firstIndex(where: { ($0.request["action"] as? String) == action }) else {
                check(false, "no \(action) request to answer (waiting: \(waiting()))")
                return [:]
            }
            let (request, done) = asked.remove(at: i)
            done(obj, err)
            return request
        }
        func list(_ threads: [[String: Any]]) -> [String: Any] {
            ["ok": true, "threads": threads, "counts": ["inbox": threads.count, "unread": 0, "snoozed": 0]]
        }
        func show(_ t: [String: Any], _ messages: [[String: Any]]) -> [String: Any] {
            ["ok": true, "thread": t, "messages": messages, "document": "<p>\(t["id"] ?? "")</p>"]
        }
        func message(_ key: String, mine: Bool = false, unread: Bool = false, status: String? = nil) -> [String: Any] {
            ["key": key, "from": mine ? "Alex" : "mailtab", "mine": mine, "ts": 1, "unread": unread,
             "status": status.map { $0 as Any } ?? NSNull()]
        }
        let marked: [String: Any] = ["ok": true, "threads": [Any]()]
        let a = thread("A", unread: true, newest: 50, count: 2, last: 500), aRead = thread("A", newest: 50, count: 2, last: 500)
        let b = thread("B", newest: 40, last: 400), c = thread("C", newest: 30, last: 300), d = thread("D", newest: 20, last: 200)

        mail.refresh()
        let first = answer("mail_list", list([a, b, c, d]))
        check(first["box"] as? String == "inbox" && first["query"] == nil && first["limit"] as? Int == MailModel.listLimit,
              "the inbox is listed first, the newest \(MailModel.listLimit): \(first)")
        check(mail.threads?.map(\.id) == ["A", "B", "C", "D"], "the list as the desktop sorts it")

        // ↓ with nothing open opens the first thread; an unread one is marked read up to its newest message
        mail.perform(.thread(1))
        check(waiting() == ["mail_show"], "↓ opens the first thread: \(waiting())")
        answer("mail_show", show(a, [message("s49"), message("s50", unread: true)]))
        check(mail.current == "s50", "it opens at the first unread message")
        check(mail.threads?.first?.unread == false, "read at once in the list")
        let read = answer("mail_mark", marked)
        check(read["op"] as? String == "read" && read["upto"] as? Int == 50 && read["threads"] as? [String] == ["A"],
              "marked read up to its newest: \(read)")
        answer("mail_list", list([aRead, b, c, d]))

        // ↓ and ↑ step through; a thread read before and unchanged in the list shows at once, at its newest message
        // now that it is read, and is not marked again
        mail.perform(.thread(1))
        check(mail.selected == "B", "↓: the next one down")
        answer("mail_show", show(b, [message("s40")]))
        mail.perform(.thread(-1))
        check(mail.selected == "A" && mail.shown?.thread.id == "A" && mail.current == "s50", "↑: the one above, kept")
        check(mail.shown?.thread.unread == false, "with the list's flags, not those it was kept with")
        check(waiting().isEmpty, "nothing asked: \(waiting())")

        // e archives the open thread up to its newest message and opens the next one down, kept already
        mail.perform(.archive)
        check(waiting() == ["mail_mark"], "e: archive, and the next thread from the cache: \(waiting())")
        check(mail.selected == "B" && mail.shown?.thread.id == "B" && mail.threads?.map(\.id) == ["B", "C", "D"],
              "it leaves the inbox at once")
        let archived = answer("mail_mark", marked)
        check(archived["op"] as? String == "archive" && archived["upto"] as? Int == 50 && archived["until"] == nil
              && archived["threads"] as? [String] == ["A"], "archived up to its newest: \(archived)")
        answer("mail_list", list([b, c, d]))

        // a list asked before an archive and answered after it is out of date
        mail.refresh()
        mail.archiveOrRestore("C")
        check(mail.selected == "B", "archiving a thread from the list leaves the open one open")
        answer("mail_list", list([b, c, d]))
        check(mail.threads?.map(\.id) == ["B", "D"], "the older list does not bring it back")
        answer("mail_mark", marked)
        answer("mail_list", list([b, d]))
        check(mail.threads?.map(\.id) == ["B", "D"], "the list after the archive")

        // a refused mark says so; the list is read again to undo what was shown
        mail.markUnread()
        check(mail.selected == nil, "Mark unread closes the thread")
        let unread = answer("mail_mark", nil, err: "refused inside an agent session")
        check(unread["op"] as? String == "unread" && unread["upto"] == nil, "unread names no message: \(unread)")
        check(mail.notice == "Not marked unread: refused inside an agent session", "the refusal is shown: \(mail.notice ?? "none")")
        check(waiting() == ["mail_list"], "the list again after a refusal")
        answer("mail_list", list([b, d]))

        // the open thread is read again only when the list shows it changed, or a reply of the owner's is on its way
        mail.open("D")
        answer("mail_show", show(d, [message("s20")]))
        mail.refresh()
        answer("mail_list", list([b, d]))
        check(waiting().isEmpty, "unchanged: not read again: \(waiting())")
        mail.refresh()
        answer("mail_list", list([b, thread("D", newest: 21, count: 2, last: 210)]))
        check(waiting() == ["mail_show"], "a new message: read again: \(waiting())")
        answer("mail_show", show(thread("D", newest: 21, count: 2, last: 210), [message("s20"), message("s21")]))

        // a reply: its key is kept until the desktop has it, so a retry cannot send it twice
        let draft = mail.draft(for: "D")
        draft.text = "  Yes, merge it.\n"
        mail.send("D")
        let try1 = answer("mail_reply", nil, err: "the connection dropped before it answered")
        check(try1["text"] as? String == "Yes, merge it." && try1["thread"] as? String == "D", "the reply, trimmed: \(try1)")
        check(draft.error == "Not sent: the connection dropped before it answered" && draft.text == "  Yes, merge it.\n",
              "a failed send keeps the text and says why")
        mail.send("D")
        let try2 = answer("mail_reply", ["ok": true, "key": "i9", "status": "sending"])
        check((try2["key"] as? String) != nil && try2["key"] as? String == try1["key"] as? String, "the retry has the same key")
        check(draft.text.isEmpty && draft.key == nil && draft.error == nil, "sent: the box is empty, the key gone")
        check(waiting() == ["mail_show", "mail_list"], "sent: the thread and the list again: \(waiting())")
        let dReplied = thread("D", newest: 21, count: 3, last: 210)
        answer("mail_show", show(dReplied, [message("s20"), message("s21"), message("i9", mine: true, status: "sending")]))
        check(mail.current == "i9", "the thread comes back at the reply")
        answer("mail_list", list([b, dReplied]))
        check(waiting() == ["mail_show"], "a reply on its way: the thread is read again: \(waiting())")
        answer("mail_show", show(dReplied, [message("s20"), message("s21"), message("i9", mine: true, status: "delivered")]))
        mail.refresh()
        answer("mail_list", list([b, dReplied]))
        check(waiting().isEmpty, "delivered: not read again: \(waiting())")
        draft.text = "And the next one."
        mail.send("D")
        let try3 = answer("mail_reply", ["ok": true, "key": "i10", "status": "sending"])
        check(try3["key"] as? String != try1["key"] as? String, "a new reply has a new key")
        answer("mail_show", show(dReplied, []))
        answer("mail_list", list([b, dReplied]))
        draft.text = String(repeating: "x", count: MailModel.maxReply + 1)
        mail.send("D")
        check(waiting().isEmpty && (draft.error ?? "").hasPrefix("Too long to send"), "too long: refused here: \(draft.error ?? "none")")

        // a reply with a file and no text: the file goes as base64, kept until the desktop has it
        draft.text = ""
        let png = Data([0x89, 0x50, 0x4E, 0x47, 1, 2, 3])
        check(mail.drafts.add([.image(png)], to: "D") == .added, "a pasted image is taken")
        let file = draft.files.first
        mail.send("D")
        let fails = answer("mail_reply", nil, err: "ssh: connection closed")
        let sentFiles = fails["files"] as? [[String: String]]
        check(fails["text"] as? String == "" && sentFiles?.count == 1 && sentFiles?.first?["data"] == png.base64EncodedString()
              && (sentFiles?.first?["name"] ?? "").hasSuffix(".png"), "the file as base64, with its name: \(fails)")
        check(draft.files == [file].compactMap { $0 } && draft.shown, "a failed send keeps the file, and the box")
        mail.send("D")
        answer("mail_reply", ["ok": true, "key": "i11", "status": "sending"])
        check(draft.files.isEmpty && !draft.shown, "sent: the file leaves the draft, and the empty box closes")
        check(file.map { !FileManager.default.fileExists(atPath: mail.drafts.url($0).path) } ?? false,
              "and its copy is gone")
        answer("mail_show", show(dReplied, []))
        answer("mail_list", list([b, dReplied]))

        // snooze: up to the newest message, until the time chosen; the thread above opens (it was the last one)
        let wake = Date(timeIntervalSince1970: 1_900_000_000)
        mail.snooze(until: wake)
        check(waiting() == ["mail_mark"] && mail.selected == "B" && mail.shown?.thread.id == "B",
              "snoozed: the next thread opens, kept: \(waiting())")
        let snoozed = answer("mail_mark", marked)
        check(snoozed["op"] as? String == "snooze" && snoozed["upto"] as? Int == 21
              && snoozed["until"] as? Double == wake.timeIntervalSince1970, "snoozed up to its newest, until then: \(snoozed)")
        answer("mail_list", list([b]))

        // Move to Inbox, from the Archived box: no message named, no time
        mail.show(.archived)
        answer("mail_list", list([thread("E", newest: 5, last: 50, box: .archived)]))
        mail.archiveOrRestore("E")
        let back = answer("mail_mark", marked)
        check(back["op"] as? String == "inbox" && back["upto"] == nil && back["until"] == nil, "back to the inbox: \(back)")
        check(mail.threads?.isEmpty == true, "it leaves the Archived box at once")
        answer("mail_list", list([]))
        mail.show(.inbox)
        answer("mail_list", list([b]))

        // syncing with Gmail: the action alone; a list asked before it answers is out of date; the counts in a line
        mail.refresh()
        mail.sync()
        check(mail.syncing, "a spinner while it runs")
        answer("mail_list", list([b, c]))
        check(mail.threads?.map(\.id) == ["B"], "a list asked before the sync answered is not shown")
        let synced = answer("mail_sync", ["ok": true, "archived": 5, "read": 6, "gmail_archived": 1, "gmail_read": 0])
        check(synced.keys.sorted() == ["action"], "nothing but the action: \(synced)")
        check(!mail.syncing && mail.synced == "Synced with Gmail: here 5 archived, 6 read; in Gmail 1 mail archived",
              "the counts that are not 0: \(mail.synced ?? "none")")
        check(MailSync(archived: 0, read: 0, gmailArchived: 0, gmailRead: 0).line == "Synced with Gmail: already in step",
              "nothing to change says so")
        answer("mail_list", list([b, c]))
        check(mail.threads?.map(\.id) == ["B", "C"], "the list after the sync")
        mail.sync()
        answer("mail_sync", nil, err: "Gmail did not answer")
        check(mail.notice == "Not synced with Gmail: Gmail did not answer", "a failed sync says why: \(mail.notice ?? "none")")
        answer("mail_list", list([b]))
        mail.notice = nil
        mail.sync()
        answer("mail_sync", ["ok": false, "error": "no Gmail login is set up on the desktop"])
        check(!mail.syncing && mail.synced == nil && mail.notice == "Not synced with Gmail: no Gmail login is set up on the desktop",
              "the desktop's refusal (no Gmail login) is shown: \(mail.notice ?? "none")")
        answer("mail_list", list([b]))

        // an agent's latest mail, opened from the board: the thread, and the box it is in listed
        mail.reveal("F")
        let archivedF = thread("F", newest: 7, last: 70, box: .archived)
        answer("mail_show", show(archivedF, [message("s7")]))
        let fBox = answer("mail_list", list([archivedF]))
        check(mail.selected == "F" && mail.listing == .archived && fBox["box"] as? String == "archived",
              "opened from the board: its box is listed: \(fBox)")
        check(mail.threads?.map(\.id) == ["F"], "and it is in the list")
        mail.show(.inbox)
        answer("mail_list", list([b]))

        // a search covers every box
        mail.find("  merge it ")
        let search = answer("mail_list", list([dReplied]))
        check(search["box"] as? String == "all" && search["query"] as? String == "merge it", "a search lists every box: \(search)")
        mail.find("")
        check((answer("mail_list", list([b]))["query"]) == nil && mail.listing == .inbox, "an empty search goes back to the box")
        check(waiting().isEmpty && copied.isEmpty, "nothing left unanswered, nothing copied: \(waiting()) \(copied)")
    }

    /// Drafts: kept per thread on disk with their files, marked in the list while they hold something, the box shown
    /// when asked for or holding something; files from a pasteboard, within the mailroom's limits.
    static func drafts(_ check: (Bool, String) -> Void) {
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }            // this test's own folder
        let fm = FileManager.default
        var drafts: MailDrafts? = MailDrafts(folder: folder)
        guard let kept = drafts else { return }
        check(kept.loadProblem == nil && kept.held.isEmpty, "no folder yet: no drafts, no problem")

        // the list's "Draft" follows whether a draft holds something
        let a = kept.draft("A")
        check(!a.shown, "an empty draft is not shown")
        a.text = "  "
        check(kept.held.isEmpty, "blanks are not a draft")
        a.text = "Half a thought"
        check(kept.held == ["A"] && a.shown, "text: a draft, shown")
        a.ask()
        check(a.wantsKeyboard, "Reply asks for the keyboard")
        a.leave()
        check(a.shown, "moving away keeps a box that holds something")

        // files: copied in, refused whole past the limits, a folder refused
        let src = folder.appendingPathComponent("src", isDirectory: true)
        try? fm.createDirectory(at: src, withIntermediateDirectories: true)
        let csv = src.appendingPathComponent("results.csv")
        try? Data("a,b\n".utf8).write(to: csv)
        check(kept.add([.file(csv), .image(Data([1, 2]))], to: "B") == .added, "a file and an image are taken")
        let b = kept.draft("B")
        check(b.files.map(\.name).first == "results.csv" && b.files.map(\.size) == [4, 2] && kept.held.contains("B"),
              "with their names and sizes: \(b.files)")
        check(b.files.allSatisfy { fm.fileExists(atPath: kept.url($0).path) }, "each copied into the folder")
        try? Data("changed".utf8).write(to: csv)
        check((try? kept.data(b.files[0])) == Data("a,b\n".utf8), "the copy is the file as it was added")
        func refused(_ a: MailDrafts.Added, _ words: String) -> Bool {
            if case .refused(let why) = a { return why.contains(words) } else { return false }
        }
        let many = (0..<MailModel.maxFiles - 1).map { _ in Attachments.Item.image(Data([0])) }
        check(refused(kept.add(many, to: "B"), "at most \(MailModel.maxFiles) files") && b.files.count == 2,
              "past the most files: none taken")
        let big = src.appendingPathComponent("film.mov")
        fm.createFile(atPath: big.path, contents: nil)
        try? FileHandle(forWritingTo: big).truncate(atOffset: UInt64(MailModel.maxFileBytes + 1))   // one byte past
        let tooBig = kept.add([.file(big)], to: "B")
        check(tooBig == .tooBig([big]) && b.files.count == 2, "past the most bytes: for the desktop instead: \(tooBig)")
        check(refused(kept.add([.image(Data(count: MailModel.maxFileBytes))], to: "B"), "a reply carries at most"),
              "an image past the most bytes: refused")
        check(refused(kept.add([.file(src)], to: "B"), "is not a file"), "a folder is refused")
        let gone = b.files[1]
        kept.remove(gone, from: "B")
        check(b.files.count == 1 && !fm.fileExists(atPath: kept.url(gone).path), "a file taken out, and its copy")

        // saved and read back by a new MailDrafts, as after a restart; a reply on its way keeps its key
        let c = kept.draft("C")
        c.text = "Sending when the app quit"
        let key = c.begin()?.key
        kept.saveNow()
        drafts = nil
        let again = MailDrafts(folder: folder)
        check(again.loadProblem == nil && again.held == ["A", "B", "C"], "the drafts again: \(again.held)")
        check(again.draft("A").text == "Half a thought" && again.draft("B").files.map(\.name) == ["results.csv"]
              && again.draft("C").key == key && key != nil, "with their text, files and keys")
        again.discard("B")
        check(again.draft("B").files.isEmpty && !again.draft("B").shown && again.held == ["A", "C"]
              && (try? fm.contentsOfDirectory(atPath: folder.path))?.filter({ $0 != "drafts.json" && $0 != "src" }) == [],
              "discarded: nothing left of it, nor of the file taken out earlier")

        // a saved draft whose file's copy has gone keeps the rest, and says so
        check(again.add([.image(Data([9]))], to: "A") == .added, "an image for A")
        if let f = again.draft("A").files.first {
            again.saveNow()
            try? fm.removeItem(at: again.url(f))
            let lost = MailDrafts(folder: folder)
            check(lost.draft("A").files.isEmpty && lost.draft("A").text == "Half a thought"
                  && (lost.loadProblem ?? "").contains(f.name), "a missing copy: left out, and named: \(lost.loadProblem ?? "none")")
        }

        // a drafts.json that cannot be read is moved aside, not written over
        let index = folder.appendingPathComponent("drafts.json")
        try? Data("{not json".utf8).write(to: index)
        let unread = MailDrafts(folder: folder)
        let asides = (try? fm.contentsOfDirectory(atPath: folder.path))?.filter { $0.hasPrefix("drafts.json.unread-") } ?? []
        check(unread.held.isEmpty && asides.count == 1 && (unread.loadProblem ?? "").contains(asides.first ?? "?")
              && !fm.fileExists(atPath: index.path), "an unreadable drafts.json is kept aside: \(unread.loadProblem ?? "none")")
        unread.draft("Z").text = "new"
        unread.saveNow()
        check((try? Data(contentsOf: folder.appendingPathComponent(asides.first ?? "?"))) == Data("{not json".utf8),
              "and a save leaves it as it was")

        // one that can be neither read nor moved aside (its aside names taken) is never saved over, and the drafts
        // are saved once it has gone
        let now = Int(Date().timeIntervalSince1970)
        for t in now...now + 2 {
            try? fm.createDirectory(at: folder.appendingPathComponent("drafts.json.unread-\(t)"), withIntermediateDirectories: true)
        }
        try? Data("{not json".utf8).write(to: index)
        let stuck = MailDrafts(folder: folder)
        var told: [String] = []
        stuck.problem = { told.append($0) }
        check((stuck.loadProblem ?? "").contains("could not be moved aside"),
              "an unreadable drafts.json that cannot be moved aside: said so: \(stuck.loadProblem ?? "none")")
        stuck.draft("S").text = "kept back"
        stuck.saveNow()
        check((try? Data(contentsOf: index)) == Data("{not json".utf8) && told.count == 1,
              "and a save leaves it as it was, saying why: \(told)")
        try? fm.removeItem(at: index)                        // the owner moves it out of the way
        stuck.saveNow()
        check(MailDrafts(folder: folder).draft("S").text == "kept back", "once it has gone, the drafts are saved")

        // what a pasteboard gives: files, else an image with no text; text is typed
        let pb = NSPasteboard(name: NSPasteboard.Name("agentboard-mailtest-\(UUID().uuidString)"))
        defer { pb.releaseGlobally() }
        pb.clearContents()
        pb.setData(Data([0x89, 0x50]), forType: .png)
        check(Attachments.holds(pb) && Attachments.items(pb) == [.image(Data([0x89, 0x50]))], "an image alone is a file")
        pb.clearContents()
        pb.setString("words", forType: .string)
        pb.setData(Data([0x89, 0x50]), forType: .png)
        check(!Attachments.holds(pb) && Attachments.items(pb).isEmpty, "an image beside text is text")
        pb.clearContents()
        pb.writeObjects([csv as NSURL])
        check(Attachments.holds(pb) && Attachments.items(pb) == [.file(csv)], "a file copied in the Finder")

        // the model: Reply opens the open thread's box, files on a pasteboard go into it, an empty box closes when
        // the owner moves on
        var asked: [String] = []
        var uploads: [(file: URL, done: @MainActor (String?, String?) -> Void)] = []
        /// The oldest upload asked for ends as the desktop would end it; none asked for fails the test, not the run.
        func finishUpload(_ path: String?, _ why: String?) {
            guard !uploads.isEmpty else { return check(false, "no upload was asked for") }
            uploads.removeFirst().done(path, why)
        }
        let modelFolder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: modelFolder) }     // this test's own folder
        let mail = MailModel(call: { r, _ in asked.append(r["action"] as? String ?? "?") },
                             upload: { file, done in uploads.append((file, done)) },
                             ahead: 0, cacheBytes: 1 << 20, draftsFolder: modelFolder)
        mail.open("X")
        mail.perform(.reply)
        check(mail.draft(for: "X").shown, "r: the box")
        mail.open("Y")
        check(!mail.draft(for: "X").shown, "an empty box closes when another thread opens")
        pb.clearContents()
        pb.setData(Data([7]), forType: .png)
        check(mail.attach(pb) && mail.draft(for: "Y").files.count == 1 && mail.draft(for: "Y").shown, "a paste or drop: in the box")
        mail.open("X")
        check(mail.draft(for: "Y").shown && mail.drafts.held == ["Y"], "a box with a file stays, marked a draft")
        mail.drafts.discard("Y")
        pb.clearContents()
        pb.setString("just text", forType: .string)
        check(!mail.attach(pb), "text is not taken")

        // a file too big for a reply: the box asks; copied to the desktop, its path joins the text, and the reply
        // waits for it; one that fails says why
        pb.clearContents()
        pb.writeObjects([big as NSURL])
        check(mail.attach(pb) && mail.draft(for: "X").oversize == [big] && mail.draft(for: "X").files.isEmpty,
              "too big: the box asks what to do")
        let x = mail.draft(for: "X")
        x.text = "The film:"
        mail.copyToDesktop(x.oversize, for: "X")
        check(uploads.map(\.file) == [big] && x.copying == ["film.mov"] && x.begin() == nil, "copying: the reply waits")
        finishUpload("/home/owner/.local/state/agent-board/mail-files/2026-11-18/film.mov", nil)
        check(x.copying.isEmpty && x.text == "The film:\n\nToo big to attach, so copied to the desktop: "
              + "/home/owner/.local/state/agent-board/mail-files/2026-11-18/film.mov (25.0M)", "its path in the reply: \(x.text)")
        mail.copyToDesktop([big], for: "X")
        finishUpload(nil, "ssh: connection refused")
        check(x.error == "film.mov not copied to the desktop: ssh: connection refused" && x.copying.isEmpty,
              "a copy that fails says why: \(x.error ?? "none")")
        check(!asked.contains("mail_reply"), "nothing sent meanwhile: \(asked)")
    }

    /// An option picked in a mail (MailWidgets): the reply's first line, replacing the line an earlier pick put there
    /// and keeping what the owner typed; the box opens and nothing is sent. Widgets as mail_show gives them. (The choice
    /// lines themselves come from widgets.js: --selftest-widgets.)
    static func choices(_ check: (Bool, String) -> Void) {
        let d = Draft(text: "", files: [], key: nil)
        d.choose("Option 2: Ship it now")
        check(d.text == "Option 2: Ship it now" && d.shown && d.wantsKeyboard, "a pick opens the box with it: \(d.text)")
        d.text += "\nbut keep the branch"
        d.choose("Option 3: Drop it")
        check(d.text == "Option 3: Drop it\nbut keep the branch",
              "another pick replaces its line and keeps your words: \(d.text)")
        d.choose("Options 1 and 3: Wait for review; Drop it")
        check(d.text == "Options 1 and 3: Wait for review; Drop it\nbut keep the branch",
              "several: \(d.text)")
        d.choose("")
        check(d.text == "but keep the branch" && d.choice == nil, "nothing picked: only your words: \(d.text)")
        let typed = Draft(text: "my words first", files: [], key: nil)
        typed.choose("Option 1: Wait for review")
        check(typed.text == "Option 1: Wait for review\nmy words first", "words typed before the pick stay: \(typed.text)")
        typed.text = "Option 1: Wait for review, I mean\nmy words first"            // the owner edited the pick's line
        typed.choose("Option 2: Ship it now")
        check(typed.text == "Option 2: Ship it now\nOption 1: Wait for review, I mean\nmy words first",
              "a pick line you edited is yours, and stays: \(typed.text)")
        typed.clear()
        typed.choose("Option 1: Wait for review")
        check(typed.text == "Option 1: Wait for review", "after Discard a pick starts afresh: \(typed.text)")

        var asked: [String] = []
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        let mail = MailModel(call: { request, _ in asked.append(request["action"] as? String ?? "?") },
                             upload: { _, _ in }, ahead: 0, cacheBytes: 1 << 20, draftsFolder: folder)
        mail.choose("Option 2: Ship it now", for: "W")
        let w = mail.draft(for: "W")
        check(w.text == "Option 2: Ship it now" && w.shown && w.wantsKeyboard, "the thread's reply box opens with it")
        check(asked.isEmpty, "nothing is sent or asked for: \(asked)")

        // widgets as mail_show gives them: on an agent's messages, none when missing, in order, the spec unchanged
        let answer: [String: Any] = ["ok": true, "document": "<p>W</p>", "thread": thread("W", newest: 3, last: 30),
            "messages": [["key": "s2", "from": "mailtab", "mine": false, "ts": 1, "unread": false, "status": NSNull()],
                         ["key": "s3", "from": "mailtab", "mine": false, "ts": 2, "unread": false, "status": NSNull(),
                          "widgets": [["id": "s3-0", "kind": "choices",
                                       "spec": ["question": "Which?", "options": [["label": "A"], ["label": "B", "recommended": true]]]],
                                      ["id": "s3-1", "kind": "plot",
                                       "spec": ["title": "T", "kind": "line", "x": ["future_key": "x_label kept"], "y": ["min": 0],
                                                "series": [["name": "a", "x": [1, 2], "y": [0.5, NSNull()]]]]]]],
                         ["key": "s4", "from": "mailtab", "mine": false, "ts": 3, "unread": false, "status": NSNull(),
                          "widgets": [["id": "s4-0", "kind": "plot"]]]]]
        let (shown, why) = MailShow.decode(answer, nil)
        check(shown?.messages.first?.widgets == nil && shown?.widgets.map(\.id) == ["s3-0", "s3-1"],
              "widgets decode, none when missing: \(why ?? "")")
        if let s = shown {
            check(s.widgets.last?.spec == .object(["title": .string("T"), "kind": .string("line"),
                                                   "x": .object(["future_key": .string("x_label kept")]),
                                                   "y": .object(["min": .number(0)]),
                                                   "series": .array([.object(["name": .string("a"),
                                                                              "x": .array([.number(1), .number(2)]),
                                                                              "y": .array([.number(0.5), .null])])])]),
                  "a spec's keys reach widgets.js as written, snake_case too: \(String(describing: s.widgets.last?.spec))")
            check(s.messages.last?.widgets == nil && s.dropped.isEmpty && s.widgetProblems.count == 1
                  && s.widgetProblems[0].hasPrefix("s4: "),
                  "widgets that do not decode leave their message plain, and say so apart: \(s.widgetProblems)")
            let again = try? JSONDecoder().decode([MailWidget].self, from: Data(MailWidgets.json(s.widgets).utf8))
            check(again == s.widgets, "a widget's spec reaches widgets.js as it came")
            check((try? MailWidgets.json([])) == "[]", "no widgets: []")
        }
        // opened, such a thread says which mails' options or plots could not be drawn, not that they were not understood
        var showing: [(request: [String: Any], done: @MainActor ([String: Any]?, String?) -> Void)] = []
        let viewFolder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: viewFolder) }      // this test's own folder
        let viewer = MailModel(call: { request, done in showing.append((request, done)) }, upload: { _, _ in },
                               ahead: 0, cacheBytes: 1 << 20, draftsFolder: viewFolder)
        viewer.open("W")
        if let i = showing.firstIndex(where: { ($0.request["action"] as? String) == "mail_show" }) {
            showing.remove(at: i).done(answer, nil)
        }
        check(viewer.shown?.thread.id == "W" && viewer.threadError == nil
              && (viewer.notice ?? "").hasPrefix("Options or plots in 1 mail could not be drawn, shown as plain text: s4: "),
              "the notice: \(viewer.notice ?? "none"); the error: \(viewer.threadError ?? "none")")
    }

    /// Reading ahead, the kept threads, ← → and paging, with one thread read ahead on each side of the open one.
    static func ahead(_ check: (Bool, String) -> Void) {
        var asked: [(request: [String: Any], done: @MainActor ([String: Any]?, String?) -> Void)] = []
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        var copied: [URL] = []                                             // nothing is copied to the desktop here
        let mail = MailModel(call: { request, done in asked.append((request, done)) },
                             upload: { file, _ in copied.append(file) },
                             ahead: 1, cacheBytes: 1 << 20, draftsFolder: folder)
        /// The threads mail_show is asked for, in the order asked.
        func shows() -> [String] {
            asked.compactMap { ($0.request["action"] as? String) == "mail_show" ? $0.request["thread"] as? String : nil }
        }
        /// Answer the oldest request for `action` (with `newest`, the newest), on `thread` when given.
        func answer(_ action: String, thread id: String?, newest: Bool, _ obj: [String: Any]) {
            let matching = asked.indices.filter {
                (asked[$0].request["action"] as? String) == action && (id == nil || (asked[$0].request["thread"] as? String) == id)
            }
            guard let i = newest ? matching.last : matching.first else {
                check(false, "no \(action) request for \(id ?? "any thread") to answer")
                return
            }
            asked.remove(at: i).done(obj, nil)
        }
        func list(_ threads: [[String: Any]]) -> [String: Any] {
            ["ok": true, "threads": threads, "counts": ["inbox": threads.count, "unread": 0, "snoozed": 0]]
        }
        func show(_ t: [String: Any], _ keys: [String], _ doc: String) -> [String: Any] {
            ["ok": true, "thread": t, "document": doc,
             "messages": keys.map { ["key": $0, "from": "mailtab", "mine": false, "ts": 1, "unread": false, "status": NSNull()] }]
        }
        let a = thread("A", newest: 9, last: 90), b = thread("B", newest: 8, last: 80), c = thread("C", newest: 7, last: 70)
        let d = thread("D", newest: 6, last: 60)

        mail.refresh()
        answer("mail_list", thread: nil, newest: false, list([a, b, c, d]))
        check(shows() == ["A", "B"], "with none open, the top threads are read ahead: \(shows())")
        mail.open("A")
        check(shows() == ["A", "B"] && mail.shown == nil, "opening one already on its way asks nothing more")
        answer("mail_show", thread: "A", newest: false, show(a, ["s1", "s2", "s3"], "<p>A</p>"))
        check(mail.shown?.thread.id == "A" && mail.current == "s3", "it shows when that answer comes, at its newest mail")

        // → and ← through the thread's mails, stopping at either end; Space and ⇧Space each ask for a page
        mail.perform(.message(1))
        check(mail.current == "s3", "→ at the newest stays")
        mail.perform(.message(-1))
        mail.perform(.message(-1))
        check(mail.current == "s1", "← twice: two older")
        mail.perform(.message(-1))
        check(mail.current == "s1", "← at the oldest stays")
        mail.perform(.page(1))
        mail.perform(.page(-1))
        check(mail.page == MailPage(n: 2, by: -1), "a page down, then one up: \(String(describing: mail.page))")

        // ↓ to a thread read ahead: at once, and the next one is read ahead
        answer("mail_show", thread: "B", newest: false, show(b, ["s4"], "<p>B</p>"))
        mail.perform(.thread(1))
        check(mail.shown?.thread.id == "B" && mail.current == "s4", "B shows at once")
        check(shows() == ["C"], "and C is read ahead (A is kept): \(shows())")

        // C changes in the list while its read-ahead is on its way: ↓ waits for that answer and shows it, then asks
        // once more and shows the newer
        let c2 = thread("C", newest: 10, count: 2, last: 100)
        mail.refresh()
        answer("mail_list", thread: nil, newest: false, list([a, b, c2, d]))
        check(shows() == ["C"], "the read-ahead does not ask again for a thread on its way: \(shows())")
        mail.perform(.thread(1))
        check(mail.shown?.thread.id == "B" && shows() == ["C", "D"], "↓ waits for C; D is read ahead: \(shows())")
        answer("mail_show", thread: "C", newest: false, show(c, ["s5"], "<p>C</p>"))
        check(mail.shown?.document == "<p>C</p>" && shows() == ["D", "C"], "that answer shows, and C is asked for again: \(shows())")
        answer("mail_show", thread: "C", newest: false, show(c2, ["s5", "s10"], "<p>C2</p>"))
        check(mail.shown?.document == "<p>C2</p>" && mail.current == "s5" && shows() == ["D"],
              "then the newer, at the mail being read, asked once only: \(shows())")
        answer("mail_show", thread: "D", newest: false, show(d, ["s6"], "<p>D</p>"))

        // C changes in the list while open: it is read afresh; a second poll before that answer asks again, and the
        // first answer, coming last, changes nothing
        let c3 = thread("C", newest: 11, count: 3, last: 110)
        mail.refresh()
        answer("mail_list", thread: nil, newest: false, list([c3, a, b, d]))
        mail.refresh()
        answer("mail_list", thread: nil, newest: false, list([c3, a, b, d]))
        check(shows() == ["C", "C"], "each poll that finds it changed asks again: \(shows())")
        answer("mail_show", thread: "C", newest: true, show(c3, ["s5", "s10", "s11"], "<p>C new</p>"))
        check(mail.shown?.document == "<p>C new</p>" && mail.current == "s5", "the newer answer shows, at the mail being read")
        answer("mail_show", thread: "C", newest: false, show(c3, ["s5"], "<p>C old</p>"))
        check(mail.shown?.document == "<p>C new</p>", "the older answer, coming after, is not shown")
        mail.perform(.thread(1))
        mail.perform(.thread(-1))
        check(mail.shown?.document == "<p>C new</p>" && asked.isEmpty, "nor kept: C again shows the newer, asking nothing")

        // a thread the list no longer vouches for is read again when opened
        mail.refresh()
        answer("mail_list", thread: nil, newest: false, list([c3, a, thread("B", newest: 12, count: 2, last: 120), d]))
        mail.open("B")
        check(mail.shown?.thread.id == "C" && shows() == ["B"], "B changed since it was kept: asked for, not shown from the cache")
        answer("mail_show", thread: "B", newest: false, show(thread("B", newest: 12, count: 2, last: 120), ["s4", "s12"], "<p>B2</p>"))
        check(mail.shown?.document == "<p>B2</p>" && asked.isEmpty, "then shown: \(shows())")
        check(copied.isEmpty, "nothing copied to the desktop: \(copied)")
    }
}
