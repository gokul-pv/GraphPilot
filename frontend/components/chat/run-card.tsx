'use client'

import { useMemo } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { AlertTriangle, GitBranch, ShieldAlert } from 'lucide-react'
import { rollup, statusCounts, useRunStore } from '@/lib/store/run-store'
import { compactNumber, dollars, seconds } from '@/lib/format'
import { RUN_STATE_LABEL, RUN_STATE_STATUS, STATUS_TEXT } from '@/lib/status'
import { StatusDot } from '@/components/status-dot'
import { cn } from '@/lib/utils'
import type { NodeState } from '@/lib/api/types'

/** Nodes in execution order, so the list reads as a transcript of the run. */
function useOrderedNodes(): NodeState[] {
  const nodes = useRunStore((s) => s.nodes)
  const graph = useRunStore((s) => s.graph)
  return useMemo(() => {
    const order = new Map((graph?.nodes ?? []).map((n, i) => [n.id, i]))
    return Object.values(nodes).sort((a, b) => {
      // Completion time is the true execution order; fall back to graph order
      // for nodes that have not finished.
      const ta = a.started_at ?? Infinity
      const tb = b.started_at ?? Infinity
      if (ta !== tb) return ta - tb
      return (order.get(a.node_id) ?? 0) - (order.get(b.node_id) ?? 0)
    })
  }, [nodes, graph])
}

function NodeLine({
  node,
  onSelect,
  selected,
}: {
  node: NodeState
  onSelect: (id: string) => void
  selected: boolean
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(node.node_id)}
      className={cn(
        'flex w-full items-baseline gap-2 rounded px-2 py-1 text-left transition-colors',
        selected ? 'bg-accent' : 'hover:bg-accent/50',
      )}
    >
      <StatusDot status={node.status} size="sm" />
      <span className="text-[12px]">{node.skill}</span>
      <span className="font-mono text-[10px] text-muted-foreground tabular">
        {node.node_id}
      </span>
      {node.result?.error_code && (
        <span className={cn('text-[10px]', STATUS_TEXT.failed)}>
          {node.result.error_code}
        </span>
      )}
      <span className="ml-auto shrink-0 font-mono text-[10px] text-muted-foreground tabular">
        {node.result?.elapsed_s ? seconds(node.result.elapsed_s) : ''}
      </span>
    </button>
  )
}

/**
 * One run, rendered as the assistant's half of an exchange.
 *
 * The header is live: status, wave, node progress and running totals all
 * update off the SSE stream. Graph mutations get their own callouts because
 * the orchestrator growing its own DAG mid-run — a critic gating a branch, the
 * planner inserting recovery — is the most interesting thing it does and is
 * invisible in the final answer.
 */
export function RunCard({
  onSelectNode,
  selectedNodeId,
  onOpenGraph,
}: {
  onSelectNode: (id: string) => void
  selectedNodeId: string | null
  onOpenGraph: () => void
}) {
  const state = useRunStore((s) => s.state)
  const graph = useRunStore((s) => s.graph)
  const nodes = useRunStore((s) => s.nodes)
  const answer = useRunStore((s) => s.answer)
  const error = useRunStore((s) => s.error)
  const wave = useRunStore((s) => s.wave)
  const mutations = useRunStore((s) => s.mutations)
  const streamError = useRunStore((s) => s.streamError)

  const ordered = useOrderedNodes()
  const totals = rollup(nodes)
  const counts = statusCounts(graph)
  const total = graph?.nodes.length ?? 0
  const done = counts.complete + counts.failed + counts.skipped

  return (
    <div className="rounded-xl border bg-card">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-3 py-2">
        <div className="flex items-center gap-1.5">
          <StatusDot status={RUN_STATE_STATUS[state]} />
          <span className="text-xs font-medium">{RUN_STATE_LABEL[state]}</span>
        </div>
        {state === 'running' && wave > 0 && (
          <span className="font-mono text-[11px] text-muted-foreground tabular">
            wave {wave}
          </span>
        )}
        {total > 0 && (
          <span className="font-mono text-[11px] text-muted-foreground tabular">
            {done}/{total} nodes
          </span>
        )}
        {totals.totalTokens > 0 && (
          <span className="font-mono text-[11px] text-muted-foreground tabular">
            {compactNumber(totals.totalTokens)} tok
          </span>
        )}
        {totals.cost > 0 && (
          <span className="font-mono text-[11px] text-muted-foreground tabular">
            {dollars(totals.cost)}
          </span>
        )}
        <button
          type="button"
          onClick={onOpenGraph}
          className="ml-auto flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          <GitBranch className="size-3" />
          Graph
        </button>
      </header>

      {state === 'queued' && (
        <p className="border-b px-3 py-2 text-[11px] text-muted-foreground">
          Queued. Runs execute one at a time, and the first one after a restart
          also waits for the gateway to come up — up to 45 seconds.
        </p>
      )}

      {streamError && (
        <p className="flex items-center gap-1.5 border-b px-3 py-2 text-[11px] text-status-running">
          <AlertTriangle className="size-3" />
          {streamError}
        </p>
      )}

      {ordered.length > 0 && (
        <div className="space-y-0.5 p-1.5">
          {ordered.map((node) => (
            <NodeLine
              key={node.node_id}
              node={node}
              onSelect={onSelectNode}
              selected={node.node_id === selectedNodeId}
            />
          ))}
        </div>
      )}

      {mutations.map((m) => (
        <div
          key={m.seq}
          className={cn(
            'mx-1.5 mb-1.5 flex items-start gap-1.5 rounded-md border px-2 py-1.5 text-[11px] leading-relaxed',
            m.cause === 'critic_fail'
              ? 'border-mutation-critic/40 bg-mutation-critic/5'
              : 'border-mutation-recovery/40 bg-mutation-recovery/5',
          )}
        >
          <ShieldAlert
            className={cn(
              'mt-0.5 size-3 shrink-0',
              m.cause === 'critic_fail'
                ? 'text-mutation-critic'
                : 'text-mutation-recovery',
            )}
          />
          <span>
            {m.cause === 'critic_fail' ? (
              <>
                A critic rejected <code className="font-mono">{m.nodeId}</code> and
                the graph grew a retry branch.
              </>
            ) : (
              <>
                <code className="font-mono">{m.nodeId}</code> failed; the planner
                inserted{' '}
                <code className="font-mono">{m.recoveryNodeId ?? 'a recovery node'}</code>
                {m.reason ? ` — ${m.reason}` : '.'}
              </>
            )}
          </span>
        </div>
      ))}

      {error && (
        <div className="mx-1.5 mb-1.5 rounded-md border border-status-failed/40 bg-status-failed/5 px-2 py-1.5 text-[11px]">
          {error}
        </div>
      )}

      {answer && (
        <div className="border-t px-3 py-3">
          <div
            className={cn(
              'prose prose-sm dark:prose-invert max-w-none',
              '[&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-[12px]',
              '[&_pre]:overflow-auto [&_pre]:rounded-md [&_pre]:border [&_pre]:bg-muted/40 [&_pre]:p-3',
              '[&_p]:my-2 [&_ul]:my-2 [&_ol]:my-2 [&_li]:my-0.5 [&_h1]:text-base [&_h2]:text-sm [&_h3]:text-sm',
              '[&_table]:w-full [&_th]:text-left [&_td]:align-top',
              'text-[13px] leading-relaxed',
            )}
          >
            <Markdown remarkPlugins={[remarkGfm]}>{answer}</Markdown>
          </div>
        </div>
      )}
    </div>
  )
}
