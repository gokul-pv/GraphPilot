import { ConsoleShell } from '@/components/console-shell'

/**
 * The whole console is one route.
 *
 * Session identity travels as `?session=<id>` rather than a dynamic segment:
 * the production build is a static export served by agent/api.py, and a
 * dynamic segment would need `generateStaticParams` over session ids that do
 * not exist at build time.
 */
export default function Page() {
  return <ConsoleShell />
}
