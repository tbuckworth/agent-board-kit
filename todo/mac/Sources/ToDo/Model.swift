import Foundation

// The data model: GitHub issues as todo-desk sends them, what the app reads out of them (list, checklist,
// notes...), and the queue of edits not yet on GitHub. Pure logic, no UI, so --selftest can check it.

let assignedLabel = "status:agent-assigned"

struct AgentEvent: Codable, Equatable {
    var kind: String
    var at: String?
    var text: String
    var url: String?
}

/// Read from the marker comments on an assigned task (todo-desk's agent_info).
struct AgentInfo: Codable, Equatable {
    var status: String?
    var name: String?
    var assignedAt: String?
    var events: [AgentEvent]
}

struct Issue: Codable, Equatable {
    var number: Int
    var title: String
    var body: String
    var state: String
    var url: String
    var createdAt: String
    var updatedAt: String
    var labels: [String]
    var agent: AgentInfo?
    /// The group this task is in: its parent issue (a GitHub sub-issue link), or nil.
    var parent: Int? = nil

    var isOpen: Bool { state == "OPEN" }

    /// "owner/name #N", read from the issue's URL as todo-desk sent it (https://github.com/owner/name/issues/N),
    /// so the tasks repo is never written into the app; just "#N" for a URL of another shape.
    var shortRef: String {
        let p = url.components(separatedBy: "/")
        if p.count >= 7, p[5] == "issues", !p[3].isEmpty, !p[4].isEmpty { return "\(p[3])/\(p[4]) #\(number)" }
        return "#\(number)"
    }
}

/// Every task in a group carries this label as well as the sub-issue link: linking alone doesn't change a task's
/// updatedAt, and the apps only fetch tasks updated since their last sync.
let subtaskLabel = "subtask"

struct GroupMeta: Codable, Equatable {
    var name: String
    var sortOrder: Int?
}

struct ListMeta: Codable, Equatable {
    var listName: String
    var groupName: String?
    var sortOrder: Int?
}

/// `.todo/config.json` in the tasks repo: groups and which group each list is in (shared with the web app).
struct TodoConfig: Codable, Equatable {
    var groups: [GroupMeta]
    var listMetadata: [ListMeta]

    init(groups: [GroupMeta] = [], listMetadata: [ListMeta] = []) {
        self.groups = groups
        self.listMetadata = listMetadata
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        groups = (try? c.decode([GroupMeta].self, forKey: .groups)) ?? []
        listMetadata = (try? c.decode([ListMeta].self, forKey: .listMetadata)) ?? []
    }

    /// The metadata for a list, by lower-case name. The file has both "Tasks" and "tasks"; one with a group wins.
    func meta(_ list: String) -> ListMeta? {
        let all = listMetadata.filter { $0.listName.lowercased() == list.lowercased() }
        return all.first { $0.groupName != nil } ?? all.first
    }

    func group(of list: String) -> String? { meta(list)?.groupName }
}

// MARK: - issue bodies

/// A body is notes, then `## ` sections (Checklist, Claude Resolution Notes, anything else). Edits change one
/// part and keep every other part as it was, so other tools' sections survive.
struct BodySection: Equatable {
    var header: String          // "" for the notes before any heading
    var text: String
}

enum IssueBody {
    static let checklistHeader = "## Checklist"
    static let claudeHeader = "## Claude Resolution Notes"

    static func split(_ body: String) -> [BodySection] {
        var out: [BodySection] = []
        var header = ""
        var buf: [Substring] = []
        for line in body.replacingOccurrences(of: "\r\n", with: "\n").split(separator: "\n", omittingEmptySubsequences: false) {
            if line.hasPrefix("## ") {
                out.append(BodySection(header: header, text: buf.joined(separator: "\n")))
                header = line.trimmingCharacters(in: .whitespaces)
                buf = []
            } else {
                buf.append(line)
            }
        }
        out.append(BodySection(header: header, text: buf.joined(separator: "\n")))
        return out
    }

