/**
 * Display formatting.
 *
 * Numbers in this console are compared down columns — tokens per node, cost
 * per skill, latency per turn — so everything here is built to be scanned
 * vertically and paired with the `.tabular` class.
 */

/** 12_400 → "12.4k". Keeps token counts to a constant width while streaming. */
export function compactNumber(n: number): string {
  if (!Number.isFinite(n)) return '—'
  if (Math.abs(n) < 1000) return String(Math.round(n))
  if (Math.abs(n) < 1_000_000) return `${(n / 1000).toFixed(1)}k`
  return `${(n / 1_000_000).toFixed(2)}M`
}

export function integer(n: number): string {
  return Number.isFinite(n) ? n.toLocaleString('en-US') : '—'
}

/**
 * Cost, with enough precision to stay honest at these magnitudes — a node
 * often costs a few thousandths of a cent, and rounding it to $0.00 hides
 * exactly the thing the panel exists to show.
 */
export function dollars(n: number): string {
  if (!Number.isFinite(n)) return '—'
  if (n === 0) return '$0'
  if (n < 0.01) return `$${n.toFixed(5)}`
  return `$${n.toFixed(4)}`
}

export function seconds(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return '—'
  if (s < 1) return `${Math.round(s * 1000)}ms`
  if (s < 60) return `${s.toFixed(1)}s`
  const m = Math.floor(s / 60)
  return `${m}m ${Math.round(s % 60)}s`
}

export function millis(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return '—'
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(2)}s`
}

/** Clock position inside a recording, for the trajectory scrub bar. */
export function timecode(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return '0:00'
  const total = Math.max(0, Math.round(ms / 1000))
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

export function bytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 ** 2).toFixed(1)} MB`
}

/** Relative time for the session list. Epoch *seconds*, as Python sends. */
export function relativeTime(epochSeconds: number | null): string {
  if (!epochSeconds) return ''
  const diff = Date.now() / 1000 - epochSeconds
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`
  return new Date(epochSeconds * 1000).toLocaleDateString()
}

export function truncate(s: string, max: number): string {
  return s.length <= max ? s : `${s.slice(0, max - 1)}…`
}
