"""T9.7 게이트 내 두 슬라이스 — S1 WS 프레임 맥락 전파 + S2 HITL 워커측 초안 보류.

S1(ADR 0027 결정 13): `WorkTicket`·`TicketFrame`에 `context: str | None` 옵셔널 필드 추가·
왕복·디스패처 전파·`handle_push_work` 소비. 두 방향 파싱 호환(구버전 wire→신버전 파서 성공,
신버전 wire→구버전 파서 거부 실증)까지 게이트 내 결정론.

S2(ADR 0025 결정 4·5): `TicketFrame.hitl` 힌트(중앙 `HitlToggleMap`이 dispatch 시점에 계산해
싣는다) + 워커측 `PendingDraft` 보류 store + `submit_pending_draft` 진입점. 실 owner 검토
UI·크로스머신 롤아웃은 게이트 밖(수동) — 여기는 결정론 로직만.

전부 결정론: 고정 clock, Fake send 콜백, FakeRunner 대역, 실 네트워크·실 claude·스레드 0.
"""

from datetime import date, datetime, timezone
from typing import Callable

import pytest
from pydantic import ValidationError

from agent_org_network.agent_card import AgentCard
from agent_org_network.dispatch import InMemoryWorkQueueDispatcher, WorkTicket
from agent_org_network.hitl import HitlToggleMap
from agent_org_network.registry import Registry
from agent_org_network.runtime import Answer, ClaudeCodeRuntime
from agent_org_network.transport import (
    CentralFrame,
    PushWork,
    RegisterWorker,
    SubmitAnswer,
    TicketFrame,
    WebSocketDispatcher,
    from_ticket_frame,
    to_ticket_frame,
)
from agent_org_network.worker import PendingDraft, WorkerLogic

BASE_TS = datetime(2026, 7, 2, 9, 0, 0, tzinfo=timezone.utc)


def _fixed_clock(ts: datetime) -> Callable[[], datetime]:
    return lambda: ts


def _card(
    agent_id: str = "cs_ops",
    owner: str = "alice",
    approval_when: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="cs",
        summary="고객 지원 담당",
        domains=["환불"],
        last_reviewed_at=date(2026, 6, 20),
        approval_when=approval_when if approval_when is not None else [],
    )


class _Recorder:
    """워커 소켓 send 콜백 stub — 내보낸 프레임을 기록만 한다."""

    def __init__(self) -> None:
        self.sent: list[CentralFrame] = []

    def __call__(self, frame: CentralFrame) -> None:
        self.sent.append(frame)


class _RecordingRunner:
    """실 claude 대역 — 고정 응답, 마지막 프롬프트 기록."""

    def __init__(self, reply: str = "답") -> None:
        self.reply = reply

    def __call__(
        self, prompt: str, *, cwd: str | None = None, system_prompt: str | None = None
    ) -> str:
        return self.reply


# ══════════════════════════════════════════════════════════════════════════
# S1 — WS 프레임 맥락 전파 (ADR 0027 결정 13·T9.4 c-3)
# ══════════════════════════════════════════════════════════════════════════


# ── ① context 왕복 항등 ──────────────────────────────────────────────────


def test_WorkTicket_context_기본값은_None():
    ticket = WorkTicket(
        owner_id="alice",
        agent_id="cs_ops",
        question="Q",
        enqueued_at=BASE_TS,
    )
    assert ticket.context is None


def test_to_ticket_frame이_context를_싣는다():
    ticket = WorkTicket(
        owner_id="alice",
        agent_id="cs_ops",
        question="Q",
        enqueued_at=BASE_TS,
        ticket_id="tid-1",
        context="이전 턴: 환불 정책이 뭔가요?",
    )
    frame = to_ticket_frame(ticket)

    assert frame.context == "이전 턴: 환불 정책이 뭔가요?"


