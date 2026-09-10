"""Memory: a typed service with four kinds.

Session 7 adds vector retrieval on top of the Session 6 service. Reads go
through FAISS first (cosine similarity over the `embedding` field). When
the vector path returns nothing, the read falls back to the S6 keyword
overlap. Writes embed the descriptor at insert time for items of kind
`fact`, `preference`, and `tool_outcome`. Scratchpad items skip embedding.

The classifying write for ambiguous free-form content still uses one
gateway chat call routed `auto_route="memory"`. The embedding call is a
separate gateway endpoint, `POST /v1/embed`, exposed by `gateway.embed()`.

Three honest design choices flagged in the Session 7 notes:
  1. Vector retrieval only. Hybrid retrieval with RRF arrives in a future
     session.
  2. Sliding-window chunking inside `index_document` is heuristic.
     Semantic chunking arrives in Session 8.
  3. The embedding model is fixed at the gateway level. Switching it
     invalidates every FAISS index already built. Treat the model as a
     project-level constant.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from gateway import LLM, embed as _gateway_embed, ensure_gateway
from schemas import MemoryItem, ToolCall, new_id
from vector_index import VectorIndex

STATE_PATH = Path(__file__).parent / "state" / "memory.json"
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

# Kinds for which an embedding is computed at write time. Scratchpad items
# are run-scoped and skip the vector path.
_EMBEDDABLE_KINDS = {"fact", "preference", "tool_outcome"}


# ── persistence ─────────────────────────────────────────────────────────────

def _load() -> list[MemoryItem]:
    if not STATE_PATH.exists():
        return []
    raw = json.loads(STATE_PATH.read_text())
    return [MemoryItem.model_validate(r) for r in raw]


def _save(items: list[MemoryItem]) -> None:
    STATE_PATH.write_text(
        json.dumps([i.model_dump(mode="json") for i in items], indent=2)
    )


# ── vector index ────────────────────────────────────────────────────────────

def _index() -> VectorIndex:
    """Return a freshly-loaded FAISS index every call.

    Re-reading the index file is cheap at S7 scale and keeps the agent
    process consistent with writes made by the MCP subprocess (which runs
    `index_document` in a separate Python process and persists to the same
    disk files). On cold start (no index files on disk), the index is
    rebuilt from items already persisted in `memory.json`.
    """
    idx = VectorIndex(STATE_PATH.parent)
    if idx.size == 0:
        for item in _load():
            if item.embedding is not None:
                idx.add(item.id, item.embedding)
        if idx.size > 0:
            idx.persist()
    return idx


def _try_embed(text: str, task_type: str) -> list[float] | None:
    """Compute an embedding via the gateway. Returns None if the gateway is
    unavailable. The caller decides whether to persist a non-embedded item."""
    try:
        resp = _gateway_embed(text, task_type=task_type)
        return list(resp["embedding"])
    except Exception as e:
        print(f"[memory] embedding failed ({e!r}); item written without vector")
        return None


# ── keyword search (Session 6 path, used as fallback) ───────────────────────

_STOPWORDS = {
    "the", "is", "a", "an", "of", "to", "and", "or", "in", "on", "for", "at",
    "with", "by", "from", "what", "how", "when", "where", "why", "this", "that",
    "it", "be", "as", "are", "was", "were", "i", "you", "me", "my", "your",
}


def _tokens(text: str) -> set[str]:
    return {
        w for w in re.findall(r"\w+", text.lower())
        if w not in _STOPWORDS and len(w) > 2
    }


def _keyword_search(
    query: str,
    history: list[dict] | None,
    *,
    kinds: list[str] | None,
    top_k: int,
) -> list[MemoryItem]:
    items = _load()
    if kinds:
        items = [i for i in items if i.kind in kinds]
    qtoks = _tokens(query)
    if history:
        for h in history[-3:]:
            qtoks |= _tokens(json.dumps(h, default=str))
    scored: list[tuple[int, MemoryItem]] = []
    for item in items:
        itoks = {w.lower() for w in item.keywords} | _tokens(item.descriptor)
        score = len(qtoks & itoks)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: -x[0])
    return [i for _, i in scored[:top_k]]


# ── vector search (the new S7 path) ─────────────────────────────────────────

def _vector_search(
    query: str,
    *,
    kinds: list[str] | None,
    top_k: int,
) -> list[MemoryItem]:
    qvec = _try_embed(query, task_type="retrieval_query")
    if qvec is None:
        return []
    idx = _index()
    if idx.size == 0:
        return []
    hits = idx.search(qvec, k=top_k * 2 if kinds else top_k)
    if not hits:
        return []
    by_id: dict[str, MemoryItem] = {item.id: item for item in _load()}
    out: list[MemoryItem] = []
    for item_id, _score in hits:
        item = by_id.get(item_id)
        if item is None:
            continue
        if kinds and item.kind not in kinds:
            continue
        out.append(item)
        if len(out) >= top_k:
            break
    return out


def read(
    query: str,
    history: list[dict] | None = None,
    *,
    kinds: list[str] | None = None,
    top_k: int = 8,
) -> list[MemoryItem]:
    """Vector first, keyword as fallback when vector returns nothing."""
    vec_hits = _vector_search(query, kinds=kinds, top_k=top_k)
    if vec_hits:
        return vec_hits
    return _keyword_search(query, history, kinds=kinds, top_k=top_k)


# ── writes ──────────────────────────────────────────────────────────────────

class _Classification(BaseModel):
    """What the LLM classifier returns for an ambiguous free-form write."""

    kind: str
    descriptor: str
    keywords: list[str] = Field(default_factory=list)
    value: dict = Field(default_factory=dict)


def _persist_item(item: MemoryItem) -> MemoryItem:
    """Append `item` to the JSON store and, if it has an embedding, to the
    FAISS index. Returns the same item for caller convenience."""
    items = _load()
    items.append(item)
    _save(items)
    if item.embedding is not None and item.kind in _EMBEDDABLE_KINDS:
        idx = _index()
        idx.add(item.id, item.embedding)
        idx.persist()
    return item


def _fallback_remember(
    raw_text: str, *, source: str, run_id: str, goal_id: str | None,
) -> MemoryItem:
    """Deterministic write when the classifier LLM is unavailable.
    Keyword extraction is naive (top word tokens); kind defaults to fact.
    The embedding is still attempted; if it fails the item persists without
    a vector and stays reachable through the keyword fallback."""
    toks = list(_tokens(raw_text))[:10]
    descriptor = raw_text[:200]
    embedding = _try_embed(descriptor, task_type="retrieval_document")
    item = MemoryItem(
        id=new_id("mem"),
        kind="fact",
        keywords=toks,
        descriptor=descriptor,
        value={"raw": raw_text},
        embedding=embedding,
        source=source,
        run_id=run_id,
        goal_id=goal_id,
    )
    return _persist_item(item)


def remember(
    raw_text: str,
    *,
    source: str,
    run_id: str,
    goal_id: str | None = None,
) -> MemoryItem:
    """LLM-classified write for ambiguous content (user input, free-form
    observation). One classifier call plus one embed call. If the
    classifier fails, the deterministic fallback handles the write."""
    ensure_gateway()
    schema = _Classification.model_json_schema()
    try:
        reply = _llm_classify(raw_text, schema)
    except Exception as e:
        print(f"[memory.remember] classifier failed ({e!r}); falling back to fact-write")
        return _fallback_remember(raw_text, source=source, run_id=run_id, goal_id=goal_id)

    parsed = reply.get("parsed") or {}
    # NOTES_RUNS §6 (2): the classifier at temp=1.0 sometimes returns an
    # empty `value` dict (the C-run-1 birthday case), discarding the only
    # structured handle to the raw content. If `value` is empty or missing,
    # fall back to {"raw": raw_text} so the originating text is at least
    # always retrievable from the saved item.
    parsed_value = parsed.get("value")
    if not parsed_value:
        parsed_value = {"raw": raw_text}
    c = _Classification.model_validate({
        "kind": parsed.get("kind", "fact"),
        "descriptor": parsed.get("descriptor", raw_text[:120]),
        "keywords": parsed.get("keywords") or list(_tokens(raw_text))[:10],
        "value": parsed_value,
    })

    embedding: list[float] | None = None
    if c.kind in _EMBEDDABLE_KINDS:
        embedding = _try_embed(c.descriptor, task_type="retrieval_document")

    item = MemoryItem(
        id=new_id("mem"),
        kind=c.kind,  # type: ignore[arg-type]
        keywords=[k.lower() for k in c.keywords],
        descriptor=c.descriptor,
        value=c.value,
        embedding=embedding,
        source=source,
        run_id=run_id,
        goal_id=goal_id,
    )
    return _persist_item(item)


def _llm_classify(raw_text: str, schema: dict) -> dict:
    return LLM().chat(
        prompt=(
            "Classify the following content into a JSON memory record.\n\n"
            f"CONTENT: {raw_text!r}\n\n"
            "Return a JSON object with these fields:\n"
            "- kind ∈ {fact, preference, tool_outcome, scratchpad}.\n"
            "- descriptor: one short human-readable line. MUST include any\n"
            "  specific dates (e.g. '15 May 2026'), numbers, names, places,\n"
            "  or other concrete entities present in the content — these are\n"
            "  what later retrieval will key off. 'Mom's birthday is on 15\n"
            "  May 2026' is a good descriptor; 'birthday and reminder\n"
            "  schedule' is a bad descriptor.\n"
            "- keywords: 3-8 lowercase search keywords pulled from the content.\n"
            "- value: a dict with structured fields (entities, dates,\n"
            "  attributes). MUST NOT be empty when the content has any\n"
            "  identifiable entity — if you cannot classify a specific\n"
            "  attribute, include {\"raw\": <the original content>}."
        ),
        auto_route="memory",
        provider="g",
        response_format={
            "type": "json_schema",
            "schema": schema,
            "name": "Classification",
            "strict": True,
        },
        temperature=1.0,
    )


def record_outcome(
    *,
    tool_call: ToolCall,
    result_text: str,
    artifact_id: str | None,
    run_id: str,
    goal_id: str | None,
) -> MemoryItem:
    """Zero-LLM-classify write for a deterministic tool outcome. Kind is
    `tool_outcome` by construction. Embedding is computed from the
    descriptor so the outcome remains retrievable by semantic similarity."""
    arg_words = []
    for v in tool_call.arguments.values():
        if isinstance(v, str):
            arg_words += _tokens(v)
        elif isinstance(v, (int, float)):
            arg_words.append(str(v))
    keywords = list({tool_call.name.lower(), *arg_words})[:10]

    descriptor = f"{tool_call.name}({json.dumps(tool_call.arguments)[:80]}) -> "
    if artifact_id:
        descriptor += f"artifact {artifact_id}"
    else:
        descriptor += result_text[:120].replace("\n", " ")

    embedding = _try_embed(descriptor, task_type="retrieval_document")

    item = MemoryItem(
        id=new_id("mem"),
        kind="tool_outcome",
        keywords=keywords,
        descriptor=descriptor,
        value={
            "tool": tool_call.name,
            "arguments": tool_call.arguments,
            "result_preview": result_text[:400],
        },
        artifact_id=artifact_id,
        embedding=embedding,
        source="action",
        run_id=run_id,
        goal_id=goal_id,
    )
    return _persist_item(item)


def add_fact(
    descriptor: str,
    *,
    value: dict | None = None,
    keywords: list[str] | None = None,
    source: str,
    run_id: str,
    goal_id: str | None = None,
) -> MemoryItem:
    """Direct fact write used by document-indexing tools. Skips the LLM
    classifier (kind is known) but still embeds the descriptor."""
    embedding = _try_embed(descriptor, task_type="retrieval_document")
    item = MemoryItem(
        id=new_id("mem"),
        kind="fact",
        keywords=list({k.lower() for k in (keywords or list(_tokens(descriptor))[:10])}),
        descriptor=descriptor,
        value=value or {},
        embedding=embedding,
        source=source,
        run_id=run_id,
        goal_id=goal_id,
    )
    return _persist_item(item)


def clear() -> None:
    """Wipe persistent memory and the vector index. Useful between
    assignment attempts."""
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    VectorIndex(STATE_PATH.parent).clear()
