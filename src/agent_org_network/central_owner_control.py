"""Owner-scoped Central control ports and safe projections.

This module is intentionally a capability boundary.  It does not import the
legacy web surface, an Owner workspace, or a correction service.  A composed
application must provide all three ports (authentication, reads, and writes);
missing capabilities fail closed with :class:`OwnerControlUnavailable`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
import re
from typing import Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MAX_TEXT_BYTES = 64 * 1024


class OwnerControlError(RuntimeError):
    """Base error for the Central owner-scoped control boundary."""


class OwnerControlUnavailable(OwnerControlError):
    """A required Central or source capability is unavailable."""


class OwnerControlForbidden(OwnerControlError):
    """The current session is not the bound Card Owner."""


class OwnerControlStale(OwnerControlError):
    """The supplied Card Owner binding or expected revision is stale."""


class OwnerControlAction(StrEnum):
    SUPERVISION_READ = "supervision.read"
    SUPERVISION_CORRECT = "supervision.correct"
    SCORECARD_READ = "scorecard.read"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class OwnerControlBinding(_Frozen):
    """Session-derived current Card Owner/Card generation binding.

    The binding contains no cookie, token, credential, OIDC claim, or
    workspace reference.  It is an internal proof supplied by Central's
    admission layer, not a client-selected owner claim.
    """

    org_id: str
    owner_user_id: str
    agent_card_id: str
    card_revision: int = Field(gt=0)
    assignment_generation: int = Field(gt=0)
    assignment_revision: int = Field(gt=0)
    card_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_ref: str
    credential_generation: int = Field(gt=0)
    device_thumbprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    central_origin: str

    @field_validator("org_id", "owner_user_id", "agent_card_id", "credential_ref")
    @classmethod
    def _reference(cls, value: str) -> str:
        if _REFERENCE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("central_origin")
    @classmethod
    def _origin(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("exact HTTPS origin required")
        return value

    @property
    def card_id(self) -> str:
        """Compatibility view; the canonical field is ``agent_card_id``."""
        return self.agent_card_id


class OwnerControlAnswer(_Frozen):
    """Redacted Central-held answer projection for the bound Card Owner."""

    org_id: str
    record_id: str
    request_id: str
    card_id: str
    card_revision: int = Field(gt=0)
    assignment_revision: int = Field(gt=0)
    card_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_user_id: str
    assignment_generation: int = Field(gt=0)
    mode: Literal["full", "backup"]
    question: str
    answer_text: str
    answered_at: datetime
    review_status: Literal["not_required", "pending", "reviewed"]
    needs_review: bool
    sources: tuple[str, ...] = ()

    @field_validator("org_id", "record_id", "request_id", "card_id", "owner_user_id")
    @classmethod
    def _references(cls, value: str) -> str:
        if _REFERENCE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("question", "answer_text")
    @classmethod
    def _text(cls, value: str) -> str:
        if not value:
            raise ValueError("text must not be empty")
        if len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
            raise ValueError("text exceeds bounded input")
        return value

    @field_validator("sources")
    @classmethod
    def _sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(_REFERENCE.fullmatch(value) is None for value in values):
            raise ValueError("safe source reference required")
        return values


class OwnerControlPresence(_Frozen):
    """Safe presence projection; volatile presence is never an authority."""

    org_id: str
    owner_user_id: str
    card_id: str
    card_revision: int = Field(gt=0)
    assignment_revision: int = Field(gt=0)
    card_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    assignment_generation: int = Field(gt=0)
    status: Literal["online", "offline", "unknown", "unavailable"]
    observed_at: datetime | None = None

    @field_validator("org_id", "owner_user_id", "card_id")
    @classmethod
    def _references(cls, value: str) -> str:
        if _REFERENCE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value


class OwnerControlCorrection(_Frozen):
    """Immutable correction projection; original AnswerRecord is untouched."""

    org_id: str
    correction_record_id: str
    record_id: str
    card_id: str
    card_revision: int = Field(gt=0)
    assignment_revision: int = Field(gt=0)
    card_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_user_id: str
    assignment_generation: int = Field(gt=0)
    corrected_text: str
    corrected_at: datetime
    rationale: str
    review_id: str
    idempotency_key: str
    expected_revision: int = Field(gt=0)

    @field_validator(
        "org_id", "correction_record_id", "record_id", "card_id", "owner_user_id",
        "review_id", "idempotency_key",
    )
    @classmethod
    def _references(cls, value: str) -> str:
        if _REFERENCE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("corrected_text", "rationale")
    @classmethod
    def _text(cls, value: str) -> str:
        if not value:
            raise ValueError("text must not be empty")
        if len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
            raise ValueError("text exceeds bounded input")
        return value

    @property
    def source_record_id(self) -> str:
        """Compatibility view for the immutable source AnswerRecord ID."""
        return self.record_id


class OwnerControlScorecard(_Frozen):
    """Owner self scorecard projection (no ranking, grade, or raw evidence)."""

    org_id: str
    owner_user_id: str
    card_id: str
    card_revision: int = Field(gt=0)
    assignment_revision: int = Field(gt=0)
    card_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    assignment_generation: int = Field(gt=0)
    window_since: datetime
    window_until: datetime
    total_answers: int = Field(ge=0)
    bad_feedback_answers: int = Field(ge=0)
    corrected_count: int = Field(ge=0)
    handled_rate: float | None = Field(default=None, ge=0, le=1)
    online_ratio: float | None = Field(default=None, ge=0, le=1)
    stale_ratio: float | None = Field(default=None, ge=0, le=1)
    weak_identity_note: str | None = None

    @field_validator("org_id", "owner_user_id", "card_id")
    @classmethod
    def _references(cls, value: str) -> str:
        if _REFERENCE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("weak_identity_note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        if value is not None:
            if len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
                raise ValueError("note exceeds bounded input")
        return value

    @model_validator(mode="after")
    def _window(self) -> "OwnerControlScorecard":
        if self.window_since > self.window_until:
            raise ValueError("scorecard window must be ordered")
        ratios = (self.handled_rate, self.online_ratio, self.stale_ratio)
        if any(value is None for value in ratios) and any(value is not None for value in ratios):
            raise ValueError("scorecard ratios must be complete or empty")
        return self


class OwnerControlCorrectionCommand(_Frozen):
    """Exact append-only correction command.

    ``corrected_text`` and ``rationale`` preserve caller bytes (no trim or
    normalization).  The target record is supplied by the control endpoint's
    resource binding, not as an untrusted owner claim in this DTO.
    """

    kind: Literal["correct"]
    review_id: str
    idempotency_key: str
    corrected_text: str
    rationale: str
    expected_revision: int = Field(gt=0)

    @field_validator("review_id", "idempotency_key")
    @classmethod
    def _references(cls, value: str) -> str:
        if _REFERENCE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("corrected_text", "rationale")
    @classmethod
    def _text(cls, value: str) -> str:
        if not value:
            raise ValueError("text must not be empty")
        if len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
            raise ValueError("text exceeds bounded input")
        return value


OwnerControlAnswerList: TypeAlias = tuple[OwnerControlAnswer, ...]
OwnerControlCorrectionList: TypeAlias = tuple[OwnerControlCorrection, ...]


class OwnerControlAuthPort(Protocol):
    """Resolve and reauthorize a current session-derived Owner binding."""

    def bind(self) -> OwnerControlBinding: ...

    def authorize(self, binding: OwnerControlBinding, action: str) -> bool: ...

    def reauthorize(self, binding: OwnerControlBinding, action: str) -> bool: ...


class OwnerControlReadPort(Protocol):
    """Source port for safe owner-scoped read projections."""

    def answers(
        self, binding: OwnerControlBinding, *, needs_review: bool | None = None,
    ) -> OwnerControlAnswerList: ...

    def presence(self, binding: OwnerControlBinding) -> OwnerControlPresence: ...

    def corrections(
        self, binding: OwnerControlBinding, *, record_id: str,
    ) -> OwnerControlCorrectionList: ...

    def scorecard(self, binding: OwnerControlBinding) -> OwnerControlScorecard: ...


class OwnerControlWritePort(Protocol):
    """Source port for append-only owner correction commands."""

    def correct(
        self, binding: OwnerControlBinding, command: OwnerControlCorrectionCommand,
    ) -> OwnerControlCorrection: ...


def _assert_projection_scope(
    binding: OwnerControlBinding,
    *,
    org_id: str,
    owner_user_id: str,
    card_id: str,
    card_revision: int,
    assignment_revision: int,
    assignment_generation: int,
    card_digest: str,
) -> None:
    if (
        org_id != binding.org_id
        or owner_user_id != binding.owner_user_id
        or card_id != binding.card_id
    ):
        raise OwnerControlForbidden("foreign owner/card projection")
    if (
        card_revision != binding.card_revision
        or assignment_revision != binding.assignment_revision
        or assignment_generation != binding.assignment_generation
        or card_digest != binding.card_digest
    ):
        raise OwnerControlStale("owner/card binding is stale")


class OwnerControlApplication:
    """Small fail-closed application service over the three capability ports."""

    def __init__(
        self,
        *,
        auth: OwnerControlAuthPort | None,
        reads: OwnerControlReadPort | None = None,
        writes: OwnerControlWritePort | None = None,
    ) -> None:
        self._auth, self._reads, self._writes = auth, reads, writes

    def _binding(self, action: OwnerControlAction) -> OwnerControlBinding:
        if self._auth is None:
            raise OwnerControlUnavailable("owner control authentication unavailable")
        try:
            binding = self._auth.bind()
        except (OwnerControlError, OwnerControlUnavailable, OwnerControlForbidden, OwnerControlStale):
            raise
        except Exception as error:
            raise OwnerControlUnavailable("owner control authentication unavailable") from error
        if type(binding) is not OwnerControlBinding:
            raise OwnerControlUnavailable("owner control binding unavailable")
        try:
            allowed = self._auth.authorize(binding, action.value)
        except (OwnerControlError, OwnerControlUnavailable, OwnerControlForbidden, OwnerControlStale):
            raise
        except Exception as error:
            raise OwnerControlUnavailable("owner control authorization unavailable") from error
        if allowed is not True:
            raise OwnerControlForbidden("owner control action forbidden")
        return binding

    def _reauthorize(self, binding: OwnerControlBinding, action: OwnerControlAction) -> None:
        auth = self._auth
        if auth is None:
            raise OwnerControlUnavailable("owner control authentication unavailable")
        try:
            allowed = auth.reauthorize(binding, action.value)
        except (OwnerControlError, OwnerControlUnavailable, OwnerControlForbidden, OwnerControlStale):
            raise
        except Exception as error:
            raise OwnerControlUnavailable("owner control reauthorization unavailable") from error
        if allowed is not True:
            raise OwnerControlForbidden("owner control action no longer authorized")

    def answers(self, *, needs_review: bool | None = None) -> OwnerControlAnswerList:
        if needs_review is not None and type(needs_review) is not bool:
            raise ValueError("strict needs_review filter required")
        binding = self._binding(OwnerControlAction.SUPERVISION_READ)
        reads = self._reads
        if reads is None:
            raise OwnerControlUnavailable("owner answer capability unavailable")
        try:
            values = reads.answers(binding, needs_review=needs_review)
            answer_values = _answer_list(values)
        except (OwnerControlError, OwnerControlUnavailable, OwnerControlForbidden, OwnerControlStale):
            raise
        except Exception as error:
            raise OwnerControlUnavailable("owner answer capability unavailable") from error
        self._reauthorize(binding, OwnerControlAction.SUPERVISION_READ)
        if needs_review is not None:
            answer_values = tuple(item for item in answer_values if item.needs_review is needs_review)
        for item in answer_values:
            _assert_projection_scope(
                binding,
                org_id=item.org_id,
                owner_user_id=item.owner_user_id,
                card_id=item.card_id,
                card_revision=item.card_revision,
                assignment_revision=item.assignment_revision,
                assignment_generation=item.assignment_generation,
                card_digest=item.card_digest,
            )
        return answer_values

    def presence(self) -> OwnerControlPresence:
        binding = self._binding(OwnerControlAction.SUPERVISION_READ)
        reads = self._reads
        if reads is None:
            raise OwnerControlUnavailable("owner presence capability unavailable")
        try:
            value = reads.presence(binding)
        except (OwnerControlError, OwnerControlUnavailable, OwnerControlForbidden, OwnerControlStale):
            raise
        except Exception as error:
            raise OwnerControlUnavailable("owner presence capability unavailable") from error
        if type(value) is not OwnerControlPresence:
            raise OwnerControlUnavailable("owner presence projection unavailable")
        _assert_projection_scope(
            binding,
            org_id=value.org_id,
            owner_user_id=value.owner_user_id,
            card_id=value.card_id,
            card_revision=value.card_revision,
            assignment_revision=value.assignment_revision,
            assignment_generation=value.assignment_generation,
            card_digest=value.card_digest,
        )
        if value.status in {"unknown", "unavailable"}:
            raise OwnerControlUnavailable("owner presence capability unavailable")
        self._reauthorize(binding, OwnerControlAction.SUPERVISION_READ)
        return value

    def corrections(self, *, record_id: str) -> OwnerControlCorrectionList:
        if _REFERENCE.fullmatch(record_id) is None:
            raise ValueError("bounded record reference required")
        binding = self._binding(OwnerControlAction.SUPERVISION_READ)
        reads = self._reads
        if reads is None:
            raise OwnerControlUnavailable("owner correction capability unavailable")
        try:
            values = reads.corrections(binding, record_id=record_id)
            correction_values = _correction_list(values)
        except (OwnerControlError, OwnerControlUnavailable, OwnerControlForbidden, OwnerControlStale):
            raise
        except Exception as error:
            raise OwnerControlUnavailable("owner correction capability unavailable") from error
        self._reauthorize(binding, OwnerControlAction.SUPERVISION_READ)
        if any(item.record_id != record_id for item in correction_values):
            raise OwnerControlForbidden("foreign correction record")
        for item in correction_values:
            _assert_projection_scope(
                binding,
                org_id=item.org_id,
                owner_user_id=item.owner_user_id,
                card_id=item.card_id,
                card_revision=item.card_revision,
                assignment_revision=item.assignment_revision,
                assignment_generation=item.assignment_generation,
                card_digest=item.card_digest,
            )
        return correction_values

    def scorecard(self) -> OwnerControlScorecard:
        binding = self._binding(OwnerControlAction.SCORECARD_READ)
        reads = self._reads
        if reads is None:
            raise OwnerControlUnavailable("owner scorecard capability unavailable")
        try:
            value = reads.scorecard(binding)
        except (OwnerControlError, OwnerControlUnavailable, OwnerControlForbidden, OwnerControlStale):
            raise
        except Exception as error:
            raise OwnerControlUnavailable("owner scorecard capability unavailable") from error
        if type(value) is not OwnerControlScorecard:
            raise OwnerControlUnavailable("owner scorecard projection unavailable")
        _assert_projection_scope(
            binding,
            org_id=value.org_id,
            owner_user_id=value.owner_user_id,
            card_id=value.card_id,
            card_revision=value.card_revision,
            assignment_revision=value.assignment_revision,
            assignment_generation=value.assignment_generation,
            card_digest=value.card_digest,
        )
        self._reauthorize(binding, OwnerControlAction.SCORECARD_READ)
        return value

    def correct(self, command: OwnerControlCorrectionCommand) -> OwnerControlCorrection:
        binding = self._binding(OwnerControlAction.SUPERVISION_CORRECT)
        writes = self._writes
        if writes is None:
            raise OwnerControlUnavailable("owner correction write capability unavailable")
        if type(command) is not OwnerControlCorrectionCommand:
            raise ValueError("strict owner correction command required")
        try:
            # This is the pre-commit authorization seam.  A concrete writer
            # must perform its own transaction CAS after this check.
            self._reauthorize(binding, OwnerControlAction.SUPERVISION_CORRECT)
            value = writes.correct(binding, command)
        except (OwnerControlError, OwnerControlUnavailable, OwnerControlForbidden, OwnerControlStale):
            raise
        except Exception as error:
            raise OwnerControlUnavailable("owner correction write capability unavailable") from error
        if type(value) is not OwnerControlCorrection:
            raise OwnerControlUnavailable("owner correction receipt unavailable")
        _assert_projection_scope(
            binding,
            org_id=value.org_id,
            owner_user_id=value.owner_user_id,
            card_id=value.card_id,
            card_revision=value.card_revision,
            assignment_revision=value.assignment_revision,
            assignment_generation=value.assignment_generation,
            card_digest=value.card_digest,
        )
        if (
            value.review_id != command.review_id
            or value.idempotency_key != command.idempotency_key
            or value.expected_revision != command.expected_revision
        ):
            raise OwnerControlStale("correction receipt does not match command")
        return value


def _answer_list(values: Sequence[OwnerControlAnswer]) -> OwnerControlAnswerList:
    result = tuple(values)
    if any(type(item) is not OwnerControlAnswer for item in result):
        raise OwnerControlUnavailable("owner answer projection unavailable")
    return result


def _correction_list(values: Sequence[OwnerControlCorrection]) -> OwnerControlCorrectionList:
    result = tuple(values)
    if any(type(item) is not OwnerControlCorrection for item in result):
        raise OwnerControlUnavailable("owner correction projection unavailable")
    return result


# Concise aliases used by callers that refer to these projections without the
# ``Control`` prefix.  They do not create a second schema.
Answer = OwnerControlAnswer
Presence = OwnerControlPresence
Correction = OwnerControlCorrection
Scorecard = OwnerControlScorecard
CorrectionCommand = OwnerControlCorrectionCommand


__all__ = [
    "Answer",
    "Correction",
    "CorrectionCommand",
    "OwnerControlAction",
    "OwnerControlAnswer",
    "OwnerControlAnswerList",
    "OwnerControlApplication",
    "OwnerControlBinding",
    "OwnerControlCorrection",
    "OwnerControlCorrectionCommand",
    "OwnerControlCorrectionList",
    "OwnerControlError",
    "OwnerControlForbidden",
    "OwnerControlAuthPort",
    "OwnerControlPresence",
    "OwnerControlReadPort",
    "OwnerControlScorecard",
    "OwnerControlStale",
    "OwnerControlUnavailable",
    "OwnerControlWritePort",
    "Presence",
    "Scorecard",
]
