# 기업 준비도 재감사 — 2026-07-12 기준

## 결론

2026-07-12 재감사 시점의 Agent Org Network는 **기업 production 제품이 아니었다.** 기능이 풍부하고 결정론 테스트가 잘 갖춰진 아키텍처 데모에 가까웠다. 책임 라우팅·합의·판례·담당자 감독이라는 제품 가설은 유효했지만, 사용자가 던진 질문이 사람 처분 뒤 원 질문자에게 돌아오는 수명주기가 끊겨 있었다. 인증·권한·영속 workflow·운영 복구도 실제 기업 데이터를 맡길 수준에 못 미쳤다. 위 결론 가운데 “production 아님” 판정은 지금도 유효하며, 이후 진척은 아래 진행 메모에서 구분한다.

> **2026-07-13 진행 메모.** 아래 평가는 7월 12일 재감사 시점의 기준선이다. 이후 P17.2c-2가 모든 사용자 표면을 Request-first 계약으로 바꿨고, P17.4가 Unowned Manager Assign/Dismiss 뒤 같은 Request 재개·Declined를 단일 프로세스 개발 경계에서 닫았다. P17.5 S1~S2는 request-aware Case·concurrence claim과 Owner direct consensus 재개 코어를 구현했다. 다만 Deadlock·Manager mediation(S3), 웹·채널 조립(S5), production OIDC/RBAC/Authority, durable linked workflow·lease/outbox·운영 게이트는 남아 있으므로 “production 아님” 판정과 파일럿 보류는 그대로다.

따라서 7월 12일 당시의 올바른 다음 단계는 기능을 더 붙이는 일이 아니라 제품을 **“조직 질문이 책임자를 거쳐 최종 답변이나 명시적 거절에 도달하도록 보장하는 종결 계층”**으로 좁히고 Question Request 수명주기와 보안 경계를 다시 세우는 것이었다. Question Request와 공통 Finalization은 이후 구현됐지만, 현재 우선순위는 남은 사람 처분 수직 슬라이스와 production 보안·내구성 경계를 닫는 데 있다.

이 문서는 2026-07-12 코드와 문서를 기준으로 한 스냅샷이다. 현재 진행 계약은 PRD rev13·TRD rev14, [ADR 0042](adr/0042-question-request-lifecycle-and-user-outcome-integrity.md)·[0043](adr/0043-request-first-application-and-approval-before-finalization.md)·[0044](adr/0044-sqlite-completion-uow-and-schema-capability.md)·[0045](adr/0045-request-aware-unowned-manager-disposition.md)·[0046](adr/0046-request-aware-contested-resolution-and-post-primary-grounding.md), [Phase 17](tasks-v0.md)이 맡는다.

## 제품 유용성 판단

### 해결할 가치가 있는 문제

문서 검색은 “어디에 쓰여 있는가”를 찾는 데 강하지만, 다음 질문에는 약하다.

- 이 예외를 최종적으로 누가 책임지는가.
- 두 팀 경계에 걸린 질문을 누가 닫는가.
- 담당이 비어 있을 때 누가 새 책임자를 정하는가.
- 사람이 내린 책임 결정을 다음에 어떻게 재사용하는가.
- 질문자가 최종 답이나 명시적 거절을 언제 받는가.

이 프로젝트의 차별점은 검색·RAG가 아니라 **책임 공백과 경계 질문을 종결하고 그 결정을 학습하는 루프**다. Agent Card·ConflictCase·ManagerItem·Precedent는 이 루프를 위한 내부 모델이어야 한다.

### 도입 주체별 해야 할 일

| 주체 | 해결해야 할 일 | 성공의 증거 |
|---|---|---|
| 질문자 | 익숙한 채널에서 질문하고 최종 결과를 받는다 | 답·거절·실패 중 하나와 추적 가능한 Request ID |
| Owner | 자기 책임 영역과 승인 지식만 유지하고 예외를 처리한다 | 반복 질문은 자동 종결되고 예외만 처리함에 남음 |
| Manager | 담당 공백과 경계만 결정하고 같은 결정을 재사용한다 | 열린 질문이 SLA 안에 줄고 같은 분쟁이 반복되지 않음 |
| 운영자 | 어느 질문이 누구에게 걸려 있는지 증거로 추적한다 | 재시작·재시도 뒤에도 상태·감사·결과가 일치함 |

