"""ADR 0061 S1c.2b: sealed profile and signed-readback contract.

This is deliberately *not* a BindingPending terminal writer.  It offers the
first trustworthy boundary around a Pending operation: a bootstrap-attested
profile can open a deterministic test transport, which returns a signed,
body-free observation.  Bound and source reads stay unavailable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Mapping, final

from agent_org_network.sqlite_source_binding_worker_v7 import SourceBindingOperationRequest
from agent_org_network.trusted_source_binding_authority import SourceBindingAuthorityCapability


_MINT = secrets.token_urlsafe(32)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


class TrustedSourceIntegrationUnavailable(RuntimeError):
    """The profile is unavailable, revoked, or not bootstrap-attested."""


class SourceConditionalConflict(RuntimeError):
    """A source CAS, semantic-idempotency, or worker-fence precondition failed."""


@dataclass(frozen=True, init=False)
class SourceIntegrationProfile:
    """Immutable central configuration; credential_ref is opaque, never material."""

    profile_id: str
    profile_version: str
    profile_digest: str
    org_id: str
    source_ref: str
    external_target_fingerprint: str
    mtls_client_identity_ref: str
    tls_server_identity: str
    credential_ref: str
    credential_generation: int
    policy_digest: str
    signing_key_id: str

    def __init__(
        self, token: str, *, profile_id: str, profile_version: str, profile_digest: str,
        org_id: str, source_ref: str, external_target_fingerprint: str,
        mtls_client_identity_ref: str, tls_server_identity: str, credential_ref: str,
        credential_generation: int, policy_digest: str, signing_key_id: str,
    ) -> None:
        if not hmac.compare_digest(token, _MINT):
            raise TypeError("bootstrap registry-only integration profile")
        fields = (profile_id, profile_version, profile_digest, org_id, source_ref,
                  external_target_fingerprint, mtls_client_identity_ref,
                  tls_server_identity, credential_ref, policy_digest, signing_key_id)
        if any(type(value) is not str or not value for value in fields) or credential_generation < 1:
            raise ValueError("canonical source integration profile required")
        if not credential_ref.startswith(("vault://", "hsm://")) or not mtls_client_identity_ref.startswith("hsm://"):
            raise ValueError("only opaque credential and mTLS identity references are permitted")
        if len(profile_digest) != 64 or any(ch not in "0123456789abcdef" for ch in profile_digest):
            raise ValueError("profile digest must be sha256")
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "profile_version", profile_version)
        object.__setattr__(self, "profile_digest", profile_digest)
        object.__setattr__(self, "org_id", org_id)
        object.__setattr__(self, "source_ref", source_ref)
        object.__setattr__(self, "external_target_fingerprint", external_target_fingerprint)
        object.__setattr__(self, "mtls_client_identity_ref", mtls_client_identity_ref)
        object.__setattr__(self, "tls_server_identity", tls_server_identity)
        object.__setattr__(self, "credential_ref", credential_ref)
        object.__setattr__(self, "credential_generation", credential_generation)
        object.__setattr__(self, "policy_digest", policy_digest)
        object.__setattr__(self, "signing_key_id", signing_key_id)


@dataclass(frozen=True)
class StableSourceReadback:
    """Signed, body-free exact source observation; it is not a Bound receipt."""

    profile_id: str
    profile_version: str
    profile_digest: str
    source_ref: str
    external_target_fingerprint: str
    expected_source_revision: str
    observed_source_revision: str
    external_generation: int
    semantic_digest: str
    idempotency_key: str
    binding_generation: int
    lease_fence: int
    policy_digest: str
    observed_at: datetime
    key_id: str
    signature: str

    def payload(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id, "profile_version": self.profile_version,
            "profile_digest": self.profile_digest, "source_ref": self.source_ref,
            "external_target_fingerprint": self.external_target_fingerprint,
            "expected_source_revision": self.expected_source_revision,
            "observed_source_revision": self.observed_source_revision,
            "external_generation": self.external_generation,
            "semantic_digest": self.semantic_digest, "idempotency_key": self.idempotency_key,
            "binding_generation": self.binding_generation, "lease_fence": self.lease_fence,
            "policy_digest": self.policy_digest,
            "observed_at": self.observed_at.astimezone(UTC).isoformat(timespec="milliseconds"),
            "key_id": self.key_id,
        }

    @property
    def payload_digest(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True)
class _Operation:
    request_digest: str
    observation: StableSourceReadback


@final
class DeterministicSignedFakeTransport:
    """An in-process source stand-in with source-side CAS/idempotency/fencing.

    It has no database dependency.  Consequently it cannot be invoked while a
    worker transaction is held by this slice's composition: the caller must
    prepare a Pending request, commit, and call this transport afterwards.
    """

    def __init__(self, profile: SourceIntegrationProfile, signing_key: bytes, *, source_revision: str) -> None:
        if type(signing_key) is not bytes or not signing_key or not source_revision:
            raise ValueError("trusted signed fake transport requires source identity")
        self._profile = profile
        self._key = signing_key
        self._source_revision = source_revision
        self._fence = 0
        self._generation = 0
        self._operations: dict[str, _Operation] = {}
        self.calls = 0
        self.fail_after_apply = False

    def _request_payload(self, request: SourceBindingOperationRequest, fence: int) -> dict[str, object]:
        return {"org_id": request.org_id, "source_ref": request.source_ref,
                "semantic_digest": request.semantic_digest, "idempotency_key": request.idempotency_key,
                "expected_source_revision": request.expected_source_revision,
                "policy_digest": request.policy_digest, "boundary_digest": request.boundary_digest,
                "lease_fence": fence}

    def apply(self, request: SourceBindingOperationRequest, lease_fence: int) -> StableSourceReadback:
        profile = self._profile
        if type(request) is not SourceBindingOperationRequest or type(lease_fence) is not int or lease_fence < 1:
            raise SourceConditionalConflict("sealed Pending request and lease fence required")
        if request.org_id != profile.org_id or request.source_ref != profile.source_ref or request.policy_digest != profile.policy_digest:
            raise TrustedSourceIntegrationUnavailable("source/profile/policy mismatch")
        # Rejections above happen at the profile gate, before any source call.
        self.calls += 1
        if request.expected_source_revision != self._source_revision:
            raise SourceConditionalConflict("expected source revision CAS failed")
        payload = self._request_payload(request, lease_fence)
        request_digest = _digest(payload)
        prior = self._operations.get(request.idempotency_key)
        if prior is not None:
            if not hmac.compare_digest(prior.request_digest, request_digest):
                raise SourceConditionalConflict("semantic idempotency conflict")
            return prior.observation
        if lease_fence < self._fence:
            raise SourceConditionalConflict("worker fence conflict")
        self._fence = lease_fence
        self._generation += 1
        observed_at = datetime(2000, 1, 1, tzinfo=UTC)
        unsigned = StableSourceReadback(
            profile.profile_id, profile.profile_version, profile.profile_digest, profile.source_ref,
            profile.external_target_fingerprint, request.expected_source_revision, self._source_revision,
            self._generation, request.semantic_digest, request.idempotency_key, 1, lease_fence,
            request.policy_digest, observed_at, profile.signing_key_id, "",
        )
        signed = StableSourceReadback(
            profile_id=unsigned.profile_id, profile_version=unsigned.profile_version,
            profile_digest=unsigned.profile_digest, source_ref=unsigned.source_ref,
            external_target_fingerprint=unsigned.external_target_fingerprint,
            expected_source_revision=unsigned.expected_source_revision,
            observed_source_revision=unsigned.observed_source_revision,
            external_generation=unsigned.external_generation, semantic_digest=unsigned.semantic_digest,
            idempotency_key=unsigned.idempotency_key, binding_generation=unsigned.binding_generation,
            lease_fence=unsigned.lease_fence, policy_digest=unsigned.policy_digest,
            observed_at=unsigned.observed_at, key_id=unsigned.key_id,
            signature=hmac.new(self._key, _canonical(unsigned.payload()), hashlib.sha256).hexdigest(),
        )
        self._operations[request.idempotency_key] = _Operation(request_digest, signed)
        if self.fail_after_apply:
            raise TimeoutError("controlled late observation")
        return signed

    def read_back(self, request: SourceBindingOperationRequest, lease_fence: int) -> StableSourceReadback:
        # A repeat of the same external generation is the deterministic stability proof.
        return self.apply(request, lease_fence)


@final
class TrustedSourceIntegrationSession:
    def __init__(
        self, token: str, registry: TrustedSourceIntegrationRegistry, profile_id: str,
        transport: DeterministicSignedFakeTransport, capability: SourceBindingAuthorityCapability,
    ) -> None:
        if not hmac.compare_digest(token, _MINT):
            raise TypeError("registry-only integration session")
        if type(capability) is not SourceBindingAuthorityCapability:
            raise TypeError("bootstrap authority capability required")
        self._registry, self._profile_id, self._transport, self._capability, self._closed = (
            registry, profile_id, transport, capability, False
        )

    def close(self) -> None:
        self._closed = True

    def _require_live_binding(self) -> None:
        profile = self._registry._profiles.get(self._profile_id)  # pyright: ignore[reportPrivateUsage]
        if (
            self._closed or profile is None or not self._registry._is_live(self._profile_id)  # pyright: ignore[reportPrivateUsage]
            or not self._capability.is_live() or not self._capability.matches_source_ref(profile.source_ref)
        ):
            raise TrustedSourceIntegrationUnavailable("bootstrap authority/profile binding unavailable")

    def apply_pending(self, request: SourceBindingOperationRequest, lease_fence: int) -> StableSourceReadback:
        self._require_live_binding()
        return self._transport.apply(request, lease_fence)

    def read_back_pending(self, request: SourceBindingOperationRequest, lease_fence: int) -> StableSourceReadback:
        self._require_live_binding()
        return self._transport.read_back(request, lease_fence)


@final
class TrustedPendingExecutor:
    """Sealed Pending-worker adapter for the profile-enabled fake transport.

    It returns only a verified signed observation.  The worker still records an
    observation digest and remains BindingPending; this class has no terminal
    or read-serving operation.
    """
    def __init__(self, token: str, session: TrustedSourceIntegrationSession) -> None:
        if not hmac.compare_digest(token, _MINT) or type(session) is not TrustedSourceIntegrationSession:
            raise TypeError("registry session-only Pending executor")
        self._session = session
        self.last_readback: StableSourceReadback | None = None

    def apply(self, request: SourceBindingOperationRequest, lease_fence: int) -> StableSourceReadback:
        observation = self._session.apply_pending(request, lease_fence)
        repeated = self._session.read_back_pending(request, lease_fence)
        if observation != repeated or not self._session._registry.verify_readback(self._session._profile_id, observation):  # pyright: ignore[reportPrivateUsage]
            raise TrustedSourceIntegrationUnavailable("unstable or unsigned source readback")
        self.last_readback = observation
        return observation


def _open_trusted_pending_executor(session: TrustedSourceIntegrationSession) -> TrustedPendingExecutor:  # pyright: ignore[reportUnusedFunction]
    return TrustedPendingExecutor(_MINT, session)


@final
class TrustedSourceIntegrationRegistry:
    """Bootstrap-owned registry.  Profile changes/revocation require its sealed control."""

    def __init__(self, token: str, profiles: Mapping[str, SourceIntegrationProfile], signing_keys: Mapping[str, bytes]) -> None:
        if not hmac.compare_digest(token, _MINT):
            raise TypeError("bootstrap-only trusted integration registry")
        if not profiles or set(profiles) != {profile.profile_id for profile in profiles.values()}:
            raise ValueError("canonical profile registry required")
        if any(type(profile) is not SourceIntegrationProfile for profile in profiles.values()):
            raise ValueError("arbitrary profile rejected")
        if any(type(key) is not str or type(value) is not bytes or not value for key, value in signing_keys.items()):
            raise ValueError("canonical signing keys required")
        if any(profile.signing_key_id not in signing_keys for profile in profiles.values()):
            raise ValueError("profile signing key unavailable")
        self._profiles, self._keys, self._revoked = dict(profiles), dict(signing_keys), set[str]()

    def _is_live(self, profile_id: str) -> bool:
        return profile_id in self._profiles and profile_id not in self._revoked

    def open(self, profile_id: str, capability: SourceBindingAuthorityCapability, *, source_revision: str) -> TrustedSourceIntegrationSession:
        if type(capability) is not SourceBindingAuthorityCapability or not capability.is_live() or not self._is_live(profile_id):
            raise TrustedSourceIntegrationUnavailable("bootstrap capability/profile unavailable")
        profile = self._profiles[profile_id]
        if not capability.matches_source_ref(profile.source_ref):
            raise TrustedSourceIntegrationUnavailable("profile source is not bootstrap-attested")
        return TrustedSourceIntegrationSession(
            _MINT, self, profile_id,
            DeterministicSignedFakeTransport(profile, self._keys[profile.signing_key_id], source_revision=source_revision),
            capability,
        )

    def verify_readback(self, profile_id: str, readback: StableSourceReadback) -> bool:
        if not self._is_live(profile_id) or type(readback) is not StableSourceReadback:
            return False
        profile = self._profiles[profile_id]
        if (readback.profile_id, readback.profile_version, readback.profile_digest, readback.source_ref,
            readback.external_target_fingerprint, readback.key_id) != (profile.profile_id, profile.profile_version,
            profile.profile_digest, profile.source_ref, profile.external_target_fingerprint, profile.signing_key_id):
            return False
        key = self._keys.get(readback.key_id)
        return key is not None and hmac.compare_digest(readback.signature, hmac.new(key, _canonical(readback.payload()), hashlib.sha256).hexdigest())

    def _revoke(self, token: str, profile_id: str) -> None:
        if not hmac.compare_digest(token, _MINT) or profile_id not in self._profiles:
            raise TrustedSourceIntegrationUnavailable("profile revoke denied")
        self._revoked.add(profile_id)


@final
class TrustedSourceIntegrationWiring:
    """Sealed bootstrap wiring; this is the only production session opener."""

    def __init__(self, token: str, registry: TrustedSourceIntegrationRegistry) -> None:
        if not hmac.compare_digest(token, _MINT) or type(registry) is not TrustedSourceIntegrationRegistry:
            raise TypeError("production bootstrap-only trusted integration wiring")
        self._registry = registry

    def open_for_bootstrap(
        self, handle: object, capability: SourceBindingAuthorityCapability, *, profile_id: str,
        source_revision: str,
    ) -> TrustedSourceIntegrationSession | None:
        if not bool(getattr(handle, "_authorized_source_integration", False)):
            return None
        if getattr(handle, "trusted_source_integration_wiring", None) is not self:
            return None
        try:
            return self._registry.open(profile_id, capability, source_revision=source_revision)
        except (TrustedSourceIntegrationUnavailable, ValueError):
            return None

    def _revoke_for_bootstrap(self, token: str, profile_id: str) -> None:
        self._registry._revoke(token, profile_id)  # pyright: ignore[reportPrivateUsage]


@final
class TrustedSourceIntegrationControl:
    """Private bootstrap control: only it may revoke a profile."""
    def __init__(self, token: str, registry: TrustedSourceIntegrationRegistry) -> None:
        if not hmac.compare_digest(token, _MINT):
            raise TypeError("bootstrap-only integration control")
        self._registry = registry
    def revoke(self, profile_id: str) -> None:
        self._registry._revoke(_MINT, profile_id)  # pyright: ignore[reportPrivateUsage]


def _bootstrap_trusted_source_integration_registry(  # pyright: ignore[reportUnusedFunction]
    *, profiles: Mapping[str, SourceIntegrationProfile], signing_keys: Mapping[str, bytes]
) -> tuple[TrustedSourceIntegrationRegistry, TrustedSourceIntegrationControl]:
    registry = TrustedSourceIntegrationRegistry(_MINT, profiles, signing_keys)
    return registry, TrustedSourceIntegrationControl(_MINT, registry)


def _bootstrap_trusted_source_integration_wiring(  # pyright: ignore[reportUnusedFunction]
    *, profiles: Mapping[str, SourceIntegrationProfile], signing_keys: Mapping[str, bytes]
) -> TrustedSourceIntegrationWiring:
    registry, _control = _bootstrap_trusted_source_integration_registry(profiles=profiles, signing_keys=signing_keys)
    return TrustedSourceIntegrationWiring(_MINT, registry)


def _bootstrap_source_integration_profile(**values: object) -> SourceIntegrationProfile:  # pyright: ignore[reportUnusedFunction]
    """Trusted bootstrap helper; values contain references only, never credential material."""
    return SourceIntegrationProfile(_MINT, **values)  # type: ignore[arg-type]
