"""Central Question Intake application service for the RB3.1a first slice."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, final

from pydantic import BaseModel, ConfigDict, field_validator

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorizationGrant,
    CentralAuthorizer,
    ResourceRef,
)
from agent_org_network.question_request import (
    Clock,
    DuplicateQuestionRequestError,
    QuestionRequest,
    QuestionRequestStore,
    RequestIdFactory,
)


class ReceivedDeadlinePolicy(Protocol):
    def deadline_for(
        self, org_id: str, state_kind: str, started_at: datetime
    ) -> datetime: ...


@final
class CentralQuestionIntakeInvalid(Exception):
    """The caller supplied no valid question, principal, or request ID."""


@final
class CentralQuestionIntakeForbidden(Exception):
    """A valid principal does not currently have create permission."""


@final
class CentralQuestionIntakeNotFound(Exception):
    """A read is missing, foreign, revoked, or denied without distinction."""


@final
class CentralQuestionIntakeConflict(Exception):
    """The generated request ID already exists."""


@final
class CentralQuestionIntakeUnavailable(Exception):
    """A clock, SLA, Authority, or durable store dependency is unavailable."""


class ReceivedQuestionProjection(BaseModel, frozen=True):
    """Body-free read model for the existing durable Received state."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str
    state: Literal["received"] = "received"
    created_at: datetime

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("nonblank request ID required")
        return value

    @field_validator("created_at")
    @classmethod
    def _created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone-aware created_at required")
        return value


class CentralQuestionIntakeApplication:
    """Authorize, durably receive, and read only the caller's Question Request."""

    def __init__(
        self,
        *,
        requests: QuestionRequestStore,
        central_authorizer: CentralAuthorizer,
        deadline_policy: ReceivedDeadlinePolicy,
        request_id_factory: RequestIdFactory,
        clock: Clock,
    ) -> None:
        self._requests = requests
        self._central_authorizer = central_authorizer
        self._deadline_policy = deadline_policy
        self._request_id_factory = request_id_factory
        self._clock = clock

    def create(
        self,
        question: str,
        principal: AuthenticatedPrincipal,
    ) -> ReceivedQuestionProjection:
        canonical_principal = _require_principal(principal)
        if type(question) is not str or not question.strip():
            raise CentralQuestionIntakeInvalid
        create_resource = ResourceRef(
            org_id=canonical_principal.org_id,
            kind="question",
        )
        self._require_authorized(
            canonical_principal,
            "question.create",
            create_resource,
            denied=CentralQuestionIntakeForbidden,
        )
        try:
            request_id = self._request_id_factory()
            started_at = self._clock()
            if (
                type(request_id) is not str
                or not request_id.strip()
                or type(started_at) is not datetime
                or started_at.tzinfo is None
                or started_at.utcoffset() is None
            ):
                raise CentralQuestionIntakeUnavailable
            due_at = self._deadline_policy.deadline_for(
                canonical_principal.org_id,
                "received",
                started_at,
            )
            if (
                type(due_at) is not datetime
                or due_at.tzinfo is None
                or due_at.utcoffset() is None
                or due_at < started_at
            ):
                raise CentralQuestionIntakeUnavailable
            received = QuestionRequest.receive(
                org_id=canonical_principal.org_id,
                requester_id=canonical_principal.subject_id,
                question=question,
                request_id_factory=lambda: request_id,
                clock=lambda: started_at,
                due_at=due_at,
            )
        except CentralQuestionIntakeUnavailable:
            raise
        except Exception:
            raise CentralQuestionIntakeUnavailable from None
        try:
            stored = self._requests.create(received)
        except DuplicateQuestionRequestError:
            raise CentralQuestionIntakeConflict from None
        except Exception:
            raise CentralQuestionIntakeUnavailable from None
        if type(stored) is not QuestionRequest or stored != received:
            raise CentralQuestionIntakeUnavailable
        return _project_received(stored)

    def read_own(
        self,
        request_id: str,
        principal: AuthenticatedPrincipal,
    ) -> ReceivedQuestionProjection:
        canonical_principal = _require_principal(principal)
        if (
            type(request_id) is not str
            or not request_id
            or request_id != request_id.strip()
        ):
            raise CentralQuestionIntakeInvalid
        try:
            request = self._requests.get(request_id)
        except Exception:
            raise CentralQuestionIntakeUnavailable from None
        if request is None:
            raise CentralQuestionIntakeNotFound
        if type(request) is not QuestionRequest:
            raise CentralQuestionIntakeUnavailable
        resource = ResourceRef(
            org_id=request.org_id,
            kind="question",
            resource_id=request.request_id,
            owner_subject_id=request.requester_id,
        )
        self._require_authorized(
            canonical_principal,
            "question.read",
            resource,
            denied=CentralQuestionIntakeNotFound,
        )
        if (
            request.org_id != canonical_principal.org_id
            or request.requester_id != canonical_principal.subject_id
        ):
            raise CentralQuestionIntakeNotFound
        return _project_received(request)

    def _require_authorized(
        self,
        principal: AuthenticatedPrincipal,
        action: Literal["question.create", "question.read"],
        resource: ResourceRef,
        *,
        denied: type[CentralQuestionIntakeForbidden]
        | type[CentralQuestionIntakeNotFound],
    ) -> None:
        try:
            result = self._central_authorizer.authorize(principal, action, resource)
        except Exception:
            raise CentralQuestionIntakeUnavailable from None
        if type(result) is AuthorizationDenied:
            if result.kind == "policy_unavailable":
                raise CentralQuestionIntakeUnavailable
            raise denied
        if type(result) is not AuthorizationGrant:
            raise CentralQuestionIntakeUnavailable
        try:
            canonical = AuthorizationGrant.model_validate(result)
            matches = bool(
                canonical.org_id == principal.org_id == resource.org_id
                and canonical.subject_id == principal.subject_id
                and canonical.action == action
                and canonical.resource == resource
                and canonical.roles
            )
        except Exception:
            raise CentralQuestionIntakeUnavailable from None
        if not matches:
            raise denied
        try:
            verified = self._central_authorizer.verify(
                result,
                principal,
                action,
                resource,
            )
        except Exception:
            raise CentralQuestionIntakeUnavailable from None
        if type(verified) is not bool:
            raise CentralQuestionIntakeUnavailable
        if not verified:
            raise denied


def _require_principal(value: object) -> AuthenticatedPrincipal:
    if type(value) is not AuthenticatedPrincipal:
        raise CentralQuestionIntakeInvalid
    try:
        return AuthenticatedPrincipal.model_validate(value)
    except Exception:
        raise CentralQuestionIntakeInvalid from None


def _project_received(request: QuestionRequest) -> ReceivedQuestionProjection:
    if request.state.kind != "received" or request.revision != 0:
        raise CentralQuestionIntakeUnavailable
    return ReceivedQuestionProjection(
        request_id=request.request_id,
        state="received",
        created_at=request.created_at,
    )


__all__ = [
    "CentralQuestionIntakeApplication",
    "CentralQuestionIntakeConflict",
    "CentralQuestionIntakeForbidden",
    "CentralQuestionIntakeInvalid",
    "CentralQuestionIntakeNotFound",
    "CentralQuestionIntakeUnavailable",
    "ReceivedQuestionProjection",
]
