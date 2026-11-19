import Foundation

// The desktop collector's JSON (bin/agent-board). Snake case is converted on decode; anything the app
// can live without is optional so an older or newer collector never breaks the board.

struct Envelope: Decodable { let type: String }

struct Snapshot: Decodable {
    var generated: Double
    var host: String?
    var agents: [Agent]
    var timeline: [TimelineItem]
    var summary: [String: Int]
    var mail: MailInfo
    var usage: UsageInfo?
    var metrics: Metrics?
    var gmail: GmailStatus?
    var worktrees: [FreeWorktree]
    var owner: String?
    var desktop: DesktopUpdates?
    var restart: RestartStatus?
    var sent: SentCounts?
    var chats: ChatsInfo?
    var meetings: MeetingsInfo?
    var mailbox: MailboxSummary?
    /// What failed this poll: the board sources the desktop lists (collector: Collector.problems), then the parts of
    /// the snapshot this app could not decode. The top bar's ProblemsChip lists them.
    var problems: [Problem]

    enum CodingKeys: String, CodingKey {
        case generated, host, agents, timeline, summary, mail, usage, metrics, gmail, worktrees, owner, desktop, restart, sent, chats,
             meetings, mailbox, problems
    }

    /// Lossy where it matters: one odd agent, timeline row, usage or metrics field never blanks the board, and what
    /// was left out is added to `problems` (--selftest-decode checks this).
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var failed: [Problem] = []
        func note(_ why: [String], _ noun: String) {
            if let first = why.first {
                failed.append(Problem(source: "\(why.count) \(noun)\(why.count == 1 ? "" : "s") not understood", error: first))
            }
        }
        func part<T: Decodable>(_ type: T.Type, _ key: CodingKeys) -> T? {
            var why: [String] = []
            let out = c.lossyValue(T.self, forKey: key, failures: &why)
            if let first = why.first { failed.append(Problem(source: key.stringValue + " not understood", error: first)) }
            return out
        }
        func rows<T: Decodable>(_ type: T.Type, _ key: CodingKeys, _ noun: String) -> [T] {
            var why: [String] = []
            let out = c.lossyArray(T.self, forKey: key, failures: &why)
            note(why, noun)
            return out
        }
        generated = try c.decode(Double.self, forKey: .generated)
        host = part(String.self, .host)
        agents = rows(Agent.self, .agents, "agent")
        timeline = rows(TimelineItem.self, .timeline, "timeline row")
        summary = part([String: Int].self, .summary) ?? [:]
        mail = part(MailInfo.self, .mail) ?? MailInfo(ok: false, held: [], heldCount: 0, heldOwner: 0)
        let u = part(UsageInfo.self, .usage)
        note(u?.dropped ?? [], "usage field")
        usage = u
        let m = part(Metrics.self, .metrics)
        note(m?.dropped ?? [], "metrics field")
        metrics = m
        gmail = part(GmailStatus.self, .gmail)
        worktrees = rows(FreeWorktree.self, .worktrees, "worktree")
        owner = part(String.self, .owner)
        desktop = part(DesktopUpdates.self, .desktop)
        restart = part(RestartStatus.self, .restart)
        sent = part(SentCounts.self, .sent)
        let ch = part(ChatsInfo.self, .chats)
        note(ch?.dropped ?? [], "group chat")
        chats = ch
        let mt = part(MeetingsInfo.self, .meetings)
        note(mt?.dropped ?? [], "meeting")
        meetings = mt
        mailbox = part(MailboxSummary.self, .mailbox)
        problems = rows(Problem.self, .problems, "problem") + failed
    }
}

/// The meetings that start within the desktop's look-ahead (agentboard/meetings.py), and how long before a start
/// each alert comes (Meetings.swift). The desktop sends the section only when the owner has given it a Google login
/// with calendar access; without one there is no section and no meeting alerts.
struct MeetingsInfo: Decodable, Equatable {
    var notifyMinutes: Double
    var fullscreenMinutes: Double
    var events: [Meeting]
    var dropped: [String]         // why meetings that do not decode were left out; not in the JSON

    enum CodingKeys: String, CodingKey { case notifyMinutes, fullscreenMinutes, events }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        notifyMinutes = try c.decode(Double.self, forKey: .notifyMinutes)
        fullscreenMinutes = try c.decode(Double.self, forKey: .fullscreenMinutes)
        var why: [String] = []
        events = c.lossyArray(Meeting.self, forKey: .events, failures: &why)
        dropped = why
    }
}

