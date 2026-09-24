"""FastAPI service.

    POST /v1/agent/run                 run an agent
    POST /v1/approvals/decision        approve or reject a suspended write
    GET  /v1/sessions/{id}             the ADK session and its events (audit)
    GET  /v1/tools                     tools and their governance
    GET  /v1/models                    models this key can call
    POST /v1/evals/run                 run the governance eval suite
    GET  /healthz /readyz /metrics     operations

Why a thin service in front of ADK
----------------------------------
ADK ships `adk api_server`, and for a local UI it is the right thing to use.
It is not the right thing to expose to other systems, because it has no notion
of *your* tenants, *your* scopes or *your* spend limits — the caller names the
agent and the session and is trusted. This service exists to resolve a bearer
token into a tenant and a scope set before anything reaches the runtime, and
to return a result shaped for a caller rather than an event stream.

Three operational decisions worth copying
-----------------------------------------
**Liveness and readiness answer different questions.** `/healthz` must never
touch a dependency — a health check that calls the model provider takes your
service down when the provider has a bad minute.

**A guardrail block is a 200.** It is the safety system working, not an error.
Returning 5xx means your on-call is paged every time the product does its job.

**A foreign resource is a 404, not a 403.** A 403 confirms the resource exists.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse

from ..agents.models import list_available_models
from ..config import Principal, Settings, load_settings
from ..errors import HarnessError
from ..evals.dataset import EvalSuite
from ..evals.runner import EvalRunner
from ..observability import log, new_id
from ..runtime.harness import AgentHarness
from ..runtime.result import ApprovalRequest, RunRequest
from ..tools.adk_tools import TOOL_SCOPES, TOOL_TAGS, WRITE_TOOLS, build_tools
from .schemas import ApprovalDecisionModel, RunRequestModel, RunResponseModel

_LOG = logging.getLogger("agent_harness.api")

_state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    _state["settings"] = settings
    _state["harness"] = AgentHarness(settings)
    log(_LOG, logging.INFO, "api.startup", **settings.redacted())
    try:
        yield
    finally:
        _state["harness"].close()
        log(_LOG, logging.INFO, "api.shutdown")


app = FastAPI(
    title="agent-harness",
    version="0.2.0",
    summary="Governance in front of a Google ADK agent: guardrails, scopes, cost, approvals.",
    lifespan=lifespan,
)


def harness() -> AgentHarness:
    instance = cast("AgentHarness | None", _state.get("harness"))
    if instance is None:  # pragma: no cover - only before startup
        raise HTTPException(status_code=503, detail="harness is not ready")
    return instance


def settings() -> Settings:
    return cast("Settings", _state["settings"])


def principal(
    authorization: Annotated[str | None, Header()] = None,
    cfg: Settings = Depends(settings),
) -> Principal:
    """Resolve a bearer token to a Principal.

    A development-grade credential store — see config.py. What should survive
    the swap to a real identity provider is the shape: the caller never states
    its own tenant or its own scopes.
    """
    if not cfg.service_tokens:
        if cfg.env != "dev":
            raise HTTPException(status_code=500, detail="AH_SERVICE_TOKENS is not configured")
        return Principal(
            tenant_id="local",
            token_id="local",
            scopes=frozenset({"agent.run", "evals.run", "kb.read", "orders.read", "orders.write"}),
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    found = cfg.principal_for(authorization.split(None, 1)[1].strip())
    if found is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return found


def require(scope: str) -> Callable[..., Principal]:
    def _dep(who: Principal = Depends(principal)) -> Principal:
        if not who.has(scope):
            raise HTTPException(status_code=403, detail=f"missing scope {scope}")
        return who

    return _dep


# --- middleware and errors ---------------------------------------------------


@app.middleware("http")
async def add_request_id(request: Request, call_next: Any) -> Response:
    request_id = request.headers.get("x-request-id") or new_id(12)
    response: Response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


@app.exception_handler(HarnessError)
async def harness_error_handler(request: Request, exc: HarnessError) -> JSONResponse:
    http_status = {
        "budget_exceeded": 402,
        "rate_limited": 429,
        "guardrail_blocked": 422,
        "tool_permission_denied": 403,
        "config_error": 500,
    }.get(exc.code, 500)
    log(_LOG, logging.WARNING, "api.harness_error", code=exc.code, path=request.url.path)
    return JSONResponse(
        status_code=http_status,
        content={
            "error": exc.message if exc.user_safe else "the request could not be completed",
            "code": exc.code,
            "detail": exc.detail if exc.user_safe else None,
        },
    )


@app.exception_handler(Exception)
async def unexpected_handler(request: Request, exc: Exception) -> JSONResponse:
    ref = new_id(12)
    _LOG.exception("api.unhandled", extra={"extra_fields": {"path": request.url.path, "ref": ref}})
    return JSONResponse(
        status_code=500,
        content={"error": "internal error", "code": "internal_error", "run_id": ref},
        headers={"x-error-ref": ref},
    )


# --- endpoints ---------------------------------------------------------------


def _to_model(result: Any, *, include_tools: bool) -> RunResponseModel:
    payload = result.as_dict()
    return RunResponseModel(
        run_id=payload["run_id"],
        session_id=payload["session_id"],
        status=payload["status"],
        answer=payload["answer"],
        agent=payload["agent"],
        citations=payload["citations"],
        model_calls=payload["model_calls"],
        tools_executed=payload["tools_executed"],
        denied_tools=payload["denied_tools"],
        cost_usd=payload["cost_usd"],
        duration_ms=payload["duration_ms"],
        approval=payload["approval"],
        guardrail_findings=payload["guardrail_findings"],
        tool_calls=payload["tool_calls"] if include_tools else None,
    )


@app.post("/v1/agent/run", response_model=RunResponseModel)
async def run_agent(
    body: RunRequestModel,
    include_tools: bool = False,
    who: Principal = Depends(require("agent.run")),
    h: AgentHarness = Depends(harness),
) -> RunResponseModel:
    requested = frozenset(body.scopes) if body.scopes else who.scopes
    result = await h.run_async(
        RunRequest(
            question=body.question,
            tenant_id=who.tenant_id,
            user_id=who.tenant_id,
            session_id=body.session_id,
            agent=body.agent,
            # Narrowing only. The token's scopes are the ceiling.
            scopes=frozenset(requested & who.scopes),
            allowed_domains=frozenset(body.allowed_domains),
            metadata={**body.metadata, "token_id": who.token_id},
        )
    )
    return _to_model(result, include_tools=include_tools)


@app.post("/v1/approvals/decision", response_model=RunResponseModel)
async def decide_approval(
    body: ApprovalDecisionModel,
    who: Principal = Depends(require("agent.run")),
    h: AgentHarness = Depends(harness),
) -> RunResponseModel:
    """Resume a suspended run with a human's decision.

    The approval is bound to a session, and the session is bound to a tenant,
    so a caller can only decide approvals on its own runs. The check is the
    session lookup below — without it, an approval id from a log file would be
    enough to authorise somebody else's refund.
    """
    session = await h.session_service.get_session(
        app_name=h.settings.service_name,
        user_id=who.tenant_id,
        session_id=body.session_id,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="no such session")

    request = RunRequest(
        question=body.question,
        tenant_id=who.tenant_id,
        user_id=who.tenant_id,
        session_id=body.session_id,
        agent=body.agent,
        scopes=who.scopes,
    )
    approval = ApprovalRequest(
        approval_id=body.approval_id,
        function_call_id=body.function_call_id,
        session_id=body.session_id,
        tool=body.tool,
    )
    log(
        _LOG,
        logging.INFO,
        "api.approval_decided",
        approval=body.approval_id,
        tool=body.tool,
        approve=body.approve,
        by=who.token_id,
    )
    result = await h.approve_async(request, approval, approved=body.approve)
    return _to_model(result, include_tools=True)


@app.get("/v1/sessions/{session_id}")
async def get_session(
    session_id: str,
    who: Principal = Depends(require("agent.run")),
    h: AgentHarness = Depends(harness),
) -> dict[str, Any]:
    """The ADK session and its events — the audit trail.

    ADK persists every event of every run: the model's requests, the tool
    calls, the responses. There is no second audit log in this project,
    deliberately: a second one would drift from the first.
    """
    session = await h.session_service.get_session(
        app_name=h.settings.service_name, user_id=who.tenant_id, session_id=session_id
    )
    if session is None:
        raise HTTPException(status_code=404, detail="no such session")
    return {
        "session_id": session.id,
        "tenant_id": who.tenant_id,
        "last_update_time": session.last_update_time,
        "events": [
            {
                "id": e.id,
                "author": e.author,
                "timestamp": e.timestamp,
                "tool_calls": [c.name for c in e.get_function_calls()],
                "text": "".join(p.text or "" for p in (e.content.parts or []))[:2000]
                if e.content
                else "",
            }
            for e in session.events
        ],
    }


@app.get("/v1/tools")
def list_tools(who: Principal = Depends(principal)) -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": name,
                "description": (tool.description or "").split("\n")[0],
                "scopes": sorted(TOOL_SCOPES.get(name, frozenset())),
                "tags": sorted(TOOL_TAGS.get(name, frozenset())),
                "needs_approval": name in WRITE_TOOLS,
                "callable_by_you": TOOL_SCOPES.get(name, frozenset()) <= who.scopes,
            }
            for name, tool in sorted(build_tools().items())
        ]
    }


@app.get("/v1/models")
def list_models(
    who: Principal = Depends(require("agent.run")), cfg: Settings = Depends(settings)
) -> dict[str, Any]:
    try:
        return {"configured": cfg.model_deep, "available": list_available_models(cfg)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"could not list models: {exc}") from exc


@app.post("/v1/evals/run")
def run_evals(
    path: str = "evals/support.jsonl",
    min_pass_rate: float = 0.9,
    who: Principal = Depends(require("evals.run")),
    h: AgentHarness = Depends(harness),
) -> dict[str, Any]:
    try:
        suite = EvalSuite.from_jsonl(path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"cannot load suite: {exc}") from exc
    report = EvalRunner(h).run(suite)
    ok, problems = report.gate(min_pass_rate=min_pass_rate)
    return {"gate_passed": ok, "problems": problems, **report.as_dict()}


# --- operations --------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness. Deliberately touches nothing."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz(response: Response, h: AgentHarness = Depends(harness)) -> dict[str, Any]:
    health = h.health()
    if not health.get("model"):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        health["status"] = "degraded"
    return health


@app.get("/metrics", response_class=PlainTextResponse)
def metrics(h: AgentHarness = Depends(harness)) -> str:
    """Prometheus text exposition. ADK's OpenTelemetry spans go to a collector;
    these are the business counters that belong on an alert."""
    return h.metrics.prometheus()
