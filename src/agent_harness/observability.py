"""Metrics, structured logging and the cost ledger.

What is deliberately NOT here
-----------------------------
A tracer. ADK emits OpenTelemetry spans for every agent, model call and tool
call already, and running a second tracing system alongside it produces two
half-complete pictures of the same run instead of one good one. Point ADK's
telemetry at your collector and you have distributed tracing; there is nothing
for this file to add.

What ADK does not give you, and is therefore here
-------------------------------------------------
**Cost.** ADK reports `usage_metadata` per call. It does not know what a token
costs, does not keep a running total, and has no opinion about budget
ceilings. "Which agent, which tenant, which step spent the money" is the
question you actually ask during an incident, and only you can answer it.

**Business metrics.** Guardrail findings by rail and surface, tool denials,
approvals requested, runs by outcome. Prometheus text exposition is included
because `/metrics` is how these reach an alert.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

_BUCKETS_MS: tuple[float, ...] = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10_000, 30_000)


def new_id(n: int = 16) -> str:
    return uuid.uuid4().hex[:n]


# --- metrics -----------------------------------------------------------------


@dataclass(slots=True)
class _Histogram:
    buckets: dict[float, int] = field(default_factory=lambda: dict.fromkeys(_BUCKETS_MS, 0))
    count: int = 0
    total: float = 0.0

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        for edge in _BUCKETS_MS:
            if value <= edge:
                self.buckets[edge] += 1

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0


class Metrics:
    """Counters and histograms with Prometheus text exposition. Thread-safe.

    Labels are a sorted tuple so the same label set always produces the same
    series. Keep cardinality low: a label whose value is a user id or a
    question will destroy your metrics backend.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._hists: dict[tuple[str, tuple[tuple[str, str], ...]], _Histogram] = {}

    @staticmethod
    def _key(name: str, labels: dict[str, str] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        return (name, tuple(sorted((k, str(v)) for k, v in (labels or {}).items())))

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        with self._lock:
            key = self._key(name, labels)
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._hists.setdefault(self._key(name, labels), _Histogram()).observe(value)

    @staticmethod
    def _render(key: tuple[str, tuple[tuple[str, str], ...]]) -> str:
        name, labels = key
        if not labels:
            return name
        return f"{name}{{{','.join(f'{k}="{v}"' for k, v in labels)}}}"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": {
                    self._render(k): round(v, 8) for k, v in sorted(self._counters.items(), key=str)
                },
                "histograms": {
                    self._render(k): {
                        "count": h.count,
                        "sum": round(h.total, 3),
                        "mean": round(h.mean, 3),
                    }
                    for k, h in sorted(self._hists.items(), key=str)
                },
            }

    def prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            for key, value in sorted(self._counters.items(), key=str):
                lines.append(f"# TYPE {key[0]} counter")
                lines.append(f"{self._render(key)} {value}")
            for (name, labels), hist in sorted(self._hists.items(), key=str):
                lines.append(f"# TYPE {name} histogram")
                for edge in _BUCKETS_MS:
                    items = [*labels, ("le", str(edge))]
                    lines.append(
                        f"{name}_bucket{{{','.join(f'{k}="{v}"' for k, v in items)}}} "
                        f"{hist.buckets[edge]}"
                    )
                inf = [*labels, ("le", "+Inf")]
                lines.append(
                    f"{name}_bucket{{{','.join(f'{k}="{v}"' for k, v in inf)}}} {hist.count}"
                )
                suffix = f"{{{','.join(f'{k}="{v}"' for k, v in labels)}}}" if labels else ""
                lines.append(f"{name}_sum{suffix} {round(hist.total, 6)}")
                lines.append(f"{name}_count{suffix} {hist.count}")
        return "\n".join(lines) + "\n"


# --- cost ledger -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostEntry:
    run_id: str
    agent: str
    tenant_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float
    cache_hit: bool = False


class CostLedger:
    """Append-only record of what every model call cost, and for whom.

    `total_for_run` is what the per-run ceiling is checked against, and the API
    returns it on every response — cost that is only visible in a monthly
    billing export is cost nobody manages.

    This is not an invoice. Reconcile it against the provider's export; it
    exists to answer the question the export cannot, which is *which agent and
    which tenant*.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[CostEntry] = []

    def record(self, entry: CostEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    def entries(self, run_id: str | None = None) -> list[CostEntry]:
        with self._lock:
            if run_id is None:
                return list(self._entries)
            return [e for e in self._entries if e.run_id == run_id]

    def total_for_run(self, run_id: str) -> float:
        return round(sum(e.cost_usd for e in self.entries(run_id)), 8)

    def total_for_tenant(self, tenant_id: str) -> float:
        with self._lock:
            return round(sum(e.cost_usd for e in self._entries if e.tenant_id == tenant_id), 8)

    def breakdown(self, run_id: str) -> dict[str, Any]:
        entries = self.entries(run_id)
        by_agent: dict[str, float] = {}
        for e in entries:
            by_agent[e.agent] = round(by_agent.get(e.agent, 0.0) + e.cost_usd, 8)
        return {
            "calls": len(entries),
            "input_tokens": sum(e.input_tokens for e in entries),
            "output_tokens": sum(e.output_tokens for e in entries),
            "cached_tokens": sum(e.cached_tokens for e in entries),
            "total_usd": self.total_for_run(run_id),
            "by_agent_usd": by_agent,
        }


# --- logging -----------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, with the ambient OpenTelemetry trace context
    spliced in so a log line can be joined to the ADK span that produced it."""

    def __init__(self, service: str, env: str) -> None:
        super().__init__()
        self.service = service
        self.env = env

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": self.service,
            "env": self.env,
        }
        if (ctx := _otel_ids()) is not None:
            payload.update(ctx)
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _otel_ids() -> dict[str, str] | None:
    """Current OpenTelemetry trace and span ids, if ADK has a span open.

    Best effort: if the OTel API is absent or no span is recording, log lines
    simply carry no trace context rather than failing.
    """
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if not ctx.is_valid:
            return None
        return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
    except Exception:  # noqa: BLE001 - observability must never break the request
        return None


class _TextFormatter(logging.Formatter):
    def __init__(self, service: str, env: str) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s | %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict) and extra:
            return f"{base} " + " ".join(f"{k}={v}" for k, v in extra.items())
        return base


def configure_logging(level: str = "INFO", fmt: str = "json", *, service: str, env: str) -> None:
    """Idempotent. Safe to call from tests and from the API startup hook."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        _JsonFormatter(service, env) if fmt == "json" else _TextFormatter(service, env)
    )
    root.addHandler(handler)
    root.setLevel(level.upper())
    # These are chatty and duplicate what the governance plugin already records.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    logging.getLogger("google_adk").setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """Log with structured fields that survive into the JSON formatter."""
    logger.log(level, msg, extra={"extra_fields": fields})
