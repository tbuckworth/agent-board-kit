// The Mail tab on the owner's phone, as an installable app (agentboard/mailweb.py serves it; README, "Mail on the
// phone").
// Every call goes to POST api, except an attachment, which download() fetches with GET part/KEY/INDEX; both carry
// X-Agent-Board. Mail needs a session, which a passkey sign-in (fingerprint) gives; a passkey is enrolled only from the
// one-time link the Mac shows (/enrol#CODE). Each reply, and each message to the orchestrator, is approved with the
// passkey again (approved), over a challenge bound to that action and its exact request. Data from the mailbox is put
// on the page as text, never as HTML, except a thread's document, which the desktop has sanitized and which is shown in
// a sandboxed frame that runs no script. An agent's options and plots (mail_show's messages' widgets) are drawn by the
// Mac tab's own renderer (widgets.js), from their data, and its code blocks are listed with a Copy button each, from
// their text; both in a sheet over the thread. Each question answered there is kept in the thread's draft apart from
// the others, as on the Mac, and the reply is put together as the Mac puts it together (replytext.js). The list also
// shows the desktop's login keyring as the Mac's top bar does, when the desktop turns that on, and unlocks it with the
// owner's desktop password, which the passkey approves each time over a nonce made for that submission, and which the
// page keeps nowhere (unlockDesktop).
//
// The page stores no mail on the phone: the lists and the threads read (ahead or opened) live in its memory only, and
// go when it is closed or signs out (forget, which drops answers still on their way too); README, "Mail on the phone",
// says what the phone itself keeps. A thread's draft is kept on the desktop, the same one the Mac shows (README,
// "Drafts"): what is typed goes there a second after typing stops, and at once when the thread or the reply is left or
// the page is hidden, so a draft outlives the page; until the desktop has taken it, it is in the page's memory only
// (as is a message to the orchestrator). The history holds only which view is showing, never a thread: the app opens
// on the list, however it is opened or reloaded, and Android's back gesture leaves a thread for the list, whose place
// is kept, or closes a sheet (CloseWatcher).
//
// Speed: the list asks for itself and the session's status at once, and draws over the skeleton the page starts with;
// the top threads are read ahead (READ_AHEAD, AHEAD_AT_ONCE at a time), so most open without asking the desktop, as on
// the Mac (MailModel's read-ahead and MailCache); each box's last list shows at once when it is picked again; leaving
// a thread asks nothing; a mark (archive, snooze, unread) shows at once and is sent behind it, while fast mode's
// switch waits for the desktop's answer ("Fast…"), since the mailroom says what it did to the agent's effort.
"use strict";

const $ = (id) => document.getElementById(id);
const READ_AHEAD = 4;            // threads at the top of the list read before they are asked for, and 2 below the open one
const AHEAD_AT_ONCE = 2;         // read-ahead requests going at once (the desktop takes 8 from a device)
const CACHE_CHARS = 24e6;        // documents kept in memory: 24 million characters, the least recently used going first
const POLL_MS = 30000;           // the list is read again this often while the app is on screen
const LIST_LIMIT = 100;          // the mailroom's default
const FAST_TICK_MS = 20000;      // fast mode's minutes left are drawn again this often while the app is on screen
const SAVE_MS = 1000;            // a draft is saved on the desktop this long after typing stops
const HEX = /^#[0-9A-Fa-f]{6}$/; // a group colour as the desktop sends it (agentboard/colours.py)

const state = {
  box: "inbox", query: "",
  threads: null, counts: null,   // the list on screen
  openId: null,                  // the thread on screen, or null on the list
  lastId: null,                  // the thread last opened, for a forward step back into it
  shown: null,                   // the mail_show answer in the frame (kept after the thread closes: reopening is instant)
  limits: null,                  // the files a reply carries and a message's length, from status
  signedIn: false,
};
const lists = new Map();         // "box" or "?query" -> {threads, counts}: each list's last answer, shown at once
// thread id -> its draft (newDraft): {text, picks, comments, files: [{id (the SHA-256 of its bytes), name, size}],
// held, open, focus}, as the desktop keeps it, with how this page stands with the desktop's copy (rev, desk, read, ...).
// picks are the owner's answers to the thread's options (replytext.js's), in the order their widgets come in the
// thread, one per question; comments the owner's comments on the agent's text, made on the Mac (the phone makes none,
// but shows and sends them); focus the answer (its widget's id) whose comment field asked for the keyboard, until it
// has it; held the reply
// request (thread, key, text, files) on its way, or sent with no answer saying whether the desktop kept it: until it
// is sent or discarded the draft holds still and Send sends that request again, under its key (send). The desktop's
// draft holds that key too, so the Mac holds it still as well.
const drafts = new Map();
const deskDrafts = new Map();    // thread id -> rev: the threads whose draft the desktop holds (draft_list): marks
const kept = new Map();          // thread id -> {show, n}: threads read, least recently used first
let keptChars = 0;
const fetching = new Map();      // thread id -> {n, promise}: mail_show asked and not answered
let asked = 0;                   // requests numbered as they go
let listAsked = 0;               // the newest list request: an older one's answer is out of date
let changedAt = 0;               // the newest change: a list asked before it is out of date
let changing = 0;                // changes not answered yet: a list answered meanwhile may predate them
let forgotAt = 0;                // the last request asked before forget(): its answer, or an earlier one, is dropped
let sending = false;             // an action is being approved or sent (approved)
let statusAsked = null;          // the status call's promise (the reply limits)
let listedAt = 0;                // when the list was last read
let frameDoc = null;             // the document the frame holds (as framed)
let signinAhead = null;          // {promise, at}: sign-in options asked for as the gate showed
let fastAsked = null;            // the thread whose fast mode is being switched, until the desktop answers
let researcherAsked = null;      // the thread whose Researcher switch is being approved or saved
let researcherProgress = "";
let researcherChangedAt = 0;     // mail_show asked before this may carry an old Researcher setting
const researcherUnconfirmed = new Set();
let orchKey = null;              // the message to the orchestrator's idempotency key, until it is sent or changed

class NotSignedIn extends Error {}
class Refused extends Error {}       // the desktop says it kept nothing of the request (an answer's `refused`)
class NotAsked extends Error {}      // the request never left the phone: its approval failed (approved)

// What a held draft says: the Mac's Draft.frozenWhy, word for word. Change both together: each is checked against
// mac/Sources/AgentBoard/reply-text-cases.json, this one by tests/test_mailweb.py as the page shows it, the Mac's by
// its --selftest-mail.
const FROZEN_WHY = "This reply may have been sent. Send again sends this same reply, and the mailroom keeps it only " +
  "once; until then it cannot change, unless you discard it.";

// Chrome lets keepalive requests on their way at once carry 64 KiB together; the page keeps under it.
const KEEPALIVE_BYTES = 60000;
let keptAlive = 0;               // bytes of keepalive requests on their way

// One API call: `body`'s fields and the action, as one line of JSON. `tail`, given only for keyring_unlock, follows
// that line (the desktop's password, which mailweb.Handler._api_call reads there), outside the JSON a passkey
// approves.
// `outlive`: the call goes on after the page has gone (keepalive), as a draft's save when the page is hidden or closed,
// while the keepalive requests on their way stay under KEEPALIVE_BYTES together; a longer one goes as any call does.
async function api(action, body, tail, outlive) {
  const line = JSON.stringify(Object.assign({ action: action }, body));
  const bytes = new TextEncoder().encode(line).length;
  const alive = !!outlive && keptAlive + bytes < KEEPALIVE_BYTES;
  if (alive) keptAlive += bytes;
  let res;
  try {
    res = await fetch("api", {
      method: "POST",
      headers: { "Content-Type": tail === undefined ? "application/json" : "application/octet-stream",
                 "X-Agent-Board": "1" },
      body: tail === undefined ? line : line + "\n" + tail,
      cache: "no-store",
      keepalive: alive,
    });
  } finally {
    if (alive) keptAlive -= bytes;
  }
  let answer;
  try { answer = await res.json(); } catch (e) { throw new Error("the desktop answered " + res.status); }
  if (res.status === 401) throw new NotSignedIn(answer.error || "not signed in");
  if (!answer.ok) {
    throw new (answer.refused === true ? Refused : Error)(answer.error || "the desktop refused, with no reason");
  }
  return answer;
}

function shown(el, text) {
  el.textContent = text || "";
  el.hidden = !text;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function icon(name, cls) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon" + (cls ? " " + cls : ""));
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#" + name);
  svg.append(use);
  return svg;
}

// The passkey calls, sign and createPasskey, are web/passkeys.js's (the voice page's too).

// ---------------------------------------------------------------- times, as the Mac writes them (MailFmt)

const pad = (n) => String(n).padStart(2, "0");
const clock = (d) => pad(d.getHours()) + ":" + pad(d.getMinutes());
const sameDay = (a, b) => a.toDateString() === b.toDateString();
const weekday = (d) => d.toLocaleDateString(undefined, { weekday: "short" });
const dayMonth = (d, year) => d.getDate() + " " + d.toLocaleDateString(undefined, { month: "short" }) + (year ? " " + d.getFullYear() : "");

// A thread's time in the list: "09:05" today, "Yesterday", "Mon" within six days, "12 Nov", "1 Dec 2025". The Mac has
// the same rule (MailFmt.listTime in mac/Sources/AgentBoard/Mail.swift): change both together.
function listTime(ts, now) {
  const d = new Date(ts * 1000);
  now = now || new Date();
  if (sameDay(d, now)) return clock(d);
  if (d < now) {
    const y = new Date(now);
    y.setDate(y.getDate() - 1);
    if (sameDay(d, y)) return "Yesterday";
    if (now - d < 6 * 86400000) return weekday(d);
  }
  return dayMonth(d, d.getFullYear() !== now.getFullYear());
}

// A time ahead, such as a snooze's: "17:00" today, "Sat 08:00" within six days, else "Mon 30 Nov 08:00". The Mac has
// the same rule (MailFmt.at in mac/Sources/AgentBoard/Mail.swift): change both together.
function aheadTime(ts, now) {
  const d = new Date(ts * 1000);
  now = now || new Date();
  if (sameDay(d, now)) return clock(d);
  return weekday(d) + " " + (d - now < 6 * 86400000 ? "" : dayMonth(d, false) + " ") + clock(d);
}

// A mail's time in full: "Mon 16 Nov, 20:31", as the document heads each mail.
function fullTime(ts) {
  const d = new Date(ts * 1000);
  return weekday(d) + " " + dayMonth(d, d.getFullYear() !== new Date().getFullYear()) + ", " + clock(d);
}

// ---------------------------------------------------------------- signing in and enrolling

function gate(text, button, action) {
  $("gate").hidden = false;
  $("gatetext").textContent = text;
  shown($("gateerror"), "");
  const b = $("gatebutton");
  b.hidden = !button;
  b.textContent = button || "";
  b.onclick = action ? async () => {
    b.disabled = true;
    try { await action(); } catch (e) { shown($("gateerror"), e.message || String(e)); } finally { b.disabled = false; }
  } : null;
}

// The sign-in gate, with a sign-in's options asked for at once, so a tap goes straight to the fingerprint.
function signInGate() {
  signinAhead = { promise: api("signin_begin", {}), at: Date.now() };
  signinAhead.promise.catch((e) => shown($("gateerror"), e.message || String(e)));
  gate("Sign in to read agent mail.", "Sign in with your fingerprint", signIn);
}

// Open the app: always on the list. The enrolment link (/enrol#CODE) opens the enrolment instead.
async function start() {
  const code = location.pathname.endsWith("/enrol") ? location.hash.slice(1) : "";
  if (code) {
    history.replaceState(null, "", "./");           // the code leaves the address bar and the history
    return gate("Enrol this phone for agent mail. You'll be asked for your fingerprint or screen lock.",
                "Enrol this phone", () => enrol(code));
  }
  // An address naming a thread (/#ID), or a reload while a thread was open, lands on the list too.
  if (location.hash || history.state || location.pathname !== "/") history.replaceState(null, "", "./");
  hideThread(true);
  if (!state.threads) $("skeleton").hidden = false;
  const status = statusAsked = api("status", {});
  status.then((st) => {
    state.limits = st.limits;
    // the desktop's limit, felt before the fingerprint; maxlength counts UTF-16 units, never fewer than its characters
    $("orchtext").maxLength = st.limits.message;
    showKeyring(st.keyring);
  }, () => {});
  try {
    await refreshList();                             // asked together with the status
  } catch (e) {
    if (!(e instanceof NotSignedIn)) {
      await status;                                  // throws when the desktop cannot be reached at all
      state.signedIn = true;
      $("gate").hidden = true;
      return shown($("listerror"), e.message);
    }
    state.signedIn = false;
    const st = await status;
    if (!st.enrolled) {
      return gate("No phone is enrolled. On your Mac, right-click Agent Board's menu-bar icon, choose Phone…, then "
                  + "scan its code with this phone.", null, null);
    }
    return signInGate();
  }
  state.signedIn = true;
  $("gate").hidden = true;
}

