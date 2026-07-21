"""Question Request 수명주기 도메인 코어(P17.2a·ADR 0042).

질문 한 건의 접수부터 사용자 terminal 결과까지를 frozen aggregate로 표현한다.
WorkTicket·ConflictCase·ManagerItem·AnswerRecord와 구분되는 request_id가 전체 수명의
상관키다. 이 모듈은 상태 전이와 revision CAS만 소유하며 웹·런타임·영속 DB는 모른다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from threading import RLock
from typing import Annotated, Literal, Protocol, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Clock: TypeAlias = Callable[[], datetime]
RequestIdFactory: TypeAlias = Callable[[], str]
InitialDisposition: TypeAlias = Literal["routed", "contested", "unowned"]
ManagerPublicKind: TypeAlias = Literal["contested", "unowned", "dispatched"]
QuestionPendingKind: TypeAlias = Literal["routing", "routed", "contested", "unowned"]
HandlingKind: TypeAlias = Literal[
    "system",
    "runtime_ticket",
    "conflict_case",
    "manager_item",
    "approval_item",
]


class QuestionRequestTransitionError(ValueError):
    """ADR 0042가 허용하지 않는 Request State 전이."""


class DuplicateQuestionRequestError(ValueError):
    """같은 request_id를 두 번 create하려는 시도."""


class InvalidNewQuestionRequestError(ValueError):
    """Store 신규 등록 경계에 접수 원형이 아닌 Request가 들어온 경우."""


class CompareAndSetError(ValueError):
    """CAS 호출 인자의 ID·revision·시간·전이 계약 위반."""


class _FrozenModel(BaseModel):
    """Question Request 값 객체 공통 규율: frozen·추가 필드 거부·문자열 nonblank."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    @field_validator("*", mode="after")
    @classmethod
    def _strings_must_be_nonblank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("문자열 값은 빈 문자열이나 공백일 수 없습니다.")
        return value


class RouteTarget(_FrozenModel):
    """사람 처분·라우팅이 확정한 재실행 대상의 불변 snapshot."""

    intent: str
    agent_id: str
    requires_approval: bool
    authority_version: str | None = None


class HandlingAssignment(_FrozenModel):
    """비종결 Request를 현재 책임지는 처리 객체와 SLA 마감."""

    kind: HandlingKind
    ref: str
    due_at: datetime

    @field_validator("due_at", mode="after")
    @classmethod
    def _due_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if not _is_timezone_aware(value):
            raise ValueError("HandlingAssignment.due_at은 timezone-aware여야 합니다.")
        return value


class Received(_FrozenModel):
    kind: Literal["received"] = "received"
    handling: HandlingAssignment

    @model_validator(mode="after")
    def _validate_handling(self) -> Self:
        _require_handling(self.handling, kind="system")
        return self


class ReadyToDispatch(_FrozenModel):
    kind: Literal["ready_to_dispatch"] = "ready_to_dispatch"
    route: RouteTarget
    attempt: int = Field(ge=1)
    trigger_key: str
    handling: HandlingAssignment

    @model_validator(mode="after")
    def _validate_handling(self) -> Self:
        _require_handling(self.handling, kind="system", ref=self.trigger_key)
        return self


class AwaitingAnswer(_FrozenModel):
    kind: Literal["awaiting_answer"] = "awaiting_answer"
    route: RouteTarget
    attempt: int = Field(ge=1)
    ticket_id: str
    handling: HandlingAssignment

    @model_validator(mode="after")
    def _validate_handling(self) -> Self:
        _require_handling(
            self.handling,
            kind="runtime_ticket",
            ref=self.ticket_id,
        )
        return self


class AwaitingConflict(_FrozenModel):
    kind: Literal["awaiting_conflict"] = "awaiting_conflict"
    case_id: str
    handling: HandlingAssignment

    @model_validator(mode="after")
    def _validate_handling(self) -> Self:
        _require_handling(self.handling, kind="conflict_case", ref=self.case_id)
        return self


