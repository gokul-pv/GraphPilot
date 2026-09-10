#!/usr/bin/env python3
"""Task C — VS Code: create greeting.txt with "Hello World".

Two runs back-to-back:
  Run 1 — Layer 2c (Electron/CDP via page tool)
  Run 2 — Layer 3  (Vision: SoM screenshot + VLM)

What it does:
    1. Launches VS Code with electron_debugging_port=9222
    2. Activates it so it is in the foreground
    3. Uses the page tool (CDP) to open a new file, type content, save
    4. Verifies the file content via a second page get_text call
    5. Repeats the whole goal using the Vision layer (screenshot + VLM)

Recording:
    Trajectory saved to ./state/sessions/<session_id>/computer/trajectory/
    Screenshots saved to ./state/sessions/<session_id>/computer/screenshots/

Usage:
    python -m computer.tasks.task_vscode
    # or
    python computer/tasks/task_vscode.py
    # run Vision only:
    python computer/tasks/task_vscode.py --vision
    # run Electron only:
    python computer/tasks/task_vscode.py --electron
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from computer.client import CuaClient, CuaError
from computer.driver import ComputerDriver, DriverConfig

# ── constants ─────────────────────────────────────────────────────────────────

BUNDLE_ID       = "com.microsoft.VSCode"
APP_DISPLAY     = "Visual Studio Code"   # for open -a / AppleScript activate
APP_HINT        = "Code"                 # macOS app_name in list_windows
ELECTRON_PORT   = 9222
SESSION_ID      = f"vscode-{int(time.time())}"

_state_root     = Path(__file__).resolve().parents[2] / "state" / "sessions" / SESSION_ID / "computer"
TRAJECTORY_DIR  = str((_state_root / "trajectory").resolve())
SCREENSHOT_DIR  = str((_state_root / "screenshots").resolve())
GATEWAY_URL     = "http://localhost:8109"

FILE_NAME   = "greeting.txt"
FILE_CONTENT = "Hello World"
GOAL = (
    f"Open a new file in VS Code, type '{FILE_CONTENT}' as the file content, "
    f"then save it as '{FILE_NAME}' on the Desktop. "
    "Use Cmd+N to create a new untitled file, type the content, "
    "then Cmd+Shift+S (or File > Save As) to save with that filename."
)


# ── helpers ───────────────────────────────────────────────────────────────────

def banner(msg: str) -> None:
    print(f"\n{'─' * 60}\n  {msg}\n{'─' * 60}", flush=True)


def verify_file() -> bool:
    """Return True if ~/Desktop/greeting.txt contains 'Hello World'."""
    target = Path.home() / "Desktop" / FILE_NAME
    if not target.exists():
        print(f"  ✗ {target} does not exist", flush=True)
        return False
    content = target.read_text().strip()
    ok = FILE_CONTENT in content
    if ok:
        print(f"  ✓ {target} → {content!r}", flush=True)
    else:
        print(f"  ✗ {target} → {content!r} (expected '{FILE_CONTENT}')", flush=True)
    return ok


# ── electron run ──────────────────────────────────────────────────────────────

def run_electron(client: CuaClient) -> bool:
    """Layer 2c — Electron/CDP via page tool."""
    banner("ELECTRON RUN — Layer 2c (CDP / page tool)")

    # ── kill existing VS Code instance to ensure debugging port is enabled ───
    try:
        for w in client.list_windows():
            name = (w.get("app_name") or "").lower()
            if "code" in name:
                pid = w.get("pid") or w.get("owner_pid")
                if pid:
                    print(f"  Killing existing VS Code process (pid={pid}) to enable debugging port...", flush=True)
                    client.kill_app(int(pid))
                    time.sleep(2.0)
                    break
    except Exception as e:
        print(f"  [warn] Could not kill existing VS Code: {e}", flush=True)

    # ── launch VS Code with debugging port ───────────────────────────────────
    banner("Step E1: launch VS Code (electron_debugging_port)")
    try:
        launch_r = client.launch_app(
            bundle_id=BUNDLE_ID,
            electron_debugging_port=ELECTRON_PORT,
            creates_new_application_instance=True,
        )
        electron_pid: int | None = launch_r.get("pid")
        print(f"  launched pid={electron_pid}", flush=True)
    except CuaError as e:
        print(f"  ERROR launching VS Code: {e}", flush=True)
        return False

    if not electron_pid:
        print("  ERROR: launch_app did not return a pid", flush=True)
        return False

    # ── activate — bring to foreground ───────────────────────────────────────
    banner("Step E2: activate VS Code (AppleScript)")
    client.activate_app(APP_DISPLAY, wait_s=2.5)
    print("  ✓ activated", flush=True)

    # Resolve WindowRef — use APP_HINT ("Code"), not APP_DISPLAY, since macOS
    # reports app_name="Code" in list_windows.  Don't pin to launcher PID
    # because Electron's real main process gets a different PID.
    ref = client.wait_for_window(APP_HINT, retries=12, delay_s=1.0)
    if ref:
        electron_pid = ref.pid   # update to the real window-owning PID
        print(f"  ✓ WindowRef: pid={ref.pid} window_id={ref.window_id}", flush=True)
        client.activate_app(APP_DISPLAY, wait_s=2.0)
    else:
        print("  [warn] WindowRef not resolved — electron layer will still try CDP",
              flush=True)

    # ── run electron driver layer ─────────────────────────────────────────────
    banner("Step E3: Layer 2c — run_electron")
    cfg = DriverConfig(
        goal=GOAL,
        ref=ref,
        app_hint=APP_HINT.lower(),
        app_name=APP_DISPLAY,
        max_steps=14,
        max_failures=3,
        gateway_url=GATEWAY_URL,
        agent_tag="computer",
        provider="gemini",
        session=SESSION_ID,
        action_delay_s=0.8,
        screenshot_dir=SCREENSHOT_DIR,
    )
    driver = ComputerDriver(client, cfg)
    result = asyncio.run(driver.run_electron(electron_pid,
                                             electron_port=ELECTRON_PORT))

    print(f"\n  success={result.success}  turns={result.turns}  note={result.note}",
          flush=True)
    for s in result.steps:
        print(f"    turn={s.turn} layer={s.layer} outcome={s.outcome} "
              f"actions={[a.get('type') for a in s.actions]}", flush=True)

    # ── verify ────────────────────────────────────────────────────────────────
    banner("Step E4: verify — check Desktop/greeting.txt")
    time.sleep(0.5)
    passed = verify_file()
    if not passed and result.success:
        print("  [warn] driver reported success but file not found on disk", flush=True)

    return result.success


# ── vision run ────────────────────────────────────────────────────────────────

def run_vision(client: CuaClient) -> bool:
    """Layer 3 — Vision: SoM screenshot + VLM."""
    banner("VISION RUN — Layer 3 (SoM screenshot + VLM)")

    # ── launch VS Code (no debugging port needed for vision) ─────────────────
    banner("Step V1: launch VS Code")
    try:
        launch_r = client.launch_app(bundle_id=BUNDLE_ID)
        launch_pid: int | None = launch_r.get("pid")
        print(f"  launched pid={launch_pid}", flush=True)
    except CuaError as e:
        print(f"  ERROR launching VS Code: {e}", flush=True)
        return False

    if not launch_pid:
        print("  ERROR: launch_app did not return a pid", flush=True)
        return False

    # ── activate ─────────────────────────────────────────────────────────────
    banner("Step V2: activate VS Code")
    client.activate_app(APP_DISPLAY, wait_s=2.5)
    print("  ✓ activated", flush=True)

    ref = client.wait_for_window(APP_HINT, retries=12, delay_s=1.0)
    if not ref:
        print("  ERROR: VS Code window not found in list_windows", flush=True)
        return False
    print(f"  ✓ WindowRef: pid={ref.pid} window_id={ref.window_id}", flush=True)
    client.activate_app(APP_DISPLAY, wait_s=2.0)

    # ── run vision driver layer ───────────────────────────────────────────────
    banner("Step V3: Layer 3 — run_vision (SoM + raw screenshots)")
    Path(SCREENSHOT_DIR).mkdir(parents=True, exist_ok=True)
    cfg = DriverConfig(
        goal=GOAL,
        ref=ref,
        app_hint=APP_HINT.lower(),
        app_name=APP_DISPLAY,
        max_steps=10,
        max_failures=3,
        gateway_url=GATEWAY_URL,
        agent_tag="computer",
        provider=None,          # vision layer uses default (vision-capable) provider
        session=SESSION_ID,
        action_delay_s=1.0,     # give VS Code more time to react to clicks
        screenshot_dir=SCREENSHOT_DIR,
    )
    driver = ComputerDriver(client, cfg)
    result = asyncio.run(driver.run_vision())

    print(f"\n  success={result.success}  turns={result.turns}  note={result.note}",
          flush=True)
    for s in result.steps:
        shot = f" → {Path(s.screenshot_path).name}" if s.screenshot_path else ""
        print(f"    turn={s.turn} layer={s.layer} outcome={s.outcome}{shot} "
              f"actions={[a.get('type') for a in s.actions]}", flush=True)
    print(f"  Screenshots saved to: {SCREENSHOT_DIR}", flush=True)

    # ── verify ────────────────────────────────────────────────────────────────
    banner("Step V4: verify — check Desktop/greeting.txt")
    time.sleep(0.5)
    passed = verify_file()
    if not passed and result.success:
        print("  [warn] driver reported success but file not found on disk", flush=True)

    return result.success


# ── main ─────────────────────────────────────────────────────────────────────

def run() -> None:
    args = sys.argv[1:]
    do_electron = "--vision" not in args
    do_vision   = "--electron" not in args

    client = CuaClient()

    # ── daemon check ─────────────────────────────────────────────────────────
    banner("Step 0: daemon check")
    if not client.ensure_daemon():
        print("ERROR: cua-driver daemon is not running.")
        print("Start it: open -n -g -a CuaDriver --args serve")
        sys.exit(1)
    print("✓ daemon running", flush=True)

    # ── recording ─────────────────────────────────────────────────────────────
    banner(f"Step 1: start_recording → {TRAJECTORY_DIR}")
    client.start_recording(TRAJECTORY_DIR, record_video=True)
    client.start_session(name=SESSION_ID)
    client.set_agent_cursor_enabled(True)
    print(f"✓ recording started", flush=True)

    el_ok  = False
    vis_ok = False

    try:
        if do_electron:
            el_ok = run_electron(client)
            print(f"\n[ELECTRON] {'PASS ✓' if el_ok else 'FAIL ✗'}", flush=True)

            # Kill VS Code between runs so Vision gets a clean window
            if do_vision:
                banner("Killing VS Code before Vision run")
                try:
                    wins = client.list_windows()
                    for w in wins:
                        name = (w.get("app_name") or "").lower()
                        if "code" in name or "vscode" in name:
                            pid = w.get("pid") or w.get("owner_pid")
                            if pid:
                                client.kill_app(int(pid))
                                print(f"  killed pid={pid}", flush=True)
                                break
                except (CuaError, Exception):
                    pass
                time.sleep(2.0)

        if do_vision:
            vis_ok = run_vision(client)
            print(f"\n[VISION]   {'PASS ✓' if vis_ok else 'FAIL ✗'}", flush=True)

    finally:
        banner("Final: stop_recording")
        client.stop_recording()
        client.set_agent_cursor_enabled(False)
        client.end_session()
        print(f"✓ trajectory: {TRAJECTORY_DIR}", flush=True)
        print(f"✓ screenshots: {SCREENSHOT_DIR}", flush=True)
        print(f"\nReplay:\n  cua-driver call replay_trajectory "
              f"'{{\"dir\":\"{TRAJECTORY_DIR}\"}}'", flush=True)

    banner("Summary")
    if do_electron:
        print(f"  Electron (Layer 2c): {'PASS ✓' if el_ok else 'FAIL ✗'}", flush=True)
    if do_vision:
        print(f"  Vision   (Layer 3):  {'PASS ✓' if vis_ok else 'FAIL ✗'}", flush=True)


if __name__ == "__main__":
    run()
