"""RB3.2b.6-B redacted OperationalEvent/AuditRecord deterministic contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

from agent_org_network.central_operational_evidence import (
    ManagerItemChange,
    OperationalEvent,
    OperationalEvidenceUnavailable,
    OperationalEvidenceProjector,
    OperationalEvidenceResyncRequired,
    QuestionChange,
    RegistryChange,
    SafeResourceRef,
    SourceReceiptProvenance,
    UserActor,
    append_committed_source_evidence,
    canonical_v19_file_authority,
    central_operational_evidence_schema_ready,
    migrate_central_operational_evidence_schema,
    OperationalEvidenceReader,
    source_receipt_digest,
)
from agent_org_network.central_question_lifecycle import (
    CentralQuestionLifecycleStore,
    CentralQuestionLifecycleUnavailable,
    migrate_central_question_lifecycle_schema,
)
from agent_org_network.question_request import QuestionRequest


NOW = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)


def _v18(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE aon_installation_schema(name TEXT PRIMARY KEY,version INTEGER NOT NULL)")
        connection.execute("INSERT INTO aon_installation_schema VALUES ('central-installation',18)")
        connection.execute("CREATE TABLE central_question_create_receipts(org_id TEXT,request_id TEXT)")
        connection.execute("CREATE TABLE central_question_answer_ingest_receipts(org_id TEXT,ticket_id TEXT)")
        connection.execute("CREATE TABLE central_question_feedback_receipts(org_id TEXT,receipt_id TEXT)")
        connection.execute(
            """CREATE TABLE production_registry_user_command_receipts(
               org_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
               operation TEXT NOT NULL, principal_id TEXT NOT NULL,
               command_digest TEXT NOT NULL, result_user_id TEXT NOT NULL,
               result_revision INTEGER NOT NULL, email_digest TEXT NOT NULL,
               registry_fingerprint TEXT NOT NULL,
               resource_fingerprint TEXT NOT NULL, evidence_digest TEXT NOT NULL,
               created_at TEXT NOT NULL,
               authority_policy_revision_id TEXT NOT NULL,
               authority_policy_epoch INTEGER NOT NULL,
               authority_policy_digest TEXT NOT NULL,
               PRIMARY KEY(org_id,idempotency_key)
            )"""
        )


def _migrate(path: Path) -> None:
    _v18(path)
    migrate_central_operational_evidence_schema(path)


def _append(path: Path, number: int) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        occurred_at = (NOW + timedelta(seconds=number)).isoformat()
        if connection.execute(
            "SELECT 1 FROM production_registry_user_command_receipts WHERE org_id=? AND idempotency_key=?",
            ("acme", f"receipt-{number}"),
        ).fetchone() is None:
            connection.execute(
                "INSERT INTO production_registry_user_command_receipts VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "acme", f"receipt-{number}", "user.register", "operator",
                    f"{number:064x}", f"user-{number}", number, "b" * 64,
                    "c" * 64, "d" * 64, "e" * 64, occurred_at,
                    "policy-v1", 1, "a" * 64,
                ),
            )
        receipt_digest = source_receipt_digest(
            connection, "registry_user_registration", "acme", f"receipt-{number}"
        )
        append_committed_source_evidence(
            connection, org_id="acme", receipt_id=f"receipt-{number}",
            command_digest=f"{number:064x}", event_type="registry_user_registered",
            action="registry.user.register",
            resource=SafeResourceRef(kind="registry_user", resource_id=f"user-{number}"),
            change=RegistryChange(), actor_user_id="operator",
            occurred_at=occurred_at,
            policy_revision_id="policy-v1", policy_epoch=1, policy_digest="a" * 64,
            source=SourceReceiptProvenance(
                kind="registry_user_registration", receipt_key=f"receipt-{number}",
                receipt_digest=receipt_digest,
            ),
        )


def test_v18_to_v19_fault_is_marker_last_and_retry_converges(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite"
    _v18(database)

    with pytest.raises(RuntimeError):
        migrate_central_operational_evidence_schema(
            database, fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError(point))
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version FROM aon_installation_schema").fetchone() == (18,)
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='central_operational_events'").fetchone() is None

    migrate_central_operational_evidence_schema(database)
    assert central_operational_evidence_schema_ready(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version FROM aon_installation_schema").fetchone() == (19,)


def test_registry_receipt_semantics_reject_coordinated_resource_mismatch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite"
    _migrate(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO production_registry_user_command_receipts VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "acme", "registry-1", "user.register", "operator", "c" * 64,
                "alice", 1, "b" * 64, "d" * 64, "e" * 64, "f" * 64,
                NOW.isoformat(), "policy-v1", 1, "a" * 64,
            ),
        )
        digest = source_receipt_digest(
            connection, "registry_user_registration", "acme", "registry-1"
        )
        with pytest.raises(OperationalEvidenceUnavailable):
            append_committed_source_evidence(
                connection, org_id="acme", receipt_id="registry:registry-1",
                command_digest="c" * 64,
                event_type="registry_user_registered",
                action="registry.user.register",
                resource=SafeResourceRef(
                    kind="registry_user", resource_id="bob"
                ),
                change=RegistryChange(), actor_user_id="operator",
                occurred_at=NOW.isoformat(), policy_revision_id="policy-v1",
                policy_epoch=1, policy_digest="a" * 64,
                source=SourceReceiptProvenance(
                    kind="registry_user_registration",
                    receipt_key="registry-1", receipt_digest=digest,
                ),
            )
        assert connection.execute(
            "SELECT count(*) FROM central_operational_audit_records"
        ).fetchone() == (0,)


def test_projector_is_restart_safe_redacted_and_retention_requires_resync(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite"
    _migrate(database)
    for index in range(1, 1003):
        _append(database, index)

    def clock() -> datetime:
        return NOW + timedelta(hours=1)
    assert OperationalEvidenceProjector(database, worker_id="worker-1", clock=clock, retention_count=1000).drain() == 1002
    # A fresh projector observes no pending rows; it must not allocate another cursor.
    assert OperationalEvidenceProjector(database, worker_id="worker-2", clock=clock, retention_count=1000).drain() == 0
    reader = OperationalEvidenceReader(database)
    with pytest.raises(OperationalEvidenceResyncRequired) as error:
        reader.feed("acme", 0)
    assert error.value.oldest_available_cursor == 3
    assert error.value.latest_cursor == 1002
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM central_operational_events").fetchone() == (1000,)
        assert connection.execute("SELECT count(*) FROM central_operational_audit_records").fetchone() == (1000,)
        assert connection.execute("SELECT count(*) FROM central_operational_event_intents").fetchone() == (1000,)
    events = reader.feed("acme", 1001)
    assert [event.cursor for event in events] == [1002]
    dumped = events[0].model_dump()
    assert "question" not in dumped["change"]
    assert "answer" not in dumped["change"]
    assert "comment" not in dumped["change"]
    assert "rationale" not in dumped["change"]
    assert "source_uri" not in dumped["change"]
    assert "credential" not in dumped and "session" not in dumped


def test_changed_intent_and_gap_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite"
    _migrate(database)
    _append(database, 1)
    # Exact receipt replay is write-zero; a changed command cannot quietly
    # acquire a second audit binding for the same source receipt.
    _append(database, 1)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM central_operational_audit_records").fetchone() == (1,)
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(OperationalEvidenceUnavailable):
            append_committed_source_evidence(
                connection, org_id="acme", receipt_id="receipt-1", command_digest="e" * 64,
                event_type="registry_user_registered", action="registry.user.register",
                resource=SafeResourceRef(kind="registry_user", resource_id="user-1"),
                change=RegistryChange(), actor_user_id="operator", occurred_at=NOW.isoformat(),
                policy_revision_id="policy-v1", policy_epoch=1, policy_digest="a" * 64,
                source=SourceReceiptProvenance(
                    kind="registry_user_registration", receipt_key="receipt-1",
                    receipt_digest=source_receipt_digest(
                        connection, "registry_user_registration", "acme", "receipt-1"
                    ),
                ),
            )
        connection.rollback()
    with sqlite3.connect(database) as connection:
        # Intent source fields are trigger-frozen, proving a changed payload
        # cannot be silently projected as a new operation.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE central_operational_event_intents SET payload_digest=?", ("f" * 64,))
    projector = OperationalEvidenceProjector(database, worker_id="worker-1", clock=lambda: NOW)
    assert projector.drain() == 1
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO central_operational_retention_authorizations VALUES ('acme',1)")
        connection.execute("DELETE FROM central_operational_events WHERE org_id='acme' AND cursor=1")
        connection.execute("DELETE FROM central_operational_retention_authorizations WHERE org_id='acme'")
    with pytest.raises(OperationalEvidenceUnavailable):
        OperationalEvidenceReader(database).audit_list("acme")


def test_last_event_id_zero_is_a_real_since_cursor_not_an_absent_header(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite"
    _migrate(database)
    _append(database, 1)
    _append(database, 2)
    assert OperationalEvidenceProjector(database, worker_id="worker-1", clock=lambda: NOW).drain() == 2
    reader = OperationalEvidenceReader(database)
    assert [event.cursor for event in reader.feed("acme", 0)] == [1, 2]
    # Header absence intentionally starts after the current high-water mark.
    assert reader.feed("acme") == ()


def test_v19_hook_unavailable_rolls_back_actual_lifecycle_source_transition(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite"
    migrate_central_question_lifecycle_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE aon_installation_schema(name TEXT PRIMARY KEY,version INTEGER NOT NULL)")
        connection.execute("INSERT INTO aon_installation_schema VALUES ('central-installation',19)")
    store = CentralQuestionLifecycleStore(database)
    request = QuestionRequest.receive(
        org_id="acme", requester_id="requester", question="safe question",
        request_id_factory=lambda: "request-1", clock=lambda: NOW,
        due_at=NOW + timedelta(minutes=5),
    )
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        store.create_or_replay(request, idempotency_key="create-1")
    store.close()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM question_requests").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM central_question_create_receipts").fetchone() == (0,)


@pytest.mark.parametrize(
    "mutation",
    (
        "ALTER TABLE central_operational_events ADD COLUMN forged TEXT",
        "DROP INDEX central_operational_audit_org_time",
        "DROP TRIGGER central_operational_event_intents_frozen",
    ),
)
def test_reader_and_readiness_fail_closed_on_owned_catalog_drift(
    tmp_path: Path, mutation: str,
) -> None:
    database = tmp_path / "central.sqlite"
    _migrate(database)
    _append(database, 1)
    assert OperationalEvidenceProjector(database, worker_id="worker-1", clock=lambda: NOW).drain() == 1
    with sqlite3.connect(database) as connection:
        connection.execute(mutation)
    assert central_operational_evidence_schema_ready(database) is False
    reader = OperationalEvidenceReader(database)
    with pytest.raises(OperationalEvidenceUnavailable):
        reader.feed("acme", 0)
    with pytest.raises(OperationalEvidenceUnavailable):
        reader.audit_list("acme")
    with pytest.raises(OperationalEvidenceUnavailable):
        reader.audit_detail("acme", "missing")


@pytest.mark.parametrize(
    ("trigger_name", "mutation"),
    (
        (
            "central_operational_audit_records_no_update",
            "UPDATE central_operational_audit_records SET command_digest='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'",
        ),
        (
            "central_operational_event_intents_frozen",
            "UPDATE central_operational_event_intents SET payload_digest='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'",
        ),
        (
            "central_operational_events_no_update",
            "UPDATE central_operational_events SET payload_digest='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'",
        ),
    ),
)
def test_reader_recomputes_each_durable_evidence_layer(
    tmp_path: Path, trigger_name: str, mutation: str,
) -> None:
    database = tmp_path / "central.sqlite"
    _migrate(database)
    _append(database, 1)
    assert OperationalEvidenceProjector(database, worker_id="worker-1", clock=lambda: NOW).drain() == 1
    with sqlite3.connect(database) as connection:
        trigger_ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger_name,)
        ).fetchone()[0]
        connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute(mutation)
        connection.execute(trigger_ddl)
    with pytest.raises(OperationalEvidenceUnavailable):
        OperationalEvidenceReader(database).feed("acme", 0)


def test_source_receipt_tamper_and_raw_like_scalars_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite"
    _migrate(database)
    _append(database, 1)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE production_registry_user_command_receipts SET idempotency_key='changed'"
        )
    with pytest.raises(OperationalEvidenceUnavailable):
        OperationalEvidenceReader(database).feed("acme", 0)

    for raw_like in ("https://raw.example", "owner@example.test", "line break", "../secret"):
        with pytest.raises(ValidationError):
            SafeResourceRef(kind="registry_user", resource_id=raw_like)


def test_event_change_resource_actor_mapping_is_exact_and_exhaustive() -> None:
    with pytest.raises(ValidationError):
        OperationalEvent(
            cursor=1, event_id="event-1", org_id="acme",
            event_type="answer_finalized", occurred_at=NOW,
            actor=UserActor(user_id="operator"),
            resource=SafeResourceRef(kind="registry_user", resource_id="user-1"),
            outcome="committed", audit_id="audit-1", receipt_id="receipt-1",
            policy_epoch=1, policy_digest="a" * 64, change=RegistryChange(),
        )

    authority = canonical_v19_file_authority(
        source_policy_digest="a" * 64, current_snapshot_digest="a" * 64
    )
    assert authority.policy_revision_id == f"yaml:{'a' * 64}"
    assert authority.policy_epoch == 1
    with pytest.raises(OperationalEvidenceUnavailable):
        canonical_v19_file_authority(
            source_policy_digest="a" * 64, current_snapshot_digest="b" * 64
        )


def test_initial_transition_receipt_binds_question_and_manager_events_exactly(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite"
    _migrate(database)
    authority = canonical_v19_file_authority(
        source_policy_digest="a" * 64, current_snapshot_digest="a" * 64
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE question_requests(request_id TEXT PRIMARY KEY NOT NULL)"
        )
        connection.execute(
            """CREATE TABLE central_question_initial_transition_receipts(
               receipt_id TEXT PRIMARY KEY NOT NULL,
               org_id TEXT NOT NULL,
               request_id TEXT NOT NULL UNIQUE,
               command_digest TEXT NOT NULL CHECK(length(command_digest)=64),
               from_state TEXT NOT NULL CHECK(from_state='received'),
               to_state TEXT NOT NULL CHECK(to_state IN
                 ('awaiting_manager','awaiting_conflict','ready_to_dispatch','declined')),
                   manager_item_id TEXT UNIQUE,
                   policy_revision_id TEXT NOT NULL,
                   policy_epoch INTEGER NOT NULL CHECK(policy_epoch>0),
                   policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),
                   authority_policy_revision_id TEXT NOT NULL,
                   authority_policy_epoch INTEGER NOT NULL CHECK(authority_policy_epoch>0),
                   authority_policy_digest TEXT NOT NULL CHECK(length(authority_policy_digest)=64),
                   created_at TEXT NOT NULL,
               FOREIGN KEY(request_id) REFERENCES question_requests(request_id)
                 ON UPDATE RESTRICT ON DELETE RESTRICT
            )"""
        )
        connection.execute("INSERT INTO question_requests VALUES ('request-1')")
        transition_receipt_id = "initial-transition:request-1"
        connection.execute(
            "INSERT INTO central_question_initial_transition_receipts VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                transition_receipt_id, "acme", "request-1", "c" * 64, "received",
                "awaiting_manager", "manager-1", authority.policy_revision_id,
                    authority.policy_epoch, authority.policy_digest,
                    authority.policy_revision_id, authority.policy_epoch,
                    authority.policy_digest, NOW.isoformat(),
                ),
        )
        question_digest = source_receipt_digest(
            connection, "question_initial_state", "acme", transition_receipt_id
        )
        append_committed_source_evidence(
            connection, org_id="acme", receipt_id="question-initial:request-1",
            command_digest="c" * 64, event_type="request_state_changed",
            action="question.initial_transition",
            resource=SafeResourceRef(
                kind="question_request", resource_id="request-1"
            ),
            change=QuestionChange(
                request_id="request-1", from_state="received",
                to_state="awaiting_manager",
            ),
            actor_user_id=None, occurred_at=NOW.isoformat(),
            policy_revision_id=authority.policy_revision_id,
            policy_epoch=authority.policy_epoch,
            policy_digest=authority.policy_digest,
            source=SourceReceiptProvenance(
                kind="question_initial_state", receipt_key=transition_receipt_id,
                receipt_digest=question_digest,
            ),
        )
        manager_digest = source_receipt_digest(
            connection, "manager_initial", "acme", transition_receipt_id
        )
        append_committed_source_evidence(
            connection, org_id="acme", receipt_id="manager-initial:request-1",
            command_digest="c" * 64, event_type="manager_item_changed",
            action="manager_item.create",
            resource=SafeResourceRef(kind="manager_item", resource_id="manager-1"),
            change=ManagerItemChange(
                manager_item_id="manager-1", request_id="request-1"
            ),
            actor_user_id=None, occurred_at=NOW.isoformat(),
            policy_revision_id=authority.policy_revision_id,
            policy_epoch=authority.policy_epoch,
            policy_digest=authority.policy_digest,
            source=SourceReceiptProvenance(
                kind="manager_initial", receipt_key=transition_receipt_id,
                receipt_digest=manager_digest,
            ),
        )

    assert OperationalEvidenceProjector(
        database, worker_id="worker-1", clock=lambda: NOW
    ).drain() == 2
    assert [
        event.event_type
        for event in OperationalEvidenceReader(database).feed("acme", 0)
    ] == ["manager_item_changed", "request_state_changed"]

    malformed = tmp_path / "malformed.sqlite"
    _migrate(malformed)
    with sqlite3.connect(malformed) as connection:
        connection.execute(
            "CREATE TABLE question_requests(request_id TEXT PRIMARY KEY NOT NULL)"
        )
        connection.execute(
            """CREATE TABLE central_question_initial_transition_receipts(
               receipt_id TEXT PRIMARY KEY NOT NULL,
               org_id TEXT NOT NULL,
               request_id TEXT NOT NULL UNIQUE,
               command_digest TEXT NOT NULL CHECK(length(command_digest)=64),
               from_state TEXT NOT NULL CHECK(from_state='received'),
               to_state TEXT NOT NULL,
               manager_item_id TEXT UNIQUE,
               policy_revision_id TEXT NOT NULL,
               policy_epoch INTEGER NOT NULL CHECK(policy_epoch>0),
               policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),
               created_at TEXT NOT NULL,
               FOREIGN KEY(request_id) REFERENCES question_requests(request_id)
            )"""
        )
        connection.execute("INSERT INTO question_requests VALUES ('request-1')")
        connection.execute(
            "INSERT INTO central_question_initial_transition_receipts VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "wrong:request-1", "acme", "request-1", "c" * 64, "received",
                "awaiting_manager", "manager-1", authority.policy_revision_id,
                authority.policy_epoch, authority.policy_digest, NOW.isoformat(),
            ),
        )
        with pytest.raises(OperationalEvidenceUnavailable):
            source_receipt_digest(
                connection, "question_initial_state", "acme", "wrong:request-1"
            )


@pytest.mark.parametrize(
    ("table", "key_column"),
    (
        ("production_agent_card_command_receipts", "idempotency_key"),
        ("central_inbox_conflict_receipts", "receipt_id"),
        ("central_inbox_approval_reassignment_receipts", "receipt_id"),
        ("central_inbox_backup_review_disposition_receipts", "receipt_id"),
    ),
)
def test_v18_nonempty_producer_without_exact_safe_authority_rolls_back(
    tmp_path: Path, table: str, key_column: str,
) -> None:
    database = tmp_path / "central.sqlite"
    _v18(database)
    with sqlite3.connect(database) as connection:
        connection.execute(f"CREATE TABLE {table}(org_id TEXT,{key_column} TEXT)")
        connection.execute(f"INSERT INTO {table} VALUES ('acme','receipt-1')")
    with pytest.raises(OperationalEvidenceUnavailable):
        migrate_central_operational_evidence_schema(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version FROM aon_installation_schema").fetchone() == (18,)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='central_operational_events'"
        ).fetchone() is None


def test_v19_manifest_writer_matrix_has_authority_companions_for_every_kind() -> None:
    """The v19 source manifest is an executable writer/catalog contract."""
    from agent_org_network import central_inbox_approval, central_inbox_conflict
    from agent_org_network import central_inbox_review, central_question_lifecycle
    from agent_org_network import sqlite_production_agent_cards
    from agent_org_network import sqlite_production_registry_users
    from agent_org_network.central_operational_evidence import (  # pyright: ignore[reportPrivateUsage]
        _SOURCE_MANIFESTS,  # pyright: ignore[reportPrivateUsage]
    )

    ddl_text = "\n".join(
        (
            *central_question_lifecycle._TABLES,  # pyright: ignore[reportPrivateUsage]
            *central_inbox_conflict._TABLES,  # pyright: ignore[reportPrivateUsage]
            *central_inbox_approval._TABLES.values(),  # pyright: ignore[reportPrivateUsage]
            *central_inbox_review._TABLES.values(),  # pyright: ignore[reportPrivateUsage]
            sqlite_production_registry_users._SCHEMA,  # pyright: ignore[reportPrivateUsage]
            sqlite_production_agent_cards._SCHEMA,  # pyright: ignore[reportPrivateUsage]
        )
    )
    assert len(_SOURCE_MANIFESTS) == 19
    for manifest in _SOURCE_MANIFESTS.values():  # pyright: ignore[reportPrivateUsage]
        table_start = ddl_text.find(f"CREATE TABLE {manifest.table}")
        assert table_start >= 0, manifest.table
        table_end = ddl_text.find("\n)", table_start)
        table_ddl = ddl_text[table_start:table_end]
        assert "authority_policy_revision_id" in table_ddl, manifest.kind
        assert "authority_policy_epoch" in table_ddl, manifest.kind
        assert "authority_policy_digest" in table_ddl, manifest.kind
