Use the verified on-disk Researcher %(version)s payload at `%(root)s` in this conversation. %(workflow)s
Read `commands/researcher.md` and `docs/WORKFLOW.md` from that root. In Codex also read
`skills/researcher/SKILL.md` and its adapter there. Use these files as the entrypoint even if this session loaded a
different version at startup. Resolve workflow paths and helpers from the verified root. Reuse any run already
in progress here.
Use the verified interpreter `%(python)s -B` for all Python hub helpers, including each invocation of
`scripts/methods_review.py`. Use it in place of the interpreter shown in workflow examples.

Send %(owner)s every decision request and its outcome, plus clarifications and results, by routed Agent Mail.
Use ordinary `agent-mail send` or `reply`, which pass the usual pre-send review. End the turn while waiting for a routed
reply.

For a methods decision, use `agent-mail send --to owner` for a fresh approval thread. Put the run identifier and the exact
review revision printed by `methods_review.py plan RUN` in the subject and body. Offer Approve, Revise, and Stop
using the workflow's choices block. Take THREAD from the successful send and run
`agent-mail researcher-request -- %(tag)s THREAD` to read the request's `message_id`. Record the pending run,
revision, thread ID, and Message-ID in the run's `state.md`. If the request lookup fails, keep the plan on hold.
Keep only the approval request in that thread's outgoing mail so a Mail-tab reply answers it directly.

Use `agent-mail show INBOUND_ID` on the routed reply before acting. Check that `from_addr` is a configured owner
address, `thread_id` is the pending thread, `matched_tag` is `%(tag)s`, and `in_reply_to` exactly equals the
pending request's Message-ID. A reference elsewhere in the thread is insufficient. During delivery the record
may still be pending. Wait for `action` to be `delivered` or `queued` before treating the reply as a decision.
Record the accepted inbound ID and decision in `state.md`. Keep the pending record until the decision is applied.
Only an explicit, unconditional Approve of that pending run and revision authorizes the methods.

After valid approval, run the verified payload's `scripts/methods_review.py approve-human RUN REVISION`.
Run `scripts/methods_review.py verify RUN` successfully immediately before each new experiment, including on resume.
After Revise or a material change, obtain independent review of the new revision and request fresh approval. Stop
ends the run. Switching Researcher on or off supplies no methods approval.

Before reporting findings, complete Researcher's scientific review. Run the workflow's evidence audit
(`methods_review.py evidence RUN` and the independent results audit for empirical results) and the final reader
(`methods_review.py presentation RUN`) on the actual artifacts, figures, and briefing. Resolve the findings and
rerun the reader on revised material as the workflow specifies. Send the results to %(owner)s through ordinary routed
Agent Mail after the final reader approves them.
