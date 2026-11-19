import AppKit
import Combine

/// The owner's unsent Mail-tab replies, one per thread, kept on this Mac so that a draft stays with its thread while
/// they read others and survives the app quitting. Text and key go to FOLDER/drafts.json a moment after each change (and
/// when the app quits); each file is copied into FOLDER under its id when it is added, and the copy goes when the file
/// is sent, taken out or discarded. A reply on its way when the app quit keeps its key, so sending it again sends it
/// once.
@MainActor
final class MailDrafts: ObservableObject {
    /// The threads whose draft holds text or files: the list marks them "Draft".
    @Published private(set) var held: Set<String> = []
    /// Told what went wrong writing the folder, for the tab to show.
    var problem: ((String) -> Void)?
    /// What went wrong reading the saved drafts, if anything.
    private(set) var loadProblem: String?
    let folder: URL

    private var drafts: [String: Draft] = [:]
    private var watching: [String: [AnyCancellable]] = [:]
    private var pending: DispatchWorkItem?
    private var dirty = false            // changed since drafts.json was last written (or read)
    private var stuck: String?           // a drafts.json not read and not moved aside: not written over while there
    private var quitting: NSObjectProtocol?

    private struct Saved: Codable {
        var text: String
        var key: String?
        var files: [DraftFile]
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

    /// The drafts saved in `folder`. A draft file whose copy is missing is left out, and loadProblem says so. A
    /// drafts.json that cannot be read is moved aside (to drafts.json.unread-<time>), so that saving does not write
    /// over it, and loadProblem says where; one that cannot be moved either is not saved over until it has gone.
    init(folder: URL) {
        self.folder = folder
        let index = folder.appendingPathComponent("drafts.json")
        if FileManager.default.fileExists(atPath: index.path) {
            do {
                let saved = try JSONDecoder().decode([String: Saved].self, from: Data(contentsOf: index))
                var missing: [String] = []
                for (id, s) in saved {
                    let files = s.files.filter { FileManager.default.fileExists(atPath: url($0).path) }
                    missing += s.files.filter { !files.contains($0) }.map(\.name)
                    adopt(id, Draft(text: s.text, files: files, key: s.key))
                }
                if !missing.isEmpty { loadProblem = "Files missing from saved drafts, left out: " + missing.joined(separator: ", ") }
            } catch {
                let aside = folder.appendingPathComponent("drafts.json.unread-\(Int(Date().timeIntervalSince1970))")
                let moved = (try? FileManager.default.moveItem(at: index, to: aside)) != nil
                if !moved { stuck = "\(index.path) could not be read or moved aside, so drafts are not saved over it" }
                loadProblem = "Saved drafts not read (\(error.localizedDescription)); "
                    + (moved ? "the file is kept as \(aside.path)" : "the file at \(index.path) could not be moved aside, "
                       + "so drafts are not saved until it is moved or removed")
            }
        }
        quitting = NotificationCenter.default.addObserver(forName: NSApplication.willTerminateNotification, object: nil,
                                                          queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { if self?.dirty == true { self?.saveNow() } }
        }
    }

