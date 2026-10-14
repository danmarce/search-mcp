# search-mcp

An [MCP](https://modelcontextprotocol.io) server with **one tool**: broad web search for current,
time-sensitive information. It returns short **snippets** (title, url, snippet), not full pages. It is backed by
the keyless multi-engine [`ddgs`](https://pypi.org/project/ddgs/) library, which fans out across several web search
engines with fallback (its encyclopedia engines, Wikipedia and Grokipedia, are left out: encyclopedic hits are
the wrong answer for current events).

It is deliberately small. It is meant to sit next to other, non-overlapping tools (an offline encyclopedia, a
curated news feed, structured local data), so a small local model can pick the right tool from the docstrings
alone. Design rationale: [CLAUDE.md](CLAUDE.md).

## Tool

`web_search(query, max_results=10, region=None)`

| arg | |
|---|---|
| `query` | sent to the engines **verbatim**, never rewritten |
| `max_results` | 1–25, default 10 |
| `region` | `country-lang` code such as `us-en`, `es-es`, `de-de`; default `wt-wt` (no region) |

Every result is one JSON text block with a `status` field. Callers branch on it, never on prose:

```json
{"status":"ok","query":"...","backend":"auto-web","region":"wt-wt","safesearch":"moderate","max_results":10,
 "attempts":1,"count":2,"results":[{"title":"...","url":"https://...","snippet":"..."}]}
```

- **`ok`**: the search ran and returned results. (`results:[]` is reserved for a backend that can prove a
  search was empty; ddgs can't, see below.)
- **`refused`**: input the caller can fix (blank query, `max_results` out of range, malformed region), with
  `reason` and `advice`. It is returned as a normal result, never raised.
- **`error`**: the search backend failed (blocked, rate-limited, timed out, network down), with `kind` and
  `detail`. A backend outage is always reported as an error, never as an empty result. A failed search is
  retried server-side first (`SEARCH_ATTEMPTS`, default 2), because a second try with a different engine
  order often succeeds. Every result echoes `attempts`, so an `error` has already been retried.
  `kind:"empty_or_blocked"` means no engine returned anything: ddgs counts an engine that blocks or
  rate-limits it as "no results", so it can't tell an obscure query from a blocked server. One reworded retry
  is reasonable; otherwise treat search as unavailable.

All resolved parameters are echoed back, so a result is reproducible. The tool declares a `title` and all four
MCP hints: read-only, non-destructive, idempotent, open-world.

## Run

```bash
uv sync
uv run search-mcp serve                     # stdio, for a local MCP client
SEARCH_MCP_TOKEN=secret uv run search-mcp serve --transport http --host 0.0.0.0 --port 8000   # streamable-http at /mcp
uv run pytest                               # no network: the backend is faked
```

| env | default | |
|---|---|---|
| `SEARCH_MCP_TOKEN` | unset | bearer token required on HTTP (`/healthz` stays open) |
| `SEARCH_TIMEOUT` | `10` | seconds ddgs waits on each batch of engines |
| `SEARCH_ATTEMPTS` | `2` | backend calls per search before reporting an error (each reshuffles the engines) |
| `DDGS_PROXY` | unset | optional outbound proxy for ddgs (read by the library itself) |

> **TLS note:** behind a TLS-inspecting proxy, `uv` may report `UnknownIssuer`. Pass `--system-certs` (or set
> `UV_NATIVE_TLS=1`) so it trusts the OS certificate store.

### Claude Desktop / Claude Code (stdio)

```json
{
  "mcpServers": {
    "search": { "command": "uv", "args": ["--directory", "/path/to/search-mcp", "run", "search-mcp", "serve"] }
  }
}
```

## Docker

```bash
docker build -t search-mcp .
docker run --rm -p 8000:8000 -e SEARCH_MCP_TOKEN=... search-mcp
```

The server is stateless: no database, no volume. It needs outbound HTTPS to the public search engines.

## Auth and exposure

Auth is deliberately simple: stdio, or streamable-HTTP with a single bearer token (`SEARCH_MCP_TOKEN`) on a
private network, for example behind [mcpo](https://github.com/open-webui/mcpo). There is **no OAuth, no users
and no audit log**, by design. To publish it more widely, put a management layer in front of it: an MCP
gateway or auth proxy that handles OAuth, users and audit for all your servers at once.

## A note on the backend

`ddgs` gets its results by scraping search-engine result pages. It needs no API key, but it is not a sanctioned
API, and engines may rate-limit or block it. That shows up here as `status:"error"`. The backend sits behind a
single function (`src/search_mcp/backend.py`), so a sanctioned API (Brave, Mojeek) or a self-hosted SearXNG can
replace it by editing that one file.

## AI assistance

search-mcp is developed openly with the help of Claude (Anthropic). Commits Claude helped write carry a
`Co-Authored-By: Claude` trailer. The code and design are open source so the work can be inspected, reused, and
given back.

## License

[MPL-2.0](LICENSE) © Daniel Núñez.
