#!/usr/bin/env bash
# 중앙 MCP 서버(`ask_org` 진입점)를 stdio 전송으로 띄운다(T3.2 수동 시연 — ADR 0006).
#
# 실 stdio 서버 기동은 *게이트 밖 수동 시연*이다(run_central.sh의 실 web/WS 셸과 같은
# 경계) — 결정론 테스트는 in-memory call_tool 로만 돈다. 보통은 이 셸을 직접 돌리지 않고,
# MCP 클라이언트(Claude Desktop·IDE 등)가 이 명령을 stdio 서버로 *띄우게* 설정한다.
#
# 사용:  scripts/run_mcp.sh
# 백엔드는 build_demo().ask(기본 런타임=진짜 Claude). 클라이언트는 stdin/stdout으로
# MCP 프로토콜을 주고받으며 `ask_org(question)` 도구를 호출한다. 답엔 담당·신뢰 상태
# (mode)·출처가 텍스트로 박혀 온다(일반 MCP 클라이언트엔 우리 UI가 없으므로 — ADR 0006).
#
# Claude Desktop 등록 예(claude_desktop_config.json):
#   {
#     "mcpServers": {
#       "agent-org-network": {
#         "command": "uv",
#         "args": ["run", "python", "-m", "agent_org_network.mcp_server"],
#         "cwd": "<이 저장소 절대경로>"
#       }
#     }
#   }
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[mcp] ask_org stdio 서버 기동 (Ctrl-C 종료) — 보통은 MCP 클라이언트가 이 명령을 띄운다" >&2
exec uv run python -m agent_org_network.mcp_server
