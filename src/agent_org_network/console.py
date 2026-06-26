"""운영자 콘솔 SSE 이벤트 직렬화 — ConsoleEvent sealed sum + serialize_console_event (T9.2(a)).

도메인 사건을 SSE 이벤트 페이로드(dict)로 투영하는 순수 함수.
render_mcp_notification·serialize_reply 정신 — 도메인 값에서만 투영, IO 0, SDK 0.

ConsoleEvent sealed sum 4+1 variant:
  - QuestionReceived: 질문 인입
  - RoutingDecisionRecorded: RoutingDecision(Routed/Contested/Unowned) 기존 sealed sum 재사용
  - AnswerSent: 답 전송
  - WorkerConnected: 워커 연결
  - WorkerDisconnected: 워커 해제

노출 불변식:
  - 콘솔은 운영 면 → 내부값(agent_id·decision 상세) 노출 OK.
  - 사용자向 비밀은 렌더 규율 — 도메인 값에서만 투영하므로 구조적으로 안 샘.
  - 사용자 채팅(OrgReply)과 다른 면(운영 면).

망라성(exhaustiveness):
  - match + assert_never로 ConsoleEvent(union) 전 variant를 망라한다.
  - 새 variant 추가 시 assert_never가 타입 에러를 냄(render_mcp_notification 정신).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, assert_never

from agent_org_network.decision import Contested, Routed, RoutingDecision, Unowned
from agent_org_network.token import WorkerRole

AnswerMode = Literal["full", "draft_only", "backup"]


# ── ConsoleEvent sealed sum ──────────────────────────────────────────────
#
# 운영자 콘솔이 SSE 피드로 노출하는 도메인 사건 4종.
# "타입이 곧 상태"(RoutingDecision·ReevalOutcome 정신) — 각 variant가 자기 필드를 들고
# serialize_console_event가 match+assert_never로 망라 직렬화.


@dataclass(frozen=True)
class QuestionReceived:
    """질문 인입 사건 — 사용자 질문이 중앙에 들어왔다."""

    question: str
    session_id: str
    at: datetime


@dataclass(frozen=True)
class RoutingDecisionRecorded:
    """라우팅 결정 사건 — Routed/Contested/Unowned 중 하나가 결정됐다.

    RoutingDecision(기존 sealed sum)을 그대로 재사용 — 새 추상 0.
    """

    decision: RoutingDecision
    at: datetime


@dataclass(frozen=True)
class AnswerSent:
    """답 전송 사건 — 워커(또는 런타임)가 답을 회신해 사용자에게 전달됐다."""

    ticket_id: str
    answered_by: str
    mode: AnswerMode
    at: datetime


@dataclass(frozen=True)
class WorkerConnected:
    """워커 연결 사건 — owner 워커가 중앙에 WebSocket으로 연결했다."""

    owner_id: str
    role: WorkerRole
    at: datetime


@dataclass(frozen=True)
class WorkerDisconnected:
    """워커 해제 사건 — owner 워커의 WebSocket 연결이 끊겼다."""

    owner_id: str
    role: WorkerRole
    at: datetime


ConsoleEvent = (
    QuestionReceived
    | RoutingDecisionRecorded
    | AnswerSent
    | WorkerConnected
    | WorkerDisconnected
)


# ── 직렬화 순수 함수 ─────────────────────────────────────────────────────
#
# serialize_console_event(event) -> dict:
#   도메인 사건 → SSE 이벤트 페이로드(dict). 순수 함수 — IO 0·SDK 0.
#   match+assert_never로 ConsoleEvent 전 variant 망라.
#   노출 불변식: 콘솔은 운영 면이라 내부값(agent_id·decision) OK.
#   실 SSE 스트리밍·브라우저 push는 게이트 밖(T9.2(c)).


def _serialize_routing_decision(decision: RoutingDecision) -> dict[str, object]:
    """RoutingDecision sealed sum → dict 투영(Routed/Contested/Unowned 망라)."""
    match decision:
        case Routed() as r:
            return {
                "decision_kind": "routed",
                "primary_agent_id": r.primary.agent_id,
                "confidence": r.confidence,
                "reason": r.reason,
                "intent": r.intent,
            }
        case Contested() as c:
            return {
                "decision_kind": "contested",
                "candidate_agent_ids": [card.agent_id for card in c.candidates],
                "reason": c.reason,
                "intent": c.intent,
            }
        case Unowned() as u:
            return {
                "decision_kind": "unowned",
                "escalated_to": u.escalated_to,
                "reason": u.reason,
                "intent": u.intent,
            }
        case _ as never:
            assert_never(never)


def serialize_console_event(event: ConsoleEvent) -> dict[str, object]:
    """ConsoleEvent → SSE 이벤트 페이로드(dict) — 순수 함수·망라성 보장.

    match+assert_never로 ConsoleEvent 전 variant를 망라한다(render_mcp_notification 정신).
    새 variant 추가 시 pyright가 assert_never 도달 가능으로 에러를 낸다.

    노출 불변식: 콘솔은 운영 면이라 내부값 노출 OK. 도메인 값에서만 투영하므로
    사용자向 비밀이 구조적으로 안 샘(Notification이 식별자만 든 것과 같은 결).
    """
    match event:
        case QuestionReceived(question=q, session_id=sid, at=at):
            return {
                "event_type": "question_received",
                "question": q,
                "session_id": sid,
                "at": at.isoformat(),
            }
        case RoutingDecisionRecorded(decision=decision, at=at):
            payload: dict[str, object] = {
                "event_type": "routing_decision_recorded",
                "at": at.isoformat(),
            }
            payload.update(_serialize_routing_decision(decision))
            return payload
        case AnswerSent(ticket_id=tid, answered_by=by, mode=mode, at=at):
            return {
                "event_type": "answer_sent",
                "ticket_id": tid,
                "answered_by": by,
                "mode": mode,
                "at": at.isoformat(),
            }
        case WorkerConnected(owner_id=oid, role=role, at=at):
            return {
                "event_type": "worker_connected",
                "owner_id": oid,
                "role": role,
                "at": at.isoformat(),
            }
        case WorkerDisconnected(owner_id=oid, role=role, at=at):
            return {
                "event_type": "worker_disconnected",
                "owner_id": oid,
                "role": role,
                "at": at.isoformat(),
            }
        case _ as never:
            assert_never(never)
