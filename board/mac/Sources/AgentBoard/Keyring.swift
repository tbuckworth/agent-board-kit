import AppKit
import SwiftUI

// The desktop's keyrings (agentboard/keyring.py), for a Linux desktop that logs in by itself and so leaves
// gnome-keyring locked after a restart: the top bar's mark while Chrome's is locked, and the sheet that unlocks them
// with the owner's password, which goes to `agent-board keyring-unlock` on the ssh process's stdin
// (Actions.keyringUnlock). The password lives only in the sheet's field, which is emptied as it is sent; it is never
// saved, logged, drafted or put on a pasteboard. Swift cannot zero the strings and data that held it, and AutoFill is
// only not asked for (no content type), not ruled out. All of it is off unless the desktop has the keyring unlock on:
// without a keyring section in the snapshot there is no mark, no sheet and no notification.

/// One keyring on the desktop as keyring.status reads it: its object path (nil when the "default" alias names
/// none), its label, and whether it is locked (nil: not read).
struct KeyringCollection: Decodable, Equatable {
    var path: String?
    var label: String?
    var locked: Bool?
}

/// The snapshot's keyring section (keyring.status): Chrome's keyring's state ("locked", "unlocked" or "unknown") and
/// why it is unknown, the boot it was read in, which the once-a-boot notification is keyed on (RedRules), and each
/// watched keyring by name ("default", Chrome's, and "login").
struct KeyringStatus: Decodable, Equatable {
    var state: String
    var error: String?
    var bootId: String?
    var collections: [String: KeyringCollection]? = nil
}

/// How the sheet and its answers name the keyrings keyring.py watches (WATCHED), Chrome's first. The phone's Agent
/// Mail names them the same (keyringName, keyringKeys and keyringLine in web/mail/app.js): change both together.
enum KeyringNames {
    static let watched = ["default", "login"]

    /// "default" as Chrome's keyring, by its label; the others by their labels, quoted.
    static func name(_ key: String, in cols: [String: KeyringCollection]?) -> String {
        let quoted = cols?[key]?.label.map { "'\($0)'" }
        return key == "default" ? "Chrome's " + (quoted ?? "keyring") : (quoted ?? key)
    }

    /// The watched names, without login when the "default" alias names login itself: one keyring, named once.
    static func keys(in cols: [String: KeyringCollection]?) -> [String] {
        let path = cols?["default"]?.path
        return path != nil && path == cols?["login"]?.path ? ["default"] : watched
    }

    static func list(_ keys: [String], in cols: [String: KeyringCollection]?) -> String {
        keys.map { name($0, in: cols) }.joined(separator: " and ")
    }

    /// One line on each watched keyring: "Chrome's 'Default keyring': locked · 'Login': unlocked"; nil without them.
    static func line(_ st: KeyringStatus?) -> String? {
        guard let cols = st?.collections else { return nil }
        return keys(in: cols).map { key -> String in
            let locked = cols[key]?.locked
            return "\(name(key, in: cols)): " + (locked == nil ? "unknown" : locked == true ? "locked" : "unlocked")
        }.joined(separator: " · ")
    }
}

/// What the top bar shows for Chrome's keyring: nothing while it is unlocked or the desktop sends no state, a red mark
/// while it is locked, a grey one while its state is unknown. The phone's Agent Mail shows the same mark in the same
/// words (showKeyring and keyringIdle in web/mail/app.js): change both together.
enum KeyringMark: Equatable {
    case hidden
    case locked
    case unknown(String)

    init(_ st: KeyringStatus?) {
        switch st?.state {
        case "locked": self = .locked
        case "unknown": self = .unknown(st?.error ?? "no reason given")
        default: self = .hidden
        }
    }

    var text: String { self == .locked ? "Desktop browser locked" : "Desktop keyring unknown" }
    var color: Color { self == .locked ? AgentState.blocked.color : .secondary }

    /// The mark's tooltip, naming Chrome's keyring from `st`.
    func help(_ st: KeyringStatus?) -> String {
        let chrome = KeyringNames.name("default", in: st?.collections)
        switch self {
        case .hidden: return ""
        case .locked: return "\(chrome) on the desktop is locked, so Chrome there waits at its password dialog and "
            + "agents can't use the desktop browser. Click to unlock it."
        case .unknown(let why): return "The board can't tell whether \(chrome) on the desktop is locked: \(why)"
        }
    }
}

/// The top bar's lock mark; a click opens the unlock sheet.
struct KeyringChip: View {
    @ObservedObject var model: BoardModel
    @Environment(\.zoom) private var z

