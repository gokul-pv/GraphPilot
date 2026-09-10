"""Browser drivers — Layer 2b (a11y) and Layer 3 (set-of-marks vision).

Both drivers share:
  - the element enumerator (`dom.enumerate_interactives`)
  - the action vocabulary and JSON schema
  - the dispatcher and per-step loop machinery
  - the failure / done logic

They differ only in `_decide()`: the SoM driver attaches an annotated
screenshot to a /v1/vision call; the a11y driver sends the legend alone
to a /v1/chat call. This is intentional — Layer 2b is "Layer 3 minus the
screenshot" structurally, with much lower per-call cost.

Framework-free: Playwright for the browser, Pillow for annotation, httpx
for the gateway. Nothing else on the shipped path.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import Page

from .client import V9Client
from .dom import Element, PageSnapshot, enumerate_interactives
from .highlight import annotate, to_data_url


# ─── action vocabulary (shared) ──────────────────────────────────────────────
ACTION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thinking", "actions"],
    "properties": {
        "thinking": {"type": "string", "description": "1–2 sentences of reasoning"},
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["click", "type", "key", "scroll", "drag", "wait", "done"],
                    },
                    "mark":   {"type": "integer"},
                    "value":  {"type": "string"},
                    "clear":  {"type": "boolean"},
                    "from_x": {"type": "integer"},
                    "from_y": {"type": "integer"},
                    "to_x":   {"type": "integer"},
                    "to_y":   {"type": "integer"},
                    "amount": {"type": "integer"},
                    "seconds": {"type": "number"},
                    "success": {"type": "boolean"},
                    "note":    {"type": "string"},
                    "direction": {"type": "string"},
                },
            },
        },
    },
}


SYSTEM_PROMPT_VISION = (
    "You are a browser-driving agent. Each turn you receive a screenshot of "
    "the current page with numbered dashed boxes over every visible "
    "interactive element, plus a text legend mapping each number to "
    "[id]<tag>name</tag>. Your job is to make progress toward the user's goal "
    "by emitting a short list of actions. Available actions:\n"
    "  click(mark)                — click the centre of the boxed element\n"
    "  type(mark, value, clear?)  — focus the element then type `value`\n"
    "  key(value)                 — press a key like 'Enter', 'Tab', 'Escape'\n"
    "  scroll(direction, amount?) — direction in ['up','down','left','right']\n"
    "  drag(from_x, from_y, to_x, to_y) — mouse drag in CSS pixels (use for "
    "    canvas/SVG drawing where no element id covers the target)\n"
    "  wait(seconds)              — pause to let the page settle\n"
    "  done(success, note)        — finish; success=true if the goal is met\n"
    "Return MULTIPLE actions in a turn only when their effect is obvious; "
    "otherwise emit one action and let the next screenshot inform the next "
    "step. Be terse in `thinking` — one or two sentences."
)

SYSTEM_PROMPT_A11Y = (
    "You are a browser-driving agent. Each turn you receive ONLY a text "
    "legend of the page's visible interactive elements, formatted as "
    "[id]<tag role=\"role\">name</tag>. There is no screenshot. Make progress "
    "toward the user's goal by emitting a short list of actions. Available "
    "actions:\n"
    "  click(mark)                — click the centre of the named element\n"
    "  type(mark, value, clear?)  — focus the element then type `value`\n"
    "  key(value)                 — press a key like 'Enter', 'Tab', 'ArrowDown'\n"
    "  scroll(direction, amount?) — direction in ['up','down','left','right']\n"
    "  wait(seconds)              — pause to let the page settle\n"
    "  done(success, note)        — finish; success=true if the goal is met\n"
    "Choose the element whose `name` best matches the next step toward the "
    "goal. You CANNOT see the page — rely on the names. If two elements have "
    "similar names, prefer the one whose tag/role disambiguates "
    "(e.g. <input> for typing).\n"
    "\n"
    "CRITICAL RULES:\n"
    "  - Never bundle `done` with other actions in the same turn. After a "
    "    click or type, wait and observe the next turn's element list to "
    "    confirm the desired state before declaring done.\n"
    "  - Element names like `Sort: Trending`, `Beds & Baths`, or anything "
    "    ending with `▾` or `:` are DROPDOWN TRIGGERS. When you click one, "
    "    it must be the SINGLE action that turn — the popup options only "
    "    appear in the next turn's element list. Do NOT chain another click "
    "    after a dropdown trigger in the same turn; that second click will "
    "    hit a stale element.\n"
    "  - Verify URL fragments / page state on the next turn before declaring "
    "    done. The `PAGE URL` line in your prompt is your ground truth for "
    "    filter state on apps that encode filters in the URL.\n"
    "  - At most 2 actions per turn. Most turns should be ONE action.\n"
    "Be terse in `thinking` — one or two sentences."
)

# Back-compat alias for code that imported SYSTEM_PROMPT.
SYSTEM_PROMPT = SYSTEM_PROMPT_VISION


# ─── shared records ──────────────────────────────────────────────────────────
@dataclass
class StepRecord:
    turn: int
    thinking: str
    actions: list[dict]
    outcome: str
    provider: str
    model: str
    latency_ms: int
    tokens_in: int
    tokens_out: int


@dataclass
class DriverConfig:
    goal: str
    max_steps: int = 12
    max_failures: int = 3
    artifacts_dir: Optional[str] = None
    pause_between_steps: float = 0.5
    # V9 routing pins. Without these, the gateway picks the first eligible
    # provider in its failover ring — which can be the slow local Ollama for
    # text-only calls. Pin to e.g. "gemini" for Layer-2b runs.
    provider: Optional[str] = None
    model: Optional[str] = None


@dataclass
class DriverResult:
    success: bool
    note: str
    steps: list[StepRecord] = field(default_factory=list)


# ─── action dispatcher (shared) ──────────────────────────────────────────────
async def _dispatch(action: dict, page: Page, snap: PageSnapshot) -> str:
    t = action.get("type", "")
    if t == "click":
        el = snap.by_id(int(action.get("mark", -1)))
        if not el:
            return f"error: no element with mark {action.get('mark')!r}"
        await page.mouse.click(el.cx, el.cy)
        return "ok"
    if t == "type":
        el = snap.by_id(int(action.get("mark", -1)))
        if not el:
            return f"error: no element with mark {action.get('mark')!r}"
        await page.mouse.click(el.cx, el.cy)
        if action.get("clear", True):
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
        await page.keyboard.type(str(action.get("value", "")))
        return "ok"
    if t == "key":
        await page.keyboard.press(str(action.get("value", "Enter")))
        return "ok"
    if t == "scroll":
        d = str(action.get("direction", "down"))
        amt = int(action.get("amount", 600))
        dx, dy = 0, 0
        if d == "down":    dy = amt
        elif d == "up":    dy = -amt
        elif d == "right": dx = amt
        elif d == "left":  dx = -amt
        await page.mouse.wheel(dx, dy)
        return "ok"
    if t == "drag":
        fx, fy = int(action["from_x"]), int(action["from_y"])
        tx, ty = int(action["to_x"]),   int(action["to_y"])
        await page.mouse.move(fx, fy)
        await page.mouse.down()
        for i in range(1, 26):
            await page.mouse.move(fx + (tx - fx) * i / 25, fy + (ty - fy) * i / 25)
        await page.mouse.up()
        return "ok"
    if t == "wait":
        await asyncio.sleep(float(action.get("seconds", 0.5)))
        return "ok"
    if t == "done":
        return "ok"
    return f"error: unknown action {t!r}"


# ─── shared loop machinery ───────────────────────────────────────────────────
class BaseDriver:
    """Shared per-turn loop. Subclasses provide `_decide()` and the
    `SYSTEM_PROMPT` to use."""

    SYSTEM_PROMPT: str = ""
    LAYER_NAME: str = "base"   # 'vision' | 'a11y' — surfaced in artifacts

    def __init__(self, page: Page, client: V9Client, config: DriverConfig):
        self.page = page
        self.client = client
        self.config = config
        self.steps: list[StepRecord] = []

    def _history_text(self) -> str:
        if not self.steps:
            return "(no actions yet)"
        recent = self.steps[-5:]
        lines = []
        for s in recent:
            acts = ", ".join(
                f"{a['type']}({a.get('mark') or a.get('value', '')})"
                for a in s.actions[:3]
            )
            lines.append(f"turn {s.turn}: {acts} → {s.outcome}")
        return "\n".join(lines)

    async def _decide(self, snap: PageSnapshot, turn: int):
        """Returns (parsed_dict, GatewayResult). Subclass implements."""
        raise NotImplementedError

    async def step(self, turn: int) -> tuple[bool, bool, str]:
        snap = await enumerate_interactives(self.page)
        parsed, result = await self._decide(snap, turn)

        if not parsed:
            rec = StepRecord(turn, "", [],
                             f"error: parsed output missing; raw={result.text[:120]!r}",
                             result.provider, result.model, result.latency_ms,
                             result.input_tokens, result.output_tokens)
            self.steps.append(rec)
            return False, False, "no parsed output"

        thinking = parsed.get("thinking", "")
        actions = parsed.get("actions") or []
        outcomes: list[str] = []
        done_seen, success_seen, done_note = False, False, ""
        for a in actions:
            if a.get("type") == "done":
                done_seen = True
                success_seen = bool(a.get("success", False))
                done_note = a.get("note", "")
                outcomes.append(f"done({success_seen})")
                break
            try:
                outcome = await _dispatch(a, self.page, snap)
            except Exception as e:
                outcome = f"error: {type(e).__name__}: {e}"
            outcomes.append(outcome)
            if outcome.startswith("error"):
                break
            await asyncio.sleep(self.config.pause_between_steps)

        rec = StepRecord(
            turn=turn, thinking=thinking, actions=actions,
            outcome=" | ".join(outcomes) or "ok",
            provider=result.provider, model=result.model,
            latency_ms=result.latency_ms,
            tokens_in=result.input_tokens, tokens_out=result.output_tokens,
        )
        self.steps.append(rec)
        return done_seen, success_seen, done_note

    async def run(self) -> DriverResult:
        failures = 0
        for turn in range(1, self.config.max_steps + 1):
            done, success, note = await self.step(turn)
            last = self.steps[-1]
            if "error" in last.outcome:
                failures += 1
                if failures >= self.config.max_failures:
                    return DriverResult(False, f"giveup after {failures} consecutive failures",
                                        steps=self.steps)
            else:
                failures = 0
            if done:
                return DriverResult(success, note, steps=self.steps)
        return DriverResult(False, f"step cap reached ({self.config.max_steps})",
                            steps=self.steps)


# ─── Layer 3 — set-of-marks vision ───────────────────────────────────────────
class SetOfMarksDriver(BaseDriver):
    """Layer 3: screenshot with numbered marks + V9 /v1/vision call."""

    SYSTEM_PROMPT = SYSTEM_PROMPT_VISION
    LAYER_NAME = "vision"

    async def _decide(self, snap: PageSnapshot, turn: int):
        raw_png = await self.page.screenshot(full_page=False)
        marked = annotate(raw_png, snap.elements, snap.dpr)
        data_url = to_data_url(marked)

        if self.config.artifacts_dir:
            from pathlib import Path
            d = Path(self.config.artifacts_dir)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"turn_{turn:02d}_raw.png").write_bytes(raw_png)
            (d / f"turn_{turn:02d}_marked.png").write_bytes(marked)
            (d / f"turn_{turn:02d}_legend.txt").write_text(snap.legend(), encoding="utf-8")

        prompt = (
            f"GOAL: {self.config.goal}\n\n"
            f"VIEWPORT: {snap.viewport_w}x{snap.viewport_h} (CSS px, dpr={snap.dpr})\n"
            f"INTERACTIVE ELEMENTS ({len(snap.elements)}):\n{snap.legend()}\n\n"
            f"RECENT ACTIONS:\n{self._history_text()}\n\n"
            f"What is the next set of actions?"
        )
        result = await self.client.vision(
            data_url, prompt, system=self.SYSTEM_PROMPT,
            schema=ACTION_SCHEMA, schema_name="AgentOutput", max_tokens=1024,
            provider=self.config.provider, model=self.config.model,
        )
        return result.parsed, result


# ─── Layer 2b — a11y-text-only ───────────────────────────────────────────────
class A11yDriver(BaseDriver):
    """Layer 2b: legend-only text call to V9 /v1/chat. No screenshot, no
    vision-capable provider required, much lower per-call cost."""

    SYSTEM_PROMPT = SYSTEM_PROMPT_A11Y
    LAYER_NAME = "a11y"

    async def _decide(self, snap: PageSnapshot, turn: int):
        if self.config.artifacts_dir:
            from pathlib import Path
            d = Path(self.config.artifacts_dir)
            d.mkdir(parents=True, exist_ok=True)
            raw_png = await self.page.screenshot(full_page=False)
            (d / f"turn_{turn:02d}_raw.png").write_bytes(raw_png)
            (d / f"turn_{turn:02d}_legend.txt").write_text(snap.legend(), encoding="utf-8")

        prompt = (
            f"GOAL: {self.config.goal}\n\n"
            f"PAGE URL: {self.page.url}\n"
            f"VIEWPORT: {snap.viewport_w}x{snap.viewport_h}\n"
            f"INTERACTIVE ELEMENTS ({len(snap.elements)}):\n{snap.legend()}\n\n"
            f"RECENT ACTIONS:\n{self._history_text()}\n\n"
            f"What is the next set of actions? Names are your only guide — "
            f"no screenshot is available."
        )
        result = await self.client.chat(
            prompt, system=self.SYSTEM_PROMPT,
            schema=ACTION_SCHEMA, schema_name="AgentOutput", max_tokens=1024,
            provider=self.config.provider, model=self.config.model,
        )
        return result.parsed, result
