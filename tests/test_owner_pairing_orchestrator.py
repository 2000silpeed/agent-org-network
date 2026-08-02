from datetime import UTC, datetime, timedelta
import importlib
from pathlib import Path
from typing import NotRequired, TypedDict

import pytest
from pydantic import SecretStr

from agent_org_network.central_owner_pairing_client import (
    OwnerPairingCode,
    RedeemedOwnerPairing,
)
from agent_org_network.owner_credential_envelope import (
    CredentialEnvelopeAad,
    encrypt_owner_credential,
)
from agent_org_network.owner_device_key_store import (
    OwnerDeviceKeyMaterialV1,
    OwnerInstallationPublicBindingV1,
    OwnerPairingPendingV1,
    owner_profile_id,
)
from agent_org_network.owner_pairing_digest import (
    owner_pairing_redeem_request_digest,
)
from agent_org_network.owner_pairing_orchestrator import (
    OwnerPairingOrchestrator,
    OwnerPairingOrchestrationUnavailable,
)
from agent_org_network.owner_pairing_recovery_store import (
    OwnerPairingRecoveryStore,
)
from agent_org_network.production_owner_device_key_store import (
    ProductionOwnerDeviceKeyStore,
)


NOW = datetime(2026, 8, 2, 3, 4, 5, tzinfo=UTC)


class _PairArguments(TypedDict):
    binding: OwnerInstallationPublicBindingV1
    device: OwnerDeviceKeyMaterialV1
    pairing_code: OwnerPairingCode
    pairing_intent_digest: str
    issue_receipt_id: str
    issue_receipt_digest: str
    pairing_expires_at: datetime
    redeem_idempotency_key: str
    pairing_reference: NotRequired[str]


class _Backend:
    priority = 5

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[(service, username)]


_Backend.__module__ = "keyring.backends.macOS"
_Backend.__qualname__ = "Keyring"


class _KeyringModule:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend

    def get_keyring(self) -> _Backend:
        return self.backend


class _Verifier:
    def __init__(self, binding: OwnerInstallationPublicBindingV1) -> None:
        self.binding = binding
        self.calls = 0

    def redeem(
        self,
        code: OwnerPairingCode,
        *,
        device_public_key: object,
        idempotency_key: str,
    ) -> RedeemedOwnerPairing:
        from agent_org_network.owner_credential_envelope import X25519PublicJwk

        assert isinstance(device_public_key, X25519PublicJwk)
        self.calls += 1
        issued = NOW + timedelta(minutes=1)
        expires = issued + timedelta(days=30)
        aad = CredentialEnvelopeAad(
            agent_card_id=self.binding.agent_card_id,
            credential_generation=1,
            credential_id="credential-1",
            device_key_thumbprint=self.binding.device_key_thumbprint,
            expires_at=expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            issued_at=issued.strftime("%Y-%m-%dT%H:%M:%SZ"),
            org_id=self.binding.org_id,
            owner_user_id=self.binding.owner_user_id,
            scope=("author.read", "author.write"),
        )
        envelope = encrypt_owner_credential(aad, device_public_key)
        return RedeemedOwnerPairing(
            audience="owner-install",
            org_id=self.binding.org_id,
            owner_id=self.binding.owner_user_id,
            agent_id=self.binding.agent_card_id,
            card_revision=self.binding.agent_card_revision,
            card_digest=self.binding.agent_card_digest,
            device_key_thumbprint=self.binding.device_key_thumbprint,
            identity_provider="corp",
            credential_id="credential-1",
            credential_generation=1,
            expires_at=expires,
            envelope=envelope,
            redeem_request_digest=owner_pairing_redeem_request_digest(
                intent_id=code.intent_id,
                idempotency_key=idempotency_key,
                device_key_thumbprint=self.binding.device_key_thumbprint,
            ),
            pairing_intent_digest="b" * 64,
            issue_receipt_id="issue-1",
            issue_receipt_digest="c" * 64,
            redeem_receipt_id=idempotency_key,
            redeem_receipt_digest="d" * 64,
        )


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    backend = _Backend()
    real_import = importlib.import_module

    def load(name: str):
        return _KeyringModule(backend) if name == "keyring" else real_import(name)

    monkeypatch.setattr(importlib, "import_module", load)
    device = OwnerDeviceKeyMaterialV1.generate()
    binding = OwnerInstallationPublicBindingV1(
        central_origin="https://central.example",
        org_id="acme",
        owner_user_id="owner-1",
        agent_card_id="support",
        agent_card_revision=3,
        agent_card_digest="a" * 64,
        device_key_thumbprint=device.device_key_thumbprint,
    )
    keys = ProductionOwnerDeviceKeyStore(tmp_path / "locks")
    recovery = OwnerPairingRecoveryStore(
        tmp_path / "recovery.sqlite",
        device_keys=keys,
        clock=lambda: NOW + timedelta(minutes=2),
    )
    verifier = _Verifier(binding)
    return binding, device, keys, recovery, verifier


