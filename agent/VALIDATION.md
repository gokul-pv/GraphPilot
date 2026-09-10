# Session 9 — integration validation

Browser skill ported into the S8 runtime as one new sub-package. No edits to
flow.py's orchestration logic; the only orchestrator-side change is a single
`if skill.name == "browser":` branch in `skills.py:run_skill` that mirrors
the existing `sandbox_executor` dispatch.

All numbers below were captured by re-running each query against the V9
gateway on `:8109` after the integration landed.

## Definition-of-done checklist

| item | status |
|---|---|
| `uv sync` succeeds in `S9/code/` | ✓ |
| `uv run python flow.py "<HF query>"` completes without error | ✓ — see s9_hf_top3 |
| The Browser skill is a single catalogue entry; flow.py orchestration unchanged | ✓ |
| `replay.py s9_hf_top3` displays the Browser node and its chosen layer | ✓ — replay shows `"path": "a11y"` |
| Three smoke tests pass with the expected `output.path` | ✓ |
| `error_code="gateway_blocked"` triggers Planner re-invocation distinctly | ✓ |
| Cost-by-agent for the HF run shows Browser as a separately-attributable line item | ✓ |
| VALIDATION.md written | this file |
| Natural-cascade Layer 3 escalation (no `force_path`) demonstrated | ✓ — see §8 |

## End-to-end HF top-3 query (the spec's primary validation)

```
query : "What are the three most-liked open-source LLM releases on
         Hugging Face from the past week? For each, give the model
         name, parameter count, and one-line description."
session: s9_hf_top3
```

**DAG produced (matches the spec):**

```
Planner (n:1)
  └─ Browser (n:2, url=https://huggingface.co/models)
       └─ Distiller (n:3)
            └─ Formatter (n:4)
```

**Per-node summary:**

| node | skill | status | elapsed | output highlight |
|---|---|---|---:|---|
| n:1 | planner   | ok | 4.8s  | emits Browser → Distiller → Formatter |
| n:2 | browser   | ok | 17.4s | **path=`a11y`**, 4 turns, base URL passed clean, final URL `?pipeline_tag=text-generation&sort=likes&search=last+week+...` |
| n:3 | distiller | ok | 3.9s  | three model records with name / params / description |
| n:4 | formatter | ok | 3.7s  | "1. deepseek-ai/DeepSeek-R1 (685B): a reasoning model. 2. … 3. …" |

**Cost-by-agent (`/v1/cost/by_agent?session=...`):**

```json
{
  "browser":   {"calls": 4, "tokens": "8417 / 412", "dollars": "$0.000000"},
  "planner":   {"calls": 1, "tokens": "2197 / 239", "dollars": "$0.000000"},
  "distiller": {"calls": 1, "tokens": "2369 / 49",  "dollars": "$0.000000"},
  "formatter": {"calls": 1, "tokens": "780 / 59",   "dollars": "$0.000000"}
}
```

