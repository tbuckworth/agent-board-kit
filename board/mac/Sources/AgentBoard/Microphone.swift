import CoreAudio
import Foundation

/// Tells when an app on this Mac starts to record from a microphone: a dictation app does as the owner starts to
/// dictate. It watches each app's own recording state (CoreAudio's process objects, kAudioProcessPropertyIsRunningInput),
/// not a device's, so a voice played on a headset that also has the microphone never counts; and it needs no microphone
/// permission, as it reads only whether an app records and hears nothing. An app already recording when the watch
/// begins (a call, say) is no start; its next one is. macOS 14.2 or later: before, it says so in the log and never
/// tells.
@MainActor
final class MicrophoneWatch {
    private let started: (String) -> Void
    private var apps: [AudioObjectID: AudioObjectPropertyListenerBlock] = [:]   // each app watched, with its listener
    private var recording: Set<AudioObjectID> = []
    private var listListener: AudioObjectPropertyListenerBlock?
    private static let system = AudioObjectID(kAudioObjectSystemObject)

    /// `started`: told, on the main queue, each time an app goes from not recording to recording, with the app's bundle
    /// id, or its pid when it has none, or "audio object N" when neither can be read.
    init(started: @escaping (String) -> Void) {
        self.started = started
        guard #available(macOS 14.2, *) else {
            NSLog("AgentBoard: this macOS cannot say when a microphone opens (14.2 and later can)")
            return
        }
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            MainActor.assumeIsolated { self?.scan(initial: false) }
        }
        var list = Self.address(kAudioHardwarePropertyProcessObjectList)
        let status = AudioObjectAddPropertyListenerBlock(Self.system, &list, .main, block)
        guard status == noErr else {
            NSLog("AgentBoard: cannot watch which apps record: OSStatus %d", status)
            return
        }
        listListener = block
        scan(initial: true)
    }

    /// How many apps it watches.
    var watching: Int { apps.count }

    /// Whether app `id` records now: a start when it did not.
    func changed(_ id: AudioObjectID, recording now: Bool) {
        if !now {
            recording.remove(id)
        } else if recording.insert(id).inserted {
            started(Self.name(id))
        }
    }

    /// App `id`, newly watched, records (`now`) or not: in the `initial` scan, an app already recording (a call) is no
    /// start, only noted, so that its next start is one; in a later scan it is a start, as an app may first appear as
    /// it starts to record.
    func appeared(_ id: AudioObjectID, recording now: Bool, initial: Bool) {
        if initial {
            if now { recording.insert(id) }
        } else {
            changed(id, recording: now)
        }
    }

    /// The watch off every app (the box closed); false, each logged, when one would not come off.
    @discardableResult
    func stop() -> Bool {
        var ok = true
        if #available(macOS 14.2, *) {
            for (id, block) in apps {
                ok = Self.unlisten(id, kAudioProcessPropertyIsRunningInput, block, gone: false) && ok
            }
            if let block = listListener {
                ok = Self.unlisten(Self.system, kAudioHardwarePropertyProcessObjectList, block, gone: false) && ok
            }
        }
        apps = [:]
        recording = []
        listListener = nil
        return ok
    }

    /// Watch each app CoreAudio lists now (appeared), and drop those gone; when the list cannot be read, keep those
    /// watched, so that an app still recording is not taken for a new start once it can be.
    @available(macOS 14.2, *)
    private func scan(initial: Bool) {
        guard let listed = Self.objects(Self.system, kAudioHardwarePropertyProcessObjectList) else { return }
        let now = Set(listed)
        for (id, block) in apps where !now.contains(id) {
            _ = Self.unlisten(id, kAudioProcessPropertyIsRunningInput, block, gone: true)
            apps[id] = nil
            recording.remove(id)
        }
        for id in now where apps[id] == nil {
            let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
                MainActor.assumeIsolated { self?.changed(id, recording: MicrophoneWatch.records(id)) }
            }
            var a = Self.address(kAudioProcessPropertyIsRunningInput)
            let status = AudioObjectAddPropertyListenerBlock(id, &a, .main, block)
            guard status == noErr else {
                NSLog("AgentBoard: cannot watch whether %@ records: OSStatus %d", Self.name(id), status)
                continue
            }
            apps[id] = block
            appeared(id, recording: Self.records(id), initial: initial)
        }
    }

    private static func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                   mElement: kAudioObjectPropertyElementMain)
    }

    /// Whether app `id` records now.
    @available(macOS 14.2, *)
    private static func records(_ id: AudioObjectID) -> Bool {
        var a = address(kAudioProcessPropertyIsRunningInput)
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        let status = AudioObjectGetPropertyData(id, &a, 0, nil, &size, &value)
        if status != noErr { NSLog("AgentBoard: cannot read whether %@ records: OSStatus %d", name(id), status) }
        return status == noErr && value != 0
    }

    /// App `id`'s bundle id, or its pid when it has none, for the log.
    private static func name(_ id: AudioObjectID) -> String {
        guard #available(macOS 14.2, *) else { return "audio object \(id)" }
        var a = address(kAudioProcessPropertyBundleID)
        var bundle: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        if AudioObjectGetPropertyData(id, &a, 0, nil, &size, &bundle) == noErr,
           let b = bundle?.takeRetainedValue() as String?, !b.isEmpty {
            return b
        }
        var p = address(kAudioProcessPropertyPID)
        var pid: pid_t = 0
        var psize = UInt32(MemoryLayout<pid_t>.size)
        return AudioObjectGetPropertyData(id, &p, 0, nil, &psize, &pid) == noErr ? "pid \(pid)" : "audio object \(id)"
    }

    /// The objects `owner` lists under `selector`; nil, logged, when they cannot be read.
    private static func objects(_ owner: AudioObjectID, _ selector: AudioObjectPropertySelector) -> [AudioObjectID]? {
        var a = address(selector)
        var size: UInt32 = 0
        var status = AudioObjectGetPropertyDataSize(owner, &a, 0, nil, &size)
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        if status == noErr { status = AudioObjectGetPropertyData(owner, &a, 0, nil, &size, &ids) }
        guard status == noErr else {
            NSLog("AgentBoard: cannot list which apps use audio: OSStatus %d", status)
            return nil
        }
        return Array(ids.prefix(Int(size) / MemoryLayout<AudioObjectID>.size))
    }

    /// Take `block` off `id`; false, logged, when it would not come off, unless the object is `gone` with its app.
    private static func unlisten(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector,
                                 _ block: @escaping AudioObjectPropertyListenerBlock, gone: Bool) -> Bool {
        var a = address(selector)
        let status = AudioObjectRemovePropertyListenerBlock(id, &a, .main, block)
        if status != noErr && !gone {
            NSLog("AgentBoard: cannot stop watching audio object %u: OSStatus %d", id, status)
        }
        return status == noErr
    }
}
