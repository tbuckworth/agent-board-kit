# Writing email to {{OWNER_NAME}}

{{OWNER_NAME}} reads agent mail on a phone, across many agents and projects, and has forgotten the
details of this one. Write every mail as a report to a busy senior manager: the point first, plain
words, structure they can scan, visuals in the body, and exactly what (if anything) they must do. These
rules come from several hundred replies in which an owner of this set-up had to ask for exactly these
things. A reviewer with no context checks each mail to {{OWNER_NAME}} before it leaves and sends it back
once if it falls short.

## Every mail

1. **Bottom line first.** The first two lines say what this is about (project, what it answers) and
   the headline: result, verdict or status. If they must act, the first lines say so, in words that
   stand on their own: what would be done, with what, at what cost. The question itself can wait
   until after the context and recommendation it depends on (see 5).
2. **No-context reader.** Every internal label, acronym, run id, codename, version tag, file or
   variable name, "cell", "phase 2", "the fixed10 set", "D2026-...", "my last mail", "as discussed"
   is either defined in a few plain words on first use or replaced. Widely known terms (GPU, API,
   LLM, PR, accuracy) are fine.
3. **Scannable.** Bullets or short sections; no paragraph over about 80 words; tabular facts in a
   real HTML table, never pipe or space-aligned text; no raw markdown symbols in custom HTML.
4. **Concise.** Senior-manager register. Detail they do not need to act or to understand the
   headline goes below a clear break, in an attachment or behind a link. Length is fine when it is
   structured and they asked for the long thing (a table of every run, a full list).
5. **The ask.** If they must decide or do something: one clear question, with the options, your
   recommendation and the default if they do not answer ("Yes or no: ...", "Reply 1, 2 or 3; I
   recommend 2"). It must make sense where it stands. Ask it at the top only as a summary that stands
   on its own (what exactly would be done, with what tool or model, how, at what cost). Otherwise ask
   it right after your recommendation, and let the first lines say a decision follows ("Decision
   needed: whether to ...; my recommendation and the question are below"). A bare yes/no before they
   know what it is about makes them scroll down and back up. Never hand them a task you could do
   yourself; unavoidable manual steps come as exact copy-paste commands with where to run them.
6. **Nothing they cannot open.** No desktop file paths, "open ~/...", local commands or private repo
   paths as the way to see a result. Embed plots and images; link anything else to a page they can
   open on a phone.
7. **One mail, not a burst.** Several updates within an hour become one mail. A correction restates
   the whole current picture rather than patching an earlier mail.

## By kind

- **Results** (experiment, eval, analysis): one or more embedded plots showing the main result
  whenever there are numbers that can be plotted ("a point, then the plot that shows it, then two
  lines on what to see"); methodology in plain language (what was run, on what data, how it was
  measured, n); a comparison point so they can tell good from bad (baseline, before/after, chance);
  one line on how it could be wrong; a concrete example when the measure is abstract; what next.
- **Literature review**: the answer first (what is known, the closest prior work, what is still
  open); each source one line with a link and why it matters; a comparison table when there are
  several.
- **Recommendation / shopping**: the top pick and why first; a comparison table (price, the specs
  that matter, upgrade or downgrade against what they have now); product images embedded; buy links.
- **Status**: a one-line verdict (on track / blocked / done); Done / Running / Next as bullets; for
  compute, cost so far, run time and ETA; a table when there are many runs.
- **Decision / blocked**: the first lines say what needs deciding, in plain words; then the context the
  answer depends on, lettered options, your recommendation, the default and the question. Short is good.
- **Done**: what changed in plain words, where to see it (link), whether anything is needed from
  them, and side effects (restart? interrupts agents?). Short is good.
- **Plan / proposal**: what it does for them first, then how, risks, then numbered decisions with the
  recommendation first.
- **System notice**: plain English: what happened, whether anything is lost, what to do (or "nothing").

## Mail to anyone else

Mail to collaborators (the `collaborators` in the mailroom config, and their agents) and to people
{{OWNER_NAME}} asked you to write to is reviewed too, for clarity rather than visuals: {{OWNER_NAME}}
and the collaborator must be able to follow the thread later, without your context, if someone asks
them about it.

- First lines: what this answers or hands over, and the headline (the answer, the data, the ask).
- Define every label you coined (run ids, "fixed10", "phase 2", "#548", "the marker") or replace it.
- Requests say exactly what, in what form and by when; handovers say what each file is, where it is (a
  location the reader can reach, never a path on {{OWNER_NAME}}'s machines), its format and how it was
  made.
- Counts and lists agree with each other: if you say twelve directories, name twelve.
- Never mail secrets, tokens, keys or account details: a credential holds the mail every time.
- To a person who has not heard from you: say you are {{OWNER_NAME}}'s AI agent and why they asked you
  to write.

## How, with agent-mail

- Plots, screenshots and GIFs inline: `--image NAME=PATH` (PNG, JPEG, GIF or WebP; repeatable), placed
  in your `--html` with `<img src="cid:NAME" alt="what it shows" style="max-width:100%">`. An image the
  HTML does not place goes at the end. Never data: URIs (Gmail strips them) or local paths.
- Tables: write `--html FILE` with a real `<table>`; pipe tables in plain text do not render.
- Files they asked for (PDF, zip, CSV): `--attach PATH`.
- Write the body to a file with a quoted heredoc (`cat > body.txt <<'EOF'`): an unquoted heredoc or
  double quotes let the shell eat `$6,037` and backticks.
- Plots: the title states the point; axes labelled with units; a legend naming the real models or
  conditions; colour-blind-safe colours plus line styles or markers; n in the caption; one point per
  plot, few plots.
- If the review sends a mail back (`NOT SENT`, exit 3): fix it and run the same command again; the
  next mail always goes. `--no-review "why"` sends now if you disagree or it is urgent;
  `agent-mail withdraw` drops it. Unrevised drafts are sent as they are after 30 minutes.
