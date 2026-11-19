import Foundation

// Screen time for the Time page (agentboard/screen.py). Off unless the owner sets Settings.activityWatch: then
// ActivityWatch runs on this Mac and answers on localhost only. The app asks it for the window events in the time
// the owner was not idle, and reduces them to one entry per 5-minute block in which they were active for at least a
// minute: [block start, app, window title], the app and title that held the screen longest. Only those entries leave
// the Mac, with the "screen" action, at launch and every 15 minutes, starting where the desktop's answer says. A
// failed upload shows on the Time sheet. While the setting is unset nothing is asked, nothing is sent and nothing
// shows.

enum ScreenTime {
    static let every: TimeInterval = 15 * 60
    static let minActive = 60.0         // seconds of activity in a block that make it count
    static let partBytes = 48_000       // one upload in UTF-8 bytes, so at most as many characters: well under the
                                        // MAX_REQUEST characters `agent-board act` reads (bin/agent-board)
    static let maxChars = 1000          // a name is cut to this so one entry always fits a part; the desktop keeps less
    static let lookback = 3600.0        // read this much before the start too: an event that began earlier and runs
                                        // into it then counts, whether or not ActivityWatch returns such events
    /// ActivityWatch's query language: window events intersected with the periods the idle ("afk") watcher
    /// marked active.
    static let statements = [
        #"window = flood(query_bucket(find_bucket("aw-watcher-window_")));"#,
        #"afk = flood(query_bucket(find_bucket("aw-watcher-afk_")));"#,
        #"active = filter_keyvals(afk, "status", ["not-afk"]);"#,
        #"RETURN = sort_by_timestamp(filter_period_intersect(window, active));"#,
    ]

    struct Event {
        var start: Double
        var duration: Double
        var app: String
        var title: String
    }

    struct Failure: Error, LocalizedError {
        var errorDescription: String?
        init(_ s: String) { errorDescription = s }
    }

    private static let queue = DispatchQueue(label: "screen-time")      // one upload at a time

    /// Whether the owner has turned screen time on (Settings.activityWatch).
    static var enabled: Bool { Settings.activityWatch != nil }

    /// Upload now and every 15 minutes; `report` gets nil after an upload that went through, else what stopped it.
    /// While screen time is off a tick does nothing but clear any earlier error, so setting activityWatch turns it on
    /// at the next tick and unsetting it turns it off, with no relaunch.
    static func start(report: @escaping (String?) -> Void) {
        let tick = Timer(timeInterval: every, repeats: true) { _ in upload(report) }
        tick.tolerance = 60
        RunLoop.main.add(tick, forMode: .default)
        upload(report)
    }

    static func upload(_ report: @escaping (String?) -> Void) {
        guard enabled else {
            report(nil)
            return
        }
        queue.async {
            let problem: String?
            do {
                try uploadOnce()
                problem = nil
            } catch {
                problem = error.localizedDescription
            }
            DispatchQueue.main.async { report(problem) }
        }
    }

    /// Ask the desktop where to start and how long a block is (an empty upload, which stores nothing).
    static func plan() throws -> (next: Int, block: Int) {
        let answer = try send([])
        guard let next = answer["next"] as? Int, let block = answer["block"] as? Int, block > 0 else {
            throw Failure("the desktop's answer has no start or block length")
        }
        return (next, block)
    }

    /// The blocks from `next` to now, from ActivityWatch.
    static func blocks(next: Int, block: Int, now: Date) throws -> [[Any]] {
        reduce(try activeEvents(from: Double(next) - lookback, to: now.timeIntervalSince1970), block: block, from: next)
    }

    /// Read ActivityWatch from where the desktop says to now, and send the blocks in parts.
    static func uploadOnce() throws {
        let (next, block) = try plan()
        for part in parts(try blocks(next: next, block: block, now: Date())) { _ = try send(part) }
    }

