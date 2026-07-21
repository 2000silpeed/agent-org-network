from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
from datetime import timedelta
from datetime import UTC, datetime
from pathlib import Path

import pytest
import agent_org_network.sqlite_reciprocal_review_human_disposition as human_disposition_module

from agent_org_network.reciprocal_review import (
    HumanPrincipal,
    RegisterArtifactRevision,
    SubmitHumanDisposition,
    ApproveRevision,
    RequestChanges,
)
from agent_org_network.sqlite_durable_reciprocal_review import (
    migrate_sqlite_durable_reciprocal_review_ledger,
)
from agent_org_network.sqlite_reciprocal_review_ai_batches import (
    RecordAiAdvisoryBatch,
    create_sqlite_reciprocal_review_ai_batch_uow,
    migrate_sqlite_reciprocal_review_ai_batches,
)
from agent_org_network.sqlite_reciprocal_review_human_disposition import (
    SqliteReciprocalReviewHumanDispositionConflict,
    SqliteReciprocalReviewHumanDispositionError,
    create_sqlite_reciprocal_review_human_disposition_uow,
    migrate_sqlite_reciprocal_review_human_disposition_v2,
    upgrade_sqlite_reciprocal_review_cycles_to_v2,
)
from agent_org_network.sqlite_reciprocal_review_uow import (
    ArtifactContent,
    InitialReviewPolicy,
    InitialReviewRequirement,
    create_sqlite_reciprocal_review_uow,
)
from agent_org_network.sqlite_reciprocal_review_lease import (
    AiReviewerAssignment,
    ClaimReviewRun,
    create_sqlite_reciprocal_review_lease_uow,
    migrate_sqlite_reciprocal_review_lease,
)
from agent_org_network.reciprocal_review import AiAdvisoryFindingBatch, AiReviewerPrincipal
from agent_org_network.reciprocal_review import (
    SourceBindingCapability,
    SourceBoundaryDriftActionAuthorization,
    SourceBoundaryEnforcementPlan,
)
from agent_org_network.sqlite_reciprocal_review_source_binding import (
    SqliteReciprocalReviewSourceBindingError,
    create_sqlite_reciprocal_review_source_binding_intent_uow,
    migrate_sqlite_reciprocal_review_source_binding_v6,
)


NOW = datetime(2026, 7, 20, tzinfo=UTC)
SHA = "a" * 64


class _Content:
    def verify(self, **_: object) -> ArtifactContent:
        return ArtifactContent(content_sha256=SHA, source_boundary_digest="b" * 64)


class _Policy:
    def initial_policy(self, **_: object) -> InitialReviewPolicy:
        return InitialReviewPolicy(
            "c" * 64, (InitialReviewRequirement("ai", "all", 1, "rubric", NOW, False),)
        )


class _RegistrationAuthority:
    def authorize_registration(self, **_: object) -> bool:
        return True

    def authorize_reviewer(self, **_: object) -> bool:
        return True


class _LeaseAuthority:
    def authorize_ai_reviewer(self, **_: object) -> bool:
        return True

    def authorize_human_reviewer(self, **_: object) -> bool:
        return True


class _LeasePolicy:
    def requirement_is_current(self, **_: object) -> bool:
        return True


class _BatchPolicy:
    def batch_is_current(self, **_: object) -> bool:
        return True

    def permits_awaiting_human_disposition(self, **_: object) -> bool:
        return True


def _author() -> HumanPrincipal:
    return HumanPrincipal(
        org_id="org", subject_id="author", authenticated_at=NOW, authn_context_digest=SHA
    )


def _ai() -> AiReviewerPrincipal:
    return AiReviewerPrincipal(
        org_id="org",
        reviewer_id="ai-reviewer",
        model_execution_ref="execution",
        deployment_digest=SHA,
        rubric_digest=SHA,
    )


