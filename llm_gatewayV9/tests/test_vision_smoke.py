"""V9 vision smoke: round-trip an image through the gateway.

Generates a tiny PNG locally so the test has no external dependency.
Drives three cases:

  1. data: URL inline (the most common shape — browser-use produces these)
  2. http URL — gateway should fetch and re-inline as data: URL
  3. provider explicitly pinned to a text-only provider — gateway should 503
     with "no_vision" in the attempts.

Run from llm_gatewayV9/: uv run python tests/test_vision_smoke.py
"""
import base64
import io
import json
import sys
from pathlib import Path

import httpx


GW = "http://localhost:8109"


def _make_png_red_blue() -> bytes:
    """A 32x32 PNG, left half red, right half blue.  No PIL — hand-rolled
    minimal PNG via zlib + CRC."""
    import zlib, struct

    W = H = 32

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw = bytearray()
    for y in range(H):
        raw.append(0)  # filter byte: None
        for x in range(W):
            if x < W // 2:
                raw.extend(b"\xff\x20\x20")  # red
            else:
                raw.extend(b"\x20\x40\xff")  # blue
    idat = zlib.compress(bytes(raw))
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _data_url(img: bytes, mt: str = "image/png") -> str:
    return f"data:{mt};base64,{base64.b64encode(img).decode()}"


def _post(body: dict) -> dict:
    with httpx.Client(timeout=120) as c:
        r = c.post(f"{GW}/v1/chat", json=body)
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            print("[error] status:", r.status_code)
            print(r.text)
            raise
        return r.json()


def case_data_url():
    print("\n=== case 1: data: URL, auto-route ===")
    img = _make_png_red_blue()
    body = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "This image has two halves. Name the color on the left and "
                    "the color on the right. Respond as 'left: <color>, right: <color>'."
                )},
                {"type": "image_url", "image_url": {"url": _data_url(img)}},
            ],
        }],
        "max_tokens": 64,
        "temperature": 0,
        "agent": "v9_vision_smoke",
    }
    out = _post(body)
    print("provider:", out["provider"], "| model:", out["model"])
    print("text    :", out["text"])
    print("usage   :", out["input_tokens"], "in /", out["output_tokens"], "out")
    print("dialect :", out["tool_call_dialect"], "| latency:", out["latency_ms"], "ms")
    assert "red" in out["text"].lower() and "blue" in out["text"].lower(), \
        f"expected red & blue in response; got: {out['text']!r}"
    print("[PASS]")


def case_http_url():
    print("\n=== case 2: http URL — gateway should fetch & inline ===")
    # Small public PNG. Wikipedia hotlinks reliably and the file is tiny.
    url = "https://upload.wikimedia.org/wikipedia/commons/thumb/4/47/PNG_transparency_demonstration_1.png/120px-PNG_transparency_demonstration_1.png"
    body = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "In one short sentence: what is shown?"},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }],
        "max_tokens": 64,
        "temperature": 0,
        "agent": "v9_vision_smoke",
    }
    out = _post(body)
    print("provider:", out["provider"], "| model:", out["model"])
    print("text    :", out["text"])
    print("usage   :", out["input_tokens"], "in /", out["output_tokens"], "out")
    print("[PASS]")


def case_text_only_provider_skipped():
    print("\n=== case 3: pin to text-only provider — should be skipped/errored ===")
    img = _make_png_red_blue()
    body = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "what colors?"},
                {"type": "image_url", "image_url": {"url": _data_url(img)}},
            ],
        }],
        "max_tokens": 64,
        "provider": "groq",   # text-only — router must skip
    }
    try:
        out = _post(body)
        # If we got a 200, the gateway must have failed over — but groq was
        # explicitly pinned, so failover is suppressed and a 503 is expected.
        print("[unexpected 200]", out)
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        print("status :", e.response.status_code)
        body = e.response.json()
        msg = body.get("detail") if isinstance(body, dict) else str(body)
        print("detail :", str(msg)[:300])
        assert "no_vision" in str(msg) or "vision" in str(msg).lower(), \
            "expected the failure reason to mention vision"
        print("[PASS]")


if __name__ == "__main__":
    case_data_url()
    case_http_url()
    case_text_only_provider_skipped()
    print("\nall green ✓")
