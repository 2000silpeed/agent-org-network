"""P17.2a QuestionRequest 도메인 코어의 결정론 단위 테스트.

ADR 0042의 처리 책임·SLA, 전이 연속성, 승인 우회 방지, revision CAS를
검증한다. 실 clock/id/network/sleep은 사용하지 않는다.
"""

from __future__ import annotations

import json
import inspect
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingAnswer,
    AwaitingApproval,
    AwaitingConflict,
    AwaitingManager,
    CompareAndSetError,
    DeclinedRequest,
    DuplicateQuestionRequestError,
    FailedRequest,
    HandlingAssignment,
    HandlingKind,
    InMemoryQuestionRequestStore,
    InitialDisposition,
    InvalidNewQuestionRequestError,
    QuestionRequest,
    QuestionRequestState,
    QuestionRequestStore,
    QuestionRequestTransitionError,
    ReadyToDispatch,
    Received,
    RouteTarget,
    validate_compare_and_set_semantics,
    validate_new_question_request_semantics,
)

_T0 = datetime(2026, 7, 12, 9, 0, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(minutes=1)
_T2 = _T0 + timedelta(minutes=2)
_DUE = _T0 + timedelta(hours=1)


class _Unset:
    pass


_UNSET = _Unset()


def _handling(
    kind: HandlingKind,
    ref: str,
    *,
    due_at: datetime = _DUE,
) -> HandlingAssignment:
    return HandlingAssignment(kind=kind, ref=ref, due_at=due_at)


def _route(
    agent_id: str = "cs_ops",
    *,
    requires_approval: bool = False,
) -> RouteTarget:
    return RouteTarget(
        intent="환불",
        agent_id=agent_id,
        requires_approval=requires_approval,
        authority_version="authority-v1",
    )


def _received_state(ref: str = "question-intake") -> Received:
    return Received(handling=_handling("system", ref))


def _ready(
    *,
    route: RouteTarget | None = None,
    attempt: int = 1,
    trigger_key: str = "initial-route",
) -> ReadyToDispatch:
    return ReadyToDispatch(
        route=route or _route(),
        attempt=attempt,
        trigger_key=trigger_key,
        handling=_handling("system", trigger_key),
    )


def _awaiting_answer(
    *,
    route: RouteTarget | None = None,
    attempt: int = 1,
    ticket_id: str = "ticket-1",
) -> AwaitingAnswer:
    return AwaitingAnswer(
        route=route or _route(),
        attempt=attempt,
        ticket_id=ticket_id,
        handling=_handling("runtime_ticket", ticket_id),
    )


def _awaiting_conflict(case_id: str = "case-1") -> AwaitingConflict:
    return AwaitingConflict(
        case_id=case_id,
        handling=_handling("conflict_case", case_id),
    )


def _awaiting_manager(
    public_kind: InitialDisposition | str = "unowned",
    *,
    item_id: str = "manager-item-1",
    route: RouteTarget | None = None,
    attempt: int | None = None,
) -> AwaitingManager:
    payload: dict[str, object] = {
        "item_id": item_id,
        "public_kind": public_kind,
        "handling": _handling("manager_item", item_id),
    }
    if route is not None:
        payload["route"] = route
    if attempt is not None:
        payload["attempt"] = attempt
    return AwaitingManager.model_validate(payload)


def _awaiting_approval(
    *,
    route: RouteTarget | None = None,
    attempt: int = 1,
    draft_ref: str = "draft-1",
) -> AwaitingApproval:
    return AwaitingApproval(
        route=route or _route(),
        attempt=attempt,
        draft_ref=draft_ref,
        handling=_handling("approval_item", draft_ref),
    )


def _state(kind: str) -> QuestionRequestState:
    states: dict[str, QuestionRequestState] = {
        "received": _received_state(),
        "ready_to_dispatch": _ready(),
        "awaiting_answer": _awaiting_answer(),
        "awaiting_conflict": _awaiting_conflict(),
        "awaiting_manager": _awaiting_manager(),
        "awaiting_approval": _awaiting_approval(),
        "answered": AnsweredRequest(record_id="record-1"),
        "declined": DeclinedRequest(reason_code="manager_declined"),
        "failed": FailedRequest(error_code="unrecoverable_runtime_error"),
    }
    return states[kind]


_KINDS = (
    "received",
    "ready_to_dispatch",
    "awaiting_answer",
    "awaiting_conflict",
    "awaiting_manager",
    "awaiting_approval",
    "answered",
    "declined",
    "failed",
)

_ALLOWED_TOPOLOGY: dict[str, frozenset[str]] = {
    "received": frozenset({"ready_to_dispatch", "awaiting_conflict", "awaiting_manager", "failed"}),
    "ready_to_dispatch": frozenset({"awaiting_answer", "awaiting_approval", "answered", "failed"}),
    "awaiting_answer": frozenset({"awaiting_approval", "answered", "awaiting_manager", "failed"}),
    "awaiting_conflict": frozenset({"ready_to_dispatch", "awaiting_manager", "declined", "failed"}),
    "awaiting_manager": frozenset({"ready_to_dispatch", "declined", "failed"}),
    "awaiting_approval": frozenset({"answered", "declined", "failed"}),
    "answered": frozenset(),
    "declined": frozenset(),
    "failed": frozenset(),
}

_FORBIDDEN_PAIRS = tuple(
    (source, target)
    for source in _KINDS
    for target in _KINDS
    if target not in _ALLOWED_TOPOLOGY[source]
)


def _request(
    *,
    state: QuestionRequestState | None = None,
    request_id: str = "request-1",
    revision: int = 0,
    created_at: datetime = _T0,
    updated_at: datetime = _T0,
    question: str = "환불이 가능한가요?",
    intent: str | None | _Unset = _UNSET,
    initial_disposition: InitialDisposition | None | _Unset = _UNSET,
) -> QuestionRequest:
    actual_state = state if state is not None else _ready()
    if isinstance(intent, _Unset) and isinstance(initial_disposition, _Unset):
        if isinstance(actual_state, Received):
            actual_intent: str | None = None
            actual_disposition: InitialDisposition | None = None
        elif isinstance(actual_state, AwaitingConflict):
            actual_intent = "환불"
            actual_disposition = "contested"
        elif isinstance(actual_state, AwaitingManager) and actual_state.public_kind == "unowned":
            actual_intent = None
            actual_disposition = "unowned"
        elif isinstance(actual_state, AwaitingManager) and actual_state.public_kind == "contested":
            actual_intent = "환불"
            actual_disposition = "contested"
        else:
            actual_intent = "환불"
            actual_disposition = "routed"
    elif not isinstance(intent, _Unset) and not isinstance(
        initial_disposition,
        _Unset,
    ):
        actual_intent = intent
        actual_disposition = initial_disposition
    else:
        raise AssertionError("test fixture must set intent and disposition together")
    return QuestionRequest(
        request_id=request_id,
        org_id="org-1",
        requester_id="user-1",
        session_id="session-1",
        question=question,
        context_snapshot="앞선 대화 맥락",
        intent=actual_intent,
        initial_disposition=actual_disposition,
        state=actual_state,
        revision=revision,
        created_at=created_at,
        updated_at=updated_at,
    )


def _received_without_routing() -> QuestionRequest:
    return _new_received(request_id="request-initial-routing")


def _new_received(
    *,
    request_id: str = "request-1",
    question: str = "환불이 가능한가요?",
    at: datetime = _T0,
) -> QuestionRequest:
    return QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        session_id="session-1",
        question=question,
        context_snapshot="앞선 대화 맥락",
        request_id_factory=lambda: request_id,
        clock=lambda: at,
        due_at=_DUE,
    )


