import AppKit
import SwiftUI

// The desktop's pending updates, and a restart that brings every agent back (agentboard/restart.py).

struct DesktopUpdates: Decodable {
    var total: Int?
    var security: Int?
    var rebootRequired: Bool?
    var rebootSince: Double?
    var kernel: String?
    var newestKernel: String?
    var gpuOk: Bool?
    var gpuMessage: String?
    var snaps: [String]?
    var highlights: [String]?
}

struct RestartStatus: Decodable {
    var status: String?
    var message: String?
    var restoredAt: Double?
    var active: Bool { ["pending", "waiting", "updating", "rebooting", "restoring"].contains(status ?? "") }
}

struct PlanWait: Decodable, Hashable { var what: String?; var command: String? }
struct PlanProcess: Decodable, Hashable { var pid: Int; var cmd: String }

struct PlanAgent: Decodable, Identifiable {
    var id: String
    var cli: String
    var display: String
    var state: String
    var reason: String?
    var lastNote: Note?
    var waits: [PlanWait]?
    var processes: [PlanProcess]?
    var choice: String
    var model: String?
    var effort: String?
    var account: String?
}

struct RestartPlan: Decodable {
    var updates: DesktopUpdates?
    var agents: [PlanAgent]
    var warnings: [String]
    var status: RestartStatus?

    enum CodingKeys: String, CodingKey { case updates, agents, warnings, status }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        updates = try? c.decodeIfPresent(DesktopUpdates.self, forKey: .updates)
        agents = c.lossyArray(PlanAgent.self, forKey: .agents)
        warnings = (try? c.decodeIfPresent([String].self, forKey: .warnings)) ?? []
        status = try? c.decodeIfPresent(RestartStatus.self, forKey: .status)
    }
}

extension DesktopUpdates {
    var summary: String {
        var parts: [String] = []
        if let t = total { parts.append(t == 0 ? "up to date" : "\(t) update\(t == 1 ? "" : "s")") }
        if let s = security, s > 0 { parts.append("\(s) security") }
        if rebootRequired == true { parts.append("reboot needed") }
        return parts.isEmpty ? "checking…" : parts.joined(separator: " · ")
    }
    var color: Color {
        if (security ?? 0) > 0 { return AgentState.blocked.color }
        if rebootRequired == true || gpuOk == false { return Palette.attention }
        return .secondary
    }
}

/// The top-right chip: pending updates, or the restart in progress.
struct DesktopChip: View {
    @ObservedObject var model: BoardModel
    @Environment(\.zoom) private var z

    var body: some View {
        let st = model.snapshot?.restart
        Button { model.showRestart = true } label: {
            HStack(spacing: 4) {
                if st?.active == true {
                    ProgressView().controlSize(.mini)
                    Text("Restart: " + (st?.message ?? st?.status ?? "")).lineLimit(1)
                } else if let u = model.snapshot?.desktop {
                    Image(systemName: "arrow.triangle.2.circlepath")
                    Text("Desktop: " + u.summary)
                } else {
                    Image(systemName: "desktopcomputer")
                    Text("Desktop")
                }
            }
            .font(.system(size: 11 * z, weight: .medium))
            .foregroundColor(st?.active == true ? Palette.attention : (model.snapshot?.desktop?.color ?? .secondary))
        }
        .buttonStyle(.borderless)
        .frame(maxWidth: 360 * z)
        .help(helpText)
    }

    var helpText: String {
        guard let u = model.snapshot?.desktop else { return "Restart or update the desktop" }
        var lines = ["Kernel \(u.kernel ?? "?") → \(u.newestKernel ?? "?")"]
        if u.gpuOk == false { lines.append("GPU: " + (u.gpuMessage ?? "not working")) }
        let hi = u.highlights ?? []
        if !hi.isEmpty { lines.append("Including: " + hi.joined(separator: ", ")) }
        lines.append("Click to restart or update")
        return lines.joined(separator: "\n")
    }
}

struct RestartSheet: View {
    @ObservedObject var model: BoardModel
    var preloaded: RestartPlan? = nil            // offscreen renders: no network
    @State private var plan: RestartPlan?
    @State private var error: String?
    @State private var choices: [String: String] = [:]
    @State private var waitForBusy = true
    @State private var running = false

    var status: RestartStatus? { model.snapshot?.restart }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Restart the desktop").font(.headline)
            if let st = status, st.active {
                activeView(st)
            } else if let p = plan {
                if let e = error {                       // a failed run, with the plan still on screen
                    Label(e, systemImage: "exclamationmark.triangle.fill")
                        .foregroundColor(AgentState.blocked.color).font(.callout.weight(.semibold))
                }
                planView(p)
            } else if let e = error {
                Text(e).foregroundColor(AgentState.blocked.color)
            } else {
                ProgressView("Checking updates and every agent…").controlSize(.small)
            }
            HStack {
                if let st = status, ["pending", "waiting"].contains(st.status ?? "") {
                    Button("Cancel the restart") {
                        Actions.run(["action": "restart_cancel"]) { r in model.show(r.ok ? "Restart cancelled." : r.message) }
                    }
                }
                Spacer()
                Button("Close") { model.showRestart = false }.keyboardShortcut(.cancelAction)
                if status?.active != true, let p = plan {
                    Button("Restart") { confirm(update: false, p) }.disabled(running)
                    Button("Update & Restart") { confirm(update: true, p) }.disabled(running).keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(18)
        .frame(width: 720, height: 620)
        .onAppear(perform: load)
    }

    func activeView(_ st: RestartStatus) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack { ProgressView().controlSize(.small); Text((st.status ?? "").capitalized + ": " + (st.message ?? "")) }
            Text("The board reconnects by itself once the desktop is back, and you'll get an email summary.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
            Spacer()
        }
    }

