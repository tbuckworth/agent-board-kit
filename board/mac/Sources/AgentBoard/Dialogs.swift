import AppKit

/// A plain-text box for dialogs where ⌘↩ (Return or keypad Enter) calls its `submit`, which sendOnCommandEnter makes
/// press the dialog's main button; plain Return still makes a new line. Given a `take` (MessageFiles), it takes pasted
/// and dropped files and images.
@MainActor
func textArea(width: CGFloat, height: CGFloat) -> (NSScrollView, FileTakingTextView) {
    let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: width, height: height))
    scroll.hasVerticalScroller = true
    scroll.borderType = .bezelBorder
    let text = FileTakingTextView(frame: scroll.bounds)
    text.submitWith = .command
    text.isRichText = false
    text.font = .systemFont(ofSize: 13)
    text.autoresizingMask = [.width]
    text.isAutomaticQuoteSubstitutionEnabled = false
    scroll.documentView = text
    return (scroll, text)
}

/// The files a dialog's message carries (Dialogs.compose): pasted or dropped on its text (Attachments), or picked with
/// its Attach button, within a Mail-tab reply's limits (MailModel.maxFiles, maxFileBytes), as the desktop takes them
/// ({"name", "data": base64}, actions.py's _request_files). `changed` runs after each change.
@MainActor
final class MessageFiles {
    private(set) var files: [(name: String, data: Data)] = []
    var changed: (() -> Void)?

    /// Add `items` (all or none): nil, or why not.
    func add(_ items: [Attachments.Item], at date: Date) -> String? {
        if let no = Attachments.refusal(items, count: files.count, bytes: bytes, what: "a message") { return no.why }
        var added: [(name: String, data: Data)] = []
        do {
            for item in items { added.append((Attachments.name(item, at: date), try Attachments.data(item))) }
        } catch {
            return "Not attached: \(error.localizedDescription)"
        }
        files += added
        changed?()
        return nil
    }

    func removeAll() {
        files = []
        changed?()
    }

    var bytes: Int { files.reduce(0) { $0 + $1.data.count } }

    /// What the request carries of them: actions.py's "files".
    var request: [[String: String]] { files.map { ["name": $0.name, "data": $0.data.base64EncodedString()] } }

    /// One line naming them, for under the text.
    var summary: String {
        files.isEmpty ? "Paste or drop images and files on the text, or attach them."
            : "\(files.count) file\(files.count == 1 ? "" : "s"), \(Fmt.bytes(Double(bytes))): "
              + files.map(\.name).joined(separator: ", ")
    }
}

/// Dialogs.filesRow's view, which keeps its buttons' actions (a button's target is weak).
final class FilesRowView: NSView {
    private var actions: [ClosureAction] = []

    /// Clicking `button` runs `action`.
    @MainActor
    func click(_ button: NSButton, _ action: @escaping @MainActor () -> Void) {
        let a = ClosureAction(action)
        actions.append(a)
        button.target = a
        button.action = #selector(ClosureAction.run)
    }
}

/// Make ⌘↩ press this button (it is the alert's main one) and wire the text box to it.
@MainActor
func sendOnCommandEnter(_ button: NSButton, _ text: FileTakingTextView) {
    button.keyEquivalent = "\r"
    button.keyEquivalentModifierMask = [.command]
    text.submit = { [weak button] in button?.performClick(nil) }
}

/// Every action that changes anything on the desktop asks first. Force close asks twice as hard:
/// it shows what agent-close --dry-run says would be discarded, and the name must be typed back.
@MainActor
enum Dialogs {
    static weak var window: NSWindow?
    /// Dialogs open now. The board holds its updates meanwhile (BoardModel.handle), so typing in one never waits on
    /// a redraw of every tile.
    private(set) static var openCount = 0

