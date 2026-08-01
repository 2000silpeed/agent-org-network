#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repository_root"

uv run ruff check .
uv run pytest -q \
  tests/test_a2a_remote_runtime.py \
  tests/test_support_contract.py \
  tests/test_central_bootstrap_admin.py \
  tests/test_central_question_intake.py \
  tests/test_smoke.py \
  tests/test_question_request.py \
  tests/test_question_request_sqlite.py \
  tests/test_question_resolution_application.py \
  tests/test_auth.py \
  tests/test_security_regression.py \
  tests/test_ci_workflow.py
(
  cd frontend
  corepack pnpm test
  corepack pnpm exec tsc --noEmit
  corepack pnpm lint
)
