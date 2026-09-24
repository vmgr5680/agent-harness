"""The per-tenant rate limit — RUNBOOK §6.

    ask:       What is the refund window for a delivered order?
    then ask it again, immediately.
    look for:  the second run answers "This tenant is sending requests too
               quickly. Try again in 60 seconds."

A token bucket sized for one tenant, set here to a burst of two and a refill of
one request per minute. One question costs two model calls — the tool call and
the answer — so the first question empties the bucket and the second is refused
in `before_model_callback`, before the provider is called.

Two things worth noticing.

**The limit is per tenant, not per process.** Every app in this directory runs
as tenant `local`; the HTTP API derives the tenant from the caller's bearer
token instead. A limiter keyed on the process protects your provider quota. A
limiter keyed on the tenant protects one customer from another.

**This is the setting that will break your eval suite.** A suite is a batch job
that fires every case at once and exhausts the bucket, turning real passes into
"this tenant is sending requests too quickly". `make evals` sets
`AH_RATE_LIMIT_RPM=0` for exactly that reason. Running the CLI by hand does
not.
"""

from __future__ import annotations

from agent_harness.adk_app import Concept, build_app

CONCEPT = Concept(
    name="c10_rate_limit",
    title="A tenant that has used its quota",
    ask="What is the refund window for a delivered order?",
    look_for="ask twice: the second run is refused before the model is called",
    runbook="§6",
)

app = build_app(
    CONCEPT,
    env={"AH_RATE_LIMIT_RPM": "1", "AH_RATE_LIMIT_BURST": "2"},
)
root_agent = app.root_agent
