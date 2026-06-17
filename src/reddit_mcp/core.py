"""Terms-aware, read-only Reddit research client.

This module holds the Reddit logic exactly once. The MCP server in
:mod:`reddit_mcp.mcp_server` is a thin wrapper that serialises these structured
results to JSON; nothing here imports the MCP SDK.

Design notes
------------
* **Read-only, public JSON.** The client only reads Reddit's public ``.json``
  endpoints (e.g. ``/r/<sub>/top.json``). It never writes, votes, or posts.
  Reddit's API terms require a descriptive ``User-Agent`` and ask automated
  clients to respect rate limits; both are enforced here, not left to callers.
* **Polite by default.** A descriptive User-Agent, a minimum spacing between
  requests, and exponential backoff with jitter on ``429``/``401``/``403`` and
  repeated ``5xx`` keep low-volume discovery within the documented envelope.
  For higher volume, OAuth, or comment-tree reliability, callers should
  escalate to the official authenticated API (see README).
* **Lineage everywhere.** Every result carries the source URL, the UTC time it
  was retrieved, and the access path, so downstream research can cite and
  re-verify the data instead of trusting an opaque blob.
* **Credentials at call time.** No credential is embedded. The optional bearer
  token is resolved from the environment when the client is constructed, never
  baked into source or defaults.

The public Reddit JSON surface is not a stable contract; validate current
behavior from the real runtime before depending on a path (datacenter/CI IPs
are frequently ``403``-blocked).
"""

from __future__ import annotations

import os
import random
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Literal

from reddit_mcp import __version__
from reddit_mcp.transport import Clock, Response, SystemClock, Transport, TransportError, UrllibTransport

PUBLIC_BASE = "https://www.reddit.com"
OAUTH_BASE = "https://oauth.reddit.com"

Sort = Literal["relevance", "hot", "top", "new", "comments"]
TimeWindow = Literal["hour", "day", "week", "month", "year", "all"]

SORTS: frozenset[str] = frozenset({"relevance", "hot", "top", "new", "comments"})
TIME_WINDOWS: frozenset[str] = frozenset({"hour", "day", "week", "month", "year", "all"})

# Status codes that mean "slow down / not allowed right now" -> backoff + retry.
_BACKOFF_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
# Status codes that are terminal for the request and should surface as an error.
_HARD_DENY_STATUSES: frozenset[int] = frozenset({401, 403, 404})

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


class RedditError(Exception):
    """A Reddit request failed for an expected, user-facing reason.

    The MCP layer maps this to a ``ToolError`` so clients get a clean message
    instead of a stack trace.
    """


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clamp_limit(limit: int) -> int:
    if limit < 1:
        raise RedditError(f"limit must be >= 1, got {limit}")
    return min(limit, MAX_LIMIT)


