from __future__ import annotations

import sqlite3
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_org_network.sqlite_durable_reciprocal_review import (
    migrate_sqlite_durable_reciprocal_review_ledger,
)
from agent_org_network.sqlite_reciprocal_review_human_disposition import (
    SqliteReciprocalReviewHumanDispositionError,
    create_sqlite_reciprocal_review_human_disposition_uow,
    migrate_sqlite_reciprocal_review_human_disposition_v2,
    upgrade_sqlite_reciprocal_review_cycles_to_v2,
)
from agent_org_network.sqlite_reciprocal_review_human_terminal import (
    COMPONENT_ID,
    SqliteReciprocalReviewHumanTerminalError,
    migrate_sqlite_reciprocal_review_human_terminal_v3,
    validate_sqlite_reciprocal_review_human_terminal,
)
from agent_org_network.reciprocal_review import (
    HumanPrincipal,
    HumanReviewConclusion,
    RecordHumanReviewTerminal,
)
from agent_org_network.sqlite_reciprocal_review_lease import (
    ClaimReviewRun,
    HumanReviewerAssignment,
    create_sqlite_reciprocal_review_lease_uow,
    migrate_sqlite_reciprocal_review_lease,
)
from agent_org_network.sqlite_reciprocal_review_human_terminal import (
    create_sqlite_reciprocal_review_human_terminal_uow,
)
from agent_org_network.sqlite_reciprocal_review_assignment_terminal import (
    assignment_human_authority_payload,
    sqlite_db_now,
    create_sqlite_reciprocal_review_assignment_human_terminal_uow,
    create_sqlite_reciprocal_review_assignment_lease,
    migrate_sqlite_reciprocal_review_assignment_terminal_v4,
    provision_sqlite_reciprocal_review_v4_cycle,
    validate_sqlite_reciprocal_review_assignment_terminal,
)
from agent_org_network.sqlite_reciprocal_review_uow import (
    ArtifactContent,
    InitialReviewPolicy,
    InitialReviewRequirement,
    create_sqlite_reciprocal_review_uow,
)


def _v2_snapshot(connection: sqlite3.Connection) -> None:
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    migrate_sqlite_reciprocal_review_human_disposition_v2(connection)
    upgrade_sqlite_reciprocal_review_cycles_to_v2(connection)


def test_v3_requires_explicit_v2_snapshot_and_preserves_v1_v2_catalog() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    with pytest.raises(SqliteReciprocalReviewHumanTerminalError):
        migrate_sqlite_reciprocal_review_human_terminal_v3(connection)
    _v2_snapshot(connection)
    migrate_sqlite_reciprocal_review_human_terminal_v3(connection)
    validate_sqlite_reciprocal_review_human_terminal(connection)
    assert connection.execute(
        "SELECT component_id FROM schema_component_manifests WHERE component_id=?", (COMPONENT_ID,)
    ).fetchone() == (COMPONENT_ID,)


def test_v3_catalog_corruption_is_fail_closed() -> None:
    connection = sqlite3.connect(":memory:")
    _v2_snapshot(connection)
    migrate_sqlite_reciprocal_review_human_terminal_v3(connection)
    connection.execute("DROP TRIGGER reciprocal_review_human_terminal_results_no_update")
    with pytest.raises(SqliteReciprocalReviewHumanTerminalError):
        validate_sqlite_reciprocal_review_human_terminal(connection)


def test_v2_disposition_writer_is_fail_closed_after_v3_cutover(tmp_path: Path) -> None:
    path = tmp_path / "cutover.sqlite"
    connection = sqlite3.connect(path)
    _v2_snapshot(connection)
    migrate_sqlite_reciprocal_review_human_terminal_v3(connection)
    connection.close()
    uow = create_sqlite_reciprocal_review_human_disposition_uow(
        path,
        trusted_human_authority_keys={"reviewer": b"key"},
        trusted_ai_execution_keys={"ai-key": b"key"},
        clock=lambda: datetime.now(UTC),
    )
    # The guard precedes every v2 read/write and does not need a valid command to deny.
    with pytest.raises(SqliteReciprocalReviewHumanDispositionError):
        uow.submit(None, None)  # type: ignore[arg-type]


NOW = datetime(2026, 7, 20, tzinfo=UTC)
SHA = "a" * 64


class _LeaseAuthority:
    def authorize_human_reviewer(self, **_: object) -> bool:
        return True

    def authorize_ai_reviewer(self, **_: object) -> bool:
        return True


class _LeasePolicy:
    def requirement_is_current(self, **_: object) -> bool:
        return True


