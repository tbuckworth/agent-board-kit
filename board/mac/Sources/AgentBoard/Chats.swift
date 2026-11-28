import AppKit
import Security
import SwiftUI

// Group chats between the owner and some of their agents (agentboard/chats.py, over the mailroom's agent-group).
// Every post reaches every member; the owner's own wake them all. The snapshot carries a summary for the top bar's
// Chats button; the sheet reads a chat's members and posts when it is open. What the owner has seen is kept here, per
// chat, as the time of the newest post they have had open (chatSeen).
//
// A chat's posts can be in topics: the sheet shows All and one row per topic, and the owner's post goes in the topic
// picked, or opens a new one; a reply stays in the topic of the post it answers.

struct ChatTopic: Decodable, Identifiable, Hashable {
    var id: String
    var title: String
    var posts: Int?              // in a chat's list of topics: how many posts are in it
    var lastPost: Double?        // and when the newest was, ms since the epoch
}

struct ChatSummary: Decodable, Identifiable, Equatable {
    var id: String
    var title: String
    var hidden: Bool?
    var members: Int?
    var posts: Int?
    var lastPost: Double?        // ms since the epoch
    var subgroup: String?        // the board sub-group whose chat this is (subchats.py); it decides the members
    var topics: [ChatTopic]?     // agent-group's list of the chat's topics, the one the sheet shows; nil: none listed
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

/// A member's own alert setting for one topic, where that is the one in force (chats.show).
struct ChatTopicAlert: Decodable, Hashable {
    var topic: String            // the topic's id
    var alerts: String
}

struct ChatMember: Decodable, Hashable {
    var device: String
    var agent: String
    var alerts: String?
    var topicAlerts: [ChatTopicAlert]?
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
    var fromOwner: Bool          // the owner's own
    var senderLabel: String      // agent-group's name for the sender, shown as it is
    var replyTo: String?
    var topic: ChatTopic?        // {id, title}, or nil: in no topic
    var text: String
    var ref: String?             // the owner's: the post's ref on the board's record of what they sent

    /// Who sent it, as the sheet names them: "You", or agent-group's label as it is.
    var who: String { fromOwner ? "You" : senderLabel }
}

extension ChatPost {
    enum CodingKeys: String, CodingKey { case id, created, fromOwner, senderLabel, fromAgent, replyTo, topic, text, ref }

    /// Whose it is as agent-group says it (from_owner, sender_label) when the desktop gives that; else from_agent,
    /// null for the owner's own. (In an extension, so the memberwise initialiser stays.)
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        created = try c.decode(Double.self, forKey: .created)
        let agent = try c.decodeIfPresent(String.self, forKey: .fromAgent)
        fromOwner = try c.decodeIfPresent(Bool.self, forKey: .fromOwner) ?? (agent == nil)
        senderLabel = try c.decodeIfPresent(String.self, forKey: .senderLabel) ?? agent ?? ""
        replyTo = try c.decodeIfPresent(String.self, forKey: .replyTo)
        topic = try c.decodeIfPresent(ChatTopic.self, forKey: .topic)
        text = try c.decode(String.self, forKey: .text)
        ref = try c.decodeIfPresent(String.self, forKey: .ref)
    }
}

/// Which posts the open chat shows, and where the owner's next post goes: all of them (the post then goes in no
/// topic), one topic's, or none yet, for a new topic the post opens.
enum TopicPick: Hashable {
    case all
    case topic(String)
    case new
}

enum ChatTopics {
    /// agent-group's topics for a chat (each titled by its newest post), the most recently active first.
    static func newestFirst(_ topics: [ChatTopic]?) -> [ChatTopic] {
        (topics ?? []).sorted { ($0.lastPost ?? 0) > ($1.lastPost ?? 0) }
    }

    static func shown(_ posts: [ChatPost], _ pick: TopicPick) -> [ChatPost] {
        switch pick {
        case .all: return posts
        case .topic(let id): return posts.filter { $0.topic?.id == id }
        case .new: return []
        }
    }

