"""Content-addressable artifact store.

Raw bytes the agent fetches or produces live here, keyed by sha256 of the
content. Memory holds the handle + a short descriptor; this module owns the
bytes. Perception sees handles, Decision sees bytes only when Perception
attaches them. The 50 KB of HTML touches exactly one LLM call across a run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from schemas import Artifact

STORE = Path(__file__).parent / "state" / "artifacts"
STORE.mkdir(parents=True, exist_ok=True)


def put(blob: bytes, *, content_type: str, source: str, descriptor: str) -> str:
    """Write blob (deduped by content hash) and return its handle."""
    digest = hashlib.sha256(blob).hexdigest()[:16]
    art_id = f"art:{digest}"
    bin_path = STORE / f"{digest}.bin"
    meta_path = STORE / f"{digest}.json"
    if not bin_path.exists():
        bin_path.write_bytes(blob)
        meta = Artifact(
            id=art_id,
            content_type=content_type,
            size_bytes=len(blob),
            source=source,
            descriptor=descriptor,
        )
        meta_path.write_text(meta.model_dump_json(indent=2))
    return art_id


def get_bytes(artifact_id: str) -> bytes:
    digest = artifact_id.removeprefix("art:")
    return (STORE / f"{digest}.bin").read_bytes()


def get_meta(artifact_id: str) -> Artifact:
    digest = artifact_id.removeprefix("art:")
    raw = json.loads((STORE / f"{digest}.json").read_text())
    return Artifact.model_validate(raw)


def exists(artifact_id: str) -> bool:
    digest = artifact_id.removeprefix("art:")
    return (STORE / f"{digest}.bin").exists()