def test_from_ticket_frame이_context를_복원한다():
    frame = TicketFrame(
        ticket_id="tid-2",
        agent_id="cs_ops",
        question="Q",
        enqueued_at=BASE_TS,
        context="맥락 문자열",
    )
    ticket = from_ticket_frame(frame, owner_id="alice")

    assert ticket.context == "맥락 문자열"


def test_context_왕복이_항등이다():
    original = WorkTicket(
        owner_id="alice",
        agent_id="cs_ops",
        question="Q",
        enqueued_at=BASE_TS,
        ticket_id="tid-3",
        context="라운드트립 맥락",
    )
    restored = from_ticket_frame(to_ticket_frame(original), owner_id=original.owner_id)

    assert restored.context == original.context
    assert restored == original


def test_context_None_왕복도_항등이다():
    """context=None 무회귀 — 미주입 턴은 그대로 None으로 왕복한다."""
    original = WorkTicket(
        owner_id="alice",
        agent_id="cs_ops",
        question="Q",
        enqueued_at=BASE_TS,
        ticket_id="tid-4",
    )
    restored = from_ticket_frame(to_ticket_frame(original), owner_id=original.owner_id)

    assert restored.context is None
    assert restored == original


# ── ② 구버전 wire(context 없음) → 신버전 파서 성공(None) ────────────────────


def test_구버전_wire_context_없음_신버전_파서가_성공한다():
    """context 키가 아예 없는 구버전 프레임도 신버전 TicketFrame이 기본값 None으로 파싱한다."""
    legacy_wire = {
        "ticket_id": "tid-legacy",
        "agent_id": "cs_ops",
        "question": "환불 되나요?",
        "enqueued_at": BASE_TS.isoformat(),
    }
    frame = TicketFrame.model_validate(legacy_wire)

    assert frame.context is None
    assert frame.hitl is False


# ── ③ 신버전 wire(context 실림) → 구버전 파서 거부 실증(ValidationError) ────


