"""Files the owner attaches to a reply they write in the Mail tab (`agent-mail mailbox answer --json`; README.md, "The Mail
tab's mailbox"). The reply keeps them in its message in the message store (the inbound row's eml), which the backup
copies; this module names them (safe_name, unique), reads them back from a stored reply (of_row), and writes the
copies an agent opens: one folder per reply, named by its key "i<inbound id>", under FILES_DIR of the machine the
agent's session runs on (write; deliver.place_files writes them here or asks another machine to over SSH), with the
lines that tell the agent where they are (lines)."""
import os
import re
from pathlib import Path

from . import config, mailview, messages

FILES_DIR = config.STATE_DIR / "files"
MAX_NAME_CHARS = 120          # a file name's characters, its extension included
MAX_EXT_CHARS = 16            # a longer "extension" is cut with the rest of the name
DEFAULT_NAME = "file"         # a name with nothing safe left in it
KEY_RE = re.compile(r"i[0-9]+")
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
    folders 700), and return their absolute paths in order. key is the reply's, i<inbound id>; writing the same files
    again rewrites them. Raises ValueError for another key, a name unique would change or two names alike, and
    OSError when a file cannot be written."""
    if not KEY_RE.fullmatch(key):
        raise ValueError("%r is not a reply's key, i<inbound id>" % (key,))
    names = [name for name, _data in files]
    if unique(names) != names:
        raise ValueError("file names must be safe and distinct: %r" % (names,))
    paths = []
    for name, data in files:
        path = Path(root).absolute() / key / name
        messages.write_durably(path, data)
        paths.append(str(path))
    return paths


def lines(owner_name, placed):
    """What tells an agent where the files of owner_name's reply are, placed being [(path, content type, bytes)]."""
    n = len(placed)
    return "%s attached %d file%s, saved on this machine; open %s with your file or image tools:\n%s" % (
        owner_name, n, "" if n == 1 else "s", "it" if n == 1 else "them",
        "\n".join("- %s (%s, %s)" % (path, ctype, mailview.size(size)) for path, ctype, size in placed))
