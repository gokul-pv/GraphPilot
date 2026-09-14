'use client'

import { Handle, Position, type NodeProps } from '@xyflow/react'
import { memo } from 'react'
import { cn } from '@/lib/utils'
import { compactNumber, seconds } from '@/lib/format'
import { STATUS_BORDER, STATUS_TEXT, telemetryIsMissing } from '@/lib/status'
import { StatusDot } from '@/components/status-dot'
import type { NodeState, NodeStatus } from '@/lib/api/types'

export interface SkillNodeData extends Record<string, unknown> {
  nodeId: string
  skill: string
  status: NodeStatus
  wave?: number
  node?: NodeState
  selected?: boolean
  isNew?: boolean
}

/**
 * One node on the canvas.
 *
 * Deliberately dense: skill, status, wave, provider, tokens and elapsed are
 * all visible without a click, because the whole point of the canvas is to
 * see where a run is spending itself at a glance. The inspector is for depth,
 * not for basic facts.
 *
 * Failures show their `error_code` rather than the free-text error — the code
 * is a closed taxonomy and fits, the message does not.
 */
function SkillNodeImpl({ data, selected }: NodeProps) {
  const d = data as SkillNodeData
  const result = d.node?.result
  const missingTelemetry = telemetryIsMissing(d.skill, result ?? null)
  const tokens = (result?.input_tokens ?? 0) + (result?.output_tokens ?? 0)

  return (
    <div
      className={cn(
        'group relative w-52 rounded-lg border bg-card px-3 py-2 shadow-sm transition-all',
        STATUS_BORDER[d.status],
        selected && 'ring-2 ring-ring ring-offset-2 ring-offset-background',
        d.status === 'running' && 'shadow-[0_0_0_3px_var(--status-running)]/10',
        // A node that only just appeared is the graph growing itself — the
        // most interesting thing this orchestrator does, so it announces.
        d.isNew && 'animate-in fade-in zoom-in-95 duration-500',
      )}
    >
      <Handle type="target" position={Position.Left} className="!size-1.5 !border-0 !bg-border" />

      <div className="flex items-center gap-1.5">
        <StatusDot status={d.status} />
        <span className="truncate text-[13px] font-medium">{d.skill}</span>
        <span className="ml-auto font-mono text-[10px] text-muted-foreground tabular">
          {d.nodeId}
        </span>
      </div>

      <div className="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground tabular">
        {d.wave != null && <span>w{d.wave}</span>}
        {result?.provider && <span className="truncate">{result.provider}</span>}
        {missingTelemetry ? (
          <span className="italic opacity-70">no node telemetry</span>
        ) : (
          tokens > 0 && <span>{compactNumber(tokens)} tok</span>
        )}
        {result?.elapsed_s ? <span className="ml-auto">{seconds(result.elapsed_s)}</span> : null}
      </div>

      {result?.error_code && (
        <div className={cn('mt-1 truncate text-[10px] font-medium', STATUS_TEXT.failed)}>
          {result.error_code}
        </div>
      )}

      <Handle type="source" position={Position.Right} className="!size-1.5 !border-0 !bg-border" />
    </div>
  )
}

export const SkillNode = memo(SkillNodeImpl)
