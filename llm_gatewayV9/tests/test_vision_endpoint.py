"""V9 /v1/vision smoke. Sends a tiny red/blue PNG with a JSON schema and
expects {"left":"red","right":"blue"} parsed structured output."""
import base64, struct, zlib, json
import httpx

GW = "http://localhost:8109"


def make_png() -> bytes:
    W = H = 32
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    raw = bytearray()
    for _ in range(H):
        raw.append(0)
        for x in range(W):
            raw.extend(b"\xff\x20\x20" if x < W // 2 else b"\x20\x40\xff")
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")


schema = {
    "type": "object",
    "properties": {
        "left":  {"type": "string"},
        "right": {"type": "string"},
    },
    "required": ["left", "right"],
    "additionalProperties": False,
}

body = {
    "image": f"data:image/png;base64,{base64.b64encode(make_png()).decode()}",
    "prompt": "Name the color on the left half and the color on the right half.",
    "schema": schema,
    "schema_name": "Halves",
    "agent": "v9_vision_endpoint_smoke",
}

with httpx.Client(timeout=60) as c:
    r = c.post(f"{GW}/v1/vision", json=body)
    r.raise_for_status()
    out = r.json()

print("provider:", out["provider"], "/ model:", out["model"], "/ latency:", out["latency_ms"], "ms")
print("text    :", out["text"])
print("parsed  :", json.dumps(out["parsed"], indent=2))
print("usage   :", out["input_tokens"], "in /", out["output_tokens"], "out")

assert out["parsed"], "parsed should be populated when schema is given"
assert out["parsed"]["left"].lower().startswith("red"), out["parsed"]
assert out["parsed"]["right"].lower().startswith("blue"), out["parsed"]
print("\n[PASS]")