def _digest(value: object) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _terminal_principal(subject_id: str = "reviewer") -> HumanPrincipal:
    # The terminal authority payload is rebuilt after assignment/claim in _terminal_command.
    return HumanPrincipal(
        org_id="org", subject_id=subject_id, authenticated_at=NOW, authn_context_digest=SHA
    )


def _seed_terminal(
    path: Path, *, rule: str = "all", count: int = 1, reviewers: tuple[str, ...] = ("reviewer",)
) -> dict[str, str]:
    c = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(c)
    now = "2026-07-20T00:00:00.000Z"
    provenance = _digest({"kind": "ai", "source": "model"})
    c.execute(
        "INSERT INTO durable_reciprocal_review_artifact_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "artifact",
            "revision",
            1,
            None,
            "ref",
            SHA,
            "internal",
            "boundary",
            "ai",
            provenance,
            _digest(("event",)),
            "b" * 64,
            None,
            1,
            now,
        ),
    )
    c.execute(
        "INSERT INTO durable_reciprocal_review_provenance_events VALUES(?,?,?,?,?,?,?,?)",
        ("org", "event", "revision", "model_execution", "model", SHA, None, now),
    )
    c.execute(
        "INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)",
        ("org", "revision", "event", 1),
    )
    c.execute(
        "INSERT INTO durable_reciprocal_review_cycles VALUES(?,?,?,?,?,?,?,?,?)",
        ("org", "cycle", "revision", 1, "review_open", 1, provenance, "c" * 64, now),
    )
    c.execute(
        "INSERT INTO durable_reciprocal_review_requirements VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "org",
            "requirement",
            "cycle",
            "human",
            rule,
            count,
            "independent",
            "rubric",
            "standard",
            _digest({"reviewer_kind": "human", "reviewers": reviewers}),
            now,
            0,
        ),
    )
    c.commit()
    migrate_sqlite_reciprocal_review_human_disposition_v2(c)
    upgrade_sqlite_reciprocal_review_cycles_to_v2(c)
    migrate_sqlite_reciprocal_review_human_terminal_v3(c)
    migrate_sqlite_reciprocal_review_lease(c)
    c.close()
    lease = create_sqlite_reciprocal_review_lease_uow(
        path,
        reviewer_authorization=_LeaseAuthority(),
        policy_snapshot=_LeasePolicy(),
        db_time=lambda connection: NOW,
        token_key=b"lease",
    )
    tokens: dict[str, str] = {}
    for reviewer in reviewers:
        run = f"run-{reviewer}"
        lease.assign(
            HumanReviewerAssignment(
                f"assign-{reviewer}",
                f"assign-audit-{reviewer}",
                f"assign-outbox-{reviewer}",
                run,
                "cycle",
                "requirement",
                _terminal_principal(reviewer),
            )
        )
        claim = lease.claim(
            ClaimReviewRun(
                f"claim-{reviewer}",
                f"claim-audit-{reviewer}",
                f"claim-outbox-{reviewer}",
                run,
                _terminal_principal(reviewer),
                timedelta(minutes=5),
            )
        )
        assert claim.lease_token is not None
        tokens[reviewer] = claim.lease_token
    return tokens


def _terminal_command(
    path: Path, token: str, reviewer: str = "reviewer", suffix: str = ""
) -> tuple[HumanPrincipal, RecordHumanReviewTerminal]:
    import hashlib
    import hmac
    import json

    c = sqlite3.connect(path)
    assignment = c.execute(
        "SELECT assignment_digest FROM reciprocal_review_lease_reviewer_assignments WHERE reviewer_ref=?",
        (reviewer,),
    ).fetchone()[0]
    c.close()
    payload = {
        "org_id": "org",
        "reviewer": reviewer,
        "authenticated_at": "2026-07-20T00:00:00.000Z",
        "revision_id": "revision",
        "cycle_id": "cycle",
        "requirement_id": "requirement",
        "review_run_id": f"run-{reviewer}",
        "assignment_digest": assignment,
        "policy_digest": "c" * 64,
        "provenance_digest": _digest({"kind": "ai", "source": "model"}),
        "contributor_digest": _digest(()),
        "independence_digest": _digest(True),
        "content_digest": SHA,
        "rubric_digest": "d" * 64,
        "input_digest": "e" * 64,
    }
    principal = _terminal_principal(reviewer).model_copy(
        update={
            "authn_context_digest": hmac.new(
                b"human",
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
                hashlib.sha256,
            ).hexdigest()
        }
    )
    return principal, RecordHumanReviewTerminal(
        receipt_id=f"terminal{suffix}",
        audit_id=f"terminal-audit{suffix}",
        outbox_id=f"terminal-outbox{suffix}",
        idempotency_key=f"terminal-key{suffix}",
        cycle_id="cycle",
        requirement_id="requirement",
        review_run_id=f"run-{reviewer}",
        lease_epoch=1,
        lease_token=token,
        conclusion=HumanReviewConclusion(
            content_digest=SHA, rubric_digest="d" * 64, input_digest="e" * 64
        ),
    )


