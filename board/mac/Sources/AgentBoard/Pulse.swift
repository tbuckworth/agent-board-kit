import AppKit
import SwiftUI

/// The pulsing dot on a busy tile. Core Animation runs the pulse in the window server, so the app does no work
/// per frame; a SwiftUI `repeatForever` animation would re-run the board's view update and a Core Animation commit
/// on every frame.
struct PulseDot: View {
    @Environment(\.zoom) private var z
    var body: some View {
        PulseCircle(color: .white).frame(width: 6 * z, height: 6 * z)
    }
}

/// A pulsing dot of `color` that fills its frame: the busy tile's, and the meeting overlay's.
struct PulseCircle: View {
    var color: NSColor
    var body: some View {
        PulseLayer(color: color).allowsHitTesting(false)
    }
}

private struct PulseLayer: NSViewRepresentable {
    var color: NSColor
    func makeNSView(context: Context) -> PulseNSView { PulseNSView(color: color) }
    func updateNSView(_ view: PulseNSView, context: Context) {}
}

final class PulseNSView: NSView {
    private var color = NSColor.white

    convenience init(color: NSColor) {
        self.init(frame: .zero)
        self.color = color
    }

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        wantsLayer = true
    }

    override func hitTest(_ point: NSPoint) -> NSView? { nil }     // clicks and drags belong to the tile

    /// Drawn once into the layer; also what an offscreen render (cacheDisplay) sees.
    override func draw(_ dirtyRect: NSRect) {
        color.setFill()
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
