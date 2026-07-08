# ADR 0037 — Co-grounding(다중 에이전트 지식 접지): Authority primary 단일 귀속 · 포트 최소 진화 · 답+합의 병행

- 상태: 채택(Accepted)
- 날짜: 2026-07-06 (사용자 승인)
- 계보: ADR 0036 결정 5(에스컬레이션 사다리 ①·COMPLEMENTS 엣지가 다중 접지를 구동하는 다리)의 **첫 코드 액션 구체화**. ADR 0024(발화 스레드 `assemble_context`·이름 충돌 해소 대상)·ADR 0027(공급자 런타임 포트 무변경 원칙)·ADR 0033(중앙 답변·`Knowledge Store`)·ADR 0028(§17 stage-2·`TwoStageRouter`·Contested tie-break)·ADR 0002/0008/0014(Contested·1인칭 합의·Manager 큐)·ADR 0012(`Answer Record`·`BackupReview` HITL)의 후속(supersede 아님).
- 근거 본체: [`docs/trustgraph-eval-2026-07-06.md`](../trustgraph-eval-2026-07-06.md) §3(답변 통증 = 경계 넘는 접지)·§9-3(D1 단일 접지=절반 답 vs D2 이중 접지=완전한 답 실증).

---

## 맥락 — 측정된 통증(경계 넘는 관계형 답변)의 첫 처방

ADR 0036이 라우팅·답변 통증의 성격을 확정했다: 지식 깊이가 아니라 **"라우팅 × 단일 에이전트 접지"의 구조적 경계**. 근거가 두 도메인에 갈리는 관계형 질문("환불불가면 청약철회 안 되나?")이 단일 카드 OKF만 주입되므로 구조적으로 절반만 답된다(§9-3 D1/D2 실증). ADR 0036 결정 5는 이 통증의 첫 코드 액션을 **다중 에이전트 지식 접지**(에스컬레이션 사다리 ①·외부 그래프가 아니라 우리 자신의 것)로 박았다.

이 ADR은 그 첫 액션의 **도메인 shape·경계·불변식 정합**을 확정한다. 되돌리기 어려운 두 결정 — (1) 여러 owner 지식에 걸친 답의 **Authority 귀속 모델**, (2) 공급자 런타임 **포트 시그니처 진화** — 이 있어 ADR로 박는다. 여기에 사용자가 결정한 **답+합의 병행**(Contested 질문이 답과 ConflictCase를 동시 산출)이 만드는 새 긴장(이중 기록·HITL 이중 경로·스트리밍 배타)을 결정 안에서 명시적으로 해소한다.

**이건 접지 계층 결정이지 라우팅 종착·admission 재정의가 아니다** — 4대 불변식은 이 ADR이 건드리지 않는다(§불변식 자체점검).

---

## 결정

### 결정 1 — 명명: co-grounding(다중 접지) · `session.assemble_context`와 구분

새 개념을 **co-grounding(다중 접지·Multi-Agent Grounding)**으로 명명한다. 접지 대상을 담는 값 객체는 **`GroundingSet`**, 그 대상을 고르는 정책 포트는 **`GroundingSelector`**다.

**`session.assemble_context`(발화 스레드)와 다른 축임을 명문화한다(_Avoid_):**

- `session.assemble_context(session, current_question) -> str`(ADR 0024 결정 3·`session.py:250`)는 **시간 축** — *한 사용자의 발화 스레드*(과거 턴 `User:/Assistant:`)를 멀티턴 `context: str`로 조립한다. 이미 구현됨.
- **co-grounding**은 **지식 축** — *여러 owner의 OKF 지식*을 하나의 질문에 함께 접지한다.
- 둘은 `build_provider_request`에서 **다른 인자로 만난다**: `assemble_context` 산출물 → `context=`, co-grounding 산출물 → `okf`(접지) 슬롯. **절대 같은 이름/같은 슬롯에 섞지 않는다**(유비쿼터스 언어 한 용어=한 뜻).
- tasks 790의 "assemble_context 미구현·항상 None"은 느슨한 표현이었다. `context`(멀티턴)는 구현됐고, 부재한 건 이 co-grounding(okf 다중화)이다. tasks·docs에서 이 축은 **co-grounding**으로 표기한다.
- **fan-out이 아니다**(CONTEXT §245 _Avoid_ 정합) — fan-out은 여러 담당이 각자 답을 내 합치는 것(Phase 9 연기). co-grounding은 **담당 1명이 여러 지식을 근거로 하나의 답**을 낸다. 접지 원천만 다중, 실행·답변자·책임은 단수.