def _persist_history(
    store: InMemoryQuestionRequestStore,
    history: list[QuestionRequest],
) -> QuestionRequest:
    assert history
    store.create(history[0])
    for current, updated in zip(history[:-1], history[1:], strict=True):
        assert store.compare_and_set(
            current.request_id,
            current.revision,
            current,
            updated,
        )
    return history[-1]


# ── 값 객체·aggregate 생성 ────────────────────────────────────────────────


def test_receive는_주입_id_clock_SLA를_사용하고_기본_system책임을_지정한다() -> None:
    calls = {"id": 0, "clock": 0}

    def id_factory() -> str:
        calls["id"] += 1
        return "request-generated"

    def clock() -> datetime:
        calls["clock"] += 1
        return _T0

    request = QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        session_id="session-1",
        question="환불이 가능한가요?",
        context_snapshot="맥락",
        request_id_factory=id_factory,
        clock=clock,
        due_at=_DUE,
    )

    assert request.request_id == "request-generated"
    assert request.state == Received(
        handling=_handling("system", "question-intake:request-generated")
    )
    assert request.intent is None
    assert request.initial_disposition is None
    assert request.revision == 0
    assert request.created_at == _T0
    assert request.updated_at == _T0
    assert calls == {"id": 1, "clock": 1}


def test_receive_public_API에는_system_handler_ref_override가_없다() -> None:
    signature = inspect.signature(QuestionRequest.receive)

    assert "system_handler_ref" not in signature.parameters


def test_receive_기본_system_handler_ref는_request별로_고유하다() -> None:
    first = QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        question="첫 질문",
        request_id_factory=lambda: "request-first",
        clock=lambda: _T0,
        due_at=_DUE,
    )
    second = QuestionRequest.receive(
        org_id="org-1",
        requester_id="user-1",
        question="둘째 질문",
        request_id_factory=lambda: "request-second",
        clock=lambda: _T0,
        due_at=_DUE,
    )

    assert isinstance(first.state, Received)
    assert isinstance(second.state, Received)
    assert first.state.handling.ref == "question-intake:request-first"
    assert second.state.handling.ref == "question-intake:request-second"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "system", "ref": " ", "due_at": _DUE},
        {
            "kind": "system",
            "ref": "intake",
            "due_at": datetime(2026, 7, 12, 10, 0, 0),
        },
        {"kind": "unknown", "ref": "intake", "due_at": _DUE},
    ],
)
def test_HandlingAssignment는_kind_ref_timezone_aware_SLA를_검증한다(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        HandlingAssignment.model_validate(payload)


@pytest.mark.parametrize(
    ("factory", "expected_kind", "expected_ref"),
    [
        (lambda: _received_state(), "system", "question-intake"),
        (lambda: _ready(), "system", "initial-route"),
        (lambda: _awaiting_answer(), "runtime_ticket", "ticket-1"),
        (lambda: _awaiting_conflict(), "conflict_case", "case-1"),
        (lambda: _awaiting_manager(), "manager_item", "manager-item-1"),
        (lambda: _awaiting_approval(), "approval_item", "draft-1"),
    ],
)
def test_모든_비종결상태는_처리책임과_SLA를_가진다(
    factory: Callable[[], QuestionRequestState],
    expected_kind: str,
    expected_ref: str,
) -> None:
    state = factory()

    assert not isinstance(
        state,
        (AnsweredRequest, DeclinedRequest, FailedRequest),
    )
    assert state.handling.kind == expected_kind
    assert state.handling.ref == expected_ref
    assert state.handling.due_at == _DUE


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "received"},
        {
            "kind": "ready_to_dispatch",
            "route": _route(),
            "attempt": 1,
            "trigger_key": "route",
        },
        {
            "kind": "awaiting_answer",
            "route": _route(),
            "attempt": 1,
            "ticket_id": "ticket",
        },
        {"kind": "awaiting_conflict", "case_id": "case"},
        {
            "kind": "awaiting_manager",
            "item_id": "item",
            "public_kind": "unowned",
        },
        {
            "kind": "awaiting_approval",
            "route": _route(),
            "attempt": 1,
            "draft_ref": "draft",
        },
    ],
)
def test_비종결상태는_HandlingAssignment가_필수다(payload: dict[str, object]) -> None:
    state_type = {
        "received": Received,
        "ready_to_dispatch": ReadyToDispatch,
        "awaiting_answer": AwaitingAnswer,
        "awaiting_conflict": AwaitingConflict,
        "awaiting_manager": AwaitingManager,
        "awaiting_approval": AwaitingApproval,
    }[str(payload["kind"])]

    with pytest.raises(ValidationError):
        state_type.model_validate(payload)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Received(handling=_handling("runtime_ticket", "intake")),
        lambda: ReadyToDispatch(
            route=_route(),
            attempt=1,
            trigger_key="trigger",
            handling=_handling("system", "other-trigger"),
        ),
        lambda: AwaitingAnswer(
            route=_route(),
            attempt=1,
            ticket_id="ticket",
            handling=_handling("runtime_ticket", "other-ticket"),
        ),
        lambda: AwaitingConflict(
            case_id="case",
            handling=_handling("manager_item", "case"),
        ),
        lambda: AwaitingManager(
            item_id="item",
            public_kind="unowned",
            handling=_handling("manager_item", "other-item"),
        ),
        lambda: AwaitingApproval(
            route=_route(),
            attempt=1,
            draft_ref="draft",
            handling=_handling("approval_item", "other-draft"),
        ),
    ],
)
def test_상태별_HandlingAssignment_kind와_ref가_일치해야한다(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValidationError):
        factory()


