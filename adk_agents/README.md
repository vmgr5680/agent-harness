# One concept at a time, in the browser

```bash
make web            # adk web adk_agents
```

Open <http://localhost:8000/dev-ui/>. The agent picker lists every folder in
this directory, and the prefix tells you which article it belongs to:

| Prefix | Article | What changes between apps |
|---|---|---|
| `cNN_*` | **Part 1 — The Agent Harness** | one **control** turned on or off, same support agent |
| `pNN_*` | **Part 2 — Agentic Patterns** | the **shape** of the agent (chain, router, fan-out, loop), same controls |
| `support` | both | the whole system at once |

Same repo, same `GovernancePlugin`, same scope tables. Part 2 exists to show
that the harness does not care what shape the agent is.

Pick an app, paste its question, read the event trace. Then pick the app below
it and ask the same thing.

> **Stay offline.** `adk web` loads `.env` by itself, walking up from this
> folder, so it goes live the moment a key is in there. `ADK_DISABLE_LOAD_DOTENV=1 make web`
> holds it on the offline model. Everything below was recorded offline.

---

## Part 1 — the harness, one control at a time

Each app is the same system with **one concept turned on or off**, so a
control fires where you can watch it instead of five firing at once.

| App | Runbook | Ask it | What to look for |
|---|---|---|---|
| **support** | §0 | What is the status of order ORD-10021? | redacted tool output, a citation, and a cost in the ledger |
| **c01_budget_ceiling** | §1 | What is the refund window for a delivered order? | the final event is the cost-ceiling admission, not an answer |
| **c02_tool_scopes** | §2 | Refund order ORD-10021 for 249 dollars. | the agent's tool list has no issue_refund, and nothing was denied |
| **c03_subagent_researcher** | §3 | Refund order ORD-10021 for 249 dollars. | kb_search and calculator only — no order tool, no write tool |
| **c04_subagent_analyst** | §3 | What is the status of order ORD-10021? | no functionCall event at all; then ask it for 249.00 * 0.15 |
| **c05_guardrails_off** | §4 | What is the status of order ORD-10021? | the card number and email are visible in the functionResponse event |
| **c06_guardrails_shadow** | §4 | Use my key AIzaSyB1c2D3e4F5g6H7i8J9k0L1m2N3o4P5q6R to look up my order. | the run completes; the terminal logs would_action=block shadowed=true |
| **c07_guardrails_enforce** | §4 | Use my key AIzaSyB1c2D3e4F5g6H7i8J9k0L1m2N3o4P5q6R to look up my order. | blocked with no llm_request event at all; cost $0.000000 |
| **c08_injection_in_tool_output** | §4 | What is the status of order ORD-10077? | the tool result is withheld from the model; no refund is attempted |
| **c09_human_approval** | §5 | Refund order ORD-10021 for 249 dollars. | an approval prompt carrying the exact arguments; the run parks |
| **c10_rate_limit** | §6 | What is the refund window for a delivered order? | ask twice: the second run is refused before the model is called |

Each folder's `agent.py` is about ten lines of configuration under a docstring
explaining what you are looking at. Read the docstring; that is where the
concept is written down. The runbook column points at
[`docs/RUNBOOK.md`](../docs/RUNBOOK.md), which shows the same concept from the
CLI, with output.

---

## Part 2 — agentic patterns, one shape at a time

Each app is a `google.adk.workflow.Workflow` graph built in
[`agents/patterns.py`](../src/agent_harness/agents/patterns.py). ADK 2.9
deprecates `SequentialAgent`, `ParallelAgent` and `LoopAgent` in favour of
this graph, so these use it. Every routing and review decision that code can
make is a plain Python function node, not a model call.

| App | Pattern | Ask it | What to look for |
|---|---|---|---|
| **p01_chain** | prompt chaining + code gate | What is the status of order ORD-10021? | lookup, then the gate, then a writer that holds no tools |
| **p02_router** | routing | What is the refund window for a delivered order? | a route event and no model call before it; only the policy branch runs |
| **p03_fanout** | parallelisation | Can I still get a refund on order ORD-10021? | order_lookup and kb_search in flight together, both screened, then one writer |
| **p04_review_loop** | evaluator-optimizer | What is the refund window for a delivered order? | a cited draft routes to publish; an uncited one goes back, at most 3 times |