    private static func present(_ alert: NSAlert, _ done: @escaping (NSApplication.ModalResponse) -> Void) {
        Dialogs.openCount += 1
        let finish: (NSApplication.ModalResponse) -> Void = { r in
            Dialogs.openCount -= 1
            done(r)
        }
        if let w = window, w.isVisible {
            alert.beginSheetModal(for: w, completionHandler: finish)
        } else {
            finish(alert.runModal())
        }
    }

    // MARK: the three kinds of dialog

    /// A confirmation that first runs the read-only `check` and shows its answer in the dialog, through `text`: the
    /// clean-ups and the force removals. With `typeBack` it is destructive: shown as critical, and `request` runs only
    /// once that name is typed back (sent as "confirm"). A toast says `done`, or `failed` and the desktop's reason;
    /// `refresh` asks for a fresh snapshot after a success.
    private static func confirmAfterCheck(model: BoardModel, checking: String, check: [String: Any], title: String,
                                          text: @escaping (String) -> String, button: String, typeBack: String?,
                                          request: [String: Any], done: String, failed: String, refresh: Bool) {
        model.show(checking)
        Actions.run(check) { answer in
            let alert = NSAlert()
            alert.messageText = title
            alert.informativeText = text(answer.message.isEmpty ? "(no answer)" : String(answer.message.suffix(900)))
            var field: NSTextField?
            if let name = typeBack {
                alert.alertStyle = .critical
                let f = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
                f.placeholderString = name
                alert.accessoryView = f
                field = f
            }
            let main = alert.addButton(withTitle: button)
            main.hasDestructiveAction = typeBack != nil
            alert.addButton(withTitle: "Cancel")
            if let f = field { alert.window.initialFirstResponder = f }
            present(alert) { r in
                guard r == .alertFirstButtonReturn else { return }
                var req = request
                if let name = typeBack {
                    guard field?.stringValue == name else {
                        model.show("Name didn't match; nothing was done.")
                        return
                    }
                    req["confirm"] = name
                }
                Actions.run(req) { res in
                    model.show(res.ok ? done : failed + res.message)
                    if res.ok && refresh { model.refreshAll() }
                }
            }
        }
    }

