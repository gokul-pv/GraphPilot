# GraphPilot Console

A chat-shaped front end for the GraphPilot orchestrator: ask a question, watch
the DAG execute live, and click into any node for the exact prompt it sent, the
tokens it spent, the tools it called, and whatever it saw on screen.

Everything it renders already existed — the orchestrator has always written its
graph, per-node prompts and cua-driver recordings to disk. Until now nothing
read them back.

## Running it

Two processes. The API must be up first; it spawns the LLM gateway on :8109 and
that can take up to 45 seconds on a cold start.

```bash
# terminal 1 — API on :8110 (spawns the gateway on :8109)
cd agent && uv run api.py

# terminal 2 — console on :3000, proxying /api/* to :8110
cd frontend && pnpm dev
```

Open <http://localhost:3000>.

### Single-origin build

`agent/api.py` serves a static export itself, so there is no second process in
production:

```bash
cd frontend && pnpm build:export   # emits out/
cd ../agent && uv run api.py       # console + API together on :8110
```

`next.config.ts` switches on `BUILD_TARGET`: unset, it installs a dev rewrite
proxy to :8110; set to `export`, it emits a static site and drops the proxy
(static export does not support rewrites, and the export is same-origin anyway).

## How it is wired

| Concern | Where |
|---|---|
| Wire types, mirrored from `agent/core/schemas.py` | `lib/api/types.ts` |
| Typed fetch + SSE URLs | `lib/api/client.ts` |
| Run state, reduced from the event stream | `lib/store/run-store.ts` |
| EventSource subscription and reconnect | `lib/store/use-run-stream.ts` |
| Dagre layout for the DAG | `lib/graph/layout.ts` |
| Trajectory ↔ video clock alignment | `lib/media/trajectory.ts` |
| Status vocabulary shared by every panel | `lib/status.ts` |

Types are hand-written rather than generated from `/openapi.json`: almost every
route in `api.py` is annotated `-> dict` with no `response_model`, so a
generator would emit `Record<string, unknown>` for the whole API.

### Three properties the store depends on

1. **Idempotent on `seq`.** `EventSource` reconnects by itself, and although
   sse-starlette emits an `id:` field, `api.py` reads the resume point from the
   `after` query parameter rather than the `Last-Event-ID` header — so a
   browser auto-reconnect replays from seq 1. Dropping events at or below
   `lastSeq` makes that harmless.
2. **Graph state is replaced, never merged.** Every graph-bearing event carries
   the complete node-link payload, so there is nothing to diff.
3. **Live and historical runs take the same shape.** `hydrate()` fills the store
   from `GET /api/sessions/{sid}`, so no component knows or cares which it is
   looking at.

### Layout stability

`graph.json` carries no coordinates, so positions come from dagre — keyed on
topology alone (sorted node ids plus edges). Status changes arrive constantly
during a run and must not move anything; only a real mutation relays.

## Two honest gaps

Both are backend data limitations that the UI surfaces rather than papers over:

- **Browser and computer nodes record no telemetry.** Both skills return early
  in `core/skills.py`, before the helper that lifts usage off the gateway
  reply, so they report provider `""` and zero tokens however much they spent.
  The inspector says so instead of showing a confident `$0.00`.
- **Computer spend is logged under the wrong session id.**
  `computer/skill.py:280` passes `session=session_name or self.session`, and
  `session_name` is always truthy, so those gateway calls are tagged with the
  cua session name rather than the run id. `lib/cost.ts` recovers the tag from
  the trajectory files and issues a second query — which works for finished
  runs but not live ones. Changing that line to `session=self.session` fixes it
  everywhere and makes the workaround unnecessary.

## Fonts

System stacks, set in `app/globals.css`. `next/font/google` would make
`next build` require network access to fonts.googleapis.com, which is a poor
dependency for a console that runs beside the agent on localhost. To use Geist
instead, add `next/font/google` back in `app/layout.tsx` and point `--font-sans`
and `--font-mono` at its variables.
