import AppKit
import AVFoundation
import Combine
import SwiftUI

// The ask box (⌃⌥A anywhere, once turned on: Settings.askBox; README "The ask box on the Mac"): the owner talks to the
// desk (agentboard/desk.py, the always-running agent: desk_say), or asks one agent about its work, by typing or by
// dictating with whatever dictation app they use (one that types or pastes into the focused field), and hears the
// answer. Before the box appears, the desktop says which agent is behind the front window (the voice_looking
// action: agentboard/voicedesk.py's looking), which the desk is told with each question. The box opens on the desk;
// its picker asks one agent's read-only copy directly instead (voice_ask: voicedesk.ask). The desktop's voice (Kokoro:
// agentboard/speech.py) speaks either answer a sentence at a time while it is still being written, each sentence's
// audio coming as a part of the answer (AskClip), played here in order (AskSpeaker); the desk's parts also say what it
// is doing ("Asking loader-fix…"), and what it sent an agent for the owner is shown under its answer (AskSend). A
// sentence the desktop's voice could not speak comes with why, and a voice of this Mac's reads it (AskVoice), the box
// saying so. The owner talking over it interrupts it: an app starting to record (MicrophoneWatch: the dictation app, as
// the owner starts to dictate), or a new question, stops the voice at once. Nothing the box sends goes to Google, unlike
// the voice page (Gemini Live).

/// The box's rules, pure (--selftest-ask).
enum AskRules {
    /// The box's window title starts with the desktop's voicedesk.VOICE_TITLE, so that `looking` passes over the box and
    /// names the window the owner was on before (tests/test_askbox.py checks the two agree).
    static let titlePrefix = "Agent Front Desk"
    /// One change of the text that inserts this many characters or more is taken for a dictation (a dictation app pastes
    /// or types the whole of one at once), and the question is sent autoSendDelay seconds later, unless the owner edits
    /// again first.
    static let dictation = 10
    static let autoSendDelay: TimeInterval = 0.8
    /// The earlier questions and answers a follow-up carries (at most actions.HISTORY_PAIRS on the desktop), and the
    /// characters of each it sends at most: the desktop takes up to actions.HISTORY_CHARS, and puts at most
    /// voicedesk.MAX_QUESTION of the whole to the agent, so a longer answer would only be cut there.
    static let historyPairs = 3
    static let historyChars = 4000
    /// How long the box waits for the desktop to say who is behind the window before it opens anyway, still looking.
    static let lookWait: TimeInterval = 5

    static func title(_ who: String) -> String { titlePrefix + " — Ask " + who }

    /// How many characters the change from `old` to `new` inserted: `new` less what the two share at the start and at
    /// the end.
    static func inserted(from old: String, to new: String) -> Int {
        let a = Array(old), b = Array(new)
        var start = 0
        while start < a.count && start < b.count && a[start] == b[start] { start += 1 }
        var end = 0
        while end < a.count - start && end < b.count - start && a[a.count - 1 - end] == b[b.count - 1 - end] { end += 1 }
        return b.count - start - end
    }
}

/// When the box sends by itself: a dictation (AskRules.dictation) sets `deadline`, any other change clears it.
struct AutoSend {
    private(set) var deadline: TimeInterval?

    mutating func edited(from old: String, to new: String, at now: TimeInterval) {
        let dictated = AskRules.inserted(from: old, to: new) >= AskRules.dictation
        deadline = dictated && !new.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? now + AskRules.autoSendDelay : nil
    }

    func due(at now: TimeInterval) -> Bool { deadline.map { now >= $0 } ?? false }

    mutating func clear() { deadline = nil }
}

/// A live agent the box can ask, as voice_looking lists them.
struct AskAgent: Equatable, Identifiable {
    var id: String
    var name: String
    var status = ""
    var folder = ""

    var label: String { name.isEmpty ? String(id.prefix(8)) : name }
}

/// Whom the box asks: the desk (desk_say), or one agent's read-only copy (voice_ask).
enum AskTarget: Equatable {
    case desk
    case agent(AskAgent)

    var label: String {
        switch self {
        case .desk: return "the desk"
        case .agent(let a): return a.label
        }
    }

    /// The label at the start of a sentence: an agent's name as it is.
    var heading: String { self == .desk ? "The desk" : label }

    var agent: AskAgent? {
        if case .agent(let a) = self { return a }
        return nil
    }
}

/// What the desktop said of the window the owner was on: whether it could tell (`ok`), the live agent behind it, its
/// sentence about the window, and the live agents, for the picker.
struct AskLook: Equatable {
    var agent: AskAgent?
    var said: String
    var agents: [AskAgent]
    var ok = true

    static let looking = AskLook(agent: nil, said: "Still asking the desktop which agent is behind your window…",
                                 agents: [], ok: false)

    /// What the desk is told of the owner's screen with a question (desk_say's `looking`): this, as voice_looking gave it; or,
    /// while the box's own look-up has not answered, true, for the desk to read the screen itself.
    var forDesk: Any {
        if self == .looking { return true }
        var d: [String: Any] = ["ok": ok, "said": said]
        if !ok { d["error"] = said }
        if let a = agent { d["agent"] = ["id": a.id, "name": a.name, "live": true] }
        return d
    }

    /// From voice_looking's answer, or why there is none (Actions.runJSON's pair).
    static func from(_ obj: [String: Any]?, _ err: String?) -> AskLook {
        guard let obj = obj else {
            return AskLook(agent: nil, said: "The desktop did not say who is behind your window: "
                           + (err ?? "no answer"), agents: [], ok: false)
        }
        let agents = (obj["agents"] as? [[String: Any]] ?? []).compactMap { a -> AskAgent? in
            guard let id = a["id"] as? String, !id.isEmpty else { return nil }
            return AskAgent(id: id, name: a["name"] as? String ?? "", status: a["status"] as? String ?? "",
                            folder: a["folder"] as? String ?? "")
        }
        guard (obj["ok"] as? Bool) == true else {
            return AskLook(agent: nil, said: "Could not tell what is in front: "
                           + ((obj["error"] as? String) ?? err ?? "no reason given"), agents: agents, ok: false)
        }
        let said = (obj["said"] as? String) ?? ""
        guard let a = obj["agent"] as? [String: Any], (a["live"] as? Bool) == true, let id = a["id"] as? String,
              !id.isEmpty else {
            return AskLook(agent: nil, said: said, agents: agents)
        }
        let known = agents.first { $0.id == id }
        return AskLook(agent: known ?? AskAgent(id: id, name: a["name"] as? String ?? ""), said: said, agents: agents)
    }
}

/// What the desk sent an agent for the owner in a turn (desk_say's `sends`), shown under its answer.
struct AskSend: Equatable {
    var agent: String
    var text: String
    var ok: Bool
    var ref: String?
    var error: String?

    static func from(_ d: [String: Any]) -> AskSend? {
        guard let agent = d["agent"] as? String, let text = d["text"] as? String else { return nil }
        return AskSend(agent: agent, text: text, ok: (d["ok"] as? Bool) == true, ref: d["ref"] as? String,
                       error: d["error"] as? String)
    }

    var line: String {
        ok ? "Sent to \(agent)" + (ref.map { " as message \($0)" } ?? "") + ": \(text)"
            : "Not sent to \(agent) (\(error ?? "no reason given")): \(text)"
    }
}

/// One question in the box and what came of it.
struct AskExchange: Identifiable, Equatable {
    enum State: Equatable {
        case waiting                         // sent once the question before it is answered
        case asking
        case answered(String, seconds: Double?)
        case failed(String)
        case stopped                         // by Stop: an answer that comes later is dropped
    }

    let id: Int
    let target: AskTarget
    let question: String
    var state: State
    var heard: [String] = []                 // the sentences spoken so far, while it is being answered
    var fallback: String?                    // why this Mac's voice read some of it: the desktop's could not
    var status: String?                      // what the desk is doing, while it answers ("Asking loader-fix…")
    var sends: [AskSend] = []                // what the desk sent for the owner in this turn
    var screenError: String?                 // why the desk could not read the owner's screen for it

