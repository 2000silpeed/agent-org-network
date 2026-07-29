"""ADR 0063 production-source enablement boundary.

This module deliberately models only the centrally approved, secret-free
configuration shape.  A real PostgreSQL registry, use-only secret resolver,
mTLS session, and external contract adapter are not present in this process;
therefore opening a production integration is always fail-closed.  In
particular, the ADR 0061 deterministic transport cannot be used here.
"""
from __future__ import annotations

import re
from typing import final
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from agent_org_network.production_bootstrap import ProductionDependencyUnavailable


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class ExternalSecretHandle(_FrozenModel):
    """A resolver reference, never credential material.

    ``SecretStr`` ensures that an otherwise valid reference is also redacted
    from reprs and JSON diagnostics.  It is intentionally not an accessor for
    a raw secret.
    """

    secret_ref: str = Field(repr=False)
    generation: int
    resolver_identity: str

    @field_validator("secret_ref", mode="after")
    @classmethod
    def _must_be_use_only_reference(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"vault", "hsm"}
            or not parsed.netloc
            or not parsed.path.strip("/")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("external secret handle must be an opaque vault or HSM reference")
        return value

    @field_serializer("secret_ref", when_used="json")
    def _redact_secret_reference(self, value: str) -> str:
        del value
        return "<external-secret-handle>"

    @field_validator("generation", mode="after")
    @classmethod
    def _generation_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("secret-handle generation must be positive")
        return value

    @field_validator("resolver_identity", mode="after")
    @classmethod
    def _resolver_must_be_identity_reference(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "secret-manager" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("secret resolver identity reference required")
        return value


class ProductionSourceIntegrationProfile(_FrozenModel):
    """Approved central profile metadata, with no credential or source body."""

    registry_identity: str
    profile_id: str
    profile_version: str
    profile_digest: str
    service_identity: str
    secret_handle: ExternalSecretHandle
    mtls_client_identity_ref: str
    trust_policy_ref: str
    external_contract_digest: str
    enforcement_evidence_ref: str
    authority_epoch: int
    approved: bool

    @field_validator(
        "profile_id",
        "profile_version",
        "enforcement_evidence_ref",
        mode="after",
    )
    @classmethod
    def _must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("production profile reference must be nonblank")
        return value

    @field_validator("registry_identity", mode="after")
    @classmethod
    def _registry_must_be_postgres_identity(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.netloc:
            raise ValueError("durable PostgreSQL registry identity required")
        return value

    @field_validator("service_identity", mode="after")
    @classmethod
    def _service_identity_must_be_spiffe(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "spiffe" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("workload SPIFFE identity required")
        return value

    @field_validator("mtls_client_identity_ref", mode="after")
    @classmethod
    def _mtls_identity_must_be_hsm_reference(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "hsm" or not parsed.netloc or not parsed.path.strip("/"):
            raise ValueError("mTLS client identity HSM reference required")
        return value

    @field_validator("trust_policy_ref", mode="after")
    @classmethod
    def _trust_policy_must_be_reference(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "trust" or not parsed.netloc or not parsed.path.strip("/"):
            raise ValueError("server trust policy reference required")
        return value

    @field_validator("profile_digest", "external_contract_digest", mode="after")
    @classmethod
    def _must_be_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("SHA-256 digest required")
        return value

    @field_validator("authority_epoch", mode="after")
    @classmethod
    def _authority_epoch_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("authority epoch must be positive")
        return value

    @field_validator("approved", mode="after")
    @classmethod
    def _must_be_explicitly_approved(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("production profile must be approved")
        return value


@final
class ProductionSourceIntegrationWiring:
    """Bootstrap-owned configuration gate for the future real adapter.

    The booleans are attestations from a composition root, not a replacement
    for the required real dependencies.  They are retained so the eventual
    registry implementation has one canonical gate, but cannot mint an
    adapter capability in this in-process implementation.
    """

    def __init__(
        self,
        *,
        profile: ProductionSourceIntegrationProfile,
        durable_registry_live: bool,
        service_identity_live: bool,
        secret_resolver_live: bool,
        mtls_trust_live: bool,
        external_contract_live: bool,
        authority_epoch_live: int,
    ) -> None:
        if type(profile) is not ProductionSourceIntegrationProfile:
            raise TypeError("approved production profile required")
        if any(
            type(value) is not bool
            for value in (
                durable_registry_live,
                service_identity_live,
                secret_resolver_live,
                mtls_trust_live,
                external_contract_live,
            )
        ) or type(authority_epoch_live) is not int:
            raise TypeError("production dependency attestations have invalid types")
        self._profile = profile
        self._prerequisites_live = (
            durable_registry_live
            and service_identity_live
            and secret_resolver_live
            and mtls_trust_live
            and external_contract_live
            and authority_epoch_live == profile.authority_epoch
        )

    def open_for_bootstrap(
        self, handle: object, *, profile_id: str
    ) -> ProductionDependencyUnavailable:
        """Fail closed until a real, separately deployed backend is supplied."""
        del handle, profile_id
        # Do not let correct-looking profile DTOs or fake sessions become a
        # production capability.  ADR 0063 requires live external proofs.
        return ProductionDependencyUnavailable()


def _bootstrap_production_source_integration_wiring(  # pyright: ignore[reportUnusedFunction]
    *,
    profile: ProductionSourceIntegrationProfile,
    durable_registry_live: bool,
    service_identity_live: bool,
    secret_resolver_live: bool,
    mtls_trust_live: bool,
    external_contract_live: bool,
    authority_epoch_live: int,
) -> ProductionSourceIntegrationWiring:
    """Composition-root helper; it does not enable a real source adapter."""
    return ProductionSourceIntegrationWiring(
        profile=profile,
        durable_registry_live=durable_registry_live,
        service_identity_live=service_identity_live,
        secret_resolver_live=secret_resolver_live,
        mtls_trust_live=mtls_trust_live,
        external_contract_live=external_contract_live,
        authority_epoch_live=authority_epoch_live,
    )
