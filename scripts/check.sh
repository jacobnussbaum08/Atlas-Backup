#!/usr/bin/env bash
# Run every automated check: backend lint, types, tests; frontend types, tests, build.
#
#   scripts/check.sh
#
# Backend integration tests run when ATLAS_TEST_DATABASE_URL (and optionally
# ATLAS_TEST_REDIS_URL) are set; otherwise they are reported as skipped.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

echo "==> backend: ruff"
uv run ruff format --check backend
uv run ruff check backend

echo "==> backend: mypy (strict)"
uv run mypy

echo "==> backend: pytest"
uv run pytest

echo "==> frontend: typecheck, tests, build"
cd "$root/frontend"
pnpm install --frozen-lockfile
pnpm typecheck
pnpm test
pnpm build

echo "All checks passed."
