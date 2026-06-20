.PHONY: up down logs controller controller-apply wiki api types \
	pi-up pi-down pi-wiki pi-pipeline \
	test test-ui test-ui-headed test-ui-debug test-ui-report

# ── Local Docker Compose targets ────────────────────────────────────────────

up:
	docker compose up -d --build
	@echo "Waiting for healthchecks..."
	@docker compose ps

down:
	docker compose --profile controller --profile scheduler down --remove-orphans

logs:
	docker compose logs -f

# Deterministic controller — the sole orchestrator. It senses the field into
# tensions and runs the whole loop (scout → extract → resolve → synthesize →
# challenge → investigate + periodic belief/memory consolidation) under an
# explicit rule table. In-process: needs only Postgres + an LLM key, no agent
# stack. `make controller` previews one round's plan (shadow, writes nothing);
# `make controller-apply` acts and loops to quiescence.
controller:
	docker compose build controller
	docker compose run --rm --no-deps \
		--entrypoint "uv run mesh-controller" controller

controller-apply:
	docker compose build controller
	docker compose run --rm --no-deps \
		--entrypoint "uv run mesh-controller --apply" controller

# ── Raspberry Pi (4 GB) helpers — assume COMPOSE_FILE includes the overlay ──
pi-up:
	docker compose up -d --build
	@docker compose ps

pi-down:
	docker compose --profile ui --profile extra down --remove-orphans

# Browse the wiki on demand, then `docker compose stop wiki` to free the RAM.
pi-wiki:
	docker compose up -d wiki
	@echo "wiki → http://localhost:3000 (or the Pi's tailnet name)"

# One bounded controller round via the scheduler image.
pi-pipeline:
	docker compose run --rm --no-deps \
		--entrypoint "uv run mesh-controller --apply" scheduler

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
