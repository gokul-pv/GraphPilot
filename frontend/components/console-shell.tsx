'use client'

import { useCallback, useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from '@/components/ui/resizable'
import { ScrollArea } from '@/components/ui/scroll-area'
import { SessionSidebar } from '@/components/sessions/session-sidebar'
import { Composer } from '@/components/chat/composer'
import { RunCard } from '@/components/chat/run-card'
import { DetailPanel, type DetailTab } from '@/components/detail-panel'
import { cancelRun, getSession, startRun } from '@/lib/api/client'
import { useRunStore } from '@/lib/store/run-store'
import { useRunStream } from '@/lib/store/use-run-stream'

/**
 * The console.
 *
 * Session identity lives in the URL as `?session=<id>` rather than a dynamic
 * route segment: the production build is a static export served by api.py, and
 * a dynamic segment would need `generateStaticParams` over ids that do not
 * exist at build time.
 */
export function ConsoleShell() {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [tab, setTab] = useState<DetailTab>('graph')
  const [pendingQuery, setPendingQuery] = useState<string | null>(null)

  const queryClient = useQueryClient()
  const store = useRunStore()
  const isLive = store.state === 'running' || store.state === 'queued'

  useRunStream(sessionId, true)

  // Read the session out of the URL on load and on back/forward.
  useEffect(() => {
    const sync = () => {
      const id = new URLSearchParams(window.location.search).get('session')
      setSessionId(id)
    }
    sync()
    window.addEventListener('popstate', sync)
    return () => window.removeEventListener('popstate', sync)
  }, [])

  const navigate = useCallback((id: string | null) => {
    const url = new URL(window.location.href)
    if (id) url.searchParams.set('session', id)
    else url.searchParams.delete('session')
    window.history.pushState({}, '', url)
    setSessionId(id)
    setSelectedNodeId(null)
  }, [])

  // Load a session that is not the one we just started. The store takes the
  // same shape either way, so every component below is indifferent to which
  // path filled it.
  useEffect(() => {
    if (!sessionId) {
      useRunStore.getState().reset()
      return
    }
    if (useRunStore.getState().sessionId === sessionId) return

    let cancelled = false
    getSession(sessionId)
      .then((detail) => {
        if (!cancelled) useRunStore.getState().hydrate(detail)
      })
      .catch(() => {
        // A session id that is not on disk yet is the normal case for a run
        // started moments ago; the event stream fills it in.
        if (!cancelled) {
          useRunStore.getState().begin(sessionId, pendingQuery ?? '', 'queued')
        }
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  // Keep the session list honest while a run writes to disk.
  useEffect(() => {
    if (!isLive) queryClient.invalidateQueries({ queryKey: ['sessions'] })
  }, [isLive, queryClient])

  const submit = useCallback(
    async (query: string) => {
      setPendingQuery(query)
      const { session_id } = await startRun(query)
      // Subscribe before the planner finishes — this is exactly why the POST
      // returns an id immediately.
      useRunStore.getState().begin(session_id, query, 'queued')
      navigate(session_id)
      queryClient.invalidateQueries({ queryKey: ['sessions'] })
    },
    [navigate, queryClient],
  )

  const cancel = useCallback(async () => {
    if (!sessionId) return
    try {
      await cancelRun(sessionId)
    } catch {
      // 409 means it already finished — nothing to undo.
    }
  }, [sessionId])

  const selectNode = useCallback((id: string) => {
    setSelectedNodeId(id)
    setTab('node')
  }, [])

  return (
    <div className="flex h-dvh flex-col">
      {/* react-resizable-panels v4: the group takes `orientation` (not
          `direction`), and panel sizes are percentages only as strings — a
          bare number is interpreted as pixels. */}
      <ResizablePanelGroup orientation="horizontal" className="min-h-0 flex-1">
        <ResizablePanel defaultSize="18" minSize="12" maxSize="30">
          <SessionSidebar
            activeSessionId={sessionId}
            onSelect={navigate}
            onNew={() => navigate(null)}
            refetchInterval={isLive ? 3000 : false}
          />
        </ResizablePanel>

        <ResizableHandle withHandle />

        <ResizablePanel defaultSize="40" minSize="25">
          <div className="flex h-full flex-col">
            <ScrollArea className="min-h-0 flex-1">
              <div className="space-y-3 p-3">
                {!sessionId && !store.query && (
                  <div className="px-2 py-16 text-center">
                    <h1 className="text-sm font-medium">GraphPilot</h1>
                    <p className="mx-auto mt-1.5 max-w-sm text-xs leading-relaxed text-muted-foreground">
                      Ask a question and watch the orchestrator build a DAG to
                      answer it. Every node keeps its prompt, its tokens and
                      whatever it saw on screen.
                    </p>
                  </div>
                )}

                {(store.query || sessionId) && (
                  <>
                    {store.query && (
                      <div className="flex justify-end">
                        <div className="max-w-[85%] rounded-xl rounded-br-sm bg-primary px-3 py-2 text-[13px] leading-relaxed text-primary-foreground">
                          {store.query}
                        </div>
                      </div>
                    )}
                    <RunCard
                      onSelectNode={selectNode}
                      selectedNodeId={selectedNodeId}
                      onOpenGraph={() => setTab('graph')}
                    />
                  </>
                )}
              </div>
            </ScrollArea>

            <Composer onSubmit={submit} onCancel={cancel} busy={isLive} />
          </div>
        </ResizablePanel>

        <ResizableHandle withHandle />

        <ResizablePanel defaultSize="42" minSize="25">
          <DetailPanel
            sessionId={sessionId}
            tab={tab}
            onTabChange={setTab}
            selectedNodeId={selectedNodeId}
            onSelectNode={setSelectedNodeId}
          />
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  )
}
