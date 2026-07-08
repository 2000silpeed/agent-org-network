"""메일 질문 채널(Phase 15 A축) 결정론 테스트 — A1 추출·A2 회신 투영·A3 포트.

ADR 0040(발신자=약신원·회신 목적지 전용) 정신: 픽스처는 전부 합성(실 PII 0). 실 네트워크·
실 IMAP·비결정 0 — `FakeMailChannel`(A3)·순수 함수(A1/A2)만 돈다.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_org_network.ask_org import Answered, AskOrg, Pending
from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.classifier import FakeClassifier
from agent_org_network.dispatch import LocalRuntimeDispatcher
from agent_org_network.mail import (
    AmbiguousMail,
    FakeMailChannel,
    ImapMailChannel,
    InboundMail,
    MailQuestion,
    MailReply,
    NotAQuestion,
    OutboundMail,
    extract_mail_question,
    project_reply_to_mail,
)
from agent_org_network.mcp_server import reply_to_mcp_text
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import StubRuntime
from agent_org_network.user import User

_NEUTRAL_SUBJECT = "[조직에 묻기] 답변"

# 메일 회신에 절대 새면 안 되는 조직 내부값 부분문자열(subject·body 둘 다 대상).
_LEAKY_SUBSTRINGS = (
    "confidence",
    "candidates",
    "manager_id",
    "ticket_id",
    "reason",
    "intent",
    "escalated_to",
    "primary",
)


def _assert_no_leak(reply: MailReply) -> None:
    combined = f"{reply.subject} {reply.body}"
    for keyword in _LEAKY_SUBSTRINGS:
        assert keyword not in combined, f"leak: {keyword!r} in {combined!r}"


# ── A1: extract_mail_question ────────────────────────────────────────────────


def test_순수_질문_텍스트는_MailQuestion() -> None:
    payload: dict[str, Any] = {"text": "환불 절차가 어떻게 되나요?"}
    result = extract_mail_question(payload)
    assert result == MailQuestion(text="환불 절차가 어떻게 되나요?")


def test_인용_답장_몸통만_남기고_추출() -> None:
    payload: dict[str, Any] = {
        "text": (
            "환불 절차가 어떻게 되나요?\n"
            "\n"
            "On Mon, Jan 5, 2026 at 3:00 PM, Kim <kim@example.com> wrote:\n"
            "> 이전 메일 내용입니다.\n"
            "> 추가 인용 줄입니다.\n"
        )
    }
    result = extract_mail_question(payload)
    assert isinstance(result, MailQuestion)
    assert result.text == "환불 절차가 어떻게 되나요?"
    assert "이전 메일" not in result.text


def test_서명_제거() -> None:
    payload: dict[str, Any] = {
        "text": "환불 절차가 어떻게 되나요?\n-- \n김철수 드림\n010-1234-5678"
    }
    result = extract_mail_question(payload)
    assert isinstance(result, MailQuestion)
    assert result.text == "환불 절차가 어떻게 되나요?"
    assert "010-1234-5678" not in result.text


def test_빈_본문은_NotAQuestion_empty() -> None:
    payload: dict[str, Any] = {"text": "   "}
    assert extract_mail_question(payload) == NotAQuestion(reason="empty")


def test_본문_키_전혀_없으면_NotAQuestion_empty() -> None:
    assert extract_mail_question({}) == NotAQuestion(reason="empty")


def test_FYI_메일은_필터() -> None:
    payload: dict[str, Any] = {
        "text": "FYI 이번 주 공지사항 공유드립니다. 회신 불필요합니다."
    }
    assert extract_mail_question(payload) == NotAQuestion(reason="fyi")


def test_일정_조율_메일은_필터() -> None:
    payload: dict[str, Any] = {
        "text": "다음 주 회의 일정 조율 부탁드립니다. 목요일 오후 3시 어떠세요?"
    }
    assert extract_mail_question(payload) == NotAQuestion(reason="scheduling")


def test_자동응답_메일은_필터() -> None:
    payload: dict[str, Any] = {"text": "부재중입니다. 복귀 후 회신드리겠습니다."}
    assert extract_mail_question(payload) == NotAQuestion(reason="auto_reply")


def test_다주제_메일은_AmbiguousMail_multi_topic() -> None:
    payload: dict[str, Any] = {
        "text": "환불은 어떻게 되나요? 그리고 계약서는 언제 검토되나요?"
    }
    result = extract_mail_question(payload)
    assert isinstance(result, AmbiguousMail)
    assert result.reason == "multi_topic"


def test_불명확_진술은_AmbiguousMail_unclear() -> None:
    payload: dict[str, Any] = {"text": "이번 건은 조금 애매한 것 같습니다."}
    result = extract_mail_question(payload)
    assert isinstance(result, AmbiguousMail)
    assert result.reason == "unclear"


def test_fullwidth_물음표만_있어도_질문으로_인식() -> None:
    """"환불？"처럼 fullwidth 물음표(U+FF1F)만 있는 짧은 CJK 질문이 unclear로 새지 않는다."""
    payload: dict[str, Any] = {"text": "환불？"}
    result = extract_mail_question(payload)
    assert result == MailQuestion(text="환불？")


def test_FYI와_질문이_함께_있으면_질문이_우선한다() -> None:
    """약신호(fyi)는 질문 마커가 있으면 필터링하지 않는다(실 질문 드롭 방지)."""
    payload: dict[str, Any] = {"text": "FYI, 환불 절차 어떻게 되나요?"}
    result = extract_mail_question(payload)
    assert result == MailQuestion(text="FYI, 환불 절차 어떻게 되나요?")


def test_순수_FYI는_질문_마커_없으면_필터() -> None:
    payload: dict[str, Any] = {"text": "FYI 이번 주 공지사항 공유드립니다."}
    assert extract_mail_question(payload) == NotAQuestion(reason="fyi")


def test_회신불필요는_강신호라_질문이_있어도_필터() -> None:
    payload: dict[str, Any] = {"text": "회신 불필요합니다. 환불 절차가 어떻게 되나요?"}
    result = extract_mail_question(payload)
    assert isinstance(result, NotAQuestion)
    assert result.reason == "fyi"


def test_URL_쿼리스트링의_물음표는_다주제_오탐하지_않는다() -> None:
    payload: dict[str, Any] = {
        "text": "이 링크 http://example.com/page?a=1 어떻게 되나요?"
    }
    result = extract_mail_question(payload)
    assert isinstance(result, MailQuestion)


def test_연속_물음표_강조는_다주제_오탐하지_않는다() -> None:
    payload: dict[str, Any] = {"text": "정말 환불되나요??"}
    result = extract_mail_question(payload)
    assert isinstance(result, MailQuestion)


def test_hr_구분선은_서명으로_오인해_본문이_잘리지_않는다() -> None:
    payload: dict[str, Any] = {"text": "환불 절차가 어떻게 되나요?\n\n--\n\n감사합니다."}
    result = extract_mail_question(payload)
    assert isinstance(result, MailQuestion)
    assert "감사합니다" in result.text


def test_벤더_payload_키_교체_폴백_body_content() -> None:
    payload: dict[str, Any] = {"body": {"content": "환불 절차가 어떻게 되나요?"}}
    assert extract_mail_question(payload) == MailQuestion(text="환불 절차가 어떻게 되나요?")


def test_벤더_payload_키_교체_폴백_content_top_level() -> None:
    payload: dict[str, Any] = {"content": "환불 절차가 어떻게 되나요?"}
    assert extract_mail_question(payload) == MailQuestion(text="환불 절차가 어떻게 되나요?")


def test_벤더_payload_키_교체_폴백_text_plain() -> None:
    payload: dict[str, Any] = {"text/plain": "환불 절차가 어떻게 되나요?"}
    assert extract_mail_question(payload) == MailQuestion(text="환불 절차가 어떻게 되나요?")


def test_벤더_payload_키_교체_폴백_body_문자열() -> None:
    payload: dict[str, Any] = {"body": "환불 절차가 어떻게 되나요?"}
    assert extract_mail_question(payload) == MailQuestion(text="환불 절차가 어떻게 되나요?")


def test_HTML_태그_및_엔티티_정리() -> None:
    payload: dict[str, Any] = {"text": "<p>환불 절차가 어떻게 되나요?</p>&nbsp;"}
    result = extract_mail_question(payload)
    assert isinstance(result, MailQuestion)
    assert "<p>" not in result.text
    assert result.text == "환불 절차가 어떻게 되나요?"


# ── A2: project_reply_to_mail ────────────────────────────────────────────────


def test_Answered_회신_subject는_원제목_에코() -> None:
    reply = Answered(
        text="환불 가능합니다.",
        answered_by=("o1", "cs_ops"),
        mode="full",
        sources=("정책 문서",),
    )
    mail_reply = project_reply_to_mail(reply, original_subject="환불 문의")
    assert mail_reply.subject == "Re: 환불 문의"
    assert "환불 가능합니다." in mail_reply.body
    _assert_no_leak(mail_reply)


def test_Answered_회신_subject_없으면_중립_고정() -> None:
    reply = Answered(text="환불 가능합니다.", answered_by=("o1", "cs_ops"), mode="full")
    mail_reply = project_reply_to_mail(reply)
    assert mail_reply.subject == _NEUTRAL_SUBJECT
    _assert_no_leak(mail_reply)


@pytest.mark.parametrize(
    "reply",
    [
        Pending(kind="contested", message="담당을 확인하고 있어요."),
        Pending(kind="unowned", message="매니저에게 전달했어요."),
        Pending(kind="dispatched", message="답변 준비되면 알림드릴게요.", tracking="tok-123"),
    ],
)
def test_Pending_각_kind별_subject_에코와_중립(reply: Pending) -> None:
    with_subject = project_reply_to_mail(reply, original_subject="문의")
    without_subject = project_reply_to_mail(reply)
    assert with_subject.subject == "Re: 문의"
    assert without_subject.subject == _NEUTRAL_SUBJECT
    _assert_no_leak(with_subject)
    _assert_no_leak(without_subject)


def test_subject는_kind에_따라_분기하지_않는다() -> None:
    """Answered/Pending·kind가 달라도 같은 original_subject면 subject가 동일해야 한다."""
    original = "문의"
    replies: tuple[Answered | Pending, ...] = (
        Answered(text="t", answered_by=("o", "a"), mode="full"),
        Pending(kind="contested", message="m"),
        Pending(kind="unowned", message="m"),
        Pending(kind="dispatched", message="m", tracking="tok"),
    )
    subjects = {project_reply_to_mail(r, original_subject=original).subject for r in replies}
    assert subjects == {f"Re: {original}"}


def test_A2_body는_reply_to_mcp_text_재사용() -> None:
    reply = Answered(
        text="환불 가능합니다.",
        answered_by=("o1", "cs_ops"),
        mode="full",
        sources=("정책",),
    )
    mail_reply = project_reply_to_mail(reply)
    assert mail_reply.body == reply_to_mcp_text(reply)


def test_A2_Pending_body도_reply_to_mcp_text_재사용() -> None:
    reply = Pending(kind="dispatched", message="답변 준비되면 알림드릴게요.", tracking="tok-1")
    mail_reply = project_reply_to_mail(reply)
    assert mail_reply.body == reply_to_mcp_text(reply)


# ── A3: MailChannel 포트 + FakeMailChannel ───────────────────────────────────


def test_FakeMailChannel_poll_배출_후_재poll_빈() -> None:
    inbound = InboundMail(
        sender="user@example.com",
        subject="문의",
        payload={"text": "환불 절차가 어떻게 되나요?"},
        message_id="m1",
    )
    channel = FakeMailChannel([inbound])
    assert channel.poll() == (inbound,)
    assert channel.poll() == ()


def test_FakeMailChannel_send_로그에_적재() -> None:
    channel = FakeMailChannel()
    mail_reply = MailReply(subject="Re: 문의", body="답변입니다.")
    outbound = OutboundMail(recipient="user@example.com", reply=mail_reply, in_reply_to="m1")
    channel.send(outbound)
    assert channel.sent == [outbound]


def test_ImapMailChannel_poll_NotImplementedError() -> None:
    with pytest.raises(NotImplementedError):
        ImapMailChannel().poll()


def test_ImapMailChannel_send_NotImplementedError() -> None:
    with pytest.raises(NotImplementedError):
        ImapMailChannel().send(
            OutboundMail(recipient="x@example.com", reply=MailReply(subject="s", body="b"))
        )


def test_0매칭_메일_질문은_Pending_unowned_경유_중립_회신으로_미아없음() -> None:
    """0매칭 메일 질문도 채널 어느 지점에서도 드롭되지 않고 중립 회신이 sent에 남는다."""
    registry = Registry()
    registry.register_user(User(id="root_manager"))
    classifier = FakeClassifier("존재하지않는_intent")
    router = Router(registry, classifier, root_user="root_manager")
    dispatcher = LocalRuntimeDispatcher(StubRuntime())
    audit_log = InMemoryAuditLog()
    ask = AskOrg(router, dispatcher, audit_log)

    channel = FakeMailChannel(
        [
            InboundMail(
                sender="user@example.com",
                subject="문의",
                payload={"text": "주차권 갱신은 어디서 하나요?"},
                message_id="m1",
            )
        ]
    )

    (inbound,) = channel.poll()
    extraction = extract_mail_question(inbound.payload)
    assert isinstance(extraction, MailQuestion)

    reply = ask.handle(extraction.text, User(id="mail_guest"))
    assert isinstance(reply, Pending)
    assert reply.kind == "unowned"

    mail_reply = project_reply_to_mail(reply, original_subject=inbound.subject)
    outbound = OutboundMail(
        recipient=inbound.sender, reply=mail_reply, in_reply_to=inbound.message_id
    )
    channel.send(outbound)

    assert channel.sent == [outbound]
    assert channel.sent[0].reply.subject == "Re: 문의"
    _assert_no_leak(mail_reply)
