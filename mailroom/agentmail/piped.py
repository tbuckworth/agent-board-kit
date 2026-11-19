"""What was piped or redirected into a command, without waiting on an input that never ends. Stdlib only.

Claude Code's Bash tool usually runs a command with `< /dev/null`, but not when the command holds a heredoc:
then the command's input is the tool's own socket, which never reaches end-of-file, so `cat > mail.html <<EOF
... EOF; agent-mail send --html mail.html` would wait for ever on a body that never comes. So input is read only
from a pipe or a file, and a pipe that stays silent gives up.
"""
import os
import select
import stat
import sys

IDLE_SECONDS = 60


def piped_text(stream=None, idle=IDLE_SECONDS):
    """The text piped or redirected in, or None when nothing was: the input is a terminal, a device such as
    /dev/null, or a socket. Raises TimeoutError when a pipe sends nothing for `idle` seconds."""
    stream = sys.stdin if stream is None else stream
    if stream is None:
        return None
    fd = stream.fileno()
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return None
    if stat.S_ISREG(mode):
        return stream.read()
    if not stat.S_ISFIFO(mode):
        return None
    chunks = []
    while True:
        ready, _, _ = select.select([fd], [], [], idle)
        if not ready:
            raise TimeoutError("nothing came through the pipe for %d s" % idle)
        data = os.read(fd, 65536)
        if not data:
            return b"".join(chunks).decode("utf-8", "replace")
        chunks.append(data)
