"""Eval cases: the definition of "working" that your code can check.

A case is a question plus a set of *assertions about the run*, not just about
the final string. That distinction is the whole reason agent evals are
different from model evals: you care whether the agent looked the order up, not
only whether the sentence it produced sounds right. An agent that guesses the
correct answer has failed the test that matters.

Four families of assertion, in descending order of how much they are worth:

  1. **Behavioural** — `must_call` / `must_not_call`. Did it use the right
     tools? Deterministic, cheap, and the best predictor of production
     behaviour.
  2. **Safety** — `expect_status`, `must_block`. Did the guardrails do what the
     policy says? A regression here is an incident, so these are the cases that
     should gate a deploy.
  3. **Content** — `must_include` / `must_not_include`. Cheap string checks.
     Brittle if over-used; excellent for "did it cite POL-REFUND-01".
  4. **Judged** — a rubric scored by a model. Not implemented here: ADK ships
     `rubric_based_final_response_quality_v1`, `hallucinations_v1` and
     `safety_v1` already. `to_adk_evalset()` exports these cases into ADK's
     format so `adk eval` scores them with those metrics. Reimplementing a
     judge would be strictly worse than using the one that is maintained.

Budget assertions (`max_cost_usd`, `max_model_calls`) sit alongside them because a
change that doubles the cost of every run is a regression even when every
answer stays correct.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    question: str
    agent: str = "support"
    tenant_id: str = "acme"
    scopes: frozenset[str] = frozenset()
    approvals: frozenset[str] = frozenset()
    # behavioural
    #   must_call        the agent ATTEMPTED this tool
    #   must_not_call    the agent never even attempted it
    #   must_not_execute the attempt may happen, but it must not succeed
    #
    # The third one is the least-privilege assertion and it is the one worth
    # understanding. When an agent tries to call a tool it has no scope for,
    # the attempt is real and the execution is denied. Asserting "never
    # attempted" there would test the model's restraint; asserting "never
    # executed" tests your authorisation. Only one of those is a control.
    must_call: tuple[str, ...] = ()
    must_not_call: tuple[str, ...] = ()
    must_not_execute: tuple[str, ...] = ()
    # safety
    expect_status: str | None = None
    must_block: bool = False
    # content
    must_include: tuple[str, ...] = ()
    must_not_include: tuple[str, ...] = ()
    must_cite: tuple[str, ...] = ()
    # economics
    max_cost_usd: float | None = None
    max_model_calls: int | None = None
    # judged
    rubric: str | None = None
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EvalCase:
        return cls(
            id=str(raw["id"]),
            question=str(raw["question"]),
            agent=str(raw.get("agent", "support")),
            tenant_id=str(raw.get("tenant_id", "acme")),
            scopes=frozenset(raw.get("scopes", [])),
            approvals=frozenset(raw.get("approvals", [])),
            must_call=tuple(raw.get("must_call", [])),
            must_not_call=tuple(raw.get("must_not_call", [])),
            must_not_execute=tuple(raw.get("must_not_execute", [])),
            expect_status=raw.get("expect_status"),
            must_block=bool(raw.get("must_block", False)),
            must_include=tuple(raw.get("must_include", [])),
            must_not_include=tuple(raw.get("must_not_include", [])),
            must_cite=tuple(raw.get("must_cite", [])),
            max_cost_usd=raw.get("max_cost_usd"),
            max_model_calls=raw.get("max_model_calls", raw.get("max_steps")),
            rubric=raw.get("rubric"),
            tags=tuple(raw.get("tags", [])),
        )


@dataclass(slots=True)
class EvalSuite:
    name: str
    cases: list[EvalCase] = field(default_factory=list)

    @classmethod
    def from_jsonl(cls, path: str | Path, *, name: str | None = None) -> EvalSuite:
        p = Path(path)
        cases: list[EvalCase] = []
        for line_no, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                cases.append(EvalCase.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError) as exc:
                raise ValueError(f"{p}:{line_no}: {exc}") from exc
        ids = [c.id for c in cases]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{p}: duplicate case ids")
        return cls(name=name or p.stem, cases=cases)

    def filter(self, *, tags: set[str] | None = None) -> EvalSuite:
        if not tags:
            return self
        return EvalSuite(self.name, [c for c in self.cases if set(c.tags) & tags])


# --- ADK interoperability ----------------------------------------------------


def to_adk_evalset(suite: EvalSuite) -> dict[str, Any]:
    """Export the suite in ADK's eval-set format.

    The same cases, scored by ADK's maintained metrics — tool trajectory,
    response match, hallucination and safety — instead of anything hand-rolled.
    Write the result to `<name>.evalset.json` and run `adk eval`.

    Cases that only assert governance (a guardrail block, a scope denial) are
    skipped: there is no reference response to match, and ADK has no opinion
    about whether a request *should* have been refused. Those stay in this
    package's runner, which is exactly the split the whole project argues for.
    """
    cases: list[dict[str, Any]] = []
    for case in suite.cases:
        if case.must_block or case.expect_status in {"blocked", "needs_approval"}:
            continue
        cases.append(
            {
                "eval_id": case.id,
                "conversation": [
                    {
                        "invocation_id": case.id,
                        "user_content": {
                            "parts": [{"text": case.question}],
                            "role": "user",
                        },
                        # No reference response is asserted. Pinning exact text
                        # makes a suite that fails on every harmless rewording,
                        # which teams learn to ignore. The tool trajectory is
                        # the stable signal, and it is what `must_call` pins.
                        "intermediate_data": {
                            "tool_uses": [{"name": name} for name in case.must_call]
                        },
                    }
                ],
                "session_input": {
                    "app_name": "agent_harness",
                    "user_id": f"eval-{case.tenant_id}",
                    "state": {"tenant_id": case.tenant_id},
                },
            }
        )
    return {"eval_set_id": suite.name, "name": suite.name, "eval_cases": cases}


# `adk eval` reads criteria from `test_config.json` beside the eval-set file,
# and falls back to `tool_trajectory_avg_score` *and* `response_match_score`.
# The export above deliberately asserts no reference response, so under the
# default criteria every case scores zero on a metric it was never given the
# data for — a suite that reports 0/8 whatever the agent did. Writing the
# config alongside the set keeps the exported criteria honest about what the
# cases actually pin.
ADK_EVAL_CRITERIA: dict[str, object] = {"criteria": {"tool_trajectory_avg_score": 1.0}}


def write_adk_evalset(suite: EvalSuite, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(to_adk_evalset(suite), indent=2), encoding="utf-8")
    (p.parent / "test_config.json").write_text(
        json.dumps(ADK_EVAL_CRITERIA, indent=2), encoding="utf-8"
    )
    return p
