.PHONY: help venv install fixtures test demo demo-llm lint clean docker docker-servers zip

PY ?= python
QUERY ?= 给我生成一份关于 Pilbara 锂矿的今日简报

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv:  ## Create .venv
	$(PY) -m venv .venv

install:  ## Install runtime + dev dependencies
	$(PY) -m pip install -r requirements.txt pytest reportlab
	$(PY) -m pip install -e .

fixtures:  ## Regenerate the synthetic fixture set (deterministic)
	$(PY) scripts/make_fixtures.py

test:  ## Run the test suite
	$(PY) -m pytest -q

demo:  ## Run the offline brief with the deterministic planner
	MINING_AGENT_DATA_MODE=offline $(PY) -m mining_agent "$(QUERY)" --no-llm --json --out output/briefing.md

demo-llm:  ## Run the brief with the ReAct planner (needs ANTHROPIC_API_KEY)
	$(PY) -m mining_agent "$(QUERY)" --json --print --out output/briefing.md

lint:  ## Byte-compile everything as a cheap syntax check
	$(PY) -m compileall -q src scripts tests

clean:  ## Remove build artifacts and generated output
	rm -rf output/*.md output/*.json .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

docker:  ## Build and run the full stack (3 servers + agent)
	docker compose up --build

docker-servers:  ## Run only the three MCP servers over SSE on :8001-8003
	docker compose up --build mining-news-mcp mineral-pdf-mcp lme-price-mcp

zip:  ## Package the deliverable
	$(PY) scripts/package.py
