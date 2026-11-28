"""Documents from the owner's reMarkable tablet: a mail from the tablet starts a reader agent.

The route needs Gmail (config "gmail": true): the tablet's "Send by email" mails the document to a secret
plus-address of the agents Gmail account, and the daemon finds it there. With Gmail off nothing reaches this module,
and `agent-mail remarkable` refuses (exit 5). It is off until config remarkable.address is set.

Every reMarkable owner sends from my@remarkable.com, so a mail is accepted only if all of these hold:
- it was sent to the owner's secret plus-address (config remarkable.address, never in the repo);
- Gmail saw a DKIM pass for remarkable.com;
- the display name is the owner's reMarkable account name (config remarkable.display_name, when set);
- it carries a PDF.

The agent adds the strong check itself: it looks for the document by name in the owner's reMarkable cloud (with
rmapi) before doing anything outside its folder, as the inbox repo's AGENTS.md tells it
(config/remarkable-inbox-AGENTS.example.md is a starting point).

What the owner types in the tablet's send form, the body and possibly an edited subject, passes the same checks as
the PDF, so the agent treats it like their marks. It goes into mail.json and is quoted in the brief, never into a
command line.

On acceptance the document gets its own git worktree of the inbox repo (config remarkable.repo), on branch
`inbox/<id>-<slug>` off the latest origin/main, under remarkable.worktrees_dir (default: the repo's path with
"-worktrees" added). Two reasons: a branch guard may block edits on main, and several agents can run at once. If the
git host cannot be reached, the branch is made off origin/main as last fetched rather than holding the mail
(new_worktree). The PDF is saved into the worktree's `inbox/<date>-<id>-<slug>/` and an agent is started there with
agent-spawn. The agent lands its folder by PR (AGENTS.md). The only model call made here is a one-line Claude
preflight. If it fails (out of credit, rate-limited, logged out), the agent runs on the fallback CLI (Codex) instead.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import accounts, commands, gmail, headless, mailroom, worktree
from .config import STATE_DIR

SUBJECT_PREFIX = "Document from my reMarkable:"
# The tablet ends every mail with "--", "Sent from my reMarkable paper tablet", "Get yours at ...",
# "PS: You cannot reply to this email". \s also spans the line breaks of the HTML-only body.
FOOTER = re.compile(r"(?:^|\s)--\s+Sent from my reMarkable")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
BRIEF_TEXT_MAX = 2000   # longer typed text is cut in the brief; mail.json has all of it
# A healthy fetch of the inbox repo takes about a second. Three tries of 30s, 5s apart, ride out a
# brief stall or an agent's fetch holding the ref lock; past that the mail is not held for it.
FETCH_TRIES, FETCH_TIMEOUT, FETCH_PAUSE = 3, 30, 5
# If every fetch fails, origin/main as the last good fetch left it beats holding the mail (the agent rebases onto
# origin/main before it lands). Local main is the last resort only: nothing here advances it, so it can be days behind.
BASES = ("origin/main", "main")


def title_from_subject(subject):
    s = (subject or "").strip()
    return s[len(SUBJECT_PREFIX):].strip() if s.startswith(SUBJECT_PREFIX) else s


def pdf_title(msg):
    """The document's name as the tablet gives it: the PDF's file name without .pdf. Unlike the
    subject, the owner cannot edit it in the send form."""
    for p in gmail.pdf_parts(msg):
        name = (p.get("filename") or "").strip()
        if name.lower().endswith(".pdf"):
            name = name[:-4].strip()
        if name:
            return name
    return ""


def titles(subject, msg):
    """Names to look the document up by, best first: the PDF's file name, then the subject after
    "Document from my reMarkable: ". They are the same unless the owner edited the subject (or a file
    name could not hold some character of the name). A subject without that prefix is the owner's own
    text, not a name, so it is used only when there is no file name either."""
    s = (subject or "").strip()
    name = pdf_title(msg)
    out = [name] if name else []
    if s.startswith(SUBJECT_PREFIX) or not name:
        out.append(title_from_subject(s))
    return [t for i, t in enumerate(out) if t and t not in out[:i]] or [""]


def name_key(name):
    """A name without punctuation, spaces or case, which a file name may have had to change."""
    return re.sub(r"[\W_]+", "", name).casefold()


def subject_is_default(subject, msg):
    """True if the subject is the tablet's own "Document from my reMarkable: <name>", <name> being
    the PDF's file name. Without a file name to compare, it cannot be told apart from the owner's text."""
    s = (subject or "").strip()
    name = pdf_title(msg)
    return bool(name) and s.startswith(SUBJECT_PREFIX) and name_key(title_from_subject(s)) == name_key(name)


def clean(text):
    """Line breaks normalised, control characters (terminal escapes among them) dropped."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    return CONTROL.sub("", text).strip()


