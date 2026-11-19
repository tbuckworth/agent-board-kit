import AppKit
import UserNotifications

/// One notification: a thing shown in red on the board, whose `key` stays the same while it stays red, or a meeting
/// about to start (Meetings.swift), whose notification carries the meeting's `link`.
struct RedItem: Equatable {
    var key: String
    var title: String
    var body: String
    var agent: String?
    var link: AlertLink? = nil
}

/// What a click on a notification opens instead of the board: a meeting's link, in the Chrome profile signed in to
/// `account` (BoardModel.openMeeting).
struct AlertLink: Equatable {
    var url: String
    var account: String
}

/// What the board shows in red right now, by the same rules the views colour by: a blocked tile, a stranded reply,
/// a plan window or Codex credits nearly out (UsageLevel), a Claude login signed in to another login's account
/// (SharedLogin), security updates on the desktop, and the link to the desktop down (after `linkGrace`, so a
/// reconnect after sleep is not news).
enum RedRules {
    static let linkGrace: TimeInterval = 300

    static func items(snapshot: Snapshot?, usage: UsageInfo?, link: LinkState, downFor: TimeInterval) -> [RedItem] {
        var out: [RedItem] = []
        for a in snapshot?.agents ?? [] {
            if a.agentState == .blocked {
                out.append(RedItem(key: "blocked:" + a.id, title: "\(a.display) is blocked", body: a.reason, agent: a.id))
            }
            if a.stranded == true {
                out.append(RedItem(key: "stranded:" + a.id, title: "Your reply to \(a.display) is stranded",
                                   body: a.reason, agent: a.id))
            }
        }
        for acct in usage?.accounts ?? [] {
            let who = acct.label ?? acct.target ?? "plan"
            if let shared = acct.sameAccountAs, !shared.isEmpty {
                out.append(RedItem(key: "same-account:\(acct.id)", title: "Two Claude logins share one account",
                                   body: SharedLogin.body(acct, shared, in: usage?.accounts ?? []), agent: nil))
            }
            for w in acct.windows ?? [] where UsageLevel(left: w.left) == .nearlyOut {
                let reset = w.resetsAt.map { " · resets " + Fmt.clock($0) } ?? ""
                out.append(RedItem(key: "usage:\(acct.id):\(w.id)", title: "\(who): \(Int(w.left))% left",
                                   body: "\(w.name ?? w.kind ?? "usage window") is nearly used up\(reset)", agent: nil))
            }
            if let cr = acct.creditsMeter, cr.spending == true, let left = cr.leftPct, UsageLevel(left: left) == .nearlyOut {
                out.append(RedItem(key: "credits:\(acct.id)", title: "\(who): \(Int(left))% of credits left",
                                   body: "Codex credits are nearly used up", agent: nil))
            }
        }
        if let s = snapshot?.desktop?.security, s > 0 {
            out.append(RedItem(key: "security", title: "Desktop: \(s) security update\(s == 1 ? "" : "s")",
                               body: snapshot?.desktop?.summary ?? "", agent: nil))
        }
        if link != .live && downFor >= linkGrace {
            var why = ""
            if case .down(let w) = link { why = w }
            out.append(RedItem(key: "link", title: "Agent Board can't reach \(Settings.host)",
                               body: "No data for \(Fmt.age(downFor))" + (why.isEmpty ? "" : ": " + why), agent: nil))
        }
        return out
    }
}

/// What to say about a Claude login signed in to the same account as an earlier one (mark_shared_logins).
enum SharedLogin {
    /// The names of the logins with these targets (UsageAccount.shortLabel; the target when the reading lacks it).
    static func names(_ shared: [String], in accounts: [UsageAccount]) -> String {
        shared.map { t in accounts.first { $0.target == t }?.shortLabel ?? t }.joined(separator: ", ")
    }

