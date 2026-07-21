"""워커 연결·전달을 중앙 Authority로 결박하는 P17.8 S5 경계.

이 모듈은 legacy ``TokenStore``를 production credential source로 승격하지 않는다.
opaque credential의 검증은 바깥 adapter가 맡고, 여기에는 검증 뒤의 식별자와 generation만
들어온다. 따라서 raw credential은 connection principal이나 delivery binding에 남지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Protocol, TypeAlias, cast, final

from pydantic import BaseModel, ConfigDict, field_validator

from agent_org_network.central_authority import (
    Action,
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    AuthorizationDenied,
    AuthorizationGrant,
    CentralAuthorizer,
    ResourceRef,
    WorkerBinding,
)

WorkerAction: TypeAlias = Literal[
    "worker.connect",
    "worker.submit",
    "worker.publish_index",
    "worker.sync_knowledge",
]
WorkerAuthorizationOutcome: TypeAlias = Literal["allowed", "denied", "unavailable"]
WorkerConnectionRole: TypeAlias = Literal["primary", "backup"]
WorkerPolicySnapshotProvider: TypeAlias = Callable[[], AuthorityPolicySnapshot]

WORKER_ACTION_MANIFEST: frozenset[WorkerAction] = frozenset(
    {"worker.connect", "worker.submit", "worker.publish_index", "worker.sync_knowledge"}
)


class _FrozenWorkerModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, revalidate_instances="always"
    )

    @field_validator("*", mode="after")
    @classmethod
    def _nonblank_strings(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("문자열 값은 비어 있을 수 없습니다.")
        return value


@final
class WorkerConnectionPrincipal(_FrozenWorkerModel):
    """검증된 한 WS 연결의 identity. raw credential은 절대 포함하지 않는다."""

    org_id: str
    owner_id: str
    credential_id: str
    credential_generation: int
    role: WorkerConnectionRole
    connection_epoch: str

    @field_validator("credential_generation", mode="after")
    @classmethod
    def _positive_generation(cls, value: int) -> int:
        if value < 1:
            raise ValueError("credential_generation은 1 이상이어야 합니다.")
        return value


@final
class DeliveryBinding(_FrozenWorkerModel):
    """push 당시의 정확한 세션·카드·attempt 기록. submit 전 재검증 대상이다."""

    ticket_id: str
    agent_card_id: str
    owner_id: str
    connection: WorkerConnectionPrincipal
    attempt: int

    @field_validator("attempt", mode="after")
    @classmethod
    def _positive_attempt(cls, value: int) -> int:
        if value < 1:
            raise ValueError("attempt는 1 이상이어야 합니다.")
        return value


class WorkerBindingSource(Protocol):
    def binding_for(self, principal: WorkerConnectionPrincipal) -> WorkerBinding | None: ...


@final
class StrictSnapshotWorkerBindingSource:
    """현재 strict 정책 snapshot에서 exact worker binding만 찾는 adapter.

    callable provider도 지원하지만 예외·잘못된 snapshot은 ``None``으로 수렴시킨다. 이는
    policy 장애와 권한 거부를 바깥 ``WorkerAuthorization``이 구분할 수 있게 한다.
    """

    def __init__(self, snapshot: AuthorityPolicySnapshot | WorkerPolicySnapshotProvider) -> None:
        self._source = snapshot
        self._unavailable = False

    def binding_for(self, principal: WorkerConnectionPrincipal) -> WorkerBinding | None:
        snapshot = self.snapshot()
        if snapshot is None:
            self._unavailable = True
            return None
        return next(
            (
                binding
                for binding in snapshot.worker_bindings
                if binding.org_id == principal.org_id
                and binding.credential_id == principal.credential_id
            ),
            None,
        )

    def snapshot(self) -> AuthorityPolicySnapshot | None:
        try:
            value = (
                self._source if type(self._source) is AuthorityPolicySnapshot else self._source()
            )
            if type(value) is not AuthorityPolicySnapshot:
                return None
            return AuthorityPolicySnapshot.model_validate(value, strict=True)
        except Exception:
            return None

    @property
    def unavailable(self) -> bool:
        return self._unavailable


def _principal(value: object) -> WorkerConnectionPrincipal | None:
    if type(value) is not WorkerConnectionPrincipal:
        return None
    try:
        return WorkerConnectionPrincipal.model_validate(value, strict=True)
    except Exception:
        return None


def _binding(value: object) -> DeliveryBinding | None:
    if type(value) is not DeliveryBinding:
        return None
    try:
        return DeliveryBinding.model_validate(value, strict=True)
    except Exception:
        return None


@final
class WorkerAuthorization:
    """S5 worker action을 binding·central grant 모두로 확인한다.

    Authority/provider 한쪽만 빠진 조립은 fail-open하지 않고 ``unavailable``이다.
    """

    def __init__(
        self,
        *,
        configured_org_id: str,
        central_authorizer: CentralAuthorizer | None,
        binding_source: WorkerBindingSource | None,
    ) -> None:
        self._configured_org_id = (
            configured_org_id
            if type(configured_org_id) is str and configured_org_id.strip()
            else None
        )
        self._central_authorizer = central_authorizer
        self._binding_source = binding_source

    def authorize_connection(self, principal: object) -> WorkerAuthorizationOutcome:
        canonical = _principal(principal)
        if canonical is None or canonical.org_id != self._configured_org_id:
            return "denied"
        return self._authorize(canonical, "worker.connect", self._connection_resource(canonical))

    def authorize_delivery(
        self,
        principal: object,
        action: object,
        *,
        agent_card_id: object,
        current_owner_id: object,
    ) -> WorkerAuthorizationOutcome:
        canonical = _principal(principal)
        if (
            canonical is None
            or canonical.org_id != self._configured_org_id
            or type(action) is not str
            or action not in WORKER_ACTION_MANIFEST - {"worker.connect"}
            or type(agent_card_id) is not str
            or not agent_card_id.strip()
            or type(current_owner_id) is not str
            or not current_owner_id.strip()
            or current_owner_id != canonical.owner_id
        ):
            return "denied"
        return self._authorize(
            canonical,
            action,
            ResourceRef(
                org_id=canonical.org_id,
                kind="agent_card",
                resource_id=agent_card_id,
                owner_subject_id=current_owner_id,
            ),
        )

    def verify_delivery_binding(
        self,
        binding: object,
        principal: object,
        *,
        ticket_id: object,
        agent_card_id: object,
        current_owner_id: object,
    ) -> bool:
        canonical_binding = _binding(binding)
        canonical_principal = _principal(principal)
        return bool(
            canonical_binding is not None
            and canonical_principal is not None
            and canonical_binding.connection == canonical_principal
            and canonical_binding.ticket_id == ticket_id
            and canonical_binding.agent_card_id == agent_card_id
            and canonical_binding.owner_id == current_owner_id == canonical_principal.owner_id
        )

    @staticmethod
    def _connection_resource(principal: WorkerConnectionPrincipal) -> ResourceRef:
        return ResourceRef(
            org_id=principal.org_id,
            kind="worker_credential",
            resource_id=principal.credential_id,
            owner_subject_id=principal.owner_id,
        )

    def _authorize(
        self,
        principal: WorkerConnectionPrincipal,
        action: WorkerAction,
        resource: ResourceRef,
    ) -> WorkerAuthorizationOutcome:
        authorizer = self._central_authorizer
        source = self._binding_source
        if authorizer is None or source is None or self._configured_org_id is None:
            return "unavailable"
        try:
            expected = source.binding_for(principal)
        except Exception:
            return "unavailable"
        if expected is None:
            return "unavailable" if getattr(source, "unavailable", False) else "denied"
        if not (
            expected.org_id == principal.org_id
            and expected.credential_id == principal.credential_id
            and expected.owner_subject_id == principal.owner_id
            and expected.connection_role == principal.role
            and expected.generation == principal.credential_generation
        ):
            return "denied"
        human_principal = AuthenticatedPrincipal(
            org_id=principal.org_id,
            subject_id=principal.owner_id,
            identity_provider="worker_binding",
            identity_session_id=principal.connection_epoch,
        )
        try:
            result = authorizer.authorize(human_principal, cast(Action, action), resource)
        except Exception:
            return "unavailable"
        if type(result) is AuthorizationDenied:
            return "unavailable" if result.kind == "policy_unavailable" else "denied"
        if type(result) is not AuthorizationGrant:
            return "denied"
        try:
            if not authorizer.verify(result, human_principal, cast(Action, action), resource):
                return "denied"
        except Exception:
            return "unavailable"
        return "allowed"
