"""OfflineLlm — a deterministic `BaseLlm` so the whole system runs with no key.

This is the ADK-native replacement for the hand-rolled `EchoProvider`, and it
exists for the same four reasons:

1. **The test suite must not call a paid API.** Tests that cost money get
   deleted; tests that need a key get skipped in CI and rot.
2. **Harness behaviour must be assertable.** "Does the governance plugin redact
   tool output before it reaches the next prompt?" is a question about *your*
   code, and answering it needs a model whose output you control exactly.
3. **Evals need a baseline.** A regression suite that only runs against a
   moving model cannot tell you whether the model changed or your code did.
4. **Anyone can run the demo.** `make demo` works with no key and no network.

Two modes. `script` makes a test dictate the exact sequence of responses. With
no script, a small rule engine plays a plausible tool-using agent — reading the
*actual* tool catalogue from `llm_request.tools_dict`, so a subagent that was
given three tools cannot propose a fourth.

Swapping this for a real model is one string: `model="gemini-2.5-flash-lite"`.
Nothing else in the package changes, which is the property worth having.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Sequence
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

_ORDER_RE = re.compile(r"\bORD-\d{4,}\b", re.IGNORECASE)
_MATH_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*[-+*/]\s*\d+(?:\.\d+)?")
_REFUND_INTENT = re.compile(
    r"\b(?:refund|reimburse|money back)\b[^.\n]{0,40}\bORD-\d{4,}\b|"
    r"\bORD-\d{4,}\b[^.\n]{0,40}\b(?:refund|reimburse)\b",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(
    r"(\d+(?:\.\d{1,2})?)\s*(?:dollars?|usd|\$)|\$\s*(\d+(?:\.\d{1,2})?)", re.IGNORECASE
)
_DOC_ID_RE = re.compile(r"\b(POL-[A-Z]+-\d+)\b")
_POLICY_WORDS = ("policy", "refund", "return", "warranty", "window")

HANDOFF_MARKER = "What the earlier steps found:"
"""Where a workflow step's instruction starts the state it was handed.

