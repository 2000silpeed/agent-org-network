# ADR 0071 — Non-actionable Conversational Intake

## 결정

단독 인사처럼 조직 업무나 지식 처리를 요구하지 않는 입력은 `Unowned`로 보내지 않는다. `Unowned`는 실제 업무 질문의 담당 공백이며 기존처럼 root User/Manager 처분으로 이어진다.

Question Request는 항상 먼저 `Received`로 저장한다. Router 이전의 결정론적 exact-only intake 규칙이 비업무 대화를 판별한 경우에만 `initial_disposition="non_actionable"`, `intent=None`, `DeclinedRequest(reason_code="non_actionable_conversation")`로 revision 1에서 terminal 전이한다. 이 전이는 Router·Authority·ConflictCase·ManagerItem·runtime을 호출하지 않는다.

판별은 NFKC, casefold, 공백 정규화 후 전체 발화 allowlist 대조만 허용한다. 문장부호/접두사/의미 추론/LLM 판정은 하지 않는다. 따라서 `안녕하세요, 환불 규정은?`와 실제 미분류·0매칭·권한 거부·의존성 장애는 기존 경로를 유지한다.

## 결과

단순 대화가 담당 Agent Card나 Manager 큐를 오염시키지 않지만, 입력 이력과 terminal outcome은 남아 사용자 결과 기준 미아 없음은 유지된다. 최초 `DeclinedRequest`는 오직 이 조합만 허용하도록 aggregate와 SQLite hydrate에서 양방향 검증한다. 이후 사람/승인 처분의 Declined는 기존 수명주기에서 계속 허용한다.
