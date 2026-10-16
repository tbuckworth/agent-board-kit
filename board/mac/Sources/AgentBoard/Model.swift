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

    enum CodingKeys: String, CodingKey {
        case generated, host, agents, timeline, summary, mail, usage, metrics, gmail, worktrees, owner, desktop, restart
    }

    /// Lossy where it matters: one odd agent, timeline row, usage or metrics field never blanks the board.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        generated = try c.decode(Double.self, forKey: .generated)
        host = try? c.decodeIfPresent(String.self, forKey: .host)
        agents = c.lossyArray(Agent.self, forKey: .agents)
        timeline = c.lossyArray(TimelineItem.self, forKey: .timeline)
        summary = (try? c.decodeIfPresent([String: Int].self, forKey: .summary)) ?? [:]
        mail = (try? c.decode(MailInfo.self, forKey: .mail)) ?? MailInfo(ok: false, held: [], heldCount: 0, heldOwner: 0)
        usage = try? c.decodeIfPresent(UsageInfo.self, forKey: .usage)
        metrics = try? c.decodeIfPresent(Metrics.self, forKey: .metrics)
        gmail = try? c.decodeIfPresent(GmailStatus.self, forKey: .gmail)
        worktrees = c.lossyArray(FreeWorktree.self, forKey: .worktrees)
        owner = try? c.decodeIfPresent(String.self, forKey: .owner)
        desktop = try? c.decodeIfPresent(DesktopUpdates.self, forKey: .desktop)
        restart = try? c.decodeIfPresent(RestartStatus.self, forKey: .restart)
    }
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
    var subagentTree: Bool?
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

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        metrics = try? c.decodeIfPresent(Metrics.self, forKey: .metrics)
        usage = try? c.decodeIfPresent(UsageInfo.self, forKey: .usage)
    }
}

/// Consumes one array element of any shape, so a lossy decode can skip it.
private struct Skip: Decodable { init(from decoder: Decoder) throws {} }

extension KeyedDecodingContainer {
    func lossyArray<T: Decodable>(_ type: T.Type, forKey key: Key) -> [T] {
        guard var arr = try? nestedUnkeyedContainer(forKey: key) else { return [] }
        var out: [T] = []
        while !arr.isAtEnd {
            if let v = try? arr.decode(T.self) { out.append(v) } else if (try? arr.decode(Skip.self)) == nil { break }
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

    enum CodingKeys: String, CodingKey { case ts, mem, cpu, disks, gpus }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ts = (try? c.decode(Double.self, forKey: .ts)) ?? 0
        mem = try? c.decodeIfPresent(Mem.self, forKey: .mem)
        cpu = try? c.decodeIfPresent(CPU.self, forKey: .cpu)
        disks = c.lossyArray(Disk.self, forKey: .disks)
        gpus = c.lossyArray(GPU.self, forKey: .gpus)
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
    var power: Double?
}

struct UsageRecommendation: Decodable {
    var use: String?
    var line: String?
}

struct UsageInfo: Decodable {
    var source: String
    var accounts: [UsageAccount]
    var observedAt: Double?
    var error: String?
    var recommendation: UsageRecommendation?

    enum CodingKeys: String, CodingKey { case source, accounts, observedAt, error, recommendation }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        source = (try? c.decode(String.self, forKey: .source)) ?? "?"
        accounts = c.lossyArray(UsageAccount.self, forKey: .accounts)
        observedAt = try? c.decodeIfPresent(Double.self, forKey: .observedAt)
        error = try? c.decodeIfPresent(String.self, forKey: .error)
        recommendation = try? c.decodeIfPresent(UsageRecommendation.self, forKey: .recommendation)
    }
}

struct UsageAccount: Decodable, Identifiable {
    var target: String?
    var label: String?
    var plan: String?
    var windows: [UsageWindow]?
    var observedAt: Double?
    var stale: Bool?
    var cached: Bool?
    var error: String?
    var id: String { target ?? label ?? "?" }

    enum CodingKeys: String, CodingKey { case target, label, plan, windows, observedAt, stale, cached, error }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        target = try? c.decodeIfPresent(String.self, forKey: .target)
        label = try? c.decodeIfPresent(String.self, forKey: .label)
        plan = try? c.decodeIfPresent(String.self, forKey: .plan)
        windows = c.lossyArray(UsageWindow.self, forKey: .windows)
        observedAt = try? c.decodeIfPresent(Double.self, forKey: .observedAt)
        stale = try? c.decodeIfPresent(Bool.self, forKey: .stale)
        cached = try? c.decodeIfPresent(Bool.self, forKey: .cached)
        error = try? c.decodeIfPresent(String.self, forKey: .error)
    }
}

struct UsageWindow: Decodable, Identifiable {
    var kind: String?
    var name: String?
    var usedPct: Double?
    var remainingPct: Double?
    var resetsAt: Double?
    var model: String?
    var id: String { (kind ?? "") + (name ?? "") }
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