def test_actual_ai_human_run_terminal_is_atomic_and_opens_disposition(tmp_path: Path) -> None:
    path = tmp_path / "terminal.sqlite"
    token = _seed_terminal(path)["reviewer"]
    principal, command = _terminal_command(path, token)
    result = create_sqlite_reciprocal_review_human_terminal_uow(
        path,
        trusted_human_review_run_authority_keys={"reviewer": b"human"},
        trusted_ai_execution_keys={"ai": b"ai"},
        trusted_lease_token_key=b"lease",
        clock=lambda: NOW,
    ).record(principal, command)
    assert (result.cycle_state, result.cycle_revision) == ("awaiting_human_disposition", 2)
    c = sqlite3.connect(path)
    assert c.execute(
        "SELECT state FROM durable_reciprocal_review_runs WHERE review_run_id='run-reviewer'"
    ).fetchone() == ("recorded",)
    assert c.execute(
        "SELECT count(*) FROM reciprocal_review_human_terminal_receipts"
    ).fetchone() == (1,)
    assert c.execute(
        "SELECT count(*) FROM reciprocal_review_human_terminal_results"
    ).fetchone() == (1,)
    assert c.execute("SELECT count(*) FROM reciprocal_review_human_terminal_audit").fetchone() == (
        1,
    )
    assert c.execute("SELECT count(*) FROM reciprocal_review_human_terminal_outbox").fetchone() == (
        1,
    )


def test_v4_assignment_terminal_counts_distinct_assignments_and_fences_v3(tmp_path: Path) -> None:
    """A reclaim/retry is not a second vote; two planned assignments are."""
    path = tmp_path / "assignment-v4.sqlite"
    _seed_terminal(path, rule="quorum", count=2, reviewers=("reviewer",))
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_assignment_terminal_v4(c)
    provision_sqlite_reciprocal_review_v4_cycle(
        c,
        org_id="org",
        cycle_id="cycle",
        assignments=(
            ("requirement", ("reviewer", "reviewer2"), "d" * 64, "e" * 64, SHA),
        ),
    )
    c.commit()
    validate_sqlite_reciprocal_review_assignment_terminal(c)
    run = c.execute(
        "SELECT assignment_run_id,assignment_id,assignment_digest FROM reciprocal_review_v4_assignment_runs "
        "JOIN reciprocal_review_v4_reviewer_assignments USING(org_id,assignment_id) "
        "WHERE reviewer_ref='reviewer'"
    ).fetchone()
    c.close()
    assert run is not None
    lease = create_sqlite_reciprocal_review_assignment_lease(
        path, clock=lambda: NOW, token_key=b"v4-lease"
    )
    epoch, token, _ = lease.claim(
        org_id="org", assignment_run_id=run[0], principal=_terminal_principal(), lease_for=timedelta(minutes=5)
    )
    epoch, token, _ = lease.renew(
        org_id="org", assignment_run_id=run[0], principal=_terminal_principal(), lease_epoch=epoch, lease_token=token, lease_for=timedelta(minutes=5)
    )
    principal = _v4_authorized_principal(path, run[0], "reviewer")
    state, revision = create_sqlite_reciprocal_review_assignment_human_terminal_uow(
        path, trusted_human_assignment_authority_keys={"reviewer": b"v4-human"}, trusted_lease_token_key=b"v4-lease", clock=lambda: NOW
    ).record(
        principal=principal, receipt_id="v4-receipt", audit_id="v4-audit", outbox_id="v4-outbox", idempotency_key="v4-key",
        assignment_run_id=run[0], lease_epoch=epoch, lease_token=token,
        conclusion=HumanReviewConclusion(content_digest=SHA, rubric_digest="d" * 64, input_digest="e" * 64),
    )
    assert (state, revision) == ("review_open", 1)
    # This v4-owned cycle refuses the predecessor terminal writer before any write.
    old_principal, old_command = _terminal_command(path, "wrong")
    with pytest.raises(SqliteReciprocalReviewHumanTerminalError, match="v4-provisioned"):
        create_sqlite_reciprocal_review_human_terminal_uow(
            path, trusted_human_review_run_authority_keys={"reviewer": b"human"}, trusted_ai_execution_keys={"ai": b"ai"}, trusted_lease_token_key=b"lease", clock=lambda: NOW
        ).record(old_principal, old_command)
    with pytest.raises(Exception):
        lease.renew(
            org_id="org", assignment_run_id=run[0], principal=_terminal_principal(), lease_epoch=epoch, lease_token=token, lease_for=timedelta(minutes=5)
        )


