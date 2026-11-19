import AppKit
import Security
import SwiftUI

// Group chats between the owner and some of their agents (agentboard/chats.py, over the mailroom's agent-group).
// Every post reaches every member; the owner's own wake them all. The snapshot carries a summary for the top bar's
// Chats button; the sheet reads a chat's members and posts when it is open. What the owner has seen is kept here, per
// chat, as the time of the newest post they have had open (chatSeen).

struct ChatSummary: Decodable, Identifiable, Equatable {
    var id: String
    var title: String
    var hidden: Bool?
    var members: Int?
    var posts: Int?
    var lastPost: Double?        // ms since the epoch
    var subgroup: String?        // the board sub-group whose chat this is (subchats.py); it decides the members
}

struct ChatsInfo: Decodable, Equatable {
    var chats: [ChatSummary]
    var dropped: [String]         // why chats that do not decode were left out

    enum CodingKeys: String, CodingKey { case chats }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var why: [String] = []
        chats = c.lossyArray(ChatSummary.self, forKey: .chats, failures: &why)
        dropped = why
    }
}

struct ChatMember: Decodable, Hashable {
    var device: String
    var agent: String
    var alerts: String?
    var label: String { device == "desktop" ? agent : agent + "@" + device }
}

struct Chat: Decodable {
    var id: String
    var title: String
    var members: [ChatMember]
    var maker: ChatMember?
    var memberAlerts: [ChatMember]?
    var subgroup: String?        // as ChatSummary's
    var unnamed: [String]?       // a sub-group's agents it leaves out, having no name a chat can use (subchats.py)
}

struct ChatPost: Decodable, Identifiable, Equatable {
    var id: String
    var created: Double
    var fromAgent: String?       // nil: the owner
    var replyTo: String?
    var text: String
    var ref: String?             // the owner's: the post's ref on the board's record of what they sent
}

/// agent-group's limit on the agents in a chat; the desktop checks it too (chats.MAX_MEMBERS).
let chatMaxMembers = 16

/// When the owner last had each chat open: the newest post they saw there, in ms.
enum ChatSeen {
    static let key = "chatSeen"
    static func get() -> [String: Double] { UserDefaults.standard.dictionary(forKey: key) as? [String: Double] ?? [:] }
    static func mark(_ id: String, _ ms: Double?) {
        guard let ms = ms else { return }
        var seen = get()
        if ms > (seen[id] ?? 0) { seen[id] = ms }
        UserDefaults.standard.set(seen, forKey: key)
    }
    /// Chats with a post newer than `seen` says the owner has had open, hidden ones left out.
    static func unread(_ chats: [ChatSummary], seen: [String: Double]) -> [ChatSummary] {
        chats.filter { $0.hidden != true && ($0.lastPost ?? 0) > (seen[$0.id] ?? 0) }
    }
}

/// A new post id: 16 random bytes in base64url without padding, not starting with "-" (agent-group's rule). It is
/// the post's idempotency key, so sending the same draft twice posts it once.
func newPostID() -> String {
    while true {
        var bytes = [UInt8](repeating: 0, count: 16)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        precondition(status == errSecSuccess, "no random bytes for a post id (status \(status))")
        let id = Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
        if !id.hasPrefix("-") { return id }
    }
}

/// The top bar's way in, shown once the desktop has group chats: "Chats", or "2 new" in bold.
struct ChatsButton: View {
    @ObservedObject var model: BoardModel
    @Environment(\.zoom) private var z

    var body: some View {
        if let info = model.snapshot?.chats {
            let n = ChatSeen.unread(info.chats, seen: ChatSeen.get()).count
            Button { model.sheet = .chats(nil) } label: {
                Label(n > 0 ? "\(n) new" : "Chats", systemImage: "bubble.left.and.bubble.right")
                    .font(.system(size: 11 * z, weight: n > 0 ? .bold : .regular))
                    .foregroundColor(n > 0 ? Palette.ok : .secondary)
            }
            .buttonStyle(.borderless)
            .help("Group chats between you and your agents: \(n) with posts you haven't seen.")
        }
    }
}

struct ChatsSheet: View {
    @ObservedObject var model: BoardModel
    @Environment(\.zoom) private var z
    @State var selected: String?
    @State private var chat: Chat?
    @State private var posts: [ChatPost] = []
    @State private var draft = ""
    @State private var draftID: String?
    @State private var sending = false
    @State private var error: String?
    @State private var showHidden = false
    @State private var listed: [ChatSummary]?     // chat_list's, read when the sheet opens, on each poll and after a change
    private let poll = Timer.publish(every: 10, on: .main, in: .common).autoconnect()

