from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from agent_org_network.question_surface_composition import QuestionSurfaceComposition
from agent_org_network.reciprocal_review import SourceBindingAuthorizationEnvelopeV7
from agent_org_network.sqlite_source_binding_worker_v7 import SourceBindingOperationRequest
from agent_org_network.production_bootstrap import (
    ProductionBootstrapHandle,
    ProductionDependencies,
    bootstrap_authorized_production,
    bootstrap_production,
)
from agent_org_network.trusted_source_binding_authority import (
    _bootstrap_source_binding_wiring,  # pyright: ignore[reportPrivateUsage]
)
from agent_org_network.trusted_source_integration import (
    DeterministicSignedFakeTransport,
    SourceConditionalConflict,
    SourceIntegrationProfile,
    TrustedSourceIntegrationRegistry,
    TrustedSourceIntegrationUnavailable,
    _bootstrap_source_integration_profile,  # pyright: ignore[reportPrivateUsage]
    _bootstrap_trusted_source_integration_registry,  # pyright: ignore[reportPrivateUsage]
    _bootstrap_trusted_source_integration_wiring,  # pyright: ignore[reportPrivateUsage]
)
import test_production_authority as production_authority


def _profile() -> object:
    return _bootstrap_source_integration_profile(
        profile_id="confluence-prod", profile_version="1", profile_digest="a" * 64,
        org_id="org", source_ref="source", external_target_fingerprint="target-sha256",
        mtls_client_identity_ref="hsm://mtls/client-1", tls_server_identity="source.example",
        credential_ref="vault://opaque/rotation-7", credential_generation=7,
        policy_digest="policy", signing_key_id="source-observer-1",
    )


def _request(
    *, org_id: str = "org", source_ref: str = "source", expected_source_revision: str = "r1",
    policy_digest: str = "policy", key: str = "delivery",
) -> SourceBindingOperationRequest:
    # The real Pending worker is the only production mint.  The object is made
    # directly here solely to exercise this isolated adapter contract.
    request = object.__new__(SourceBindingOperationRequest)
    object.__setattr__(request, "org_id", org_id)
    object.__setattr__(request, "intent_id", "pending")
    object.__setattr__(request, "source_ref", source_ref)
    object.__setattr__(request, "semantic_digest", "intent-digest")
    object.__setattr__(request, "idempotency_key", key)
    object.__setattr__(request, "expected_source_revision", expected_source_revision)
    object.__setattr__(request, "policy_digest", policy_digest)
    object.__setattr__(request, "boundary_digest", "boundary")
    object.__setattr__(request, "verified_authorization", object())
    return request


def test_registry_rejects_arbitrary_profile_and_profile_constructor_is_sealed() -> None:
    with pytest.raises(TypeError):
        TrustedSourceIntegrationRegistry("caller", {}, {})
    with pytest.raises(TypeError):
        SourceIntegrationProfile(
            "caller",
            profile_id="confluence-prod", profile_version="1", profile_digest="a" * 64,
            org_id="org", source_ref="source", external_target_fingerprint="target-sha256",
            mtls_client_identity_ref="hsm://mtls/client-1", tls_server_identity="source.example",
            credential_ref="vault://opaque/rotation-7", credential_generation=7,
            policy_digest="policy", signing_key_id="source-observer-1",
        )


def test_profile_never_accepts_credential_material() -> None:
    values = dict(_profile().__dict__)
    values["credential_ref"] = "actual-bearer-secret"
    with pytest.raises(ValueError, match="opaque credential"):
        _bootstrap_source_integration_profile(**values)


def test_signed_fake_enforces_cas_semantic_idempotency_and_fence_and_is_stable() -> None:
    profile = _profile()
    assert hasattr(profile, "credential_ref")
    transport = DeterministicSignedFakeTransport(profile, b"observer-key", source_revision="r1")  # type: ignore[arg-type]
    first = transport.apply(_request(), 3)
    assert transport.apply(_request(), 3) == first
    assert transport.read_back(_request(), 3) == first
    with pytest.raises(SourceConditionalConflict):
        transport.apply(_request(key="delivery-2"), 2)
    with pytest.raises(SourceConditionalConflict):
        transport.apply(_request(expected_source_revision="old"), 4)
    with pytest.raises(TrustedSourceIntegrationUnavailable):
        transport.apply(_request(policy_digest="other"), 4)


def test_readback_signature_tamper_and_profile_revoke_fail_closed() -> None:
    profile = _profile()
    registry, control = _bootstrap_trusted_source_integration_registry(
        profiles={"confluence-prod": profile}, signing_keys={"source-observer-1": b"observer-key"}  # type: ignore[dict-item]
    )
    transport = DeterministicSignedFakeTransport(profile, b"observer-key", source_revision="r1")  # type: ignore[arg-type]
    observation = transport.apply(_request(), 1)
    assert registry.verify_readback("confluence-prod", observation)
    assert not registry.verify_readback("confluence-prod", replace(observation, observed_source_revision="tampered"))
    control.revoke("confluence-prod")
    assert not registry.verify_readback("confluence-prod", observation)
    with pytest.raises(TrustedSourceIntegrationUnavailable):
        registry.open("confluence-prod", object(), source_revision="r1")  # type: ignore[arg-type]


