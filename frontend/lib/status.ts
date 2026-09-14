/**
 * One vocabulary for node and run status.
 *
 * Status is the most repeated signal in this console — graph nodes, session
 * rows, wave banners, the inspector header, the run card — and it has to read
 * the same in all of them. Spelling the colours out per component is how that
 * drifts, so they live here against the `--status-*` tokens in globals.css.
 */

import type { ErrorCode, NodeStatus, RunState } from '@/lib/api/types'

export const STATUS_LABEL: Record<NodeStatus, string> = {
  pending: 'Pending',
  running: 'Running',
  complete: 'Complete',
  failed: 'Failed',
  skipped: 'Skipped',
}

/** Tailwind text colour per status. */
export const STATUS_TEXT: Record<NodeStatus, string> = {
  pending: 'text-status-pending',
  running: 'text-status-running',
  complete: 'text-status-complete',
  failed: 'text-status-failed',
  skipped: 'text-status-skipped',
}

/** Background for a filled dot or ring. */
export const STATUS_BG: Record<NodeStatus, string> = {
  pending: 'bg-status-pending',
  running: 'bg-status-running',
  complete: 'bg-status-complete',
  failed: 'bg-status-failed',
  skipped: 'bg-status-skipped',
}

export const STATUS_BORDER: Record<NodeStatus, string> = {
  pending: 'border-status-pending/40',
  running: 'border-status-running',
  complete: 'border-status-complete/60',
  failed: 'border-status-failed',
  skipped: 'border-status-skipped/40',
}

export const RUN_STATE_LABEL: Record<RunState, string> = {
  queued: 'Queued',
  running: 'Running',
  complete: 'Complete',
  failed: 'Failed',
  cancelled: 'Cancelled',
  unknown: 'Unknown',
}

/** Run states map onto the node status ramp so the palette stays closed. */
export const RUN_STATE_STATUS: Record<RunState, NodeStatus> = {
  queued: 'pending',
  running: 'running',
  complete: 'complete',
  failed: 'failed',
  cancelled: 'skipped',
  unknown: 'pending',
}

/**
 * Plain-English gloss for the structured failure codes, from the taxonomy in
 * core/schemas.py. A raw `gateway_blocked` tells an operator nothing about
 * what to do next; "the page never rendered" does.
 */
export const ERROR_CODE_LABEL: Record<ErrorCode, string> = {
  gateway_blocked: 'Blocked — CAPTCHA, login wall, or the page never rendered',
  extraction_failed: 'Rendered, but no useful content could be extracted',
  interaction_failed: 'Could not complete the goal within the turn cap',
  timeout: 'Wall-clock cap hit',
  vlm_unavailable: 'Every vision provider refused or returned 503',
  permission_denied: 'Accessibility permission missing, or cua-driver is not running',
  window_not_found: 'Target window was not visible',
}

/** Skills that own their own cascade and bypass the LLM chat dispatch. */
export const CASCADE_SKILLS = new Set(['browser', 'computer'])

/**
 * Whether a node's telemetry is structurally absent rather than genuinely
 * zero. The browser and computer skills return early in core/skills.py,
 * before the helper that lifts usage off the gateway reply, so they always
 * record provider "" and zero tokens — however much they actually spent.
 */
export function telemetryIsMissing(skill: string, result: { provider: string } | null) {
  return CASCADE_SKILLS.has(skill) && !!result && !result.provider
}
