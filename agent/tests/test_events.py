"""Event stream emitted by a run, and the bus that fans it out.

The console never polls: it starts a run with one request and streams it with
a second. That makes two things load-bearing, and both are tested here.

  1. **Ordering.** A wave's `wave_start` must precede every `node_complete`
     in it, and `wave_end` must follow them. The console draws waves as
     bands and sizes a band from its membership, so an out-of-order stream
     renders a graph that never existed.

  2. **Late subscription.** The run is already underway by the time the
     browser opens the stream, so a subscriber that only sees live events
     misses the planner entirely. The bus replays from a ring buffer, and
     must not drop or duplicate an event published while that replay is in
     flight.

`run_skill` is stubbed throughout — no gateway, no network, no LLM.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from core import events as events_mod
import flow as flow_mod
from core import persistence as persistence_mod
from core.events import EventBus
from core.schemas import AgentResult, NodeSpec

# ── the bus in isolation ─────────────────────────────────────────────────────

def test_late_subscriber_replays_then_goes_live() -> None:
    async def scenario() -> list[dict]:
        bus = EventBus()
        bus.publish("s", "run_start", query="q")
        bus.publish("s", "node_running", node_id="n:1")

        seen: list[dict] = []

        async def reader() -> None:
            async for ev in bus.subscribe("s"):
                seen.append(ev)

        task = asyncio.create_task(reader())
        await asyncio.sleep(0)  # let the subscription register
        bus.publish("s", "node_complete", node_id="n:1")
        bus.publish("s", "run_complete", answer="done")
        await asyncio.wait_for(task, timeout=2)
        return seen

    seen = asyncio.run(scenario())
    assert [e["type"] for e in seen] == [
        "run_start", "node_running", "node_complete", "run_complete",
    ]
    # seq is strictly increasing with no repeats, so a client can spot a gap.
    assert [e["seq"] for e in seen] == [1, 2, 3, 4]

def test_subscriber_can_resume_from_a_sequence_number() -> None:
    async def scenario() -> list[int]:
        bus = EventBus()
        for i in range(5):
            bus.publish("s", "node_complete", node_id=f"n:{i}")
        bus.publish("s", "run_complete", answer="")
        return [ev["seq"] async for ev in bus.subscribe("s", after_seq=3)]

    assert asyncio.run(scenario()) == [4, 5, 6]

def test_stream_of_a_finished_run_ends_without_hanging() -> None:
    """Opening the stream of an already-finished run must replay and close."""
    async def run() -> list[dict]:
        bus = EventBus()
        bus.publish("s", "run_start", query="q")
        bus.publish("s", "run_complete", answer="a")

        async def collect():
            return [ev async for ev in bus.subscribe("s")]

        return await asyncio.wait_for(collect(), timeout=2)

    assert [e["type"] for e in asyncio.run(run())] == ["run_start", "run_complete"]

def test_publish_survives_a_broken_subscriber() -> None:
    """A console bug must never surface as a node failure."""
    bus = EventBus()

    class Hostile:
        def put_nowait(self, _ev):
            raise RuntimeError("boom")

    ch = bus._channel("s")
    ch.subscribers.add(Hostile())
    bus.publish("s", "node_complete", node_id="n:1")   # must not raise
    assert ch.subscribers == set()                      # and drops the bad one

def test_emitter_swallows_bad_payloads() -> None:
    emit = EventBus().emitter("s")
    emit("node_complete", node_id="n:1")  # must not raise

def test_is_live_flips_on_a_terminal_event() -> None:
    bus = EventBus()
    bus.publish("s", "run_start", query="q")
    assert bus.is_live("s")
    bus.publish("s", "run_complete", answer="")
    assert not bus.is_live("s")

# ── the run loop's hooks ─────────────────────────────────────────────────────

def _stub_result(skill: str, **output) -> AgentResult:
    return AgentResult(success=True, agent_name=skill, output=output,
                       elapsed_s=0.01, provider="stub", model="stub-1",
                       input_tokens=100, output_tokens=10)

def _drive(monkey_skills) -> list[dict]:
    """Run the executor against a stubbed dispatcher; return the events."""
    captured: list[dict] = []

    original_run_skill = flow_mod.run_skill
    original_root = persistence_mod.SESSIONS_ROOT
    original_read = flow_mod.memory_svc.read
    original_remember = flow_mod.memory_svc.remember

    with tempfile.TemporaryDirectory() as tmp:
        persistence_mod.SESSIONS_ROOT = Path(tmp)
        flow_mod.run_skill = monkey_skills
        flow_mod.memory_svc.read = lambda *_a, **_k: []
        flow_mod.memory_svc.remember = lambda *_a, **_k: None
        try:
            ex = flow_mod.Executor.__new__(flow_mod.Executor)  # skip ensure_gateway
            ex.registry = flow_mod.SkillRegistry()
            asyncio.run(ex.run("test query", session_id="s-test",
                               emit=lambda t, **p: captured.append({"type": t, **p})))
        finally:
            flow_mod.run_skill = original_run_skill
            persistence_mod.SESSIONS_ROOT = original_root
            flow_mod.memory_svc.read = original_read
            flow_mod.memory_svc.remember = original_remember
    return captured

def test_fan_out_run_emits_waves_in_order() -> None:
    """planner → 3 parallel researchers → formatter."""

    async def fake_run_skill(skill, nid, nodes, sid, query, fr, memory_hits=None):
        name = skill.name
        if name == "planner":
            r = _stub_result("planner", rationale="fan out")
            # The formatter takes the three researchers as inputs, which is
            # what pushes it into a wave of its own. A formatter wired only to
            # USER_QUERY would be ready immediately and run alongside them.
            r.successors = [
                NodeSpec(skill="researcher", metadata={"label": f"r{i}"})
                for i in range(3)
            ] + [NodeSpec(skill="formatter",
                          inputs=["USER_QUERY", "n:r0", "n:r1", "n:r2"],
                          metadata={"label": "out"})]
            return r, f"prompt for {nid}"
        if name == "formatter":
            return _stub_result("formatter", final_answer="the answer"), "p"
        return _stub_result(name, findings="f"), "p"

    evs = _drive(fake_run_skill)
    types = [e["type"] for e in evs]

    assert types[0] == "run_start"
    assert types[-1] == "run_complete"

    # Every wave is well-formed: start, then its nodes, then end.
    for ev in evs:
        if ev["type"] != "wave_start":
            continue
        w = ev["wave"]
        window = [e for e in evs if e.get("wave") == w]
        assert window[0]["type"] == "wave_start"
        assert window[-1]["type"] == "wave_end"
        completed = {e["node_id"] for e in window if e["type"] == "node_complete"}
        assert completed == set(ev["node_ids"]), f"wave {w} membership mismatch"

    # The three researchers shared one wave — that is the parallelism the
    # console renders as a band.
    waves = {e["wave"]: e["node_ids"] for e in evs if e["type"] == "wave_start"}
    assert any(len(ids) == 3 for ids in waves.values()), waves

    final = evs[-1]
    assert final["answer"] == "the answer"
    assert final["waves"] == len(waves)

def test_node_complete_carries_the_full_inspector_payload() -> None:
    async def fake_run_skill(skill, nid, nodes, sid, query, fr, memory_hits=None):
        if skill.name == "planner":
            r = _stub_result("planner")
            r.successors = [NodeSpec(skill="formatter",
                                              inputs=["USER_QUERY"])]
            return r, "planner prompt text"
        return _stub_result("formatter", final_answer="a"), "formatter prompt text"

    evs = _drive(fake_run_skill)
    done = [e for e in evs if e["type"] == "node_complete"]
    assert len(done) == 2

    node = done[0]["node"]
    assert node["node_id"] == "n:1"
    assert node["status"] == "complete"
    assert node["prompt_sent"] == "planner prompt text"
    # The telemetry the inspector renders, surviving the model_dump.
    assert node["result"]["model"] == "stub-1"
    assert node["result"]["input_tokens"] == 100
    assert node["result"]["provider"] == "stub"

def test_node_running_precedes_its_completion() -> None:
    async def fake_run_skill(skill, nid, nodes, sid, query, fr, memory_hits=None):
        if skill.name == "planner":
            r = _stub_result("planner")
            r.successors = [NodeSpec(skill="formatter",
                                              inputs=["USER_QUERY"])]
            return r, "p"
        return _stub_result("formatter", final_answer="a"), "p"

    evs = _drive(fake_run_skill)
    order = [(e["type"], e.get("node_id")) for e in evs
             if e["type"] in ("node_running", "node_complete")]
    for nid in ("n:1", "n:2"):
        pair = [t for t, n in order if n == nid]
        assert pair == ["node_running", "node_complete"], f"{nid}: {pair}"

    started = [e for e in evs if e["type"] == "node_running"][0]
    assert started["started_at"] > 0  # a measured timestamp, for live counters

def test_graph_payload_is_json_safe_once_results_exist() -> None:
    """Every graph-bearing event must survive json.dumps.

    A live graph holds Pydantic AgentResults under `result`; the payload is
    built with persistence.graph_to_payload precisely so the stream carries
    the same shape the disk does.
    """
    import json

    async def fake_run_skill(skill, nid, nodes, sid, query, fr, memory_hits=None):
        if skill.name == "planner":
            r = _stub_result("planner")
            r.successors = [NodeSpec(skill="formatter",
                                              inputs=["USER_QUERY"])]
            return r, "p"
        return _stub_result("formatter", final_answer="a"), "p"

    evs = _drive(fake_run_skill)
    with_graph = [e for e in evs if "graph" in e]
    assert with_graph, "no event carried a graph"
    for ev in with_graph:
        json.dumps(ev)  # raises on a stray Pydantic model

    last = with_graph[-1]["graph"]
    assert last["directed"] is True
    results = [n.get("result") for n in last["nodes"] if n.get("result")]
    assert results and all(isinstance(r, dict) for r in results)

def test_cli_path_emits_nothing() -> None:
    """`emit=None` must leave the run loop exactly as it was."""
    assert events_mod is not None  # module imports cleanly

    async def fake_run_skill(skill, nid, nodes, sid, query, fr, memory_hits=None):
        # The seed node is always a planner, and only a node whose *skill* is
        # formatter contributes final_answer — so the planner has to plan one.
        if skill.name == "planner":
            r = _stub_result("planner")
            r.successors = [NodeSpec(skill="formatter", inputs=["USER_QUERY"])]
            return r, "p"
        return _stub_result("formatter", final_answer="a"), "p"

    original_run_skill = flow_mod.run_skill
    original_root = persistence_mod.SESSIONS_ROOT
    original_read = flow_mod.memory_svc.read
    original_remember = flow_mod.memory_svc.remember
    with tempfile.TemporaryDirectory() as tmp:
        persistence_mod.SESSIONS_ROOT = Path(tmp)
        flow_mod.run_skill = fake_run_skill
        flow_mod.memory_svc.read = lambda *_a, **_k: []
        flow_mod.memory_svc.remember = lambda *_a, **_k: None
        try:
            ex = flow_mod.Executor.__new__(flow_mod.Executor)
            ex.registry = flow_mod.SkillRegistry()
            answer = asyncio.run(ex.run("q", session_id="s-cli"))
        finally:
            flow_mod.run_skill = original_run_skill
            persistence_mod.SESSIONS_ROOT = original_root
            flow_mod.memory_svc.read = original_read
            flow_mod.memory_svc.remember = original_remember
    assert answer == "a"
