"""The search backend seam: ONE function between the tool and the search provider.

Two jobs: (1) the swap path — a sanctioned API (Brave / Mojeek) or a self-hosted SearXNG drops in behind the
same signature as a one-file edit; (2) the test seam — ddgs isn't httpx-injectable, so tests replace
`search_backend` with canned rows instead of using httpx.MockTransport.

Contract: return [{title, url, snippet}] (possibly empty = "the web has nothing"); RAISE on a backend fault.
The server maps a raise to status:"error" — never let a fault come back as an empty list.
"""

from __future__ import annotations

import random
from typing import Any

from ddgs import DDGS
from ddgs.engines import ENGINES
from ddgs.exceptions import DDGSException

# ddgs's "auto" with the encyclopedia engines removed. Plain "auto" PINS wikipedia + grokipedia to the front of
# every text search: encyclopedic hits are the wrong answer for this current-events tier (openzim-mcp is the deep
# tier), and with region "wt-wt" the wikipedia engine always fails anyway (it builds wt.wikipedia.org). An
# explicit list isn't shuffled by ddgs, so we shuffle per call like "auto" does, to spread load across engines.
BACKEND = "auto-web"        # echoed label: every ddgs text engine except EXCLUDED_ENGINES
EXCLUDED_ENGINES = frozenset({"wikipedia", "grokipedia"})
SAFESEARCH = "moderate"     # fixed: this serves a family door; not a caller knob


def web_engines() -> str:
    """The ddgs text engines minus the encyclopedias, shuffled — as a ddgs `backend` string."""
    keys = [k for k in ENGINES["text"] if k not in EXCLUDED_ENGINES]
    random.shuffle(keys)
    return ",".join(keys)

# ⚠ ddgs cannot report a clean empty search. It raises `DDGSException("No results found.")` when no engine
# produced a row and none raised — but its engines treat ANY non-200 response (403 block, 429 rate limit, captcha
# page) as "no results" (ddgs/base.py `request` returns None), so "every engine is blocking this server" raises
# the very same exception as "the web has nothing". Mapping it to [] would be the silent-empty bug this server
# exists to fix, so it becomes EmptyOrBlocked (-> status:"error", kind:"empty_or_blocked"). A future backend that
# CAN tell the difference (a sanctioned API) returns [] for a genuine empty.
_NO_RESULTS = "No results found."


class EmptyOrBlocked(Exception):
    """No engine returned a result, and ddgs can't say whether the web had nothing or the engines refused us."""


def search_backend(query: str, max_results: int, region: str, timeout: int) -> list[dict[str, str]]:
    """Return [{title, url, snippet}]. Raises on backend failure (-> caller maps to status:error); raises
    EmptyOrBlocked when ddgs found nothing, since it can't tell an empty web from blocked engines."""
    try:
        with DDGS(timeout=timeout) as d:
            rows: list[dict[str, Any]] = d.text(query, region=region, safesearch=SAFESEARCH,
                                                 max_results=max_results, backend=web_engines())
    except DDGSException as e:
        if type(e) is DDGSException and str(e) == _NO_RESULTS:
            raise EmptyOrBlocked(_NO_RESULTS) from e
        raise
    return [{"title": r.get("title") or "", "url": r.get("href") or "", "snippet": r.get("body") or ""}
            for r in rows]
