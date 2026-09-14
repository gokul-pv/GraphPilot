'use client'

import { useQuery } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { listSessions } from '@/lib/api/client'
import { compactNumber, dollars, relativeTime, truncate } from '@/lib/format'
import { RUN_STATE_STATUS } from '@/lib/status'
import { StatusDot } from '@/components/status-dot'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import type { SessionRow } from '@/lib/api/types'

function statusOf(row: SessionRow) {
  if (row.live) return 'running' as const
  if (row.state) return RUN_STATE_STATUS[row.state]
  if (row.statuses.failed) return 'failed' as const
  if (row.statuses.complete) return 'complete' as const
  return 'pending' as const
}

/**
 * Past runs, newest first.
 *
 * Each row is one exchange: the orchestrator runs one DAG per query and
 * returns one answer, so a session *is* a conversation turn. Follow-ups start
 * their own run rather than extending this one.
 */
export function SessionSidebar({
  activeSessionId,
  onSelect,
  onNew,
  refetchInterval,
}: {
  activeSessionId: string | null
  onSelect: (sessionId: string) => void
  onNew: () => void
  refetchInterval: number | false
}) {
  const { data: sessions, isLoading } = useQuery({
    queryKey: ['sessions'],
    queryFn: listSessions,
    refetchInterval,
  })

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b px-3 py-2.5">
        <span className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
          Runs
        </span>
        <Button
          size="sm"
          variant="ghost"
          onClick={onNew}
          className="ml-auto h-7 gap-1 px-2 text-xs"
        >
          <Plus className="size-3.5" />
          New
        </Button>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-0.5 p-1.5">
          {isLoading &&
            Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-14 w-full" />
            ))}

          {sessions?.length === 0 && (
            <p className="px-2 py-6 text-center text-xs text-muted-foreground">
              No runs yet. Ask something below to start one.
            </p>
          )}

          {sessions?.map((row) => {
            const status = statusOf(row)
            const active = row.session_id === activeSessionId
            return (
              <button
                key={row.session_id}
                type="button"
                onClick={() => onSelect(row.session_id)}
                className={cn(
                  'w-full rounded-md px-2 py-2 text-left transition-colors',
                  active ? 'bg-accent' : 'hover:bg-accent/50',
                )}
              >
                <div className="flex items-center gap-1.5">
                  <StatusDot status={status} size="sm" />
                  <span className="truncate text-[12px] leading-snug">
                    {row.query ? truncate(row.query, 42) : row.session_id}
                  </span>
                </div>
                <div className="mt-1 flex items-center gap-2 pl-3 font-mono text-[10px] text-muted-foreground tabular">
                  <span>{row.node_count} nodes</span>
                  {row.input_tokens + row.output_tokens > 0 && (
                    <span>{compactNumber(row.input_tokens + row.output_tokens)} tok</span>
                  )}
                  {row.cost > 0 && <span>{dollars(row.cost)}</span>}
                  <span className="ml-auto">{relativeTime(row.updated_at)}</span>
                </div>
              </button>
            )
          })}
        </div>
      </ScrollArea>
    </div>
  )
}