def test_pair_orchestrator는CAS와keychain을같은순서로완료한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding, device, keys, recovery, verifier = _setup(tmp_path, monkeypatch)
    result = OwnerPairingOrchestrator(
        verifier=verifier,
        device_keys=keys,
        recovery=recovery,
        clock=lambda: NOW + timedelta(minutes=1),
    ).pair(
        binding=binding,
        device=device,
        pairing_code=OwnerPairingCode(
            intent_id="intent-1", value=SecretStr("p" * 32)
        ),
        pairing_intent_digest="b" * 64,
        issue_receipt_id="issue-1",
        issue_receipt_digest="c" * 64,
        pairing_expires_at=NOW + timedelta(minutes=5),
        redeem_idempotency_key="redeem-1",
    )
    assert result.kind == "finalized"
    assert verifier.calls == 1
    profile_id = owner_profile_id(binding)
    bundle = keys.load(profile_id)
    assert bundle is not None
    assert bundle.active is not None
    assert bundle.pairing_pending is None
    assert recovery.read_snapshot(profile_id) is None


def test_pair_orchestrator는서버응답digest가다르면저장을하지않는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding, device, keys, recovery, verifier = _setup(tmp_path, monkeypatch)
    original = verifier.redeem

    def wrong(
        code: OwnerPairingCode,
        *,
        device_public_key: object,
        idempotency_key: str,
    ) -> RedeemedOwnerPairing:
        return original(
            code,
            device_public_key=device_public_key,
            idempotency_key=idempotency_key,
        ).model_copy(
            update={"redeem_request_digest": "e" * 64}
        )

    verifier.redeem = wrong  # type: ignore[method-assign]
    with pytest.raises(OwnerPairingOrchestrationUnavailable):
        OwnerPairingOrchestrator(
            verifier=verifier,
            device_keys=keys,
            recovery=recovery,
            clock=lambda: NOW + timedelta(minutes=1),
        ).pair(
            binding=binding,
            device=device,
            pairing_code=OwnerPairingCode(
                intent_id="intent-1", value=SecretStr("p" * 32)
            ),
            pairing_intent_digest="b" * 64,
            issue_receipt_id="issue-1",
            issue_receipt_digest="c" * 64,
            pairing_expires_at=NOW + timedelta(minutes=5),
            redeem_idempotency_key="redeem-1",
        )
    assert keys.load(owner_profile_id(binding)).active is None  # type: ignore[union-attr]


