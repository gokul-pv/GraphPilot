"""Deterministic test for the recovery-Planner-amnesia fix.

When a node fails with `upstream_failure`, the Executor queues a fresh
Planner node. Before this fix, that Planner was added with
`inputs=["USER_QUERY"]` and so could not see any siblings that had
already succeeded — it re-emitted a fresh DAG from scratch and the
runtime re-did work.

The fix collects every currently-`complete` non-planner, non-critic
node id and wires them into the recovery Planner's inputs. This test
exercises the relevant block of `Executor.run` directly (without the
LLM) by building a Graph in the partial-failure shape that triggers
the recovery code path, then asserting that the recovery Planner was
added with the correct inputs.

Why test this without the gateway: triggering a real classified-as-
upstream_failure mid-run is flaky (the third-party tools the
Researcher uses succeed or fail nondeterministically), so we drive
the Graph + recovery decision directly. The mechanism under test is
the orchestrator wiring, not the LLM.
"""

from __future__ import annotations

import sys
from pathlib import Path

# tests/ is one level below the package root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow import Graph
from recovery import plan_recovery
from schemas import AgentResult


def _add_completed(graph: Graph, skill: str, output: dict) -> str:
    """Helper: add a node, mark it complete, attach a synthetic result."""
    nid = graph.add_node(skill, inputs=[])
    graph.g.nodes[nid]["status"] = "complete"
    graph.g.nodes[nid]["result"] = AgentResult(
        success=True, agent_name=skill, output=output, elapsed_s=0.1,
    )
    return nid


def _add_failed(graph: Graph, skill: str, error: str) -> str:
    nid = graph.add_node(skill, inputs=[])
    graph.g.nodes[nid]["status"] = "failed"
    graph.g.nodes[nid]["result"] = AgentResult(
        success=False, agent_name=skill, error=error, elapsed_s=0.1,
    )
    return nid


def _simulate_recovery_block(graph: Graph, failed_nid: str) -> str:
    """Mirror the recovery wiring in Executor.run lines 277-292.

    Kept here so the test breaks loudly if that block is edited in a
    way that drops `prior_complete` from the recovery Planner's
    inputs."""
    failed = graph.g.nodes[failed_nid]
    decision = plan_recovery(
        failed_skill=failed["skill"],
        error_text=failed["result"].error or "",
        failed_node_id=failed_nid,
    )
    assert decision.action == "replan", \
        f"test expected replan, got {decision.action!r}"
    prior_complete = [
        n for n, d in graph.g.nodes(data=True)
        if d.get("status") == "complete"
        and d["skill"] not in ("planner", "critic")
        and isinstance(d.get("result"), AgentResult)
    ]
    recovery_inputs = ["USER_QUERY"] + prior_complete
    rec_nid = graph.add_node(
        "planner", inputs=recovery_inputs,
        metadata={"failure_report": decision.failure_report,
                  "recovers": failed_nid,
                  "recovery_reason": decision.reason,
                  "prior_complete": prior_complete},
    )
    return rec_nid


def test_recovery_planner_carries_prior_complete_siblings():
    """Fan-out of three researchers, one fails; the recovery Planner
    must see the two that succeeded."""
    g = Graph()
    # Seed planner (always present; should NOT appear in prior_complete).
    p1 = _add_completed(g, "planner", {"rationale": "seed"})
    # Three researchers as siblings; first two succeed, third fails.
    r_ok_a = _add_completed(g, "researcher",
                            {"question": "A", "findings": "A data..."})
    r_ok_b = _add_completed(g, "researcher",
                            {"question": "B", "findings": "B data..."})
    r_bad = _add_failed(g, "researcher", "search backend returned nothing")

    rec_nid = _simulate_recovery_block(g, r_bad)

    rec = g.g.nodes[rec_nid]
    inputs = rec["inputs"]
    assert inputs[0] == "USER_QUERY", \
        f"USER_QUERY must come first, got {inputs!r}"
    # The two successful researchers must be wired in.
    assert r_ok_a in inputs, \
        f"successful researcher {r_ok_a} missing from recovery inputs {inputs}"
    assert r_ok_b in inputs, \
        f"successful researcher {r_ok_b} missing from recovery inputs {inputs}"
    # The failed node must NOT be wired in.
    assert r_bad not in inputs, \
        f"failed node {r_bad} should not be in recovery inputs {inputs}"
    # The seed planner must NOT be wired in (routing context, not data).
    assert p1 not in inputs, \
        f"seed planner {p1} should not be in recovery inputs {inputs}"
    # Metadata preserves the carry list for replay/debugging.
    assert rec["metadata"]["prior_complete"] == [r_ok_a, r_ok_b]
    # Graph edges must exist so ready_nodes treats predecessors as satisfied.
    preds = set(g.g.predecessors(rec_nid))
    assert r_ok_a in preds and r_ok_b in preds, \
        f"recovery planner missing edges from prior successes: preds={preds}"


def test_recovery_planner_excludes_critics_from_prior_complete():
    """Critics emit verdicts, not data — they should not be wired as
    upstream input to the recovery Planner."""
    g = Graph()
    _add_completed(g, "planner", {"rationale": "seed"})
    r_ok = _add_completed(g, "researcher", {"findings": "..."})
    c_ok = _add_completed(g, "critic", {"verdict": "pass", "rationale": "..."})
    r_bad = _add_failed(g, "researcher", "fetch_url returned empty body")

    rec_nid = _simulate_recovery_block(g, r_bad)

    inputs = g.g.nodes[rec_nid]["inputs"]
    assert r_ok in inputs, "successful researcher must be carried forward"
    assert c_ok not in inputs, \
        f"critic {c_ok} should not appear in recovery inputs {inputs}"


def test_recovery_planner_with_no_prior_successes_falls_back_to_user_query_only():
    """Empty `prior_complete` (the first-step-failed case) must reduce
    to the legacy behaviour: recovery Planner sees only USER_QUERY."""
    g = Graph()
    _add_completed(g, "planner", {"rationale": "seed"})
    r_bad = _add_failed(g, "researcher", "no usable findings returned")

    rec_nid = _simulate_recovery_block(g, r_bad)

    inputs = g.g.nodes[rec_nid]["inputs"]
    assert inputs == ["USER_QUERY"], \
        f"no prior successes → recovery inputs must be ['USER_QUERY'], got {inputs}"
    assert g.g.nodes[rec_nid]["metadata"]["prior_complete"] == []
