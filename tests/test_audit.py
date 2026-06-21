import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import AskOrg, Pending
from agent_org_network.audit import InMemoryAuditLog, JsonlAuditLog
from agent_org_network.classifier import FakeClassifier
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.dispatch import (
    EscalatedToManager,
    InMemoryWorkQueueDispatcher,
    LocalRuntimeDispatcher,
)
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import StubRuntime
from agent_org_network.user import User


_FIXED_DT = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return _FIXED_DT


def _card(agent_id: str, domains: list[str], owner: str = "D", knowledge_sources: list[str] | None = None) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary="요약",
        domains=domains,
        last_reviewed_at=date(2026, 6, 20),
        knowledge_sources=knowledge_sources or [],
    )


def _ask_org_with(
    cards: list[AgentCard],
    intent: str,
    audit_log: InMemoryAuditLog | JsonlAuditLog,
) -> AskOrg:
    registry = Registry()
    for c in cards:
        registry.register(c)
    classifier = FakeClassifier(intent)
    router = Router(registry, classifier, root_user="root")
    return AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=audit_log,
        clock=_fixed_clock,
    )


def _ask_org_with_queue(
    cards: list[AgentCard],
    intent: str,
    audit_log: InMemoryAuditLog | JsonlAuditLog,
    dispatcher: InMemoryWorkQueueDispatcher,
) -> AskOrg:
    """InMemoryWorkQueueDispatcher를 주입한 AskOrg 조립 헬퍼(escalation/AwaitingWorker 경로)."""
    registry = Registry()
    for c in cards:
        registry.register(c)
    classifier = FakeClassifier(intent)
    router = Router(registry, classifier, root_user="root")
    return AskOrg(
        router=router,
        dispatcher=dispatcher,
        audit_log=audit_log,
        clock=_fixed_clock,
    )


def _timeout_clock(timeout_elapsed: timedelta) -> Callable[[], datetime]:
    """dispatch 첫 호출은 BASE_TS, 이후(poll waited 계산)는 timeout 경과 시각을 반환하는 clock.

    InMemoryWorkQueueDispatcher.dispatch 1회 + poll 1회 = 총 2회 호출.
    AskOrg._clock(audit 기록)은 별도 주입이므로 dispatcher clock과 무관.
    """
    call_count = 0

    def clock() -> datetime:
        nonlocal call_count
        call_count += 1
        return _FIXED_DT if call_count == 1 else _FIXED_DT + timeout_elapsed

    return clock


def test_Routed_처리가_내부값까지_JSONL_한줄로_기록된다(tmp_path: Path) -> None:
    sources = ["위키/계약가이드", "Notion/FAQ"]
    c = _card("contract_ops", ["계약 검토"], knowledge_sources=sources)
    audit = JsonlAuditLog(tmp_path / "audit.jsonl")
    ask = _ask_org_with([c], "계약 검토", audit)
    user = User(id="u1")

    ask.handle("이 계약 조건 바꿔도 돼?", user)

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert record["timestamp"] == _FIXED_DT.isoformat()
    assert record["user_id"] == "u1"
    assert record["intent"] == "계약 검토"
    assert record["decision"]["disposition"] == "routed"
    assert record["decision"]["primary"] == "contract_ops"
    assert record["decision"]["confidence"] == 1.0
    assert record["answer"]["mode"] == "full"


def test_Unowned_처리가_escalated_to까지_기록된다(tmp_path: Path) -> None:
    c = _card("contract_ops", ["계약 검토"])
    audit = JsonlAuditLog(tmp_path / "audit.jsonl")
    ask = _ask_org_with([c], "주차장", audit)
    user = User(id="u2")

    ask.handle("주차장 정기권 어떻게 갱신해요?", user)

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert record["decision"]["disposition"] == "unowned"
    assert record["decision"]["escalated_to"] == "root"
    assert record["answer"] is None


def test_InMemoryAuditLog_handle_1회에_entries_1개_decision_원형보존() -> None:
    sources = ["위키/계약가이드"]
    c = _card("contract_ops", ["계약 검토"], knowledge_sources=sources)
    audit = InMemoryAuditLog()
    ask = _ask_org_with([c], "계약 검토", audit)
    user = User(id="u3")

    ask.handle("계약 리뷰 부탁해요", user)

    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert isinstance(entry.decision, Routed)
    assert entry.decision.primary.agent_id == "contract_ops"
    assert entry.timestamp == _FIXED_DT


