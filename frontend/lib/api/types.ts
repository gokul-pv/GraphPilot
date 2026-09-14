/**
 * The Python API's wire shapes, by hand.
 *
 * Deliberately not generated from /openapi.json: nearly every route in
 * agent/api.py is annotated `-> dict` with no `response_model`, so FastAPI's
 * schema describes them all as bare objects and a generator would emit
 * `Record<string, unknown>` for the entire API.
 *
 * Mirrors, in order:
 *   agent/core/schemas.py   — AgentResult, NodeState, Browser/ComputerOutput
 *   agent/api.py            — SessionRow (_summarise), run status
 *   agent/core/events.py    — the SSE envelope
 *   agent/core/media.py     — the artifact manifest
 *
 * When one of those changes, this file is the thing that has to change with
 * it; nothing here is enforced at runtime.
 */

// ── schemas.py ───────────────────────────────────────────────────────────────

/** Browser skill uses gateway_blocked…vlm_unavailable; computer adds the last two. */
export type ErrorCode =
  | 'gateway_blocked'
  | 'extraction_failed'
  | 'interaction_failed'
  | 'timeout'
  | 'vlm_unavailable'
  | 'permission_denied'
  | 'window_not_found'

export type NodeStatus =
  | 'pending'
  | 'running'
  | 'complete'
  | 'failed'
  | 'skipped'

/** One entry per MCP tool a skill actually dispatched. */
export interface ToolCall {
  name: string
  arguments: Record<string, unknown>
  result_preview: string
  elapsed_s: number
}

export interface NodeSpec {
  skill: string
  inputs: string[]
  metadata: Record<string, unknown>
}

export interface AgentResult {
  success: boolean
  agent_name: string
  output: Record<string, unknown>
  artifacts: string[]
  successors: NodeSpec[]
  cost: number
  elapsed_s: number
  provider: string
  error: string | null
  error_code: ErrorCode | null

  model: string
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  /**
   * Gateway-measured. Differs from elapsed_s, which is wall clock for the
   * whole node including prompt render and every MCP round trip; the gap is
   * orchestrator overhead and is intentional.
   */
  latency_ms: number
  tool_calls: ToolCall[]
  llm_calls: number
}

/** `path` is the cascade layer that actually ran. */
export interface BrowserOutput {
  url: string
  goal: string
  path: 'extract' | 'deterministic' | 'a11y' | 'vision'
  turns: number
  content: string | null
  actions: Record<string, unknown>[]
  final_url: string | null
}

export interface ComputerOutput {
  goal: string
  path: 'ax_extract' | 'deterministic' | 'ax_llm' | 'electron' | 'vision'
  turns: number
  content: string | null
  /** Thin: `{type, element_index}`. The trajectory manifest is richer. */
  actions: Record<string, unknown>[]
  window_id: string
}

export interface NodeState {
  node_id: string
  skill: string
  status: NodeStatus
  inputs: string[]
  result: AgentResult | null
  /** The exact bytes that hit the gateway, not a reconstruction. */
  prompt_sent: string | null
  started_at: number | null
  completed_at: number | null
  retries: number
}

// ── graph (networkx node_link_data) ──────────────────────────────────────────

export interface GraphNode {
  id: string
  skill: string
  inputs: string[]
  metadata: Record<string, unknown>
  status: NodeStatus
  result?: AgentResult | null
  _result_typed?: boolean
}

export interface GraphEdge {
  source: string
  target: string
}

export interface GraphPayload {
  directed: boolean
  multigraph: boolean
  graph: Record<string, unknown>
  nodes: GraphNode[]
  edges: GraphEdge[]
}

// ── api.py ───────────────────────────────────────────────────────────────────

export type RunState =
  | 'queued'
  | 'running'
  | 'complete'
  | 'failed'
  | 'cancelled'
  | 'unknown'

export interface SessionRow {
  session_id: string
  query: string
  answer: string
  node_count: number
  statuses: Partial<Record<NodeStatus, number>>
  input_tokens: number
  output_tokens: number
  cost: number
  /** Sum of per-node elapsed. Exceeds wall clock when a wave ran in parallel
   *  — it is compute spent, not time passed. */
  compute_s: number
  providers: string[]
  updated_at: number | null
  live: boolean
  state: RunState | null
}

export interface SessionDetail {
  session_id: string
  query: string
  graph: GraphPayload
  nodes: Record<string, NodeState>
  summary: SessionRow
  events: RunEvent[]
}

