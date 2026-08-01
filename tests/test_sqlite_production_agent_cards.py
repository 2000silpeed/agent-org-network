from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

import agent_org_network.sqlite_production_agent_cards as card_store_module
from agent_org_network.central_operational_evidence import (
    migrate_central_operational_evidence_schema,
)
from agent_org_network.sqlite_production_agent_cards import (
    CurrentCardRegistrationAuthorization,
    ProductionAgentCardCommand,
    ProductionAgentCardConflict,
    ProductionAgentCardUnavailable,
    SqliteProductionAgentCards,
)
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    SqliteProductionRegistryUsers,
)


class _UserAuthorizer:
    def current(self, command: object, transaction: sqlite3.Connection) -> CurrentUserRegistrationAuthorization:
        _ = command, transaction
        return CurrentUserRegistrationAuthorization(
            authority_epoch=1, policy_digest="b" * 64, evidence_digest="a" * 64
        )

    def verify_precommit(
        self, command: object, evidence: object, transaction: sqlite3.Connection
    ) -> bool:
        _ = command, evidence, transaction
        return True


class _CardAuthorizer:
    def current(self, command: object, transaction: sqlite3.Connection) -> CurrentCardRegistrationAuthorization:
        _ = command, transaction
        return CurrentCardRegistrationAuthorization(
            authority_epoch=1, policy_digest="d" * 64, evidence_digest="c" * 64
        )

    def verify_precommit(
        self, command: object, evidence: object, transaction: sqlite3.Connection
    ) -> bool:
        _ = command, evidence, transaction
        return True


def _card(agent_id: str = "support") -> dict[str, object]:
    return {
        "agent_id": agent_id,
        "owner": "root",
        "team": "support",
        "summary": "Support card",
        "domains": ["support"],
        "last_reviewed_at": "2026-07-27",
        "maintainer": None,
        "can_answer": ["refund"],
        "cannot_answer": [],
        "approval_when": [],
        "collaborate_when": [],
        "knowledge_sources": ["okf"],
        "trust_labels": ["internal"],
    }


def _command(**changes: object) -> ProductionAgentCardCommand:
    values: dict[str, object] = {
        "org_id": "acme",
        "principal_id": "root",
        "idempotency_key": "card-command-1",
        "expected_revision": 1,
        "card": _card(),
    }
    values.update(changes)
    return ProductionAgentCardCommand(**values)  # type: ignore[arg-type]


def _database(path: Path, *, org_id: str = "acme") -> SqliteProductionAgentCards:
    SqliteProductionRegistryUsers.migrate(path)
    users = SqliteProductionRegistryUsers(path, authorize=_UserAuthorizer())
    users.register(
        ProductionRegistryUserCommand(
            org_id=org_id,
            principal_id="root",
            idempotency_key=f"user-{org_id}",
            expected_revision=0,
            user_id="root",
            email=f"root@{org_id}.example",
        )
    )
    SqliteProductionAgentCards.migrate(path)
    return SqliteProductionAgentCards(path, authorize=_CardAuthorizer())


def _enable_v19(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE aon_installation_schema"
            "(name TEXT PRIMARY KEY,version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO aon_installation_schema VALUES ('central-installation',18)"
        )
    migrate_central_operational_evidence_schema(path)


def _v19_database(path: Path) -> SqliteProductionAgentCards:
    SqliteProductionRegistryUsers.migrate(path)
    _enable_v19(path)
    users = SqliteProductionRegistryUsers(path, authorize=_UserAuthorizer())
    users.register(
        ProductionRegistryUserCommand(
            org_id="acme",
            principal_id="root",
            idempotency_key="user-acme",
            expected_revision=0,
            user_id="root",
            email="root@acme.example",
        )
    )
    SqliteProductionAgentCards.migrate(path)
    return SqliteProductionAgentCards(path, authorize=_CardAuthorizer())


