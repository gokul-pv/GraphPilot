/**
 * Aligning a cua-driver trajectory to its recording.
 *
 * Everything a trajectory records shares one clock: `session.json` gives
 * `started_at_monotonic_ms` as the origin, each turn's `action.json` carries
 * `t_ms_from_session_start`, and `cursor.jsonl` carries `t_ms` on the same
 * basis. That is what lets the console put turns on the video's scrub bar and
 * draw the real cursor over the frame, with no calibration step.
 *
 * The one thing not to assume is that the clocks agree perfectly. If a
 * trajectory's timings run past the end of the video, the recording started
 * late or was cut short, and a cursor drawn from them would be confidently
 * wrong. `alignmentIsPlausible` is the guard: the caller degrades to chapter
 * markers alone rather than showing a cursor that lies.
 */

import type { CursorSample, TrajectoryTurn } from '@/lib/api/types'

/**
 * Cursor position at a moment, interpolated between the two nearest samples.
 *
 * The track is ~27 Hz (794 samples over 29.7 s in the calculator run), which
 * is below a 60 Hz redraw, so interpolating rather than snapping is what keeps
 * the overlay from stepping visibly.
 *
 * Binary search rather than a linear scan: this runs inside a
 * requestAnimationFrame loop over a track that can hold thousands of samples.
 */
export function cursorAt(
  samples: CursorSample[],
  tMs: number,
): { x: number; y: number } | null {
  if (samples.length === 0) return null
  if (tMs <= samples[0].t_ms) return { x: samples[0].x, y: samples[0].y }
  const last = samples[samples.length - 1]
  if (tMs >= last.t_ms) return { x: last.x, y: last.y }

  let lo = 0
  let hi = samples.length - 1
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (samples[mid].t_ms <= tMs) lo = mid
    else hi = mid
  }

  const a = samples[lo]
  const b = samples[hi]
  const span = b.t_ms - a.t_ms
  if (span <= 0) return { x: a.x, y: a.y }
  const f = (tMs - a.t_ms) / span
  return { x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f }
}

/**
 * The turn in flight at a moment.
 *
 * A turn occupies the band from `t_start_ms` (dispatched) to `t_ms`
 * (resolved), so this is a range test, not a nearest-marker lookup. Turns
 * missing either timestamp are skipped rather than guessed at.
 */
export function turnAt(turns: TrajectoryTurn[], tMs: number): TrajectoryTurn | null {
  let current: TrajectoryTurn | null = null
  for (const turn of turns) {
    if (turn.t_start_ms == null || turn.t_ms == null) continue
    if (tMs >= turn.t_start_ms && tMs <= turn.t_ms) return turn
    // Otherwise remember the most recent turn that has already resolved, so
    // the gaps between actions still attribute to something.
    if (tMs > turn.t_ms) current = turn
  }
  return current
}

export interface ChapterMarker {
  turn: TrajectoryTurn
  /** Position along the scrub bar, 0–1. */
  fraction: number
}

/** Turns as scrub-bar markers. Turns without timings, or past the end of the
 *  recording, are dropped rather than clamped onto the final frame. */
export function chapterMarkers(
  turns: TrajectoryTurn[],
  durationMs: number | null,
): ChapterMarker[] {
  if (!durationMs || durationMs <= 0) return []
  const markers: ChapterMarker[] = []
  for (const turn of turns) {
    const t = turn.t_start_ms ?? turn.t_ms
    if (t == null || t < 0 || t > durationMs) continue
    markers.push({ turn, fraction: t / durationMs })
  }
  return markers
}

/**
 * Whether trajectory timings and the recording plausibly share a clock.
 *
 * Checks that the last turn lands inside the recording, with a little slack
 * for the gap between the final action and `stop_recording`. When this is
 * false the caller should drop the cursor overlay and the markers rather than
 * place them wrongly.
 */
export function alignmentIsPlausible(
  turns: TrajectoryTurn[],
  durationMs: number | null,
): boolean {
  if (!durationMs || durationMs <= 0) return false
  const times = turns
    .map((t) => t.t_ms ?? t.t_start_ms)
    .filter((t): t is number => t != null)
  if (times.length === 0) return false
  const lastAction = Math.max(...times)
  return lastAction >= 0 && lastAction <= durationMs * 1.25
}

/**
 * Scale a point recorded in screen coordinates onto the rendered element.
 *
 * Click points and cursor samples are in the display space cua-driver saw,
 * which is neither the video's pixel dimensions nor the size the element is
 * laid out at. Callers pass the natural size they are mapping from — the
 * video's `videoWidth`/`videoHeight`, or an image's `naturalWidth`/`Height`.
 */
export function toRenderedPoint(
  point: { x: number; y: number },
  natural: { width: number; height: number },
  rendered: { width: number; height: number },
): { x: number; y: number } | null {
  if (!natural.width || !natural.height) return null
  return {
    x: (point.x / natural.width) * rendered.width,
    y: (point.y / natural.height) * rendered.height,
  }
}

/** Actions the operator cares about, as opposed to session bookkeeping. */
const BOOKKEEPING_TOOLS = new Set(['start_session', 'end_session'])

export function isBookkeeping(turn: TrajectoryTurn): boolean {
  return turn.tool != null && BOOKKEEPING_TOOLS.has(turn.tool)
}