### 결정 2 — Authority = primary 단일 귀속 (supporting은 sources 레이블만)

여러 owner 지식에 걸친 답이라도 **책임(`answered_by`)은 primary owner 하나**다. supporting owner는 자기 지식이 *인용*됐을 뿐 이 답의 책임·검토·정정 주체가 아니다.

- **`Answered.answered_by`·`AnswerRecord.answered_by`는 primary 단일** — 무변경. 다중 기여자·primary+contributors 모델을 **채택하지 않는다.**
- **근거 = Authority 중앙 불변식.** primary는 라우팅이 정한 담당(`card.domains` 게이트 통과)이다. supporting을 answered_by에 넣으면 카드 자기보고도 routing_rules 선언도 아닌 **새 권한 창설**이 된다 — 금지(ADR 0004). supporting은 근거 제공자이지 권한 주체가 아니다.
- **owner 거버넌스 모델 정합(CONTEXT §22).** 답의 책임은 primary owner가 진다. supporting owner는 이 답을 검토·정정할 주체가 아니다(그 지식 자체의 정정은 그 owner의 OKF 정정 루프로 흐르지, 이 답의 정정 주체를 가르지 않는다).
- **supporting은 `sources`에만 반영.** `Answered.sources: tuple[str, ...]`는 이미 다중이다. supporting 카드의 `knowledge_sources`(출처 **레이블**)를 primary의 것과 병합해 `sources`에 투영한다 — "이 답이 어떤 지식에 근거했나"는 정직하게 노출되되 책임은 primary에 남는다. **agent_id·confidence·candidate 목록은 절대 sources에 넣지 않는다**(레이블만·노출 불변식·결정 6).

열린 질문(정직): supporting owner가 "내 지식이 오용됐다"고 볼 여지 — v0는 supporting을 *같은 intent의 인접 후보*(contested candidates)로만 한정하므로 도메인 근접성이 높아 오용 위험이 낮다. 후속 엣지 기반(결정 4)에서 이 문제가 커지면 재검토(v0 범위 밖 명시).

### 결정 3 — 포트 최소 진화: `grounding: str | None` 옵셔널 인자 · 중앙 KnowledgeStore 조립

co-grounding은 **포트 시그니처를 최소로만 진화**시킨다 — `AgentRuntime.answer`·`RuntimeDispatcher.dispatch`에 **`grounding: str | None = None` 옵셔널 인자 하나**를 추가한다. `_resolve_okf`·`build_provider_request`·`Answer` 계약은 무변경.

- **`grounding=None`(기본)**: 런타임이 기존처럼 `_resolve_okf(card.agent_id)`로 자기 접지 — **회귀 0**(기존 모든 호출부 100% 무변경).
- **`grounding=str`**: 그 문자열이 `build_provider_request(..., okf=grounding)`의 접지 슬롯으로 실려 자기 해소를 대체한다. 문자열은 `### {agent_id}\n{body}` 섹션들을 `read_okf_bundle`·`resolve_knowledge_text`와 **같은 포맷**으로 병합한 것(포맷 일관성).

**다중 접지 문자열은 중앙(`AskOrg`)이 `KnowledgeStore`로 조립한다 — 워커가 여러 카드를 받아 각자 접지하는 B안은 기각한다:**

