/**
 * One run's live state, reduced from the SSE event stream.
 *
 * Three properties this depends on, all deliberate:
 *
 *   1. **Idempotent on `seq`.** Events at or below `lastSeq` are dropped.
 *      EventSource reconnects on its own, and although sse-starlette emits an
 *      `id:` field, api.py reads the resume point from the `after` query
 *      parameter rather than the `Last-Event-ID` header — so a browser
 *      auto-reconnect replays the session from seq 1. Filtering by seq makes
 *      that harmless and needs no backend change.
 *
 *   2. **Graph state is replaced, never merged.** Every graph-bearing event
 *      carries the complete node-link payload (persistence.graph_to_payload),
 *      so there is nothing to diff and no way to drift out of sync.
 *
 *   3. **Live and historical runs produce the same shape.** `hydrate()` fills
 *      this from GET /api/sessions/{sid}, so every component downstream is
 *      indifferent to whether it is watching a run or reading one back.
 */

import { create } from 'zustand'
import type {
  GraphPayload,
  NodeState,
  RunEvent,
  RunState,
  SessionDetail,
} from '@/lib/api/types'

/** Why the orchestrator grew its own graph mid-run. */
export interface Mutation {
  seq: number
  cause: 'critic_fail' | 'recovery_planner'
  nodeId: string
  recoveryNodeId?: string
  reason?: string
}

export interface RunSnapshot {
  sessionId: string | null
  query: string
  state: RunState
  /** Why the run ended, when it ended badly. */
  error: string | null
  answer: string | null

  graph: GraphPayload | null
  /** Full records, keyed by node id. Carries prompt_sent and telemetry. */
  nodes: Record<string, NodeState>
  /** node id → wave it ran in, from wave_start. */
  waveOf: Record<string, number>
  /** Nodes the backend says are running right now. */
  running: Set<string>

  wave: number
  mutations: Mutation[]
  lastSeq: number
  /** True once a terminal event has arrived; the server closes the stream. */
  finished: boolean
  /** Set when the stream drops and we are waiting on a reopen. */
  streamError: string | null
}

const EMPTY: RunSnapshot = {
  sessionId: null,
  query: '',
  state: 'unknown',
  error: null,
  answer: null,
  graph: null,
  nodes: {},
  waveOf: {},
  running: new Set(),
  wave: 0,
  mutations: [],
  lastSeq: 0,
  finished: false,
  streamError: null,
}

interface RunStore extends RunSnapshot {
  /** Begin tracking a session, discarding whatever was here before. */
  begin: (sessionId: string, query: string, state: RunState) => void
  /** Fill from GET /api/sessions/{sid} — the replay path. */
  hydrate: (detail: SessionDetail) => void
  apply: (event: RunEvent) => void
  setState: (state: RunState) => void
  setStreamError: (message: string | null) => void
  reset: () => void
}

