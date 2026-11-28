"""The relying party's side of WebAuthn (passkeys) for a page on one origin: checking a new passkey when it is enrolled
(registration) and a signed sign-in or approval with it (assertion), as the W3C WebAuthn Level 2 specification lays
out (sections 7.1 and 7.2). Only ES256 keys (ECDSA on P-256 with SHA-256), which Android's passkeys use, are taken.
Signatures are checked with the `cryptography` package, never by hand. The caller issues each challenge, keeps it
until it is used once, and stores the credentials.

A registration is checked without attestation ("none"): what makes a new passkey trustworthy is the one-time
enrolment code its page was opened with, which the caller checks. The passkey's public key comes from the browser's
AuthenticatorAttestationResponse.getPublicKey() (SubjectPublicKeyInfo, DER); it and the credential id must be the ones
in the authenticator data's attested credential data (its key a COSE_Key, read by _cose_p256).
"""
import base64
import hashlib
import hmac
import json
import struct
from typing import NamedTuple, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

ES256 = -7                    # COSE algorithm: ECDSA with SHA-256 on P-256
UP, UV, AT = 0x01, 0x04, 0x40 # authenticator data flags: user present, user verified, attested credential data


class Refused(Exception):
    """A registration or assertion that is not accepted, and why."""


class AuthData(NamedTuple):
    rp_id_hash: bytes
    flags: int
    sign_count: int
    credential_id: Optional[bytes]      # in a registration's authenticator data (the AT flag), else None
    credential_key: Optional[bytes]     # and its public key (a COSE_Key, CBOR) with whatever follows it