def test_aggregate_SLA_due_at은_updated_at보다_빠를수없다() -> None:
    stale = _ready(trigger_key="stale")
    payload = stale.model_dump()
    payload["handling"] = _handling("system", "stale", due_at=_T0)
    stale = ReadyToDispatch.model_validate(payload)

    with pytest.raises(ValidationError):
        _request(state=stale, updated_at=_T1)


@pytest.mark.parametrize(
    "terminal_type,payload",
    [
        (AnsweredRequest, {"record_id": "record"}),
        (DeclinedRequest, {"reason_code": "declined"}),
        (FailedRequest, {"error_code": "failed"}),
    ],
)
def test_terminal상태에는_HandlingAssignment를_둘수없다(
    terminal_type: type[AnsweredRequest] | type[DeclinedRequest] | type[FailedRequest],
    payload: dict[str, object],
) -> None:
    payload["handling"] = _handling("system", "should-not-exist")
    with pytest.raises(ValidationError):
        terminal_type.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "item_id": "item",
            "public_kind": "dispatched",
            "handling": _handling("manager_item", "item"),
        },
        {
            "item_id": "item",
            "public_kind": "dispatched",
            "route": _route(),
            "handling": _handling("manager_item", "item"),
        },
        {
            "item_id": "item",
            "public_kind": "unowned",
            "route": _route(),
            "attempt": 1,
            "handling": _handling("manager_item", "item"),
        },
        {
            "item_id": "item",
            "public_kind": "contested",
            "attempt": 1,
            "handling": _handling("manager_item", "item"),
        },
    ],
)
def test_AwaitingManager의_dispatched만_route와_attempt를_가진다(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AwaitingManager.model_validate(payload)


def test_strict모델은_python입력의_암묵적_coercion을_거부한다() -> None:
    with pytest.raises(ValidationError):
        ReadyToDispatch.model_validate(
            {
                "route": _route(),
                "attempt": "1",
                "trigger_key": "route",
                "handling": _handling("system", "route"),
            }
        )
    with pytest.raises(ValidationError):
        RouteTarget.model_validate({"intent": "환불", "agent_id": "owner", "requires_approval": 0})
    with pytest.raises(ValidationError):
        HandlingAssignment.model_validate(
            {
                "kind": "system",
                "ref": "route",
                "due_at": _DUE.isoformat(),
            }
        )


def test_strict_JSON_roundtrip은_discriminator와_datetime을_복원한다() -> None:
    original = _request(
        state=_awaiting_approval(
            route=_route(requires_approval=True),
            attempt=2,
        ),
        revision=5,
        updated_at=_T1,
    )

    restored = QuestionRequest.model_validate_json(original.model_dump_json())

    assert restored == original
    assert isinstance(restored.state, AwaitingApproval)
    assert restored.state.handling.due_at == _DUE


def test_Received에_라우팅_metadata를_직접_주입할수없다() -> None:
    payload = _received_without_routing().model_dump()
    payload["intent"] = "환불"
    payload["initial_disposition"] = "routed"

    with pytest.raises(ValidationError):
        QuestionRequest.model_validate(payload)


@pytest.mark.parametrize("field", ["request_id", "org_id", "requester_id", "question"])
def test_QuestionRequest_필수문자열은_nonblank다(field: str) -> None:
    payload = _request().model_dump()
    payload[field] = " "
    with pytest.raises(ValidationError):
        QuestionRequest.model_validate(payload)


@pytest.mark.parametrize("field", ["session_id", "context_snapshot", "intent"])
def test_QuestionRequest_선택문자열도_값이_있으면_nonblank다(field: str) -> None:
    payload = _request().model_dump()
    payload[field] = " "
    with pytest.raises(ValidationError):
        QuestionRequest.model_validate(payload)


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_QuestionRequest_시간은_timezone_aware다(field: str) -> None:
    payload = _request().model_dump()
    payload[field] = datetime(2026, 7, 12, 9, 0, 0)
    with pytest.raises(ValidationError):
        QuestionRequest.model_validate(payload)


def test_QuestionRequest_updated_at은_created_at보다_빠를수없다() -> None:
    with pytest.raises(ValidationError):
        _request(created_at=_T1, updated_at=_T0)


@pytest.mark.parametrize(
    ("intent", "initial_disposition"),
    [
        ("환불", None),
        (None, "routed"),
        (None, "contested"),
        ("", "routed"),
        ("", "contested"),
        ("", "unowned"),
    ],
)
def test_초기_disposition별_intent계약을_위반하면_거부한다(
    intent: str | None,
    initial_disposition: InitialDisposition | None,
) -> None:
    payload = _request().model_dump()
    payload["intent"] = intent
    payload["initial_disposition"] = initial_disposition

    with pytest.raises(ValidationError):
        QuestionRequest.model_validate(payload)


@pytest.mark.parametrize("intent", [None, "환불"])
def test_Unowned는_intent_None또는_nonblank를_허용한다(intent: str | None) -> None:
    request = _request(
        state=_awaiting_manager("unowned"),
        intent=intent,
        initial_disposition="unowned",
    )

    assert request.intent == intent
    assert request.initial_disposition == "unowned"


@pytest.mark.parametrize(
    ("state", "intent", "initial_disposition"),
    [
        (_awaiting_conflict(), "환불", "routed"),
        (_awaiting_conflict(), "환불", "unowned"),
        (_awaiting_manager("unowned"), "환불", "routed"),
        (_awaiting_manager("unowned"), "환불", "contested"),
        (_awaiting_manager("contested"), "환불", "routed"),
        (_awaiting_manager("contested"), "환불", "unowned"),
        (
            _awaiting_manager(
                "dispatched",
                route=_route("other-owner"),
                attempt=1,
            ),
            "계약",
            "routed",
        ),
    ],
)
def test_직접생성은_상태와_초기라우팅이력의_모순을_거부한다(
    state: QuestionRequestState,
    intent: str | None,
    initial_disposition: InitialDisposition,
) -> None:
    with pytest.raises(ValidationError):
        _request(
            state=state,
            intent=intent,
            initial_disposition=initial_disposition,
        )


@pytest.mark.parametrize(
    ("state", "intent", "initial_disposition"),
    [
        (_awaiting_conflict(), "환불", "routed"),
        (_awaiting_manager("unowned"), "환불", "contested"),
        (_awaiting_manager("contested"), "환불", "unowned"),
        (
            _awaiting_manager(
                "dispatched",
                route=_route("other-owner"),
                attempt=1,
            ),
            "계약",
            "contested",
        ),
    ],
)
def test_strict_JSON_hydrate도_상태와_라우팅이력_모순을_거부한다(
    state: QuestionRequestState,
    intent: str | None,
    initial_disposition: InitialDisposition,
) -> None:
    payload = _request().model_dump(mode="json")
    payload["state"] = state.model_dump(mode="json")
    payload["intent"] = intent
    payload["initial_disposition"] = initial_disposition

    with pytest.raises(ValidationError):
        QuestionRequest.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("initial_disposition", ["routed", "contested", "unowned"])
@pytest.mark.parametrize(
    "state",
    [
        _ready(),
        _awaiting_answer(),
        _awaiting_approval(),
        AnsweredRequest(record_id="record"),
        DeclinedRequest(reason_code="declined"),
        FailedRequest(error_code="failed"),
    ],
)
def test_해소후_실행과_terminal상태는_모든_초기disposition이력을_보존할수있다(
    state: QuestionRequestState,
    initial_disposition: InitialDisposition,
) -> None:
    intent = None if initial_disposition == "unowned" else "환불"

    request = _request(
        state=state,
        intent=intent,
        initial_disposition=initial_disposition,
    )
    restored = QuestionRequest.model_validate_json(request.model_dump_json())

    assert restored == request


@pytest.mark.parametrize("initial_disposition", ["routed", "contested", "unowned"])
def test_dispatched_Manager는_모든_초기disposition에서_합법적으로_hydrate된다(
    initial_disposition: InitialDisposition,
) -> None:
    intent = None if initial_disposition == "unowned" else "환불"
    request = _request(
        state=_awaiting_manager(
            "dispatched",
            route=_route(),
            attempt=2,
        ),
        intent=intent,
        initial_disposition=initial_disposition,
    )

    assert QuestionRequest.model_validate_json(request.model_dump_json()) == request


def test_pre_routing_Failed는_라우팅_metadata없이_종결할수있다() -> None:
    received = _received_without_routing()

    failed = received.transition(
        FailedRequest(error_code="router_unavailable"),
        clock=lambda: _T1,
    )

    assert failed.intent is None
    assert failed.initial_disposition is None
    assert isinstance(failed.state, FailedRequest)


def test_QuestionRequest_State_HandlingAssignment는_frozen이다() -> None:
    request = _request()
    state = _ready()
    handling = _handling("system", "route")

    with pytest.raises(ValidationError):
        setattr(request, "question", "바꾼 질문")
    with pytest.raises(ValidationError):
        setattr(state, "attempt", 2)
    with pytest.raises(ValidationError):
        setattr(handling, "ref", "other")


# ── 최초 Router 결과 원자 기록 ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("disposition", "target"),
    [
        ("routed", _ready()),
        ("contested", _awaiting_conflict("case-initial")),
        ("unowned", _awaiting_manager("unowned", item_id="manager-initial")),
    ],
)
def test_receive_후_최초라우팅은_metadata와_state를_한_revision에_기록한다(
    disposition: InitialDisposition,
    target: QuestionRequestState,
) -> None:
    received = _received_without_routing()

    routed = received.record_initial_routing(
        intent="환불",
        disposition=disposition,
        target=target,
        clock=lambda: _T1,
    )

    assert routed.intent == "환불"
    assert routed.initial_disposition == disposition
    assert routed.state == target
    assert routed.revision == 1
    assert routed.created_at == _T0
    assert routed.updated_at == _T1
    assert received.intent is None
    assert received.initial_disposition is None
    assert isinstance(received.state, Received)


