"""Part 2 · Prompt chaining, with a gate written in code.

    ask:       What is the status of order ORD-10021?
    look for:  lookup, then the gate, then a writer that holds no tools.

The graph (`agents/patterns.py::chain`):

    START → intake → lookup (order_lookup) → gate (code)
                                              ├─ found     → writer (no tools)
                                              ├─ not_found → "I could not find that order…"
                                              └─ unchecked → "I could not check that order…"

In the trace:

    lookup    functionCall      order_lookup  {'order_id': 'ORD-10021'}
    lookup    functionResponse  order_lookup  {... '[REDACTED:card_number]' ...}
    chain     route             found
    writer    text              (the reply, written from state, with no tools)

Then ask about **ORD-99999**. The gate routes to `not_found` and the writer
never runs: a fluent reply about an order that does not exist is stopped before
it is paid for.

**The gate reads the tool's result, not the lookup agent's prose.** The first
version read the prose, and when a model call failed it told a customer their order
did not exist when nothing had been checked. `not_found` and `unchecked` are
different facts and get different messages.
"""

from __future__ import annotations

from agent_harness.adk_app import Concept, build_pattern_app

CONCEPT = Concept(
    name="p01_chain",
    title="Prompt chaining, with a gate in code",
    ask="What is the status of order ORD-10021?",
    look_for="lookup, then the gate, then a writer that holds no tools",
    part=2,
)

app = build_pattern_app(CONCEPT, "chain")
root_agent = app.root_agent