class AwaitingManager(_FrozenModel):
    kind: Literal["awaiting_manager"] = "awaiting_manager"
    item_id: str
    public_kind: ManagerPublicKind
    route: RouteTarget | None = None
    attempt: int | None = Field(default=None, ge=1)
    handling: HandlingAssignment

    @model_validator(mode="after")
    def _validate_manager_context_and_handling(self) -> Self:
        _require_handling(self.handling, kind="manager_item", ref=self.item_id)
        if self.public_kind == "dispatched":
            if self.route is None or self.attempt is None:
                raise ValueError(
                    "dispatched AwaitingManager에는 이전 route와 attempt가 필요합니다."
                )
        elif self.route is not None or self.attempt is not None:
            raise ValueError(
                "contested/unowned AwaitingManager에는 route와 attempt를 둘 수 없습니다."
            )
        return self


class AwaitingApproval(_FrozenModel):
    kind: Literal["awaiting_approval"] = "awaiting_approval"
    route: RouteTarget
    attempt: int = Field(ge=1)
    draft_ref: str
    handling: HandlingAssignment

    @model_validator(mode="after")
    def _validate_handling(self) -> Self:
        _require_handling(
            self.handling,
            kind="approval_item",
            ref=self.draft_ref,
        )
        return self


class AnsweredRequest(_FrozenModel):
    kind: Literal["answered"] = "answered"
    record_id: str


class DeclinedRequest(_FrozenModel):
    kind: Literal["declined"] = "declined"
    reason_code: str


class FailedRequest(_FrozenModel):
    kind: Literal["failed"] = "failed"
    error_code: str


QuestionRequestState: TypeAlias = Annotated[
    Received
    | ReadyToDispatch
    | AwaitingAnswer
    | AwaitingConflict
    | AwaitingManager
    | AwaitingApproval
    | AnsweredRequest
    | DeclinedRequest
    | FailedRequest,
    Field(discriminator="kind"),
]

RequestStateKind: TypeAlias = Literal[
    "received",
    "ready_to_dispatch",
    "awaiting_answer",
    "awaiting_conflict",
    "awaiting_manager",
    "awaiting_approval",
    "answered",
    "declined",
    "failed",
]

