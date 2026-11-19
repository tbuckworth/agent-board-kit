import Charts
import SwiftUI

// Where a plan window's allowance has gone since it last reset: the sheet a usage meter opens. The desktop works it
// out (the usage_split action, agentboard/allowance.py): each agent's use priced at API rates, turned into percent of
// the window by one fitted factor, by board group and sub-group, and Other, the reading less the agents' estimate.

struct UsageSplit: Decodable {
    var target: String
    var window: String
    var since: Double
    var resetsAt: Double
    var used: Double
    var dollars: Double
    var scale: SplitScale?
    var agentsPct: Double?
    var otherPct: Double?
    var groups: [SplitGroup]
    var chart: SplitChart
    var layout: [TimeLayoutGroup]
    var notes: [String]?
}

/// The factor from dollars to percent of the window: from `points` stretches of readings, each typically missed by
/// `rms` points of the window.
struct SplitScale: Decodable {
    var pctPerDollar: Double
    var points: Int
    var rms: Double
}

struct SplitAgent: Decodable, Identifiable {
    var id: String
    var name: String
    var dollars: Double
    var pct: Double?
    var inferred: Bool
}

struct SplitSub: Decodable, Identifiable {
    var name: String
    var dollars: Double
    var pct: Double?
    var agents: [SplitAgent]
    var id: String { name }
}

struct SplitGroup: Decodable, Identifiable {
    var name: String
    var dollars: Double
    var pct: Double?
    var agents: [SplitAgent]
    var subgroups: [SplitSub]
    var id: String { name }
}

/// Each group's use from the window's start to the end of each step (cumulative), in "%" of the window or, without a
/// factor, in "$"; `other` per step, in percent, without a factor nil.
struct SplitChart: Decodable {
    var start: Double
    var step: Double
    var unit: String
    var groups: [SplitSeries]
    var other: [Double]?
}

struct SplitSeries: Decodable {
    var name: String
    var values: [Double]
}

/// One point of the stacked chart.
struct SplitPoint: Identifiable {
    var band: String
    var date: Date
    var value: Double
    var id: String { band + "@" + String(date.timeIntervalSince1970) }
}

enum SplitBands {
    static let other = "Other"
    static let otherColor = Color(hex: 0xC9C8C2)

    /// The chart's points, one band per group in the desktop's order (biggest first, so at the bottom), then Other.
    static func points(_ c: SplitChart) -> [SplitPoint] {
        var bands = c.groups.map { ($0.name, $0.values) }
        if let o = c.other { bands.append((other, o)) }
        return bands.flatMap { name, values in
            values.enumerated().map { i, v in
                SplitPoint(band: name, date: Date(timeIntervalSince1970: c.start + Double(i + 1) * c.step), value: v)
            }
        }
    }

    static func color(_ band: String, layout: [TimeLayoutGroup]) -> Color {
        band == other ? otherColor : TimeColors.group(band, layout: layout)
    }
}

struct UsageSplitSheet: View {
    @ObservedObject var model: BoardModel
    @Environment(\.zoom) private var z
    let target: String
    let window: String
    let title: String
    @State private var split: UsageSplit?
    @State private var error: String?
    @State private var open: Set<String> = []        // the groups and sub-groups shown open

    init(model: BoardModel, target: String, window: String, title: String, preloaded: UsageSplit? = nil) {
        self.model = model
        self.target = target
        self.window = window
        self.title = title
        _split = State(initialValue: preloaded)
        _open = State(initialValue: Set(preloaded?.groups.prefix(1).map(\.name) ?? []))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(title).font(.headline)
                Spacer()
                if split == nil && error == nil { ProgressView().controlSize(.small) }
            }
            if let e = error {
                Text(e).font(.callout).foregroundColor(AgentState.blocked.color).textSelection(.enabled)
            }
            if let s = split {
                Text(headline(s)).font(.system(size: 12 * z))
                chart(s).frame(height: 190 * z)
                ScrollView { list(s) }
                Text(method(s)).font(.system(size: 10.5 * z)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                ForEach(s.notes ?? [], id: \.self) { n in
                    Text(n).font(.system(size: 10.5 * z)).foregroundColor(Palette.attention)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                Spacer()
            }
            HStack {
                Spacer()
                Button("Close") { model.sheet = nil }.keyboardShortcut(.cancelAction)
            }
        }
        .padding(18)
        .frame(width: 680, height: 700)
        .onAppear { if split == nil { load() } }
    }

