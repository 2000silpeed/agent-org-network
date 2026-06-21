#!/usr/bin/env bash
# 중앙 서버(통합 app: 사용자 web 라우트 + owner 워커 WS, 같은 dispatcher)를 띄운다.
# T6.3 슬라이스2b-ii end-to-end 수동 시연 — ADR 0011 결정 6.
#
# 사용:  scripts/run_central.sh [PORT]
# 기본 포트 8000. 워커는 ws://127.0.0.1:<PORT>/worker 로 붙고, 사용자는
# http://127.0.0.1:<PORT>/ask 로 질문, GET /ask/{tracking} 으로 답을 회수한다.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-8000}"
echo "[central] http://127.0.0.1:${PORT}  (web /ask, 워커 ws /worker — 한 dispatcher 공유)"
exec uv run uvicorn agent_org_network.server:central_app --host 127.0.0.1 --port "${PORT}"
