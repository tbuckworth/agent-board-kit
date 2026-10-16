import AppKit
import Foundation

/// "Open in Kitty": attach to one agent's session on the desktop. A new OS window in the running kitty
/// (via its remote-control socket, kitty.conf `listen_on unix:/tmp/kitty-{pid}`), running
/// `kitten ssh -t desktop tmux attach -t =SESSION`; with no kitty running, a new kitty with that command.
enum Kitty {
    static let kitten = first(["/opt/homebrew/bin/kitten", "/Applications/kitty.app/Contents/MacOS/kitten", "/usr/local/bin/kitten"])
    static let kitty = first(["/opt/homebrew/bin/kitty", "/Applications/kitty.app/Contents/MacOS/kitty", "/usr/local/bin/kitty"])

    private static func first(_ paths: [String]) -> String? {
        paths.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    /// The command kitty should run for this agent, or nil if there is nothing to attach to.
    static func command(for a: Agent) -> [String]? {
        guard let kitten = kitten, let remote = remoteCommand(for: a) else { return nil }
        // one remote line; if attaching fails, say so and wait instead of the window vanishing
        let line = remote + " || { echo; echo '[agent board] attach failed (see above). Press Enter to close.'; read _; }"
        return [kitten, "ssh", "-t", Settings.host, line]
    }

    /// What to run on the desktop: tmux attach, or `claude attach` for a --bg session, under that session's own
    /// login (a background session on a second login is invisible to the default login's `claude attach`).
    static func remoteCommand(for a: Agent) -> String? {
        if let t = a.tmux, !t.isEmpty { return "tmux attach -t " + shellQuote("=" + t) }
        if a.kind == "bg" && a.cli == "claude" {
            let env = (a.accountDefault ?? true) ? "" : (a.configDir.map { "env CLAUDE_CONFIG_DIR=" + shellQuote($0) + " " } ?? "")
            return env + "~/.local/bin/claude attach " + a.id
        }
        return nil
    }

    static func canOpen(_ a: Agent) -> Bool { a.live && command(for: a) != nil }

    @MainActor
    static func open(_ a: Agent, model: BoardModel) {
        guard let cmd = command(for: a) else {
            model.show(kitten == nil ? "kitty isn't installed in /Applications or /opt/homebrew." : "\(a.display) has no tmux session to attach to.")
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let message = launch(cmd)
            Task { @MainActor in
                model.show(message ?? "Opened \(a.display) in kitty.")
                NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == "net.kovidgoyal.kitty" }?
                    .activate(options: [.activateIgnoringOtherApps])
            }
        }
    }

    /// nil on success, else what went wrong.
    private static func launch(_ cmd: [String]) -> String? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        if let kitten = kitten, let sock = socket(kitten) {
            let (code, out) = run(kitten, ["@", "--to", "unix:" + sock, "launch", "--type=os-window", "--cwd", home] + cmd)
            if code == 0 {
                let id = out.trimmingCharacters(in: .whitespacesAndNewlines)
                if !id.isEmpty { _ = run(kitten, ["@", "--to", "unix:" + sock, "focus-window", "--match", "id:" + id]) }
                return nil
            }
        }
        guard let kitty = kitty else { return "kitty isn't installed" }
        let (code, out) = run(kitty, ["--detach"] + cmd)      // no kitty running: start one with the command
        return code == 0 ? nil : "kitty didn't start: \(out.suffix(200))"
    }

    /// What Open in Kitty would do for each live agent in a snapshot, without opening anything.
    static func selftest(snapshot path: String) -> Int32 {
        guard let d = FileManager.default.contents(atPath: path), let snap = try? JSON.decoder.decode(Snapshot.self, from: d) else { return 1 }
        print("kitten:", kitten ?? "missing", "| kitty:", kitty ?? "missing", "| socket:", kitten.flatMap { socket($0) } ?? "none (would start kitty)")
        let model = MainActor.assumeIsolated { () -> BoardModel in
            let m = BoardModel(layout: Layout(), persist: false)
            m.snapshot = snap
            return m
        }
        for a in snap.agents where a.live {
            print("  \(a.display): " + (command(for: a).map { $0.joined(separator: " ") } ?? "(nothing to attach to)"))
            let url = MainActor.assumeIsolated { model.latestMailURL(for: a) }
            print("      latest mail: " + (url.map { u in Browser.command(u, signedInAs: snap.owner)?.joined(separator: " ") ?? "open " + u.absoluteString } ?? "(none in the last week)"))
        }
        return 0
    }

    /// A running kitty's remote-control socket that answers.
    private static func socket(_ kitten: String) -> String? {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: "/tmp")) ?? []
        for n in names where n.hasPrefix("kitty-") {
            let path = "/tmp/" + n
            var st = stat()
            guard lstat(path, &st) == 0, (st.st_mode & S_IFMT) == S_IFSOCK else { continue }
            if run(kitten, ["@", "--to", "unix:" + path, "ls"]).0 == 0 { return path }
        }
        return nil
    }

    private static func run(_ exe: String, _ args: [String]) -> (Int32, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        var env = ProcessInfo.processInfo.environment          // a GUI app's PATH lacks Homebrew and kitty
        env["PATH"] = "/opt/homebrew/bin:/Applications/kitty.app/Contents/MacOS:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        p.environment = env
        let out = Pipe()
        p.standardOutput = out
        p.standardError = out
        do { try p.run() } catch { return (127, error.localizedDescription) }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus, String(data: data, encoding: .utf8) ?? "")
    }
}
