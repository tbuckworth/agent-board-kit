"""agent-mail backup: copy the message store and the mailroom database to each directory of config backup.targets.
In each target: messages/ gets every stored message it lacks, checked by its sha256 once copied; latest.sqlite is a
consistent copy of the database (SQLite's online backup), replaced whole; daily/mailroom-YYYY-MM-DD.sqlite.gz is the
first copy of each day, gzipped. Nothing in a target is ever deleted. systemd/agent-mail-backup.timer runs it
hourly."""
import gzip
import os
import sqlite3
import time
from pathlib import Path

from . import config, jsonfile, messages

META = "backup:%s"   # the store's meta key of a target's last whole copy (epoch seconds)


def copy_database(st, path):
    """A consistent copy of st's database at path, written whole or not at all: SQLite's online backup into a temp
    file beside it, then put in place durably (jsonfile.replace)."""
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():   # left by a run that stopped part-way
        tmp.unlink()
    dst = sqlite3.connect(str(tmp))
    try:
        st.con.backup(dst)
    finally:
        dst.close()
    os.chmod(tmp, messages.FILE_MODE)
    jsonfile.replace(tmp, path, True)


def copy_to(st, target, now):
    """Bring target up to date (the module docstring says what it holds), then record in the store (meta) that target
    was copied at now. Makes target (mode 0700) when missing, but never a folder above it. Returns (how many messages
    it copied, the daily copy it made or None). Raises OSError or ValueError (a damaged message, or a copy that does
    not match its sha256)."""
    root = Path(os.path.expanduser(target))
    try:
        root.mkdir(mode=messages.DIR_MODE, exist_ok=True)
    except FileNotFoundError:
        raise OSError("%s is missing, so %s cannot be made there: is its disk mounted?" % (root.parent, root.name))
    copied = 0
    for src in sorted(config.MESSAGES_DIR.glob("*/*.eml")):
        dst = messages.path_in(root / "messages", src.stem)
        if dst.exists():
            continue
        messages.write_durably(dst, messages.read_checked(src, src.stem))
        messages.read_checked(dst, src.stem)
        copied += 1
    latest = root / "latest.sqlite"
    copy_database(st, latest)
    daily = root / "daily" / ("mailroom-%s.sqlite.gz" % time.strftime("%Y-%m-%d", time.localtime(now)))
    made = None
    if not daily.exists():
        messages.write_durably(daily, gzip.compress(latest.read_bytes()))
        made = daily.name
    st.set_meta(META % target, now)
    return copied, made


def run(st, targets, now):
    """Copy to each of targets (config backup.targets) with copy_to, which records each target copied in the store,
    going on past a target that fails. Returns (whether every target was copied, one line saying what was done in
    each)."""
    ok, parts = True, []
    for target in targets:
        try:
            copied, made = copy_to(st, target, now)
        except (OSError, ValueError, sqlite3.Error) as e:
            ok = False
            parts.append("%s: FAILED: %s: %s" % (target, type(e).__name__, e))
            continue
        parts.append("%s: %d new message%s, latest.sqlite%s" % (target, copied, "" if copied == 1 else "s",
                                                              ", " + made if made else ""))
    return ok, "backup " + ("ok" if ok else "FAILED") + ": " + "; ".join(parts)
