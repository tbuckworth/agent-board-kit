"""The escalation ladder: one incident per resource, advanced on every check.

    open ──owner live──► ping ─────────────► asked ──deadline, no answer──► 2nd ping (not crash loops)
      │                                        │                            └► crash loop: auto-stop (policy) + FYI
      ├─owner exited──► resume 1 turn + email the human ─► asked            └► else: email the human ─► escalated
      └─owner unknown─► investigator (live mode) ─► found? ping : email the human ─► escalated
    escalated ──crash loop past deadline──► auto-stop (policy) + FYI;  re-email at most daily or on a new crash loop

Owners answer with `cloud-watch ack|close|disown` (cli.py), which resolve or reset the incident.
"""
import json

from . import gather, investigator, messages, observe, owner, policy, rules, util


class Ladder:
    def __init__(self, db, cfg, now, notifier, live):
        self.db, self.cfg, self.now, self.n, self.live = db, cfg, now, notifier, live
        self._book = None
        self._verdicts = {}

    @property
    def book(self):
        if self._book is None:
            self._book = owner.sessions()
        return self._book

    def reload(self, res):
        return self.db.resource(res["key"])

    # ------------------------------------------------------------------ entry
    def process(self):
        for inc in self.db.q("select i.* from incidents i join resources r on r.key=i.key "
                             "where i.closed_at is null and r.gone_at is not null"):
            self.db.close_incident(inc["id"], "gone", self.now)
            self.db.log("resolve", "gone", key=inc["key"], incident_id=inc["id"], now=self.now)
        for res in self.db.live_resources():
            try:
                self.one(res)
            except Exception as e:  # noqa: BLE001 - one bad resource must not stop the rest
                self.db.log("error", "ladder: %s: %s" % (type(e).__name__, e), key=res["key"], ok=False, now=self.now)

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
                            now=self.now)
            return
        names = sorted({f["rule"] for f in owner_f})
        if inc is None:
            inc = self.db.new_incident(res["key"], names, owner_f[0]["severity"], self.now)
            self.db.log("open", "; ".join(f["text"] for f in owner_f), key=res["key"], incident_id=inc["id"],
                        now=self.now)
        else:
            self.db.update_incident(inc["id"], rules=json.dumps(names))  # also refreshes updated_at
            inc = self.db.incident(inc["id"])
        self.advance(inc, res, owner_f)

    # ------------------------------------------------------------------ owner path
    def ensure_owner(self, res):
        if res["owner_session"] or res["ownership"] == "foreign":
            return res, None
        if res["owner_checked_at"] and self.now - res["owner_checked_at"] < 1800:
            return res, None
        upd = owner.resolve(self.db, self.cfg, res, self.now, book=self.book)
        cands = upd.pop("_candidates", None)
        if upd:
            self.db.update_resource(res["key"], **upd)
            self.db.log("owner", upd.get("owner_evidence") or "-", key=res["key"], now=self.now)
        return self.reload(res), cands

    def advance(self, inc, res, findings):
        crash = any(f["rule"] == "crash_loop" for f in findings)
        wait_min = self.cfg["rules"]["crash_loop"]["owner_wait_min"] if crash else self.cfg["ladder"]["owner_wait_min"]
        wait = wait_min * 60
        state = inc["state"]
        if state == "open":
            res, cands = self.ensure_owner(res)
            if not res["owner_session"] and not inc["investigated_at"]:
                res = self.investigate(inc, res, findings, cands)
            if res["owner_session"]:
                if owner.is_live(self.book, res["owner_session"]):
                    self.n.ping(inc, res, self.ping_text(inc, res, findings, wait_min, crash))
                    self.db.update_incident(inc["id"], state="asked", ping_count=1, last_ping_at=self.now,
                                            deadline_at=self.now + wait)
                else:
                    self.n.ping(inc, res, messages.resume_turn(self.ping_text(inc, res, findings, wait_min, crash)),
                                resume=True)
                    reason = ("the session that launched it, **%s** (`%s`), has exited. I resumed it for one turn to ask, "
                              "and am emailing you at the same time; whichever answers first wins." % (
                                  res["owner_name"] or "?", res["owner_session"]))
                    self.escalate(inc, res, findings, reason, crash, state_after="asked", resumed=True)
            else:
                reason = "I could not tell which session launched it (%s)." % (res["owner_evidence"] or "no evidence")
                self.escalate(inc, res, findings, reason, crash)
            return
        if self.now < (inc["deadline_at"] or 0):
            return
        if state == "asked":
            live = res["owner_session"] and owner.is_live(self.book, res["owner_session"])
            if (not crash and live and not inc["resumed_at"] and inc["ping_count"] < 2
                    and inc["ping_count"] < self.cfg["ladder"]["max_pings"]):
                self.n.ping(inc, res, self.ping_text(inc, res, findings, wait_min, crash))
                self.db.update_incident(inc["id"], ping_count=inc["ping_count"] + 1, last_ping_at=self.now,
                                        deadline_at=self.now + wait)
                return
            if crash and self.try_auto_stop(inc, res, findings):
                return
            if inc["email_count"]:
                self.db.update_incident(inc["id"], state="escalated")
                return
            reason = "I messaged **%s** at %s and got no answer by %s." % (
                res["owner_name"] or res["owner_session"], util.iso(inc["last_ping_at"], local=True),
                util.iso(inc["deadline_at"], local=True))
            self.escalate(inc, res, findings, reason, crash)
            return
        if state == "escalated":
            if crash and self.try_auto_stop(inc, res, findings):
                return
            sig = ",".join(sorted({f["rule"] for f in findings}))
            new_crash = crash and "crash_loop" not in (inc["last_email_sig"] or "")
            stale = self.now - (inc["last_email_at"] or 0) >= self.cfg["ladder"]["reemail_hours"] * 3600
            if new_crash or (stale and sig):
                self.escalate(inc, res, findings, "still unresolved since %s." % util.iso(inc["opened_at"], local=True),
                              crash)

    def ping_text(self, inc, res, findings, wait_min, crash):
        stoppable = crash and policy.can_stop(res, "watchdog", self.cfg, self.now, fresh_crash=True)[0]
        return messages.ping(inc, res, findings, self.now, wait_min, stoppable, self.cfg)

    def investigate(self, inc, res, findings, cands):
        self.db.update_incident(inc["id"], investigated_at=self.now)
        if not self.live:
            self.db.log("investigate", "would run the investigator (owner unknown)", key=res["key"],
                        incident_id=inc["id"], shadow=True, now=self.now)
            return res
        verdict, status = investigator.run(self.db, self.cfg, res, findings, cands, self.now)
        if verdict and verdict.get("owner_session_id") and verdict.get("confidence") in ("high", "medium"):
            entry = self.book.get(verdict["owner_session_id"]) or {}
            self.db.update_resource(res["key"], owner_session=verdict["owner_session_id"],
                                    owner_cli=entry.get("cli") or "claude",
                                    owner_name=entry.get("name") or verdict.get("owner_name"), owner_source="model",
                                    owner_confidence=verdict["confidence"],
                                    owner_evidence="investigator: %s" % verdict.get("evidence", ""))
        elif verdict:
            self.db.update_resource(res["key"], owner_evidence="%s | investigator: %s" % (
                res["owner_evidence"] or "", verdict.get("summary") or verdict.get("evidence") or ""))
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

    def escalate(self, inc, res, findings, reason, crash, state_after="escalated", resumed=False):
        wait = self.cfg["rules"]["crash_loop"]["owner_wait_min"] * 60 if crash else self.cfg["ladder"]["owner_wait_min"] * 60
        ok_stop, _ = policy.can_stop(res, "watchdog", self.cfg, self.now, fresh_crash=True) if crash else (False, "")
        stop_at = (self.now + wait) if crash and ok_stop else None
        subject, md = messages.escalation(inc, res, findings, self.now, reason,
                                          self.recommendation(res, findings, crash), crash, stop_at, cfg=self.cfg)
        self.n.email(subject, md, key=res["key"], incident_id=inc["id"])
        upd = {"state": state_after, "email_count": (inc["email_count"] or 0) + 1, "last_email_at": self.now,
               "last_email_sig": ",".join(sorted({f["rule"] for f in findings}))}
        if not inc["deadline_at"] or self.now >= inc["deadline_at"]:
            upd["deadline_at"] = self.now + wait
        if resumed:
            upd["resumed_at"] = self.now
        self.db.update_incident(inc["id"], **upd)

    def try_auto_stop(self, inc, res, findings):
        ok, reason = policy.can_stop(res, "watchdog", self.cfg, self.now, fresh_crash=True)
        if not ok:
            self.db.log("auto-stop-skip", reason, key=res["key"], incident_id=inc["id"], now=self.now)
            return False
        # Re-check the logs right now; never act on a stale observation.
        acct = self.cfg["accounts"][res["account"]]
        okc, detail = gather.op(res["account"], acct, "crash_check",
                                {"window_min": self.cfg["rules"]["crash_loop"]["window_min"]}, res["native_id"])
        fresh = okc and observe.crash_candidate(detail, self.cfg["rules"]["crash_loop"])
        ok, reason = policy.can_stop(res, "watchdog", self.cfg, self.now, fresh_crash=fresh)
        if not ok:
            self.db.log("auto-stop-skip", reason, key=res["key"], incident_id=inc["id"], now=self.now)
            return False
        stopped, detail = self.n.stop(res, "watchdog", inc["id"])
        subject, md = messages.auto_stopped(inc, res, findings, self.now, stopped, detail, cfg=self.cfg)
        self.n.email(subject, md, key=res["key"], incident_id=inc["id"])
        if stopped:
            self.db.close_incident(inc["id"], "auto-stopped", self.now)
        return stopped

    # ------------------------------------------------------------------ paths that only email the human
    def cost_email(self, res, f):
        if f["tier"] <= (res["cost_email_tier"] or 0):
            return
        inc = self.db.open_incident(res["key"])
        subject, md = messages.cost_email(inc["id"] if inc else None, res, f, self.now, cfg=self.cfg)
        self.n.email(subject, md, key=res["key"], incident_id=inc["id"] if inc else None)
        self.db.update_resource(res["key"], cost_email_tier=f["tier"])

    def foreign_crash(self, res):
        k = "foreign_crash:" + res["key"]
        last = self.db.meta(k, 0)
        if self.now - last < 24 * 3600:
            return
        subject, md = messages.foreign_crash(res, self.now, cfg=self.cfg)
        self.n.email(subject, md, key=res["key"])
        self.db.set_meta(k, self.now)
