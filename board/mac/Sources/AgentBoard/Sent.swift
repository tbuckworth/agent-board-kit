import AppKit
import SwiftUI

// What the owner sent from the board, and whether each message has been dealt with (agentboard/sent.py). An agent
// marks a message done with a one-line note; it is bold here until the owner ticks it off.

struct SentCounts: Decodable, Equatable {
    var toCheck: Int?
    var waiting: Int?
}

struct SentDone: Decodable {
    var ts: Double?
    var note: String?
    var by: String?              // the session that marked it done
}

struct SentMessage: Decodable, Identifiable {
    var ref: String
    var ts: Double?
    var kind: String?
    var to: String?
    var ok: Bool?
    var text: String
    var error: String?
    var state: String            // waiting, done, checked, failed
    var done: SentDone?
    var id: String { ref }

    enum CodingKeys: String, CodingKey { case ref, ts, kind, to, ok, text, error, state, done }
}

struct SentList: Decodable {
    var messages: [SentMessage]
    var dropped: [String]         // why messages that do not decode were left out

    enum CodingKeys: String, CodingKey { case messages }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var why: [String] = []
        messages = c.lossyArray(SentMessage.self, forKey: .messages, failures: &why)
        dropped = why
    }
}

/// The top bar's way in: "Sent", or "2 to check" in bold once agents have marked messages done.
struct SentButton: View {
    @ObservedObject var model: BoardModel
    @Environment(\.zoom) private var z

    var body: some View {
        let n = model.snapshot?.sent?.toCheck ?? 0
        let waiting = model.snapshot?.sent?.waiting ?? 0
        Button { model.sheet = .sent } label: {
            Label(n > 0 ? "\(n) to check" : "Sent", systemImage: "paperplane")
                .font(.system(size: 11 * z, weight: n > 0 ? .bold : .regular))
                .foregroundColor(n > 0 ? Palette.ok : .secondary)
        }
        .buttonStyle(.borderless)
        .help("What you've sent from the board: \(n) marked done by the agent, waiting for you to tick off; "
              + "\(waiting) not marked done yet.")
    }
}

struct SentSheet: View {
    @ObservedObject var model: BoardModel
    @Environment(\.zoom) private var z
    @State private var messages: [SentMessage] = []
    @State private var error: String?
    @State private var loaded = false
    @State private var hideTicked = UserDefaults.standard.object(forKey: "sentHideTicked") as? Bool ?? true
    @State private var expanded: Set<String> = []
    private let poll = Timer.publish(every: 15, on: .main, in: .common).autoconnect()

