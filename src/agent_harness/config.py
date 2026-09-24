"""Configuration, resolved once from the environment.

Three rules this module exists to enforce:

1. **Nothing reads os.environ except this file** (and `agents/models.py`, which
   *writes* the two variables the Google SDK insists on reading itself). A
   setting read in three places is a setting with three different values
   during an incident.

2. **Settings are frozen and passed explicitly.** No module-level singleton
   that imports mutate. Tests construct a `Settings` directly; production calls
   `load_settings()` once at startup and threads it down.

3. **Validation happens at load, not at use.** A malformed `AH_MAX_MODEL_CALLS`
   should crash the process on boot, not three minutes into a customer's
   request.

Why not pydantic-settings, when ADK already pulls pydantic in? Because this is
the one file that must not fail in an interesting way. It is 200 lines of
stdlib with explicit bounds on every numeric setting and an explicit error
message for every failure, and that is worth more here than brevity. If your
service already standardises on pydantic-settings, port it — the shape is
identical and nothing else imports `os.environ`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from .errors import ConfigError

GuardrailMode = Literal["off", "shadow", "enforce"]
ProviderName = Literal["offline", "gemini"]

_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_FALSE: Final = frozenset({"0", "false", "no", "off"})

# The default model. Change it in one place, or per environment.
# `agent-harness models` lists what your key can actually call, which is the
# only reliable source — model identifiers change faster than documentation.
DEFAULT_MODEL: Final = "gemini-3.1-flash-lite"


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making the request. Produced by token lookup, never by the caller."""

    tenant_id: str
    token_id: str
    scopes: frozenset[str]

    def has(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True, slots=True)
class Settings:
    # --- provider and models -------------------------------------------------
    # "offline" runs the deterministic OfflineLlm: no key, no network, no cost.
    provider: ProviderName = "offline"
    google_api_key: str = ""
    gemini_api_key: str = ""
    model_deep: str = DEFAULT_MODEL  # the agent that writes the answer
    model_fast: str = DEFAULT_MODEL  # subagents that fetch and quote
    model_judge: str = DEFAULT_MODEL  # the eval judge

    # --- termination ceilings ------------------------------------------------
    # ADK enforces max_model_calls itself (RunConfig.max_llm_calls). The other
    # two are enforced by the harness. Any one of the three ends a run.
    max_model_calls: int = 12
    run_timeout_s: float = 120.0
    budget_usd_per_run: float = 0.05
    budget_usd_per_tenant_day: float = 25.0

    # --- economics -----------------------------------------------------------
    rate_limit_rpm: float = 60.0
    rate_limit_burst: int = 10
    pricing_override: dict[str, dict[str, float]] = field(default_factory=dict)

    # --- guardrails ----------------------------------------------------------
    guardrail_mode: GuardrailMode = "enforce"
    guardrail_max_input_chars: int = 8000
    guardrail_strict_output: bool = False

    # --- persistence and observability ---------------------------------------
    # Empty means ADK's InMemorySessionService. A URL means DatabaseSessionService,
    # which persists every event of every run — that is the audit trail.
    session_db_url: str = ""
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    service_name: str = "agent_harness"
    env: str = "dev"

    # --- API auth ------------------------------------------------------------
    service_tokens: dict[str, Principal] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.provider == "gemini" and bool(self.google_api_key or self.gemini_api_key)

    def principal_for(self, token: str) -> Principal | None:
        return self.service_tokens.get(token)

    def redacted(self) -> dict[str, object]:
        """Safe to log. The key never appears, only whether one is present."""
        return {
            "provider": self.provider,
            "api_key_present": bool(self.google_api_key or self.gemini_api_key),
            "model_deep": self.model_deep,
            "model_fast": self.model_fast,
            "max_model_calls": self.max_model_calls,
            "budget_usd_per_run": self.budget_usd_per_run,
            "guardrail_mode": self.guardrail_mode,
            "guardrail_strict_output": self.guardrail_strict_output,
            "sessions": "database" if self.session_db_url else "memory",
            "env": self.env,
            "tenants": sorted({p.tenant_id for p in self.service_tokens.values()}),
        }


# --- parsing helpers ---------------------------------------------------------


def _str(env: Mapping[str, str], key: str, default: str) -> str:
    # Blank means unset, as it does for every other type here. `.env.example`
    # ships `AH_MODEL_FAST=` empty, and treating that as the model name "" left
    # every "fast" agent with no model at all — found live on 2026-09-23 as
    # `ValueError: model is required` in the Part 2 pattern apps.
    raw = env.get(key)
    return default if raw is None or not raw.strip() else raw.strip()


def _int(env: Mapping[str, str], key: str, default: int, *, min_: int, max_: int) -> int:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if not min_ <= value <= max_:
        raise ConfigError(f"{key} must be between {min_} and {max_}, got {value}")
    return value


def _float(env: Mapping[str, str], key: str, default: float, *, min_: float, max_: float) -> float:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if not min_ <= value <= max_:
        raise ConfigError(f"{key} must be between {min_} and {max_}, got {value}")
    return value


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{key} must be a boolean, got {raw!r}")


