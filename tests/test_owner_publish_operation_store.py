from __future__ import annotations

from base64 import b64decode, b64encode
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

from agent_org_network.owner_local_authoring_repository import AuthoringArtifactRef, OwnerLocalAuthoringKey
from agent_org_network.owner_publish_operation_store import OwnerPublishCommitted, OwnerPublishOperationStore, OwnerPublishOperationStoreUnavailable, OwnerPublishPrepared


class Keys:
    def current(self) -> OwnerLocalAuthoringKey:
        return OwnerLocalAuthoringKey(key_id="k", key=b"k" * 32)


def prepared() -> OwnerPublishPrepared:
    ref = AuthoringArtifactRef(organization_id="acme", agent_id="support", run_id="run", revision=1, artifact_kind="full_draft_bundle", artifact_digest="d" * 64)
    raw = {"org_id":"acme", "agent_id":"support", "run_id":"run", "review_revision":2, "publishing_claim_digest":"a" * 64, "card_revision":2, "card_digest":"b" * 64, "source_set_digest":"c" * 64, "admitted_bundle_digest":"d" * 64, "artifact_ref":ref, "operation_digest":"0" * 64}
    draft = OwnerPublishPrepared.model_construct(_fields_set=set(raw), **raw)
    return OwnerPublishPrepared.model_validate(raw | {"operation_digest": OwnerPublishPrepared.digest_for(draft)})


def test_encrypted_restart_and_transaction_rollback_do_not_persist_body(tmp_path: Path) -> None:
    path = tmp_path / "publish.sqlite"
    state = prepared()
    key = OwnerPublishOperationStore.operation_key("acme", "support", "run")
    failing = OwnerPublishOperationStore(path, keys=Keys(), fault=lambda point: (_ for _ in ()).throw(RuntimeError()) if point == "before_commit" else None)
    with pytest.raises(OwnerPublishOperationStoreUnavailable):
        failing.save(key, state, expected_stage=None)
    assert failing.load(key, state.operation_digest) is None
    store = OwnerPublishOperationStore(path, keys=Keys())
    store.save(key, state, expected_stage=None)
    assert OwnerPublishOperationStore(path, keys=Keys()).load(key, state.operation_digest) == state
    admitted_bundle = b'{"agent_id":"support","documents":["owner-only plaintext"]}'
    assert admitted_bundle not in path.read_bytes()


def test_save_rejects_operation_key_bound_to_different_semantic_fields(tmp_path: Path) -> None:
    store = OwnerPublishOperationStore(tmp_path / "publish.sqlite", keys=Keys())
    state = prepared()
    wrong_key = store.operation_key("acme", "support", "different-run")

    with pytest.raises(OwnerPublishOperationStoreUnavailable):
        store.save(wrong_key, state, expected_stage=None)


def test_load_rejects_decrypted_state_misbound_to_operation_key(tmp_path: Path) -> None:
    path = tmp_path / "publish.sqlite"
    store = OwnerPublishOperationStore(path, keys=Keys())
    state = prepared()
    key = store.operation_key("acme", "support", "run")
    store.save(key, state, expected_stage=None)
    wrong_key = store.operation_key("acme", "support", "different-run")

    with sqlite3.connect(path) as con:
        digest, stage, key_id, nonce, ciphertext = con.execute(
            "SELECT operation_digest,stage,key_id,nonce,ciphertext FROM owner_publish_operations"
        ).fetchone()
        old_aad = json.dumps([key, digest, stage, key_id], sort_keys=True, separators=(",", ":")).encode()
        plaintext = AESGCM(b"k" * 32).decrypt(
            b64decode(nonce), b64decode(ciphertext), old_aad
        )
        new_aad = json.dumps(
            [wrong_key, digest, stage, key_id], sort_keys=True, separators=(",", ":")
        ).encode()
        new_nonce = b"n" * 12
        new_ciphertext = AESGCM(b"k" * 32).encrypt(new_nonce, plaintext, new_aad)
        con.execute("DROP TRIGGER owner_publish_operations_transition")
        con.execute(
            "UPDATE owner_publish_operations SET operation_key=?,nonce=?,ciphertext=?",
            (wrong_key, b64encode(new_nonce).decode(), b64encode(new_ciphertext).decode()),
        )

    with pytest.raises(OwnerPublishOperationStoreUnavailable):
        store.load(wrong_key, state.operation_digest)


def test_committed_sha_and_index_digest_are_immutable(tmp_path: Path) -> None:
    store = OwnerPublishOperationStore(tmp_path / "publish.sqlite", keys=Keys())
    state = prepared()
    key = store.operation_key("acme", "support", "run")
    store.save(key, state, expected_stage=None)
    committed = OwnerPublishCommitted.model_validate(state.model_dump() | {"commit_sha": sha256(b"sha").hexdigest(), "committed_tree_index_digest": sha256(b"index").hexdigest()})
    store.save(key, committed, expected_stage="prepared")
    assert store.load(key, state.operation_digest) == committed
    with pytest.raises(OwnerPublishOperationStoreUnavailable):
        store.save(key, committed, expected_stage="prepared")
