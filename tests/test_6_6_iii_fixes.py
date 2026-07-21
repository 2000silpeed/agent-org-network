"""T6.6-iii legacy 검토 계약과 P17 질문 표면 분리를 검증한다.

[Major 1] legacy AskOrg 검토 동작은 단위 경계에 남고 P17 사용자 경로와 섞이지 않는다.
[Major 2] record_review 거짓 audit 제거: 검토 후 audit에 decision=Unowned 줄이 없어야 한다.
[Minor 1] _project_review_outcome match+assert_never 대칭 — 컴파일 타임 검사(pyright strict).
[Minor 2] mark_reviewed Protocol docstring 계약 명시 — 동작 변경 없음.

전부 결정론: FakeRuntime, 고정 clock, InMemory store, 실 claude·네트워크·프로세스 0.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.agent_card import AgentCard
from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.decision import Unowned
from agent_org_network.dispatch import (
    DelegationSnapshot,
    InMemoryWorkQueueDispatcher,
)
from agent_org_network.review import ApproveBackup, CorrectBackup, InMemoryBackupReviewStore
from agent_org_network.runtime import Answer, StubRuntime
from agent_org_network.transport import RegisterWorker, WebSocketDispatcher


# ── 공통 픽스처 ───────────────────────────────────────────────────────────

BASE_TS = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
SNAPSHOT_TS = datetime(2026, 6, 20, 8, 0, 0, tzinfo=timezone.utc)


def _fixed_clock(ts: datetime = BASE_TS):  # type: ignore[no-untyped-def]
    return lambda: ts


def _card(owner: str = "cs_lead", agent_id: str = "cs_ops") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="cs",
        summary="고객 지원 담당",
        domains=["cs"],
        last_reviewed_at=date(2026, 6, 20),
    )


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: Any


def _result(res: Response) -> HttpResult:
    body: Any = res.json()
    return HttpResult(status=res.status_code, body=body)


# ═══════════════════════════════════════════════════════════════════════════
# [Major 1] P17 사용자 경로는 legacy WS/review side effect와 분리돼야 한다
# ═══════════════════════════════════════════════════════════════════════════


def _make_ws_dispatcher(
    owner_id: str = "cs_lead",
    agent_id: str = "cs_ops",
) -> tuple[WebSocketDispatcher, InMemoryBackupReviewStore]:
    """backup 워커가 있는 WebSocketDispatcher + 공유 review_store."""
    clock = _fixed_clock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(clock=clock)
    review_store = InMemoryBackupReviewStore()
    disp = WebSocketDispatcher(
        clock=clock,
        queue=queue,
        staleness_threshold=timedelta(days=7),
        review_store=review_store,
    )
    snapshot = DelegationSnapshot(
        owner_id=owner_id,
        agent_ids=(agent_id,),
        snapshot_at=SNAPSHOT_TS,
    )
    disp.register_delegation(snapshot)
    return disp, review_store


def test_Major1_P17_web_경로는_legacy_WS와_backup_review를_호출하지_않는다() -> None:
    """P17.2c-2 이후 `/ask*`는 AskOrg의 WS/review 경계를 다시 타지 않는다."""
    from agent_org_network.web import create_app

    ws, review_store = _make_ws_dispatcher()
    app = create_app(
        runtime=StubRuntime(),
        dispatcher=ws,
        review_store=review_store,
    )
    client = TestClient(app, raise_server_exceptions=True)
    http: Any = client

    rec: list[Any] = []
    ws.register(RegisterWorker(owner_id="cs_lead", role="backup"), lambda f: rec.append(f))

    r = _result(cast(Response, http.post("/ask", json={"question": "환불 되나요?"})))
    assert r.status == 200
    assert r.body["type"] == "answered"
    assert r.body["request_id"]
    assert "tracking" not in r.body
    assert rec == []
    assert review_store.pending_for_owner("cs_lead") == []

    restored = _result(cast(Response, http.get(f"/ask/{r.body['request_id']}")))
    assert restored.body == r.body


# ═══════════════════════════════════════════════════════════════════════════
# [Major 2] 검토 후 audit에 decision=Unowned 거짓 기록이 없어야 한다
# ═══════════════════════════════════════════════════════════════════════════


def _make_ws_dispatcher_simple() -> tuple[WebSocketDispatcher, InMemoryBackupReviewStore]:
    clock = _fixed_clock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(clock=clock)
    review_store = InMemoryBackupReviewStore()
    disp = WebSocketDispatcher(
        clock=clock, queue=queue, staleness_threshold=timedelta(days=7), review_store=review_store
    )
    snapshot = DelegationSnapshot(owner_id="alice", agent_ids=("cs_ops",), snapshot_at=SNAPSHOT_TS)
    disp.register_delegation(snapshot)
    return disp, review_store


def _make_ask_org_for_major2(
    disp: WebSocketDispatcher,
    review_store: InMemoryBackupReviewStore,
    audit: InMemoryAuditLog,
) -> Any:
    from agent_org_network.ask_org import AskOrg
    from agent_org_network.conflict import InMemoryPrecedentStore
    from agent_org_network.registry import Registry
    from agent_org_network.router import Router
    from agent_org_network.user import User

    class _FakeClassifier:
        def classify(self, question: str) -> str:
            return "cs"

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
        audit_log=audit,
        clock=_fixed_clock(BASE_TS),
        review_store=review_store,
    )


def test_Major2_검토_후_audit에_Unowned_거짓_기록이_없다() -> None:
    """[Major 2] record_review 가 AuditEntry(decision=Unowned) 를 남기지 않아야 한다.

    수정 전: record_review 가 Unowned 로 거짓 audit 줄을 append → entries 증가.
    수정 후: record_review 가 audit 에 아무것도 남기지 않는다.
    """
    from agent_org_network.user import User

    audit = InMemoryAuditLog()
    disp, review_store = _make_ws_dispatcher_simple()
    ask = _make_ask_org_for_major2(disp, review_store, audit)

    rec: list[Any] = []
    disp.register(RegisterWorker(owner_id="alice", role="backup"), lambda f: rec.append(f))
    ask.handle("환불 되나요?", User(id="web_guest"))

    # backup 워커 회신 시뮬
    assert len(rec) >= 1
    ticket_id: str = rec[0].ticket.ticket_id
    disp.submit(ticket_id, Answer(text="백업 답변", mode="backup"))

    items = review_store.pending_for_owner("alice")
    assert len(items) == 1
    item = items[0]

    entries_before = len(audit.entries)

    ask.record_review(item.item_id, ApproveBackup(by_owner="alice"))

    # [Major 2] 핵심: record_review 가 audit에 아무것도 남기지 않아야 한다.
    # 검토 기록은 BackupReviewStore.history 가 담당(전이≠기록, ADR 0012 결정 7).
    assert len(audit.entries) == entries_before, (
        f"record_review 가 audit 줄을 {len(audit.entries) - entries_before}개 추가했다 — "
        "거짓 AuditEntry(decision=Unowned) 제거 필요(Major 2 미수정)"
    )


def test_Major2_audit에_Unowned_disposition이_없다() -> None:
    """[Major 2] 검토 후 audit.entries에 decision.disposition=='unowned' 줄이 없어야 한다."""
    from agent_org_network.user import User

    audit = InMemoryAuditLog()
    disp, review_store = _make_ws_dispatcher_simple()
    ask = _make_ask_org_for_major2(disp, review_store, audit)

    rec: list[Any] = []
    disp.register(RegisterWorker(owner_id="alice", role="backup"), lambda f: rec.append(f))
    ask.handle("환불 되나요?", User(id="web_guest"))

    ticket_id: str = rec[0].ticket.ticket_id
    disp.submit(ticket_id, Answer(text="백업 답변", mode="backup"))

    item = review_store.pending_for_owner("alice")[0]
    ask.record_review(item.item_id, ApproveBackup(by_owner="alice"))

    # 어떤 audit 줄도 Unowned decision이어선 안 된다(검토 행위가 미아 처분으로 오독).
    for entry in audit.entries:
        assert not isinstance(entry.decision, Unowned) or entry.intent != "backup_review", (
            "audit에 intent='backup_review'인 Unowned 줄이 있다 — 거짓 기록(Major 2 미수정)"
        )


def test_Major2_검토_전이는_store_history에_남는다() -> None:
    """[Major 2] 검토 기록은 audit이 아니라 BackupReviewStore.history가 담당한다.

    record_review 호출 후 review_store.history 에 reviewed 항목이 남아야 한다.
    """
    from agent_org_network.user import User

    audit = InMemoryAuditLog()
    disp, review_store = _make_ws_dispatcher_simple()
    ask = _make_ask_org_for_major2(disp, review_store, audit)

    rec: list[Any] = []
    disp.register(RegisterWorker(owner_id="alice", role="backup"), lambda f: rec.append(f))
    ask.handle("환불 되나요?", User(id="web_guest"))

    ticket_id: str = rec[0].ticket.ticket_id
    disp.submit(ticket_id, Answer(text="백업 답변", mode="backup"))

    item = review_store.pending_for_owner("alice")[0]

    history_len_before = len(review_store.history)
    ask.record_review(item.item_id, CorrectBackup(by_owner="alice", corrected_text="정정 답변"))

    # 검토 전이는 store.history 에 reviewed 항목으로 남아야 한다.
    assert len(review_store.history) > history_len_before, (
        "record_review 후 store.history에 reviewed 항목이 없다"
    )
    last_history = review_store.history[-1]
    assert last_history.status == "reviewed"
    assert isinstance(last_history.review, CorrectBackup)
