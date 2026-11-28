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
// AgentBoard --render-chats CHATS.json OUT.png [--light]
//                                          draw the Chats sheet from saved chats, one chat and its posts (as the
//                                          chat_list, chat_show and chat_posts actions give them; ChatsPreload)
// AgentBoard --render-meeting OUT.png [--untitled] [--light]
//                                          draw the meeting overlay for one screen into a PNG, for a meeting a minute
//                                          away: one with a title and a video link, or with --untitled a free/busy one
// AgentBoard --render-mail LIST.json SHOW.json [SHOW.json ...] OUT.png [--light] [--snapshot SNAPSHOT.json]
//            [--drafts DRAFTS.json] [--archive] [--popout]
//                                          draw the Mail tab from a saved mail_list answer after opening the threads of
//                                          saved mail_show answers in turn and the first again (its document captured
//                                          from WebKit), printing how long each took to reach WebKit's page and how
//                                          many of its widgets (options and plots) were drawn and comments highlighted;
//                                          with a saved snapshot, the board has its agents, so the threads of those at
//                                          work have the brown dot (MailRules.working); with saved drafts (a
//                                          drafts.json), their replies; with
//                                          --archive, that thread then archived, so the Undo bar shows, and no thread
//                                          is open; with --popout, the last thread given then popped out into a window
//                                          of its own, drawn beside the tab
// AgentBoard --render-restart PLAN.json OUT.png
//                                          draw the restart sheet from a saved plan (`agent-restart plan --json`)
// AgentBoard --render-keyring OUT.png [--light]
//                                          draw the top bar with Chrome's keyring on the desktop locked, and the
//                                          unlock sheet with a password typed, after a wrong one, after login's alone
//                                          (partial) and after the right one
// AgentBoard --render-ask OUT.png [--light]
//                                          draw the ask box (⌃⌥A) on the desk with a sample exchange, one answer in with
//                                          what the desk sent under it and the next being answered, what the desk is
//                                          doing said, beside a box with no agent found (still on the desk), and one
//                                          asking an agent's copy whose answer this Mac's voice read, the desktop's
//                                          voice not answering
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
// AgentBoard --selftest-decode             the snapshot's problem list (the desktop's, then what does not decode),
//                                          a per-token login's spend meter and the start dialog's logins, a switch
//                                          target's own model, and the reading of an action's answer, offline
// AgentBoard --selftest-chats              group-chat post ids, which chats count as new, who sent each post (you or
//                                          an agent), topics and where a post goes, and the member line, offline
// AgentBoard --selftest-mail               the Mail tab's rules (snooze times and the Snooze menu, the list's changes
//                                          and times, the keys, links and the document view's navigations, attachment
//                                          files, reply keys, the reply's text from answers, comments and words, on the
//                                          cases the phone's copy is checked on too, what Undo sends) and
//                                          what it asks the desktop for when a thread is opened, archived (closing it,
//                                          ↓ and ↑ then opening the threads beside it), stepped through, answered, put
//                                          back by Undo or popped out into a window of its own (and read there with the
//                                          tab), against saved answers; the read mark after a thread's second on
//                                          screen, and none when it is left sooner; the Undo bar going after its time;
//                                          a tile's double-click opening its agent's latest mail, and that mail's Gmail
//                                          link (none for mail kept for the tab alone); the busy dots following the
//                                          tiles, and a row's dots drawn offscreen; the group colours the desktop
//                                          sends, and a grouped row drawn light and dark, a plain row but for its
//                                          strip, which opening a thread leaves at the tab's left edge (what is over
//                                          the list fits its column at every zoom); fast mode's switch asking for
//                                          mail_fast, the tab and a thread's window showing it alike, and its minutes
//                                          left on the switch and the row's badge; the Researcher switch asking for
//                                          mail_researcher with the routing tag shown, and what it says of delivery;
//                                          offline
// AgentBoard --selftest-widgets            the options and plots agents put in mail, drawn by widgets.js in the
//                                          document view offscreen: the cards, the plot, an option picked (with a
//                                          comment too) as its question's answer in the draft, two questions
//                                          answered, a widget that does not check out
// AgentBoard --selftest-comments           comments on text selected in agents' mail, in the document view
//                                          offscreen: the Comment button and menu item, the quote in the draft, the
//                                          highlights following the draft and drawn again when the thread is
// AgentBoard --selftest-popout             threads popped out of the Mail tab into windows of their own, offscreen,
//                                          against a stand-in desktop: a double-click on a row opens one window per
//                                          thread and brings it forward again; the window and the tab share the
//                                          thread's draft (its text, answers and comments, in both reply boxes and both
//                                          documents); the window's keys (r, Esc, Space, e, z, and ↓ left alone; u,
//                                          which closes it, and Undo opens it again; s, the Snooze menu at its Snooze
//                                          button); fast mode switched from the window, on and off in the list, the
//                                          tab and the window alike; the Researcher switch pressed in the window;
//                                          closing frees the window and its web view; it can go full screen
// AgentBoard --selftest-copy               the code blocks' Copy buttons in the document view offscreen: one per
//                                          block, clear of its text; a click puts the exact text on a pasteboard
// AgentBoard --selftest-forward            agent mail's Forward buttons in the document view offscreen: one on each
//                                          agent's mail, none on yours; a click asks for mail_forward against a saved
//                                          answer and opens the draft's Gmail page (a stand-in opener), or says why not
// AgentBoard --selftest-meetings           when each meeting alert comes, once, with catch-up and none for meetings
//                                          long started; a meeting's title and main button; the meetings section as
//                                          the desktop sends it, or none, offline (posts and shows nothing)
// AgentBoard --selftest-cpu SNAPSHOT.json [SECONDS]
//                                          the board offscreen for SECONDS (10): the CPU it used
// AgentBoard --selftest-keys               the window's zoom shortcuts and ⌘↩ in a dialog's text box, offscreen,
//                                          and the files a message dialog takes (KeyTest.dialogFiles)
// AgentBoard --selftest-layout             the group and sub-group operations, reading and saving a layout file
//                                          (a temporary one), and that the saved layout still loads
// AgentBoard --selftest-kitty SNAPSHOT.json
//                                          print what Open in Kitty and the latest-mail link would run for each
//                                          live agent; a dry run that opens nothing and checks nothing
// AgentBoard --selftest-keyring            the lock mark, the keyrings' names, the desktop's unlock answers (partial
//                                          ones too), the unlock sheet's sending (the field emptied, one line on ssh's
//                                          stdin, to a stand-in), the ssh command line, the script a kit wrapper runs,
//                                          and the once-a-boot notification, offline
// AgentBoard --selftest-screen             the screen-time block rules, offline
// AgentBoard --selftest-usage             the usage breakdown's chart bands and colours, offline
// AgentBoard --selftest-phone              the phone sheet's QR code reads back as its address, and the desktop's
//                                          enrolment answer decodes, offline
// AgentBoard --selftest-security           the snapshot's security alerts: one notification for each new one, none
//                                          again after a relaunch, titled by kind, a click opening its thread, offline
//                                          (posts nothing)
// AgentBoard --selftest-time               the Time sheet's one-day lanes, hour span and block runs, its
//                                          work-time choice, and the group colours the desktop sends, offline
// AgentBoard --selftest-title              the board window's title, which names its agent for the voice desk, offline
// AgentBoard --selftest-ask                the ask box: when a dictation asks by itself (and typing never does), the
//                                          box opening on the desk and what it sends it (words and screen, no
//                                          history), the desk's status lines, answer and sends, a new conversation;
//                                          the question, agent and history sent to an agent picked, a question
//                                          waiting its turn, an answer's sentences played in order as they come, a new
//                                          question or a dictation interrupting it, the fallback to this Mac's voice
//                                          said, Stop (the real player too, on silence), the desktop's voice_looking
//                                          answers, the microphone watch (on this Mac's apps), and the voice it picks
//                                          (and the one this Mac has), offline
// AgentBoard --screen-preview HOURS        the screen-time blocks of the last HOURS hours from ActivityWatch,
//                                          printed, not uploaded (needs the activityWatch setting, Remote.swift)
//
// The sources:
// Alerts.swift      what counts as red on the board, and a notification for each new red thing
// Allowance.swift   where a plan window's allowance went: the sheet a usage meter opens, --selftest-usage
// App.swift         the app delegate: menu-bar item, window, global hotkeys, menus and window shortcuts
// AskBox.swift      the ask box (⌃⌥A, off until turned on): the desk asked (told the window in front), or an agent's
//                   copy, by typing or by any dictation app, the answer heard a sentence at a time in the desktop's
//                   voice (or this Mac's, said so), what the desk sent shown under it; --selftest-ask
// Board.swift       BoardModel, the board's state and actions, and the Mac-only layout of tiles in groups
// Browser.swift     opening a Google link (Gmail, a meeting) in the Chrome profile signed in to its account
// Chats.swift       group chats with agents: the top bar's Chats button, the chat sheet with its topics, New chat,
//                   --selftest-chats
// Compose.swift     a mail reply's box, with files dropped or pasted in, answers and comments, and the draft that
//                   keeps its key; ReplyText, the text a reply sends
// CPUTest.swift     --selftest-cpu
// Dialogs.swift     the confirmations and text dialogs every action on the desktop goes through
// Keyring.swift     the desktop's keyrings: the top bar's lock mark for Chrome's, the unlock sheet, --selftest-keyring
// Kitty.swift       Open in Kitty: a terminal attached to an agent's session on the desktop
// Mail.swift        the Mail tab: the mailroom's threads, a thread with its reply box, snoozing, Undo, --selftest-mail
// MailComments.swift comments on text selected in an agent's mail, highlighted in it; --selftest-comments
// MailCopy.swift    a Copy button on each code block of a thread document, onto the pasteboard; --selftest-copy
// MailDrafts.swift  your unsent replies, kept on the desktop and shared with the phone, with a write-ahead copy here
// MailEnlarge.swift a plot, table or image of a thread document enlarged in a window of its own; --selftest-widgets
// MailForward.swift a Forward button on each agent's mail, making a draft of it in your Gmail; --selftest-forward
// MailHTML.swift    a thread's HTML document in a locked-down WKWebView; its links and attachments
// MailWindows.swift threads popped out of the Mail tab into windows of their own, one per thread; --selftest-popout
// MailWidgets.swift the options and plots in agents' mail, drawn in the document by widgets.js; --selftest-widgets
// main.swift        these flags and the dispatch to each mode
// Meetings.swift    meeting alerts: a notification, then a full-screen overlay, before each meeting; --selftest-meetings
// Microphone.swift  MicrophoneWatch: when an app starts to record (a dictation starting), which interrupts the ask box
// Model.swift       the collector's snapshot as Swift types, decoded leniently (--selftest-decode)
// Phone.swift       the phone sheet: the QR code that enrols your phone for Agent Mail, signing it out; --selftest-phone
// Pulse.swift       the pulsing dot on a busy tile and on the meeting overlay
// Remote.swift      settings, the SSH stream from the collector, and the actions sent to the desktop
// Render.swift      the offscreen renders, --probe, --selftest-keys and --selftest-layout
// Restart.swift     the Desktop chip and the restart sheet
// SecurityAlerts.swift the mailroom's security alerts kept off Gmail, a notification each; --selftest-security
// ScreenTime.swift  screen time from ActivityWatch, uploaded for the Time sheet (off unless activityWatch is set)
// Sent.swift        the messages sent from the board, and ticking them off
// Switch.swift      the sheet that changes an agent's CLI, login, model or effort
// Theme.swift       state colours, zoom, usage levels and formatting
// Time.swift        the Time sheet: where your time went
// Views.swift       the board's views: top bar, tiles, groups, worktrees, detail panel, overview, footer
// widgets.js        a resource, not Swift: the renderer that draws the options and plots in a thread's document
// reply-text-cases.json a resource, not Swift: the cases ReplyText and MailWidgets.answer, and the phone's copy of
//                   them, are checked on (--selftest-mail)

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
if let i = args.firstIndex(of: "--render-chats"), args.count > i + 2 {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { Renderer.renderChats(args[i + 1], to: args[i + 2], dark: !args.contains("--light")) }
    exit(code)
}
if let i = args.firstIndex(of: "--render-mail"), args.count > i + 3 {
    NSApplication.shared.setActivationPolicy(.prohibited)
    var files = args[(i + 1)...].filter { $0 != "--light" && $0 != "--archive" && $0 != "--popout" }
    /// The file after `flag`, taken out of `files` with it; nil without the flag. Exits when no file follows it.
    func option(_ flag: String, _ what: String) -> String? {
        guard let s = files.firstIndex(of: flag) else { return nil }
        guard files.indices.contains(s + 1) else {
            FileHandle.standardError.write(Data("\(flag) takes \(what)\n".utf8))
            exit(2)
        }
        defer { files.removeSubrange(s...(s + 1)) }
        return files[s + 1]
    }
    let snapshot = option("--snapshot", "a saved snapshot's file")
    let drafts = option("--drafts", "a saved drafts.json")
    guard files.count >= 3 else {
        FileHandle.standardError.write(Data("--render-mail takes LIST.json, at least one SHOW.json and OUT.png\n".utf8))
        exit(2)
    }
    let code = MainActor.assumeIsolated {
        Renderer.renderMail(list: files.first!, shows: Array(files.dropFirst().dropLast()), to: files.last!,
                            dark: !args.contains("--light"), snapshot: snapshot, drafts: drafts,
                            archive: args.contains("--archive"), popout: args.contains("--popout"))
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
if let i = args.firstIndex(of: "--render-keyring"), args.count > i + 1 {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { Renderer.renderKeyring(to: args[i + 1], dark: !args.contains("--light")) }
    exit(code)
}
if let i = args.firstIndex(of: "--render-ask"), args.count > i + 1 {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { Renderer.renderAsk(to: args[i + 1], dark: !args.contains("--light")) }
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
if args.contains("--selftest-title") {
    let code = MainActor.assumeIsolated { BoardTitle.selftest() }
    exit(code)
}
if args.contains("--selftest-ask") {
    let code = MainActor.assumeIsolated { AskTest.run() }
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
if args.contains("--selftest-popout") {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { PopoutTest.run() }
    exit(code)
}
if args.contains("--selftest-copy") {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { CopyTest.run() }
    exit(code)
}
if args.contains("--selftest-forward") {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { ForwardTest.run() }
    exit(code)
}
if args.contains("--selftest-widgets") {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { WidgetTest.run() }
    exit(code)
}
if args.contains("--selftest-comments") {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { CommentTest.run() }
    exit(code)
}
if args.contains("--selftest-meetings") {
    let code = MainActor.assumeIsolated { MeetingsTest.run() }
    exit(code)
}
if args.contains("--selftest-usage") {
    exit(UsageSplitTest.run())
}
if args.contains("--selftest-keyring") {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { KeyringTest.run() }
    exit(code)
}
if args.contains("--selftest-phone") {
    exit(PhoneTest.run())
}
if args.contains("--selftest-security") {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let code = MainActor.assumeIsolated { SecurityAlertsTest.run() }
    exit(code)
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
