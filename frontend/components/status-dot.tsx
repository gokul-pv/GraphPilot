'use client'

import { cn } from '@/lib/utils'
import type { NodeStatus } from '@/lib/api/types'
import { STATUS_BG, STATUS_LABEL } from '@/lib/status'

/**
 * The status glyph, used everywhere status appears.
 *
 * `running` gets a pulsing halo rather than a spinner: there can be a dozen
 * of these on screen at once during a parallel wave, and a dozen spinners is
 * noise. The halo reads as activity at the edge of vision without demanding
 * attention.
 */
export function StatusDot({
  status,
  size = 'md',
  className,
}: {
  status: NodeStatus
  size?: 'sm' | 'md'
  className?: string
}) {
  const box = size === 'sm' ? 'size-1.5' : 'size-2'
  return (
    <span
      className={cn('relative inline-flex shrink-0', box, className)}
      title={STATUS_LABEL[status]}
      data-status={status}
    >
      {status === 'running' && (
        <span
          className={cn(
            'absolute inline-flex size-full animate-ping rounded-full opacity-60',
            STATUS_BG[status],
          )}
        />
      )}
      <span
        className={cn('relative inline-flex size-full rounded-full', STATUS_BG[status])}
      />
    </span>
  )
}
