'use client'

import { useMemo, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { StatusDot } from '@/components/status-dot'
import { cn } from '@/lib/utils'
import { compactNumber, integer, millis, dollars, seconds } from '@/lib/format'
import { ERROR_CODE_LABEL, STATUS_LABEL, telemetryIsMissing } from '@/lib/status'
import type { NodeState, ToolCall } from '@/lib/api/types'

/** Key/value row, the unit the telemetry tab is built from. */
function Field({
  label,
  value,
  hint,
  mono = true,
}: {
  label: string
  value: React.ReactNode
  hint?: string
  mono?: boolean
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5">
      <dt className="shrink-0 text-xs text-muted-foreground" title={hint}>
        {label}
      </dt>
      <dd className={cn('truncate text-right text-xs tabular', mono && 'font-mono')}>
        {value}
      </dd>
    </div>
  )
}

function EmptyTab({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-4 py-8 text-center text-xs text-muted-foreground">{children}</div>
  )
}

function PromptTab({ node }: { node: NodeState }) {
  if (!node.prompt_sent) {
    return (
      <EmptyTab>
        No prompt recorded. The browser and computer skills drive their own
        cascades and never render one.
      </EmptyTab>
    )
  }
  return (
    <div className="p-3">
      <div className="mb-2 flex items-center justify-between text-[11px] text-muted-foreground">
        <span>Exact bytes sent to the gateway</span>
        <span className="tabular">{integer(node.prompt_sent.length)} chars</span>
      </div>
      <pre className="scroll-slim max-h-full overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words">
        {node.prompt_sent}
      </pre>
    </div>
  )
}

function OutputTab({ node }: { node: NodeState }) {
  const output = node.result?.output
  if (!output || Object.keys(output).length === 0) {
    return <EmptyTab>This node produced no structured output.</EmptyTab>
  }

  // Browser and computer write a typed payload; surfacing the cascade layer
  // and turn count up front saves reading the JSON to find them.
  const path = typeof output.path === 'string' ? output.path : null
  const turns = typeof output.turns === 'number' ? output.turns : null

  return (
    <div className="p-3">
      {path && (
        <div className="mb-2 flex items-center gap-2">
          <Badge variant="secondary" className="font-mono text-[10px]">
            layer: {path}
          </Badge>
          {turns != null && (
            <Badge variant="outline" className="font-mono text-[10px] tabular">
              {turns} turns
            </Badge>
          )}
          {typeof output.final_url === 'string' && (
            <span className="truncate text-[11px] text-muted-foreground">
              {output.final_url}
            </span>
          )}
        </div>
      )}
      <pre className="scroll-slim overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words">
        {JSON.stringify(output, null, 2)}
      </pre>
    </div>
  )
}

function TelemetryTab({ node }: { node: NodeState }) {
  const r = node.result
  if (!r) return <EmptyTab>This node has not produced a result yet.</EmptyTab>

  const missing = telemetryIsMissing(node.skill, r)
  const overhead =
    r.latency_ms && r.elapsed_s ? r.elapsed_s - r.latency_ms / 1000 : null

  return (
    <div className="p-3">
      {missing && (
        <div className="mb-3 rounded-md border border-status-running/40 bg-status-running/5 p-2.5 text-[11px] leading-relaxed">
          <span className="font-medium text-status-running">
            Telemetry not recorded at node level.
          </span>{' '}
          The <span className="font-mono">{node.skill}</span> skill owns its own
          cascade and returns before the gateway-usage helper runs, so its
          tokens and cost read as zero here however much it actually spent. The
          Cost tab has the real figures, from the gateway ledger.
        </div>
      )}

      <dl className="divide-y">
        <Field label="Status" value={STATUS_LABEL[node.status]} mono={false} />
        <Field label="Skill" value={node.skill} />
        <Field label="Provider" value={r.provider || (missing ? '—' : 'none')} />
        <Field label="Model" value={r.model || '—'} />
        <Field
          label="Input tokens"
          value={missing ? '—' : integer(r.input_tokens)}
        />
        <Field
          label="Output tokens"
          value={missing ? '—' : integer(r.output_tokens)}
        />
        <Field
          label="Cache read"
          value={missing ? '—' : integer(r.cache_read_tokens)}
          hint="Tokens served from the provider's prompt cache"
        />
        <Field label="Cost" value={missing ? '—' : dollars(r.cost)} />
        <Field label="LLM calls" value={integer(r.llm_calls)} />
        <Field label="Retries" value={integer(node.retries)} />
        <Field
          label="Gateway latency"
          value={millis(r.latency_ms)}
          hint="Measured by the gateway, for the model call alone"
        />
        <Field
          label="Node elapsed"
          value={seconds(r.elapsed_s)}
          hint="Wall clock for the whole node, including prompt render and every tool round trip"
        />
        {overhead != null && overhead > 0 && (
          <Field
            label="Orchestrator overhead"
            value={seconds(overhead)}
            hint="elapsed − gateway latency. The gap is intentional; it is the cost of everything around the model call."
          />
        )}
      </dl>

      {r.error && (
        <div className="mt-3 rounded-md border border-status-failed/40 bg-status-failed/5 p-2.5">
          {r.error_code && (
            <div className="mb-1 text-[11px] font-medium text-status-failed">
              {ERROR_CODE_LABEL[r.error_code]}
            </div>
          )}
          <pre className="font-mono text-[11px] whitespace-pre-wrap break-words">
            {r.error}
          </pre>
        </div>
      )}
    </div>
  )
}

function ToolCallRow({ call }: { call: ToolCall }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-md border">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-2.5 py-2 text-left hover:bg-accent/50"
      >
        <span className="font-mono text-[11px] font-medium">{call.name}</span>
        <span className="ml-auto shrink-0 font-mono text-[10px] text-muted-foreground tabular">
          {seconds(call.elapsed_s)}
        </span>
      </button>
      {open && (
        <div className="space-y-2 border-t px-2.5 py-2">
          <div>
            <div className="mb-1 text-[10px] text-muted-foreground">Arguments</div>
            <pre className="overflow-auto rounded bg-muted/40 p-2 font-mono text-[10px] whitespace-pre-wrap break-words">
              {JSON.stringify(call.arguments, null, 2)}
            </pre>
          </div>
          <div>
            <div className="mb-1 text-[10px] text-muted-foreground">Result preview</div>
            <pre className="overflow-auto rounded bg-muted/40 p-2 font-mono text-[10px] whitespace-pre-wrap break-words">
              {call.result_preview}
            </pre>
          </div>
        </div>
      )}
    </div>
  )
}

