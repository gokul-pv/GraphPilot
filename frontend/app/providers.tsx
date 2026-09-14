'use client'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'

export function Providers({ children }: { children: React.ReactNode }) {
  // One client per mount, created lazily so it is never shared across
  // requests during SSR.
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // The console reads a local filesystem, not a network API; the
            // cost of a refetch is nil and stale rows are actively confusing
            // while a run is writing to disk.
            staleTime: 2_000,
            refetchOnWindowFocus: true,
            retry: 1,
          },
        },
      }),
  )
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}
