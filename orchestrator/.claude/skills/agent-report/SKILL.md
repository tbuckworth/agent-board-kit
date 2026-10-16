---
name: agent-report
description: Send {{OWNER_NAME}} an HTML status email about what every agent on this machine is doing. Use when asked "what are the agents up to", "status report", "how are my agents", or when an email asks for an overview.
---

# Agent status report

The facts are gathered with no model. You add a short narrative.

1. Read the facts:

   ```bash
   agent-status report --format text --since 24h
   ```

   Widen `--since` (for example `3d`) if asked about a longer period.

2. Write two to four sentences of narrative to a file. Lead with what needs {{OWNER_NAME}}: a session
   waiting for input, a quota event, held mail. Then anything that looks wrong: two sessions on
   one branch, a session idle for days with uncommitted changes, a spawned agent with no notes.
   Then the one-line gist of what the busy sessions are doing, from their notes. Plain prose, no
   bullet points, no headings.

   ```bash
   cat > "${TMPDIR:-/tmp}/narrative.txt" <<'TXT'
   ...
   TXT
   ```

3. Send it. The `--session orchestrator` flag tags the mail so the reply comes back to you.

   ```bash
   agent-status report --format html --since 24h --narrative "${TMPDIR:-/tmp}/narrative.txt" \
     --email {{OWNER_EMAIL}} --session orchestrator
   ```

4. If the request came by email, also `agent-mail reply N` with one line saying the report was
   sent. If it came from a peer session, `agent-send` one line back.

You run with a fresh context per event, so there is no waiting for other agents to answer: the
report is the facts plus your narrative, sent in this same run.
