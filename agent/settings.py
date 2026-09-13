"""Ports and service URLs, resolved once from the environment.

A leaf module: it imports nothing local, so every other module — including
the subpackages under browser/ and computer/ — can pull from it without a
cycle.

This exists because the gateway URL used to be hardcoded in nine places, so
setting the port env var moved the server but left every client still dialling
8109. Import from here instead of writing a literal.
"""

from __future__ import annotations

import os


def _int_env(*names: str, default: int) -> int:
    """First env var that is set and parses as an int, else `default`.

    Tolerates a malformed value rather than refusing to boot — a typo'd port
    should not take the whole agent down.
    """
    for n in names:
        raw = os.getenv(n)
        if raw:
            try:
                return int(raw)
            except ValueError:
                print(f"[settings] ignoring non-numeric {n}={raw!r}")
    return default


# The LLM gateway. GATEWAY_V9_PORT / LLM_GATEWAY_V9_URL are the pre-rename
# names, still honoured so an existing .env keeps working.
GATEWAY_PORT = _int_env("GATEWAY_PORT", "GATEWAY_V9_PORT", default=8109)
GATEWAY_URL = (
    os.getenv("LLM_GATEWAY_URL")
    or os.getenv("LLM_GATEWAY_V9_URL")
    or f"http://localhost:{GATEWAY_PORT}"
).rstrip("/")

# The console API — serves the built frontend and the SSE run stream.
API_PORT = _int_env("AGENT_API_PORT", default=8110)

__all__ = ["GATEWAY_PORT", "GATEWAY_URL", "API_PORT"]
