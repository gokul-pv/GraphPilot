'use client'

/**
 * Subscribe a session's SSE stream into the run store.
 *
 * Two things about api.py shape this:
 *
 *   - POST /api/runs returns the session id *before* the planner runs, on
 *     purpose, so a client can subscribe before the first wave. Callers should
 *     start this hook the moment that POST resolves.
 *
 *   - The server ends the stream itself on a terminal event, which arrives as
 *     an EventSource `error` (the connection simply closes). That is
 *     indistinguishable at the browser API from a real drop, so "finished" is
 *     decided by the store having seen a terminal event, never by the socket
 *     closing.
 */

import { useEffect, useRef } from 'react'
import { eventsUrl } from '@/lib/api/client'
import type { RunEvent, RunEventType } from '@/lib/api/types'
import { useRunStore } from './run-store'

const EVENT_TYPES: RunEventType[] = [
  'run_start',
  'wave_start',
  'node_running',
  'node_complete',
  'graph_mutated',
  'wave_end',
  'run_complete',
  'run_failed',
  'run_cancelled',
]

/** Backoff for reopening a stream that dropped before finishing. */
const RETRY_MS = [500, 1000, 2000, 4000, 8000]

export function useRunStream(sessionId: string | null, enabled = true) {
  const apply = useRunStore((s) => s.apply)
  const setStreamError = useRunStore((s) => s.setStreamError)

  // Read through refs inside the effect so reconnecting does not need the
  // effect to depend on (and therefore tear down on) every seq change.
  const seqRef = useRef(0)
  const attemptRef = useRef(0)

  useEffect(() => {
    if (!sessionId || !enabled) return

    let source: EventSource | null = null
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    let closed = false

    const open = () => {
      if (closed) return
      // Resume from the last seq we saw. Harmless if the server replays more:
      // the store drops anything at or below lastSeq.
      source = new EventSource(eventsUrl(sessionId, seqRef.current))

      const onMessage = (e: MessageEvent) => {
        let event: RunEvent
        try {
          event = JSON.parse(e.data)
        } catch {
          return // a truncated frame is not worth tearing the stream down for
        }
        seqRef.current = Math.max(seqRef.current, event.seq)
        attemptRef.current = 0
        apply(event)
      }

      // sse-starlette sets a per-frame `event:` name, so the default
      // "message" listener never fires — each type must be bound by name.
      for (const type of EVENT_TYPES) {
        source.addEventListener(type, onMessage as EventListener)
      }

      source.onerror = () => {
        source?.close()
        source = null
        if (closed) return

        // The server closes the stream after a terminal event. That is a
        // normal end, not a failure, and the store already knows.
        if (useRunStore.getState().finished) return

        const attempt = attemptRef.current++
        if (attempt >= RETRY_MS.length) {
          setStreamError('lost connection to the run stream')
          return
        }
        setStreamError(`reconnecting (attempt ${attempt + 1})`)
        retryTimer = setTimeout(open, RETRY_MS[attempt])
      }
    }

    open()

    return () => {
      closed = true
      if (retryTimer) clearTimeout(retryTimer)
      source?.close()
    }
  }, [sessionId, enabled, apply, setStreamError])
}
