"""T6.6 슬라이스 iii — 검토 루프 통합 테스트 (ADR 0012 결정 7).

생성 트리거·retrieve 덧씌움·audit·web 왕복을 결정론으로 검증한다.
전부 결정론: FakeRuntime, 고정 clock, InMemory store, 실 claude 0.

커버 범위:
  - 생성 트리거: backup 답 종착 시 BackupReviewStore.add 자동 호출.
               mode=full 답은 검토 항목 생성 안 함.
  - retrieve 덧씌움: CorrectBackup 후 retrieve가 정정 답(answered_by=owner, mode=full).
                  ApproveBackup 후 retrieve가 원 text + mode=full.
                  DismissBackup 후 retrieve가 큐의 원 backup 답 그대로(mode=backup 유지).
                  미검토 tracking은 큐 poll 그대로(mode=backup).
  - 큐 멱등 보존: 정정이 큐 answered 상태를 바꾸지 않는다.
  - audit: 검토 사후 줄 기록(전이 ≠ 기록).
  - web: GET /inbox/{owner_id}/backup-reviews(pending 조회),
         POST /backup-reviews/{item_id}(검토 HTTP 왕복).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import AskOrg, Answered
from agent_org_network.audit import AuditLog, InMemoryAuditLog
from agent_org_network.conflict import InMemoryPrecedentStore
from agent_org_network.dispatch import (
    DelegationSnapshot,
    Delivered,
    InMemoryWorkQueueDispatcher,
)
from agent_org_network.review import (
    ApproveBackup,
    BackupReviewItem,
    BackupReviewService,
    CorrectBackup,
    DismissBackup,
    InMemoryBackupReviewStore,
)
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import Answer, StubRuntime
from agent_org_network.transport import RegisterWorker, WebSocketDispatcher
from agent_org_network.user import User


# ── pyright strict: starlette TestClient httpx 메서드 반환 Unknown 회피 ─────

@dataclass(frozen=True)
class HttpResult:
    status: int
    body: Any


def _result(res: Response) -> HttpResult:
    body: Any = res.json()
    return HttpResult(status=res.status_code, body=body)


def _get(client: TestClient, url: str) -> HttpResult:
    http: Any = client
    return _result(cast(Response, http.get(url)))


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> HttpResult:
    http: Any = client
    return _result(cast(Response, http.post(url, json=payload)))


# ── 공통 픽스처 ───────────────────────────────────────────────────────────

BASE_TS = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
SNAPSHOT_TS = datetime(2026, 6, 20, 8, 0, 0, tzinfo=timezone.utc)


def _fixed_clock(ts: datetime = BASE_TS):  # type: ignore[no-untyped-def]
    return lambda: ts


def _card(owner: str = "alice", agent_id: str = "cs_ops") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="cs",
        summary="고객 지원 담당",
        domains=["cs"],
        last_reviewed_at=date(2026, 6, 20),
    )


class _FakeClassifier:
    def classify(self, question: str) -> str:
        return "cs"


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def __call__(self, frame: Any) -> None:
        self.sent.append(frame)


# ── 생성 트리거: backup submit → BackupReviewStore.add ──────────────────


def _make_ws_dispatcher_with_review_store(
    owner_id: str = "alice",
    agent_id: str = "cs_ops",
    snapshot_at: datetime = SNAPSHOT_TS,
    staleness_threshold: timedelta = timedelta(days=7),
) -> tuple[WebSocketDispatcher, InMemoryBackupReviewStore]:
    clock = _fixed_clock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(clock=clock)
    review_store = InMemoryBackupReviewStore()
    disp = WebSocketDispatcher(
        clock=clock,
        queue=queue,
        staleness_threshold=staleness_threshold,
        review_store=review_store,
    )
    snapshot = DelegationSnapshot(
        owner_id=owner_id,
        agent_ids=(agent_id,),
        snapshot_at=snapshot_at,
    )
    disp.register_delegation(snapshot)
    return disp, review_store


def test_backup_submit_시_검토_항목_생성된다() -> None:
    disp, review_store = _make_ws_dispatcher_with_review_store()
    card = _card()
    rec = _Recorder()

    disp.register(RegisterWorker(owner_id="alice", role="backup"), rec)
    ticket = disp.dispatch("환불 되나요?", card)
    disp.submit(ticket.ticket_id, Answer(text="백업 답변", mode="backup"))

    items = review_store.pending_for_owner("alice")
    assert len(items) == 1
    assert items[0].owner_id == "alice"
    assert items[0].agent_id == "cs_ops"
    assert items[0].question == "환불 되나요?"
    assert items[0].backup_answer_text == "백업 답변"
    assert items[0].ticket_id == ticket.ticket_id


def test_full_모드_submit은_검토_항목_생성_안_함() -> None:
    clock = _fixed_clock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(clock=clock)
    review_store = InMemoryBackupReviewStore()
    disp = WebSocketDispatcher(clock=clock, queue=queue, review_store=review_store)
    card = _card()
    rec = _Recorder()

    disp.register(RegisterWorker(owner_id="alice", role="primary"), rec)
    ticket = disp.dispatch("환불 되나요?", card)
    disp.submit(ticket.ticket_id, Answer(text="실시간 답변", mode="full"))

    assert review_store.pending_for_owner("alice") == []


def test_backup_submit_review_store_없이도_동작한다() -> None:
    clock = _fixed_clock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(clock=clock)
    disp = WebSocketDispatcher(clock=clock, queue=queue)
    card = _card()
    rec = _Recorder()

    disp.register(RegisterWorker(owner_id="alice", role="backup"), rec)
    ticket = disp.dispatch("환불 되나요?", card)
    disp.submit(ticket.ticket_id, Answer(text="백업 답변"))


# ── retrieve 덧씌움 ───────────────────────────────────────────────────────


def _make_ask_org_with_review(
    disp: WebSocketDispatcher,
    review_store: InMemoryBackupReviewStore,
    audit: AuditLog | None = None,
) -> AskOrg:
    registry = Registry()
    registry.register_user(User(id="root"))
    registry.register_user(User(id="alice", manager="root"))
    registry.register(_card(owner="alice"))
    registry.validate()

    precedents = InMemoryPrecedentStore()
    router = Router(registry, _FakeClassifier(), root_user="root", precedents=precedents)

    return AskOrg(
        router=router,
        dispatcher=disp,
        audit_log=audit or InMemoryAuditLog(),
        clock=_fixed_clock(BASE_TS),
        review_store=review_store,
    )


def _setup_backup_and_submit(
    disp: WebSocketDispatcher,
    ask: AskOrg,
    review_store: InMemoryBackupReviewStore,
    question: str = "환불 되나요?",
    answer_text: str = "백업 답변",
) -> tuple[str, BackupReviewItem]:
    """backup 워커 등록 → 질문 → submit → tracking + review item 반환."""
    rec = _Recorder()
    disp.register(RegisterWorker(owner_id="alice", role="backup"), rec)
    user = User(id="web_guest")
    pending = ask.handle(question, user)
    tracking: str = pending.tracking  # type: ignore[union-attr]
    tracking_map: dict[str, Any] = ask._tracking  # type: ignore[reportPrivateUsage]
    ticket = tracking_map[tracking]
    disp.submit(ticket.ticket_id, Answer(text=answer_text))
    item = review_store.pending_for_owner("alice")[0]
    return tracking, item


def test_CorrectBackup_후_retrieve가_정정_답을_돌려준다() -> None:
    disp, review_store = _make_ws_dispatcher_with_review_store()
    ask = _make_ask_org_with_review(disp, review_store)

    tracking, item = _setup_backup_and_submit(disp, ask, review_store)

    svc = BackupReviewService(review_store)
    svc.review(item.item_id, CorrectBackup(by_owner="alice", corrected_text="정정된 환불 안내"))

    result = ask.retrieve(tracking)
    assert isinstance(result, Answered)
    assert result.text == "정정된 환불 안내"
    assert result.mode == "full"
    assert result.answered_by == ("alice", "cs_ops")


def test_ApproveBackup_후_retrieve가_원_text에_mode_full을_돌려준다() -> None:
    disp, review_store = _make_ws_dispatcher_with_review_store()
    ask = _make_ask_org_with_review(disp, review_store)

    tracking, item = _setup_backup_and_submit(disp, ask, review_store)

    svc = BackupReviewService(review_store)
    svc.review(item.item_id, ApproveBackup(by_owner="alice"))

    result = ask.retrieve(tracking)
    assert isinstance(result, Answered)
    assert result.text == "백업 답변"
    assert result.mode == "full"


def test_DismissBackup_후_retrieve가_큐_원_backup_답을_돌려준다() -> None:
    disp, review_store = _make_ws_dispatcher_with_review_store()
    ask = _make_ask_org_with_review(disp, review_store)

    tracking, item = _setup_backup_and_submit(disp, ask, review_store)

    svc = BackupReviewService(review_store)
    svc.review(item.item_id, DismissBackup(by_owner="alice"))

    result = ask.retrieve(tracking)
    assert isinstance(result, Answered)
    assert result.text == "백업 답변"
    assert result.mode == "backup"


def test_미검토_tracking_retrieve는_큐_poll_그대로() -> None:
    disp, review_store = _make_ws_dispatcher_with_review_store()
    ask = _make_ask_org_with_review(disp, review_store)

    tracking, _item = _setup_backup_and_submit(disp, ask, review_store)

    result = ask.retrieve(tracking)
    assert isinstance(result, Answered)
    assert result.text == "백업 답변"
    assert result.mode == "backup"


def test_큐_멱등_보존_정정이_큐_answered를_바꾸지_않는다() -> None:
    disp, review_store = _make_ws_dispatcher_with_review_store()
    ask = _make_ask_org_with_review(disp, review_store)

    tracking, item = _setup_backup_and_submit(disp, ask, review_store)

    svc = BackupReviewService(review_store)
    svc.review(item.item_id, CorrectBackup(by_owner="alice", corrected_text="정정 답변"))

    tracking_map: dict[str, Any] = ask._tracking  # type: ignore[reportPrivateUsage]
    ticket = tracking_map[tracking]
    queue_outcome = disp.poll(ticket)
    assert isinstance(queue_outcome, Delivered)
    assert queue_outcome.answer.text == "백업 답변"
    assert queue_outcome.answer.mode == "backup"


# ── audit: 검토는 audit이 아니라 store.history가 기록(전이 ≠ 기록) ──────────


def test_검토_후_audit에_사후_줄이_남지_않는다() -> None:
    """[Major 2] record_review는 audit에 아무것도 남기지 않는다.

    검토 기록은 BackupReviewStore.history(append-only 전이 보관소)가 담당하고,
    audit은 질문→라우팅→디스패치→답의 절차 기록 전용이다(전이≠기록, ADR 0012 결정 7).
    """
    audit = InMemoryAuditLog()
    disp, review_store = _make_ws_dispatcher_with_review_store()
    ask = _make_ask_org_with_review(disp, review_store, audit=audit)

    _tracking, item = _setup_backup_and_submit(disp, ask, review_store)

    entries_before = len(audit.entries)

    ask.record_review(item.item_id, ApproveBackup(by_owner="alice"))

    # audit에는 아무것도 추가되지 않아야 한다(검토 기록 = store.history 몫).
    assert len(audit.entries) == entries_before, (
        f"record_review가 audit 줄을 {len(audit.entries) - entries_before}개 추가했다 — "
        "검토 기록은 store.history 담당(ADR 0012 결정 7)"
    )


def test_검토_전이가_store_history에_남는다() -> None:
    """검토 전이는 BackupReviewStore.history에 reviewed 항목으로 남는다."""
    audit = InMemoryAuditLog()
    disp, review_store = _make_ws_dispatcher_with_review_store()
    ask = _make_ask_org_with_review(disp, review_store, audit=audit)

    _tracking, item = _setup_backup_and_submit(disp, ask, review_store)

    history_len_before = len(review_store.history)
    ask.record_review(item.item_id, ApproveBackup(by_owner="alice"))

    assert len(review_store.history) > history_len_before
    last = review_store.history[-1]
    assert last.status == "reviewed"
    assert last.owner_id == "alice"
    assert last.question == "환불 되나요?"


# ── web: 처리함 백업 검토 탭 ─────────────────────────────────────────────


def _make_review_app() -> tuple[TestClient, InMemoryBackupReviewStore, BackupReviewService]:
    from agent_org_network.web import create_app

    review_store = InMemoryBackupReviewStore()
    review_svc = BackupReviewService(review_store)

    app = create_app(
        runtime=StubRuntime(),
        review_store=review_store,
        review_service=review_svc,
    )
    client = TestClient(app, raise_server_exceptions=True)
    return client, review_store, review_svc


def _make_item(
    owner_id: str = "alice",
    agent_id: str = "cs_ops",
    ticket_id: str = "ticket-001",
    item_id: str = "item-001",
) -> BackupReviewItem:
    return BackupReviewItem(
        owner_id=owner_id,
        agent_id=agent_id,
        question="환불 되나요?",
        backup_answer_text="백업 답변",
        ticket_id=ticket_id,
        snapshot_at=SNAPSHOT_TS,
        answered_at=BASE_TS,
        item_id=item_id,
    )


def test_GET_inbox_backup_reviews_pending_조회() -> None:
    client, review_store, _ = _make_review_app()
    review_store.add(_make_item())

    r = _get(client, "/inbox/alice/backup-reviews")
    assert r.status == 200
    data: list[Any] = r.body
    assert len(data) == 1
    assert data[0]["item_id"] == "item-001"
    assert data[0]["owner_id"] == "alice"
    assert data[0]["question"] == "환불 되나요?"
    assert data[0]["backup_answer_text"] == "백업 답변"
    assert data[0]["status"] == "pending_review"


def test_GET_inbox_backup_reviews_다른_owner_격리() -> None:
    client, review_store, _ = _make_review_app()
    review_store.add(_make_item(owner_id="bob", ticket_id="t-bob", item_id="item-bob"))

    r = _get(client, "/inbox/alice/backup-reviews")
    assert r.status == 200
    assert r.body == []


def test_POST_backup_reviews_Approve_검토() -> None:
    client, review_store, _ = _make_review_app()
    review_store.add(_make_item())

    r = _post(client, "/backup-reviews/item-001", {"type": "approve", "by_owner": "alice", "rationale": "맞는 답변"})
    assert r.status == 200
    assert r.body["status"] == "reviewed"
    assert r.body["review"]["type"] == "approve"


def test_POST_backup_reviews_Correct_검토() -> None:
    client, review_store, _ = _make_review_app()
    review_store.add(_make_item())

    r = _post(
        client,
        "/backup-reviews/item-001",
        {"type": "correct", "by_owner": "alice", "corrected_text": "정정된 환불 안내입니다.", "sources": ["위키/환불정책"]},
    )
    assert r.status == 200
    assert r.body["status"] == "reviewed"
    assert r.body["review"]["type"] == "correct"
    assert r.body["review"]["corrected_text"] == "정정된 환불 안내입니다."


def test_POST_backup_reviews_Dismiss_검토() -> None:
    client, review_store, _ = _make_review_app()
    review_store.add(_make_item())

    r = _post(client, "/backup-reviews/item-001", {"type": "dismiss", "by_owner": "alice"})
    assert r.status == 200
    assert r.body["status"] == "reviewed"
    assert r.body["review"]["type"] == "dismiss"


def test_POST_backup_reviews_타인_검토_400() -> None:
    client, review_store, _ = _make_review_app()
    review_store.add(_make_item())

    r = _post(client, "/backup-reviews/item-001", {"type": "approve", "by_owner": "bob"})
    assert r.status == 400


def test_POST_backup_reviews_미존재_item_404() -> None:
    client, _, _ = _make_review_app()

    r = _post(client, "/backup-reviews/nonexistent", {"type": "approve", "by_owner": "alice"})
    assert r.status == 404
