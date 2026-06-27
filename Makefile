.PHONY: up down logs controller controller-apply wiki api types types-check check hooks wiki-install \
	pi-up pi-down pi-pipeline \
	test test-ui test-ui-headed test-ui-debug test-ui-report

# ── Local Docker Compose targets ────────────────────────────────────────────

up:
	docker compose up -d --build --remove-orphans
	@echo "Waiting for healthchecks..."
	@docker compose ps

down:
	docker compose down --remove-orphans

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
	docker compose up -d --build --remove-orphans
	@docker compose ps

pi-down:
	docker compose down --remove-orphans

# One bounded controller round (shadow → use controller-apply to act).
pi-pipeline:
	docker compose run --rm --no-deps \
		--entrypoint "uv run mesh-controller --apply" controller

# ── Wiki / API convenience ──────────────────────────────────────────────────

wiki:
	@open http://localhost:3000

api:
	@open http://localhost:8000/docs

# Regenerate apps/wiki/src/lib/api-types.ts from the API's OpenAPI spec.
# Self-contained (mirrors CI): reuses an API already running on :8000 if present,
# otherwise boots a throwaway one, regenerates, and stops it. Run after ANY change
# to apps/api handlers, response models, or Pydantic schemas.
types:
	@if curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then \
		echo "→ using the API already running on :8000"; \
		( cd apps/wiki && npm run generate-types ); \
	else \
		echo "→ booting a throwaway API on :8000..."; \
		mkdir -p ./data; \
		API_HOST=127.0.0.1 API_PORT=8000 uv run mesh-api >/tmp/mesh-api-types.log 2>&1 & \
		api_pid=$$!; \
		for i in $$(seq 1 30); do curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break; sleep 1; done; \
		if ! curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then \
			echo "API failed to start (see /tmp/mesh-api-types.log)"; kill $$api_pid 2>/dev/null; exit 1; fi; \
		( cd apps/wiki && npm run generate-types ); rc=$$?; \
		kill $$api_pid 2>/dev/null; \
		exit $$rc; \
	fi

# Drift guard (the same check CI runs): regenerate the types and fail if the
# checked-in file changed. Run this — or `make check` — before pushing API changes.
types-check: types
	@git diff --exit-code apps/wiki/src/lib/api-types.ts \
		|| { echo "ERROR: api-types.ts was stale — the regenerated file above is now staged for commit."; exit 1; }

# ── Local CI gate ────────────────────────────────────────────────────────────

# One-time per clone: activate the committed git hooks (.githooks/). The pre-push
# hook runs `make types-check` when apps/api changed and `make test-ui` when
# apps/wiki changed, so API↔wiki drift is caught before it reaches CI. Bypass an
# individual push with `git push --no-verify`.
hooks:
	chmod +x .githooks/*
	git config core.hooksPath .githooks
	@echo "Git hooks activated (core.hooksPath=.githooks). pre-push now guards API↔wiki drift."

# Install the wiki's npm deps (needed by lint/typecheck/build/test-ui/types).
wiki-install:
	cd apps/wiki && npm ci --no-audit --no-fund

# Full local mirror of CI — run before pushing, especially for API or wiki
# changes. Order matches .github/workflows/ci.yml: the Python gate, the API→wiki
# type-contract drift guard, then the wiki lint/typecheck/build + E2E. Catches
# both classes of cross-cutting drift (stale api-types.ts, stale wiki tests) that
# the Python-only gate misses. Assumes `make wiki-install` has been run and
# Playwright browsers are present (`cd apps/wiki && npx playwright install chromium`).
check:
	uv run ruff check .
	uv run mypy .
	uv run pytest
	$(MAKE) types-check
	cd apps/wiki && npm run lint && npm run typecheck && npm run build
	$(MAKE) test-ui

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
