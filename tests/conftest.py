"""Shared offline test doubles: a fake transport and a controllable clock.

Nothing in the test suite touches the network. The fake transport returns
queued :class:`Response` objects (or raises a queued error), and the fake clock
records sleeps so backoff and min-interval spacing can be asserted without real
time passing.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from reddit_mcp.transport import Response, TransportError


class FakeTransport:
    """Returns queued responses in order; records every request it received."""

    def __init__(self) -> None:
        self._queue: deque[Response | Exception] = deque()
        self.requests: list[dict[str, Any]] = []

    def queue_response(
        self,
        status: int,
        body: str,
        url: str = "https://www.reddit.com/x.json",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._queue.append(Response(status=status, body=body, url=url, headers=headers or {}))

    def queue_error(self, message: str = "boom") -> None:
        self._queue.append(TransportError(message))

    def get(self, url: str, headers: dict[str, str], timeout: float) -> Response:
        self.requests.append({"url": url, "headers": headers, "timeout": timeout})
        if not self._queue:
            raise AssertionError(f"unexpected request with empty queue: {url}")
        item = self._queue.popleft()
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    """A monotonic clock that only advances when sleep() is called."""

    def __init__(self) -> None:
        self._t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._t += seconds