    var chats: [ChatSummary] {
        let all = (listed ?? []).sorted { ($0.lastPost ?? 0) > ($1.lastPost ?? 0) }
        return showHidden ? all : all.filter { $0.hidden != true }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Group chats").font(.headline)
                Spacer()
                Toggle("Show hidden", isOn: $showHidden).toggleStyle(.checkbox)
                Button("New chat…") { model.sheet = .newChat(nil) }
            }
            if let e = error {
                Text(e).font(.callout).foregroundColor(AgentState.blocked.color).textSelection(.enabled)
            }
            HStack(alignment: .top, spacing: 12) {
                list.frame(width: 210)
                Divider()
                thread
            }
            HStack {
                Text("Your posts wake every member. An agent's post is information to the others, never an instruction.")
                    .font(.system(size: 11 * z)).foregroundStyle(.secondary)
                Spacer()
                Button("Close") { model.sheet = nil }.keyboardShortcut(.cancelAction)
            }
        }
        .padding(18)
        .frame(width: 820, height: 620)
        .onAppear { loadList(); load() }
        .onChange(of: selected) { _ in chat = nil; posts = []; draftID = nil; load() }
        .onReceive(poll) { _ in loadList(); loadPosts(since: posts.last?.id) }
    }

    var list: some View {
        let unread = Set(ChatSeen.unread(chats, seen: ChatSeen.get()).map(\.id))
        return ScrollView {
            LazyVStack(alignment: .leading, spacing: 4) {
                if listed == nil {
                    ProgressView().controlSize(.small).padding(.top, 12)
                } else if chats.isEmpty {
                    Text("No chats yet. Make one with New chat…, or from an agent's menu.")
                        .font(.system(size: 11 * z)).foregroundStyle(.secondary).padding(.top, 12)
                }
                ForEach(chats) { c in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(c.title).font(.system(size: 12.5 * z, weight: unread.contains(c.id) ? .bold : .regular))
                            .lineLimit(2)
                        Text("\(c.members ?? 0) agent\(c.members == 1 ? "" : "s") · \(c.posts ?? 0) post\(c.posts == 1 ? "" : "s")"
                             + (c.subgroup != nil ? " · sub-group" : "") + (c.hidden == true ? " · hidden" : ""))
                            .font(.system(size: 10.5 * z)).foregroundStyle(.secondary)
                    }
                    .padding(6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 5)
                        .fill(selected == c.id ? Palette.ok.opacity(0.15) : Color.clear))
                    .contentShape(Rectangle())
                    .onTapGesture { selected = c.id }
                    .contextMenu {
                        Button(c.hidden == true ? "Show on the board" : "Hide") { setHidden(c.id, c.hidden != true) }
                    }
                }
            }
        }
    }

    @ViewBuilder var thread: some View {
        if let c = chat {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline) {
                    Text(c.title).font(.title3.weight(.semibold)).lineLimit(1)
                    Spacer()
                    membersMenu(c)
                }
                Text(memberLine(c)).font(.system(size: 11 * z)).foregroundStyle(.secondary).lineLimit(2)
                ScrollViewReader { reader in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 8) {
                            ForEach(posts) { p in row(p).id(p.id) }
                        }
                    }
                    .onChange(of: posts.last?.id) { last in if let last = last { reader.scrollTo(last, anchor: .bottom) } }
                }
                TextEditor(text: $draft)
                    .font(.system(size: 13 * z))
                    .frame(height: 76)
                    .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.secondary.opacity(0.4)))
                HStack {
                    Text("⌘↩ sends to every member").font(.system(size: 11 * z)).foregroundStyle(.secondary)
                    Spacer()
                    if sending { ProgressView().controlSize(.small) }
                    Button("Send") { send(c) }
                        .keyboardShortcut(.return, modifiers: [.command])
                        .disabled(sending || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        } else if selected == nil {
            Text("Pick a chat, or make a new one.").foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    func row(_ p: ChatPost) -> some View {
        let mine = p.fromAgent == nil
        let answering = p.replyTo.flatMap { r in posts.first { $0.id == r } }
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(mine ? "You" : p.fromAgent!).fontWeight(.semibold)
                Text(SentSheet.when(p.created / 1000)).monospacedDigit()
                if let a = answering { Text("↩ " + (a.fromAgent ?? "you")).lineLimit(1) }
                Spacer()
                if let ref = p.ref { Text(ref).monospaced() }
            }
            .font(.system(size: 11 * z)).foregroundStyle(.secondary)
            Text(p.text).font(.system(size: 12.5 * z)).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 6).fill(mine ? Palette.ok.opacity(0.10) : Color.primary.opacity(0.04)))
    }

    func memberLine(_ c: Chat) -> String {
        let alerts = Dictionary((c.memberAlerts ?? []).map { ($0.label, $0.alerts ?? "all") }, uniquingKeysWith: { a, _ in a })
        let names = c.members.map { m in m.label + (alerts[m.label].map { " (\($0))" } ?? "") }
        let maker = c.maker.map { "made by " + $0.label } ?? "made by you"
        let left = (c.unnamed ?? []).isEmpty ? ""
            : " · not in it, having no name a chat can use (/rename it): " + (c.unnamed ?? []).joined(separator: ", ")
        return (names.isEmpty ? "no agents" : names.joined(separator: ", ")) + " · " + maker
            + " · in brackets: which posts wake each one" + left
    }

    /// Add or remove members: named agents on the board that aren't in it, and its members. A sub-group's chat has
    /// the sub-group's agents, so it says so instead.
    @ViewBuilder func membersMenu(_ c: Chat) -> some View {
        if let sub = c.subgroup {
            Text("Members follow the sub-group “\(sub)”")
                .font(.system(size: 11 * z)).foregroundStyle(.secondary)
                .help("Move tiles into or out of the sub-group on the board to change who is in this chat.")
        } else {
            editableMembersMenu(c)
        }
    }

    func editableMembersMenu(_ c: Chat) -> some View {
        let inChat = Set(c.members.map(\.label))
        let addable = (model.snapshot?.agents ?? []).filter { !$0.name.isEmpty && !inChat.contains($0.name) }
        return Menu("Members") {
            Section("Add") {
                ForEach(addable) { a in Button(a.display) { setMember(c, a, "add") } }
            }
            Section("Remove") {
                ForEach(c.members, id: \.self) { m in
                    if let a = model.snapshot?.agents.first(where: { $0.name == m.agent && m.device == "desktop" }) {
                        Button(m.label) { setMember(c, a, "remove") }
                    } else {
                        Text(m.label + " (not on the board)")
                    }
                }
            }
        }
        .fixedSize()
    }

    func loadList() {
        Actions.fetch(["action": "chat_list"], key: "chats", as: [ChatSummary].self) { got, why in
            guard let got = got else { error = why; return }
            listed = got
            if selected == nil { selected = chats.first?.id }
        }
    }

    func load() {
        guard let id = selected else { return }
        Actions.fetch(["action": "chat_show", "group": id], key: "chat", as: Chat.self) { c, why in
            guard selected == id else { return }
            if let c = c { chat = c; error = nil } else { error = why }
        }
        loadPosts(since: nil)
    }

    func loadPosts(since: String?) {
        guard let id = selected else { return }
        var req: [String: Any] = ["action": "chat_posts", "group": id]
        if let s = since { req["since"] = s }
        Actions.fetch(req, key: "posts", as: [ChatPost].self) { got, why in
            guard selected == id else { return }
            guard let got = got else { error = why; return }
            if since == nil { posts = got } else { posts += got.filter { p in !posts.contains { $0.id == p.id } } }
            ChatSeen.mark(id, posts.last?.created)
        }
    }

    func send(_ c: Chat) {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let id = draftID ?? newPostID()
        draftID = id                    // kept until the post is in, so sending again cannot post it twice
        sending = true
        Actions.run(["action": "chat_post", "group": c.id, "post": id, "title": c.title, "text": text]) { res in
            sending = false
            guard res.ok else { error = "Not posted: \(res.message)"; return }
            if draft.trimmingCharacters(in: .whitespacesAndNewlines) == text { draft = "" }
            draftID = nil
            error = nil
            loadPosts(since: posts.last?.id)
        }
    }

    func setMember(_ c: Chat, _ a: Agent, _ op: String) {
        Actions.run(["action": "chat_members", "group": c.id, "op": op, "members": [a.id]]) { res in
            if !res.ok { error = "Couldn't \(op) \(a.display): \(res.message)" }
            load()
            loadList()
        }
    }

    func setHidden(_ id: String, _ hidden: Bool) {
        Actions.run(["action": "chat_hide", "group": id, "hidden": hidden]) { res in
            error = res.ok ? nil : "Couldn't \(hidden ? "hide" : "show") it: \(res.message)"
            if res.ok { model.show(hidden ? "Hidden. Show hidden lists it again." : "Shown again.") }
            loadList()
        }
    }
}

