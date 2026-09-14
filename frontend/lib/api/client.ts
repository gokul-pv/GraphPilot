/**
 * Typed calls against the Python console API.
 *
 * Everything is same-origin by default and reaches :8110 one of two ways:
 * the `next dev` rewrite proxy in development, or directly, because the
 * static export is served by api.py itself. NEXT_PUBLIC_API_BASE overrides
 * that — set it (together with AGENT_API_CORS_ORIGINS on the Python side)
 * only if the dev proxy ever buffers the SSE stream.
 */

import type {
  ArtifactManifest,
  AppState,
  CostByAgent,
  CursorSample,
  HealthResponse,
  RunStatus,
  SessionDetail,
  SessionRow,
  SkillSpec,
} from './types'

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? ''

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly url: string,
  ) {
    super(`${status} ${detail}`)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`
  const res = await fetch(url, {
    ...init,
    headers: { 'content-type': 'application/json', ...init?.headers },
  })
  if (!res.ok) {
    // FastAPI puts the message in `detail`; fall back to the status text when
    // the body is not the JSON we expect (a proxy error page, say).
    let detail = res.statusText
    try {
      const body = await res.json()
      if (typeof body?.detail === 'string') detail = body.detail
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, detail, url)
  }
  return res.json() as Promise<T>
}

// ── sessions ─────────────────────────────────────────────────────────────────

export const listSessions = () =>
  request<{ sessions: SessionRow[] }>('/api/sessions').then((r) => r.sessions)

export const getSession = (sid: string) =>
  request<SessionDetail>(`/api/sessions/${encodeURIComponent(sid)}`)

export const getArtifacts = (sid: string) =>
  request<ArtifactManifest>(`/api/sessions/${encodeURIComponent(sid)}/artifacts`)

/**
 * URL for one media file inside a session.
 *
 * Each segment is encoded separately: trajectory directories are named by raw
 * node id, so the path contains a colon (`computer/trajectory/n:2/…`), while
 * the slashes between segments must survive as slashes.
 */
export function fileUrl(sid: string, relPath: string): string {
  const encoded = relPath.split('/').map(encodeURIComponent).join('/')
  return `${API_BASE}/api/sessions/${encodeURIComponent(sid)}/files/${encoded}`
}

export const getAppState = (sid: string, relPath: string) =>
  fetch(fileUrl(sid, relPath)).then((r) => {
    if (!r.ok) throw new ApiError(r.status, r.statusText, r.url)
    return r.json() as Promise<AppState>
  })

/** A plain-text artifact — the set-of-marks legends, mainly. */
export const getTextFile = (sid: string, relPath: string) =>
  fetch(fileUrl(sid, relPath)).then((r) => {
    if (!r.ok) throw new ApiError(r.status, r.statusText, r.url)
    return r.text()
  })

/**
 * Parse cursor.jsonl into samples ordered by timestamp.
 *
 * Sorting is not redundant: playback binary-searches this array, so an
 * out-of-order line from a recorder hiccup would silently return the wrong
 * position rather than fail.
 */
export async function getCursorTrack(
  sid: string,
  relPath: string,
): Promise<CursorSample[]> {
  const res = await fetch(fileUrl(sid, relPath))
  if (!res.ok) throw new ApiError(res.status, res.statusText, res.url)
  const samples: CursorSample[] = []
  for (const line of (await res.text()).split('\n')) {
    if (!line.trim()) continue
    try {
      const s = JSON.parse(line)
      if (typeof s?.t_ms === 'number') samples.push(s)
    } catch {
      // A recording killed mid-write leaves a truncated final line.
    }
  }
  return samples.sort((a, b) => a.t_ms - b.t_ms)
}

// ── runs ─────────────────────────────────────────────────────────────────────

export const startRun = (query: string, sessionId?: string) =>
  request<{ session_id: string; state: 'queued' }>('/api/runs', {
    method: 'POST',
    body: JSON.stringify({ query, session_id: sessionId ?? null }),
  })

export const resumeRun = (sid: string) =>
  request<{ session_id: string; state: 'queued'; resumed: true }>(
    `/api/runs/${encodeURIComponent(sid)}/resume`,
    { method: 'POST' },
  )

export const cancelRun = (sid: string) =>
  request<{ session_id: string; state: 'cancelling' }>(
    `/api/runs/${encodeURIComponent(sid)}/cancel`,
    { method: 'POST' },
  )

export const getRunStatus = (sid: string) =>
  request<RunStatus>(`/api/runs/${encodeURIComponent(sid)}/status`)

/** The SSE endpoint. `after` is the last seq seen, so a reopen resumes. */
export function eventsUrl(sid: string, after = 0): string {
  return `${API_BASE}/api/runs/${encodeURIComponent(sid)}/events?after=${after}`
}

// ── catalogue and gateway ────────────────────────────────────────────────────

export const getSkills = () =>
  request<{ skills: Record<string, SkillSpec> }>('/api/skills').then((r) => r.skills)

export const getHealth = () => request<HealthResponse>('/api/health')

/**
 * Per-skill spend, through api.py's reverse proxy — the gateway installs no
 * CORS middleware, so a browser cannot call :8109 directly.
 *
 * Caller passes the tag to filter on. For most skills that is the run id, but
 * the computer cascade logs its calls under its own cua session name, so its
 * spend has to be fetched separately; see lib/cost.ts.
 */
export const getCostByAgent = (session?: string) =>
  request<CostByAgent>(
    `/api/gateway/v1/cost/by_agent${session ? `?session=${encodeURIComponent(session)}` : ''}`,
  )
