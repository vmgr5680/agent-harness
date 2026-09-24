"""Tool scopes — RUNBOOK §2.

    ask:       Refund order ORD-10021 for 249 dollars.
    look for:  no refusal, no approval prompt, and no `issue_refund` anywhere.

This app is `support` with the write scope withheld: the grant is
`{kb.read, orders.read}` instead of the spec's full set. `build_agent`
intersects the grant with the agent's own scopes before it constructs the tool
list, so `issue_refund` is not on the agent at all.

Compare the agent's tool list here with `c09_human_approval`, which is the same
agent with `orders.write` granted: `kb_search`, `order_lookup`, `calculator`
and the two subagents here, the same five plus `issue_refund` there.

The distinction worth internalising: a model that *declines* to call a
forbidden tool is being well-behaved. A tool that was never constructed is a
control. Only one of those still holds when the model changes.
"""

from __future__ import annotations

from agent_harness.adk_app import Concept, build_app

CONCEPT = Concept(
    name="c02_tool_scopes",
    title="A tool the caller may not use is a tool that does not exist",
    ask="Refund order ORD-10021 for 249 dollars.",
    look_for="the agent's tool list has no issue_refund, and nothing was denied",
    runbook="§2",
)

app = build_app(CONCEPT, scopes=frozenset({"kb.read", "orders.read"}))
root_agent = app.root_agent
