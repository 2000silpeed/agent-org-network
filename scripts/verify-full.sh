#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repository_root"

uv run pytest -q
uv run pyright
uv run ruff check .
(
  cd frontend
  corepack pnpm test
  corepack pnpm exec tsc --noEmit
  corepack pnpm lint
  corepack pnpm build
)
