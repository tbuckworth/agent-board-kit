"""Pre-send review of agent mail: is it readable by someone with no context?

`agent-mail send`/`reply` call `gate()` before composing a mail. A cheap model with a fresh context
(`claude -p`, no tools, no MCP, no settings, no CLAUDE.md) reads only the email as its reader would see
it, and before a reply to the owner the owner's message that it answers; it classifies the email, and either
passes it or returns concrete fixes. Mail to the owner is held to the owner's rules (review_prompt.md,
email_rules.md); mail to collaborators and other people to clarity rules (review_prompt_outside.md), because
the owner and the collaborator must be able to follow it later. One pushback per email at most: the next send
from the same session to the same kind of reader after a `revise` always goes out. A credential in a mail
holds it every time, model or not. The model runs on each ready Claude login in usage.order, then on the
optional API-key fallback (orchestrator_fallback_secret; headless.logins, without Codex); every other failure
(no claude binary, timeout, bad JSON, every login failing) passes the mail unreviewed.
Deterministic checks (`lint`, and `layout` for a plain-text body) run first and need no model.
"""
import html as htmllib
import json
import os
import re
import time
from pathlib import Path

from . import accounts, headless, imagefile, usage
from .compose import MARKED, NOTE, cut_items, flatten, text_to_html
from .config import STATE_DIR

PROMPT_PATH = Path(__file__).with_name("review_prompt.md")
OUTSIDE_PROMPT_PATH = Path(__file__).with_name("review_prompt_outside.md")
RULES_PATH = Path(__file__).with_name("email_rules.md")
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
    "model": "haiku",    # the reviewer's model
    "timeout": 40,       # seconds per reviewer run
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
    # their own apps), added to every review prompt so the reviewer does not ask for those to be defined.
    "owner_context": "",
    # Review the mail sent after a `revise` too: it goes out whatever the verdict, and the review is recorded.
    "review_round2": True,
}
# How much of the message a reply answers the reviewer is shown, in characters, and the lines that frame it.
ANSWERED_MAX = 6000
ANSWERED = "[The message this email answers:]\n%s\n[End of that message. The email:]\n\n"
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


def _image_marker(attrs):
    """Where an image sits and whether the reader sees it: [IMAGE embedded|remote|DATA-URI ...|LOCAL FILE ...: alt]."""
    src = attrs.get("src") or ""
    kind = ("embedded" if src.startswith("cid:") else "DATA-URI (Gmail strips these: reader sees nothing)"
            if src.startswith("data:") else "LOCAL FILE (reader sees nothing)"
            if src.startswith(("file:", "/", "~")) else "remote")
    return "\n[IMAGE %s: %s]\n" % (kind, (attrs.get("alt") or attrs.get("title") or src)[:120])


def render(text, html_body=None):
    """What the reviewer reads: the HTML part Gmail shows (agent-mail renders one from the text when the
    agent gave none), flattened to text (compose.flatten) with headings, list items, link targets other than
    mailto: (but for a link whose words are its target, as agent-mail makes of a bare address, which is read once),
    [IMAGE ...] markers, [TABLE] blocks whose cells each start " | ", and [CODE] blocks, each a fenced block's lines
    one to a line, without their leading spaces, which the Mail tab gives a Copy button."""
    return flatten(html_body or text_to_html(text), image=_image_marker,
                   link=lambda href, words: " <%s>" % href[:200] if href and href != words
                   and not href.startswith("mailto:") else "",
                   mark_blocks=True, keep_pre=True, rule="")


def words(text):
    return len(re.findall(r"\w+", text))


_PATH = re.compile(r"(?<![\w<])(?:~/|/media/|/home/|/tmp/|/mnt/|/ephemeral/)[\w./-]+")


# The narrowest image that looks sharp in agent mail: a mail's column shows an image about 600 px across (compose's 42em
# body), on screens of 2 or 3 pixels to each px, so one under twice that is drawn blown up, and looks grainy.
MIN_IMAGE_WIDTH = 1200