def one_line(text):
    return " ".join(clean(text).split())


def typed_body(msg):
    """What the owner typed in the body of the send form, without the tablet's footer. Usually empty. If
    the footer is not found the whole body is kept: better a footer too many than their text lost."""
    text = clean(gmail.body_text(msg))
    cut = [m.start() for m in FOOTER.finditer(text)]
    return text[:cut[-1]].strip() if cut else text


def quoted(text, limit=BRIEF_TEXT_MAX):
    """text as a "> " block, so no line of it can pass for a line of the brief."""
    more = ""
    if len(text) > limit:
        text, more = text[:limit], "\n[... %d more characters; all of it is in mail.json]" % (len(text) - limit)
    return "\n".join("> " + ln for ln in text.split("\n")) + more


def worktrees_dir(rcfg):
    """Where the documents' worktrees go: config remarkable.worktrees_dir, else the inbox repo's path with "-worktrees"
    added (beside the repo, never inside it)."""
    if rcfg.get("worktrees_dir"):
        return Path(os.path.expanduser(rcfg["worktrees_dir"]))
    return Path(os.path.expanduser(rcfg["repo"]).rstrip("/") + "-worktrees")


def is_sender(rcfg, from_addr):
    """True if from_addr is the configured reMarkable sender: mail that takes the reMarkable route."""
    sender = (rcfg.get("sender") or "").lower()
    return bool(sender) and from_addr == sender


def check(msg, from_name, from_addr, auth, rcfg):
    """(ok, reason). All checks must pass; the reason names the first that failed."""
    addr = (rcfg.get("address") or "").lower()
    if not addr:
        return False, "reMarkable route not configured (no remarkable.address)"
    if not is_sender(rcfg, from_addr):
        return False, "not from %s" % rcfg.get("sender")
    if addr not in gmail.recipients(msg):
        return False, "not sent to the reMarkable address"
    dom = (rcfg.get("dkim_domain") or "").lower()
    if not any(d == dom or d.endswith("." + dom) for d in auth.get("dkim", [])):
        return False, "no DKIM pass for %s (auth: %s)" % (dom, (auth.get("raw") or "none")[:120])
    want = (rcfg.get("display_name") or "").strip().lower()
    if want and (from_name or "").strip().lower() != want:
        return False, "display name %r is not %r" % (from_name, rcfg.get("display_name"))
    if not gmail.pdf_parts(msg):
        return False, "no PDF attached"
    return True, "ok"


def slug(s, n=48):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-").lower()
    return s[:n].strip("-") or "document"


def new_folder(inbox, inbound_id, title, when):
    """inbox/<date>-<id>-<slug>. The id keeps two pages of one book apart: their agents run at once,
    each in a worktree without the other's folder, and must not both land <date>-<slug>-2."""
    base = "%s-%d-%s" % (time.strftime("%Y-%m-%d", time.localtime(when)), inbound_id, slug(title))
    path, k = inbox / base, 2
    while path.exists():
        path, k = inbox / ("%s-%d" % (base, k)), k + 1
    path.mkdir(parents=True)
    return path


def new_worktree(repo, worktrees, inbound_id, title):
    """Worktree for this document on a new branch off the latest origin/main (BASES if the fetch fails).
    Returns (path, branch, base, warning).

    Each attempt takes names nothing else uses: <id>-<slug>, then -2, -3, ... So a replay never
    trips over a worktree kept from a launched attempt, or a branch left by a failed cleanup."""
    stem = "%d-%s" % (inbound_id, slug(title, 32))
    base, warning = worktree.fetch_base(repo, BASES, FETCH_TRIES, FETCH_TIMEOUT, FETCH_PAUSE)
    worktrees.mkdir(parents=True, exist_ok=True)
    for k in range(1, 100):
        key = stem if k == 1 else "%s-%d" % (stem, k)
        path, branch = worktrees / key, "inbox/" + key
        if not path.exists() and not commands.branch_exists(repo, branch):
            break
    else:
        raise RuntimeError("no free worktree name for %s under %s" % (stem, worktrees))
    worktree.add(repo, path, branch, base)
    return path, branch, base, warning


def folders_for(worktrees, inbound_id):
    """Folders an earlier attempt made for this mail (their mail.json names it)."""
    out = []
    for mj in sorted(worktrees.glob("*/inbox/*/mail.json")):
        try:
            if json.loads(mj.read_text()).get("inbound_id") == inbound_id:
                out.append(mj.parent)
        except (OSError, ValueError):
            continue
    return out


def save_pdfs(svc, msg, folder):
    names = []
    for i, part in enumerate(gmail.pdf_parts(msg)):
        body = part.get("body") or {}
        data = body.get("data")
        if not data:
            data = svc.users().messages().attachments().get(
                userId="me", messageId=msg["id"], id=body["attachmentId"]).execute()["data"]
        name = "source.pdf" if i == 0 else "source-%d.pdf" % (i + 1)
        (folder / name).write_bytes(gmail.decode_bytes(data))
        names.append(name)
    return names