def test_v4_provision_rejects_invalid_plan_without_partial_mirror(tmp_path: Path) -> None:
    """Assignment planning is all-or-nothing and cannot reuse a reviewer."""
    path = tmp_path / "invalid-plan.sqlite"
    _seed_terminal(path, rule="quorum", count=2, reviewers=("reviewer",))
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_assignment_terminal_v4(c)

    with pytest.raises(Exception, match="assignment"):
        provision_sqlite_reciprocal_review_v4_cycle(
            c,
            org_id="org",
            cycle_id="cycle",
            assignments=(("requirement", ("reviewer", "reviewer"), "d" * 64, "e" * 64, SHA),),
        )

    assert c.execute("SELECT count(*) FROM durable_reciprocal_review_cycles_v4").fetchone() == (0,)
    assert c.execute("SELECT count(*) FROM reciprocal_review_v4_cycle_ownership").fetchone() == (0,)
    assert c.execute("SELECT count(*) FROM reciprocal_review_v4_reviewer_assignments").fetchone() == (0,)


def test_v4_validation_rejects_orphan_or_extra_terminal_evidence(tmp_path: Path) -> None:
    path = tmp_path / "v4-evidence.sqlite"
    _seed_terminal(path, rule="any", count=1)
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_assignment_terminal_v4(c)
    provision_sqlite_reciprocal_review_v4_cycle(
        c, org_id="org", cycle_id="cycle",
        assignments=(("requirement", ("reviewer",), "d" * 64, "e" * 64, SHA),),
    )
    c.execute(
        "INSERT INTO reciprocal_review_v4_human_terminal_audit VALUES(?,?,?,?,?)",
        ("org", "orphan", "missing", SHA, "2026-07-20T00:00:00.000Z"),
    )
    with pytest.raises(Exception, match="evidence graph"):
        validate_sqlite_reciprocal_review_assignment_terminal(c)


def test_v4_validation_rejects_missing_or_extra_assignment_run(tmp_path: Path) -> None:
    path = tmp_path / "v4-run-chain.sqlite"
    _seed_terminal(path, rule="any", count=1)
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_assignment_terminal_v4(c)
    provision_sqlite_reciprocal_review_v4_cycle(
        c, org_id="org", cycle_id="cycle",
        assignments=(("requirement", ("reviewer",), "d" * 64, "e" * 64, SHA),),
    )
    c.commit()
    c.execute("DELETE FROM reciprocal_review_v4_assignment_runs")
    with pytest.raises(Exception, match="missing run"):
        validate_sqlite_reciprocal_review_assignment_terminal(c)

    c.rollback()
    c.execute(
        "INSERT INTO reciprocal_review_v4_assignment_runs VALUES(?,?,?,?,?,?,?,?)",
        ("org", "forged-run", "assignment:cycle:requirement:1", 2, 1, None, None, "queued"),
    )
    with pytest.raises(Exception, match="missing run"):
        validate_sqlite_reciprocal_review_assignment_terminal(c)


def test_v4_catalog_extra_object_is_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "v4-extra-catalog.sqlite"
    _seed_terminal(path)
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_assignment_terminal_v4(c)
    c.execute("CREATE TABLE reciprocal_review_v4_forged_extra (value TEXT)")
    with pytest.raises(Exception, match="catalog"):
        validate_sqlite_reciprocal_review_assignment_terminal(c)


@pytest.mark.parametrize("lease_for", (timedelta(0), timedelta(seconds=-1)))
def test_v4_assignment_lease_requires_positive_duration(tmp_path: Path, lease_for: timedelta) -> None:
    path = tmp_path / "invalid-lease.sqlite"
    _seed_terminal(path, rule="any", count=1)
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_assignment_terminal_v4(c)
    provision_sqlite_reciprocal_review_v4_cycle(
        c, org_id="org", cycle_id="cycle",
        assignments=(("requirement", ("reviewer",), "d" * 64, "e" * 64, SHA),),
    )
    run = c.execute("SELECT assignment_run_id FROM reciprocal_review_v4_assignment_runs").fetchone()[0]
    c.commit()
    with pytest.raises(Exception, match="lease"):
        create_sqlite_reciprocal_review_assignment_lease(
            path, clock=lambda: NOW, token_key=b"v4-lease"
        ).claim(
            org_id="org", assignment_run_id=run, principal=_terminal_principal(), lease_for=lease_for
        )


