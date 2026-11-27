"""agent-mail between machines, over SSH (remote.call): a machine with config mail_proxy (the Mac) runs its mail
commands on the central mailroom (the desktop) through proxy, and serve answers what one machine asks another: a
proxied mail or group command, a delivery to a session, the files of a Mail-tab reply for a session to open, a session
lookup, a transcript search, a group notice flag.

The mail commands a machine proxies are PROXY_ACTIONS, each with the flags it passes on; its group commands go as
op "group" (groupcmd.proxy). A send or reply also carries the
origin session, resolved where it was typed, and its mail (outbound.Mail.as_json); withdraw carries the origin. The
result is the text the command prints on the mailroom, which the asking machine prints as its own; the result of a send
or reply the pre-send review held starts with review.NOT_SENT (exit 3 there). The machines are updated one at a time,
so requests and results keep their exact format: an older agent-mail on either side keeps working."""
import base64
import hashlib
import json
import time
import uuid
from pathlib import Path

from . import (config, deliver, groupcmd, groups, listing, outbound, recovery, registry, remote, replyfiles,
               researcher_payload, researchermode, review, widgets)

# The mail commands a machine with config mail_proxy runs on the central mailroom, and the flags each passes on
# (as "args"; a send's or reply's request_id travels beside them).
PROXY_ACTIONS = {
    "send": ("to", "cc", "subject", "no_review"),
    "reply": ("inbound_id", "no_review", "subject"),
    "sent": ("limit",),
    "inbox": ("limit", "held"),
    "show": ("inbound_id",),
    "receipt": ("request_id",),
    "researcher-state": ("tag", "applied", "payload"),
    "researcher-request": ("tag", "thread"),
    "withdraw": (),
}
# Flags added to PROXY_ACTIONS since its first format: passed only when given, so that a request without one keeps
# that format and a mailroom that does not know the flag yet still takes it.
LATER_FLAGS = {"reply": ("subject",), "researcher-state": ("applied", "payload")}


def flags(action, parsed):
    """The flags of agent-mail <action> that proxy passes on, read off its parsed arguments."""
    return {k: getattr(parsed, k) for k in PROXY_ACTIONS[action]
            if getattr(parsed, k) is not None or k not in LATER_FLAGS.get(action, ())}


def proxy(cfg, action, args, origin, mail, request_id):
    """Run agent-mail <action> (PROXY_ACTIONS) on the central mailroom, config mail_proxy, over SSH, with its flags
    args; origin (the session it runs for: send, reply, withdraw) and mail with its request_id (send, reply) or None.
    Returns (what it printed there, whether the pre-send review held the mail). Raises remote.RemoteError."""
    payload = {"op": "mail", "action": action, "args": args}
    if origin is not None:
        payload["origin"] = remote.identity(dict(origin, host=cfg["host_id"]))
    if mail is not None:
        payload.update(mail.as_json())
        payload["request_id"] = request_id
    result = remote.call(cfg, cfg["mail_proxy"], payload, timeout=60)
    return result, result.startswith(review.NOT_SENT)


def serve(ctx, wire):
    """Answer one request another machine sent over SSH: wire is its JSON, and the return value is the response,
    {"protocol", "ok": true, "result"} or {"protocol", "ok": false, "error"} as JSON. Only a configured peer is
    answered, and only the configured central mailroom may deliver to a session here or set its group notice flags.
    What a request does: a mail command (mail), a delivery without resume or permission bypass (agent-send, a
    subprocess) to a session by its id or, for a group post, its name, a registry lookup by id or name, a transcript
    search (recovery.search), the installed Researcher payload for a session (central mailroom only), a group
    command (groupcmd.serve), raising group notice flags (groups.raise_flags), and
    writing the files of a Mail-tab reply (replyfiles.write, under this machine's replyfiles.FILES_DIR; only from the
    central mailroom), which answers with their paths here."""
    cfg = ctx.cfg
    response = {"protocol": remote.PROTOCOL, "ok": False}
    try:
        if len(wire.encode()) > remote.MAX_REQUEST:
            raise ValueError(remote.TOO_LARGE)
        req = json.loads(wire)
        if req.get("protocol") != remote.PROTOCOL or req.get("destination_host") != cfg["host_id"]:
            raise ValueError("protocol or destination host mismatch")
        if req.get("source_host") not in cfg["remote_hosts"]:
            raise ValueError("source host is not a configured peer")
        op = req.get("op")
        if op == "ping":
            result = {"host": cfg["host_id"], "protocol": remote.PROTOCOL}
        elif op == "mail":
            result = mail(ctx, req)
        elif op == "deliver":
            if cfg.get("mail_proxy") != req["source_host"]:
                raise ValueError("only the configured central mailroom may deliver")
            if "agent" in req:   # a group post, to the session of that name
                rc, detail = deliver.deliver_named(str(req["agent"]), req["text"], timeout=20)
            else:
                sid = str(uuid.UUID(req["session_id"]))
                if req["cli"] not in config.CLIS:
                    raise ValueError("invalid CLI")
                rc, detail = deliver.deliver(req["cli"], sid, req["text"], resume=False, dangerous=False, timeout=20)
            result = {"code": rc, "detail": detail}
        elif op == "researcher-payload":
            if cfg.get("mail_proxy") != req["source_host"]:
                raise ValueError("only the configured central mailroom may verify the installed Researcher payload")
            sid = str(uuid.UUID(req["session_id"]))
            if req["cli"] not in config.CLIS:
                raise ValueError("invalid CLI")
            try:
                result = researcher_payload.local(cfg, req["cli"], sid)._asdict()
            except researcher_payload.Unavailable as e:
                result = {"error": str(e), "missing_interpreter": isinstance(e, researcher_payload.NoInterpreter)}
        elif op == "files":
            if cfg.get("mail_proxy") != req["source_host"]:
                raise ValueError("only the configured central mailroom may write a reply's files")
            files = [(str(f["name"]), base64.b64decode(f["data"], validate=True)) for f in req["files"]]
            result = {"paths": replyfiles.write(replyfiles.FILES_DIR, str(req["key"]), files)}
        elif op == "group-flag":
            if cfg.get("mail_proxy") != req["source_host"]:
                raise ValueError("only the configured central mailroom may set group notice flags")
            groups.raise_flags([str(a) for a in req["agents"]])
            result = {}
        elif op == "group":
            result = groupcmd.serve(cfg, req)
        elif op == "search":
            result = recovery.search(req["queries"], cfg)
        elif op == "lookup":
            if "name" in req:   # a group member: a session's exact name
                sess, code, _ = registry.by_name(registry.all_sessions(), str(req["name"]))
            else:
                sid = str(uuid.UUID(req["session_id"]))
                if req["cli"] not in config.CLIS:
                    raise ValueError("invalid CLI")
                sess, code, _ = registry.by_id(registry.all_sessions(req["cli"]), sid)
            if code:
                raise ValueError("exact session is not uniquely present in the registry")
            result = {k: sess.get(k) for k in ("cli", "id", "name", "cwd", "live")}
        else:
            raise ValueError("unsupported RPC operation")
        response.update(ok=True, result=result)
    except Exception as e:
        response["error"] = str(e) or type(e).__name__
    return json.dumps(response)


