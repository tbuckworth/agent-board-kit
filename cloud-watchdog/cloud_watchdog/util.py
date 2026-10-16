"""Small helpers: time, durations, money, subprocess."""
import datetime as dt
import re
import subprocess
import time

UTC = dt.timezone.utc


def now():
    return time.time()


def parse_duration(text):
    """'90m', '24h', '7d', '1d12h', '3600' (seconds) -> seconds."""
    text = str(text).strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    total, pos = 0.0, 0
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([smhdw])", text):
        if m.start() != pos:
            raise ValueError("bad duration: %r" % text)
        total += float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        pos = m.end()
    if pos != len(text) or not total:
        raise ValueError("bad duration: %r (use e.g. 30m, 24h, 7d)" % text)
    return total


def parse_time(value):
    """ISO-8601 string (with or without zone; naive = UTC) or epoch -> epoch seconds, or None."""
    if value in (None, "", "Pending"):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.timestamp()


def iso(epoch, local=False):
    if epoch is None:
        return "-"
    t = dt.datetime.fromtimestamp(epoch, UTC)
    if local:
        t = t.astimezone()
        return t.strftime("%Y-%m-%d %H:%M %Z")
    return t.strftime("%Y-%m-%d %H:%M UTC")


def age(seconds):
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return "%dd %dh" % (d, h)
    if h:
        return "%dh %dm" % (h, m)
    return "%dm" % m


def money(x, est=False):
    if x is None:
        return "?"
    s = "$%.2f" % x if x < 100 else "$%s" % format(round(x), ",")
    return ("≈" + s) if est else s


def run(cmd, env=None, timeout=120, cwd=None, input_text=None):
    """Run a command; return (code, stdout, stderr). Never raises on failure or timeout."""
    try:
        p = subprocess.run(cmd, env=env, cwd=cwd, input=input_text, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ss" % timeout
    except OSError as e:
        return 127, "", str(e)


def json_tail(text, opener="["):
    """The CLI tools print notices before their JSON; return the parsed JSON from the first opener."""
    import json
    i = text.find(opener)
    if i < 0:
        raise ValueError("no JSON in output: %r" % text[:200])
    return json.loads(text[i:])


def modal_user(res):
    """Who is behind a Modal app: its latest deployer, else (a `modal run` app) its creator."""
    keys = res.keys()   # a collector dict or a sqlite Row
    return (res["deployed_by"] if "deployed_by" in keys else None) or (res["created_by"] if "created_by" in keys else None)