class Credential(NamedTuple):
    """An enrolled passkey: its credential id, its public key (SubjectPublicKeyInfo, DER) and the last sign count."""
    id: bytes
    public_key: bytes
    sign_count: int


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text):
    """The bytes of base64url `text` (padding optional). Raises Refused when it is not base64url."""
    if not isinstance(text, str) or not all(c.isalnum() or c in "-_=" for c in text):
        raise Refused("not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        raise Refused("not base64url") from None


def auth_data(data):
    """The fields of authenticator data `data` the relying party checks. Raises Refused when it is too short."""
    if len(data) < 37:
        raise Refused("the authenticator data is %d bytes, under the 37 it always has" % len(data))
    rp_id_hash, flags, count = data[:32], data[32], struct.unpack(">I", data[33:37])[0]
    cred = key = None
    if flags & AT:
        if len(data) < 55:
            raise Refused("the authenticator data says it holds a credential, but is too short for one")
        n = struct.unpack(">H", data[53:55])[0]
        cred, key = data[55:55 + n], data[55 + n:]
        if len(cred) != n:
            raise Refused("the authenticator data's credential id is cut short")
    return AuthData(rp_id_hash, flags, count, cred, key)


COSE_DEPTH = 1         # maps a COSE_Key nests: the key itself, with no map inside it


def _cbor_item(data, at, depth):
    """(the CBOR item at offset `at` of `data`, the offset after it), for the kinds a COSE_Key uses: whole numbers,
    negative ones, byte strings, and maps of them keyed by numbers, `depth` deep at most. Raises Refused for anything
    else or data cut short."""
    if at >= len(data):
        raise Refused("the authenticator data's key is cut short (CBOR)")
    kind, info, at = data[at] >> 5, data[at] & 31, at + 1
    if info < 24:
        n = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        if at + size > len(data):
            raise Refused("the authenticator data's key is cut short (CBOR)")
        n, at = int.from_bytes(data[at:at + size], "big"), at + size
    else:
        raise Refused("the authenticator data's key is CBOR of a length this does not read")
    if kind == 0:
        return n, at
    if kind == 1:
        return -1 - n, at
    if kind == 2:
        if at + n > len(data):
            raise Refused("the authenticator data's key is cut short (CBOR)")
        return data[at:at + n], at + n
    if kind == 5:
        if depth <= 0:
            raise Refused("the authenticator data's key nests deeper than a COSE key does")
        items = {}
        for _ in range(n):
            k, at = _cbor_item(data, at, depth - 1)
            if not isinstance(k, int):
                raise Refused("the authenticator data's key has a map key that is not a number")
            items[k], at = _cbor_item(data, at, depth - 1)
        return items, at
    raise Refused("the authenticator data's key holds CBOR of a kind a COSE key does not use")


def _cose_p256(data):
    """(x, y) of the P-256 ES256 COSE_Key at the start of `data`: {1: 2 (EC2), 3: -7 (ES256), -1: 1 (P-256), -2: x,
    -3: y}. Raises Refused for any other key."""
    key, _ = _cbor_item(data, 0, COSE_DEPTH)
    if not (isinstance(key, dict) and key.get(1) == 2 and key.get(3) == ES256 and key.get(-1) == 1
            and isinstance(key.get(-2), bytes) and len(key[-2]) == 32
            and isinstance(key.get(-3), bytes) and len(key[-3]) == 32):
        raise Refused("the authenticator data's key is not a P-256 COSE key for ES256")
    return int.from_bytes(key[-2], "big"), int.from_bytes(key[-3], "big")


def _client_data(raw, kind, challenge, origin):
    """Check clientDataJSON `raw`: its type is `kind`, its challenge is `challenge` (bytes) and its origin is
    `origin`, not from inside another site's frame."""
    try:
        cd = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise Refused("the client data is not JSON") from None
    if not isinstance(cd, dict) or cd.get("type") != kind:
        raise Refused("the client data's type is not %s" % kind)
    if not hmac.compare_digest(unb64url(cd.get("challenge", "")), challenge):
        raise Refused("the client data's challenge is not the one issued")
    if cd.get("origin") != origin:
        raise Refused("the client data's origin %r is not %s" % (cd.get("origin"), origin))
    if cd.get("crossOrigin") is True:
        raise Refused("the passkey was used from inside another site's frame")


def _checked_auth_data(data, rp_id):
    """auth_data(data), with its relying party `rp_id` and the user both present and verified."""
    ad = auth_data(data)
    if not hmac.compare_digest(ad.rp_id_hash, hashlib.sha256(rp_id.encode()).digest()):
        raise Refused("the passkey is for another site, not %s" % rp_id)
    if not ad.flags & UP:
        raise Refused("the user was not present")
    if not ad.flags & UV:
        raise Refused("the user was not verified (no fingerprint or screen lock)")
    return ad


def _p256_key(der):
    try:
        key = serialization.load_der_public_key(der)
    except ValueError:
        raise Refused("the public key is not a SubjectPublicKeyInfo") from None
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise Refused("the public key is not an ES256 (P-256) key")
    return key


def registration(rp_id, origin, challenge, credential_id, client_data_json, authenticator_data, public_key,
                 public_key_alg):
    """A new passkey from navigator.credentials.create(), checked: the client data is a create for `challenge` on
    `origin`; the authenticator data is for `rp_id`, with the user present and verified, and holds `credential_id`
    and the key `public_key`; the key is ES256. All but the ids and strings are bytes. Returns its Credential. Raises
    Refused."""
    _client_data(client_data_json, "webauthn.create", challenge, origin)
    ad = _checked_auth_data(authenticator_data, rp_id)
    if ad.credential_id is None or not hmac.compare_digest(ad.credential_id, credential_id):
        raise Refused("the authenticator data does not hold the credential id given")
    if public_key_alg != ES256:
        raise Refused("only ES256 passkeys are taken, not algorithm %r" % public_key_alg)
    numbers = _p256_key(public_key).public_numbers()
    if _cose_p256(ad.credential_key) != (numbers.x, numbers.y):
        raise Refused("the public key given is not the key in the authenticator data")
    return Credential(credential_id, public_key, ad.sign_count)


def assertion(rp_id, origin, challenge, credential, client_data_json, authenticator_data, signature):
    """A sign-in or approval from navigator.credentials.get() with Credential `credential`, checked: the client data
    is a get for `challenge` on `origin`; the authenticator data is for `rp_id`, with the user present and verified;
    the signature is the passkey's over the authenticator data and the client data's hash; and the sign count went up
    (a passkey that keeps none, as synced ones do, sends 0 each time). Returns the new sign count. Raises Refused."""
    _client_data(client_data_json, "webauthn.get", challenge, origin)
    ad = _checked_auth_data(authenticator_data, rp_id)
    try:
        _p256_key(credential.public_key).verify(
            signature, authenticator_data + hashlib.sha256(client_data_json).digest(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise Refused("the signature is not the passkey's") from None
    if (ad.sign_count or credential.sign_count) and ad.sign_count <= credential.sign_count:
        raise Refused("the sign count did not go up (%d after %d): the passkey may have been copied"
                      % (ad.sign_count, credential.sign_count))
    return ad.sign_count
