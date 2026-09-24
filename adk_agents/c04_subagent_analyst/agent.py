"""Least privilege: the agent that holds no scopes at all — RUNBOOK §3.

    ask:       What is the status of order ORD-10021?
    then ask:  What is 249.00 * 0.15?
    look for:  the first question produces no tool call whatsoever.

The analyst does arithmetic on amounts another agent has already retrieved. It
holds no scopes, so `tools_for()` builds it exactly one tool — `calculator`,
which requires none.

The first question is the point: there is no `order_lookup` to reach for, so
the trace contains no `functionCall` event at all and the agent says plainly
that it could not retrieve anything. The second question shows it is not
broken, just narrow.

In `support` this agent is a callable tool, not a transfer target. Run it as a
root here so the empty tool list is the first thing you see.
"""

from __future__ import annotations

from agent_harness.adk_app import Concept, build_app

CONCEPT = Concept(
    name="c04_subagent_analyst",
    title="No scopes, so no tools to reach for",
    ask="What is the status of order ORD-10021?",
    look_for="no functionCall event at all; then ask it for 249.00 * 0.15",
    runbook="§3",
)

app = build_app(CONCEPT, agent="analyst")
root_agent = app.root_agent
