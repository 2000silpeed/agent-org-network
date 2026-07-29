from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent_org_network.production_bootstrap import ProductionDependencyUnavailable
from agent_org_network.production_source_integration import (
    ExternalSecretHandle,
    ProductionSourceIntegrationProfile,
    _bootstrap_production_source_integration_wiring,  # pyright: ignore[reportPrivateUsage]
)


def _secret_handle() -> ExternalSecretHandle:
    return ExternalSecretHandle(
        secret_ref="vault://production/confluence/token",
        generation=7,
        resolver_identity="secret-manager://primary",
    )


def _profile() -> ProductionSourceIntegrationProfile:
    return ProductionSourceIntegrationProfile(
        registry_identity="postgres://registry-fingerprint/sha256:registry",
        profile_id="confluence-primary",
        profile_version="42",
        profile_digest="a" * 64,
        service_identity="spiffe://aon/production/source-worker",
        secret_handle=_secret_handle(),
        mtls_client_identity_ref="hsm://mtls/source-worker",
        trust_policy_ref="trust://source/confluence/pinned",
        external_contract_digest="b" * 64,
        enforcement_evidence_ref="evidence://gateway/no-bypass/2026-07-20",
        authority_epoch=11,
        approved=True,
    )


def test_external_secret_handle_accepts_only_reference_and_never_serializes_raw_material() -> None:
    handle = _secret_handle()

    assert "token" not in repr(handle)
    assert "token" not in json.dumps(handle.model_dump(mode="json"))
    with pytest.raises(ValidationError):
        ExternalSecretHandle(
            secret_ref="actual-bearer-secret",
            generation=1,
            resolver_identity="secret-manager://primary",
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("registry_identity", ""),
        ("service_identity", ""),
        ("mtls_client_identity_ref", ""),
        ("trust_policy_ref", ""),
        ("external_contract_digest", "not-a-digest"),
        ("enforcement_evidence_ref", ""),
        ("authority_epoch", 0),
        ("approved", False),
    ],
)
def test_missing_or_unapproved_production_profile_shape_is_unavailable(
    field: str, value: object
) -> None:
    values = _profile().model_dump()
    values[field] = value

    with pytest.raises(ValidationError):
        ProductionSourceIntegrationProfile.model_validate(values, strict=True)


def test_fake_profile_and_session_cannot_satisfy_production_capability() -> None:
    wiring = _bootstrap_production_source_integration_wiring(
        profile=_profile(),
        durable_registry_live=True,
        service_identity_live=True,
        secret_resolver_live=True,
        mtls_trust_live=True,
        external_contract_live=True,
        authority_epoch_live=11,
    )

    result = wiring.open_for_bootstrap(object(), profile_id="confluence-primary")

    assert type(result) is ProductionDependencyUnavailable
    assert result.code == "production_adapters_unavailable"


def test_approved_attested_shape_stays_unavailable_without_real_backend() -> None:
    wiring = _bootstrap_production_source_integration_wiring(
        profile=_profile(),
        durable_registry_live=True,
        service_identity_live=True,
        secret_resolver_live=True,
        mtls_trust_live=True,
        external_contract_live=True,
        authority_epoch_live=11,
    )

    assert type(wiring.open_for_bootstrap(object(), profile_id="confluence-primary")) is ProductionDependencyUnavailable
