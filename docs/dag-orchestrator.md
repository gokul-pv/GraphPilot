# DAG Orchestrator

[Repository overview](../README.md) · [Recorded DAG demo](https://www.youtube.com/watch?v=7seKiAq5N_o)

The orchestrator turns a request into a dependency graph of skills. It uses NetworkX to store the graph and asyncio to run ready nodes in parallel. The main implementation is [flow.py](../agent/flow.py); skill dispatch lives in [skills.py](../agent/skills.py).

## Setup

Use Python 3.11+ and `uv`. Run these commands from the repository root to prepare the gateway:

```bash
cd llm_gatewayV9
uv sync
cd ..
```

The current `agent/pyproject.toml` declares `name = "Multi-Agent Orchestrator"`. Spaces make this invalid package metadata, so `uv sync` and `uv run` fail in `agent/`. Until the metadata is fixed, create an environment and install its declared dependencies directly:

```bash
uv venv --python 3.11 agent/.venv
agent/.venv/bin/python - <<'PY'
import subprocess
import sys
import tomllib

with open("agent/pyproject.toml", "rb") as file:
    dependencies = tomllib.load(file)["project"]["dependencies"]
subprocess.run(
    ["uv", "pip", "install", "--python", sys.executable, *dependencies],
    check=True,
)
PY
```

This reads the dependency list without installing the project itself. The older `agent/requirements.txt` does not include all dependencies.

Configure the root `.env` and start the gateway as shown in the [quick start](../README.md#quick-start). For web research, install Chromium as described in the [browser guide](browser-automation.md#run-the-example). Optionally copy `agent/.env.example` to `agent/.env` and set a real Tavily key; otherwise search uses DDGS. The MCP server is launched automatically for tool-using skills.

Run from the repository root, with the gateway running:

```bash
cd agent
.venv/bin/python flow.py "Find the populations of London, Paris, and Berlin and compare them."
# Or enter queries interactively:
.venv/bin/python flow.py --interactive
```

## Execution and recovery

- **Planning:** the planner emits skills and input references. A node can receive `USER_QUERY`, earlier node outputs, or artifact handles. Worker-specific questions go in `metadata.question`.
- **Parallel work:** ready nodes run together in a batch. For a city comparison, the planner can create one researcher per city and a formatter that depends on all three. The exact graph is chosen by the model.
- **Graph growth:** skills can return successors. YAML can also declare automatic successors, such as `coder` followed by `sandbox_executor`.
- **Critic checks:** `critic: true` inserts checks on outgoing edges to non-critic children. The distiller enables this by default. A failed verdict skips the associated child and can request recovery, capped once per target node during a run.
- **Recovery planning:** eligible task failures invoke the planner with the error and completed non-planner/non-critic results. Transient gateway errors, malformed plans, missing desktop permissions/windows, and planner failures do not trigger this replanning path. Unrelated branches can continue.

The executor has a 60-node execution cap. Recovery and critic checks can still leave an incomplete answer; they do not guarantee task success.

## Sessions, replay, and memory

Each run prints a session ID. State is stored under `agent/state/sessions/<session_id>/`: `graph.pkl` holds the graph, `query.txt` holds the request, and `nodes/` holds prompts, results, timing, and status.

From `agent/`, replace `SESSION_ID` with an actual saved ID:

```bash
.venv/bin/python flow.py --resume SESSION_ID
.venv/bin/python replay.py SESSION_ID
```

Resume retains completed work and resets nodes saved as running to pending. An interrupted browser or desktop action may therefore run again. Replay is a terminal viewer: Enter advances, `p` expands the stored prompt, `o` expands output, and `q` exits.

Memory uses FAISS search with keyword fallback. Hits are loaded at session start and shared with the standard skill prompts. Keep embedding configuration consistent when reusing stored memory; see the [gateway guide](../llm_gatewayV9/README.md#embeddings).

## Skills and extension points

The [skill catalog](../agent/agent_config.yaml) declares prompt paths, descriptions, allowed tools, generation settings, optional provider pins, automatic successors, and critic checks. Add a catalog entry and prompt file for a new standard LLM skill. New tool integrations or custom execution behavior also require Python changes.

Browser, computer, and sandbox execution have dedicated dispatch paths. The [coder prompt](../agent/prompts/coder.md) remains a stub. The subprocess runner exists, with a 30-second default timeout and truncated captured output; it provides no full filesystem or network isolation.

## Validation status

The linked video is historical demo evidence. This documentation update checked source behavior, links, command syntax, and setup metadata; it did not rerun model-backed tasks or desktop workflows. The gateway dependency dry run passed; the agent metadata failure above was reproduced.

Existing focused tests cover recovery, critic insertion, and reuse of completed results. After setup, install the test dependencies and run them from `agent/`:

```bash
uv pip install --python .venv/bin/python pytest pytest-asyncio
.venv/bin/python -m pytest tests/test_recovery.py tests/test_recovery_amnesia.py tests/test_critic_autoinsert.py
```

The root demo runner also calls `uv run` inside `agent/`, so it is affected by the same package-name issue. Use the direct Python commands above for this checkout.
