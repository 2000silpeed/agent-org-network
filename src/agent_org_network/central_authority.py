"""중앙 Authority 정책 스냅샷과 deny-safe RBAC 코어(P17.8 S1·ADR 0050).

이 모듈은 시작 시 한 번 확정한 단일 조직 정책만 해석한다. HTTP·MCP·워커 연결과
Question Surface 조립은 후속 슬라이스가 맡으며, 여기서는 인증된 주체와 리소스를 중앙
permission 및 현재 소유자에 대조하는 순수 계약만 제공한다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Literal, Protocol, TypeAlias, cast, final

from pydantic import BaseModel, ConfigDict, PrivateAttr, field_validator, model_validator
import yaml

Role: TypeAlias = Literal[
    "requester",
    "owner",
    "manager",
    "approver",
    "operator",
    "auditor",
    "admin",
]

Action: TypeAlias = Literal[
    "question.create",
    "question.read",
    "question.stream",
    "approval.list",
    "approval.read",
    "approval.decide",
    "approval.reassign",
    "approval.expire",
    "conflict.open",
    "conflict.escalate",
    "conflict.list",
    "conflict.concur",
    "conflict.document.read",
    "manager.list",
    "manager.act",
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
    "worker.connect",
    "worker.submit",
    "worker.publish_index",
    "worker.sync_knowledge",
]

AUTHORITY_ROLES: frozenset[str] = frozenset(
    {"requester", "owner", "manager", "approver", "operator", "auditor", "admin"}
)
AUTHORITY_ACTION_MANIFEST: frozenset[str] = frozenset(
    {
        "question.create",
        "question.read",
        "question.stream",
        "approval.list",
        "approval.read",
        "approval.decide",
        "approval.reassign",
        "approval.expire",
        "conflict.open",
        "conflict.escalate",
        "conflict.list",
        "conflict.concur",
        "conflict.document.read",
        "manager.list",
        "manager.act",
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
        "worker.connect",
        "worker.submit",
        "worker.publish_index",
        "worker.sync_knowledge",
    }
)

# 현재 주체 귀속이 필요한 action-role 조합. 같은 action을 별도 운영 역할이 가진 경우에는
# 그 역할의 중앙 permission으로만 판단할 수 있도록 action 하나를 전역 owner-only로 만들지 않는다.
DYNAMIC_SUBJECT_REQUIREMENTS: Mapping[Action, frozenset[Role]] = MappingProxyType(
    {
        "question.read": frozenset({"requester"}),
        "question.stream": frozenset({"requester"}),
        "approval.list": frozenset({"owner", "approver"}),
        "approval.read": frozenset({"owner", "approver"}),
        "approval.decide": frozenset({"owner", "approver"}),
        "approval.reassign": frozenset({"owner", "approver"}),
        "conflict.open": frozenset({"requester"}),
        "conflict.list": frozenset({"owner"}),
        "conflict.concur": frozenset({"owner"}),
        "conflict.document.read": frozenset({"owner"}),
        "manager.list": frozenset({"manager"}),
        "manager.act": frozenset({"manager"}),
        "supervision.read": frozenset({"owner"}),
        "supervision.correct": frozenset({"owner"}),
        "scorecard.read": frozenset({"owner"}),
        "card.read": frozenset({"owner"}),
        "author.read": frozenset({"owner"}),
        "author.write": frozenset({"owner"}),
        "author.publish": frozenset({"owner"}),
    }
)

# RBAC와 별도로 action은 자신이 허용하는 ResourceRef 종류를 가진다. 아직
# 존재하지 않는 ConflictCase가 아니라, 이미 접수된 Question Request만
# conflict.open의 대상이다. conflict.escalate는 반대로 이미 durable하게
# 존재하는 open conflict_case만 대상이다(ADR 0050 §11).
ACTION_RESOURCE_KIND_REQUIREMENTS: Mapping[Action, str] = MappingProxyType(
    {"conflict.open": "question_request", "conflict.escalate": "conflict_case"}
)
ACTION_ALLOWED_ROLES: Mapping[Action, frozenset[Role]] = MappingProxyType(
    {"conflict.open": frozenset({"requester"}), "conflict.escalate": frozenset({"operator"})}
)

PolicyFailureKind: TypeAlias = Literal[
    "missing_policy",
    "invalid_yaml",
    "invalid_document",
    "unknown_key",
    "unknown_role",
    "unknown_action",
    "duplicate",
    "blank_value",
    "cross_org",
    "schema_version",
    "policy_version",
    "digest_mismatch",
]


class AuthorityPolicyLoadError(RuntimeError):
    """정책 원문이나 내부 예외를 노출하지 않는 시작 실패."""

    kind: PolicyFailureKind

    def __init__(self, kind: PolicyFailureKind) -> None:
        self.kind = kind
        super().__init__(f"authority policy rejected: {kind}")


class _FrozenAuthorityModel(BaseModel):
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


@final
class AuthenticatedPrincipal(_FrozenAuthorityModel):
    org_id: str
    subject_id: str
    identity_provider: str
    identity_session_id: str


@final
class ResourceRef(_FrozenAuthorityModel):
    org_id: str
    kind: str
    resource_id: str | None = None
    owner_subject_id: str | None = None


@final
class ConflictOpenRequestSnapshot(_FrozenAuthorityModel):
    """Read-only proof that an existing Question Request remains openable."""

    org_id: str
    request_id: str
    requester_subject_id: str
    state_kind: Literal["received"]
    revision: Literal[0]


class ConflictOpenRequestResolver(Protocol):
    """ResourceRef is untrusted input; this server-side resolver is the proof."""

    def resolve_conflict_open_request(
        self, *, request_id: str
    ) -> ConflictOpenRequestSnapshot | None: ...


@final
class ConflictEscalateCaseSnapshot(_FrozenAuthorityModel):
    """Read-only proof that a durable open Conflict Case remains escalatable."""

    org_id: str
    conflict_id: str
    state_kind: Literal["open"]
    awaiting_request_state_kind: Literal["awaiting_conflict"]


class ConflictEscalateCaseResolver(Protocol):
    """ResourceRef is untrusted input; this server-side resolver is the proof."""

    def resolve_conflict_escalate_case(
        self, *, conflict_id: str
    ) -> ConflictEscalateCaseSnapshot | None: ...


@final
class SubjectRoleBinding(_FrozenAuthorityModel):
    org_id: str
    subject_id: str
    roles: tuple[Role, ...]

    @model_validator(mode="after")
    def _roles_are_unique(self) -> SubjectRoleBinding:
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("roles는 중복될 수 없습니다.")
        return self


@final
class RolePermission(_FrozenAuthorityModel):
    role: Role
    actions: tuple[Action, ...]

    @model_validator(mode="after")
    def _actions_are_unique(self) -> RolePermission:
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("actions는 중복될 수 없습니다.")
        return self


@final
class RouteRule(_FrozenAuthorityModel):
    org_id: str
    intent: str
    agent_card_id: str


@final
class WorkerBinding(_FrozenAuthorityModel):
    org_id: str
    credential_id: str
    owner_subject_id: str
    connection_role: Literal["primary", "backup"]
    generation: int

    @field_validator("generation", mode="after")
    @classmethod
    def _generation_is_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("generation은 1 이상이어야 합니다.")
        return value


@final
class AuthorityPolicySnapshot(_FrozenAuthorityModel):
    schema_version: Literal[1]
    org_id: str
    policy_version: str
    content_sha256: str
    subject_roles: tuple[SubjectRoleBinding, ...]
    role_permissions: tuple[RolePermission, ...]
    route_rules: tuple[RouteRule, ...]
    worker_bindings: tuple[WorkerBinding, ...]
    _grant_seal: object = PrivateAttr(default_factory=object)

    @field_validator("content_sha256", mode="after")
    @classmethod
    def _digest_is_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("content_sha256은 소문자 SHA-256이어야 합니다.")
        return value

    @model_validator(mode="after")
    def _snapshot_is_unique_and_single_org(self) -> AuthorityPolicySnapshot:
        if any(binding.org_id != self.org_id for binding in self.subject_roles):
            raise ValueError("subject role 조직이 snapshot과 다릅니다.")
        if any(rule.org_id != self.org_id for rule in self.route_rules):
            raise ValueError("route rule 조직이 snapshot과 다릅니다.")
        if any(binding.org_id != self.org_id for binding in self.worker_bindings):
            raise ValueError("worker binding 조직이 snapshot과 다릅니다.")
        if len({binding.subject_id for binding in self.subject_roles}) != len(self.subject_roles):
            raise ValueError("subject role binding은 중복될 수 없습니다.")
        if len({permission.role for permission in self.role_permissions}) != len(
            self.role_permissions
        ):
            raise ValueError("role permission은 중복될 수 없습니다.")
        route_keys = {(rule.intent, rule.agent_card_id) for rule in self.route_rules}
        if len(route_keys) != len(self.route_rules):
            raise ValueError("route rule은 중복될 수 없습니다.")
        if len({binding.credential_id for binding in self.worker_bindings}) != len(
            self.worker_bindings
        ):
            raise ValueError("worker binding은 중복될 수 없습니다.")
        return self


@final
class AuthorizationGrant(_FrozenAuthorityModel):
    org_id: str
    subject_id: str
    action: Action
    resource: ResourceRef
    roles: tuple[Role, ...]
    policy_version: str
    policy_digest: str
    _grant_seal: object = PrivateAttr(default_factory=object)

    @field_validator("policy_digest", mode="after")
    @classmethod
    def _policy_digest_is_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("policy_digest는 소문자 SHA-256이어야 합니다.")
        return value


@final
class AuthorizationDenied(_FrozenAuthorityModel):
    kind: Literal["not_found_or_denied", "policy_unavailable"]


AuthorizationResult: TypeAlias = AuthorizationGrant | AuthorizationDenied
PolicySnapshotProvider: TypeAlias = Callable[[], AuthorityPolicySnapshot]


class CentralAuthorizer(Protocol):
    def authorize(
        self,
        principal: AuthenticatedPrincipal,
        action: Action,
        resource: ResourceRef,
    ) -> AuthorizationResult: ...

    def verify(
        self,
        grant: AuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: Action,
        resource: ResourceRef,
    ) -> bool: ...


class _DuplicateYamlKeyError(ValueError):
    pass


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    pairs = cast(
        list[tuple[object, object]],
        loader.construct_pairs(node, deep=deep),  # pyright: ignore[reportUnknownMemberType]
    )
    mapping: dict[object, object] = {}
    for key, value in pairs:
        if key in mapping:
            raise _DuplicateYamlKeyError
        mapping[key] = value
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _canonical_policy_payload(document: object) -> dict[str, object]:
    """digest 전에 전체 shape와 의미를 검증하고 순서 없는 집합을 정규화한다."""

    parsed = _mapping(
        document,
        allowed=frozenset(
            {
                "schema_version",
                "org_id",
                "policy_version",
                "content_sha256",
                "subject_roles",
                "role_permissions",
                "route_rules",
                "worker_bindings",
            }
        ),
    )
    if parsed["schema_version"] != 1 or type(parsed["schema_version"]) is not int:
        raise AuthorityPolicyLoadError("schema_version")
    org_id = _nonblank(parsed["org_id"])
    policy_version = _nonblank(parsed["policy_version"])
    _nonblank(parsed["content_sha256"])
    subject_roles = _parse_subject_roles(parsed["subject_roles"], org_id)
    role_permissions = _parse_role_permissions(parsed["role_permissions"])
    route_rules = _parse_route_rules(parsed["route_rules"], org_id)
    worker_bindings = _parse_worker_bindings(parsed["worker_bindings"], org_id)
    return {
        "schema_version": 1,
        "org_id": org_id,
        "policy_version": policy_version,
        "subject_roles": [binding.model_dump() for binding in subject_roles],
        "role_permissions": [permission.model_dump() for permission in role_permissions],
        "route_rules": [rule.model_dump() for rule in route_rules],
        "worker_bindings": [binding.model_dump() for binding in worker_bindings],
    }


def canonical_policy_digest(document: object) -> str:
    """선언 digest 자체와 YAML 표현 순서를 제외한 정책 SHA-256을 계산한다."""

    try:
        canonical = json.dumps(
            _canonical_policy_payload(document),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(canonical).hexdigest()
    except AuthorityPolicyLoadError:
        raise
    except Exception:
        raise AuthorityPolicyLoadError("invalid_document") from None


def _mapping(value: object, *, allowed: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise AuthorityPolicyLoadError("invalid_document")
    raw_mapping = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in raw_mapping):
        raise AuthorityPolicyLoadError("invalid_document")
    result = {str(key): item for key, item in raw_mapping.items()}
    if set(result) - allowed:
        raise AuthorityPolicyLoadError("unknown_key")
    if allowed - set(result):
        raise AuthorityPolicyLoadError("invalid_document")
    return result


def _sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise AuthorityPolicyLoadError("invalid_document")
    return cast(list[object], value)


def _nonblank(value: object) -> str:
    if not isinstance(value, str):
        raise AuthorityPolicyLoadError("invalid_document")
    if not value.strip():
        raise AuthorityPolicyLoadError("blank_value")
    return value


def _same_org(value: object, expected_org_id: str) -> str:
    org_id = _nonblank(value)
    if org_id != expected_org_id:
        raise AuthorityPolicyLoadError("cross_org")
    return org_id


def _roles(value: object) -> tuple[Role, ...]:
    raw_roles = _sequence(value)
    if not raw_roles:
        raise AuthorityPolicyLoadError("invalid_document")
    roles: list[Role] = []
    for raw_role in raw_roles:
        role = _nonblank(raw_role)
        if role not in AUTHORITY_ROLES:
            raise AuthorityPolicyLoadError("unknown_role")
        roles.append(role)  # type: ignore[arg-type]
    if len(set(roles)) != len(roles):
        raise AuthorityPolicyLoadError("duplicate")
    return tuple(sorted(roles))


def _actions(value: object) -> tuple[Action, ...]:
    raw_actions = _sequence(value)
    if not raw_actions:
        raise AuthorityPolicyLoadError("invalid_document")
    actions: list[Action] = []
    for raw_action in raw_actions:
        action = _nonblank(raw_action)
        if action not in AUTHORITY_ACTION_MANIFEST:
            raise AuthorityPolicyLoadError("unknown_action")
        actions.append(action)  # type: ignore[arg-type]
    if len(set(actions)) != len(actions):
        raise AuthorityPolicyLoadError("duplicate")
    return tuple(sorted(actions))


def _parse_subject_roles(value: object, org_id: str) -> tuple[SubjectRoleBinding, ...]:
    bindings: list[SubjectRoleBinding] = []
    seen: set[str] = set()
    for value_entry in _sequence(value):
        entry = _mapping(
            value_entry,
            allowed=frozenset({"org_id", "subject_id", "roles"}),
        )
        subject_id = _nonblank(entry["subject_id"])
        if subject_id in seen:
            raise AuthorityPolicyLoadError("duplicate")
        seen.add(subject_id)
        bindings.append(
            SubjectRoleBinding(
                org_id=_same_org(entry["org_id"], org_id),
                subject_id=subject_id,
                roles=_roles(entry["roles"]),
            )
        )
    return tuple(sorted(bindings, key=lambda binding: binding.subject_id))


def _parse_role_permissions(value: object) -> tuple[RolePermission, ...]:
    permissions: list[RolePermission] = []
    seen: set[str] = set()
    for value_entry in _sequence(value):
        entry = _mapping(value_entry, allowed=frozenset({"role", "actions"}))
        role = _nonblank(entry["role"])
        if role not in AUTHORITY_ROLES:
            raise AuthorityPolicyLoadError("unknown_role")
        if role in seen:
            raise AuthorityPolicyLoadError("duplicate")
        seen.add(role)
        permissions.append(
            RolePermission(role=role, actions=_actions(entry["actions"]))  # type: ignore[arg-type]
        )
    return tuple(sorted(permissions, key=lambda permission: permission.role))


def _parse_route_rules(value: object, org_id: str) -> tuple[RouteRule, ...]:
    rules: list[RouteRule] = []
    seen: set[tuple[str, str]] = set()
    for value_entry in _sequence(value):
        entry = _mapping(
            value_entry,
            allowed=frozenset({"org_id", "intent", "agent_card_id"}),
        )
        intent = _nonblank(entry["intent"])
        agent_card_id = _nonblank(entry["agent_card_id"])
        key = (intent, agent_card_id)
        if key in seen:
            raise AuthorityPolicyLoadError("duplicate")
        seen.add(key)
        rules.append(
            RouteRule(
                org_id=_same_org(entry["org_id"], org_id),
                intent=intent,
                agent_card_id=agent_card_id,
            )
        )
    return tuple(sorted(rules, key=lambda rule: (rule.intent, rule.agent_card_id)))


def _parse_worker_bindings(value: object, org_id: str) -> tuple[WorkerBinding, ...]:
    bindings: list[WorkerBinding] = []
    seen: set[str] = set()
    for value_entry in _sequence(value):
        entry = _mapping(
            value_entry,
            allowed=frozenset(
                {
                    "org_id",
                    "credential_id",
                    "owner_subject_id",
                    "connection_role",
                    "generation",
                }
            ),
        )
        credential_id = _nonblank(entry["credential_id"])
        if credential_id in seen:
            raise AuthorityPolicyLoadError("duplicate")
        seen.add(credential_id)
        connection_role = _nonblank(entry["connection_role"])
        if connection_role not in {"primary", "backup"}:
            raise AuthorityPolicyLoadError("invalid_document")
        generation = entry["generation"]
        if type(generation) is not int or generation < 1:
            raise AuthorityPolicyLoadError("invalid_document")
        bindings.append(
            WorkerBinding(
                org_id=_same_org(entry["org_id"], org_id),
                credential_id=credential_id,
                owner_subject_id=_nonblank(entry["owner_subject_id"]),
                connection_role=connection_role,  # type: ignore[arg-type]
                generation=generation,
            )
        )
    return tuple(sorted(bindings, key=lambda binding: binding.credential_id))


def load_authority_policy_yaml(
    text: str,
    *,
    expected_org_id: str,
    expected_policy_version: str | None = None,
) -> AuthorityPolicySnapshot:
    """YAML을 startup-immutable 단일 조직 정책으로 정규화한다."""

    expected_org_id = _nonblank(expected_org_id)
    if not text.strip():
        raise AuthorityPolicyLoadError("missing_policy")
    try:
        raw_document = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except _DuplicateYamlKeyError:
        raise AuthorityPolicyLoadError("duplicate") from None
    except yaml.YAMLError:
        raise AuthorityPolicyLoadError("invalid_yaml") from None
    except Exception:
        raise AuthorityPolicyLoadError("invalid_yaml") from None

    document = _mapping(
        raw_document,
        allowed=frozenset(
            {
                "schema_version",
                "org_id",
                "policy_version",
                "content_sha256",
                "subject_roles",
                "role_permissions",
                "route_rules",
                "worker_bindings",
            }
        ),
    )
    if document["schema_version"] != 1 or type(document["schema_version"]) is not int:
        raise AuthorityPolicyLoadError("schema_version")
    org_id = _same_org(document["org_id"], expected_org_id)
    policy_version = _nonblank(document["policy_version"])
    if expected_policy_version is not None:
        if policy_version != _nonblank(expected_policy_version):
            raise AuthorityPolicyLoadError("policy_version")
    declared_digest = _nonblank(document["content_sha256"])
    try:
        computed_digest = canonical_policy_digest(document)
    except AuthorityPolicyLoadError:
        raise
    except Exception:
        raise AuthorityPolicyLoadError("invalid_document") from None
    if declared_digest != computed_digest:
        raise AuthorityPolicyLoadError("digest_mismatch")

    try:
        return AuthorityPolicySnapshot(
            schema_version=1,
            org_id=org_id,
            policy_version=policy_version,
            content_sha256=computed_digest,
            subject_roles=_parse_subject_roles(document["subject_roles"], org_id),
            role_permissions=_parse_role_permissions(document["role_permissions"]),
            route_rules=_parse_route_rules(document["route_rules"], org_id),
            worker_bindings=_parse_worker_bindings(document["worker_bindings"], org_id),
        )
    except AuthorityPolicyLoadError:
        raise
    except Exception:
        raise AuthorityPolicyLoadError("invalid_document") from None


@final
class SnapshotCentralAuthorizer:
    """현재 startup snapshot만 읽는 default-deny 중앙 authorizer."""

    def __init__(
        self,
        snapshot: AuthorityPolicySnapshot | PolicySnapshotProvider,
        *,
        conflict_open_request_resolver: ConflictOpenRequestResolver | None = None,
        conflict_escalate_case_resolver: ConflictEscalateCaseResolver | None = None,
    ) -> None:
        try:
            candidate = snapshot if type(snapshot) is AuthorityPolicySnapshot else snapshot()
            self._snapshot = _canonical_snapshot(candidate)
        except Exception:
            self._snapshot = None
        self._conflict_open_request_resolver = conflict_open_request_resolver
        self._conflict_escalate_case_resolver = conflict_escalate_case_resolver

    def authorize(
        self,
        principal: AuthenticatedPrincipal,
        action: object,
        resource: ResourceRef,
    ) -> AuthorizationResult:
        canonical_principal = _canonical_principal(principal)
        canonical_resource = _canonical_resource(resource)
        if canonical_principal is None or canonical_resource is None:
            return AuthorizationDenied(kind="not_found_or_denied")
        snapshot = self._snapshot
        if snapshot is None:
            return AuthorizationDenied(kind="policy_unavailable")

        if type(action) is not str or action not in AUTHORITY_ACTION_MANIFEST:
            return AuthorizationDenied(kind="not_found_or_denied")
        canonical_action = cast(Action, action)
        if (
            canonical_principal.org_id != snapshot.org_id
            or canonical_resource.org_id != snapshot.org_id
        ):
            return AuthorizationDenied(kind="not_found_or_denied")
        required_kind = ACTION_RESOURCE_KIND_REQUIREMENTS.get(canonical_action)
        if required_kind is not None:
            if canonical_resource.kind != required_kind or canonical_resource.resource_id is None:
                return AuthorizationDenied(kind="not_found_or_denied")
            # conflict_case는 existing open Case 실재로 동적 결박한다(owner_subject_id 불요).
            if required_kind != "conflict_case" and canonical_resource.owner_subject_id is None:
                return AuthorizationDenied(kind="not_found_or_denied")
        if canonical_action == "conflict.open" and not self._current_conflict_open_request_matches(
            canonical_principal, canonical_resource
        ):
            return AuthorizationDenied(kind="not_found_or_denied")
        if (
            canonical_action == "conflict.escalate"
            and not self._current_conflict_escalate_case_matches(
                canonical_principal, canonical_resource
            )
        ):
            return AuthorizationDenied(kind="not_found_or_denied")

        subject = next(
            (
                binding
                for binding in snapshot.subject_roles
                if binding.subject_id == canonical_principal.subject_id
            ),
            None,
        )
        if subject is None:
            return AuthorizationDenied(kind="not_found_or_denied")
        granting_roles: set[Role] = {
            permission.role
            for permission in snapshot.role_permissions
            if permission.role in subject.roles
            if canonical_action in permission.actions
        }
        allowed_roles = ACTION_ALLOWED_ROLES.get(canonical_action)
        if allowed_roles is not None:
            granting_roles &= allowed_roles
        if not granting_roles:
            return AuthorizationDenied(kind="not_found_or_denied")
        dynamic_roles = DYNAMIC_SUBJECT_REQUIREMENTS.get(canonical_action, frozenset())
        eligible_roles: set[Role] = {
            role
            for role in granting_roles
            if role not in dynamic_roles
            or canonical_resource.owner_subject_id == canonical_principal.subject_id
        }
        if not eligible_roles:
            return AuthorizationDenied(kind="not_found_or_denied")

        grant = AuthorizationGrant(
            org_id=snapshot.org_id,
            subject_id=canonical_principal.subject_id,
            action=canonical_action,
            resource=canonical_resource,
            roles=tuple(sorted(eligible_roles)),
            policy_version=snapshot.policy_version,
            policy_digest=snapshot.content_sha256,
        )
        grant._grant_seal = snapshot._grant_seal  # pyright: ignore[reportPrivateUsage]
        return grant

    def _current_conflict_open_request_matches(
        self, principal: AuthenticatedPrincipal, resource: ResourceRef
    ) -> bool:
        resolver = self._conflict_open_request_resolver
        if resolver is None or resource.resource_id is None or resource.owner_subject_id is None:
            return False
        try:
            current = resolver.resolve_conflict_open_request(request_id=resource.resource_id)
            return (
                type(current) is ConflictOpenRequestSnapshot
                and current.org_id == principal.org_id == resource.org_id
                and current.request_id == resource.resource_id
                and current.requester_subject_id
                == principal.subject_id
                == resource.owner_subject_id
                and current.state_kind == "received"
                and current.revision == 0
            )
        except Exception:
            return False

    def _current_conflict_escalate_case_matches(
        self, principal: AuthenticatedPrincipal, resource: ResourceRef
    ) -> bool:
        resolver = self._conflict_escalate_case_resolver
        if resolver is None or resource.resource_id is None:
            return False
        try:
            current = resolver.resolve_conflict_escalate_case(conflict_id=resource.resource_id)
            return (
                type(current) is ConflictEscalateCaseSnapshot
                and current.org_id == principal.org_id == resource.org_id
                and current.conflict_id == resource.resource_id
                and current.state_kind == "open"
                and current.awaiting_request_state_kind == "awaiting_conflict"
            )
        except Exception:
            return False

    def verify(
        self,
        grant: AuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: object,
        resource: ResourceRef,
    ) -> bool:
        snapshot = self._snapshot
        if snapshot is None:
            return False
        return verify_authorization_grant(
            grant,
            principal,
            action,
            resource,
            snapshot,
            conflict_open_request_resolver=self._conflict_open_request_resolver,
            conflict_escalate_case_resolver=self._conflict_escalate_case_resolver,
        )


def _canonical_principal(value: object) -> AuthenticatedPrincipal | None:
    if type(value) is not AuthenticatedPrincipal:
        return None
    try:
        return AuthenticatedPrincipal.model_validate(value)
    except Exception:
        return None


def _canonical_resource(value: object) -> ResourceRef | None:
    if type(value) is not ResourceRef:
        return None
    try:
        return ResourceRef.model_validate(value)
    except Exception:
        return None


def _canonical_snapshot(value: object) -> AuthorityPolicySnapshot | None:
    if type(value) is not AuthorityPolicySnapshot:
        return None
    try:
        AuthorityPolicySnapshot.model_validate(value)
        if canonical_policy_digest(value.model_dump(mode="json")) != value.content_sha256:
            return None
        return value
    except Exception:
        return None


def _canonical_grant(value: object) -> AuthorizationGrant | None:
    if type(value) is not AuthorizationGrant:
        return None
    try:
        AuthorizationGrant.model_validate(value)
        return value
    except Exception:
        return None


def verify_authorization_grant(
    grant: AuthorizationGrant,
    principal: AuthenticatedPrincipal,
    action: object,
    resource: ResourceRef,
    snapshot: AuthorityPolicySnapshot,
    *,
    conflict_open_request_resolver: ConflictOpenRequestResolver | None = None,
    conflict_escalate_case_resolver: ConflictEscalateCaseResolver | None = None,
) -> bool:
    """grant를 현재 호출과 exact 비교해 다른 명령·리소스 재사용을 막는다."""

    canonical_principal = _canonical_principal(principal)
    canonical_resource = _canonical_resource(resource)
    canonical_grant = _canonical_grant(grant)
    canonical_snapshot = _canonical_snapshot(snapshot)
    if (
        canonical_grant is None
        or canonical_principal is None
        or canonical_resource is None
        or canonical_snapshot is None
        or type(action) is not str
        or action not in AUTHORITY_ACTION_MANIFEST
    ):
        return False
    try:
        if (
            canonical_grant._grant_seal  # pyright: ignore[reportPrivateUsage]
            is not canonical_snapshot._grant_seal  # pyright: ignore[reportPrivateUsage]
        ):
            return False
        expected = SnapshotCentralAuthorizer(
            canonical_snapshot,
            conflict_open_request_resolver=conflict_open_request_resolver,
            conflict_escalate_case_resolver=conflict_escalate_case_resolver,
        ).authorize(
            canonical_principal,
            action,
            canonical_resource,
        )
        return type(expected) is AuthorizationGrant and canonical_grant == expected
    except Exception:
        return False
