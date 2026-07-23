from __future__ import annotations
# pyright: reportArgumentType=false

import sqlite3
import threading
from pathlib import Path

import pytest

import agent_org_network.sqlite_durable_tenant_operational_mutations as sqlite_durable_tenant_operational_mutations
from agent_org_network.sqlite_durable_tenant_operational_mutations import (
    capture_sqlite_tenant_operational_mutation_scope_snapshot,
    migrate_sqlite_durable_tenant_operational_mutations,
)
from agent_org_network.sqlite_durable_tenant_operational_authorization import (
    migrate_sqlite_durable_tenant_operational_authorization,
)
from agent_org_network.sqlite_operational_tenant_sources import (
    migrate_sqlite_operational_tenant_sources,
)
from agent_org_network.sqlite_tenant_operational_mutation_uow import (
    CardRegisterCommand,
    CardTransferOwnerCommand,
    HitlWriteCommand,
    SessionEndCommand,
    SqliteTenantOperationalMutationUowError,
    TenantOperationalAuthorizationBinding,
    execute_sqlite_tenant_operational_mutation,
)
from agent_org_network.sqlite_tenant_port_audit_v2 import migrate_sqlite_tenant_port_audit_v2


def _connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    migrate_sqlite_operational_tenant_sources(connection)
    migrate_sqlite_tenant_port_audit_v2(connection)
    migrate_sqlite_durable_tenant_operational_mutations(connection)
    connection.execute(
        "INSERT INTO operational_registry_state VALUES(?,0,?,?,?)",
        (
            "acme",
            '{"cards":{},"manager_refs":{},"users":["one","two"]}',
            "d" * 64,
            "2026-01-01T00:00:00.000Z",
        ),
    )
    connection.execute(
        "INSERT INTO operational_sessions VALUES(?,?,?,'active',?,?,0)",
        ("acme", "s", "u", "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
    )
    connection.commit()
    return connection


def _base(connection: sqlite3.Connection) -> dict[str, object]:
    return {
        "org_id": "acme",
        "command_id": "c1",
        "principal_id": "p",
        "expected_scope": capture_sqlite_tenant_operational_mutation_scope_snapshot(connection),
        "created_at": "2026-01-01T00:00:01.000Z",
    }


def _binding() -> TenantOperationalAuthorizationBinding:
    return TenantOperationalAuthorizationBinding(
        '{"kind":"agent_card","org_id":"acme","owner_subject_id":"one","resource_id":"card"}',
        '{"kind":"agent_card","org_id":"acme","owner_subject_id":"one","resource_id":"card"}',
        "a" * 64,
        "b" * 64,
        "evidence",
        "approver",
        "d" * 64,
        "e" * 64,
        "2026-01-01T00:00:00.000Z",
    )


@pytest.mark.parametrize("kind", ["register", "transfer", "session", "hitl"])
def test_after_evidence_fault_rolls_back_state_receipt_evidence_audit_and_intents(
    tmp_path: Path, kind: str
) -> None:
    connection = _connection(tmp_path / f"evidence-{kind}.sqlite")
    try:
        migrate_sqlite_durable_tenant_operational_authorization(connection)
        if kind == "register":
            command = CardRegisterCommand(**_base(connection), card_id="card", owner_id="one")
        elif kind == "transfer":
            connection.execute(
                'UPDATE operational_registry_state SET payload_json=\'{"cards":{"card":{"owner":"one"}},"manager_refs":{},"users":["one","two"]}\' WHERE org_id=\'acme\''
            )
            connection.commit()
            command = CardTransferOwnerCommand(**_base(connection), card_id="card", owner_id="two")
        elif kind == "session":
            command = SessionEndCommand(**_base(connection), session_id="s")
        else:
            command = HitlWriteCommand(**_base(connection), card_id="card", on=True)
        registry_before = connection.execute(
            "SELECT revision,payload_json,payload_digest,updated_at FROM operational_registry_state WHERE org_id='acme'"
        ).fetchone()
        session_before = connection.execute(
            "SELECT status,last_active_at,revision FROM operational_sessions WHERE org_id='acme' AND session_id='s'"
        ).fetchone()
        hitl_before = connection.execute(
            "SELECT \"on\",explicit,revision,updated_at FROM operational_hitl_toggles WHERE org_id='acme' AND agent_id='card'"
        ).fetchone()
        with pytest.raises(RuntimeError, match="after_evidence"):
            execute_sqlite_tenant_operational_mutation(
                connection,
                command,
                authorization_binding=_binding(),
                fault_injector=lambda point: (
                    (_ for _ in ()).throw(RuntimeError(point))
                    if point == "after_evidence"
                    else None
                ),
            )
        for table in (
            "durable_tenant_operational_mutation_receipts",
            "durable_tenant_operational_authorization_evidence",
            "operational_audit_events_v2",
            "durable_tenant_operational_mutation_audit_intents",
            "durable_tenant_operational_mutation_outbox_intents",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert (
            connection.execute(
                "SELECT revision,payload_json,payload_digest,updated_at FROM operational_registry_state WHERE org_id='acme'"
            ).fetchone()
            == registry_before
        )
        assert (
            connection.execute(
                "SELECT status,last_active_at,revision FROM operational_sessions WHERE org_id='acme' AND session_id='s'"
            ).fetchone()
            == session_before
        )
        assert (
            connection.execute(
                "SELECT \"on\",explicit,revision,updated_at FROM operational_hitl_toggles WHERE org_id='acme' AND agent_id='card'"
            ).fetchone()
            == hitl_before
        )
    finally:
        connection.close()


def test_register_transfer_session_and_hitl_commit_with_receipt_audit_outbox(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path / "uow.sqlite")
    try:
        original = CardRegisterCommand(**_base(connection), card_id="card", owner_id="one")
        first = execute_sqlite_tenant_operational_mutation(connection, original)
        assert first.replayed is False
        assert execute_sqlite_tenant_operational_mutation(connection, original).replayed is True
        assert (
            connection.execute(
                "SELECT count(*) FROM durable_tenant_operational_mutation_receipts"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM operational_audit_events_v2").fetchone()[0]
            == 1
        )
        transfer = _base(connection) | {"command_id": "c2"}
        execute_sqlite_tenant_operational_mutation(
            connection, CardTransferOwnerCommand(**transfer, card_id="card", owner_id="two")
        )
        end = _base(connection) | {"command_id": "c3"}
        execute_sqlite_tenant_operational_mutation(
            connection, SessionEndCommand(**end, session_id="s")
        )
        hitl = _base(connection) | {"command_id": "c4"}
        execute_sqlite_tenant_operational_mutation(
            connection, HitlWriteCommand(**hitl, card_id="card", on=True)
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM durable_tenant_operational_mutation_receipts"
            ).fetchone()[0]
            == 4
        )
        assert (
            connection.execute("SELECT count(*) FROM operational_audit_events_v2").fetchone()[0]
            == 4
        )
    finally:
        connection.close()


def test_stale_scope_cross_command_and_fault_roll_back_everything(tmp_path: Path) -> None:
    connection = _connection(tmp_path / "uow.sqlite")
    try:
        command = CardRegisterCommand(**_base(connection), card_id="card", owner_id="one")
        connection.execute(
            "UPDATE operational_sessions SET revision=1 WHERE org_id='acme' AND session_id='s'"
        )
        connection.commit()
        with pytest.raises(SqliteTenantOperationalMutationUowError):
            execute_sqlite_tenant_operational_mutation(connection, command)
        command = CardRegisterCommand(**_base(connection), card_id="card", owner_id="one")
        with pytest.raises(RuntimeError, match="after_receipt"):
            execute_sqlite_tenant_operational_mutation(
                connection,
                command,
                fault_injector=lambda point: (
                    (_ for _ in ()).throw(RuntimeError(point)) if point == "after_receipt" else None
                ),
            )
        assert (
            connection.execute(
                "SELECT count(*) FROM durable_tenant_operational_mutation_receipts"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM operational_audit_events_v2").fetchone()[0]
            == 0
        )
        assert (
            '"card":{"owner"'
            not in connection.execute(
                "SELECT payload_json FROM operational_registry_state WHERE org_id='acme'"
            ).fetchone()[0]
        )
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ["register", "transfer", "session", "hitl"])
@pytest.mark.parametrize(
    "point",
    [
        "after_state",
        "after_receipt",
        "after_audit",
        "after_outbox",
        "after_readback",
        "before_commit",
    ],
)
def test_every_fault_point_rolls_back_each_action(tmp_path: Path, kind: str, point: str) -> None:
    connection = _connection(tmp_path / f"{kind}-{point}.sqlite")
    try:
        if kind == "register":
            command = CardRegisterCommand(**_base(connection), card_id="card", owner_id="one")
        elif kind == "transfer":
            connection.execute(
                'UPDATE operational_registry_state SET payload_json=\'{"cards":{"card":{"owner":"one"}},"manager_refs":{},"users":["one","two"]}\' WHERE org_id=\'acme\''
            )
            connection.commit()
            command = CardTransferOwnerCommand(**_base(connection), card_id="card", owner_id="two")
        elif kind == "session":
            command = SessionEndCommand(**_base(connection), session_id="s")
        else:
            command = HitlWriteCommand(**_base(connection), card_id="card", on=True)
        registry_before = connection.execute(
            "SELECT revision,payload_json,payload_digest,updated_at FROM operational_registry_state WHERE org_id='acme'"
        ).fetchone()
        session_before = connection.execute(
            "SELECT status,last_active_at,revision FROM operational_sessions WHERE org_id='acme' AND session_id='s'"
        ).fetchone()
        hitl_before = connection.execute(
            "SELECT \"on\",explicit,revision,updated_at FROM operational_hitl_toggles WHERE org_id='acme' AND agent_id='card'"
        ).fetchone()
        with pytest.raises(RuntimeError, match=point):
            execute_sqlite_tenant_operational_mutation(
                connection,
                command,
                fault_injector=lambda actual: (
                    (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
                ),
            )
        assert (
            connection.execute(
                "SELECT count(*) FROM durable_tenant_operational_mutation_receipts"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM durable_tenant_operational_mutation_audit_intents"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM durable_tenant_operational_mutation_outbox_intents"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM operational_audit_events_v2").fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT revision,payload_json,payload_digest,updated_at FROM operational_registry_state WHERE org_id='acme'"
            ).fetchone()
            == registry_before
        )
        assert (
            connection.execute(
                "SELECT status,last_active_at,revision FROM operational_sessions WHERE org_id='acme' AND session_id='s'"
            ).fetchone()
            == session_before
        )
        assert (
            connection.execute(
                "SELECT \"on\",explicit,revision,updated_at FROM operational_hitl_toggles WHERE org_id='acme' AND agent_id='card'"
            ).fetchone()
            == hitl_before
        )
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ["register", "transfer", "session", "hitl"])
def test_same_effect_without_receipt_is_conflict(tmp_path: Path, kind: str) -> None:
    connection = _connection(tmp_path / f"same-{kind}.sqlite")
    try:
        if kind == "register":
            connection.execute(
                'UPDATE operational_registry_state SET payload_json=\'{"cards":{"card":{"owner":"one"}},"manager_refs":{},"users":["one","two"]}\' WHERE org_id=\'acme\''
            )
            command = CardRegisterCommand(**_base(connection), card_id="card", owner_id="one")
        elif kind == "transfer":
            connection.execute(
                'UPDATE operational_registry_state SET payload_json=\'{"cards":{"card":{"owner":"two"}},"manager_refs":{},"users":["one","two"]}\' WHERE org_id=\'acme\''
            )
            command = CardTransferOwnerCommand(**_base(connection), card_id="card", owner_id="two")
        elif kind == "session":
            connection.execute(
                "UPDATE operational_sessions SET status='ended',revision=1 WHERE org_id='acme' AND session_id='s'"
            )
            command = SessionEndCommand(**_base(connection), session_id="s")
        else:
            connection.execute(
                "INSERT INTO operational_hitl_toggles VALUES('acme','card',1,1,0,'2026-01-01T00:00:00.000Z')"
            )
            command = HitlWriteCommand(**_base(connection), card_id="card", on=True)
        connection.commit()
        # Refresh only the scope captured before external state: it must not
        # turn an existing effect into a successful replay without a receipt.
        if kind == "register":
            command = CardRegisterCommand(**_base(connection), card_id="card", owner_id="one")
        elif kind == "transfer":
            command = CardTransferOwnerCommand(**_base(connection), card_id="card", owner_id="two")
        elif kind == "session":
            command = SessionEndCommand(**_base(connection), session_id="s")
        else:
            command = HitlWriteCommand(**_base(connection), card_id="card", on=True)
        with pytest.raises(SqliteTenantOperationalMutationUowError):
            execute_sqlite_tenant_operational_mutation(connection, command)
    finally:
        connection.close()


def test_eight_connections_same_transfer_replay_once(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite"
    bootstrap = _connection(path)
    try:
        bootstrap.execute(
            'UPDATE operational_registry_state SET payload_json=\'{"cards":{"card":{"owner":"one"}},"manager_refs":{},"users":["one","two"]}\' WHERE org_id=\'acme\''
        )
        bootstrap.commit()
        command = CardTransferOwnerCommand(**_base(bootstrap), card_id="card", owner_id="two")
    finally:
        bootstrap.close()
    barrier = threading.Barrier(8)
    outcomes: list[object] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def contender() -> None:
        # CI 저사양 러너에서 8커넥션 락 큐 대기가 5s를 넘어 R1.0 검증이
        # OperationalError→unavailable로 오탐된 flake(2026-07-22 run 29892932953).
        # 대기 여유만 늘린다 — 1 winner + 7 exact replay 단언은 불변.
        connection = sqlite3.connect(path, timeout=30)
        try:
            barrier.wait(timeout=5)
            result = execute_sqlite_tenant_operational_mutation(connection, command)
            with lock:
                outcomes.append(result)
        except BaseException as error:  # pragma: no cover - reported below
            with lock:
                failures.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=contender) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert failures == []
    assert len(outcomes) == 8
    assert sum(getattr(value, "replayed", None) is False for value in outcomes) == 1
    assert sum(getattr(value, "replayed", None) is True for value in outcomes) == 7
    verify = sqlite3.connect(path)
    try:
        assert (
            verify.execute(
                "SELECT revision,payload_json FROM operational_registry_state WHERE org_id='acme'"
            ).fetchone()[0]
            == 1
        )
        for table in (
            "durable_tenant_operational_mutation_receipts",
            "durable_tenant_operational_mutation_audit_intents",
            "durable_tenant_operational_mutation_outbox_intents",
            "operational_audit_events_v2",
        ):
            assert verify.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    finally:
        verify.close()


def test_replay_survives_a_committed_unrelated_source_write_between_open_and_validate_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic trace-based reproduction of the 8-way replay flake.

    open() captures one R1.0 scope snapshot; validate_only() re-captures a
    second one to compare against it. A committed write from an unrelated
    connection landing between those two captures must not surface as
    "R1.0 capability가 unavailable입니다." for a pure replay — R1.0
    availability is schema canonicality, not a source-content CAS."""
    path = tmp_path / "race.sqlite"
    connection = _connection(path)
    try:
        connection.execute(
            'UPDATE operational_registry_state SET payload_json=\'{"cards":{"card":{"owner":"one"}},"manager_refs":{},"users":["one","two"]}\' WHERE org_id=\'acme\''
        )
        connection.commit()
        command = CardTransferOwnerCommand(**_base(connection), card_id="card", owner_id="two")
        winning = execute_sqlite_tenant_operational_mutation(connection, command)
        assert winning.replayed is False
    finally:
        connection.close()

    calls = {"n": 0}
    original_capture = (
        sqlite_durable_tenant_operational_mutations.capture_sqlite_tenant_operational_mutation_scope_snapshot
    )

    def interleaving_capture(target: sqlite3.Connection):
        calls["n"] += 1
        result = original_capture(target)
        if calls["n"] == 1:
            # This lands after open()'s snapshot capture but before
            # validate_only()'s re-capture — exactly the window observed in
            # the 8-way race.
            other = sqlite3.connect(path)
            try:
                other.execute(
                    "INSERT INTO operational_hitl_toggles VALUES('acme','other-card',1,0,0,'2026-01-01T00:00:02.000Z')"
                )
                other.commit()
            finally:
                other.close()
        return result

    monkeypatch.setattr(
        sqlite_durable_tenant_operational_mutations,
        "capture_sqlite_tenant_operational_mutation_scope_snapshot",
        interleaving_capture,
    )

    replay_connection = sqlite3.connect(path)
    try:
        result = execute_sqlite_tenant_operational_mutation(replay_connection, command)
    finally:
        replay_connection.close()
    assert result.replayed is True


def test_eight_connections_different_session_commands_have_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite"
    bootstrap = _connection(path)
    try:
        scope = capture_sqlite_tenant_operational_mutation_scope_snapshot(bootstrap)
    finally:
        bootstrap.close()
    barrier = threading.Barrier(8)
    outcomes: list[object] = []
    lock = threading.Lock()

    def contender(index: int) -> None:
        # CI 저사양 러너에서 8커넥션 락 큐 대기가 5s를 넘어 R1.0 검증이
        # OperationalError→unavailable로 오탐된 flake(2026-07-22 run 29892932953).
        # 대기 여유만 늘린다 — 1 winner + 7 exact replay 단언은 불변.
        connection = sqlite3.connect(path, timeout=30)
        try:
            barrier.wait(timeout=5)
            command = SessionEndCommand(
                "acme", f"c{index}", "p", scope, "2026-01-01T00:00:01.000Z", "s"
            )
            try:
                value: object = execute_sqlite_tenant_operational_mutation(connection, command)
            except SqliteTenantOperationalMutationUowError:
                value = "conflict"
            with lock:
                outcomes.append(value)
        finally:
            connection.close()

    threads = [threading.Thread(target=contender, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(outcomes) == 8
    assert sum(value != "conflict" for value in outcomes) == 1
    verify = sqlite3.connect(path)
    try:
        assert verify.execute(
            "SELECT status,revision FROM operational_sessions WHERE org_id='acme' AND session_id='s'"
        ).fetchone() == ("ended", 1)
        assert (
            verify.execute(
                "SELECT count(*) FROM durable_tenant_operational_mutation_receipts"
            ).fetchone()[0]
            == 1
        )
        assert verify.execute("SELECT count(*) FROM operational_audit_events_v2").fetchone()[0] == 1
    finally:
        verify.close()
