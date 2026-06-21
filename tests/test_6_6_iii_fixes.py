"""T6.6-iii code-reviewer 지적 수정 검증 — red→green.

[Major 1] build_demo review_store 연결: web 경로에서 retrieve 덧씌움이 작동해야 한다.
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
from agent_org_network.review import (
    ApproveBackup,
    BackupReviewService,
    CorrectBackup,
    InMemoryBackupReviewStore,
)
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
# [Major 1] web 경로에서 retrieve 덧씌움이 작동해야 한다
# build_demo 가 review_store 를 AskOrg 에 전달해야 bundle.ask._review_store 가
# 실 store를 가리킨다. 미연결이면 retrieve 덧씌움이 None 처리라 mode 변경이 없다.
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


def test_Major1_web_경로에서_retrieve_덧씌움이_Correct_후_full을_돌려준다() -> None:
    """[Major 1] create_app에 review_store 주입 → bundle.ask._review_store가 실 store.

    흐름:
      1. WebSocketDispatcher + review_store 같은 인스턴스로 create_app.
      2. POST /ask → backup 워커가 회신 → dispatched(tracking).
      3. POST /backup-reviews/{item_id} 로 Correct 처분.
      4. GET /ask/{tracking} → 정정 text + mode=full 이어야 한다.

    수정 전: bundle.ask._review_store=None이라 retrieve가 poll 그대로 반환(mode=backup).
    수정 후: bundle.ask._review_store=review_store라 Correct 반영(mode=full).
    """
    from agent_org_network.web import create_app

    ws, review_store = _make_ws_dispatcher()
    review_svc = BackupReviewService(review_store)

    app = create_app(
        runtime=StubRuntime(),
        dispatcher=ws,
        review_store=review_store,
        review_service=review_svc,
    )
    client = TestClient(app, raise_server_exceptions=True)
    http: Any = client

    # 1. 질문 → backup 워커가 연결돼 있으므로 dispatched(tracking).
    rec: list[Any] = []
    ws.register(RegisterWorker(owner_id="cs_lead", role="backup"), lambda f: rec.append(f))

    r = _result(cast(Response, http.post("/ask", json={"question": "환불 되나요?"})))
    assert r.status == 200
    assert r.body["type"] == "pending"
    assert r.body["kind"] == "dispatched"
    tracking: str = r.body["tracking"]

    # 2. backup 워커 회신 시뮬 — submit 으로 backup 답 종착.
    assert len(rec) == 1
    ticket_id: str = rec[0].ticket.ticket_id
    ws.submit(ticket_id, Answer(text="백업 환불 안내", mode="backup"))

    # 3. review_store 에 항목이 생겼어야 한다.
    items = review_store.pending_for_owner("cs_lead")
    assert len(items) == 1, "review_store에 검토 항목이 없다 — Major 1 미수정"
    item = items[0]

    # 4. Correct 처분 (web 라우트 경유).
    r2 = _result(
        cast(
            Response,
            http.post(
                f"/backup-reviews/{item.item_id}",
                json={
                    "type": "correct",
                    "by_owner": "cs_lead",
                    "corrected_text": "정정된 환불 안내입니다.",
                },
            ),
        )
    )
    assert r2.status == 200

    # 5. retrieve → 정정 text + mode=full 이어야 한다.
    r3 = _result(cast(Response, http.get(f"/ask/{tracking}")))
    assert r3.status == 200
    assert r3.body["type"] == "answered", (
        f"retrieve가 answered 아님: {r3.body} — "
        "build_demo에 review_store가 연결되지 않아 덧씌움 미작동(Major 1 미수정)"
    )
    assert r3.body["mode"] == "full", (
        f"mode={r3.body.get('mode')} — Correct 후 full 이어야 하는데 backup 그대로(Major 1 미수정)"
    )
    assert r3.body["text"] == "정정된 환불 안내입니다."


def test_Major1_web_경로에서_retrieve_덧씌움이_Approve_후_full을_돌려준다() -> None:
    """[Major 1] Approve 처분 후 retrieve → mode=full."""
    from agent_org_network.web import create_app

    ws, review_store = _make_ws_dispatcher()
    review_svc = BackupReviewService(review_store)

    app = create_app(
        runtime=StubRuntime(),
        dispatcher=ws,
        review_store=review_store,
        review_service=review_svc,
    )
    client = TestClient(app, raise_server_exceptions=True)
    http: Any = client

    rec: list[Any] = []
    ws.register(RegisterWorker(owner_id="cs_lead", role="backup"), lambda f: rec.append(f))

    r = _result(cast(Response, http.post("/ask", json={"question": "환불 되나요?"})))
    tracking: str = r.body["tracking"]
    ticket_id: str = rec[0].ticket.ticket_id
    ws.submit(ticket_id, Answer(text="백업 환불 안내", mode="backup"))

    items = review_store.pending_for_owner("cs_lead")
    assert len(items) == 1
    item = items[0]

    http.post(
        f"/backup-reviews/{item.item_id}",
        json={"type": "approve", "by_owner": "cs_lead"},
    )

    r3 = _result(cast(Response, http.get(f"/ask/{tracking}")))
    assert r3.status == 200
    assert r3.body["type"] == "answered"
    assert r3.body["mode"] == "full", (
        f"Approve 후 mode={r3.body.get('mode')} — full 이어야 한다(Major 1 미수정)"
    )


# ═══════════════════════════════════════════════════════════════════════════
# [Major 2] 검토 후 audit에 decision=Unowned 거짓 기록이 없어야 한다
# ═══════════════════════════════════════════════════════════════════════════


def _make_ws_dispatcher_simple() -> tuple[WebSocketDispatcher, InMemoryBackupReviewStore]:
    clock = _fixed_clock(BASE_TS)
    queue = InMemoryWorkQueueDispatcher(clock=clock)
    review_store = InMemoryBackupReviewStore()
    disp = WebSocketDispatcher(clock=clock, queue=queue, staleness_threshold=timedelta(days=7), review_store=review_store)
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
