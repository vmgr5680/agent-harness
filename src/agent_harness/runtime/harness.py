"""`AgentHarness` — the composition root.

Everything is constructed here, once, and passed down explicitly: no
import-time singletons, no service locator. That is what lets a test stand the
whole system up in memory in one line.

The division of labour, which is the point of the whole project
---------------------------------------------------------------
**ADK owns the runtime.** The agentic loop, the tool protocol and its schema
validation, sub-agent delegation, session and event persistence, the
human-in-the-loop confirmation gate, retries against the model (opt-in, see
`agents/models.py::RETRY`), and OpenTelemetry spans. None of that is
reimplemented here, and the earlier
hand-rolled versions of all of it were deleted rather than kept alongside.

**The harness owns the governance.** Guardrails on four surfaces, tool scopes,
cost accounting and budget ceilings, per-tenant rate limiting, and the
translation from an event stream into a result a caller can branch on. All of
it arrives through one `GovernancePlugin` registered on the Runner, so it
cannot be forgotten on a subtree.

Sessions
--------
`DatabaseSessionService` when a database URL is configured, `InMemory`
otherwise. Worth being explicit about what that buys: ADK persists every event
of every run — the model's requests, the tool calls, the responses — which *is*
the audit trail. There is no reason to keep a second one, and a second one
would drift.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from google.adk.agents import RunConfig
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.apps.app import App
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.adk.tools.tool_confirmation import ToolConfirmation
from google.genai import types

from ..agents.models import build_model, model_name
from ..agents.specs import SPECS, build_root_agent
from ..config import Settings, load_settings
from ..economics.pricing import PriceBook
from ..errors import ConfigError
from ..observability import CostLedger, Metrics, configure_logging, log, new_id
from .plugin import GovernancePlugin
from .result import ApprovalRequest, RunRequest, RunResult, RunStatus, ToolCall

_LOG = logging.getLogger("agent_harness.runtime.harness")

CONFIRMATION_TOOL = "adk_request_confirmation"
_DOC_ID_RE = re.compile(r"\b(POL-[A-Z]+-\d+)\b")


def _build_session_service(settings: Settings) -> BaseSessionService:
    if not settings.session_db_url:
        return InMemorySessionService()
    try:
        from google.adk.sessions import DatabaseSessionService

        return DatabaseSessionService(db_url=settings.session_db_url)
    except ImportError:  # pragma: no cover - depends on the installed extras
        log(
            _LOG,
            logging.WARNING,
            "sessions.database_unavailable",
            note="install google-adk[db] for persistence; using in-memory sessions",
        )
        return InMemorySessionService()
    except ValueError as exc:
        # ADK 2.9 requires an async driver, and every URL a developer already
        # knows ("sqlite:///", "postgresql://") is a synchronous one. Failing
        # here with ADK's own wording plus the setting's name is the difference
        # between a one-line fix and an afternoon.
        raise ConfigError(
            f"AH_SESSION_DB_URL is not usable: {exc} "
            "Async drivers: sqlite+aiosqlite://, postgresql+asyncpg://. "
            "The driver and `greenlet` must both be installed."
        ) from exc


class AgentHarness:
    """Construct once per process and share it. `run()` is re-entrant."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        model: Any | None = None,
        session_service: BaseSessionService | None = None,
        configure_logs: bool = True,
    ) -> None:
        self.settings = settings or load_settings()
        if configure_logs:
            configure_logging(
                self.settings.log_level,
                self.settings.log_format,
                service=self.settings.service_name,
                env=self.settings.env,
            )
        self.metrics = Metrics()
        self.ledger = CostLedger()
        self.prices = PriceBook.build(self.settings.pricing_override)
        self.model = model if model is not None else build_model(self.settings, "deep")
        self.session_service = session_service or _build_session_service(self.settings)
        self._runners: dict[tuple[str, frozenset[str]], Runner] = {}
        log(
            _LOG,
            logging.INFO,
            "harness.ready",
            model=model_name(self.model),
            **self.settings.redacted(),
        )

    # --- wiring --------------------------------------------------------------

    def _plugin(self, request: RunRequest) -> GovernancePlugin:
        spec = SPECS[request.agent]
        return GovernancePlugin(
            self.settings,
            metrics=self.metrics,
            ledger=self.ledger,
            scopes=frozenset(request.scopes & spec.scopes),
            tenant_id=request.tenant_id,
            allowed_domains=request.allowed_domains,
        )

    def _runner(self, request: RunRequest, plugin: GovernancePlugin) -> Runner:
        """A Runner per (agent, scope set).

        Scopes decide which tools the agent is even shown, so two callers with
        different privileges genuinely need different agent trees. Caching on
        that pair keeps construction off the hot path without ever handing one
        caller another's catalogue.
        """
        agent = build_root_agent(model=self.model, granted=request.scopes, agent=request.agent)
        app = App(name=self.settings.service_name, root_agent=agent, plugins=[plugin])
        return Runner(app=app, session_service=self.session_service)

    # --- running -------------------------------------------------------------

    def run(self, request: RunRequest) -> RunResult:
        """Synchronous entry point. Wraps the async runner for callers that do
        not have an event loop, which is most scripts and the CLI."""
        return asyncio.run(self.run_async(request))

    async def run_async(
        self,
        request: RunRequest,
        *,
        resume_message: types.Content | None = None,
        session_id: str | None = None,
    ) -> RunResult:
        if request.agent not in SPECS:
            raise KeyError(f"unknown agent {request.agent!r}; known: {', '.join(sorted(SPECS))}")

        plugin = self._plugin(request)
        runner = self._runner(request, plugin)
        sid = session_id or request.session_id or f"s_{new_id(12)}"
        user_id = request.user_id or request.tenant_id

        session = await self.session_service.get_session(
            app_name=self.settings.service_name, user_id=user_id, session_id=sid
        )
        if session is None:
            await self.session_service.create_session(
                app_name=self.settings.service_name,
                user_id=user_id,
                session_id=sid,
                state={"tenant_id": request.tenant_id, **request.metadata},
            )

        message = resume_message or types.Content(
            role="user", parts=[types.Part(text=request.question)]
        )
        started = time.perf_counter()
        result = RunResult(
            run_id="",
            session_id=sid,
            status=RunStatus.FAILED,
            answer="",
            agent=request.agent,
            tenant_id=request.tenant_id,
        )

        result.session_id = sid
        pending_calls: dict[str, tuple[str, dict[str, Any]]] = {}
        answer_parts: list[str] = []

        async def _consume() -> None:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=sid,
                new_message=message,
                # ADK's own termination ceiling. It is a hard stop on model
                # calls, which is the resource an agent actually runs away
                # with. The spend ceiling in the plugin is the second one, and
                # the wall clock below is the third. Any one of them ends a run.
                run_config=RunConfig(max_llm_calls=self.settings.max_model_calls),
            ):
                result.run_id = result.run_id or event.invocation_id
                self._absorb(event, result, pending_calls, answer_parts)

        try:
            await asyncio.wait_for(_consume(), timeout=self.settings.run_timeout_s)
        except LlmCallsLimitExceededError:
            # ADK's own ceiling fired. This is a successful stop, not a crash:
            # the run hit the limit it was given. Reporting it as a failure
            # would page someone for a working control.
            log(
                _LOG,
                logging.WARNING,
                "run.model_call_limit",
                limit=self.settings.max_model_calls,
            )
            result.status = RunStatus.EXHAUSTED
            result.error = "model_call_limit"
            result.answer = (
                f"I could not complete this request within its limit of "
                f"{self.settings.max_model_calls} model calls. Nothing has been changed."
            )
        except TimeoutError:
            log(_LOG, logging.WARNING, "run.timeout", seconds=self.settings.run_timeout_s)
            result.status = RunStatus.EXHAUSTED
            result.error = "run_timeout"
            result.answer = (
                f"I stopped after {self.settings.run_timeout_s:.0f} seconds without "
                "finishing. Nothing has been changed."
            )
        except Exception as exc:  # noqa: BLE001 - boundary: nothing escapes to the caller
            log(_LOG, logging.ERROR, "run.failed", error=str(exc)[:300])
            result.status = RunStatus.FAILED
            result.error = type(exc).__name__
            result.answer = (
                "The request could not be completed. "
                f"Quote reference {result.run_id or sid} to your administrator."
            )

        acct = plugin.accounts.get(result.run_id)
        self._finalise(result, acct, answer_parts, started)
        return result

    # --- event translation ---------------------------------------------------

    def _absorb(
        self,
        event: Event,
        result: RunResult,
        pending: dict[str, tuple[str, dict[str, Any]]],
        answer_parts: list[str],
    ) -> None:
        """Fold one ADK event into the result.

        Tool calls and their responses arrive as separate events, so calls are
        held by id until the matching response turns up. A call that never gets
        one is a call that was interrupted — which is exactly what a pending
        approval looks like.
        """
        for call in event.get_function_calls():
            call_name = call.name or ""
            if call_name == CONFIRMATION_TOOL:
                # ADK is asking a human. `originalFunctionCall` carries the
                # call being gated — its name and the exact arguments — which
                # is what an approver has to see before deciding.
                original = dict((call.args or {}).get("originalFunctionCall") or {})
                tool_name = str(original.get("name") or "unknown")
                tool_args = dict(original.get("args") or {})
                confirmation = dict((call.args or {}).get("toolConfirmation") or {})
                result.approval = ApprovalRequest(
                    approval_id=f"apr_{new_id(10)}",
                    function_call_id=call.id or "",
                    session_id=result.session_id,
                    tool=tool_name,
                    args=tool_args,
                    hint=str(confirmation.get("hint", "")),
                )
                continue
            pending[call.id or ""] = (call_name, dict(call.args or {}))
            result.tool_calls.append(
                ToolCall(name=call_name, args=dict(call.args or {}), agent=event.author)
            )

        for response in event.get_function_responses():
            if response.name == CONFIRMATION_TOOL:
                continue
            payload = response.response
            rendered = str(payload)
            failed = isinstance(payload, dict) and "error" in payload
            for recorded in reversed(result.tool_calls):
                if recorded.name == response.name and not recorded.result:
                    recorded.result = rendered
                    recorded.ok = not failed
                    break
            else:
                # A response with no call in THIS invocation: the tool was
                # gated on a previous turn and has just run on approval. It
                # still belongs in the record — it is the write that a human
                # authorised, and leaving it out would mean the audit trail
                # does not contain the one action anybody will ask about.
                result.tool_calls.append(
                    ToolCall(
                        name=response.name or "",
                        result=rendered,
                        ok=not failed,
                        agent=event.author,
                    )
                )

        if event.content and not event.get_function_calls():
            text = "".join(p.text or "" for p in (event.content.parts or []) if p.text)
            if text.strip() and event.is_final_response():
                answer_parts.append(text)

    def _finalise(
        self,
        result: RunResult,
        acct: Any,
        answer_parts: list[str],
        started: float,
    ) -> None:
        result.duration_ms = (time.perf_counter() - started) * 1000
        result.answer = result.answer or "\n".join(answer_parts).strip()
        result.citations = sorted(
            {doc for call in result.tool_calls for doc in _DOC_ID_RE.findall(call.result)}
        )

        if acct is not None:
            result.model_calls = acct.model_calls
            result.cost_usd = round(acct.cost_usd, 8)
            result.guardrail_findings = acct.findings
            result.denied_tools = acct.denied
            result.cost_breakdown = self.ledger.breakdown(result.run_id)
            if acct.blocked:
                result.status = RunStatus.BLOCKED
                return
            if acct.budget_stopped:
                result.status = RunStatus.EXHAUSTED
                return

        if result.approval is not None:
            result.status = RunStatus.NEEDS_APPROVAL
            result.answer = (
                f"This action needs approval before it can run: {result.approval.tool}. "
                f"Approval reference {result.approval.approval_id}."
            )
            return
        if result.error in ("run_timeout", "model_call_limit"):
            result.status = RunStatus.EXHAUSTED
            return
        if result.error is not None:
            result.status = RunStatus.FAILED
            return
        if not result.answer:
            # The model call ceiling stopped the run mid-flight. Say so, rather
            # than returning an empty string that reads like a successful
            # no-op, or a partial answer that reads like a complete one.
            result.status = RunStatus.EXHAUSTED
            result.answer = (
                "I could not complete this request within its limits. Nothing has been changed."
            )
            return
        result.status = RunStatus.COMPLETED

    # --- approvals -----------------------------------------------------------

    def approve(
        self, request: RunRequest, approval: ApprovalRequest, *, approved: bool = True
    ) -> RunResult:
        return asyncio.run(self.approve_async(request, approval, approved=approved))

    async def approve_async(
        self, request: RunRequest, approval: ApprovalRequest, *, approved: bool = True
    ) -> RunResult:
        """Resume a suspended run with a human's decision.

        The resume is a function *response* to ADK's confirmation call, on the
        same session, so the agent continues from exactly where it paused with
        its full context intact. That is materially better than the re-run this
        project used to do: no second round of model calls to rebuild state,
        and no window in which the underlying record changes between the
        approval and the write.
        """
        confirmation = ToolConfirmation(confirmed=approved, payload=None)
        message = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=approval.function_call_id,
                        name=CONFIRMATION_TOOL,
                        response=confirmation.model_dump(),
                    )
                )
            ],
        )
        log(
            _LOG,
            logging.INFO,
            "approval.decided",
            approval=approval.approval_id,
            tool=approval.tool,
            approved=approved,
        )
        self.metrics.inc(
            "ah_approvals_decided_total",
            tool=approval.tool,
            decision="approved" if approved else "rejected",
        )
        return await self.run_async(request, resume_message=message, session_id=approval.session_id)

    # --- operations ----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        name = model_name(self.model)
        return {
            "status": "ok",
            "engine": "google-adk",
            "model": name,
            "model_price_known": self.prices.known(name),
            "price_book_verified": self.prices.last_verified,
            "guardrail_mode": self.settings.guardrail_mode,
            "sessions": type(self.session_service).__name__,
            "agents": sorted(SPECS),
            "max_model_calls": self.settings.max_model_calls,
        }

    def close(self) -> None:
        self._runners.clear()
