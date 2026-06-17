"""Offline unit tests for the Reddit research client.

Every test injects a :class:`FakeTransport` and :class:`FakeClock`, so the full
request/parse/backoff path runs without any network access.
"""

from __future__ import annotations

import pytest

from reddit_mcp.core import MAX_LIMIT, OAUTH_BASE, PUBLIC_BASE, RedditClient, RedditError
from tests import fixtures
from tests.conftest import FakeClock, FakeTransport


def make_client(transport: FakeTransport, **kwargs: object) -> RedditClient:
    return RedditClient(transport=transport, clock=FakeClock(), bearer_token=None, **kwargs)


# -- happy paths ---------------------------------------------------------------


def test_search_returns_posts_and_lineage() -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.search_listing(), url="https://www.reddit.com/search.json?q=x")
    client = make_client(transport)

    result = client.search("python", sort="top", time="week", limit=10)

    assert result["count"] == 2
    assert [p["id"] for p in result["posts"]] == ["p1", "p2"]
    assert result["after"] == "t3_p2"
    assert result["sort"] == "top"
    lineage = result["lineage"]
    assert lineage["source"] == "reddit"
    assert lineage["access_path"] == "reddit-public-json"
    assert lineage["url"].startswith("https://www.reddit.com/search.json")
    assert lineage["retrieved_at"].endswith("Z")
    # The request went to the public host with our query params.
    sent = transport.requests[0]["url"]
    assert sent.startswith(f"{PUBLIC_BASE}/search.json?")
    assert "q=python" in sent and "raw_json=1" in sent


def test_search_scoped_to_subreddit_restricts_sr() -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.search_listing())
    client = make_client(transport)

    result = client.search("rust", subreddit="programming")

    assert result["subreddit"] == "programming"
    sent = transport.requests[0]["url"]
    assert "/r/programming/search.json" in sent
    assert "restrict_sr=true" in sent


def test_subreddit_top_parses_listing() -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.top_listing())
    client = make_client(transport)

    result = client.subreddit_top("test", time="day", limit=5)

    assert result["count"] == 1
    assert result["posts"][0]["title"] == "Top Post"
    assert "/r/test/top.json" in transport.requests[0]["url"]
    assert result["lineage"]["subreddit"] == "test"


def test_get_post_returns_single_post() -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.by_id_listing())
    client = make_client(transport)

    result = client.get_post("p1")

    assert result["post"]["id"] == "p1"
    assert result["post"]["permalink"].startswith(PUBLIC_BASE)
    assert "/by_id/t3_p1.json" in transport.requests[0]["url"]


def test_get_comments_normalises_and_flags_more() -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.comments_payload())
    client = make_client(transport)

    result = client.get_comments("p1", limit=50)

    assert result["count"] == 1
    assert result["comments"][0]["body"] == "a comment"
    # The "more" stub is reported as missing coverage, not silently dropped.
    assert result["has_more"] is True
    assert result["post"]["title"] == "Commented Post"


def test_get_comments_skips_junk_children() -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.comments_with_junk_children())
    client = make_client(transport)

    result = client.get_comments("p1")
    # Non-dict child and the non-t1 (t3) child are skipped; only the real t1 counts.
    assert result["count"] == 1
    assert result["comments"][0]["body"] == "real"
    assert result["has_more"] is False


def test_get_comments_handles_unexpected_shape() -> None:
    transport = FakeTransport()
    transport.queue_response(200, "{}")  # not the [post, comments] array
    client = make_client(transport)

    result = client.get_comments("p1")
    assert result["count"] == 0
    assert result["post"] is None
    assert result["has_more"] is False


# -- rate-limit / backoff ------------------------------------------------------


def test_backoff_then_success_on_429() -> None:
    transport = FakeTransport()
    transport.queue_response(429, "slow down")
    transport.queue_response(503, "unavailable")
    transport.queue_response(200, fixtures.top_listing())
    clock = FakeClock()
    client = RedditClient(transport=transport, clock=clock, bearer_token=None, base_backoff=1.0)

    result = client.subreddit_top("test")

    assert result["count"] == 1
    assert len(transport.requests) == 3
    # Two backoff sleeps happened before success (exponential growth).
    assert len(clock.sleeps) == 2
    assert clock.sleeps[1] > clock.sleeps[0]


def test_retry_after_header_honoured_on_429() -> None:
    transport = FakeTransport()
    transport.queue_response(429, "slow down", headers={"Retry-After": "5"})
    transport.queue_response(200, fixtures.top_listing())
    clock = FakeClock()
    client = RedditClient(transport=transport, clock=clock, bearer_token=None, base_backoff=1.0)

    client.subreddit_top("test")
    # The exact Retry-After value is used instead of exponential backoff.
    assert 5.0 in clock.sleeps


def test_malformed_retry_after_falls_back_to_backoff() -> None:
    transport = FakeTransport()
    transport.queue_response(503, "later", headers={"Retry-After": "soon-ish"})
    transport.queue_response(429, "later", headers={"Retry-After": "-3"})
    transport.queue_response(200, fixtures.top_listing())
    clock = FakeClock()
    client = RedditClient(transport=transport, clock=clock, bearer_token=None, base_backoff=1.0)

    client.subreddit_top("test")
    # Neither malformed value (non-numeric, negative) was used verbatim.
    assert 5.0 not in clock.sleeps
    assert all(s > 0 for s in clock.sleeps)


