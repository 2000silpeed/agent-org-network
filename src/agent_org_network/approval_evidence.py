"""본문을 보관하지 않는 process-local Approval 사건 증거(P17.6b S4.1)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from threading import RLock
from typing import Annotated, ClassVar, Literal, Protocol, TypeAlias, cast, final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)

APPROVAL_CANDIDATE_DIGEST_DOMAIN = "aon.approval.candidate.v1"
APPROVAL_ACTION_DIGEST_DOMAIN = "aon.approval.action.v1"
APPROVAL_EVENT_DIGEST_DOMAIN = "aon.approval.event.v1"

_SHA256_PATTERN = r"[0-9a-f]{64}"
_EVENT_ID_PATTERN = re.compile(r"approval-event-[0-9a-f]{64}\Z")

Sha256Digest: TypeAlias = Annotated[str, Field(pattern=_SHA256_PATTERN)]
ApprovalSystemId: TypeAlias = Literal[
    "approval_boundary",
    "approval_operations",
    "approval_expiry",
    "approval_retention",
]


class ApprovalEvidenceError(RuntimeError):
    """Approval 증거 경계의 field-free 오류."""

    code: str
    retryable: bool


class ApprovalEvidenceDependency(ApprovalEvidenceError):
    code = "approval_evidence_dependency"
    retryable = True

    def __init__(self) -> None:
        super().__init__("Approval 증거 의존성을 확인할 수 없습니다.")


class ApprovalEvidenceIntegrity(ApprovalEvidenceError):
    code = "approval_evidence_integrity"
    retryable = False

    def __init__(self) -> None:
        super().__init__("Approval 증거 무결성을 확인할 수 없습니다.")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    @field_validator("*", mode="after")
    @classmethod
    def _strings_must_be_nonblank(cls, value: object, info: ValidationInfo) -> object:
        # event_id의 빈 default는 model-level validator가 결정론 ID로 교체한다.
        if info.field_name != "event_id" and isinstance(value, str) and not value.strip():
            raise ValueError("문자열 값은 비어 있거나 공백일 수 없습니다.")
        return value


@final
class ApprovalHumanSubject(_FrozenModel):
    kind: Literal["human"] = "human"
    subject_id: str


@final
class ApprovalSystemSubject(_FrozenModel):
    kind: Literal["system"] = "system"
    system_id: ApprovalSystemId


ApprovalEventSubject: TypeAlias = Annotated[
    ApprovalHumanSubject | ApprovalSystemSubject,
    Field(discriminator="kind"),
]


class _ApprovalEventBase(_FrozenModel):
    """모든 사건에 허용되는 ID·digest·policy·time 공통 필드."""

    logical_slot: ClassVar[str]

    event_id: str = ""
    org_id: str
    request_id: str
    item_id: str
    draft_id: str
    approval_round: int = Field(ge=1)
    subject: ApprovalEventSubject
    candidate_digest: Sha256Digest
    policy_version: str
    occurred_at: datetime

    @field_validator("occurred_at", mode="after")
    @classmethod
    def _occurred_at_must_be_aware(cls, value: datetime) -> datetime:
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Approval 사건 시각은 timezone-aware여야 합니다.")
        return value.astimezone(UTC)

    @field_validator("subject", mode="after")
    @classmethod
    def _subject_must_be_exact(cls, value: ApprovalEventSubject) -> ApprovalEventSubject:
        if type(value) not in (ApprovalHumanSubject, ApprovalSystemSubject):
            raise ValueError("Approval 사건 주체는 sealed exact type이어야 합니다.")
        return value

    @model_validator(mode="after")
    def _event_id_must_match_logical_identity(self) -> _ApprovalEventBase:
        expected = _derive_event_id(self)
        if self.event_id == "":
            object.__setattr__(self, "event_id", expected)
        elif self.event_id != expected:
            raise ValueError("Approval event_id와 논리 사건 identity가 다릅니다.")
        if _EVENT_ID_PATTERN.fullmatch(self.event_id) is None:
            raise ValueError("Approval event_id 형식이 올바르지 않습니다.")
        return self


@final
class ApprovalRequestedEvent(_ApprovalEventBase):
    logical_slot = "requested"
    kind: Literal["requested"] = "requested"

    @model_validator(mode="after")
    def _requested_subject_and_round_are_valid(self) -> ApprovalRequestedEvent:
        if (
            type(self.subject) is not ApprovalSystemSubject
            or self.subject.system_id != "approval_boundary"
            or self.approval_round != 1
        ):
            raise ValueError("requested 사건의 주체 또는 세대가 올바르지 않습니다.")
        return self


class _ApprovalDecisionEvent(_ApprovalEventBase):
    logical_slot = "decision"
    action_digest: Sha256Digest

    @model_validator(mode="after")
    def _decision_subject_is_human(self) -> _ApprovalDecisionEvent:
        if type(self.subject) is not ApprovalHumanSubject:
            raise ValueError("Approval 처분 사건에는 human 주체가 필요합니다.")
        return self


@final
class ApprovalApprovedEvent(_ApprovalDecisionEvent):
    kind: Literal["approved"] = "approved"
    terminal_record_id: str


@final
class ApprovalApprovedWithEditEvent(_ApprovalDecisionEvent):
    kind: Literal["approved_with_edit"] = "approved_with_edit"
    terminal_record_id: str


@final
class ApprovalRejectedEvent(_ApprovalDecisionEvent):
    kind: Literal["rejected"] = "rejected"
    reason_digest: Sha256Digest


@final
class ApprovalReassignedEvent(_ApprovalEventBase):
    logical_slot = "reassigned"
    kind: Literal["reassigned"] = "reassigned"
    predecessor_item_id: str
    action_digest: Sha256Digest

    @model_validator(mode="after")
    def _generation_link_is_valid(self) -> ApprovalReassignedEvent:
        if self.approval_round < 2 or self.predecessor_item_id == self.item_id:
            raise ValueError("reassigned 사건의 세대 링크가 올바르지 않습니다.")
        if (
            type(self.subject) is ApprovalSystemSubject
            and self.subject.system_id != "approval_expiry"
        ):
            raise ValueError("자동 재지정은 expiry system 주체만 기록할 수 있습니다.")
        return self


@final
class ApprovalExpiredEvent(_ApprovalEventBase):
    logical_slot = "expired"
    kind: Literal["expired"] = "expired"
    action_digest: Sha256Digest

    @model_validator(mode="after")
    def _subject_is_expiry(self) -> ApprovalExpiredEvent:
        if (
            type(self.subject) is not ApprovalSystemSubject
            or self.subject.system_id != "approval_expiry"
        ):
            raise ValueError("expired 사건에는 expiry system 주체가 필요합니다.")
        return self


@final
class ApprovalUnavailableEvent(_ApprovalEventBase):
    logical_slot = "unavailable"
    kind: Literal["unavailable"] = "unavailable"
    action_digest: Sha256Digest
    error_ref: Literal["approval_unavailable"] = "approval_unavailable"

    @model_validator(mode="after")
    def _subject_is_expiry(self) -> ApprovalUnavailableEvent:
        if (
            type(self.subject) is not ApprovalSystemSubject
            or self.subject.system_id != "approval_expiry"
        ):
            raise ValueError("unavailable 사건에는 expiry system 주체가 필요합니다.")
        return self


@final
class ApprovalRetentionEligibleEvent(_ApprovalEventBase):
    logical_slot = "retention_eligible"
    kind: Literal["retention_eligible"] = "retention_eligible"
    terminal_kind: Literal["answered", "declined", "unavailable"]
    request_revision: int = Field(ge=1)
    terminal_at: datetime
    terminal_evidence_digest: Sha256Digest
    retain_until: datetime

    @field_validator("terminal_at", "retain_until", mode="after")
    @classmethod
    def _retention_times_must_be_aware(cls, value: datetime) -> datetime:
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Approval retention 시각은 timezone-aware여야 합니다.")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _subject_is_retention(self) -> ApprovalRetentionEligibleEvent:
        if (
            type(self.subject) is not ApprovalSystemSubject
            or self.subject.system_id != "approval_retention"
            or self.retain_until < self.terminal_at
            or self.occurred_at != self.retain_until
        ):
            raise ValueError("retention eligibility 사건 증거가 올바르지 않습니다.")
        return self


ApprovalEvent: TypeAlias = Annotated[
    ApprovalRequestedEvent
    | ApprovalApprovedEvent
    | ApprovalApprovedWithEditEvent
    | ApprovalRejectedEvent
    | ApprovalReassignedEvent
    | ApprovalExpiredEvent
    | ApprovalUnavailableEvent
    | ApprovalRetentionEligibleEvent,
    Field(discriminator="kind"),
]

_EVENT_TYPES = (
    ApprovalRequestedEvent,
    ApprovalApprovedEvent,
    ApprovalApprovedWithEditEvent,
    ApprovalRejectedEvent,
    ApprovalReassignedEvent,
    ApprovalExpiredEvent,
    ApprovalUnavailableEvent,
    ApprovalRetentionEligibleEvent,
)
_EVENT_ADAPTER: TypeAdapter[ApprovalEvent] = TypeAdapter(ApprovalEvent)


def _canonical_json_value(value: object) -> object:
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ApprovalEvidenceIntegrity()
        return value
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ApprovalEvidenceIntegrity()
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if type(value) in (list, tuple):
        sequence = cast(list[object] | tuple[object, ...], value)
        return [_canonical_json_value(item) for item in sequence]
    if type(value) is dict:
        plain = cast(dict[object, object], value)
        if any(type(key) is not str for key in plain):
            raise ApprovalEvidenceIntegrity()
        return {cast(str, key): _canonical_json_value(item) for key, item in plain.items()}
    if isinstance(value, BaseModel):
        try:
            dumped = value.model_dump(mode="python", round_trip=True, warnings="error")
        except Exception as error:
            raise ApprovalEvidenceIntegrity() from error
        return _canonical_json_value(dumped)
    raise ApprovalEvidenceIntegrity()


def _canonical_sha256(domain: str, payload: object) -> str:
    try:
        envelope = {"domain": domain, "payload": _canonical_json_value(payload)}
        raw = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except ApprovalEvidenceError:
        raise
    except Exception as error:
        raise ApprovalEvidenceIntegrity() from error
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def approval_candidate_digest(payload: object) -> str:
    return _canonical_sha256(APPROVAL_CANDIDATE_DIGEST_DOMAIN, payload)


def approval_action_digest(payload: object) -> str:
    return _canonical_sha256(APPROVAL_ACTION_DIGEST_DOMAIN, payload)


def approval_event_digest(payload: object) -> str:
    return _canonical_sha256(APPROVAL_EVENT_DIGEST_DOMAIN, payload)


def _derive_event_id(event: _ApprovalEventBase) -> str:
    identity: dict[str, object] = {
        "org_id": event.org_id,
        "request_id": event.request_id,
        "slot": event.logical_slot,
    }
    if event.logical_slot == "retention_eligible":
        identity.update(draft_id=event.draft_id, policy_version=event.policy_version)
    else:
        identity["item_id"] = event.item_id
    return f"approval-event-{approval_event_digest(identity)}"


def canonical_approval_event(raw: object) -> ApprovalEvent:
    """외부 port 사건을 exact sealed type의 deep canonical 복사본으로 만든다."""

    if type(raw) not in _EVENT_TYPES:
        raise ApprovalEvidenceIntegrity()
    assert isinstance(raw, _ApprovalEventBase)
    try:
        dumped = raw.model_dump(mode="python", round_trip=True, warnings="error")
        canonical = _EVENT_ADAPTER.validate_python(dumped, strict=True)
    except Exception as error:
        raise ApprovalEvidenceIntegrity() from error
    if type(canonical) is not type(raw) or canonical != raw:
        raise ApprovalEvidenceIntegrity()
    return canonical


class ApprovalEventReader(Protocol):
    """Approval 사건을 변경할 수 없는 감독용 읽기 포트."""

    def get(self, event_id: str) -> ApprovalEvent | None: ...

    def for_request(self, org_id: str, request_id: str) -> tuple[ApprovalEvent, ...]: ...


class ApprovalEventJournal(ApprovalEventReader, Protocol):
    """process-local append-once Approval 사건 journal 포트."""

    def append_batch_once(
        self,
        events: tuple[ApprovalEvent, ...],
    ) -> tuple[ApprovalEvent, ...]: ...

    def append_once(self, event: ApprovalEvent) -> ApprovalEvent: ...


@final
class InMemoryApprovalEventJournal:
    """RLock과 copy-on-write commit을 쓰는 원자 process-local journal."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._by_id: dict[str, ApprovalEvent] = {}
        self._order: tuple[str, ...] = ()

    def append_batch_once(
        self,
        events: tuple[ApprovalEvent, ...],
    ) -> tuple[ApprovalEvent, ...]:
        if type(events) is not tuple:
            raise ApprovalEvidenceIntegrity()
        canonical = tuple(canonical_approval_event(event) for event in events)
        if not canonical:
            return ()

        with self._lock:
            next_by_id = dict(self._by_id)
            next_order = list(self._order)
            for event in canonical:
                existing = next_by_id.get(event.event_id)
                if existing is not None:
                    if existing != event:
                        raise ApprovalEvidenceIntegrity()
                    continue
                next_by_id[event.event_id] = event
                next_order.append(event.event_id)
            self._by_id = next_by_id
            self._order = tuple(next_order)
            return tuple(canonical_approval_event(event) for event in canonical)

    def append_once(self, event: ApprovalEvent) -> ApprovalEvent:
        return self.append_batch_once((event,))[0]

    def get(self, event_id: str) -> ApprovalEvent | None:
        if type(event_id) is not str or not event_id.strip():
            raise ApprovalEvidenceIntegrity()
        with self._lock:
            event = self._by_id.get(event_id)
            return None if event is None else canonical_approval_event(event)

    def for_request(self, org_id: str, request_id: str) -> tuple[ApprovalEvent, ...]:
        if (
            type(org_id) is not str
            or not org_id.strip()
            or type(request_id) is not str
            or not request_id.strip()
        ):
            raise ApprovalEvidenceIntegrity()
        with self._lock:
            return tuple(
                canonical_approval_event(self._by_id[event_id])
                for event_id in self._order
                if self._by_id[event_id].org_id == org_id
                and self._by_id[event_id].request_id == request_id
            )


