"""Error taxonomy.

Much smaller than it used to be, and the reason is the point of this project:
ADK owns the runtime, so provider timeouts, rate limits, retries (once
configured in `agents/models.py`), malformed model output and tool schema
violations are its problem and are handled inside its loop. What remains here
is what the *governance* layer raises — and
governance failures are, almost without exception, terminal and user-safe.

Two questions decide how a caller treats one:

    terminal    must the run stop, or can the agent continue with this as an
                observation it gets to react to?
    user_safe   may this text reach an end user, or must it be replaced with a
                generic message and a reference id?

A budget exhaustion is terminal and user-safe. A configuration error is
terminal and not user-safe — it may name internal hosts, and the user cannot
act on it anyway.
"""

from __future__ import annotations


class HarnessError(Exception):
    """Base class. Never raised directly."""

    terminal: bool = True
    user_safe: bool = False
    code: str = "harness_error"

    def __init__(self, message: str, *, detail: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, object] = detail or {}

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "terminal": self.terminal,
            "detail": self.detail,
        }


class ConfigError(HarnessError):
    """A setting is missing, malformed or contradictory. Fail at startup, not
    three minutes into a customer's request."""

    code = "config_error"
    user_safe = False


class GuardrailBlocked(HarnessError):
    """A rail refused the input or the output.

    This is a *successful* outcome of the safety system, which is why it is
    user-safe and why the API returns 200 with a `blocked` status rather than a
    5xx. Paging an on-call engineer every time the product does its job is how
    a guardrail programme gets switched off.
    """

    code = "guardrail_blocked"
    user_safe = True

    def __init__(self, message: str, *, rail: str, findings: list[str] | None = None) -> None:
        super().__init__(message, detail={"rail": rail, "findings": findings or []})
        self.rail = rail
        self.findings = findings or []


class BudgetExceeded(HarnessError):
    """A call was refused because it would cross a spend ceiling."""

    code = "budget_exceeded"
    user_safe = True


class RateLimited(HarnessError):
    """Our own per-tenant limiter, not the provider's."""

    code = "rate_limited"
    user_safe = True


class ToolPermissionDenied(HarnessError):
    """The principal does not hold the scope a tool requires.

    Non-terminal: the denial is handed back to the model as a tool result, and
    the model usually adapts. The run continues.
    """

    code = "tool_permission_denied"
    terminal = False
    user_safe = True


class ApprovalRequired(HarnessError):
    """A write needs a human. Not an error condition — a pause."""

    code = "approval_required"
    user_safe = True
