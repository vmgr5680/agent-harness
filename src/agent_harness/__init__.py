"""agent_harness — the governance layer around a Google ADK agent.

ADK (Agent Development Kit) gives you the runtime: the agentic loop, the tool
protocol, sub-agents, sessions, the human-in-the-loop confirmation gate and
OpenTelemetry spans (retries too, once configured: they are off by default).
This package is the part it does not give you,
and the part no framework can, because half of it encodes decisions only your
organisation can make:

    guardrails on four surfaces      what may enter the context, and leave it
    tool scopes                      who may call what
    cost accounting and ceilings     what a run is allowed to spend
    per-tenant rate limiting         one caller cannot starve the rest
    a result you can branch on       instead of an event stream

Quick start — no API key, no network:

    from agent_harness import AgentHarness, RunRequest

    harness = AgentHarness()
    result = harness.run(RunRequest(
        question="What is the refund window for a delivered order?",
        tenant_id="acme",
        scopes=frozenset({"kb.read", "orders.read"}),
    ))
    print(result.answer, result.status.value, result.cost_usd)

Set `GOOGLE_API_KEY` and the same code runs against Gemini. Nothing above the
model changes — that portability is the point of keeping governance in a
plugin rather than in the agents.
"""

from __future__ import annotations

from .agents.specs import SPECS, AgentSpec
from .config import Settings, load_settings
from .errors import HarnessError
from .runtime.harness import AgentHarness
from .runtime.result import ApprovalRequest, RunRequest, RunResult, RunStatus, ToolCall

__all__ = [
    "SPECS",
    "AgentHarness",
    "AgentSpec",
    "ApprovalRequest",
    "HarnessError",
    "RunRequest",
    "RunResult",
    "RunStatus",
    "Settings",
    "ToolCall",
    "load_settings",
]

__version__ = "0.2.0"
