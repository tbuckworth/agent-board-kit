import AppKit
import SwiftUI

enum AgentState: String, CaseIterable, Identifiable {
    case busy, background, waiting, blocked, idle, stale, offline
    var id: String { rawValue }

    /// An agent at work: in a turn (busy), or its turn ended while it waits on a background command it started, which
    /// wakes it when that finishes (background). In this order: the first outranks the second (foremost).
    static let working: [AgentState] = [.busy, .background]

    /// What one mark shows for these states: the first state of `working` among them, nil when none is at work.
    static func foremost(_ states: [AgentState]) -> AgentState? { working.first(where: states.contains) }

    var label: String {
        switch self {
        case .busy: return "busy"
        case .background: return "background"
        case .waiting: return "waiting on you"
        case .blocked: return "blocked"
        case .idle: return "idle"
        case .stale: return "stale"
        case .offline: return "offline"
        }
    }

    /// Busy is a warm caramel (#B6854D, deepened for white text);
    /// green is reserved for "waiting on you", the one the owner acts on.
    var hex: UInt32 {
        switch self {
        case .busy: return 0xAD7D48
        case .background: return 0x80592F       // the same caramel, darker: only waiting on a background command
        case .waiting: return 0x2EA043
        case .blocked: return 0xDA3633
        case .idle: return 0x2F6FDB
        case .stale: return 0x5B6470
        case .offline: return 0x2B3036
        }
    }

    var color: Color { Color(hex: hex) }
    var nsColor: NSColor { NSColor(hex: hex) }
}

/// Board zoom (⌘+ / ⌘- / ⌘0): every font and tile dimension is multiplied by it.
private struct ZoomKey: EnvironmentKey { static let defaultValue: CGFloat = 1 }

extension EnvironmentValues {
    var zoom: CGFloat {
        get { self[ZoomKey.self] }
        set { self[ZoomKey.self] = newValue }
    }
}

/// A label's icon and title side by side, the gap between them (5 points before zoom) multiplied by `zoom` as every
/// other dimension is: Label's own gap is 8 points at every zoom, so a row of labels zoomed out would outgrow its room.
struct ZoomedLabelStyle: LabelStyle {
    let zoom: CGFloat

    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 5 * zoom) {
            configuration.icon
            configuration.title
        }
    }
}

enum Palette {
    /// held mail from the owner, usage running low
    static let attention = Color(hex: 0xD99A1E)
    /// healthy meters
    static let ok = Color(hex: 0x3D8BD9)
}

/// A colour as the desktop sends it, "#RRGGBB" (agentboard/colours.py); anything else fails the decoding.
struct HexColour: Decodable, Equatable {
    let rgb: UInt32
    var color: Color { Color(hex: rgb) }

    init(rgb: UInt32) { self.rgb = rgb }

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        let text = try c.decode(String.self)
        let digits = text.dropFirst()
        guard text.first == "#", digits.count == 6, digits.allSatisfy(\.isHexDigit), let v = UInt32(digits, radix: 16) else {
            throw DecodingError.dataCorruptedError(in: c, debugDescription: "not a #RRGGBB colour: \(text)")
        }
        rgb = v
    }
}

/// The board groups' colours, as the desktop sends them with the Time page's figures and the usage breakdown
/// (agentboard/colours.py, for_layout): each group's colour and its sub-groups' shades, in layout order, and
/// Ungrouped's. The Mail tab's threads come with their own (MailboxThread). Names are values here, not keys, which the
/// decoder's snake-case rule would change.
struct GroupColours: Decodable, Equatable {
    struct Shade: Decodable, Equatable {
        var name: String
        var colour: HexColour
    }

    struct Group: Decodable, Equatable {
        var name: String
        var colour: HexColour
        var shades: [Shade]
    }

    var groups: [Group]
    var ungrouped: HexColour

    /// A group's colour; Ungrouped's for a name the layout does not hold, the Time page's Unsorted time and the usage
    /// breakdown's.
    func group(_ name: String) -> Color {
        (groups.first { $0.name == name }?.colour ?? ungrouped).color
    }

    /// A sub-group's shade; the group's colour for time in no sub-group (nil), or in a sub-group the layout no longer
    /// holds (the Time page still shows its time).
    func subgroup(_ sub: String?, of group: String) -> Color {
        guard let s = sub, let shade = groups.first(where: { $0.name == group })?.shades.first(where: { $0.name == s })
        else { return self.group(group) }
        return shade.colour.color
    }
}