    /// chat_post's fields for where the owner's post goes, or nil when it cannot go yet (a new topic with no title). A
    /// reply goes by the post it answers alone, since it stays in that post's topic.
    static func place(_ pick: TopicPick, newTitle: String, replyTo: ChatPost?) -> [String: String]? {
        if let r = replyTo { return ["reply_to": r.id] }
        switch pick {
        case .all: return [:]
        case .topic(let id): return ["topic": id]
        case .new:
            let t = newTitle.trimmingCharacters(in: .whitespacesAndNewlines)
            return t.isEmpty ? nil : ["new_topic": t]
        }
    }

    /// Where the post goes, in words, as place sends it: a reply into its post's topic, else the topic picked, named
    /// by the first of `topics` with its id, or by its id when none has it.
    static func destination(_ pick: TopicPick, newTitle: String, replyTo: ChatPost?, topics: [ChatTopic]) -> String {
        func named(_ t: ChatTopic) -> String { "in topic “\(t.title)”" }
        if let r = replyTo { return "as a reply " + (r.topic.map(named) ?? "in no topic") }
        switch pick {
        case .all: return "in no topic"
        case .topic(let id): return topics.first { $0.id == id }.map(named) ?? "in topic \(id)"
        case .new:
            let t = newTitle.trimmingCharacters(in: .whitespacesAndNewlines)
            return t.isEmpty ? "opening a new topic (give it a title)" : "opening topic “\(t)”"
        }
    }
}

/// What the sheet says about a chat: its members, and why a new one could not be opened.
enum ChatText {
    /// What the New chat sheet says when chat_new's answer (`answer`, and `why` from Actions.decodeAnswer) gives no
    /// chat it can open. A chat agent-group made is never said to be not made: the desktop's own words when it says
    /// so ("made", chats.new), and "Made" when it answered with a chat this app cannot read; else "Not made:" and why.
    static func newChatProblem(_ answer: [String: Any]?, _ why: String?) -> String {
        let why = why ?? "no answer"
        let again = ". Do not make it again: it is in the list of chats."
        if answer?.keys.contains("made") == true { return why + again }
        if (answer?["ok"] as? Bool) == true {
            let id = (answer?["chat"] as? [String: Any])?["id"] as? String
            return "Made" + (id.map { " chat " + $0 } ?? "") + ", but " + why + again
        }
        return "Not made: " + why
    }

    /// The line under a chat's title: its agents, each with the posts that wake it (and its own setting for a topic,
    /// titled from `topics`), then who made it, and a sub-group's agents it leaves out.
    static func memberLine(_ c: Chat, topics: [ChatTopic]) -> String {
        let alerts = Dictionary((c.memberAlerts ?? []).map { ($0.label, $0) }, uniquingKeysWith: { a, _ in a })
        let titles = Dictionary(topics.map { ($0.id, $0.title) }, uniquingKeysWith: { a, _ in a })
        let names = c.members.map { m in m.label + (alerts[m.label].map { " (" + alertText($0, titles) + ")" } ?? "") }
        var parts = [names.isEmpty ? "no agents" : names.joined(separator: ", ")]
        // made by an agent, or by the owner ("you")
        parts.append(c.maker.map { "made by " + $0.label } ?? "made by you")
        parts.append("in brackets: which posts wake each one")
        if let left = c.unnamed, !left.isEmpty {
            parts.append("not in it, having no name a chat can use (/rename it): " + left.joined(separator: ", "))
        }
        return parts.joined(separator: " · ")
    }

