from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sqlite3
from threading import Barrier, Lock

import pytest
from pydantic import SecretBytes

from agent_org_network.owner_device_key_store import (
    OwnerCredentialSlotV1,
    OwnerDeviceKeyMaterialV1,
    OwnerDeviceKeyStoreConflict,
    OwnerInstallationPublicBindingV1,
    OwnerPairingPendingV1,
    OwnerSecretBundleWriteReceipt,
    binding_digest,
    bundle_public_digest,
    credential_public_digest,
    owner_profile_id,
)
from agent_org_network.owner_pairing_recovery_store import (
    CreateIntentRecovery,
    FinalizeFromStoredCredentialCommand,
    LegacyOwnerPairingRequiresRepair,
    MarkCredentialStored,
    MarkRedeemSubmitted,
    OwnerPairingRecoveryConflict,
    OwnerPairingRecoveryStore,
    OwnerPairingRecoveryUnavailable,
    RecoverFromKeychainCommand,
)
from agent_org_network.production_owner_device_key_store import (
    ProductionOwnerDeviceKeyStore,
)


NOW = datetime(2026, 7, 28, 1, 2, 3, tzinfo=UTC)


class _FinalizeBackend:
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


_FinalizeBackend.__module__ = "keyring.backends.macOS"
_FinalizeBackend.__qualname__ = "Keyring"


class _KeyringModule:
    def __init__(self, backend: _FinalizeBackend) -> None:
        self._backend = backend

    def get_keyring(self) -> _FinalizeBackend:
        return self._backend


def _binding() -> OwnerInstallationPublicBindingV1:
    return OwnerInstallationPublicBindingV1(
        central_origin="https://central.example",
        org_id="acme",
        owner_user_id="owner-1",
        agent_card_id="support",
        agent_card_revision=3,
        agent_card_digest="a" * 64,
        device_key_thumbprint="A" * 43,
    )


def _finalize_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fault: Callable[[str], None] | None = None,
) -> tuple[
    OwnerPairingRecoveryStore,
    FinalizeFromStoredCredentialCommand,
    ProductionOwnerDeviceKeyStore,
    _FinalizeBackend,
]:
    backend = _FinalizeBackend()
    real_import = importlib.import_module

    def load(name: str):
        return _KeyringModule(backend) if name == "keyring" else real_import(name)

    monkeypatch.setattr(importlib, "import_module", load)
    keys = ProductionOwnerDeviceKeyStore(tmp_path / "locks")
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
    profile_id = owner_profile_id(binding)
    pairing = OwnerPairingPendingV1(
        pairing_intent_id="intent-1",
        pairing_intent_digest="b" * 64,
        issue_receipt_id="receipt-1",
        issue_receipt_digest="c" * 64,
        redeem_idempotency_key="central-redeem-1",
        redeem_command_digest="d" * 64,
        pairing_expires_at=(NOW + timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    )
    slot = OwnerCredentialSlotV1(
        credential_id="credential-1",
        credential_generation=2,
        credential_secret=SecretBytes(b"s" * 32),
        issued_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        scope=("author.read",),
        aad_digest="2" * 64,
        envelope_digest="3" * 64,
        redeem_receipt_id="redeem-receipt-1",
        redeem_receipt_digest="4" * 64,
    )
    keys.create_pending(
        profile_id, binding=binding, device=device, pairing_pending=pairing
    )
    keys.store_active(
        profile_id, expected_revision=1, slot=slot, keep_pairing_pending=True
    )
    stored_bundle = keys.load(profile_id)
    assert stored_bundle is not None
    path = tmp_path / "finalize.sqlite"
    store = OwnerPairingRecoveryStore(
        path, device_keys=keys, clock=lambda: NOW + timedelta(seconds=3),
        fault=fault,
    )
    store.create_intent_recovery(
        CreateIntentRecovery(
            profile_id=profile_id, now=NOW, binding=binding,
            pairing_intent_id=pairing.pairing_intent_id,
            pairing_intent_digest=pairing.pairing_intent_digest,
            issue_receipt_id=pairing.issue_receipt_id,
            issue_receipt_digest=pairing.issue_receipt_digest,
            pairing_expires_at=NOW + timedelta(minutes=5),
            idempotency_key="idem-create",
        )
    )
    store.mark_redeem_submitted(
        MarkRedeemSubmitted(
            profile_id=profile_id, expected_state="intent_issued",
            expected_updated_at=NOW,
            redeem_idempotency_key=pairing.redeem_idempotency_key,
            redeem_command_digest=pairing.redeem_command_digest,
            now=NOW + timedelta(seconds=1), idempotency_key="idem-redeem",
        )
    )
    store.mark_credential_stored(
        MarkCredentialStored(
            profile_id=profile_id, expected_state="redeem_submitted",
            expected_updated_at=NOW + timedelta(seconds=1),
            credential_id=slot.credential_id,
            credential_generation=slot.credential_generation,
            credential_public_digest=credential_public_digest(slot),
            bundle_revision=stored_bundle.bundle_revision,
            bundle_public_digest=bundle_public_digest(stored_bundle),
            now=NOW + timedelta(seconds=2), idempotency_key="idem-stored",
        )
    )
    finalized = stored_bundle.model_copy(
        update={
            "bundle_revision": stored_bundle.bundle_revision + 1,
            "pairing_pending": None,
        }
    )
    command = FinalizeFromStoredCredentialCommand(
        profile_id=profile_id, expected_state="credential_stored",
        expected_updated_at=NOW + timedelta(seconds=2),
        expected_central_origin=binding.central_origin,
        expected_bundle_public_digest=bundle_public_digest(finalized),
        now=NOW + timedelta(seconds=3), idempotency_key="idem-finalize",
    )
    return store, command, keys, backend


def _recover_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fault: Callable[[str], None] | None = None,
) -> tuple[
    OwnerPairingRecoveryStore,
    RecoverFromKeychainCommand,
    ProductionOwnerDeviceKeyStore,
    _FinalizeBackend,
]:
    _store, finalize_command, keys, backend = _finalize_fixture(
        tmp_path, monkeypatch
    )
    keys.clear_pairing_pending(
        finalize_command.profile_id, expected_revision=2
    )
    command = RecoverFromKeychainCommand(
        profile_id=finalize_command.profile_id,
        expected_central_origin=finalize_command.expected_central_origin,
        now=NOW + timedelta(seconds=3),
        idempotency_key="idem-recover",
    )
    return (
        OwnerPairingRecoveryStore(
            tmp_path / "recover.sqlite",
            device_keys=keys,
            clock=lambda: NOW + timedelta(seconds=3),
            fault=fault,
        ),
        command,
        keys,
        backend,
    )


