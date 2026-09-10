"""Computer skill — five-layer cascade wrapper.

Parallel to browser/skill.py.  Translates the orchestrator's NodeSpec
into ComputerOutput / AgentResult, owns the five-layer cascade, and
wraps every run in start_recording / stop_recording.

Five-layer cascade:

    Precondition  permission check + window resolution + AppleScript activation
    Layer 1  ax_extract   — read AX tree directly; no LLM, zero cost
    Layer 2a deterministic — hotkey / element_index sequences; no LLM
    Layer 2b ax_llm       — AX tree markdown → V9 /v1/chat
    Layer 2c electron     — CDP page tool → V9 /v1/chat (Electron apps)
    Layer 3  vision       — SoM screenshot + raw screenshot → V9 /v1/vision

Recording:
    Every run calls start_recording (record_video=True) → run cascade
    → stop_recording. The cua-driver daemon records the session live.
    Trajectories land in:
        state/sessions/<session_id>/computer/trajectory/<node_id>/
    Vision screenshots (SoM + raw) land in:
        state/sessions/<session_id>/computer/screenshots/<node_id>/

Cursor overlay:
    start_session + set_agent_cursor_enabled = True at the top of each
    run so the demo video shows the agent cursor.

Replay:
macOS start command:
    open -n -g -a CuaDriver --args serve
"""
from __future__ import annotations

import asyncio
import random
import string
import time
from pathlib import Path

from schemas import AgentResult, ComputerOutput, NodeSpec

from .client import CuaClient, CuaError, WindowRef
from .driver import ComputerDriver, DriverConfig, DriverResult

# Known Electron app bundle IDs (apps that need electron_debugging_port).
_ELECTRON_BUNDLES = {
    "com.microsoft.vscode",
    "com.todesktop.230313mzl4w4u92",  # Cursor
    "com.tinyspeck.slackmacgap",       # Slack
    "notion.id",
    "com.hnc.discord",
    "com.linear.app",
    "md.obsidian",
    "com.1password.1password",
}
_ELECTRON_PORT = 9222

# Shorthand app-hint names → bundle IDs for known Electron apps.
# Used when the planner omits bundle_id but names the app by a common alias.
_ELECTRON_APP_HINTS: dict[str, str] = {
    "vs code":            "com.microsoft.vscode",
    "vscode":             "com.microsoft.vscode",
    "visual studio code": "com.microsoft.vscode",
    "code":               "com.microsoft.vscode",
    "cursor":             "com.todesktop.230313mzl4w4u92",
    "slack":              "com.tinyspeck.slackmacgap",
    "notion":             "notion.id",
    "discord":            "com.hnc.discord",
    "linear":             "com.linear.app",
    "obsidian":           "md.obsidian",
    "1password":          "com.1password.1password",
}

# Canonical macOS display names for known Electron apps (bundle_id → OS name).
# Used for activate_app and wait_for_window.
_ELECTRON_DISPLAY_NAMES: dict[str, str] = {
    "com.microsoft.vscode":            "Visual Studio Code",
    "com.todesktop.230313mzl4w4u92":  "Cursor",
    "com.tinyspeck.slackmacgap":       "Slack",
    "notion.id":                       "Notion",
    "com.hnc.discord":                 "Discord",
    "com.linear.app":                  "Linear",
    "md.obsidian":                     "Obsidian",
    "com.1password.1password":         "1Password",
}


