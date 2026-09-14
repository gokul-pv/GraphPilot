"""Media manifest for one session's on-disk artifacts.

Distinct from `services/artifacts.py`, which is the content-addressable
`art:<sha16>` blob store that skills pass between nodes. This module is about
the *media* the browser and computer cascades leave behind — screenshots,
set-of-marks overlays, cua-driver trajectory recordings — none of which is
reachable through any other surface.

`AgentResult.artifacts` would be the obvious source and is always empty: both
cascades write files but never record their paths. Verified against
run-5ee5ecc6/nodes/n_002.json — a completed computer node sitting on 9.8 MB of
media with `artifacts: []`. So the manifest is built by walking the session
directory instead, which also means it works for sessions written before any
of this existed.

Layouts, all optional and all observed on disk:

    browser/browser_<epoch>/<layer>/turn_NN_{raw,marked}.png
                                    turn_NN_legend.txt
    computer/screenshots/<node_id>/vision_{raw,som}_turn_NNN.png
    computer/trajectory/<node_id>/session.json
                                  recording.mp4
                                  cursor.jsonl
                                  turn-000NN/{action,app_state}.json
                                             {screenshot,click}.png

Two things the console depends on:

  - **Every path is relative to the session directory**, ready to hand back to
    `GET /api/sessions/{sid}/files/{path}` unchanged. Nothing here leaks an
    absolute path, including `session.json`'s own `video.absolute_path`.
  - **A malformed or half-written file degrades to a missing field**, never an
    exception. A run that was killed mid-recording is the normal case, not an
    error: `session.json` can claim a video that was never finalised, so the
    file is stat'd rather than trusted.

`app_state.json` is listed by path but never inlined — its `tree_markdown` is
the whole accessibility tree (249 elements in the calculator run) and would
dwarf the rest of the manifest many times over. The console lazy-loads it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Node ids contain a colon ("n:2"), so trajectory directories are named
# `computer/trajectory/n:2/`. Colons are legal in a path segment (RFC 3986
# pchar), but a client must still percent-encode them; see the files route.
_BROWSER_TURN_RE = re.compile(r"^turn_(\d+)_(raw|marked|legend)\.(?:png|txt)$")
_VISION_TURN_RE = re.compile(r"^vision_(raw|som)_turn_(\d+)\.png$")
_TRAJ_TURN_RE = re.compile(r"^turn-(\d+)$")


def _read_json(path: Path) -> dict | None:
    """Parse a JSON file, or None if it is missing, corrupt, or unreadable."""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _rel(path: Path, base: Path) -> str:
    """Session-relative POSIX path, for the files route."""
    return path.relative_to(base).as_posix()


def _subdirs(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.iterdir() if p.is_dir())
    except OSError:
        return []


def _files(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.iterdir() if p.is_file())
    except OSError:
        return []


# ── browser ──────────────────────────────────────────────────────────────────

def scan_browser(base: Path) -> list[dict]:
    """One entry per (browser run, cascade layer) that produced turn files.

    A single node can cascade through several layers, each writing into its
    own subdirectory, so the layer is part of the identity rather than a
    property of the run.

    `marked` is None on every layer but vision/set-of-marks — the a11y layer
    screenshots the page without annotating it.
    """
    root = base / "browser"
    if not root.is_dir():
        return []

    out: list[dict] = []
    for run_dir in _subdirs(root):
        for layer_dir in _subdirs(run_dir):
            turns: dict[int, dict] = {}
            for f in _files(layer_dir):
                m = _BROWSER_TURN_RE.match(f.name)
                if m is None:
                    continue
                turn, kind = int(m.group(1)), m.group(2)
                row = turns.setdefault(
                    turn, {"turn": turn, "raw": None, "marked": None, "legend": None})
                row[kind] = _rel(f, base)
            if turns:
                out.append({
                    "run": run_dir.name,
                    "layer": layer_dir.name,
                    "turns": [turns[k] for k in sorted(turns)],
                })
    return out


# ── computer: vision screenshots ─────────────────────────────────────────────

def scan_computer_screenshots(base: Path) -> list[dict]:
    """Layer-3 vision screenshots, keyed by node id.

    Written only when the cascade falls all the way through to the vision
    layer, so this is empty for every run that succeeded on AX.
    """
    root = base / "computer" / "screenshots"
    if not root.is_dir():
        return []

    out: list[dict] = []
    for node_dir in _subdirs(root):
        turns: dict[int, dict] = {}
        for f in _files(node_dir):
            m = _VISION_TURN_RE.match(f.name)
            if m is None:
                continue
            kind, turn = m.group(1), int(m.group(2))
            row = turns.setdefault(turn, {"turn": turn, "raw": None, "som": None})
            row[kind] = _rel(f, base)
        if turns:
            out.append({"node_id": node_dir.name,
                        "turns": [turns[k] for k in sorted(turns)]})
    return out


# ── computer: cua-driver trajectories ────────────────────────────────────────

def _trajectory_turn(turn_dir: Path, base: Path, turn_no: int) -> dict:
    """One turn: the action taken, plus whatever frames were captured.

    Timings are milliseconds from session start and share an origin with both
    the video and cursor.jsonl, which is what lets the console put turns on
    the video's scrub bar. `t_start_ms` is when the action was dispatched and
    `t_ms` when it resolved, so the pair is a duration band, not an instant.
    """
    action = _read_json(turn_dir / "action.json") or {}
    row: dict = {
        "turn": turn_no,
        "tool": action.get("tool"),
        "arguments": action.get("arguments") or {},
        "result_summary": action.get("result_summary"),
        "click_point": action.get("click_point"),
        "t_ms": action.get("t_ms_from_session_start"),
        "t_start_ms": action.get("t_start_ms_from_session_start"),
        "screenshot": None,
        "click": None,
        "app_state": None,
    }
    for key, name in (("screenshot", "screenshot.png"),
                      ("click", "click.png"),
                      ("app_state", "app_state.json")):
        f = turn_dir / name
        if f.is_file():
            row[key] = _rel(f, base)
    return row


def scan_trajectories(base: Path) -> list[dict]:
    """One entry per computer node that recorded a cua-driver trajectory.

    `session.json` is the recorder's own manifest and is preferred over
    globbing for the video — but it is written before the file is finalised,
    so `video.present` is cross-checked against the file actually being there.
    A run killed mid-recording leaves a truthful-looking manifest and no
    playable video.

    `cua_session` is recovered from the first turn's `arguments.session`. It
    is the tag the computer cascade puts on its gateway calls, and without it
    that spend cannot be matched back to this run — see the note in api.py.
    """
    root = base / "computer" / "trajectory"
    if not root.is_dir():
        return []

    out: list[dict] = []
    for node_dir in _subdirs(root):
        meta = _read_json(node_dir / "session.json") or {}
        entry: dict = {
            "node_id": node_dir.name,
            "started_at_monotonic_ms": meta.get("started_at_monotonic_ms"),
            "video": None,
            "cursor": None,
            "cua_session": None,
            "turns": [],
        }

        video_meta = meta.get("video") or {}
        # Ignore video_meta["absolute_path"]: it is this machine's filesystem
        # layout and must never reach a client.
        video_file = node_dir / (video_meta.get("path") or "recording.mp4")
        if video_file.is_file():
            try:
                size = video_file.stat().st_size
            except OSError:
                size = 0
            entry["video"] = {
                "path": _rel(video_file, base),
                "duration_ms": video_meta.get("duration_ms"),
                "finalized": bool(video_meta.get("finalized")),
                "bytes": size,
            }

        cursor_file = node_dir / "cursor.jsonl"
        if cursor_file.is_file():
            entry["cursor"] = {
                "path": _rel(cursor_file, base),
                "sample_count": (meta.get("cursor") or {}).get("sample_count"),
            }

        turn_dirs: list[tuple[int, Path]] = []
        for d in _subdirs(node_dir):
            m = _TRAJ_TURN_RE.match(d.name)
            if m is not None:
                turn_dirs.append((int(m.group(1)), d))
        for turn_no, d in sorted(turn_dirs):
            turn = _trajectory_turn(d, base, turn_no)
            if entry["cua_session"] is None:
                session_tag = turn["arguments"].get("session")
                if isinstance(session_tag, str) and session_tag:
                    entry["cua_session"] = session_tag
            entry["turns"].append(turn)

        # A directory that was created but never written to is not a trajectory.
        if entry["video"] or entry["turns"]:
            out.append(entry)
    return out


# ── manifest ─────────────────────────────────────────────────────────────────

def scan_session(base: Path) -> dict:
    """The whole media manifest for one session directory.

    Returns the full shape with empty collections rather than omitting keys,
    so the console can render without optional-chaining every access.
    """
    return {
        "browser": scan_browser(base),
        "computer": {
            "screenshots": scan_computer_screenshots(base),
            "trajectories": scan_trajectories(base),
        },
    }


__all__ = ["scan_session", "scan_browser", "scan_computer_screenshots",
           "scan_trajectories"]
