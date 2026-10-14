"""The streamable-http bearer gate: /healthz always open, everything else needs the token when one is set."""

import asyncio

from search_mcp.config import Settings
from search_mcp.server import BearerAuth


async def _inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"mcp"})


def _status(token: str | None, path: str, auth: bytes | None = None) -> int:
    headers = [(b"authorization", auth)] if auth else []
    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    gate = BearerAuth(_inner, Settings(http_token=token))
    asyncio.run(gate({"type": "http", "path": path, "headers": headers}, None, send))
    return sent[0]["status"]


def test_healthz_is_open_even_with_token():
    assert _status("s3cret", "/healthz") == 200


def test_mcp_requires_the_bearer_when_set():
    assert _status("s3cret", "/mcp") == 401
    assert _status("s3cret", "/mcp", b"Bearer wrong") == 401
    assert _status("s3cret", "/mcp", b"Bearer s3cret") == 200


def test_no_token_means_open():
    assert _status(None, "/mcp") == 200
