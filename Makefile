UV ?= uv

UI_PORT ?= 8501
API_PORT ?= 8000
API_URL ?= http://127.0.0.1:$(API_PORT)

.PHONY: sync test lint fmt typecheck api ui ui-http dev fetch fixtures

sync:  ## Create/refresh the virtualenv from pyproject + uv.lock
	$(UV) sync

# Measured: 98.94% (45 of 4,256 statements), every non-Streamlit module at 100%. The gate
# sits just under the measurement so a refactor fails the build on lost coverage rather
# than on a rounding boundary.
COV_MIN ?= 98

test:  ## Run the test suite with coverage, gated
	$(UV) run pytest --cov=mdq --cov-report=term-missing --cov-fail-under=$(COV_MIN)

lint:  ## Lint + format check (no writes)
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt:  ## Format and auto-fix
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck:  ## mypy --strict over src/mdq
	$(UV) run mypy

api:  ## Run the FastAPI app (WP6)
	$(UV) run uvicorn mdq.api.app:app --reload --port $(API_PORT)

ui:  ## Run the dashboard standalone — service in-process, no API needed (WP7)
	$(UV) run streamlit run src/mdq/dashboard/app.py --server.port $(UI_PORT) -- --embedded

ui-http:  ## Run the dashboard as a client of a running `make api`
	$(UV) run streamlit run src/mdq/dashboard/app.py --server.port $(UI_PORT) -- --api-url $(API_URL)

dev:  ## Run the API and the dashboard together; Ctrl-C stops both
	@echo "API  -> $(API_URL)/docs"
	@echo "UI   -> http://127.0.0.1:$(UI_PORT)"
	@trap 'kill 0' INT TERM EXIT; \
	$(UV) run uvicorn mdq.api.app:app --port $(API_PORT) & \
	$(UV) run streamlit run src/mdq/dashboard/app.py --server.port $(UI_PORT) -- --api-url $(API_URL) & \
	wait

fetch:  ## Download + checksum-verify the pinned HuggingFace sample into data/raw
	$(UV) run python scripts/fetch_data.py --all

fixtures:  ## Regenerate tests/fixtures from data/raw (deterministic, <400 KB)
	$(UV) run python scripts/make_fixtures.py