    func headline(_ s: UsageSplit) -> String {
        let used = String(format: "%.0f%% used", s.used)
        let since = "since the reset \(Fmt.clock(s.since))"
        guard let a = s.agentsPct, let o = s.otherPct else { return "\(used) \(since)" }
        return "\(used) \(since): the board's agents ≈ \(pct(a)), other ≈ \(pct(max(0, o)))"
    }

    func method(_ s: UsageSplit) -> String {
        let priced = "The agents' use is priced at API rates ($\(Fmt.count(s.dollars)) in this window)"
        guard let k = s.scale else {
            return priced + ". There are too few usage readings yet to turn dollars into percent, so the shares are in dollars and Other is not known."
        }
        let fit = String(format: "%.2f%% of the window per $100, fitted on %d stretches of usage readings; one stretch is typically off by %.1f points",
                         k.pctPerDollar * 100, k.points, k.rms)
        let below = (s.otherPct ?? 0) < 0 ? String(format: " Here the agents' estimate is %.1f points above the reading, so Other shows as 0.", -(s.otherPct ?? 0)) : ""
        return priced + " and turned into percent by one factor (\(fit)). Other is the reading less the agents' estimate: the claude.ai and phone apps, and the Mac's own agents." + below
    }

    func pct(_ v: Double) -> String { v < 0.95 ? String(format: "%.1f%%", v) : String(format: "%.0f%%", v) }

    func share(_ pct: Double?, _ dollars: Double) -> String { pct.map { self.pct($0) } ?? "$" + Fmt.count(dollars) }

    func chart(_ s: UsageSplit) -> some View {
        let pts = SplitBands.points(s.chart)
        let bands = s.chart.groups.map(\.name) + (s.chart.other == nil ? [] : [SplitBands.other])
        return Chart(pts) { p in
            AreaMark(x: .value("Time", p.date), y: .value("Use", p.value), stacking: .standard)
                .foregroundStyle(by: .value("Group", p.band))
        }
        .chartForegroundStyleScale(domain: bands, range: bands.map { SplitBands.color($0, layout: s.layout) })
        .chartYAxisLabel(s.chart.unit == "%" ? "% of the window" : "$ at API rates")
        .chartLegend(position: .bottom, alignment: .leading)
    }

