from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agent_org_network.credential_delivery import DeliveryStage, StageMissing
from agent_org_network.sqlite_durable_credential_issue_staging import (
    CredentialIssueStageRequest,
    SqliteCredentialIssueStagingError,
    stage_sqlite_durable_credential_issue_target,
)
from agent_org_network.sqlite_durable_credential_issue_targets import (
    DurableCredentialIssueTargetReservation,
    migrate_sqlite_durable_credential_issue_targets_schema,
)


def _parent(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE durable_credentials (credential_id TEXT NOT NULL, org_id TEXT NOT NULL, owner_subject_id TEXT NOT NULL, role TEXT NOT NULL, generation INTEGER NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL, secret_hash TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT, PRIMARY KEY (org_id, credential_id), CHECK (generation >= 1), CHECK (revision >= 1), CHECK (status IN ('active','revoked')));
            CREATE TABLE credential_command_receipts (org_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt INTEGER NOT NULL, command_digest TEXT NOT NULL, credential_id TEXT NOT NULL, result_revision INTEGER NOT NULL, result_json TEXT NOT NULL, delivery_ref TEXT, PRIMARY KEY (org_id, request_id, attempt));
            CREATE TABLE credential_audit_intents (id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, action TEXT NOT NULL, credential_id TEXT NOT NULL, principal_subject_id TEXT NOT NULL, evidence_id TEXT NOT NULL, detail_json TEXT NOT NULL);
            CREATE TABLE credential_outbox_intents (id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, kind TEXT NOT NULL, credential_id TEXT NOT NULL, payload_json TEXT NOT NULL);
            """
        )
        connection.commit()
    finally:
        connection.close()


def _request() -> CredentialIssueStageRequest:
    reservation = DurableCredentialIssueTargetReservation(
        org_id="org",
        target_id="target",
        credential_id="credential",
        command_digest="a" * 64,
        principal_id="principal",
        owner_subject_id="owner",
        role="role",
        expires_at=None,
        resource_fingerprint="b" * 64,
        approval_evidence_id="evidence",
        approval_command_digest="c" * 64,
        approval_resource_fingerprint="d" * 64,
        target_generation=1,
        created_at="2026-07-19T00:00:00.000Z",
    )
    return CredentialIssueStageRequest(
        reservation=reservation,
        stage_key="e" * 64,
        raw_secret="only-in-memory",
        now="2026-07-19T00:00:00.000Z",
    )


class StubDelivery:
    def __init__(self) -> None:
        self.recoveries = 0
        self.stages = 0
        self.stage = DeliveryStage("e" * 64, "delivery:v1:" + "f" * 64)
        self.recovered: DeliveryStage | StageMissing = StageMissing()

    def recover_stage(self, stage_key: str) -> DeliveryStage | StageMissing:
        self.recoveries += 1
        return self.recovered

    def stage_once(self, raw_secret: str, stage_key: str) -> DeliveryStage:
        self.stages += 1
        return self.stage


def test_claim_winner_stages_once_and_persists_only_secret_hash(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    delivery = StubDelivery()
    result = stage_sqlite_durable_credential_issue_target(path, _request(), delivery)
    assert result == delivery.stage
    assert delivery.recoveries == delivery.stages == 1
    connection = sqlite3.connect(path)
    try:
        fence = connection.execute(
            "SELECT secret_hash,delivery_ref,state FROM credential_issue_stage_fences_v2"
        ).fetchone()
        assert fence == (
            hashlib.sha256(b"only-in-memory").hexdigest(),
            delivery.stage.delivery_ref,
            "Staged",
        )
        dump = " ".join(str(row) for row in connection.execute("SELECT * FROM sqlite_schema"))
        for table in (
            "durable_credential_issue_targets_v1",
            "credential_issue_stage_fences_v2",
            "credential_audit_intents",
            "credential_outbox_intents",
        ):
            dump += " ".join(str(row) for row in connection.execute(f"SELECT * FROM {table}"))
        assert "only-in-memory" not in dump
    finally:
        connection.close()


def test_different_secret_for_same_semantic_target_fails_before_external_stage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    delivery = StubDelivery()
    stage_sqlite_durable_credential_issue_target(path, _request(), delivery)
    with pytest.raises(SqliteCredentialIssueStagingError):
        stage_sqlite_durable_credential_issue_target(
            path,
            CredentialIssueStageRequest(
                _request().reservation, "e" * 64, "different", "2026-07-19T00:00:00.000Z"
            ),
            delivery,
        )
    assert delivery.stages == 1


def test_persist_fault_leaves_claimed_then_retry_recovers_without_restage(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    delivery = StubDelivery()
    with pytest.raises(RuntimeError, match="before_stage_cas"):
        stage_sqlite_durable_credential_issue_target(
            path,
            _request(),
            delivery,
            fault_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError(point)) if point == "before_stage_cas" else None
            ),
        )
    delivery.recovered = delivery.stage
    retry = CredentialIssueStageRequest(
        _request().reservation, "e" * 64, "only-in-memory", "2026-07-19T00:00:01.000Z"
    )
    assert stage_sqlite_durable_credential_issue_target(path, retry, delivery) == delivery.stage
    assert delivery.stages == 1


def test_persisted_staged_replay_does_not_call_delivery_recovery(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    delivery = StubDelivery()
    stage_sqlite_durable_credential_issue_target(path, _request(), delivery)
    before = delivery.recoveries
    retry = CredentialIssueStageRequest(
        _request().reservation, "e" * 64, "only-in-memory", "2026-07-19T00:00:01.000Z"
    )
    assert stage_sqlite_durable_credential_issue_target(path, retry, delivery) == delivery.stage
    assert delivery.recoveries == before


def test_recover_ambiguity_leaves_claimed_and_retry_never_restages(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)

    class AmbiguousDelivery(StubDelivery):
        def recover_stage(self, stage_key: str) -> DeliveryStage | StageMissing:
            self.recoveries += 1
            raise RuntimeError("ambiguous")

    delivery = AmbiguousDelivery()
    with pytest.raises(SqliteCredentialIssueStagingError):
        stage_sqlite_durable_credential_issue_target(path, _request(), delivery)
    assert delivery.stages == 0
    retry_delivery = StubDelivery()
    retry_delivery.recovered = retry_delivery.stage
    retry = CredentialIssueStageRequest(
        _request().reservation, "e" * 64, "only-in-memory", "2026-07-19T00:00:01.000Z"
    )
    assert (
        stage_sqlite_durable_credential_issue_target(path, retry, retry_delivery)
        == retry_delivery.stage
    )
    assert retry_delivery.stages == 0


def test_invalid_recovery_union_never_calls_stage_once(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)

    class InvalidRecovery(StubDelivery):
        def recover_stage(self, stage_key: str) -> DeliveryStage | StageMissing:
            self.recoveries += 1
            return None  # type: ignore[return-value]

    delivery = InvalidRecovery()
    with pytest.raises(SqliteCredentialIssueStagingError):
        stage_sqlite_durable_credential_issue_target(path, _request(), delivery)
    assert delivery.stages == 0
    assert (
        sqlite3.connect(path)
        .execute("SELECT state FROM credential_issue_stage_fences_v2")
        .fetchone()[0]
        == "ClaimedStage"
    )


def test_actual_credential_conflict_rolls_initial_target_and_fence_back_together(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO durable_credentials VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "credential",
            "org",
            "owner",
            "role",
            1,
            1,
            "active",
            "a" * 64,
            "2026-07-19T00:00:00.000Z",
            None,
            None,
        ),
    )
    connection.commit()
    connection.close()
    with pytest.raises(Exception):
        stage_sqlite_durable_credential_issue_target(path, _request(), StubDelivery())
    check = sqlite3.connect(path)
    assert (
        check.execute("SELECT count(*) FROM durable_credential_issue_targets_v1").fetchone()[0] == 0
    )
    assert check.execute("SELECT count(*) FROM credential_issue_stage_fences_v2").fetchone()[0] == 0
    check.close()


def test_stage_error_and_after_cas_fault_retry_recover_only(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)

    class Broken(StubDelivery):
        def stage_once(self, raw_secret: str, stage_key: str) -> DeliveryStage:
            self.stages += 1
            raise RuntimeError("ambiguous")

    broken = Broken()
    with pytest.raises(SqliteCredentialIssueStagingError):
        stage_sqlite_durable_credential_issue_target(path, _request(), broken)
    assert broken.stages == 1
    recovered = StubDelivery()
    recovered.recovered = recovered.stage
    retry = CredentialIssueStageRequest(
        _request().reservation, "e" * 64, "only-in-memory", "2026-07-19T00:00:01.000Z"
    )
    assert stage_sqlite_durable_credential_issue_target(path, retry, recovered) == recovered.stage
    assert recovered.stages == 0


def test_same_now_retry_rotates_claim_generation_then_recovers_without_restage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)

    class Ambiguous(StubDelivery):
        def stage_once(self, raw_secret: str, stage_key: str) -> DeliveryStage:
            self.stages += 1
            raise RuntimeError("ambiguous")

    first = Ambiguous()
    with pytest.raises(SqliteCredentialIssueStagingError):
        stage_sqlite_durable_credential_issue_target(path, _request(), first)
    before = (
        sqlite3.connect(path)
        .execute("SELECT claim_generation FROM credential_issue_stage_fences_v2")
        .fetchone()[0]
    )
    recovered = StubDelivery()
    recovered.recovered = recovered.stage
    assert (
        stage_sqlite_durable_credential_issue_target(path, _request(), recovered) == recovered.stage
    )
    after = (
        sqlite3.connect(path)
        .execute("SELECT claim_generation,state FROM credential_issue_stage_fences_v2")
        .fetchone()
    )
    assert after == (before + 1, "Staged")
    assert first.stages == 1
    assert recovered.stages == 0


def test_wrong_delivery_stage_key_does_not_advance_claimed_fence(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)

    class WrongKeyDelivery(StubDelivery):
        def stage_once(self, raw_secret: str, stage_key: str) -> DeliveryStage:
            self.stages += 1
            return DeliveryStage("d" * 64, "delivery:v1:" + "f" * 64)

    delivery = WrongKeyDelivery()
    with pytest.raises(SqliteCredentialIssueStagingError):
        stage_sqlite_durable_credential_issue_target(path, _request(), delivery)
    assert (
        sqlite3.connect(path)
        .execute("SELECT state FROM credential_issue_stage_fences_v2")
        .fetchone()[0]
        == "ClaimedStage"
    )


def test_32_way_race_has_one_external_stage_and_one_staged_fence(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)

    class LockedDelivery(StubDelivery):
        def __init__(self) -> None:
            super().__init__()
            self.lock = threading.Lock()

        def recover_stage(self, stage_key: str) -> DeliveryStage | StageMissing:
            with self.lock:
                return super().recover_stage(stage_key)

        def stage_once(self, raw_secret: str, stage_key: str) -> DeliveryStage:
            with self.lock:
                return super().stage_once(raw_secret, stage_key)

    delivery = LockedDelivery()
    barrier = threading.Barrier(32)

    def attempt() -> bool:
        barrier.wait()
        try:
            return (
                stage_sqlite_durable_credential_issue_target(path, _request(), delivery)
                == delivery.stage
            )
        except SqliteCredentialIssueStagingError:
            return False

    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(attempt) for _ in range(32)]
        outcomes = [future.result() for future in futures]
    assert any(outcomes)
    assert delivery.stages == 1
    assert (
        sqlite3.connect(path)
        .execute("SELECT state FROM credential_issue_stage_fences_v2")
        .fetchone()[0]
        == "Staged"
    )


def test_unrelated_targets_can_stage_in_parallel(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    first = _request()
    second_reservation = replace(
        first.reservation,
        target_id="target-2",
        credential_id="credential-2",
        command_digest="1" * 64,
    )
    second = CredentialIssueStageRequest(second_reservation, "d" * 64, "other-in-memory", first.now)
    started = threading.Barrier(2)

    class Blocking(StubDelivery):
        def stage_once(self, raw_secret: str, stage_key: str) -> DeliveryStage:
            self.stages += 1
            started.wait(timeout=2)
            return DeliveryStage(stage_key, "delivery:v1:" + "f" * 64)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(stage_sqlite_durable_credential_issue_target, path, request, Blocking())
            for request in (first, second)
        ]
        assert all(future.result().delivery_ref == "delivery:v1:" + "f" * 64 for future in futures)


def test_after_stage_cas_fault_rolls_back_to_claimed_then_recovers_only(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    delivery = StubDelivery()
    with pytest.raises(RuntimeError, match="after_stage_cas"):
        stage_sqlite_durable_credential_issue_target(
            path,
            _request(),
            delivery,
            fault_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError(point)) if point == "after_stage_cas" else None
            ),
        )
    assert (
        sqlite3.connect(path)
        .execute("SELECT state FROM credential_issue_stage_fences_v2")
        .fetchone()[0]
        == "ClaimedStage"
    )
    delivery.recovered = delivery.stage
    retry = CredentialIssueStageRequest(
        _request().reservation, "e" * 64, "only-in-memory", "2026-07-19T00:00:01.000Z"
    )
    assert stage_sqlite_durable_credential_issue_target(path, retry, delivery) == delivery.stage
    assert delivery.stages == 1