def _v4_authorized_principal(
    path: Path, run_id: str, reviewer: str, *, authenticated_at: datetime | None = None,
    candidate_reviewers: tuple[str, ...] | None = None,
) -> HumanPrincipal:
    c = sqlite3.connect(path)
    assignment_id, assignment_digest, contributors, policy, provenance, content, rubric, input_digest = c.execute(
        "SELECT assignment_id,assignment_digest,contributor_digest,policy_digest,provenance_digest,content_digest,rubric_digest,input_digest FROM reciprocal_review_v4_assignment_runs "
        "JOIN reciprocal_review_v4_reviewer_assignments USING(org_id,assignment_id) "
        "WHERE assignment_run_id=?", (run_id,)
    ).fetchone()
    completion_rule, required_count = c.execute(
        "SELECT completion_rule,required_count FROM durable_reciprocal_review_requirements WHERE org_id='org' AND requirement_id='requirement' AND cycle_id='cycle' AND reviewer_kind='human'"
    ).fetchone()
    authority_time = authenticated_at or sqlite_db_now(c)
    candidates = candidate_reviewers or tuple(row[0] for row in c.execute(
        "SELECT reviewer_ref FROM reciprocal_review_v4_reviewer_assignments WHERE org_id='org' AND cycle_id='cycle' AND requirement_id='requirement' ORDER BY ordinal"
    ))
    contributor_refs = tuple(row[0] for row in c.execute(
        "SELECT principal_ref FROM durable_reciprocal_review_provenance_events WHERE org_id='org' AND revision_id='revision' AND principal_kind='human' ORDER BY principal_ref"
    ))
    c.close()
    payload = assignment_human_authority_payload(
        org_id="org", reviewer=reviewer, authenticated_at=authority_time,
        expires_at=authority_time + timedelta(minutes=5), revision_id="revision", cycle_id="cycle",
        requirement_id="requirement", assignment_id=assignment_id, assignment_run_id=run_id,
        assignment_digest=assignment_digest, policy_digest=policy, provenance_digest=provenance,
        contributor_digest=contributors, content_digest=content, rubric_digest=rubric,
        input_digest=input_digest, completion_rule=completion_rule, required_count=required_count,
        candidate_reviewers=candidates, contributors=contributor_refs,
    )
    return _terminal_principal(reviewer).model_copy(update={"authenticated_at": authority_time,
        "authn_context_digest": hmac.new(
            b"v4-human", json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256
        ).hexdigest()
    })


@pytest.mark.parametrize(("rule", "count", "reviewers", "terminal_count"), (
    ("all", 2, ("reviewer", "reviewer2"), 2),
    ("any", 1, ("reviewer", "reviewer2", "reviewer3"), 1),
    ("quorum", 2, ("reviewer", "reviewer2", "reviewer3"), 2),
))
def test_v4_threshold_uses_distinct_assignments(
    tmp_path: Path, rule: str, count: int, reviewers: tuple[str, ...], terminal_count: int
) -> None:
    path = tmp_path / f"{rule}.sqlite"
    _seed_terminal(path, rule=rule, count=count)
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_assignment_terminal_v4(c)
    provision_sqlite_reciprocal_review_v4_cycle(c, org_id="org", cycle_id="cycle", assignments=(("requirement", reviewers, "d" * 64, "e" * 64, SHA),))
    runs = c.execute("SELECT assignment_run_id,reviewer_ref FROM reciprocal_review_v4_assignment_runs JOIN reciprocal_review_v4_reviewer_assignments USING(org_id,assignment_id) ORDER BY reviewer_ref").fetchall()
    c.commit()
    c.close()
    lease = create_sqlite_reciprocal_review_assignment_lease(path, clock=lambda: NOW, token_key=b"v4-lease")
    uow = create_sqlite_reciprocal_review_assignment_human_terminal_uow(path, trusted_human_assignment_authority_keys={reviewer: b"v4-human" for reviewer in reviewers}, trusted_lease_token_key=b"v4-lease", clock=lambda: NOW)
    for index, (run_id, reviewer) in enumerate(runs[:terminal_count], start=1):
        epoch, token, _ = lease.claim(org_id="org", assignment_run_id=run_id, principal=_terminal_principal(reviewer), lease_for=timedelta(minutes=1))
        state, _ = uow.record(principal=_v4_authorized_principal(path, run_id, reviewer), receipt_id=f"receipt-{index}", audit_id=f"audit-{index}", outbox_id=f"outbox-{index}", idempotency_key=f"key-{index}", assignment_run_id=run_id, lease_epoch=epoch, lease_token=token, conclusion=HumanReviewConclusion(content_digest=SHA, rubric_digest="d" * 64, input_digest="e" * 64))
        assert state == ("awaiting_human_disposition" if index == terminal_count else "review_open")


