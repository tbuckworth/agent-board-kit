import SwiftUI

/// Change an agent's CLI / Claude login, or just its model and effort, through the
/// mailroom's agent-switch. An idle agent switches at once; a busy one only after "Interrupt and switch".
struct SwitchSheet: View {
    let session: SwitchSession
    @ObservedObject var model: BoardModel
    var preloaded: SwitchOptions? = nil          // offscreen renders: no network
    @State private var options: SwitchOptions?
    @State private var error: String?
    @State private var target = ""
    @State private var modelChoice = ""
    @State private var effortChoice = ""
    @State private var running = false
    @State private var mustInterrupt = false

    var agent: Agent { session.agent }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text((session.modelOnly ? "Change model / effort: " : "Change agent: ") + agent.display).font(.headline)
            if let o = options {
                Text("Now: " + [o.agent.cli, o.agent.account ?? "", o.agent.model ?? "", o.agent.effort ?? ""]
                    .filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.system(size: 12)).foregroundStyle(.secondary)
                if o.agent.movable == false {
                    Text("Can't be switched: \(o.agent.whyNot ?? "unknown reason")").foregroundColor(AgentState.blocked.color)
                }
                if !session.modelOnly { targetList(o) }
                pickers(o)
                notes(o)
            } else if let e = error {
                Text(e).foregroundColor(AgentState.blocked.color).fixedSize(horizontal: false, vertical: true)
            } else {
                ProgressView("Reading the options…").controlSize(.small)
            }
            if running {
                ProgressView(selectedTarget?.kind == "handoff" ? "Handing off… this can take up to a minute" : "Switching…")
                    .controlSize(.small)
            }
            HStack {
                Spacer()
                Button("Cancel") { model.switching = nil }.keyboardShortcut(.cancelAction)
                Button(interrupting ? "Interrupt and switch" : "Switch") { apply() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!canApply)
            }
        }
        .padding(18)
        .frame(width: 480)
        .onAppear(perform: load)
    }

    // MARK: pieces

    func targetList(_ o: SwitchOptions) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Run it on").font(.system(size: 12, weight: .semibold))
            ForEach(o.targets) { t in
                Button {
                    if target != t.target { target = t.target; modelChoice = ""; effortChoice = "" }
                } label: {
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: target == t.target ? "largecircle.fill.circle" : "circle")
                        VStack(alignment: .leading, spacing: 1) {
                            Text(t.label + ((t.current ?? false) ? "  (now)" : "")).font(.system(size: 12, weight: .medium))
                            Text([t.usage ?? "", kindText(t.kind)].filter { !$0.isEmpty }.joined(separator: " · "))
                                .font(.system(size: 10)).foregroundStyle(.secondary)
                        }
                        Spacer()
                        statusBadge(t.status)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(!(t.available ?? true) || o.agent.movable == false)
                .opacity((t.available ?? true) ? 1 : 0.45)
            }
        }
    }

    func pickers(_ o: SwitchOptions) -> some View {
        let cli = selectedTarget?.cli ?? o.agent.cli
        let models = o.models[cli] ?? []
        let sameCLI = cli == o.agent.cli
        let def = o.defaults?[cli]
        let keepModel = sameCLI ? "Keep current (\(o.agent.model ?? "?"))" : "Default (\(def?.model ?? "?"))"
        let keepEffort = sameCLI ? "Keep current (\(o.agent.effort ?? "?"))" : "Default (\(def?.effort ?? "?"))"
        let efforts = effortList(models: models, current: sameCLI ? o.agent.model : def?.model)
        return VStack(alignment: .leading, spacing: 6) {
            Picker("Model", selection: $modelChoice) {
                Text(keepModel).tag("")
                ForEach(models) { m in Text(m.label).tag(m.value) }
            }
            .onChange(of: modelChoice) { _ in if !efforts.contains(effortChoice) { effortChoice = "" } }
            Picker("Effort", selection: $effortChoice) {
                Text(keepEffort).tag("")
                ForEach(efforts, id: \.self) { e in Text(e).tag(e) }
            }
            .disabled(efforts.isEmpty)
        }
        .disabled(o.agent.movable == false)
    }

    func notes(_ o: SwitchOptions) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            if interrupting {
                Label("It's mid-turn: switching will interrupt it (it's told why).", systemImage: "exclamationmark.triangle.fill")
                    .foregroundColor(Palette.attention).font(.system(size: 12))
            }
            if selectedTarget?.kind == "handoff" {
                Text("Claude Code ↔ Codex is a handoff: a new session starts in the same tmux pane and folder, reads a handoff record (its brief + the condensed conversation), and its mail routing moves over. Same name on the board.")
                    .font(.system(size: 10)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if let st = selectedTarget?.status, st == "low" || st == "out" {
                Text("That account is \(st == "out" ? "out of usage" : "low on usage").")
                    .foregroundColor(Palette.attention).font(.system(size: 11))
            }
            if (o.agent.backgroundJobs ?? 0) > 0 {
                Text("It has \(o.agent.backgroundJobs ?? 0) background job(s) running.")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
            }
        }
    }

    func statusBadge(_ status: String?) -> some View {
        let s = status ?? "unknown"
        let color: Color = s == "ok" ? AgentState.waiting.color : (s == "low" ? Palette.attention : (s == "out" ? AgentState.blocked.color : .secondary))
        return Text(s).font(.system(size: 9, weight: .bold)).foregroundColor(.white)
            .padding(.horizontal, 5).padding(.vertical, 1).background(Capsule().fill(color))
    }

    func kindText(_ kind: String?) -> String {
        switch kind ?? "" {
        case "resume": return "keeps the conversation"
        case "handoff": return "hands off to a new session"
        default: return ""
        }
    }

    func effortList(models: [SwitchModel], current: String?) -> [String] {
        if !modelChoice.isEmpty { return models.first { $0.value == modelChoice }?.efforts ?? [] }
        if let cur = current, let m = models.first(where: { $0.value == cur }) { return m.efforts ?? [] }
        var seen: [String] = []
        for m in models { for e in m.efforts ?? [] where !seen.contains(e) { seen.append(e) } }
        return seen
    }

    // MARK: state

    var selectedTarget: SwitchTarget? { options?.targets.first { $0.target == target } }
    var interrupting: Bool { mustInterrupt || (options?.willInterrupt ?? false) }

    var changes: Bool {
        guard let o = options else { return false }
        let moving = !session.modelOnly && !(selectedTarget?.current ?? true)
        return (moving || !modelChoice.isEmpty || !effortChoice.isEmpty) && o.agent.movable != false
    }

    var canApply: Bool { changes && !running && (selectedTarget?.available ?? true) }

    func load() {
        if let o = preloaded {
            options = o
            target = o.targets.first { $0.current ?? false }?.target ?? ""
            return
        }
        Actions.runJSON(["action": "switch_options", "id": agent.id]) { obj, err in
            guard let obj = obj, (obj["ok"] as? Bool) == true, let raw = obj["options"],
                  let data = try? JSONSerialization.data(withJSONObject: raw) else {
                error = (obj?["error"] as? String) ?? err ?? "could not read the options"
                return
            }
            do {
                let o = try JSON.decoder.decode(SwitchOptions.self, from: data)
                options = o
                target = o.targets.first { $0.current ?? false }?.target ?? ""
            } catch {
                self.error = "options not understood: \(error)"
            }
        }
    }

    func apply() {
        var req: [String: Any] = ["action": "switch", "id": agent.id, "force": interrupting]
        if !session.modelOnly, let t = selectedTarget, !(t.current ?? false) { req["to"] = t.target }
        if !modelChoice.isEmpty { req["model"] = modelChoice }
        if !effortChoice.isEmpty { req["effort"] = effortChoice }
        running = true
        error = nil
        Actions.runJSON(req) { obj, err in
            running = false
            guard let obj = obj else { error = err ?? "no answer"; return }
            if (obj["needs_force"] as? Bool) == true && !interrupting {
                mustInterrupt = true          // it became busy meanwhile: ask again, explicitly
                return
            }
            let detail = (obj["output"] as? String) ?? (obj["error"] as? String) ?? ""
            if (obj["ok"] as? Bool) == true {
                model.switching = nil
                model.show("Switched \(agent.display)" + (detail.isEmpty ? "." : ": \(detail)"))
                model.refreshAll()
            } else {
                error = detail.isEmpty ? "the switch failed" : detail
                options = options      // keep the sheet open with the reason
            }
        }
    }
}


/// Decode the desktop's answer to switch_options (shared by the sheet and the probe).
func decodeSwitchOptions(_ obj: [String: Any]?) -> SwitchOptions? {
    guard let raw = obj?["options"], let data = try? JSONSerialization.data(withJSONObject: raw) else { return nil }
    return try? JSON.decoder.decode(SwitchOptions.self, from: data)
}
