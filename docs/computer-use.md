# Computer Use on macOS

[Repository overview](../README.md) · [DAG orchestration](dag-orchestrator.md) · [Browser automation](browser-automation.md)

The computer skill controls applications on your Mac through `cua-driver`. The DAG planner can schedule it alongside research and other skills. Its own control loop reads the application, chooses actions, and returns the selected path, actions, turn count, and any extracted content. Model calls go through the [LLM gateway](../llm_gatewayV9/README.md).

[Watch the recorded Notes demo](https://www.youtube.com/watch?v=Zgq-_iuWEkM): the agent creates a note with its cursor overlay enabled. This recording is historical evidence; the desktop examples were not rerun for this documentation update.

## Five execution paths

| Path | What it does |
| --- | --- |
| `ax_extract` | Reads text from the accessibility tree for suitable read-only requests, without a model call. |
| `deterministic` | Executes supplied actions, such as shortcuts or indexed clicks, without a model call. |
| `ax_llm` | Sends indexed accessibility elements to a text model, then executes its chosen actions. |
| `electron` | Uses the Chrome DevTools Protocol to inspect and control an Electron app's DOM with model guidance. |
| `vision` | Sends annotated screenshots to a vision model and uses window-relative coordinates for actions. |

Selection depends on the goal, application, and available UI information. Read-only extraction is skipped for editing tasks. Supplied actions can run directly; native apps usually use accessibility, while Electron apps use their debugging connection. Vision is a fallback when earlier control attempts fail and a target window is available. A run does not necessarily visit every path.

## Setup

Use macOS, Python 3.11+, and the [agent dependency setup](dag-orchestrator.md#setup). Install CuaDriver and its CLI at `~/.local/bin/cua-driver`, and grant Accessibility and Screen Recording permissions using the [desktop driver reference](desktop-driver.md).

Configure the gateway on port 8109 with Gemini for the default text-control path and a vision-capable model for screenshots. Provider keys belong in the repository root `.env`; see the [gateway setup](../llm_gatewayV9/README.md#start-and-configure). Start the gateway separately, then start the driver daemon:

```bash
open -n -g -a CuaDriver --args serve
```

These tasks operate your host desktop. Notes and VS Code must be installed for their respective examples. The VS Code Electron example closes an existing Code process before relaunching with debugging enabled, so save open work first.

## Run the examples

Start at the repository root. Notes creates an “Agent Test Note”; the VS Code examples write “Hello World” to `~/Desktop/greeting.txt`. Choose the command for the example you want:

```bash
cd agent
# Notes through accessibility and a text model:
uv run python -m computer.tasks.task_notes
# VS Code through Electron DOM control:
uv run python -m computer.tasks.task_vscode --electron
# VS Code through screenshots:
uv run python -m computer.tasks.task_vscode --vision
```

These scripts call the desktop driver directly. For planner-managed work, run a request through `flow.py` from `agent/`:

```bash
uv run python flow.py "Open Notes and create a note titled Agent Test with the body Hello from the agent."
```

The planner chooses the graph for that request. The [computer skill](../agent/computer/skill.py) handles desktop execution and returns its result to the orchestrator.

## Actions and recordings

DAG runs save node outputs under `agent/state/sessions/SESSION_ID/nodes/`, including recorded actions on successful computer nodes. From `agent/`, inspect a saved DAG session with:

```bash
uv run python replay.py SESSION_ID
```

Replace `SESSION_ID` with the printed ID. This terminal viewer shows node data; it does not play desktop video.

Computer-node recordings are attempted under `agent/state/sessions/SESSION_ID/computer/trajectory/NODE_ID/`; vision screenshots go under `computer/screenshots/NODE_ID/`. Recording is best-effort. Standalone scripts instead print their own recording directory, such as `state/sessions/notes-TIMESTAMP/computer/trajectory/`. They do not create a DAG graph for `replay.py`. Use the driver reference for native recording and playback commands.
