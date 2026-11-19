import Foundation

/// Settings live in UserDefaults (`defaults write org.agentkit.agentboard host desktop`).
enum Settings {
    static let defaults = UserDefaults.standard
    static var host: String {
        get { defaults.string(forKey: "host") ?? "desktop" }
        set { defaults.set(newValue, forKey: "host") }
    }
    /// AGENTBOARD_COLLECTOR overrides it for one run (--probe against a branch's collector).
    static var collector: String {
        get { ProcessInfo.processInfo.environment["AGENTBOARD_COLLECTOR"] ?? defaults.string(forKey: "collector")
                ?? "~/.local/bin/agent-board" }
        set { defaults.set(newValue, forKey: "collector") }
    }
    /// ActivityWatch's server on this Mac, for screen time on the Time page (ScreenTime.swift). Unset (the default),
    /// screen time is off: the app asks ActivityWatch nothing and uploads nothing. Turn it on with
    /// `defaults write org.agentkit.agentboard activityWatch http://localhost:5600`.
    static var activityWatch: String? {
        get { defaults.string(forKey: "activityWatch").flatMap { $0.isEmpty ? nil : $0 } }
        set { defaults.set(newValue, forKey: "activityWatch") }
    }
}

enum LinkState: Equatable {
    case connecting
    case live
    case down(String)
}

private let sshPath = "/usr/bin/ssh"
private let sshOptions = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15",
                          "-o", "ServerAliveCountMax=2", "-o", "ControlMaster=no", "-T"]

/// One line from the collector, decoded on the stream's own queue: snapshots are large, and decoding them on the
/// main thread would hold up typing and scrolling.
enum StreamMessage {
    case snapshot(Snapshot)
    case metrics(MetricsMessage)
    case error(String?)
    case notUnderstood(String)
    case other                      // hello and anything newer: still a sign of life

    var kind: String {
        switch self {
        case .snapshot: return "snapshot"
        case .metrics: return "metrics"
        case .error: return "error"
        case .notUnderstood: return "notUnderstood"
        case .other: return "other"
        }
    }
}

/// The one long-lived `ssh HOST agent-board stream`. JSON lines in; small command lines out.
/// Reconnects with backoff, and restarts if the stream goes quiet (the collector speaks at least every 30 s).
/// Actions go over it too when the collector says it takes them (hello `acts`), so a click needs no new SSH.
final class StreamLink {
    var onMessage: ((StreamMessage) -> Void)?
    var onState: ((LinkState) -> Void)?

    private var process: Process?
    private var stdin: FileHandle?
    private var errTail = ""
    private var backoff: TimeInterval = 1
    private var generation = 0
    private var lastData = Date()
    private var watchdog: Timer?
    private let queue = DispatchQueue(label: "agentboard.stream")