# ── T6.3 2b 선결 — escalation→audit 기록 신규 테스트 ──────────────────────────


def test_escalation_시_audit_dispatch_outcome이_EscalatedToManager이고_manager_id_reason이_남는다() -> None:
    """핵심: reply는 Pending(dispatched)이지만 audit에는 EscalatedToManager 원형이 남는다.

    노출 불변식(manager_id·reason은 Pending에 안 샘) + audit 완전성(여기선 전부 기록)의
    비대칭 해소 — 2b 선결의 핵심 테스트.
    """
    c = _card("cs_ops", ["환불"], owner="alice")
    audit = InMemoryAuditLog()
    clock = _timeout_clock(timedelta(seconds=200))
    dispatcher = InMemoryWorkQueueDispatcher(
        clock=clock,
        timeout=timedelta(seconds=60),
        manager_of=lambda owner_id: "boss_" + owner_id,
    )
    ask = _ask_org_with_queue([c], "환불", audit, dispatcher)
    user = User(id="u1")

    reply = ask.handle("환불 되나요?", user)

    # 사용자向 reply는 Pending(dispatched) — manager_id·reason 안 샘
    assert isinstance(reply, Pending)
    assert reply.kind == "dispatched"
    assert not hasattr(reply, "manager_id")

    # audit에는 EscalatedToManager 원형이 전부 남아야 한다
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert isinstance(entry.dispatch_outcome, EscalatedToManager)
    assert entry.dispatch_outcome.manager_id == "boss_alice"
    assert "alice" in entry.dispatch_outcome.reason
    assert entry.answer is None  # escalation엔 답 없음


def test_escalation_JSONL_직렬화_disposition_escalated_to_reason_answer_None(tmp_path: Path) -> None:
    """JsonlAuditLog escalation 경로: record 키 모양 검증."""
    c = _card("cs_ops", ["환불"], owner="alice")
    audit = JsonlAuditLog(tmp_path / "audit.jsonl")
    clock = _timeout_clock(timedelta(seconds=200))
    dispatcher = InMemoryWorkQueueDispatcher(
        clock=clock,
        timeout=timedelta(seconds=60),
        manager_of=lambda owner_id: "mgr_" + owner_id,
    )
    ask = _ask_org_with_queue([c], "환불", audit, dispatcher)
    user = User(id="u1")

    ask.handle("환불 되나요?", user)

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert record["dispatch"]["disposition"] == "escalated_to_manager"
    assert record["dispatch"]["escalated_to"] == "mgr_alice"
    assert "reason" in record["dispatch"]
    assert record["answer"] is None


def test_Unowned_escalated_to와_dispatch_escalated_to가_같은_키_모양() -> None:
    """통일성: Unowned decision의 record["decision"]["escalated_to"]와
    EscalatedToManager dispatch의 record["dispatch"]["escalated_to"]가 같은 키를 쓴다.

    두 escalation 대상을 같은 모양으로 직렬화한다는 audit 설계 의도 검증.
    """
    # Unowned 경로
    c_unowned = _card("contract_ops", ["계약 검토"])
    audit_unowned = InMemoryAuditLog()
    ask_unowned = _ask_org_with([c_unowned], "주차장", audit_unowned)
    ask_unowned.handle("주차장 정기권?", User(id="u_unowned"))

    entry_unowned = audit_unowned.entries[0]
    assert isinstance(entry_unowned.decision, Unowned)
    assert entry_unowned.decision.escalated_to == "root"

    # EscalatedToManager 경로
    c_escalated = _card("cs_ops", ["환불"], owner="alice")
    audit_escalated = InMemoryAuditLog()
    clock = _timeout_clock(timedelta(seconds=200))
    dispatcher = InMemoryWorkQueueDispatcher(
        clock=clock,
        timeout=timedelta(seconds=60),
        manager_of=lambda owner_id: "root_mgr",
    )
    ask_escalated = _ask_org_with_queue([c_escalated], "환불", audit_escalated, dispatcher)
    ask_escalated.handle("환불 되나요?", User(id="u_escalated"))

    entry_escalated = audit_escalated.entries[0]
    assert isinstance(entry_escalated.dispatch_outcome, EscalatedToManager)
    assert entry_escalated.dispatch_outcome.manager_id == "root_mgr"

    # JSONL로 직렬화해 키 이름 일치 확인
    unowned_record = json.loads(entry_unowned.to_jsonl())
    escalated_record = json.loads(entry_escalated.to_jsonl())

    # 두 escalation 대상 모두 "escalated_to" 키를 사용
    assert "escalated_to" in unowned_record["decision"]
    assert "escalated_to" in escalated_record["dispatch"]
    assert unowned_record["decision"]["escalated_to"] == "root"
    assert escalated_record["dispatch"]["escalated_to"] == "root_mgr"


