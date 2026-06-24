#!/usr/bin/env bash
# 중앙 서버(통합 app: 사용자 web 라우트 + owner 워커 WS, 같은 dispatcher)를 띄운다.
# T6.3 슬라이스2b-ii end-to-end 수동 시연 — ADR 0011 결정 6.
#
# 사용:  scripts/run_central.sh [PORT] [HOST]
# 기본 포트 8000, 기본 HOST 127.0.0.1(로컬 전용). 다른 기기(LAN)에서 워커가 붙게 하려면
# HOST를 0.0.0.0으로 준다:  scripts/run_central.sh 8000 0.0.0.0
#   주의(보안): 0.0.0.0은 포트를 네트워크에 연다 — 워커 등록 인증은 아직 stub(T6.5 전)이라
#   신뢰된 LAN에서만 쓰고 방화벽으로 포트를 통제한다.
# 워커는 ws://<HOST>:<PORT>/worker, 사용자는 http://<HOST>:<PORT>/ask 로 붙는다.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-8000}"
HOST="${2:-127.0.0.1}"
echo "[central] http://${HOST}:${PORT}  (web /ask, 워커 ws /worker — 한 dispatcher 공유)"
exec uv run uvicorn agent_org_network.server:central_app --host "${HOST}" --port "${PORT}"