export const useRunStore = create<RunStore>((set, get) => ({
  ...EMPTY,

  begin: (sessionId, query, state) =>
    set({ ...EMPTY, running: new Set(), sessionId, query, state }),

  hydrate: (detail) => {
    // The detail view's `events` are the same replay buffer the stream would
    // deliver, so feeding them through the same reducer keeps one code path.
    set({
      ...EMPTY,
      running: new Set(),
      sessionId: detail.session_id,
      query: detail.query,
      graph: detail.graph,
      nodes: detail.nodes,
      answer: detail.summary.answer || null,
      state: detail.summary.state ?? 'unknown',
    })
    for (const event of detail.events) get().apply(event)
  },

  setState: (state) => set({ state }),
  setStreamError: (streamError) => set({ streamError }),
  reset: () => set({ ...EMPTY, running: new Set() }),

  apply: (event) => {
    const prev = get()

    // Replay after a reconnect, or an event for a session we are no longer
    // watching. Either way it must not disturb current state.
    if (event.seq <= prev.lastSeq) return
    if (prev.sessionId && event.session_id !== prev.sessionId) return

    const next: Partial<RunSnapshot> = { lastSeq: event.seq, streamError: null }

    switch (event.type) {
      case 'run_start':
        next.graph = event.graph
        next.query = event.query
        next.state = 'running'
        break

      case 'wave_start': {
        next.graph = event.graph
        next.wave = event.wave
        const waveOf = { ...prev.waveOf }
        for (const id of event.node_ids) waveOf[id] = event.wave
        next.waveOf = waveOf
        break
      }

      case 'node_running': {
        const running = new Set(prev.running)
        running.add(event.node_id)
        next.running = running
        next.nodes = {
          ...prev.nodes,
          [event.node_id]: {
            // A node can be re-run on resume, so keep whatever is known and
            // overlay the fresh start rather than dropping the old record.
            ...prev.nodes[event.node_id],
            node_id: event.node_id,
            skill: event.skill,
            status: 'running',
            inputs: event.inputs,
            started_at: event.started_at,
            result: prev.nodes[event.node_id]?.result ?? null,
            prompt_sent: prev.nodes[event.node_id]?.prompt_sent ?? null,
            completed_at: null,
            retries: prev.nodes[event.node_id]?.retries ?? 0,
          },
        }
        break
      }

      case 'node_complete': {
        // The frame carries the whole NodeState — prompt_sent, result,
        // telemetry — so the inspector never has to fetch anything.
        const running = new Set(prev.running)
        running.delete(event.node_id)
        next.running = running
        next.nodes = { ...prev.nodes, [event.node_id]: event.node }
        if (event.wave) next.waveOf = { ...prev.waveOf, [event.node_id]: event.wave }
        break
      }

      case 'graph_mutated':
        next.graph = event.graph
        next.mutations = [
          ...prev.mutations,
          {
            seq: event.seq,
            cause: event.cause,
            nodeId: event.node_id,
            recoveryNodeId: event.recovery_node_id,
            reason: event.reason,
          },
        ]
        break

      case 'wave_end':
        next.graph = event.graph
        break

      case 'run_complete':
        next.graph = event.graph
        next.answer = event.answer
        next.state = 'complete'
        next.finished = true
        next.running = new Set()
        break

      case 'run_failed':
        next.error = event.error
        next.state = 'failed'
        next.finished = true
        next.running = new Set()
        break

      case 'run_cancelled':
        next.error = event.reason
        next.state = 'cancelled'
        next.finished = true
        next.running = new Set()
        break
    }

    set(next)
  },
}))

// ── selectors ────────────────────────────────────────────────────────────────

/**
 * Per-run totals from the node records.
 *
 * `undercounts` is not a caveat to bury: the browser and computer skills
 * return before the telemetry helper in core/skills.py runs, so their nodes
 * record provider "" and zero tokens no matter how much they actually spent.
 * The UI has to say so rather than render a confident $0.00, and point at the
 * gateway rollup, which does capture it.
 */
export function rollup(nodes: Record<string, NodeState>) {
  let inputTokens = 0
  let outputTokens = 0
  let cacheReadTokens = 0
  let cost = 0
  let computeS = 0
  let llmCalls = 0
  let toolCalls = 0
  let undercounts = false
  const providers = new Set<string>()

  for (const node of Object.values(nodes)) {
    const r = node.result
    if (!r) continue
    inputTokens += r.input_tokens || 0
    outputTokens += r.output_tokens || 0
    cacheReadTokens += r.cache_read_tokens || 0
    cost += r.cost || 0
    computeS += r.elapsed_s || 0
    llmCalls += r.llm_calls || 0
    toolCalls += r.tool_calls?.length || 0
    if (r.provider) providers.add(r.provider)
    if ((node.skill === 'browser' || node.skill === 'computer') && r.success) {
      undercounts = true
    }
  }

  return {
    inputTokens,
    outputTokens,
    cacheReadTokens,
    totalTokens: inputTokens + outputTokens,
    cost,
    computeS,
    llmCalls,
    toolCalls,
    providers: [...providers].sort(),
    undercounts,
  }
}

/** Status counts for the progress strip. */
export function statusCounts(graph: GraphPayload | null) {
  const counts = { pending: 0, running: 0, complete: 0, failed: 0, skipped: 0 }
  for (const node of graph?.nodes ?? []) counts[node.status] += 1
  return counts
}
