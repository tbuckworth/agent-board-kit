import AppKit
import UserNotifications

/// One of the mailroom's security alerts that it kept for the Mail tab rather than sent through Gmail (the desktop's
/// security-alerts.jsonl, which the collector passes on as the snapshot's security_alerts): when (unix seconds), its
/// kind (the mailroom's SECURITY_ALERTS: phone-enrolment, fast-mode, fast-keys...), its subject and its thread in the
/// Mail tab.
struct SecurityAlert: Decodable, Equatable {
    var at: Double
    var kind: String
    var subject: String
    var threadId: String
}

/// A macOS notification for each security alert newer than the newest one notified so far, whose time is kept in the
/// app's defaults (`newestKey`), so neither a relaunch nor a reconnect notifies one twice. With the mailroom's mail kept
/// off Gmail, these alerts would otherwise reach the owner only in the Mail tab, where whoever caused one could archive
/// it unseen from the phone; a notification here is a channel the phone cannot touch. A click opens the alert's thread
/// in the Mail tab (Alerts' delegate, onThread). README, "Security alerts on the Mac".
@MainActor
final class SecurityAlerts {
    nonisolated static let newestKey = "securityAlertNewest"

    private(set) var newest: Double?
    private let save: (Double) -> Void
    private let post: ([SecurityAlert]) -> Void

    init(newest: Double? = UserDefaults.standard.object(forKey: SecurityAlerts.newestKey) as? Double,
         save: ((Double) -> Void)? = nil, post: (([SecurityAlert]) -> Void)? = nil) {
        self.newest = newest
        self.save = save ?? { UserDefaults.standard.set($0, forKey: SecurityAlerts.newestKey) }
        self.post = post ?? SecurityAlerts.notify
    }

    /// The snapshot's alerts now: those newer than the newest notified are posted, oldest first, one notification
    /// each, and the newest of them is kept. Returns what was posted.
    @discardableResult
    func take(_ alerts: [SecurityAlert]) -> [SecurityAlert] {
        let fresh = alerts.filter { a in newest.map { a.at > $0 } ?? true }.sorted { $0.at < $1.at }
        guard let last = fresh.last else { return [] }
        newest = last.at
        save(last.at)
        post(fresh)
        return fresh
    }

    /// A notification's title for an alert of `kind`: what happened, in a few words; an unknown kind says its name.
    nonisolated static func title(_ kind: String) -> String {
        switch kind {
        case "phone-enrolment": return "Security: a phone was enrolled for Agent Mail"
        case "fast-mode": return "Security: fast mode could not put an agent's effort back"
        case "fast-keys": return "Security: fast mode's keys may have reached a dialog"
        case "dkim": return "Security: mail failed its sender check and is held"
        case "collab-thread": return "Security: a collaborator's agent started a thread"
        case "flood": return "Security: a collaborator's mail is held"
        default: return "Security alert (\(kind))"
        }
    }

    /// One notification an alert, each its own (identifier "security.<at>"), carrying its thread for the click; without
    /// notification permission, Notification Center through osascript (Alerts.fallback), which opens nothing.
    nonisolated static func notify(_ alerts: [SecurityAlert]) {
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            let allowed = settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional
            DispatchQueue.main.async {
                let center = UNUserNotificationCenter.current()
                for a in alerts {
                    let body = a.subject + " · open it in the Mail tab"
                    if allowed {
                        let c = UNMutableNotificationContent()
                        c.title = title(a.kind)
                        c.body = String(body.prefix(240))
                        c.sound = .default
                        c.userInfo = ["thread": a.threadId]
                        center.add(UNNotificationRequest(identifier: "security." + String(a.at), content: c, trigger: nil))
                    } else {
                        Alerts.fallback(RedItem(key: "security." + String(a.at), title: title(a.kind), body: body,
                                                agent: nil))
                    }
                }
            }
        }
    }
}

