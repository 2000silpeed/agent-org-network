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

이 모듈은 포트·값 객체와 InMemory 구현, 적재·처리 서비스를 함께 둔다. InMemory 구현은
프로세스 안의 결정론 테스트와 단일 프로세스 실행용이며, 재시작 내구성은 영속 adapter가 맡는다.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from agent_org_network.conflict import (
    ConflictCase,
    ConflictCaseStore,
    ConflictEscalationCause,
    DivergentVotes,
    PrecedentStore,
    Resolution,
)
from agent_org_network.decision import Unowned
from agent_org_network.dispatch import EscalatedToManager

if TYPE_CHECKING:
    from agent_org_network.p17_manager_disposition import (
        ClaimAttempt,
        DeadlockManagerClaimAttempt,
        DeadlockManagerDispositionClaim,
        DeadlockManagerDispositionCommand,
        DeadlockManagerReservationControlToken,
        DeadlockManagerSealedClaimAvailable,
        DeadlockManagerSealedClaimHandle,
        ManagerDispositionClaim,
        P17ManagerDispositionCommand,
        ReservationControlToken,
        ReservedAssignOwnerClaim,
        ReservedDismissClaim,
        ReservedDeadlockAssignClaim,
        ReservedDeadlockDismissClaim,
        ResumeEvidence,
        SealedClaimAvailable,
        SealedClaimHandle,
        SealedDeadlockAssignClaim,
        SealedDeadlockDismissClaim,
    )
    from agent_org_network.request_route_authority import RequestRouteGrantRejected

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
    cause: ConflictEscalationCause | None = None


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


