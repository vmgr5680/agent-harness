"""Pattern detectors for sensitive data and for injection-shaped text.

Please read this before you rely on it
--------------------------------------
Regular expressions detect *format*, not *meaning*. They will find a card
number written as sixteen digits and miss the same number written in words.
They will flag an order id that happens to look like a Social Security number.
Both mistakes are guaranteed, not hypothetical.

That is not an argument against having them. It is an argument for knowing what
layer they are. In a serious deployment this module is the **cheap first pass**
in front of a real detection stack — a named-entity model, a commercial data
loss prevention service, or both — and the interface below is deliberately
narrow so that swapping the implementation does not touch the rails.

Two conventions worth copying:

- **Every example SSN in this codebase is in the 900 range.** The Social
  Security Administration has never issued a 9xx number, so the test fixtures
  cannot collide with a real person's identifier. Using 123-45-6789 in a
  fixture puts a real, issued number in your repository.
- **Card numbers are Luhn-checked before they are reported.** Sixteen digits
  are a very common shape; sixteen digits that pass Luhn are not. Skipping the
  check is how a PII dashboard fills with order numbers and gets ignored.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from .types import Severity


@dataclass(frozen=True, slots=True)
class Detection:
    kind: str
    start: int
    end: int
    value: str
    severity: Severity


def mask(value: str, *, keep: int = 4) -> str:
    """Mask a matched value for safe logging: last `keep` characters survive."""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


# --- sensitive data ----------------------------------------------------------

_EMAIL: Final = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_SSN: Final = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
_SSN_LOOSE: Final = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD: Final = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PHONE: Final = re.compile(r"\b(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b")
_IBAN: Final = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")

# Credentials. These are severity CRITICAL everywhere, in both directions: a
# key in a prompt is an exfiltration event and a key in an output is a breach.
_SECRETS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("bearer_header", re.compile(r"\bAuthorization:\s*Bearer\s+[A-Za-z0-9._\-]{16,}")),
)

_URL: Final = re.compile(r"https?://([^\s/$?#\)\]\"'>]+)", re.IGNORECASE)
# Markdown image/link with an interpolated-looking query string: the classic
# zero-click exfiltration shape, where rendering the answer makes the request.
_MD_EXFIL: Final = re.compile(r"!?\[[^\]]*\]\((https?://[^)]*[?&][^)]*)\)", re.IGNORECASE)


def luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum. `digits` must contain only 0-9."""
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def find_secrets(text: str) -> Iterator[Detection]:
    for kind, pattern in _SECRETS:
        for m in pattern.finditer(text):
            yield Detection(kind, m.start(), m.end(), m.group(0), Severity.CRITICAL)


def find_pii(text: str) -> Iterator[Detection]:
    for m in _EMAIL.finditer(text):
        yield Detection("email", m.start(), m.end(), m.group(0), Severity.MEDIUM)

    for m in _SSN_LOOSE.finditer(text):
        # A 9xx number cannot have been issued, so treat it as a test fixture
        # rather than a real identifier — but still report it, at LOW, because
        # a fixture reaching production data flow is its own smell.
        issued = _SSN.fullmatch(m.group(0)) is not None
        yield Detection(
            "ssn" if issued else "ssn_test_fixture",
            m.start(),
            m.end(),
            m.group(0),
            Severity.CRITICAL if issued else Severity.LOW,
        )

    for m in _CARD.finditer(text):
        digits = re.sub(r"[ -]", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            yield Detection("card_number", m.start(), m.end(), m.group(0), Severity.CRITICAL)

    for m in _PHONE.finditer(text):
        yield Detection("phone", m.start(), m.end(), m.group(0), Severity.MEDIUM)

    for m in _IBAN.finditer(text):
        yield Detection("iban", m.start(), m.end(), m.group(0), Severity.HIGH)


# --- prompt injection --------------------------------------------------------
#
# Every pattern here is a heuristic and every heuristic here is bypassable.
# Detection is the *third* line of defence against prompt injection. The first
# two are architectural and live elsewhere in this codebase:
#
#   1. Least privilege   -- tools/registry.py: a subagent that reads untrusted
#                           content holds no write scopes, so a successful
#                           injection has nothing worth stealing.
#   2. Human approval    -- harness/loop.py: side-effecting tools stop the run
#                           and ask. An injection cannot approve itself.
#
# Treat what follows as a smoke alarm, not a sprinkler system.

_INJECTION_PATTERNS: Final[tuple[tuple[str, re.Pattern[str], Severity], ...]] = (
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}"
            r"\b(?:previous|prior|above|earlier|all)\b[^.\n]{0,20}"
            r"\b(?:instruction|prompt|rule|direction|context)s?\b",
            re.IGNORECASE,
        ),
        Severity.HIGH,
    ),
    (
        "system_prompt_probe",
        re.compile(
            r"\b(?:reveal|show|print|repeat|output|disclose)\b[^.\n]{0,30}"
            r"\b(?:system prompt|initial instructions|your instructions|your rules)\b",
            re.IGNORECASE,
        ),
        Severity.HIGH,
    ),
    (
        "role_hijack",
        re.compile(
            r"\b(?:you are now|from now on you are|act as|pretend to be|new persona)\b"
            r"[^.\n]{0,40}\b(?:developer mode|dan|unrestricted|no rules|jailbroken)\b",
            re.IGNORECASE,
        ),
        Severity.HIGH,
    ),
    (
        "fake_turn_boundary",
        re.compile(
            r"(?:^|\n)\s*(?:<\|?(?:im_start|system|assistant)\|?>|\[/?(?:INST|SYSTEM)\])",
            re.IGNORECASE,
        ),
        Severity.HIGH,
    ),
    (
        "tool_coercion",
        re.compile(
            r"\b(?:call|invoke|run|use)\b[^.\n]{0,25}\b(?:tool|function|api)\b[^.\n]{0,40}"
            r"\b(?:send|email|post|upload|transfer|exfiltrat\w*)\b",
            re.IGNORECASE,
        ),
        Severity.CRITICAL,
    ),
    (
        "exfil_instruction",
        re.compile(
            r"\b(?:send|post|forward|upload|leak)\b[^.\n]{0,40}"
            r"\b(?:to|at)\b\s+https?://|\bbase64\b[^.\n]{0,30}\b(?:encode|then send)\b",
            re.IGNORECASE,
        ),
        Severity.CRITICAL,
    ),
)


def find_injection(text: str) -> Iterator[Detection]:
    for kind, pattern, severity in _INJECTION_PATTERNS:
        for m in pattern.finditer(text):
            yield Detection(kind, m.start(), m.end(), m.group(0)[:120], severity)


def find_urls(text: str) -> Iterator[Detection]:
    for m in _URL.finditer(text):
        host = m.group(1).split("@")[-1].split(":")[0].lower()
        yield Detection("url", m.start(), m.end(), host, Severity.LOW)


def find_markdown_exfil(text: str) -> Iterator[Detection]:
    for m in _MD_EXFIL.finditer(text):
        yield Detection("markdown_exfil", m.start(), m.end(), m.group(1)[:160], Severity.CRITICAL)


def redact(text: str, detections: list[Detection]) -> str:
    """Replace detections with typed placeholders, right to left.

    Right to left so that earlier offsets stay valid. Typed placeholders
    (`[REDACTED:card_number]`) rather than blanket asterisks, because the model
    still needs to know that a card number *was* there in order to produce a
    sensible answer about it.
    """
    out = text
    for d in sorted(detections, key=lambda d: d.start, reverse=True):
        out = f"{out[: d.start]}[REDACTED:{d.kind}]{out[d.end :]}"
    return out
