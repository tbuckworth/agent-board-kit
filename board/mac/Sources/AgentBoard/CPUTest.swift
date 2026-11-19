import AppKit
import SwiftUI

/// Headless CPU check (--selftest-cpu SNAPSHOT [SECONDS]): the real board views from a saved snapshot in an
/// offscreen window, the run loop left to run, and this process's CPU time over that span. Busy tiles' pulses
/// are the main thing it measures. Nothing is shown, sent or saved.
@MainActor
enum CPUTest {
    static func run(snapshot path: String, seconds: Double) -> Int32 {
        guard let data = FileManager.default.contents(atPath: path),
              let snap = try? JSON.decoder.decode(Snapshot.self, from: data) else {
            print("cannot read a snapshot from \(path)")
            return 1
        }
        let model = BoardModel(layout: Layout(), persist: false)
        model.apply(snap)
        model.link = .live
        let size = NSRect(x: 0, y: 0, width: 1100, height: 700)
        let host = NSHostingView(rootView: BoardView(model: model).frame(width: size.width, height: size.height))
        host.frame = size
        let win = NSWindow(contentRect: size, styleMask: [.borderless], backing: .buffered, defer: false)
        win.contentView = host
        win.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        win.orderBack(nil)
        let tick = Timer(timeInterval: 2, repeats: true) { _ in     // the app's clock: every "ago" label redraws
            Task { @MainActor in model.now = Date() }
        }
        RunLoop.main.add(tick, forMode: .default)
        RunLoop.current.run(until: Date().addingTimeInterval(2))
        let busy = snap.agents.filter { $0.agentState == .busy }.count
        let c0 = cpuSeconds(), w0 = Date()
        RunLoop.current.run(until: Date().addingTimeInterval(seconds))
        let used = cpuSeconds() - c0, wall = Date().timeIntervalSince(w0)
        tick.invalidate()
        print(String(format: "%d busy tiles, clock every 2 s: %.1f%% CPU (%.2f s of CPU in %.1f s)", busy, used / wall * 100, used, wall))
        var seen = Set<ObjectIdentifier>()
        let dots = layers(in: host).filter { $0.name == "pulse" && seen.insert(ObjectIdentifier($0)).inserted }
        if !dots.isEmpty {
            print("pulse dots: \(dots.count), animating in Core Animation: \(dots.filter { $0.animation(forKey: "pulse") != nil }.count)")
        }
        return 0
    }

    /// Every layer under a view (the Core Animation pulse names its layer "pulse").
    static func layers(in view: NSView) -> [CALayer] {
        func walk(_ l: CALayer) -> [CALayer] { [l] + (l.sublayers ?? []).flatMap(walk) }
        return (view.layer.map(walk) ?? []) + view.subviews.flatMap { layers(in: $0) }
    }

    static func cpuSeconds() -> Double {
        var u = rusage()
        getrusage(RUSAGE_SELF, &u)
        return Double(u.ru_utime.tv_sec + u.ru_stime.tv_sec) + Double(u.ru_utime.tv_usec + u.ru_stime.tv_usec) / 1e6
    }
}