    /// --screen-preview HOURS: print the blocks of the last HOURS hours; nothing is uploaded.
    static func preview(hours: Double) -> Int32 {
        guard enabled else {
            print("Screen time is off. Turn it on with: defaults write org.agentkit.agentboard activityWatch http://localhost:5600")
            return 1
        }
        do {
            let block = try plan().block
            let now = Date()
            let from = Int(now.timeIntervalSince1970 - hours * 3600) / block * block
            for b in try blocks(next: from, block: block, now: now) {
                print(Date(timeIntervalSince1970: Double(b[0] as? Int ?? 0)), b[1], "|", b[2])
            }
            return 0
        } catch {
            print(error.localizedDescription)
            return 1
        }
    }

    /// The desktop's answer to one "screen" action, or what went wrong.
    static func send(_ blocks: [[Any]]) throws -> [String: Any] {
        let (answer, err) = Actions.runRaw(["action": "screen", "blocks": blocks])
        guard let obj = answer else { throw Failure(err ?? "") }         // runRaw names what went wrong
        guard obj["ok"] as? Bool == true else { throw Failure((obj["error"] as? String) ?? "the desktop refused the upload") }
        return obj
    }

    // MARK: ActivityWatch

    private final class Reply: @unchecked Sendable {
        var data: Data?, response: URLResponse?, error: Error?
    }

    /// The window events while the owner was active between two moments (seconds since the epoch), oldest first.
    static func activeEvents(from: Double, to: Double) throws -> [Event] {
        guard let server = Settings.activityWatch else { throw Failure("screen time is off (no activityWatch setting)") }
        let iso = ISO8601DateFormatter()
        let period = iso.string(from: Date(timeIntervalSince1970: from)) + "/" + iso.string(from: Date(timeIntervalSince1970: to))
        guard let query = URL(string: server + "/api/0/query/") else {
            throw Failure("the ActivityWatch address is not a URL: \(server)")
        }
        var req = URLRequest(url: query, timeoutInterval: 60)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["timeperiods": [period], "query": statements])
        let reply = Reply(), done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { d, r, e in
            reply.data = d; reply.response = r; reply.error = e
            done.signal()
        }.resume()
        done.wait()
        if let e = reply.error { throw Failure("ActivityWatch: \(e.localizedDescription)") }
        let status = (reply.response as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200, let data = reply.data else { throw Failure("ActivityWatch answered HTTP \(status)") }
        guard let periods = try? JSONSerialization.jsonObject(with: data) as? [[[String: Any]]], periods.count == 1 else {
            throw Failure("ActivityWatch's answer is not one list of events")
        }
        return try periods[0].map(event)
    }

    static func event(_ o: [String: Any]) throws -> Event {
        guard let ts = o["timestamp"] as? String, let start = seconds(ts), let duration = o["duration"] as? Double,
              let data = o["data"] as? [String: Any], let app = data["app"] as? String else {
            throw Failure("ActivityWatch sent an event this app does not understand")
        }
        return Event(start: start, duration: duration, app: String(app.prefix(maxChars)),     // a window may have no title
                     title: String((data["title"] as? String ?? "").prefix(maxChars)))
    }

    private static let whole = ISO8601DateFormatter()

    /// Seconds since the epoch from an ActivityWatch timestamp ("...T10:00:00.123000+00:00" or "...T10:00:00Z"),
    /// with any number of fractional digits.
    static func seconds(_ s: String) -> Double? {
        guard let frac = s.range(of: #"\.[0-9]+"#, options: .regularExpression) else {
            return whole.date(from: s)?.timeIntervalSince1970
        }
        var rest = s
        rest.removeSubrange(frac)
        guard let f = Double("0" + String(s[frac])), let d = whole.date(from: rest) else { return nil }
        return d.timeIntervalSince1970 + f
    }

    // MARK: blocks

    private struct Window: Hashable, Comparable {
        var app: String, title: String
        static func < (a: Window, b: Window) -> Bool { (a.app, a.title) < (b.app, b.title) }
    }

