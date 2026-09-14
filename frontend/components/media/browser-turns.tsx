'use client'

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fileUrl, getTextFile } from '@/lib/api/client'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import type { BrowserArtifactRun, BrowserTurn } from '@/lib/api/types'

/**
 * The legend maps the numbered boxes on a set-of-marks screenshot back to
 * page elements. Without it a marked frame is unreadable, so it sits beside
 * the image rather than behind a disclosure.
 *
 * Fetched through the query client rather than a bespoke effect: these files
 * never change once written, so caching them by path means flipping back and
 * forth between turns costs nothing.
 */
function Legend({ sessionId, path }: { sessionId: string; path: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['legend', sessionId, path],
    queryFn: () => getTextFile(sessionId, path),
    staleTime: Infinity,
  })

  if (error) {
    return <p className="p-2 text-[11px] text-muted-foreground">Legend unavailable.</p>
  }
  return (
    <pre className="scroll-slim h-full overflow-auto rounded-md border bg-muted/40 p-2 font-mono text-[10px] leading-relaxed whitespace-pre-wrap">
      {isLoading ? 'Loading…' : data}
    </pre>
  )
}

/**
 * Browser cascade screenshots, one cascade layer at a time.
 *
 * `marked` exists only on the vision / set-of-marks layer — the a11y layer
 * screenshots without annotating — so the raw⇄marked toggle appears only when
 * there is something to toggle to.
 */
export function BrowserTurns({
  sessionId,
  runs,
}: {
  sessionId: string
  runs: BrowserArtifactRun[]
}) {
  const [runIndex, setRunIndex] = useState(0)
  const [turnIndex, setTurnIndex] = useState(0)
  const [marked, setMarked] = useState(true)

  const run = runs[runIndex]
  const turn: BrowserTurn | undefined = run?.turns[turnIndex]
  const hasMarked = !!turn?.marked
  const image = hasMarked && marked ? turn.marked : turn?.raw

  if (!run || !turn) {
    return (
      <p className="p-4 text-center text-xs text-muted-foreground">
        No browser screenshots in this session.
      </p>
    )
  }

  return (
    <div className="flex h-full flex-col gap-3 p-3">
      <div className="flex flex-wrap items-center gap-2">
        {runs.length > 1 && (
          <select
            value={runIndex}
            onChange={(e) => {
              setRunIndex(Number(e.target.value))
              setTurnIndex(0)
            }}
            className="rounded border bg-card px-1.5 py-1 font-mono text-[10px]"
          >
            {runs.map((r, i) => (
              <option key={`${r.run}-${r.layer}`} value={i}>
                {r.run} · {r.layer}
              </option>
            ))}
          </select>
        )}
        <Badge variant="secondary" className="font-mono text-[10px]">
          layer: {run.layer}
        </Badge>
        {hasMarked && (
          <label className="ml-auto flex cursor-pointer items-center gap-1.5 text-[10px]">
            <input
              type="checkbox"
              checked={marked}
              onChange={(e) => setMarked(e.target.checked)}
              className="size-3 accent-[var(--primary)]"
            />
            set-of-marks overlay
          </label>
        )}
      </div>

      <div className="flex items-center gap-1">
        {run.turns.map((t, i) => (
          <button
            key={t.turn}
            type="button"
            onClick={() => setTurnIndex(i)}
            className={cn(
              'rounded px-2 py-1 font-mono text-[10px] tabular transition-colors',
              i === turnIndex
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted hover:bg-accent',
            )}
          >
            {t.turn}
          </button>
        ))}
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[3fr_2fr]">
        <div className="min-h-0 overflow-hidden rounded-lg border bg-black">
          {image ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={fileUrl(sessionId, image)}
              alt={`Turn ${turn.turn} ${hasMarked && marked ? 'annotated' : 'raw'} screenshot`}
              loading="lazy"
              className="block size-full object-contain"
            />
          ) : (
            <div className="flex h-full items-center justify-center text-[11px] text-muted-foreground">
              No screenshot for this turn.
            </div>
          )}
        </div>

        <div className="min-h-0">
          {turn.legend ? (
            <Legend sessionId={sessionId} path={turn.legend} />
          ) : (
            <p className="p-2 text-[11px] text-muted-foreground">
              No legend — this layer did not annotate the page.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
