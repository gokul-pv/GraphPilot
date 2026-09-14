'use client'

import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { DagCanvas } from '@/components/graph/dag-canvas'
import { NodeInspector } from '@/components/inspector/node-inspector'
import { MediaPanel } from '@/components/media/media-panel'
import { CostPanel } from '@/components/cost/cost-panel'
import { useRunStore } from '@/lib/store/run-store'

export type DetailTab = 'graph' | 'node' | 'media' | 'cost'

/**
 * The right pane: four views onto the run selected on the left.
 *
 * Tab state is lifted so that clicking a node — in the graph or in the run
 * card — can pull the pane to the inspector. Mounting all four and letting the
 * Tabs primitive hide the inactive ones keeps the canvas viewport and the
 * video's playback position across tab switches.
 */
export function DetailPanel({
  sessionId,
  tab,
  onTabChange,
  selectedNodeId,
  onSelectNode,
}: {
  sessionId: string | null
  tab: DetailTab
  onTabChange: (tab: DetailTab) => void
  selectedNodeId: string | null
  onSelectNode: (nodeId: string) => void
}) {
  const nodes = useRunStore((s) => s.nodes)
  const isLive = useRunStore((s) => s.state === 'running' || s.state === 'queued')
  const selected = selectedNodeId ? (nodes[selectedNodeId] ?? null) : null

  return (
    <Tabs
      value={tab}
      onValueChange={(v) => onTabChange(v as DetailTab)}
      className="flex h-full min-h-0 flex-col gap-0"
    >
      {/* The Nova preset marks the active tab with `data-active`, not Radix's
          own `data-[state=active]`, and ships a `line` variant that is exactly
          this underline treatment. */}
      <TabsList
        variant="line"
        className="h-9 w-full shrink-0 justify-start rounded-none border-b px-1"
      >
        {(['graph', 'node', 'media', 'cost'] as const).map((value) => (
          <TabsTrigger
            key={value}
            value={value}
            className="h-9 flex-none px-3 text-xs capitalize data-active:text-foreground"
          >
            {value}
            {value === 'node' && selected && (
              <span className="ml-1 font-mono text-[10px] text-muted-foreground">
                {selected.node_id}
              </span>
            )}
          </TabsTrigger>
        ))}
      </TabsList>

      <TabsContent value="graph" className="m-0 min-h-0 flex-1">
        <DagCanvas
          selectedNodeId={selectedNodeId}
          onSelectNode={(id) => {
            onSelectNode(id)
            onTabChange('node')
          }}
        />
      </TabsContent>

      <TabsContent value="node" className="m-0 min-h-0 flex-1">
        <NodeInspector node={selected} />
      </TabsContent>

      <TabsContent value="media" className="m-0 min-h-0 flex-1">
        <MediaPanel sessionId={sessionId} isLive={isLive} />
      </TabsContent>

      <TabsContent value="cost" className="m-0 min-h-0 flex-1">
        <CostPanel sessionId={sessionId} />
      </TabsContent>
    </Tabs>
  )
}
