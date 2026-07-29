from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable

import pytest

from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    ProductionRegistryUserConflict,
    ProductionRegistryUserDenied,
    ProductionRegistryUserRevisionConflict,
    ProductionRegistryUserUnavailable,
    SqliteProductionRegistryUsers,
)


def _command(**changes: object) -> ProductionRegistryUserCommand:
    values: dict[str, object] = {
        "org_id": "acme",
        "principal_id": "root",
        "idempotency_key": "cmd-1",
        "expected_revision": 0,
        "user_id": "root",
        "email": "root@company.com",
        "manager_id": None,
    }
    values.update(changes)
    return ProductionRegistryUserCommand(**values)  # type: ignore[arg-type]


def _allow(command: ProductionRegistryUserCommand) -> CurrentUserRegistrationAuthorization:
    assert command.org_id == "acme"
    assert command.principal_id == "root"
    return CurrentUserRegistrationAuthorization(
        authority_epoch=1,
        policy_digest="b" * 64,
        evidence_digest="a" * 64,
    )


class _Authorizer:
    def __init__(
        self,
        current: Callable[
            [ProductionRegistryUserCommand], CurrentUserRegistrationAuthorization
        ] = _allow,
    ) -> None:
        self._current = current

    def current(
        self, command: ProductionRegistryUserCommand, transaction: sqlite3.Connection
    ) -> CurrentUserRegistrationAuthorization:
        _ = transaction
        return self._current(command)

    def verify_precommit(
        self,
        command: ProductionRegistryUserCommand,
        evidence: CurrentUserRegistrationAuthorization,
        transaction: sqlite3.Connection,
    ) -> bool:
        return self.current(command, transaction) == evidence


def _store(path: Path, **kwargs: object) -> SqliteProductionRegistryUsers:
    SqliteProductionRegistryUsers.migrate(path)
    return SqliteProductionRegistryUsers(path, authorize=_Authorizer(kwargs.pop("authorize", _allow)), **kwargs)  # type: ignore[arg-type]


def test_register는_revision_user_receipt_audit_outbox를_원자적으로_쓴다(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "registry.db")
    result = store.register(_command())
    assert result.revision == 1
    assert result.user.user_id == "root"
    assert store.revision("acme") == 1
    assert store.users("acme") == (result.user,)
    assert store.counts("acme") == {
        "receipts": 1,
        "audit": 1,
        "outbox": 1,
    }


def test_same_key_same_digest는_저장_result를_replay한다(tmp_path: Path) -> None:
    store = _store(tmp_path / "registry.db")
    first = store.register(_command())
    replay = store.register(_command())
    assert replay.user == first.user
    assert replay.revision == first.revision
    assert replay.replayed is True
    assert store.counts("acme")["receipts"] == 1


def test_same_key_different_digest는_conflict다(tmp_path: Path) -> None:
    store = _store(tmp_path / "registry.db")
    store.register(_command())
    with pytest.raises(ProductionRegistryUserConflict):
        store.register(_command(email="other@company.com"))


def test_expected_revision_manager_email불변식을_강제한다(tmp_path: Path) -> None:
    store = _store(tmp_path / "registry.db")
    store.register(_command())
    with pytest.raises(ProductionRegistryUserRevisionConflict):
        store.register(
            _command(
                idempotency_key="cmd-2",
                user_id="alice",
                email="alice@company.com",
                manager_id="root",
                expected_revision=0,
            )
        )
    with pytest.raises(ProductionRegistryUserConflict):
        store.register(
            _command(
                idempotency_key="cmd-3",
                user_id="alice",
                email="root@company.com",
                manager_id="root",
                expected_revision=1,
            )
        )


def test_authorization_denial은_write_0이다(tmp_path: Path) -> None:
    def deny(_command: ProductionRegistryUserCommand) -> CurrentUserRegistrationAuthorization:
        raise ProductionRegistryUserDenied()

    store = _store(tmp_path / "registry.db", authorize=deny)
    with pytest.raises(ProductionRegistryUserDenied):
        store.register(_command())
    assert store.revision("acme") == 0
    assert store.users("acme") == ()


def test_fault는_부분쓰기를_남기지_않는다(tmp_path: Path) -> None:
    def fault(point: str) -> None:
        if point == "after_user":
            raise RuntimeError("fault")

    store = _store(tmp_path / "registry.db", fault_injector=fault)
    with pytest.raises(RuntimeError):
        store.register(_command())
    assert store.revision("acme") == 0
    assert store.users("acme") == ()
    assert store.counts("acme") == {"receipts": 0, "audit": 0, "outbox": 0}


def test_email은_cross_org에서도_한_winner다(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    SqliteProductionRegistryUsers.migrate(path)

    def run(index: int) -> bool:
        org = f"org-{index}"
        store = SqliteProductionRegistryUsers(
            path,
            authorize=_Authorizer(lambda _command: CurrentUserRegistrationAuthorization(
                authority_epoch=1,
                policy_digest="b" * 64,
                evidence_digest="a" * 64,
            )),
        )
        try:
            store.register(
                _command(
                    org_id=org,
                    idempotency_key=f"command-{index}",
                    principal_id=f"admin-{index}",
                )
            )
            return True
        except ProductionRegistryUserConflict:
            return False

    with ThreadPoolExecutor(max_workers=32) as pool:
        assert sum(pool.map(run, range(32))) == 1


def test_authorization_snapshot_drift는_commit전_write0이다(tmp_path: Path) -> None:
    calls = 0

    def drift(_command: ProductionRegistryUserCommand) -> CurrentUserRegistrationAuthorization:
        nonlocal calls
        calls += 1
        return CurrentUserRegistrationAuthorization(
            authority_epoch=calls,
            policy_digest="b" * 64,
            evidence_digest="a" * 64,
        )

    store = _store(tmp_path / "registry.db", authorize=drift)
    with pytest.raises(ProductionRegistryUserDenied):
        store.register(_command())
    assert store.users("acme") == ()


def test_runtime_open은_partial_schema를_repair하지_않고_bytes를_보존한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE production_registry_users(x TEXT)")
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(ProductionRegistryUserUnavailable):
        SqliteProductionRegistryUsers(path, authorize=_Authorizer())
    assert path.read_bytes() == before


def test_receipt에는_email과_raw_evidence가_없다(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    store = _store(path)
    store.register(_command())
    connection = sqlite3.connect(path)
    for table in (
        "production_registry_user_command_receipts",
        "production_registry_user_audit",
        "production_registry_user_outbox",
    ):
        values = connection.execute(f"SELECT * FROM {table}").fetchall()
        assert "root@company.com" not in repr(values)
    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(production_registry_user_command_receipts)"
        )
    }
    assert "result_json" not in columns


def test_noop_trigger와_extra_component_object는_runtime에서_failclosed다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.db"
    SqliteProductionRegistryUsers.migrate(path)
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER production_registry_user_receipts_immutable")
    connection.execute(
        "CREATE TRIGGER production_registry_user_receipts_immutable "
        "BEFORE UPDATE ON production_registry_user_command_receipts BEGIN SELECT 1; END"
    )
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(ProductionRegistryUserUnavailable):
        SqliteProductionRegistryUsers(path, authorize=_Authorizer())
    assert path.read_bytes() == before
