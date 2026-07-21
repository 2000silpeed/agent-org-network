#!/usr/bin/env bash
# 로컬/데모 웹 백엔드(agent_org_network.web:app — 채팅 + 운영 면)를 띄운다.
#
# 운영 면(처리함·큐·모니터링·그래프·빌더 + 프론트 Next의 /inbox·/console)은 세션 로그인이
# 필요하다(T6.5·ADR 0016). 그 세션 서명 키가 OPERATOR_SESSION_SECRET 이고, **미설정이면
# 인증이 통째로 OFF**라 POST /login 이 500, 운영 라우트가 401("세션 미들웨어 미부착")을 낸다.
#
# 이 스크립트는 그 키를 로컬에서 durable하게 만들어 "로그인이 기본으로 동작"하게 한다:
#   1) repo 루트의 .env(.gitignore — 커밋 안 됨)를 읽고
#   2) OPERATOR_SESSION_SECRET 이 비어 있으면 한 번 생성해 .env 에 적어 둔다(재시작에도 보존)
#   3) uvicorn 을 그 env 로 띄운다.
# 실 비밀은 커밋되지 않는다. 프로덕션은 이 스크립트가 아니라 배포 환경의 시크릿 매니저로
# OPERATOR_SESSION_SECRET 을 주입할 것(README 7장·web.py 모듈 주석).
#
# 사용:  scripts/run_web.sh [PORT]
#   PORT 기본 8011 — 프론트 Next 프록시(frontend/app/api/[...path]/route.ts)의 기본 타깃.
#   .env 에 둔 다른 변수(AON_CLASSIFIER·AON_PROVIDER 등)도 그대로 uvicorn 으로 흘러간다.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-8011}"
ENV_FILE=".env"

# 1) .env 로드(있으면) — KEY=VALUE 를 export. 개발자 소유 로컬 파일(표준 .env 관례).
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

# 2) 세션 키가 없으면 생성해 .env 에 보존(durable — 재시작에도 같은 키라 기존 세션 유지).
if [[ -z "${OPERATOR_SESSION_SECRET:-}" ]]; then
  OPERATOR_SESSION_SECRET="$(openssl rand -hex 32)"
  export OPERATOR_SESSION_SECRET
  printf '\n# 로컬 개발용 운영 세션 서명 키 — run_web.sh 자동 생성(커밋 금지·.gitignore)\nOPERATOR_SESSION_SECRET=%s\n' \
    "${OPERATOR_SESSION_SECRET}" >> "${ENV_FILE}"
  echo "[web] OPERATOR_SESSION_SECRET 신규 생성 → ${ENV_FILE} 에 보존(다음 실행부터 재사용)"
fi

echo "[web] http://127.0.0.1:${PORT}  (채팅 /ask · 운영 면 /inbox·/monitor·/org — 세션 로그인 필요)"
echo "[web]   로그인:  curl -c cj.txt -X POST localhost:${PORT}/login -d '{\"user_id\":\"cs_lead\"}' -H 'content-type: application/json'"
exec uv run uvicorn agent_org_network.web:app --port "${PORT}"
