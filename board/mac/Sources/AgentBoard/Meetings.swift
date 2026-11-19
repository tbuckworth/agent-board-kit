import AppKit
import Carbon
import SwiftUI

/// The two alerts before a meeting: a notification notify_minutes before it starts, then a full-screen overlay
/// fullscreen_minutes before (the snapshot's meetings section gives both).
enum MeetingAlertKind: String {
    case notify, fullscreen
}

/// When each alert is due, and the words for it. Pure, for --selftest-meetings.
enum MeetingRules {
    /// A meeting that started longer ago than this gets no alert (a Mac that wakes or an app that starts mid-meeting).
    static let lateGrace: TimeInterval = 120

    /// One alert of one meeting. A meeting moved to another time is alerted for again.
    static func key(_ m: Meeting, _ kind: MeetingAlertKind) -> String { "\(m.key)|\(kind.rawValue)" }

    /// The alerts to give at `now` that are not in `fired`: each kind is due from its alert point (the start minus its
    /// lead) until lateGrace after the start, so a Mac that wakes, or an app that starts, after the alert point still
    /// gives it. When both are due at once, only the overlay.
    static func due(_ info: MeetingsInfo, now: Double, fired: Set<String>) -> [(Meeting, MeetingAlertKind)] {
        var out: [(Meeting, MeetingAlertKind)] = []
        for m in info.events where now < m.start + lateGrace {
            if now >= m.start - info.fullscreenMinutes * 60 && !fired.contains(key(m, .fullscreen)) {
                out.append((m, .fullscreen))
            } else if now >= m.start - info.notifyMinutes * 60 && !fired.contains(key(m, .notify)) {
                out.append((m, .notify))
            }
        }
        return out
    }

    /// "Starts in 10 min", "Starting now", "Started 1 min ago".
    static func toGo(_ start: Double, now: Double) -> String {
        let s = start - now
        if s >= 15 { return "Starts in \(Int((s / 60).rounded(.up))) min" }
        if s > -60 { return "Starting now" }
        return "Started \(Int(-s / 60)) min ago"
    }

    /// "10:00 – 10:30"; the end names its day only when that is not the start's.
    static func span(_ from: Double, _ to: Double) -> String {
        let start = Date(timeIntervalSince1970: from), end = Date(timeIntervalSince1970: to)
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        let until = Calendar.current.isDate(end, inSameDayAs: start) ? f.string(from: end) : Fmt.clock(to)
        return "\(Fmt.clock(from)) – \(until)"
    }

    /// "10:00 – 10:30 · Personal calendar".
    static func when(_ m: Meeting) -> String { "\(span(m.start, m.end)) · \(m.calendar) calendar" }

    /// The notification: the meeting's name, how long to go and when; a click joins it or opens its calendar.
    static func notification(_ m: Meeting, now: Double) -> RedItem {
        RedItem(key: "meeting:" + key(m, .notify), title: m.name,
                body: [toGo(m.start, now: now), when(m)].joined(separator: " · "),
                agent: nil, link: AlertLink(url: m.primary.url, account: m.account))
    }
}

/// The meeting alerts. It keeps the last meetings the desktop sent, so a short disconnect loses nothing, and checks
/// them every `interval` seconds and when the Mac wakes. Each alert is given once per meeting id, start and kind;
/// what has been given is kept in the app's defaults (`savedKey`) for a day, so a relaunch doesn't repeat it.
@MainActor
final class MeetingAlerts {
    nonisolated static let savedKey = "meetingAlertsFired"
    nonisolated static let interval: TimeInterval = 15

    private(set) var info: MeetingsInfo?
    private var fired: [String: Double]           // MeetingRules.key -> its meeting's start
    private let save: ([String: Double]) -> Void
    private let post: (RedItem) -> Void
    private let show: (Meeting) -> Void
    private var timer: Timer?

    init(saved: [String: Double] = UserDefaults.standard.dictionary(forKey: MeetingAlerts.savedKey) as? [String: Double] ?? [:],
         save: (([String: Double]) -> Void)? = nil, post: @escaping (RedItem) -> Void, show: @escaping (Meeting) -> Void) {
        fired = saved
        self.save = save ?? { UserDefaults.standard.set($0, forKey: MeetingAlerts.savedKey) }
        self.post = post
        self.show = show
    }

