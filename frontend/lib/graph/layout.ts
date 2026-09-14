/**
 * Lay the DAG out with dagre and hand it to React Flow.
 *
 * graph.json has no coordinates — it is `nx.node_link_data`, pure topology —
 * so positions are computed here.
 *
 * The property that matters is **stability**. Status changes arrive
 * constantly during a run (every node_running and node_complete carries a
 * fresh graph), and a layout recomputed on each one makes the canvas jitter
 * as dagre reorders equal-rank nodes. So the layout is keyed on topology
 * alone: the sorted node ids plus the edge list. Status, tokens and results
 * change freely without moving anything, while a genuine mutation — the
 * critic inserting a gate, the planner adding a recovery node — relays once
 * and settles.
 *
 * Disconnected components are expected, not a bug: a successor that declares
 * no inputs at all becomes a second root, so the graph is a forest more often
 * than a tree. dagre handles that; it just needs to not be assumed away.
 */

import dagre from '@dagrejs/dagre'
import type { GraphPayload, NodeStatus } from '@/lib/api/types'

export const NODE_WIDTH = 208
export const NODE_HEIGHT = 76

export interface LaidOutNode {
  id: string
  position: { x: number; y: number }
  data: {
    nodeId: string
    skill: string
    status: NodeStatus
    wave?: number
  }
}

export interface LaidOutEdge {
  id: string
  source: string
  target: string
}

export interface Layout {
  nodes: LaidOutNode[]
  edges: LaidOutEdge[]
  /** Changes only when topology changes; use it to memoize. */
  key: string
}

/**
 * Identity of the *shape* of a graph, ignoring everything mutable.
 *
 * Two payloads with the same key lay out identically, so the previous
 * positions can be reused verbatim.
 */
export function topologyKey(graph: GraphPayload | null): string {
  if (!graph) return ''
  const nodes = graph.nodes.map((n) => n.id).sort()
  const edges = graph.edges.map((e) => `${e.source}>${e.target}`).sort()
  return `${nodes.join(',')}|${edges.join(',')}`
}

export function layoutGraph(
  graph: GraphPayload | null,
  waveOf: Record<string, number> = {},
): Layout {
  const key = topologyKey(graph)
  if (!graph || graph.nodes.length === 0) return { nodes: [], edges: [], key }

  const g = new dagre.graphlib.Graph()
  g.setDefaultEdgeLabel(() => ({}))
  g.setGraph({
    rankdir: 'LR',
    // Waves run in parallel, so siblings are the common case and need room
    // to breathe; ranks are sequential and can sit closer.
    nodesep: 28,
    ranksep: 72,
    marginx: 24,
    marginy: 24,
  })

  for (const node of graph.nodes) {
    g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT })
  }
  for (const edge of graph.edges) {
    // Guard against an edge naming a node that is not in `nodes`: dagre would
    // silently invent the missing endpoint and lay out a phantom.
    if (g.hasNode(edge.source) && g.hasNode(edge.target)) {
      g.setEdge(edge.source, edge.target)
    }
  }

  dagre.layout(g)

  const nodes: LaidOutNode[] = graph.nodes.map((node) => {
    const laid = g.node(node.id)
    return {
      id: node.id,
      // dagre centres nodes; React Flow positions by top-left corner.
      position: {
        x: (laid?.x ?? 0) - NODE_WIDTH / 2,
        y: (laid?.y ?? 0) - NODE_HEIGHT / 2,
      },
      data: {
        nodeId: node.id,
        skill: node.skill,
        status: node.status,
        wave: waveOf[node.id],
      },
    }
  })

  const edges: LaidOutEdge[] = graph.edges.map((e) => ({
    id: `${e.source}->${e.target}`,
    source: e.source,
    target: e.target,
  }))

  return { nodes, edges, key }
}
