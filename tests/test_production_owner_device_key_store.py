from datetime import UTC, datetime, timedelta
import importlib
import json
import os
from pathlib import Path

from pydantic import SecretBytes, ValidationError
import pytest

from agent_org_network.owner_device_key_store import (
    OwnerCredentialSlotV1,
    OwnerDeviceKeyMaterialV1,
    OwnerDeviceKeyStoreConflict,
    OwnerDeviceKeyStoreUnavailable,
    OwnerInstallationPublicBindingV1,
    OwnerPairingPendingV1,
    binding_digest,
    bundle_public_digest,
    canonicalize_central_origin,
    credential_public_digest,
    envelope_digest,
    owner_keychain_account_ref,
    owner_profile_id,
)
from agent_org_network.production_owner_device_key_store import (
    ProductionOwnerDeviceKeyStore,
    decode_owner_secret_bundle,
    encode_owner_secret_bundle,
)

NOW = datetime(2026, 7, 28, tzinfo=UTC)


class _MemoryBackend:
    priority = 5

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.set_count = 0

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.set_count += 1
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[(service, username)]


_MemoryBackend.__module__ = "keyring.backends.macOS"
_MemoryBackend.__qualname__ = "Keyring"


class _Module:
    def __init__(self, backend: object) -> None:
        self.backend = backend

    def get_keyring(self) -> object:
        return self.backend


def _store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ProductionOwnerDeviceKeyStore, _MemoryBackend]:
    backend = _MemoryBackend()
    real = importlib.import_module

    def load(name: str):
        return _Module(backend) if name == "keyring" else real(name)

    monkeypatch.setattr(importlib, "import_module", load)
    return ProductionOwnerDeviceKeyStore(tmp_path / "locks"), backend


def _models():
    device = OwnerDeviceKeyMaterialV1.generate()
    binding = OwnerInstallationPublicBindingV1(
        central_origin="https://example.com",
        org_id="acme",
        owner_user_id="owner",
        agent_card_id="support",
        agent_card_revision=2,
        agent_card_digest="a" * 64,
        device_key_thumbprint=device.device_key_thumbprint,
    )
    pairing = OwnerPairingPendingV1(
        pairing_intent_id="intent-1",
        pairing_intent_digest="b" * 64,
        issue_receipt_id="issue-1",
        issue_receipt_digest="c" * 64,
        redeem_idempotency_key="redeem-1",
        redeem_command_digest="d" * 64,
        pairing_expires_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    slot = OwnerCredentialSlotV1(
        credential_id="credential-1",
        credential_generation=1,
        credential_secret=SecretBytes(b"s" * 32),
        issued_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        scope=("author.read", "author.write"),
        aad_digest="e" * 64,
        envelope_digest="f" * 64,
        redeem_receipt_id="receipt-1",
        redeem_receipt_digest="1" * 64,
    )
    return device, binding, pairing, slot


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://ExAmPle.com:443", "https://example.com"),
        ("https://[2001:0db8::1]:443", "https://[2001:db8::1]"),
        ("https://example.com:8443", "https://example.com:8443"),
    ],
)
def test_origin_canonicalization(raw: str, expected: str) -> None:
    assert canonicalize_central_origin(raw) == expected
    assert canonicalize_central_origin(expected) == expected


@pytest.mark.parametrize(
    "raw",
    [
        " https://example.com",
        "https://user@example.com",
        "https://example.com/",
        "https://example.com?q=1",
        "https://bad_host",
        "https://example.com.",
        "http://example.com",
        "https://localhost",
        "https://127.0.0.1",
        "https://127.99.1.2",
        "https://[::1]",
        "https://[fe80::1%25en0]",
    ],
)
def test_origin_invalid_failclosed(raw: str) -> None:
    with pytest.raises(OwnerDeviceKeyStoreUnavailable):
        canonicalize_central_origin(raw)
    assert canonicalize_central_origin(
        "http://127.0.0.1", allow_loopback_http=True
    ) == "http://127.0.0.1"


def test_codec_digest_and_secret_redaction() -> None:
    device, binding, pairing, slot = _models()
    from agent_org_network.owner_device_key_store import (
        OwnerInstallationSecretBundleV1,
    )

    bundle = OwnerInstallationSecretBundleV1(
        bundle_revision=1,
        binding=binding,
        binding_digest=binding_digest(binding),
        device=device,
        pairing_pending=pairing,
        active=slot,
        pending=None,
    )
    raw = encode_owner_secret_bundle(bundle)
    assert decode_owner_secret_bundle(raw) == bundle
    assert encode_owner_secret_bundle(decode_owner_secret_bundle(raw)) == raw
    assert "b'sssss" not in repr(bundle)
    assert len(binding_digest(binding)) == 64
    assert len(credential_public_digest(slot)) == 64
    assert len(bundle_public_digest(bundle)) == 64
    assert envelope_digest(b"{}") != __import__("hashlib").sha256(b"{}").hexdigest()
    assert owner_profile_id(binding) == owner_profile_id(binding)
    assert owner_keychain_account_ref(owner_profile_id(binding)) == __import__(
        "hashlib"
    ).sha256(owner_profile_id(binding).encode()).hexdigest()
    tampered = json.loads(raw)
    tampered["extra"] = 1
    with pytest.raises(OwnerDeviceKeyStoreUnavailable):
        decode_owner_secret_bundle(json.dumps(tampered))


