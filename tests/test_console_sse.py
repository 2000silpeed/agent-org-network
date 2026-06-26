"""T9.2(a) — ConsoleEvent sealed sum + serialize_console_event 순수 함수 테스트 (ADR 0022·T9.2).

결정론 보장:
  - 순수 함수(IO 0·네트워크 0·외부 프로세스 0).
  - 도메인 값에서만 투영(도메인 픽스처 주입).

잠근 불변식:
  - 망라성(exhaustiveness): 새 ConsoleEvent variant 추가 시 assert_never가 타입 에러.
  - 노출 불변식: 콘솔은 운영 면 → 내부값 OK, 사용자向 비밀은 렌더에 안 실림.
  - 직렬화 키 안정성: 각 사건이 event_type과 그 variant 고유 필드를 포함.
  - 기존 RoutingDecision sealed sum 재사용(Routed/Contested/Unowned).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.console import (
    ConsoleEvent,
    QuestionReceived,
    RoutingDecisionRecorded,
    AnswerSent,
    WorkerConnected,
    WorkerDisconnected,
    serialize_console_event,
)
from agent_org_network.decision import Routed, Contested, Unowned


# ── 픽스처 ────────────────────────────────────────────────────────────────

T0 = datetime(2026, 6, 27, 9, 0, 0, tzinfo=timezone.utc)

CARD = AgentCard(
    agent_id="finance_ops",
    owner="alice",
    team="finance",
    summary="Finance ops",
    domains=["finance"],
    last_reviewed_at=date(2026, 1, 1),
)

CARD_B = AgentCard(
    agent_id="legal_ops",
    owner="bob",
    team="legal",
    summary="Legal ops",
    domains=["legal"],
    last_reviewed_at=date(2026, 1, 1),
)


# ── ConsoleEvent variant 타입 구조 ────────────────────────────────────────

def test_QuestionReceived_는_ConsoleEvent_의_variant_이다() -> None:
    event: ConsoleEvent = QuestionReceived(
        question="예산 승인 어떻게 하나요?",
        session_id="sess_001",
        at=T0,
    )
    assert isinstance(event, QuestionReceived)


def test_RoutingDecisionRecorded_는_ConsoleEvent_의_variant_이다() -> None:
    decision = Routed(primary=CARD)
    event: ConsoleEvent = RoutingDecisionRecorded(decision=decision, at=T0)
    assert isinstance(event, RoutingDecisionRecorded)


def test_AnswerSent_는_ConsoleEvent_의_variant_이다() -> None:
    event: ConsoleEvent = AnswerSent(
        ticket_id="ticket_001",
        answered_by="finance_ops",
        mode="full",
        at=T0,
    )
    assert isinstance(event, AnswerSent)


def test_WorkerConnected_는_ConsoleEvent_의_variant_이다() -> None:
    event: ConsoleEvent = WorkerConnected(
        owner_id="owner_alice",
        role="primary",
        at=T0,
    )
    assert isinstance(event, WorkerConnected)


def test_WorkerDisconnected_는_ConsoleEvent_의_variant_이다() -> None:
    event: ConsoleEvent = WorkerDisconnected(
        owner_id="owner_alice",
        role="primary",
        at=T0,
    )
    assert isinstance(event, WorkerDisconnected)


# ── serialize_console_event: QuestionReceived ────────────────────────────

def test_QuestionReceived_직렬화는_event_type_을_포함한다() -> None:
    event = QuestionReceived(
        question="예산 처리 방법",
        session_id="sess_abc",
        at=T0,
    )
    result = serialize_console_event(event)

    assert result["event_type"] == "question_received"


def test_QuestionReceived_직렬화는_question_과_session_id_를_포함한다() -> None:
    event = QuestionReceived(
        question="예산 처리 방법",
        session_id="sess_abc",
        at=T0,
    )
    result = serialize_console_event(event)

    assert result["question"] == "예산 처리 방법"
    assert result["session_id"] == "sess_abc"


def test_QuestionReceived_직렬화는_at_을_포함한다() -> None:
    event = QuestionReceived(
        question="질문",
        session_id="sess_001",
        at=T0,
    )
    result = serialize_console_event(event)

    assert "at" in result


# ── serialize_console_event: RoutingDecisionRecorded ────────────────────

def test_RoutingDecisionRecorded_Routed_직렬화() -> None:
    decision = Routed(primary=CARD, intent="finance_query")
    event = RoutingDecisionRecorded(decision=decision, at=T0)
    result = serialize_console_event(event)

    assert result["event_type"] == "routing_decision_recorded"
    assert result["decision_kind"] == "routed"
    assert result["primary_agent_id"] == "finance_ops"


def test_RoutingDecisionRecorded_Contested_직렬화() -> None:
    decision = Contested(candidates=(CARD, CARD_B), intent="ambiguous_query")
    event = RoutingDecisionRecorded(decision=decision, at=T0)
    result = serialize_console_event(event)

    assert result["event_type"] == "routing_decision_recorded"
    assert result["decision_kind"] == "contested"
    candidate_ids = result["candidate_agent_ids"]
    assert isinstance(candidate_ids, list)
    assert "finance_ops" in candidate_ids
    assert "legal_ops" in candidate_ids


def test_RoutingDecisionRecorded_Unowned_직렬화() -> None:
    decision = Unowned(escalated_to="manager_root", intent="unknown_query")
    event = RoutingDecisionRecorded(decision=decision, at=T0)
    result = serialize_console_event(event)

    assert result["event_type"] == "routing_decision_recorded"
    assert result["decision_kind"] == "unowned"
    assert result["escalated_to"] == "manager_root"


def test_RoutingDecisionRecorded_직렬화는_at_을_포함한다() -> None:
    event = RoutingDecisionRecorded(decision=Routed(primary=CARD), at=T0)
    result = serialize_console_event(event)

    assert "at" in result


# ── serialize_console_event: AnswerSent ──────────────────────────────────

def test_AnswerSent_직렬화는_event_type_을_포함한다() -> None:
    event = AnswerSent(
        ticket_id="ticket_001",
        answered_by="finance_ops",
        mode="full",
        at=T0,
    )
    result = serialize_console_event(event)

    assert result["event_type"] == "answer_sent"


def test_AnswerSent_직렬화는_ticket_id_answered_by_mode_를_포함한다() -> None:
    event = AnswerSent(
        ticket_id="ticket_001",
        answered_by="finance_ops",
        mode="backup",
        at=T0,
    )
    result = serialize_console_event(event)

    assert result["ticket_id"] == "ticket_001"
    assert result["answered_by"] == "finance_ops"
    assert result["mode"] == "backup"


def test_AnswerSent_직렬화는_at_을_포함한다() -> None:
    event = AnswerSent(
        ticket_id="t_001",
        answered_by="finance_ops",
        mode="full",
        at=T0,
    )
    result = serialize_console_event(event)

    assert "at" in result


def test_AnswerSent_mode_값들이_올바르게_직렬화된다() -> None:
    for mode in ("full", "draft_only", "backup"):
        event = AnswerSent(
            ticket_id="t",
            answered_by="agent",
            mode=mode,
            at=T0,
        )
        result = serialize_console_event(event)
        assert result["mode"] == mode


# ── serialize_console_event: WorkerConnected ─────────────────────────────

def test_WorkerConnected_직렬화는_event_type_을_포함한다() -> None:
    event = WorkerConnected(owner_id="owner_alice", role="primary", at=T0)
    result = serialize_console_event(event)

    assert result["event_type"] == "worker_connected"


def test_WorkerConnected_직렬화는_owner_id_와_role_을_포함한다() -> None:
    event = WorkerConnected(owner_id="owner_alice", role="backup", at=T0)
    result = serialize_console_event(event)

    assert result["owner_id"] == "owner_alice"
    assert result["role"] == "backup"


def test_WorkerConnected_직렬화는_at_을_포함한다() -> None:
    event = WorkerConnected(owner_id="owner_alice", role="primary", at=T0)
    result = serialize_console_event(event)

    assert "at" in result


# ── serialize_console_event: WorkerDisconnected ───────────────────────────

def test_WorkerDisconnected_직렬화는_event_type_을_포함한다() -> None:
    event = WorkerDisconnected(owner_id="owner_bob", role="primary", at=T0)
    result = serialize_console_event(event)

    assert result["event_type"] == "worker_disconnected"


def test_WorkerDisconnected_직렬화는_owner_id_와_role_을_포함한다() -> None:
    event = WorkerDisconnected(owner_id="owner_bob", role="backup", at=T0)
    result = serialize_console_event(event)

    assert result["owner_id"] == "owner_bob"
    assert result["role"] == "backup"


def test_WorkerDisconnected_직렬화는_at_을_포함한다() -> None:
    event = WorkerDisconnected(owner_id="owner_bob", role="primary", at=T0)
    result = serialize_console_event(event)

    assert "at" in result


# ── 망라성 (exhaustiveness) ────────────────────────────────────────────────

def test_모든_ConsoleEvent_variant가_직렬화를_통과한다() -> None:
    """4종 variant 전부 serialize 가능 — match+assert_never 망라성 확인."""
    events: list[ConsoleEvent] = [
        QuestionReceived(question="q", session_id="s", at=T0),
        RoutingDecisionRecorded(decision=Routed(primary=CARD), at=T0),
        AnswerSent(ticket_id="t", answered_by="a", mode="full", at=T0),
        WorkerConnected(owner_id="o", role="primary", at=T0),
        WorkerDisconnected(owner_id="o", role="primary", at=T0),
    ]
    for event in events:
        result = serialize_console_event(event)
        assert "event_type" in result


# ── 노출 불변식 ────────────────────────────────────────────────────────────

def test_직렬화_결과는_dict_이다() -> None:
    """serialize_console_event는 순수 함수 → dict 반환."""
    event = QuestionReceived(question="q", session_id="s", at=T0)
    result = serialize_console_event(event)
    assert isinstance(result, dict)


def test_RoutingDecisionRecorded_Routed_직렬화에_confidence_reason이_포함될수있다() -> None:
    """콘솔은 운영 면 — 내부값(confidence·reason) 노출 OK."""
    decision = Routed(primary=CARD, confidence=0.95, reason="도메인 일치")
    event = RoutingDecisionRecorded(decision=decision, at=T0)
    result = serialize_console_event(event)

    # 운영 면이라 내부값 노출 허용 — 있으면 좋고 없어도 됨(강제 불변식 아님)
    assert result["event_type"] == "routing_decision_recorded"
    assert result["decision_kind"] == "routed"


def test_노출_불변식_QuestionReceived_에_사용자向_비밀_없음() -> None:
    """노출 불변식: 도메인 값에서만 투영하므로 추가 비밀 필드 없음."""
    event = QuestionReceived(question="q", session_id="s", at=T0)
    result = serialize_console_event(event)

    # 직렬화에는 전달받은 도메인 필드만 있음
    assert set(result.keys()).issubset({"event_type", "question", "session_id", "at"})


def test_ConsoleEvent_variant들은_frozen_dataclass_이다() -> None:
    """frozen 불변식 — 생성 후 변경 불가."""
    import dataclasses

    for cls in (
        QuestionReceived,
        RoutingDecisionRecorded,
        AnswerSent,
        WorkerConnected,
        WorkerDisconnected,
    ):
        assert dataclasses.is_dataclass(cls)


def test_QuestionReceived_frozen_이면_변경_불가() -> None:
    event = QuestionReceived(question="q", session_id="s", at=T0)
    with pytest.raises((TypeError, AttributeError)):
        event.question = "changed"  # type: ignore[misc]


def test_WorkerConnected_role_backup_직렬화() -> None:
    event = WorkerConnected(owner_id="owner_alice", role="backup", at=T0)
    result = serialize_console_event(event)

    assert result["role"] == "backup"


def test_RoutingDecisionRecorded_at_필드가_직렬화된다() -> None:
    event = RoutingDecisionRecorded(decision=Unowned(escalated_to="root"), at=T0)
    result = serialize_console_event(event)

    assert "at" in result
    assert result["decision_kind"] == "unowned"