    /// A member's alert setting for the whole chat, then each topic's own where that is in force: "named; Release: all".
    static func alertText(_ m: ChatMember, _ titles: [String: String]) -> String {
        let perTopic = (m.topicAlerts ?? []).map { (titles[$0.topic] ?? $0.topic) + ": " + $0.alerts }.sorted()
        return ([m.alerts ?? "all"] + perTopic).joined(separator: "; ")
    }
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

/// A new post id: 16 random bytes in base64url without padding, not starting with "-" (agent-group's rule for the
/// ids it makes, the desktop's chats.NEW_ID_RE). It is the post's idempotency key, so sending the same draft twice
/// posts it once.
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

/// What --render-chats draws the sheet from: chat_list's chats, one chat as chat_show gives it and its posts as
/// chat_posts gives them, and the topic picked (its id; none: All).
struct ChatsPreload: Decodable {
    var chats: [ChatSummary]
    var chat: Chat
    var posts: [ChatPost]
    var topic: String?
}

struct ChatsSheet: View {
    @ObservedObject var model: BoardModel
    @Environment(\.zoom) private var z
    let preloaded: ChatsPreload?                  // offscreen renders: no network
    @State var selected: String?
    @State private var chat: Chat?
    @State private var posts: [ChatPost] = []
    @State private var draft = ""
    @State private var draftID: String?
    @State private var sending = false
    @State private var error: String?
    @State private var showHidden = false
    @State private var listed: [ChatSummary]?     // chat_list's, read when the sheet opens, on each poll and after a change
    @State private var pick: TopicPick = .all
    @State private var newTopic = ""              // the title of the topic the owner's next post opens
    @State private var replyTo: ChatPost?
    @State private var opening: String?           // the owner's post that opened a topic, picked once it is in
    private let poll = Timer.publish(every: 10, on: .main, in: .common).autoconnect()

    init(model: BoardModel, selected: String?, preloaded: ChatsPreload? = nil) {
        self.model = model
        self.preloaded = preloaded
        _selected = State(initialValue: preloaded?.chat.id ?? selected)
        _listed = State(initialValue: preloaded?.chats)
        _chat = State(initialValue: preloaded?.chat)
        _posts = State(initialValue: preloaded?.posts ?? [])
        _pick = State(initialValue: preloaded?.topic.map { .topic($0) } ?? .all)
    }

    var chats: [ChatSummary] {
        let all = (listed ?? []).sorted { ($0.lastPost ?? 0) > ($1.lastPost ?? 0) }
        return showHidden ? all : all.filter { $0.hidden != true }
    }

    /// The open chat's topics, as agent-group lists them with the chats (read on each poll, and after a post opens one).
    var topics: [ChatTopic] { ChatTopics.newestFirst(listed?.first { $0.id == selected }?.topics) }

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
        .frame(width: 1000, height: 660)
        .onAppear { if preloaded == nil { loadList(); load() } }
        .onChange(of: selected) { _ in
            chat = nil; posts = []; draftID = nil; pick = .all; newTopic = ""; replyTo = nil; opening = nil
            load()
        }
        .onReceive(poll) { _ in
            guard preloaded == nil else { return }
            loadList()
            loadPosts(since: posts.last?.id)
        }
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
                        Text(listLine(c)).font(.system(size: 10.5 * z)).foregroundStyle(.secondary)
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

    func listLine(_ c: ChatSummary) -> String {
        let topicCount = c.topics?.count ?? 0
        return "\(c.members ?? 0) agent\(c.members == 1 ? "" : "s") · \(c.posts ?? 0) post\(c.posts == 1 ? "" : "s")"
            + (topicCount > 0 ? " · \(topicCount) topic\(topicCount == 1 ? "" : "s")" : "")
            + (c.subgroup != nil ? " · sub-group" : "") + (c.hidden == true ? " · hidden" : "")
    }

    @ViewBuilder var thread: some View {
        if let c = chat {
            VStack(alignment: .leading, spacing: 8) {
                header(c)
                Text(ChatText.memberLine(c, topics: topics)).font(.system(size: 11 * z)).foregroundStyle(.secondary)
                    .lineLimit(3)
                HStack(alignment: .top, spacing: 10) {
                    topicList.frame(width: 170)
                    Divider()
                    VStack(alignment: .leading, spacing: 8) {
                        postList
                        composer(c)
                    }
                }
            }
        } else if selected == nil {
            Text("Pick a chat, or make a new one.").foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    func header(_ c: Chat) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(c.title).font(.title3.weight(.semibold)).lineLimit(1)
            Spacer()
            membersMenu(c)
        }
    }

