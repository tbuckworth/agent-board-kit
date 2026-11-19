import AppKit
import SwiftUI
import UniformTypeIdentifiers

// Writing to agents from a box on the board: a mail reply (Mail.swift), with files. The draft keeps a key while it is
// being sent, which the desktop keeps as the idempotency key (the mailroom's reply key), so sending again after a
// failure or a dropped connection sends it once. The key is a group-chat post id (Chats.swift newPostID), which also
// fits the mailroom's reply keys (16 to 64 of [A-Za-z0-9_-], not starting with "-"). Drafts are kept on the Mac
// (MailDrafts.swift).
//
// The group-chat sheet (Chats.swift) still has its own composer with the same key rule; one day the sheet may move
// onto Draft and ComposeBox, and newPostID here under a general name.

/// A file put in a draft. Its bytes are kept in the drafts' folder under `id` (MailDrafts) until it is sent or removed.
struct DraftFile: Identifiable, Equatable, Codable {
    var id: String          // a UUID, also the name of its copy in the folder
    var name: String        // the file's name, as the agent gets it
    var size: Int           // bytes
}

/// What the owner is writing in one box: text and files, and while it is being sent, its key.
@MainActor
final class Draft: ObservableObject {
    @Published var text: String
    @Published private(set) var files: [DraftFile]
    @Published private(set) var open = false          // asked for (Reply), so shown even while empty
    @Published var wantsKeyboard = false              // Reply asked for the box: its text takes the keyboard once shown
    @Published private(set) var sending = false
    @Published var error: String?
    /// Files too big for a reply, waiting for the owner to say whether to copy them to the desktop instead.
    @Published var oversize: [URL] = []
    /// Files on their way to the desktop, by name; the reply waits for them.
    @Published private(set) var copying: [String] = []
    /// The key of the reply being sent: made at the first try and kept until the desktop has it.
    private(set) var key: String?
    /// The line an option picked in the mail put first in the text (choose), while it is still there as it was put;
    /// not kept across a restart of the app.
    private(set) var choice: String?

    init(text: String, files: [DraftFile], key: String?) {
        self.text = text
        self.files = files
        self.key = key
    }

    /// Nothing written and no file.
    var blank: Bool { Draft.blank(text, files) }
    static func blank(_ text: String, _ files: [DraftFile]) -> Bool {
        text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && files.isEmpty
    }
    /// Whether its box is on screen: asked for, or holding something to send or say.
    var shown: Bool { open || !blank || sending || error != nil || !copying.isEmpty }
    var bytes: Int { files.reduce(0) { $0 + $1.size } }

    /// Reply: show the box and give it the keyboard.
    func ask() {
        open = true
        wantsKeyboard = true
    }

    /// An option picked in the mail: `line` ("Option 2: ...", from MailWidgets) becomes the text's first line,
    /// replacing the line an earlier pick put there if it is still there unchanged, and keeping everything else; an
    /// empty `line` (nothing picked) only takes that line out. The box opens with the keyboard in it.
    func choose(_ line: String) {
        var rest = text
        if let old = choice, rest == old || rest.hasPrefix(old + "\n") {        // its whole first line, unedited
            rest = rest == old ? "" : String(rest.dropFirst(old.count + 1))
        }
        text = line.isEmpty ? rest : rest.isEmpty ? line : line + "\n" + rest
        choice = line.isEmpty ? nil : line
        ask()
    }

    /// The owner has moved to another thread: a box they opened and left empty goes.
    func leave() {
        if blank && !sending { open = false; error = nil }
    }

    /// Discard: no text, no files, no box. The files' copies are the caller's to remove (MailDrafts.discard).
    func clear() {
        text = ""
        choice = nil
        files = []
        open = false
        error = nil
    }

    func add(_ new: [DraftFile]) {
        files += new
        error = nil
    }

    /// Take file `id` out of the draft; nil when it is not there.
    func remove(_ id: String) -> DraftFile? {
        guard let i = files.firstIndex(where: { $0.id == id }) else { return nil }
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
        if let l = line { text = text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? l : text + "\n\n" + l }
        if let f = failure { error = f }
    }

    /// The trimmed text, the files and the key, to send now; nil when there is nothing to send, a send is under way or
    /// a file is still on its way to the desktop.
    func begin() -> (text: String, files: [DraftFile], key: String)? {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !(t.isEmpty && files.isEmpty), !sending, copying.isEmpty else { return nil }
        let k = key ?? newPostID()
        key = k
        sending = true
        error = nil
        return (t, files, k)
    }