@pytest.mark.parametrize("intent", [None, "환불"])
def test_최초_Unowned는_None또는_nonblank_intent를_원자기록한다(
    intent: str | None,
) -> None:
    routed = _received_without_routing().record_initial_routing(
        intent=intent,
        disposition="unowned",
        target=_awaiting_manager("unowned"),
        clock=lambda: _T1,
    )

    assert routed.intent == intent
    assert routed.initial_disposition == "unowned"


@pytest.mark.parametrize("disposition", ["routed", "contested"])
def test_Routed와_Contested는_nonblank_intent가_필수다(
    disposition: InitialDisposition,
) -> None:
    target: QuestionRequestState = _ready() if disposition == "routed" else _awaiting_conflict()

    with pytest.raises(QuestionRequestTransitionError):
        _received_without_routing().record_initial_routing(
            intent=None,
            disposition=disposition,
            target=target,
            clock=lambda: _T1,
        )


def test_최초_Unowned도_빈문자열_intent는_거부한다() -> None:
    with pytest.raises(QuestionRequestTransitionError):
        _received_without_routing().record_initial_routing(
            intent="",
            disposition="unowned",
            target=_awaiting_manager("unowned"),
            clock=lambda: _T1,
        )


@pytest.mark.parametrize(
    ("disposition", "target"),
    [
        ("routed", _awaiting_conflict()),
        ("routed", _ready(attempt=2)),
        ("contested", _awaiting_manager("contested")),
        ("unowned", _ready()),
        ("unowned", _awaiting_manager("dispatched", route=_route(), attempt=1)),
    ],
)
def test_최초_disposition_target_attempt가_불일치하면_거부한다(
    disposition: InitialDisposition,
    target: QuestionRequestState,
) -> None:
    with pytest.raises(QuestionRequestTransitionError):
        _received_without_routing().record_initial_routing(
            intent="환불",
            disposition=disposition,
            target=target,
            clock=lambda: _T1,
        )


