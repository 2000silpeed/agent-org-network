from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_org_network.sqlite_durable_credential_issue_commit import (
    CredentialIssueMaterializationSnapshot,
    SQLITE_DURABLE_CREDENTIAL_ISSUE_COMMIT_FAULT_POINTS,
    SqliteCredentialIssueCommitError,
    MaterializationVerification,
)
from agent_org_network.credential_issue_materialization_test_support import (
    commit_staged_credential_issue_for_test,
    verified_materialization_proof_for_test,
)
from agent_org_network.sqlite_durable_credential_issue_staging import (
    CredentialIssueStageRequest,
    stage_sqlite_durable_credential_issue_target,
)
from agent_org_network.sqlite_durable_credential_issue_targets import (
    DurableCredentialIssueTargetReservation,
    migrate_sqlite_durable_credential_issue_targets_schema,
)
from agent_org_network.credential_mcp import DeliveryStage, StageMissing


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


class _Delivery:
    stage = DeliveryStage("e" * 64, "delivery:v1:" + "f" * 64)

    def recover_stage(self, stage_key: str) -> DeliveryStage | StageMissing:
        return StageMissing()

    def stage_once(self, raw_secret: str, stage_key: str) -> DeliveryStage:
        return self.stage


class _Verifier:
    def __init__(self, *, prepare: bool = True, prewrite: bool = True) -> None:
        self.prepare_allowed = prepare
        self.prewrite_allowed = prewrite

    def prepare(
        self, snapshot: CredentialIssueMaterializationSnapshot
    ) -> MaterializationVerification:
        if not self.prepare_allowed:
            raise RuntimeError("unavailable")
        return verified_materialization_proof_for_test(snapshot)  # type: ignore[return-value]

    def verify_prewrite(
        self, proof: MaterializationVerification, snapshot: CredentialIssueMaterializationSnapshot
    ) -> bool:
        return self.prewrite_allowed


def _stage(path: Path) -> None:
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
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
        target_generation=2,
        created_at="2026-07-19T00:00:00.000Z",
    )
    stage_sqlite_durable_credential_issue_target(
        path,
        CredentialIssueStageRequest(
            reservation, "e" * 64, "raw-only-in-memory", reservation.created_at
        ),
        _Delivery(),
    )


def _counts(path: Path) -> tuple[int, int, int, int]:
    connection = sqlite3.connect(path)
    try:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in (
                "durable_credentials",
                "credential_command_receipts",
                "credential_audit_intents",
                "credential_outbox_intents",
            )
        )  # type: ignore[return-value]
    finally:
        connection.close()


def _commit(
    path: Path,
    now: str,
    verifier: _Verifier | None = None,
    *,
    release: Callable[[str], None] | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> None:
    commit_staged_credential_issue_for_test(
        path,
        "org",
        "target",
        now,
        verifier or _Verifier(),
        release=release,
        fault_injector=fault_injector,
    )


def test_commit_materializes_exact_staged_target_and_releases_persisted_ref_only_after_commit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential.sqlite"
    _stage(path)
    released: list[str] = []
    _commit(path, "2026-07-19T00:00:01.000Z", release=released.append)
    assert released == ["delivery:v1:" + "f" * 64]
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT generation,revision,secret_hash FROM durable_credentials"
        ).fetchone() == (2, 1, hashlib.sha256(b"raw-only-in-memory").hexdigest())
        assert connection.execute(
            "SELECT request_id,attempt,delivery_ref FROM credential_command_receipts"
        ).fetchone() == ("target", 2, "delivery:v1:" + "f" * 64)
        assert connection.execute(
            "SELECT state FROM durable_credential_issue_targets_v1"
        ).fetchone() == ("Committed",)
        assert connection.execute(
            "SELECT state FROM credential_issue_stage_fences_v2"
        ).fetchone() == ("Committed",)
        assert "raw-only-in-memory" not in " ".join(
            str(row) for row in connection.execute("SELECT * FROM sqlite_schema")
        )
    finally:
        connection.close()


