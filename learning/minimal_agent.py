#!/usr/bin/env python3
"""
==============================================================================
  LEARNING EXAMPLE — NOT PRODUCTION CODE. DO NOT SHIP ANYTHING SHAPED LIKE IT.
==============================================================================

An ADK agent in about eighty lines, so you can hold the shape in your head
before reading the real thing in ../src.

Run it:
    python learning/minimal_agent.py                    # offline, no key
    GOOGLE_API_KEY=... python learning/minimal_agent.py  # live

What ADK gives you, visible here
--------------------------------
    the agentic loop      you never write a `while`. Runner + LlmAgent do it.
    the tool protocol     a typed Python function IS the tool. ADK derives the
                          schema from the signature and the docstring.
    schema validation     a missing argument goes back to the model as a
                          correctable error, not an exception.
    sub-agents            AgentTool, one line.
    human in the loop     require_confirmation=True.
    sessions and events   every turn persisted, which is your audit trail.
    tracing               OpenTelemetry spans, free.

What it does NOT give you, and why each is a module in ../src
-------------------------------------------------------------
    guardrails            runtime/plugin.py + guardrails/
                          Nothing here stops a card number from the tool
                          response landing in the prompt, the answer and the
                          logs. Look at what `order_lookup` returns below.
    tool scopes           runtime/plugin.py
                          Every tool here is callable by every caller.
    cost and budgets      economics/pricing.py + the plugin
                          usage_metadata exists; nothing turns it into money
                          or refuses a call that would cross a ceiling.
    per-tenant limits     economics/ratelimit.py
    a result contract     runtime/result.py
                          Here you fish text out of an event stream by hand.

The honest summary: ADK removed about 400 lines of loop, dispatch and HITL
plumbing from this project. It removed none of the governance, because none of
the governance is a framework's decision to make.
"""

from __future__ import annotations

import asyncio
import os

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.function_tool import FunctionTool
from google.genai import types

# --- the domain --------------------------------------------------------------

ORDERS = {
    # Note the card number and the email. Nobody typed them; the tool returns
    # them. That is the surface the real harness scans and this file does not.
    "ORD-10021": {
        "status": "delivered",
        "total_usd": 249.00,
        "payment_card": "4242 4242 4242 4242",
        "customer_email": "dana.whitfield@example.com",
    },
}
POLICY = {
    "POL-REFUND-01": "Refunds may be requested within 30 days of the delivery date.",
    "POL-RETURN-01": "Return shipping is free for orders above 100 USD.",
}


def kb_search(query: str) -> dict:
    """Search the refund policy.

    Args:
        query: What to look for.
    """
    hits = {k: v for k, v in POLICY.items() if any(w in v.lower() for w in query.lower().split())}
    return {"hits": hits or POLICY}


def order_lookup(order_id: str) -> dict:
    """Look up one order by id, for example ORD-10021.

    Args:
        order_id: The order identifier.
    """
    return ORDERS.get(order_id.upper(), {"error": "no such order"})


def issue_refund(order_id: str, amount_usd: float) -> dict:
    """Issue a refund. Changes state.

    Args:
        order_id: The order to refund.
        amount_usd: How much to refund.
    """
    return {"status": "refund_submitted", "order_id": order_id, "amount_usd": amount_usd}


# --- the agent ---------------------------------------------------------------


def build() -> LlmAgent:
    model = os.environ.get("AH_MODEL_DEEP", "gemini-3.1-flash-lite")
    if not os.environ.get("GOOGLE_API_KEY"):
        from agent_harness.agents.offline import OfflineLlm

        model = OfflineLlm()  # type: ignore[assignment]
    return LlmAgent(
        name="support",
        model=model,
        instruction=(
            "You answer questions about orders and refunds. Retrieve policies "
            "with kb_search and cite the doc_id. Never guess an order's status."
        ),
        tools=[
            FunctionTool(kb_search),
            FunctionTool(order_lookup),
            # One keyword is the entire human-in-the-loop gate. In the real
            # harness this is the control that makes prompt injection
            # survivable rather than catastrophic.
            FunctionTool(issue_refund, require_confirmation=True),
        ],
    )


async def ask(question: str) -> None:
    sessions = InMemorySessionService()
    runner = Runner(app_name="minimal", agent=build(), session_service=sessions)
    await sessions.create_session(app_name="minimal", user_id="u1", session_id="s1")

    print(f"\n> {question}")
    async for event in runner.run_async(
        user_id="u1",
        session_id="s1",
        new_message=types.Content(role="user", parts=[types.Part(text=question)]),
    ):
        for call in event.get_function_calls():
            print(f"  tool: {call.name}({dict(call.args or {})})")
        if event.is_final_response() and event.content:
            text = "".join(p.text or "" for p in event.content.parts or [])
            if text.strip():
                print(f"  => {text[:300]}")


async def main() -> None:
    live = "live (Gemini)" if os.environ.get("GOOGLE_API_KEY") else "offline"
    print(f"minimal ADK agent — {live}")
    await ask("What is the refund window for a delivered order?")
    await ask("What is the status of order ORD-10021?")
    print(
        "\nNotice what just happened on the second question: the card number and "
        "the email address went into the model's context and would have gone "
        "into your logs. Nothing here stopped it.\n"
        "That is the gap ../src/agent_harness/runtime/plugin.py exists to close."
    )


if __name__ == "__main__":
    asyncio.run(main())