    /// All, then one row per topic (the most recently active first), then New topic….
    var topicList: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 2) {
                topicRow("All", count: posts.count, pick: .all, icon: "tray.full")
                ForEach(topics) { t in topicRow(t.title, count: t.posts, pick: .topic(t.id), icon: "number") }
                topicRow("New topic…", count: nil, pick: .new, icon: "plus")
            }
        }
    }

    func topicRow(_ title: String, count: Int?, pick p: TopicPick, icon: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 5) {
            Image(systemName: icon).foregroundStyle(.secondary).frame(width: 14)
            Text(title).lineLimit(2)
            Spacer(minLength: 2)
            if let n = count { Text("\(n)").monospacedDigit().foregroundStyle(.secondary) }
        }
        .font(.system(size: 12 * z, weight: pick == p ? .semibold : .regular))
        .padding(5)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 5).fill(pick == p ? Palette.ok.opacity(0.15) : Color.clear))
        .contentShape(Rectangle())
        .onTapGesture {
            pick = p
            if p == .new { replyTo = nil }        // the sheet keeps a reply in its post's topic: a new one answers none
        }
    }

    @ViewBuilder var postList: some View {
        let shown = ChatTopics.shown(posts, pick)
        ScrollViewReader { reader in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    if pick == .new {
                        Text("Your post opens the topic, under the title you give it below.")
                            .font(.system(size: 11.5 * z)).foregroundStyle(.secondary).padding(.top, 8)
                    }
                    ForEach(shown) { p in row(p, showTopic: pick == .all).id(p.id) }
                }
            }
            .onChange(of: shown.last?.id) { last in if let last = last { reader.scrollTo(last, anchor: .bottom) } }
        }
    }

    func row(_ p: ChatPost, showTopic: Bool) -> some View {
        let answering = p.replyTo.flatMap { r in posts.first { $0.id == r } }
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(p.who).fontWeight(.semibold).lineLimit(1)
                Text(SentSheet.when(p.created / 1000)).monospacedDigit()
                if showTopic, let t = p.topic { Text("# " + t.title).lineLimit(1) }
                if let a = answering { Text("↩ " + a.who).lineLimit(1) }
                Spacer()
                if let ref = p.ref { Text(ref).monospaced() }
                Button { startReply(p) } label: { Image(systemName: "arrowshape.turn.up.left") }
                    .buttonStyle(.borderless)
                    .help("Reply to this post; the reply stays in its topic.")
            }
            .font(.system(size: 11 * z)).foregroundStyle(.secondary)
            Text(p.text).font(.system(size: 12.5 * z)).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 6).fill(p.fromOwner ? Palette.ok.opacity(0.10) : Color.primary.opacity(0.04)))
    }

    func composer(_ c: Chat) -> some View {
        let place = ChatTopics.place(pick, newTitle: newTopic, replyTo: replyTo)
        return VStack(alignment: .leading, spacing: 6) {
            if let r = replyTo {
                HStack(spacing: 6) {
                    Text("↩ Replying to \(r.who): " + (r.text.split(separator: "\n").first.map(String.init) ?? ""))
                        .lineLimit(1)
                    Spacer()
                    Button("Cancel reply") { replyTo = nil }.buttonStyle(.borderless)
                }
                .font(.system(size: 11 * z)).foregroundStyle(.secondary)
            } else if pick == .new {
                TextField("The new topic's title", text: $newTopic).textFieldStyle(.roundedBorder)
            }
            TextEditor(text: $draft)
                .font(.system(size: 13 * z))
                .frame(height: 76)
                .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.secondary.opacity(0.4)))
            HStack {
                Text("⌘↩ sends to every member, "
                     // a topic the owner's post just opened is named by that post until the chats are read again
                     + ChatTopics.destination(pick, newTitle: newTopic, replyTo: replyTo,
                                              topics: topics + posts.compactMap(\.topic)))
                    .font(.system(size: 11 * z)).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                if sending { ProgressView().controlSize(.small) }
                Button("Send") { send(c) }
                    .keyboardShortcut(.return, modifiers: [.command])
                    .disabled(sending || place == nil || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
    }

    func startReply(_ p: ChatPost) {
        replyTo = p
        if pick == .new { pick = p.topic.map { .topic($0.id) } ?? .all }
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
            if let o = opening, let p = posts.first(where: { $0.id == o }) {
                opening = nil
                if let t = p.topic, pick == .new { pick = .topic(t.id) }   // the topic the owner's post opened
            }
            ChatSeen.mark(id, posts.last?.created)
        }
    }

    func send(_ c: Chat) {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let place = ChatTopics.place(pick, newTitle: newTopic, replyTo: replyTo) else { return }
        let id = draftID ?? newPostID()
        draftID = id                    // kept until the post is in, so sending again cannot post it twice
        sending = true
        var req: [String: Any] = ["action": "chat_post", "group": c.id, "post": id, "title": c.title, "text": text]
        for (k, v) in place { req[k] = v }
        Actions.run(req) { res in
            sending = false
            guard res.ok else { error = "Not posted: \(res.message)"; return }
            if draft.trimmingCharacters(in: .whitespacesAndNewlines) == text { draft = "" }
            draftID = nil
            error = nil
            replyTo = nil
            if place["new_topic"] != nil { newTopic = ""; opening = id }
            loadPosts(since: posts.last?.id)
            loadList()                      // the chat's topics and counts, as agent-group lists them
        }
    }

    /// One change to a chat on the desktop (chat_members, chat_hide): a failure shown as
    /// `failed` and the desktop's reason, `done` as a toast once it is made, then the chats read again, and the open
    /// chat too unless `listOnly`.
    func change(_ req: [String: Any], failed: String, done: String? = nil, listOnly: Bool = false) {
        Actions.run(req) { res in
            error = res.ok ? nil : "\(failed): \(res.message)"
            if res.ok, let d = done { model.show(d) }
            if !listOnly { load() }
            loadList()
        }
    }

    func setMember(_ c: Chat, _ a: Agent, _ op: String) {
        change(["action": "chat_members", "group": c.id, "op": op, "members": [a.id]],
               failed: "Couldn't \(op) \(a.display)")
    }

    func setHidden(_ id: String, _ hidden: Bool) {
        change(["action": "chat_hide", "group": id, "hidden": hidden], failed: "Couldn't \(hidden ? "hide" : "show") it",
               done: hidden ? "Hidden. Show hidden lists it again." : "Shown again.", listOnly: true)
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
        Actions.runJSON(["action": "chat_new", "title": t, "members": Array(picked)]) { answer, err in
            working = false
            let (chat, why) = Actions.decodeAnswer(answer, err, key: "chat", as: Chat.self)
            guard let chat = chat else { error = ChatText.newChatProblem(answer, why); return }
            model.sheet = .chats(chat.id)
        }
    }
}