def test_Delivered_answer_파생_및_dispatch_disposition_delivered_하위호환(tmp_path: Path) -> None:
    """LocalRuntimeDispatcher → Routed → Delivered: answer 파생 + disposition 직렬화 검증."""
    sources = ["위키/계약가이드"]
    c = _card("contract_ops", ["계약 검토"], knowledge_sources=sources)
    audit = JsonlAuditLog(tmp_path / "audit.jsonl")
    ask = _ask_org_with([c], "계약 검토", audit)
    user = User(id="u1")

    ask.handle("이 계약 조건 바꿔도 돼?", user)

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    # dispatch disposition
    assert record["dispatch"]["disposition"] == "delivered"

    # answer는 Delivered.answer에서 파생 — answer 키 있음
    assert record["answer"] is not None
    assert record["answer"]["mode"] == "full"

    # InMemory 검증 — entry.answer가 Delivered.answer와 같은 source
    audit_mem = InMemoryAuditLog()
    ask_mem = _ask_org_with([_card("contract_ops2", ["계약 검토2"], knowledge_sources=sources)], "계약 검토2", audit_mem)
    ask_mem.handle("계약서 봐줘", user)
    entry = audit_mem.entries[0]
    assert entry.answer is not None
    assert entry.answer.mode == "full"


def test_Contested_Unowned_dispatch_outcome_None_answer_None(tmp_path: Path) -> None:
    """Contested·Unowned는 dispatch를 안 하므로 dispatch_outcome=None, record["dispatch"]=None."""
    # Contested 경로
    c1 = _card("cs_ops", ["환불"], owner="owner_CS")
    c2 = _card("sales_ops", ["환불"], owner="owner_Sales")
    audit_contested = InMemoryAuditLog()
    ask_contested = _ask_org_with([c1, c2], "환불", audit_contested)
    ask_contested.handle("환불 되나요?", User(id="u1"))

    entry_contested = audit_contested.entries[0]
    assert isinstance(entry_contested.decision, Contested)
    assert entry_contested.dispatch_outcome is None
    assert entry_contested.answer is None

    # JSONL
    record_c = json.loads(entry_contested.to_jsonl())
    assert record_c["dispatch"] is None
    assert record_c["answer"] is None

    # Unowned 경로
    c3 = _card("contract_ops", ["계약 검토"])
    audit_unowned_path = tmp_path / "unowned.jsonl"
    audit_unowned = JsonlAuditLog(audit_unowned_path)
    ask_unowned = _ask_org_with([c3], "주차장", audit_unowned)
    ask_unowned.handle("주차장 어디예요?", User(id="u2"))

    lines = audit_unowned_path.read_text(encoding="utf-8").strip().splitlines()
    record_u = json.loads(lines[0])
    assert record_u["dispatch"] is None
    assert record_u["answer"] is None


def test_AwaitingWorker_audit_waited_seconds_포함() -> None:
    """timeout 전 poll → AwaitingWorker: record["dispatch"]["waited_seconds"] 존재.

    AskOrg는 dispatch 후 즉시 poll — timeout보다 경과가 짧아야 AwaitingWorker.
    dispatch clock=BASE_TS, poll clock=BASE_TS(경과=0) — 같은 시각이면 waited=0s.
    """
    c = _card("cs_ops", ["환불"], owner="alice")
    audit = InMemoryAuditLog()
    # 고정 clock: dispatch·poll 모두 동일 시각 → waited=0, timeout 미경과
    dispatcher = InMemoryWorkQueueDispatcher(
        clock=_fixed_clock,
        timeout=timedelta(seconds=120),
    )
    ask = _ask_org_with_queue([c], "환불", audit, dispatcher)
    user = User(id="u1")

    ask.handle("환불 되나요?", user)

    assert len(audit.entries) == 1
    entry = audit.entries[0]
    # waited=0 — AwaitingWorker(timeout 전)
    from agent_org_network.dispatch import AwaitingWorker
    assert isinstance(entry.dispatch_outcome, AwaitingWorker)
    assert entry.dispatch_outcome.waited.total_seconds() == 0.0

    record = json.loads(entry.to_jsonl())
    assert record["dispatch"]["disposition"] == "awaiting_worker"
    assert "waited_seconds" in record["dispatch"]
    assert record["dispatch"]["waited_seconds"] == 0.0