_TERMINAL_KINDS: frozenset[RequestStateKind] = frozenset({"answered", "declined", "failed"})
_ALLOWED_TRANSITIONS: dict[RequestStateKind, frozenset[RequestStateKind]] = {
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
_INITIAL_TARGET_KIND: dict[InitialDisposition, RequestStateKind] = {
    "routed": "ready_to_dispatch",
    "contested": "awaiting_conflict",
    "unowned": "awaiting_manager",
}


def _transition_violation(
    current: QuestionRequestState,
    target: QuestionRequestState,
) -> str | None:
    """ADR 0042 전이표 위반 사유를 돌려준다(None이면 허용)."""
    if current.kind in _TERMINAL_KINDS:
        return f"terminal 상태 {current.kind!r}는 부활할 수 없습니다."
    if current.kind == target.kind:
        return f"same-state 임의 덮어쓰기는 허용되지 않습니다: {current.kind!r}"
    if target.kind not in _ALLOWED_TRANSITIONS[current.kind]:
        return f"허용되지 않은 Question Request 전이: {current.kind!r} → {target.kind!r}"

    if isinstance(current, ReadyToDispatch):
        if isinstance(target, (AwaitingAnswer, AwaitingApproval)) and (
            target.route != current.route or target.attempt != current.attempt
        ):
            return "ReadyToDispatch의 route와 attempt는 실행/승인 대기까지 같아야 합니다."
        if isinstance(target, AnsweredRequest) and current.route.requires_approval:
            return "승인이 필요한 route는 AwaitingApproval을 거쳐야 합니다."

    if isinstance(current, AwaitingAnswer):
        if isinstance(target, AwaitingApproval) and (
            target.route != current.route or target.attempt != current.attempt
        ):
            return "AwaitingAnswer의 route와 attempt는 승인 대기까지 같아야 합니다."
        if isinstance(target, AnsweredRequest) and current.route.requires_approval:
            return "승인이 필요한 route는 AwaitingApproval을 거쳐야 합니다."
        if isinstance(target, AwaitingManager):
            if target.public_kind != "dispatched":
                return (
                    "AwaitingAnswer에서 Manager 대기로 갈 때 public_kind는 'dispatched'여야 합니다."
                )
            if target.route != current.route or target.attempt != current.attempt:
                return "dispatched AwaitingManager는 직전 실행의 route와 attempt를 보존해야 합니다."

    if isinstance(current, AwaitingConflict):
        if isinstance(target, AwaitingManager) and target.public_kind != "contested":
            return "AwaitingConflict에서 Manager 대기로 갈 때 public_kind는 'contested'여야 합니다."
        if isinstance(target, ReadyToDispatch) and target.attempt != 1:
            return "Conflict 해소 후 첫 실행 attempt는 1이어야 합니다."

    if isinstance(current, AwaitingManager) and isinstance(target, ReadyToDispatch):
        expected_attempt = (
            current.attempt + 1
            if current.public_kind == "dispatched" and current.attempt is not None
            else 1
        )
        if target.attempt != expected_attempt:
            return f"Manager 해소 후 실행 attempt는 {expected_attempt}이어야 합니다."
    return None


def _approval_reassignment_violation(
    current: AwaitingApproval,
    target: AwaitingApproval,
) -> str | None:
    """전용 Approval 재지정이 허용하는 유일한 same-state shape."""
    if (
        current.handling.kind != "approval_item"
        or current.handling.ref != current.draft_ref
        or target.handling.kind != "approval_item"
        or target.handling.ref != target.draft_ref
    ):
        return "Approval 재지정 handling은 각 Item ID와 exact-link되어야 합니다."
    if target.route != current.route:
        return "Approval 재지정은 기존 RouteTarget을 바꿀 수 없습니다."
    if target.attempt != current.attempt:
        return "Approval 재지정은 실행 attempt를 바꿀 수 없습니다."
    if target.draft_ref == current.draft_ref:
        return "Approval 재지정은 다른 successor Item ID를 가리켜야 합니다."
    return None


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _require_handling(
    handling: HandlingAssignment,
    *,
    kind: HandlingKind,
    ref: str | None = None,
) -> None:
    if handling.kind != kind:
        raise ValueError(f"이 상태의 HandlingAssignment.kind는 {kind!r}여야 합니다.")
    if ref is not None and handling.ref != ref:
        raise ValueError("HandlingAssignment.ref는 상태가 가리키는 처리 객체와 같아야 합니다.")


def _nonterminal_handling(
    state: QuestionRequestState,
) -> HandlingAssignment | None:
    if isinstance(state, (AnsweredRequest, DeclinedRequest, FailedRequest)):
        return None
    return state.handling


class QuestionRequest(_FrozenModel):
    """질문 한 건의 접수부터 terminal까지를 대표하는 frozen aggregate."""

    request_id: str
    org_id: str
    requester_id: str
    session_id: str | None = None
    question: str
    context_snapshot: str | None = None
    intent: str | None = None
    initial_disposition: InitialDisposition | None = None
    state: QuestionRequestState
    revision: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def _timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if not _is_timezone_aware(value):
            raise ValueError("QuestionRequest 시간은 timezone-aware여야 합니다.")
        return value

    @model_validator(mode="after")
    def _validate_aggregate_consistency(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at은 created_at보다 빠를 수 없습니다.")

        handling = _nonterminal_handling(self.state)
        if handling is not None and handling.due_at < self.updated_at:
            raise ValueError(
                "비종결 상태의 HandlingAssignment.due_at은 updated_at보다 빠를 수 없습니다."
            )

        disposition = self.initial_disposition
        if isinstance(self.state, Received) and (
            self.intent is not None or disposition is not None
        ):
            raise ValueError("Received에는 intent와 initial_disposition을 직접 기록할 수 없습니다.")

        if disposition is None:
            if self.intent is not None:
                raise ValueError("initial_disposition이 없으면 intent도 None이어야 합니다.")
            if not isinstance(self.state, (Received, FailedRequest)):
                raise ValueError(
                    "Received와 라우팅 전 FailedRequest 외 상태에는 "
                    "initial_disposition이 필요합니다."
                )
        elif disposition in ("routed", "contested") and self.intent is None:
            raise ValueError(
                "routed/contested initial_disposition에는 nonblank intent가 필요합니다."
            )

        if self.intent is not None and isinstance(
            self.state,
            (ReadyToDispatch, AwaitingAnswer, AwaitingApproval),
        ):
            if self.state.route.intent != self.intent:
                raise ValueError("QuestionRequest.intent와 state.route.intent는 같아야 합니다.")

        if isinstance(self.state, AwaitingConflict) and disposition != "contested":
            raise ValueError("AwaitingConflict는 contested initial_disposition에서만 가능합니다.")

        if isinstance(self.state, AwaitingManager):
            if self.state.public_kind == "unowned" and disposition != "unowned":
                raise ValueError(
                    "unowned AwaitingManager에는 unowned initial_disposition이 필요합니다."
                )
            if self.state.public_kind == "contested" and disposition != "contested":
                raise ValueError(
                    "contested AwaitingManager에는 contested initial_disposition이 필요합니다."
                )
            if (
                self.state.public_kind == "dispatched"
                and self.intent is not None
                and self.state.route is not None
                and self.state.route.intent != self.intent
            ):
                raise ValueError(
                    "dispatched AwaitingManager의 route.intent는 "
                    "QuestionRequest.intent와 같아야 합니다."
                )
        return self

    @classmethod
    def receive(
        cls,
        *,
        org_id: str,
        requester_id: str,
        question: str,
        request_id_factory: RequestIdFactory,
        clock: Clock,
        due_at: datetime,
        session_id: str | None = None,
        context_snapshot: str | None = None,
    ) -> Self:
        """질문을 Received revision 0으로 접수한다(id·clock은 호출자가 주입)."""
        request_id = request_id_factory()
        now = clock()
        return cls(
            request_id=request_id,
            org_id=org_id,
            requester_id=requester_id,
            session_id=session_id,
            question=question,
            context_snapshot=context_snapshot,
            state=Received(
                handling=HandlingAssignment(
                    kind="system",
                    ref=f"question-intake:{request_id}",
                    due_at=due_at,
                )
            ),
            revision=0,
            created_at=now,
            updated_at=now,
        )

    @property
    def is_terminal(self) -> bool:
        return self.state.kind in _TERMINAL_KINDS

    def transition(
        self,
        target: QuestionRequestState,
        *,
        clock: Clock,
    ) -> QuestionRequest:
        """ADR 허용 전이만 적용하고 revision·updated_at을 한 칸 전진시킨다."""
        if (
            isinstance(self.state, Received)
            and self.intent is None
            and self.initial_disposition is None
            and isinstance(
                target,
                (ReadyToDispatch, AwaitingConflict, AwaitingManager),
            )
        ):
            raise QuestionRequestTransitionError(
                "최초 라우팅은 record_initial_routing(...)으로 기록해야 합니다."
            )

        violation = _transition_violation(self.state, target)
        if violation is not None:
            raise QuestionRequestTransitionError(violation)

        now = self._transition_time(clock)

        if (
            not isinstance(
                target,
                (AnsweredRequest, DeclinedRequest, FailedRequest),
            )
            and target.handling.due_at < now
        ):
            raise QuestionRequestTransitionError(
                "target HandlingAssignment.due_at은 전이 시각보다 빠를 수 없습니다."
            )

        return QuestionRequest(
            request_id=self.request_id,
            org_id=self.org_id,
            requester_id=self.requester_id,
            session_id=self.session_id,
            question=self.question,
            context_snapshot=self.context_snapshot,
            intent=self.intent,
            initial_disposition=self.initial_disposition,
            state=target,
            revision=self.revision + 1,
            created_at=self.created_at,
            updated_at=now,
        )

    def reassign_approval(
        self,
        *,
        previous_item_id: str,
        successor_item_id: str,
        due_at: datetime,
        clock: Clock,
    ) -> QuestionRequest:
        """Approval assignment만 새 Item과 SLA로 교체하는 전용 same-state 전이."""
        current = self.state
        if not isinstance(current, AwaitingApproval):
            raise QuestionRequestTransitionError(
                "Approval 재지정은 AwaitingApproval 상태에서만 가능합니다."
            )
        if previous_item_id != current.draft_ref:
            raise QuestionRequestTransitionError(
                "Approval 재지정 previous Item ID가 현재 Request와 다릅니다."
            )
        if successor_item_id == previous_item_id:
            raise QuestionRequestTransitionError(
                "Approval 재지정 successor Item ID는 이전 Item과 달라야 합니다."
            )

        now = self._transition_time(clock)
        try:
            target = AwaitingApproval(
                route=current.route,
                attempt=current.attempt,
                draft_ref=successor_item_id,
                handling=HandlingAssignment(
                    kind="approval_item",
                    ref=successor_item_id,
                    due_at=due_at,
                ),
            )
        except Exception as error:
            raise QuestionRequestTransitionError(
                "Approval 재지정 Item ID 또는 SLA가 유효하지 않습니다."
            ) from error
        violation = _approval_reassignment_violation(current, target)
        if violation is not None:
            raise QuestionRequestTransitionError(violation)
        if target.handling.due_at < now:
            raise QuestionRequestTransitionError(
                "Approval 재지정 SLA는 전이 시각보다 빠를 수 없습니다."
            )
        return QuestionRequest(
            request_id=self.request_id,
            org_id=self.org_id,
            requester_id=self.requester_id,
            session_id=self.session_id,
            question=self.question,
            context_snapshot=self.context_snapshot,
            intent=self.intent,
            initial_disposition=self.initial_disposition,
            state=target,
            revision=self.revision + 1,
            created_at=self.created_at,
            updated_at=now,
        )

    def record_initial_routing(
        self,
        *,
        intent: str | None,
        disposition: InitialDisposition,
        target: QuestionRequestState,
        clock: Clock,
    ) -> QuestionRequest:
        """첫 Router 결과를 metadata·상태·revision 한 전이로 원자 표현한다.

        Received이면서 intent/initial_disposition이 아직 비어 있을 때만 가능하다.
        disposition별 target 상태를 고정해 최초 라우팅 사실과 수명 상태가 갈리지 않게 한다.
        """
        violation = _initial_routing_violation(self, intent, disposition, target)
        if violation is not None:
            raise QuestionRequestTransitionError(violation)
        now = self._transition_time(clock)
        if not isinstance(
            target,
            (ReadyToDispatch, AwaitingConflict, AwaitingManager),
        ):
            raise QuestionRequestTransitionError(
                "최초 라우팅 target은 Routed·Contested·Unowned 상태여야 합니다."
            )
        if target.handling.due_at < now:
            raise QuestionRequestTransitionError(
                "target HandlingAssignment.due_at은 전이 시각보다 빠를 수 없습니다."
            )
        return QuestionRequest(
            request_id=self.request_id,
            org_id=self.org_id,
            requester_id=self.requester_id,
            session_id=self.session_id,
            question=self.question,
            context_snapshot=self.context_snapshot,
            intent=intent,
            initial_disposition=disposition,
            state=target,
            revision=self.revision + 1,
            created_at=self.created_at,
            updated_at=now,
        )

    def _transition_time(self, clock: Clock) -> datetime:
        now = clock()
        if not _is_timezone_aware(now):
            raise QuestionRequestTransitionError("전이 clock은 timezone-aware여야 합니다.")
        if now < self.updated_at:
            raise QuestionRequestTransitionError("updated_at은 역행할 수 없습니다.")
        return now


def question_pending_kind(request: QuestionRequest) -> QuestionPendingKind:
    """Question Request 전체 state를 내부 참조 없는 사용자 Pending 분류로 축약한다."""
    state = request.state
    if isinstance(state, Received):
        return "routing"
    if isinstance(state, (ReadyToDispatch, AwaitingAnswer, AwaitingApproval)):
        return "routed"
    if isinstance(state, AwaitingConflict):
        return "contested"
    if isinstance(state, AwaitingManager):
        if state.public_kind == "unowned":
            return "unowned"
        if state.public_kind == "contested":
            return "contested"
        return "routed"
    raise QuestionRequestTransitionError("terminal Question Request에는 Pending kind가 없습니다.")


def _initial_routing_violation(
    current: QuestionRequest,
    intent: str | None,
    disposition: InitialDisposition,
    target: QuestionRequestState,
) -> str | None:
    if not isinstance(current.state, Received):
        return "최초 라우팅은 Received 상태에서만 기록할 수 있습니다."
    if current.intent is not None or current.initial_disposition is not None:
        return "최초 라우팅 metadata는 한 번만 기록할 수 있습니다."
    if intent is not None and not intent.strip():
        return "최초 라우팅 intent는 nonblank여야 합니다."
    if disposition in ("routed", "contested") and intent is None:
        return f"{disposition} 최초 라우팅에는 nonblank intent가 필요합니다."
    expected_kind = _INITIAL_TARGET_KIND.get(disposition)
    if expected_kind is None:
        return f"알 수 없는 initial disposition: {disposition!r}"
    if target.kind != expected_kind:
        return f"initial disposition {disposition!r}의 target은 {expected_kind!r}여야 합니다."
    if isinstance(target, ReadyToDispatch) and target.route.intent != intent:
        return "최초 routed RouteTarget.intent는 QuestionRequest.intent와 같아야 합니다."
    if isinstance(target, ReadyToDispatch) and target.attempt != 1:
        return "최초 routed 실행 attempt는 1이어야 합니다."
    if isinstance(target, AwaitingManager) and target.public_kind != "unowned":
        return "최초 unowned AwaitingManager.public_kind는 'unowned'여야 합니다."
    return None


def validate_compare_and_set_semantics(
    request_id: str,
    expected_revision: int,
    current: QuestionRequest,
    updated: QuestionRequest,
) -> None:
    """Store 구현이 공유하는 authoritative Question Request CAS 의미 검증.

    저장소별 원자성 구현과 분리해 InMemory·SQLite가 같은 ID, revision, 불변
    envelope, 최초 라우팅, 상태 전이 연속성 규칙을 적용하게 한다. 저장된 현재값과
    ``current``의 exact equality 판정은 원자 저장소 내부에서 수행한다.
    """
    if not request_id.strip():
        raise CompareAndSetError("CAS request_id는 nonblank여야 합니다.")
    if current.request_id != request_id or updated.request_id != request_id:
        raise CompareAndSetError(
            "CAS request_id는 current·updated aggregate의 request_id와 같아야 합니다."
        )
    if current.revision != expected_revision:
        raise CompareAndSetError("expected_revision은 current.revision과 같아야 합니다.")
    if updated.revision != expected_revision + 1:
        raise CompareAndSetError("CAS updated.revision은 expected_revision + 1이어야 합니다.")
    if updated.updated_at < current.updated_at:
        raise CompareAndSetError("CAS updated_at은 역행할 수 없습니다.")
    if isinstance(current.state, AwaitingApproval) and isinstance(
        updated.state,
        AwaitingApproval,
    ):
        violation = _approval_reassignment_violation(current.state, updated.state)
        if violation is None and updated.state.handling.due_at < updated.updated_at:
            violation = "Approval 재지정 SLA는 updated_at보다 빠를 수 없습니다."
    else:
        violation = _transition_violation(current.state, updated.state)
    if violation is not None:
        raise CompareAndSetError(violation)

    immutable_envelope = (
        "org_id",
        "requester_id",
        "session_id",
        "question",
        "context_snapshot",
        "created_at",
    )
    changed = tuple(
        field for field in immutable_envelope if getattr(current, field) != getattr(updated, field)
    )
    if changed:
        raise CompareAndSetError(
            f"CAS는 QuestionRequest immutable envelope를 바꿀 수 없습니다: {changed!r}"
        )

    metadata_changed = (
        current.intent != updated.intent
        or current.initial_disposition != updated.initial_disposition
    )
    if metadata_changed:
        if updated.initial_disposition is None:
            raise CompareAndSetError("최초 라우팅 metadata에는 initial_disposition이 필요합니다.")
        initial_violation = _initial_routing_violation(
            current,
            updated.intent,
            updated.initial_disposition,
            updated.state,
        )
        if initial_violation is not None:
            raise CompareAndSetError(initial_violation)


def validate_new_question_request_semantics(request: QuestionRequest) -> None:
    """Store 신규 등록은 아직 전이되지 않은 접수 원형만 허용한다."""
    violations: list[str] = []
    if not isinstance(request.state, Received):
        violations.append("state는 Received여야 합니다")
    elif request.state.handling.ref != f"question-intake:{request.request_id}":
        violations.append("Received handling.ref는 question-intake:{request_id}여야 합니다")
    if request.revision != 0:
        violations.append("revision은 0이어야 합니다")
    if request.created_at != request.updated_at:
        violations.append("created_at과 updated_at은 같아야 합니다")
    if violations:
        raise InvalidNewQuestionRequestError(
            "신규 Question Request가 접수 원형이 아닙니다: " + ", ".join(violations)
        )


class QuestionRequestStore(Protocol):
    """Question Request 수명의 단일 저장 포트.

    반환 계약:
      - create: 저장한 frozen aggregate 반환, duplicate면 예외.
      - get: 현재 aggregate 또는 None.
      - compare_and_set: 교체 성공 True, 정상 경쟁 패배/미존재 False, 호출 계약 위반은 예외.
      - nonterminal: terminal 제외 snapshot을 (created_at, request_id)로 정렬해 반환.
    """

    def create(self, request: QuestionRequest) -> QuestionRequest: ...

    def get(self, request_id: str) -> QuestionRequest | None: ...

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool: ...

    def nonterminal(self) -> list[QuestionRequest]: ...


class InMemoryQuestionRequestStore:
    """단일 프로세스 개발·결정론 테스트용 thread-safe revision CAS Store."""

    workflow_durability: Literal["ephemeral", "durable"] = "ephemeral"

    def __init__(self) -> None:
        self._requests: dict[str, QuestionRequest] = {}
        self._lock = RLock()

    @staticmethod
    def _copy(request: QuestionRequest) -> QuestionRequest:
        return QuestionRequest.model_validate(
            request.model_dump(mode="python", round_trip=True),
            strict=True,
        )

    def create(self, request: QuestionRequest) -> QuestionRequest:
        canonical = self._copy(request)
        validate_new_question_request_semantics(canonical)
        with self._lock:
            if canonical.request_id in self._requests:
                raise DuplicateQuestionRequestError(
                    f"이미 존재하는 Question Request: {canonical.request_id!r}"
                )
            self._requests[canonical.request_id] = self._copy(canonical)
            return self._copy(canonical)

    def get(self, request_id: str) -> QuestionRequest | None:
        with self._lock:
            stored = self._requests.get(request_id)
            return None if stored is None else self._copy(stored)

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        canonical_current = self._copy(current)
        canonical_updated = self._copy(updated)
        validate_compare_and_set_semantics(
            request_id,
            expected_revision,
            canonical_current,
            canonical_updated,
        )
        with self._lock:
            stored = self._requests.get(request_id)
            if stored is None:
                return False
            if stored.revision != expected_revision or stored != canonical_current:
                return False
            self._requests[request_id] = self._copy(canonical_updated)
            return True

    def nonterminal(self) -> list[QuestionRequest]:
        with self._lock:
            snapshot = [
                self._copy(request)
                for request in self._requests.values()
                if not request.is_terminal
            ]
        return sorted(snapshot, key=lambda request: (request.created_at, request.request_id))
