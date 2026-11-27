"""Researcher controls for an existing conversation, addressed by the agent's stable mail routing tag."""
import json
import re
import sqlite3
from pathlib import Path
from shlex import quote
from typing import Optional

from . import config, deliver, researcher_payload, review, routing, watch
from .store import Store

RETRY_SECONDS = 60
PROMPT_PATH = Path(__file__).with_name("researcher_prompt.md")


def supported(session: Optional[sqlite3.Row]) -> bool:
    return session is not None and session["cli"] in config.CLIS


def check_routed_tag(st: Store, tag: str, source_host: Optional[str]) -> None:
    """Check that the tag belongs to a supported session on the requesting host, when given."""
    if not re.fullmatch(routing.TAG_RE, tag):
        raise ValueError("invalid mail routing tag")
    session = st.session_by_tag(tag)
    if not supported(session) or (source_host is not None and session["origin_host"] != source_host):
        raise ValueError("Researcher is unavailable for this routing tag")


def state(st: Store, tag: str, source_host: Optional[str]) -> dict:
    """Read the current preference, including the default revision, from the central store."""
    check_routed_tag(st, tag, source_host)
    mode = st.researcher_mode(tag)
    return {"on": bool(mode and mode["enabled"]), "revision": mode["revision"] if mode else 0,
            "applied": is_applied(mode)}


def is_applied(mode: Optional[sqlite3.Row]) -> bool:
    """An On acknowledgement counts only with a bound verified payload."""
    return bool(mode and mode["applied_at"] is not None and (not mode["enabled"] or mode["payload"]))


def request_receipt(st: Store, cfg: dict, tag: str, thread_id: str, source_host: Optional[str]) -> dict:
    """Read the sole outgoing request to the owner in a dedicated approval thread from the store."""
    check_routed_tag(st, tag, source_host)
    sent = st.sent_rows_in_thread(thread_id)
    if len(sent) != 1 or sent[0]["tag"] != tag:
        raise ValueError("expected one agent mail from this routing tag in the approval thread")
    request = sent[0]
    recipients = review.addresses(request["to_addr"], request["cc_addr"])
    if not recipients or not recipients <= review.owner_addresses(cfg):
        raise ValueError("the Researcher approval request must go to the owner alone")
    if not request["message_id"]:
        raise ValueError("the Researcher approval request has no recorded Message-ID")
    return {"thread_id": thread_id, "message_id": request["message_id"]}


def application(st: Store, tag: str, origin: tuple[str, str, str], revision: int,
                fingerprint: Optional[str]) -> sqlite3.Row:
    """Check that the acknowledgement names the current conversation, revision and payload."""
    session = st.session_by_tag(tag)
    if not supported(session) or origin != (session["origin_host"], session["cli"], session["session_id"]):
        raise ValueError("only the routed conversation may acknowledge its Researcher preference")
    mode = st.researcher_mode(tag)
    if mode is None or mode["revision"] != revision:
        raise ValueError("Researcher revision is no longer current")
    if mode["enabled"] and (not mode["payload"] or fingerprint != json.loads(mode["payload"])["fingerprint"]):
        raise ValueError("acknowledging On needs the verified Researcher payload from the current activation")
    return mode


def acknowledge(st: Store, cfg: dict, tag: str, origin: tuple[str, str, str], revision: int,
                fingerprint: Optional[str], now: float) -> dict:
    """Recheck the On payload on the receiving host, then record application of this still-current revision."""
    mode = application(st, tag, origin, revision, fingerprint)
    if mode["enabled"]:
        try:
            payload = researcher_payload.on_host(cfg, *origin)
            if payload.fingerprint != fingerprint:
                raise researcher_payload.Unavailable(
                    researcher_payload.FAILURE_PREFIX + ": installed files changed during activation; delivery will retry."
                )
        except researcher_payload.Unavailable as e:
            st.reject_researcher_payload(tag, revision, mode["payload"], str(e), now)
            raise
    with st.transaction():
        application(st, tag, origin, revision, fingerprint)
        if not st.acknowledge_researcher_mode(tag, revision, now):
            raise ValueError("Researcher revision is no longer current")
        return state(st, tag, origin[0])