@final
class ApprovalEventRecorder:
    """append 결과와 exact reread가 일치할 때만 사건 기록을 확정한다."""

    def __init__(self, journal: ApprovalEventJournal) -> None:
        self._journal = journal

    def matches_journal(self, journal: ApprovalEventJournal) -> bool:
        """composition이 쓰기·읽기에 같은 journal을 쓰는지 검증한다."""
        return self._journal is journal

    def record(self, event: ApprovalEvent) -> ApprovalEvent:
        return self.record_batch((event,))[0]

    def record_batch(self, events: tuple[ApprovalEvent, ...]) -> tuple[ApprovalEvent, ...]:
        if type(events) is not tuple:
            raise ApprovalEvidenceIntegrity()
        expected = tuple(canonical_approval_event(event) for event in events)
        if not expected:
            return ()
        try:
            raw_result = self._journal.append_batch_once(expected)
        except Exception as error:
            return self._repair_response_loss(expected, error)

        if type(raw_result) is not tuple or len(raw_result) != len(expected):
            raise ApprovalEvidenceIntegrity()
        result = tuple(canonical_approval_event(event) for event in raw_result)
        if result != expected:
            raise ApprovalEvidenceIntegrity()
        return self._exact_reread(expected, missing_is_integrity=True)

    def _repair_response_loss(
        self,
        expected: tuple[ApprovalEvent, ...],
        append_error: Exception,
    ) -> tuple[ApprovalEvent, ...]:
        found: list[ApprovalEvent | None] = []
        for event in expected:
            try:
                raw = self._journal.get(event.event_id)
            except Exception as read_error:
                raise ApprovalEvidenceDependency() from read_error
            if raw is None:
                found.append(None)
                continue
            canonical = canonical_approval_event(raw)
            if canonical != event:
                raise ApprovalEvidenceIntegrity()
            found.append(canonical)
        if all(event is not None for event in found):
            return tuple(cast(ApprovalEvent, event) for event in found)
        if all(event is None for event in found):
            if isinstance(append_error, ApprovalEvidenceIntegrity):
                raise ApprovalEvidenceIntegrity() from append_error
            raise ApprovalEvidenceDependency() from append_error
        raise ApprovalEvidenceIntegrity()

    def _exact_reread(
        self,
        expected: tuple[ApprovalEvent, ...],
        *,
        missing_is_integrity: bool,
    ) -> tuple[ApprovalEvent, ...]:
        result: list[ApprovalEvent] = []
        for event in expected:
            try:
                raw = self._journal.get(event.event_id)
            except Exception as error:
                raise ApprovalEvidenceDependency() from error
            if raw is None:
                if missing_is_integrity:
                    raise ApprovalEvidenceIntegrity()
                raise ApprovalEvidenceDependency()
            canonical = canonical_approval_event(raw)
            if canonical != event:
                raise ApprovalEvidenceIntegrity()
            result.append(canonical)
        return tuple(result)
