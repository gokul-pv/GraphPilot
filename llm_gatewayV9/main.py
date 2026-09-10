import os, time, json
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).parent
load_dotenv(ROOT.parent / ".env")

import db
import providers as P
from router import Router, RouterPool, DEFAULT_ROUTER_ORDER, LIMITS, SHORTCUTS, resolve
from cache import GeminiCache
from schemas import ChatRequest, ChatResponse, ToolCall, RouterDecision, EmbedRequest, EmbedResponse, BatchChatRequest, VisionRequest, ResponseFormat
import embedders as E

DEFAULT_ORDER = ["ollama", "gemini", "nvidia", "groq", "cerebras", "openrouter", "github"]
ORDER = [x.strip() for x in os.getenv("LLM_ORDER", ",".join(DEFAULT_ORDER)).split(",") if x.strip()]
ROUTER_ORDER = [x.strip() for x in os.getenv("ROUTER_ORDER", ",".join(DEFAULT_ROUTER_ORDER)).split(",") if x.strip()]
PORT = int(os.getenv("GATEWAY_V9_PORT", "8109"))

# V8: agent_routing.yaml maps `agent="<name>"` to a preferred provider name.
# The caller's explicit `provider=` still wins. Loaded once at import; the
# file is small enough (~10 lines) that hot-reloading would be over-engineering.
import yaml
_AGENT_ROUTING_PATH = ROOT / "agent_routing.yaml"
AGENT_ROUTING: dict[str, str] = {}
if _AGENT_ROUTING_PATH.exists():
    try:
        AGENT_ROUTING = yaml.safe_load(_AGENT_ROUTING_PATH.read_text()) or {}
    except Exception as e:  # pragma: no cover - logged then ignored
        print(f"[v8] failed to parse agent_routing.yaml: {e!r}")
        AGENT_ROUTING = {}

# Tier -> worker failover order. TINY prefers small fast workers; LARGE prefers
# long-context Gemini; HUGE is rejected (Summarizer Agent will live in V7).
TIER_TO_ORDER = {
    "TINY":  ["github", "openrouter", "groq", "nvidia", "cerebras", "gemini", "ollama"],
    "LARGE": ["gemini", "groq", "nvidia", "cerebras", "github", "openrouter", "ollama"],
}

# Router envelope: cap the sample at ~800 chars (first 400 + last 400).
# Keeps router input under 400 tokens regardless of worker payload size, so
# routing decisions never burn through router quota on big prompts.
ROUTER_SAMPLE_HEAD = 400
ROUTER_SAMPLE_TAIL = 400
ROUTER_PROMPT = (
    "You are a routing classifier. Given a token_count and a content sample, "
    "output exactly one of: TINY, LARGE, or HUGE.\n\n"
    "Rules:\n"
    "- TINY: token_count below 1000 with simple factual content.\n"
    "- LARGE: token_count between 1000 and 8000, OR token_count below 1000 "
    "but content is dense (code, base64, multilingual, technical).\n"
    "- HUGE: token_count above 8000.\n\n"
    "Output the single word and nothing else."
)


def _estimate_tokens(text: str) -> int:
    """words * 1.4 — deliberately rough. The router sample handles the cases
    where rough isn't good enough (code, CJK, base64)."""
    return int(len(text.split()) * 1.4)


def _build_sample(text: str) -> str:
    if len(text) <= ROUTER_SAMPLE_HEAD + ROUTER_SAMPLE_TAIL + 10:
        return text
    return text[:ROUTER_SAMPLE_HEAD] + "\n...\n" + text[-ROUTER_SAMPLE_TAIL:]


def _tier_from_count(tokens: int) -> str:
    """Deterministic fallback when the router LLM is unreachable or replies
    with garbage. Pure token-count rule, identical thresholds."""
    if tokens > 8000:
        return "HUGE"
    if tokens >= 1000:
        return "LARGE"
    return "TINY"


def _parse_tier(text: str) -> Optional[str]:
    up = (text or "").upper()
    for tier in ("HUGE", "LARGE", "TINY"):
        if tier in up:
            return tier
    return None


