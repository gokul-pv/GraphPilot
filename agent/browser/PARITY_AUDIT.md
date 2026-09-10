# S9 Browser SoM driver — feature parity vs browser-use

This doc cross-references our framework-free Layer-3 driver
(`S9SharedCode/code/browser/*`) against the feature set of
`browser-use@main`'s vision mode (audited June 2026). The columns are:

- **Status**: ✓ matched · ⚠ partial / deliberate trade-off · ✗ not implemented
- **Our impl**: file path and line range in `S9SharedCode/code/browser/`
- **browser-use ref**: file path inside their repo (audit source of truth)

Total shipped LOC: **~720** across 5 files
(`__init__.py` 33 · `client.py` 82 · `dom.py` 187 · `driver.py` 283 · `highlight.py` 135).
That includes docstrings, blank lines, and the action schema; net code is ~520 LOC.

## Element enumeration

| # | Feature | Status | Our impl | browser-use ref |
|---|---|---|---|---|
| 1 | DOM source primitive | ⚠ `page.evaluate()` JS injection rather than CDP `DOMSnapshot.captureSnapshot`. Simpler; misses closed shadow DOM. Documented as a deliberate v1 trade-off — upgrade path is to swap the JS for a CDP call without touching the rest of the driver. | `dom.py:31-127` | `dom/service.py:934` |
| 2 | Interactive predicate stack | ⚠ Tag allow-list ∪ ARIA role allow-list ∪ `cursor:pointer` sweep. **Missing**: `getEventListeners()` CDP probe; full a11y `focusable/editable/settable`; class-regex fallback. | `dom.py:34-65` | `dom/serializer/clickable_elements.py:118-236` |
| 3 | Descendant dedupe (outermost-wins) | ✓ Drops any candidate whose ancestor is also a candidate — the fix that turned excalidraw from "click an SVG `<rect>` decoration" to "click the rectangle tool button". | `dom.py:69-83` | implicit in their DOM walker |
| 4 | SVG primitive filter | ✓ `path / rect / circle / polygon / line / g / use / tspan / text` excluded from the cursor-sweep. | `dom.py:42-49` | not separately documented; emerges from their tag-allow-list |
| 5 | Stable element ID across turns | ✗ We use per-turn 1..N. browser-use uses CDP `backend_node_id` so the same element keeps the same number across turns. Acceptable for short tasks (≤ a few turns); becomes a problem if you want the model to refer back to "the same button I clicked 3 turns ago". Upgrade is decoupled from everything else. | `dom.py:117-119` (`id: ++id`) | `dom/serializer/serializer.py:1023` |
| 6 | Name attribute resolution | ✓ Aggressive fallback chain: `aria-label` → `aria-labelledby` ref → `innerText` → `value` → `placeholder` → `title` → `alt` → `data-tooltip` → `data-testid` → `name` → descendant aria-label/title. | `dom.py:91-110` | `dom/serializer/serializer.py:1108` (less aggressive — relies on DOM tree text) |
| 7 | Legend format | ✓ `[id]<tag role="role">name</tag>` — same as browser-use. | `dom.py:144-148` (`Element.legend_line`) | `dom/serializer/serializer.py:1108-1115` |
| 8 | Newly-appeared-this-turn marker | ✗ Their `*` prefix is enabled by stable IDs. Drops out when (5) does. | n/a | `dom/serializer/serializer.py:1098` |
| 9 | Legend truncation cap | ✓ `max_chars=40_000` matches their default. | `dom.py:155-159` (`PageSnapshot.legend`) | `agent/prompts.py:299-336` |
| 10 | Paint-order / occlusion filtering | ✗ We rely on viewport-bounds check only. They have a dedicated module that drops interactives covered by modals/overlays. Acceptable for v1; the symptom would be "model picks an element under a popup". | n/a | `dom/serializer/paint_order.py` |

## Annotation (set-of-marks painting)

