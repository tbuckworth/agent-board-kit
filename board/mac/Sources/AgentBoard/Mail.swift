import AppKit
import Carbon
import Combine
import SwiftUI
import WebKit

// The Mail tab: mail between the owner and their agents, read from the mailroom on the desktop, which keeps the
// messages and the owner's flags (read, archived, snoozed) as a mail server would (`agent-mail mailbox`, through the
// board's mail_* actions). The tab lists a box or a search, shows one thread as the HTML document the desktop makes of
// it (MailHTML.swift), and sends the owner's marks and replies back, with files. The owner's unsent replies are kept
// on the desktop, with a write-ahead copy on the Mac (MailDrafts.swift).

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

/// An agent as the desktop keys it: its CLI and session id (Agent.cli and Agent.id).
struct AgentKey: Decodable, Hashable {
    var cli: String
    var id: String
}

extension Agent {
    var key: AgentKey { AgentKey(cli: cli, id: id) }
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
    /// The board's agents that wrote in it (mail_list with "sessions"); nil in mail_show's answer, or from a desktop
    /// that does not send them.
    var sessions: [AgentKey]? = nil
    /// Its group, also with "sessions" (the desktop's mailbox.add_groups): the board group and sub-group of the agent
    /// of its newest agent mail (nil for none: Ungrouped), which hovering over its row names (MailRow.groupName), the
    /// group's colour, and the sub-group's shade, else the group's colour, for the strip down the row's left edge. nil
    /// in mail_show's answer, or when the desktop could not say (MailList.groupsError).
    var group: String? = nil
    var subgroup: String? = nil
    var colour: HexColour? = nil
    var shade: HexColour? = nil
    /// Its fast mode while it is on (MailFast); nil when off, or from a mailroom that has none.
    var fast: MailFast? = nil
    /// Its agent's Researcher setting (MailResearcher); nil when the desktop sends none (the researcher controls are
    /// off there, or the thread has no agent it can route to).
    var researcher: MailResearcher? = nil
}

/// A thread's Researcher switch (mail_researcher; the mailroom's researcher controls, which drive the `researcher`
/// Claude Code plugin): whether it is on for the thread's routed `agent`, the routing `tag` the switch was drawn for
/// (sent back as expected_tag, so a click never lands on another agent), and how its delivery to the agent got on,
/// with why it failed (such as the plugin missing where the agent runs).
struct MailResearcher: Decodable, Equatable {
    let on: Bool
    let agent: String
    let tag: String?
    let delivery: String?
    let deliveryDetail: String?

    var deliveryNote: String? {
        switch delivery {
        case "pending": return "Delivery pending."
        case "queued": return "Delivery queued."
        case "accepted": return "Awaiting agent confirmation."
        case "unavailable":
            if let detail = deliveryDetail?.trimmingCharacters(in: .whitespacesAndNewlines), !detail.isEmpty {
                return "Delivery failed. " + detail
            }
            return "Delivery failed. Agent unavailable."
        case "error": return "Delivery failed."
        default: return nil
        }
    }
}

/// A thread's fast mode, for quick back-and-forths with its agent (mail_fast; the mailroom's `mailbox fast`): while it
/// is on, the agent answers the owner there in a few plain lines at low effort, and its replies in the thread skip the
/// review before sending. It ends when the owner switches it off, or once `until` passes with no message of the owner's
/// in the thread (each one moves it on), and the agent's effort goes back. Only `until` must be there: the rest are
/// shown when they are.
struct MailFast: Decodable, Equatable {
    var since: Double?            // when the owner switched it on
    var until: Double             // when it ends unless the owner writes in the thread again
    var minutes: Double?          // its length
    var agent: String?            // the agent it applies to
    var effort: String?           // low (lowered), lowering (the mailroom's next poll lowers it, or it waits), or
                                  // unchanged (it changes only how the agent answers)
    var was: String?              // the effort it goes back to
    var note: String?             // one plain sentence on what it does to the agent's effort, and why when unchanged

    /// Minutes left at `now`, rounded up and at most its length; 0 once `until` has passed.
    func left(at now: Date) -> Int {
        let seconds = until - now.timeIntervalSince1970
        guard seconds > 0 else { return 0 }
        var left = (seconds / 60).rounded(.up)
        if let m = minutes, m > 0 { left = min(left, m.rounded(.up)) }
        return Int(left)
    }

    /// "Fast · 7 min"; "Fast · ending" once `until` has passed, until the desktop says it has ended (within its poll,
    /// 30 s). The phone's page has the same rule (fastLabel in web/mail/app.js): change both together.
    func label(at now: Date) -> String {
        let n = left(at: now)
        return n > 0 ? "Fast · \(n) min" : "Fast · ending"
    }
}

struct MailCounts: Decodable, Equatable {
    var inbox: Int
    var unread: Int
    var snoozed: Int
}

/// mail_list's answer. A thread that does not decode is left out, and why is kept in `dropped`. `sessionsError`: why
/// the desktop could not say which agents wrote in each thread; `groupsError`: why it could not say their groups.
struct MailList: Decodable {
    var threads: [MailboxThread]
    var counts: MailCounts?
    var sessionsError: String?
    var groupsError: String?
    var dropped: [String]

    enum CodingKeys: String, CodingKey { case threads, counts, sessionsError, groupsError }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard c.contains(.threads) else {
            throw DecodingError.keyNotFound(CodingKeys.threads, .init(codingPath: c.codingPath, debugDescription: "no threads"))
        }
        var why: [String] = []
        threads = c.lossyArray(MailboxThread.self, forKey: .threads, failures: &why)
        counts = c.lossyValue(MailCounts.self, forKey: .counts, failures: &why)
        sessionsError = c.lossyValue(String.self, forKey: .sessionsError, failures: &why)
        groupsError = c.lossyValue(String.self, forKey: .groupsError, failures: &why)
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
    /// Which of its messages did not decode, said above the thread; nil when all did.
    var droppedLine: String? {
        dropped.first.map {
            "\(dropped.count) message\(dropped.count == 1 ? "" : "s") not understood (the document still has them): \($0)"
        }
    }
    /// Which of its mails' options or plots could not be drawn, said in the tab's notice; nil when all could.
    var widgetLine: String? {
        widgetProblems.first.map {
            let n = widgetProblems.count
            return "Options or plots in \(n) mail\(n == 1 ? "" : "s") could not be drawn, shown as plain text: \($0)"
        }
    }

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

/// mail_forward's answer: the draft it made in the owner's Gmail, and the draft's message, which Gmail's page opens.
struct MailForwarded: Decodable {
    var draft: String
    var message: String
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

    /// What the Undo bar says once it is done: "Archived", and for a snooze its end, `until`, as MailFmt.at gives it
    /// from `now`. The phone's page says the same (done in web/mail/app.js): change both together.
    func done(until: Double?, now: Date) -> String {
        switch self {
        case .read: return "Marked read"
        case .unread: return "Marked unread"
        case .archive: return "Archived"
        case .inbox: return "Moved to the inbox"
        case .snooze: return "Snoozed" + (until.map { " until " + MailFmt.at($0, now: now) } ?? "")
        }
    }

    /// Whether it names the newest agent mail the owner was shown (mail_mark's `upto`): read, archive and snooze, the
    /// desktop's UPTO_OPS.
    var reaches: Bool { self == .read || self == .archive || self == .snooze }
}

/// One mail_mark of a thread: the op, the newest agent mail it reaches (MailOp.reaches) and a snooze's end.
struct MailMark: Equatable {
    var op: MailOp
    var upto: Int? = nil
    var until: Double? = nil
}

/// A change the owner made to a thread, which Undo puts back (MailModel.undo): what the bar says of it, the thread's row
/// as the tab had it before and as the desktop answered it after, whether it was the open thread and the mail being
/// read in it, and whether it had a window of its own (MailModel.popouts). A Discard of the thread's draft is
/// `discarded`, its rows the thread's as it was.
struct MailUndo {
    var line: String
    var before: MailboxThread
    var after: MailboxThread
    var open: Bool
    var reading: String?
    var popped: Bool
    var discarded: Discarded? = nil

    /// A draft discarded: the desktop's version of it, which Undo brings back (nil: kept on this Mac alone), and the
    /// draft as it was.
    struct Discarded {
        var version: String?
        var draft: DraftSnapshot
    }
}

/// A thread as one place reads it, the tab (its open thread) or a window of the thread's own (MailModel.popouts):
/// what MailThreadView shows.
struct MailReading {
    var id: String                // the thread
    var shown: MailShow?          // its answer; in the tab, while another thread loads, the one before
    var current: String?          // the mail being read: outlined, and ← → step from it
    var page: MailPage?           // the last Space or ⇧Space
    var error: String?            // why it is not shown, or which of its messages were not understood
}

// MARK: - rules

/// What the tab shows before the desktop answers, and when it asks again. The phone's page has the same rules
/// (changed, applying, behind and stale in web/mail/app.js): change both together.
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

    /// The row Undo puts back for `before` at time `now`: as it was, or in the inbox when it was snoozed until a time
    /// now past, where it would be by now (as on the phone's page).
    static func undone(_ before: MailboxThread, now: Double) -> MailboxThread {
        guard before.box == MailBox.snoozed.rawValue, let until = before.snoozeUntil, until <= now else { return before }
        return changed(before, .inbox, until: nil)
    }

    /// The marks that put a thread back as it was (`before`, the row the tab had) from where a change left it (`after`,
    /// the desktop's answer): first its box, the inbox, Archived up to the newest mail it had, or Snoozed until the
    /// same time (so mail come since still brings it back, as after any archive or snooze); then read up to that mail,
    /// or unread. None when the change left it as it was; nil when `before` is in no box a mark can put it back in.
    static func undoing(_ before: MailboxThread, from after: MailboxThread) -> [MailMark]? {
        var marks: [MailMark] = []
        if before.box != after.box || before.snoozeUntil != after.snoozeUntil {
            switch MailBox(rawValue: before.box) {
            case .inbox?: marks.append(MailMark(op: .inbox))
            case .archived?: marks.append(MailMark(op: .archive, upto: before.newest))
            case .snoozed?: marks.append(MailMark(op: .snooze, upto: before.newest, until: before.snoozeUntil))
            case .all?, nil: return nil
            }
        }
        if before.unread != after.unread {
            marks.append(before.unread ? MailMark(op: .unread) : MailMark(op: .read, upto: before.newest))
        }
        return marks
    }

    /// The list with `row` put back as it was: in its place when listed, or out of it when it does not belong to the
    /// box listed; else, in a box it belongs to, in its place by time (the list's order, newest first). A search or
    /// All (`box` nil or .all) lists every box, so a change never took it out of them.
    static func restoring(_ row: MailboxThread, in list: [MailboxThread], box: MailBox?) -> [MailboxThread] {
        var out = list
        let belongs = box == nil || box == .all || row.box == box?.rawValue
        if let i = out.firstIndex(where: { $0.id == row.id }) {
            if belongs { out[i] = row } else { out.remove(at: i) }
        } else if belongs, box != nil, box != .all {
            out.insert(row, at: out.firstIndex { $0.last < row.last } ?? out.endIndex)
        }
        return out
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

    /// The message a place shows `s` at when no message is asked for, `was` being what it showed before and `reading`
    /// the mail being read in it: the same thread read again stays at that mail while it is there and nothing in the
    /// thread is unread; otherwise MailRules.focus.
    static func opening(_ s: MailShow, was: MailShow?, reading: String?) -> String? {
        let stay = was?.thread.id == s.thread.id && !s.thread.unread && s.messages.contains { $0.key == reading }
        return stay ? reading : focus(s.messages, unread: s.thread.unread)
    }

    /// The message a thread opened from a tile opens at: the agent's latest mail to the owner, the agent mail sent at `at`
    /// (the tile's lastOut, which is the mailroom's own time for it), else the thread's newest message.
    static func revealed(_ messages: [MailboxMessage], at: Double?) -> String? {
        at.flatMap { ts in messages.last(where: { !$0.mine && $0.ts == ts })?.key } ?? messages.last?.key
    }

    /// The state of the thread's row's dot: the foremost (AgentState.foremost) of the states of the board's agents at
    /// work (`working`) that wrote in it, nil (no dot) when none did.
    static func working(_ t: MailboxThread, _ working: [AgentKey: AgentState]) -> AgentState? {
        AgentState.foremost((t.sessions ?? []).compactMap { working[$0] })
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
    case unread               // mark the open thread unread, which closes it (the Unread button)
    case snooze               // the Snooze menu, with the keyboard in it
    case undo                 // put back the last change on the Undo bar
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
    /// thread; Space and ⇧Space a screen down and up; e archive; u mark unread (as the Unread button, which closes the
    /// thread); s the Snooze menu; z or ⌘Z undo, as in Gmail; r reply; ⌘V files or an image into the reply; and while
    /// the owner types (`typing`, a text box has the keyboard), only Esc, which gives the keyboard back (the box takes its
    /// own ⌘V and ⌘Z). With a `list` (the tab) ↓ j ↑ k step through it; without one (a thread's own window) they are
    /// not commands, so the arrows scroll the document. nil: the key is not the tab's (no other ⌘, nor ⌥ or ⌃).
    static func command(_ chars: String, keyCode: UInt16, flags: NSEvent.ModifierFlags, typing: Bool,
                        list: Bool) -> MailKey? {
        let mods = flags.intersection([.command, .option, .control, .shift])
        if typing { return keyCode == UInt16(kVK_Escape) && mods.isEmpty ? .release : nil }
        if mods == .shift { return chars == " " ? .page(-1) : nil }
        if mods == .command {
            switch Int(keyCode) {
            case kVK_ANSI_V: return .paste
            case kVK_ANSI_Z: return .undo
            default: return nil
            }
        }
        guard mods.isEmpty else { return nil }
        switch chars {
        case down, "j": return list ? .thread(1) : nil
        case up, "k": return list ? .thread(-1) : nil
        case right: return .message(1)
        case left: return .message(-1)
        case " ": return .page(1)
        case "e": return .archive
        case "u": return .unread
        case "s": return .snooze
        case "z": return .undo
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
    /// at the same moment as an earlier one is left out. The phone's page has the same rule (web/mail/snooze.js): change
    /// both together.
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

    /// The Snooze menu at `now`, the Snooze button's and s's alike: each preset as "Tomorrow · Thu 08:00", then Pick
    /// date & time (no date), after a line.
    static func items(now: Date, calendar cal: Calendar = .current) -> [SnoozeItem] {
        presets(now: now, calendar: cal).map {
            SnoozeItem(title: $0.label + " · " + MailFmt.at($0.date.timeIntervalSince1970, now: now, calendar: cal),
                       date: $0.date)
        } + [SnoozeItem(title: "Pick date & time…", date: nil)]
    }

    /// `items` as one of AppKit's menus, for s (MailModel.popUp): a line before Pick date & time; an item with a date
    /// snoozes until then (`snooze`), and Pick date & time asks for one (`pick`).
    @MainActor
    static func menu(_ items: [SnoozeItem], snooze: @escaping @MainActor (Date) -> Void,
                     pick: @escaping @MainActor () -> Void) -> NSMenu {
        let menu = NSMenu()
        menu.autoenablesItems = false
        for item in items {
            if item.date == nil { menu.addItem(.separator()) }
            let action = ClosureAction { if let d = item.date { snooze(d) } else { pick() } }
            let m = NSMenuItem(title: item.title, action: #selector(ClosureAction.run), keyEquivalent: "")
            m.target = action
            m.representedObject = action                  // the item keeps its action: a target is held weakly
            menu.addItem(m)
        }
        return menu
    }
}

/// One item of the Snooze menu (Snooze.items): what it says, and when it snoozes until; nil for Pick date & time.
struct SnoozeItem: Equatable {
    var title: String
    var date: Date?
}

/// An AppKit menu item's or button's action as a closure: its target, which something must keep, as a target is
/// weak (a menu item keeps it as its representedObject; Dialogs.filesRow in its view).
@MainActor
final class ClosureAction: NSObject {
    private let action: @MainActor () -> Void
    init(_ action: @escaping @MainActor () -> Void) { self.action = action }
    @objc func run() { action() }
}

enum MailFmt {
    /// The list's time: "14:02" today, "Yesterday", a weekday within the last week, "13 Nov" this year, "13 Nov 2025"
    /// before. The phone's page has the same rule (listTime in web/mail/app.js): change both together.
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

    /// A time ahead, such as a snooze's: "17:00" today, "Sat 08:00" within six days, else "Mon 30 Nov 08:00". The
    /// phone's page has the same rule (aheadTime in web/mail/app.js): change both together.
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

/// The Mail tab's state, kept while the tab is away so a draft or the open thread survives a look at the board, and
/// that of the threads popped out into windows of their own (`popouts`, which MailWindows follows). It polls the list
/// every 15 s while the tab and the window are on screen, or a thread has a window, and reads a thread being read
/// again only when the list shows that it changed or a reply of the owner's in it is still on its way. Threads it has read
/// are kept (MailCache), and the `ahead` threads on each side of the open one (or the top ones, with none open) are
/// read before they are asked for, so stepping through the list shows each at once. A thread read in the tab and in
/// its window at once is read from the desktop once for both, and has one draft (MailDrafts), which both reply boxes
/// and both documents' cards and highlights show.
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
    /// The app's: seconds the Undo bar offers a change for, after the last change or Undo.
    nonisolated static let appUndoSeconds: TimeInterval = 10
    /// The app's: seconds an unread thread is on screen, in the tab or its own window, before it is marked read.
    nonisolated static let appReadSeconds: TimeInterval = 1

    @Published private(set) var box = MailBox.inbox
    @Published private(set) var search = ""                    // the words searched for; "" lists the box
    @Published private(set) var threads: [MailboxThread]?      // nil until the list has come
    @Published private(set) var counts: MailCounts?
    @Published private(set) var listError: String?
    @Published private(set) var sessionsError: String?         // why the list's rows have no busy dots (MailList)
    @Published private(set) var groupsError: String?           // why they have no group colours (MailList)
    /// The tab's open thread as it reads it (MailReading); nil with none open.
    @Published private(set) var tab: MailReading?
    @Published var notice: String?                              // the last thing that failed: a mark, a link, a file
    @Published private(set) var fastAsked: String?              // the thread whose fast mode is being switched
    @Published private(set) var researcherAsked: String?
    @Published private(set) var researcherUnconfirmed: Set<String> = []
    @Published private(set) var now = Date()                    // the list's times count from it; set with each list
    /// The changes Undo can put back, oldest first. The bar offers the newest, and they all go `undoSeconds` after the
    /// last change or Undo.
    @Published private(set) var undos: [MailUndo] = []
    /// Why the last Undo failed: the bar says it until closed, or until another change takes its place.
    @Published private(set) var undoFailure: String?
    /// Changes the owner made and Undos that the desktop has not answered. Undo does nothing while there are any (the bar's
    /// button is greyed), so it never overtakes the change it undoes, nor another Undo; z and ⌘Z are kept until then
    /// (pressUndo).
    @Published private(set) var undoWaiting = 0
    /// The threads popped out into windows of their own, by thread: each window shows its entry (MailWindows makes and
    /// closes the windows to match).
    @Published private(set) var popouts: [String: MailReading] = [:]
    /// The tab's reading, part by part: the open thread, its answer (while another loads, the one before), the message
    /// being read (outlined, and ← → step from it), the last Space or ⇧Space, and why it is not shown.
    var selected: String? { tab?.id }
    var shown: MailShow? { tab?.shown }
    var current: String? { tab?.current }
    var page: MailPage? { tab?.page }
    var threadError: String? { tab?.error }
    /// A thread popped out again while its window is open: the window comes to the front.
    let raise = PassthroughSubject<String, Never>()
    /// s (askSnooze): the Snooze menu asked for in a place, the tab (nil) or a thread's own window (its id), whose
    /// ThreadActions pops it up at its Snooze button.
    let snoozeAsked = PassthroughSubject<String?, Never>()
    /// How the Snooze menu pops up at its button (`view`, behind it): AppKit's menu, under the button, which takes the
    /// keyboard (↑ ↓ move, Enter picks, Esc closes it); a test's own in --selftest-popout, which keeps it off screen.
    var popUp: (NSMenu, NSView) -> Void = { menu, view in
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: view.isFlipped ? view.bounds.maxY + 2 : -2), in: view)
    }
    /// A thread opened from a tile, and its agent's latest mail's time: open it there and list its box once it loads.
    private var revealing: (id: String, at: Double?)?

    /// The owner's unsent replies, per thread; only its reply box watches each, so typing redraws nothing else.
    let drafts: MailDrafts
    /// The owner's Google account (the snapshot's owner; BoardModel.apply sets it), whose Gmail a forward's draft opens in.
    var account: String?
    /// How a forward's draft opens: in the Chrome profile signed in to the account (Browser.open: nil when Chrome took
    /// it, else why it went to the default browser instead); a test's own in --selftest-forward.
    var openLink: (URL, String?) -> String? = Browser.open

    private let call: Call
    private let upload: Upload
    private let ahead: Int
    private let undoSeconds: TimeInterval
    private let readSeconds: TimeInterval
    private var cache: MailCache
    private var fetching: [String: (n: Int, waiting: [Shown])] = [:]   // mail_show asked and not answered, per thread
    private var loading: Set<String> = []   // threads a load waits for: it shows each wherever it is read
    private var targets: [String: String] = [:]   // the mail the latest load of a thread was asked to open at
    private var reads: [String: Timer] = [:]      // unread threads on screen, each marked read when its timer fires
    /// Where the tab's open thread was in the list when Archive, Inbox or Snooze closed it: the threads then below and
    /// above it, which ↓ and ↑ open (step). nil once a thread is opened any other way, or another box or search listed.
    private var gap: (below: String?, above: String?)?
    private var pages = 0            // Space and ⇧Space presses, numbered: each MailPage is told from the last by its n
    private var timer: Timer?
    private var undoTimer: Timer?
    private var undoPressed = 0      // presses of z and ⌘Z kept while undoWaiting (pressUndo)
    private var keyMonitor: Any?
    private var asked = 0            // requests numbered as they go
    private var listAsked = 0        // the newest list request: an older one's answer is out of date
    private var changedAt = 0        // the newest change (a mark, a fast switch): a list asked before it is out of date
    private var changing = 0         // changes not answered yet: a list answered meanwhile may predate them
    private var researcherChangedAt = 0   // mail_show asked before this may carry an old setting

    /// Whether the tab is on screen (MailView) and whether the board's window is (BoardModel.windowVisible). The list
    /// is polled while both are, or while a thread has a window of its own; the tab's keys (MailKeys) work while the tab
    /// is.
    var tabShown = false { didSet { if tabShown != oldValue { update() } } }
    var windowVisible = true { didSet { if windowVisible != oldValue { update() } } }

    /// `ahead` threads on each side of the open one are read before they are asked for (0: none); `cacheBytes` of
    /// documents are kept; drafts are kept in `draftsFolder` (MailDrafts), and with `syncDrafts` on the desktop too,
    /// saved there `saveDelay` after the last change; the Undo bar offers a change for `undoSeconds`; an unread thread
    /// is marked read once on screen for `readSeconds` (startRead).
    init(call: @escaping Call, upload: @escaping Upload, ahead: Int, cacheBytes: Int, draftsFolder: URL,
         undoSeconds: TimeInterval = MailModel.appUndoSeconds, readSeconds: TimeInterval = MailModel.appReadSeconds,
         syncDrafts: Bool = false, saveDelay: TimeInterval = MailDrafts.appSaveDelay) {
        self.call = call
        self.upload = upload
        self.ahead = ahead
        self.undoSeconds = undoSeconds
        self.readSeconds = readSeconds
        self.cache = MailCache(budget: cacheBytes)
        self.drafts = MailDrafts(folder: draftsFolder, call: syncDrafts ? call : nil, saveDelay: saveDelay)
        notice = drafts.loadProblem
        drafts.problem = { [weak self] in self?.notice = $0 }
    }

    /// The app's: requests go to the desktop as board actions; drafts are kept in `draftsFolder`, and with
    /// `syncDrafts` on the desktop, shared with the phone (an offscreen render keeps them to itself).
    convenience init(draftsFolder: URL, syncDrafts: Bool) {
        self.init(call: MailModel.live, upload: MailModel.liveUpload, ahead: MailModel.appAhead,
                  cacheBytes: MailModel.appCacheBytes, draftsFolder: draftsFolder, syncDrafts: syncDrafts)
    }

    /// The box listed, or nil while searching (a search covers every box).
    var listing: MailBox? { search.isEmpty ? box : nil }

    /// A thread's row as it now is: the list's, which polls keep fresh, else the one in its answer in the tab or its
    /// window.
    func current(_ id: String) -> MailboxThread? {
        threads?.first { $0.id == id } ?? seen(id)
    }

    /// A thread's row as the owner has been shown it: in its answer in the tab or its window, else the list's.
    private func seen(_ id: String) -> MailboxThread? {
        places.lazy.compactMap { self.reading($0)?.shown?.thread }.first { $0.id == id } ?? threads?.first { $0.id == id }
    }

    // MARK: places

    /// Every place a thread is read in: the tab (nil), then each thread's own window (its id), in order of thread.
    private var places: [String?] { [nil] + popouts.keys.sorted().map(Optional.some) }

    /// The reading in a place: the tab's (`window` nil), or thread `window`'s window's.
    private func reading(_ window: String?) -> MailReading? {
        guard let id = window else { return tab }
        return popouts[id]
    }

    /// The places reading thread `id`: the tab while it is its open thread, and its own window.
    private func readers(of id: String) -> [String?] { places.filter { reading($0)?.id == id } }

    /// Change the reading in a place by `edit`; nothing when the place reads no thread.
    private func change(_ window: String?, _ edit: (inout MailReading) -> Void) {
        guard var r = reading(window) else { return }
        edit(&r)
        if let id = window { popouts[id] = r } else { tab = r }
    }

    /// Each answer of thread `id` a place shows (the tab's, also while another thread loads there, and its window's),
    /// its row changed by `edit`.
    private func changeRow(_ id: String, _ edit: (MailboxThread) -> MailboxThread) {
        for p in places {
            if let t = reading(p)?.shown?.thread, t.id == id {
                let row = edit(t)
                change(p) { $0.shown?.thread = row }
            }
        }
    }

    func draft(for id: String) -> Draft { drafts.draft(id) }

    private func update() {
        let on = (tabShown && windowVisible) || !popouts.isEmpty
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
                                                 flags: e.modifierFlags, typing: w.firstResponder is NSText,
                                                 list: true) else { return e }
                if key == .release { w.makeFirstResponder(nil) } else { self.perform(key) }
                return nil
            }
        } else if !tabShown, let m = keyMonitor {
            NSEvent.removeMonitor(m)
            keyMonitor = nil
        }
    }

    // MARK: reading

    /// The list again (each poll, ⌘R, and after every change), and the desktop's list of drafts (MailDrafts.refresh).
    func refresh() {
        drafts.refresh(reading: Set(places.compactMap { reading($0)?.id }))
        asked += 1
        let n = asked
        listAsked = n
        var req: [String: Any] = ["action": "mail_list", "box": (listing ?? .all).rawValue, "limit": MailModel.listLimit,
                                  "sessions": true]
        if !search.isEmpty { req["query"] = search }
        call(req) { [weak self] obj, err in
            guard let self = self, n == self.listAsked, n > self.changedAt, self.changing == 0 else { return }
            self.now = Date()
            let (got, why) = Actions.decodeAnswer(obj, err, key: nil, as: MailList.self)
            guard let list = got else { self.listError = why; return }
            self.threads = list.threads
            self.counts = list.counts
            self.sessionsError = list.sessionsError
            self.groupsError = list.groupsError
            self.listError = list.dropped.first.map {
                "\(list.dropped.count) part\(list.dropped.count == 1 ? "" : "s") of the list not understood: \($0)"
            }
            if !self.researcherUnconfirmed.isEmpty { self.researcherUnconfirmed.subtract(list.threads.map(\.id)) }
            // each thread being read whose answer the list shows out of date, in the tab or its window, read once
            let answers = self.places.compactMap { self.reading($0) }.compactMap { r in r.shown?.thread.id == r.id ? r.shown : nil }
            var stale = Set<String>()
            for s in answers {
                let id = s.thread.id
                let row = list.threads.first { $0.id == id }
                if let row = row, row.researcher != s.thread.researcher {
                    self.changeRow(id) { self.withResearcher($0, row.researcher) }
                }
                if MailRules.stale(s, row: row)
                    || (row == nil && self.fetching[id] == nil &&
                        (s.thread.researcher != nil || self.researcherUnconfirmed.contains(id))) {
                    stale.insert(id)
                }
            }
            for id in stale.sorted() { self.load(id, focus: nil, fresh: true) }
            self.readAhead()
        }
    }

    func show(_ b: MailBox) {
        guard b != box || !search.isEmpty else { return }
        box = b
        search = ""
        gap = nil
        threads = nil
        refresh()
    }

    /// Search every box for threads with all of `words` (in the subject, an agent's name or a message); "" goes back to
    /// the box.
    func find(_ words: String) {
        let w = words.trimmingCharacters(in: .whitespacesAndNewlines)
        guard w != search else { return }
        search = w
        gap = nil
        threads = nil
        refresh()
    }

    /// Open a thread, at the message `key` (nil: as display decides): at once when it was read before and the list
    /// shows it unchanged since, else once the desktop answers. The one open already is read again only if it failed
    /// to load. Where a thread Archive, Inbox or Snooze closed was (gap) is forgotten.
    func open(_ id: String, at key: String? = nil) {
        guard id != selected || shown?.thread.id != id else { return }
        if revealing?.id != id { revealing = nil }               // another thread opened before it came
        gap = nil
        let old = selected
        tab = MailReading(id: id, shown: shown, current: current)  // what was shown stays until its answer comes
        if let old = old, old != id { leave(old) }
        drafts.opened(id)
        if let s = kept(id) {
            display(s, focus: key, in: nil)
            startRead(s)
        } else {
            load(id, focus: key, fresh: false)
        }
        readAhead()
    }

    /// Thread `id` as it was read before, when the list shows it unchanged since, with the list's flags (the newer).
    private func kept(_ id: String) -> MailShow? {
        guard let row = threads?.first(where: { $0.id == id }), var s = cache.take(id), !MailRules.stale(s, row: row) else {
            return nil
        }
        s.thread = row
        return s
    }

    /// The owner has moved off thread `id` in the tab, or closed its window: unless the thread is still being read in
    /// the other place, a reply box opened and left empty goes, and an unread thread is left unread (stopRead).
    private func leave(_ id: String) {
        guard readers(of: id).isEmpty else { return }
        draft(for: id).leave()
        drafts.left(id)
        stopRead(id)
    }