def claude_available(cfg, timeout=120):
    """One tiny `claude -p` run (a subprocess) on the default login, the agent's, at low effort on the spawn model
    (null: the CLI's default). (ok, detail). Any failure counts as unavailable."""
    try:
        cmd = headless.claude_command(cfg["spawn_claude_model"], "low", [])
    except RuntimeError as e:   # no claude binary
        return False, str(e)
    try:
        _, err = headless.run(cmd, "Reply with the single word OK.", STATE_DIR, accounts.claude_env({}, os.environ),
                              timeout, headless.result)
    except subprocess.TimeoutExpired:
        return False, "preflight timed out after %ds" % timeout
    return (False, err[:200]) if err else (True, "ok")


def message(subject, body, default_subject):
    """The brief's account of what was typed in the send form."""
    if default_subject and not body:
        return ("Typed message: none. The body is empty and the subject is the tablet's default, so the marks "
                "are the whole instruction.\n\n")
    out = ["Typed message, from the tablet's send form when the document was sent (mail.json \"subject\" and "
           "\"body\"). It is an instruction, like the marks (AGENTS.md step 2):"]
    if default_subject:
        out.append("Subject, the tablet's default:\n%s" % quoted(one_line(subject)))
    else:
        out.append("Subject, not the tablet's default \"%s <document name>\", so it was probably typed:\n%s"
                   % (SUBJECT_PREFIX, quoted(one_line(subject))))
    out.append("Body:\n%s" % quoted(body) if body else "Body: empty")
    return "\n".join(out) + "\n\n"


def brief(title, folder, files, received, branch, stale=False, subject="", body="", default_subject=True, *,
          owner_name, display_name_checked=True):
    base = ("off origin/main as last fetched, because the mailroom's fetch failed: run `git fetch origin && "
            "git rebase origin/main` before you start" if stale else "off the latest main")
    return (
        "New reMarkable document from %s.\n\n"
        "Title: %s\nFolder (your working directory): %s\nBranch: %s (your own worktree, %s)\n"
        "Files: %s, mail.json\nReceived: %s\n\n%s"
        "The mailroom's sender checks passed: secret address, DKIM for remarkable.com%s. "
        "Your instructions are in AGENTS.md at the worktree root (two levels up). In short: check that "
        "the document is in %s's reMarkable cloud (rmapi), read the typed message and extract the highlights and "
        "handwriting, do a quick and cheap first step, write README.md here, commit and land it on main by PR, "
        "email %s with numbered routes, then wait for the reply. Never put the document's title, the subject or "
        "the message into a shell command: read them from mail.json."
        % (owner_name, one_line(title), folder, branch, base, ", ".join(files),
           time.strftime("%Y-%m-%d %H:%M", time.localtime(received)), message(subject, body, default_subject),
           ", display name" if display_name_checked else "", owner_name, owner_name))


