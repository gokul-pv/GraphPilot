"""Perception: the agent's orchestrator.

Runs every loop iteration. Looks at the user's original query, the memory
hits, and the run history so far, and emits the current Observation —
which goals exist, which are done, and whether the next unfinished goal
needs raw bytes from a specific artifact.

Perception never reads artifact bytes. It sees handles + descriptors only.
When a goal needs bytes, Perception flips `send_artifact: true` and points
`artifact_index` at one of the artifacts listed in MEMORY HITS. The outer
loop resolves the index back to the artifact id and attaches the bytes.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from gateway import LLM, ensure_gateway
from schemas import Goal, MemoryItem, Observation, new_id


class _GoalDelta(BaseModel):
    """What the Perception LLM emits per goal. No `id` field — goals are
    identified by their position in the output list. The LLM cannot drift
    identity across iterations because there is no identity field to drift."""

    text: str = Field(max_length=240)
    done: bool = False
    send_artifact: bool = False
    artifact_index: int | None = None


class _PerceptionOutput(BaseModel):
    goals: list[_GoalDelta] = Field(default_factory=list, max_length=10)


SYSTEM = (
    "You are the Perception layer of an agent.\n"
    "Each iteration you see the user's query, the prior goal list, the\n"
    "current memory hits (descriptors only — never raw bytes), and the run\n"
    "history. Return the CURRENT goal list as JSON matching the schema.\n\n"
    "Goals are identified by POSITION in the output array. Always return\n"
    "the goals in the SAME ORDER as PRIOR GOALS. Do not reorder, do not\n"
    "drop a prior goal, do not add a goal in the middle.\n"
    "You MAY append new goals at the END when a discovery action on a\n"
    "prior turn (for example, listing the contents of a directory) reveals\n"
    "concrete items that were unknown at decomposition time. In that case\n"
    "keep all prior goals verbatim and append one new goal per concrete\n"
    "item, then re-append the original synthesis/report goal LAST so it\n"
    "stays the final step.\n\n"
    "You speak at the level of INTENT, not tool selection. Write each goal\n"
    "as a short imperative describing WHAT must happen, not WHICH tool\n"
    "will do it. Decision is the layer that maps intent to a tool; leave\n"
    "that choice to Decision. Example intent verbs you may use: fetch,\n"
    "open, list, look up the time, convert currency, save a note, make\n"
    "this content searchable, query the existing knowledge base, extract,\n"
    "summarise, compare, synthesise. Do not name specific tools.\n\n"
    "Procedure:\n"
    "1. If PRIOR GOALS is empty, decompose the query into one or more short\n"
    "   imperative goals (one per distinct part). If the query asks to\n"
    "   read/fetch/process N items (\"top 3 results\", \"first 5 articles\"),\n"
    "   emit a SEPARATE fetch goal for each item plus the final\n"
    "   synthesis goal — NOT a single umbrella goal.\n"
    "   If the query asks to ingest N files so they can be searched\n"
    "   later, emit one goal per file expressing that its content should\n"
    "   be made searchable, plus a final report goal.\n"
    "   If MEMORY HITS already contain `fact` items whose descriptors\n"
    "   start with `[sandbox:` or `[art:` (these mark previously-indexed\n"
    "   chunks of source documents), the next goal for any question\n"
    "   about that material is to QUERY THE EXISTING KNOWLEDGE BASE\n"
    "   rather than to re-fetch or re-open the original sources. Pair\n"
    "   that query goal with a final synthesis/answer goal — never emit\n"
    "   a knowledge-base query as the only goal, because the user still\n"
    "   needs an answer produced from the returned chunks.\n"
    "   Whenever the user's query is a question (rather than a pure\n"
    "   action like 'save X' or 'fetch Y'), the LAST goal in your\n"
    "   decomposition must be a synthesis/answer goal that emits the\n"
    "   final reply (verbs like answer, tell, summarise, compare, list,\n"
    "   extract, identify, describe).\n"
    "2. Otherwise copy each prior goal's `text` verbatim into the same slot.\n"
    "   Mark `done: true` the moment RUN HISTORY shows an action satisfying\n"
    "   it. Once done, leave it done in every later iteration.\n"
    "3. For the FIRST unfinished goal (lowest-index slot with done=false),\n"
    "   set `send_artifact: true` whenever ANY of these apply:\n"
    "     - the goal text contains extract / summarise / list / synthesise /\n"
    "       analyse / evaluate / select / compare / pick / choose / decide;\n"
    "     - the goal needs information that lives inside a fetched page or\n"
    "       file rather than just in the short descriptor.\n"
    "   In that case pick `artifact_index` = the `i` value (0, 1, 2, ...)\n"
    "   of the most relevant MEMORY HITS entry (entries whose `i` is null\n"
    "   are not artifacts and cannot be picked). When in doubt, attach the\n"
    "   most recent artifact whose descriptor matches the goal topic.\n"
    "4. Only when the goal is purely fetch / search / compute / open / time\n"
    "   should you leave `send_artifact: false` and `artifact_index: null`.\n\n"
    "Example. Given\n"
    '  MEMORY HITS: [{"i":0,"artifact_id":"art:aaa","descriptor":'
    '"page fetch result -> art:aaa"}]\n'
    '  PRIOR GOALS: [{"text":"Fetch the page","done":false,'
    '"send_artifact":false,"artifact_index":null},\n'
    '                {"text":"Extract X","done":false,'
    '"send_artifact":false,"artifact_index":null}]\n'
    "return:\n"
    '  {"goals":[\n'
    '    {"text":"Fetch the page","done":true,'
    '"send_artifact":false,"artifact_index":null},\n'
    '    {"text":"Extract X","done":false,'
    '"send_artifact":true,"artifact_index":0}\n'
    "  ]}"
)


def _snapshot_history(history: list[dict]) -> list[dict]:
    out = []
    for h in history[-10:]:
        clipped = {}
        for k, v in h.items():
            if isinstance(v, str) and len(v) > 2000:
                clipped[k] = v[:2000] + "..."
            else:
                clipped[k] = v
        out.append(clipped)
    return out


def _snapshot_hits(hits: list[MemoryItem]) -> list[dict]:
    """Render the memory hits the LLM sees. Artifacts are indexed (i) so
    Perception can point at them by integer; non-artifact hits show i=null."""
    art_pos = 0
    out = []
    for h in hits[:12]:
        i = None
        if h.artifact_id:
            i = art_pos
            art_pos += 1
        out.append({
            "i": i,
            "kind": h.kind,
            "descriptor": h.descriptor,
            "keywords": h.keywords,
            "artifact_id": h.artifact_id,
        })
    return out


def observe(
    query: str,
    hits: list[MemoryItem],
    history: list[dict],
    prior_goals: list[Goal],
    run_id: str,
) -> Observation:
    ensure_gateway()

    art_ids_in_order = [h.artifact_id for h in hits[:12] if h.artifact_id]

    prior_snapshot = [g.model_dump() for g in prior_goals] if prior_goals else []
    prompt = (
        f"USER QUERY:\n  {query}\n\n"
        f"PRIOR GOALS:\n{json.dumps(prior_snapshot, indent=2)}\n\n"
        f"MEMORY HITS (handles + descriptors only, no raw bytes; `i` is the\n"
        f"artifact_index to pass back when send_artifact is true):\n"
        f"{json.dumps(_snapshot_hits(hits), indent=2)}\n\n"
        f"RUN HISTORY (last 10 events):\n"
        f"{json.dumps(_snapshot_history(history), indent=2, default=str)}\n\n"
        f"Return the current goal list as JSON matching the schema."
    )

    schema = _PerceptionOutput.model_json_schema()
    reply = LLM().chat(
        prompt=prompt,
        system=SYSTEM,
        auto_route="perception",
        provider="g",
        response_format={
            "type": "json_schema",
            "schema": schema,
            "name": "PerceptionOutput",
            "strict": True,
        },
        temperature=1.0,
    )

    parsed = reply.get("parsed")
    if not parsed or not parsed.get("goals"):
        return Observation(goals=[Goal(id=new_id("g"), text=query)])

    # Synthesis-type goals require Decision to actually produce a
    # substantive answer; we won't let Perception declare them done on the
    # strength of a tool-call alone.
    SYNTHESIS_KW = (
        "evaluate", "select", "synthes", "compare", "decide", "recommend",
        "tell me which", "most appropriate", "analy", "pick", "choose",
        "summarise", "summarize", "answer", "identify", "find", "determine",
        "extract", "list", "report", "tell", "explain", "describe", "name",
    )

    # Goal-count invariant: never contract, never reorder. Prior goals keep
    # their slot and id; Perception may APPEND new goals after the prior
    # list when a discovery action (e.g. list_dir) reveals work that wasn't
    # knowable on iter 1. NOTES_RUNS §6 (4): the previous hard-truncate to
    # `len(prior_goals)` blocked F-run-1 verbatim — list_dir revealed five
    # papers, but the goal list was locked to the three placeholders emitted
    # before the listing was known. We still drop appended goals whose text
    # duplicates a prior goal (the temp=1.0 dup-append failure mode that
    # motivated the original lock).
    raw_goals = parsed["goals"]
    if prior_goals:
        prior_texts = {g.text.strip().lower() for g in prior_goals}
        deduped = list(raw_goals[:len(prior_goals)])
        for extra in raw_goals[len(prior_goals):]:
            t = (extra.get("text") or "").strip().lower()
            if not t or t in prior_texts:
                continue
            prior_texts.add(t)
            deduped.append(extra)
        raw_goals = deduped

    out_goals: list[Goal] = []
    for i, d in enumerate(raw_goals):
        delta = _GoalDelta.model_validate(d)
        attach: str | None = None
        if delta.send_artifact and delta.artifact_index is not None:
            if 0 <= delta.artifact_index < len(art_ids_in_order):
                attach = art_ids_in_order[delta.artifact_index]

        gid = prior_goals[i].id if i < len(prior_goals) else new_id("g")
        was_done = prior_goals[i].done if i < len(prior_goals) else False

        proposed_done = was_done or delta.done
        if proposed_done and not was_done:
            gtext_lc = delta.text.lower()
            if any(kw in gtext_lc for kw in SYNTHESIS_KW):
                has_answer = any(
                    h.get("kind") == "answer"
                    and h.get("goal_id") == gid
                    and len((h.get("text") or "")) > 60
                    for h in history
                )
                if not has_answer:
                    proposed_done = False

        out_goals.append(Goal(
            id=gid,
            text=delta.text,
            done=proposed_done,
            attach_artifact_id=attach,
        ))

    # Safety net: if the first unfinished goal needs raw bytes (its text
    # matches a synthesis keyword) AND we have artifacts in memory AND the
    # model forgot to set send_artifact, force-attach the most recent
    # artifact. The LLM at temp=1.0 is otherwise too unreliable about this.
    for g in out_goals:
        if g.done:
            continue
        if g.attach_artifact_id:
            break  # already attached, nothing to do
        if not art_ids_in_order:
            break  # no artifacts available yet
        if any(kw in g.text.lower() for kw in SYNTHESIS_KW):
            g.attach_artifact_id = art_ids_in_order[-1]
        break  # only act on the FIRST unfinished goal
    return Observation(goals=out_goals)