/// A new chat: its title and which agents are in it. Only a named session can be a member.
struct NewChatSheet: View {
    @ObservedObject var model: BoardModel
    let preselected: String?
    @Environment(\.zoom) private var z
    @State private var title = ""
    @State private var picked: Set<String> = []
    @State private var working = false
    @State private var error: String?

    var agents: [Agent] { (model.snapshot?.agents ?? []).filter(\.live).sorted { $0.display < $1.display } }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("New group chat").font(.headline)
            TextField("Title, e.g. Launch plan", text: $title).textFieldStyle(.roundedBorder)
            Text("Agents in it (up to \(chatMaxMembers)). Each is woken only by posts that name it or answer it, and by every post of yours.")
                .font(.system(size: 11 * z)).foregroundStyle(.secondary)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 3) {
                    ForEach(agents) { a in
                        Toggle(isOn: Binding(get: { picked.contains(a.id) },
                                             set: { on in if on { picked.insert(a.id) } else { picked.remove(a.id) } })) {
                            Text(a.name.isEmpty ? "\(a.display) (no name: /rename it first)" : a.display)
                        }
                        .toggleStyle(.checkbox)
                        .disabled(a.name.isEmpty)
                    }
                }
            }
            if let e = error {
                Text(e).font(.callout).foregroundColor(AgentState.blocked.color).textSelection(.enabled)
            }
            HStack {
                if working { ProgressView().controlSize(.small) }
                Spacer()
                Button("Cancel") { model.sheet = nil }.keyboardShortcut(.cancelAction)
                Button("Create") { create() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(working || picked.isEmpty || title.trimmingCharacters(in: .whitespaces).isEmpty
                              || picked.count > chatMaxMembers)
            }
        }
        .padding(18)
        .frame(width: 460, height: 520)
        .onAppear { if let p = preselected, agents.contains(where: { $0.id == p && !$0.name.isEmpty }) { picked = [p] } }
    }

    func create() {
        let t = title.trimmingCharacters(in: .whitespaces)
        working = true
        Actions.fetch(["action": "chat_new", "title": t, "members": Array(picked)], key: "chat", as: Chat.self) { chat, why in
            working = false
            guard let chat = chat else { error = "Not made: " + (why ?? "no answer"); return }
            model.sheet = .chats(chat.id)
        }
    }
}