- **B안(카드 여럿을 포트에 넘김) 기각 사유 = owner 격리 위반.** 크로스머신 워커는 *자기 지식만* 접근 가능(중앙 토큰 0·비소유 격리·ADR 0030). supporting owner의 OKF를 워커가 읽으면 격리가 깨진다.
- **A안(중앙 조립) 채택.** 중앙은 이미 모든 agent_id 지식을 `KnowledgeStore`로 갖는다(ADR 0033). 중앙이 GroundingSet의 각 agent_id를 `resolve_knowledge_text`로 해소·병합해 하나의 `grounding` 문자열로 만들어 dispatch로 흘린다. 격리 위반 0·모델 토큰 0(스토어는 텍스트일 뿐).
- 이는 stage-2 `EmbeddingConfidenceAssessor`가 "인프로세스=디스크 직독, 크로스머신=중앙 스토어"로 가른 선례(CONTEXT §260)와 동형이다.

**포트 무변경 원칙(ADR 0027)과의 정합**: 옵셔널 인자·기본값 폴백이라 기존 계약의 *진짜 변경*이 아니라 *하위호환 확장*이다(ADR 0033의 `knowledge_store` 옵셔널 주입, ADR 0031의 스트리밍 seam 추가와 같은 결). `AgentRuntime` 포트의 답변 계약(`answer(question, card, context) -> Answer`)의 정신은 보존된다.

### 결정 4 — `GroundingSelector` seam: v0 `ContestedGroundingSelector` · 후속 `EdgeGroundingSelector`

접지 대상 선택은 **교체 가능한 포트**로 추상한다 — 접지 원천 선택 정책을 하드코딩하지 않는다(주입 seam).

```python
class GroundingSet(BaseModel, frozen=True):
    primary: AgentCard
    supporting: tuple[AgentCard, ...] = ()      # primary 제외·중복 금지
    def agent_ids(self) -> tuple[str, ...]: ...  # (primary, *supporting)

class GroundingSelector(Protocol):
    def select(self, decision: RoutingDecision) -> GroundingSet | None: ...
```

- **v0 = `ContestedGroundingSelector`**: `decision`이 `Contested`면 candidates 전원을 GroundingSet으로 접는다(primary = 결정 5의 tie-break가 고른 top-1, supporting = 나머지). `Routed`/`Unowned`면 `None`(단일 접지 폴백·회귀 0).
- **후속 = `EdgeGroundingSelector`**(ADR 0036 §9·에스컬레이션 사다리): `Routed`에서도 primary의 COMPLEMENTS 엣지 이웃을 supporting으로 견인한다(non-contested 관계형 질문). **v0는 엣지·그래프를 짓지 않는다** — 포트 시그니처만 후속을 받을 수 있게 넓게 둔다(후속 selector가 그래프 스토어를 생성자 주입으로 받으면 `select(decision)` 시그니처는 그대로 견딤).
- `GroundingSet`은 `Contested.candidates`·`Routed.collaborators`와 대칭(primary 분리·추가 튜플·중복 금지)이되, Contested와 달리 **primary가 정해진다**(담당 미정이 아니라 근거 확장).

### 결정 5 — 긴장 처분: 답+합의 병행 (co-ground 답 + ConflictCase 병존)

**한 Contested 질문은 이제 Answered(answered_by=primary)와 ConflictCase를 *동시에* 산출한다.**

- **답을 즉시 낸다**: 후보 전원 co-ground → co-grounded 답 → `answered_by` = stage-2 최고신뢰 primary. **동률이면 결정론 tie-break**(후보 정렬 순서 — 예: agent_id 사전순 first, 또는 stage-1 score 순 후 agent_id 순). tie-break는 라우터/selector 정책값이지 임의·비결정론 금지.
- **동시에 ConflictCase도 그대로 연다**: 담당 결정은 사람 1인칭 합의에 남긴다(ADR 0008·미아 없음 안전망 보존). 기존 Contested arm의 부수효과(`open_for_intent` 중복 가드·`open_case`·push 통지)를 그대로 수행한다.

이 병행이 만드는 세 긴장을 아래에서 명시적으로 푼다.

#### 5-(a) 이중 산출 = 이중 기록 아님 (전이 ≠ 기록 정합)

한 질문 → **`AnswerRecord` 1건 + `ConflictCase` 1건**. 이 둘은 audit에서 중복·충돌 기록이 아니다 — **다른 축·다른 저장소·다른 관심사**다:

