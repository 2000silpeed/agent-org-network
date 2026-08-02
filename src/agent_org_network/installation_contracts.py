"""Frozen RB3 installation and control-transport boundary contracts.

These are declaration-time contracts.  They do not bind a socket, perform an
HTTP call, or compose a runtime.  Composition roots consume the values later.
"""

from __future__ import annotations

from enum import Enum
import re
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"v[1-9][0-9]*")
_SAFE_ROUTE = re.compile(r"/[a-z0-9][a-z0-9/_-]*")
_FORBIDDEN_TRANSPORT_KEYS = frozenset(
    {"raw", "draft", "token", "password", "secret", "source", "content", "upstream"}
)


class InstallationKind(str, Enum):
    """The sealed product-installation set from ADR 0067 and ADR 0075."""

    CENTRAL_SERVER = "central_server"
    CARD_OWNER = "card_owner"
    QUESTION_USER = "question_user"


class ListenerScope(str, Enum):
    """Whether a process listener is private to its installation or public."""

    PRIVATE = "private"
    PUBLIC = "public"


CentralOwnerControlAction = Literal["pair", "publish", "work_ticket", "submit_answer"]


class ListenerPort(BaseModel, frozen=True):
    """A concrete TCP port; absence of a listener is represented by no value."""

    model_config = ConfigDict(extra="forbid", strict=True)

    value: int = Field(ge=1, le=65535)


class ListenerBind(BaseModel, frozen=True):
    """A listener bind with Card Owner loopback-only enforcement."""

    model_config = ConfigDict(extra="forbid", strict=True)

    host: str
    installation: InstallationKind
    scope: ListenerScope = ListenerScope.PRIVATE

    @field_validator("installation", mode="before")
    @classmethod
    def _installation_kind(cls, value: object) -> object:
        return _parse_installation_kind(value)

    @field_validator("scope", mode="before")
    @classmethod
    def _scope(cls, value: object) -> object:
        if type(value) is str:
            try:
                return ListenerScope(value)
            except ValueError:
                return value
        return value

    @model_validator(mode="after")
    def _installation_boundary(self) -> "ListenerBind":
        if self.installation is InstallationKind.QUESTION_USER:
            raise ValueError("Question User MCP has no network listener")
        if self.installation is InstallationKind.CARD_OWNER:
            if self.host not in {"127.0.0.1", "::1"} or self.scope is not ListenerScope.PRIVATE:
                raise ValueError("Card Owner listener must be private loopback")
        if self.scope is ListenerScope.PUBLIC and self.host in {"127.0.0.1", "::1"}:
            raise ValueError("public listener cannot bind loopback")
        if self.host in {"0.0.0.0", "::"} and self.scope is not ListenerScope.PUBLIC:
            raise ValueError("wildcard listener must be explicitly public")
        return self


