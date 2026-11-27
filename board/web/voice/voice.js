// The voice page, the owner's front desk (agentboard/voiceweb.py; README, "Voice page"). Talk asks the desktop for a
// token for one conversation, opens Google's Gemini Live socket with it, and streams the microphone there; Gemini's
// voice is played as it comes, and both sides are written down as Gemini transcribes them. When Gemini calls a tool,
// the page asks the desktop (POST /voice/api, "tool"), which checks the call, and gives Gemini the desktop's answer.
// The page shows only text (textContent, never markup), keeps nothing (no storage), and loads nothing from elsewhere.
// Each conversation's transcript is saved on the desktop (POST /voice/api, "transcript"), a turn at a time.
// It signs in, and enrols this device's passkey from the link `agent-board voice-enrol` gives (/voice/enrol#CODE),
// through its own API, which takes the Mail page's passkey calls: a device of the voice page alone has no other way in.
"use strict";

const API = "/voice/api";
const OUT_RATE = 24000;                 // Gemini speaks 16-bit PCM, mono, 24 kHz, little-endian
const DOING = {                          // what the page says while a tool runs, by the tool's name
  list_agents: () => "Listing the agents",
  what_am_i_looking_at: () => "Looking at your Mac's screen",
  ask_agent: (args) => "Asking " + (typeof args.agent === "string" ? args.agent.slice(0, 100) : "an agent"),
};
const $ = (id) => document.getElementById(id);

let talk = null;                         // the conversation going, or null (start)
let record = null;                       // the latest conversation's transcript: {id, log, sent, saving}
const SAVE_BATCH = 50;                   // entries one "transcript" call carries at most: voiceweb.TRANSCRIPT_LINES

class NotSignedIn extends Error {}

async function api(action, body) {
  const res = await fetch(API, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Agent-Board": "1" },
    body: JSON.stringify(Object.assign({ action: action }, body)),
    cache: "no-store",
  });
  let answer;
  try { answer = await res.json(); } catch (e) { throw new Error("the desktop answered " + res.status); }
  if (res.status === 401) throw new NotSignedIn(answer.error || "not signed in");
  if (!answer.ok) throw new Error(answer.error || "the desktop refused, with no reason");
  return answer;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function shown(e, text) {
  e.textContent = text || "";
  e.hidden = !text;
}

function setState(text, asking) {
  $("state").textContent = text;
  $("barstate").textContent = text;
  $("state").classList.toggle("asking", !!asking);
}

function scrollDown() {
  window.scrollTo(0, document.body.scrollHeight);
}

// ---------------------------------------------------------------- the transcript

// A new entry: "you" (the owner), "desk" (Gemini), "tool" (a tool call) or "note". It goes in the saved transcript too,
// as it reads when next saved, unless `unsaved` (a tool call's line, saved once it has ended: logged).
function entry(kind, text, unsaved) {
  const li = el("li", kind);
  if (kind === "you" || kind === "desk") li.append(el("span", "who", kind === "you" ? "You" : "Front desk"));
  const body = el("span", "text", text || "");
  li.append(body);
  $("transcript").append(li);
  if (record && !unsaved) record.log.push({ who: kind, body: body });
  scrollDown();
  return body;
}

// Gemini's transcription of one side (`who`), added to that side's entry for this turn.
function heard(t, who, text) {
  if (!text) return;
  if (!t.lines[who]) t.lines[who] = entry(who);
  t.lines[who].textContent += text;
  scrollDown();
}

// The conversation's id for its file on the desktop: its start's local time and six random hex digits.
function conversationId() {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  const r = Array.from(crypto.getRandomValues(new Uint8Array(3)), (b) => b.toString(16).padStart(2, "0")).join("");
  return "" + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "-" + p(d.getHours()) + p(d.getMinutes())
    + p(d.getSeconds()) + "-" + r;
}

// A line for the saved transcript alone, as it reads now: a tool call's, once it has ended.
function logged(who, text) {
  if (record) record.log.push({ who: who, body: { textContent: text } });
}

