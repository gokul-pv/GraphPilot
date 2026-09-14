'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fileUrl, getAppState, getCursorTrack } from '@/lib/api/client'
import {
  alignmentIsPlausible,
  chapterMarkers,
  cursorAt,
  isBookkeeping,
  toRenderedPoint,
  turnAt,
} from '@/lib/media/trajectory'
import { bytes, millis, timecode } from '@/lib/format'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import type { Trajectory, TrajectoryTurn } from '@/lib/api/types'

/**
 * Cursor overlay, redrawn against the video's own clock.
 *
 * Driven by requestAnimationFrame rather than `timeupdate`, which fires only
 * about four times a second and would make the cursor lurch.
 */
function CursorOverlay({
  videoRef,
  samples,
  active,
}: {
  videoRef: React.RefObject<HTMLVideoElement | null>
  samples: { t_ms: number; x: number; y: number }[]
  active: boolean
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    if (!active || samples.length === 0) return
    let frame = 0

    const draw = () => {
      frame = requestAnimationFrame(draw)
      const video = videoRef.current
      const canvas = canvasRef.current
      if (!video || !canvas) return

      const w = video.clientWidth
      const h = video.clientHeight
      if (!w || !h) return
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w
        canvas.height = h
      }

      const ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.clearRect(0, 0, w, h)

      const point = cursorAt(samples, video.currentTime * 1000)
      if (!point) return
      // Cursor samples are in the display space cua-driver saw; map them onto
      // however large the video is actually rendered.
      const at = toRenderedPoint(
        point,
        { width: video.videoWidth, height: video.videoHeight },
        { width: w, height: h },
      )
      if (!at) return

      ctx.beginPath()
      ctx.arc(at.x, at.y, 9, 0, Math.PI * 2)
      ctx.strokeStyle = 'rgba(255,255,255,0.9)'
      ctx.lineWidth = 2
      ctx.stroke()
      ctx.beginPath()
      ctx.arc(at.x, at.y, 3.5, 0, Math.PI * 2)
      ctx.fillStyle = 'rgba(255,80,80,0.95)'
      ctx.fill()
    }

    frame = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(frame)
  }, [videoRef, samples, active])

  if (!active) return null
  return (
    <canvas
      ref={canvasRef}
      className="pointer-events-none absolute inset-0 size-full"
      aria-hidden
    />
  )
}

function TurnRow({
  turn,
  active,
  onSeek,
}: {
  turn: TrajectoryTurn
  active: boolean
  onSeek: (turn: TrajectoryTurn) => void
}) {
  const ok = turn.result_summary?.startsWith('✅')
  return (
    <button
      type="button"
      onClick={() => onSeek(turn)}
      className={cn(
        'flex w-full items-baseline gap-2 rounded px-2 py-1.5 text-left transition-colors',
        active ? 'bg-accent' : 'hover:bg-accent/50',
        isBookkeeping(turn) && 'opacity-55',
      )}
    >
      <span className="w-6 shrink-0 font-mono text-[10px] text-muted-foreground tabular">
        {turn.turn}
      </span>
      <span className="shrink-0 font-mono text-[11px] font-medium">{turn.tool ?? '—'}</span>
      <span className="truncate text-[11px] text-muted-foreground">
        {turn.result_summary?.replace(/^[✅❌]\s*/, '') ?? ''}
      </span>
      <span
        className={cn(
          'ml-auto shrink-0 font-mono text-[10px] tabular',
          ok === false ? 'text-status-failed' : 'text-muted-foreground',
        )}
      >
        {timecode(turn.t_start_ms)}
      </span>
    </button>
  )
}

