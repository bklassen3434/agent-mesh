.PHONY: up down logs pipeline smoke wiki api types

# ── Local Docker Compose targets ────────────────────────────────────────────

up:
	docker compose up -d --build
	@echo "Waiting for healthchecks..."
	@docker compose ps

down:
	docker compose down

logs:
	docker compose logs -f

# Run one full pipeline cycle via the coordinator container.
# Starts the coordinator profile (which normally stays stopped), runs once, exits.
pipeline:
	docker compose run --rm \
		-e MESH_PIPELINE_CATEGORIES=$${MESH_PIPELINE_CATEGORIES:-cs.AI,cs.RO,cs.LG} \
		-e MESH_PIPELINE_MAX_PAPERS=$${MESH_PIPELINE_MAX_PAPERS:-10} \
		coordinator

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
