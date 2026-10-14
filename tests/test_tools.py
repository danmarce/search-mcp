"""Tool-level: call web_search by name through the server against a fake backend (no network, no live ddgs).

Pins the declared metadata in the wire JSON, the ok/refused/error contract as a caller actually sees it, the
href->url / body->snippet mapping, and the seam's empty-vs-fault translation of ddgs's exceptions.
"""

import asyncio
import json

import pytest
from ddgs.engines import ENGINES
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from search_mcp import backend
from search_mcp.config import Settings
from search_mcp.server import build_server

ROWS = [
    {"title": "Quake hits coast", "url": "https://example.org/a", "snippet": "A magnitude 6 quake…"},
    {"title": "Aftershocks", "url": "https://example.org/b", "snippet": "Residents report…"},
]


class FakeSearch:
    """Stands in for backend.search_backend; records every call."""

    def __init__(self, rows=None, exc: Exception | None = None, script: list | None = None) -> None:
        self.rows, self.exc = rows if rows is not None else ROWS, exc
        self.script = list(script or [])  # per-call outcomes (rows or an exception) before falling back to rows/exc
        self.calls: list[tuple] = []

    def __call__(self, query, max_results, region, timeout):
        self.calls.append((query, max_results, region, timeout))
        outcome = self.script.pop(0) if self.script else (self.exc or self.rows)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _call(fake: FakeSearch, args: dict) -> dict:
    res = asyncio.run(build_server(Settings(), search=fake).call_tool("web_search", args))
    texts = [c for c in res.content if c.type == "text"]
    return json.loads(texts[-1].text)


def test_every_tool_declares_title_and_all_four_hints():
    tools = asyncio.run(build_server(Settings()).list_tools())
    assert {t.name for t in tools} == {"web_search"}  # exactly one tool — see CLAUDE.md before adding another
    for t in tools:
        wire = t.annotations.model_dump(by_alias=True)  # the camelCase JSON a client/directory actually reads
        for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert isinstance(wire.get(hint), bool), f"{t.name}.{hint} unset"
        assert t.title and wire.get("title") == t.title, f"{t.name} missing title"
    wire = tools[0].annotations.model_dump(by_alias=True)
    assert tools[0].title == "Web search"
    assert wire["readOnlyHint"] and not wire["destructiveHint"] and wire["idempotentHint"] and wire["openWorldHint"]


def test_ok_sends_query_verbatim_and_echoes_params():
    fake = FakeSearch()
    query = '  "Cotopaxi" eruption   latest  site:example.org '
    d = _call(fake, {"query": query, "max_results": 5, "region": "es-es"})
    assert d["status"] == "ok" and d["count"] == 2 and d["results"] == ROWS
    assert fake.calls[0][:3] == (query, 5, "es-es")  # verbatim — not stripped, not rewritten
    assert (d["query"], d["region"], d["max_results"], d["backend"]) == (query, "es-es", 5, "auto-web")


def test_defaults_are_resolved_and_echoed():
    fake = FakeSearch()
    d = _call(fake, {"query": "weather quito today"})
    assert fake.calls[0][1:3] == (10, "wt-wt")
    assert (d["max_results"], d["region"], d["safesearch"]) == (10, "wt-wt", "moderate")


def test_empty_results_are_ok_not_error():
    # the seam contract: a backend that CAN tell a genuine empty (not ddgs) returns [] -> ok, not error
    d = _call(FakeSearch(rows=[]), {"query": "xqzvbnm nothing matches"})
    assert d["status"] == "ok" and d["results"] == [] and d["count"] == 0


def test_backend_failure_is_error_not_empty():
    fake = FakeSearch(exc=RatelimitException("202 Ratelimit"))
    d = _call(fake, {"query": "anything"})
    assert d["status"] == "error" and d["kind"] == "backend"
    assert "Ratelimit" in d["detail"] and "results" not in d
    assert d["attempts"] == 2 and len(fake.calls) == 2      # retried before giving up


@pytest.mark.parametrize("first_failure", [
    RuntimeError("DDGSException: Connection reset by peer"),   # the real-world case: DDG reset, others blocked
    backend.EmptyOrBlocked("No results found."),
])
def test_one_failed_attempt_is_retried_transparently(first_failure):
    fake = FakeSearch(script=[first_failure])
    d = _call(fake, {"query": "USD to EUR exchange rate today", "max_results": 5})
    assert d["status"] == "ok" and d["attempts"] == 2 and d["results"] == ROWS
    assert fake.calls[0] == fake.calls[1]                     # same verbatim args each attempt