/** The AX tree the model saw before choosing this turn's action. */
function AppStatePanel({ sessionId, path }: { sessionId: string; path: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['app-state', sessionId, path],
    queryFn: () => getAppState(sessionId, path),
    staleTime: Infinity,
  })

  if (isLoading) return <div className="p-2 text-[11px] text-muted-foreground">Loading…</div>
  if (error || !data) {
    return <div className="p-2 text-[11px] text-muted-foreground">Could not read app state.</div>
  }
  return (
    <div>
      <div className="mb-1 text-[10px] text-muted-foreground tabular">
        {data.element_count} elements · pid {data.pid}
      </div>
      <pre className="scroll-slim max-h-64 overflow-auto rounded bg-muted/40 p-2 font-mono text-[10px] leading-relaxed whitespace-pre-wrap">
        {data.tree_markdown}
      </pre>
    </div>
  )
}

/**
 * A cua-driver trajectory, played back against its own recording.
 *
 * Everything here hangs off one shared clock: `session.json` gives the origin,
 * turns carry `t_ms_from_session_start`, and cursor.jsonl is on the same
 * basis. So turns become chapter markers on the scrub bar, and the cursor can
 * be drawn over the frame — no calibration, provided the clocks agree, which
 * `alignmentIsPlausible` checks before trusting them.
 */