/// --selftest-chats: post ids agent-group accepts, which chats count as new, who sent each post and which topic it is
/// in, where a post goes, and the member line, offline.
enum ChatTest {
    static func run() -> Int32 {
        var fails: [String] = []
        func check(_ ok: Bool, _ what: String) { if !ok { fails.append(what) } }
        // chats.NEW_ID_RE on the desktop: 22 base64url characters, the first not "-"
        let pattern = try! NSRegularExpression(pattern: "^[A-Za-z0-9_][A-Za-z0-9_-]{21}$")
        let ids = (0..<2000).map { _ in newPostID() }
        let bad = ids.filter { pattern.firstMatch(in: $0, range: NSRange($0.startIndex..., in: $0)) == nil }
        check(bad.isEmpty, "post ids the desktop refuses: \(bad.prefix(3))")
        check(Set(ids).count == ids.count, "a post id came up twice in \(ids.count)")

        func summary(_ id: String, _ hidden: Bool?, _ posts: Int, _ last: Double?) -> ChatSummary {
            ChatSummary(id: id, title: id, hidden: hidden, members: 1, posts: posts, lastPost: last, topics: [])
        }
        let chats = [summary("a", false, 2, 100), summary("b", false, 3, 300), summary("c", nil, 1, 50),
                     summary("d", true, 1, 900), summary("e", false, 0, nil)]
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

        // chat_new's answer, as chats.new gives it from agent-group's new --json: the sheet opens the chat made only
        // when it decodes, so a chat made is never said to be not made
        func made(_ topicAlerts: Any) -> [String: Any] {
            let alpha: [String: Any] = ["device": "desktop", "agent": "alpha"]
            return ["ok": true, "chat": [
                "id": "q8F3Ab_-q8F3Ab_-q8F3Ab", "title": "-Launch", "maker": NSNull(), "task": NSNull(),
                "hidden": false, "created": 1, "updated": 1, "posts": 0, "last_post": NSNull(), "topics": [],
                "members": [alpha],
                "member_alerts": [alpha.merging(["alerts": "named", "topic_alerts": topicAlerts]) { a, _ in a }]]]
        }
        let (fresh, freshWhy) = Actions.decodeAnswer(made([]), nil, key: "chat", as: Chat.self)
        check(fresh?.id == "q8F3Ab_-q8F3Ab_-q8F3Ab" && fresh?.memberAlerts?.first?.topicAlerts == [],
              "chat_new's answer decodes, so the sheet opens the chat made: \(freshWhy ?? "")")
        let (raw, rawWhy) = Actions.decodeAnswer(made([String: Any]()), nil, key: "chat", as: Chat.self)
        check(raw == nil, "agent-group's own {topic id: setting} is not read: the desktop gives the list (chats.new)")
        // a chat made is never said to be not made: not when the desktop says agent-group's answer did not check out,
        // nor when this app cannot read the chat it was given
        let words = "agent-group made chat q8F3Ab_-q8F3Ab_-q8F3Ab, but its answer is not understood: no member_alerts"
        let said = [ChatText.newChatProblem(["ok": false, "error": words, "made": "q8F3Ab_-q8F3Ab_-q8F3Ab"], words),
                    ChatText.newChatProblem(["ok": false, "error": words, "made": NSNull()], words),
                    ChatText.newChatProblem(made([String: Any]()), rawWhy),
                    ChatText.newChatProblem(["ok": false, "error": "the chat needs a title"], "the chat needs a title"),
                    ChatText.newChatProblem(nil, "no answer from the desktop")]
        let again = ". Do not make it again: it is in the list of chats."
        check(said[0] == words + again && said[1] == words + again,
              "made but not understood on the desktop: its words, with the chat's id when known: \(said[0])")
        check(said[2].hasPrefix("Made chat q8F3Ab_-q8F3Ab_-q8F3Ab, but not understood: ") && said[2].hasSuffix(again),
              "made but not understood here: \(said[2])")
        check(said[3] == "Not made: the chat needs a title" && said[4] == "Not made: no answer from the desktop",
              "refused, or no answer: not made: \(said[3...])")

        topicsAndMembers(check)
        sendersAndTopics(check)

        if fails.isEmpty { print("chats selftest: ok"); return 0 }
        fails.forEach { print("FAIL: " + $0) }
        return 1
    }