def _create(key: str = "idem-create") -> CreateIntentRecovery:
    return CreateIntentRecovery(
        profile_id="profile-1",
        now=NOW,
        binding=_binding(),
        pairing_intent_id="intent-1",
        pairing_intent_digest="b" * 64,
        issue_receipt_id="receipt-1",
        issue_receipt_digest="c" * 64,
        pairing_expires_at=NOW + timedelta(minutes=5),
        idempotency_key=key,
    )


def _credential_projection() -> dict[str, object]:
    from agent_org_network.owner_device_key_store import SUITE

    return {
        "aad_digest": "2" * 64,
        "credential_generation": 2,
        "credential_id": "credential-1",
        "envelope_digest": "3" * 64,
        "envelope_suite": SUITE,
        "envelope_version": 1,
        "expires_at": (NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "issued_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "redeem_receipt_digest": "4" * 64,
        "redeem_receipt_id": "redeem-receipt-1",
        "scope": ["author.read"],
        "slot_version": 1,
    }


def _store_credential(path: Path) -> None:
    from agent_org_network.owner_device_key_store import (
        credential_public_digest_from_projection,
    )

    store = OwnerPairingRecoveryStore(path)
    store.create_intent_recovery(_create())
    store.mark_redeem_submitted(
        MarkRedeemSubmitted(
            profile_id="profile-1", expected_state="intent_issued",
            expected_updated_at=NOW, redeem_idempotency_key="central-redeem-1",
            redeem_command_digest="d" * 64, now=NOW + timedelta(seconds=1),
            idempotency_key="idem-redeem",
        )
    )
    store.mark_credential_stored(
        MarkCredentialStored(
            profile_id="profile-1", expected_state="redeem_submitted",
            expected_updated_at=NOW + timedelta(seconds=1),
            credential_id="credential-1", credential_generation=2,
            credential_public_digest=credential_public_digest_from_projection(
                _credential_projection()
            ),
            bundle_revision=1,
            bundle_public_digest="f" * 64, now=NOW + timedelta(seconds=2),
            idempotency_key="idem-stored",
        )
    )


def _replay_commands() -> tuple[
    CreateIntentRecovery, MarkRedeemSubmitted, MarkCredentialStored
]:
    return (
        _create(),
        MarkRedeemSubmitted(
            profile_id="profile-1",
            expected_state="intent_issued",
            expected_updated_at=NOW,
            redeem_idempotency_key="central-redeem-1",
            redeem_command_digest="d" * 64,
            now=NOW + timedelta(seconds=1),
            idempotency_key="idem-redeem",
        ),
        MarkCredentialStored(
            profile_id="profile-1",
            expected_state="redeem_submitted",
            expected_updated_at=NOW + timedelta(seconds=1),
            credential_id="credential-1",
            credential_generation=2,
            credential_public_digest="e" * 64,
            bundle_revision=1,
            bundle_public_digest="f" * 64,
            now=NOW + timedelta(seconds=2),
            idempotency_key="idem-stored",
        ),
    )


def _install_terminal_fixture(path: Path, action: str) -> None:
    from agent_org_network.owner_device_key_store import (
        SUITE,
        credential_public_digest_from_projection,
        owner_keychain_account_ref,
    )

    if action == "pairing.finalize":
        _store_credential(path)
        verification = "paired"
        bundle_revision = 2
        terminal_at = NOW + timedelta(seconds=3)
        command = {
            "expected_central_origin": _binding().central_origin,
            "expected_bundle_public_digest": "1" * 64,
            "expected_state": "credential_stored",
            "expected_updated_at": (NOW + timedelta(seconds=2)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "profile_id": "profile-1",
        }
        kind = "finalized"
        domain = b"aon.owner.local.pairing-finalize.v2\0"
    else:
        OwnerPairingRecoveryStore(path)
        verification = "recovered_unverified"
        bundle_revision = 7
        terminal_at = NOW
        command = {
            "expected_central_origin": _binding().central_origin,
            "profile_id": "profile-1",
        }
        kind = "recovered_unverified"
        domain = b"aon.owner.local.profile-recover.v2\0"
    instant = terminal_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    credential_projection = _credential_projection()
    profile = {
        "profile_id": "profile-1",
        "central_origin": _binding().central_origin,
        "keychain_account_ref": owner_keychain_account_ref("profile-1"),
        "org_id": _binding().org_id,
        "owner_user_id": _binding().owner_user_id,
        "agent_card_id": _binding().agent_card_id,
        "agent_card_revision": _binding().agent_card_revision,
        "agent_card_digest": _binding().agent_card_digest,
        "device_key_thumbprint": _binding().device_key_thumbprint,
        "binding_digest": binding_digest(_binding()),
        "credential_id": "credential-1",
        "credential_generation": 2,
        "credential_public_digest": credential_public_digest_from_projection(
            credential_projection
        ),
        "bundle_revision": bundle_revision,
        "bundle_public_digest": "1" * 64,
        "issued_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope_json": '["author.read"]',
        "slot_version": 1,
        "envelope_suite": SUITE,
        "envelope_version": 1,
        "aad_digest": credential_projection["aad_digest"],
        "envelope_digest": credential_projection["envelope_digest"],
        "redeem_receipt_id": credential_projection["redeem_receipt_id"],
        "redeem_receipt_digest": credential_projection["redeem_receipt_digest"],
        "verification": verification,
    }
    result = {
        "binding_digest": profile["binding_digest"],
        "bundle_public_digest": profile["bundle_public_digest"],
        "bundle_revision": profile["bundle_revision"],
        "credential_generation": profile["credential_generation"],
        "credential_id": profile["credential_id"],
        "credential_public_digest": profile["credential_public_digest"],
        "kind": kind,
        "profile_id": "profile-1",
        "state": None,
        "verification": verification,
    }
    def canonical(value: object) -> str:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    command_json = canonical(command)
    resource_json = canonical(profile)
    result_json = canonical(result)
    command_digest = sha256(domain + command_json.encode()).hexdigest()
    resource_digest = sha256(
        b"aon.owner.local.recovery-row.v2\0" + resource_json.encode()
    ).hexdigest()
    receipt_projection = {
        "action": action,
        "command_digest": command_digest,
        "created_at": instant,
        "idempotency_key": f"idem-{action}",
        "resource_digest": resource_digest,
        "result_digest": sha256(result_json.encode()).hexdigest(),
        "row_created_at": instant,
        "row_updated_at": instant,
    }
    receipt_digest = sha256(
        b"aon.owner.local.receipt.v2\0" + canonical(receipt_projection).encode()
    ).hexdigest()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO owner_installation_profiles VALUES("
            + ",".join("?" for _ in range(28))
            + ")",
            (*profile.values(), instant, instant),
        )
        connection.execute(
            "INSERT INTO owner_pairing_recovery_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"idem-{action}", action,
                command_digest, command_json, resource_digest,
                resource_json, result_json, instant, instant, instant,
                receipt_digest,
            ),
        )
        if action == "pairing.finalize":
            connection.execute(
                "DELETE FROM owner_pairing_recoveries WHERE profile_id='profile-1'"
            )


def test_세_recovery전이는_exact_replay와_CAS를보장한다(tmp_path: Path) -> None:
    store = OwnerPairingRecoveryStore(tmp_path / "owner.sqlite")
    created = store.create_intent_recovery(_create())
    assert created.kind == "intent_created"
    assert store.create_intent_recovery(_create()).kind == "replayed"

    redeem_command = MarkRedeemSubmitted(
            profile_id="profile-1",
            expected_state="intent_issued",
            expected_updated_at=NOW,
            redeem_idempotency_key="central-redeem-1",
            redeem_command_digest="d" * 64,
            now=NOW + timedelta(seconds=1),
            idempotency_key="idem-redeem",
        )
    submitted = store.mark_redeem_submitted(redeem_command)
    assert submitted.kind == "redeem_submitted"
    assert store.mark_redeem_submitted(
        MarkRedeemSubmitted(
            profile_id="profile-1",
            expected_state="intent_issued",
            expected_updated_at=NOW,
            redeem_idempotency_key="central-redeem-1",
            redeem_command_digest="d" * 64,
            now=NOW + timedelta(seconds=1),
            idempotency_key="idem-redeem",
        )
    ).kind == "replayed"

    stored_command = MarkCredentialStored(
            profile_id="profile-1",
            expected_state="redeem_submitted",
            expected_updated_at=NOW + timedelta(seconds=1),
            credential_id="credential-1",
            credential_generation=2,
            credential_public_digest="e" * 64,
            bundle_revision=1,
            bundle_public_digest="f" * 64,
            now=NOW + timedelta(seconds=2),
            idempotency_key="idem-stored",
        )
    stored = store.mark_credential_stored(stored_command)
    assert stored.kind == "credential_stored"
    assert stored.credential_generation == 2
    assert store.create_intent_recovery(_create()).model_dump() == created.model_copy(
        update={"kind": "replayed"}
    ).model_dump()
    assert store.mark_redeem_submitted(redeem_command).model_dump() == (
        submitted.model_copy(update={"kind": "replayed"}).model_dump()
    )
    assert store.mark_credential_stored(stored_command).model_dump() == (
        stored.model_copy(update={"kind": "replayed"}).model_dump()
    )

    with pytest.raises(OwnerPairingRecoveryConflict):
        store.mark_credential_stored(
            MarkCredentialStored(
                profile_id="profile-1",
                expected_state="redeem_submitted",
                expected_updated_at=NOW + timedelta(seconds=1),
                credential_id="credential-2",
                credential_generation=3,
                credential_public_digest="1" * 64,
                bundle_revision=2,
                bundle_public_digest="2" * 64,
                now=NOW + timedelta(seconds=3),
                idempotency_key="different",
            )
        )


def test_finalize는keychain_clear후paired_profile로atomic종결하고replay한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, command, keys, backend = _finalize_fixture(tmp_path, monkeypatch)
    result = store.finalize(command)
    assert result.kind == "finalized"
    assert result.verification == "paired"
    assert keys.load(command.profile_id).pairing_pending is None  # type: ignore[union-attr]
    writes = backend.set_count
    assert store.finalize(command).kind == "replayed"
    assert backend.set_count == writes
    with sqlite3.connect(tmp_path / "finalize.sqlite") as connection:
        assert connection.execute(
            "SELECT count(*) FROM owner_pairing_recoveries"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT verification FROM owner_installation_profiles"
        ).fetchone() == ("paired",)


def test_finalize는keychain_clear뒤crash를already_cleared로복구한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(point: str) -> None:
        if point == "after_keychain_clear":
            raise RuntimeError("crash")

    store, command, keys, backend = _finalize_fixture(
        tmp_path, monkeypatch, fault=crash
    )
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        store.finalize(command)
    assert keys.load(command.profile_id).pairing_pending is None  # type: ignore[union-attr]
    writes = backend.set_count
    resumed = OwnerPairingRecoveryStore(
        tmp_path / "finalize.sqlite",
        device_keys=keys,
        clock=lambda: NOW + timedelta(seconds=3),
    )
    assert resumed.finalize(command).kind == "finalized"
    assert backend.set_count == writes


def test_finalize_before_commit_fault는SQLite를rollback하고resume한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(point: str) -> None:
        if point == "before_commit":
            raise RuntimeError("crash")

    _store, command, keys, _backend = _finalize_fixture(tmp_path, monkeypatch)
    store = OwnerPairingRecoveryStore(
        tmp_path / "finalize.sqlite",
        device_keys=keys,
        clock=lambda: NOW + timedelta(seconds=3),
        fault=crash,
    )
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        store.finalize(command)
    with sqlite3.connect(tmp_path / "finalize.sqlite") as connection:
        assert connection.execute(
            "SELECT count(*) FROM owner_installation_profiles"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT state FROM owner_pairing_recoveries"
        ).fetchone() == ("credential_stored",)
    resumed = OwnerPairingRecoveryStore(
        tmp_path / "finalize.sqlite",
        device_keys=keys,
        clock=lambda: NOW + timedelta(seconds=3),
    )
    assert resumed.finalize(command).kind == "finalized"


def test_finalize_wrong_digest와expired는keychain_write전에failclosed한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, command, keys, backend = _finalize_fixture(tmp_path, monkeypatch)
    writes = backend.set_count
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        store.finalize(
            command.model_copy(update={"expected_bundle_public_digest": "0" * 64})
        )
    assert backend.set_count == writes
    expired = OwnerPairingRecoveryStore(
        tmp_path / "finalize.sqlite",
        device_keys=keys,
        clock=lambda: NOW + timedelta(days=31),
    )
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        expired.finalize(command)
    assert backend.set_count == writes


@pytest.mark.parametrize("mode", ["missing", "corrupt"])
def test_finalize_missing_or_corrupt_keychain은failclosed한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    root = tmp_path / mode
    store, command, keys, backend = _finalize_fixture(root, monkeypatch)
    if mode == "missing":
        keys.delete(command.profile_id, expected_revision=2)
    else:
        account = next(iter(backend.values))
        backend.values[account] = "{bad"
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        store.finalize(command)
    with sqlite3.connect(root / "finalize.sqlite") as connection:
        assert connection.execute(
            "SELECT state FROM owner_pairing_recoveries"
        ).fetchone() == ("credential_stored",)
        assert connection.execute(
            "SELECT count(*) FROM owner_installation_profiles"
        ).fetchone() == (0,)


def test_finalize_32way는한finalized와31replay다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, command, _keys, _backend = _finalize_fixture(tmp_path, monkeypatch)

    def run(_: int) -> str:
        return store.finalize(command).kind

    with ThreadPoolExecutor(max_workers=32) as pool:
        kinds = list(pool.map(run, range(32)))
    assert kinds.count("finalized") == 1
    assert kinds.count("replayed") == 31


def test_finalize_32distinct_store의keychain_CAS_conflict는exact_final에join한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, command, _keys, backend = _finalize_fixture(tmp_path, monkeypatch)
    before_clear_writes = backend.set_count
    barrier = Barrier(32)
    conflict_lock = Lock()
    conflicts = 0
    original_clear = ProductionOwnerDeviceKeyStore.clear_pairing_pending

    def observed_clear(
        self: ProductionOwnerDeviceKeyStore,
        key: str,
        *,
        expected_revision: int,
    ) -> OwnerSecretBundleWriteReceipt:
        nonlocal conflicts
        try:
            return original_clear(
                self, key, expected_revision=expected_revision
            )
        except OwnerDeviceKeyStoreConflict:
            with conflict_lock:
                conflicts += 1
            raise

    monkeypatch.setattr(
        ProductionOwnerDeviceKeyStore,
        "clear_pairing_pending",
        observed_clear,
    )

    def synchronize(point: str) -> None:
        if point == "before_keychain_clear":
            barrier.wait(timeout=10)

    stores: list[OwnerPairingRecoveryStore] = []
    for _index in range(32):
        keys = ProductionOwnerDeviceKeyStore(tmp_path / "locks")
        stores.append(
            OwnerPairingRecoveryStore(
                tmp_path / "finalize.sqlite",
                device_keys=keys,
                clock=lambda: NOW + timedelta(seconds=3),
                fault=synchronize,
            )
        )

    def run(index: int) -> str:
        return stores[index].finalize(command).kind

    with ThreadPoolExecutor(max_workers=32) as pool:
        kinds = list(pool.map(run, range(32)))
    assert kinds.count("finalized") == 1
    assert kinds.count("replayed") == 31
    assert conflicts == 31
    assert backend.set_count == before_clear_writes + 1
    with sqlite3.connect(tmp_path / "finalize.sqlite") as connection:
        assert connection.execute(
            "SELECT count(*) FROM owner_installation_profiles"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM owner_pairing_recovery_receipts "
            "WHERE action='pairing.finalize'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM owner_pairing_recoveries"
        ).fetchone() == (0,)


def test_recover_from_keychain은active_only를unverified_profile로atomic복구한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, command, _keys, _backend = _recover_fixture(tmp_path, monkeypatch)

    result = store.recover_from_keychain(command)
    assert result.kind == "recovered_unverified"
    assert result.verification == "recovered_unverified"
    assert store.recover_from_keychain(command).kind == "replayed"

    with sqlite3.connect(tmp_path / "recover.sqlite") as connection:
        assert connection.execute(
            "SELECT verification FROM owner_installation_profiles"
        ).fetchone() == ("recovered_unverified",)
        assert connection.execute(
            "SELECT count(*) FROM owner_pairing_recovery_receipts "
            "WHERE action='profile.recover'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM owner_pairing_recoveries"
        ).fetchone() == (0,)
        dump = " ".join(
            str(value)
            for row in connection.iterdump()
            for value in (row,)
        )
        assert "ssssssss" not in dump
        assert "private_key" not in dump


def test_recover_from_keychain은다른명령과local_row충돌을failclosed한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, command, _keys, _backend = _recover_fixture(tmp_path, monkeypatch)
    store.recover_from_keychain(command)
    with pytest.raises(OwnerPairingRecoveryConflict):
        store.recover_from_keychain(
            command.model_copy(update={"idempotency_key": "other-recover"})
        )

    root = tmp_path / "recovery-conflict"
    conflict_store, conflict_command, _keys, _backend = _recover_fixture(
        root, monkeypatch
    )
    bundle = _keys.load(conflict_command.profile_id)
    assert bundle is not None
    conflict_store.create_intent_recovery(
        CreateIntentRecovery(
            profile_id=conflict_command.profile_id,
            binding=bundle.binding,
            pairing_intent_id="different-intent",
            pairing_intent_digest="b" * 64,
            issue_receipt_id="different-receipt",
            issue_receipt_digest="c" * 64,
            pairing_expires_at=NOW + timedelta(minutes=5),
            now=NOW,
            idempotency_key="different-create",
        )
    )
    with pytest.raises(OwnerPairingRecoveryConflict):
        conflict_store.recover_from_keychain(conflict_command)


@pytest.mark.parametrize("mode", ["expired", "pending", "pairing_pending", "origin"])
def test_recover_from_keychain은non_active_only와drift를failclosed한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    store, command, keys, _backend = _recover_fixture(tmp_path, monkeypatch)
    if mode == "expired":
        store = OwnerPairingRecoveryStore(
            tmp_path / "recover.sqlite",
            device_keys=keys,
            clock=lambda: NOW + timedelta(days=31),
        )
    elif mode == "origin":
        command = command.model_copy(
            update={"expected_central_origin": "https://other.example"}
        )
    else:
        bundle = keys.load(command.profile_id)
        assert bundle is not None and bundle.active is not None
        if mode == "pending":
            keys.replace(
                command.profile_id,
                expected_revision=bundle.bundle_revision,
                value=bundle.model_copy(
                    update={
                        "bundle_revision": bundle.bundle_revision + 1,
                        "pending": bundle.active.model_copy(
                            update={
                                "credential_id": "credential-2",
                                "credential_generation": 3,
                                "aad_digest": "5" * 64,
                                "envelope_digest": "6" * 64,
                                "redeem_receipt_id": "redeem-receipt-2",
                                "redeem_receipt_digest": "7" * 64,
                            }
                        ),
                    }
                ),
            )
        else:
            keys.replace(
                command.profile_id,
                expected_revision=bundle.bundle_revision,
                value=bundle.model_copy(
                    update={
                        "bundle_revision": bundle.bundle_revision + 1,
                        "pairing_pending": OwnerPairingPendingV1(
                            pairing_intent_id="drift-intent",
                            pairing_intent_digest="b" * 64,
                            issue_receipt_id="drift-receipt",
                            issue_receipt_digest="c" * 64,
                            redeem_idempotency_key="drift-redeem",
                            redeem_command_digest="d" * 64,
                            pairing_expires_at=(
                                NOW + timedelta(minutes=5)
                            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        ),
                    }
                ),
            )
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        store.recover_from_keychain(command)
    with sqlite3.connect(tmp_path / "recover.sqlite") as connection:
        assert connection.execute(
            "SELECT count(*) FROM owner_installation_profiles"
        ).fetchone() == (0,)


def test_recover_from_keychain_before_commit_fault는rollback하고resume한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(point: str) -> None:
        if point == "before_commit":
            raise RuntimeError("crash")

    store, command, keys, _backend = _recover_fixture(
        tmp_path, monkeypatch, fault=crash
    )
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        store.recover_from_keychain(command)
    with sqlite3.connect(tmp_path / "recover.sqlite") as connection:
        assert connection.execute(
            "SELECT count(*) FROM owner_installation_profiles"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM owner_pairing_recovery_receipts"
        ).fetchone() == (0,)
    resumed = OwnerPairingRecoveryStore(
        tmp_path / "recover.sqlite",
        device_keys=keys,
        clock=lambda: NOW + timedelta(seconds=3),
    )
    assert resumed.recover_from_keychain(command).kind == "recovered_unverified"


def test_recover_from_keychain_32distinct_store는한recovered와31replay다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, command, _keys, backend = _recover_fixture(tmp_path, monkeypatch)
    stores = [
        OwnerPairingRecoveryStore(
            tmp_path / "recover.sqlite",
            device_keys=ProductionOwnerDeviceKeyStore(tmp_path / "locks"),
            clock=lambda: NOW + timedelta(seconds=3),
        )
        for _index in range(32)
    ]

    def run(index: int) -> str:
        return stores[index].recover_from_keychain(command).kind

    before = backend.set_count
    with ThreadPoolExecutor(max_workers=32) as pool:
        kinds = list(pool.map(run, range(32)))
    assert kinds.count("recovered_unverified") == 1
    assert kinds.count("replayed") == 31
    assert backend.set_count == before


def test_same_create_16thread는한_row와_replay15개다(tmp_path: Path) -> None:
    path = tmp_path / "owner.sqlite"
    OwnerPairingRecoveryStore(path)

    def run(_: int) -> str:
        return OwnerPairingRecoveryStore(path).create_intent_recovery(_create()).kind

    with ThreadPoolExecutor(max_workers=16) as pool:
        kinds = list(pool.map(run, range(16)))
    assert kinds.count("intent_created") == 1
    assert kinds.count("replayed") == 15


@pytest.mark.parametrize("coordinated", [False, True])
def test_live_store의모든replay는current_row_tamper를먼저failclosed한다(
    tmp_path: Path, coordinated: bool,
) -> None:
    path = tmp_path / "live-replay-tamper.sqlite"
    store = OwnerPairingRecoveryStore(path)
    create, submitted, stored = _replay_commands()
    store.create_intent_recovery(create)
    store.mark_redeem_submitted(submitted)
    store.mark_credential_stored(stored)

    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE name='owner_pairing_recoveries_exact_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER owner_pairing_recoveries_exact_update")
        if coordinated:
            connection.execute(
                "UPDATE owner_pairing_recoveries SET credential_id=?,"
                "credential_generation=?,credential_public_digest=?,"
                "bundle_revision=?,bundle_public_digest=?",
                ("credential-evil", 3, "1" * 64, 2, "2" * 64),
            )
        else:
            connection.execute(
                "UPDATE owner_pairing_recoveries SET credential_id=?",
                ("credential-evil",),
            )
        connection.execute(trigger_sql)

    replay_calls = (
        lambda: store.create_intent_recovery(create),
        lambda: store.mark_redeem_submitted(submitted),
        lambda: store.mark_credential_stored(stored),
    )
    for replay in replay_calls:
        with pytest.raises(OwnerPairingRecoveryUnavailable):
            replay()


def test_terminal_schema는direct_recovery_delete와forged_profile을거부한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "terminal-schema.sqlite"
    _store_credential(path)
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM owner_pairing_recoveries WHERE profile_id='profile-1'"
            )

    forged = tmp_path / "forged-profile.sqlite"
    _install_terminal_fixture(forged, "profile.recover")
    with sqlite3.connect(forged) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE "
            "name='owner_pairing_recovery_receipts_no_delete'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER owner_pairing_recovery_receipts_no_delete")
        connection.execute("DELETE FROM owner_pairing_recovery_receipts")
        connection.execute(trigger_sql)
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        OwnerPairingRecoveryStore(forged)