function ToolsTab({ node }: { node: NodeState }) {
  const calls = node.result?.tool_calls ?? []
  if (calls.length === 0) {
    return <EmptyTab>This node dispatched no MCP tools.</EmptyTab>
  }
  return (
    <div className="space-y-1.5 p-3">
      {calls.map((call, i) => (
        <ToolCallRow key={`${call.name}-${i}`} call={call} />
      ))}
    </div>
  )
}

/**
 * Everything known about one node.
 *
 * No fetching: `node_complete` carries the whole NodeState — prompt_sent,
 * result, telemetry, tool trace — and the historical path hydrates the same
 * shape from GET /api/sessions/{sid}. So this is a pure view.
 */
export function NodeInspector({ node }: { node: NodeState | null }) {
  const tokens = useMemo(() => {
    const r = node?.result
    return (r?.input_tokens ?? 0) + (r?.output_tokens ?? 0)
  }, [node])

  if (!node) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center text-sm text-muted-foreground">
        Select a node in the graph to inspect its prompt, output and telemetry.
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      <header className="shrink-0 border-b px-3 py-2.5">
        <div className="flex items-center gap-2">
          <StatusDot status={node.status} />
          <span className="text-sm font-medium">{node.skill}</span>
          <span className="font-mono text-[11px] text-muted-foreground tabular">
            {node.node_id}
          </span>
          {tokens > 0 && (
            <Badge variant="outline" className="ml-auto font-mono text-[10px] tabular">
              {compactNumber(tokens)} tok
            </Badge>
          )}
        </div>
        {node.inputs.length > 0 && (
          <div className="mt-1.5 flex flex-wrap items-center gap-1">
            <span className="text-[10px] text-muted-foreground">inputs</span>
            {node.inputs.map((input) => (
              <code
                key={input}
                className="rounded bg-muted px-1 py-0.5 font-mono text-[10px]"
              >
                {input}
              </code>
            ))}
          </div>
        )}
      </header>

      <Tabs defaultValue="prompt" className="flex min-h-0 flex-1 flex-col gap-0">
        <TabsList
          variant="line"
          className="h-8 w-full shrink-0 justify-start rounded-none border-b px-1"
        >
          {(['prompt', 'output', 'telemetry', 'tools'] as const).map((tab) => (
            <TabsTrigger
              key={tab}
              value={tab}
              className="h-8 flex-none px-3 text-xs capitalize data-active:text-foreground"
            >
              {tab}
              {tab === 'tools' && (node.result?.tool_calls?.length ?? 0) > 0 && (
                <span className="ml-1 text-[10px] text-muted-foreground tabular">
                  {node.result?.tool_calls.length}
                </span>
              )}
            </TabsTrigger>
          ))}
        </TabsList>

        <ScrollArea className="min-h-0 flex-1">
          <TabsContent value="prompt" className="m-0">
            <PromptTab node={node} />
          </TabsContent>
          <TabsContent value="output" className="m-0">
            <OutputTab node={node} />
          </TabsContent>
          <TabsContent value="telemetry" className="m-0">
            <TelemetryTab node={node} />
          </TabsContent>
          <TabsContent value="tools" className="m-0">
            <ToolsTab node={node} />
          </TabsContent>
        </ScrollArea>
      </Tabs>
    </div>
  )
}
