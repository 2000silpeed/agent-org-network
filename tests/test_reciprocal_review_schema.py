from __future__ import annotations

import sqlite3
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_org_network.reciprocal_review import (
    AiAdvisoryFindingBatch,
    AiReviewerPrincipal,
    ArtifactRevision,
    AuthorshipEvent,
    EffectiveAuthorshipProvenance,
    HumanPrincipal,
    ReviewOpen,
    ReviewRequirement,
    ReviewRun,
    ReviewFinding,
    BindingReady,
    HumanDispositionReceipt,
    ApproveRevision,
    AcceptFinding,
)
from agent_org_network.sqlite_durable_reciprocal_review import (
    COMPONENT_ID,
    SqliteDurableReciprocalReviewError,
    migrate_sqlite_durable_reciprocal_review_ledger,
    open_sqlite_durable_reciprocal_review_ledger,
)


NOW = datetime(2026, 7, 20, tzinfo=UTC)
SHA = "a" * 64


def test_reciprocal_review_values_are_frozen_strict_and_never_trust_caller_origin() -> None:
    human = HumanPrincipal(
        org_id="org", subject_id="human", authenticated_at=NOW, authn_context_digest=SHA
    )
    reviewer = AiReviewerPrincipal(
        org_id="org",
        reviewer_id="reviewer",
        model_execution_ref="execution",
        deployment_digest=SHA,
        rubric_digest="b" * 64,
    )
    assert human.kind == "human"
    assert reviewer.kind == "ai_reviewer"
    with pytest.raises((ValidationError, TypeError)):
        HumanPrincipal.model_validate(
            {
                "org_id": "org",
                "subject_id": "human",
                "authenticated_at": NOW,
                "authn_context_digest": SHA,
                "origin": "human",
            }
        )
    with pytest.raises(ValidationError):
        AuthorshipEvent.model_validate(
            {
                "event_id": "event",
                "org_id": "org",
                "revision_id": "revision",
                "contributor": reviewer,
                "kind": "human",
                "content_digest": SHA,
                "created_at": NOW,
            }
        )
    with pytest.raises(ValidationError):
        human.subject_id = "other"  # type: ignore[misc]


def test_revision_cycle_run_and_ai_batch_keep_the_required_evidence_shapes() -> None:
    human = HumanPrincipal(
        org_id="org", subject_id="human", authenticated_at=NOW, authn_context_digest=SHA
    )
    event = AuthorshipEvent(
        event_id="event",
        org_id="org",
        revision_id="revision",
        contributor=human,
        content_digest=SHA,
        created_at=NOW,
    )
    revision = ArtifactRevision(
        org_id="org",
        artifact_id="artifact",
        revision_id="revision",
        revision_no=1,
        parent_revision_id=None,
        kind="knowledge",
        content_ref="git:commit",
        content_sha256=SHA,
        lineage_event_ids=(event.event_id,),
        effective_provenance=EffectiveAuthorshipProvenance(kind="human", digest="b" * 64),
        authenticated_provenance_events=(event,),
        data_classification="internal",
        data_boundary_snapshot_ref="boundary",
        data_boundary_digest="c" * 64,
        created_at=NOW,
        schema_version=1,
    )
    requirement = ReviewRequirement(
        requirement_id="requirement",
        org_id="org",
        cycle_id="cycle",
        reviewer_kind="ai",
        completion_rule="all",
        required_count=1,
        independence_rule="different_deployment",
        rubric_version="rubric",
        deadline_at=NOW,
        risk_class="standard",
        waivable=True,
    )
    run = ReviewRun(
        review_run_id="run",
        org_id="org",
        requirement_id="requirement",
        run_attempt=1,
        lease_epoch=1,
        lease_token_hash=SHA,
        state="leased",
        created_at=NOW,
    )
    batch = AiAdvisoryFindingBatch(
        batch_id="batch",
        org_id="org",
        review_run_id="run",
        model_execution_ref="execution",
        prompt_digest=SHA,
        rubric_digest="b" * 64,
        input_digest="c" * 64,
        signature="sig",
        findings=(),
        created_at=NOW,
    )
    assert revision.lineage_event_ids == ("event",)
    assert requirement.required_count == 1 and run.lease_token_hash == SHA and batch.findings == ()
    assert ReviewOpen().kind == "review_open"
    with pytest.raises(ValidationError):
        AiAdvisoryFindingBatch(
            batch_id="batch",
            org_id="org",
            review_run_id="run",
            model_execution_ref="execution",
            prompt_digest=SHA,
            rubric_digest="b" * 64,
            input_digest="c" * 64,
            signature="sig",
            findings=(),
            created_at=NOW,
            approve=True,  # pyright: ignore[reportCallIssue] - strict model rejection assertion
        )  # type: ignore[call-arg]


