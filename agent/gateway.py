"""Bridge to the LLM gateway.

Auto-starts the gateway if it is not already up, then re-exports its `LLM`
client, a module-level `embed()` helper, and a cost estimator backed by the
gateway's own price table.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import httpx

from settings import GATEWAY_URL

GATEWAY_DIR = Path(__file__).resolve().parents[1] / "llm_gateway"


def _is_up() -> bool:
    try:
        httpx.get(f"{GATEWAY_URL}/v1/routers", timeout=2.0)
        return True
    except Exception:
        return False


def ensure_gateway() -> None:
    """Start the gateway if it is not already running. Idempotent."""
    if _is_up():
        return
    if not GATEWAY_DIR.exists():
        raise RuntimeError(
            f"Gateway directory not found at {GATEWAY_DIR}. "
            "The gateway must be present before running the agent."
        )
    print(f"[gateway] launching from {GATEWAY_DIR}")
    subprocess.Popen(
        ["uv", "run", "main.py"],
        cwd=str(GATEWAY_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(45):
        time.sleep(1)
        if _is_up():
            print(f"[gateway] up on {GATEWAY_URL}")
            return
    raise RuntimeError(f"Gateway failed to start within 45s. Check {GATEWAY_DIR}")


# Load the gateway's client.py by path rather than by import. The gateway dir
# has its own `schemas.py`, which would shadow ours if we put it on sys.path.
import importlib.util as _importlib_util

_client_path = GATEWAY_DIR / "client.py"
if _client_path.exists():
    _spec = _importlib_util.spec_from_file_location("llm_gateway_client", _client_path)
    _mod = _importlib_util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    LLM = _mod.LLM
else:
    LLM = None  # importers should call ensure_gateway() first


_pricing_path = GATEWAY_DIR / "pricing.py"
if _pricing_path.exists():
    _pspec = _importlib_util.spec_from_file_location("llm_gateway_pricing", _pricing_path)
    _pmod = _importlib_util.module_from_spec(_pspec)
    _pspec.loader.exec_module(_pmod)
    _estimate_usd = _pmod.estimate_usd
else:
    _estimate_usd = None


def estimate_cost(provider: str, in_tokens: int, out_tokens: int) -> float:
    """USD estimate for one call, using the gateway's own price table.

    Loaded by path rather than copied so the table stays single-sourced in
    llm_gateway/pricing.py. The free-tier providers (gemini, nvidia,
    openrouter) and local ollama are all 0.00 there, so a zero is usually
    correct rather than missing. W&B is the one that bills — for those calls
    this number is the only spend signal the system produces.
    """
    if _estimate_usd is None or not provider:
        return 0.0
    try:
        return _estimate_usd(provider, in_tokens, out_tokens)
    except Exception:
        return 0.0


def embed(text: str, task_type: str = "retrieval_document") -> dict:
    """Compute an embedding for `text` via the gateway's embed endpoint."""
    ensure_gateway()
    if LLM is None:
        raise RuntimeError(
            f"Gateway client unavailable. Confirm {GATEWAY_DIR}/client.py exists."
        )
    return LLM().embed(text, task_type=task_type)


__all__ = ["ensure_gateway", "LLM", "GATEWAY_URL", "GATEWAY_DIR", "embed",
           "estimate_cost"]
