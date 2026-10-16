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

    private static func present(_ alert: NSAlert, _ done: @escaping (NSApplication.ModalResponse) -> Void) {
        if let w = window, w.isVisible {
            alert.beginSheetModal(for: w, completionHandler: done)
        } else {
            done(alert.runModal())
        }
    }

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
        model.show("Checking what a force close of \(a.display) would discard…")
        Actions.run(["action": "check_close", "id": a.id]) { check in
            let alert = NSAlert()
            alert.alertStyle = .critical
            alert.messageText = "Force close \(a.display)?"
            alert.informativeText = """
            This runs agent-close --force. Uncommitted and unmerged work in its worktree is DISCARDED, \
            and its session ends immediately.

            agent-close --dry-run says:
            \(check.message.isEmpty ? "(no answer)" : String(check.message.suffix(900)))

            Type the agent's name to confirm: \(a.display)
            """
            let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
            field.placeholderString = a.display
            alert.accessoryView = field
            let button = alert.addButton(withTitle: "Force close")
            button.hasDestructiveAction = true
            alert.addButton(withTitle: "Cancel")
            alert.window.initialFirstResponder = field
            present(alert) { r in
                guard r == .alertFirstButtonReturn else { return }
                guard field.stringValue == a.display else {
                    model.show("Name didn't match; nothing was done.")
                    return
                }
                Actions.run(["action": "force_close", "id": a.id, "confirm": field.stringValue]) { res in
                    model.show(res.ok ? "Force-closed \(a.display)." : "Force close failed: \(res.message)")
                }
            }
        }
    }

    /// The everyday way to tidy away a dead agent: plain agent-close, which never discards anything.
    static func cleanup(_ a: Agent, model: BoardModel) {
        model.show("Checking what cleaning up \(a.display) would do…")
        Actions.run(["action": "check_close", "id": a.id]) { check in
            let alert = NSAlert()
            alert.messageText = "Clean up \(a.display)?"
            alert.informativeText = """
            This runs agent-close without --force. It removes the agent's linked worktree and branch only if \
            all of its work is landed on main. If anything is uncommitted or unmerged, it refuses and nothing \
            changes. A main checkout is never removed.

            agent-close --dry-run says:
            \(check.message.isEmpty ? "(no answer)" : String(check.message.suffix(900)))
            """
            alert.addButton(withTitle: "Clean up")
            alert.addButton(withTitle: "Cancel")
            present(alert) { r in
                guard r == .alertFirstButtonReturn else { return }
                Actions.run(["action": "cleanup", "id": a.id]) { res in
                    model.show(res.ok ? "Cleaned up \(a.display)." : "Not cleaned up: \(res.message)")
                }
            }
        }
    }

    // MARK: worktrees with no agent

    static func wtCleanup(_ w: FreeWorktree, model: BoardModel) {
        model.show("Checking what cleaning up \(w.name) would do…")
        Actions.run(["action": "wt_check", "path": w.path]) { check in
            let alert = NSAlert()
            alert.messageText = "Clean up worktree \(w.name)?"
            alert.informativeText = """
            This removes the worktree and its branch only if all of its work is landed on main, using \
            agent-close's own checks. If anything is uncommitted or unmerged, it refuses and nothing changes.

            \(check.message.isEmpty ? "(no answer)" : String(check.message.suffix(900)))
            """
            alert.addButton(withTitle: "Clean up")
            alert.addButton(withTitle: "Cancel")
            present(alert) { r in
                guard r == .alertFirstButtonReturn else { return }
                Actions.run(["action": "wt_cleanup", "path": w.path]) { res in
                    model.show(res.ok ? "Removed \(w.name)." : "Not removed: \(res.message)")
                    if res.ok { model.refreshAll() }
                }
            }
        }
    }

    static func wtForce(_ w: FreeWorktree, model: BoardModel) {
        model.show("Checking what force removing \(w.name) would discard…")
        Actions.run(["action": "wt_check", "path": w.path]) { check in
            let alert = NSAlert()
            alert.alertStyle = .critical
            alert.messageText = "Force remove worktree \(w.name)?"
            alert.informativeText = """
            Uncommitted and unmerged work in it is DISCARDED, with its branch.

            \(check.message.isEmpty ? "(no answer)" : String(check.message.suffix(900)))

            Type the worktree's name to confirm: \(w.name)
            """
            let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
            field.placeholderString = w.name
            alert.accessoryView = field
            let button = alert.addButton(withTitle: "Force remove")
            button.hasDestructiveAction = true
            alert.addButton(withTitle: "Cancel")
            alert.window.initialFirstResponder = field
            present(alert) { r in
                guard r == .alertFirstButtonReturn else { return }
                guard field.stringValue == w.name else { model.show("Name didn't match; nothing was done."); return }
                Actions.run(["action": "wt_force", "path": w.path, "confirm": field.stringValue]) { res in
                    model.show(res.ok ? "Force-removed \(w.name)." : "Force remove failed: \(res.message)")
                    if res.ok { model.refreshAll() }
                }
            }
        }
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
        let alert = NSAlert()
        alert.messageText = "Start an agent in \(w.name)"
        alert.informativeText = """
        A new agent starts in this worktree (agent-spawn --dir, login picked by usage), with your instructions \
        and the worktree's path, branch and git state. E.g. "land what's worth keeping, then clean this up". ⌘↩ starts it.
        """
        let (scroll, text) = textArea(width: 420, height: 140)
        alert.accessoryView = scroll
        sendOnCommandEnter(alert.addButton(withTitle: "Start agent"), text)
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = text
        present(alert) { r in
            let body = text.string.trimmingCharacters(in: .whitespacesAndNewlines)
            guard r == .alertFirstButtonReturn, !body.isEmpty else { return }
            model.show("Starting an agent in \(w.name)…")
            Actions.run(["action": "wt_spawn", "path": w.path, "text": body]) { res in
                model.show(res.ok ? "Agent started in \(w.name); its tile appears in a moment." : "Start failed: \(res.message)")
                if res.ok { model.refreshAll() }
            }
        }
    }

    /// Like an email to the orchestrator: it can start a new agent, pass something on to one, or answer a question.
    static func orchestrator(model: BoardModel) {
        let alert = NSAlert()
        alert.messageText = "Message the orchestrator"
        alert.informativeText = """
        Works like an email to it: ask it to start a new agent ("spin up an agent on myrepo to …"), pass \
        something to one, or answer a question. It runs once and answers you by email. ⌘↩ sends.
        """
        let (scroll, text) = textArea(width: 420, height: 150)
        alert.accessoryView = scroll
        sendOnCommandEnter(alert.addButton(withTitle: "Send"), text)
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = text
        present(alert) { r in
            let body = text.string.trimmingCharacters(in: .whitespacesAndNewlines)
            guard r == .alertFirstButtonReturn, !body.isEmpty else { return }
            Actions.run(["action": "orchestrate", "text": body]) { res in
                model.show(res.ok ? "Sent to the orchestrator; it will email you." : "Not sent: \(res.message)")
            }
        }
    }

    /// Start an exited agent again: a new tmux session in its folder, the same conversation, model and login.
    static func resume(_ a: Agent, model: BoardModel) {
        let alert = NSAlert()
        alert.messageText = "Resume \(a.display)?"
        alert.informativeText = """
        It starts again in a new tmux session in \((a.cwd as NSString).lastPathComponent), continuing the same \
        conversation with the same \(a.cli == "codex" ? "model and effort" : "login, model and effort"). It's told it was \
        resumed, and about any mail you sent while it was down. Optional note for it (⌘↩ resumes):
        """
        let (scroll, text) = textArea(width: 420, height: 90)
        alert.accessoryView = scroll
        sendOnCommandEnter(alert.addButton(withTitle: "Resume"), text)
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = text
        present(alert) { r in
            guard r == .alertFirstButtonReturn else { return }
            let note = text.string.trimmingCharacters(in: .whitespacesAndNewlines)
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
        let alert = NSAlert()
        alert.messageText = "Message \(a.display)"
        alert.informativeText = "Delivered as a message from you, relayed by agent-board. ⌘↩ sends."
        let (scroll, text) = textArea(width: 380, height: 120)
        alert.accessoryView = scroll
        sendOnCommandEnter(alert.addButton(withTitle: "Send"), text)
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = text
        present(alert) { r in
            let body = text.string.trimmingCharacters(in: .whitespacesAndNewlines)
            guard r == .alertFirstButtonReturn, !body.isEmpty else { return }
            Actions.run(["action": "send", "id": a.id, "text": body]) { res in
                model.show(res.ok ? "Sent to \(a.display)." : "Send failed: \(res.message)")
            }
        }
    }

    static func rename(group: TileGroup, model: BoardModel) {
        let alert = NSAlert()
        alert.messageText = "Rename group"
        let field = NSTextField(string: group.name)
        field.frame = NSRect(x: 0, y: 0, width: 260, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: "Rename")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        present(alert) { r in
            if r == .alertFirstButtonReturn { model.renameGroup(group.id, to: field.stringValue) }
        }
    }

    static func newGroup(model: BoardModel, with id: String? = nil, parent: UUID? = nil) {
        let alert = NSAlert()
        alert.messageText = parent == nil ? "New group" : "New sub-group in \(model.layout.find(parent!)?.name ?? "group")"
        let field = NSTextField(string: "")
        field.placeholderString = "e.g. Work, Personal, Experiments"
        field.frame = NSRect(x: 0, y: 0, width: 260, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: "Create")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        present(alert) { r in
            guard r == .alertFirstButtonReturn else { return }
            let name = field.stringValue.trimmingCharacters(in: .whitespaces)
            model.newGroup(name: name.isEmpty ? nil : name, with: id, parent: parent)
        }
    }

    static func host(model: BoardModel) {
        let alert = NSAlert()
        alert.messageText = "Desktop SSH host"
        alert.informativeText = "An alias from ~/.ssh/config that logs in without a prompt. Collector path on the desktop: \(Settings.collector)"
        let field = NSTextField(string: Settings.host)
        field.frame = NSRect(x: 0, y: 0, width: 260, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        present(alert) { r in
            let h = field.stringValue.trimmingCharacters(in: .whitespaces)
            guard r == .alertFirstButtonReturn, !h.isEmpty, !h.hasPrefix("-") else { return }
            Settings.host = h
            model.reconnect()
        }
    }
}