def _principal() -> HumanPrincipal:
    payload = {
        "org_id": "org",
        "subject_id": "human",
        "authenticated_at": "2026-07-20T00:00:00.000Z",
        "revision_id": "revision",
        "cycle_id": "cycle:revision",
        "action": "approve_revision",
        "policy_digest": "c" * 64,
        "provenance_digest": _provenance(),
        "independence_digest": _digest(("independent",)),
    }
    return HumanPrincipal(
        org_id="org",
        subject_id="human",
        authenticated_at=NOW,
        authn_context_digest=hmac.new(
            b"human-key",
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest(),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _provenance() -> str:
    return _digest(
        {
            "kind": "human",
            "principal": _author().model_dump(mode="json"),
            "content": SHA,
            "lineage": ("event",),
        }
    )


def _seed(path: Path) -> None:
    c = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(c)
    c.close()


    registration = create_sqlite_reciprocal_review_uow(
        path,
        content_verifier=_Content(),
        review_policy_registry=_Policy(),
        reviewer_authorization=_RegistrationAuthority(),
        clock=lambda: NOW,
    )
    registration.register(
        _author(),
        RegisterArtifactRevision(
            receipt_id="registration",
            artifact_id="artifact",
            revision_id="revision",
            kind="knowledge",
            content_ref="git:commit",
            content_sha256=SHA,
            provenance_event_id="event",
            audit_id="registration-audit",
            outbox_id="registration-outbox",
        ),
    )
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_lease(c)
    migrate_sqlite_reciprocal_review_ai_batches(c)
    c.commit()
    c.close()
    lease = create_sqlite_reciprocal_review_lease_uow(
        path,
        reviewer_authorization=_LeaseAuthority(),
        policy_snapshot=_LeasePolicy(),
        db_time=lambda connection: NOW,
        token_key=b"lease-key",
    )
    lease.assign(
        AiReviewerAssignment(
            "assign",
            "assign-audit",
            "assign-outbox",
            "run",
            "cycle:revision",
            "requirement:revision:1",
            _ai(),
        )
    )
    claim = lease.claim(
        ClaimReviewRun("claim", "claim-audit", "claim-outbox", "run", _ai(), timedelta(minutes=5))
    )
    assert claim.lease_token is not None
    signed = _digest(
        {
            "org_id": "org",
            "batch_id": "batch",
            "review_run_id": "run",
            "model_execution_ref": "execution",
            "rubric_digest": SHA,
            "prompt_digest": "b" * 64,
            "input_digest": SHA,
            "findings": [],
            "cycle_id": "cycle:revision",
            "requirement_id": "requirement:revision:1",
            "policy_digest": "c" * 64,
            "provenance_digest": _provenance(),
            "content_digest": SHA,
            "deployment_digest": SHA,
        }
    )
    signature = hmac.new(b"ai-key", signed.encode(), hashlib.sha256).hexdigest()
    batch = RecordAiAdvisoryBatch(
        receipt_id="batch-receipt",
        audit_id="batch-audit",
        outbox_id="batch-outbox",
        principal=_ai(),
        lease_epoch=claim.lease_epoch,
        lease_token=claim.lease_token,
        batch=AiAdvisoryFindingBatch(
            batch_id="batch",
            org_id="org",
            review_run_id="run",
            model_execution_ref="execution",
            prompt_digest="b" * 64,
            rubric_digest=SHA,
            input_digest=SHA,
            signature=signature,
            signature_algorithm="hmac-sha256",
            signing_key_id="ai",
            signed_payload_digest=signed,
            findings=(),
            created_at=NOW,
        ),
    )
    create_sqlite_reciprocal_review_ai_batch_uow(
        path,
        lease_uow=lease,
        policy_snapshot=_BatchPolicy(),
        trusted_execution_keys={"ai": b"ai-key"},
    ).record(batch)
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_human_disposition_v2(c)
    upgrade_sqlite_reciprocal_review_cycles_to_v2(c)
    c.close()


def _command(receipt: str = "receipt", key: str = "key") -> SubmitHumanDisposition:
    return SubmitHumanDisposition(
        receipt_id=receipt,
        audit_id=f"audit-{receipt}",
        outbox_id=f"outbox-{receipt}",
        cycle_id="cycle:revision",
        expected_cycle_revision=1,
        idempotency_key=key,
        disposition=ApproveRevision(),
    )


def test_human_disposition_atomically_records_body_free_receipt_and_binding_ready(
    tmp_path: Path,
) -> None:
    _seed(tmp_path / "review.sqlite")
    result = create_sqlite_reciprocal_review_human_disposition_uow(
        tmp_path / "review.sqlite",
        trusted_human_authority_keys={"human": b"human-key"},
        trusted_ai_execution_keys={"ai": b"ai-key"},
        clock=lambda: NOW,
    ).submit(_principal(), _command())
    assert (result.cycle_state, result.cycle_revision, result.action) == (
        "binding_ready",
        2,
        "approve_revision",
    )
    c = sqlite3.connect(tmp_path / "review.sqlite")
    assert c.execute(
        "SELECT count(*) FROM reciprocal_review_human_disposition_receipts"
    ).fetchone() == (1,)
    assert c.execute(
        "SELECT state_kind,cycle_revision FROM durable_reciprocal_review_cycles_v2"
    ).fetchone() == ("binding_ready", 2)
    assert c.execute(
        "SELECT count(*) FROM reciprocal_review_human_disposition_audit"
    ).fetchone() == (1,)
    assert c.execute(
        "SELECT count(*) FROM reciprocal_review_human_disposition_outbox"
    ).fetchone() == (1,)
    c.close()


def test_replay_rechecks_authority_and_conflicting_key_or_stale_revision_writes_nothing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "review.sqlite"
    _seed(path)
    uow = create_sqlite_reciprocal_review_human_disposition_uow(
        path,
        trusted_human_authority_keys={"human": b"human-key"},
        trusted_ai_execution_keys={"ai": b"ai-key"},
        clock=lambda: NOW,
    )
    first = uow.submit(_principal(), _command())
    assert uow.submit(_principal(), _command()) == first
    revoked = create_sqlite_reciprocal_review_human_disposition_uow(
        path,
        trusted_human_authority_keys={"human": b"revoked"},
        trusted_ai_execution_keys={"ai": b"ai-key"},
        clock=lambda: NOW,
    )
    with pytest.raises(SqliteReciprocalReviewHumanDispositionError):
        revoked.submit(_principal(), _command())
    with pytest.raises(SqliteReciprocalReviewHumanDispositionConflict):
        uow.submit(_principal(), _command("other", "key"))


def test_v1_only_is_unavailable_and_fault_rolls_back_cas_and_receipt(tmp_path: Path) -> None:
    path = tmp_path / "review.sqlite"
    _seed(path)
    # Fresh fixture for fault because a successful disposition is terminal.
    fault_path = tmp_path / "fault.sqlite"
    _seed(fault_path)
    uow = create_sqlite_reciprocal_review_human_disposition_uow(
        fault_path,
        trusted_human_authority_keys={"human": b"human-key"},
        trusted_ai_execution_keys={"ai": b"ai-key"},
        clock=lambda: NOW,
        fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError(point)) if point == "after_cycle_cas" else None
        ),
    )
    with pytest.raises(RuntimeError):
        uow.submit(_principal(), _command())
    c = sqlite3.connect(fault_path)
    assert c.execute(
        "SELECT state_kind,cycle_revision FROM durable_reciprocal_review_cycles_v2"
    ).fetchone() == ("awaiting_human_disposition", 1)
    assert c.execute(
        "SELECT count(*) FROM reciprocal_review_human_disposition_receipts"
    ).fetchone() == (0,)
    c.close()


