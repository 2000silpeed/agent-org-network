"""ApprovalDraft의 본문 없는 terminal 증거와 보존 적격성 값 객체.

이 모듈은 보존 정책의 입력·출력 shape만 정의한다. Draft redaction·삭제·purge 실행이나
완료 증명은 다루지 않는다. terminal 증거에는 식별자와 digest만 두며 질문·초안·수정
본문·source·reason 원문을 저장하지 않는다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol, Self, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


Digest64: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]


def _aware(value: datetime, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}은 timezone-aware여야 합니다.")
    return value.astimezone(UTC)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    @field_validator("*", mode="after")
    @classmethod
    def _strings_must_be_nonblank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("문자열 값은 비어 있거나 공백일 수 없습니다.")
        return value


class ApprovalAnsweredTerminalEvidence(_FrozenModel):
    """승인된 답의 exact terminal을 가리키는 본문 없는 증거."""

    kind: Literal["answered"] = "answered"
    org_id: str
    request_id: str
    current_item_id: str
    draft_id: str
    approval_round: int = Field(ge=1)
    request_revision: int = Field(ge=1)
    record_id: str
    terminal_digest: Digest64
    candidate_digest: Digest64
    action_digest: Digest64
    approval_policy_version: str
    terminal_at: datetime

    @field_validator("terminal_at", mode="after")
    @classmethod
    def _terminal_at_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "ApprovalAnsweredTerminalEvidence.terminal_at")


class ApprovalDeclinedTerminalEvidence(_FrozenModel):
    """사람 Reject로 종결된 Request의 본문 없는 증거."""

    kind: Literal["declined"] = "declined"
    org_id: str
    request_id: str
    current_item_id: str
    draft_id: str
    approval_round: int = Field(ge=1)
    request_revision: int = Field(ge=1)
    reason_digest: Digest64
    action_digest: Digest64
    approval_policy_version: str
    terminal_at: datetime

    @field_validator("terminal_at", mode="after")
    @classmethod
    def _terminal_at_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "ApprovalDeclinedTerminalEvidence.terminal_at")


class ApprovalUnavailableTerminalEvidence(_FrozenModel):
    """승인자 부재 정책으로 Failed가 된 Request의 본문 없는 증거."""

    kind: Literal["unavailable"] = "unavailable"
    org_id: str
    request_id: str
    current_item_id: str
    draft_id: str
    approval_round: int = Field(ge=1)
    request_revision: int = Field(ge=1)
    error_code: Literal["approval_unavailable"] = "approval_unavailable"
    evidence_digest: Digest64
    candidate_digest: Digest64
    approval_policy_version: str
    lifecycle_policy_version: str
    terminal_at: datetime

    @field_validator("terminal_at", mode="after")
    @classmethod
    def _terminal_at_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "ApprovalUnavailableTerminalEvidence.terminal_at")


ApprovalDraftTerminalEvidence: TypeAlias = Annotated[
    ApprovalAnsweredTerminalEvidence
    | ApprovalDeclinedTerminalEvidence
    | ApprovalUnavailableTerminalEvidence,
    Field(discriminator="kind"),
]


_TERMINAL_TYPES = (
    ApprovalAnsweredTerminalEvidence,
    ApprovalDeclinedTerminalEvidence,
    ApprovalUnavailableTerminalEvidence,
)


class ApprovalDraftRetentionDecision(_FrozenModel):
    """한 exact terminal과 평가 시각에 결박된 보존 정책 결과."""

    terminal: ApprovalDraftTerminalEvidence
    evaluated_at: datetime
    policy_version: str
    retain_until: datetime
    purge_eligible: bool

    @field_validator("terminal", mode="before")
    @classmethod
    def _terminal_model_subclass_is_not_a_sealed_arm(cls, value: object) -> object:
        if isinstance(value, _TERMINAL_TYPES) and type(value) not in _TERMINAL_TYPES:
            raise ValueError("Approval terminal evidence exact arm이 필요합니다.")
        return value

    @field_validator("evaluated_at", "retain_until", mode="after")
    @classmethod
    def _times_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "ApprovalDraftRetentionDecision timestamp")

    @model_validator(mode="after")
    def _time_bounds_must_be_safe(self) -> Self:
        if type(self.terminal) not in _TERMINAL_TYPES:
            raise ValueError("Approval terminal evidence exact arm이 필요합니다.")
        terminal_at = self.terminal.terminal_at
        if self.evaluated_at < terminal_at:
            raise ValueError("보존 평가는 terminal 시각보다 빠를 수 없습니다.")
        if self.retain_until < terminal_at:
            raise ValueError("retain_until은 terminal 시각보다 빠를 수 없습니다.")
        if self.purge_eligible and self.evaluated_at < self.retain_until:
            raise ValueError("retain_until 전에는 purge eligible일 수 없습니다.")
        return self


class ApprovalDraftRetentionPolicy(Protocol):
    """exact terminal 뒤 Draft 보존 기한과 purge 적격성만 판정하는 정책 포트."""

    def evaluate(
        self,
        *,
        terminal: ApprovalDraftTerminalEvidence,
        evaluated_at: datetime,
    ) -> ApprovalDraftRetentionDecision: ...


class ApprovalDraftRetained(_FrozenModel):
    """terminal 증거가 없거나 수렴 중이라 Draft를 계속 보존한다는 상태."""

    kind: Literal["retained"] = "retained"
    reason: Literal[
        "active_assignment",
        "finalization_pending",
        "terminalization_pending",
    ]
    purge_eligible: Literal[False] = False
    retain_until: None = None


class ApprovalDraftRetentionEvaluated(_FrozenModel):
    """정책이 계산한 보존 기한과 적격성. 삭제 완료를 뜻하지 않는다."""

    kind: Literal["evaluated"] = "evaluated"
    retain_until: datetime
    purge_eligible: bool
    policy_version: str

    @field_validator("retain_until", mode="after")
    @classmethod
    def _retain_until_must_be_aware(cls, value: datetime) -> datetime:
        return _aware(value, "ApprovalDraftRetentionEvaluated.retain_until")


ApprovalDraftRetentionStatus: TypeAlias = Annotated[
    ApprovalDraftRetained | ApprovalDraftRetentionEvaluated,
    Field(discriminator="kind"),
]