    /// A dialog with a text box: its main button, or ⌘↩, hands the text, trimmed, to `action`. Empty text does
    /// nothing unless `optional`, or there are `files`: given them, the box takes pasted and dropped files and images,
    /// and a row under it names them, with Attach… (a file panel) and Remove (filesRow).
    private static func compose(title: String, info: String, width: CGFloat, height: CGFloat, button: String,
                                optional: Bool, files: MessageFiles?, above: NSView? = nil,
                                action: @escaping (String) -> Void) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = info
        let (scroll, text) = textArea(width: width, height: height)
        var box: NSView = scroll
        if let files = files {
            let (row, take) = filesRow(scroll, files, width: width)
            text.take = take
            box = row
        }
        if let above = above {          // a row over the text box, such as the start dialog's login
            let both = NSView(frame: NSRect(x: 0, y: 0, width: width, height: box.frame.height + above.frame.height + 6))
            box.frame.origin.y = 0
            above.frame.origin.y = box.frame.height + 6
            [box, above].forEach(both.addSubview)
            box = both
        }
        alert.accessoryView = box
        sendOnCommandEnter(alert.addButton(withTitle: button), text)
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = text
        present(alert) { r in
            let body = text.string.trimmingCharacters(in: .whitespacesAndNewlines)
            guard r == .alertFirstButtonReturn, optional || !body.isEmpty || !(files?.files.isEmpty ?? true) else { return }
            action(body)
        }
    }

    /// `scroll` (the text box) over a row for `files`: Attach… (a file panel), what they are (MessageFiles.summary, or
    /// why some were not attached, in red), and Remove; with the text box's `take`, which adds what a pasteboard holds
    /// (Attachments.items) and says whether it took any.
    static func filesRow(_ scroll: NSScrollView, _ files: MessageFiles, width: CGFloat) -> (NSView, (NSPasteboard) -> Bool) {
        let row: CGFloat = 28
        let box = FilesRowView(frame: NSRect(x: 0, y: 0, width: width, height: scroll.frame.height + row))
        scroll.frame.origin.y = row
        box.addSubview(scroll)
        let attach = NSButton(title: "Attach…", target: nil, action: nil)
        attach.image = NSImage(systemSymbolName: "paperclip", accessibilityDescription: "Attach files")
        attach.imagePosition = .imageLeading
        attach.frame = NSRect(x: 0, y: 0, width: 96, height: 24)
        let remove = NSButton(title: "Remove", target: nil, action: nil)
        remove.frame = NSRect(x: width - 80, y: 0, width: 80, height: 24)
        let label = NSTextField(labelWithString: "")
        label.frame = NSRect(x: 100, y: 4, width: width - 184, height: 16)
        label.font = .systemFont(ofSize: 11)
        label.lineBreakMode = .byTruncatingTail
        [attach, label, remove].forEach(box.addSubview)
        func say(_ refused: String?) {
            label.stringValue = refused ?? files.summary
            label.textColor = refused == nil ? .secondaryLabelColor : .systemRed
            label.toolTip = label.stringValue
            remove.isHidden = files.files.isEmpty
        }
        files.changed = { say(nil) }
        say(nil)
        box.click(attach) {
            let panel = NSOpenPanel()
            panel.allowsMultipleSelection = true
            panel.canChooseDirectories = false
            guard panel.runModal() == .OK, !panel.urls.isEmpty else { return }
            if let why = files.add(panel.urls.map { .file($0) }, at: Date()) { say(why) }
        }
        box.click(remove) { files.removeAll() }
        return (box, { pb in
            let items = Attachments.items(pb)
            guard !items.isEmpty else { return false }
            if let why = files.add(items, at: Date()) { say(why) }
            return true
        })
    }

    /// A dialog with a one-line field that starts as `initial`: its main button hands what was typed, trimmed of
    /// spaces, to `action`.
    private static func askLine(title: String, info: String, initial: String, placeholder: String?, button: String,
                                action: @escaping (String) -> Void) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = info
        let field = NSTextField(string: initial)
        field.placeholderString = placeholder
        field.frame = NSRect(x: 0, y: 0, width: 260, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: button)
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        present(alert) { r in
            if r == .alertFirstButtonReturn { action(field.stringValue.trimmingCharacters(in: .whitespaces)) }
        }
    }

    // MARK: agents

    static func close(_ a: Agent, model: BoardModel) {
        let alert = NSAlert()
        alert.messageText = "Close \(a.display)?"
        alert.informativeText = """
        The agent is asked to land its work and close itself
        (a "close" message from you, relayed by agent-board through agent-send).

        If it has unlanded work, it emails you to ask whether to land or discard it.
        """
        alert.addButton(withTitle: "Close")
        alert.addButton(withTitle: "Cancel")
        present(alert) { r in
            guard r == .alertFirstButtonReturn else { return }
            Actions.run(["action": "close", "id": a.id]) { res in
                model.show(res.ok ? "Asked \(a.display) to close." : "Close failed: \(res.message)")
            }
        }
    }

    static func forceClose(_ a: Agent, model: BoardModel) {
        confirmAfterCheck(
            model: model, checking: "Checking what a force close of \(a.display) would discard…",
            check: ["action": "check_close", "id": a.id], title: "Force close \(a.display)?",
            text: { said in
                """
                This runs agent-close --force. Uncommitted and unmerged work in its worktree is DISCARDED, \
                and its session ends immediately.

                agent-close --dry-run says:
                \(said)

                Type the agent's name to confirm: \(a.display)
                """
            },
            button: "Force close", typeBack: a.display, request: ["action": "force_close", "id": a.id],
            done: "Force-closed \(a.display).", failed: "Force close failed: ", refresh: false)
    }

    /// The everyday way to tidy away a dead agent: plain agent-close, which never discards anything.
    static func cleanup(_ a: Agent, model: BoardModel) {
        confirmAfterCheck(
            model: model, checking: "Checking what cleaning up \(a.display) would do…",
            check: ["action": "check_close", "id": a.id], title: "Clean up \(a.display)?",
            text: { said in
                """
                This runs agent-close without --force. It removes the agent's linked worktree and branch only if \
                all of its work is landed on main. If anything is uncommitted or unmerged, it refuses and nothing \
                changes. A main checkout is never removed.

                agent-close --dry-run says:
                \(said)
                """
            },
            button: "Clean up", typeBack: nil, request: ["action": "cleanup", "id": a.id],
            done: "Cleaned up \(a.display).", failed: "Not cleaned up: ", refresh: false)
    }

    // MARK: worktrees with no agent

    static func wtCleanup(_ w: FreeWorktree, model: BoardModel) {
        confirmAfterCheck(
            model: model, checking: "Checking what cleaning up \(w.name) would do…",
            check: ["action": "wt_check", "path": w.path], title: "Clean up worktree \(w.name)?",
            text: { said in
                """
                This removes the worktree and its branch only if all of its work is landed on main, using \
                agent-close's own checks. If anything is uncommitted or unmerged, it refuses and nothing changes.

                \(said)
                """
            },
            button: "Clean up", typeBack: nil, request: ["action": "wt_cleanup", "path": w.path],
            done: "Removed \(w.name).", failed: "Not removed: ", refresh: true)
    }

    static func wtForce(_ w: FreeWorktree, model: BoardModel) {
        confirmAfterCheck(
            model: model, checking: "Checking what force removing \(w.name) would discard…",
            check: ["action": "wt_check", "path": w.path], title: "Force remove worktree \(w.name)?",
            text: { said in
                """
                Uncommitted and unmerged work in it is DISCARDED, with its branch.

                \(said)

                Type the worktree's name to confirm: \(w.name)
                """
            },
            button: "Force remove", typeBack: w.name, request: ["action": "wt_force", "path": w.path],
            done: "Force-removed \(w.name).", failed: "Force remove failed: ", refresh: true)
    }

    /// "Clean up all fully landed worktrees": agent-close's dry run on every agent-less worktree, a confirmation
    /// listing exactly what goes and what stays, then a plain clean up of each (never --force).
    static func cleanupLanded(model: BoardModel) {
        model.show("Checking every worktree with agent-close's dry run (fetching each repo)…")
        Actions.runJSON(["action": "wt_landed_check"]) { obj, err in
            guard let cands = obj?["candidates"] as? [[String: Any]] else {
                model.show("Check failed: \(err ?? (obj?["error"] as? String) ?? "no answer")")
                return
            }
            let go = cands.filter { ($0["removable"] as? Bool) == true }
            let stay = cands.filter { ($0["removable"] as? Bool) != true }
            func names(_ xs: [[String: Any]], _ limit: Int) -> String {
                let n = xs.prefix(limit).map { "• " + (($0["name"] as? String) ?? "?") }.joined(separator: "\n")
                return xs.count > limit ? n + "\n… and \(xs.count - limit) more" : n
            }
            let alert = NSAlert()
            if go.isEmpty {
                alert.messageText = "Nothing to clean up"
                alert.informativeText = "None of the \(cands.count) worktrees with no agent is fully landed on main, so agent-close would keep them all."
                alert.addButton(withTitle: "OK")
                present(alert) { _ in }
                return
            }
            alert.messageText = "Remove \(go.count) fully landed worktree\(go.count == 1 ? "" : "s")?"
            alert.informativeText = """
            agent-close's dry run says these are fully landed on main (squash merges count), so each worktree \
            and its branch can go:

            \(names(go, 20))

            \(stay.count) other\(stay.count == 1 ? "" : "s") with uncommitted or unmerged work stay. Each removal is a \
            plain clean up that checks again first; nothing is forced.
            """
            alert.addButton(withTitle: "Remove \(go.count)")
            alert.addButton(withTitle: "Cancel")
            present(alert) { r in
                guard r == .alertFirstButtonReturn else { return }
                model.show("Removing \(go.count) worktrees…")
                let paths = go.compactMap { $0["path"] as? String }
                Actions.runJSON(["action": "wt_landed_cleanup", "paths": paths]) { res, err in
                    let removed = res?["removed"] as? Int ?? 0
                    let kept = res?["kept"] as? Int ?? 0
                    model.show(res == nil ? "Clean up failed: \(err ?? "no answer")"
                               : "Removed \(removed) worktree\(removed == 1 ? "" : "s")" + (kept > 0 ? "; \(kept) kept" : "") + ".")
                    model.refreshAll()
                    let failed = (res?["results"] as? [[String: Any]] ?? []).filter { ($0["ok"] as? Bool) != true }
                    if !failed.isEmpty {
                        let a = NSAlert()
                        a.messageText = "\(failed.count) kept"
                        a.informativeText = failed.prefix(12).map { "• \(($0["name"] as? String) ?? "?"): \(($0["detail"] as? String) ?? "")" }
                            .joined(separator: "\n")
                        a.addButton(withTitle: "OK")
                        present(a) { _ in }
                    }
                }
            }
        }
    }

    static func wtSpawn(_ w: FreeWorktree, model: BoardModel) {
        let logins = model.snapshot?.spawnLogins ?? []
        let (row, picker) = loginRow(logins, width: 420)
        let info = """
        A new agent starts in this worktree (agent-spawn --dir), with your instructions and the worktree's path, \
        branch and git state. E.g. "land what's worth keeping, then clean this up". ⌘↩ starts it.
        """
        compose(title: "Start an agent in \(w.name)", info: info, width: 420, height: 140, button: "Start agent",
                optional: false, files: nil, above: logins.isEmpty ? nil : row) { body in
            var req: [String: Any] = ["action": "wt_spawn", "path": w.path, "text": body]
            let login = picker.selectedItem?.representedObject as? SpawnLogin
            if let login = login { req["account"] = login.name }
            model.show("Starting an agent in \(w.name)" + (login.map { " on \($0.label)" } ?? "") + "…")
            Actions.run(req) { res in
                model.show(res.ok ? "Agent started in \(w.name); its tile appears in a moment." : "Start failed: \(res.message)")
                if res.ok { model.refreshAll() }
            }
        }
    }

    /// The start dialog's login: "Automatic" (agent-spawn picks one by usage), then each login a new agent can be
    /// started on by name (the snapshot's spawnLogins, such as a model through OpenRouter), its item's
    /// representedObject the SpawnLogin (Automatic's none). Items are added to the menu itself, which keeps two of the
    /// same label apart.
    static func loginRow(_ logins: [SpawnLogin], width: CGFloat) -> (NSView, NSPopUpButton) {
        let row = NSView(frame: NSRect(x: 0, y: 0, width: width, height: 26))
        let label = NSTextField(labelWithString: "Login")
        label.frame = NSRect(x: 0, y: 5, width: 44, height: 16)
        let picker = NSPopUpButton(frame: NSRect(x: 48, y: 0, width: width - 48, height: 26), pullsDown: false)
        picker.menu?.addItem(NSMenuItem(title: "Automatic (by usage)", action: nil, keyEquivalent: ""))
        for login in logins {
            let item = NSMenuItem(title: login.label, action: nil, keyEquivalent: "")
            item.representedObject = login
            picker.menu?.addItem(item)
        }
        picker.selectItem(at: 0)
        [label, picker].forEach(row.addSubview)
        return (row, picker)
    }

    /// Like an email to the orchestrator: it can start a new agent, pass something on to one, or answer a question.
    static func orchestrator(model: BoardModel) {
        let info = """
        Works like an email to it: ask it to start a new agent ("spin up an agent on myrepo to …"), pass \
        something to one, or answer a question. It runs once and answers you by email. Paste or drop images and \
        files on the text, or attach them: it gets them, and can pass them on. ⌘↩ sends.
        """
        let files = MessageFiles()
        compose(title: "Message the orchestrator", info: info, width: 420, height: 150, button: "Send",
                optional: false, files: files) { body in
            // "mac": its key in the desktop's actions.ORCH_SOURCES, so the message's header says it came from here;
            // the message's idempotency key is a post id (newPostID), as a reply's is (Compose.swift)
            var req: [String: Any] = ["action": "orchestrate", "text": body, "source": "mac", "key": newPostID()]
            if !files.files.isEmpty {
                req["files"] = files.request
                model.show("Sending to the orchestrator with \(files.summary)…")
            }
            Actions.run(req) { res in
                model.show(res.ok ? "Sent to the orchestrator; it will email you." : "Not sent: \(res.message)")
            }
        }
    }

    /// Start an exited agent again: a new tmux session in its folder, the same conversation, model and login.
    static func resume(_ a: Agent, model: BoardModel) {
        let info = """
        It starts again in a new tmux session in \((a.cwd as NSString).lastPathComponent), continuing the same \
        conversation with the same \(a.cli == "codex" ? "model and effort" : "login, model and effort"). It's told it was \
        resumed, and about any mail you sent while it was down. Optional note for it (⌘↩ resumes):
        """
        compose(title: "Resume \(a.display)?", info: info, width: 420, height: 90, button: "Resume",
                optional: true, files: nil) { note in
            model.show("Resuming \(a.display)…")
            var req: [String: Any] = ["action": "resume", "id": a.id]
            if !note.isEmpty { req["text"] = note }
            Actions.run(req) { res in
                model.show(res.ok ? res.message : "Resume: \(res.message)")
                model.refreshAll()
            }
        }
    }

    static func send(_ a: Agent, model: BoardModel) {
        let files = MessageFiles()
        compose(title: "Message \(a.display)",
                info: "Delivered as a message from you, relayed by agent-board. Paste or drop images and files on the "
                    + "text, or attach them: they are saved on the desktop for it to open. ⌘↩ sends.",
                width: 380, height: 120, button: "Send", optional: false, files: files) { body in
            var req: [String: Any] = ["action": "send", "id": a.id, "text": body]
            if !files.files.isEmpty {
                req["files"] = files.request
                model.show("Sending to \(a.display) with \(files.summary)…")
            }
            Actions.run(req) { res in
                model.show(res.ok ? "Sent to \(a.display)." : "Send failed: \(res.message)")
            }
        }
    }

    // MARK: groups and settings

    static func rename(group: TileGroup, model: BoardModel) {
        askLine(title: "Rename group", info: "", initial: group.name, placeholder: nil, button: "Rename") { name in
            model.renameGroup(group.id, to: name)
        }
    }

    static func newGroup(model: BoardModel, with id: String? = nil, parent: UUID? = nil) {
        let title = parent == nil ? "New group" : "New sub-group in \(model.layout.find(parent!)?.name ?? "group")"
        askLine(title: title, info: "", initial: "", placeholder: "e.g. Work, Personal, Experiments", button: "Create") { name in
            model.newGroup(name: name.isEmpty ? nil : name, with: id, parent: parent)
        }
    }

    static func host(model: BoardModel) {
        askLine(title: "Desktop SSH host",
                info: "An alias from ~/.ssh/config that logs in without a prompt. Collector path on the desktop: \(Settings.collector)",
                initial: Settings.host, placeholder: nil, button: "Save") { h in
            guard !h.isEmpty, !h.hasPrefix("-") else { return }
            Settings.host = h
            model.reconnect()
        }
    }
}
