import type { Metadata } from 'next'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Providers } from './providers'
import './globals.css'

export const metadata: Metadata = {
  title: 'GraphPilot Console',
  description: 'Run, watch and audit GraphPilot agent DAGs.',
}

export default function RootLayout({ children }: LayoutProps<'/'>) {
  return (
    // Dark by default: this is an operator console that sits open beside a
    // terminal. The light palette is fully defined in globals.css, so removing
    // this one class is all it takes to flip.
    <html lang="en" className="dark h-full antialiased">
      <body className="h-full">
        <Providers>
          <TooltipProvider>{children}</TooltipProvider>
        </Providers>
      </body>
    </html>
  )
}