/// --selftest-chats: post ids agent-group accepts, and which chats count as new, offline.
enum ChatTest {
    static func run() -> Int32 {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append(what) } }
        // chats.ID_RE on the desktop: 22 base64url characters, the first not "-"
        let pattern = try! NSRegularExpression(pattern: "^[A-Za-z0-9_][A-Za-z0-9_-]{21}$")
        let ids = (0..<2000).map { _ in newPostID() }
        let bad = ids.filter { pattern.firstMatch(in: $0, range: NSRange($0.startIndex..., in: $0)) == nil }
        check(bad.isEmpty, "post ids the desktop refuses: \(bad.prefix(3))")
        check(Set(ids).count == ids.count, "a post id came up twice in \(ids.count)")

        let chats = [ChatSummary(id: "a", title: "seen", hidden: false, members: 1, posts: 2, lastPost: 100),
                     ChatSummary(id: "b", title: "newer", hidden: false, members: 1, posts: 3, lastPost: 300),
                     ChatSummary(id: "c", title: "never opened", hidden: nil, members: 1, posts: 1, lastPost: 50),
                     ChatSummary(id: "d", title: "hidden", hidden: true, members: 1, posts: 1, lastPost: 900),
                     ChatSummary(id: "e", title: "no posts", hidden: false, members: 1, posts: 0, lastPost: nil)]
        let unread = ChatSeen.unread(chats, seen: ["a": 100, "b": 200, "d": 0]).map(\.id)
        check(unread == ["b", "c"], "new: a post since you had it open, or never opened; not hidden or empty: \(unread)")

        // chat_show's answer for a sub-group's chat (actions.chat_show_action), and for another chat
        let shown: [String: Any] = ["ok": true, "chat": ["id": "a", "title": "Alpha", "members": [], "maker": NSNull(),
                                                         "member_alerts": [], "subgroup": "Alpha", "unnamed": ["Beta docs"]]]
        let (sub, why) = Actions.decodeAnswer(shown, nil, key: "chat", as: Chat.self)
        check(sub?.subgroup == "Alpha", "a sub-group's chat says which: \(why ?? String(describing: sub?.subgroup))")
        check(sub?.unnamed == ["Beta docs"], "and which of its agents it leaves out for want of a name")
        let plain: [String: Any] = ["ok": true, "chat": ["id": "b", "title": "x", "members": [], "subgroup": NSNull()]]
        let (other, _) = Actions.decodeAnswer(plain, nil, key: "chat", as: Chat.self)
        check(other != nil && other?.subgroup == nil, "another chat's members are yours to change")

        if fails.isEmpty { print("chats selftest: ok"); return 0 }
        fails.forEach { print("FAIL: " + $0) }
        return 1
    }
}
