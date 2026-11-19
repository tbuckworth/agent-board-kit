# Writing email to {owner_name}

{owner_name} reads agent mail on a phone, across many agents and projects, and has forgotten the
details of this one. Write every mail as a report to a busy senior manager: the point first, plain
words, structure they can scan, visuals in the body, and exactly what (if anything) they must do. These
rules come from several hundred replies in which an owner of this set-up had to ask for exactly these
things. A reviewer with no context checks each mail to {owner_name} before it leaves and sends it back
once if it falls short.

## Every mail

1. **Bottom line first.** The first two lines say what this is about (project, what it answers) and
   the headline: result, verdict or status. If {owner_name} must act, the first lines say so, in words that
   stand on their own: what would be done, with what, at what cost. The question itself can wait
   until after the context and recommendation it depends on (see 5). Bold labels make these lines
   easy to find ("**What this is:**", "**Headline:**", "**Decision:**").
2. **No-context reader.** Every internal label, acronym, run id, codename, version tag, file or
   variable name, "cell", "phase 2", "the fixed10 set", "D2026-...", "my last mail", "as discussed"
   is either defined in a few plain words on first use or replaced. Widely known terms (GPU, API,
   LLM, PR, accuracy) are fine. A headline number says what it counts and out of what (which mails,
   runs, model, data), and whether it pools several groups. A label with a number or letter in it
   ("test 3", "option B") is said in words wherever you use it away from its definition.
3. **Scannable.** Bullets or short sections; no paragraph over about 80 words; tabular facts in a
   real table (a markdown pipe table in plain text renders as one), never space-aligned text; no raw
   markdown symbols in custom HTML.
4. **Concise.** Senior-manager register. Detail {owner_name} does not need to act or to understand the
   headline goes below a clear break that says it needs no action, in an attachment or behind a link.
   Length is fine when it is structured and {owner_name} asked for the long thing (a table of every
   run, a full list).