@pytest.mark.parametrize("point", SQLITE_DURABLE_CREDENTIAL_ISSUE_COMMIT_FAULT_POINTS)
def test_each_materialization_write_fault_rolls_back_all_effect_rows(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / "credential.sqlite"
    _stage(path)
    with pytest.raises(RuntimeError, match=point):
        _commit(
            path,
            "2026-07-19T00:00:01.000Z",
            fault_injector=lambda actual: (
                (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
            ),
        )
    assert _counts(path) == (0, 0, 0, 0)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT state FROM durable_credential_issue_targets_v1"
        ).fetchone() == ("Staged",)
        assert connection.execute(
            "SELECT state FROM credential_issue_stage_fences_v2"
        ).fetchone() == ("Staged",)
    finally:
        connection.close()


def test_replay_writes_nothing_and_retries_only_same_persisted_delivery_ref(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _stage(path)
    with pytest.raises(SqliteCredentialIssueCommitError):
        _commit(
            path,
            "2026-07-19T00:00:01.000Z",
            release=lambda _: (_ for _ in ()).throw(RuntimeError()),
        )
    assert _counts(path) == (1, 1, 1, 1)
    released: list[str] = []
    _commit(path, "2026-07-19T00:00:01.000Z", release=released.append)
    assert released == ["delivery:v1:" + "f" * 64]
    assert _counts(path) == (1, 1, 1, 1)


@pytest.mark.parametrize(
    "tamper",
    (
        "DELETE FROM credential_audit_intents WHERE credential_id='credential'",
        "DELETE FROM credential_outbox_intents WHERE credential_id='credential'",
        "UPDATE credential_command_receipts SET command_digest='b' || substr(command_digest,2)",
        "UPDATE credential_command_receipts SET result_json='{}'",
        "INSERT INTO credential_command_receipts VALUES('org','orphan',1,'a','credential',1,'{}',NULL)",
    ),
)
def test_corrupt_committed_aggregate_neither_backfills_nor_releases(
    tmp_path: Path, tamper: str
) -> None:
    path = tmp_path / "credential.sqlite"
    _stage(path)
    _commit(path, "2026-07-19T00:00:01.000Z")
    connection = sqlite3.connect(path)
    connection.execute(tamper)
    connection.commit()
    connection.close()
    released: list[str] = []
    with pytest.raises(SqliteCredentialIssueCommitError):
        _commit(path, "2026-07-19T00:00:01.000Z", release=released.append)
    assert released == []


def test_post_stage_evidence_drift_marks_target_and_fence_cleanup_pending_without_abort(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential.sqlite"
    _stage(path)
    connection = sqlite3.connect(path)
    # Simulate an offline-corrupted post-stage snapshot.  Runtime validation
    # must not repair it into a credential; it records only cleanup work.
    connection.execute("DROP TRIGGER durable_credential_issue_targets_v1_immutable_snapshot")
    connection.execute(
        "UPDATE durable_credential_issue_targets_v1 SET approval_evidence_id='changed' WHERE target_id='target'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteCredentialIssueCommitError):
        _commit(path, "2026-07-19T00:00:01.000Z")
    assert _counts(path) == (0, 0, 0, 0)
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


@pytest.mark.parametrize("verifier", (_Verifier(prepare=False), _Verifier(prewrite=False)))
def test_stage_after_current_authority_evidence_or_resource_drift_becomes_cleanup_pending(
    tmp_path: Path, verifier: _Verifier
) -> None:
    path = tmp_path / "credential.sqlite"
    _stage(path)
    with pytest.raises(SqliteCredentialIssueCommitError):
        _commit(path, "2026-07-19T00:00:01.000Z", verifier)
    assert _counts(path) == (0, 0, 0, 0)
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


def test_committed_replay_current_verifier_denial_never_releases(tmp_path: Path) -> None:
    path = tmp_path / "credential.sqlite"
    _stage(path)
    _commit(path, "2026-07-19T00:00:01.000Z")
    released: list[str] = []
    with pytest.raises(SqliteCredentialIssueCommitError):
        _commit(
            path, "2026-07-19T00:00:01.000Z", _Verifier(prewrite=False), release=released.append
        )
    assert released == []


class _ForgedVerifier:
    def prepare(self, snapshot: CredentialIssueMaterializationSnapshot) -> object:
        return object()

    def verify_prewrite(
        self, proof: object, snapshot: CredentialIssueMaterializationSnapshot
    ) -> bool:
        return True


class _ExplodingVerifier:
    def prepare(
        self, snapshot: CredentialIssueMaterializationSnapshot
    ) -> MaterializationVerification:
        raise RuntimeError("authority unavailable")

    def verify_prewrite(
        self, proof: MaterializationVerification, snapshot: CredentialIssueMaterializationSnapshot
    ) -> bool:
        raise RuntimeError("unreachable")


@pytest.mark.parametrize(
    ("label", "verifier"),
    (
        ("revoked_after_stage", _Verifier(prepare=False)),
        ("invalid_evidence", _Verifier(prepare=False)),
        ("owner_or_resource_drift", _Verifier(prewrite=False)),
        ("prewrite_policy_flip", _Verifier(prewrite=False)),
        ("forged_proof", _ForgedVerifier()),
        ("verifier_unavailable", _ExplodingVerifier()),
    ),
)
def test_every_precommit_verifier_failure_only_records_cleanup_pending(
    tmp_path: Path, label: str, verifier: object
) -> None:
    path = tmp_path / f"{label}.sqlite"
    _stage(path)
    with pytest.raises(SqliteCredentialIssueCommitError):
        commit_staged_credential_issue_for_test(
            path,
            "org",
            "target",
            "2026-07-19T00:00:01.000Z",
            verifier,  # pyright: ignore[reportArgumentType]
        )
    assert _counts(path) == (0, 0, 0, 0)
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


def test_commit_module_is_not_available_until_r3_2_implementation_exists(tmp_path: Path) -> None:
    with pytest.raises(SqliteCredentialIssueCommitError):
        commit_staged_credential_issue_for_test(
            tmp_path / "missing.sqlite", "org", "target", "2026-07-19T00:00:00.000Z", _Verifier()
        )