def test_신버전_wire_context_실림_구버전_파서는_거부한다():
    """extra="forbid" 비대칭 함정 실증 — 신버전 필드가 실린 프레임을 구버전 스키마가 거부.

    ADR 0027 결정 13 경고 — 롤아웃은 워커 선행(forward-compatible first) 근거를 여기서
    결정론으로 고정한다. `_LegacyTicketFrame`은 T9.7 이전 스키마(ticket_id·agent_id·
    question·enqueued_at 4필드, extra="forbid")를 흉내낸다.
    """
    from pydantic import BaseModel, ConfigDict

    class _LegacyTicketFrame(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")

        ticket_id: str
        agent_id: str
        question: str
        enqueued_at: datetime

    new_wire = to_ticket_frame(
        WorkTicket(
            owner_id="alice",
            agent_id="cs_ops",
            question="환불 되나요?",
            enqueued_at=BASE_TS,
            ticket_id="tid-new",
            context="이전 턴 맥락",
        )
    ).model_dump(mode="json")

    with pytest.raises(ValidationError):
        _LegacyTicketFrame.model_validate(new_wire)


def test_신버전_wire_context_None이어도_구버전_파서를_깬다():
    """model_dump(mode="json")은 context=None도 항상 직렬화 — 맥락 유무와 무관하게 깨짐."""
    from pydantic import BaseModel, ConfigDict

    class _LegacyTicketFrame(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")

        ticket_id: str
        agent_id: str
        question: str
        enqueued_at: datetime

    new_wire = to_ticket_frame(
        WorkTicket(
            owner_id="alice",
            agent_id="cs_ops",
            question="Q",
            enqueued_at=BASE_TS,
            ticket_id="tid-none",
        )
    ).model_dump(mode="json")

    assert "context" in new_wire  # exclude_none 없음 — None도 실린다
    with pytest.raises(ValidationError):
        _LegacyTicketFrame.model_validate(new_wire)


# ── ④ 디스패처→프레임→handle_push_work가 Stub 런타임 관측 seam에 context 도달 ──


def test_WebSocketDispatcher_dispatch가_context를_큐에_전파한다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    card = _card()

    ticket = dispatcher.dispatch("Q", card, context="이전 대화 요약")

    assert ticket.context == "이전 대화 요약"


def test_WebSocketDispatcher_dispatch가_push된_프레임에_context를_싣는다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    rec = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec)
    card = _card(owner="alice")

    dispatcher.dispatch("Q", card, context="스레드 맥락")

    pushes = [f for f in rec.sent if isinstance(f, PushWork)]
    assert len(pushes) == 1
    assert pushes[0].ticket.context == "스레드 맥락"


def test_handle_push_work가_runtime_answer에_context를_전달한다():
    """디스패처→프레임→handle_push_work 경로가 Stub 런타임 관측 seam에 context를 나른다."""

    class _ContextObservingRuntime:
        def __init__(self) -> None:
            self.last_context: str | None = "미호출"

        def answer(
            self, question: str, card: AgentCard, context: str | None = None
        ) -> Answer:
            self.last_context = context
            return Answer(text="답", sources=(), mode="full")

    runtime = _ContextObservingRuntime()
    card = _card()
    logic = WorkerLogic(owner_id="alice", cards={"cs_ops": card}, runtime=runtime)  # type: ignore[arg-type]

    push = PushWork(
        ticket=TicketFrame(
            ticket_id="tkt-ctx",
            agent_id="cs_ops",
            question="환불 되나요?",
            enqueued_at=BASE_TS,
            context="이전 턴: 배송 문의",
        )
    )

    submit = logic.handle_push_work(push)

    assert runtime.last_context == "이전 턴: 배송 문의"
    assert isinstance(submit, SubmitAnswer)


def test_handle_push_work_context_None이면_런타임도_None을_받는다():
    """무회귀 — context 미주입 턴은 runtime.answer(context=None)으로 기존 동작 그대로."""

    class _ContextObservingRuntime:
        def __init__(self) -> None:
            self.last_context: str | None = "미호출"

        def answer(
            self, question: str, card: AgentCard, context: str | None = None
        ) -> Answer:
            self.last_context = context
            return Answer(text="답", sources=(), mode="full")

    runtime = _ContextObservingRuntime()
    card = _card()
    logic = WorkerLogic(owner_id="alice", cards={"cs_ops": card}, runtime=runtime)  # type: ignore[arg-type]

    push = PushWork(
        ticket=TicketFrame(
            ticket_id="tkt-none",
            agent_id="cs_ops",
            question="환불 되나요?",
            enqueued_at=BASE_TS,
        )
    )

    logic.handle_push_work(push)

    assert runtime.last_context is None


# ══════════════════════════════════════════════════════════════════════════
# S2 — HITL 워커측 초안 보류 (ADR 0025 결정 4·5·T9.7 S2)
# ══════════════════════════════════════════════════════════════════════════


def _push(
    ticket_id: str = "tkt-1",
    agent_id: str = "cs_ops",
    question: str = "환불 되나요?",
    hitl: bool = False,
) -> PushWork:
    return PushWork(
        ticket=TicketFrame(
            ticket_id=ticket_id,
            agent_id=agent_id,
            question=question,
            enqueued_at=BASE_TS,
            hitl=hitl,
        )
    )


def _logic_with_reply(reply: str = "네, 7일 이내 가능합니다.") -> WorkerLogic:
    card = _card()
    return WorkerLogic(
        owner_id="alice",
        cards={"cs_ops": card},
        runtime=ClaudeCodeRuntime(runner=_RecordingRunner(reply)),
        clock=_fixed_clock(BASE_TS),
    )


# ── ① 힌트 off → 즉시 회신(무변경) ──────────────────────────────────────────


def test_hitl_off_힌트면_즉시_SubmitAnswer를_반환한다():
    logic = _logic_with_reply("즉답")

    submit = logic.handle_push_work(_push(hitl=False))

    assert isinstance(submit, SubmitAnswer)
    assert submit.answer.text == "즉답"
    assert logic.pending_draft("tkt-1") is None


# ── ② 힌트 on → 보류(회신 0·store 적재) ────────────────────────────────────


def test_hitl_on_힌트면_즉시_회신하지_않고_보류한다():
    logic = _logic_with_reply("초안 답변")

    result = logic.handle_push_work(_push(ticket_id="tkt-2", hitl=True))

    assert result is None
    pending = logic.pending_draft("tkt-2")
    assert pending is not None
    assert isinstance(pending, PendingDraft)
    assert pending.ticket_id == "tkt-2"
    assert pending.draft_answer.text == "초안 답변"
    assert pending.agent_id == "cs_ops"
    assert pending.made_at == BASE_TS


def test_hitl_on_보류에_context가_보존된다():
    logic = _logic_with_reply("초안")
    push = PushWork(
        ticket=TicketFrame(
            ticket_id="tkt-ctx-pending",
            agent_id="cs_ops",
            question="Q",
            enqueued_at=BASE_TS,
            context="이전 턴 맥락",
            hitl=True,
        )
    )

    logic.handle_push_work(push)

    pending = logic.pending_draft("tkt-ctx-pending")
    assert pending is not None
    assert pending.context == "이전 턴 맥락"


def test_카드를_못_찾으면_hitl_힌트와_무관하게_즉시_폴백_회신한다():
    """미등록 agent_id는 검토할 실 초안이 없다 — HITL on이어도 즉시 폴백(미아 방지)."""
    logic = _logic_with_reply("답")

    submit = logic.handle_push_work(
        _push(ticket_id="tkt-unknown", agent_id="unknown_ops", hitl=True)
    )

    assert isinstance(submit, SubmitAnswer)
    assert "unknown_ops" in submit.answer.text
    assert logic.pending_draft("tkt-unknown") is None


# ── ③ 승인 전송 ──────────────────────────────────────────────────────────


def test_submit_pending_draft_승인은_원문_그대로_전송한다():
    logic = _logic_with_reply("원본 초안")
    logic.handle_push_work(_push(ticket_id="tkt-3", hitl=True))

    submit = logic.submit_pending_draft("tkt-3")

    assert isinstance(submit, SubmitAnswer)
    assert submit.ticket_id == "tkt-3"
    assert submit.answer.text == "원본 초안"
    # 전송 후 보류 항목은 제거된다.
    assert logic.pending_draft("tkt-3") is None


# ── ④ 수정 전송(edited_text 반영) ──────────────────────────────────────────


def test_submit_pending_draft_수정_텍스트가_반영된다():
    logic = _logic_with_reply("원본 초안")
    logic.handle_push_work(_push(ticket_id="tkt-4", hitl=True))

    submit = logic.submit_pending_draft("tkt-4", edited_text="수정된 답변입니다.")

    assert submit.answer.text == "수정된 답변입니다."
    assert logic.pending_draft("tkt-4") is None


def test_submit_pending_draft_수정시_sources_mode는_원본_보존():
    card = _card(agent_id="cs_ops")
    card = AgentCard(
        agent_id="cs_ops",
        owner="alice",
        team="cs",
        summary="s",
        domains=["환불"],
        last_reviewed_at=date(2026, 6, 20),
        knowledge_sources=["위키/환불정책"],
    )
    logic = WorkerLogic(
        owner_id="alice",
        cards={"cs_ops": card},
        runtime=ClaudeCodeRuntime(runner=_RecordingRunner("원본")),
        clock=_fixed_clock(BASE_TS),
    )
    logic.handle_push_work(_push(ticket_id="tkt-5", hitl=True))

    submit = logic.submit_pending_draft("tkt-5", edited_text="수정본")

    assert submit.answer.sources == ("위키/환불정책",)


def test_submit_pending_draft_미보류_ticket은_KeyError():
    logic = _logic_with_reply("답")

    with pytest.raises(KeyError):
        logic.submit_pending_draft("no-such-ticket")


# ── ⑤ 보류 방치 — store에 남고 워커는 아무것도 안 함(워커측 TTL 없음) ──────────


def test_보류_방치시_store에_남아있고_워커는_아무것도_안_한다():
    """미아 없음은 중앙 큐 timeout 단일 진실 — 워커측엔 TTL이 없어 방치돼도 그대로 남는다."""
    logic = _logic_with_reply("방치된 초안")
    logic.handle_push_work(_push(ticket_id="tkt-abandoned", hitl=True))

    # "시간이 흘러도"(워커 로직엔 시간 개념이 없다 — clock을 다시 advance해도 워커가
    # 스스로 정리하는 어떤 동작도 없음을 반복 조회로 확인) 보류가 그대로 남는다.
    for _ in range(3):
        pending = logic.pending_draft("tkt-abandoned")
        assert pending is not None
        assert pending.draft_answer.text == "방치된 초안"


# ── ⑥ 콘솔 토글→다음 dispatch 힌트 반영 e2e ────────────────────────────────


def test_hitl_toggle_on이면_다음_dispatch_프레임에_힌트가_반영된다():
    toggles = HitlToggleMap()
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS), hitl_toggles=toggles)
    rec = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec)
    card = _card(owner="alice", agent_id="cs_ops")

    toggles.set("cs_ops", True)
    dispatcher.dispatch("Q1", card)

    pushes = [f for f in rec.sent if isinstance(f, PushWork)]
    assert len(pushes) == 1
    assert pushes[0].ticket.hitl is True


