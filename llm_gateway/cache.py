"""Gemini prompt cache: SHA-256-keyed reuse of cached system content.

Implicit prefix caching on OpenAI-compat providers does not need a module —
the gateway just keeps system byte-stable across calls and the upstream takes
care of the rest. So this module is Gemini-only.
"""
from __future__ import annotations
import time, hashlib, asyncio, httpx
from typing import Optional


class GeminiCache:
    """Maps SHA-256(system_text) -> (cache_resource_name, expires_at)."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(model: str, text: str) -> str:
        h = hashlib.sha256()
        h.update(model.encode())
        h.update(b"\x00")
        h.update(text.encode())
        return h.hexdigest()

    async def get_or_create(self, api_key: str, model: str, text: str, base_url: str) -> tuple[Optional[str], int]:
        """Returns (cache_resource_name|None, cache_creation_input_tokens).
        cache_creation_input_tokens is non-zero only when we mint a fresh entry.
        """
        key = self._key(model, text)
        now = time.time()
        async with self._lock:
            if key in self._store:
                name, exp = self._store[key]
                if exp > now + 5:
                    return name, 0
                self._store.pop(key, None)

        # Mint a new cached content. Gemini requires the content list.
        url = f"{base_url}/cachedContents?key={api_key}"
        body = {
            "model": f"models/{model}",
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "ttl": f"{self.ttl}s",
        }
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(url, json=body)
                if r.status_code != 200:
                    return None, 0
                d = r.json()
                name = d.get("name")  # "cachedContents/<id>"
                usage = d.get("usageMetadata") or {}
                tokens = usage.get("totalTokenCount", 0) or len(text) // 4
                if not name:
                    return None, 0
                async with self._lock:
                    self._store[key] = (name, now + self.ttl)
                return name, tokens
        except Exception:
            return None, 0