def test_backoff_exhausted_raises() -> None:
    transport = FakeTransport()
    for _ in range(5):  # max_retries(2) + 1 attempts, queue extra to be safe
        transport.queue_response(503, "unavailable")
    client = RedditClient(transport=transport, clock=FakeClock(), bearer_token=None, max_retries=2)

    with pytest.raises(RedditError, match="unexpected status 503"):
        client.subreddit_top("test")
    # initial + 2 retries = 3 attempts
    assert len(transport.requests) == 3


def test_transport_error_retries_then_raises() -> None:
    transport = FakeTransport()
    transport.queue_error("connection refused")
    transport.queue_error("connection refused")
    clock = FakeClock()
    client = RedditClient(transport=transport, clock=clock, bearer_token=None, max_retries=1)

    with pytest.raises(RedditError, match="failed after 2 attempts"):
        client.subreddit_top("test")
    assert len(transport.requests) == 2  # initial attempt + one retry
    assert clock.sleeps  # backed off before retrying


def test_transport_error_then_recovers() -> None:
    transport = FakeTransport()
    transport.queue_error("dns")
    transport.queue_response(200, fixtures.top_listing())
    client = make_client(transport, max_retries=2)

    result = client.subreddit_top("test")
    assert result["count"] == 1


def test_min_interval_spacing_between_calls() -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.empty_listing())
    transport.queue_response(200, fixtures.empty_listing())
    clock = FakeClock()
    client = RedditClient(transport=transport, clock=clock, bearer_token=None, min_interval=2.0)

    client.subreddit_top("a")
    client.subreddit_top("b")
    # The second call had to wait out the min interval (no time passed between).
    assert any(abs(s - 2.0) < 1e-9 for s in clock.sleeps)


# -- hard denials / errors -> RedditError --------------------------------------


@pytest.mark.parametrize("status", [401, 403, 404])
def test_hard_deny_statuses_raise_immediately(status: int) -> None:
    transport = FakeTransport()
    transport.queue_response(status, "denied")
    client = make_client(transport)

    with pytest.raises(RedditError, match=f"status {status}"):
        client.subreddit_top("test")
    assert len(transport.requests) == 1  # no retry on a hard deny


def test_non_json_body_raises() -> None:
    transport = FakeTransport()
    transport.queue_response(200, "<html>not json</html>")
    client = make_client(transport)

    with pytest.raises(RedditError, match="non-JSON"):
        client.subreddit_top("test")


def test_unexpected_status_raises() -> None:
    transport = FakeTransport()
    transport.queue_response(418, "teapot")
    client = make_client(transport)

    with pytest.raises(RedditError, match="unexpected status 418"):
        client.subreddit_top("test")


def test_get_post_missing_raises() -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.empty_listing())
    client = make_client(transport)

    with pytest.raises(RedditError, match="no post found"):
        client.get_post("nope")


# -- input validation ----------------------------------------------------------


def test_empty_query_rejected() -> None:
    with pytest.raises(RedditError, match="query must not be empty"):
        make_client(FakeTransport()).search("   ")


def test_invalid_sort_rejected() -> None:
    with pytest.raises(RedditError, match="invalid sort"):
        make_client(FakeTransport()).search("x", sort="sideways")  # type: ignore[arg-type]


def test_invalid_time_rejected() -> None:
    with pytest.raises(RedditError, match="invalid time"):
        make_client(FakeTransport()).subreddit_top("x", time="decade")  # type: ignore[arg-type]


def test_limit_below_one_rejected() -> None:
    with pytest.raises(RedditError, match="limit must be >= 1"):
        make_client(FakeTransport()).subreddit_top("x", limit=0)


def test_limit_clamped_to_max() -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.empty_listing())
    client = make_client(transport)
    client.subreddit_top("x", limit=999)
    assert f"limit={MAX_LIMIT}" in transport.requests[0]["url"]


# -- credentials at call time --------------------------------------------------


def test_bearer_token_switches_host_and_adds_auth_header() -> None:
    transport = FakeTransport()
    transport.queue_response(200, fixtures.top_listing(), url=f"{OAUTH_BASE}/r/test/top.json")
    client = RedditClient(transport=transport, clock=FakeClock(), bearer_token="secret-token")

    result = client.subreddit_top("test")

    assert result["lineage"]["access_path"] == "reddit-oauth-json"
    assert transport.requests[0]["url"].startswith(OAUTH_BASE)
    assert transport.requests[0]["headers"]["Authorization"] == "Bearer secret-token"


def test_token_resolved_from_env_not_embedded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDDIT_BEARER_TOKEN", "env-token")
    transport = FakeTransport()
    transport.queue_response(200, fixtures.top_listing())
    client = RedditClient(transport=transport, clock=FakeClock())
    client.subreddit_top("test")
    assert transport.requests[0]["headers"]["Authorization"] == "Bearer env-token"


def test_user_agent_is_descriptive_and_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDDIT_USER_AGENT", raising=False)
    transport = FakeTransport()
    transport.queue_response(200, fixtures.empty_listing())
    transport.queue_response(200, fixtures.empty_listing())

    default_client = make_client(transport)
    default_client.subreddit_top("x")
    assert "reddit-mcp/" in transport.requests[0]["headers"]["User-Agent"]

    custom = RedditClient(transport=transport, clock=FakeClock(), bearer_token=None, user_agent="my-bot/1.0 (+url)")
    custom.subreddit_top("x")
    assert transport.requests[1]["headers"]["User-Agent"] == "my-bot/1.0 (+url)"
