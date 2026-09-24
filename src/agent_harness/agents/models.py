"""Model selection: which `BaseLlm` each agent gets.

Three things this module exists to do.

**Route by role, not by call site.** The root agent, which writes the answer a
customer reads, gets the `deep` model. Subagents, which fetch and quote, get
`fast`. Nothing outside this file names a model, so moving the researcher onto
a cheaper model is one environment variable rather than an audit of the tree.

**Make offline the default.** With no key, every agent gets `OfflineLlm` and
the entire system — demo, tests, evals — runs with no network and no cost.
Swapping in Gemini is a string.

**Resolve credentials once, explicitly.** An AI Studio key is the Gemini API,
not Vertex AI. `google-genai` reads `GOOGLE_API_KEY`, while most of this
project's documentation and every Gemini tutorial says `GEMINI_API_KEY`. That
mismatch is a twenty-minute debugging session for everyone who hits it, so it
is reconciled here, in one place, with the Vertex switch pinned off.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.genai import types

from ..config import Settings
from ..observability import log
from .offline import OfflineLlm

_LOG = logging.getLogger("agent_harness.agents.models")

Role = Literal["deep", "fast", "judge"]


def configure_credentials(settings: Settings) -> bool:
    """Put the key where `google-genai` will actually look. Returns True if a
    usable credential is present.

    Deliberately explicit rather than clever: it sets the environment variables
    the SDK reads, logs that it did so without ever logging the key, and pins
    `GOOGLE_GENAI_USE_VERTEXAI=FALSE` because an AI Studio key is not a Vertex
    credential and the failure when they are confused is unhelpful.
    """
    key = settings.google_api_key or settings.gemini_api_key
    if not key:
        return False
    os.environ["GOOGLE_API_KEY"] = key
    # google-genai warns when both are set and silently prefers GOOGLE_API_KEY.
    # Removing the duplicate makes the precedence explicit instead of a log line
    # people learn to ignore.
    os.environ.pop("GEMINI_API_KEY", None)
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
    log(_LOG, logging.INFO, "credentials.configured", vertex=False, key_present=True)
    return True


# ADK's `Gemini` does NOT retry by default: `retry_options=None` tells the
# google-genai client "never retry". Found during a Gemini 503 on 2026-09-23,
# when every overloaded call failed on its first attempt. Retries are opt-in,
# so they are opted into here, for the transient statuses only.
RETRY = types.HttpRetryOptions(
    attempts=4,  # including the first call
    initial_delay=1.0,
    max_delay=16.0,
    http_status_codes=[408, 429, 500, 502, 503, 504],
)


def build_model(settings: Settings, role: Role = "deep") -> BaseLlm:
    """The model for one role.

    For Gemini this is ADK's own `Gemini` class — the same one its registry
    would build from a bare model string, so streaming and the OpenTelemetry
    spans come with it — constructed explicitly so it can carry `RETRY`. A bare
    string would get ADK's default, which is no retries at all.
    """
    if settings.provider == "offline" or not configure_credentials(settings):
        if settings.provider != "offline":
            log(
                _LOG,
                logging.WARNING,
                "credentials.missing",
                note="no GOOGLE_API_KEY or GEMINI_API_KEY; falling back to the offline model",
            )
        return OfflineLlm()
    name = {
        "deep": settings.model_deep,
        "fast": settings.model_fast,
        "judge": settings.model_judge,
    }[role]
    return Gemini(model=name, retry_options=RETRY)


def model_name(model: BaseLlm | str) -> str:
    return model if isinstance(model, str) else model.model


def list_available_models(settings: Settings) -> list[dict[str, Any]]:
    """Ask the API which models this key can actually call.

    Worth having as a first-class command. Model identifiers change faster than
    documentation does, and "the model id in the tutorial no longer exists" is
    a failure that looks like a code bug. This turns it into a list.
    """
    if not configure_credentials(settings):
        raise RuntimeError("no API key configured; set GOOGLE_API_KEY (or GEMINI_API_KEY)")
    from google import genai

    client = genai.Client()
    out: list[dict[str, Any]] = []
    for model in client.models.list():
        actions = list(model.supported_actions or [])
        if "generateContent" not in actions:
            continue
        out.append(
            {
                "name": (model.name or "").removeprefix("models/"),
                "display_name": model.display_name,
                "input_token_limit": model.input_token_limit,
                "output_token_limit": model.output_token_limit,
            }
        )
    return sorted(out, key=lambda m: m["name"])