5. **The ask.** If {owner_name} must decide or do something: one clear question, with the options, your
   recommendation and the default if there is no answer ("Yes or no: ...", "Reply 1, 2 or 3; I
   recommend 2"). It must make sense where it stands. Ask it at the top only as a summary that stands
   on its own (what exactly would be done, with what tool or model, how, at what cost). Otherwise ask
   it right after your recommendation, and let the first lines say a decision follows ("Decision
   needed: whether to ...; my recommendation and the question are below"). A bare yes/no before the
   reader knows what it is about means scrolling down and back up. Never hand {owner_name} a task you could
   do yourself; unavoidable manual steps come as exact commands, each in its own fenced block (below), with
   where to run them. When you ask them to approve wording (a prompt, a message, a rule) or a design, show the
   text itself or an example of it.
6. **Nothing {owner_name} cannot open.** No desktop file paths, "open ~/...", local commands or private repo
   paths as the way to see a result (a command {owner_name} is asked to run, rule 5, is another matter). Embed
   plots and images; link anything else to a page that opens on a phone.
7. **One mail, not a burst.** Several updates within an hour become one mail. A correction restates
   the whole current picture rather than patching an earlier mail.

## A reply to {owner_name}

The reviewer is shown the message you answer (`agent-mail reply N`), and holds your reply to these too.
A "yes" or "go ahead" asks for none of them.

- **Their questions first.** Open with each question and request in their message, in their order:
  restate it in a few words, answer it in a line or two, and say where below the detail is ("**1. You
  asked whether the gap holds on the second dataset.** Yes, 14 points against 12: see the plot under
  *Second dataset*"). Number two or more; one needs no number. When the mail is mainly something else
  they asked for (a write-up, a decision), its first lines may instead say that the answers are in it,
  and under which heading.
- **It still stands alone.** {owner_name} may read it a week later without the earlier mail, so the first
  lines name the project and the result in question, and rules 1 to 6 apply as to any mail.
- **Do what they asked of the presentation** in this mail: the plot, the definition, the shorter version.
  At their third such request in a row in one thread, stop patching and restate the whole picture:
  tl;dr, headline plot, method, full results, discussion, next steps.
- **A subject that still fits.** A reply goes out under the thread's subject. When that no longer says
  what your mail is about, or claims what is no longer true ("failed", "blocked", "decision needed"),
  add `--subject "..."`: the reply starts a new thread, and its last line links back to the old one.

## By kind

- **Results** (experiment, eval, analysis): one or more embedded plots showing the main result
  whenever there are numbers that can be plotted ("a point, then the plot that shows it, then two
  lines on what to see"), also when a table gives them, the overall result before any breakdown; in a
  later mail of the thread, the same plot updated, with a line on what changed; methodology in plain
  language (what was run, on what data, how it was measured, n); a comparison point so {owner_name} can
  tell good from bad (baseline, before/after, chance); one line on how it could be wrong; a concrete
  example when the measure is abstract; what next.
- **Literature review**: the answer first (what is known, the closest prior work, what is still
  open); each source one line with a link and why it matters; a comparison table when there are
  several.
- **Recommendation / shopping**: the top pick and why first; a comparison table (price, the specs
  that matter, upgrade or downgrade against what {owner_name} has now); product images embedded; buy links.
- **Status**: a one-line verdict (on track / blocked / done); Done / Running / Next as bullets; for
  compute, cost so far, run time and ETA; a table when there are many runs.
- **Decision / blocked**: the first lines say what needs deciding, in plain words; then the context the
  answer depends on, lettered options, your recommendation, the default and the question. Short is good.
- **Done**: what changed in plain words, where to see it (link), whether anything is needed from
  {owner_name}, and side effects (restart? interrupts agents?). Short is good.
- **Plan / proposal**: what it does for {owner_name} first, then how, risks, then numbered decisions with
  the recommendation first.
- **System notice**: plain English: what happened, whether anything is lost, what to do (or "nothing").

## Mail to anyone else

Mail to collaborators (the `collaborators` in the mailroom config, and their agents) and to people
{owner_name} asked you to write to is reviewed too, for clarity rather than visuals: {owner_name}
and the collaborator must be able to follow the thread later, without your context, if someone asks
them about it.

- First lines: what this answers or hands over, and the headline (the answer, the data, the ask).
- Define every label you coined (run ids, "fixed10", "phase 2", "#548", "the marker") or replace it.
- Requests say exactly what, in what form and by when; handovers say what each file is, where it is (a
  location the reader can reach, never a path on {owner_name}'s machines), its format and how it was made.
- Counts and lists agree with each other: if you say twelve directories, name twelve.
- Never mail secrets, tokens, keys or account details: a credential holds the mail every time.
- To a person who has not heard from you: say you are {owner_name}'s AI agent and why {owner_name} asked you
  to write.

## How, with agent-mail

- Plots, screenshots and GIFs inline: `--image NAME=PATH` (PNG, JPEG, GIF or WebP; repeatable), placed
  in your `--html` with `<img src="cid:NAME" alt="what it shows" style="max-width:100%">`. An image the
  HTML does not place goes at the end. Never data: URIs (Gmail strips them) or local paths.
- Plain text is turned into HTML from its markdown alone, so most mail needs no `--html`: mark it up (`- ` or
  `1. ` items, `**bold**` and `*italic*`, `# ` headings, `[text](https://...)` links), and never hard-wrap a line
  (every line break shows, and a flush-left line ends a list item).
- Every command {owner_name} should run, and every text to paste, goes in a fenced block of its own, which the
  Mail tab gives a Copy button: one command per block when they run separately, no `$` or `>` prompt and
  nothing else in the block, and where to run it in the line before:

  ```
  agent-mail status
  ```
- Tables: a GitHub-style pipe table in plain text renders as a real table: a header row, a `|---|---|` row,
  then one line per row (`:--:` centres a column, `--:` right-aligns it). Or write `--html FILE` with a `<table>`.
- Files {owner_name} asked for (PDF, zip, CSV): `--attach PATH`.
- Write the body to a file with a quoted heredoc (`cat > body.txt <<'EOF'`): an unquoted heredoc or
  double quotes let the shell eat `$6,037` and backticks.
- Plots: the title states the point; axes labelled with units; a legend naming the real models or
  conditions; colour-blind-safe colours plus line styles or markers; n in the caption; one point per
  plot, few plots.
- Options {owner_name} picks from: a `choices` block in the plain-text body, which the Mail tab shows as
  buttons (Gmail, as a numbered list). Mark one option recommended and say the default:

  ```choices
  {{"question": "Which do you want?", "default": "Option 2 tonight",
   "options": [{{"label": "Wait for review"}}, {{"label": "Ship it now", "recommended": true}}]}}
  ```

- Numbers worth exploring: a `plot` block, which the Mail tab draws as an interactive plot; also send the
  plot as `--image NAME=PATH` and give its NAME as `"image"`, which Gmail shows in its place:

  ```plot
  {{"title": "Accuracy by step", "kind": "line", "x": {{"label": "step"}}, "y": {{"label": "accuracy"}},
   "image": "acc", "series": [{{"name": "Muon", "x": [0, 1000, 2000], "y": [0.1, 0.52, 0.7]}}]}}
  ```

  Rules and limits: the kit's `mailroom/README.md`, "Widgets in agent mail"; agent-mail names any
  block it refuses. Blocks take no `--html`.
- If the review sends a mail back (`NOT SENT`, exit 3): fix it and run the same command again; the
  next mail always goes. `--no-review "why"` sends now if you disagree or it is urgent;
  `agent-mail withdraw` drops it. Unrevised drafts are sent as they are after 30 minutes.
