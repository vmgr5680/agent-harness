"""Entry point for ADK's own tooling: `adk web`, `adk run`, `adk eval`.

    adk web adk_agents          # the browser UI, with the event trace
    adk run adk_agents/support  # a terminal chat
    adk eval adk_agents/support var/support.evalset.json

ADK's CLI discovers a `root_agent` in a module named `agent`. Exposing one here
means the same agents, the same tools and the same governance plugin are
driveable from ADK's tooling as from this package's own API — there is no
second definition to keep in sync.

The plugin is attached through `App`, which is what ADK's CLI loads when it is
present. That matters: without it `adk web` would run the agents with no
guardrails, no scope checks and no cost ceiling, which is exactly the
half-configured path that ends up in production by accident.

This app is the whole system at once, which makes it the right default and a
poor way to learn: when five controls fire on one run, none of them is
attributable to something you did. The `cNN_*` folders beside this one are the
same system with one concept turned on or off each, listed side by side in the
`adk web` picker. Start there, and see `adk_agents/README.md`.
"""

from __future__ import annotations

from google.adk.apps.app import App

from agent_harness.adk_app import Concept
from agent_harness.agents.models import build_model
from agent_harness.agents.specs import SUPPORT, build_root_agent
from agent_harness.config import load_settings
from agent_harness.runtime.plugin import GovernancePlugin

CONCEPT = Concept(
    name="support",
    title="The whole system, every control live",
    ask="What is the status of order ORD-10021?",
    look_for="redacted tool output, a citation, and a cost in the ledger",
    runbook="§0",
)

_settings = load_settings()

# The interactive tooling has no bearer token to derive scopes from, so it runs
# with the support agent's full designed privileges. That is correct for a
# local developer UI and wrong for anything else — the HTTP API in
# `agent_harness.api` resolves scopes from the caller's token instead.
_scopes = SUPPORT.scopes

root_agent = build_root_agent(
    model=build_model(_settings, "deep"),
    granted=_scopes,
    agent="support",
)

app = App(
    name=_settings.service_name,
    root_agent=root_agent,
    plugins=[
        GovernancePlugin(
            _settings,
            scopes=_scopes,
            tenant_id="local",
        )
    ],
)
