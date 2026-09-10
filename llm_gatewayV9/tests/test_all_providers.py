#!/usr/bin/env python3
"""Per-provider matrix test for llm_gatewayV2.

Tests A (basic), B (tools), C (structured), D (caching), E (reasoning)
against each of the 7 providers. Prints a matrix at the end.

Assumes V2 is running at http://localhost:8100 (env LLM_GATEWAY_V2_URL to override).
"""
from __future__ import annotations
import os, sys, json, time, httpx

URL = os.getenv("LLM_GATEWAY_V2_URL", "http://localhost:8100")
PROVIDERS = ["o", "g", "n", "gr", "c", "or", "gh"]
PROVIDER_NAMES = {"o":"ollama","g":"gemini","n":"nvidia","gr":"groq","c":"cerebras","or":"openrouter","gh":"github"}

ADD_TOOL = {
    "name": "add",
    "description": "Return a + b.",
    "input_schema": {
        "type": "object",
        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
        "required": ["a", "b"],
    },
}

CITY_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
    "required": ["city", "country"],
}


def post(body, timeout=120, retries=1):
    """POST to /v1/chat with optional retry on transient upstream failure."""
    last_code, last_body = 0, {}
    for attempt in range(retries + 1):
        try:
            r = httpx.post(f"{URL}/v1/chat", json=body, timeout=timeout)
            last_code = r.status_code
            try: last_body = r.json()
            except Exception: last_body = {"raw": r.text}
        except Exception as e:
            last_code, last_body = 0, {"raw": f"{e}"}
        if last_code == 200:
            return last_code, last_body
        if attempt < retries:
            time.sleep(6)
    return last_code, last_body


def _budget(p, default):
    # gemma4:31b on ollama is a "thinking" model that emits ~300+ internal tokens
    # before producing visible text. Bump its budget so the response isn't empty.
    return 1024 if p == "o" else default


def test_basic(p):
    code, d = post({"prompt": "Say hi in 3 words.", "provider": p, "max_tokens": _budget(p, 256)},
                   timeout=120 if p != "o" else 180)
    if code == 200 and d.get("text", "").strip():
        return "OK", d.get("text","").strip()[:40]
    return "FAIL", f"code={code} {str(d)[:80]}"


def test_tools(p):
    msgs = [{"role": "user", "content": "What is 7 plus 5? Use the add tool."}]
    code, d = post({"messages": msgs, "provider": p, "tools": [ADD_TOOL], "tool_choice": "auto",
                    "max_tokens": _budget(p, 512), "temperature": 0}, timeout=180)
    if code != 200:
        return "FAIL", f"first call code={code} {str(d)[:80]}"
    tcs = d.get("tool_calls") or []
    if not tcs:
        return "FAIL", f"no tool_calls; text={d.get('text','')[:80]}"
    tc = tcs[0]
    args = tc.get("arguments") or {}
    a, b = args.get("a"), args.get("b")
    if {a, b} != {7, 5} and {a, b} != {7.0, 5.0}:
        return "PARTIAL", f"tool_call args={args}"
    # Send tool result back
    msgs2 = msgs + [
        {"role": "assistant", "content": "", "tool_calls": [tc]},
        {"role": "tool", "tool_call_id": tc["id"], "tool_name": tc["name"], "content": json.dumps({"result": 12})},
    ]
    code, d2 = post({"messages": msgs2, "provider": p, "tools": [ADD_TOOL],
                    "max_tokens": _budget(p, 256), "temperature": 0}, timeout=180)
    if code != 200:
        return "PARTIAL", f"tool_call ok but second call code={code} {str(d2)[:80]}"
    final = (d2.get("text") or "").strip()
    if "12" in final:
        return "OK", f"dialect={d.get('tool_call_dialect')} final='{final[:40]}'"
    return "PARTIAL", f"final='{final[:60]}'"


