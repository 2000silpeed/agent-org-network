"""P17.2c-1a — legacy 호환 Request 상관키 도메인 계약.

새 Application Service가 사용할 ``for_request`` 관문만 non-null 상관키를 강제하고,
기존 생성자는 ``request_id=None``인 값을 계속 만든다. 이 파일은 상관키 자리를 만드는
슬라이스만 검증한다. Request-first intake나 Answer Finalization은 다루지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import cast

import pytest

from agent_org_network.answer_record import AnswerRecord
from agent_org_network.audit import AuditEntry
from agent_org_network.conflict import Candidate, ConflictCase, Resolution
from agent_org_network.decision import Unowned
from agent_org_network.dispatch import EscalatedToManager, WorkTicket
from agent_org_network.manager_queue import (
    AssignOwner,
    FromDeadlock,
    FromDispatch,
    FromUnowned,
    ManagerItem,
    ManagerResolution,
)
from agent_org_network.session import SessionTurn
from agent_org_network.transport import to_ticket_frame

NOW = datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc)


def _legacy_ticket() -> WorkTicket:
    return WorkTicket(
        owner_id="owner-a",
        agent_id="card-a",
        question="환불은 언제 되나요?",
        enqueued_at=NOW,
        ticket_id="ticket-1",
    )


def _legacy_case() -> ConflictCase:
    return ConflictCase(
        intent="refund",
        question="환불은 언제 되나요?",
        candidates=(
            Candidate(agent_id="card-a", owner="owner-a"),
            Candidate(agent_id="card-b", owner="owner-b"),
        ),
        opened_at=NOW,
        case_id="case-1",
    )


def _legacy_manager_item() -> ManagerItem:
    return ManagerItem(
        manager_id="manager-1",
        source=FromUnowned(
            decision=Unowned(escalated_to="manager-1", reason="후보 없음"),
            question="누가 담당하나요?",
        ),
        created_at=NOW,
        item_id="item-1",
    )


def _legacy_answer_record() -> AnswerRecord:
    return AnswerRecord(
        record_id="record-1",
        question="환불은 언제 되나요?",
        answer_text="영업일 3일 이내입니다.",
        answered_by="owner-a",
        agent_id="card-a",
        mode="full",
        session_id="session-1",
        answered_at=NOW,
    )


def _legacy_turn() -> SessionTurn:
    return SessionTurn(
        question="환불은 언제 되나요?",
        answer_text="영업일 3일 이내입니다.",
        answered_by="owner-a",
        at=NOW,
    )


def _legacy_audit() -> AuditEntry:
    return AuditEntry(
        timestamp=NOW,
        user_id="user-1",
        question="누가 담당하나요?",
        intent="unknown",
        decision=Unowned(escalated_to="manager-1", reason="후보 없음"),
    )


def test_legacy_생성자는_request_상관키를_추정하지_않는다() -> None:
    ticket = _legacy_ticket()
    assert ticket.request_id is None
    assert ticket.attempt is None
    assert _legacy_case().request_id is None
    assert _legacy_manager_item().request_id is None
    assert _legacy_answer_record().request_id is None
    assert _legacy_turn().request_id is None
    assert _legacy_audit().request_id is None


@pytest.mark.parametrize("request_id", [None, "", "   "])
def test_for_request_관문은_모든_모델에서_nonblank_request_id를_강제한다(
    request_id: str | None,
) -> None:
    factories: tuple[Callable[[], object], ...] = (
        lambda: WorkTicket.for_request(
            request_id=cast(str, request_id),
            attempt=1,
            owner_id="owner-a",
            agent_id="card-a",
            question="q",
            enqueued_at=NOW,
        ),
        lambda: ConflictCase.for_request(
            request_id=cast(str, request_id),
            intent="refund",
            question="q",
            candidates=(Candidate(agent_id="card-a", owner="owner-a"),),
            opened_at=NOW,
        ),
        lambda: ManagerItem.for_request(
            request_id=cast(str, request_id),
            manager_id="manager-1",
            source=FromUnowned(
                decision=Unowned(escalated_to="manager-1", reason="none"),
                question="q",
            ),
            created_at=NOW,
        ),
        lambda: AnswerRecord.for_request(
            request_id=cast(str, request_id),
            record_id="record-1",
            question="q",
            answer_text="a",
            answered_by="owner-a",
            agent_id="card-a",
            mode="full",
            session_id=None,
            answered_at=NOW,
        ),
        lambda: SessionTurn.for_request(
            request_id=cast(str, request_id),
            question="q",
            answer_text="a",
            answered_by="owner-a",
            at=NOW,
        ),
        lambda: AuditEntry.for_request(
            request_id=cast(str, request_id),
            timestamp=NOW,
            user_id="user-1",
            question="q",
            intent="unknown",
            decision=Unowned(escalated_to="manager-1", reason="none"),
        ),
    )
    for factory in factories:
        with pytest.raises(ValueError, match="request_id"):
            factory()


def test_for_request_관문이_상관키를_그대로_보존한다() -> None:
    request_id = "request-1"
    ticket = WorkTicket.for_request(
        request_id=request_id,
        attempt=2,
        owner_id="owner-a",
        agent_id="card-a",
        question="q",
        enqueued_at=NOW,
        ticket_id="ticket-2",
        context="이전 대화",
    )
    case = ConflictCase.for_request(
        request_id=request_id,
        intent="refund",
        question="q",
        candidates=(Candidate(agent_id="card-a", owner="owner-a"),),
        opened_at=NOW,
        case_id="case-2",
    )
    answer = AnswerRecord.for_request(
        request_id=request_id,
        record_id="record-2",
        question="q",
        answer_text="a",
        answered_by="owner-a",
        agent_id="card-a",
        mode="full",
        session_id=None,
        answered_at=NOW,
    )
    turn = SessionTurn.for_request(
        request_id=request_id,
        question="q",
        answer_text="a",
        answered_by="owner-a",
        at=NOW,
    )
    audit = AuditEntry.for_request(
        request_id=request_id,
        timestamp=NOW,
        user_id="user-1",
        question="q",
        intent="refund",
        decision=Unowned(escalated_to="manager-1", reason="none"),
    )

    assert ticket.request_id == request_id
    assert ticket.attempt == 2
    assert case.request_id == request_id
    assert answer.request_id == request_id
    assert turn.request_id == request_id
    assert audit.request_id == request_id


@pytest.mark.parametrize("attempt", [0, -1])
def test_WorkTicket_for_request는_양의_attempt만_받는다(attempt: int) -> None:
    with pytest.raises(ValueError, match="attempt"):
        WorkTicket.for_request(
            request_id="request-1",
            attempt=attempt,
            owner_id="owner-a",
            agent_id="card-a",
            question="q",
            enqueued_at=NOW,
        )


def test_WorkTicket_for_request는_None_request_id와_None_attempt_쌍도_거부한다() -> None:
    with pytest.raises(ValueError, match="request_id"):
        WorkTicket.for_request(
            request_id=cast(str, None),
            attempt=cast(int, None),
            owner_id="owner-a",
            agent_id="card-a",
            question="q",
            enqueued_at=NOW,
        )


def test_WorkTicket_raw_constructor도_request_id와_attempt의_쌍을_강제한다() -> None:
    with pytest.raises(ValueError, match="request_id.*attempt|attempt.*request_id"):
        WorkTicket(
            owner_id="owner-a",
            agent_id="card-a",
            question="q",
            enqueued_at=NOW,
            request_id="request-1",
        )
    with pytest.raises(ValueError, match="request_id.*attempt|attempt.*request_id"):
        WorkTicket(
            owner_id="owner-a",
            agent_id="card-a",
            question="q",
            enqueued_at=NOW,
            attempt=1,
        )


@pytest.mark.parametrize("attempt", [True, "1", 1.0])
def test_WorkTicket_raw_constructor는_non_int_attempt를_ValueError로_거부한다(
    attempt: object,
) -> None:
    with pytest.raises(ValueError, match="attempt"):
        WorkTicket(
            owner_id="owner-a",
            agent_id="card-a",
            question="q",
            enqueued_at=NOW,
            request_id="request-1",
            attempt=cast(int, attempt),
        )


def test_ConflictCase_resolve는_request_id를_보존한다() -> None:
    case = ConflictCase.for_request(
        request_id="request-1",
        intent="refund",
        question="q",
        candidates=(Candidate(agent_id="card-a", owner="owner-a"),),
        opened_at=NOW,
        case_id="case-1",
    )
    resolved = case.resolve(Resolution(intent="refund", primary="card-a"))
    assert resolved.request_id == "request-1"


def test_ManagerItem_resolve는_request_id를_보존한다() -> None:
    item = ManagerItem.for_request(
        request_id="request-1",
        manager_id="manager-1",
        source=FromUnowned(
            decision=Unowned(escalated_to="manager-1", reason="none"),
            question="q",
        ),
        created_at=NOW,
        item_id="item-1",
    )
    action = AssignOwner(by_manager="manager-1", primary="card-a")
    resolved = item.resolve(ManagerResolution(action=action, resolution=None))
    assert resolved.request_id == "request-1"


def test_ManagerItem_for_request는_Deadlock_case의_request_id_일치를_강제한다() -> None:
    case = ConflictCase.for_request(
        request_id="request-other",
        intent="refund",
        question="q",
        candidates=(Candidate(agent_id="card-a", owner="owner-a"),),
        opened_at=NOW,
    )
    with pytest.raises(ValueError, match="ConflictCase.*request_id"):
        ManagerItem.for_request(
            request_id="request-1",
            manager_id="manager-1",
            source=FromDeadlock(case=case),
            created_at=NOW,
        )


def test_ManagerItem_for_request는_Dispatch_ticket의_request_id_일치를_강제한다() -> None:
    ticket = WorkTicket.for_request(
        request_id="request-other",
        attempt=1,
        owner_id="owner-a",
        agent_id="card-a",
        question="q",
        enqueued_at=NOW,
    )
    source = FromDispatch(
        outcome=EscalatedToManager(ticket=ticket, manager_id="manager-1", reason="timeout")
    )
    with pytest.raises(ValueError, match="WorkTicket.*request_id"):
        ManagerItem.for_request(
            request_id="request-1",
            manager_id="manager-1",
            source=source,
            created_at=NOW,
        )


def test_ManagerItem_raw_constructor도_Deadlock_case_request_id_불일치를_거부한다() -> None:
    case = ConflictCase.for_request(
        request_id="request-other",
        intent="refund",
        question="q",
        candidates=(Candidate(agent_id="card-a", owner="owner-a"),),
        opened_at=NOW,
    )
    with pytest.raises(ValueError, match="ConflictCase.*request_id"):
        ManagerItem(
            manager_id="manager-1",
            source=FromDeadlock(case=case),
            created_at=NOW,
            request_id="request-1",
        )


def test_ManagerItem_raw_constructor도_Dispatch_ticket_request_id_불일치를_거부한다() -> None:
    ticket = WorkTicket.for_request(
        request_id="request-other",
        attempt=1,
        owner_id="owner-a",
        agent_id="card-a",
        question="q",
        enqueued_at=NOW,
    )
    with pytest.raises(ValueError, match="WorkTicket.*request_id"):
        ManagerItem(
            manager_id="manager-1",
            source=FromDispatch(
                outcome=EscalatedToManager(
                    ticket=ticket,
                    manager_id="manager-1",
                    reason="timeout",
                )
            ),
            created_at=NOW,
            request_id="request-1",
        )


def test_ManagerItem_raw_constructor는_outer_None과_correlated_Deadlock을_거부한다() -> None:
    case = ConflictCase.for_request(
        request_id="request-1",
        intent="refund",
        question="q",
        candidates=(Candidate(agent_id="card-a", owner="owner-a"),),
        opened_at=NOW,
    )
    with pytest.raises(ValueError, match="ConflictCase.*request_id"):
        ManagerItem(
            manager_id="manager-1",
            source=FromDeadlock(case=case),
            created_at=NOW,
        )


def test_ManagerItem_raw_constructor는_outer_None과_correlated_Dispatch를_거부한다() -> None:
    ticket = WorkTicket.for_request(
        request_id="request-1",
        attempt=1,
        owner_id="owner-a",
        agent_id="card-a",
        question="q",
        enqueued_at=NOW,
    )
    with pytest.raises(ValueError, match="WorkTicket.*request_id"):
        ManagerItem(
            manager_id="manager-1",
            source=FromDispatch(
                outcome=EscalatedToManager(
                    ticket=ticket,
                    manager_id="manager-1",
                    reason="timeout",
                )
            ),
            created_at=NOW,
        )


def test_ManagerItem_for_request는_일치하는_중첩_request_id를_받는다() -> None:
    ticket = WorkTicket.for_request(
        request_id="request-1",
        attempt=1,
        owner_id="owner-a",
        agent_id="card-a",
        question="q",
        enqueued_at=NOW,
    )
    item = ManagerItem.for_request(
        request_id="request-1",
        manager_id="manager-1",
        source=FromDispatch(
            outcome=EscalatedToManager(
                ticket=ticket,
                manager_id="manager-1",
                reason="timeout",
            )
        ),
        created_at=NOW,
    )
    assert item.request_id == ticket.request_id


def test_AuditEntry_legacy_JSON_shape은_request_id_키가_추가되지_않는다() -> None:
    entry = _legacy_audit()
    expected = {
        "timestamp": NOW.isoformat(),
        "user_id": "user-1",
        "question": "누가 담당하나요?",
        "intent": "unknown",
        "decision": {
            "disposition": "unowned",
            "escalated_to": "manager-1",
            "reason": "후보 없음",
        },
        "answer": None,
        "dispatch": None,
        "tracking": None,
    }
    assert entry.as_record() == expected
    assert entry.to_jsonl() == json.dumps(expected, ensure_ascii=False)


def test_AuditEntry_correlated_JSON에만_request_id가_실린다() -> None:
    entry = AuditEntry.for_request(
        request_id="request-1",
        timestamp=NOW,
        user_id="user-1",
        question="q",
        intent="unknown",
        decision=Unowned(escalated_to="manager-1", reason="none"),
    )
    assert entry.as_record()["request_id"] == "request-1"


def test_legacy_AnswerRecord_model_dump와_JSON_key_shape이_그대로다() -> None:
    record = _legacy_answer_record()
    expected = {
        "record_id": "record-1",
        "question": "환불은 언제 되나요?",
        "answer_text": "영업일 3일 이내입니다.",
        "answered_by": "owner-a",
        "agent_id": "card-a",
        "mode": "full",
        "session_id": "session-1",
        "answered_at": NOW,
        "needs_correction_review": False,
    }
    assert record.model_dump() == expected
    assert record.model_dump_json() == (
        '{"record_id":"record-1","question":"환불은 언제 되나요?",'
        '"answer_text":"영업일 3일 이내입니다.","answered_by":"owner-a",'
        '"agent_id":"card-a","mode":"full","session_id":"session-1",'
        '"answered_at":"2026-07-12T09:00:00Z","needs_correction_review":false}'
    )


def test_correlated_AnswerRecord_model_dump에는_request_id가_실린다() -> None:
    record = AnswerRecord.for_request(
        request_id="request-1",
        record_id="record-1",
        question="q",
        answer_text="a",
        answered_by="owner-a",
        agent_id="card-a",
        mode="full",
        session_id=None,
        answered_at=NOW,
    )
    assert record.model_dump()["request_id"] == "request-1"
    assert json.loads(record.model_dump_json())["request_id"] == "request-1"


def test_WorkTicket_wire_DTO에는_request_id와_attempt를_추가하지_않는다() -> None:
    ticket = WorkTicket.for_request(
        request_id="request-1",
        attempt=1,
        owner_id="owner-a",
        agent_id="card-a",
        question="q",
        enqueued_at=NOW,
        ticket_id="ticket-1",
    )
    payload = to_ticket_frame(ticket).model_dump(mode="json")
    assert payload == {
        "ticket_id": "ticket-1",
        "agent_id": "card-a",
        "question": "q",
        "enqueued_at": "2026-07-12T09:00:00Z",
        "context": None,
        "hitl": False,
    }
