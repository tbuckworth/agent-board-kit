import AppKit
import SwiftUI
import UniformTypeIdentifiers

// Writing to agents from a box on the board: a mail reply (Mail.swift), with files, the owner's answers to an agent's
// options and comments on text of its mail, which ReplyText puts together into the text sent. The draft keeps a key while
// it is being sent, which the desktop keeps as the idempotency key (the mailroom's reply key), so sending again after a
// failure or a dropped connection sends it once. The key is a group-chat post id (Chats.swift newPostID), which also
// fits the mailroom's reply keys (16 to 64 of [A-Za-z0-9_-], not starting with "-"). Drafts are kept on the desktop,
// shared with the phone, with a write-ahead copy on the Mac (MailDrafts.swift).
//
// The group-chat sheet (Chats.swift) still has its own composer with the same key rule; one day the sheet may move
// onto Draft and ComposeBox, and newPostID here under a general name.

/// A file put in a draft. Its id is the SHA-256 of its bytes, in hex, as the desktop keeps it among the thread's drafts
/// (agentboard/maildrafts.py); until the desktop has taken a draft naming it, a copy is kept in the drafts' folder under
/// that id (MailDrafts).
struct DraftFile: Identifiable, Equatable, Codable {
    var id: String          // the SHA-256 of its bytes, in hex; also the name of its copy in the folder
    var name: String        // the file's name, as the agent gets it
    var size: Int           // bytes
}

/// A draft's content as the desktop keeps it (maildrafts.check_content) and as this Mac last read or saved it there:
/// what is typed (untrimmed), the answers, the comments, the files and the key of a reply that may be on its way.
struct DraftSnapshot: Codable, Equatable {
    var text: String
    var picks: [DraftPick]
    var comments: [DraftComment]
    var files: [DraftFile]
    var key: String?

    static let empty = DraftSnapshot(text: "", picks: [], comments: [], files: [], key: nil)

    var blank: Bool { Draft.blank(text, files, picks, comments) }

    /// Whether `a` and `b` are the same draft to the desktop, which keeps a blank one with no key as none. The phone's
    /// page has the same rule (same in web/mail/app.js): change both together.
    static func same(_ a: DraftSnapshot?, _ b: DraftSnapshot?) -> Bool {
        let x = a ?? .empty, y = b ?? .empty
        return x == y || (x.blank && x.key == nil && y.blank && y.key == nil)
    }
}

/// A version of a draft the desktop keeps (maildrafts.summary): Discard's, an older one, or one a device saved without
/// having seen the draft as it was (a conflict). The Versions menu lists them, newest first, and brings one back.
struct DraftVersion: Decodable, Equatable, Identifiable {
    var id: String
    var at: Double            // when its content was written
    var device: String?       // where: the device the desktop names ("mac" for this app)
    var why: String           // discarded, saved, cleared, conflict or replaced
    var kept: Double          // when it became a version
    var words: String         // the start of its text
    var picks: Int
    var comments: Int
    var files: Int

    /// "your Mac", or "your DEVICE" for another device the desktop names, for what a version or a change came from.
    static func place(_ device: String?) -> String { "your " + name(device) }

    /// "Mac" for this app's device (or none said), else the device as the desktop names it.
    static func name(_ device: String?) -> String { device == nil || device == "mac" ? "Mac" : device! }

    /// The menu's line: why it was kept, when it was written and where, and its first words (or what it holds). The
    /// phone's page says the same (versionLabel in web/mail/app.js): change both together.
    func label(now: Date) -> String {
        let why = ["discarded": "Discarded", "saved": "Earlier", "cleared": "Emptied", "conflict": "Not taken",
                   "replaced": "Replaced"][self.why] ?? self.why
        let held = [(picks, "answer"), (comments, "comment"), (files, "file")].filter { $0.0 > 0 }
            .map { "\($0.0) \($0.1)\($0.0 == 1 ? "" : "s")" }.joined(separator: ", ")
        let what = words.isEmpty ? "(\(held.isEmpty ? "empty" : held))" : words.count > 60 ? String(words.prefix(60)) + "…" : words
        return "\(why) \(MailFmt.at(at, now: now)), \(DraftVersion.name(device)): \(what)"
    }
}

/// The owner's answer to one of an agent's questions (a `choices` widget, widgets.js): the widget's id ("s<sent id>-<n>"),
/// the options picked (indexes from 0, ascending), the question and the picked options' labels from the widget's spec,
/// made plain (ReplyText.plain), and the owner's comment on it: nil until one is asked for (Choose with a comment…).
struct DraftPick: Codable, Equatable, Identifiable {
    var widget: String
    var picked: [Int]
    var question: String
    var labels: [String]
    var comment: String?
    var id: String { widget }
}

/// The owner's comment on text selected in an agent's mail (MailComments.swift): its id (made here; the text's
/// highlight in the document carries it), the mail ("s<id>"), where the quote starts in that mail's text, counting only
/// the characters that are not white space (so the same place is found however the text is laid out), the quote (the
/// text shown in the selection, made plain: ReplyText.plain), and the comment.
struct DraftComment: Codable, Equatable, Identifiable {
    var id: String
    var message: String
    var at: Int
    var quote: String
    var comment: String
}

