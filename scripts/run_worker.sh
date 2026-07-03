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
#
# owner 초안 검토 웹(env AON_OWNER_UI_PORT, ADR 0025 결정 4·T9.7 S4 — 게이트 밖 수동):
#   설정 시 워커가 같은 프로세스에서 owner 로컬 검토 웹을 http://127.0.0.1:<PORT>에 띄운다.
#   HITL on으로 보류된 초안(Pending Draft)을 owner가 보고 승인/수정하면 활성 중앙 연결로
#   회신된다(owner 검토 루프). bind는 127.0.0.1 고정(외부 미도달). 미설정이면 기존 동작.
#   예: AON_OWNER_UI_PORT=8790 scripts/run_worker.sh cs_lead
set -euo pipefail
cd "$(dirname "$0")/.."

OWNER="${1:?owner를 지정하세요: scripts/run_worker.sh <OWNER_ID> [ROLE] [PORT] [CENTRAL_HOST]}"
ROLE="${2:-primary}"
PORT="${3:-8000}"
CENTRAL_HOST="${4:-127.0.0.1}"
URL="ws://${CENTRAL_HOST}:${PORT}/worker"

# admission 토큰(T9.5·ADR 0026): 중앙이 AON_DB로 실 토큰 검증을 켰다면 필수.
#   TOKEN=<콘솔에서 발급받은 평문 토큰> scripts/run_worker.sh cs_lead primary 8000 <중앙IP>
TOKEN_ARGS=()
if [ -n "${TOKEN:-}" ]; then
  TOKEN_ARGS=(--token "${TOKEN}")
fi

echo "[worker] owner=${OWNER} role=${ROLE}  ->  ${URL}"
exec uv run python -u -m agent_org_network.worker --owner "${OWNER}" --role "${ROLE}" --url "${URL}" "${TOKEN_ARGS[@]}"