async def _classify_tier(req: ChatRequest, role: str, router_pool: RouterPool, prompt_text: str):
    """Run a router-LLM classification. Returns a RouterDecision (without
    chosen_worker_* fields, which are filled in by the caller after worker pick).

    Failover: try each router provider in order. Only fall back to the
    pure token-count rule when all routers in the pool have failed.
    """
    estimated = _estimate_tokens(prompt_text)

    # Short-circuit HUGE — no need to spend a router call.
    if estimated > 8000:
        return RouterDecision(
            role=role, tier="HUGE", estimated_tokens=estimated,
            router_provider="(skipped)", router_model="(skipped)",
            router_latency_ms=0, fallback_used=True,
        )

    sample = _build_sample(prompt_text)
    envelope = f"token_count: {estimated}\nsample:\n{sample}"
    call_role = f"router_{role}"

    last_provider = ""
    last_model = ""
    last_latency = 0

    for name in router_pool.candidates():
        ok, why = router_pool.state[name].can_use(LIMITS[name], 400)
        if not ok:
            continue
        provider = router_pool.providers[name]
        t0 = time.time()
        router_pool.state[name].record(0)
        last_provider = name
        last_model = provider.model
        try:
            result = await provider.chat(
                messages=[{"role": "user", "content": envelope}],
                system_blocks=ROUTER_PROMPT,
                max_tokens=8, temperature=0,
                model=None, tools=None, tool_choice=None,
                reasoning="off", response_format=None,
                cache_system=False,
            )
            latency = int((time.time() - t0) * 1000)
            last_latency = latency
            tokens = (result.get("input_tokens") or 0) + (result.get("output_tokens") or 0)
            router_pool.state[name].tokens_today += tokens
            router_pool.state[name].tokens_minute.append((time.time(), tokens))
            tier = _parse_tier(result.get("text", ""))
            # Sanity clamp: HUGE is only valid when the deterministic count
            # agrees. Small router LLMs occasionally hallucinate HUGE on small
            # inputs that look "dense" (URLs, JSON brackets, code fragments).
            # The 8000-token ceiling is hard — override the LLM here to keep
            # the request servable.
            if tier == "HUGE" and estimated <= 8000:
                tier = "LARGE"
            if tier is None:
                # Router returned text we couldn't classify — try the next router
                # rather than giving up immediately. Log this attempt as a soft
                # failure with the actual response captured.
                db.log_call(provider=name, model=result.get("model", provider.model),
                            input_tokens=result.get("input_tokens", 0),
                            output_tokens=result.get("output_tokens", 0),
                            latency_ms=latency, status="error",
                            error=f"unparseable tier reply: {result.get('text','')[:100]}",
                            prompt_chars=len(envelope),
                            call_role=call_role, router_decision="unparseable")
                continue
            db.log_call(provider=name, model=result.get("model", provider.model),
                        input_tokens=result.get("input_tokens", 0),
                        output_tokens=result.get("output_tokens", 0),
                        latency_ms=latency, status="ok",
                        prompt_chars=len(envelope), response_chars=len(result.get("text", "")),
                        call_role=call_role, router_decision=tier)
            return RouterDecision(
                role=role, tier=tier, estimated_tokens=estimated,
                router_provider=name, router_model=result.get("model", provider.model),
                router_latency_ms=latency, fallback_used=False,
            )
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            last_latency = latency
            db.log_call(provider=name, model=provider.model,
                        status="error", error=str(e)[:500],
                        latency_ms=latency, call_role=call_role,
                        router_decision="error")
            # Move on to the next router. No backoff for routing — keep the
            # router pool aggressive since each call is cheap.
            continue

    # All routers in the pool failed — deterministic token-count fallback.
    return RouterDecision(
        role=role, tier=_tier_from_count(estimated), estimated_tokens=estimated,
        router_provider=last_provider or "(unavailable)",
        router_model=last_model or "(unavailable)",
        router_latency_ms=last_latency, fallback_used=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    app.state.cache = GeminiCache(ttl_seconds=300)
    app.state.providers = P.build_providers(app.state.cache)
    app.state.router = Router(app.state.providers, ORDER)
    app.state.router_providers = P.build_router_providers()
    app.state.router_pool = RouterPool(app.state.router_providers, ROUTER_ORDER)
    app.state.embedders, app.state.embed_order = E.build_embedders()
    yield


app = FastAPI(title="LLM Gateway V9", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


def _normalize_messages(req: ChatRequest):
    if req.messages:
        return list(req.messages)
    msgs = []
    msgs.append({"role": "user", "content": req.prompt or ""})
    return msgs


def _system_blocks(req: ChatRequest):
    """Returns the system_blocks payload to hand to the provider adapter."""
    if req.system is None:
        return None
    if isinstance(req.system, str):
        if req.cache_system:
            return [{"text": req.system, "cache": True}]
        return req.system
    return [b.model_dump() if hasattr(b, "model_dump") else b for b in req.system]


def _est_tokens(messages, system_blocks, max_tokens):
    chars = 0
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, list):
            chars += len(P._extract_text_blocks(c))
            # V9: image blocks count as ~258 tokens each on Gemini, ~85 base
            # tokens on OpenAI; use 300 chars per image as a coarse estimate
            # (gets multiplied by ~0.25 in chars→tokens below).
            chars += 1200 * sum(1 for b in c if isinstance(b, dict) and b.get("type") in ("image_url", "image", "input_image"))
        else:
            chars += len(str(c))
    if isinstance(system_blocks, str):
        chars += len(system_blocks)
    elif isinstance(system_blocks, list):
        for b in system_blocks:
            chars += len(b.get("text", "") if isinstance(b, dict) else "")
    return chars // 4 + max_tokens


def _backoff_for(err: Exception, has_model_override: bool = False):
    msg = str(err).lower()
    status = getattr(err, "status", None)
    if status == 429:
        if "queue" in msg: return 15, "server queue full"
        if "quota" in msg or "rpm" in msg or "per minute" in msg: return 60, "RPM quota burned"
        if "rpd" in msg or "per day" in msg or "daily" in msg: return 3600, "RPD quota burned"
        return 30, "rate limited"
    if status and 500 <= status < 600: return 20, f"upstream {status}"
    if status == 408 or "timeout" in msg: return 10, "timeout"
    if status in (401, 403):
        # When the caller explicitly picked a model, 403/404 likely means
        # "this model not available to your account" rather than "key dead".
        # Don't blackball the whole provider for 10 minutes.
        if has_model_override:
            return 0, ""
        return 600, "auth error"
    if status == 404 and has_model_override:
        return 0, ""
    return 0, ""


def _attempts_str(attempts):
    return "; ".join(f"{a['provider']}:{a['reason']}" for a in attempts)


def _required_caps(req: ChatRequest):
    caps = []
    if req.tools: caps.append("tools")
    if req.reasoning and req.reasoning != "off": caps.append("reasoning")
    if req.response_format: caps.append("structured")
    # V9: auto-detect multimodal content. If any message carries image blocks,
    # only providers whose configured model supports vision are eligible.
    if req.messages:
        for m in req.messages:
            if P._content_has_image(m.get("content")):
                caps.append("vision")
                break
    return caps


async def _resolve_image_urls(messages: list[dict]) -> list[dict]:
    """V9: fetch any http(s) image URLs in message content and inline them as
    data: URLs. Providers downstream only ever see data: URLs, which keeps
    Gemini/Ollama translation paths simple. Mutates a copy; original is intact.
    """
    import base64
    import httpx as _httpx

    async def _fetch_to_data_url(url: str) -> str:
        # A real-browser UA — Wikimedia and many CDNs refuse python-default UAs.
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; LLMGatewayV9/0.1; +image-resolver)",
            "Accept": "image/*,*/*;q=0.8",
        }
        async with _httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as c:
            try:
                r = await c.get(url)
                r.raise_for_status()
            except _httpx.HTTPError as e:
                raise HTTPException(400, f"failed to fetch image url {url!r}: {e}")
            mt = (r.headers.get("content-type") or "image/png").split(";")[0].strip()
            b64 = base64.b64encode(r.content).decode()
            return f"data:{mt};base64,{b64}"

    out = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        new_blocks = []
        changed = False
        for b in content:
            if isinstance(b, dict) and b.get("type") == "image_url":
                iu = b.get("image_url")
                url = iu.get("url") if isinstance(iu, dict) else iu
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    data_url = await _fetch_to_data_url(url)
                    new_blocks.append({"type": "image_url", "image_url": {"url": data_url}})
                    changed = True
                    continue
            new_blocks.append(b)
        if changed:
            new_m = dict(m)
            new_m["content"] = new_blocks
            out.append(new_m)
        else:
            out.append(m)
    return out