def test_timeout_after_external_effect_is_not_a_terminal_proof() -> None:
    profile = _profile()
    transport = DeterministicSignedFakeTransport(profile, b"observer-key", source_revision="r1")  # type: ignore[arg-type]
    transport.fail_after_apply = True
    with pytest.raises(TimeoutError):
        transport.apply(_request(), 1)
    transport.fail_after_apply = False
    # The later exact readback proves only the same source observation.  This
    # slice has no Bound writer, so the Pending aggregate cannot be terminalized.
    assert transport.read_back(_request(), 1).observed_source_revision == "r1"


def test_only_authorized_live_bootstrap_opens_profile_and_close_or_source_mismatch_is_zero_call(
    tmp_path: Path,
) -> None:
    profile = _bootstrap_source_integration_profile(
        profile_id="profile", profile_version="1", profile_digest="b" * 64,
        org_id="acme", source_ref="source:trusted", external_target_fingerprint="target",
        mtls_client_identity_ref="hsm://client", tls_server_identity="source.example",
        credential_ref="vault://opaque/7", credential_generation=7, policy_digest="policy",
        signing_key_id="signer",
    )
    integration_wiring = _bootstrap_trusted_source_integration_wiring(
        profiles={"profile": profile}, signing_keys={"signer": b"key"}  # type: ignore[dict-item]
    )
    capability = production_authority._capability()  # pyright: ignore[reportPrivateUsage]
    surface = capability._question_surface  # pyright: ignore[reportPrivateUsage]
    target = tmp_path / "binding.sqlite"

    def _resolver(
        envelope: SourceBindingAuthorizationEnvelopeV7, purpose: str, now: datetime
    ) -> bool:
        return True

    binding_wiring = _bootstrap_source_binding_wiring(
        issuer_registry={"issuer": b"issuer-key"}, resolver=_resolver,
        database_wiring=target, source_wiring="source:trusted",
    )

    def _composition_factory(*, production_style: bool) -> QuestionSurfaceComposition:
        return surface

    dependencies = ProductionDependencies(
        composition_factory=_composition_factory,
        close=lambda: None, authority_capability=capability, source_binding_wiring=binding_wiring,
        trusted_source_integration_wiring=integration_wiring,
    )
    ordinary = bootstrap_production(environ=production_authority._environ(), dependency_factory=production_authority._Factory(dependencies))  # pyright: ignore[reportPrivateUsage]
    assert type(ordinary) is ProductionBootstrapHandle
    assert ordinary.open_trusted_source_integration(profile_id="profile", source_revision="r1") is None
    ordinary.close()

    # A new single-use authority/capability is required for the authorized bootstrap.
    capability = production_authority._capability()  # pyright: ignore[reportPrivateUsage]
    surface = capability._question_surface  # pyright: ignore[reportPrivateUsage]

    def _authorized_composition_factory(*, production_style: bool) -> QuestionSurfaceComposition:
        return surface

    authorized = bootstrap_authorized_production(
        environ=production_authority._environ(),  # pyright: ignore[reportPrivateUsage]
        dependency_factory=production_authority._Factory(  # pyright: ignore[reportPrivateUsage]
            ProductionDependencies(
                composition_factory=_authorized_composition_factory, close=lambda: None,
                authority_capability=capability, source_binding_wiring=binding_wiring,
                trusted_source_integration_wiring=integration_wiring,
            )
        ),
    )
    assert type(authorized) is ProductionBootstrapHandle
    session = authorized.open_trusted_source_integration(profile_id="profile", source_revision="r1")
    assert session is not None
    transport = session._transport  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(TrustedSourceIntegrationUnavailable):
        session.apply_pending(_request(org_id="acme", source_ref="foreign"), 1)
    assert transport.calls == 0
    # A source-wiring substitution after the session is open is a live binding
    # failure, not merely a denial of future opens.
    binding_wiring._source_wiring = "foreign"  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(TrustedSourceIntegrationUnavailable):
        session.apply_pending(_request(org_id="acme", source_ref="source:trusted"), 1)
    assert transport.calls == 0
    binding_wiring._source_wiring = "source:trusted"  # pyright: ignore[reportPrivateUsage]
    capability._state = "revoked"  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(TrustedSourceIntegrationUnavailable):
        session.read_back_pending(_request(org_id="acme", source_ref="source:trusted"), 1)
    assert transport.calls == 0
    # Session has not touched the fake source when a profile/source gate is denied.
    assert authorized.open_trusted_source_integration(profile_id="unknown", source_revision="r1") is None
    authorized.close()
    with pytest.raises(TrustedSourceIntegrationUnavailable):
        session.apply_pending(_request(org_id="acme", source_ref="source:trusted"), 1)
    assert transport.calls == 0
    assert authorized.open_trusted_source_integration(profile_id="profile", source_revision="r1") is None
