"""Skill registry + per-skill execution.

The orchestrator (flow.py) treats every node as a `Skill` object loaded
from agent_config.yaml. There is no Python class per skill — that
abstraction would have to be added at the point where a skill needs
behaviour the orchestrator can't infer from the yaml. Today every skill
either calls the gateway or (for sandbox_executor) calls sandbox.py.

What lives here:
  - Skill / SkillRegistry
  - input resolution (`n:...`, `art:...`, `USER_QUERY`, literals)
  - prompt rendering (template + inputs + optional failure report)
  - JSON parsing of the model's reply (single top-level object)
  - the MCP tool schemas exposed to tool-using skills
  - `run_skill(...)` — the dispatcher
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import yaml
from pydantic import ValidationError

import artifacts as artifacts_svc
from gateway import LLM, estimate_cost
from schemas import AgentResult, NodeSpec

ROOT = Path(__file__).parent
AGENT_CONFIG_PATH = ROOT / "agent_config.yaml"


# ── catalogue ────────────────────────────────────────────────────────────────

class Skill:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.prompt_path = ROOT / cfg["prompt"]
        self.description = cfg.get("description", "")
        self.tools_allowed: list[str] = cfg.get("tools_allowed", []) or []
        self.internal_successors: list[str] = cfg.get("internal_successors", []) or []
        self.critic: bool = bool(cfg.get("critic", False))
        self.provider_pin: str | None = cfg.get("provider_pin")
        # P2 #10: per-skill temperature / max_tokens come from the yaml so
        # tuning a single skill no longer requires a code edit. Defaults
        # are deliberately conservative; a skill that wants exploration
        # (Researcher) bumps temperature; a skill that wants determinism
        # (Critic, Distiller) drops it to ~0.
        self.temperature: float = float(cfg.get("temperature", 0.3))
        self.max_tokens: int = int(cfg.get("max_tokens", 2048))

    def prompt_template(self) -> str:
        if not self.prompt_path.exists():
            return f"You are the {self.name} skill. (Prompt file missing.)"
        return self.prompt_path.read_text()


class SkillRegistry:
    def __init__(self):
        cfg = yaml.safe_load(AGENT_CONFIG_PATH.read_text())
        self._skills: dict[str, Skill] = {n: Skill(n, c) for n, c in cfg.items()}

    def get(self, name: str) -> Skill:
        if name not in self._skills:
            raise KeyError(f"unknown skill: {name}")
        return self._skills[name]

    def names(self) -> list[str]:
        return list(self._skills)


# ── input resolution + prompt rendering ──────────────────────────────────────

def resolve_inputs(node_inputs: list[str], graph_nodes, query: str) -> list[dict]:
    """Materialise each input id into a dict the prompt can serialise.

    Recognised input forms:
      - "USER_QUERY"  → the original user query text
      - "n:<i>"       → the AgentResult.output of that completed node
      - "art:<sha>"   → the bytes of an artifact, decoded as utf-8 best-effort
      - any other     → passed through as a free-form string

    `graph_nodes` is the nx node-view dict from flow.Graph; we read each
    upstream node's `result` attribute (set when the orchestrator marks
    the node complete).
    """
    out = []
    for inp in node_inputs:
        if inp == "USER_QUERY":
            out.append({"id": "USER_QUERY", "kind": "query", "value": query})
        elif inp.startswith("n:") and inp in graph_nodes:
            upstream = graph_nodes[inp].get("result")
            if isinstance(upstream, AgentResult):
                out.append({"id": inp, "kind": "upstream",
                            "skill": upstream.agent_name, "output": upstream.output})
            else:
                out.append({"id": inp, "kind": "upstream-missing", "output": None})
        elif inp.startswith("art:"):
            try:
                blob = artifacts_svc.get_bytes(inp)
                text = blob.decode("utf-8", errors="replace")
                out.append({"id": inp, "kind": "artifact", "text": text[:20_000]})
            except Exception as e:
                out.append({"id": inp, "kind": "artifact-missing", "error": str(e)})
        else:
            out.append({"id": inp, "kind": "literal", "value": inp})
    return out


def _format_memory_hits(hits: list) -> str:
    """Compact rendering of FAISS-ranked MemoryItem hits for the prompt.

    Each hit is shown as one line: kind, descriptor, source, plus a 400-char
    preview of `value.chunk` when present (indexed-document chunks) or of
    `value.raw` (classifier facts). The full chunk would blow the prompt,
    but the descriptor + preview is enough for the Planner to decide
    whether memory already covers the query and for downstream skills to
    synthesise from indexed material without an extra Retriever round-trip.
    """
    if not hits:
        return ""
    lines = []
    for h in hits[:8]:  # cap to keep the prompt bounded
        kind = getattr(h, "kind", "?")
        desc = (getattr(h, "descriptor", "") or "")[:200]
        source = getattr(h, "source", "")
        val = getattr(h, "value", {}) or {}
        chunk = val.get("chunk")
        raw = val.get("raw")
        line = f"  - [{kind}] {desc}"
        if source:
            line += f"\n      source: {source}"
        if isinstance(chunk, str) and chunk.strip():
            preview = chunk[:2000].replace("\n", " ")
            more = " …" if len(chunk) > 2000 else ""
            line += f"\n      chunk: {preview}{more}"
        elif isinstance(raw, str) and raw.strip():
            raw_more = " …" if len(raw) > 2000 else ""
            line += f"\n      raw: {raw[:2000]}{raw_more}"
        lines.append(line)
    return "\n".join(lines)


def render_prompt(skill: Skill, query: str, resolved: list[dict],
                  failure_report: str | None = None,
                  memory_hits: list | None = None,
                  question: str | None = None) -> str:
    parts = [skill.prompt_template().rstrip()]
    # USER_QUERY top-line: only when the Planner wired USER_QUERY into this
    # node's inputs. Earlier versions added it unconditionally, which
    # leaked the full original query into every fan-out worker — three
    # researcher siblings spawned to "find population of A / B / C" all
    # saw the same "compare A, B, C" query and each one ended up
    # searching for all three. Per-node scoping now travels through
    # `metadata.question` (rendered as QUESTION below) and the INPUTS
    # block; USER_QUERY is present only when the Planner asked for it.
    user_query_in_inputs = any(
        isinstance(r, dict) and r.get("id") == "USER_QUERY" for r in resolved
    )
    if user_query_in_inputs:
        parts += ["", f"USER_QUERY: {query}"]
    # QUESTION: the per-node sub-question the Planner attached via
    # `metadata.question`. This is how a fan-out worker learns *its*
    # slice of the user's request without seeing the whole query.
    if isinstance(question, str) and question.strip():
        parts += ["", f"QUESTION: {question.strip()}"]
    if failure_report:
        parts += ["", f"FAILURE:\n{failure_report}"]
    # Memory hits — FAISS-ranked MemoryItems from session-start memory.read.
    # Same hits flow into every skill's prompt this run (the contract:
    # every cognitive role can see what the agent already knows).
    hits_block = _format_memory_hits(memory_hits or [])
    if hits_block:
        parts += ["", f"MEMORY HITS ({len(memory_hits)} from FAISS):", hits_block]
    parts += ["", "INPUTS:", json.dumps(resolved, indent=2, default=str)[:20_000]]
    return "\n".join(parts)


def parse_skill_json(text: str) -> dict:
    """Skills return a single top-level JSON object. Strip markdown fences
    if the model added them despite being told not to."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[:-3]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                pass
    return {}


