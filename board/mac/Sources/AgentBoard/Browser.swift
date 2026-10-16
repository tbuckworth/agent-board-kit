import AppKit
import Foundation

/// Open a Gmail link in the Chrome profile signed in to that Google account. A plain `open URL` lands in whichever
/// Chrome profile was used last; if that is the work or agents profile, Gmail says the account is inaccessible.
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