def test_최초라우팅_intent는_nonblank이고_RouteTarget과_같아야한다() -> None:
    received = _received_without_routing()

    with pytest.raises(QuestionRequestTransitionError):
        received.record_initial_routing(
            intent=" ",
            disposition="routed",
            target=_ready(),
            clock=lambda: _T1,
        )

    mismatched_route = RouteTarget(
        intent="계약",
        agent_id="legal_ops",
        requires_approval=False,
    )
    with pytest.raises(QuestionRequestTransitionError):
        received.record_initial_routing(
            intent="환불",
            disposition="routed",
            target=_ready(route=mismatched_route),
            clock=lambda: _T1,
        )


@pytest.mark.parametrize(
    "target",
    [_ready(), _awaiting_conflict(), _awaiting_manager()],
)
def test_receive에서_generic_최초라우팅_transition은_전용메서드를_요구한다(
    target: QuestionRequestState,
) -> None:
    with pytest.raises(QuestionRequestTransitionError, match="record_initial_routing"):
        _received_without_routing().transition(target, clock=lambda: _T1)


# ── ADR 0042 전이표·의미 연속성 ──────────────────────────────────────────


def _valid_transition_cases() -> list[tuple[QuestionRequest, QuestionRequestState]]:
    plain = _route()
    approval = _route(requires_approval=True)
    return [
        (_received_without_routing(), FailedRequest(error_code="router-failed")),
        (_request(state=_ready(route=plain)), _awaiting_answer(route=plain)),
        (_request(state=_ready(route=plain)), _awaiting_approval(route=plain)),
        (_request(state=_ready(route=plain)), AnsweredRequest(record_id="record")),
        (_request(state=_ready(route=plain)), FailedRequest(error_code="dispatch")),
        (_request(state=_ready(route=approval)), _awaiting_answer(route=approval)),
        (_request(state=_ready(route=approval)), _awaiting_approval(route=approval)),
        (_request(state=_awaiting_answer(route=plain)), _awaiting_approval(route=plain)),
        (_request(state=_awaiting_answer(route=plain)), AnsweredRequest(record_id="record")),
        (
            _request(state=_awaiting_answer(route=plain)),
            _awaiting_manager("dispatched", route=plain, attempt=1),
        ),
        (_request(state=_awaiting_answer(route=plain)), FailedRequest(error_code="worker")),
        (_request(state=_awaiting_conflict()), _ready(route=plain, attempt=1)),
        (_request(state=_awaiting_conflict()), _awaiting_manager("contested")),
        (_request(state=_awaiting_conflict()), DeclinedRequest(reason_code="declined")),
        (_request(state=_awaiting_conflict()), FailedRequest(error_code="case")),
        (_request(state=_awaiting_manager("unowned")), _ready(route=plain, attempt=1)),
        (_request(state=_awaiting_manager("contested")), _ready(route=plain, attempt=1)),
        (
            _request(
                state=_awaiting_manager(
                    "dispatched",
                    route=plain,
                    attempt=1,
                )
            ),
            _ready(route=_route("finance_ops"), attempt=2, trigger_key="retry-2"),
        ),
        (_request(state=_awaiting_manager()), DeclinedRequest(reason_code="declined")),
        (_request(state=_awaiting_manager()), FailedRequest(error_code="manager")),
        (_request(state=_awaiting_approval()), AnsweredRequest(record_id="record")),
        (_request(state=_awaiting_approval()), DeclinedRequest(reason_code="rejected")),
        (_request(state=_awaiting_approval()), FailedRequest(error_code="approval")),
    ]


@pytest.mark.parametrize(("current", "target"), _valid_transition_cases())
def test_ADR0042_허용전이는_revision과_updated_at을_전진시킨다(
    current: QuestionRequest,
    target: QuestionRequestState,
) -> None:
    current_payload = current.model_dump()
    current_payload["revision"] = 7
    current_payload["updated_at"] = _T1
    current = QuestionRequest.model_validate(current_payload)

    updated = current.transition(target, clock=lambda: _T2)

    assert updated.state == target
    assert updated.revision == 8
    assert updated.updated_at == _T2
    assert updated.created_at == current.created_at
    assert updated.request_id == current.request_id


