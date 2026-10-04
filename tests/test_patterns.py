"""Part 2 — the workflow-pattern apps (`adk_agents/pNN_*`).

Part 1's apps (`cNN_*`, `tests/test_adk_concepts.py`) turn one control on or
off inside the same support agent. Part 2's apps change the *shape* of the
agent — a chain, a router, a fan-out, a review loop — and keep the governance
identical. So every test here asserts two things: the pattern did what its
docstring promises, and the plugin still saw everything inside the graph.

The structural checks every folder must pass (a `CONCEPT` card, exactly one
`GovernancePlugin`, a row in the catalogue) run over these folders too, from
`tests/test_adk_concepts.py`.
"""

from __future__ import annotations

import asyncio
import importlib
import pathlib
from dataclasses import dataclass, field
from typing import Any

import pytest
from google.adk.apps.app import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent_harness.adk_app import PATTERN_SCOPES, Concept, concept_settings
from agent_harness.agents.offline import OfflineLlm
from agent_harness.agents.patterns import (
    DECLINE_MESSAGE,
    GIVE_UP_MESSAGE,
    MAX_REVIEW_ATTEMPTS,
    NO_ORDER_MESSAGE,
    UNAVAILABLE_MESSAGE,
    UNCHECKED_MESSAGE,
    chain,
    review_loop,
)
from agent_harness.runtime.plugin import GovernancePlugin
from agent_harness.tools.adk_tools import WRITE_TOOLS

AGENTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "adk_agents"

_OFFLINE = {
    "AH_PROVIDER": "offline",
    "AH_LOG_LEVEL": "CRITICAL",
    "AH_LOG_FORMAT": "text",
    "AH_SESSION_DB_URL": "",
    "AH_ENV": "dev",
}

PART2 = sorted(p.name for p in AGENTS_DIR.iterdir() if p.is_dir() and p.name.startswith("p"))


@dataclass(slots=True)
class Trace:
    authors: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)  # (author, tool)
    responses: list[str] = field(default_factory=list)
    routes: list[Any] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    account: Any = None

    def ran(self, author: str) -> bool:
        return author in self.authors

    def tools(self) -> list[str]:
        return [tool for _, tool in self.calls]


@pytest.fixture(scope="module")
def apps() -> dict[str, tuple[Concept, App]]:
    with pytest.MonkeyPatch.context() as mp:
        for key, value in _OFFLINE.items():
            mp.setenv(key, value)
        mp.syspath_prepend(str(AGENTS_DIR))
        loaded: dict[str, tuple[Concept, App]] = {}
        for folder in PART2:
            module = importlib.import_module(f"{folder}.agent")
            loaded[folder] = (module.CONCEPT, module.app)
        return loaded


async def _drive(app: App, question: str) -> Trace:
    service = InMemorySessionService()
    runner = Runner(app=app, session_service=service)
    session = f"s{abs(hash(question)) % 9973}"
    await service.create_session(app_name=app.name, user_id="u1", session_id=session)
    trace = Trace()
    invocation = ""
    async for event in runner.run_async(
        user_id="u1",
        session_id=session,
        new_message=types.Content(role="user", parts=[types.Part(text=question)]),
    ):
        invocation = invocation or event.invocation_id
        trace.authors.append(event.author or "")
        for call in event.get_function_calls():
            trace.calls.append((event.author or "", call.name or ""))
        for response in event.get_function_responses():
            trace.responses.append(str(response.response))
        if event.actions and event.actions.route is not None:
            trace.routes.append(event.actions.route)
        if getattr(event, "output", None) is not None:
            trace.outputs.append(str(event.output))
    trace.account = app.plugins[0].accounts.get(invocation)  # type: ignore[union-attr]
    return trace


def run(apps: dict[str, tuple[Concept, App]], folder: str, question: str | None = None) -> Trace:
    concept, app = apps[folder]
    return asyncio.run(_drive(app, question or concept.ask))


def test_part2_has_the_four_patterns() -> None:
    assert PART2 == ["p01_chain", "p02_router", "p03_fanout", "p04_review_loop"]