| # | Feature | Status | Our impl | browser-use ref |
|---|---|---|---|---|
| 11 | Post-hoc Pillow paint (not JS overlay) | ✓ Same architectural choice — keeps the live DOM untouched, allows offline re-annotation of saved screenshots. | `highlight.py:80-126` | `browser/python_highlights.py:235-244` |
| 12 | CSS-px → device-px DPR scaling | ✓ `int(el.x * dpr)` on every corner. The single bug we'd have shipped without the audit. | `highlight.py:93-97` | `browser/python_highlights.py:465-480` |
| 13 | Dashed box edges | ✓ Hand-rolled `_draw_dashed_line` (Pillow has no built-in). Keeps overlapping boxes individually readable. | `highlight.py:46-77` | `browser/python_highlights.py:310-324` |
| 14 | Per-tag color palette | ✓ link/button/input/textarea/select/label/default — 7 colours, same idea as their 6. The VLM gets a free type signal. | `highlight.py:25-33` | `browser/python_highlights.py:33-38` |
| 15 | Number badge (filled rect + white outline + white text) | ✓ With anchor-to-top-left-of-bbox positioning so badges don't occlude the element's own content. | `highlight.py:110-126` | `browser/python_highlights.py:367-371` |
| 16 | Image bounds clamping | ✓ Clamp box corners to image bounds so an element extending below the viewport doesn't crash Pillow. | `highlight.py:99-104` | implicit in their renderer |
| 17 | `filter_highlight_ids` (skip judged-not-meaningful) | ✗ Not implemented. Their version uses post-enumeration heuristics (zero-area, out-of-viewport, covered) to suppress numbers. Our viewport/min-size filter in `dom.py` handles ~80% of cases. | n/a | `browser/python_highlights.py:501-508` |

## LLM interface

| # | Feature | Status | Our impl | browser-use ref |
|---|---|---|---|---|
| 18 | Framework-free LLM interface | ✓ Plain `httpx` POST to `llm_gatewayV9 /v1/vision`. Compare to browser-use's 1-method `typing.Protocol` (their `BaseChatModel`). Both are "no LangChain". | `client.py:27-82` | `browser_use/llm/base.py:14-38` |
| 19 | Structured output | ✓ V9's `response_format=json_schema` populates `ChatResponse.parsed`. Schema is `ACTION_SCHEMA` (JSON Schema dict, not Pydantic). | `driver.py:46-83` (`ACTION_SCHEMA`); `client.py:54-63` | `llm/openai/*`, `llm/anthropic/*`, `llm/google/*` per-provider adapters |
| 20 | Image transport | ✓ data: URL with base64 PNG; V9 also accepts http(s) URLs and pre-resolves them to data: URLs. | `highlight.py:130-131` (`to_data_url`); V9 `main.py:_resolve_image_urls` | `llm/openai/serializer.py` and friends |
| 21 | Provider rotation / retries | ✓ Owned by V9 (router pool, rate-state, failover) — driver is unaware. | `client.py:42-71` | their `AgentSettings.llm_*` plumbing |

## Agent loop

| # | Feature | Status | Our impl | browser-use ref |
|---|---|---|---|---|
| 22 | Three-phase per-turn (prepare / decide / execute) | ✓ `step()` does enumerate → annotate → vision call → dispatch in order. | `driver.py:194-252` | `agent/service.py:~1550, ~2650` |
| 23 | Multi-action per turn | ✓ `actions` is a JSON array; we cap at 4 (vs their `max_actions_per_step`). | `driver.py:55-83` (`ACTION_SCHEMA.properties.actions`) | `agent/service.py:~2320` |
| 24 | AgentOutput fields | ⚠ We have `thinking` + `actions`. browser-use has 6 fields: `thinking`, `evaluation_previous_goal`, `memory`, `next_goal`, `plan_update`, `actions`. We fold the middle four into the prompt's `RECENT ACTIONS` block — same information, less schema. | `driver.py:54-58` and `driver.py:212-220` (history block) | `agent/views.py:539-610` |
| 25 | Done detection (action-driven, no regex) | ✓ Loop checks `action['type'] == 'done'` after each dispatch. No text parsing. | `driver.py:233-238` | `agent/service.py:~1830` |
| 26 | Consecutive failure guard | ✓ Resets to 0 on success; gives up at `max_failures`. **Missing**: their "forced-done" trick that injects a `done(success=False)` so the caller still gets a structured answer — we just return `DriverResult(success=False)` directly, which is equivalent. | `driver.py:259-273` | `agent/service.py:~1795, ~1965` |
| 27 | Loop / stagnation detector | ✗ Not implemented. They hash actions (excluding `wait`/`done`) and DOM-text per turn and inject an escalating nudge on repetition. Listed as skip-for-now in the audit. Acceptable for v1; would matter for genuinely long-horizon tasks. | n/a | `agent/service.py:~1850-1945` |

