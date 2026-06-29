# HITL 런타임 토글 — LLM 초안→owner 검토·전송은 *기존 draft_only/Approval 재사용*이다(새 기계 아님)

상태: accepted (2026-06-26) · **Phase 9의 ADR-B** · ADR 0012 결정 4(`Answer.mode` 신뢰 상태)·T2.5(`Routed.requires_approval`→`AskOrg._apply_approval_gate`→`mode="draft_only"`)의 *런타임 토글 확장* — 새 도메인 기계 0 · ADR 0014 결정 4(Approval은 게이트지 escalation 아님)와 정합 · CONTEXT(Approval·Answer 절에 런타임 토글 추가)·PRD §3·§5·TRD §4·§6 갱신 · **ADR 0029(OKF 자동 저작)가 정신 재사용**: 이 ADR의 "LLM 초안→owner 검토→확정"을 *지식 저작*에 본뜸(자동 산출=초안·owner 검토 거쳐 commit/publish — 단 저작은 답 mode 토글과 다른 축이라 `hitl_to_mode` 그대로 안 쓰고 staged 상태기계 패턴만 본뜸).

## 맥락 — 새 기계가 아니라 기존 게이트의 런타임 토글

Phase 9 제품 비전에서 답은 *하이브리드 HITL*이다 — LLM이 초안을 만들고, owner가 검토·수정해 전송하거나(HITL on) 자동 전송한다(HITL off). 운영자가 *런타임에* 이 토글을 켜고 끈다(확정 결정 5).

핵심 통찰: **이건 새 도메인 기계가 아니다.** 이미 있다.

- `Routed.requires_approval`이 True면 `AskOrg._apply_approval_gate`가 답을 `mode="draft_only"`로 내린다(T2.5, `ask_org.py:161`). `draft_only`는 "사람 승인 전까지는 초안"이라는 신뢰 상태다(CONTEXT Answer 절·ADR 0012 결정 4).
- `full`은 "owner 실시간 답, 그대로 사용자에게"다.

그러니까 **HITL on = `draft_only`(owner 검토·수정·전송)·HITL off = `full`(자동)**이 정확히 기존 두 mode다. Phase 9가 더하는 것은 *기계*가 아니라 그 게이트를 *런타임에 토글*하는 능력 + 그 토글을 mode로 매핑하는 순수 로직이다. `_apply_approval_gate`의 mode 강제 패턴(라우팅이 강제·워커 자기보고 아님)을 그대로 재사용·확장한다.

설계 제약:

1. **결정론 게이트.** 토글 상태·→mode 매핑은 게이트 내 순수 로직. 실 owner UI 검토·전송 조작은 게이트 밖(T9.7 수동).
2. **기존 자산 재사용.** `_apply_approval_gate`·`Answer.mode`·`Routed.requires_approval`을 확장하지 새로 짜지 않는다 — *가장 게이트 내·외부 결정 0*(tasks line 261 — 권장 조기 진입).

## 결정

### 1. HITL 토글 = 에이전트별 + 콘솔 런타임 토글, 기본값은 카드 approval 정책 시드

- **에이전트별 토글**: 각 Agent Card(agent_id)마다 HITL on/off 상태를 둔다 — 어떤 담당은 사람 검토를 거치고(민감), 어떤 담당은 자동.
- **콘솔 런타임 토글**: 운영자가 콘솔(T9.2 POST 명령)에서 그 상태를 *런타임에* 바꾼다. 재배포·재시작 없이.
- **기본값 시드**: 토글 초기값은 그 카드의 *approval 정책에서 시드*한다 — 카드 `approval_when`이 그 intent를 들면(Approval 게이트가 걸리는 담당) HITL on으로, 안 들면 off로 시작. 즉 기존 under-claim 자기보고(ADR 0004 — owner가 "사람 사인 필요"로 스스로 좁힘)가 *기본값*을 정하고, 운영자가 런타임에 덮어쓴다(중앙 권위).
- 토글 상태는 *신뢰 게이트 상태*이지 *권한 선언*이 아니다 — Authority 중앙(routing_rules.yaml)을 안 건드린다(아래 불변식).

### 2. HITL → `Answer.mode` 매핑 = 순수 함수, `_apply_approval_gate` 재사용·확장

```python
def hitl_to_mode(hitl_on: bool) -> AnswerMode:
    return "draft_only" if hitl_on else "full"
```

