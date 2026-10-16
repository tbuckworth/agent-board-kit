"""Side effects: agent messages (agent-send), emails to the human (the mailroom's `agent-mail send
--unrouted`, so a reply reaches the orchestrator), provider stops. In shadow mode each is logged as what
*would* have happened, and nothing leaves the machine."""
import html
import tempfile

from . import config, creds, gather, util


def md_to_html(md):
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
    def __init__(self, db, cfg, shadow, now):
        self.db, self.cfg, self.shadow, self.now = db, cfg, shadow, now

    def ping(self, inc, res, text, resume=False):
        action = "resume" if resume else "ping"
        target = res["owner_session"]
        if self.shadow:
            self.db.log(action, "would message %s: %s" % (res["owner_name"] or target, text.splitlines()[0]),
                        key=res["key"], incident_id=inc["id"], shadow=True, now=self.now)
            return True, "shadow"
        cmd = ["agent-send", "--from", "cloud-watchdog", "--exact-id", "--cli", res["owner_cli"] or "claude",
               "--offline", "resume" if resume else "fail", target, text]
        code, out, err = util.run(cmd, env=creds.base_env(), timeout=120)
        ok = code == 0
        self.db.log(action, "to %s: exit %s %s" % (res["owner_name"] or target, code, (out + err).strip()[-200:]),
                    key=res["key"], incident_id=inc["id"], ok=ok, now=self.now)
        return ok, code

    def email(self, subject, md, key=None, incident_id=None, digest_if_capped=True):
        if self.shadow:
            self.db.log("email", "would email %s: %s" % (self.cfg.get("owner_name") or "the owner", subject), key=key,
                        incident_id=incident_id, shadow=True, now=self.now)
            return True
        if self.db.emails_today(self.now) >= self.cfg["max_emails_per_day"]:
            if digest_if_capped:
                self.db.add_digest("capped_email", key, "%s (held back: daily email cap)" % subject, self.now)
            self.db.log("email", "capped: %s" % subject, key=key, incident_id=incident_id, ok=False, now=self.now)
            return False
        return self.send(subject, md, key=key, incident_id=incident_id)

    def send(self, subject, md, key=None, incident_id=None):
        to = self.cfg.get("owner_email")
        if not to:
            self.db.log("email", "not sent, no owner_email configured (cloud-watchdog config.toml or the mailroom's "
                        "config.json): %s" % subject, key=key, incident_id=incident_id, ok=False, now=self.now)
            return False
        outbox = config.STATE_DIR / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=outbox, suffix=".md", delete=False) as t, \
                tempfile.NamedTemporaryFile("w", dir=outbox, suffix=".html", delete=False) as h:
            t.write(md)
            h.write(md_to_html(md))
        cmd = ["agent-mail", "send", "--unrouted", "--to", to, "--subject", subject,
               "--body-file", t.name, "--html", h.name]
        code, out, err = util.run(cmd, env=creds.base_env(), timeout=120)
        ok = code == 0
        self.db.log("email", "%s: %s" % (subject, (out + err).strip()[-200:]), key=key, incident_id=incident_id, ok=ok,
                    now=self.now)
        return ok

    def stop(self, res, actor, incident_id=None):
        acct = self.cfg["accounts"][res["account"]]
        if self.shadow:
            self.db.log("stop", "would stop %s (%s)" % (res["name"], res["key"]), key=res["key"],
                        incident_id=incident_id, actor=actor, shadow=True, now=self.now)
            return True, "shadow"
        ok, result = gather.op(res["account"], acct, "stop", native_id=res["native_id"])
        detail = result.get("message") if ok and isinstance(result, dict) else str(result)
        ok = ok and isinstance(result, dict) and result.get("ok")
        self.db.log("stop", "%s: %s" % (res["key"], detail), key=res["key"], incident_id=incident_id, actor=actor,
                    ok=bool(ok), now=self.now)
        return bool(ok), detail