def test_success_and_genuine_empty_are_not_retried():
    for rows in (ROWS, []):
        fake = FakeSearch(rows=rows)
        d = _call(fake, {"query": "q"})
        assert d["status"] == "ok" and d["attempts"] == 1 and len(fake.calls) == 1


def test_last_failure_kind_is_reported():
    fake = FakeSearch(script=[RuntimeError("reset"), backend.EmptyOrBlocked("No results found.")])
    d = _call(fake, {"query": "q"})
    assert d["kind"] == "empty_or_blocked" and d["attempts"] == 2


def test_attempts_is_configurable(monkeypatch):
    monkeypatch.setenv("SEARCH_ATTEMPTS", "1")
    fake = FakeSearch(exc=RuntimeError("down"))
    d = _call(fake, {"query": "q"})
    assert d["status"] == "error" and d["attempts"] == 1 and len(fake.calls) == 1


@pytest.mark.parametrize("args", [
    {"query": ""},
    {"query": "   \n\t"},
    {"query": "x", "max_results": 0},
    {"query": "x", "max_results": 26},
    {"query": "x", "region": "us"},          # not country-lang: would crash every ddgs engine
    {"query": "x", "region": "en_US"},
])
def test_bad_input_is_refused_not_raised_and_backend_untouched(args):
    fake = FakeSearch()
    d = _call(fake, args)
    assert d["status"] == "refused" and d["reason"] and d["advice"]
    assert fake.calls == []


# --- the seam itself: ddgs raises on empty; only that exact case may become [] ---


class FakeDDGS:
    def __init__(self, rows=None, exc=None):
        self.rows, self.exc, self.kw = rows, exc, None

    def __call__(self, timeout):  # stands in for the DDGS class
        self.timeout = timeout
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def text(self, query, **kw):
        self.kw = {"query": query, **kw}
        if self.exc:
            raise self.exc
        return self.rows


def test_seam_maps_ddgs_rows_and_passes_params(monkeypatch):
    fake = FakeDDGS(rows=[{"title": "T", "href": "https://x.test/", "body": "B"}, {"title": "no href"}])
    monkeypatch.setattr(backend, "DDGS", fake)
    out = backend.search_backend("q  verbatim", 7, "us-en", 10)
    assert out == [{"title": "T", "url": "https://x.test/", "snippet": "B"},
                   {"title": "no href", "url": "", "snippet": ""}]
    engines = fake.kw.pop("backend").split(",")
    assert fake.kw == {"query": "q  verbatim", "region": "us-en", "safesearch": "moderate", "max_results": 7}
    assert fake.timeout == 10
    # "auto" minus the encyclopedias: every other ddgs text engine, each once, no wikipedia/grokipedia
    assert sorted(engines) == sorted(k for k in ENGINES["text"] if k not in {"wikipedia", "grokipedia"})
    assert len(engines) >= 3 and "auto" not in engines


def test_seam_never_turns_ddgs_no_results_into_empty_list(monkeypatch):
    # ddgs counts a 403/429/captcha as "no results", so its "No results found." may mean "every engine blocked
    # us" — it must surface as EmptyOrBlocked, never as [] (the silent-empty bug).
    monkeypatch.setattr(backend, "DDGS", FakeDDGS(exc=DDGSException("No results found.")))
    with pytest.raises(backend.EmptyOrBlocked):
        backend.search_backend("q", 10, "wt-wt", 10)


def test_empty_or_blocked_is_error_with_its_own_kind():
    d = _call(FakeSearch(exc=backend.EmptyOrBlocked("No results found.")), {"query": "anything"})
    assert d["status"] == "error" and d["kind"] == "empty_or_blocked" and d["detail"]
    assert "results" not in d and d["query"] == "anything"


@pytest.mark.parametrize("exc", [
    DDGSException("ConnectError: engine unreachable"),   # engines failed -> same base type, different message
    TimeoutException("No results found."),               # subclass, even with the same message
    RatelimitException("202 Ratelimit"),
    OSError("network down"),
])
def test_seam_propagates_real_failures(monkeypatch, exc):
    monkeypatch.setattr(backend, "DDGS", FakeDDGS(exc=exc))
    with pytest.raises(type(exc)):
        backend.search_backend("q", 10, "wt-wt", 10)