- **`AnswerRecord`(answered_by=primary·`AnswerRecordStore`)** = "중앙이 낸 답의 감사 단위"(기록 축·ADR 0033 결정 4). 담당자 스코어카드·질문자 정정 배지의 원천.
- **`ConflictCase`(candidates·`ConflictCaseStore`)** = "미해소 담당 다툼의 처리 단위"(도메인 전이 축·ADR 0008). 후보 owner 처리함·1인칭 합의의 원천.
- **`AuditEntry`**(절차 로그)는 `decision`(=Contested 원형)을 담아 다툼 사실을 이미 기록한다. co-grounded 답이 나가도 `decision`은 여전히 Contested이므로 audit의 라우팅 사실은 **정직하게 "Contested였다"**로 남는다(답이 났다고 Routed로 위장하지 않는다). 답 사실은 `AnswerRecord`가, 다툼 사실은 `ConflictCase`가, 절차 사실은 `AuditEntry`가 각각 정확히 1건씩 담는다 — **세 축이 겹치지 않는다.**
- 이중 기록 우려의 핵심: "같은 답이 두 번 기록되나?" — **아니다.** 답은 `AnswerRecord` 1건뿐. `ConflictCase`는 답을 담지 않는다(question·candidates·status만). 전이(다툼)와 기록(답)이 분리된다(전이 ≠ 기록).

#### 5-(b) HITL 이중 경로 공존 (정정 vs 합의 — 독립 축, 소급 무효화 없음)

한 질문에 두 HITL 경로가 동시에 존재한다 — primary owner의 **정정 경로**(`AnswerRecord`·`BackupReview`)와 후보들의 **1인칭 합의 경로**(`ConflictCase`·`ConcurOnPrimary`). 이 둘의 관계를 **독립 축**으로 박는다:

- **정정 카운트 이중집계 없음.** 정정은 `AnswerRecord`(answered_by=primary) 단일 축으로만 집계된다 — co-grounded 답의 정정 대상은 primary owner 하나(결정 2). ConflictCase는 정정을 세지 않는다(합의 표만 센다). 두 저장소가 다른 것을 세므로 이중집계 구조적 불가.
- **처리함 혼선 없음.** primary owner는 자기 스코어카드에서 이 답을 본다. 후보 owner들(primary 포함)은 자기 처리함에서 ConflictCase를 본다 — 둘은 다른 화면·다른 액션(정정 vs 합의 표). primary owner는 두 곳에 다 뜨지만 액션이 다르므로 혼선이 아니라 정합이다("내가 낸 잠정 답을 정정할 수도, 담당 다툼에 표를 던질 수도 있다").
- **합의 종결이 답을 소급 무효화하지 않는다(독립 축·핵심 결정).** ConflictCase가 resolved되어 담당이 primary가 아닌 다른 후보로 정해져도, **이미 나간 co-grounded 답(`AnswerRecord`)은 소급 철회·무효화되지 않는다.** 근거: (1) 답은 이미 사용자에게 전달됐고 append-only 기록이다(전이 ≠ 기록 — 기록은 되돌리지 않는다). (2) 합의 결론은 **미래 라우팅**을 바꾼다(`Precedent`로 기록돼 다음 같은 intent 질문을 새 담당으로 라우팅) — 과거 답을 바꾸는 게 아니다. (3) 답이 틀렸다면 그건 소급 무효화가 아니라 **정정 경로**(`AnswerRecord`·`Correction`)로 처리된다 — 정정 주체는 여전히 그 답의 answered_by(당시 primary). 합의로 담당이 바뀌면 *이후* 답의 primary가 바뀔 뿐.
- **열린 질문(정직):** "잠정 답이 나갔는데 합의가 다른 담당으로 났다"는 상황에서 사용자에게 "담당이 바뀌었으니 답을 다시 보라" 같은 후속 통지가 필요한가 — v0는 하지 않는다(정정 배지 pull 경로로 충분·과도 엔지니어링 회피). 이 후속 통지 필요성은 실 사용 관찰 후 재검토(v0 범위 밖·후속 훅 미설계).

