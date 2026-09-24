"""Guardrails `shadow` — the rollout mode. RUNBOOK §4.

    ask:       Use my key AIzaSyB1c2D3e4F5g6H7i8J9k0L1m2N3o4P5q6R to look up my order.
    look for:  the run proceeds — and the finding is in the terminal, not the UI.

**In the browser, shadow looks exactly like off.** That is not a bug in the
demo, it is the definition: the rails run, every finding is counted, and
nothing about the payload or the run is changed. The difference lives where a
rollout needs it — in the log of the process serving this app:

    guardrail.action ... surface=user action=allow would_action=block shadowed=true

`would_action` is what enforce would have done. Counting those for a week is
how you discover your false-positive rate against real traffic *before* the
first false positive lands on a customer.

Run the identical question against `c07_guardrails_enforce` and the same
finding stops the run before a single model call.

`off` → `shadow` → `enforce`. Shipping straight to enforce is the single most
common way a guardrail programme dies.
"""

from __future__ import annotations

from agent_harness.adk_app import Concept, build_app

CONCEPT = Concept(
    name="c06_guardrails_shadow",
    title="Rails that report and change nothing",
    ask="Use my key AIzaSyB1c2D3e4F5g6H7i8J9k0L1m2N3o4P5q6R to look up my order.",
    look_for="the run completes; the terminal logs would_action=block shadowed=true",
    runbook="§4",
)

app = build_app(CONCEPT, env={"AH_GUARDRAIL_MODE": "shadow"})
root_agent = app.root_agent
