"""GovernancePlugin — the harness, as one ADK plugin.

This file is the whole argument of the project in one class. ADK gives you the
agentic loop, the tool protocol, sub-agents, sessions, the confirmation gate
and OpenTelemetry spans. It does not give you any of the following, and all of
it plugs in here, at the Runner, so it applies to every agent in the tree
without a single agent knowing about it:

    on_user_message_callback  → input rails (redact)
    before_run_callback       → input rails (block), budget reset
    before_model_callback     → per-tenant rate limit, spend ceiling
    after_model_callback      → token accounting, cost ledger, OUTPUT rails
    before_tool_callback      → scope enforcement, audit of the attempt
    after_tool_callback       → TOOL-OUTPUT rails (the surface that matters)
    on_model_error_callback   → error taxonomy, metrics
    on_tool_error_callback    → a failed tool becomes an observation
    after_run_callback        → persist the run, emit metrics, clean up

Why a plugin and not per-agent callbacks
----------------------------------------
`LlmAgent` takes `before_model_callback` and friends directly, and for one
agent that is simpler. It is the wrong shape for governance: a three-agent tree
means wiring the same eight callbacks onto three agents and remembering to do
it again on the fourth. Governance that can be forgotten on one agent is not
governance. A plugin is registered once, on the Runner, and cannot be omitted
from a subtree.

The one thing to notice
-----------------------
`after_tool_callback` is where the money is. It is the surface almost every
deployed agent leaves unguarded: not what the user typed, but what the billing
API returned and is about to be pasted into the next prompt. Nobody types a
card number.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from ..config import Settings
from ..economics.pricing import PriceBook
from ..economics.ratelimit import TokenBucket
from ..guardrails.pipeline import GuardrailPipeline, build_pipelines
from ..guardrails.types import Action, GuardrailContext
from ..observability import CostEntry, CostLedger, Metrics, log
from ..tools.adk_tools import TOOL_SCOPES, TRUSTED_OUTPUT, WRITE_TOOLS

_LOG = logging.getLogger("agent_harness.runtime.plugin")

MODEL_UNAVAILABLE = "temp:model_unavailable"
"""Session-state key set when a model call failed during this run."""

BLOCK_MESSAGE = (
    "This request could not be processed because it contained content the security "
    "policy does not allow. If this is unexpected, quote reference {ref} to your "
    "administrator."
)
BUDGET_MESSAGE = (
    "I stopped before completing this request because it reached its cost ceiling. "
    "Nothing has been changed."
)


@dataclass(slots=True)
class RunAccount:
    """Per-invocation state. One of these exists while a run is in flight."""

    invocation_id: str
    tenant_id: str
    scopes: frozenset[str]
    started_at: float = field(default_factory=time.monotonic)
    model_calls: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    tools_attempted: list[str] = field(default_factory=list)
    tools_executed: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    blocked: bool = False
    budget_stopped: bool = False
    approval_required: str | None = None


class GovernancePlugin(BasePlugin):
    """Everything ADK does not do for you, applied to everything ADK does."""

    def __init__(
        self,
        settings: Settings,
        *,
        metrics: Metrics | None = None,
        ledger: CostLedger | None = None,
        scopes: frozenset[str] = frozenset(),
        tenant_id: str = "anonymous",
        allowed_domains: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(name="agent_harness_governance")
        self.settings = settings
        self.metrics: Metrics = metrics or Metrics()
        self.ledger = ledger or CostLedger()
        self.prices = PriceBook.build(settings.pricing_override)
        self.limiter = TokenBucket(
            rate_per_minute=settings.rate_limit_rpm, burst=settings.rate_limit_burst
        )
        self.input_rails: GuardrailPipeline
        self.output_rails: GuardrailPipeline
        self.input_rails, self.output_rails = build_pipelines(settings, metrics=self.metrics)
        self.scopes = scopes
        self.tenant_id = tenant_id
        self.allowed_domains = allowed_domains
        self.accounts: dict[str, RunAccount] = {}

        # Only warn about models that will actually be called. Warning about a
        # configured-but-unused model id on every offline run is noise, and
        # noise is how a warning that matters gets ignored.
        live_models = {settings.model_fast, settings.model_deep} if settings.is_live else set()
        for name in live_models:
            if name and not self.prices.known(name):
                log(
                    _LOG,
                    logging.WARNING,
                    "pricing.unknown_model",
                    model=name,
                    note=(
                        "cost will be estimated at the most expensive known rate. "
                        "Set AH_PRICING_JSON to the real rates."
                    ),
                )

    # --- helpers -------------------------------------------------------------

    def account(self, invocation_id: str) -> RunAccount:
        acct = self.accounts.get(invocation_id)
        if acct is None:
            acct = RunAccount(
                invocation_id=invocation_id, tenant_id=self.tenant_id, scopes=self.scopes
            )
            self.accounts[invocation_id] = acct
        return acct

    @staticmethod
    def _text_of(content: types.Content | None) -> str:
        if content is None:
            return ""
        return "".join(p.text or "" for p in (content.parts or []))

    # --- input rails ---------------------------------------------------------

    async def on_user_message_callback(
        self, *, invocation_context: InvocationContext, user_message: types.Content
    ) -> types.Content | None:
        """Scan what the user sent. Redaction happens here; the block happens
        in before_run_callback, which is the hook that can stop the run."""
        acct = self.account(invocation_context.invocation_id)
        text = self._text_of(user_message)
        if not text.strip():
            return None

        verdict = self.input_rails.run(
            text,
            GuardrailContext(tenant_id=acct.tenant_id, direction="input", surface="user"),
        )
        acct.findings.extend(f.as_dict() for f in verdict.findings)

        if verdict.blocked:
            acct.blocked = True
            self.metrics.inc("ah_input_blocked_total", tenant=acct.tenant_id)
            return None
        if verdict.text != text:
            self.metrics.inc("ah_input_redacted_total", tenant=acct.tenant_id)
            return types.Content(role="user", parts=[types.Part(text=verdict.text)])
        return None

    async def before_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> types.Content | None:
        """Returning Content short-circuits the run — this is the block.

        Note where it sits: *before* any model call. A blocked request costs
        nothing, which is what makes a credential paste cheap as well as safe.
        """
        acct = self.account(invocation_context.invocation_id)
        if acct.blocked:
            ref = invocation_context.invocation_id[:12]
            log(
                _LOG,
                logging.WARNING,
                "guardrail.run_blocked",
                tenant=acct.tenant_id,
                ref=ref,
                reasons=[f["kind"] for f in acct.findings],
            )
            return types.Content(
                role="model", parts=[types.Part(text=BLOCK_MESSAGE.format(ref=ref))]
            )
        return None

    # --- the model call ------------------------------------------------------

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        """Rate limit and spend ceiling. Returning an LlmResponse skips the
        call entirely, which is how a budget refusal avoids paying to discover
        that it would have gone over."""
        acct = self.account(callback_context.invocation_id)

        allowed, retry_after = self.limiter.allow(acct.tenant_id)
        if not allowed:
            self.metrics.inc("ah_rate_limited_total", tenant=acct.tenant_id)
            return _text_response(
                "This tenant is sending requests too quickly. "
                f"Try again in {retry_after:.0f} seconds."
            )

        ceiling = self.settings.budget_usd_per_run
        if ceiling > 0 and acct.cost_usd >= ceiling:
            acct.budget_stopped = True
            self.metrics.inc("ah_budget_refusals_total", scope="run", tenant=acct.tenant_id)
            log(
                _LOG,
                logging.WARNING,
                "budget.exceeded",
                tenant=acct.tenant_id,
                spent_usd=round(acct.cost_usd, 8),
                ceiling_usd=ceiling,
            )
            return _text_response(BUDGET_MESSAGE)

        acct.model_calls += 1
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        """Two jobs: account for what the call cost, and scan what it said.

        The output rails run here rather than at the end of the run so that
        they apply to every agent in the tree, including a subagent whose
        answer the parent will paste into its own context.
        """
        acct = self.account(callback_context.invocation_id)
        self._meter(acct, callback_context, llm_response)

        content = llm_response.content
        if content is None or llm_response.partial:
            return None
        # A response that is asking for a tool is not an answer; there is
        # nothing to show a human yet.
        if any(p.function_call for p in (content.parts or [])):
            return None
        text = self._text_of(content)
        if not text.strip():
            return None

        verdict = self.output_rails.run(
            text,
            GuardrailContext(
                tenant_id=acct.tenant_id,
                direction="output",
                surface="model_output",
                allowed_domains=self.allowed_domains,
            ),
        )
        acct.findings.extend(f.as_dict() for f in verdict.findings)
        if verdict.action is Action.ALLOW and verdict.text == text:
            return None

        self.metrics.inc(
            "ah_output_guardrail_total",
            action=verdict.action.value,
            agent=callback_context.agent_name,
        )
        log(
            _LOG,
            logging.WARNING if verdict.blocked else logging.INFO,
            "guardrail.output",
            agent=callback_context.agent_name,
            action=verdict.action.value,
            reasons=verdict.reasons(),
        )
        return llm_response.model_copy(
            update={"content": types.Content(role="model", parts=[types.Part(text=verdict.text)])}
        )

    def _meter(self, acct: RunAccount, ctx: CallbackContext, response: LlmResponse) -> None:
        usage = response.usage_metadata
        if usage is None:
            return
        model = response.model_version or self.settings.model_deep
        # thoughts are billed as output. Counting only candidates is the most
        # common way a cost report quietly under-reports.
        output_tokens = int(usage.candidates_token_count or 0) + int(
            getattr(usage, "thoughts_token_count", 0) or 0
        )
        input_tokens = int(usage.prompt_token_count or 0)
        cached = int(getattr(usage, "cached_content_token_count", 0) or 0)
        cost = self.prices.cost(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached,
        )
        acct.cost_usd += cost
        acct.input_tokens += input_tokens
        acct.output_tokens += output_tokens
        acct.cached_tokens += cached
        self.ledger.record(
            CostEntry(
                run_id=acct.invocation_id,
                agent=ctx.agent_name,
                tenant_id=acct.tenant_id,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached,
                cost_usd=cost,
                cache_hit=bool(cached),
            )
        )
        self.metrics.inc("ah_llm_cost_usd", cost, model=model, tenant=acct.tenant_id)
        self.metrics.inc("ah_llm_tokens_total", float(input_tokens + output_tokens), model=model)
        self.metrics.inc("ah_llm_calls_total", model=model, agent=ctx.agent_name)

    async def on_model_error_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        error: Exception,
    ) -> LlmResponse | None:
        """A provider failure becomes a user-safe message, not a stack trace."""
        self.metrics.inc(
            "ah_llm_errors_total",
            agent=callback_context.agent_name,
            error=type(error).__name__,
        )
        log(_LOG, logging.ERROR, "model.error", error=type(error).__name__, detail=str(error)[:200])
        # A fact later steps can branch on, so none of them has to infer an
        # outage from the apology below. `temp:` keeps it to this run.
        callback_context.state[MODEL_UNAVAILABLE] = True
        return _text_response(
            "The assistant is temporarily unable to reach its model. Please try again shortly."
        )

    # --- tools ---------------------------------------------------------------

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any] | None:
        """Scope enforcement. Returning a dict skips the tool and hands that
        dict back to the model as the result.

        ADK has no notion of scopes: a tool in an agent's list is callable.
        `tools_for()` already filtered the catalogue, which is a correctness
        and cost control. This is the security control — the check that still
        holds when a tool reaches an agent it should not have.
        """
        acct = self.account(tool_context.invocation_id)
        acct.tools_attempted.append(tool.name)
        required = TOOL_SCOPES.get(tool.name, frozenset())
        if not required <= acct.scopes:
            missing = ", ".join(sorted(required - acct.scopes))
            acct.denied.append(tool.name)
            self.metrics.inc("ah_tool_calls_total", tool=tool.name, outcome="denied")
            log(
                _LOG,
                logging.WARNING,
                "tool.denied",
                tool=tool.name,
                tenant=acct.tenant_id,
                missing_scopes=missing,
            )
            return {
                "error": (
                    f"Permission denied: calling {tool.name} requires the scope(s) "
                    f"{missing}, which this session does not hold. Do not retry it."
                )
            }
        if tool.name in WRITE_TOOLS:
            log(
                _LOG,
                logging.INFO,
                "tool.write_attempted",
                tool=tool.name,
                args=str(tool_args)[:200],
            )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Scan the tool result before it re-enters the prompt.

        This is the hook that matters most in the whole file. A tool response
        is untrusted input on two counts: it carries customer data the user
        never typed, and it is the most likely carrier of an indirect prompt
        injection. By the time it is in the context, it is too late.
        """
        acct = self.account(tool_context.invocation_id)

        # ADK's confirmation gate refuses the call with this exact shape. It is
        # a pause, not a failure, and the run result has to say so.
        if isinstance(result, dict) and "requires confirmation" in str(result.get("error", "")):
            acct.approval_required = tool.name
            self.metrics.inc("ah_approvals_requested_total", tool=tool.name)
            return None

        if isinstance(result, dict) and "error" not in result:
            acct.tools_executed.append(tool.name)
        self.metrics.inc("ah_tool_calls_total", tool=tool.name, outcome="ok")

        if tool.name in TRUSTED_OUTPUT:
            return None

        rendered = str(result)
        verdict = self.input_rails.run(
            rendered,
            GuardrailContext(
                tenant_id=acct.tenant_id,
                direction="input",
                surface="tool_output",
                metadata={"tool": tool.name},
            ),
        )
        acct.findings.extend(f.as_dict() for f in verdict.findings)
        if verdict.blocked:
            self.metrics.inc("ah_tool_output_blocked_total", tool=tool.name)
            log(
                _LOG,
                logging.WARNING,
                "guardrail.tool_output_blocked",
                tool=tool.name,
                reasons=verdict.reasons(),
            )
            return {
                "error": (
                    f"The result from {tool.name} was withheld by a security control "
                    f"({', '.join(verdict.reasons()) or 'policy'}). "
                    "Do not retry this tool with the same arguments."
                )
            }
        if verdict.text != rendered:
            self.metrics.inc("ah_tool_output_redacted_total", tool=tool.name)
            # The model gets the redacted text, and it gets it as a string so
            # the placeholders survive intact.
            return {"result": verdict.text, "_redacted": True}
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict[str, Any] | None:
        """A tool failure is an observation, not an exception.

        Most of the time the model recovers on the next step. Letting the
        exception propagate throws away everything the run has spent so far.
        """
        self.metrics.inc("ah_tool_calls_total", tool=tool.name, outcome=type(error).__name__)
        log(_LOG, logging.ERROR, "tool.error", tool=tool.name, error=str(error)[:200])
        return {"error": f"{tool.name} failed: {type(error).__name__}: {error}"}

    # --- teardown ------------------------------------------------------------

    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        acct = self.accounts.get(invocation_context.invocation_id)
        if acct is None:
            return
        duration_ms = (time.monotonic() - acct.started_at) * 1000
        self.metrics.observe("ah_run_duration_ms", duration_ms, tenant=acct.tenant_id)
        self.metrics.observe("ah_run_model_calls", float(acct.model_calls))
        self.metrics.inc(
            "ah_runs_total",
            tenant=acct.tenant_id,
            status="blocked"
            if acct.blocked
            else "needs_approval"
            if acct.approval_required
            else "exhausted"
            if acct.budget_stopped
            else "completed",
        )
        log(
            _LOG,
            logging.INFO,
            "run.finished",
            tenant=acct.tenant_id,
            model_calls=acct.model_calls,
            cost_usd=round(acct.cost_usd, 8),
            duration_ms=round(duration_ms, 2),
            tools=acct.tools_executed,
        )


def _text_response(text: str) -> LlmResponse:
    """A synthetic model response, used to short-circuit a call."""
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))