def test_terminal_bundle은첫복구후같은idempotency로replayed된다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding, device, keys, recovery, verifier = _setup(tmp_path, monkeypatch)
    orchestrator = OwnerPairingOrchestrator(
        verifier=verifier,
        device_keys=keys,
        recovery=recovery,
        clock=lambda: NOW + timedelta(minutes=1),
    )
    arguments: _PairArguments = {
        "binding": binding,
        "device": device,
        "pairing_code": OwnerPairingCode(
            intent_id="intent-1", value=SecretStr("p" * 32)
        ),
        "pairing_intent_digest": "b" * 64,
        "issue_receipt_id": "issue-1",
        "issue_receipt_digest": "c" * 64,
        "pairing_expires_at": NOW + timedelta(minutes=5),
        "redeem_idempotency_key": "redeem-1",
    }
    profile_id = owner_profile_id(binding)
    keys.create_pending(
        profile_id,
        binding=binding,
        device=device,
        pairing_pending=OwnerPairingPendingV1(
            pairing_intent_id="intent-1",
            pairing_intent_digest="b" * 64,
            issue_receipt_id="issue-1",
            issue_receipt_digest="c" * 64,
            redeem_idempotency_key="redeem-1",
            redeem_command_digest=owner_pairing_redeem_request_digest(
                intent_id="intent-1",
                idempotency_key="redeem-1",
                device_key_thumbprint=binding.device_key_thumbprint,
            ),
            pairing_expires_at="2026-08-02T03:09:05Z",
        ),
    )
    bundle = keys.load(profile_id)
    assert bundle is not None
    redeemed = verifier.redeem(
        arguments["pairing_code"],
        device_public_key=device.public_key,
        idempotency_key="redeem-1",
    )
    slot = orchestrator._slot(redeemed, bundle)  # pyright: ignore[reportPrivateUsage]
    keys.store_active(
        profile_id,
        expected_revision=bundle.bundle_revision,
        slot=slot,
        keep_pairing_pending=False,
    )
    assert recovery.read_snapshot(profile_id) is None
    recovered = orchestrator.pair(**arguments)
    assert recovered.kind == "recovered_unverified"
    assert recovered.verification == "recovered_unverified"
    replayed = orchestrator.pair(**arguments)
    assert replayed.kind == "replayed"
    assert replayed.verification == "recovered_unverified"
    assert verifier.calls == 1


def test_terminal_bundle의caller_binding_drift는recovery_write없이거절된다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding, device, keys, recovery, verifier = _setup(tmp_path, monkeypatch)
    orchestrator = OwnerPairingOrchestrator(
        verifier=verifier,
        device_keys=keys,
        recovery=recovery,
        clock=lambda: NOW + timedelta(minutes=1),
    )
    base: _PairArguments = {
        "binding": binding,
        "device": device,
        "pairing_code": OwnerPairingCode(
            intent_id="intent-1", value=SecretStr("p" * 32)
        ),
        "pairing_intent_digest": "b" * 64,
        "issue_receipt_id": "issue-1",
        "issue_receipt_digest": "c" * 64,
        "pairing_expires_at": NOW + timedelta(minutes=5),
        "redeem_idempotency_key": "redeem-1",
    }
    assert not recovery.has_terminal_device_binding(binding.device_key_thumbprint)
    assert orchestrator.pair(**base).kind == "finalized"
    assert recovery.has_terminal_device_binding(binding.device_key_thumbprint)
    drifted = binding.model_copy(update={"central_origin": "https://other.example"})
    drifted_args: _PairArguments = {
        **base,
        "binding": drifted,
    }
    with pytest.raises(OwnerPairingOrchestrationUnavailable):
        orchestrator.pair(**drifted_args)
    assert recovery.read_snapshot(owner_profile_id(binding)) is None
    assert keys.load(owner_profile_id(drifted)) is None
    assert verifier.calls == 1


def test_terminal_active_expiry는recovery_write없이unavailable이다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding, device, keys, recovery, verifier = _setup(tmp_path, monkeypatch)
    arguments: _PairArguments = {
        "binding": binding,
        "device": device,
        "pairing_code": OwnerPairingCode(
            intent_id="intent-1", value=SecretStr("p" * 32)
        ),
        "pairing_intent_digest": "b" * 64,
        "issue_receipt_id": "issue-1",
        "issue_receipt_digest": "c" * 64,
        "pairing_expires_at": NOW + timedelta(minutes=5),
        "redeem_idempotency_key": "redeem-1",
    }
    assert OwnerPairingOrchestrator(
        verifier=verifier,
        device_keys=keys,
        recovery=recovery,
        clock=lambda: NOW + timedelta(minutes=1),
    ).pair(**arguments).kind == "finalized"
    future = NOW + timedelta(days=31)
    recovery._clock = lambda: future  # pyright: ignore[reportPrivateUsage]
    expired_args: _PairArguments = {
        **arguments,
        "pairing_expires_at": NOW + timedelta(days=40),
    }
    with pytest.raises(OwnerPairingOrchestrationUnavailable):
        OwnerPairingOrchestrator(
            verifier=verifier,
            device_keys=keys,
            recovery=recovery,
            clock=lambda: future,
        ).pair(**expired_args)
    assert recovery.read_snapshot(owner_profile_id(binding)) is None
    assert verifier.calls == 1
