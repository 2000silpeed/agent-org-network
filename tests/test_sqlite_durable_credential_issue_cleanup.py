from __future__ import annotations

import sqlite3
import threading
import time
from hashlib import sha256
from pathlib import Path

import pytest

from agent_org_network.sqlite_durable_credential_issue_cleanup import (
    COMPONENT_ID,
    SqliteCredentialIssueCleanupError,
    migrate_sqlite_durable_credential_issue_cleanup_schema,
    open_sqlite_durable_credential_issue_cleanup_connection,
    reconcile_sqlite_durable_credential_issue_cleanup,
)
from agent_org_network.sqlite_durable_credential_issue_targets import (
    DurableCredentialIssueTargetReservation,
    _PARENT_DDLS,  # pyright: ignore[reportPrivateUsage]
    migrate_sqlite_durable_credential_issue_targets_schema,
)

NOW = "2026-07-19T00:00:00.000Z"
REF = "delivery:v1:" + "d" * 64


def _path(tmp_path: Path) -> Path:
    path = tmp_path / "cleanup.sqlite"
    connection = sqlite3.connect(path)
    try:
        for ddl in _PARENT_DDLS.values():
            connection.execute(ddl)
    finally:
        connection.close()
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    return path


def _pending(path: Path, target_id: str = "target") -> None:
    digest = sha256(("command:" + target_id).encode()).hexdigest()
    reservation = DurableCredentialIssueTargetReservation(
        "org",
        target_id,
        f"credential-{target_id}",
        digest,
        "principal",
        "owner",
        "worker",
        None,
        "b" * 64,
        "evidence",
        digest,
        "b" * 64,
        1,
        NOW,
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        stage_key = sha256(("stage:" + target_id).encode()).hexdigest()
        connection.execute(
            "INSERT INTO durable_credential_issue_targets_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            reservation.row(),
        )
        connection.execute(
            "INSERT INTO credential_issue_stage_fences_v2 VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("org", target_id, stage_key, "e" * 64, REF, 1, "f" * 64, "CleanupPending", NOW, NOW),
        )
        connection.execute(
            "UPDATE durable_credential_issue_targets_v1 SET state='CleanupPending' WHERE org_id='org' AND target_id=?",
            (target_id,),
        )
        connection.commit()
    finally:
        connection.close()


class _Abort:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.refs: list[str] = []

    def abort(self, delivery_ref: str) -> None:
        self.refs.append(delivery_ref)
        if self.fail:
            raise RuntimeError("unavailable")


def test_schema_is_additive_parent_bound_and_fault_atomic(tmp_path: Path) -> None:
    path = _path(tmp_path)
    connection = sqlite3.connect(path)
    try:
        for point in (
            "after_intents",
            "after_attempts",
            "after_results",
            "before_manifest",
            "after_manifest",
        ):
            with pytest.raises(RuntimeError, match=point):
                migrate_sqlite_durable_credential_issue_cleanup_schema(
                    connection,
                    fault_injector=lambda actual: (
                        (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
                    ),
                )
            assert (
                connection.execute(
                    "SELECT name FROM sqlite_schema WHERE name LIKE 'credential_issue_cleanup_%'"
                ).fetchall()
                == []
            )
        migrate_sqlite_durable_credential_issue_cleanup_schema(connection)
        assert connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?", (COMPONENT_ID,)
        ).fetchone()
    finally:
        connection.close()


def test_success_failure_restart_and_non_pending_never_abort(tmp_path: Path) -> None:
    path = _path(tmp_path)
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_credential_issue_cleanup_schema(connection)
    connection.close()
    _pending(path)
    failed = _Abort(True)
    assert (
        reconcile_sqlite_durable_credential_issue_cleanup(
            path, "org", "target", NOW, failed
        ).cleaned
        is False
    )
    succeeded = _Abort()
    assert (
        reconcile_sqlite_durable_credential_issue_cleanup(
            path, "org", "target", "2026-07-19T00:00:01.000Z", succeeded
        ).cleaned
        is True
    )
    assert failed.refs == succeeded.refs == [REF]
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT state FROM durable_credential_issue_targets_v1"
        ).fetchone() == ("Cleaned",)
        assert connection.execute(
            "SELECT state FROM credential_issue_stage_fences_v2"
        ).fetchone() == ("Cleaned",)
        assert connection.execute(
            "SELECT delivery_ref FROM credential_issue_cleanup_attempts_v1"
        ).fetchall() == [(REF,), (REF,)]
        assert connection.execute(
            "SELECT outcome FROM credential_issue_cleanup_results_v1 ORDER BY attempt"
        ).fetchall() == [("unavailable",), ("aborted",)]
    finally:
        connection.close()
    assert (
        reconcile_sqlite_durable_credential_issue_cleanup(
            path, "org", "target", NOW, succeeded
        ).attempted
        is False
    )


