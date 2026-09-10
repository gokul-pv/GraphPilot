"""Tool-use loop wrapper around the gateway + MCP server.

When a Session 8 skill declares `tools_allowed: [...]` in agent_config.yaml,
its dispatch goes through `run_with_tools` (below) rather than a single
chat call. The wrapper drives the conversation until the model stops
asking for tool_calls and emits text:

    1. chat(messages, tools=schemas)
    2. if reply.tool_calls is non-empty:
         for each tc: dispatch via MCP, append a `role="tool"` message
         append assistant message with tool_calls
         go to 1
       else:
         return reply.text

The MCP server is the same `mcp_server.py` carried over from S7. We open
one stdio session per skill invocation (the spawn cost is ~100ms and the
session lives only for the lifetime of one node — keeping it short means
no shared mutable state between skills).

This file is small on purpose. If the cost of a per-skill subprocess
becomes the bottleneck, the right fix is a shared session at the
Executor level, not a more clever client here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from gateway import LLM

MCP_SERVER = Path(__file__).parent / "mcp_server.py"
MAX_TOOL_HOPS = 6  # hard cap so a model that loves tool-use can't cost a fortune


async def _dispatch_tool(session: ClientSession, name: str, args: dict) -> str:
    """Run one MCP tool call and return its result as one text blob."""
    try:
        result = await session.call_tool(name, arguments=args)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
    parts: list[str] = []
    for c in (getattr(result, "content", None) or []):
        t = getattr(c, "text", None)
        parts.append(t if t is not None else str(c))
    return "\n".join(parts) if parts else ""


async def run_with_tools(*, prompt: str, tools_payload: list[dict],
                         agent: str, session_id: str,
                         provider_pin: str | None = None,
                         max_tokens: int = 2048,
                         temperature: float = 0.3) -> dict:
    """Multi-turn chat: dispatch tool_calls via MCP, keep going until the
    model returns text. Returns the FINAL gateway reply dict (so callers
    can read `text`, `provider`, etc. the same way they would for a
    one-shot call)."""
    messages: list[dict] = [{"role": "user", "content": prompt}]
    last_reply: dict = {}

    server_params = StdioServerParameters(command=sys.executable, args=[str(MCP_SERVER)])
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            for _ in range(MAX_TOOL_HOPS + 1):
                reply = await _chat(messages=messages, tools=tools_payload,
                                    agent=agent, session_id=session_id,
                                    provider_pin=provider_pin,
                                    max_tokens=max_tokens, temperature=temperature)
                last_reply = reply
                tool_calls = reply.get("tool_calls") or []
                if not tool_calls:
                    return reply
                # Carry the assistant's tool-call turn back through.
                messages.append({
                    "role": "assistant",
                    "content": reply.get("text", "") or "",
                    "tool_calls": tool_calls,
                })
                for tc in tool_calls:
                    result_text = await _dispatch_tool(mcp, tc["name"],
                                                      tc.get("arguments") or {})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result_text[:8_000],  # cap per-tool reply
                    })
    # Hit the hop cap. Return whatever the gateway last said.
    return last_reply


async def _chat(*, messages, tools, agent, session_id, provider_pin,
                max_tokens, temperature) -> dict:
    import asyncio as _a
    return await _a.to_thread(
        LLM().chat,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        agent=agent,
        session=session_id,
        provider=provider_pin,
        max_tokens=max_tokens,
        temperature=temperature,
    )
