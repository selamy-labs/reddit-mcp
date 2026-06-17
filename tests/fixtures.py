"""Canned, sanitized Reddit JSON used to drive the client fully offline.

These are minimal hand-built payloads shaped like Reddit's public JSON. They are
not copied from live Reddit; they exist only to exercise the parser and the
request/backoff path without any network access.
"""

from __future__ import annotations

import json
from typing import Any


def _post_child(post_id: str, title: str, **over: Any) -> dict[str, Any]:
    data = {
        "id": post_id,
        "name": f"t3_{post_id}",
        "title": title,
        "author": "example_user",
        "subreddit": "test",
        "score": 42,
        "num_comments": 3,
        "created_utc": 1_700_000_000,
        "permalink": f"/r/test/comments/{post_id}/{title.lower().replace(' ', '_')}/",
        "url": f"https://example.invalid/{post_id}",
        "is_self": True,
        "selftext": "body text",
        "over_18": False,
        "locked": False,
        "stickied": False,
        "removed_by_category": None,
    }
    data.update(over)
    return {"kind": "t3", "data": data}


def listing(*children: dict[str, Any], after: str | None = None) -> str:
    return json.dumps({"kind": "Listing", "data": {"after": after, "children": list(children)}})


def search_listing() -> str:
    return listing(
        _post_child("p1", "First Hit"),
        _post_child("p2", "Second Hit"),
        after="t3_p2",
    )


def top_listing() -> str:
    return listing(_post_child("t1", "Top Post"))


def by_id_listing() -> str:
    return listing(_post_child("p1", "Single Post"))


def empty_listing() -> str:
    return listing()


def comments_payload() -> str:
    post = {"kind": "Listing", "data": {"after": None, "children": [_post_child("p1", "Commented Post")]}}
    comment = {
        "kind": "t1",
        "data": {
            "id": "c1",
            "name": "t1_c1",
            "author": "commenter",
            "body": "a comment",
            "score": 7,
            "created_utc": 1_700_000_100,
            "permalink": "/r/test/comments/p1/x/c1/",
            "is_submitter": False,
        },
    }
    more = {"kind": "more", "data": {"count": 5, "children": ["c2", "c3"]}}
    comments = {"kind": "Listing", "data": {"after": None, "children": [comment, more]}}
    return json.dumps([post, comments])


def comments_with_junk_children() -> str:
    """A comment listing containing a non-dict child and a non-t1 child.

    Both must be skipped without crashing or counting toward results.
    """
    post = {"kind": "Listing", "data": {"after": None, "children": []}}
    odd = {"kind": "t1", "data": {"id": "c9", "name": "t1_c9", "body": "real", "author": "u"}}
    not_a_comment = {"kind": "t3", "data": {"id": "x", "title": "a post nested oddly"}}
    children = ["this is not a dict", not_a_comment, odd]
    comments = {"kind": "Listing", "data": {"after": None, "children": children}}
    return json.dumps([post, comments])
