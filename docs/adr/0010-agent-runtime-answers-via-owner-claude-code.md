# Agent Runtime의 답변 주체는 각 Owner의 Claude Code다 — 중앙 API 키 LLM이 아니라

상태: accepted (2026-06-20) · ADR 0007의 "stub → LlmRuntime(RAG)"를 "owner Claude Code"로 구체화

ADR 0007은 Agent Runtime을 포트로 올리며 실서비스 구현을 `LlmRuntime`(owner `knowledge_sources` RAG)로 적었다. 그 라인은 *중앙이 API 키로 LLM을 직접 부르고 owner 문서를 중앙에서 RAG* 하는 그림을 암시한다. 그러나 PRD §2·§3의 비전은 "구성원이 자기 업무 에이전트를 만들어 **자기 지식으로 답**하고, 중앙은 연결자일 뿐 **답은 담당이 한다**"이다. 중앙이 지식을 들고 RAG로 답해 버리면 이 비전과 어긋나고(중앙 지식 소유, ADR 0006 위반), API 키·비용·모델 운영을 중앙이 떠안는다.

그래서 **Agent Runtime의 답변 주체를 각 Owner의 Claude Code로 못박는다.** 중앙 API 키 기반 LLM RAG를 쓰지 않는다. 답을 만드는 곳은 owner의 환경(그의 Claude Code, 그의 인증, 그의 지식)이고, 중앙은 그 환경을 호출·기록할 뿐이다.

이는 ADR 0007의 뒤집음이 아니라 **구체화**다 — 포트(`AgentRuntime.answer`)는 그대로 두고, 그 자리에 들어갈 구현을 "중앙 LLM RAG"에서 "owner Claude Code"로 바꾼다. 두 단계로 간다.

- **임시(T6.1, 지금)** — 중앙에서 헤드리스 Claude Code CLI(`claude -p`)를 1회성으로 호출해 답을 만든다. API 키 불필요, 로컬 `claude` 인증을 그대로 쓴다(동작 확인: `claude -p "..." --output-format text` → 답, ~5s). 이 단계에선 **모든 카드가 이 하나의 중앙 `claude`로 답한다** — owner별 격리는 아직 없다. `StubRuntime`(canned)을 대체해 진짜 텍스트 답이 end-to-end로 흐르는 첫 단계. 구현 클래스명은 `ClaudeCodeRuntime`.
- **최종(T6.3)** — 각 Owner PC의 Claude Code에 분산 연결(MCP/A2A 등록·호출)한다. 답변 주체가 그 owner의 환경이 되어 PRD의 "자기 지식으로 답"이 진짜로 성립한다. T6.1의 `ClaudeCodeRuntime`은 호출 대상을 "중앙 1회성 `claude -p`"에서 "owner별 분산 엔드포인트"로 바꾸며 이어진다.

근거:
- **PRD 정합** — "자기 지식으로 답"·"답은 담당이 한다"·"중앙은 연결자"(§2·§3)를 구현 레벨에서 지킨다. 중앙 LLM RAG는 이를 깬다.
- **중앙 지식 소유 회피** — 중앙이 owner 문서를 모아 RAG 하지 않는다(ADR 0006: 중앙은 *연결·호출·기록*만).
- **API 키·비용·운영 회피** — 중앙이 모델 키와 추론 비용을 떠안지 않는다. 추론은 각 owner 환경(또는 임시로 로컬 `claude`)에서 일어난다.

## Consequences

- Agent Runtime은 여전히 포트(`AgentRuntime.answer(question, card) -> Answer`). 테스트·스켈레톤은 `StubRuntime`, 실 답변은 `ClaudeCodeRuntime`(T6.1) → owner별 분산 Claude Code(T6.3). 포트는 안 바뀐다.
- **임시의 한계** — T6.1은 중앙 단일 `claude -p` 1회성이라 **owner별 지식 격리가 아직 없다**. 모든 카드가 같은 중앙 모델로 답하고, 카드의 `summary`·`domains` 등을 프롬프트 컨텍스트로 줄 뿐 owner의 실제 사적 지식에 접근하지는 못한다. 진짜 격리는 T6.3 분산에서 온다.
- **`knowledge_sources`는 아직 출처 레이블** — 카드의 `knowledge_sources`는 현재 `Answer.sources`로 흐르는 *출처 표시*이지 실제 문서 인덱스가 아니다. 진짜 문서 RAG(인덱싱·검색)는 별도 후속 결정이며 이 ADR 범위 밖이다.
- **결정론 테스트는 stub로** — `claude -p`는 비결정·네트워크·외부 프로세스라 단위 테스트에 부적합하다. 단위 테스트는 `StubRuntime` 주입을 유지하고(ADR 0003), `ClaudeCodeRuntime`의 실제 호출 품질은 eval/수동 시연으로 본다.
- **외부 의존 추가** — 런타임이 로컬 `claude` 실행 파일(CLI)에 의존한다. 부재·인증 만료·타임아웃은 런타임 레이어가 다룰 실패 모드다(엔지니어 구현 책임).
- TRD §4 포트 라인·§5 분산 전송, tasks T6.1·T6.3, CONTEXT Agent Runtime 절, PRD §5·§6을 이 방향으로 갱신한다.