/// What Send takes from a draft: the text typed (trimmed), the answers, the comments on text and the files.
struct DraftContent: Equatable {
    var text: String
    var picks: [DraftPick]
    var comments: [DraftComment]
    var files: [DraftFile]
}

/// The text a reply sends, put together from a draft's content: the Mac's copy of its form. The phone's copy is
/// web/mail/replytext.js, and both are checked on the same cases (reply-text-cases.json, MailTest.sharedCases), so a
/// change to the form goes in both and in the cases. The agent's words
/// are always quoted, as Markdown quote lines ("> "), and the owner's never. First each answered question, in the order
/// its widget comes in the thread: the question and each option picked ("> 2. LFU with ageing, 512 MB") quoted, then
/// "My answer: option 2" ("options 1 and 3" for several), and "My comment: COMMENT" when one was written; then each
/// comment on text, in the order its text comes in the thread, as the quote with the comment on the next line; then
/// what the owner typed. A blank line comes between them, and an empty comment adds nothing. The reply box shows each
/// of them whole, as sent.
enum ReplyText {
    /// The agent's words of an answer, as the box shows them and the reply quotes them: the question, then each option
    /// picked, numbered as in the mail.
    static func asked(_ p: DraftPick) -> [String] {
        [p.question] + zip(p.picked, p.labels).map { "\($0 + 1). \($1)" }
    }

    /// The owner's words of an answer: "My answer: option 2", or "My answer: options 1 and 3".
    static func answered(_ p: DraftPick) -> String {
        let n = p.picked.map { String($0 + 1) }
        let list = n.count < 2 ? n.joined() : n.dropLast().joined(separator: ", ") + " and " + (n.last ?? "")
        return "My answer: " + (n.count == 1 ? "option " : "options ") + list
    }

    static func compose(_ c: DraftContent) -> String {
        func note(_ s: String?) -> [String] {
            let t = trim(s ?? "")
            return t.isEmpty ? [] : [t]
        }
        let answers = c.picks.map {
            asked($0).flatMap(quoted) + [answered($0)] + note($0.comment).map { "My comment: " + $0 }
        }
        let quotes = c.comments.map { quoted($0.quote) + note($0.comment) }
        let typed = c.text.isEmpty ? [] : [[c.text]]
        return (answers + quotes + typed).map { $0.joined(separator: "\n") }.joined(separator: "\n\n")
    }

    /// `text`, trimmed, as Markdown quote lines: "> " before each line, ">" alone for an empty one.
    static func quoted(_ text: String) -> [String] {
        trim(text)
            .split(omittingEmptySubsequences: false, whereSeparator: \.isNewline)
            .map { $0.isEmpty ? ">" : "> " + $0 }
    }

    /// `s` with no white space at either end, white space being Unicode's (the White_Space property), as `plain` takes
    /// it, character by character (scalars). Not Foundation's whitespacesAndNewlines, which also holds the zero-width
    /// space, white space before Unicode 4.0.1 and a format character since: an invisible character is not trimmed, so
    /// what a draft holds at its ends is either white space or sent.
    static func trim(_ s: String) -> String {
        let u = s.unicodeScalars
        guard let first = u.firstIndex(where: { !$0.properties.isWhitespace }),
              let last = u.lastIndex(where: { !$0.properties.isWhitespace }) else { return "" }
        return String(u[first...last])
    }

    /// Characters that are neither white space nor ignorable but show as a blank: the braille blank and the Hangul
    /// fillers. The comments script finds quotes without them too (MailComments.script's BARE).
    static let blanks: Set<Unicode.Scalar> = ["\u{2800}", "\u{115F}", "\u{1160}", "\u{3164}", "\u{FFA0}"]

    /// Agent text as a draft takes it (an answer's question and labels, a comment's quote), so that nothing in it is
    /// sent unseen: each blank (`blanks`) a space, no default-ignorable characters (zero-width and bidirectional
    /// controls, variation selectors, tag characters), each run of white space (NBSP and the other spaces too) one
    /// space, and each line trimmed, empty lines left out; `lines` false makes it one line.
    static func plain(_ s: String, lines: Bool) -> String {
        let visible = s.unicodeScalars.compactMap { c -> Unicode.Scalar? in
            blanks.contains(c) ? " " : c.properties.isDefaultIgnorableCodePoint ? nil : c
        }
        let shown = String(String.UnicodeScalarView(visible))
        let rows = lines ? shown.split(whereSeparator: \.isNewline) : [Substring(shown)]
        return rows.map { $0.split(whereSeparator: \.isWhitespace).joined(separator: " ") }.filter { !$0.isEmpty }
            .joined(separator: "\n")
    }

