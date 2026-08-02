"""Crash-resumable Owner pair/redeem orchestration."""

from __future__ import annotations

from base64 import urlsafe_b64decode
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol, cast

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_der_private_key
from pydantic import SecretBytes

from agent_org_network.central_owner_pairing_client import (
    CentralOwnerPairingUnavailable,
    OwnerPairingCode,
    RedeemedOwnerPairing,
)
from agent_org_network.owner_credential_envelope import (
    OwnerCredentialEnvelopeUnavailable,
    credential_aad_digest,
    decrypt_owner_credential,
    serialize_owner_credential_envelope,
)
from agent_org_network.owner_device_key_store import (
    OwnerCredentialSlotV1,
    OwnerDeviceKeyMaterialV1,
    OwnerDeviceKeyStoreConflict,
    OwnerDeviceKeyStoreUnavailable,
    OwnerInstallationPublicBindingV1,
    OwnerPairingPendingV1,
    binding_digest,
    bundle_public_digest,
    credential_public_digest,
    envelope_digest,
    owner_profile_id,
)
from agent_org_network.owner_pairing_digest import (
    owner_pairing_redeem_request_digest,
)
from agent_org_network.owner_pairing_recovery_store import (
    CreateIntentRecovery,
    FinalizeFromStoredCredentialCommand,
    MarkCredentialStored,
    MarkRedeemSubmitted,
    OwnerPairingRecoveryResult,
    OwnerPairingRecoverySnapshot,
    OwnerPairingRecoveryStore,
    OwnerPairingRecoveryUnavailable,
)
from agent_org_network.production_owner_device_key_store import (
    ProductionOwnerDeviceKeyStore,
)


class OwnerPairingOrchestrationUnavailable(Exception):
    pass


class OwnerPairingVerifier(Protocol):
    def redeem(
        self,
        code: OwnerPairingCode,
        *,
        device_public_key: object,
        idempotency_key: str,
    ) -> RedeemedOwnerPairing: ...


def _utc_second(value: datetime) -> datetime:
    if (
        value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.microsecond != 0
    ):
        raise OwnerPairingOrchestrationUnavailable()
    return value.astimezone(UTC)


def _next_after(clock: Callable[[], datetime], previous: datetime) -> datetime:
    now = _utc_second(clock())
    return now if now > previous else previous + timedelta(seconds=1)


def _credential_secret_bytes(value: str) -> bytes:
    try:
        decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as error:
        raise OwnerPairingOrchestrationUnavailable() from error
    if len(decoded) != 32:
        raise OwnerPairingOrchestrationUnavailable()
    return decoded


def _derived_ref(prefix: str, value: str) -> str:
    return prefix + sha256(value.encode("utf-8")).hexdigest()


def _private_key(device: OwnerDeviceKeyMaterialV1) -> X25519PrivateKey:
    try:
        key = load_der_private_key(
            device.private_key_pkcs8_der.get_secret_value(), password=None
        )
    except (TypeError, ValueError) as error:
        raise OwnerPairingOrchestrationUnavailable() from error
    if not isinstance(key, X25519PrivateKey):
        raise OwnerPairingOrchestrationUnavailable()
    return key