class VersionedPublicRouteNamespace(BaseModel, frozen=True):
    """A normalized public HTTP route beneath its explicit version namespace."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: str
    path: str

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if _VERSION.fullmatch(value) is None:
            raise ValueError("version must be v<positive integer>")
        return value

    @field_validator("path")
    @classmethod
    def _path_shape(cls, value: str) -> str:
        if _SAFE_ROUTE.fullmatch(value) is None or "//" in value or "/../" in value:
            raise ValueError("normalized absolute route required")
        return value

    @model_validator(mode="after")
    def _under_version(self) -> "VersionedPublicRouteNamespace":
        if not (self.path == f"/{self.version}" or self.path.startswith(f"/{self.version}/")):
            raise ValueError("public route must be under its version namespace")
        return self

    def allows(self, candidate: str) -> bool:
        """Match only the namespace itself or a slash-delimited descendant."""
        return candidate == self.path or candidate.startswith(f"{self.path}/")


class InstallationListener(BaseModel, frozen=True):
    """A process listener that cannot receive a caller-selected upstream."""

    model_config = ConfigDict(extra="forbid", strict=True)

    bind: ListenerBind
    port: ListenerPort
    public_routes: tuple[VersionedPublicRouteNamespace, ...] = ()

    @model_validator(mode="after")
    def _routes_follow_scope(self) -> "InstallationListener":
        if self.bind.scope is ListenerScope.PRIVATE and self.public_routes:
            raise ValueError("private listener cannot declare public routes")
        if self.bind.scope is ListenerScope.PUBLIC and not self.public_routes:
            raise ValueError("public listener requires versioned public routes")
        return self


class CentralOwnerControlEnvelope(BaseModel, frozen=True):
    """Body-free Central Server ↔ Card Owner control command metadata."""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: CentralOwnerControlAction
    org_id: str
    owner_id: str
    agent_card_id: str
    card_revision: int = Field(gt=0)
    card_digest: str
    credential_ref: str
    idempotency_key: str
    expected_revision: int = Field(ge=0)

    @field_validator(
        "org_id", "owner_id", "agent_card_id", "credential_ref", "idempotency_key"
    )
    @classmethod
    def _reference(cls, value: str) -> str:
        if _REFERENCE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("card_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 digest required")
        return value

    @model_validator(mode="before")
    @classmethod
    def _no_body_or_secret_fields(cls, value: object) -> object:
        _reject_forbidden_transport_keys(value)
        return value


CENTRAL_SERVER_ALLOWED_PROCESSES: tuple[str, ...] = ("central-next", "central-api")
CARD_OWNER_ALLOWED_PROCESSES: tuple[str, ...] = ("owner-local-next", "owner-api", "owner-worker")
QUESTION_USER_ALLOWED_PROCESSES: tuple[str, ...] = ("question-user-mcp",)
QUESTION_USER_MCP_TOOL_MANIFEST: tuple[str, ...] = ("ask_org", "get_question")
CENTRAL_SERVER_IMPORT_ALLOWLIST: tuple[str, ...] = (
    "agent_org_network.agent_card",
    "agent_org_network.central_api",
    "agent_org_network.central_authority",
    "agent_org_network.central_bootstrap_admin",
    "agent_org_network.central_bootstrap_device",
    "agent_org_network.central_bootstrap_sqlite",
    "agent_org_network.central_browser_auth",
    "agent_org_network.central_browser_auth_sqlite",
    "agent_org_network.central_browser_oidc",
    "agent_org_network.central_cli",
    "agent_org_network.central_composition",
    "agent_org_network.central_inbox_api",
    "agent_org_network.central_inbox_approval",
    "agent_org_network.central_inbox_conflict",
    "agent_org_network.central_inbox_review",
    "agent_org_network.central_lifecycle_recovery",
    "agent_org_network.central_oidc_principal",
    "agent_org_network.central_operational_evidence",
    "agent_org_network.central_policy_revision",
    "agent_org_network.central_question_intake",
    "agent_org_network.central_question_lifecycle",
    "agent_org_network.central_question_request_sqlite",
    "agent_org_network.central_registry_admission",
    "agent_org_network.central_web_runtime",
    "agent_org_network.decision",
    "agent_org_network.oidc",
    "agent_org_network.question_request",
    "agent_org_network.registry",
    "agent_org_network.sqlite_production_agent_cards",
    "agent_org_network.sqlite_production_registry_users",
    "agent_org_network.user",
)
CARD_OWNER_IMPORT_ALLOWLIST: tuple[str, ...] = (
    "agent_org_network.owner_api",
    "agent_org_network.owner_cli",
    "agent_org_network.owner_composition",
)
QUESTION_USER_IMPORT_ALLOWLIST: tuple[str, ...] = (
    "agent_org_network.question_user_mcp",
)
CENTRAL_SERVER_SURFACE_ALLOWLIST: tuple[str, ...] = (
    "GET /admin/agent-cards",
    "GET /admin/users",
    "GET /healthz",
    "GET /onboarding/status",
    "GET /readyz",
    "GET /v1/admin/policy",
    "GET /v1/admin/scorecard",
    "GET /v1/browser-auth/callback",
    "GET /v1/browser-auth/session",
    "GET /v1/console/audit",
    "GET /v1/console/audit/{audit_id}",
    "GET /v1/console/feed",
    "GET /v1/console/org",
    "GET /v1/inbox/approvals",
    "GET /v1/inbox/approvals/{approval_item_id}",
    "GET /v1/inbox/backup-reviews",
    "GET /v1/inbox/backup-reviews/{review_id}",
    "GET /v1/inbox/conflicts",
    "GET /v1/inbox/conflicts/{case_id}",
    "GET /v1/inbox/reevaluations",
    "GET /v1/inbox/reevaluations/{reevaluation_id}",
    "GET /v1/questions/{request_id}",
    "GET /v1/questions/{request_id}/stream",
    "POST /admin/agent-cards",
    "POST /admin/users",
    "POST /v1/admin/agent-cards/{card_id}/owner-transfers",
    "POST /v1/admin/agent-cards/{card_id}/revocations",
    "POST /v1/admin/policy/revisions",
    "POST /v1/browser-auth/login/start",
    "POST /v1/browser-auth/logout",
    "POST /v1/inbox/approvals/{approval_item_id}/dispositions",
    "POST /v1/inbox/approvals/{approval_item_id}/reassignments",
    "POST /v1/inbox/backup-reviews/{review_id}/dispositions",
    "POST /v1/inbox/conflicts/{case_id}/concurrences",
    "POST /v1/inbox/reevaluations/{reevaluation_id}/dispositions",
    "POST /v1/questions",
    "POST /v1/questions/{request_id}/feedback",
)
CARD_OWNER_SURFACE_ALLOWLIST: tuple[str, ...] = (
    "GET /healthz",
    "GET /readyz",
    "GET /v1/pairing/status",
    "POST /v1/pairing/redeem",
)
QUESTION_USER_SURFACE_ALLOWLIST: tuple[str, ...] = ()

_ALLOWED_PROCESSES: dict[InstallationKind, frozenset[str]] = {
    InstallationKind.CENTRAL_SERVER: frozenset(CENTRAL_SERVER_ALLOWED_PROCESSES),
    InstallationKind.CARD_OWNER: frozenset(CARD_OWNER_ALLOWED_PROCESSES),
    InstallationKind.QUESTION_USER: frozenset(QUESTION_USER_ALLOWED_PROCESSES),
}
_IMPORT_ALLOWLISTS: dict[InstallationKind, tuple[str, ...]] = {
    InstallationKind.CENTRAL_SERVER: CENTRAL_SERVER_IMPORT_ALLOWLIST,
    InstallationKind.CARD_OWNER: CARD_OWNER_IMPORT_ALLOWLIST,
    InstallationKind.QUESTION_USER: QUESTION_USER_IMPORT_ALLOWLIST,
}
_SURFACE_ALLOWLISTS: dict[InstallationKind, tuple[str, ...]] = {
    InstallationKind.CENTRAL_SERVER: CENTRAL_SERVER_SURFACE_ALLOWLIST,
    InstallationKind.CARD_OWNER: CARD_OWNER_SURFACE_ALLOWLIST,
    InstallationKind.QUESTION_USER: QUESTION_USER_SURFACE_ALLOWLIST,
}
_TOOL_ALLOWLISTS: dict[InstallationKind, tuple[str, ...]] = {
    InstallationKind.CENTRAL_SERVER: (),
    InstallationKind.CARD_OWNER: (),
    InstallationKind.QUESTION_USER: QUESTION_USER_MCP_TOOL_MANIFEST,
}


class ArtifactManifest(BaseModel, frozen=True):
    """Static allowlist declaration for one installation artifact.

    The manifest purposefully lists only its own processes, routes, tools and
    imported modules.  Unknown fields are rejected, so it cannot smuggle an
    upstream URL, raw body, or secret through a declaration surface.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    installation: InstallationKind
    processes: tuple[str, ...] = ()
    routes: tuple[VersionedPublicRouteNamespace, ...] = ()
    tools: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    surfaces: tuple[str, ...] = ()

    @field_validator("installation", mode="before")
    @classmethod
    def _installation_kind(cls, value: object) -> object:
        return _parse_installation_kind(value)

    @model_validator(mode="before")
    @classmethod
    def _forbid_transport_payloads(cls, value: object) -> object:
        _reject_forbidden_transport_keys(value)
        return value

    @model_validator(mode="after")
    def _installation_allowlists(self) -> "ArtifactManifest":
        expected_processes = tuple(sorted(_ALLOWED_PROCESSES[self.installation]))
        if tuple(sorted(self.processes)) != expected_processes:
            raise ValueError("process manifest must match the installation allowlist exactly")
        if self.routes:
            raise ValueError("this first slice exposes no versioned public route namespace")
        if self.tools != _TOOL_ALLOWLISTS[self.installation]:
            raise ValueError("tool manifest must match the installation allowlist exactly")
        if self.imports != _IMPORT_ALLOWLISTS[self.installation]:
            raise ValueError("import graph must match the installation allowlist exactly")
        if self.surfaces != _SURFACE_ALLOWLISTS[self.installation]:
            raise ValueError("surface graph must match the installation allowlist exactly")
        return self


