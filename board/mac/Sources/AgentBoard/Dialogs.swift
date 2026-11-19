import AppKit

/// A plain-text box for dialogs where ⌘↩ (Return or keypad Enter) presses the dialog's main button;
/// plain Return still makes a new line.
final class SubmitTextView: NSTextView {
    var onSubmit: (() -> Void)?

    override func keyDown(with event: NSEvent) {
        if event.modifierFlags.intersection([.command, .option, .control, .shift]) == .command,
           event.keyCode == 36 || event.keyCode == 76 {
            onSubmit?()
            return
        }
        super.keyDown(with: event)
    }
}

@MainActor
func textArea(width: CGFloat, height: CGFloat) -> (NSScrollView, SubmitTextView) {
    let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: width, height: height))
    scroll.hasVerticalScroller = true
    scroll.borderType = .bezelBorder
    let text = SubmitTextView(frame: scroll.bounds)
    text.isRichText = false
    text.font = .systemFont(ofSize: 13)
    text.autoresizingMask = [.width]
    text.isAutomaticQuoteSubstitutionEnabled = false
    scroll.documentView = text
    return (scroll, text)
}

/// Make ⌘↩ press this button (it is the alert's main one) and wire the text box to it.
@MainActor
func sendOnCommandEnter(_ button: NSButton, _ text: SubmitTextView) {
    button.keyEquivalent = "\r"
    button.keyEquivalentModifierMask = [.command]
    text.onSubmit = { [weak button] in button?.performClick(nil) }
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
    /// nothing unless `optional`.
    private static func compose(title: String, info: String, width: CGFloat, height: CGFloat, button: String,
                                optional: Bool, action: @escaping (String) -> Void) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = info
        let (scroll, text) = textArea(width: width, height: height)
        alert.accessoryView = scroll
        sendOnCommandEnter(alert.addButton(withTitle: button), text)
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = text
        present(alert) { r in
            let body = text.string.trimmingCharacters(in: .whitespacesAndNewlines)
            guard r == .alertFirstButtonReturn, optional || !body.isEmpty else { return }
            action(body)
        }
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
        let info = """
        A new agent starts in this worktree (agent-spawn --dir, login picked by usage), with your instructions \
        and the worktree's path, branch and git state. E.g. "land what's worth keeping, then clean this up". ⌘↩ starts it.
        """
        compose(title: "Start an agent in \(w.name)", info: info, width: 420, height: 140, button: "Start agent",
                optional: false) { body in
            model.show("Starting an agent in \(w.name)…")
            Actions.run(["action": "wt_spawn", "path": w.path, "text": body]) { res in
                model.show(res.ok ? "Agent started in \(w.name); its tile appears in a moment." : "Start failed: \(res.message)")
                if res.ok { model.refreshAll() }
            }
        }
    }

    /// Like an email to the orchestrator: it can start a new agent, pass something on to one, or answer a question.
    static func orchestrator(model: BoardModel) {
        let info = """
        Works like an email to it: ask it to start a new agent ("spin up an agent on myrepo to …"), pass \
        something to one, or answer a question. It runs once and answers you by email. ⌘↩ sends.
        """
        compose(title: "Message the orchestrator", info: info, width: 420, height: 150, button: "Send",
                optional: false) { body in
            Actions.run(["action": "orchestrate", "text": body]) { res in
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
                optional: true) { note in
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
        compose(title: "Message \(a.display)",
                info: "Delivered as a message from you, relayed by agent-board. ⌘↩ sends.",
                width: 380, height: 120, button: "Send", optional: false) { body in
            Actions.run(["action": "send", "id": a.id, "text": body]) { res in
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