    /// [block start, app, title] for each block that starts at or after `from` and holds at least minActive
    /// seconds of activity: the app and title on screen longest in it (on a tie, the first by name).
    static func reduce(_ events: [Event], block: Int, from: Int) -> [[Any]] {
        var held: [Int: [Window: Double]] = [:]
        for e in events {
            var t = e.start
            let end = e.start + e.duration
            while t < end {
                let b = Int((t / Double(block)).rounded(.down)) * block
                let upTo = min(end, Double(b + block))
                held[b, default: [:]][Window(app: e.app, title: e.title), default: 0] += upTo - t
                t = upTo
            }
        }
        return held.keys.sorted().compactMap { b -> [Any]? in
            guard b >= from, let by = held[b], by.values.reduce(0, +) >= minActive,
                  let top = by.min(by: { ($1.value, $0.key) < ($0.value, $1.key) }) else { return nil }
            return [b, top.key.app, top.key.title]
        }
    }

    /// The blocks in order, cut into parts of at most partBytes of JSON each.
    static func parts(_ blocks: [[Any]]) -> [[[Any]]] {
        var out: [[[Any]]] = [], part: [[Any]] = [], size = 0
        for b in blocks {
            let n = ((try? JSONSerialization.data(withJSONObject: b))?.count ?? partBytes) + 1
            if !part.isEmpty && size + n > partBytes {
                out.append(part)
                part = []
                size = 0
            }
            part.append(b)
            size += n
        }
        if !part.isEmpty { out.append(part) }
        return out
    }
}

/// --selftest-screen: the block rules and the timestamp parsing, offline (reads and sends nothing).
enum ScreenTest {
    static func run() -> Int32 {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append(what) } }
        let t0 = 1_793_000_100.0                          // a block start (a multiple of 300)
        let ev = { (s: Double, d: Double, app: String, title: String) in ScreenTime.Event(start: s, duration: d, app: app, title: title) }
        func row(_ b: [Any]?) -> String { b.map { "\($0[0]) \($0[1]) \($0[2])" } ?? "none" }
        let blocks = ScreenTime.reduce([
            ev(t0 - 100, 200, "Mail", "Inbox"),           // 100 s before `from`, 100 s in the first block
            ev(t0 + 100, 90, "Safari", "arXiv"),          // the longest in the first block
            ev(t0 + 250, 100, "Zoom", "Standup"),         // 50 s in the first block, 50 in the second
            ev(t0 + 700, 30, "Notes", "x"),               // a third block with 30 s only: does not count
        ], block: 300, from: Int(t0))
        check(blocks.count == 1, "one block counts: \(blocks)")
        check(row(blocks.first) == "\(Int(t0)) Mail Inbox", "the window on screen longest wins (Mail 100 s over Safari 90 s): \(blocks)")
        let tie = ScreenTime.reduce([ev(t0, 40, "B", ""), ev(t0 + 40, 40, "A", "")], block: 300, from: Int(t0))
        check(tie.first?[1] as? String == "A", "a tie goes to the first by name: \(tie)")
        let split = ScreenTime.reduce([ev(t0 + 200, 400, "Safari", "long")], block: 300, from: Int(t0))
        check(split.map { $0[0] as? Int } == [Int(t0), Int(t0) + 300], "an event across blocks counts in each it fills: \(split)")
        check(ScreenTime.seconds("2026-10-30T10:00:00.250000+00:00") == 1_793_354_400.25, "six fractional digits")
        check(ScreenTime.seconds("2026-10-30T10:00:00.5Z") == 1_793_354_400.5, "one fractional digit, Z")
        check(ScreenTime.seconds("2026-10-30T10:00:00+01:00") == 1_793_350_800, "no fraction, an offset")
        check(ScreenTime.seconds("yesterday") == nil, "not a timestamp")
        let many = (0..<3000).map { [Int(t0) + 300 * $0, "Safari", String(repeating: "t", count: 40)] as [Any] }
        let parts = ScreenTime.parts(many)
        check(parts.count > 1 && parts.flatMap { $0 }.map { $0[0] as? Int } == many.map { $0[0] as? Int },
              "parts keep every block, in order")
        check(parts.allSatisfy { ((try? JSONSerialization.data(withJSONObject: $0))?.count ?? .max) <= ScreenTime.partBytes },
              "every part is within partBytes")
        print(fails.isEmpty ? "screen time: all checks pass" : fails.map { "FAIL " + $0 }.joined(separator: "\n"))
        return fails.isEmpty ? 0 : 1
    }
}