def test_v19_card_registration_appends_canonical_evidence_and_replay_is_write_zero(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.db"
    store = _v19_database(path)

    result = store.register(_command())
    replay = store.register(_command())

    assert result.replayed is False
    assert replay.replayed is True
    with sqlite3.connect(path) as connection:
        authorities = connection.execute(
            "SELECT policy_revision_id,policy_epoch,policy_digest "
            "FROM central_operational_audit_records ORDER BY receipt_id"
        ).fetchall()
        assert (f"yaml:{'d' * 64}", 1, "d" * 64) in authorities
        assert connection.execute(
            "SELECT COUNT(*) FROM central_operational_audit_records"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM central_operational_event_intents"
        ).fetchone() == (2,)


@pytest.mark.parametrize("failure", ("append", "catalog_missing", "catalog_drift"))
def test_v19_card_evidence_failure_rolls_back_source_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    path = tmp_path / f"{failure}.db"
    store = _v19_database(path)
    if failure == "append":
        def fail_append(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("append fault")

        monkeypatch.setattr(
            card_store_module,
            "append_committed_source_evidence_if_v19",
            fail_append,
        )
    else:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "DROP TABLE central_operational_retention_receipts"
                if failure == "catalog_missing"
                else "DROP INDEX central_operational_audit_org_time"
            )

    with pytest.raises(Exception):
        store.register(_command())

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT revision FROM production_registry_revisions WHERE org_id='acme'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM production_agent_cards"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM production_agent_card_command_receipts"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM production_agent_card_audit"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM production_agent_card_outbox"
        ).fetchone() == (0,)


def test_canonical_catalog_migration_is_restart_safe_and_fault_atomic(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    SqliteProductionRegistryUsers.migrate(path)

    def crash(point: str) -> None:
        if point == "mid-DDL":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError):
        SqliteProductionAgentCards.migrate(path, fault_injector=crash)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='production_agent_cards'"
        ).fetchone() is None

    SqliteProductionAgentCards.migrate(path)
    SqliteProductionAgentCards.migrate(path)
    assert SqliteProductionAgentCards(path, authorize=_CardAuthorizer()).cards("acme") == ()


@pytest.mark.parametrize(
    "ddl",
    (
        "CREATE TABLE production_agent_cards (broken TEXT)",
        "CREATE TABLE production_agent_card_shadow (secret TEXT)",
    ),
)
def test_migration_never_repairs_partial_or_extra_card_catalog(tmp_path: Path, ddl: str) -> None:
    path = tmp_path / "registry.db"
    SqliteProductionRegistryUsers.migrate(path)
    with sqlite3.connect(path) as connection:
        connection.execute(ddl)
    before = path.read_bytes()
    with pytest.raises(ProductionAgentCardUnavailable):
        SqliteProductionAgentCards.migrate(path)
    assert path.read_bytes() == before


def test_register는_O2a_revision과_card_receipt_audit_outbox를_같이_commit한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.db"
    store = _database(path)
    result = store.register(_command())
    assert result.revision == 2
    assert result.card.agent_id == "support"
    assert store.revision("acme") == 2
    assert store.cards("acme") == (result.card,)
    assert store.counts("acme") == {"receipts": 1, "audit": 1, "outbox": 1}

    connection = sqlite3.connect(path)
    card_json = connection.execute(
        "SELECT card_json FROM production_agent_cards"
    ).fetchone()[0]
    assert '"summary":"Support card"' in card_json
    for table in (
        "production_agent_card_command_receipts",
        "production_agent_card_audit",
        "production_agent_card_outbox",
    ):
        assert "Support card" not in repr(connection.execute(f"SELECT * FROM {table}").fetchall())


def test_register는_existing_O2a_user를_admit_card참조로_검증한다(tmp_path: Path) -> None:
    store = _database(tmp_path / "registry.db")
    with pytest.raises(ProductionAgentCardConflict):
        store.register(
            _command(
                card={**_card(), "owner": "ghost"},
            )
        )
    assert store.cards("acme") == ()


