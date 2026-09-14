'use client'

import { useState } from 'react'
import { fileUrl } from '@/lib/api/client'
import { cn } from '@/lib/utils'
import type { ComputerScreenshots } from '@/lib/api/types'

/**
 * Layer-3 vision screenshots from the computer cascade.
 *
 * Written only when the cascade falls all the way through to vision, so this
 * is empty for every run that succeeded on the accessibility tree — which is
 * most of them. It exists because when it *does* fire, the set-of-marks frame
 * is the only record of what the model was actually looking at.
 */
export function VisionScreenshots({
  sessionId,
  screenshots,
}: {
  sessionId: string
  screenshots: ComputerScreenshots
}) {
  const [index, setIndex] = useState(0)
  const [som, setSom] = useState(true)

  const turn = screenshots.turns[index]
  const hasSom = !!turn?.som
  const image = hasSom && som ? turn.som : turn?.raw

  if (!turn) {
    return (
      <p className="p-4 text-center text-xs text-muted-foreground">
        No vision screenshots for this node.
      </p>
    )
  }

  return (
    <div className="flex h-full flex-col gap-3 p-3">
      <div className="flex items-center gap-2">
        <span className="font-mono text-[10px] text-muted-foreground">
          {screenshots.node_id}
        </span>
        {hasSom && (
          <label className="ml-auto flex cursor-pointer items-center gap-1.5 text-[10px]">
            <input
              type="checkbox"
              checked={som}
              onChange={(e) => setSom(e.target.checked)}
              className="size-3 accent-[var(--primary)]"
            />
            set-of-marks overlay
          </label>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1">
        {screenshots.turns.map((t, i) => (
          <button
            key={t.turn}
            type="button"
            onClick={() => setIndex(i)}
            className={cn(
              'rounded px-2 py-1 font-mono text-[10px] tabular transition-colors',
              i === index ? 'bg-primary text-primary-foreground' : 'bg-muted hover:bg-accent',
            )}
          >
            {t.turn}
          </button>
        ))}
      </div>

      <div className="min-h-0 flex-1 overflow-hidden rounded-lg border bg-black">
        {image ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={fileUrl(sessionId, image)}
            alt={`Vision turn ${turn.turn}`}
            loading="lazy"
            className="block size-full object-contain"
          />
        ) : (
          <div className="flex h-full items-center justify-center text-[11px] text-muted-foreground">
            No image for this turn.
          </div>
        )}
      </div>
    </div>
  )
}