### 독립 제품으로 만들지 말아야 할 조건

실제 질문 100~200건을 `단순 검색 / 명확한 책임 / 경계 / 담당 공백 / action 요청`으로 사람이 분류한다.

- 경계+담당 공백이 전체의 15~20%보다 적고 반복 클러스터도 3개 미만이면, 독립 종결 계층의 운영비가 효용보다 클 가능성이 높다.
- 질문 대부분이 단순 검색이면 Confluence/Rovo 검색 위에 얇은 책임 표시 기능으로 축소한다.
- Manager가 정치적 질문을 실제로 닫지 않아 열린 Case가 쌓이면 제품의 핵심 루프가 성립하지 않는다.
- 고위험 승인 책임자를 정할 수 없다면 해당 영역은 파일럿에서 제외한다.

이 기준은 가설 검증을 위한 초기 판정선이며, 실제 corpus에서 조정한다.

## 2026-07-12 기준 준비도

| 영역 | 5점 만점 | 판정 |
|---|---:|---|
| 결정론 도메인 코어 | 3.5 | sealed sum·포트·Fake 주입·대규모 회귀 테스트는 강점 |
| CI·변경 안전망 | 2.0 | 로컬 게이트와 CI 계약은 생겼지만 실제 required check·배포 게이트는 미검증 |
| 인증 | 1.0 | OIDC 포트는 있으나 기본 조립이 인증 없이 뜨는 fail-open |
| RBAC·조직 격리 | 0.5 | 세션 신원과 일부 1인칭 검사는 있으나 역할·관리 API 권한 모델이 불충분 |
| 질문 종결성 | 0.5 | 당시 Routed 일부만 추적했고 Contested·Unowned 해소 뒤 원 질문 재개가 없었음 |
| 영속 workflow | 0.5 | 일부 SQLite Store만 있고 질문·Case·Manager 처분·outbox 원자성 없음 |
| MCP·채널 일관성 | 0.5 | MCP가 별 demo 조립을 만들며 웹/SSE와 같은 application state를 보장하지 못함 |
| 관측성·SRE | 0.5 | 감사·콘솔은 있으나 표준 trace/metrics·SLO·alert·복구 훈련 부재 |
| 배포·공급망 | 0.5 | production profile·컨테이너·마이그레이션·SBOM·백업/복원 절차 부재 |

종합하면 약 **1/5** 수준이다. “기능이 많다”와 “기업에서 안전하게 운영할 수 있다”를 구분해야 한다.

## 근거가 된 구조적 결함

### 1. 재감사 당시 사용자 질문의 수명주기가 없었다

- `ask_org.py`의 기존 tracking은 비동기 Routed에만 생겼고 프로세스 메모리에 머물렀다.
- Contested와 Unowned는 Pending을 반환했지만 안정적인 사용자 상관키가 없었다.
- ConflictCase는 `open_for_intent`로 intent당 중복을 막아 두 번째 이후 원 질문을 잃었다.
- 합의·Manager 처분은 Case/Item을 닫을 뿐 원 질문을 다시 실행하거나 질문자에게 결과를 전달하지 않았다.
- 일반 SSE Routed 경로는 `DoneEvent`를 냈지만 AnswerRecord를 남기지 않았다. retrieve와 blocking도 Approval 적용 위치가 달랐다.

이 결함은 “큐에 들어갔다”를 “질문이 해결됐다”로 오해한 데서 생겼다. [ADR 0042](adr/0042-question-request-lifecycle-and-user-outcome-integrity.md)가 별도 Question Request aggregate와 공통 Answer Finalization을 채택했다.

