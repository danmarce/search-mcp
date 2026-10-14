"""MCP server: ONE broad-web search tool, snippets only.

Design contract (see CLAUDE.md for the why):
  * Exactly one tool, `web_search` — the family-door model picks tools purely from docstrings, so this server
    must not overlap its siblings (news-mcp = curated current events, openzim-mcp = deep/encyclopedic).
  * The query is passed to the backend VERBATIM; every resolved parameter is echoed back.
  * status = ok | refused | error. An empty search is "ok" with results=[]; a backend fault is "error" —
    never a silent empty list (that made "the web has nothing" and "search is down" look identical).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import backend
from .config import DEFAULT_MAX_RESULTS, DEFAULT_REGION, MAX_RESULTS_RANGE, Settings

SearchFn = Callable[[str, int, str, int], list[dict[str, str]]]

log = logging.getLogger("search_mcp")

INSTRUCTIONS = """\
Broad web search for CURRENT, time-sensitive information. Returns short snippets (title, url, snippet) from
several search engines — not full pages. Synthesize an answer from the snippets, cite the `url` of each source
you use, and say so plainly when the snippets don't answer the question. Every result has a `status`: "ok"
(the search ran; `results` may be empty — then the web had nothing for that query), "refused" (fix the input
as `advice` says), or "error" (the search failed and the server ALREADY retried it — don't repeat the same
query; say web search is unavailable right now. Only for kind "empty_or_blocked" is one reworded query worth
trying, since an extremely obscure query looks the same).
"""

# Region codes are `country-lang` (ddgs engines split on "-"); "wt-wt" = no region. A malformed one would crash
# every engine, so it is refused up front as a caller-fixable input.
REGION_RE = re.compile(r"[A-Za-z]{2}-[A-Za-z]{2}")


# All four hints explicit (omitted hints default to the worst case: destructive + open-world). web_search only
# reads, has no side effects, and reaches the public web through ddgs -> read-only, idempotent, open-world.
# The human-readable title goes in both Tool.title and annotations.title (older clients read the latter).
def _search_hints(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                           openWorldHint=True)


def _out(obj: dict[str, Any]) -> str:
    """One compact JSON text block: no escaped non-ASCII, no spare whitespace (tokens matter for a 12B model)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _refused(reason: str, advice: str, **extra: Any) -> str:
    """A caller-fixable input decision — a normal response, NOT an exception (raising it would make a correct
    block, a false block and a crash all look like the same opaque "Error executing tool")."""
    return _out({"status": "refused", "reason": reason, "advice": advice, **extra})


def _error(kind: str, detail: str, **extra: Any) -> str:
    """A genuine fault (backend down / blocked / timed out) — rephrasing the query won't fix it."""
    return _out({"status": "error", "kind": kind, "detail": detail, **extra})


def build_server(settings: Settings, search: SearchFn | None = None) -> MCPServer:
    """`search` defaults to the ddgs seam; tests inject a fake (no network)."""
    mcp = MCPServer(name="search", instructions=INSTRUCTIONS)

    @mcp.tool(title="Web search", annotations=_search_hints("Web search"), structured_output=False)
    async def web_search(query: str, max_results: int = DEFAULT_MAX_RESULTS, region: str | None = None) -> str:
        """Search the web for CURRENT information: news, current events, anything recent/latest/today/this week,
        weather, prices, time-sensitive facts. Do NOT use for general knowledge like recipes, definitions, or
        history — those have better tools. Returns snippets (title, url, snippet), not full pages.

        Args:
            query: what to search for, sent to the search engines exactly as written.
            max_results: how many results (default 10, allowed 1-25).
            region: optional region code `country-lang`, e.g. "us-en", "es-es", "de-de"; default "wt-wt" (no region).
        """
        lo, hi = MAX_RESULTS_RANGE
        resolved_region = region if region is not None else DEFAULT_REGION
        params = {"query": query, "backend": backend.BACKEND, "region": resolved_region,
                  "safesearch": backend.SAFESEARCH, "max_results": max_results}
        if not query or not query.strip():
            return _refused("query is empty", "pass the words to search for", **params)
        if not lo <= max_results <= hi:
            return _refused(f"max_results {max_results} is outside {lo}-{hi}",
                            f"use a value from {lo} to {hi} (default {DEFAULT_MAX_RESULTS})", **params)
        if not REGION_RE.fullmatch(resolved_region):
            return _refused(f"region {resolved_region!r} is not a `country-lang` code",
                            'use e.g. "us-en", "es-es", "de-de", or omit it for "wt-wt" (no region)', **params)
        fn = search or backend.search_backend
        # Retry server-side: a failed ddgs search usually means the engines it happened to hit blocked us this
        # time, and a second call (fresh engine shuffle) often succeeds — but a model told "error" won't retry.
        # Both failure kinds are retried: from ddgs, "backend" is mostly the same no-engine-answered condition
        # with the last engine's exception attached. A refusal never reaches here, so it's never retried.
        for attempt in range(1, settings.attempts + 1):
            try:
                # ddgs is synchronous (it fans out on its own thread pool) — keep it off the event loop.
                results = await asyncio.to_thread(fn, query, max_results, resolved_region, settings.timeout)
            except Exception as e:  # noqa: BLE001 - any backend failure (incl. EmptyOrBlocked) is retried, then surfaced
                failure = e
            else:
                return _out({"status": "ok", **params, "attempts": attempt, "count": len(results),
                             "results": results})
            log.warning("web_search attempt %d/%d failed: %s: %s", attempt, settings.attempts,
                        type(failure).__name__, failure)
        if isinstance(failure, backend.EmptyOrBlocked):
            return _error("empty_or_blocked",
                          "no search engine returned any result, even after retrying. The backend can't tell a "
                          "query with no matches from engines blocking or rate-limiting this server, so it is "
                          "reported, not hidden as an empty list. A very obscure query may do better reworded; "
                          "otherwise treat web search as unavailable right now.", **params, attempts=attempt)
        return _error("backend", f"{type(failure).__name__}: {failure}", **params, attempts=attempt)

    return mcp


# --- streamable-http bearer gate -------------------------


class BearerAuth:
    """ASGI gate for streamable-http:
      * /healthz        — always open (Docker healthcheck).
      * everything else — the MCP app, requiring `Authorization: Bearer <token>` when a token is set.
    """

    def __init__(self, app, settings: Settings) -> None:
        self.app = app
        self.expected = f"Bearer {settings.http_token}".encode() if settings.http_token else None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            if scope["path"] == "/healthz":
                await _respond(send, 200, b"ok")
                return
            if self.expected is not None:
                got = dict(scope["headers"]).get(b"authorization", b"")
                if not hmac.compare_digest(got, self.expected):
                    await _respond(send, 401, b"unauthorized")
                    return
        await self.app(scope, receive, send)


async def _respond(send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
