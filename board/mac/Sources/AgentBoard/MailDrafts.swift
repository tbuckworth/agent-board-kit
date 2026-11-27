import AppKit
import Combine
import CryptoKit

/// The owner's unsent Mail-tab replies, one per thread, kept on the desktop and shared with the phone (README, "Drafts";
/// agentboard/maildrafts.py), with a copy on this Mac written ahead of the desktop: FOLDER/drafts.json holds each
/// thread's draft as it is here, with the desktop's rev and draft it was last read or saved at, and FOLDER a copy of
/// each file the desktop has not taken yet, under its id (the SHA-256 of its bytes). So a dropped connection or the app
/// quitting loses nothing, and what the desktop has not taken goes to it once it answers again.
///
/// With the desktop (`call`): a thread's draft is read from it (draft_get) when the thread opens, and again when its
/// list of drafts (draft_list, read with each list of threads: refresh) shows it changed. It is saved there (draft_put,
/// from the rev it was last read at) `saveDelay` after the last change and when the owner leaves the thread, its new
/// files first (draft_file_add). A save the desktop did not take, as the draft changed elsewhere since, is kept there as a
/// version; the draft then shows as the desktop has it, and its note says so (Draft.note). A draft changed here on the
/// draft the desktop still has is saved on its newer rev. Discard is kept on the desktop as a version, which Undo
/// (MailModel) brings back, as the Versions menu brings back any (draft_restore). The list's "Draft" marks are the
/// desktop's list, with the drafts here it has not taken yet. A read or save that fails is said in the box
/// (Draft.deskProblem) until one succeeds. Without the desktop (`call` nil: renders, and the self-tests of other
/// things), drafts stay on this Mac.
@MainActor
final class MailDrafts: ObservableObject {
    /// The threads whose draft holds something to send: the list marks them "Draft".
    @Published private(set) var held: Set<String> = []
    /// Told what went wrong writing the folder, for the tab to show.
    var problem: ((String) -> Void)?
    /// What went wrong reading the saved drafts, if anything.
    private(set) var loadProblem: String?
    let folder: URL
    /// How a draft request reaches the desktop (MailModel's call); nil: drafts stay on this Mac.
    let call: MailModel.Call?
    /// How long after the last change a draft is saved on the desktop.
    let saveDelay: TimeInterval
    /// The app's: a second after typing stops.
    nonisolated static let appSaveDelay: TimeInterval = 1

    private var drafts: [String: Draft] = [:]
    private var sync: [String: Sync] = [:]
    private var desk: [String: Int] = [:]                 // the desktop's list: the threads holding a draft, their revs
    private var undone: [String: DraftSnapshot] = [:]     // drafts discarded here alone, whose files' copies Undo needs
    private var watching: [String: [AnyCancellable]] = [:]
    private var pending: DispatchWorkItem?
    private var pushes: [String: DispatchWorkItem] = [:]  // saves on the desktop waiting for typing to stop
    private var dirty = false            // changed since drafts.json was last written (or read)
    private var stuck: String?           // a drafts.json not read and not moved aside: not written over while there
    private var quitting: NSObjectProtocol?

    /// What this Mac knows of a thread's draft on the desktop.
    private struct Sync {
        var rev: Int?                    // the desktop's rev of the draft as this Mac last read or saved it; nil: never
        var desk: DraftSnapshot?         // the draft the desktop had at `rev` (nil: none)
        var read = false                 // read from the desktop since the app started: a save waits for it
        var reading = false              // draft_get on its way
        var busy = false                 // a save (its files, then draft_put), a discard or a restore on its way
        var discarding = false           // a Discard on its way, or waiting for a save to end: no save meanwhile
        var onDesk: Set<String> = []     // files the desktop has taken (draft_file_add)
        var settled: [@MainActor (Settled) -> Void] = []   // waiting until the draft is saved (Send, a restore)
    }

    /// How a wait for a draft to be saved on the desktop ended (settle).
    enum Settled: Equatable {
        case saved
        case conflict(String)            // the desktop did not take it, as the draft had changed there: the note
        case failed(String)              // the desktop could not be asked, or refused it: why
    }