class RunnableLocalReferencePromotionEvidence(BaseModel, frozen=True):
    """Evidence required before a later support-contract promotion decision."""

    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_support_level: Literal["runnable_local_reference"] = "runnable_local_reference"
    clean_install: bool
    doctor_passed: bool
    separate_processes: bool
    test_oidc_issuer: bool
    loopback_tls_ca: bool
    test_keychain: bool
    card_owner_pairing: bool
    published_revision_receipt: bool
    question_user_mcp: bool
    raw_and_secret_non_egress: bool

    @model_validator(mode="after")
    def _all_evidence_required(self) -> "RunnableLocalReferencePromotionEvidence":
        if not self.is_complete:
            raise ValueError("all local reference evidence must be present before promotion review")
        return self

    @property
    def is_complete(self) -> bool:
        return all(
            (
                self.clean_install,
                self.doctor_passed,
                self.separate_processes,
                self.test_oidc_issuer,
                self.loopback_tls_ca,
                self.test_keychain,
                self.card_owner_pairing,
                self.published_revision_receipt,
                self.question_user_mcp,
                self.raw_and_secret_non_egress,
            )
        )


def _reject_forbidden_transport_keys(value: object) -> None:
    """Reject nested body/secret/upstream fields before Pydantic coercion."""

    if isinstance(value, dict):
        fields = cast(dict[object, object], value)
        for key, nested in fields.items():
            if type(key) is not str:
                raise ValueError("transport field names must be strings")
            normalized = key.lower().replace("-", "_")
            pieces = frozenset(part for part in normalized.split("_") if part)
            if pieces & _FORBIDDEN_TRANSPORT_KEYS:
                raise ValueError("raw body, secret, and caller-selected upstream fields are forbidden")
            _reject_forbidden_transport_keys(nested)
    elif isinstance(value, (list, tuple)):
        values = cast(list[object] | tuple[object, ...], value)
        for nested in values:
            _reject_forbidden_transport_keys(nested)


def _parse_installation_kind(value: object) -> object:
    if type(value) is str:
        try:
            return InstallationKind(value)
        except ValueError:
            return value
    return value


__all__ = [
    "ArtifactManifest",
    "CARD_OWNER_ALLOWED_PROCESSES",
    "CARD_OWNER_IMPORT_ALLOWLIST",
    "CARD_OWNER_SURFACE_ALLOWLIST",
    "CENTRAL_SERVER_ALLOWED_PROCESSES",
    "CENTRAL_SERVER_IMPORT_ALLOWLIST",
    "CENTRAL_SERVER_SURFACE_ALLOWLIST",
    "CentralOwnerControlEnvelope",
    "InstallationKind",
    "InstallationListener",
    "ListenerBind",
    "ListenerPort",
    "ListenerScope",
    "QUESTION_USER_ALLOWED_PROCESSES",
    "QUESTION_USER_IMPORT_ALLOWLIST",
    "QUESTION_USER_MCP_TOOL_MANIFEST",
    "QUESTION_USER_SURFACE_ALLOWLIST",
    "RunnableLocalReferencePromotionEvidence",
    "VersionedPublicRouteNamespace",
]