def test_hitl_toggle_off로_되돌리면_다음_dispatch_힌트도_반영된다():
    toggles = HitlToggleMap()
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS), hitl_toggles=toggles)
    rec = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec)
    card = _card(owner="alice", agent_id="cs_ops")

    toggles.set("cs_ops", True)
    dispatcher.dispatch("Q1", card)
    toggles.set("cs_ops", False)
    dispatcher.dispatch("Q2", card)

    pushes = [f for f in rec.sent if isinstance(f, PushWork)]
    assert len(pushes) == 2
    assert pushes[0].ticket.hitl is True
    assert pushes[1].ticket.hitl is False


def test_hitl_toggle_카드_approval_when_시드가_토글_미set이어도_힌트를_올린다():
    """카드 approval_when이 있으면 시드로 hitl on — 콘솔이 명시 set 안 해도 힌트가 True."""
    registry = Registry()
    card = _card(owner="alice", agent_id="legal_review", approval_when=["계약 해지"])
    registry.register(card)
    toggles = HitlToggleMap()
    dispatcher = WebSocketDispatcher(
        clock=_fixed_clock(BASE_TS), hitl_toggles=toggles, registry=registry
    )
    rec = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec)

    dispatcher.dispatch("계약 해지 문의", card)

    pushes = [f for f in rec.sent if isinstance(f, PushWork)]
    assert len(pushes) == 1
    assert pushes[0].ticket.hitl is True


# ── ⑦ 미주입 하위호환 — hitl_toggles 없으면 힌트 항상 False ────────────────


def test_hitl_toggles_미주입이면_힌트는_항상_False():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))  # hitl_toggles 미주입
    rec = _Recorder()
    dispatcher.register(RegisterWorker(owner_id="alice"), rec)
    card = _card(owner="alice")

    dispatcher.dispatch("Q", card)

    pushes = [f for f in rec.sent if isinstance(f, PushWork)]
    assert len(pushes) == 1
    assert pushes[0].ticket.hitl is False


def test_InMemoryWorkQueueDispatcher_dispatch에_context_키워드_인자_동작():
    """InMemoryWorkQueueDispatcher.dispatch(context=)가 WorkTicket.context에 실린다."""
    dispatcher = InMemoryWorkQueueDispatcher(clock=_fixed_clock(BASE_TS))
    card = _card()

    ticket = dispatcher.dispatch("Q", card, context="맥락")

    assert ticket.context == "맥락"
