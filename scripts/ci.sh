#!/usr/bin/env bash
# Portable CI pipeline — runs the same checks as `make ci`.
# Use this on Windows where the local GNU make is too old to honor SHELL := bash.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }

# --- Python (apps/predictor) ---
step "ruff check"
( cd apps/predictor && uv run ruff check . )

step "ruff format --check"
( cd apps/predictor && uv run ruff format --check . )

step "mypy"
( cd apps/predictor && uv run mypy )

step "pytest"
( cd apps/predictor && uv run pytest )

# --- OpenAPI schema drift check ---
step "openapi schema drift"
( cd apps/predictor && uv run python scripts/dump_openapi.py )
git diff --exit-code packages/schemas/openapi.json apps/ui/src/api/openapi.json

# --- UI + schemas (pnpm workspaces) ---
step "pnpm -r lint"
pnpm -r lint

step "pnpm -r typecheck"
pnpm -r typecheck

step "pnpm -r test"
pnpm -r test

echo
echo "ci: ALL GREEN"