def _validate_structured(text: str, schema: dict):
    try:
        obj = json.loads(text)
    except Exception as e:
        raise ValueError(f"output is not JSON: {e}")
    Draft202012Validator(schema).validate(obj)
    return obj


@app.post("/v1/chat")
async def chat(req: ChatRequest):
    router = app.state.router
    router_pool = app.state.router_pool
    messages = _normalize_messages(req)
    # V9: pre-resolve any http(s) image URLs to data: URLs once, centrally.
    # Cheap when there are no images (function is a pass-through).
    if any(P._content_has_image(m.get("content")) for m in messages):
        messages = await _resolve_image_urls(messages)
    system_blocks = _system_blocks(req)
    prompt_text = "".join(
        (P._extract_text_blocks(m.get("content", "")) if isinstance(m.get("content"), list)
         else str(m.get("content", "")))
        for m in messages
    )
    est = _est_tokens(messages, system_blocks, req.max_tokens)
    explicit_override = bool(req.provider)
    required_caps = _required_caps(req)

    # V8: if the caller tagged the request with an agent name and did not
    # pin a provider explicitly, apply agent_routing.yaml's preferred provider.
    # This mutates req.provider so the rest of the function (router-pick,
    # candidate-narrowing, single-candidate-wait) sees the pin.
    if req.agent and not req.provider:
        pinned = AGENT_ROUTING.get(req.agent)
        if pinned and pinned in router.providers:
            req.provider = pinned
            explicit_override = True

    # V8: retry-on-5xx with `retries` surfaced in the response. The
    # per-provider failover loop below already rotates providers on
    # ProviderError; this counter exists for the single-provider retry case
    # (mostly meaningful when `provider=` is explicit). One retry, backoff
    # capped at 2s as the spec says.
    retries = 0

    # V3: auto_route runs a router-LLM classifier first and uses tier-specific
    # failover order. Explicit `provider` overrides routing (caller knows best).
    router_decision: Optional[RouterDecision] = None
    if req.auto_route and not req.provider:
        router_decision = await _classify_tier(req, req.auto_route, router_pool, prompt_text)
        if router_decision.tier == "HUGE":
            raise HTTPException(
                503,
                {
                    "error": "input exceeds 8000 tokens",
                    "hint": "Use the Summarizer Agent (V7, not yet implemented). "
                            "For now, chunk the input or set provider=g explicitly to try Gemini anyway.",
                    "router_decision": router_decision.model_dump(),
                },
            )
        # Replace failover order with the tier-specific one, intersected with
        # what's actually wired in this gateway.
        tier_order = TIER_TO_ORDER[router_decision.tier]
        candidates = [p for p in tier_order if p in router.providers]
    else:
        candidates = router.candidates(req.provider) if req.provider else list(router.order)

    if req.provider and not candidates:
        raise HTTPException(400, f"unknown provider '{req.provider}'. Try one of: {list(router.providers)} or shortcuts {list(SHORTCUTS)}")

    all_attempts = []
    last_err = None

    # When explicit provider is requested and the only blocker is cooldown,
    # wait briefly rather than 503-ing — this is what users intuitively expect.
    if explicit_override and len(candidates) == 1:
        import asyncio as _asyncio
        deadline = time.time() + 30
        while time.time() < deadline:
            name, _ = router.pick(est, candidates, required_caps=required_caps)
            if name is not None:
                break
            cd = router.state[candidates[0]].snapshot(LIMITS[candidates[0]])["cooldown_remaining"]
            if cd <= 0 or cd > 30:
                break
            await _asyncio.sleep(min(cd + 0.05, 5))

    for _ in range(len(candidates) + 1):
        name, atts = router.pick(est, candidates, required_caps=required_caps)
        all_attempts.extend(atts)
        if name is None:
            break

        provider = router.providers[name]
        t0 = time.time()
        router.state[name].record(0)

        try:
            if req.stream:
                async def gen():
                    try:
                        agg = []
                        async for chunk in provider.stream(messages,
                                                          max_tokens=req.max_tokens,
                                                          temperature=req.temperature,
                                                          model=req.model,
                                                          tools=req.tools,
                                                          tool_choice=req.tool_choice,
                                                          reasoning=req.reasoning,
                                                          response_format=req.response_format,
                                                          system_blocks=system_blocks,
                                                          cache_system=bool(req.cache_system)):
                            agg.append(chunk)
                            if chunk.startswith("[[TOOL_CALL_DELTA]]"):
                                yield f"data: {json.dumps({'provider': name, 'tool_call_delta': chunk[len('[[TOOL_CALL_DELTA]] '):]})}\n\n"
                            else:
                                yield f"data: {json.dumps({'provider': name, 'delta': chunk})}\n\n"
                        text = "".join(agg)
                        latency = int((time.time() - t0) * 1000)
                        db.log_call(provider=name, model=req.model or provider.model,
                                    latency_ms=latency, status="ok",
                                    prompt_chars=len(prompt_text), response_chars=len(text),
                                    override=req.provider, attempted=_attempts_str(all_attempts),
                                    agent=req.agent, session=req.session, retries=retries)
                        yield f"data: {json.dumps({'done': True, 'provider': name})}\n\n"
                    except Exception as e:
                        db.log_call(provider=name, model=req.model or provider.model,
                                    status="error", error=str(e)[:500],
                                    latency_ms=int((time.time() - t0) * 1000),
                                    prompt_chars=len(prompt_text),
                                    override=req.provider, attempted=_attempts_str(all_attempts),
                                    agent=req.agent, session=req.session, retries=retries)
                        yield f"data: {json.dumps({'error': str(e)[:300]})}\n\n"
                return StreamingResponse(gen(), media_type="text/event-stream")

            # V8: one same-provider retry on transient 5xx / timeout before
            # we fall through to the failover loop. Exponential backoff capped
            # at 2s, exactly as the spec says. `retries` is surfaced in the
            # response so the orchestrator's replay can show it.
            try:
                result = await provider.chat(messages,
                                             max_tokens=req.max_tokens,
                                             temperature=req.temperature,
                                             model=req.model,
                                             tools=req.tools,
                                             tool_choice=req.tool_choice,
                                             reasoning=req.reasoning,
                                             response_format=req.response_format,
                                             system_blocks=system_blocks,
                                             cache_system=bool(req.cache_system))
            except (P.ProviderError, Exception) as transient:
                status = getattr(transient, "status", None)
                msg = str(transient).lower()
                retryable = (
                    (status is not None and 500 <= status < 600)
                    or status == 408
                    or "timeout" in msg
                )
                if not retryable:
                    raise
                import asyncio as _a
                await _a.sleep(min(2.0, 0.5 * (2 ** retries)))
                retries += 1
                result = await provider.chat(messages,
                                             max_tokens=req.max_tokens,
                                             temperature=req.temperature,
                                             model=req.model,
                                             tools=req.tools,
                                             tool_choice=req.tool_choice,
                                             reasoning=req.reasoning,
                                             response_format=req.response_format,
                                             system_blocks=system_blocks,
                                             cache_system=bool(req.cache_system))
            latency = int((time.time() - t0) * 1000)

            # Optional: validate structured output and (single) retry on failure.
            parsed = None
            if req.response_format and req.response_format.schema_ and not result["tool_calls"]:
                try:
                    parsed = _validate_structured(result["text"], req.response_format.schema_)
                except (ValueError, ValidationError) as ve:
                    # one corrective retry
                    fix_msgs = list(messages) + [
                        {"role": "assistant", "content": result["text"]},
                        {"role": "user", "content": f"Your previous reply did not match the required JSON schema: {ve}. Reply ONLY with valid JSON conforming to the schema."},
                    ]
                    result = await provider.chat(fix_msgs,
                                                 max_tokens=req.max_tokens,
                                                 temperature=0,
                                                 model=req.model,
                                                 response_format=req.response_format,
                                                 system_blocks=system_blocks,
                                                 cache_system=bool(req.cache_system))
                    try:
                        parsed = _validate_structured(result["text"], req.response_format.schema_)
                    except (ValueError, ValidationError) as ve2:
                        raise HTTPException(503, f"structured output failed validation: {ve2}")

            tokens = (result["input_tokens"] or 0) + (result["output_tokens"] or 0)
            router.state[name].tokens_today += tokens
            router.state[name].tokens_minute.append((time.time(), tokens))
            if router_decision is not None:
                router_decision.chosen_worker_provider = name
                router_decision.chosen_worker_model = result["model"]
            db.log_call(provider=name, model=result["model"],
                        input_tokens=result["input_tokens"], output_tokens=result["output_tokens"],
                        cache_create_tokens=result["cache_creation_input_tokens"],
                        cache_read_tokens=result["cache_read_input_tokens"],
                        latency_ms=latency, status="ok",
                        prompt_chars=len(prompt_text), response_chars=len(result["text"]),
                        override=req.provider, attempted=_attempts_str(all_attempts),
                        tool_calls=len(result["tool_calls"]),
                        reasoning_applied=result["reasoning_applied"],
                        tool_dialect=result["tool_call_dialect"],
                        call_role="worker",
                        router_decision=router_decision.tier if router_decision else None,
                        agent=req.agent, session=req.session, retries=retries)
            return ChatResponse(
                provider=name,
                model=result["model"],
                text=result["text"],
                tool_calls=[ToolCall(**tc) for tc in result["tool_calls"]],
                stop_reason=result["stop_reason"],
                input_tokens=result["input_tokens"],
                output_tokens=result["output_tokens"],
                cache_creation_input_tokens=result["cache_creation_input_tokens"],
                cache_read_input_tokens=result["cache_read_input_tokens"],
                latency_ms=latency,
                tool_call_dialect=result["tool_call_dialect"],
                reasoning_applied=result["reasoning_applied"],
                parsed=parsed,
                attempted=all_attempts,
                router_decision=router_decision,
                retries=retries,
            ).model_dump()

        except P.ProviderError as e:
            last_err = str(e)
            secs, reason = _backoff_for(e, has_model_override=bool(req.model))
            if secs > 0:
                router.state[name].mark_unavailable(secs, reason)
            db.log_call(provider=name, model=req.model or provider.model,
                        status="error", error=str(e)[:500],
                        latency_ms=int((time.time() - t0) * 1000),
                        prompt_chars=len(prompt_text),
                        override=req.provider, attempted=_attempts_str(all_attempts),
                        agent=req.agent, session=req.session, retries=retries)
            tag = f"failed: {str(e)[:100]}"
            if secs > 0: tag += f" → backoff {secs:.0f}s ({reason})"
            all_attempts.append({"provider": name, "reason": tag})
            if explicit_override or not getattr(e, "retryable", True):
                raise HTTPException(502, f"{name} failed: {e}")
            candidates = [c for c in candidates if c != name]
            continue
        except HTTPException:
            raise
        except Exception as e:
            last_err = str(e)
            secs, reason = _backoff_for(e, has_model_override=bool(req.model))
            if secs > 0:
                router.state[name].mark_unavailable(secs, reason)
            db.log_call(provider=name, model=req.model or provider.model,
                        status="error", error=str(e)[:500],
                        latency_ms=int((time.time() - t0) * 1000),
                        prompt_chars=len(prompt_text),
                        override=req.provider, attempted=_attempts_str(all_attempts),
                        agent=req.agent, session=req.session, retries=retries)
            all_attempts.append({"provider": name, "reason": f"exception: {str(e)[:120]}"})
            if explicit_override:
                raise HTTPException(502, f"{name} failed: {e}")
            candidates = [c for c in candidates if c != name]
            continue

    raise HTTPException(503, f"all providers unavailable. attempts: {all_attempts}. last_error: {last_err}")


