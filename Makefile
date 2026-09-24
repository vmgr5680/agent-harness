.DEFAULT_GOAL := help
PY      := .venv/bin/python
PIP     := .venv/bin/pip
ADK     := .venv/bin/adk
PYTHON  ?= python3.12

.PHONY: help venv install test cov live-test lint fmt type check demo learn ask \
        evals evalset models web web-offline serve api-smoke docker up down clean \
        tools health adk-run adk-eval

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv (Python 3.12+)
	$(PYTHON) -m venv .venv && $(PIP) install --upgrade pip

install: venv ## Install the package with dev and api extras
	$(PIP) install -e ".[dev,api]"

# The suite never calls a paid API. GOOGLE_API_KEY is unset explicitly so that
# a developer with one exported does not silently start billing themselves.
test: ## Run the test suite (offline, no key needed)
	env -u GOOGLE_API_KEY -u GEMINI_API_KEY $(PY) -m pytest -q

cov: ## Run the suite with coverage
	env -u GOOGLE_API_KEY -u GEMINI_API_KEY $(PY) -m pytest -q --cov=agent_harness --cov-report=term-missing

live-test: ## Run the live Gemini smoke test (needs GOOGLE_API_KEY; costs a fraction of a cent)
	$(PY) -m pytest -q -m live

lint: ## Lint
	$(PY) -m ruff check src tests learning adk_agents

fmt: ## Format
	$(PY) -m ruff format src tests learning adk_agents

type: ## Type-check
	$(PY) -m mypy

check: lint type test ## Everything CI runs

demo: ## Tour of the whole system (offline unless a key is set)
	AH_LOG_LEVEL=WARNING AH_LOG_FORMAT=text $(PY) -m agent_harness.cli demo

learn: ## Run the 80-line ADK learning example
	$(PY) learning/minimal_agent.py

ask: ## make ask Q="your question"
	AH_LOG_LEVEL=WARNING AH_LOG_FORMAT=text $(PY) -m agent_harness.cli ask "$(Q)"

models: ## List the models your key can actually call
	$(PY) -m agent_harness.cli models --filter flash

tools: ## Show every tool with its scopes, tags and approval requirement
	$(PY) -m agent_harness.cli tools

health: ## Resolved configuration: model, price book, guardrail mode, sessions
	AH_LOG_LEVEL=WARNING AH_LOG_FORMAT=text $(PY) -m agent_harness.cli health

# AH_RATE_LIMIT_RPM=0 is deliberate. The limiter is a per-tenant control sized
# for interactive traffic; an eval suite is a batch job that fires every case at
# once and will exhaust the bucket, turning real passes into "exhausted" runs.
# Leaving it on here is the first self-inflicted eval failure most teams hit.
evals: ## Run the governance eval suite and write a report
	AH_LOG_LEVEL=WARNING AH_LOG_FORMAT=text AH_RATE_LIMIT_RPM=0 \
	  $(PY) -m agent_harness.cli evals evals/support.jsonl --report var/report.json

evalset: ## Export the suite for ADK's own evaluator (writes var/test_config.json too)
	$(PY) -m agent_harness.cli export-evalset evals/support.jsonl var/support.evalset.json

# ADK's metrics score the *model's* behaviour, so this one wants a real key.
# Offline it reports 1/8: the deterministic fixture model does not reproduce
# the tool trajectory the cases pin, which is the point of a fixture.
# AH_RATE_LIMIT_RPM=0 for the same reason `make evals` sets it — see below.
adk-eval: evalset ## Score the exported suite with ADK's own evaluator
	AH_RATE_LIMIT_RPM=0 $(ADK) eval adk_agents/support var/support.evalset.json \
	  --print_detailed_results --log_level error

# `adk web` and `adk run` load .env themselves, walking up from the agent
# folder. This package's own CLI does not. So these two go live the moment a
# key is in .env, while `make ask` stays offline until you source it — set
# ADK_DISABLE_LOAD_DOTENV=1 to hold them offline.
web: ## ADK's browser UI: every concept app in one picker, with the event trace
	$(ADK) web adk_agents

web-offline: ## The same picker, pinned to the offline model whatever is in .env
	ADK_DISABLE_LOAD_DOTENV=1 $(ADK) web adk_agents

adk-run: ## ADK's terminal chat against the same agent and plugin
	$(ADK) run adk_agents/support --log_level error

serve: ## Run the HTTP API on :8000
	$(PY) -m agent_harness.cli serve

api-smoke: ## Curl the running API (needs `make serve` in another shell)
	curl -sS -X POST localhost:8000/v1/agent/run \
	  -H 'content-type: application/json' \
	  -H 'authorization: Bearer dev-token-abc' \
	  -d '{"question":"What is the refund window for a delivered order?"}' | $(PY) -m json.tool

docker: ## Build the image
	docker build -t agent-harness:0.2.0 .

up: ## docker compose up
	docker compose up --build

down: ## docker compose down
	docker compose down -v

clean: ## Remove build and test artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage var
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