    /// Pop thread `id` out into a window of its own (MailWindows), or bring its window to the front when it has one
    /// (one that failed to load is read again). It shows as a thread opened in the tab does: at once when it was read
    /// before and the list shows it unchanged since, else once the desktop answers, at its first unread mail or its
    /// newest; an unread thread is marked read after its second on screen (startRead).
    func popOut(_ id: String) {
        if let p = popouts[id] {
            if p.shown == nil && p.error != nil {
                change(id) { $0.error = nil }
                load(id, focus: nil, fresh: false)
            }
            raise.send(id)
            return
        }
        popouts[id] = MailReading(id: id)
        update()
        drafts.opened(id)
        if let s = kept(id) {
            display(s, focus: nil, in: id)
            startRead(s)
        } else {
            load(id, focus: nil, fresh: false)
        }
    }

    /// Thread `id`'s window has closed, or Unread closed it: the window goes (MailWindows), and with it the reading.
    func popIn(_ id: String) {
        guard popouts.removeValue(forKey: id) != nil else { return }
        leave(id)
        update()
    }

    /// Open a thread from the board (an agent's latest mail, sent at `at`) at that mail (MailRules.revealed) and list
    /// the box it is in, so it shows in the list: the inbox, Snoozed or Archived.
    func reveal(_ id: String, at: Double? = nil) {
        if selected == id, let s = shown, s.thread.id == id {
            change(nil) { $0.current = MailRules.revealed(s.messages, at: at) ?? $0.current }
            listBox(of: s.thread)
        } else {
            revealing = (id, at)
            open(id)
        }
    }

    private func listBox(of t: MailboxThread) {
        revealing = nil
        if let b = MailBox(rawValue: t.box), listing != b { show(b) }
    }

    func close() {
        let old = selected
        tab = nil
        if let old = old { leave(old) }
    }

    /// Read a thread from the desktop and show it wherever it is still being read (readers: the tab, as the open thread,
    /// and its window) at the message `focus` (nil: as each place decides); an unread thread is then marked read, once,
    /// after its second on screen (startRead).
    /// `fresh`: ask anew even when a request for it is on its way already, which may predate a change; else wait for
    /// that one, unless a load waits for it already, which shows it wherever it is read (so each answer is shown and
    /// marked read once), at the latest message asked for (`targets`). A request already on its way that it waited for
    /// may have been asked before the list showed the thread changed; then it shows that answer and asks once more,
    /// afresh.
    private func load(_ id: String, focus key: String?, fresh: Bool) {
        if let k = key { targets[id] = k }
        guard fresh || !loading.contains(id) else { return }
        loading.insert(id)
        fetch(id, fresh: fresh) { [weak self] got, why in
            guard let self = self else { return }
            self.loading.remove(id)
            let at = self.targets.removeValue(forKey: id)
            let readers = self.readers(of: id)
            guard !readers.isEmpty else { return }
            guard let s = got else { return readers.forEach { self.change($0) { $0.error = why } } }
            readers.forEach { self.display(s, focus: at, in: $0) }
            self.startRead(s)
            if !fresh, MailRules.behind(s, row: self.threads?.first { $0.id == id }) { self.load(id, focus: nil, fresh: true) }
        }
    }

    /// Show `s` in a place reading its thread, the tab (`window` nil) or its own window, at the message `key`; with
    /// none given, a thread opened from a tile goes, in the tab, to its agent's mail (MailRules.revealed), and otherwise
    /// MailRules.opening decides.
    private func display(_ s: MailShow, focus key: String?, in window: String?) {
        guard let r = reading(window) else { return }
        if let line = s.widgetLine { notice = line }
        let reveal = window == nil && revealing?.id == s.thread.id ? revealing : nil
        let at = key ?? reveal.flatMap { MailRules.revealed(s.messages, at: $0.at) }
            ?? MailRules.opening(s, was: r.shown, reading: r.current)
        change(window) {
            $0.shown = s
            $0.current = at
            $0.error = s.droppedLine
        }
        if reveal != nil { listBox(of: s.thread) }
    }

    /// `s` is on screen, in the tab or its thread's window: an unread thread is marked read once it has been on screen
    /// for `readSeconds` (readNow). A wait already running for it runs on, as the thread has been on screen since it
    /// began, in one place or the other. The owner leaving it before then (leave), or changing it (mark), ends the wait
    /// (stopRead): it stays unread and nothing is sent. The tab's open thread counts as on screen while it is open,
    /// whether or not the Mail tab or the board's window is in front.
    private func startRead(_ s: MailShow) {
        let id = s.thread.id
        guard s.thread.unread, reads[id] == nil else { return }
        let t = Timer(timeInterval: readSeconds, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.readNow(id) }                  // the main run loop's timer
        }
        RunLoop.main.add(t, forMode: .common)                               // on time while a menu is open too
        reads[id] = t
    }

    /// Thread `id`'s time on screen is up: it is marked read up to the newest message on screen now (mark), when a
    /// place still shows it (its own answer, not another's while it loads) and it is still unread.
    private func readNow(_ id: String) {
        reads[id] = nil
        let onScreen = places.contains { reading($0).map { $0.id == id && $0.shown?.thread.id == id } ?? false }
        if onScreen && seen(id)?.unread == true { mark(id, .read, undoable: false) }
    }

    /// The wait to mark thread `id` read ends with nothing marked.
    private func stopRead(_ id: String) {
        reads.removeValue(forKey: id)?.invalidate()
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
            let (show, why) = MailShow.decode(obj, err)
            var got = show
            if n <= self.researcherChangedAt || self.researcherAsked != nil {
                got?.thread.researcher = self.current(id)?.researcher
            } else if got != nil, self.fetching[id]?.n == n, self.threads?.contains(where: { $0.id == id }) != true,
                      self.researcherUnconfirmed.contains(id) {
                self.researcherUnconfirmed.remove(id)
            }
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

    /// Change a thread's flags on the desktop (mail_mark), showing the change at once, in the list, the tab and its
    /// window, and the list as the desktop has it once it answers. read, archive and snooze reach up to the newest
    /// message the owner has been shown: the thread's answer in the tab or its window, else the list's. A refusal is
    /// said above the thread. `undoable`: a change the owner made, which the Undo bar offers once the desktop has made
    /// it, and which ends the wait to mark the thread read (stopRead).
    private func mark(_ id: String, _ op: MailOp, until: Double? = nil, undoable: Bool) {
        guard let t = seen(id) else { return }
        let open = id == selected, reading = open ? current : nil, popped = popouts[id] != nil
        threads = threads.map { MailRules.applying(op, to: id, in: $0, box: listing, until: until) }
        changeRow(id) { MailRules.changed($0, op, until: until) }
        if undoable {
            undoWaiting += 1
            stopRead(id)
        }
        send(MailMark(op: op, upto: op.reaches ? t.newest : nil, until: until), to: id) { [weak self] after, why in
            guard let self = self else { return }
            if let after = after {
                if undoable {
                    self.undoFailure = nil
                    self.undos.append(MailUndo(line: op.done(until: until, now: Date()), before: t, after: after,
                                               open: open, reading: reading, popped: popped))
                    self.offerUndo()
                }
            } else {
                self.notice = "\(op.failure): \(why ?? "no answer")"
            }
            if undoable { self.answered(refused: after == nil) }
        }
    }

    /// Send mark `m` of thread `id` to the desktop (mail_mark). A list asked before it answers is out of date, and the
    /// list is read again once it has. `done` gets the thread's row as the desktop answered it, or why not.
    private func send(_ m: MailMark, to id: String, _ done: @escaping @MainActor (MailboxThread?, String?) -> Void) {
        asked += 1
        changedAt = asked
        changing += 1
        var req: [String: Any] = ["action": "mail_mark", "threads": [id], "op": m.op.rawValue]
        if let u = m.upto { req["upto"] = u }
        if let u = m.until { req["until"] = u }
        call(req) { [weak self] obj, err in
            guard let self = self else { return }
            self.changing -= 1
            let (rows, why) = Actions.decodeAnswer(obj, err, key: "threads", as: [MailboxThread].self)
            let row = rows?.first { $0.id == id }
            done(row, row == nil ? why ?? "the desktop's answer has no row for the thread" : nil)
            self.refresh()
        }
    }

    /// Send `marks` of thread `id` one after another, each once the desktop has answered the one before; then `done`
    /// with nil, or with why the first it refused was refused, sending none after it.
    private func send(_ marks: ArraySlice<MailMark>, to id: String, _ done: @escaping @MainActor (String?) -> Void) {
        guard let m = marks.first else { return done(nil) }
        send(m, to: id) { [weak self] row, why in
            guard row != nil else { return done("\(m.op.failure.lowercased()): \(why ?? "no answer")") }
            guard let self = self else { return }
            self.send(marks.dropFirst(), to: id, done)
        }
    }

    /// The Undo bar shows the newest change on it for `undoSeconds` from now; then the bar goes, with every change on it.
    private func offerUndo() {
        undoTimer?.invalidate()
        let t = Timer(timeInterval: undoSeconds, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.undos = [] }                   // the main run loop's timer
        }
        RunLoop.main.add(t, forMode: .common)
        undoTimer = t
    }

    /// Undo (the bar's button, and z and ⌘Z through pressUndo): put back the newest change on the bar. The thread is
    /// shown at once as it was before (MailRules.undone: a snooze since past puts it in the inbox), in its place in the
    /// list and in its window, opened again at the mail being read if it was the open one, and in a window of its own
    /// again if it had one that has closed since (Unread closes it); then the marks that put it back are sent
    /// (MailRules.undoing): its box (a snooze until the same time), then read or unread. The bar then offers the change
    /// before it, if any, for `undoSeconds` more. Nothing while a change or an Undo is unanswered. A refusal ends the
    /// bar's changes, the bar says why, and the list is read again. A Discard is undone by bringing the draft back
    /// (undoDiscard).
    func undo() {
        guard undoWaiting == 0, let u = undos.popLast() else { return }
        undoTimer?.invalidate()
        if let gone = u.discarded { return undoDiscard(u.before.id, gone) }
        let id = u.before.id
        let back = withResearcher(MailRules.undone(u.before, now: Date().timeIntervalSince1970), current(id)?.researcher)
        guard let marks = MailRules.undoing(back, from: u.after) else {
            undos = []
            undoFailure = "Not undone: the thread was in a box the tab does not know, “\(u.before.box)”"
            return
        }
        threads = threads.map { MailRules.restoring(back, in: $0, box: listing) }
        changeRow(id) { _ in back }
        if u.open { open(id, at: u.reading) }
        if u.popped && popouts[id] == nil { popOut(id) }
        undoWaiting += 1
        send(marks[...], to: id) { [weak self] why in
            guard let self = self else { return }
            if let why = why {
                self.undos = []
                self.undoFailure = "Not undone: " + why
            } else if !self.undos.isEmpty {
                self.offerUndo()
            }
            self.answered(refused: why != nil)
        }
    }

    /// Undo of a Discard of thread `id`'s draft: the desktop brings back the version it kept of it (MailDrafts.restore);
    /// one only this Mac had is put back here (MailDrafts.bringBack). A refusal is said on the bar, as for a mark.
    private func undoDiscard(_ id: String, _ gone: MailUndo.Discarded) {
        guard let version = gone.version else {
            drafts.bringBack(id, gone.draft)
            if !undos.isEmpty { offerUndo() }
            return
        }
        undoWaiting += 1
        drafts.restore(id, version: version) { [weak self] why in
            guard let self = self else { return }
            if let why = why {
                self.undos = []
                self.undoFailure = "Not undone: " + why
            } else if !self.undos.isEmpty {
                self.offerUndo()
            }
            self.answered(refused: why != nil)
        }
    }

    /// Discard (the box's trash button): thread `id`'s draft emptied, here and on the desktop, which keeps it as a
    /// version; the Undo bar offers to bring it back once the desktop has it (MailDrafts.discard), as it offers a mark.
    func discard(_ id: String) {
        let row = seen(id) ?? current(id)
        undoWaiting += 1
        drafts.discard(id) { [weak self] version, before in
            guard let self = self else { return }
            if let b = before, let row = row {
                self.undoFailure = nil
                self.undos.append(MailUndo(line: "Draft discarded", before: row, after: row, open: false, reading: nil,
                                           popped: false, discarded: MailUndo.Discarded(version: version, draft: b)))
                self.offerUndo()
            }
            self.answered(refused: before == nil)
        }
    }

    /// A version from the box's Versions menu brought back as thread `id`'s draft (MailDrafts.restore); why not, in the
    /// box.
    func restoreVersion(_ id: String, _ v: DraftVersion) {
        drafts.restore(id, version: v.id) { [weak self] why in
            if let why = why { self?.draft(for: id).error = "Not brought back: " + why }
        }
    }

    /// z and ⌘Z: Undo at once, or, while a change or an Undo is unanswered, once it has been answered, so that a quick z
    /// after a change, or a second z, is not lost. A refusal meanwhile drops the presses kept, as what they would undo
    /// is then not known.
    func pressUndo() {
        if undoWaiting > 0 { undoPressed += 1 } else { undo() }
    }

    /// A change the owner made, or an Undo, answered (`refused`: not made): a press of z kept meanwhile Undoes now, once
    /// nothing else is unanswered.
    private func answered(refused: Bool) {
        undoWaiting -= 1
        if refused {
            undoPressed = 0
        } else if undoWaiting == 0, undoPressed > 0 {
            undoPressed -= 1
            undo()
        }
    }

    /// The bar's ×, after an Undo failed.
    func dismissUndoFailure() { undoFailure = nil }

    /// The Fast switch (MailModeBar, atop the thread in the tab and in its own window): fast mode for thread `id` on or off
    /// (mail_fast). The desktop answers with the thread as the mailroom has it now, whose fast mode the list and every
    /// place reading the thread show at once; a refusal (a group's thread, one no single agent owns) is said above it.
    /// A list asked before the answer may predate it, so it is not shown (as after a mark); the list is read again once
    /// it has come.
    func setFast(_ id: String, on: Bool) {
        guard fastAsked == nil else { return }
        fastAsked = id
        asked += 1
        changedAt = asked
        changing += 1
        call(["action": "mail_fast", "thread": id, "on": on]) { [weak self] obj, err in
            guard let self = self else { return }
            self.fastAsked = nil
            self.changing -= 1
            let (got, why) = Actions.decodeAnswer(obj, err, key: "thread", as: MailboxThread.self)
            if let t = got {
                self.now = Date()                    // the minutes left count from the answer, not the last list
                self.updateModeRow(id) { row in
                    var r = row
                    r.fast = t.fast
                    return r
                }
            } else {
                self.notice = "Fast mode not switched \(on ? "on" : "off"): " + (why ?? "no answer")
            }
            self.refresh()
        }
    }

    /// Update the list and every reader while keeping the rest of each row, including the list's sessions.
    private func updateModeRow(_ id: String, _ edit: (MailboxThread) -> MailboxThread) {
        threads = threads?.map { $0.id == id ? edit($0) : $0 }
        changeRow(id, edit)
    }

    private func withResearcher(_ row: MailboxThread, _ state: MailResearcher?) -> MailboxThread {
        var row = row
        row.researcher = state
        return row
    }

    /// Request a switch for the rendered routing tag, then read the mailroom's state for the list and open threads.
    func toggleResearcher(_ id: String, from before: MailResearcher) {
        guard researcherAsked == nil, !researcherUnconfirmed.contains(id),
              let expectedTag = before.tag, !expectedTag.isEmpty else { return }
        let on = !before.on
        researcherAsked = id
        changing += 1
        call(["action": "mail_researcher", "thread": id, "on": on, "expected_tag": expectedTag]) { [weak self] obj, err in
            guard let self = self else { return }
            self.researcherAsked = nil
            self.changing -= 1
            self.asked += 1
            self.changedAt = self.asked
            self.researcherChangedAt = self.asked
            let readers = Set(self.places.compactMap { self.reading($0)?.id })
            let listed = Set(self.threads?.filter { $0.researcher != nil }.map(\.id) ?? [])
            self.researcherUnconfirmed.formUnion(listed.union(readers.filter { self.current($0)?.researcher != nil }))
            let (got, why) = Actions.decodeAnswer(obj, err, key: "thread", as: MailboxThread.self)
            if let t = got, t.id == id, let state = t.researcher,
               state.on == on, state.tag == expectedTag, !state.agent.isEmpty {
                self.updateModeRow(id) { self.withResearcher($0, state) }
                self.researcherUnconfirmed.remove(id)
                if let warning = obj?["error"] as? String {
                    self.notice = warning
                }
            } else {
                self.researcherUnconfirmed.insert(id)
                self.notice = "Could not confirm the Researcher setting for \(before.agent). "
                    + (why ?? "Refreshing the thread before switching Researcher again.")
            }
            // Sibling threads may share the setting. Read their own routes, including popouts outside the list.
            for other in readers.sorted()
                where self.threads?.contains(where: { $0.id == other }) != true {
                self.load(other, focus: nil, fresh: true)
            }
            self.refresh()
        }
    }

    /// The first button, and e: archive thread `id` (nil: the open one), or move it back to the inbox when it is
    /// archived or snoozed. The tab's open thread closes, opening no other, and ↓ and ↑ then open the threads that were
    /// below and above it (step); a thread's own window stays, showing it archived. Undo puts it back.
    func archiveOrRestore(_ id: String? = nil) {
        guard let id = id ?? selected, let t = current(id) else { return }
        let place = id == selected ? around(id) : nil
        mark(id, t.box == MailBox.inbox.rawValue ? .archive : .inbox, undoable: true)
        if id == selected { putAway(place) }
    }

    /// Snooze thread `id` (nil: the open one) until `date`; the tab's open thread closes as after Archive, and a
    /// thread's own window stays, showing it snoozed. Undo puts it back.
    func snooze(_ id: String? = nil, until date: Date) {
        guard let id = id ?? selected else { return }
        let place = id == selected ? around(id) : nil
        mark(id, .snooze, until: date.timeIntervalSince1970, undoable: true)
        if id == selected { putAway(place) }
    }

    /// The threads below and above thread `id` in the list (nil past either end); nil when it is not listed.
    private func around(_ id: String) -> (below: String?, above: String?)? {
        guard let list = threads, let i = list.firstIndex(where: { $0.id == id }) else { return nil }
        return (list.indices.contains(i + 1) ? list[i + 1].id : nil, i > 0 ? list[i - 1].id : nil)
    }

    /// Archive, Inbox or Snooze took the tab's open thread away from `place` in the list (around, asked before the
    /// change): it closes ("No thread open"), and ↓ and ↑ then open the threads that were below and above it.
    private func putAway(_ place: (below: String?, above: String?)?) {
        close()
        gap = place
    }

    /// Mark a thread unread (nil: the open one); it closes, in the tab and its own window, as in Gmail, so it isn't
    /// marked read again when it next loads. Undo puts it back.
    func markUnread(_ id: String? = nil) {
        guard let id = id ?? selected else { return }
        mark(id, .unread, undoable: true)
        if id == selected { close() }
        popIn(id)
    }

    /// Right-click's Mark as read. Undo puts it back.
    func markRead(_ id: String) {
        mark(id, .read, undoable: true)
    }

    /// ↓ or j (1) and ↑ or k (-1): the next thread down the list or up it. With none open: the one that was below or
    /// above the thread Archive, Inbox or Snooze last closed (gap), while it is still listed; else the first one.
    func step(_ by: Int) {
        guard let list = threads, !list.isEmpty else { return }
        guard let id = selected, let i = list.firstIndex(where: { $0.id == id }) else {
            let back = gap.flatMap { by > 0 ? $0.below : $0.above }
            open(list.first { $0.id == back }?.id ?? list[0].id)
            return
        }
        if list.indices.contains(i + by) { open(list[i + by].id) }
    }

    /// → (1) and ← (-1): the newer or older mail becomes the one being read, in the tab's open thread, or in thread
    /// `window`'s own window.
    func stepMessage(_ by: Int, in window: String? = nil) {
        guard let r = reading(window), let s = r.shown, s.thread.id == r.id,
              let key = MailRules.step(from: r.current, by: by, in: s.messages) else { return }
        change(window) { $0.current = key }
    }

    /// Space (1) and ⇧Space (-1): the document a screen down or up, in the tab, or in thread `window`'s own window.
    func turnPage(_ by: Int, in window: String? = nil) {
        guard reading(window) != nil else { return }
        pages += 1
        change(window) { $0.page = MailPage(n: pages, by: by) }
    }

    /// What a key does (MailKeys.command), in the tab, or in thread `window`'s own window, which has no list (the
    /// window's keys are MailKeys.command's with no list). `release` takes the window, so the key monitor does it.
    func perform(_ key: MailKey, in window: String? = nil) {
        switch key {
        case .thread(let by): if window == nil { step(by) }
        case .message(let by): stepMessage(by, in: window)
        case .page(let by): turnPage(by, in: window)
        case .archive: archiveOrRestore(window)
        case .unread: markUnread(window)
        case .snooze: askSnooze(window)
        case .undo: pressUndo()
        case .reply: reply(window)
        case .paste: _ = attach(.general, to: window)
        case .release: break
        }
    }

    /// s: the Snooze menu, with the keyboard in it, for thread `window`'s own window, or the tab's open thread (nil),
    /// which that place's ThreadActions pops up at its Snooze button (snoozeAsked); nothing with no thread open.
    func askSnooze(_ window: String? = nil) {
        guard let id = window ?? selected, current(id) != nil else { return }
        snoozeAsked.send(window)
    }

    /// Reply (the button, r): thread `id`'s reply box (nil: the open thread's), with the keyboard in it.
    func reply(_ id: String? = nil) {
        guard let id = id ?? selected else { return }
        draft(for: id).ask()
    }

    /// Files or an image on `pb` (a paste, a drop) into thread `id`'s reply (nil: the open thread's), which opens;
    /// whether there were any. Too many or too big: none, and the box says why.
    func attach(_ pb: NSPasteboard, to id: String? = nil) -> Bool {
        let items = Attachments.items(pb)
        guard let id = id ?? selected, !items.isEmpty else { return false }
        add(items, to: id)
        return true
    }

    /// The paperclip: files chosen in a panel, a sheet on `window` (the one the reply box is in), into thread `id`'s
    /// reply.
    func pickFiles(for id: String, in window: NSWindow?) {
        guard let w = window else { return }
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
    /// there joins thread `id`'s reply once it has arrived, and the reply waits for them. Not while the reply is frozen
    /// (Draft.frozen).
    func copyToDesktop(_ files: [URL], for id: String) {
        let d = draft(for: id)
        guard !d.frozen else {
            d.error = "Not copied. \(Draft.frozenWhy)"
            return
        }
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

    /// Send a thread's draft as the owner's reply to its newest agent mail (mail_reply): its text, made of the owner's
    /// answers, comments and words (ReplyText), and its files. The draft is saved on the desktop first with the reply's
    /// key (MailDrafts.settle), so the desktop empties it once the mailroom has the reply; a draft that changed
    /// elsewhere meanwhile is not sent, and shows as it is there. Its files go as the desktop's own copies where it
    /// has them, else as base64. Then read the thread again, brought to that reply, wherever it is being read
    /// (the tab, its window). When it fails, the desktop may have kept the reply (no answer came, or agent-mail failed
    /// or timed out after the mailroom kept it), so the draft stays frozen with its key (Draft.finish); only when the
    /// desktop says it refused it before the mailroom ("refused"), or an attempt never sent fails here, does it not.
    func send(_ id: String) {
        let d = draft(for: id)
        let n = d.reply.unicodeScalars.count
        guard n <= MailModel.maxReply else {
            d.error = "Too long to send: \(Fmt.count(Double(n))) characters; the mailroom takes at most \(Fmt.count(Double(MailModel.maxReply)))."
            return
        }
        let retry = d.frozen                   // an earlier attempt may have reached the desktop
        guard let reply = d.begin() else { return }
        drafts.settle(id) { [weak self] how in
            if case .conflict(let note) = how { return d.finish(failure: "Not sent: " + note, maybeSent: true) }
            // saved, or not saved on the desktop (unreachable, or a desktop older than shared drafts): the reply goes
            // anyway, its files as base64 where the desktop does not have them, as a draft not saved there loses nothing
            // by the reply going; the phone, whose page's server always keeps drafts, waits for the save instead
            self?.deliver(id, d, reply.content, key: reply.key, retry: retry)
        }
    }

    private func deliver(_ id: String, _ d: Draft, _ content: DraftContent, key: String, retry: Bool) {
        var req: [String: Any] = ["action": "mail_reply", "thread": id, "key": key, "text": ReplyText.compose(content)]
        do {
            if !content.files.isEmpty {
                req["files"] = try content.files.map { f -> [String: String] in
                    drafts.onDesk(f, of: id) ? ["name": f.name, "draft": f.id]
                        : ["name": f.name, "data": try drafts.data(f).base64EncodedString()]
                }
            }
        } catch {
            d.finish(failure: "Not sent: a file could not be read: \(error.localizedDescription)", maybeSent: retry)
            return
        }
        call(req) { [weak self] obj, err in
            let (got, why) = Actions.decodeAnswer(obj, err, key: nil, as: MailReply.self)
            d.finish(failure: got == nil ? "Not sent: " + (why ?? "no answer") : nil,
                     maybeSent: obj?["refused"] as? Bool != true)
            guard let sent = got, let self = self else { return }
            if let e = obj?["error"] as? String, !e.isEmpty { self.notice = e }    // sent, but its draft not emptied
            if self.current(id)?.fast != nil { d.ask() }      // fast mode: the box stays, for the next message
            let cleared = obj?["draft"] as? [String: Any]
            self.drafts.sent(id, cleared: cleared?["cleared"] as? Bool == true, rev: cleared?["rev"] as? Int)
            if !self.readers(of: id).isEmpty { self.load(id, focus: sent.key, fresh: true) }
            self.refresh()
        }
    }

    /// A Forward button: the desktop makes a draft in the owner's Gmail of agent mail `key` of thread `id`, a new mail
    /// of its content, images and attachments with no recipient yet (mail_forward), and the draft opens in that Gmail
    /// (openLink), for the owner to address and send. `done` gets nil once it has opened, else why not, which the tab says.
    func forward(_ key: String, in id: String, done: @escaping @MainActor (String?) -> Void) {
        call(["action": "mail_forward", "thread": id, "key": key]) { [weak self] obj, err in
            guard let self = self else { return done("the Mail tab has gone") }
            let (got, why) = Actions.decodeAnswer(obj, err, key: nil, as: MailForwarded.self)
            guard let draft = got, let url = MailForward.draftURL(draft.message, account: self.account) else {
                let failure = "Not forwarded: " + (why ?? "Gmail made a draft whose message id (\(got?.message ?? "")) "
                                                   + "its page cannot open; it is in Gmail's Drafts")
                self.notice = failure
                return done(failure)
            }
            if let wrong = self.openLink(url, self.account) {
                self.notice = "The draft opened in the default browser, which may be the wrong Google account: " + wrong
            }
            done(nil)
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
/// that has no mailbox section it says so and asks the desktop nothing. Equatable, so the board's clock and snapshots
/// redraw it only when what it shows changes (BoardView puts it in .equatable()).
struct MailView: View, Equatable {
    let mail: MailModel           // the columns watch it; this view only places them
    let connected: Bool           // a snapshot has come
    let available: Bool           // it has a mailbox section
    let working: [AgentKey: AgentState]   // the board's agents at work (BoardModel.workingAgents): their threads' dots
    @Environment(\.zoom) private var z

    static func == (a: MailView, b: MailView) -> Bool {
        a.mail === b.mail && a.connected == b.connected && a.available == b.available && a.working == b.working
    }

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
                // laid from its left edge: were anything in it wider than the column, the rows' strips would stay
                // in view
                MailListColumn(mail: mail, drafts: mail.drafts, working: working)
                    .frame(width: MailListColumn.width * z, alignment: .leading).clipped()
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
    /// Its width, before zoom.
    static let width: CGFloat = 300
    /// The room round what is over the list (search, the box, ThreadActions), before zoom.
    static let padding: CGFloat = 10

    @ObservedObject var mail: MailModel
    @ObservedObject var drafts: MailDrafts
    let working: [AgentKey: AgentState]   // the board's agents at work
    @Environment(\.zoom) private var z
    @State private var query = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 8 * z) {
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
                if mail.search.isEmpty {
                    BoxSwitch(box: mail.box, show: { mail.show($0) })
                } else {
                    Text("Search: “\(mail.search)”, in every box")
                        .font(.system(size: 11.5 * z, weight: .medium)).lineLimit(2)
                }
                if let line = summary {
                    Text(line).font(.system(size: 10.5 * z)).foregroundStyle(.secondary)
                }
                if let e = mail.listError {
                    Text(e).font(.system(size: 11 * z)).foregroundColor(AgentState.blocked.color).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let e = mail.sessionsError {
                    Text("No busy dots: the desktop could not say which agents wrote in each thread: " + e)
                        .font(.system(size: 10.5 * z)).foregroundStyle(.secondary).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let e = mail.groupsError {
                    Text("No group colours: the desktop could not say each thread's group: " + e)
                        .font(.system(size: 10.5 * z)).foregroundStyle(.secondary).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ThreadActions(mail: mail, window: nil)
            }
            .padding(MailListColumn.padding * z)
            Divider()
            list.overlay(alignment: .bottom) { UndoBar(mail: mail).padding(10 * z) }
        }
        .onAppear { query = mail.search }
        .onChange(of: mail.search) { query = $0 }       // a thread opened from a tile lists its box, ending a search
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
                                    mixed: mail.listing == .all || mail.listing == nil, draft: drafts.held.contains(t.id),
                                    working: MailRules.working(t, working))
                                .contentShape(Rectangle())
                                .onTapGesture { mail.open(t.id) }
                                // beside the click, not after it: a click opens the thread at once, and the second
                                // of a double-click pops it out
                                .simultaneousGesture(TapGesture(count: 2).onEnded { mail.popOut(t.id) })
                                .contextMenu {
                                    Button("Open in new window") { mail.popOut(t.id) }
                                    Divider()
                                    Button(t.unread ? "Mark as read" : "Mark as unread") {
                                        if t.unread { mail.markRead(t.id) } else { mail.markUnread(t.id) }
                                    }
                                    Button(t.box == MailBox.inbox.rawValue ? "Archive" : "Move to Inbox") { mail.archiveOrRestore(t.id) }
                                }
                                .id(t.id)
                            Divider().padding(.leading, MailRow.textInset * z)
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
            // a list that comes (another box, a search) or a column that appears shows the open thread's row: a thread
            // opened from a tile is opened before its box is listed
            .onChange(of: mail.threads == nil) { waiting in if !waiting, let id = mail.selected { reader.scrollTo(id) } }
            .onAppear { if let id = mail.selected { reader.scrollTo(id) } }
        }
    }
}

/// The box listed (Inbox, Snoozed, Archived, All), as one row of equal segments, the listed one shaded: drawn at the
/// board's zoom, so it fits the column at every zoom (AppKit's segmented control keeps its size, 310 points, wider than
/// the column at zoom 1).
struct BoxSwitch: View {
    let box: MailBox
    let show: (MailBox) -> Void
    @Environment(\.zoom) private var z

    var body: some View {
        HStack(spacing: 0) {
            ForEach(MailBox.allCases) { b in
                Button { show(b) } label: {
                    Text(b.label).font(.system(size: 12 * z, weight: b == box ? .semibold : .regular)).lineLimit(1)
                        .padding(.vertical, 4 * z).frame(maxWidth: .infinity)
                        .background(RoundedRectangle(cornerRadius: 5)
                                        .fill(b == box ? Color.primary.opacity(0.12) : .clear))
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(2 * z)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.06)))
    }
}

/// One thread in the list, Gmail's way: who, how many, when; the subject; the start of the newest message. Unread is
/// bold and dark with a blue dot; read is plain and greyed, on a grey ground; the open thread is on the selection's
/// blue. A thread the desktop sent its group's colours with (MailboxThread.colour) has a strip of its sub-group's shade
/// down its left edge (README, "Group colours"), and is otherwise drawn as any other, so the blue stands out. A brown
/// dot beside the blue one's place marks a thread one of whose agents is at work now, in its tile's colour: a busy
/// tile's caramel, or the darker caramel of one waiting on a background command (MailRules.working). "Draft" marks a
/// reply the owner has started, and "Fast · 7 min" a thread in fast mode (FastBadge); a snoozed thread shows when it
/// comes back instead of its time.
struct MailRow: View {
    let thread: MailboxThread
    let selected: Bool
    let now: Date
    let mixed: Bool               // the list holds every box (All, or a search): mark the archived ones
    let draft: Bool               // an unsent reply of the owner's is kept for it
    let working: AgentState?      // busy or background: the state of an agent at work that wrote in it (MailRules.working)
    @Environment(\.zoom) private var z

    static let dot: CGFloat = 7, dotGap: CGFloat = 3, gap: CGFloat = 7, padding: CGFloat = 10
    /// Where the text starts, from the row's left edge (before zoom): the dividers between rows start there too.
    static let textInset = padding + dot + dotGap + dot + gap
    /// The group strip's width down a grouped row's left edge (before zoom).
    static let strip: CGFloat = 5

    /// What hovering over a grouped row names: "Work: Alpha", "Work", or "Ungrouped"; nil for a row
    /// the desktop sent no colours with.
    static func groupName(_ t: MailboxThread) -> String? {
        guard t.colour != nil else { return nil }
        guard let g = t.group else { return "Ungrouped" }
        return t.subgroup.map { "\(g): \($0)" } ?? g
    }

    /// The row's ground: the selection's blue; else, read, the plain grey.
    var ground: Color {
        if selected { return Palette.ok.opacity(0.16) }
        return thread.unread ? Color.clear : Color.primary.opacity(0.045)
    }

    /// What the row shows of thread `t`, all the agents' words, each left to right (Fmt.leftToRight): who wrote in
    /// it, its subject and its snippet.
    static func texts(_ t: MailboxThread) -> (agents: String, subject: String, snippet: String) {
        (Fmt.leftToRight(t.agents.isEmpty ? "(no agent)" : t.agents.joined(separator: ", ")),
         Fmt.leftToRight(t.subject.isEmpty ? "(no subject)" : t.subject), Fmt.leftToRight(t.snippet))
    }

    var body: some View {
        let t = thread, shown = MailRow.texts(thread)
        let ink: Color = t.unread ? .primary : .secondary
        HStack(alignment: .top, spacing: MailRow.gap * z) {
            // each dot keeps its place, shown or not, so every row's text starts at the same x
            HStack(spacing: MailRow.dotGap * z) {
                Circle().fill(t.unread ? Palette.ok : Color.clear).frame(width: MailRow.dot * z, height: MailRow.dot * z)
                if let state = working {
                    Circle().fill(state.color).frame(width: MailRow.dot * z, height: MailRow.dot * z)
                        .help(state.label)
                } else {
                    Color.clear.frame(width: MailRow.dot * z, height: MailRow.dot * z)
                }
            }
            .padding(.top, 5 * z)
            VStack(alignment: .leading, spacing: 2 * z) {
                HStack(alignment: .firstTextBaseline, spacing: 5 * z) {
                    Text(shown.agents)
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
                    if let fast = t.fast { FastBadge(fast: fast, now: now) }
                    Spacer(minLength: 4 * z)
                    if t.attachments {
                        Image(systemName: "paperclip").font(.system(size: 10.5 * z)).foregroundStyle(.secondary)
                    }
                    when
                }
                Text(shown.subject)
                    .font(.system(size: 12 * z, weight: t.unread ? .bold : .regular)).foregroundColor(ink).lineLimit(1)
                Text(shown.snippet).font(.system(size: 11.5 * z)).foregroundStyle(.secondary).lineLimit(2)
                if mixed && t.box == MailBox.archived.rawValue {
                    Text("Archived").font(.system(size: 10 * z, weight: .medium)).foregroundStyle(.secondary)
                        .padding(.horizontal, 5 * z).padding(.vertical, 1)
                        .background(Capsule().fill(Color.primary.opacity(0.08)))
                }
            }
        }
        .padding(.vertical, 8 * z).padding(.horizontal, MailRow.padding * z)
        .background(ground)
        .overlay(alignment: .leading) {
            if let s = t.shade { Rectangle().fill(s.color).frame(width: MailRow.strip * z) }
        }
        .help(MailRow.groupName(t) ?? "")
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

/// A thread's buttons: Reply, Archive (Inbox for a thread out of the inbox), Snooze and Unread. Above the list for the
/// tab's open thread (`window` nil; none open, they are greyed), and atop a popped-out thread's window for its own
/// (`window` its id). Snooze, clicked or s (MailModel.askSnooze), pops up its choices as AppKit's menu under the
/// button, with the keyboard in it.
struct ThreadActions: View {
    @ObservedObject var mail: MailModel
    let window: String?
    @Environment(\.zoom) private var z
    @State private var picking = false
    @State private var picked = Date()
    @State private var anchor = MenuAnchor.Spot()

    /// The thread the buttons act on.
    var id: String? { window ?? mail.selected }

    var body: some View {
        let t = id.flatMap { mail.current($0) }
        let inbox = t?.box == MailBox.inbox.rawValue
        HStack(spacing: 8 * z) {
            Button { mail.reply(id) } label: { Label("Reply", systemImage: "arrowshape.turn.up.left") }
                .help("Reply (r); drag files onto the mail or paste them to send them with it").fixedSize()
            Button { mail.archiveOrRestore(id) } label: {
                Label(inbox ? "Archive" : "Inbox", systemImage: inbox ? "archivebox" : "tray.and.arrow.down")
            }
            .help(inbox ? "Archive (e): out of the inbox until an agent writes in it again" : "Back to the inbox (e)")
            .fixedSize()
            Button { popSnooze() } label: { Label("Snooze", systemImage: "clock") }
                .help("Snooze (s): out of the inbox until then; back unread").fixedSize()
                .background(MenuAnchor(spot: anchor))
            Button { mail.markUnread(id) } label: { Label("Unread", systemImage: "envelope.badge") }
                .help("Mark unread (u), and close").fixedSize()
            Spacer(minLength: 0)
        }
        .buttonStyle(.borderless)
        .labelStyle(ZoomedLabelStyle(zoom: z))
        .font(.system(size: 11.5 * z))
        .disabled(t == nil)
        .sheet(isPresented: $picking) {
            SnoozePicker(date: $picked) { date in
                picking = false
                if let d = date { mail.snooze(id, until: d) }
            }
        }
        // on the main queue's next turn: the key has been handled before AppKit's menu takes the keyboard
        .onReceive(mail.snoozeAsked) { place in
            if place == window { DispatchQueue.main.async { MainActor.assumeIsolated { popSnooze() } } }
        }
    }

    /// Pick date & time…: the picker, at tomorrow 08:00.
    func pickDate() {
        picked = Snooze.morning(after: Date()) ?? Date().addingTimeInterval(86400)
        picking = true
    }

    /// Snooze, clicked or s: the Snooze menu (Snooze.items) as one of AppKit's menus, popped up under the Snooze
    /// button (MailModel.popUp), for the thread open here then.
    func popSnooze() {
        guard let id = id, mail.current(id) != nil else { return }
        guard let view = anchor.view else {
            mail.notice = "The Snooze menu could not be shown: no Snooze button"
            return
        }
        mail.popUp(Snooze.menu(Snooze.items(now: Date()), snooze: { mail.snooze(id, until: $0) }, pick: pickDate), view)
    }
}

/// An empty AppKit view behind a SwiftUI control (its background), kept in `spot`, for AppKit to pop a menu up at the
/// control. It takes no clicks.
struct MenuAnchor: NSViewRepresentable {
    /// Where the view is kept, weakly: it goes with the control.
    final class Spot {
        weak var view: NSView?
    }

    final class Empty: NSView {
        override func hitTest(_ point: NSPoint) -> NSView? { nil }
    }

    let spot: Spot

    func makeNSView(context: Context) -> Empty {
        let v = Empty()
        spot.view = v
        return v
    }

    func updateNSView(_ v: Empty, context: Context) { spot.view = v }
}

/// At the foot of the list, after a change Undo can put back: what was done ("Archived") and Undo, until the bar's
/// changes go (MailModel.undos); after an Undo failed, why, in the failures' red, until closed.
struct UndoBar: View {
    @ObservedObject var mail: MailModel
    @Environment(\.zoom) private var z

    var body: some View {
        if let why = mail.undoFailure {
            bar {
                Text(why).foregroundColor(AgentState.blocked.color).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8 * z)
                Button { mail.dismissUndoFailure() } label: { Image(systemName: "xmark") }.buttonStyle(.borderless)
            }
        } else if let u = mail.undos.last {
            bar {
                Text(u.line).lineLimit(2)
                Spacer(minLength: 8 * z)
                Button("Undo") { mail.undo() }
                    .buttonStyle(.borderless).font(.system(size: 12 * z, weight: .semibold)).foregroundColor(Palette.ok)
                    .disabled(mail.undoWaiting > 0).help("Undo (z or ⌘Z)")
            }
        }
    }

    func bar<C: View>(@ViewBuilder _ content: () -> C) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 0) { content() }
            .font(.system(size: 12 * z))
            .padding(.horizontal, 12 * z).padding(.vertical, 8 * z)
            .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: .windowBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.primary.opacity(0.15)))
            .shadow(color: .black.opacity(0.18), radius: 6, y: 2)
    }
}

