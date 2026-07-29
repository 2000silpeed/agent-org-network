import os
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest

from agent_org_network.owner_authoring_operation_store import (
    OwnerAuthoringOperationState,
    OwnerAuthoringOperationStore,
    OwnerAuthoringOperationStoreUnavailable,
)
from agent_org_network.owner_local_authoring_repository import OwnerLocalAuthoringKey


class _Keys:
    def __init__(self, key: bytes = b"k" * 32) -> None:
        self.key = key

    def current(self) -> OwnerLocalAuthoringKey:
        return OwnerLocalAuthoringKey(key_id="key-1", key=self.key)


def _started() -> OwnerAuthoringOperationState:
    return OwnerAuthoringOperationState(
        identity_digest="a" * 64,
        stage="started",
        payload={"start_command": {"id": "command"}, "run": {"id": "run"}},
    )


def _draft() -> OwnerAuthoringOperationState:
    return OwnerAuthoringOperationState(
        identity_digest="a" * 64,
        stage="draft_ready",
        payload={
            "start_command": {},
            "run": {},
            "draft_ref": {},
            "draft_bundle_base64": "eA==",
            "complete_command": {},
        },
    )


def test_store는_0600_encrypted_row와_exact_replay를보장한다(tmp_path: Path) -> None:
    path = tmp_path / "operations.sqlite"
    store = OwnerAuthoringOperationStore(path, keys=_Keys())
    key = store.operation_key("acme", "support", "command-1")
    store.save(key, _started(), expected_stage=None)
    assert store.load(key, "a" * 64) == _started()
    assert path.stat().st_mode & 0o077 == 0
    disk = path.read_bytes()
    assert b'"start_command"' not in disk
    assert b'"command"' not in disk


def test_symlink_wrong_key_tamper_schema_drift는_failclosed다(tmp_path: Path) -> None:
    outside = tmp_path / "outside.sqlite"
    outside.touch()
    link = tmp_path / "link.sqlite"
    os.symlink(outside, link)
    with pytest.raises(OwnerAuthoringOperationStoreUnavailable):
        OwnerAuthoringOperationStore(link, keys=_Keys())

    path = tmp_path / "operations.sqlite"
    store = OwnerAuthoringOperationStore(path, keys=_Keys())
    key = store.operation_key("acme", "support", "command-1")
    store.save(key, _started(), expected_stage=None)
    with pytest.raises(OwnerAuthoringOperationStoreUnavailable):
        OwnerAuthoringOperationStore(path, keys=_Keys(b"x" * 32)).load(
            key, "a" * 64
        )
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE owner_authoring_operations SET ciphertext='AAAA' "
                "WHERE operation_key=?",
                (key,),
            )
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE extra(value TEXT) STRICT")
        connection.commit()
    with pytest.raises(OwnerAuthoringOperationStoreUnavailable):
        store.load(key, "a" * 64)


def test_identity와_stage는_DB_trigger와_CAS가불변으로지킨다(tmp_path: Path) -> None:
    path = tmp_path / "operations.sqlite"
    store = OwnerAuthoringOperationStore(path, keys=_Keys())
    key = store.operation_key("acme", "support", "command-1")
    store.save(key, _started(), expected_stage=None)
    with pytest.raises(OwnerAuthoringOperationStoreUnavailable):
        store.save(key, _started(), expected_stage=None)
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE owner_authoring_operations SET identity_digest=?",
                ("b" * 64,),
            )


def test_same_name_weak_trigger와_post_init_regular_swap은_read_write0이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "operations.sqlite"
    store = OwnerAuthoringOperationStore(path, keys=_Keys())
    key = store.operation_key("acme", "support", "command-1")
    store.save(key, _started(), expected_stage=None)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER owner_authoring_operations_no_delete")
        connection.execute(
            "CREATE TRIGGER owner_authoring_operations_no_delete "
            "BEFORE DELETE ON owner_authoring_operations BEGIN SELECT 1; END"
        )
        connection.commit()
    with pytest.raises(OwnerAuthoringOperationStoreUnavailable):
        store.load(key, "a" * 64)

    original = tmp_path / "original.sqlite"
    os.replace(path, original)
    os.symlink(original, path)
    with pytest.raises(OwnerAuthoringOperationStoreUnavailable):
        store.load(key, "a" * 64)
    path.unlink()
    OwnerAuthoringOperationStore(path, keys=_Keys())
    with pytest.raises(OwnerAuthoringOperationStoreUnavailable):
        store.load(key, "a" * 64)
    with pytest.raises(OwnerAuthoringOperationStoreUnavailable):
        store.save(key, _started(), expected_stage=None)


