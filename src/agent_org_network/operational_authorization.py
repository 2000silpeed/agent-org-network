"""운영·관리·저작 application 경계용 중앙 권한 확인 계약(P17.8 S4.1).

이 모듈은 HTTP나 MCP principal resolver를 소유하지 않는다. 이미 인증된
``AuthenticatedPrincipal``과 현재 리소스를 받아, configured one-org 및 중앙
Authority의 sealed grant를 한 번의 application 경계에서 대조한다. 표면 adapter는
후속 슬라이스에서 이 결과를 자기 오류 표현으로만 바꾼다.
"""

from __future__ import annotations

from typing import Literal, TypeAlias, cast, final

from agent_org_network.central_authority import (
    Action,
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorizationGrant,
    CentralAuthorizer,
    ResourceRef,
)

OperationalAuthorizationOutcome: TypeAlias = Literal["allowed", "denied", "unavailable"]
OperationalAction: TypeAlias = Literal[
    "supervision.read",
    "supervision.correct",
    "scorecard.read",
    "monitor.read",
    "audit.read",
    "org_graph.read",
    "session.end",
    "hitl.read",
    "hitl.write",
    "worker_credential.issue",
    "worker_credential.read",
    "worker_credential.revoke",
    "card.read",
    "card.register",
    "card.transfer_owner",
    "user.register",
    "author.read",
    "author.write",
    "author.publish",
]

# 이 경계가 책임지는 S4 application command/query의 닫힌 부분집합이다. 각 값은
# S1의 전역 manifest에도 있어야 하며, 아래 authorize가 질문·승인 경로에 재사용되지
# 않도록 별도 상수로 남긴다.
OPERATIONAL_ACTION_MANIFEST: frozenset[OperationalAction] = frozenset(
    {
        "supervision.read",
        "supervision.correct",
        "scorecard.read",
        "monitor.read",
        "audit.read",
        "org_graph.read",
        "session.end",
        "hitl.read",
        "hitl.write",
        "worker_credential.issue",
        "worker_credential.read",
        "worker_credential.revoke",
        "card.read",
        "card.register",
        "card.transfer_owner",
        "user.register",
        "author.read",
        "author.write",
        "author.publish",
    }
)


def _canonical_principal(value: object) -> AuthenticatedPrincipal | None:
    """상속·duck principal을 경계 밖으로 밀어낸다."""
    if type(value) is not AuthenticatedPrincipal:
        return None
    try:
        return AuthenticatedPrincipal.model_validate(value, strict=True)
    except Exception:
        return None


def _canonical_resource(value: object) -> ResourceRef | None:
    """상속·duck resource를 경계 밖으로 밀어낸다."""
    if type(value) is not ResourceRef:
        return None
    try:
        return ResourceRef.model_validate(value, strict=True)
    except Exception:
        return None


@final
class OperationalAuthorization:
    """S4 action을 중앙 Authority의 exact sealed grant로 확인한다.

    ``central_authorizer``가 없는 조립은 production-ready 권한 구성이 아니므로
    ``unavailable``로 닫는다. 이는 어떤 도메인 write도 수행하지 않는 순수 경계다.
    """

    def __init__(
        self,
        *,
        configured_org_id: str,
        central_authorizer: CentralAuthorizer | None,
    ) -> None:
        self._configured_org_id = (
            configured_org_id
            if type(configured_org_id) is str and bool(configured_org_id.strip())
            else None
        )
        self._central_authorizer = central_authorizer

    def authorize(
        self,
        principal: object,
        action: object,
        resource: object,
    ) -> OperationalAuthorizationOutcome:
        """권한 결과만 반환하고 입력·예외·grant 세부를 밖으로 내보내지 않는다."""
        canonical_principal = _canonical_principal(principal)
        canonical_resource = _canonical_resource(resource)
        if (
            canonical_principal is None
            or canonical_resource is None
            or type(action) is not str
            or action not in OPERATIONAL_ACTION_MANIFEST
        ):
            return "denied"
        configured_org_id = self._configured_org_id
        if (
            configured_org_id is None
            or canonical_principal.org_id != configured_org_id
            or canonical_resource.org_id != configured_org_id
        ):
            return "denied"
        authorizer = self._central_authorizer
        if authorizer is None:
            return "unavailable"
        canonical_action = cast(Action, action)
        try:
            raw = authorizer.authorize(canonical_principal, canonical_action, canonical_resource)
        except Exception:
            return "unavailable"
        if type(raw) is AuthorizationDenied:
            try:
                denied = AuthorizationDenied.model_validate(raw, strict=True)
            except Exception:
                return "denied"
            return "unavailable" if denied.kind == "policy_unavailable" else "denied"
        if type(raw) is not AuthorizationGrant:
            return "denied"
        try:
            grant = AuthorizationGrant.model_validate(raw, strict=True)
            if not (
                grant.org_id == canonical_principal.org_id == canonical_resource.org_id
                and grant.subject_id == canonical_principal.subject_id
                and grant.action == canonical_action
                and grant.resource == canonical_resource
                and bool(grant.roles)
            ):
                return "denied"
        except Exception:
            return "denied"
        try:
            verifier = authorizer.verify
            if not callable(verifier):
                return "unavailable"
            verified = verifier(raw, canonical_principal, canonical_action, canonical_resource)
        except Exception:
            return "unavailable"
        if type(verified) is not bool:
            return "unavailable"
        return "allowed" if verified else "denied"
