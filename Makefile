.PHONY: dev dev-down dev-reset test test-unit test-integration e2e build migrate seed benchmark

COMPOSE := docker compose

dev:
	$(COMPOSE) up --build --wait
	@echo "Rainstone demo: http://localhost:5173"

dev-down:
	$(COMPOSE) down

dev-reset:
	$(COMPOSE) down --volumes
	$(COMPOSE) up --build --wait
	@echo "Rainstone demo reset: http://localhost:5173"

test: test-unit test-integration
	$(COMPOSE) run --rm frontend npm test -- --run
	$(COMPOSE) run --rm frontend npm run typecheck
	$(COMPOSE) run --rm frontend npm run lint

test-unit:
	$(COMPOSE) build backend
	$(COMPOSE) run --rm --no-deps backend pytest backend/tests/unit
	$(COMPOSE) run --rm --no-deps backend ruff check backend

test-integration:
	$(COMPOSE) --profile test up --build --abort-on-container-exit --exit-code-from integration-tests integration-tests

e2e:
	$(COMPOSE) --profile e2e up --build --abort-on-container-exit --exit-code-from e2e e2e

build:
	docker build --target runtime -t rainstone:local .

migrate:
	$(COMPOSE) run --rm init alembic upgrade head

seed:
	$(COMPOSE) run --rm init rainstone ingest-fixtures --path fixtures/phase1.json

benchmark:
	$(COMPOSE) exec -T backend python scripts/benchmark_reporting.py --jobs 100000