    /// chat_list's and chat_show's answers with topics and per-topic alert settings, as chats.rows and chats.show give
    /// them from agent-group's JSON; the member line.
    static func topicsAndMembers(_ check: (Bool, String) -> Void) {
        let alice: [String: Any] = ["device": "desktop", "agent": "alice"]
        let bob: [String: Any] = ["device": "desktop", "agent": "bob"]
        let carol: [String: Any] = ["device": "mac", "agent": "carol"]
        func topic(_ id: String, _ title: String, _ n: Int, _ last: Double) -> [String: Any] {
            ["id": id, "title": title, "posts": n, "last_post": last]
        }
        let listed: [String: Any] = ["ok": true, "chats": [
            ["id": "s", "title": "Release", "hidden": false, "members": 2, "posts": 5, "last_post": 5,
             "subgroup": NSNull(), "unnamed": [],
             "topics": [topic("q8F3Ab_-q8F3Ab_-q8F3Ab", "Release v2", 3, 2), topic("DataDataDataDataDataDA", "Data", 1, 4)]],
            ["id": "o", "title": "older desktop", "hidden": false, "members": 1, "posts": 0, "last_post": NSNull()]]]
        let (rows, rowsWhy) = Actions.decodeAnswer(listed, nil, key: "chats", as: [ChatSummary].self)
        check(rows?.count == 2, "chat_list with topics decodes, and a chat with no topics listed: \(rowsWhy ?? "")")
        let topics = ChatTopics.newestFirst(rows?.first?.topics)
        check(topics.map(\.title) == ["Data", "Release v2"] && topics.map { $0.posts ?? 0 } == [1, 3],
              "a chat's topics as agent-group lists them, the most recently active first: \(topics.map(\.title))")
        check(rows?.last?.topics == nil && ChatTopics.newestFirst(rows?.last?.topics).isEmpty, "no topics listed: none")

        let show: [String: Any] = ["ok": true, "chat": [
            "id": "s", "title": "Release", "maker": alice, "subgroup": NSNull(), "unnamed": [],
            "members": [alice, bob, carol],
            "member_alerts": [alice.merging(["alerts": "named",
                                             "topic_alerts": [["topic": "q8F3Ab_-q8F3Ab_-q8F3Ab", "alerts": "all"]]]) { a, _ in a },
                              bob.merging(["alerts": "all", "topic_alerts": []]) { a, _ in a }]]]
        let (chat, chatWhy) = Actions.decodeAnswer(show, nil, key: "chat", as: Chat.self)
        check(chat != nil, "chat_show with per-topic settings decodes: \(chatWhy ?? "")")
        guard let c = chat else { return }
        check(c.memberAlerts?.first?.topicAlerts == [ChatTopicAlert(topic: "q8F3Ab_-q8F3Ab_-q8F3Ab", alerts: "all")],
              "a member's per-topic setting keeps its topic id as it is")
        let line = ChatText.memberLine(c, topics: topics)
        check(line == "alice (named; Release v2: all), bob (all), carol@mac · made by alice · in brackets: which posts wake each one",
              "the member line: its agents with their settings, who made it: \(line)")
        var mine = c
        mine.maker = nil
        check(ChatText.memberLine(mine, topics: []).contains(" · made by you · "), "one the owner made: made by you")
    }

