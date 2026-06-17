# reddit-mcp

`reddit-mcp` is a small [Model Context Protocol](https://modelcontextprotocol.io)
server that exposes **read-only Reddit research** as typed tools. It turns the
"call Reddit" capability that previously lived as prose in the `reddit-research`
skill into callable tools, with the terms-aware and rate-limit discipline baked
into the server rather than left to the caller.

Reddit is a noisy public-discussion source, not ground truth. Use these tools
for trend discovery, theme-finding, and hypothesis generation, and check
high-value claims against independent sources.

## Tools

| Tool | Purpose |
| --- | --- |
| `reddit_search(query, subreddit?, sort?, time?, limit?)` | Search Reddit, optionally scoped to one subreddit. |
| `reddit_subreddit_top(subreddit, time?, limit?)` | A subreddit's top posts for a time window. |
| `reddit_get_post(id)` | A single post's metadata by base-36 id. |
| `reddit_get_comments(post_id, limit?)` | Top-level comments for a post (flags `has_more`). |

`sort` is one of `relevance`/`hot`/`top`/`new`/`comments`; `time` is one of
`hour`/`day`/`week`/`month`/`year`/`all`. Every result carries a `lineage`
block (`source`, `access_path`, `url`, `retrieved_at`) so findings can be cited
and re-verified.

## Access & terms

- **Read-only.** The server only reads Reddit's public `.json` endpoints. It
  never writes, votes, or posts.
- **Polite by default.** A descriptive `User-Agent`, minimum spacing between
  requests, and exponential backoff with jitter on `429`/`5xx` keep low-volume
  discovery within Reddit's documented envelope. A `Retry-After` header on a
  rate-limit response is honoured when present. `401`/`403`/`404` surface
  immediately as errors (often a datacenter/CI IP block, removed content, or an
  auth requirement).
- **Escalate when needed.** For higher volume, OAuth, or reliable comment-tree
  pagination, set `REDDIT_BEARER_TOKEN` to use the authenticated API host
  (`oauth.reddit.com`). Obtain a token per Reddit's API terms.

Reddit's public JSON surface is **not** a stable contract; validate current
behavior from the real runtime before depending on a path.

## Configuration (environment, resolved at call time)

| Variable | Effect |
| --- | --- |
| `REDDIT_USER_AGENT` | Override the default descriptive UA. |
| `REDDIT_BEARER_TOKEN` | When set, requests use the authenticated API host. **Never embed this in code or config files.** |

Credentials are read from the environment when a request is made and are never
baked into the package.

## Install

Run directly from GitHub with the MCP extra:

```bash
uvx --from "git+https://github.com/selamy-labs/reddit-mcp@v0.1.0#egg=reddit-mcp[mcp]" reddit-mcp
```

Or with pipx:

```bash
pipx install "reddit-mcp[mcp] @ git+https://github.com/selamy-labs/reddit-mcp@v0.1.0"
```

## MCP client config

```json
{
  "mcpServers": {
    "reddit": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/selamy-labs/reddit-mcp@v0.1.0#egg=reddit-mcp[mcp]",
        "reddit-mcp"
      ],
      "env": {
        "REDDIT_USER_AGENT": "your-app/1.0 (research; +https://example.com)"
      }
    }
  }
}
```

## Architecture

The Reddit logic lives once in `reddit_mcp.core.RedditClient`; the MCP server in
`reddit_mcp.mcp_server` is a thin wrapper that serialises structured results to
JSON and maps expected failures to `ToolError`. All network access goes through
an **injected transport** (`reddit_mcp.transport`), and all timing through an
injected clock, so the full request/parse/backoff path is exercised offline in
tests with canned fixtures. The default `UrllibTransport` uses only the standard
library, so the core package has zero runtime dependencies; the `mcp` SDK is an
optional extra needed only to run the server.

## Development

```bash
python -m pip install -e ".[test]"
ruff format --check .
ruff check .
coverage run -m pytest
coverage report --fail-under=95
```

## License

MIT — see [LICENSE](LICENSE).