def _validate_manager_source_request_id(
    source: EscalationSource,
    request_id: str | None,
) -> None:
    """correlated ManagerItem과 중첩 Case/Ticket이 같은 Request를 가리키는지 검증."""
    if isinstance(source, FromDeadlock):
        if source.case.request_id != request_id:
            raise ValueError(
                "FromDeadlock ConflictCase.request_id는 ManagerItem.request_id와 같아야 합니다."
            )
        if request_id is None:
            if source.cause is not None:
                raise ValueError("legacy FromDeadlock에는 escalation cause를 둘 수 없습니다.")
            return
        cause = source.cause
        if cause is None:
            raise ValueError("request-aware FromDeadlock에는 escalation cause가 필요합니다.")
        case = source.case
        if (
            case.status != "open"
            or case.resolution is not None
            or case.manager_item_id is not None
            or case.decline_reason is not None
        ):
            raise ValueError("request-aware FromDeadlock은 escalation 직전 open Case가 필요합니다.")
        if cause.round != case.concurrence_round:
            raise ValueError("FromDeadlock cause round가 Case concurrence round와 다릅니다.")
        expected_reason = deadlock_reason(cause)
        if source.reason != expected_reason:
            raise ValueError("request-aware FromDeadlock reason이 cause 투영과 다릅니다.")
    elif isinstance(source, FromDispatch):
        if source.outcome.ticket.request_id != request_id:
            raise ValueError(
                "FromDispatch WorkTicket.request_id는 ManagerItem.request_id와 같아야 합니다."
            )


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
    request_id: str | None = None

    def __post_init__(self) -> None:
        from agent_org_network.request_correlation import validate_optional_request_id

        correlated_request_id = validate_optional_request_id(self.request_id)
        _validate_manager_source_request_id(self.source, correlated_request_id)

    @classmethod
    def for_request(
        cls,
        *,
        request_id: str,
        manager_id: str,
        source: EscalationSource,
        created_at: datetime,
        item_id: str | None = None,
    ) -> "ManagerItem":
        """Request-first 경로용 open Item 생성 관문과 중첩 상관키 검증."""
        from agent_org_network.request_correlation import require_request_id

        correlated_request_id = require_request_id(request_id)
        _validate_manager_source_request_id(source, correlated_request_id)
        if item_id is None:
            return cls(
                manager_id=manager_id,
                source=source,
                created_at=created_at,
                request_id=correlated_request_id,
            )
        return cls(
            manager_id=manager_id,
            source=source,
            created_at=created_at,
            item_id=item_id,
            request_id=correlated_request_id,
        )

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
            request_id=self.request_id,
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

    이 공개 5메서드만 구현한 custom store는 순차 하위호환 대상이다. 처분 선점과 여러
    외부 side effect의 부분 성공 재시도를 원자적으로 보장하려면 아래 선택 seam까지
    구현하거나, 영속 adapter에서 transaction/outbox에 해당하는 동등한 보장을 제공해야
    한다. 5메서드 폴백 자체는 그 보장을 가장하지 않는다.
    """

    def enqueue(self, item: ManagerItem) -> None: ...

    def get(self, item_id: str) -> ManagerItem | None: ...

    def pending_for_manager(self, manager_id: str) -> list[ManagerItem]: ...

    def get_by_case(self, case_id: str) -> ManagerItem | None: ...

    def mark_resolved(self, item: ManagerItem) -> None: ...


class RequestAwareManagerQueueStore(Protocol):
    """Request-first 경로가 쓰는 요청별 원자 생성·조회 보조 포트.

    기존 ``ManagerQueueStore`` 공개 5메서드와 custom store 호환성은 그대로 둔다.
    ``get_by_request``는 open/resolved 여부와 무관하게 마지막 저장본을 반환한다.
    """

    def create_or_get_for_request(
        self,
        item: ManagerItem,
    ) -> tuple[ManagerItem, bool]: ...

    def get_by_request(self, request_id: str) -> ManagerItem | None: ...


def deadlock_reason(cause: ConflictEscalationCause) -> str:
    if isinstance(cause, DivergentVotes):
        return "divergent_votes"
    return f"candidate_registry_changed:{cause.reason_code}"


def _manager_request_fingerprint(item: ManagerItem) -> tuple[object, ...]:
    """Request-aware Item의 ID·시각·상태 제외 semantic payload."""
    from agent_org_network.request_correlation import require_request_id

    request_id = require_request_id(item.request_id)
    if isinstance(item.source, FromUnowned):
        return request_id, item.manager_id, item.source
    if isinstance(item.source, FromDeadlock):
        case = item.source.case
        cause = item.source.cause
        if cause is None:
            raise ValueError("request-aware FromDeadlock에는 escalation cause가 필요합니다.")
        return (
            request_id,
            item.manager_id,
            "from_deadlock",
            case.request_id,
            case.case_id,
            case.intent,
            case.question,
            case.candidates,
            case.concurrence_round,
            cause,
        )
    raise ValueError("request-aware FromDispatch ManagerItem은 지원하지 않습니다.")


_ManagerEffectKey = Literal["precedent", "conflict_case"]
_RunManagerEffectOnce = Callable[[_ManagerEffectKey, Callable[[], None]], None]


@dataclass
class _ManagerActionProgress:
    """한 open 항목에서 선점된 처분과 성공한 외부 효과의 프로세스 내 ledger."""

    action: ManagerAction
    completed_effects: set[_ManagerEffectKey] = field(
        default_factory=lambda: set[_ManagerEffectKey]()
    )


@runtime_checkable
class _AtomicManagerQueueStore(Protocol):
    """ManagerQueueStore 공개 포트를 깨지 않는 선택적 원자 전이 seam.

    InMemory처럼 한 프로세스 안에서 임계구역을 제공할 수 있는 구현만 만족한다.
    기존 외부 구현은 이 메서드 없이도 기존 ManagerQueueStore 계약을 계속 만족하며,
    ManagerQueueService는 그 경우 비트랜잭션 get→mark_resolved 경로로 폴백한다.
    """

    def resolve_if_open(
        self,
        item_id: str,
        action: ManagerAction,
        transition: Callable[[ManagerItem, _RunManagerEffectOnce], ManagerItem],
    ) -> ManagerItem | None: ...


class InMemoryManagerQueueStore:
    """append-only 정신의 in-memory Manager 큐 저장소.

    open 항목은 `_open`(item_id 색인)에 둔다. resolved되면 `_open`에서 빼 `history`
    (append-only)에 결말을 남긴다 — 처리함 목록은 open만, 이력은 전부(ConflictCase·
    BackupReview store와 같은 구조). `_by_case`(case_id 색인)는 Deadlocked 중복 적재
    방지(같은 case가 두 번 큐에 들지 않게 — 같은 다툼이 질문마다 항목을 양산하지 않게,
    ConflictCaseStore.open_for_intent 정신).

    모든 공개 조회·전이는 하나의 RLock으로 보호한다. 선택적 원자 seam은 동시 처분과
    프로세스 내 부분 성공 재시도를 닫지만, 재시작 내구성을 제공하지는 않는다.
    """

    workflow_durability: Literal["ephemeral", "durable"] = "ephemeral"

    def __init__(self) -> None:
        self._open: dict[str, ManagerItem] = {}
        self._by_case: dict[str, ManagerItem] = {}  # case_id → open item(FromDeadlock 한정)
        self._by_request: dict[str, ManagerItem] = {}
        self._history: list[ManagerItem] = []
        # callback이 일부 외부 효과까지만 성공한 뒤 실패해도 같은 처분의 재시도가 이미
        # 성공한 효과를 반복하지 않게 한다. 다른 처분은 open 항목을 가로채지 못한다.
        # 프로세스 메모리 ledger이므로 재시작 내구성은 영속 adapter/UoW·outbox의 책임이다.
        self._action_progress: dict[str, _ManagerActionProgress] = {}
        # P17.4 request-aware 처분 winner. resolved 뒤에도 남겨 Request CAS·Authority
        # 부분 성공 재시도의 증거로 쓴다. legacy progress ledger와 수명이 다르다.
        self._disposition_claims: dict[
            str, ManagerDispositionClaim | DeadlockManagerDispositionClaim
        ] = {}
        self._disposition_control_tokens: dict[
            str, ReservationControlToken | DeadlockManagerReservationControlToken
        ] = {}
        self._disposition_forward_handles: dict[
            str, SealedClaimHandle | DeadlockManagerSealedClaimHandle
        ] = {}
        self._disposition_resume_evidence: dict[str, ResumeEvidence] = {}
        self._disposition_generations: set[str] = set()
        self._disposition_validation: set[str] = set()
        self._disposition_reentered: set[str] = set()
        # FastAPI 동기 라우트는 스레드풀 병렬 실행된다. 조회 snapshot과 open→resolved
        # 전이 전체를 같은 RLock으로 직렬화한다(resolve_if_open callback도 lock 안에서 실행).
        self._lock = RLock()

    @property
    def history(self) -> list[ManagerItem]:
        """외부 mutation이 backing workflow evidence를 바꾸지 않는 deep snapshot."""
        with self._lock:
            return deepcopy(self._history)

    def enqueue(self, item: ManagerItem) -> None:
        item = deepcopy(item)
        if item.request_id is not None:
            raise ValueError(
                "request-aware ManagerItem은 create_or_get_for_request로만 생성할 수 있습니다."
            )
        with self._lock:
            existing = self._open.get(item.item_id)
            if existing is None:
                existing = next(
                    (
                        stored
                        for stored in reversed(self._history)
                        if stored.item_id == item.item_id
                    ),
                    None,
                )
            if existing is not None:
                if existing.request_id is not None or existing != item:
                    raise ValueError("ManagerItem.item_id가 기존 항목과 충돌합니다.")
                return
            # 공개 포트를 직접 쓰는 기존 호출자도 같은 ConflictCase의 open 항목을
            # 양산하지 않게 한다. 반환 계약(None)은 그대로이고, 삽입 여부가 필요한
            # AskOrg만 아래 원자 seam을 사용한다.
            if isinstance(item.source, FromDeadlock) and item.source.case.case_id in self._by_case:
                return
            self._enqueue_unlocked(item)

    def enqueue_deadlock_if_absent(
        self,
        item: ManagerItem,
    ) -> tuple[ManagerItem, bool]:
        """같은 case_id의 open FromDeadlock을 CAS처럼 정확히 한 번 적재한다.

        이미 열려 있으면 그 항목과 `False`, 이번 호출이 넣었으면 입력 항목과 `True`를
        반환한다. 판정·`_open`·`history`·`_by_case` 갱신은 한 임계구역이다.
        """
        item = deepcopy(item)
        if item.request_id is not None:
            raise ValueError(
                "request-aware FromDeadlock은 P17.5 전용 경로 없이 적재할 수 없습니다."
            )
        if not isinstance(item.source, FromDeadlock):
            raise ValueError("원자 deadlock 적재는 FromDeadlock 항목만 받을 수 있습니다.")
        case_id = item.source.case.case_id
        with self._lock:
            existing = self._by_case.get(case_id)
            if existing is not None:
                return deepcopy(existing), False
            if any(existing.item_id == item.item_id for existing in self._history):
                raise ValueError("ManagerItem.item_id가 기존 항목과 충돌합니다.")
            self._enqueue_unlocked(item)
            return item, True

    def get(self, item_id: str) -> ManagerItem | None:
        with self._lock:
            # open 또는 history에서 최신 상태 반환
            if item_id in self._open:
                return deepcopy(self._open[item_id])
            for h in reversed(self._history):
                if h.item_id == item_id:
                    return deepcopy(h)
            return None

    def pending_for_manager(self, manager_id: str) -> list[ManagerItem]:
        with self._lock:
            return deepcopy([item for item in self._open.values() if item.manager_id == manager_id])

    def get_by_case(self, case_id: str) -> ManagerItem | None:
        with self._lock:
            return deepcopy(self._by_case.get(case_id))

    def create_or_get_for_request(
        self,
        item: ManagerItem,
    ) -> tuple[ManagerItem, bool]:
        """요청별 Unowned/Deadlock 항목을 한 번만 만들고 semantic 재시도만 수용한다."""
        from agent_org_network.request_correlation import LinkedEntityMismatchError

        item = deepcopy(item)
        fingerprint = _manager_request_fingerprint(item)
        from agent_org_network.request_correlation import require_request_id

        request_id = require_request_id(item.request_id)
        with self._lock:
            existing = self._by_request.get(request_id)
            if existing is not None:
                if _manager_request_fingerprint(existing) != fingerprint:
                    raise LinkedEntityMismatchError(
                        f"Question Request {request_id!r}의 ManagerItem payload가 다릅니다."
                    )
                return deepcopy(existing), False
            if isinstance(item.source, FromDeadlock):
                by_case = self._by_case.get(item.source.case.case_id)
                if by_case is not None:
                    raise LinkedEntityMismatchError(
                        f"ConflictCase {item.source.case.case_id!r}의 ManagerItem이 이미 있습니다."
                    )
            if item.status != "open" or item.resolution is not None:
                raise ValueError("request-aware ManagerItem 생성은 open 원형만 받습니다.")
            if any(existing.item_id == item.item_id for existing in self._history):
                raise LinkedEntityMismatchError(
                    f"ManagerItem.item_id가 다른 Request와 충돌합니다: {item.item_id!r}"
                )
            self._enqueue_unlocked(item)
            return deepcopy(item), True

    def get_by_request(self, request_id: str) -> ManagerItem | None:
        from agent_org_network.request_correlation import require_request_id

        correlated_request_id = require_request_id(request_id)
        with self._lock:
            return deepcopy(self._by_request.get(correlated_request_id))

    def mark_resolved(self, item: ManagerItem) -> None:
        item = deepcopy(item)
        if item.request_id is not None:
            raise ValueError(
                "request-aware ManagerItem은 generation-bound claim으로만 종결할 수 있습니다."
            )
        # 직접 사용자의 기존 공개 계약 보존: 알 수 없는/이미 닫힌 item을 넘겨도 resolved
        # history를 append하던 동작은 유지한다. 서비스 경합은 resolve_if_open이 원자적으로 막는다.
        with self._lock:
            current = self._open.get(item.item_id)
            if current is None:
                current = next(
                    (
                        stored
                        for stored in reversed(self._history)
                        if stored.item_id == item.item_id
                    ),
                    None,
                )
            if current is not None:
                if current.request_id is not None:
                    raise ValueError(
                        "request-aware ManagerItem은 legacy mark_resolved로 닫을 수 없습니다."
                    )
                if (
                    current.manager_id != item.manager_id
                    or current.source != item.source
                    or current.created_at != item.created_at
                ):
                    raise ValueError("ManagerItem.item_id가 기존 항목과 충돌합니다.")
            self._mark_resolved_unlocked(item)

    def resolve_if_open(
        self,
        item_id: str,
        action: ManagerAction,
        transition: Callable[[ManagerItem, _RunManagerEffectOnce], ManagerItem],
    ) -> ManagerItem | None:
        """open 항목의 처분 선점·성공 효과 ledger·종결을 한 임계구역에서 수행한다.

        먼저 들어온 action이 항목을 선점한다. callback이 실패하면 항목은 open이고 같은
        action으로 재시도할 수 있으며, `run_effect_once`로 성공 표시된 효과는 건너뛴다.
        선점 뒤 다른 action은 명시적 상충으로 거부한다. callback이 돌려준 값은 동일
        item_id의 resolved 전이여야 한다.
        """
        action = deepcopy(action)
        with self._lock:
            current = self._open.get(item_id)
            if current is None:
                return None
            if current.request_id is not None:
                raise ValueError(
                    "request-aware ManagerItem은 legacy resolve_if_open으로 처리할 수 없습니다."
                )

            progress = self._action_progress.get(item_id)
            if progress is None:
                progress = _ManagerActionProgress(action=action)
                self._action_progress[item_id] = progress
            elif progress.action != action:
                raise ValueError(
                    "상충하는 Manager 처분: "
                    f"item_id={item_id!r}는 이미 {progress.action!r} 처분이 진행 중입니다."
                )

            def run_effect_once(
                effect: _ManagerEffectKey,
                operation: Callable[[], None],
            ) -> None:
                if effect in progress.completed_effects:
                    return
                operation()
                # operation이 정상 반환한 뒤에만 완료로 남긴다. commit 전 실패라면 같은
                # action의 다음 호출이 이 효과를 다시 실행할 수 있다.
                progress.completed_effects.add(effect)

            resolved = deepcopy(transition(deepcopy(current), run_effect_once))
            if resolved.item_id != item_id:
                raise ValueError("원자 전이는 같은 ManagerItem.item_id를 보존해야 합니다.")
            if resolved.status != "resolved":
                raise ValueError("원자 전이는 resolved ManagerItem을 반환해야 합니다.")
            self._mark_resolved_unlocked(resolved)
            return deepcopy(resolved)

    def reserve_validated_action(
        self,
        item_id: str,
        command: P17ManagerDispositionCommand,
        validate: Callable[
            [ManagerItem],
            ReservedAssignOwnerClaim | ReservedDismissClaim,
        ],
    ) -> ClaimAttempt:
        """첫 reservation만 control token을 받고 follower는 상태값만 받는다."""
        from agent_org_network.p17_manager_disposition import (
            ClaimAcquired,
            ClaimConflict,
            ClaimInProgress,
            ManagerDispositionIntegrity,
            ReservedAssignOwnerClaim,
            ReservedDismissClaim,
            ReservationControlToken,
            SealedAssignOwnerClaim,
            SealedClaimAvailable,
            SealedClaimHandle,
            SealedDismissClaim,
            canonical_manager_claim,
            canonical_manager_command,
            manager_claim_matches_command,
        )

        command = canonical_manager_command(command)
        with self._lock:
            existing = self._disposition_claims.get(item_id)
            if existing is not None:
                if not isinstance(
                    existing,
                    (
                        ReservedAssignOwnerClaim,
                        ReservedDismissClaim,
                    ),
                ):
                    if not isinstance(existing, (SealedAssignOwnerClaim, SealedDismissClaim)):
                        return ClaimConflict()
                if not manager_claim_matches_command(existing, command):
                    return ClaimConflict()
                if isinstance(existing, (ReservedAssignOwnerClaim, ReservedDismissClaim)):
                    return ClaimInProgress()
                handle = self._disposition_forward_handles.get(item_id)
                if type(handle) is not SealedClaimHandle:
                    raise ManagerDispositionIntegrity()
                return deepcopy(SealedClaimAvailable(claim=existing, handle=handle))

            if item_id in self._disposition_validation:
                self._disposition_reentered.add(item_id)
                raise ManagerDispositionIntegrity()

            current = self._open.get(item_id)
            if current is None:
                if any(item.item_id == item_id for item in self._history):
                    raise ManagerDispositionIntegrity()
                raise ManagerDispositionIntegrity()
            if current.status != "open" or current.resolution is not None:
                raise ManagerDispositionIntegrity()
            if not isinstance(current.source, FromUnowned):
                raise ManagerDispositionIntegrity()

            # callback 재진입은 fail-closed하고, 예외면 reservation을 남기지 않는다.
            self._disposition_validation.add(item_id)
            try:
                raw_claim = validate(deepcopy(current))
            except BaseException:
                self._disposition_reentered.discard(item_id)
                raise
            finally:
                self._disposition_validation.discard(item_id)
            if item_id in self._disposition_reentered:
                self._disposition_reentered.remove(item_id)
                raise ManagerDispositionIntegrity()
            claim = canonical_manager_claim(raw_claim)
            if not isinstance(claim, (ReservedAssignOwnerClaim, ReservedDismissClaim)):
                raise ManagerDispositionIntegrity()
            if (
                claim.item_id != current.item_id
                or claim.request_id != current.request_id
                or claim.by_manager != current.manager_id
                or claim.idempotency_key != f"manager-disposition:{current.item_id}"
                or not manager_claim_matches_command(claim, command)
                or claim.generation in self._disposition_generations
            ):
                raise ManagerDispositionIntegrity()
            control = ReservationControlToken(
                generation=claim.generation,
                token=uuid.uuid4().hex,
            )
            self._disposition_claims[item_id] = deepcopy(claim)
            self._disposition_control_tokens[item_id] = deepcopy(control)
            self._disposition_generations.add(claim.generation)
            return deepcopy(ClaimAcquired(claim=claim, control_token=control))

    def claim_for_item(self, item_id: str) -> ManagerDispositionClaim | None:
        from agent_org_network.p17_manager_disposition import (
            ManagerDispositionIntegrity,
            ReservedAssignOwnerClaim,
            ReservedDismissClaim,
            SealedAssignOwnerClaim,
            SealedDismissClaim,
        )

        with self._lock:
            claim = self._disposition_claims.get(item_id)
            if claim is None:
                current = self._open.get(item_id)
                if current is not None and not isinstance(current.source, FromUnowned):
                    raise ManagerDispositionIntegrity()
                return None
            if not isinstance(
                claim,
                (
                    ReservedAssignOwnerClaim,
                    ReservedDismissClaim,
                    SealedAssignOwnerClaim,
                    SealedDismissClaim,
                ),
            ):
                raise ManagerDispositionIntegrity()
            return deepcopy(claim)

    def validate_action_reservation(
        self,
        claim: ReservedAssignOwnerClaim | ReservedDismissClaim,
        *,
        control_token: ReservationControlToken,
    ) -> None:
        from agent_org_network.p17_manager_disposition import (
            ManagerDispositionIntegrity,
            ReservationControlToken,
            ReservedAssignOwnerClaim,
            ReservedDismissClaim,
            canonical_manager_claim,
        )

        canonical_claim = canonical_manager_claim(claim)
        if not isinstance(canonical_claim, (ReservedAssignOwnerClaim, ReservedDismissClaim)):
            raise ManagerDispositionIntegrity()
        try:
            if type(control_token) is not ReservationControlToken:
                raise TypeError("ReservationControlToken exact type이 필요합니다.")
            canonical_control = ReservationControlToken.model_validate(
                control_token.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        with self._lock:
            stored_claim = self._disposition_claims.get(canonical_claim.item_id)
            stored_control = self._disposition_control_tokens.get(canonical_claim.item_id)
            if (
                type(stored_claim) is not type(canonical_claim)
                or stored_claim != canonical_claim
                or type(stored_control) is not ReservationControlToken
                or stored_control != canonical_control
            ):
                raise ManagerDispositionIntegrity()

    def reserve_validated_deadlock_action(
        self,
        item_id: str,
        command: DeadlockManagerDispositionCommand,
        *,
        validate: Callable[
            [ManagerItem],
            ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        ],
    ) -> DeadlockManagerClaimAttempt:
        from agent_org_network.p17_manager_disposition import (
            DeadlockManagerClaimAcquired,
            DeadlockManagerClaimConflict,
            DeadlockManagerClaimInProgress,
            DeadlockManagerReservationControlToken,
            DeadlockManagerSealedClaimAvailable,
            DeadlockManagerSealedClaimHandle,
            ManagerDispositionIntegrity,
            ReservedDeadlockAssignClaim,
            ReservedDeadlockDismissClaim,
            SealedDeadlockAssignClaim,
            SealedDeadlockDismissClaim,
            canonical_deadlock_manager_claim,
            canonical_deadlock_manager_command,
            deadlock_manager_claim_matches_command,
        )

        canonical_command = canonical_deadlock_manager_command(command)
        with self._lock:
            existing = self._disposition_claims.get(item_id)
            if existing is not None:
                if not isinstance(
                    existing,
                    (
                        ReservedDeadlockAssignClaim,
                        ReservedDeadlockDismissClaim,
                        SealedDeadlockAssignClaim,
                        SealedDeadlockDismissClaim,
                    ),
                ):
                    return DeadlockManagerClaimConflict()
                if not deadlock_manager_claim_matches_command(existing, canonical_command):
                    return DeadlockManagerClaimConflict()
                if isinstance(
                    existing,
                    (ReservedDeadlockAssignClaim, ReservedDeadlockDismissClaim),
                ):
                    return DeadlockManagerClaimInProgress()
                handle = self._disposition_forward_handles.get(item_id)
                if type(handle) is not DeadlockManagerSealedClaimHandle:
                    raise ManagerDispositionIntegrity()
                return deepcopy(DeadlockManagerSealedClaimAvailable(claim=existing, handle=handle))

            if item_id in self._disposition_validation:
                self._disposition_reentered.add(item_id)
                raise ManagerDispositionIntegrity()
            current = self._open.get(item_id)
            if current is None or current.status != "open" or current.resolution is not None:
                raise ManagerDispositionIntegrity()
            source = current.source
            if (
                not isinstance(source, FromDeadlock)
                or current.request_id is None
                or source.cause is None
            ):
                raise ManagerDispositionIntegrity()

            self._disposition_validation.add(item_id)
            try:
                raw_claim = validate(deepcopy(current))
            except BaseException:
                self._disposition_reentered.discard(item_id)
                raise
            finally:
                self._disposition_validation.discard(item_id)
            if item_id in self._disposition_reentered:
                self._disposition_reentered.remove(item_id)
                raise ManagerDispositionIntegrity()
            claim = canonical_deadlock_manager_claim(raw_claim)
            if not isinstance(claim, (ReservedDeadlockAssignClaim, ReservedDeadlockDismissClaim)):
                raise ManagerDispositionIntegrity()
            case = source.case
            if (
                claim.item_id != current.item_id
                or claim.request_id != current.request_id
                or claim.case_id != case.case_id
                or claim.org_id != canonical_command.principal.org_id
                or claim.by_manager != current.manager_id
                or claim.intent != case.intent
                or claim.round != case.concurrence_round
                or claim.cause != source.cause
                or claim.idempotency_key != f"manager-disposition:{current.item_id}"
                or not deadlock_manager_claim_matches_command(claim, canonical_command)
                or claim.generation in self._disposition_generations
            ):
                raise ManagerDispositionIntegrity()
            if isinstance(claim, ReservedDeadlockAssignClaim) and claim.agent_id not in (
                candidate.agent_id for candidate in case.candidates
            ):
                raise ManagerDispositionIntegrity()
            control = DeadlockManagerReservationControlToken(
                generation=claim.generation,
                token=uuid.uuid4().hex,
            )
            self._disposition_claims[item_id] = deepcopy(claim)
            self._disposition_control_tokens[item_id] = deepcopy(control)
            self._disposition_generations.add(claim.generation)
            return deepcopy(DeadlockManagerClaimAcquired(claim=claim, control_token=control))

    def deadlock_claim_for_item(
        self,
        item_id: str,
    ) -> DeadlockManagerDispositionClaim | None:
        from agent_org_network.p17_manager_disposition import (
            ManagerDispositionIntegrity,
            ReservedDeadlockAssignClaim,
            ReservedDeadlockDismissClaim,
            SealedDeadlockAssignClaim,
            SealedDeadlockDismissClaim,
        )

        with self._lock:
            claim = self._disposition_claims.get(item_id)
            if claim is None:
                current = self._open.get(item_id)
                if current is not None and not isinstance(current.source, FromDeadlock):
                    raise ManagerDispositionIntegrity()
                return None
            if not isinstance(
                claim,
                (
                    ReservedDeadlockAssignClaim,
                    ReservedDeadlockDismissClaim,
                    SealedDeadlockAssignClaim,
                    SealedDeadlockDismissClaim,
                ),
            ):
                raise ManagerDispositionIntegrity()
            return deepcopy(claim)

    def validate_deadlock_action_reservation(
        self,
        claim: ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        *,
        control_token: DeadlockManagerReservationControlToken,
    ) -> None:
        from agent_org_network.p17_manager_disposition import (
            DeadlockManagerReservationControlToken,
            ManagerDispositionIntegrity,
            ReservedDeadlockAssignClaim,
            ReservedDeadlockDismissClaim,
            canonical_deadlock_manager_claim,
        )

        canonical_claim = canonical_deadlock_manager_claim(claim)
        if not isinstance(
            canonical_claim,
            (ReservedDeadlockAssignClaim, ReservedDeadlockDismissClaim),
        ):
            raise ManagerDispositionIntegrity()
        try:
            if type(control_token) is not DeadlockManagerReservationControlToken:
                raise TypeError("DeadlockManagerReservationControlToken exact type이 필요합니다.")
            canonical_control = DeadlockManagerReservationControlToken.model_validate(
                control_token.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        with self._lock:
            stored_claim = self._disposition_claims.get(canonical_claim.item_id)
            stored_control = self._disposition_control_tokens.get(canonical_claim.item_id)
            if (
                type(stored_claim) is not type(canonical_claim)
                or stored_claim != canonical_claim
                or type(stored_control) is not DeadlockManagerReservationControlToken
                or stored_control != canonical_control
            ):
                raise ManagerDispositionIntegrity()

    def seal_deadlock_claim(
        self,
        claim: ReservedDeadlockAssignClaim | ReservedDeadlockDismissClaim,
        *,
        control_token: DeadlockManagerReservationControlToken,
    ) -> DeadlockManagerSealedClaimAvailable:
        from agent_org_network.p17_manager_disposition import (
            DeadlockManagerReservationControlToken,
            DeadlockManagerSealedClaimAvailable,
            DeadlockManagerSealedClaimHandle,
            ManagerDispositionIntegrity,
            ReservedDeadlockAssignClaim,
            ReservedDeadlockDismissClaim,
            SealedDeadlockAssignClaim,
            SealedDeadlockDismissClaim,
            canonical_deadlock_manager_claim,
        )

        canonical_claim = canonical_deadlock_manager_claim(claim)
        if not isinstance(
            canonical_claim,
            (ReservedDeadlockAssignClaim, ReservedDeadlockDismissClaim),
        ):
            raise ManagerDispositionIntegrity()
        try:
            if type(control_token) is not DeadlockManagerReservationControlToken:
                raise ManagerDispositionIntegrity()
            canonical_control = DeadlockManagerReservationControlToken.model_validate(
                control_token.model_dump(mode="python", round_trip=True), strict=True
            )
        except ManagerDispositionIntegrity:
            raise
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        with self._lock:
            current = self._disposition_claims.get(canonical_claim.item_id)
            stored_control = self._disposition_control_tokens.get(canonical_claim.item_id)
            if current != canonical_claim or stored_control != canonical_control:
                raise ManagerDispositionIntegrity()
            if isinstance(current, ReservedDeadlockAssignClaim):
                sealed: SealedDeadlockAssignClaim | SealedDeadlockDismissClaim = (
                    SealedDeadlockAssignClaim(
                        generation=current.generation,
                        idempotency_key=current.idempotency_key,
                        request_id=current.request_id,
                        case_id=current.case_id,
                        item_id=current.item_id,
                        org_id=current.org_id,
                        by_manager=current.by_manager,
                        intent=current.intent,
                        round=current.round,
                        cause=current.cause,
                        rationale=current.rationale,
                        agent_id=current.agent_id,
                        requires_approval=current.requires_approval,
                    )
                )
            elif isinstance(current, ReservedDeadlockDismissClaim):
                sealed = SealedDeadlockDismissClaim(
                    generation=current.generation,
                    idempotency_key=current.idempotency_key,
                    request_id=current.request_id,
                    case_id=current.case_id,
                    item_id=current.item_id,
                    org_id=current.org_id,
                    by_manager=current.by_manager,
                    intent=current.intent,
                    round=current.round,
                    cause=current.cause,
                    rationale=current.rationale,
                )
            else:
                raise ManagerDispositionIntegrity()
            handle = DeadlockManagerSealedClaimHandle(
                generation=sealed.generation,
                forward_token=uuid.uuid4().hex,
            )
            self._disposition_claims[sealed.item_id] = deepcopy(sealed)
            self._disposition_control_tokens.pop(sealed.item_id, None)
            self._disposition_forward_handles[sealed.item_id] = deepcopy(handle)
            return deepcopy(DeadlockManagerSealedClaimAvailable(claim=sealed, handle=handle))

    def abandon_unmutated_deadlock_assign(
        self,
        claim: ReservedDeadlockAssignClaim,
        *,
        control_token: DeadlockManagerReservationControlToken,
        rejection: RequestRouteGrantRejected,
    ) -> None:
        from agent_org_network.p17_manager_disposition import (
            DeadlockManagerReservationControlToken,
            ManagerDispositionIntegrity,
            ReservedDeadlockAssignClaim,
            canonical_deadlock_manager_claim,
        )
        from agent_org_network.request_route_authority import RequestRouteGrantRejected

        canonical_claim = canonical_deadlock_manager_claim(claim)
        if type(canonical_claim) is not ReservedDeadlockAssignClaim:
            raise ManagerDispositionIntegrity()
        try:
            if type(control_token) is not DeadlockManagerReservationControlToken:
                raise ManagerDispositionIntegrity()
            canonical_control = DeadlockManagerReservationControlToken.model_validate(
                control_token.model_dump(mode="python", round_trip=True), strict=True
            )
            if type(rejection) is not RequestRouteGrantRejected:
                raise ManagerDispositionIntegrity()
            canonical_rejection = RequestRouteGrantRejected.model_validate(
                rejection.model_dump(mode="python", round_trip=True), strict=True
            )
        except ManagerDispositionIntegrity:
            raise
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        with self._lock:
            if (
                self._disposition_claims.get(canonical_claim.item_id) != canonical_claim
                or self._disposition_control_tokens.get(canonical_claim.item_id)
                != canonical_control
                or canonical_rejection.idempotency_key != canonical_claim.idempotency_key
                or canonical_claim.item_id in self._disposition_forward_handles
                or canonical_claim.item_id in self._disposition_resume_evidence
            ):
                raise ManagerDispositionIntegrity()
            self._disposition_claims.pop(canonical_claim.item_id, None)
            self._disposition_control_tokens.pop(canonical_claim.item_id, None)

    def deadlock_claim_for_handle(
        self,
        handle: DeadlockManagerSealedClaimHandle,
    ) -> SealedDeadlockAssignClaim | SealedDeadlockDismissClaim:
        from agent_org_network.p17_manager_disposition import (
            DeadlockManagerSealedClaimHandle,
            ManagerDispositionIntegrity,
            SealedDeadlockAssignClaim,
            SealedDeadlockDismissClaim,
        )

        try:
            if type(handle) is not DeadlockManagerSealedClaimHandle:
                raise ManagerDispositionIntegrity()
            canonical_handle = DeadlockManagerSealedClaimHandle.model_validate(
                handle.model_dump(mode="python", round_trip=True), strict=True
            )
        except ManagerDispositionIntegrity:
            raise
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        with self._lock:
            for item_id, stored_handle in self._disposition_forward_handles.items():
                if stored_handle == canonical_handle:
                    claim = self._disposition_claims.get(item_id)
                    if (
                        isinstance(
                            claim,
                            (SealedDeadlockAssignClaim, SealedDeadlockDismissClaim),
                        )
                        and claim.generation == canonical_handle.generation
                    ):
                        return deepcopy(claim)
                    break
            raise ManagerDispositionIntegrity()

    def seal_claim(
        self,
        claim: ReservedAssignOwnerClaim | ReservedDismissClaim,
        *,
        control_token: ReservationControlToken,
    ) -> SealedClaimAvailable:
        """generation-bound control token으로 reserved 값을 sealed 값으로 교체한다."""
        claim = deepcopy(claim)
        control_token = deepcopy(control_token)
        from agent_org_network.p17_manager_disposition import (
            ManagerDispositionIntegrity,
            ReservedAssignOwnerClaim,
            ReservedDismissClaim,
            SealedClaimAvailable,
            SealedClaimHandle,
            SealedAssignOwnerClaim,
            SealedDismissClaim,
        )

        with self._lock:
            current = self._disposition_claims.get(claim.item_id)
            stored_control = self._disposition_control_tokens.get(claim.item_id)
            if current != claim or stored_control != control_token:
                raise ManagerDispositionIntegrity()
            if isinstance(current, ReservedAssignOwnerClaim):
                sealed: SealedAssignOwnerClaim | SealedDismissClaim = SealedAssignOwnerClaim(
                    generation=current.generation,
                    idempotency_key=current.idempotency_key,
                    request_id=current.request_id,
                    item_id=current.item_id,
                    org_id=current.org_id,
                    by_manager=current.by_manager,
                    intent=current.intent,
                    agent_id=current.agent_id,
                    requires_approval=current.requires_approval,
                    rationale=current.rationale,
                )
            elif isinstance(current, ReservedDismissClaim):
                sealed = SealedDismissClaim(
                    generation=current.generation,
                    idempotency_key=current.idempotency_key,
                    request_id=current.request_id,
                    item_id=current.item_id,
                    org_id=current.org_id,
                    by_manager=current.by_manager,
                    rationale=current.rationale,
                )
            else:
                raise ManagerDispositionIntegrity()
            handle = SealedClaimHandle(
                generation=sealed.generation,
                forward_token=uuid.uuid4().hex,
            )
            self._disposition_claims[current.item_id] = deepcopy(sealed)
            self._disposition_control_tokens.pop(current.item_id, None)
            self._disposition_forward_handles[current.item_id] = deepcopy(handle)
            return deepcopy(SealedClaimAvailable(claim=sealed, handle=handle))

    def abandon_unmutated_claim(
        self,
        claim: ReservedAssignOwnerClaim,
        *,
        control_token: ReservationControlToken,
    ) -> None:
        """Authority가 write 0을 보증한 reserved claim만 흔적 없이 해제한다."""
        claim = deepcopy(claim)
        control_token = deepcopy(control_token)
        from agent_org_network.p17_manager_disposition import (
            ManagerDispositionIntegrity,
            ReservedAssignOwnerClaim,
        )

        with self._lock:
            current = self._disposition_claims.get(claim.item_id)
            if (
                current != claim
                or not isinstance(current, ReservedAssignOwnerClaim)
                or self._disposition_control_tokens.get(claim.item_id) != control_token
            ):
                raise ManagerDispositionIntegrity()
            del self._disposition_claims[claim.item_id]
            self._disposition_control_tokens.pop(claim.item_id, None)

    def record_resume_evidence(
        self,
        handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
        evidence: ResumeEvidence,
    ) -> None:
        from agent_org_network.p17_manager_disposition import (
            DeadlockManagerSealedClaimHandle,
            ManagerDispositionIntegrity,
            ResumeEvidence,
            SealedClaimHandle,
            SealedAssignOwnerClaim,
            SealedDeadlockAssignClaim,
        )

        try:
            if type(handle) is SealedClaimHandle:
                canonical_handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle = (
                    SealedClaimHandle.model_validate(
                        handle.model_dump(mode="python", round_trip=True), strict=True
                    )
                )
            elif type(handle) is DeadlockManagerSealedClaimHandle:
                canonical_handle = DeadlockManagerSealedClaimHandle.model_validate(
                    handle.model_dump(mode="python", round_trip=True), strict=True
                )
            else:
                raise ManagerDispositionIntegrity()
            if type(evidence) is not ResumeEvidence:
                raise ManagerDispositionIntegrity()
            canonical_evidence = ResumeEvidence.model_validate(
                evidence.model_dump(mode="python", round_trip=True), strict=True
            )
        except ManagerDispositionIntegrity:
            raise
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        with self._lock:
            item_id = next(
                (
                    key
                    for key, stored in self._disposition_forward_handles.items()
                    if stored == canonical_handle and type(stored) is type(canonical_handle)
                ),
                None,
            )
            if item_id is None:
                raise ManagerDispositionIntegrity()
            claim = self._disposition_claims.get(item_id)
            if type(canonical_handle) is SealedClaimHandle:
                if not isinstance(claim, SealedAssignOwnerClaim):
                    raise ManagerDispositionIntegrity()
                expected_from, expected_to = 1, 2
            elif isinstance(claim, SealedDeadlockAssignClaim):
                expected_from, expected_to = 2, 3
            else:
                raise ManagerDispositionIntegrity()
            if (
                canonical_evidence.request_id != claim.request_id
                or canonical_evidence.route.intent != claim.intent
                or canonical_evidence.route.agent_id != claim.agent_id
                or canonical_evidence.route.requires_approval != claim.requires_approval
                or canonical_evidence.route.authority_version is None
                or not canonical_evidence.route.authority_version.strip()
                or canonical_evidence.attempt != 1
                or canonical_evidence.trigger_key != f"request-dispatch:{claim.request_id}:1"
                or canonical_evidence.to_revision != canonical_evidence.from_revision + 1
                or canonical_evidence.from_revision != expected_from
                or canonical_evidence.to_revision != expected_to
            ):
                raise ManagerDispositionIntegrity()
            existing = self._disposition_resume_evidence.get(item_id)
            if existing is not None and existing != canonical_evidence:
                raise ManagerDispositionIntegrity()
            self._disposition_resume_evidence[item_id] = deepcopy(canonical_evidence)

    def resume_evidence_for_claim(
        self,
        handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
    ) -> ResumeEvidence | None:
        from agent_org_network.p17_manager_disposition import (
            DeadlockManagerSealedClaimHandle,
            ManagerDispositionIntegrity,
            SealedClaimHandle,
            SealedAssignOwnerClaim,
            SealedDeadlockAssignClaim,
        )

        try:
            if type(handle) is SealedClaimHandle:
                canonical_handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle = (
                    SealedClaimHandle.model_validate(
                        handle.model_dump(mode="python", round_trip=True), strict=True
                    )
                )
            elif type(handle) is DeadlockManagerSealedClaimHandle:
                canonical_handle = DeadlockManagerSealedClaimHandle.model_validate(
                    handle.model_dump(mode="python", round_trip=True), strict=True
                )
            else:
                raise ManagerDispositionIntegrity()
        except ManagerDispositionIntegrity:
            raise
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        with self._lock:
            item_id = next(
                (
                    key
                    for key, stored in self._disposition_forward_handles.items()
                    if stored == canonical_handle and type(stored) is type(canonical_handle)
                ),
                None,
            )
            if item_id is None:
                raise ManagerDispositionIntegrity()
            claim = self._disposition_claims.get(item_id)
            if type(canonical_handle) is SealedClaimHandle:
                if not isinstance(claim, SealedAssignOwnerClaim):
                    raise ManagerDispositionIntegrity()
            elif not isinstance(claim, SealedDeadlockAssignClaim):
                raise ManagerDispositionIntegrity()
            return deepcopy(self._disposition_resume_evidence.get(item_id))

    def resolve_for_claim(
        self,
        handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle,
        resolved: ManagerItem,
    ) -> ManagerItem:
        """sealed P17 winner와 exact resolution만 한 임계구역에서 종결한다."""
        resolved = deepcopy(resolved)
        from agent_org_network.p17_manager_disposition import (
            DeadlockManagerSealedClaimHandle,
            ManagerDispositionIntegrity,
            SealedClaimHandle,
            SealedAssignOwnerClaim,
            SealedDeadlockAssignClaim,
            SealedDeadlockDismissClaim,
            SealedDismissClaim,
        )

        try:
            if type(handle) is SealedClaimHandle:
                canonical_handle: SealedClaimHandle | DeadlockManagerSealedClaimHandle = (
                    SealedClaimHandle.model_validate(
                        handle.model_dump(mode="python", round_trip=True), strict=True
                    )
                )
            elif type(handle) is DeadlockManagerSealedClaimHandle:
                canonical_handle = DeadlockManagerSealedClaimHandle.model_validate(
                    handle.model_dump(mode="python", round_trip=True), strict=True
                )
            else:
                raise ManagerDispositionIntegrity()
        except ManagerDispositionIntegrity:
            raise
        except Exception as error:
            raise ManagerDispositionIntegrity() from error
        with self._lock:
            item_id = next(
                (
                    key
                    for key, stored in self._disposition_forward_handles.items()
                    if stored == canonical_handle and type(stored) is type(canonical_handle)
                ),
                None,
            )
            if item_id is None:
                raise ManagerDispositionIntegrity()
            winner = self._disposition_claims.get(item_id)
            if not isinstance(
                winner,
                (
                    SealedAssignOwnerClaim,
                    SealedDismissClaim,
                    SealedDeadlockAssignClaim,
                    SealedDeadlockDismissClaim,
                ),
            ):
                raise ManagerDispositionIntegrity()
            if type(canonical_handle) is SealedClaimHandle and not isinstance(
                winner, (SealedAssignOwnerClaim, SealedDismissClaim)
            ):
                raise ManagerDispositionIntegrity()
            if type(canonical_handle) is DeadlockManagerSealedClaimHandle and not isinstance(
                winner, (SealedDeadlockAssignClaim, SealedDeadlockDismissClaim)
            ):
                raise ManagerDispositionIntegrity()
            current = self._open.get(item_id)
            if current is None:
                stored = next(
                    (item for item in reversed(self._history) if item.item_id == item_id),
                    None,
                )
                if stored != resolved or stored is None:
                    raise ManagerDispositionIntegrity()
                candidate = stored
            else:
                if (
                    resolved.item_id != current.item_id
                    or resolved.request_id != current.request_id
                    or resolved.manager_id != current.manager_id
                    or resolved.source != current.source
                    or resolved.created_at != current.created_at
                    or resolved.status != "resolved"
                    or resolved.resolution is None
                ):
                    raise ManagerDispositionIntegrity()
                candidate = resolved
            if candidate.resolution is None:
                raise ManagerDispositionIntegrity()
            action = candidate.resolution.action
            if isinstance(winner, SealedAssignOwnerClaim):
                if candidate.resolution.resolution is not None:
                    raise ManagerDispositionIntegrity()
                evidence = self._disposition_resume_evidence.get(item_id)
                if evidence is None:
                    raise ManagerDispositionIntegrity()
                if not isinstance(action, AssignOwner) or action != AssignOwner(
                    by_manager=winner.by_manager,
                    primary=winner.agent_id,
                    rationale=winner.rationale,
                ):
                    raise ManagerDispositionIntegrity()
            elif isinstance(winner, SealedDeadlockAssignClaim):
                evidence = self._disposition_resume_evidence.get(item_id)
                expected_resolution = Resolution(
                    intent=winner.intent,
                    primary=winner.agent_id,
                    rationale=winner.rationale,
                )
                if (
                    evidence is None
                    or not isinstance(action, AssignOwner)
                    or action
                    != AssignOwner(
                        by_manager=winner.by_manager,
                        primary=winner.agent_id,
                        rationale=winner.rationale,
                    )
                    or candidate.resolution.resolution != expected_resolution
                ):
                    raise ManagerDispositionIntegrity()
            else:
                if candidate.resolution.resolution is not None or not isinstance(action, Dismiss):
                    raise ManagerDispositionIntegrity()
                if action != Dismiss(
                    by_manager=winner.by_manager,
                    rationale=winner.rationale,
                ):
                    raise ManagerDispositionIntegrity()
            if current is None:
                return deepcopy(candidate)
            self._mark_resolved_unlocked(candidate)
            return deepcopy(candidate)

    def _mark_resolved_unlocked(self, item: ManagerItem) -> None:
        self._open.pop(item.item_id, None)
        item = deepcopy(item)
        self._history.append(item)
        self._action_progress.pop(item.item_id, None)
        if item.request_id is not None:
            self._by_request[item.request_id] = item
        # case 색인도 정리(resolved됐으므로 중복 방지 해제)
        if isinstance(item.source, FromDeadlock):
            self._by_case.pop(item.source.case.case_id, None)

    def _enqueue_unlocked(self, item: ManagerItem) -> None:
        item = deepcopy(item)
        self._open[item.item_id] = item
        self._history.append(item)
        if item.request_id is not None:
            self._by_request[item.request_id] = item
        if isinstance(item.source, FromDeadlock):
            self._by_case[item.source.case.case_id] = item


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

    InMemory store에서는 선택적 원자 seam을 사용하고, 공개 5메서드만 제공하는 custom
    store에서는 순차 하위호환 경로를 사용한다.
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
        item = self._queue_store.get(item_id)
        if item is None:
            raise ValueError(f"미존재 Manager 큐 항목: {item_id!r}")

        if item.request_id is not None:
            raise ValueError(
                "request-aware ManagerItem은 P17ManagerDispositionApplication으로만 "
                "처분할 수 있습니다."
            )

        self._validate_first_person(item, action)

        # 멱등: 이미 resolved면 그대로
        if item.status == "resolved":
            return item

        # InMemory store가 제공하는 선택적 원자 seam: callback 안의 판정·Precedent/case
        # side effect·ManagerItem 종결이 store의 한 임계구역에서 실행된다. FastAPI는 요청마다
        # ManagerQueueService를 새로 만들므로 서비스 인스턴스 lock으로는 이 경합을 못 막는다.
        if isinstance(self._queue_store, _AtomicManagerQueueStore):
            resolved = self._queue_store.resolve_if_open(
                item_id,
                action,
                lambda current, run_effect_once: self._resolve_open_item(
                    current,
                    action,
                    run_effect_once,
                ),
            )
            if resolved is not None:
                return resolved
            # 다른 요청이 먼저 끝냈다. 그 terminal 값을 멱등 결과로 돌려준다.
            latest = self._queue_store.get(item_id)
            if latest is None:
                raise ValueError(f"미존재 Manager 큐 항목: {item_id!r}")
            return latest

        # 기존 5메서드 custom store의 순차 하위호환. 이 경로는 처분 선점/부분 성공 ledger가
        # 없어 동시 처리나 외부 효과의 exactly-once 재시도를 보장하지 않는다.
        resolved_item = self._resolve_open_item(item, action)
        self._queue_store.mark_resolved(resolved_item)
        return resolved_item

    @staticmethod
    def _validate_first_person(item: ManagerItem, action: ManagerAction) -> None:
        """처분 주체가 현재 항목의 Manager인지 검증한다(원자 callback에서도 재검증)."""
        if action.by_manager != item.manager_id:
            raise ValueError(
                f"1인칭 위반: by_manager({action.by_manager!r})가 "
                f"item.manager_id({item.manager_id!r})와 다름 — 자기 큐만 처리할 수 있다"
            )

    def _resolve_open_item(
        self,
        item: ManagerItem,
        action: ManagerAction,
        run_effect_once: _RunManagerEffectOnce | None = None,
    ) -> ManagerItem:
        """open 항목에 행위 side effect를 적용하고 resolved 값을 만든다(보관은 호출자)."""
        from typing import assert_never

        self._validate_first_person(item, action)

        def run_effect(
            effect: _ManagerEffectKey,
            operation: Callable[[], None],
        ) -> None:
            if run_effect_once is None:
                operation()
                return
            run_effect_once(effect, operation)

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

                precedents = self._precedents
                if intent and precedents is not None:
                    conflict_resolution = Resolution(
                        intent=intent,
                        primary=action.primary,
                        rationale=action.rationale,
                    )
                    resolution_to_record = conflict_resolution

                    def record_precedent() -> None:
                        precedents.record(resolution_to_record)

                    run_effect("precedent", record_precedent)

                # FromDeadlock이면 ConflictCase도 resolved
                case_store = self._case_store
                if isinstance(item.source, FromDeadlock) and case_store is not None:
                    case = item.source.case
                    existing = case_store.get(case.case_id)
                    if existing is not None:
                        if conflict_resolution is None:
                            # intent 없는 경우에도 case는 닫아야 함
                            conflict_resolution = Resolution(
                                intent=case.intent,
                                primary=action.primary,
                                rationale=action.rationale,
                            )
                        resolved_case = existing.resolve(conflict_resolution)

                        def resolve_conflict_case() -> None:
                            case_store.mark_resolved(resolved_case)

                        run_effect("conflict_case", resolve_conflict_case)

            case Reroute():
                pass  # Precedent 안 만듦

            case Dismiss():
                pass  # Precedent 안 만듦

            case _ as never:
                assert_never(never)

        mgr_resolution = ManagerResolution(action=action, resolution=conflict_resolution)
        return item.resolve(mgr_resolution)