def test_concurrent_reconcile_serializes_abort_and_schema_tamper_fails_closed(
    tmp_path: Path,
) -> None:
    path = _path(tmp_path)
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_credential_issue_cleanup_schema(connection)
    connection.close()
    _pending(path)
    abort = _Abort()
    barrier = threading.Barrier(8)
    results: list[object] = []

    def run() -> None:
        barrier.wait()
        results.append(
            reconcile_sqlite_durable_credential_issue_cleanup(path, "org", "target", NOW, abort)
        )

    workers = [threading.Thread(target=run) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert len(abort.refs) == 1 and sum(getattr(value, "cleaned", False) for value in results) == 1
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE credential_issue_cleanup_results_v1")
    finally:
        connection.close()
    with pytest.raises(SqliteCredentialIssueCleanupError):
        open_sqlite_durable_credential_issue_cleanup_connection(path)


@pytest.mark.parametrize(
    "point", ("after_attempt_committed", "after_abort", "after_result", "after_cleaned_cas")
)
def test_fault_restart_never_false_cleans_and_reuses_ref(tmp_path: Path, point: str) -> None:
    path = _path(tmp_path)
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_credential_issue_cleanup_schema(connection)
    connection.close()
    _pending(path)
    first = _Abort()
    with pytest.raises(RuntimeError, match=point):
        reconcile_sqlite_durable_credential_issue_cleanup(
            path,
            "org",
            "target",
            NOW,
            first,
            fault_injector=lambda actual: (
                (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
            ),
        )
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT state FROM durable_credential_issue_targets_v1"
        ).fetchone() == ("CleanupPending",)
        assert connection.execute(
            "SELECT state FROM credential_issue_stage_fences_v2"
        ).fetchone() == ("CleanupPending",)
    finally:
        connection.close()
    retry = _Abort()
    assert reconcile_sqlite_durable_credential_issue_cleanup(
        path, "org", "target", NOW, retry
    ).cleaned
    assert set(first.refs + retry.refs) == {REF}


def test_parent_mismatch_or_intent_ref_drift_never_calls_abort(tmp_path: Path) -> None:
    path = _path(tmp_path)
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_credential_issue_cleanup_schema(connection)
    connection.close()
    _pending(path)
    abort = _Abort()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE credential_issue_stage_fences_v2 SET state='Staged' WHERE org_id='org' AND target_id='target'"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SqliteCredentialIssueCleanupError):
        reconcile_sqlite_durable_credential_issue_cleanup(path, "org", "target", NOW, abort)
    assert abort.refs == []


def test_32_way_same_target_and_cross_org_never_crosses_cleanup(tmp_path: Path) -> None:
    path = _path(tmp_path)
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_credential_issue_cleanup_schema(connection)
    connection.close()
    _pending(path)
    abort = _Abort()
    barrier = threading.Barrier(32)
    outcomes: list[object] = []

    def run() -> None:
        barrier.wait()
        outcomes.append(
            reconcile_sqlite_durable_credential_issue_cleanup(path, "org", "target", NOW, abort)
        )

    workers = [threading.Thread(target=run) for _ in range(32)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert len(abort.refs) == 1
    assert sum(getattr(value, "cleaned", False) for value in outcomes) == 1
    assert not reconcile_sqlite_durable_credential_issue_cleanup(
        path, "foreign", "target", NOW, abort
    ).attempted
    assert abort.refs == [REF]


def test_distinct_targets_abort_in_parallel_and_cleanup_module_has_no_forbidden_surface(
    tmp_path: Path,
) -> None:
    path = _path(tmp_path)
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_credential_issue_cleanup_schema(connection)
    connection.close()
    targets = tuple(f"target-{index}" for index in range(32))
    for target in targets:
        _pending(path, target)
    entered = threading.Barrier(32)
    released = threading.Event()
    refs: list[str] = []

    class BlockingAbort:
        def abort(self, delivery_ref: str) -> None:
            refs.append(delivery_ref)
            entered.wait(timeout=10)
            released.wait(timeout=10)

    abort = BlockingAbort()
    workers = [
        threading.Thread(
            target=reconcile_sqlite_durable_credential_issue_cleanup,
            args=(path, "org", target, NOW, abort),
        )
        for target in targets
    ]
    for worker in workers:
        worker.start()
    for _ in range(100):
        if len(refs) == 32:
            break
        time.sleep(0.02)
    assert len(refs) == 32
    released.set()
    for worker in workers:
        worker.join()
    module_source = (
        Path(__file__).parents[1]
        / "src/agent_org_network/sqlite_durable_credential_issue_cleanup.py"
    )
    text = module_source.read_text()
    assert "sqlite_durable_credential_issue_staging" not in text
    assert ".release(" not in text
    assert "materialization" not in text


def test_ref_composite_foreign_keys_and_append_only_triggers_reject_tamper(tmp_path: Path) -> None:
    path = _path(tmp_path)
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_credential_issue_cleanup_schema(connection)
    connection.close()
    _pending(path)
    assert reconcile_sqlite_durable_credential_issue_cleanup(
        path, "org", "target", NOW, _Abort()
    ).cleaned
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO credential_issue_cleanup_attempts_v1 VALUES(?,?,?,?,?)",
                ("org", "target", 99, "delivery:v1:" + "a" * 64, NOW),
            )
        for table in (
            "credential_issue_cleanup_intents_v1",
            "credential_issue_cleanup_attempts_v1",
            "credential_issue_cleanup_results_v1",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE {table} SET delivery_ref=?", ("delivery:v1:" + "a" * 64,)
                )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"DELETE FROM {table}")
    finally:
        connection.close()


def test_bad_timestamp_is_write_zero_and_check_bypass_row_fails_closed(tmp_path: Path) -> None:
    path = _path(tmp_path)
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_credential_issue_cleanup_schema(connection)
    connection.close()
    _pending(path)
    abort = _Abort()
    with pytest.raises(SqliteCredentialIssueCleanupError):
        reconcile_sqlite_durable_credential_issue_cleanup(
            path, "org", "target", "grant=raw-secret", abort
        )
    assert abort.refs == []
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM credential_issue_cleanup_intents_v1"
        ).fetchone() == (0,)
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "INSERT INTO credential_issue_cleanup_intents_v1 VALUES(?,?,?,?)",
            ("org", "target", REF, "grant=raw-secret"),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(SqliteCredentialIssueCleanupError):
        open_sqlite_durable_credential_issue_cleanup_connection(path)
