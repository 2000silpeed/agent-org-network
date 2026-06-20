# Agent Runtime을 범위 안으로 승격하고 walking skeleton으로 end-to-end를 먼저 세운다

상태: accepted (2026-06-20) · ADR 0004의 "Agent Runtime 범위 밖"을 뒤집음 · 실서비스 구현 `LlmRuntime(RAG)`는 ADR 0010에서 "owner Claude Code"로 구체화됨

초기 MVP는 "누구에게 보낼지"까지(brain-only)였으나, 제품 비전은 *실제로 답이 돌아오는* end-to-end다. 그래서 **Agent Runtime**(Agent Card를 구동해 owner의 `knowledge_sources`에 근거해 답하는 실행 주체)을 범위 안으로 올리고, 분류기와 같은 **포트**로 둔다 — `StubRuntime`(canned) → `LlmRuntime`(RAG). 전체를 한 번에 만들지 않고, 각 부분을 stub해서라도 **질문 → 라우팅 → 호출 → 답 → 화면**을 먼저 한 바퀴 도는 **walking skeleton**을 세운 뒤 각 부분을 깊게 한다.

## Consequences

- Agent Runtime은 포트 — 테스트·스켈레톤은 `StubRuntime`, 실서비스는 `LlmRuntime`.
- 분산 Agent 실제 전송(로컬 PC 도달)은 후순위 — 스켈레톤은 in-process stub.
- PRD 범위에 3개 프론트 면·MCP 서버·모니터링이 들어오고, "Agent Runtime 실제 실행 / UI 대시보드 범위 밖"은 삭제된다.
- 제품 면이 셋(사용자·개인 Owner·운영/Manager)으로 갈린다.
