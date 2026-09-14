'use client'

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getArtifacts } from '@/lib/api/client'
import { BrowserTurns } from './browser-turns'
import { TrajectoryPlayer } from './trajectory-player'
import { VisionScreenshots } from './vision-screenshots'
import { cn } from '@/lib/utils'
import { Skeleton } from '@/components/ui/skeleton'

type Source =
  | { kind: 'trajectory'; nodeId: string }
  | { kind: 'vision'; nodeId: string }
  | { kind: 'browser' }

/**
 * Whatever media a session produced.
 *
 * The two cascades leave genuinely different shapes behind — the browser
 * writes bare screenshot/legend pairs, cua-driver writes a recording plus a
 * per-turn trajectory — so they get separate viewers rather than one
 * component bent to serve both.
 *
 * Everything here is optional by design: recording is best-effort, `click.png`
 * only exists on click turns, `marked.png` only on the vision layer, and a
 * session may have no media at all.
 */
export function MediaPanel({
  sessionId,
  isLive,
}: {
  sessionId: string | null
  isLive: boolean
}) {
  const [source, setSource] = useState<Source | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: ['artifacts', sessionId],
    queryFn: () => getArtifacts(sessionId!),
    enabled: !!sessionId,
    // Media lands on disk as the run proceeds, so keep checking while it is
    // live and stop once it is not.
    refetchInterval: isLive ? 4000 : false,
  })

  if (!sessionId) {
    return <Placeholder>Start or open a run to see its screenshots and recordings.</Placeholder>
  }
  if (isLoading) {
    return (
      <div className="space-y-2 p-3">
        <Skeleton className="h-6 w-40" />
        <Skeleton className="h-48 w-full" />
      </div>
    )
  }
  if (error || !data) {
    return <Placeholder>Could not read this session&rsquo;s artifacts.</Placeholder>
  }

  const trajectories = data.computer.trajectories
  const vision = data.computer.screenshots
  const browser = data.browser

  const sources: { value: Source; label: string }[] = [
    ...trajectories.map((t) => ({
      value: { kind: 'trajectory' as const, nodeId: t.node_id },
      label: `computer ${t.node_id}`,
    })),
    ...vision.map((v) => ({
      value: { kind: 'vision' as const, nodeId: v.node_id },
      label: `vision ${v.node_id}`,
    })),
    ...(browser.length > 0
      ? [{ value: { kind: 'browser' as const }, label: 'browser' }]
      : []),
  ]

  if (sources.length === 0) {
    return (
      <Placeholder>
        {isLive
          ? 'No media yet. Screenshots and recordings appear as the browser or computer skills run.'
          : 'This run produced no screenshots or recordings.'}
      </Placeholder>
    )
  }

  const active = source ?? sources[0].value
  const activeKey = active.kind === 'browser' ? 'browser' : `${active.kind}:${active.nodeId}`

  return (
    <div className="flex h-full flex-col">
      {sources.length > 1 && (
        <div className="flex shrink-0 items-center gap-1 border-b px-3 py-2">
          {sources.map(({ value, label }) => {
            const key = value.kind === 'browser' ? 'browser' : `${value.kind}:${value.nodeId}`
            return (
              <button
                key={key}
                type="button"
                onClick={() => setSource(value)}
                className={cn(
                  'rounded px-2 py-1 font-mono text-[10px] transition-colors',
                  key === activeKey
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-muted hover:bg-accent',
                )}
              >
                {label}
              </button>
            )
          })}
        </div>
      )}

      <div className="min-h-0 flex-1">
        {active.kind === 'trajectory' && (
          <TrajectoryPlayer
            key={active.nodeId}
            sessionId={sessionId}
            trajectory={trajectories.find((t) => t.node_id === active.nodeId)!}
          />
        )}
        {active.kind === 'vision' && (
          <VisionScreenshots
            key={active.nodeId}
            sessionId={sessionId}
            screenshots={vision.find((v) => v.node_id === active.nodeId)!}
          />
        )}
        {active.kind === 'browser' && <BrowserTurns sessionId={sessionId} runs={browser} />}
      </div>
    </div>
  )
}

function Placeholder({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center p-8 text-center text-sm text-muted-foreground">
      {children}
    </div>
  )
}
