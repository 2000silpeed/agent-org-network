"""owner 복귀 검토 루프 — BackupReviewItem + BackupReviewStore + BackupReview (T6.6 슬라이스 iii, ADR 0012 결정 7).

백업 워커가 owner 이름으로 낸 미검토 답(mode=backup)을 owner가 복귀 후
검토·정정·승격하는 루프다. ConflictCase/ConflictCaseStore 패턴의 두 번째 인스턴스 —
"미해소 다툼" 대신 "미검토 백업 답"을 owner별 처리함에 보관한다.

도메인 위치: 검토는 *다툼 도메인(conflict.py)이 아니라* 답 가용성/책임 도메인이라
별 모듈(review.py)로 분리했다. 패턴은 100% 재사용(Protocol + InMemory, owner 색인,
불변 전이).

전이 ≠ 기록:
  - `BackupReviewStore`(이 모듈) — 미검토 상태의 도메인 보관소(전이).
  - `AuditLog`(`audit.py`) — 검토 행위의 절차 기록(기록).
  둘은 서로 다른 책임이라 분리한다.

Precedent 안 만듦(ADR 0012 결정 7-5):
  검토는 "그 답이 맞나"의 판단이지 "누가 담당인가"의 판단이 아니다. 담당은
  이미 그 owner로 정해졌으므로 Precedent(라우팅 판례)를 건드리지 않는다.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol

Clock = Callable[[], datetime]


def default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_item_id() -> str:
    return uuid.uuid4().hex


# ── 검토 결과: BackupReview sealed sum ────────────────────────────────────
#
# owner의 검토 행위는 셋 중 하나(ConsensusOutcome 정신 — "타입이 곧 상태").
# by_owner 1인칭 — 검토는 그 owner만 할 수 있다(처리함이 owner 귀속).
# 검토 서비스가 item.owner_id == review.by_owner를 강제한다
# (ConsensusService가 후보 owner를 강제하는 것과 같은 정신).


@dataclass(frozen=True)
class ApproveBackup:
    """owner가 백업 답을 승인 — '이대로 맞다'. retrieve 재노출 시 mode=full 승격."""

    by_owner: str
    rationale: str = ""


@dataclass(frozen=True)
class CorrectBackup:
    """owner가 정정 — 새 답을 발행한다. 기존 backup 답을 대체할 owner 실답.

    retrieve 재노출 시 정정 text + mode=full로 투영된다(신뢰 복원).
    answered_by는 여전히 owner(정정도 owner가 발행함 — answered_by 불변식).
    """

    by_owner: str
    corrected_text: str
    sources: tuple[str, ...] = ()
    rationale: str = ""


@dataclass(frozen=True)
class DismissBackup:
    """owner가 무시 — '검토했고 따로 조치 안 함'.

    검토 완료 사실은 남는다(미검토와 구분) — 책임 실질화의 최소선.
    retrieve 재노출은 큐의 원 backup 답 그대로(mode=backup 유지 — 정정 없음).
    """

    by_owner: str
    rationale: str = ""


BackupReview = ApproveBackup | CorrectBackup | DismissBackup
#   owner 검토 행위의 sealed sum(세 결말의 망라).


# ── 검토 대상 보관 단위: BackupReviewItem ─────────────────────────────────
#
# 백업이 owner 이름으로 답한 한 건의 검토 대기 항목. ConflictCase가 미해소 다툼을
# 담듯, 이건 *미검토 백업 답*을 담는다 — Owner 처리함(Inbox)의 두 번째 면.
# open → reviewed 전이는 `review_with()`가 새 인스턴스를 돌려준다(불변 + 새 인스턴스,
# ConflictCase.resolve()와 같은 정신).

ReviewStatus = Literal["pending_review", "reviewed"]


@dataclass(frozen=True)
class BackupReviewItem:
    """백업이 owner 이름으로 답한 한 건의 검토 대기 항목.

    owner가 복귀해 보고·정정·승격할 대상. 답 본문(backup_answer_text)을 보관해
    owner가 "백업이 내 이름으로 뭐라 답했나"를 보고 정정 판단을 내린다
    (ConflictCase가 question 원문을 보관하는 것과 같은 정신).

    item_id = ticket_id 재사용(1 답 1 검토 — 별 ID 불요). 주입 clock 결정론.
    """

    owner_id: str
    agent_id: str
    question: str
    backup_answer_text: str
    ticket_id: str
    snapshot_at: datetime
    answered_at: datetime
    item_id: str = field(default_factory=_new_item_id)
    status: ReviewStatus = "pending_review"
    review: BackupReview | None = None

    def review_with(self, review: BackupReview) -> "BackupReviewItem":
        """검토 결과를 안은 reviewed 항목을 새로 만든다(item_id 보존, 불변 + 새 인스턴스).

        ConflictCase.resolve()와 같은 전이 — 파괴적 변경 X.
        """
        return BackupReviewItem(
            owner_id=self.owner_id,
            agent_id=self.agent_id,
            question=self.question,
            backup_answer_text=self.backup_answer_text,
            ticket_id=self.ticket_id,
            snapshot_at=self.snapshot_at,
            answered_at=self.answered_at,
            item_id=self.item_id,
            status="reviewed",
            review=review,
        )


# ── 검토 대상 보관 포트: BackupReviewStore ────────────────────────────────
#
# ConflictCaseStore와 같은 패턴(Protocol + InMemory, owner 색인).
# `pending_for_owner` = open_for_owner 동형 = owner 처리함의 두 번째 탭.
# 전이 ≠ 기록 — 여긴 미검토 도메인 상태를 보관하는 곳이지 절차 기록(AuditLog)이 아니다.


class BackupReviewStore(Protocol):
    """미검토 백업 답 보관·조회 포트 — owner 처리함 두 번째 면의 데이터 원천.

    `pending_for_owner`가 owner 복귀 시 "내가 검토할 백업 답들" 조회.
    `mark_reviewed`가 pending_review→reviewed 상태 전이 기록.
    `get_by_ticket`은 retrieve 덧씌움이 tracking→ticket→검토 조회에 쓴다.
    """

    def add(self, item: BackupReviewItem) -> None: ...

    def get(self, item_id: str) -> BackupReviewItem | None: ...

    def get_by_ticket(self, ticket_id: str) -> BackupReviewItem | None: ...

    def pending_for_owner(self, owner_id: str) -> list[BackupReviewItem]: ...

    def mark_reviewed(self, item: BackupReviewItem) -> None:
        """pending_review → reviewed 전이를 기록한다.

        계약: `item`은 `status="reviewed"`·`review`가 채워진 인스턴스여야 한다
        (`review_with()`로 생성, 이미 reviewed인지 검사는 *호출자(BackupReviewService)*가
        담당한다 — 이미 reviewed인 item을 넘기지 않도록 `BackupReviewService.review`가
        사전 검증한다. 이 메서드는 멱등을 *보장하지 않아도 되며*, 이중 전이 방지는
        서비스 계층의 책임이다).
        """
        ...


class InMemoryBackupReviewStore:
    """append-only 정신의 in-memory 검토 저장소.

    pending 항목은 `_pending`(item_id 색인)에 둔다. reviewed되면 `_pending`에서 빼
    `history`(append-only)에 결말을 남긴다 — 처리함 목록은 pending만, 이력은 전부.
    `_by_ticket`(ticket_id 색인)은 retrieve 덧씌움이 ticket_id로 검색할 때 쓴다
    (큐 도메인을 건드리지 않고 검토 store만 조회).
    """

    def __init__(self) -> None:
        self._pending: dict[str, BackupReviewItem] = {}
        self._all: dict[str, BackupReviewItem] = {}       # item_id → 최신 상태(pending/reviewed 공통)
        self._by_ticket: dict[str, BackupReviewItem] = {} # ticket_id → 최신 상태
        self.history: list[BackupReviewItem] = []

    def add(self, item: BackupReviewItem) -> None:
        self._pending[item.item_id] = item
        self._all[item.item_id] = item
        self._by_ticket[item.ticket_id] = item
        self.history.append(item)

    def get(self, item_id: str) -> BackupReviewItem | None:
        return self._all.get(item_id)

    def get_by_ticket(self, ticket_id: str) -> BackupReviewItem | None:
        """ticket_id로 검색 — retrieve 덧씌움이 tracking→ticket→검토 조회에 쓴다."""
        return self._by_ticket.get(ticket_id)

    def pending_for_owner(self, owner_id: str) -> list[BackupReviewItem]:
        return [item for item in self._pending.values() if item.owner_id == owner_id]

    def mark_reviewed(self, item: BackupReviewItem) -> None:
        self._pending.pop(item.item_id, None)
        self._all[item.item_id] = item
        self._by_ticket[item.ticket_id] = item
        self.history.append(item)


# ── 검토 서비스: BackupReviewService ─────────────────────────────────────
#
# owner의 검토 행위(BackupReview)를 받아 상태 전이를 수행하는 도메인 서비스.
# 1인칭 강제: item.owner_id == review.by_owner (ConsensusService와 같은 정신).
# 검토 전이는 store에 반영, 감사 기록은 호출자(웹/ask_org)가 audit에 남긴다(전이≠기록).


class BackupReviewService:
    """검토 행위를 받아 BackupReviewItem 상태를 전이시키는 도메인 서비스.

    1인칭 강제: 검토는 그 owner만 할 수 있다 — item.owner_id != review.by_owner면
    ValueError(ConsensusService가 후보 owner가 아닌 by_owner를 거부하듯).
    Precedent는 만들지 않는다(검토≠라우팅 판례 — ADR 0012 결정 7-5).
    """

    def __init__(self, store: BackupReviewStore) -> None:
        self._store = store

    def review(self, item_id: str, review: BackupReview) -> BackupReviewItem:
        """검토 결과를 적용해 reviewed 항목을 돌려준다.

        1인칭 검증: review.by_owner가 item.owner_id여야 한다(타인 처리 금지).
        item_id 미존재 → ValueError.
        이미 reviewed면 상태 그대로 돌려준다(멱등).
        """
        item = self._store.get(item_id)
        if item is None:
            raise ValueError(f"미존재 검토 항목: {item_id!r}")
        if review.by_owner != item.owner_id:
            raise ValueError(
                f"검토자({review.by_owner!r})가 항목 owner({item.owner_id!r})와 다름 — "
                "자기 처리함만 검토할 수 있다"
            )
        if item.status == "reviewed":
            return item
        reviewed = item.review_with(review)
        self._store.mark_reviewed(reviewed)
        return reviewed
