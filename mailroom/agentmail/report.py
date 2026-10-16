"""Assemble and render the "what are my agents up to" report. No model calls."""
import datetime
import html
import os
import time

from . import registry, statuslog


def _fmt_age(ts, now=None):
    if not ts:
        return "-"
    d = int((now or time.time()) - ts)
    if d < 90:
        return "%ds" % d
    if d < 5400:
        return "%dm" % (d // 60)
    if d < 172800:
        return "%.1fh" % (d / 3600)
    return "%dd" % (d // 86400)


def _fmt_ts(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%a %H:%M") if ts else "-"


def gather(since_seconds=86400, now=None):
    now = now or time.time()
    since = now - since_seconds
    events = statuslog.read(since=since)
    by_sid = {}
    for e in events:
        by_sid.setdefault(e.get("session_id") or "", []).append(e)

    sessions = {}
    for s in registry.all_sessions():
        if s["live"] or s["id"] in by_sid:
            sessions[s["id"]] = dict(s)
    for sid, evs in by_sid.items():
        if sid and sid not in sessions:
            last = evs[-1]
            sessions[sid] = {"cli": last.get("cli", "?"), "name": last.get("name", ""), "id": sid,
                             "cwd": last.get("cwd", ""), "live": False, "status": "exited",
                             "kind": "", "updated_at": last.get("ts", 0)}

    rows = []
    for sid, s in sessions.items():
        evs = by_sid.get(sid, [])
        idles = [e for e in evs if e.get("event") == "idle"]
        notes = [e for e in evs if e.get("event") == "note"]
        quota = [e for e in evs if e.get("event") == "quota"]
        facts = statuslog.git_facts(s.get("cwd", ""))
        last_ts = max([e.get("ts", 0) for e in evs] + [s.get("updated_at") or 0])
        status = s.get("status") or ("live" if s["live"] else "exited")
        if s.get("waiting_for"):
            status = "waiting: " + s["waiting_for"]
        rows.append({
            "cli": s["cli"], "name": s.get("name") or "", "id": sid, "cwd": s.get("cwd", ""),
            "live": s["live"], "status": status, "kind": s.get("kind", ""),
            "branch": facts["branch"], "dirty": facts["dirty"], "last_commit": facts["last_commit"],
            "repo": facts["repo"] or os.path.basename(s.get("cwd", "") or ""),
            "turns": len(idles), "last_idle": idles[-1]["ts"] if idles else None,
            "last_activity": last_ts, "notes": notes[-6:], "quota": len(quota),
            "title": s.get("title", ""),
        })
    rows.sort(key=lambda r: (not r["live"], -(r["last_activity"] or 0)))

    held = None
    try:
        from .config import DB_PATH
        if DB_PATH.exists():
            from .store import Store
            held = Store(DB_PATH).counts().get("held", 0)
    except Exception:
        held = None

    unattributed = [e for e in by_sid.get("", []) if e.get("event") == "note"]
    return {
        "host": registry.HOST, "generated": now, "since_seconds": since_seconds,
        "rows": rows, "held_mail": held, "unattributed_notes": unattributed[-10:],
        "live": sum(1 for r in rows if r["live"]),
        "waiting": sum(1 for r in rows if r["status"].startswith("waiting")),
        "quota_events": sum(r["quota"] for r in rows),
    }


def render_text(rep):
    now = rep["generated"]
    out = ["Agents on %s · %s · window %s" % (rep["host"], _fmt_ts(now), _fmt_age(now - rep["since_seconds"], now)),
           "live %d · waiting on you %d · held mail %s · quota events %d" % (
               rep["live"], rep["waiting"], rep["held_mail"] if rep["held_mail"] is not None else "?", rep["quota_events"]),
           ""]
    for r in rep["rows"]:
        head = "%-6s %-34s %-10s %s" % (r["cli"], (r["name"] or r["id"][:8])[:34], r["status"][:10],
                                        ("%s@%s" % (r["repo"], r["branch"])) if r["branch"] else r["cwd"])
        out.append(head)
        bits = []
        if r["dirty"]:
            bits.append("%d changed" % r["dirty"])
        if r["last_commit"]:
            bits.append("last commit: " + r["last_commit"])
        bits.append("turns %d" % r["turns"])
        bits.append("idle since %s" % _fmt_age(r["last_idle"], now) if r["last_idle"] else "activity %s ago" % _fmt_age(r["last_activity"], now))
        out.append("       " + " · ".join(bits))
        for n in r["notes"]:
            out.append("       %s  %s" % (_fmt_ts(n["ts"]), n.get("text", "")))
        out.append("")
    if rep["unattributed_notes"]:
        out.append("Notes with no session:")
        for n in rep["unattributed_notes"]:
            out.append("  %s  %s  (%s)" % (_fmt_ts(n["ts"]), n.get("text", ""), n.get("cwd", "")))
    return "\n".join(out)


def render_markdown(rep):
    now = rep["generated"]
    out = ["# Agents on %s" % rep["host"],
           "",
           "_%s · live %d · waiting on you %d · held mail %s · quota events %d_" % (
               _fmt_ts(now), rep["live"], rep["waiting"],
               rep["held_mail"] if rep["held_mail"] is not None else "?", rep["quota_events"]),
           "",
           "| CLI | Session | Status | Repo @ branch | Changed | Turns | Idle | Last commit |",
           "|---|---|---|---|---:|---:|---|---|"]
    for r in rep["rows"]:
        out.append("| %s | %s | %s | %s | %s | %d | %s | %s |" % (
            r["cli"], r["name"] or r["id"][:8], r["status"],
            ("%s @ %s" % (r["repo"], r["branch"])) if r["branch"] else r["cwd"],
            r["dirty"] if r["dirty"] is not None else "", r["turns"],
            _fmt_age(r["last_idle"], now) if r["last_idle"] else "-", r["last_commit"].replace("|", "/")))
    notes = [(r, n) for r in rep["rows"] for n in r["notes"]]
    if notes:
        out += ["", "## Notes", ""]
        for r, n in notes:
            out.append("- **%s** %s — %s" % (r["name"] or r["id"][:8], _fmt_ts(n["ts"]), n.get("text", "")))
    return "\n".join(out)


def render_html(rep, narrative=None):
    now = rep["generated"]
    e = html.escape
    css_td = "padding:6px 10px;border-bottom:1px solid #e3e6ea;vertical-align:top;font-size:14px"
    css_th = "text-align:left;padding:6px 10px;border-bottom:2px solid #18212c;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#5e6975"
    parts = ['<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#18212c;max-width:900px">',
             '<h2 style="margin:0 0 4px;font-size:22px">Agents on %s</h2>' % e(rep["host"]),
             '<p style="margin:0 0 16px;color:#5e6975;font-size:13px">%s &middot; live <b>%d</b> &middot; waiting on you <b>%d</b> &middot; held mail <b>%s</b> &middot; quota events <b>%d</b></p>' % (
                 e(_fmt_ts(now)), rep["live"], rep["waiting"],
                 rep["held_mail"] if rep["held_mail"] is not None else "?", rep["quota_events"])]
    if narrative:
        parts.append('<div style="padding:10px 14px;border-left:3px solid #2953a6;background:#f4f6f9;margin:0 0 16px;font-size:15px">%s</div>' % narrative)
    parts.append('<table style="border-collapse:collapse;width:100%%"><thead><tr>%s</tr></thead><tbody>' % "".join(
        '<th style="%s">%s</th>' % (css_th, h) for h in ("CLI", "Session", "Status", "Repo @ branch", "Changed", "Turns", "Idle", "Last commit")))
    for r in rep["rows"]:
        colour = "#2e7d4f" if r["live"] and not r["status"].startswith("waiting") else ("#c23a30" if r["status"].startswith("waiting") else "#5e6975")
        parts.append("<tr>" + "".join('<td style="%s">%s</td>' % (css_td, c) for c in (
            e(r["cli"]),
            "<b>%s</b>%s" % (e(r["name"] or r["id"][:8]), ('<br><span style="color:#5e6975;font-size:12px">%s</span>' % e(r["title"][:60])) if r.get("title") and not r["name"] else ""),
            '<span style="color:%s">%s</span>' % (colour, e(r["status"])),
            e(("%s @ %s" % (r["repo"], r["branch"])) if r["branch"] else r["cwd"]),
            '<span style="font-variant-numeric:tabular-nums">%s</span>' % (r["dirty"] if r["dirty"] is not None else ""),
            '<span style="font-variant-numeric:tabular-nums">%d</span>' % r["turns"],
            e(_fmt_age(r["last_idle"], now) if r["last_idle"] else "-"),
            '<span style="font-family:Menlo,Consolas,monospace;font-size:12px">%s</span>' % e(r["last_commit"]),
        )) + "</tr>")
        if r["notes"]:
            parts.append('<tr><td colspan="8" style="%s;padding-top:0;color:#3b4450">%s</td></tr>' % (
                css_td, "<br>".join('<span style="color:#5e6975;font-size:12px">%s</span> %s' % (e(_fmt_ts(n["ts"])), e(n.get("text", ""))) for n in r["notes"])))
    parts.append("</tbody></table>")
    if rep["unattributed_notes"]:
        parts.append('<h3 style="font-size:15px;margin:18px 0 6px">Notes with no session</h3><ul style="margin:0;padding-left:18px;font-size:14px">')
        for n in rep["unattributed_notes"]:
            parts.append("<li><span style=\"color:#5e6975;font-size:12px\">%s</span> %s <span style=\"color:#5e6975\">(%s)</span></li>" % (e(_fmt_ts(n["ts"])), e(n.get("text", "")), e(n.get("cwd", ""))))
        parts.append("</ul>")
    parts.append('<p style="margin:18px 0 0;color:#9aa4b1;font-size:12px">Generated by agent-status with no model involved. Reply to this mail to reach the orchestrator.</p></div>')
    return "\n".join(parts)
