"""Rails that run on the way *in* — before anything reaches the model.

"In" is broader than most implementations assume. It is not only what the user
typed. It is also every tool result, every retrieved document and every memory
entry that is about to be placed in the model's context. Those surfaces are
where the interesting attacks and the interesting leaks actually live, which is
why every rail here takes a `surface` and several behave differently for
`user` than for `tool_output`.
"""

from __future__ import annotations

from .detectors import Detection, find_injection, find_pii, find_secrets, mask, redact
from .types import Action, Finding, GuardrailContext, RailResult, Severity


def _findings(rail: str, detections: list[Detection], note: str = "") -> tuple[Finding, ...]:
    return tuple(
        Finding(
            rail=rail,
            kind=d.kind,
            severity=d.severity,
            start=d.start,
            end=d.end,
            sample=mask(d.value),
            note=note,
        )
        for d in detections
    )


class LengthRail:
    """A cheap denial-of-wallet control.

    A 400,000-character paste is not a question, it is a bill. Truncation
    rather than rejection keeps a legitimate long document usable, and the
    finding records that it happened so nobody debugs a mysteriously
    half-answered request for an afternoon.
    """

    name = "length"
    direction = "input"

    def __init__(self, max_chars: int = 8000) -> None:
        self.max_chars = max_chars

    def check(self, text: str, ctx: GuardrailContext) -> RailResult:
        if len(text) <= self.max_chars:
            return RailResult(self.name, Action.ALLOW, text)
        return RailResult(
            self.name,
            Action.REDACT,
            text[: self.max_chars],
            (
                Finding(
                    rail=self.name,
                    kind="oversized_input",
                    severity=Severity.LOW,
                    start=self.max_chars,
                    end=len(text),
                    sample=f"{len(text)} chars",
                    note=f"truncated to {self.max_chars}",
                ),
            ),
        )


class SecretRail:
    """Credentials, in either direction, are always a block.

    There is no legitimate reason for a live API key to be in a prompt. Unlike
    PII, redaction is the wrong answer: if a key has been pasted it should be
    rotated, and silently stripping it means nobody ever learns that it leaked.
    Blocking is the only action that produces the alert.
    """

    name = "secrets"
    direction = "both"

    def check(self, text: str, ctx: GuardrailContext) -> RailResult:
        detections = list(find_secrets(text))
        if not detections:
            return RailResult(self.name, Action.ALLOW, text)
        return RailResult(
            self.name,
            Action.BLOCK,
            redact(text, detections),
            _findings(self.name, detections, note="rotate this credential"),
        )


class PIIRail:
    """Detect personal data and replace it with typed placeholders.

    Redact rather than block, because blocking makes the assistant useless for
    the support and healthcare workloads where this matters most. The model can
    reason perfectly well about `[REDACTED:card_number]`; it just must not
    receive the digits.

    Severity gates the action: a phone number is redacted, a live Social
    Security number in a *user* prompt is escalated to a block, because at that
    point somebody has pasted something they should not have and the right
    outcome is a conversation, not a silent substitution.
    """

    name = "pii"
    direction = "both"

    def __init__(self, *, block_on_critical: bool = True) -> None:
        self.block_on_critical = block_on_critical

    def check(self, text: str, ctx: GuardrailContext) -> RailResult:
        detections = list(find_pii(text))
        if not detections:
            return RailResult(self.name, Action.ALLOW, text)
        cleaned = redact(text, detections)
        critical = [d for d in detections if d.severity is Severity.CRITICAL]
        # Tool output is *expected* to contain customer data — that is what the
        # tool is for. Redact it and move on. A user pasting the same data is a
        # different event and deserves a different response.
        escalate = self.block_on_critical and critical and ctx.surface == "user"
        action = Action.BLOCK if escalate else Action.REDACT
        return RailResult(self.name, action, cleaned, _findings(self.name, detections))


class InjectionRail:
    """Heuristic prompt-injection detection, with honest limits.

    Read the long comment in detectors.py. This rail is a detector, not a
    defence. Its real job is to raise a *signal* — a metric you can alert on
    and a finding you can correlate with which document the agent had just
    retrieved — while least privilege and approval gates do the protecting.

    The surface distinction is the load-bearing part. Injection-shaped text
    from a user is usually curiosity and is flagged. The identical text
    arriving inside a retrieved document is an attacker writing to a channel
    the user does not control, and that is blocked.
    """

    name = "injection"
    direction = "input"

    def check(self, text: str, ctx: GuardrailContext) -> RailResult:
        detections = list(find_injection(text))
        if not detections:
            return RailResult(self.name, Action.ALLOW, text)
        untrusted = ctx.surface in ("tool_output", "memory")
        worst = max(d.severity.value for d in detections)
        # ALLOW here does not mean "nothing happened". The finding is still
        # returned, logged and counted — it is flagged and metered, just not
        # enforced. That distinction is the whole design of this rail.
        action = Action.BLOCK if untrusted or worst == Severity.CRITICAL.value else Action.ALLOW
        note = "content from an untrusted surface" if untrusted else "user-authored"
        return RailResult(self.name, action, text, _findings(self.name, detections, note))


def default_input_rails(max_chars: int = 8000) -> tuple[object, ...]:
    """Order matters: truncate first so the expensive scans see bounded text."""
    return (LengthRail(max_chars), SecretRail(), PIIRail(), InjectionRail())
