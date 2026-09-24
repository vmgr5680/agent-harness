"""Request and response models for the HTTP API.

Two fields carry more weight than the rest:

  `run_id`    returned on every response, including errors. It is the id that
              joins a complaint to the ADK session, the OpenTelemetry trace and
              the log lines.
  `cost_usd`  returned per request, not aggregated monthly. Cost that is only
              visible in a billing export is cost nobody manages.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RunRequestModel(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)
    agent: str = Field(default="support", max_length=64)
    session_id: str | None = Field(default=None, max_length=128)
    # The caller may narrow its own privileges but never widen them: the
    # effective set is (token scopes) ∩ (requested scopes) ∩ (agent scopes).
    scopes: list[str] = Field(default_factory=list, max_length=32)
    allowed_domains: list[str] = Field(default_factory=list, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCallModel(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: str = ""
    ok: bool = True
    agent: str = ""


class ApprovalModel(BaseModel):
    approval_id: str
    function_call_id: str
    session_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    hint: str = ""


class RunResponseModel(BaseModel):
    run_id: str
    session_id: str
    status: str
    answer: str
    agent: str
    citations: list[str] = Field(default_factory=list)
    model_calls: int = 0
    tools_executed: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    duration_ms: float = 0.0
    approval: ApprovalModel | None = None
    guardrail_findings: list[dict[str, Any]] = Field(default_factory=list)
    # Full tool detail is opt-in: it is large, and even redacted it is more
    # than a caller needs by default.
    tool_calls: list[ToolCallModel] | None = None


class ApprovalDecisionModel(BaseModel):
    approve: bool = True
    # Everything needed to resume the suspended run. The caller echoes these
    # back from the original response rather than the server holding request
    # state between calls.
    question: str = Field(min_length=1, max_length=20_000)
    session_id: str = Field(min_length=1, max_length=128)
    function_call_id: str = Field(min_length=1, max_length=128)
    approval_id: str = Field(min_length=1, max_length=128)
    tool: str = Field(min_length=1, max_length=128)
    agent: str = Field(default="support", max_length=64)
    note: str = Field(default="", max_length=500)


class ErrorModel(BaseModel):
    error: str
    code: str
    run_id: str | None = None
    detail: dict[str, Any] | None = None
