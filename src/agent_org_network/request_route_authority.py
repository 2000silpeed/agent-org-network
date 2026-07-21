"""Request-scoped Route Authority grant 계약(ADR 0046).

Owner consensus와 두 Manager 처분 출처를 하나의 Request first-winner slot에 기록한다.
전역 Route Authority 정책은 바꾸지 않는다.
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, TypeAlias, final

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from agent_org_network.question_resolution import AuthorityGrant, RouteAuthority


class _FrozenDto(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    @field_validator("*", mode="after")
    @classmethod
    def _strings_must_be_nonblank(cls, value: object, info: ValidationInfo) -> object:
        del info
        if isinstance(value, str) and not value.strip():
            raise ValueError("문자열 값은 비어 있거나 공백일 수 없습니다.")
        return value


@final
class FromUnownedManagerGrant(_FrozenDto):
    kind: Literal["unowned_manager"] = "unowned_manager"
    item_id: str
    by_manager: str


@final
class FromOwnerConsensusGrant(_FrozenDto):
    kind: Literal["owner_consensus"] = "owner_consensus"
    case_id: str
    round: int = Field(ge=1)


@final
class FromDeadlockManagerGrant(_FrozenDto):
    kind: Literal["deadlock_manager"] = "deadlock_manager"
    case_id: str
    item_id: str
    by_manager: str


RequestRouteGrantSource: TypeAlias = Annotated[
    FromUnownedManagerGrant | FromOwnerConsensusGrant | FromDeadlockManagerGrant,
    Field(discriminator="kind"),
]


@final
class RequestRouteGrantAssignment(_FrozenDto):
    org_id: str
    request_id: str
    intent: str
    agent_id: str
    source: RequestRouteGrantSource
    idempotency_key: str


@final
class RequestRouteGrantReceipt(_FrozenDto):
    kind: Literal["receipt"] = "receipt"
    assignment: RequestRouteGrantAssignment
    grant_version: str


@final
class RequestRouteGrantRejected(_FrozenDto):
    kind: Literal["rejected"] = "rejected"
    idempotency_key: str
    authority_write_applied: Literal[False] = False
    idempotency_write_applied: Literal[False] = False
    reason_code: str


@final
class RequestRouteGrantConflict(_FrozenDto):
    kind: Literal["conflict"] = "conflict"


RequestRouteGrantResult: TypeAlias = Annotated[
    RequestRouteGrantReceipt | RequestRouteGrantRejected | RequestRouteGrantConflict,
    Field(discriminator="kind"),
]


class RequestRouteAuthority(RouteAuthority, Protocol):
    def grant_for_request(
        self,
        assignment: RequestRouteGrantAssignment,
    ) -> RequestRouteGrantResult: ...

    def authorize_for_request(
        self,
        org_id: str,
        request_id: str,
        intent: str,
        agent_id: str,
    ) -> AuthorityGrant | None: ...
