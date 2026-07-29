"""Owner-local encrypted, append-only journal for the O5b publish saga.

This is deliberately independent from the O3 authoring-operation journal.  It
contains only stable identifiers, digests and local artifact references: never
the admitted OKF payload.
"""
from __future__ import annotations

from base64 import b64decode, b64encode
from collections.abc import Callable
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from agent_org_network.owner_local_authoring_repository import (
    AuthoringArtifactRef,
    OwnerLocalAuthoringKeyProvider,
)


class OwnerPublishOperationStoreUnavailable(Exception):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: str) -> str:
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("lowercase sha256 required")
    return value


class OwnerPublishPrepared(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    org_id: str
    agent_id: str
    run_id: str
    review_revision: Literal[2] = 2
    publishing_claim_digest: str
    card_revision: int
    card_digest: str
    source_set_digest: str
    admitted_bundle_digest: str
    artifact_ref: AuthoringArtifactRef
    operation_digest: str

    @field_validator("publishing_claim_digest", "card_digest", "source_set_digest", "admitted_bundle_digest", "operation_digest")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        return _digest(value)

    @model_validator(mode="after")
    def _consistent(self) -> "OwnerPublishPrepared":
        if self.card_revision <= 0 or self.artifact_ref.organization_id != self.org_id or self.artifact_ref.agent_id != self.agent_id or self.artifact_ref.run_id != self.run_id or self.artifact_ref.revision != 1 or self.artifact_ref.artifact_kind != "full_draft_bundle" or self.artifact_ref.artifact_digest != self.admitted_bundle_digest:
            raise ValueError("exact local admitted artifact required")
        if self.operation_digest != self.digest_for(self):
            raise ValueError("canonical operation digest required")
        return self

    @staticmethod
    def digest_for(value: "OwnerPublishPrepared") -> str:
        payload = value.model_dump(
            mode="json",
            exclude={"operation_digest", "commit_sha", "committed_tree_index_digest"},
        )
        return sha256(_canonical(payload)).hexdigest()


class OwnerPublishCommitted(OwnerPublishPrepared, frozen=True):
    commit_sha: str
    committed_tree_index_digest: str

    @field_validator("commit_sha", "committed_tree_index_digest")
    @classmethod
    def _commit_digest(cls, value: str) -> str:
        return _digest(value)


OwnerPublishOperationState = OwnerPublishPrepared | OwnerPublishCommitted

_SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_publish_operations (
 operation_key TEXT PRIMARY KEY, operation_digest TEXT NOT NULL, stage TEXT NOT NULL,
 key_id TEXT NOT NULL, nonce TEXT NOT NULL, ciphertext TEXT NOT NULL,
 CHECK(length(operation_key)=64), CHECK(length(operation_digest)=64),
 CHECK(stage IN ('prepared','committed'))
) STRICT;
CREATE TRIGGER IF NOT EXISTS owner_publish_operations_no_delete BEFORE DELETE ON owner_publish_operations BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS owner_publish_operations_transition BEFORE UPDATE ON owner_publish_operations
WHEN OLD.operation_key != NEW.operation_key OR OLD.operation_digest != NEW.operation_digest OR NOT (OLD.stage='prepared' AND NEW.stage='committed')
BEGIN SELECT RAISE(ABORT,'invalid transition'); END;
"""


def _no_fault(_point: str) -> None:
    return None


class OwnerPublishOperationStore:
    def __init__(self, path: str | Path, *, keys: OwnerLocalAuthoringKeyProvider, fault: Callable[[str], None] | None = None) -> None:
        self._path = str(Path(path))
        self._keys = keys
        self._fault: Callable[[str], None] = fault or _no_fault
        self._lock = RLock()
        try:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._connect() as con:
                con.executescript(_SCHEMA)
        except Exception as error:
            raise OwnerPublishOperationStoreUnavailable() from error

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    @staticmethod
    def operation_key(org_id: str, agent_id: str, run_id: str, review_revision: int = 2) -> str:
        return sha256(_canonical([org_id, agent_id, run_id, review_revision])).hexdigest()

    def load(self, operation_key: str, operation_digest: str) -> OwnerPublishOperationState | None:
        try:
            with self._lock, self._connect() as con:
                row = con.execute("SELECT operation_digest,stage,key_id,nonce,ciphertext FROM owner_publish_operations WHERE operation_key=?", (operation_key,)).fetchone()
            if row is None:
                return None
            if row[0] != operation_digest:
                raise OwnerPublishOperationStoreUnavailable()
            key = self._keys.current()
            if row[2] != key.key_id:
                raise OwnerPublishOperationStoreUnavailable()
            plaintext = AESGCM(key.key).decrypt(b64decode(row[3], validate=True), b64decode(row[4], validate=True), _canonical([operation_key, row[0], row[1], row[2]]))
            data = json.loads(plaintext)
            state: OwnerPublishOperationState = OwnerPublishCommitted.model_validate(data) if row[1] == "committed" else OwnerPublishPrepared.model_validate(data)
            if (
                operation_key
                != self.operation_key(
                    state.org_id, state.agent_id, state.run_id, state.review_revision
                )
                or state.operation_digest != row[0]
                or _canonical(state.model_dump(mode="json")) != plaintext
            ):
                raise OwnerPublishOperationStoreUnavailable()
            return state
        except OwnerPublishOperationStoreUnavailable:
            raise
        except (InvalidTag, ValueError, sqlite3.Error, json.JSONDecodeError) as error:
            raise OwnerPublishOperationStoreUnavailable() from error

    def save(self, operation_key: str, state: OwnerPublishOperationState, *, expected_stage: Literal["prepared"] | None) -> None:
        stage = "committed" if type(state) is OwnerPublishCommitted else "prepared"
        try:
            if operation_key != self.operation_key(
                state.org_id, state.agent_id, state.run_id, state.review_revision
            ):
                raise OwnerPublishOperationStoreUnavailable()
            with self._lock:
                key = self._keys.current()
                nonce = os.urandom(12)
                plaintext = _canonical(state.model_dump(mode="json"))
                ciphertext = AESGCM(key.key).encrypt(nonce, plaintext, _canonical([operation_key, state.operation_digest, stage, key.key_id]))
                with self._connect() as con:
                    con.execute("BEGIN IMMEDIATE")
                    self._fault("after_begin")
                    existing = con.execute("SELECT operation_digest,stage FROM owner_publish_operations WHERE operation_key=?", (operation_key,)).fetchone()
                    if expected_stage is None:
                        if existing is not None:
                            raise OwnerPublishOperationStoreUnavailable()
                        con.execute("INSERT INTO owner_publish_operations VALUES (?,?,?,?,?,?)", (operation_key, state.operation_digest, stage, key.key_id, b64encode(nonce).decode(), b64encode(ciphertext).decode()))
                        self._fault("after_insert")
                    else:
                        if existing != (state.operation_digest, expected_stage):
                            raise OwnerPublishOperationStoreUnavailable()
                        con.execute("UPDATE owner_publish_operations SET stage=?,key_id=?,nonce=?,ciphertext=? WHERE operation_key=?", (stage, key.key_id, b64encode(nonce).decode(), b64encode(ciphertext).decode(), operation_key))
                        self._fault("after_update")
                    self._fault("before_commit")
                    con.commit()
        except OwnerPublishOperationStoreUnavailable:
            raise
        except Exception as error:
            raise OwnerPublishOperationStoreUnavailable() from error


__all__ = ["OwnerPublishCommitted", "OwnerPublishOperationState", "OwnerPublishOperationStore", "OwnerPublishOperationStoreUnavailable", "OwnerPublishPrepared"]