    static func body(_ acct: UsageAccount, _ shared: [String], in accounts: [UsageAccount]) -> String {
        "\(acct.shortLabel) is signed in to the same account as \(names(shared, in: accounts)), " +
            "so agents on either spend one allowance. Sign it in again, choosing the account it is for: on the desktop, run " +
            "CLAUDE_CONFIG_DIR=\(acct.loginDir ?? "<its folder>") claude and type /login."
    }
}

/// A macOS notification when anything on the board turns red. Each red thing notifies once; it notifies again only
/// after it has stopped being red. What has been notified is saved, so a relaunch doesn't repeat it.
@MainActor
final class Alerts: NSObject, UNUserNotificationCenterDelegate {
    nonisolated static let savedKey = "redNotified"
    nonisolated static let groupOver = 3                          // more new reds than this at once: one notification for all

    /// Open the board, selecting this agent (from a click on the notification).
    var onOpen: ((String?) -> Void)?
    /// Open a notification's link (from a click on a meeting's notification).
    var onLink: ((AlertLink) -> Void)?
    private(set) var red: Set<String>
    private let save: ([String]) -> Void
    private let post: ([RedItem]) -> Void

    init(saved: [String] = UserDefaults.standard.stringArray(forKey: Alerts.savedKey) ?? [],
         save: (([String]) -> Void)? = nil, post: (([RedItem]) -> Void)? = nil) {
        red = Set(saved)
        self.save = save ?? { UserDefaults.standard.set($0, forKey: Alerts.savedKey) }
        self.post = post ?? Alerts.notify
        super.init()
    }

    /// Ask once for permission (macOS shows its prompt the first time) and take clicks on the notifications.
    func start() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted, !UserDefaults.standard.bool(forKey: "redNotifyWelcomed") else { return }
            UserDefaults.standard.set(true, forKey: "redNotifyWelcomed")
            DispatchQueue.main.async {
                Alerts.notify([RedItem(key: "welcome", title: "Agent Board notifications are on",
                                       body: "You'll get one whenever something on the board turns red.", agent: nil)])
            }
        }
    }

    /// The reds now. Returns what was newly red (and so notified).
    @discardableResult
    func update(_ items: [RedItem]) -> [RedItem] {
        let keys = Set(items.map { $0.key })
        let fresh = items.filter { !red.contains($0.key) }
        if keys != red {
            red = keys
            save(keys.sorted())
        }
        if !fresh.isEmpty { post(fresh) }
        return fresh
    }

    nonisolated static func notify(_ items: [RedItem]) {
        let center = UNUserNotificationCenter.current()
        let shown: [RedItem] = items.count > groupOver ? [RedItem(
            key: "group", title: "\(items.count) things turned red on the board",
            body: items.prefix(4).map { $0.title }.joined(separator: " · ") + (items.count > 4 ? " · …" : ""),
            agent: nil)] : items
        center.getNotificationSettings { settings in
            let allowed = settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional
            DispatchQueue.main.async {
                for item in shown {
                    if allowed {
                        let c = UNMutableNotificationContent()
                        c.title = item.title
                        c.body = String(item.body.prefix(240))
                        c.sound = .default
                        var info: [String: String] = [:]
                        if let id = item.agent { info["agent"] = id }
                        if let link = item.link { info["link"] = link.url; info["account"] = link.account }
                        c.userInfo = info
                        center.add(UNNotificationRequest(identifier: "red." + item.key, content: c, trigger: nil))
                    } else {
                        fallback(item)
                    }
                }
            }
        }
    }

    /// Without notification permission: Notification Center through osascript, text passed as arguments (never
    /// put into the script).
    nonisolated private static func fallback(_ item: RedItem) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title \"Agent Board\" subtitle (item 1 of argv)",
                       "-e", "end run", item.title, String(item.body.prefix(240))]
        try? p.run()
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        let agent = info["agent"] as? String
        var link: AlertLink?
        if let url = info["link"] as? String, let account = info["account"] as? String { link = AlertLink(url: url, account: account) }
        DispatchQueue.main.async { [weak self] in
            MainActor.assumeIsolated {
                if let link = link, let onLink = self?.onLink { onLink(link) } else { self?.onOpen?(agent) }
            }
        }
        completionHandler()
    }
}