def _choice(env: Mapping[str, str], key: str, default: str, allowed: tuple[str, ...]) -> str:
    # An empty value means "absent", as it does for _int/_float/_bool. Every
    # blank line in .env.example relies on that, and a variable exported empty
    # by `set -a && source .env` before the file was filled in must not be a
    # different thing from a variable that was never exported at all.
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value not in allowed:
        raise ConfigError(f"{key} must be one of {allowed}, got {raw.strip()!r}")
    return value


def _tokens(env: Mapping[str, str], key: str) -> dict[str, Principal]:
    """Parse "token:tenant:scope|scope,token2:tenant2:scope".

    A development-grade credential store, and it says so in .env.example. In
    production the token resolves to a Principal via your identity provider;
    only the *shape* of what comes back should survive that swap — the caller
    never states its own tenant or scopes.
    """
    raw = env.get(key, "").strip()
    if not raw:
        return {}
    out: dict[str, Principal] = {}
    for i, entry in enumerate(raw.split(",")):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            raise ConfigError(f"{key}[{i}] must be token:tenant:scopes, got {entry!r}")
        token, tenant, scopes = (p.strip() for p in parts)
        if not token or not tenant:
            raise ConfigError(f"{key}[{i}] has an empty token or tenant")
        if len(token) < 8:
            raise ConfigError(f"{key}[{i}] token is shorter than 8 characters")
        out[token] = Principal(
            tenant_id=tenant,
            token_id=f"tok_{token[:4]}",  # never store or log the whole token
            scopes=frozenset(s for s in scopes.split("|") if s),
        )
    return out


def _pricing(env: Mapping[str, str], key: str) -> dict[str, dict[str, float]]:
    raw = env.get(key, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{key} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"{key} must be a JSON object of model -> rates")
    out: dict[str, dict[str, float]] = {}
    for model, rates in parsed.items():
        if not isinstance(rates, dict):
            raise ConfigError(f"{key}[{model}] must be an object")
        try:
            out[str(model)] = {str(k): float(v) for k, v in rates.items()}
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{key}[{model}] rates must be numbers") from exc
    return out


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Resolve settings and validate them. Raises ConfigError on anything
    malformed. Call this once, at startup, and let the process die if it
    throws."""
    e = os.environ if env is None else env

    google_key = _str(e, "GOOGLE_API_KEY", "")
    gemini_key = _str(e, "GEMINI_API_KEY", "")
    # Default to live when a key is present: the common mistake is configuring
    # a key and silently getting the offline model because a second variable
    # was not also set.
    default_provider = "gemini" if (google_key or gemini_key) else "offline"
    provider = _choice(e, "AH_PROVIDER", default_provider, ("offline", "gemini"))
    if provider == "gemini" and not (google_key or gemini_key):
        raise ConfigError(
            "AH_PROVIDER=gemini requires GOOGLE_API_KEY (or GEMINI_API_KEY). "
            "Create one at https://aistudio.google.com/apikey"
        )

    model_deep = _str(e, "AH_MODEL_DEEP", DEFAULT_MODEL)
    settings = Settings(
        provider=provider,  # type: ignore[arg-type]
        google_api_key=google_key,
        gemini_api_key=gemini_key,
        model_deep=model_deep,
        model_fast=_str(e, "AH_MODEL_FAST", model_deep),
        model_judge=_str(e, "AH_MODEL_JUDGE", model_deep),
        max_model_calls=_int(e, "AH_MAX_MODEL_CALLS", 12, min_=1, max_=200),
        run_timeout_s=_float(e, "AH_RUN_TIMEOUT_S", 120.0, min_=1.0, max_=3600.0),
        budget_usd_per_run=_float(e, "AH_BUDGET_USD_PER_RUN", 0.05, min_=0.0, max_=1000.0),
        budget_usd_per_tenant_day=_float(
            e, "AH_BUDGET_USD_PER_TENANT_DAY", 25.0, min_=0.0, max_=1_000_000.0
        ),
        rate_limit_rpm=_float(e, "AH_RATE_LIMIT_RPM", 60.0, min_=0.0, max_=1_000_000.0),
        rate_limit_burst=_int(e, "AH_RATE_LIMIT_BURST", 10, min_=1, max_=100_000),
        pricing_override=_pricing(e, "AH_PRICING_JSON"),
        guardrail_mode=_choice(  # type: ignore[arg-type]
            e, "AH_GUARDRAIL_MODE", "enforce", ("off", "shadow", "enforce")
        ),
        guardrail_max_input_chars=_int(
            e, "AH_GUARDRAIL_MAX_INPUT_CHARS", 8000, min_=100, max_=1_000_000
        ),
        guardrail_strict_output=_bool(e, "AH_GUARDRAIL_STRICT_OUTPUT", False),
        session_db_url=_str(e, "AH_SESSION_DB_URL", ""),
        log_level=_str(e, "AH_LOG_LEVEL", "INFO").upper(),
        log_format=_choice(e, "AH_LOG_FORMAT", "json", ("json", "text")),  # type: ignore[arg-type]
        service_name=_str(e, "AH_SERVICE_NAME", "agent_harness"),
        env=_str(e, "AH_ENV", "dev"),
        service_tokens=_tokens(e, "AH_SERVICE_TOKENS"),
    )

    if settings.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError(f"AH_LOG_LEVEL is not a valid level: {settings.log_level}")
    if settings.env != "dev" and settings.guardrail_mode == "off":
        raise ConfigError("AH_GUARDRAIL_MODE=off is only permitted when AH_ENV=dev")
    return settings