/// --selftest-security: the snapshot's security alerts decoded (one that does not decode left out and said), one
/// notification for each alert not notified before, and none again after a relaunch, titled by kind, and a click on one
/// opening its thread. Offline: nothing is posted.
@MainActor
enum SecurityAlertsTest {
    static func run() -> Int32 {
        var fails = 0
        func check(_ ok: Bool, _ what: String) { print((ok ? "ok    " : "FAIL  ") + what); if !ok { fails += 1 } }
        let json = """
        {"generated": 1800000300, "agents": [], "timeline": [], "summary": {}, "worktrees": [],
         "mail": {"ok": true, "held": [], "held_count": 0, "held_owner": 0}, "problems": [],
         "security_alerts": [
          {"at": 1800000000.5, "kind": "phone-enrolment", "subject": "Phone mail: a passkey was enrolled",
           "thread_id": "L0123456789abcdef"},
          {"at": 1800000100, "kind": "fast-keys", "subject": "Fast mode pressed a key", "thread_id": "L00000000000000aa"},
          {"at": "soon", "kind": "flood", "subject": "x", "thread_id": "y"}]}
        """
        guard let snap = try? JSON.decoder.decode(Snapshot.self, from: Data(json.utf8)) else {
            print("FAIL  the snapshot does not decode")
            return 1
        }
        let alerts = snap.securityAlerts
        check(alerts.map { $0.threadId } == ["L0123456789abcdef", "L00000000000000aa"]
              && alerts.first?.at == 1800000000.5 && alerts.first?.kind == "phone-enrolment",
              "the alerts decode, the one that does not left out")
        check(snap.problems.contains { $0.source.contains("security alert") }, "and what was left out is said")
        let none = try? JSON.decoder.decode(Snapshot.self, from: Data("""
        {"generated": 1, "agents": [], "timeline": [], "summary": {}, "worktrees": [],
         "mail": {"ok": true, "held": [], "held_count": 0, "held_owner": 0}, "problems": []}
        """.utf8))
        check(none?.securityAlerts == [] && none?.problems.isEmpty == true, "a desktop that sends none: none, no problem")

        var posted: [[SecurityAlert]] = []
        var saved: Double?
        let security = SecurityAlerts(newest: nil, save: { saved = $0 }, post: { posted.append($0) })
        security.take([])
        check(posted.isEmpty && saved == nil, "no alerts: nothing posted")
        security.take(alerts.reversed())
        check(posted.count == 1 && posted[0].map { $0.at } == [1800000000.5, 1800000100],
              "first sight: one notification each, oldest first")
        check(saved == 1800000100, "the newest notified is kept")
        security.take(alerts)
        check(posted.count == 1, "the same alerts again: nothing")
        let relaunched = SecurityAlerts(newest: saved, save: { saved = $0 }, post: { posted.append($0) })
        relaunched.take(alerts)
        check(posted.count == 1, "after a relaunch: nothing again")
        let next = SecurityAlert(at: 1800000200, kind: "fast-mode", subject: "Effort not put back", threadId: "L1")
        relaunched.take(alerts + [next])
        check(posted.count == 2 && posted[1] == [next] && saved == 1800000200, "a new one: that one alone")
        relaunched.take([next])
        check(posted.count == 2, "the older ones gone from the snapshot (past 7 days): nothing")

        check(SecurityAlerts.title("phone-enrolment") == "Security: a phone was enrolled for Agent Mail",
              "titled by kind")
        check(SecurityAlerts.title("new-kind") == "Security alert (new-kind)", "an unknown kind says its name")
        check(Alerts.target(["thread": "L0123456789abcdef"]) == .thread("L0123456789abcdef"),
              "a click on a security alert opens its thread")
        check(Alerts.target(["agent": "abc"]) == .board("abc")
              && Alerts.target(["link": "https://x", "account": "a"]) == .link(AlertLink(url: "https://x", account: "a")),
              "other notifications open as before")
        print(fails == 0 ? "security alerts OK" : "security alerts: \(fails) FAILED")
        return fails == 0 ? 0 : 1
    }
}
