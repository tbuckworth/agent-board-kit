import AppKit
import Foundation

/// Open a Google link (Gmail, a meeting's) in the Chrome profile signed in to that Google account. A plain `open URL`
/// lands in whichever Chrome profile was used last; if that is the work or agents profile, Gmail says the account is
/// inaccessible.
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

    /// Why there is no Chrome profile to open a link in.
    struct NoProfile: Error, LocalizedError {
        var errorDescription: String?
        init(_ why: String) { errorDescription = why }
    }

    /// The command that would open `url`, for the self-test and for doing it. Throws NoProfile when there is no
    /// account to match, no Chrome, or no Chrome profile signed in to `email`. Reads Chrome's Local State.
    static func command(_ url: URL, signedInAs email: String?) throws -> [String] {
        guard let email = email, !email.isEmpty else { throw NoProfile("the desktop sent no Google account for you") }
        guard FileManager.default.fileExists(atPath: chromeApp) else { throw NoProfile("Chrome isn't in /Applications") }
        guard let profile = chromeProfile(for: email) else { throw NoProfile("no Chrome profile is signed in to \(email)") }
        // -n: a running Chrome otherwise ignores --args; it hands the URL to the existing browser in that profile
        return ["/usr/bin/open", "-na", chromeApp, "--args", "--profile-directory=" + profile, url.absoluteString]
    }

    /// Open `url` in the Chrome profile signed in as `email`. When that can't be done it opens `url` in the default
    /// browser instead and returns why, for the caller to show; nil when Chrome took it.
    static func open(_ url: URL, signedInAs email: String?) -> String? {
        let cmd: [String]
        do {
            cmd = try command(url, signedInAs: email)
        } catch {
            NSWorkspace.shared.open(url)
            return error.localizedDescription
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cmd[0])
        p.arguments = Array(cmd.dropFirst())
        do {
            try p.run()
        } catch {
            NSWorkspace.shared.open(url)
            return "Chrome didn't start: \(error.localizedDescription)"
        }
        return nil
    }
}
