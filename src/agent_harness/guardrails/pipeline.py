"""The guardrail pipeline: run the rails, combine the verdicts, honour the mode.

Two ideas do most of the work here.

**Most-severe-wins composition.** Rails do not vote and do not negotiate. Each
returns an action; the pipeline takes the maximum. Redactions compose, because
each rail sees the text the previous one produced — so a tool result can be
stripped of a card number by one rail and of an injected instruction by the
next, in one pass.

**Three modes, and you deploy through them in order.**

    off      rails do not run. Local debugging only; config.py refuses this
             outside AH_ENV=dev.
    shadow   rails run, findings are logged and metered, nothing is changed or
             blocked. This is how you find out what your false positive rate
             actually is, on your traffic, before anyone's request breaks.
    enforce  rails are authoritative.

Shipping straight to `enforce` is the single most common way a guardrail
programme fails: the first week produces a wave of blocked legitimate requests,
support escalates, and the rails get switched off permanently. Run shadow until
the finding rate is boring, then promote.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from ..config import Settings
from ..observability import Metrics, log
from .input_rails import default_input_rails
from .output_rails import default_output_rails
from .types import Action, Finding, GuardrailContext, GuardrailDecision, Rail

_LOG = logging.getLogger("agent_harness.guardrails")


class GuardrailPipeline:
    def __init__(
        self,
        rails: Sequence[Rail],
        *,
        mode: str = "enforce",
        metrics: Metrics | None = None,
    ) -> None:
        self.rails = list(rails)
        self.mode = mode
        self.metrics = metrics or Metrics()

    @classmethod
    def for_input(cls, settings: Settings, *, metrics: Metrics | None = None) -> GuardrailPipeline:
        return cls(
            default_input_rails(settings.guardrail_max_input_chars),  # type: ignore[arg-type]
            mode=settings.guardrail_mode,
            metrics=metrics,
        )

    @classmethod
    def for_output(cls, settings: Settings, *, metrics: Metrics | None = None) -> GuardrailPipeline:
        return cls(
            default_output_rails(strict_pii=settings.guardrail_strict_output),  # type: ignore[arg-type]
            mode=settings.guardrail_mode,
            metrics=metrics,
        )

    def run(self, text: str, ctx: GuardrailContext) -> GuardrailDecision:
        started = time.perf_counter()
        if self.mode == "off":
            return GuardrailDecision(Action.ALLOW, text, mode=self.mode)

        current = text
        findings: list[Finding] = []
        worst = Action.ALLOW
        ran: list[str] = []

        for rail in self.rails:
            if rail.direction not in ("both", ctx.direction):
                continue
            ran.append(rail.name)
            result = rail.check(current, ctx)
            findings.extend(result.findings)
            if result.action.severity > worst.severity:
                worst = result.action
            if result.action is Action.BLOCK:
                # Stop on the first block: the later rails would be scanning
                # text that is never going to be used, and on the input side
                # that text may be adversarial.
                current = result.text
                break
            current = result.text

        latency_ms = (time.perf_counter() - started) * 1000
        effective = worst if self.mode == "enforce" else Action.ALLOW
        out_text = current if self.mode == "enforce" else text

        decision = GuardrailDecision(
            action=effective,
            text=out_text,
            mode=self.mode,
            findings=tuple(findings),
            would_action=worst,
            latency_ms=latency_ms,
            rails_run=tuple(ran),
        )
        self._emit(decision, ctx)
        return decision

    def _emit(self, decision: GuardrailDecision, ctx: GuardrailContext) -> None:
        self.metrics.observe(
            "ah_guardrail_latency_ms", decision.latency_ms, direction=ctx.direction
        )
        for finding in decision.findings:
            self.metrics.inc(
                "ah_guardrail_findings_total",
                rail=finding.rail,
                kind=finding.kind,
                severity=finding.severity.value,
                direction=ctx.direction,
                surface=ctx.surface,
                mode=self.mode,
            )
        if decision.would_action is not Action.ALLOW:
            self.metrics.inc(
                "ah_guardrail_actions_total",
                action=decision.would_action.value,
                enforced=str(self.mode == "enforce").lower(),
                direction=ctx.direction,
            )
            log(
                _LOG,
                logging.WARNING if decision.blocked else logging.INFO,
                "guardrail.action",
                direction=ctx.direction,
                surface=ctx.surface,
                tenant=ctx.tenant_id,
                action=decision.action.value,
                would_action=decision.would_action.value,
                shadowed=decision.shadowed,
                reasons=decision.reasons(),
            )


def build_pipelines(
    settings: Settings, *, metrics: Metrics | None = None
) -> tuple[GuardrailPipeline, GuardrailPipeline]:
    """(input, output). Constructed once per process and shared."""
    shared = metrics or Metrics()
    return (
        GuardrailPipeline.for_input(settings, metrics=shared),
        GuardrailPipeline.for_output(settings, metrics=shared),
    )