/// One meeting on one of the owner's calendars. A calendar the owner's login sees as free/busy only gives no title
/// and no join link. Fields the desktop adds beyond these are ignored.
struct Meeting: Decodable, Equatable {
    var calendar: String          // the calendar's label
    var id: String
    var title: String?
    var start: Double
    var end: Double
    var joinUrl: String?
    var openUrl: String           // the calendar's day view, in `account`
    var account: String           // the Google account to open it in (Browser.open)

    /// The title, or "<calendar> meeting" when there is none.
    var name: String { title ?? calendar + " meeting" }
    /// This meeting at this start: a meeting moved to another time is another.
    var key: String { "\(id)|\(Int(start))" }
    /// The overlay's main button: Join when there is a video link, else the calendar.
    var primary: (label: String, url: String) { joinUrl.map { ("Join", $0) } ?? ("Open calendar", openUrl) }
}

/// Something that failed: a board source on the desktop, or a part of its snapshot this app could not decode.
struct Problem: Decodable, Hashable {
    var source: String
    var error: String
}

/// A decoding failure in a few words: where in the JSON, and what was wrong ("agents.Index 3.state: Expected to
/// decode String but found a number instead.").
func describeDecodeError(_ error: Error) -> String {
    guard let e = error as? DecodingError else { return error.localizedDescription }
    let context: DecodingError.Context
    switch e {
    case .typeMismatch(_, let c), .valueNotFound(_, let c), .keyNotFound(_, let c), .dataCorrupted(let c):
        context = c
    @unknown default:
        return String(describing: e)
    }
    let path = context.codingPath.map { $0.stringValue }.joined(separator: ".")
    return path.isEmpty ? context.debugDescription : path + ": " + context.debugDescription
}

/// A linked git worktree with no agent in it (the board's bottom section).
struct FreeWorktree: Decodable, Identifiable, Equatable {
    var path: String
    var name: String
    var repo: String
    var branch: String
    var locked: Bool?
    var git: GitStage?
    var lastCommit: Commit?
    var tmux: String?
    var id: String { "wt:" + path }
    var repoBranch: String { branch.isEmpty ? "\(repo) (detached)" : "\(repo)@\(branch)" }
}

/// Whether the optional read/unread check against the owner's Gmail is working.
struct GmailStatus: Decodable {
    var ok: Bool?
    var checkedAt: Double?
    var error: String?
}

struct MetricsMessage: Decodable {
    var metrics: Metrics?
    var usage: UsageInfo?

    enum CodingKeys: String, CodingKey { case metrics, usage }

    /// Decode failures are not reported from here: a snapshot carries the same readings at least every 30 s, and its
    /// problems name what did not decode.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        metrics = try? c.decodeIfPresent(Metrics.self, forKey: .metrics)
        usage = try? c.decodeIfPresent(UsageInfo.self, forKey: .usage)
    }
}

/// Consumes one array element of any shape, so a lossy decode can skip it.
private struct Skip: Decodable { init(from decoder: Decoder) throws {} }

extension KeyedDecodingContainer {
    /// The value at `key`: nil when it is missing or null, and nil with why added to `failures` (describeDecodeError)
    /// when it does not decode.
    func lossyValue<T: Decodable>(_ type: T.Type, forKey key: Key, failures: inout [String]) -> T? {
        do {
            return try decodeIfPresent(T.self, forKey: key)
        } catch {
            failures.append(describeDecodeError(error))
            return nil
        }
    }

    /// The array at `key`, decoded element by element: an element that does not decode is left out and why is
    /// added to `failures` (describeDecodeError). A missing key or null is an empty array; a value that is not an
    /// array is an empty array and one failure.
    func lossyArray<T: Decodable>(_ type: T.Type, forKey key: Key, failures: inout [String]) -> [T] {
        guard contains(key), (try? decodeNil(forKey: key)) != true else { return [] }   // decodeNil throws only for a missing key
        var arr: UnkeyedDecodingContainer
        do {
            arr = try nestedUnkeyedContainer(forKey: key)
        } catch {
            failures.append(describeDecodeError(error))
            return []
        }
        var out: [T] = []
        while !arr.isAtEnd {
            do {
                out.append(try arr.decode(T.self))
            } catch {
                failures.append(describeDecodeError(error))
                if (try? arr.decode(Skip.self)) == nil { break }
            }
        }
        return out
    }
}

