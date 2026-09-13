"""USD pricing per 1M tokens, keyed by provider name.

Backs the `dollars` field on /v1/cost/by_agent. Prices are list rates for the
*configured worker model* of each provider — swap a model and the rate below
stops matching, so these are estimates. Tokens are the number to trust;
dollars are derived for human readability.

This table is not decorative. W&B Inference is genuinely metered and sits
second in the default failover order, so real money flows through it — and it
is the ONLY cost visibility the gateway has. `router.LIMITS` throttles
requests and tokens but has no concept of dollars, so nothing here stops a
runaway loop from spending; see "Known gaps" in README.md. Keep these rates
accurate, and cross-check the ledger against your provider's billing page
now and then.

Pricing source: provider docs as of 2026-09. Update alongside `router.LIMITS`
when tiers shift.
"""
from __future__ import annotations


# USD per 1,000,000 tokens (input, output). Unknown providers fall back to
# (0.0, 0.0) via estimate_usd's .get() — $0 rather than a fabricated number.
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    # Free tier under the AI-Studio quota.
    "gemini":     (0.00, 0.00),
    # NVIDIA NIM free tier (build.nvidia.com), text models only here.
    "nvidia":     (0.00, 0.00),
    # Groq's openai/gpt-oss-120b: listed at $0.15/$0.75 per Mtok as of 2026-04.
    "groq":       (0.15, 0.75),
    # OpenRouter ":free" tag — $0 by definition.
    "openrouter": (0.00, 0.00),
    # W&B zai-org/GLM-5.3-Flash (cache hits bill at $0.05, not modelled here).
    # The router pool runs openai/gpt-oss-20b at $0.03/$0.13 — cheaper than
    # this, so worker rates keep the estimate on the conservative side.
    "wandb":      (0.15, 0.50),
    # Local — never charged.
    "ollama":     (0.00, 0.00),
}


def estimate_usd(provider: str, in_tokens: int, out_tokens: int) -> float:
    p_in, p_out = PRICING_USD_PER_MTOK.get(provider, (0.0, 0.0))
    return round((in_tokens / 1e6) * p_in + (out_tokens / 1e6) * p_out, 6)
