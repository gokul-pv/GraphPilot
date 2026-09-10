"""Deterministic test for the critic auto-insertion fix.

In S8 (and S9 before this fix), the critic auto-insertion block only
fired when a `critic: true` skill (today: distiller) emitted NEW dynamic
successors during `extend_from`. If the Planner had already pre-wired
the full chain upfront — planner → researcher → distiller → formatter —
then distiller spawned nothing dynamically, `added` was empty, the
auto-insertion was skipped, and the `critic: true` flag was effectively
a no-op for the entire run.

The fix reads the graph's actual outgoing edges from the completing
critic-tagged node and gates each non-critic child with a Critic, so
both shapes (dynamic-spawn and pre-planned) are covered.

These tests drive `Graph.extend_from` directly with a synthetic
AgentResult — no LLM call — so the orchestrator wiring is verified
deterministically.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow import Graph
from schemas import AgentResult
from skills import SkillRegistry


def _ok_result(skill: str) -> AgentResult:
    """Synthetic successful result with no dynamic successors."""
    return AgentResult(success=True, agent_name=skill,
                       output={"ok": True}, elapsed_s=0.1)


def test_critic_spliced_on_pre_planned_distiller_to_formatter():
    """The student-reported case: Planner pre-wires distiller → formatter,
    distiller is `critic: true`. After distiller completes, the orchestrator
    must splice a Critic between distiller and formatter."""
    registry = SkillRegistry()
    g = Graph()

    # Mimic what the Planner emitted: a researcher feeding a distiller
    # feeding a formatter, all wired upfront.
    researcher = g.add_node("researcher", inputs=["USER_QUERY"])
    distiller = g.add_node("distiller", inputs=[researcher])
    formatter = g.add_node("formatter", inputs=["USER_QUERY", distiller])

    # Sanity check the baseline edges.
    assert g.g.has_edge(researcher, distiller)
    assert g.g.has_edge(distiller, formatter)

    # Distiller completes; the executor calls extend_from. The result has
    # no dynamic successors — this is the case that pre-fix silently
    # bypassed the auto-critic.
    added = g.extend_from(distiller, _ok_result("distiller"), registry=registry)

    # After the fix: a Critic must sit between distiller and formatter.
    assert not g.g.has_edge(distiller, formatter), \
        "distiller → formatter edge should have been removed (re-routed via critic)"
    critic_nodes = [n for n in added if g.g.nodes[n]["skill"] == "critic"]
    assert len(critic_nodes) == 1, \
        f"expected exactly one critic to be auto-inserted, got {critic_nodes}"
    critic_nid = critic_nodes[0]
    assert g.g.has_edge(distiller, critic_nid), \
        "distiller → critic edge missing"
    assert g.g.has_edge(critic_nid, formatter), \
        "critic → formatter edge missing"
    md = g.g.nodes[critic_nid]["metadata"]
    assert md["target"] == distiller and md["child"] == formatter, \
        f"critic metadata wrong: {md}"
    # Critic must see USER_QUERY (otherwise it falls back to MEMORY HITS,
    # which can contain stale facts from prior sessions and mislead it
    # about what the user actually asked).
    assert "USER_QUERY" in g.g.nodes[critic_nid]["inputs"], \
        f"auto-inserted critic missing USER_QUERY in inputs: {g.g.nodes[critic_nid]['inputs']}"
    assert distiller in g.g.nodes[critic_nid]["inputs"], \
        f"auto-inserted critic missing target in inputs: {g.g.nodes[critic_nid]['inputs']}"


def test_critic_skipped_when_child_is_already_a_critic():
    """If the Planner emitted a Critic explicitly between distiller and
    its consumer, the auto-insertion must NOT add a second one."""
    registry = SkillRegistry()
    g = Graph()
    distiller = g.add_node("distiller", inputs=["USER_QUERY"])
    user_critic = g.add_node("critic", inputs=[distiller],
                             metadata={"target": distiller, "child": "<tbd>"})
    formatter = g.add_node("formatter", inputs=[user_critic])

    pre = sum(1 for _, d in g.g.nodes(data=True) if d["skill"] == "critic")
    g.extend_from(distiller, _ok_result("distiller"), registry=registry)
    post = sum(1 for _, d in g.g.nodes(data=True) if d["skill"] == "critic")
    assert post == pre, \
        f"auto-insertion ran when downstream was already a critic; before={pre} after={post}"
    # Original edges remain intact.
    assert g.g.has_edge(distiller, user_critic)
    assert g.g.has_edge(user_critic, formatter)


def test_critic_spliced_on_each_outgoing_edge():
    """If a critic-tagged node has multiple outgoing edges (one to a
    distiller fan-in, one to a summariser fan-in, etc.), each one gets
    its own critic. The flag means always-gate, not gate-once."""
    registry = SkillRegistry()
    g = Graph()
    distiller = g.add_node("distiller", inputs=["USER_QUERY"])
    fmt_a = g.add_node("formatter", inputs=[distiller])
    fmt_b = g.add_node("formatter", inputs=[distiller])

    added = g.extend_from(distiller, _ok_result("distiller"), registry=registry)
    critic_nodes = [n for n in added if g.g.nodes[n]["skill"] == "critic"]
    assert len(critic_nodes) == 2, \
        f"expected one critic per outgoing edge (2), got {critic_nodes}"
    assert not g.g.has_edge(distiller, fmt_a)
    assert not g.g.has_edge(distiller, fmt_b)
    # Each formatter now sits behind its own critic.
    children = {g.g.nodes[c]["metadata"]["child"] for c in critic_nodes}
    assert children == {fmt_a, fmt_b}, \
        f"expected critics targeting both formatters, got children={children}"


def test_non_critic_skill_does_not_trigger_auto_insertion():
    """Regression guard: only `critic: true` skills should trigger the
    auto-insertion. A vanilla researcher → distiller edge stays put."""
    registry = SkillRegistry()
    g = Graph()
    researcher = g.add_node("researcher", inputs=["USER_QUERY"])
    distiller = g.add_node("distiller", inputs=[researcher])

    g.extend_from(researcher, _ok_result("researcher"), registry=registry)
    # No critic should have been spliced.
    assert g.g.has_edge(researcher, distiller), \
        "researcher → distiller edge should be untouched"
    critics = [n for n, d in g.g.nodes(data=True) if d["skill"] == "critic"]
    assert critics == [], f"unexpected critic auto-insertion: {critics}"
