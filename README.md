# Agent Org Network

조직 구성원이 **자기 업무 에이전트(Agent Card)** 를 만들어 *자기 지식으로 답하게* 하고,
중앙(MCP 서버)이 **담당·권한·판례**를 기준으로 질문을 알맞은 에이전트에 연결해 답을
돌려주는 협업형 AI 조직 — walking skeleton(v0).

라우팅의 진실(담당·권한·판례)은 중앙이 쥔다. 지식은 담당자(owner)가 **명시 지정한 만큼만**
중앙 지식 저장소(Knowledge Store)로 동기화돼 답의 접지에 쓰이고, 잘못 나간 답은 담당자가
감독 화면에서 사후 정정한다(원 답은 불변 — 정정 이벤트만 쌓임). 모르면 지어내지 않고
사람(Manager)에게 넘긴다.

> 🎬 **전 기능 시나리오 투어**: [`docs/scenario.html`](docs/scenario.html) — 브라우저로 열면 데모 조직이
> 모든 기능(라우팅·합의·판례·미아·승인·분산·백업·인증·모니터링·그래프·MCP·eval)을 거치는 체계적
> 시나리오를 단계별로 볼 수 있다.

## 핵심 개념

- **Agent Card** — 한 책임(직무)의 담당 영역·답변 범위·이관 규칙을 선언한 YAML. 정체성(`agent_id`)은
  담당자가 바뀌어도 불변. 코드 밖 `registry/agents/*.yaml`로 등록.
- **RoutingDecision** — 질문의 처분: `Routed`(담당 정해짐) / `Contested`(후보 다툼) / `Unowned`(미아).
- **Precedent(판례)** — 다툼을 사람이 1인칭 합의로 풀면 `Resolution`이 판례로 쌓여 라우터가 학습한다.
- **OKF 번들** — owner가 자기 지식을 마크다운+프론트매터로 `okf/<agent_id>/`에 둔다(벡터DB·RAG 0).
  레거시 런타임(claude-code)은 이 디렉터리를 cwd로 *읽어* 답하고, Phase 12부터는 이 지정 경계가
  중앙 Knowledge Store로 동기화돼 중앙 답변의 접지가 된다.
- **Knowledge Store / Knowledge Sync** — 워커(지식 공급자)가 명시 지정한 문서만 admission
  (지정 경계 대조 + 민감정보 패턴 필터)을 거쳐 중앙에 *본문*으로 동기화된다. 라우팅 인덱스는
  여전히 목차만 본다(ADR 0033 — 라우팅 축 vs 답변 축 분리).
- **Answer Record / Correction Event** — 중앙이 낸 답은 Answer Record로 남고, 정정은 원 레코드를
  고치지 않는 새 Correction Event로 쌓인다(전이 ≠ 기록). 질문자는 답변 페이지의 정정 배지(풀 방식)로 본다.
- **Presence** — 담당자 워커의 WS 연결이 곧 하트비트. 온라인=사전 검토, 오프라인=자동 발신+사후 교정.
- **불변식** — ① 어떤 질문도 미아로 남지 않는다(0매칭→Manager) ② 유효하지 않은 카드는 등록 안 됨
  ③ 권한은 중앙만 선언 ④ 전이 ≠ 기록 ⑤ 답엔 항상 담당·신뢰 상태(승인/초안/백업/출처)가 붙는다.

## 실행 모델 (현행 — Phase 12 구현·ADR 0033/0034)

라우팅 계층("누가 무엇을 담당하나")은 그대로이고, 실행 계층("답을 어디서 만드나")은 Phase 12에서
**"중앙 답변 + 지식 동기화 + 담당자 감독"** 으로 옮겨졌다(ADR 0033 — 실행 위치 계보
0010→0017→0027→0033). "owner PC가 꺼지면 그 에이전트는 답 불능"이라는 이전 구조의 가용성 구멍을
메우는 전환이다.

