.PHONY: up down logs pipeline skeptic consolidate belief-consolidate smoke wiki api types \
	test test-ui test-ui-headed test-ui-debug test-ui-report

# ── Local Docker Compose targets ────────────────────────────────────────────

up:
	docker compose up -d --build
	@echo "Waiting for healthchecks..."
	@docker compose ps

down:
	docker compose --profile skeptic --profile scheduler down --remove-orphans

logs:
	docker compose logs -f

# Run one full pipeline cycle via the coordinator container.
# Starts the coordinator profile (which normally stays stopped), runs once, exits.
pipeline:
	docker compose build coordinator
	docker compose run --rm \
		-e MESH_PIPELINE_CATEGORIES=$${MESH_PIPELINE_CATEGORIES:-cs.AI,cs.RO,cs.LG} \
		-e MESH_PIPELINE_MAX_PAPERS=$${MESH_PIPELINE_MAX_PAPERS:-10} \
		coordinator

# Run one falsification sweep — Curator picks beliefs worth challenging,
# Skeptic assesses each, the orchestrator writes counter-claims + revisions.
# Activates the skeptic profile (curator + skeptic + skeptic-sweep) which is
# excluded from the default `make up`.
skeptic:
	docker compose --profile skeptic up -d --build curator skeptic
	docker compose build skeptic-sweep
	docker compose --profile skeptic run --rm skeptic-sweep

# Run one memory-consolidation cycle — distills recent episodic history into
# procedural heuristics via the batch API. Needs only Postgres + an LLM key, so
# it reuses the skeptic-sweep job container (same coordinator image, has the
# writer + LLM env) with the entry point overridden and --no-deps, rather than
# adding a new service. Requires `make up` (mesh-postgres) first.
consolidate:
	docker compose --profile skeptic build skeptic-sweep
	docker compose --profile skeptic run --rm --no-deps \
		--entrypoint "uv run mesh-consolidate" skeptic-sweep

belief-consolidate:
	docker compose --profile skeptic build skeptic-sweep
	docker compose --profile skeptic run --rm --no-deps \
		--entrypoint "uv run mesh-belief-consolidate" skeptic-sweep

# Smoke test: bring up the stack, run one pipeline cycle, check row counts.
smoke: up
	@echo "Running smoke pipeline..."
	$(MAKE) pipeline
	@echo ""
	@echo "Checking DB row counts..."
	uv run mesh.cli pipeline-stats --last 1
	@echo ""
	@echo "Checking A2A discovery..."
	uv run mesh.cli a2a-discover \
		--agent-urls "http://localhost:8001,http://localhost:8002,http://localhost:8003,http://localhost:8004"
	@echo "Smoke test complete."

# ── Wiki / API convenience ──────────────────────────────────────────────────

wiki:
	@open http://localhost:3000

api:
	@open http://localhost:8000/docs

# Regenerate apps/wiki/src/lib/api-types.ts from a running API at :8000.
# Errors out if the API isn't reachable — boot it via `make up` first.
types:
	@cd apps/wiki && npm run generate-types

# ── Tests ───────────────────────────────────────────────────────────────────

# Full suite: Python unit tests + wiki Playwright E2E.
test:
	uv run pytest
	$(MAKE) test-ui

# Wiki E2E. Playwright boots a mock API + a production wiki build itself
# (see apps/wiki/playwright.config.ts) — no docker-compose stack needed.
test-ui:
	cd apps/wiki && npx playwright test

test-ui-headed:
	cd apps/wiki && npx playwright test --headed

test-ui-debug:
	cd apps/wiki && npx playwright test --debug

test-ui-report:
	cd apps/wiki && npx playwright show-report
