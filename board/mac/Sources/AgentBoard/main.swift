import AppKit
import SwiftUI

// AgentBoard                               the menu-bar app
// AgentBoard --render SNAPSHOT.json OUT.png [WIDTH HEIGHT] [--light] [--select ID-PREFIX] [--overview]
//                                          draw the board from a saved snapshot into a PNG and exit
//                                          (screenshots without screen-recording permission)
// AgentBoard --probe                       connect exactly as the app does, print what arrives, run a
//                                          read-only check_close on one agent, and exit (smoke test)
// AgentBoard --icon DIR.iconset            draw the app icon at every iconset size

let args = CommandLine.arguments
if let i = args.firstIndex(of: "--render"), args.count > i + 2 {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let rest = Array(args[(i + 3)...])
    let nums = rest.prefix(2).compactMap { Double($0) }
    let size = CGSize(width: nums.count > 0 ? nums[0] : 1100, height: nums.count > 1 ? nums[1] : 700)
    var select: String?
    if let s = rest.firstIndex(of: "--select"), rest.count > s + 1 { select = rest[s + 1] }
    var layoutFile: String?
    if let s = rest.firstIndex(of: "--layout"), rest.count > s + 1 { layoutFile = rest[s + 1] }
    var zoom: CGFloat = 1
    if let s = rest.firstIndex(of: "--zoom"), rest.count > s + 1, let z = Double(rest[s + 1]) { zoom = CGFloat(z) }
    let code = MainActor.assumeIsolated {
        Renderer.render(snapshot: args[i + 1], to: args[i + 2], size: size, dark: !rest.contains("--light"),
                        select: select, overview: rest.contains("--overview"), zoom: zoom, layoutFile: layoutFile)
    }
    exit(code)
}

if args.contains("--selftest-keys") {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { KeyTest.run() }
    exit(code)
}
if let i = args.firstIndex(of: "--render-restart"), args.count > i + 2 {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { Renderer.renderRestart(plan: args[i + 1], to: args[i + 2]) }
    exit(code)
}
if let i = args.firstIndex(of: "--render-switch"), args.count > i + 3 {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { Renderer.renderSwitch(options: args[i + 1], snapshot: args[i + 2], to: args[i + 3]) }
    exit(code)
}
if let i = args.firstIndex(of: "--selftest-kitty"), args.count > i + 1 {
    exit(Kitty.selftest(snapshot: args[i + 1]))
}
if args.contains("--selftest-layout") {
    let code = MainActor.assumeIsolated { LayoutTest.run() }
    exit(code)
}
if args.contains("--probe") {
    let code = MainActor.assumeIsolated { Probe.run() }
    exit(code)
}
if let i = args.firstIndex(of: "--icon"), args.count > i + 1 {
    _ = NSApplication.shared
    let code = MainActor.assumeIsolated { Renderer.icon(into: args[i + 1]) }
    exit(code)
}

let app = NSApplication.shared
let delegate = MainActor.assumeIsolated { AppDelegate() }
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