def instruction(cfg: dict, session: sqlite3.Row, mode: sqlite3.Row, *, control: bool) -> str:
    """Build the revisioned instruction. When enabled, read the prompt file and use cfg's workflow for the CLI."""
    tag, revision, enabled = mode["tag"], mode["revision"], mode["enabled"]
    headline = "[Agent Mail Researcher %s for %s, revision %d]" % ("on" if enabled else "off", tag, revision)
    check = (
        "Run `agent-mail researcher-state -- %s` before acting on this mode. "
        "If its revision or state differs, ignore this notice's instructions and use the current preference for "
        "new work. Always handle the attached owner message, including an outstanding decision." % tag
        if not control else
        "Run `agent-mail researcher-state -- %s` before applying this control. "
        "If the revision or state differs or applied is true, discard this control." % tag
    )
    payload_arg = ""
    if enabled:
        if not mode["payload"]:
            return "\n".join((headline, check, "Researcher is waiting for a verified installed payload. "
                             "Handle the owner message normally and retain any pending decision while research waits."))
        payload = json.loads(mode["payload"])
        work = PROMPT_PATH.read_text(encoding="utf-8").strip() % {
            "workflow": cfg["researcher_workflow"][session["cli"]], "root": payload["root"],
            "version": payload["version"], "python": quote(payload["python"]), "tag": tag, "owner": cfg["owner_name"],
        }
        payload_arg = " --payload " + payload["fingerprint"]
    else:
        work = (
            "Stop automatically invoking Researcher for new work. Preserve any in-flight run and pending approval "
            "and handle its incoming decision through the same workflow and Agent Mail approval rules. "
            "Switching off cannot approve an outstanding design."
        )
    ack = ("After reading the workflow and before starting new Researcher work, run "
           "`agent-mail researcher-state --applied %d%s -- %s` using your own --session if required. "
           "Start new Researcher work only after it succeeds." % (revision, payload_arg, tag)
           if enabled else
           "After applying Off, run `agent-mail researcher-state --applied %d -- %s` with your own --session if required."
           % (revision, tag))
    return "\n".join((headline, check, ack, work) if enabled else (headline, check, work, ack))


def report_interpreter_blocker(st: Store, cfg: dict, session: sqlite3.Row,
                               mode: sqlite3.Row, detail: str) -> None:
    """Ask the live conversation to send one routed blocker report and record transport acceptance."""
    tag, revision = mode["tag"], mode["revision"]
    if mode["blocker_reported"]:
        return
    text = (
        "[Agent Mail Researcher blocked for %s, revision %d]\n"
        "Run `agent-mail researcher-state -- %s` and discard this notice unless On is still requested at "
        "revision %d and has yet to be applied. Keep Researcher on hold until you receive and apply a verified On "
        "control. Send %s one ordinary routed Agent Mail report for this revision with the blocker below.\n%s"
        % (tag, revision, tag, revision, cfg["owner_name"], detail)
    )
    code, _ = deliver.deliver_session(session, text, cfg, False)
    if code in (0, deliver.HELD):
        st.record_researcher_blocker(tag, revision)
    else:
        watch.log("researcher %s revision %d interpreter blocker delivery failed (%d); will retry" % (tag, revision, code))


def dispatch(st: Store, cfg: dict, session: sqlite3.Row,
             mode: sqlite3.Row) -> Optional[tuple[Optional[str], str, str]]:
    """Verify On from local disk or peer RPC and bind or clear its payload in the store.

    Send the control over the exact-session transport (agent-send locally, peer RPC remotely).
    Return the bound payload, outcome and detail, or None if the revision is superseded.
    """
    if mode["enabled"]:
        try:
            payload = researcher_payload.on_host(cfg, session["origin_host"], session["cli"], session["session_id"])
        except researcher_payload.Unavailable as e:
            if not st.bind_researcher_payload(mode["tag"], mode["revision"], None):
                return None
            if isinstance(e, researcher_payload.NoInterpreter):
                report_interpreter_blocker(st, cfg, session, mode, str(e))
            return None, "unavailable", str(e)
        revision = mode["revision"]
        if not st.bind_researcher_payload(mode["tag"], revision, json.dumps(payload._asdict())):
            return None
        mode = st.researcher_mode(mode["tag"])
        if mode is None or mode["revision"] != revision:
            return None
    code, detail = deliver.deliver_session(session, instruction(cfg, session, mode, control=True), cfg, False)
    if code == 0:
        return mode["payload"], "accepted", detail
    if code == deliver.HELD:
        return mode["payload"], "queued", detail
    return mode["payload"], ("unavailable" if code in deliver.SESSION_GONE else "error"), detail


def tick(st: Store, cfg: dict, now: float) -> None:
    """Send each due control through the exact-session transport and save its outcome without resuming sessions."""
    for due in st.due_researcher_modes(now):
        tag, revision = due["tag"], due["revision"]
        session = st.session_by_tag(tag)
        mode = st.researcher_mode(tag)
        if mode is None or mode["revision"] != revision or is_applied(mode):
            continue
        if supported(session):
            result = dispatch(st, cfg, session, mode)
            if result is None:
                continue
            payload, outcome, detail = result
        else:
            payload, outcome, detail = mode["payload"], "unavailable", "no supported owner for the routing tag"
        retry_at = now + RETRY_SECONDS if outcome in ("unavailable", "error") else None
        saved = st.record_researcher_delivery(tag, revision, payload, outcome, detail, retry_at)
        if not saved or (outcome, detail) != (mode["status"], mode["detail"]):
            watch.log("researcher %s revision %d %s%s: %s" % (
                tag, revision, outcome, "" if saved else " (superseded)", " ".join(detail.splitlines())))