#### 5-(c) 사용자向 노출 — 답만 나가고 "contested"는 새지 않는다 (스트리밍 배타 자동 보존)

이게 병행 결정의 **가장 날카로운 지점**이다(스트리밍 done/pending 상호배타·CONTEXT §117과 충돌 가능). 결정:

- **co-grounded 답은 `Answered`(블로킹)/`meta→token*→done`(스트리밍)으로 나간다.** contested라는 내부 사실은 노출 불변식상 **절대 사용자向에 새지 않는다** — "잠정(provisional)·담당 확정 전" 같은 표식을 **붙이지 않는다.** 사용자는 정상 답 하나를 본다.
- **ConflictCase open은 부수효과(side-effect)이지 사용자向 프레임이 아니다.** 후보 owner 처리함·push 통지로만 흐른다(조직 내부). 사용자向 응답에는 어떤 흔적도 없다.
- **∴ 스트리밍 done/pending 배타는 자동으로 보존된다.** 병행이라도 사용자는 **`done` 하나만** 받는다(`pending`을 함께 받지 않는다). ConflictCase는 프레임이 아니라 서버 측 부수효과이므로 "한 스트림에 done과 pending 동시"가 **구조적으로 발생하지 않는다.** 기존 `AskEvent` sealed sum·이벤트 순서 불변(`meta→token*→done`)·상호배타 계약은 **무변경**이다.
- **이것이 병행 결정의 스트리밍 계약과의 화해 지점이다**: "답+합의 병행"은 *산출물*이 둘(Answered + ConflictCase)이라는 뜻이지 *사용자向 프레임*이 둘이라는 뜻이 아니다. 사용자向은 답 프레임 하나, ConflictCase는 owner측 부수효과. 이 구분이 done/pending 배타를 깨지 않고 병행을 성립시킨다.
- **`handle`(블로킹) 흐름 진화**: 기존 Contested arm은 `Pending(kind="contested")`를 냈다. 병행에서는 **Contested를 (co-ground 답을 내는) Routed-유사 답 경로로 흘리되 ConflictCase 부수효과를 유지**한다 — 즉 Contested arm이 "Pending 반환" 대신 "co-grounded dispatch → Answered 반환 + ConflictCase open"으로 바뀐다. `handle_stream`도 대칭: Contested가 `PendingEvent` 단독 대신 `meta→token*→done`(co-grounded 답) + ConflictCase 부수효과.

  **정직한 경계 표시(mcp-runtime-engineer 협의 필요)**: 이 arm 재배선은 dispatch가 GroundingSet의 supporting 지식을 실제 조립·전달하는 실 배선(중앙 KnowledgeStore 다중 조회·크로스머신 격리)을 요구한다. **도메인 shape·프레임 계약·부수효과 경계는 이 ADR이 확정**하지만, 실 dispatch 배선·프롬프트 실 접지·크로스머신 조립은 **mcp-runtime-engineer** 영역이다. 특히 "Contested를 답 경로로 흘릴 때 dispatch가 어느 시점에 grounding 문자열을 받는가"(라우터가 Contested를 낸 뒤 selector→조립→dispatch)의 배선 순서는 mcp-runtime-engineer가 확정한다.

### 결정 6 — 노출 불변식 (새 노출 필드 0)

- `GroundingSet`·supporting agent_id·candidates·confidence·grounding 메모는 **사용자向 투영에 절대 실리지 않는다**(test_web `_LEAKY_KEYS` 확장 단언 대상).
- `Answered`로 나가는 건 primary의 `answered_by` + 병합 `sources`(레이블만)뿐. `sources` 병합은 `card.knowledge_sources`(출처 레이블) 합집합이지 agent_id가 아니다.
- co-grounding이 노출하는 **새 필드는 0**이다 — 노출 경계 무관(보존).

---

## 근거

