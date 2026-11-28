// The text a reply sends, put together on the phone as the Mac puts it together: this file is the phone's copy of
// ReplyText (mac/Sources/AgentBoard/Compose.swift), which owns the form, and of MailWidgets.answer (MailWidgets.swift),
// which takes an answer from widgets.js. The two copies are checked on the same cases,
// mac/Sources/AgentBoard/reply-text-cases.json: the Mac's --selftest-mail runs its own on them, and
// tests/test_mailweb.py runs this file on them under node. Change the form in both copies and in the cases, together.
//
// The agent's words are always quoted, as Markdown quote lines ("> "), and the owner's never. First each answered
// question, in the order its widget comes in the thread: the question and each option picked ("> 2. LFU with ageing,
// 512 MB") quoted, then "My answer: option 2" ("options 1 and 3" for several), and "My comment: COMMENT" when one was
// written; then each comment on text (the Mac's; the phone makes none), as the quote with the comment on the next
// line; then what was typed. A blank line comes between them, and an empty comment adds nothing.
//
// An answer (a DraftPick, as the Mac's) is {widget, picked, question, labels, comment}: the widget's id ("s<sent
// id>-<n>"), the options picked (indexes from 0, ascending), the question and the picked options' labels from the
// widget's spec, made plain, and the owner's comment on it, null until one is asked for. Lines and characters are as
// Swift counts them: white space and new lines by the first character of each grapheme cluster (Intl.Segmenter), and
// trimming by Unicode's White_Space, character by character, as ReplyText.trim trims.
"use strict";

const ReplyText = (() => {
  // characters that are neither white space nor ignorable but show as a blank: the braille blank and the Hangul
  // fillers (ReplyText.blanks)
  const BLANKS = /[\u2800\u115F\u1160\u3164\uFFA0]/gu;
  const IGNORABLE = /\p{Default_Ignorable_Code_Point}/gu;
  const WHITE = /^\p{White_Space}/u;                            // Character.isWhitespace, by its first scalar
  const NEWLINE = /^[\n\u000B\u000C\r\u0085\u2028\u2029]/u;     // Character.isNewline ("\r\n" is one)
  const LINES = /\r\n|[\n\u000B\u000C\r\u0085\u2028\u2029]/u;    // where text splits into lines
  const EDGES = /^\p{White_Space}+|\p{White_Space}+$/gu;
  const GRAPHEMES = new Intl.Segmenter("en", { granularity: "grapheme" });

  // `s` with no white space at either end, white space being Unicode's (ReplyText.trim): not a zero-width space or a
  // byte-order mark, which are invisible but not white space, nor what String.prototype.trim takes.
  function trim(s) {
    return s.replace(EDGES, "");
  }

  // Agent text as a draft takes it (an answer's question and labels), so that nothing in it is sent unseen: each blank
  // a space, no default-ignorable characters (zero-width and bidirectional controls, variation selectors, tag
  // characters), each run of white space one space, and each line trimmed, empty lines left out; `lines` false makes
  // it one line.
  function plain(s, lines) {
    const visible = s.replace(BLANKS, " ").replace(IGNORABLE, "");
    const rows = [[]];
    let word = "";
    const end = () => {
      if (word) rows[rows.length - 1].push(word);
      word = "";
    };
    for (const { segment } of GRAPHEMES.segment(visible)) {
      if (lines && NEWLINE.test(segment)) {
        end();
        rows.push([]);
      } else if (WHITE.test(segment)) {
        end();
      } else {
        word += segment;
      }
    }
    end();
    return rows.map((r) => r.join(" ")).filter((r) => r).join("\n");
  }

  // `text`, trimmed, as Markdown quote lines: "> " before each line, ">" alone for an empty one.
  function quoted(text) {
    return trim(text).split(LINES).map((l) => (l ? "> " + l : ">"));
  }

  // The agent's words of an answer: the question, then each option picked, numbered as in the mail.
  function asked(p) {
    const out = [p.question];
    for (let k = 0; k < Math.min(p.picked.length, p.labels.length); k++) out.push(p.picked[k] + 1 + ". " + p.labels[k]);
    return out;
  }

  // The owner's words of an answer: "My answer: option 2", or "My answer: options 1 and 3".
  function answered(p) {
    const n = p.picked.map((i) => String(i + 1));
    const list = n.length < 2 ? n.join("") : n.slice(0, -1).join(", ") + " and " + n[n.length - 1];
    return "My answer: " + (n.length === 1 ? "option " : "options ") + list;
  }

  // The text sent for a draft's content: {text (trimmed already), picks, comments ({quote, comment})}.
  function compose(c) {
    const note = (s) => {
      const t = trim(s === null ? "" : s);
      return t ? [t] : [];
    };
    const answers = c.picks.map((p) => asked(p).flatMap(quoted).concat([answered(p)],
                                                                         note(p.comment).map((t) => "My comment: " + t)));
    const quotes = c.comments.map((q) => quoted(q.quote).concat(note(q.comment)));
    const typed = c.text ? [[c.text]] : [];
    return answers.concat(quotes, typed).map((lines) => lines.join("\n")).join("\n\n");
  }

  // The text Send sends for a draft ({text as typed, picks, comments}): its text trimmed (Draft.reply).
  function reply(d) {
    return compose({ text: trim(d.text), picks: d.picks, comments: d.comments });
  }

  // Where widget `key` ("s12-3") comes in its thread: the agent's mail's id, as ids grow in the order mail is sent,
  // then the widget's place in it.
  function place(key) {
    return key.slice(1).split("-").map(Number);
  }

  // An answer as widgets.js gives it ({widget, picked, comment}) for one of the thread's `widgets` (mail_show's
  // messages' widgets, never the mail's page), as the draft takes it: {answer, comment}, the answer with the question
  // and the picked options' labels from that widget's spec, made plain (the mail can hide words in a card, as in a
  // run of non-breaking spaces past its edge, but not in the reply), and whether it asks for a comment; null when it
  // is not one: a choices widget of the thread, options it has, each in order once, and a question and labels with
  // something to show.
  function answer(raw, widgets) {
    if (raw === null || typeof raw !== "object" || typeof raw.widget !== "string") return null;
    const w = widgets.find((x) => x.id === raw.widget && x.kind === "choices");
    const spec = w && w.spec;
    if (!spec || typeof spec.question !== "string" || !Array.isArray(spec.options)) return null;
    const picked = raw.picked;
    if (!Array.isArray(picked) || typeof raw.comment !== "boolean") return null;
    if (!picked.every((i, k) => Number.isInteger(i) && i >= 0 && i < spec.options.length && (k === 0 || picked[k - 1] < i))) {
      return null;
    }
    const labels = picked.map((i) => {
      const o = spec.options[i];
      return o !== null && typeof o === "object" && typeof o.label === "string" ? plain(o.label, false) : "";
    });
    const question = plain(spec.question, false);
    if (!question || labels.some((l) => !l)) return null;
    return { answer: { widget: raw.widget, picked: picked.slice(), question: question, labels: labels, comment: null },
             comment: raw.comment };
  }

  return { trim, plain, asked, answered, reply, place, answer };
})();

if (typeof module !== "undefined") module.exports = ReplyText;