class OwnerPairingOrchestrator:
    def __init__(
        self,
        *,
        verifier: OwnerPairingVerifier,
        device_keys: ProductionOwnerDeviceKeyStore,
        recovery: OwnerPairingRecoveryStore,
        clock: Callable[[], datetime],
    ) -> None:
        if (
            not callable(getattr(verifier, "redeem", None))
            or type(device_keys) is not ProductionOwnerDeviceKeyStore
            or type(recovery) is not OwnerPairingRecoveryStore
            or not callable(clock)
        ):
            raise OwnerPairingOrchestrationUnavailable()
        self._verifier = verifier
        self._device_keys = device_keys
        self._recovery = recovery
        self._clock = clock

    def pair(
        self,
        *,
        binding: OwnerInstallationPublicBindingV1,
        device: OwnerDeviceKeyMaterialV1,
        pairing_code: OwnerPairingCode,
        pairing_intent_digest: str,
        issue_receipt_id: str,
        issue_receipt_digest: str,
        pairing_expires_at: datetime,
        redeem_idempotency_key: str,
    ) -> OwnerPairingRecoveryResult:
        if (
            type(binding) is not OwnerInstallationPublicBindingV1
            or type(device) is not OwnerDeviceKeyMaterialV1
            or type(pairing_code) is not OwnerPairingCode
            or device.device_key_thumbprint != binding.device_key_thumbprint
        ):
            raise OwnerPairingOrchestrationUnavailable()
        now = _utc_second(self._clock())
        expires_at = _utc_second(pairing_expires_at)
        if expires_at <= now or pairing_code.intent_id == "":
            raise OwnerPairingOrchestrationUnavailable()
        profile_id = owner_profile_id(binding)
        redeem_digest = owner_pairing_redeem_request_digest(
            intent_id=pairing_code.intent_id,
            idempotency_key=redeem_idempotency_key,
            device_key_thumbprint=binding.device_key_thumbprint,
        )
        try:
            bundle = self._device_keys.load(profile_id)
            if bundle is None:
                pending = OwnerPairingPendingV1(
                    pairing_intent_id=pairing_code.intent_id,
                    pairing_intent_digest=pairing_intent_digest,
                    issue_receipt_id=issue_receipt_id,
                    issue_receipt_digest=issue_receipt_digest,
                    redeem_idempotency_key=redeem_idempotency_key,
                    redeem_command_digest=redeem_digest,
                    pairing_expires_at=expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
                self._device_keys.create_pending(
                    profile_id, binding=binding, device=device, pairing_pending=pending
                )
                bundle = self._device_keys.load(profile_id)
            if bundle is None or bundle.binding != binding or bundle.pairing_pending is None:
                raise OwnerPairingOrchestrationUnavailable()
            pending = bundle.pairing_pending
            if (
                pending.pairing_intent_id != pairing_code.intent_id
                or pending.pairing_intent_digest != pairing_intent_digest
                or pending.issue_receipt_id != issue_receipt_id
                or pending.issue_receipt_digest != issue_receipt_digest
                or pending.redeem_idempotency_key != redeem_idempotency_key
                or pending.redeem_command_digest != redeem_digest
                or pending.pairing_expires_at != expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            ):
                raise OwnerPairingOrchestrationUnavailable()

            snapshot = self._recovery.read_snapshot(profile_id)
            if snapshot is None:
                self._recovery.create_intent_recovery(
                    CreateIntentRecovery(
                        profile_id=profile_id,
                        now=now,
                        binding=binding,
                        pairing_intent_id=pairing_code.intent_id,
                        pairing_intent_digest=pairing_intent_digest,
                        issue_receipt_id=issue_receipt_id,
                        issue_receipt_digest=issue_receipt_digest,
                        pairing_expires_at=expires_at,
                        idempotency_key=_derived_ref("intent-", pairing_code.intent_id),
                    )
                )
                snapshot = self._recovery.read_snapshot(profile_id)
            if snapshot is None:
                raise OwnerPairingOrchestrationUnavailable()
            self._validate_snapshot(
                snapshot,
                profile_id=profile_id,
                binding=binding,
                pairing_intent_id=pairing_code.intent_id,
                pairing_intent_digest=pairing_intent_digest,
                issue_receipt_id=issue_receipt_id,
                issue_receipt_digest=issue_receipt_digest,
                pairing_expires_at=expires_at,
                redeem_idempotency_key=redeem_idempotency_key,
                redeem_digest=redeem_digest,
            )
            if snapshot.state == "intent_issued":
                submitted_at = _next_after(self._clock, snapshot.updated_at)
                self._recovery.mark_redeem_submitted(
                    MarkRedeemSubmitted(
                        profile_id=profile_id,
                        expected_state="intent_issued",
                        expected_updated_at=snapshot.updated_at,
                        redeem_idempotency_key=redeem_idempotency_key,
                        redeem_command_digest=redeem_digest,
                        now=submitted_at,
                        idempotency_key=_derived_ref("redeem-submit-", redeem_idempotency_key),
                    )
                )
                snapshot = self._recovery.read_snapshot(profile_id)
            if snapshot is None or snapshot.state not in {"redeem_submitted", "credential_stored"}:
                raise OwnerPairingOrchestrationUnavailable()
            self._validate_snapshot(
                snapshot,
                profile_id=profile_id,
                binding=binding,
                pairing_intent_id=pairing_code.intent_id,
                pairing_intent_digest=pairing_intent_digest,
                issue_receipt_id=issue_receipt_id,
                issue_receipt_digest=issue_receipt_digest,
                pairing_expires_at=expires_at,
                redeem_idempotency_key=redeem_idempotency_key,
                redeem_digest=redeem_digest,
            )

            if snapshot.state == "credential_stored":
                return self._finalize(profile_id, binding, snapshot)

            redeemed = self._verifier.redeem(
                pairing_code,
                device_public_key=bundle.device.public_key,
                idempotency_key=redeem_idempotency_key,
            )
            self._validate_redeemed(
                redeemed,
                binding,
                pairing_intent_digest,
                issue_receipt_id,
                issue_receipt_digest,
                redeem_digest,
                redeem_idempotency_key,
            )
            slot = self._slot(redeemed, bundle)
            current = self._device_keys.load(profile_id)
            if current is None:
                raise OwnerPairingOrchestrationUnavailable()
            active = current.active
            if active is None:
                try:
                    self._device_keys.store_active(
                        profile_id,
                        expected_revision=current.bundle_revision,
                        slot=slot,
                        keep_pairing_pending=True,
                    )
                except OwnerDeviceKeyStoreConflict:
                    winner = self._device_keys.load(profile_id)
                    if (
                        winner is None
                        or winner.active is None
                        or credential_public_digest(winner.active)
                        != credential_public_digest(slot)
                    ):
                        raise
            elif credential_public_digest(active) != credential_public_digest(slot):
                raise OwnerPairingOrchestrationUnavailable()
            stored = self._device_keys.load(profile_id)
            if stored is None or stored.active is None:
                raise OwnerPairingOrchestrationUnavailable()
            stored_public_digest = credential_public_digest(stored.active)
            snapshot = self._recovery.read_snapshot(profile_id)
            if snapshot is None:
                raise OwnerPairingOrchestrationUnavailable()
            if snapshot.state == "redeem_submitted":
                stored_at = _next_after(self._clock, snapshot.updated_at)
                self._recovery.mark_credential_stored(
                    MarkCredentialStored(
                        profile_id=profile_id,
                        expected_state="redeem_submitted",
                        expected_updated_at=snapshot.updated_at,
                        credential_id=stored.active.credential_id,
                        credential_generation=stored.active.credential_generation,
                        credential_public_digest=stored_public_digest,
                        bundle_revision=stored.bundle_revision,
                        bundle_public_digest=bundle_public_digest(stored),
                        now=stored_at,
                        idempotency_key=_derived_ref("credential-stored-", redeem_idempotency_key),
                    )
                )
                snapshot = self._recovery.read_snapshot(profile_id)
            if snapshot is None or snapshot.state != "credential_stored":
                raise OwnerPairingOrchestrationUnavailable()
            self._validate_snapshot(
                snapshot,
                profile_id=profile_id,
                binding=binding,
                pairing_intent_id=pairing_code.intent_id,
                pairing_intent_digest=pairing_intent_digest,
                issue_receipt_id=issue_receipt_id,
                issue_receipt_digest=issue_receipt_digest,
                pairing_expires_at=expires_at,
                redeem_idempotency_key=redeem_idempotency_key,
                redeem_digest=redeem_digest,
            )
            return self._finalize(profile_id, binding, snapshot)
        except OwnerPairingOrchestrationUnavailable:
            raise
        except (
            CentralOwnerPairingUnavailable,
            OwnerCredentialEnvelopeUnavailable,
            OwnerDeviceKeyStoreConflict,
            OwnerDeviceKeyStoreUnavailable,
            OwnerPairingRecoveryUnavailable,
            ValueError,
            TypeError,
        ) as error:
            raise OwnerPairingOrchestrationUnavailable() from error

    def _validate_redeemed(
        self,
        value: RedeemedOwnerPairing,
        binding: OwnerInstallationPublicBindingV1,
        pairing_intent_digest: str,
        issue_receipt_id: str,
        issue_receipt_digest: str,
        redeem_digest: str,
        redeem_idempotency_key: str,
    ) -> None:
        if (
            type(value) is not RedeemedOwnerPairing
            or value.org_id != binding.org_id
            or value.owner_id != binding.owner_user_id
            or value.agent_id != binding.agent_card_id
            or value.card_revision != binding.agent_card_revision
            or value.card_digest != binding.agent_card_digest
            or value.device_key_thumbprint != binding.device_key_thumbprint
            or value.credential_generation != 1
            or value.envelope.aad.scope != ("author.read", "author.write")
            or value.envelope.aad.audience != "owner-install"
            or value.envelope.aad.expires_at != value.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            or value.expires_at <= _utc_second(self._clock())
            or value.pairing_intent_digest != pairing_intent_digest
            or value.issue_receipt_id != issue_receipt_id
            or value.issue_receipt_digest != issue_receipt_digest
            or value.redeem_request_digest != redeem_digest
            or value.redeem_receipt_id != redeem_idempotency_key
            or value.envelope.aad != value.envelope.aad.model_copy(
                update={
                    "org_id": binding.org_id,
                    "owner_user_id": binding.owner_user_id,
                    "agent_card_id": binding.agent_card_id,
                    "device_key_thumbprint": binding.device_key_thumbprint,
                    "credential_generation": 1,
                    "credential_id": value.credential_id,
                }
            )
        ):
            raise OwnerPairingOrchestrationUnavailable()

    @staticmethod
    def _validate_snapshot(
        snapshot: OwnerPairingRecoverySnapshot,
        *,
        profile_id: str,
        binding: OwnerInstallationPublicBindingV1,
        pairing_intent_id: str,
        pairing_intent_digest: str,
        issue_receipt_id: str,
        issue_receipt_digest: str,
        pairing_expires_at: datetime,
        redeem_idempotency_key: str,
        redeem_digest: str,
    ) -> None:
        if (
            snapshot.profile_id != profile_id
            or owner_profile_id(binding) != profile_id
            or snapshot.central_origin != binding.central_origin
            or snapshot.org_id != binding.org_id
            or snapshot.owner_user_id != binding.owner_user_id
            or snapshot.agent_card_id != binding.agent_card_id
            or snapshot.agent_card_revision != binding.agent_card_revision
            or snapshot.agent_card_digest != binding.agent_card_digest
            or snapshot.device_key_thumbprint != binding.device_key_thumbprint
            or snapshot.binding_digest != binding_digest(binding)
            or snapshot.pairing_intent_id != pairing_intent_id
            or snapshot.pairing_intent_digest != pairing_intent_digest
            or snapshot.issue_receipt_id != issue_receipt_id
            or snapshot.issue_receipt_digest != issue_receipt_digest
            or snapshot.pairing_expires_at != pairing_expires_at
            or snapshot.redeem_idempotency_key
            not in {None, redeem_idempotency_key}
            or snapshot.redeem_command_digest not in {None, redeem_digest}
        ):
            raise OwnerPairingOrchestrationUnavailable()

    def _slot(
        self, value: RedeemedOwnerPairing, bundle: object
    ) -> OwnerCredentialSlotV1:
        if not hasattr(bundle, "device"):
            raise OwnerPairingOrchestrationUnavailable()
        device = cast(OwnerDeviceKeyMaterialV1, getattr(bundle, "device"))
        private = _private_key(device)
        decrypted = decrypt_owner_credential(
            value.envelope, private, expected_aad=value.envelope.aad
        )
        return OwnerCredentialSlotV1(
            credential_id=value.credential_id,
            credential_generation=value.credential_generation,
            credential_secret=SecretBytes(
                _credential_secret_bytes(decrypted.credential_secret.get_secret_value())
            ),
            issued_at=value.envelope.aad.issued_at,
            expires_at=value.envelope.aad.expires_at,
            scope=value.envelope.aad.scope,
            aad_digest=credential_aad_digest(value.envelope.aad),
            envelope_digest=envelope_digest(
                serialize_owner_credential_envelope(value.envelope)
            ),
            redeem_receipt_id=value.redeem_receipt_id,
            redeem_receipt_digest=value.redeem_receipt_digest,
        )

    def _finalize(
        self,
        profile_id: str,
        binding: OwnerInstallationPublicBindingV1,
        snapshot: OwnerPairingRecoverySnapshot,
    ) -> OwnerPairingRecoveryResult:
        if snapshot.state != "credential_stored":
            raise OwnerPairingOrchestrationUnavailable()
        current = self._device_keys.load(profile_id)
        if current is None or current.active is None:
            raise OwnerPairingOrchestrationUnavailable()
        if snapshot.bundle_revision is None:
            raise OwnerPairingOrchestrationUnavailable()
        if current.pairing_pending is not None:
            final = current.model_copy(
                update={
                    "bundle_revision": current.bundle_revision + 1,
                    "pairing_pending": None,
                }
            )
            expected = bundle_public_digest(final)
        elif current.bundle_revision == snapshot.bundle_revision + 1:
            expected = bundle_public_digest(current)
        else:
            raise OwnerPairingOrchestrationUnavailable()
        return self._recovery.finalize(
            FinalizeFromStoredCredentialCommand(
                profile_id=profile_id,
                expected_state="credential_stored",
                expected_updated_at=snapshot.updated_at,
                expected_central_origin=binding.central_origin,
                expected_bundle_public_digest=expected,
                now=_next_after(self._clock, snapshot.updated_at),
                idempotency_key=_derived_ref("pairing-finalize-", profile_id),
            )
        )


__all__ = [
    "OwnerPairingOrchestrationUnavailable",
    "OwnerPairingOrchestrator",
    "OwnerPairingVerifier",
]
