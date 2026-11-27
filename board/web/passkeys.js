// Passkeys (WebAuthn) in the browser, for the pages mail-web serves: the Mail page (web/mail/app.js) and the voice
// page (web/voice/voice.js). One copy, served to each page at its own address (agentboard/mailweb.py, voiceweb.py),
// as the desktop checks them (agentboard/webauthn.py): the server's options in, the credential it takes out.
"use strict";

// base64url <-> bytes, for the passkey calls
function fromB64url(s) {
  const b = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));
  return Uint8Array.from(b, (c) => c.charCodeAt(0));
}
function toB64url(buf) {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// A list of the server's {type, id} passkey descriptors, each id as bytes.
function idsToBytes(list) {
  return list.map((c) => ({ type: c.type, id: fromB64url(c.id) }));
}

// A passkey's signature over the server's options for navigator.credentials.get(), as the server checks it
// (webauthn.assertion).
async function sign(o) {
  const opts = Object.assign({}, o, { challenge: fromB64url(o.challenge) });
  opts["allowCredentials"] = idsToBytes(o["allowCredentials"]);
  const made = await navigator.credentials.get({ publicKey: opts });
  return {
    id: toB64url(made.rawId),
    clientDataJSON: toB64url(made.response.clientDataJSON),
    authenticatorData: toB64url(made.response.authenticatorData),
    signature: toB64url(made.response.signature),
  };
}

// A new passkey made with the server's options for navigator.credentials.create(), as the server checks it
// (webauthn.registration).
async function createPasskey(o) {
  const opts = Object.assign({}, o, {
    challenge: fromB64url(o.challenge),
    user: Object.assign({}, o.user, { id: fromB64url(o.user.id) }),
  });
  opts["excludeCredentials"] = idsToBytes(o["excludeCredentials"]);
  const made = await navigator.credentials.create({ publicKey: opts });
  const r = made.response;
  return {
    id: toB64url(made.rawId),
    clientDataJSON: toB64url(r.clientDataJSON),
    authenticatorData: toB64url(r.getAuthenticatorData()),
    publicKey: toB64url(r.getPublicKey()),
    publicKeyAlgorithm: r.getPublicKeyAlgorithm(),
  };
}
