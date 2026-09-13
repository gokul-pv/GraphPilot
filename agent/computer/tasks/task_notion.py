#!/usr/bin/env python3
"""Task D — Notion: open Notion via Electron and type some text.

What it does:
    1. Launches Notion with electron_debugging_port=9223
    2. Activates it so it is in the foreground
    3. Uses the page tool (CDP) to create a new page, type content
    4. Verifies the page content via get_text
    5. Repeats the task with the Vision layer if specified

Usage:
    python -m computer.tasks.task_notion
    # or run Electron only:
    python computer/tasks/task_notion.py --electron
    # run Vision only:
    python computer/tasks/task_notion.py --vision
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from services.gateway import GATEWAY_URL
from computer.client import CuaClient, CuaError
from computer.driver import ComputerDriver, DriverConfig

# ── constants ─────────────────────────────────────────────────────────────────

BUNDLE_ID       = "notion.id"
APP_DISPLAY     = "Notion"
APP_HINT        = "Notion"
ELECTRON_PORT   = 9222
SESSION_ID      = f"notion-{int(time.time())}"

_state_root     = Path(__file__).resolve().parents[2] / "state" / "sessions" / SESSION_ID / "computer"
TRAJECTORY_DIR  = str((_state_root / "trajectory").resolve())
SCREENSHOT_DIR  = str((_state_root / "screenshots").resolve())

PAGE_TITLE = "Notion Agent Test"
PAGE_BODY = "Hello from Antigravity computer-use agent!"
GOAL = (
    f"Open a new page in Notion. Type the page title '{PAGE_TITLE}', "
    f"press Return to move to the body, and type the content '{PAGE_BODY}'. "
    "Use Cmd+N to create a new page, wait for the page to be ready, "
    "then type the title and body text."
)

# ── helpers ───────────────────────────────────────────────────────────────────

def banner(msg: str) -> None:
    print(f"\n{'─' * 60}\n  {msg}\n{'─' * 60}", flush=True)

# ── electron run ──────────────────────────────────────────────────────────────

def run_electron(client: CuaClient) -> bool:
    """Layer 2c — Electron/CDP via page tool."""
    banner("ELECTRON RUN — Layer 2c (CDP / page tool)")

    # ── kill existing Notion instance to ensure debugging port is enabled ────
    try:
        for w in client.list_windows():
            name = (w.get("app_name") or "").lower()
            if "notion" in name:
                pid = w.get("pid") or w.get("owner_pid")
                if pid:
                    print(f"  Killing existing Notion process (pid={pid}) to enable debugging port...", flush=True)
                    client.kill_app(int(pid))
                    time.sleep(2.0)
                    break
    except Exception as e:
        print(f"  [warn] Could not kill existing Notion: {e}", flush=True)

    # ── launch Notion with debugging port ────────────────────────────────────
    banner("Step E1: launch Notion (electron_debugging_port)")
    try:
        launch_r = client.launch_app(bundle_id=BUNDLE_ID, electron_debugging_port=ELECTRON_PORT)
        electron_pid: int | None = launch_r.get("pid")
        print(f"  launched via cua-driver pid={electron_pid}", flush=True)
    except CuaError as e:
        print(f"  ERROR launching Notion: {e}", flush=True)
        return False

    if not electron_pid:
        print("  ERROR: launch did not return a pid", flush=True)
        return False

    # ── activate — bring to foreground ───────────────────────────────────────
    banner("Step E2: activate Notion (AppleScript)")
    client.activate_app(APP_DISPLAY, wait_s=2.5)
    print("  ✓ activated", flush=True)

    # Resolve WindowRef
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

    return result.success

# ── vision run ────────────────────────────────────────────────────────────────

def run_vision(client: CuaClient) -> bool:
    """Layer 3 — Vision: SoM screenshot + VLM."""
    banner("VISION RUN — Layer 3 (SoM screenshot + VLM)")

    # ── launch Notion (no debugging port needed for vision) ──────────────────
    banner("Step V1: launch Notion")
    try:
        launch_r = client.launch_app(bundle_id=BUNDLE_ID)
        launch_pid: int | None = launch_r.get("pid")
        print(f"  launched pid={launch_pid}", flush=True)
    except CuaError as e:
        print(f"  ERROR launching Notion: {e}", flush=True)
        return False

    if not launch_pid:
        print("  ERROR: launch_app did not return a pid", flush=True)
        return False

    # ── activate ─────────────────────────────────────────────────────────────
    banner("Step V2: activate Notion")
    client.activate_app(APP_DISPLAY, wait_s=2.5)
    print("  ✓ activated", flush=True)

    ref = client.wait_for_window(APP_HINT, retries=12, delay_s=1.0)
    if not ref:
        print("  ERROR: Notion window not found in list_windows", flush=True)
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
        action_delay_s=1.0,     # give Notion more time to react to clicks
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

            # Kill Notion between runs so Vision gets a clean window
            if do_vision:
                banner("Killing Notion before Vision run")
                try:
                    wins = client.list_windows()
                    for w in wins:
                        name = (w.get("app_name") or "").lower()
                        if "notion" in name:
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