    /// Where a widget ("s12-3") or a mail ("s12") comes in its thread: the agent's mail's id, as ids grow in the order
    /// mail is sent, then the widget's place in it. (The ids a draft takes are the thread's own: MailDocumentLoader
    /// checks them.)
    static func place(_ key: String) -> [Int] {
        key.dropFirst().split(separator: "-").compactMap { Int($0) }
    }
}

/// What the owner is writing in one box: text and files, answers and comments, and while it is being sent, its key.
@MainActor
final class Draft: ObservableObject {
    @Published var text: String
    @Published private(set) var files: [DraftFile]
    /// The answers, in the order their widgets come in the thread; one per widget.
    @Published private(set) var picks: [DraftPick]
    /// The comments on text, in the order their text comes in the thread.
    @Published private(set) var comments: [DraftComment]
    /// The answer (its widget's id) or comment (its id) whose comment field asked for the keyboard, until it has it.
    @Published var focus: String?
    @Published private(set) var open = false          // asked for (Reply), so shown even while empty
    @Published var wantsKeyboard = false              // Reply asked for the box: its text takes the keyboard once shown
    @Published private(set) var sending = false
    @Published var error: String?
    /// Files too big for a reply, waiting for the owner to say whether to copy them to the desktop instead.
    @Published var oversize: [URL] = []
    /// Files on their way to the desktop, by name; the reply waits for them.
    @Published private(set) var copying: [String] = []
    /// The key of the reply being sent: made at the first try and kept until the desktop has it, or until it says it
    /// refused it.
    private(set) var key: String?
    /// What the desktop said of the draft that the owner should know: that it changed elsewhere, so the version here is
    /// under Versions (MailDrafts); nil once a change has been taken.
    @Published var note: String?
    /// The versions the desktop keeps of the draft, newest first: the Versions menu.
    @Published var versions: [DraftVersion] = []
    /// Why the draft could not be read from or saved on the desktop the last time it was tried, which this Mac keeps
    /// meanwhile (MailDrafts); nil once one has succeeded.
    @Published var deskProblem: String?

    init(text: String, files: [DraftFile], key: String?, picks: [DraftPick] = [], comments: [DraftComment] = []) {
        self.text = text
        self.files = files
        self.key = key
        self.picks = picks.sorted(by: Draft.before)
        self.comments = comments.sorted(by: Draft.before)
    }

    private static func before(_ a: DraftPick, _ b: DraftPick) -> Bool {
        ReplyText.place(a.widget).lexicographicallyPrecedes(ReplyText.place(b.widget))
    }

    private static func before(_ a: DraftComment, _ b: DraftComment) -> Bool {
        (ReplyText.place(a.message) + [a.at]).lexicographicallyPrecedes(ReplyText.place(b.message) + [b.at])
    }

    /// Nothing written, answered or commented, and no file.
    var blank: Bool { Draft.blank(text, files, picks, comments) }
    nonisolated static func blank(_ text: String, _ files: [DraftFile], _ picks: [DraftPick], _ comments: [DraftComment]) -> Bool {
        ReplyText.trim(text).isEmpty && files.isEmpty && picks.isEmpty && comments.isEmpty
    }
    var content: DraftContent {
        DraftContent(text: ReplyText.trim(text), picks: picks, comments: comments, files: files)
    }
    /// The draft as the desktop keeps it (MailDrafts).
    var snapshot: DraftSnapshot {
        DraftSnapshot(text: text, picks: picks, comments: comments, files: files, key: key)
    }

    /// The draft as the desktop has it now (`s`, nil for none): another device's change, a version brought back.
    func adopt(_ s: DraftSnapshot?) {
        let s = s ?? .empty
        key = s.key
        text = s.text
        picks = s.picks.sorted(by: Draft.before)
        comments = s.comments.sorted(by: Draft.before)
        files = s.files
        focus = nil
        error = nil
    }
    /// The text Send would send now (ReplyText).
    var reply: String { ReplyText.compose(content) }
    /// Whether the draft holds still: while it holds a key, a reply that is on its way or may have reached the desktop
    /// (no answer came), until it is sent again, which the key makes send it once, or discarded. Changed and sent again
    /// under that key, the desktop would keep the first and the change would be lost.
    var frozen: Bool { key != nil }
    /// Why a frozen draft takes no change.
    static let frozenWhy = "This reply may have been sent. Send again sends this same reply, and the mailroom keeps it "
        + "only once; until then it cannot change, unless you discard it."
    /// Whether its box is on screen: asked for, or holding something to send or say.
    var shown: Bool { open || !blank || sending || error != nil || !copying.isEmpty }
    var bytes: Int { files.reduce(0) { $0 + $1.size } }

    /// Reply: show the box and give it the keyboard.
    func ask() {
        open = true
        wantsKeyboard = true
    }