    /// Check every `interval` seconds and when the Mac wakes.
    func start() {
        let t = Timer(timeInterval: MeetingAlerts.interval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.check(now: Date()) }
        }
        t.tolerance = 3
        RunLoop.main.add(t, forMode: .common)
        timer = t
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.check(now: Date()) }
        }
    }

    /// The desktop's newest meetings, replacing the last; then a check at `now`.
    func take(_ info: MeetingsInfo, now: Date) {
        self.info = info
        check(now: now)
    }

    /// Give the alerts due at `now` (MeetingRules.due). An overlay also counts as the meeting's notification.
    func check(now: Date) {
        guard let info = info else { return }
        let t = now.timeIntervalSince1970
        let due = MeetingRules.due(info, now: t, fired: Set(fired.keys))
        guard !due.isEmpty else { return }
        for (m, kind) in due {
            for k in kind == .fullscreen ? [MeetingAlertKind.notify, .fullscreen] : [.notify] {
                fired[MeetingRules.key(m, k)] = m.start
            }
        }
        fired = fired.filter { $0.value > t - 86400 }
        save(fired)
        for (m, kind) in due {
            switch kind {
            case .notify: post(MeetingRules.notification(m, now: t))
            case .fullscreen: show(m)
            }
        }
    }
}

/// The full-screen overlay: a panel over each screen, above every window and the menu bar, on every Space and over
/// full-screen apps. It takes the keyboard without making the app active, so Esc reaches it and the app the owner was
/// in stays in front. A meeting due while one is showing waits its turn, unless it has started over lateGrace ago by then.
@MainActor
final class MeetingOverlay {
    private var panels: [OverlayPanel] = []
    private var waiting: [Meeting] = []
    private var escape: Any?
    private let open: (AlertLink) -> Void

    /// `open`: what Join and Open calendar run (BoardModel.openMeeting).
    init(open: @escaping (AlertLink) -> Void) {
        self.open = open
    }

    func show(_ m: Meeting) {
        if panels.isEmpty { present(m) } else { waiting.append(m) }
    }

    private func present(_ m: Meeting) {
        let view = MeetingOverlayView(meeting: m, onPrimary: { [weak self] in
            self?.open(AlertLink(url: m.primary.url, account: m.account))
            self?.dismiss()
        }, onDismiss: { [weak self] in self?.dismiss() })
        for screen in NSScreen.screens {
            let p = OverlayPanel(frame: screen.frame)
            p.contentView = FirstClickHostingView(rootView: view)
            p.alphaValue = 0
            p.orderFrontRegardless()
            panels.append(p)
        }
        (panels.first { $0.screen == NSScreen.main } ?? panels.first)?.makeKey()
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.25
            for p in panels { p.animator().alphaValue = 1 }
        }
        escape = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            guard e.keyCode == UInt16(kVK_Escape), e.window is OverlayPanel else { return e }
            self?.dismiss()
            return nil
        }
    }

    private func dismiss() {
        for p in panels { p.orderOut(nil) }
        panels = []
        if let m = escape { NSEvent.removeMonitor(m) }
        escape = nil
        let now = Date().timeIntervalSince1970
        waiting.removeAll { now >= $0.start + MeetingRules.lateGrace }
        if !waiting.isEmpty { present(waiting.removeFirst()) }
    }
}

/// A borderless panel the size of a screen: above every window, the menu bar's included, on every Space and over
/// full-screen apps, and able to take the keyboard without activating the app (nonactivatingPanel).
final class OverlayPanel: NSPanel {
    init(frame: NSRect) {
        super.init(contentRect: frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        level = .screenSaver
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        isFloatingPanel = true
        hidesOnDeactivate = false
        isReleasedWhenClosed = false
        isOpaque = false
        backgroundColor = .clear
        hasShadow = false
    }

    override var canBecomeKey: Bool { true }
}

/// A click on a button goes through even while another app is active (the first click would otherwise only focus).
private final class FirstClickHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

/// What the overlay shows on each screen: how long to go, the meeting, when, and Join (or Open calendar) and
/// Dismiss.
struct MeetingOverlayView: View {
    let meeting: Meeting
    let onPrimary: () -> Void
    let onDismiss: () -> Void

    var body: some View {
        TimelineView(.periodic(from: .now, by: 5)) { ctx in
            content(now: ctx.date.timeIntervalSince1970)
        }
    }