@pytest.mark.parametrize("folder", PART2)
def test_every_part2_app_says_it_is_part2(
    apps: dict[str, tuple[Concept, App]], folder: str
) -> None:
    concept, _ = apps[folder]
    assert concept.part == 2


def test_pattern_apps_are_read_only_by_construction() -> None:
    """None of the patterns writes, so none is granted a write scope."""
    assert not any(scope.endswith(".write") for scope in PATTERN_SCOPES)
    assert WRITE_TOOLS  # the set exists; the grant simply never reaches it


# --- p01 chain ----------------------------------------------------------------


def test_p01_chain_hands_the_lookup_to_a_writer_with_no_tools(
    apps: dict[str, tuple[Concept, App]],
) -> None:
    t = run(apps, "p01_chain")
    assert t.calls == [("lookup", "order_lookup")], "the writer holds no tools to call"
    assert "found" in t.routes
    assert t.ran("writer")


def test_p01_chain_gate_stops_before_the_writer_is_paid_for(
    apps: dict[str, tuple[Concept, App]],
) -> None:
    t = run(apps, "p01_chain", "What is the status of order ORD-99999?")
    assert "not_found" in t.routes
    assert not t.ran("writer")
    assert NO_ORDER_MESSAGE in t.outputs


def test_p01_chain_never_says_not_found_when_nothing_was_checked() -> None:
    """Found live, when model calls failed: the first version of the gate read the
    lookup agent's prose, and told a customer their order did not exist when
    the model had simply been unreachable. The gate now reads the tool result.
    """
    with pytest.MonkeyPatch.context() as mp:
        for key, value in _OFFLINE.items():
            mp.setenv(key, value)
        settings = concept_settings()
    app = App(
        name="chain_outage",
        root_agent=chain(lambda: OfflineLlm(fail_times=10)),
        plugins=[GovernancePlugin(settings, scopes=PATTERN_SCOPES, tenant_id="t")],
    )
    t = asyncio.run(_drive(app, "What is the status of order ORD-10021?"))
    assert "unchecked" in t.routes
    assert UNCHECKED_MESSAGE in t.outputs
    assert NO_ORDER_MESSAGE not in t.outputs


def test_p01_chain_never_says_not_found_when_the_lookup_failed() -> None:
    """A failed lookup is not a missing order. Here the plugin refuses the call
    (no orders.read scope) and returns an error; the gate must say "could not
    check", because the order system was never asked."""
    with pytest.MonkeyPatch.context() as mp:
        for key, value in _OFFLINE.items():
            mp.setenv(key, value)
        settings = concept_settings()
    app = App(
        name="chain_denied",
        root_agent=chain(lambda: OfflineLlm()),
        plugins=[GovernancePlugin(settings, scopes=frozenset({"kb.read"}), tenant_id="t")],
    )
    t = asyncio.run(_drive(app, "What is the status of order ORD-10021?"))
    assert "unchecked" in t.routes
    assert UNCHECKED_MESSAGE in t.outputs
    assert NO_ORDER_MESSAGE not in t.outputs
    assert not t.ran("writer")


def test_p01_chain_redacts_inside_the_graph(apps: dict[str, tuple[Concept, App]]) -> None:
    t = run(apps, "p01_chain")
    seen = " ".join(t.responses)
    assert "4242 4242 4242 4242" not in seen
    assert "[REDACTED:card_number]" in seen


# --- p02 router ---------------------------------------------------------------


def test_p02_router_sends_a_policy_question_to_the_policy_branch_only(
    apps: dict[str, tuple[Concept, App]],
) -> None:
    t = run(apps, "p02_router")
    assert t.routes[0] == "policy"
    assert t.tools() == ["kb_search"]
    assert not t.ran("lookup")


def test_p02_router_sends_an_order_question_to_the_order_branch_only(
    apps: dict[str, tuple[Concept, App]],
) -> None:
    t = run(apps, "p02_router", "Where is order ORD-10021?")
    assert t.routes[0] == "orders"
    assert t.tools() == ["order_lookup"]
    assert not t.ran("policy")


