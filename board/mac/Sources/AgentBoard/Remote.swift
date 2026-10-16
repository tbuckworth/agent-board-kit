import Foundation

/// Settings live in UserDefaults (`defaults write org.agentkit.agentboard host desktop`).
enum Settings {
    static let defaults = UserDefaults.standard
    static var host: String {
        get { defaults.string(forKey: "host") ?? "desktop" }
        set { defaults.set(newValue, forKey: "host") }
    }
    static var collector: String {
        get { defaults.string(forKey: "collector") ?? "~/.local/bin/agent-board" }
        set { defaults.set(newValue, forKey: "collector") }
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

/// The one long-lived `ssh HOST agent-board stream`. JSON lines in; small command lines out.
/// Reconnects with backoff, and restarts if the stream goes quiet (the collector speaks at least every 30 s).
final class StreamLink {
    var onLine: ((Data) -> Void)?
    var onState: ((LinkState) -> Void)?

    private var process: Process?
    private var stdin: FileHandle?
    private var buffer = Data()
    private var errTail = ""
    private var backoff: TimeInterval = 1
    private var generation = 0
    private var lastData = Date()
    private var watchdog: Timer?
    private let queue = DispatchQueue(label: "agentboard.stream")

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
        buffer = Data()
        errTail = ""
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
        stdin = inp.fileHandleForWriting
        lastData = Date()
        watchdog = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            guard let self = self, self.generation == gen else { return }
            if Date().timeIntervalSince(self.lastData) > 75 {
                self.onState?(.down("no data for 75 s; reconnecting"))
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
    }

    func send(_ obj: [String: Any]) {
        guard let h = stdin, var data = try? JSONSerialization.data(withJSONObject: obj) else { return }
        data.append(0x0A)
        queue.async { try? h.write(contentsOf: data) }
    }

    private func consume(_ data: Data, gen: Int) {
        guard !data.isEmpty else { return }
        buffer.append(data)
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer.subdata(in: buffer.startIndex..<nl)
            buffer.removeSubrange(buffer.startIndex...nl)
            guard !line.isEmpty else { continue }
            DispatchQueue.main.async { [weak self] in
                guard let self = self, self.generation == gen else { return }
                self.lastData = Date()
                self.backoff = 1
                self.onLine?(line)
            }
        }
    }

    private func ended(gen: Int, status: Int32) {
        guard gen == generation else { return }
        watchdog?.invalidate()
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

/// One validated action: `ssh HOST agent-board act` with the request as JSON on stdin. Never a shell string.
enum Actions {
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

    private static func runRaw(_ request: [String: Any]) -> ([String: Any]?, String?) {
        guard let body = try? JSONSerialization.data(withJSONObject: request) else { return (nil, "could not encode request") }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: sshPath)
        p.arguments = sshOptions + [Settings.host, "exec \(Settings.collector) act"]
        let out = Pipe(), err = Pipe(), inp = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = inp
        do { try p.run() } catch { return (nil, "could not start ssh: \(error.localizedDescription)") }
        inp.fileHandleForWriting.write(body)
        try? inp.fileHandleForWriting.close()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { return (obj, nil) }
        let e = String(data: errData, encoding: .utf8) ?? ""
        return (nil, e.isEmpty ? "no answer from the desktop" : e)
    }

    private static func runSync(_ request: [String: Any]) -> ActionResult {
        guard let body = try? JSONSerialization.data(withJSONObject: request) else {
            return ActionResult(ok: false, message: "could not encode request")
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: sshPath)
        p.arguments = sshOptions + [Settings.host, "exec \(Settings.collector) act"]
        let out = Pipe(), err = Pipe(), inp = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = inp
        do { try p.run() } catch {
            return ActionResult(ok: false, message: "could not start ssh: \(error.localizedDescription)")
        }
        inp.fileHandleForWriting.write(body)
        try? inp.fileHandleForWriting.close()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            let ok = obj["ok"] as? Bool ?? false
            let err = (obj["error"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            let msg = err ?? (obj["output"] as? String) ?? ""
            return ActionResult(ok: ok, message: msg)
        }
        let e = String(data: errData, encoding: .utf8) ?? ""
        return ActionResult(ok: false, message: e.isEmpty ? "no answer from the desktop" : e)
    }
}
