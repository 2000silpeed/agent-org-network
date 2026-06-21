"""Manager 큐 — escalation 수렴 보관함 + Manager 처리 행위 (T5.2, ADR 0014 예정).

그동안 세 군데가 "T5.2 자리만"으로 미뤄졌다. 모두 *담당/답을 사람(Manager)이
정해야 하는 미해소 escalation*이다 — 이제 한 큐로 수렴한다.

  - `DispatchOutcome.EscalatedToManager`(dispatch.py·ADR 0011) — owner 부재/timeout.
  - `ConsensusOutcome.Deadlocked`(conflict.py·ADR 0008) — 후보 합의 교착.
  - `RoutingDecision.Unowned`(decision.py) — 미아(후보 0).

세 출처는 **하나의 `ManagerItem`로 통합**하되, 출처는 sealed sum `EscalationSource`로
갈라 망라성을 강제한다(ConflictCase/BackupReviewItem이 *별 store*인 것과 *다른* 판단 —
근거는 아래 "통합 vs 출처별").

패턴 원본: `ConflictCaseStore`(conflict.py)·`BackupReviewStore`(review.py)의
Protocol + InMemory + owner 색인 패턴을 *세 번째 인스턴스*로 재사용한다 — 단 색인 키가
`owner`가 아니라 **`manager_id`**(escalation은 owner가 아니라 *그 위 사람*에게 귀속).

전이 ≠ 기록:
  - `ManagerQueueStore`(이 모듈) — 미해소 escalation의 도메인 보관소(전이).
  - `AuditLog`(audit.py) — 절차 기록(기록). escalation은 이미 audit에 남는다
    (`AuditEntry.decision`=Unowned / `dispatch_outcome`=EscalatedToManager·ADR 0011
    결정 5). 큐 적재는 그 기록과 *별개*의 전이 보관이다.

미아 없음 종착(불변식 완성):
  세 출처는 지금껏 "처분 상태"만 남기고 *사람 손에 닿지 않았다*. Manager 큐 적재가
  그 마지막 한 칸 — escalation은 반드시 큐에 쌓여 Manager를 기다린다(영구 소실 0).
  Manager가 처리(`ManagerAction`)하면 resolved로 전이하고 처리함에서 빠진다.

──────────────────────────────────────────────────────────────────────────────
통합 vs 출처별 (왜 한 `ManagerItem`인가)

ConflictCase·BackupReviewItem은 별 store다 — *Owner* 처리함의 서로 다른 두 탭이고
담는 값(다툼·후보 vs 백업 답)이 근본적으로 다르다(CONTEXT BackupReviewStore _Avoid).
Manager 큐는 다르다:
  - 세 출처는 모두 "*사람이 담당/답을 정해야 하는 한 escalation*"이라는 **한 종류**다.
    Manager 화면에서 한 큐로(PRD §4 "Manager 큐: 승인·escalation·합의 실패").
  - 셋이 별 store면 Manager 화면이 세 store를 합쳐 보여야 하고 `pending_for_manager`가
    셋으로 쪼개진다 — owner 처리함이 두 탭으로 갈린 것과 *반대 방향*(거긴 정말 다른 일,
    여긴 같은 일의 다른 출처).
  - 그래서 보관 단위는 하나(`ManagerItem`)로 통합하고, *출처의 차이*만 sealed sum
    `EscalationSource`로 안에 담아 망라성을 강제한다(타입이 곧 상태 정신 — 처리 시
    `match`로 출처별 행위를 가른다).
──────────────────────────────────────────────────────────────────────────────

이 모듈은 **shape(포트 + 값 객체 + InMemory stub + 적재/처리 시그니처)만** 둔다 —
실제 red→green(적재 흐름·처리 행위 본동작)은 tdd-engineer가 채운다.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol

from agent_org_network.conflict import (
    ConflictCase,
    ConflictCaseStore,
    PrecedentStore,
    Resolution,
)
from agent_org_network.decision import Unowned
from agent_org_network.dispatch import EscalatedToManager

Clock = Callable[[], datetime]


def default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_item_id() -> str:
    return uuid.uuid4().hex


# ── 수렴 소스: EscalationSource sealed sum ─────────────────────────────────
#
# escalation이 어디서 왔나 — 세 출처의 망라(RoutingDecision·ConsensusOutcome·
# DispatchOutcome 정신, "타입이 곧 상태"). Manager는 출처에 따라 *처리 행위가 다르므로*
# (미아→담당 지정, 합의 교착→중재, owner 부재→재라우팅) 출처를 1급으로 갈라 둔다.
#
# 각 출처는 *원형 처분을 그대로 안는다*(audit이 decision·dispatch_outcome 원형을 안는
# 정신). 그래야 Manager가 큐에서 "무엇을 두고 escalation됐나"의 전체 맥락을 본다 —
# 후보 목록(Deadlocked)·owner/agent_id(EscalatedToManager)·root(Unowned)까지.


@dataclass(frozen=True)
class FromUnowned:
    """미아(후보 0) → Manager 큐. `RoutingDecision.Unowned`의 수렴.

    `decision`(Unowned 원형 — escalated_to=root·reason)을 안는다. Unowned엔 question이
    없으므로(decision.py 참조) `question`을 함께 든다(적재 시 ask_org가 원문을 넘김).
    Manager 처리: 기존 Owner 지정 또는 신규 카드 생성 → Resolution(Gap 해소, CONTEXT
    Conflict.Gap). manager_id는 root(미아는 사람 위계 꼭대기로, decision.escalated_to).
    """

    decision: Unowned
    question: str


@dataclass(frozen=True)
class FromDeadlock:
    """후보 합의 교착 → Manager 큐. `ConsensusOutcome.Deadlocked`의 수렴.

    `case`(ConflictCase 원형 — intent·question·candidates·case_id)를 안는다. 케이스가
    이미 question·후보를 들고 case_id로 추적되므로 그대로 재사용(Deadlocked.case).
    `reason`(표 갈림 근거)도 보존(사람용). Manager 처리: 중재로 primary 지정 →
    Resolution → 그 ConflictCase resolved(Overlap 합의 실패의 사람 종결, CONTEXT
    Conflict.Overlap "합의 실패 시 Manager로"). manager_id는 후보 Owner들의 manager.
    """

    case: ConflictCase
    reason: str = ""


@dataclass(frozen=True)
class FromDispatch:
    """owner 부재/timeout → Manager 큐. `DispatchOutcome.EscalatedToManager`의 수렴.

    `outcome`(EscalatedToManager 원형 — ticket·manager_id·reason)을 안는다. ticket이
    owner_id·agent_id·question을 들어 Manager가 "누가 무엇에 답 못 했나"를 본다.
    manager_id는 outcome.manager_id(이미 owner의 manages 상위로 채워짐 — ADR 0011 결정 4,
    `_make_escalated`). Manager 처리: 재라우팅(다른 카드로)·직접 답 위임·대기 등 운영
    판단(Transfer 또는 사람 답). 담당은 정해져 있었으나 *답이 안 나온* escalation이라
    미아(FromUnowned)·합의 실패(FromDeadlock)와 결이 다르다(담당 부재가 아니라 가용성).
    """

    outcome: EscalatedToManager


EscalationSource = FromUnowned | FromDeadlock | FromDispatch
#   escalation 출처의 sealed sum(세 출처의 망라). 새 출처가 생기면 여기 더하고
#   처리/적재의 match가 컴파일 타임에 누락을 잡는다(assert_never).


# ── 보관 단위: ManagerItem ────────────────────────────────────────────────
#
# Manager 큐에 쌓인 한 escalation. ConflictCase가 미해소 다툼을, BackupReviewItem이
# 미검토 백업 답을 담듯, 이건 *미해소 escalation*을 담는다 — Manager 처리함의 한 항목.
# open → resolved 전이는 `resolve()`가 새 인스턴스를 돌려준다(불변 + 새 인스턴스,
# ConflictCase.resolve()·BackupReviewItem.review_with()와 같은 정신).

ManagerItemStatus = Literal["open", "resolved"]


@dataclass(frozen=True)
class ManagerItem:
    """Manager 큐에 쌓인 한 escalation 항목(미해소 → resolved).

    `manager_id`(귀속 키 — 어느 Manager 큐의 항목인가, pending_for_manager 색인) +
    `source`(EscalationSource — 출처 원형) + 생성 시각(주입 clock 결정론) +
    status/resolution(resolved일 때만).

    manager_id 결정(적재자 책임 — 아래 `manager_id_for_*` 참조):
      - FromUnowned:   root(decision.escalated_to — 미아는 사람 위계 꼭대기).
      - FromDeadlock:  후보 Owner들의 manager(없으면/엇갈리면 root — LCA는 범위 밖,
                       단일 Manager 수렴이므로 첫 후보 owner의 manager 또는 root).
      - FromDispatch:  outcome.manager_id(이미 채워짐; None이면 root로 보정).
    manager_id가 끝내 None이 되지 않게 적재자가 root로 보정한다(미아 없음 — escalation은
    반드시 *누군가의* 큐에 닿는다. 루트 User가 마지막 수신자).

    노출 불변식: ManagerItem은 *운영 면*(Manager 큐) 전용이라 내부값(후보·manager_id·
    reason·owner)을 그대로 노출한다 — ConflictCase가 처리함에 후보를 노출하듯(채팅
    OrgReply 불변식과 다른 면). 사용자 채팅엔 여전히 Pending 안내만 간다.
    """

    manager_id: str
    source: EscalationSource
    created_at: datetime
    item_id: str = field(default_factory=_new_item_id)
    status: ManagerItemStatus = "open"
    resolution: "ManagerResolution | None" = None

    def question(self) -> str:
        """출처에서 원 질문을 꺼낸다(Manager 화면 표시·맥락용).

        세 출처가 question을 *다른 자리*에 든다 — FromUnowned는 자체 필드, FromDeadlock은
        case.question, FromDispatch는 outcome.ticket.question. 출처별 접근을 여기 한 곳에
        모아 호출처가 match를 반복하지 않게 한다(shape — tdd가 본동작 채움).
        """
        from typing import assert_never

        match self.source:
            case FromUnowned():
                return self.source.question
            case FromDeadlock():
                return self.source.case.question
            case FromDispatch():
                return self.source.outcome.ticket.question
            case _ as never:
                assert_never(never)

    def resolve(self, resolution: "ManagerResolution") -> "ManagerItem":
        """처리 결론을 안은 resolved 항목을 새로 만든다(item_id·source 보존).

        ConflictCase.resolve()·BackupReviewItem.review_with()와 같은 전이(파괴적 변경 X).
        """
        return ManagerItem(
            manager_id=self.manager_id,
            source=self.source,
            created_at=self.created_at,
            item_id=self.item_id,
            status="resolved",
            resolution=resolution,
        )


# ── Manager 처리 행위: ManagerAction sealed sum ───────────────────────────
#
# Manager가 한 항목에 내리는 처분 — sealed sum("타입이 곧 상태", ConcurOnPrimary·
# BackupReview 정신, 1인칭 by_manager). 처리 서비스가 item.manager_id == by_manager를
# 강제(ConsensusService가 후보 owner를, BackupReviewService가 owner_id를 강제하듯).
#
# 출처별로 *유효한* 행위가 다르다(미아는 Assign, 교착은 Resolve, 부재는 Reroute가
# 자연스럽다) — 단 MVP는 행위 타입으로 망라하고 출처-행위 적합성 강제는 후속(자리만).


@dataclass(frozen=True)
class AssignOwner:
    """Manager가 담당을 지정한다 — 미아(FromUnowned)·교착(FromDeadlock)의 사람 종결.

    `primary`(지정한 카드 agent_id). intent가 있으면(Unowned/Deadlock 모두 분류 라벨
    보유) Resolution(intent→primary)으로 떨어져 **Precedent로 학습**된다(CONTEXT
    Conflict "두 경로 모두 결론은 Resolution → Precedent"). 이게 Gap/Overlap의 사람
    해소가 라우터 학습으로 닫히는 지점 — ConsensusOutcome.Agreed가 표로 닫는 것과 대칭.
    """

    by_manager: str
    primary: str
    rationale: str = ""


@dataclass(frozen=True)
class Reroute:
    """Manager가 다른 카드로 재지정한다 — owner 부재(FromDispatch)의 운영 판단.

    `to_agent`(재라우팅 대상 카드 agent_id). 담당은 있었으나 답이 안 나온 case라
    *Transfer*(이관, CONTEXT — 배정된 primary를 사후에 바꿈)에 해당. Precedent를 만들지
    *않는다*(일회 가용성 사건이지 담당 규칙 변경이 아니다 — BackupReview가 Precedent를
    안 만드는 정신과 같다). 단순 재시도와 구분: 사람이 명시적으로 다른 담당을 지목.
    """

    by_manager: str
    to_agent: str
    rationale: str = ""


@dataclass(frozen=True)
class Dismiss:
    """Manager가 종결한다 — "확인했고 추가 조치 안 함"(처리 완료 사실은 남김).

    재라우팅/담당 지정 없이 큐에서 내린다(예: 중복·무효 질문, 이미 다른 경로로 해소).
    BackupReview.Dismiss와 같은 정신 — 미해소와 구분되는 "검토 완료" 종결. Precedent X.
    """

    by_manager: str
    rationale: str = ""


ManagerAction = AssignOwner | Reroute | Dismiss
#   Manager 처분의 sealed sum(세 행위의 망라). 처리 서비스가 match로 출처/행위를 본다.


# ── 처리 결론: ManagerResolution ──────────────────────────────────────────
#
# Manager가 한 항목을 처리한 결론(ManagerItem.resolution에 박힘). conflict.Resolution
# (intent→primary, 합의 결론)과 *구분*한다 — ManagerResolution은 "이 escalation을
# 사람이 이렇게 종결했다"는 큐 항목의 결말이고, 그 안에 (AssignOwner면) conflict의
# Resolution을 *담을 수 있다*(Precedent로 흐른 결론). Reroute/Dismiss는 conflict
# Resolution이 없다(라우팅 판례를 만들지 않으므로).


@dataclass(frozen=True)
class ManagerResolution:
    """한 ManagerItem의 처리 결말 — 어떤 행위로 종결됐나 + (있으면) 라우팅 Resolution.

    `action`(Manager가 내린 ManagerAction 원형 — 1인칭 by_manager 보존) + `resolution`
    (AssignOwner가 Precedent로 흘린 conflict.Resolution; Reroute/Dismiss면 None).
    "타입이 곧 상태" — resolved ManagerItem이 이걸 안으면 그 항목은 사람 손에서 닫혔다.
    """

    action: ManagerAction
    resolution: Resolution | None = None


# ── manager_id 결정 (적재자 책임 — 사람 그래프 상향) ──────────────────────
#
# 어느 Manager 큐에 적재할지. 사람 그래프(owner→manager, ADR 0005 `manages`)를 타고
# 오른다. *디스패처가 manager_of 콜백을 주입받듯*(dispatch.py), 큐 적재자도 그래프
# 조회를 주입받는다 — Registry.get_user(owner).manager가 출처. 결합을 피하려 콜백
# 시그니처로 둔다(Registry 직접 의존 회피, dispatch.py `_manager_of` 정신).
#
# 단일 Manager 수렴(과도 확장 금지): 멀티홉(manager의 manager까지 등반)·LCA(여러 후보
# owner의 공통 상위)는 PRD §6 후순위로 명시. 여기선 *한 단계 위 + root 폴백*만.

ManagerOf = Callable[[str], str | None]
#   owner_id(User.id) → 그 User의 manager(User.id) | None(루트). Registry.get_user로 구현.


def manager_id_for_unowned(decision: Unowned) -> str:
    """미아의 manager_id = root(decision.escalated_to). 미아는 곧장 꼭대기로.

    Router가 Unowned에 이미 root(escalated_to)를 박았으므로(router.py·decision.py) 그대로
    쓴다 — 미아엔 owner가 없어 사람 그래프를 탈 시작점이 없다(꼭대기가 마지막 수신자).
    """
    return decision.escalated_to


def manager_id_for_deadlock(case: ConflictCase, manager_of: ManagerOf, root: str) -> str:
    """교착의 manager_id = 후보 Owner들의 manager(엇갈리면/없으면 root).

    단일 Manager 수렴 — LCA(공통 상위)는 범위 밖(PRD §6). MVP는 *첫 후보 owner의 manager*
    를 쓰고, 없거나(루트 owner) manager_of가 None을 주면 root로 폴백한다(미아 없음 — 반드시
    누군가의 큐). 후보들의 manager가 서로 다른 복합 케이스의 LCA 등반은 후속.
    """
    if not case.candidates:
        return root
    first_owner = case.candidates[0].owner
    mgr = manager_of(first_owner)
    return mgr if mgr is not None else root


def manager_id_for_dispatch(outcome: EscalatedToManager, root: str) -> str:
    """owner 부재의 manager_id = outcome.manager_id(None이면 root 보정).

    디스패처가 이미 owner의 manages 상위를 채웠다(ADR 0011 결정 4, `_make_escalated`).
    None(루트 owner의 부재)이면 root로 보정 — escalation은 반드시 누군가의 큐에 닿는다.
    """
    return outcome.manager_id if outcome.manager_id is not None else root


# ── 보관 포트: ManagerQueueStore ──────────────────────────────────────────
#
# ConflictCaseStore·BackupReviewStore와 *같은 패턴*(Protocol + InMemory, 색인 조회).
# 차이는 색인 키가 owner가 아니라 **manager_id** — escalation은 owner가 아니라 그 위
# 사람에게 귀속한다. `pending_for_manager` = open_for_owner/pending_for_owner 동형 =
# Manager 처리함의 데이터 원천. 전이 ≠ 기록 — 미해소 escalation 도메인 상태 보관이지
# 절차 기록(AuditLog)이 아니다(escalation은 audit에 이미 남는다 — ADR 0011 결정 5).


class ManagerQueueStore(Protocol):
    """미해소 escalation(ManagerItem) 보관·조회 포트 — Manager 처리함의 데이터 원천.

    `ConflictCaseStore`·`BackupReviewStore`·`AuditLog`·`PrecedentStore`와 같은 포트 패턴
    (Protocol + InMemory). `pending_for_manager`가 Manager 화면의 "내 큐에 쌓인
    escalation들" 조회(open_for_owner 동형). `mark_resolved`가 open→resolved 전이.
    `get_by_case`는 같은 ConflictCase의 중복 적재 방지(open_for_intent 정신)에 쓴다 —
    Deadlocked가 같은 case로 두 번 들어오지 않게.
    """

    def enqueue(self, item: ManagerItem) -> None: ...

    def get(self, item_id: str) -> ManagerItem | None: ...

    def pending_for_manager(self, manager_id: str) -> list[ManagerItem]: ...

    def get_by_case(self, case_id: str) -> ManagerItem | None: ...

    def mark_resolved(self, item: ManagerItem) -> None: ...


class InMemoryManagerQueueStore:
    """append-only 정신의 in-memory Manager 큐 저장소.

    open 항목은 `_open`(item_id 색인)에 둔다. resolved되면 `_open`에서 빼 `history`
    (append-only)에 결말을 남긴다 — 처리함 목록은 open만, 이력은 전부(ConflictCase·
    BackupReview store와 같은 구조). `_by_case`(case_id 색인)는 Deadlocked 중복 적재
    방지(같은 case가 두 번 큐에 들지 않게 — 같은 다툼이 질문마다 항목을 양산하지 않게,
    ConflictCaseStore.open_for_intent 정신).

    구현은 후속 — 여기선 시그니처와 상태 그릇만 둔다(stub).
    """

    def __init__(self) -> None:
        self._open: dict[str, ManagerItem] = {}
        self._by_case: dict[str, ManagerItem] = {}  # case_id → open item(FromDeadlock 한정)
        self.history: list[ManagerItem] = []

    def enqueue(self, item: ManagerItem) -> None:
        self._open[item.item_id] = item
        self.history.append(item)
        # FromDeadlock이면 case_id 색인에 등록(중복 적재 방지용)
        if isinstance(item.source, FromDeadlock):
            self._by_case[item.source.case.case_id] = item

    def get(self, item_id: str) -> ManagerItem | None:
        # open 또는 history에서 최신 상태 반환
        if item_id in self._open:
            return self._open[item_id]
        for h in reversed(self.history):
            if h.item_id == item_id:
                return h
        return None

    def pending_for_manager(self, manager_id: str) -> list[ManagerItem]:
        return [item for item in self._open.values() if item.manager_id == manager_id]

    def get_by_case(self, case_id: str) -> ManagerItem | None:
        return self._by_case.get(case_id)

    def mark_resolved(self, item: ManagerItem) -> None:
        self._open.pop(item.item_id, None)
        self.history.append(item)
        # case 색인도 정리(resolved됐으므로 중복 방지 해제)
        if isinstance(item.source, FromDeadlock):
            self._by_case.pop(item.source.case.case_id, None)


# ── 처리 서비스: ManagerQueueService ──────────────────────────────────────
#
# Manager 처분(ManagerAction)을 받아 ManagerItem을 전이시키는 도메인 서비스.
# ConsensusService·BackupReviewService와 같은 정신:
#   - 1인칭 강제: item.manager_id == action.by_manager (타인 큐 처리 금지).
#   - AssignOwner는 intent가 있으면 Resolution→Precedent로 흘린다(Gap/Overlap 사람
#     해소의 학습 — PrecedentStore 주입). Reroute/Dismiss는 Precedent 안 만듦.
#   - FromDeadlock을 AssignOwner로 닫으면 *그 ConflictCase도* resolved시킨다(case_store
#     주입 — 합의 실패의 사람 종결이 케이스를 닫는다, ConsensusService.Agreed가 닫듯).
#   - 전이만 — 처리 *기록*은 audit이 별개로(전이 ≠ 기록). 단 escalation 적재 자체는
#     이미 ADR 0011 결정 5로 audit에 남으므로 여기선 큐 전이만 책임진다.
#
# 처리 행위 범위(T5.2 어디까지): 자리 + 기본 처리(세 행위 전이·1인칭·Precedent 흘림·
# case 종결)까지. 출처-행위 적합성 강제(미아에 Reroute 거부 등)·멀티홉·자동 통지는 후속.


class ManagerQueueService:
    """Manager 처분을 받아 ManagerItem 상태를 전이시키는 도메인 서비스.

    1인칭 강제: action.by_manager가 item.manager_id여야 한다(ValueError — 자기 큐만 처리,
    ConsensusService가 후보 owner를, BackupReviewService가 owner_id를 강제하듯).
    AssignOwner+intent → Resolution을 PrecedentStore에 record(라우터 학습). FromDeadlock을
    AssignOwner로 닫으면 그 ConflictCase도 mark_resolved(case_store). 멱등: 이미 resolved면
    그대로 반환.

    구현은 후속 — 여기선 시그니처만 둔다(stub).
    """

    def __init__(
        self,
        queue_store: ManagerQueueStore,
        precedents: PrecedentStore | None = None,
        case_store: ConflictCaseStore | None = None,
    ) -> None:
        self._queue_store = queue_store
        # AssignOwner 결론을 라우터 학습으로 흘린다(Gap/Overlap 사람 해소 → Precedent).
        # 미주입이면 학습 없이 전이만(하위호환 — 큐 닫기는 되되 판례는 안 남음).
        self._precedents = precedents
        # FromDeadlock을 닫을 때 그 ConflictCase도 resolved시킨다(합의 실패의 사람 종결).
        # 미주입이면 큐 항목만 닫고 케이스는 그대로(하위호환).
        self._case_store = case_store

    def act(self, item_id: str, action: ManagerAction) -> ManagerItem:
        """처분을 적용해 resolved ManagerItem을 돌려준다.

        1인칭 검증(by_manager == item.manager_id), item_id 미존재 → ValueError,
        이미 resolved면 멱등 반환. AssignOwner는 (intent 있으면) Precedent record +
        (FromDeadlock이면) ConflictCase resolved까지. match+assert_never로 행위 망라.
        """
        from typing import assert_never

        item = self._queue_store.get(item_id)
        if item is None:
            raise ValueError(f"미존재 Manager 큐 항목: {item_id!r}")

        # 1인칭 강제
        if action.by_manager != item.manager_id:
            raise ValueError(
                f"1인칭 위반: by_manager({action.by_manager!r})가 "
                f"item.manager_id({item.manager_id!r})와 다름 — 자기 큐만 처리할 수 있다"
            )

        # 멱등: 이미 resolved면 그대로
        if item.status == "resolved":
            return item

        # 행위별 처리
        conflict_resolution: Resolution | None = None

        match action:
            case AssignOwner():
                # intent가 있는 출처(FromDeadlock)에서 Precedent 기록
                intent: str | None = None
                match item.source:
                    case FromDeadlock():
                        intent = item.source.case.intent
                    case FromUnowned():
                        # Unowned엔 intent 없음(decision.py Unowned 필드 없음)
                        intent = None
                    case FromDispatch():
                        intent = None
                    case _ as never:
                        assert_never(never)

                if intent and self._precedents is not None:
                    conflict_resolution = Resolution(
                        intent=intent,
                        primary=action.primary,
                        rationale=action.rationale,
                    )
                    self._precedents.record(conflict_resolution)

                # FromDeadlock이면 ConflictCase도 resolved
                if isinstance(item.source, FromDeadlock) and self._case_store is not None:
                    case = item.source.case
                    existing = self._case_store.get(case.case_id)
                    if existing is not None:
                        if conflict_resolution is None:
                            # intent 없는 경우에도 case는 닫아야 함
                            conflict_resolution = Resolution(
                                intent=case.intent,
                                primary=action.primary,
                                rationale=action.rationale,
                            )
                        resolved_case = existing.resolve(conflict_resolution)
                        self._case_store.mark_resolved(resolved_case)

            case Reroute():
                pass  # Precedent 안 만듦

            case Dismiss():
                pass  # Precedent 안 만듦

            case _ as never:
                assert_never(never)

        mgr_resolution = ManagerResolution(action=action, resolution=conflict_resolution)
        resolved_item = item.resolve(mgr_resolution)
        self._queue_store.mark_resolved(resolved_item)
        return resolved_item
