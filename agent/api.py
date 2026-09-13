"""HTTP API for the GraphPilot console.

The orchestrator is a CLI: `flow.py "<query>"` prints progress and returns one
string. This wraps it for a browser — start a run, stream it as it executes,
read any past run back off disk — and proxies the gateway so the frontend
talks to a single origin.

Run it with:

    cd agent && uv run api.py          # :8110, serves the built frontend too

Three things here are load-bearing rather than incidental:

  - **One Executor, built once.** `Executor.__init__` calls `ensure_gateway()`,
    which will spawn the gateway subprocess and block up to 45s waiting for it.
    Doing that per request would make the first click of every session hang, so
    it happens once, off the event loop, behind a lock.

  - **Runs are serialised.** `Executor.run` reads and writes module-global
    memory state, and the gateway enforces per-provider rate limits that
    concurrent DAGs would burn through immediately. A second run queues.

  - **Session ids are validated before touching the filesystem.**
    `SessionStore.__init__` mkdirs its directory and does no path sanitising,
    so an unchecked path parameter both escapes the sessions root and litters
    empty directories.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import persistence
from events import bus
from gateway import GATEWAY_URL, ensure_gateway
from persistence import SessionStore, list_sessions
from settings import API_PORT

ROOT = Path(__file__).parent
FRONTEND_DIST = ROOT.parent / "frontend" / "dist"
PORT = API_PORT

# Session ids are generated as f"run-{uuid4().hex[:8]}" but --resume accepts
# anything — including ids from older runs, which used an "s8-" prefix — so
# keep this permissive about shape and strict about traversal.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

app = FastAPI(title="GraphPilot Console API")


# ── executor lifecycle ───────────────────────────────────────────────────────

class _Runner:
    """Owns the single Executor and the one-at-a-time run slot."""

    def __init__(self) -> None:
        self._executor = None
        self._build_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self.tasks: dict[str, asyncio.Task] = {}
        self.status: dict[str, dict] = {}

    async def executor(self):
        async with self._build_lock:
            if self._executor is None:
                from flow import Executor
                # ensure_gateway() can block for 45s spawning the gateway.
                self._executor = await asyncio.to_thread(Executor)
            return self._executor

    def _record(self, sid: str, **fields: Any) -> None:
        self.status.setdefault(sid, {})
        self.status[sid].update(fields)

    async def start(self, sid: str, query: str, *, resume: bool = False) -> None:
        """Queue a run. Returns as soon as the task is scheduled."""
        self._record(sid, session_id=sid, state="queued", query=query,
                     resumed=resume, queued_at=time.time())
        task = asyncio.create_task(self._execute(sid, query, resume))
        self.tasks[sid] = task

    async def _execute(self, sid: str, query: str, resume: bool) -> None:
        emit = bus.emitter(sid)
        try:
            executor = await self.executor()
        except Exception as e:
            self._record(sid, state="failed", error=f"gateway unavailable: {e}")
            emit("run_failed", error=f"gateway unavailable: {e}")
            return

        async with self._run_lock:
            self._record(sid, state="running", started_at=time.time())
            try:
                answer = await executor.run(query, session_id=sid,
                                            resume=resume, emit=emit)
                self._record(sid, state="complete", answer=answer,
                             finished_at=time.time())
            except asyncio.CancelledError:
                self._record(sid, state="cancelled", finished_at=time.time())
                # Nodes stay "running" on disk; Executor.run resets them to
                # pending on resume, which is what makes cancel recoverable.
                emit("run_cancelled", reason="cancelled by request")
                raise
            except Exception as e:
                self._record(sid, state="failed", error=f"{type(e).__name__}: {e}",
                             finished_at=time.time())
                emit("run_failed", error=f"{type(e).__name__}: {e}")
            finally:
                self.tasks.pop(sid, None)

    def cancel(self, sid: str) -> bool:
        task = self.tasks.get(sid)
        if task is None or task.done():
            return False
        task.cancel()
        return True


runner = _Runner()


# ── helpers ──────────────────────────────────────────────────────────────────

def _valid_sid(sid: str) -> str:
    if not SESSION_ID_RE.match(sid):
        raise HTTPException(400, f"malformed session id: {sid!r}")
    return sid


def _existing_sid(sid: str) -> str:
    """Validate shape AND existence before constructing a SessionStore.

    SessionStore mkdirs on construct, so reading an unknown id would create
    an empty session rather than 404.
    """
    _valid_sid(sid)
    if sid not in set(list_sessions()):
        raise HTTPException(404, f"no such session: {sid}")
    return sid


def _raw_graph(sid: str) -> dict | None:
    """Read graph.json without reviving AgentResults.

    The console renders the JSON shape directly, and skipping the Pydantic
    round trip means one malformed session cannot raise SessionLoadError and
    take out the whole listing.
    """
    path = persistence.SESSIONS_ROOT / sid / "graph.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # graph.json is written atomically, but a reader can still catch the
        # moment between tmp-write and os.replace on some filesystems.
        return None


def _summarise(sid: str) -> dict:
    """One row for the session list."""
    graph = _raw_graph(sid)
    nodes = (graph or {}).get("nodes", [])
    statuses: dict[str, int] = {}
    in_tok = out_tok = 0
    cost = 0.0
    elapsed = 0.0
    started: float | None = None
    answer = ""
    providers: set[str] = set()

    for n in nodes:
        statuses[n.get("status", "pending")] = statuses.get(n.get("status", "pending"), 0) + 1
        r = n.get("result") or {}
        if not isinstance(r, dict):
            continue
        in_tok += r.get("input_tokens") or 0
        out_tok += r.get("output_tokens") or 0
        cost += r.get("cost") or 0.0
        elapsed += r.get("elapsed_s") or 0.0
        if r.get("provider"):
            providers.add(r["provider"])
        if n.get("skill") == "formatter":
            fa = (r.get("output") or {}).get("final_answer")
            if isinstance(fa, str) and fa.strip():
                answer = fa

    query = ""
    qpath = persistence.SESSIONS_ROOT / sid / "query.txt"
    if qpath.exists():
        try:
            query = qpath.read_text()
        except OSError:
            pass
    try:
        started = (persistence.SESSIONS_ROOT / sid).stat().st_mtime
    except OSError:
        pass

    live = bus.is_live(sid) and sid in runner.tasks
    return {
        "session_id": sid,
        "query": query,
        "answer": answer,
        "node_count": len(nodes),
        "statuses": statuses,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost": round(cost, 6),
        # Sum of per-node elapsed. Exceeds wall clock whenever a wave ran in
        # parallel, which is the point — it is compute spent, not time passed.
        "compute_s": round(elapsed, 2),
        "providers": sorted(providers),
        "updated_at": started,
        "live": live,
        "state": (runner.status.get(sid) or {}).get("state"),
    }


# ── sessions ─────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def get_sessions() -> dict:
    rows = [_summarise(sid) for sid in list_sessions()]
    rows.sort(key=lambda r: r["updated_at"] or 0, reverse=True)
    return {"sessions": rows}


@app.get("/api/sessions/{sid}")
async def get_session(sid: str) -> dict:
    _existing_sid(sid)
    store = SessionStore(sid)
    graph = _raw_graph(sid)
    if graph is None:
        raise HTTPException(404, f"session {sid} has no graph on disk yet")

    # Node files carry prompt_sent and measured timestamps, which graph.json
    # does not. Read them raw for the same reason as the graph.
    nodes: dict[str, dict] = {}
    if store.nodes_dir.exists():
        for p in sorted(store.nodes_dir.glob("n_*.json")):
            try:
                ns = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            nodes[ns["node_id"]] = ns

    return {
        "session_id": sid,
        "query": store.read_query() if store.query_path.exists() else "",
        "graph": graph,
        "nodes": nodes,
        "summary": _summarise(sid),
        "events": bus.history(sid),
    }


@app.get("/api/sessions/{sid}/nodes/{nid}")
async def get_node(sid: str, nid: str) -> dict:
    _existing_sid(sid)
    state = SessionStore(sid).read_node(nid)
    if state is None:
        raise HTTPException(404, f"no node {nid} in session {sid}")
    return state.model_dump(mode="json")


# ── runs ─────────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    query: str = Field(min_length=1)
    session_id: str | None = None


@app.post("/api/runs")
async def post_run(req: RunRequest) -> dict:
    """Start a run and return its session id immediately.

    The id is generated here rather than inside `Executor.run` so the client
    can open the event stream before the planner finishes — otherwise the
    first wave is already over by the time anyone is listening.
    """
    import uuid
    sid = _valid_sid(req.session_id) if req.session_id else f"run-{uuid.uuid4().hex[:8]}"
    if sid in runner.tasks:
        raise HTTPException(409, f"session {sid} is already running")
    await runner.start(sid, req.query)
    return {"session_id": sid, "state": "queued"}


@app.post("/api/runs/{sid}/resume")
async def post_resume(sid: str) -> dict:
    _existing_sid(sid)
    if sid in runner.tasks:
        raise HTTPException(409, f"session {sid} is already running")
    store = SessionStore(sid)
    query = store.read_query() if store.query_path.exists() else ""
    await runner.start(sid, query, resume=True)
    return {"session_id": sid, "state": "queued", "resumed": True}


@app.post("/api/runs/{sid}/cancel")
async def post_cancel(sid: str) -> dict:
    _valid_sid(sid)
    if not runner.cancel(sid):
        raise HTTPException(409, f"session {sid} is not running")
    return {"session_id": sid, "state": "cancelling"}


@app.get("/api/runs/{sid}/status")
async def get_run_status(sid: str) -> dict:
    _valid_sid(sid)
    return runner.status.get(sid) or {"session_id": sid, "state": "unknown"}


@app.get("/api/runs/{sid}/events")
async def get_run_events(sid: str, request: Request, after: int = 0):
    """Server-sent events for one run.

    `after` is the last `seq` the client saw, so a reconnect resumes instead
    of replaying from the beginning.
    """
    _valid_sid(sid)

    async def stream():
        async for event in bus.subscribe(sid, after_seq=after):
            if await request.is_disconnected():
                break
            yield {"event": event["type"], "id": str(event["seq"]),
                   "data": json.dumps(event)}

    return EventSourceResponse(stream())


# ── catalogue ────────────────────────────────────────────────────────────────

_SKILLS_CACHE: dict | None = None


@app.get("/api/skills")
async def get_skills() -> dict:
    """The skill catalogue from agent_config.yaml.

    The console labels nodes with `description` and marks which skills gate
    their children with a critic, so it needs the same yaml the registry reads.
    """
    global _SKILLS_CACHE
    if _SKILLS_CACHE is None:
        cfg = yaml.safe_load((ROOT / "agent_config.yaml").read_text()) or {}
        _SKILLS_CACHE = {
            name: {
                "description": spec.get("description", ""),
                "tools_allowed": spec.get("tools_allowed", []),
                "critic": bool(spec.get("critic", False)),
                "internal_successors": spec.get("internal_successors", []),
                "temperature": spec.get("temperature", 0.3),
                "max_tokens": spec.get("max_tokens", 2048),
                "provider_pin": spec.get("provider_pin"),
            }
            for name, spec in cfg.items()
            if isinstance(spec, dict)
        }
    return {"skills": _SKILLS_CACHE}


@app.get("/api/health")
async def get_health() -> dict:
    gateway_up = False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            gateway_up = (await client.get(f"{GATEWAY_URL}/v1/routers")).status_code == 200
    except Exception:
        pass
    return {
        "ok": True,
        "gateway": {"url": GATEWAY_URL, "up": gateway_up},
        "active_runs": sorted(runner.tasks),
        "frontend_built": FRONTEND_DIST.exists(),
    }


# ── gateway proxy ────────────────────────────────────────────────────────────

@app.api_route("/api/gateway/{path:path}",
               methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_gateway(path: str, request: Request) -> Response:
    """Pass through to the gateway on :8109.

    The gateway installs no CORS middleware, so a browser on the Vite dev
    origin cannot call it directly. Proxying here keeps the gateway untouched
    and gives the frontend one origin for everything.
    """
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            upstream = await client.request(
                request.method,
                f"{GATEWAY_URL}/{path}",
                params=dict(request.query_params),
                content=body or None,
                headers={"content-type": request.headers.get("content-type",
                                                             "application/json")},
            )
    except httpx.RequestError as e:
        raise HTTPException(502, f"gateway unreachable at {GATEWAY_URL}: {e}") from e
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


# ── static frontend ──────────────────────────────────────────────────────────

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"),
              name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str) -> Response:
        """Serve the built frontend, falling back to index.html for routes.

        `/api/...` is excluded: this route is registered last, so it would
        otherwise answer an unmatched API path with a 200 and a page of HTML,
        turning a typo'd endpoint into a JSON parse error in the client
        instead of the 404 it is.
        """
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(404, f"no such endpoint: /{full_path}")
        candidate = FRONTEND_DIST / full_path
        # resolve() collapses any ".." before the containment check, so a
        # crafted path cannot climb out of the build directory.
        if full_path:
            try:
                resolved = candidate.resolve()
                if resolved.is_file() and resolved.is_relative_to(
                    FRONTEND_DIST.resolve()
                ):
                    return FileResponse(resolved)
            except OSError:
                pass
        return FileResponse(FRONTEND_DIST / "index.html")
else:
    @app.get("/")
    async def no_frontend() -> JSONResponse:
        return JSONResponse({
            "error": "frontend not built",
            "fix": "cd frontend && pnpm install && pnpm build",
            "note": f"expected a build at {FRONTEND_DIST}",
            "api": "/api/health",
        }, status_code=503)


def main() -> None:
    print(f"[api] GraphPilot console on http://localhost:{PORT}")
    if not FRONTEND_DIST.exists():
        print(f"[api] no frontend build at {FRONTEND_DIST} — "
              f"run `cd frontend && pnpm build`, or `pnpm dev` for the dev server")
    # Started eagerly so the gateway's 45s cold start is paid here, at boot,
    # rather than on the user's first query.
    try:
        ensure_gateway()
    except Exception as e:
        print(f"[api] gateway not available ({e}); runs will fail until it is up")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