@pytest.mark.parametrize(("source_kind", "target_kind"), _FORBIDDEN_PAIRS)
def test_ADR0042_금지전이는_거부한다(source_kind: str, target_kind: str) -> None:
    current = _request(state=_state(source_kind), revision=3, updated_at=_T1)

    with pytest.raises(QuestionRequestTransitionError):
        current.transition(_state(target_kind), clock=lambda: _T2)


@pytest.mark.parametrize("terminal_kind", ["answered", "declined", "failed"])
def test_terminal_Request는_어떤_상태로도_부활하지_않는다(terminal_kind: str) -> None:
    current = _request(state=_state(terminal_kind), revision=3, updated_at=_T1)

    for target_kind in _KINDS:
        with pytest.raises(QuestionRequestTransitionError):
            current.transition(_state(target_kind), clock=lambda: _T2)


@pytest.mark.parametrize("source", ["ready", "answer"])
def test_requires_approval_route는_Answered로_직행할수없다(source: str) -> None:
    route = _route(requires_approval=True)
    state: QuestionRequestState = (
        _ready(route=route) if source == "ready" else _awaiting_answer(route=route)
    )
    current = _request(state=state, updated_at=_T1)

    with pytest.raises(QuestionRequestTransitionError, match="AwaitingApproval"):
        current.transition(AnsweredRequest(record_id="bypass"), clock=lambda: _T2)


def test_승인불필요_route도_HITL정책에_따라_AwaitingApproval을_선택할수있다() -> None:
    route = _route(requires_approval=False)
    current = _request(state=_awaiting_answer(route=route), updated_at=_T1)

    updated = current.transition(
        _awaiting_approval(route=route),
        clock=lambda: _T2,
    )

    assert isinstance(updated.state, AwaitingApproval)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (_request(state=_ready()), _awaiting_answer(route=_route("other"))),
        (_request(state=_ready()), _awaiting_answer(attempt=2)),
        (_request(state=_ready()), _awaiting_approval(route=_route("other"))),
        (_request(state=_ready()), _awaiting_approval(attempt=99)),
        (_request(state=_awaiting_answer()), _awaiting_approval(route=_route("other"))),
        (_request(state=_awaiting_answer()), _awaiting_approval(attempt=99)),
    ],
)
def test_같은실행의_route와_attempt는_실행_승인대기까지_바뀌지않는다(
    current: QuestionRequest,
    target: QuestionRequestState,
) -> None:
    with pytest.raises(QuestionRequestTransitionError):
        current.transition(target, clock=lambda: _T1)


@pytest.mark.parametrize("public_kind", ["unowned", "dispatched"])
def test_AwaitingConflict에서_Manager대기는_contested만_허용한다(
    public_kind: str,
) -> None:
    route = _route()
    target = (
        _awaiting_manager("dispatched", route=route, attempt=1)
        if public_kind == "dispatched"
        else _awaiting_manager(public_kind)
    )

    with pytest.raises(QuestionRequestTransitionError):
        _request(state=_awaiting_conflict()).transition(target, clock=lambda: _T1)


@pytest.mark.parametrize("public_kind", ["unowned", "contested"])
def test_AwaitingAnswer에서_Manager대기는_dispatched만_허용한다(
    public_kind: str,
) -> None:
    with pytest.raises(QuestionRequestTransitionError):
        _request(state=_awaiting_answer()).transition(
            _awaiting_manager(public_kind),
            clock=lambda: _T1,
        )


@pytest.mark.parametrize(
    "target",
    [
        _awaiting_manager("dispatched", route=_route("other"), attempt=1),
        _awaiting_manager("dispatched", route=_route(), attempt=2),
    ],
)
def test_dispatched_Manager대기는_직전_AwaitingAnswer실행을_보존한다(
    target: AwaitingManager,
) -> None:
    with pytest.raises(QuestionRequestTransitionError):
        _request(state=_awaiting_answer()).transition(target, clock=lambda: _T1)


@pytest.mark.parametrize(
    ("current", "bad_attempt"),
    [
        (_request(state=_awaiting_conflict()), 2),
        (_request(state=_awaiting_manager("unowned")), 2),
        (_request(state=_awaiting_manager("contested")), 99),
        (
            _request(
                state=_awaiting_manager(
                    "dispatched",
                    route=_route(),
                    attempt=3,
                )
            ),
            3,
        ),
        (
            _request(
                state=_awaiting_manager(
                    "dispatched",
                    route=_route(),
                    attempt=3,
                )
            ),
            99,
        ),
    ],
)
def test_책임해소후_Retry_attempt는_연속되어야한다(
    current: QuestionRequest,
    bad_attempt: int,
) -> None:
    with pytest.raises(QuestionRequestTransitionError):
        current.transition(
            _ready(attempt=bad_attempt, trigger_key="resolution"),
            clock=lambda: _T1,
        )


def test_Unowned_Manager해소는_현재intent를_RouteTarget에_처음기록한다() -> None:
    current = _request(state=_awaiting_manager("unowned"))
    resolved_route = RouteTarget(
        intent="계약",
        agent_id="legal_ops",
        requires_approval=False,
    )

    resolved = current.transition(
        _ready(route=resolved_route, trigger_key="manager-resolution"),
        clock=lambda: _T1,
    )

    assert current.intent is None
    assert resolved.intent is None
    assert isinstance(resolved.state, ReadyToDispatch)
    assert resolved.state.route.intent == "계약"


def test_target_SLA가_전이시각보다_빠르면_거부한다() -> None:
    stale_target = _ready(trigger_key="stale")
    payload = stale_target.model_dump()
    payload["handling"] = _handling("system", "stale", due_at=_T1)
    stale_target = ReadyToDispatch.model_validate(payload)

    with pytest.raises(QuestionRequestTransitionError, match="due_at"):
        _request(state=_awaiting_manager()).transition(
            stale_target,
            clock=lambda: _T2,
        )