- **지식 동기화(Knowledge Sync)** — owner 워커의 역할이 "답변 실행자"에서 **지식 공급자**로 바뀌었다.
  워커는 자기가 명시 지정한 경계(`AON_KNOWLEDGE_PATHS`, 기본은 카드별 `okf/<agent_id>/`)의 문서만
  시작 시(+ `AON_KNOWLEDGE_SYNC_INTERVAL_SECONDS` 주기로) 중앙에 동기화한다. 중앙은 수용 관문
  (admission)을 통과한 본문만 Knowledge Store에 담는다 — 지정 경계 밖 경로 거부, 민감정보 패턴
  (주민등록번호·API 키·비밀번호류) 자동 거부, 타 owner 사칭 거부.
- **중앙 답변** — 인프로세스 공급자 런타임(`AON_PROVIDER=claude-api`·`codex`)이 Knowledge Store의
  동기화된 본문을 우선 접지하고, 없으면 디스크 OKF로 폴백해 답을 만든다. 자격증명은 **중앙 조직
  API 키 1개**(`AON_PROVIDER_KEY` → `ANTHROPIC_API_KEY`) — 이전의 "중앙 토큰 0" 속성은 가용성을
  위해 ADR 0033에서 정직하게 폐기했다(키는 env로만 보관·로그 미노출·비용은 agent_id 태깅으로 사후
  집계). `AON_PROVIDER` 미설정(레거시 `claude-code`)이면 기존 cwd 접지 그대로다. 낡은 지식
  (`AON_KNOWLEDGE_STALE_SECONDS` 초과)이어도 답을 차단하지 않는다(낡음 표식만 — 미아 없음 보존).
- **프레즌스(Presence)** — 워커 WS 연결이 곧 하트비트다. 담당자가 온라인이면 답이 나가기 전
  사전 검토로 조여지고(HITL 상향 — 카드 `approval_when`이 건 것은 오프라인이어도 못 푼다),
  오프라인이면 자동 발신 후 "검토 필요" 표식이 붙어 사후 교정 대상이 된다.
- **담당자 감독(Supervised Answering)** — 중앙이 낸 답은 Answer Record로 남고, 담당자는 감독 화면
  (`/supervision`)에서 열람·정정한다. 정정은 원 레코드를 고치지 않는 Correction Event append이고
  (전이 ≠ 기록), 질문자는 답변 페이지 정정 배지(풀 방식)로 정정본을 본다. 정정은 판례·지식
  재평가 큐(reeval)에도 적재된다.

기존 분산 워커 회신 경로(워커가 로컬 claude로 답해 회신·백업 워커·Manager escalation)는 호환
경로로 남아 있다 — 아래 "분산" 절. **분산 배선에서도 담당 워커가 미연결이면 중앙 런타임이
Knowledge Store 접지로 대신 답한다**(`WebSocketDispatcher`에 중앙 런타임을 폴백원으로 주입) —
"담당자 PC 꺼져도 답변"이 인프로세스 경로뿐 아니라 분산에서도 성립한다. 워커가 연결돼 있으면
폴백은 발동하지 않고 기존 워커 회신 경로 그대로다(회귀 0).

**잔여(아직 안 된 것 — 정직한 구분):** ① 질문자 정정 **실 푸시/메신저 통지**(현재는 답변 페이지
풀 방식 배지만) ② **SQLite 영속 확장** — 카드·Answer Record/Correction Event(현재 InMemory + YAML
시드. 감사 로그·토큰은 `AON_DB` durable 경로 있음) ③ **실 SSO 운영자 role 게이트**(관리 UI 접근은
현재 세션 신원까지 — role 구분은 실 SSO 활성 시 강화) ④ **크로스머신 실 재시연**(수동).

## 설치