    // Only touched on `queue`:
    private var buffer = Data()
    private var qGeneration = 0
    private var qStdin: FileHandle?
    private var acts = false                                   // this connection's collector answers "act"
    private var nextRid = 0
    private var pending: [Int: (Data, String) -> Void] = [:]   // rid -> completion(stdout, stderr)
    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    func start() {
        stop()
        generation += 1
        let gen = generation
        onState?(.connecting)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: sshPath)
        p.arguments = sshOptions + [Settings.host, "exec \(Settings.collector) stream"]
        let out = Pipe(), err = Pipe(), inp = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = inp
        errTail = ""
        queue.async { [weak self] in
            self?.qGeneration = gen
            self?.buffer = Data()
        }
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            if data.isEmpty { h.readabilityHandler = nil; return }      // EOF: stop polling
            self?.queue.async { self?.consume(data, gen: gen) }
        }
        err.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            if data.isEmpty { h.readabilityHandler = nil; return }
            guard let s = String(data: data, encoding: .utf8), !s.isEmpty else { return }
            self?.queue.async { self?.errTail = String(((self?.errTail ?? "") + s).suffix(400)) }
        }
        p.terminationHandler = { [weak self] proc in
            out.fileHandleForReading.readabilityHandler = nil
            err.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async { self?.ended(gen: gen, status: proc.terminationStatus) }
        }
        do {
            try p.run()
        } catch {
            onState?(.down("could not start ssh: \(error.localizedDescription)"))
            scheduleRetry(gen: gen)
            return
        }
        process = p
        let input = inp.fileHandleForWriting
        stdin = input
        queue.async { [weak self] in self?.qStdin = input }
        lastData = Date()
        let dog = Timer(timeInterval: 10, repeats: true) { [weak self] _ in
            guard let self = self, self.generation == gen else { return }
            if Date().timeIntervalSince(self.lastData) > 75 {
                self.onState?(.down("no data for 75 s; reconnecting"))
                self.start()
            }
        }
        dog.tolerance = 2
        RunLoop.main.add(dog, forMode: .default)
        watchdog = dog
    }

    func stop() {
        watchdog?.invalidate()
        watchdog = nil
        generation += 1
        if let p = process, p.isRunning { p.terminate() }
        process = nil
        stdin = nil
        queue.async { [weak self] in self?.dropConnection() }
    }

    func send(_ obj: [String: Any]) {
        guard let h = stdin, var data = try? JSONSerialization.data(withJSONObject: obj) else { return }
        data.append(0x0A)
        queue.async { try? h.write(contentsOf: data) }
    }

    /// One action over the open stream, waiting for its answer: (stdout, stderr) as `agent-board act` wrote them.
    /// nil when the stream can't take it (not connected, or an older collector), so the caller uses its own SSH.
    /// Never call this on the main thread or the stream's queue.
    func requestSync(_ request: [String: Any], timeout: TimeInterval = 960) -> (Data, String)? {
        let done = DispatchSemaphore(value: 0)
        var answer: (Data, String)?
        var rid: Int?
        queue.sync {
            guard acts, let h = qStdin else { return }
            nextRid += 1
            let n = nextRid
            guard var line = try? JSONSerialization.data(withJSONObject: ["cmd": "act", "rid": n, "req": request]) else { return }
            line.append(0x0A)
            pending[n] = { out, err in answer = (out, err); done.signal() }
            do {
                try h.write(contentsOf: line)
                rid = n
            } catch {
                pending[n] = nil
            }
        }
        guard let sent = rid else { return nil }
        if done.wait(timeout: .now() + timeout) == .timedOut {
            queue.sync { _ = pending.removeValue(forKey: sent) }
            return (Data(), "no answer from the desktop in \(Int(timeout)) s; check the board before trying again")
        }
        return answer
    }

    /// On `queue`: the connection is gone, so no answer will come for what was asked.
    private func dropConnection() {
        acts = false
        qStdin = nil
        qGeneration = -1
        let waiting = pending
        pending = [:]
        for done in waiting.values {
            done(Data(), "the connection to the desktop dropped before it answered; check the board before trying again")
        }
    }

    private func consume(_ data: Data, gen: Int) {
        guard !data.isEmpty, gen == qGeneration else { return }
        buffer.append(data)
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer.subdata(in: buffer.startIndex..<nl)
            buffer.removeSubrange(buffer.startIndex...nl)
            guard !line.isEmpty else { continue }
            guard let msg = decode(line) else { continue }
            DispatchQueue.main.async { [weak self] in
                guard let self = self, self.generation == gen else { return }
                self.lastData = Date()
                self.backoff = 1
                self.onMessage?(msg)
            }
        }
    }

    /// The collector writes `{"type":"…",` first, so the type is read from the start of the line, not by parsing
    /// it all. nil for an action's answer, which goes to its waiting request instead.
    private func decode(_ line: Data) -> StreamMessage? {
        switch lineType(line) {
        case "snapshot":
            do { return .snapshot(try decoder.decode(Snapshot.self, from: line)) } catch {
                return .notUnderstood("snapshot not understood: \(error)")
            }
        case "metrics":
            return (try? decoder.decode(MetricsMessage.self, from: line)).map { .metrics($0) } ?? .other
        case "error":
            return .error((try? decoder.decode(ErrorMessage.self, from: line))?.error)
        case "act":
            if let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any], let rid = obj["rid"] as? Int,
               let done = pending.removeValue(forKey: rid) {
                done(Data(((obj["out"] as? String) ?? "").utf8), (obj["err"] as? String) ?? "")
            }
            return nil
        case "hello":
            if let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] { acts = obj["acts"] != nil }
            return .other
        default:
            return .other
        }
    }

    private func lineType(_ line: Data) -> String {
        let head = Array(line.prefix(40))
        let open = Array(#"{"type":""#.utf8)
        if head.starts(with: open), let end = head[open.count...].firstIndex(of: 0x22) {
            return String(decoding: head[open.count..<end], as: UTF8.self)
        }
        return (try? decoder.decode(Envelope.self, from: line))?.type ?? ""
    }

    private func ended(gen: Int, status: Int32) {
        guard gen == generation else { return }
        watchdog?.invalidate()
        queue.async { [weak self] in self?.dropConnection() }
        let why = errTail.trimmingCharacters(in: .whitespacesAndNewlines)
            .components(separatedBy: "\n").last ?? ""
        onState?(.down(why.isEmpty ? "connection closed (exit \(status))" : why))
        scheduleRetry(gen: gen)
    }

    private func scheduleRetry(gen: Int) {
        let delay = backoff
        backoff = min(backoff * 2, 30)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self = self, self.generation == gen else { return }
            self.start()
        }
    }
}