    var answer: String? {
        if case .answered(let text, _) = state { return text }
        return nil
    }
}

/// One sentence of an answer to speak, from a part of voice_ask's answer ({seq, text, audio | error}): the desktop's
/// voice for it (a WAV), or, when that could not be had, its text and why, for this Mac's voice to read.
struct AskClip: Equatable {
    let exchange: Int
    let seq: Int
    let text: String
    let audio: Data?
    let fallback: String?

    static func from(_ part: [String: Any], exchange: Int) -> AskClip? {
        guard let seq = part["seq"] as? Int, let text = part["text"] as? String else { return nil }
        let audio = (part["audio"] as? String).flatMap { Data(base64Encoded: $0) }
        let why = audio == nil ? ((part["error"] as? String) ?? "the desktop sent no audio for it") : nil
        return AskClip(exchange: exchange, seq: seq, text: text, audio: audio, fallback: why)
    }
}

/// What speaks the answers: AskSpeaker, or a test's stand-in. play() speaks one clip, its audio or else its text in
/// this Mac's voice, tells `fellBack` why when audio it was given would not play (its text is read instead), and calls
/// `done` when it has finished, never once stopped; stop() silences it at once.
@MainActor
protocol AskSpeaking: AnyObject {
    func play(_ clip: AskClip, fellBack: @escaping (String) -> Void, done: @escaping () -> Void)
    func stop()
    /// Whether this Mac has a voice to read a clip that has no audio.
    var canFallBack: Bool { get }
}

/// The voice on this Mac that reads what the desktop's voice could not speak: the one the askVoice setting names (its
/// name or identifier), the best of them, this Mac's language first; else this Mac's default voice for its language;
/// else the best of Apple's voices in that language, premium, then enhanced, then compact. Apple's own voices alone
/// (com.apple.*), which AVSpeechSynthesizer runs on the device: never a voice another app provides, which could send
/// the text anywhere.
enum AskVoice {
    struct Candidate: Equatable {
        var identifier: String
        var name: String
        var language: String
        var quality: Int                    // AVSpeechSynthesisVoiceQuality: 1 compact (default), 2 enhanced, 3 premium
    }

    /// `wanted`: Settings.askVoice; `language`: this Mac's ("en-US"); `systemDefault`: the identifier of this Mac's
    /// default voice for it.
    static func pick(_ voices: [Candidate], wanted: String?, language: String, systemDefault: String? = nil) -> Candidate? {
        let apple = voices.filter { $0.identifier.hasPrefix("com.apple.") }
        func base(_ l: String) -> String { l.split(separator: "-").first.map(String.init) ?? l }
        func rank(_ v: Candidate) -> Int { v.language == language ? 2 : base(v.language) == base(language) ? 1 : 0 }
        func best(_ vs: [Candidate]) -> Candidate? {
            vs.max { (rank($0), $0.quality, $1.identifier) < (rank($1), $1.quality, $0.identifier) }
        }
        if let w = wanted { return best(apple.filter { $0.name == w || $0.identifier == w }) }
        if let d = systemDefault, let v = apple.first(where: { $0.identifier == d }) { return v }
        return best(apple.filter { $0.identifier.hasPrefix("com.apple.voice.") && base($0.language) == base(language) })
    }

    static func installed() -> [Candidate] {
        AVSpeechSynthesisVoice.speechVoices().map {
            Candidate(identifier: $0.identifier, name: $0.name, language: $0.language, quality: $0.quality.rawValue)
        }
    }

    /// nil when no such voice is installed (none of the name askVoice gives): the box then says so and speaks nothing.
    static func best() -> AVSpeechSynthesisVoice? {
        let language = AVSpeechSynthesisVoice.currentLanguageCode()
        return pick(installed(), wanted: Settings.askVoice, language: language,
                    systemDefault: AVSpeechSynthesisVoice(language: language)?.identifier)
            .flatMap { AVSpeechSynthesisVoice(identifier: $0.identifier) }
    }
}

/// Plays the desktop's voice (a clip's WAV, AVAudioPlayer), or reads a clip that has none, or whose audio will not
/// play, in AskVoice's voice, on the device. Only the clip playing now is ever told it is done.
@MainActor
final class AskSpeaker: NSObject, AskSpeaking, AVAudioPlayerDelegate, AVSpeechSynthesizerDelegate {
    private let synth = AVSpeechSynthesizer()
    let voice = AskVoice.best()
    private var player: AVAudioPlayer?
    private var utterance: AVSpeechUtterance?
    private var text = ""                    // the clip playing: read instead, should its audio fail to decode
    private var fellBack: ((String) -> Void)?
    private var done: (() -> Void)?

    var canFallBack: Bool { voice != nil }
    var isPlaying: Bool { (player?.isPlaying ?? false) || synth.isSpeaking }

    override init() {
        super.init()
        synth.delegate = self
    }

    func play(_ clip: AskClip, fellBack: @escaping (String) -> Void, done: @escaping () -> Void) {
        stop()
        self.done = done
        self.fellBack = fellBack
        text = clip.text
        if let audio = clip.audio {
            do {
                let p = try AVAudioPlayer(data: audio)
                p.delegate = self
                player = p
                if p.play() { return }
                player = nil
                fellBack("the desktop's audio would not play")
            } catch {
                fellBack("the desktop's audio would not play: \(error.localizedDescription)")
            }
        }
        read()
    }

    /// The clip's text in this Mac's voice: it came with no audio, or its audio would not play.
    private func read() {
        guard let voice = voice, !text.isEmpty else { return finished() }
        let u = AVSpeechUtterance(string: text)
        u.voice = voice
        utterance = u
        synth.speak(u)
    }

    func stop() {
        done = nil
        fellBack = nil
        player?.stop()
        player = nil
        utterance = nil
        synth.stopSpeaking(at: .immediate)
    }

    private func finished() {
        let d = done
        done = nil
        fellBack = nil
        player = nil
        utterance = nil
        d?()
    }

    nonisolated func audioPlayerDidFinishPlaying(_ p: AVAudioPlayer, successfully flag: Bool) {
        ended(ObjectIdentifier(p))
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(_ p: AVAudioPlayer, error: Error?) {
        let why = "the desktop's audio could not be decoded" + (error.map { ": \($0.localizedDescription)" } ?? "")
        let which = ObjectIdentifier(p)
        DispatchQueue.main.async { [weak self] in
            MainActor.assumeIsolated {
                guard let self = self, self.player.map({ ObjectIdentifier($0) }) == which else { return }
                self.player = nil
                self.fellBack?(why)
                self.read()
            }
        }
    }

    nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish u: AVSpeechUtterance) {
        ended(ObjectIdentifier(u))
    }

    nonisolated private func ended(_ which: ObjectIdentifier) {
        DispatchQueue.main.async { [weak self] in
            MainActor.assumeIsolated {
                guard let self = self else { return }
                if self.player.map({ ObjectIdentifier($0) }) == which
                    || self.utterance.map({ ObjectIdentifier($0) }) == which {
                    self.finished()
                }
            }
        }
    }
}

/// The box's state: whom it asks (the desk, unless an agent is picked), the questions and answers, and the text being
/// typed or dictated. One question goes to the desktop at a time; one asked meanwhile waits its turn. A question to an
/// agent carries the box's answers from it before; one to the desk carries none (the desk remembers) but what is on
/// the owner's screen. An answer's sentences are spoken one after another, in order, as they come, until the owner
/// interrupts.
@MainActor
final class AskBox: ObservableObject {
    /// One action to the desktop: the request, what takes each part of its answer, and what takes the answer.
    typealias Send = ([String: Any], (([String: Any]) -> Void)?, @escaping ([String: Any]?, String?) -> Void) -> Void

    @Published private(set) var look: AskLook
    @Published var target: AskTarget = .desk
    @Published private(set) var exchanges: [AskExchange] = []
    @Published var text = "" {
        didSet { if text != oldValue { edited(from: oldValue) } }
    }
    @Published var note: String?

