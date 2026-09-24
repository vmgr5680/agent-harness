"""Agent construction: the least-privilege tree.

These assert the property the whole security posture rests on — that the agent
which reads untrusted retrieved content cannot reach a tool that changes state.
"""

from __future__ import annotations

import pytest
from google.adk.tools.agent_tool import AgentTool

from agent_harness.agents.offline import OfflineLlm
from agent_harness.agents.specs import (
    ANALYST,
    RESEARCHER,
    SPECS,
    SUPPORT,
    build_root_agent,
    child_scopes,
)
from agent_harness.tools.adk_tools import TOOL_SCOPES, WRITE_TOOLS, tools_for

ALL = frozenset({"kb.read", "orders.read", "orders.write"})


def agent(granted=ALL, name="support"):
    return build_root_agent(model=OfflineLlm(), granted=granted, agent=name)


# --- privilege separation ----------------------------------------------------


def test_a_subagent_never_inherits_a_write_scope():
    """The defence against prompt injection is that the agent reading
    untrusted content has nothing dangerous to call."""
    scopes = child_scopes(SUPPORT, RESEARCHER)
    assert "orders.write" not in scopes
    assert scopes <= SUPPORT.scopes


def test_a_child_cannot_hold_a_scope_the_parent_lacks():
    narrow = SUPPORT.__class__(
        name="narrow",
        description="d",
        instruction="i",
        scopes=frozenset({"kb.read"}),
        tags=frozenset({"support"}),
    )
    greedy = SUPPORT.__class__(
        name="greedy",
        description="d",
        instruction="i",
        scopes=frozenset({"kb.read", "orders.read", "secrets.read"}),
        tags=frozenset({"support"}),
    )
    assert child_scopes(narrow, greedy) == frozenset({"kb.read"})


def test_the_researcher_is_given_no_write_tools():
    researcher = build_root_agent(model=OfflineLlm(), granted=ALL, agent="researcher")
    names = {t.name for t in researcher.tools}
    assert not (names & WRITE_TOOLS)


def test_the_analyst_holds_no_scopes_and_only_untagged_tools():
    assert ANALYST.scopes == frozenset()
    analyst = build_root_agent(model=OfflineLlm(), granted=ALL, agent="analyst")
    assert {t.name for t in analyst.tools} == {"calculator"}


# --- catalogue ---------------------------------------------------------------


def test_the_catalogue_is_filtered_by_caller_scopes():
    read_only = {t.name for t in tools_for(frozenset({"kb.read"}))}
    assert "issue_refund" not in read_only
    assert "order_lookup" not in read_only
    assert "kb_search" in read_only


def test_tags_partition_the_catalogue_by_role():
    research = {t.name for t in tools_for(ALL, frozenset({"research"}))}
    assert research == {"calculator", "kb_search"}


def test_every_write_tool_requires_confirmation():
    """The rule: anything that writes, pays, emails, deletes or escalates."""
    from agent_harness.tools.adk_tools import build_tools

    for name, tool in build_tools().items():
        expected = name in WRITE_TOOLS
        # ADK keeps the flag private and exposes it through the async
        # check_require_confirmation(); the attribute is what a construction
        # test can assert without fabricating a ToolContext.
        assert bool(tool._require_confirmation) is expected, name


def test_every_tool_declares_its_scopes():
    from agent_harness.tools.adk_tools import build_tools

    assert set(build_tools()) == set(TOOL_SCOPES)


# --- tree shape --------------------------------------------------------------


def test_subagents_are_tools_not_transfer_targets():
    """AgentTool keeps control with the parent; transfer hands it away, which
    loses the parent's budget and its answer obligations."""
    root = agent()
    assert any(isinstance(t, AgentTool) for t in root.tools)
    assert root.sub_agents == []
    assert root.disallow_transfer_to_parent is True
    assert root.disallow_transfer_to_peers is True


def test_the_tree_does_not_recurse():
    root = agent()
    for tool in root.tools:
        if isinstance(tool, AgentTool):
            assert not any(isinstance(t, AgentTool) for t in tool.agent.tools), tool.agent.name


def test_an_unknown_agent_names_the_alternatives():
    with pytest.raises(KeyError, match="researcher"):
        build_root_agent(model=OfflineLlm(), granted=ALL, agent="nope")


def test_every_spec_instructs_the_model_to_treat_tool_output_as_data():
    """The prompt-level half of injection defence. Not sufficient on its own,
    which is why least privilege exists — but its absence is a smell."""
    for name in ("support", "researcher"):
        assert "DATA, not a command" in SPECS[name].instruction