// Start again (the session has gone, or signing out): what cannot reach the desktop says so on the gate.
function restart() {
  forget();
  return start().catch((e) => gate("The desktop could not be reached: " + (e.message || String(e)), "Try again", start));
}

async function enrol(code) {
  const got = await api("enrol_begin", { code: code });
  await api("enrol_finish", { credential: await createPasskey(got.options) });
  return start();
}

async function signIn() {
  const ahead = signinAhead;
  signinAhead = null;
  let got = null;
  if (ahead && Date.now() - ahead.at < 90000) {       // a challenge lives 120 s
    try { got = await ahead.promise; } catch (e) { got = null; }
  }
  if (!got) got = await api("signin_begin", {});
  await api("signin_finish", { credential: await sign(got.options) });
  return start();
}

async function signOut() {
  await api("signout", {});
  return restart();
}

// Everything read from the desktop, gone from memory (signing out, or a session that has ended).
function forget() {
  forgotAt = asked;                                  // what is still on its way is dropped when it comes (keep, refreshList)
  for (const d of drafts.values()) clearTimeout(d.timer);
  drafts.clear();
  deskDrafts.clear();
  kept.clear();
  keptChars = 0;
  lists.clear();
  researcherUnconfirmed.clear();
  fetching.clear();
  aheadQueue.length = 0;
  state.threads = state.counts = state.shown = state.lastId = null;
  $("text").value = "";
  $("orchtext").value = "";                          // the message to the orchestrator, half written
  orchKey = null;
  if (sheet === $("keyringsheet")) closeSheet();     // which empties its field and lets a waiting unlock go
  showKeyring(null);
  state.signedIn = false;
  frameDoc = null;
  shownSignature = "";
  newFrame("");
  $("threads").replaceChildren();
  $("extras").replaceChildren();
  $("attachments").replaceChildren();
  $("versions").replaceChildren();                   // the drafts' versions and what the desktop said of one
  $("versions").hidden = true;
  shown($("draftnote"), "");
  clearAnswers();
}

// Any call that finds the session gone goes back to signing in; any other failure is said in `errorEl`.
async function guarded(fn, errorEl) {
  try {
    return await fn();
  } catch (e) {
    if (e instanceof NotSignedIn) return restart();
    if (errorEl) shown(errorEl, e.message || String(e));
  }
}

// ---------------------------------------------------------------- the list

const listKey = () => (state.query ? "?" + state.query : state.box);

// Read the list on screen again. Its answer is shown unless a later list or a change overtook it (the Mac's rule:
// MailModel.refresh). Raises what api raises.
async function refreshList() {
  const n = ++asked;
  listAsked = n;
  const key = listKey();
  // with sessions, each thread comes with its group's colours (README, "Group colours"), or groups_error says why not
  const got = await api("mail_list", state.query ? { box: "all", query: state.query, limit: LIST_LIMIT, sessions: true }
                                                 : { box: state.box, limit: LIST_LIMIT, sessions: true });
  if (n !== listAsked || n <= changedAt || n <= forgotAt || changing > 0) return;
  for (const t of got.threads) researcherUnconfirmed.delete(t.id);
  listedAt = Date.now();
  lists.set(key, { threads: got.threads, counts: got.counts });
  showList(got.threads, got.counts);
  shown($("listerror"), "");
  const odd = got.threads.filter((t) => t.colour !== undefined && !(HEX.test(t.colour) && HEX.test(t.shade)));
  shown($("colourerror"), got.groups_error ? "No group colours: " + got.groups_error
    : odd.length ? "No group colours on " + odd.length + " thread(s): a colour that is not #RRGGBB" : "");
  recheckOpen();
  readAhead();
  refreshDrafts();
}

// The list again in the background (a poll, a return to the app, after a change); a failure is said over the list.
function quietRefresh() {
  return guarded(refreshList, $("listerror"));
}

let shownSignature = "";
function showList(threads, counts) {
  state.threads = threads;
  state.counts = counts;
  $("skeleton").hidden = true;
  const c = counts || {};
  shown($("unreadcount"), c.unread ? String(c.unread) : "");
  $("summary").textContent = state.query
    ? threads.length + " thread" + (threads.length === 1 ? "" : "s")
    : (c.inbox || 0) + " in the inbox · " + (c.unread || 0) + " unread · " + (c.snoozed || 0) + " snoozed";
  shown($("listmore"), threads.length >= LIST_LIMIT ? "The newest " + threads.length + "; search to find older mail." : "");
  renderRows();
  showModes();
}

// The rows, drawn again only when something they show has changed (and not under a finger's swipe).
function renderRows(force) {
  if (!state.threads) return;
  if (swipe.mode === "swipe") { swipe.redraw = true; return; }
  const marked = Array.from(new Set([...drafts.keys(), ...deskDrafts.keys()])).filter(hasDraft);
  const signature = JSON.stringify([listKey(), state.threads, marked,
                                   state.threads.map((t) => (t.fast ? fastLabel(t.fast) : ""))]);
  if (!force && signature === shownSignature) return;
  shownSignature = signature;
  const items = state.threads.map(threadItem);
  if (!items.length) items.push(el("li", "empty", state.query ? "No thread has all of these words." : {
    inbox: "Nothing in the inbox.", snoozed: "Nothing snoozed.", archived: "Nothing archived.", all: "No mail yet.",
  }[state.box]));
  $("threads").replaceChildren(...items);
}

// Whether thread `id` has a reply started: its draft here holds something, or the desktop lists one newer than this
// page has read (a draft started on the Mac), and nothing here is unsaved.
function hasDraft(id) {
  const d = drafts.get(id);
  if (d && !blank(d)) return true;
  const listed = deskDrafts.get(id);
  return listed !== undefined && !(d && (unsaved(d) || (d.rev !== null && d.rev >= listed)));
}

// Nothing written, answered, commented or attached.
const blank = (d) => !ReplyText.trim(d.text) && !d.files.length && !d.picks.length && !d.comments.length;

const rowOf = (id) => (state.threads || []).find((t) => t.id === id) || null;

// One thread in the list, as the Mac's MailRow: who, how many, when; the subject; the start of its newest mail.
// Unread is bold and dark with a blue dot; read is plain and greyed, on a grey ground. A thread the desktop sends its
// group's colours with has a strip of its sub-group's shade down its left edge (app.css, li.grouped), and is otherwise
// drawn as any other. "Draft" marks a reply started, and "Fast · 7 min" a thread in fast mode (fastLabel); a snoozed
// thread shows when it comes back. The second dot's place is the Mac's brown busy dot, which the phone does not draw:
// its server has no cheap source of the agents' states (README, "Mail on the phone").
function threadItem(t) {
  const li = el("li", t.unread ? "unread" : "read");
  li.dataset.id = t.id;
  if (HEX.test(t.colour) && HEX.test(t.shade)) {   // any other is named over the list (refreshList)
    li.classList.add("grouped");
    li.style.setProperty("--shade", t.shade);
  }
  const row = el("div", "row");
  const dots = el("span", "dots");
  dots.append(el("i", "dot new"), el("i", "dot"));
  const top = el("div", "top");
  top.append(el("span", "who", (t.agents || []).length ? t.agents.join(", ") : "(no agent)"));
  if (t.count > 1) top.append(el("span", "count", String(t.count)));
  if (hasDraft(t.id)) top.append(el("span", "draftmark", "Draft"));
  if (t.fast) top.append(fastMark(t.fast));
  top.append(el("span", "grow"));
  if (t.attachments) top.append(icon("i-clip", "clip"));
  const snoozed = t.box === "snoozed" && t.snooze_until;
  const at = el("span", "when" + (snoozed ? " snoozed" : ""), snoozed ? "Until " + aheadTime(t.snooze_until)
                                                                      : listTime(t.last));
  top.append(at);
  const main = el("div", "main");
  main.append(top, el("div", "subj", t.subject || "(no subject)"), el("div", "snip", t.snippet || ""));
  if ((state.query || state.box === "all") && t.box === "archived") main.append(el("span", "chip", "Archived"));
  row.append(dots, main);
  li.append(row);
  return li;
}

function pickBox(box) {
  document.querySelectorAll("#boxes button").forEach((x) => x.classList.toggle("on", x.dataset.box === box));
  state.box = box;
  showCachedList();
  quietRefresh();
}

// The last list of the box or search now picked, at once, or the skeleton until it comes.
function showCachedList() {
  const had = lists.get(listKey());
  $("listscroll").scrollTop = 0;
  if (had) return showList(had.threads, had.counts);
  state.threads = null;
  shownSignature = "";
  $("threads").replaceChildren();
  $("skeleton").hidden = false;
}

function search(words) {
  if (words === state.query) return;
  state.query = words;
  $("boxes").hidden = !!words;
  shown($("searching"), words ? "Search: “" + words + "”, in every box" : "");
  $("clearquery").hidden = !$("query").value;
  showCachedList();
  quietRefresh();
}

// ---------------------------------------------------------------- reading threads, and reading ahead

// Whether the list's row for a thread is newer than an answer about it, or a reply in it is still on its way.
// The Mac has the same rules (MailRules.behind and .stale in mac/Sources/AgentBoard/Mail.swift): change both together.
const behind = (show, row) => !!row && (row.last !== show.thread.last || row.count !== show.thread.count);
const pending = (show) => show.messages.some((m) => m.mine && (m.status === "sending" || m.status === "queued"));
const stale = (show, row) => behind(show, row) || pending(show);

// Keep `show`, the answer to request `n`, unless an answer to a later request is kept or it was asked before the
// page forgot everything (forget); then drop the least recently used past CACHE_CHARS, never this one.
function keep(show, n) {
  const id = show.thread.id;
  const had = kept.get(id);
  if (n <= forgotAt || (had && had.n > n)) return;
  if (had) keptChars -= had.show.document.length;
  kept.delete(id);
  kept.set(id, { show: show, n: n });
  keptChars += show.document.length;
  for (const [old, k] of kept) {
    if (keptChars <= CACHE_CHARS || old === id) break;
    kept.delete(old);
    keptChars -= k.show.document.length;
  }
}

// The kept answer for `id`, now the most recently used.
function take(id) {
  const k = kept.get(id);
  if (!k) return null;
  kept.delete(id);
  kept.set(id, k);
  return k.show;
}

// mail_show for `id`, kept when it comes. A request on its way for it already is waited for, unless `fresh`.
function fetchShow(id, fresh) {
  const f = fetching.get(id);
  if (f && !fresh) return f.promise;
  const n = ++asked;
  const promise = api("mail_show", { thread: id }).then((got) => {
    if (n <= researcherChangedAt || researcherAsked) {
      const current = rowOf(id) || (state.shown && state.shown.thread.id === id && state.shown.thread);
      got.thread.researcher = current ? current.researcher : null;
    } else if (n > forgotAt && fetching.get(id)?.n === n && !rowOf(id)) {
      researcherUnconfirmed.delete(id);
    }
    keep(got, n);
    return got;
  }).finally(() => {
    if (fetching.get(id) && fetching.get(id).n === n) fetching.delete(id);
  });
  fetching.set(id, { n: n, promise: promise });
  return promise;
}

const aheadQueue = [];
let aheadGoing = 0;

// Read the top READ_AHEAD threads, and the two below the open one, that are neither kept as the list has them nor on
// their way: in memory only, and not when the phone asks to save data.
function readAhead() {
  if (!state.threads || (navigator.connection && navigator.connection.saveData)) return;
  const list = state.threads;
  const i = state.openId ? list.findIndex((t) => t.id === state.openId) : -1;
  for (const t of list.slice(0, READ_AHEAD).concat(i >= 0 ? list.slice(i + 1, i + 3) : [])) {
    if (!aheadQueue.includes(t.id) && wanted(t)) aheadQueue.push(t.id);
  }
  pump();
}

function wanted(row) {
  const k = kept.get(row.id);
  return !fetching.has(row.id) && !(k && !stale(k.show, row));
}

function pump() {
  while (aheadGoing < AHEAD_AT_ONCE && aheadQueue.length) {
    const id = aheadQueue.shift();
    const row = rowOf(id);
    if (!row || !wanted(row)) continue;
    aheadGoing += 1;
    // A thread that fails to come is left unkept: opening it asks again, and load() says why (or, the session gone,
    // goes back to signing in). Nothing is lost by not saying it here, where nobody asked for the thread.
    fetchShow(id, false).catch(() => {}).finally(() => { aheadGoing -= 1; pump(); });
  }
}

