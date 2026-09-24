"""Guardrails `off` — the control group. RUNBOOK §4.

    ask:       What is the status of order ORD-10021?
    look for:  expand the order_lookup functionResponse. The card number and the
               email address are both there, in the event the model was given.

This is what the agent looks like with the rails switched off, and it is here
so that `c07_guardrails_enforce` has something to be compared against. Run the
same question in both and diff the `functionResponse` event.

Nobody types a card number. The billing API returns one. That is why the most
important hook in the plugin is `after_tool_callback`, not
`on_user_message_callback`.

`AH_GUARDRAIL_MODE=off` is refused unless `AH_ENV=dev`, which is why this file
sets both. An app that can only exist in dev is the correct shape for a
control group.
"""

from __future__ import annotations

from agent_harness.adk_app import Concept, build_app

CONCEPT = Concept(
    name="c05_guardrails_off",
    title="The control group: what leaks with the rails off",
    ask="What is the status of order ORD-10021?",
    look_for="the card number and email are visible in the functionResponse event",
    runbook="§4",
)

app = build_app(CONCEPT, env={"AH_GUARDRAIL_MODE": "off", "AH_ENV": "dev"})
root_agent = app.root_agent