    private let send: Send
    private let speaker: AskSpeaking
    private let clock: () -> TimeInterval
    private let timers: Bool
    private var auto = AutoSend()
    private var timer: Timer?
    private var generation = 0
    private var nextID = 0
    private var clips: [AskClip] = []        // come and not yet spoken, in order of exchange and sentence
    private var speaking: AskClip?
    private var nextSeq: [Int: Int] = [:]    // exchange -> the sentence it speaks next
    private var answeredIDs: Set<Int> = []   // exchanges whose answer has come: no more sentences to wait for
    private var quietThrough = 0             // the exchanges up to this one are not spoken: the owner interrupted

    /// `send`: one action to the desktop, answering on the main queue (Actions.runJSON, or a test's). `timers`: a real
    /// timer sends a dictation; without, a test calls tick().
    init(look: AskLook, send: @escaping Send = Actions.runJSON, speaker: AskSpeaking,
         clock: @escaping () -> TimeInterval = { Date().timeIntervalSince1970 }, timers: Bool = true) {
        self.look = look
        self.send = send
        self.speaker = speaker
        self.clock = clock
        self.timers = timers
    }

    /// A later answer from the desktop (the box opened before it came): what the desk is told of the owner's screen.
    func update(_ look: AskLook) {
        self.look = look
    }

    func choose(_ target: AskTarget) {
        self.target = target
        note = nil
    }

    private func edited(from old: String) {
        auto.edited(from: old, to: text, at: clock())
        timer?.invalidate()
        timer = nil
        guard timers, let deadline = auto.deadline else { return }
        let t = Timer(timeInterval: max(0, deadline - clock()), repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    /// Send a dictation whose delay has passed with no edit since.
    func tick() {
        if auto.due(at: clock()) { submit() }
    }

    /// Enter, a dictation's delay passed, or the Ask button: the text as a question to the box's target. It interrupts
    /// the answers before it.
    func submit() {
        auto.clear()
        timer?.invalidate()
        let q = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        interrupt()
        note = nil
        nextID += 1
        exchanges.append(AskExchange(id: nextID, target: target, question: q, state: .waiting))
        text = ""
        pump()
    }

    /// The box's earlier questions to `agent` and their answers, the last AskRules.historyPairs, before exchange `i`.
    func history(before i: Int, for agent: AskAgent) -> [[String: String]] {
        exchanges[..<i].filter { $0.target.agent?.id == agent.id && $0.answer != nil }.suffix(AskRules.historyPairs)
            .map { ["question": String($0.question.prefix(AskRules.historyChars)),
                    "answer": String(($0.answer ?? "").prefix(AskRules.historyChars))] }
    }

    /// Ask the first question waiting, unless one is being asked.
    private func pump() {
        guard !exchanges.contains(where: { $0.state == .asking }),
              let i = exchanges.firstIndex(where: { $0.state == .waiting }) else { return }
        let ex = exchanges[i]
        exchanges[i].state = .asking
        let gen = generation
        let request: [String: Any]
        switch ex.target {
        case .desk:
            request = ["action": "desk_say", "text": ex.question, "looking": look.forDesk]
        case .agent(let a):
            request = ["action": "voice_ask", "agent": a.id, "question": ex.question,
                       "history": history(before: i, for: a)]
        }
        send(request, { [weak self] part in
            MainActor.assumeIsolated { self?.heard(ex.id, gen, part) }
        }) { [weak self] obj, err in
            MainActor.assumeIsolated { self?.answered(ex.id, gen, obj, err) }
        }
    }

    /// A part of an answer: one sentence to speak, in its turn, or what the desk is doing now.
    private func heard(_ id: Int, _ gen: Int, _ part: [String: Any]) {
        guard gen == generation else { return }
        if let status = part["status"] as? String {
            if let i = exchanges.firstIndex(where: { $0.id == id }), exchanges[i].state == .asking {
                exchanges[i].status = status
            }
        } else if let clip = AskClip.from(part, exchange: id) {
            take(clip)
        }
    }

    private func take(_ clip: AskClip) {
        guard let i = exchanges.firstIndex(where: { $0.id == clip.exchange }),
              clip.seq >= nextSeq[clip.exchange, default: 0],
              speaking.map({ $0.exchange != clip.exchange || $0.seq != clip.seq }) ?? true,
              !clips.contains(where: { $0.exchange == clip.exchange && $0.seq == clip.seq }) else { return }
        if case .asking = exchanges[i].state { exchanges[i].heard.append(clip.text) }
        guard clip.exchange > quietThrough else {              // interrupted: shown, never spoken
            nextSeq[clip.exchange] = clip.seq + 1
            return
        }
        if let why = clip.fallback { fellBack(clip.exchange, why) }
        clips.append(clip)
        clips.sort { ($0.exchange, $0.seq) < ($1.exchange, $1.seq) }
        speakNext()
    }

    /// Why this Mac's voice reads (some of) an exchange's answer, for the box to say.
    private func fellBack(_ id: Int, _ why: String) {
        guard let i = exchanges.firstIndex(where: { $0.id == id }) else { return }
        exchanges[i].fallback = speaker.canFallBack ? why : why + "; and this Mac has no voice to read it"
    }

    /// Speak the next clip once the one before it has finished: the next sentence of its answer, or, once that answer
    /// has come, whichever of it came next.
    private func speakNext() {
        guard speaking == nil, let clip = clips.first,
              clip.seq == nextSeq[clip.exchange, default: 0] || answeredIDs.contains(clip.exchange) else { return }
        clips.removeFirst()
        speaking = clip
        let gen = generation
        speaker.play(clip, fellBack: { [weak self] why in
            guard let self = self, gen == self.generation else { return }
            self.fellBack(clip.exchange, why)
        }) { [weak self] in
            guard let self = self, gen == self.generation else { return }
            self.speaking = nil
            self.nextSeq[clip.exchange] = clip.seq + 1
            self.speakNext()
        }
    }

    private func answered(_ id: Int, _ gen: Int, _ obj: [String: Any]?, _ err: String?) {
        guard gen == generation, let i = exchanges.firstIndex(where: { $0.id == id }) else { return }
        exchanges[i].sends = (obj?["sends"] as? [[String: Any]] ?? []).compactMap(AskSend.from)
        exchanges[i].screenError = obj?["screen_error"] as? String
        if let obj = obj, (obj["ok"] as? Bool) == true {
            let answer = ((obj["answer"] as? String) ?? (obj["text"] as? String) ?? "")   // voice_ask's, desk_say's
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let seconds = (obj["seconds"] as? Double) ?? ((obj["timings"] as? [String: Any])?["end"] as? Double)
            exchanges[i].state = .answered(answer, seconds: seconds)
            for part in obj["parts"] as? [[String: Any]] ?? [] {      // over an ssh of its own: they come in it
                if let clip = AskClip.from(part, exchange: id) { take(clip) }
            }
            if obj["parts"] == nil && !answer.isEmpty {              // a desktop that sends no parts
                take(AskClip(exchange: id, seq: 0, text: answer, audio: nil,
                             fallback: "the desktop sent no audio for this answer"))
            }
        } else {
            exchanges[i].state = .failed((obj?["error"] as? String) ?? err ?? "no answer from the desktop")
        }
        answeredIDs.insert(id)
        speakNext()
        pump()
    }

    /// New conversation: the desk forgets what was said before (desk_new); the box says when it has.
    func newConversation() {
        note = "Starting a new conversation with the desk…"
        send(["action": "desk_new"], nil) { [weak self] obj, err in
            MainActor.assumeIsolated {
                guard let self = self else { return }
                self.note = (obj?["ok"] as? Bool) == true
                    ? "A new conversation: the desk remembers nothing said before."
                    : "No new conversation: " + ((obj?["error"] as? String) ?? err ?? "no answer from the desktop")
            }
        }
    }

    /// The owner interrupts: starts to dictate (AskDesk's MicrophoneWatch), or asks again (submit). The voice stops at
    /// once, and nothing more of the answers to the questions asked so far is spoken; they still come, and are shown.
    func interrupt() {
        quietThrough = nextID
        speaker.stop()
        speaking = nil
        clips = []
    }

    /// Stop: it interrupts, and the questions not yet answered are dropped too (their copies, or the desk's turns, on
    /// the desktop finish unheard).
    func stop() {
        interrupt()
        generation += 1
        auto.clear()
        timer?.invalidate()
        for i in exchanges.indices where exchanges[i].state == .waiting || exchanges[i].state == .asking {
            exchanges[i].state = .stopped
        }
    }
}

/// The box's window: a floating panel over the current Space, full-screen apps included. Esc closes it, wherever the
/// keyboard is in it.
final class AskPanel: NSPanel {
    var onEscape: (() -> Void)?

    init(size: CGSize) {
        super.init(contentRect: NSRect(origin: .zero, size: size), styleMask: [.titled, .closable, .fullSizeContentView],
                   backing: .buffered, defer: false)
        titleVisibility = .hidden
        titlebarAppearsTransparent = true
        isMovableByWindowBackground = true
        level = .floating
        collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        isFloatingPanel = true
        hidesOnDeactivate = false
        isReleasedWhenClosed = false
    }

    override var canBecomeKey: Bool { true }

    override func cancelOperation(_ sender: Any?) { onEscape?() }

    /// ⌘V and the other editing keys go to the text, whether or not the menu-bar-only app's Edit menu would take them:
    /// dictation apps paste a dictation with ⌘V.
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let edits: [String: Selector] = ["v": #selector(NSText.paste(_:)), "c": #selector(NSText.copy(_:)),
                                         "x": #selector(NSText.cut(_:)), "a": #selector(NSText.selectAll(_:)),
                                         "z": Selector(("undo:"))]
        if event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command,
           let key = event.charactersIgnoringModifiers, let action = edits[key],
           NSApp.sendAction(action, to: nil, from: self) {
            return true
        }
        return super.performKeyEquivalent(with: event)
    }
}

/// Opens and closes the box (⌃⌥A, AppDelegate). The desktop is asked who is behind the front window before the box
/// appears (or AskRules.lookWait at most); the box activates the app, so that a dictation app types into its field, and
/// on closing hands the keyboard back to the app that was in front, unless the owner has moved to another since.
@MainActor
final class AskDesk: NSObject, NSWindowDelegate {
    private var panel: AskPanel?
    private var box: AskBox?
    private var previous: NSRunningApplication?
    private var opening = 0                  // bumped by each press and close: an older look-up's answer is dropped
    private var titles: AnyCancellable?
    private lazy var speaker = AskSpeaker()
    private var microphone: MicrophoneWatch?  // while the box is open: an app starting to record interrupts its voice