    var shown: [SentMessage] { hideTicked ? messages.filter { $0.state != "checked" } : messages }
    var doneRefs: [String] { messages.filter { $0.state == "done" }.map(\.ref) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Sent messages").font(.headline)
                Spacer()
                Toggle("Hide ticked off", isOn: $hideTicked)
                    .toggleStyle(.checkbox)
                    .onChange(of: hideTicked) { v in UserDefaults.standard.set(v, forKey: "sentHideTicked") }
            }
            Text("Bold: the agent says it's done (its note is underneath). Tick it off once you've checked.")
                .font(.system(size: 11 * z)).foregroundStyle(.secondary)
            if let e = error {
                Text(e).font(.callout).foregroundColor(AgentState.blocked.color)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 6) {
                    if loaded && shown.isEmpty {
                        Text(messages.isEmpty ? "Nothing sent from the board yet." : "Everything is ticked off.")
                            .foregroundStyle(.secondary).padding(.top, 24).frame(maxWidth: .infinity)
                    }
                    ForEach(shown) { m in row(m) }
                }
            }
            HStack {
                if !loaded { ProgressView().controlSize(.small) }
                Spacer()
                if !doneRefs.isEmpty {
                    Button("Tick off all \(doneRefs.count) done") { setChecked(doneRefs, true) }
                }
                Button("Close") { model.sheet = nil }.keyboardShortcut(.cancelAction)
            }
        }
        .padding(18)
        .frame(width: 660, height: 600)
        .onAppear(perform: load)
        .onReceive(poll) { _ in load() }
    }

    func row(_ m: SentMessage) -> some View {
        let checked = m.state == "checked"
        let long = m.text.count > 240 || m.text.split(separator: "\n", omittingEmptySubsequences: false).count > 3
        let open = expanded.contains(m.ref)
        return HStack(alignment: .top, spacing: 8) {
            Button { setChecked([m.ref], !checked) } label: {
                Image(systemName: checked ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 15 * z))
                    .foregroundColor(m.state == "done" ? Palette.ok : .secondary)
            }
            .buttonStyle(.borderless)
            .help(checked ? "Ticked off. Click to untick." : "Tick off")
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(SentSheet.when(m.ts)).monospacedDigit()
                    Text("→ " + (m.to ?? "?") + SentSheet.kindLabel(m.kind)).lineLimit(1)
                    Spacer()
                    Text(m.ref).monospaced()
                    Text(SentSheet.label(m.state)).foregroundColor(SentSheet.color(m.state))
                }
                .font(.system(size: 11 * z))
                .foregroundStyle(.secondary)
                Text(m.text)
                    .font(.system(size: 12.5 * z, weight: m.state == "done" ? .bold : .regular))
                    .foregroundColor(checked ? .secondary : .primary)
                    .lineLimit(open ? nil : 3)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if long {
                    Button(open ? "Less" : "More") {
                        if open { expanded.remove(m.ref) } else { expanded.insert(m.ref) }
                    }
                    .buttonStyle(.link).font(.system(size: 11 * z))
                }
                if let d = m.done {
                    Text("Done \(SentSheet.when(d.ts))\(doneBy(d)): \(d.note ?? "no note")")
                        .font(.system(size: 11.5 * z, weight: m.state == "done" ? .semibold : .regular))
                        .foregroundColor(m.state == "done" ? Palette.ok : .secondary)
                        .textSelection(.enabled)
                }
                if m.state == "failed" {
                    Text("Not delivered: " + (m.error ?? "unknown error"))
                        .font(.system(size: 11 * z)).foregroundColor(AgentState.blocked.color).lineLimit(3)
                }
            }
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 6)
            .fill(m.state == "done" ? Palette.ok.opacity(0.10) : Color.primary.opacity(0.04)))
    }

    /// " by NAME" when the agent that marked it done is on the board (one of a group chat's members, say).
    func doneBy(_ d: SentDone) -> String {
        guard let by = d.by, let a = model.snapshot?.agents.first(where: { $0.id == by }) else { return "" }
        return " by " + a.display
    }

    func load() {
        Actions.fetch(["action": "sent", "limit": 400], key: nil, as: SentList.self) { list, why in
            loaded = true
            guard let list = list else { error = why; return }
            messages = list.messages
            let n = list.dropped.count
            error = list.dropped.first.map { "\(n) message\(n == 1 ? "" : "s") not understood: " + $0 }
        }
    }

    /// A tick shows at once. An untick waits for the desktop's answer and the reload after it, which bring the
    /// message's state from sent.py.
    func setChecked(_ refs: [String], _ on: Bool) {
        if on {
            for i in messages.indices where refs.contains(messages[i].ref) { messages[i].state = "checked" }
        }
        Actions.run(["action": "sent_mark", "refs": refs, "checked": on]) { res in
            if !res.ok { error = "Couldn't save the tick: \(res.message)" }
            load()
        }
    }

    static func when(_ ts: Double?) -> String {
        guard let ts = ts else { return "-" }
        let date = Date(timeIntervalSince1970: ts)
        let f = DateFormatter()
        f.dateFormat = Calendar.current.isDateInToday(date) ? "HH:mm" : "EEE d MMM HH:mm"
        return f.string(from: date)
    }

    static func kindLabel(_ kind: String?) -> String {
        switch kind {
        case "resume": return " (resume note)"
        case "spawn": return " (instructions)"
        case "chat": return " (your post)"
        default: return ""
        }
    }

    static func label(_ state: String) -> String {
        switch state {
        case "done": return "done: check it"
        case "checked": return "ticked off"
        case "failed": return "not delivered"
        default: return "waiting"
        }
    }

    static func color(_ state: String) -> Color {
        switch state {
        case "done": return Palette.ok
        case "failed": return AgentState.blocked.color
        default: return .secondary
        }
    }
}
