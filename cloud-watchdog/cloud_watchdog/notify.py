"""Side effects: agent messages (agent-send), emails to the human (the mailroom's `agent-mail send
--unrouted`, so a reply reaches the orchestrator), provider stops. In shadow mode a message, an incident email or a
stop is only logged as what *would* have happened; send(), which the digest and the alerts use, always sends."""
import html
import tempfile

from . import config, creds, messages


def md_to_html(md):
    """The HTML part of an email. With the `markdown` package (requirements.txt; in the kit's venv) the markdown is
    rendered; without it the text is sent preformatted."""
    try:
        import markdown
        body = markdown.markdown(md, extensions=["tables", "fenced_code", "sane_lists"])
    except ImportError:
        body = "<pre>%s</pre>" % html.escape(md)
    body = body.replace("<code>", '<code style="background:#f3f3f3;padding:0 3px;border-radius:3px">')
    body = body.replace("<table>", '<table style="border-collapse:collapse;font-size:13px">')
    for tag in ("<td>", "<th>"):
        body = body.replace(tag, tag[:-1] + ' style="border:1px solid #ccc;padding:3px 6px;text-align:left;vertical-align:top">')
    return ('<html><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;'
            'line-height:1.45;color:#222;max-width:860px">%s</body></html>' % body)


class Notifier:
    def __init__(self, db, cfg, shadow, now, run, op):
        """`run` runs agent-send and agent-mail (util.run's signature) and `op` a provider op in its credentialed child
        (gather.op's): the two ways out of the machine. In shadow mode ping, email and stop call neither;
        send() always sends (the daily digest goes out in shadow mode too)."""
        self.db, self.cfg, self.shadow, self.now, self.run, self.op = db, cfg, shadow, now, run, op

    def ping(self, inc, res, text, resume=False):
        """Message the owner session through agent-send (resume=True: resume an exited session for one turn).
        Returns (ok, detail): agent-send's exit status and output, or "shadow"."""
        action = "resume" if resume else "ping"
        target = res["owner_session"]
        if self.shadow:
            self.db.log(action, "would message %s: %s" % (messages.owner_label(res), text.splitlines()[0]),
                        key=res["key"], incident_id=inc["id"], shadow=True, now=self.now)
            return True, "shadow"
        cmd = ["agent-send", "--from", "cloud-watchdog", "--exact-id", "--cli", res["owner_cli"] or "claude",
               "--offline", "resume" if resume else "fail", target, text]
        code, out, err = self.run(cmd, env=creds.base_env(), timeout=120)
        ok = code == 0
        detail = "exit %s %s" % (code, (out + err).strip()[-200:])
        self.db.log(action, "to %s: %s" % (messages.owner_label(res), detail),
                    key=res["key"], incident_id=inc["id"], ok=ok, shadow=False, now=self.now)
        return ok, detail

    def email(self, subject, md, key=None, incident_id=None):
        """Email the human. Every outcome is recorded as an `email` action. Returns:
          "sent"    agent-mail sent it (send() writes the .md and .html bodies under STATE_DIR/outbox first);
          "failed"  agent-mail failed, after the same outbox files were written;
          "folded"  max_emails_per_day was reached: nothing sent, the subject goes into the next digest;
          "shadow"  shadow mode: nothing sent and no outbox files."""
        if self.shadow:
            self.db.log("email", "would email %s: %s" % (messages.human(self.cfg), subject), key=key,
                        incident_id=incident_id, shadow=True, now=self.now)
            return "shadow"
        if self.db.emails_today(self.now) >= self.cfg["max_emails_per_day"]:
            self.db.add_digest("capped_email", key, "%s (held back: daily email cap)" % subject, self.now)
            self.db.log("email", "capped: %s" % subject, key=key, incident_id=incident_id, ok=False, shadow=False,
                        now=self.now)
            return "folded"
        return "sent" if self.send(subject, md, key=key, incident_id=incident_id) else "failed"

    def send(self, subject, md, key=None, incident_id=None):
        """Email the human (owner_email) `md` with messages.footer for `incident_id` under it, through agent-mail,
        after writing the .md and .html bodies under STATE_DIR/outbox. Logs an `email` action. Returns whether
        agent-mail sent it (False, logged, when no owner_email is configured)."""
        to = self.cfg.get("owner_email")
        if not to:
            self.db.log("email", "not sent, no owner_email configured (cloud-watchdog config.toml or the mailroom's "
                        "config.json): %s" % subject, key=key, incident_id=incident_id, ok=False, shadow=False,
                        now=self.now)
            return False
        md += messages.footer(incident_id, self.cfg["shadow_days"], self.cfg)
        outbox = config.STATE_DIR / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=outbox, suffix=".md", delete=False) as t, \
                tempfile.NamedTemporaryFile("w", dir=outbox, suffix=".html", delete=False) as h:
            t.write(md)
            h.write(md_to_html(md))
        cmd = ["agent-mail", "send", "--unrouted", "--to", to, "--subject", subject,
               "--body-file", t.name, "--html", h.name]
        code, out, err = self.run(cmd, env=creds.base_env(), timeout=120)
        ok = code == 0
        self.db.log("email", "%s: %s" % (subject, (out + err).strip()[-200:]), key=key, incident_id=incident_id, ok=ok,
                    shadow=False, now=self.now)
        return ok

    def stop(self, res, actor, incident_id=None):
        acct = self.cfg["accounts"][res["account"]]
        if self.shadow:
            self.db.log("stop", "would stop %s" % messages.label(res), key=res["key"],
                        incident_id=incident_id, actor=actor, shadow=True, now=self.now)
            return True, "shadow"
        ok, result = self.op(acct, "stop", native_id=res["native_id"])
        detail = result.get("message") if ok and isinstance(result, dict) else str(result)
        ok = ok and isinstance(result, dict) and result.get("ok")
        self.db.log("stop", "%s: %s" % (res["key"], detail), key=res["key"], incident_id=incident_id, actor=actor,
                    ok=bool(ok), shadow=False, now=self.now)
        return bool(ok), detail
