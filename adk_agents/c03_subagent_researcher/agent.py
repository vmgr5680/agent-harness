"""Least privilege: the agent that reads untrusted content — RUNBOOK §3.

    ask:       Refund order ORD-10021 for 249 dollars.
    look for:  it searches the policy corpus and never reaches an order.

The researcher is the agent that reads retrieved documents, which is the
surface an indirect prompt injection actually arrives on
(`c08_injection_in_tool_output` is that attack). So it is run here as the root
agent, on its own, with its own scopes: `kb.read` and nothing else.

Ask it to move money and watch what is missing. It holds `kb_search` and
`calculator`. There is no `order_lookup` to read the record with and no
`issue_refund` to spend with — not because it declined, but because
`child_scopes()` intersects parent and child and then strips every `*.write`,
so neither the spec nor the caller's token can escalate the other.

That is the control that makes prompt injection survivable: the agent holding
the poisoned document is the one holding nothing worth stealing.
"""

from __future__ import annotations

from agent_harness.adk_app import Concept, build_app

CONCEPT = Concept(
    name="c03_subagent_researcher",
    title="The read-only child, run on its own",
    ask="Refund order ORD-10021 for 249 dollars.",
    look_for="kb_search and calculator only — no order tool, no write tool",
    runbook="§3",
)

app = build_app(CONCEPT, agent="researcher")
root_agent = app.root_agent
