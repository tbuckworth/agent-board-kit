"""Groups and email (README.md, "Groups"): the mailroom mails the owner once for each group made (announce), and
their reply in that mail's thread becomes their post in the group (post_reply). A mail of theirs asking the
orchestrator for a group makes it once (request_mail, group_id).

A group's mail is routed as a session's is: the group has a row in the store's sessions table, with cli
routing.GROUP_CLI and the group's id as its session id, so the routing slip (routing.match) finds the group by the
reply's In-Reply-To, its Gmail thread or its reply tag. The daemon posts a reply only when the owner sent it and
DKIM passed, as it delivers any mail of theirs (bin/agent-mailroom)."""
from . import config, groupcmd, groups, outbound, routing
from .groups import GroupError

SUBJECT = 'Group "%s": %s and you'
REQUEST_SECONDS = 24 * 3600   # how old a mail of the owner's may be and still make a group (request_mail)


def post_id(gmail_id):
    """The id of the post a reply with this Gmail id becomes (groups.derived_id)."""
    return groups.derived_id(gmail_id)


def group_id(gmail_id):
    """The id of the group a mail of the owner's asks the orchestrator for (groups.derived_id)."""
    return groups.derived_id("group:" + gmail_id)


def request_mail(st, cfg, inbound_id, now):
    """The Gmail id of inbound mail inbound_id, when an orchestrator run may make a group as the owner for it (agent-group
    new --for-mail): the owner's own mail (config allowlist), given to the orchestrator (match_rule "orchestrator")
    within REQUEST_SECONDS of now. The daemon gives the owner's mail to the orchestrator only with a DKIM pass; a mail handed
    to it by hand (agent-mail rerun, or release N orchestrator) is vouched for by whoever did that. Raises GroupError
    (4: no such mail; 2: any other) saying which it is not."""
    row = st.inbound(inbound_id)
    if row is None:
        raise GroupError("no inbound mail #%d" % inbound_id, 4)
    if routing.sender_tier(row["from_addr"], cfg) != "owner":
        raise GroupError("mail #%d is not %s's own" % (inbound_id, cfg["owner_name"]), 2)
    if row["match_rule"] != "orchestrator":
        raise GroupError("mail #%d was not given to the orchestrator" % inbound_id, 2)
    if now - row["received_at"] > REQUEST_SECONDS:
        raise GroupError("mail #%d is more than %d hours old" % (inbound_id, REQUEST_SECONDS // 3600), 2)
    return row["gmail_id"]


def text(g):
    """The plain text of a group's mail to the owner."""
    return ('The group "%s" was made by %s. Its members are %s and you.\n\n'
            "Reply to this mail to post in the group: your reply wakes every member. Agents' posts stay on the "
            "Agent Board and do not come to your inbox.\n"
            % (g.title, "you" if g.maker is None else groups.member_label(g.maker), groupcmd.who_list(g.members)))


def announce(svc, st, cfg):
    """Mail the owner about each group they have not been mailed about and have not hidden, from the group's own routing row
    (made in the store's sessions table the first time), and record it in the store's meta (group-mail:<group id>).
    Returns the groups mailed. A failed send raises: the groups not yet recorded are mailed on the next call."""
    with groups.Store(groups.ROOT) as gst:
        pending = [g for g in gst.groups() if not g.hidden and not st.get_meta("group-mail:" + g.id)]
    ctx = outbound.Context.opened(cfg, st, svc)
    for g in pending:
        row = st.session_for(routing.GROUP_CLI, g.id, g.title, "", origin_host=cfg["host_id"])
        subject = routing.tag_subject(SUBJECT % (g.title, groupcmd.who_list(g.members)), row["tag"])
        res, _ = outbound.transmit(ctx, row, config.owner_address(cfg), None, subject,
                                   outbound.Mail(text(g), None, [], []), None)
        st.set_meta("group-mail:" + g.id, res["id"])
    return pending


def post_command(cfg, group_id):
    """The command that posts a reply's text in the group by hand, as the owner: what the mailroom tells the owner when it
    cannot post it itself. It carries no --id: the failed post may be stored already, undelivered, under
    post_id(gmail id), and agent-group refuses a stored id without delivering it, so only a fresh id reaches the
    members, at worst as a second copy."""
    return "agent-group post %s --as %s" % (group_id, config.OWNER_ALIAS)


def post_reply(cfg, group_id, body, gmail_id):
    """Post the owner's reply, with Gmail id gmail_id and body its text without the quoted mail, in the group as their
    post, as agent-group post does (groupcmd.run): it writes the group store (the post, reads.json, deliveries.jsonl),
    delivers the post to the members through agent-send or SSH and raises their notice flags. Returns what
    agent-group prints. Raises GroupError when it cannot be posted (an empty reply, say)."""
    return groupcmd.run(cfg, groups.member(cfg["host_id"], None), "post",
                        {"group": group_id, "text": body, "reply_to": None, "post_id": post_id(gmail_id)})
