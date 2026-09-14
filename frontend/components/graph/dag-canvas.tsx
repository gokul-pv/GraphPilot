'use client'

import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  type Edge,
  type Node,
  type NodeMouseHandler,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useMemo, useState } from 'react'
import { SkillNode, type SkillNodeData } from './skill-node'
import { layoutGraph, topologyKey } from '@/lib/graph/layout'
import { useRunStore } from '@/lib/store/run-store'
import { cn } from '@/lib/utils'
import type { NodeStatus } from '@/lib/api/types'

const nodeTypes = { skill: SkillNode }

const STATUS_STROKE: Record<NodeStatus, string> = {
  pending: 'var(--status-pending)',
  running: 'var(--status-running)',
  complete: 'var(--status-complete)',
  failed: 'var(--status-failed)',
  skipped: 'var(--status-skipped)',
}

function Canvas({
  selectedNodeId,
  onSelectNode,
}: {
  selectedNodeId: string | null
  onSelectNode: (nodeId: string) => void
}) {
  const graph = useRunStore((s) => s.graph)
  const nodes = useRunStore((s) => s.nodes)
  const waveOf = useRunStore((s) => s.waveOf)

  // Positions are recomputed only when topology changes. Status arrives on
  // every event and must not move anything — see lib/graph/layout.ts.
  const key = topologyKey(graph)
  const layout = useMemo(
    () => layoutGraph(graph, waveOf),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key, waveOf],
  )

  // Which nodes appeared at the last topology change, so the graph growing
  // itself — a critic inserting a gate, the planner adding recovery — is
  // visible rather than something you have to catch.
  //
  // Adjusted during render rather than in an effect: this is derived state,
  // and React re-renders with the new value before painting, so the node
  // never flashes in un-animated first. Reading it from a ref during render
  // would be unreliable under concurrent rendering.
  //
  // `fresh` is not cleared on a timer because it does not need to be: the
  // animation is a one-shot CSS keyframe that plays when the element mounts,
  // so a stale class is inert.
  const [grown, setGrown] = useState<{
    key: string
    ids: Set<string>
    fresh: Set<string>
  } | null>(null)

  if (grown === null || grown.key !== layout.key) {
    const ids = new Set(layout.nodes.map((n) => n.id))
    setGrown({
      key: layout.key,
      ids,
      // Nothing is "new" on the first layout — the whole graph arriving at
      // once is not growth.
      fresh:
        grown === null
          ? new Set<string>()
          : new Set([...ids].filter((id) => !grown.ids.has(id))),
    })
  }
  const fresh = grown?.fresh

  const rfNodes: Node[] = useMemo(
    () =>
      layout.nodes.map((n) => ({
        id: n.id,
        type: 'skill',
        position: n.position,
        selected: n.id === selectedNodeId,
        data: {
          ...n.data,
          node: nodes[n.id],
          isNew: fresh?.has(n.id) ?? false,
        } satisfies SkillNodeData,
      })),
    [layout, nodes, selectedNodeId, fresh],
  )

  const statusOf = useMemo(() => {
    const map: Record<string, NodeStatus> = {}
    for (const n of graph?.nodes ?? []) map[n.id] = n.status
    return map
  }, [graph])

  const rfEdges: Edge[] = useMemo(
    () =>
      layout.edges.map((e) => {
        // Colour an edge by where it leads: a link into a running node is the
        // live frontier of the run and should read as such.
        const target = statusOf[e.target] ?? 'pending'
        return {
          id: e.id,
          source: e.source,
          target: e.target,
          animated: target === 'running',
          style: {
            stroke: STATUS_STROKE[target],
            strokeWidth: target === 'pending' ? 1 : 1.5,
            opacity: target === 'pending' ? 0.4 : 0.8,
          },
        }
      }),
    [layout, statusOf],
  )

  const handleNodeClick: NodeMouseHandler = (_e, node) => onSelectNode(node.id)

  return (
    <ReactFlow
      nodes={rfNodes}
      edges={rfEdges}
      nodeTypes={nodeTypes}
      onNodeClick={handleNodeClick}
      fitView
      // Refit whenever the graph grows, so a node added mid-run does not
      // appear off-screen.
      fitViewOptions={{ padding: 0.2, maxZoom: 1.1 }}
      minZoom={0.15}
      maxZoom={1.8}
      proOptions={{ hideAttribution: true }}
      nodesDraggable={false}
      nodesConnectable={false}
      edgesFocusable={false}
      className="bg-background"
    >
      <Background variant={BackgroundVariant.Dots} gap={16} size={1} className="opacity-40" />
      <Controls
        showInteractive={false}
        className="!border !border-border !bg-card !shadow-sm [&_button]:!border-border [&_button]:!bg-card [&_button]:!fill-foreground hover:[&_button]:!bg-accent"
      />
      <MiniMap
        pannable
        zoomable
        className="!border !border-border !bg-card"
        maskColor="color-mix(in oklch, var(--background) 70%, transparent)"
        nodeColor={(n) => STATUS_STROKE[(n.data as SkillNodeData).status] ?? 'var(--muted)'}
      />
    </ReactFlow>
  )
}

/**
 * The DAG, live.
 *
 * `graph.nodes[].id` and `edges[].source/target` map 1:1 onto React Flow, so
 * there is no translation layer — only layout, which the backend does not
 * provide.
 */
export function DagCanvas(props: {
  selectedNodeId: string | null
  onSelectNode: (nodeId: string) => void
  className?: string
}) {
  const hasGraph = useRunStore((s) => (s.graph?.nodes.length ?? 0) > 0)

  if (!hasGraph) {
    return (
      <div
        className={cn(
          'flex h-full items-center justify-center p-8 text-center text-sm text-muted-foreground',
          props.className,
        )}
      >
        The graph appears as soon as the planner emits its first wave.
      </div>
    )
  }

  return (
    <div className={cn('h-full w-full', props.className)}>
      <ReactFlowProvider>
        <Canvas
          selectedNodeId={props.selectedNodeId}
          onSelectNode={props.onSelectNode}
        />
      </ReactFlowProvider>
    </div>
  )
}
