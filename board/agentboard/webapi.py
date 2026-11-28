"""One JSON request to a web API, standard library only: what the Google client (google.py) sends its requests
through. A failure carries the HTTP status and never the request, which may hold a token or a password.
"""
import json
import urllib.error
import urllib.parse
import urllib.request


class ApiError(RuntimeError):
    """A request that failed. `status` is the HTTP status, None when the API could not be reached or did not answer
    in JSON."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def request(method, url, name, error, form=None, body=None, headers=None, timeout=20, raw=None):
    """One request to the API that errors call `name`: `form` goes URL-encoded, `body` as JSON, `raw` (bytes) as it
    is, under the Content-Type `headers` give it. `timeout`: the seconds each step may take, sending the whole request
    being one step. The answer's JSON, {} for an empty answer. Raises error(message, status), an ApiError, whose
    message names the status only."""
    headers = dict(headers or {})
    data = None
    if raw is not None:
        data = raw
    elif body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    elif form:
        data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode()
            return json.loads(text) if text.strip() else {}
    except urllib.error.HTTPError as e:
        raise error("%s answered HTTP %d" % (name, e.code), e.code) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise error("%s unreachable: %s" % (name, type(e).__name__)) from None
