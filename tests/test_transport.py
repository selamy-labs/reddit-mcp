"""Tests for the real stdlib transport, exercised against a loopback server.

These never touch the public internet -- a throwaway ``http.server`` on
127.0.0.1 stands in for Reddit so the production code path (UrllibTransport)
gets real coverage offline.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from reddit_mcp.transport import Response, SystemClock, TransportError, UrllibTransport


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/ok"):
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *_: object) -> None:  # silence test output
        pass


@pytest.fixture()
def server() -> Iterator[str]:
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        thread.join()


def test_urllib_transport_success(server: str) -> None:
    resp = UrllibTransport().get(f"{server}/ok", headers={"User-Agent": "t"}, timeout=5.0)
    assert isinstance(resp, Response)
    assert resp.status == 200
    assert resp.json() == {"ok": True}


def test_urllib_transport_http_error_becomes_response(server: str) -> None:
    resp = UrllibTransport().get(f"{server}/missing", headers={"User-Agent": "t"}, timeout=5.0)
    assert resp.status == 404
    assert resp.body == "not found"


def test_urllib_transport_connection_failure_raises() -> None:
    # Port 1 on loopback refuses connections -> transport-level failure.
    with pytest.raises(TransportError, match="transport failure"):
        UrllibTransport().get("http://127.0.0.1:1/x", headers={"User-Agent": "t"}, timeout=2.0)


def test_system_clock_monotonic_and_sleep() -> None:
    clock = SystemClock()
    start = clock.monotonic()
    clock.sleep(0)  # zero-duration sleep keeps the test fast
    assert clock.monotonic() >= start