    var body: some View {
        let mark = KeyringMark(model.snapshot?.keyring)
        if mark != .hidden {
            Button { model.sheet = .keyring } label: {
                Label(mark.text, systemImage: mark == .locked ? "lock.fill" : "lock.trianglebadge.exclamationmark")
                    .font(.system(size: 11 * z, weight: .semibold))
                    .foregroundColor(mark.color)
            }
            .buttonStyle(.borderless)
            .help(mark.help(model.snapshot?.keyring))
        }
    }
}

/// A snapshot with nothing in it but the keyrings, as on a desktop after a restart once login is unlocked:
/// Chrome's "Default keyring" locked, "Login" unlocked, in the boot `bootId`. For --selftest-keyring and
/// --render-keyring.
enum KeyringSample {
    static func locked(bootId: String) throws -> Snapshot {
        let json = """
            {"generated": 1795000000, "agents": [], "timeline": [], "summary": {}, "worktrees": [], "problems": [],
             "mail": {"ok": true, "held": [], "held_count": 0, "held_owner": 0},
             "keyring": {"state": "locked", "locked": true, "error": null, "boot_id": "\(bootId)",
              "collections": {
               "default": {"path": "/org/freedesktop/secrets/collection/Default_5fkeyring", "label": "Default keyring",
                           "locked": true, "error": null},
               "login": {"path": "/org/freedesktop/secrets/collection/login", "label": "Login", "locked": false,
                         "error": null}}}}
            """
        return try JSON.decoder.decode(Snapshot.self, from: Data(json.utf8))
    }
}

/// `agent-board keyring-unlock`'s one JSON line (keyring.unlock_from).
struct KeyringAnswer: Decodable, Equatable {
    var result: String
    var error: String?
    var retryAfter: Int?
    var tried: [String: String]?                     // each keyring tried: "unlocked", "wrong" or "error"
    var collections: [String: KeyringCollection]?   // as read after
}

/// What the sheet says after an unlock, and whether it is good news.
struct KeyringSaid: Equatable {
    var text: String
    var ok: Bool

    /// What the desktop's stdout (its one JSON line) and ssh's stderr come to. The phone's Agent Mail says the same of
    /// each answer (unlockSaid in web/mail/app.js): change both together.
    static func of(stdout: Data, stderr: String) -> KeyringSaid {
        let line = String(decoding: stdout, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !line.isEmpty else {
            let why = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            return KeyringSaid(text: "Not unlocked: " + (why.isEmpty ? "no answer from the desktop" : why), ok: false)
        }
        guard let a = try? JSON.decoder.decode(KeyringAnswer.self, from: Data(line.utf8)) else {
            return KeyringSaid(text: "Not unlocked: the desktop's answer was not understood: \(line.prefix(200))",
                               ok: false)
        }
        let tried = a.tried ?? [:]
        let keys = KeyringNames.keys(in: a.collections).filter { tried[$0] != nil }
        func named(_ outcome: String) -> [String] { keys.filter { tried[$0] == outcome } }
        func list(_ keys: [String]) -> String { KeyringNames.list(keys, in: a.collections) }
        switch a.result {
        case "unlocked":
            return KeyringSaid(text: "Unlocked. The desktop's browser can use its passwords again.", ok: true)
        case "wrong":
            let still = named("wrong")
            return KeyringSaid(text: "Wrong password." + (still.isEmpty ? "" : " Still locked: \(list(still))."),
                               ok: false)
        case "partial":
            let refused = named("wrong"), failed = named("error")
            var text = "Unlocked \(list(named("unlocked")))"
            if !refused.isEmpty { text += ", but \(list(refused)) has a different password" }
            if !failed.isEmpty {
                text += (refused.isEmpty ? ", but " : "; ") + "\(list(failed)) could not be unlocked: "
                    + (a.error ?? "the desktop gave no reason")
            }
            return KeyringSaid(text: text + ".", ok: tried["default"] == "unlocked")
        case "rate_limited":
            let when = a.retryAfter.map { "in \(max(1, ($0 + 59) / 60)) min" } ?? "later (the desktop did not say when)"
            return KeyringSaid(text: "Too many wrong passwords: try again \(when).", ok: false)
        default:
            return KeyringSaid(text: "Not unlocked: " + (a.error ?? "the desktop gave no reason"), ok: false)
        }
    }
}

/// The unlock sheet's state: the password being typed, whether one is on its way, and what came of the last.
@MainActor
final class KeyringUnlock: ObservableObject {
    @Published var password = ""
    @Published private(set) var busy = false
    @Published private(set) var said: KeyringSaid?
    /// One line to the desktop, (stdout, stderr) back, off the main thread: Actions.keyringUnlock, or a stand-in.
    private let send: (Data) -> (Data, String)
    /// After each answer: the board asks the desktop to read the keyring's state again.
    var answered: (@MainActor () -> Void)?