@pytest.mark.parametrize("action", ["pairing.finalize", "profile.recover"])
def test_terminal_profile과receipt의canonical_fixture는reopen된다(
    tmp_path: Path, action: str,
) -> None:
    path = tmp_path / f"{action}.sqlite"
    _install_terminal_fixture(path, action)
    OwnerPairingRecoveryStore(path)


@pytest.mark.parametrize("action", ["pairing.finalize", "profile.recover"])
@pytest.mark.parametrize(
    ("table", "trigger", "column", "value"),
    [
        (
            "owner_pairing_recovery_receipts",
            "owner_pairing_recovery_receipts_no_update",
            "command_digest",
            "0" * 64,
        ),
        (
            "owner_pairing_recovery_receipts",
            "owner_pairing_recovery_receipts_no_update",
            "result_json",
            "{}",
        ),
        (
            "owner_pairing_recovery_receipts",
            "owner_pairing_recovery_receipts_no_update",
            "idempotency_key",
            "idem-evil",
        ),
        (
            "owner_pairing_recovery_receipts",
            "owner_pairing_recovery_receipts_no_update",
            "action",
            "intent.create",
        ),
        (
            "owner_pairing_recovery_receipts",
            "owner_pairing_recovery_receipts_no_update",
            "resource_json",
            "{}",
        ),
        (
            "owner_pairing_recovery_receipts",
            "owner_pairing_recovery_receipts_no_update",
            "created_at",
            "2026-07-28T01:09:03Z",
        ),
        (
            "owner_installation_profiles",
            "owner_installation_profiles_no_update",
            "agent_card_digest",
            "0" * 64,
        ),
        (
            "owner_installation_profiles",
            "owner_installation_profiles_no_update",
            "scope_json",
            '["author.read","author.read"]',
        ),
        (
            "owner_installation_profiles",
            "owner_installation_profiles_no_update",
            "central_origin",
            "https://evil.example",
        ),
        (
            "owner_installation_profiles",
            "owner_installation_profiles_no_update",
            "keychain_account_ref",
            "0" * 64,
        ),
        (
            "owner_installation_profiles",
            "owner_installation_profiles_no_update",
            "binding_digest",
            "0" * 64,
        ),
        (
            "owner_installation_profiles",
            "owner_installation_profiles_no_update",
            "credential_id",
            "credential-evil",
        ),
        (
            "owner_installation_profiles",
            "owner_installation_profiles_no_update",
            "bundle_public_digest",
            "0" * 64,
        ),
    ],
)
def test_terminal_receipt와profile_tamper는trigger복원뒤failclosed한다(
    tmp_path: Path,
    action: str,
    table: str,
    trigger: str,
    column: str,
    value: str,
) -> None:
    path = tmp_path / f"{action}-{column}.sqlite"
    _install_terminal_fixture(path, action)
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (trigger,)
        ).fetchone()[0]
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(
            f"UPDATE {table} SET {column}=? WHERE profile_id='profile-1'"
            if table == "owner_installation_profiles"
            else f"UPDATE {table} SET {column}=? WHERE action=?",
            (value,) if table == "owner_installation_profiles" else (value, action),
        )
        connection.execute(trigger_sql)
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        OwnerPairingRecoveryStore(path)


