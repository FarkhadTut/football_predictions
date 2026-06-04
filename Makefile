# Football Predictor — root Makefile
#
# All recipes assume the dev environment has `uv`, `pnpm`, and Node 24 installed.
# Python deps are managed per-app via `uv sync` against `apps/predictor/pyproject.toml`.

# Force POSIX shell — keeps recipes portable and avoids cmd.exe codepage issues
# when the Windows username contains non-ASCII characters.
SHELL := bash

.PHONY: ci lint test typecheck py-lint py-test py-typecheck ui-lint ui-test ui-typecheck \
        dev dev-api dev-ui smoke-live migrate seed probes install clean

# ---- aggregate ----

ci: lint typecheck test

lint: py-lint ui-lint

test: py-test ui-test

typecheck: py-typecheck ui-typecheck

install:
	cd apps/predictor && uv sync --all-groups
	pnpm install --frozen-lockfile=false

# ---- python (apps/predictor) ----

py-lint:
	cd apps/predictor && uv run ruff check . && uv run ruff format --check .

py-test:
	cd apps/predictor && uv run pytest

py-typecheck:
	cd apps/predictor && uv run mypy

# ---- ui (apps/ui + packages/schemas) ----

ui-lint:
	pnpm -r lint

ui-test:
	pnpm -r test

ui-typecheck:
	pnpm -r typecheck

# ---- dev loops ----

dev-api:
	cd apps/predictor && uv run uvicorn predictor.api.app:app --reload

dev-ui:
	pnpm --filter ui dev

dev: ## Run API + UI side by side (Phase 1+; for now use dev-api or dev-ui)
	@echo "use 'make dev-api' or 'make dev-ui' for now"

# ---- data / ops ----

smoke-live:
	cd apps/predictor && uv run python scripts/smoke_live.py

migrate:
	cd apps/predictor && uv run alembic upgrade head

seed:
	cd apps/predictor && uv run python scripts/seed.py

probes:
	cd apps/predictor && uv run python scripts/probes/the_odds_api_probe.py
	cd apps/predictor && uv run python scripts/probes/soccerdata_probe.py

clean:
	rm -rf apps/predictor/.venv apps/predictor/.pytest_cache apps/predictor/.mypy_cache \
	       apps/predictor/.ruff_cache apps/ui/node_modules node_modules apps/ui/dist
