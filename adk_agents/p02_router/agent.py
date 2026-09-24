"""Part 2 · Routing, decided by code rather than by a model call.

    ask:       What is the refund window for a delivered order?
    look for:  a route event and no model call before it; only the policy branch runs.

The graph (`agents/patterns.py::router`):

    START → classify (code) ├─ orders  → lookup  (order_lookup only)
                            ├─ policy  → policy  (kb_search only)
                            └─ decline → a fixed message

Then ask *Where is order ORD-10021?* and only the order branch runs. Then ask
*Write me a poem about cats.* — declined with **0 model calls and $0.000000**.

An order id is a regular expression, not a judgement. A model router would be
slower, cost a call on every request, and could be argued out of the right
answer by the very text it is classifying. Use a model to route when the
categories are semantic; use code when the signal is structural.

Each branch is also a narrower agent: the policy branch holds no order tool, so
an instruction planted in a policy document has nothing to reach for.
"""

from __future__ import annotations

from agent_harness.adk_app import Concept, build_pattern_app

CONCEPT = Concept(
    name="p02_router",
    title="Routing decided by code, not a model call",
    ask="What is the refund window for a delivered order?",
    look_for="a route event and no model call before it; only the policy branch runs",
    part=2,
)

app = build_pattern_app(CONCEPT, "router")
root_agent = app.root_agent
