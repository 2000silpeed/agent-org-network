"""T6.6 슬라이스 iii — owner 복귀 검토 루프 단위 테스트 (ADR 0012 결정 7).

전부 결정론: 고정 clock, InMemory store, 실 claude·실 네트워크·실 프로세스 0.

커버 범위:
  - BackupReviewItem: 값 객체·불변·review_with() 전이(새 인스턴스·item_id 보존)
  - BackupReviewStore(InMemory): add→pending_for_owner→mark_reviewed·get·owner 격리
  - BackupReview 1인칭: by_owner≠item.owner_id면 ValueError
  - BackupReviewService: 검토 전이(pending→reviewed)·이미 reviewed 멱등·미존재 ValueError
  - Precedent 안 만듦: 검토는 라우팅 판례가 아님(store만 이 테스트로 보장)
"""

import pytest
from datetime import datetime, timezone

from agent_org_network.review import (
    ApproveBackup,
    BackupReviewItem,
    BackupReviewService,
    CorrectBackup,
    DismissBackup,
    InMemoryBackupReviewStore,
)


# ── 픽스처 ────────────────────────────────────────────────────────────────

FIXED_TS = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
SNAPSHOT_TS = datetime(2026, 6, 20, 8, 0, 0, tzinfo=timezone.utc)


def _item(
    owner_id: str = "alice",
    agent_id: str = "cs_ops",
    ticket_id: str = "ticket-001",
    item_id: str = "item-001",
) -> BackupReviewItem:
    return BackupReviewItem(
        owner_id=owner_id,
        agent_id=agent_id,
        question="환불 되나요?",
        backup_answer_text="백업이 낸 환불 안내 답변입니다.",
        ticket_id=ticket_id,
        snapshot_at=SNAPSHOT_TS,
        answered_at=FIXED_TS,
        item_id=item_id,
    )


# ── BackupReviewItem 단위 ─────────────────────────────────────────────────


def test_BackupReviewItem이_frozen이다():
    item = _item()
    with pytest.raises((AttributeError, TypeError)):
        item.owner_id = "other"  # type: ignore[misc]


def test_BackupReviewItem_기본_status가_pending_review다():
    item = _item()
    assert item.status == "pending_review"
    assert item.review is None


def test_review_with가_reviewed_새_인스턴스를_돌려준다():
    item = _item(item_id="item-abc")
    review = ApproveBackup(by_owner="alice")
    reviewed = item.review_with(review)

    assert reviewed is not item
    assert reviewed.item_id == "item-abc"
    assert reviewed.status == "reviewed"
    assert reviewed.review == review


def test_review_with가_원본을_불변으로_남긴다():
    item = _item()
    _ = item.review_with(ApproveBackup(by_owner="alice"))
    assert item.status == "pending_review"
    assert item.review is None


def test_review_with_CorrectBackup이_정정_텍스트를_보존한다():
    item = _item()
    review = CorrectBackup(
        by_owner="alice",
        corrected_text="정정된 환불 안내",
        sources=("위키/환불정책",),
    )
    reviewed = item.review_with(review)
    assert isinstance(reviewed.review, CorrectBackup)
    assert reviewed.review.corrected_text == "정정된 환불 안내"
    assert reviewed.review.sources == ("위키/환불정책",)


def test_review_with_DismissBackup이_이유를_보존한다():
    item = _item()
    review = DismissBackup(by_owner="alice", rationale="이미 사용자가 됐음")
    reviewed = item.review_with(review)
    assert isinstance(reviewed.review, DismissBackup)
    assert reviewed.review.rationale == "이미 사용자가 됐음"


# ── InMemoryBackupReviewStore 단위 ────────────────────────────────────────


def test_add_후_get으로_조회된다():
    store = InMemoryBackupReviewStore()
    item = _item(item_id="item-001")
    store.add(item)
    assert store.get("item-001") == item


def test_get_없는_item_id는_None():
    store = InMemoryBackupReviewStore()
    assert store.get("nonexistent") is None


def test_pending_for_owner가_해당_owner_pending만_돌려준다():
    store = InMemoryBackupReviewStore()
    alice_item = _item(owner_id="alice", ticket_id="t-alice", item_id="item-alice")
    bob_item = _item(owner_id="bob", ticket_id="t-bob", item_id="item-bob")
    store.add(alice_item)
    store.add(bob_item)

    alice_pending = store.pending_for_owner("alice")
    bob_pending = store.pending_for_owner("bob")

    assert len(alice_pending) == 1
    assert alice_pending[0].owner_id == "alice"
    assert len(bob_pending) == 1
    assert bob_pending[0].owner_id == "bob"