    func planView(_ p: RestartPlan) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if let u = p.updates {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Updates: " + u.summary + (u.rebootSince.map { " since " + Fmt.clock($0) } ?? ""))
                        .font(.system(size: 12, weight: .semibold))
                    Text("Kernel \(u.kernel ?? "?") → \(u.newestKernel ?? "?")"
                         + ((u.highlights ?? []).isEmpty ? "" : " · including " + (u.highlights ?? []).prefix(4).joined(separator: ", "))
                         + ((u.snaps ?? []).isEmpty ? "" : " · snaps: " + (u.snaps ?? []).joined(separator: ", ")))
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                    if u.gpuOk == false {
                        Text("GPU: \(u.gpuMessage ?? "not working"). A reboot loads the matching driver.")
                            .font(.system(size: 11)).foregroundColor(Palette.attention)
                    }
                }
            }
            Text("Agents (\(p.agents.count)): what happens to each after the restart")
                .font(.system(size: 12, weight: .semibold))
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(p.agents) { a in agentRow(a) }
                }
            }
            .frame(maxHeight: .infinity)
            if !p.warnings.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(p.warnings, id: \.self) { w in
                        Text("⚠ " + w).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(2)
                    }
                }
            }
            Toggle("Give busy agents up to 15 minutes to reach a stopping point first (they're told the restart is coming)",
                   isOn: $waitForBusy).font(.system(size: 11))
        }
    }

    func agentRow(_ a: PlanAgent) -> some View {
        let st = AgentState(rawValue: a.state) ?? .idle
        return HStack(alignment: .top, spacing: 8) {
            RoundedRectangle(cornerRadius: 3).fill(tileColor(st)).frame(width: 12, height: 12).padding(.top, 2)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(a.display).font(.system(size: 12, weight: .semibold)).lineLimit(1)
                    Text(st.label + " · " + a.cli + (a.model.map { " · " + $0 } ?? "")).font(.system(size: 10)).foregroundStyle(.secondary)
                }
                if let n = a.lastNote?.text, !n.isEmpty {
                    Text(n).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                }
                ForEach(a.waits ?? [], id: \.self) { w in
                    Text("⏳ waiting on: " + (w.what ?? w.command ?? "")).font(.system(size: 10)).lineLimit(1)
                }
                if let ps = a.processes, !ps.isEmpty {
                    Text("⚠ \(ps.count) local process\(ps.count == 1 ? "" : "es") will be killed: " + ps.map { $0.cmd }.joined(separator: " · "))
                        .font(.system(size: 10)).foregroundColor(Palette.attention).lineLimit(2)
                        .help(ps.map { "\($0.pid): \($0.cmd)" }.joined(separator: "\n"))
                }
            }
            Spacer(minLength: 8)
            Picker("", selection: Binding(get: { choices[a.id] ?? a.choice }, set: { choices[a.id] = $0 })) {
                Text("Nudge").tag("nudge")
                Text("Quiet").tag("quiet")
                Text("Don't resume").tag("skip")
            }
            .pickerStyle(.segmented).labelsHidden().frame(width: 250)
            .help("Nudge: resumed and told what it was doing. Quiet: resumed, left idle. Don't resume: stays closed.")
        }
    }

    func tileColor(_ s: AgentState) -> Color { s == .offline ? .secondary : s.color }

    func load() {
        if let p = preloaded { plan = p; return }
        Actions.runJSON(["action": "restart_plan"]) { obj, err in
            guard let raw = obj?["plan"], let data = try? JSONSerialization.data(withJSONObject: raw) else {
                error = (obj?["error"] as? String) ?? err ?? "could not read the plan"
                return
            }
            do { plan = try JSON.decoder.decode(RestartPlan.self, from: data) } catch { self.error = "plan not understood: \(error)" }
        }
    }

    func confirm(update: Bool, _ p: RestartPlan) {
        let final = Dictionary(uniqueKeysWithValues: p.agents.map { ($0.id, choices[$0.id] ?? $0.choice) })
        let n = { (c: String) in final.values.filter { $0 == c }.count }
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = update ? "Update and restart the desktop now?" : "Restart the desktop now?"
        alert.informativeText = """
        \(n("nudge") + n("quiet")) agents will be resumed afterwards (\(n("nudge")) nudged, \(n("quiet")) quietly)\
        \(n("skip") > 0 ? ", \(n("skip")) not resumed" : ""). \(waitForBusy ? "Busy agents get up to 15 minutes to stop first. " : "")\
        \(update ? "Updates install first; if they fail, it stops without rebooting. " : "")\
        The desktop will be unreachable for several minutes; you'll get an email when it's back.
        """
        alert.addButton(withTitle: update ? "Update & Restart" : "Restart").hasDestructiveAction = true
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        running = true
        error = nil
        Actions.runJSON(["action": "restart_run", "update": update, "wait_minutes": waitForBusy ? 15 : 0, "choices": final]) { obj, err in
            running = false
            if (obj?["ok"] as? Bool) == true {
                model.show("Restart started: the board reconnects when the desktop is back.")
                model.refreshAll()
            } else {
                let e = (obj?["error"] as? String) ?? err ?? "could not start the restart"
                error = "Not restarted: " + e
                model.show("Not restarted: " + e)
            }
        }
    }
}
