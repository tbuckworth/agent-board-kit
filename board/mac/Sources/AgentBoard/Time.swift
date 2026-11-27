import AppKit
import SwiftUI

// Where the owner's time went, per day and board group (the desktop computes it: agentboard/timeuse.py).
// Each group has its colour, the Mail tab's, and sub-groups are shades of it, darker to lighter in layout order;
// Unsorted time is Ungrouped's grey (the desktop sends them all: agentboard/colours.py). Time the desktop guessed a
// group for (an ungrouped tile, a meeting, a document, an email) is hatched. Hovering names a segment below the chart.
// Clicking narrows: a group
// shows only that group, split by sub-group; then a sub-group shows only that sub-group. Everything shown can count
// all time, only work time or only the rest (the desktop's work hours; the choice is remembered). Clicking a day's date
// lists what counted that day. "1 day" shows one day across its hours instead, a lane per group and per
// sub-group (only the selected group's when one is selected), stepped day by day. The tiles and items behind what
// is shown are listed below the chart, and clicking
// one shows how its time adds up: its 5-minute blocks per day and the activities in them.

struct TimeRow: Decodable {
    var group: String
    var subgroup: String?
    var inferred: Bool
    var minutes: Double
    var workMinutes: Double       // the part of `minutes` in work time
}

struct TimeDay: Decodable, Identifiable {
    var day: String
    var rows: [TimeRow]
    var workday: Bool             // one of the work hours' weekdays (the desktop's setting)
    var date: Date                // local midnight of `day`; a day that is not "yyyy-MM-dd" fails the decoding
    var id: String { day }

    enum CodingKeys: String, CodingKey { case day, rows, workday }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        day = try c.decode(String.self, forKey: .day)
        rows = try c.decode([TimeRow].self, forKey: .rows)
        workday = try c.decode(Bool.self, forKey: .workday)
        guard let d = TimeSheet.date(day) else {
            throw DecodingError.dataCorruptedError(forKey: .day, in: c, debugDescription: "not a yyyy-MM-dd day: \(day)")
        }
        date = d
    }
}

struct TimeItem: Decodable {
    var day: String
    var group: String
    var subgroup: String?
    var inferred: Bool
    var key: String
    var label: String
    var kind: String
    var minutes: Double
    var workMinutes: Double       // the part of `minutes` in work time
    var blocks: [Double]          // the starts of the blocks it was active in that day (seconds since the epoch)
    var workBlocks: [Double]      // those of `blocks` in work time
    var kinds: [String: Int]      // its activities that day, by kind
}

struct TimeLayoutGroup: Decodable {
    var name: String
    var subgroups: [String]
}

struct TimeDoc: Decodable {
    var generated: Double?
    var block: Double             // seconds in one block of counted time
    var layout: [TimeLayoutGroup]
    var warnings: [String]
    var waiting: [String]         // sources a device has uploaded nothing for yet: named, but not failures
    var daily: [TimeDay]
    var items: [TimeItem]
    var unsorted: String          // the group of time no group fits (timeuse.UNSORTED)
    var colours: GroupColours     // the groups' colours and sub-groups' shades; Unsorted takes Ungrouped's

    /// The figures counting only `hours`: every row's and item's minutes, and every item's blocks, in that time, and
    /// the rows and items with none of it left out. The days stay; TimeSheet decides which it shows.
    func showing(_ hours: TimeHours) -> TimeDoc {
        guard hours != .all else { return self }
        let work = hours == .work
        var d = self
        d.daily = daily.map { day in
            var day = day
            day.rows = day.rows.compactMap { r in
                var r = r
                r.minutes = work ? r.workMinutes : r.minutes - r.workMinutes
                return r.minutes > 0.05 ? r : nil          // the desktop rounds to 0.1
            }
            return day
        }
        d.items = items.compactMap { it in
            var it = it
            let inWork = Set(it.workBlocks)
            it.minutes = work ? it.workMinutes : it.minutes - it.workMinutes
            it.blocks = it.blocks.filter { inWork.contains($0) == work }
            return it.blocks.isEmpty ? nil : it
        }
        return d
    }
}

/// Which of the owner's time the sheet counts: all of it, work time (the desktop's work hours, timeuse work_hours) or the
/// rest. Remembered as `timeHours`; --render-time takes it as --hours.
enum TimeHours: String, CaseIterable {
    case all, work, off

    var label: String {
        switch self {
        case .all: return "All time"
        case .work: return "Work time"
        case .off: return "Outside work"
        }
    }
}

/// One coloured part of a day's bar: a group, or a sub-group of the selected group, split by guessed or not.
struct TimeSegment: Identifiable {
    var group: String         // the board group it belongs to
    var sub: String?          // its sub-group; nil for time in none
    var name: String          // what hovering shows: the group, or "Group: Sub-group"
    var color: Color
    var inferred: Bool
    var minutes: Double
    var id: String { name + (inferred ? "~" : "") }
}

/// One lane of the one-day view: a group's time in no sub-group, or one sub-group's, and the blocks (their start
/// times) it was active in that day.
struct TimeLane: Identifiable {
    var group: String
    var sub: String?
    var name: String
    var color: Color
    var solid: Set<Double>        // blocks where some of its time there was placed by hand (a grouped tile, a fixed group)
    var guessed: Set<Double>      // blocks where all of its time there is guessed
    var minutes: Double
    var id: String { name }
}

