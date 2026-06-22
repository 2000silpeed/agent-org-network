"""지식 변경 재평가 루프 — ReevalItem + ReevalStore + ReevalService + StalenessPropagator (T7.3, ADR 0019).

**이 모듈은 shape(미구현 통과 stub)다 — tdd-engineer가 red→green으로 채운다.**

OKF 커밋(변경 이벤트)이 그 정책에 기댄 *과거 Precedent·답*을 stale로 표식하고 owner
재평가 큐에 적재하는 루프다(ADR 0017 결정 3② "살아있는 지식의 심장"·ADR 0019). ADR
0008(ConflictCase)·ADR 0012 결정 7(BackupReview)의 *처리함 포트 패턴*을 그대로 잇는 N번째
인스턴스 — 담는 값만 다르다(미해소 다툼·미검토 백업 답 → stale 표식 대상). 패턴 100%
재사용(Protocol + InMemory, owner 색인, 불변 전이).

도메인 위치: 변경 전파는 *다툼(conflict.py)·답 검토(review.py)와 다른 일*이라 별 모듈
(reeval.py)로 분리한다(ConflictCaseStore가 BackupReviewStore와 갈리는 판단과 동형).

전이 ≠ 기록:
  - `ReevalStore`(이 모듈) — 재평가 대기 상태의 도메인 보관소(전이).
  - `AuditLog`(`audit.py`) — 재검토 행위의 절차 기록(기록·호출자 책임).

stale ≠ 무효화(ADR 0019 결정 6):
  stale 플래그는 라우팅을 *바꾸지 않는다*(Router lookup이 needs_review를 안 봄 — 미아 없음).
  무효화는 owner가 처리함에서 `InvalidatePrecedent`를 명시 처분한 후만, 그것도 append-only로.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

if TYPE_CHECKING:
    from agent_org_network.audit import AuditReader
    from agent_org_network.conflict import PrecedentStore
    from agent_org_network.git_gateway import OkfChangeEvent

Clock = Callable[[], datetime]


def default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_item_id() -> str:
    return uuid.uuid4().hex


# ── 재평가 결과: ReevalOutcome sealed sum ─────────────────────────────────
#
# owner의 재평가 처분은 다섯 중 하나(ConsensusOutcome·BackupReview 정신 — "타입이 곧 상태").
# by_owner 1인칭 — 재평가는 그 owner만(자기 판례·자기 답). 서비스가 강제한다.
# arm 명명 확정(ADR 0019 결정 6):
#   Precedent 대상: KeepPrecedent | InvalidatePrecedent | SupersedePrecedent
#   Answer    대상: AcknowledgeAnswer | ReAnswer


@dataclass(frozen=True)
class KeepPrecedent:
    """owner가 '이 판례는 변경과 무관·그대로 둠'. needs_review는 남되 재평가는 닫힘."""

    by_owner: str
    rationale: str = ""


@dataclass(frozen=True)
class InvalidatePrecedent:
    """owner가 '이 판례를 무효로' — 라우팅 제외 명시 의사(실 제외 메커니즘은 후속).

    자동 무효화가 아니라 *owner 명시 후만*(ADR 0019 결정 6). append-only 표현
    (store 삭제 X — 무효 의사 표식까지가 이 ADR 범위).
    """

    by_owner: str
    rationale: str = ""


@dataclass(frozen=True)
class SupersedePrecedent:
    """owner가 '새 결론으로 갈음' — 새 Resolution을 record(intent 키 덮어쓰기).

    `InMemoryPrecedentStore.record`의 기존 동작으로 새 판례가 intent 키를 덮는다
    (삭제 없는 갱신 — append-only). 새 primary로 라우팅이 전환된다.
    """

    by_owner: str
    new_primary: str
    rationale: str = ""


@dataclass(frozen=True)
class AcknowledgeAnswer:
    """owner가 '옛 답이지만 그대로 유효 인정'. 재평가 닫힘·답 불변(audit append-only)."""

    by_owner: str
    rationale: str = ""


@dataclass(frozen=True)
class ReAnswer:
    """owner가 '다시 답해야 함' 표식 — 실 재답변 실행은 후속(자리만).

    audit은 append-only라 옛 답을 되쓰지 않는다 — 재답변 필요라는 *전이*만 든다.
    """

    by_owner: str
    rationale: str = ""


ReevalOutcome = (
    KeepPrecedent
    | InvalidatePrecedent
    | SupersedePrecedent
    | AcknowledgeAnswer
    | ReAnswer
)
#   owner 재평가 처분의 sealed sum(다섯 결말의 망라).


# ── 재평가 대상 보관 단위: ReevalItem ─────────────────────────────────────
#
# OKF 변경이 stale로 표식한 한 건의 재평가 대기 항목. ConflictCase가 미해소 다툼을,
# BackupReviewItem이 미검토 백업 답을 담듯 이건 *stale 표식 대상*(과거 판례·답)을 담는다
# — Owner 처리함(Inbox)의 세 번째 면. pending_review → reviewed 전이는 `review_with()`가
# item_id 보존한 새 인스턴스를 돌려준다(불변 + 새 인스턴스, BackupReviewItem 동형).
#
# DDD 주의(ADR 0019 결정 3 open question): subject_kind 문자열 판별자 + untyped subject_ref는
# 'sealed sum' 관용구(RoutingDecision — 타입 자체가 판별자)에서 약하게 이탈한다. MVP 허용
# (두 종류·단순 ref), ReevalSubject sealed sum 분리는 후속.

SubjectKind = Literal["precedent", "answer"]
ReevalStatus = Literal["pending_review", "reviewed"]


@dataclass(frozen=True)
class ReevalItem:
    """OKF 변경이 stale로 표식한 한 건의 재평가 대기 항목(ADR 0019 결정 3).

    `subject_kind`(precedent/answer) · `subject_ref`(precedent=intent 키 / answer=audit
    기록순 인덱스 문자열) · `owner_id`(처리함 귀속 키 — Precedent=primary 카드 owner /
    Answer=`rec["decision"]["owner"]`) · `agent_id`(어느 번들 변경) · `trigger_sha`(이 변경을
    부른 커밋 = `event.new_sha`) · `flagged_at`(주입 clock 결정론) · `status` · `review`
    (reviewed일 때만 — ReevalOutcome) · `item_id`(uuid).

    pending_review → reviewed 전이는 `review_with()`가 item_id 보존한 새 인스턴스를
    돌려준다(파괴적 변경 X — BackupReviewItem.review_with()·ConflictCase.resolve() 정신).
    """

    subject_kind: SubjectKind
    subject_ref: str
    owner_id: str
    agent_id: str
    trigger_sha: str
    flagged_at: datetime
    item_id: str = field(default_factory=_new_item_id)
    status: ReevalStatus = "pending_review"
    review: ReevalOutcome | None = None

    def review_with(self, review: ReevalOutcome) -> "ReevalItem":
        """재평가 처분을 안은 reviewed 항목을 새로 만든다(item_id 보존, 불변 + 새 인스턴스).

        BackupReviewItem.review_with()와 같은 전이 — 파괴적 변경 X.
        """
        import dataclasses
        return dataclasses.replace(self, status="reviewed", review=review)


# ── 재평가 대상 보관 포트: ReevalStore ────────────────────────────────────
#
# ConflictCaseStore·BackupReviewStore·ManagerQueueStore와 같은 패턴(Protocol + InMemory,
# owner 색인)의 네 번째 인스턴스. `pending_for_owner` = open_for_owner/pending_for_owner
# 동형 = owner 처리함의 세 번째 탭. 전이 ≠ 기록.


class ReevalStore(Protocol):
    """stale 재평가 항목 보관·조회 포트 — owner 처리함 세 번째 면의 데이터 원천.

    `pending_for_owner`가 owner 복귀 시 "내가 재평가할 stale 판례·답들" 조회.
    `mark_reviewed`가 pending_review→reviewed 상태 전이 기록.
    BackupReviewStore와 100% 동형(담는 값만 다름).
    """

    def add(self, item: ReevalItem) -> None: ...

    def get(self, item_id: str) -> ReevalItem | None: ...

    def pending_for_owner(self, owner_id: str) -> list[ReevalItem]: ...

    def mark_reviewed(self, item: ReevalItem) -> None: ...


class InMemoryReevalStore:
    """append-only 정신의 in-memory 재평가 저장소.

    pending 항목은 `_pending`(item_id 색인)에 둔다. reviewed되면 `_pending`에서 빼
    `history`(append-only)에 결말을 남긴다 — 처리함 목록은 pending만, 이력은 전부.
    InMemoryBackupReviewStore와 같은 구조.
    """

    def __init__(self) -> None:
        self._pending: dict[str, ReevalItem] = {}
        self._all: dict[str, ReevalItem] = {}
        self.history: list[ReevalItem] = []

    def add(self, item: ReevalItem) -> None:
        self._pending[item.item_id] = item
        self._all[item.item_id] = item

    def get(self, item_id: str) -> ReevalItem | None:
        return self._all.get(item_id)

    def pending_for_owner(self, owner_id: str) -> list[ReevalItem]:
        return [item for item in self._pending.values() if item.owner_id == owner_id]

    def mark_reviewed(self, item: ReevalItem) -> None:
        self._pending.pop(item.item_id, None)
        self._all[item.item_id] = item
        self.history.append(item)


# ── 재평가 서비스: ReevalService ─────────────────────────────────────────
#
# owner의 재평가 처분(ReevalOutcome)을 받아 상태 전이를 수행하는 도메인 서비스.
# 1인칭 강제: item.owner_id == review.by_owner (BackupReviewService·ConsensusService 정신).
# 전이만 store에 반영, 재검토 행위 기록은 호출자(웹/ask_org)가 audit에 남긴다(전이≠기록).


class ReevalService:
    """재평가 처분을 받아 ReevalItem 상태를 전이시키는 도메인 서비스.

    1인칭 강제: 재평가는 그 owner만 — item.owner_id != review.by_owner면 ValueError
    (BackupReviewService가 by_owner를 강제하듯). 전이는 store에 반영, audit 기록은 호출자.
    """

    def __init__(self, store: ReevalStore) -> None:
        self._store = store

    def review(self, item_id: str, review: ReevalOutcome) -> ReevalItem:
        """재평가 처분을 적용해 reviewed 항목을 돌려준다.

        1인칭 검증: review.by_owner가 item.owner_id여야 한다(타인 처리 금지).
        item_id 미존재 → ValueError. 이미 reviewed면 그대로(멱등).
        순서: item None 검사 → 1인칭 검증 → 멱등(reviewed면 return) → 전이.
        """
        item = self._store.get(item_id)
        if item is None:
            raise ValueError(f"미존재 item_id: {item_id!r}")
        if review.by_owner != item.owner_id:
            raise ValueError(
                f"1인칭 위반: {review.by_owner!r}는 {item.owner_id!r}의 항목을 처리할 수 없습니다."
            )
        if item.status == "reviewed":
            return item
        reviewed = item.review_with(review)
        self._store.mark_reviewed(reviewed)
        return reviewed


# ── 변경 전파기: StalenessPropagator ─────────────────────────────────────
#
# OKF 커밋 변경 이벤트(OkfChangeEvent)를 받아 영향받는 과거 Precedent·답을 식별하고
# 재평가 큐에 적재하는 도메인 서비스(ADR 0019 결정 2·3). agent_id 단위 거친 매칭
# (과검출 허용·놓침 0). 새 통지 인프라 0 — 적재가 곧 처리함 nudge(결정 5).


class StalenessPropagator:
    """OKF 커밋 변경을 받아 영향 Precedent·답을 stale 표식·재평가 적재하는 서비스.

    `commit_okf_bundle`(git_gateway.py)이 커밋 직후 `on_okf_committed(event)`를 1회 호출한다.

    영향 식별(ADR 0019 결정 2 — agent_id 거친 매칭, 과검출 허용·놓침 0):
      ① Precedent 축: `precedents.find_by_primary(event.agent_id)`로 그 agent를 primary로
         둔 판례 전부. `flag_stale`로 needs_review 표식 + ReevalItem(subject_kind="precedent",
         subject_ref=intent, owner_id=primary 카드 owner) 적재.
      ② Answer 축: `audit_reader.records()` 순회 — records()는 **직렬화 dict**다(AuditEntry
         객체 아님·audit.py). dict 접근으로 가린다:
           - `rec["decision"]["disposition"] == "routed"` and `rec["decision"]["primary"] ==
             event.agent_id`.
           - 답 SHA = `(rec.get("answer") or {}).get("snapshot_sha")` — **_answer_record는
             snapshot_sha가 None이면 키 자체를 안 넣는다**(audit.py)라 .get으로 안전 접근.
           - 그 SHA가 *현 HEAD와 다르거나 None(키 부재 포함)* 이면 영향(보수 — None 답도 포함,
             과검출이지만 누락 없음). ordering 판정은 MVP 포기(불투명 SHA older-than 비교 불가).
         영향 답마다 ReevalItem(subject_kind="answer", subject_ref=audit 인덱스,
         owner_id=`rec["decision"]["owner"]`) 적재.

    `owner_of`는 agent_id → owner 콜백(Precedent 축 owner 귀속용 — primary 카드 owner를
    Registry에서 얻는 자리, `manager_of` 정신). 미주입이면 None 안전 처리.
    """

    def __init__(
        self,
        precedents: "PrecedentStore",
        audit_reader: "AuditReader",
        reeval_store: ReevalStore,
        owner_of: Callable[[str], str | None] | None = None,
        clock: Clock = default_clock,
    ) -> None:
        self._precedents = precedents
        self._audit_reader = audit_reader
        self._reeval_store = reeval_store
        self._owner_of = owner_of
        self._clock = clock

    def on_okf_committed(self, event: "OkfChangeEvent") -> None:
        """변경 이벤트로 영향 Precedent·답을 식별·표식·적재한다(ADR 0019 결정 2·3)."""
        now = self._clock()

        # ① Precedent 축: agent_id 역색인으로 영향 판례 찾기
        affected_precedents = self._precedents.find_by_primary(event.agent_id)
        for precedent in affected_precedents:
            if precedent.needs_review:
                continue
            intent = precedent.resolution.intent
            self._precedents.flag_stale(intent, trigger_sha=event.new_sha, at=now)
            owner_id = (
                self._owner_of(event.agent_id)
                if self._owner_of is not None
                else None
            ) or ""
            self._reeval_store.add(
                ReevalItem(
                    subject_kind="precedent",
                    subject_ref=intent,
                    owner_id=owner_id,
                    agent_id=event.agent_id,
                    trigger_sha=event.new_sha,
                    flagged_at=now,
                )
            )

        # ② Answer 축: audit 순회 — dict 접근 (ADR 0019 결정 2② 주의)
        records = self._audit_reader.records()
        for idx, rec in enumerate(records):
            decision: dict[str, Any] = cast(dict[str, Any], rec.get("decision") or {})
            if decision.get("disposition") != "routed":
                continue
            if decision.get("primary") != event.agent_id:
                continue
            answer_rec: dict[str, Any] = cast(dict[str, Any], rec.get("answer") or {})
            snapshot_sha: str | None = cast("str | None", answer_rec.get("snapshot_sha"))
            if snapshot_sha == event.new_sha:
                continue
            owner_id_raw: str = cast(str, decision.get("owner") or "")
            subject_ref = str(idx)
            # dedup 가드: 같은 (subject_kind="answer", subject_ref)가 이미 pending이면 skip
            pending = self._reeval_store.pending_for_owner(owner_id_raw)
            already_queued = any(
                p.subject_kind == "answer" and p.subject_ref == subject_ref
                for p in pending
            )
            if already_queued:
                continue
            self._reeval_store.add(
                ReevalItem(
                    subject_kind="answer",
                    subject_ref=subject_ref,
                    owner_id=owner_id_raw,
                    agent_id=event.agent_id,
                    trigger_sha=event.new_sha,
                    flagged_at=now,
                )
            )