- `hitl_on → mode="draft_only"`(owner 검토·수정·전송 대기) · `off → "full"`(자동 전송).
- **`_apply_approval_gate`의 mode 강제 패턴 재사용**: 라우팅/운영 토글이 강제하지(워커·런타임 자기보고 아님) Agent Runtime이 자기 mode를 자처하지 않는다 — 디스패처가 backup을 강제 하향하고 라우팅이 draft_only를 강제하는 그 정신.
- **mode 우선순위 보존(ADR 0012)**: HITL on이 `mode="draft_only"`를 *격상*하되 `backup`은 덮지 않는다(`backup`이 더 강한 하향 — owner 미검토라 승인 대기보다 약한 신뢰가 맞다). `full`→`draft_only`만 격상, `draft_only`/`backup`은 그대로. 기존 `_apply_approval_gate`의 우선순위 로직을 그대로 따른다(`ask_org.py:188`의 `if reply.mode == "backup": return reply`).
- **`requires_approval`과의 합류**: 카드 `approval_when` 기반 `requires_approval`(라우팅 결정에 붙는 게이트)과 런타임 HITL 토글은 *둘 다 draft_only로 내리는* 같은 결과를 낳는다 — OR 결합(둘 중 하나라도 on이면 draft_only). 카드 정책이 approval을 요구하면 운영자가 HITL을 off로 내려도 그 카드는 draft_only를 유지한다(under-claim 자기보고는 *덮을 수 없는 보수 신호* — owner가 "이건 사람 사인 필요"로 좁힌 걸 운영자가 풀어선 안 됨). 반대로 카드가 approval을 안 요구해도 운영자가 HITL on을 켜면 draft_only로 올린다(운영 안전 상향은 허용). **풀기(완화)는 카드 정책에 막히고, 조이기(상향)는 런타임 토글로 가능** — 안전 방향 단조성.

### 3. 실 승인 행위(draft→full 풀기)는 이 ADR 범위 밖 — 게이트 *표시*까지

T2.5 경계를 그대로 유지한다 — 이건 *게이트 표시*(답을 draft_only로 보이기)까지다. owner가 로컬 UI에서 초안을 검토·수정·전송하는 실 행위(draft→full 풀기)는 owner 클라이언트(T9.7·게이트 밖 수동)·Manager 큐 별 탭(ADR 0014 결정 4)의 영역이다. 이 ADR은 *mode를 어떻게 내리고 누가 토글하나*까지.

## 근거

- **새 기계 0** — HITL은 이미 있는 `draft_only`/`full` 두 신뢰 상태에 *런타임 토글*만 얹는다. 새 타입·새 store·새 전이를 안 만든다(tasks line 256 — "새 기계가 아니라 기존 draft_only/Approval 재사용").
- **mode 강제 패턴 정합** — `_apply_approval_gate`가 라우팅 강제로 mode를 내리는 그 자리에 런타임 토글을 합류시킨다(같은 함수·같은 우선순위). 디스패처 backup 하향·라우팅 draft_only 강제와 한 패턴.
- **under-claim 단조성** — owner가 카드로 좁힌 보수 신호(approval_when)는 운영자가 *풀 수 없고*(안전), 운영자는 *조일 수만* 있다(상향). Authority 중앙(중앙이 권한 선언)과 under-claim 자기보고(ADR 0004 — 카드는 자기를 좁히기만)가 토글에서도 일관된다.

## Consequences

- **HITL 토글 상태 보관** — 에이전트별 on/off. *전이 없는 운영 설정값*이라 별 store가 과한지(ADR 0022 결정 3 "정적 매핑에 store는 과도"의 정신) vs 런타임 토글이라 상태가 바뀌므로 가벼운 in-memory 토글 맵으로 둘지 — **shape: in-memory 토글 맵(`agent_id → bool`)이 MVP**(런타임에 바뀌므로 정적 상수는 아니되, durable·전이 기록은 불요 — 콘솔이 set, 답 생성이 read). 영속이 요구되면 `SessionStore`처럼 store로 승격(후속).
- **`hitl_to_mode` 순수 함수 + `_apply_approval_gate` 확장** — 매핑은 순수 함수로 격리(결정론). `_apply_approval_gate`가 `requires_approval` OR HITL 토글을 보고 draft_only를 강제(OR 결합·backup 우선순위 보존).
- **콘솔 토글 명령(T9.2 (b))** — POST 명령이 토글 상태를 바꾸고, 이후 답이 그 mode로 나간다(결정론 테스트: 토글 변경→다음 답 mode 반영).
- **불변식 영향 없음**:
  - **노출 불변식** — `mode`는 *원래 노출하는 신뢰 상태값*이다(CONTEXT Answer 절 — 사용자向 `Answered`에 그대로 실림, 조직 내부 구조 아님). HITL 토글이 그 값을 바꿔도 노출 경계는 그대로(draft_only/full은 둘 다 노출 OK).
  - **Authority 중앙** — 토글은 *신뢰 게이트*지 *권한 선언*이 아니다. 누가 담당인지(routing_rules.yaml)·누가 owner인지(card.owner)를 안 건드린다.
  - **전이 ≠ 기록** — 토글 변경은 운영 설정 set이지 도메인 전이가 아니다. audit·트랜스크립트 무관.
  - **미아 없음** — 토글은 답의 *신뢰 표시*만 바꾸지 라우팅 종착을 안 바꾼다.
- **갱신 대상**: CONTEXT(Approval·Answer 절에 HITL 런타임 토글 추가)·PRD §3·§5·TRD §4·§6(Approval→mode 강제 절에 토글 합류).

## 결정 대기

이 ADR은 **외부 결정 0**이다(tasks line 261 — 기존 draft_only/Approval 재사용이라 외부 결정 없음·권장 조기 진입). 6개 결정 대기 항목 중 ADR-B에 걸리는 것은 없다. 토글 상태의 영속(in-memory vs store 승격)은 *요구가 관측될 때* 당기는 후속 판단이지 게이트 진입 전 확정 사항이 아니다.
