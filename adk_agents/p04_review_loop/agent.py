"""Part 2 · Evaluator-optimizer: draft, check in code, revise — with a ceiling.

    ask:       What is the refund window for a delivered order?
    look for:  a cited draft routes to publish; an uncited one goes back, at most 3 times.

The graph (`agents/patterns.py::review_loop`):

    START → intake → drafter (kb_search) → review (code) ├─ publish
                        ↑                                ├─ give_up (after 3)
                        └──────────── revise ────────────┘

Then ask *Tell me something nice.* There is nothing to cite, so the trace
shows `revise`, `revise`, `give_up`, and an honest message instead of a
fourth attempt.

The evaluator is a regular expression — "does the answer cite a POL- doc_id?"
A model judge would cost a call on every iteration and occasionally approve an
uncited answer. Keep model judges for questions code cannot answer, and give
the loop a ceiling either way: a loop without one is a budget without one.
"""

from __future__ import annotations

from agent_harness.adk_app import Concept, build_pattern_app

CONCEPT = Concept(
    name="p04_review_loop",
    title="Draft, check in code, revise — with a ceiling",
    ask="What is the refund window for a delivered order?",
    look_for="a cited draft routes to publish; an uncited one goes back, at most 3 times",
    part=2,
)

app = build_pattern_app(CONCEPT, "review_loop")
root_agent = app.root_agent