    /// An option picked or unpicked in the mail (MailWidgets): `answer` replaces its question's earlier answer, keeping
    /// the comment on it, and goes when nothing is picked. With `comment` (Choose with a comment…) its comment field
    /// shows and takes the keyboard. The box opens; otherwise the keyboard stays where it is. Whether it was taken: not
    /// while the draft is frozen.
    @discardableResult
    func pick(_ answer: DraftPick, comment: Bool) -> Bool {
        guard !frozen else { return false }
        var all = picks.filter { $0.widget != answer.widget }
        if !answer.picked.isEmpty {
            var p = answer
            p.comment = picks.first { $0.widget == answer.widget }?.comment ?? (comment ? "" : nil)
            all.append(p)
            all.sort(by: Draft.before)
            if comment { focus = p.widget }
        }
        picks = all
        open = true
        return true
    }

    /// The × on an answer's line: the answer goes, and its card in the mail shows nothing picked. Not while frozen.
    func drop(_ widget: String) {
        guard !frozen else { return }
        picks.removeAll { $0.widget == widget }
    }

    /// A comment on `quote`, text selected in mail `message` starting at `at` (DraftComment): it joins the others in
    /// their order, and the box opens with the keyboard in its comment field. Whether it was taken: not while frozen.
    @discardableResult
    func addComment(message: String, at: Int, quote: String) -> Bool {
        guard !frozen else { return false }
        let c = DraftComment(id: UUID().uuidString, message: message, at: at, quote: quote, comment: "")
        comments = (comments + [c]).sorted(by: Draft.before)
        focus = c.id
        open = true
        return true
    }

    /// The × on a comment: it goes, and so does its highlight in the mail. Not while frozen.
    func uncomment(_ id: String) {
        guard !frozen else { return }
        comments.removeAll { $0.id == id }
    }

    /// What the owner writes in answer `id`'s comment field (its widget's id) or comment `id`'s. Not while frozen.
    func note(_ text: String, on id: String) {
        guard !frozen else { return }
        if let i = picks.firstIndex(where: { $0.widget == id }) {
            picks[i].comment = text
        } else if let i = comments.firstIndex(where: { $0.id == id }) {
            comments[i].comment = text
        }
    }

    /// The owner has moved to another thread: a box they opened and left empty goes.
    func leave() {
        if blank && !sending { open = false; error = nil }
    }

    /// Discard: no text, answers, comments or files, no box, and no key, so a reply that may have reached the desktop is
    /// forgotten and the next one is a reply of its own. The files' copies are the caller's to remove
    /// (MailDrafts.discard).
    func clear() {
        key = nil
        note = nil
        text = ""
        picks = []
        comments = []
        focus = nil
        files = []
        open = false
        error = nil
    }

    func add(_ new: [DraftFile]) {
        files += new
        error = nil
    }

    /// Take file `id` out of the draft; nil when it is not there, or the draft is frozen.
    func remove(_ id: String) -> DraftFile? {
        guard !frozen, let i = files.firstIndex(where: { $0.id == id }) else { return nil }
        return files.remove(at: i)
    }

    /// File `name` is on its way to the desktop.
    func copyStarted(_ name: String) {
        copying.append(name)
        open = true
    }

    /// File `name` has reached the desktop, and `line` (where it is) joins the text; or it has not, and `failure` says
    /// why.
    func copyEnded(_ name: String, line: String?, failure: String?) {
        if let i = copying.firstIndex(of: name) { copying.remove(at: i) }
        if let l = line { text = ReplyText.trim(text).isEmpty ? l : text + "\n\n" + l }
        if let f = failure { error = f }
    }

    /// The content and the key, to send now; nil when there is nothing to send, a send is under way or a file is still
    /// on its way to the desktop.
    func begin() -> (content: DraftContent, key: String)? {
        guard !blank, !sending, copying.isEmpty else { return nil }
        let k = key ?? newPostID()
        key = k
        sending = true
        error = nil
        return (content, k)
    }

    /// The send is over. With no `failure` the desktop has it: the key and the draft's content go (it was frozen while
    /// it went), and the box closes. With one, everything stays and `failure` says why; the key stays too when the
    /// desktop may have the reply (`maybeSent`: no answer came), so the draft stays frozen until Send sends it once, and
    /// goes when the desktop refused it, so it can be changed.
    func finish(failure: String?, maybeSent: Bool) {
        sending = false
        if let f = failure {
            error = f
            if !maybeSent { key = nil }
            return
        }
        key = nil
        error = nil
        note = nil
        text = ""
        picks = []
        comments = []
        files = []
        open = false
    }
}

/// Files on a pasteboard as a reply takes them: files (copied in the Finder, dragged from anywhere), else an image
/// with no text beside it (a screenshot copied to the clipboard, a browser's Copy Image), as PNG. Text is left to be
/// typed.
enum Attachments {
    enum Item: Equatable {
        case file(URL)
        case image(Data)          // PNG
    }

