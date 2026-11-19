"""The escalation ladder: one incident per resource, advanced on every check.

    open ──owner live──► ping ─────────────► asked ──deadline, no answer──► 2nd ping (not crash loops)
      │                                        │                            └► crash loop: auto-stop (policy) + FYI
      ├─owner exited──► resume 1 turn + email the human ─► asked            └► else: email the human ─► escalated
      └─owner unknown─► investigator (live mode) ─► found? ping : email the human ─► escalated
    escalated ──crash loop past deadline──► auto-stop (policy) + FYI;  re-email at most daily or on a new crash loop

A failed message, or an owner agent-send cannot say is live, goes to the human instead. A failed escalation, cost or
foreign crash-loop email is resent next check; the email after an auto-stop is not (the stop itself is logged).
Owners answer with `cloud-watch ack|close|disown` (cli.py), which resolve or reset the incident.
"""
import json

from . import messages, observe, owner, policy, rules, util

OWNER_RECHECK_S = 30 * 60   # an incident's owner search runs again after this long


def signature(findings):
    """The rules behind these findings as an incident's last_email_sig records them."""
    return ",".join(sorted({f["rule"] for f in findings}))


class Ladder:
    def __init__(self, db, cfg, now, notifier, live, sessions, resolve_owner, run_investigator, op):
        """The collaborators that reach outside the database: `sessions` reads the agent-send address book
        (owner.sessions), `resolve_owner` searches for a resource's owner (owner.resolve), `run_investigator` asks the
        model (investigator.run) and `op` runs a provider op in its credentialed child (gather.op)."""
        self.db, self.cfg, self.now, self.n, self.live = db, cfg, now, notifier, live
        self.sessions, self.resolve_owner = sessions, resolve_owner
        self.run_investigator, self.op = run_investigator, op
        self._book = None
        self.book_error = None
        self._verdicts = {}

    @property
    def book(self):
        """The agent-send address book, read once per check; None when it could not be read, and `book_error`
        then holds the cause, logged once."""
        if self._book is None and self.book_error is None:
            try:
                self._book = self.sessions()
            except owner.AddressBookError as e:
                self.book_error = str(e)
                self.db.log("error", "address book unavailable: %s" % e, ok=False, shadow=False, now=self.now)
        return self._book

    def owner_live(self, res):
        """Whether the owner session is live; None when the address book could not be read."""
        return None if self.book is None else owner.is_live(self.book, res["owner_session"])

    def will_stop(self, res, crash):
        """Whether the watchdog will stop it at the deadline (unless the fresh log check then disagrees): what a
        ping or an escalation email promises."""
        return crash and policy.can_stop(res, "watchdog", self.cfg, self.now, fresh_crash=True)[0]

    def reload(self, res):
        return self.db.resource(res["key"])

    # ------------------------------------------------------------------ entry
    def process(self):
        for inc in self.db.q("select i.* from incidents i join resources r on r.key=i.key "
                             "where i.closed_at is null and r.gone_at is not null"):
            self.db.close_incident(inc["id"], "gone", self.now)
            self.db.log("resolve", "gone", key=inc["key"], incident_id=inc["id"], shadow=False, now=self.now)
        for res in self.db.live_resources():
            try:
                self.one(res)
            except Exception as e:  # noqa: BLE001 - one bad resource must not stop the rest
                self.db.log("error", "ladder: %s: %s" % (type(e).__name__, e), key=res["key"], ok=False, shadow=False,
                            now=self.now)

    def one(self, res):
        findings = rules.evaluate(res, self.cfg, self.now)
        owner_f = [f for f in findings if f["audience"] == "owner"]
        for f in findings:
            if f["audience"] == "foreign_crash":
                self.foreign_crash(res)
            elif f["audience"] == "human":
                self.cost_email(res, f)
        inc = self.db.open_incident(res["key"])
        if not owner_f:
            # Close once it has stopped billing, or no rule has fired for an hour (so a rule hovering at its
            # threshold does not open a fresh incident, and a fresh email, every ten minutes).
            if inc is not None and (not res["billing_now"] or self.now - inc["updated_at"] > 3600):
                self.db.close_incident(inc["id"], "cleared", self.now)
                self.db.log("resolve", "cleared: no rule fires any more", key=res["key"], incident_id=inc["id"],
                            shadow=False, now=self.now)
            return
        names = sorted({f["rule"] for f in owner_f})
        if inc is None:
            inc = self.db.new_incident(res["key"], names, owner_f[0]["severity"], self.now)
            self.db.log("open", "; ".join(f["text"] for f in owner_f), key=res["key"], incident_id=inc["id"],
                        shadow=False, now=self.now)
        else:
            self.db.update_incident(inc["id"], self.now, rules=json.dumps(names))  # also refreshes updated_at
            inc = self.db.incident(inc["id"])
        self.advance(inc, res, owner_f)

    # ------------------------------------------------------------------ owner path
    def ensure_owner(self, res):
        return owner.refresh(self.db, self.cfg, res, self.now, self.book, OWNER_RECHECK_S, self.resolve_owner)

    def wait_min(self, crash):
        """Minutes the owner has to answer: the crash-loop rule's wait for a crash loop, else the ladder's."""
        return self.cfg["rules"]["crash_loop"]["owner_wait_min"] if crash else self.cfg["ladder"]["owner_wait_min"]

    def advance(self, inc, res, findings):
        """Take the incident's next step: an open one at once, an asked or escalated one once its deadline passed."""
        crash = any(f["rule"] == "crash_loop" for f in findings)
        if inc["state"] == "open":
            self.advance_open(inc, res, findings, crash)
        elif self.now < (inc["deadline_at"] or 0):
            return
        elif inc["state"] == "asked":
            self.advance_asked(inc, res, findings, crash)
        elif inc["state"] == "escalated":
            self.advance_escalated(inc, res, findings, crash)

    def advance_open(self, inc, res, findings, crash):
        """Find the owner and message it; resume an exited owner for one turn and email the human at the same time;
        email the human alone when nobody is found or the address book cannot say whether the owner is live."""
        res, cands = self.ensure_owner(res)
        if not res["owner_session"] and not inc["investigated_at"]:
            res = self.investigate(inc, res, findings, cands)
        if not res["owner_session"]:
            reason = "I could not tell which session launched it (%s)." % (res["owner_evidence"] or "no evidence")
            self.escalate(inc, res, findings, reason, crash, resumed=False)
            return
        live = self.owner_live(res)
        if live is None:
            self.escalate(inc, res, findings, self.liveness_unknown(res), crash, resumed=False)
        elif live:
            self.ping(inc, res, findings, crash)
        else:
            if not inc["resumed_at"]:   # set: resumed already, and only the email to the human failed
                text = messages.resume_turn(inc, res, findings, self.now, self.wait_min(crash),
                                            self.will_stop(res, crash), self.cfg)
                ok, detail = self.n.ping(inc, res, text, resume=True)
                if not ok:
                    self.escalate(inc, res, findings, self.unreachable(res, detail), crash, resumed=False)
                    return
                self.db.update_incident(inc["id"], self.now, resumed_at=self.now)
            reason = ("the session that launched it, **%s** (`%s`), has exited. I resumed it for one turn to ask, "
                      "and am emailing you at the same time; whichever answers first wins." % (
                          res["owner_name"] or "?", res["owner_session"]))
            self.escalate(inc, res, findings, reason, crash, resumed=True)

    def advance_asked(self, inc, res, findings, crash):
        """The owner did not answer in time: message a live owner again (up to max_pings; not for a crash loop or a
        resumed owner), else stop a crash loop the policy allows, else email the human unless already emailed."""
        another_ping = (res["owner_session"] and not crash and not inc["resumed_at"]
                        and inc["ping_count"] < self.cfg["ladder"]["max_pings"])
        live = self.owner_live(res) if another_ping else False
        if live:
            self.ping(inc, res, findings, crash)
            return
        if crash and self.try_auto_stop(inc, res):
            return
        if inc["email_count"]:
            self.db.update_incident(inc["id"], self.now, state="escalated")
            return
        reason = "I messaged **%s** at %s and got no answer by %s." % (
            messages.owner_label(res), util.iso(inc["last_ping_at"], local=True),
            util.iso(inc["deadline_at"], local=True))
        if live is None:
            reason += " " + self.liveness_unknown(res)
        self.escalate(inc, res, findings, reason, crash, resumed=False)

    def advance_escalated(self, inc, res, findings, crash):
        """The human was emailed: stop a crash loop the policy allows, else email again on a new crash loop or once
        reemail_hours have passed."""
        if crash and self.try_auto_stop(inc, res):
            return
        sig = signature(findings)
        new_crash = crash and "crash_loop" not in (inc["last_email_sig"] or "")
        stale = self.now - (inc["last_email_at"] or 0) >= self.cfg["ladder"]["reemail_hours"] * 3600
        if new_crash or (stale and sig):
            self.escalate(inc, res, findings, "still unresolved since %s." % util.iso(inc["opened_at"], local=True),
                          crash, resumed=False)

    def ping(self, inc, res, findings, crash):
        """Message the live owner and wait for an answer until the new deadline; if the message fails, email the
        human."""
        wait_min = self.wait_min(crash)
        ok, detail = self.n.ping(inc, res, messages.ping(inc, res, findings, self.now, wait_min,
                                                         self.will_stop(res, crash), self.cfg))
        if not ok:
            self.escalate(inc, res, findings, self.unreachable(res, detail), crash, resumed=False)
            return
        self.db.update_incident(inc["id"], self.now, state="asked", ping_count=inc["ping_count"] + 1,
                                last_ping_at=self.now, deadline_at=self.now + wait_min * 60)

    def unreachable(self, res, detail):
        return "I could not message **%s**: %s." % (messages.owner_label(res), detail)

    def liveness_unknown(self, res):
        return "I could not check whether **%s** is live: %s." % (messages.owner_label(res), self.book_error)

    def investigate(self, inc, res, findings, cands):
        self.db.update_incident(inc["id"], self.now, investigated_at=self.now)
        if not self.live:
            self.db.log("investigate", "would run the investigator (owner unknown)", key=res["key"],
                        incident_id=inc["id"], shadow=True, now=self.now)
            return res
        verdict, status = self.run_investigator(self.db, self.cfg, res, findings, cands, self.now)
        if verdict and verdict.get("owner_session_id") and verdict.get("confidence") in ("high", "medium"):
            entry = (self.book or {}).get(verdict["owner_session_id"]) or {}   # no address book: the verdict's name
            self.db.update_resource(res["key"], **owner.assignment(
                session=verdict["owner_session_id"], cli=entry.get("cli") or "claude",
                name=entry.get("name") or verdict.get("owner_name"), source="model", confidence=verdict["confidence"],
                evidence="investigator: %s" % verdict.get("evidence", ""), now=self.now))
        else:
            self.db.update_resource(res["key"], owner_evidence="%s | investigator: %s" % (
                res["owner_evidence"] or "",
                (verdict.get("summary") or verdict.get("evidence") or "") if verdict else status))
        self._verdicts[res["key"]] = verdict
        return self.reload(res)

    def recommendation(self, res, findings, crash):
        v = self._verdicts.get(res["key"])
        if v and v.get("recommendation"):
            return "%s (investigator: %s)" % (v["recommendation"], v.get("summary") or "")
        if crash:
            return "stop it: it is crash-looping and doing no useful work."
        if any(f["rule"] == "idle_gpu" for f in findings):
            return "copy any results off, then terminate it: the GPU has been idle."
        return "check whether it is still needed; if nobody claims it, stop it."

    def escalate(self, inc, res, findings, reason, crash, resumed):
        """Email the human. Once the email is sent (or folded into the digest) the incident is "asked" if the owner was
        resumed for one turn and may still answer, else "escalated". A failed send records nothing, so the next
        check sends it again."""
        wait = self.wait_min(crash) * 60
        stop_at = (self.now + wait) if self.will_stop(res, crash) else None
        subject, md = messages.escalation(inc, res, findings, self.now, reason,
                                          self.recommendation(res, findings, crash), stop_at)
        if self.n.email(subject, md, key=res["key"], incident_id=inc["id"]) == "failed":
            return
        upd = {"state": "asked" if resumed else "escalated", "email_count": (inc["email_count"] or 0) + 1,
               "last_email_at": self.now, "last_email_sig": signature(findings)}
        if not inc["deadline_at"] or self.now >= inc["deadline_at"]:
            upd["deadline_at"] = self.now + wait
        self.db.update_incident(inc["id"], self.now, **upd)

    def try_auto_stop(self, inc, res):
        ok, reason = policy.can_stop(res, "watchdog", self.cfg, self.now, fresh_crash=True)
        if not ok:
            self.db.log("auto-stop-skip", reason, key=res["key"], incident_id=inc["id"], shadow=False, now=self.now)
            return False
        # Re-check the logs right now; never act on a stale observation.
        acct = self.cfg["accounts"][res["account"]]
        okc, detail = self.op(acct, "crash_check", {"window_min": self.cfg["rules"]["crash_loop"]["window_min"]},
                              res["native_id"])
        if not okc:
            self.db.log("auto-stop-skip", "fresh crash check failed: %s" % str(detail)[:200], key=res["key"],
                        incident_id=inc["id"], ok=False, shadow=False, now=self.now)
            return False
        fresh = observe.crash_candidate(detail, self.cfg["rules"]["crash_loop"])
        ok, reason = policy.can_stop(res, "watchdog", self.cfg, self.now, fresh_crash=fresh)
        if not ok:
            self.db.log("auto-stop-skip", reason, key=res["key"], incident_id=inc["id"], shadow=False, now=self.now)
            return False
        stopped, detail = self.n.stop(res, "watchdog", inc["id"])
        subject, md = messages.auto_stopped(inc, res, self.now, stopped, detail)
        self.n.email(subject, md, key=res["key"], incident_id=inc["id"])
        if stopped:
            self.db.close_incident(inc["id"], "auto-stopped", self.now)
        return stopped

    # ------------------------------------------------------------------ paths that only email the human
    def cost_email(self, res, f):
        if f["tier"] <= (res["cost_email_tier"] or 0):
            return
        inc = self.db.open_incident(res["key"])
        subject, md = messages.cost_email(inc["id"] if inc else None, res, f, self.now)
        if self.n.email(subject, md, key=res["key"], incident_id=inc["id"] if inc else None) != "failed":
            self.db.update_resource(res["key"], cost_email_tier=f["tier"])

    def foreign_crash(self, res):
        """Email the human about someone else's crash-looping app, again every ladder.reemail_hours while it lasts."""
        k = "foreign_crash:" + res["key"]
        last = self.db.meta(k, 0)
        if self.now - last < self.cfg["ladder"]["reemail_hours"] * 3600:
            return
        subject, md = messages.foreign_crash(res, self.now)
        if self.n.email(subject, md, key=res["key"]) != "failed":
            self.db.set_meta(k, self.now)