# ── M1: audit 레코드에 snapshot_sha 기록 ──────────────────────────────────────


def test_snapshot_sha_있는_Answer_audit_레코드에_sha_실린다() -> None:
    """M1: Answer.snapshot_sha가 있으면 audit 레코드 answer["snapshot_sha"]에 나타난다."""
    from agent_org_network.audit import AuditEntry
    from agent_org_network.decision import Routed
    from agent_org_network.dispatch import Delivered, WorkTicket
    from agent_org_network.runtime import Answer

    sha = "abc1234" * 5
    answer = Answer(text="스냅샷 답", mode="full", snapshot_sha=sha)
    card = _card("cs_ops", ["환불"])
    decision = Routed(primary=card, confidence=1.0, reason="test")
    ticket = WorkTicket(owner_id="cs_lead", agent_id="cs_ops", question="환불?", enqueued_at=_FIXED_DT)
    outcome = Delivered(ticket=ticket, answer=answer)
    entry = AuditEntry(
        timestamp=_FIXED_DT,
        user_id="u1",
        question="환불?",
        intent="환불",
        decision=decision,
        dispatch_outcome=outcome,
    )

    record = entry.as_record()
    assert record["answer"] is not None
    assert record["answer"]["snapshot_sha"] == sha


def test_snapshot_sha_None인_Answer_audit_레코드에_키_없음() -> None:
    """M1: snapshot_sha=None이면 기존 모양 유지 — 키가 없다."""
    from agent_org_network.audit import AuditEntry
    from agent_org_network.decision import Routed
    from agent_org_network.dispatch import Delivered, WorkTicket
    from agent_org_network.runtime import Answer

    answer = Answer(text="일반 답", mode="full", snapshot_sha=None)
    card = _card("cs_ops", ["환불"])
    decision = Routed(primary=card, confidence=1.0, reason="test")
    ticket = WorkTicket(owner_id="cs_lead", agent_id="cs_ops", question="환불?", enqueued_at=_FIXED_DT)
    outcome = Delivered(ticket=ticket, answer=answer)
    entry = AuditEntry(
        timestamp=_FIXED_DT,
        user_id="u1",
        question="환불?",
        intent="환불",
        decision=decision,
        dispatch_outcome=outcome,
    )

    record = entry.as_record()
    assert record["answer"] is not None
    assert "snapshot_sha" not in record["answer"]


def test_snapshot_sha_JSONL_직렬화_왕복() -> None:
    """M1: to_jsonl → json.loads 왕복 시 snapshot_sha 보존(문자열 그대로)."""
    import json

    from agent_org_network.audit import AuditEntry
    from agent_org_network.decision import Routed
    from agent_org_network.dispatch import Delivered, WorkTicket
    from agent_org_network.runtime import Answer

    sha = "deadbeef" * 5
    answer = Answer(text="왕복 답", mode="full", snapshot_sha=sha)
    card = _card("legal_ops", ["계약 검토"])
    decision = Routed(primary=card, confidence=1.0, reason="test")
    ticket = WorkTicket(owner_id="legal_lead", agent_id="legal_ops", question="계약?", enqueued_at=_FIXED_DT)
    outcome = Delivered(ticket=ticket, answer=answer)
    entry = AuditEntry(
        timestamp=_FIXED_DT,
        user_id="u2",
        question="계약?",
        intent="계약 검토",
        decision=decision,
        dispatch_outcome=outcome,
    )

    roundtripped = json.loads(entry.to_jsonl())
    assert roundtripped["answer"]["snapshot_sha"] == sha