def test_structured(p):
    body = {
        "prompt": "Paris is in which country? Respond with JSON {city,country}.",
        "provider": p,
        "max_tokens": _budget(p, 512), "temperature": 0,
        "response_format": {"type": "json_schema", "schema": CITY_SCHEMA, "name": "loc", "strict": True},
    }
    code, d = post(body)
    if code != 200:
        return "FAIL", f"code={code} {str(d)[:80]}"
    parsed = d.get("parsed")
    if parsed and parsed.get("city","").lower() == "paris" and parsed.get("country","").lower() == "france":
        return "OK", f"{parsed}"
    # try plain text parse fallback
    try:
        obj = json.loads(d.get("text") or "{}")
        if obj.get("city","").lower() == "paris" and obj.get("country","").lower() == "france":
            return "OK", f"{obj} (text)"
    except Exception:
        pass
    return "PARTIAL", f"parsed={parsed} text='{(d.get('text') or '')[:80]}'"


def test_caching(p):
    if p == "o":
        return "SKIP", "ollama: local, no upstream cache"
    long_sys = ("You are a meticulous geography tutor. Always answer concisely. " * 200).strip()
    body = {
        "prompt": "Capital of France?",
        "provider": p,
        "system": long_sys,
        "cache_system": True,
        "max_tokens": _budget(p, 80), "temperature": 0,
    }
    code1, d1 = post(body)
    if code1 != 200:
        return "FAIL", f"first code={code1} {str(d1)[:80]}"
    time.sleep(0.4)
    code2, d2 = post(body)
    if code2 != 200:
        return "FAIL", f"second code={code2} {str(d2)[:80]}"
    cr1 = d1.get("cache_read_input_tokens", 0) or 0
    cr2 = d2.get("cache_read_input_tokens", 0) or 0
    cw1 = d1.get("cache_creation_input_tokens", 0) or 0
    if cr2 > 0 or cw1 > 0:
        return "OK", f"cw1={cw1} cr2={cr2}"
    return "n/a", f"no cache signal cr1={cr1} cr2={cr2} (provider may not surface)"


def test_reasoning(p):
    body = {
        "prompt": "If a train leaves Boston at 3pm at 60mph and another leaves NYC (200mi south) at 4pm at 80mph headed north, when do they meet? Be brief.",
        "provider": p,
        "reasoning": "high",
        "max_tokens": _budget(p, 400), "temperature": 0,
    }
    t0 = time.time()
    code, d = post(body, timeout=180)
    dt = time.time() - t0
    if code != 200:
        return "FAIL", f"code={code} {str(d)[:80]}"
    if d.get("reasoning_applied"):
        return "OK", f"applied=True latency={dt:.1f}s"
    return "n/a", f"applied=False (model lacks knob) latency={dt:.1f}s"


def run_provider(p):
    name = PROVIDER_NAMES[p]
    row = {}
    details = {}
    for col, fn in [("basic", test_basic), ("tools", test_tools),
                    ("struct", test_structured), ("cache", test_caching),
                    ("reasoning", test_reasoning)]:
        try:
            status, info = fn(p)
        except Exception as e:
            status, info = "FAIL", f"exc {e}"
        row[col] = status
        details[col] = info
        print(f"  [{name:10s}] {col:10s} → {status:7s} {info[:100]}", flush=True)
    return name, row, details


def main():
    import concurrent.futures as cf
    matrix = {}
    all_details = {}
    print("Running all 7 providers in parallel...\n", flush=True)
    with cf.ThreadPoolExecutor(max_workers=7) as ex:
        futs = {ex.submit(run_provider, p): p for p in PROVIDERS}
        for fut in cf.as_completed(futs):
            name, row, det = fut.result()
            matrix[name] = row
            all_details[name] = det

    # Pretty matrix
    print("\n" + "=" * 78)
    cols = ["basic", "tools", "struct", "cache", "reasoning"]
    print(f"{'provider':12s}" + "  ".join(f"{c:9s}" for c in cols))
    print("-" * 78)
    for name in [PROVIDER_NAMES[p] for p in PROVIDERS]:
        row = matrix.get(name, {})
        print(f"{name:12s}" + "  ".join(f"{row.get(c,'?'):9s}" for c in cols))
    print("=" * 78)

    # Exit non-zero if any provider's basic test failed.
    bad = [n for n, r in matrix.items() if r.get("basic") == "FAIL"]
    if bad:
        print(f"\nFAIL: basic test broken on: {bad}")
        sys.exit(1)
    print("\nbasic-test ok across all providers.")


if __name__ == "__main__":
    main()