struct ActionResult {
    var ok: Bool
    var message: String
}

/// One validated action: the request as JSON to `agent-board act` on the desktop, over the open stream when it
/// takes actions and the request fits its line, else `ssh HOST agent-board act` with the JSON on stdin. Never a shell
/// string.
enum Actions {
    /// The board's stream, set when it connects. A click then costs no SSH handshake, login or Python start-up.
    static weak var stream: StreamLink?

    static func run(_ request: [String: Any], completion: @escaping (ActionResult) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let result = runSync(request)
            DispatchQueue.main.async { completion(result) }
        }
    }

    /// The desktop's whole JSON answer (for actions that return data, e.g. agent-switch options), or an error.
    static func runJSON(_ request: [String: Any], completion: @escaping ([String: Any]?, String?) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let (obj, err) = runRaw(request)
            DispatchQueue.main.async { completion(obj, err) }
        }
    }

    /// An action that answers with data: the answer's value under `key` (the whole answer when nil) decoded as T,
    /// or why not. Exactly one of the two is nil. On the main queue.
    static func fetch<T: Decodable>(_ request: [String: Any], key: String?, as type: T.Type,
                                    completion: @escaping (T?, String?) -> Void) {
        runJSON(request) { obj, err in
            let (value, why) = decodeAnswer(obj, err, key: key, as: type)
            completion(value, why)
        }
    }

    /// What fetch makes of runJSON's (answer, error): the desktop's own error unless the answer says ok, else what is
    /// missing or not understood.
    static func decodeAnswer<T: Decodable>(_ obj: [String: Any]?, _ err: String?, key: String?, as type: T.Type) -> (T?, String?) {
        guard let obj = obj else { return (nil, err ?? "no answer from the desktop") }
        guard (obj["ok"] as? Bool) == true else { return (nil, (obj["error"] as? String) ?? "the desktop refused, with no reason") }
        var raw: Any? = obj
        if let key = key { raw = obj[key] }
        guard let value = raw, JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(withJSONObject: value) else {
            return (nil, "the desktop's answer has no \(key ?? "data")")
        }
        do {
            return (try JSON.decoder.decode(T.self, from: data), nil)
        } catch {
            return (nil, "not understood: " + describeDecodeError(error))
        }
    }

    /// Over the stream only, for --probe: nil when the stream can't take actions.
    static func viaStream(_ request: [String: Any], completion: @escaping ((Data, String)?) -> Void) {
        let link = stream
        DispatchQueue.global(qos: .userInitiated).async {
            let r = link?.requestSync(request, timeout: 30)
            DispatchQueue.main.async { completion(r) }
        }
    }

    /// The longest line the stream's reader on the desktop takes (bin/agent-board, Inbox.MAX_LINE); it drops a longer
    /// one whole, so a request near it (a mail reply with files) goes over SSH of its own.
    static let streamLine = 1 << 20

    /// (stdout, stderr) of `agent-board act` for one request; stdout is its JSON answer.
    static func exchange(_ request: [String: Any]) -> (Data, String) {
        guard let body = try? JSONSerialization.data(withJSONObject: request) else { return (Data(), "could not encode request") }
        if body.count + 100 < streamLine, let answer = stream?.requestSync(request) { return answer }   // 100: the envelope
        return overSSH("act") { try $0.write(contentsOf: body) }
    }

    /// Copy the file at `url` to the desktop for a mail reply it is too big for (`agent-board receive`): its path
    /// there, or why not. The file is read as it goes, a megabyte at a time. Answers on the main queue.
    static func upload(_ url: URL, completion: @escaping (String?, String?) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let (data, e) = overSSH("receive") { h in
                guard let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize else {
                    throw CocoaError(.fileReadUnknown)
                }
                try h.write(contentsOf: JSONSerialization.data(withJSONObject: ["name": url.lastPathComponent, "size": size]))
                try h.write(contentsOf: Data([0x0A]))
                let f = try FileHandle(forReadingFrom: url)
                defer { try? f.close() }
                var left = size                  // no more than the size sent, should the file grow meanwhile
                while left > 0, let chunk = try f.read(upToCount: min(left, 1 << 20)), !chunk.isEmpty {
                    try h.write(contentsOf: chunk)
                    left -= chunk.count
                }
            }
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let path = (obj?["ok"] as? Bool) == true ? obj?["path"] as? String : nil
            let why = (obj?["error"] as? String) ?? (e.isEmpty ? "no answer from the desktop" : e)
            DispatchQueue.main.async { completion(path, path == nil ? why : nil) }
        }
    }

    /// `ssh HOST <collector> COMMAND`, with `input` writing its stdin: (stdout, stderr, and what stopped the input).
    /// A desktop that stops reading fails the write rather than signalling the app (F_SETNOSIGPIPE).
    private static func overSSH(_ command: String, input: (FileHandle) throws -> Void) -> (Data, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: sshPath)
        p.arguments = sshOptions + [Settings.host, "exec \(Settings.collector) \(command)"]
        let out = Pipe(), err = Pipe(), inp = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = inp
        do { try p.run() } catch { return (Data(), "could not start ssh: \(error.localizedDescription)") }
        _ = fcntl(inp.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1)
        var stopped: String?
        do { try input(inp.fileHandleForWriting) } catch { stopped = error.localizedDescription }
        try? inp.fileHandleForWriting.close()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        let e = String(data: errData, encoding: .utf8) ?? ""
        return (data, stopped.map { "not all sent (\($0)) " + e } ?? e)
    }

    /// The desktop's JSON answer, or what went wrong; synchronous (off the main thread).
    static func runRaw(_ request: [String: Any]) -> ([String: Any]?, String?) {
        let (data, e) = exchange(request)
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { return (obj, nil) }
        return (nil, e.isEmpty ? "no answer from the desktop" : e)
    }

    private static func runSync(_ request: [String: Any]) -> ActionResult {
        let (data, e) = exchange(request)
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            let ok = obj["ok"] as? Bool ?? false
            let err = (obj["error"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            let msg = err ?? (obj["output"] as? String) ?? ""
            return ActionResult(ok: ok, message: msg)
        }
        return ActionResult(ok: false, message: e.isEmpty ? "no answer from the desktop" : e)
    }
}