extension Color {
    init(hex: UInt32, alpha: Double = 1) {
        self.init(.sRGB, red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255, opacity: alpha)
    }
}

extension NSColor {
    convenience init(hex: UInt32) {
        self.init(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
    }
}

enum Fmt {
    /// `text` to show with each line laid out left to right, whatever right-to-left letters it holds: a left-to-right
    /// mark before each line. Laid out by its first strong letter, a line that starts with a right-to-left one (a
    /// Hebrew geresh looks like an apostrophe) would show its words in another order than they come in it. For display
    /// only: what the app sends never carries the marks.
    static func leftToRight(_ text: String) -> String {
        text.split(separator: "\n", omittingEmptySubsequences: false).map { "\u{200E}" + $0 }.joined(separator: "\n")
    }

    static func age(_ seconds: Double?) -> String {
        guard let s = seconds else { return "-" }
        let d = max(0, Int(s))
        if d < 90 { return "\(d)s" }
        if d < 5400 { return "\(d / 60)m" }
        if d < 172800 { return String(format: "%.1fh", Double(d) / 3600) }
        return "\(d / 86400)d"
    }

    static func ago(_ ts: Double?, now: Date) -> String {
        guard let ts = ts, ts > 0 else { return "-" }
        return age(now.timeIntervalSince1970 - ts)
    }

    static func bytes(_ b: Double?) -> String {
        guard let b = b else { return "-" }
        let units = ["B", "K", "M", "G", "T", "P"]
        var v = b, i = 0
        while v >= 1024 && i < units.count - 1 { v /= 1024; i += 1 }
        return v >= 100 || i == 0 ? String(format: "%.0f%@", v, units[i]) : String(format: "%.1f%@", v, units[i])
    }

    /// 45000.4 -> "45,000"
    static func count(_ v: Double) -> String {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.maximumFractionDigits = 0
        return f.string(from: NSNumber(value: v)) ?? String(format: "%.0f", v)
    }

    static func clock(_ ts: Double) -> String {
        let f = DateFormatter()
        let date = Date(timeIntervalSince1970: ts)
        f.dateFormat = Calendar.current.isDateInToday(date) ? "HH:mm" : "EEE HH:mm"
        return f.string(from: date)
    }

    /// "today 14:30", "tomorrow 09:00", "Tue 13 Oct 09:00", plus "(in 3.2h)": when a usage window resets.
    static func resetAt(_ ts: Double?, now: Date) -> String {
        guard let ts = ts else { return "reset time unknown" }
        let date = Date(timeIntervalSince1970: ts)
        let cal = Calendar.current
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        let time = f.string(from: date)
        let day: String
        if cal.isDate(date, inSameDayAs: now) {
            day = "today"
        } else if let t = cal.date(byAdding: .day, value: 1, to: now), cal.isDate(date, inSameDayAs: t) {
            day = "tomorrow"
        } else {
            f.dateFormat = "EEE d MMM"
            day = f.string(from: date)
        }
        return "\(day) \(time) (\(until(ts, now: now)))"
    }

    static func until(_ ts: Double?, now: Date) -> String {
        guard let ts = ts else { return "" }
        let d = ts - now.timeIntervalSince1970
        return d <= 0 ? "now" : "in " + age(d)
    }
}

/// Single-quote a string for a POSIX shell.
func shellQuote(_ s: String) -> String {
    let safe = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./=:@%+-")
    if !s.isEmpty && s.unicodeScalars.allSatisfy({ safe.contains($0) }) { return s }
    return "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
}

/// How much is left of a plan window (UsageWindow.left) or of Codex credits: the colour its meter takes, and, at
/// nearlyOut, a notification (RedRules).
enum UsageLevel {
    case nearlyOut, low, plenty

    init(left: Double) { self = left <= 10 ? .nearlyOut : (left <= 25 ? .low : .plenty) }

    var color: Color {
        switch self {
        case .nearlyOut: return AgentState.blocked.color
        case .low: return Palette.attention
        case .plenty: return Palette.ok
        }
    }
}

func meterTint(_ fraction: Double) -> Color {
    if fraction >= 0.9 { return AgentState.blocked.color }
    if fraction >= 0.75 { return Palette.attention }
    return Palette.ok
}