/// --selftest-alerts SNAPSHOT.json: the red rules and the once-per-red notifying, offline. Nothing is posted.
@MainActor
enum AlertsTest {
    static func run(snapshot path: String) -> Int32 {
        guard let data = FileManager.default.contents(atPath: path),
              var snap = try? JSON.decoder.decode(Snapshot.self, from: data), snap.agents.count >= 5 else {
            print("need a snapshot with at least 5 agents")
            return 1
        }
        var fails = 0
        func check(_ ok: Bool, _ what: String) { print((ok ? "ok    " : "FAIL  ") + what); if !ok { fails += 1 } }
        for i in snap.agents.indices { snap.agents[i].state = "idle"; snap.agents[i].stranded = false }
        snap.desktop = nil
        var posted: [[RedItem]] = []
        var saved: [String] = []
        let alerts = Alerts(saved: [], save: { saved = $0 }, post: { posted.append($0) })
        let items = { RedRules.items(snapshot: snap, usage: nil, link: .live, downFor: 0) }
        alerts.update(items())
        check(posted.isEmpty, "nothing red: nothing posted")
        snap.agents[0].state = "blocked"
        snap.agents[0].reason = "hit a plan limit"
        alerts.update(items())
        check(posted.count == 1 && posted[0].map { $0.key } == ["blocked:" + snap.agents[0].id], "a tile turns red: one notification")
        check(posted.last?.first?.agent == snap.agents[0].id && posted.last?.first?.body == "hit a plan limit", "it names the agent and why")
        alerts.update(items())
        check(posted.count == 1, "still red on the next snapshot: not again")
        let again = Alerts(saved: saved, save: { _ in }, post: { posted.append($0) })
        again.update(items())
        check(posted.count == 1, "after a relaunch: not again")
        snap.agents[0].state = "idle"
        alerts.update(items())
        snap.agents[0].state = "blocked"
        alerts.update(items())
        check(posted.count == 2, "red, then not, then red again: notified again")
        snap.agents[1].stranded = true
        alerts.update(items())
        check(posted.count == 3 && posted.last?.first?.key == "stranded:" + snap.agents[1].id, "a stranded reply")
        for i in 2..<6 where i < snap.agents.count { snap.agents[i].state = "blocked" }
        let fresh = alerts.update(items())
        check(fresh.count >= 3 && posted.count == 4, "several at once: one call (\(fresh.count) items, grouped when over \(Alerts.groupOver))")
        let reading = """
        {"source": "agent-usage", "accounts": [{"target": "claude:default", "label": "Default Claude (Max plan)"},
         {"target": "claude:work", "label": "Work seat", "same_account_as": ["claude:default"],
          "login_dir": "~/.claude-work"}]}
        """
        let usage = try? JSON.decoder.decode(UsageInfo.self, from: Data(reading.utf8))
        let shared = RedRules.items(snapshot: nil, usage: usage, link: .live, downFor: 0)
        check(shared.map { $0.key } == ["same-account:claude:work"], "a login on another login's account is red")
        check(shared.first?.body.contains("same account as Default Claude") == true &&
              shared.first?.body.contains("CLAUDE_CONFIG_DIR=~/.claude-work claude") == true,
              "it names the other login and gives the command")
        let downShort = RedRules.items(snapshot: nil, usage: nil, link: .down("no route"), downFor: 60)
        let downLong = RedRules.items(snapshot: nil, usage: nil, link: .down("no route"), downFor: 400)
        check(downShort.isEmpty && downLong.map { $0.key } == ["link"], "the link: red only after 5 minutes down")
        print(fails == 0 ? "alerts OK" : "alerts: \(fails) FAILED")
        return fails == 0 ? 0 : 1
    }
}