    private static let fileURLs: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]

    /// Whether `pb` holds anything `items` takes; cheap, for a drag's every move.
    static func holds(_ pb: NSPasteboard) -> Bool {
        pb.canReadObject(forClasses: [NSURL.self], options: fileURLs)
            || (pb.string(forType: .string) == nil && pb.canReadItem(withDataConformingToTypes: [UTType.image.identifier]))
    }

    static func items(_ pb: NSPasteboard) -> [Item] {
        if let urls = pb.readObjects(forClasses: [NSURL.self], options: fileURLs) as? [URL], !urls.isEmpty {
            return urls.map { .file($0) }
        }
        guard pb.string(forType: .string) == nil else { return [] }
        if let png = pb.data(forType: .png) { return [.image(png)] }
        guard let tiff = NSImage(pasteboard: pb)?.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else { return [] }
        return [.image(png)]
    }

    /// Why an item cannot be attached: a folder, or a file that cannot be read.
    struct Unreadable: Error {
        let why: String
    }

    /// The bytes `item` holds, without reading a file's. Throws Unreadable for a folder or a file it cannot look at.
    private static func size(_ item: Item) throws -> Int {
        switch item {
        case .image(let data):
            return data.count
        case .file(let src):
            let v: URLResourceValues
            do { v = try src.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey]) } catch {
                throw Unreadable(why: "\(src.lastPathComponent) could not be read: \(error.localizedDescription)")
            }
            guard v.isRegularFile == true, let n = v.fileSize else {
                throw Unreadable(why: "\(src.lastPathComponent) is not a file (a folder?); attach the files in it.")
            }
            return n
        }
    }

    /// Why `items` cannot join `count` files of `bytes` bytes in one reply or message (`what`, as "a reply"), within
    /// the mailroom's limits (MailModel.maxFiles, maxFileBytes), or nil. `tooBig` is true when only their bytes are
    /// too many, so that a caller may offer something else (MailDrafts.Added.tooBig).
    @MainActor
    static func refusal(_ items: [Item], count: Int, bytes: Int, what: String) -> (why: String, tooBig: Bool)? {
        guard count + items.count <= MailModel.maxFiles else {
            return ("Not attached: \(what) carries at most \(MailModel.maxFiles) files.", false)
        }
        var total = bytes
        for item in items {
            do { total += try size(item) } catch {
                return ("Not attached: \((error as? Unreadable)?.why ?? error.localizedDescription)", false)
            }
        }
        guard total <= MailModel.maxFileBytes else {
            return ("Not attached: that makes \(Fmt.bytes(Double(total))) of files, and \(what) carries at most "
                    + "\(Fmt.bytes(Double(MailModel.maxFileBytes))).", true)
        }
        return nil
    }

    /// The bytes of `item`: a pasted image's, or a file's, read now.
    static func data(_ item: Item) throws -> Data {
        switch item {
        case .image(let png): return png
        case .file(let src): return try Data(contentsOf: src)
        }
    }

    /// The name an item is sent under: a file's own, or "Pasted image HH.mm.ss.png".
    static func name(_ item: Item, at date: Date) -> String {
        switch item {
        case .file(let url): return url.lastPathComponent
        case .image: return "Pasted image \(MailFmt.formatter("HH.mm.ss", .current).string(from: date)).png"
        }
    }
}

/// How a box takes files: from a pasteboard (a paste or a drop: whether it took any), from the paperclip's panel, how
/// one is taken out again, and what is offered for files too big for it: `offer` says it, under `action`'s name.
struct ComposeFiles {
    let take: (NSPasteboard) -> Bool
    let pick: () -> Void
    let remove: (DraftFile) -> Void
    let offer: (_ files: [URL]) -> String
    let action: String
    let instead: ([URL]) -> Void
}

/// The box: the owner's answers and comments on text above its text (⌘↩ or Send sends them all, ReplyText; with
/// `returnSends`, Return alone in any of its fields does too, and ⇧↩ or ⌥↩ makes a new line), each shown whole as it is
/// sent, its files, why the last send failed, what the desktop said of the draft (Draft.note), `footer` beside Send,
/// the Versions menu (the desktop's versions of the draft, one brought back by `restore`) and Discard, which the Undo
/// bar can put back. While the draft is frozen (Draft.frozen) nothing in it can be changed, and the box says why.
struct ComposeBox<Footer: View>: View {
    @ObservedObject var draft: Draft
    let placeholder: String
    let height: CGFloat
    let returnSends: Bool
    let files: ComposeFiles
    let send: () -> Void
    let discard: () -> Void
    let restore: ((DraftVersion) -> Void)?
    let footer: Footer
    @Environment(\.zoom) private var z
    @State private var entries = CGRect.zero          // the answers and quotes, whole, where they are in their scroll view
    @State private var seenEnd = false                // they have been scrolled to their end since they last changed

    /// The most height the answers and quotes take before they scroll (before zoom), so Send stays in view.
    static var maxEntries: CGFloat { 260 }