    /// chat_posts' answer, as chats.posts gives it from agent-group's JSON: who sent each post, its topic, and where
    /// the owner's next post goes.
    static func sendersAndTopics(_ check: (Bool, String) -> Void) {
        let release: [String: Any] = ["id": "8fLuiBEUyaigdFaTgeVN9A", "title": "Release"]
        func post(_ id: String, _ created: Double, from: String, agent: Any = NSNull(), owner: Bool, label: String,
                  reply: Any = NSNull(), topic: Any = NSNull()) -> [String: Any] {
            ["id": id, "created": created, "from": from, "from_agent": agent, "from_owner": owner, "sender_label": label,
             "reply_to": reply, "topic": topic, "text": "post " + id]
        }
        let answer: [String: Any] = ["ok": true, "posts": [
            post("p1", 1, from: "desktop", owner: true, label: "Alex", topic: release),
            post("p2", 2, from: "desktop", agent: "alice", owner: false, label: "alice@desktop",
                 reply: "p1", topic: release),
            post("p3", 3, from: "desktop", agent: "bob", owner: false, label: "bob@desktop"),
            post("p4", 4, from: "desktop", agent: "carol", owner: false, label: "carol@desktop",
                 topic: ["id": "DataDataDataDataDataDA", "title": "Data"]),
            post("p5", 5, from: "mac", agent: "mac-agent", owner: false, label: "mac-agent@mac",
                 topic: ["id": "8fLuiBEUyaigdFaTgeVN9A", "title": "Release v2"])]]
        let (got, why) = Actions.decodeAnswer(answer, nil, key: "posts", as: [ChatPost].self)
        guard let posts = got else { check(false, "chat_posts with topics decodes: \(why ?? "")"); return }
        check(posts.map(\.who) == ["You", "alice@desktop", "bob@desktop", "carol@desktop", "mac-agent@mac"],
              "each post names its sender, agent-group's label as it is: \(posts.map(\.who))")
        check(posts.map(\.fromOwner) == [true, false, false, false, false], "only the owner's own post is theirs")

        // a desktop that gives only from_agent (null: the owner's own) still names each sender
        let older: [String: Any] = ["ok": true, "posts": [
            ["id": "o1", "created": 1, "from_agent": NSNull(), "reply_to": NSNull(), "text": "mine"],
            ["id": "o2", "created": 2, "from_agent": "alice", "reply_to": "o1", "text": "hers"]]]
        let (old, oldWhy) = Actions.decodeAnswer(older, nil, key: "posts", as: [ChatPost].self)
        check(old?.map(\.who) == ["You", "alice"] && old?.map(\.fromOwner) == [true, false] && old?.first?.topic == nil,
              "posts with from_agent alone: \(oldWhy ?? String(describing: old?.map(\.who)))")

        check(ChatTopics.shown(posts, .topic("8fLuiBEUyaigdFaTgeVN9A")).map(\.id) == ["p1", "p2", "p5"],
              "a topic shows its own posts")
        check(ChatTopics.shown(posts, .all).count == 5 && ChatTopics.shown(posts, .new).isEmpty,
              "All shows every post, a new topic none yet")

        let p2 = posts[1], p3 = posts[2]
        check(ChatTopics.place(.all, newTitle: "", replyTo: nil) == [:], "under All, a post goes in no topic")
        check(ChatTopics.place(.topic("T"), newTitle: "", replyTo: nil) == ["topic": "T"], "in the topic picked")
        check(ChatTopics.place(.new, newTitle: "  Launch  ", replyTo: nil) == ["new_topic": "Launch"],
              "or it opens a new topic with its title")
        check(ChatTopics.place(.new, newTitle: "   ", replyTo: nil) == nil, "not a new topic without a title")
        check(ChatTopics.place(.topic("T"), newTitle: "", replyTo: p3) == ["reply_to": "p3"],
              "a reply goes by the post it answers alone, so it stays in that post's topic")

        let topics = [ChatTopic(id: "8fLuiBEUyaigdFaTgeVN9A", title: "Release v2", posts: 3, lastPost: 5)]
        let said = [ChatTopics.destination(.all, newTitle: "", replyTo: nil, topics: topics),
                    ChatTopics.destination(.topic("8fLuiBEUyaigdFaTgeVN9A"), newTitle: "", replyTo: nil, topics: topics),
                    ChatTopics.destination(.new, newTitle: " ", replyTo: nil, topics: topics),
                    ChatTopics.destination(.new, newTitle: "Launch", replyTo: nil, topics: topics),
                    ChatTopics.destination(.all, newTitle: "", replyTo: p2, topics: topics),
                    ChatTopics.destination(.topic("8fLuiBEUyaigdFaTgeVN9A"), newTitle: "", replyTo: p3, topics: topics)]
        check(said == ["in no topic", "in topic “Release v2”", "opening a new topic (give it a title)",
                       "opening topic “Launch”", "as a reply in topic “Release”", "as a reply in no topic"],
              "the line under the box says where the post goes, a reply by its post's topic: \(said)")
        let opened = ChatTopics.destination(.topic("DataDataDataDataDataDA"), newTitle: "", replyTo: nil,
                                            topics: topics + posts.compactMap(\.topic))
        check(opened == "in topic “Data”", "a topic not yet in the chats' list is named by its post: \(opened)")
        let unlisted = ChatTopics.destination(.topic("GoneGoneGoneGoneGoneGA"), newTitle: "", replyTo: nil,
                                              topics: topics)
        check(unlisted == "in topic GoneGoneGoneGoneGoneGA",
              "a picked topic the topics given do not list is still the one the post goes in: \(unlisted)")
    }
}