    /// ⌃⌥A: close the box when it has the keyboard, else ask about the window in front now.
    func toggle() {
        if let p = panel, p.isKeyWindow, NSApp.isActive {
            close()
            return
        }
        close(handBack: false)
        let front = NSWorkspace.shared.frontmostApplication
        previous = front?.processIdentifier == ProcessInfo.processInfo.processIdentifier ? nil : front
        opening += 1
        let n = opening
        Actions.runJSON(["action": "voice_looking"]) { [weak self] obj, err in
            MainActor.assumeIsolated {
                guard let self = self, self.opening == n else { return }
                let look = AskLook.from(obj, err)
                if let box = self.box { box.update(look) } else { self.present(look) }
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + AskRules.lookWait) { [weak self] in
            MainActor.assumeIsolated {
                guard let self = self, self.opening == n, self.box == nil else { return }
                self.present(.looking)
            }
        }
    }

    private func present(_ look: AskLook) {
        let box = AskBox(look: look, speaker: speaker)
        let panel = AskPanel(size: AskBoxView.size)
        panel.contentView = NSHostingView(rootView: AskBoxView(box: box, close: { [weak self] in self?.close() }))
        panel.onEscape = { [weak self] in self?.close() }
        panel.delegate = self
        titles = box.$target.sink { [weak panel] t in panel?.title = AskRules.title(t.label) }
        microphone = MicrophoneWatch { [weak box] app in
            NSLog("AgentBoard: %@ started to record: the ask box's voice stops", app)
            box?.interrupt()
        }
        let mouse = NSEvent.mouseLocation          // the screen the owner is working on
        if let screen = NSScreen.screens.first(where: { NSMouseInRect(mouse, $0.frame, false) }) ?? NSScreen.main {
            let f = panel.frame, v = screen.visibleFrame
            panel.setFrameTopLeftPoint(NSPoint(x: v.midX - f.width / 2, y: v.maxY - v.height * 0.15))
        }
        self.box = box
        self.panel = panel
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        focus(panel)
    }

    /// The keyboard to the box's text, once SwiftUI has made it.
    private func focus(_ panel: NSPanel, tries: Int = 5) {
        panel.contentView?.layoutSubtreeIfNeeded()
        if let tv = panel.contentView.flatMap({ AskDesk.textView(in: $0) }) {
            panel.makeFirstResponder(tv)
        } else if tries > 0 {
            DispatchQueue.main.async { [weak self, weak panel] in
                MainActor.assumeIsolated { if let p = panel { self?.focus(p, tries: tries - 1) } }
            }
        }
    }

    static func textView(in view: NSView) -> NSTextView? {
        if let tv = view as? NSTextView { return tv }
        return view.subviews.lazy.compactMap { textView(in: $0) }.first
    }

    /// Stop, close the box, and (with `handBack`, while this app is still in front) give the keyboard back to the app
    /// that was in front before.
    func close(handBack: Bool = true) {
        opening += 1
        box?.stop()
        microphone?.stop()
        microphone = nil
        titles = nil
        if let p = panel {
            p.delegate = nil
            p.orderOut(nil)
        }
        panel = nil
        box = nil
        if handBack, NSApp.isActive, let app = previous, !app.isTerminated {
            if #available(macOS 14.0, *) {
                NSApp.yieldActivation(to: app)
                _ = app.activate(from: .current, options: [])
            } else {
                _ = app.activate(options: [])
            }
        }
        if handBack { previous = nil }
    }

    func windowWillClose(_ notification: Notification) {
        close()
    }
}

/// The box: whom it asks (the desk, or the agent picked) and what is on the owner's screen, the questions and answers
/// (under the desk's, what it sent for the owner), and the text. A Stop button is always there; New conversation, for the desk.
struct AskBoxView: View {
    @ObservedObject var box: AskBox
    var close: () -> Void

