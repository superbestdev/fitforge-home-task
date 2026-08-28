# FitForge agentic support — everything runs locally, nothing costs money.
.DEFAULT_GOAL := help
SHELL := /bin/bash

COMPOSE := docker compose
RUN     := $(COMPOSE) run --rm --no-deps api

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

.PHONY: env
env: ## Create .env from the example if it does not exist
	@test -f .env || (cp .env.example .env && echo "created .env")

.PHONY: up
up: env ## Start the whole stack
	$(COMPOSE) up -d --build
	@echo "waiting for the api to become healthy..."
	@until curl -fsS http://localhost:8000/health >/dev/null 2>&1; do sleep 2; done
	@echo ""
	@echo "  chat      http://localhost:5173"
	@echo "  console   http://localhost:5173/console"
	@echo "  docs      http://localhost:5173/docs"
	@echo "  api docs  http://localhost:8000/docs"
	@echo ""
	@echo "Next: make models && make seed && make ingest"

.PHONY: models
models: ## Pull the local LLM + embedding models into Ollama (several GB)
	$(COMPOSE) up -d ollama
	@until $(COMPOSE) exec -T ollama ollama list >/dev/null 2>&1; do sleep 2; done
	$(COMPOSE) run --rm ollama-pull

.PHONY: down
down: ## Stop everything
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop everything and delete all data (destructive)
	$(COMPOSE) down -v
	rm -rf data/manuals/*.pdf data/ocr_cache/*.pdf

.PHONY: logs
logs: ## Tail the api logs
	$(COMPOSE) logs -f api

.PHONY: ps
ps: ## Show container status
	$(COMPOSE) ps

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

.PHONY: seed
seed: ## Generate the catalog, customers, orders, and the manual PDF corpus
	$(RUN) python -m seed.generate_catalog
	$(RUN) python -m seed.generate_manuals

.PHONY: ingest
ingest: ## Ingest manuals: classify -> OCR -> chunk -> extract -> embed -> index
	$(RUN) python -m services.ingest.pipeline

.PHONY: reingest
reingest: ## Rebuild the whole index from scratch
	$(RUN) python -m services.ingest.pipeline --reingest

.PHONY: coverage
coverage: ## Show what documentation the agent actually has
	@curl -s http://localhost:8000/api/coverage | python -m json.tool | head -40

# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

.PHONY: demo
demo: ## Scripted multi-issue session: two machines, two threads, one order
	$(RUN) python -m evals.demo_multi_issue

.PHONY: sample
sample: ## Generate a realistic sample manual PDF for upload testing
	$(RUN) python -m seed.generate_sample_manual

.PHONY: test
test: ## Run the test suite
	$(RUN) python -m pytest tests/ -q

.PHONY: eval
eval: ## Replay the golden sessions and report agent quality
	$(RUN) python -m evals.run_golden

.PHONY: e2e
e2e: ## Browser tests: drives both UIs in real Chromium (needs the stack up)
	cd web/e2e && npm install && npx playwright install chromium && npm run all

.PHONY: metrics
metrics: ## Print the production signals
	@curl -s http://localhost:8000/api/metrics | python -m json.tool

.PHONY: health
health: ## Deep health check across every dependency
	@curl -s http://localhost:8000/health/deep | python -m json.tool

# ---------------------------------------------------------------------------
# Optional
# ---------------------------------------------------------------------------

.PHONY: obs
obs: ## Start self-hosted Langfuse tracing (http://localhost:3000)
	$(COMPOSE) --profile obs up -d
	@echo "Langfuse: http://localhost:3000 — create a project, then put the"
	@echo "keys in .env and set LANGFUSE_ENABLED=true"

.PHONY: psql
psql: ## Open a database shell
	$(COMPOSE) exec postgres psql -U fitforge -d fitforge