    init(send: @escaping (Data) -> (Data, String) = Actions.keyringUnlock) {
        self.send = send
    }

    var canSubmit: Bool { !busy && !password.isEmpty }

    /// Send the password with its newline, emptying the field at once; nothing when it is empty or one is on its way.
    func submit() {
        guard canSubmit else { return }
        var line = Data(password.utf8)
        line.append(0x0A)
        password = ""
        busy = true
        said = nil
        let send = self.send
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let (out, err) = send(line)
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self = self else { return }
                    self.busy = false
                    self.said = KeyringSaid.of(stdout: out, stderr: err)
                    self.answered?()
                }
            }
        }
    }
}

/// The unlock sheet: a password field with no autofill, Unlock, and what came of it.
struct KeyringSheet: View {
    @ObservedObject var model: BoardModel
    @StateObject private var unlock: KeyringUnlock
    @FocusState private var focused: Bool

    /// `unlock`: the sheet's state, for an offscreen render; nil makes a new one, which goes with the sheet.
    @MainActor
    init(model: BoardModel, unlock: KeyringUnlock? = nil) {
        _model = ObservedObject(wrappedValue: model)
        _unlock = StateObject(wrappedValue: unlock ?? KeyringUnlock())
    }

    var body: some View {
        let mark = KeyringMark(model.snapshot?.keyring)
        VStack(alignment: .leading, spacing: 12) {
            Text("Unlock the desktop").font(.headline)
            Text("The desktop logs in by itself, so its keyrings stay locked after a restart and Chrome there waits "
                 + "at its password dialog. Type the keyring's password: it is tried on each one that is locked, "
                 + "Chrome's first, goes to the desktop over SSH and is kept nowhere.")
                .fixedSize(horizontal: false, vertical: true)
            SecureField("Keyring password", text: $unlock.password)
                .textContentType(nil)
                .focused($focused)
                .disabled(unlock.busy)
                .onSubmit { unlock.submit() }
            HStack(spacing: 6) {
                if unlock.busy {
                    ProgressView().controlSize(.small)
                    Text("Unlocking…").foregroundStyle(.secondary)
                } else if let s = unlock.said {
                    Image(systemName: s.ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                        .foregroundColor(s.ok ? .green : AgentState.blocked.color)
                    Text(s.text).fixedSize(horizontal: false, vertical: true)
                } else if case .unknown = mark {
                    Text(mark.help(model.snapshot?.keyring)).foregroundStyle(.secondary)
                } else {
                    Text(KeyringNames.line(model.snapshot?.keyring)
                         ?? (mark == .locked ? "Chrome's keyring is locked." : "Chrome's keyring is unlocked."))
                        .foregroundStyle(.secondary)
                }
            }
            HStack {
                Spacer()
                Button("Close") { unlock.password = ""; model.sheet = nil }.keyboardShortcut(.cancelAction)
                Button("Unlock") { unlock.submit() }.disabled(!unlock.canSubmit).keyboardShortcut(.defaultAction)
            }
        }
        .padding(18)
        .frame(width: 460)
        .onAppear {
            focused = true
            unlock.answered = { [weak model] in model?.refreshKeyring() }
        }
        .onDisappear { unlock.password = "" }
    }
}

/// --selftest-keyring: the mark, the keyrings' names, the desktop's answers (partial ones too), the sheet's sending
/// (to a stand-in desktop), the ssh command line and the once-a-boot notification, offline.
@MainActor
enum KeyringTest {
    static func run() -> Int32 {
        var fails = 0
        func check(_ ok: Bool, _ what: String) { print((ok ? "ok    " : "FAIL  ") + what); if !ok { fails += 1 } }

        let locked = KeyringStatus(state: "locked", error: nil, bootId: "boot-1")
        check(KeyringMark(nil) == .hidden && KeyringMark(KeyringStatus(state: "unlocked")) == .hidden,
              "no mark while unlocked, or with no state from the desktop")
        check(KeyringMark(locked) == .locked && KeyringMark(locked).text == "Desktop browser locked",
              "a red \"Desktop browser locked\" mark while locked")
        check(KeyringMark(KeyringStatus(state: "unknown", error: "busctl: no bus")) == .unknown("busctl: no bus"),
              "a grey mark, with the reason, while unknown")
        let snap = try? KeyringSample.locked(bootId: "boot-0")
        check(snap?.keyring?.state == "locked" && snap?.keyring?.bootId == "boot-0"
              && snap?.keyring?.collections?["default"]?.label == "Default keyring"
              && snap?.keyring?.collections?["login"]?.locked == false, "the snapshot's keyrings decode")
        check(KeyringNames.line(snap?.keyring) == "Chrome's 'Default keyring': locked · 'Login': unlocked",
              "the sheet names each keyring and its state: \(KeyringNames.line(snap?.keyring) ?? "nothing")")
        check(KeyringMark(snap?.keyring).help(snap?.keyring)
                .hasPrefix("Chrome's 'Default keyring' on the desktop is locked"),
              "the mark's tooltip names Chrome's keyring")

        func said(_ out: String, _ err: String = "") -> KeyringSaid {
            KeyringSaid.of(stdout: Data(out.utf8), stderr: err)
        }
        check(said(#"{"result": "unlocked", "locked": false}"# + "\n").ok, "unlocked is good news")
        check(said(#"{"result": "wrong", "locked": true}"#).text.hasPrefix("Wrong password"),
              "a wrong password says so")
        let cols = """
            "collections": {"default": {"path": "/c/Default_5fkeyring", "label": "Default keyring", "locked": true},
                            "login": {"path": "/c/login", "label": "Login", "locked": false}}
            """
        let partial = said(#"{"result": "partial", "locked": true, "#
                           + #""tried": {"default": "wrong", "login": "unlocked"}, "# + cols + "}")
        check(partial.text == "Unlocked 'Login', but Chrome's 'Default keyring' has a different password."
              && !partial.ok, "login opened, Chrome's not: says which, and is not good news: \(partial.text)")
        let other = said(#"{"result": "partial", "locked": false, "tried": {"default": "unlocked", "login": "wrong"}, "#
                         + cols + "}")
        check(other.text == "Unlocked Chrome's 'Default keyring', but 'Login' has a different password." && other.ok,
              "Chrome's opened, login not: the browser works: \(other.text)")
        let both = said(#"{"result": "wrong", "locked": true, "tried": {"default": "wrong", "login": "wrong"}, "#
                        + cols + "}")
        check(both.text == "Wrong password. Still locked: Chrome's 'Default keyring' and 'Login'.",
              "a wrong password names what is still locked: \(both.text)")
        let failing = said(#"{"result": "partial", "locked": false, "error": "login: no reply", "#
                           + #""tried": {"default": "unlocked", "login": "error"}, "# + cols + "}")
        check(failing.text == "Unlocked Chrome's 'Default keyring', but 'Login' could not be unlocked: login: no reply."
              && failing.ok, "one opened, the other failing: says why: \(failing.text)")
        let one = said(#"{"result": "wrong", "locked": true, "tried": {"default": "wrong", "login": "wrong"}, "#
                       + #""collections": {"default": {"path": "/c/login", "label": "Login"}, "#
                       + #""login": {"path": "/c/login", "label": "Login"}}}"#)
        check(one.text == "Wrong password. Still locked: Chrome's 'Login'.",
              "an alias naming login itself names it once: \(one.text)")
        check(said(#"{"result": "rate_limited", "locked": true, "retry_after": 125}"#)
                .text.contains("try again in 3 min"), "held back: when to try again")
        check(said(#"{"result": "rate_limited", "locked": true}"#).text.contains("did not say when"),
              "held back with no time given: no time made up")
        check(said(#"{"result": "error", "locked": null, "error": "no session bus"}"#).text
              == "Not unlocked: no session bus", "an error gives the desktop's reason")
        check(said("", "ssh: connect to host desktop port 22: Operation timed out\n").text
              == "Not unlocked: ssh: connect to host desktop port 22: Operation timed out", "no answer: ssh's reason")
        check(!said("Traceback (most recent call last):").ok, "an answer that is not JSON is not good news")

        let args = Actions.sshArguments("keyring-unlock", interpreter: Actions.isolatedPython)
        check(args.last == "exec /usr/bin/python3 -I \(Settings.collector) keyring-unlock"
              && args.contains("BatchMode=yes"),
              "the ssh command line is the isolated system Python and the command alone: \(args.last ?? "")")
        let kitArgs = Actions.sshArguments("keyring-unlock", interpreter: Actions.isolatedPython,
                                           collector: Actions.shellQuoted("/home/alex/agent board kit/board/bin/agent-board"))
        check(kitArgs.last == "exec /usr/bin/python3 -I '/home/alex/agent board kit/board/bin/agent-board' keyring-unlock",
              "and on the kit's script itself, quoted: \(kitArgs.last ?? "")")
        // the kit's wrapper (bin/kit's wrapper_text), which the system's Python cannot run: the script it runs
        let wrapper = "#!/usr/bin/env bash\n\(Actions.kitWrapperMark) (generated by kit install; re-run it to update, "
            + "safe to delete)\nexec /home/alex/.local/share/agent-kit/venv/bin/python "
            + "'/home/alex/agent board kit/board/bin/agent-board' \"$@\"\n"
        check(Actions.wrappedScript(wrapper) == "/home/alex/agent board kit/board/bin/agent-board",
              "a kit wrapper's script, its quotes taken off: \(Actions.wrappedScript(wrapper) ?? "none")")
        check(Actions.wrappedScript("#!/usr/bin/env bash\nexec /usr/bin/env python3 /k/board/bin/agent-board \"$@\"\n")
                == "/k/board/bin/agent-board"
              && Actions.wrappedScript("#!/usr/bin/env bash\nexec /k/bin/tool \"$@\"\n") == nil
              && Actions.wrappedScript("#!/usr/bin/env python3\nimport sys\n") == nil,
              "through env too; none for a wrapper of a script that is no Python, or for the script itself")
        check(Actions.shellWords(#"exec "a b" c\ d 'e'"'"'f' "g\"h""#) == ["exec", "a b", "c d", "e'f", "g\"h"],
              "shell words, quoted as shlex.quote and sh quote them")
        check(Actions.shellQuoted("it's") == #"'it'\''s'"#, "a quote inside a quoted word")

        var sent: [Data] = []
        let gate = DispatchSemaphore(value: 0)
        let unlock = KeyringUnlock(send: { line in
            sent.append(line)
            gate.wait()
            return (Data(#"{"result": "wrong", "locked": true}"#.utf8), "")
        })
        var answers = 0
        unlock.answered = { answers += 1 }
        unlock.submit()
        check(sent.isEmpty && !unlock.busy, "an empty field sends nothing")
        let secret = "selftest pässword"
        unlock.password = secret
        unlock.submit()
        check(unlock.password.isEmpty && unlock.busy, "the field is emptied as it is sent")
        unlock.password = "again"
        unlock.submit()
        func spin(until done: () -> Bool) {
            let end = Date().addingTimeInterval(5)
            while !done() && Date() < end { RunLoop.current.run(until: Date().addingTimeInterval(0.02)) }
        }
        spin { !sent.isEmpty }
        gate.signal()
        spin { !unlock.busy }
        check(sent == [Data((secret + "\n").utf8)], "one line went, the password and its newline, nothing while busy")
        check(unlock.said?.text.hasPrefix("Wrong password") == true && answers == 1,
              "the answer is shown and the board asked to read the state again")
        let stored = UserDefaults.standard.dictionaryRepresentation().values.contains { "\($0)".contains(secret) }
        check(!stored, "the password is in no setting")

        var posted: [[RedItem]] = []
        var once: [String] = []
        let alerts = Alerts(saved: [], once: [], save: { _ in }, saveOnce: { once = $0 }, post: { posted.append($0) })
        var s = snap
        func reds() -> [RedItem] { RedRules.items(snapshot: s, usage: nil, link: .live, downFor: 0) }
        alerts.update(reds())
        check(posted.count == 1 && posted[0].map(\.key) == ["keyring:boot-0"],
              "locked: one notification, keyed on the boot")
        s?.keyring = KeyringStatus(state: "unknown", error: "busctl timed out", bootId: "boot-0")
        alerts.update(reds())
        s?.keyring = KeyringStatus(state: "locked", error: nil, bootId: "boot-0")
        alerts.update(reds())
        check(posted.count == 1, "locked again in the same boot, after an unknown reading: not again")
        let relaunched = Alerts(saved: [], once: once, save: { _ in }, saveOnce: { _ in }, post: { posted.append($0) })
        relaunched.update(reds())
        check(posted.count == 1, "after a relaunch: not again")
        s?.keyring = KeyringStatus(state: "locked", error: nil, bootId: "boot-2")
        relaunched.update(reds())
        check(posted.count == 2 && posted[1].first?.key == "keyring:boot-2", "the next boot: once again")
        print(fails == 0 ? "keyring OK" : "keyring: \(fails) FAILED")
        return fails == 0 ? 0 : 1
    }
}