# ── MCP tool schemas exposed through the gateway tools= channel ──────────────

_TOOL_CATALOG = {
    "web_search": {
        "name": "web_search",
        "description": "Search the web (Tavily primary, DDG fallback). Hard-capped at 5 results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 3},
            },
            "required": ["query"],
        },
    },
    "fetch_url": {
        "name": "fetch_url",
        "description": "Fetch clean markdown from a URL via crawl4ai.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    "search_knowledge": {
        "name": "search_knowledge",
        "description": "Vector search over the agent's indexed knowledge base.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
}


def tool_payload(tool_names: list[str]) -> list[dict] | None:
    if not tool_names:
        return None
    return [_TOOL_CATALOG[n] for n in tool_names if n in _TOOL_CATALOG]


# ── per-node execution ───────────────────────────────────────────────────────

async def run_skill(skill: Skill, node_id: str, graph_nodes,
                    session_id: str, query: str,
                    failure_report: str | None,
                    *, memory_hits: list | None = None) -> tuple[AgentResult, str]:
    """Dispatch one node. Returns (result, rendered_prompt).

    `memory_hits` is the FAISS-ranked MemoryItem list captured once at
    session start by Executor.run and threaded through here so every
    skill's prompt can see the same hits. Memory works because the
    orchestrator delivers the
    hits, not just because the FAISS index is on disk.

    sandbox_executor bypasses the gateway: it picks the `code` field out of
    its upstream coder node and runs sandbox.run_python directly. All other
    skills are LLM-backed and route through the gateway with
    agent=<skill_name> so agent_routing.yaml + cost-by-agent kick in."""
    resolved = resolve_inputs(graph_nodes[node_id]["inputs"], graph_nodes, query)
    # Per-node sub-question from the Planner's `metadata.question`. Travels
    # into the rendered prompt as a QUESTION: block so a fan-out worker
    # (e.g. one of three researchers spawned to cover three cities) can
    # see *its* slice of the user's request even when USER_QUERY is not
    # in its inputs.
    node_meta = graph_nodes[node_id].get("metadata") or {}
    question = node_meta.get("question") if isinstance(node_meta, dict) else None
    rendered = render_prompt(skill, query, resolved, failure_report,
                             memory_hits=memory_hits, question=question)
    started = time.time()

    if skill.name == "sandbox_executor":
        code = ""
        for r in resolved:
            if r.get("kind") == "upstream" and isinstance(r.get("output"), dict):
                code = r["output"].get("code") or code
        if not code:
            return AgentResult(
                success=False, agent_name=skill.name,
                error="no code in upstream coder output",
                elapsed_s=time.time() - started,
            ), rendered
        from sandbox import run_python
        out = run_python(code)
        return AgentResult(
            success=(out["exit_code"] == 0 and not out["timed_out"]),
            agent_name=skill.name, output=out,
            elapsed_s=time.time() - started,
        ), rendered

    if skill.name == "browser":
        # Same shape as sandbox_executor: the Browser skill owns its own
        # cascade (extract → deterministic → a11y → vision) and never
        # touches the LLM tool/text channel — so we bypass render_prompt
        # and the gateway-chat dispatch entirely and hand off to
        # BrowserSkill.run(NodeSpec).
        node_dict = graph_nodes[node_id]
        node_spec = NodeSpec(
            skill="browser",
            inputs=node_dict.get("inputs") or [],
            metadata=node_dict.get("metadata") or {},
        )
        from browser.skill import BrowserSkill
        sk = BrowserSkill(
            artifacts_root=str(ROOT / "state" / "sessions" / session_id / "browser"),
            session=session_id,
        )
        result = await sk.run(node_spec)
        if not result.elapsed_s:
            result.elapsed_s = time.time() - started
        return result, rendered

    if skill.name == "computer":
        # Same bypass as browser: the Computer skill owns the cua-driver
        # cascade (ax_extract → deterministic → ax_llm → vision) and
        # never goes through the LLM chat channel.  It runs synchronously
        # (cua-driver RPC is blocking) so we wrap it in asyncio.to_thread.
        node_dict = graph_nodes[node_id]
        c_metadata = dict(node_dict.get("metadata") or {})
        # Derive goal from USER_QUERY if not explicitly set in metadata.
        if not c_metadata.get("goal"):
            user_q = next((r["value"] for r in resolved
                           if r.get("id") == "USER_QUERY"), None)
            if user_q:
                c_metadata["goal"] = str(user_q)
        node_spec = NodeSpec(
            skill="computer",
            inputs=node_dict.get("inputs") or [],
            metadata={
                **c_metadata,
                "node_id": node_id,  # trajectory file name matches node_id
            },
            resolved_inputs=resolved,
        )
        from computer.skill import ComputerSkill
        sk = ComputerSkill(
            artifacts_root=str(ROOT / "state" / "sessions" / session_id / "computer"),
            session=session_id,
        )
        result = await asyncio.to_thread(sk.run, node_spec)
        if not result.elapsed_s:
            result.elapsed_s = time.time() - started
        return result, rendered

    def telemetry(reply: dict) -> dict:
        """Pull the usage fields off a gateway reply into AgentResult kwargs.

        Two reply shapes reach this: a plain `LLM().chat` response, whose
        counters cover the single call it made, and an `mcp_runner` response,
        which carries a `_usage` dict already summed over every tool hop plus
        a `_tool_trace`. Prefer `_usage` when present — the top-level fields
        on that reply describe only the final hop.
        """
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

    tools = tool_payload(skill.tools_allowed)
    if tools:
        # Multi-turn tool-use loop. mcp_runner opens one MCP stdio session
        # per skill invocation, dispatches each tool_call the model emits,
        # and feeds the results back until the model produces final text.
        from mcp_runner import run_with_tools
        reply = await run_with_tools(
            prompt=rendered,
            tools_payload=tools,
            agent=skill.name,
            session_id=session_id,
            provider_pin=skill.provider_pin,
            max_tokens=skill.max_tokens,
            temperature=skill.temperature,
        )
    else:
        reply = await asyncio.to_thread(
            LLM().chat,
            prompt=rendered,
            agent=skill.name,
            session=session_id,
            provider=skill.provider_pin,
            max_tokens=skill.max_tokens,
            temperature=skill.temperature,
        )
    parsed = parse_skill_json(reply.get("text", ""))

    # Lift orchestrator-recognised fields out of the skill's JSON.
    # NOTES_RUNS feedback P0 #1: malformed successors used to be silently
    # dropped, which surfaces later as a baffling "missing node" bug.
    # Now: log the offending JSON + the validation error, then fail the
    # node so the failure path (and replay) surfaces it.
    raw_successors = parsed.pop("successors", []) or []
    successors: list[NodeSpec] = []
    rejected: list[str] = []
    for s in raw_successors:
        try:
            successors.append(NodeSpec.model_validate(s))
        except ValidationError as ve:
            rejected.append(f"successor={s!r}  error={ve}")
    if skill.name == "planner":
        for s in parsed.get("nodes", []) or []:
            try:
                successors.append(NodeSpec.model_validate(s))
            except ValidationError as ve:
                rejected.append(f"node={s!r}  error={ve}")

    if rejected:
        err = (
            f"{skill.name}: {len(rejected)} malformed NodeSpec(s) emitted.\n"
            + "\n".join(f"  - {line}" for line in rejected)
        )
        print(f"[skills] {err}")
        return AgentResult(
            success=False, agent_name=skill.name,
            output=parsed, successors=successors,
            elapsed_s=time.time() - started,
            error=err,
            **telemetry(reply),
        ), rendered

    return AgentResult(
        success=True,
        agent_name=skill.name,
        output=parsed,
        successors=successors,
        elapsed_s=time.time() - started,
        **telemetry(reply),
    ), rendered