def test_pending_for_owner_없으면_빈_리스트():
    store = InMemoryBackupReviewStore()
    assert store.pending_for_owner("unknown") == []


def test_mark_reviewed_후_pending_for_owner에서_사라진다():
    store = InMemoryBackupReviewStore()
    item = _item(item_id="item-001")
    store.add(item)

    reviewed = item.review_with(ApproveBackup(by_owner="alice"))
    store.mark_reviewed(reviewed)

    assert store.pending_for_owner("alice") == []


def test_mark_reviewed_후_history에_reviewed_항목이_남는다():
    store = InMemoryBackupReviewStore()
    item = _item(item_id="item-001")
    store.add(item)

    reviewed = item.review_with(ApproveBackup(by_owner="alice"))
    store.mark_reviewed(reviewed)

    assert any(h.status == "reviewed" for h in store.history)


def test_mark_reviewed_후_get으로_reviewed_항목_조회된다():
    store = InMemoryBackupReviewStore()
    item = _item(item_id="item-001")
    store.add(item)

    reviewed = item.review_with(ApproveBackup(by_owner="alice"))
    store.mark_reviewed(reviewed)

    result = store.get("item-001")
    assert result is not None
    assert result.status == "reviewed"


def test_get_by_ticket으로_ticket_id_조회된다():
    store = InMemoryBackupReviewStore()
    item = _item(ticket_id="ticket-xyz", item_id="item-xyz")
    store.add(item)
    assert store.get_by_ticket("ticket-xyz") == item


def test_get_by_ticket_없으면_None():
    store = InMemoryBackupReviewStore()
    assert store.get_by_ticket("nonexistent") is None


def test_owner_격리_다른_owner_pending에_안_섞인다():
    store = InMemoryBackupReviewStore()
    items = [
        _item(owner_id="alice", ticket_id=f"t-alice-{i}", item_id=f"item-alice-{i}")
        for i in range(3)
    ]
    bob_item = _item(owner_id="bob", ticket_id="t-bob", item_id="item-bob")
    for it in items:
        store.add(it)
    store.add(bob_item)

    assert len(store.pending_for_owner("alice")) == 3
    assert len(store.pending_for_owner("bob")) == 1


# ── BackupReviewService 단위 ──────────────────────────────────────────────


def test_BackupReviewService_Approve_전이():
    store = InMemoryBackupReviewStore()
    service = BackupReviewService(store)
    item = _item(owner_id="alice", item_id="item-001")
    store.add(item)

    reviewed = service.review("item-001", ApproveBackup(by_owner="alice"))

    assert reviewed.status == "reviewed"
    assert isinstance(reviewed.review, ApproveBackup)


def test_BackupReviewService_Correct_전이():
    store = InMemoryBackupReviewStore()
    service = BackupReviewService(store)
    item = _item(owner_id="alice", item_id="item-001")
    store.add(item)

    review = CorrectBackup(by_owner="alice", corrected_text="정정된 답변입니다.")
    reviewed = service.review("item-001", review)

    assert reviewed.status == "reviewed"
    assert isinstance(reviewed.review, CorrectBackup)
    assert reviewed.review.corrected_text == "정정된 답변입니다."


def test_BackupReviewService_Dismiss_전이():
    store = InMemoryBackupReviewStore()
    service = BackupReviewService(store)
    item = _item(owner_id="alice", item_id="item-001")
    store.add(item)

    reviewed = service.review("item-001", DismissBackup(by_owner="alice"))

    assert reviewed.status == "reviewed"
    assert isinstance(reviewed.review, DismissBackup)


def test_BackupReviewService_1인칭_검증_타인_검토_ValueError():
    store = InMemoryBackupReviewStore()
    service = BackupReviewService(store)
    item = _item(owner_id="alice", item_id="item-001")
    store.add(item)

    with pytest.raises(ValueError, match="alice"):
        service.review("item-001", ApproveBackup(by_owner="bob"))


def test_BackupReviewService_미존재_item_id_ValueError():
    store = InMemoryBackupReviewStore()
    service = BackupReviewService(store)

    with pytest.raises(ValueError, match="미존재"):
        service.review("nonexistent", ApproveBackup(by_owner="alice"))


def test_BackupReviewService_이미_reviewed면_멱등():
    store = InMemoryBackupReviewStore()
    service = BackupReviewService(store)
    item = _item(owner_id="alice", item_id="item-001")
    store.add(item)

    first = service.review("item-001", ApproveBackup(by_owner="alice"))
    second = service.review("item-001", ApproveBackup(by_owner="alice"))

    assert first.status == "reviewed"
    assert second.status == "reviewed"
