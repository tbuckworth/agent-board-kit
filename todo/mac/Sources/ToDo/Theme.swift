import AppKit
import SwiftUI

/// AgentBoard's colours, so the two apps read as one family. Caramel means the same thing in both:
/// an agent is working on it.
enum Palette {
    static let caramel = Color(hex: 0xAD7D48)
    static let caramelDark = Color(hex: 0x80592F)
    static let caramelLift = NSColor(hex: 0xC99A66)       // the caramel, lifted for the menu bar
    static let red = Color(hex: 0xDA3633)
    static let green = Color(hex: 0x2EA043)
    static let blue = Color(hex: 0x2F6FDB)
    static let slate = Color(hex: 0x5B6470)
    static let attention = Color(hex: 0xD99A1E)
    static let ok = Color(hex: 0x3D8BD9)
    static let tag = Color(hex: 0x7A5BD6)                  // tags, near the tag: labels' 5319E7

    /// An agent's state in AgentBoard's colours: caramel busy, darker caramel waiting on a background command,
    /// green waiting on you, red blocked, blue idle, grey stale.
    static func agentState(_ state: String) -> Color {
        switch state {
        case "busy": return caramel
        case "background": return caramelDark
        case "waiting": return green
        case "blocked": return red
        case "idle": return blue
        default: return slate
        }
    }
}

/// Zoom (⌘+ / ⌘- / ⌘0): every font and fixed dimension is multiplied by it.
private struct ZoomKey: EnvironmentKey { static let defaultValue: CGFloat = 1 }

extension EnvironmentValues {
    var zoom: CGFloat {
        get { self[ZoomKey.self] }
        set { self[ZoomKey.self] = newValue }
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
    static let isoParser: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    static func date(_ iso: String?) -> Date? {
        guard let s = iso else { return nil }
        return isoParser.date(from: s)
    }

    /// "now", "12m", "5h", "3d", "8w", "2y": short enough for the end of a row.
    static func age(_ iso: String?, now: Date = Date()) -> String {
        guard let d = date(iso) else { return "" }
        let s = max(0, now.timeIntervalSince(d))
        if s < 60 { return "now" }
        if s < 3600 { return "\(Int(s / 60))m" }
        if s < 86400 { return "\(Int(s / 3600))h" }
        if s < 86400 * 60 { return "\(Int(s / 86400))d" }
        if s < 86400 * 730 { return "\(Int(s / 86400 / 7))w" }
        return "\(Int(s / 86400 / 365))y"
    }

    static func ago(_ iso: String?, now: Date = Date()) -> String {
        let a = age(iso, now: now)
        return a.isEmpty ? "" : (a == "now" ? "just now" : a + " ago")
    }

    static func day(_ iso: String?) -> String {
        guard let d = date(iso) else { return "" }
        let f = DateFormatter()
        f.dateFormat = Calendar.current.isDate(d, equalTo: Date(), toGranularity: .year) ? "d MMM" : "d MMM yyyy"
        return f.string(from: d)
    }

    static func clock(_ iso: String?) -> String {
        guard let d = date(iso) else { return "" }
        let f = DateFormatter()
        f.dateFormat = Calendar.current.isDateInToday(d) ? "HH:mm" : "d MMM HH:mm"
        return f.string(from: d)
    }
}

/// "to-buy" -> "To Buy", the way the web app names lists.
func titleCase(_ s: String) -> String {
    s.split(whereSeparator: { $0 == "-" || $0 == "_" || $0 == " " })
        .map { $0.prefix(1).uppercased() + $0.dropFirst().lowercased() }
        .joined(separator: " ")
}

/// Single-quote a string for a POSIX shell.
func shellQuote(_ s: String) -> String {
    let safe = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./=:@%+-")
    if !s.isEmpty && s.unicodeScalars.allSatisfy({ safe.contains($0) }) { return s }
    return "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
}

func copyToPasteboard(_ s: String) {
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(s, forType: .string)
}