// ---------------------------------------------------------------- a thread

// Open a thread from the list: a history entry of its own (holding no thread), so the back gesture comes back here.
function openThread(id) {
  if (state.openId) return;
  history.pushState({ view: "thread" }, "");
  showThread(id);
}

function showThread(id) {
  const row = rowOf(id);
  state.openId = state.lastId = id;
  shown($("threaderror"), "");
  const v = $("threadview");
  v.hidden = false;
  void v.offsetWidth;                                // its start, off to the right, so it slides in
  v.classList.add("in");
  document.body.classList.add("reading");
  $("listview").inert = true;
  readDraft(id);                                     // its draft, as the desktop has it now
  const k = kept.get(id);
  if (k && !(row && stale(k.show, row))) {
    const s = take(id);
    display(row ? Object.assign({}, s, { thread: Object.assign({}, s.thread, { researcher: null }, row) }) : s);
  } else {
    if (!(state.shown && state.shown.thread.id === id)) loading(row);
    $("progress").hidden = false;
    load(id, false);
  }
}

// While a thread is on its way: its subject, from the list, over a skeleton, and the bar running. The reply names the
// thread's own agents from the list, and stays shut until its mail is on screen (replyReady).
function loading(row) {
  $("docsubject").textContent = row ? row.subject || "(no subject)" : "";
  $("docload").hidden = false;
  $("extrasbutton").hidden = true;
  showModes();
  $("replyto").textContent = row && (row.agents || []).length ? "Reply to " + row.agents.join(", ") : "Reply";
  updateFab();
}

// Read a thread and show it if it is still the one open. An answer to a request that was on its way already may
// predate what the list shows: then it is shown and asked for once more, afresh.
async function load(id, fresh) {
  try {
    const got = await fetchShow(id, fresh);
    if (state.openId !== id) return;
    display(got);
    if (!fresh && behind(got, rowOf(id))) load(id, true);
  } catch (e) {
    if (e instanceof NotSignedIn) return restart();
    if (state.openId === id) {
      $("progress").hidden = true;
      shown($("threaderror"), e.message || String(e));
    }
  }
}

// The open thread read again when the list shows it changed, or while a reply in it is on its way.
function recheckOpen() {
  if (!state.openId || !state.shown || state.shown.thread.id !== state.openId) return;
  const row = rowOf(state.openId);
  if (row) {
    state.shown.thread.box = row.box;
    state.shown.thread.researcher = row.researcher;
  }
  if (stale(state.shown, row) ||
      (!row && !fetching.has(state.openId) &&
       (state.shown.thread.researcher || researcherUnconfirmed.has(state.openId)))) load(state.openId, true);
}

// Room under the document for the floating buttons, so they never cover its last lines (a style of the page's own,
// added to the document's head; the frame still runs no script and loads nothing).
const ROOM = "<style>html > body { padding-bottom: 104px; }</style>";
const framed = (html) => html.replace("<head>", "<head>" + ROOM);

// A frame for document `doc`, in place of the one before. A frame of its own for each document: a frame that is
// navigated again (srcdoc set anew) adds an entry to the history, which the back gesture would step through instead of
// leaving the thread. A new frame's first document replaces its blank one, adding none.
function newFrame(doc) {
  const old = $("doc");
  const f = old.cloneNode(false);                    // its id, title and sandbox: no script, no same origin
  f.removeAttribute("srcdoc");
  if (doc) f.srcdoc = doc;
  f.addEventListener("load", frameLoaded);
  old.replaceWith(f);
}

function frameLoaded(e) {
  if (!e.target.srcdoc) return;
  $("docload").hidden = true;
  $("progress").hidden = true;
}

// Show `s`, the open thread's answer: the document in a new frame (only when it changed, so a thread opened again shows
// at once), what is in it behind the extras button, and the draft. An unread thread is then marked read up to its
// newest message, and the threads below it are read ahead.
function display(s) {
  state.shown = s;
  const t = s.thread;
  const inbox = t.box === "inbox";
  $("archive").setAttribute("aria-label", inbox ? "Archive" : "Move to the inbox");
  $("archiveicon").setAttribute("href", inbox ? "#i-archive" : "#i-inbox");
  const doc = framed(s.document);
  if (doc !== frameDoc) {
    frameDoc = doc;
    newFrame(doc);                                    // its load clears the skeleton
  } else {
    $("docload").hidden = true;
    $("progress").hidden = true;
  }
  showUnderMail(s.thread.id, s.messages, s.document);
  showAttachments(s.messages);
  showExtrasButton();
  showModes();
  const mail = s.messages.filter((m) => !m.mine);
  $("replyto").textContent = mail.length ? "Reply to " + mail[mail.length - 1].from : "Reply";
  showDraft();
  if (t.unread) markRead(s);
  readAhead();                                       // the two below it, now that it is in
}

// What the frame (whose sandbox runs no script) cannot do, in the sheet over the thread, in message order, each
// message's under a heading with its sender and time: its options and plots, drawn by the Mac tab's renderer, and its
// code blocks (an agent puts a command for the owner to run in one), each with a Copy button. The code blocks are read
// from the thread's document with DOMParser, which runs and loads nothing, and only their text is used. The frame
// keeps each widget's plain-text version, so one that cannot be drawn is still read there; the notice says which and
// why, and names a mail the document does not hold (the mailroom gives each its <details id=KEY>), whose code blocks
// are not listed. The cards show the draft's answers (markPicks), and an option picked goes to thread `id`'s draft
// (pickAnswer); a long press, the phone's right-click, picks with a comment at once, with no menu.
function showUnderMail(id, messages, html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const sections = [], problems = [], missing = [];
  const widgets = messages.flatMap((m) => m.widgets || []);
  for (const m of messages) {
    const section = document.createElement("section");
    const head = document.createElement("h2");
    head.textContent = (m.from || "") + ", " + fullTime(m.ts);
    section.append(head);
    for (const w of m.widgets || []) {
      const place = document.createElement("div");
      place.setAttribute("data-widget", String(w && w.id));
      section.append(place);
    }
    AgentBoardWidgets.render(section, m.widgets || [], {
      pick: (answer) => pickAnswer(id, widgets, answer),
      menu: false,
      problem: (why) => problems.push(why),
    });
    section.querySelectorAll("div[data-widget]:not(.abw-drawn)").forEach((p) => p.remove());
    const holder = doc.getElementById(m.key);
    if (!holder) missing.push(m.key);
    for (const pre of holder ? holder.querySelectorAll("pre") : []) section.append(codeBlock(pre.textContent));
    if (section.childElementCount > 1) sections.push(section);
  }
  $("extras").replaceChildren(...sections);
  $("extras").hidden = !sections.length;
  markPicks();
  shown($("widgetnotice"), [
    problems.length ? "Some options or plots could not be drawn, so the mail shows them as plain text: "
                      + problems.join("; ") + "." : "",
    missing.length ? "The thread's document does not hold mail " + missing.join(", ")
                     + ", so any code blocks in it are not listed here." : "",
  ].filter(Boolean).join(" "));
}

// The button that opens the sheet, saying what kinds of thing are in it: "Options · Plot · Code · Files".
function showExtrasButton() {
  const x = $("extras");
  const n = (sel) => x.querySelectorAll(sel).length;
  const kind = (count, one, many) => (count ? (count === 1 ? one : many) : "");
  const label = [kind(n(".abw-choices"), "Options", "Options"), kind(n(".abw-plot"), "Plot", "Plots"),
                 kind(n(".code"), "Code", "Code"), kind($("attachments").childElementCount, "File", "Files")]
    .filter(Boolean).join(" · ") || (!$("widgetnotice").hidden ? "About this thread" : "");
  $("extraslabel").textContent = label;
  $("extrasbutton").hidden = !label;
}

// A code block's text, with a button that copies it exactly (its trailing new line left off) and says so for 1.5 s,
// or says why the clipboard refused it. The Mac tab has the same rule (MailCopy.script in
// mac/Sources/AgentBoard/MailCopy.swift): change both together.
function codeBlock(text) {
  const exact = text.replace(/\n$/, "");
  const box = document.createElement("div");
  box.className = "code";
  const pre = document.createElement("pre");
  pre.textContent = exact;
  const button = document.createElement("button");
  button.className = "plain copy";
  button.textContent = "Copy";
  let timer = null;
  button.addEventListener("click", async () => {
    clearTimeout(timer);
    try {
      await navigator.clipboard.writeText(exact);
      button.textContent = "Copied";
    } catch (e) {
      button.textContent = "Not copied";
      shown($("threaderror"), "Not copied: " + (e.message || String(e)));
      snack("Not copied: " + (e.message || String(e)));
    }
    timer = setTimeout(() => { button.textContent = "Copy"; }, 1500);
  });
  box.append(button, pre);
  return box;
}

// An option picked or unpicked in thread `id`'s options (`raw`, widgets.js's answer), taken as the Mac takes one
// (MailHTML's userContentController, Draft.pick in Compose.swift): checked against `widgets`, the thread's own as the
// desktop gave them, whose spec gives the question and labels (ReplyText.answer); it replaces that question's earlier
// answer in the draft, keeping the comment on it, and goes when nothing is picked. The sheet stays as it is, so the
// owner can go on picking in other questions; a long press (with a comment) adds a comment field under the answer and
// opens the
// reply on it. Nothing is sent until Send, which still asks for the fingerprint. An option counts only while thread
// `id`, whose mail drew it, is the one on screen: a card from a thread left behind puts nothing in another's reply.
function pickAnswer(id, widgets, raw) {
  if (!(replyReady() && state.openId === id)) return;
  if (draft().held) {
    snack("An option picked was not taken. " + FROZEN_WHY);
    return markPicks();
  }
  const got = ReplyText.answer(raw, widgets);
  if (!got) {
    snack("An option picked was not taken: widgets.js gave an answer not understood");
    return markPicks();
  }
  keepDraft();
  const d = draft(), a = got.answer;
  const had = d.picks.find((p) => p.widget === a.widget);
  const picks = d.picks.filter((p) => p.widget !== a.widget);
  if (a.picked.length) {
    a.comment = had && had.comment !== null ? had.comment : got.comment ? "" : null;
    picks.push(a);
    picks.sort(inThread);
    if (got.comment) {
      d.focus = a.widget;
      d.open = true;
    }
  }
  d.picks = picks;
  showDraft();
  markPicks();
  saveSoon(id);
}

// Answers in the order their widgets come in the thread (Draft.before).
function inThread(a, b) {
  const x = ReplyText.place(a.widget), y = ReplyText.place(b.widget);
  for (let i = 0; i < Math.min(x.length, y.length); i++) if (x[i] !== y[i]) return x[i] - y[i];
  return x.length - y.length;
}

// An answer's ×: it goes from the draft, and its cards show nothing picked.
function dropAnswer(widget) {
  const d = draft();
  d.picks = d.picks.filter((p) => p.widget !== widget);
  showDraft();
  markPicks();
  saveSoon(state.openId);
}

// The open thread's cards as its draft has them: each question's answer picked, nothing in the rest.
function markPicks() {
  const d = state.openId ? drafts.get(state.openId) : null;
  AgentBoardWidgets.mark($("extras"), Object.fromEntries((d ? d.picks : []).map((p) => [p.widget, p.picked])));
}

// The thread's attachments, an agent's or the owner's own, listed in the sheet: a navigation from the sandboxed frame
// carries no session, so the frame's attachment links are inert and these fetch the files instead.
function showAttachments(messages) {
  const items = [];
  for (const m of messages) {
    for (const a of m.attachments || []) {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.append(icon("i-clip"), el("span", "", a.name + " (" + Math.ceil(a.size / 1024) + " KB" + (m.mine ? ", yours" : "") + ")"));
      b.addEventListener("click", () => download(m.key, a.index, a.name));
      li.append(b);
      items.push(li);
    }
  }
  $("attachments").replaceChildren(...items);
  $("attachments").hidden = !items.length;
}

// One attachment, fetched by this page's script (with X-Agent-Board, as the server requires) and saved as a file.
function download(key, index, name) {
  return guarded(async () => {
    const res = await fetch("part/" + encodeURIComponent(key) + "/" + index,
                            { headers: { "X-Agent-Board": "1" }, cache: "no-store" });
    if (res.status === 401) throw new NotSignedIn("not signed in");
    if (!res.ok) throw new Error(name + " did not download: " + (await res.text()).trim());
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }, $("threaderror"));
}

// Back to the list: through the history when opening the thread added to it, so the back gesture agrees.
function leaveThread() {
  if (history.state && history.state.view === "thread") history.back();
  else hideThread();
}

