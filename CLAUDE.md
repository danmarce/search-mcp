# CLAUDE.md — context for search-mcp

A tiny MCP server that gives an LLM a **single broad-web search tool**, backed by the keyless
multi-engine `ddgs` library. It is the "broad web" tier of a grounded-model shelf — the thing that
answers *"recent / latest / today / this week / prices / weather / current events"* — replacing a
DuckDuckGo tool that currently only exists as an **OpenWebUI-native tool** (not portable, off-standard).

**Read [`C:\Code\forge-mcp`](../forge-mcp) first** — its `CLAUDE.md`, `server.py`, and `tests/test_tools.py`
are the reference implementation for the house MCP standard (`~/.claude/mcp-server-standard.md`). This
server is much smaller than forge-mcp but follows the same contract.

Build it in a dedicated session; deploy config goes in the homelab repo, not here (see **Deployment**).

## Where it fits (why the surface is deliberately tiny)

The family door (Gemma 12B via OpenWebUI) already has a curated, **non-overlapping** tool shelf, and the
12B picks the right tool purely from docstrings — that only works if the tools don't collide:

- **openzim-mcp** — deep / encyclopedic / historical (Wikipedia offline). The "read it deeply" tier.
- **news-mcp** — curated RSS current events (editorial/balance choice).
- **igepn-mcp / football-mcp** — structured local data (quakes/volcanoes, football tables).
- **search-mcp** ← *this* — broad web, **snippets only**, for everything the above don't cover.

So this server exposes **exactly one tool**. Do **not** add news/images/video/fetch tools — those
either belong to a sibling (news-mcp) or were deliberately dropped (see **Decisions**). A fat tool
surface here would muddy the 12B's tool selection, which is the whole thing the shelf protects.

## The one tool: `web_search`

```python
web_search(query: str, max_results: int = 10, region: str | None = None) -> <result block>
```

- **`query`** — passed to the backend **verbatim**. Never rewritten/"enhanced".
- **`max_results`** — default **10** (matches the proven OpenWebUI tool). Clamp to `1..25`; an
  out-of-range value is a `refused`, not a crash.