def lint(text, html_body=None, images=(), *, owner_name):
    """Model-free problems that are wrong whatever the email is about: each one holds the mail (round 1). owner_name
    (config owner_name) is the reader the texts name. images are checked (outbound.check_image). An image under
    MIN_IMAGE_WIDTH is one, but a GIF (mostly a screen recording, too big to send at that width)."""
    out = []
    raw = html_body or ""
    if re.search(r"""(?i)<img[^>]+src=["']data:""", raw):
        out.append("An image uses a data: URI. Gmail strips these, so %s sees a blank. Embed it with "
                   "--image NAME=PATH and <img src=\"cid:NAME\">." % owner_name)
    if re.search(r"""(?i)<img[^>]+src=["'](?:file:|/|~)""", raw):
        out.append("An image points at a file on this machine, which %s cannot see. Embed it with "
                   "--image NAME=PATH and <img src=\"cid:NAME\">." % owner_name)
    for name, data in images:              # each checked by outbound.check_image, so its header gives its size
        width = imagefile.size(data)[0]
        if width < MIN_IMAGE_WIDTH and imagefile.subtype(data) != "gif":          # a GIF, a screen recording
            out.append("Image %s is %d px wide, so it looks grainy on %s's screens, which show it about 600 px across "
                       "at 2 or 3 screen pixels each. Make it at least %d px wide: matplotlib's savefig(dpi=200) or "
                       "more, a screenshot at 2x." % (name, width, owner_name, MIN_IMAGE_WIDTH))
    cids = set(re.findall(r"""(?i)src=["']cid:([^"'>\s]+)""", raw))
    missing = sorted(cids - {n for n, _ in images})
    if missing:
        out.append("The HTML shows cid:%s but no --image of that name was given, so %s sees a broken image."
                   % (", cid:".join(missing), owner_name))
    return out


# A plain-text body with no markers and a paragraph over the guide's ~80 words reads as a wall of text.
SOUP_WORDS, SOUP_PARAGRAPH = 150, 80
_URL = re.compile(r"(?i)(?:https?://|www\.)")
_LETTERED = re.compile(r"[a-z][.)]\s")   # "a. ..." / "b) ..." options, which agent-mail does not make a list


def _carries_on(before, line):
    """Whether `line` goes on with the sentence `before` broke off (a hard wrap), rather than starting a line on
    purpose: a row of a pipe or space-aligned table, a link, a lettered option."""
    if ("|" in before + line or re.search(r"\S {3,}\S", before) or re.search(r"\S {3,}\S", line)
            or _URL.match(line) or _LETTERED.match(line)):
        return False
    return before.endswith(",") or line[:1].islower()


def layout(text):
    """Faults in the HTML agent-mail renders from a plain-text body (no --html), which the reviewer cannot see:
    it reads that HTML flattened. Each holds the mail like a lint (round 1)."""
    text = text or ""
    body = text_to_html(text)
    paras = [[htmllib.unescape(re.sub(r"<[^>]+>", "", line)).strip() for line in p.split("<br>\n")]
             for p in re.findall(r"(?s)<p>(.*?)</p>", body)]
    out = []
    n, longest = len(text.split()), max((len(" ".join(p).split()) for p in paras), default=0)
    if n >= SOUP_WORDS and longest > SOUP_PARAGRAPH and not re.search(r"<(?:li|h\d|pre|b)\b", body):
        out.append("This %d-word plain-text mail has no bullets, bold, headings or fenced block, and a paragraph of %d "
                   "words, so it arrives as a wall of bare paragraphs. Give it structure: a **bold** lead-in or # heading "
                   "per part, - bullets for lists, paragraphs under about 80 words; or send it as --html."
                   % (n, longest))
    split = [after for before, after in cut_items(text) if _carries_on(before, after)]
    if split:
        out.append("A list item goes on to a line that starts flush left (\"%s\"%s), which ends the list and leaves the "
                   "rest of the sentence as a separate paragraph. Write each item as one unwrapped line, or indent its "
                   "continuation lines by two spaces."
                   % (split[0][:60], ", and %d more" % (len(split) - 1) if len(split) > 1 else ""))
    wraps = [(a, b) for p in paras for a, b in zip(p, p[1:]) if _carries_on(a, b)]
    if len(wraps) > 3:
        out.append("The text is hard-wrapped: %d line breaks fall mid-sentence (\"...%s / %s...\"). agent-mail keeps every "
                   "line break, so on a phone it reads as ragged short lines. Write each paragraph as one line."
                   % (len(wraps), wraps[0][0][-30:], wraps[0][1][:30]))
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