export function TrajectoryPlayer({
  sessionId,
  trajectory,
}: {
  sessionId: string
  trajectory: Trajectory
}) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [currentMs, setCurrentMs] = useState(0)
  const [selectedTurn, setSelectedTurn] = useState<TrajectoryTurn | null>(null)
  const [showCursor, setShowCursor] = useState(true)
  const [showClick, setShowClick] = useState(true)

  const durationMs = trajectory.video?.duration_ms ?? null
  const aligned = useMemo(
    () => alignmentIsPlausible(trajectory.turns, durationMs),
    [trajectory.turns, durationMs],
  )
  const markers = useMemo(
    () => (aligned ? chapterMarkers(trajectory.turns, durationMs) : []),
    [trajectory.turns, durationMs, aligned],
  )

  const { data: cursorSamples = [] } = useQuery({
    queryKey: ['cursor', sessionId, trajectory.cursor?.path],
    queryFn: () => getCursorTrack(sessionId, trajectory.cursor!.path),
    enabled: !!trajectory.cursor?.path && aligned,
    staleTime: Infinity,
  })

  // While playing, the highlighted turn follows the video. A manual selection
  // wins until playback moves past it.
  const activeTurn = useMemo(
    () => selectedTurn ?? turnAt(trajectory.turns, currentMs),
    [selectedTurn, trajectory.turns, currentMs],
  )

  const seekTo = useCallback((turn: TrajectoryTurn) => {
    setSelectedTurn(turn)
    const t = turn.t_start_ms ?? turn.t_ms
    const video = videoRef.current
    if (video && t != null) video.currentTime = t / 1000
  }, [])

  const video = trajectory.video
  const frame = selectedTurn && showClick ? selectedTurn.click : null
  const stillFrame = frame ?? selectedTurn?.screenshot ?? null

  return (
    <div className="flex h-full flex-col gap-3 p-3">
      <div className="flex items-center gap-2">
        <Badge variant="outline" className="font-mono text-[10px]">
          {trajectory.node_id}
        </Badge>
        {video && (
          <span className="font-mono text-[10px] text-muted-foreground tabular">
            {timecode(video.duration_ms)} · {bytes(video.bytes)}
          </span>
        )}
        <div className="ml-auto flex items-center gap-3 text-[10px]">
          {trajectory.cursor && aligned && (
            <label className="flex cursor-pointer items-center gap-1.5">
              <input
                type="checkbox"
                checked={showCursor}
                onChange={(e) => setShowCursor(e.target.checked)}
                className="size-3 accent-[var(--primary)]"
              />
              cursor ({trajectory.cursor.sample_count ?? cursorSamples.length})
            </label>
          )}
          <label className="flex cursor-pointer items-center gap-1.5">
            <input
              type="checkbox"
              checked={showClick}
              onChange={(e) => setShowClick(e.target.checked)}
              className="size-3 accent-[var(--primary)]"
            />
            click frames
          </label>
        </div>
      </div>

      {!aligned && trajectory.turns.length > 0 && (
        <p className="rounded-md border border-status-running/40 bg-status-running/5 px-2.5 py-2 text-[11px] leading-relaxed">
          Turn timings fall outside the recording, so the cursor overlay and
          scrub markers are hidden rather than placed wrongly. The per-turn
          frames below are unaffected.
        </p>
      )}

      <div className="relative overflow-hidden rounded-lg border bg-black">
        {video && !stillFrame ? (
          <>
            <video
              ref={videoRef}
              src={fileUrl(sessionId, video.path)}
              controls
              preload="metadata"
              className="block max-h-[42vh] w-full object-contain"
              onTimeUpdate={(e) => {
                setCurrentMs(e.currentTarget.currentTime * 1000)
                setSelectedTurn(null)
              }}
            />
            <CursorOverlay
              videoRef={videoRef}
              samples={cursorSamples}
              active={showCursor && aligned && cursorSamples.length > 0}
            />
          </>
        ) : stillFrame ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={fileUrl(sessionId, stillFrame)}
            alt={`Turn ${selectedTurn?.turn} frame`}
            className="block max-h-[42vh] w-full object-contain"
          />
        ) : (
          <div className="flex h-40 items-center justify-center text-[11px] text-muted-foreground">
            {video && !video.finalized
              ? 'The recording was never finalised — the run ended before it closed.'
              : 'No recording for this node.'}
          </div>
        )}
      </div>

      {markers.length > 0 && (
        <div className="relative h-6 shrink-0">
          <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-border" />
          {markers.map(({ turn, fraction }) => (
            <button
              key={turn.turn}
              type="button"
              onClick={() => seekTo(turn)}
              title={`Turn ${turn.turn} · ${turn.tool ?? ''} · ${timecode(turn.t_start_ms)}`}
              style={{ left: `${fraction * 100}%` }}
              className={cn(
                'absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-background transition-transform hover:scale-150',
                activeTurn?.turn === turn.turn ? 'bg-primary' : 'bg-muted-foreground',
              )}
            />
          ))}
        </div>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-2">
        <div className="scroll-slim min-h-0 overflow-auto rounded-md border p-1">
          {trajectory.turns.map((turn) => (
            <TurnRow
              key={turn.turn}
              turn={turn}
              active={activeTurn?.turn === turn.turn}
              onSeek={seekTo}
            />
          ))}
        </div>

        <div className="scroll-slim min-h-0 overflow-auto rounded-md border p-2.5">
          {activeTurn ? (
            <div className="space-y-2.5">
              <div className="flex items-center gap-2">
                <span className="font-mono text-xs font-medium">{activeTurn.tool}</span>
                <span className="font-mono text-[10px] text-muted-foreground tabular">
                  {millis(
                    activeTurn.t_ms != null && activeTurn.t_start_ms != null
                      ? activeTurn.t_ms - activeTurn.t_start_ms
                      : null,
                  )}
                </span>
                {activeTurn.click_point && (
                  <Badge variant="secondary" className="font-mono text-[10px] tabular">
                    {Math.round(activeTurn.click_point.x)},{' '}
                    {Math.round(activeTurn.click_point.y)}
                  </Badge>
                )}
              </div>

              {activeTurn.result_summary && (
                <p className="text-[11px] leading-relaxed">{activeTurn.result_summary}</p>
              )}

              {Object.keys(activeTurn.arguments).length > 0 && (
                <div>
                  <div className="mb-1 text-[10px] text-muted-foreground">Arguments</div>
                  <pre className="overflow-auto rounded bg-muted/40 p-2 font-mono text-[10px] whitespace-pre-wrap break-words">
                    {JSON.stringify(activeTurn.arguments, null, 2)}
                  </pre>
                </div>
              )}

              {activeTurn.app_state && (
                <div>
                  <div className="mb-1 text-[10px] text-muted-foreground">
                    Accessibility tree the model saw
                  </div>
                  <AppStatePanel sessionId={sessionId} path={activeTurn.app_state} />
                </div>
              )}
            </div>
          ) : (
            <p className="p-2 text-[11px] text-muted-foreground">
              Pick a turn to see its action, arguments and the accessibility tree
              the model was given.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
