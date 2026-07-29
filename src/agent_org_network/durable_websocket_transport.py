"""Durable dispatch의 실제 WebSocket 전송 어댑터.

Legacy ``WebSocketDispatcher``의 메모리 작업 큐는 사용하지 않는다. 이 모듈은
인증된 연결 세션 레지스트리, ``PushWork`` 송신, ``SubmitAnswer``의 durable
ingestion UoW 인계만 소유한다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from agent_org_network.approval import AnswerCandidate, FinalizationCandidate, NoApprovalRequired
from agent_org_network.durable_dispatch_delivery import (
    Delivered,
    DispatchFrame,
    DispatchOutcome,
    Undeliverable,
)
from agent_org_network.question_request import RouteTarget
from agent_org_network.sqlite_durable_answer_ingestion_uow import (
    DurableAnswerAccepted,
    DurableWorkerAnswerCommand,
)
from agent_org_network.transport import PushWork, SubmitAnswer, TicketFrame
from agent_org_network.worker_authorization import WorkerConnectionPrincipal

SendPush = Callable[[PushWork], None]


class DurableAnswerIngestion(Protocol):
    def accept(
        self,
        *,
        submitter: WorkerConnectionPrincipal,
        command: DurableWorkerAnswerCommand,
    ) -> DurableAnswerAccepted: ...


@dataclass(frozen=True)
class _Connection:
    principal: WorkerConnectionPrincipal
    send: SendPush


class DurableWebSocketDispatchChannel:
    """실 WS 연결에 PushWork를 보내고 답을 durable terminal authority에 인계한다."""

    def __init__(
        self,
        *,
        ingestion: DurableAnswerIngestion,
        policy_version: str,
        authorization: "CurrentWorkerAuthorization",
        answer_target_reader: "DurableAnswerTargetReader",
        current_owner: "CurrentOwner",
        principal_is_current: "CurrentPrincipal",
    ) -> None:
        if not policy_version.strip():
            raise ValueError("policy_version은 비어 있을 수 없습니다.")
        self._ingestion = ingestion
        self._policy_version = policy_version
        self._authorization = authorization
        self._answer_target_reader = answer_target_reader
        self._current_owner = current_owner
        self._principal_is_current = principal_is_current
        self._connections: dict[tuple[str, str], _Connection] = {}
        self._lock = threading.RLock()

    def register(
        self, principal: WorkerConnectionPrincipal, send: SendPush
    ) -> None:
        with self._lock:
            self._connections[(principal.org_id, principal.owner_id)] = _Connection(
                principal, send
            )

    def disconnect(self, principal: WorkerConnectionPrincipal) -> None:
        with self._lock:
            key = (principal.org_id, principal.owner_id)
            current = self._connections.get(key)
            if current is not None and current.principal == principal:
                del self._connections[key]

    def deliver(self, frame: DispatchFrame) -> DispatchOutcome:
        with self._lock:
            connection = self._connections.get((frame.org_id, frame.owner_id))
            if connection is None:
                return Undeliverable(reason_code="no_connected_worker")
            owner = self._current_owner(frame.org_id, frame.agent_id)
            if (
                owner != frame.owner_id
                or not self._principal_is_current(connection.principal)
                or self._authorization.authorize_delivery(
                    connection.principal,
                    "worker.submit",
                    agent_card_id=frame.agent_id,
                    current_owner_id=owner or "",
                )
                != "allowed"
            ):
                return Undeliverable(reason_code="channel_error")
            push = PushWork(
                ticket=TicketFrame(
                    ticket_id=frame.ticket_id,
                    agent_id=frame.agent_id,
                    question=frame.question,
                    enqueued_at=frame.enqueued_at,
                    context=frame.context_snapshot,
                )
            )
            try:
                connection.send(push)
            except Exception:
                return Undeliverable(reason_code="channel_error")
            return Delivered()

    def submit(
        self,
        principal: WorkerConnectionPrincipal,
        submit: SubmitAnswer,
    ) -> DurableAnswerAccepted:
        frame = self._answer_target_reader.read(submit.ticket_id)
        owner = (
            self._current_owner(frame.org_id, frame.agent_id) if frame is not None else None
        )
        if (
            frame is None
            or not self._principal_is_current(principal)
            or owner != principal.owner_id
            or frame.owner_id != principal.owner_id
            or self._authorization.authorize_delivery(
                principal,
                "worker.submit",
                agent_card_id=frame.agent_id,
                current_owner_id=owner or "",
            )
            != "allowed"
        ):
            from agent_org_network.sqlite_durable_answer_ingestion_uow import (
                DurableAnswerIngestionConflict,
            )

            raise DurableAnswerIngestionConflict(
                "현재 인증 세션에 결박된 durable delivery가 없습니다."
            )
        answer = submit.answer
        command = DurableWorkerAnswerCommand(
            ticket_id=frame.ticket_id,
            request_id=frame.request_id,
            expected_request_revision=frame.expected_request_revision,
            handoff=FinalizationCandidate(
                request_id=frame.request_id,
                expected_revision=frame.expected_request_revision,
                attempt=frame.attempt,
                route=RouteTarget(
                    intent=frame.intent,
                    agent_id=frame.agent_id,
                    requires_approval=False,
                ),
                candidate=AnswerCandidate(
                    text=answer.text, sources=answer.sources, mode=answer.mode
                ),
                approval_evaluation=NoApprovalRequired(
                    policy_version=self._policy_version
                ),
            ),
        )
        accepted = self._ingestion.accept(submitter=principal, command=command)
        return accepted


class DurableAnswerTargetReader(Protocol):
    def read(self, ticket_id: str) -> DispatchFrame | None: ...


class CurrentOwner(Protocol):
    def __call__(self, org_id: str, agent_card_id: str) -> str | None: ...


class CurrentWorkerAuthorization(Protocol):
    def authorize_delivery(
        self,
        principal: object,
        action: object,
        *,
        agent_card_id: object,
        current_owner_id: object,
    ) -> str: ...


class CurrentPrincipal(Protocol):
    def __call__(self, principal: WorkerConnectionPrincipal) -> bool: ...


__all__ = ["DurableWebSocketDispatchChannel"]
