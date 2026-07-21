# Agent Org Network

회사 안의 질문이 **책임자를 거쳐 최종 답변이나 명시적 거절에 도달하도록 관리하는** 질문 종결 계층입니다. 담당 공백과 팀 경계에서 사람이 내린 결정을 기록하고, 조건이 맞는 다음 질문에 재사용합니다. 두 번째 제품 축으로 사람이 만든 산출물은 AI가 자문 검토하고 AI가 만든 산출물은 사람이 binding 검토하는 **상호 검토·통제된 개선**을 채택했습니다.

> **현재 상태 — 기업용 재기초화 진행 중.** 이 저장소는 기능과 테스트가 풍부한 아키텍처 데모이며, 아직 실제 기업 데이터를 맡길 production 제품이 아닙니다. 기본 rule 경로의 30문항 실측도 분류 60.0%·라우팅 73.3%로 기존 80% 기준에 못 미칩니다. Question Request 수명주기·fail-closed 인증/권한·durable workflow·복구 게이트가 끝날 때까지 개발·평가용으로만 사용하세요. 상호 검토 원장·개선 후보·승격·롤백은 [ADR 0047](docs/adr/0047-reciprocal-review-and-governed-improvement.md)의 설계만 채택했으며 아직 구현되지 않았습니다. 자세한 판정은 [`docs/enterprise-readiness-2026-07-12.md`](docs/enterprise-readiness-2026-07-12.md), 진행 순서는 [`docs/tasks-v0.md`의 Phase 17](docs/tasks-v0.md#phase-17--기업-파일럿-재기초화)과 [Phase 18](docs/tasks-v0.md#phase-18--상호-검토와-통제된-개선)에 있습니다.

사람으로 치면 검색창보다 책임 있는 안내 데스크에 가깝습니다. “어디에 쓰여 있나”뿐 아니라 “이 예외를 누가 책임지고 언제 닫나”를 다룹니다. 담당이 불명확하면 먼저 사람에게 결정을 요청하고, 책임자가 확정된 뒤 승인된 지식으로 답합니다.

> 🎬 **기존 기능 둘러보기**: [`docs/scenario.html`](docs/scenario.html) — 라우팅·합의·판례·승인·감독의 과거 데모입니다. ADR 0042 이전 흐름이 섞여 있으므로 production 계약으로 보지 마세요.

---

## 무슨 문제를 푸나

회사에는 담당이 흩어져 있습니다. 환불은 CS팀, 계약은 법무팀, 급여는 인사팀. 질문하는 사람은 누가 담당인지 모르고, 담당자는 자기 일로 바빠서 매번 답하기 어렵습니다.

이 시스템은 그 사이에 서서 **“이 질문은 누가 책임지고 어떻게 끝낼 것인가”**를 관리합니다. 담당자는 책임 범위와 승인 지식을 유지하고, 시스템은 질문 상태·사람 처분·최종 결과를 하나의 Request ID로 연결합니다.

### 경계 질문은 책임자를 먼저 정합니다

가끔 **두 담당자 영역에 걸친 질문**이 옵니다.

> "환불 안 된다는 조항이 있으면 청약철회도 못 하나요?"

이건 청약철회 담당자의 지식과 약관 조항 담당자의 지식이 **둘 다** 있어야 제대로 답합니다. 예전에는 한쪽에만 연결돼서 **반쪽짜리 답**이 나왔어요.

이런 질문은 `Contested`로 두고 후보 Owner의 합의나 Manager 중재를 기다립니다. 책임자가 정해지기 전에는 사전순 후보를 임의 담당자로 표시하거나 최종 답을 보내지 않습니다. primary가 확정된 뒤에만 보조 담당자의 승인 지식을 함께 접지하고, 같은 원 질문을 재개해 답이나 명시적 거절로 끝냅니다. 수명주기는 [ADR 0042](docs/adr/0042-question-request-lifecycle-and-user-outcome-integrity.md), Request-first 구현과 Approval 선행 순서는 [ADR 0043](docs/adr/0043-request-first-application-and-approval-before-finalization.md)에 정리했습니다. 사용자 채널 전환과 Unowned 처분, Contested direct 합의·Deadlock/Registry drift의 Manager Assign/Dismiss, resolution evidence 기반 typed grounding과 필수 지식 실패 종결, composition·웹·두 UI·blocking/GET/SSE/MCP 연결, S6 전체 경쟁·장애·독립 리뷰까지 구현했습니다. production 권한·내구성 게이트는 남아 있습니다.

---

## 핵심 개념

- **Question Request** — 질문 한 건의 접수부터 답·거절·실패까지를 나타내는 수명주기. 도메인 코어, 단일 인스턴스 SQLite Store·복구 coordinator, 기존 객체의 `request_id` 상관 자리, Router보다 먼저 Request를 저장하고 요청자 소유권을 확인하는 Application Service까지 구현됐습니다. 같은 프로세스의 동시 advance와 linked orphan 재개, durable/ephemeral 혼합 조립 거부도 들어갔습니다. 웹·SSE·retrieve·MCP와 두 UI가 같은 Request ID를 쓰며, 기존 `tracking`은 Request ID와 값이 정확히 같은 URI 별칭입니다. Unowned는 Manager Assign 뒤 같은 Request를 실행하고 Dismiss 뒤 명시적으로 거절합니다. Contested의 direct 합의와 Deadlock Manager 중재, primary 확정 뒤 typed grounding, 채널 조립, terminal POST·seal 응답 유실 복구와 전체 경쟁·fault 검증도 단일 프로세스 범위에서 구현됐습니다. durable linked writer·다중 인스턴스 lease는 아직 진행 중입니다.
- **Unowned Manager 처분** — request-aware Unowned만 별 application이 처리합니다. Assign은 현재 Agent Card·Owner·under-claim과 request-scoped 중앙 Authority grant를 확인한 뒤 Router를 다시 돌리지 않고 같은 Request를 재개합니다. Dismiss는 같은 Request를 `manager_declined`로 닫습니다. 전역 판례나 조직 전체 Authority 규칙은 바꾸지 않습니다. 현재는 단일 프로세스 InMemory claim과 demo Authority까지 검증한 개발 경계라 production 권한·내구성을 대신하지 않습니다.
- **Approval 경계** — 승인이 필요하거나 `draft_only`인 후보는 최종 답으로 기록하지 않고 `AwaitingApproval`에 둡니다. 중앙 정책과 인증된 승인자를 확인한 승인·수정승인만 Finalization 후보가 되고, 반려는 명시적 거절로 끝납니다. 배정 세대, 만료·재지정·unavailable, 인증 기반 웹·MCP·두 UI, 본문 없는 감사·알림·보존 판정까지 단일 프로세스에서 구현했습니다. durable transaction·journal·outbox·재시작 복구는 P17.9 범위입니다.
- **Answer Finalization** — 승인된 후보만 AnswerRecord·AnsweredRequest·terminal audit·request-correlated SessionTurn·delivery outbox로 한 번에 확정합니다. 모든 현재 사용자 채널이 이 결과를 사용하고, 승인·commit 전 token은 외부에 내보내지 않습니다. InMemory와 SQLite 단일 인스턴스 UoW가 구현됐고, SQLite는 재시작 복원·strict reader·32-connection 경쟁까지 검증했습니다. online Owner는 본문 없는 승인 대기, offline 자동발신은 사후교정 필요 기록으로 남습니다. Approval과의 단일 transaction, outbox 소비·lease, 다중 인스턴스는 남아 있어 production 보장은 아닙니다.
- **Question Surface Composition** — 웹·SSE·retrieve·MCP가 같은 Request·Approval·실행·Finalization 객체를 쓰도록 묶습니다. P17.7은 demo import와 자동 fallback이 없는 readiness-only bootstrap, production-style durability·identity gate, 단일 사용 조립 증명과 수명 소유권을 추가했습니다. 성공은 `composition_contract_only`이며 production 서버나 배포 준비를 뜻하지 않습니다. 현재 질문 실행은 중앙/로컬 Runtime의 completed-inline 답만 허용하고, 분산 WorkTicket·재시작 복구는 P17.9 전까지 사용자 질문 경로가 아닙니다.
- **Agent Card** — 한 담당자의 "명함 + 담당 범위"를 적은 파일(YAML). 담당자가 바뀌어도 이 명함의 정체성(`agent_id`)은 그대로예요.
- **라우팅 결과** — 질문이 오면 셋 중 하나로 정리됩니다. `Routed`(담당 정해짐) / `Contested`(후보가 여럿이라 다툼) / `Unowned`(아무도 담당 아님).
- **판례(Precedent)** — 사람이 “이 조건에서는 이 팀이 책임진다”고 정한 결정을 다음 질문에 재사용합니다. 현재 전역 intent 기준 판례는 문맥이 너무 넓어 Phase 17에서 적용 범위를 좁힐 예정입니다.
- **OKF 번들** — 담당자가 자기 지식을 그냥 마크다운 파일로 적어두는 폴더예요(`okf/<agent_id>/`). 복잡한 벡터DB·검색엔진 없이, 파일을 읽어서 답합니다.
- **지식 동기화 + 중앙 답변** — 담당자가 "이 문서까지 공개"라고 지정한 만큼만 중앙 창고로 올라오고, 중앙이 그걸 읽어서 답합니다. 담당자 PC가 꺼져 있어도 답이 나가요.
- **감독 + 정정** — 나간 답은 기록으로 남고, 담당자가 나중에 보고 고칠 수 있어요. 원래 답은 지우지 않고 **고침 기록만 덧붙입니다**(누가 언제 뭘 고쳤는지 남기려고).
- **상호 검토·통제된 개선(설계 단계)** — 사람 작성 revision에는 AI 자문 검토를, AI·mixed revision에는 사람의 binding 검토를 요구합니다. 사람이 수용한 finding은 candidate revision을 만들고, 그 candidate가 target별 독립 평가와 별도 사람 승격을 거쳐야 serving state가 됩니다. 외부 target·source serving은 매 read의 exact identity를 gateway/native enforcement와 대조해 fail-close하고, package-only target은 durable pending handoff만 남기며 직접 적용하지 않습니다. 격리·비활성도 같은 상태기계와 사람 권한으로 다루며, AI가 Authority·RBAC·production code를 스스로 바꾸는 구조가 아닙니다.

### 절대 안 깨지는 규칙 (불변식)

1. **어떤 질문도 버려지지 않는다** — 큐 적재로 끝내지 않고, 모든 질문이 조회 가능한 상태나 답·거절·실패 중 하나에 있어야 합니다.
2. **엉터리 명함은 등록 안 된다** — 형식·소유자 확인을 통과 못 하면 거부.
3. **권한은 중앙만 정한다** — 명함이 "내가 이것도 담당"이라고 자기 권한을 늘릴 수 없어요.
4. **답을 바꾸는 것과 기록하는 것은 별개** — 고침은 원본을 덮어쓰지 않고 따로 쌓입니다.
5. **책임자 확정 전에는 최종 답을 보내지 않는다** — `Contested`는 먼저 Pending입니다.
6. **모든 채널은 같은 최종화를 쓴다** — 웹·SSE·retrieve·MCP의 mode·record_id·감사가 같아야 합니다.

---

## 빠른 시작

[uv](https://docs.astral.sh/uv/) 기반입니다(Python 3.12).

```bash
git clone <repo> && cd agent-org-network
uv sync                     # 의존성 설치(.venv 자동 생성)
```

개발 데모를 띄우고 첫 질문 던지기:

```bash
uv run uvicorn agent_org_network.web:app --port 8099
```

```bash
curl -s -X POST http://127.0.0.1:8099/ask -H 'Content-Type: application/json' \
  -d '{"question":"20일 전 결제한 상품을 단순 변심으로 환불하면 10만원 중 얼마 돌려받나요?"}'
# → 개발 profile의 샘플 조직·분류기·런타임으로 응답
```

새 canonical API는 `POST /requests`, `GET /requests/{request_id}`, `GET /requests/{request_id}/stream`입니다. 기본 SSE reconnect는 Pending 한 건 뒤 닫힙니다. Manager 처분을 기다리며 같은 연결을 유지하려면 native `GET /requests/{request_id}/stream?watch=true`를 사용하세요. 기존 `/ask`, `/ask/stream`, `/ask/{tracking}`은 같은 P17 결과를 돌려주는 호환 URI이고 `tracking` 값은 Request ID 자체입니다.

브라우저로 `http://127.0.0.1:8099/` 를 열면 채팅 화면이 나옵니다.

> 이 명령은 demo profile입니다. 인증·조직 격리·production Authority가 없는 기본 실행을 실제 회사 네트워크나 데이터에 연결하지 마세요. P17.7은 조립 계약만 검사하는 readiness-only 경계라 별도 production 서버 명령이 아직 없습니다. 실제 환경은 P17.8 Authority/RBAC와 P17.9 durable workflow가 끝날 때까지 시작에 실패합니다.

---

## 제대로 돌려보기 (게이트)

CI와 같은 검사를 로컬에서 실행합니다.

```bash
uv sync --locked --all-extras --dev
uv run ruff check .
uv run pyright
uv run pytest -q

cd frontend
npm ci
npm test
npx tsc --noEmit
npm run lint
npm run build
```

> `pyright`는 **꼭 `uv run pyright`처럼 아무 인자 없이** 돌리세요. 파일을 지정하면 설정을 잘못 읽어서 없는 에러가 생깁니다.

---

## 개발 데모 사용법

### 1) 웹 — 채팅 + 운영 화면

```bash
uv run uvicorn agent_org_network.web:app --port 8099
```

| 화면 | 주소 | 누가 쓰나 |
|---|---|---|
| 채팅 | `/` | 질문하는 사람 — 묻고 답받기 |
| Owner 처리함 | `/inbox` | 담당자 — 다툼 합의 + 백업 답 검토 |
| 모니터링 | `/monitor/view` | 운영자 — 모든 질문·답 추적 |
| 조직 그래프 | `/org/view` | 운영자 — 전체 그림(사람·명함·연결) |
| 명함 빌더 | `/builder` | 담당자 — 명함 만들고 검증 |
| 지식 저작 | `/author` | 담당자 — 문서 올리면 AI가 개념 뽑아 정리→검토→반영 |
| 감독 | `/supervision` | 담당자 — 자기 에이전트 답 보기·고치기·"내 성적" |
| 관리 | `/admin` | 운영자 — 새 명함 등록·소유자 변경·스코어카드 |

채팅(`/ask`)은 익명이고, 운영 화면 인증도 **서명 키를 넣었을 때만** 켜집니다. 키가 없으면 운영·관리 API까지 인증 없이 열리는 fail-open 개발 동작입니다. 외부 인터페이스에 bind하지 마세요.

```bash
OPERATOR_SESSION_SECRET=$(openssl rand -hex 32) uv run uvicorn agent_org_network.web:app --port 8099
# 운영 화면 들어가기 전에: POST /login {"user_id":"cs_lead"} 로 로그인
```

로컬에선 `scripts/run_web.sh`(기본 포트 8011)를 쓰면 편합니다 — 처음 실행 시 서명 키를 한 번 만들어 gitignored `.env`에 보존하므로(재시작에도 같은 키), **로그인이 기본으로 동작**합니다. 직접 채울 변수는 `.env.example` 참고.

**질문을 더 똑똑하게 알아듣게 하기** — 기본은 키워드로 담당을 찾습니다(`환불`·`가격`·`계약` 같은 단어가 있어야 함). 자연스러운 문장까지 알아듣게 하려면 `AON_CLASSIFIER=llm`으로 띄우세요. 그러면 실제 claude가 질문을 읽고 담당을 정합니다.

```bash
AON_CLASSIFIER=llm uv run uvicorn agent_org_network.web:app --port 8099
# → "산 지 20일 됐는데 마음 바뀌어 돌려받고 싶어요"처럼 '환불' 단어가 없어도 CS로 연결
```

### 2) 실험용 분산 데모 — 중앙 + 담당자별 워커

담당자들이 각자 자기 컴퓨터에서 워커를 띄우는 방식입니다. 워커는 접속하는 순간 자기 지식을 중앙에 올리고(동기화), 접속해 있다는 것 자체가 "나 지금 온라인" 신호가 됩니다.

한 컴퓨터에서 시험:

```bash
scripts/run_central.sh 8000                # 중앙
scripts/run_worker.sh cs_lead primary 8000 # 담당자 워커 — 인자: <소유자> [역할] [포트] [중앙주소]
```

여러 컴퓨터(같은 네트워크)에서 돌리는 전체 절차와 주의사항은 [`scripts/demo_e2e.md`](scripts/demo_e2e.md)에 있습니다.

> 이 경로에는 과거 분산 실행 모델과 현재 지식 동기화·presence 기능이 함께 남아 있습니다. `/worker`는 지금도 지식·인덱스·legacy 운영 기능을 담당하지만, `/ask*` 사용자 질문은 P17 Request/Finalization을 사용하며 WS WorkTicket으로 이중 전송하지 않습니다. 분산 질문 실행은 durable WorkTicket·lease·복구가 들어오는 P17.9 전까지 비활성입니다. Unowned와 Contested 책임 결정·재개, typed grounding, 채널 조립과 전체 경쟁·fault 검증은 단일 프로세스 개발 경계까지 구현됐습니다. production 인증·Authority와 workflow 내구성이 없어 production 보장은 아직 성립하지 않습니다.

### 3) MCP 서버 — 내 MCP 클라이언트에서 질문

```bash
uv run python -m agent_org_network.mcp_server   # 도구: ask_org(question)
# Claude Desktop 같은 데 등록하는 법은 scripts/run_mcp.sh 참고
```

`ask_org(question)`과 `get_question(request_id)`은 웹과 같은 Request-first·Finalization DTO를 사용합니다. 다만 standalone MCP와 웹은 별 프로세스·별 composition입니다. 같은 계약을 쓴다는 사실만으로 메모리 상태를 공유하지 않으며, 현재 기본 팩토리는 각각 InMemory completion을 사용합니다. 같은 상태가 필요하면 공유 durable composition을 명시적으로 조립해야 합니다.

### 4) 정확도 측정 (골든셋 eval)

```bash
uv run python -m agent_org_network.eval --classifier rule   # 키워드 방식(빠름)
uv run python -m agent_org_network.eval --classifier llm    # 실 claude 분류(느림)
```

### 5) 명함 검증

```bash
uv run python -m agent_org_network.registry registry        # 등록된 명함·사용자 전부 검사
```

---

## 새 명함 등록 · 소유자 변경 (`/admin`)

> 아래 기능은 개발 데모입니다. default-deny RBAC와 production OIDC 조립이 완료되기 전에는 실제 조직의 관리 권한으로 사용하지 마세요.

명함을 YAML 파일 고치고 재시작하는 대신, 브라우저에서 바로 등록합니다. 검증을 통과한 것만 **즉시 반영**되고, 등록·변경 이력은 감사 로그로 남습니다.

**새 명함 등록** — `/admin`의 "신규 카드 등록"에서 폼을 채우고 제출하면 검증(형식·소유자 실재 확인)을 거칩니다. 통과하면 그 순간부터 질문 라우팅에 잡혀요(재시작 불필요). 엉터리면 사유와 함께 거부됩니다.

**소유자 변경** — 담당자가 바뀔 때 씁니다. 명함의 정체성은 그대로 두고 소유자만 교체하는데, 이때 옛 소유자의 워커 접속은 자동으로 끊깁니다(보안). 지식·판례·설정은 명함에 붙어 있어서 **그대로 유지**되고, 앞으로의 답을 고칠 권한만 새 소유자에게 넘어갑니다. 과거에 나간 답의 "누가 답했나"는 바뀌지 않아요.

---

## 감독 — 나간 답 보고 고치기 (`/supervision`)

담당자가 자기 에이전트로 나간 답을 살펴보는 화면입니다.

- **열람** — 자기 에이전트의 질문·답·고침 이력을 최신순으로.
- **검토 필요만 보기** — 담당자가 자리를 비운 사이 자동으로 나간 답만 추려서.
- **온라인 배지** — 내 워커가 지금 온라인인지 상단에 표시.
- **정정** — 고친 답을 보내면 원래 답은 그대로 두고 고침 기록이 덧붙습니다. 질문한 사람은 답변 페이지에서 고쳐진 내용을 확인할 수 있어요(고친 사람·사유 같은 내부 정보는 안 보이고요).

---

## 개발 profile 설정 (환경변수)

| 변수 | 기본값 | 역할 |
|---|---|---|
| `AON_CLASSIFIER` | 키워드 | `llm`이면 실 claude가 질문을 읽고 담당을 정함(자연어 인식) |
| `AON_ROUTER` | 분류기 | `index`면 담당자가 저작한 개념 목차로 라우팅 |
| `AON_PROVIDER` | `claude-code` | 답을 어디서 만드나 — `claude-api`·`codex`는 SDK로 중앙에서 |
| `AON_PROVIDER_KEY` | 없음 | 중앙 조직 API 키(없으면 `ANTHROPIC_API_KEY` 사용) |
| `AON_KNOWLEDGE_PATHS` | 명함별 폴더 | 워커가 중앙에 올릴 지식 범위(지정한 만큼만 올라감) |
| `AON_KNOWLEDGE_SYNC_INTERVAL_SECONDS` | `0`(1회만) | 지식 재동기화 주기 |
| `AON_KNOWLEDGE_STALE_SECONDS` | `1800`(30분) | 지식이 오래됐다고 볼 기준(넘어도 답은 막지 않고 "오래됨" 표시만) |
| `AON_PRESENCE_GRACE_SECONDS` | `0`(즉시) | 워커 끊긴 뒤 오프라인으로 볼 때까지의 유예 |
| `AON_DB` | 없음(메모리) | legacy 세션·답·고침·피드백·지식용 SQLite 경로. P17 terminal completion은 별 storage factory를 명시해야 하며, 기본 웹·standalone MCP가 이 값만으로 상태를 공유하지는 않음 |
| `OPERATOR_SESSION_SECRET` | 없음(로그인 OFF) | 개발 데모 운영 화면 인증 토글. production 인증 설정이 아님 |

---

## 데모 회사 (샘플 데이터)

`registry/`(사용자 6·명함 5) + `samples/questions.jsonl`(골든셋 질문 30개) + `okf/`(지식 번들).

| 명함 | 소유자 | 담당 |
|---|---|---|
| contract_ops | legal_lead | 계약 검토 |
| cs_ops | cs_lead | 환불, 보상 |
| finance_ops | finance_lead | 가격, 보상 |
| hr_ops | hr_lead | 채용·휴가·평가 (급여이체는 담당 아님) |
| it_ops | it_lead | 계정·접근권한·보안 |

`보상`은 cs_ops와 finance_ops가 함께 담당이라 **다툼(Contested)** 이 납니다. 과거 데모는 두 지식을 즉시 함께 접지했지만, ADR 0042의 목표 계약은 책임자가 정해질 때까지 Pending으로 두는 것입니다. `급여이체`는 인사가 “담당 아님”으로 빼놔서 아무도 담당하지 않는 **Unowned** 예시가 됩니다.

---

## 더 깊이 (설계 문서)

- 제품 요구사항: [`docs/prd-v0.md`](docs/prd-v0.md)
- 기술 설계: [`docs/trd-v0.md`](docs/trd-v0.md)
- 작업 목록: [`docs/tasks-v0.md`](docs/tasks-v0.md)
- 용어집: [`CONTEXT.md`](CONTEXT.md)
- 기업 준비도 재감사: [`docs/enterprise-readiness-2026-07-12.md`](docs/enterprise-readiness-2026-07-12.md)
- 아키텍처 결정 기록: [`docs/adr/`](docs/adr/) (ADR 0001~0047)

## 스택

Python 3.12 · pydantic v2 · FastAPI/uvicorn · pytest · pyright(strict) · ruff · MCP SDK · anthropic SDK(선택). 운영 화면은 빌드 없는 순수 HTML/CSS/fetch, 별도 사용자 UI는 [`frontend/`](frontend/)(Next.js). 답변은 로컬 `claude` CLI 또는 중앙 SDK 런타임으로 만듭니다.