// The entries not yet saved, sent to the desktop in order, one call after another; a failure is shown and the
// entries are tried again with the next save. It runs when a turn is complete and when the conversation ends, so no
// entry is sent while Gemini's transcription may still be adding to it.
function save() {
  const r = record;
  if (!r || r.saving) { if (r) r.again = true; return; }
  const batch = r.log.slice(r.sent, r.sent + SAVE_BATCH)
    .map((e) => ({ who: e.who, text: e.body.textContent || "" })).filter((e) => e.text);
  const upto = Math.min(r.log.length, r.sent + SAVE_BATCH);
  if (upto === r.sent) return;
  if (!batch.length) { r.sent = upto; return save(); }
  r.saving = true;
  api("transcript", { conversation: r.id, entries: batch }).then(() => {
    r.sent = upto;
    r.saving = false;
    if (r.again || r.sent < r.log.length) { r.again = false; save(); }
  }, (e) => {
    r.saving = false;
    shown($("error"), "The transcript could not be saved on the desktop: " + (e.message || String(e)));
  });
}

// ---------------------------------------------------------------- sound

function b64(buffer) {
  const bytes = new Uint8Array(buffer);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

// Gemini's voice (base64 PCM at OUT_RATE), played after what is already queued.
function play(t, data) {
  const raw = atob(data);
  const view = new DataView(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) view.setUint8(i, raw.charCodeAt(i));
  const n = raw.length >> 1;
  if (!n) return;
  const buf = t.ctx.createBuffer(1, n, OUT_RATE);
  const ch = buf.getChannelData(0);
  for (let i = 0; i < n; i++) ch[i] = view.getInt16(2 * i, true) / 32768;
  const src = t.ctx.createBufferSource();
  src.buffer = buf;
  src.connect(t.ctx.destination);
  const at = Math.max(t.ctx.currentTime + 0.02, t.playAt);
  src.start(at);
  t.playAt = at + buf.duration;
  t.playing.add(src);
  src.onended = () => t.playing.delete(src);
}

// Stop Gemini's voice at once (the owner spoke over it, or the conversation ended).
function hush(t) {
  for (const src of t.playing) {
    try { src.stop(); } catch (e) { /* it had not started, or had ended */ }
  }
  t.playing.clear();
  t.playAt = 0;
}

// The microphone, through capture.js, each 40 ms chunk sent to Gemini once the session is set up.
async function listen(t) {
  await t.ctx.audioWorklet.addModule("/voice/capture.js");
  const node = new AudioWorkletNode(t.ctx, "pcm16-capture");
  node.port.onmessage = (e) => {
    if (!(e.data instanceof ArrayBuffer)) { t.rate = e.data.rate; return; }
    if (t.ready && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ realtimeInput: { audio: { data: b64(e.data), mimeType: "audio/pcm;rate=" + t.rate } } }));
    }
  };
  const silent = t.ctx.createGain();      // the node must reach the output to be run; it adds nothing to it
  silent.gain.value = 0;
  t.ctx.createMediaStreamSource(t.stream).connect(node);
  node.connect(silent).connect(t.ctx.destination);
  t.node = node;
}

// ---------------------------------------------------------------- Gemini's messages

function send(t, msg) {
  if (t.ws && t.ws.readyState === WebSocket.OPEN) t.ws.send(JSON.stringify(msg));
}