/// Where the sheet starts (the offscreen render, --render-time): a group, a sub-group of it (.some(nil): time in
/// none), a day, an item whose breakdown is open, named by part of its title, which time counts, and the range
/// in days (1: the one-day view; nil: the remembered one).
struct TimeFocus {
    var group: String?
    var sub: String??
    var day: String?
    var item: String?
    var hours: TimeHours
    var range: Int?
}

/// One line of the item list: a tile, or every item of one title in one group (a repeating meeting), with the
/// day rows behind it.
struct TimeLine: Identifiable {
    var id: String
    var label: String
    var kind: String
    var group: String
    var sub: String?
    var inferred: Bool
    var items: [TimeItem]
    var minutes: Double { items.map(\.minutes).reduce(0, +) }
}

/// Diagonal stripes over a pale fill: drawn time the desktop guessed a group for.
struct Hatched: View {
    var color: Color

    var body: some View {
        Canvas { ctx, size in Hatched.draw(in: ctx, area: Path(CGRect(origin: .zero, size: size)), size: size, color: color) }
    }

    /// The hatching of `area` (within a canvas of `size`): the bars' segments and the one-day view's guessed blocks.
    static func draw(in ctx: GraphicsContext, area: Path, size: CGSize, color: Color) {
        ctx.fill(area, with: .color(color.opacity(0.28)))
        ctx.drawLayer { layer in
            layer.clip(to: area)
            var p = Path()
            var x = -size.height
            while x < size.width {
                p.move(to: CGPoint(x: x, y: size.height))
                p.addLine(to: CGPoint(x: x + size.height, y: 0))
                x += 5
            }
            layer.stroke(p, with: .color(color), lineWidth: 1.5)
        }
    }
}

struct TimeSheet: View {
    @ObservedObject var model: BoardModel
    /// Figures to show instead of asking the desktop, and where to start (the offscreen render, --render-time). A
    /// render changes none of the app's remembered settings.
    var preloaded: (doc: TimeDoc, focus: TimeFocus)?
    @Environment(\.zoom) private var z
    @State private var loaded: TimeDoc?
    @State private var error: String?
    @State private var loading = false
    @State private var range = UserDefaults.standard.object(forKey: "timeRange") as? Int ?? 14
    @State private var selected: String?          // a group name: only that group, split by sub-group
    @State private var sub: String??              // within it, one sub-group; .some(nil) is time in none
    @State private var day: String?               // one day: what counted towards it
    @State private var openLine: String?          // the item list line whose breakdown is shown
    @State private var hover: String?

    @State private var hours = UserDefaults.standard.string(forKey: "timeHours").flatMap(TimeHours.init) ?? .all
    /// The ranges the picker offers, in days; 1 is the one-day view (also what --render-time --range accepts).
    static let ranges = [1, 7, 14, 30]
    /// The desktop's figures, counting only the time chosen (TimeDoc.showing).
    var doc: TimeDoc? { loaded?.showing(hours) }
    /// Work time leaves out the days that have none.
    func shows(_ d: TimeDay) -> Bool { hours != .work || d.workday }
    /// Every day the figures cover, without the days that are not workdays when only work time counts.
    var allDays: [TimeDay] { (doc?.daily ?? []).filter(shows) }
    var oneDay: Bool { range == 1 }
    /// The day whose items are listed: the one clicked, or in the one-day view the latest shown when none is (or
    /// the one clicked is no longer shown).
    var focusDay: String? {
        guard oneDay else { return day }
        return day.flatMap { d in allDays.contains { $0.day == d } ? d : nil } ?? allDays.last?.day
    }
    /// The days shown: the range's last days (in the one-day view, the focused day), without the days that are not
    /// workdays when only work time counts. The legend, the chart and the item list all count only these.
    var days: [TimeDay] {
        oneDay ? allDays.filter { $0.day == focusDay } : Array((doc?.daily ?? []).suffix(range)).filter(shows)
    }
    var layout: [TimeLayoutGroup] { doc?.layout ?? [] }
    /// The groups' colours (TimeDoc.colours). Before the figures come there are no groups, days or items to colour,
    /// as with `layout`: an empty set stands in, never drawn.
    var colours: GroupColours { doc?.colours ?? GroupColours(groups: [], ungrouped: HexColour(rgb: 0)) }
    var groupNames: [String] {
        var names = layout.map(\.name)
        if let u = doc?.unsorted, days.contains(where: { $0.rows.contains { $0.group == u } }) { names.append(u) }
        return names
    }

    var title: String {
        var t = "Where your time went"
        if let g = selected { t += ": " + g + (sub.map { " › " + ($0 ?? "no sub-group") } ?? "") }
        if let d = focusDay { t += " · " + TimeSheet.dayLabel(d, oneLine: true) }
        return t
    }

    /// In the one-day view: the day `by` days on among those shown.
    func step(_ by: Int) {
        let all = allDays.map(\.day)
        guard let f = focusDay, let i = all.firstIndex(of: f), all.indices.contains(i + by) else { return }
        day = all[i + by]
        openLine = nil
    }

