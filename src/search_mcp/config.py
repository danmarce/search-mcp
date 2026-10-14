"""Runtime settings, all from environment variables (the only secret is the optional HTTP bearer)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_MAX_RESULTS = 10          # matches the proven OpenWebUI DuckDuckGo tool
MAX_RESULTS_RANGE = (1, 25)       # out of range -> status:"refused", not a crash
DEFAULT_REGION = "wt-wt"          # ddgs "no region"


@dataclass(frozen=True)
class Settings:
    # Per-search backend timeout in seconds (ddgs waits this long on each batch of engines).
    timeout: int = field(default_factory=lambda: int(os.environ.get("SEARCH_TIMEOUT", "10")))
    # Bearer token required on streamable-http when set (sent as an Authorization header).
    http_token: str | None = field(default_factory=lambda: os.environ.get("SEARCH_MCP_TOKEN") or None)