    init(draft: Draft, placeholder: String, height: CGFloat, returnSends: Bool, files: ComposeFiles,
         send: @escaping () -> Void, discard: @escaping () -> Void, restore: ((DraftVersion) -> Void)? = nil,
         @ViewBuilder footer: () -> Footer) {
        self.draft = draft
        self.placeholder = placeholder
        self.height = height
        self.returnSends = returnSends
        self.files = files
        self.send = send
        self.discard = discard
        self.restore = restore
        self.footer = footer()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6 * z) {
            if !draft.picks.isEmpty || !draft.comments.isEmpty { entryList }
            ComposeField(text: $draft.text, wantsKeyboard: $draft.wantsKeyboard, placeholder: placeholder, height: height,
                         editable: !draft.frozen, take: files.take, submit: returnKey)
            if !draft.files.isEmpty { chips }
            if !draft.copying.isEmpty {
                HStack(spacing: 6 * z) {
                    ProgressView().controlSize(.small)
                    Text("Copying \(draft.copying.joined(separator: ", ")) to the desktop…").lineLimit(1)
                }
                .font(.system(size: 11.5 * z)).foregroundStyle(.secondary)
            }
            if let e = draft.error {
                Text(e).font(.system(size: 11.5 * z)).foregroundColor(AgentState.blocked.color).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let p = draft.deskProblem {
                Text("Kept on this Mac, not yet on the desktop: \(p)").font(.system(size: 11.5 * z))
                    .foregroundStyle(.secondary).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            if let n = draft.note {
                Text(n).font(.system(size: 11.5 * z, weight: .medium)).foregroundColor(Palette.attention)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            if draft.frozen && !draft.sending {
                Text(Draft.frozenWhy)
                    .font(.system(size: 11.5 * z)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8 * z) {
                footer
                Spacer()
                if draft.sending { ProgressView().controlSize(.small) }
                Button { files.pick() } label: { Image(systemName: "paperclip") }
                    .buttonStyle(.borderless).disabled(draft.frozen)
                    .help("Attach files (or drag them here, or paste with ⌘V)")
                if let restore = restore, !draft.versions.isEmpty { versionsMenu(restore) }
                Button { discard() } label: { Image(systemName: "trash") }
                    .buttonStyle(.borderless).disabled(draft.sending || !draft.copying.isEmpty)
                    .help("Discard this draft (Undo puts it back, and Versions keeps it)")
                Button("Send") { send() }
                    .keyboardShortcut(.return, modifiers: [.command])
                    .disabled(!canSend)
            }
        }
        .alert("Too big to send", isPresented: Binding(get: { !draft.oversize.isEmpty },
                                                        set: { if !$0 { draft.oversize = [] } })) {
            Button("Cancel", role: .cancel) {}
            Button(files.action) { files.instead(draft.oversize) }
        } message: {
            Text(files.offer(draft.oversize))
        }
    }

    /// The Versions menu: the desktop's versions of the draft, newest first, each by why it was kept, when and where
    /// it was written and its first words (DraftVersion.label); choosing one brings it back as the draft.
    func versionsMenu(_ restore: @escaping (DraftVersion) -> Void) -> some View {
        Menu {
            Section("Versions the desktop keeps") {
                ForEach(draft.versions) { v in
                    Button(v.label(now: Date())) { restore(v) }
                }
            }
        } label: {
            Image(systemName: "clock.arrow.circlepath")
        }
        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
        .disabled(draft.sending || draft.frozen)
        .help("Versions: bring back an earlier or discarded draft")
    }

    /// Whether the answers and quotes are taller than their room and have not been scrolled to their end since they
    /// last changed: Send (and ⌘↩) waits, so nothing in them is sent unread.
    var unseen: Bool { entries.height > ComposeBox.maxEntries * z && !seenEnd }

    /// Whether Send (and ⌘↩) sends now.
    var canSend: Bool { !draft.sending && !draft.blank && draft.copying.isEmpty && !unseen }

    /// What Return alone does in the box's fields, with `returnSends`: what Send does, and nothing while Send waits;
    /// nil, a new line, without it.
    var returnKey: (() -> Void)? {
        guard returnSends else { return nil }
        return { if canSend { send() } }
    }

    /// What the answers and quotes hold of the agent's words, which, when it changes, must be read to the end again
    /// (the owner's comments in them aside).
    var agentsWords: [String] { draft.picks.flatMap(ReplyText.asked) + draft.comments.map(\.quote) }

    /// The answers and the quotes, each whole as it is sent (ReplyText): an answer's question and options picked as a
    /// quote, then the owner's answer, a comment's quote, each with its ×, and their comment fields. Past maxEntries they
    /// scroll, and until they have been scrolled to their end a line under them says so and Send waits (unseen); macOS
    /// may hide the scroll bar until they are scrolled.
    @ViewBuilder var entryList: some View {
        ScrollView(.vertical) {
            VStack(alignment: .leading, spacing: 6 * z) {
                ForEach(draft.picks) { p in
                    entry(remove: { draft.drop(p.widget) }, help: "Take this answer out") {
                        VStack(alignment: .leading, spacing: 2 * z) {
                            quote(ReplyText.asked(p).joined(separator: "\n"))
                            Text(ReplyText.answered(p)).font(.system(size: 12.5 * z, weight: .medium))
                        }
                    }
                    if let c = p.comment { note(c, on: p.widget, placeholder: "Your comment on this choice…") }
                }
                ForEach(draft.comments) { c in
                    entry(remove: { draft.uncomment(c.id) }, help: "Take this comment out") { quote(c.quote) }
                    note(c.comment, on: c.id, placeholder: "Your comment on this…")
                }
            }
            .padding(.trailing, 4 * z)
            .background(GeometryReader { g in
                Color.clear.preference(key: EntriesFrame.self, value: g.frame(in: .named(EntriesFrame.space)))
            })
        }
        .scrollIndicators(.visible)
        .coordinateSpace(name: EntriesFrame.space)
        .frame(height: min(entries.height, ComposeBox.maxEntries * z))
        .onPreferenceChange(EntriesFrame.self) { f in
            entries = f
            if f.maxY <= min(f.height, ComposeBox.maxEntries * z) + 1 { seenEnd = true }
        }
        .onChange(of: agentsWords) { _ in seenEnd = false }
        if unseen {
            Text("More below: scroll to the end to send.")
                .font(.system(size: 11.5 * z, weight: .medium)).foregroundColor(Palette.attention)
        }
    }

    /// The agent's words in an entry, whole, left to right, in the order the reply sends them (Fmt.leftToRight), with
    /// the highlight's colour beside them.
    func quote(_ text: String) -> some View {
        Text(Fmt.leftToRight(text)).font(.system(size: 12.5 * z)).italic().foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            .padding(.leading, 7 * z)
            .overlay(alignment: .leading) { Rectangle().fill(Color(hex: MailComments.highlight)).frame(width: 3 * z) }
    }

    /// An answer's or a quote's lines, with its ×.
    func entry<Line: View>(remove: @escaping () -> Void, help: String, @ViewBuilder line: () -> Line) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6 * z) {
            line().frame(maxWidth: .infinity, alignment: .leading)
            Button(action: remove) { Image(systemName: "xmark.circle.fill") }
                .buttonStyle(.borderless).foregroundStyle(.secondary).disabled(draft.frozen).help(help)
        }
    }

    /// The comment field of answer or comment `id`, which takes the keyboard when the draft's focus names it.
    func note(_ text: String, on id: String, placeholder: String) -> some View {
        let keyboard = Binding(get: { draft.focus == id }, set: { if !$0, draft.focus == id { draft.focus = nil } })
        return ComposeField(text: Binding(get: { text }, set: { draft.note($0, on: id) }), wantsKeyboard: keyboard,
                            placeholder: placeholder, height: 40, editable: !draft.frozen, take: files.take,
                            submit: returnKey)
            .padding(.leading, 14 * z)
    }

    var chips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6 * z) {
                ForEach(draft.files) { f in
                    HStack(spacing: 4 * z) {
                        Image(systemName: "doc").foregroundStyle(.secondary)
                        Text(f.name).lineLimit(1)
                        Text(Fmt.bytes(Double(f.size))).foregroundStyle(.secondary)
                        Button { files.remove(f) } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.borderless).foregroundStyle(.secondary).disabled(draft.frozen)
                            .help("Take this file out")
                    }
                    .font(.system(size: 11.5 * z))
                    .padding(.horizontal, 7 * z).padding(.vertical, 3 * z)
                    .background(Capsule().fill(Color.primary.opacity(0.07)))
                }
            }
        }
    }
}

