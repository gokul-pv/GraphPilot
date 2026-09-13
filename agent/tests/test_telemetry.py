"""Per-node telemetry capture.

The gateway returns model, token counts, cache reads and latency on every
reply. Until the console was built, skills.py read only `provider` off that
reply and discarded the rest, so every node written to disk recorded
`cost: 0.0` and no tokens — which made a node inspector impossible.

Two things are easy to get wrong here and are pinned below:

  1. A tool-using skill loops chat↔tool inside mcp_runner. The final reply's
     counters describe only the LAST hop, so a researcher that ran three
     searches would under-report by most of its spend. mcp_runner sums across
     hops into `_usage`, and skills.py must prefer that over the top-level
     fields.

  2. Sessions written before these fields existed must still load. Pydantic
     defaults cover it, but persistence raises SessionLoadError on a failed
     model_validate, so a regression here breaks the history browser rather
     than degrading it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas import AgentResult


def _telemetry(reply: dict) -> dict:
    """Mirror of the helper inside skills.run_skill.

    run_skill defines it as a closure over one dispatch, so it cannot be
    imported. Importing skills at module scope would also drag in gateway.py,
    which spawns the gateway subprocess. The logic under test is small enough
    that duplicating it here is the lesser evil; if it grows, lift it to a
    module-level function in skills.py and import it.
    """
    from skills import estimate_cost

    usage = reply.get("_usage") or {
        "input_tokens": reply.get("input_tokens") or 0,
        "output_tokens": reply.get("output_tokens") or 0,
        "cache_read_tokens": reply.get("cache_read_input_tokens") or 0,
        "latency_ms": reply.get("latency_ms") or 0,
        "llm_calls": 1,
    }
    provider = reply.get("provider", "")
    return {
        "provider": provider,
        "model": reply.get("model", "") or "",
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_read_tokens": usage["cache_read_tokens"],
        "latency_ms": usage["latency_ms"],
        "llm_calls": usage["llm_calls"],
        "tool_calls": reply.get("_tool_trace") or [],
        "cost": estimate_cost(provider, usage["input_tokens"],
                              usage["output_tokens"]),
    }


# ── plain (non-tool) skill ───────────────────────────────────────────────────

def test_plain_reply_lifts_every_counter() -> None:
    t = _telemetry({
        "provider": "groq",
        "model": "openai/gpt-oss-120b",
        "input_tokens": 1204,
        "output_tokens": 87,
        "cache_read_input_tokens": 512,
        "latency_ms": 830,
        "text": "{}",
    })
    assert t["provider"] == "groq"
    assert t["model"] == "openai/gpt-oss-120b"
    assert t["input_tokens"] == 1204
    assert t["output_tokens"] == 87
    # The gateway names this cache_read_input_tokens; AgentResult shortens it.
    assert t["cache_read_tokens"] == 512
    assert t["latency_ms"] == 830
    assert t["llm_calls"] == 1
    assert t["tool_calls"] == []


def test_groq_is_priced_from_the_gateway_table() -> None:
    # groq is 0.15 in / 0.75 out per 1M tokens in llm_gateway/pricing.py.
    t = _telemetry({"provider": "groq", "input_tokens": 1_000_000,
                    "output_tokens": 1_000_000})
    assert t["cost"] == 0.9


def test_free_provider_costs_nothing() -> None:
    # gemini is priced at 0.00/0.00, so zero here is correct, not missing.
    t = _telemetry({"provider": "gemini", "input_tokens": 50_000,
                    "output_tokens": 9_000})
    assert t["cost"] == 0.0


def test_unknown_provider_does_not_raise() -> None:
    assert _telemetry({"provider": "nonesuch", "input_tokens": 10,
                       "output_tokens": 10})["cost"] == 0.0


def test_missing_counters_default_to_zero() -> None:
    t = _telemetry({"provider": "gemini"})
    assert (t["input_tokens"], t["output_tokens"], t["latency_ms"]) == (0, 0, 0)
    assert t["model"] == ""


# ── tool-using skill ─────────────────────────────────────────────────────────

def test_usage_sums_across_hops_not_just_the_last() -> None:
    """The regression that motivated this: a researcher's search tokens.

    Three hops of 900 in / 40 out, and the final hop alone reports 300/120.
    Reading the top-level fields would report 300 input tokens for a node
    that actually spent 3000.
    """
    reply = {
        "provider": "gemini", "model": "gemini-2.5-flash",
        "input_tokens": 300, "output_tokens": 120, "latency_ms": 400,
        "_usage": {"input_tokens": 3000, "output_tokens": 200,
                   "cache_read_tokens": 0, "latency_ms": 5200, "llm_calls": 3},
        "_tool_trace": [
            {"name": "web_search", "arguments": {"query": "Mumbai population"},
             "result_preview": "...", "result_chars": 4200, "elapsed_s": 1.8},
            {"name": "fetch_url", "arguments": {"url": "https://example.org"},
             "result_preview": "...", "result_chars": 9100, "elapsed_s": 2.4},
        ],
    }
    t = _telemetry(reply)
    assert t["input_tokens"] == 3000
    assert t["output_tokens"] == 200
    assert t["latency_ms"] == 5200
    assert t["llm_calls"] == 3
    assert [c["name"] for c in t["tool_calls"]] == ["web_search", "fetch_url"]
    assert t["tool_calls"][0]["arguments"]["query"] == "Mumbai population"


def test_tool_trace_survives_onto_agent_result() -> None:
    r = AgentResult(success=True, agent_name="researcher",
                    **_telemetry({
                        "provider": "gemini",
                        "_usage": {"input_tokens": 10, "output_tokens": 2,
                                   "cache_read_tokens": 0, "latency_ms": 30,
                                   "llm_calls": 2},
                        "_tool_trace": [{"name": "web_search", "arguments": {},
                                         "result_preview": "", "result_chars": 0,
                                         "elapsed_s": 0.1}],
                    }))
    round_tripped = AgentResult.model_validate(r.model_dump())
    assert round_tripped.tool_calls[0]["name"] == "web_search"
    assert round_tripped.llm_calls == 2


# ── backward compatibility ───────────────────────────────────────────────────

def test_pre_telemetry_result_still_validates() -> None:
    """Exactly the shape written to disk before these fields existed."""
    legacy = {
        "success": True, "agent_name": "researcher",
        "output": {"question": "q", "findings": "f"},
        "artifacts": [], "successors": [],
        "cost": 0.0, "elapsed_s": 36.17, "provider": "gemini",
        "error": None, "error_code": None,
    }
    r = AgentResult.model_validate(legacy)
    assert r.model == ""
    assert r.input_tokens == 0
    assert r.tool_calls == []
    assert r.llm_calls == 0
