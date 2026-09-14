'use client'

import { useQuery } from '@tanstack/react-query'
import { getArtifacts, getHealth } from '@/lib/api/client'
import { fetchRunCost } from '@/lib/cost'
import { rollup, useRunStore } from '@/lib/store/run-store'
import { compactNumber, dollars, integer, millis, seconds } from '@/lib/format'
import { cn } from '@/lib/utils'
import { Skeleton } from '@/components/ui/skeleton'

function Stat({
  label,
  value,
  hint,
  muted,
}: {
  label: string
  value: React.ReactNode
  hint?: string
  muted?: boolean
}) {
  return (
    <div className="rounded-lg border px-3 py-2" title={hint}>
      <div className="text-[10px] tracking-wide text-muted-foreground uppercase">{label}</div>
      <div
        className={cn(
          'mt-0.5 font-mono text-lg tabular',
          muted && 'text-muted-foreground',
        )}
      >
        {value}
      </div>
    </div>
  )
}

/** Gateway reachability, from the API's own health probe. */
function GatewayStrip() {
  const { data } = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    refetchInterval: 15_000,
  })
  if (!data) return null
  return (
    <div className="flex items-center gap-2 rounded-lg border px-3 py-2 text-[11px]">
      <span
        className={cn(
          'size-2 rounded-full',
          data.gateway.up ? 'bg-status-complete' : 'bg-status-failed',
        )}
      />
      <span className={data.gateway.up ? '' : 'text-status-failed'}>
        Gateway {data.gateway.up ? 'up' : 'unreachable'}
      </span>
      <span className="font-mono text-muted-foreground">{data.gateway.url}</span>
      {data.active_runs.length > 0 && (
        <span className="ml-auto font-mono text-muted-foreground tabular">
          {data.active_runs.length} active
        </span>
      )}
    </div>
  )
}

/**
 * Where a run's tokens and money went.
 *
 * Two sources, because neither alone is complete:
 *
 *   - the node records, summed, which are exact for ordinary skills but read
 *     zero for browser and computer (they return before the telemetry helper
 *     in core/skills.py);
 *   - the gateway ledger, which captures everything — including the cascade
 *     skills — but needs a per-node recovery query for computer nodes, whose
 *     calls are logged under a cua session tag rather than the run id.
 *
 * The panel shows both and says plainly where each is weak, rather than
 * presenting one confident number that is quietly wrong.
 */
export function CostPanel({ sessionId }: { sessionId: string | null }) {
  const nodes = useRunStore((s) => s.nodes)
  const local = rollup(nodes)

  const { data: artifacts } = useQuery({
    queryKey: ['artifacts', sessionId],
    queryFn: () => getArtifacts(sessionId!),
    enabled: !!sessionId,
  })

  const { data: cost, isLoading, error } = useQuery({
    queryKey: ['cost', sessionId, artifacts?.computer.trajectories.length ?? 0],
    queryFn: () => fetchRunCost(sessionId!, artifacts?.computer.trajectories ?? []),
    enabled: !!sessionId,
  })

  if (!sessionId) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center text-sm text-muted-foreground">
        Start or open a run to see what it spent.
      </div>
    )
  }

  const maxDollars = Math.max(...(cost?.rows.map((r) => r.dollars) ?? [0]), 0.000001)

  return (
    <div className="scroll-slim h-full space-y-3 overflow-auto p-3">
      <GatewayStrip />

      <div className="grid grid-cols-2 gap-2">
        <Stat
          label="Tokens"
          value={compactNumber(local.totalTokens)}
          hint={`${integer(local.inputTokens)} in · ${integer(local.outputTokens)} out`}
        />
        <Stat label="Node cost" value={dollars(local.cost)} />
        <Stat
          label="Compute"
          value={seconds(local.computeS)}
          hint="Sum of per-node elapsed. Exceeds wall clock when a wave ran in parallel — it is compute spent, not time passed."
        />
        <Stat
          label="LLM calls"
          value={integer(local.llmCalls)}
          hint={`${local.toolCalls} MCP tool calls`}
        />
      </div>

      {local.undercounts && (
        <p className="rounded-md border border-status-running/40 bg-status-running/5 px-2.5 py-2 text-[11px] leading-relaxed">
          The node totals above <span className="font-medium">undercount</span> this
          run: its browser or computer nodes record zero tokens regardless of what
          they spent. The gateway breakdown below is the accurate figure.
        </p>
      )}

      <div>
        <h3 className="mb-2 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
          Gateway ledger, by skill
        </h3>

        {isLoading && <Skeleton className="h-24 w-full" />}
        {error && (
          <p className="rounded-md border px-2.5 py-2 text-[11px] text-muted-foreground">
            The gateway did not answer. It is reverse-proxied through the API,
            so this fails whenever the gateway is down.
          </p>
        )}

        {cost && cost.rows.length === 0 && (
          <p className="rounded-md border px-2.5 py-2 text-[11px] text-muted-foreground">
            No calls logged for this run.
          </p>
        )}

        {cost && cost.rows.length > 0 && (
          <div className="space-y-1">
            {cost.rows.map((row) => (
              <div key={`${row.skill}-${row.provider}`} className="rounded-md border p-2">
                <div className="flex items-baseline gap-2">
                  <span className="font-mono text-xs font-medium">{row.skill}</span>
                  <span className="font-mono text-[10px] text-muted-foreground">
                    {row.provider}
                  </span>
                  {row.recovered && (
                    <span
                      className="font-mono text-[9px] text-status-running"
                      title="Recovered via this node's cua session tag — its gateway calls are not logged under the run id."
                    >
                      recovered
                    </span>
                  )}
                  <span className="ml-auto font-mono text-xs tabular">
                    {dollars(row.dollars)}
                  </span>
                </div>
                <div className="mt-1 h-1 overflow-hidden rounded-full bg-muted">
                  <div
                    className="h-full rounded-full bg-chart-1"
                    style={{ width: `${(row.dollars / maxDollars) * 100}%` }}
                  />
                </div>
                <div className="mt-1 flex gap-3 font-mono text-[10px] text-muted-foreground tabular">
                  <span>{integer(row.calls)} calls</span>
                  <span>{compactNumber(row.inputTokens + row.outputTokens)} tok</span>
                  <span>{millis(row.latencyMs)}</span>
                  {row.errors > 0 && (
                    <span className="text-status-failed">{row.errors} errors</span>
                  )}
                  {row.retries > 0 && <span>{row.retries} retries</span>}
                </div>
              </div>
            ))}

            <div className="flex items-baseline gap-2 px-2 pt-1">
              <span className="text-[11px] text-muted-foreground">Total</span>
              <span className="ml-auto font-mono text-sm tabular">
                {dollars(cost.totalDollars)}
              </span>
            </div>
          </div>
        )}

        {cost && cost.unattributedNodes.length > 0 && (
          <p className="mt-2 rounded-md border border-status-running/40 bg-status-running/5 px-2.5 py-2 text-[11px] leading-relaxed">
            {cost.unattributedNodes.join(', ')} produced no trajectory, so the tag
            needed to find their gateway calls is unrecoverable and their spend is
            missing from this total.
          </p>
        )}
      </div>
    </div>
  )
}
