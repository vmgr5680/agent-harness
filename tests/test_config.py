from __future__ import annotations

import pytest

from agent_harness.config import DEFAULT_MODEL, Principal, load_settings
from agent_harness.errors import ConfigError


def test_no_key_means_offline(env):
    """The default has to be the one that costs nothing and needs nothing."""
    s = load_settings(env)
    assert s.provider == "offline"
    assert s.is_live is False


def test_a_key_flips_the_default_to_live():
    """The common mistake is configuring a key and silently still getting the
    offline model because a second variable was not also set."""
    s = load_settings({"GOOGLE_API_KEY": "AIza-test-key-value", "AH_LOG_LEVEL": "CRITICAL"})
    assert s.provider == "gemini"
    assert s.is_live is True
    assert s.model_deep == DEFAULT_MODEL


def test_gemini_api_key_is_accepted_as_well_as_google_api_key():
    s = load_settings({"GEMINI_API_KEY": "AIza-test-key-value"})
    assert s.provider == "gemini"


def test_explicit_gemini_without_a_key_fails_loudly(env):
    with pytest.raises(ConfigError, match="GOOGLE_API_KEY"):
        load_settings({**env, "AH_PROVIDER": "gemini"})


def test_an_empty_choice_means_absent_not_invalid(env):
    # `set -a && source .env && set +a` on a .env.example that has not been
    # filled in yet exports AH_PROVIDER as "". That is the same as unset.
    blank = {**env, "AH_PROVIDER": "", "AH_GUARDRAIL_MODE": "", "AH_LOG_FORMAT": ""}
    s = load_settings(blank)
    assert s.provider == "offline"
    assert s.guardrail_mode == "enforce"
    assert s.log_format == "json"
    # A key present still flips the default, exactly as when it is unset.
    assert load_settings({**blank, "GOOGLE_API_KEY": "AIza-test"}).provider == "gemini"


def test_a_genuinely_wrong_choice_still_fails(env):
    with pytest.raises(ConfigError, match="AH_PROVIDER must be one of"):
        load_settings({**env, "AH_PROVIDER": "openai"})


def test_the_fast_and_judge_models_default_to_the_deep_one(env):
    s = load_settings({**env, "AH_MODEL_DEEP": "gemini-x"})
    assert s.model_fast == "gemini-x" and s.model_judge == "gemini-x"


def test_malformed_integer_fails_at_load_not_at_use(env):
    with pytest.raises(ConfigError, match="AH_MAX_MODEL_CALLS"):
        load_settings({**env, "AH_MAX_MODEL_CALLS": "twelve"})


def test_out_of_range_values_are_refused(env):
    with pytest.raises(ConfigError, match="between"):
        load_settings({**env, "AH_MAX_MODEL_CALLS": "5000"})


def test_guardrails_cannot_be_disabled_outside_dev(env):
    with pytest.raises(ConfigError, match="only permitted when AH_ENV=dev"):
        load_settings({**env, "AH_ENV": "prod", "AH_GUARDRAIL_MODE": "off"})
    assert load_settings({**env, "AH_GUARDRAIL_MODE": "off"}).guardrail_mode == "off"


def test_tokens_parse_into_principals(env):
    s = load_settings({**env, "AH_SERVICE_TOKENS": "tok-abcdefgh:acme:agent.run|evals.run"})
    who = s.principal_for("tok-abcdefgh")
    assert isinstance(who, Principal)
    assert who.tenant_id == "acme"
    assert who.has("agent.run") and not who.has("admin")


def test_short_tokens_are_refused(env):
    with pytest.raises(ConfigError, match="shorter than 8"):
        load_settings({**env, "AH_SERVICE_TOKENS": "abc:acme:agent.run"})


def test_token_id_does_not_carry_the_whole_token(env):
    s = load_settings({**env, "AH_SERVICE_TOKENS": "supersecrettoken:acme:agent.run"})
    who = s.principal_for("supersecrettoken")
    assert who is not None and "supersecrettoken" not in who.token_id


def test_redacted_never_contains_the_key():
    s = load_settings({"GOOGLE_API_KEY": "AIzaSyB1c2D3e4F5g6H7i8J9k0L1m2N3o4P5q6R"})
    assert "AIza" not in repr(s.redacted())
    assert s.redacted()["api_key_present"] is True


def test_pricing_override_is_parsed(env):
    s = load_settings({**env, "AH_PRICING_JSON": '{"m":{"input":1.0,"output":2.0}}'})
    assert s.pricing_override["m"]["output"] == 2.0


def test_pricing_override_rejects_garbage(env):
    with pytest.raises(ConfigError, match="valid JSON"):
        load_settings({**env, "AH_PRICING_JSON": "{not json"})


def test_a_blank_model_role_falls_back_to_the_deep_model() -> None:
    """`.env.example` ships AH_MODEL_FAST= and AH_MODEL_JUDGE= empty."""
    settings = load_settings(
        {"AH_MODEL_DEEP": "gemini-x", "AH_MODEL_FAST": "", "AH_MODEL_JUDGE": "  "}
    )
    assert settings.model_fast == "gemini-x"
    assert settings.model_judge == "gemini-x"