def test_durable_reciprocal_review_schema_is_atomic_idempotent_and_exact(tmp_path: Path) -> None:
    path = tmp_path / "review.sqlite"
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(RuntimeError, match="after_artifact_revisions"):
            migrate_sqlite_durable_reciprocal_review_ledger(
                connection,
                fault_injector=lambda point: (
                    (_ for _ in ()).throw(RuntimeError(point))
                    if point == "after_artifact_revisions"
                    else None
                ),
            )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name LIKE 'durable_reciprocal_review%' "
            ).fetchall()
            == []
        )
        migrate_sqlite_durable_reciprocal_review_ledger(connection)
        migrate_sqlite_durable_reciprocal_review_ledger(connection)
        assert connection.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?", (COMPONENT_ID,)
        ).fetchone()
    finally:
        connection.close()
    open_sqlite_durable_reciprocal_review_ledger(path).close()


@pytest.mark.parametrize(
    "sql",
    (
        "DROP INDEX durable_reciprocal_review_requirements_cycle_idx",
        "DROP TRIGGER durable_reciprocal_review_artifact_revisions_no_update",
        "PRAGMA foreign_keys=OFF; CREATE TABLE copied AS SELECT * FROM durable_reciprocal_review_artifact_revisions; DROP TABLE durable_reciprocal_review_artifact_revisions; ALTER TABLE copied RENAME TO durable_reciprocal_review_artifact_revisions",
    ),
)
def test_durable_reciprocal_review_schema_rejects_catalog_or_fk_drift(
    tmp_path: Path, sql: str
) -> None:
    path = tmp_path / "review.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    connection.executescript(sql)
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableReciprocalReviewError):
        open_sqlite_durable_reciprocal_review_ledger(path)


def test_durable_reciprocal_review_open_rejects_check_bypassed_forged_row(tmp_path: Path) -> None:
    path = tmp_path / "review.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "INSERT INTO durable_reciprocal_review_artifact_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "artifact",
            "revision",
            1,
            None,
            "git:commit",
            "not-a-hash",
            "internal",
            "boundary",
            "human",
            SHA,
            SHA,
            SHA,
            None,
            1,
            "2026-07-20T00:00:00.000Z",
        ),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableReciprocalReviewError):
        open_sqlite_durable_reciprocal_review_ledger(path)


def test_domain_rejects_noncanonical_time_or_inverted_finding_span() -> None:
    with pytest.raises(ValidationError):
        HumanPrincipal(
            org_id="org",
            subject_id="human",
            authenticated_at=datetime(2026, 7, 20),
            authn_context_digest=SHA,
        )
    with pytest.raises(ValidationError):
        ReviewFinding(
            finding_id="finding",
            severity="warning",
            evidence_digest=SHA,
            evidence_start=3,
            evidence_end=2,
        )


def test_durable_reciprocal_review_rejects_extra_prefixed_catalog_member(tmp_path: Path) -> None:
    path = tmp_path / "review.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    connection.execute("CREATE TABLE durable_reciprocal_review_forged(value TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableReciprocalReviewError):
        open_sqlite_durable_reciprocal_review_ledger(path)


def test_binding_ready_requires_matching_human_revision_disposition() -> None:
    human = HumanPrincipal(
        org_id="org", subject_id="human", authenticated_at=NOW, authn_context_digest=SHA
    )
    receipt = HumanDispositionReceipt(
        receipt_id="receipt",
        org_id="org",
        cycle_id="cycle",
        principal=human,
        disposition=ApproveRevision(),
        command_digest=SHA,
        created_at=NOW,
    )
    assert (
        BindingReady(action="approve_revision", human_disposition_receipt=receipt).kind
        == "binding_ready"
    )
    with pytest.raises(ValidationError):
        BindingReady(action="request_changes", human_disposition_receipt=receipt)
    finding_receipt = receipt.model_copy(
        update={"disposition": AcceptFinding(finding_id="finding")}
    )
    with pytest.raises(ValidationError):
        BindingReady(action="approve_revision", human_disposition_receipt=finding_receipt)


def test_durable_reciprocal_review_row_audit_rejects_check_bypassed_cycle_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "review.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "INSERT INTO durable_reciprocal_review_artifact_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "artifact",
            "revision",
            1,
            None,
            "git:commit",
            SHA,
            "internal",
            "boundary",
            "human",
            SHA,
            SHA,
            SHA,
            None,
            1,
            "2026-07-20T00:00:00.000Z",
        ),
    )
    connection.execute(
        "INSERT INTO durable_reciprocal_review_cycles VALUES(?,?,?,?,?,?,?,?,?)",
        ("org", "cycle", "revision", 1, "forged", 3, SHA, SHA, "2026-07-20T00:00:00.000Z"),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableReciprocalReviewError):
        open_sqlite_durable_reciprocal_review_ledger(path)