**현재 진행 상황.** 모든 새 사용자 입력 표면은 라우팅 전에 Request ID를 만들고 blocking·SSE·retrieve·MCP가 같은 Approval boundary·Finalization·canonical 조회를 쓴다. Unowned는 같은 Request의 재개 또는 Declined까지 연결됐다. Contested는 Owner direct consensus 재개 코어까지 구현됐지만, Deadlock·Manager mediation(S3)과 웹·채널 연결(S5)은 아직 끝나지 않았다. 따라서 7월 12일의 “모든 사람 처분 뒤 원 질문 수명주기가 끊긴다”는 진술은 기준선 설명으로만 유효하다.

### 2. production과 demo가 분리되지 않았다

- `web.py`는 `build_demo`·`DEMO_OKF_ROOT`를 직접 import하고 모듈 기본 앱도 데모 조립을 쓴다.
- `server.py`의 `central_app`은 환경 변수 하나로 세션 키를 선택하며, 없으면 인증 없이 시작한다.
- `demo.py`의 기본 분류기는 RuleBasedClassifier이고 사용자·카드·키워드가 코드에 묶여 있다.
- `mcp_server.py`도 `build_demo()`를 별도로 호출해 `AON_DB`가 없으면 웹 프로세스와 상태가 갈라진다.

production entry point는 필수 설정 누락을 개발 편의 폴백으로 덮지 말고 시작에 실패해야 한다.

### 3. Authority와 권한 검사가 선언보다 약하다

- 문서는 중앙 `routing_rules.yaml`을 불변식으로 말하지만 파일과 runtime 정책 저장소가 아직 없다.
- 카드 domain이 사실상 후보 권한으로 쓰이는 경로가 남아 있어 자기보고와 중앙 Authority의 경계가 흐리다.
- 인증된 사용자라는 사실과 관리·감독·토큰 발급 권한을 구분하는 default-deny RBAC가 없다.
- 워커 연결·소유권 변경·in-flight ticket 사이의 binding을 submit 시점까지 일관되게 재검증하지 않는다.

P17.8은 권한을 선언·검증·감사하는 한 정책 경계로 모아야 한다.

### 4. 중요한 상태가 메모리에 남는다

재감사 당시에는 Question Request의 SQLite v1 Store와 `Received`·`ReadyToDispatch` Recovery Runner까지만 추가됐고 실제 Application Service에는 조립되지 않았다. 이후 P17.3c와 P17.2c-2가 SQLite terminal completion과 사용자 표면 조립을 완료했다. P17.4와 P17.5 S1~S2의 Manager·Conflict 처분 상태는 여전히 단일 프로세스 InMemory이며, Manager 큐·ConflictCase·Approval과 request-scoped Authority grant는 하나의 durable transaction으로 묶이지 않는다. 프로세스가 중간에 죽으면 사람 처분과 원 질문 재개 사이에 반쪽 상태가 생길 수 있으며, 남은 간극은 P17.9가 맡는다.

단일 인스턴스 파일럿은 SQLite transaction과 reconciler로 시작할 수 있다. 다중 인스턴스 production은 Postgres·lease·transactional outbox·backup/restore 검증 뒤에 열어야 한다.

### 5. 실행 모델과 제품 표면이 지나치게 넓다

중앙 런타임, owner 워커 답변, 지식 공급 워커, 백업 워커, 여러 라우터와 저작·스코어카드·그래프·메일·멀티 LLM 경로가 한 조립에 공존한다. 실 서비스 가치가 검증되기 전에 운영 면이 넓어져 핵심 질문 종결 흐름이 묻혔다.

Phase 17 동안 스코어카드·멀티 LLM·백업 워커·고급 그래프·범용 빌더는 제거하지 않되 파일럿 기본 표면에서 숨긴다. 첫 수직 슬라이스는 한 부서·Confluence 한 스페이스·웹/SSE 한 채널로 제한한다.

## 목표 아키텍처

첫 단계는 마이크로서비스가 아니라 경계를 지키는 모듈러 모놀리스다.

