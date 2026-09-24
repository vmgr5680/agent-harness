"""HTTP surface.

Integration tests against a real harness with in-memory ADK sessions, not
mocks of the layer below. They are fast because nothing needs a network.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

pytest.importorskip("fastapi", reason="install with pip install '.[api]'")
from fastapi.testclient import TestClient

from agent_harness.api import app as app_module

TOKEN_ACME = "dev-token-acme"
TOKEN_GLOBEX = "dev-token-globex"
TOKEN_NOSCOPE = "dev-token-limited"

ENV = {
    "AH_PROVIDER": "offline",
    "AH_LOG_LEVEL": "CRITICAL",
    "AH_LOG_FORMAT": "text",
    "AH_RATE_LIMIT_RPM": "0",
    "AH_ENV": "dev",
    "AH_SESSION_DB_URL": "",
    "AH_SERVICE_TOKENS": (
        f"{TOKEN_ACME}:acme:agent.run|evals.run|kb.read|orders.read|orders.write,"
        f"{TOKEN_GLOBEX}:globex:agent.run|kb.read|orders.read,"
        f"{TOKEN_NOSCOPE}:acme:kb.read"
    ),
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    for key in list(os.environ):
        if key.startswith(("AH_", "GOOGLE_API", "GEMINI_API")):
            monkeypatch.delenv(key, raising=False)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    with TestClient(app_module.app) as c:
        yield c


def auth(token: str = TOKEN_ACME) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def run(client, question: str, token: str = TOKEN_ACME, **params):
    return client.post("/v1/agent/run", json={"question": question, **params}, headers=auth(token))


# --- operations --------------------------------------------------------------


def test_healthz_touches_no_dependency(client):
    """A liveness probe that calls the model provider takes you down with it."""
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_reports_the_engine(client):
    body = client.get("/readyz").json()
    assert body["status"] == "ok"
    assert body["engine"] == "google-adk"


def test_metrics_are_prometheus_text(client):
    run(client, "What is the refund window?")
    assert "ah_runs_total" in client.get("/metrics").text


# --- auth --------------------------------------------------------------------


def test_a_missing_token_is_rejected(client):
    assert client.post("/v1/agent/run", json={"question": "hi"}).status_code == 401


def test_an_unknown_token_is_rejected(client):
    assert run(client, "hi", token="nope-nope-nope").status_code == 401


def test_a_token_without_the_scope_is_forbidden(client):
    assert run(client, "hi", token=TOKEN_NOSCOPE).status_code == 403


def test_a_caller_cannot_widen_its_own_scopes(client):
    """globex holds no orders.write, so asking for it must not grant it."""
    r = run(
        client,
        "Refund order ORD-10021 for 249 dollars",
        token=TOKEN_GLOBEX,
        scopes=["kb.read", "orders.read", "orders.write"],
    )
    assert r.status_code == 200
    assert r.json()["status"] != "needs_approval"


# --- running -----------------------------------------------------------------


def test_a_run_returns_an_answer_an_id_and_a_cost(client):
    body = run(client, "What is the refund window?").json()
    assert body["status"] == "completed"
    assert body["run_id"] and body["session_id"]
    assert body["cost_usd"] > 0
    assert body["tool_calls"] is None, "tool detail must be opt-in"


def test_tool_detail_is_available_on_request(client):
    r = client.post(
        "/v1/agent/run?include_tools=true",
        json={"question": "What is the refund window?"},
        headers=auth(),
    )
    assert r.json()["tool_calls"]


def test_an_oversized_question_is_rejected_by_validation(client):
    assert run(client, "x" * 25_000).status_code == 422


def test_a_blocked_request_returns_200_with_a_blocked_status(client):
    """A guardrail block is a normal outcome of a working system, not a 5xx."""
    r = run(client, "my key is AIzaSyB1c2D3e4F5g6H7i8J9k0L1m2N3o4P5q6R")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "blocked"
    assert body["cost_usd"] == 0.0


def test_personal_data_from_a_tool_never_reaches_the_response(client):
    body = run(client, "What is the status of order ORD-10021?").json()
    assert "4242 4242 4242 4242" not in body["answer"]


# --- approvals ---------------------------------------------------------------


def test_the_approval_flow_end_to_end(client):
    started = run(client, "Refund order ORD-10021 for 249 dollars").json()
    assert started["status"] == "needs_approval"
    approval = started["approval"]
    assert approval["tool"] == "issue_refund"

    decided = client.post(
        "/v1/approvals/decision",
        json={
            "approve": True,
            "question": "Refund order ORD-10021 for 249 dollars",
            "session_id": approval["session_id"],
            "function_call_id": approval["function_call_id"],
            "approval_id": approval["approval_id"],
            "tool": approval["tool"],
        },
        headers=auth(),
    )
    assert decided.status_code == 200
    assert "issue_refund" in decided.json()["tools_executed"]


def test_another_tenant_cannot_decide_your_approval(client):
    """The approval is bound to a session, and the session to a tenant.
    Without that check an approval id from a log file would be enough."""
    started = run(client, "Refund order ORD-10021 for 249 dollars").json()
    approval = started["approval"]
    r = client.post(
        "/v1/approvals/decision",
        json={
            "approve": True,
            "question": "Refund order ORD-10021 for 249 dollars",
            "session_id": approval["session_id"],
            "function_call_id": approval["function_call_id"],
            "approval_id": approval["approval_id"],
            "tool": approval["tool"],
        },
        headers=auth(TOKEN_GLOBEX),
    )
    assert r.status_code == 404


# --- sessions and audit ------------------------------------------------------


def test_the_session_endpoint_returns_the_adk_event_trail(client):
    started = run(client, "What is the refund window?").json()
    body = client.get(f"/v1/sessions/{started['session_id']}", headers=auth()).json()
    assert body["events"]
    assert any(e["tool_calls"] for e in body["events"])


def test_one_tenant_cannot_read_another_tenants_session(client):
    started = run(client, "What is the refund window?").json()
    sid = started["session_id"]
    assert client.get(f"/v1/sessions/{sid}", headers=auth()).status_code == 200
    # A foreign session is indistinguishable from a missing one.
    assert client.get(f"/v1/sessions/{sid}", headers=auth(TOKEN_GLOBEX)).status_code == 404


# --- other endpoints ---------------------------------------------------------


def test_tools_advertise_their_governance(client):
    tools = client.get("/v1/tools", headers=auth()).json()["tools"]
    refund = next(t for t in tools if t["name"] == "issue_refund")
    assert refund["needs_approval"] is True
    assert refund["scopes"] == ["orders.write"]


def test_tool_visibility_reflects_the_callers_scopes(client):
    tools = client.get("/v1/tools", headers=auth(TOKEN_GLOBEX)).json()["tools"]
    refund = next(t for t in tools if t["name"] == "issue_refund")
    assert refund["callable_by_you"] is False


def test_evals_can_be_run_over_http(client):
    r = client.post("/v1/evals/run?path=evals/support.jsonl", headers=auth())
    body = r.json()
    assert r.status_code == 200
    assert body["total"] >= 8
    assert body["gate_passed"] is True


def test_a_missing_eval_suite_is_a_400_not_a_500(client):
    assert client.post("/v1/evals/run?path=nope.jsonl", headers=auth()).status_code == 400


def test_evals_require_their_own_scope(client):
    assert client.post("/v1/evals/run", headers=auth(TOKEN_GLOBEX)).status_code == 403
