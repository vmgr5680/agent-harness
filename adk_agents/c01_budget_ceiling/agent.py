"""The spend ceiling — RUNBOOK §1.

    ask:       What is the refund window for a delivered order?
    look for:  the answer is an admission, not a short answer.

ADK ends a run on `max_llm_calls`. It has no opinion about money, because it
does not know what a token costs. The spend ceiling is enforced here, in
`before_model_callback`: the plugin returns a response instead of letting the
call happen, so a run that would go over costs nothing to stop.

The ceiling is $0.000005 and one offline model call costs about $0.000008, so the
first call goes through and the second is refused. In the event trace that is
two `llm_request` events and a final text event reading *"I stopped before
completing this request because it reached its cost ceiling. Nothing has been
changed."*

That sentence is the concept. A truncated answer that reads like a finished one
is the failure this exists to prevent.

**`AH_BUDGET_USD_PER_RUN=0` disables the ceiling, it does not set it to zero.**
The check is `if ceiling > 0`. Setting it to `0` to block everything gets you
an unlimited run.
"""

from __future__ import annotations

from agent_harness.adk_app import Concept, build_app

CONCEPT = Concept(
    name="c01_budget_ceiling",
    title="A run that stops because it ran out of money",
    ask="What is the refund window for a delivered order?",
    look_for="the final event is the cost-ceiling admission, not an answer",
    runbook="§1",
)

app = build_app(CONCEPT, env={"AH_BUDGET_USD_PER_RUN": "0.000005"})
root_agent = app.root_agent