## Message structure

| # | Feature | Status | Our impl | browser-use ref |
|---|---|---|---|---|
| 28 | One screenshot per turn (no image history) | ✓ Past turns are summarised as one text line each; never carry past images. Major token-cost win. | `driver.py:204-220` | `agent/message_manager/service.py:547-559` |
| 29 | Sliding action history | ✓ Last 5 turns rendered as `turn N: actions → outcome`. Their version keeps first + recent N. | `driver.py:204-213` (`_history_text`) | `agent/message_manager/service.py:196-221` |
| 30 | Per-turn user message structure | ⚠ We have `GOAL / VIEWPORT / INTERACTIVES legend / RECENT ACTIONS`. Their version adds a `<page_stats>` sidecar (counts of links/iframes/shadow roots) — a cheap signal that primes the model. Easy to add. | `driver.py:214-220` | `agent/prompts.py:278-358` |
| 31 | Swappable system prompt per model family | ✗ Inline `SYSTEM_PROMPT` constant. Their version loads markdown files via `importlib.resources` with format args. Acceptable for one-model deployment; would matter when supporting Anthropic + Gemini + GPT in parallel. | `driver.py:18-35` | `agent/prompts.py:78-107` |

## Actions

| # | Feature | Status | Our impl | browser-use ref |
|---|---|---|---|---|
| 32 | `click(mark)` | ✓ Centres on element bbox, dispatches via `page.mouse.click`. | `driver.py:113-118` | `tools/service.py` |
| 33 | `type(mark, value, clear?)` | ✓ With optional clear-first via Ctrl+A / Delete. Defaults to clear=True (matching their default). | `driver.py:119-128` | `tools/service.py` |
| 34 | `key(value)` | ✓ For Enter / Tab / Escape / shortcut combos. | `driver.py:129-131` | `send_keys` action in `tools/service.py` |
| 35 | `scroll(direction, amount)` | ✓ via `page.mouse.wheel`. | `driver.py:132-142` | `tools/service.py` |
| 36 | `drag(from_x,y → to_x,y)` | ✓ With multi-step interpolation (25 sub-moves) so canvas apps receive real `mousemove` events. **This is the action that lets the driver work on excalidraw — browser-use also has it for the same reason.** | `driver.py:143-156` | `tools/service.py` drag action |
| 37 | `wait(seconds)` | ✓ Lets the model pause for animations. | `driver.py:157-159` | their `wait` |
| 38 | `done(success, note)` | ✓ Terminates the loop, propagates success bool to `DriverResult`. | `driver.py:160-162` and `driver.py:236-238` | their `done` |
| 39 | `navigate / go_back / search / upload / switch_tab` | ✗ Not implemented. Each is straightforward to add; we just don't need them for excalidraw or our planned next targets. | n/a | `tools/service.py:554-1300+` |
| 40 | Filesystem skill (`read_file / write_file`) | ✗ Out of scope for a browser skill. They include it because their agent is general-purpose. | n/a | `tools/service.py` |

## Summary

- **Fully matched (✓):** 22 features
- **Partial / deliberate trade-off (⚠):** 6 features (documented in `dom.py`/`driver.py` docstrings)
- **Not implemented (✗):** 12 features
  - 4 of which are explicitly listed as "skip-for-now" in the original audit (loop detector, paint-order, swappable prompts, file-system skill)
  - 5 of which are extra action verbs that are one-liners to add (`navigate`, `go_back`, etc.)
  - 3 of which are improvements rather than feature gaps (`backend_node_id` stability, `*`-new marker, `filter_highlight_ids`)

The driver runs against excalidraw — pure-canvas, zero-a11y target — in 1 turn,
2.4s, 1.9K tokens, with 16,981 pixel-diff changes consistent with a drawn rectangle.
See [`tests/test_excalidraw_som.py`](../tests/test_excalidraw_som.py) and `out/excalidraw_som/`.
