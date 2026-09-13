#!/usr/bin/env python3
"""Task A — Calculator arithmetic via deterministic hotkeys.

Cascade layer: Layer 2a (deterministic)
Vision calls:  ZERO  ← handled entirely without screenshots
LLM calls:     ZERO  (pure key sequence)

What it does:
    Compute 7 × 8 on macOS Calculator using cua-driver's AX element_index.
    Follows the exact sequence from CUA_DRIVER_GUIDE.md §5:
        scan → button indices → act (7, ×, 8, =) → verify result = 56

Recording:
    Trajectory saved to ./state/sessions/<session_id>/computer/trajectory/
    Run after to replay: cua-driver call replay_trajectory '{"trajectory_dir":"..."}'

Usage:
    python -m computer.tasks.task_calculator
    # or
    python computer/tasks/task_calculator.py
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

# Allow running from the agent root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from computer.client import CuaClient

SESSION_ID = f"calc-{int(time.time())}"
TRAJECTORY_DIR = str((Path(__file__).resolve().parents[2] / "state" / "sessions" / SESSION_ID / "computer" / "trajectory").resolve())

# ── helpers ───────────────────────────────────────────────────────────────────

def banner(msg: str) -> None:
    print(f"\n{'─' * 60}\n  {msg}\n{'─' * 60}")


def parse_element_index(tree: str, label: str) -> int | None:
    """Find the first element_index for a given button label in tree_markdown."""
    # Matches both '[element_index N] AXButton "label"' and '[N] AXButton (label)' formats.
    if label == "×":
        pattern = r'\[(?:element_index\s+)?(\d+)\]\s+\w+\s+(?:\(Multiply\)|\(\*\)|\(×\)|\("Multiply"\)|\("\*"\)|\("×"\)|id=Multiply)'
    elif label == "=":
        pattern = r'\[(?:element_index\s+)?(\d+)\]\s+\w+\s+(?:\(Equals\)|\(=\)|\("Equals"\)|\("="\)|id=Equals)'
    else:
        pattern = rf'\[(?:element_index\s+)?(\d+)\]\s+\w+\s+(?:\({re.escape(label)}\)|"{re.escape(label)}")'

    m = re.search(pattern, tree, re.IGNORECASE)
    return int(m.group(1)) if m else None


# ── task ──────────────────────────────────────────────────────────────────────

def run() -> None:
    client = CuaClient()

    # ── 0. daemon check ──────────────────────────────────────────────────────
    banner("Step 0: verifying cua-driver daemon")
    if not client.ensure_daemon():
        print("ERROR: cua-driver daemon is not running.")
        print("Start it: open -n -g -a CuaDriver --args serve")
        sys.exit(1)
    print("✓ daemon running")

    # ── 1. start recording ───────────────────────────────────────────────────
    banner("Step 1: start_recording → " + TRAJECTORY_DIR)
    client.start_recording(TRAJECTORY_DIR, record_video=True)
    client.start_session(name=SESSION_ID)
    client.set_agent_cursor_enabled(True)
    print(f"✓ recording to {TRAJECTORY_DIR}")

    try:
        # ── 2. launch Calculator ─────────────────────────────────────────────
        banner("Step 2: launch macOS Calculator")
        launch_r = client.launch_app(bundle_id="com.apple.calculator")
        launch_pid: int | None = launch_r.get("pid")
        print(f"  launched pid={launch_pid}: {launch_r}")

        # ── 3. activate (AppleScript workaround for background launch) ───────
        banner("Step 3: activate Calculator (AppleScript)")
        client.activate_app("Calculator", wait_s=1.5)
        print("✓ Calculator activated")

        # ── 4. find window ────────────────────────────────────────────────────
        # Pass pid= so we get the window from THIS launch, not a pre-existing instance.
        banner("Step 4: find Calculator window")
        ref = client.wait_for_window("Calculator", retries=10, delay_s=0.5,
                                     pid=launch_pid)
        if not ref:
            print("ERROR: Calculator window not found in list_windows")
            sys.exit(1)
        print(f"✓ window found: pid={ref.pid} window_id={ref.window_id}")

        # ── 5. scan AX tree ───────────────────────────────────────────────────
        # Retry up to 5s in case of timing variance on app startup.
        banner("Step 5: scan AX tree (capture_mode=ax)")
        state = {}
        for ax_attempt in range(1, 6):
            state = client.get_window_state(ref, capture_mode="ax", query=None)
            if state.get("element_count", 0) > 0:
                break
            print(f"  AX tree empty (attempt {ax_attempt}/5), waiting 1s…")
            time.sleep(1.0)
        if state.get("element_count", 0) == 0:
            print("ERROR: AX tree empty — check Accessibility permission")
            sys.exit(1)
        tree = state["tree_markdown"]
        print(f"  element_count={state['element_count']}")
        # Print just the button lines for readability
        btn_lines = [l for l in tree.splitlines() if "AXButton" in l][:25]
        print("\n".join(f"    {l}" for l in btn_lines))

        # ── 6. resolve element indices ────────────────────────────────────────
        # Labels in Calculator: "7", "×" (or "*"), "8", "="
        # We use parse_element_index to find them dynamically.
        labels = {"7": None, "×": None, "8": None, "=": None}
        for lbl in list(labels.keys()):
            idx = parse_element_index(tree, lbl)
            labels[lbl] = idx

        # Fallback: try ASCII "*" and "×" (Unicode)
        if labels["×"] is None:
            labels["×"] = parse_element_index(tree, "*")

        print(f"\n  resolved indices: {labels}")
        missing = [k for k, v in labels.items() if v is None]
        if missing:
            print(f"ERROR: could not find buttons: {missing}")
            print("Full tree:")
            print(tree[:3000])
            sys.exit(1)

        # ── 7. act: 7 × 8 = ──────────────────────────────────────────────────
        banner("Step 6: press 7, ×, 8, =  (Layer 2a — deterministic)")
        sequence = [
            ("7", labels["7"]),
            ("×", labels["×"]),
            ("8", labels["8"]),
            ("=", labels["="]),
        ]
        for btn_label, idx in sequence:
            print(f"  click element_index={idx}  [{btn_label}]")
            client.click_element(ref, element_index=idx)
            time.sleep(0.3)

        # ── 8. verify result ──────────────────────────────────────────────────
        banner("Step 7: verify — re-scan and check for '56'")
        time.sleep(0.5)
        verify_state = client.get_window_state(ref, capture_mode="ax", query="56")
        result_found = "56" in (verify_state.get("tree_markdown") or "")
        if result_found:
            print("✓ PASS — Calculator displays 56 (7 × 8 = 56)")
        else:
            print("✗ FAIL — '56' not found in AX tree")
            print(verify_state.get("tree_markdown", "")[:500])

    finally:
        # ── 9. stop recording ─────────────────────────────────────────────────
        banner("Step 8: stop_recording")
        client.stop_recording()
        client.set_agent_cursor_enabled(False)
        client.end_session()
        print(f"✓ trajectory saved to: {TRAJECTORY_DIR}")
        print(f"\nTrajectory dir: {TRAJECTORY_DIR}")
        print(f"To replay: cua-driver call replay_trajectory '{{\"dir\":\"{TRAJECTORY_DIR}\"}}'")


if __name__ == "__main__":
    run()
