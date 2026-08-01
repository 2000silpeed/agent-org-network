"""One-time, non-browser Bootstrap Admin admission application (RB3.2a)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import stat
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
import yaml

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorizationGrant,
    CentralAuthorizer,
    ResourceRef,
)
from agent_org_network.central_bootstrap_sqlite import (
    BootstrapAdminSeal,
    CentralBootstrapAdminSealStore,
    CentralBootstrapSealConflict,
    CentralBootstrapSealUnavailable,
)
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    ProductionRegistryUserConflict,
    ProductionRegistryUserDenied,
    ProductionRegistryUserUnavailable,
    SqliteProductionRegistryUsers,
    TxCurrentUserRegistrationAuthorizer,
    production_registry_user_command_digest,
)


_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MAX_ATTESTATION_BYTES = 64 * 1024


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    pairs = cast(
        list[tuple[object, object]],
        loader.construct_pairs(node, deep=deep),  # pyright: ignore[reportUnknownMemberType]
    )
    values: dict[object, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("duplicate bootstrap attestation key")
        values[key] = value
    return values


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


class BootstrapAdminConfigurationError(RuntimeError):
    """Profile/attestation is missing or malformed; no raw values are retained."""


class BootstrapAdminDenied(RuntimeError):
    """Current OIDC identity or Authority does not admit this bootstrap command."""


class BootstrapAdminConflict(RuntimeError):
    """A different bootstrap command/replay owns the one-time admission."""


class BootstrapAdminUnavailable(RuntimeError):
    """A durable dependency, schema, device flow, or read-back is unavailable."""


class BootstrapOidcDeviceDenied(RuntimeError):
    """Optional device adapter signal mapped to the stable denied outcome."""


class BootstrapOidcDeviceUnavailable(RuntimeError):
    """Optional device adapter signal mapped to the stable unavailable outcome."""


class BootstrapAdminAttestation(BaseModel, frozen=True):
    """Exact, non-secret attestation input; raw identity data is intentionally absent."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int
    attestation_id: str
    org_id: str
    registry_user_id: str
    oidc_provider_id: str
    oidc_issuer_digest: str
    oidc_audience_digest: str
    oidc_subject_digest: str
    verified_email_digest: str
    device_authorization_ref: str
    idempotency_key: str
    expected_registry_revision: int
    authority_policy_digest: str

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported bootstrap attestation schema")
        return value

    @field_validator("expected_registry_revision")
    @classmethod
    def _revision(cls, value: int) -> int:
        if value != 0:
            raise ValueError("bootstrap expected registry revision must be zero")
        return value

    @field_validator(
        "attestation_id",
        "org_id",
        "registry_user_id",
        "oidc_provider_id",
        "device_authorization_ref",
        "idempotency_key",
    )
    @classmethod
    def _opaque(cls, value: str) -> str:
        if _REFERENCE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator(
        "oidc_issuer_digest",
        "oidc_audience_digest",
        "oidc_subject_digest",
        "verified_email_digest",
        "authority_policy_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("lowercase SHA-256 digest required")
        return value

    @classmethod
    def load(cls, path: Path) -> "BootstrapAdminAttestation":
        """Strict JSON/YAML loader that deliberately hides raw parse values."""
        try:
            metadata = path.lstat()
        except OSError as error:
            raise BootstrapAdminConfigurationError() from error
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_ATTESTATION_BYTES:
            raise BootstrapAdminConfigurationError()
        try:
            raw = path.read_text(encoding="utf-8")
            # This tiny exact-field document has no legitimate YAML anchor/alias use.
            # Rejecting both before construction eliminates alias-expansion resource attacks.
            if "&" in raw or "*" in raw:
                raise ValueError("YAML anchors and aliases are not accepted")
            value = yaml.load(raw, Loader=_UniqueKeyLoader)
            if not isinstance(value, dict):
                raise ValueError
            return cls.model_validate(value, strict=True)
        except (OSError, ValidationError, ValueError, yaml.YAMLError) as error:
            raise BootstrapAdminConfigurationError() from error

    def digest(self) -> str:
        return sha256(
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()


@dataclass(frozen=True, repr=False, slots=True)
class VerifiedBootstrapIdentity:
    """Verified, memory-only device identity.  Its repr can never disclose claims."""

    issuer: str
    audience: str
    subject: str
    email: str
    email_verified: bool

    def __repr__(self) -> str:
        return "VerifiedBootstrapIdentity(redacted)"


class BootstrapOidcDeviceAuthorizer(Protocol):
    def authorize(
        self,
        *,
        provider_id: str,
        issuer: str,
        audience: str,
        device_authorization_ref: str,
    ) -> VerifiedBootstrapIdentity: ...


@dataclass(frozen=True, slots=True)
class BootstrapAdminConfig:
    org_id: str
    oidc_provider_id: str
    oidc_issuer: str
    oidc_audience: str
    authority_policy_digest: str


@dataclass(frozen=True, slots=True)
class BootstrapAdminResult:
    state: str
    attestation_id: str
    registry_user_id: str
    revision: int
    registration_command_digest: str
    seal_digest: str
    replayed: bool


RegistryFactory = Callable[[TxCurrentUserRegistrationAuthorizer], SqliteProductionRegistryUsers]
Clock = Callable[[], datetime]
FaultInjector = Callable[[str], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _no_fault(_point: str) -> None:
    return None


def _registry_factory_for(
    path: Path,
) -> RegistryFactory:
    def factory(authorizer: TxCurrentUserRegistrationAuthorizer) -> SqliteProductionRegistryUsers:
        return SqliteProductionRegistryUsers(path, authorize=authorizer)

    return factory


class BootstrapAdminApplication:
    """Validate identity + current Authority, then reuse Registry admission unchanged."""

    def __init__(
        self,
        *,
        config: BootstrapAdminConfig,
        authority: CentralAuthorizer,
        device_authorizer: BootstrapOidcDeviceAuthorizer,
        registry_path: Path,
        seals: CentralBootstrapAdminSealStore,
        registry_factory: RegistryFactory | None = None,
        clock: Clock | None = None,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        _validate_config(config)
        if not registry_path.is_file():
            raise BootstrapAdminUnavailable()
        self._config = config
        self._authority = authority
        self._device_authorizer = device_authorizer
        self._seals = seals
        self._registry_factory: RegistryFactory = registry_factory or _registry_factory_for(registry_path)
        self._clock = clock or _utc_now
        self._fault = fault_injector or _no_fault

    def run(self, attestation: BootstrapAdminAttestation) -> BootstrapAdminResult:
        if type(attestation) is not BootstrapAdminAttestation:
            raise BootstrapAdminConfigurationError()
        identity = self._verified_identity(attestation)
        try:
            command = ProductionRegistryUserCommand(
                org_id=attestation.org_id,
                principal_id=attestation.registry_user_id,
                idempotency_key=attestation.idempotency_key,
                expected_revision=0,
                user_id=attestation.registry_user_id,
                email=identity.email,
                manager_id=None,
            )
        except Exception:
            raise BootstrapAdminDenied() from None
        command_digest = production_registry_user_command_digest(command)
        grant = self._current_grant(attestation, command, identity)
        registration_authorizer = BootstrapAdminRegistrationAuthorizer(
            command=command,
            principal=_principal_for(attestation),
            resource=_resource_for(attestation),
            grant=grant,
            authority=self._authority,
            attestation=attestation,
        )
        try:
            registry = self._registry_factory(registration_authorizer)
            existing = self._seals.get(attestation.org_id)
            if existing is not None and not _seal_matches(existing, attestation, command_digest):
                raise BootstrapAdminConflict()
            result = registry.register(command)
            if result.revision != 1 or result.user.manager_id is not None:
                raise BootstrapAdminUnavailable()
            self._fault("after_registry_before_seal")
            sealed = self._seals.seal(
                org_id=attestation.org_id,
                attestation_id=attestation.attestation_id,
                attestation_digest=attestation.digest(),
                registry_user_id=attestation.registry_user_id,
                registration_command_digest=command_digest,
                authority_policy_digest=attestation.authority_policy_digest,
                sealed_at=_require_aware(self._clock()),
            )
            if not _seal_matches(sealed, attestation, command_digest):
                raise BootstrapAdminUnavailable()
            # Registry.register's replay path validates the receipt/audit/outbox semantic graph.
            reread = registry.register(command)
            if reread.user != result.user or reread.revision != result.revision or not reread.replayed:
                raise BootstrapAdminUnavailable()
            return BootstrapAdminResult(
                state="BootstrapSealed",
                attestation_id=attestation.attestation_id,
                registry_user_id=attestation.registry_user_id,
                revision=result.revision,
                registration_command_digest=command_digest,
                seal_digest=sealed.seal_digest,
                replayed=result.replayed,
            )
        except BootstrapAdminConflict:
            raise
        except (ProductionRegistryUserConflict, CentralBootstrapSealConflict):
            raise BootstrapAdminConflict() from None
        except ProductionRegistryUserDenied:
            raise BootstrapAdminDenied() from None
        except (ProductionRegistryUserUnavailable, CentralBootstrapSealUnavailable):
            raise BootstrapAdminUnavailable() from None
        except BootstrapAdminUnavailable:
            raise
        except Exception:
            raise BootstrapAdminUnavailable() from None

    def _verified_identity(self, attestation: BootstrapAdminAttestation) -> VerifiedBootstrapIdentity:
        if (
            attestation.org_id != self._config.org_id
            or attestation.oidc_provider_id != self._config.oidc_provider_id
            or attestation.oidc_issuer_digest != _digest(self._config.oidc_issuer)
            or attestation.oidc_audience_digest != _digest(self._config.oidc_audience)
            or attestation.authority_policy_digest != self._config.authority_policy_digest
        ):
            raise BootstrapAdminDenied()
        try:
            identity = self._device_authorizer.authorize(
                provider_id=self._config.oidc_provider_id,
                issuer=self._config.oidc_issuer,
                audience=self._config.oidc_audience,
                device_authorization_ref=attestation.device_authorization_ref,
            )
        except BootstrapOidcDeviceDenied:
            raise BootstrapAdminDenied() from None
        except BootstrapOidcDeviceUnavailable:
            raise BootstrapAdminUnavailable() from None
        except Exception:
            raise BootstrapAdminUnavailable() from None
        if type(identity) is not VerifiedBootstrapIdentity:
            raise BootstrapAdminUnavailable()
        if (
            not identity.email_verified
            or identity.issuer != self._config.oidc_issuer
            or identity.audience != self._config.oidc_audience
            or _digest(identity.issuer + "\x00" + identity.subject)
            != attestation.oidc_subject_digest
            or _digest(identity.email) != attestation.verified_email_digest
        ):
            raise BootstrapAdminDenied()
        return identity

    def _current_grant(
        self,
        attestation: BootstrapAdminAttestation,
        command: ProductionRegistryUserCommand,
        identity: VerifiedBootstrapIdentity,
    ) -> AuthorizationGrant:
        _ = identity  # raw identity never crosses the Authority/Registry command boundary except email.
        principal = _principal_for(attestation)
        resource = _resource_for(attestation)
        try:
            result = self._authority.authorize(principal, "user.register", resource)
            if type(result) is AuthorizationDenied and result.kind == "policy_unavailable":
                raise BootstrapAdminUnavailable()
            if type(result) is AuthorizationDenied:
                raise BootstrapAdminDenied()
            if type(result) is not AuthorizationGrant:
                raise BootstrapAdminUnavailable()
            canonical = AuthorizationGrant.model_validate(result)
            matches = bool(
                canonical.org_id == principal.org_id == resource.org_id
                and canonical.subject_id == principal.subject_id
                and canonical.action == "user.register"
                and canonical.resource == resource
                and canonical.roles
                and set(canonical.roles).issubset({"admin", "operator"})
                and canonical.policy_digest == attestation.authority_policy_digest
            )
            if not matches:
                raise BootstrapAdminDenied()
            if not self._authority.verify(result, principal, "user.register", resource):
                raise BootstrapAdminDenied()
            if command.manager_id is not None or command.expected_revision != 0:
                raise BootstrapAdminUnavailable()
            return result
        except BootstrapAdminDenied:
            raise
        except Exception:
            raise BootstrapAdminUnavailable() from None


class BootstrapAdminRegistrationAuthorizer:
    """Transaction-time current Authority proof for one exact Registry command."""

    def __init__(
        self,
        *,
        command: ProductionRegistryUserCommand,
        principal: AuthenticatedPrincipal,
        resource: ResourceRef,
        grant: AuthorizationGrant,
        authority: CentralAuthorizer,
        attestation: BootstrapAdminAttestation,
    ) -> None:
        self._command = command
        self._principal = principal
        self._resource = resource
        self._grant = grant
        self._authority = authority
        self._attestation = attestation

    def current(
        self, command: ProductionRegistryUserCommand, transaction: sqlite3.Connection
    ) -> CurrentUserRegistrationAuthorization:
        _ = transaction
        if command != self._command:
            raise ProductionRegistryUserDenied()
        try:
            current_grant = self._authority.authorize(
                self._principal, "user.register", self._resource
            )
        except Exception:
            raise ProductionRegistryUserUnavailable() from None
        if (
            type(current_grant) is AuthorizationDenied
            and current_grant.kind == "policy_unavailable"
        ):
            raise ProductionRegistryUserUnavailable()
        if (
            type(current_grant) is not AuthorizationGrant
            or AuthorizationGrant.model_validate(current_grant).model_dump(mode="python")
            != self._grant.model_dump(mode="python")
            or not self._authority.verify(
                current_grant, self._principal, "user.register", self._resource
            )
        ):
            raise ProductionRegistryUserDenied()
        return CurrentUserRegistrationAuthorization(
            authority_epoch=0,
            policy_digest=self._attestation.authority_policy_digest,
            evidence_digest=_registration_evidence_digest(
                self._attestation.digest(), production_registry_user_command_digest(command)
            ),
        )

    def verify_precommit(
        self,
        command: ProductionRegistryUserCommand,
        evidence: CurrentUserRegistrationAuthorization,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = transaction
        try:
            return evidence == self.current(command, transaction)
        except ProductionRegistryUserUnavailable:
            raise
        except Exception:
            return False


def _validate_config(config: BootstrapAdminConfig) -> None:
    try:
        for value in (config.org_id, config.oidc_provider_id):
            if _REFERENCE.fullmatch(value) is None:
                raise ValueError
        for value in (config.oidc_issuer, config.oidc_audience):
            if not value.strip():
                raise ValueError
        if len(config.authority_policy_digest) != 64 or any(
            character not in "0123456789abcdef" for character in config.authority_policy_digest
        ):
            raise ValueError
    except Exception as error:
        raise BootstrapAdminConfigurationError() from error


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _registration_evidence_digest(attestation_digest: str, command_digest: str) -> str:
    return _digest("bootstrap-admin\x00" + attestation_digest + "\x00" + command_digest)


def _principal_for(attestation: BootstrapAdminAttestation) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id=attestation.org_id,
        subject_id=attestation.registry_user_id,
        identity_provider=attestation.oidc_provider_id,
        identity_session_id=attestation.oidc_subject_digest,
    )


def _resource_for(attestation: BootstrapAdminAttestation) -> ResourceRef:
    return ResourceRef(org_id=attestation.org_id, kind="user", resource_id=attestation.registry_user_id)


def _seal_matches(
    seal: BootstrapAdminSeal, attestation: BootstrapAdminAttestation, command_digest: str
) -> bool:
    return (
        seal.org_id,
        seal.attestation_id,
        seal.attestation_digest,
        seal.registry_user_id,
        seal.registration_command_digest,
        seal.authority_policy_digest,
    ) == (
        attestation.org_id,
        attestation.attestation_id,
        attestation.digest(),
        attestation.registry_user_id,
        command_digest,
        attestation.authority_policy_digest,
    )


def _require_aware(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise BootstrapAdminUnavailable()
    return value


__all__ = [
    "BootstrapAdminApplication",
    "BootstrapAdminAttestation",
    "BootstrapAdminConfig",
    "BootstrapAdminConflict",
    "BootstrapAdminConfigurationError",
    "BootstrapAdminDenied",
    "BootstrapAdminRegistrationAuthorizer",
    "BootstrapAdminResult",
    "BootstrapAdminUnavailable",
    "BootstrapOidcDeviceAuthorizer",
    "BootstrapOidcDeviceDenied",
    "BootstrapOidcDeviceUnavailable",
    "VerifiedBootstrapIdentity",
]