function hideThread(instant) {
  closeSheet();
  keepDraft();
  $("text").value = "";                              // kept in its draft; the next thread's shows when it is in
  clearAnswers();
  if (state.openId) {
    const d = drafts.get(state.openId);
    if (d) d.open = false;
    saveDraft(state.openId);                         // left: saved on the desktop now
  }
  state.openId = null;
  const v = $("threadview");
  v.classList.remove("in");
  document.body.classList.remove("reading");
  $("listview").inert = false;
  if (instant) v.hidden = true;
  else setTimeout(() => { if (!state.openId) v.hidden = true; }, 320);
  renderRows();                                      // a draft started, a thread read
}

// ---------------------------------------------------------------- changing threads: shown at once, undone at a tap

// A thread as it will be after `op`. The Mac has the same rule (MailRules.changed in mac/Sources/AgentBoard/Mail.swift):
// change both together.
function changed(t, op, until) {
  const n = Object.assign({}, t);
  if (op === "read") n.unread = false;
  if (op === "unread") n.unread = true;
  if (op === "archive") { n.box = "archived"; n.snooze_until = null; }
  if (op === "inbox") { n.box = "inbox"; n.snooze_until = null; }
  if (op === "snooze") { n.box = "snoozed"; n.snooze_until = until; }
  return n;
}

// The list as `op` on thread `id` leaves it: changed, and gone when it no longer belongs to the box listed; every list
// kept is changed alike. The Mac has the same rule (MailRules.applying in mac/Sources/AgentBoard/Mail.swift): change
// both together.
function applying(op, id, until) {
  for (const [key, l] of lists) {
    const box = key.startsWith("?") ? "all" : key;
    l.threads = l.threads.flatMap((t) => {
      if (t.id !== id) return [t];
      const n = changed(t, op, until);
      return box === "all" || n.box === box ? [n] : [];
    });
  }
  const now = lists.get(listKey());
  if (now) showList(now.threads, state.counts);
}

const FAILURE = { read: "Not marked read", unread: "Not marked unread", archive: "Not archived",
                  inbox: "Not moved to the inbox", snooze: "Not snoozed" };

// Change a thread on the desktop (mail_mark): read, archive and snooze reach up to `upto`, the newest message the
// owner has been shown. The list is read again once the desktop answers. Raises what api raises.
async function mark(id, op, upto, until) {
  changedAt = ++asked;
  changing += 1;
  const body = { threads: [id], op: op };
  if (op === "read" || op === "archive" || op === "snooze") body.upto = upto;
  if (until) body.until = until;
  try {
    await api("mail_mark", body);
  } finally {
    changing -= 1;
  }
}

// `op` on thread `t` (as the list or the open thread has it): shown at once, sent, and said in the snackbar with an
// Undo that puts the thread back as it was; a failure is said there too, and the list read again.
async function change(t, op, until, quiet) {
  applying(op, t.id, until);
  if (!quiet) snack(done(op, until), "Undo", () => undo(t, op));
  try {
    await mark(t.id, op, t.newest, until);
  } catch (e) {
    if (e instanceof NotSignedIn) return restart();
    snack(FAILURE[op] + ": " + (e.message || String(e)));
    return quietRefresh();
  }
  if (op !== "read") quietRefresh();                 // the counts; a read mark changes nothing else the list shows
}

// What the snackbar says once `op` is done. The Mac's Undo bar says the same (MailOp.done in
// mac/Sources/AgentBoard/Mail.swift): change both together.
function done(op, until) {
  return { archive: "Archived", inbox: "Moved to the inbox", unread: "Marked unread", read: "Marked read",
           snooze: "Snoozed until " + aheadTime(until || 0) }[op];
}

// Put thread `t` back as it was before `op`: in its box (a snooze still ahead snoozed again), read or unread.
function undo(t, op) {
  if (op === "unread") return change(t, "read", null, true);
  if (t.box === "snoozed" && t.snooze_until && t.snooze_until * 1000 > Date.now()) return change(t, "snooze", t.snooze_until, true);
  return change(t, t.box === "archived" ? "archive" : "inbox", null, true);
}

// Mark the open thread read up to its newest message, on screen at once.
function markRead(s) {
  const t = s.thread;
  t.unread = false;
  const row = rowOf(t.id);
  if (row && row.unread && state.counts && row.box === "inbox") state.counts.unread = Math.max(0, state.counts.unread - 1);
  if (row) {
    change(Object.assign({}, row, { newest: t.newest }), "read", null, true);   // says a failure, reads the list again
  } else {
    mark(t.id, "read", t.newest).catch((e) => {
      if (e instanceof NotSignedIn) return restart();
      t.unread = true;                               // still unread on the desktop, so here too
      snack(FAILURE.read + ": " + (e.message || String(e)));
    });
  }
}

// Archive (or move back to the inbox), snooze or mark unread the open thread, from its bar; back to the list.
function act(op, until) {
  const s = state.shown;
  if (!s || s.thread.id !== state.openId) return;
  const t = Object.assign({}, rowOf(s.thread.id) || s.thread, { newest: s.thread.newest });
  leaveThread();
  change(t, op, until);
}

// What a swipe does to thread `t`: "archive" for a thread in the inbox, else "inbox" (back to it).
function swipeAction(t) {
  return t.box === "inbox" ? "archive" : "inbox";
}

// ---------------------------------------------------------------- fast mode

// Fast mode (mail_fast; the mailroom's `mailbox fast`) is for quick back-and-forths with a thread's agent: while it is
// on, the agent answers there in a few plain lines at low effort and its replies skip the review before sending. It
// ends when the owner switches it off, or once `until` passes with no message of the owner's in the thread (each one
// moves it on).

// "Fast · 7 min": its minutes left, rounded up and at most its length; "Fast · ending" once `until` has passed, until
// the desktop says it has ended (within its poll, 30 s). The Mac has the same rule (MailFast.label in
// mac/Sources/AgentBoard/Mail.swift): change both together.
function fastLabel(fast, now) {
  const left = Math.min(Math.ceil((fast.until * 1000 - (now || new Date())) / 60000),
                        fast.minutes > 0 ? Math.ceil(fast.minutes) : Infinity);
  return left > 0 ? "Fast · " + left + " min" : "Fast · ending";
}

// A row's badge for a thread in fast mode.
function fastMark(fast) {
  const s = el("span", "fastmark");
  s.append(icon("i-bolt"), el("span", "", fastLabel(fast)));
  return s;
}

// The open thread, as the list has it (polled, so the newer) or else its answer.
function modeThread() {
  const id = state.openId;
  if (!id) return null;
  return rowOf(id) || (state.shown && state.shown.thread.id === id ? state.shown.thread : null);
}

// The note under the bar of fast mode f: what it does to the agent's effort (unchanged, and why, when it cannot lower
// it), first "Lowering effort…" while the effort is still to be lowered (the mailroom does it at its next poll, within
// 30 s, and the list then says so). The Mac has the same rule (MailModeBar.texts in Mail.swift).
function fastNote(f) {
  const said = f.note || "";
  return f.effort === "lowering" ? ("Lowering effort… " + said).trim() : said;
}

// The open thread's Fast switch, on ("Fast · 7 min") or off, and under the bar its note (fastNote).
function showFast() {
  const t = modeThread(), b = $("fast");
  b.hidden = !t;
  const f = t && t.fast;
  b.classList.toggle("on", !!f);
  b.setAttribute("aria-pressed", f ? "true" : "false");
  b.disabled = !!t && fastAsked === t.id;
  $("fastlabel").textContent = !t ? "Fast" : fastAsked === t.id ? "Fast…" : f ? fastLabel(f) : "Fast";
  shown($("fastnote"), f ? fastNote(f) : "");
}

// Switch the open thread's fast mode on or off (mail_fast). The desktop answers with the thread as the mailroom has it
// now, which the switch and every list kept show at once; a refusal (a group's thread, one no single agent owns) is
// said in the snackbar. A list asked before the answer may predate it, so it is not shown (as after a mark).
async function toggleFast() {
  const t = modeThread();
  if (!t || fastAsked) return;
  const id = t.id, on = !t.fast;
  fastAsked = id;
  showFast();
  changedAt = ++asked;
  changing += 1;
  try {
    const got = await api("mail_fast", { thread: id, on: on });
    if (!got.thread) throw new Error("the desktop's answer has no thread");
    setThreadFields(id, { fast: got.thread.fast || null });
  } catch (e) {
    if (e instanceof NotSignedIn) return restart();
    snack("Fast mode not switched " + (on ? "on" : "off") + ": " + (e.message || String(e)));
  } finally {
    changing -= 1;
    fastAsked = null;
    showFast();
  }
  quietRefresh();
}

// Keep the returned mode wherever the same thread is already held.
function setThreadFields(id, fields) {
  for (const l of lists.values()) l.threads = l.threads.map((t) => (t.id === id ? Object.assign({}, t, fields) : t));
  if (state.shown?.thread.id === id) state.shown.thread = Object.assign({}, state.shown.thread, fields);
  if (kept.has(id)) {
    const k = kept.get(id);
    k.show = Object.assign({}, k.show, { thread: Object.assign({}, k.show.thread, fields) });
  }
  const now = lists.get(listKey());
  if (now) showList(now.threads, state.counts);
  else showModes();
}

function researcherState(t) {
  const r = t && t.researcher;
  return r && typeof r.on === "boolean" && typeof r.agent === "string" && r.agent.trim() ? r : null;
}

const RESEARCHER_DELIVERY = new Map([
  ["pending", "Delivery pending."],
  ["queued", "Delivery queued."],
  ["accepted", "Awaiting agent confirmation."],
  ["unavailable", "Delivery failed. Agent unavailable."],
  ["error", "Delivery failed."],
]);

function researcherStatus(r, hasTag) {
  const requested = "Requested " + (r.on ? "on" : "off") + " for " + r.agent + ".";
  if (!hasTag) return requested + " Update the desktop to change this setting.";
  const detail = r.delivery === "unavailable" && typeof r.delivery_detail === "string" ? r.delivery_detail.trim() : "";
  if (detail) return requested + " Delivery failed. " + detail;
  const delivery = RESEARCHER_DELIVERY.get(r.delivery);
  if (delivery) return requested + " " + delivery;
  return r.on ? r.agent + " will ask you to approve its experiment plan in Mail." : "Off for " + r.agent + ".";
}

function showModes() {
  showFast();
  const t = modeThread(), r = researcherState(t), b = $("researcher");
  const checking = r && researcherUnconfirmed.has(t.id);
  const hasTag = typeof r?.tag === "string" && !!r.tag;
  b.hidden = !r;
  b.disabled = !hasTag || !!researcherAsked || !!checking;
  b.onclick = hasTag ? () => toggleResearcher(t.id, r) : null;
  b.classList.toggle("on", !!r?.on);
  b.setAttribute("aria-pressed", r?.on ? "true" : "false");
  const help = r?.on ? "Turn off Researcher." : "Turn on Researcher to approve this agent's experiment plan in Mail.";
  b.title = $("researcherhelp").textContent = help;
  const status = !r ? "" : researcherAsked === t.id ? researcherProgress :
    checking ? "Checking Researcher for " + r.agent + "…" : researcherStatus(r, hasTag);
  shown($("researchernote"), status);
}

async function toggleResearcher(id, before) {
  if (sending || researcherAsked || researcherUnconfirmed.has(id)) return;
  const on = !before.on, n = ++asked;
  researcherAsked = id;
  changing += 1;
  let confirmed = false, attempted = true;
  try {
    const got = await approved("mail_researcher", { thread: id, on: on, expected_tag: before.tag }, (said) => {
      researcherProgress = said;
      showModes();
    }, { sending: "Saving Researcher…" });
    if (n <= forgotAt) return;
    const r = researcherState(got.thread);
    if (got.thread?.id !== id || r?.on !== on || r?.tag !== before.tag) {
      throw new Error("Refreshing the thread before switching Researcher again.");
    }
    confirmed = true;
    setThreadFields(id, { researcher: r });
    if (got.error) snack(got.error);
  } catch (e) {
    if (n <= forgotAt) return;
    if (e instanceof NotSignedIn) return restart();
    attempted = !(e instanceof NotAsked);
    snack("Could not confirm the Researcher setting for " + before.agent + ". " + (e.message || String(e)));
  } finally {
    changing -= 1;
    researcherAsked = null;
    researcherProgress = "";
    if (n > forgotAt) {
      if (attempted) {
        for (const l of lists.values()) {
          for (const row of l.threads) {
            if (row.id !== id && researcherState(row)) researcherUnconfirmed.add(row.id);
          }
        }
        if (!confirmed) researcherUnconfirmed.add(id);
      }
      researcherChangedAt = changedAt = ++asked;
      if (state.openId && !rowOf(state.openId)) load(state.openId, true);
      quietRefresh();
    }
    showModes();
  }
}

// ---------------------------------------------------------------- the snackbar

