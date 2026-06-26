# Agent Org Network

조직 구성원이 **자기 업무 에이전트(Agent Card)** 를 만들어 *자기 지식으로 답하게* 하고,
중앙(MCP 서버)이 **담당·권한·판례**를 기준으로 질문을 알맞은 에이전트에 연결해 답을
돌려주는 협업형 AI 조직 — walking skeleton(v0).

중앙은 지식을 소유하지 않는다. **연결자**다. 답은 담당 에이전트(= 그 owner의 Claude Code)가
자기 **OKF 지식 번들**을 읽어서 한다. 모르면 지어내지 않고 사람(Manager)에게 넘긴다.

> 🎬 **전 기능 시나리오 투어**: [`docs/scenario.html`](docs/scenario.html) — 브라우저로 열면 데모 조직이
> 모든 기능(라우팅·합의·판례·미아·승인·분산·백업·인증·모니터링·그래프·MCP·eval)을 거치는 체계적
> 시나리오를 단계별로 볼 수 있다.

## 핵심 개념

- **Agent Card** — 한 책임(직무)의 담당 영역·답변 범위·이관 규칙을 선언한 YAML. 정체성(`agent_id`)은
  담당자가 바뀌어도 불변. 코드 밖 `registry/agents/*.yaml`로 등록.
- **RoutingDecision** — 질문의 처분: `Routed`(담당 정해짐) / `Contested`(후보 다툼) / `Unowned`(미아).
- **Precedent(판례)** — 다툼을 사람이 1인칭 합의로 풀면 `Resolution`이 판례로 쌓여 라우터가 학습한다.
- **OKF 번들** — owner가 자기 지식을 마크다운+프론트매터로 `okf/<agent_id>/`에 둔다. 워커의 Claude Code가
  그 디렉터리를 cwd로 *읽어* 답한다(벡터DB·RAG 0).
- **불변식** — ① 어떤 질문도 미아로 남지 않는다(0매칭→Manager) ② 유효하지 않은 카드는 등록 안 됨
  ③ 권한은 중앙만 선언 ④ 전이 ≠ 기록 ⑤ 답엔 항상 담당·신뢰 상태(승인/초안/백업/출처)가 붙는다.

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
uv run pytest        # 단위 테스트(결정론) — 900 passed
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

채팅(`/ask`)은 익명이고, **운영 면(처리함·큐·모니터링·그래프·빌더)은 인증**이 필요하다.
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

### 2) 분산 — 중앙 + owner 워커 (WebSocket)

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
- 아키텍처 결정: [`docs/adr/`](docs/adr/) (ADR 0001~0023)

## 스택

Python 3.12 · pydantic v2 · FastAPI/uvicorn · pytest · pyright(strict) · ruff · MCP SDK ·
프론트는 빌드 없는 순수 HTML/CSS/fetch. 답변 런타임은 `claude -p` 헤드리스(중앙 API 키 0).
