import AppKit
import SwiftUI

/// The pulsing dot on a task an agent is working on (the agent board's view). Core Animation runs the pulse in
/// the window server, so the app does no work per frame. A SwiftUI `repeatForever` animation re-ran the view
/// update and a Core Animation commit every frame: it held the agent board at 35% CPU, and a board offscreen with
/// two busy tiles at a full core.
struct PulseDot: View {
    @Environment(\.zoom) private var z
    var body: some View {
        PulseLayer().frame(width: 6 * z, height: 6 * z).allowsHitTesting(false)
    }
}

private struct PulseLayer: NSViewRepresentable {
    func makeNSView(context: Context) -> PulseNSView { PulseNSView() }
    func updateNSView(_ view: PulseNSView, context: Context) {}
}

final class PulseNSView: NSView {
    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        wantsLayer = true
    }

    override func hitTest(_ point: NSPoint) -> NSView? { nil }     // clicks and drags belong to the row

    /// Drawn once into the layer; also what an offscreen render (cacheDisplay) sees.
    override func draw(_ dirtyRect: NSRect) {
        NSColor.white.setFill()
        NSBezierPath(ovalIn: bounds).fill()
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        guard window != nil, let layer = layer, layer.animation(forKey: "pulse") == nil else { return }
        layer.name = "pulse"
        let a = CABasicAnimation(keyPath: "opacity")
        a.fromValue = 0.3
        a.toValue = 1.0
        a.duration = 0.9
        a.autoreverses = true
        a.repeatCount = .infinity
        a.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        a.isRemovedOnCompletion = false
        layer.add(a, forKey: "pulse")
    }
}
