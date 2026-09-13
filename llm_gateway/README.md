# LLM Gateway

[Repository overview](../README.md)

A FastAPI service that gives the agent one interface for model calls. It supports chat, tool calls, structured output, batch requests, vision, embeddings, routing, and usage tracking. The default address is `http://localhost:8109`.

## Start and configure

Use Python 3.11+ and `uv`. Create `.env` in the **repository root**, one directory above this gateway. For a Gemini-backed starting configuration:

```dotenv
GEMINI_API_KEY=your-key
EMBED_ORDER=gemini
```

Start from the repository root:

```bash
cd llm_gateway
uv sync
uv run python main.py
```

Open the [dashboard](http://localhost:8109) to inspect providers, calls, and rate state. Probe the configured workers with:

```bash
curl -s http://localhost:8109/v1/providers
```

Provider adapters are enabled by these environment variables:

| Provider | Required setting | Optional setting |
| --- | --- | --- |
| Gemini | `GEMINI_API_KEY` | `GEMINI_MODEL` |
| NVIDIA | `NVIDIA_API_KEY` | `NVIDIA_MODEL` |
| Groq | `GROQ_API_KEY` | `GROQ_MODEL` |
| OpenRouter | `OPEN_ROUTER_API_KEY` | `OPENROUTER_MODEL` |
| W&B Inference | `WANDB_API_KEY` | `WANDB_PROJECT` (`<team>/<project>`, sent as the `OpenAI-Project` header), `WANDB_MODEL`, `WANDB_MAX_CTX` |
| Ollama | `OLLAMA_MODEL` and a running local model | `OLLAMA_URL` defaults to `http://localhost:11434` |

These are implemented adapters; model availability depends on your provider account. Inspect `/v1/providers` and `/v1/capabilities` for this deployment's configured models and supported features.

`LLM_ORDER` sets the worker order; the default is Gemini, W&B, Groq, OpenRouter, NVIDIA, then Ollama. `ROUTER_ORDER` and `ROUTER_<PROVIDER>_MODEL` configure the separate classification pool, which defaults to W&B, Groq, NVIDIA. `GATEWAY_PORT` changes the server port and propagates to the agent's clients through `agent/settings.py`; the legacy `GATEWAY_V9_PORT` and `LLM_GATEWAY_V9_URL` names are still honoured.

## Routing and failure behavior

Selection follows this order:

1. An explicit request `provider` wins.
2. Otherwise, a matching entry in [agent_routing.yaml](agent_routing.yaml) pins the request if that provider is configured.
3. Otherwise, `auto_route` asks a small router model for a `TINY`, `LARGE`, or `HUGE` tier. A count-based fallback is used if classification fails. `TINY` and `LARGE` select worker orders; `HUGE` is rejected with HTTP 503.
4. Without a pin or `auto_route`, the configured worker order is used.

Candidates are filtered by capabilities, context limits, and rate state. **Explicit and configured skill pins restrict the call to one provider.** They do not fail over to another provider, despite the older comments in the routing YAML. A pinned request can briefly wait for cooldown; unpinned requests can try another candidate after retryable provider errors.

Non-streaming calls retry a transient 5xx/408/timeout once on the same provider. JSON-schema output is validated and gets one corrective attempt if invalid. Streaming returns server-sent events; an error after streaming starts is reported in that stream, without provider failover. Model support for tools, vision, reasoning, and caching varies.

The default skill map mostly pins Gemini, with critic on Groq and retriever/sandbox_executor on W&B. Only configured providers activate those pins. The sandbox executor itself runs locally and bypasses the gateway.

## API

| Method and path | Purpose |
| --- | --- |
| `POST /v1/chat` | Prompt or messages; optional provider/model, tools, JSON schema, streaming, routing, and agent/session tags. |
| `POST /v1/chat/batch` | A `calls` list with optional `max_concurrency` (default 4); results retain input order and can contain per-call errors. Use non-streaming calls. |
| `POST /v1/vision` | One `image` URL or base64 data URL and a `prompt`; optional output `schema`. Uses the chat pipeline and vision-capable candidates. |
| `POST /v1/embed` | Embed `text`, with optional `task_type` and `provider`. |
| `GET /v1/providers`, `/v1/capabilities` | Configured workers, models, limits, and capabilities. |
| `GET /v1/status`, `/v1/routers`, `/v1/embedders` | Worker, router, and embedding configuration/rate state. |
| `GET /v1/calls` | Recent call records; optional `limit`, `provider`, and `status` filters. |
| `GET /v1/cost/by_agent` | Usage grouped by agent; optional `session` and `agent` filters. |
| `GET /`, `/help` | Dashboard and bundled help page. |

A minimal call:

```bash
curl -s http://localhost:8109/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Say hello.","agent":"example","max_tokens":100}'
```

Chat responses include provider/model, text, tool calls, token counts, latency, attempts, retries, and optional parsed structured output or router decision. The gateway returns tool requests; the caller executes tools and sends results back. See [schemas.py](schemas.py) for exact fields and [client.py](client.py) for the Python client.

## Embeddings

The embedding endpoint produces 768-dimensional vectors and rejects text over 8,000 characters. `task_type` is `retrieval_document` by default; use `retrieval_query` for searches.

The default order is Ollama (`nomic-embed-text`), then Gemini (`gemini-embedding-001`) when a key is present. Configure it with `EMBED_ORDER`, `EMBED_OLLAMA_MODEL`, and `EMBED_FALLBACK_MODEL`. An explicit embedding `provider` prevents failover.

**Equal dimensions do not make different models' vectors interchangeable.** Use one embedding model for both stored documents and queries. The default cross-model fallback can mix incompatible embeddings; `EMBED_ORDER=gemini` in the example keeps one model. For local embeddings, use `EMBED_ORDER=ollama` and install `nomic-embed-text` in Ollama. Rebuild stored embeddings when changing models.

## Usage and validation

Calls are recorded in SQLite at `llm_gateway/gateway.db`. Dollar totals come from the provider-level table in [pricing.py](pricing.py); they are estimates, not billing records, and the free-tier providers are assigned zero. Token counts are the more useful usage measure — but see Known gaps below, because for W&B the dollar figure is the one that matters.

This documentation was checked against the current source and an offline dependency dry run. Provider integration tests and live model requests were not run as part of this update.

## Known gaps

### The gateway throttles requests and tokens, but not dollars

`router.LIMITS` tracks RPM, RPD, TPM, and a daily token ceiling. It has no concept of cost. That was harmless while every provider in the ring was free-tier or local — it no longer is.

**W&B Inference is metered**, and it sits second in the default worker order. It also publishes no rate limits: the [usage-limits docs](https://docs.wandb.ai/inference/usage-limits) give only a monthly *spend* cap and an undocumented concurrency limit that surfaces as `429 Concurrency limit reached`. So W&B's `LIMITS` entry leaves the quota dimensions deliberately unbounded — inventing throttles would just make the router skip a provider that was actually available — and the 429-backoff path absorbs concurrency rejects instead.

The consequence: **nothing in this gateway stops a runaway agent loop from spending money.** The only backstops today are

- the provider-side monthly spend cap you set in your W&B account, and
- `GET /v1/cost/by_agent`, which you have to watch yourself.

A fix would follow the shape the rate limiter already uses: record per-call dollars in [db.py](db.py) alongside tokens, expose a `spend_today` rollup, and have `RateState.can_use` compare it against a `MAX_USD_PER_DAY` environment variable. Because that check lives in the same place as the rate checks, an exhausted budget would make W&B simply ineligible — the request fails over to the free providers rather than erroring. Keep the rates in [pricing.py](pricing.py) accurate, since that table would become the enforcement input rather than just a report.
