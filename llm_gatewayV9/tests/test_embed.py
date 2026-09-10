"""V7 embed-endpoint tests. Run from llm_gatewayV7/:  uv run pytest -v tests/test_embed.py

Markers:
  - local:   requires `ollama` running locally with `nomic-embed-text` pulled
  - network: requires GEMINI_API_KEY in ../.env and outbound HTTPS

The tests start an in-process httpx ASGI client against the V7 FastAPI app —
they do NOT require V7 to be running on port 8107. This keeps the test suite
fast (~5s for the four tests) and self-contained.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from dotenv import load_dotenv

# Add parent dir to path so `import main` finds V7's modules.
HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))
load_dotenv(HERE.parent / ".env")  # same .env as V3

EXPECTED_OLLAMA_DIM = 768  # nomic-embed-text
EXPECTED_FALLBACK_DIM = 768  # gemini-embedding-001 with outputDimensionality=768


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def client():
    """In-process ASGI client. Manually drives FastAPI's lifespan so
    app.state.embedders is wired before any test sends a request."""
    import main as M
    transport = httpx.ASGITransport(app=M.app)
    async with M.app.router.lifespan_context(M.app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            timeout=60,
        ) as c:
            yield c


@pytest.mark.local
@pytest.mark.asyncio
async def test_ollama_embed(client):
    """Hits the live Ollama endpoint; asserts shape and dim = 768."""
    r = await client.post("/v1/embed", json={
        "text": "the quick brown fox",
        "task_type": "retrieval_document",
        "provider": "ollama",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    print("ollama:", {k: v for k, v in d.items() if k != "embedding"}, "vec[0:3]:", d["embedding"][:3])
    assert d["provider"] == "ollama"
    assert d["model"]
    assert d["dim"] == EXPECTED_OLLAMA_DIM
    assert isinstance(d["embedding"], list) and len(d["embedding"]) == EXPECTED_OLLAMA_DIM
    assert all(isinstance(x, (int, float)) for x in d["embedding"][:5])


@pytest.mark.network
@pytest.mark.asyncio
async def test_fallback_embed(client):
    """Hits Gemini gemini-embedding-001; asserts shape and dim > 0 (stable)."""
    if not os.getenv("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    r = await client.post("/v1/embed", json={
        "text": "the quick brown fox",
        "task_type": "retrieval_document",
        "provider": "gemini",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    print("gemini:", {k: v for k, v in d.items() if k != "embedding"}, "vec[0:3]:", d["embedding"][:3])
    assert d["provider"] == "gemini"
    assert d["model"]
    assert d["dim"] == EXPECTED_FALLBACK_DIM > 0
    assert isinstance(d["embedding"], list) and len(d["embedding"]) == d["dim"]


@pytest.mark.network
@pytest.mark.asyncio
async def test_failover(client, monkeypatch):
    """Point Ollama at an unused port → ring should fall over to Gemini."""
    if not os.getenv("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    # Rebuild embedders with a broken Ollama URL, install onto app state.
    import main as M
    import embedders as E
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:1")  # unbound
    broken_embedders, order = E.build_embedders()
    monkeypatch.setattr(M.app.state, "embedders", broken_embedders)
    monkeypatch.setattr(M.app.state, "embed_order", order)

    r = await client.post("/v1/embed", json={
        "text": "fall over please",
        "task_type": "retrieval_query",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    print("failover:", {k: v for k, v in d.items() if k != "embedding"})
    assert d["provider"] == "gemini"
    assert len(d["attempted"]) >= 1 and d["attempted"][0]["provider"] == "ollama"


@pytest.mark.local
@pytest.mark.asyncio
async def test_provider_explicit(client):
    """Pinned provider should appear in the response provider field."""
    r = await client.post("/v1/embed", json={
        "text": "hello world",
        "provider": "ollama",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    print("explicit:", {k: v for k, v in d.items() if k != "embedding"})
    assert d["provider"] == "ollama"
    assert d["dim"] == EXPECTED_OLLAMA_DIM