def test_transition_clock은_timezone_aware이고_역행하지_않아야한다() -> None:
    current = _request(state=_ready(), updated_at=_T1)

    with pytest.raises(QuestionRequestTransitionError):
        current.transition(
            _awaiting_answer(),
            clock=lambda: datetime(2026, 7, 12, 9, 2, 0),
        )
    with pytest.raises(QuestionRequestTransitionError):
        current.transition(_awaiting_answer(), clock=lambda: _T0)


# ── QuestionRequestStore·공통 CAS 의미 검증 ─────────────────────────────


@pytest.mark.parametrize(
    "candidate",
    [
        _request(state=_ready(), revision=0, updated_at=_T0),
        _request(state=_received_state(), revision=1, updated_at=_T0),
        _request(state=_received_state(), revision=0, updated_at=_T1),
    ],
)
def test_신규create_ingress는_접수원형만_허용한다(candidate: QuestionRequest) -> None:
    with pytest.raises(InvalidNewQuestionRequestError):
        validate_new_question_request_semantics(candidate)


def test_InMemory_create로_중간상태_직접주입을_우회할수없다() -> None:
    store = InMemoryQuestionRequestStore()
    forged = _request(state=_awaiting_manager("unowned"), revision=0)

    with pytest.raises(InvalidNewQuestionRequestError):
        store.create(forged)
    assert store.get(forged.request_id) is None


def test_직접생성_Received도_request별_intake_ref가_아니면_create할수없다() -> None:
    for request_id in ("request-first", "request-second"):
        store = InMemoryQuestionRequestStore()
        forged = _request(
            request_id=request_id,
            state=_received_state("question-intake"),
        )

        with pytest.raises(InvalidNewQuestionRequestError, match="handling.ref"):
            store.create(forged)
        assert store.get(request_id) is None


def test_InMemory_create는_receive가_만든_접수원형을_허용한다() -> None:
    store = InMemoryQuestionRequestStore()
    received = _received_without_routing()

    assert store.create(received) == received
    assert store.get(received.request_id) == received


def test_store_create_get_반환계약과_duplicate_거부() -> None:
    store: QuestionRequestStore = InMemoryQuestionRequestStore()
    request = _new_received()

    assert store.create(request) == request
    assert store.get(request.request_id) == request
    assert store.get("missing") is None
    with pytest.raises(DuplicateQuestionRequestError):
        store.create(request)


def test_CAS_성공은_True_경쟁패배와_미존재는_False다() -> None:
    store = InMemoryQuestionRequestStore()
    received = _new_received()
    current = received.record_initial_routing(
        intent="환불",
        disposition="routed",
        target=_ready(),
        clock=lambda: _T0,
    )
    updated = current.transition(_awaiting_answer(), clock=lambda: _T1)
    _persist_history(store, [received, current])

    assert store.compare_and_set("request-1", 1, current, updated) is True
    assert store.get("request-1") == updated
    assert store.compare_and_set("request-1", 1, current, updated) is False

    missing = _new_received(request_id="missing")
    missing_next = missing.record_initial_routing(
        intent="환불",
        disposition="routed",
        target=_ready(),
        clock=lambda: _T0,
    )
    assert store.compare_and_set("missing", 0, missing, missing_next) is False


def test_CAS는_request_id_expected_revision_next_revision_입력계약을_검증한다() -> None:
    current = _request()
    updated = current.transition(_awaiting_answer(), clock=lambda: _T1)

    with pytest.raises(CompareAndSetError):
        validate_compare_and_set_semantics("other", 0, current, updated)
    with pytest.raises(CompareAndSetError):
        validate_compare_and_set_semantics("request-1", 1, current, updated)

    payload = updated.model_dump()
    payload["revision"] = 2
    skipped_revision = QuestionRequest.model_validate(payload)
    with pytest.raises(CompareAndSetError):
        validate_compare_and_set_semantics("request-1", 0, current, skipped_revision)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("org_id", "org-other"),
        ("requester_id", "user-other"),
        ("session_id", "session-other"),
        ("question", "변조된 질문"),
        ("context_snapshot", "변조된 맥락"),
        ("created_at", _T0 - timedelta(minutes=1)),
    ],
)
def test_공통_CAS검증은_aggregate_immutable_envelope_변경을_거부한다(
    field: str,
    replacement: object,
) -> None:
    current = _request()
    valid_next = current.transition(_awaiting_answer(), clock=lambda: _T1)
    payload = valid_next.model_dump()
    payload[field] = replacement
    forged_next = QuestionRequest.model_validate(payload)

    with pytest.raises(CompareAndSetError):
        validate_compare_and_set_semantics("request-1", 0, current, forged_next)


@pytest.mark.parametrize(
    "forged_target",
    [
        _awaiting_answer(route=_route("other")),
        _awaiting_answer(attempt=99),
        AnsweredRequest(record_id="approval-bypass"),
    ],
)
def test_공통_CAS검증은_객체shape가_유효해도_전이의미위반을_거부한다(
    forged_target: QuestionRequestState,
) -> None:
    approval_route = _route(requires_approval=True)
    current = _request(state=_ready(route=approval_route))
    updated_payload = current.model_dump()
    updated_payload["state"] = forged_target
    updated_payload["revision"] = 1
    updated_payload["updated_at"] = _T1
    if isinstance(forged_target, (AwaitingAnswer, AwaitingApproval)):
        # aggregate intent 일치만 맞추고 route/attempt 연속성은 의도적으로 깨뜨린다.
        target_payload = forged_target.model_dump()
        target_route_payload = forged_target.route.model_dump()
        target_route_payload["intent"] = "환불"
        target_payload["route"] = RouteTarget.model_validate(target_route_payload)
        updated_payload["state"] = forged_target.__class__.model_validate(target_payload)
    forged_updated = QuestionRequest.model_validate(updated_payload)

    with pytest.raises(CompareAndSetError):
        validate_compare_and_set_semantics("request-1", 0, current, forged_updated)


