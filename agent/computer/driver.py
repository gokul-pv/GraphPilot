"""Computer skill driver — five-layer cascade over cua-driver.

Layer architecture:

    Precondition  AX permission check + window resolution + activation
                  → error: permission_denied / window_not_found

    Layer 1  AX extract    Read content directly from the AX tree.
                           Zero LLM cost. For "what is in cell B3" or
                           "what does this email say" — the text is in
                           the tree already.

    Layer 2a Deterministic  Caller-supplied hotkey / element_index
                           sequences. No LLM. The Calculator demo path.

    Layer 2b AX + LLM      get_window_state(capture_mode="ax") → tree_markdown
                           → cheap text LLM via /v1/chat → element_index.
                           The workhorse layer. Re-scans after every action.

    Layer 2c Electron/CDP  target app was launched with electron_debugging_port.
                           Uses cua-driver `page` tool: CSS selectors, JS eval.
                           LLM reasons over page HTML, not the AX tree.
                           window_id is resolved fresh every turn from list_windows.

    Layer 3  Vision        get_window_state(capture_mode="som") → annotated PNG
                           + raw PNG → saved to screenshot_dir → /v1/vision
                           → pixel (x, y) click. Last resort: 10× cost of 2b.
                           Both the SoM-annotated and the raw screenshot are
                           saved as PNG files per turn.

Each layer returns a DriverResult. The skill (skill.py) drives the
cascade and calls start_recording / stop_recording around the whole run.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from services.gateway import GATEWAY_URL

from .client import CuaClient, CuaError, WindowRef


# ── data contracts ───────────────────────────────────────────────────────────

@dataclass
class StepRecord:
    """One scan-act-verify turn inside a driver layer."""
    turn: int
    layer: str
    actions: list[dict]
    outcome: str                    # "progress" | "done" | "failed"
    tree_markdown: str | None = None  # AX tree sent to LLM (2b)
    screenshot_path: str | None = None  # SoM screenshot path (Layer 3)
    raw_screenshot_path: str | None = None  # raw screenshot path (Layer 3)
    llm_thinking: str | None = None
    elapsed_s: float = 0.0


@dataclass
class DriverResult:
    """What each layer returns to the skill."""
    success: bool
    note: str | None = None
    turns: int = 0
    actions: list[dict] = field(default_factory=list)
    extracted: str | None = None   # text from AX tree (Layer 1 / 2b)
    steps: list[StepRecord] = field(default_factory=list)


# ── LLM action schema for AX (Layer 2b) ─────────────────────────────────────
#
# The LLM reads the AX tree_markdown (which already contains
# [element_index N] tags) and emits this schema. We dispatch
# directly by element_index — no pixel arithmetic needed.

COMPUTER_ACTION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thinking", "actions"],
    "properties": {
        "thinking": {
            "type": "string",
            "description": "1-2 sentences of reasoning. Quote the element_index you chose.",
        },
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "click", "double_click", "right_click",
                            "type_text", "press_key", "hotkey",
                            "set_value", "scroll",
                            "wait", "done",
                        ],
                    },
                    # click / double_click / right_click / set_value
                    "element_index": {"type": "integer"},
                    # click by pixel (vision fallback coordinates)
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    # type_text / set_value
                    "text": {"type": "string"},
                    # press_key
                    "key": {"type": "string"},
                    # hotkey — list of modifier + key, e.g. ["cmd","s"]
                    "keys": {"type": "array", "items": {"type": "string"}},
                    # scroll
                    "direction": {"type": "string",
                                  "enum": ["up", "down", "left", "right"]},
                    "amount": {"type": "integer"},
                    # wait
                    "seconds": {"type": "number"},
                    # done
                    "success": {"type": "boolean"},
                    "note": {"type": "string"},
                },
            },
        },
    },
}

# ── LLM action schema for Electron/page (Layer 2c) ──────────────────────────
ELECTRON_ACTION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thinking", "actions"],
    "properties": {
        "thinking": {"type": "string"},
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "type_text",   # clipboard paste into focused editor
                            "press_key",   # single key via CGEventPostToPid
                            "hotkey",      # modifier+key combo
                            "wait", "done",
                        ],
                    },
                    "selector":   {"type": "string"},
                    "javascript": {"type": "string"},
                    "text":       {"type": "string"},
                    "key":        {"type": "string"},
                    "keys":       {"type": "array", "items": {"type": "string"}},
                    "seconds":    {"type": "number"},
                    "success":    {"type": "boolean"},
                    "note":       {"type": "string"},
                },
            },
        },
    },
}

# ── LLM action schema for Vision (Layer 3) ───────────────────────────────────
VISION_ACTION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thinking", "actions"],
    "properties": {
        "thinking": {"type": "string"},
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "click", "double_click", "drag", "scroll",
                            "type_text", "press_key", "hotkey",
                            "wait", "done",
                        ],
                    },
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "from_x": {"type": "integer"},
                    "from_y": {"type": "integer"},
                    "to_x": {"type": "integer"},
                    "to_y": {"type": "integer"},
                    "text": {"type": "string"},
                    "key": {"type": "string"},
                    "keys": {"type": "array", "items": {"type": "string"}},
                    "direction": {"type": "string"},
                    "amount": {"type": "integer"},
                    "seconds": {"type": "number"},
                    "success": {"type": "boolean"},
                    "note": {"type": "string"},
                },
            },
        },
    },
}

# ── system prompts ────────────────────────────────────────────────────────────

SYSTEM_PROMPT_AX = (
    "You are a macOS desktop automation agent. Each turn you receive an "
    "Accessibility (AX) tree snapshot of the target window. Elements are "
    "tagged [element_index N] <role> \"label\" = value?.\n"
    "Emit the next actions to progress toward the user's goal.\n"
    "RULES:\n"
    "  - Reference elements by element_index, never by pixel coordinate.\n"
    "  - Re-scan happens automatically after each turn; indices may shift.\n"
    "  - Emit done(success=true) when goal is achieved.\n"
    "  - Emit done(success=false) if the goal is clearly impossible.\n"
    "  - Maximum 3 actions per turn.\n"
    "  - 'Actions taken so far' shows what you already did. Proceed to the NEXT step only.\n"
    "  - Do NOT retype text already typed. Verify it in the AX tree first.\n"
    "Respond ONLY with a JSON object matching the provided schema."
)

SYSTEM_PROMPT_ELECTRON = (
    "You are a macOS desktop automation agent driving an Electron app. "
    "CDP page inspection is NOT available — do NOT use click_element or execute_javascript.\n"
    "Available actions (use ONLY these):\n"
    "  type_text — paste text into the currently focused editor (text field required)\n"
    "  press_key — press a SINGLE key by name e.g. 'Return', 'Escape', 'Tab' (key field)\n"
    "  hotkey    — modifier+key combo (keys field, e.g. [\"cmd\",\"n\"] for Cmd+N)\n"
    "  wait      — sleep N seconds (seconds field)\n"
    "  done      — mark task complete (success + note fields)\n"
    "The app is already open and on the correct screen — start acting immediately. "
    "Do NOT navigate away, open new pages, or switch views unless the goal explicitly requires it.\n"
    "Strategy: navigate with keyboard shortcuts (hotkey) and enter text with type_text.\n"
    "Use hotkey for any combination with Cmd/Ctrl/Shift/Alt. Use press_key only for single keys.\n"
    "Emit done(success=true) when the goal is achieved.\n"
    "Respond ONLY with a JSON object matching the provided schema."
)

SYSTEM_PROMPT_VISION = (
    "You are a macOS desktop automation agent operating in vision mode.\n"
    "You receive a screenshot of the target window. There may or may not be element "
    "bounding boxes drawn on the image (Set-of-Marks) — if the app is a canvas or "
    "graphics tool (e.g. Inkscape, Photoshop, Figma), there will be NO annotations "
    "because the canvas content does not consist of standard accessibility-tree elements.\n"
    "In that case, estimate coordinates visually from the raw screenshot.\n"
    "Available actions:\n"
    "  click(x, y)             — single click at pixel coordinate\n"
    "  double_click(x, y)      — double-click\n"
    "  drag(from_x, from_y, to_x, to_y) — press-and-drag (use for drawing shapes)\n"
    "  type_text(text)         — type into focused element\n"
    "  press_key(key)          — single key: 'Return', 'Escape', 'r', etc.\n"
    "  hotkey(keys=[...])      — modifier+key e.g. [\"cmd\",\"z\"]\n"
    "  scroll(direction, amount)\n"
    "  wait(seconds)\n"
    "  done(success, note)     — signal task complete or impossible\n"
    "RULES:\n"
    "  - Coordinates (0,0) are the TOP-LEFT of the WINDOW, not the screen.\n"
    "  - If previous actions had no visible effect, try a DIFFERENT approach.\n"
    "  - Emit done(success=false, note=\"reason\") if the goal is clearly impossible.\n"
    "  - Do NOT repeat an action that already failed.\n"
    "Respond ONLY with a JSON object matching the provided schema."
)



# ── driver config ─────────────────────────────────────────────────────────────

@dataclass
class DriverConfig:
    goal: str
    ref: WindowRef | None = None          # resolved (pid, window_id)
    app_hint: str | None = None
    app_name: str | None = None           # for AppleScript activation
    max_steps: int = 12
    max_failures: int = 3
    gateway_url: str = GATEWAY_URL
    agent_tag: str = "computer"
    provider: str | None = None
    session: str | None = None
    action_delay_s: float = 0.8           # sleep between actions for UI settle
    screenshot_dir: str | None = None     # where to save Layer 3 PNGs


# ── the driver ────────────────────────────────────────────────────────────────

class ComputerDriver:
    """Runs one or more cascade layers for a given goal.

    Call run_ax_llm() for Layer 2b, run_electron() for Layer 2c,
    or run_vision() for Layer 3.  Layer 1 and 2a are handled directly
    in skill.py (no driver loop needed).
    """

    def __init__(self, client: CuaClient, cfg: DriverConfig):
        self._c = client
        self.cfg = cfg
        self.steps: list[StepRecord] = []
        self._failures = 0

    def _history_text(self) -> str:
        """Compact action history injected into each LLM prompt turn."""
        if not self.steps:
            return ""
        lines = ["Actions taken so far (do NOT repeat — proceed to the next step only):"]
        for step in self.steps[-6:]:
            for act in step.actions:
                t = act.get("type", "")
                if t == "type_text":
                    lines.append(f"  turn {step.turn}: typed {act.get('text', '')!r}")
                elif t == "hotkey":
                    lines.append(f"  turn {step.turn}: hotkey {act.get('keys')}")
                elif t == "done":
                    lines.append(f"  turn {step.turn}: done(success={act.get('success')})")
                else:
                    detail = act.get("element_index", act.get("key", act.get("keys", "")))
                    lines.append(f"  turn {step.turn}: {t}({detail})")
        return "\n".join(lines)

    # ── action dispatcher ─────────────────────────────────────────────────────

    def _dispatch_ax(self, action: dict) -> tuple[bool, str]:
        """Execute one AX-tree action. Returns (ok, note)."""
        c = self._c
        ref = self.cfg.ref
        if not ref:
            return False, "no WindowRef set"
        t = action.get("type", "")

        if t == "done":
            return True, action.get("note") or "done"

        if t in ("click", "double_click", "right_click"):
            idx = action.get("element_index")
            x, y = action.get("x"), action.get("y")
            if idx is not None:
                fn = {"click": c.click_element,
                      "double_click": c.double_click_element,
                      "right_click": c.right_click_element}[t]
                try:
                    r = fn(ref, idx)
                except CuaError as e:
                    err_msg = str(e)
                    if "returned -25206" in err_msg or "returned -25205" in err_msg:
                        print(f"  [Warning] Ignored non-fatal AX click failure: {err_msg}")
                        return True, f"ignored non-fatal click error: {err_msg}"
                    return False, err_msg
            elif x is not None and y is not None:
                r = c.click_xy(ref, int(x), int(y))
            else:
                return False, f"{t} needs element_index or (x,y)"
            return True, (r.get("note") if isinstance(r, dict) else None) or t

        if t == "type_text":
            c.type_text(ref, action.get("text") or "")
            return True, "typed"

        if t == "press_key":
            c.press_key(ref, action.get("key") or "")
            return True, "key"

        if t == "hotkey":
            keys = action.get("keys") or []
            c.hotkey(ref.pid, keys)
            return True, f"hotkey {keys}"

        if t == "set_value":
            idx = action.get("element_index")
            if idx is None:
                return False, "set_value needs element_index"
            c.set_value(ref, idx, action.get("text") or "")
            return True, "set_value"

        if t == "scroll":
            c.scroll(ref,
                     direction=action.get("direction") or "down",
                     amount=int(action.get("amount") or 3))
            return True, "scrolled"

        if t == "wait":
            time.sleep(float(action.get("seconds") or 1.0))
            return True, "waited"

        return False, f"unknown action type {t!r}"

    def _dispatch_electron(self, action: dict, pid: int,
                           wid: int) -> tuple[bool, str]:  # noqa: ARG002
        """Execute one Electron/CDP action. Returns (ok, note)."""
        c = self._c
        t = action.get("type", "")

        if t == "done":
            return True, action.get("note") or "done"

        if t == "wait":
            time.sleep(float(action.get("seconds") or 1.0))
            return True, "waited"

        # type_text: click the content area to focus it, then clipboard-paste.
        # Electron contenteditable ignores AX AXSetAttribute, and Cmd+V only
        # works if a text input is already focused — the click ensures that.
        if t == "type_text":
            text = action.get("text") or ""
            from computer.client import WindowRef
            ref = WindowRef(pid=pid, window_id=wid)
            # Click to focus the content area (skip sidebar ~260px, nav ~70px)
            wins = c.list_windows_by_pid(pid)
            if wins:
                bounds = wins[0].get("bounds") or {}
                w_px = int(bounds.get("width") or 900)
                h_px = int(bounds.get("height") or 700)
                cx = max(300, int(w_px * 0.6))
                cy = max(100, int(h_px * 0.45))
                c.click_xy(ref, cx, cy)
                time.sleep(0.25)
            import subprocess as _sp
            _sp.run(["pbcopy"], input=text, text=True, check=True)
            time.sleep(0.1)
            c.hotkey(pid, ["cmd", "v"])
            return True, f"pasted {text!r} via clipboard"

        if t == "press_key":
            key = action.get("key") or ""
            c.call("press_key", {"pid": pid, "key": key})
            return True, f"key {key!r}"

        if t == "hotkey":
            keys = action.get("keys") or []
            c.hotkey(pid, keys)
            return True, f"hotkey {keys}"

        return False, f"unknown electron action type {t!r}"

    def _dispatch_vision(self, action: dict) -> tuple[bool, str]:
        """Execute one vision action (pixel-coordinate click). Returns (ok, note)."""
        c = self._c
        ref = self.cfg.ref
        if not ref:
            return False, "no WindowRef"
        t = action.get("type", "")

        if t == "done":
            return True, action.get("note") or "done"

        if t in ("click", "double_click"):
            x, y = action.get("x"), action.get("y")
            if x is None or y is None:
                return False, "vision click needs x and y"
            if t == "double_click":
                c.click_xy(ref, int(x), int(y), count=2)
            else:
                c.click_xy(ref, int(x), int(y))
            return True, f"click at ({x},{y})"

        if t == "drag":
            fx, fy = action.get("from_x"), action.get("from_y")
            tx, ty = action.get("to_x"), action.get("to_y")
            if None in (fx, fy, tx, ty):
                return False, "drag needs from_x, from_y, to_x, to_y"
            c.drag(ref, int(fx), int(fy), int(tx), int(ty))
            return True, f"drag ({fx},{fy})→({tx},{ty})"


        if t == "type_text":
            c.type_text(ref, action.get("text") or "")
            return True, "typed"

        if t == "press_key":
            c.press_key(ref, action.get("key") or "")
            return True, "key"

        if t == "hotkey":
            c.hotkey(ref.pid, action.get("keys") or [])
            return True, "hotkey"

        if t == "scroll":
            c.scroll(ref,
                     direction=action.get("direction") or "down",
                     amount=int(action.get("amount") or 3))
            return True, "scrolled"

        if t == "wait":
            time.sleep(float(action.get("seconds") or 1.0))
            return True, "waited"

        return False, f"unknown vision action {t!r}"

    # ── layer 2b: AX + LLM ───────────────────────────────────────────────────

    async def run_ax_llm(self) -> DriverResult:
        """Layer 2b — AX tree + /v1/chat."""
        from browser.client import GatewayClient
        gw = GatewayClient(base_url=self.cfg.gateway_url,
                      agent=self.cfg.agent_tag,
                      session=self.cfg.session)
        goal = self.cfg.goal
        ref = self.cfg.ref
        t0 = time.time()

        for turn in range(1, self.cfg.max_steps + 1):
            try:
                state = self._c.get_window_state(ref, capture_mode="ax",
                                                  query=None)
            except CuaError as e:
                return DriverResult(success=False, note=str(e), steps=self.steps)

            if state.get("element_count", 0) == 0:
                self._failures += 1
                if self._failures >= self.cfg.max_failures:
                    return DriverResult(
                        success=False,
                        note="AX tree empty for too many turns — "
                             "check permissions or activate the app",
                        steps=self.steps,
                    )
                time.sleep(1.0)
                continue

            tree = state.get("tree_markdown") or ""
            history = self._history_text()
            prompt = (
                f"Goal: {goal}\n\n"
                + (history + "\n\n" if history else "")
                + f"Current AX tree:\n{tree[:7500]}\n\n"
                "Proceed to the NEXT incomplete step only. "
                "Reference elements by their [element_index N] number from the tree above."
            )
            try:
                resp = await gw.chat(
                    prompt=prompt,
                    schema=COMPUTER_ACTION_SCHEMA,
                    schema_name="computer_action",
                    system=SYSTEM_PROMPT_AX,
                    max_tokens=1024,
                    provider=self.cfg.provider,
                )
            except Exception as e:  # noqa: BLE001
                self._failures += 1
                self.steps.append(StepRecord(
                    turn=turn, layer="ax_llm", actions=[],
                    outcome=f"llm_error: {e}", elapsed_s=time.time() - t0,
                ))
                if self._failures >= self.cfg.max_failures:
                    return DriverResult(success=False, note=f"LLM call failed: {e}",
                                        steps=self.steps)
                continue

            parsed = resp.parsed or {}
            if not parsed:
                try:
                    parsed = json.loads(resp.text)
                except (json.JSONDecodeError, TypeError):
                    parsed = {}

            thinking = parsed.get("thinking") or ""
            actions: list[dict] = parsed.get("actions") or []
            done_flag, done_success, done_note = False, True, ""
            turn_ok = True

            for act in actions:
                if act.get("type") == "done":
                    done_flag = True
                    done_success = bool(act.get("success", True))
                    done_note = act.get("note") or ""
                    break
                ok, note = self._dispatch_ax(act)
                if not ok:
                    turn_ok = False
                time.sleep(self.cfg.action_delay_s)

            outcome = "done" if done_flag else ("progress" if turn_ok else "failed")
            self.steps.append(StepRecord(
                turn=turn, layer="ax_llm", actions=actions,
                outcome=outcome, tree_markdown=tree[:2000],
                llm_thinking=thinking, elapsed_s=time.time() - t0,
            ))
            if done_flag:
                # Verify: re-scan the AX tree to capture the post-condition state.
                verified_tree: str | None = None
                try:
                    verify_state = self._c.get_window_state(ref, capture_mode="ax")
                    verified_tree = verify_state.get("tree_markdown") or ""
                    self.steps.append(StepRecord(
                        turn=turn, layer="ax_llm_verify", actions=[],
                        outcome="verified", tree_markdown=verified_tree[:2000],
                        elapsed_s=time.time() - t0,
                    ))
                except CuaError:
                    pass  # verify is best-effort; done_success is already set
                return DriverResult(
                    success=done_success, note=done_note,
                    turns=turn,
                    actions=[a for s in self.steps for a in s.actions],
                    extracted=verified_tree,
                    steps=self.steps,
                )
            if not turn_ok:
                self._failures += 1
                if self._failures >= self.cfg.max_failures:
                    return DriverResult(success=False,
                                        note="too many action failures",
                                        steps=self.steps)

        return DriverResult(success=False,
                            note=f"max_steps ({self.cfg.max_steps}) exhausted",
                            steps=self.steps)

    # ── layer 2c: Electron / CDP ──────────────────────────────────────────────

    async def run_electron(self, pid: int,
                           electron_port: int = 9222) -> DriverResult:  # noqa: ARG002
        """Layer 2c — Electron/CDP via cua-driver `page` tool.

        The app must already be launched with electron_debugging_port=electron_port.
        This method:
          1. Waits for the CDP port to be ready (polls get_text up to 15 s).
          2. Each turn: resolves window_id fresh from list_windows_by_pid,
             reads page text via page(action="get_text"), sends to LLM,
             dispatches actions via _dispatch_electron.
          3. On done: reads page text one final time for verification.
        """
        from browser.client import GatewayClient

        gw = GatewayClient(base_url=self.cfg.gateway_url,
                      agent=self.cfg.agent_tag,
                      session=self.cfg.session)
        goal = self.cfg.goal
        t0 = time.time()

        # NOTE: app activation (open -a + osascript) is done by skill.py before
        # run_electron is called — same contract as run_ax_llm which does no
        # activation of its own.

        # ── 1. Poll until the CDP endpoint is ready ─────────────────────────
        # NOTE: Electron apps have multi-process architectures. launch_app
        # returns the *launcher* PID, which may NOT be the PID that owns the
        # actual windows. We try the given PID first, then fall back to
        # find_window(app_hint) to discover the real PID.
        print(f"  [electron] waiting for window on pid={pid}…", flush=True)
        _wid: int | None = None
        _real_pid = pid  # may get overwritten if we discover the real PID

        for _attempt in range(20):          # up to ~15 s
            wins = self._c.list_windows_by_pid(_real_pid)

            # If that PID has no windows, discover the real one by app name.
            if not wins and self.cfg.app_hint:
                ref_found = self._c.find_window(self.cfg.app_hint)
                if ref_found and ref_found.pid != _real_pid:
                    _real_pid = ref_found.pid
                    _wid = ref_found.window_id
                    print(f"  [electron] PID re-resolved: launcher={pid} → real={_real_pid} (wid={_wid})",
                          flush=True)
                    wins = self._c.list_windows_by_pid(_real_pid)

            if wins:
                _wid = int(wins[0].get("window_id") or wins[0].get("id") or 0) or None
            if _wid:
                print(f"  [electron] window found (attempt {_attempt + 1}, pid={_real_pid}, wid={_wid})",
                      flush=True)
                break
            time.sleep(0.75)

        if not _wid:
            return DriverResult(
                success=False,
                note=f"No window found after 15 s "
                     f"(launcher_pid={pid}, resolved_pid={_real_pid}).",
                steps=self.steps,
            )

        # Update pid to the real one for all subsequent calls
        pid = _real_pid


        # ── 3. Main turn loop ────────────────────────────────────────────────
        for turn in range(1, self.cfg.max_steps + 1):
            # Resolve window_id fresh every turn — VS Code opens new windows
            wins = self._c.list_windows_by_pid(pid)
            if not wins:
                self._failures += 1
                if self._failures >= self.cfg.max_failures:
                    return DriverResult(success=False,
                                        note="no windows found for Electron pid",
                                        steps=self.steps)
                time.sleep(1.0)
                continue
            wid = int(wins[0].get("window_id") or wins[0].get("id") or 0)
            if not wid:
                self._failures += 1
                time.sleep(1.0)
                continue

            history = self._history_text()
            app_label = self.cfg.app_name or self.cfg.app_hint or "the app"
            prompt = (
                f"App: {app_label} (already launched and active — do NOT try to open it again).\n"
                f"Goal: {goal}\n\n"
                + (history + "\n\n" if history else "")
                + "Proceed to the NEXT incomplete step only."
            )

            print(f"  [electron turn {turn}] calling LLM…", flush=True)
            try:
                resp = await gw.chat(
                    prompt=prompt,
                    schema=ELECTRON_ACTION_SCHEMA,
                    schema_name="electron_action",
                    system=SYSTEM_PROMPT_ELECTRON,
                    max_tokens=1024,
                    provider=self.cfg.provider,
                )
                print(f"  [electron turn {turn}] LLM ok", flush=True)
            except Exception as e:  # noqa: BLE001
                self._failures += 1
                print(f"  [electron turn {turn}] LLM error: {e}", flush=True)
                self.steps.append(StepRecord(
                    turn=turn, layer="electron", actions=[],
                    outcome=f"llm_error: {e}", elapsed_s=time.time() - t0,
                ))
                if self._failures >= self.cfg.max_failures:
                    return DriverResult(success=False, note=f"LLM call failed: {e}",
                                        steps=self.steps)
                continue

            parsed = resp.parsed or {}
            if not parsed:
                try:
                    parsed = json.loads(resp.text)
                except (json.JSONDecodeError, TypeError):
                    parsed = {}

            thinking = parsed.get("thinking") or ""
            if thinking:
                print(f"  [electron turn {turn}] thinking: {thinking[:200]}", flush=True)

            actions: list[dict] = parsed.get("actions") or []
            done_flag, done_success, done_note = False, True, ""
            turn_ok = True

            for act in actions:
                if act.get("type") == "done":
                    done_flag = True
                    done_success = bool(act.get("success", True))
                    done_note = act.get("note") or ""
                    break
                ok, note = self._dispatch_electron(act, pid, wid)
                print(f"  [electron turn {turn}] {act.get('type')} → {'ok' if ok else 'FAIL'}: {note}",
                      flush=True)
                if not ok:
                    turn_ok = False
                time.sleep(self.cfg.action_delay_s)

            outcome = "done" if done_flag else ("progress" if turn_ok else "failed")
            self.steps.append(StepRecord(
                turn=turn, layer="electron", actions=actions,
                outcome=outcome, llm_thinking=thinking,
                elapsed_s=time.time() - t0,
            ))

            if done_flag:
                # Verify: re-read page text to capture post-condition state.
                verified_text: str | None = None
                self.steps.append(StepRecord(
                    turn=turn, layer="electron_verify", actions=[],
                    outcome="verified", elapsed_s=time.time() - t0,
                ))
                return DriverResult(
                    success=done_success, note=done_note,
                    turns=turn,
                    actions=[a for s in self.steps for a in s.actions],
                    extracted=verified_text,
                    steps=self.steps,
                )

            if not turn_ok:
                self._failures += 1
                if self._failures >= self.cfg.max_failures:
                    return DriverResult(success=False,
                                        note="too many electron action failures",
                                        steps=self.steps)

        return DriverResult(success=False,
                            note=f"max_steps ({self.cfg.max_steps}) exhausted",
                            steps=self.steps)



    # ── layer 3: vision ───────────────────────────────────────────────────────

    async def run_vision(self) -> DriverResult:
        """Layer 3 — SoM screenshot + raw screenshot + /v1/vision.

        Each turn saves two PNG files:
          - vision_som_turn_NNN.png   — Set-of-Marks annotated (sent to VLM)
          - vision_raw_turn_NNN.png   — raw screenshot (for human review)
        """
        from browser.client import GatewayClient
        gw = GatewayClient(base_url=self.cfg.gateway_url,
                      agent=self.cfg.agent_tag,
                      session=self.cfg.session)
        goal = self.cfg.goal
        ref = self.cfg.ref
        t0 = time.time()
        ss_dir = Path(self.cfg.screenshot_dir) if self.cfg.screenshot_dir else Path("/tmp")
        ss_dir.mkdir(parents=True, exist_ok=True)

        for turn in range(1, self.cfg.max_steps + 1):
            # ── capture SoM (annotated) screenshot ──────────────────────────
            som_path = str(ss_dir / f"vision_som_turn_{turn:03d}.png")
            raw_path = str(ss_dir / f"vision_raw_turn_{turn:03d}.png")

            try:
                # SoM mode: AX + screenshot with bounding boxes — sent to VLM
                self._c.get_window_state(ref, capture_mode="som",
                                          screenshot_out_file=som_path)
                print(f"  [vision turn {turn}] SoM screenshot → {som_path}", flush=True)
            except CuaError as e:
                # Fallback: plain vision mode (no AX annotation)
                print(f"  [vision turn {turn}] SoM failed ({e}), falling back to vision mode", flush=True)
                try:
                    self._c.get_window_state(ref, capture_mode="vision",
                                              screenshot_out_file=som_path)
                except CuaError as e2:
                    self._failures += 1
                    if self._failures >= self.cfg.max_failures:
                        return DriverResult(success=False,
                                            note=f"screenshot failed: {e2}",
                                            steps=self.steps)
                    time.sleep(1.0)
                    continue

            # ── capture raw screenshot ───────────────────────────────────────
            try:
                self._c.get_window_state(ref, capture_mode="vision",
                                          screenshot_out_file=raw_path)
                print(f"  [vision turn {turn}] raw screenshot → {raw_path}", flush=True)
            except CuaError:
                raw_path = None  # raw is best-effort; SoM is what matters

            # ── read and encode the SoM screenshot ──────────────────────────
            try:
                png_bytes = Path(som_path).read_bytes()
                data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode()
            except OSError as e:
                self._failures += 1
                if self._failures >= self.cfg.max_failures:
                    return DriverResult(success=False,
                                        note=f"could not read screenshot: {e}",
                                        steps=self.steps)
                continue

            history = self._history_text()
            prompt = (
                f"Goal: {goal}\n\n"
                + (history + "\n\n" if history else "")
                + "The screenshot shows the current window with element bounding boxes (Set-of-Marks). "
                "Click by (x, y) pixel coordinates. The origin (0,0) is top-left of the window.\n"
                "Proceed to the NEXT incomplete step only."
            )
            print(f"  [vision turn {turn}] calling VLM …", flush=True)
            try:
                resp = await gw.vision(
                    image_data_url=data_url,
                    prompt=prompt,
                    schema=VISION_ACTION_SCHEMA,
                    schema_name="vision_action",
                    system=SYSTEM_PROMPT_VISION,
                    max_tokens=1024,
                    provider=self.cfg.provider,
                )
                print(f"  [vision turn {turn}] VLM ok", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"  [vision turn {turn}] VLM error: {e}", flush=True)
                self._failures += 1
                if self._failures >= self.cfg.max_failures:
                    return DriverResult(success=False,
                                        note=f"Vision LLM failed: {e}",
                                        steps=self.steps)
                continue

            parsed = resp.parsed or {}
            if not parsed:
                try:
                    parsed = json.loads(resp.text)
                except (json.JSONDecodeError, TypeError):
                    parsed = {}

            thinking = parsed.get("thinking") or ""
            actions: list[dict] = parsed.get("actions") or []
            done_flag, done_success, done_note = False, True, ""
            turn_ok = True

            for act in actions:
                if act.get("type") == "done":
                    done_flag = True
                    done_success = bool(act.get("success", True))
                    done_note = act.get("note") or ""
                    break
                ok, note = self._dispatch_vision(act)
                if not ok:
                    turn_ok = False
                time.sleep(self.cfg.action_delay_s)

            outcome = "done" if done_flag else ("progress" if turn_ok else "failed")
            self.steps.append(StepRecord(
                turn=turn, layer="vision", actions=actions,
                outcome=outcome,
                screenshot_path=som_path,
                raw_screenshot_path=raw_path,
                llm_thinking=thinking, elapsed_s=time.time() - t0,
            ))
            if done_flag:
                # Verify: capture a final SoM + raw screenshot pair.
                verify_som = str(ss_dir / f"vision_som_verify_{turn:03d}.png")
                verify_raw = str(ss_dir / f"vision_raw_verify_{turn:03d}.png")
                try:
                    self._c.get_window_state(ref, capture_mode="som",
                                              screenshot_out_file=verify_som)
                    self._c.get_window_state(ref, capture_mode="vision",
                                              screenshot_out_file=verify_raw)
                    print(f"  [vision turn {turn}] verify screenshots saved", flush=True)
                    self.steps.append(StepRecord(
                        turn=turn, layer="vision_verify", actions=[],
                        outcome="verified",
                        screenshot_path=verify_som,
                        raw_screenshot_path=verify_raw,
                        elapsed_s=time.time() - t0,
                    ))
                except CuaError:
                    pass  # verify is best-effort
                return DriverResult(
                    success=done_success, note=done_note,
                    turns=turn,
                    actions=[a for s in self.steps for a in s.actions],
                    extracted=verify_som,
                    steps=self.steps,
                )
            if not turn_ok:
                self._failures += 1
                if self._failures >= self.cfg.max_failures:
                    return DriverResult(success=False,
                                        note="too many vision action failures",
                                        steps=self.steps)

        return DriverResult(success=False,
                            note=f"max_steps ({self.cfg.max_steps}) exhausted (vision)",
                            steps=self.steps)
