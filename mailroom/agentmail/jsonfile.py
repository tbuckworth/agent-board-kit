"""JSON files the tools keep (state, caches, trust lists, a login's .claude.json): reading one, and writing one so
that a reader never sees half of it: a temp file in the same folder, then os.replace. write_text does the same for
any text file (Codex's config.toml), and write_bytes for any bytes, durably on request (the message store). Also
JSON-lines logs: adding a line to one, every line of one, and the tail of one someone else writes (a Claude
transcript, a Codex rollout)."""
import json
import os
import sys
from pathlib import Path

PRIVATE = 0o600   # owner only
PLAIN = 0o666     # what open() creates a file with; the umask takes away the rest (usually leaving 0o644)


def read(path):
    """The JSON in path, or {} if the file is missing. A file that is there but is not JSON raises ValueError
    naming it, and one that cannot be read raises OSError: a damaged file is never read as empty, which would
    drop what it holds. The operator looks at it and fixes it. A cache, or a daemon's state file, may be deleted
    instead (it is rebuilt, losing only what it remembered); a login's .claude.json must be fixed, not deleted."""
    path = Path(path)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        return json.loads(text)
    except ValueError as e:
        raise ValueError("%s is not JSON: %s" % (path, e)) from e


def write(path, data, mode, indent):
    """Write data to path as JSON, atomically (write_text)."""
    write_text(path, json.dumps(data, indent=indent), mode)


def write_text(path, text, mode):
    """Write text to path atomically, UTF-8 (write_bytes, not durable)."""
    write_bytes(path, text.encode("utf-8"), mode, False)


def write_bytes(path, data, mode, durable):
    """Write data to path atomically: into a temp file in the same folder, created with mode (before the umask), then
    put in place (replace). Creates the folder if it is missing. durable: on disk before it returns (replace). A
    write that fails leaves no temp file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        replace(tmp, path, durable)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def replace(tmp, path, durable):
    """Rename the finished file tmp over path. durable: tmp is synced to disk before, and path's folder after, so that
    a crash leaves the old file or the whole new one, never a short one."""
    if durable:
        sync(tmp)
    os.replace(tmp, path)
    if durable:
        sync(Path(path).parent)


def sync(path):
    """Flush the file or folder at path to disk."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_line(path, data, mode):
    """Append data to the JSON-lines log at path as one line, creating the file with mode (before the umask)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(data, ensure_ascii=False) + "\n")


def read_lines(path):
    """The JSON object on each line of a JSON-lines log, oldest first; [] if the file is missing. A line that is not
    a JSON object (one whose writer was killed mid-line, or damage) is skipped, and how many were is said on stderr
    with the path: from a daemon that reaches the journal, from a command the terminal, apart from what it prints
    on stdout. Raises OSError if the file cannot be read."""
    try:
        fh = open(path, errors="replace")
    except FileNotFoundError:
        return []
    rows, bad = [], 0
    with fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                row = None
            if isinstance(row, dict):
                rows.append(row)
            else:
                bad += 1
    if bad:
        print("%s: skipped %d line(s) that are not JSON objects" % (path, bad), file=sys.stderr)
    return rows


def read_tail(path, nbytes):
    """The lines in the last nbytes of a file (the first may be cut short), decoded leniently."""
    with open(path, "rb") as fh:
        fh.seek(max(0, os.path.getsize(path) - nbytes))
        return fh.read().decode("utf-8", "replace").splitlines()


class JsonFile:
    """One JSON file whose location and format several places share, such as a daemon's state."""

    def __init__(self, path, mode, indent):
        self.path, self.mode, self.indent = Path(path), mode, indent

    def read(self):
        return read(self.path)

    def write(self, data):
        write(self.path, data, self.mode, self.indent)