    private func content(now: Double) -> some View {
        ZStack {
            Color(nsColor: .windowBackgroundColor).opacity(0.94)
            VStack(spacing: 0) {
                HStack(spacing: 12) {
                    PulseCircle(color: AgentState.blocked.nsColor).frame(width: 14, height: 14)
                    Text(MeetingRules.toGo(meeting.start, now: now).uppercased())
                        .font(.system(size: 18, weight: .semibold)).tracking(1.6)
                        .foregroundColor(AgentState.blocked.color)
                }
                Text(meeting.name)
                    .font(.system(size: 56, weight: .bold))
                    .multilineTextAlignment(.center).lineLimit(3).minimumScaleFactor(0.6)
                    .padding(.top, 26)
                Text(MeetingRules.when(meeting))
                    .font(.system(size: 24, weight: .medium).monospacedDigit())
                    .foregroundStyle(.secondary)
                    .padding(.top, 16)
                HStack(spacing: 18) {
                    Button(meeting.primary.label, action: onPrimary).buttonStyle(OverlayButtonStyle(prominent: true))
                    Button("Dismiss", action: onDismiss).buttonStyle(OverlayButtonStyle(prominent: false))
                }
                .padding(.top, 48)
                Text("Esc dismisses").font(.system(size: 13)).foregroundStyle(.tertiary).padding(.top, 18)
            }
            .frame(maxWidth: 900)
            .padding(60)
        }
    }
}

private struct OverlayButtonStyle: ButtonStyle {
    var prominent: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 20, weight: .semibold))
            .foregroundColor(prominent ? .white : .primary)
            .frame(minWidth: 150)
            .padding(.horizontal, 30).padding(.vertical, 14)
            .background(RoundedRectangle(cornerRadius: 12)
                .fill(prominent ? AgentState.waiting.color : Color.primary.opacity(0.1)))
            .opacity(configuration.isPressed ? 0.75 : 1)
            .contentShape(RoundedRectangle(cornerRadius: 12))
    }
}

