"""Part 2 · Parallelisation: two independent reads, then a join.

    ask:       Can I still get a refund on order ORD-10021?
    look for:  order_lookup and kb_search in flight together, both screened, then one writer.

The graph (`agents/patterns.py::fanout`):

    START → intake → ┬─ lookup (order_lookup) ─┬→ join → writer (no tools)
                     └─ policy (kb_search)    ─┘

Both function calls appear before either response. The order branch's result
arrives with `[REDACTED:card_number]` in it even though it ran concurrently
with the policy branch: the plugin is on the Runner, not on a branch, so there
is no path through the graph it does not see.

Latency is the obvious win. The less obvious one is that each branch is a
narrower agent than one agent doing both would be.
"""

from __future__ import annotations

from agent_harness.adk_app import Concept, build_pattern_app

CONCEPT = Concept(
    name="p03_fanout",
    title="Two independent reads at once, then a join",
    ask="Can I still get a refund on order ORD-10021?",
    look_for="order_lookup and kb_search in flight together, both screened, then one writer",
    part=2,
)

app = build_pattern_app(CONCEPT, "fanout")
root_agent = app.root_agent