def spawn(folder, name, cli, text, timeout=240):
    """cli "auto": agent-spawn picks the CLI and login by remaining usage (--account auto)."""
    exe = Path(__file__).resolve().parent.parent / "bin" / "agent-spawn"
    how = ["--account", "auto"] if cli == "auto" else ["--cli", cli]
    r = subprocess.run([str(exe), "--dir", str(folder)] + how + ["--name", name, "--brief-text", text, "--json"],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:   # its reason is the last line it printed, after any warnings
        raise RuntimeError("agent-spawn exit %d: %s" % (r.returncode, (r.stderr or r.stdout).strip()[-300:]))
    out = r.stdout
    return json.loads(out[out.index("{"):])


def handle(svc, cfg, inbound_id, msg, from_name, from_addr, subject, auth, dry_run=False, preflight=None):
    """Returns (status, detail). status is "started", "rejected" (quiet hold), "failed" (hold and
    alert) or "dry-run"."""
    rcfg = cfg["remarkable"]
    ok, why = check(msg, from_name, from_addr, auth, rcfg)
    if not ok:
        return "rejected", "reMarkable: " + why
    names = titles(subject, msg)
    title, body, default_subject = names[0], typed_body(msg), subject_is_default(subject, msg)
    if dry_run:
        typed = ["" if default_subject else "subject %r" % subject, "body %r" % body[:200] if body else ""]
        return "dry-run", "would start a reMarkable agent for %r; typed message: %s" % (
            title, ", ".join(t for t in typed if t) or "none")
    folder = name = wt = None
    try:
        if not rcfg.get("repo"):
            raise RuntimeError("config remarkable.repo is not set (the inbox repo whose AGENTS.md the agent follows)")
        repo = Path(os.path.expanduser(rcfg["repo"]))
        worktrees = worktrees_dir(rcfg)
        if not (repo / ".git").exists():
            raise RuntimeError("reMarkable inbox repo missing: %s" % repo)
        name = "rm-%d-%s" % (inbound_id, slug(title, 24))
        if commands.tmux_has(name):
            # an earlier attempt for this mail launched it (agent-spawn would refuse the name anyway)
            earlier = folders_for(worktrees, inbound_id)
            return "failed", ("tmux session %s already exists from an earlier attempt%s; nothing started. If that "
                              "agent is not working on this document, end it (`tmux kill-session -t %s`) and replay; "
                              "the replay gets a fresh worktree and leaves the old one for you to remove"
                              % (name, (" (folder %s)" % ", ".join(map(str, earlier))) if earlier else "", name))
        received = int(msg.get("internalDate", 0)) / 1000 or time.time()
        wt = new_worktree(repo, worktrees, inbound_id, title)
        folder = new_folder(wt[0] / "inbox", inbound_id, title, received)
        files = save_pdfs(svc, msg, folder)
        cli = rcfg["cli"]
        pre = ""
        if cfg.get("spawn_route") == "auto" and cli == "claude":
            cli = "auto"   # routed by usage: agent-usage already knows which logins are out, so no preflight
        elif cli == "claude":
            up, pre = (preflight or claude_available)(cfg)
            if not up and rcfg.get("fallback_cli"):
                cli = rcfg["fallback_cli"]
        (folder / "mail.json").write_text(json.dumps({
            "inbound_id": inbound_id, "gmail_id": msg.get("id"), "subject": subject, "title": title,
            "titles": names, "subject_is_default": default_subject, "body": body,
            "from": "%s <%s>" % (from_name, from_addr), "received": received, "files": files,
            "checks": {"address": True, "dkim": auth.get("dkim", []), "display_name": from_name},
            "cli": cli, "claude_preflight": pre, "worktree": str(wt[0]), "branch": wt[1], "base": wt[2],
            "base_warning": wt[3]},
            indent=2) + "\n")
        plan = spawn(folder, name, cli, brief(title, folder, files, received, wt[1], stale=bool(wt[3]),
                                              subject=subject, body=body, default_subject=default_subject,
                                              owner_name=cfg["owner_name"],
                                              display_name_checked=bool((rcfg.get("display_name") or "").strip())))
        if cli == "auto":
            cli = plan.get("cli") or "claude"
            pre = "routed: %s" % (plan.get("route") or "default")
            mail = json.loads((folder / "mail.json").read_text())
            mail.update(cli=cli, account=plan.get("account"), route=plan.get("route"))
            (folder / "mail.json").write_text(json.dumps(mail, indent=2) + "\n")
    except Exception as e:
        detail = "reMarkable agent did not start: %s: %s" % (type(e).__name__, e)
        if wt is not None and commands.tmux_has(name):
            # absent before this attempt, so agent-spawn started it here (then timed out or printed
            # no JSON): the agent may be working in this folder, so keep it
            return "failed", "%s. tmux session %s exists and the agent may be running in %s; check it " \
                             "before replaying (a replay needs that session ended, and gets a fresh worktree)" % (
                                 detail, name, folder)
        if wt is not None:   # nothing was launched: a replay starts clean; the PDF is still in Gmail
            left = worktree.drop(repo, wt[0], wt[1])
            if left:
                detail += ". Could not remove %s; remove by hand (a replay is not blocked)" % "; ".join(left)
        return "failed", detail
    if pre.startswith("routed"):
        note = " (account %s; %s)" % (plan.get("account") or "-", pre)
    else:
        note = "" if cli == rcfg["cli"] else " (fallback; Claude preflight: %s)" % pre
    warn = ". Warning: %s" % wt[3] if wt[3] else ""
    return "started", "reMarkable agent %s (%s %s, effort %s)%s in %s%s" % (
        plan.get("name"), cli, plan.get("model"), plan.get("effort"), note, folder, warn)


def record(svc, st, cfg, inbound_id, gid, status, detail, from_addr, subject):
    """Store and label the outcome the same way for the daemon and `agent-mail remarkable N`."""
    from . import alerts
    action = "delivered" if status == "started" else "held"
    mailroom.set_state(svc, st, cfg, inbound_id, gid, action, True, match_rule="remarkable", detail=detail)
    if status == "started":
        return
    if status == "failed":
        alerts.send_alert(svc, st, cfg, "remarkable", gid, "reMarkable document #%d: agent did not start" % inbound_id,
                          "From %s, subject %r.\n%s\n\nThe mail is held. Fix the cause, then `agent-mail remarkable %d`."
                          % (from_addr, subject, detail, inbound_id))
