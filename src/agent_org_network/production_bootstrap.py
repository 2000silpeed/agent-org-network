"""Fail-closed production composition bootstrap contract.

This boundary validates production configuration without importing demo, web, or
runtime-selection modules.  Real production adapters intentionally remain unavailable
until their durable implementations are supplied by a later phase.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from copy import copy
from dataclasses import dataclass, field
from threading import RLock
from typing import Literal, Protocol, Self, TypeAlias, final
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    PrivateAttr,
    SecretStr,
    computed_field,
    field_validator,
)

from agent_org_network.question_surface_composition import (
    QuestionSurfaceComposition,
    _claim_question_surface_production_contract_attestation,  # pyright: ignore[reportPrivateUsage]
)
from agent_org_network.production_authority import ProductionAuthorityCapability
from agent_org_network.trusted_source_binding_authority import (
    SourceBindingAuthorityCapability,
    SourceBindingAuthorityWiring,
)
from agent_org_network.trusted_source_integration import (
    TrustedSourceIntegrationSession,
    TrustedSourceIntegrationWiring,
)


_ENV_FIELDS: tuple[tuple[str, str], ...] = (
    ("AON_PRODUCTION_ORG_ID", "org_id"),
    ("AON_PRODUCTION_DATABASE_DSN", "database_dsn"),
    ("AON_PRODUCTION_OIDC_ISSUER", "oidc_issuer"),
    ("AON_PRODUCTION_OIDC_CLIENT_ID", "oidc_client_id"),
    ("AON_PRODUCTION_OIDC_CLIENT_SECRET", "oidc_client_secret"),
    ("AON_PRODUCTION_SESSION_SECRET", "session_secret"),
    ("AON_PRODUCTION_AUTHORITY_POLICY_REF", "authority_policy_ref"),
    ("AON_PRODUCTION_PROVIDER", "provider"),
    ("AON_PRODUCTION_PROVIDER_CREDENTIAL", "provider_credential"),
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class ProductionBootstrapConfig(_FrozenModel):
    """The complete, validated production bootstrap configuration."""

    org_id: str
    database_dsn: SecretStr
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: SecretStr
    session_secret: SecretStr
    authority_policy_ref: str
    provider: str
    provider_credential: SecretStr

    @field_validator(
        "org_id",
        "oidc_issuer",
        "oidc_client_id",
        "authority_policy_ref",
        "provider",
        mode="after",
    )
    @classmethod
    def _strings_must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("production configuration values must be nonblank")
        return value

    @field_validator(
        "database_dsn",
        "oidc_client_secret",
        "session_secret",
        "provider_credential",
        mode="after",
    )
    @classmethod
    def _secrets_must_be_nonblank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("production secret configuration values must be nonblank")
        return value

    @field_validator("database_dsn", mode="after")
    @classmethod
    def _database_must_be_postgres(cls, value: SecretStr) -> SecretStr:
        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.netloc:
            raise ValueError("production database must use a network PostgreSQL DSN")
        return value

    @field_validator("oidc_issuer", mode="after")
    @classmethod
    def _oidc_issuer_must_be_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.query)
            or bool(parsed.fragment)
        ):
            raise ValueError("production OIDC issuer must use HTTPS")
        return value


_CLEANUP_FAILURE_MESSAGE = "production bootstrap cleanup failed"


class _CleanupSequence:
    """Track two ordered cleanup steps without retaining their exceptions."""

    def __init__(
        self,
        *,
        composition_close: Callable[[], None] | None,
        dependencies_close: Callable[[], None] | None,
    ) -> None:
        self._composition_close = composition_close
        self._dependencies_close = dependencies_close
        self._composition_closed = composition_close is None
        self._dependencies_closed = dependencies_close is None
        self._lock = RLock()

    @property
    def cleanup_pending(self) -> bool:
        with self._lock:
            return not (self._composition_closed and self._dependencies_closed)

    def close(self) -> None:
        failed = False
        with self._lock:
            if not self._composition_closed:
                assert self._composition_close is not None
                try:
                    self._composition_close()
                except Exception:
                    failed = True
                else:
                    self._composition_closed = True
                    self._composition_close = None
            if not self._dependencies_closed:
                assert self._dependencies_close is not None
                try:
                    self._dependencies_close()
                except Exception:
                    failed = True
                else:
                    self._dependencies_closed = True
                    self._dependencies_close = None
        if failed:
            raise RuntimeError(_CLEANUP_FAILURE_MESSAGE)

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        del memo
        return self


class _ProductionBootstrapFailureModel(_FrozenModel):
    _cleanup_sequence: _CleanupSequence | None = PrivateAttr(default=None)

    @computed_field
    @property
    def cleanup_pending(self) -> bool:
        sequence = self._cleanup_sequence
        return sequence is not None and sequence.cleanup_pending

    def close(self) -> None:
        sequence = self._cleanup_sequence
        if sequence is not None:
            sequence.close()

    def with_cleanup_sequence(self, sequence: _CleanupSequence) -> Self:
        self._cleanup_sequence = sequence
        return self


class MissingProductionConfiguration(_ProductionBootstrapFailureModel):
    kind: Literal["missing_production_configuration"] = "missing_production_configuration"
    code: Literal["missing_production_configuration"] = "missing_production_configuration"
    missing_keys: tuple[str, ...]


class InvalidProductionConfiguration(_ProductionBootstrapFailureModel):
    kind: Literal["invalid_production_configuration"] = "invalid_production_configuration"
    code: Literal["invalid_production_configuration"] = "invalid_production_configuration"


class ProductionDependencyUnavailable(_ProductionBootstrapFailureModel):
    kind: Literal["production_adapters_unavailable"] = "production_adapters_unavailable"
    code: Literal["production_adapters_unavailable"] = "production_adapters_unavailable"


class ProductionCompositionRejected(_ProductionBootstrapFailureModel):
    kind: Literal["production_composition_rejected"] = "production_composition_rejected"
    code: Literal["production_composition_rejected"] = "production_composition_rejected"


ProductionBootstrapFailure: TypeAlias = (
    MissingProductionConfiguration
    | InvalidProductionConfiguration
    | ProductionDependencyUnavailable
    | ProductionCompositionRejected
)


class ProductionCompositionFactory(Protocol):
    def __call__(self, *, production_style: bool) -> QuestionSurfaceComposition: ...


@dataclass(frozen=True)
class ProductionDependencies:
    """Externally opened dependencies and their composition factory."""

    composition_factory: ProductionCompositionFactory
    close: Callable[[], None]
    # P17.7 callers may omit this during the transition.  P17.8 callers use
    # ``bootstrap_authorized_production`` below, which makes the capability
    # mandatory rather than silently treating metadata as an authority proof.
    authority_capability: ProductionAuthorityCapability | None = None
    source_binding_wiring: SourceBindingAuthorityWiring | None = None
    trusted_source_integration_wiring: TrustedSourceIntegrationWiring | None = None

    def __post_init__(self) -> None:
        if not callable(self.composition_factory) or not callable(self.close):
            raise TypeError("production dependencies require callable factories and cleanup")


class ProductionDependencyFactory(Protocol):
    def open(
        self,
        config: ProductionBootstrapConfig,
    ) -> ProductionDependencies | ProductionDependencyUnavailable: ...


@dataclass
class ProductionBootstrapHandle:
    """Own a production-style composition and its external dependencies."""

    composition: QuestionSurfaceComposition
    readiness_scope: Literal["composition_contract_only"] = "composition_contract_only"
    authority_capability: ProductionAuthorityCapability | None = None
    source_binding_wiring: SourceBindingAuthorityWiring | None = None
    trusted_source_integration_wiring: TrustedSourceIntegrationWiring | None = None
    _source_binding_closed: bool = field(default=False, init=False, repr=False)
    _authorized_source_integration: bool = field(default=False, init=False, repr=False)
    _cleanup_sequence: _CleanupSequence = field(repr=False, kw_only=True)

    def close(self) -> None:
        """Close composition first, continue cleanup, and retry only failed steps."""
        self._source_binding_closed = True
        self._cleanup_sequence.close()

    def open_source_binding_authority(self) -> SourceBindingAuthorityCapability | None:
        """Available only from a live, successfully authorized bootstrap."""
        wiring, authority = self.source_binding_wiring, self.authority_capability
        if self._source_binding_closed or type(wiring) is not SourceBindingAuthorityWiring or type(authority) is not ProductionAuthorityCapability:
            return None
        return wiring.open_for_bootstrap(self, authority)

    def open_trusted_source_integration(
        self, *, profile_id: str, source_revision: str
    ) -> TrustedSourceIntegrationSession | None:
        """Only the authorized bootstrap can open a profile-bound fake session."""
        wiring = self.trusted_source_integration_wiring
        capability = self.open_source_binding_authority()
        if self._source_binding_closed or type(wiring) is not TrustedSourceIntegrationWiring or capability is None:
            return None
        return wiring.open_for_bootstrap(self, capability, profile_id=profile_id, source_revision=source_revision)


def _reject_composition(
    *,
    dependencies: ProductionDependencies,
) -> ProductionCompositionRejected:
    sequence = _CleanupSequence(
        composition_close=None,
        dependencies_close=dependencies.close,
    )
    try:
        sequence.close()
    except RuntimeError:
        pass
    rejection = ProductionCompositionRejected()
    if sequence.cleanup_pending:
        return rejection.with_cleanup_sequence(sequence)
    return rejection


def load_production_config(
    environ: Mapping[str, str],
) -> ProductionBootstrapConfig | MissingProductionConfiguration | InvalidProductionConfiguration:
    """Load exactly the required environment values without reflecting invalid inputs."""
    missing = tuple(env_key for env_key, _ in _ENV_FIELDS if env_key not in environ)
    if missing:
        return MissingProductionConfiguration(missing_keys=missing)
    values = {field_name: environ[env_key] for env_key, field_name in _ENV_FIELDS}
    try:
        return ProductionBootstrapConfig.model_validate(values, strict=True)
    except Exception:
        return InvalidProductionConfiguration()


def bootstrap_production(
    *,
    environ: Mapping[str, str],
    dependency_factory: ProductionDependencyFactory,
) -> ProductionBootstrapHandle | ProductionBootstrapFailure:
    """Validate, open, and compose a production-style surface fail-closed."""
    config = load_production_config(environ)
    if isinstance(
        config,
        (MissingProductionConfiguration, InvalidProductionConfiguration),
    ):
        return config
    try:
        dependencies = dependency_factory.open(config)
    except Exception:
        return ProductionDependencyUnavailable()
    if type(dependencies) is ProductionDependencyUnavailable:
        return dependencies
    if type(dependencies) is not ProductionDependencies:
        return ProductionDependencyUnavailable()
    try:
        composition = dependencies.composition_factory(production_style=True)
    except Exception:
        return _reject_composition(dependencies=dependencies)
    if not _claim_question_surface_production_contract_attestation(composition):
        return _reject_composition(dependencies=dependencies)
    capability = dependencies.authority_capability
    if capability is not None:
        if type(capability) is not ProductionAuthorityCapability or not capability.claim(
            composition
        ):
            return _reject_composition(dependencies=dependencies)
    cleanup_sequence = _CleanupSequence(
        composition_close=composition.close,
        dependencies_close=dependencies.close,
    )
    return ProductionBootstrapHandle(
        composition=composition,
        authority_capability=capability,
        source_binding_wiring=dependencies.source_binding_wiring,
        trusted_source_integration_wiring=dependencies.trusted_source_integration_wiring,
        _cleanup_sequence=copy(cleanup_sequence),
    )


def bootstrap_authorized_production(
    *,
    environ: Mapping[str, str],
    dependency_factory: ProductionDependencyFactory,
) -> ProductionBootstrapHandle | ProductionBootstrapFailure:
    """P17.8 production composition entry point with mandatory authority proof.

    ``bootstrap_production`` remains the P17.7 contract-only entry point for its
    existing adapter migration tests.  A caller choosing the central-authority
    production profile must use this gate; an absent, forged, or replayed
    capability is a composition rejection before any handle is returned.
    """
    config = load_production_config(environ)
    if isinstance(config, (MissingProductionConfiguration, InvalidProductionConfiguration)):
        return config
    try:
        dependencies = dependency_factory.open(config)
    except Exception:
        return ProductionDependencyUnavailable()
    if type(dependencies) is ProductionDependencyUnavailable:
        return dependencies
    if type(dependencies) is not ProductionDependencies:
        return ProductionDependencyUnavailable()
    capability = dependencies.authority_capability
    if type(
        capability
    ) is not ProductionAuthorityCapability or not capability.matches_configured_org(config.org_id):
        return _reject_composition(dependencies=dependencies)
    # Use the existing validation and cleanup sequence; its capability claim is
    # the final single-use, exact-Question-Surface binding step.
    result = bootstrap_production(
        environ=environ, dependency_factory=_FixedDependenciesFactory(dependencies)
    )
    if type(result) is ProductionBootstrapHandle:
        result._authorized_source_integration = True  # pyright: ignore[reportPrivateUsage]
    return result


@final
class _FixedDependenciesFactory:
    """Prevent a second factory open from substituting dependencies after validation."""

    def __init__(self, dependencies: ProductionDependencies) -> None:
        self._dependencies = dependencies

    def open(self, config: ProductionBootstrapConfig) -> ProductionDependencies:
        del config
        return self._dependencies


class _UnavailableProductionDependencyFactory:
    def open(
        self,
        config: ProductionBootstrapConfig,
    ) -> ProductionDependencyUnavailable:
        del config
        return ProductionDependencyUnavailable()


def check_production_readiness_from_env(
    environ: Mapping[str, str] | None = None,
) -> ProductionBootstrapFailure:
    """Check the actual entry point without claiming unavailable adapters are ready."""
    result = bootstrap_production(
        environ=os.environ if environ is None else environ,
        dependency_factory=_UnavailableProductionDependencyFactory(),
    )
    if type(result) is ProductionBootstrapHandle:
        try:
            result.close()
        except Exception:
            pass
        return ProductionCompositionRejected()
    if isinstance(
        result,
        (
            MissingProductionConfiguration,
            InvalidProductionConfiguration,
            ProductionDependencyUnavailable,
            ProductionCompositionRejected,
        ),
    ):
        return result
    return ProductionCompositionRejected()
