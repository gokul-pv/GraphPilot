# Browser Automation

[Repository overview](../README.md) · [Recorded Hugging Face demo](https://www.youtube.com/watch?v=74mO8zHaTCM)

The browser skill fetches pages and interacts with websites as a step in the DAG. It uses Playwright Chromium, with text and vision calls sent through the LLM gateway. The Hugging Face demo illustrates discovering text-generation models and comparing their details; it is a recorded example, not a fresh validation run.

## Four execution paths

| Path | How it works |
| --- | --- |
| `extract` | Fetches HTML and extracts useful text without a browser-control model call. |
| `deterministic` | Runs supplied Playwright selector actions, such as click or fill. Used only when selectors are provided. |
| `a11y` | Gives a text model an indexed representation of page elements and executes its actions. |
| `vision` | Gives a vision model annotated screenshots so it can act when the earlier interaction path fails. |

The skill tries extraction first, then supplied selector actions, then accessibility interaction, and finally vision. It stops when a path succeeds. A reported deterministic-action failure can end the attempt before later paths. Detected CAPTCHA, login-wall, and anti-bot markers return `gateway_blocked`; the skill does not solve those challenges. The orchestrator can then request a different plan.

The planner supplies the target URL and goal in node metadata. Results include the selected `path`, content, actions, turn count, and final URL. The [implementation](../agent/browser/skill.py) owns the browser loop; the [catalog](../agent/agent_config.yaml) exposes it to the planner.

## Run the example

Complete the [dependency setup](dag-orchestrator.md#setup), configure Gemini for the default accessibility path, and start the gateway on port 8109. Install Chromium and run the query, starting at the repository root:

```bash
cd agent
uv run python -m playwright install chromium
uv run python flow.py "Find the top 3 most-liked Text Generation models on Hugging Face and compare their architectures, parameter counts, and use cases."
```

The planner may use browser discovery, parallel workers for model details, extraction, critic checks, and formatting. The graph is generated for each request; this command does not enforce a fixed two-stage workflow. Results depend on the live site, access restrictions, and configured models.

Web research through the researcher skill also uses Chromium for fetched pages. Its optional Tavily key belongs in `agent/.env`; gateway provider keys belong in the root `.env`.

## Inspect a run

The CLI prints the session ID. From `agent/`, replace `SESSION_ID` with that ID:

```bash
uv run python replay.py SESSION_ID
```

Press `o` on a browser node to inspect its output and chosen path. Node records live under `agent/state/sessions/SESSION_ID/nodes/`. Accessibility and vision runs can save screenshots and element legends under `agent/state/sessions/SESSION_ID/browser/`, grouped by attempt and layer. Extraction-only runs do not produce a screenshot trail.

Use the gateway dashboard at <http://localhost:8109> and `GET /v1/cost/by_agent?session=SESSION_ID` for recorded usage. The checkout provides terminal replay and saved artifacts; it does not include an HTML report generator or a bundled replay report.