- **`region`** — optional `ddgs` region code (e.g. `"us-en"`, `"wt-wt"` = no region). Pass through verbatim.
- Docstring (the 12B's decision boundary — keep it sharp): *"Search the web for CURRENT information:
  news, current events, anything recent/latest/today/this week, weather, prices, time-sensitive facts.
  Do NOT use for general knowledge like recipes, definitions, or history — those have better tools."*

**Return** (JSON in the text block; the caller branches on `status`, never on prose):

```json
{
  "status": "ok",
  "query": "<verbatim>",
  "backend": "auto-web",
  "region": "wt-wt",
  "count": 7,
  "results": [
    { "title": "...", "url": "...", "snippet": "..." }
  ]
}
```

`ddgs.text()` returns `title` / `href` / `body` — map **`href` → `url`**, **`body` → `snippet`**. Echo
the resolved params (query, backend, region, max_results) so a result is reproducible (standard §5).

## Result contract: ok / refused / error (standard §3)

- **ok** — the search ran. Includes the **empty case**: no hits → `status:"ok"`, `results:[]`,
  `count:0`. *A zero-result search is a successful search, not an error* — **but only when the backend can
  prove it was empty.** ddgs can't (a blocked engine looks empty), so its "nothing" is `error` /
  `kind:"empty_or_blocked"` — see Findings 1.
- **refused** — caller-fixable input: blank/whitespace query, `max_results` out of range. Include
  `reason` + `advice`. **Never raise it as an exception** (that makes a correct block, a false block,
  and a crash indistinguishable to the caller).
- **error** — a genuine fault: the backend raised / all engines failed / rate-limited to nothing.
  `kind:"backend"` + `detail`. **Fail loud** — backend down must be a clear `error`, **never** a silent
  empty list (which would look identical to "the web has nothing", the exact bug in the OpenWebUI tool
  that returns `"No results."` for both).

## Backend: `ddgs` behind a one-function seam (this is also the test seam)

`ddgs` ("Dux Distributed Global Search") is itself a **multi-engine metasearch** — `backend="auto"`
fans out across google/bing/brave/startpage/mojeek/duckduckgo/yandex/yahoo/wikipedia with fallback.
Keyless. That already gives the engine diversity SearXNG would (see **Decisions** — SearXNG is not
needed).

Isolate the actual search call in **one small function/object** — do not inline `ddgs` in the tool
handler:

```python
# backend.py (the seam) — as built; region is already resolved/validated by the server
def search_backend(query: str, max_results: int, region: str, timeout: int) -> list[dict]:
    """Return [{title,url,snippet}]. Raises on backend failure (→ caller maps to status:error)."""
    try:
        with DDGS(timeout=timeout) as d:
            rows = d.text(query, region=region, safesearch="moderate", max_results=max_results,
                          backend=web_engines())   # auto minus wikipedia/grokipedia — Decision 5
    except DDGSException as e:
        if type(e) is DDGSException and str(e) == "No results found.":
            raise EmptyOrBlocked(...) from e   # NOT [] — ddgs can't tell empty from blocked (Findings 1)
        raise
    return [{"title": r.get("title") or "", "url": r.get("href") or "", "snippet": r.get("body") or ""} for r in rows]
```

The server takes the seam as `build_server(settings, search=...)` (default: the ddgs one), so tests inject a
fake rather than monkeypatching a module global.

Two payoffs for one cheap seam:
1. **Swap path** — if `ddgs` ever gets rate-limited/blocked, or the with-the-grain principle wins, drop
   in a `BraveBackend` / `MojeekBackend` (sanctioned APIs) or a self-hosted SearXNG backend behind the
   **same signature**. One-file edit, no server rewrite. (This is *not* built now — YAGNI — the seam is
   just the shape that makes it a one-file edit later.)
2. **Test seam** — `ddgs` isn't `httpx`-injectable, so the standard's "fake backend, no network" is done
   by **injecting/monkeypatching `search_backend`** to return canned rows (or raise), rather than
   `httpx.MockTransport`. Same intent as forge-mcp's tests: call the tool **by name through the server**,
   assert the shaped result, zero network.

## Tool metadata (standard §1 — all explicit, pinned by a test)

| field | value | why |
|---|---|---|
| `title` | `"Web search"` | human name |
| `readOnlyHint` | `true` | only reads/computes |
| `destructiveHint` | `false` | reads only |
| `idempotentHint` | `true` | pure read, no side effects |
| `openWorldHint` | `true` | reaches the public web via ddgs |

Set `title` in **both** `@mcp.tool(title=...)` and `ToolAnnotations(title=...)`. Confirm the installed
`mcp` SDK supports `title=` on the decorator; bump the dep if not.

## Tests (standard §2)

- A test lists tools through the server and asserts `web_search` has a `title` and **all four hints as
  booleans in the wire JSON** (`annotations.model_dump(by_alias=True)`). A new tool missing metadata
  must fail.
- `web_search` is called **by name through the server** with the backend seam faked:
  - normal rows → `status:"ok"`, correct `href→url` / `body→snippet` mapping, echoed params.
  - empty rows → `status:"ok"`, `results:[]`, `count:0` (NOT an error).
  - backend raises → `status:"error"`, `kind:"backend"`, `detail` present.
  - blank query / bad `max_results` → `status:"refused"` with `advice` (and **not** raised).
- No network, no live ddgs call in tests.

## Keep bulk data out (standard §4)

Results are inherently small (title/url/snippet) — no base64 concern, nothing to move out-of-band.
**Do not add page-fetch/content-extraction here** (ddgs *has* `.extract()` — resist it). If a fetch
tier is ever wanted it is a *separate* server and must use the out-of-band link/ref pattern (see
forge-mcp §4); it does not belong in this one.

## Dependencies & conventions (standard §7)

- Python 3.12, `uv`, **few deps**: `ddgs`, `mcp` (SDK). That's essentially it.
- Behind a TLS-inspecting proxy (e.g. the dev box), `uv` needs `--system-certs` (or `UV_NATIVE_TLS=1`);
  consider a `truststore` injection at runtime like forge-mcp if ddgs's outbound HTTPS hits an
  inspecting middlebox during dev. (Deploy target has no TLS inspection.)
- This CLAUDE.md carries the *why* — don't let a future session undo a deliberate choice (see **Decisions**).

## Auth & deployment (standard §6 — keep the repo generic)

- Transports: **stdio** (for a local client) and **streamable-HTTP + bearer** (`SEARCH_MCP_TOKEN` from
  env) for the networked/family-door path. `/healthz` stays open. **No OAuth** in the server — an mcpo
  gateway fronts it.
- The repo is **generic and public** — no homelab hostnames, IPs, ports, or mcpo wiring in code/tests/docs.
- **Deployment lives in the homelab repo**, not here: `C:\Code\homelab\containers\stacks\search-mcp`
  (compose + bearer secret + mcpo route + OWUI wiring). The family-door model reaches this via mcpo like
  the other data-MCPs; a tight on-by-default docstring is what makes the 12B pick it unprompted.

## Decisions (the why — do not re-litigate)

1. **`ddgs` backend, not the Brave Search API.** Keyless, Python-native, multi-engine, and *already
   proven* (it's what the OpenWebUI DuckDuckGo tool uses; Gemma answered a current event well on its
   snippets). The honest asterisk: `ddgs` **scrapes** engine result pages, the gray zone
   [[data-access-with-the-grain]] usually avoids — knowingly accepted here because (a) it's already in
   use via OWUI and (b) this is the broad *fallback* tier. **Brave API or Mojeek API** are the
   *sanctioned* upgrade if the principle or reliability ever wins — dropped in behind the seam, not now.
2. **No SearXNG.** `ddgs` is itself a multi-engine aggregator (`backend="auto"`), so a self-hosted
   SearXNG would be a second service delivering aggregation we already have. SearXNG only becomes worth
   a container if `ddgs` gets blocked/rate-limited enough that owning the metasearch IP pays off —
   decide that *then*, with evidence.
3. **Snippets only, no fetch.** openzim-mcp is the deep tier; snippets + the model's synthesis proved
   enough for the time-sensitive tier. `research-mcp` (planned search+fetch) was **retired, not built** —
   openzim stays the knowledge tier. If snippets ever fall short, a fetch tier is a *separate* later
   server, not bolted on here.
4. **One tool, not the `ddgs` 6-tool surface.** `ddgs[mcp]` (the library's own `ddgs mcp`, stdio) exposes
   `search_text/images/news/videos/books/extract_content` with no metadata, no status contract, no config
   passthrough. That's **fine for personal Claude Code** (`pip install -U ddgs[mcp]`), but **wrong for the
   family door** — its `search_news`/`extract_content` collide with news-mcp / the retired research tier
   and pollute the 12B's tool selection. This server is the curated, scoped, to-standard counterpart.
5. **`auto` minus the encyclopedias (`backend` echoes `"auto-web"`).** ddgs's `auto` *pins* `wikipedia` +
   `grokipedia` to the front of every text search. Encyclopedic hits are the wrong answer for this
   current-events tier (openzim-mcp is the deep tier) and aren't the best web results anyway — and with region
   `wt-wt` the wikipedia engine always fails (it builds `wt.wikipedia.org`, which doesn't exist). Grokipedia
   is excluded on its own merits too: given the controversy over its content, it is not a source this server
   should feed to the family door — so don't re-add it even if the encyclopedia reasoning ever changes. So
   `backend.web_engines()` passes every *other* ddgs text engine as an explicit list, **shuffled per call**
   (ddgs doesn't shuffle explicit lists; `auto` does, to spread load). Built from ddgs's own registry minus
   `EXCLUDED_ENGINES`, so engines ddgs adds later are picked up automatically.

## Findings (made while building — ddgs 9.16, 2026-10)

1. ★ **ddgs cannot report a clean empty search — so this server never returns `ok/[]` from ddgs.** ddgs raises
   `DDGSException("No results found.")` when no engine produced a row and none *raised*. But its engines treat
   **any non-200 response as "no results"** (`ddgs/base.py` `request()` returns `None` on 403/429/captcha), so
   "every engine is blocking/rate-limiting this server" raises *the identical exception* as "the web has
   nothing". An early version mapped it to `[]` — i.e. a ban looked like an empty web, the exact OpenWebUI bug
   this server exists to fix. Now the seam raises `EmptyOrBlocked` → `status:"error"`, `kind:"empty_or_blocked"`
   (a distinct kind, so a caller *can* try one rewording). Real exceptions (connection errors, `Timeout`/
   `RatelimitException`) still propagate as `kind:"backend"`. **Do not "fix" this back to `[]`.** The `ok/[]`
   path stays in the contract for a future backend that can tell (a sanctioned API). Pinned by `test_seam_*`.
2. **ddgs is flaky from a single IP, and it's the engines, not us.** Live runs (8 current-events queries,
   repeated) succeeded ~40–100% per run, varying run to run — same for plain `auto` and `auto-web`, same for
   `wt-wt` and `us-en`. Per-engine probes after ~150 test queries: nearly every engine returning nothing (Google
   403, Brave 429, Mojeek 403 — all silently "empty", per finding 1). So **heavy testing gets this IP
   throttled**; keep live smoke tests to a handful of queries. Family-door volume is low, so this is tolerable —
   but it's the evidence to watch: if `empty_or_blocked` / `backend` errors are common in production, that's
   the trigger for Decision 1's sanctioned-API swap (Brave/Mojeek API behind the seam), not for more retries.
3. **Region must be `country-lang`.** Engines do `region.split("-")` — a bare `"us"` crashes every engine, so
   the server **refuses** a malformed region up front (caller-fixable) instead of reporting a backend error.
4. **ddgs/primp logging goes to stderr** (primp's Rust "response: <url> <code>" lines included) — verified
   stdout stays clean, so stdio transport is safe. The `ddgs` logger's INFO "Error in engine …" lines are useful
   ops signal; left on.
5. **TLS:** ddgs's HTTP client is primp (Rust), so the `truststore` injection doesn't apply to it; it worked
   fine through the dev box's TLS-inspecting proxy anyway. `uv` itself still needs `--system-certs` there.
6. **`mcp>=2.2`**, not the siblings' `>=1.12`: `mcp.server.mcpserver.MCPServer` and `title=` on the decorator
   are 2.x APIs. (forge-mcp/news-mcp declare `>=1.12` but lock 2.x — their floor is stale.)

## Build order

1. Scaffold `uv` project; add `ddgs` + `mcp`. Mirror forge-mcp's layout (`src/search_mcp/{__init__,server,backend}.py`, `tests/`).
2. `backend.py` — the `search_backend()` seam over `ddgs.text(...)` (auto minus encyclopedias, Decision 5).
3. `server.py` — `MCPServer`, the single `web_search` tool with full metadata, the ok/refused/error wrapping, the `BearerAuth` + `/healthz` (copy forge-mcp's shape, minus upload/img).
4. `__init__.py` — CLI (`serve --transport stdio|http`), truststore inject if needed.
5. `tests/test_tools.py` — metadata-in-wire-JSON pin + the four `web_search` cases against the faked seam.
6. README (generic/public), LICENSE (MPL-2.0 like forge-mcp), the `Co-Authored-By: Claude` trailer on commits.