    static func join(_ sections: [BodySection]) -> String {
        var parts: [String] = []
        for s in sections {
            let t = s.text.trimmingCharacters(in: CharacterSet(charactersIn: "\n"))
            if !s.header.isEmpty {
                parts.append(s.header + (t.isEmpty ? "" : "\n" + t))
            } else if !t.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                parts.append(t)
            }
        }
        return parts.map { $0.replacingOccurrences(of: "\\s+$", with: "", options: .regularExpression) }.joined(separator: "\n\n")
    }

    static func notes(_ body: String) -> String {
        split(body).first?.text.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    static func section(_ body: String, _ header: String) -> String? {
        split(body).first { $0.header == header }?.text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func claudeNotes(_ body: String) -> String? {
        guard let s = section(body, claudeHeader), !s.isEmpty else { return nil }
        return s
    }

    private static let checkbox = try! NSRegularExpression(pattern: #"^(\s*[-*] \[)([ xX])(\]\s*)(.*)$"#)

    static func parseCheckbox(_ line: String) -> (checked: Bool, text: String)? {
        let ns = line as NSString
        guard let m = checkbox.firstMatch(in: line, range: NSRange(location: 0, length: ns.length)) else { return nil }
        let mark = ns.substring(with: m.range(at: 2))
        return (mark.lowercased() == "x", ns.substring(with: m.range(at: 4)).trimmingCharacters(in: .whitespaces))
    }

    /// The checkboxes in one part of the body (the "## Checklist" section, or "" for loose ones in the notes).
    static func checklist(_ body: String, in header: String = checklistHeader) -> [ChecklistItem] {
        guard let s = split(body).first(where: { $0.header == header }) else { return [] }
        var items: [ChecklistItem] = []
        for (i, line) in s.text.split(separator: "\n", omittingEmptySubsequences: false).enumerated() {
            if let c = parseCheckbox(String(line)) {
                items.append(ChecklistItem(header: header, line: i, text: c.text, checked: c.checked))
            }
        }
        return items
    }

    private static func editLine(_ body: String, _ item: ChecklistItem, _ change: (String) -> String?) -> String {
        var secs = split(body)
        guard let si = secs.firstIndex(where: { $0.header == item.header }) else { return body }
        var lines = secs[si].text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        guard item.line < lines.count, parseCheckbox(lines[item.line]) != nil else { return body }
        if let new = change(lines[item.line]) { lines[item.line] = new } else { lines.remove(at: item.line) }
        secs[si].text = lines.joined(separator: "\n")
        return join(secs)
    }

    static func toggling(_ body: String, _ item: ChecklistItem) -> String {
        editLine(body, item) { line in
            let ns = line as NSString
            guard let m = checkbox.firstMatch(in: line, range: NSRange(location: 0, length: ns.length)) else { return line }
            let mark = ns.substring(with: m.range(at: 2)).lowercased() == "x" ? " " : "x"
            return ns.replacingCharacters(in: m.range(at: 2), with: mark)
        }
    }

    static func removing(_ body: String, _ item: ChecklistItem) -> String { editLine(body, item) { _ in nil } }

    /// The item's text replaced; its box (ticked or not) and indentation stay as they were.
    static func editing(_ body: String, _ item: ChecklistItem, to text: String) -> String {
        editLine(body, item) { line in
            let ns = line as NSString
            guard let m = checkbox.firstMatch(in: line, range: NSRange(location: 0, length: ns.length)) else { return line }
            return ns.substring(to: m.range(at: 4).location) + text.trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }

    static func adding(_ body: String, item text: String) -> String {
        let line = "- [ ] " + text.trimmingCharacters(in: .whitespacesAndNewlines)
        var secs = split(body)
        if let si = secs.firstIndex(where: { $0.header == checklistHeader }) {
            let t = secs[si].text.trimmingCharacters(in: .whitespacesAndNewlines)
            secs[si].text = t.isEmpty ? line : t + "\n" + line
        } else {
            secs.insert(BodySection(header: checklistHeader, text: line), at: min(1, secs.count))
        }
        return join(secs)
    }

    static func withNotes(_ body: String, _ notes: String) -> String {
        var secs = split(body)
        if secs.isEmpty { secs = [BodySection(header: "", text: "")] }
        secs[0].text = notes.trimmingCharacters(in: .whitespacesAndNewlines)
        return join(secs)
    }

    /// A new task's body: notes, then a checklist section if it has items (as the web app writes it).
    static func build(notes: String, checklist items: [String]) -> String {
        var secs = [BodySection(header: "", text: notes.trimmingCharacters(in: .whitespacesAndNewlines))]
        let lines = items.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }.map { "- [ ] " + $0 }
        if !lines.isEmpty { secs.append(BodySection(header: checklistHeader, text: lines.joined(separator: "\n"))) }
        return join(secs)
    }
}

struct ChecklistItem: Identifiable, Equatable {
    var header: String
    var line: Int
    var text: String
    var checked: Bool
    var id: String { "\(header)#\(line)" }
}

// MARK: - labels

enum Labels {
    /// One of each per task: adding one replaces the other with the same prefix.
    static let single = ["list:", "section:", "group:", "action:", "priority:", "importance:", "urgency:", "due:", "follow-up:"]

    static func value(_ labels: [String], _ prefix: String) -> String? {
        guard let l = labels.first(where: { $0.lowercased().hasPrefix(prefix) }) else { return nil }
        let v = String(l.dropFirst(prefix.count))
        return v.isEmpty ? nil : v
    }

    /// current + add - remove, the same rule as todo-desk's apply_labels.
    static func apply(_ current: [String], add: [String], remove: [String]) -> [String] {
        let rm = Set(remove.map { $0.lowercased() })
        var out = current.filter { !rm.contains($0.lowercased()) }
        for a in add {
            if let p = single.first(where: { a.lowercased().hasPrefix($0) }) {
                out = out.filter { !$0.lowercased().hasPrefix(p) || $0.lowercased() == a.lowercased() }
            }
            if !out.contains(where: { $0.lowercased() == a.lowercased() }) { out.append(a) }
        }
        return out
    }
}

let sections = ["personal", "work", "home", "health", "finance"]
let priorities = ["high", "medium", "low"]

/// Tags: `tag:<name>` labels, any number per task, such as `tag:home` for what can only be done at
/// home. The list filters can show only the tasks with a tag, or hide them.
enum Tags {
    static let prefix = "tag:"

    /// A typed tag as a label name: trimmed, lower case, spaces to dashes, nothing but a-z, 0-9 and dashes.
    static func normalise(_ s: String) -> String {
        var t = s.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if t.hasPrefix(prefix) { t = String(t.dropFirst(prefix.count)) }
        t = t.replacingOccurrences(of: #"\s+"#, with: "-", options: .regularExpression)
            .replacingOccurrences(of: "[^a-z0-9-]", with: "", options: .regularExpression)
            .replacingOccurrences(of: "-{2,}", with: "-", options: .regularExpression)
        return t.trimmingCharacters(in: CharacterSet(charactersIn: "-"))
    }

    static func of(_ labels: [String]) -> [String] {
        Array(Set(labels.filter { $0.lowercased().hasPrefix(prefix) }.map { normalise($0) }.filter { !$0.isEmpty })).sorted()
    }
}

/// One tag in the list filter: only the tasks with it, or none of them. Not in the filter: neutral.
enum TagMode: String, Codable {
    case only, hide

    /// A click on the chip: neutral → only → hide → neutral.
    static func next(_ m: TagMode?) -> TagMode? {
        switch m {
        case nil: return .only
        case .only: return .hide
        case .hide: return nil
        }
    }
}

/// Waiting on someone: the `status:waiting` label, an optional `follow-up:YYYY-MM-DD` label for when
/// to chase, and who it is on the body's first line, `Waiting on: <who>`. Waiting tasks are greyed at the bottom
/// of a list until the follow-up day.
enum Waiting {
    static let label = "status:waiting"
    /// A daily agent thinks the wait is over (the email came, say): it swaps `status:waiting` for this, keeps who and
    /// the follow-up, and gives its evidence in the Claude notes. Such a task goes to the top: "Came through?"
    static let cleared = "status:waiting-cleared"
    static let followUpPrefix = "follow-up:"
    private static let line = try! NSRegularExpression(pattern: #"^\s*waiting on:\s*(.*?)\s*$"#, options: [.caseInsensitive])

    private static func firstLine(_ body: String) -> String {
        String(body.replacingOccurrences(of: "\r\n", with: "\n").split(separator: "\n", omittingEmptySubsequences: false).first ?? "")
    }

    private static func isLine(_ s: String) -> Bool {
        line.firstMatch(in: s, range: NSRange(location: 0, length: (s as NSString).length)) != nil
    }

    /// Who, from the body's first line, if it is a "Waiting on:" line.
    static func who(_ body: String) -> String? {
        let first = firstLine(body)
        let ns = first as NSString
        guard let m = line.firstMatch(in: first, range: NSRange(location: 0, length: ns.length)) else { return nil }
        let w = ns.substring(with: m.range(at: 1))
        return w.isEmpty ? nil : w
    }

    /// The body without its "Waiting on:" line (and the blank lines after it).
    static func stripped(_ body: String) -> String {
        let b = body.replacingOccurrences(of: "\r\n", with: "\n")
        let first = firstLine(b)
        guard isLine(first) else { return body }
        return String(b.dropFirst(first.count)).replacingOccurrences(of: #"^\n+"#, with: "", options: .regularExpression)
    }

    /// The body with "Waiting on: <who>" as its first line, or with no such line when `who` is nil or empty.
    static func withWho(_ body: String, _ who: String?) -> String {
        let rest = stripped(body)
        let w = (who ?? "").replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
        if w.isEmpty { return rest }
        return "Waiting on: " + w + (rest.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "" : "\n\n" + rest)
    }

    private static let dayFormat: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    /// `YYYY-MM-DD` for a day, in the Mac's time zone.
    static func day(_ d: Date) -> String { dayFormat.string(from: d) }

    static func date(_ day: String) -> Date? { dayFormat.date(from: day) }

    /// `YYYY-MM-DD`, `days` from `now`.
    static func day(in days: Int, from now: Date = Date()) -> String {
        day(Calendar.current.date(byAdding: .day, value: days, to: now) ?? now)
    }

    /// "23 Oct" (with the year when it isn't this year).
    static func short(_ day: String, now: Date = Date()) -> String {
        guard let d = date(day) else { return day }
        let f = DateFormatter()
        f.dateFormat = Calendar.current.isDate(d, equalTo: now, toGranularity: .year) ? "d MMM" : "d MMM yyyy"
        return f.string(from: d)
    }
}

/// Normalise a typed list name the way the web app does: lower case, spaces to dashes.
func listSlug(_ s: String) -> String {
    s.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        .replacingOccurrences(of: #"\s+"#, with: "-", options: .regularExpression)
}

// MARK: - a task as the app shows it

struct TaskItem: Identifiable, Equatable {
    var issue: Issue
    var notes: String
    var looseChecklist: [ChecklistItem]
    var checklist: [ChecklistItem]
    var claudeNotes: String?
    var list: String
    var section: String?
    var group: String?
    var action: String
    var priority: String
    var tags: [String]
    var waiting: Bool
    var cleared: Bool                 // an agent thinks the wait is over: the owner confirms or says still waiting
    var waitingOn: String?
    var followUp: String?             // YYYY-MM-DD
    var pending = false
    var conflict: [String]?
    var failed: String?

    var id: Int { issue.number }
    var number: Int { issue.number }
    var parent: Int? { issue.parent }
    var importance: String? { Labels.value(issue.labels, "importance:")?.lowercased() }
    var title: String { issue.title }
    var isOpen: Bool { issue.isOpen }
    var isLocal: Bool { issue.number < 0 }
    var assigned: Bool { issue.labels.contains(assignedLabel) }
    var agent: AgentInfo? { issue.agent }

    /// An agent is (or should soon be) working on it: assigned, open, and not reported done, blocked or failed.
    var agentActive: Bool {
        guard assigned, isOpen else { return false }
        guard let s = agent?.status else { return true }
        return s == "assigned" || s == "started"
    }

    var allChecklist: [ChecklistItem] { looseChecklist + checklist }
    var checklistDone: Int { allChecklist.filter { $0.checked }.count }

    /// Waiting, and the follow-up day has come: shown as usual again, flagged "Follow up".
    func followUpDue(today: String) -> Bool {
        guard waiting, isOpen, let f = followUp else { return false }
        return f <= today
    }

    /// Waiting, and not yet time to chase: greyed, at the bottom of its list.
    func greyed(today: String) -> Bool { waiting && !cleared && isOpen && !followUpDue(today: today) }

    /// An agent thinks the wait is over and the owner hasn't said yet: at the top of its list.
    var cameThrough: Bool { cleared && isOpen }

    init(_ issue: Issue, config: TodoConfig?) {
        self.issue = issue
        let secs = IssueBody.split(issue.body)
        notes = Waiting.stripped(secs.first?.text.trimmingCharacters(in: .whitespacesAndNewlines) ?? "")
        looseChecklist = IssueBody.checklist(issue.body, in: "")
        checklist = IssueBody.checklist(issue.body)
        claudeNotes = IssueBody.claudeNotes(issue.body)
        list = (Labels.value(issue.labels, "list:") ?? "tasks").lowercased()
        section = Labels.value(issue.labels, "section:")
        group = Labels.value(issue.labels, "group:") ?? config?.group(of: list)
        action = Labels.value(issue.labels, "action:") ?? "manual"
        priority = Labels.value(issue.labels, "priority:") ?? "medium"
        tags = Tags.of(issue.labels)
        waiting = issue.labels.contains { $0.lowercased() == Waiting.label }
        cleared = issue.labels.contains { $0.lowercased() == Waiting.cleared }
        waitingOn = Waiting.who(issue.body)
        followUp = Labels.value(issue.labels, Waiting.followUpPrefix)
    }

    func matches(words: [String]) -> Bool {
        let hay = (issue.title + "\n" + notes).lowercased()
        return words.allSatisfy { hay.contains($0) }
    }
}

// MARK: - edits not yet on GitHub

struct ConfigOp: Codable, Equatable {
    var op: String
    var list: String?
    var group: String?
    var order: Int?                   // set_sort: the list's or group's new sortOrder

    var json: [String: Any] {
        var d: [String: Any] = ["op": op]
        if let l = list { d["list"] = l }
        if let g = group { d["group"] = g }
        if let o = order { d["order"] = o }
        return d
    }
}

/// One change waiting to reach GitHub. Updates to the same task merge while they wait, so a task edited
/// five times offline is one request, checked against what it looked like before the first edit.
struct PendingOp: Codable, Identifiable, Equatable {
    enum Kind: String, Codable { case create, update, config, deleteList, setParent }

    var id = UUID()
    var kind: Kind
    var number = 0                    // negative: a task created here, not yet on GitHub
    var title: String?                // create
    var body: String?
    var labels: [String]?
    var set: [String: String] = [:]   // update: title, body, state
    var base: [String: String] = [:]  // update: title and body as they were before the first edit
    var add: [String] = []
    var remove: [String] = []
    var force = false
    var configOps: [ConfigOp] = []
    var list: String?                 // deleteList
    var parent: Int?                  // setParent: the group to put the task in (negative: one not on GitHub yet), nil: none
    var conflict: [String]?
    var error: String?
    var attempts = 0
    var revision = 0                  // bumped when a later edit is folded in, so the folded request is new
    var sent = false                  // a create that was sent once is never changed (its reply may have been lost)
    var created = Date()
    var what: String                  // for the pending list: "Complete “Buy milk”"

    var blocked: Bool { conflict != nil || attempts >= PendingOp.maxAttempts }
    static let maxAttempts = 4

    /// What todo-desk is sent. "op" lets it recognise a retry of something it already did.
    var request: [String: Any] {
        var r: [String: Any]
        switch kind {
        case .create:
            r = ["cmd": "create", "title": title ?? "", "body": body ?? "", "labels": labels ?? []]
        case .update:
            r = ["cmd": "update", "number": number, "set": set, "base": base, "add": add, "remove": remove, "force": force]
        case .config:
            r = ["cmd": "config", "ops": configOps.map { $0.json }]
        case .deleteList:
            r = ["cmd": "delete_list", "list": list ?? ""]
        case .setParent:
            r = ["cmd": "set_parent", "number": number, "parent": parent.map { $0 as Any } ?? NSNull()]
        }
        r["op"] = id.uuidString + (force ? "-force" : "") + "-\(attempts)-\(revision)"
        return r
    }

    /// Fold a later update of the same task into this one.
    mutating func absorb(_ later: PendingOp) {
        for (k, v) in later.set { set[k] = v }
        for (k, v) in later.base where base[k] == nil && set[k] != nil { base[k] = v }
        for a in later.add {
            remove.removeAll { $0.lowercased() == a.lowercased() }
            if let p = Labels.single.first(where: { a.lowercased().hasPrefix($0) }) {
                add.removeAll { $0.lowercased().hasPrefix(p) }
            }
            if !add.contains(where: { $0.lowercased() == a.lowercased() }) { add.append(a) }
        }
        for r in later.remove {
            add.removeAll { $0.lowercased() == r.lowercased() }
            if !remove.contains(where: { $0.lowercased() == r.lowercased() }) { remove.append(r) }
        }
        what = later.what
        revision += 1
    }

    /// Fold an update (with no state change) into a task that is itself still waiting to be created.
    mutating func absorbIntoCreate(_ later: PendingOp) {
        if let t = later.set["title"] { title = t }
        if let b = later.set["body"] { body = b }
        labels = Labels.apply(labels ?? [], add: later.add, remove: later.remove)
        revision += 1
    }
}

enum Overlay {
    /// What the app shows for a task: GitHub's copy with every waiting edit applied on top.
    static func apply(_ issue: Issue?, _ ops: [PendingOp], number: Int, now: String) -> Issue? {
        var out = issue
        for op in ops {
            switch op.kind {
            case .create where op.number == number:
                out = Issue(number: number, title: op.title ?? "", body: op.body ?? "", state: "OPEN", url: "",
                            createdAt: now, updatedAt: now, labels: op.labels ?? [], agent: nil)
            case .update where op.number == number:
                guard var i = out else { continue }
                if let t = op.set["title"] { i.title = t }
                if let b = op.set["body"] { i.body = b }
                if let s = op.set["state"] { i.state = s }
                i.labels = Labels.apply(i.labels, add: op.add, remove: op.remove)
                if op.conflict == nil { i.updatedAt = max(i.updatedAt, isoString(op.created)) }
                out = i
            case .setParent where op.number == number:
                guard var i = out else { continue }
                i.parent = op.parent
                i.labels = op.parent == nil ? Labels.apply(i.labels, add: [], remove: [subtaskLabel])
                    : Labels.apply(i.labels, add: [subtaskLabel], remove: [])
                out = i
            case .deleteList:
                guard var i = out, let l = op.list,
                      i.labels.contains(where: { $0.lowercased() == "list:" + l }) else { continue }
                let rm = i.labels.filter { $0.lowercased().hasPrefix("list:") || $0.lowercased().hasPrefix("group:") }
                i.labels = Labels.apply(i.labels, add: ["list:tasks"], remove: rm)
                out = i
            default:
                continue
            }
        }
        return out
    }

    static func config(_ cfg: TodoConfig?, _ ops: [PendingOp]) -> TodoConfig? {
        var c = cfg ?? TodoConfig()
        var touched = false
        for op in ops {
            let list = op.kind == .deleteList ? [ConfigOp(op: "remove_list", list: op.list)] : op.configOps
            for o in list {
                touched = true
                let name = (o.list ?? "").lowercased()
                switch o.op {
                case "add_list":
                    if c.meta(name) == nil { c.listMetadata.append(ListMeta(listName: name, groupName: o.group, sortOrder: 0)) }
                case "remove_list":
                    c.listMetadata.removeAll { $0.listName.lowercased() == name }
                case "set_group":
                    if c.meta(name) == nil { c.listMetadata.append(ListMeta(listName: name, groupName: nil, sortOrder: 0)) }
                    for i in c.listMetadata.indices where c.listMetadata[i].listName.lowercased() == name {
                        c.listMetadata[i].groupName = o.group
                    }
                case "add_group":
                    if let g = o.group, !c.groups.contains(where: { $0.name == g }) {
                        let top = c.groups.compactMap { $0.sortOrder }.max() ?? 0
                        c.groups.append(GroupMeta(name: g, sortOrder: top + 1))
                    }
                case "set_sort":
                    if let n = o.order, let g = o.group, o.list == nil {
                        for i in c.groups.indices where c.groups[i].name == g { c.groups[i].sortOrder = n }
                    } else if let n = o.order, !name.isEmpty {
                        if c.meta(name) == nil { c.listMetadata.append(ListMeta(listName: name, groupName: nil, sortOrder: n)) }
                        for i in c.listMetadata.indices where c.listMetadata[i].listName.lowercased() == name { c.listMetadata[i].sortOrder = n }
                    }
                case "remove_group":
                    c.groups.removeAll { $0.name == o.group }
                    for i in c.listMetadata.indices where c.listMetadata[i].groupName == o.group { c.listMetadata[i].groupName = nil }
                default:
                    break
                }
            }
        }
        return touched || cfg != nil ? c : nil
    }
}

func isoString(_ d: Date) -> String { Fmt.isoParser.string(from: d) }

/// The label changes that turn a task's fields into the edited ones. Only fields that changed are touched,
/// and labels the app doesn't manage are never sent.
func labelDiff(from t: TaskItem, list: String, section: String?, priority: String,
               config: TodoConfig?) -> (add: [String], remove: [String]) {
    var add: [String] = [], remove: [String] = []
    let labels = t.issue.labels
    func swap(_ prefix: String, _ new: String?) {
        let old = labels.filter { $0.lowercased().hasPrefix(prefix) }
        let wanted = new.map { prefix + $0 }
        if let w = wanted, old.count == 1, old[0].lowercased() == w.lowercased() { return }
        if wanted == nil && old.isEmpty { return }
        remove += old.filter { $0.lowercased() != wanted?.lowercased() }
        if let w = wanted { add.append(w) }
    }
    let newList = listSlug(list).isEmpty ? "tasks" : listSlug(list)
    if newList != t.list {
        swap("list:", newList)
        swap("group:", config?.group(of: newList))
    }
    let sec = section.map { $0.trimmingCharacters(in: .whitespaces).lowercased() }.flatMap { $0.isEmpty ? nil : $0 }
    if sec != t.section?.lowercased() { swap("section:", sec) }
    if priority != t.priority { swap("priority:", priority) }
    return (add, remove)
}

/// The label changes from a task's tags to `tags` (typed names are normalised). Removes use GitHub's spelling.
func tagDiff(from t: TaskItem, to tags: [String]) -> (add: [String], remove: [String]) {
    let want = Set(tags.map(Tags.normalise).filter { !$0.isEmpty })
    let add = want.subtracting(t.tags).sorted().map { Tags.prefix + $0 }
    let remove = t.issue.labels.filter { $0.lowercased().hasPrefix(Tags.prefix) && !want.contains(Tags.normalise($0)) }
    return (add, remove)
}

/// The label changes for waiting on someone: on (with an optional follow-up day) or off, which takes the
/// follow-up off too. An old follow-up label is removed by name, so a helper that doesn't know the label is
/// single-valued still ends with one.
func waitingDiff(from t: TaskItem, waiting: Bool, followUp: String?) -> (add: [String], remove: [String]) {
    let olds = t.issue.labels.filter { $0.lowercased().hasPrefix(Waiting.followUpPrefix) }
    let clearedLabels = t.issue.labels.filter { $0.lowercased() == Waiting.cleared }
    guard waiting else { return ([], t.issue.labels.filter { $0.lowercased() == Waiting.label } + clearedLabels + olds) }
    var add: [String] = [], remove: [String] = clearedLabels       // waiting again: the agent's "came through?" goes
    if !t.waiting { add.append(Waiting.label) }
    if let f = followUp, !f.isEmpty {
        if t.followUp != f { add.append(Waiting.followUpPrefix + f); remove += olds }
    } else {
        remove += olds
    }
    return (add, remove)
}

// MARK: - groups of tasks (the same rules as the web app's groups.js)

/// One row of a task list: a task, how deep it sits under groups, and its own counts if it is a group.
struct RowEntry: Identifiable, Equatable {
    var task: TaskItem
    var depth = 0
    var childCount = 0
    var doneCount = 0
    var expanded = false
    var id: Int { task.number }
    var isGroup: Bool { childCount > 0 }
}

enum Grouping {
    /// group number -> (tasks in it, how many are done), over every task that names it as its group
    static func counts(_ all: [TaskItem]) -> [Int: (total: Int, done: Int)] {
        var c: [Int: (total: Int, done: Int)] = [:]
        for t in all {
            guard let p = t.parent else { continue }
            let old = c[p] ?? (0, 0)
            c[p] = (old.total + 1, old.done + (t.isOpen ? 0 : 1))
        }
        return c
    }

    /// Rows for `tasks` (filtered and ordered already): a task whose group is also shown sits under it, indented,
    /// unless the group is folded. A task reached through no group (a parent loop) still shows at the top.
    static func rows(_ tasks: [TaskItem], all: [TaskItem], folded: Set<Int>) -> [RowEntry] {
        let shown = Set(tasks.map { $0.number })
        var children: [Int: [TaskItem]] = [:]
        for t in tasks {
            if let p = t.parent, p != t.number, shown.contains(p) { children[p, default: []].append(t) }
        }
        let c = counts(all)
        let byNumber = Dictionary(tasks.map { ($0.number, $0) }, uniquingKeysWith: { a, _ in a })
        var out: [RowEntry] = []
        var placed = Set<Int>()
        func add(_ t: TaskItem, _ depth: Int) {
            guard !placed.contains(t.number) else { return }
            placed.insert(t.number)
            let n = c[t.number] ?? (0, 0)
            let expanded = n.total > 0 && !folded.contains(t.number)
            out.append(RowEntry(task: t, depth: depth, childCount: n.total, doneCount: n.done, expanded: expanded))
            if expanded { for ch in children[t.number] ?? [] { add(ch, depth + 1) } }
        }
        for t in tasks {
            if let p = t.parent, p != t.number, shown.contains(p) { continue }
            add(t, 0)
        }
        func inFolded(_ t: TaskItem) -> Bool {
            var seen = Set<Int>(), p = t.parent
            while let q = p, let g = byNumber[q], !seen.contains(q) {
                if folded.contains(q) { return true }
                seen.insert(q)
                p = g.parent
            }
            return false
        }
        for t in tasks where !placed.contains(t.number) && !inFolded(t) { add(t, 0) }
        return out
    }

    /// The open tasks in group `n`, and in groups within it
    static func openDescendants(_ n: Int, all: [TaskItem]) -> [Int] {
        var out: [Int] = [], seen: Set<Int> = [n], queue = [n]
        while !queue.isEmpty {
            let g = queue.removeFirst()
            for t in all where t.parent == g && !seen.contains(t.number) {
                seen.insert(t.number)
                queue.append(t.number)
                if t.isOpen { out.append(t.number) }
            }
        }
        return out
    }

    /// The numbers plus the open tasks of any group among them, each once
    static func withMembers(_ numbers: [Int], all: [TaskItem]) -> [Int] {
        var out = numbers, seen = Set(numbers)
        for n in numbers {
            for d in openDescendants(n, all: all) where !seen.contains(d) { seen.insert(d); out.append(d) }
        }
        return out
    }

    /// A new group's rating labels: the highest importance among its tasks, urgent if any is, the earliest due date
    static func ratingLabels(_ tasks: [TaskItem]) -> [String] {
        let order = ["high": 3, "medium": 2, "low": 1]
        let imp = tasks.compactMap { $0.importance }.max { (order[$0] ?? 0) < (order[$1] ?? 0) }
        var out: [String] = []
        if let i = imp, order[i] != nil {
            out.append("importance:" + i)
            let urgent = tasks.contains { Labels.value($0.issue.labels, "urgency:")?.lowercased() == "high" }
            out.append("urgency:" + (urgent ? "high" : "low"))
        }
        if let due = tasks.compactMap({ Labels.value($0.issue.labels, "due:") }).sorted().first { out.append("due:" + due) }
        return out
    }
}

// MARK: - running agents (Assign to a running agent)

/// One agent running on the agent board, as todo-desk's `agents` command lists it.
struct BoardAgent: Codable, Equatable, Identifiable {
    var id: String
    var cli: String
    var name: String
    var state: String                 // waiting, blocked, idle, background, busy or stale: the board's colours
    var folder: String
    var note: String
    var active: String?
    var group: String?

    /// The board's words for its state, short enough for a row.
    var stateLabel: String {
        switch state {
        case "waiting": return "waiting on you"
        case "background": return "bg wait"
        default: return state
        }
    }

    /// The search box: every word must appear in the name, folder, group or last note.
    func matches(_ query: String) -> Bool {
        let hay = [name, folder, group ?? "", note].joined(separator: " ").lowercased()
        return query.lowercased().split(separator: " ").allSatisfy { hay.contains($0) }
    }

    static func decode(_ any: Any?) -> [BoardAgent] {
        ((any as? [Any]) ?? []).compactMap { a in
            guard let d = a as? [String: Any], JSONSerialization.isValidJSONObject(d),
                  let data = try? JSONSerialization.data(withJSONObject: d) else { return nil }
            return try? JSONDecoder().decode(BoardAgent.self, from: data)
        }
    }
}
