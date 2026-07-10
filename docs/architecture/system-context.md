# System context

`reddit-mcp` exposes read-only Reddit research tools to an MCP client. The
server delegates Reddit behavior to one client, which owns validation, request
pacing, retries, response normalization, and source lineage.

```mermaid
flowchart LR
    mcp_client["MCP client"]
    server["reddit-mcp<br/>FastMCP server"]
    client["RedditClient<br/>validation, pacing, retries, lineage"]
    config["Runtime environment<br/>REDDIT_USER_AGENT<br/>REDDIT_BEARER_TOKEN (optional)"]
    public_api["Reddit public JSON<br/>www.reddit.com"]
    oauth_api["Reddit OAuth API<br/>oauth.reddit.com"]

    mcp_client -->|"stdio JSON-RPC tool call"| server
    server -->|"typed Python call"| client
    config -->|"read when the client is constructed"| client
    client -->|"unauthenticated HTTPS GET"| public_api
    client -.->|"bearer-authenticated HTTPS GET"| oauth_api
    public_api -->|"JSON posts and comments"| client
    oauth_api -.->|"JSON posts and comments"| client
    client -->|"structured result or RedditError"| server
    server -->|"JSON result or ToolError"| mcp_client
```

The OAuth path is used only when `REDDIT_BEARER_TOKEN` is present. Both paths
are read-only. `UrllibTransport` performs HTTP I/O, while the MCP wrapper maps
expected `RedditError` failures to MCP `ToolError` responses.
