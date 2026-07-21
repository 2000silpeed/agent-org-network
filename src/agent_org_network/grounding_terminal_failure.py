"""Grounding 판독 결과를 Question Request의 terminal 실패로 기록한다.

레코더의 반환값은 CAS 호출 응답이 아니라 반드시 뒤이은 Request Store 재조회
증거로 결정한다. 이 경계 덕분에 응답 유실과 동시 terminal 전이를 구분하면서도
Question Request를 단일 진실 원천으로 유지한다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol, TypeAlias, final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_org_network.question_request import (
    FailedRequest,
    QuestionRequest,
    QuestionRequestStore,
    ReadyToDispatch,
)

Clock: TypeAlias = Callable[[], datetime]
GroundingTerminalFailureCode: TypeAlias = Literal[
    "required_grounding_missing",
    "required_grounding_invalid",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )


@final
class GroundingTerminalFailureRequested(_FrozenModel):
    """Answer Source가 요청하는 Request-scoped terminal 실패 명령."""

    kind: Literal["grounding_terminal_failure_requested"] = "grounding_terminal_failure_requested"
    request_id: str
    expected_revision: int = Field(ge=0)
    error_code: GroundingTerminalFailureCode

    @field_validator("request_id", mode="after")
    @classmethod
    def _request_id_must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request_id는 nonblank여야 합니다.")
        return value


class GroundingTerminalFailureError(RuntimeError):
    """Grounding terminal 기록 경계의 닫힌 오류 기반형."""

    code: str
    retryable: bool


class GroundingTerminalFailureIntegrity(GroundingTerminalFailureError):
    code = "grounding_terminal_failure_integrity"
    retryable = False

    def __init__(self) -> None:
        super().__init__("Grounding terminal 실패의 저장 증거가 일치하지 않습니다.")


class GroundingTerminalFailureConflict(GroundingTerminalFailureError):
    code = "grounding_terminal_failure_conflict"
    retryable = False

    def __init__(self) -> None:
        super().__init__("Question Request가 더 이상 요청한 Ready revision이 아닙니다.")


class GroundingTerminalFailureDependency(GroundingTerminalFailureError):
    code = "grounding_terminal_failure_dependency"
    retryable = True

    def __init__(self) -> None:
        super().__init__("Grounding terminal 실패 기록 의존성을 확인할 수 없습니다.")


class GroundingTerminalFailureRecorder(Protocol):
    """Ready Question Request를 grounding terminal 실패로 닫는 포트."""

    def fail_if_ready(
        self,
        *,
        request_id: str,
        expected_revision: int,
        error_code: GroundingTerminalFailureCode,
    ) -> QuestionRequest: ...


@final
class QuestionRequestGroundingTerminalFailureRecorder:
    """QuestionRequestStore의 read/CAS/read 증거를 따르는 레코더."""

    def __init__(self, requests: QuestionRequestStore, clock: Clock) -> None:
        self._requests = requests
        self._clock = clock

    def matches_request_store(self, requests: QuestionRequestStore) -> bool:
        """조립 경계가 동일 Store 인스턴스 사용 여부를 검증할 수 있게 한다."""
        return self._requests is requests

    def fail_if_ready(
        self,
        *,
        request_id: str,
        expected_revision: int,
        error_code: GroundingTerminalFailureCode,
    ) -> QuestionRequest:
        command = self._canonical_command(
            request_id=request_id,
            expected_revision=expected_revision,
            error_code=error_code,
        )
        current = self._read(command.request_id)
        if current.is_terminal:
            return current
        if (
            type(current.state) is not ReadyToDispatch
            or current.revision != command.expected_revision
        ):
            raise GroundingTerminalFailureConflict()

        try:
            target = current.transition(
                FailedRequest(error_code=command.error_code),
                clock=self._clock,
            )
        except Exception as error:
            raise GroundingTerminalFailureDependency() from error

        try:
            changed = self._requests.compare_and_set(
                command.request_id,
                command.expected_revision,
                current,
                target,
            )
        except Exception as error:
            latest = self._read(command.request_id)
            if latest == target:
                return target
            if latest.is_terminal:
                return latest
            if latest == current:
                raise GroundingTerminalFailureDependency() from error
            raise GroundingTerminalFailureConflict() from error

        latest = self._read(command.request_id)
        if type(changed) is not bool:
            raise GroundingTerminalFailureIntegrity()
        if changed:
            if latest == target:
                return target
            raise GroundingTerminalFailureIntegrity()
        if latest.is_terminal:
            return latest
        raise GroundingTerminalFailureConflict()

    @staticmethod
    def _canonical_command(
        *,
        request_id: str,
        expected_revision: int,
        error_code: GroundingTerminalFailureCode,
    ) -> GroundingTerminalFailureRequested:
        try:
            return GroundingTerminalFailureRequested.model_validate(
                {
                    "request_id": request_id,
                    "expected_revision": expected_revision,
                    "error_code": error_code,
                },
                strict=True,
            )
        except Exception as error:
            raise GroundingTerminalFailureIntegrity() from error

    def _read(self, request_id: str) -> QuestionRequest:
        try:
            raw = self._requests.get(request_id)
        except Exception as error:
            raise GroundingTerminalFailureDependency() from error
        if type(raw) is not QuestionRequest or raw.request_id != request_id:
            raise GroundingTerminalFailureIntegrity()
        try:
            return QuestionRequest.model_validate(
                raw.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        except Exception as error:
            raise GroundingTerminalFailureIntegrity() from error