- **측정된 통증 정조준(ADR 0036 §9-3).** D1(단일 접지)=절반 답 vs D2(다중 접지)=완전한 답이 실 답변으로 실증됐다. co-grounding이 그 D2를 프로덕션 경로로 옮기는 최소 수술이다.
- **포트 최소 진화가 플랫폼·큰 계약 변경보다 우세.** 옵셔널 인자 하나·기본값 폴백이라 회귀 0이면서 후속(엣지 기반)이 같은 seam에 꽂힌다. `GroundingSelector` 포트가 에스컬레이션 사다리(①→②③④)의 교체 지점이다.
- **답+합의 병행이 "답이 지금 필요"와 "담당은 사람이 정한다"를 둘 다 만족.** 잠정이라도 답을 내(통증 즉시 완화) 동시에 다툼을 사람에게 남긴다(Authority·1인칭 합의 보존). 소급 무효화 없음·독립 축 결정으로 두 경로가 서로를 오염시키지 않는다.
- **스트리밍 배타는 "산출물 둘 ≠ 프레임 둘"로 화해.** ConflictCase를 사용자向 프레임이 아니라 owner측 부수효과로 두면 done/pending 배타가 자동 보존된다 — 기존 `AskEvent` 계약 무변경.

## 계보 (기존 ADR과의 관계 — 전부 계승·supersede 없음)

- **ADR 0036 결정 5 — 구체화.** "다중 에이전트 접지가 첫 코드 액션·COMPLEMENTS가 다리"를 shape·포트·불변식으로 실체화. 에스컬레이션 사다리 ①이 이 ADR.
- **ADR 0024(발화 스레드 `assemble_context`) — 이름 충돌 해소.** co-grounding(지식 축)과 `assemble_context`(시간 축)를 명시 구분(결정 1). `context`/`okf` 다른 슬롯.
- **ADR 0027(공급자 런타임 포트) — 무변경 정신 계승.** `grounding` 옵셔널 인자는 하위호환 확장(포트 계약 정신 보존).
- **ADR 0033(중앙 답변·`Knowledge Store`) — 조립 원천.** 다중 접지 문자열은 중앙 KnowledgeStore로 조립(격리·중앙 토큰 0 보존).
- **ADR 0028 §17(stage-2·`TwoStageRouter`) — tie-break 재사용.** co-grounded 답의 primary 선정은 stage-2 최고신뢰 + 결정론 tie-break(결정 5).
- **ADR 0008/0014(Contested·1인칭 합의·Manager 큐) — 병존.** ConflictCase는 그대로 열리고 사람 합의로 종결. 답+합의 병행이 이를 대체하지 않고 병행.
- **ADR 0012(`Answer Record`·`BackupReview` HITL) — 이중 경로 공존.** 정정 축(AnswerRecord)과 합의 축(ConflictCase)이 독립(결정 5-b).

## 4대 불변식 + 노출 불변식 자체점검

- **미아 없음 — 보존.** co-grounding은 접지 *품질*이지 라우팅 *종착*이 아니다. selector `None`이면 단일 접지 폴백. Contested는 여전히 ConflictCase를 열어(병행) 사람 합의로 종결(안전망 유지)·0매칭→Unowned/root escalation 무변경. 질문이 떨어질 자리 없음.
- **유효하지 않은 카드는 등록되지 않는다 — 무관(보존).** admission 무변경. GroundingSet에 드는 카드는 이미 등록된 카드(Contested.candidates는 admission 통과분).
- **Authority 중앙 — 보존.** `answered_by` = primary 단일(결정 2). supporting은 근거이지 권한 창설 아님. tie-break는 권한 안 순위(authorized 후보 사이)이지 권한 생성 아님.
- **전이 ≠ 기록 — 보존.** 접지=실행(도메인). AnswerRecord(답·기록)·ConflictCase(다툼·전이)·AuditEntry(절차·로그)가 다른 축·다른 저장소로 정확히 1건씩(결정 5-a). 합의 종결이 과거 답을 소급 안 함(결정 5-b) — 기록은 되돌리지 않는다.
- **노출 불변식 — 보존.** co-grounding 새 노출 필드 0(결정 6). contested·GroundingSet·supporting agent_id·confidence 미노출. 스트리밍 done/pending 배타 자동 보존(결정 5-c).

## 결과