struct ErrorMessage: Decodable { var error: String }

struct Agent: Decodable, Identifiable, Equatable {
    var id: String
    var cli: String
    var name: String
    var display: String
    var title: String?
    var cwd: String
    var live: Bool
    var rawStatus: String
    var kind: String
    var waitingFor: String?
    var tmux: String?
    var model: String?
    var startedAt: Double?
    var lastActivity: Double?
    var lastTurnEnd: Double?
    var turns24h: Int?
    var notes: [Note]
    var events: [LogEvent]
    var lastNote: Note?
    var mail: AgentMail
    var repo: String
    var branch: String
    var dirty: Int?
    var lastCommit: Commit?
    var state: String
    var reason: String
    var fresh: Double?
    var closed: Bool?
    var stranded: Bool?
    var bg: Bool?
    var git: GitStage?
    var subagents: [Subagent]?
    var leftBehind: LeftBehind?
    var account: String?
    var accountDefault: Bool?
    var configDir: String?
    /// Set when it runs on a later login while the first one has room for its model (collector: classify.login_note).
    var loginNote: String?

    var agentState: AgentState { AgentState(rawValue: state) ?? (live ? .idle : .offline) }   // a newer collector's state
    var repoBranch: String {
        if repo.isEmpty { return (cwd as NSString).lastPathComponent }
        return branch.isEmpty || branch == "HEAD" ? repo : "\(repo)@\(branch)"
    }
}

/// A Codex subagent running inside its parent's process. Older collectors sent just the nickname.
struct Subagent: Decodable, Equatable, Hashable {
    var id: String = ""
    var nickname: String
    var path: String = ""
    var busy: Bool = false
    var lastActivity: Double = 0

    enum CodingKeys: String, CodingKey { case id, nickname, path, busy, lastActivity }

    init(from decoder: Decoder) throws {
        if let name = try? decoder.singleValueContainer().decode(String.self) {
            nickname = name
            return
        }
        let c = try decoder.container(keyedBy: CodingKeys.self)
        nickname = (try? c.decode(String.self, forKey: .nickname)) ?? "?"
        id = (try? c.decode(String.self, forKey: .id)) ?? ""
        path = (try? c.decode(String.self, forKey: .path)) ?? ""
        busy = (try? c.decode(Bool.self, forKey: .busy)) ?? false
        lastActivity = (try? c.decode(Double.self, forKey: .lastActivity)) ?? 0
    }
}

/// What an offline agent left: its directory (usually a worktree) and maybe an empty tmux session.
struct LeftBehind: Decodable, Equatable {
    var cwdExists: Bool?
    var tmux: String?
}

/// Where the agent's work is on its way to main (collector: classify.git_stage).
struct GitStage: Decodable, Equatable {
    var stage: String
    var chips: [String]
    var summary: String
    var pr: PullRequest?
    var upstream: String?
    var ahead: Int?
    var behind: Int?
    var untracked: Int?
    var `default`: String?
    var notInDefault: Int?
    var linked: Bool?
}

struct PullRequest: Decodable, Equatable {
    var number: Int?
    var state: String?
    var url: String?
    var isDraft: Bool?
    var title: String?
}

struct Note: Decodable, Equatable, Hashable {
    var ts: Double
    var text: String
}

struct LogEvent: Decodable, Equatable, Hashable {
    var ts: Double
    var event: String
    var text: String?
}

struct Commit: Decodable, Equatable {
    var hash: String?
    var subject: String?
    var ts: Double?
}

struct AgentMail: Decodable, Equatable {
    var lastOut: Double?
    var lastOutSubject: String?
    var lastIn: Double?
    var unanswered: Bool
    var read: Bool?
    var lastOutId: String?
    var gmailThread: String?
    /// The Mail tab's thread of its latest mail to the owner; "" when it has none (collector: classify.mail_state).
    var lastOutThread: String?
    var held: Int?
    var threads: [MailThread]?
}

struct MailThread: Decodable, Equatable, Hashable {
    var subject: String
    var lastOut: Double
    var lastIn: Double
    var unanswered: Bool?
}

struct TimelineItem: Decodable, Hashable {
    var ts: Double
    var event: String
    var text: String?
    var id: String
    var name: String
}

struct MailInfo: Decodable {
    var ok: Bool?
    var held: [HeldMail]
    var heldCount: Int
    var heldOwner: Int?
}

