import AppKit
import Foundation

/// An agent's latest mail to the owner, opened in their Gmail: double-click the agent on a task, as on the agent
/// board. The desktop helper's `agent_mail` finds the mail in the mailroom's records and, when the agent board's
/// optional Gmail read-check is on, its thread in the owner's mailbox; this turns that into a link (the thread, else
/// a search for the message). The same link AgentBoard builds (`Board.latestMailURL`).
enum AgentMail {
    /// The thread itself when the desktop found it in his mailbox, else a search for that exact message.
    static func url(thread: String?, messageID: String?, owner: String?) -> URL? {
        // ?authuser=EMAIL picks the account (Gmail redirects to its /u/N/); a literal /u/EMAIL/ path gives
        // "Temporary Error (404): your account is temporarily unavailable"
        let safe = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "@.-_"))   // `+` would read as a space
        let base = "https://mail.google.com/mail/" + (owner.flatMap { $0.isEmpty ? nil : $0 }.flatMap {
            $0.addingPercentEncoding(withAllowedCharacters: safe).map { "?authuser=" + $0 } } ?? "u/0/")
        if let t = thread, !t.isEmpty { return URL(string: base + "#all/" + t) }
        guard let mid = messageID, !mid.isEmpty else { return nil }
        let q = ("rfc822msgid:" + mid.trimmingCharacters(in: CharacterSet(charactersIn: "<> ")))
            .addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
        return URL(string: base + "#search/" + q)
    }
}

/// Open a Gmail link in the Chrome profile signed in to that Google account (AgentBoard's `Browser`). A plain
/// `open URL` lands in whichever Chrome profile was used last; if that is the work or agents profile, Gmail says
/// the account is inaccessible.
enum Browser {
    static let chromeApp = "/Applications/Google Chrome.app"

    /// The profile directory (e.g. "Default", "Profile 3") whose signed-in account is `email`, from Chrome's own list.
    static func chromeProfile(for email: String) -> String? {
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Google/Chrome/Local State")
        guard let data = try? Data(contentsOf: path),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let cache = (root["profile"] as? [String: Any])?["info_cache"] as? [String: Any] else { return nil }
        for (dir, info) in cache {
            if let user = (info as? [String: Any])?["user_name"] as? String, user.lowercased() == email.lowercased() {
                return dir
            }
        }
        return nil
    }

    /// The command that would open `url`, for the self-test and for doing it.
    static func command(_ url: URL, signedInAs email: String?) -> [String]? {
        guard let email = email, !email.isEmpty, FileManager.default.fileExists(atPath: chromeApp),
              let profile = chromeProfile(for: email) else { return nil }
        // -n: a running Chrome otherwise ignores --args; it hands the URL to the existing browser in that profile
        return ["/usr/bin/open", "-na", chromeApp, "--args", "--profile-directory=" + profile, url.absoluteString]
    }

    static func open(_ url: URL, signedInAs email: String?) {
        guard let cmd = command(url, signedInAs: email) else {
            NSWorkspace.shared.open(url)
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cmd[0])
        p.arguments = Array(cmd.dropFirst())
        if (try? p.run()) == nil { NSWorkspace.shared.open(url) }
    }
}

/// `--selftest`: the links, and what a double-click does with each answer from the desktop.
@MainActor
enum AgentMailSelfTest {
    static func run(_ check: (Bool, String) -> Void) {
        let owner = "owner@example.com"
        check(AgentMail.url(thread: "18f0000000c0ffee", messageID: "<x@y>", owner: owner)?.absoluteString
              == "https://mail.google.com/mail/?authuser=owner@example.com#all/18f0000000c0ffee", "agent mail: thread link")
        check(AgentMail.url(thread: nil, messageID: "<CAJt+a=b@mail.gmail.com>", owner: owner)?.absoluteString
              == "https://mail.google.com/mail/?authuser=owner@example.com#search/rfc822msgid%3ACAJt%2Ba%3Db%40mail%2Egmail%2Ecom",
              "agent mail: search link")
        check(AgentMail.url(thread: "", messageID: "<a@b>", owner: nil)?.absoluteString.hasPrefix("https://mail.google.com/mail/u/0/#search/") == true,
              "agent mail: no owner")
        check(AgentMail.url(thread: nil, messageID: nil, owner: owner) == nil, "agent mail: nothing to open")

        let info = AgentInfo(status: "started", name: "todo-9-x", assignedAt: nil, events: [])
        let issue = Issue(number: 9, title: "T", body: "", state: "OPEN", url: "", createdAt: "2026-10-01T00:00:00Z",
                          updatedAt: "2026-10-02T00:00:00Z", labels: ["list:tasks", assignedLabel], agent: info)
        let desk = FakeDesk()
        let s = Store(channel: desk, persist: false)
        s.install(issues: [issue], config: nil)
        var opened: [(URL, String?)] = []
        s.openURL = { opened.append(($0, $1)) }
        desk.answer("agent_mail") { _ in .success(["name": "todo-9-x", "owner": owner,
                                                   "mail": ["message_id": "<a@b>", "subject": "Done: the thing", "thread": "abc"]]) }
        s.openAgentMail(s.items[9]!)
        check(desk.requests.last?["cmd"] as? String == "agent_mail" && desk.requests.last?["number"] as? Int == 9,
              "agent mail: asks the desktop for task 9's agent")
        check(opened.count == 1 && opened[0].0.absoluteString.hasSuffix("#all/abc") && opened[0].1 == owner, "agent mail: opens the thread")
        check(s.toast?.text == "Opening todo-9-x's latest email: Done: the thing",
              "agent mail: says what it opens (\(s.toast?.text ?? "-"))")
        desk.answer("agent_mail") { _ in .success(["name": "todo-9-x", "owner": owner, "mail": NSNull()]) }
        s.openAgentMail(s.items[9]!)
        check(opened.count == 1 && s.toast?.text == "todo-9-x hasn't emailed you yet.", "agent mail: none yet")
    }
}