# ── V8 additions: batch endpoint and cost-by-agent ────────────────────────────

@app.post("/v1/chat/batch")
async def chat_batch(req: BatchChatRequest):
    """Run N chat requests concurrently with bounded parallelism. The gateway
    manages the rate-limit ladder centrally so callers do not need to open
    their own connection pools. Results are returned IN INPUT ORDER. Each
    inner call goes through the same `/v1/chat` pipeline (agent routing,
    retry, failover, db logging) — this endpoint is sugar on top."""
    import asyncio as _a
    sem = _a.Semaphore(max(1, req.max_concurrency))

    async def _one(call: ChatRequest):
        async with sem:
            try:
                return await chat(call)
            except HTTPException as he:
                return {"error": str(he.detail), "status_code": he.status_code}
            except Exception as e:
                return {"error": str(e)[:400], "status_code": 500}

    results = await _a.gather(*[_one(c) for c in req.calls])
    return {"results": results}


@app.post("/v1/vision")
async def vision(req: VisionRequest):
    """V9: single-image vision call. Thin shim over /v1/chat that:
      - packs `image` + `prompt` into a multimodal user message
      - forces routing to a vision-capable provider (via `vision` cap)
      - optionally enforces a JSON schema for structured output

    Returns the same ChatResponse shape as /v1/chat; if a schema was provided
    the parsed object is in `.parsed`.
    """
    content: list[dict[str, Any]] = [{"type": "text", "text": req.prompt}]
    content.append({"type": "image_url", "image_url": {"url": req.image}})

    inner = ChatRequest(
        messages=[{"role": "user", "content": content}],
        system=req.system,
        provider=req.provider,
        model=req.model,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        response_format=(
            ResponseFormat(type="json_schema", schema=req.schema_, name=req.schema_name, strict=True)
            if req.schema_ else None
        ),
        agent=req.agent,
        session=req.session,
    )
    return await chat(inner)