def _require(value: str, name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise RedditError(f"{name} must not be empty")
    return cleaned


def _validate_choice(value: str, allowed: frozenset[str], name: str) -> str:
    if value not in allowed:
        raise RedditError(f"invalid {name} {value!r}; expected one of {sorted(allowed)}")
    return value


class RedditClient:
    """Low-volume, read-only client over Reddit's public JSON endpoints.

    All network access goes through the injected :class:`Transport`, and all
    timing through the injected :class:`Clock`, so the full request/backoff path
    is exercised offline in tests with canned fixtures.
    """

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        clock: Clock | None = None,
        user_agent: str | None = None,
        bearer_token: str | None = None,
        min_interval: float = 1.0,
        max_retries: int = 4,
        base_backoff: float = 1.0,
        timeout: float = 15.0,
    ) -> None:
        self._transport = transport or UrllibTransport()
        self._clock = clock or SystemClock()
        # A descriptive UA is required by Reddit's terms. Default is honest about
        # what this is; operators may override via REDDIT_USER_AGENT.
        self._user_agent = user_agent or os.environ.get(
            "REDDIT_USER_AGENT",
            f"reddit-mcp/{__version__} (read-only research; +https://github.com/selamy-labs/reddit-mcp)",
        )
        # Credentials are resolved at call time, never embedded. When a token is
        # present, requests go to the authenticated host with higher limits.
        self._bearer_token = bearer_token if bearer_token is not None else os.environ.get("REDDIT_BEARER_TOKEN")
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._timeout = timeout
        self._last_request_at: float | None = None

    @property
    def _base(self) -> str:
        return OAUTH_BASE if self._bearer_token else PUBLIC_BASE

    @property
    def _access_path(self) -> str:
        return "reddit-oauth-json" if self._bearer_token else "reddit-public-json"

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        return headers

    def _respect_min_interval(self) -> None:
        """Block until at least ``min_interval`` has passed since the last call."""
        if self._last_request_at is None:
            return
        elapsed = self._clock.monotonic() - self._last_request_at
        wait = self._min_interval - elapsed
        if wait > 0:
            self._clock.sleep(wait)

    def _backoff_seconds(self, attempt: int, response: Response | None) -> float:
        """Exponential backoff with jitter, honouring ``Retry-After`` if given."""
        if response is not None:
            retry_after = self._retry_after_seconds(response)
            if retry_after is not None:
                return retry_after
        # attempt is 0-based: 1, 2, 4, 8 ... times base, plus jitter. The jitter
        # is for thundering-herd avoidance, not cryptographic use.
        return self._base_backoff * (2**attempt) + random.uniform(0, self._base_backoff)  # noqa: S311

    @staticmethod
    def _retry_after_seconds(response: Response) -> float | None:
        """Parse a ``Retry-After`` header (delta-seconds form) if present.

        Reddit sends ``Retry-After`` on ``429`` responses; honouring it is more
        polite than blind exponential backoff. Only the numeric delta-seconds
        form is supported; an HTTP-date or malformed value falls back to
        exponential backoff.
        """
        raw = response.header("Retry-After")
        if raw is None:
            return None
        try:
            seconds = float(raw.strip())
        except ValueError:
            return None
        return seconds if seconds >= 0 else None

    def _get_json(self, path: str, params: dict[str, Any]) -> tuple[Any, str]:
        """Fetch ``path`` with ``params`` and return ``(parsed_json, final_url)``.

        Applies min-interval spacing before each attempt, retries with backoff on
        rate-limit/5xx statuses, and raises :class:`RedditError` on hard denials,
        transport failure, malformed JSON, or exhausted retries.
        """
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self._base}{path}"
        if query:
            url = f"{url}?{query}"

        last_error = ""
        for attempt in range(self._max_retries + 1):
            self._respect_min_interval()
            try:
                response = self._transport.get(url, headers=self._headers(), timeout=self._timeout)
            except TransportError as error:
                last_error = str(error)
                if attempt < self._max_retries:
                    self._clock.sleep(self._backoff_seconds(attempt, None))
                    continue
                raise RedditError(f"reddit request failed after {attempt + 1} attempts: {last_error}") from error
            finally:
                self._last_request_at = self._clock.monotonic()

            if response.status == 200:
                try:
                    return response.json(), response.url
                except ValueError as error:
                    raise RedditError(f"reddit returned non-JSON body for {url}: {error}") from error

            if response.status in _HARD_DENY_STATUSES:
                raise RedditError(
                    f"reddit denied {url} with status {response.status} "
                    f"(access path: {self._access_path}); this often means the IP is blocked, "
                    "the content is unavailable, or authentication is required"
                )

            if response.status in _BACKOFF_STATUSES and attempt < self._max_retries:
                self._clock.sleep(self._backoff_seconds(attempt, response))
                last_error = f"status {response.status}"
                continue

            raise RedditError(f"reddit returned unexpected status {response.status} for {url}")

        # Unreachable: the loop always returns or raises, but this guards against
        # a future change to the retry structure leaving the function fall-through.
        raise RedditError(f"reddit request exhausted retries for {url}: {last_error}")  # pragma: no cover

    def _lineage(self, url: str, **extra: Any) -> dict[str, Any]:
        """Build the source-lineage block attached to every result."""
        lineage = {
            "source": "reddit",
            "access_path": self._access_path,
            "url": url,
            "retrieved_at": _now_iso(),
        }
        lineage.update(extra)
        return lineage

    # -- Normalisers -----------------------------------------------------------

    @staticmethod
    def _post_from_listing_child(child: dict[str, Any]) -> dict[str, Any]:
        data = child.get("data", {})
        return {
            "id": data.get("id"),
            "fullname": data.get("name"),
            "title": data.get("title"),
            "author": data.get("author"),
            "subreddit": data.get("subreddit"),
            "score": data.get("score"),
            "num_comments": data.get("num_comments"),
            "created_utc": data.get("created_utc"),
            "permalink": f"{PUBLIC_BASE}{data.get('permalink', '')}" if data.get("permalink") else None,
            "url": data.get("url"),
            "is_self": data.get("is_self"),
            "selftext": data.get("selftext"),
            # Preserve unavailable/special states instead of dropping them.
            "over_18": data.get("over_18"),
            "locked": data.get("locked"),
            "stickied": data.get("stickied"),
            "removed_by_category": data.get("removed_by_category"),
        }

    @staticmethod
    def _comment_from_child(child: dict[str, Any]) -> dict[str, Any] | None:
        # Skip "more" pagination stubs; the caller learns coverage is partial.
        if child.get("kind") != "t1":
            return None
        data = child.get("data", {})
        return {
            "id": data.get("id"),
            "fullname": data.get("name"),
            "author": data.get("author"),
            "body": data.get("body"),
            "score": data.get("score"),
            "created_utc": data.get("created_utc"),
            "permalink": f"{PUBLIC_BASE}{data.get('permalink', '')}" if data.get("permalink") else None,
            "is_submitter": data.get("is_submitter"),
        }

    def _posts_from_listing(self, payload: Any) -> tuple[list[dict[str, Any]], str | None]:
        """Return ``(posts, after_cursor)`` from a Reddit listing payload."""
        listing = payload.get("data", {}) if isinstance(payload, dict) else {}
        children = listing.get("children", []) if isinstance(listing, dict) else []
        posts = [self._post_from_listing_child(child) for child in children if isinstance(child, dict)]
        after = listing.get("after") if isinstance(listing, dict) else None
        return posts, after

    # -- Tools -----------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        subreddit: str | None = None,
        sort: Sort = "relevance",
        time: TimeWindow = "all",
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Search Reddit, optionally scoped to one subreddit. Read-only."""
        query = _require(query, "query")
        _validate_choice(sort, SORTS, "sort")
        _validate_choice(time, TIME_WINDOWS, "time")
        limit = _clamp_limit(limit)

        params: dict[str, Any] = {"q": query, "sort": sort, "t": time, "limit": limit, "raw_json": 1}
        if subreddit:
            sub = _require(subreddit, "subreddit")
            path = f"/r/{urllib.parse.quote(sub)}/search.json"
            params["restrict_sr"] = "true"
        else:
            path = "/search.json"

        payload, final_url = self._get_json(path, params)
        posts, after = self._posts_from_listing(payload)
        return {
            "query": query,
            "subreddit": subreddit,
            "sort": sort,
            "time": time,
            "count": len(posts),
            "after": after,
            "posts": posts,
            "lineage": self._lineage(final_url, query=query),
        }

    def subreddit_top(
        self,
        subreddit: str,
        *,
        time: TimeWindow = "day",
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """List a subreddit's top posts for a time window. Read-only."""
        sub = _require(subreddit, "subreddit")
        _validate_choice(time, TIME_WINDOWS, "time")
        limit = _clamp_limit(limit)

        path = f"/r/{urllib.parse.quote(sub)}/top.json"
        params = {"t": time, "limit": limit, "raw_json": 1}
        payload, final_url = self._get_json(path, params)
        posts, after = self._posts_from_listing(payload)
        return {
            "subreddit": sub,
            "time": time,
            "count": len(posts),
            "after": after,
            "posts": posts,
            "lineage": self._lineage(final_url, subreddit=sub),
        }

    def get_post(self, post_id: str) -> dict[str, Any]:
        """Fetch a single post's metadata by its base-36 id. Read-only."""
        pid = _require(post_id, "post_id")
        path = f"/by_id/t3_{urllib.parse.quote(pid)}.json"
        payload, final_url = self._get_json(path, {"raw_json": 1})
        posts, _ = self._posts_from_listing(payload)
        if not posts:
            raise RedditError(f"no post found for id {pid!r}")
        return {
            "post": posts[0],
            "lineage": self._lineage(final_url, post_id=pid),
        }

    def get_comments(self, post_id: str, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """Fetch top-level comments for a post. Read-only.

        Reddit's comment endpoint returns a two-element array ``[post, comments]``.
        ``more`` stubs (collapsed/paginated threads) are reported as missing
        coverage rather than silently dropped.
        """
        pid = _require(post_id, "post_id")
        limit = _clamp_limit(limit)
        path = f"/comments/{urllib.parse.quote(pid)}.json"
        payload, final_url = self._get_json(path, {"limit": limit, "raw_json": 1})

        comments: list[dict[str, Any]] = []
        has_more = False
        post: dict[str, Any] | None = None
        if isinstance(payload, list) and len(payload) == 2:
            post_posts, _ = self._posts_from_listing(payload[0])
            post = post_posts[0] if post_posts else None
            children = payload[1].get("data", {}).get("children", []) if isinstance(payload[1], dict) else []
            for child in children:
                if not isinstance(child, dict):
                    continue
                if child.get("kind") == "more":
                    has_more = True
                    continue
                normalised = self._comment_from_child(child)
                if normalised is not None:
                    comments.append(normalised)

        return {
            "post_id": pid,
            "post": post,
            "count": len(comments),
            "has_more": has_more,
            "comments": comments,
            "lineage": self._lineage(final_url, post_id=pid),
        }
