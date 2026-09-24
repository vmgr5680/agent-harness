"""Tools as ADK `FunctionTool`s, plus the governance ADK does not carry.

What ADK gives you here
-----------------------
`FunctionTool(fn)` derives the JSON schema from the Python signature and the
docstring, validates the model's arguments against it, and surfaces a missing
argument back to the model as a correctable error. `require_confirmation=True`
suspends the run and asks a human. That is genuinely most of the work, and all
of it used to be hand-rolled in `tools/registry.py`.

What it does not give you, and why it is here
---------------------------------------------
**Scopes.** ADK has no concept of "this principal may not call this tool". A
tool in an agent's list is callable. `TOOL_SCOPES` is the side table the
governance plugin checks in `before_tool_callback`, which is the only place a
call can still be refused.

**Tags.** Which tools a *subagent* may hold. Used to build the least-privilege
agent tree in `agents.py`.

**Output classification.** Whether a tool's result must be scanned before it
re-enters the prompt. Arithmetic is structurally safe; a billing record is not.

Keeping the domain logic in `tools/builtin.py` and only the ADK surface here is
deliberate: the handlers are pure functions with their own tests, and the day
you swap ADK for something else, this file is what you rewrite.
"""

from __future__ import annotations

from typing import Any, Final

from google.adk.tools.function_tool import FunctionTool

from . import domain as builtin

# --- scope and classification tables ----------------------------------------

TOOL_SCOPES: Final[dict[str, frozenset[str]]] = {
    "kb_search": frozenset({"kb.read"}),
    "order_lookup": frozenset({"orders.read"}),
    "calculator": frozenset(),
    "issue_refund": frozenset({"orders.write"}),
}

TOOL_TAGS: Final[dict[str, frozenset[str]]] = {
    "kb_search": frozenset({"research", "support"}),
    "order_lookup": frozenset({"support"}),
    "calculator": frozenset({"research", "support", "analysis"}),
    "issue_refund": frozenset({"support"}),
}

# Tools whose output does NOT need scanning before it re-enters the prompt.
# Only structurally-safe output qualifies. `calculator` returns a number it
# computed from an expression the model wrote; it cannot introduce customer
# data or an injected instruction. Everything else is untrusted input.
TRUSTED_OUTPUT: Final[frozenset[str]] = frozenset({"calculator"})

# Tools that change state. ADK enforces the confirmation; this set is what the
# audit log and the eval assertions key on.
WRITE_TOOLS: Final[frozenset[str]] = frozenset({"issue_refund"})


# --- the tool functions ------------------------------------------------------
#
# ADK reads the signature and the docstring to build the schema the model sees,
# so both are load-bearing. The docstring is prompt text: it is re-sent on every
# step of every run, which makes verbosity here a recurring bill.


def kb_search(query: str, top_k: int = 3) -> dict[str, Any]:
    """Search the policy knowledge base and return matching documents.

    Args:
        query: What to search for, in plain words.
        top_k: How many documents to return, 1 to 10.

    Returns:
        The matching documents, each with a doc_id you must cite.
    """
    return builtin.kb_search({"query": query, "top_k": top_k})


def order_lookup(order_id: str) -> dict[str, Any]:
    """Fetch one order by its identifier, for example ORD-10021.

    Args:
        order_id: The order identifier.

    Returns:
        The order record, or an error if no such order exists.
    """
    try:
        return builtin.order_lookup({"order_id": order_id})
    except Exception as exc:  # noqa: BLE001 - a tool error is an observation
        # Returned rather than raised: the model reads this and usually
        # recovers on the next step. Raising would end the run.
        return {"error": str(exc)}


def calculator(expression: str) -> dict[str, Any]:
    """Evaluate one arithmetic expression, for example '249.00 * 0.15'.

    Args:
        expression: Arithmetic only. No names, no function calls.

    Returns:
        The expression and its result, or an error.
    """
    try:
        return builtin.calculator({"expression": expression})
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def issue_refund(order_id: str, amount_usd: float, reason: str = "") -> dict[str, Any]:
    """Issue a refund against an order. This changes state and needs approval.

    Args:
        order_id: The order to refund.
        amount_usd: The amount, which may not exceed the order total.
        reason: Why the refund is being issued.

    Returns:
        The refund confirmation, or an error.
    """
    try:
        return builtin.issue_refund(
            {"order_id": order_id, "amount_usd": amount_usd, "reason": reason}
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# --- construction ------------------------------------------------------------


def build_tools() -> dict[str, FunctionTool]:
    """All tools, by name. `require_confirmation` is the approval gate.

    Note it is set on `issue_refund` and nothing else. The rule is simple and
    worth stating: **anything that writes, pays, emails, deletes or escalates
    gets confirmation.** A read does not. Getting this list wrong is how an
    injected instruction turns into a money movement.
    """
    return {
        "kb_search": FunctionTool(kb_search),
        "order_lookup": FunctionTool(order_lookup),
        "calculator": FunctionTool(calculator),
        "issue_refund": FunctionTool(issue_refund, require_confirmation=True),
    }


def tools_for(scopes: frozenset[str], tags: frozenset[str] | None = None) -> list[FunctionTool]:
    """The tools a given principal and agent may hold.

    Filtering the catalogue is not a security control on its own — the check in
    `before_tool_callback` is — but it is a correctness control and a cost
    control. A model cannot misuse a tool it was never shown, and every tool
    omitted is tokens saved on every step of every run.
    """
    all_tools = build_tools()
    return [
        tool
        for name, tool in sorted(all_tools.items())
        if TOOL_SCOPES.get(name, frozenset()) <= scopes
        and (tags is None or (TOOL_TAGS.get(name, frozenset()) & tags))
    ]
