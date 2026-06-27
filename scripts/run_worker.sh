#!/usr/bin/env bash
# owner 워커 프로세스를 띄운다 — 중앙에 아웃바운드 WS로 붙어 PushWork를 받아
# 로컬 claude(ClaudeCodeRuntime)로 답하고 SubmitAnswer로 회신한다.
# T6.3 슬라이스2b-ii(primary) · T6.6 슬라이스 iv(backup) — ADR 0011 결정 6, ADR 0012 결정 2.
# 실 claude 인증 전제(로컬 claude 로그인).
#
# 사용:  scripts/run_worker.sh <OWNER_ID> [ROLE] [PORT] [CENTRAL_HOST]
# OWNER_ID 예: cs_lead | legal_lead | finance_lead (데모 카드의 owner)
# ROLE        : primary(기본) | backup  — backup은 owner 위임 격리 백업(ADR 0012)
# PORT        : 중앙 포트(기본 8000)
# CENTRAL_HOST: 중앙 기기 호스트/IP(기본 127.0.0.1). 다른 기기(LAN)면 중앙의 LAN IP를 준다:
#               scripts/run_worker.sh cs_lead primary 8000 192.168.0.10
# 한 owner = 한 (등급)워커. primary와 backup을 따로 띄울 수 있다
# (예: primary 끄고 backup만 두면 backup이 그 owner 작업을 받아 답한다 → 처리함 검토).
#
# 답 생성 런타임 선택(env AON_PROVIDER, ADR 0027 T9.6 — 게이트 밖 수동):
#   미설정(기본)        : ClaudeCodeRuntime — `claude -p` 서브프로세스(기존 동작·무변경).
#   AON_PROVIDER=claude-api : ClaudeApiRuntime — owner OAuth 인프로세스 anthropic SDK 스트리밍
#                           (프로세스 스폰 회피=속도·중앙 토큰 0·owner의 Anthropic OAuth 프로필
#                            자동 해석). 전제: owner가 `claude` 로그인 또는 `ant auth login` 됨.
#                           extra: uv sync --extra claude-api
#   AON_PROVIDER=codex   : CodexApiRuntime — owner ~/.codex/auth.json(ChatGPT 구독 OAuth) 인프로세스
#                           openai SDK 스트리밍(중앙 토큰 0). 전제: owner가 `codex login` 됨.
#                           extra: uv sync --extra codex  (AON_PROVIDER=openai도 같은 어댑터)
#   예: AON_PROVIDER=claude-api scripts/run_worker.sh cs_lead
#       AON_PROVIDER=codex scripts/run_worker.sh cs_lead
set -euo pipefail
cd "$(dirname "$0")/.."

OWNER="${1:?owner를 지정하세요: scripts/run_worker.sh <OWNER_ID> [ROLE] [PORT] [CENTRAL_HOST]}"
ROLE="${2:-primary}"
PORT="${3:-8000}"
CENTRAL_HOST="${4:-127.0.0.1}"
URL="ws://${CENTRAL_HOST}:${PORT}/worker"

echo "[worker] owner=${OWNER} role=${ROLE}  ->  ${URL}"
exec uv run python -m agent_org_network.worker --owner "${OWNER}" --role "${ROLE}" --url "${URL}"
