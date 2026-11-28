"""What agent-mail prints of the store: sent mail (sent), inbound mail (inbox, show) and the outcome of a send another
machine proxied here (receipt). Each returns the text, so the command run here and the same command proxied from
another machine (rpc.py) print the same. Read-only."""
import datetime
import json
import uuid

from .outbound import MailError


def timestamp(v):
    """A stored time (epoch seconds) as local "YYYY-MM-DD HH:MM", or "-" when there is none."""
    try:
        return datetime.datetime.fromtimestamp(int(float(v))).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "-"


def sent(st, limit, host_id):
    """The latest `limit` sent mails, one line each; a session on another machine than host_id (config host_id, where
    the store is) shows as cli@host, and mail kept for the Mail tab alone (no Gmail id) says so."""
    return "".join("%s  %-6s %-28s -> %-30s %s%s\n" % (
        timestamp(r["sent_at"]),
        (r["cli"] or "?") + ("@" + r["origin_host"] if r["origin_host"] and r["origin_host"] != host_id else ""),
        (r["session_name"] or (r["session_id"] or "")[:8])[:28], r["to_addr"], r["subject"],
        "" if r["gmail_id"] else "  (in the Mail tab)")
        for r in st.list_sent(limit))


def inbox(st, held, limit):
    rows = st.list_inbound("held" if held else None, limit)
    if not rows:
        return ("nothing held" if held else "no inbound mail seen yet") + "\n"
    out = []
    for r in rows:
        out.append("%-4d %s  %-9s %-14s %-30s %s\n" % (
            r["id"], timestamp(r["received_at"]), r["action"] or "", (r["match_rule"] or "-"),
            r["from_addr"], (r["subject"] or "")[:60]))
        if r["detail"]:
            out.append("     %s\n" % r["detail"])
    return "".join(out)


def show(st, inbound_id):
    """Every column of one inbound mail, its body last. Raises MailError when there is no such mail."""
    r = st.inbound(inbound_id)
    if not r:
        raise MailError("no inbound mail with id %s" % inbound_id, 4)
    return "".join("%-14s %s\n" % (k, r[k]) for k in r.keys() if k != "body") + "body:\n" + (r["body"] or "") + "\n"


def receipt(st, request_id, host_id):
    """The recorded status and printed result of proxied request request_id (a UUID; ValueError otherwise), or that
    host_id (config host_id, where the store is) has no record of it."""
    row = st.proxy_request(str(uuid.UUID(request_id)))
    return (json.dumps({"status": row["status"], "result": row["result"]}) if row
            else "request not recorded on %s" % host_id) + "\n"
