"""Files the owner attaches to a reply they write in the Mail tab (`agent-mail mailbox answer --json`; README.md, "The
Mail tab's mailbox"), and files given with a message (agent-send's and agent-orchestrate's --input-json; attach). The
reply keeps them in its message in the message store (the inbound row's eml), which the backup copies; this module
names them (safe_name, unique), reads them back from a stored reply (of_row), and writes the copies an agent opens:
one folder per reply, named by its key "i<inbound id>", under FILES_DIR of the machine the agent's session runs on
(write; deliver.place_files writes them here or asks another machine to over SSH), or per message,
"m<seconds>-<random>", under this machine's (attach), with the lines that tell the agent where they are (lines)."""
import mimetypes
import os
import re
import secrets
import time
from pathlib import Path

from . import config, mailview, messages

FILES_DIR = config.STATE_DIR / "files"
MAX_NAME_CHARS = 120          # a file name's characters, its extension included
MAX_EXT_CHARS = 16            # a longer "extension" is cut with the rest of the name
DEFAULT_NAME = "file"         # a name with nothing safe left in it
KEY_RE = re.compile(r"i[0-9]+|m[0-9]+-[0-9a-f]{6}")    # a reply's folder, i<inbound id>; a message's (attach)
_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")


def safe_name(name):
    """name as a file name that is safe to write in a folder of its own: its last path component (after the last / or
    \\), without control characters, leading dots or spaces, or trailing spaces, cut to MAX_NAME_CHARS with its
    extension kept; DEFAULT_NAME when nothing is left."""
    base = re.split(r"[\\/]", _UNSAFE.sub("", name))[-1].lstrip(". ").rstrip()
    if not base:
        return DEFAULT_NAME
    stem, ext = os.path.splitext(base)
    if len(ext) > MAX_EXT_CHARS:
        stem, ext = base, ""
    return (stem[:MAX_NAME_CHARS - len(ext)] + ext) if len(base) > MAX_NAME_CHARS else base


def unique(names):
    """names made safe (safe_name), the second of two that match, ignoring case (the Mac's file system does), made
    "NAME (2).EXT", the third "NAME (3).EXT", and so on."""
    out, taken = [], set()
    for n in names:
        name = safe_name(n)
        stem, ext = os.path.splitext(name)
        k = 2
        while name.lower() in taken:
            name = safe_name("%s (%d)%s" % (stem[:MAX_NAME_CHARS - len(ext) - 8], k, ext))
            k += 1
        taken.add(name.lower())
        out.append(name)
    return out


def of_row(row):
    """The files of inbound row row: the parts (mailview.Part) of the stored message of a Mail-tab reply with files,
    [] for a row without one (eml). Raises ValueError for a damaged message, OSError when it cannot be read."""
    return mailview.parse(messages.load(row["eml"])).parts if row["eml"] else []


def write(root, key, files):
    """Write files, [(name, bytes)] with the names unique gives, to root/KEY/NAME (messages.write_durably: mode 600,
    folders 700), and return their absolute paths in order. key is the reply's, i<inbound id>, or a message's,
    m<seconds>-<random> (attach); writing the same files again rewrites them. Raises ValueError for another key, a
    name unique would change or two names alike, and OSError when a file cannot be written."""
    if not KEY_RE.fullmatch(key):
        raise ValueError("%r is not a reply's key, i<inbound id>, or a message's, m<seconds>-<random>" % (key,))
    names = [name for name, _data in files]
    if unique(names) != names:
        raise ValueError("file names must be safe and distinct: %r" % (names,))
    paths = []
    for name, data in files:
        path = Path(root).absolute() / key / name
        messages.write_durably(path, data)
        paths.append(str(path))
    return paths


def content_type(name):
    """The content type a file's name suggests (mimetypes), else application/octet-stream: the type a reply's file is
    stored under (outbound.attach), and a message's file is told under (attach)."""
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def with_lines(body, said):
    """body, then a blank line and said, the lines that tell an agent where its files are (lines); said alone when
    body is empty."""
    return "%s\n\n%s" % (body, said) if body else said


def lines(who, placed):
    """What tells an agent where the files `who` attached are, placed being [(path, content type, bytes)]."""
    n = len(placed)
    return "%s attached %d file%s, saved on this machine; open %s with your file or image tools:\n%s" % (
        who, n, "" if n == 1 else "s", "it" if n == 1 else "them",
        "\n".join("- %s (%s, %s)" % (path, ctype, mailview.size(size)) for path, ctype, size in placed))


def attach(root, files, who):
    """Write the files of a message, [(name, bytes)] with the names unique gives (mailbox.answer_input), to
    root/m<seconds>-<random>/ (write), and return the lines that tell the agent who reads the message where they are,
    `who` having attached them (lines), each with its content type (content_type). Raises as write does."""
    paths = write(root, "m%d-%s" % (time.time(), secrets.token_hex(3)), files)
    return lines(who, [(path, content_type(name), len(data)) for path, (name, data) in zip(paths, files)])