function received(t, data) {
  let msg;
  try {
    msg = JSON.parse(typeof data === "string" ? data : new TextDecoder().decode(data));
  } catch (e) {
    entry("note", "A message from Gemini could not be read: " + (e.message || String(e)));
    return;
  }
  if (msg.setupComplete) {
    t.ready = true;
    setState("Listening. Speak when you're ready.");
    return;
  }
  if (msg.toolCall) {
    for (const call of msg.toolCall.functionCalls || []) runTool(t, call);
    return;
  }
  if (msg.toolCallCancellation) {
    for (const id of msg.toolCallCancellation.ids || []) {
      const going = t.calls.get(id);
      if (going) {
        t.calls.delete(id);
        going.line.textContent += ": cancelled";
        logged("tool", going.line.textContent);
        going.line.parentNode.classList.remove("going");
      }
    }
    showAsking(t);
    return;
  }
  if (msg.goAway) {
    entry("note", "Gemini will end this conversation shortly; tap Talk again to go on.");
    return;
  }
  const sc = msg.serverContent;
  if (!sc) return;
  if (sc.interrupted) hush(t);
  if (sc.inputTranscription) {
    if (t.lines.desk) t.lines.desk = null;          // the owner speaks after Gemini: a new entry
    heard(t, "you", sc.inputTranscription.text);
  }
  for (const part of (sc.modelTurn && sc.modelTurn.parts) || []) {
    if (part.inlineData && part.inlineData.data) play(t, part.inlineData.data);
  }
  if (sc.outputTranscription) {
    t.lines.you = null;                              // Gemini answers: the owner's entry is done
    heard(t, "desk", sc.outputTranscription.text);
  }
  if (sc.turnComplete) {
    t.lines = {};
    save();
  }
}

// The state line while a tool runs: what the first of them is doing.
function showAsking(t) {
  const first = t.calls.values().next().value;
  if (first) setState(first.doing + "…", true);
  else if (t.ready) setState("Listening.");
}

// One of Gemini's tool calls, asked of the desktop; its answer (or why there is none) goes back to Gemini, unless
// Gemini cancelled the call or the conversation has ended.
async function runTool(t, call) {
  const args = call.args && typeof call.args === "object" ? call.args : {};
  const doing = (DOING[call.name] || (() => "Running " + String(call.name).slice(0, 40)))(args);
  const line = entry("tool", doing, true);
  line.parentNode.classList.add("going");
  const going = { doing: doing, line: line };
  t.calls.set(call.id, going);
  showAsking(t);
  let response;
  const started = Date.now();
  try {
    response = (await api("tool", { name: call.name, args: args })).response;
  } catch (e) {
    if (e instanceof NotSignedIn) signedOut(true);
    response = { error: e.message };
  }
  if (t.calls.get(call.id) !== going) return;       // cancelled, or the conversation has ended
  t.calls.delete(call.id);
  line.parentNode.classList.remove("going");
  line.textContent = doing + (response.error ? ": failed, " + response.error
    : ": done in " + Math.round((Date.now() - started) / 1000) + " s");
  logged("tool", line.textContent);
  showAsking(t);
  const answer = { id: call.id, name: call.name, response: response };
  if (call.name === "ask_agent") answer.scheduling = "WHEN_IDLE";    // said once Gemini's filler line is done
  send(t, { toolResponse: { functionResponses: [answer] } });
}

// ---------------------------------------------------------------- a conversation

