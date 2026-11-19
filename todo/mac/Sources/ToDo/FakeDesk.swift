import Foundation

/// A stand-in for todo-desk, for the self-tests and the offscreen renderer: the real link is ssh to the desktop,
/// which those runs must not need. It answers the commands it was given answers for, at once and on the caller's
/// queue (the self-tests check right after the call); any other request fails as an unconnected link does.
final class FakeDesk: DeskChannel {
    var onEvent: (([String: Any]) -> Void)?
    var onState: ((LinkState) -> Void)?
    var onConnect: (() -> Void)?
    private(set) var state: LinkState = .down("a fake desk")
    /// Every request made, in order.
    private(set) var requests: [[String: Any]] = []
    private var answers: [String: ([String: Any]) -> Result<[String: Any], LinkError>] = [:]

    /// Answer the requests whose `cmd` is `cmd`.
    func answer(_ cmd: String, _ reply: @escaping ([String: Any]) -> Result<[String: Any], LinkError>) {
        answers[cmd] = reply
    }

    func start() {
        state = .live
        onState?(.live)
        onConnect?()
    }

    func stop() {
        state = .down("stopped")
        onState?(state)
    }

    func request(_ obj: [String: Any], timeout: TimeInterval, completion: @escaping (Result<[String: Any], LinkError>) -> Void) {
        requests.append(obj)
        guard let reply = (obj["cmd"] as? String).flatMap({ answers[$0] }) else {
            completion(.failure(LinkError(message: "the desktop is not connected", offline: true)))
            return
        }
        completion(reply(obj))
    }

    /// An event from the desk, as the helper's unsolicited lines arrive.
    func send(event: [String: Any]) { onEvent?(event) }

    /// A Codable value as the JSON objects the helper sends.
    static func json<T: Encodable>(_ value: T) -> Any {
        guard let data = try? JSONEncoder().encode(value), let obj = try? JSONSerialization.jsonObject(with: data) else {
            preconditionFailure("FakeDesk.json: \(T.self) didn't encode")
        }
        return obj
    }
}