struct HeldMail: Decodable, Identifiable, Hashable {
    var id: Int
    var ts: Double
    var from: String
    var subject: String
    var reason: String
    var owner: Bool
}

struct Metrics: Decodable {
    var ts: Double
    var mem: Mem?
    var cpu: CPU?
    var disks: [Disk]
    var gpus: [GPU]
    var dropped: [String]         // why parts of the reading did not decode (Snapshot.problems); not in the JSON

    enum CodingKeys: String, CodingKey { case ts, mem, cpu, disks, gpus }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var why: [String] = []
        ts = c.lossyValue(Double.self, forKey: .ts, failures: &why) ?? 0
        mem = c.lossyValue(Mem.self, forKey: .mem, failures: &why)
        cpu = c.lossyValue(CPU.self, forKey: .cpu, failures: &why)
        disks = c.lossyArray(Disk.self, forKey: .disks, failures: &why)
        gpus = c.lossyArray(GPU.self, forKey: .gpus, failures: &why)
        dropped = why
    }
}

struct Mem: Decodable {
    var total: Double
    var available: Double
    var used: Double
    var swapTotal: Double?
    var swapUsed: Double?
}

struct CPU: Decodable {
    var pct: Double?
    var load1: Double?
    var ncpu: Int?
}

struct Disk: Decodable, Identifiable {
    var device: String
    var mount: String
    var kind: String
    var model: String
    var label: String
    var total: Double
    var free: Double
    var used: Double
    var id: String { mount }
}

struct GPU: Decodable {
    var name: String
    var memUsed: Double?
    var memTotal: Double?
    var util: Double?
    var temp: Double?
}

struct UsageRecommendation: Decodable, Equatable {
    var use: String?
    var line: String?
}

struct UsageInfo: Decodable, Equatable {
    var source: String
    var accounts: [UsageAccount]
    var observedAt: Double?
    var error: String?
    var recommendation: UsageRecommendation?
    var dropped: [String]         // why parts of the reading, its accounts' included, did not decode; not in the JSON

    enum CodingKeys: String, CodingKey { case source, accounts, observedAt, error, recommendation }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var why: [String] = []
        source = c.lossyValue(String.self, forKey: .source, failures: &why) ?? "?"
        let read = c.lossyArray(UsageAccount.self, forKey: .accounts, failures: &why)
        accounts = read
        observedAt = c.lossyValue(Double.self, forKey: .observedAt, failures: &why)
        error = c.lossyValue(String.self, forKey: .error, failures: &why)
        recommendation = c.lossyValue(UsageRecommendation.self, forKey: .recommendation, failures: &why)
        dropped = why + read.flatMap { $0.dropped }
    }
}

struct UsageAccount: Decodable, Identifiable, Equatable {
    var target: String?
    var label: String?
    var plan: String?
    var windows: [UsageWindow]?
    var observedAt: Double?
    var stale: Bool?
    var cached: Bool?
    var error: String?
    var creditsMeter: CreditsMeter?
    /// The targets of the Claude logins before this one that are signed in to the same account (one allowance).
    var sameAccountAs: [String]?
    var loginDir: String?          // that login's folder, with ~ for the desktop's home folder
    var dropped: [String]         // why parts of the account, a window or the credits bar, did not decode; not in the JSON
    var id: String { target ?? label ?? "?" }
    /// The label without the part in brackets ("Default Claude (Max plan; the default login, ~/.claude)" gives
    /// "Default Claude"), else the target.
    var shortLabel: String {
        guard let label = label else { return target ?? "?" }
        guard let cut = label.firstIndex(of: "(") else { return label }
        return String(label[..<cut]).trimmingCharacters(in: .whitespaces)
    }

    enum CodingKeys: String, CodingKey { case target, label, plan, windows, observedAt, stale, cached, error, creditsMeter, sameAccountAs, loginDir }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var why: [String] = []
        target = c.lossyValue(String.self, forKey: .target, failures: &why)
        label = c.lossyValue(String.self, forKey: .label, failures: &why)
        plan = c.lossyValue(String.self, forKey: .plan, failures: &why)
        windows = c.lossyArray(UsageWindow.self, forKey: .windows, failures: &why)
        observedAt = c.lossyValue(Double.self, forKey: .observedAt, failures: &why)
        stale = c.lossyValue(Bool.self, forKey: .stale, failures: &why)
        cached = c.lossyValue(Bool.self, forKey: .cached, failures: &why)
        error = c.lossyValue(String.self, forKey: .error, failures: &why)
        creditsMeter = c.lossyValue(CreditsMeter.self, forKey: .creditsMeter, failures: &why)
        sameAccountAs = c.lossyValue([String].self, forKey: .sameAccountAs, failures: &why)
        loginDir = c.lossyValue(String.self, forKey: .loginDir, failures: &why)
        dropped = why
    }
}