[uv](https://docs.astral.sh/uv/) 기반(Python 3.12).

```bash
git clone <repo> && cd agent-org-network
uv sync                     # 의존성 설치(.venv)
```

실 claude 답(`ClaudeCodeRuntime`)을 보려면 [Claude Code CLI](https://claude.com/claude-code)(`claude`)가
설치·인증돼 있어야 한다(중앙 API 키 불필요 — 로컬 claude 인증 사용). 결정론 테스트·데모는 `claude` 없이도 돈다.

## 게이트(테스트·타입·린트)

```bash
uv run pytest        # 단위 테스트(결정론) — 2399 passed
uv run pyright       # 타입 검사(strict) — 0 errors
uv run ruff check    # 린트 — 0
```

> 주의: pyright는 **반드시 `uv run pyright`(인자 없이)** 로 — 파일을 지정하면 설정·venv 해석이 깨져 거짓 에러가 난다.

## 기본 사용법

### 1) 웹 (채팅 + 운영 면)

```bash
uv run uvicorn agent_org_network.web:app --port 8099
```

| 면 | URL | 페르소나 |
|---|---|---|
| 채팅 | `http://127.0.0.1:8099/` | 실 사용자(익명) — 묻고 답받음 |
| Owner 처리함 | `/inbox` | Owner — 다툼 합의 + 백업 답 검토 |
| 운영 모니터링 | `/monitor/view` | 운영자 — 모든 Q&A 절차·답 추적 |
| Org 그래프 | `/org/view` | 운영자 — 전체 그림(User·Card·엣지) |
| Agent 빌더 | `/builder` | Owner — 카드 구성·검증·YAML 미리보기 |
| OKF 저작 | `/author` | Owner — 문서 올리면 LLM이 개념 추출→검토→커밋→목차 publish |
| 담당자 감독 | `/supervision` | Owner — 자기 에이전트 답 열람·검토 필요 필터·정정·프레즌스 배지 |
| 관리 UI | `/admin` | 운영자 — 신규 Agent Card 라이브 등록·오너 변경(아래 "관리 UI" 절) |

채팅(`/ask`)은 익명이고, **운영 면(처리함·큐·모니터링·그래프·빌더·관리 UI)은 인증**이 필요하다.
인증을 켜려면 세션 서명 키를 env로 준다(미설정 시 데모 인증 OFF):

```bash
OPERATOR_SESSION_SECRET=$(openssl rand -hex 32) uv run uvicorn agent_org_network.web:app --port 8099
# 운영 면 진입 전 POST /login {"user_id":"cs_lead"} 으로 로그인(세션 신원 1개 고정·가장 불가)
```

채팅으로 실 claude 답 받기(웹 기본 런타임 = `ClaudeCodeRuntime` + owner OKF 번들):

```bash
curl -s -X POST http://127.0.0.1:8099/ask -H 'Content-Type: application/json' \
  -d '{"question":"20일 전 결제한 상품을 단순 변심으로 환불하면 10만원 중 얼마 돌려받나요?"}'
# → cs_ops가 okf/cs_ops/refund-policy.md 를 읽고 "45,000원" 계산 + 담당·출처
```

**분류기 선택** — 기본은 키워드 규칙(`RuleBasedClassifier`, 빠름·결정론, `환불`·`가격`·`계약` 등
키워드가 있어야 라우팅). 자연어 질문까지 라우팅하려면 `AON_CLASSIFIER=llm`으로 띄운다 — 실
claude(Haiku)가 질문을 담당 도메인으로 분류한다(중앙 API 키 0·로컬 claude 인증, ADR 0015 단일 출처).

```bash
AON_CLASSIFIER=llm uv run uvicorn agent_org_network.web:app --port 8099          # 웹
AON_CLASSIFIER=llm scripts/run_central.sh 8000 0.0.0.0                            # 분산 중앙
# → "산 지 20일 됐는데 마음 바뀌어 돌려받고 싶어요"처럼 '환불' 단어가 없어도 cs_ops로 라우팅
```

**답변 런타임·라우터 선택** — 답변 런타임 기본은 `ClaudeCodeRuntime`(로컬 `claude` CLI). `AON_PROVIDER=claude-api`로 띄우면
in-process SDK(`ClaudeApiRuntime`·claude-sonnet-5)가 중앙 Knowledge Store의 동기화 본문을 우선 접지해 답한다(부재 시 디스크
OKF 폴백·자격증명은 중앙 조직 키 — `AON_PROVIDER_KEY`/`ANTHROPIC_API_KEY`, ADR 0033). 라우팅은
`AON_ROUTER=index`로 published **목차(KnowledgeIndex) 토큰 매칭**(`TwoStageRouter`)을 켜면, owner가 `/author`로 저작·publish한
개념이 곧바로 라우팅에 반영된다 — **크로스머신 fan-out**(owner 저작→실 git 커밋→실 WS로 중앙에 목차만 전송→중앙 수용→라우팅·판례
재평가). 라우팅 인덱스는 여전히 목차만 본다 — 본문은 owner가 명시 지정한 만큼만 Knowledge Store에 동기화된다(답변 축 전용).

### 2) 분산 — 중앙 + owner 워커 (WebSocket)

Phase 12부터 워커는 접속하자마자 자기 지정 경계(`AON_KNOWLEDGE_PATHS`, 기본 `okf/<agent_id>/`)의
문서를 중앙 Knowledge Store로 **지식 동기화**하고, 연결 자체가 그 담당자의 **프레즌스**(온라인/오프라인)가
된다. 워커가 라우팅된 질문을 로컬 claude로 답해 회신하는 기존 경로도 그대로 돈다.

한 기기(localhost):

```bash
scripts/run_central.sh 8000                # 중앙(web /ask + 워커 ws /worker, 한 dispatcher)
scripts/run_worker.sh cs_lead primary 8000 # owner 워커(로컬 claude로 답) — 인자: <OWNER> [ROLE] [PORT] [CENTRAL_HOST]
# POST /ask → dispatched+tracking 회수, GET /ask/{tracking} → 워커가 회신한 실 claude 답
```

여러 기기(같은 LAN — 윈도우/맥/리눅스에 각 담당자 워커): 중앙은 LAN에 노출하고, 각 워커는
중앙의 LAN IP로 붙는다. 전체 절차·실패 모드는 [`scripts/demo_e2e.md`](scripts/demo_e2e.md) 참고.

```bash
# 중앙 기기(IP 예: 192.168.0.10) — 0.0.0.0으로 바인딩해 LAN에 연다
scripts/run_central.sh 8000 0.0.0.0

# 각 OS 기기에서 자기 담당자 워커를 띄운다(저장소 체크아웃 + uv sync + 로컬 claude 로그인 전제)
scripts/run_worker.sh cs_lead    primary 8000 192.168.0.10   # 예: 윈도우 = cs_lead
scripts/run_worker.sh legal_lead primary 8000 192.168.0.10   # 예: 맥    = legal_lead
scripts/run_worker.sh finance_lead primary 8000 192.168.0.10 # 예: 리눅스 = finance_lead
```

> 전제: ① 각 워커 기기에 로컬 `claude` CLI 설치·로그인(답은 그 기기의 claude가 만든다) ②
> 그 owner의 OKF 번들이 `okf/<agent_id>/`에 있어야 지식 기반 답이 나온다(없으면 "문서 없어
> 모른다"로 정직하게 답함 — 현재 샘플은 `cs_ops`·`contract_ops`만 있고 `finance_ops`는 없다) ③
> 워커 등록 인증은 아직 stub(T6.5/SSO 전)이라 **신뢰된 LAN에서만** — 0.0.0.0 바인딩은 포트를
> 네트워크에 연다 ④ 방화벽에서 중앙 포트(예 8000)를 허용.

가용성: owner PC 워커가 부재면 owner 위임 **백업 워커**(`--role backup`)가 신뢰 하향(`mode=backup`)으로
답하고, owner 복귀 시 처리함에서 검토(승인/정정/무시)한다. 둘 다 부재면 Manager escalation(미아 없음).

### 3) MCP 서버 (사용자의 MCP 클라이언트에서 질문)

```bash
uv run python -m agent_org_network.mcp_server      # stdio 서버 — 도구 ask_org(question)
# Claude Desktop 등 MCP 클라이언트 등록은 scripts/run_mcp.sh 참고
```

### 4) 골든셋 eval (분류·라우팅 정확도)

```bash
uv run python -m agent_org_network.eval --classifier rule   # 결정론 키워드(빠른 확인)
uv run python -m agent_org_network.eval --classifier llm    # 실 claude 분류(느림·게이트 밖)
```

### 5) 레지스트리 검증 (코드 밖 YAML 등록)

```bash
uv run python -m agent_org_network.registry registry        # 5장 카드·6명 유저 load + validate
```

## 관리 UI — 신규 Agent Card 등록·오너 변경 (`/admin`)

카드 등록·오너 변경을 YAML 편집·재시작 없이 브라우저에서 한다(ADR 0034). admission 검증을 통과한
제출만 **라이브 Registry에 즉시 반영**되고, 등록·전이 이력은 감사 로그가 진다 — `registry/agents/*.yaml`
파일은 부팅 시 초기 시드로만 쓰인다.

**신규 Agent Card 등록:**

1. `http://<중앙>/admin` 접속. 인증이 켜져 있으면(`OPERATOR_SESSION_SECRET`) 화면에서 로그인
   (세션 신원 — 미로그인 요청은 401).
2. "신규 카드 등록" 탭에서 폼 입력 — `agent_id`(불변 정체성)·owner(Registry 실재 User)·team·
   한 줄 요약·domains(쉼표 구분)·maintainer 등.
3. 등록 제출 → **admission 검증**: 형식·`agent_id` wire-format·참조 무결성(owner·maintainer 실재)을
   전부 통과해야 한다. 무효면 **422 + 사유 목록**이 화면에 뜨고, 중복 `agent_id`면 **409**.
   우회 등록 API는 없다("유효하지 않은 카드는 등록되지 않는다" 불변식).
4. 통과 즉시 라이브 반영 + 감사 기록(`CardRegistered`). 라우터는 매 질문마다 라이브 카드를 읽으므로
   **등록 직후 질문부터 라우팅에 잡힌다**(재시작·재색인 불요).

**오너 변경(Ownership Transfer):**

1. `/admin`의 "오너 변경" 탭에서 대상 카드(드롭다운)와 새 owner를 지정.
2. 제출 → **재검증(재-admission)**: `agent_id`는 불변이고 owner 값만 교체된 카드 후보가 신규 등록과
   같은 관문을 다시 통과해야 한다. 새 owner가 Registry에 없는 등 무효면 422 — 스위치는 일어나지 않는다.
3. 통과 시 **스위치**(카드의 owner 교체)와 **같은 임계 구역에서** 구 owner의 **활성 워커 토큰 전부가
   자동 revoke**되고(구 owner가 카드를 여럿 가져도 함께 — over-revoke는 owner 격리의 안전측),
   구 owner 워커의 WS 세션이 끊긴다(프레즌스 offline·진행 중 작업은 재큐).
4. `OwnershipTransfer` 감사 기록(누가·어느 카드·A→B·언제) append — 전이와 기록은 분리(전이 ≠ 기록).

오너가 바뀌어도 **지식(Knowledge Store)·판례·HITL 토글은 유지**된다(전부 `agent_id`에 붙는 카드의
자산 — 사람에 안 붙음). 과거 답의 `answered_by`는 불변이고, **정정 권한만 새 owner로 이동**한다
(판정 기준 = "현재 그 카드의 owner인가").

## 담당자 감독 — 답 열람·사후 정정 (`/supervision`)

담당자(owner)가 자기 에이전트로 나간 답을 감독하는 화면. 중앙이 낸 답은 Answer Record로 남고,
이 화면이 그 목록을 최신순으로 보여준다.

- **열람** — 에이전트 ID를 넣으면 자기 에이전트의 질문·답·정정 이력이 뜬다(4초 폴링 — 작성 중이면
  리렌더를 건너뛰어 입력이 날아가지 않는다).
- **검토 필요 필터** — 담당자 오프라인 중 자동 발신된 답(`needs_correction_review`)만 추려 본다.
- **프레즌스 배지** — 자기 워커의 온라인/오프라인 상태가 상단에 표시된다.
- **정정 제출** — 정정본(+선택 사유)을 보내면 원 답은 그대로 두고 Correction Event가 append되고,
  판례·지식 재평가 큐에 적재된다. 같은 정정 재제출은 멱등(중복 이벤트 없음). **정정 제출의 신원은
  인증 활성 시 로그인 세션에서 강제**된다(클라이언트 자기보고가 아님) — 판정은 "현재 카드 owner"만
  통과하므로, 오너 변경 후엔 새 owner 세션만 정정할 수 있고 구 owner는 403이다.

**질문자 쪽 정정 배지(풀 방식):** 답변에 실려 온 `record_id`로 `GET /answer/{record_id}/correction`을
조회하면 — 정정이 있으면 원문 + 정정본(+정정 시각), 없으면 원문만 온다. 정정 주체·사유 같은 감독
내부값은 질문자에게 노출하지 않는다. 실 푸시/메신저 통지는 잔여(실 사용자 단계).

## 환경변수 시임

| env | 기본값 | 역할 |
|---|---|---|
| `AON_CLASSIFIER` | 미설정(키워드 규칙) | `llm`이면 실 claude(Haiku) 분류 — 자연어 질문 라우팅 |
| `AON_ROUTER` | 미설정(분류기 라우터) | `index`면 published 목차 토큰 매칭(`TwoStageRouter`) |
| `AON_PROVIDER` | 미설정(`claude-code`) | 답변 런타임 선택 — `claude-api`·`codex`는 in-process SDK + Knowledge Store 우선 접지 |
| `AON_PROVIDER_KEY` | 미설정 | 중앙 조직 API 키(ADR 0033 결정 2). 미설정 시 `ANTHROPIC_API_KEY` → 그것도 없으면 SDK 기본 |
| `AON_KNOWLEDGE_PATHS` | 카드별 `{agent_id}` 디렉터리 | 워커 지식 동기화의 명시 지정 경계(okf 루트 상대·`,`/`;` 구분) — 지정분만 올라간다 |
| `AON_KNOWLEDGE_SYNC_INTERVAL_SECONDS` | `0`(주기 재송신 없음) | 워커의 주기 재동기화 간격(시작 시 1회는 항상 발신) |
| `AON_KNOWLEDGE_STALE_SECONDS` | `1800`(30분) | 중앙 지식 낡음 임계 — 초과해도 답은 차단하지 않음(낡음 표식만) |
| `AON_PRESENCE_GRACE_SECONDS` | `0`(즉시 오프라인) | 워커 연결 끊김 후 오프라인 판정 유예 |
| `AON_DB` | 미설정(InMemory) | SQLite 영속(세션·토큰·감사 durable — 카드·답변 레코드 영속은 잔여) |
| `OPERATOR_SESSION_SECRET` | 미설정(인증 OFF) | 운영 면 세션 서명 키 — 설정 시 로그인 필수 |

## 데모 조직 (샘플 데이터)

`registry/`(유저 6·카드 5) + `samples/questions.jsonl`(골든셋 30) + `okf/`(OKF 번들).

| Agent Card | Owner | 담당(domains) |
|---|---|---|
| contract_ops | legal_lead | 계약 검토 |
| cs_ops | cs_lead | 환불, 보상 |
| finance_ops | finance_lead | 가격, 보상 |
| hr_ops | hr_lead | 채용·휴가·평가·급여이체(cannot_answer) |
| it_ops | it_lead | 계정·접근권한·보안 |

(`보상`은 cs_ops·finance_ops가 공유 → Contested 다툼. `급여이체`는 hr_ops가 cannot_answer로 차감 → Unowned.)

## 단일 진실 원천(SSOT)

- 제품 요구사항: [`docs/prd-v0.md`](docs/prd-v0.md)
- 기술 설계: [`docs/trd-v0.md`](docs/trd-v0.md)
- 작업 목록: [`docs/tasks-v0.md`](docs/tasks-v0.md)
- 도메인 용어집: [`CONTEXT.md`](CONTEXT.md)
- 아키텍처 결정: [`docs/adr/`](docs/adr/) (ADR 0001~0034)

## 스택

Python 3.12 · pydantic v2 · FastAPI/uvicorn · pytest · pyright(strict) · ruff · MCP SDK · anthropic SDK(선택 extra).
백엔드 운영 면은 빌드 없는 순수 HTML/CSS/fetch, 별도 사용자 UI는 [`frontend/`](frontend/)(Next.js 14). 답변 런타임은
`claude -p` 헤드리스(`ClaudeCodeRuntime`) 또는 in-process 공급자 SDK(`ClaudeApiRuntime`·`CodexApiRuntime`) —
in-process 런타임은 중앙 Knowledge Store 우선 접지·디스크 폴백, 자격증명은 중앙 조직 키 1개(ADR 0033).
