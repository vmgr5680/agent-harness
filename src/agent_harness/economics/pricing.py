"""Inference economics: turning tokens into dollars.

Read this warning before you trust a number that comes out of this file
------------------------------------------------------------------------
The rates below are a **configuration default, not a source of truth**. Model
prices change, and they change without asking you. Two consequences:

1. The table records `LAST_VERIFIED`. If that date is old, treat every cost
   figure the harness reports as an estimate with unknown error.
2. Everything is overridable at run time via `AH_PRICING_JSON`, so correcting
   a price is a config change and not a deploy.

For real accounting, reconcile against the provider's billing export. This
ledger exists to answer "which agent, which tenant, which step" — a question
the billing export cannot answer — not to be the invoice.

Rates are USD per 1,000,000 tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

LAST_VERIFIED: Final = "2026-09-23"

# Gemini API list pricing for paid tier, USD per 1M tokens.
# `cached_input` is the discounted rate for tokens served from context cache.
_DEFAULT_RATES: Final[dict[str, dict[str, float]]] = {
    # Published on ai.google.dev/gemini-api/docs/pricing (checked 2026-09-23):
    # text input $0.25, output $1.50 "including thinking tokens", cache $0.025.
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50, "cached_input": 0.025},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "cached_input": 0.31},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cached_input": 0.075},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "cached_input": 0.025},
    # The offline model. Priced at the cheapest real tier rather than at zero,
    # deliberately: it means the cost ledger, the per-run budget ceiling and the
    # eval suite's cost-regression assertions are all exercised on every offline
    # run, for free. A zero here would leave that whole path untested until the
    # first time somebody pointed it at a real model.
    "offline-1": {"input": 0.10, "output": 0.40, "cached_input": 0.025},
}

# What to charge when a model id is not in the table. Charging zero would hide
# the spend; charging the most expensive known rate makes an unknown model
# loud rather than silent, which is the behaviour you want from a budget.
_FALLBACK: Final[dict[str, float]] = {"input": 1.25, "output": 10.00, "cached_input": 0.31}


@dataclass(frozen=True, slots=True)
class PriceBook:
    rates: dict[str, dict[str, float]]
    last_verified: str = LAST_VERIFIED

    @classmethod
    def build(cls, overrides: dict[str, dict[str, float]] | None = None) -> PriceBook:
        merged = {model: dict(rates) for model, rates in _DEFAULT_RATES.items()}
        for model, rates in (overrides or {}).items():
            merged.setdefault(model, dict(_FALLBACK)).update(rates)
        return cls(rates=merged)

    def known(self, model: str) -> bool:
        return model in self.rates

    def rate(self, model: str) -> dict[str, float]:
        return self.rates.get(model, _FALLBACK)

    def cost(
        self, model: str, *, input_tokens: int, output_tokens: int, cached_tokens: int = 0
    ) -> float:
        """Cost of one call in USD.

        `cached_tokens` is the subset of `input_tokens` that the provider served
        from its context cache. They are billed at the cheaper rate, so they are
        subtracted from the full-price count rather than added on top — getting
        this backwards is the most common way a cost dashboard lies.
        """
        r = self.rate(model)
        billable_input = max(0, input_tokens - cached_tokens)
        total = (
            billable_input * r["input"]
            + cached_tokens * r.get("cached_input", r["input"])
            + output_tokens * r["output"]
        ) / 1_000_000
        return round(total, 8)

    def estimate_tokens(self, text: str) -> int:
        """A rough token count for pre-flight budget checks only.

        Four characters per token is the usual English approximation. It is
        wrong for code, wrong for non-Latin scripts, and good enough to refuse
        a request that would obviously blow the budget before paying to find
        out. Never report this number as usage — use the provider's count.
        """
        return max(1, len(text) // 4)