Second questions worth asking:

- `p01_chain` — *What is the status of order ORD-99999?* The gate reads the
  tool result (not the prose) and routes to
  `not_found` and no writer runs: a fluent reply about a missing order is
  stopped before it is paid for.
- `p02_router` — *Write me a poem about cats.* Declined with **0 model calls
  and $0.000000**. The decision was a regular expression.
- `p04_review_loop` — *Tell me something nice.* Nothing to cite, so
  `revise`, `revise`, `give_up`, and an honest message instead of a fourth try.

Orchestrator-workers and human-in-the-loop are also patterns, and Part 1
already has them: `support` (with `c03`/`c04`) is orchestrator-workers through
`AgentTool`, and `c09_human_approval` is the approval gate.

---

## Three Part 1 pairs worth running back to back

**`c05_guardrails_off` → `c07_guardrails_enforce`.** Same question, same agent.
Expand the `order_lookup` **functionResponse** in each and compare:

```
off:      'customer_email': 'dana.whitfield@example.com', 'payment_card': '4242 4242 4242 4242'
enforce:  'customer_email': '[REDACTED:email]',           'payment_card': '[REDACTED:card_number]'
```

The redaction is in the event the model was **given**, not something applied to
the answer afterwards. The model never saw the card, so it could not have
echoed it. Nobody types a card number; the billing API returns one.

**`c07_guardrails_enforce` → `c08_injection_in_tool_output`.** Type
*"Ignore all previous instructions…"* at `c07` yourself: the run completes and
the finding is recorded. Ask `c08` about **ORD-10077**, whose support note
contains that same sentence, and the tool result is withheld from the model
entirely. Identical text, different surface, different verdict — and the
asymmetry is deliberate. A user can see what they typed. Nobody can see what
the billing API returned.

**`c02_tool_scopes` → `c09_human_approval`.** Ask both for a refund. One never
had the tool; the other stops and shows you `issue_refund(order_id='ORD-10021',
amount_usd=249.0)` and waits. Approving "a refund" is not a control. Approving
those numbers is.

---

## What a concept folder cannot change

**The model-call ceiling.** `AH_MAX_MODEL_CALLS` reaches ADK through
`RunConfig(max_llm_calls=…)`, which the harness builds per run. Under
`adk web` the `RunConfig` belongs to ADK's server, not to the app — it reads
`ADK_MAX_LLM_CALLS` from the process environment instead. So that one is a
server restart rather than an entry in the picker:

```bash
ADK_MAX_LLM_CALLS=1 ADK_DISABLE_LOAD_DOTENV=1 make web
```

**The wall-clock ceiling**, for the same reason: `asyncio.wait_for` is in
`AgentHarness.run_async`, and `adk web` does not go through it. Both ceilings
are demonstrated from the CLI in [RUNBOOK §1](../docs/RUNBOOK.md).

That gap is worth knowing rather than papering over: **a control that lives in
one front door is not a control.** The three that do apply everywhere —
guardrails, scopes and spend — are in the plugin, which is registered on the
Runner and cannot be omitted from a subtree.

---

## Scopes here are the agent's own

None of these apps has a bearer token to derive privileges from, so each runs
with its agent spec's full designed scopes (or the narrower set its concept is
about). That is right for a local dev UI and wrong for anything else — the HTTP
API resolves scopes from the caller's token instead, and never lets the caller
state its own. See [RUNBOOK §9](../docs/RUNBOOK.md).

---

## Adding one

```python
from agent_harness.adk_app import Concept, build_app  # Part 2: build_pattern_app

CONCEPT = Concept(
    name="c11_your_concept",  # must equal the folder name
    title="...",
    ask="...",  # the question that makes it fire
    look_for="...",  # the one thing in the trace
    runbook="§4",
)

app = build_app(CONCEPT, env={"AH_SOMETHING": "..."})
root_agent = app.root_agent
```

Plus a two-line `__init__.py` (`from . import agent`) and a row in the table
above. [`tests/test_adk_concepts.py`](../tests/test_adk_concepts.py) drives
every folder the way ADK's server does and asserts the thing its docstring
promises, so a demonstration that quietly stopped demonstrating anything fails
there rather than in front of someone learning from it.
