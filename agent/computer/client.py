"""cua-driver client for the Computer skill.

Uses the `cua-driver call <tool> <json>` subprocess interface — the same
interface documented in the CUA Driver CLI reference.

All calls route through the running daemon (started with
`open -n -g -a CuaDriver --args serve`) so the per-window element-index
cache survives across calls. If the daemon is not running the first
`call()` raises `RuntimeError` with the correct start command.

The public contract of every method mirrors the tool schema exactly.
Where the guide says:
    cua-driver call get_window_state '{"pid":N,"window_id":N,...}'
this module does exactly that via subprocess.run.

`find_window(app_hint)` returns a `WindowRef` namedtuple (pid, window_id)
because every element-indexed action requires both.

Thread-safety: each call spawns its own subprocess; there is no shared
mutable state, so the client is safe to use from asyncio.to_thread.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Path to the cua-driver binary (installed by the cua installer).
_CUA = str(Path.home() / ".local" / "bin" / "cua-driver")


@dataclass
class WindowRef:
    """A (pid, window_id) pair — required by every element-indexed tool."""
    pid: int
    window_id: int


class CuaError(RuntimeError):
    """Raised when a cua-driver call returns a non-zero exit code."""


class CuaClient:
    """Thin wrapper around `cua-driver call <tool> <json>`.

    Usage::
        c = CuaClient()
        ref = c.find_window("Calculator")
        c.start_session("calc-demo")           # enables cursor overlay
        state = c.get_window_state(ref, capture_mode="ax", query="button")
        c.click_element(ref, element_index=5)  # cursor shown at click point
        c.end_session()
    """

    def __init__(self, cua_bin: str = _CUA, timeout: float = 30.0,
                 session: str | None = None):
        self._bin = cua_bin
        self._timeout = timeout
        self.session = session  # auto-injected into every action for cursor overlay

    def _sess(self) -> dict:
        """Return {"session": name} when a session is active, else {}."""
        return {"session": self.session} if self.session else {}

    # ── low-level dispatcher ─────────────────────────────────────────────────

    def call(self, tool: str, args: dict) -> dict:
        """Run `cua-driver call <tool> <json>`, return parsed JSON.

        Raises:
            RuntimeError — daemon not running (instructs how to start)
            CuaError    — tool returned non-zero exit code
        """
        try:
            proc = subprocess.run(
                [self._bin, "call", tool, json.dumps(args)],
                capture_output=True, text=True, timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            raise CuaError(
                f"{tool} timed out after {self._timeout}s — "
                "check daemon is running and the target app is ready"
            )
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            if "daemon" in err.lower() or "not running" in err.lower() or \
               "connection refused" in err.lower() or "no such file" in err.lower():
                raise RuntimeError(
                    f"cua-driver daemon is not running. Start it with:\n"
                    f"    open -n -g -a CuaDriver --args serve\n"
                    f"Then verify with: cua-driver status\n"
                    f"Original error: {err}"
                )
            raise CuaError(f"{tool} failed (exit {proc.returncode}): {err}")
        raw = proc.stdout.strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    # ── daemon management ────────────────────────────────────────────────────

    def ensure_daemon(self) -> bool:
        """Return True if daemon is running, False if it couldn't be detected."""
        try:
            status = subprocess.run(
                [self._bin, "status"], capture_output=True, text=True, timeout=5
            )
            return "is running" in (status.stdout + status.stderr).lower()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    # ── session & overlay ────────────────────────────────────────────────────

    def start_session(self, name: str) -> dict:
        """Declare a named agent session. Subsequent action calls auto-include it."""
        self.session = name
        return self.call("start_session", {"session": name})

    def end_session(self, name: str | None = None) -> dict:
        session_name = name or self.session or ""
        self.session = None
        if session_name:
            return self.call("end_session", {"session": session_name})
        return {}

    def set_agent_cursor_enabled(self, enabled: bool = True) -> dict:
        """Show/hide the agent-cursor overlay."""
        args: dict = {"enabled": enabled}
        if enabled and self.session:
            args["cursor_id"] = self.session
        return self.call("set_agent_cursor_enabled", args)

    # ── recording ────────────────────────────────────────────────────────────

    def start_recording(self, output_dir: str, record_video: bool = True) -> dict:
        """Start recording every tool call to a turn-numbered trajectory dir.

        Args:
            output_dir:   Directory where turn folders are written.
            record_video: Capture inline H.264 video via ScreenCaptureKit
                          (macOS 15+, Screen Recording TCC). Enabled by default.
                          If SCK is unavailable the daemon falls back gracefully.
        """
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        return self.call("start_recording", {
            "output_dir": output_dir,
            "record_video": record_video,
        })

    def stop_recording(self) -> dict:
        return self.call("stop_recording", {})

    # ── discovery ────────────────────────────────────────────────────────────

    def list_apps(self) -> list[dict]:
        r = self.call("list_apps", {})
        return r.get("apps") or []

    def list_windows(self) -> list[dict]:
        r = self.call("list_windows", {})
        return r.get("windows") or []

    def list_windows_by_pid(self, pid: int) -> list[dict]:
        """Return all windows owned by exactly this PID."""
        return [
            w for w in self.list_windows()
            if int(w.get("pid") or w.get("owner_pid") or 0) == pid
        ]

    def get_accessibility_tree(self) -> dict:
        """Lightweight desktop snapshot — no TCC needed."""
        return self.call("get_accessibility_tree", {})

    def get_screen_size(self) -> dict:
        return self.call("get_screen_size", {})

    # ── app lifecycle ────────────────────────────────────────────────────────

    def launch_app(self, bundle_id: str | None = None,
                   name: str | None = None,
                   electron_debugging_port: int | None = None,
                   creates_new_application_instance: bool | None = None) -> dict:
        """Launch an app via LaunchServices. Does NOT steal focus."""
        args: dict = {}
        if bundle_id:
            args["bundle_id"] = bundle_id
        if name:
            args["name"] = name
        if electron_debugging_port:
            args["electron_debugging_port"] = electron_debugging_port
        if creates_new_application_instance is not None:
            args["creates_new_application_instance"] = creates_new_application_instance
        return self.call("launch_app", args)

    def kill_app(self, pid: int) -> dict:
        return self.call("kill_app", {"pid": pid})

    def activate_app(self, app_name: str, wait_s: float = 0.8) -> None:
        """Bring app to front — required before first AX scan.

        Uses `open -a <name>` (foregrounds immediately) followed by AppleScript
        activate as a belt-and-suspenders approach. cua-driver's launch_app keeps
        the app in the background (self_activation_suppressed: true); without an
        explicit foreground call, get_window_state returns element_count=0.
        """
        # open -a is more reliable than AppleScript for actually foregrounding
        subprocess.run(["open", "-a", app_name], capture_output=True, check=False)
        # Belt-and-suspenders: AppleScript activate as well
        subprocess.run(
            ["osascript", "-e", f'tell application "{app_name}" to activate'],
            capture_output=True, check=False,
        )
        time.sleep(wait_s)

    # ── perception ────────────────────────────────────────────────────────────

    def get_window_state(self, ref: WindowRef,
                         capture_mode: str = "ax",
                         query: str | None = None,
                         screenshot_out_file: str | None = None) -> dict:
        """Walk the AX tree (and optionally capture a screenshot).

        Args:
            ref:          (pid, window_id) — both required.
            capture_mode: "ax"     — AX tree only (fast, no Screen Recording TCC)
                          "som"    — AX + screenshot annotated with element boxes
                          "vision" — screenshot only (for vision-LM agents)
            query:        Case-insensitive filter on tree_markdown output.
                          element_index values are unchanged; only the rendered
                          Markdown is trimmed. Use "button" to see only buttons.
            screenshot_out_file: Path where the PNG is saved (som / vision modes).

        Returns dict with at minimum:
            element_count   int   0 = AX walk empty (check permissions / activation)
            tree_markdown   str   Markdown AX tree with [element_index N] tags
            pid             int
            window_id       int
        """
        # session is NOT sent here — passing session to get_window_state causes
        # the daemon to return element_count:0 (empty AX tree). Session is only
        # for action tools (click, type_text, etc.) where the cursor overlay appears.
        args: dict = {
            "pid": ref.pid,
            "window_id": ref.window_id,
            "capture_mode": capture_mode,
        }
        if query:
            args["query"] = query
        if screenshot_out_file:
            args["screenshot_out_file"] = screenshot_out_file
        return self.call("get_window_state", args)

    # ── actions ──────────────────────────────────────────────────────────────

    def click_element(self, ref: WindowRef, element_index: int,
                      count: int = 1, modifier: str | None = None) -> dict:
        """Click by element_index (semantic). Requires prior get_window_state."""
        args: dict = {
            "pid": ref.pid, "window_id": ref.window_id,
            "element_index": element_index, "count": count,
            **self._sess(),
        }
        if modifier:
            args["modifier"] = modifier
        return self.call("click", args)

    def click_xy(self, ref: WindowRef, x: int, y: int,
                 count: int = 1, modifier: str | None = None) -> dict:
        """Click by pixel coordinate (vision layer)."""
        args: dict = {
            "pid": ref.pid, "window_id": ref.window_id,
            "x": x, "y": y, "count": count,
            **self._sess(),
        }
        if modifier:
            args["modifier"] = modifier
        return self.call("click", args)

    def double_click_element(self, ref: WindowRef, element_index: int) -> dict:
        return self.call("double_click", {
            "pid": ref.pid, "window_id": ref.window_id,
            "element_index": element_index,
            **self._sess(),
        })

    def right_click_element(self, ref: WindowRef, element_index: int) -> dict:
        return self.call("right_click", {
            "pid": ref.pid, "window_id": ref.window_id,
            "element_index": element_index,
            **self._sess(),
        })

    def type_text(self, ref: WindowRef, text: str) -> dict:
        """Insert text via AXSetAttribute — more reliable than press_key for strings."""
        return self.call("type_text", {
            "pid": ref.pid, "window_id": ref.window_id,
            "text": text,
            **self._sess(),
        })

    def press_key(self, ref: WindowRef, key: str) -> dict:
        """Press a single key: 'Return', 'Tab', 'Escape', 'Delete', etc."""
        return self.call("press_key", {
            "pid": ref.pid, "window_id": ref.window_id,
            "key": key,
            **self._sess(),
        })

    def hotkey(self, pid: int, keys: list[str]) -> dict:
        """Send a key combination, e.g. keys=["cmd", "s"] for Cmd+S.

        Note: hotkey takes pid only (not window_id) per the cua-driver schema.
        Requires at least 2 keys (one modifier + one key).
        """
        return self.call("hotkey", {"pid": pid, "keys": keys, **self._sess()})

    def set_value(self, ref: WindowRef, element_index: int,
                  value: str) -> dict:
        """Directly set AX value on text fields / sliders — faster than typing."""
        return self.call("set_value", {
            "pid": ref.pid, "window_id": ref.window_id,
            "element_index": element_index, "value": value,
            **self._sess(),
        })

    def scroll(self, ref: WindowRef,
               direction: str = "down", amount: int = 3) -> dict:
        """Scroll the window. Uses keyboard-synthesised scroll events."""
        return self.call("scroll", {
            "pid": ref.pid, "window_id": ref.window_id,
            "direction": direction, "amount": amount,
            **self._sess(),
        })

    def drag(self, ref: WindowRef, from_x: int, from_y: int,
             to_x: int, to_y: int) -> dict:
        """Click-drag from (from_x, from_y) to (to_x, to_y) in pixel space."""
        return self.call("drag", {
            "pid": ref.pid, "window_id": ref.window_id,
            "from_x": from_x, "from_y": from_y,
            "to_x": to_x, "to_y": to_y,
            **self._sess(),
        })


    # ── Electron / CDP ────────────────────────────────────────────────────────

    def page(self, pid: int, action: str,
             window_id: int | None = None,
             selector: str | None = None,
             css_selector: str | None = None,
             javascript: str | None = None,
             attributes: list[str] | None = None,
             bundle_id: str | None = None) -> dict:
        """Drive an Electron/Tauri app via the cua-driver `page` tool.

        Supported actions:
            "get_text"          — extract visible text; returns {"text": "..."}
            "query_dom"         — find elements by CSS selector; needs css_selector,
                                  optional attributes=[]; returns {"elements": [...]}
            "click_element"     — click a CSS-selected element with cursor animation;
                                  needs selector=; returns {}
            "execute_javascript"— run JS and return result; needs javascript=;
                                  returns {"result": ...}

        The app must have been launched with electron_debugging_port.
        window_id is required by the daemon for all page actions.
        """
        args: dict = {"pid": pid, "action": action}
        if window_id is not None:
            args["window_id"] = window_id
        if selector:
            args["selector"] = selector
        if css_selector:
            args["css_selector"] = css_selector
        if javascript:
            args["javascript"] = javascript
        if attributes:
            args["attributes"] = attributes
        if bundle_id:
            args["bundle_id"] = bundle_id
        return self.call("page", args)

    # ── window resolution helpers ─────────────────────────────────────────────

    def find_window(self, app_hint: str,
                    pid: int | None = None) -> Optional[WindowRef]:
        """Return the first WindowRef whose app name contains `app_hint`
        (case-insensitive). Returns None if no match found.

        Args:
            app_hint: Case-insensitive substring of the app name or bundle ID.
            pid:      When set, only match windows owned by this exact PID.
                      Use this after launch_app() to avoid matching an older
                      instance of the same app.
        """
        windows = self.list_windows()
        hint = app_hint.lower()

        def _pid_ok(w: dict) -> bool:
            if pid is None:
                return True
            return int(w.get("pid") or w.get("owner_pid") or 0) == pid

        def _ref(w: dict) -> Optional[WindowRef]:
            wpid = w.get("pid") or w.get("owner_pid")
            wid = w.get("window_id") or w.get("id")
            if wpid and wid:
                return WindowRef(pid=int(wpid), window_id=int(wid))
            return None

        # Try matching only app_name or bundle_id first to avoid matching
        # random window titles (e.g. searching for "Code" matching an IDE title with a "/code/" path).
        for check_title in [False, True]:
            def _name_match(w: dict) -> bool:
                if not check_title:
                    name = (w.get("app_name") or "").lower()
                    bundle = (w.get("bundle_id") or "").lower()
                    return hint in name or hint in bundle
                else:
                    name = (w.get("app_name") or w.get("title") or "").lower()
                    bundle = (w.get("bundle_id") or "").lower()
                    title = (w.get("title") or "").lower()
                    return hint in name or hint in bundle or hint in title

            # Phase 1: on-screen window with a non-empty title (the definitive main window)
            for w in windows:
                if not _pid_ok(w) or not _name_match(w):
                    continue
                if w.get("is_on_screen") and (w.get("title") or "").strip():
                    ref = _ref(w)
                    if ref:
                        return ref

            # Phase 2: any titled window (title may be blank briefly after launch)
            for w in windows:
                if not _pid_ok(w) or not _name_match(w):
                    continue
                if (w.get("title") or "").strip():
                    ref = _ref(w)
                    if ref:
                        return ref

            # Phase 3: on-screen window with no title (prefer visible over toolbar)
            for w in windows:
                if not _pid_ok(w) or not _name_match(w):
                    continue
                if w.get("is_on_screen"):
                    ref = _ref(w)
                    if ref:
                        return ref

            # Phase 4: any matching window (toolbar / off-screen fallback)
            for w in windows:
                if not _pid_ok(w) or not _name_match(w):
                    continue
                ref = _ref(w)
                if ref:
                    return ref

        if pid is not None:
            # PID filter was set but nothing matched — skip list_apps fallback
            return None

        # Fallback: search list_apps for pid, then list_windows for that pid
        for app in self.list_apps():
            name = (app.get("name") or "").lower()
            bundle = (app.get("bundle_id") or "").lower()
            if hint in name or hint in bundle:
                apid = app.get("pid")
                if apid:
                    for w in windows:
                        if int(w.get("pid") or w.get("owner_pid") or 0) == int(apid):
                            wid = w.get("window_id") or w.get("id")
                            if wid:
                                return WindowRef(pid=int(apid), window_id=int(wid))
        return None

    def wait_for_window(self, app_hint: str,
                        retries: int = 10,
                        delay_s: float = 0.5,
                        pid: int | None = None) -> Optional[WindowRef]:
        """Poll until the app's window appears in list_windows or retries exhausted.

        Args:
            pid: When set, only match windows for this PID (see find_window).
        """
        for _ in range(retries):
            ref = self.find_window(app_hint, pid=pid)
            if ref:
                return ref
            time.sleep(delay_s)
        return None