    deinit {
        if let q = quitting { NotificationCenter.default.removeObserver(q) }
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

    /// What `add` did.
    enum Added: Equatable {
        case added
        case refused(String)          // none of them, and why
        case tooBig([URL])            // none: files past the bytes a reply carries, which may go to the desktop instead
    }

    /// Put `items` in thread `id`'s draft, copied into the folder; or, when they would take it past the mailroom's
    /// limits (MailModel.maxFiles, maxFileBytes) or cannot be read, none of them.
    func add(_ items: [Attachments.Item], to id: String) -> Added {
        let d = draft(id)
        guard d.files.count + items.count <= MailModel.maxFiles else {
            return .refused("Not attached: a reply carries at most \(MailModel.maxFiles) files.")
        }
        var sizes: [Int] = []
        for item in items {
            switch item {
            case .image(let data):
                sizes.append(data.count)
            case .file(let src):
                let v: URLResourceValues
                do { v = try src.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey]) } catch {
                    return .refused("Not attached: \(src.lastPathComponent) could not be read: \(error.localizedDescription)")
                }
                guard v.isRegularFile == true, let n = v.fileSize else {
                    return .refused("Not attached: \(src.lastPathComponent) is not a file (a folder?); attach the files in it.")
                }
                sizes.append(n)
            }
        }
        let total = d.bytes + sizes.reduce(0, +)
        guard total <= MailModel.maxFileBytes else {
            let urls = items.compactMap { item -> URL? in if case .file(let u) = item { return u } else { return nil } }
            return urls.count == items.count ? .tooBig(urls)
                : .refused("Not attached: that makes \(Fmt.bytes(Double(total))) of files, and a reply carries at most "
                           + "\(Fmt.bytes(Double(MailModel.maxFileBytes))).")
        }
        var added: [DraftFile] = []
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            for (item, n) in zip(items, sizes) {
                let f = DraftFile(id: UUID().uuidString, name: MailDrafts.name(item, at: Date()), size: n)
                switch item {
                case .image(let data): try data.write(to: url(f))
                case .file(let src): try FileManager.default.copyItem(at: src, to: url(f))
                }
                added.append(f)
            }
        } catch {
            added.forEach { try? FileManager.default.removeItem(at: url($0)) }   // our own copies, none kept
            return .refused("Not attached: \(error.localizedDescription)")
        }
        d.add(added)
        return .added
    }

    /// The name an item is sent under: a file's own, or "Pasted image HH.mm.ss.png".
    static func name(_ item: Attachments.Item, at date: Date) -> String {
        switch item {
        case .file(let url): return url.lastPathComponent
        case .image: return "Pasted image \(MailFmt.formatter("HH.mm.ss", .current).string(from: date)).png"
        }
    }

    /// Take file `f` out of thread `id`'s draft, and its copy.
    func remove(_ f: DraftFile, from id: String) {
        guard draft(id).remove(f.id) != nil else { return }
        forget([f])
    }

    /// Thread `id`'s draft emptied and closed, its files' copies removed.
    func discard(_ id: String) {
        let d = draft(id)
        let files = d.files
        d.clear()
        forget(files)
    }

    /// Files the desktop has now: their copies go.
    func sent(_ files: [DraftFile]) { forget(files) }

    /// Write drafts.json now: every draft that holds something, or a key (a reply that may be on its way). A
    /// change waiting to be saved is saved by this, and at the latest when the app quits. While an unread drafts.json
    /// that could not be moved aside is still there, nothing is written: `problem` says so, and the drafts stay to save.
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
        let keep = drafts.filter { !$0.value.blank || $0.value.key != nil }
            .mapValues { Saved(text: $0.text, key: $0.key, files: $0.files) }
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            try JSONEncoder().encode(keep).write(to: index, options: .atomic)
        } catch {
            problem?("Drafts not saved: \(error.localizedDescription)")
        }
    }

    private func forget(_ files: [DraftFile]) {
        for f in files {
            do { try FileManager.default.removeItem(at: url(f)) } catch {
                problem?("A draft's copy of \(f.name) was not removed: \(error.localizedDescription)")
            }
        }
        saveNow()
    }

    /// Keep `d` as thread `id`'s draft: `held` follows whether it holds anything, and each change is saved a moment
    /// later.
    private func adopt(_ id: String, _ d: Draft) {
        drafts[id] = d
        if !d.blank { held.insert(id) }
        watching[id] = [
            Publishers.CombineLatest(d.$text, d.$files)
                .map(Draft.blank)
                .removeDuplicates()
                .sink { [weak self] blank in
                    if blank { self?.held.remove(id) } else { self?.held.insert(id) }
                },
            d.objectWillChange.sink { [weak self] _ in self?.saveSoon() },
        ]
    }

    private func saveSoon() {
        dirty = true
        pending?.cancel()
        let work = DispatchWorkItem { [weak self] in MainActor.assumeIsolated { self?.saveNow() } }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5, execute: work)
    }
}
