"""Decision: one LLM call per turn.

Given the current goal, the relevant memory hits (descriptors only), the
recent history, and optionally the raw bytes of an artifact Perception
attached to this goal, the model picks ONE of:

  (a) answer in plain text — the answer may itself be summarisation,
      extraction, comparison, translation, or any other semantic work the
      LLM does on the attached content;
  (b) call exactly one MCP tool from the available tool list.

There is no taxonomy of "operation kinds". The model decides what it is
doing. Decision just routes the dispatch.
"""

from __future__ import annotations

import json

from gateway import LLM, ensure_gateway
from schemas import DecisionOutput, Goal, MemoryItem, ToolCall

SYSTEM = (
    "You are the Decision layer of an agent.\n"
    "Inputs you receive: ONE current goal, the relevant memory snippets,\n"
    "recent history, and optionally the raw bytes of one attached artifact.\n\n"
    "Choose EXACTLY ONE response:\n"
    "  (a) Reply with the final answer to this goal as plain text. If the\n"
    "      goal asks you to summarise, extract, compare, or transform the\n"
    "      attached content, do that work inside your reply.\n"
    "  (b) Call exactly ONE tool from the available MCP tools when you need\n"
    "      external work (fetching, file ops, time, currency, web search).\n\n"
    "Rules:\n"
    "- Never narrate. Answer or call a tool, never both.\n"
    "- Never invent a tool that is not in the tool list.\n"
    "- If the goal is already satisfied by the memory hits + history, answer\n"
    "  directly without calling a tool.\n"
    "- Artifact handles (strings starting with `art:`) are NOT file paths,\n"
    "  URLs, or tool arguments. NEVER pass an `art:...` value to read_file,\n"
    "  list_dir, fetch_url, or ANY other tool. If a goal needs the bytes of\n"
    "  an artifact, those bytes will already appear in the ATTACHED\n"
    "  ARTIFACTS section of your input — answer directly from that text.\n"
    "  WRONG:  read_file({\"path\": \"art:abc1234\"})\n"
    "  WRONG:  fetch_url({\"url\": \"art:abc1234\"})\n"
    "  RIGHT:  read the bytes already in ATTACHED ARTIFACTS and answer.\n"
    "- read_file and list_dir operate on the local sandbox/ directory, not\n"
    "  artifacts. Only call them when the user has asked you to read/list a\n"
    "  real sandbox file by name.\n"
    "- Answer using whatever is in front of you: memory hits, history, and\n"
    "  any attached artifact bytes. Be substantive — at least 3 sentences\n"
    "  or a list of items when the goal is to extract/list/select/compare.\n"
    "- For 'remember X', 'save X', 'set a reminder', 'note X' style goals,\n"
    "  call create_file (or update_file when re-saving) under the sandbox\n"
    "  with a filename describing the topic. Do NOT reply that you cannot\n"
    "  set reminders — create_file IS how you set them.\n"
    "- When the goal asks to make a file's or fetched content's contents\n"
    "  SEARCHABLE for later turns or runs (phrasings like 'index', 'ingest',\n"
    "  'make searchable', 'add to the knowledge base', 'load into memory'),\n"
    "  call `index_document`. `read_file` only returns the bytes once and\n"
    "  then discards them; `index_document` chunks the content and writes\n"
    "  the chunks into Memory so they survive across turns and runs. Use\n"
    "  `read_file` only for one-shot inspection of a known sandbox file.\n"
    "- When the goal asks to ANSWER a question and the MEMORY HITS already\n"
    "  contain `fact` items whose descriptors begin with `[sandbox:` or\n"
    "  `[art:` (those are previously-indexed chunks of source documents),\n"
    "  call `search_knowledge` against the question rather than re-fetching\n"
    "  the URL or re-reading the file. The indexed chunks are why the\n"
    "  corpus was indexed in the first place; re-fetching is wasted work.\n"
    "  The chunk text for each indexed hit is shown inline under the hit's\n"
    "  descriptor (`chunk: ...`); synthesise directly from those previews\n"
    "  rather than re-issuing the same vector query."
)

# How much attached content to send to the model per turn. Most LARGE-tier
# workers handle 30 KB comfortably; truncate above that and let the model
# work with a head-and-tail window.
ATTACH_HEAD = 20_000
ATTACH_TAIL = 10_000