/// The right pane: the open thread (MailThreadView, in the tab's web view), or a line saying there is none.
struct MailThreadPane: View {
    @ObservedObject var mail: MailModel
    @Environment(\.zoom) private var z

    var body: some View {
        if let r = mail.tab {
            MailThreadView(mail: mail, reading: r, window: nil, web: MailWeb.view, host: { Dialogs.window })
        } else {
            VStack(spacing: 10 * z) {
                MailNotice(mail: mail)
                Spacer()
                Image(systemName: "envelope.open").font(.system(size: 30 * z)).foregroundStyle(.tertiary)
                Text("No thread open").font(.system(size: 14 * z, weight: .medium)).foregroundStyle(.secondary)
                Text("Click one on the left, or press ↓.").font(.system(size: 12 * z)).foregroundStyle(.secondary)
                Spacer()
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

/// The last thing that failed (MailModel.notice), with its ×, over a thread wherever it is read.
struct MailNotice: View {
    @ObservedObject var mail: MailModel
    @Environment(\.zoom) private var z

    var body: some View {
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
}

/// A thread as one place reads it (`reading`): the tab's open thread (`window` nil), or a thread in its own window
/// (`window` its id). The notice, the bar naming the mail being read in a thread of several, the document (its subject
/// heads it, so scrolls with it) in `web`, that place's web view, and the reply box while there is one (ReplyArea),
/// whose file panel is a sheet on `host`, the place's window.
struct MailThreadView: View {
    @ObservedObject var mail: MailModel
    let reading: MailReading
    let window: String?
    let web: MailWebView
    let host: () -> NSWindow?
    @Environment(\.zoom) private var z

    var body: some View {
        let id = reading.id
        VStack(alignment: .leading, spacing: 0) {
            MailNotice(mail: mail)
            MailModeBar(mail: mail, id: id)
            bar
            document
            let mine = reading.shown?.thread.id == id ? reading.shown : nil
            let to = mine?.messages.last(where: { !$0.mine })?.from ?? mail.current(id)?.agents.last
            ReplyArea(mail: mail, id: id, draft: mail.draft(for: id), to: to, fast: mail.current(id)?.fast != nil,
                      replies: mine?.messages.filter { $0.mine && $0.status != nil } ?? [], host: host)
        }
    }

    /// In a thread of several mails, the one being read (outlined in the document): "Mail 2 of 5 · its sender · its
    /// time", and the buttons that do ← and →.
    @ViewBuilder var bar: some View {
        if let s = reading.shown, s.thread.id == reading.id, s.messages.count > 1,
           let i = s.messages.firstIndex(where: { $0.key == reading.current }) {
            let m = s.messages[i]
            HStack(spacing: 8 * z) {
                Text("Mail \(i + 1) of \(s.messages.count)").font(.system(size: 12 * z, weight: .semibold).monospacedDigit())
                Text("\(m.mine ? "You" : m.from) · \(MailFmt.at(m.ts, now: Date()))")
                    .font(.system(size: 12 * z)).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                HStack(spacing: 8 * z) {
                    Button { mail.stepMessage(-1, in: window) } label: { Image(systemName: "chevron.left") }
                        .help("Older mail (←)").disabled(i == 0)
                    Button { mail.stepMessage(1, in: window) } label: { Image(systemName: "chevron.right") }
                        .help("Newer mail (→)").disabled(i == s.messages.count - 1)
                }
                .buttonStyle(.borderless)
            }
            .padding(.horizontal, 8 * z).padding(.vertical, 4 * z)
            .background(RoundedRectangle(cornerRadius: 6).fill(Palette.ok.opacity(0.12)))
            .padding(.horizontal, 10 * z).padding(.vertical, 6 * z)
        }
    }

    /// The document. Its web view's loader keeps the closures given it, and the web view keeps its loader, so they
    /// capture the model and the thread alone: written plainly, each would capture this view, which holds the web view,
    /// and a window's web view would never be freed.
    @ViewBuilder var document: some View {
        if let s = reading.shown {
            let thread = s.thread.id
            MailDocument(html: s.document, thread: thread, agentMail: Set(s.messages.filter { !$0.mine }.map(\.key)),
                         widgets: s.widgets, draft: mail.draft(for: thread),
                         current: reading.current, page: reading.page, zoom: z, web: web, follow: { [mail] in mail.follow($0) },
                         refuse: { [mail] in mail.notice = $0 }, problem: { [mail] in mail.notice = $0 },
                         take: { [mail] in mail.attach($0, to: thread) }, forward: { [mail] in mail.forward($1, in: $0, done: $2) })
                .overlay {
                    if s.thread.id != reading.id {
                        ZStack {
                            Color(nsColor: .windowBackgroundColor).opacity(0.85)
                            if let e = reading.error { error(e) } else { ProgressView() }
                        }
                    }
                }
        } else if let e = reading.error {
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

/// Above the open thread: its Fast switch (MailModel.setFast), a capsule with a bolt, "Fast" or, while fast mode is on,
/// "Fast · 7 min"; and beside it, while on, the mailroom's note on what fast mode does to the agent's effort, so the
/// owner sees when it is left unchanged and why. When the desktop sends the thread's Researcher setting
/// (MailResearcher), a Researcher switch for its routed agent comes after Fast, with a line under them on how the
/// setting got on (MailModel.toggleResearcher). The minutes left count from the list's clock (MailModel.now, set with each
/// list, every 15 s while the tab is on screen), so the bar is drawn again only as the list is; nothing animates.
struct MailModeBar: View {
    @ObservedObject var mail: MailModel
    let id: String
    @Environment(\.zoom) private var z

    /// Fast mode's violet, for its note and the list's badge: lighter in dark mode.
    static let colour = Color(nsColor: NSColor(name: nil) { look in
        NSColor(hex: look.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? 0xA371F7 : 0x8250DF)
    })
    /// The switch's fill while it is on, under white words: the deeper violet in either mode.
    static let fill = Color(hex: 0x8250DF)

    /// What the bar shows of fast mode `fast` (nil: off) at `now`: the switch's words, the note beside it (nil while
    /// off), and the switch's help. `switching`: the desktop has not answered the last click yet. While the effort is
    /// still to be lowered (the mailroom does it at its next poll, within 30 s, and the list then says so), the note
    /// starts "Lowering effort…". The phone's page has the same rule (fastNote in web/mail/app.js): change both
    /// together.
    static func texts(_ fast: MailFast?, now: Date, switching: Bool) -> (label: String, note: String?, help: String) {
        guard let f = fast else {
            return (switching ? "Fast…" : "Fast", nil,
                    "Fast mode, for quick back-and-forths with this thread's agent: it answers you here in a few plain "
                    + "lines at low effort, and its replies here skip the review before sending. Return sends your reply, "
                    + "and the box stays open for the next. It ends when you switch "
                    + "it off, or after some minutes (5 by default: the mailroom's mailbox.fast_minutes) with no "
                    + "message from you here; then the agent's effort goes back.")
        }
        let who = f.agent.map { " for \($0)" } ?? ""
        let said = f.note ?? (f.effort == "unchanged" ? "Effort unchanged." : "")
        let note = f.effort == "lowering" ? ("Lowering effort… " + said).trimmingCharacters(in: .whitespaces) : said
        return (switching ? "Fast…" : f.label(at: now), note.isEmpty ? nil : note,
                "Fast mode is on\(who) until \(MailFmt.at(f.until, now: now)), and each message of yours here moves its "
                + "end on. \(note.isEmpty ? "" : note + " ")Click to switch it off.")
    }

    var body: some View {
        let t = mail.current(id)
        let on = t?.fast != nil
        let shown = Self.texts(t?.fast, now: mail.now, switching: mail.fastAsked == id)
        VStack(alignment: .leading, spacing: 4 * z) {
            HStack(alignment: .center, spacing: 8 * z) {
                if let note = shown.note {
                    Text(Fmt.leftToRight(note)).font(.system(size: 11.5 * z, weight: .medium)).foregroundColor(Self.colour)
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                }
                Spacer(minLength: 0)
                Button { mail.setFast(id, on: !on) } label: {
                    HStack(spacing: 3 * z) {
                        Image(systemName: on ? "bolt.fill" : "bolt")
                        Text(shown.label).monospacedDigit()
                    }
                }
                .buttonStyle(MailModeStyle(on: on, fill: Self.fill))
                .disabled(t == nil || mail.fastAsked != nil)
                .help(shown.help)
                if let researcher = t?.researcher {
                    Button("Researcher") { mail.toggleResearcher(id, from: researcher) }
                        .buttonStyle(MailModeStyle(on: researcher.on, fill: .accentColor))
                        .accessibilityValue(researcherStatus(researcher))
                        .help(researcher.on ? "Turn off Researcher."
                              : "Turn on Researcher to approve this agent's experiment plan in Mail.")
                        .disabled(mail.researcherAsked != nil || mail.researcherUnconfirmed.contains(id)
                                  || researcher.tag?.isEmpty != false)
                }
            }
            if let researcher = t?.researcher {
                Text(Fmt.leftToRight(researcherStatus(researcher)))
                    .font(.system(size: 11.5 * z)).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 14 * z).padding(.top, 6 * z).padding(.bottom, 2 * z)
    }

    private func researcherStatus(_ state: MailResearcher) -> String {
        if mail.researcherAsked == id { return "Saving Researcher for \(state.agent)…" }
        if mail.researcherUnconfirmed.contains(id) { return "Checking Researcher for \(state.agent)…" }
        let requested = "Requested \(state.on ? "on" : "off") for \(state.agent)."
        if state.tag?.isEmpty != false { return requested + " Update the desktop to change this setting." }
        if let delivery = state.deliveryNote { return requested + " " + delivery }
        return state.on ? "\(state.agent) will ask you to approve its experiment plan in Mail." : "Off for \(state.agent)."
    }
}

private struct MailModeStyle: ButtonStyle {
    let on: Bool
    let fill: Color
    @Environment(\.zoom) private var z
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 11.5 * z, weight: .semibold))
            .foregroundColor(on ? .white : .primary)
            .padding(.horizontal, 9 * z).padding(.vertical, 3 * z)
            .background(Capsule().fill(on ? fill : Color.clear))
            .overlay(Capsule().strokeBorder(on ? fill : Color.primary.opacity(0.25)))
            .contentShape(Capsule())
            .opacity(enabled ? (configuration.isPressed ? 0.75 : 1) : 0.6)
    }
}

/// A row's badge for a thread in fast mode: a bolt and "Fast · 7 min" (MailFast.label), in fast mode's violet.
struct FastBadge: View {
    let fast: MailFast
    let now: Date
    @Environment(\.zoom) private var z

    var body: some View {
        HStack(spacing: 2 * z) {
            Image(systemName: "bolt.fill")
            Text(fast.label(at: now)).monospacedDigit()
        }
        .font(.system(size: 10.5 * z, weight: .semibold))
        .foregroundColor(MailModeBar.colour)
        .fixedSize()
        .help(fast.note.map { "Fast mode is on. " + $0 } ?? "Fast mode is on")
    }
}

/// Under the thread: its reply box, while the owner has asked for it (Reply) or it holds a draft; else nothing, so the
/// document has the pane. The tab and the thread's own window show the same draft, so text typed in one is in the
/// other. In fast mode Return alone sends, as ⌘↩ does, and ⇧↩ makes a new line.
struct ReplyArea: View {
    /// The room round the box, before zoom.
    static let padding: CGFloat = 12

    let mail: MailModel
    let id: String
    @ObservedObject var draft: Draft
    let to: String?                // whose mail a reply answers
    let fast: Bool                 // the thread is in fast mode
    let replies: [MailboxMessage]  // the owner's replies in the thread that have a state
    let host: () -> NSWindow?      // the window the box is in: the paperclip's panel is a sheet on it
    @Environment(\.zoom) private var z

    var body: some View {
        if draft.shown {
            VStack(spacing: 0) {
                Divider()
                ComposeBox(draft: draft, placeholder: to.map { "Reply to \($0)…" } ?? "Reply…", height: 110,
                           returnSends: fast, files: ComposeFiles(take: { mail.attach($0, to: id) }, pick: { mail.pickFiles(for: id, in: host()) },
                                               remove: { mail.drafts.remove($0, from: id) }, offer: MailModel.desktopOffer,
                                               action: "Copy to the desktop", instead: { mail.copyToDesktop($0, for: id) }),
                           send: { mail.send(id) }, discard: { mail.discard(id) }, restore: { mail.restoreVersion(id, $0) }) {
                    ReplyStates(to: to, fast: fast, replies: replies)
                }
                .padding(ReplyArea.padding * z)
            }
        }
    }
}

/// Beside a thread's Send button: how the owner's last three replies in it got on, or, before any reply, which key
/// sends (keys).
struct ReplyStates: View {
    let to: String?               // whose mail a reply answers: the thread's newest agent message's sender
    let fast: Bool                // the thread is in fast mode
    let replies: [MailboxMessage] // the owner's replies in the thread that have a state, oldest first
    @Environment(\.zoom) private var z

    /// Which key sends a reply: Return in fast mode, else ⌘↩.
    static func keys(fast: Bool) -> String { fast ? "↩ sends, ⇧↩ makes a new line" : "⌘↩ sends" }

    var body: some View {
        if replies.isEmpty {
            Text(Self.keys(fast: fast) + (to.map { ", in answer to \($0)'s last mail" } ?? ""))
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
/// archived, snoozed, marked, answered and put back by Undo, against a stand-in desktop whose requests wait until the
/// test answers them (so it picks the order they come back in). Offline; it writes one attachment to a temporary
/// folder and removes it.
@MainActor
enum MailTest {
    static func run() -> Int32 {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append(what) } }
        rules(check)
        model(check)
        closing(check)
        reading(check)
        undo(check)
        popouts(check)
        board(check)
        gmailLink(check)
        dots(check)
        colours(check)
        edge(check)
        ahead(check)
        drafts(check)
        shared(check)
        answers(check)
        sharedCases(check)
        fast(check)
        researcher(check)
        if fails.isEmpty { print("mail selftest: ok"); return 0 }
        fails.forEach { print("FAIL: " + $0) }
        return 1
    }

    static func researcher(_ check: (Bool, String) -> Void) {
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }
        let desk = Desk()
        let mail = desk.model(folder: folder, upload: { _, _ in })
        let a = thread("A", newest: 1, last: 10)
        let b = thread("B", newest: 2, last: 20)
        func mode(_ row: [String: Any], on: Bool, tag: String, delivery: String,
                  deliveryDetail: String? = nil) -> [String: Any] {
            var researcher: [String: Any] = ["agent": "alpha", "on": on, "tag": tag, "delivery": delivery]
            researcher["delivery_detail"] = deliveryDetail
            return row.merging(["researcher": researcher]) { $1 }
        }
        let aOff = mode(a, on: false, tag: "alpha001", delivery: "applied")
        let aOn = mode(a, on: true, tag: "alpha001", delivery: "pending")
        let bOff = mode(b, on: false, tag: "alpha001", delivery: "applied")
        let bOn = mode(b, on: true, tag: "alpha001", delivery: "error")
        let unavailable = mode(b, on: true, tag: "alpha001", delivery: "unavailable",
                               deliveryDetail: "Refresh the Researcher 0.7+ plugin on mac.")
        let newOwner = mode(a, on: false, tag: "alpha002", delivery: "applied")
        func toggle(_ id: String) {
            if let displayed = mail.current(id)?.researcher { mail.toggleResearcher(id, from: displayed) }
        }
        let aShow = Desk.show(aOff, Desk.messages(["s1"]))
        mail.refresh()
        desk.answer("mail_list", Desk.list([aOff]))
        mail.open("A")
        desk.answer("mail_show", thread: "A", aShow)
        mail.popOut("A")
        mail.popOut("B")
        desk.answer("mail_show", thread: "B", Desk.show(bOff, Desk.messages(["s2"])))
        toggle("A")
        toggle("B")
        check(desk.waiting == ["mail_list", "mail_researcher"] && mail.current("A")?.researcher?.on == false,
              "researcher awaits confirmation and allows one switch at a time: \(desk.waiting)")
        let request = desk.answer("mail_researcher", ["ok": true, "thread": aOn])
        check(request["thread"] as? String == "A" && request["on"] as? Bool == true
              && request["expected_tag"] as? String == "alpha001", "researcher switches the rendered owner of A on")
        check(mail.current("A")?.researcher?.on == true && mail.shown?.thread.researcher?.on == true
              && mail.popouts["A"]?.shown?.thread.researcher?.on == true, "researcher shows the result in both readers")
        check(mail.current("A")?.researcher?.deliveryNote == "Delivery pending.", "saved mode is awaiting delivery")
        desk.answer("mail_list", Desk.list([aOff]))
        check(mail.current("A")?.researcher?.on == true, "an older poll cannot undo the switch")
        desk.answer("mail_show", thread: "B", Desk.show(bOn, Desk.messages(["s2"])))
        check(mail.popouts["B"]?.shown?.thread.researcher?.on == true, "a sibling popout outside the list reads its own state")
        check(mail.current("B")?.researcher?.deliveryNote == "Delivery failed.", "a refreshed reader knows delivery failed")
        desk.answer("mail_list", Desk.list([aOn]))
        desk.answer("mail_show", thread: "B", Desk.show(unavailable, Desk.messages(["s2"])))
        check(mail.current("B")?.researcher?.deliveryNote == "Delivery failed. Refresh the Researcher 0.7+ plugin on mac.",
              "an unavailable delivery shows the preflight message after refresh")

        toggle("A")
        let refused = desk.answer("mail_researcher", ["ok": false, "error": "Researcher owner changed. Refresh the thread."])
        check(refused["expected_tag"] as? String == "alpha001", "a stale click targets the old owner")
        check(mail.current("A")?.researcher?.on == true && mail.researcherUnconfirmed.contains("A")
              && mail.notice?.contains("owner changed") == true, "a changed owner requires a new read")
        toggle("A")
        check(!desk.waiting.contains("mail_researcher"), "the switch waits for a fresh route after refusal")
        desk.answer("mail_list", newest: true, Desk.list([newOwner]))
        check(mail.current("A")?.researcher?.tag == "alpha002", "a fresh list supplies the new owner's identity")
        mail.refresh()
        desk.answer("mail_list", newest: true, Desk.list([a]))
        check(mail.current("A")?.researcher == nil && mail.popouts["A"]?.shown?.thread.researcher == nil,
              "an older mailroom's list hides the unavailable switch in tab and popout")
        toggle("A")
        check(!desk.waiting.contains("mail_researcher") && desk.problems.isEmpty,
              "an unavailable route sends no command: \(desk.problems)")
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

        // what Undo sends: the box the thread was in, then its read state, from the desktop's row after the change
        func seen(_ t: MailboxThread) -> MailboxThread { var r = t; r.unread = false; return r }
        let open = seen(row("o")), unopened = row("u")                       // read, and unread, in the inbox
        let undone = { (before: MailboxThread, op: MailOp, until: Double?) in
            MailRules.undoing(before, from: MailRules.changed(before, op, until: until))
        }
        check(undone(open, .archive, nil) == [MailMark(op: .inbox)], "an archive: back to the inbox")
        check(undone(open, .snooze, 9) == [MailMark(op: .inbox)], "a snooze: back to the inbox, the snooze gone")
        check(undone(open, .unread, nil) == [MailMark(op: .read, upto: 1)], "marked unread: read again, as far as before")
        check(undone(unopened, .read, nil) == [MailMark(op: .unread)], "marked read: unread again")
        check(undone(seen(row("a", .archived)), .inbox, nil) == [MailMark(op: .archive, upto: 1)],
              "out of Archived: archived again")
        check(undone(seen(row("s", .snoozed)), .inbox, nil) == [MailMark(op: .snooze, upto: 1, until: 5)],
              "out of Snoozed: snoozed again, until the same time")
        check(undone(seen(row("s", .snoozed)), .snooze, 9) == [MailMark(op: .snooze, upto: 1, until: 5)],
              "snoozed again until another time: back to the first time")
        var woke = unopened                                                  // unread as it woke from a snooze
        woke.snoozeUntil = nil
        var wokeArchived = seen(woke)
        wokeArchived.box = MailBox.archived.rawValue                         // the archive ended the snooze, and its unread
        check(MailRules.undoing(woke, from: wokeArchived) == [MailMark(op: .inbox), MailMark(op: .unread)],
              "an archive that also made it read: back to the inbox, then unread")
        check(MailRules.undoing(open, from: open) == [], "nothing changed: nothing to send")
        var nowhere = open
        nowhere.box = "elsewhere"
        check(MailRules.undoing(nowhere, from: open) == nil, "a box no mark puts a thread back in: no Undo")
        let asleep = seen(row("s", .snoozed))                                // snoozed until 5
        check(MailRules.undone(asleep, now: 4) == asleep && MailRules.undone(open, now: 1e12) == open,
              "put back as it was: a snooze still ahead, a thread not snoozed")
        let woken = MailRules.undone(asleep, now: 5)
        check(woken.box == "inbox" && woken.snoozeUntil == nil && woken.unread == asleep.unread,
              "a snooze since past: put back in the inbox, where it would be by now")
        check(MailRules.undoing(woken, from: MailRules.changed(asleep, .inbox, until: nil)) == [],
              "and nothing to send when it is there already")

        // the list Undo shows: the thread back in its place by time, or out of a box it no longer belongs to
        func dated(_ id: String, _ last: Double, _ box: MailBox = .inbox) -> MailboxThread {
            var t = row(id, box)
            t.last = last
            return t
        }
        let listed = [dated("n", 30), dated("o", 10)]
        check(ids(MailRules.restoring(dated("m", 20), in: listed, box: .inbox)) == ["n", "m", "o"],
              "back in the inbox, in its place")
        check(ids(MailRules.restoring(dated("p", 5), in: listed, box: .inbox)) == ["n", "o", "p"], "the oldest: at the end")
        check(ids(MailRules.restoring(dated("m", 20, .snoozed), in: listed, box: .inbox)) == ["n", "o"],
              "not into a box it was not in")
        check(ids(MailRules.restoring(dated("o", 10, .archived), in: listed, box: .inbox)) == ["n"], "out of a box it has left")
        let back = MailRules.restoring(seen(dated("o", 10)), in: listed, box: .inbox)
        check(ids(back) == ["n", "o"] && !back[1].unread, "listed: put back in its place, as it was")
        check(ids(MailRules.restoring(dated("m", 20), in: listed, box: nil)) == ["n", "o"]
              && ids(MailRules.restoring(dated("m", 20), in: listed, box: .all)) == ["n", "o"],
              "a search or All lists only what the desktop found")
        let searched = MailRules.restoring(dated("o", 10, .archived), in: listed, box: nil)
        check(ids(searched) == ["n", "o"] && searched[1].box == "archived", "a search keeps it, as it was")

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
            MailKeys.command(chars, keyCode: code, flags: flags, typing: typing, list: true)
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
        let z = UInt16(kVK_ANSI_Z)
        check(key("z", z, [], typing: false) == .undo && key("z", z, .command, typing: false) == .undo, "z and ⌘Z undo")
        check(key("z", z, .command, typing: true) == nil && key("z", z, [], typing: true) == nil,
              "in a text box, ⌘Z is the box's own undo, and z types")
        check(key("z", z, [.command, .shift], typing: false) == nil && key("Z", z, .shift, typing: false) == nil
              && key("z", z, [.command, .option], typing: false) == nil, "⇧⌘Z, ⇧z and ⌥⌘Z are not the tab's")
        let esc = UInt16(kVK_Escape)
        check(key("\u{1b}", esc, [], typing: true) == .release && key("\u{1b}", esc, [], typing: false) == nil,
              "Esc stops the typing, and is left alone otherwise")
        func windowKey(_ chars: String, _ code: UInt16, _ flags: NSEvent.ModifierFlags) -> MailKey? {
            MailKeys.command(chars, keyCode: code, flags: flags, typing: false, list: false)
        }
        check(windowKey(MailKeys.down, 125, arrow) == nil && windowKey("k", 40, []) == nil
              && windowKey(MailKeys.right, 124, arrow) == .message(1) && windowKey("e", 14, []) == .archive
              && windowKey("z", z, []) == .undo && windowKey(" ", 49, []) == .page(1) && windowKey("r", 15, []) == .reply,
              "a thread's own window: no list, so ↓ and k are left to the document; the rest are the tab's")
        let v = UInt16(kVK_ANSI_V)
        check(key("r", 15, [], typing: false) == .reply && key("r", 15, [], typing: true) == nil, "r replies, unless typing")
        check(key("v", v, .command, typing: false) == .paste, "⌘V with no text box: files into the reply")
        check(key("v", v, .command, typing: true) == nil && key("v", v, [.command, .shift], typing: false) == nil
              && key("c", 8, .command, typing: false) == nil, "⌘V in a text box is its own; ⇧⌘V and ⌘C are not the tab's")
        let u = UInt16(kVK_ANSI_U), s = UInt16(kVK_ANSI_S)
        check(key("u", u, [], typing: false) == .unread && key("s", s, [], typing: false) == .snooze
              && windowKey("u", u, []) == .unread && windowKey("s", s, []) == .snooze,
              "u marks unread and s opens the Snooze menu, in the tab and in a thread's own window")
        check(key("u", u, [], typing: true) == nil && key("s", s, [], typing: true) == nil, "in a text box, u and s type")
        check(key("u", u, .command, typing: false) == nil && key("S", s, .shift, typing: false) == nil
              && key("s", s, .option, typing: false) == nil && key("u", u, .control, typing: false) == nil
              && key("s", s, .command, typing: false) == nil, "⌘U, ⇧S, ⌥S, ⌃U and ⌘S are not the tab's")

        // the Snooze menu, the Snooze button's and s's alike: each preset with its time, then Pick date & time; as an
        // AppKit menu (s's), the same titles, a line before the last, and each item doing what the button's does
        let wednesday = at("2026-11-18T14:20:00Z")
        let items = Snooze.items(now: wednesday, calendar: cal)
        check(items.map(\.title) == ["Later today · 17:00", "Tomorrow · Thu 08:00", "This weekend · Sat 08:00",
                                     "Next week · Mon 08:00", "Pick date & time…"],
              "the Snooze menu's items: \(items.map(\.title))")
        check(items.dropLast().map(\.date) == Snooze.presets(now: wednesday, calendar: cal).map(\.date)
              && items.last?.date == nil, "the presets' times, then none for Pick date & time")
        var snoozed: [Date] = [], picks = 0
        let menu = Snooze.menu(items, snooze: { snoozed.append($0) }, pick: { picks += 1 })
        check(menu.items.filter { !$0.isSeparatorItem }.map(\.title) == items.map(\.title)
              && menu.items.firstIndex(where: \.isSeparatorItem) == items.count - 1, "s's menu: the same items, a line "
              + "before Pick date & time: \(menu.items.map { $0.isSeparatorItem ? "—" : $0.title })")
        if menu.items.count == items.count + 1 {
            _ = NSApplication.shared                      // AppKit sends a menu item's action through the app (NSApp)
            menu.performActionForItem(at: 1)
            menu.performActionForItem(at: items.count)
        }
        let second = items.dropFirst().first?.date
        check(!snoozed.isEmpty && snoozed == [second].compactMap { $0 } && picks == 1,
              "its items snooze until their time, and Pick date & time asks for one: \(snoozed) \(picks)")

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

    /// The desktop the model tests stand in for. The MailModel it makes asks it, and each request waits in `asked` until
    /// the test answers it, so the test picks the order the answers come in. Answering a request that was not asked is
    /// a problem, kept in `problems` for the test to check.
    final class Desk {
        var asked: [(request: [String: Any], done: @MainActor ([String: Any]?, String?) -> Void)] = []
        private(set) var problems: [String] = []
        /// The desktop's drafts, for a model that keeps its drafts there: each draft request is answered from them a
        /// moment later, rather than waiting in `asked`.
        private(set) var drafts: DeskDrafts?

        /// A model asking this desk, keeping drafts in `folder` (and in `drafts`, the desktop's, when given, saved
        /// there MailTest.saveDelay after a change), with `upload` its way for files too big for a reply, which marks a
        /// thread read after the tests' second on screen (MailTest.readSeconds).
        @MainActor func model(folder: URL, upload: @escaping MailModel.Upload, ahead: Int = 0,
                              undoSeconds: TimeInterval = MailModel.appUndoSeconds, drafts: DeskDrafts? = nil) -> MailModel {
            self.drafts = drafts
            return MailModel(call: { [unowned self] request, done in
                if let kept = self.drafts, (request["action"] as? String)?.hasPrefix("draft_") == true {
                    kept.asked.append(request)
                    kept.later { let (obj, err) = kept.answer(request); done(obj, err) }
                } else {
                    self.asked.append((request, done))
                }
            }, upload: upload, ahead: ahead, cacheBytes: 1 << 20, draftsFolder: folder, undoSeconds: undoSeconds,
               readSeconds: MailTest.readSeconds, syncDrafts: drafts != nil, saveDelay: MailTest.saveDelay)
        }

        /// The actions asked and not answered, oldest first.
        var waiting: [String] { asked.map { $0.request["action"] as? String ?? "?" } }

        /// The threads mail_show is asked for and not answered, oldest first.
        var shows: [String] {
            asked.compactMap { ($0.request["action"] as? String) == "mail_show" ? $0.request["thread"] as? String : nil }
        }

        /// Answer the oldest request for `action` (with `newest`, the newest), on `thread` when given, with `obj` or the
        /// error `err`; that request.
        @MainActor @discardableResult
        func answer(_ action: String, thread: String? = nil, newest: Bool = false, _ obj: [String: Any]?,
                    err: String? = nil) -> [String: Any] {
            let matching = asked.indices.filter {
                (asked[$0].request["action"] as? String) == action
                    && (thread == nil || (asked[$0].request["thread"] as? String) == thread)
            }
            guard let i = newest ? matching.last : matching.first else {
                problems.append("no \(action) request\(thread.map { " for " + $0 } ?? "") to answer (waiting: \(waiting))")
                return [:]
            }
            let (request, done) = asked.remove(at: i)
            done(obj, err)
            return request
        }

        /// mail_list's answer: these threads, counted as the inbox.
        static func list(_ threads: [[String: Any]]) -> [String: Any] {
            ["ok": true, "threads": threads, "counts": ["inbox": threads.count, "unread": 0, "snoozed": 0]]
        }

        /// mail_show's answer: thread `t` with `messages`, and `document` (a paragraph of its id unless given).
        static func show(_ t: [String: Any], _ messages: [[String: Any]], document: String? = nil) -> [String: Any] {
            ["ok": true, "thread": t, "messages": messages, "document": document ?? "<p>\(t["id"] ?? "")</p>"]
        }

        /// One message of mail_show's answer: an agent's (mailtab's), or with `mine` the owner's, sent at `ts`.
        static func message(_ key: String, mine: Bool = false, ts: Double = 1, unread: Bool = false,
                            status: String? = nil) -> [String: Any] {
            ["key": key, "from": mine ? "Alex" : "mailtab", "mine": mine, "ts": ts, "unread": unread,
             "status": status.map { $0 as Any } ?? NSNull()]
        }

        /// Agents' messages of these keys.
        static func messages(_ keys: [String]) -> [[String: Any]] { keys.map { message($0) } }
    }

    /// The tests' wait after a change before a draft is saved on the desktop (the app's is MailDrafts.appSaveDelay).
    static let saveDelay: TimeInterval = 0.2

    /// The desktop's drafts, as agentboard/maildrafts.py keeps them, for the rules the Mac meets: a rev from one count,
    /// a save from a rev that is not the current one (or changing a draft that holds a key, but for the key going from
    /// a device that has seen it) not taken and kept as a version, Discard kept as one, a version brought back with the draft it replaces kept, files
    /// kept by their digest and to be there before a draft names them, and a reply sent with the draft's key emptying
    /// it. (The desktop's versions of earlier and emptied drafts are tested there: tests/test_maildrafts.py.)
    @MainActor
    final class DeskDrafts {
        struct Version {
            var id: String
            var content: DraftSnapshot
            var why: String
            var device: String?
        }
        struct Kept {
            var rev = 0
            var draft: DraftSnapshot?
            var device: String?
            var versions: [Version] = []          // oldest first
        }
        private var count = 0
        private(set) var threads: [String: Kept] = [:]
        private(set) var files: [String: Data] = [:]
        /// Why every request fails, while the desktop cannot be reached.
        var down: String?
        /// Every draft request, in order.
        var asked: [[String: Any]] = []
        /// While true, the requests wait to be answered until release().
        var holding = false
        private var held: [() -> Void] = []

        /// Answer a request a moment from now, or once released while holding.
        func later(_ answer: @escaping () -> Void) {
            if holding { return held.append(answer) }
            DispatchQueue.main.async { MainActor.assumeIsolated { answer() } }
        }

        /// Answer the requests held, in order, and hold no more.
        func release() {
            holding = false
            let waiting = held
            held = []
            waiting.forEach { later($0) }
        }

        /// The draft_put requests asked.
        var puts: [[String: Any]] { asked.filter { $0["action"] as? String == "draft_put" } }

        /// A change made on another device (a laptop): thread `id`'s draft now `s`, files `data` kept first.
        func save(_ id: String, _ s: DraftSnapshot?, device: String = "laptop", data: [Data] = []) {
            data.forEach { files[MailDrafts.digest($0)] = $0 }
            set(id, s, device)
        }

        /// A reply with key `key` reached the mailroom: the draft holding it goes (maildrafts.Store.sent); its rev, or
        /// nil when the draft holds another key.
        func sent(_ id: String, key: String) -> Int? {
            guard let k = threads[id], k.draft?.key == key else { return nil }
            set(id, nil, nil)
            return threads[id]?.rev
        }

        private func set(_ id: String, _ s: DraftSnapshot?, _ device: String?) {
            count += 1
            var k = threads[id] ?? Kept()
            k.rev = count
            k.draft = s.flatMap { $0.blank && $0.key == nil ? nil : $0 }
            k.device = k.draft == nil ? nil : device
            threads[id] = k
        }

        private func keep(_ id: String, _ s: DraftSnapshot?, _ why: String, _ device: String?) -> String? {
            guard var c = s, !c.blank else { return nil }
            c.key = nil
            let v = Version(id: String(UUID().uuidString.lowercased().filter(\.isHexDigit).prefix(12)), content: c, why: why,
                            device: device)
            threads[id, default: Kept()].versions.removeAll { $0.content == c }
            threads[id, default: Kept()].versions.append(v)
            return v.id
        }

        private func view(_ id: String, _ more: [String: Any] = [:]) -> [String: Any] {
            let k = threads[id] ?? Kept()
            let versions: [[String: Any]] = k.versions.reversed().map { v in
                ["id": v.id, "at": 1.0, "device": null(v.device), "why": v.why, "kept": 1.0,
                 "words": String(v.content.text.split(whereSeparator: \.isWhitespace).joined(separator: " ").prefix(80)),
                 "picks": v.content.picks.count, "comments": v.content.comments.count, "files": v.content.files.count]
            }
            return ["ok": true, "thread": id, "rev": k.rev, "draft": null(k.draft.flatMap(MailDrafts.json)),
                    "updated": k.draft == nil ? NSNull() : ["at": 1.0, "device": null(k.device)] as [String: Any],
                    "versions": versions].merging(more) { $1 }
        }

        /// `x`, or JSON's null.
        private func null(_ x: Any?) -> Any { x ?? NSNull() }

        /// The desktop's answer to draft request `r`, or the error it fails with.
        func answer(_ r: [String: Any]) -> ([String: Any]?, String?) {
            if let why = down { return (nil, why) }
            let id = r["thread"] as? String ?? "", base = r["base_rev"] as? Int ?? -1, device = r["device"] as? String
            let k = threads[id] ?? Kept()
            func unkeyed(_ s: DraftSnapshot?) -> DraftSnapshot? { s.map { var c = $0; c.key = nil; return c } }
            switch r["action"] as? String ?? "" {
            case "draft_list":
                let held = threads.filter { !($0.value.draft?.blank ?? true) }.mapValues { ["rev": $0.rev] as [String: Any] }
                return (["ok": true, "drafts": held], nil)
            case "draft_get":
                return (view(id), nil)
            case "draft_file_add":
                guard let data = Data(base64Encoded: r["data"] as? String ?? "") else { return (["ok": false, "error": "not base64"], nil) }
                files[MailDrafts.digest(data)] = data
                return (["ok": true, "file": ["id": MailDrafts.digest(data), "size": data.count]], nil)
            case "draft_put":
                guard let body = r["draft"], let json = try? JSONSerialization.data(withJSONObject: body),
                      let s = try? JSONDecoder().decode(DraftSnapshot.self, from: json) else {
                    return (["ok": false, "error": "a draft is {text, picks, comments, files, key}"], nil)
                }
                let same = DraftSnapshot.same(unkeyed(s), unkeyed(k.draft))
                let frozen = k.draft?.key != nil && (!same || (s.key != nil && s.key != k.draft?.key)
                                                     || (s.key == nil && base != k.rev))
                if (base != k.rev && !same) || frozen {
                    let kept = same ? nil : keep(id, s, "conflict", device)
                    return (view(id, ["conflict": true, "kept": null(kept)]), nil)
                }
                if let missing = s.files.first(where: { files[$0.id] == nil }) {
                    return (["ok": false, "error": "\(missing.name) is not on the desktop; attach it again"], nil)
                }
                if s != k.draft { set(id, s, device) }
                return (view(id), nil)
            case "draft_discard":
                guard base == k.rev else { return (view(id, ["conflict": true]), nil) }
                let v = keep(id, k.draft, "discarded", k.device)
                if k.draft != nil { set(id, nil, device) }
                return (view(id, ["version": null(v)]), nil)
            case "draft_restore":
                guard let v = k.versions.first(where: { $0.id == r["version"] as? String }) else {
                    return (["ok": false, "error": "that version of this draft is no longer kept"], nil)
                }
                guard base == k.rev else { return (view(id, ["conflict": true]), nil) }
                if unkeyed(k.draft) != v.content { _ = keep(id, k.draft, "replaced", k.device) }
                set(id, v.content, device)
                return (view(id), nil)
            default:
                return (["ok": false, "error": "unknown action"], nil)
            }
        }
    }

    /// The tests' second on screen before an unread thread is marked read (the model's readSeconds; the app's is
    /// MailModel.appReadSeconds), shorter so they run quickly.
    static let readSeconds: TimeInterval = 0.15

    /// Let `seconds` pass on the main run loop, whose timers fire meanwhile (the read mark's, the Undo bar's).
    static func pass(_ seconds: TimeInterval) { RunLoop.main.run(until: Date().addingTimeInterval(seconds)) }

    /// mail_mark's answer: the thread `t` with `changes`, as the desktop has it after the mark.
    static func marked(_ t: [String: Any], _ changes: [String: Any] = [:]) -> [String: Any] {
        ["ok": true, "threads": [t.merging(changes) { $1 }]]
    }

    /// A thread as mail_list and mail_show give it, about `subject` ("about ID" when nil).
    static func thread(_ id: String, unread: Bool = false, newest: Int, count: Int = 1, last: Double,
                       box: MailBox = .inbox, subject: String? = nil) -> [String: Any] {
        ["id": id, "subject": subject ?? "about " + id, "agents": ["mailtab (claude)"], "snippet": "…", "last": last,
         "count": count, "unread": unread, "box": box.rawValue, "snooze_until": NSNull(), "newest": newest,
         "attachments": false]
    }

    static func model(_ check: (Bool, String) -> Void) {
        let desk = Desk()
        defer { desk.problems.forEach { check(false, $0) } }
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        var copied: [URL] = []                                             // nothing is copied to the desktop here
        let mail = desk.model(folder: folder, upload: { file, _ in copied.append(file) })
        let a = thread("A", unread: true, newest: 50, count: 2, last: 500), aRead = thread("A", newest: 50, count: 2, last: 500)
        let b = thread("B", newest: 40, last: 400), c = thread("C", newest: 30, last: 300), d = thread("D", newest: 20, last: 200)

        mail.refresh()
        let first = desk.answer("mail_list", Desk.list([a, b, c, d]))
        check(first["box"] as? String == "inbox" && first["query"] == nil && first["limit"] as? Int == MailModel.listLimit,
              "the inbox is listed first, the newest \(MailModel.listLimit): \(first)")
        check(mail.threads?.map(\.id) == ["A", "B", "C", "D"], "the list as the desktop sorts it")

        // ↓ with nothing open opens the first thread; an unread one is marked read up to its newest message once it has
        // been on screen for its second (reading has the rest)
        mail.perform(.thread(1))
        check(desk.waiting == ["mail_show"], "↓ opens the first thread: \(desk.waiting)")
        desk.answer("mail_show", Desk.show(a, [Desk.message("s49"), Desk.message("s50", unread: true)]))
        check(mail.current == "s50", "it opens at the first unread message")
        check(desk.waiting.isEmpty && mail.threads?.first?.unread == true, "not read before its second on screen")
        pass(readSeconds * 2)
        check(mail.threads?.first?.unread == false, "then read in the list")
        let read = desk.answer("mail_mark", marked(aRead))
        check(read["op"] as? String == "read" && read["upto"] as? Int == 50 && read["threads"] as? [String] == ["A"],
              "marked read up to its newest: \(read)")
        desk.answer("mail_list", Desk.list([aRead, b, c, d]))

        // ↓ and ↑ step through; a thread read before and unchanged in the list shows at once, at its newest message
        // now that it is read, and is not marked again
        mail.perform(.thread(1))
        check(mail.selected == "B", "↓: the next one down")
        desk.answer("mail_show", Desk.show(b, [Desk.message("s40")]))
        mail.perform(.thread(-1))
        check(mail.selected == "A" && mail.shown?.thread.id == "A" && mail.current == "s50", "↑: the one above, kept")
        check(mail.shown?.thread.unread == false, "with the list's flags, not those it was kept with")
        check(desk.waiting.isEmpty, "nothing asked: \(desk.waiting)")

        // e archives the open thread up to its newest message and closes it, opening no other (closing has the rest);
        // ↓ then opens the thread that was below it, kept already
        mail.perform(.archive)
        check(desk.waiting == ["mail_mark"] && mail.tab == nil, "e: archive, and no thread open: \(desk.waiting)")
        check(mail.threads?.map(\.id) == ["B", "C", "D"], "it leaves the inbox at once")
        let archived = desk.answer("mail_mark", marked(aRead, ["box": "archived"]))
        check(archived["op"] as? String == "archive" && archived["upto"] as? Int == 50 && archived["until"] == nil
              && archived["threads"] as? [String] == ["A"], "archived up to its newest: \(archived)")
        desk.answer("mail_list", Desk.list([b, c, d]))
        mail.perform(.thread(1))
        check(mail.selected == "B" && mail.shown?.thread.id == "B" && desk.waiting.isEmpty,
              "↓: the thread that was below it, at once: \(desk.waiting)")

        // a list asked before an archive and answered after it is out of date
        mail.refresh()
        mail.archiveOrRestore("C")
        check(mail.selected == "B", "archiving a thread from the list leaves the open one open")
        desk.answer("mail_list", Desk.list([b, c, d]))
        check(mail.threads?.map(\.id) == ["B", "D"], "the older list does not bring it back")
        desk.answer("mail_mark", marked(c, ["box": "archived"]))
        desk.answer("mail_list", Desk.list([b, d]))
        check(mail.threads?.map(\.id) == ["B", "D"], "the list after the archive")

        // a refused mark says so; the list is read again to undo what was shown
        mail.markUnread()
        check(mail.selected == nil, "Mark unread closes the thread")
        let unread = desk.answer("mail_mark", nil, err: "refused inside an agent session")
        check(unread["op"] as? String == "unread" && unread["upto"] == nil, "unread names no message: \(unread)")
        check(mail.notice == "Not marked unread: refused inside an agent session", "the refusal is shown: \(mail.notice ?? "none")")
        check(desk.waiting == ["mail_list"], "the list again after a refusal")
        desk.answer("mail_list", Desk.list([b, d]))

        // the open thread is read again only when the list shows it changed, or a reply of the owner's is on its way
        mail.open("D")
        desk.answer("mail_show", Desk.show(d, [Desk.message("s20")]))
        mail.refresh()
        desk.answer("mail_list", Desk.list([b, d]))
        check(desk.waiting.isEmpty, "unchanged: not read again: \(desk.waiting)")
        mail.refresh()
        desk.answer("mail_list", Desk.list([b, thread("D", newest: 21, count: 2, last: 210)]))
        check(desk.waiting == ["mail_show"], "a new message: read again: \(desk.waiting)")
        desk.answer("mail_show", Desk.show(thread("D", newest: 21, count: 2, last: 210), [Desk.message("s20"), Desk.message("s21")]))

        // a reply: its key is kept until the desktop has it, so a retry cannot send it twice
        let draft = mail.draft(for: "D")
        draft.text = "  Yes, merge it.\n"
        mail.send("D")
        let try1 = desk.answer("mail_reply", nil, err: "the connection dropped before it answered")
        check(try1["text"] as? String == "Yes, merge it." && try1["thread"] as? String == "D", "the reply, trimmed: \(try1)")
        check(draft.error == "Not sent: the connection dropped before it answered" && draft.text == "  Yes, merge it.\n",
              "a failed send keeps the text and says why")
        mail.send("D")
        let try2 = desk.answer("mail_reply", ["ok": true, "key": "i9", "status": "sending"])
        check((try2["key"] as? String) != nil && try2["key"] as? String == try1["key"] as? String, "the retry has the same key")
        check(draft.text.isEmpty && draft.key == nil && draft.error == nil, "sent: the box is empty, the key gone")
        check(desk.waiting == ["mail_show", "mail_list"], "sent: the thread and the list again: \(desk.waiting)")
        let dReplied = thread("D", newest: 21, count: 3, last: 210)
        desk.answer("mail_show", Desk.show(dReplied, [Desk.message("s20"), Desk.message("s21"), Desk.message("i9", mine: true, status: "sending")]))
        check(mail.current == "i9", "the thread comes back at the reply")
        desk.answer("mail_list", Desk.list([b, dReplied]))
        check(desk.waiting == ["mail_show"], "a reply on its way: the thread is read again: \(desk.waiting)")
        desk.answer("mail_show", Desk.show(dReplied, [Desk.message("s20"), Desk.message("s21"), Desk.message("i9", mine: true, status: "delivered")]))
        mail.refresh()
        desk.answer("mail_list", Desk.list([b, dReplied]))
        check(desk.waiting.isEmpty, "delivered: not read again: \(desk.waiting)")
        draft.text = "And the next one."
        mail.send("D")
        let try3 = desk.answer("mail_reply", ["ok": true, "key": "i10", "status": "sending"])
        check(try3["key"] as? String != try1["key"] as? String, "a new reply has a new key")
        desk.answer("mail_show", Desk.show(dReplied, []))
        desk.answer("mail_list", Desk.list([b, dReplied]))
        draft.text = String(repeating: "x", count: MailModel.maxReply + 1)
        mail.send("D")
        check(desk.waiting.isEmpty && (draft.error ?? "").hasPrefix("Too long to send"), "too long: refused here: \(draft.error ?? "none")")

        // a reply with a file and no text: the file goes as base64, kept until the desktop has it
        draft.text = ""
        let png = Data([0x89, 0x50, 0x4E, 0x47, 1, 2, 3])
        check(mail.drafts.add([.image(png)], to: "D") == .added, "a pasted image is taken")
        let file = draft.files.first
        mail.send("D")
        let fails = desk.answer("mail_reply", nil, err: "ssh: connection closed")
        let sentFiles = fails["files"] as? [[String: String]]
        check(fails["text"] as? String == "" && sentFiles?.count == 1 && sentFiles?.first?["data"] == png.base64EncodedString()
              && (sentFiles?.first?["name"] ?? "").hasSuffix(".png"), "the file as base64, with its name: \(fails)")
        check(draft.files == [file].compactMap { $0 } && draft.shown, "a failed send keeps the file, and the box")
        mail.send("D")
        desk.answer("mail_reply", ["ok": true, "key": "i11", "status": "sending"])
        check(draft.files.isEmpty && !draft.shown, "sent: the file leaves the draft, and the empty box closes")
        check(file.map { !FileManager.default.fileExists(atPath: mail.drafts.url($0).path) } ?? false,
              "and its copy is gone")
        desk.answer("mail_show", Desk.show(dReplied, []))
        desk.answer("mail_list", Desk.list([b, dReplied]))

        // snooze: up to the newest message, until the time chosen; it closes too, and ↑ opens the thread above it
        let wake = Date(timeIntervalSince1970: 1_900_000_000)
        mail.snooze(until: wake)
        check(desk.waiting == ["mail_mark"] && mail.tab == nil, "snoozed: no thread open: \(desk.waiting)")
        let snoozed = desk.answer("mail_mark", marked(dReplied, ["box": "snoozed", "snooze_until": wake.timeIntervalSince1970]))
        check(snoozed["op"] as? String == "snooze" && snoozed["upto"] as? Int == 21
              && snoozed["until"] as? Double == wake.timeIntervalSince1970, "snoozed up to its newest, until then: \(snoozed)")
        desk.answer("mail_list", Desk.list([b]))
        mail.perform(.thread(-1))
        check(mail.selected == "B" && mail.shown?.thread.id == "B" && desk.waiting.isEmpty,
              "↑: the thread that was above it, kept: \(desk.waiting)")

        // Move to Inbox, from the Archived box: no message named, no time
        mail.show(.archived)
        let e = thread("E", newest: 5, last: 50, box: .archived)
        desk.answer("mail_list", Desk.list([e]))
        mail.archiveOrRestore("E")
        let back = desk.answer("mail_mark", marked(e, ["box": "inbox"]))
        check(back["op"] as? String == "inbox" && back["upto"] == nil && back["until"] == nil, "back to the inbox: \(back)")
        check(mail.threads?.isEmpty == true, "it leaves the Archived box at once")
        desk.answer("mail_list", Desk.list([]))
        mail.show(.inbox)
        desk.answer("mail_list", Desk.list([b]))

        // an agent's latest mail, opened from the board: the thread, and the box it is in listed
        mail.reveal("F")
        let archivedF = thread("F", newest: 7, last: 70, box: .archived)
        desk.answer("mail_show", Desk.show(archivedF, [Desk.message("s7")]))
        let fBox = desk.answer("mail_list", Desk.list([archivedF]))
        check(mail.selected == "F" && mail.listing == .archived && fBox["box"] as? String == "archived",
              "opened from the board: its box is listed: \(fBox)")
        check(mail.threads?.map(\.id) == ["F"], "and it is in the list")
        mail.show(.inbox)
        desk.answer("mail_list", Desk.list([b]))

        // a search covers every box
        mail.find("  merge it ")
        let search = desk.answer("mail_list", Desk.list([dReplied]))
        check(search["box"] as? String == "all" && search["query"] as? String == "merge it", "a search lists every box: \(search)")
        mail.find("")
        check((desk.answer("mail_list", Desk.list([b]))["query"]) == nil && mail.listing == .inbox, "an empty search goes back to the box")
        check(desk.waiting.isEmpty && copied.isEmpty, "nothing left unanswered, nothing copied: \(desk.waiting) \(copied)")
    }

    /// Archive, Snooze and Move to Inbox on the tab's open thread close it, opening no other ("No thread open"); ↓ (or
    /// j) then opens the thread that was below it in the list and ↑ (or k) the one above. Opened any other way, or with
    /// another box listed, that place is forgotten; a thread gone from the list since, or none there, leaves ↓ and ↑ as
    /// with nothing open: the first thread. (A thread's own window stays: popouts.)
    static func closing(_ check: (Bool, String) -> Void) {
        let desk = Desk()
        defer { desk.problems.forEach { check(false, $0) } }
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        let mail = desk.model(folder: folder, upload: { _, _ in })
        func listed() -> [String] { mail.threads?.map(\.id) ?? [] }
        func op(_ request: [String: Any]) -> String { "\(request["op"] ?? "-") \(request["threads"] ?? "-")" }
        let rows = ["A", "B", "C", "D", "E", "F", "G"].enumerated().map { i, id in
            thread(id, newest: 70 - 10 * i, last: Double(700 - 100 * i))
        }
        let (a, b, c, d, e, f, g) = (rows[0], rows[1], rows[2], rows[3], rows[4], rows[5], rows[6])
        /// mail_show's answer for thread `t`, one mail.
        func show(_ t: [String: Any]) {
            desk.answer("mail_show", thread: t["id"] as? String, Desk.show(t, Desk.messages(["s\(t["newest"] ?? "")"])))
        }
        mail.refresh()
        desk.answer("mail_list", Desk.list(rows))

        // e on C: archived and closed, no other thread opened; ↑ then opens B, the one above it
        mail.open("C")
        show(c)
        mail.perform(.archive)
        check(mail.tab == nil && listed() == ["A", "B", "D", "E", "F", "G"] && desk.waiting == ["mail_mark"],
              "e: C archived and closed, no other thread opened: \(listed()) \(desk.waiting)")
        check(op(desk.answer("mail_mark", marked(c, ["box": "archived"]))) == "archive [\"C\"]", "C's archive mark")
        desk.answer("mail_list", Desk.list([a, b, d, e, f, g]))
        mail.perform(.thread(-1))
        check(mail.selected == "B", "↑ then: B, the thread above it: \(mail.selected ?? "none")")
        show(b)

        // e on B, then ↓: D, the one below it
        mail.perform(.archive)
        check(mail.tab == nil, "B archived: closed")
        desk.answer("mail_mark", marked(b, ["box": "archived"]))
        desk.answer("mail_list", Desk.list([a, d, e, f, g]))
        mail.perform(.thread(1))
        check(mail.selected == "D", "↓ then: D, the thread below it: \(mail.selected ?? "none")")
        show(d)

        // Snooze on D closes it too; a thread opened by a click then forgets where D was: closed again by Unread, ↓
        // opens the first thread, not E
        let wake = Date(timeIntervalSince1970: 1_900_000_000)
        mail.snooze(until: wake)
        check(mail.tab == nil && listed() == ["A", "E", "F", "G"], "snoozed: closed: \(listed())")
        check(op(desk.answer("mail_mark", marked(d, ["box": "snoozed", "snooze_until": wake.timeIntervalSince1970])))
              == "snooze [\"D\"]", "D's snooze mark")
        desk.answer("mail_list", Desk.list([a, e, f, g]))
        mail.open("G")
        show(g)
        mail.markUnread()
        desk.answer("mail_mark", marked(g, ["unread": true]))
        desk.answer("mail_list", Desk.list([a, e, f, g]))
        mail.perform(.thread(1))
        check(mail.selected == "A", "opened by a click since: ↓ opens the first thread: \(mail.selected ?? "none")")
        show(a)

        // e on A; the thread below it, E, then archived elsewhere, gone from the list: ↓ opens the first thread
        mail.perform(.archive)
        desk.answer("mail_mark", marked(a, ["box": "archived"]))
        desk.answer("mail_list", Desk.list([f, g]))
        mail.perform(.thread(1))
        check(mail.selected == "F", "the thread below gone from the list: ↓ opens the first: \(mail.selected ?? "none")")
        show(f)

        // Move to Inbox on an archived thread open in the tab closes it too, and ↓ opens the one below it
        mail.show(.archived)
        let archived = [b, c, e, f].map { $0.merging(["box": "archived"]) { $1 } }
        desk.answer("mail_list", Desk.list(archived))
        mail.open("C")                                                    // kept: shown at once
        check(mail.shown?.thread.id == "C" && desk.waiting.isEmpty, "C, archived, open: \(desk.waiting)")
        mail.archiveOrRestore()
        check(mail.tab == nil && listed() == ["B", "E", "F"], "Move to Inbox: closed: \(listed())")
        check(op(desk.answer("mail_mark", marked(archived[1], ["box": "inbox"]))) == "inbox [\"C\"]", "C's inbox mark")
        desk.answer("mail_list", Desk.list([archived[0], archived[2], archived[3]]))
        mail.perform(.thread(1))
        check(mail.selected == "E", "↓: E, below it: \(mail.selected ?? "none")")
        show(archived[2])

        // E moved to the inbox too (F below it); another box listed in between forgets where it was: ↓ opens the first
        mail.archiveOrRestore()
        desk.answer("mail_mark", marked(archived[2], ["box": "inbox"]))
        desk.answer("mail_list", Desk.list([archived[0], archived[3]]))
        mail.show(.inbox)
        desk.answer("mail_list", Desk.list([c, e, f, g]))
        mail.show(.archived)
        desk.answer("mail_list", Desk.list([archived[0], archived[3]]))
        mail.perform(.thread(1))
        check(mail.selected == "B", "another box listed since: ↓ opens the first thread: \(mail.selected ?? "none")")
        check(desk.waiting.isEmpty, "nothing left unanswered: \(desk.waiting)")
    }

    /// The read mark: an unread thread is marked read once it has been on screen for its second (the app's is 1 s;
    /// the tests' readSeconds), in the tab or its own window, up to the newest mail on screen then; on screen in the
    /// tab and then its window, its second runs on. Left sooner, it stays unread and nothing is sent: another thread
    /// opened in its place, closed, archived, snoozed, marked unread (u, which closes it), its window closed, or
    /// archived in its window, which stays; marked read by the owner meanwhile, it is marked read once. A thread opened
    /// again, by Undo too, starts its second again.
    static func reading(_ check: (Bool, String) -> Void) {
        let desk = Desk()
        defer { desk.problems.forEach { check(false, $0) } }
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        let mail = desk.model(folder: folder, upload: { _, _ in })
        func op(_ request: [String: Any]) -> String {
            "\(request["op"] ?? "-") \(request["threads"] ?? "-") upto \(request["upto"] ?? "-")"
        }
        func unread(_ id: String) -> Bool { mail.current(id)?.unread == true }
        check(MailModel.appReadSeconds == 1, "the app marks a thread read after a second on screen")
        let a = thread("A", unread: true, newest: 50, count: 2, last: 500)
        let b = thread("B", unread: true, newest: 40, last: 400), c = thread("C", newest: 30, last: 300)
        let d = thread("D", unread: true, newest: 20, last: 200), e = thread("E", unread: true, newest: 10, last: 100)
        let f = thread("F", unread: true, newest: 5, last: 50), g = thread("G", unread: true, newest: 3, last: 30)
        let read = { (t: [String: Any]) in t.merging(["unread": false]) { $1 } }
        var rows = [a, b, c, d, e, f, g]
        /// The list again, as the desktop has it after a change: these rows.
        func relist() { desk.answer("mail_list", Desk.list(rows)) }
        mail.refresh()
        relist()

        // on screen for less than its second, A is not read; another thread opened in its place then: still unread
        mail.open("A")
        desk.answer("mail_show", Desk.show(a, [Desk.message("s49"), Desk.message("s50", unread: true)]))
        check(mail.shown?.thread.id == "A" && desk.waiting.isEmpty && unread("A"), "shown: not read at once")
        pass(readSeconds / 2)
        check(desk.waiting.isEmpty && unread("A"), "nor halfway through its second")
        mail.open("C")
        desk.answer("mail_show", Desk.show(c, Desk.messages(["s30"])))
        pass(readSeconds * 2)
        check(desk.waiting.isEmpty && unread("A"), "left for C before its second: unread, nothing sent: \(desk.waiting)")

        // closed before its second: unread
        mail.open("A")
        check(mail.shown?.thread.id == "A" && desk.waiting.isEmpty, "A again, kept: at once")
        mail.close()
        pass(readSeconds * 2)
        check(desk.waiting.isEmpty && unread("A"), "closed before its second: unread: \(desk.waiting)")

        // u within its second: marked unread, as the Unread button, and closed; no read mark after
        mail.open("A")
        mail.perform(.unread)
        check(mail.tab == nil, "u closes it")
        check(op(desk.answer("mail_mark", marked(a))) == "unread [\"A\"] upto -", "u: the unread mark")
        relist()
        pass(readSeconds * 2)
        check(desk.waiting.isEmpty && unread("A"), "and no read mark after it: \(desk.waiting)")

        // archived within its second: archived only; Undo opens it again, and its second starts again: then it is read,
        // up to its newest mail
        mail.open("A")
        mail.perform(.archive)
        check(op(desk.answer("mail_mark", marked(a, ["box": "archived"]))) == "archive [\"A\"] upto 50", "e: archived")
        rows.removeFirst()
        relist()
        pass(readSeconds * 2)
        check(desk.waiting.isEmpty, "archived before its second: no read mark: \(desk.waiting)")
        mail.undo()
        check(mail.shown?.thread.id == "A" && unread("A") && desk.waiting == ["mail_mark"], "Undo: A open again, unread")
        check(op(desk.answer("mail_mark", marked(a))) == "inbox [\"A\"] upto -", "Undo's mark: back to the inbox")
        rows.insert(a, at: 0)
        relist()
        check(desk.waiting.isEmpty, "not read before its second again: \(desk.waiting)")
        pass(readSeconds * 2)
        check(op(desk.answer("mail_mark", marked(read(a)))) == "read [\"A\"] upto 50" && !unread("A"),
              "then read, up to its newest mail")
        rows[0] = read(a)
        relist()

        // snoozed within its second: snoozed only
        mail.open("B")
        desk.answer("mail_show", Desk.show(b, Desk.messages(["s40"])))
        let wake = Date(timeIntervalSince1970: 1_900_000_000)
        mail.snooze(until: wake)
        let snoozed = desk.answer("mail_mark", marked(b, ["box": "snoozed", "snooze_until": wake.timeIntervalSince1970]))
        check(mail.tab == nil && op(snoozed) == "snooze [\"B\"] upto 40", "Snooze: snoozed and closed")
        rows.remove(at: 1)
        relist()
        pass(readSeconds * 2)
        check(desk.waiting.isEmpty, "snoozed before its second: no read mark: \(desk.waiting)")

        // marked read by the owner within its second (right-click): read once, by that mark
        mail.open("D")
        desk.answer("mail_show", Desk.show(d, Desk.messages(["s20"])))
        mail.markRead("D")
        check(op(desk.answer("mail_mark", marked(read(d)))) == "read [\"D\"] upto 20", "the owner's read mark")
        rows[2] = read(d)
        relist()
        pass(readSeconds * 2)
        check(desk.waiting.isEmpty, "and no second one after its second: \(desk.waiting)")

        // E in its own window: closed before its second, unread
        mail.popOut("E")
        desk.answer("mail_list", Desk.list(rows))                         // the poll that starts with a window
        desk.answer("mail_show", Desk.show(e, Desk.messages(["s10"])))
        check(mail.popouts["E"]?.shown?.thread.id == "E" && desk.waiting.isEmpty, "E in its window")
        mail.popIn("E")
        pass(readSeconds * 2)
        check(desk.waiting.isEmpty && unread("E"), "its window closed before its second: unread: \(desk.waiting)")

        // archived in its window within its second: the window stays, showing it, and it is not read
        mail.popOut("E")
        desk.answer("mail_list", Desk.list(rows))
        check(mail.popouts["E"]?.shown?.thread.id == "E" && desk.waiting.isEmpty, "E in its window again, kept: at once")
        mail.perform(.archive, in: "E")
        check(op(desk.answer("mail_mark", marked(e, ["box": "archived"]))) == "archive [\"E\"] upto 10"
              && mail.popouts["E"]?.shown?.thread.box == "archived", "e in its window: archived, the window stays")
        rows.remove(at: 3)
        relist()
        pass(readSeconds * 2)
        check(desk.waiting.isEmpty, "archived in its window before its second: no read mark: \(desk.waiting)")
        mail.popIn("E")

        // F on screen in the tab, then in its window as the tab moves on: its second runs on, and it is read once
        mail.open("F")
        desk.answer("mail_show", Desk.show(f, Desk.messages(["s5"])))
        pass(readSeconds * 0.6)
        mail.popOut("F")
        desk.answer("mail_list", Desk.list(rows))
        mail.open("C")
        check(mail.popouts["F"]?.shown?.thread.id == "F" && mail.shown?.thread.id == "C" && desk.waiting.isEmpty,
              "F kept, in its window at once; the tab on C, kept")
        pass(readSeconds * 0.6)
        check(op(desk.answer("mail_mark", marked(read(f)))) == "read [\"F\"] upto 5",
              "on screen in the tab, then its window: read once its second is up: \(desk.waiting)")
        rows[3] = read(f)
        relist()
        mail.popIn("F")

        // G read again within its second, a new mail come: read up to the newest mail on screen when its second is up
        mail.open("G")
        desk.answer("mail_show", Desk.show(g, Desk.messages(["s3"])))
        pass(readSeconds / 2)
        let g2 = thread("G", unread: true, newest: 4, count: 2, last: 40)
        mail.refresh()
        desk.answer("mail_list", Desk.list([g2] + rows.dropLast()))
        desk.answer("mail_show", Desk.show(g2, Desk.messages(["s3", "s4"])))
        check(mail.shown?.thread.count == 2 && desk.waiting.isEmpty, "G's new mail shown, not read yet")
        pass(readSeconds * 2)
        check(op(desk.answer("mail_mark", marked(read(g2)))) == "read [\"G\"] upto 4", "read up to the new mail")
        desk.answer("mail_list", Desk.list([read(g2)] + rows.dropLast()))
        check(desk.waiting.isEmpty, "nothing left unanswered: \(desk.waiting)")
    }

    /// Undo: each change the owner makes (archive, back to the inbox, snooze, unread, read) is offered on the bar once the
    /// desktop has made it, and the marks that open a thread are not; Undo sends the marks that put the thread back,
    /// one after another, shows it back in its place at once and opens it again, at the mail being read, if it was
    /// open. Several come back newest first. z pressed while a change or an Undo is unanswered is kept until it is
    /// answered, and dropped if it is refused; the bar's button does nothing meanwhile. The bar goes after its time; a
    /// refused Undo is said on the bar, sends nothing more, and the list is read again.
    static func undo(_ check: (Bool, String) -> Void) {
        let desk = Desk()
        defer { desk.problems.forEach { check(false, $0) } }
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        let mail = desk.model(folder: folder, upload: { _, _ in }, undoSeconds: 0.3)
        func listed() -> [String] { mail.threads?.map(\.id) ?? [] }
        func op(_ request: [String: Any]) -> String {
            "\(request["op"] ?? "-") \(request["threads"] ?? "-") upto \(request["upto"] ?? "-") until \(request["until"] ?? "-")"
        }
        let a = thread("A", unread: true, newest: 50, count: 2, last: 500), aRead = thread("A", newest: 50, count: 2, last: 500)
        let b = thread("B", newest: 40, last: 400), c = thread("C", newest: 30, last: 300)
        let d = thread("D", unread: true, newest: 20, last: 200)

        mail.refresh()
        desk.answer("mail_list", Desk.list([a, b, c, d]))
        // opening an unread thread marks it read, after its second on screen: nothing to undo
        mail.open("A")
        desk.answer("mail_show", Desk.show(a, Desk.messages(["s49", "s50"])))
        pass(readSeconds * 2)
        desk.answer("mail_mark", marked(aRead))
        desk.answer("mail_list", Desk.list([aRead, b, c, d]))
        check(mail.undos.isEmpty, "the read mark that comes with opening a thread is not offered: \(mail.undos.map(\.line))")

        // e archives; the bar offers it once the desktop has archived it. z pressed before then is kept, and Undoes
        // then: the thread back in its place in the list, open again at the mail being read, at once; the inbox mark sent
        mail.stepMessage(-1)
        mail.perform(.archive)
        check(mail.tab == nil && listed() == ["B", "C", "D"], "archived: no thread open: \(listed())")
        check(mail.undos.isEmpty && mail.undoWaiting == 1, "nothing offered before the desktop answers")
        mail.perform(.undo)
        check(desk.waiting == ["mail_mark"], "z before it has answered sends nothing yet: \(desk.waiting)")
        desk.answer("mail_mark", marked(aRead, ["box": "archived"]))
        check(listed() == ["A", "B", "C", "D"] && mail.selected == "A" && mail.shown?.thread.id == "A" && mail.current == "s49",
              "then the z kept Undoes it: back, open at the mail being read: \(listed()) \(mail.current ?? "none")")
        check(mail.undos.isEmpty && mail.undoWaiting == 1, "the bar has nothing more to offer")
        check(desk.waiting == ["mail_mark", "mail_list"], "the inbox mark, nothing to read: \(desk.waiting)")
        desk.answer("mail_list", Desk.list([b, c, d]))
        check(listed() == ["A", "B", "C", "D"], "a list answered before the Undo is does not take it out again")
        let inbox = desk.answer("mail_mark", marked(aRead))
        check(inbox["op"] as? String == "inbox" && inbox["upto"] == nil && inbox["until"] == nil
              && inbox["threads"] as? [String] == ["A"], "back to the inbox: \(op(inbox))")
        check(mail.undoFailure == nil && mail.undoWaiting == 0 && desk.waiting == ["mail_list"], "done; the list again: \(desk.waiting)")
        desk.answer("mail_list", Desk.list([aRead, b, c, d]))

        // several in a row come back newest first, each opened again; a second z while the first Undo is on its way
        // is kept for when it has been answered, and the bar's button does nothing meanwhile
        mail.archiveOrRestore()
        desk.answer("mail_mark", marked(aRead, ["box": "archived"]))
        desk.answer("mail_list", Desk.list([b, c, d]))
        mail.perform(.thread(1))
        check(mail.selected == "B", "↓: B, below A")
        desk.answer("mail_show", Desk.show(b, Desk.messages(["s40"])))
        mail.archiveOrRestore()
        check(mail.tab == nil, "B archived too: no thread open")
        desk.answer("mail_mark", marked(b, ["box": "archived"]))
        desk.answer("mail_list", Desk.list([c, d]))
        check(mail.undos.map(\.before.id) == ["A", "B"], "both on the bar, B the newest: \(mail.undos.map(\.before.id))")
        mail.perform(.undo)
        check(mail.selected == "B" && listed() == ["B", "C", "D"] && mail.undos.map(\.before.id) == ["A"],
              "z: B first, opened again: \(listed())")
        mail.perform(.undo)
        mail.undo()
        check(desk.waiting == ["mail_mark"] && mail.undos.count == 1, "a second z waits for the first, the button too: \(desk.waiting)")
        let bBack = desk.answer("mail_mark", marked(b))
        check(bBack["op"] as? String == "inbox" && bBack["threads"] as? [String] == ["B"], "B's mark: \(op(bBack))")
        check(mail.selected == "A" && mail.current == "s49" && listed() == ["A", "B", "C", "D"] && mail.undos.isEmpty,
              "then the second z: A: \(listed())")
        check(desk.waiting == ["mail_mark", "mail_list"], "A's mark, and the list after B's: \(desk.waiting)")
        let aBack = desk.answer("mail_mark", marked(aRead))
        check(aBack["op"] as? String == "inbox" && aBack["threads"] as? [String] == ["A"], "A's mark: \(op(aBack))")
        desk.answer("mail_list", Desk.list([aRead, b, c, d]))
        desk.answer("mail_list", Desk.list([aRead, b, c, d]))

        // a snooze: Undo takes the snooze away again
        let wake = Date(timeIntervalSince1970: 1_900_000_000)
        mail.snooze(until: wake)
        desk.answer("mail_mark", marked(aRead, ["box": "snoozed", "snooze_until": wake.timeIntervalSince1970]))
        desk.answer("mail_list", Desk.list([b, c, d]))
        check(mail.undos.last?.line == "Snoozed until " + MailFmt.at(wake.timeIntervalSince1970, now: Date()),
              "the bar says until when: \(mail.undos.last?.line ?? "nothing")")
        mail.undo()
        let unsnoozed = desk.answer("mail_mark", marked(aRead))
        check(unsnoozed["op"] as? String == "inbox" && unsnoozed["until"] == nil && unsnoozed["upto"] == nil,
              "a snooze undone: back to the inbox, no snooze: \(op(unsnoozed))")
        check(mail.selected == "A" && mail.current == "s49", "and open again")
        desk.answer("mail_list", Desk.list([aRead, b, c, d]))

        // Unread closes the thread; Undo marks it read again up to the newest mail it had, and opens it, unmarked
        mail.markUnread()
        check(mail.selected == nil, "Unread closes it")
        desk.answer("mail_mark", marked(aRead, ["unread": true]))
        desk.answer("mail_list", Desk.list([a, b, c, d]))
        check(mail.undos.last?.line == "Marked unread", "the bar: \(mail.undos.last?.line ?? "nothing")")
        mail.undo()
        check(mail.selected == "A" && mail.current == "s49" && mail.threads?.first?.unread == false
              && desk.waiting == ["mail_mark"], "open again, read, and only Undo's mark asked: \(desk.waiting)")
        let reread = desk.answer("mail_mark", marked(aRead))
        check(reread["op"] as? String == "read" && reread["upto"] as? Int == 50, "read again up to its newest: \(op(reread))")
        desk.answer("mail_list", Desk.list([aRead, b, c, d]))

        // Mark as read on a thread not open: Undo marks it unread, and the open thread stays
        mail.markRead("D")
        desk.answer("mail_mark", marked(d, ["unread": false]))
        desk.answer("mail_list", Desk.list([aRead, b, c, thread("D", newest: 20, last: 200)]))
        check(mail.undos.last?.line == "Marked read", "the bar: \(mail.undos.last?.line ?? "nothing")")
        mail.undo()
        check(mail.selected == "A" && mail.threads?.last?.unread == true, "D unread again at once, A still open")
        let unreadAgain = desk.answer("mail_mark", marked(d))
        check(unreadAgain["op"] as? String == "unread" && unreadAgain["upto"] == nil, "unread again: \(op(unreadAgain))")
        desk.answer("mail_list", Desk.list([aRead, b, c, d]))

        // an archive the desktop also marked read (one that ended a snooze it had woken from): Undo sends the inbox
        // mark, then, once that is answered, the unread one
        mail.archiveOrRestore("D")
        desk.answer("mail_mark", marked(d, ["box": "archived", "unread": false]))
        desk.answer("mail_list", Desk.list([aRead, b, c]))
        mail.undo()
        check(listed() == ["A", "B", "C", "D"] && mail.threads?.last?.unread == true, "back, unread, at once: \(listed())")
        let first = desk.answer("mail_mark", marked(d, ["unread": false]))
        check(first["op"] as? String == "inbox", "the box first: \(op(first))")
        check(desk.waiting == ["mail_mark", "mail_list"], "then the read state: \(desk.waiting)")
        let second = desk.answer("mail_mark", marked(d))
        check(second["op"] as? String == "unread" && second["threads"] as? [String] == ["D"], "unread again: \(op(second))")
        check(mail.undoFailure == nil && mail.undoWaiting == 0, "done")
        desk.answer("mail_list", Desk.list([aRead, b, c, d]))
        desk.answer("mail_list", Desk.list([aRead, b, c, d]))

        // a refused Undo: nothing sent after the refused mark, the bar says why until closed, the list read again
        mail.archiveOrRestore("D")
        desk.answer("mail_mark", marked(d, ["box": "archived", "unread": false]))
        desk.answer("mail_list", Desk.list([aRead, b, c]))
        mail.undo()
        check(mail.selected == "A" && listed() == ["A", "B", "C", "D"], "a thread not open: back in the list, A still open")
        desk.answer("mail_mark", nil, err: "refused inside an agent session")
        check(mail.undoFailure == "Not undone: not moved to the inbox: refused inside an agent session",
              "the refusal on the bar: \(mail.undoFailure ?? "none")")
        check(desk.waiting == ["mail_list"], "no unread mark after it, the list again: \(desk.waiting)")
        desk.answer("mail_list", Desk.list([aRead, b, c]))
        check(listed() == ["A", "B", "C"], "as the desktop has it")
        mail.dismissUndoFailure()
        check(mail.undoFailure == nil, "closed")

        // a refused change drops the z kept meanwhile: the change before it stays on the bar, not undone
        mail.archiveOrRestore("C")
        desk.answer("mail_mark", marked(c, ["box": "archived"]))
        desk.answer("mail_list", Desk.list([aRead, b]))
        mail.archiveOrRestore("B")
        mail.perform(.undo)
        desk.answer("mail_mark", nil, err: "refused inside an agent session")
        check(mail.notice == "Not archived: refused inside an agent session" && desk.waiting == ["mail_list"]
              && mail.undos.map(\.before.id) == ["C"], "B not archived; C not undone: \(desk.waiting)")
        desk.answer("mail_list", Desk.list([aRead, b]))
        mail.undo()
        desk.answer("mail_mark", marked(c))
        desk.answer("mail_list", Desk.list([aRead, b, c]))

        // Move to Inbox, from Archived: Undo archives it again, up to the newest mail it had, and it is listed there again
        let e = thread("E", newest: 5, last: 50, box: .archived)
        mail.show(.archived)
        desk.answer("mail_list", Desk.list([e]))
        mail.archiveOrRestore("E")
        desk.answer("mail_mark", marked(e, ["box": "inbox"]))
        desk.answer("mail_list", Desk.list([]))
        check(mail.undos.last?.line == "Moved to the inbox" && listed().isEmpty, "the bar: \(mail.undos.last?.line ?? "nothing")")
        mail.undo()
        check(listed() == ["E"], "back in Archived at once: \(listed())")
        let rearchived = desk.answer("mail_mark", marked(e))
        check(rearchived["op"] as? String == "archive" && rearchived["upto"] as? Int == 5, "archived again: \(op(rearchived))")
        desk.answer("mail_list", Desk.list([e]))

        // Move to Inbox, from Snoozed, of a snooze whose time has since passed: Undo leaves it in the inbox, where it
        // would be by now, and so sends nothing
        let f = thread("F", newest: 6, last: 60, box: .snoozed).merging(["snooze_until": 1.0]) { $1 }
        mail.show(.snoozed)
        desk.answer("mail_list", Desk.list([f]))
        mail.archiveOrRestore("F")
        desk.answer("mail_mark", marked(f, ["box": "inbox", "snooze_until": NSNull()]))
        desk.answer("mail_list", Desk.list([]))
        mail.undo()
        check(desk.waiting.isEmpty && listed().isEmpty && mail.undoFailure == nil,
              "a snooze since past: nothing sent, not back in Snoozed: \(desk.waiting) \(listed())")
        mail.show(.inbox)
        desk.answer("mail_list", Desk.list([aRead, b, c, d]))

        // the bar goes after its time, and then z has nothing to undo
        mail.archiveOrRestore()
        desk.answer("mail_mark", marked(aRead, ["box": "archived"]))
        desk.answer("mail_list", Desk.list([b, c, d]))
        check(!mail.undos.isEmpty && mail.tab == nil, "offered")
        pass(0.6)
        check(mail.undos.isEmpty, "gone after its time")
        mail.perform(.undo)
        check(desk.waiting.isEmpty && mail.tab == nil, "z then does nothing: \(desk.waiting)")
    }

    /// Threads popped out into windows of their own (MailModel.popouts): read as the tab reads them (kept, or asked
    /// for; marked read after their second on screen), and read once for the tab and the window when both show the
    /// thread, at the latest mail asked for; a window's mail being read and paging its own; popped out again, only
    /// raised; the list's polls and a reply read the window's thread again; e from the window keeps it, archived, and z
    /// puts it back; s asks the place it was pressed in for its Snooze menu, and Snooze keeps the window too; u
    /// (Unread) lets it go and Undo pops it out again; an empty reply box stays while the thread is read in the other
    /// place; a thread that does not load says why in its window and is read again when popped out again.
    static func popouts(_ check: (Bool, String) -> Void) {
        let desk = Desk()
        defer { desk.problems.forEach { check(false, $0) } }
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        let mail = desk.model(folder: folder, upload: { _, _ in })
        var raised: [String] = [], menus: [String?] = []
        let watch = mail.raise.sink { raised.append($0) }
        let snoozes = mail.snoozeAsked.sink { menus.append($0) }
        defer { watch.cancel(); snoozes.cancel() }
        func show(_ t: [String: Any], _ keys: [String], unread: Set<String> = []) -> [String: Any] {
            Desk.show(t, keys.map { Desk.message($0, mine: $0.hasPrefix("i"), unread: unread.contains($0)) })
        }
        func op(_ request: [String: Any]) -> String { "\(request["op"] ?? "-") \(request["threads"] ?? "-") upto \(request["upto"] ?? "-")" }
        let a = thread("A", unread: true, newest: 50, count: 3, last: 500)
        let b = thread("B", newest: 40, last: 400), c = thread("C", newest: 30, last: 300)

        mail.refresh()
        desk.answer("mail_list", Desk.list([a, b, c]))
        // popped out, not open in the tab: asked for, at its first unread mail, marked read once; the list is polled
        // while a window is open, the tab away
        mail.popOut("A")
        check(desk.waiting == ["mail_list", "mail_show"] && desk.shows == ["A"] && mail.popouts.keys.sorted() == ["A"]
              && mail.selected == nil, "popped out: its thread asked for, and the list, the tab left alone: \(desk.waiting)")
        desk.answer("mail_list", Desk.list([a, b, c]))
        // the tab opens it too before the answer: one request, one read mark, shown in both
        mail.open("A")
        check(desk.shows == ["A"] && desk.waiting == ["mail_show"], "opened in the tab meanwhile: no second request: \(desk.waiting)")
        desk.answer("mail_show", show(a, ["s48", "s49", "s50"], unread: ["s49", "s50"]))
        check(mail.popouts["A"]?.current == "s49" && mail.current == "s49" && mail.shown?.thread.id == "A",
              "shown in both, each at the first unread mail")
        pass(readSeconds * 2)
        let read = desk.answer("mail_mark", marked(thread("A", newest: 50, count: 3, last: 500)))
        check(op(read) == "read [\"A\"] upto 50" && desk.waiting == ["mail_list"], "marked read once: \(op(read)) \(desk.waiting)")
        desk.answer("mail_list", Desk.list([thread("A", newest: 50, count: 3, last: 500), b, c]))

        // the window's mail being read and its pages are its own
        mail.perform(.message(1), in: "A")
        mail.perform(.page(1), in: "A")
        check(mail.popouts["A"]?.current == "s50" && mail.popouts["A"]?.page == MailPage(n: 1, by: 1)
              && mail.current == "s49" && mail.page == nil, "→ and Space in the window move it, not the tab")
        mail.perform(.thread(1), in: "A")
        check(mail.selected == "A" && desk.waiting.isEmpty, "the list's keys do nothing from a window")

        // popped out again: its window comes forward, nothing is asked; another thread, kept and unchanged, shows at once
        mail.popOut("A")
        check(raised == ["A"] && desk.waiting.isEmpty && mail.popouts.count == 1, "popped out again: raised, not opened again")
        mail.open("B")
        desk.answer("mail_show", show(b, ["s40"]))
        mail.popOut("B")
        check(mail.popouts["B"]?.shown?.thread.id == "B" && mail.popouts["B"]?.current == "s40" && desk.waiting.isEmpty,
              "a thread kept and unchanged: in its window at once: \(desk.waiting)")

        // a poll that finds A changed: A read once, for the window (the tab shows B); B unchanged, not read
        mail.refresh()
        let a2 = thread("A", unread: true, newest: 51, count: 4, last: 510)
        desk.answer("mail_list", Desk.list([a2, b, c]))
        check(desk.shows == ["A"], "the window's thread is read again when the list shows it changed: \(desk.waiting)")
        desk.answer("mail_show", show(a2, ["s48", "s49", "s50", "s51"], unread: ["s51"]))
        check(mail.popouts["A"]?.shown?.thread.count == 4 && mail.popouts["A"]?.current == "s51",
              "the window shows the new mail, at it")
        pass(readSeconds * 2)
        check(op(desk.answer("mail_mark", marked(a2, ["unread": false]))) == "read [\"A\"] upto 51",
              "and marks it read after its second")
        desk.answer("mail_list", Desk.list([thread("A", newest: 51, count: 4, last: 510), b, c]))

        // a reply sent from the window: A read again, for the window, at the reply
        mail.draft(for: "A").text = "Go ahead."
        mail.send("A")
        desk.answer("mail_reply", ["ok": true, "key": "i60", "status": "sending"])
        check(desk.waiting == ["mail_show", "mail_list"] && desk.shows == ["A"], "sent: A and the list again: \(desk.waiting)")
        let a3 = thread("A", newest: 51, count: 5, last: 600)
        desk.answer("mail_show", show(a3, ["s48", "s49", "s50", "s51", "i60"]))
        check(mail.popouts["A"]?.current == "i60", "the window comes back at the reply")
        desk.answer("mail_list", Desk.list([a3, b, c]))

        // e in A's window: archived, the window stays and shows it so; the tab's B stays open; z (Undo's own path,
        // pressUndo) puts A back, in the list and the window
        mail.perform(.archive, in: "A")
        check(mail.popouts["A"]?.shown?.thread.box == "archived" && mail.selected == "B" && mail.threads?.map(\.id) == ["B", "C"],
              "e in the window: archived, out of the inbox, its window still showing it; the tab's thread stays")
        mail.perform(.undo, in: "A")
        check(desk.waiting == ["mail_mark"], "z before the archive is answered is kept: \(desk.waiting)")
        check(op(desk.answer("mail_mark", marked(a3, ["box": "archived"]))) == "archive [\"A\"] upto 51",
              "archived up to its newest")
        check(mail.popouts["A"]?.shown?.thread.box == "inbox" && mail.threads?.map(\.id) == ["A", "B", "C"] && mail.selected == "B",
              "then z: back in the inbox, in the list and the window; the tab not moved")
        check(op(desk.answer("mail_mark", marked(a3))) == "inbox [\"A\"] upto -" && raised == ["A"],
              "the inbox mark, and no window raised")
        desk.answer("mail_list", Desk.list([a3, b, c]))
        desk.answer("mail_list", Desk.list([a3, b, c]))

        // an empty reply box opened in the window stays while B is still read there, though the tab moves on
        mail.reply("B")
        mail.open("C")
        check(mail.draft(for: "B").shown, "the tab moves on: B's empty box stays, for its window")
        desk.answer("mail_show", show(c, ["s30"]))

        // u in B's window, as Unread, lets it go; Undo pops it out again
        mail.perform(.unread, in: "B")
        check(mail.popouts["B"] == nil && !mail.draft(for: "B").shown && mail.selected == "C",
              "u: B's window is let go, its empty box with it; the tab's C stays")
        desk.answer("mail_mark", marked(b, ["unread": true]))
        desk.answer("mail_list", Desk.list([a3, thread("B", unread: true, newest: 40, last: 400), c]))
        mail.undo()
        check(mail.popouts["B"]?.shown?.thread.id == "B" && mail.popouts["B"]?.shown?.thread.unread == false && mail.selected == "C",
              "Undo: B in a window again, read, from what was kept; the tab's C stays")
        check(op(desk.answer("mail_mark", marked(b))) == "read [\"B\"] upto 40", "and only Undo's read mark is sent")
        desk.answer("mail_list", Desk.list([a3, b, c]))

        // s asks the place it was pressed in for its Snooze menu (its ThreadActions pops it up): B's window, the tab
        mail.perform(.snooze, in: "B")
        mail.perform(.snooze)
        check(menus == ["B", nil], "s: B's window's Snooze menu, then the tab's: \(menus)")

        // Snooze from B's window (not the tab's thread): the window stays, saying until when; the tab's C stays open
        let wake = Date(timeIntervalSince1970: 1_900_000_000)
        mail.snooze("B", until: wake)
        check(mail.popouts["B"]?.shown?.thread.box == "snoozed" && mail.selected == "C" && mail.threads?.map(\.id) == ["A", "C"]
              && MailWindowView.place(mail.current("B"), now: Date())
                == MailOp.snooze.done(until: wake.timeIntervalSince1970, now: Date()),
              "snoozed from its window: out of the inbox, the window still open saying until when; the tab's C stays")
        let snoozed = desk.answer("mail_mark", marked(b, ["box": "snoozed", "snooze_until": wake.timeIntervalSince1970]))
        check(op(snoozed) == "snooze [\"B\"] upto 40" && snoozed["until"] as? Double == wake.timeIntervalSince1970,
              "snoozed up to its newest, until then: \(op(snoozed))")
        desk.answer("mail_list", Desk.list([a3, c]))

        // a window whose thread does not load says why; popped out again, it comes forward and is read again
        let d = thread("D", newest: 20, last: 200)
        mail.popOut("D")
        check(desk.shows == ["D"], "D asked for: \(desk.waiting)")
        desk.answer("mail_show", nil, err: "ssh: connection reset")
        check(mail.popouts["D"]?.error == "ssh: connection reset" && mail.popouts["D"]?.shown == nil,
              "not loaded: its window says why: \(mail.popouts["D"]?.error ?? "nothing")")
        mail.popOut("D")
        check(raised.last == "D" && desk.shows == ["D"] && mail.popouts["D"]?.error == nil,
              "popped out again: its window comes forward, and D is asked for again: \(desk.waiting)")
        desk.answer("mail_show", show(d, ["s19", "s20"]))
        check(mail.popouts["D"]?.shown?.thread.id == "D" && mail.popouts["D"]?.current == "s20" && mail.popouts["D"]?.error == nil,
              "then it shows")

        // the tab opens a thread at a mail while its window's read is on its way: no second request, and it opens at
        // that mail, the latest asked for
        let e = thread("E", newest: 10, count: 3, last: 100)
        mail.popOut("E")
        mail.open("E", at: "s8")
        check(desk.shows == ["E"], "one request for E: \(desk.waiting)")
        desk.answer("mail_show", show(e, ["s8", "s9", "s10"]))
        check(mail.current == "s8" && mail.popouts["E"]?.shown?.thread.id == "E", "the tab opens E at s8: \(mail.current ?? "none")")
        mail.popIn("B")
        mail.popIn("A")
        mail.popIn("D")
        mail.popIn("E")
        check(mail.popouts.isEmpty && desk.waiting.isEmpty, "every window closed; nothing left to answer: \(desk.waiting)")
        mail.close()
        mail.perform(.snooze)
        mail.perform(.unread)
        check(menus.count == 2 && desk.waiting.isEmpty, "with no thread open, s and u do nothing: \(menus) \(desk.waiting)")
    }

    /// Fast mode: its minutes left, as the phone's page counts them; a thread's fast mode read from a saved answer, and
    /// the bar's and the badge's words for it; the Fast switch asking the desktop for mail_fast with the thread and the
    /// value, the answer shown at once in the row (its sessions kept) and the open thread, a list asked before the
    /// answer not undoing it, a second click while one is unanswered asking nothing, and a refusal said.
    static func fast(_ check: (Bool, String) -> Void) {
        // the minutes left: rounded up, at most its length, "ending" once its end has passed (the phone's fastLabel
        // has the same cases, in tests/test_mailweb.py)
        let t0 = Date(timeIntervalSince1970: 1_800_000_000)
        let cases: [(Double, Double?)] = [(420, 10), (400, 10), (61, 10), (1, 10), (0, 10), (-5, 10), (610, 10), (90, 0.5),
                                          (90, nil)]
        let labels = cases.map { MailFast(until: 1_800_000_000 + $0.0, minutes: $0.1).label(at: t0) }
        check(labels == ["Fast · 7 min", "Fast · 7 min", "Fast · 2 min", "Fast · 1 min", "Fast · ending", "Fast · ending",
                         "Fast · 10 min", "Fast · 1 min", "Fast · 2 min"], "fast mode's minutes left: \(labels)")

        // a saved answer: one thread in fast mode with its effort unchanged, one with it off, one from a mailroom
        // that does not say
        let note = "Effort unchanged: the agent runs on mac, where the mailroom presses no keys."
        let unchanged: [String: Any] = ["since": 1_799_999_800.0, "until": 1_800_000_400.0, "minutes": 10,
                                        "agent": "board-fast", "effort": "unchanged", "was": NSNull(), "note": note]
        var g = thread("G", newest: 5, last: 50, by: [("codex", "C1")])
        g["fast"] = unchanged
        var h = thread("H", newest: 4, last: 40)
        h["fast"] = NSNull()
        let (saved, why) = Actions.decodeAnswer(["ok": true, "threads": [g, h, thread("I", newest: 3, last: 30)]], nil,
                                                key: nil, as: MailList.self)
        check(saved?.threads.map(\.id) == ["G", "H", "I"], "the saved list decodes: \(why ?? "") \(saved?.dropped ?? [])")
        let f = saved?.threads.first?.fast
        check(f == MailFast(since: 1_799_999_800, until: 1_800_000_400, minutes: 10, agent: "board-fast",
                            effort: "unchanged", was: nil, note: note), "a thread's fast mode: \(String(describing: f))")
        check(saved?.threads.dropFirst().allSatisfy { $0.fast == nil } == true, "off, or not said: no fast mode")
        let bar = MailModeBar.texts(f, now: t0, switching: false)
        check(bar.label == "Fast · 7 min" && bar.note == note, "the bar: the minutes left and the note: \(bar)")
        check(bar.help.hasPrefix("Fast mode is on for board-fast until ") && bar.help.contains(note)
              && bar.help.hasSuffix("Click to switch it off."), "the switch's help, on: \(bar.help)")
        let off = MailModeBar.texts(nil, now: t0, switching: false)
        check(off.label == "Fast" && off.note == nil && off.help.hasPrefix("Fast mode, for quick back-and-forths")
              && off.help.contains("(5 by default: the mailroom's mailbox.fast_minutes)"), "the bar, off: \(off)")
        check(MailModeBar.texts(f, now: t0, switching: true).label == "Fast…" && MailModeBar.texts(nil, now: t0, switching: true).label
              == "Fast…", "the switch while the desktop has not answered")
        check(MailModeBar.texts(f, now: t0.addingTimeInterval(401), switching: false).label == "Fast · ending",
              "past its end, until the desktop says it has ended")

        // the switch
        var asked: [(request: [String: Any], done: @MainActor ([String: Any]?, String?) -> Void)] = []
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        let mail = MailModel(call: { request, done in asked.append((request, done)) }, upload: { _, _ in },
                             ahead: 0, cacheBytes: 1 << 20, draftsFolder: folder)
        func waiting() -> [String] { asked.map { $0.request["action"] as? String ?? "?" } }
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
        func wire(_ request: [String: Any]) -> String {
            (try? JSONSerialization.data(withJSONObject: request, options: .sortedKeys)).flatMap { String(data: $0, encoding: .utf8) } ?? ""
        }
        var gOff = g
        gOff["fast"] = NSNull()
        var gOn = thread("G", newest: 5, last: 50)        // as mail_fast answers it: no sessions
        let since = Date().timeIntervalSince1970
        let lowering: [String: Any] = ["since": since, "until": since + 300, "minutes": 5, "agent": "board-fast",
                                       "effort": "lowering", "was": "xhigh", "note": "Back to xhigh when fast mode ends."]
        gOn["fast"] = lowering
        var gLow = g                                      // as the list has it once the mailroom has lowered it
        gLow["fast"] = lowering.merging(["effort": "low",
                                         "note": "Effort low until fast mode ends, then xhigh again."]) { $1 }
        mail.refresh()
        answer("mail_list", list([gOff, h]))
        mail.open("G")
        answer("mail_show", ["ok": true, "thread": gOff, "document": "<p>G</p>",
                             "messages": [["key": "s5", "from": "board-fast", "mine": false, "ts": 1, "unread": false,
                                           "status": NSNull()]]])
        check(mail.current("G")?.fast == nil, "off at first")
        mail.refresh()                                    // a list on its way as the switch is clicked
        mail.setFast("G", on: true)
        check(waiting() == ["mail_list", "mail_fast"] && mail.fastAsked == "G", "the switch asks: \(waiting())")
        mail.setFast("G", on: true)
        check(waiting() == ["mail_list", "mail_fast"], "a second click while it is unanswered asks nothing: \(waiting())")
        let onReq = answer("mail_fast", ["ok": true, "thread": gOn])
        check(wire(onReq) == #"{"action":"mail_fast","on":true,"thread":"G"}"#, "switched on: \(wire(onReq))")
        let row = mail.threads?.first { $0.id == "G" }
        check(row?.fast?.effort == "lowering" && row?.sessions == [AgentKey(cli: "codex", id: "C1")]
              && mail.fastAsked == nil, "on in the row at once, its sessions kept: \(String(describing: row))")
        check(mail.shown?.thread.fast?.note == "Back to xhigh when fast mode ends.", "and in the open thread")
        let started = MailModeBar.texts(mail.current("G")?.fast, now: mail.now, switching: false)
        check(started.label == "Fast · 5 min" && started.note == "Lowering effort… Back to xhigh when fast mode ends.",
              "the bar, just switched on, before the mailroom has lowered the effort: \(started)")
        answer("mail_list", list([gOff, h]))              // asked before the answer: out of date
        check(mail.threads?.first { $0.id == "G" }?.fast != nil, "an older list does not switch it off again")
        answer("mail_list", list([gLow, h]))              // read again after the answer: the mailroom has lowered it
        let lowered = MailModeBar.texts(mail.current("G")?.fast, now: mail.now, switching: false)
        check(lowered.note == "Effort low until fast mode ends, then xhigh again.",
              "the bar once the effort is low: \(lowered)")
        // a reply sent in fast mode leaves the box open, empty, with the keyboard, for the next message
        let quick = mail.draft(for: "G")
        quick.text = "And the tests?"
        mail.send("G")
        answer("mail_reply", ["ok": true, "key": "i70", "status": "sending"])
        check(quick.text.isEmpty && quick.shown && quick.wantsKeyboard, "sent in fast mode: the box stays open, empty")
        answer("mail_show", ["ok": true, "thread": gLow, "document": "<p>G</p>",
                             "messages": [["key": "i70", "from": "Alex", "mine": true, "ts": 2, "unread": false,
                                           "status": "delivered"]]])     // delivered: the list reads it no more
        answer("mail_list", list([gLow, h]))
        mail.setFast("G", on: false)
        let offReq = answer("mail_fast", ["ok": true, "thread": gOff])
        check(wire(offReq) == #"{"action":"mail_fast","on":false,"thread":"G"}"#, "switched off: \(wire(offReq))")
        check(mail.current("G")?.fast == nil && mail.shown?.thread.fast == nil, "off at once")
        answer("mail_list", list([gOff, h]))
        mail.setFast("H", on: true)
        answer("mail_fast", nil, err: "agent-mail: fast mode is for a thread one agent owns; this one is a group's")
        check(mail.notice == "Fast mode not switched on: agent-mail: fast mode is for a thread one agent owns; this one is "
              + "a group's" && mail.fastAsked == nil, "a refusal is said: \(mail.notice ?? "none")")
        check(waiting() == ["mail_list"], "the list again after a refusal: \(waiting())")
        answer("mail_list", list([gOff, h]))
        check(waiting().isEmpty, "nothing left unanswered: \(waiting())")
    }

    /// One of the board's agents as the snapshot has it, `state` its tile's, with its latest mail to the owner in `thread`
    /// ("" for none) sent at `lastOut`.
    static func agent(_ id: String, _ name: String, cli: String = "claude", state: String, thread: String,
                      lastOut: Double?) -> [String: Any] {
        ["id": id, "cli": cli, "name": name, "display": name, "cwd": "/work/" + name, "live": true,
         "raw_status": state == "busy" ? "busy" : "idle", "kind": "interactive", "notes": [Any](), "events": [Any](),
         "repo": name, "branch": "main", "state": state, "reason": "",
         "mail": ["unanswered": lastOut != nil, "last_out_thread": thread, "last_out": lastOut.map { $0 as Any } ?? NSNull()]]
    }

    /// A snapshot with these agents, and a mailbox unless `mailbox` is false; nil (and a failed check) when it does not
    /// decode.
    static func snapshot(_ agents: [[String: Any]], mailbox: Bool = true, _ check: (Bool, String) -> Void) -> Snapshot? {
        var obj: [String: Any] = ["generated": 1_800_000_000, "agents": agents, "timeline": [Any](),
                                  "mail": ["held": [Any](), "held_count": 0]]
        if mailbox { obj["mailbox"] = ["unread": 0, "inbox": 1] }
        do {
            let snap = try JSON.decoder.decode(Snapshot.self, from: JSONSerialization.data(withJSONObject: obj))
            check(snap.agents.count == agents.count && snap.problems.isEmpty, "the stand-in snapshot decodes: \(snap.problems)")
            return snap
        } catch {
            check(false, "the stand-in snapshot decodes: \(describeDecodeError(error))")
            return nil
        }
    }

    /// A thread as mail_list gives it when asked for its sessions: the board's agents (cli, id) that wrote in it.
    static func thread(_ id: String, newest: Int, last: Double, box: MailBox = .inbox, by: [(String, String)]) -> [String: Any] {
        var t = thread(id, newest: newest, last: last, box: box)
        t["sessions"] = by.map { ["cli": $0.0, "id": $0.1] }
        return t
    }

    /// A double-click on a tile (BoardModel.openFromTile): the Mail tab, the agent's latest thread opened at its own
    /// latest mail and the box it is in listed; the thread open already, listed again; another thread opened before
    /// it came, the reveal dropped; no thread, or no mailbox, a toast and nothing asked.
    static func board(_ check: (Bool, String) -> Void) {
        let desk = Desk()
        defer { desk.problems.forEach { check(false, $0) } }
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        var copied = 0                                                     // nothing is copied to the desktop here
        let mail = desk.model(folder: folder, upload: { _, _ in copied += 1 })
        let board = BoardModel(layout: Layout(), persist: false, mail: mail)
        /// mail_show's answer: thread `t` with messages of these keys and times, the owner's those whose key starts "i".
        func show(_ t: [String: Any], _ messages: [(key: String, ts: Double)]) -> [String: Any] {
            Desk.show(t, messages.map { Desk.message($0.key, mine: $0.key.hasPrefix("i"), ts: $0.ts) })
        }
        let agents = [agent("A1", "alpha", state: "busy", thread: "T2", lastOut: 300),
                      agent("B1", "beta", cli: "codex", state: "idle", thread: "T3", lastOut: 999),
                      agent("C1", "quiet", state: "idle", thread: "", lastOut: nil)]
        guard let snap = snapshot(agents, check) else { return }
        board.apply(snap)
        let tile = Dictionary(uniqueKeysWithValues: board.agents.map { ($0.name, $0) })
        guard let alpha = tile["alpha"], let beta = tile["beta"], let quiet = tile["quiet"] else {
            check(false, "the board has the stand-in agents: \(tile.keys.sorted())")
            return
        }

        // alpha's latest mail, in an archived thread the owner has answered since: opened at alpha's mail, Archived listed
        board.openFromTile(alpha)
        check(board.tab == .mail && board.selectedID == "A1" && mail.selected == "T2" && desk.waiting == ["mail_show"],
              "a double-click opens the Mail tab and asks for the tile's thread: \(board.tab) \(desk.waiting)")
        let t2 = thread("T2", newest: 9, last: 400, box: .archived, by: [("claude", "A1")])
        desk.answer("mail_show", show(t2, [("s8", 100), ("s9", 300), ("i4", 400)]))
        check(mail.shown?.thread.id == "T2" && mail.current == "s9",
              "it opens at the agent's latest mail, not at the reply after it: \(mail.current ?? "none")")
        let archived = desk.answer("mail_list", Desk.list([t2]))
        check(mail.listing == .archived && archived["box"] as? String == "archived" && archived["sessions"] as? Bool == true,
              "the box it is in is listed, with each thread's agents: \(archived)")
        check(mail.threads?.map(\.id) == ["T2"] && desk.waiting.isEmpty, "and it is in the list: \(desk.waiting)")

        // beta's, snoozed, with no mail at its time in the thread: opened at the thread's newest mail, Snoozed listed
        board.tab = .board
        board.openFromTile(beta)
        let t3 = thread("T3", newest: 21, last: 600, box: .snoozed, by: [("codex", "B1")])
        desk.answer("mail_show", show(t3, [("s20", 500), ("s21", 550), ("i22", 600)]))
        check(board.tab == .mail && mail.current == "i22", "no mail at its time: the newest: \(mail.current ?? "none")")
        check(desk.answer("mail_list", Desk.list([t3]))["box"] as? String == "snoozed" && mail.listing == .snoozed,
              "a snoozed thread: Snoozed is listed")

        // the thread open already, in another box: no thread asked for, its mail again, its box listed again
        mail.show(.inbox)
        desk.answer("mail_list", Desk.list([]))
        mail.stepMessage(-1)
        board.tab = .board
        board.openFromTile(beta)
        check(desk.waiting == ["mail_list"] && mail.listing == .snoozed && mail.current == "i22" && board.tab == .mail,
              "open already: at its mail, its box listed: \(desk.waiting) \(mail.current ?? "none")")
        desk.answer("mail_list", Desk.list([t3]))

        // a reveal overtaken: another thread opened before the tile's came; opened later, the tile's opens as any other
        board.tab = .board
        board.openFromTile(alpha)
        check(desk.waiting == ["mail_show"] && mail.selected == "T2", "alpha's thread asked for again: \(desk.waiting)")
        mail.open("T3")                                   // listed and kept: shown at once
        desk.answer("mail_show", show(t2, [("s8", 100), ("s9", 300), ("i4", 400)]))
        check(mail.selected == "T3" && mail.shown?.thread.id == "T3" && desk.waiting.isEmpty,
              "the overtaken answer shows nothing: \(desk.waiting)")
        mail.open("T2")
        desk.answer("mail_show", show(t2, [("s8", 100), ("s9", 300), ("i4", 400)]))
        check(mail.current == "i4" && mail.listing == .snoozed && desk.waiting.isEmpty,
              "opened later, at its newest, its box not listed: \(mail.current ?? "none") \(desk.waiting)")

        // no mail in the last week: a toast; the board stays, the tile selected; nothing asked
        board.tab = .board
        board.openFromTile(quiet)
        check(board.tab == .board && board.selectedID == "C1" && board.toast == "quiet hasn't mailed you in the last week."
              && desk.waiting.isEmpty, "no thread: a toast: \(board.toast ?? "none") \(desk.waiting)")

        // no mailbox on the desktop: a toast too
        guard let bare = snapshot(agents, mailbox: false, check) else { return }
        board.apply(bare)
        board.openFromTile(alpha)
        check(board.tab == .board && (board.toast ?? "").hasPrefix("The desktop has no mailbox yet") && desk.waiting.isEmpty,
              "no mailbox: a toast: \(board.toast ?? "none")")
        check(copied == 0, "nothing copied to the desktop")
    }

    /// `view` drawn offscreen in a 300 × 70 window, light or dark, over the window's ground as the board draws the
    /// list (a list row's drawing tests).
    static func drawnRow<V: View>(_ view: V, dark: Bool) -> NSBitmapImageRep? {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let (win, host) = Renderer.offscreen(view.frame(width: 300, height: 70, alignment: .topLeading)
                                                 .background(Color(nsColor: .windowBackgroundColor)),
                                             size: CGSize(width: 300, height: 70), dark: dark)
        return withExtendedLifetime(win) {
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
            guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return nil }
            host.cacheDisplay(in: host.bounds, to: rep)
            return rep
        }
    }

    /// The colour at (x, y) in points from a drawn row's top left, as 0xRRGGBB in sRGB.
    static func pixel(_ rep: NSBitmapImageRep, _ x: CGFloat, _ y: CGFloat) -> UInt32 {
        let scale = CGFloat(rep.pixelsWide) / 300
        guard let c = rep.colorAt(x: Int(x * scale), y: Int(y * scale))?.usingColorSpace(.sRGB) else { return 0 }
        func ch(_ v: CGFloat) -> UInt32 { UInt32(max(0, min(255, (v * 255).rounded()))) }
        return ch(c.redComponent) << 16 | ch(c.greenComponent) << 8 | ch(c.blueComponent)
    }

    /// Whether two colours are the same but for rounding in the conversions to and from the window's colour space.
    static func near(_ a: UInt32, _ b: UInt32) -> Bool {
        [16, 8, 0].allSatisfy { abs(Int((a >> $0) & 0xFF) - Int((b >> $0) & 0xFF)) <= 3 }
    }

    /// Whether two drawn rows are the same, byte for byte, right of `x` (points from the left edge).
    static func same(_ a: NSBitmapImageRep, _ b: NSBitmapImageRep, from x: CGFloat) -> Bool {
        guard a.pixelsWide == b.pixelsWide, a.pixelsHigh == b.pixelsHigh, a.bytesPerRow == b.bytesPerRow,
              a.bitsPerPixel == b.bitsPerPixel, let da = a.bitmapData, let db = b.bitmapData else { return false }
        let from = Int(x * CGFloat(a.pixelsWide) / 300) * a.bitsPerPixel / 8
        let width = a.pixelsWide * a.bitsPerPixel / 8
        return (0..<a.pixelsHigh).allSatisfy { y in
            memcmp(da + y * a.bytesPerRow + from, db + y * b.bytesPerRow + from, width - from) == 0
        }
    }

    /// The group colours (README, "Group colours"): a list's threads keep the colours the desktop sends, and a colour
    /// that is not #RRGGBB leaves its thread out, saying why; groups_error is kept to be said over the list. Drawn,
    /// light and dark, unread, read or open, a grouped row is the plain row of the same thread sent no colours, but for
    /// the strip of its sub-group's shade (Ungrouped's grey) down its left edge, so the open thread's blue is the one
    /// colour behind the rows; it keeps its blue and brown dots.
    static func colours(_ check: (Bool, String) -> Void) {
        let desk = Desk()
        defer { desk.problems.forEach { check(false, $0) } }
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        let mail = desk.model(folder: folder, upload: { _, _ in })
        func grouped(_ id: String, unread: Bool, group: String?, sub: String?, colour: String,
                     shade: String) -> [String: Any] {
            var t = thread(id, unread: unread, newest: 10, last: 100)
            t["group"] = group.map { $0 as Any } ?? NSNull()
            t["subgroup"] = sub.map { $0 as Any } ?? NSNull()
            t["colour"] = colour
            t["shade"] = shade
            return t
        }
        mail.refresh()
        var listed = Desk.list([grouped("A", unread: true, group: "Work", sub: "Alpha", colour: "#0E8A84",
                                        shade: "#006A65"),
                                grouped("B", unread: false, group: nil, sub: nil, colour: "#8C877F", shade: "#8C877F"),
                                grouped("C", unread: false, group: "Work", sub: nil, colour: "teal", shade: "#0E8A84"),
                                thread("D", newest: 9, last: 90)])              // a desktop that sends none
        listed["groups_error"] = "ValueError: groups.json does not hold a layout"
        desk.answer("mail_list", listed)
        let got = mail.threads ?? []
        check(got.map(\.id) == ["A", "B", "D"] && (mail.listError ?? "").contains("not a #RRGGBB colour: teal"),
              "a colour that is not #RRGGBB leaves its thread out, said: \(got.map(\.id)) \(mail.listError ?? "none")")
        check(got.first?.group == "Work" && got.first?.subgroup == "Alpha"
              && got.first?.colour == HexColour(rgb: 0x0E8A84) && got.first?.shade == HexColour(rgb: 0x006A65),
              "a thread keeps its group and colours: \(String(describing: got.first))")
        check(got.count == 3 && got[1].group == nil && got[1].colour == HexColour(rgb: 0x8C877F) && got[2].colour == nil,
              "Ungrouped has its grey; a thread sent none has none")
        check(mail.groupsError == "ValueError: groups.json does not hold a layout", "groups_error is kept to be said")
        guard got.count == 3 else { return }
        check(got.map(MailRow.groupName) == ["Work: Alpha", "Ungrouped", nil],
              "hovering names the group: \(got.map(MailRow.groupName))")

        // drawn, light and dark: the row right of the strip, the strip at the left edge, the dots
        let groundAt = (x: CGFloat(296), y: CGFloat(40)), stripAt = (x: CGFloat(2), y: CGFloat(40))
        let dotY = 8 + 5 + MailRow.dot / 2
        let blueX = MailRow.padding + MailRow.dot / 2, brownX = blueX + MailRow.dot + MailRow.dotGap
        let hex = { (c: UInt32) in String(format: "%06X", c) }
        /// Thread `t` as one the desktop sent no colours with.
        func plain(_ t: MailboxThread) -> MailboxThread {
            var p = t
            (p.group, p.subgroup, p.colour, p.shade) = (nil, nil, nil, nil)
            return p
        }
        for dark in [false, true] {
            let mode = dark ? "dark" : "light"
            func row(_ t: MailboxThread, unread: Bool, selected: Bool = false,
                     working: AgentState? = nil) -> NSBitmapImageRep? {
                var t = t
                t.unread = unread
                return drawnRow(MailRow(thread: t, selected: selected, now: Date(timeIntervalSince1970: 1000), mixed: false,
                                        draft: false, working: working), dark: dark)
            }
            for (t, what) in [(got[0], "Work: Alpha"), (got[1], "Ungrouped")] {
                for (unread, selected) in [(true, false), (false, false), (true, true), (false, true)] {
                    let state = "\(mode), \(what), \(unread ? "unread" : "read")\(selected ? ", open" : "")"
                    guard let coloured = row(t, unread: unread, selected: selected),
                          let bare = row(plain(t), unread: unread, selected: selected) else {
                        check(false, "\(state): the rows could not be drawn")
                        continue
                    }
                    let grounds = [coloured, bare].map { hex(pixel($0, groundAt.x, groundAt.y)) }
                    check(same(coloured, bare, from: MailRow.strip + 1),
                          "\(state): drawn as the plain row but for its strip: grounds \(grounds[0]) and \(grounds[1])")
                }
            }
            guard let unread = row(got[0], unread: true, working: .busy), let none = row(got[1], unread: false),
                  let plainRow = row(got[2], unread: true), let open = row(got[0], unread: false, selected: true),
                  let shut = row(got[0], unread: false), let blueSwatch = drawnRow(Palette.ok, dark: dark),
                  let brownSwatch = drawnRow(AgentState.busy.color, dark: dark),
                  let shadeSwatch = drawnRow(Color(hex: 0x006A65), dark: dark),
                  let greySwatch = drawnRow(Color(hex: 0x8C877F), dark: dark) else {
                check(false, "\(mode): the rows could not be drawn")
                return
            }
            check(!near(pixel(open, groundAt.x, groundAt.y), pixel(shut, groundAt.x, groundAt.y)),
                  "\(mode): the open thread's row is on the selection's blue: \(hex(pixel(open, groundAt.x, groundAt.y)))")
            check(near(pixel(unread, stripAt.x, stripAt.y), pixel(shadeSwatch, 150, 35))
                  && near(pixel(none, stripAt.x, stripAt.y), pixel(greySwatch, 150, 35))
                  && near(pixel(open, stripAt.x, stripAt.y), pixel(shadeSwatch, 150, 35)),
                  "\(mode): the strip is the sub-group's shade, Ungrouped's grey, open or not: "
                  + "\(hex(pixel(unread, stripAt.x, stripAt.y)))")
            check(pixel(plainRow, stripAt.x, stripAt.y) == pixel(plainRow, groundAt.x, groundAt.y),
                  "\(mode): a thread sent no colours has no strip")
            check(near(pixel(unread, blueX, dotY), pixel(blueSwatch, 150, 35))
                  && near(pixel(unread, brownX, dotY), pixel(brownSwatch, 150, 35)),
                  "\(mode): the blue and brown dots as ever on a grouped row: \(hex(pixel(unread, blueX, dotY))) "
                  + "\(hex(pixel(unread, brownX, dotY)))")
        }
    }

    /// The list's left edge: the box switch (BoxSwitch) and the buttons over the list (ThreadActions) fit its column
    /// at every zoom, with a thread open (Archive, the longest) or none, so opening a thread moves nothing in the
    /// column and each row's strip stays at the tab's left edge. A segment of the switch, clicked, lists its box;
    /// Snooze, clicked, pops up the Snooze menu, as s does (--selftest-popout).
    static func edge(_ check: (Bool, String) -> Void) {
        let desk = Desk()
        defer { desk.problems.forEach { check(false, $0) } }
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        let mail = desk.model(folder: folder, upload: { _, _ in })
        let a = thread("A", newest: 9, last: 90), b = thread("B", newest: 8, last: 80)
        mail.refresh()
        desk.answer("mail_list", Desk.list([a, b]))

        /// The room over the list, before zoom.
        let room = MailListColumn.width - 2 * MailListColumn.padding
        /// Whether `view` fits its room in the column at every zoom.
        func fits<V: View>(_ what: String, _ view: V) {
            for z in BoardModel.zoomSteps {
                let (win, host) = Renderer.offscreen(view.environment(\.zoom, z), size: nil, dark: false)
                let width = host.fittingSize.width
                check(width <= room * z, "\(what), zoom \(z): it fits the column: \(width) in \(room * z)")
                withExtendedLifetime(win) {}
            }
        }

        fits("the box switch", BoxSwitch(box: .inbox, show: { _ in }))
        var listed: [MailBox] = []
        let (switchWin, switchHost) = Renderer.offscreen(BoxSwitch(box: .inbox, show: { listed.append($0) })
                                                            .frame(width: room), size: nil, dark: false)
        let third = NSPoint(x: room * 5 / 8, y: switchHost.bounds.midY)          // the middle of the third of four
        PopoutTest.click(switchHost.convert(third, to: nil), in: switchWin, count: 1)
        check(Renderer.wait(3, step: 0.01, until: { !listed.isEmpty }) && listed == [.archived],
              "Archived, clicked, lists the archive: \(listed)")
        withExtendedLifetime(switchWin) {}

        /// The left edge of the list (the scroll view the column's width) in the tab drawn by `host`.
        func listLeft(_ host: NSView) -> CGFloat? {
            settle(host)
            return Renderer.all(NSScrollView.self, in: host).map { $0.convert($0.bounds, to: host) }
                .first { $0.width == MailListColumn.width }?.minX
        }

        fits("the buttons over the list, none open", ThreadActions(mail: mail, window: nil))
        let tab = MailView(mail: mail, connected: true, available: true, working: [:]).environment(\.zoom, 1)
        let (win, host) = Renderer.offscreen(tab, size: CGSize(width: 1000, height: 700), dark: false)
        let before = listLeft(host)
        check(before == 0, "with no thread open, the list starts at the tab's left edge: \(String(describing: before))")
        mail.open("A")
        desk.answer("mail_show", thread: "A", Desk.show(a, Desk.messages(["s1", "s2"])))
        let after = listLeft(host)
        check(after == 0, "a thread opened, the list has not moved: its left edge at \(String(describing: after))")

        // Snooze, clicked: the Snooze menu pops up under the button (mail.popUp, kept off the screen here), as s does
        var popped: [(menu: NSMenu, at: NSView)] = []
        mail.popUp = { popped.append(($0, $1)) }
        if let button = Renderer.find(MenuAnchor.Empty.self, in: host) {
            PopoutTest.click(button.convert(NSPoint(x: button.bounds.midX, y: button.bounds.midY), to: nil), in: win,
                             count: 1)
            check(Renderer.wait(3, step: 0.01, until: { !popped.isEmpty }) && popped.count == 1
                  && popped.first?.at === button, "Snooze clicked: one menu, at the button: \(popped.count)")
            let titles = popped.first?.menu.items.filter { !$0.isSeparatorItem }.map(\.title)
            check(titles == Snooze.items(now: Date()).map(\.title), "the Snooze menu's items: \(titles ?? [])")
        } else {
            check(false, "the tab has no Snooze button")
        }
        withExtendedLifetime(win) {}
        fits("the buttons over the list, a thread open", ThreadActions(mail: mail, window: nil))
    }

    /// The busy dots: a thread is dotted while an agent that wrote in it is at work on the board (busy, or waiting on a
    /// background command it started), every thread of it, busy winning over background, and follows the tiles as
    /// their states change, from the board's snapshot alone. Drawn, the dot takes its own place beside the unread one,
    /// in its tile's colour, and moves nothing else in the row.
    static func dots(_ check: (Bool, String) -> Void) {
        let board = BoardModel(layout: Layout(), persist: false,
                               mail: MailModel(call: { _, _ in }, upload: { _, _ in }, ahead: 0, cacheBytes: 0,
                                               draftsFolder: MailDrafts.scratchFolder()))
        defer { try? FileManager.default.removeItem(at: board.mail.drafts.folder) }   // this test's own folder
        func agents(alpha: String, beta: String) -> [[String: Any]] {
            [agent("A1", "alpha", state: alpha, thread: "T2", lastOut: 300),
             agent("B1", "beta", cli: "codex", state: beta, thread: "T3", lastOut: 500)]
        }
        let rows: [MailboxThread] = [
            thread("T1", newest: 1, last: 100, box: .archived, by: [("claude", "A1")]),     // alpha's older thread
            thread("T2", newest: 9, last: 300, by: [("claude", "A1")]),                     // its latest
            thread("T3", newest: 21, last: 500, by: [("codex", "B1")]),                     // beta's
            thread("T4", newest: 30, last: 600, by: [("claude", "A1"), ("codex", "B1")]),   // both of them
            thread("T5", newest: 31, last: 700, by: [("codex", "A1")]),                     // another CLI's A1
            thread("T6", newest: 32, last: 800, by: []),                                    // the mailroom's own
            thread("T7", newest: 33, last: 900)                                             // a desktop that says nothing
        ].compactMap { t in try? JSON.decoder.decode(MailboxThread.self, from: JSONSerialization.data(withJSONObject: t)) }
        check(rows.count == 7, "the stand-in threads decode: \(rows.count)")
        func dotted() -> [String: AgentState] {
            Dictionary(uniqueKeysWithValues: rows.compactMap { t in
                MailRules.working(t, board.workingAgents).map { (t.id, $0) }
            })
        }
        func tab() -> MailView { MailView(mail: board.mail, connected: true, available: true, working: board.workingAgents) }
        let alphaKey = AgentKey(cli: "claude", id: "A1"), betaKey = AgentKey(cli: "codex", id: "B1")

        guard let first = snapshot(agents(alpha: "busy", beta: "idle"), check) else { return }
        board.apply(first)
        check(board.workingAgents == [alphaKey: .busy], "alpha is busy: \(board.workingAgents)")
        check(dotted() == ["T1": .busy, "T2": .busy, "T4": .busy],
              "a busy agent's threads, the older one too, are dotted: \(dotted())")
        let before = tab()
        guard let second = snapshot(agents(alpha: "idle", beta: "busy"), check) else { return }
        board.apply(second)
        check(dotted() == ["T3": .busy, "T4": .busy], "alpha idle and beta busy: the dots follow the tiles: \(dotted())")
        check(before != tab(), "and the tab is drawn again")
        guard let third = snapshot(agents(alpha: "waiting", beta: "background"), check) else { return }
        board.apply(third)
        check(board.workingAgents == [betaKey: .background] && dotted() == ["T3": .background, "T4": .background],
              "waiting on you: no dot; waiting on a background command it started: the background dot: \(dotted())")
        check(tab() != MailView(mail: board.mail, connected: true, available: true, working: [betaKey: .busy]),
              "busy to background draws the tab again")
        guard let fourth = snapshot(agents(alpha: "busy", beta: "background"), check) else { return }
        board.apply(fourth)
        check(dotted() == ["T1": .busy, "T2": .busy, "T3": .background, "T4": .busy],
              "a thread of a busy agent and a background one is dotted busy: \(dotted())")
        check(AgentState.foremost([.background, .busy]) == .busy && AgentState.foremost([.waiting, .background])
              == .background && AgentState.foremost([.waiting, .blocked, .idle, .stale, .offline]) == nil
              && AgentState.foremost([]) == nil, "busy outranks background, and no other state makes a dot")
        guard let twice = snapshot([agent("A1", "alpha", state: "background", thread: "T2", lastOut: 300),
                                    agent("A1", "alpha", state: "busy", thread: "T2", lastOut: 300)], check) else { return }
        board.apply(twice)
        check(board.workingAgents == [alphaKey: .busy],
              "one agent on two tiles, background and busy, is busy: \(board.workingAgents)")
        board.apply(second)
        check(before != tab() && tab() == MailView(mail: board.mail, connected: true, available: true,
                                                   working: [betaKey: .busy]),
              "the tab is drawn again only when the agents at work change")

        // drawn: the dot in its own place, after the blue one's; the rest of the row where it was
        func drawn<V: View>(_ view: V) -> NSBitmapImageRep? { drawnRow(view, dark: false) }
        func drawn(unread: Bool, working: AgentState?) -> NSBitmapImageRep? {
            var t = rows[1]
            t.unread = unread
            return drawn(MailRow(thread: t, selected: false, now: Date(timeIntervalSince1970: 1000), mixed: false,
                                 draft: false, working: working))
        }
        /// Whether two pictures of a row are the same, byte for byte, right of the dots.
        func sameText(_ a: NSBitmapImageRep, _ b: NSBitmapImageRep) -> Bool {
            same(a, b, from: MailRow.textInset - MailRow.gap)
        }
        let y = 8 + 5 + MailRow.dot / 2                                       // the row's padding, the dots' own, half one
        let blueX = MailRow.padding + MailRow.dot / 2, brownX = blueX + MailRow.dot + MailRow.dotGap
        guard let plain = drawn(unread: false, working: nil), let busy = drawn(unread: false, working: .busy),
              let background = drawn(unread: false, working: .background),
              let unread = drawn(unread: true, working: nil), let both = drawn(unread: true, working: .busy) else {
            check(false, "the rows could not be drawn")
            return
        }
        // the dots' colours as this window draws them (its colour space is not always sRGB): a swatch of each
        guard let brownSwatch = drawn(AgentState.busy.color), let darkSwatch = drawn(AgentState.background.color),
              let blueSwatch = drawn(Palette.ok) else {
            check(false, "the swatches could not be drawn")
            return
        }
        let brown = pixel(brownSwatch, 150, 35), dark = pixel(darkSwatch, 150, 35), blue = pixel(blueSwatch, 150, 35)
        let hex = { (c: UInt32) in String(format: "%06X", c) }
        check(near(pixel(busy, brownX, y), brown) && !near(pixel(plain, brownX, y), brown),
              "busy: a brown dot in its own place: \(hex(pixel(busy, brownX, y))) (a busy tile's \(hex(brown))), "
              + "not at work: \(hex(pixel(plain, brownX, y)))")
        check(near(pixel(background, brownX, y), dark) && !near(dark, brown),
              "background: the dot in a background tile's darker caramel: \(hex(pixel(background, brownX, y))) "
              + "(\(hex(dark)), a busy one's \(hex(brown)))")
        check(near(pixel(both, blueX, y), blue) && near(pixel(both, brownX, y), brown),
              "unread and busy: blue, then brown: \(hex(pixel(both, blueX, y))) \(hex(pixel(both, brownX, y)))")
        check(pixel(busy, blueX, y) == pixel(plain, blueX, y), "busy alone leaves the blue dot's place empty")
        check(sameText(plain, busy) && sameText(plain, background) && sameText(unread, both),
              "the dot moves nothing else in the row")
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

        // saved and read back by a new MailDrafts, as after a restart, with answers and comments; a reply on its way
        // keeps its key
        let c = kept.draft("C")
        c.text = "Sending when the app quit"
        let key = c.begin()?.key
        let answered = kept.draft("D")
        answered.pick(DraftPick(widget: "s12-1", picked: [0, 2], question: "Which days?", labels: ["Mon", "Wed"]),
                      comment: true)
        answered.note("not Friday", on: "s12-1")
        answered.addComment(message: "s12", at: 40, quote: "the 512 MB\ncache size")
        answered.note("Can we try 1 GB too?", on: answered.comments[0].id)
        kept.saveNow()
        drafts = nil
        let again = MailDrafts(folder: folder)
        check(again.loadProblem == nil && again.held == ["A", "B", "C", "D"], "the drafts again: \(again.held)")
        check(again.draft("A").text == "Half a thought" && again.draft("B").files.map(\.name) == ["results.csv"]
              && again.draft("C").key == key && key != nil, "with their text, files and keys")
        check(again.draft("D").picks == answered.picks && again.draft("D").comments == answered.comments
              && again.draft("D").reply == answered.reply, "and their answers and comments: \(again.draft("D").reply.debugDescription)")
        again.discard("D")
        var undo: DraftSnapshot?
        again.discard("B") { _, before in undo = before }
        let copies = { (try? fm.contentsOfDirectory(atPath: folder.path))?.filter { $0 != "drafts.json" && $0 != "src" } ?? [] }
        check(again.draft("B").files.isEmpty && !again.draft("B").shown && again.held == ["A", "C"]
              && copies() == again.draft("B").files.map(\.id) + (undo?.files.map(\.id) ?? []),
              "discarded: emptied, its file's copy kept for Undo alone, none of the file taken out earlier: \(copies())")
        if let b = undo { again.bringBack("B", b) }
        check(again.draft("B").files.map(\.name) == ["results.csv"] && again.held == ["A", "B", "C"], "Undo: back, with its file")
        again.discard("B")
        again.saveNow()
        _ = MailDrafts(folder: folder)                    // the next start: what nothing needs goes
        check(copies().isEmpty, "a discarded draft's copies go once Undo cannot bring it back: \(copies())")

        // a drafts.json saved before answers and comments were kept reads, with none
        let older = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: older) }             // this test's own folder
        try? fm.createDirectory(at: older, withIntermediateDirectories: true)
        try? Data(#"{"E": {"text": "from before", "files": []}}"#.utf8).write(to: older.appendingPathComponent("drafts.json"))
        let before = MailDrafts(folder: older)
        check(before.loadProblem == nil && before.draft("E").text == "from before" && before.draft("E").picks.isEmpty
              && before.draft("E").comments.isEmpty, "an older drafts.json: \(before.loadProblem ?? "read")")

        // a saved draft whose file's copy has gone keeps the rest, and says so
        check(again.add([.image(Data([9]))], to: "A") == .added, "an image for A")
        if let f = again.draft("A").files.first {
            again.saveNow()
            try? fm.removeItem(at: again.url(f))
            let lost = MailDrafts(folder: folder)
            check(lost.draft("A").files.isEmpty && lost.draft("A").text == "Half a thought"
                  && (lost.loadProblem ?? "").contains(f.name), "a missing copy: left out, and named: \(lost.loadProblem ?? "none")")
        }

        // a drafts.json whose answers are in another shape (an earlier build's "choice") is moved aside and said, not
        // read as no drafts
        let shaped = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: shaped) }            // this test's own folder
        try? fm.createDirectory(at: shaped, withIntermediateDirectories: true)
        let earlier = #"{"G": {"text": "kept", "files": [], "picks": [{"widget": "s1-0", "picked": [0], "question": "Q?", "#
            + #""choice": "Option 1: A"}]}}"#
        try? Data(earlier.utf8).write(to: shaped.appendingPathComponent("drafts.json"))
        let reshaped = MailDrafts(folder: shaped)
        let shapedAside = (try? fm.contentsOfDirectory(atPath: shaped.path))?.filter { $0.hasPrefix("drafts.json.unread-") } ?? []
        check(reshaped.held.isEmpty && shapedAside.count == 1 && (reshaped.loadProblem ?? "").contains(shapedAside.first ?? "?"),
              "drafts of another shape: kept aside and said: \(reshaped.loadProblem ?? "none")")

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
        finishUpload("/home/alex/.local/state/agent-board/mail-files/2026-11-18/film.mov", nil)
        check(x.copying.isEmpty && x.text == "The film:\n\nToo big to attach, so copied to the desktop: "
              + "/home/alex/.local/state/agent-board/mail-files/2026-11-18/film.mov (25.0M)", "its path in the reply: \(x.text)")
        mail.copyToDesktop([big], for: "X")
        finishUpload(nil, "ssh: connection refused")
        check(x.error == "film.mov not copied to the desktop: ssh: connection refused" && x.copying.isEmpty,
              "a copy that fails says why: \(x.error ?? "none")")
        check(!asked.contains("mail_reply"), "nothing sent meanwhile: \(asked)")
    }

    /// Drafts kept on the desktop and shared between devices (MailDrafts with a desktop, here DeskDrafts): read when the
    /// thread opens, saved a moment after typing stops, a change another device made meanwhile shown with a note and this
    /// Mac's kept as a version, which the Versions menu brings back; Discard on the Undo bar, undone from the desktop's
    /// version; a save that fails kept in drafts.json and saved once the desktop answers again; files uploaded before
    /// the draft naming them and sent from the desktop's copy, which empties the draft there; a file another device attached
    /// sent from here; the list's marks from the desktop's list; and drafts saved before the desktop kept them sent
    /// there at the first list, copies under an earlier build's ids renamed to their digests.
    static func shared(_ check: (Bool, String) -> Void) {
        func snap(_ text: String, files: [DraftFile] = []) -> DraftSnapshot {
            DraftSnapshot(text: text, picks: [], comments: [], files: files, key: nil)
        }
        let desk = Desk(), kept = DeskDrafts()
        defer { desk.problems.forEach { check(false, $0) } }
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        let mail = desk.model(folder: folder, upload: { _, _ in }, drafts: kept)
        let a = thread("A", newest: 5, last: 50)
        let settle = saveDelay + 0.15

        // opened: the draft saved on the laptop shows
        kept.save("A", snap("From the laptop"))
        mail.open("A")
        desk.answer("mail_show", Desk.show(a, [Desk.message("s5")]))
        pass(0.05)
        let d = mail.draft(for: "A")
        check(d.text == "From the laptop" && d.shown && kept.asked.compactMap { $0["action"] as? String } == ["draft_get"],
              "a thread opened shows the desktop's draft: \(d.text.debugDescription) \(kept.asked)")

        // typing: saved once, a moment after it stops, as this Mac's
        d.text = "From the laptop, and the Mac"
        pass(saveDelay / 2)
        d.text += "."
        pass(saveDelay / 2)
        check(kept.puts.isEmpty, "not saved while typing goes on: \(kept.puts.count)")
        pass(settle)
        check(kept.puts.count == 1 && kept.threads["A"]?.draft?.text == "From the laptop, and the Mac."
              && kept.threads["A"]?.device == "mac", "saved once typing stopped: \(kept.puts)")

        // the laptop changed it meanwhile: this Mac's change is not taken but kept as a version, the draft shows as the
        // laptop left it, and the box says so
        kept.save("A", snap("The laptop's newer words"))
        d.text = "The Mac's words on the old draft"
        pass(settle)
        check(d.text == "The laptop's newer words"
              && d.note == "This draft changed on your laptop since; your version is under Versions.",
              "a conflict: the desktop's draft, and a note: \(d.text.debugDescription) \(d.note ?? "no note")")
        check(kept.threads["A"]?.versions.last?.content.text == "The Mac's words on the old draft"
              && d.versions.first?.why == "conflict" && kept.threads["A"]?.draft?.text == "The laptop's newer words",
              "this Mac's words kept as a version: \(d.versions.map(\.words))")

        // typing on while that save was on its way: what was typed is kept as a version too
        kept.save("A", snap("The laptop's newest words"))
        d.text = "Saved on an old draft"
        kept.holding = true
        pass(settle)
        d.text = "Saved on an old draft, and more typed meanwhile"
        kept.release()
        pass(0.2)
        let kepts = kept.threads["A"]?.versions.map(\.content.text) ?? []
        check(kepts.suffix(2) == ["Saved on an old draft", "Saved on an old draft, and more typed meanwhile"]
              && d.text == "The laptop's newest words", "both kept as versions, the laptop's draft shown: \(kepts)")
        kept.save("A", snap("The laptop's newer words"))
        mail.refresh()
        pass(0.1)
        desk.answer("mail_list", Desk.list([a]))
        check(d.text == "The laptop's newer words", "and read again as the laptop left it: \(d.text.debugDescription)")
        let line = d.versions.first(where: { $0.why == "conflict" && $0.words == "The Mac's words on the old draft" })?
            .label(now: Date()) ?? "none"
        check(line.hasPrefix("Not taken ") && line.hasSuffix(", Mac: The Mac's words on the old draft"), "the menu's line: \(line)")

        // Versions: one brought back, the draft it replaces kept
        if let v = d.versions.first(where: { $0.words == "The Mac's words on the old draft" }) { mail.restoreVersion("A", v) }
        pass(0.15)
        check(d.text == "The Mac's words on the old draft" && kept.threads["A"]?.draft?.text == d.text && d.note == nil,
              "a version brought back, here and on the desktop: \(d.text.debugDescription)")
        check(d.versions.contains { $0.why == "replaced" && $0.words == "The laptop's newer words" },
              "the draft it replaced is kept: \(d.versions.map(\.words))")

        // Discard: emptied at once, offered on the Undo bar once the desktop has it as a version; Undo brings it back
        mail.discard("A")
        check(d.blank && mail.undos.isEmpty && mail.undoWaiting == 1, "Discard: empty at once, the bar waits for the desktop")
        pass(0.1)
        check(kept.threads["A"]?.draft == nil && mail.undos.last?.line == "Draft discarded" && mail.undoWaiting == 0
              && !mail.drafts.held.contains("A"), "discarded there too, and offered: \(mail.undos.map(\.line))")
        mail.undo()
        pass(0.15)
        check(d.text == "The Mac's words on the old draft" && kept.threads["A"]?.draft?.text == d.text && mail.undos.isEmpty,
              "Undo: back, here and on the desktop: \(d.text.debugDescription)")

        // a save that fails: kept in drafts.json, saved once the desktop answers again
        kept.down = "ssh: connection refused"
        d.text = "Written while the desktop is away"
        pass(settle)
        check(kept.threads["A"]?.draft?.text == "The Mac's words on the old draft"
              && d.deskProblem == "ssh: connection refused", "not on the desktop yet, and the box says why: \(d.deskProblem ?? "none")")
        mail.drafts.saveNow()
        check(MailDrafts(folder: folder).draft("A").text == "Written while the desktop is away",
              "this Mac keeps it, as after a restart")
        kept.down = nil
        mail.refresh()
        pass(0.15)
        desk.answer("mail_list", Desk.list([a]))
        check(kept.threads["A"]?.draft?.text == "Written while the desktop is away" && d.deskProblem == nil,
              "saved at the next list, once the desktop answers: \(kept.threads["A"]?.draft?.text ?? "none")")

        // a file: uploaded before the draft naming it, its copy here then gone; sent from the desktop's copy, after
        // the draft is saved with the reply's key, which empties the draft there for the laptop too
        let png = Data([0x89, 0x50, 0x4E, 0x47, 7, 7])
        check(mail.drafts.add([.image(png)], to: "A") == .added, "a pasted image is taken")
        pass(settle)
        let f = d.files.first
        check(f.map { kept.files[$0.id] == png && kept.threads["A"]?.draft?.files == [$0] } == true,
              "uploaded, then named in the draft: \(kept.threads["A"]?.draft?.files ?? [])")
        check(f.map { !FileManager.default.fileExists(atPath: mail.drafts.url($0).path) } == true,
              "and its copy here goes once the desktop has the draft")
        if case .refused(let why) = mail.drafts.add([.image(png)], to: "A") {
            check(why.hasSuffix(" is in this reply already."), "the same file twice: \(why)")
        } else {
            check(false, "the same file was taken twice")
        }
        mail.send("A")
        pass(0.1)
        let key = kept.threads["A"]?.draft?.key
        check(key != nil && desk.waiting == ["mail_reply"], "the draft saved with the reply's key first: \(desk.waiting)")
        let rev = key.flatMap { kept.sent("A", key: $0) }
        let reply = desk.answer("mail_reply", ["ok": true, "key": "i9", "status": "sending",
                                               "draft": ["cleared": rev != nil, "rev": rev ?? -1]])
        check((reply["files"] as? [[String: String]]) == f.map { [["name": $0.name, "draft": $0.id]] }
              && reply["key"] as? String == key, "the file named from the desktop's copy: \(reply["files"] ?? "none")")
        check(d.blank && !d.frozen && kept.threads["A"]?.draft == nil, "sent: empty here and on the desktop")
        desk.answer("mail_show", Desk.show(a, [Desk.message("s5"), Desk.message("i9", mine: true, status: "sending")]))
        desk.answer("mail_list", Desk.list([a]))
        pass(settle)
        check((kept.puts.last?["draft"] as? [String: Any])?["key"] != nil, "and no save after it: \(kept.puts.count)")

        // a file attached on the laptop: shown here once the list says the draft changed, and sent from the desktop
        let jpg = Data("not really a photo".utf8)
        let otherFile = DraftFile(id: MailDrafts.digest(jpg), name: "laptop.jpg", size: jpg.count)
        kept.save("A", snap("From my laptop", files: [otherFile]), data: [jpg])
        mail.refresh()
        pass(0.15)
        desk.answer("mail_list", Desk.list([a]))
        check(d.files == [otherFile] && d.text == "From my laptop", "the laptop's draft, read again: \(d.files)")
        mail.send("A")
        pass(0.1)
        let otherKey = kept.threads["A"]?.draft?.key
        let otherRev = otherKey.flatMap { kept.sent("A", key: $0) }
        let fromOther = desk.answer("mail_reply", ["ok": true, "key": "i10", "status": "sending",
                                                   "draft": ["cleared": otherRev != nil, "rev": otherRev ?? -1]])
        check((fromOther["files"] as? [[String: String]]) == [["name": "laptop.jpg", "draft": otherFile.id]],
              "sent from the desktop's copy: \(fromOther["files"] ?? "none")")
        desk.answer("mail_show", Desk.show(a, [Desk.message("s5")]))
        desk.answer("mail_list", Desk.list([a]))

        // the list's marks: a draft saved on the laptop is marked; one emptied there is not
        kept.save("B", snap("A draft on the laptop"))
        mail.refresh()
        pass(0.1)
        desk.answer("mail_list", Desk.list([a, thread("B", newest: 4, last: 40)]))
        check(mail.drafts.held == ["B"], "marked from the desktop's list: \(mail.drafts.held)")
        kept.save("B", nil)
        mail.refresh()
        pass(0.1)
        desk.answer("mail_list", Desk.list([a, thread("B", newest: 4, last: 40)]))
        check(mail.drafts.held.isEmpty, "and no longer once emptied there: \(mail.drafts.held)")

        // drafts saved before the desktop kept them: sent there at the first list, a copy kept under an earlier
        // build's id renamed to its digest and uploaded
        let older = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: older) }           // this test's own folder
        try? FileManager.default.createDirectory(at: older, withIntermediateDirectories: true)
        let txt = Data("old notes".utf8), uuid = "0E1F2A3B-0000-4000-8000-000000000000"
        try? txt.write(to: older.appendingPathComponent(uuid))
        try? Data(#"{"M": {"text": "from before", "key": null, "files": [{"id": "\#(uuid)", "name": "notes.txt", "size": 9}]}}"#.utf8)
            .write(to: older.appendingPathComponent("drafts.json"))
        let desk2 = Desk(), kept2 = DeskDrafts()
        defer { desk2.problems.forEach { check(false, $0) } }
        let migrated = desk2.model(folder: older, upload: { _, _ in }, drafts: kept2)
        check(migrated.drafts.draft("M").files.map(\.id) == [MailDrafts.digest(txt)]
              && FileManager.default.fileExists(atPath: older.appendingPathComponent(MailDrafts.digest(txt)).path),
              "the copy renamed to its digest: \(migrated.drafts.draft("M").files)")
        migrated.refresh()
        pass(0.2)
        desk2.answer("mail_list", Desk.list([]))
        check(kept2.threads["M"]?.draft?.text == "from before" && kept2.files[MailDrafts.digest(txt)] == txt,
              "sent to the desktop at the first list, with its file: \(kept2.asked.compactMap { $0["action"] as? String })")
    }

    /// ReplyText and MailWidgets.answer on the cases the phone's copy of them is checked on too (reply-text-cases.json,
    /// a resource of the app; web/mail/replytext.js under node in tests/test_mailweb.py): how a draft trims the owner's
    /// words, the agent's text made plain, what widgets.js posts as the draft takes it, the text a draft sends, and what
    /// a draft held still says (Draft.frozenWhy; the phone's page is checked on it as it shows it). Each text is
    /// compared scalar for scalar, as the phone's are, not by Swift's canonical equivalence.
    static func sharedCases(_ check: (Bool, String) -> Void) {
        struct Trim: Decodable { var what, text, trimmed: String }
        struct Plain: Decodable { var what, text, plain: String; var lines: Bool }
        struct Reply: Decodable { var what, text, reply: String; var picks: [DraftPick]; var comments: [DraftComment] }
        struct Taken: Decodable { var answer: DraftPick; var comment: Bool }
        struct Answer: Decodable { var what: String; var taken: Taken? }
        struct Answers: Decodable { var widgets: [MailWidget]; var cases: [Answer] }
        struct Cases: Decodable {
            var frozenWhy: String
            var trim: [Trim]
            var plain: [Plain]
            var answers: Answers
            var replies: [Reply]
        }
        let cases: Cases, posts: [Any]
        do {
            let data = try Data(contentsOf: MailWidgets.resource("reply-text-cases", "json"))
            cases = try JSONDecoder().decode(Cases.self, from: data)
            let answers = (try JSONSerialization.jsonObject(with: data) as? [String: Any])?["answers"] as? [String: Any]
            posts = (answers?["cases"] as? [[String: Any]] ?? []).map { $0["post"] as Any }   // as widgets.js posts
        } catch {
            return check(false, "the reply text's cases: \(error)")
        }
        func sameText(_ a: String, _ b: String) -> Bool { Array(a.unicodeScalars) == Array(b.unicodeScalars) }
        func samePick(_ a: DraftPick, _ b: DraftPick) -> Bool {
            a.widget == b.widget && a.picked == b.picked && sameText(a.question, b.question) && a.comment == b.comment
                && a.labels.count == b.labels.count && zip(a.labels, b.labels).allSatisfy { sameText($0, $1) }
        }
        check(!cases.trim.isEmpty && !cases.plain.isEmpty && !cases.replies.isEmpty && !cases.answers.cases.isEmpty
              && posts.count == cases.answers.cases.count, "the reply text's cases: every kind, each answer's post")
        check(sameText(Draft.frozenWhy, cases.frozenWhy), "a held draft's words: \(cases.frozenWhy.debugDescription)")
        for t in cases.trim {
            let got = Draft(text: t.text, files: [], key: nil).content.text
            check(sameText(got, t.trimmed), "trim, \(t.what): \(got.debugDescription)")
        }
        for t in cases.plain {
            let got = ReplyText.plain(t.text, lines: t.lines)
            check(sameText(got, t.plain), "plain, \(t.what): \(got.debugDescription)")
        }
        for (t, post) in zip(cases.answers.cases, posts) {
            let got = MailWidgets.answer(post, of: cases.answers.widgets)
            let ok = got.map { g in t.taken.map { g.comment == $0.comment && samePick(g.answer, $0.answer) } ?? false }
                ?? (t.taken == nil)
            check(ok, "an answer, \(t.what): \(String(describing: got))")
        }
        for t in cases.replies {
            let got = Draft(text: t.text, files: [], key: nil, picks: t.picks, comments: t.comments).reply
            check(sameText(got, t.reply), "a reply, \(t.what): \(got.debugDescription)")
        }
    }

    /// Answers to an agent's options and comments on its text (Draft.pick, Draft.addComment): kept apart, in the
    /// thread's order, one answer per question; the text a reply sends (ReplyText); sending and Discard. Widgets as
    /// mail_show gives them. (The answers themselves come from widgets.js, --selftest-widgets; the comments from the
    /// document, --selftest-comments.)
    static func answers(_ check: (Bool, String) -> Void) {
        func pick(_ widget: String, _ picked: [Int], _ question: String, _ labels: [String]) -> DraftPick {
            DraftPick(widget: widget, picked: picked, question: question, labels: labels)
        }
        let cache = pick("s12-0", [1], "Which cache policy should the loader use?", ["LFU with ageing, 512 MB"])
        let ship = pick("s12-1", [0, 2], "Which days may it ship?", ["Monday", "Wednesday"])
        let early = pick("s3-0", [0], "Keep the old loader?", ["Yes"])
        let d = Draft(text: "", files: [], key: nil)
        d.pick(ship, comment: false)
        d.pick(cache, comment: false)
        check(d.picks == [cache, ship] && d.shown && !d.wantsKeyboard && d.focus == nil,
              "two questions: both answers, in the widgets' order; the box opens and the keyboard stays: \(d.picks)")
        d.pick(early, comment: false)
        check(d.picks.map(\.widget) == ["s3-0", "s12-0", "s12-1"], "an earlier mail's comes first (by number): \(d.picks)")
        let lru = pick("s12-0", [0], cache.question, ["LRU, 256 MB"])
        /// The draft's answer to the cache question.
        let cached = { d.picks.first { $0.widget == "s12-0" } }
        d.pick(lru, comment: true)
        check(d.picks.map(\.labels) == [["Yes"], ["LRU, 256 MB"], ship.labels] && cached()?.comment == ""
              && d.focus == "s12-0", "another option: only its answer changes; with a comment, a field with the keyboard")
        d.note("cap it at 1 GB", on: "s12-0")
        d.pick(cache, comment: false)
        check(cached()?.labels == cache.labels && cached()?.comment == "cap it at 1 GB", "picked again: the comment stays")
        d.pick(pick("s12-1", [], ship.question, []), comment: false)
        d.drop("s3-0")
        check(d.picks.map(\.widget) == ["s12-0"], "unpicked, or its × : the answer goes: \(d.picks)")
        d.addComment(message: "s12", at: 40, quote: "the 512 MB cache size")
        let quoted = d.comments.first
        check(quoted?.comment == "" && d.focus == quoted?.id && !d.blank, "a comment on text: its field has the keyboard")
        d.addComment(message: "s3", at: 900, quote: "the old loader")
        d.addComment(message: "s12", at: 7, quote: "Two things")
        check(d.comments.map(\.quote) == ["the old loader", "Two things", "the 512 MB cache size"],
              "comments in the text's order: by mail, then place: \(d.comments.map(\.quote))")
        d.uncomment(d.comments[0].id)
        d.uncomment(d.comments[0].id)
        d.note("Can we try 1 GB too?", on: quoted?.id ?? "")
        d.text = "  Thanks, go ahead.\n"

        // what widgets.js posts as the draft takes it, and plain text: the shared cases (sharedCases)
        let agentMail: Set<String> = ["s5", "s7"]
        let comment: [String: Any] = ["message": "s5", "at": 12, "quote": "the 512\u{00A0}\u{00A0}MB\u{200B} cache size"]
        let got = MailComments.comment(comment, of: agentMail)
        check(got.comment?.message == "s5" && got.comment?.at == 12 && got.comment?.quote == "the 512 MB cache size"
              && got.why == nil, "a comment as the script posts it, its quote made plain: \(got.comment?.quote ?? "none")")
        let badComments: [(String, Any?, String)] = [
            ("message", "i6", "the owner's own mail"), ("message", "s5-0", "not a mail"), ("message", "s9", "another thread's mail"),
            ("at", -1, "a place before the start"), ("quote", " \n\u{00A0}\u{200B} ", "a quote of only space"), ("quote", nil, "no quote")]
        for (key, value, what) in badComments {
            var post = comment
            post[key] = value
            let refused = MailComments.comment(post, of: agentMail)
            check(refused.comment == nil && refused.why == "the comments script posted a comment not understood",
                  "a comment not taken: \(what): \(refused.why ?? "taken")")
        }
        // right-to-left characters (a Hebrew geresh, an Arabic digit, a Hebrew letter), whatever the script said:
        // refused
        for quote in ["Set the disk quota to \u{5F3}500\u{5F3} \u{5F3}50\u{5F3} GB", "Quota \u{661}\u{660}",
                      "\u{5DE}\u{5D7}\u{5E7} it"] {
            let refused = MailComments.comment(["message": "s5", "at": 0, "quote": quote], of: agentMail)
            check(refused.comment == nil && refused.why == MailComments.rightToLeftWhy,
                  "right to left: refused: \(quote.debugDescription): \(refused.why ?? "taken")")
        }
        let most = MailComments.comment(["message": "s5", "at": 0, "quote": String(repeating: "x", count: MailComments.maxQuote)
                                                                          + String(repeating: "\u{2800}", count: 3000)],
                                        of: agentMail)
        let long = MailComments.comment(["message": "s5", "at": 0, "quote": String(repeating: "x", count: MailComments.maxQuote + 1)],
                                        of: agentMail)
        check(most.comment?.quote.count == MailComments.maxQuote && long.comment == nil
              && long.why == "select less: at most \(Fmt.count(Double(MailComments.maxQuote))) characters",
              "a quote of at most \(MailComments.maxQuote) characters, made plain: \(long.why ?? "taken")")

        // the text sent: answers (the question and options picked quoted, the agent's words; the owner's answer and
        // comment not), then comments, then the owner's words, a blank line apart
        check(d.reply == """
            > Which cache policy should the loader use?
            > 2. LFU with ageing, 512 MB
            My answer: option 2
            My comment: cap it at 1 GB

            > the 512 MB cache size
            Can we try 1 GB too?

            Thanks, go ahead.
            """, "the reply's text: \(d.reply.debugDescription)")
        // the box, and the thread list's rows, lay the agent's words out left to right, in the order they come in the
        // text, whatever right-to-left letters they start with (a geresh looks like an apostrophe)
        let geresh = "\u{05F3}"
        let reordered = "\(geresh)Delete it.\(geresh) Do not \(geresh)keep the old bucket\(geresh)"
        check(readsInOrder(Fmt.leftToRight(reordered + "\n" + reordered)) && !readsInOrder(reordered),
              "agent text is shown left to right, as sent")
        let row = MailRow.texts(MailboxThread(id: "t", subject: reordered, agents: [reordered, "Delete-bot keep"],
                                              snippet: reordered, last: 0, count: 1, unread: false, box: "inbox",
                                              snoozeUntil: nil, newest: 1, attachments: false))
        check([row.agents, row.subject, row.snippet].allSatisfy(readsInOrder),
              "the thread list shows who wrote, the subject and the snippet left to right: \(row)")

        // the reply box shows each answer and quote whole, as sent: a long one makes it taller, past two lines; past
        // the entries' height a line says there is more to scroll to
        let (longAnswer, shortAnswer) = (boxHeight(longAnswer: true), boxHeight(longAnswer: false))
        let (longQuote, shortQuote) = (boxHeight(longQuote: true), boxHeight(longQuote: false))
        check(longAnswer - shortAnswer > 45, "a long answer is shown whole: \(longAnswer) vs \(shortAnswer)")
        check(longQuote - shortQuote > 45, "a long quote is shown whole: \(longQuote) vs \(shortQuote)")
        // entries taller than their room scroll, and a line under them says so: the box is taller than its text field
        // and that room alone
        let overflowing = Draft(text: "", files: [], key: nil)
        overflowing.addComment(message: "s5", at: 0, quote: String(repeating: "the 512 MB cache size ", count: 80))
        let (over, none) = (boxHeight(overflowing), boxHeight(Draft(text: "", files: [], key: nil)))
        check(over - none > ComposeBox<EmptyView>.maxEntries + 6 + 10 && longQuote - none < ComposeBox<EmptyView>.maxEntries,
              "a line says there is more below, only when there is: \(over) and \(longQuote), over \(none)")
        // and Send (⌘↩) waits until they have been scrolled to their end, again once the agent's words in them change,
        // but not for the owner's own comment
        let held = sendWaits(overflowing, then: { _ in })
        let read = sendWaits(overflowing, then: { scrollEntries($0) })
        let added = sendWaits(overflowing, then: {
            scrollEntries($0)
            overflowing.addComment(message: "s5", at: 900, quote: "and the 1 GB one")
        })
        let noted = sendWaits(overflowing, then: {
            scrollEntries($0)
            overflowing.note("Can we try 1 GB too?", on: overflowing.comments[0].id)
        })
        check(held && !read && added && !noted, "Send waits for the end of what overflows: \([held, read, added, noted])")
        // in fast mode Return alone sends, as Send does, keypad Enter and ⌘↩ too, and nothing while Send waits; ⇧↩ and
        // ⌥↩ make a new line; out of fast mode Return makes a new line
        let plainReturn = pressReturn("Ship it", returnSends: true, [])
        let enter = pressReturn("Ship it", returnSends: true, [], keyCode: 76)
        let command = pressReturn("Ship it", returnSends: true, [.command])
        let shift = pressReturn("Ship it", returnSends: true, [.shift])
        let option = pressReturn("Ship it", returnSends: true, [.option])
        let empty = pressReturn("", returnSends: true, [])
        let notFast = pressReturn("Ship it", returnSends: false, [])
        check(plainReturn == (1, "Ship it") && enter == (1, "Ship it") && command == (1, "Ship it"),
              "fast mode: Return, Enter and ⌘↩ send: \([plainReturn, enter, command])")
        check(shift.sent == 0 && shift.text.contains("\n") && option.sent == 0 && option.text.contains("\n"),
              "fast mode: ⇧↩ and ⌥↩ make a new line: \([shift, option])")
        check(empty == (0, ""), "fast mode: Return in an empty box sends nothing: \(empty)")
        check(notFast.sent == 0 && notFast.text.contains("\n"), "out of fast mode Return makes a new line: \(notFast)")
        // a quote of the most characters a comment takes (MailComments.maxQuote) fits the entries' room whole in the
        // narrowest box: the window at its narrowest less the mail list and the divider beside it (SwiftUI's, 1 point),
        // less the room round the box
        let narrowest = BoardView.minWidth - MailListColumn.width - 1 - 2 * ReplyArea.padding
        let prose = String(repeating: "The loader keeps a 512 MB cache and evicts by LFU with ageing; the numbers below come "
                           + "from the overnight run on the shared box. ", count: 10)
        let fullest = Draft(text: "", files: [], key: nil)
        fullest.addComment(message: "s5", at: 0, quote: String(prose.prefix(MailComments.maxQuote)))
        let (fits, bare) = (boxHeight(fullest, width: narrowest), boxHeight(Draft(text: "", files: [], key: nil), width: narrowest))
        check(fits - bare <= ComposeBox<EmptyView>.maxEntries + 6,
              "a quote of \(MailComments.maxQuote) characters fits the narrowest box whole: \(fits) over \(bare)")

        // sending: the text above goes, and the draft holds still meanwhile; once the desktop has it, the draft is empty
        var sent: [[String: Any]] = []
        var replies: [@MainActor ([String: Any]?, String?) -> Void] = []
        let sendFolder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: sendFolder) }     // this test's own folder
        let sender = MailModel(call: { request, done in
            if (request["action"] as? String) == "mail_reply" { sent.append(request); replies.append(done) }
        }, upload: { _, _ in }, ahead: 0, cacheBytes: 1 << 20, draftsFolder: sendFolder)
        let r = sender.draft(for: "R")
        r.pick(cache, comment: false)
        r.pick(ship, comment: false)
        r.addComment(message: "s12", at: 40, quote: "the 512 MB cache size")
        check(sender.drafts.held == ["R"], "answers alone are a draft, marked in the list")
        sender.send("R")
        check(sent.first?["text"] as? String == r.reply && r.sending, "Send sends the reply's text: \(sent.first?["text"] ?? "none")")
        check(!r.pick(lru, comment: false) && r.picks == [cache, ship], "while it is on its way, an answer is not changed")
        replies.first?(["ok": true, "key": "K1"], nil)
        check(r.blank && !r.sending && !r.shown && !r.frozen, "sent: the draft is empty, its key gone")
        r.text = String(repeating: "long ", count: MailModel.maxReply / 5 + 1)
        sender.send("R")
        check(sent.count == 1 && (r.error ?? "").hasPrefix("Too long to send"), "too long a reply is not sent: \(r.error ?? "none")")
        sender.drafts.discard("R")
        check(r.picks.isEmpty && r.comments.isEmpty && r.blank && !r.shown && r.focus == nil && sender.drafts.held.isEmpty,
              "Discard: no answers, no comments")

        // a send that gets no answer may have reached the desktop, which keeps a key's first reply: the draft holds
        // still, and Send sends it again as it was, under its key
        let f = sender.draft(for: "F")
        f.pick(cache, comment: false)
        f.addComment(message: "s12", at: 40, quote: "the 512 MB cache size")
        sender.send("F")
        replies.last?(nil, "ssh: connection reset")
        check(f.frozen && (f.error ?? "").hasPrefix("Not sent: "), "no answer: the draft is frozen: \(f.error ?? "no error")")
        let firstID = f.comments.first?.id ?? ""
        check(!f.pick(lru, comment: false) && !f.addComment(message: "s12", at: 0, quote: "more"), "no answer or comment taken")
        f.note("cap it", on: firstID)
        f.drop("s12-0")
        f.uncomment(firstID)
        check(f.picks == [cache] && f.comments.map(\.comment) == [""], "no comment written, nothing taken out")
        if case .refused(let why) = sender.drafts.add([.image(Data([1]))], to: "F") {
            check(why == "Not attached. " + Draft.frozenWhy, "no file taken: \(why)")
        } else {
            check(false, "a file was taken into a frozen draft")
        }
        sender.copyToDesktop([URL(fileURLWithPath: "/tmp/film.mov")], for: "F")
        check(f.copying.isEmpty && f.error == "Not copied. " + Draft.frozenWhy, "nothing copied to the desktop for it")
        sender.send("F")
        check(sent.count == 3 && sent[2]["key"] as? String == sent[1]["key"] as? String
              && sent[2]["text"] as? String == sent[1]["text"] as? String, "sent again: the same key and text")
        replies.last?(["ok": true, "key": "K2"], nil)
        check(f.blank && !f.frozen, "then sent: empty, the key gone")
        // agent-mail failing or timing out may come after the mailroom kept it: still frozen, and the box says so
        f.pick(cache, comment: false)
        sender.send("F")
        replies.last?(["ok": false, "error": "timed out after 60s"], nil)
        check(f.frozen && !f.pick(lru, comment: false), "a desktop failure that is not a refusal: frozen")
        sender.send("F")
        check(sent.count == 5 && sent[4]["key"] as? String == sent[3]["key"] as? String, "and sent again under its key")
        // a reply the desktop refused before the mailroom never reached it: the key goes, so the draft can change
        replies.last?(["ok": false, "error": "reply too long or not text", "refused": true], nil)
        check(!f.frozen && f.pick(lru, comment: false), "refused: the draft can change again")
        // Discard forgets a reply that may have gone: the next one has a key of its own
        sender.send("F")
        replies.last?(nil, "no answer")
        sender.drafts.discard("F")
        f.pick(cache, comment: false)
        sender.send("F")
        check(sent.count == 7 && (sent[6]["key"] as? String) != (sent[5]["key"] as? String),
              "after Discard, a new key: \(sent.suffix(2).map { $0["key"] ?? "" })")

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

    /// Whether `text`, laid out as the app's text is (AppKit's text system, its base direction from its first strong
    /// letter), shows each "Delete" to the left of the "keep" that follows it, as they come in it.
    static func readsInOrder(_ text: String) -> Bool {
        let storage = NSTextStorage(string: text, attributes: [.font: NSFont.systemFont(ofSize: 13)])
        let layout = NSLayoutManager(), container = NSTextContainer(size: NSSize(width: 2000, height: 1000))
        layout.addTextContainer(container)
        storage.addLayoutManager(layout)
        layout.ensureLayout(for: container)
        let all = text as NSString
        func lefts(_ word: String) -> [CGFloat] {
            var out: [CGFloat] = [], from = 0
            while true {
                let r = all.range(of: word, range: NSRange(location: from, length: all.length - from))
                if r.location == NSNotFound { return out }
                let glyphs = layout.glyphRange(forCharacterRange: r, actualCharacterRange: nil)
                out.append(layout.boundingRect(forGlyphRange: glyphs, in: container).minX)
                from = r.location + r.length
            }
        }
        let (delete, keep) = (lefts("Delete"), lefts("keep"))
        return !delete.isEmpty && delete.count == keep.count && zip(delete, keep).allSatisfy { $0 < $1 }
    }

    /// The reply box's height, drawn offscreen 400 points wide, holding one answer: a long question and choice, or
    /// short ones.
    static func boxHeight(longAnswer: Bool) -> CGFloat {
        let d = Draft(text: "", files: [], key: nil)
        d.pick(DraftPick(widget: "s5-0", picked: [0],
                         question: longAnswer ? String(repeating: "Which cache policy? ", count: 9) : "Which?",
                         labels: [longAnswer ? String(repeating: "keep the old results ", count: 10) : "Keep"]),
               comment: false)
        return boxHeight(d)
    }

    /// The same, holding one quote, long or short.
    static func boxHeight(longQuote: Bool) -> CGFloat {
        let d = Draft(text: "", files: [], key: nil)
        d.addComment(message: "s5", at: 0, quote: String(repeating: "the 512 MB cache size ", count: longQuote ? 20 : 1))
        return boxHeight(d)
    }

    /// The reply box's height for `d`, `width` points wide, once its layout has settled.
    static func boxHeight(_ d: Draft, width: CGFloat = 400) -> CGFloat {
        withBox(d, width: width) { _, host in host.fittingSize.height }
    }

    /// `look` at the reply box drawn offscreen for `d`, `width` points wide, in its window, once its layout has
    /// settled; `send` is what its Send does, and Return alone too with `returnSends` (fast mode).
    static func withBox<T>(_ d: Draft, width: CGFloat = 400, returnSends: Bool = false, send: @escaping () -> Void = {},
                           _ look: (NSWindow, NSView) -> T) -> T {
        let none = ComposeFiles(take: { _ in false }, pick: {}, remove: { _ in }, offer: { _ in "" }, action: "", instead: { _ in })
        let box = ComposeBox(draft: d, placeholder: "Reply…", height: 110, returnSends: returnSends, files: none, send: send,
                             discard: {}) { EmptyView() }
            .frame(width: width)
        let (win, host) = Renderer.offscreen(box, size: nil, dark: false)
        return withExtendedLifetime(win) {
            settle(host)
            return look(win, host)
        }
    }

    /// Lay `host` out until its height stops changing (the box's entries take their height a pass later).
    static func settle(_ host: NSView) {
        var last: CGFloat = -1
        _ = Renderer.wait(3, step: 0.05) {
            host.layoutSubtreeIfNeeded()
            let h = host.fittingSize.height
            defer { last = h }
            return h == last
        }
    }

    /// Whether ⌘↩ in the box for `d` sends nothing after `then`, which Send would have sent.
    static func sendWaits(_ d: Draft, then: (NSView) -> Void) -> Bool {
        var sent = 0
        return withBox(d, send: { sent += 1 }) { win, host in
            then(host)
            settle(host)
            _ = Renderer.wait(0.3, step: 0.05) { false }
            if let e = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [.command], timestamp: 0,
                                        windowNumber: win.windowNumber, context: nil, characters: "\r",
                                        charactersIgnoringModifiers: "\r", isARepeat: false, keyCode: 36) {
                _ = win.performKeyEquivalent(with: e)
            }
            _ = Renderer.wait(0.3, step: 0.05) { sent > 0 }
            return sent == 0
        }
    }

    /// Return, held with `flags`, in the text of a reply box whose draft holds `text`, routed as AppKit routes a key:
    /// to the window's key equivalents (Send's ⌘↩), then to the text. How often Send's action ran, and the text then.
    static func pressReturn(_ text: String, returnSends: Bool, _ flags: NSEvent.ModifierFlags,
                            keyCode: UInt16 = 36) -> (sent: Int, text: String) {
        let d = Draft(text: text, files: [], key: nil)
        var sent = 0
        return withBox(d, returnSends: returnSends, send: { sent += 1 }) { win, host in
            guard let tv = Renderer.all(FileTakingTextView.self, in: host).first, win.makeFirstResponder(tv),
                  let e = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags, timestamp: 0,
                                           windowNumber: win.windowNumber, context: nil, characters: "\r",
                                           charactersIgnoringModifiers: "\r", isARepeat: false, keyCode: keyCode)
            else { return (-1, "no text field") }
            tv.setSelectedRange(NSRange(location: (tv.string as NSString).length, length: 0))
            if !win.performKeyEquivalent(with: e) { tv.keyDown(with: e) }
            _ = Renderer.wait(0.3, step: 0.05) { sent > 0 }
            return (sent, d.text)
        }
    }

    /// Scroll the box's answers and quotes (its scroll view that is not a text field's) to their end.
    static func scrollEntries(_ view: NSView) {
        for sv in Renderer.all(NSScrollView.self, in: view) where !(sv.documentView is NSTextView) {
            guard let doc = sv.documentView else { continue }
            let end = doc.isFlipped ? max(0, doc.frame.height - sv.contentView.bounds.height) : 0
            sv.contentView.scroll(to: NSPoint(x: 0, y: end))
            sv.reflectScrolledClipView(sv.contentView)
        }
        _ = Renderer.wait(0.3, step: 0.05) { false }
    }

    /// Reading ahead, the kept threads, ← → and paging, with one thread read ahead on each side of the open one.
    static func ahead(_ check: (Bool, String) -> Void) {
        let desk = Desk()
        defer { desk.problems.forEach { check(false, $0) } }
        let folder = MailDrafts.scratchFolder()
        defer { try? FileManager.default.removeItem(at: folder) }          // this test's own folder
        var copied: [URL] = []                                             // nothing is copied to the desktop here
        let mail = desk.model(folder: folder, upload: { file, _ in copied.append(file) }, ahead: 1)
        let a = thread("A", newest: 9, last: 90), b = thread("B", newest: 8, last: 80), c = thread("C", newest: 7, last: 70)
        let d = thread("D", newest: 6, last: 60)

        mail.refresh()
        desk.answer("mail_list", Desk.list([a, b, c, d]))
        check(desk.shows == ["A", "B"], "with none open, the top threads are read ahead: \(desk.shows)")
        mail.open("A")
        check(desk.shows == ["A", "B"] && mail.shown == nil, "opening one already on its way asks nothing more")
        desk.answer("mail_show", thread: "A", Desk.show(a, Desk.messages(["s1", "s2", "s3"]), document: "<p>A</p>"))
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
        desk.answer("mail_show", thread: "B", Desk.show(b, Desk.messages(["s4"]), document: "<p>B</p>"))
        mail.perform(.thread(1))
        check(mail.shown?.thread.id == "B" && mail.current == "s4", "B shows at once")
        check(desk.shows == ["C"], "and C is read ahead (A is kept): \(desk.shows)")

        // C changes in the list while its read-ahead is on its way: ↓ waits for that answer and shows it, then asks
        // once more and shows the newer
        let c2 = thread("C", newest: 10, count: 2, last: 100)
        mail.refresh()
        desk.answer("mail_list", Desk.list([a, b, c2, d]))
        check(desk.shows == ["C"], "the read-ahead does not ask again for a thread on its way: \(desk.shows)")
        mail.perform(.thread(1))
        check(mail.shown?.thread.id == "B" && desk.shows == ["C", "D"], "↓ waits for C; D is read ahead: \(desk.shows)")
        desk.answer("mail_show", thread: "C", Desk.show(c, Desk.messages(["s5"]), document: "<p>C</p>"))
        check(mail.shown?.document == "<p>C</p>" && desk.shows == ["D", "C"], "that answer shows, and C is asked for again: \(desk.shows)")
        desk.answer("mail_show", thread: "C", Desk.show(c2, Desk.messages(["s5", "s10"]), document: "<p>C2</p>"))
        check(mail.shown?.document == "<p>C2</p>" && mail.current == "s5" && desk.shows == ["D"],
              "then the newer, at the mail being read, asked once only: \(desk.shows)")
        desk.answer("mail_show", thread: "D", Desk.show(d, Desk.messages(["s6"]), document: "<p>D</p>"))

        // C changes in the list while open: it is read afresh; a second poll before that answer asks again, and the
        // first answer, coming last, changes nothing
        let c3 = thread("C", newest: 11, count: 3, last: 110)
        mail.refresh()
        desk.answer("mail_list", Desk.list([c3, a, b, d]))
        mail.refresh()
        desk.answer("mail_list", Desk.list([c3, a, b, d]))
        check(desk.shows == ["C", "C"], "each poll that finds it changed asks again: \(desk.shows)")
        desk.answer("mail_show", thread: "C", newest: true, Desk.show(c3, Desk.messages(["s5", "s10", "s11"]), document: "<p>C new</p>"))
        check(mail.shown?.document == "<p>C new</p>" && mail.current == "s5", "the newer answer shows, at the mail being read")
        desk.answer("mail_show", thread: "C", Desk.show(c3, Desk.messages(["s5"]), document: "<p>C old</p>"))
        check(mail.shown?.document == "<p>C new</p>", "the older answer, coming after, is not shown")
        mail.perform(.thread(1))
        mail.perform(.thread(-1))
        check(mail.shown?.document == "<p>C new</p>" && desk.asked.isEmpty, "nor kept: C again shows the newer, asking nothing")

        // a thread the list no longer vouches for is read again when opened
        mail.refresh()
        desk.answer("mail_list", Desk.list([c3, a, thread("B", newest: 12, count: 2, last: 120), d]))
        mail.open("B")
        check(mail.shown?.thread.id == "C" && desk.shows == ["B"], "B changed since it was kept: asked for, not shown from the cache")
        desk.answer("mail_show", thread: "B", Desk.show(thread("B", newest: 12, count: 2, last: 120), Desk.messages(["s4", "s12"]), document: "<p>B2</p>"))
        check(mail.shown?.document == "<p>B2</p>" && desk.asked.isEmpty, "then shown: \(desk.shows)")
        check(copied.isEmpty, "nothing copied to the desktop: \(copied)")
    }
}