@pytest.mark.parametrize("corruption", ("missing", "extra", "reordered"))
def test_durable_reciprocal_review_row_audit_rejects_forged_lineage_members(
    tmp_path: Path, corruption: str
) -> None:
    path = tmp_path / f"lineage-{corruption}.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    digest = hashlib.sha256(b'["event-a","event-b"]').hexdigest()
    connection.execute(
        "INSERT INTO durable_reciprocal_review_artifact_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "artifact",
            "revision",
            1,
            None,
            "git:commit",
            SHA,
            "internal",
            "boundary",
            "human",
            SHA,
            digest,
            SHA,
            None,
            1,
            "2026-07-20T00:00:00.000Z",
        ),
    )
    connection.execute(
        "INSERT INTO durable_reciprocal_review_provenance_events VALUES(?,?,?,?,?,?,?,?)",
        ("org", "event-a", "revision", "human", "source-a", SHA, None, "2026-07-20T00:00:00.000Z"),
    )
    connection.execute(
        "INSERT INTO durable_reciprocal_review_provenance_events VALUES(?,?,?,?,?,?,?,?)",
        ("org", "event-b", "revision", "human", "source-b", SHA, None, "2026-07-20T00:00:00.000Z"),
    )
    if corruption == "extra":
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)",
            ("org", "revision", "event-a", 0),
        )
        connection.execute(
            "INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)",
            ("org", "revision", "event-b", 1),
        )
    elif corruption == "reordered":
        connection.execute(
            "INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)",
            ("org", "revision", "event-b", 1),
        )
        connection.execute(
            "INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)",
            ("org", "revision", "event-a", 2),
        )
    else:
        connection.execute(
            "INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)",
            ("org", "revision", "event-a", 1),
        )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableReciprocalReviewError):
        open_sqlite_durable_reciprocal_review_ledger(path)


def test_durable_reciprocal_review_rejects_unrelated_provenance_event_in_lineage_closure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unrelated-lineage.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    lineage = ("event-1", "event-2")
    digest = hashlib.sha256(b'["event-1","event-2"]').hexdigest()
    for revision_id, revision_no in (("revision-1", 1), ("revision-2", 2)):
        connection.execute(
            "INSERT INTO durable_reciprocal_review_artifact_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "org",
                "artifact",
                revision_id,
                revision_no,
                None,
                "git:commit",
                SHA,
                "internal",
                "boundary",
                "human",
                SHA,
                digest
                if revision_id == "revision-1"
                else hashlib.sha256(b'["event-2"]').hexdigest(),
                SHA,
                None,
                1,
                "2026-07-20T00:00:00.000Z",
            ),
        )
    connection.execute(
        "INSERT INTO durable_reciprocal_review_provenance_events VALUES(?,?,?,?,?,?,?,?)",
        (
            "org",
            "event-1",
            "revision-1",
            "human",
            "source-1",
            SHA,
            None,
            "2026-07-20T00:00:00.000Z",
        ),
    )
    connection.execute(
        "INSERT INTO durable_reciprocal_review_provenance_events VALUES(?,?,?,?,?,?,?,?)",
        (
            "org",
            "event-2",
            "revision-2",
            "human",
            "source-2",
            SHA,
            None,
            "2026-07-20T00:00:00.000Z",
        ),
    )
    for ordinal, event_id in enumerate(lineage, start=1):
        connection.execute(
            "INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)",
            ("org", "revision-1", event_id, ordinal),
        )
    connection.execute(
        "INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)",
        ("org", "revision-2", "event-2", 1),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableReciprocalReviewError):
        open_sqlite_durable_reciprocal_review_ledger(path)


def test_durable_reciprocal_review_rejects_forged_or_missing_requirement_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "review.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "INSERT INTO durable_reciprocal_review_artifact_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "artifact",
            "revision",
            1,
            None,
            "git:commit",
            SHA,
            "internal",
            "boundary",
            "human",
            SHA,
            SHA,
            SHA,
            None,
            1,
            "2026-07-20T00:00:00.000Z",
        ),
    )
    connection.execute(
        "INSERT INTO durable_reciprocal_review_cycles VALUES(?,?,?,?,?,?,?,?,?)",
        ("org", "cycle", "revision", 1, "review_open", 1, SHA, SHA, "2026-07-20T00:00:00.000Z"),
    )
    connection.execute(
        "INSERT INTO durable_reciprocal_review_requirements VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "requirement",
            "cycle",
            "ai",
            "all",
            1,
            "",
            "rubric",
            "high",
            "not-a-digest",
            "2026-07-20T00:00:00.000Z",
            1,
        ),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SqliteDurableReciprocalReviewError):
        open_sqlite_durable_reciprocal_review_ledger(path)
