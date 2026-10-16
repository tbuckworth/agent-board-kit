"""Fallback owner search by a model, used only when the deterministic search finds nobody confident.

A fresh `claude -p`: read-only tools, no MCP servers, no session persistence, no provider keys in
its environment, `--max-budget-usd` per run and a daily cap checked here. It returns a verdict; it
never stops anything itself.
"""
import json
import re

from . import config, creds, messages, util

PROMPT = """You are cloud-watchdog's investigator. A paid cloud resource is flagged, and the cheap
deterministic search could not tell which agent session launched it. Work out the most likely owner.

Resource:
{facts}

Flagged because: {why}
Deterministic search said: {evidence}
Candidate sessions from transcript search (may be empty): {candidates}

Useful, all read-only: `agent-send --list --json` (live and recent sessions, their cwd), `agent-status report
--since 7d` (what each agent has been doing), `agent-mail sent --limit 200` (mail history), Grep/Read over
~/.claude/projects/*/*.jsonl and ~/.codex/sessions (transcripts; look for the launch command), and
`git -C <repo> log` in the repos under {repos_root}. Be quick: a few searches, not an essay. Do not run
anything else.

Answer with ONLY a JSON object, no prose around it:
{{"owner_session_id": "<uuid or null>", "owner_name": "<session name or null>", "confidence": "high|medium|low",
  "evidence": "<one or two sentences>", "recommendation": "stop|keep|ask_human", "summary": "<one sentence for {human}>"}}
"""

ALLOWED = ["Read", "Grep", "Glob", "Bash(agent-send --list:*)", "Bash(agent-status report:*)",
           "Bash(agent-status log:*)", "Bash(agent-mail sent:*)", "Bash(git -C:*)", "Bash(git log:*)"]


def budget_left(db, cfg, now):
    return cfg["investigator"]["daily_cap_usd"] - db.model_spend_today(now)


def run(db, cfg, res, findings, candidates, now):
    icfg = cfg["investigator"]
    if not icfg.get("enabled"):
        return None, "investigator disabled"
    if budget_left(db, cfg, now) < icfg["max_budget_usd"]:
        return None, "daily model budget used up"
    cands = [{k: c.get(k) for k in ("session_id", "cli", "cwd", "launch_hit", "score")} for c in candidates or []]
    prompt = PROMPT.format(facts="\n".join(messages.facts(res, now)) + "\nname: %s\nkey: %s" % (res["name"], res["key"]),
                           why="; ".join(f["text"] for f in findings), evidence=res["owner_evidence"] or "nothing",
                           candidates=json.dumps(cands), repos_root=cfg.get("repos_root") or "~/code",
                           human=cfg.get("owner_name") or "the owner")
    workdir = config.STATE_DIR / "cloud-watchdog-investigator"
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = ["claude", "-p", "--model", icfg["model"], "--output-format", "json",
           "--max-budget-usd", str(icfg["max_budget_usd"]), "--permission-mode", "dontAsk",
           "--tools", "Read,Grep,Glob,Bash", "--allowedTools"] + ALLOWED + [
           "--strict-mcp-config", "--no-session-persistence",
           "--add-dir", str(config.HOME / ".claude" / "projects"), "--add-dir", str(config.HOME / ".codex" / "sessions"),
           "--add-dir", str(config.expand(cfg.get("repos_root") or "~/code")), "--", prompt]
    code, out, err = util.run(cmd, env=creds.base_env(), timeout=icfg["timeout_s"], cwd=str(workdir))
    cost, verdict = None, None
    try:
        j = json.loads(out)
        cost = j.get("total_cost_usd")
        m = re.search(r"\{.*\}", j.get("result") or "", re.S)
        verdict = json.loads(m.group(0)) if m else None
    except ValueError:
        pass
    db.log("investigate", {"exit": code, "verdict": verdict, "stderr": err.strip()[-200:] if code else ""},
           key=res["key"], cost=cost, ok=verdict is not None, now=now)
    return verdict, ("ok" if verdict else "no verdict (exit %s)" % code)
