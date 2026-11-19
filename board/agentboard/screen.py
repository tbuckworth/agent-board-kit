"""The owner's screen time on the Mac, for the Time page: the app and window active, per 5-minute block.

The Mac app reads ActivityWatch and uploads the blocks (the "screen" action, ScreenTime.swift). The desktop keeps
the blocks of the Time page's span in screen.json, private (0600); timesources.screen_time reads them.
"""
import json

from . import sources

PATH = sources.BOARD_STATE / "screen.json"
APP_CHARS, TITLE_CHARS = 80, 200      # what is kept of each name; the guess prompt and the key use less


def clean(blocks, block):
    """(The blocks as stored, None) or (None, error). A block is [start, app, title], the start in seconds since
    the epoch and a multiple of `block`."""
    if not isinstance(blocks, list):
        return None, "blocks must be a list"
    out = []
    for b in blocks:
        if not (isinstance(b, list) and len(b) == 3 and type(b[0]) is int and b[0] % block == 0
                and isinstance(b[1], str) and isinstance(b[2], str)):
            return None, "each block must be [start, app, title], the start a whole multiple of %d seconds" % block
        out.append([b[0], b[1][:APP_CHARS], b[2][:TITLE_CHARS]])
    return out, None


def held(path):
    """The blocks stored at `path`, oldest first; none when there is no file. A file that is not JSON raises, so a
    damaged store is noticed rather than quietly replaced."""
    return json.loads(path.read_text()) if path.exists() else []


def store(blocks, since, path):
    """Merge `blocks` into the file at `path`, where a block sent again replaces its earlier copy, and drop every
    block that starts before `since`. Returns the blocks held, oldest first. Uploads run one at a time, under a
    lock beside the file."""
    with sources.file_lock(path.with_suffix(".lock")):
        by_start = {b[0]: b for b in held(path)}
        by_start.update({b[0]: b for b in blocks})
        keep = [by_start[t] for t in sorted(by_start) if t >= since]
        sources.write_json(path, keep, mode=0o600)
    return keep