class ComputerSkill:
    NAME = "computer"

    def __init__(
        self,
        *,
        gateway_url: str = "http://localhost:8109",
        agent_tag: str = "computer",
        ax_provider_pin: str | None = "gemini",
        vision_provider_pin: str | None = None,
        artifacts_root: str | None = None,
        max_steps_ax: int = 12,
        max_steps_vision: int = 8,
        wall_clock_s: float = 120.0,
        action_delay_s: float = 0.8,
        session: str | None = None,
    ):
        self.gateway_url = gateway_url
        self.agent_tag = agent_tag
        self.ax_provider_pin = ax_provider_pin
        self.vision_provider_pin = vision_provider_pin
        self.artifacts_root = Path(artifacts_root) if artifacts_root else None
        self.max_steps_ax = max_steps_ax
        self.max_steps_vision = max_steps_vision
        self.wall_clock_s = wall_clock_s
        self.action_delay_s = action_delay_s
        self.session = session

    # ── public entry point ───────────────────────────────────────────────────

    def run(self, node: NodeSpec) -> AgentResult:
        """Synchronous entry point called by skills.py via asyncio.to_thread."""
        meta = node.metadata or {}
        goal = meta.get("goal") or ""
        # Fallback: derive goal from resolved USER_QUERY if not in metadata
        if not goal:
            for r in (node.resolved_inputs or []):
                if r.get("id") == "USER_QUERY":
                    goal = str(r.get("value") or "")
                    break
        app_hint = meta.get("app") or meta.get("app_hint") or ""
        app_name = meta.get("app_name") or app_hint    # for AppleScript
        bundle_id = meta.get("bundle_id") or ""
        window_id_hint = meta.get("window_id")
        force_path = meta.get("force_path") or ""
        node_id = meta.get("node_id") or f"node_{int(time.time())}"

        if not goal:
            return self._pack_error("", "permission_denied",
                                    "metadata.goal is required")

        t0 = time.time()
        client = CuaClient()

        # ── daemon check ────────────────────────────────────────────────────
        if not client.ensure_daemon():
            return self._pack_error(goal, "permission_denied",
                                    "cua-driver daemon is not running. "
                                    "Start it: open -n -g -a CuaDriver --args serve",
                                    elapsed=time.time() - t0)

        # ── trajectory directory ────────────────────────────────────────────
        traj_dir: str | None = None
        if self.artifacts_root:
            traj_dir = str(self.artifacts_root / "trajectory" / node_id)
            Path(traj_dir).mkdir(parents=True, exist_ok=True)

        # ── start cua-driver native recording (live, record_video=True) ────
        if traj_dir:
            try:
                client.start_recording(traj_dir, record_video=True)
                print(f"[computer] recording started → {traj_dir}", flush=True)
            except (CuaError, Exception):  # noqa: BLE001
                pass  # recording is best-effort; don't abort the run

        # ── agent cursor overlay (for demo videos) ──────────────────────────
        _run_tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        session_name = f"computer-{node_id}-{_run_tag}"
        try:
            client.start_session(name=session_name)
            client.set_agent_cursor_enabled(True)
        except (CuaError, Exception):  # noqa: BLE001
            pass  # overlay is best-effort

        try:
            result = self._cascade(
                client, meta, node_id, goal,
                app_hint, app_name, bundle_id, window_id_hint,
                force_path, t0, session_name=session_name,
            )
        finally:
            # ── stop recording ────────────────────────────────────────────────
            try:
                client.stop_recording()
                if traj_dir:
                    print(f"[computer] recording stopped → {traj_dir}", flush=True)
            except (CuaError, Exception):  # noqa: BLE001
                pass
            try:
                client.set_agent_cursor_enabled(False)
                client.end_session()
            except (CuaError, Exception):  # noqa: BLE001
                pass

        return result

    # ── cascade ──────────────────────────────────────────────────────────────

    def _cascade(
        self, client: CuaClient, meta: dict,
        node_id: str, goal: str,
        app_hint: str, app_name: str, bundle_id: str,
        window_id_hint: int | str | None, force_path: str, t0: float,
        session_name: str | None = None,
    ) -> AgentResult:
        # ── Precondition: is this an Electron app? ──────────────────────────
        is_electron = (
            bundle_id.lower() in _ELECTRON_BUNDLES or
            meta.get("electron_debugging_port") is not None or
            force_path == "electron"
        )
        # Infer bundle_id from app hint when planner omits it.
        if not is_electron and app_hint:
            _inferred = _ELECTRON_APP_HINTS.get(app_hint.lower().strip())
            if _inferred:
                bundle_id = _inferred
                is_electron = True
                print(f"[computer] inferred Electron bundle: {bundle_id}", flush=True)
        electron_pid: int | None = meta.get("electron_pid")
        electron_port: int = int(meta.get("electron_debugging_port") or _ELECTRON_PORT)

        # ── Precondition: resolve window ────────────────────────────────────
        ref: WindowRef | None = None
        if window_id_hint and not is_electron:
            windows = client.list_windows()
            wid = int(window_id_hint)
            for w in windows:
                if int(w.get("window_id") or w.get("id") or 0) == wid:
                    ref = WindowRef(pid=int(w["pid"] or w.get("owner_pid")),
                                    window_id=wid)
                    break
        if not ref and not is_electron and app_hint:
            ref = client.find_window(app_hint)

        # If window still not found, launch the app via open -a and wait
        if not ref and not is_electron and (app_hint or app_name):
            print(f"[computer] launching app: {app_name or app_hint}", flush=True)
            client.activate_app(app_name or app_hint, wait_s=2.5)
            ref = client.wait_for_window(app_hint, retries=10, delay_s=0.5)

        # ── Precondition: verify AX tree is non-empty ───────────────────────
        state: dict = {}
        if not is_electron:
            if not ref:
                return self._pack_error(
                    goal, "window_not_found",
                    f"could not find or launch app_hint={app_hint!r}. "
                    f"Check the app name and try again.",
                    elapsed=time.time() - t0,
                )
            state = client.get_window_state(ref, capture_mode="ax")
            if state.get("element_count", 0) == 0:
                if app_name:
                    client.activate_app(app_name)
                    state = client.get_window_state(ref, capture_mode="ax")
                if state.get("element_count", 0) == 0:
                    return self._pack_error(
                        goal, "permission_denied",
                        "AX tree is empty after activation. "
                        "Grant Accessibility permission: "
                        "System Settings → Privacy & Security → Accessibility. "
                        "Then run: cua-driver permissions grant",
                        elapsed=time.time() - t0,
                    )

        # ── screenshot directory for Layer 3 ────────────────────────────────
        ss_dir: str | None = None
        if self.artifacts_root:
            ss_dir = str(self.artifacts_root / "screenshots" / node_id)

        # ── build driver config ─────────────────────────────────────────────
        cfg = DriverConfig(
            goal=goal,
            ref=ref,
            app_hint=app_hint or None,
            app_name=app_name or None,
            max_steps=self.max_steps_ax,
            max_failures=3,
            gateway_url=self.gateway_url,
            agent_tag=self.agent_tag,
            provider=self.ax_provider_pin,
            session=session_name or self.session,
            action_delay_s=self.action_delay_s,
            screenshot_dir=ss_dir,
        )
        driver = ComputerDriver(client, cfg)

        ax_note = "skipped"
        el_note = "skipped (not Electron)"
        vis_note = "skipped"

        # ── Layer 1: AX extract (no LLM, read-only goals) ───────────────────
        # Only fires for non-mutation goals (no action verbs) on non-Electron apps.
        if force_path not in ("ax_llm", "vision", "deterministic", "electron") \
                and not is_electron and state:
            extract = _ax_extract_read(state, goal)
            if extract:
                print("[computer] Layer 1: ax_extract succeeded", flush=True)
                return self._pack("ax_extract", goal, turns=0,
                                  content=extract,
                                  window_id=str(ref.window_id if ref else ""),
                                  elapsed=time.time() - t0)

        # ── Layer 2a: deterministic (no LLM) ────────────────────────────────
        det_actions = meta.get("actions") or []
        if det_actions and force_path not in ("ax_llm", "vision", "electron"):
            print(f"[computer] Layer 2a: running {len(det_actions)} deterministic actions", flush=True)
            det = self._run_deterministic(client, ref, det_actions)
            if det.success:
                return self._pack_driver("deterministic", goal, det,
                                        window_id=str(ref.window_id if ref else ""),
                                        elapsed=time.time() - t0)

        # ── Layer 2b: AX + LLM ──────────────────────────────────────────────
        if not is_electron and force_path not in ("vision", "electron"):
            print("[computer] Layer 2b: ax_llm", flush=True)
            try:
                ax_result = asyncio.run(driver.run_ax_llm())
                if ax_result.success:
                    return self._pack_driver("ax_llm", goal, ax_result,
                                            window_id=str(ref.window_id if ref else ""),
                                            elapsed=time.time() - t0)
                ax_note = ax_result.note or "ax_llm failed"
                print(f"[computer] Layer 2b failed: {ax_note}", flush=True)
            except Exception as e:
                ax_note = f"ax_llm exception: {e}"
                print(f"[computer] Layer 2b exception: {e}", flush=True)
        else:
            ax_note = "skipped (Electron or force_path)"

        # ── Layer 2c: Electron / CDP ─────────────────────────────────────────
        if is_electron or force_path == "electron":
            if not electron_pid:
                if bundle_id or app_hint:
                    print(f"[computer] Layer 2c: launching Electron app (port={electron_port})", flush=True)
                    try:
                        launch_r = client.launch_app(
                            bundle_id=bundle_id or None,
                            name=app_name or app_hint or None,
                            electron_debugging_port=electron_port,
                        )
                        electron_pid = launch_r.get("pid")
                        print(f"[computer] launched pid={electron_pid}", flush=True)
                    except CuaError as e:
                        return self._pack_error(
                            goal, "window_not_found",
                            f"failed to launch Electron app: {e}",
                            elapsed=time.time() - t0,
                        )
                if not electron_pid:
                    return self._pack_error(
                        goal, "window_not_found",
                        "provide metadata.electron_pid or bundle_id for Electron",
                        elapsed=time.time() - t0,
                    )

            # launch_app uses -g (background); AX cannot see backgrounded windows.
            _canonical = _ELECTRON_DISPLAY_NAMES.get(bundle_id.lower().strip())
            _act_name = _canonical or app_name or app_hint
            if _act_name:
                print(f"[computer] activating: {_act_name}", flush=True)
                try:
                    client.activate_app(_act_name, wait_s=2.0)
                except (CuaError, Exception):  # noqa: BLE001
                    pass

            # Resolve WindowRef for the Electron app — used by vision fallback.
            # NOTE: Electron's multi-process arch means launch_app returns the
            # *launcher* PID, not the main window PID. Try with pid filter
            # first, then without it as fallback.
            if not ref:
                print("[computer] resolving WindowRef for Electron app…", flush=True)
                _hints = list(dict.fromkeys(filter(None, [_canonical, app_name, app_hint])))
                for _name in _hints:
                    ref = client.wait_for_window(
                        _name, retries=8, delay_s=1.0, pid=electron_pid,
                    )
                    if ref:
                        break
                # Fallback: try WITHOUT pid filter (launcher PID is stale)
                if not ref:
                    print("[computer] pid-filtered search failed, trying without pid filter…", flush=True)
                    for _name in _hints:
                        ref = client.wait_for_window(
                            _name, retries=6, delay_s=1.0,
                        )
                        if ref:
                            # Update electron_pid to the real one
                            electron_pid = ref.pid
                            print(f"[computer] real PID discovered: {electron_pid}", flush=True)
                            break
                if ref:
                    print(f"[computer] WindowRef resolved: pid={ref.pid} window_id={ref.window_id}", flush=True)
                    if _act_name:
                        print(f"[computer] activating window: {_act_name}", flush=True)
                        try:
                            client.activate_app(_act_name, wait_s=3.0)
                        except (CuaError, Exception):  # noqa: BLE001
                            pass
            cfg.ref = ref  # propagate into driver config

            cfg.max_steps = self.max_steps_ax
            elec_driver = ComputerDriver(client, cfg)
            print("[computer] Layer 2c: electron", flush=True)
            try:
                el_result = asyncio.run(elec_driver.run_electron(electron_pid,
                                                                  electron_port=electron_port))
                if el_result.success:
                    return self._pack_driver("electron", goal, el_result,
                                            window_id=str(electron_pid),
                                            elapsed=time.time() - t0)
                el_note = el_result.note or "electron failed"
                print(f"[computer] Layer 2c failed: {el_note}", flush=True)
            except Exception as e:
                el_note = f"electron exception: {e}"
                print(f"[computer] Layer 2c exception: {e}", flush=True)
        else:
            el_note = "skipped (not Electron)"

        # ── Layer 3: Vision ──────────────────────────────────────────────────
        # Ensure ref is set even for Electron apps that failed CDP.
        if not ref and electron_pid:
            _canonical = _ELECTRON_DISPLAY_NAMES.get(bundle_id.lower().strip())
            print("[computer] Layer 3: resolving WindowRef for vision fallback…", flush=True)
            for _name in dict.fromkeys(
                filter(None, [_canonical, app_name, app_hint])
            ):
                ref = client.wait_for_window(_name, retries=5, delay_s=0.8,
                                             pid=electron_pid)
                if ref:
                    print(f"[computer] vision WindowRef: pid={ref.pid} window_id={ref.window_id}", flush=True)
                    break
            cfg.ref = ref

        if ref:
            cfg.max_steps = self.max_steps_vision
            cfg.provider = self.vision_provider_pin
            vis_driver = ComputerDriver(client, cfg)
            print("[computer] Layer 3: vision (SoM + raw screenshots)", flush=True)
            try:
                vis_result = asyncio.run(vis_driver.run_vision())
                if vis_result.success:
                    return self._pack_driver("vision", goal, vis_result,
                                            window_id=str(ref.window_id),
                                            elapsed=time.time() - t0)
                vis_note = vis_result.note or "vision failed"
                print(f"[computer] Layer 3 failed: {vis_note}", flush=True)
            except Exception as e:
                vis_note = f"vision exception: {e}"
                print(f"[computer] Layer 3 exception: {e}", flush=True)
        else:
            vis_note = "no WindowRef for vision"
            print(f"[computer] Layer 3 skipped: {vis_note}", flush=True)

        last = vis_note or el_note or ax_note or "all layers exhausted"
        return self._pack_error(goal, "interaction_failed",
                                f"all layers exhausted; last: {last}",
                                elapsed=time.time() - t0)

    # ── Layer 2a: deterministic ──────────────────────────────────────────────

    def _run_deterministic(self, client: CuaClient,
                           ref: WindowRef | None,
                           actions: list[dict]) -> DriverResult:
        """Execute caller-supplied actions without LLM."""
        if not ref:
            return DriverResult(success=False, note="no WindowRef for deterministic")
        cfg = DriverConfig(goal="deterministic", ref=ref)
        drv = ComputerDriver(client, cfg)
        for act in actions:
            ok, note = drv._dispatch_ax(act)
            if not ok:
                return DriverResult(success=False, note=note)
            time.sleep(self.action_delay_s)
        return DriverResult(success=True, turns=len(actions), actions=actions)

    # ── packers ──────────────────────────────────────────────────────────────

    def _pack(self, path: str, goal: str, *, turns: int,
              content: str | None = None, actions: list | None = None,
              window_id: str = "", elapsed: float = 0.0) -> AgentResult:
        out = ComputerOutput(
            goal=goal, path=path, turns=turns,
            content=content, actions=actions or [],
            window_id=window_id,
        )
        return AgentResult(success=True, agent_name=self.NAME,
                           output=out.model_dump(), elapsed_s=elapsed)

    def _pack_driver(self, path: str, goal: str, drv: DriverResult, *,
                     window_id: str = "", elapsed: float = 0.0) -> AgentResult:
        out = ComputerOutput(
            goal=goal, path=path,
            turns=drv.turns or 0,
            content=drv.extracted,
            actions=drv.actions or [],
            window_id=window_id,
        )
        return AgentResult(success=True, agent_name=self.NAME,
                           output=out.model_dump(), elapsed_s=elapsed)

    def _pack_error(self, goal: str, code: str, msg: str,
                    *, elapsed: float = 0.0) -> AgentResult:
        out = ComputerOutput(
            goal=goal or "", path="ax_extract", turns=0, content=None,
        )
        return AgentResult(
            success=False, agent_name=self.NAME,
            output=out.model_dump(), error=msg,
            error_code=code,  # type: ignore[arg-type]
            elapsed_s=elapsed,
        )


# ── Layer 1 helper (module-level, not a method) ──────────────────────────────

def _ax_extract_read(state: dict, goal: str) -> str | None:
    """Return AX tree content only for pure read-only goals.

    Goals containing any mutation verb are always escalated to Layer 2b+
    regardless of what is in the AX tree. This avoids the previous bug
    where goals like 'create greeting.txt' were silently returned as
    tree text because the AX tree happened to be non-empty.
    """
    import re
    _MUTATION_VERBS = frozenset({
        "move", "click", "drag", "type", "press", "open", "close",
        "create", "delete", "start", "stop", "send", "write", "save",
        "perform", "execute", "launch", "resize", "scroll",
    })
    goal_lower = goal.lower()
    if any(re.search(rf'\b{v}\b', goal_lower) for v in _MUTATION_VERBS):
        return None  # has mutation verb → never short-circuit in Layer 1
    read_verbs = ("get", "read", "find", "list", "check", "what",
                  "show", "display", "report", "tell", "extract")
    if not any(re.search(rf'\b{v}\b', goal_lower) for v in read_verbs):
        return None  # no read verb → escalate
    tree = state.get("tree_markdown") or ""
    if len(tree) < 100:
        return None
    return tree