def hints(text, html_body=None, images=(), *, owner_name):
    """Model-free observations passed to the reviewer, which decides whether they matter here. owner_name (config
    owner_name) is the reader the texts name."""
    out = []
    shown = render(text, html_body)
    paths = sorted(set(_PATH.findall(shown)))
    if paths:
        out.append("Mentions paths on this machine (%s); %s reads mail on a phone and cannot open them."
                   % (", ".join(paths[:3]), owner_name))
    if not images and re.search(r"(?i)\b(attached|(see|in) the (plot|figure|chart|graph|image)s?|"
                                r"(plot|figure|chart|graph)s? (below|above))\b", shown):
        out.append("Refers to a plot/figure/attachment, but no image is embedded in the mail.")
    outside = re.sub(r"(?s)\[(%s)\].*?\[/\1\]" % "|".join(MARKED.values()), "", shown)   # a pipeline in a block is no table
    if len(re.findall(r"(?m)^.*\S\s*\|\s*\S.*\|", outside)) >= 2:
        out.append("Has pipe-separated rows that did not render as a table: Gmail shows them as a jumble of | "
                   "characters. Write a GitHub-style pipe table (a header row, then a |---|---| row) or send a "
                   "real <table> with --html.")
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


def collaborator(addr, entry):
    """Who config collaborators entry `entry` (for address addr) stands for in the review texts: its owner, else
    the address."""
    return entry.get("owner") or addr


def read_prompt(path, cfg):
    """A prompt or rules file (PROMPT_PATH, OUTSIDE_PROMPT_PATH, RULES_PATH) with config owner_name put in for its
    {owner_name}, and the people config collaborators stand for (collaborator), or "none configured", for its
    {collaborators}. Reads the file; a literal brace in one is written twice."""
    people = sorted({collaborator(addr, c) for addr, c in cfg["collaborators"].items()})
    context = (settings(cfg)["owner_context"] or "").strip()
    return path.read_text().format(owner_name=cfg["owner_name"], collaborators=", ".join(people) or "none configured",
                                   owner_context=("\n" + context + "\n") if context else "")


def _claude_cmd(cfg, system_prompt):
    """The reviewer's claude command: the review model, no tools, settings or session file, its system prompt and
    the verdict's JSON schema. Raises RuntimeError when there is no claude binary."""
    return headless.claude_command(settings(cfg)["model"], None, [
        "--tools", "", "--setting-sources", "", "--no-session-persistence", "--system-prompt", system_prompt,
        "--json-schema", json.dumps(SCHEMA)])


def ask_model(cfg, subject, shown, notes=(), reader=None, answers=None):
    """One fresh model call on the email, as a `claude -p` subprocess. Returns (dict, info); raises on any
    failure. `reader` is who receives a mail that is not to the owner (the outside prompt is used). `answers` is the text
    of the message the email replies to, shown before it (its first ANSWERED_MAX characters); None or blank shows
    the email alone. A login that fails (usage limit, logged out) passes the call to the next one in
    headless.logins; a timeout ends it."""
    prompt = read_prompt(OUTSIDE_PROMPT_PATH if reader else PROMPT_PATH, cfg)
    msg = "Subject: %s\n\n%s" % (subject, shown)
    if reader:
        msg = "To: %s\n%s" % (reader, msg)
    if (answers or "").strip():
        msg = ANSWERED % answers.strip()[:ANSWERED_MAX] + msg
    if notes:
        msg += "\n\n[Automatic checks already found:]\n" + "\n".join("- " + n for n in notes)
    cwd = STATE_DIR / "review-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    cmd, timeout = _claude_cmd(cfg, prompt), settings(cfg)["timeout"]
    t0 = time.time()
    errors = []
    for name, c, env in (headless.logins(cfg, cmd, os.environ, None)
                         or [("default", cmd, accounts.claude_env({}, os.environ))]):
        j, err = headless.run(c, msg, cwd, env, timeout, headless.result)
        if not err:
            break
        errors.append("%s: %s" % (name, err[:200]))
    else:
        raise RuntimeError("; ".join(errors)[:300])
    info = {"auth": name}
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
# agent-board's agentboard/mailforward.py strips this from forwarded mail: change both together.
EXPIRED_NOTE = ("[Sent unchanged by the mailroom: the pre-send review asked the agent for changes and it did not "
                "revise or withdraw this mail within %d minutes.]")


