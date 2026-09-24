"""The request and result types the harness exposes.

These exist so that callers — the HTTP API, the CLI, the eval runner — never
handle ADK `Event` objects directly. An event stream is the right internal
representation and the wrong external contract: it is long, it is versioned by
someone else, and most of it is uninteresting to a caller who asked a question
and wants an answer.

`RunResult` is deliberately opinionated about what a caller always needs:

    status        what happened, as one word you can branch on
    answer        the text, already through the output rails
    cost_usd      per request, not per month
    run_id        the id that joins this to logs, traces and the session
    steps         what it actually did, for audit and for evals
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RunStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"  # a guardrail refused
    NEEDS_APPROVAL = "needs_approval"  # paused for a human
    EXHAUSTED = "exhausted"  # out of steps, time or budget
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunRequest:
    question: str
    tenant_id: str = "anonymous"
    user_id: str = "anonymous"
    session_id: str | None = None
    agent: str = "support"
    # Scopes the CALLER holds. The effective set is the intersection with the
    # agent's own, so neither can escalate the other.
    scopes: frozenset[str] = frozenset()
    allowed_domains: frozenset[str] = frozenset()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolCall:
    """One tool invocation, as it will appear in an audit record."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    ok: bool = True
    agent: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.name,
            "args": self.args,
            "result": self.result[:1000],
            "ok": self.ok,
            "agent": self.agent,
        }


@dataclass(slots=True)
class ApprovalRequest:
    """A write the agent wants to make, waiting on a human.

    `function_call_id` is ADK's handle for the paused call. Resuming means
    sending a function response with that id and a `ToolConfirmation`, which is
    what `AgentHarness.approve()` does.
    """

    approval_id: str
    function_call_id: str
    session_id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "function_call_id": self.function_call_id,
            "session_id": self.session_id,
            "tool": self.tool,
            "args": self.args,
            "hint": self.hint,
        }


@dataclass(slots=True)
class RunResult:
    run_id: str
    session_id: str
    status: RunStatus
    answer: str
    agent: str
    tenant_id: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    model_calls: int = 0
    cost_usd: float = 0.0
    cost_breakdown: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    approval: ApprovalRequest | None = None
    guardrail_findings: list[dict[str, Any]] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is RunStatus.COMPLETED

    @property
    def tools_attempted(self) -> list[str]:
        return [c.name for c in self.tool_calls]

    @property
    def tools_executed(self) -> list[str]:
        """Attempted is not executed. The difference is your authorisation
        working, and it is the distinction the eval assertions turn on."""
        return [c.name for c in self.tool_calls if c.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "answer": self.answer,
            "agent": self.agent,
            "tenant_id": self.tenant_id,
            "citations": self.citations,
            "model_calls": self.model_calls,
            "tools_attempted": self.tools_attempted,
            "tools_executed": self.tools_executed,
            "denied_tools": self.denied_tools,
            "tool_calls": [c.as_dict() for c in self.tool_calls],
            "cost_usd": round(self.cost_usd, 8),
            "cost_breakdown": self.cost_breakdown,
            "duration_ms": round(self.duration_ms, 2),
            "approval": self.approval.as_dict() if self.approval else None,
            "guardrail_findings": self.guardrail_findings,
            "error": self.error,
        }
