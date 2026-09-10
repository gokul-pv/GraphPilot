"""Framework-free client for llm_gatewayV9.

Plain httpx — no LangChain, no provider SDKs. The shipped Browser skill
talks to the gateway over HTTP, the same way every other S-session skill
does. Provider rotation, retries, agent tagging are the gateway's job.

Two methods: `vision()` hits /v1/vision for Layer-3 set-of-marks calls,
`chat()` hits /v1/chat for Layer-2b a11y-text calls (no image, cheaper,
doesn't require a vision-capable provider). `cost_by_agent()` queries the
gateway's V8 ledger so tests can pull real numbers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx


@dataclass
class GatewayResult:
    """Normalised reply from either /v1/vision or /v1/chat."""
    parsed: dict | None
    text: str
    provider: str
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int


# Back-compat alias — the early SoM driver imports `VisionResult`.
VisionResult = GatewayResult


class V9Client:
    """One client, two methods: vision() and chat(). Both speak to V9."""
    def __init__(
        self,
        base_url: str = "http://localhost:8109",
        agent: str = "s9_browser",
        timeout: float = 120.0,
        session: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.agent = agent
        self.timeout = timeout
        # Default session tag for ledger attribution. Per-call overrides win.
        self.session = session

    @staticmethod
    def _normalise(d: dict) -> GatewayResult:
        return GatewayResult(
            parsed=d.get("parsed"),
            text=d.get("text") or "",
            provider=d.get("provider", ""),
            model=d.get("model", ""),
            latency_ms=int(d.get("latency_ms") or 0),
            input_tokens=int(d.get("input_tokens") or 0),
            output_tokens=int(d.get("output_tokens") or 0),
        )

    async def vision(
        self,
        image_data_url: str,
        prompt: str,
        *,
        schema: Optional[dict] = None,
        schema_name: str = "out",
        system: Optional[str] = None,
        max_tokens: int = 1024,
        session: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> GatewayResult:
        body: dict[str, Any] = {
            "image": image_data_url,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "agent": self.agent,
        }
        if schema:        body["schema"] = schema
        if schema:        body["schema_name"] = schema_name
        if system:        body["system"] = system
        s = session or self.session
        if s:             body["session"] = s
        if model:         body["model"] = model
        if provider:      body["provider"] = provider

        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base_url}/v1/vision", json=body)
            r.raise_for_status()
            return self._normalise(r.json())

    async def chat(
        self,
        prompt: str,
        *,
        schema: Optional[dict] = None,
        schema_name: str = "out",
        system: Optional[str] = None,
        max_tokens: int = 1024,
        session: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> GatewayResult:
        """Plain text-only call. Used by the Layer-2b a11y driver: legend +
        goal in, action JSON out. Skipping the image cuts ~1K input tokens
        per turn vs vision()."""
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "agent": self.agent,
        }
        if schema:
            body["response_format"] = {
                "type": "json_schema", "schema": schema,
                "name": schema_name, "strict": True,
            }
        if system:    body["system"] = system
        s = session or self.session
        if s:         body["session"] = s
        if model:     body["model"] = model
        if provider:  body["provider"] = provider

        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base_url}/v1/chat", json=body)
            r.raise_for_status()
            return self._normalise(r.json())

    async def cost_by_agent(self, agent: Optional[str] = None,
                            session: Optional[str] = None) -> dict:
        """Pull the V9 ledger for this agent/session — tests use it to
        report real numbers rather than wall-clock estimates."""
        params: dict[str, Any] = {}
        if agent:   params["agent"] = agent
        if session: params["session"] = session
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.base_url}/v1/cost/by_agent", params=params)
            r.raise_for_status()
            return r.json()


# Back-compat alias.
V9VisionClient = V9Client