    func list(_ s: UsageSplit) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(s.groups) { g in
                row(g.name, share(g.pct, g.dollars), color: SplitBands.color(g.name, layout: s.layout), depth: 0,
                    key: g.name, opens: true)
                if open.contains(g.name) {
                    ForEach(g.subgroups) { sub in
                        row(sub.name, share(sub.pct, sub.dollars), color: TimeColors.subgroup(sub.name, of: g.name, layout: s.layout),
                            depth: 1, key: g.name + "/" + sub.name, opens: true)
                        if open.contains(g.name + "/" + sub.name) {
                            ForEach(sub.agents) { agentRow($0, depth: 2) }
                        }
                    }
                    ForEach(g.agents) { agentRow($0, depth: 1) }
                }
            }
            if let o = s.otherPct {
                row(SplitBands.other, pct(max(0, o)), color: SplitBands.otherColor, depth: 0, key: "", opens: false)
            }
        }
    }

    func row(_ name: String, _ value: String, color: Color, depth: Int, key: String, opens: Bool) -> some View {
        HStack(spacing: 6) {
            Image(systemName: open.contains(key) ? "chevron.down" : "chevron.right")
                .font(.system(size: 9 * z)).foregroundStyle(.secondary).opacity(opens ? 1 : 0)
            RoundedRectangle(cornerRadius: 2).fill(color).frame(width: 10 * z, height: 10 * z)
            Text(name).font(.system(size: 12 * z, weight: depth == 0 ? .semibold : .regular)).lineLimit(1)
            Spacer()
            Text(value).font(.system(size: 12 * z).monospacedDigit())
        }
        .padding(.leading, CGFloat(depth) * 18 * z)
        .padding(.vertical, 2)
        .contentShape(Rectangle())
        .onTapGesture {
            guard opens else { return }
            if open.contains(key) { open.remove(key) } else { open.insert(key) }
        }
    }

    func agentRow(_ a: SplitAgent, depth: Int) -> some View {
        HStack(spacing: 6) {
            Text(a.name).font(.system(size: 11.5 * z)).lineLimit(1)
            if a.inferred {
                Text("grouped by its repo").font(.system(size: 10 * z)).foregroundStyle(.secondary)
            }
            Spacer()
            Text(share(a.pct, a.dollars)).font(.system(size: 11.5 * z).monospacedDigit()).foregroundStyle(.secondary)
        }
        .padding(.leading, CGFloat(depth) * 18 * z + 22 * z)
        .padding(.vertical, 1)
    }

    func load() {
        Actions.fetch(["action": "usage_split", "target": target, "window": window], key: "split", as: UsageSplit.self) { got, why in
            if let got = got {
                split = got
                open = Set(got.groups.prefix(1).map(\.name))
            } else {
                error = why
            }
        }
    }
}

// MARK: --selftest-usage

enum UsageSplitTest {
    /// The chart's bands: groups in the desktop's order, then Other; each point at the end of its step.
    static func run() -> Int32 {
        let json = """
        {"target": "claude:default", "window": "weekly", "since": 1000, "resets_at": 605800, "used": 13,
         "dollars": 400.0, "scale": {"pct_per_dollar": 0.03, "points": 50, "rms": 1.0}, "agents_pct": 12.0,
         "other_pct": 1.0, "groups": [{"name": "Admin", "dollars": 200, "pct": 6.7, "agents": [], "subgroups": []}],
         "chart": {"start": 1000, "step": 3600, "unit": "%", "groups": [{"name": "Admin", "values": [1, 2]},
                   {"name": "Work", "values": [0.5, 0.5]}], "other": [0, 0.5]},
         "layout": [{"name": "Work", "subgroups": ["Alpha"]}, {"name": "Admin", "subgroups": []}], "notes": []}
        """
        var failures: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { failures.append(what) } }
        guard let s = try? JSON.decoder.decode(UsageSplit.self, from: Data(json.utf8)) else {
            print("FAIL: the sample split does not decode")
            return 1
        }
        let pts = SplitBands.points(s.chart)
        check(pts.map(\.band) == ["Admin", "Admin", "Work", "Work", "Other", "Other"], "bands in order, Other last")
        check(pts.first?.date == Date(timeIntervalSince1970: 4600), "a point sits at the end of its step")
        check(s.scale?.points == 50 && s.resetsAt == 605800, "snake-case fields decode")
        check(SplitBands.color("Other", layout: s.layout) == SplitBands.otherColor, "Other has its own colour")
        check(SplitBands.color("Admin", layout: s.layout) == TimeColors.group("Admin", layout: s.layout),
              "a group has its Time page colour")
        let window = try? JSON.decoder.decode(UsageWindow.self, from: Data(#"{"kind": "model_weekly", "model": "fable", "key": "model:fable"}"#.utf8))
        check(window?.key == "model:fable", "a meter's window carries the desktop's key, which a click asks for")
        failures.forEach { print("FAIL: " + $0) }
        print(failures.isEmpty ? "usage split: all checks pass" : "usage split: \(failures.count) failed")
        return failures.isEmpty ? 0 : 1
    }
}