    /// The desktop's answer about a thread's draft (draft_get, draft_put, draft_discard, draft_restore: maildrafts.view).
    struct Answer: Decodable {
        var rev: Int
        var draft: DraftSnapshot?
        var updated: Updated?
        var versions: [DraftVersion]
        var conflict: Bool?
        var kept: String?                // a conflict: the version keeping what this Mac saved, or nil for none
        var version: String?             // a discard: the version keeping what was discarded
        struct Updated: Decodable {
            var at: Double
            var device: String?
        }
    }

    private struct Saved: Codable {
        var text: String
        var key: String?
        var files: [DraftFile]
        var picks: [DraftPick]?           // absent from drafts saved before answers and comments were kept: none
        var comments: [DraftComment]?
        var rev: Int?                     // absent from drafts saved before the desktop kept them: never saved there
        var desk: DraftSnapshot?          // the desktop's draft at rev
    }

    /// The app's: Application Support/AgentBoard/mail-drafts.
    static var appFolder: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("AgentBoard/mail-drafts", isDirectory: true)
    }

    /// A new empty folder of this run's own, for a board that keeps nothing (renders, self-tests).
    static func scratchFolder() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("AgentBoard-drafts-\(UUID().uuidString)", isDirectory: true)
    }

    /// The SHA-256 of `data`, in hex: a file's id.
    nonisolated static func digest(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    /// Whether `name` is a file's id (a copy in the folder).
    nonisolated static func isDigest(_ name: String) -> Bool {
        name.count == 64 && name.allSatisfy { ("0"..."9").contains($0) || ("a"..."f").contains($0) }
    }

    /// The drafts saved in `folder`, saved on the desktop through `call` (nil: kept here alone) `saveDelay` after each
    /// change. A draft file whose copy is missing (and that the desktop does not have) is left out, and loadProblem says
    /// so; a copy kept under an id of an earlier build (a UUID) is renamed to its digest. A drafts.json that cannot be
    /// read is moved aside (to drafts.json.unread-<time>), so that saving does not write over it, and loadProblem says
    /// where; one that cannot be moved either is not saved over until it has gone. A draft saved before drafts were
    /// kept on the desktop (no rev) goes there at the first save (refresh, or its thread opened): the migration.
    init(folder: URL, call: MailModel.Call? = nil, saveDelay: TimeInterval = MailDrafts.appSaveDelay) {
        self.folder = folder
        self.call = call
        self.saveDelay = saveDelay
        let index = folder.appendingPathComponent("drafts.json")
        var read = true                                   // drafts.json read, or none: copies no draft names can go
        if FileManager.default.fileExists(atPath: index.path) {
            do {
                let saved = try JSONDecoder().decode([String: Saved].self, from: Data(contentsOf: index))
                var missing: [String] = []
                for (id, s) in saved {
                    let onDesk = Set(s.desk?.files.map(\.id) ?? [])
                    let files = s.files.compactMap { f -> DraftFile? in
                        if onDesk.contains(f.id) { return f }
                        guard let kept = migrate(f) else { missing.append(f.name); return nil }
                        return kept
                    }
                    adopt(id, Draft(text: s.text, files: files, key: s.key, picks: s.picks ?? [], comments: s.comments ?? []))
                    sync[id] = Sync(rev: s.rev, desk: s.desk)
                }
                if !missing.isEmpty { loadProblem = "Files missing from saved drafts, left out: " + missing.joined(separator: ", ") }
            } catch {
                read = false
                let aside = folder.appendingPathComponent("drafts.json.unread-\(Int(Date().timeIntervalSince1970))")
                let moved = (try? FileManager.default.moveItem(at: index, to: aside)) != nil
                if !moved { stuck = "\(index.path) could not be read or moved aside, so drafts are not saved over it" }
                loadProblem = "Saved drafts not read (\(error.localizedDescription)); "
                    + (moved ? "the file is kept as \(aside.path)" : "the file at \(index.path) could not be moved aside, "
                       + "so drafts are not saved until it is moved or removed")
            }
        }
        if read { tidy() }
        recount()
        quitting = NotificationCenter.default.addObserver(forName: NSApplication.willTerminateNotification, object: nil,
                                                          queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { if self?.dirty == true { self?.saveNow() } }
        }
    }

    deinit {
        if let q = quitting { NotificationCenter.default.removeObserver(q) }
    }

    /// File `f` as kept in the folder: under its digest, a copy kept under an earlier build's id renamed to it; nil when
    /// there is no copy.
    private func migrate(_ f: DraftFile) -> DraftFile? {
        let fm = FileManager.default
        guard fm.fileExists(atPath: url(f).path) else { return nil }
        guard !MailDrafts.isDigest(f.id) else { return f }
        guard let data = try? Data(contentsOf: url(f)) else { return nil }
        var g = f
        g.id = MailDrafts.digest(data)
        if fm.fileExists(atPath: url(g).path) { try? fm.removeItem(at: url(f)) } else { try? fm.moveItem(at: url(f), to: url(g)) }
        return fm.fileExists(atPath: url(g).path) ? g : nil
    }

    func draft(_ id: String) -> Draft {
        if let d = drafts[id] { return d }
        let d = Draft(text: "", files: [], key: nil)
        adopt(id, d)
        return d
    }

    /// Where file `f`'s copy is kept.
    func url(_ f: DraftFile) -> URL { folder.appendingPathComponent(f.id) }

    func data(_ f: DraftFile) throws -> Data { try Data(contentsOf: url(f)) }

    /// Whether the desktop has taken a draft of thread `id` naming file `f`, so a reply can name it there.
    func onDesk(_ f: DraftFile, of id: String) -> Bool { sync[id]?.desk?.files.contains(f) == true }

    /// What `add` did.
    enum Added: Equatable {
        case added
        case refused(String)          // none of them, and why
        case tooBig([URL])            // none: files past the bytes a reply carries, which may go to the desktop instead
    }

    /// Put `items` in thread `id`'s draft, copied into the folder under their digests (saved on the desktop with the
    /// draft); or, when the draft is frozen (Draft.frozen), they would take it past the mailroom's limits
    /// (MailModel.maxFiles, maxFileBytes), cannot be read or one is in the draft already, none of them.
    func add(_ items: [Attachments.Item], to id: String) -> Added {
        let d = draft(id)
        guard !d.frozen else {
            return .refused("Not attached. \(Draft.frozenWhy)")
        }
        if let no = Attachments.refusal(items, count: d.files.count, bytes: d.bytes, what: "a reply") {
            let urls = items.compactMap { item -> URL? in if case .file(let u) = item { return u } else { return nil } }
            return no.tooBig && urls.count == items.count ? .tooBig(urls) : .refused(no.why)
        }
        var added: [DraftFile] = []
        var written: [URL] = []
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            for item in items {
                let data = try Attachments.data(item)
                let f = DraftFile(id: MailDrafts.digest(data), name: Attachments.name(item, at: Date()), size: data.count)
                guard !(d.files + added).contains(where: { $0.id == f.id }) else {
                    throw Refusal("Not attached: \(f.name) is in this reply already.")
                }
                if !FileManager.default.fileExists(atPath: url(f).path) {
                    try data.write(to: url(f))
                    written.append(url(f))
                }
                added.append(f)
            }
        } catch {
            written.forEach { try? FileManager.default.removeItem(at: $0) }   // our own copies, none kept
            return .refused((error as? Refusal)?.why ?? "Not attached: \(error.localizedDescription)")
        }
        d.add(added)
        return .added
    }

    private struct Refusal: Error {
        let why: String
        init(_ why: String) { self.why = why }
    }

    /// Take file `f` out of thread `id`'s draft; its copy goes when nothing here needs it (tidy).
    func remove(_ f: DraftFile, from id: String) {
        guard draft(id).remove(f.id) != nil else { return }
        tidy()
        saveNow()
    }

    // MARK: the desktop

    /// Thread `id` has opened, in the tab or its window: its draft is read from the desktop.
    func opened(_ id: String) {
        _ = draft(id)
        read(id)
    }

    /// The owner has left thread `id`: its draft is saved on the desktop now.
    func left(_ id: String) {
        push(id)
    }

    /// The desktop's list of drafts (draft_list), for the list's marks, asked with each list of threads. A thread being
    /// read (`reading`: in the tab or a window) whose draft the list shows changed is read again; then every draft here
    /// the desktop has not taken is saved there (the write-ahead copy, after a dropped connection; and the drafts saved
    /// before drafts were kept on the desktop). A list that fails leaves the marks as they were.
    func refresh(reading: Set<String>) {
        guard let call = call else { return }
        call(["action": "draft_list"]) { [weak self] obj, _ in
            guard let self = self, (obj?["ok"] as? Bool) == true, let listed = obj?["drafts"] as? [String: Any] else { return }
            self.desk = listed.compactMapValues { ($0 as? [String: Any])?["rev"] as? Int }
            for id in reading {
                let s = self.sync[id] ?? Sync()
                let ours = DraftSnapshot.same(s.desk, nil) ? nil : s.rev
                if self.desk[id] != ours { self.read(id) }
            }
            for id in self.drafts.keys.sorted() where self.unsaved(id) { self.push(id) }
            self.recount()
        }
    }

    /// Whether thread `id`'s draft here holds anything the desktop has not taken.
    private func unsaved(_ id: String) -> Bool {
        guard let d = drafts[id] else { return false }
        return !DraftSnapshot.same(d.snapshot, sync[id]?.desk)
    }

    /// Read thread `id`'s draft from the desktop (draft_get), then save there what it has not taken from here.
    private func read(_ id: String) {
        guard let call = call, sync[id]?.reading != true else { return }
        sync[id, default: Sync()].reading = true
        call(["action": "draft_get", "thread": id]) { [weak self] obj, err in
            guard let self = self else { return }
            self.sync[id]?.reading = false
            let (got, why) = Actions.decodeAnswer(obj, err, key: nil, as: Answer.self)
            guard let v = got else { return self.tell(id, .failed(why ?? "no answer")) }
            self.sync[id]?.read = true
            self.took(id, v)
            self.push(id)
        }
    }

    /// The desktop's draft of thread `id` as read (`v`): shown here when nothing here is unsaved; when this Mac's
    /// unsaved change was made on the draft the desktop still has, it is to be saved on the newer rev; when both
    /// changed, the save that follows is a conflict, which the desktop keeps.
    private func took(_ id: String, _ v: Answer) {
        let d = draft(id)
        var s = sync[id] ?? Sync()
        d.versions = v.versions
        if !s.busy && DraftSnapshot.same(d.snapshot, s.desk) {
            s.rev = v.rev
            s.desk = v.draft
            sync[id] = s
            if !DraftSnapshot.same(d.snapshot, v.draft) { d.adopt(v.draft) }
        } else if DraftSnapshot.same(v.draft, s.desk) {
            s.rev = v.rev
            sync[id] = s
        }
        recount()
        saveNow()
    }

    /// Thread `id`'s draft saved on the desktop now, if it holds anything the desktop has not taken: its new files
    /// first, then the draft, from the rev it was last read at. Not while a save, a read, a discard or a restore of it
    /// is on its way (each saves again as it ends), nor before it has been read since the app started (read saves).
    private func push(_ id: String) {
        pushes.removeValue(forKey: id)?.cancel()
        guard let call = call, let d = drafts[id] else { return }
        let s = sync[id] ?? Sync()
        guard !s.busy, !s.reading, !s.discarding else { return }
        let now = d.snapshot
        guard !DraftSnapshot.same(now, s.desk) else { return tell(id, .saved) }
        guard s.read else { return read(id) }
        sync[id, default: Sync()].busy = true
        let base = s.rev ?? 0
        let files = now.files.filter { !s.onDesk.contains($0.id) && !(s.desk?.files.contains($0) ?? false) }
        upload(id, files[...]) { [weak self] why in
            guard let self = self else { return }
            if let why = why { return self.ended(id, .failed(why)) }
            guard let body = MailDrafts.json(now) else { return self.ended(id, .failed("the draft could not be encoded")) }
            call(["action": "draft_put", "thread": id, "base_rev": base, "draft": body, "device": "mac"]) { [weak self] obj, err in
                guard let self = self else { return }
                let (got, why) = Actions.decodeAnswer(obj, err, key: nil, as: Answer.self)
                guard let v = got else { return self.ended(id, .failed(why ?? "no answer")) }
                if v.conflict == true {
                    d.versions = v.versions
                    if d.snapshot != now && !d.snapshot.blank {
                        // typed while this save was on its way: saved from the same old rev too, so the desktop
                        // keeps it as a version as well, before the draft shows as the desktop has it
                        self.sync[id]?.busy = false
                        return self.push(id)
                    }
                    return self.ended(id, .conflict(self.differ(id, v)))
                }
                self.sync[id]?.rev = v.rev
                self.sync[id]?.desk = v.draft
                d.versions = v.versions
                d.note = nil
                self.ended(id, .saved)
            }
        }
    }

    /// Upload `files` of thread `id`'s draft (draft_file_add), one after another; then `done` with nil, or why one was
    /// not taken.
    private func upload(_ id: String, _ files: ArraySlice<DraftFile>, _ done: @escaping @MainActor (String?) -> Void) {
        guard let f = files.first, let call = call else { return done(nil) }
        guard let bytes = try? self.data(f) else { return done("\(f.name) is no longer on this Mac; attach it again") }
        call(["action": "draft_file_add", "thread": id, "data": bytes.base64EncodedString()]) { [weak self] obj, err in
            guard let self = self else { return }
            guard (obj?["ok"] as? Bool) == true, ((obj?["file"] as? [String: Any])?["id"] as? String) == f.id else {
                return done("\(f.name) not taken: " + ((obj?["error"] as? String) ?? err ?? "no answer"))
            }
            self.sync[id]?.onDesk.insert(f.id)
            self.upload(id, files.dropFirst(), done)
        }
    }

    /// The desktop did not take this Mac's draft of thread `id`, as the draft had changed there (`v`, which kept this
    /// Mac's as a version): the draft shows as the desktop has it, and the note says so. The note.
    private func differ(_ id: String, _ v: Answer) -> String {
        let d = draft(id)
        sync[id]?.rev = v.rev
        sync[id]?.desk = v.draft
        d.adopt(v.draft)
        d.versions = v.versions
        let place = DraftVersion.place(v.updated?.device)
        d.note = "This draft changed on \(place) since" + (v.kept == nil ? "." : "; your version is under Versions.")
        return d.note ?? ""
    }

    /// A save, discard or restore of thread `id` has ended (`how`): the next may go, and a save is due again if the
    /// draft changed meanwhile; else whatever waited for the draft to be saved is told how it went.
    private func ended(_ id: String, _ how: Settled) {
        sync[id]?.busy = false
        tidy()
        saveNow()
        recount()
        if how == .saved, unsaved(id), sync[id]?.discarding != true { return push(id) }
        tell(id, how)
    }

    /// Tell whatever waits for thread `id`'s draft to be saved (settle(_:then:)) how it went; the box says why a read
    /// or save failed, until one succeeds.
    private func tell(_ id: String, _ how: Settled) {
        switch how {
        case .failed(let why): drafts[id]?.deskProblem = why
        case .saved: drafts[id]?.deskProblem = nil
        case .conflict: break
        }
        let waiting = sync[id]?.settled ?? []
        sync[id]?.settled = []
        waiting.forEach { $0(how) }
    }

    /// `then` once thread `id`'s draft is saved on the desktop (now, when it is, or the desktop is not asked), or the save
    /// conflicts or fails: Send waits for it, so the desktop has the reply's key, and a restore, so nothing typed is lost.
    func settle(_ id: String, then: @escaping @MainActor (Settled) -> Void) {
        guard call != nil else { return then(.saved) }
        sync[id, default: Sync()].settled.append(then)
        push(id)
    }

    /// A reply of thread `id` has reached the mailroom: the desktop emptied the draft holding its key (`cleared`, at rev
    /// `rev`), which this Mac has emptied too; when it did not, the draft is read from it again.
    func sent(_ id: String, cleared: Bool, rev: Int?) {
        if cleared, let r = rev {
            sync[id]?.rev = r
            sync[id]?.desk = nil
        } else if call != nil {
            sync[id]?.read = false
            read(id)
        }
        tidy()
        saveNow()
        recount()
    }

    /// Discard: thread `id`'s draft emptied here at once, and on the desktop (draft_discard), which keeps it as a
    /// version. `done` gets that version (nil when it is here alone: the desktop not asked, or not answering, which
    /// then has it emptied by the next save, keeping it as a version too) and the draft as it was, for Undo; or nil
    /// and nil when there was nothing to discard, or the draft had changed on the desktop since, so it was not
    /// discarded there (it then shows as the desktop has it, and its note says so).
    func discard(_ id: String,
                 done: @escaping @MainActor (_ version: String?, _ before: DraftSnapshot?) -> Void = { _, _ in }) {
        let d = draft(id)
        let before = d.snapshot
        d.clear()
        guard !before.blank || before.key != nil else { return done(nil, nil) }
        guard call != nil else {
            undone[id] = before
            saveNow()
            return done(nil, before)
        }
        pushes.removeValue(forKey: id)?.cancel()
        sync[id, default: Sync()].discarding = true
        whenIdle(id) { [weak self] how in
            guard let self = self else { return }
            guard how == .saved else {                         // the save before it was not taken: the draft changed
                self.sync[id]?.discarding = false
                return done(nil, nil)
            }
            self.discardNow(id, before, done)
        }
    }

    private func discardNow(_ id: String, _ before: DraftSnapshot,
                            _ done: @escaping @MainActor (String?, DraftSnapshot?) -> Void) {
        guard let call = call else { return }
        sync[id, default: Sync()].busy = true
        call(["action": "draft_discard", "thread": id, "base_rev": sync[id]?.rev ?? 0, "device": "mac"]) { [weak self] obj, err in
            guard let self = self else { return }
            let d = self.draft(id)
            let (got, _) = Actions.decodeAnswer(obj, err, key: nil, as: Answer.self)
            self.sync[id]?.discarding = false
            guard let v = got else {
                self.undone[id] = before
                done(nil, before)
                return self.ended(id, .saved)                      // a save empties it there, kept as a version
            }
            d.versions = v.versions
            if v.conflict == true {
                self.sync[id]?.rev = v.rev
                self.sync[id]?.desk = v.draft
                d.adopt(v.draft)
                d.note = "This draft changed on \(DraftVersion.place(v.updated?.device)) since, so it was not discarded."
                done(nil, nil)
            } else {
                self.sync[id]?.rev = v.rev
                self.sync[id]?.desk = v.draft
                done(v.version, before)
            }
            self.ended(id, .saved)
        }
    }

    /// `then` once no save of thread `id`'s is on its way to the desktop: at once (.saved), or when the one on its way
    /// ends, with how it went.
    private func whenIdle(_ id: String, _ then: @escaping @MainActor (Settled) -> Void) {
        guard sync[id]?.busy == true else { return then(.saved) }
        sync[id, default: Sync()].settled.append(then)
    }

    /// Bring back version `version` of thread `id`'s draft (the Versions menu, and Undo of a Discard) from the desktop
    /// (draft_restore), once what is here is saved there (settle), so the draft it replaces is kept as a version.
    /// `done` gets nil once it shows, else why not (as when the draft changed on the desktop meanwhile, which its note
    /// then says too).
    func restore(_ id: String, version: String, done: @escaping @MainActor (String?) -> Void) {
        guard call != nil else { return done("the desktop keeps the versions, and it is not asked here") }
        settle(id) { [weak self] how in
            guard let self = self else { return }
            switch how {
            case .conflict(let note): return done(note)
            case .failed(let why): return done(why)
            case .saved: self.restoreNow(id, version, done)
            }
        }
    }

    private func restoreNow(_ id: String, _ version: String, _ done: @escaping @MainActor (String?) -> Void) {
        guard let call = call else { return }
        sync[id, default: Sync()].busy = true
        call(["action": "draft_restore", "thread": id, "version": version, "base_rev": sync[id]?.rev ?? 0,
              "device": "mac"]) { [weak self] obj, err in
            guard let self = self else { return }
            let d = self.draft(id)
            let (got, why) = Actions.decodeAnswer(obj, err, key: nil, as: Answer.self)
            defer { self.ended(id, .saved) }
            guard let v = got else { return done(why ?? "no answer") }
            self.sync[id]?.rev = v.rev
            self.sync[id]?.desk = v.draft
            d.adopt(v.draft)
            d.versions = v.versions
            if v.conflict == true {
                d.note = "This draft changed on \(DraftVersion.place(v.updated?.device)) meanwhile, so nothing was brought back."
                return done("the draft changed on \(DraftVersion.place(v.updated?.device)) meanwhile")
            }
            d.note = nil
            done(nil)
        }
    }

    /// Undo of a Discard that only this Mac has (discard's version nil): the draft as it was, its key left out, saved on
    /// the desktop with the next save.
    func bringBack(_ id: String, _ before: DraftSnapshot) {
        var s = before
        s.key = nil
        draft(id).adopt(s)
        undone[id] = nil
        saveNow()
    }

    /// A draft request, as JSON the desktop takes: `s` as maildrafts.check_content reads it.
    nonisolated static func json(_ s: DraftSnapshot) -> [String: Any]? {
        guard let data = try? JSONEncoder().encode(s) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    // MARK: this Mac's copy

    /// Remove each file's copy that nothing here needs: one in no draft here, or that the desktop has taken in the
    /// thread's draft, unless a Discard only this Mac has may bring it back.
    func tidy() {
        var keep = Set<String>()
        for (id, d) in drafts {
            let taken = Set(sync[id]?.desk?.files.map(\.id) ?? [])
            d.files.filter { !taken.contains($0.id) }.forEach { keep.insert($0.id) }
        }
        undone.values.forEach { $0.files.forEach { keep.insert($0.id) } }
        for name in (try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []
            where MailDrafts.isDigest(name) && !keep.contains(name) {
            do { try FileManager.default.removeItem(at: folder.appendingPathComponent(name)) } catch {
                problem?("A draft's copy of a file was not removed: \(error.localizedDescription)")
            }
        }
    }

    /// Write drafts.json now: every draft that holds something, a key (a reply that may be on its way) or a change the
    /// desktop has not taken, with the desktop's rev and draft it was last read or saved at. A change waiting to be
    /// written is written by this, and at the latest when the app quits. While an unread drafts.json that could not be
    /// moved aside is still there, nothing is written: `problem` says so, and the drafts stay to save.
    func saveNow() {
        pending?.cancel()
        pending = nil
        let index = folder.appendingPathComponent("drafts.json")
        if let why = stuck {
            guard !FileManager.default.fileExists(atPath: index.path) else {
                dirty = true                                  // still to save, once the file is out of the way
                problem?("Drafts not saved: " + why)
                return
            }
            stuck = nil
        }
        dirty = false
        var keep: [String: Saved] = [:]
        for (id, d) in drafts where !d.blank || d.key != nil || unsaved(id) {
            keep[id] = Saved(text: d.text, key: d.key, files: d.files, picks: d.picks, comments: d.comments,
                             rev: sync[id]?.rev, desk: sync[id]?.desk)
        }
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            try JSONEncoder().encode(keep).write(to: index, options: .atomic)
        } catch {
            problem?("Drafts not saved: \(error.localizedDescription)")
        }
    }

    /// The list's marks: each draft here that holds something, and each the desktop lists, unless this Mac has the
    /// draft at that rev or newer, or a change of it the desktop has not taken.
    private func recount() {
        var h = Set(drafts.filter { !$0.value.blank }.keys)
        for (id, rev) in desk where drafts[id] == nil || (!unsaved(id) && (sync[id]?.rev ?? -1) < rev) {
            h.insert(id)
        }
        if h != held { held = h }
    }

    /// Thread `id`'s draft has come to hold something (`blank` false) or nothing: its mark, unless the desktop lists a
    /// newer draft of it.
    private func mark(_ id: String, blank: Bool) {
        if !blank {
            held.insert(id)
        } else if !(desk[id].map { (sync[id]?.rev ?? -1) < $0 } ?? false) {
            held.remove(id)
        }
    }

    /// Keep `d` as thread `id`'s draft: the marks follow whether it holds anything, and each change is written here a
    /// moment later and saved on the desktop `saveDelay` after the last.
    private func adopt(_ id: String, _ d: Draft) {
        drafts[id] = d
        watching[id] = [
            Publishers.CombineLatest4(d.$text, d.$files, d.$picks, d.$comments)
                .map(Draft.blank)
                .removeDuplicates()
                .sink { [weak self] blank in self?.mark(id, blank: blank) },
            d.objectWillChange.sink { [weak self] _ in
                self?.saveSoon()
                self?.pushSoon(id)
            },
        ]
    }

    private func saveSoon() {
        dirty = true
        pending?.cancel()
        let work = DispatchWorkItem { [weak self] in MainActor.assumeIsolated { self?.saveNow() } }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5, execute: work)
    }

    private func pushSoon(_ id: String) {
        guard call != nil else { return }
        pushes[id]?.cancel()
        let work = DispatchWorkItem { [weak self] in MainActor.assumeIsolated { self?.push(id) } }
        pushes[id] = work
        DispatchQueue.main.asyncAfter(deadline: .now() + saveDelay, execute: work)
    }
}