A writer at the end of a chain has no tools; what it knows arrives as session
state interpolated into its instruction. The offline model answers from the
text after this marker, so a pattern app reads sensibly with no key.
"""


class OfflineLlm(BaseLlm):
    """Deterministic model. Same input, same output, no network, no cost."""

    model: str = "offline-1"
    script: list[str] = Field(default_factory=list)
    fail_times: int = 0
    _cursor: int = 0

    @staticmethod
    def supported_models() -> list[str]:
        return [r"offline-.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if self.fail_times > 0:
            # Fault injection, so the failure paths get the same coverage as
            # the happy ones. ADK routes this to on_model_error_callback.
            object.__setattr__(self, "fail_times", self.fail_times - 1)
            raise RuntimeError("injected model failure from OfflineLlm")

        part = self._next_part(llm_request)
        prompt_chars = _prompt_chars(llm_request.contents or [])
        out_chars = len(part.text or "") or 64
        yield LlmResponse(
            # Named, so the ledger prices it as `offline-1`. Without this the
            # plugin fell back to the configured Gemini model and billed every
            # offline run at that model's rate — or, while that model was
            # unpriced, at the most expensive known rate.
            model_version=self.model,
            content=types.Content(role="model", parts=[part]),
            # Real token counts, so the cost ledger, the per-run budget and the
            # eval suite's cost assertions are all exercised offline. The
            # four-characters-per-token approximation is the usual English one.
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=max(1, prompt_chars // 4),
                candidates_token_count=max(1, out_chars // 4),
                total_token_count=max(2, (prompt_chars + out_chars) // 4),
            ),
            finish_reason=types.FinishReason.STOP,
        )

    # --- response selection --------------------------------------------------

    def _next_part(self, llm_request: LlmRequest) -> types.Part:
        if self._cursor < len(self.script):
            raw = self.script[self._cursor]
            object.__setattr__(self, "_cursor", self._cursor + 1)
            return _part_from_script(raw)
        return self._reason(llm_request)

    def _reason(self, llm_request: LlmRequest) -> types.Part:
        contents = llm_request.contents or []
        available = set(llm_request.tools_dict or {})
        question = _first_user_text(contents)
        observed = _function_responses(contents)
        used = {name for name, _ in observed}
        lowered = question.lower()

        def can(name: str) -> bool:
            return name in available and name not in used

        if (order := _ORDER_RE.search(question)) and can("order_lookup"):
            return _call("order_lookup", {"order_id": order.group(0).upper()})

        # A state-changing intent, only once the record has been read. This is
        # the branch that exercises ADK's confirmation gate end to end.
        if (
            (order := _ORDER_RE.search(question))
            and _REFUND_INTENT.search(question)
            and "order_lookup" in used
            and can("issue_refund")
            and (amount := _amount(question, observed)) is not None
        ):
            return _call(
                "issue_refund",
                {
                    "order_id": order.group(0).upper(),
                    "amount_usd": amount,
                    "reason": "customer request",
                },
            )

        if (expr := _MATH_RE.search(question)) and can("calculator"):
            return _call("calculator", {"expression": expr.group(0)})

        # Once a state-changing tool has run, the job is done. Continuing to
        # browse the policy corpus after issuing a refund is exactly the kind
        # of aimless extra step a step ceiling exists to catch.
        if "issue_refund" in used:
            return types.Part(text=_compose(question, observed))

        if any(w in lowered for w in _POLICY_WORDS) and can("kb_search"):
            return _call("kb_search", {"query": question[:120], "top_k": 3})

        # A tool-less step in a workflow (a writer) is handed what earlier
        # steps found through its instruction, not through tool results.
        if not observed and not available and (handed := _handed(llm_request)):
            return types.Part(text=_compose_handed(handed))

        return types.Part(text=_compose(question, observed))


# --- helpers -----------------------------------------------------------------


def _call(name: str, args: dict[str, Any]) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def _part_from_script(raw: str) -> types.Part:
    """A script entry is either plain text, or `tool:name {json args}`."""
    if raw.startswith("tool:"):
        import json

        head, _, arg_blob = raw[5:].partition(" ")
        return _call(head.strip(), json.loads(arg_blob) if arg_blob.strip() else {})
    return types.Part(text=raw)


def _prompt_chars(contents: Sequence[types.Content]) -> int:
    total = 0
    for content in contents:
        for part in content.parts or []:
            total += len(part.text or "")
            if part.function_response is not None:
                total += len(str(part.function_response.response))
            if part.function_call is not None:
                total += len(str(part.function_call.args))
    return total


def _first_user_text(contents: Sequence[types.Content]) -> str:
    for content in contents:
        if content.role == "user":
            text = "".join(p.text or "" for p in content.parts or [])
            if text.strip():
                return text.strip()
    return ""


def _function_responses(contents: Sequence[types.Content]) -> list[tuple[str, str]]:
    """(tool_name, rendered_response) in the order the model saw them."""
    out: list[tuple[str, str]] = []
    for content in contents:
        for part in content.parts or []:
            if (fr := part.function_response) is not None and fr.name:
                out.append((fr.name, str(fr.response)))
    return out


def _amount(question: str, observed: list[tuple[str, str]]) -> float | None:
    """Take the amount from the request, falling back to the order total that
    was actually observed. Never invent one."""
    if m := _AMOUNT_RE.search(question):
        return float(m.group(1) or m.group(2))
    for _, body in observed:
        if m := re.search(r"'total_usd':\s*([0-9.]+)", body):
            return float(m.group(1))
    return None


def _handed(llm_request: LlmRequest) -> str:
    """The text after `HANDOFF_MARKER` in the system instruction, if any."""
    instruction = llm_request.config.system_instruction if llm_request.config else None
    if not isinstance(instruction, str) or HANDOFF_MARKER not in instruction:
        return ""
    return instruction.split(HANDOFF_MARKER, 1)[1].strip()


def _compose_handed(handed: str) -> str:
    citations = sorted(set(_DOC_ID_RE.findall(handed)))
    suffix = f" [source: {', '.join(citations)}]" if citations else ""
    return f"Drafted from what the earlier steps found: {handed[:600]}{suffix}"


def _compose(question: str, observed: list[tuple[str, str]]) -> str:
    """Build the answer out of what the tools actually returned, so a grounding
    check has something real to verify."""
    if not observed:
        return (
            f"I could not retrieve anything to support an answer to '{question[:120]}', "
            "so I will not guess."
        )
    joined = " ".join(body[:300] for _, body in observed)
    citations = sorted({doc for _, body in observed for doc in _DOC_ID_RE.findall(body)})
    suffix = f" [source: {', '.join(citations)}]" if citations else ""
    return f"Here is what the systems returned for '{question[:100]}': {joined[:600]}{suffix}"