let snackTimer = null;
function snack(text, action, onAction) {
  const b = $("snackaction");
  $("snacktext").textContent = text;
  b.hidden = !action;
  b.textContent = action || "";
  b.onclick = action ? () => { hideSnack(); onAction(); } : null;
  const s = $("snackbar");
  s.hidden = false;
  void s.offsetWidth;
  s.classList.add("on");
  clearTimeout(snackTimer);
  snackTimer = setTimeout(hideSnack, action ? 6000 : 4000);
}

function hideSnack() {
  clearTimeout(snackTimer);
  $("snackbar").classList.remove("on");
  snackTimer = setTimeout(() => { $("snackbar").hidden = true; }, 250);
}

// ---------------------------------------------------------------- sheets: what is in the thread, snoozing, the reply

let sheet = null;                // the sheet up
let watcher = null;              // its CloseWatcher: Android's back gesture closes it

function openSheet(s) {
  if (sheet === s) return;
  if (sheet) closeSheet();
  sheet = s;
  $("scrim").hidden = false;
  s.hidden = false;
  void s.offsetWidth;
  $("scrim").classList.add("on");
  s.classList.add("up");
  if (window.CloseWatcher) {
    watcher = new CloseWatcher();
    watcher.onclose = () => { watcher = null; closeSheet(); };
  }
  updateFab();
}

function closeSheet() {
  const s = sheet;
  if (!s) return;
  sheet = null;
  if (watcher) {
    const w = watcher;
    watcher = null;
    w.destroy();
  }
  if (s === $("reply")) {
    keepDraft();
    $("versions").hidden = true;
    if (state.openId && drafts.has(state.openId)) {
      drafts.get(state.openId).open = false;
      saveDraft(state.openId);                       // left: saved on the desktop now
    }
  }
  if (s === $("keyringsheet")) {
    letUnlockGo();
    clearPassword();
  }
  s.classList.remove("up");
  $("scrim").classList.remove("on");
  setTimeout(() => {
    if (sheet !== s) s.hidden = true;
    if (!sheet) $("scrim").hidden = true;
  }, 280);
  updateFab();
}

// The snooze sheet: its choices as of now (snooze.js), each with the time it wakes at, and a date and time to pick.
function openSnooze() {
  const now = Date.now();
  $("snoozechoices").replaceChildren(...snoozePresets(now).map((c) => {
    const b = el("button");
    b.append(el("span", "", c.label), el("span", "at", aheadTime(c.at)));
    b.addEventListener("click", () => { closeSheet(); act("snooze", c.at); });
    return b;
  }));
  const d = new Date(now);
  $("snoozeat").min = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + clock(d);
  $("snoozeat").value = "";
  $("snoozepicked").disabled = true;
  openSheet($("snoozesheet"));
}

function pickedSnooze() {
  const v = $("snoozeat").value;
  const at = v ? new Date(v).getTime() : NaN;      // a datetime-local value is this phone's local time
  return at > Date.now() ? Math.floor(at / 1000) : null;
}

// ---------------------------------------------------------------- drafts, kept on the desktop

// A thread's draft is kept on the desktop (agentboard/maildrafts.py; README, "Drafts"), the same one the Mac shows. It
// is read from there when its thread opens (readDraft), and again when the desktop's list of drafts shows it changed
// (refreshDrafts); it is saved there (saveDraft) SAVE_MS after typing stops, and at once when its thread or its reply
// is left or the page is hidden or closed (saveAll), so it outlives the page. A save goes from the desktop's rev the
// page last read or saved; when the draft has changed on the Mac since, the desktop keeps the page's as a version and
// the draft shows as it is there, with a note saying so (differ). What is typed and not yet saved is in the page's
// memory only.

// What the reply sheet says of draft `d`: what the desktop said of it (a change on the Mac), and why it could not be
// read or saved there the last time it was tried (`problem`, until one succeeds).
function showNote(d) {
  shown($("draftnote"), [d.note, d.problem].filter(Boolean).join(" "));
}

// A thread's draft before anything is known of it: nothing in it, and not read from the desktop yet. rev and desk: the
// desktop's rev and its draft there as the page last read or saved it (null: none); read: read since the page opened
// (a save waits for it); busy: a save, discard or restore on its way; discarding: a discard on its way, or waiting for
// a save to end (no save meanwhile); waiting: what waits for the draft to be saved (saved); note: what the desktop said
// of it; versions: the desktop's versions of it, newest first.
function newDraft() {
  return { text: "", files: [], picks: [], comments: [], held: null, open: false, focus: null, rev: null, desk: null,
           read: false, reading: null, busy: false, discarding: false, timer: null, waiting: [], note: "", problem: "",
           versions: [] };
}

function draftOf(id) {
  if (!drafts.has(id)) drafts.set(id, newDraft());
  return drafts.get(id);
}

// A draft as the desktop keeps it (maildrafts.check_content): what is typed, untrimmed, and the held reply's key.
function snapshot(d) {
  return { text: d.text, picks: d.picks, comments: d.comments, files: d.files, key: d.held ? d.held.key : null };
}

const EMPTY = { text: "", picks: [], comments: [], files: [], key: null };
const canon = (s) => JSON.stringify([s.text, s.picks.map((p) => [p.widget, p.picked, p.question, p.labels,
                                                                 p.comment === undefined ? null : p.comment]),
                                     s.comments.map((c) => [c.id, c.message, c.at, c.quote, c.comment]),
                                     s.files.map((f) => [f.id, f.name, f.size]), s.key || null]);

// Whether drafts `a` and `b` are the same to the desktop, which keeps a blank one with no key as none (the Mac's
// DraftSnapshot.same).
function same(a, b) {
  a = a || EMPTY;
  b = b || EMPTY;
  return (blank(a) && !a.key && blank(b) && !b.key) || canon(a) === canon(b);
}

// Whether draft `d` holds anything the desktop has not taken.
const unsaved = (d) => !same(snapshot(d), d.desk);

const place = (device) => (device === "phone" ? "your phone" : "your Mac");

// The reply request of thread `id`'s draft `d` under `key`: its text as the Mac puts it together, and its files named
// from the desktop's drafts by their SHA-256 (which the passkey approves with the rest).
function request(id, key, d) {
  return { thread: id, key: key, text: ReplyText.reply({ text: d.text, picks: d.picks, comments: d.comments }),
           files: d.files.map((f) => ({ name: f.name, draft: f.id })) };
}

// The desktop's draft `s` (null: none) shown as thread `id`'s: held still when it holds a key, a reply that may be on
// its way, from the Mac or from here.
function adopt(id, d, s) {
  s = s || EMPTY;
  d.text = s.text;
  d.picks = s.picks.map((p) => Object.assign({}, p, { comment: p.comment === undefined ? null : p.comment }));
  d.comments = s.comments.map((c) => Object.assign({}, c));
  d.files = s.files.map((f) => Object.assign({}, f));
  d.held = s.key ? request(id, s.key, d) : null;
  d.focus = null;
  if (state.openId === id) {
    showDraft();
    markPicks();
  }
}

// Read thread `id`'s draft from the desktop (draft_get), then save there what it has not taken from here.
function readDraft(id) {
  const d = draftOf(id);
  if (d.reading) return d.reading;
  d.reading = (async () => {
    let got;
    try {
      got = await api("draft_get", { thread: id });
    } catch (e) {
      d.reading = null;
      if (e instanceof NotSignedIn) return restart();
      d.problem = "Not read from the desktop: " + (e.message || String(e));
      if (state.openId === id) showNote(d);
      return settle(d, { failed: e.message || String(e) });
    }
    d.reading = null;
    if (drafts.get(id) !== d) return;
    d.read = true;
    d.problem = "";
    took(id, d, got);
    saveDraft(id);
  })();
  return d.reading;
}

// The desktop's draft of thread `id` as read (`v`): shown when nothing here is unsaved; when the change here was made
// on the draft the desktop still has, it is saved on the newer rev; when both changed, the save that follows is a
// conflict, which the desktop keeps as a version.
function took(id, d, v) {
  d.versions = v.versions || [];
  if (!d.busy && !unsaved(d)) {
    d.rev = v.rev;
    d.desk = v.draft;
    if (!same(snapshot(d), v.draft)) adopt(id, d, v.draft);
  } else if (same(v.draft, d.desk)) {
    d.rev = v.rev;
  }
  if (state.openId === id) showVersions();
  renderRows();
  updateFab();
}

// Save thread `id`'s draft on the desktop now (draft_put), if it holds anything the desktop has not taken: from the
// rev last read, once it has been read since the page opened. Not while a save, discard or restore of it is on its
// way, each of which saves again as it ends. `outlive`: the page is going, and the save goes on without it.
async function saveDraft(id, outlive) {
  const d = drafts.get(id);
  if (!d) return;
  clearTimeout(d.timer);
  d.timer = null;
  if (d.busy || d.reading || d.discarding) return;
  if (!unsaved(d)) return settle(d, "saved");
  if (!d.read) return readDraft(id);
  d.busy = true;
  const sent = snapshot(d), base = d.rev || 0;
  let how = "saved";
  try {
    const got = await api("draft_put", { thread: id, base_rev: base, draft: sent, device: "phone" }, undefined,
                          outlive);
    if (drafts.get(id) !== d) return;
    if (got.conflict) {
      d.versions = got.versions;
      if (!same(snapshot(d), sent) && !blank(snapshot(d))) {
        // typed while this save was on its way: saved from the same old rev too, so the desktop keeps it as a version
        // as well, before the draft shows as the desktop has it
        d.busy = false;
        return saveDraft(id, outlive);
      }
      how = { conflict: differ(id, d, got) };
    } else {
      Object.assign(d, { rev: got.rev, desk: got.draft, versions: got.versions, note: "", problem: "" });
    }
  } catch (e) {
    if (e instanceof NotSignedIn) {
      d.busy = false;
      return restart();
    }
    how = { failed: e.message || String(e) };
    d.problem = "Not saved on the desktop yet: " + how.failed;
  }
  ended(id, d, how);
}

// A save, discard or restore of thread `id`'s draft has ended (`how`): saved again if it changed meanwhile, else what
// waits for it is told how it went.
function ended(id, d, how) {
  d.busy = false;
  if (state.openId === id) {
    showNote(d);
    showVersions();
    updateFab();
  }
  renderRows();
  if (how === "saved" && unsaved(d) && !d.discarding) return saveDraft(id);
  settle(d, how);
}

function settle(d, how) {
  const waiting = d.waiting;
  d.waiting = [];
  waiting.forEach((w) => w(how));
}

// How saving thread `id`'s draft went, once it has: "saved" (also when there was nothing to save), {conflict: the
// note} or {failed: why}.
function saved(id) {
  const d = drafts.get(id);
  if (!d) return Promise.resolve("saved");
  return new Promise((resolve) => {
    d.waiting.push(resolve);
    saveDraft(id);
  });
}

// Once no save of draft `d` is on its way: "saved" at once, or how the one on its way went.
function idle(d) {
  if (!d.busy) return Promise.resolve("saved");
  return new Promise((resolve) => d.waiting.push(resolve));
}

// Thread `id`'s draft saved SAVE_MS from now, unless changed again meanwhile.
function saveSoon(id) {
  const d = drafts.get(id);
  if (!d) return;
  clearTimeout(d.timer);
  d.timer = setTimeout(() => saveDraft(id), SAVE_MS);
}

// Every draft saved now: the page is hidden, or going (`outlive`).
function saveAll(outlive) {
  keepDraft();
  for (const id of Array.from(drafts.keys())) saveDraft(id, outlive);
}

// The desktop did not take thread `id`'s draft, as it had changed on the Mac since (`v`, which kept the phone's as a
// version): the draft shows as it is there, and the note says so. The note.
function differ(id, d, v) {
  Object.assign(d, { rev: v.rev, desk: v.draft, versions: v.versions });
  adopt(id, d, v.draft);
  d.note = "This draft changed on " + place(v.updated && v.updated.device) + " since"
    + (v.kept ? "; your version is under Versions." : ".");
  if (state.openId === id) {
    showNote(d);
    if (sheet !== $("reply")) snack(d.note);
  }
  return d.note;
}

