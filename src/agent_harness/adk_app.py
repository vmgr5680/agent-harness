"""One ADK `App` per concept, so `adk web` can show them one at a time.

`adk_agents/support` is the whole system: every rail, every scope, every
ceiling, all firing on the same run. That is the right default and a bad way to
learn, for the same reason `make demo` is — when five controls fire at once,
none of them is attributable to a thing you did.

So each folder under `adk_agents/` is one concept with one thing turned on or
off, and `adk web adk_agents` lists them side by side in the agent picker. You
compare `c05_guardrails_off` with `c07_guardrails_enforce` by switching apps in
the dropdown, not by restarting the server with a different environment.

Two rules this module exists to keep
------------------------------------
**A concept's settings go through `load_settings`, not `dataclasses.replace`.**
The overlay is environment text, validated exactly as a deployment's would be,
so a concept that configures something the config layer forbids — `off` outside
`AH_ENV=dev`, say — fails at import with the real error message rather than
running in a state no deployment could reach.

**Every app gets the `GovernancePlugin`.** A concept folder configures the
governance differently; it never omits it. An `App` in this directory without
the plugin would be the half-configured path the whole project is about.

What a concept folder cannot change
-----------------------------------
The model-call ceiling. `AH_MAX_MODEL_CALLS` is applied by the harness through
`RunConfig(max_llm_calls=...)`, and under `adk web` the RunConfig belongs to
ADK's server, not to the `App` — ADK reads `ADK_MAX_LLM_CALLS` from the
environment of the process instead. It is a front-door setting, so it is a
server restart rather than an app in the picker. See RUNBOOK §1.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from google.adk.apps.app import App

from .agents.models import build_model
from .agents.patterns import PATTERNS
from .agents.specs import SPECS, build_root_agent
from .config import Settings, load_settings
from .runtime.plugin import GovernancePlugin


@dataclass(frozen=True, slots=True)
class Concept:
    """What a concept app is for, kept in the file that configures it.

    Data rather than a docstring so the test suite can assert that every folder
    declares one, and so `make concepts` can print the index without importing
    a second copy of it.
    """

    name: str
    """The folder name, which is what the `adk web` picker lists."""

    title: str
    """One line, in the language of the runbook section it belongs to."""

    ask: str
    """Paste this into the UI. It is chosen to make the control fire offline."""

    look_for: str
    """The one thing in the trace that is the point of this app."""

    runbook: str = ""
    """The RUNBOOK section this app demonstrates, e.g. "§4"."""

    part: int = 1
    """Which article the app belongs to: 1 is the harness (`cNN_*` folders),
    2 is the workflow patterns (`pNN_*` folders). Same repo, same plugin."""


def concept_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Process environment plus a concept's overlay, validated at import."""
    return load_settings({**os.environ, **(env or {})})


def build_app(
    concept: Concept,
    *,
    agent: str = "support",
    scopes: frozenset[str] | None = None,
    env: Mapping[str, str] | None = None,
    tenant_id: str = "local",
) -> App:
    """Build one concept's app: same agents, same tools, same plugin.

    `scopes` is the caller's grant. It defaults to the agent spec's own full
    privileges, which is right for a local dev UI that has no bearer token to
    derive a narrower set from, and wrong for anything with a user in front of
    it — the HTTP API resolves scopes from the caller's token instead.
    """
    settings = concept_settings(env)
    granted = SPECS[agent].scopes if scopes is None else scopes
    return App(
        name=concept.name,
        root_agent=build_root_agent(
            model=build_model(settings, "deep"),
            granted=granted,
            agent=agent,
        ),
        plugins=[GovernancePlugin(settings, scopes=granted, tenant_id=tenant_id)],
    )


# The caller grant for every pattern app: read-only. None of the four patterns
# writes, and a demonstration that does not need a privilege should not hold it.
PATTERN_SCOPES = frozenset({"kb.read", "orders.read"})


def build_pattern_app(
    concept: Concept,
    pattern: str,
    *,
    env: Mapping[str, str] | None = None,
    tenant_id: str = "local",
) -> App:
    """Build one Part 2 app: a `Workflow` graph under the same governance plugin.

    The root is a graph rather than the support agent, and that is the only
    difference. The plugin, the scope tables and the rails are the ones every
    Part 1 app uses, which is the claim Part 2 exists to demonstrate: the
    harness does not care what shape the agent is.
    """
    settings = concept_settings(env)
    root = PATTERNS[pattern](lambda: build_model(settings, "fast"))
    return App(
        name=concept.name,
        root_agent=root,
        plugins=[GovernancePlugin(settings, scopes=PATTERN_SCOPES, tenant_id=tenant_id)],
    )