def test_CAS는_최초라우팅에서만_metadata기록을_허용한다() -> None:
    store = InMemoryQuestionRequestStore()
    received = _received_without_routing()
    initially_routed = received.record_initial_routing(
        intent="환불",
        disposition="routed",
        target=_ready(),
        clock=lambda: _T1,
    )
    store.create(received)

    assert store.compare_and_set(
        received.request_id,
        received.revision,
        received,
        initially_routed,
    )

    awaiting = initially_routed.transition(
        _awaiting_answer(),
        clock=lambda: _T2,
    )
    changed_payload = awaiting.model_dump()
    changed_payload["initial_disposition"] = "contested"
    changed = QuestionRequest.model_validate(changed_payload)
    with pytest.raises(CompareAndSetError):
        store.compare_and_set("request-initial-routing", 1, initially_routed, changed)


def test_CAS는_revision만_같고_current_exact_equality가_다르면_False다() -> None:
    store = InMemoryQuestionRequestStore()
    stored_received = _new_received(question="원 질문")
    stored = stored_received.record_initial_routing(
        intent="환불",
        disposition="routed",
        target=_ready(),
        clock=lambda: _T0,
    )
    forged_received = _new_received(question="바뀐 질문")
    forged_current = forged_received.record_initial_routing(
        intent="환불",
        disposition="routed",
        target=_ready(),
        clock=lambda: _T0,
    )
    forged_next = forged_current.transition(_awaiting_answer(), clock=lambda: _T1)
    _persist_history(store, [stored_received, stored])

    assert store.compare_and_set("request-1", 1, forged_current, forged_next) is False
    assert store.get("request-1") == stored


def test_CAS_32개_경쟁자는_정확히_하나만_revision을_선점한다() -> None:
    workers = 32
    store = InMemoryQuestionRequestStore()
    received = _new_received()
    current = received.record_initial_routing(
        intent=None,
        disposition="unowned",
        target=_awaiting_manager("unowned"),
        clock=lambda: _T0,
    )
    _persist_history(store, [received, current])
    barrier = threading.Barrier(workers)
    candidates = [
        current.transition(
            _ready(
                route=_route(f"owner-{index}"),
                trigger_key=f"manager-claim-{index}",
            ),
            clock=lambda: _T1,
        )
        for index in range(workers)
    ]

    def compete(index: int) -> bool:
        barrier.wait(timeout=10.0)
        return store.compare_and_set("request-1", 1, current, candidates[index])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        won = list(pool.map(compete, range(workers)))

    assert sum(won) == 1
    assert store.get("request-1") == candidates[won.index(True)]


def test_nonterminal은_terminal을_제외하고_created_at_request_id순_snapshot이다() -> None:
    store = InMemoryQuestionRequestStore()
    request_b_received = _new_received(request_id="request-b", at=_T1)
    request_b = request_b_received.record_initial_routing(
        intent=None,
        disposition="unowned",
        target=_awaiting_manager(),
        clock=lambda: _T1,
    )
    request_z_received = _new_received(request_id="request-z")
    request_z_ready = request_z_received.record_initial_routing(
        intent="환불",
        disposition="routed",
        target=_ready(),
        clock=lambda: _T0,
    )
    request_z = request_z_ready.transition(
        AnsweredRequest(record_id="record"),
        clock=lambda: _T0,
    )
    request_c_received = _new_received(request_id="request-c")
    request_c_manager = request_c_received.record_initial_routing(
        intent=None,
        disposition="unowned",
        target=_awaiting_manager(),
        clock=lambda: _T0,
    )
    request_c = request_c_manager.transition(
        DeclinedRequest(reason_code="declined"),
        clock=lambda: _T0,
    )
    request_b0_received = _new_received(request_id="request-b0")
    request_b0 = request_b0_received.transition(
        FailedRequest(error_code="failed"),
        clock=lambda: _T0,
    )

    histories = [
        [request_b_received, request_b],
        [request_z_received, request_z_ready, request_z],
        [_new_received(request_id="request-a")],
        [request_c_received, request_c_manager, request_c],
        [request_b0_received, request_b0],
        [_new_received(request_id="request-aa")],
    ]
    for history in reversed(histories):
        _persist_history(store, history)

    snapshot = store.nonterminal()

    assert [request.request_id for request in snapshot] == [
        "request-a",
        "request-aa",
        "request-b",
    ]
    snapshot.clear()
    assert len(store.nonterminal()) == 3


def test_InMemory_Request_입출력과_snapshot은_backing_alias를_공유하지_않는다() -> None:
    store = InMemoryQuestionRequestStore()
    request = _new_received(request_id="alias-request")
    created = store.create(request)

    object.__setattr__(request, "org_id", "forged-input")
    object.__setattr__(created, "requester_id", "forged-return")
    fetched = store.get("alias-request")
    assert fetched is not None
    object.__setattr__(fetched, "question", "forged-get")
    snapshot = store.nonterminal()
    object.__setattr__(snapshot[0], "org_id", "forged-snapshot")
    snapshot.clear()

    stored = store.get("alias-request")
    assert stored is not None
    assert stored.org_id == "org-1"
    assert stored.requester_id == "user-1"
    assert stored.question == "환불이 가능한가요?"

    failed = stored.transition(FailedRequest(error_code="test-failed"), clock=lambda: _T0)
    assert store.compare_and_set(
        stored.request_id,
        stored.revision,
        stored,
        failed,
    )
    object.__setattr__(failed, "org_id", "forged-cas-input")
    after_cas = store.get("alias-request")
    assert after_cas is not None
    assert after_cas.org_id == "org-1"
    assert isinstance(after_cas.state, FailedRequest)
