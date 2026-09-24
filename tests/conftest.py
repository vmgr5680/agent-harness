"""Shared fixtures.

Every fixture builds a harness that is fully offline and fully in memory.
That is not a testing convenience — it is the payoff from keeping the model
behind `BaseLlm` and the governance in a plugin. Nothing reads a key, nothing
opens a socket, and the whole suite runs in about a second.

`OfflineLlm` is a real `BaseLlm`, not a mock of ADK. The runtime under test is
genuinely ADK: its loop, its tool dispatch, its confirmation gate, its
sessions. Only the weights are replaced.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent_harness.agents.offline import OfflineLlm
from agent_harness.config import Settings, load_settings
from agent_harness.runtime.harness import AgentHarness

ALL_SCOPES = frozenset({"kb.read", "orders.read", "orders.write"})
READ_SCOPES = frozenset({"kb.read", "orders.read"})

BASE_ENV = {
    "AH_PROVIDER": "offline",
    "AH_LOG_LEVEL": "CRITICAL",
    "AH_LOG_FORMAT": "text",
    # The limiter is sized for interactive traffic; a test suite is a batch job.
    # Leaving it on is the first self-inflicted failure most teams hit.
    "AH_RATE_LIMIT_RPM": "0",
    "AH_ENV": "dev",
    "AH_SESSION_DB_URL": "",
}


@pytest.fixture
def env() -> dict[str, str]:
    return dict(BASE_ENV)


@pytest.fixture
def settings(env: dict[str, str]) -> Settings:
    return load_settings(env)


def make_settings(**overrides: str) -> Settings:
    return load_settings({**BASE_ENV, **overrides})


def make_harness(model: OfflineLlm | None = None, **overrides: str) -> AgentHarness:
    return AgentHarness(
        make_settings(**overrides),
        model=model or OfflineLlm(),
        configure_logs=False,
    )


@pytest.fixture
def harness(settings: Settings) -> Iterator[AgentHarness]:
    h = AgentHarness(settings, model=OfflineLlm(), configure_logs=False)
    yield h
    h.close()
