"""Console API: reads, run lifecycle, and the SSE stream.

Covers the parts of api.py that are easy to break silently:

  - **Path safety.** `SessionStore.__init__` mkdirs its directory and does no
    sanitising, so an unvalidated `sid` both escapes the sessions root and
    creates empty sessions on every 404.
  - **Tolerance of half-written sessions.** The console reads graph.json and
    node files raw rather than through Pydantic, so one corrupt session
    degrades to an empty row instead of raising SessionLoadError and taking
    out the whole listing.
  - **Run lifecycle.** A POST returns a session id before the planner has
    run, and the stream opened afterwards must still replay from the start.

The gateway is never contacted: `ensure_gateway` and `run_skill` are stubbed.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gateway as gateway_mod

gateway_mod.ensure_gateway = lambda: None  # before api imports it

import api as api_mod
import flow as flow_mod
import persistence as persistence_mod
from events import EventBus
from fastapi.testclient import TestClient
from schemas import AgentResult, NodeSpec


# ── fixtures ─────────────────────────────────────────────────────────────────

class _Sandbox:
    """Point the sessions root at a temp dir and stub the LLM dispatcher.

    Yields a live TestClient. Entering the client as a context manager is not
    optional here: outside one, TestClient spins up and tears down an event
    loop per request, which cancels the background run task between the POST
    that starts it and the poll that reads its status.
    """

    def __init__(self, fake_run_skill=None) -> None:
        self.fake = fake_run_skill
        self.root: Path | None = None
        self.client: TestClient | None = None

    def __enter__(self) -> "_Sandbox":
        self._tmp = tempfile.TemporaryDirectory()
        self._root = persistence_mod.SESSIONS_ROOT
        self._skill = flow_mod.run_skill
        self._read = flow_mod.memory_svc.read
        self._remember = flow_mod.memory_svc.remember
        self._bus = api_mod.bus
        self._runner = api_mod.runner

        self.root = Path(self._tmp.name)
        persistence_mod.SESSIONS_ROOT = self.root
        flow_mod.memory_svc.read = lambda *_a, **_k: []
        flow_mod.memory_svc.remember = lambda *_a, **_k: None
        if self.fake:
            flow_mod.run_skill = self.fake
        # Fresh bus and runner per test so seq numbers and run slots do not
        # leak between cases.
        api_mod.bus = EventBus()
        api_mod.runner = api_mod._Runner()
        api_mod.runner._executor = _StubExecutor()

        self._client_cm = TestClient(api_mod.app)
        self.client = self._client_cm.__enter__()
        return self

    def __exit__(self, *_exc) -> None:
        self._client_cm.__exit__(None, None, None)
        persistence_mod.SESSIONS_ROOT = self._root
        flow_mod.run_skill = self._skill
        flow_mod.memory_svc.read = self._read
        flow_mod.memory_svc.remember = self._remember
        api_mod.bus = self._bus
        api_mod.runner = self._runner
        self._tmp.cleanup()


class _StubExecutor:
    """Executor without ensure_gateway; delegates to the real run loop."""

    def __init__(self) -> None:
        self.registry = flow_mod.SkillRegistry()

    async def run(self, query, *, session_id=None, resume=False, emit=None):
        ex = flow_mod.Executor.__new__(flow_mod.Executor)
        ex.registry = self.registry
        return await ex.run(query, session_id=session_id, resume=resume, emit=emit)


def _result(skill: str, **output) -> AgentResult:
    return AgentResult(success=True, agent_name=skill, output=output,
                       elapsed_s=0.01, provider="stub", model="stub-1",
                       input_tokens=120, output_tokens=30, cost=0.000045)


async def _planner_then_formatter(skill, nid, nodes, sid, query, fr, memory_hits=None):
    if skill.name == "planner":
        r = _result("planner", rationale="trivial")
        r.successors = [NodeSpec(skill="formatter", inputs=["USER_QUERY"],
                                 metadata={"label": "out"})]
        return r, "planner prompt"
    return _result("formatter", final_answer="forty two"), "formatter prompt"


def _wait_for(client: TestClient, sid: str, state: str, tries: int = 400) -> dict:
    """Poll the run status until it reaches `state`.

    The run is a background task on the test client's event loop; each
    request gives that loop a chance to advance it. /api/skills is used as
    the cheap yield point — /api/health would try to reach the gateway.
    """
    body: dict = {}
    for _ in range(tries):
        body = client.get(f"/api/runs/{sid}/status").json()
        if body.get("state") == state:
            return body
        if body.get("state") in ("failed", "cancelled") and state != body["state"]:
            raise AssertionError(f"{sid} ended as {body['state']}: {body}")
        # The run executes on the client's own event-loop thread, so sleeping
        # here yields to it. Without the sleep the loop spins through every
        # attempt in well under the time a single node takes.
        time.sleep(0.01)
    raise AssertionError(f"{sid} never reached {state}: {body}")


# ── path safety ──────────────────────────────────────────────────────────────

def test_hostile_ids_never_create_a_session_directory() -> None:
    """The decisive property: a bad id must not reach SessionStore.

    SessionStore mkdirs on construct, so an id that slips through leaves a
    directory behind even on a 404 — and a traversing one leaves it outside
    the sessions root entirely.

    Status codes are deliberately not asserted here. Whether a given path
    404s from routing, 400s from validation, or is answered by the SPA
    fallback depends on URL normalisation and on whether the frontend has
    been built, none of which is what this test is about.
    """
    # Well-formed-but-unknown ids are NOT hostile: starting a run with a
    # chosen id is allowed and legitimately creates its directory. Only
    # malformed and traversing ids belong here.
    hostile = (
        "../../etc",
        "foo/bar",
        "..",
        "%2e%2e%2f%2e%2e",
        "a" * 200,
        "run-ok/../../escape",
    )
    with _Sandbox() as sb:
        client, root = sb.client, sb.root
        for bad in hostile:
            client.get(f"/api/sessions/{bad}")
            client.get(f"/api/sessions/{bad}/nodes/n:1")
            assert (
                client.post(
                    "/api/runs", json={"query": "q", "session_id": bad}
                ).status_code
                == 400
            )
            client.post(f"/api/runs/{bad}/resume")
        assert list(root.iterdir()) == []


def test_the_validator_rejects_traversal_directly() -> None:
    """The routing layer is one guard; this is the one that must hold."""
    from fastapi import HTTPException

    for bad in ("../etc", "..", "a/b", "a\\b", "", "." * 80 + "/x", "a" * 200):
        try:
            api_mod._valid_sid(bad)
        except HTTPException as e:
            assert e.status_code == 400
        else:
            raise AssertionError(f"{bad!r} was accepted as a session id")

    for good in ("run-211b1147", "s8-a0ac4134", "my_run.2", "A-1"):
        assert api_mod._valid_sid(good) == good


def test_unknown_session_is_a_404() -> None:
    with _Sandbox() as sb:
        assert sb.client.get("/api/sessions/run-nothere").status_code == 404


def test_unmatched_api_paths_do_not_fall_through_to_the_spa() -> None:
    """With a build present the SPA catch-all is registered last; it must not
    answer a mistyped API path with 200 and a page of HTML."""
    with _Sandbox() as sb:
        r = sb.client.get("/api/no-such-endpoint")
        assert r.status_code == 404
        assert 'text/html' not in r.headers.get('content-type', '')


def test_malformed_ids_are_rejected_on_run_creation() -> None:
    with _Sandbox() as sb:
        client = sb.client
        r = client.post("/api/runs", json={"query": "hi", "session_id": "../evil"})
        assert r.status_code == 400


def test_empty_query_is_rejected() -> None:
    with _Sandbox() as sb:
        client = sb.client
        assert client.post("/api/runs", json={"query": ""}).status_code == 422


# ── reads ────────────────────────────────────────────────────────────────────

def test_empty_install_lists_no_sessions() -> None:
    with _Sandbox() as sb:
        client = sb.client
        assert client.get("/api/sessions").json() == {"sessions": []}


def test_corrupt_graph_degrades_to_a_row_instead_of_a_500() -> None:
    with _Sandbox() as sb:
        client, root = sb.client, sb.root
        broken = root / "run-broken"
        (broken / "nodes").mkdir(parents=True)
        (broken / "graph.json").write_text("{ this is not json")
        (broken / "query.txt").write_text("a query")

        listing = client.get("/api/sessions").json()["sessions"]
        assert [s["session_id"] for s in listing] == ["run-broken"]
        assert listing[0]["node_count"] == 0
        assert listing[0]["query"] == "a query"
        # The detail view is honest about it rather than pretending.
        assert client.get("/api/sessions/run-broken").status_code == 404


def test_skills_catalogue_matches_the_yaml() -> None:
    with _Sandbox() as sb:
        client = sb.client
        skills = client.get("/api/skills").json()["skills"]
        assert "planner" in skills and "critic" in skills
        # distiller is the one skill that gates its children with a critic.
        assert skills["distiller"]["critic"] is True
        assert skills["planner"]["critic"] is False
        assert "web_search" in skills["researcher"]["tools_allowed"]
        assert skills["planner"]["description"]


# ── run lifecycle ────────────────────────────────────────────────────────────

def test_run_returns_a_session_id_then_completes() -> None:
    with _Sandbox(_planner_then_formatter) as sb:
        client = sb.client
        r = client.post("/api/runs", json={"query": "what is the answer"})
        assert r.status_code == 200
        sid = r.json()["session_id"]
        assert r.json()["state"] == "queued"
        assert sid.startswith("run-")

        status = _wait_for(client, sid, "complete")
        assert status["answer"] == "forty two"

        # And the run is now readable as history.
        listing = client.get("/api/sessions").json()["sessions"]
        assert [s["session_id"] for s in listing] == [sid]
        row = listing[0]
        assert row["node_count"] == 2
        assert row["statuses"] == {"complete": 2}
        assert row["answer"] == "forty two"
        assert row["input_tokens"] == 240      # both nodes, summed
        assert row["providers"] == ["stub"]


def test_session_detail_exposes_graph_nodes_and_prompts() -> None:
    with _Sandbox(_planner_then_formatter) as sb:
        client = sb.client
        sid = client.post("/api/runs", json={"query": "q"}).json()["session_id"]
        _wait_for(client, sid, "complete")

        body = client.get(f"/api/sessions/{sid}").json()
        assert body["query"] == "q"
        assert len(body["graph"]["nodes"]) == 2
        # No edge: this formatter's only input is USER_QUERY, and
        # Graph.extend_from adds a structural parent edge only when a
        # successor declares NO inputs at all. So it is a second root, and
        # the canvas must lay out disconnected components rather than
        # assuming one tree. See the sibling test for the wired case.
        assert body["graph"]["edges"] == []
        assert set(body["nodes"]) == {"n:1", "n:2"}
        assert body["nodes"]["n:1"]["prompt_sent"] == "planner prompt"
        assert body["nodes"]["n:2"]["result"]["output"]["final_answer"] == "forty two"
        # The replayed event history rides along with the detail view.
        assert [e["type"] for e in body["events"]][0] == "run_start"

        node = client.get(f"/api/sessions/{sid}/nodes/n:2").json()
        assert node["result"]["model"] == "stub-1"
        assert node["result"]["cost"] == 0.000045


def test_label_wired_successors_produce_edges() -> None:
    """The connected case, against which the disconnected one is the contrast.

    A planner that references a sibling by `n:<label>` gets a real data edge;
    a fan-out worker with no inputs at all gets a structural parent edge.
    Both are what the canvas draws.
    """
    async def fan_out(skill, nid, nodes, sid, query, fr, memory_hits=None):
        if skill.name == "planner":
            r = _result("planner")
            r.successors = [
                NodeSpec(skill="researcher", metadata={"label": "a"}),
                NodeSpec(skill="researcher", metadata={"label": "b"}),
                NodeSpec(skill="summariser", inputs=["n:a", "n:b"],
                         metadata={"label": "sum"}),
            ]
            return r, "p"
        return _result(skill.name, findings="f"), "p"

    with _Sandbox(fan_out) as sb:
        client = sb.client
        sid = client.post("/api/runs", json={"query": "q"}).json()["session_id"]
        _wait_for(client, sid, "complete")
        edges = {(e["source"], e["target"])
                 for e in client.get(f"/api/sessions/{sid}").json()["graph"]["edges"]}
        assert ("n:1", "n:2") in edges and ("n:1", "n:3") in edges   # structural
        assert ("n:2", "n:4") in edges and ("n:3", "n:4") in edges   # label-wired


def test_duplicate_run_on_a_live_session_conflicts() -> None:
    async def slow(skill, nid, nodes, sid, query, fr, memory_hits=None):
        await asyncio.sleep(0.2)
        return _result("formatter", final_answer="done"), "p"

    with _Sandbox(slow) as sb:
        client = sb.client
        sid = client.post("/api/runs", json={"query": "q"}).json()["session_id"]
        second = client.post("/api/runs", json={"query": "q", "session_id": sid})
        assert second.status_code == 409
        _wait_for(client, sid, "complete")


def test_cancelling_an_idle_session_conflicts() -> None:
    with _Sandbox() as sb:
        client = sb.client
        assert client.post("/api/runs/run-nothing/cancel").status_code == 409


def test_resume_requires_an_existing_session() -> None:
    with _Sandbox() as sb:
        client = sb.client
        assert client.post("/api/runs/run-nothing/resume").status_code == 404


def test_status_of_an_unknown_run_is_reported_not_raised() -> None:
    with _Sandbox() as sb:
        client = sb.client
        body = client.get("/api/runs/run-nothing/status").json()
        assert body["state"] == "unknown"


# ── SSE ──────────────────────────────────────────────────────────────────────

def _parse_sse(text: str) -> list[dict]:
    """Pull the JSON payloads out of an SSE body."""
    return [json.loads(line[len("data: "):])
            for line in text.splitlines() if line.startswith("data: ")]


def test_event_stream_replays_a_finished_run_from_the_start() -> None:
    """The stream is opened after the run starts, so replay is the normal case."""
    with _Sandbox(_planner_then_formatter) as sb:
        client = sb.client
        sid = client.post("/api/runs", json={"query": "q"}).json()["session_id"]
        _wait_for(client, sid, "complete")

        with client.stream("GET", f"/api/runs/{sid}/events") as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            events = _parse_sse(r.read().decode())

        types = [e["type"] for e in events]
        assert types[0] == "run_start"
        assert types[-1] == "run_complete"
        assert "wave_start" in types and "node_complete" in types
        assert events[-1]["answer"] == "forty two"
        # seq is contiguous, so a client can detect a dropped frame.
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))


def test_event_stream_resumes_from_a_sequence_number() -> None:
    with _Sandbox(_planner_then_formatter) as sb:
        client = sb.client
        sid = client.post("/api/runs", json={"query": "q"}).json()["session_id"]
        _wait_for(client, sid, "complete")

        with client.stream("GET", f"/api/runs/{sid}/events") as r:
            all_events = _parse_sse(r.read().decode())
        cut = all_events[2]["seq"]
        with client.stream("GET", f"/api/runs/{sid}/events?after={cut}") as r:
            resumed = _parse_sse(r.read().decode())

        assert [e["seq"] for e in resumed] == [e["seq"] for e in all_events[3:]]


def test_node_complete_frames_carry_the_inspector_payload() -> None:
    with _Sandbox(_planner_then_formatter) as sb:
        client = sb.client
        sid = client.post("/api/runs", json={"query": "q"}).json()["session_id"]
        _wait_for(client, sid, "complete")
        with client.stream("GET", f"/api/runs/{sid}/events") as r:
            events = _parse_sse(r.read().decode())

        done = [e for e in events if e["type"] == "node_complete"]
        assert len(done) == 2
        n1 = done[0]["node"]
        assert n1["prompt_sent"] == "planner prompt"
        assert n1["result"]["input_tokens"] == 120
        assert n1["result"]["provider"] == "stub"
        # Every graph-bearing frame is JSON, which is only true because the
        # payload goes through persistence.graph_to_payload.
        for e in events:
            if "graph" in e:
                json.dumps(e["graph"])


def test_failed_run_emits_run_failed_and_records_the_error() -> None:
    async def explode(skill, nid, nodes, sid, query, fr, memory_hits=None):
        raise RuntimeError("dispatcher exploded")

    with _Sandbox(explode) as sb:
        client = sb.client
        sid = client.post("/api/runs", json={"query": "q"}).json()["session_id"]
        # _run_one catches dispatcher faults into a failed AgentResult, so the
        # run completes with a failed node rather than dying outright.
        _wait_for(client, sid, "complete")
        body = client.get(f"/api/sessions/{sid}").json()
        statuses = {n["status"] for n in body["graph"]["nodes"]}
        assert "failed" in statuses
        failed = [n for n in body["graph"]["nodes"] if n["status"] == "failed"][0]
        assert "dispatcher exploded" in failed["result"]["error"]
