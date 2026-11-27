"""Headless Chrome driven over the DevTools protocol, for the phone page's tests (test_mailweb.py): standard library
only. Chrome is started with --remote-debugging-pipe, which reads the protocol's commands from its fd 3 and writes its
answers and events to its fd 4, each message JSON ended by a NUL byte; no port is opened.

    with Chrome() as c:
        page = c.page(width=412, height=915)       # a tab, emulating a phone of that size
        page.go("http://127.0.0.1:8000/")
        page.wait("document.querySelector('#threads li')")
"""
import fcntl
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time

# Chrome, or Chromium under one of its usual names; the browser tests skip without one
EXE = next(filter(None, (shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium",
                                                     "chromium-browser"))), None)


def _above(fd, low=10):
    """`fd` moved to a descriptor numbered `low` or more (inheritable, for the child)."""
    moved = fcntl.fcntl(fd, fcntl.F_DUPFD, low)
    os.close(fd)
    return moved


class ChromeError(Exception):
    """A protocol command Chrome answered with an error, or a wait that timed out."""


class Chrome:
    """A headless Chrome of its own, with a fresh profile that goes when it is closed. The profile is made in the
    temporary folder TMPDIR names (scripts/run-tests.sh points it at its own scratch folder), never in the repo. `args`
    are more of Chrome's switches."""

    def __init__(self, args=()):
        self.profile = tempfile.mkdtemp(prefix="chrome-profile-")
        to_chrome_r, self._w = os.pipe()
        self._r, from_chrome_w = os.pipe()
        # above fd 9, so the shell's redirections to 3 and 4 cannot overwrite one of them before it is read
        to_chrome_r, from_chrome_w = (_above(to_chrome_r), _above(from_chrome_w))
        self.proc = subprocess.Popen(
            ["bash", "-c", 'exec "$0" "$@" 3<&%d 4>&%d' % (to_chrome_r, from_chrome_w), EXE, "--headless=new",
             "--remote-debugging-pipe", "--no-sandbox", "--disable-gpu", "--disable-extensions", "--disable-sync",
             "--no-first-run", "--no-default-browser-check", "--password-store=basic", "--disable-dev-shm-usage",
             "--user-data-dir=" + self.profile, *args, "about:blank"],
            pass_fds=(to_chrome_r, from_chrome_w), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self._lock = threading.Lock()
        self._next = 0
        self._waiting = {}                 # id -> queue for its answer
        self.events = {}                   # session id ("" for the browser) -> queue of (method, params)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = b""
        with os.fdopen(self._r, "rb", buffering=0) as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\0" in buf:
                    raw, buf = buf.split(b"\0", 1)
                    msg = json.loads(raw)
                    if "id" in msg:
                        q = self._waiting.pop(msg["id"], None)
                        if q:
                            q.put(msg)
                    else:
                        self.events.setdefault(msg.get("sessionId", ""), queue.Queue()).put(
                            (msg["method"], msg.get("params", {})))

    def send(self, method, params=None, session=None, timeout=30):
        """The result of protocol command `method`, in `session` (a tab's) or the browser's. Raises ChromeError."""
        with self._lock:
            self._next += 1
            n = self._next
            q = self._waiting[n] = queue.Queue()
            msg = {"id": n, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            os.write(self._w, json.dumps(msg).encode() + b"\0")
        try:
            got = q.get(timeout=timeout)
        except queue.Empty:
            raise ChromeError("%s: no answer in %s seconds" % (method, timeout)) from None
        if "error" in got:
            raise ChromeError("%s: %s" % (method, got["error"].get("message")))
        return got.get("result", {})

    def page(self, width=412, height=915, dark=False):
        """A new tab emulating a phone with touch, `width` by `height` CSS pixels at twice that in pixels, its system
        in dark mode or light."""
        target = self.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = self.send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        return Page(self, session, width, height, dark)

    def close(self):
        """Stop Chrome (all its processes: it runs in a session of its own) and remove its profile."""
        os.killpg(self.proc.pid, 15)
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, 9)
            self.proc.wait(10)
        os.close(self._w)
        shutil.rmtree(self.profile, True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Page:
    """One tab of a Chrome."""

    def __init__(self, chrome, session, width, height, dark):
        self.chrome, self.session = chrome, session
        self.send("Page.enable")                   # its events say when a page has loaded
        self.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 2,
                                                         "mobile": True})
        self.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        self.send("Emulation.setEmulatedMedia", {"features": [
            {"name": "prefers-color-scheme", "value": "dark" if dark else "light"}]})

    def send(self, method, params=None, timeout=30):
        return self.chrome.send(method, params, self.session, timeout)

    def _events(self):
        return self.chrome.events.setdefault(self.session, queue.Queue())

    def _drain(self):
        """Drop the events the tab has sent so far, so a wait sees only those after it."""
        q = self._events()
        while not q.empty():
            q.get_nowait()

    def go(self, url, timeout=30):
        """Navigate to `url` and wait for its load event."""
        self._drain()
        self.send("Page.navigate", {"url": url})
        self._until("Page.loadEventFired", timeout)

    def reload(self, timeout=30):
        self._drain()
        self.send("Page.reload")
        self._until("Page.loadEventFired", timeout)

    def _until(self, method, timeout):
        """Wait for the tab's event `method`. Raises ChromeError when it has not come within `timeout` seconds."""
        q, ends = self._events(), time.time() + timeout
        while True:
            try:
                got, _ = q.get(timeout=max(0.01, ends - time.time()))
            except queue.Empty:
                raise ChromeError("no %s in %s seconds" % (method, timeout)) from None
            if got == method:
                return

    def js(self, expression, timeout=30):
        """The value of JavaScript `expression` in the page (a promise is awaited). Raises ChromeError when it
        throws."""
        got = self.send("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True,
                                             "userGesture": True}, timeout)
        if "exceptionDetails" in got:
            d = got["exceptionDetails"]
            raise ChromeError("%s: %s" % (expression[:80], (d.get("exception") or {}).get("description") or d["text"]))
        return got["result"].get("value")

    def wait(self, condition, timeout=15):
        """The first truthy value of JavaScript `condition`, polled every 25 ms. Raises ChromeError on timing out."""
        return self.js("""new Promise((resolve, reject) => {
          const ends = Date.now() + %d;
          const look = () => { let v; try { v = (%s); } catch (e) { v = null; }
            if (v) resolve(v); else if (Date.now() > ends) reject(new Error("timed out waiting")); else setTimeout(look, 25); };
          look(); })""" % (timeout * 1000, condition), timeout + 5)

    def tap(self, selector):
        """A finger's tap on the middle of the first element `selector` names, as touch events (Input domain), once it
        is scrolled into view (only as far as it must be)."""
        box = self.js("(() => { const e = document.querySelector(%s); e.scrollIntoView({block: 'nearest'}); "
                      "const r = e.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()"
                      % json.dumps(selector))
        point = [{"x": box[0], "y": box[1]}]
        self.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": point})
        self.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def drag(self, start, end, steps=10):
        """A finger dragged from `start` to `end` ((x, y) in CSS pixels), in `steps` moves a frame (16 ms) apart."""
        self.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": start[0], "y": start[1]}]})
        for i in range(1, steps + 1):
            x = start[0] + (end[0] - start[0]) * i / steps
            y = start[1] + (end[1] - start[1]) * i / steps
            self.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]})
            time.sleep(0.016)
        self.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
