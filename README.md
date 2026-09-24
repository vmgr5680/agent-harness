# agent-harness

**Google ADK runs the agent. This is everything ADK does not do for you.**

A production-oriented reference implementation of the governance layer around a
[Google Agent Development Kit](https://google.github.io/adk-docs/) agent:
guardrails on four surfaces, tool scopes, cost ceilings, human approval, and an
eval suite that gates a deploy.

Built with **ADK 2.9** and **Gemini 3.1 Flash-Lite**. Runs **fully offline with
no API key**; set `GOOGLE_API_KEY` and the identical code runs against Gemini.

---

## The division of labour

This is the whole argument of the project, so it goes first.

| Concept | ADK gives you | You still own |
|---|---|---|
| **Agentic loop** | `LlmAgent` + `Runner`, `max_llm_calls` ceiling | a wall-clock and a **spend** ceiling; an honest message when a run stops |
| **Tools / MCP** | `FunctionTool` derives the schema from your signature; `McpToolset`; invalid args go back to the model | **scopes**, output classification, which tools a given caller even sees |
| **Sub-agents** | `AgentTool`, transfer, isolated context | **privilege narrowing** — the child that reads untrusted content gets no write scopes |
| **Model access** | provider resolution, streaming; retries **only if configured** (`retry_options` is off by default) | turning retries on (`agents/models.py::RETRY`), routing by role, per-tenant rate limiting |
| **Economics** | `usage_metadata` per call | prices, a **cost ledger**, budget ceilings, cost as a test assertion |
| **Evals** | `tool_trajectory_avg_score`, `hallucinations_v1`, `safety_v1`, rubric judges | behavioural, authorisation and cost assertions; the **deploy gate** |
| **Guardrails** | callback hooks, and nothing in them | **all of it** — four inbound surfaces, one outbound |
| **Observability** | OpenTelemetry spans, session event history | cost per agent, business metrics, Prometheus |
| **Human in the loop** | `require_confirmation=True`, suspend and resume | who may approve, and the audit record of who did |

Everything in the right-hand column arrives through **one
[`GovernancePlugin`](src/agent_harness/runtime/plugin.py)** registered on the
Runner, so it cannot be forgotten on a subtree.

> There is no hand-rolled loop, planner, tool registry, retry policy, session
> store or LLM judge in this repository. There used to be. ADK replaced all of
> them and they were **deleted**, not kept alongside.

---

## Quick start

```bash
make install     # Python 3.12+
make test        # 185 tests, ~1.6s, no network, no key
make demo        # a tour of all of it
make learn       # the 80-line version, which shows what leaks without this layer
```

`make demo` fires every control at once, which is a good trailer and a bad way
to learn. [`docs/RUNBOOK.md`](docs/RUNBOOK.md) takes the same system apart one
concept at a time, each with the CLI command, the ADK-web equivalent, and the
one line of output that is the point.

### Against a real model

```bash
cp .env.example .env
# put your AI Studio key in GOOGLE_API_KEY  →  https://aistudio.google.com/apikey
set -a && source .env && set +a

make models                 # list what your key can actually call
make ask Q="What is the refund window for a delivered order?"
```

`make models` exists because model identifiers move faster than documentation.
"The id in the tutorial no longer exists" should be a list, not a debugging
session.

### As a library

```python
from agent_harness import AgentHarness, RunRequest

harness = AgentHarness()
result = harness.run(RunRequest(
    question="What is the refund window for a delivered order?",
    tenant_id="acme",
    scopes=frozenset({"kb.read", "orders.read"}),
))
print(result.answer, result.status.value, result.cost_usd)
```

### With ADK's own tooling

```bash
make web                                   # adk web adk_agents — the event trace UI
make adk-run                               # adk run adk_agents/support — terminal chat
make adk-eval                              # export + adk eval; needs `pip install "google-adk[eval]"`
```

[`adk_agents/support/agent.py`](adk_agents/support/agent.py) exposes the same
agents **with the governance plugin attached**. Without it, `adk web` would run
your agents with no guardrails, no scope checks and no cost ceiling — which is
exactly the half-configured path that reaches production by accident.

**One concept at a time, in the picker.** `support` is the whole system firing
at once, which is the right default and a poor way to learn. Every other folder
in [`adk_agents/`](adk_agents/README.md) is the same system with exactly one
thing changed, so `adk web` lists them side by side:

| App | Ask it | And see |
|---|---|---|
| `c02_tool_scopes` | Refund order ORD-10021 for 249 dollars. | the refund tool was never built |
| `c05_guardrails_off` | What is the status of order ORD-10021? | the card number, in full |
| `c07_guardrails_enforce` | *the same question* | `[REDACTED:card_number]` |
| `c08_injection_in_tool_output` | What is the status of order ORD-10077? | a poisoned record withheld from the model |
| `c09_human_approval` | Refund order ORD-10021 for 249 dollars. | a gate, carrying the exact arguments |

Eleven apps in all, catalogued in
[`adk_agents/README.md`](adk_agents/README.md); each one's `agent.py` is ten
lines of configuration under a docstring that says what to look for.
[`tests/test_adk_concepts.py`](tests/test_adk_concepts.py) drives every one of
them the way ADK's server does and asserts the thing its docstring promises,
because a demonstration that quietly stopped demonstrating anything is worse
than none.

> **The two front doors resolve configuration differently.** `adk web`,
> `adk run` and `adk eval` load `.env` themselves, walking up from the agent
> folder. This package's CLI reads `os.environ` and nothing else. So the ADK
> tooling goes live the moment a key is in `.env`, while `make ask` stays
> offline until you `source` it. `ADK_DISABLE_LOAD_DOTENV=1` holds the ADK side
> offline.

---

## What `make demo` shows

Real output, offline:

```
A grounded answer, with citations
  status=completed  model_calls=2  cost=$0.001574  tools=kb_search

Tool output carrying personal data
  ... "customer_email": "[REDACTED:email]", "payment_card": "[REDACTED:card_number]" ...
  status=completed  model_calls=2  guardrail: card_number, email

A write that stops for a human
  This action needs approval before it can run: issue_refund.
  status=needs_approval  tools=order_lookup
  → approved by a human; executed ['issue_refund']

A credential in the prompt
  status=blocked  model_calls=0  cost=$0.000000
  guardrail: google_api_key

An injection attempt
  status=completed  guardrail: instruction_override, system_prompt_probe
```

The last two are the interesting ones.

The credential is **blocked before a single model call**, so a leaked key costs
nothing and produces an alert. The injection attempt is **flagged, not
blocked** — it came from the user, where injection-shaped text is usually
curiosity, and blocking on a heuristic teaches people to route around your
safety system. The identical text arriving inside a *retrieved document* is
blocked, and
[`test_an_injection_inside_a_tool_result_is_withheld_from_the_model`](tests/test_runtime.py)
pins that asymmetry.

---

## The four guardrail surfaces

The mistake almost everyone makes is putting a rail in one place: the user's
input. There are four inbound surfaces, and the user's typing is the one an
attacker is *least* likely to use, because it is the one the user can see.

```
user input      ─┐   surface=user
retrieved doc   ─┤   surface=tool_output      ← where the attack arrives
tool response   ─┼─► RAILS ─► model context ─► answer ─► RAILS ─► human
agent memory    ─┘   surface=memory                     surface=model_output
```

**Nobody types a card number. The billing API returns one.** That is why the
most important hook in the plugin is `after_tool_callback`, not
`on_user_message_callback`.

The same text gets a different verdict on a different surface:

| Content | From a user | From a tool result |
|---|---|---|
| *"Ignore all previous instructions…"* | **Flagged** | **Blocked** |
| A live Social Security number | **Blocked** | **Redacted** — that is what the tool is for |
| An API key | **Blocked** either way, never redacted | |

A credential is blocked rather than redacted because silently stripping it
means nobody ever learns it leaked, so it never gets rotated. Blocking is the
only action that produces the alert.

Roll out `off` → `shadow` → `enforce`. Shipping straight to `enforce` is the
single most common way a guardrail programme dies.

---

## Termination: three ceilings

An agent loop that can fail to terminate is an incident waiting for traffic.

| Ceiling | Enforced by | Setting |
|---|---|---|
| Model calls | **ADK** (`RunConfig.max_llm_calls`) | `AH_MAX_MODEL_CALLS` |
| Wall clock | the harness (`asyncio.wait_for`) | `AH_RUN_TIMEOUT_S` |
| Spend | the harness (`before_model_callback`) | `AH_BUDGET_USD_PER_RUN` |

All three end the run the same way — with an admission, not a partial answer
that reads exactly like a complete one:

```
I could not complete this request within its limit of 3 model calls.
Nothing has been changed.
```

---

## Human in the loop

`FunctionTool(issue_refund, require_confirmation=True)` is the whole gate.
ADK suspends the run, emits a confirmation request carrying the tool name and
the exact arguments, and resumes **on the same session** when a human decides —
so the agent continues with its full context rather than re-deriving it.

```
status: needs_approval | tool: issue_refund
      | args: {'order_id': 'ORD-10021', 'amount_usd': 249.0, 'reason': 'item arrived damaged'}
approved → status: completed | executed: ['issue_refund']
rejected → status: completed | executed: []
```

Verified live against Gemini 3.1 Flash-Lite, both directions.

This is the control that makes prompt injection survivable. An injected
instruction can reach the model; it still cannot move money.

---

## Evals: two suites, on purpose

**ADK evaluates quality.** `adk eval` ships `tool_trajectory_avg_score`,
`response_match_score`, `final_response_match_v2`, `hallucinations_v1`,
`safety_v1` and rubric judges. Reimplementing any of that would be strictly
worse than using the maintained version, so this project **deleted its
hand-rolled LLM judge** and exports the same cases instead:

```bash
make adk-eval     # export + `adk eval`, with the rate limiter off
```

It scores the *model's* tool trajectory, so it wants a real key — offline the
deterministic fixture model reports 1/8, which is a fixture behaving like one.
`make evalset` also writes `var/test_config.json`, pinning the criteria to
`tool_trajectory_avg_score`: ADK's defaults also demand `response_match_score`,
and the export asserts no reference response on purpose, so without that file
every case scores zero on a metric it was never given the data for.

**This suite evaluates governance** — the assertions ADK has no opinion about,
because they are about your policy rather than the model's output:

```bash
make evals
# 10/10 passed (100%)  mean cost $0.000041  ← offline-1 accounting, not a Gemini bill
```

| Assertion | Asks |
|---|---|
| `must_call` / `must_not_call` | did it use the right tools? |
| `must_not_execute` | did your **authorisation** hold? |
| `must_block` / `expect_status` | did the guardrails follow the policy? |
| `max_cost_usd` / `max_model_calls` | did a change quietly double the bill? |

`must_not_call` versus `must_not_execute` is the distinction worth
internalising: asserting an agent never *attempts* a forbidden tool tests the
model's restraint; asserting it never *executes* one tests your authorisation.
Only one of those is a control, and only one keeps passing when the model
changes.

Safety-tagged cases are absolute — one failure fails the suite whatever the
pass rate, because "95% of the time we do not leak the card number" is not a
passing grade.

> **Gotcha, and it will be your first one.** Your own per-tenant rate limiter
> will fail your eval suite. A suite fires every case at once; the limiter is
> sized for interactive traffic. `make evals` sets `AH_RATE_LIMIT_RPM=0`
> deliberately.

---

## Cost

ADK reports `usage_metadata`. It does not know what a token costs. The ledger
attributes every call to an agent and a tenant, which is the question a billing
export can never answer.

Two accounting bugs this implementation avoids, both of which silently
under-report:

- **Cached input tokens are billed at a discount**, so they are *subtracted*
  from the full-price count, not added on top.
- **Thinking tokens are billed as output.** Counting only
  `candidates_token_count` misses them.

An unknown model is charged at the **most expensive known rate** — loudly, on
purpose, so an unpriced model is visible rather than free:

```bash
agent-harness health   # → "model_price_known": false  means every figure is an over-estimate
```

Fix it with `AH_PRICING_JSON` from <https://ai.google.dev/pricing>. It is a
config change, not a deploy.

---

## Project layout

```
agent-harness/
├── adk_agents/support/agent.py   # entry point for `adk web` / `adk run` / `adk eval`
├── learning/minimal_agent.py     # 80 lines of ADK. LEARNING ONLY — and it leaks, visibly.
├── src/agent_harness/
│   ├── config.py                 # the only module that reads os.environ
│   ├── observability.py          # metrics + cost ledger (tracing is ADK's)
│   ├── agents/
│   │   ├── specs.py              # the least-privilege agent tree
│   │   ├── models.py             # model + credential resolution, model listing
│   │   └── offline.py            # OfflineLlm: a real BaseLlm, deterministic
│   ├── tools/
│   │   ├── domain.py             # pure functions, no framework imports
│   │   └── adk_tools.py          # FunctionTools + scopes, tags, write set
│   ├── guardrails/               # detectors, input/output rails, pipeline
│   ├── economics/                # pricing, rate limiting
│   ├── runtime/
│   │   ├── plugin.py             # ★ the governance layer, as one BasePlugin
│   │   ├── harness.py            # App + Runner + sessions + approvals
│   │   └── result.py             # a contract, not an event stream
│   ├── evals/                    # governance assertions + ADK evalset export
│   └── api/                      # FastAPI service
├── tests/                        # 185 tests, offline
└── docs/                         # ARCHITECTURE.md, SECURITY.md, prometheus.yml
```

---

## Technology choices

| Chosen | Why | Use instead when |
|---|---|---|
| **Google ADK** | Loop, tools, sub-agents, sessions, HITL and OTel, all maintained. The hand-rolled versions of these were ~1,200 lines and are now deleted | You are not on Gemini/Vertex and want a vendor-neutral runtime → LangGraph or a plain loop; the governance plugin's logic ports, its hooks do not |
| **`BasePlugin`, not per-agent callbacks** | Registered once on the Runner; cannot be omitted from a subtree. Governance you can forget on one agent is not governance | Never — per-agent callbacks are for agent-specific behaviour, not policy |
| **`AgentTool`, not `sub_agents` transfer** | Control returns to the parent with its budget and answer obligations intact; provenance stays clear | The child genuinely should own the rest of the conversation |
| **Gemini 3.1 Flash-Lite** | Cheap enough that a step ceiling is the binding constraint, not the bill | Harder reasoning → raise `AH_MODEL_DEEP` only; subagents stay cheap |
| **`OfflineLlm` as a real `BaseLlm`** | Tests exercise ADK's genuine loop, dispatch and HITL — only the weights are replaced | Never; a mocked Runner would test nothing |
| **ADK sessions as the audit log** | ADK already persists every event. A second log would drift from the first | You need tamper evidence → hash-chain an export; do not keep two logs |
| **ADK evals for quality, ours for governance** | Their metrics are maintained; ours encode policy they cannot know | — |
| **Regex detectors** | Fast, dependency-free, the right *first pass* | Real deployment → Presidio or a cloud DLP service behind `detectors.py` |
| **SQLite sessions** | One file, no operator. ADK 2.9's `DatabaseSessionService` is async-only, so the URL is `sqlite+aiosqlite:///…`, not the `sqlite:///…` you already know | More than one writer → Postgres (`postgresql+asyncpg://`); change `AH_SESSION_DB_URL`, nothing else |

---

## Known limits

Written down because a reference implementation that hides its edges is worse
than useless.

- **The price of `gemini-3.1-flash-lite` is not in the table.** I could not
  verify it, so it is charged at the fallback rate and `health` reports
  `model_price_known: false`. Set `AH_PRICING_JSON` before trusting any cost
  figure from a live run.
- **The prompt-injection detector is bypassable.** Every heuristic is. It is
  the third line of defence; least privilege and the approval gate are the
  first two.
- **The grounding rail is term overlap, not entailment.** It catches a
  confidently invented order status and misses a subtle misreading of a real
  one. `ALLOW`-with-findings by default for that reason.
- **The cost ledger and rate limiter are in process.** The per-tenant daily
  budget resets with the process. Move both behind Redis or the database before
  relying on them, and run one worker per container until you do.
- **`AH_SERVICE_TOKENS` is a development credential store.** Replace it with
  your identity provider; keep the shape, where the caller never states its own
  tenant or scopes.
- **`Dockerfile` and `docker-compose.yml` have not been built.** Docker was not
  available on the machine this was written on, so unlike everything else here
  they are reviewed but not executed.
- **Single-turn only.** Every eval case is one question. Real support
  conversations are five turns with pronouns, and that is where agents fail.

---

## Development

```bash
make check     # lint + type + test
make cov       # coverage
make live-test # the only test that spends money
```

Verified at the time of writing: **131 passed in 1.38s**, `ruff` clean,
`mypy --strict` clean across 31 files, live Gemini 3.1 Flash-Lite round trip
including the full approve-and-execute path.

MIT licensed. See [LICENSE](LICENSE).
