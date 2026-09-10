"""Trajectory utilities for the Computer skill.

The Computer skill uses cua-driver's native start_recording / stop_recording
for live session capture. This module provides read-only helpers for
inspecting and listing recorded trajectory directories.

Trajectory directories live at:
    state/sessions/<session_id>/computer/trajectory/<node_id>/

Each directory is written by the cua-driver daemon in its own turn-folder
format. To replay a trajectory use:
    cua-driver call replay_trajectory '{"dir":"<path>"}'
"""
from __future__ import annotations

from pathlib import Path


def list_trajectories(sessions_root: Path) -> list[Path]:
    """Return all trajectory directories under sessions_root, newest first.

    Each returned path is a directory (not a file) written by the
    cua-driver daemon during a recording session.
    """
    pattern = "**/computer/trajectory/*"
    return sorted(
        (p for p in sessions_root.glob(pattern) if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