- **v0 shape 확정**: `GroundingSet`(값 객체)·`GroundingSelector`(포트)·`ContestedGroundingSelector`(v0 구현)·`answer`/`dispatch`에 `grounding: str | None` 옵셔널 인자. Contested arm이 답+합의 병행으로 진화(handle·handle_stream 대칭).
- **후속 훅(v0 미포함·seam만)**: `EdgeGroundingSelector`(COMPLEMENTS 엣지·ADR 0036 §9)가 같은 포트에 꽂힘. 엣지·그래프 저작은 북극성(ADR 0036 결정 4·트리거 대기).
- **CONTEXT 용어 추가**: co-grounding(다중 접지)·`GroundingSet`·`GroundingSelector` + `assemble_context`(발화 스레드) vs co-grounding(지식 접지) _Avoid_.
- **역참조**: `docs/tasks-v0.md`(T9.1b)·`docs/trustgraph-eval-2026-07-06.md`에 이 ADR 번호(0037) 링크.
- **mcp-runtime-engineer 협의**: dispatch 실 배선(중앙 KnowledgeStore 다중 조회·크로스머신 격리·Contested→답 경로 배선 순서)은 shape 확정 후 mcp-runtime-engineer 영역.
- **슬라이스 D 완료(2026-07-07·mcp-runtime-engineer)** — 실 배선·프로덕션 활성화·owner 격리 실증(`tests/test_co_grounding_wiring.py`, 게이트 내 결정론):
  - **실 resolver = `provider_runtime.make_grounding_resolver(knowledge_store)`** — `GroundingSet`의 각 agent_id를 중앙 `KnowledgeStore`에서만 `resolve_knowledge_text`로 해소(단일 접지 `_resolve_okf`와 같은 함수 재사용). **결정 3 B안 기각을 코드로 실증**: `okf_root=None` 고정으로 디스크 폴백을 원천 차단 — resolver는 워커 로컬 디스크·남의 owner OKF에 접근하지 않는다(스토어 미보유 agent_id는 ""·디스크 번들 있어도 폴백 0).
  - **프로덕션 ON(config 플래그 없음·사용자 승인)**: `demo.build_demo(knowledge_store=)` 주입 시 `AskOrg`에 `ContestedGroundingSelector()`+실 resolver를 꽂고, `web.create_app`이 중앙 `_knowledge_store`를 넘겨 실 `/ask`의 contested 질문이 co-grounding 경로를 탄다. 미주입이면 OFF(기존 `Pending(contested)` 보존·옵트인 스위치).
  - **활성화 낙수(의도된 행동 전이)**: create_app 기반 기존 contested 테스트의 `/ask` 응답이 `pending/contested`→`answered`로 전이(ConflictCase는 그대로 열려 concur/Manager/판례 흐름 무회귀·결정 5). Routed/단일 경로 무영향.
  - **게이트 밖 실증(2026-07-08·실 claude)**: D1→D2를 프로덕션 OKF로 재현 확인. 질문 "장애 24시간 초과 Pro 고객 다음 달 감면액?" — **단일 접지(cs_ops만)=반쪽 답**("50% 감면이나 Pro 가격이 없어 금액 산출 불가"), **병합 접지(cs_ops 보상규칙 + finance_ops Pro가격)=완전한 답**("90,000×50%=45,000원"). §9-3 D1/D2를 프로덕션 데이터로 실 LLM 재현. **방법 caveat(정직)**: 앱 내 `ClaudeApiRuntime`(API 키 필요)이 이 환경에 부재하고 `ClaudeCodeRuntime`은 grounding을 무시하므로, 게이트 내 테스트가 *병합 grounding의 런타임 도달*을 이미 잠근 상태에서 동일 grounding·프롬프트 구조(`build_provider_request` system=persona+OKF)를 실 `claude -p`에 직접 투입해 "실 LLM 종합 품질" 축만 확인(스파이크 방식·§9-2/9-3와 동형). **앱 through-API end-to-end**(`/ask`→dispatch→`ClaudeApiRuntime`→실 API)는 여전히 API 키 필요·미실행.