    /// The send of `sent` (text and files) is over. With no `failure` the desktop has it: the key goes, the files sent
    /// leave the draft, the text goes unless the owner has typed on, and an empty box closes. Otherwise the key, text and
    /// files stay for a retry, and `failure` says why.
    func finish(_ sent: (text: String, files: [DraftFile]), failure: String?) {
        sending = false
        if let f = failure { error = f; return }
        key = nil
        error = nil
        if text.trimmingCharacters(in: .whitespacesAndNewlines) == sent.text { text = ""; choice = nil }
        files.removeAll { sent.files.contains($0) }
        if blank { open = false }
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

/// The box: its text (⌘↩ or Send sends it), its files, why the last send failed, `footer` beside Send, and Discard.
struct ComposeBox<Footer: View>: View {
    @ObservedObject var draft: Draft
    let placeholder: String
    let height: CGFloat
    let files: ComposeFiles
    let send: () -> Void
    let discard: () -> Void
    let footer: Footer
    @Environment(\.zoom) private var z
    @State private var confirmDiscard = false

    init(draft: Draft, placeholder: String, height: CGFloat, files: ComposeFiles, send: @escaping () -> Void,
         discard: @escaping () -> Void, @ViewBuilder footer: () -> Footer) {
        self.draft = draft
        self.placeholder = placeholder
        self.height = height
        self.files = files
        self.send = send
        self.discard = discard
        self.footer = footer()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6 * z) {
            ZStack(alignment: .topLeading) {
                ComposeText(text: $draft.text, wantsKeyboard: $draft.wantsKeyboard, size: 13 * z, take: files.take)
                if draft.text.isEmpty {
                    Text(placeholder)
                        .font(.system(size: 13 * z)).foregroundStyle(.tertiary)
                        .padding(.horizontal, 7 * z).padding(.vertical, 6 * z)
                        .allowsHitTesting(false)
                }
            }
            .frame(height: height * z)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .textBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.35)))
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
            HStack(spacing: 8 * z) {
                footer
                Spacer()
                if draft.sending { ProgressView().controlSize(.small) }
                Button { files.pick() } label: { Image(systemName: "paperclip") }
                    .buttonStyle(.borderless).disabled(draft.sending)
                    .help("Attach files (or drag them here, or paste with ⌘V)")
                Button { if draft.blank { discard() } else { confirmDiscard = true } } label: { Image(systemName: "trash") }
                    .buttonStyle(.borderless).disabled(draft.sending || !draft.copying.isEmpty)
                    .help("Discard this draft")
                Button("Send") { send() }
                    .keyboardShortcut(.return, modifiers: [.command])
                    .disabled(draft.sending || draft.blank || !draft.copying.isEmpty)
            }
            .alert("Discard this draft?", isPresented: $confirmDiscard) {
                Button("Cancel", role: .cancel) {}
                Button("Discard", role: .destructive) { discard() }
            } message: {
                Text("Its text and files are gone for good.")
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

    var chips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6 * z) {
                ForEach(draft.files) { f in
                    HStack(spacing: 4 * z) {
                        Image(systemName: "doc").foregroundStyle(.secondary)
                        Text(f.name).lineLimit(1)
                        Text(Fmt.bytes(Double(f.size))).foregroundStyle(.secondary)
                        Button { files.remove(f) } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.borderless).foregroundStyle(.secondary).disabled(draft.sending)
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

/// The box's text: a plain-text NSTextView that hands files and images pasted or dropped on it to `take` rather than
/// typing their names (FileTakingTextView), and takes the keyboard when `wantsKeyboard` is set, then clears it.
struct ComposeText: NSViewRepresentable {
    @Binding var text: String
    @Binding var wantsKeyboard: Bool
    let size: CGFloat
    let take: (NSPasteboard) -> Bool

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
        if tv.font?.pointSize != size { tv.font = .systemFont(ofSize: size) }
        if tv.string != text { tv.string = text }
        if wantsKeyboard {
            tv.wantsKeyboard = { [weak tv] in
                guard let tv = tv, let w = tv.window, w.makeFirstResponder(tv) else { return }
                tv.wantsKeyboard = nil
                wantsKeyboard = false
            }
            DispatchQueue.main.async { tv.wantsKeyboard?() }
        }
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var text: Binding<String>
        init(text: Binding<String>) { self.text = text }

        func textDidChange(_ n: Notification) {
            guard let tv = n.object as? NSTextView else { return }
            text.wrappedValue = tv.string
        }
    }
}

/// An NSTextView that gives files and images pasted or dropped on it to `take` (Attachments), and keeps for later a
/// wish for the keyboard made before it was in a window.
final class FileTakingTextView: NSTextView {
    var take: ((NSPasteboard) -> Bool)?
    var wantsKeyboard: (() -> Void)?

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        wantsKeyboard?()
    }

    override func paste(_ sender: Any?) {
        if take?(.general) == true { return }
        super.paste(sender)
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        Attachments.holds(sender.draggingPasteboard) ? .copy : super.draggingEntered(sender)
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        Attachments.holds(sender.draggingPasteboard) ? .copy : super.draggingUpdated(sender)
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        if Attachments.holds(sender.draggingPasteboard), take?(sender.draggingPasteboard) == true { return true }
        return super.performDragOperation(sender)
    }
}