@app.get("/v1/cost/by_agent")
async def cost_by_agent(session: Optional[str] = None, agent: Optional[str] = None):
    """Per-agent rollup. With ?session=<sid> the rollup is scoped to one
    flow-run; with ?agent=<name> the rollup is scoped to a single agent tag;
    without either, the calendar day. Used by the orchestrator's replay step
    to show how much each skill cost.

    V9: each row now carries a `dollars` field derived from `pricing.py`'s
    table.  $0 for free-tier providers (the course default); accurate-ish
    for paid providers.  Tokens remain the headline number.
    """
    import pricing as _pricing
    raw = db.by_agent(session=session)
    if agent:
        raw = {agent: raw.get(agent, [])}
    out: dict[str, list[dict]] = {}
    for ag, rows in raw.items():
        out[ag] = []
        for r in rows:
            r2 = dict(r)
            r2["dollars"] = _pricing.estimate_usd(
                r["provider"], r.get("in_tok") or 0, r.get("out_tok") or 0
            )
            out[ag].append(r2)
    return out


@app.post("/v1/embed")
async def embed(req: EmbedRequest):
    """Single new V7 endpoint. Failover ring runs Ollama → configured fallback.
    `provider` pins the choice (returns 502 on failure with no fallback).
    Rejects inputs over MAX_INPUT_CHARS with 413 — caller must chunk."""
    embedders = app.state.embedders
    if not embedders:
        raise HTTPException(503, "no embedding providers configured")

    if len(req.text) > E.MAX_INPUT_CHARS:
        raise HTTPException(
            413,
            f"text is {len(req.text)} chars; embed input is capped at "
            f"{E.MAX_INPUT_CHARS} chars (~{E.MAX_INPUT_CHARS // 4} tokens, the "
            f"gemini-embedding-001 ceiling). Chunk the input and embed each chunk.",
        )

    t0 = time.time()
    try:
        name, result, attempts, latency = await E.embed_with_failover(
            embedders, req.text, req.task_type, explicit=req.provider
        )
    except E.EmbedderError as e:
        latency = int((time.time() - t0) * 1000)
        db.log_call(
            provider=req.provider or "(any)",
            model="(none)",
            status="error",
            error=str(e)[:500],
            latency_ms=latency,
            prompt_chars=len(req.text),
            override=req.provider,
            call_role="embed",
        )
        if req.provider:
            # Pinned provider: surface upstream status faithfully.
            if e.status == 429:
                raise HTTPException(429, f"{req.provider} rate-limited: {e}")
            if e.status == 400:
                raise HTTPException(400, str(e))
            raise HTTPException(502, f"{req.provider} embed failed: {e}")
        raise HTTPException(503, str(e))

    db.log_call(
        provider=name,
        model=result["model"],
        status="ok",
        latency_ms=latency,
        prompt_chars=len(req.text),
        override=req.provider,
        attempted=_attempts_str(attempts),
        call_role="embed",
        embed_dim=result["dim"],
    )
    return EmbedResponse(
        provider=name,
        model=result["model"],
        embedding=result["embedding"],
        dim=result["dim"],
        latency_ms=latency,
        attempted=attempts,
    ).model_dump()


