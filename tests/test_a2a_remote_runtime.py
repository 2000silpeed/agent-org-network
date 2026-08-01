"""ADR 0074 A2A Remote Runtime 코어의 결정론 단위 테스트."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import assert_never

import pytest
from pydantic import ValidationError

from agent_org_network.a2a_remote_runtime import (
    A2ACompletedText,
    A2AInvocationPort,
    A2AProtocolViolation,
    A2ARemoteRejected,
    A2ARemoteRuntime,
    A2ARemoteRuntimeFailure,
    A2ARemoteRuntimeProfile,
    A2ARemoteUnavailable,
)
from agent_org_network.agent_card import AgentCard
from agent_org_network.owner_device_key_store import OwnerInstallationPublicBindingV1
from agent_org_network.runtime import Answer
from agent_org_network.transport import PushWork, TicketFrame
from agent_org_network.worker import RuntimeFailed, WorkerLogic, _serve  # pyright: ignore[reportPrivateUsage]


_TS = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
_DIGEST = "a" * 64


def _binding(agent_card_id: str = "refund_runtime") -> OwnerInstallationPublicBindingV1:
    return OwnerInstallationPublicBindingV1(
        central_origin="https://central.example.com",
        org_id="support",
        owner_user_id="owner_lee",
        agent_card_id=agent_card_id,
        agent_card_revision=4,
        agent_card_digest=_DIGEST,
        device_key_thumbprint="A" * 43,
    )


def _profile(**changes: object) -> A2ARemoteRuntimeProfile:
    values: dict[str, object] = {
        "binding": _binding(),
        "service_endpoint": "https://a2a.example.com",
        "remote_card_sha256": "b" * 64,
        "credential_ref": "keychain:a2a-remote-runtime",
    }
    values.update(changes)
    return A2ARemoteRuntimeProfile.model_validate(values)


def _card(agent_id: str = "refund_runtime") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner="owner_lee",
        team="support",
        summary="환불 절차를 안내합니다.",
        domains=["환불"],
        last_reviewed_at=_TS.date(),
    )


@dataclass
class _RecordingInvocation(A2AInvocationPort):
    outcome: A2ACompletedText | A2ARemoteRejected | A2ARemoteUnavailable | A2AProtocolViolation
    calls: list[tuple[A2ARemoteRuntimeProfile, str, str | None]]

    def invoke(
        self,
        *,
        profile: A2ARemoteRuntimeProfile,
        question: str,
        context: str | None,
    ) -> A2ACompletedText | A2ARemoteRejected | A2ARemoteUnavailable | A2AProtocolViolation:
        self.calls.append((profile, question, context))
        return self.outcome


@dataclass
class _SequencedInvocation(A2AInvocationPort):
    outcomes: list[A2ACompletedText | A2ARemoteRejected | A2ARemoteUnavailable | A2AProtocolViolation]

    def invoke(
        self,
        *,
        profile: A2ARemoteRuntimeProfile,
        question: str,
        context: str | None,
    ) -> A2ACompletedText | A2ARemoteRejected | A2ARemoteUnavailable | A2AProtocolViolation:
        del profile, question, context
        return self.outcomes.pop(0)


@dataclass
class _FakeSocket:
    received: list[str]
    sent: list[str]

    def recv(self) -> str:
        if not self.received:
            raise EOFError
        return self.received.pop(0)

    def send(self, frame: str) -> None:
        self.sent.append(frame)


def test_A2A_Remote_Runtime_Profile은_frozen_extra_forbid_정확한_binding이다() -> None:
    profile = _profile()

    assert profile.runtime_kind == "a2a_remote"
    assert profile.remote_card_path == "/.well-known/agent-card.json"
    assert profile.protocol_version == "1.0"
    assert profile.protocol_binding == "HTTP+JSON"
    assert profile.binding == _binding()
    with pytest.raises(ValidationError):
        profile.service_endpoint = "https://other.example.com"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        A2ARemoteRuntimeProfile.model_validate(
            {
                **profile.model_dump(),
                "unknown": "not-accepted",
            }
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://a2a.example.com",
        "https://a2a.example.com/",
        "https://a2a.example.com/a2a/",
        "https://a2a.example.com/a2a//v1",
        "https://a2a.example.com/a2a/./v1",
        "https://a2a.example.com/a2a/../v1",
        "https://a2a.example.com/a2a/%76%31",
        "https://a2a.example.com/a2a/v1%2Ftasks",
        "https://a2a.example.com/a2a\\v1",
        "https://a2a.example.com/a2a/\x1fv1",
        f"https://a2a.example.com/{'a' * 65}",
        "https://a2a.example.com/?token=secret",
        "https://user:secret@a2a.example.com",
        "https://a2a.example.com/#fragment",
        "https://a2a.example.com:443",
    ],
)
def test_A2A_Remote_Runtime_Profile은_비정규_HTTPS_endpoint를_거부한다(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        _profile(service_endpoint=endpoint)


def test_A2A_Remote_Runtime_Profile은_bounded_literal_path를_허용한다() -> None:
    profile = _profile(service_endpoint="https://a2a.example.com/a2a/v1")

    assert profile.service_endpoint == "https://a2a.example.com/a2a/v1"


@pytest.mark.parametrize(
    "changes",
    [
        {"remote_card_sha256": "B" * 64},
        {"credential_ref": "raw secret value"},
        {"timeout_seconds": 0},
        {"max_response_bytes": 0},
    ],
)
def test_A2A_Remote_Runtime_Profile은_digest_opaque_ref와_제한값을_검증한다(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _profile(**changes)


def test_completed_text는_질문과_context만_전달하고_Answer로_투영한다() -> None:
    profile = _profile()
    invocation = _RecordingInvocation(A2ACompletedText(text="원격 답변"), [])
    runtime = A2ARemoteRuntime(profile=profile, invocation=invocation)

    answer = runtime.answer(
        "환불 기한은?",
        _card(),
        context="Question User는 지난주에 구매했습니다.",
        grounding="이 내부 grounding은 원격으로 나가면 안 됩니다.",
    )

    assert answer == Answer(text="원격 답변", sources=(), mode="full", snapshot_sha=None)
    assert invocation.calls == [
        (profile, "환불 기한은?", "Question User는 지난주에 구매했습니다.")
    ]


def test_내부_Agent_Card와_binding이_다르면_원격_호출하지_않는다() -> None:
    invocation = _RecordingInvocation(A2ACompletedText(text="호출되면 안 됨"), [])
    runtime = A2ARemoteRuntime(profile=_profile(), invocation=invocation)

    with pytest.raises(A2ARemoteRuntimeFailure) as raised:
        runtime.answer("환불?", _card(agent_id="other_runtime"))

    assert raised.value.code == "a2a_binding_mismatch"
    assert invocation.calls == []


@pytest.mark.parametrize(
    "outcome, expected_code",
    [
        (A2ARemoteRejected(), "a2a_remote_rejected"),
        (A2ARemoteUnavailable(), "a2a_remote_unavailable"),
        (A2AProtocolViolation(), "a2a_protocol_violation"),
        (A2ACompletedText(text=" \n "), "a2a_protocol_violation"),
    ],
)
def test_A2A_실패는_redacted_typed_failure가_된다(
    outcome: A2ACompletedText | A2ARemoteRejected | A2ARemoteUnavailable | A2AProtocolViolation,
    expected_code: str,
) -> None:
    endpoint = "https://a2a.example.com"
    secret = "keychain:a2a-remote-runtime"
    runtime = A2ARemoteRuntime(
        profile=_profile(service_endpoint=endpoint, credential_ref=secret),
        invocation=_RecordingInvocation(outcome, []),
    )

    with pytest.raises(A2ARemoteRuntimeFailure) as raised:
        runtime.answer("환불?", _card())

    assert raised.value.code == expected_code
    rendered = f"{raised.value!s} {raised.value!r}"
    assert rendered == f"{expected_code} A2ARemoteRuntimeFailure(code='{expected_code}')"
    assert endpoint not in rendered
    assert secret not in rendered


def test_completed_text가_profile_response_limit을_넘으면_Answer로_투영하지_않는다() -> None:
    runtime = A2ARemoteRuntime(
        profile=_profile(max_response_bytes=1_024),
        invocation=_RecordingInvocation(A2ACompletedText(text="x" * 1_025), []),
    )

    with pytest.raises(A2ARemoteRuntimeFailure) as raised:
        runtime.answer("환불?", _card())

    assert raised.value.code == "a2a_protocol_violation"


def test_A2A_실패는_HITL_초안을_만들거나_SubmitAnswer하지_않고_다음_작업은_계속_처리한다() -> None:
    card = _card()
    invocation = _RecordingInvocation(A2ARemoteUnavailable(), [])
    runtime = A2ARemoteRuntime(profile=_profile(), invocation=invocation)
    worker = WorkerLogic(owner_id="owner_lee", cards={card.agent_id: card}, runtime=runtime)
    failed_push = PushWork(
        ticket=TicketFrame(
            ticket_id="failure-ticket",
            agent_id=card.agent_id,
            question="환불?",
            enqueued_at=_TS,
            hitl=True,
        )
    )

    failed = worker.handle_push_work(failed_push)
    assert failed == RuntimeFailed(ticket_id="failure-ticket", code="a2a_remote_unavailable")
    assert worker.pending_draft("failure-ticket") is None

    invocation.outcome = A2ACompletedText(text="다음 작업 답")
    successful_push = PushWork(
        ticket=TicketFrame(
            ticket_id="success-ticket",
            agent_id=card.agent_id,
            question="다음 질문",
            enqueued_at=_TS,
        )
    )
    submit = worker.handle_push_work(successful_push)

    assert not isinstance(submit, RuntimeFailed)
    assert submit is not None
    assert submit.ticket_id == "success-ticket"
    assert submit.answer.text == "다음 작업 답"


def test_수신_루프는_A2A_실패를_redacted_code로만_기록하고_다음_PushWork를_처리한다(
    caplog: pytest.LogCaptureFixture,
) -> None:
    card = _card()
    endpoint = "https://a2a.example.com"
    secret = "keychain:a2a-remote-runtime"
    worker = WorkerLogic(
        owner_id="owner_lee",
        cards={card.agent_id: card},
        runtime=A2ARemoteRuntime(
            profile=_profile(service_endpoint=endpoint, credential_ref=secret),
            invocation=_SequencedInvocation([A2ARemoteUnavailable(), A2ACompletedText(text="회복 답")]),
        ),
    )
    first = PushWork(
        ticket=TicketFrame(
            ticket_id="failure-ticket",
            agent_id=card.agent_id,
            question="첫 질문",
            enqueued_at=_TS,
        )
    )
    second = PushWork(
        ticket=TicketFrame(
            ticket_id="success-ticket",
            agent_id=card.agent_id,
            question="둘째 질문",
            enqueued_at=_TS,
        )
    )
    socket = _FakeSocket(
        received=[first.model_dump_json(), second.model_dump_json()],
        sent=[],
    )

    with caplog.at_level(logging.WARNING), pytest.raises(EOFError):
        _serve(socket, worker)

    assert len(socket.sent) == 1
    assert "회복 답" in socket.sent[0]
    logs = caplog.text
    assert "a2a_remote_unavailable" in logs
    assert endpoint not in logs
    assert secret not in logs


def test_A2A_호출결과는_sealed_outcome이다() -> None:
    """새 결과 종류가 추가되면 이 분기에서 갱신하도록 코어 합을 닫는다."""
    for outcome in (
        A2ACompletedText(text="답"),
        A2ARemoteRejected(),
        A2ARemoteUnavailable(),
        A2AProtocolViolation(),
    ):
        match outcome:
            case A2ACompletedText() | A2ARemoteRejected() | A2ARemoteUnavailable() | A2AProtocolViolation():
                pass
            case _ as unreachable:
                assert_never(unreachable)
