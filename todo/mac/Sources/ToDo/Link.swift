import Foundation

/// Settings live in UserDefaults (`defaults write org.agentkit.todo host desktop`).
enum Settings {
    static let defaults = UserDefaults.standard
    static var host: String {
        get { defaults.string(forKey: "host") ?? "desktop" }
        set { defaults.set(newValue, forKey: "host") }
    }
    /// todo-desk on the desktop: the wrapper the kit installer puts in ~/.local/bin.
    static var helper: String {
        get { defaults.string(forKey: "helper") ?? "~/.local/bin/todo-desk" }
        set { defaults.set(newValue, forKey: "helper") }
    }

    /// The remote command: a leading ~/ stays unquoted so the desktop's shell expands it.
    static var remoteCommand: String {
        let h = helper
        let path = h.hasPrefix("~/") ? "~/" + shellQuote(String(h.dropFirst(2))) : shellQuote(h)
        return "exec \(path) serve"
    }
}

enum LinkState: Equatable {
    case connecting
    case live
    case down(String)

    var isLive: Bool { self == .live }
}

struct LinkError: Error {
    var message: String
    var offline: Bool                 // the link dropped: the request may be retried as it is
    var reply: [String: Any]? = nil
}

private let sshPath = "/usr/bin/ssh"
private let sshOptions = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15",
                          "-o", "ServerAliveCountMax=2", "-o", "ControlMaster=no", "-T"]

/// The one long-lived `ssh HOST todo-desk serve`. JSON lines both ways: requests carry an id the reply repeats;
/// everything else is an event. Reconnects with backoff, and restarts if the helper goes quiet (it pings every 20 s).
final class DeskLink {
    var onEvent: (([String: Any]) -> Void)?
    var onState: ((LinkState) -> Void)?
    var onConnect: (() -> Void)?

    private var process: Process?
    private var stdin: FileHandle?
    private var buffer = Data()
    private var errTail = ""
    private var backoff: TimeInterval = 1
    private var generation = 0
    private var lastData = Date()
    private var watchdog: Timer?
    private var nextID = 1
    private var waiting: [Int: (Result<[String: Any], LinkError>) -> Void] = [:]
    private var timeouts: [Int: DispatchWorkItem] = [:]
    private let queue = DispatchQueue(label: "todo.link")
    private(set) var state: LinkState = .down("not started")

    func start() {
        stop()
        generation += 1
        let gen = generation
        setState(.connecting)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: sshPath)
        p.arguments = sshOptions + [Settings.host, Settings.remoteCommand]
        let out = Pipe(), err = Pipe(), inp = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = inp
        queue.sync { buffer = Data(); errTail = "" }      // buffer and errTail belong to `queue`
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            if data.isEmpty { h.readabilityHandler = nil; return }
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
            setState(.down("could not start ssh: \(error.localizedDescription)"))
            scheduleRetry(gen: gen)
            return
        }
        process = p
        stdin = inp.fileHandleForWriting
        lastData = Date()
        watchdog = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            guard let self = self, self.generation == gen else { return }
            if Date().timeIntervalSince(self.lastData) > 75 {
                self.setState(.down("no answer from the desktop for 75 s; reconnecting"))
                self.start()
            }
        }
    }

    func stop() {
        watchdog?.invalidate()
        watchdog = nil
        generation += 1
        if let p = process, p.isRunning { p.terminate() }
        process = nil
        stdin = nil
        failAll("the connection to the desktop closed")
    }

    /// Drop the connection and stay down until start() (the self-test's way of going offline).
    func disconnect() {
        stop()
        setState(.down("disconnected"))
    }

    /// Send one request; the completion runs on the main queue with the helper's reply (ok true) or an error.
    func request(_ obj: [String: Any], timeout: TimeInterval = 90,
                 completion: @escaping (Result<[String: Any], LinkError>) -> Void) {
        guard state.isLive, let h = stdin else {
            DispatchQueue.main.async { completion(.failure(LinkError(message: "the desktop is not connected", offline: true))) }
            return
        }
        let id = nextID
        nextID += 1
        var msg = obj
        msg["id"] = id
        guard var data = try? JSONSerialization.data(withJSONObject: msg) else {
            DispatchQueue.main.async { completion(.failure(LinkError(message: "could not encode the request", offline: false))) }
            return
        }
        data.append(0x0A)
        waiting[id] = completion
        let t = DispatchWorkItem { [weak self] in
            self?.finish(id, .failure(LinkError(message: "the desktop took too long to answer", offline: true)))
        }
        timeouts[id] = t
        DispatchQueue.main.asyncAfter(deadline: .now() + timeout, execute: t)
        queue.async { try? h.write(contentsOf: data) }
    }

    private func finish(_ id: Int, _ result: Result<[String: Any], LinkError>) {
        timeouts.removeValue(forKey: id)?.cancel()
        waiting.removeValue(forKey: id)?(result)
    }

    /// Fail every waiting request, after the caller (start, stop, ended) has finished changing state.
    private func failAll(_ why: String) {
        let ids = Array(waiting.keys)
        for id in ids { timeouts.removeValue(forKey: id)?.cancel() }
        let pending = ids.compactMap { waiting.removeValue(forKey: $0) }
        guard !pending.isEmpty else { return }
        DispatchQueue.main.async { for c in pending { c(.failure(LinkError(message: why, offline: true))) } }
    }

    private func setState(_ s: LinkState) {
        state = s
        onState?(s)
    }

    private func consume(_ data: Data, gen: Int) {
        buffer.append(data)
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer.subdata(in: buffer.startIndex..<nl)
            buffer.removeSubrange(buffer.startIndex...nl)
            guard !line.isEmpty, let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
            DispatchQueue.main.async { [weak self] in
                guard let self = self, self.generation == gen else { return }
                self.lastData = Date()
                if self.state != .live, obj["type"] as? String != "fatal" {
                    self.backoff = 1
                    self.setState(.live)
                    self.onConnect?()
                }
                if let id = obj["id"] as? Int {
                    if obj["ok"] as? Bool == true {
                        self.finish(id, .success(obj))
                    } else {
                        self.finish(id, .failure(LinkError(message: obj["error"] as? String ?? "the desktop refused it",
                                                           offline: false, reply: obj)))
                    }
                } else if obj["type"] as? String == "fatal" {
                    self.setState(.down(obj["error"] as? String ?? "todo-desk stopped"))
                } else {
                    self.onEvent?(obj)
                }
            }
        }
    }

    private func ended(gen: Int, status: Int32) {
        guard gen == generation else { return }
        watchdog?.invalidate()
        failAll("the connection to the desktop closed")
        let tail = queue.sync { errTail }
        let why = tail.trimmingCharacters(in: .whitespacesAndNewlines).components(separatedBy: "\n").last ?? ""
        if case .down(let fatal) = state, !fatal.isEmpty, status != 255 {
            setState(.down(fatal))                       // keep todo-desk's own explanation
        } else {
            setState(.down(status == 127 ? "todo-desk not found on the desktop at \(Settings.helper)"
                           : why.isEmpty ? "connection closed (exit \(status))" : why))
        }
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
