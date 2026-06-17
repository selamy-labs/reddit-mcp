"""HTTP transport abstraction so the core stays fully offline-testable.

The Reddit client never imports an HTTP library directly. It depends only on the
:class:`Transport` protocol below, which yields a :class:`Response`. Production
code injects :class:`UrllibTransport` (stdlib only, zero extra dependencies);
tests inject a fake transport that returns canned Reddit JSON. This keeps unit
tests hermetic -- no network, no live Reddit terms exposure -- while the real
adapter exercises the same code path.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Response:
    """A minimal HTTP response: status code, body, final URL, and headers.

    ``url`` is the final URL the transport fetched, recorded so the client can
    attach accurate source lineage to results even after redirects. ``headers``
    lets the client honour ``Retry-After`` on rate-limit responses. Header keys
    are matched case-insensitively via :meth:`header`.
    """

    status: int
    body: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        """Parse the body as JSON, raising ``ValueError`` on malformed input."""
        return json.loads(self.body)

    def header(self, name: str) -> str | None:
        """Return a header value by case-insensitive name, or ``None``."""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


class TransportError(Exception):
    """A transport failed to produce a usable response (network, DNS, etc.)."""


class Transport(Protocol):
    """Fetches a URL and returns a :class:`Response`.

    Implementations must not raise for HTTP error status codes -- they return a
    :class:`Response` with the status set so the client can apply its own
    backoff and terms-aware handling. They may raise :class:`TransportError`
    only for transport-level failures (connection refused, DNS, timeout).
    """

    def get(self, url: str, headers: dict[str, str], timeout: float) -> Response: ...


class UrllibTransport:
    """Production transport built on the standard library ``urllib``.

    Adds no third-party dependencies. HTTP error responses (4xx/5xx) are mapped
    to a :class:`Response` rather than an exception so the client controls
    backoff. Only genuine transport failures become :class:`TransportError`.
    """

    def get(self, url: str, headers: dict[str, str], timeout: float) -> Response:
        # Scheme is fixed by the caller (https Reddit base), so the urlopen audit
        # (file:/custom schemes) does not apply here.
        request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
                body = resp.read().decode("utf-8", errors="replace")
                return Response(status=resp.status, body=body, url=resp.geturl(), headers=dict(resp.headers))
        except urllib.error.HTTPError as error:  # 4xx/5xx -> let the client decide
            body = error.read().decode("utf-8", errors="replace") if error.fp else ""
            return Response(status=error.code, body=body, url=url, headers=dict(error.headers or {}))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TransportError(f"transport failure fetching {url}: {error}") from error


class Clock(Protocol):
    """A monotonic clock plus sleep, injected so backoff is testable offline."""

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock: wall-time monotonic counter and a real sleep."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
