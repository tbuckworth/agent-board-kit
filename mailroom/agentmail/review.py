"""Pre-send review of agent mail: is it readable by someone with no context?

`agent-mail send`/`reply` call `gate()` before composing a mail. A cheap model with a fresh context
(`claude -p`, no tools, no MCP, no settings, no CLAUDE.md) reads only the email as its reader would see
it, classifies it, and either passes it or returns concrete fixes. Mail to the owner is held to the
owner's rules (review_prompt.md, email_rules.md); mail to collaborators and other people to clarity rules
(review_prompt_outside.md), because the owner and the collaborator must be able to follow it later. One
pushback per email at most: the next send from the same session to the same kind of reader after a
`revise` always goes out. A credential in a mail holds it every time, model or not. The model runs on
each ready Claude login in the usage order, then on the optional API-key fallback
(orchestrator_fallback_secret); every other failure (no claude binary, timeout, bad JSON, every login
failing) passes the mail unreviewed.
Deterministic checks (`lint`) run first and need no model.
"""
import html as htmllib
import json
import os
import re
import subprocess
import time
from html.parser import HTMLParser
from pathlib import Path

from . import config, orchestrate, registry, usage
from .compose import text_to_html
from .config import STATE_DIR

PROMPT_PATH = Path(__file__).with_name("review_prompt.md")
OUTSIDE_PROMPT_PATH = Path(__file__).with_name("review_prompt_outside.md")
RULES_PATH = Path(__file__).with_name("email_rules.md")
EMPTY_MCP = STATE_DIR / "no-mcp.json"
SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "summary_for_owner": {"type": "string"},
        "predicted_reply": {"type": "string"},
        "issues": {"type": "array", "items": {
            "type": "object",
            "properties": {"fix": {"type": "string"}, "severity": {"type": "string", "enum": ["blocker", "polish"]}},
            "required": ["fix", "severity"]}},
        "verdict": {"type": "string", "enum": ["pass", "revise"]},
    },
    "required": ["kind", "predicted_reply", "issues", "verdict"],
}
DEFAULTS = {
    "enabled": True,
    "model": "haiku",
    "timeout": 40,
    # Mails shorter than this many words skip the model ("done", "blocked on X", a yes/no question).
    "min_words": 120,
    # Minutes after a `revise` before the mailroom sends the original draft anyway.
    "fallback_minutes": 30,
    # "enforce" holds a mail that needs work; "shadow" reviews and records but always sends.
    "mode": "enforce",
    # Which mail a blocker holds: "all", or a list of kinds plus any mail of at least hold_min_words. Blockers
    # in other mail go back to the agent as tips after it is sent (verdict "advised"). The lighter setting
    # ["results_report", "literature_review", "recommendation", "plan_or_proposal"] with 300 words held, in the
    # evaluation this reviewer was tuned on, 59% of the mails that had drawn a complaint instead of 93%, and about
    # 30% of all mail instead of 55%.
    "hold_kinds": "all",
    "hold_min_words": 300,
    # Also review mail to collaborators and other people (clarity and jargon, not visuals). With hold_outside
    # false their blockers come back to the agent as tips after the mail is sent ("advised") instead of holding it.
    "outside": True,
    "hold_outside": True,
    # A few sentences on who the owner is and what they already know (their field, projects, collaborators,
    # their own apps), added to both review prompts so the reviewer does not ask for those to be defined.
    "owner_context": "",
}
KINDS = ("results_report", "literature_review", "recommendation", "status_update", "decision_request", "done_note",
         "plan_or_proposal", "system_notice", "answer", "data_handover", "request", "coordination", "other")