@pytest.mark.parametrize("mutation", ["empty_credential", "unknown_scope", "short_life"])
def test_terminal_public_credential의coordinated_invalid_projection은거부한다(
    tmp_path: Path, mutation: str,
) -> None:
    path = tmp_path / f"credential-{mutation}.sqlite"
    _install_terminal_fixture(path, "profile.recover")
    with sqlite3.connect(path) as connection:
        profile_trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE "
            "name='owner_installation_profiles_no_update'"
        ).fetchone()[0]
        receipt_trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE "
            "name='owner_pairing_recovery_receipts_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER owner_installation_profiles_no_update")
        connection.execute(
            "DROP TRIGGER owner_pairing_recovery_receipts_no_update"
        )
        row = connection.execute(
            "SELECT command_digest,resource_json,result_json,created_at,"
            "row_created_at,row_updated_at,idempotency_key,action "
            "FROM owner_pairing_recovery_receipts WHERE action='profile.recover'"
        ).fetchone()
        resource = json.loads(row[1])
        result = json.loads(row[2])
        projection = _credential_projection()
        if mutation == "empty_credential":
            projection["credential_id"] = ""
            resource["credential_id"] = ""
            result["credential_id"] = ""
        elif mutation == "unknown_scope":
            projection["scope"] = ["unknown.action"]
            resource["scope_json"] = '["unknown.action"]'
        else:
            projection["expires_at"] = (
                NOW + timedelta(days=1)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            resource["expires_at"] = projection["expires_at"]
        public_digest = sha256(
            b"aon.owner.credential-public.v1\0"
            + json.dumps(
                projection, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        resource["credential_public_digest"] = public_digest
        result["credential_public_digest"] = public_digest
        resource_json = json.dumps(
            resource, sort_keys=True, separators=(",", ":")
        )
        result_json = json.dumps(result, sort_keys=True, separators=(",", ":"))
        resource_digest = sha256(
            b"aon.owner.local.recovery-row.v2\0" + resource_json.encode()
        ).hexdigest()
        receipt_projection = {
            "action": row[7],
            "command_digest": row[0],
            "created_at": row[3],
            "idempotency_key": row[6],
            "resource_digest": resource_digest,
            "result_digest": sha256(result_json.encode()).hexdigest(),
            "row_created_at": row[4],
            "row_updated_at": row[5],
        }
        receipt_digest = sha256(
            b"aon.owner.local.receipt.v2\0"
            + json.dumps(
                receipt_projection, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        updates = {
            "credential_public_digest": public_digest,
            "credential_id": resource["credential_id"],
            "scope_json": resource["scope_json"],
            "expires_at": resource["expires_at"],
        }
        connection.execute(
            "UPDATE owner_installation_profiles SET credential_public_digest=?,"
            "credential_id=?,scope_json=?,expires_at=?",
            tuple(updates.values()),
        )
        connection.execute(
            "UPDATE owner_pairing_recovery_receipts SET resource_json=?,"
            "resource_digest=?,result_json=?,receipt_digest=? "
            "WHERE action='profile.recover'",
            (resource_json, resource_digest, result_json, receipt_digest),
        )
        connection.execute(profile_trigger)
        connection.execute(receipt_trigger)
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        OwnerPairingRecoveryStore(path)


def test_terminal_bad_profile_id의coordinated_tamper도거부한다(
    tmp_path: Path,
) -> None:
    from agent_org_network.owner_device_key_store import owner_keychain_account_ref

    path = tmp_path / "bad-profile.sqlite"
    _install_terminal_fixture(path, "profile.recover")
    bad_profile = " bad-profile"
    with sqlite3.connect(path) as connection:
        profile_trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE "
            "name='owner_installation_profiles_no_update'"
        ).fetchone()[0]
        receipt_trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE "
            "name='owner_pairing_recovery_receipts_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER owner_installation_profiles_no_update")
        connection.execute(
            "DROP TRIGGER owner_pairing_recovery_receipts_no_update"
        )
        row = connection.execute(
            "SELECT idempotency_key,action,command_json,resource_json,result_json,"
            "created_at,row_created_at,row_updated_at "
            "FROM owner_pairing_recovery_receipts WHERE action='profile.recover'"
        ).fetchone()
        command = json.loads(row[2])
        resource = json.loads(row[3])
        result = json.loads(row[4])
        command["profile_id"] = bad_profile
        resource["profile_id"] = bad_profile
        resource["keychain_account_ref"] = owner_keychain_account_ref(bad_profile)
        result["profile_id"] = bad_profile
        def canonical(value: object) -> str:
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        command_json = canonical(command)
        resource_json = canonical(resource)
        result_json = canonical(result)
        command_digest = sha256(
            b"aon.owner.local.profile-recover.v2\0" + command_json.encode()
        ).hexdigest()
        resource_digest = sha256(
            b"aon.owner.local.recovery-row.v2\0" + resource_json.encode()
        ).hexdigest()
        receipt_projection = {
            "action": row[1],
            "command_digest": command_digest,
            "created_at": row[5],
            "idempotency_key": row[0],
            "resource_digest": resource_digest,
            "result_digest": sha256(result_json.encode()).hexdigest(),
            "row_created_at": row[6],
            "row_updated_at": row[7],
        }
        receipt_digest = sha256(
            b"aon.owner.local.receipt.v2\0"
            + canonical(receipt_projection).encode()
        ).hexdigest()
        connection.execute(
            "UPDATE owner_installation_profiles SET profile_id=?,"
            "keychain_account_ref=?",
            (bad_profile, resource["keychain_account_ref"]),
        )
        connection.execute(
            "UPDATE owner_pairing_recovery_receipts SET command_json=?,"
            "command_digest=?,resource_json=?,resource_digest=?,result_json=?,"
            "receipt_digest=? WHERE action='profile.recover'",
            (
                command_json, command_digest, resource_json, resource_digest,
                result_json, receipt_digest,
            ),
        )
        connection.execute(profile_trigger)
        connection.execute(receipt_trigger)
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        OwnerPairingRecoveryStore(path)


def test_empty_v1은cutover하고_nonempty_v1은repair를요구한다(tmp_path: Path) -> None:
    from agent_org_network.owner_local_authoring_repository import OwnerLocalAuthoringKey
    from agent_org_network.owner_pairing_session_store import OwnerPairingSessionStore

    class Keys:
        def current(self) -> OwnerLocalAuthoringKey:
            return OwnerLocalAuthoringKey(key_id="key-1", key=b"k" * 32)

    empty = tmp_path / "empty.sqlite"
    OwnerPairingSessionStore(empty, keys=Keys(), clock=lambda: NOW)
    OwnerPairingRecoveryStore(empty)
    with sqlite3.connect(empty) as connection:
        assert connection.execute(
            "SELECT count(*) FROM owner_pairing_recoveries"
        ).fetchone() == (0,)

    nonempty = tmp_path / "nonempty.sqlite"
    old = OwnerPairingSessionStore(nonempty, keys=Keys(), clock=lambda: NOW)
    from agent_org_network.owner_pairing_session_store import OwnerPairingBootstrap
    from pydantic import SecretStr

    old.consume(
        OwnerPairingBootstrap(
            audience="owner-install",
            org_id="acme",
            owner_id="owner-1",
            agent_id="support",
            card_revision=1,
            card_digest="a" * 64,
            device_public_key_digest="b" * 64,
            identity_provider="oidc",
            central_identity_session=SecretStr("x" * 32),
            credential_generation=1,
            owner_session_ttl_seconds=60,
            allowed_origin="https://owner.example",
            expires_at=NOW + timedelta(minutes=5),
        )
    )
    with pytest.raises(LegacyOwnerPairingRequiresRepair):
        OwnerPairingRecoveryStore(nonempty)


@pytest.mark.parametrize("point", ["after_mutation", "before_commit"])
def test_fault는recovery와receipt를함께rollback한다(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / "owner.sqlite"

    def fault(current: str) -> None:
        if current == point:
            raise RuntimeError("crash")

    with pytest.raises(OwnerPairingRecoveryUnavailable):
        OwnerPairingRecoveryStore(path, fault=fault).create_intent_recovery(_create())
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM owner_pairing_recoveries"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM owner_pairing_recovery_receipts"
        ).fetchone() == (0,)


def test_catalog와receipt_tamper는failclosed다(tmp_path: Path) -> None:
    path = tmp_path / "owner.sqlite"
    store = OwnerPairingRecoveryStore(path)
    store.create_intent_recovery(_create())
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER owner_pairing_recoveries_no_delete")
        connection.execute(
            "CREATE TRIGGER owner_pairing_recoveries_no_delete "
            "BEFORE DELETE ON owner_pairing_recoveries BEGIN SELECT 1; END"
        )
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        store.create_intent_recovery(_create())

    clean = tmp_path / "clean.sqlite"
    clean_store = OwnerPairingRecoveryStore(clean)
    clean_store.create_intent_recovery(_create())
    with sqlite3.connect(clean) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE owner_pairing_recovery_receipts SET resource_digest=?",
                ("0" * 64,),
            )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("command_digest", "0" * 64),
        ("resource_json", '{"state":"intent_issued"}'),
        (
            "result_json",
            '{"binding_digest":"' + "0" * 64 + '","bundle_public_digest":null,'
            '"bundle_revision":null,"credential_generation":null,'
            '"credential_id":null,"credential_public_digest":null,'
            '"kind":"intent_created","profile_id":"profile-1",'
            '"state":"intent_issued","verification":null}',
        ),
    ],
)
def test_trigger_drop후_historical_receipt_tamper도constructor가거부한다(
    tmp_path: Path, column: str, value: str
) -> None:
    path = tmp_path / f"{column}.sqlite"
    OwnerPairingRecoveryStore(path).create_intent_recovery(_create())
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE name='owner_pairing_recovery_receipts_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER owner_pairing_recovery_receipts_no_update")
        connection.execute(
            f"UPDATE owner_pairing_recovery_receipts SET {column}=?", (value,)
        )
        connection.execute(trigger_sql)
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        OwnerPairingRecoveryStore(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("credential_id", "credential-evil"),
        ("credential_generation", 3),
        ("credential_public_digest", "1" * 64),
        ("bundle_revision", 2),
        ("bundle_public_digest", "2" * 64),
    ],
)
def test_stored_historical_result의각credential_projection_tamper를거부한다(
    tmp_path: Path, field: str, value: object
) -> None:
    path = tmp_path / f"stored-{field}.sqlite"
    _store_credential(path)
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE name='owner_pairing_recovery_receipts_no_update'"
        ).fetchone()[0]
        result_json = connection.execute(
            "SELECT result_json FROM owner_pairing_recovery_receipts "
            "WHERE action='credential.mark_stored'"
        ).fetchone()[0]
        result = json.loads(result_json)
        result[field] = value
        tampered = json.dumps(
            result, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        connection.execute("DROP TRIGGER owner_pairing_recovery_receipts_no_update")
        connection.execute(
            "UPDATE owner_pairing_recovery_receipts SET result_json=? "
            "WHERE action='credential.mark_stored'",
            (tampered,),
        )
        connection.execute(trigger_sql)
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        OwnerPairingRecoveryStore(path)


def test_command_resource_result와digest의coordinated_tamper도latest에서거부한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "coordinated.sqlite"
    _store_credential(path)
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE name='owner_pairing_recovery_receipts_no_update'"
        ).fetchone()[0]
        command_json, resource_json, result_json = connection.execute(
            "SELECT command_json,resource_json,result_json "
            "FROM owner_pairing_recovery_receipts "
            "WHERE action='credential.mark_stored'"
        ).fetchone()
        command = json.loads(command_json)
        resource = json.loads(resource_json)
        result = json.loads(result_json)
        for value in (command, resource, result):
            value["credential_id"] = "credential-evil"
        command_json = json.dumps(command, sort_keys=True, separators=(",", ":"))
        resource_json = json.dumps(resource, sort_keys=True, separators=(",", ":"))
        result_json = json.dumps(result, sort_keys=True, separators=(",", ":"))
        command_digest = sha256(
            b"aon.owner.local.credential-stored.v2\0" + command_json.encode()
        ).hexdigest()
        resource_digest = sha256(
            b"aon.owner.local.recovery-row.v2\0" + resource_json.encode()
        ).hexdigest()
        connection.execute("DROP TRIGGER owner_pairing_recovery_receipts_no_update")
        connection.execute(
            "UPDATE owner_pairing_recovery_receipts SET command_json=?,"
            "command_digest=?,resource_json=?,resource_digest=?,result_json=? "
            "WHERE action='credential.mark_stored'",
            (
                command_json, command_digest, resource_json,
                resource_digest, result_json,
            ),
        )
        connection.execute(trigger_sql)
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        OwnerPairingRecoveryStore(path)


def test_future_expected와digest_timestamp_anchor의coordinated_tamper를거부한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "future-timestamps.sqlite"
    _store_credential(path)
    future = "2026-07-28T01:09:03Z"
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE name='owner_pairing_recovery_receipts_no_update'"
        ).fetchone()[0]
        command = json.loads(
            connection.execute(
                "SELECT command_json FROM owner_pairing_recovery_receipts "
                "WHERE action='redeem.mark_submitted'"
            ).fetchone()[0]
        )
        command["expected_updated_at"] = future
        command_json = json.dumps(command, sort_keys=True, separators=(",", ":"))
        command_digest = sha256(
            b"aon.owner.local.redeem-submitted.v2\0" + command_json.encode()
        ).hexdigest()
        connection.execute("DROP TRIGGER owner_pairing_recovery_receipts_no_update")
        connection.execute(
            "UPDATE owner_pairing_recovery_receipts SET command_json=?,"
            "command_digest=?,row_updated_at=?,created_at=? "
            "WHERE action='redeem.mark_submitted'",
            (command_json, command_digest, future, future),
        )
        connection.execute(trigger_sql)
    with pytest.raises(OwnerPairingRecoveryUnavailable):
        OwnerPairingRecoveryStore(path)