// Bring back version `version` of thread `id`'s draft (draft_restore): from the Versions list, once what is here is
// saved, so the draft it replaces is kept as a version; or the snackbar's Undo of a discard (`before`, the draft as it
// was), shown back at once. When the draft changed on the Mac meanwhile, it shows as it is there and the note says so.
async function restoreVersion(id, version, before) {
  const d = drafts.get(id);
  if (!d) return;
  if (before && !d.busy && !unsaved(d)) {
    adopt(id, d, Object.assign({}, before, { key: null }));
    renderRows();
  } else {
    const how = await saved(id);
    if (drafts.get(id) !== d) return;
    if (how !== "saved") return snack("Not brought back: " + (how.conflict || how.failed));
  }
  d.busy = true;
  try {
    const got = await api("draft_restore", { thread: id, version: version, base_rev: d.rev || 0, device: "phone" });
    if (drafts.get(id) !== d) return;
    Object.assign(d, { rev: got.rev, desk: got.draft, versions: got.versions });
    adopt(id, d, got.draft);
    d.note = got.conflict ? "This draft changed on " + place(got.updated && got.updated.device)
      + " meanwhile, so nothing was brought back." : "";
    if (got.conflict) snack(d.note);
  } catch (e) {
    if (e instanceof NotSignedIn) return restart();
    snack("Not brought back: " + (e.message || String(e)));   // an Undo's draft stays here, saved with the next save
  } finally {
    if (drafts.get(id) === d) ended(id, d, "saved");
  }
}

// The reply sheet's Versions, under its button: the desktop's versions of the open thread's draft, newest first, each
// by why it was kept, when and where it was written and its first words (versionLabel); a tap brings one back.
function showVersions() {
  const d = state.openId ? drafts.get(state.openId) : null;
  const vs = d ? d.versions : [];
  $("versionsbutton").hidden = !vs.length;
  if (!vs.length) $("versions").hidden = true;
  $("versions").replaceChildren(...vs.map((v) => {
    const li = el("li");
    const b = el("button", "", versionLabel(v));
    b.addEventListener("click", () => {
      $("versions").hidden = true;
      restoreVersion(state.openId, v.id);
    });
    li.append(b);
    return li;
  }));
}

// A version's line: "Discarded 10:42, Mac: first words…". The Mac's menu says the same (DraftVersion.label in
// mac/Sources/AgentBoard/Compose.swift): change both together.
function versionLabel(v, now) {
  const why = { discarded: "Discarded", saved: "Earlier", cleared: "Emptied", conflict: "Not taken",
                replaced: "Replaced" }[v.why] || v.why;
  const held = [[v.picks, "answer"], [v.comments, "comment"], [v.files, "file"]].filter((x) => x[0] > 0)
    .map((x) => x[0] + " " + x[1] + (x[0] === 1 ? "" : "s")).join(", ");
  const words = !v.words ? "(" + (held || "empty") + ")" : v.words.length > 60 ? v.words.slice(0, 60) + "…" : v.words;
  return why + " " + aheadTime(v.at, now) + ", " + (v.device === "phone" ? "phone" : "Mac") + ": " + words;
}

// The desktop's list of drafts (draft_list), read with each list, for the rows' Draft marks: an open thread whose
// draft it shows changed is read again, and every draft here with anything unsaved is saved (after a save that
// failed). A list that fails leaves the marks as they were; one asked before the page forgot everything is dropped.
async function refreshDrafts() {
  const n = ++asked;
  let got;
  try {
    got = await api("draft_list", {});
  } catch (e) {
    return;                                          // the list's own call says what is wrong
  }
  if (n <= forgotAt) return;
  deskDrafts.clear();
  for (const [id, v] of Object.entries(got.drafts || {})) deskDrafts.set(id, v.rev);
  const open = state.openId ? drafts.get(state.openId) : null;
  if (open && !open.reading) {
    const ours = same(open.desk, null) ? null : open.rev;
    if ((deskDrafts.has(state.openId) ? deskDrafts.get(state.openId) : null) !== ours) readDraft(state.openId);
  }
  for (const [id, d] of drafts) if (d.read && unsaved(d) && !d.timer) saveDraft(id);
  renderRows();
  updateFab();
}

// ---------------------------------------------------------------- replying

// Whether a reply may be written and sent: the open thread's own mail is on screen. Until then the sheet, the Reply
// button and Send would answer a thread whose mail has not been seen, or, while another is still in the frame, seem
// to.
function replyReady() {
  return !!state.openId && !!state.shown && state.shown.thread.id === state.openId;
}

// The open thread's draft.
function draft() {
  return draftOf(state.openId);
}

// The open thread's reply sheet as its draft has it: its answers and the Mac's comments above the owner's text, up
// once Reply
// is pressed or an option long-pressed (which gives that answer's comment field the keyboard), what the desktop said
// of the draft (its note) and its versions; the floating button says Draft while a reply is started.
function showDraft() {
  const d = draft();
  if ($("text").value !== d.text) $("text").value = d.text;
  showAnswers(d);
  $("files").replaceChildren(...d.files.map((f, i) => {
    const li = document.createElement("li");
    li.append(el("span", "", f.name + " (" + Math.ceil(f.size / 1024) + " KB)"));
    const x = el("button", "icon-button");
    x.setAttribute("aria-label", "Take out " + f.name);
    x.append(icon("i-close"));
    x.addEventListener("click", () => { d.files.splice(i, 1); showDraft(); saveSoon(state.openId); });
    li.append(x);
    return li;
  }));
  showNote(d);
  showVersions();
  if (d.open && replyReady()) openSheet($("reply"));
  else if (sheet === $("reply")) closeSheet();
  if (d.focus && sheet === $("reply")) {
    const field = Array.from($("answers").children).find((e) => e.dataset.answer === d.focus).querySelector("textarea");
    d.focus = null;
    field.focus();
  }
  updateFab();
}

let answersShown = "";           // what the reply's answers on screen show, the thread's and the comments' fields too
let answersWords = "";           // the agent's words in them
let answersSeen = false;         // they have been seen to their end since those words last changed
let answersFromTop = false;      // those words changed out of sight: the answers show from their start when next seen

// The draft's answers above the reply's text, each whole as it is sent (ReplyText): the question and the options
// picked, the agent's words, quoted and laid out left to right whatever letters they hold, then the owner's answer,
// with an × that takes it out, and once one was asked for (a long press) the comment's field; then the owner's
// comments on the agent's text, made on the Mac, each its quote, whole, with its comment's field and an ×. They are
// drawn again only when they
// change, so a field being written in keeps the keyboard.
function showAnswers(d) {
  const shown = JSON.stringify([state.openId, d.picks.map((p) => [p.widget, p.picked, p.question, p.labels,
                                                                   p.comment !== null]),
                                d.comments.map((c) => [c.id, c.quote])]);
  if (shown === answersShown) return;
  answersShown = shown;
  const words = JSON.stringify([d.picks.map(ReplyText.asked), d.comments.map((c) => c.quote)]);
  if (words !== answersWords) {
    answersWords = words;
    answersSeen = false;
    answersFromTop = sheet !== $("reply");
  }
  $("answers").replaceChildren(...d.picks.map(answerEntry), ...d.comments.map(commentEntry));
  $("answers").hidden = !d.picks.length && !d.comments.length;
}

function answerEntry(p) {
  const box = el("div", "answer");
  box.dataset.answer = p.widget;
  const asked = el("div", "asked", ReplyText.asked(p).join("\n"));
  asked.dir = "ltr";
  const words = el("div", "words");
  words.append(asked, el("div", "mine", ReplyText.answered(p)));
  const x = el("button", "icon-button");
  x.setAttribute("aria-label", "Take this answer out");
  x.append(icon("i-close"));
  x.addEventListener("click", () => dropAnswer(p.widget));
  const line = el("div", "line");
  line.append(words, x);
  box.append(line);
  if (p.comment !== null) {
    const field = el("textarea", "note");
    field.rows = 2;
    field.placeholder = "Your comment on this choice…";
    field.setAttribute("autocomplete", "off");
    field.value = p.comment;
    field.addEventListener("input", () => {
      draft().picks.find((q) => q.widget === p.widget).comment = field.value;
      updateFab();
      saveSoon(state.openId);
    });
    box.append(field);
  }
  return box;
}

// A comment of the owner's on the agent's text, made on the Mac (MailComments.swift): the quote, whole and left to
// right, as the reply quotes it, with an × that takes it out, and the comment's field.
function commentEntry(c) {
  const box = el("div", "answer quoted");
  box.dataset.comment = c.id;
  const asked = el("div", "asked", ReplyText.trim(c.quote));
  asked.dir = "ltr";
  const words = el("div", "words");
  words.append(asked);
  const x = el("button", "icon-button");
  x.setAttribute("aria-label", "Take this comment out");
  x.append(icon("i-close"));
  x.addEventListener("click", () => {
    const d = draft();
    d.comments = d.comments.filter((q) => q.id !== c.id);
    showDraft();
    saveSoon(state.openId);
  });
  const line = el("div", "line");
  line.append(words, x);
  const field = el("textarea", "note");
  field.rows = 2;
  field.placeholder = "Your comment on this…";
  field.setAttribute("autocomplete", "off");
  field.value = c.comment;
  field.addEventListener("input", () => {
    const q = draft().comments.find((q) => q.id === c.id);
    if (q) q.comment = field.value;
    updateFab();
    saveSoon(state.openId);
  });
  box.append(line, field);
  return box;
}

// No answers on screen (a thread left, everything forgotten): the next thread's are drawn when it is in.
function clearAnswers() {
  answersShown = answersWords = "";
  answersSeen = answersFromTop = false;
  $("answers").replaceChildren();
  $("answers").hidden = true;
}

// Whether the answers are taller than their room and have not been scrolled to their end since the agent's words in
// them last changed, as on the Mac (ComposeBox.unseen): Send waits, so nothing in them is sent unread. They count as
// seen once their end is on screen, in a reply sheet that is up. Changed while the sheet was down, they show from
// their start: Chrome keeps a hidden list's offset, which could be its end.
function answersUnseen() {
  const a = $("answers");
  if (a.hidden) return false;
  if (sheet === $("reply")) {
    if (answersFromTop) {
      answersFromTop = false;
      a.scrollTop = 0;
    }
    if (a.scrollTop + a.clientHeight >= a.scrollHeight - 1) answersSeen = true;
  }
  return !answersSeen;
}

// The open thread's draft held still while its reply may have reached the desktop (send), as the Mac's frozen draft
// (Draft.frozen): its text, files and answers take no change, and a line says why, but while it is being sent.
function showHeld() {
  const d = state.openId ? drafts.get(state.openId) : null;
  const held = !!(d && d.held);
  $("text").readOnly = held;
  $("attach").disabled = held;
  for (const b of document.querySelectorAll("#answers button, #files button")) b.disabled = held;
  for (const t of $("answers").querySelectorAll("textarea")) t.readOnly = held;
  $("versionsbutton").disabled = held || sending;
  shown($("held"), held && !sending ? FROZEN_WHY : "");
}

function updateFab() {
  const ready = replyReady();
  const unseen = answersUnseen();
  $("replylabel").textContent = state.openId && hasDraft(state.openId) ? "Draft" : "Reply";
  $("replybutton").hidden = sheet === $("reply") || !ready;
  $("send").disabled = sending || !ready || unseen;
  $("discard").disabled = sending;
  $("answersmore").hidden = !unseen;
  showHeld();
  $("orchbutton").hidden = sheet === $("orchsheet");
  $("orchsend").disabled = sending || !$("orchtext").value.trim();
  $("keyringunlock").disabled = sending || !$("keyringpassword").value;
}

function keepDraft() {
  if (state.openId && drafts.has(state.openId)) drafts.get(state.openId).text = $("text").value;
  else if (state.openId && $("text").value && sheet === $("reply")) draft().text = $("text").value;
}

// A new idempotency key, a reply's or a message to the orchestrator's: 16 random bytes in base64url, not starting
// with "-" (mailbox.KEY_RE).
function newKey() {
  for (;;) {
    const made = toB64url(crypto.getRandomValues(new Uint8Array(16)));
    if (made[0] !== "-") return made;
  }
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).replace(/^data:[^,]*,/, ""));
    r.onerror = () => reject(new Error(file.name + " could not be read"));
    r.readAsDataURL(file);
  });
}

// Files picked for the open thread's reply: each kept on the desktop with its draft (draft_file_add, under the SHA-256
// of its bytes, which the answer must name), then named in the draft, which is saved, so the Mac shows them too and a
// reply from either sends them from there. At most the files and bytes a reply carries (status's limits); a file in
// the draft already is not taken twice.
async function attach(list) {
  const id = state.openId;
  const d = draft();
  const files = Array.from(list);
  let lim = state.limits;
  if (!lim) {
    try { lim = (await statusAsked).limits; } catch (e) { return shown($("sendstatus"), "Not attached: " + e.message); }
  }
  const bytes = d.files.reduce((n, f) => n + f.size, 0) + files.reduce((n, f) => n + f.size, 0);
  if (d.files.length + files.length > lim.files) {
    return ($("sendstatus").textContent = "Not attached: a reply carries at most " + lim.files + " files.");
  }
  if (bytes > lim.bytes) {
    return ($("sendstatus").textContent = "Not attached: a reply carries at most " + Math.round(lim.bytes / 1048576)
            + " MB of files; send bigger ones from the Mac.");
  }
  $("sendstatus").textContent = "Attaching…";
  try {
    for (const f of files) {
      const digest = await sha256bytes(await f.arrayBuffer());
      if (d.files.some((x) => x.id === digest)) throw new Error(f.name + " is in this reply already.");
      const got = await api("draft_file_add", { thread: id, data: await readFile(f) });
      if (!got.file || got.file.id !== digest) throw new Error("the desktop kept another file than " + f.name);
      d.files.push({ id: digest, name: f.name, size: f.size });
    }
    $("sendstatus").textContent = "";
  } catch (e) {
    if (e instanceof NotSignedIn) return restart();
    $("sendstatus").textContent = "Not attached: " + (e.message || String(e));
  }
  if (drafts.get(id) !== d) return;
  saveDraft(id);                 // with the files taken before any that failed
  if (state.openId === id) showDraft();
}

