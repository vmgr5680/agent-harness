"""Shared vocabulary for the guardrail layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class Action(str, Enum):
    """What a rail wants to happen. The pipeline takes the most severe."""

    ALLOW = "allow"
    REDACT = "redact"
    BLOCK = "block"

    @property
    def severity(self) -> int:
        return {"allow": 0, "redact": 1, "block": 2}[self.value]


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing a rail noticed.

    `sample` is deliberately a *masked* excerpt. A findings list is written to
    logs and returned over the API; a guardrail that leaks the secret it caught
    into the audit trail has moved the problem rather than solved it.
    """

    rail: str
    kind: str
    severity: Severity
    start: int
    end: int
    sample: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rail": self.rail,
            "kind": self.kind,
            "severity": self.severity.value,
            "at": [self.start, self.end],
            "sample": self.sample,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class RailResult:
    rail: str
    action: Action
    text: str
    findings: tuple[Finding, ...] = ()

    @property
    def clean(self) -> bool:
        return self.action is Action.ALLOW and not self.findings


@dataclass(frozen=True, slots=True)
class GuardrailDecision:
    """The pipeline's verdict.

    `text` is what the caller must use downstream. In `shadow` mode it is the
    *original* text even when rails wanted to redact — that is the entire point
    of shadow mode, and the reason `would_action` is reported separately from
    `action`.
    """

    action: Action
    text: str
    mode: str
    findings: tuple[Finding, ...] = ()
    would_action: Action = Action.ALLOW
    latency_ms: float = 0.0
    rails_run: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.action is Action.BLOCK

    @property
    def shadowed(self) -> bool:
        """True when a rail wanted to act and the mode prevented it."""
        return self.would_action.severity > self.action.severity

    def reasons(self) -> list[str]:
        return [f"{f.rail}:{f.kind}" for f in self.findings]

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "would_action": self.would_action.value,
            "mode": self.mode,
            "shadowed": self.shadowed,
            "latency_ms": round(self.latency_ms, 3),
            "rails_run": list(self.rails_run),
            "findings": [f.as_dict() for f in self.findings],
        }


@dataclass(slots=True)
class GuardrailContext:
    """Everything a rail may need that is not the text itself.

    `allowed_domains` and `grounding_sources` are what turn two of the output
    rails from string matching into something with actual teeth.
    """

    tenant_id: str = "anonymous"
    direction: str = "input"
    surface: str = "user"  # user | tool_output | model_output | memory
    allowed_domains: frozenset[str] = frozenset()
    grounding_sources: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Rail(Protocol):
    """One check. Pure: same text and context, same result, no I/O.

    Purity is not fastidiousness — it is what makes a rail testable in
    microseconds, safe to run in shadow mode against production traffic, and
    replayable against a stored transcript during an incident.
    """

    name: str
    direction: str  # "input" | "output" | "both"

    def check(self, text: str, ctx: GuardrailContext) -> RailResult: ...