async function start() {
  // the sound must be started by the tap itself (Safari), before anything is waited for
  const Ctx = window.AudioContext || window.webkitAudioContext;
  const t = talk = { ctx: new Ctx(), lines: {}, calls: new Map(), playing: new Set(), playAt: 0, ready: false,
                     rate: 16000, ws: null, stream: null, node: null };
  t.ctx.resume();
  if (navigator.audioSession) navigator.audioSession.type = "play-and-record";   // Safari: the speaker, not the earpiece
  record = { id: conversationId(), log: [], sent: 0, saving: false, again: false };
  setButton(true);
  shown($("error"), "");
  setState("Starting…");
  entry("note", "Conversation started " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  try {
    t.stream = await navigator.mediaDevices.getUserMedia({ audio: {
      channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    if (talk !== t) return stop(t);
    const got = await api("start", {});
    if (talk !== t) return stop(t);
    await listen(t);
    t.ws = new WebSocket(got.url + "?access_token=" + encodeURIComponent(got.token));
    t.ws.binaryType = "arraybuffer";
    t.ws.onopen = () => send(t, { setup: { model: "models/" + got.model } });   // the token's setup is the one used
    t.ws.onmessage = (e) => received(t, e.data);
    t.ws.onclose = (e) => ended(t, e.code === 1000 ? "" : "Gemini closed the conversation (" + e.code
                                + (e.reason ? ": " + e.reason.slice(0, 200) : "") + ").");
    setState("Connecting to Gemini…");
  } catch (e) {
    if (e instanceof NotSignedIn) signedOut(true);
    ended(t, e.name === "NotAllowedError" ? "The microphone is not allowed for this page." : e.message || String(e));
  }
}

// Everything of conversation `t` stopped: its socket, microphone and sound.
function stop(t) {
  t.ready = false;
  for (const going of t.calls.values()) logged("tool", going.doing + ": still going when the conversation ended");
  t.calls.clear();
  if (t.ws && t.ws.readyState <= WebSocket.OPEN) t.ws.close(1000);
  if (t.stream) t.stream.getTracks().forEach((track) => track.stop());
  hush(t);
  if (t.ctx.state !== "closed") t.ctx.close();
}

function ended(t, why) {
  stop(t);
  if (talk !== t) return;
  talk = null;
  setButton(false);
  setState("Tap Talk to start a conversation.");
  if (why) shown($("error"), why);
  entry("note", "Conversation ended");
  save();
}

function setButton(on) {
  $("talk").classList.toggle("on", on);
  $("talklabel").textContent = on ? "Stop" : "Talk";
  $("talk").setAttribute("aria-pressed", on ? "true" : "false");
  $("bar").hidden = !on;                         // Stop at the bottom too, however long the transcript grows
  document.body.classList.toggle("talking", on);
}

// ---------------------------------------------------------------- signing in and enrolling (passkeys)
// The passkey calls, sign and createPasskey, are web/passkeys.js's, the Mail page's own; so are the server's handlers
// behind this page's signin_* and enrol_* (mailweb.py).

async function signIn() {
  const got = await api("signin_begin", {});
  await api("signin_finish", { credential: await sign(got.options) });
  return load();
}

async function enrol(code) {
  const got = await api("enrol_begin", { code: code });
  await api("enrol_finish", { credential: await createPasskey(got.options) });
  return load();
}

// The gate over the page: `text`, and a button doing `action` (none when `button` is empty).
function gate(text, button, action) {
  $("gate").hidden = false;
  $("talk").disabled = true;
  $("gatetext").textContent = text;
  const b = $("gatebutton");
  b.hidden = !button;
  b.textContent = button || "";
  b.onclick = async () => {
    b.disabled = true;
    shown($("error"), "");
    try { await action(); } catch (e) { shown($("error"), e.message || String(e)); } finally { b.disabled = false; }
  };
}

// Not signed in: a sign-in button once any passkey is enrolled (the phone's may be, and not this device's), and how to
// enrol this device's.
function signedOut(enrolled) {
  setState("");
  const how = "Run agent-board voice-enrol on the desktop and open the link it gives on this device.";
  if (!enrolled) return gate("No passkey is enrolled for the front desk. " + how, null, null);
  gate("Sign in with this device's passkey to talk. No passkey on this device yet? " + how,
       "Sign in with your passkey", signIn);
}

async function load() {
  $("talk").onclick = () => (talk ? ended(talk, "") : start());
  $("stop").onclick = () => { if (talk) ended(talk, ""); };
  $("gate").hidden = true;
  const code = location.pathname === "/voice/enrol" ? location.hash.slice(1) : "";
  if (code) {
    history.replaceState(null, "", "/voice");        // the code leaves the address bar and the history
    setState("");
    return gate("Enrol this device's passkey for the front desk. You'll be asked for your fingerprint or password.",
                "Enrol this device", () => enrol(code));
  }
  try {
    const st = await api("status", {});
    if (st.model) $("model").textContent = st.model;
    if (!st.ready) return shown($("error"), st.error);
    if (!st.signed_in) return signedOut(st.enrolled);
    $("talk").disabled = false;
    setState("Tap Talk to start a conversation.");
  } catch (e) {
    setState("");
    shown($("error"), "The desktop could not be reached: " + (e.message || String(e)));
  }
}

load();