/// --selftest-meetings: the meeting alerts' timing, once each, catch-up and late meetings, the title for a meeting
/// without one and the overlay's main button; then the section as the desktop sends it: fields the app does not use,
/// and a snapshot without the section (the desktop has no Google login), which gives no alerts. Offline; nothing is
/// posted or shown.
@MainActor
enum MeetingsTest {
    static func run() -> Int32 {
        var fails = 0
        func check(_ ok: Bool, _ what: String) { print((ok ? "ok    " : "FAIL  ") + what); if !ok { fails += 1 } }
        let t0 = 1_800_000_000.0
        let section = """
        {"notify_minutes": 10, "fullscreen_minutes": 1, "events": [
         {"calendar": "Personal", "id": "standup", "title": "Standup", "start": \(t0), "end": \(t0 + 900),
          "join_url": "https://meet.google.com/abc-defg-hij", "account": "owner@example.com",
          "open_url": "https://calendar.google.com/calendar/r/day/2027/1/15?authuser=owner%40example.com"},
         {"calendar": "Work", "id": "review", "title": null, "start": \(t0 + 3600), "end": \(t0 + 5400),
          "join_url": null, "account": "work@example.com",
          "open_url": "https://calendar.google.com/calendar/r/day/2027/1/15?authuser=work%40example.com"},
         {"calendar": "Work", "id": 7}]}
        """
        guard let info = try? JSON.decoder.decode(MeetingsInfo.self, from: Data(section.utf8)) else {
            print("meetings: the section did not decode")
            return 1
        }
        check(info.events.map { $0.id } == ["standup", "review"] && info.dropped.count == 1,
              "the section decodes, without the meeting that does not")
        let standup = info.events[0], review = info.events[1]
        check(standup.name == "Standup" && review.name == "Work meeting", "no title: \"<calendar> meeting\"")
        check(standup.primary.label == "Join" && standup.primary.url == standup.joinUrl, "a video link: Join")
        check(review.primary.label == "Open calendar" && review.primary.url == review.openUrl, "no video link: Open calendar")

        var posted: [RedItem] = [], shown: [String] = [], saved: [String: Double] = [:]
        func alerts(_ s: [String: Double] = [:]) -> MeetingAlerts {
            MeetingAlerts(saved: s, save: { saved = $0 }, post: { posted.append($0) }, show: { shown.append($0.id) })
        }
        func at(_ a: MeetingAlerts, _ t: Double) { a.check(now: Date(timeIntervalSince1970: t)) }

        let a = alerts()
        a.take(info, now: Date(timeIntervalSince1970: t0 - 3600))
        at(a, t0 - 601)
        check(posted.isEmpty && shown.isEmpty, "before the alert point: nothing")
        at(a, t0 - 600)
        check(posted.map { $0.title } == ["Standup"], "at start - notify_minutes: a notification")
        check(posted.first?.body.hasPrefix("Starts in 10 min · ") == true &&
              posted.first?.link == AlertLink(url: standup.joinUrl ?? "", account: "owner@example.com"),
              "it says how long to go, and a click joins (\(posted.first?.body ?? ""))")
        at(a, t0 - 585)
        check(posted.count == 1, "the notification comes once")
        at(a, t0 - 60)
        check(shown == ["standup"] && posted.count == 1, "at start - fullscreen_minutes: the overlay")
        at(a, t0 - 45)
        check(shown.count == 1, "the overlay comes once")
        let relaunched = alerts(saved)
        relaunched.take(info, now: Date(timeIntervalSince1970: t0 - 30))
        check(posted.count == 1 && shown.count == 1, "after a relaunch: not again")

        let woke = alerts()
        woke.take(info, now: Date(timeIntervalSince1970: t0 + 3600 - 300))
        check(posted.count == 2 && posted.last?.title == "Work meeting" &&
              posted.last?.link == AlertLink(url: review.openUrl, account: "work@example.com") && shown.count == 1,
              "catch-up after the notification's point: it comes then; a click opens the calendar")
        let late = alerts()
        late.take(info, now: Date(timeIntervalSince1970: t0 - 20))
        check(shown.count == 2 && posted.count == 2, "catch-up inside the last minute: the overlay only")
        at(late, t0 - 10)
        check(posted.count == 2, "and its notification counts as given")
        let started = alerts()
        started.take(info, now: Date(timeIntervalSince1970: t0 + 90))
        check(shown.count == 3, "started 90 s ago: still the overlay")
        let past = alerts()
        past.take(info, now: Date(timeIntervalSince1970: t0 + 121))
        check(shown.count == 3 && posted.count == 2, "started over 2 min ago: nothing")

        let kept = alerts()
        kept.take(info, now: Date(timeIntervalSince1970: t0 - 3600))
        at(kept, t0 - 600)                                  // no new section since: the kept meetings still alert
        check(posted.count == 3, "the last meetings received keep alerting")
        var moved = info
        moved.events[0].start = t0 + 2 * 86400
        kept.take(moved, now: Date(timeIntervalSince1970: t0 + 2 * 86400 - 600))
        check(posted.count == 4 && posted.last?.title == "Standup", "a meeting moved to a new time alerts again")
        check(saved == [MeetingRules.key(moved.events[0], .notify): t0 + 2 * 86400],
              "what was given is kept for a day (\(saved.count) kept)")

        check(MeetingRules.toGo(t0, now: t0 - 600) == "Starts in 10 min" && MeetingRules.toGo(t0, now: t0 - 59) == "Starts in 1 min"
              && MeetingRules.toGo(t0, now: t0) == "Starting now" && MeetingRules.toGo(t0, now: t0 + 90) == "Started 1 min ago",
              "how long to go")
        let end = MeetingRules.when(standup).components(separatedBy: " – ").last ?? ""
        check(end.range(of: #"^\d\d:\d\d · Personal calendar$"#, options: .regularExpression) != nil,
              "an end on the start's day names no day (\(MeetingRules.when(standup)))")
        fails += sections(t0)
        print(fails == 0 ? "meetings OK" : "meetings: \(fails) FAILED")
        return fails == 0 ? 0 : 1
    }

    /// The section as the desktop sends it: a meeting with fields this app does not use (a newer or older
    /// collector's), and a snapshot without the section, which feeds no alerts; how many failed.
    static func sections(_ t0: Double) -> Int {
        var fails = 0
        func check(_ ok: Bool, _ what: String) { print((ok ? "ok    " : "FAIL  ") + what); if !ok { fails += 1 } }
        let extra = """
        {"notify_minutes": 10, "fullscreen_minutes": 1, "events": [
         {"calendar": "Work", "id": "plan", "title": "Planning", "start": \(t0), "end": \(t0 + 1800), "join_url": null,
          "account": "work@example.com", "open_url": "https://calendar.google.com/calendar/r/day",
          "location": "Room 1", "invited": 4, "x": {"y": 1}}]}
        """
        let info = try? JSON.decoder.decode(MeetingsInfo.self, from: Data(extra.utf8))
        check(info?.events.map { $0.id } == ["plan"] && info?.dropped.isEmpty == true,
              "a meeting with fields the app does not use decodes, and they are ignored")
        if let m = info?.events.first {
            check(MeetingRules.notification(m, now: t0 - 600).body.hasSuffix(" · Work calendar"),
                  "the notification: how long to go and when (\(MeetingRules.notification(m, now: t0 - 600).body))")
        }
        let bare = #"{"generated": \#(t0), "agents": [], "timeline": [], "mail": {"held": [], "held_count": 0}}"#
        let snap = try? JSON.decoder.decode(Snapshot.self, from: Data(bare.utf8))
        check(snap != nil && snap?.meetings == nil && snap?.problems.isEmpty == true,
              "a snapshot without the section (no Google login on the desktop): no meetings, and nothing wrong")
        var posted = 0, shown = 0
        let idle = MeetingAlerts(saved: [:], save: { _ in }, post: { _ in posted += 1 }, show: { _ in shown += 1 })
        idle.check(now: Date(timeIntervalSince1970: t0 - 30))
        check(posted == 0 && shown == 0, "with no section ever received: no alerts")
        return fails
    }
}
