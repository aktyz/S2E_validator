"""The judge client's real transport, against servers on this machine.

It carries the security-relevant behaviour: the request goes to base_url only
(no redirects, no proxy from the environment), and an error never repeats what
the server sent back.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from s2e_validator.judge.chat_client import ChatClientError, _post

HEADERS = {"Content-Type": "application/json", "Authorization": "Bearer judge-key"}
BODY = {"model": "m", "messages": [{"role": "user", "content": "the photo and the description"}]}


class LocalServer:
    """One scripted answer for every request, and a record of what arrived."""

    def __init__(self, status=200, body=b"{}", headers=(), hang_up=False):
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                server.requests.append({"path": self.path, "headers": dict(self.headers),
                                        "body": self.rfile.read(length)})
                if hang_up:
                    self.close_connection = True
                    return
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                payload = body(server.requests[-1]) if callable(body) else body
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _answer

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def servers():
    started: list[LocalServer] = []

    def start(**kwargs) -> LocalServer:
        started.append(LocalServer(**kwargs))
        return started[-1]

    yield start
    for server in started:
        server.close()


def test_a_good_answer_is_decoded(servers):
    server = servers(body=json.dumps({"choices": []}).encode())

    assert _post(server.url + "/v1/chat/completions", HEADERS, BODY, 5) == {"choices": []}
    assert json.loads(server.requests[0]["body"]) == BODY


def test_an_error_reports_the_status_only(servers):
    server = servers(status=500, body=lambda request: b"echo: " + request["body"])

    with pytest.raises(ChatClientError) as raised:
        _post(server.url + "/v1/chat/completions", HEADERS, BODY, 5)

    assert str(raised.value) == "model server answered HTTP 500"
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_a_redirect_is_not_followed(servers):
    elsewhere = servers()
    server = servers(status=302, headers=[("Location", elsewhere.url + "/steal")])

    with pytest.raises(ChatClientError, match="HTTP 302"):
        _post(server.url + "/v1/chat/completions", HEADERS, BODY, 5)

    assert elsewhere.requests == []


def test_a_proxy_from_the_environment_is_ignored(servers, monkeypatch):
    proxy = servers()
    server = servers(body=b"{}")
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, proxy.url)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)

    _post(server.url + "/v1/chat/completions", HEADERS, BODY, 5)

    assert proxy.requests == []
    assert len(server.requests) == 1


def test_a_reply_that_is_not_json_says_so(servers):
    server = servers(body=b"<html>502 Bad Gateway</html>")

    with pytest.raises(ChatClientError, match="not JSON"):
        _post(server.url + "/v1/chat/completions", HEADERS, BODY, 5)


def test_a_server_that_hangs_up_is_unreachable(servers):
    server = servers(hang_up=True)

    with pytest.raises(ChatClientError, match="unreachable"):
        _post(server.url + "/v1/chat/completions", HEADERS, BODY, 5)


def test_nothing_listening_is_unreachable(servers):
    server = servers()
    url = server.url
    server.close()

    with pytest.raises(ChatClientError, match="unreachable"):
        _post(url + "/v1/chat/completions", HEADERS, BODY, 2)