def test_32_concurrent_same_command_has_one_receipt_and_different_action_conflicts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent.sqlite"
    _seed(path)

    def submit(_: int) -> str:
        return (
            create_sqlite_reciprocal_review_human_disposition_uow(
                path,
                trusted_human_authority_keys={"human": b"human-key"},
                trusted_ai_execution_keys={"ai": b"ai-key"},
                clock=lambda: NOW,
            )
            .submit(_principal(), _command())
            .receipt_id
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        assert set(pool.map(submit, range(32))) == {"receipt"}
    c = sqlite3.connect(path)
    assert c.execute(
        "SELECT count(*) FROM reciprocal_review_human_disposition_receipts"
    ).fetchone() == (1,)
    assert c.execute(
        "SELECT state_kind,cycle_revision FROM durable_reciprocal_review_cycles_v2"
    ).fetchone() == ("binding_ready", 2)
    c.close()
    with pytest.raises(SqliteReciprocalReviewHumanDispositionConflict):
        create_sqlite_reciprocal_review_human_disposition_uow(
            path,
            trusted_human_authority_keys={"human": b"human-key"},
            trusted_ai_execution_keys={"ai": b"ai-key"},
            clock=lambda: NOW,
        ).submit(_principal(), _command().model_copy(update={"disposition": RequestChanges()}))


@pytest.mark.parametrize(
    "point", ["after_cycle_cas", "after_receipt", "after_result", "after_audit", "after_outbox"]
)
def test_every_postwrite_fault_rolls_back_all_human_disposition_rows(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / f"{point}.sqlite"
    _seed(path)
    uow = create_sqlite_reciprocal_review_human_disposition_uow(
        path,
        trusted_human_authority_keys={"human": b"human-key"},
        trusted_ai_execution_keys={"ai": b"ai-key"},
        clock=lambda: NOW,
        fault_injector=lambda actual: (
            (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
        ),
    )
    with pytest.raises(RuntimeError):
        uow.submit(_principal(), _command())
    c = sqlite3.connect(path)
    assert c.execute(
        "SELECT state_kind,cycle_revision FROM durable_reciprocal_review_cycles_v2"
    ).fetchone() == ("awaiting_human_disposition", 1)
    assert all(
        c.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        for table in (
            "reciprocal_review_human_disposition_receipts",
            "reciprocal_review_human_disposition_results",
            "reciprocal_review_human_disposition_audit",
            "reciprocal_review_human_disposition_outbox",
        )
    )
    c.close()


class _HumanV6Adapter:
    def capability(self, **_: object) -> SourceBindingCapability:
        return SourceBindingCapability()

    def boundary_plan(self, **values: object) -> SourceBoundaryEnforcementPlan:
        return SourceBoundaryEnforcementPlan(
            plan_id="plan", source_ref=str(values["source_ref"]),
            expected_source_revision="source-revision", revision_id=str(values["revision_id"]),
            content_digest=str(values["content_digest"]), boundary_digest=str(values["boundary_digest"]),
            enforcement_mode="gateway",
        )

    def drift_authorization(self, *, plan: SourceBoundaryEnforcementPlan, now: datetime) -> SourceBoundaryDriftActionAuthorization:
        return SourceBoundaryDriftActionAuthorization(
            authorization_id="drift", source_ref=plan.source_ref,
            expected_source_revision=plan.expected_source_revision, boundary_digest=plan.boundary_digest,
            action="source_unpublish", expires_at=now + timedelta(minutes=5),
        )


def test_v6_legacy_writer_cannot_consume_real_v2_human_binding_ready(tmp_path: Path) -> None:
    path = tmp_path / "v2-v6.sqlite"
    _seed(path)
    create_sqlite_reciprocal_review_human_disposition_uow(
        path, trusted_human_authority_keys={"human": b"human-key"},
        trusted_ai_execution_keys={"ai": b"ai-key"}, clock=lambda: NOW,
    ).submit(_principal(), _command())
    c = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_source_binding_v6(c)
    c.close()
    with pytest.raises(SqliteReciprocalReviewSourceBindingError):
        create_sqlite_reciprocal_review_source_binding_intent_uow(path, adapter=_HumanV6Adapter())
    c = sqlite3.connect(path)
    assert c.execute("SELECT count(*) FROM reciprocal_review_v6_binding_intents").fetchone() == (0,)
    assert c.execute("SELECT state_kind FROM durable_reciprocal_review_cycles_v2").fetchone() == ("binding_ready",)
    c.close()


def test_v1_only_is_unavailable_and_v1_transition_trigger_is_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite"
    c = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(c)
    trigger = c.execute(
        "SELECT sql FROM sqlite_schema WHERE name='durable_reciprocal_review_cycles_no_update'"
    ).fetchone()
    c.close()
    uow = create_sqlite_reciprocal_review_human_disposition_uow(
        path,
        trusted_human_authority_keys={"human": b"human-key"},
        trusted_ai_execution_keys={"ai": b"ai-key"},
        clock=lambda: NOW,
    )
    with pytest.raises(SqliteReciprocalReviewHumanDispositionError):
        uow.submit(_principal(), _command())
    c = sqlite3.connect(path)
    assert (
        c.execute(
            "SELECT sql FROM sqlite_schema WHERE name='durable_reciprocal_review_cycles_no_update'"
        ).fetchone()
        == trigger
    )
    assert (
        c.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='durable_reciprocal_review_cycles_v2'"
        ).fetchone()
        is None
    )
    c.close()


def test_direct_fake_constructor_cross_org_and_expired_auth_write_nothing(tmp_path: Path) -> None:
    path = tmp_path / "authority.sqlite"
    _seed(path)
    assert not hasattr(human_disposition_module, "_CONSTRUCTION_CAPABILITY")
    assert not hasattr(human_disposition_module, "SqliteReciprocalReviewHumanDispositionUnitOfWork")
    with pytest.raises(ValueError):
        create_sqlite_reciprocal_review_human_disposition_uow(
            path,
            trusted_human_authority_keys={},
            trusted_ai_execution_keys={"ai": b"ai-key"},
            clock=lambda: NOW,
        )
    uow = create_sqlite_reciprocal_review_human_disposition_uow(
        path,
        trusted_human_authority_keys={"human": b"human-key"},
        trusted_ai_execution_keys={"ai": b"ai-key"},
        clock=lambda: NOW,
    )
    for principal in (
        _principal().model_copy(update={"org_id": "other"}),
        _principal().model_copy(update={"authenticated_at": datetime(2000, 1, 1, tzinfo=UTC)}),
    ):
        with pytest.raises(SqliteReciprocalReviewHumanDispositionError):
            uow.submit(principal, _command())
    c = sqlite3.connect(path)
    assert c.execute(
        "SELECT count(*) FROM reciprocal_review_human_disposition_receipts"
    ).fetchone() == (0,)
    c.close()


@pytest.mark.parametrize(
    "human_keys,ai_keys",
    [
        ({}, {"ai": b"ai-key"}),
        ({"human": b"human-key"}, {}),
        ({"": b"human-key"}, {"ai": b"ai-key"}),
        ({"   ": b"human-key"}, {"ai": b"ai-key"}),
        ({"human": "not-bytes"}, {"ai": b"ai-key"}),
        ({"human": b"human-key"}, {"ai": b""}),
        ({"human": b"human-key"}, {"\t": b"ai-key"}),
    ],
)
def test_factory_rejects_invalid_trusted_key_registries_before_db_write(
    tmp_path: Path, human_keys: object, ai_keys: object
) -> None:
    path = tmp_path / "invalid-keys.sqlite"
    with pytest.raises(ValueError):
        create_sqlite_reciprocal_review_human_disposition_uow(
            path,
            trusted_human_authority_keys=human_keys,  # pyright: ignore[reportArgumentType]
            trusted_ai_execution_keys=ai_keys,  # pyright: ignore[reportArgumentType]
            clock=lambda: NOW,
        )
    assert not path.exists()


@pytest.mark.parametrize("mutation", ["signature", "orphan_audit", "orphan_outbox"])
def test_restored_tamper_or_orphan_evidence_fails_closed(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / f"{mutation}.sqlite"
    _seed(path)
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys=OFF")
    if mutation == "signature":
        c.execute("DROP TRIGGER reciprocal_review_ai_advisory_batches_no_update")
        c.execute("UPDATE reciprocal_review_ai_advisory_batches SET signature='forged'")
    else:
        table = (
            "reciprocal_review_human_disposition_audit"
            if mutation == "orphan_audit"
            else "reciprocal_review_human_disposition_outbox"
        )
        c.execute(f"DROP TRIGGER {table}_no_insert") if False else None
        # Tables have no INSERT trigger; this forged orphan bypasses FK only for corruption simulation.
        if mutation == "orphan_audit":
            c.execute(
                f"INSERT INTO {table} VALUES(?,?,?,?,?)",
                ("org", "forged-audit", "forged-receipt", SHA, "2026-07-20T00:00:00.000Z"),
            )
        else:
            c.execute(
                f"INSERT INTO {table} VALUES(?,?,?,?,?)",
                ("org", "forged-outbox", "forged-receipt", SHA, "2026-07-20T00:00:00.000Z"),
            )
    c.commit()
    c.close()
    uow = create_sqlite_reciprocal_review_human_disposition_uow(
        path,
        trusted_human_authority_keys={"human": b"human-key"},
        trusted_ai_execution_keys={"ai": b"ai-key"},
        clock=lambda: NOW,
    )
    with pytest.raises(Exception):
        uow.submit(_principal(), _command())
    c = sqlite3.connect(path)
    assert c.execute(
        "SELECT count(*) FROM reciprocal_review_human_disposition_receipts"
    ).fetchone() == (0,)
    forbidden = ("binding_pending", "bound", "source", "proposal", "evaluation", "promotion")
    assert not any(
        any(token in name for token in forbidden)
        for (name,) in c.execute("SELECT name FROM sqlite_schema")
    )
    c.close()