def _format_hits(hits: list[MemoryItem]) -> str:
    # Surface enough of each hit's `value` for Decision to anchor on it.
    # NOTES_RUNS §6 (2) handled the classifier-fact case (`value.raw` such
    # as the birthday date). NOTES_FIX §3 extends this to indexed-chunk
    # facts: when a hit carries `value.chunk` (an indexed slice of a
    # document), the chunk body IS the answer material, and stripping it
    # leaves Decision unable to synthesise — it sees that chunks exist but
    # cannot read them, so it loops on `search_knowledge`. We render a
    # short chunk preview here so Decision can answer directly from the
    # memory-hit list when search_knowledge has already populated it.
    if not hits:
        return "  (none)"
    out = []
    for h in hits[:10]:
        line = f"  - [{h.kind}] {h.descriptor}"
        val = h.value or {}
        if val:
            raw = val.get("raw")
            chunk = val.get("chunk")
            if isinstance(raw, str) and raw.strip():
                raw_more = "…" if len(raw) > 2000 else ""
                line += f"\n      raw: {raw[:2000]}{raw_more}"
            elif isinstance(chunk, str) and chunk.strip():
                src = val.get("source") or ""
                preview = chunk[:2000].replace("\n", " ")
                more = "…" if len(chunk) > 2000 else ""
                line += f"\n      chunk ({src}): {preview}{more}"
            else:
                compact = {
                    k: v for k, v in val.items()
                    if k != "chunk" and not (isinstance(v, str) and len(v) > 200)
                }
                if compact:
                    line += f"\n      value: {json.dumps(compact)[:240]}"
        out.append(line)
    return "\n".join(out)


def _format_history(history: list[dict]) -> str:
    if not history:
        return "  (empty)"
    lines = []
    for h in history[-6:]:
        kind = h.get("kind", "?")
        if kind == "answer":
            lines.append(f"  - iter {h.get('iter')}: ANSWER → {(h.get('text') or '')[:140]}")
        elif kind == "action":
            tool = h.get("tool")
            # NOTES_RUNS §6 (1): agent7.py already clips result_descriptor at
            # 300 chars; clipping again at 140 here was hiding the tail of
            # multi-entry tool outputs like list_dir's file list, and Decision
            # was confidently treating partial views as complete. Match the
            # 300-char ceiling so the model sees everything that was stored.
            desc = h.get("result_descriptor", "")[:300]
            art = f" (artifact {h['artifact_id']})" if h.get("artifact_id") else ""
            lines.append(f"  - iter {h.get('iter')}: {tool}{art} → {desc}")
        else:
            lines.append(f"  - iter {h.get('iter')}: {kind} {h}")
    return "\n".join(lines)


def _format_attached(attached: list[tuple[str, bytes]]) -> str:
    if not attached:
        return ""
    parts = ["\n\nATTACHED ARTIFACTS:"]
    for art_id, data in attached:
        text = data.decode("utf-8", errors="replace")
        if len(text) > ATTACH_HEAD + ATTACH_TAIL + 50:
            text = (
                text[:ATTACH_HEAD]
                + f"\n\n...[truncated; full size {len(data)} bytes]...\n\n"
                + text[-ATTACH_TAIL:]
            )
        parts.append(f"--- {art_id} ---\n{text}")
    return "\n".join(parts)


def next_step(
    goal: Goal,
    hits: list[MemoryItem],
    attached: list[tuple[str, bytes]],
    history: list[dict],
    mcp_tools: list[dict],
) -> DecisionOutput:
    ensure_gateway()

    prompt = (
        f"GOAL:\n  {goal.text}\n\n"
        f"MEMORY HITS:\n{_format_hits(hits)}\n\n"
        f"RECENT HISTORY:\n{_format_history(history)}"
        f"{_format_attached(attached)}"
    )

    reply = LLM().chat(
        prompt=prompt,
        system=SYSTEM,
        cache_system=True,
        tools=mcp_tools,
        tool_choice="auto",
        auto_route="decision",
        temperature=0,
        max_tokens=2048,
    )

    tcs = reply.get("tool_calls") or []
    if tcs:
        tc = tcs[0]
        return DecisionOutput(
            tool_call=ToolCall(
                name=tc["name"],
                arguments=tc.get("arguments") or {},
            )
        )
    return DecisionOutput(answer=(reply.get("text") or "").strip())