@app.get("/v1/embedders")
async def list_embedders():
    return {
        "order": app.state.embed_order,
        "models": {e.name: e.model for e in app.state.embedders},
        "fixed_dim": E.EMBED_DIM,
        "max_input_chars": E.MAX_INPUT_CHARS,
        "backoff_steps_s": E.BACKOFF_STEPS,
        "live": {e.name: e.state.snapshot() for e in app.state.embedders},
        "today": db.aggregate(call_role="embed"),
    }


@app.get("/v1/providers")
async def list_providers():
    r = app.state.router
    return {
        "order": r.order,
        "providers": list(r.providers.keys()),
        "shortcuts": SHORTCUTS,
        "limits": LIMITS,
        "models": {n: p.model for n, p in r.providers.items()},
    }


@app.get("/v1/capabilities")
async def capabilities():
    r = app.state.router
    out = {}
    for name, p in r.providers.items():
        caps = dict(getattr(p, "capabilities", {}))
        # per-model overrides
        caps = P.model_capabilities(name, p.model, caps)
        caps["model"] = p.model
        caps.update({
            "max_ctx": LIMITS[name]["max_ctx"],
            "rpm": LIMITS[name]["rpm"],
            "rpd": LIMITS[name]["rpd"],
        })
        out[name] = caps
    return out


@app.get("/v1/status")
async def status():
    r = app.state.router
    return {"order": r.order, "live": r.all_status(),
            "today": db.aggregate(call_role="worker"), "limits": LIMITS}


@app.get("/v1/routers")
async def routers():
    """V3: router pool — separate from the worker pool. Shows which router LLMs
    are wired, the failover order, and live rate-state."""
    rp = app.state.router_pool
    return {
        "order": rp.order,
        "providers": list(rp.providers.keys()),
        "models": {n: p.model for n, p in rp.providers.items()},
        "live": rp.all_status(),
        "today": db.aggregate(call_role="router"),
        "limits": {k: LIMITS[k] for k in rp.providers},
        "tier_to_order": TIER_TO_ORDER,
    }


@app.get("/v1/calls")
async def calls(limit: int = 100, provider: Optional[str] = None, status: Optional[str] = None):
    return db.recent(limit=limit, provider=provider, status=status)


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(str(ROOT / "static" / "dashboard.html"))


@app.get("/help", response_class=HTMLResponse)
async def help_page():
    return FileResponse(str(ROOT / "static" / "help.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