# Credentials that must never be mailed: (label, pattern). Only a prefix of a match is ever printed or stored.
SECRET_PATTERNS = (
    ("an Anthropic or OpenAI API key", r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}"),
    ("a GitHub token", r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}"),
    ("an AWS access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("a Hugging Face token", r"\bhf_[A-Za-z0-9]{30,}"),
    ("a Google API key or OAuth token", r"\bAIza[0-9A-Za-z_-]{35}\b|\bya29\.[0-9A-Za-z_-]{20,}"),
    ("a Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("a private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def settings(cfg):
    return dict(DEFAULTS, **(cfg.get("review") or {}))


class _Render(HTMLParser):
    """HTML to the plain text a reader sees, keeping what matters for review: headings, list items,
    table rows, link targets, and where each image sits."""

    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol",
             "blockquote", "pre", "hr", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.href, self.cells = [], 0, [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("style", "script", "head", "title"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self.out.append("#" * int(tag[1]) + " ")
            elif tag == "li":
                self.out.append("- ")
        if tag == "table":
            self.out.append("[TABLE]\n")
        if tag == "tr":
            self.cells = []
        elif tag in ("td", "th") and self.cells is not None:
            self.out.append(" | ")
        elif tag == "a":
            self.href.append(a.get("href") or "")
        elif tag == "img":
            src = a.get("src") or ""
            kind = ("embedded" if src.startswith("cid:") else "DATA-URI (Gmail strips these: reader sees nothing)"
                    if src.startswith("data:") else "LOCAL FILE (reader sees nothing)"
                    if src.startswith(("file:", "/", "~")) else "remote")
            self.out.append("\n[IMAGE %s: %s]\n" % (kind, (a.get("alt") or a.get("title") or src)[:120]))

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head", "title"):
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.href:
            href = self.href.pop()
            if href and not href.startswith("mailto:"):
                self.out.append(" <%s>" % href[:200])
        elif tag in self.BLOCK:
            self.out.append("\n[/TABLE]\n" if tag == "table" else "\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"\s+", " ", data))


def render(text, html_body=None):
    """What the reviewer reads: the HTML part Gmail shows (agent-mail renders one from the text when the
    agent gave none), flattened to text with [IMAGE ...] markers and [TABLE] blocks."""
    p = _Render()
    p.feed(html_body or text_to_html(text))
    p.close()
    t = "".join(p.out)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n[ \t]+", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def words(text):
    return len(re.findall(r"\w+", text))


_PATH = re.compile(r"(?<![\w<])(?:~/|/media/|/home/|/tmp/|/mnt/|/ephemeral/)[\w./-]+")


def lint(text, html_body=None, images=()):
    """Model-free problems that are wrong whatever the email is about: each one holds the mail (round 1)."""
    out = []
    raw = html_body or ""
    if re.search(r"""(?i)<img[^>]+src=["']data:""", raw):
        out.append("An image uses a data: URI. Gmail strips these, so the reader sees a blank. Embed it with "
                   "--image NAME=PATH and <img src=\"cid:NAME\">.")
    if re.search(r"""(?i)<img[^>]+src=["'](?:file:|/|~)""", raw):
        out.append("An image points at a file on this machine, which the reader cannot see. Embed it with "
                   "--image NAME=PATH and <img src=\"cid:NAME\">.")
    cids = set(re.findall(r"""(?i)src=["']cid:([^"'>\s]+)""", raw))
    missing = sorted(cids - {n for n, _ in images})
    if missing:
        out.append("The HTML shows cid:%s but no --image of that name was given, so the reader sees a broken image."
                   % ", cid:".join(missing))
    return out


def secrets(text, html_body=None):
    """Credentials in the mail. These hold it on every round and in every mode; only --no-review sends it."""
    out = []
    for label, pat in SECRET_PATTERNS:
        m = re.search(pat, (text or "") + "\n" + (html_body or ""))
        if m:
            out.append("Contains what looks like %s ('%s...'). Never mail credentials: remove it. If it is not a "
                       "secret, send with --no-review \"why\"." % (label, m.group(0)[:6]))
    return out


def hints(text, html_body=None, images=()):
    """Model-free observations passed to the reviewer, which decides whether they matter here."""
    out = []
    shown = render(text, html_body)
    paths = sorted(set(_PATH.findall(shown)))
    if paths:
        out.append("Mentions paths on this machine (%s); the reader, often on a phone, cannot open them."
                   % ", ".join(paths[:3]))
    if not images and re.search(r"(?i)\b(attached|(see|in) the (plot|figure|chart|graph|image)s?|"
                                r"(plot|figure|chart|graph)s? (below|above))\b", shown):
        out.append("Refers to a plot/figure/attachment, but no image is embedded in the mail.")
    outside = re.sub(r"(?s)\[TABLE\].*?\[/TABLE\]", "", shown)
    if len(re.findall(r"(?m)^.*\S\s*\|\s*\S.*\|", outside)) >= 2:
        out.append("Has pipe-separated rows that are not an HTML table: Gmail shows them as a jumble of | "
                   "characters. Send a real <table> with --html.")
    # agent-mail renders markdown in plain text itself; in custom HTML, look outside code, styles and tables
    raw_text = re.sub(r"(?is)<(pre|code|style|script|table)\b.*?</\1>", "", html_body or "")
    raw_text = re.sub(r"(?i)</?(p|div|br|li|ul|ol|h[1-6]|tr|td|th|blockquote|section|body)\b[^>]*>", "\n", raw_text)
    raw_text = re.sub(r"<[^>]+>", "", raw_text)   # inline tags (<b>, <a>, <span>) do not start a line
    if re.search(r"(?m)\*\*\w|^[ \t]*#{1,4} \w", raw_text):
        out.append("Raw markdown markers (** or #) show literally in this HTML.")
    if re.search(r"(?i)\b(about|cost|costs|total|spent|of|at|for|is|was|=)\s{2,}(of|per|a|in|and|for|\w+\b)", text) \
            or re.search(r"\$\$\d|\(\s*\)|\s+,", text):
        out.append("Possible text eaten by the shell (an unquoted heredoc or double quotes turn $6,037 into ',037' "
                   "or nothing): look for a missing amount or name.")
    return out


def _claude_cmd(cfg, system_prompt):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not EMPTY_MCP.exists():
        EMPTY_MCP.write_text('{"mcpServers": {}}\n')
    cl = registry.claude_bin()
    if not cl:
        raise RuntimeError("claude binary not found")
    return [cl, "-p", "--output-format", "json", "--model", settings(cfg)["model"], "--tools", "",
            "--mcp-config", str(EMPTY_MCP), "--strict-mcp-config", "--setting-sources", "",
            "--no-session-persistence", "--system-prompt", system_prompt,
            "--json-schema", json.dumps(SCHEMA)]


def _call(cmd, msg, cwd, timeout, env=None):
    """One claude -p run. Returns its JSON result; raises RuntimeError when the run itself failed."""
    proc = subprocess.run(cmd, input=msg, capture_output=True, text=True, timeout=timeout, cwd=str(cwd), env=env)
    out = proc.stdout
    try:
        j = json.loads(out[out.find("{"):out.rfind("}") + 1])
    except ValueError:
        j = {}
    if proc.returncode != 0 or j.get("is_error") or not j:
        raise RuntimeError((j.get("result") or proc.stderr or out or "exit %d" % proc.returncode).strip()[:200])
    return j


def logins(cfg, cmd):
    """(name, command, env) for each way to run the reviewer: each ready Claude login in the usage order
    (e.g. a personal login, then a work seat), then the optional API key from Bitwarden
    (orchestrate.fallback_command: the key goes from Bitwarden to claude only)."""
    s = usage.settings(cfg)
    out = []
    for t in usage.ranked(s):
        acct = usage.account(s, t[len("claude:"):]) if t.startswith("claude:") else None
        if acct is not None and usage.account_ready(acct)[0]:
            out.append((t[len("claude:"):], cmd, usage.claude_env(acct)))
    if orchestrate.fallback_available(cfg):
        out.append(("api-key", orchestrate.fallback_command(cfg, cmd),
                    orchestrate._fallback_env(cfg, usage.claude_env({}))))
    return out


def ask_model(cfg, subject, shown, notes=(), reader=None):
    """One fresh model call on the email alone. Returns (dict, info); raises on any failure. `reader` is who
    receives a mail that is not to the owner (the outside prompt is used). A login that fails (usage limit,
    logged out) passes the call to the next one in logins(); a timeout ends it."""
    prompt = (OUTSIDE_PROMPT_PATH if reader else PROMPT_PATH).read_text()
    prompt = prompt.replace("{{OWNER_CONTEXT}}", settings(cfg)["owner_context"]).replace("{{OWNER_NAME}}",
                                                                                        config.owner_name(cfg))
    msg = "Subject: %s\n\n%s" % (subject, shown)
    if reader:
        msg = "To: %s\n%s" % (reader, msg)
    if notes:
        msg += "\n\n[Automatic checks already found:]\n" + "\n".join("- " + n for n in notes)
    cwd = STATE_DIR / "review-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    cmd, timeout = _claude_cmd(cfg, prompt), settings(cfg)["timeout"]
    t0 = time.time()
    info, errors, j = {}, [], None
    for i, (name, c, env) in enumerate(logins(cfg, cmd) or [("default", cmd, None)]):
        try:
            j = _call(c, msg, cwd, timeout, env)
        except RuntimeError as e:
            errors.append("%s: %s" % (name, e))
            continue
        info = {"auth": name, "fallback": i > 0}
        break
    if j is None:
        raise RuntimeError("; ".join(errors)[:300])
    if errors:
        info["failed"] = "; ".join(errors)[:300]
    info["seconds"] = round(time.time() - t0, 1)
    info["cost"] = j.get("total_cost_usd")
    res = j.get("structured_output")
    if not isinstance(res, dict):
        res = json.loads(re.search(r"\{.*\}", j.get("result") or "", re.S).group(0))
    if res.get("verdict") not in ("pass", "revise"):
        raise ValueError("no verdict")
    issues = [i for i in res.get("issues") or [] if isinstance(i, dict) and i.get("fix")]
    res["blockers"] = [i["fix"] for i in issues if i.get("severity") == "blocker"]
    res["polish"] = [i["fix"] for i in issues if i.get("severity") != "blocker"]
    # the verdict follows the severities, whatever the model wrote in `verdict`
    res["verdict"] = "revise" if res["blockers"] else "pass"
    return res, info


NOT_SENT = "agent-mail: NOT SENT"
EXPIRED_NOTE = ("[Sent unchanged by the mailroom: the pre-send review asked the agent for changes and it did not "
                "revise or withdraw this mail within %d minutes.]")


def prepend_note(html_body, note):
    """The note as the first paragraph of the HTML, or None to have the HTML rendered from the text."""
    if not html_body:
        return None
    para = '<p style="color:#6e7781;font-size:13px">%s</p>\n' % htmllib.escape(note)
    m = re.search(r"(?i)<body[^>]*>", html_body)
    return html_body[:m.end()] + para + html_body[m.end():] if m else para + html_body


def owner_addresses(cfg):
    return set(cfg.get("allowlist") or [])


def addresses(*recipients):
    return {a.strip().lower() for r in recipients if r for a in re.findall(r"[\w.+-]+@[\w.-]+", r)}


def to_owner(cfg, *recipients):
    return bool(addresses(*recipients) & owner_addresses(cfg))


def audience(cfg, *recipients):
    """('owner', None) when the owner is a recipient; ('collaborator', reader) when every recipient is in a
    collaborator's team; ('other', reader) for anyone else; (None, None) with no address."""
    addrs = addresses(*recipients)
    if not addrs:
        return None, None
    owner = config.owner_name(cfg)
    if addrs & owner_addresses(cfg):
        return "owner", None
    for addr, c in (cfg.get("collaborators") or {}).items():
        team = {addr.lower()} | {x.lower() for x in (c or {}).get("share_data_with") or []}
        if addrs <= team:
            who = c.get("owner") or addr
            return "collaborator", ("%s's team (%s): %s, a collaborator of %s, and their AI agent. %s and %s must "
                                    "both be able to follow this later." % (who, ", ".join(sorted(addrs)), who, owner,
                                                                          owner, who))
    return "other", ("%s: a person %s asked their agent to write to, who may never have had mail from it."
                     % (", ".join(sorted(addrs)), owner))


def gate(cfg, st, tag, recipients, subject, text, html_body=None, images=(), no_review=None, draft=None):
    """Decide whether this mail goes out now. Returns (review_id or None, stop, tips): `stop` is the NOT SENT
    message for the agent (print it, exit 3, send nothing); `tips` are polish notes to print after sending.
    Every decision is a row in `reviews`."""
    s = settings(cfg)
    aud, reader = audience(cfg, *recipients)
    if not s["enabled"] or not tag or aud is None or (aud != "owner" and not s.get("outside", True)):
        return None, None, None
    to = config.owner_name(cfg) if aud == "owner" else "the recipient"
    last = st.latest_review(tag, aud)
    round_ = 2 if (last is not None and last["verdict"] == "revise" and last["draft"]
                   and (last["audience"] or "owner") == aud) else 1
    if no_review:
        return st.record_review(tag, round_, "override", subject, reasons=str(no_review)[:300], audience=aud), None, None
    leaks = secrets(text, html_body)
    if leaks:  # every round, every mode, and no draft kept for the watchdog to send
        st.record_review(tag, round_, "revise", subject, kind="secret", reasons="; ".join(leaks)[:300], audience=aud)
        return None, pushback("secret", leaks, [], None, s["fallback_minutes"], to, keep_draft=False), None
    shown = render(text, html_body)
    n = words(shown)
    notes = lint(text, html_body, images)
    if n < s["min_words"] and not notes:
        return st.record_review(tag, round_, "skipped", subject, kind="short", words=n, audience=aud), None, None
    if round_ == 2 and not s.get("review_round2", True):
        return st.record_review(tag, round_, "skipped", subject, kind="round2", words=n, audience=aud), None, None
    extra = hints(text, html_body, images)
    recent = st.con.execute("select count(*) from sent where tag=? and sent_at>?", (tag, int(time.time()) - 3600)).fetchone()[0]
    if recent >= 2 and aud == "owner":
        extra.append("This would be mail %d from this agent to %s within the hour: unless it is urgent, it should "
                     "be one consolidated mail that restates the whole current picture." % (recent + 1, to))
    try:
        res, info = ask_model(cfg, subject, shown, notes + extra, reader=reader)
    except Exception as e:  # fail open: a review must never cost the owner a mail
        if not notes:
            return st.record_review(tag, round_, "error", subject, reasons=("%s: %s" % (type(e).__name__, e))[:300],
                                    words=n, model=s["model"], audience=aud), None, None
        res, info = {"kind": "unknown", "blockers": [], "polish": []}, {}
    blockers = notes + [f for f in res.get("blockers") or [] if f not in notes]
    polish = list(res.get("polish") or [])
    kind = next((k for k in KINDS if (res.get("kind") or "").startswith(k)), "other")
    holds = s["hold_kinds"] == "all" or kind in s["hold_kinds"] or n >= s["hold_min_words"] or bool(notes)
    if aud != "owner" and not s.get("hold_outside", True):
        holds = bool(notes)
    verdict = "pass" if not blockers else "revise" if holds else "advised"
    auth = info.get("auth")
    model = s["model"] + ((" (api key)" if auth == "api-key" else " (%s login)" % auth) if info.get("fallback") else "")
    row = dict(kind=kind, words=n, model=model, seconds=info.get("seconds"), cost=info.get("cost"), audience=aud,
               fixes=json.dumps({"blockers": blockers, "polish": polish,
                                 "predicted_reply": res.get("predicted_reply")}))
    if verdict != "revise" or round_ == 2 or s["mode"] != "enforce":
        # round 2 and shadow mode: recorded for measurement, sent regardless
        rid = st.record_review(tag, round_, verdict, subject, reasons=(res.get("predicted_reply") or "")[:300], **row)
        return rid, None, tips(blockers + polish, to) if round_ == 1 or blockers else None
    st.record_review(tag, round_, "revise", subject, reasons="; ".join(blockers)[:300], draft=json.dumps(draft or {}),
                     **row)
    return None, pushback(kind, blockers, polish, res.get("predicted_reply"), s["fallback_minutes"], to), None


def tips(fixes, to="the owner"):
    if not fixes:
        return None
    return "\n".join(["Pre-send review, for your next mail to %s (this one was sent):" % to] +
                     ["  - %s" % f for f in fixes[:5]])


def pushback(kind, blockers, polish, predicted_reply, fallback_minutes, to="the owner", keep_draft=True):
    if not keep_draft:  # a credential: nothing is kept, and the next send is checked again
        return "\n".join([NOT_SENT + ":"] + ["  %d. %s" % (i, f) for i, f in enumerate(blockers, 1)] +
                          ["", "Nothing was kept: fix it and run your send/reply command again."])
    who = "%s would and expects them" % to
    lines = [NOT_SENT + ": a reviewer with no context read it as %s to reply" % who,
             "  \"%s\"" % (predicted_reply or "with a question").strip().strip('"'),
             "instead of acting on it. Read as: %s." % (kind or "unknown"), "", "Fix before sending:"]
    lines += ["  %d. %s" % (i, f) for i, f in enumerate(blockers, 1)]
    if polish:
        lines += ["Also worth doing:"] + ["  - %s" % f for f in polish]
    lines += ["",
              "Revise and run your send/reply command again. The next mail from this session goes out whatever",
              "the review says (one pushback per mail). To send unchanged now: add --no-review \"why\".",
              "To drop the draft: agent-mail withdraw. Otherwise the mailroom sends this draft unchanged in "
              "%d min. Rules: agent-mail guide." % fallback_minutes]
    return "\n".join(lines)