def test_v4_renew_reclaim_fences_expiry_epoch_and_retired_full_token(tmp_path: Path) -> None:
    now = [NOW]
    path = tmp_path / "reclaim.sqlite"
    _seed_terminal(path, rule="any", count=1)
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_assignment_terminal_v4(c)
    provision_sqlite_reciprocal_review_v4_cycle(c, org_id="org", cycle_id="cycle", assignments=(("requirement", ("reviewer",), "d" * 64, "e" * 64, SHA),))
    run = c.execute("SELECT assignment_run_id FROM reciprocal_review_v4_assignment_runs").fetchone()[0]
    c.commit()
    c.close()
    lease = create_sqlite_reciprocal_review_assignment_lease(path, clock=lambda: now[0], token_key=b"v4-lease")
    epoch, retired, _ = lease.claim(org_id="org", assignment_run_id=run, principal=_terminal_principal(), lease_for=timedelta(minutes=1))
    c = sqlite3.connect(path)
    c.execute("UPDATE reciprocal_review_v4_assignment_runs SET expires_at='2000-01-01T00:00:00.000Z'")
    c.commit()
    c.close()
    with pytest.raises(Exception, match="renew"):
        lease.renew(org_id="org", assignment_run_id=run, principal=_terminal_principal(), lease_epoch=epoch, lease_token=retired, lease_for=timedelta(minutes=1))
    epoch2, token, _ = lease.reclaim(org_id="org", assignment_run_id=run, principal=_terminal_principal(), lease_for=timedelta(minutes=1))
    assert epoch2 == epoch + 1
    with pytest.raises(Exception, match="renew"):
        lease.renew(org_id="org", assignment_run_id=run, principal=_terminal_principal(), lease_epoch=epoch, lease_token=retired, lease_for=timedelta(minutes=1))
    state, _ = create_sqlite_reciprocal_review_assignment_human_terminal_uow(path, trusted_human_assignment_authority_keys={"reviewer": b"v4-human"}, trusted_lease_token_key=b"v4-lease", clock=lambda: now[0]).record(principal=_v4_authorized_principal(path, run, "reviewer"), receipt_id="receipt", audit_id="audit", outbox_id="outbox", idempotency_key="key", assignment_run_id=run, lease_epoch=epoch2, lease_token=token, conclusion=HumanReviewConclusion(content_digest=SHA, rubric_digest="d" * 64, input_digest="e" * 64))
    assert state == "awaiting_human_disposition"
    with pytest.raises(Exception):
        lease.claim(org_id="org", assignment_run_id=run, principal=_terminal_principal(), lease_for=timedelta(minutes=1))


def test_v4_db_expiry_overrides_divergent_caller_clock_without_write(tmp_path: Path) -> None:
    path = tmp_path / "db-time-fence.sqlite"
    _seed_terminal(path, rule="any", count=1)
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_assignment_terminal_v4(c)
    provision_sqlite_reciprocal_review_v4_cycle(
        c, org_id="org", cycle_id="cycle",
        assignments=(("requirement", ("reviewer",), "d" * 64, "e" * 64, SHA),),
    )
    run = c.execute("SELECT assignment_run_id FROM reciprocal_review_v4_assignment_runs").fetchone()[0]
    c.commit()
    c.close()
    lease = create_sqlite_reciprocal_review_assignment_lease(
        path, clock=lambda: datetime(1900, 1, 1, tzinfo=UTC), token_key=b"v4-lease"
    )
    epoch, token, _ = lease.claim(
        org_id="org", assignment_run_id=run, principal=_terminal_principal(), lease_for=timedelta(minutes=1)
    )
    c = sqlite3.connect(path)
    before = c.execute("SELECT token_hash,expires_at FROM reciprocal_review_v4_assignment_runs").fetchone()
    c.execute("UPDATE reciprocal_review_v4_assignment_runs SET expires_at='2000-01-01T00:00:00.000Z'")
    c.commit()
    c.close()
    with pytest.raises(Exception, match="renew"):
        lease.renew(org_id="org", assignment_run_id=run, principal=_terminal_principal(), lease_epoch=epoch, lease_token=token, lease_for=timedelta(minutes=1))
    c = sqlite3.connect(path)
    after = c.execute("SELECT token_hash,expires_at FROM reciprocal_review_v4_assignment_runs").fetchone()
    assert after == (before[0], "2000-01-01T00:00:00.000Z")