    static let size = CGSize(width: 600, height: 440)

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            head
            conversation
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            field
            foot
        }
        .padding(.horizontal, 18).padding(.top, 26).padding(.bottom, 14)
        .frame(width: AskBoxView.size.width, height: AskBoxView.size.height)
    }

    private var head: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "waveform.circle.fill").font(.system(size: 26)).foregroundColor(AgentState.idle.color)
            VStack(alignment: .leading, spacing: 3) {
                Text("Ask \(box.target.label)").font(.system(size: 18, weight: .semibold))
                Text(box.look.said).font(.system(size: 12)).foregroundStyle(.secondary)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            if !box.look.agents.isEmpty || box.target != .desk {
                Menu("Change") {
                    Button("The desk") { box.choose(.desk) }
                    Divider()
                    ForEach(box.look.agents) { a in
                        Button("\(a.label)'s copy (\(a.status), \(a.folder))") { box.choose(.agent(a)) }
                    }
                }
                .menuStyle(.button).buttonStyle(.borderless).fixedSize()
            }
        }
    }

    private var conversation: some View {
        ScrollViewReader { scroll in
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    if box.exchanges.isEmpty {
                        Text(intro).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                    ForEach(box.exchanges) { ex in exchange(ex).id(ex.id) }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: box.exchanges) { ex in
                if let last = ex.last { scroll.scrollTo(last.id, anchor: .bottom) }
            }
        }
    }

    private var intro: String {
        switch box.target {
        case .desk:
            return "Ask the desk about your agents: what any of them is doing, a question for one, or a message to "
                + "send one. It knows the window you were on and remembers what you asked; the desktop's voice reads "
                + "its answer as it is written."
        case .agent(let a):
            return "Ask \(a.label) about its work, without interrupting it: a read-only copy of its conversation "
                + "answers, and the desktop's voice reads it as it is written."
        }
    }

    private func exchange(_ ex: AskExchange) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(ex.question).font(.system(size: 13, weight: .medium))
                .padding(.vertical, 6).padding(.horizontal, 10)
                .background(RoundedRectangle(cornerRadius: 8).fill(AgentState.idle.color.opacity(0.16)))
                .textSelection(.enabled)
            switch ex.state {
            case .waiting:
                Text("Asked once \(ex.target.label) has answered the question before.").foregroundStyle(.secondary)
            case .asking:
                HStack(spacing: 8) {
                    PulseCircle(color: AgentState.busy.nsColor).frame(width: 9, height: 9)
                    Text(ex.status ?? (!ex.heard.isEmpty ? "\(ex.target.heading) is answering…"
                                       : ex.target == .desk ? "Asking the desk…"
                                       : "Asking \(ex.target.label)… a read-only copy of its conversation answers."))
                        .foregroundStyle(.secondary)
                }
                if !ex.heard.isEmpty {
                    Text(ex.heard.joined(separator: " ")).font(.system(size: 14)).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            case .answered(let answer, let seconds):
                VStack(alignment: .leading, spacing: 2) {
                    Text(ex.target.heading + (seconds.map { String(format: " · %.0f s", $0) } ?? ""))
                        .font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                    Text(answer.isEmpty ? "(no answer)" : answer).font(.system(size: 14)).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            case .failed(let why):
                Text("Not answered: " + why).foregroundColor(AgentState.blocked.color)
                    .fixedSize(horizontal: false, vertical: true)
            case .stopped:
                Text("Stopped.").foregroundStyle(.secondary)
            }
            ForEach(ex.sends.indices, id: \.self) { k in
                Label(ex.sends[k].line, systemImage: ex.sends[k].ok ? "paperplane.fill" : "exclamationmark.triangle")
                    .font(.system(size: 12))
                    .foregroundColor(ex.sends[k].ok ? AgentState.idle.color : AgentState.blocked.color)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            if let why = ex.screenError {
                Label("The desk could not read your screen: \(why).", systemImage: "eye.slash")
                    .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if let why = ex.fallback {
                Label("Read by this Mac's voice: \(why).", systemImage: "speaker.slash")
                    .font(.system(size: 11)).foregroundColor(AgentState.blocked.color)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .font(.system(size: 12))
    }

    private var field: some View {
        ZStack(alignment: .topLeading) {
            AskField(box: box, onEscape: close)
            if box.text.isEmpty {
                Text("Dictate with your dictation app, or type. ↩ asks.")
                    .font(.system(size: 15)).foregroundStyle(.tertiary)
                    .padding(.horizontal, 9).padding(.vertical, 6)
                    .allowsHitTesting(false)
            }
        }
        .frame(height: 64)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: .textBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.secondary.opacity(0.35)))
    }

    private var foot: some View {
        HStack(spacing: 10) {
            Text(box.note ?? "A dictation asks by itself. Esc closes.")
                .font(.system(size: 11))
                .foregroundColor(box.note == nil ? .secondary : AgentState.blocked.color)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            if box.target == .desk {
                Button("New conversation") { box.newConversation() }
            }
            Button { box.stop() } label: { Label("Stop", systemImage: "stop.fill") }
            Button("Ask") { box.submit() }
                .disabled(box.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        }
    }
}

/// The box's text: plain, its changes to AskBox.text (where a dictation is told from typing), ↩ asking and Esc closing.
/// ⌥↩ is a new line.
struct AskField: NSViewRepresentable {
    @ObservedObject var box: AskBox
    var onEscape: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(box: box, onEscape: onEscape) }

    func makeNSView(context: Context) -> NSScrollView {
        let tv = NSTextView()
        tv.isRichText = false
        tv.importsGraphics = false
        tv.allowsUndo = true
        tv.drawsBackground = false
        tv.font = .systemFont(ofSize: 15)
        tv.isAutomaticQuoteSubstitutionEnabled = false
        tv.isAutomaticDashSubstitutionEnabled = false
        tv.isVerticallyResizable = true
        tv.isHorizontallyResizable = false
        tv.autoresizingMask = [.width]
        tv.textContainer?.widthTracksTextView = true
        tv.textContainerInset = NSSize(width: 4, height: 6)
        tv.delegate = context.coordinator
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.documentView = tv
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.box = box
        context.coordinator.onEscape = onEscape
        if let tv = scroll.documentView as? NSTextView, tv.string != box.text { tv.string = box.text }
    }

    @MainActor
    final class Coordinator: NSObject, NSTextViewDelegate {
        var box: AskBox
        var onEscape: () -> Void

        init(box: AskBox, onEscape: @escaping () -> Void) {
            self.box = box
            self.onEscape = onEscape
        }

        func textDidChange(_ n: Notification) {
            guard let tv = n.object as? NSTextView else { return }
            box.text = tv.string
        }

        func textView(_ textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            switch selector {
            case #selector(NSResponder.insertNewline(_:)):
                box.submit()
                return true
            case #selector(NSResponder.cancelOperation(_:)):
                onEscape()
                return true
            default:
                return false
            }
        }
    }
}

/// --selftest-ask: when a dictation asks by itself and typing never does; the box opening on the desk, and what a
/// question to it sends (the owner's words and screen, no history), its status lines, its answer and what it sent, a
/// new conversation; the questions, agent and history sent to an agent picked; a question asked meanwhile waiting its
/// turn; an answer's sentences played one after another in order as they come, failures not spoken; a new question,
/// or the owner starting to dictate, interrupting the voice, the rest of the answers before it shown and not spoken; a
/// sentence the desktop's voice could not speak read by this Mac's voice and said so; Stop silencing it at once and
/// dropping what was still to come; the desktop's voice_looking answers; the title; the microphone watch's starts, and
/// its being on this Mac's apps; and the voice chosen, from a made-up list and from this Mac's. Offline: a stand-in
/// desktop and speaker.
@MainActor
enum AskTest {
    /// Plays nothing: keeps each clip it is given (`played`) and the one playing, which finish() ends.
    final class Speaker: AskSpeaking {
        var played: [AskClip] = []
        var playing: AskClip?
        var stops = 0
        var canFallBack = true
        private var done: (() -> Void)?
        private var fellBack: ((String) -> Void)?
        var said: [String] { played.map(\.text) }

        func play(_ clip: AskClip, fellBack: @escaping (String) -> Void, done: @escaping () -> Void) {
            played.append(clip)
            playing = clip
            self.done = done
            self.fellBack = fellBack
        }

        func stop() {
            stops += 1
            playing = nil
            done = nil
            fellBack = nil
        }

        /// The clip playing's audio would not play: this Mac's voice reads it.
        func fail(_ why: String) { fellBack?(why) }

        /// The clip playing comes to its end.
        func finish() {
            let d = done
            playing = nil
            done = nil
            d?()
        }
    }

    /// `seconds` of silence as the desktop's voice sends audio: a WAV, 24 kHz, 16-bit mono (agentboard/speech.py).
    static func silence(_ seconds: Double) -> Data {
        let samples = Int(24000 * seconds), bytes = samples * 2
        var d = Data("RIFF".utf8)
        func le(_ v: Int, _ n: Int) { for i in 0..<n { d.append(UInt8((v >> (8 * i)) & 0xff)) } }
        le(36 + bytes, 4)
        d.append(contentsOf: Array("WAVEfmt ".utf8))
        le(16, 4); le(1, 2); le(1, 2); le(24000, 4); le(48000, 4); le(2, 2); le(16, 2)
        d.append(contentsOf: Array("data".utf8))
        le(bytes, 4)
        d.append(Data(count: bytes))
        return d
    }

    /// A part of an answer as the desktop sends it: a sentence with its audio, or with why it has none.
    static func part(_ seq: Int, _ text: String, error: String? = nil) -> [String: Any] {
        error.map { ["seq": seq, "text": text, "error": $0] }
            ?? ["seq": seq, "text": text, "audio": Data("RIFF\(seq)".utf8).base64EncodedString()]
    }

    /// A voice_ask as sent: its agent, question and history.
    struct Asked: Equatable {
        var agent: String?
        var question: String?
        var history: [[String: String]]?
    }

    static func run() -> Int32 {
        var fails = 0
        func check(_ ok: Bool, _ what: String) { print((ok ? "ok    " : "FAIL  ") + what); if !ok { fails += 1 } }

        check(AskRules.inserted(from: "", to: "a") == 1 && AskRules.inserted(from: "abc", to: "abXXXXXXXXXXc") == 10
              && AskRules.inserted(from: "aaaa", to: String(repeating: "a", count: 14)) == 10
              && AskRules.inserted(from: "hello world", to: "hello") == 0
              && AskRules.inserted(from: "old words", to: "a new sentence of words") == 17,
              "the characters one change inserts")
        var typed = AutoSend(), s = ""
        for (i, c) in "What are you working on right now?".enumerated() {
            let old = s
            s.append(c)
            typed.edited(from: old, to: s, at: Double(i) * 0.1)
        }
        check(typed.deadline == nil, "typing a letter at a time never asks by itself")
        var auto = AutoSend()
        auto.edited(from: "", to: "123456789", at: 0)
        check(auto.deadline == nil, "nine characters at once are not a dictation")
        auto.edited(from: "", to: String(repeating: " ", count: 12), at: 0)
        check(auto.deadline == nil, "nor is blank space")
        auto.edited(from: "", to: "What are you working on?", at: 100)
        check(!auto.due(at: 100.79) && auto.due(at: 100.81), "a dictation asks 0.8 s later")
        auto.edited(from: "What are you working on?", to: "What are you working on?!", at: 100.5)
        check(auto.deadline == nil, "an edit within the 0.8 s holds it")
        auto.edited(from: "x", to: "x and what about the tests?", at: 200)
        check(auto.deadline.map { abs($0 - 200.8) < 1e-9 } == true, "a dictation added to typed text asks too")

        let loader = AskAgent(id: "aaaaaaaa-0000-4000-8000-00000000000a", name: "loader-fix", status: "idle",
                              folder: "loader")
        let sweep = AskAgent(id: "cccccccc-0000-4000-8000-00000000000c", name: "sweep-runner", status: "busy",
                             folder: "experiments")
        var now = 1000.0
        var sent: [[String: Any]] = []
        var answers: [([String: Any]?, String?) -> Void] = []
        var parts: [([String: Any]) -> Void] = []
        let speaker = Speaker()
        let box = AskBox(look: AskLook(agent: loader, said: "You're looking at Chrome.", agents: [loader, sweep]),
                         send: { req, onPart, done in
                             sent.append(req)
                             parts.append(onPart ?? { _ in })
                             answers.append(done)
                         }, speaker: speaker, clock: { now }, timers: false)
        func asked(_ i: Int) -> Asked {
            Asked(agent: sent[i]["agent"] as? String, question: sent[i]["question"] as? String,
                  history: sent[i]["history"] as? [[String: String]])
        }
        check(box.target == .desk, "the box opens on the desk, whoever is behind the window")
        box.choose(.agent(loader))
        box.text = "What is left before you can merge?"
        now += 0.79
        box.tick()
        check(sent.isEmpty, "nothing is sent before the 0.8 s")
        now += 0.02
        box.tick()
        check(sent.count == 1 && sent[0]["action"] as? String == "voice_ask"
              && asked(0) == Asked(agent: loader.id, question: "What is left before you can merge?", history: [])
              && box.text.isEmpty,
              "then the dictation goes to the agent picked, by its id, and the field empties")
        parts[0](part(0, "Two things."))
        check(speaker.said == ["Two things."] && speaker.playing?.audio == Data("RIFF0".utf8)
              && box.exchanges[0].heard == ["Two things."] && box.exchanges[0].state == .asking,
              "the first sentence is played as it comes, the answer still being written, and shown")
        parts[0](part(1, "The cache and the tests."))
        check(speaker.said == ["Two things."] && box.exchanges[0].heard == ["Two things.", "The cache and the tests."],
              "the next waits for the one playing")
        speaker.finish()
        check(speaker.said == ["Two things.", "The cache and the tests."], "then it is played")
        let stopsAsked = speaker.stops
        box.text = "and the tests?"
        box.submit()
        check(sent.count == 1 && box.exchanges.map(\.state) == [.asking, .waiting] && speaker.stops == stopsAsked + 1
              && speaker.playing == nil,
              "↩ while one is being answered: its voice stops at once, and the new question waits its turn")
        parts[0](part(2, "Both are small."))
        check(speaker.said.count == 2 && box.exchanges[0].heard.last == "Both are small.",
              "the rest of the answer interrupted is shown, never spoken")
        let whole = "Two things: the cache and the tests. Both are small."
        answers[0](["ok": true, "agent": "loader-fix", "answer": " \(whole) ", "seconds": 8.24, "parts": 3], nil)
        check(box.exchanges[0].state == .answered(whole, seconds: 8.24) && speaker.said.count == 2
              && box.exchanges[0].fallback == nil,
              "its answer still comes, and is shown whole, and nothing more is spoken")
        check(sent.count == 2 && asked(1) == Asked(agent: loader.id, question: "and the tests?", history: [
                  ["question": "What is left before you can merge?", "answer": whole]]),
              "then the waiting question goes, with the answer before it")
        parts[1](part(0, "The tests are slow."))
        check(speaker.said.last == "The tests are slow." && speaker.playing?.text == "The tests are slow.",
              "and its answer is spoken as it comes")
        speaker.finish()
        answers[1](["ok": false, "error": "loader-fix's copy took over 240 seconds to answer", "parts": 1], nil)
        check(box.exchanges[1].state == .failed("loader-fix's copy took over 240 seconds to answer")
              && speaker.said.count == 3, "a failure is shown, not spoken")
        for n in 3...5 {
            box.text = "Question \(n)?"
            box.submit()
            let answer = n == 5 ? String(repeating: "x", count: AskRules.historyChars + 1) : "Answer \(n)."
            answers[n - 1](["ok": true, "answer": answer, "seconds": 1.0, "parts": 0], nil)
        }
        box.text = "And?"
        box.submit()
        check(asked(5).history?.map { $0["question"] ?? "" } == ["Question 3?", "Question 4?", "Question 5?"],
              "a follow-up carries the last 3 answered, not a failure")
        check(asked(5).history?.last?["answer"]?.count == AskRules.historyChars,
              "an answer longer than historyChars goes back cut, so the desktop never refuses the history")
        parts[5](part(0, "Three answers."))
        parts[5](part(1, "All small."))
        let stopsBefore = speaker.stops, playedBefore = speaker.played.count
        box.stop()
        check(speaker.stops == stopsBefore + 1 && speaker.playing == nil && box.exchanges.last?.state == .stopped,
              "Stop: the voice stops at once and the question not answered is dropped")
        speaker.finish()
        parts[5](part(2, "Too late."))
        answers[5](["ok": true, "answer": "Too late.", "seconds": 3.0, "parts": 3], nil)
        check(speaker.played.count == playedBefore && box.exchanges.last?.state == .stopped
              && box.exchanges.last?.heard == ["Three answers.", "All small."],
              "what was still to be spoken is dropped, and a part or an answer after Stop is neither shown nor spoken")
        box.choose(.agent(sweep))
        box.text = "What are you running?"
        box.submit()
        box.text = "And on which GPU?"
        box.submit()
        check(sent.count == 7 && box.exchanges.last?.state == .waiting, "another agent's question waits its turn too")
        box.stop()
        answers[6](["ok": true, "answer": "The sweep.", "seconds": 3.0, "parts": 0], nil)
        check(sent.count == 7 && box.exchanges.suffix(2).map(\.state) == [.stopped, .stopped],
              "Stop drops a question waiting too: it is never asked")
        box.text = "What are you running now?"
        box.submit()
        check(sent.count == 8 && asked(7) == Asked(agent: sweep.id, question: "What are you running now?", history: []),
              "after Stop the box asks again; the other agent's answers are not its history")
        let down = "the desktop's voice (agent-speech) is not answering on /h/speech.sock: Connection refused"
        parts[7](part(0, "The sweep is half done.", error: down))
        check(speaker.playing?.text == "The sweep is half done." && speaker.playing?.audio == nil
              && box.exchanges.last?.fallback == down,
              "a sentence the desktop's voice could not speak: this Mac's voice reads it, and the box says why")
        speaker.finish()
        answers[7](["ok": true, "answer": "The sweep is half done.", "seconds": 4.0, "parts": 1], nil)
        box.text = "And the next?"
        box.submit()
        answers[8](["ok": true, "answer": "Over its own ssh.", "seconds": 4.0,
                    "parts": [part(0, "Over its own ssh.")]], nil)
        check(speaker.playing?.text == "Over its own ssh." && speaker.playing?.audio != nil
              && box.exchanges.last?.fallback == nil, "an answer that brings its parts with it is played from them")
        speaker.fail("the desktop's audio would not play: not a WAV")
        check(box.exchanges.last?.fallback == "the desktop's audio would not play: not a WAV",
              "audio that would not play here: this Mac's voice reads it, and the box says why")
        speaker.finish()
        box.text = "And then?"
        box.submit()
        speaker.canFallBack = false
        answers[9](["ok": true, "answer": "From an older desktop.", "seconds": 4.0], nil)
        check(speaker.playing?.text == "From an older desktop." && speaker.playing?.audio == nil
              && box.exchanges.last?.fallback
                 == "the desktop sent no audio for this answer; and this Mac has no voice to read it",
              "an answer with no parts at all is read by this Mac's voice, said so, and said when there is none")
        speaker.finish()
        let none = AskBox(look: AskLook(agent: nil, said: "No agent found behind it.", agents: [loader]),
                          send: { req, _, _ in sent.append(req) }, speaker: speaker, clock: { now }, timers: false)
        let before = sent.count
        none.text = "What is this?"
        none.submit()
        let told = sent.last?["looking"] as? [String: Any]
        check(sent.count == before + 1 && sent.last?["action"] as? String == "desk_say" && told?["agent"] == nil
              && told?["said"] as? String == "No agent found behind it.",
              "with no agent behind the window, the desk is asked all the same, and told so")
        none.choose(.agent(loader))
        none.text = "What is this?"
        none.submit()
        check(sent.count == before + 1, "an agent picked while the desk answers: it waits its turn")
        let clip = AskClip.from(["seq": 3, "text": "Hi.", "audio": Data("RIFF".utf8).base64EncodedString()],
                                exchange: 9)
        check(clip == AskClip(exchange: 9, seq: 3, text: "Hi.", audio: Data("RIFF".utf8), fallback: nil)
              && AskClip.from(["seq": 0, "text": "Hi.", "error": "down"], exchange: 1)?.fallback == "down"
              && AskClip.from(["text": "no seq"], exchange: 1) == nil, "a part read as a clip")
        none.update(AskLook(agent: sweep, said: "", agents: [loader, sweep]))
        check(none.target == .agent(loader), "a late look-up does not change an agent picked")

        let found = AskLook.from(["ok": true, "said": "You're looking at Google Chrome: Re: Loader cache. Behind it: "
                                  + "loader-fix (matched by its mail tag [#ab12cd34]).",
                                  "agent": ["cli": "claude", "id": loader.id, "name": "loader-fix", "live": true],
                                  "agents": [["name": "loader-fix", "id": loader.id, "status": "idle", "folder": "loader"],
                                             ["name": "sweep-runner", "id": sweep.id, "status": "busy", "folder": "experiments"]]],
                                 nil)
        let foundForDesk = found.forDesk as? [String: Any]
        check(found.agent == loader && found.agents == [loader, sweep] && found.said.hasSuffix("[#ab12cd34]).")
              && AskBox(look: found, speaker: speaker).target == .desk && foundForDesk?["ok"] as? Bool == true
              && (foundForDesk?["agent"] as? [String: Any])?["id"] as? String == loader.id,
              "voice_looking: the agent found is what the desk is told, the box still on the desk")
        let notLive = AskLook.from(["ok": true, "said": "It belongs to gone, which is not live here.",
                                    "agent": ["id": "dddd", "name": "gone", "live": false], "agents": []], nil)
        check(notLive.agent == nil && notLive.said == "It belongs to gone, which is not live here.",
              "an agent that is not live is not the box's")
        let failed = AskLook.from(["ok": false, "error": "ActivityWatch is not answering",
                                   "agents": [["name": "loader-fix", "id": loader.id]]], nil)
        let failedForDesk = failed.forDesk as? [String: Any]
        check(failed.agent == nil && failed.said == "Could not tell what is in front: ActivityWatch is not answering"
              && failed.agents.map(\.id) == [loader.id] && failedForDesk?["ok"] as? Bool == false
              && failedForDesk?["error"] as? String == failed.said,
              "a failure is said (to the desk as one), and the live agents still offered")
        check(AskLook.from(nil, "no answer from the desktop in 960 s").said.hasSuffix("no answer from the desktop in 960 s"),
              "no answer is said")
        check(AskTarget.desk.heading == "The desk" && AskTarget.agent(sweep).heading == "sweep-runner",
              "a sentence starts with The desk, or an agent's name as it is")
        check(AskRules.title(AskTarget.agent(loader).label) == "Agent Front Desk — Ask loader-fix"
              && AskRules.title(AskTarget.desk.label) == "Agent Front Desk — Ask the desk",
              "the title starts with the desktop's VOICE_TITLE")

        // the desk
        var dsent: [[String: Any]] = []
        var danswers: [([String: Any]?, String?) -> Void] = []
        var dparts: [([String: Any]) -> Void] = []
        let dspeaker = Speaker()
        let desk = AskBox(look: AskLook(agent: loader, said: "You're looking at kitty. Behind it: loader-fix.",
                                        agents: [loader, sweep]),
                          send: { req, onPart, done in
                              dsent.append(req)
                              dparts.append(onPart ?? { _ in })
                              danswers.append(done)
                          }, speaker: dspeaker, clock: { now }, timers: false)
        desk.text = "Tell loader-fix to rebase onto main"
        desk.submit()
        let screen = dsent[0]["looking"] as? [String: Any]
        check(dsent[0]["action"] as? String == "desk_say" && dsent[0]["text"] as? String
                == "Tell loader-fix to rebase onto main" && dsent[0]["history"] == nil && screen?["ok"] as? Bool == true
              && screen?["said"] as? String == "You're looking at kitty. Behind it: loader-fix."
              && (screen?["agent"] as? [String: Any])?["id"] as? String == loader.id
              && (screen?["agent"] as? [String: Any])?["live"] as? Bool == true,
              "a question to the desk: the owner's words and what is on their screen, the agent behind it named")
        dparts[0](["status": "Sending loader-fix a message…"])
        check(desk.exchanges[0].status == "Sending loader-fix a message…" && dspeaker.played.isEmpty,
              "what the desk is doing is shown, not spoken")
        dparts[0](part(0, "Sent."))
        dspeaker.finish()
        danswers[0](["ok": true, "text": "Sent.", "timings": ["end": 2.4, "first_delta": 1.9], "parts": 1,
                     "sends": [["agent": "loader-fix", "id": loader.id, "text": "Rebase onto main.", "ok": true,
                                "ref": "k3f9", "error": NSNull()],
                               ["agent": "sweep-runner", "id": sweep.id, "text": "Stop.", "ok": false,
                                "ref": "m2n4", "error": "waiting in Codex's queue: busy"]],
                     "screen_error": "ssh mac: no answer"], nil)
        check(desk.exchanges[0].state == .answered("Sent.", seconds: 2.4) && dspeaker.said == ["Sent."],
              "the desk's answer is shown whole, with its time, and nothing spoken twice")
        check(desk.exchanges[0].sends.map(\.line) == [
                  "Sent to loader-fix as message k3f9: Rebase onto main.",
                  "Not sent to sweep-runner (waiting in Codex's queue: busy): Stop."]
              && desk.exchanges[0].screenError == "ssh mac: no answer",
              "what it sent for the owner is shown under the answer, a failed send too, and a screen it could not read")
        desk.text = "Is parser-fix busy?"
        desk.submit()
        dparts[1](part(0, "Parser-fix is busy."))
        dparts[1](part(1, "It is running the tests."))
        desk.interrupt()
        dparts[1](part(2, "They take an hour."))
        check(dspeaker.playing == nil && dspeaker.said == ["Sent.", "Parser-fix is busy."]
              && desk.exchanges[1].heard == ["Parser-fix is busy.", "It is running the tests.", "They take an hour."],
              "the owner starts to dictate: the voice stops at once, and the rest of the answer is shown, not spoken")
        let busy = "Parser-fix is busy. It is running the tests. They take an hour."
        danswers[1](["ok": true, "text": busy, "timings": ["end": 3.1], "parts": 3], nil)
        check(desk.exchanges[1].state == .answered(busy, seconds: 3.1) && dspeaker.said.count == 2,
              "its answer still comes whole, unspoken")
        desk.text = "And parser-fix?"
        desk.submit()
        check(dsent.count == 3 && dsent[2]["history"] == nil && dsent[2]["action"] as? String == "desk_say",
              "a follow-up to the desk carries no history: the desk remembers")
        danswers[2](["ok": false, "error": "could not reach the desk on /h/desk.sock: Connection refused", "parts": 0],
                    nil)
        check(desk.exchanges[2].state == .failed("could not reach the desk on /h/desk.sock: Connection refused"),
              "a desk that is not running is said")
        desk.choose(.agent(loader))
        desk.text = "What is left?"
        desk.submit()
        check(dsent.count == 4 && dsent[3]["action"] as? String == "voice_ask"
              && (dsent[3]["history"] as? [[String: String]])?.isEmpty == true,
              "an agent picked from the desk: its copy is asked, the desk's answers not its history")
        danswers[3](["ok": true, "answer": "Nothing.", "seconds": 5.0, "parts": 0], nil)
        desk.newConversation()
        check(dsent.last?["action"] as? String == "desk_new" && desk.note == "Starting a new conversation with the desk…",
              "New conversation asks the desk for one")
        danswers.last?(["ok": true, "text": "A new conversation has started."], nil)
        check(desk.note == "A new conversation: the desk remembers nothing said before.", "and says when it has")
        desk.newConversation()
        danswers.last?(nil, "no answer from the desktop in 960 s")
        check(desk.note == "No new conversation: no answer from the desktop in 960 s", "or why not")
        let early = AskBox(look: .looking, send: { req, _, _ in dsent.append(req) }, speaker: Speaker(),
                           clock: { now }, timers: false)
        early.text = "What am I looking at?"
        early.submit()
        check(dsent.last?["looking"] as? Bool == true,
              "asked before the box's look-up answers, the desk reads the screen itself")

        func voice(_ id: String, _ name: String, _ lang: String, q: Int) -> AskVoice.Candidate {
            AskVoice.Candidate(identifier: id, name: name, language: lang, quality: q)
        }
        let compact = voice("com.apple.voice.compact.en-US.Ava", "Ava", "en-US", q: 1)
        let enhanced = voice("com.apple.voice.enhanced.en-US.Ava", "Ava", "en-US", q: 2)
        let premium = voice("com.apple.voice.premium.en-US.Ava", "Ava", "en-US", q: 3)
        let kate = voice("com.apple.voice.premium.en-GB.Kate", "Kate", "en-GB", q: 3)
        let others = [kate, voice("com.example.cloudvoices.Ava", "Ava", "en-US", q: 3),
                      voice("com.apple.speech.synthesis.voice.Albert", "Albert", "en-US", q: 1)]
        check(AskVoice.pick(others + [compact, premium, enhanced], wanted: "Ava", language: "en-GB") == premium
              && AskVoice.pick(others + [compact, enhanced], wanted: "Ava", language: "en-GB") == enhanced
              && AskVoice.pick(others + [compact, premium], wanted: compact.identifier, language: "en-GB") == compact
              && AskVoice.pick(others, wanted: "Ava", language: "en-US") == nil
              && AskVoice.pick(others + [premium], wanted: "Nobody", language: "en-US") == nil,
              "a voice named in askVoice, by name or identifier: premium then enhanced then compact; Apple's own only")
        let all = others + [compact, enhanced, premium]
        check(AskVoice.pick(all, wanted: nil, language: "en-US", systemDefault: compact.identifier) == compact
              && AskVoice.pick(all, wanted: nil, language: "en-US", systemDefault: "com.example.cloudvoices.Ava") == premium
              && AskVoice.pick(all, wanted: nil, language: "en-AU") == kate
              && AskVoice.pick(all, wanted: nil, language: "fr-FR") == nil,
              "none named: this Mac's default voice for its language, if Apple's, else Apple's best in that language")
        let here = AskVoice.pick(AskVoice.installed(), wanted: Settings.askVoice,
                                 language: AVSpeechSynthesisVoice.currentLanguageCode(),
                                 systemDefault: AVSpeechSynthesisVoice(language: AVSpeechSynthesisVoice.currentLanguageCode())?.identifier)
        print("      this Mac's voice: " + (here.map { "\($0.identifier) (quality \($0.quality))" } ?? "none"))

        var starts: [String] = []
        let microphone = MicrophoneWatch { starts.append($0) }
        let app = AudioObjectID.max           // no app's: its rules alone, whatever this Mac's apps do meanwhile
        microphone.changed(app, recording: true)
        microphone.changed(app, recording: true)
        let once = starts.count
        microphone.changed(app, recording: false)
        microphone.changed(app, recording: true)
        check(once == 1 && starts == ["audio object \(app)", "audio object \(app)"],
              "an app starting to record is a start, said by name; its still recording is none, and its next start one")
        let call = app - 1, late = app - 2
        microphone.appeared(call, recording: true, initial: true)
        let calling = starts.count
        microphone.changed(call, recording: false)
        microphone.changed(call, recording: true)
        check(calling == 2 && starts.count == 3,
              "an app already recording as the box opens (a call) is no start, and its next start is one")
        microphone.appeared(late, recording: true, initial: false)
        microphone.appeared(late - 1, recording: false, initial: false)
        check(starts.count == 4, "an app that first appears later, already recording, is a start; one silent is none")
        print("      the apps this Mac's microphone watch is on: \(microphone.watching)")
        check(microphone.watching > 0 && microphone.stop() && microphone.watching == 0,
              "the microphone watch is on this Mac's apps, and comes off them all")

        let real = AskSpeaker()               // the real one, on silence in the desktop's format: nothing is heard
        var ended = 0, fell: [String] = []
        real.play(AskClip(exchange: 1, seq: 0, text: "", audio: Data("not a WAV".utf8), fallback: nil),
                  fellBack: { fell.append($0) }) { ended += 1 }
        check(fell.count == 1 && fell[0].hasPrefix("the desktop's audio would not play") && ended == 1,
              "audio that is not a WAV: the speaker says why, and reads the text instead (here none)")
        ended = 0
        real.play(AskClip(exchange: 1, seq: 0, text: "", audio: silence(0.2), fallback: nil),
                  fellBack: { fell.append($0) }) { ended += 1 }
        let started = real.isPlaying
        RunLoop.main.run(until: Date().addingTimeInterval(0.8))
        check(started && ended == 1 && !real.isPlaying && fell.count == 1,
              "a clip of the desktop's voice (24 kHz WAV) plays to its end, and only then is the next one's turn")
        real.play(AskClip(exchange: 1, seq: 1, text: "", audio: silence(3), fallback: nil),
                  fellBack: { fell.append($0) }) { ended += 1 }
        let playing = real.isPlaying
        real.stop()
        let stopped = !real.isPlaying
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
        check(playing && stopped && ended == 1, "Stop silences it at once, and a clip stopped is never said to end")

        print(fails == 0 ? "ask box OK" : "ask box: \(fails) FAILED")
        return fails == 0 ? 0 : 1
    }
}