def mail(ctx, request):
    """Run the mail command another machine proxied here (request: its op "mail" request) and return what it prints.
    A send or reply is recorded under its request_id first, so the same request never sends twice: a repeat returns
    the recorded result, and one whose outcome is unknown (it failed or is running) is refused. Raises on a request
    this machine does not run, and as the command does (outbound.MailError)."""
    cfg, st = ctx.cfg, ctx.st
    if cfg.get("mail_proxy") or not cfg.get("send_enabled"):
        raise ValueError("this machine is not a central mailroom")
    action = request.get("action")
    if action not in PROXY_ACTIONS:
        raise ValueError("unsupported proxy mail action")
    args = request.get("args", {})
    if not isinstance(args, dict) or set(args) - set(PROXY_ACTIONS[action]):
        raise ValueError("invalid proxy arguments")
    if action == "sent":
        return listing.sent(st, args["limit"], cfg["host_id"])
    if action == "inbox":
        return listing.inbox(st, args["held"], args["limit"])
    if action == "show":
        return listing.show(st, args["inbound_id"])
    if action == "receipt":
        return listing.receipt(st, args["request_id"], cfg["host_id"])
    if action == "researcher-state" and args.get("applied") is None:
        return json.dumps(researchermode.state(st, args["tag"], request["source_host"]))
    if action == "researcher-request":
        return json.dumps(researchermode.request_receipt(st, cfg, args["tag"], args["thread"], request["source_host"]))
    origin = remote.identity(request.get("origin"))
    if origin["host"] != request["source_host"]:
        raise ValueError("origin host does not match source host")
    if action == "researcher-state":
        return json.dumps(researchermode.acknowledge(st, cfg, args["tag"],
                                                     (origin["host"], origin["cli"], origin["id"]),
                                                     args["applied"], args.get("payload"), time.time()))
    if action == "withdraw":
        return outbound.withdraw(st, outbound.sender_row(ctx, origin)["tag"])
    rid = str(uuid.UUID(request["request_id"]))
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    old = st.claim_proxy_request(rid, fingerprint)
    if old:
        if old["fingerprint"] != fingerprint:
            raise ValueError("request ID was already used for different content")
        if old["status"] == "complete":
            return old["result"]
        raise ValueError("request %s has an uncertain/in-progress outcome; inspect sent mail before retrying" % rid)
    content = _proxied_mail(request)
    if action == "send":
        out = outbound.send_mail(ctx, outbound.sender_row(ctx, origin), args["to"], args.get("cc"), args["subject"],
                                 content, args.get("no_review"))
    else:
        inb, row = outbound.reply_target(st, args["inbound_id"])
        if row is None or (row["origin_host"], row["cli"], row["session_id"]) != (origin["host"], origin["cli"],
                                                                                 origin["id"]):
            raise ValueError("reply is not owned by this origin session; ask %s for an explicit handoff"
                             % cfg["owner_name"])
        out = outbound.reply_mail(ctx, row, inb, content, args.get("no_review"), args.get("subject"),
                                  (origin["host"], origin["cli"], origin["id"]))
    result = out.text()
    st.complete_proxy_request(rid, result)
    return result


def _proxied_mail(request):
    """The mail a proxied send or reply carries. HTML travels as content, never a remote file name; an attachment
    keeps only its file name, and the text comes from the HTML when it is blank. It is checked as agent-mail checks its
    own (outbound.check_image and check_mail, which raise MailError), and its widgets are checked again, or its blocks
    prepared when an older agent-mail sent them as they were typed (widgets.prepare)."""
    m = outbound.Mail.from_json(request)
    for name, data in m.images:
        outbound.check_image(name, data)
    m = m._replace(text=outbound.plain_text(m.text, m.html), files=[(Path(n).name, d) for n, d in m.files])
    outbound.check_mail(m)
    return widgets.prepare(m)