@pytest.mark.parametrize("mode", ("expired_authority", "tampered_independence"))
def test_v4_authority_expiry_and_independence_tamper_are_write_zero(tmp_path: Path, mode: str) -> None:
    path = tmp_path / f"authority-{mode}.sqlite"
    _seed_terminal(path, rule="any", count=1)
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_assignment_terminal_v4(c)
    provision_sqlite_reciprocal_review_v4_cycle(
        c, org_id="org", cycle_id="cycle",
        assignments=(("requirement", ("reviewer",), "d" * 64, "e" * 64, SHA),),
    )
    run = c.execute("SELECT assignment_run_id FROM reciprocal_review_v4_assignment_runs").fetchone()[0]
    c.commit()
    now = sqlite_db_now(c)
    c.close()
    epoch, token, _ = create_sqlite_reciprocal_review_assignment_lease(
        path, clock=lambda: NOW, token_key=b"v4-lease"
    ).claim(org_id="org", assignment_run_id=run, principal=_terminal_principal(), lease_for=timedelta(minutes=1))
    principal = _v4_authorized_principal(
        path, run, "reviewer",
        authenticated_at=now - timedelta(minutes=6) if mode == "expired_authority" else None,
        candidate_reviewers=("forged-reviewer",) if mode == "tampered_independence" else None,
    )
    uow = create_sqlite_reciprocal_review_assignment_human_terminal_uow(
        path, trusted_human_assignment_authority_keys={"reviewer": b"v4-human"},
        trusted_lease_token_key=b"v4-lease", clock=lambda: NOW,
    )
    with pytest.raises(Exception, match="authority"):
        uow.record(principal=principal, receipt_id="receipt", audit_id="audit", outbox_id="outbox", idempotency_key="key", assignment_run_id=run, lease_epoch=epoch, lease_token=token, conclusion=HumanReviewConclusion(content_digest=SHA, rubric_digest="d" * 64, input_digest="e" * 64))
    c = sqlite3.connect(path)
    assert c.execute("SELECT count(*) FROM reciprocal_review_v4_human_terminal_receipts").fetchone() == (0,)
    assert c.execute("SELECT state FROM reciprocal_review_v4_assignment_runs").fetchone() == ("leased",)


def test_v4_32_way_same_assignment_terminal_has_one_receipt_and_threshold_transition(tmp_path: Path) -> None:
    path = tmp_path / "v4-race.sqlite"
    _seed_terminal(path, rule="any", count=1)
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_assignment_terminal_v4(c)
    provision_sqlite_reciprocal_review_v4_cycle(c, org_id="org", cycle_id="cycle", assignments=(("requirement", ("reviewer",), "d" * 64, "e" * 64, SHA),))
    run = c.execute("SELECT assignment_run_id FROM reciprocal_review_v4_assignment_runs").fetchone()[0]
    c.commit()
    c.close()
    epoch, token, _ = create_sqlite_reciprocal_review_assignment_lease(path, clock=lambda: NOW, token_key=b"v4-lease").claim(org_id="org", assignment_run_id=run, principal=_terminal_principal(), lease_for=timedelta(minutes=1))
    uow = create_sqlite_reciprocal_review_assignment_human_terminal_uow(path, trusted_human_assignment_authority_keys={"reviewer": b"v4-human"}, trusted_lease_token_key=b"v4-lease", clock=lambda: NOW)
    principal = _v4_authorized_principal(path, run, "reviewer")

    def record_once(_: int) -> tuple[str, int]:
        return uow.record(principal=principal, receipt_id="receipt", audit_id="audit", outbox_id="outbox", idempotency_key="key", assignment_run_id=run, lease_epoch=epoch, lease_token=token, conclusion=HumanReviewConclusion(content_digest=SHA, rubric_digest="d" * 64, input_digest="e" * 64))

    with ThreadPoolExecutor(max_workers=32) as pool:
        assert set(pool.map(record_once, range(32))) == {("awaiting_human_disposition", 2)}
    c = sqlite3.connect(path)
    assert c.execute("SELECT count(*) FROM reciprocal_review_v4_human_terminal_receipts").fetchone() == (1,)
    assert c.execute("SELECT state_kind,cycle_revision FROM durable_reciprocal_review_cycles_v4").fetchone() == ("awaiting_human_disposition", 2)
    validate_sqlite_reciprocal_review_assignment_terminal(c)


