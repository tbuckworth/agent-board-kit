import AppKit
import SwiftUI

// ToDo                                     the menu-bar app
// ToDo --render DATA.json OUT.png [W H] [--light] [--list NAME] [--select N] [--edit N] [--assign N]
//                                          [--agents] [--search WORDS] [--zoom Z] [--running]
//                                          (--running: the Assign sheet on "A running agent", with sample agents)
//                                          draw the real views offscreen from a todo-desk snapshot or the app's
//                                          cache, into a PNG, and exit (screenshots without screen recording)
// ToDo --probe                             connect exactly as the app does, print what arrives, and exit
// ToDo --selftest                          check the body, label and queue logic offline
// ToDo --selftest-remote                   create, edit, complete and delete a dummy task on GitHub through
//                                          the real link, including edits made while disconnected
// ToDo --assign-test                      make a dummy task and press Assign to agent on it (starts a tiny agent)
// ToDo --icon DIR.iconset                  draw the app icon at every iconset size

// A write to the ssh pipe just after ssh exits must fail quietly (EPIPE), not kill the app with SIGPIPE.
signal(SIGPIPE, SIG_IGN)

let args = CommandLine.arguments
func flag(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), args.count > i + 1 else { return nil }
    return args[i + 1]
}

if let i = args.firstIndex(of: "--render"), args.count > i + 2 {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let rest = Array(args[(i + 3)...])
    let nums = rest.prefix(2).compactMap { Double($0) }
    let size = CGSize(width: nums.count > 0 ? nums[0] : 1220, height: nums.count > 1 ? nums[1] : 780)
    let code = MainActor.assumeIsolated {
        Renderer.render(data: args[i + 1], to: args[i + 2], size: size, dark: !rest.contains("--light"),
                        list: flag("--list"), select: flag("--select").flatMap { Int($0) },
                        edit: flag("--edit").flatMap { Int($0) }, assign: flag("--assign").flatMap { Int($0) },
                        agents: rest.contains("--agents"), search: flag("--search"),
                        zoom: flag("--zoom").flatMap { Double($0) }.map { CGFloat($0) } ?? 1,
                        running: rest.contains("--running"))
    }
    exit(code)
}
if args.contains("--selftest") {
    let code = MainActor.assumeIsolated { SelfTest.run() }
    exit(code)
}
if args.contains("--selftest-clicks") {
    NSApplication.shared.setActivationPolicy(.accessory)
    let code = MainActor.assumeIsolated { ClickTest.run() }
    exit(code)
}
if args.contains("--selftest-remote") {
    let code = MainActor.assumeIsolated { RemoteTest.run() }
    exit(code)
}
if args.contains("--assign-test") {
    let code = MainActor.assumeIsolated { AssignTest.run() }
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
app.setActivationPolicy(.regular)     // always in the Dock and ⌘Tab; the menu-bar item stays too
app.run()