def test_store_revision_create_active_finalize_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, backend = _store(tmp_path, monkeypatch)
    device, binding, pairing, slot = _models()
    created = store.create_pending(
        "profile-1", binding=binding, device=device, pairing_pending=pairing
    )
    assert created.bundle_revision == 1
    assert store.load("profile-1").active is None  # type: ignore[union-attr]
    stored = store.store_active(
        "profile-1", expected_revision=1, slot=slot, keep_pairing_pending=True
    )
    assert stored.bundle_revision == 2
    finalized = store.clear_pairing_pending("profile-1", expected_revision=2)
    assert finalized.bundle_revision == 3
    loaded = store.load("profile-1")
    assert loaded is not None and loaded.active == slot and loaded.pairing_pending is None
    with pytest.raises(OwnerDeviceKeyStoreConflict):
        store.store_active(
            "profile-1", expected_revision=3, slot=slot, keep_pairing_pending=False
        )
    replacement = slot.model_copy(
        update={
            "credential_id": "credential-2",
            "redeem_receipt_id": "receipt-2",
            "aad_digest": "2" * 64,
            "envelope_digest": "3" * 64,
            "redeem_receipt_digest": "4" * 64,
        }
    )
    with pytest.raises(OwnerDeviceKeyStoreConflict):
        store.replace(
            "profile-1",
            expected_revision=3,
            value=loaded.model_copy(
                update={"bundle_revision": 4, "active": replacement}
            ),
        )
    with pytest.raises(OwnerDeviceKeyStoreConflict):
        store.clear_pairing_pending("profile-1", expected_revision=2)
    assert backend.set_count == 3
    store.delete("profile-1", expected_revision=3)
    assert store.load("profile-1") is None


def test_finalize_bundle_delta는pairing_pending만정확히소비한다() -> None:
    from agent_org_network.owner_device_key_store import (
        OwnerInstallationSecretBundleV1,
    )
    from agent_org_network.owner_pairing_recovery_store import (
        OwnerPairingRecoveryUnavailable,
        validate_finalized_owner_bundle_delta,
    )

    device, binding, pairing, slot = _models()
    stored = OwnerInstallationSecretBundleV1(
        bundle_revision=2,
        binding=binding,
        binding_digest=binding_digest(binding),
        device=device,
        pairing_pending=pairing,
        active=slot,
        pending=None,
    )
    finalized = stored.model_copy(
        update={"bundle_revision": 3, "pairing_pending": None}
    )
    validate_finalized_owner_bundle_delta(stored, finalized)
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        validate_finalized_owner_bundle_delta(
            stored, finalized.model_copy(update={"bundle_revision": 4})
        )
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        validate_finalized_owner_bundle_delta(
            stored, finalized.model_copy(update={"active": None})
        )


def test_wrong_backend_env_windows_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded: list[str] = []

    def fail(name: str):
        loaded.append(name)
        raise AssertionError

    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "evil")
    monkeypatch.setattr(importlib, "import_module", fail)
    with pytest.raises(OwnerDeviceKeyStoreUnavailable):
        ProductionOwnerDeviceKeyStore(tmp_path / "env")
    assert loaded == [] and not (tmp_path / "env").exists()
    monkeypatch.delenv("PYTHON_KEYRING_BACKEND")
    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(OwnerDeviceKeyStoreUnavailable):
        ProductionOwnerDeviceKeyStore(tmp_path / "win")
    assert not (tmp_path / "win").exists()


def test_windows_native_uses_dpapi_backend_without_importing_keyring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_org_network.production_owner_device_key_store as module

    loaded: list[str] = []

    class _FakeWindowsDpapiBackend:
        def __init__(self, root: Path) -> None:
            self.root = root
            self.values: dict[str, str] = {}

        def set_password(self, service: str, username: str, password: str) -> None:
            assert service == "agent-org-network.owner-secret-bundle.v1"
            assert len(username) == 64 and all(char in "0123456789abcdef" for char in username)
            self.values[username] = password

        def get_password(self, service: str, username: str) -> str | None:
            return self.values.get(username)

        def delete_password(self, service: str, username: str) -> None:
            self.values.pop(username, None)

    def fail_import(name: str):
        loaded.append(name)
        raise AssertionError

    monkeypatch.setattr(module, "_WindowsDpapiBackend", _FakeWindowsDpapiBackend)
    monkeypatch.setattr(importlib, "import_module", fail_import)
    monkeypatch.setattr(os, "name", "nt")
    store = ProductionOwnerDeviceKeyStore(tmp_path / "win-dpapi")
    assert str(store._root).replace("\\", "/") == str(tmp_path / "win-dpapi")  # pyright: ignore[reportPrivateUsage]
    assert loaded == []
    assert store.probe()


def test_model_invariants() -> None:
    _device, binding, pairing, slot = _models()
    with pytest.raises(ValidationError):
        OwnerCredentialSlotV1.model_validate(
            {**slot.model_dump(), "credential_secret": SecretBytes(b"x")}
        )
    with pytest.raises(ValidationError):
        OwnerInstallationPublicBindingV1.model_validate(
            {
                **binding.model_dump(),
                "agent_card_digest": "X" * 64,
            }
        )
    with pytest.raises(ValidationError):
        OwnerInstallationPublicBindingV1.model_validate(
            {**binding.model_dump(), "agent_card_revision": 0}
        )
    with pytest.raises(ValidationError):
        OwnerPairingPendingV1.model_validate(
            {
                **pairing.model_dump(),
                "pairing_expires_at": "2026-07-28T00:00:00.000Z",
            }
        )