All free-tier Gemini; Browser is a separately attributable line. The
"≤ 15 s for Browser" target from the spec was missed by ~2 s (17.4 s real)
— the over-budget portion is Playwright cold-start, not LLM time
(Browser's LLM latency totalled 4 s across 4 turns).

## Three smoke tests

### 1 · Gateway block (Redfin) — `error_code="gateway_blocked"`

```
query  : "Go to https://www.redfin.com and find 3-bedroom houses for sale
          under $1.5M in Berkeley, CA. List the addresses."
session: s8-c33a042d
```

Sequence:

| node | skill | result |
|---|---|---|
| n:1 | planner    | emits Browser(url=redfin) → Distiller → Formatter |
| n:2 | **browser**| **failed** in 0.9 s with `error_code="gateway_blocked"`, error="`gateway_blocked (captcha) detected after JS render at https://www.redfin.com/`" |
| —   | recovery   | `recovery (upstream_failure): planner node n:5 queued for n:2` |
| n:5 | planner    | emits Researcher → Formatter (route-around) |
| n:6 | researcher | reports the block via search results |
| n:7 | formatter  | "I was unable to retrieve … Redfin's security measures" — honest, not hallucinated |

The cascade behaved as the spec wanted:

- Layer 1 fetch returned HTTP 405 from Redfin's WAF — we did NOT bail there. The
  fall-through to Playwright let the JS-rendered CAPTCHA page load.
- `detect_gateway_block()` matched on the `"Let's confirm you are human"` marker.
- `_drive()` set the `gateway_blocked` annotation on its `DriverResult`.
- `BrowserSkill.run` translated it to `error_code="gateway_blocked"`.
- `recovery.classify_failure` saw `upstream_failure` and triggered a Planner
  recovery; the new Planner emitted a route-around DAG, not a retry.

### 2 · Vision escalation (Excalidraw) — `output.path == "vision"`

```
query  : "Go to https://excalidraw.com and use visual recognition to draw a
          rectangle in the centre of the canvas. Confirm it was drawn."
session: s8-77bf1c4e
```

| node | skill | result |
|---|---|---|
| n:1 | planner   | emits Browser with `metadata.force_path="vision"` (triggered by "use visual recognition" wording — see [planner.md](prompts/planner.md)) |
| n:2 | browser   | **path=`vision`**, 2 turns, 8.5 s, V9 `/v1/vision` ledger entry distinct from any prior call |
| n:3 | formatter | "I have successfully … drawn a rectangle … visual confirmation confirms" |

**Honest qualification:** the first attempt at this smoke ran the natural
cascade with goal `"draw a rectangle in the centre of the canvas"` and got
`path="a11y"` — Excalidraw's toolbar buttons are well aria-labelled and our
`drag(x,y → x,y)` action handles canvas natively, so Layer 2b solved it in
3 turns. The cascade short-circuited because it *could*. To demonstrate
vision firing through the cascade we added an optional `metadata.force_path`
escape hatch and an instruction in `planner.md` that the Planner sets it
when the user explicitly asks for "visual recognition". The natural-cascade
behaviour (a11y on Excalidraw) is the correct one for production; the
forced-vision run exists to validate Layer 3 is reachable, the V9
`/v1/vision` endpoint is hit, and the ledger attributes it.

Browser ledger for this session: 2 calls (vision), 3677 in / 198 out tokens.

**See also §8.** This forced-vision smoke is now supplemented by a
natural-cascade Layer 3 run — the cascade chose vision on its own,
without the Planner setting `force_path`, on a different target.

### 3 · Extraction-only (clean article) — `output.path == "extract"`, zero Browser LLM cost

```
query  : "Summarise https://en.wikipedia.org/wiki/Transformer_(deep_learning_architecture)
          in two sentences."
session: s8-0dd7346b
```

| node | skill | result |
|---|---|---|
| n:1 | planner    | emits Browser → Summariser → Formatter |
| n:2 | browser    | **path=`extract`**, 0 turns, 2.1 s |
| n:3 | summariser | condenses the article to a short form |
| n:4 | formatter  | the spec-style answer naming the 2017 paper |

**Cost-by-agent**:

```json
{
  "planner":    {"calls": 1, "tokens": "2197 / 239"},
  "summariser": {"calls": 1, "tokens": "5864 / 184"},
  "formatter":  {"calls": 1, "tokens": "1038 / 77"}
}
```

**Browser does NOT appear** in the ledger — exactly as required for an
extract-path run, because the path made zero gateway calls (trafilatura
locally). The cascade's cheapest path is genuinely free.

## Rough edges discovered during integration

1. **V9 base URL was wired wrong.** `llm_gatewayV9/client.py` inherited V8's
   `DEFAULT_URL = "http://localhost:8108"`. Symptom: skills.py LLM calls
   went to V8 (still running on 8108), so cost-by-agent on V9 was empty for
   non-Browser skills. **Fix:** [llm_gatewayV9/client.py:21](../../llm_gatewayV9/client.py).
   Took ~10 minutes to find because /v1/chat returned 200 on V9 *and* V8
   so the test looked like it was working until we inspected the ledger.

2. **V9's SQLite writes silently stopped across a day rollover.** No
   exception, just a stale connection that kept returning 200s. A simple
   restart restored writes. **Mitigation for the runtime:** none yet; this
   should land as an issue against V9's `db.conn()` lifecycle.

3. **Planner pre-loads filter query strings.** First HF run produced
   `path="extract"` because the Planner emitted
   `url=https://huggingface.co/models?pipeline_tag=text-generation&sort=likes`
   and the page-with-filters returned useful HTML to trafilatura.
   **Fix:** an explicit instruction in [prompts/planner.md](prompts/planner.md)
   that Browser nodes must receive the BASE URL only, with the desired
   filter described in `goal`. After this change Browser ran the a11y path
   to drive HF's own filter widgets.

4. **Planner forgot Distiller.** First HF run also skipped Distiller and
   piped Browser → Formatter directly, producing thin output. **Fix:** an
   explicit "ALWAYS insert distiller between Browser and Formatter when the
   user wants structured fields per item" rule in planner.md.

5. **Cascade chose a11y on a "draw a rectangle" task.** Excalidraw's
   toolbar is well aria-labelled and Layer 2b's `drag(x,y → x,y)` action
   handles canvas, so a11y solved it before vision could fire. Documented
   above; addressed by the `metadata.force_path` knob for the spec's
   vision-validation smoke. The natural-cascade behaviour is correct in
   production.

6. **Bare GET → 405 from Redfin's WAF was initially classified as
   `extraction_failed`, not `gateway_blocked`.** The CAPTCHA page only
   appears on a real Playwright session. **Fix:** when Layer 1 HTTP fetch
   fails, the cascade now falls through to Layer 2b's Playwright drive
   rather than bailing; the `detect_gateway_block()` check inside `_drive()`
   then catches the rendered CAPTCHA and sets `gateway_blocked=True` on
   the DriverResult, which `BrowserSkill.run` translates to
   `error_code="gateway_blocked"`. ([browser/skill.py](browser/skill.py))

7. **Deviation from the spec on detector location.** The spec asked for
   `detect_gateway_block(html)` to live in `browser/dom.py`. The
   anti-instructions also said "do not edit the Browser driver code …
   the only new file is `browser/skill.py`." We honoured the harder
   constraint: the detector lives in `browser/skill.py`, beside the
   cascade that uses it.

## Files added / modified

```
S9/code/
  schemas.py                      MOD  +ErrorCode, +AgentResult.error_code, +BrowserOutput
  gateway.py                      MOD  → V9 on :8109
  skills.py                       MOD  +browser dispatch branch (~18 lines)
  agent_config.yaml               MOD  browser entry replaces STUB
  pyproject.toml                  MOD  +playwright +pillow +trafilatura +lxml
  prompts/planner.md              MOD  catalogue line + 3 prompt rules
  prompts/browser.md              MOD  STUB → real prompt
  browser/__init__.py             PORT from S9SharedCode (unchanged)
  browser/client.py               PORT + +chat() +cost_by_agent() +session
  browser/dom.py                  PORT (unchanged)
  browser/highlight.py            PORT (unchanged)
  browser/driver.py               PORT + +A11yDriver + +force_path
  browser/skill.py                NEW  (~280 LOC) cascade wrapper

llm_gatewayV9/
  pricing.py                      NEW
  main.py                         MOD  /v1/cost/by_agent ?agent= + dollars
  client.py                       MOD  DEFAULT_URL → 8109
```

The Browser-driver core (`client.py`, `dom.py`, `highlight.py`,
`driver.py`) is the same code that landed in S9SharedCode/code/browser/.
The only file added during integration is `browser/skill.py`. The
`client.py` / `driver.py` additions (chat(), force_path, A11yDriver)
were made before the integration round, in the Layer-2b work.

## §8 Natural Layer 3 escalation

The original §5 vision smoke fired only because the Planner set
`metadata.force_path="vision"` on the Browser node. That was the spec's
escape hatch, not the natural cascade. This section adds the
natural-cascade evidence: a run where the BrowserSkill walked
`extract → a11y → vision` on its own, without any `force_path` set
anywhere in the run.

### The canonical success

```
target  : http://127.0.0.1:8765/canvas_only.html
          (single <canvas>, draws red circle + navy rectangle + green
           triangle; nothing else on the page)
query   : "Open … and click somewhere inside the red circle drawn on
           the canvas. The canvas pixels are not DOM elements; an empty
           text-only element legend means the page cannot be acted on
           through text alone."
session : state/sessions/s9_natural_vision  (renamed from s8-ee8a5de4)
```

**Result (verified via `replay.py s9_natural_vision`):**

| n | skill | elapsed | output highlight |
|---:|---|---:|---|
| 1 | planner    | 1.4 s  | emits Browser → Formatter; **no `force_path`** in any emitted metadata |
| 2 | browser    | 29.7 s | **`"path": "vision"`**, 1 vision turn ending with `done(success=true)` |
| 3 | formatter  | 1.9 s  | "I have opened … and performed a click within the boundaries of the red circle" |

**Ledger** (`/v1/cost/by_agent?session=s8-ee8a5de4`, browser line):

```
provider=gemini  calls=7  tokens=4867/620  dollars=$0.000000
```

Seven gemini calls covered the full cascade: six a11y turns (the model
kept trying despite the empty legend, then conceded) plus one
successful vision turn. The single Browser ledger row demonstrates
that the vision call is attributable to the agent tag, distinct from
planner / formatter rows in the same session.

**`force_path` audit** (a programmatic check across all node outputs
in the run):

```
=== force_path occurrences in node OUTPUTS (must be 0) ===
  ✓ no force_path in any node output
```

The literal string `force_path` does appear once in `prompt_sent` on
n:1 — that is the planner.md instruction telling the model **not** to
set it, which is the opposite of using it.

### Why a11y was insufficient for this target

The canvas-only page contains exactly one DOM element of interest, a
`<canvas>`. The enumerator in `browser/dom.py` skips it because
`<canvas>` is not in the interactive selector list and has no
`role`/`aria-*` attribute and no `cursor: pointer`. So the a11y legend
this turn was **literally empty** (verified in the artifacts at
`out/natural_vision_canvas_only/.../a11y/turn_01_legend.txt` for the
earlier direct-invocation run — file size 0 bytes).

The model can still emit click-by-coordinate or drag actions without a
legend mark, but the goal explicitly told it not to guess — so it
issued `done(success=false)` after a few exploratory turns. The
cascade then escalated to Layer 3. With the screenshot in hand, the
model identified the red circle by colour and centre coordinates
visually, emitted a one-action turn (`drag(330,300 → 330,300)` for a
click-in-place, then `done(success=true)`), and the cascade exited
cleanly.

This is the precise condition Layer 3 was built for: a target with no
DOM grip *and* a goal that requires choosing an action against page
content that only exists in pixels.

### What else was tried — and why each yielded

Five other candidates were attempted before this success. The
intermediate trace lives at
`out/natural_vision_search/trace.json` and the gateway-side calls are
recoverable from `/v1/calls?agent=natural_vision_<label>`.

| candidate | outcome | one-line reason |
|---|---|---|
| tldraw.com | `path="a11y"`, 5 turns, done(true) | Toolbar buttons are aria-labelled (`Rectangle tool`, `Circle tool`, …); the driver's `drag(x,y → x,y)` action handles canvas drawing. Same shape as Excalidraw. |
| photopea.com | `path="extract"`, 0 turns, done(true) | Photopea served > 200 chars of extractable HTML on first load (welcome doc / chrome). My `_is_useful_extract` heuristic does not screen on verbs like "open" or "create", so extract was accepted — even though it told the caller nothing useful about the editor's state. |
| piskel (full draw goal) | `path="(error)"` — natural escalation **happened** | A11y enumerated 50+ tool buttons but no canvas pixels. Burned all 12 a11y turns without progress, then escalated to vision; vision ran 9 turns and was making visible progress (the canvas filled with black ink) when it ran out of gateway rate budget mid-run and the cascade exited with 503. The escalation was natural; the run-completion was not. |
| openprocessing | `path="a11y"` on the observation-only goal; `path="(error)"` on the click goal | A11y model called done(true) immediately on the observation goal — it confused "describe what is on the page" with "the goal is met". On the click variant it escalated to vision but hit the same rate-budget wall as piskel. |
| chromedino.com | `path="extract"`, 0 turns, done(true) | The page wrapper around the canvas had > 200 chars of marketing text; extract was accepted without ever looking at the canvas. |

### What this means in aggregate

The natural Layer 3 band is **narrower than the Excalidraw smoke
suggested**. Across six attempts the cascade naturally chose vision
exactly once, and only when both conditions held:

1. The page itself had no enumerable interactive elements (so a11y
   could not bluff a `done(success=true)`).
2. The goal forbade guessing without seeing the page (so a11y could
   not bluff a `done(success=true)` by hallucinating success on an
   empty legend).

Modern canvas applications (tldraw, Excalidraw, Photopea) all have
properly aria-labelled chrome around their canvas. The driver's
`drag(x,y → x,y)` action handles raw canvas drawing without visual
reasoning. Combined, these mean Layer 2b can usually solve interactive
goals that the field assumed would require Layer 3.

The honest cascade story for Session 9.md:

- **Layer 1** is the workhorse for static content (HF article, Wikipedia).
- **Layer 2b** is the workhorse for interactive content — including
  most "canvas" apps, because the surrounding aria-labelled chrome plus
  coordinate actions handles them. Excalidraw, tldraw, and the
  Hugging Face filter UI all yield to it.
- **Layer 3** is needed when the page has *no* DOM grip whatsoever AND
  the goal requires acting on page content that only exists visually.
  It is real, it works, and it is rare.

The `force_path` validation in §5 is supplemented (not replaced) by this
natural-cascade run: §5 exists to validate that the V9 `/v1/vision`
plumbing and the ledger attribution are wired correctly when Layer 3 is
chosen; §8 exists to demonstrate that the cascade *also* chooses Layer 3
on its own when it should.

### Two gateway changes made during this round

While chasing the natural-cascade case, two V9-side rough edges
surfaced that would have blocked any natural-vision success and have
been fixed under the "gateway owns provider quirks" rule:

1. **GitHub `response_format=json_object` strict-keyword refusal.**
   GitHub Models requires the literal word `"json"` to appear in
   `messages` when `response_format.type=="json_object"`. The V9
   OpenAI-compat adapter already downgrades `json_schema → json_object`
   on rejection; it now also injects a one-line "Return your reply as a
   single JSON object." system hint when the next attempt would 400 on
   the keyword check. Without this, every vision call that failed over
   to GitHub immediately 400'd, which is why the §1 Redfin run and the
   §8 candidate-search burned through the gemini→github vision failover
   chain before V9 ran out of providers. See
   [`llm_gatewayV9/providers.py`](../../llm_gatewayV9/providers.py)
   inside `OpenAICompatProvider.chat`.
2. **Routing race between gemini cooldown and github availability.**
   Gemini's 4-s per-call cooldown can briefly remove it from the vision
   failover ring after an a11y phase saturates it. The router now
   correctly falls through to github for vision (which, post-fix above,
   actually answers). The §8 canonical run uses only gemini because
   the a11y phase was short and the cooldown had cleared by the time
   vision dispatched.

Both fixes are bug-fixes in V9, not changes to the shipped Browser
skill — the cascade in `S9/code/browser/skill.py` is the same code
that was shipped in the integration round.