async function sha256hex(text) {
  return sha256bytes(new TextEncoder().encode(text));
}

async function sha256bytes(data) {
  const h = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(h), (b) => b.toString(16).padStart(2, "0")).join("");
}

// Run `action`, one that needs the passkey again (mailweb.STEP_UP: a reply, a message to the orchestrator, an unlock
// of the desktop), with `request` once the passkey has approved exactly it: the desktop binds the challenge to the
// action and to the request's JSON text, and refuses any other. `say(text)` shows how far it has got, and once it is
// approved says `more.sending` ("Sending…" unless given). For an unlock, `more.tail()` gives what goes after the
// request's line once it is approved (the password, never part of what is approved), or null when it is no longer to
// go. One at a time (`sending`). Answers what the action answers; raises what api raises, and NotAsked (with its
// message) when the approval fails or the tail is no longer to go, as the action's request has then not left the phone.
async function approved(action, request, say, more) {
  more = more || {};
  const text = JSON.stringify(request);
  sending = true;
  updateFab();
  try {
    say("Approve with your fingerprint…");
    let approval;
    try {
      const got = await api("stepup_begin", { approves: action, digest: await sha256hex(text) });
      approval = await sign(got.options);
    } catch (e) {
      throw e instanceof NotSignedIn ? e : new NotAsked(e.message || String(e));
    }
    const tail = more.tail ? more.tail() : undefined;
    if (tail === null) throw new NotAsked("let go before it was sent");
    say(more.sending || "Sending…");
    return await api(action, { request: text, credential: approval }, tail);
  } finally {
    sending = false;
    updateFab();
  }
}

// Send the open thread's draft: its answers, the Mac's comments and the owner's words, put together as the Mac puts
// them
// (ReplyText.reply), and its files, named from the desktop's drafts by their SHA-256; the passkey approves exactly
// this reply (approved), then the desktop sends it. Only once the thread's own mail is on screen (replyReady), and its
// answers have been seen to their end (answersUnseen). The draft holds the request, its key made at the first try,
// until the desktop has it, as the Mac's draft does (Draft.begin and finish), and is saved with that key first, so the
// desktop empties the draft holding it once the mailroom has the reply, on the Mac too; a draft that could not be
// saved, or changed on the Mac meanwhile (the note says so), is not sent. A refusal (the desktop kept nothing) lets
// the key go, and so does an approval that failed on a first try (nothing left the phone); any other failure, one with
// no answer too, may have reached the desktop, which keeps a key's first reply only, so the draft holds still
// (showHeld) and Send sends that same request again.
async function send() {
  if (!replyReady()) {
    $("sendstatus").textContent = "Not sent: this thread's mail is not on screen yet.";
    return;
  }
  if (answersUnseen()) {
    $("sendstatus").textContent = "Not sent: scroll the answers to their end first.";
    return;
  }
  keepDraft();
  const id = state.openId;
  const d = draft();
  if (blank(d) || sending) return;
  const again = !!d.held;                    // an earlier try may have reached the desktop
  $("sendstatus").textContent = "Saving the draft…";
  sending = true;
  updateFab();
  let how = "saved";
  try {
    // a key let go of here (its reply never left, or was refused), which the desktop's draft still holds, goes first:
    // the desktop takes no other key on a draft holding one
    if (!again && d.desk && d.desk.key) how = await saved(id);
    if (how === "saved") {
      d.held = d.held || request(id, newKey(), d);
      how = await saved(id);
    }
  } finally {
    sending = false;
    updateFab();
  }
  if (drafts.get(id) !== d) return;          // signed out meanwhile
  if (how !== "saved") {
    if (how.failed && !again) d.held = null;
    $("sendstatus").textContent = "Not sent: "
      + (how.conflict || "the draft could not be saved on the desktop: " + how.failed);
    if (state.openId === id) showDraft();
    return;
  }
  try {
    const sent = await approved("mail_reply", d.held, (text) => { $("sendstatus").textContent = text; });
    const fresh = newDraft();                // the desktop emptied the draft holding the reply's key
    if (sent.draft && sent.draft.cleared) Object.assign(fresh, { rev: sent.draft.rev, read: true });
    fresh.versions = d.versions;
    clearTimeout(d.timer);
    drafts.set(id, fresh);
    $("sendstatus").textContent = "";
    if (state.openId === id) {
      $("text").value = "";                  // what was sent: closing the sheet keeps the field into the new draft
      closeSheet();
      showDraft();
      markPicks();
      load(id, true);
    }
    renderRows();
    snack("Reply " + sent.status + "; the mailroom hands it to the agent within a minute.");
  } catch (e) {
    if (e instanceof NotSignedIn) return restart();
    if (e instanceof Refused || (e instanceof NotAsked && !again)) {
      d.held = null;
      saveDraft(id);                         // the desktop's draft lets go of the key too
    }
    $("sendstatus").textContent = "Not sent: " + (e.message || String(e));
    updateFab();
  }
}

// Discard the open thread's draft: emptied here at once, then on the desktop (draft_discard), which keeps it as a
// version, so the snackbar's Undo brings it back (restoreVersion); what was here and not yet on the desktop is saved
// there first, so it is kept too. When the draft had changed on the Mac since, nothing is discarded and it shows as
// it is there, with a note. When the desktop cannot be asked, the next save empties it there (kept as a version too),
// and Undo puts it back here.
async function discardDraft() {
  const id = state.openId, d = drafts.get(id);
  keepDraft();
  if (!d) return closeSheet();
  const before = snapshot(d);
  clearTimeout(d.timer);
  d.discarding = true;                       // no save meanwhile: closing the sheet would save it emptied
  closeSheet();
  Object.assign(d, { text: "", picks: [], comments: [], files: [], held: null, note: "", problem: "", open: false,
                     focus: null });
  showDraft();
  markPicks();
  renderRows();
  if (blank(before) && !before.key) {
    d.discarding = false;
    return;
  }
  const how = await idle(d);
  if (drafts.get(id) !== d) return;
  if (how !== "saved") {                     // the save before it was not taken: the draft shows as the Mac left it
    d.discarding = false;
    return;
  }
  d.busy = true;
  try {
    if (!same(before, d.desk)) {
      const put = await api("draft_put", { thread: id, base_rev: d.rev || 0, draft: before, device: "phone" });
      if (drafts.get(id) !== d) return;
      if (put.conflict) return notDiscarded(id, d, put);
      Object.assign(d, { rev: put.rev, desk: put.draft });
    }
    const got = await api("draft_discard", { thread: id, base_rev: d.rev || 0, device: "phone" });
    if (drafts.get(id) !== d) return;
    if (got.conflict) return notDiscarded(id, d, got);
    Object.assign(d, { rev: got.rev, desk: got.draft, versions: got.versions });
    snack("Draft discarded", "Undo", () => restoreVersion(id, got.version, before));
  } catch (e) {
    if (e instanceof NotSignedIn) return restart();
    snack("Draft discarded", "Undo", () => {
      adopt(id, d, Object.assign({}, before, { key: null }));
      renderRows();
      saveSoon(id);
    });
  } finally {
    d.discarding = false;
    if (drafts.get(id) === d) ended(id, d, "saved");
  }
}

// A discard the desktop did not make, as the draft had changed on the Mac since (`got`, its answer): the draft shows
// as it is there, and its note says so.
function notDiscarded(id, d, got) {
  Object.assign(d, { rev: got.rev, desk: got.draft, versions: got.versions });
  adopt(id, d, got.draft);
  d.note = "This draft changed on " + place(got.updated && got.updated.device) + " since, so it was not discarded.";
  if (state.openId === id) showDraft();
  snack(d.note);
}

// ---------------------------------------------------------------- a message to the orchestrator

// Like an email to the orchestrator (the Mac's Orchestrator button): it can start an agent, pass something on to one,
// or answer a question, and answers by email. The sheet's text box holds the message, whether the sheet is up or not,
// until it is sent or the page signs out (forget).
function openOrchestrator() {
  openSheet($("orchsheet"));
  $("orchtext").focus();
}

// Send the message: the passkey approves exactly it (approved), then the desktop starts the orchestrator, whose
// answer the snackbar says; a failure is said there too, and the message stays. Its key stays with it until it is
// sent or changed, so sending it again after an answer that was lost starts no second run.
async function messageOrchestrator() {
  const text = $("orchtext").value.trim();
  if (!text || sending) return;
  orchKey = orchKey || newKey();
  try {
    const got = await approved("orchestrate", { key: orchKey, text: text },
                               (said) => { $("orchstatus").textContent = said; });
    if (!got.output) throw new Error("the desktop's answer does not say what the orchestrator is doing");
    $("orchtext").value = "";
    orchKey = null;
    closeSheet();
    snack(got.output);
  } catch (e) {
    if (e instanceof NotSignedIn) return restart();
    snack("Not sent: " + (e.message || String(e)));
  } finally {
    $("orchstatus").textContent = "";
    updateFab();
  }
}

// ---------------------------------------------------------------- the desktop's keyring (README, "Unlock the desktop")

let keyring = null;              // the desktop keyrings' state from status ({state, error, collections}), or null
let unlockGoing = null;          // the unlock waiting for its approval ({cancelled}), until it is sent or let go

// The list's mark, as the Mac's top bar has it (KeyringMark in mac/Sources/AgentBoard/Keyring.swift, word for word;
// change both together): red "Desktop browser locked" while Chrome's keyring on the desktop is locked, grey "Desktop
// keyring unknown" while its state cannot be read, and nothing while it is unlocked or no state is sent. A tap opens
// the unlock sheet.
function showKeyring(st) {
  keyring = st || null;
  const s = keyring ? keyring.state : null;
  const mark = $("keyringmark");
  mark.hidden = s !== "locked" && s !== "unknown";
  mark.classList.toggle("unknown", s === "unknown");
  $("keyringtext").textContent = s === "unknown" ? "Desktop keyring unknown" : "Desktop browser locked";
}

// The keyring's state again, from the session's status (the desktop reads it at most once a minute). A status that
// cannot be had shows the state as unknown, with why; one asked before signing out is dropped.
function refreshKeyring() {
  const n = ++asked;
  return api("status", {}).then(
    (st) => { if (n > forgotAt) showKeyring(st.keyring); },
    (e) => { if (n > forgotAt) showKeyring({ state: "unknown", error: e.message || String(e) }); });
}

// How the sheet and its answers name the desktop's keyrings (keyring.py's WATCHED, Chrome's first), from their
// collections as status and keyring-unlock give them: the Mac's KeyringNames (mac/Sources/AgentBoard/Keyring.swift),
// word for word. Change both together. "default" is Chrome's keyring, by its label; the others by their labels, quoted.
const KEYRINGS = ["default", "login"];
function keyringName(key, cols) {
  const label = cols && cols[key] ? cols[key].label : null;
  const quoted = typeof label === "string" ? "'" + label + "'" : null;
  return key === "default" ? "Chrome's " + (quoted === null ? "keyring" : quoted) : (quoted === null ? key : quoted);
}

// The watched names, without login when the "default" alias names login itself: one keyring, named once.
function keyringKeys(cols) {
  const path = cols && cols.default ? cols.default.path : null;
  return path != null && path === (cols.login ? cols.login.path : null) ? ["default"] : KEYRINGS;
}

const keyringList = (keys, cols) => keys.map((k) => keyringName(k, cols)).join(" and ");

// One line on each watched keyring ("Chrome's 'Default keyring': locked · 'Login': unlocked"), or null without them.
function keyringLine(st) {
  const cols = st ? st.collections : null;
  if (!cols) return null;
  return keyringKeys(cols).map((k) => {
    const locked = cols[k] ? cols[k].locked : null;
    return keyringName(k, cols) + ": " + (locked == null ? "unknown" : locked === true ? "locked" : "unlocked");
  }).join(" · ");
}

