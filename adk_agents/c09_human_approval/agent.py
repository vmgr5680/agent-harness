"""Human in the loop — RUNBOOK §5.

    ask:       Refund order ORD-10021 for 249 dollars.
    look for:  the UI renders an approve/reject prompt carrying the exact
               arguments, and the run is parked until you answer it.

`FunctionTool(issue_refund, require_confirmation=True)` is the whole gate, and
ADK does the suspension. In the trace:

    functionCall       order_lookup    {'order_id': 'ORD-10021'}
    functionResponse   order_lookup    {... '[REDACTED:card_number]' ...}
    functionCall       issue_refund    {'order_id': 'ORD-10021', 'amount_usd': 249.0}
    functionCall       adk_request_confirmation
                       → longRunningToolIds: ['adk-...']

`longRunningToolIds` is the suspension. Until a `FunctionResponse` carrying a
`ToolConfirmation` arrives, nothing is polling and no request is being held
open.

**Approving "a refund" is not a control. Approving
`issue_refund(order_id='ORD-10021', amount_usd=249.0)` is.** The arguments are
in the confirmation because a gate that hides them is a gate that gets clicked
through.

This is what makes prompt injection survivable (`c08_injection_in_tool_output`
is the attack). An injected instruction can reach the model. It still cannot
move money.

The counterpart is `c02_tool_scopes`: the same question against a caller
without `orders.write` never reaches a prompt at all, because the tool was
never built.
"""

from __future__ import annotations

from agent_harness.adk_app import Concept, build_app

CONCEPT = Concept(
    name="c09_human_approval",
    title="A write that stops for a human",
    ask="Refund order ORD-10021 for 249 dollars.",
    look_for="an approval prompt carrying the exact arguments; the run parks",
    runbook="§5",
)

app = build_app(CONCEPT)
root_agent = app.root_agent
