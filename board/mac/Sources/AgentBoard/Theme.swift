import AppKit
import SwiftUI

enum AgentState: String, CaseIterable, Identifiable {
    case busy, background, waiting, blocked, idle, stale, offline
    var id: String { rawValue }

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

    /// Busy is a warm Wes Anderson caramel (Isle of Dogs #B6854D, deepened for white text);
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
    /// States that need the owner's attention float to the menu bar.
    var urgent: Bool { self == .waiting || self == .blocked }
}

/// Board zoom (⌘+ / ⌘- / ⌘0): every font and tile dimension is multiplied by it.
private struct ZoomKey: EnvironmentKey { static let defaultValue: CGFloat = 1 }

extension EnvironmentValues {
    var zoom: CGFloat {
        get { self[ZoomKey.self] }
        set { self[ZoomKey.self] = newValue }
    }
}

enum Palette {
    /// held mail from the owner, usage running low
    static let attention = Color(hex: 0xD99A1E)
    /// healthy meters
    static let ok = Color(hex: 0x3D8BD9)
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

func meterTint(_ fraction: Double) -> Color {
    if fraction >= 0.9 { return AgentState.blocked.color }
    if fraction >= 0.75 { return Palette.attention }
    return Palette.ok
}
