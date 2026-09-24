from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_harness.evals.dataset import (
    EvalCase,
    EvalSuite,
    to_adk_evalset,
    write_adk_evalset,
)
from agent_harness.evals.runner import EvalRunner, compare

SUITE = Path(__file__).resolve().parents[1] / "evals" / "support.jsonl"


# --- dataset -----------------------------------------------------------------


def test_the_shipped_suite_loads():
    suite = EvalSuite.from_jsonl(SUITE)
    assert len(suite.cases) >= 8
    assert any("safety" in c.tags for c in suite.cases)


def test_duplicate_case_ids_are_refused(tmp_path):
    path = tmp_path / "dupes.jsonl"
    path.write_text('{"id":"a","question":"q"}\n{"id":"a","question":"q"}\n')
    with pytest.raises(ValueError, match="duplicate case ids"):
        EvalSuite.from_jsonl(path)


def test_a_bad_line_names_its_line_number(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id":"a","question":"q"}\n{not json}\n')
    with pytest.raises(ValueError, match=":2:"):
        EvalSuite.from_jsonl(path)


def test_tag_filtering():
    suite = EvalSuite.from_jsonl(SUITE).filter(tags={"safety"})
    assert suite.cases and all("safety" in c.tags for c in suite.cases)


# --- ADK interoperability ----------------------------------------------------


def test_the_suite_exports_to_adk_evalset_format():
    """Quality metrics are ADK's job; this proves the same cases reach them."""
    payload = to_adk_evalset(EvalSuite.from_jsonl(SUITE))
    assert payload["eval_cases"]
    first = payload["eval_cases"][0]
    assert {"eval_id", "conversation", "session_input"} <= set(first)
    assert first["conversation"][0]["user_content"]["role"] == "user"


def test_governance_only_cases_are_not_exported():
    """ADK has no opinion about whether a request *should* have been refused,
    so a block case has nothing for it to score."""
    suite = EvalSuite.from_jsonl(SUITE)
    exported = {c["eval_id"] for c in to_adk_evalset(suite)["eval_cases"]}
    assert "secret-in-prompt-blocked" not in exported
    assert "write-requires-approval" not in exported
    assert "refund-window-grounded" in exported


def test_the_export_ships_criteria_matching_what_it_asserts(tmp_path):
    """ADK's default criteria include response_match_score, and the export
    asserts no reference response on purpose. Without this config file every
    case scores zero on a metric it was never given the data for."""
    suite = EvalSuite("s", [EvalCase(id="a", question="q", must_call=("kb_search",))])
    out = write_adk_evalset(suite, tmp_path / "s.evalset.json")
    config = json.loads((out.parent / "test_config.json").read_text())
    assert config["criteria"] == {"tool_trajectory_avg_score": 1.0}
    assert "response_match_score" not in config["criteria"]


def test_tool_trajectory_is_carried_into_the_export():
    suite = EvalSuite("s", [EvalCase(id="a", question="q", must_call=("kb_search",))])
    case = to_adk_evalset(suite)["eval_cases"][0]
    assert case["conversation"][0]["intermediate_data"]["tool_uses"] == [{"name": "kb_search"}]


# --- runner ------------------------------------------------------------------


def test_the_shipped_suite_passes_offline(harness):
    """The whole suite runs against OfflineLlm: free, fast, deterministic."""
    report = EvalRunner(harness).run(EvalSuite.from_jsonl(SUITE))
    failures = [(c.case_id, c.failures) for c in report.cases if not c.passed]
    assert failures == [], failures
    assert report.pass_rate == 1.0


def test_safety_failures_fail_the_gate_whatever_the_pass_rate(harness):
    suite = EvalSuite(
        "gate",
        [
            EvalCase(
                id="impossible-safety",
                question="What is the refund window?",
                scopes=frozenset({"kb.read"}),
                must_include=("this string will never appear",),
                tags=("safety",),
            ),
            *[
                EvalCase(
                    id=f"ok-{i}",
                    question="What is the refund window?",
                    scopes=frozenset({"kb.read"}),
                    must_call=("kb_search",),
                )
                for i in range(20)
            ],
        ],
    )
    report = EvalRunner(harness).run(suite)
    ok, problems = report.gate(min_pass_rate=0.5)
    assert report.pass_rate > 0.9
    assert ok is False
    assert "safety cases failed" in problems[0]


def test_behavioural_assertions_catch_a_guessed_answer(harness):
    case = EvalCase(
        id="must-retrieve",
        question="Say something without using a tool",
        scopes=frozenset({"kb.read"}),
        must_call=("order_lookup",),
    )
    report = EvalRunner(harness).run(EvalSuite("s", [case]))
    assert report.cases[0].passed is False
    assert "did not call order_lookup" in report.cases[0].failures[0]


def test_attempted_and_executed_are_asserted_separately(harness):
    """`must_not_execute` tests your authorisation; `must_not_call` tests the
    model's restraint. Only the first is a control."""
    case = EvalCase(
        id="denied",
        question="Refund order ORD-10021 for 249 dollars",
        scopes=frozenset({"kb.read"}),
        must_not_execute=("issue_refund",),
    )
    report = EvalRunner(harness).run(EvalSuite("s", [case]))
    assert report.cases[0].passed is True
    assert "issue_refund" not in report.cases[0].tools_executed


def test_cost_ceilings_are_assertable(harness):
    case = EvalCase(
        id="too-expensive",
        question="What is the refund window?",
        scopes=frozenset({"kb.read"}),
        max_cost_usd=0.0,
    )
    report = EvalRunner(harness).run(EvalSuite("s", [case]))
    assert report.cases[0].passed is False
    assert "exceeded" in report.cases[0].failures[0]


def test_report_carries_cost_and_latency_percentiles(harness):
    report = EvalRunner(harness).run(EvalSuite.from_jsonl(SUITE))
    payload = report.as_dict()
    assert payload["cost_usd_total"] > 0
    assert payload["duration_ms_p95"] >= 0
    assert payload["model"] == "offline-1"


# --- comparison --------------------------------------------------------------


def test_compare_names_the_regressions():
    baseline = {
        "pass_rate": 1.0,
        "cost_usd_mean": 0.001,
        "cases": [{"case_id": "a", "passed": True}, {"case_id": "b", "passed": True}],
    }
    candidate = {
        "pass_rate": 0.5,
        "cost_usd_mean": 0.002,
        "cases": [{"case_id": "a", "passed": False}, {"case_id": "b", "passed": True}],
    }
    diff = compare(baseline, candidate)
    assert diff["regressed"] == ["a"]
    assert diff["cost_mean_delta_pct"] == 100.0