export interface RunStatus {
  session_id: string
  state: RunState
  query?: string
  resumed?: boolean
  queued_at?: number
  started_at?: number
  finished_at?: number
  answer?: string
  error?: string
}

export interface SkillSpec {
  description: string
  tools_allowed: string[]
  critic: boolean
  internal_successors: string[]
  temperature: number
  max_tokens: number
  provider_pin: string | null
}

export interface HealthResponse {
  ok: boolean
  gateway: { url: string; up: boolean }
  active_runs: string[]
  frontend_built: boolean
}

// ── events.py ────────────────────────────────────────────────────────────────

interface EventBase {
  seq: number
  ts: number
  session_id: string
}

/**
 * Every graph-bearing event carries the complete node-link payload, so the
 * store replaces graph state wholesale rather than diffing.
 */
export type RunEvent =
  | (EventBase & { type: 'run_start'; query: string; resumed: boolean; graph: GraphPayload })
  | (EventBase & { type: 'wave_start'; wave: number; node_ids: string[]; graph: GraphPayload })
  | (EventBase & {
      type: 'node_running'
      node_id: string
      skill: string
      inputs: string[]
      metadata: Record<string, unknown>
      started_at: number
    })
  | (EventBase & { type: 'node_complete'; node_id: string; wave: number; node: NodeState })
  | (EventBase & {
      type: 'graph_mutated'
      cause: 'critic_fail' | 'recovery_planner'
      node_id: string
      graph: GraphPayload
      verdict?: unknown
      recovery_node_id?: string
      reason?: string
      prior_complete?: unknown
    })
  | (EventBase & { type: 'wave_end'; wave: number; elapsed_s: number; graph: GraphPayload })
  | (EventBase & {
      type: 'run_complete'
      answer: string
      node_count: number
      waves: number
      critic_fail_cap_hit: string[]
      graph: GraphPayload
    })
  | (EventBase & { type: 'run_failed'; error: string })
  | (EventBase & { type: 'run_cancelled'; reason: string })

export type RunEventType = RunEvent['type']

export const TERMINAL_EVENTS: readonly RunEventType[] = [
  'run_complete',
  'run_failed',
  'run_cancelled',
]

// ── media.py ─────────────────────────────────────────────────────────────────

export interface BrowserTurn {
  turn: number
  raw: string | null
  /** Set-of-marks overlay. Only the vision layer annotates. */
  marked: string | null
  legend: string | null
}

export interface BrowserArtifactRun {
  run: string
  layer: string
  turns: BrowserTurn[]
}

export interface VisionScreenshotTurn {
  turn: number
  raw: string | null
  som: string | null
}

export interface ComputerScreenshots {
  node_id: string
  turns: VisionScreenshotTurn[]
}

export interface TrajectoryTurn {
  turn: number
  tool: string | null
  arguments: Record<string, unknown>
  result_summary: string | null
  click_point: { x: number; y: number } | null
  /** Milliseconds from session start — shares an origin with the video. */
  t_ms: number | null
  /** When the action was dispatched; with t_ms this is a duration band. */
  t_start_ms: number | null
  screenshot: string | null
  click: string | null
  /** Path only. The AX tree inside is large, so it is fetched on demand. */
  app_state: string | null
}

export interface Trajectory {
  node_id: string
  started_at_monotonic_ms: number | null
  video: {
    path: string
    duration_ms: number | null
    finalized: boolean
    bytes: number
  } | null
  cursor: { path: string; sample_count: number | null } | null
  /** The tag this node's gateway calls were logged under. */
  cua_session: string | null
  turns: TrajectoryTurn[]
}

export interface ArtifactManifest {
  session_id: string
  browser: BrowserArtifactRun[]
  computer: {
    screenshots: ComputerScreenshots[]
    trajectories: Trajectory[]
  }
}

/** One line of cursor.jsonl. */
export interface CursorSample {
  t_ms: number
  x: number
  y: number
}

/** turn-NNNNN/app_state.json, fetched lazily. */
export interface AppState {
  element_count: number
  pid: number
  tree_markdown: string
}

// ── gateway (proxied at /api/gateway/*) ──────────────────────────────────────

export interface AgentCostRow {
  agent: string
  provider: string
  calls: number
  in_tok: number
  out_tok: number
  total_latency_ms: number
  total_retries: number
  ok: number
  errors: number
  dollars: number
}

export interface CostByAgent {
  agent: Record<string, AgentCostRow[]>
}
