"""MCP server exposing read-only Reddit research as typed tools.

This is an optional integration: install it with ``pip install reddit-mcp[mcp]``.
The core package keeps its runtime dependencies minimal (stdlib only); the
``mcp`` SDK is required only to run this server.

Every tool is a thin wrapper over :class:`reddit_mcp.core.RedditClient`, so the
Reddit logic, rate-limit discipline, and source lineage live in exactly one
place. Tools take structured inputs and return JSON objects. Expected failures
(empty input, blocked access, missing post) surface as ``ToolError`` with a
clean message; the rate-limit and terms-aware behavior is baked into the client,
not left to the caller.

Credentials are resolved at call time from the environment by the client
(``REDDIT_BEARER_TOKEN``, ``REDDIT_USER_AGENT``) and are never embedded here.
"""

from __future__ import annotations

from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
except ModuleNotFoundError as error:  # pragma: no cover - import guard
    raise SystemExit(
        "reddit-mcp server requires the 'mcp' package. Install it with: pip install 'reddit-mcp[mcp]'"
    ) from error

from reddit_mcp.core import DEFAULT_LIMIT, RedditClient, RedditError, Sort, TimeWindow

INSTRUCTIONS = (
    "Read-only Reddit research tools for trend discovery and hypothesis generation. "
    "Reddit is a noisy public-discussion source, not ground truth: treat results as "
    "sentiment, themes, and candidate hypotheses, and check high-value claims against "
    "independent sources. Access uses Reddit's public JSON endpoints with a polite "
    "User-Agent and rate-limit backoff; escalate to the authenticated API "
    "(REDDIT_BEARER_TOKEN) for higher volume or reliable comment trees. Every result "
    "carries source lineage (url + retrieved_at + access_path) so findings can be cited "
    "and re-verified."
)


def _build_client() -> RedditClient:
    """Construct a client. Separated so tests can patch in a fake transport."""
    return RedditClient()


def _run(call: Any) -> dict[str, Any]:
    """Execute a client call, mapping expected failures to ``ToolError``."""
    try:
        return call()
    except RedditError as error:
        raise ToolError(str(error)) from error


def reddit_search(
    query: str,
    subreddit: str | None = None,
    sort: Sort = "relevance",
    time: TimeWindow = "all",
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Search Reddit, optionally scoped to one subreddit.

    ``sort`` is one of relevance/hot/top/new/comments; ``time`` is one of
    hour/day/week/month/year/all. Returns matched posts plus an ``after`` cursor
    and source lineage. Read-only.
    """
    client = _build_client()
    return _run(lambda: client.search(query, subreddit=subreddit, sort=sort, time=time, limit=limit))


def reddit_subreddit_top(
    subreddit: str,
    time: TimeWindow = "day",
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """List a subreddit's top posts for a time window. Read-only."""
    client = _build_client()
    return _run(lambda: client.subreddit_top(subreddit, time=time, limit=limit))


def reddit_get_post(id: str) -> dict[str, Any]:
    """Fetch a single post's metadata by its base-36 id (e.g. "abc123"). Read-only."""
    client = _build_client()
    return _run(lambda: client.get_post(id))


def reddit_get_comments(post_id: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Fetch top-level comments for a post. Read-only.

    Reports ``has_more`` when collapsed/paginated comment branches were not
    fetched, so callers know coverage is partial rather than complete.
    """
    client = _build_client()
    return _run(lambda: client.get_comments(post_id, limit=limit))


TOOLS = (
    reddit_search,
    reddit_subreddit_top,
    reddit_get_post,
    reddit_get_comments,
)


def build_server() -> FastMCP:
    """Build the reddit-mcp server with every research tool registered."""
    server = FastMCP("reddit-mcp", instructions=INSTRUCTIONS)
    for tool in TOOLS:
        server.add_tool(tool)
    return server


def main() -> None:
    """Run the reddit-mcp server over stdio."""
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