1. **Identity & Organization** — OIDC principal, User/조직 관계, RBAC, Agent Card admission, org 격리.
2. **Knowledge Publication** — owner 승인 원문·스냅샷·동기화·삭제·ACL·출처.
3. **Question Resolution** — Question Request·Router·WorkTicket·Resume Claim·Approval·Answer Finalization.
4. **Governance Case** — ConflictCase·ManagerItem·담당 주체·SLA·사람 처분.
5. **Audit & Evaluation** — append-only 감사·AnswerRecord·운영 지표·큐레이션 eval.

Question Resolution만 사용자 결과를 소유한다. Case와 큐는 Request를 참조하고, AnswerRecord와 감사는 Request 상태를 대신하지 않는다.

## 우선순위

### P0 — 파일럿 이전

- 모든 질문에 durable Request ID와 명시 상태 부여.
- Manager 큐 배선·동시성·멱등 처분 보장.
- blocking/SSE/retrieve/MCP 공통 Answer Finalization.
- Unowned·Contested·Approval 해소 뒤 원 질문 재개 또는 Declined.
- production fail-closed 조립, OIDC·default-deny RBAC·중앙 Authority.
- SQLite terminal Unit of Work·reconciler·outbox, ACL·보존·삭제 정책.
- 사용자·운영자 모두 request 상태와 처리 주체를 조회할 수 있는 표면.

### P1 — 통제 파일럿

- 한 부서·한 스페이스·한 채널 수직 슬라이스.
- 실제 corpus 100~200건과 답변 기대치가 포함된 eval.
- 구조화 로그·trace·metrics·SLO·alert·health/readiness·runbook.
- migration·backup/restore·비밀관리·rate limit·CSRF·부하/복구 시험.
- contextual Precedent와 정책 변경에 따른 무효화·재평가.

### P2 — 가치가 확인된 뒤

- 다중 조직·다중 리전·Postgres HA·정교한 비용·quota.
- Slack·메일 등 추가 채널과 action 실행.
- 멀티 LLM·백업 워커·고급 GraphRAG·스코어카드의 운영 승격.

## 파일럿 진입 게이트

아래 조건을 모두 충족하기 전에는 실제 회사 데이터를 넣지 않는다.

- Routed·Contested·Unowned·Approval의 end-to-end 종결 테스트가 있다.
- 재시작·동시 처분·SSE 중단·중복 delivery에서 terminal 결과가 하나다.
- production이 인증·DB·Authority·provider 설정 누락 시 시작에 실패한다.
- 다른 사용자·다른 조직·권한 없는 Manager/Owner의 조회와 처분이 거부된다.
- Confluence 삭제·ACL 철회·부분 실패가 중앙 지식에 반영된다.
- 골든셋이 라우팅뿐 아니라 근거 있는 답·안전한 거절·Approval을 검증한다.
- backup/restore와 장애 복구를 실제로 한 번 수행한다.
- 보안·개인정보·보존 정책 소유자가 승인한다.

## 외부 기준

이 재감사는 체크리스트 인증을 주장하지 않는다. 목표 profile과 검증 증거를 먼저 정하는 방식은 [NIST AI Risk Management Framework](https://www.nist.gov/itl/ai-risk-management-framework)와 [NIST AIRC](https://airc.nist.gov/)의 profile·TEVV 접근을 참고했다. 인증·권한·관리 행위의 보안 이벤트는 [OWASP ASVS 5.0 보안 로깅 요구사항](https://cornucopia.owasp.org/taxonomy/asvs-5.0/16-security-logging-and-error-handling/03-security-events)을 기준선으로 삼는다. trace·metrics·logs의 벤더 중립 계측은 [OpenTelemetry](https://opentelemetry.io/docs/)를 기본 후보로 둔다.

이 기준을 문서에 적는 것으로 통제가 생기지는 않는다. 각 항목은 코드·설정·자동 테스트·운영 훈련의 증거로 닫아야 한다.