def test_human_revision_registration_keeps_v3_lane_and_never_owns_v4(tmp_path: Path) -> None:
    class Content:
        def verify(self, **_: object) -> ArtifactContent:
            return ArtifactContent(content_sha256=SHA, source_boundary_digest="b" * 64)

    class Policy:
        def initial_policy(self, **_: object) -> InitialReviewPolicy:
            return InitialReviewPolicy(
                policy_digest="c" * 64,
                requirements=(
                    InitialReviewRequirement("ai", "all", 1, "rubric-ai", NOW, False),
                    InitialReviewRequirement("human", "all", 1, "rubric", NOW, False, ("reviewer",)),
                ),
            )

    class Authorization:
        def authorize_registration(self, **_: object) -> bool:
            return True

        def authorize_reviewer(self, **_: object) -> bool:
            return True

    path = tmp_path / "registration-v4-fault.sqlite"
    c = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(c)
    migrate_sqlite_reciprocal_review_human_disposition_v2(c)
    upgrade_sqlite_reciprocal_review_cycles_to_v2(c)
    migrate_sqlite_reciprocal_review_human_terminal_v3(c)
    migrate_sqlite_reciprocal_review_lease(c)
    migrate_sqlite_reciprocal_review_assignment_terminal_v4(c)
    c.close()
    uow = create_sqlite_reciprocal_review_uow(
        path, content_verifier=Content(), review_policy_registry=Policy(), reviewer_authorization=Authorization(),
        clock=lambda: NOW,
    )
    from agent_org_network.reciprocal_review import RegisterArtifactRevision

    uow.register(_terminal_principal("author"), RegisterArtifactRevision(receipt_id="receipt", artifact_id="artifact", revision_id="revision", kind="knowledge", content_ref="ref", content_sha256=SHA, provenance_event_id="event", audit_id="audit", outbox_id="outbox"))
    c = sqlite3.connect(path)
    assert c.execute("SELECT count(*) FROM durable_reciprocal_review_artifact_revisions").fetchone() == (1,)
    assert c.execute("SELECT count(*) FROM durable_reciprocal_review_cycles_v3").fetchone() == (1,)
    for table in ("durable_reciprocal_review_cycles_v4", "reciprocal_review_v4_cycle_ownership", "reciprocal_review_v4_reviewer_assignments", "reciprocal_review_v4_assignment_runs"):
        assert c.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_32_concurrent_same_terminal_has_one_lease_winner_and_one_evidence_graph(
    tmp_path: Path,
) -> None:
    path = tmp_path / "race.sqlite"
    token = _seed_terminal(path)["reviewer"]
    principal, command = _terminal_command(path, token)
    uow = create_sqlite_reciprocal_review_human_terminal_uow(
        path,
        trusted_human_review_run_authority_keys={"reviewer": b"human"},
        trusted_ai_execution_keys={"ai": b"ai"},
        trusted_lease_token_key=b"lease",
        clock=lambda: NOW,
    )

    def record_once(_: int):
        return uow.record(principal, command)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(record_once, range(32)))
    assert {result.command_digest for result in results}
    c = sqlite3.connect(path)
    assert c.execute(
        "SELECT count(*) FROM reciprocal_review_human_terminal_receipts"
    ).fetchone() == (1,)
    assert c.execute(
        "SELECT state_kind,cycle_revision FROM durable_reciprocal_review_cycles_v3"
    ).fetchone() == (
        "awaiting_human_disposition",
        2,
    )
    validate_sqlite_reciprocal_review_human_terminal(c)



@pytest.mark.parametrize(
    "point",
    (
        "after_run_cas",
        "after_cycle_cas",
        "after_receipt",
        "after_result",
        "after_audit",
        "after_outbox",
        "before_commit",
    ),
)
def test_terminal_fault_boundaries_roll_back_run_cycle_and_all_evidence(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / f"{point}.sqlite"
    token = _seed_terminal(path)["reviewer"]
    principal, command = _terminal_command(path, token)

    def fail(current: str) -> None:
        if current == point:
            raise RuntimeError(point)

    uow = create_sqlite_reciprocal_review_human_terminal_uow(
        path,
        trusted_human_review_run_authority_keys={"reviewer": b"human"},
        trusted_ai_execution_keys={"ai": b"ai"},
        trusted_lease_token_key=b"lease",
        clock=lambda: NOW,
        fault_injector=fail,
    )
    with pytest.raises(RuntimeError, match=point):
        uow.record(principal, command)
    c = sqlite3.connect(path)
    assert c.execute("SELECT state FROM durable_reciprocal_review_runs").fetchone() == ("leased",)
    assert c.execute(
        "SELECT state_kind,cycle_revision FROM durable_reciprocal_review_cycles_v3"
    ).fetchone() == (
        "review_open",
        1,
    )
    for table in (
        "reciprocal_review_human_terminal_receipts",
        "reciprocal_review_human_terminal_results",
        "reciprocal_review_human_terminal_audit",
        "reciprocal_review_human_terminal_outbox",
    ):
        assert c.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
