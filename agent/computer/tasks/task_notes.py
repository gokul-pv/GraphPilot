#!/usr/bin/env python3
"""Task B — Notes app: create a new note using AX tree + LLM judgment.

Cascade layer: Layer 2b (AX + cheap text LLM via V9 /v1/chat)
Vision calls:  ZERO  ← assignment constraint satisfied
LLM calls:     Yes — one Gemini Flash-Lite call per turn

What it does:
    1. Opens macOS Notes
    2. Creates a new note via Cmd+N (hotkey shortcut = Layer 2a bootstrap)
    3. Uses the AX tree + LLM to type the note title and body
    4. Verifies the note content is in the AX tree after typing

This demonstrates Layer 2b: the AX tree markdown is fed to the LLM,
which emits element_index-addressed actions. No pixel coordinates.

Recording:
    Trajectory saved to ./state/sessions/<session_id>/computer/trajectory/

Usage:
    python -m computer.tasks.task_notes
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from computer.client import CuaClient
from computer.driver import ComputerDriver, DriverConfig

SESSION_ID = f"notes-{int(time.time())}"
TRAJECTORY_DIR = str((Path(__file__).resolve().parents[2] / "state" / "sessions" / SESSION_ID / "computer" / "trajectory").resolve())
GATEWAY_URL = "http://localhost:8109"

NOTE_TITLE = "Agent Test Note"
NOTE_BODY = (
    "This note was created by the Session 10 Computer-Use agent.\n"
    "Layer 2b: AX tree + cheap LLM judgment.\n"
    f"Run ID: {SESSION_ID}"
)
GOAL = (
    f"A new note is already open. Type the title '{NOTE_TITLE}', "
    f"press Return to move to the body, then type the note body: '{NOTE_BODY}'. "
    "When done, press Cmd+S to save."
)


def banner(msg: str) -> None:
    print(f"\n{'─' * 60}\n  {msg}\n{'─' * 60}")


def run() -> None:
    client = CuaClient()

    banner("Step 0: daemon check")
    if not client.ensure_daemon():
        print("ERROR: daemon not running. Start: open -n -g -a CuaDriver --args serve")
        sys.exit(1)
    print("✓ daemon running")

    banner(f"Step 1: start_recording → {TRAJECTORY_DIR}")
    client.start_recording(TRAJECTORY_DIR, record_video=True)
    client.start_session(name=SESSION_ID)
    client.set_agent_cursor_enabled(True)

    try:
        # ── Launch Notes ─────────────────────────────────────────────────────
        banner("Step 2: launch macOS Notes")
        launch_r = client.launch_app(bundle_id="com.apple.Notes")
        launch_pid: int | None = launch_r.get("pid")
        client.activate_app("Notes", wait_s=1.5)

        ref = client.wait_for_window("Notes", retries=15, delay_s=0.5, pid=launch_pid)
        if not ref:
            print("ERROR: Notes window not found")
            sys.exit(1)
        print(f"✓ window pid={ref.pid} window_id={ref.window_id}")

        # ── Verify AX tree is non-empty (retry up to 8s) ─────────────────────
        state = {}
        for ax_attempt in range(1, 9):
            state = client.get_window_state(ref, capture_mode="ax")
            if state.get("element_count", 0) > 0:
                break
            print(f"  AX tree empty (attempt {ax_attempt}/8), retrying in 1s…")
            time.sleep(1.0)
        if state.get("element_count", 0) == 0:
            print("ERROR: AX tree empty — check Accessibility permission")
            sys.exit(1)
        print(f"  element_count={state['element_count']}")

        # ── Layer 2a bootstrap: Cmd+N to open a new note ────────────────────
        banner("Step 3: Cmd+N — new note (Layer 2a)")
        client.hotkey(ref.pid, ["cmd", "n"])
        time.sleep(0.8)
        print("✓ new note created")

        # ── Re-scan after new note dialog opens ──────────────────────────────
        state = client.get_window_state(ref, capture_mode="ax")
        if state.get("element_count", 0) == 0:
            print("ERROR: AX tree empty after Cmd+N")
            sys.exit(1)

        # ── Layer 2b: AX + LLM fills in the note ────────────────────────────
        banner("Step 4: Layer 2b — AX + LLM types the note")
        cfg = DriverConfig(
            goal=GOAL,
            ref=ref,
            app_hint="Notes",
            app_name="Notes",
            max_steps=10,
            max_failures=3,
            gateway_url=GATEWAY_URL,
            agent_tag="computer",
            provider="gemini",
            session=SESSION_ID,
            action_delay_s=0.6,
        )
        driver = ComputerDriver(client, cfg)
        result = asyncio.run(driver.run_ax_llm())

        print(f"\n  success={result.success}  turns={result.turns}  note={result.note}")
        for s in result.steps:
            print(f"    turn={s.turn} outcome={s.outcome} "
                  f"actions={[a.get('type') for a in s.actions]}")

        # ── Verify ────────────────────────────────────────────────────────────
        banner("Step 5: verify — re-scan for note title")
        time.sleep(0.5)
        verify = client.get_window_state(ref, capture_mode="ax",
                                          query=NOTE_TITLE)
        found = NOTE_TITLE in (verify.get("tree_markdown") or "")
        if found:
            print(f"✓ PASS — '{NOTE_TITLE}' visible in AX tree")
        else:
            print(f"✗ FAIL — '{NOTE_TITLE}' not found (LLM may not have typed it)")
            print("  Partial tree:", verify.get("tree_markdown", "")[:400])

        if not result.success:
            print(f"\n[WARNING] Layer 2b did not reach done=true: {result.note}")

    finally:
        banner("Step 6: stop_recording")
        client.stop_recording()
        client.set_agent_cursor_enabled(False)
        client.end_session()
        print(f"✓ trajectory: {TRAJECTORY_DIR}")
        print(f"\nReplay:\n  cua-driver call replay_trajectory "
              f"'{{\"dir\":\"{TRAJECTORY_DIR}\"}}'")


if __name__ == "__main__":
    run()