/// Where the box's answers and quotes are, whole, in their scroll view (ComposeBox.entryList): their height, and how far
/// they are scrolled.
private struct EntriesFrame: PreferenceKey {
    static let space = "entries"
    static var defaultValue: CGRect { .zero }
    static func reduce(value: inout CGRect, nextValue: () -> CGRect) {
        let next = nextValue()
        if next != .zero { value = next }                // views around it that set nothing give the default
    }
}

/// A text field of the box, `height` points high before zoom: ComposeText, with `placeholder` while it is empty.
struct ComposeField: View {
    @Binding var text: String
    @Binding var wantsKeyboard: Bool
    let placeholder: String
    let height: CGFloat
    let editable: Bool
    let take: (NSPasteboard) -> Bool
    let submit: (() -> Void)?
    @Environment(\.zoom) private var z

    var body: some View {
        ZStack(alignment: .topLeading) {
            ComposeText(text: $text, wantsKeyboard: $wantsKeyboard, size: 13 * z, editable: editable, take: take,
                        submit: submit)
            if text.isEmpty {
                Text(placeholder)
                    .font(.system(size: 13 * z)).foregroundStyle(.tertiary)
                    .padding(.horizontal, 7 * z).padding(.vertical, 6 * z)
                    .allowsHitTesting(false)
            }
        }
        .frame(height: height * z)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .textBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.35)))
    }
}

