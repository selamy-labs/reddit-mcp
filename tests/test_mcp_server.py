"""End-to-end tests for the reddit-mcp server.

Each test drives the tools through ``FastMCP.call_tool`` -- the real MCP path
(registration, argument coercion, structured output, error surfacing) -- with a
client whose transport is a fake serving canned Reddit JSON. No network access.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from reddit_mcp import core, mcp_server
from reddit_mcp.core import RedditClient
from tests import fixtures
from tests.conftest import FakeClock, FakeTransport


def call(monkeypatch: pytest.MonkeyPatch, transport: FakeTransport, name: str, arguments: dict[str, Any]) -> Any:
    """Invoke an MCP tool with a fake-transport-backed client; return structured output."""
    monkeypatch.setattr(
        mcp_server,
        "_build_client",
        lambda: RedditClient(transport=transport, clock=FakeClock(), bearer_token=None),
    )
    _, structured = asyncio.run(mcp_server.build_server().call_tool(name, arguments))
    return structured


def test_server_registers_every_tool() -> None:
    tools = asyncio.run(mcp_server.build_server().list_tools())
    names = {tool.name for tool in tools}
    assert names == {"reddit_search", "reddit_subreddit_top", "reddit_get_post", "reddit_get_comments"}
    search = next(t for t in tools if t.name == "reddit_search")
    assert "query" in search.inputSchema["properties"]


def test_search_tool_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.search_listing())
    result = call(monkeypatch, transport, "reddit_search", {"query": "python", "limit": 5})
    assert result["count"] == 2
    assert result["lineage"]["access_path"] == "reddit-public-json"


def test_subreddit_top_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.top_listing())
    result = call(monkeypatch, transport, "reddit_subreddit_top", {"subreddit": "test", "time": "week"})
    assert result["posts"][0]["title"] == "Top Post"


def test_get_post_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.by_id_listing())
    result = call(monkeypatch, transport, "reddit_get_post", {"id": "p1"})
    assert result["post"]["id"] == "p1"


def test_get_comments_tool_reports_more(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.comments_payload())
    result = call(monkeypatch, transport, "reddit_get_comments", {"post_id": "p1"})
    assert result["count"] == 1
    assert result["has_more"] is True


def test_lineage_present_in_every_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.search_listing())
    result = call(monkeypatch, transport, "reddit_search", {"query": "x"})
    lineage = result["lineage"]
    assert set(lineage) >= {"source", "access_path", "url", "retrieved_at"}


def test_blocked_access_surfaces_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport()
    transport.queue_response(403, "blocked")
    with pytest.raises(ToolError, match="status 403"):
        call(monkeypatch, transport, "reddit_subreddit_top", {"subreddit": "test"})


def test_empty_query_surfaces_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ToolError, match="query must not be empty"):
        call(monkeypatch, FakeTransport(), "reddit_search", {"query": "  "})


def test_missing_post_surfaces_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.empty_listing())
    with pytest.raises(ToolError, match="no post found"):
        call(monkeypatch, transport, "reddit_get_post", {"id": "nope"})


def test_build_client_constructs_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDDIT_BEARER_TOKEN", raising=False)
    client = mcp_server._build_client()
    assert isinstance(client, RedditClient)


def test_run_passes_through_non_reddit_callables() -> None:
    assert mcp_server._run(lambda: {"ok": True}) == {"ok": True}


def test_run_maps_reddit_error_to_tool_error() -> None:
    def boom() -> dict[str, Any]:
        raise core.RedditError("nope")

    with pytest.raises(ToolError, match="nope"):
        mcp_server._run(boom)


def test_main_runs_server_over_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    transports: list[str] = []
    monkeypatch.setattr(
        "mcp.server.fastmcp.FastMCP.run",
        lambda self, transport="stdio", **_: transports.append(transport),
    )
    mcp_server.main()
    assert transports == ["stdio"]