def prepend_note(html_body, note):
    """The note as the first paragraph of the HTML, or None to have the HTML rendered from the text."""
    if not html_body:
        return None
    para = NOTE % htmllib.escape(note)
    m = re.search(r"(?i)<body[^>]*>", html_body)
    return html_body[:m.end()] + para + html_body[m.end():] if m else para + html_body


def owner_addresses(cfg):
    return set(cfg["allowlist"])


def addresses(*recipients):
    return {a.strip().lower() for r in recipients if r for a in re.findall(r"[\w.+-]+@[\w.-]+", r)}


def audience(cfg, *recipients):
    """('owner', None) when the owner is a recipient; ('collaborator', reader) when every recipient is in a
    collaborator's team; ('other', reader) for anyone else; (None, None) with no address."""
    addrs, owner = addresses(*recipients), cfg["owner_name"]
    if not addrs:
        return None, None
    if addrs & owner_addresses(cfg):
        return "owner", None
    for addr, c in cfg["collaborators"].items():
        team = {addr.lower()} | {x.lower() for x in (c or {}).get("share_data_with") or []}
        if addrs <= team:
            who = collaborator(addr, c)
            return "collaborator", ("%s's team (%s): %s, a collaborator of %s, and their AI agent. %s and %s must "
                                    "both be able to follow this later." % (who, ", ".join(sorted(addrs)), who, owner,
                                                                          owner, who))
    return "other", ("%s: a person %s asked this agent to write to, who may never have had mail from it."
                     % (", ".join(sorted(addrs)), owner))