def test_p02_router_declines_off_topic_for_nothing(apps: dict[str, tuple[Concept, App]]) -> None:
    """Routing in code: the decision itself costs no model call."""
    t = run(apps, "p02_router", "Write me a poem about cats.")
    assert t.routes == ["decline"]
    assert DECLINE_MESSAGE in t.outputs
    assert t.account.model_calls == 0
    assert t.account.cost_usd == 0.0


# --- p03 fan-out --------------------------------------------------------------


def test_p03_fanout_runs_both_reads_before_either_answers(
    apps: dict[str, tuple[Concept, App]],
) -> None:
    t = run(apps, "p03_fanout")
    first_two = {tool for _, tool in t.calls[:2]}
    assert first_two == {"order_lookup", "kb_search"}, "both branches start together"
    assert t.ran("writer")
    assert t.authors.index("writer") > max(
        i for i, a in enumerate(t.authors) if a in {"lookup", "policy"}
    ), "the writer waits for the join"


def test_p03_fanout_screens_the_concurrent_branch_too(
    apps: dict[str, tuple[Concept, App]],
) -> None:
    t = run(apps, "p03_fanout")
    seen = " ".join(t.responses)
    assert "4242 4242 4242 4242" not in seen
    assert "dana.whitfield@example.com" not in seen
    assert {"card_number", "email"} <= {f["kind"] for f in t.account.findings}


def test_p03_fanout_branches_hold_only_their_own_tool(
    apps: dict[str, tuple[Concept, App]],
) -> None:
    t = run(apps, "p03_fanout")
    assert ("lookup", "kb_search") not in t.calls
    assert ("policy", "order_lookup") not in t.calls


# --- p04 review loop ----------------------------------------------------------


def test_p04_publishes_a_cited_draft_on_the_first_pass(
    apps: dict[str, tuple[Concept, App]],
) -> None:
    t = run(apps, "p04_review_loop")
    assert t.routes == ["publish"]
    assert any("POL-REFUND-01" in o for o in t.outputs)


def test_p04_gives_up_at_the_ceiling_rather_than_looping(
    apps: dict[str, tuple[Concept, App]],
) -> None:
    t = run(apps, "p04_review_loop", "Tell me something nice.")
    assert t.routes == ["revise"] * (MAX_REVIEW_ATTEMPTS - 1) + ["give_up"]
    assert GIVE_UP_MESSAGE in t.outputs


def test_p04_second_question_in_a_session_gets_its_own_ceiling(
    apps: dict[str, tuple[Concept, App]],
) -> None:
    """Session state outlives a run; the attempt counter must not."""
    _, app = apps["p04_review_loop"]

    async def twice() -> Trace:
        service = InMemorySessionService()
        runner = Runner(app=app, session_service=service)
        await service.create_session(app_name=app.name, user_id="u1", session_id="same")
        trace = Trace()
        for question in ("Tell me something nice.", "Tell me something else."):
            trace = Trace()
            async for event in runner.run_async(
                user_id="u1",
                session_id="same",
                new_message=types.Content(role="user", parts=[types.Part(text=question)]),
            ):
                if event.actions and event.actions.route is not None:
                    trace.routes.append(event.actions.route)
        return trace

    t = asyncio.run(twice())
    assert t.routes == ["revise"] * (MAX_REVIEW_ATTEMPTS - 1) + ["give_up"]


def test_p04_does_not_retry_an_outage_or_blame_the_draft() -> None:
    """Found live in a Gemini 503: the loop spent all three attempts on
    a model that was down, then said it could not cite a policy."""
    with pytest.MonkeyPatch.context() as mp:
        for key, value in _OFFLINE.items():
            mp.setenv(key, value)
        settings = concept_settings()
    app = App(
        name="loop_outage",
        root_agent=review_loop(lambda: OfflineLlm(fail_times=10)),
        plugins=[GovernancePlugin(settings, scopes=PATTERN_SCOPES, tenant_id="t")],
    )
    t = asyncio.run(_drive(app, "What is the refund window for a delivered order?"))
    assert t.routes == ["unchecked"], "one failed attempt, not three"
    assert UNAVAILABLE_MESSAGE in t.outputs
    assert GIVE_UP_MESSAGE not in t.outputs
