# DAG Orchestrator

[Repository overview](../README.md) · [Recorded DAG demo](https://www.youtube.com/watch?v=7seKiAq5N_o)

The orchestrator turns a request into a dependency graph of skills. It uses NetworkX to store the graph and asyncio to run ready nodes in parallel. The main implementation is [flow.py](../agent/flow.py); skill dispatch lives in [skills.py](../agent/core/skills.py).

## Setup

Use Python 3.11+ and `uv`. Run these commands from the repository root to install the gateway and agent dependencies in their separate environments:

```bash
cd llm_gateway
uv sync
cd ../agent
uv sync
cd ..
```

`uv sync` reads each project's `pyproject.toml`, creates its `.venv`, and includes the default development dependencies. `pyproject.toml` plus `uv.lock` are the only dependency manifests.

Configure the root `.env` and start the gateway as shown in the [quick start](../README.md#quick-start). For web research, install Chromium as described in the [browser guide](browser-automation.md#run-the-example). Optionally copy `agent/.env.example` to `agent/.env` and set a real Tavily key; otherwise search uses DDGS. The MCP server is launched automatically for tool-using skills.

Run from the repository root, with the gateway running:

```bash
cd agent
uv run python flow.py "Find the populations of London, Paris, and Berlin and compare them."
# Or enter queries interactively:
uv run python flow.py --interactive
```

## Execution and recovery

- **Planning:** the planner emits skills and input references. A node can receive `USER_QUERY`, earlier node outputs, or artifact handles. Worker-specific questions go in `metadata.question`.
- **Parallel work:** ready nodes run together in a batch. For a city comparison, the planner can create one researcher per city and a formatter that depends on all three. The exact graph is chosen by the model.
- **Graph growth:** skills can return successors. YAML can also declare automatic successors, such as `coder` followed by `sandbox_executor`.
- **Critic checks:** `critic: true` inserts checks on outgoing edges to non-critic children. The distiller enables this by default. A failed verdict skips the associated child and can request recovery, capped once per target node during a run.
- **Recovery planning:** eligible task failures invoke the planner with the error and completed non-planner/non-critic results. Transient gateway errors, malformed plans, missing desktop permissions/windows, and planner failures do not trigger this replanning path. Unrelated branches can continue.

The executor has a 60-node execution cap. Recovery and critic checks can still leave an incomplete answer; they do not guarantee task success.

## Sessions, replay, and memory

Each run prints a session ID. State is stored under `agent/state/sessions/<session_id>/`: `graph.json` holds the graph, `query.txt` holds the request, and `nodes/` holds prompts, results, timing, and status.

From `agent/`, replace `SESSION_ID` with an actual saved ID:

```bash
uv run python flow.py --resume SESSION_ID
uv run python replay.py SESSION_ID
```

Resume retains completed work and resets nodes saved as running to pending. An interrupted browser or desktop action may therefore run again. Replay is a terminal viewer: Enter advances, `p` expands the stored prompt, `o` expands output, and `q` exits.

The [web console](../frontend/README.md) reads the same sessions in a browser, drawing the graph and showing each node's prompt, tokens, and media.

Memory uses FAISS search with keyword fallback. Hits are loaded at session start and shared with the standard skill prompts. Keep embedding configuration consistent when reusing stored memory; see the [gateway guide](../llm_gateway/README.md#embeddings).

## Skills and extension points

The [skill catalog](../agent/agent_config.yaml) declares prompt paths, descriptions, allowed tools, generation settings, optional provider pins, automatic successors, and critic checks. Add a catalog entry and prompt file for a new standard LLM skill. New tool integrations or custom execution behavior also require Python changes.

Browser, computer, and sandbox execution have dedicated dispatch paths. The [coder prompt](../agent/prompts/coder.md) remains a stub. The subprocess runner exists, with a 30-second default timeout and truncated captured output; it provides no full filesystem or network isolation.

## Validation status

The linked video is historical demo evidence. Documentation checks covered source behavior, links, command syntax, and package metadata; model-backed tasks and desktop workflows were not rerun.

Existing focused tests cover recovery, critic insertion, and reuse of completed results. After setup, run them from `agent/`:

```bash
uv run python -m pytest tests/test_recovery.py tests/test_recovery_amnesia.py tests/test_critic_autoinsert.py
```

With the gateway running, `uv run python flow.py "what is 2+2"` from `agent/` is the quickest end-to-end check. `uv run pytest tests/` runs the full suite.
