"""One JSON request to a web API (webapi.request), against a real HTTP server on this machine: what it sends, what
it gives back, and what an error says."""
import http.server
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _kitenv  # noqa: E402,F401  (scratch config with an example owner, before agentboard reads any)
from agentboard import google, webapi  # noqa: E402


class Echo(http.server.BaseHTTPRequestHandler):
    """Answers /echo with the request it got, /empty with no body, /text with text that is not JSON, and anything
    else with HTTP 403."""

    def answer(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        if self.path.startswith("/echo"):
            out, code = json.dumps({"method": self.command, "path": self.path, "body": body,
                                    "type": self.headers.get("Content-Type"), "auth": self.headers.get("Authorization")}), 200
        else:
            out, code = {"/empty": ("", 204), "/text": ("<html>", 200)}.get(self.path, ("secret detail", 403))
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out.encode())

    do_GET = do_POST = do_PATCH = do_DELETE = answer

    def log_message(self, *args):
        pass


class Request(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Echo)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def ask(self, method, path, **kw):
        return webapi.request(method, self.base + path, "Example API", webapi.ApiError, **kw)

    def test_a_form_goes_url_encoded_a_body_as_json_and_raw_bytes_as_they_are(self):
        got = self.ask("POST", "/echo", form={"a": "1 2", "b": "é"})
        self.assertEqual((got["method"], got["body"], got["type"]), ("POST", "a=1+2&b=%C3%A9", "application/x-www-form-urlencoded"))
        got = self.ask("PATCH", "/echo?x=1", body={"location": "Room 2"}, headers={"Authorization": "Bearer T"})
        self.assertEqual(got, {"method": "PATCH", "path": "/echo?x=1", "body": '{"location": "Room 2"}',
                               "type": "application/json", "auth": "Bearer T"})
        self.assertEqual(self.ask("GET", "/echo")["body"], "")
        got = self.ask("POST", "/echo", raw=b"Subject: E6\r\n\r\ndone", headers={"Content-Type": "message/rfc822"})
        self.assertEqual((got["body"], got["type"]), ("Subject: E6\r\n\r\ndone", "message/rfc822"))

    def test_an_empty_answer_is_an_empty_object(self):
        self.assertEqual(self.ask("DELETE", "/empty"), {})

    def test_an_error_names_the_api_and_the_status_never_the_request(self):
        with self.assertRaises(webapi.ApiError) as e:
            self.ask("GET", "/refused?token=SECRET", headers={"Authorization": "Bearer SECRET"})
        self.assertEqual((str(e.exception), e.exception.status), ("Example API answered HTTP 403", 403))
        with self.assertRaises(webapi.ApiError) as e:
            self.ask("GET", "/text")
        self.assertEqual((str(e.exception), e.exception.status), ("Example API unreachable: JSONDecodeError", None))
        with self.assertRaises(webapi.ApiError) as e:
            webapi.request("GET", "http://127.0.0.1:1/token=SECRET", "Example API", webapi.ApiError, timeout=2)
        self.assertEqual(e.exception.status, None)
        self.assertNotIn("SECRET", str(e.exception))

    def test_the_google_client_raises_its_own_error_through_it(self):
        with self.assertRaises(google.GoogleError) as e:
            google.http("GET", self.base + "/refused")
        self.assertEqual((str(e.exception), e.exception.status), ("Google API answered HTTP 403", 403))
        self.assertEqual(google.http("POST", self.base + "/echo", body={"a": 1})["body"], '{"a": 1}')
        self.assertEqual(google.http("POST", self.base + "/echo", raw=b"x", headers={"Content-Type": "text/plain"})["body"],
                         "x")


if __name__ == "__main__":
    unittest.main()