/// The box's text: a plain-text NSTextView that hands files and images pasted or dropped on it to `take` rather than
/// typing their names (FileTakingTextView), calls `submit`, when it is given, for Return alone rather than typing a new
/// line, takes the keyboard when `wantsKeyboard` is set, then clears it, and takes no typing unless `editable`. A draft
/// can be shown in two windows at once (the Mail tab and the thread's own window, MailWindows): only the box in the key
/// window takes the keyboard, so Reply in one leaves the other window's keys as they were (takesKeyboard); with no key
/// window (the app in the background, offscreen renders) each box asking takes it in its own window.
struct ComposeText: NSViewRepresentable {
    @Binding var text: String
    @Binding var wantsKeyboard: Bool
    let size: CGFloat
    let editable: Bool
    let take: (NSPasteboard) -> Bool
    let submit: (() -> Void)?

    func makeCoordinator() -> Coordinator { Coordinator(text: $text) }

    func makeNSView(context: Context) -> NSScrollView {
        let tv = FileTakingTextView()
        tv.isRichText = false
        tv.importsGraphics = false
        tv.allowsUndo = true
        tv.drawsBackground = false
        tv.isVerticallyResizable = true
        tv.isHorizontallyResizable = false
        tv.autoresizingMask = [.width]
        tv.textContainer?.widthTracksTextView = true
        tv.textContainerInset = NSSize(width: 2, height: 6)
        tv.delegate = context.coordinator
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.documentView = tv
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let tv = scroll.documentView as? FileTakingTextView else { return }
        context.coordinator.text = $text
        tv.take = take
        tv.submit = submit
        tv.isEditable = editable
        if tv.font?.pointSize != size { tv.font = .systemFont(ofSize: size) }
        if tv.string != text { tv.string = text }
        if wantsKeyboard {
            tv.wantsKeyboard = { [weak tv] in
                guard let tv = tv, let w = tv.window else { return }
                guard ComposeText.takesKeyboard(w, key: NSApp.keyWindow) else { tv.wantsKeyboard = nil; return }
                guard w.makeFirstResponder(tv) else { return }
                tv.wantsKeyboard = nil
                wantsKeyboard = false
            }
            DispatchQueue.main.async { tv.wantsKeyboard?() }
        }
    }

    /// Whether a box in window `w` takes the keyboard it was asked for, `key` being the app's key window: in the key
    /// window, or in any when none is key.
    static func takesKeyboard(_ w: NSWindow, key: NSWindow?) -> Bool { key == nil || key === w }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var text: Binding<String>
        init(text: Binding<String>) { self.text = text }

        func textDidChange(_ n: Notification) {
            guard let tv = n.object as? NSTextView else { return }
            text.wrappedValue = tv.string
        }
    }
}

/// An NSTextView that gives files and images pasted or dropped on it to `take` (Attachments), when it is set, keeps for
/// later a wish for the keyboard made before it was in a window, and, given a `submit`, calls it for Return (or keypad
/// Enter) held with exactly the modifiers `submitWith` (of ⌘, ⌥, ⌃ and ⇧) rather than typing a new line: ⌘↩ in a
/// dialog's box (textArea), Return alone in a fast-mode thread's reply box (ComposeBox). Return with any other modifiers
/// still types a new line, and a Return that ends an input method's composing stays the input method's.
final class FileTakingTextView: NSTextView {
    var take: ((NSPasteboard) -> Bool)?
    var wantsKeyboard: (() -> Void)?
    var submit: (() -> Void)?
    var submitWith: NSEvent.ModifierFlags = []

    override func keyDown(with event: NSEvent) {
        if let submit = submit, FileTakingTextView.submits(event, with: submitWith), !hasMarkedText() {
            submit()
            return
        }
        super.keyDown(with: event)
    }

    /// Whether `event` is Return or keypad Enter held with exactly `modifiers` of ⌘, ⌥, ⌃ and ⇧.
    static func submits(_ event: NSEvent, with modifiers: NSEvent.ModifierFlags) -> Bool {
        (event.keyCode == 36 || event.keyCode == 76)
            && event.modifierFlags.intersection([.command, .option, .control, .shift]) == modifiers
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        wantsKeyboard?()
    }

    override func paste(_ sender: Any?) {
        if take?(.general) == true { return }
        super.paste(sender)
    }

    /// The types it pastes, which decide whether Paste (⌘V) is offered: a text box's, and, given a `take`, every image
    /// type too, as a screenshot copied to the clipboard is an image alone.
    override var readablePasteboardTypes: [NSPasteboard.PasteboardType] {
        take == nil ? super.readablePasteboardTypes
            : super.readablePasteboardTypes + NSImage.imageTypes.map { NSPasteboard.PasteboardType(rawValue: $0) }
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        takes(sender) ? .copy : super.draggingEntered(sender)
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        takes(sender) ? .copy : super.draggingUpdated(sender)
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        if takes(sender), take?(sender.draggingPasteboard) == true { return true }
        return super.performDragOperation(sender)
    }

    /// Whether a drag brings what `take` takes: none without it.
    private func takes(_ sender: NSDraggingInfo) -> Bool {
        take != nil && Attachments.holds(sender.draggingPasteboard)
    }
}