def test_same_command_32way는_single_write와_replay다(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    _database(path)

    def run(_index: int) -> bool:
        result = SqliteProductionAgentCards(path, authorize=_CardAuthorizer()).register(_command())
        return result.replayed

    with ThreadPoolExecutor(max_workers=32) as pool:
        replayed = list(pool.map(run, range(32)))
    assert replayed.count(False) == 1
    assert replayed.count(True) == 31


def test_same_agent_id는_org_scope다(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    store = _database(path)
    users = SqliteProductionRegistryUsers(path, authorize=_UserAuthorizer())
    users.register(
        ProductionRegistryUserCommand(
            org_id="beta",
            principal_id="root",
            idempotency_key="user-beta",
            expected_revision=0,
            user_id="root",
            email="root@beta.example",
        )
    )
    store.register(_command())
    result = store.register(
        _command(
            org_id="beta",
            idempotency_key="card-beta",
            expected_revision=1,
        )
    )
    assert result.card.agent_id == "support"
    assert len(store.cards("acme")) == len(store.cards("beta")) == 1


@pytest.mark.parametrize("field", ["roles", "authority", "permissions"])
def test_card의_권한자기보고_extra는_거부한다(field: str) -> None:
    with pytest.raises(ValidationError):
        _command(card={**_card(), field: ["admin"]})


@pytest.mark.parametrize(
    "point",
    [
        "after_card",
        "after_revision",
        "after_receipt",
        "after_audit",
        "after_outbox",
        "before_commit",
    ],
)
def test_fault는_card_revision_receipt_audit_outbox_부분쓰기를_남기지_않는다(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / "registry.db"
    _database(path)

    def fault(actual: str) -> None:
        if actual == point:
            raise RuntimeError("fault")

    store = SqliteProductionAgentCards(
        path, authorize=_CardAuthorizer(), fault_injector=fault
    )
    with pytest.raises(RuntimeError):
        store.register(_command())
    assert store.revision("acme") == 1
    assert store.cards("acme") == ()
    assert store.counts("acme") == {"receipts": 0, "audit": 0, "outbox": 0}


def test_card_json_tamper는_reader에서_failclosed다(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    store = _database(path)
    store.register(_command())
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE production_agent_cards SET card_json=replace(card_json,'Support card','tampered')"
    )
    connection.commit()
    connection.close()
    with pytest.raises(ProductionAgentCardUnavailable):
        store.cards("acme")


def test_runtime_open은_extra_component_object를_repair하지_않는다(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    _database(path)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE production_agent_card_shadow(secret TEXT)")
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(ProductionAgentCardUnavailable):
        SqliteProductionAgentCards(path, authorize=_CardAuthorizer())
    assert path.read_bytes() == before


def test_O2a_parent_schema_drift는_open과_reader에서_failclosed다(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    store = _database(path)
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER production_registry_user_receipts_immutable")
    connection.execute(
        "CREATE TRIGGER production_registry_user_receipts_immutable "
        "BEFORE UPDATE ON production_registry_user_command_receipts BEGIN SELECT 1; END"
    )
    connection.commit()
    connection.close()
    with pytest.raises(ProductionAgentCardUnavailable):
        SqliteProductionAgentCards(path, authorize=_CardAuthorizer())
    with pytest.raises(ProductionAgentCardUnavailable):
        store.cards("acme")


@pytest.mark.parametrize(
    "table",
    ["production_agent_card_audit", "production_agent_card_outbox"],
)
def test_audit_outbox는_update_delete불가다(tmp_path: Path, table: str) -> None:
    path = tmp_path / "registry.db"
    store = _database(path)
    store.register(_command())
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(f"UPDATE {table} SET result_revision=99")


def test_replay는_companion_missing_mismatch를_failclosed한다(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    store = _database(path)
    store.register(_command())
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER production_agent_card_outbox_no_delete")
    connection.execute("DELETE FROM production_agent_card_outbox")
    connection.commit()
    connection.close()
    with pytest.raises(ProductionAgentCardUnavailable):
        store.register(_command())


def test_replay는_receipt_authority_snapshot_tamper를_failclosed한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.db"
    store = _database(path)
    store.register(_command())
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER production_agent_card_receipts_immutable")
    connection.execute(
        "UPDATE production_agent_card_command_receipts SET policy_digest=?",
        ("e" * 64,),
    )
    connection.execute(
        "CREATE TRIGGER production_agent_card_receipts_immutable "
        "BEFORE UPDATE ON production_agent_card_command_receipts "
        "BEGIN SELECT RAISE(ABORT,'immutable'); END"
    )
    connection.commit()
    connection.close()
    with pytest.raises(ProductionAgentCardUnavailable):
        store.register(_command())


def test_receipt는_commit_authority_snapshot과_resource_fingerprint를_보존한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "registry.db"
    store = _database(path)
    store.register(_command())
    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT authority_epoch,policy_digest,evidence_digest,resource_fingerprint "
        "FROM production_agent_card_command_receipts"
    ).fetchone()
    assert row[0] == 1
    assert row[1:] == ("d" * 64, "c" * 64, row[3])
    assert len(row[3]) == 64


def test_same_key는_server_review_date가달라도_first_authoritative_card를_replay한다(
    tmp_path: Path,
) -> None:
    store = _database(tmp_path / "registry.db")
    first = store.register(_command(card=_card()))
    next_day = {**_card(), "last_reviewed_at": "2026-07-28"}
    replay = store.register(_command(card=next_day))
    assert replay.replayed is True
    assert replay.card == first.card