def test_weakened_check_table_sql은_canonical_catalog에서거부된다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "operations.sqlite"
    store = OwnerAuthoringOperationStore(path, keys=_Keys())
    key = store.operation_key("acme", "support", "command-1")
    store.save(key, _started(), expected_stage=None)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "DROP TRIGGER owner_authoring_operations_no_delete;"
            "DROP TRIGGER owner_authoring_operations_exact_update;"
            "ALTER TABLE owner_authoring_operations RENAME TO old_operations;"
            "CREATE TABLE owner_authoring_operations ("
            "operation_key TEXT PRIMARY KEY,identity_digest TEXT NOT NULL,"
            "stage TEXT NOT NULL,key_id TEXT NOT NULL,nonce TEXT NOT NULL,"
            "ciphertext TEXT NOT NULL,CHECK(1)) STRICT;"
            "INSERT INTO owner_authoring_operations SELECT * FROM old_operations;"
            "DROP TABLE old_operations;"
            "CREATE TRIGGER owner_authoring_operations_no_delete "
            "BEFORE DELETE ON owner_authoring_operations "
            "BEGIN SELECT RAISE(ABORT,'immutable'); END;"
            "CREATE TRIGGER owner_authoring_operations_exact_update "
            "BEFORE UPDATE ON owner_authoring_operations "
            "WHEN OLD.operation_key != NEW.operation_key "
            "OR OLD.identity_digest != NEW.identity_digest "
            "OR NOT ((OLD.stage='started' AND NEW.stage='draft_ready') "
            "OR (OLD.stage='draft_ready' AND NEW.stage='completed')) "
            "BEGIN SELECT RAISE(ABORT,'invalid transition'); END;"
        )
    with pytest.raises(OwnerAuthoringOperationStoreUnavailable):
        store.load(key, "a" * 64)


@pytest.mark.parametrize("point", ["after_begin", "after_insert", "before_commit"])
def test_insert_fault는_transaction을_rollback한다(tmp_path: Path, point: str) -> None:
    path = tmp_path / "operations.sqlite"

    def fault(current: str) -> None:
        if current == point:
            raise RuntimeError("crash")

    store = OwnerAuthoringOperationStore(path, keys=_Keys(), fault=fault)
    key = store.operation_key("acme", "support", "command-1")
    with pytest.raises(OwnerAuthoringOperationStoreUnavailable):
        store.save(key, _started(), expected_stage=None)
    assert OwnerAuthoringOperationStore(path, keys=_Keys()).load(
        key, "a" * 64
    ) is None


@pytest.mark.parametrize("point", ["after_update", "before_commit"])
def test_update_fault는_started를보존한다(tmp_path: Path, point: str) -> None:
    path = tmp_path / "operations.sqlite"
    base = OwnerAuthoringOperationStore(path, keys=_Keys())
    key = base.operation_key("acme", "support", "command-1")
    base.save(key, _started(), expected_stage=None)

    def fault(current: str) -> None:
        if current == point:
            raise RuntimeError("crash")

    crashing = OwnerAuthoringOperationStore(path, keys=_Keys(), fault=fault)
    with pytest.raises(OwnerAuthoringOperationStoreUnavailable):
        crashing.save(key, _draft(), expected_stage="started")
    assert OwnerAuthoringOperationStore(path, keys=_Keys()).load(
        key, "a" * 64
    ) == _started()


def test_same_operation_32thread는_exact_one_winner_partial0이다(tmp_path: Path) -> None:
    path = tmp_path / "operations.sqlite"
    OwnerAuthoringOperationStore(path, keys=_Keys())
    key = OwnerAuthoringOperationStore.operation_key(
        "acme", "support", "command-1"
    )
    barrier = Barrier(32)
    entered = 0
    entered_lock = Lock()

    def attempt(_index: int) -> bool:
        nonlocal entered
        with entered_lock:
            entered += 1
        barrier.wait()
        try:
            OwnerAuthoringOperationStore(path, keys=_Keys()).save(
                key, _started(), expected_stage=None
            )
            return True
        except OwnerAuthoringOperationStoreUnavailable:
            return False

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(attempt, range(32)))
    assert entered == 32
    assert results.count(True) == 1
    assert results.count(False) == 31
    assert OwnerAuthoringOperationStore(path, keys=_Keys()).load(
        key, "a" * 64
    ) == _started()


def test_A_B_A_path_swap은_connected_DB_FD_attestation으로_write0이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "operations.sqlite"
    base = OwnerAuthoringOperationStore(path, keys=_Keys())
    key = base.operation_key("acme", "support", "command-1")
    base.save(key, _started(), expected_stage=None)
    replacement = tmp_path / "replacement.sqlite"
    OwnerAuthoringOperationStore(replacement, keys=_Keys())
    held = tmp_path / "held-a.sqlite"
    returned_b = tmp_path / "returned-b.sqlite"
    armed = False

    def before_connect() -> None:
        if armed:
            os.replace(path, held)
            os.replace(replacement, path)

    def after_connect() -> None:
        if armed:
            os.replace(path, returned_b)
            os.replace(held, path)

    guarded = OwnerAuthoringOperationStore(
        path,
        keys=_Keys(),
        before_connect_hook=before_connect,
        connect_hook=after_connect,
    )
    armed = True
    with pytest.raises(OwnerAuthoringOperationStoreUnavailable):
        guarded.save(key, _started(), expected_stage=None)
    assert OwnerAuthoringOperationStore(path, keys=_Keys()).load(
        key, "a" * 64
    ) == _started()
    assert OwnerAuthoringOperationStore(returned_b, keys=_Keys()).load(
        key, "a" * 64
    ) is None
