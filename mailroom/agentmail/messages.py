"""The message store: every mail outbound.transmit sends or keeps (agents' mail and mailroom alerts, not the nudge),
as its exact MIME bytes, and every reply the owner writes in the Mail tab with files (mailbox.reply_message, which no
Gmail sees), each in a file named by its sha256 under config.MESSAGES_DIR (`<first 2 hex>/<sha256>.eml`).
outbound.transmit writes each mail here before it sends or keeps it, and the sent row records the sha256
(`sent.eml`); mailbox.answer keeps a reply with files, and its inbound row records the sha256 (`inbound.eml`). A file
never changes once written, so a copy of it (agent-mail backup) is checked by its name alone."""
import hashlib
import re
from pathlib import Path

from . import config, jsonfile

SHA_RE = re.compile(r"[0-9a-f]{64}")
FILE_MODE = 0o600
DIR_MODE = 0o700


def path_in(root, sha):
    """Where the message of sha256 sha lives under root (config.MESSAGES_DIR, or a backup's copy of it). Raises
    ValueError for anything but 64 lower-case hex digits."""
    if not SHA_RE.fullmatch(sha or ""):
        raise ValueError("not a message sha256: %r" % (sha,))
    return Path(root) / sha[:2] / (sha + ".eml")


def write_durably(path, data):
    """Write data to path whole and on disk before returning (jsonfile.write_bytes), mode FILE_MODE. Makes path's
    folder and that folder's parent (DIR_MODE) when missing, but never a folder above those: under a backup target on
    a disk that is not mounted, the write fails instead of filling the system disk."""
    path = Path(path)
    for folder in (path.parent.parent, path.parent):
        folder.mkdir(mode=DIR_MODE, exist_ok=True)
    jsonfile.write_bytes(path, data, FILE_MODE, True)


def keep(raw):
    """Store raw (a whole message's bytes) and return its sha256 in hex. A message already stored is not written
    again. Raises OSError when it cannot be written."""
    sha = hashlib.sha256(raw).hexdigest()
    path = path_in(config.MESSAGES_DIR, sha)
    if not path.exists():
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)   # a first send before the daemon ever ran
        write_durably(path, raw)
    return sha


def read_checked(path, sha):
    """The bytes in path, which must hash to sha. Raises ValueError when they do not, OSError when unreadable."""
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError("%s does not hash to its name: the stored message is damaged" % path)
    return raw


def load(sha):
    """The stored message of sha256 sha. Raises ValueError for a bad sha or a damaged file, OSError (FileNotFoundError
    among them) when it cannot be read."""
    return read_checked(path_in(config.MESSAGES_DIR, sha), sha)
