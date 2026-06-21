#!/usr/bin/env bash
# owner 워커 프로세스를 띄운다 — 중앙에 아웃바운드 WS로 붙어 PushWork를 받아
# 로컬 claude(ClaudeCodeRuntime)로 답하고 SubmitAnswer로 회신한다.
# T6.3 슬라이스2b-ii — ADR 0011 결정 6. 실 claude 인증 전제(로컬 claude 로그인).
#
# 사용:  scripts/run_worker.sh <OWNER_ID> [PORT]
# OWNER_ID 예: cs_lead | legal_lead | finance_lead (데모 카드의 owner)
# 기본 포트 8000. 한 owner = 한 워커(여러 owner면 이 스크립트를 여러 번 띄운다).
set -euo pipefail
cd "$(dirname "$0")/.."

OWNER="${1:?owner를 지정하세요: scripts/run_worker.sh <OWNER_ID> [PORT]}"
PORT="${2:-8000}"
URL="ws://127.0.0.1:${PORT}/worker"

echo "[worker] owner=${OWNER}  ->  ${URL}"
exec uv run python -m agent_org_network.worker --owner "${OWNER}" --url "${URL}"
