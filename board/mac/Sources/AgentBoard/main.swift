import AppKit
import SwiftUI

// The app's command-line flags, all of them. Every mode but the first two does its work and exits.
//
// AgentBoard                               the menu-bar app
// AgentBoard --hidden                      the same, starting in the menu bar only (the login item)
// AgentBoard --render SNAPSHOT.json OUT.png [WIDTH HEIGHT] [--light] [--select ID-PREFIX] [--overview]
//            [--layout LAYOUT.json] [--zoom FACTOR]
//                                          draw the board from a saved snapshot into a PNG (screenshots without
//                                          screen-recording permission), grouped by a saved layout, zoomed
// AgentBoard --render-time TIME.json OUT.png [--group NAME [--sub NAME]] [--day YYYY-MM-DD] [--item TITLE]
//                          [--range 1|7|14|30] [--hours all|work|off] [--light]
//                                          draw the Time sheet from saved figures (`agent-board time --json`) into
//                                          a PNG, narrowed to a group, sub-group and day, with an item's breakdown
//                                          open, over a range of days (1: the one-day view), counting all time, work
//                                          time or the rest (default all)
// AgentBoard --render-usage SPLIT.json OUT.png [--light]
//                                          draw the usage breakdown sheet from a saved usage_split answer's "split"
// AgentBoard --render-meeting OUT.png [--untitled] [--light]
//                                          draw the meeting overlay for one screen into a PNG, for a meeting a minute
//                                          away: one with a title and a video link, or with --untitled a free/busy one
// AgentBoard --render-mail LIST.json SHOW.json [SHOW.json ...] OUT.png [--light]
//                                          draw the Mail tab from a saved mail_list answer after opening the threads of
//                                          saved mail_show answers in turn and the first again (its document captured
//                                          from WebKit), printing how long each took to reach WebKit's page and how
//                                          many of its widgets (options and plots) were drawn
// AgentBoard --render-restart PLAN.json OUT.png
//                                          draw the restart sheet from a saved plan (`agent-restart plan --json`)
// AgentBoard --render-switch OPTIONS.json SNAPSHOT.json OUT.png
//                                          draw the switch sheet for the snapshot's first live agent, from options
//                                          saved by --probe (below)
// AgentBoard --probe                       connect exactly as the app does, print what arrives, run read-only
//                                          actions on one agent, the Chats sheet's reads and the Mail tab's (the
//                                          inbox and its newest thread, which marks nothing) (smoke test); with
//                                          AGENTBOARD_SAVE_SWITCH=FILE in the environment, it also writes that
//                                          agent's switch options to FILE
// AgentBoard --icon DIR.iconset            draw the app icon at every iconset size (mac/build-app.sh)
// AgentBoard --selftest-alerts SNAPSHOT.json  the red rules and once-per-red notifying, offline (posts nothing)
// AgentBoard --selftest-decode             the snapshot's problem list (the desktop's, then what does not decode)
//                                          and the reading of an action's answer, offline
// AgentBoard --selftest-chats              group-chat post ids and which chats count as new, offline
// AgentBoard --selftest-mail               the Mail tab's rules (snooze times, the list's changes and times, links
//                                          and the document view's navigations, attachment files, reply keys) and
//                                          what it asks the desktop for when a thread is opened, archived, stepped
//                                          through or answered, against saved answers; offline
// AgentBoard --selftest-widgets            the options and plots agents put in mail, drawn by widgets.js in the
//                                          document view offscreen: the cards, the plot, a pick's reply line, a
//                                          widget that does not check out
// AgentBoard --selftest-meetings           when each meeting alert comes, once, with catch-up and none for meetings
//                                          long started; a meeting's title and main button; the meetings section as
//                                          the desktop sends it, or none, offline (posts and shows nothing)
// AgentBoard --selftest-cpu SNAPSHOT.json [SECONDS]
//                                          the board offscreen for SECONDS (10): the CPU it used
// AgentBoard --selftest-keys               the window's zoom shortcuts and ⌘↩ in a dialog's text box, offscreen
// AgentBoard --selftest-layout             the group and sub-group operations, reading and saving a layout file
//                                          (a temporary one), and that the saved layout still loads
// AgentBoard --selftest-kitty SNAPSHOT.json
//                                          print what Open in Kitty and the latest-mail link would run for each
//                                          live agent; a dry run that opens nothing and checks nothing
// AgentBoard --selftest-screen             the screen-time block rules, offline
// AgentBoard --selftest-usage             the usage breakdown's chart bands and colours, offline
// AgentBoard --selftest-time               the Time sheet's one-day lanes, hour span and block runs, and its
//                                          work-time choice, offline
// AgentBoard --screen-preview HOURS        the screen-time blocks of the last HOURS hours from ActivityWatch,
//                                          printed, not uploaded (needs the activityWatch setting, Remote.swift)
//
// The sources:
// Alerts.swift      what counts as red on the board, and a notification for each new red thing
// Allowance.swift   where a plan window's allowance went: the sheet a usage meter opens, --selftest-usage
// App.swift         the app delegate: menu-bar item, window, global hotkey, menus and window shortcuts
// Board.swift       BoardModel, the board's state and actions, and the Mac-only layout of tiles in groups
// Browser.swift     opening a Google link (Gmail, a meeting) in the Chrome profile signed in to its account
// Chats.swift       group chats with agents: the top bar's Chats button, the chat sheet, New chat, --selftest-chats
// Compose.swift     a mail reply's box, with files dropped or pasted in, and the draft that keeps its key
// CPUTest.swift     --selftest-cpu
// Dialogs.swift     the confirmations and text dialogs every action on the desktop goes through
// Kitty.swift       Open in Kitty: a terminal attached to an agent's session on the desktop
// Mail.swift        the Mail tab: the mailroom's threads, a thread with its reply box, snoozing, --selftest-mail
// MailDrafts.swift  your unsent replies, kept on the Mac with their files across restarts
// MailHTML.swift    a thread's HTML document in a locked-down WKWebView; its links and attachments
// MailWidgets.swift the options and plots in agents' mail, drawn in the document by widgets.js; --selftest-widgets
// main.swift        these flags and the dispatch to each mode
// Meetings.swift    meeting alerts: a notification, then a full-screen overlay, before each meeting; --selftest-meetings
// Model.swift       the collector's snapshot as Swift types, decoded leniently (--selftest-decode)
// Pulse.swift       the pulsing dot on a busy tile and on the meeting overlay
// Remote.swift      settings, the SSH stream from the collector, and the actions sent to the desktop
// Render.swift      the offscreen renders, --probe, --selftest-keys and --selftest-layout
// Restart.swift     the Desktop chip and the restart sheet
// ScreenTime.swift  screen time from ActivityWatch, uploaded for the Time sheet (off unless activityWatch is set)
// Sent.swift        the messages sent from the board, and ticking them off
// Switch.swift      the sheet that changes an agent's CLI, login, model or effort
// Theme.swift       state colours, zoom, usage levels and formatting
// Time.swift        the Time sheet: where your time went
// Views.swift       the board's views: top bar, tiles, groups, worktrees, detail panel, overview, footer
// widgets.js        a resource, not Swift: the renderer that draws the options and plots in a thread's document

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
if let i = args.firstIndex(of: "--render-time"), args.count > i + 2 {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let value = { (flag: String) in args.firstIndex(of: flag).flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil } }
    var range: Int?
    if args.contains("--range") {
        guard let r = value("--range").flatMap({ Int($0) }), TimeSheet.ranges.contains(r) else {
            FileHandle.standardError.write(Data("--range takes one of \(TimeSheet.ranges.map(String.init).joined(separator: ", "))\n".utf8))
            exit(2)
        }
        range = r
    }
    var hours = TimeHours.all
    if args.contains("--hours") {
        guard let h = value("--hours").flatMap(TimeHours.init) else {
            FileHandle.standardError.write(Data("--hours takes one of \(TimeHours.allCases.map(\.rawValue).joined(separator: ", "))\n".utf8))
            exit(2)
        }
        hours = h
    }
    let focus = TimeFocus(group: value("--group"), sub: value("--sub").map { Optional($0) }, day: value("--day"),
                          item: value("--item"), hours: hours, range: range)
    let code = MainActor.assumeIsolated {
        Renderer.renderTime(args[i + 1], to: args[i + 2], focus: focus, dark: !args.contains("--light"))
    }
    exit(code)
}
if let i = args.firstIndex(of: "--render-usage"), args.count > i + 2 {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { Renderer.renderUsage(args[i + 1], to: args[i + 2], dark: !args.contains("--light")) }
    exit(code)
}
if let i = args.firstIndex(of: "--render-mail"), args.count > i + 3 {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let files = args[(i + 1)...].filter { $0 != "--light" }
    let code = MainActor.assumeIsolated {
        Renderer.renderMail(list: files.first!, shows: Array(files.dropFirst().dropLast()), to: files.last!,
                            dark: !args.contains("--light"))
    }
    exit(code)
}
if let i = args.firstIndex(of: "--render-meeting"), args.count > i + 1 {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated {
        Renderer.renderMeeting(to: args[i + 1], dark: !args.contains("--light"), untitled: args.contains("--untitled"))
    }
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
if let i = args.firstIndex(of: "--selftest-alerts"), args.count > i + 1 {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { AlertsTest.run(snapshot: args[i + 1]) }
    exit(code)
}
if let i = args.firstIndex(of: "--selftest-cpu"), args.count > i + 1 {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let secs = args.count > i + 2 ? Double(args[i + 2]) ?? 10 : 10
    let code = MainActor.assumeIsolated { CPUTest.run(snapshot: args[i + 1], seconds: secs) }
    exit(code)
}
if args.contains("--selftest-screen") {
    exit(ScreenTest.run())
}
if args.contains("--selftest-time") {
    let code = MainActor.assumeIsolated { TimeTest.run() }
    exit(code)
}
if args.contains("--selftest-decode") {
    exit(DecodeTest.run())
}
if args.contains("--selftest-chats") {
    exit(ChatTest.run())
}
if args.contains("--selftest-mail") {
    let code = MainActor.assumeIsolated { MailTest.run() }
    exit(code)
}
if args.contains("--selftest-widgets") {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { WidgetTest.run() }
    exit(code)
}
if args.contains("--selftest-meetings") {
    let code = MainActor.assumeIsolated { MeetingsTest.run() }
    exit(code)
}
if args.contains("--selftest-usage") {
    exit(UsageSplitTest.run())
}
if let i = args.firstIndex(of: "--screen-preview"), args.count > i + 1, let hours = Double(args[i + 1]) {
    exit(ScreenTime.preview(hours: hours))
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