/// Codex credits (agentboard/credits.py): the balance, and once the weekly window is used up, how much is left of
/// the balance it ran out at (full), since when, and when the week resets.
struct CreditsMeter: Decodable, Equatable {
    var balance: Double?
    var spending: Bool?
    var full: Double?
    var leftPct: Double?
    var since: Double?
    var resetsAt: Double?
}

struct UsageWindow: Decodable, Identifiable, Equatable {
    var kind: String?
    var name: String?
    var usedPct: Double?
    var remainingPct: Double?
    var resetsAt: Double?
    var model: String?
    /// The desktop's name for the window (sources.mark_window_keys), which the usage_split action takes.
    var key: String?
    var id: String { (kind ?? "") + (name ?? "") }
    /// Percent of the window left: the reading's own figure, else 100 minus the percent used.
    var left: Double { remainingPct ?? (100 - (usedPct ?? 0)) }
}

enum JSON {
    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()
}

// MARK: agent-switch --options --json (the mailroom's switch backend)

struct SwitchOptions: Decodable {
    var agent: SwitchAgent
    var willInterrupt: Bool?
    var targets: [SwitchTarget]
    var models: [String: [SwitchModel]]
    var defaults: [String: SwitchDefault]?
}

struct SwitchAgent: Decodable {
    var name: String?
    var cli: String
    var account: String?
    var model: String?
    var effort: String?
    var busy: Bool?
    var movable: Bool?
    var whyNot: String?
    var backgroundJobs: Int?
}

struct SwitchTarget: Decodable, Identifiable, Hashable {
    var target: String
    var label: String
    var cli: String
    var account: String?
    var current: Bool?
    var kind: String?
    var status: String?
    var usage: String?
    var available: Bool?
    var id: String { target }
}

struct SwitchModel: Decodable, Identifiable, Hashable {
    var value: String
    var label: String
    var efforts: [String]?
    var id: String { value }
}

struct SwitchDefault: Decodable {
    var model: String?
    var effort: String?
}