    func canStep(_ by: Int) -> Bool {
        let all = allDays.map(\.day)
        return focusDay.flatMap { all.firstIndex(of: $0) }.map { all.indices.contains($0 + by) } ?? false
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(title).font(.headline)
                if selected != nil {
                    Button("All groups") { selected = nil; sub = nil; openLine = nil }.buttonStyle(.link)
                }
                if let g = selected, sub != nil {
                    Button("All of \(g)") { sub = nil; openLine = nil }.buttonStyle(.link)
                }
                if day != nil && !oneDay {
                    Button("All days") { day = nil; openLine = nil }.buttonStyle(.link)
                }
                if oneDay {
                    Button { step(-1) } label: { Image(systemName: "chevron.left") }
                        .buttonStyle(.borderless).disabled(!canStep(-1)).help("The day before")
                    Button { step(1) } label: { Image(systemName: "chevron.right") }
                        .buttonStyle(.borderless).disabled(!canStep(1)).help("The day after")
                }
                Spacer()
                Picker("", selection: $hours) {
                    ForEach(TimeHours.allCases, id: \.self) { h in Text(h.label).tag(h) }
                }
                .pickerStyle(.menu)
                .fixedSize()
                .help("Count all your time, only your work time (the work hours set on the desktop), or only the time "
                      + "outside them")
                .onChange(of: hours) { v in
                    openLine = nil
                    if preloaded == nil { UserDefaults.standard.set(v.rawValue, forKey: "timeHours") }
                }
                Picker("", selection: $range) {
                    ForEach(TimeSheet.ranges, id: \.self) { r in Text(r == 1 ? "1 day" : "\(r) days").tag(r) }
                }
                .pickerStyle(.segmented)
                .frame(width: 270)
                .onChange(of: range) { v in
                    if preloaded == nil { UserDefaults.standard.set(v, forKey: "timeRange") }
                }
            }
            Text("Active time: each 5 minutes in which you prompted, messaged or emailed an agent, were in a meeting in work "
                 + "hours, edited a Google document, sent mail or made a to-do, or else used your Mac (by its screen, from "
                 + "ActivityWatch). Each source counts only once it is set up. Hatched: the group is a guess. Click a group, then a "
                 + "sub-group, to narrow; a date for that day; an item for how its time adds up. 1 day shows a day hour by hour.")
                .font(.system(size: 11 * z)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let e = error {
                Text(e).font(.callout).foregroundColor(AgentState.blocked.color)
            }
            legend
            Group { if oneDay { timeline } else { chart } }.frame(height: 260 * z)
            Text(hover ?? " ").font(.system(size: 11.5 * z)).monospacedDigit().foregroundStyle(.secondary)
            if selected != nil || focusDay != nil { itemList } else { Spacer(minLength: 0) }
            HStack {
                if loading { ProgressView().controlSize(.small) }
                if let d = doc {
                    Text("Updated \(SentSheet.when(d.generated))").font(.system(size: 11 * z)).foregroundStyle(.secondary)
                    if !d.warnings.isEmpty {
                        Text("Left out: " + d.warnings.joined(separator: "; "))
                            .font(.system(size: 11 * z)).foregroundColor(Palette.attention).lineLimit(2)
                    }
                    if !d.waiting.isEmpty {
                        Text("Waiting for a first upload: " + d.waiting.joined(separator: "; "))
                            .font(.system(size: 11 * z)).foregroundStyle(.secondary).lineLimit(2)
                    }
                }
                if let e = model.screenError {
                    Text("Screen time not sent: " + e).font(.system(size: 11 * z)).foregroundColor(Palette.attention).lineLimit(2)
                }
                Spacer()
                Button("Refresh") { load(refresh: true) }.disabled(loading)
                Button("Close") { model.sheet = nil }.keyboardShortcut(.cancelAction)
            }
        }
        .padding(18)
        .frame(width: 900, height: selected == nil && focusDay == nil ? 520 : 720)
        .onChange(of: days.map(\.day)) { shown in      // the selected day must be one of the days shown
            if let d = day, !shown.contains(d) { day = nil }
        }
        .onAppear {
            if let pre = preloaded {
                let f = pre.focus
                loaded = pre.doc
                selected = f.group
                sub = f.sub
                day = f.day
                hours = f.hours
                if let r = f.range { range = r }
                if let want = f.item { openLine = lines().first { $0.label.localizedCaseInsensitiveContains(want) }?.id }
            } else {
                load(refresh: false)
            }
        }
    }

    // MARK: legend: the groups, or the selected group's sub-groups, with hours over the range; click to narrow

    var legend: some View {
        HStack(spacing: 14) {
            if let g = selected { subLegend(g) } else { groupLegend }
        }
    }

    var groupLegend: some View {
        ForEach(groupNames, id: \.self) { g in
            legendEntry(g, color: colours.group(g), hours: hours(group: g, sub: nil), on: false,
                        help: "Show only \(g), split by sub-group") {
                selected = g
                sub = nil
                openLine = nil
            }
        }
    }

    func subLegend(_ g: String) -> some View {
        ForEach(Array(subNames(g).enumerated()), id: \.offset) { _, s in
            let on: Bool = sub == .some(s)
            legendEntry(s ?? "no sub-group", color: colours.subgroup(s, of: g), hours: hours(group: g, sub: .some(s)),
                        on: on, help: on ? "Show all of \(g)" : "Show only \(s ?? "time in no sub-group")") {
                sub = on ? nil : .some(s)
                openLine = nil
            }
        }
    }

    /// Hours over the range in a group, or in one of its sub-groups (.some(nil): time in none).
    func hours(group g: String, sub s: String??) -> Double {
        let rows = days.flatMap(\.rows).filter { $0.group == g && (s == nil || s == .some($0.subgroup)) }
        return rows.map(\.minutes).reduce(0, +) / 60
    }

    func legendEntry(_ name: String, color: Color, hours: Double, on: Bool, help: String,
                     action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 5) {
                RoundedRectangle(cornerRadius: 2).fill(color).frame(width: 11, height: 11)
                Text(name).fontWeight(on ? .bold : .regular)
                Text(String(format: "%.1f h", hours)).monospacedDigit().foregroundStyle(.secondary)
            }
            .font(.system(size: 11.5 * z))
        }
        .buttonStyle(.borderless)
        .help(help)
    }

    /// A group's sub-groups with time in the range, in layout order, time in none first. A sub-group a tile was put
    /// in that the layout no longer has still shows, so no minutes drop out.
    func subNames(_ g: String) -> [String?] {
        let rows = days.flatMap(\.rows).filter { $0.group == g && $0.minutes > 0 }
        var subs: [String?] = [nil] + (layout.first { $0.name == g }?.subgroups ?? []).map { Optional($0) }
        for r in rows where !subs.contains(r.subgroup) { subs.append(r.subgroup) }
        return subs.filter { s in rows.contains { $0.subgroup == s } }
    }

    // MARK: chart: one bar per day, segments stacked from the bottom in layout order

    /// A day's segments, bottom up: each group in layout order, its sub-groups as shades, guessed time after the
    /// rest of the same shade; only the selected group and sub-group when there are.
    func segments(_ d: TimeDay) -> [TimeSegment] {
        var out: [TimeSegment] = []
        for g in groupNames where selected == nil || selected == g {
            let rows = d.rows.filter { $0.group == g }
            for s in subNames(g) where sub == nil || sub == .some(s) {
                for inferred in [false, true] {
                    let m = rows.filter { $0.subgroup == s && $0.inferred == inferred }.map(\.minutes).reduce(0, +)
                    if m > 0 {
                        out.append(TimeSegment(group: g, sub: s, name: s.map { "\(g): \($0)" } ?? g,
                                               color: colours.subgroup(s, of: g), inferred: inferred, minutes: m))
                    }
                }
            }
        }
        return out
    }

    var chart: some View {
        let peak = max(days.map { segments($0).map(\.minutes).reduce(0, +) }.max() ?? 0, 60)
        return GeometryReader { geo in
            let plotH = geo.size.height - 30 * z
            HStack(alignment: .bottom, spacing: max(2, geo.size.width / CGFloat(max(days.count, 1)) * 0.25)) {
                ForEach(days) { d in
                    VStack(spacing: 3) {
                        let segs = segments(d)
                        let total = segs.map(\.minutes).reduce(0, +)
                        Text(total > 0 ? String(format: "%.1f", total / 60) : "")
                            .font(.system(size: 9.5 * z)).monospacedDigit().foregroundStyle(.secondary)
                        VStack(spacing: 2) {
                            ForEach(segs.reversed()) { seg in
                                segmentView(seg, day: d)
                                    .frame(height: max(1, plotH * 0.88 * CGFloat(seg.minutes / peak) - 2))
                            }
                        }
                        .opacity(day == nil || day == d.day ? 1 : 0.35)
                        Button { day = (day == d.day ? nil : d.day); openLine = nil } label: {
                            Text(TimeSheet.dayLabel(d.day)).font(.system(size: 9.5 * z))
                                .fontWeight(day == d.day ? .bold : .regular).multilineTextAlignment(.center)
                                .foregroundStyle(day == d.day ? .primary : .secondary).frame(height: 24 * z)
                        }
                        .buttonStyle(.borderless)
                        .help(day == d.day ? "Show every day" : "What counted on this day")
                    }
                    .frame(maxWidth: .infinity)
                }
            }
            .frame(maxHeight: .infinity, alignment: .bottom)
        }
    }

    func segmentView(_ seg: TimeSegment, day: TimeDay) -> some View {
        ZStack {
            if seg.inferred { Hatched(color: seg.color) } else { Rectangle().fill(seg.color) }
        }
        .clipShape(RoundedRectangle(cornerRadius: 2))
        .contentShape(Rectangle())
        .onHover { inside in
            hover = inside ? "\(TimeSheet.dayLabel(day.day, oneLine: true)) · \(seg.name)\(seg.inferred ? " (guessed)" : "") · "
                + TimeSheet.hours(seg.minutes) : nil
        }
        .onTapGesture { narrow(group: seg.group, sub: seg.sub) }
    }

    /// A click on a segment or a lane: the first narrows to its group, the next to its sub-group (or back).
    func narrow(group g: String, sub s: String?) {
        if selected == nil {
            selected = g
            sub = nil
        } else {
            sub = (sub == .some(s)) ? nil : .some(s)
        }
        openLine = nil
    }

    // MARK: one-day view: the day across its hours, a lane per group and per sub-group (the selected group's only)

    /// Each lane's 5-minute blocks drawn at their clock time, solid or hatched like the bars; hour lines behind.
    /// Hovering names the block's time and what was active in it; clicking a lane narrows as a segment does.
    var timeline: some View {
        let block = doc?.block ?? 300
        let dayItems = (doc?.items ?? []).filter { $0.day == focusDay }
        let lanes = days.first.map { TimeSheet.lanes(dayItems, order: segments($0)) } ?? []
        let span = TimeSheet.hourSpan(lanes.flatMap { Array($0.solid) + Array($0.guessed) }, block: block)
        let first = Double(span.lowerBound * 60), width = Double((span.upperBound - span.lowerBound) * 60)
        return GeometryReader { geo in
            let labelW = 170 * z
            let stripW = max(geo.size.width - labelW, 1)
            let laneH = min(36 * z, max(10, (geo.size.height - 24 * z) / CGFloat(max(lanes.count, 1)) - 4))
            let x = { (minute: Double) in CGFloat((minute - first) / width) * stripW }
            VStack(alignment: .leading, spacing: 4) {
                if lanes.isEmpty {
                    Text("Nothing counted on this day.").foregroundStyle(.secondary).frame(maxHeight: .infinity)
                }
                ForEach(lanes) { lane in
                    HStack(spacing: 0) {
                        HStack(spacing: 5) {
                            RoundedRectangle(cornerRadius: 2).fill(lane.color).frame(width: 11, height: 11)
                            Text(lane.name).lineLimit(1)
                            Text(TimeSheet.hours(lane.minutes)).monospacedDigit().foregroundStyle(.secondary)
                        }
                        .font(.system(size: 11.5 * z))
                        .frame(width: labelW, alignment: .leading)
                        Canvas { ctx, size in
                            for h in span { ctx.fill(Path(CGRect(x: x(Double(h * 60)), y: 0, width: 1, height: size.height)),
                                                     with: .color(.secondary.opacity(0.18))) }
                            let rects = { (blocks: Set<Double>) in
                                TimeSheet.runs(blocks, block: block).map { r -> CGRect in
                                    let m = TimeSheet.minuteOfDay(r.from)
                                    return CGRect(x: x(m), y: 0, width: x(m + (r.to - r.from) / 60) - x(m), height: size.height)
                                }
                            }
                            for r in rects(lane.solid) { ctx.fill(Path(roundedRect: r, cornerRadius: 2), with: .color(lane.color)) }
                            var guessed = Path()
                            for r in rects(lane.guessed) { guessed.addRoundedRect(in: r, cornerSize: CGSize(width: 2, height: 2)) }
                            Hatched.draw(in: ctx, area: guessed, size: size, color: lane.color)
                        }
                        .frame(width: stripW, height: laneH)
                        .contentShape(Rectangle())
                        .onContinuousHover { phase in
                            guard case .active(let p) = phase else { hover = nil; return }
                            hover = TimeSheet.blockNote(at: first + Double(p.x / stripW) * width, lane: lane, items: dayItems, block: block)
                        }
                        .onTapGesture { narrow(group: lane.group, sub: lane.sub) }
                    }
                }
                ZStack(alignment: .topLeading) {
                    ForEach(Array(span), id: \.self) { h in
                        Text(String(format: "%02d:00", h)).font(.system(size: 9.5 * z)).monospacedDigit().foregroundStyle(.secondary)
                            .position(x: labelW + x(Double(h * 60)), y: 8 * z)
                    }
                }
                .frame(width: geo.size.width, height: 16 * z, alignment: .topLeading)
            }
            .frame(maxHeight: .infinity, alignment: .top)
        }
    }

    /// The one-day view's lanes, in the order of `order` (a day's segments: layout order, sub-groups as shades),
    /// one per group and sub-group, with the blocks of `items` (that day's) in it: solid where any of its time in a
    /// block was placed by hand, guessed where all of it was guessed.
    static func lanes(_ items: [TimeItem], order: [TimeSegment]) -> [TimeLane] {
        var out: [TimeLane] = []
        for seg in order where !out.contains(where: { $0.group == seg.group && $0.sub == seg.sub }) {
            let mine = items.filter { $0.group == seg.group && $0.subgroup == seg.sub }
            let solid = Set(mine.filter { !$0.inferred }.flatMap(\.blocks))
            let minutes = order.filter { $0.group == seg.group && $0.sub == seg.sub }.map(\.minutes).reduce(0, +)
            out.append(TimeLane(group: seg.group, sub: seg.sub, name: seg.name, color: seg.color, solid: solid,
                                guessed: Set(mine.filter(\.inferred).flatMap(\.blocks)).subtracting(solid), minutes: minutes))
        }
        return out
    }

    /// The whole hours (local clock) the one-day view spans: from the hour of the first block to the end of the hour
    /// of the last; 09-18 for a day with none.
    static func hourSpan(_ starts: [Double], block: Double) -> ClosedRange<Int> {
        guard let lo = starts.min(), let hi = starts.max() else { return 9...18 }
        let from = Int(minuteOfDay(lo) / 60)
        return from...max(from + 1, min(24, Int((minuteOfDay(hi) + block / 60 + 59) / 60)))
    }

    /// Minutes since local midnight at `t` (seconds since the epoch), by the clock.
    static func minuteOfDay(_ t: Double) -> Double {
        let c = Calendar.current.dateComponents([.hour, .minute, .second], from: Date(timeIntervalSince1970: t))
        return Double((c.hour ?? 0) * 60 + (c.minute ?? 0)) + Double(c.second ?? 0) / 60
    }

    /// What hovering at `minute` (since local midnight) over a lane shows: the block's clock time, the lane, and
    /// the items in it active then; the lane and its hours between blocks.
    static func blockNote(at minute: Double, lane: TimeLane, items: [TimeItem], block: Double) -> String {
        guard let b = lane.solid.union(lane.guessed).first(where: { minute >= minuteOfDay($0) && minute < minuteOfDay($0) + block / 60 }) else {
            return "\(lane.name) · \(hours(lane.minutes))"
        }
        let names = items.filter { $0.group == lane.group && $0.subgroup == lane.sub && $0.blocks.contains(b) }
            .map { $0.label + ($0.inferred ? " (guessed)" : "") }
        return spans([b], block: block) + " · \(lane.name) · " + names.joined(separator: ", ")
    }

    // MARK: item list: the tiles and items behind what is shown, each with how its time adds up

    /// The lines behind the selected group, sub-group and day (every day in the range when none is selected).
    func lines() -> [TimeLine] {
        let shownDays = Set(days.map(\.day))
        var per: [String: TimeLine] = [:]
        for it in doc?.items ?? [] where (selected == nil || it.group == selected) && (sub == nil || sub == .some(it.subgroup))
            && (focusDay.map { it.day == $0 } ?? shownDays.contains(it.day)) {
            // agents by tile, everything else by title, so a repeating meeting is one line
            let k = it.group + "|" + (it.key.hasPrefix("s:") ? it.key : it.kind + "|" + it.label) + (it.inferred ? "~" : "")
            per[k, default: TimeLine(id: k, label: it.label, kind: it.kind, group: it.group, sub: it.subgroup,
                                     inferred: it.inferred, items: [])].items.append(it)
        }
        return per.values.sorted { $0.minutes > $1.minutes }
    }

    var itemList: some View {
        let rows = lines()
        return ScrollView {
            LazyVStack(alignment: .leading, spacing: 3) {
                ForEach(rows.prefix(80)) { r in
                    VStack(alignment: .leading, spacing: 2) {
                        Button { openLine = (openLine == r.id ? nil : r.id) } label: {
                            HStack(spacing: 8) {
                                Image(systemName: openLine == r.id ? "chevron.down" : "chevron.right")
                                    .font(.system(size: 9 * z)).foregroundStyle(.secondary).frame(width: 10)
                                RoundedRectangle(cornerRadius: 2).fill(colours.subgroup(r.sub, of: r.group))
                                    .frame(width: 9, height: 9)
                                Text(TimeSheet.hours(r.minutes)).monospacedDigit().frame(width: 52, alignment: .trailing)
                                Text(r.label).lineLimit(1)
                                Text(TimeSheet.kindLabel(r.kind) + (r.inferred ? ", guessed" : "")).foregroundStyle(.secondary)
                                Spacer()
                                Text(selected == nil ? (r.sub.map { "\(r.group): \($0)" } ?? r.group) : (r.sub ?? ""))
                                    .foregroundStyle(.secondary)
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .help(openLine == r.id ? "Hide how this adds up" : "How this time adds up")
                        if openLine == r.id, let block = doc?.block { breakdown(r, block: block) }
                    }
                    .font(.system(size: 11.5 * z))
                }
            }
        }
        .frame(maxHeight: .infinity)
    }

    /// How a line's time adds up: the blocks it was active in, what they would make alone, and per day the blocks
    /// as clock times with the activities in them.
    func breakdown(_ r: TimeLine, block: Double) -> some View {
        let count = r.items.map(\.blocks.count).reduce(0, +)
        let whole = Double(count) * block / 60
        let shared = r.minutes < whole - 0.5
        return VStack(alignment: .leading, spacing: 2) {
            Text("Active in \(count) blocks of \(Int(block / 60)) minutes, \(TimeSheet.hours(whole)); counted "
                 + TimeSheet.hours(r.minutes) + (shared ? ", because a block shared with other groups or items is split between them." : "."))
                .foregroundStyle(.secondary)
            ForEach(Array(r.items.sorted { $0.day > $1.day }.enumerated()), id: \.offset) { _, it in
                HStack(alignment: .top, spacing: 8) {
                    Text(TimeSheet.dayLabel(it.day, oneLine: true)).frame(width: 86, alignment: .leading)
                    Text(TimeSheet.hours(it.minutes)).monospacedDigit().frame(width: 52, alignment: .trailing)
                    Text(TimeSheet.spans(it.blocks, block: block)).monospacedDigit()
                    Text(TimeSheet.counts(it.kinds)).foregroundStyle(.secondary)
                }
            }
        }
        .font(.system(size: 11 * z))
        .padding(.leading, 28)
        .padding(.bottom, 4)
    }

    func load(refresh: Bool) {
        loading = true
        Actions.fetch(["action": "time", "refresh": refresh], key: "time", as: TimeDoc.self) { d, why in
            loading = false
            guard let d = d else { error = why; return }
            loaded = d
            error = nil
        }
    }

    /// The local date of a "yyyy-MM-dd" day, as time.json names days.
    static func date(_ iso: String) -> Date? {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f.date(from: iso)
    }

    static func dayLabel(_ iso: String, oneLine: Bool = false) -> String {
        guard let d = date(iso) else { return iso }
        let f = DateFormatter()
        f.dateFormat = oneLine ? "EEE d MMM" : "EEE\nd"
        return f.string(from: d)
    }


    static func hours(_ minutes: Double) -> String {
        minutes >= 60 ? String(format: "%.1f h", minutes / 60) : String(format: "%.0f min", minutes)
    }

    /// "09:05–09:20, 14:10–14:15": blocks (their start times) as runs of local clock time.
    static func spans<S: Sequence>(_ starts: S, block: Double) -> String where S.Element == Double {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return runs(starts, block: block)
            .map { f.string(from: Date(timeIntervalSince1970: $0.from)) + "–" + f.string(from: Date(timeIntervalSince1970: $0.to)) }
            .joined(separator: ", ")
    }

    /// Blocks (their start times) merged into runs of back-to-back blocks, earliest first; `to` is the end of a run.
    static func runs<S: Sequence>(_ starts: S, block: Double) -> [(from: Double, to: Double)] where S.Element == Double {
        var out: [(from: Double, to: Double)] = []
        for s in starts.sorted() {
            if let last = out.last, last.to == s { out[out.count - 1].to = s + block } else { out.append((s, s + block)) }
        }
        return out
    }

    /// The activity kinds agentboard/timesources.py writes: what the item list calls an item of that kind, and how
    /// a breakdown counts one activity and several.
    static let kinds: [String: (label: String, one: String, many: String)] = [
        "prompt": ("agent", "prompt typed", "prompts typed"),
        "board": ("agent", "board message", "board messages"),
        "email": ("agent", "email to the agent", "emails to the agent"),
        "meeting": ("meeting", "calendar event", "calendar events"),
        "doc": ("document", "document edit", "document edits"),
        "mail": ("your email", "email sent", "emails sent"),
        "todo": ("to-do", "to-do made or closed", "to-dos made or closed"),
        "screen": ("screen time", "block on screen", "blocks on screen"),
    ]

    static func kindLabel(_ kind: String) -> String { kinds[kind]?.label ?? kind }

    /// "12 board messages, 2 prompts typed": activities by kind, the most first.
    static func counts(_ byKind: [String: Int]) -> String {
        byKind.sorted { ($1.value, $0.key) < ($0.value, $1.key) }.map { kind, n in
            "\(n) " + (kinds[kind].map { n == 1 ? $0.one : $0.many } ?? kind)
        }.joined(separator: ", ")
    }
}

/// --selftest-time: the one-day view's lanes, its hour span and the runs of blocks, and counting only work time or
/// the rest (TimeDoc.showing), offline.
@MainActor
enum TimeTest {
    static func run() -> Int32 {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append(what) } }
        let at = { (h: Int, m: Int) in
            Calendar.current.date(from: DateComponents(year: 2026, month: 11, day: 2, hour: h, minute: m))!.timeIntervalSince1970
        }
        let item = { (group: String, sub: String?, inferred: Bool, blocks: [Double]) in
            TimeItem(day: "2026-11-02", group: group, subgroup: sub, inferred: inferred, key: "k:" + group, label: group,
                     kind: "prompt", minutes: Double(blocks.count * 5), workMinutes: Double(blocks.filter { $0 < at(18, 0) }.count * 5),
                     blocks: blocks, workBlocks: blocks.filter { $0 < at(18, 0) }, kinds: [:])
        }
        let seg = { (g: String, s: String?, inferred: Bool, m: Double) in
            TimeSegment(group: g, sub: s, name: s.map { "\(g): \($0)" } ?? g, color: .gray, inferred: inferred, minutes: m)
        }
        let items = [item("Work", nil, false, [at(9, 5), at(9, 10)]), item("Work", nil, true, [at(9, 10), at(14, 0)]),
                     item("Work", "Alpha", false, [at(10, 0)]), item("Personal", nil, true, [at(20, 0)])]
        let order = [seg("Work", nil, false, 10), seg("Work", nil, true, 5), seg("Work", "Alpha", false, 5), seg("Personal", nil, true, 5)]
        let lanes = TimeSheet.lanes(items, order: order)
        check(lanes.map(\.name) == ["Work", "Work: Alpha", "Personal"], "one lane per group and sub-group, in segment order: \(lanes.map(\.name))")
        check(lanes.first?.solid == [at(9, 5), at(9, 10)] && lanes.first?.guessed == [at(14, 0)],
              "a block with time placed by hand is solid even where a guess shares it")
        check(lanes.first?.minutes == 15, "a lane's minutes are its segments', guessed and not")
        check(lanes.last?.solid.isEmpty == true && lanes.last?.guessed == [at(20, 0)], "time that is only guessed is hatched")
        check(TimeSheet.hourSpan([at(9, 5), at(13, 55)], block: 300) == 9...14, "the span runs from the first block's hour to the end of the last's")
        check(TimeSheet.hourSpan([at(23, 55)], block: 300) == 23...24, "a block at 23:55 ends the span at midnight")
        check(TimeSheet.hourSpan([], block: 300) == 9...18, "a day with no blocks spans working hours")
        check(TimeSheet.runs([at(9, 10), at(9, 5), at(9, 30)], block: 300).map(\.from) == [at(9, 5), at(9, 30)],
              "back-to-back blocks merge into one run")
        check(TimeSheet.spans([at(9, 5), at(9, 10)], block: 300) == "09:05–09:15", "a run is named by its clock times")
        check(TimeSheet.blockNote(at: 9 * 60 + 12, lane: lanes[0], items: items, block: 300) == "09:10–09:15 · Work · Work, Work (guessed)",
              "hovering a block names its time and what was active in it")

        // TimeDoc.showing: a Monday with a block in work time and one after it, and a Saturday
        let row = { (m: Int, w: Int) in #"{"group": "Work", "subgroup": null, "inferred": false, "minutes": \#(m), "work_minutes": \#(w)}"# }
        let json = #"{"block": 300, "layout": [], "warnings": [], "waiting": [], "unsorted": "Unsorted", "#
            + ##""colours": {"groups": [], "ungrouped": "#8C877F"}, "##
            + #""items": [{"day": "2026-11-02", "group": "Work", "#
            + #""subgroup": null, "inferred": false, "key": "k", "label": "k", "kind": "prompt", "minutes": 10, "work_minutes": 5, "#
            + #""blocks": [\#(at(9, 0)), \#(at(18, 0))], "work_blocks": [\#(at(9, 0))], "kinds": {}}], "daily": ["#
            + #"{"day": "2026-11-02", "workday": true, "rows": [\#(row(10, 5))]}, "#
            + #"{"day": "2026-11-07", "workday": false, "rows": [\#(row(5, 0))]}]}"#
        do {
            let doc = try JSON.decoder.decode(TimeDoc.self, from: Data(json.utf8))
            let work = doc.showing(.work), off = doc.showing(.off)
            check(work.daily.map { $0.rows.map(\.minutes) } == [[5], []], "work time: each row's work minutes, none on a Saturday")
            check(off.daily.map { $0.rows.map(\.minutes) } == [[5], [5]], "outside work: the rest, the Saturday's included")
            check(work.items.first?.blocks == [at(9, 0)] && off.items.first?.blocks == [at(18, 0)],
                  "an item's blocks split by work time")
            check(doc.showing(.all).daily.map { $0.rows.map(\.minutes) } == [[10], [5]], "all time is the figures as they are")
            check(doc.daily.map(\.workday) == [true, false], "a day says whether it is a workday")
        } catch {
            check(false, "time.json with work time not understood: \(describeDecodeError(error))")
        }
        // the colours the desktop sends (agentboard/colours.py): a group's, its sub-groups' shades, Ungrouped's
        let palette = ##"{"groups": [{"name": "Work", "colour": "#0E8A84", "shades": [{"name": "Alpha", "##
            + ##""colour": "#006A65"}, {"name": "Beta", "colour": "#007A75"}]}], "ungrouped": "#8C877F"}"##
        if let c = try? JSON.decoder.decode(GroupColours.self, from: Data(palette.utf8)) {
            check(c.group("Work") == Color(hex: 0x0E8A84) && c.group("Unsorted") == Color(hex: 0x8C877F),
                  "a group has its colour, time in no group Ungrouped's")
            check(c.subgroup("Alpha", of: "Work") == Color(hex: 0x006A65)
                  && c.subgroup(nil, of: "Work") == Color(hex: 0x0E8A84), "a sub-group has its shade, time in none the group's")
            check(c.subgroup("Gone", of: "Work") == Color(hex: 0x0E8A84), "a sub-group no longer there: the group's colour")
        } else {
            check(false, "the desktop's colours do not decode")
        }
        for bad in ["\"0E8A84\"", "\"#0E8A8\"", "\"#0E8A8G\"", "\"#0E8A84 \"", "14"] {
            check((try? JSON.decoder.decode([HexColour].self, from: Data("[\(bad)]".utf8))) == nil, "\(bad) is not a colour")
        }
        print(fails.isEmpty ? "time: all checks pass" : fails.map { "FAIL " + $0 }.joined(separator: "\n"))
        return fails.isEmpty ? 0 : 1
    }
}

/// The top bar's way in.
struct TimeButton: View {
    @ObservedObject var model: BoardModel
    @Environment(\.zoom) private var z

    var body: some View {
        Button { model.sheet = .time } label: {
            Label("Time", systemImage: "chart.bar.xaxis").font(.system(size: 11 * z)).foregroundColor(.secondary)
        }
        .buttonStyle(.borderless)
        .help("Where your time went, per day and board group")
    }
}