def gate(cfg, st, tag, recipients, subject, text, html_body=None, images=(), no_review=None, draft=None, answers=None,
         fast=False):
    """Decide whether this mail goes out now. Returns (review_id or None, stop, tips): `stop` is the NOT SENT
    message for the agent (print it, exit 3, send nothing); `tips` are polish notes to print after sending.
    `answers` is the text of the message this mail replies to (None: not a reply); the reviewer of mail to the owner is
    shown it, the reviewer of other mail is not. fast: the mail is a reply in a thread where fast mode is on for its
    session (agentmail/fastmode.py), which skips every check but the one for credentials. Every decision is a row in
    `reviews`."""
    s = settings(cfg)
    aud, reader = audience(cfg, *recipients)
    if not s["enabled"] or not tag or aud is None or (aud != "owner" and not s["outside"]):
        return None, None, None
    to = cfg["owner_name"] if aud == "owner" else "the recipient"
    last = st.latest_review(tag, aud)
    round_ = 2 if (last is not None and last["verdict"] == "revise" and last["draft"]
                   and (last["audience"] or "owner") == aud) else 1
    if no_review:
        return st.record_review(tag, round_, "override", subject, reasons=str(no_review)[:300], audience=aud), None, None
    leaks = secrets(text, html_body)
    if leaks:  # every round, every mode, and no draft kept for the watchdog to send
        st.record_review(tag, round_, "revise", subject, kind="secret", reasons="; ".join(leaks)[:300], audience=aud)
        return None, pushback("secret", leaks, [], None, s["fallback_minutes"], to, keep_draft=False,
                              reviewer_error=None), None
    shown = render(text, html_body)
    n = words(shown)
    if fast:
        return st.record_review(tag, round_, "skipped", subject, kind="fast", words=n, audience=aud), None, None
    notes = lint(text, html_body, images, owner_name=cfg["owner_name"])
    if aud == "owner" and not html_body and n >= s["min_words"]:
        notes += layout(text)
    if n < s["min_words"] and not notes:
        return st.record_review(tag, round_, "skipped", subject, kind="short", words=n, audience=aud), None, None
    if round_ == 2 and not s["review_round2"]:
        return st.record_review(tag, round_, "skipped", subject, kind="round2", words=n, audience=aud), None, None
    extra = hints(text, html_body, images, owner_name=cfg["owner_name"])
    recent = st.sent_count_since(tag, time.time() - 3600)
    if recent >= 2 and aud == "owner":
        extra.append("This would be mail %d from this agent to %s within the hour: unless it is urgent, it should "
                     "be one consolidated mail that restates the whole current picture." % (recent + 1, to))
    error = None
    try:
        res, info = ask_model(cfg, subject, shown, notes + extra, reader=reader,
                              answers=answers if aud == "owner" else None)
    except Exception as e:  # fail open: a review must never cost the owner a mail
        error = "%s: %s" % (type(e).__name__, e)
        if not notes:
            return st.record_review(tag, round_, "error", subject, reasons=error[:300],
                                    words=n, model=s["model"], audience=aud), None, None
        # the automatic checks alone hold it
        res, info = {"kind": "unknown", "blockers": [], "polish": []}, {}
    blockers = notes + [f for f in res.get("blockers") or [] if f not in notes]
    polish = list(res.get("polish") or [])
    kind = next((k for k in KINDS if (res.get("kind") or "").startswith(k)), "other")
    holds = s["hold_kinds"] == "all" or kind in s["hold_kinds"] or n >= s["hold_min_words"] or bool(notes)
    if aud != "owner" and not s["hold_outside"]:
        holds = bool(notes)
    verdict = "pass" if not blockers else "revise" if holds else "advised"
    model = model_label(cfg, s["model"], info.get("auth"))
    fixes = {"blockers": blockers, "polish": polish, "predicted_reply": res.get("predicted_reply")}
    if error:
        fixes["reviewer_error"] = error
    row = dict(kind=kind, words=n, model=model, seconds=info.get("seconds"), cost=info.get("cost"), audience=aud,
               fixes=json.dumps(fixes))
    if verdict != "revise" or round_ == 2 or s["mode"] != "enforce":
        # round 2 and shadow mode: recorded for measurement, sent regardless
        rid = st.record_review(tag, round_, verdict, subject, reasons=(res.get("predicted_reply") or "")[:300], **row)
        return rid, None, tips(blockers + polish, to) if round_ == 1 or blockers else None
    st.record_review(tag, round_, "revise", subject, reasons="; ".join(blockers)[:300], draft=json.dumps(draft or {}),
                     **row)
    return None, pushback(kind, blockers, polish, res.get("predicted_reply"), s["fallback_minutes"], to,
                          reviewer_error=error), None


def model_label(cfg, model, auth):
    """The review row's model: `model`, and the way it ran (auth: a headless.logins name, None when it did not run)
    when that is not the first Claude login in usage.order: "MODEL (NAME login)", or "MODEL (api key)" for
    headless.API_KEY."""
    if auth == headless.API_KEY:
        return model + " (api key)"
    s = usage.settings(cfg)
    first = next((usage.account_of(t) for t in usage.ranked(s) if t != usage.CODEX), None)
    return "%s (%s login)" % (model, auth) if auth in s["claude_accounts"] and auth != first else model


def tips(fixes, to):
    """The polish notes printed after a mail to `to` (its reader's name, as gate words it) was sent, or None."""
    if not fixes:
        return None
    return "\n".join(["Pre-send review, for your next mail to %s (this one was sent):" % to] +
                     ["  - %s" % f for f in fixes[:5]])


def pushback(kind, blockers, polish, predicted_reply, fallback_minutes, to, keep_draft=True, *, reviewer_error):
    """The NOT SENT message for a held mail to `to` (its reader's name, as gate words it). `reviewer_error` says why
    the reviewer did not run (None: it ran); the automatic checks alone hold such a mail, and the message says so."""
    if not keep_draft:  # a credential: nothing is kept, and the next send is checked again
        return "\n".join([NOT_SENT + ":"] + ["  %d. %s" % (i, f) for i, f in enumerate(blockers, 1)] +
                          ["", "Nothing was kept: fix it and run your send/reply command again."])
    if reviewer_error:
        lines = [NOT_SENT + ": the automatic checks found the problems below. The reviewer could not run (%s),"
                 % reviewer_error[:200], "so nothing else was checked.", "", "Fix before sending:"]
    else:
        lines = [NOT_SENT + ": a reviewer with no context read it as %s would and expects them to reply" % to,
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