/// --selftest-decode: the snapshot's problems (the desktop's, then what this app could not decode) and how an
/// action's answer is read (Actions.decodeAnswer), offline.
enum DecodeTest {
    static func run() -> Int32 {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append(what) } }

        // a snapshot with an agent row, a usage window, a disk and an optional part that do not decode
        let agent = #"{"id":"a1","cli":"claude","name":"n","display":"alpha","cwd":"/w","live":true,"raw_status":"idle","kind":"interactive","notes":[],"events":[],"mail":{"unanswered":false},"repo":"r","branch":"main","state":"idle","reason":"idle"}"#
        let line = [
            #"{"generated":1800000000,"agents":["#, agent, #",{"id":7}],"timeline":[],"mail":{"held":[],"held_count":0},"#,
            #""usage":{"source":"agent-usage","accounts":[{"target":"claude:default","windows":[{"kind":"session","remaining_pct":"lots"}]}]},"#,
            #""metrics":{"ts":1800000000,"disks":[{"device":"/dev/sda1"}],"gpus":[]},"#,
            #""chats":{"chats":[{"id":"q8F3Ab_-q8F3Ab_-q8F3Ab","title":"Launch","posts":2,"last_post":5},{"id":7}]},"#,
            #""meetings":{"notify_minutes":10,"fullscreen_minutes":1,"events":[{"calendar":"Work","id":"m1","title":null,"#,
            #""start":1800000600,"end":1800002400,"join_url":null,"open_url":"https://calendar.google.com/","#,
            #""account":"owner@example.com","location":"Room 1"},"#,
            #"{"calendar":"Work","id":"m2"}]},"mailbox":{"unread":"three","inbox":12},"#,
            #""gmail":"broken","problems":[{"source":"worktrees","error":"RuntimeError: git hung"}]}"#,
        ].joined()
        do {
            let snap = try JSON.decoder.decode(Snapshot.self, from: Data(line.utf8))
            let sources = snap.problems.map { $0.source }
            check(sources == ["worktrees", "1 agent not understood", "1 usage field not understood",
                              "1 metrics field not understood", "gmail not understood", "1 group chat not understood",
                              "1 meeting not understood", "mailbox not understood"],
                  "the desktop's problems, then what did not decode: \(sources)")
            let errors = snap.problems.map { $0.error }
            check(errors.first == "RuntimeError: git hung", "the desktop's own error is kept: \(errors)")
            check(zip(errors.dropFirst(), ["agents.", "usage.", "metrics.", "gmail", "chats.", "meetings.", "mailbox."]).allSatisfy { pair in pair.0.hasPrefix(pair.1) },
                  "each decode problem says where: \(errors)")
            check(snap.agents.map { $0.id } == ["a1"], "the agent that decodes is kept")
            check(snap.chats?.chats.map { $0.title } == ["Launch"], "the chat that decodes is kept")
            check(snap.meetings?.events.map { $0.id } == ["m1"] && snap.meetings?.notifyMinutes == 10,
                  "the meeting that decodes is kept, with the lead times")
            check(snap.meetings?.events.first?.account == "owner@example.com", "a field the app does not use is ignored")
            check(snap.usage?.accounts.count == 1 && snap.usage?.accounts.first?.windows?.isEmpty == true,
                  "the usage account is kept, without the window that does not decode")
        } catch {
            fails.append("the snapshot did not decode at all: \(describeDecodeError(error))")
        }
        let clean = #"{"generated":1800000000,"agents":[],"timeline":[],"mail":{"held":[],"held_count":0},"mailbox":{"unread":3,"inbox":12}}"#
        do {
            let snap = try JSON.decoder.decode(Snapshot.self, from: Data(clean.utf8))
            check(snap.problems.isEmpty, "a snapshot with nothing wrong has no problems: \(snap.problems)")
            check(snap.mailbox == MailboxSummary(unread: 3, inbox: 12), "the mailbox's counts: \(String(describing: snap.mailbox))")
        } catch {
            fails.append("a clean snapshot did not decode: \(describeDecodeError(error))")
        }

        // an action's answer: the value under a key or the whole answer, the desktop's error, nothing, or a bad payload
        let wanted = Problem(source: "s", error: "e")
        let underKey: [String: Any] = ["ok": true, "it": ["source": "s", "error": "e"]]
        let whole: [String: Any] = ["ok": true, "source": "s", "error": "e"]
        let refused: [String: Any] = ["ok": false, "error": "no session abc"]
        let noKey: [String: Any] = ["ok": true]
        let garbled: [String: Any] = ["ok": true, "it": ["source": 3]]
        let a1 = Actions.decodeAnswer(underKey, nil, key: "it", as: Problem.self)
        check(a1.0 == wanted && a1.1 == nil, "an ok answer: the value under the key")
        let a2 = Actions.decodeAnswer(whole, nil, key: nil, as: Problem.self)
        check(a2.0 == wanted && a2.1 == nil, "no key: the whole answer")
        let a3 = Actions.decodeAnswer(refused, nil, key: "it", as: Problem.self)
        check(a3.0 == nil && a3.1 == "no session abc", "ok false: the desktop's error (\(a3.1 ?? "none"))")
        let a4 = Actions.decodeAnswer(nil, "could not start ssh", key: "it", as: Problem.self)
        check(a4.0 == nil && a4.1 == "could not start ssh", "no answer: the transport's error (\(a4.1 ?? "none"))")
        let a5 = Actions.decodeAnswer(noKey, nil, key: "it", as: Problem.self)
        check(a5.0 == nil && a5.1 == "the desktop's answer has no it", "ok, but the key is missing (\(a5.1 ?? "none"))")
        let a6 = Actions.decodeAnswer(garbled, nil, key: "it", as: Problem.self)
        check(a6.0 == nil && (a6.1 ?? "").hasPrefix("not understood: "), "a payload that does not decode (\(a6.1 ?? "none"))")

        print(fails.isEmpty ? "decode OK" : "decode FAILED: " + fails.joined(separator: "; "))
        return fails.isEmpty ? 0 : 1
    }
}

/// Any JSON value, kept as it came (a widget's spec on its way to widgets.js, MailWidgets.swift). Decode it with a
/// plain JSONDecoder: JSON.decoder's snake_case conversion would change an object's keys.
enum JSONValue: Codable, Equatable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let b): try c.encode(b)
        case .number(let n): try c.encode(n)
        case .string(let s): try c.encode(s)
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }
}