// What the sheet says before an unlock: the Mac sheet's line for the keyrings' state (KeyringSheet, and
// KeyringMark.help while Chrome's keyring's state is unknown).
function keyringIdle() {
  const s = keyring ? keyring.state : null;
  if (s === "unknown") {
    return "The board can't tell whether " + keyringName("default", keyring.collections) + " on the desktop is locked: "
      + (keyring.error || "no reason given");
  }
  return keyringLine(keyring) || (s === "locked" ? "Chrome's keyring is locked." : "Chrome's keyring is unlocked.");
}

// What the sheet says of keyring_unlock's answer `a` (keyring-unlock's own JSON line: README, "Unlock the desktop"),
// as [text, good news]: the Mac's KeyringSaid.of, word for word. Change both together. `tried` names each keyring the
// password went to; a partial unlock opened one and not the other, and is good news when Chrome's opened.
function unlockSaid(a) {
  const tried = a.tried || {};
  const keys = keyringKeys(a.collections).filter((k) => tried[k] != null);
  const named = (outcome) => keys.filter((k) => tried[k] === outcome);
  const list = (ks) => keyringList(ks, a.collections);
  if (a.result === "unlocked") return ["Unlocked. The desktop's browser can use its passwords again.", true];
  if (a.result === "wrong") {
    const still = named("wrong");
    return ["Wrong password." + (still.length ? " Still locked: " + list(still) + "." : ""), false];
  }
  if (a.result === "partial") {
    const refused = named("wrong"), failed = named("error");
    let text = "Unlocked " + list(named("unlocked"));
    if (refused.length) text += ", but " + list(refused) + " has a different password";
    if (failed.length) {
      text += (refused.length ? "; " : ", but ") + list(failed) + " could not be unlocked: "
        + (a.error || "the desktop gave no reason");
    }
    return [text + ".", tried.default === "unlocked"];
  }
  if (a.result === "rate_limited") {
    const when = typeof a.retry_after === "number" ? "in " + Math.max(1, Math.ceil(a.retry_after / 60)) + " min"
      : "later (the desktop did not say when)";
    return ["Too many wrong passwords: try again " + when + ".", false];
  }
  return ["Not unlocked: " + (a.error || "the desktop gave no reason"), false];
}

function sayUnlock(text, how) {
  $("keyringsaid").textContent = text;
  $("keyringsaid").className = "status" + (how ? " " + how : "");
}

function openKeyring() {
  clearPassword();
  sayUnlock(keyringIdle());
  openSheet($("keyringsheet"));
  $("keyringpassword").focus();
}

// The field emptied: as its password is sent, on closing the sheet, on signing out and whenever the page is hidden.
function clearPassword() {
  $("keyringpassword").value = "";
  updateFab();
}

// The unlock waiting for its approval, if any, let go, so its password is not sent: the sheet closed, the page
// signed out, or the page was left.
function letUnlockGo() {
  if (unlockGoing) unlockGoing.cancelled = true;
  unlockGoing = null;
}

// Unlock the desktop's keyring with what was typed. The field is emptied at once, as on the Mac. The passkey approves
// this one unlock (approved) over a nonce made for it, so nothing made from the password, not even a digest, leaves
// the phone before the password itself, which goes once, after the request's line, with the approval, unless the
// unlock was let go meanwhile; the desktop gives it to keyring-unlock alone. Its answer is said in the sheet, and the
// mark shows the state read straight after, and a desktop that could not record the outcome says so after it.
async function unlockDesktop() {
  const typed = $("keyringpassword").value;
  if (!typed || sending) return;
  clearPassword();
  const going = unlockGoing = { cancelled: false };
  try {
    const got = await approved("keyring_unlock", { nonce: newKey() }, (said) => sayUnlock(said),
                               { sending: "Unlocking…", tail: () => (going.cancelled ? null : typed) });
    showKeyring(got.keyring);
    const [text, good] = unlockSaid(got.unlock);
    sayUnlock(got.error ? text + " (" + got.error + ")" : text, good ? "ok" : "bad");    // a record that failed
  } catch (e) {
    if (e instanceof NotSignedIn) return restart();
    if (!going.cancelled) sayUnlock("Not unlocked: " + (e.message || String(e)), "bad");
  } finally {
    if (unlockGoing === going) unlockGoing = null;
  }
}

// ---------------------------------------------------------------- touch: a row swiped away, the list pulled down

const swipe = { mode: null, li: null, x0: 0, y0: 0, dx: 0, at: 0, top: false, redraw: false, until: 0 };
let refreshing = false;

function touchStart(e) {
  if (e.touches.length !== 1 || refreshing) { swipe.mode = "none"; return; }
  const p = e.touches[0];
  Object.assign(swipe, { mode: null, x0: p.clientX, y0: p.clientY, dx: 0, at: Date.now(),
                         li: e.target.closest("#threads li[data-id]"), top: $("listscroll").scrollTop <= 0 });
}

function touchMove(e) {
  if (swipe.mode === "none" || e.touches.length !== 1) return;
  const p = e.touches[0];
  const dx = p.clientX - swipe.x0, dy = p.clientY - swipe.y0;
  if (!swipe.mode) {
    if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
    if (swipe.li && dx < 0 && Math.abs(dx) > Math.abs(dy) * 1.4) {
      swipe.mode = "swipe";
      const t = rowOf(swipe.li.dataset.id);
      const under = el("div", "under" + (t && swipeAction(t) === "inbox" ? " to-inbox" : ""));
      under.append(icon(t && swipeAction(t) === "inbox" ? "i-inbox" : "i-archive"),
                   el("span", "", t && swipeAction(t) === "inbox" ? "Inbox" : "Archive"));
      swipe.li.prepend(under);
      swipe.li.classList.add("swiping");
    } else if (swipe.top && dy > 0 && Math.abs(dy) > Math.abs(dx)) {
      swipe.mode = "pull";
      $("ptr").classList.remove("settle");
    } else {
      swipe.mode = "none";
      return;
    }
  }
  if (swipe.mode === "swipe") {
    swipe.dx = Math.min(0, dx);
    swipe.li.querySelector(".row").style.transform = "translateX(" + swipe.dx + "px)";
  } else if (swipe.mode === "pull") {
    const d = Math.min(Math.max(dy, 0) * 0.5, 110);
    swipe.dx = d;
    const ptr = $("ptr");
    ptr.style.opacity = String(Math.min(1, d / 64));
    ptr.style.transform = "translateY(" + (d - 40) + "px) rotate(" + d * 4 + "deg)";
  }
}

function touchEnd() {
  const mode = swipe.mode;
  swipe.mode = null;
  if (mode === "swipe") endSwipe();
  else if (mode === "pull") endPull(swipe.dx);
}

// A row swiped past 35% of its width, or flicked, is archived (out of the inbox, moved back to it), with Undo.
function endSwipe() {
  const li = swipe.li, row = li.querySelector(".row");
  const width = li.getBoundingClientRect().width;
  const flick = swipe.dx < -60 && Date.now() - swipe.at < 250;
  swipe.until = Date.now() + 400;                    // the click a lifted finger may still make is not an open
  li.classList.remove("swiping");
  const t = rowOf(li.dataset.id);
  if (!t || !(swipe.dx < -0.35 * width || flick)) {
    row.style.transform = "";
    setTimeout(() => { li.querySelector(".under") && li.querySelector(".under").remove(); finishSwipe(); }, 240);
    return;
  }
  row.style.transform = "translateX(-100%)";
  setTimeout(() => {
    li.style.height = li.offsetHeight + "px";
    void li.offsetHeight;
    li.classList.add("gone");
    li.style.height = "0px";
    setTimeout(() => { change(t, swipeAction(t)); finishSwipe(); }, 230);
  }, 180);
}

function finishSwipe() {
  if (swipe.redraw) {
    swipe.redraw = false;
    renderRows(true);
  }
}

// The list pulled down past 64 px (at half the finger's travel) is read again, the spinner turning until it comes.
async function endPull(d) {
  const ptr = $("ptr");
  ptr.classList.add("settle");
  if (d < 64) {
    ptr.style.opacity = "0";
    ptr.style.transform = "";
    return;
  }
  refreshing = true;
  ptr.style.transform = "translateY(16px)";
  ptr.classList.add("spinning");
  const started = Date.now();
  await quietRefresh();
  await new Promise((r) => setTimeout(r, Math.max(0, 500 - (Date.now() - started))));
  ptr.classList.remove("spinning");
  ptr.style.opacity = "0";
  ptr.style.transform = "";
  refreshing = false;
}

// ---------------------------------------------------------------- wiring

history.scrollRestoration = "manual";
$("threads").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-id]");
  if (li && Date.now() > swipe.until) openThread(li.dataset.id);
});
const scroller = $("listscroll");
scroller.addEventListener("touchstart", touchStart, { passive: true });
scroller.addEventListener("touchmove", touchMove, { passive: true });
scroller.addEventListener("touchend", touchEnd, { passive: true });
scroller.addEventListener("touchcancel", touchEnd, { passive: true });
document.querySelectorAll("#boxes button").forEach((b) => b.addEventListener("click", () => pickBox(b.dataset.box)));
let searching;
$("query").addEventListener("input", () => {
  clearTimeout(searching);
  $("clearquery").hidden = !$("query").value;
  searching = setTimeout(() => search($("query").value.trim()), 300);
});
$("query").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { clearTimeout(searching); search($("query").value.trim()); $("query").blur(); }
});
$("clearquery").addEventListener("click", () => { $("query").value = ""; search(""); });
$("menubutton").addEventListener("click", (e) => { e.stopPropagation(); $("menu").hidden = !$("menu").hidden; });
document.addEventListener("click", () => { $("menu").hidden = true; });
$("refresh").addEventListener("click", () => quietRefresh());
$("signout").addEventListener("click", () => guarded(signOut, $("listerror")));
$("back").addEventListener("click", leaveThread);
$("archive").addEventListener("click", () => act(state.shown && state.shown.thread.box === "inbox" ? "archive" : "inbox"));
$("unread").addEventListener("click", () => act("unread"));
$("snoozebutton").addEventListener("click", openSnooze);
$("fast").addEventListener("click", toggleFast);
$("snoozeat").addEventListener("input", () => { $("snoozepicked").disabled = !pickedSnooze(); });
$("snoozepicked").addEventListener("click", () => {
  const at = pickedSnooze();
  if (!at) return;
  closeSheet();
  act("snooze", at);
});
$("extrasbutton").addEventListener("click", () => openSheet($("extrasheet")));
$("replybutton").addEventListener("click", () => {
  if (!replyReady()) return;
  draft().open = true;
  showDraft();
  $("text").focus();
});
$("scrim").addEventListener("click", closeSheet);
document.querySelectorAll(".sheetclose").forEach((b) => b.addEventListener("click", closeSheet));
$("doc").addEventListener("load", frameLoaded);
$("text").addEventListener("input", () => { keepDraft(); updateFab(); saveSoon(state.openId); });
$("answers").addEventListener("scroll", updateFab, { passive: true });
window.addEventListener("resize", updateFab);       // the keyboard, shown or hidden, changes the answers' room
$("attach").addEventListener("change", () => { const l = $("attach").files; attach(l).finally(() => { $("attach").value = ""; }); });
$("discard").addEventListener("click", discardDraft);
$("versionsbutton").addEventListener("click", () => { $("versions").hidden = !$("versions").hidden; });
$("send").addEventListener("click", send);
$("orchbutton").addEventListener("click", openOrchestrator);
$("orchtext").addEventListener("input", () => { orchKey = null; updateFab(); });   // another message: another key
$("orchsend").addEventListener("click", messageOrchestrator);
$("keyringmark").addEventListener("click", openKeyring);
$("keyringpassword").addEventListener("input", updateFab);
$("keyringpassword").addEventListener("keydown", (e) => { if (e.key === "Enter") unlockDesktop(); });
$("keyringunlock").addEventListener("click", unlockDesktop);
window.addEventListener("pagehide", () => { letUnlockGo(); clearPassword(); saveAll(true); });
window.addEventListener("popstate", (e) => {
  if (e.state && e.state.view === "thread") {
    if (!state.openId && state.lastId && rowOf(state.lastId)) showThread(state.lastId);
    else if (!state.openId) history.replaceState(null, "");
  } else if (state.openId) {
    hideThread();
  }
});
setInterval(() => { if (!document.hidden && state.signedIn) { quietRefresh(); refreshKeyring(); } }, POLL_MS);
setInterval(() => { if (!document.hidden && state.threads) { renderRows(); showFast(); } }, FAST_TICK_MS);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearPassword();                             // an unlock under way holds its password already, and goes on
    saveAll(true);                               // Android may close the page from here: the drafts are on the desktop
  }
  else if (state.signedIn && Date.now() - listedAt > 10000) { quietRefresh(); refreshKeyring(); }
});
restart();
