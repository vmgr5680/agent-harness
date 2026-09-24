"""MCP (Model Context Protocol) servers, connected with ADK's own toolset.

ADK ships `McpToolset`, which handles the whole protocol: discovery, the
session, schema translation, and — usefully — `tool_filter`,
`tool_name_prefix` and `require_confirmation`. None of that is reimplemented
here.

What this module adds is the part ADK cannot decide for you, and the defaults
below are deliberately unfriendly:

  - **Every imported tool requires confirmation** unless you name it in
    `read_only`. You cannot tell from a description whether `update_record` is
    idempotent, and the failure is asymmetric.
  - **Every imported tool is namespaced.** The day you connect two servers that
    both advertise `search`, you will want this.
  - **Every imported tool's description is screened**, because —

The thing people miss
---------------------
**A third-party server's tool descriptions are your prompt.** They are placed
in the model's context on every step, so a hostile or compromised server can
attempt prompt injection through the tool catalogue itself, before any tool is
ever called. `GovernedMcpToolset` runs every description through the same
injection detector as any other untrusted text and replaces the ones that trip
it.

Connect to a server you do not operate and its catalogue is attacker-controlled
text you are pasting into your system prompt on every request.

Usage:

    from mcp import StdioServerParameters
    from agent_harness.tools.mcp import build_mcp_toolset

    toolset = build_mcp_toolset(
        connection_params=StdioServerParameters(command="npx", args=["-y", "@acme/mcp"]),
        server="acme",
        read_only={"search", "fetch"},   # reviewed by hand, tool by tool
    )
    agent = LlmAgent(name="support", model=..., tools=[toolset])

Requires the `mcp` package: `pip install mcp`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool

from ..observability import log

_LOG = logging.getLogger("agent_harness.tools.mcp")

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_DESCRIPTION = 400


def sanitise_description(raw: str, *, tool: str) -> str:
    """A third-party tool description goes into our prompt. Treat it as data.

    Control characters stripped, length capped, and anything that trips the
    injection detector replaced outright. A withheld description costs the
    model some accuracy in tool choice; an obeyed one costs rather more.
    """
    from ..guardrails.detectors import find_injection

    text = _CONTROL.sub(" ", str(raw or "")).strip()
    text = re.sub(r"\s+", " ", text)[:_MAX_DESCRIPTION]
    hits = list(find_injection(text))
    if hits:
        log(
            _LOG,
            logging.WARNING,
            "mcp.description_rejected",
            tool=tool,
            kinds=[h.kind for h in hits],
        )
        return f"(description withheld: failed content screening) tool named {tool}"
    return text


def build_mcp_toolset(
    *,
    connection_params: Any,
    server: str,
    read_only: frozenset[str] | set[str] = frozenset(),
    tool_filter: list[str] | None = None,
    cache_ttl_seconds: float = 300.0,
) -> Any:
    """An `McpToolset` with governance defaults and screened descriptions.

    `read_only` is the allowlist you maintain by hand after reading what each
    tool actually does. Names in it are registered without a confirmation gate;
    everything else stops the run and asks a human.
    """
    try:
        from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("MCP support needs the `mcp` package: pip install mcp") from exc

    allowed = set(read_only)

    class GovernedMcpToolset(McpToolset):
        async def get_tools(
            self, readonly_context: ReadonlyContext | None = None
        ) -> list[BaseTool]:
            tools: list[BaseTool] = await super().get_tools(readonly_context)
            for tool in tools:
                tool.description = sanitise_description(tool.description, tool=tool.name)
            log(_LOG, logging.INFO, "mcp.tools_loaded", server=server, count=len(tools))
            return tools

    return GovernedMcpToolset(
        connection_params=connection_params,
        tool_filter=tool_filter,
        # Namespaced, because two servers will eventually both advertise
        # `search` and the collision is silent.
        tool_name_prefix=server,
        tool_list_cache_ttl_seconds=cache_ttl_seconds,
        # Anything not explicitly reviewed and listed as read-only stops the
        # run and asks a human. Unfriendly on purpose.
        require_confirmation=lambda **kwargs: _needs_confirmation(kwargs, allowed, server),
    )


def _needs_confirmation(kwargs: dict[str, Any], read_only: set[str], server: str) -> bool:
    """Confirmation is required unless the tool was reviewed and allowlisted."""
    name = str(kwargs.get("tool_name") or kwargs.get("name") or "")
    bare = name.removeprefix(f"{server}_").removeprefix(f"{server}__")
    return bare not in read_only
