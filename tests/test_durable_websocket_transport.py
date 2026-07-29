# pyright: reportPrivateUsage=false
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from agent_org_network.durable_dispatch_delivery import Delivered, DispatchFrame
from agent_org_network.durable_websocket_transport import DurableWebSocketDispatchChannel
from agent_org_network.server import create_durable_worker_app
from agent_org_network.sqlite_durable_answer_ingestion_uow import (
    DurableAnswerAccepted,
    DurableAnswerIngestionConflict,
    DurableWorkerAnswerCommand,
)
from agent_org_network.transport import (
    AnswerFrame,
    PushWork,
    SubmitAnswer,
    WebSocketDispatcher,
)
from agent_org_network.worker_authorization import WorkerConnectionPrincipal

NOW = datetime(2026, 7, 27, tzinfo=UTC)


class _Ingestion:
    def __init__(self) -> None:
        self.calls: list[tuple[WorkerConnectionPrincipal, DurableWorkerAnswerCommand]] = []

    def accept(
        self,
        submitter: WorkerConnectionPrincipal,
        command: DurableWorkerAnswerCommand,
    ) -> DurableAnswerAccepted:
        self.calls.append((submitter, command))
        return DurableAnswerAccepted("r", command.ticket_id, command.request_id, "a", 3)


class _Authorization:
    outcome = "allowed"

    def authorize_delivery(self, *_args: object, **_kwargs: object) -> str:
        return self.outcome


class _Reader:
    def read(self, ticket_id: str) -> DispatchFrame | None:
        del ticket_id
        return _frame()


def _channel(ingestion: _Ingestion) -> DurableWebSocketDispatchChannel:
    def current_owner(org_id: str, agent_card_id: str) -> str | None:
        del org_id, agent_card_id
        return "owner-a"

    def principal_is_current(principal: WorkerConnectionPrincipal) -> bool:
        del principal
        return True

    return DurableWebSocketDispatchChannel(
        ingestion=ingestion,
        policy_version="v1",
        authorization=_Authorization(),
        answer_target_reader=_Reader(),
        current_owner=current_owner,
        principal_is_current=principal_is_current,
    )


def _principal(owner: str = "owner-a") -> WorkerConnectionPrincipal:
    return WorkerConnectionPrincipal(
        org_id="org-a",
        owner_id=owner,
        credential_id="cred-a",
        credential_generation=1,
        role="primary",
        connection_epoch="epoch-a",
    )


def _frame() -> DispatchFrame:
    return DispatchFrame(
        ticket_id="ticket-a",
        request_id="request-a",
        org_id="org-a",
        attempt=1,
        lease_epoch=1,
        agent_id="card-a",
        owner_id="owner-a",
        question="질문",
        context_snapshot="문맥",
        session_id="session-a",
        intent="refund",
        expected_request_revision=2,
        enqueued_at=NOW,
    )


def test_실_채널은_연결된_인증_세션에_PushWork를_보낸다() -> None:
    ingestion = _Ingestion()
    channel = _channel(ingestion)
    sent: list[PushWork] = []
    channel.register(_principal(), sent.append)

    assert channel.deliver(_frame()) == Delivered()
    assert len(sent) == 1
    assert isinstance(sent[0], PushWork)
    assert sent[0].ticket.enqueued_at == NOW
    assert sent[0].ticket.context == "문맥"


def test_SubmitAnswer는_delivery_세션에_결박되어_durable_ingestion만_호출한다() -> None:
    ingestion = _Ingestion()
    channel = _channel(ingestion)
    principal = _principal()
    channel.register(principal, lambda _frame: None)
    assert channel.deliver(_frame()) == Delivered()

    accepted = channel.submit(
        principal,
        SubmitAnswer(ticket_id="ticket-a", answer=AnswerFrame(text="답", sources=("근거",))),
    )

    assert accepted.ticket_id == "ticket-a"
    assert len(ingestion.calls) == 1
    _, command = ingestion.calls[0]
    assert command.request_id == "request-a"
    assert command.expected_request_revision == 2
    assert command.handoff.route.intent == "refund"


def test_다른_인증_세션의_SubmitAnswer는_write_전에_거부한다() -> None:
    ingestion = _Ingestion()
    channel = _channel(ingestion)
    channel.register(_principal(), lambda _frame: None)
    channel.deliver(_frame())

    with pytest.raises(DurableAnswerIngestionConflict):
        channel.submit(
            _principal("attacker"),
            SubmitAnswer(ticket_id="ticket-a", answer=AnswerFrame(text="탈취")),
        )
    assert ingestion.calls == []


def test_새_channel과_reconnect_principal도_durable_read로_느린_답을_수용한다() -> None:
    ingestion = _Ingestion()
    channel = _channel(ingestion)
    reconnect = _principal().model_copy(update={"connection_epoch": "epoch-new"})

    channel.submit(
        reconnect,
        SubmitAnswer(ticket_id="ticket-a", answer=AnswerFrame(text="느린 답")),
    )

    assert len(ingestion.calls) == 1


def test_revoke나_current_owner_drift면_push와_submit_write가_0이다() -> None:
    def drifted_owner(org_id: str, agent_card_id: str) -> str | None:
        del org_id, agent_card_id
        return "other-owner"

    def principal_is_current(principal: WorkerConnectionPrincipal) -> bool:
        del principal
        return False

    ingestion = _Ingestion()
    authorization = _Authorization()
    authorization.outcome = "denied"
    channel = DurableWebSocketDispatchChannel(
        ingestion=ingestion,
        policy_version="v1",
        authorization=authorization,
        answer_target_reader=_Reader(),
        current_owner=drifted_owner,
        principal_is_current=principal_is_current,
    )
    sent: list[PushWork] = []
    channel.register(_principal(), sent.append)

    assert channel.deliver(_frame()).kind == "undeliverable"
    with pytest.raises(DurableAnswerIngestionConflict):
        channel.submit(
            _principal(),
            SubmitAnswer(ticket_id="ticket-a", answer=AnswerFrame(text="거부")),
        )
    assert sent == []
    assert ingestion.calls == []


def test_durable_worker_app은_legacy_submit_없이_실_WS로_push와_submit을_왕복한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_calls = 0

    def legacy_submit(*_args: object, **_kwargs: object) -> None:
        nonlocal legacy_calls
        legacy_calls += 1

    monkeypatch.setattr(WebSocketDispatcher, "submit", legacy_submit)
    ingestion = _Ingestion()
    channel = _channel(ingestion)
    principal = _principal()
    app = create_durable_worker_app(channel, lambda _register: principal)

    with TestClient(app).websocket_connect("/worker") as ws:
        ws.send_json({"type": "register_worker", "owner_id": "owner-a", "token": "secret"})
        assert ws.receive_json()["type"] == "welcome"
        assert channel.deliver(_frame()) == Delivered()
        pushed = ws.receive_json()
        assert pushed["type"] == "push_work"
        ws.send_json(
            {
                "type": "submit_answer",
                "ticket_id": "ticket-a",
                "answer": {"text": "실 답", "sources": ["근거"], "mode": "full"},
            }
        )

    assert len(ingestion.calls) == 1
    assert legacy_calls == 0
