from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_org_network.reciprocal_review import HumanPrincipal, RegisterArtifactRevision
from agent_org_network.sqlite_durable_reciprocal_review import (
    SqliteDurableReciprocalReviewError,
    migrate_sqlite_durable_reciprocal_review_ledger,
    open_sqlite_durable_reciprocal_review_ledger,
)
from agent_org_network.sqlite_reciprocal_review_uow import (
    ArtifactContent,
    InitialReviewPolicy,
    InitialReviewRequirement,
    SqliteReciprocalReviewConflict,
    SqliteReciprocalReviewRegistrationError,
    create_sqlite_reciprocal_review_uow,
)


NOW = datetime(2026, 7, 20, tzinfo=UTC)
SHA = "a" * 64


class _Content:
    def verify(self, *, org_id: str, content_ref: str, content_sha256: str) -> ArtifactContent:
        assert org_id == "org" and content_ref == "git:commit"
        return ArtifactContent(content_sha256=content_sha256, source_boundary_digest="b" * 64)


class _Policy:
    def initial_policy(self, *, org_id: str, kind: str, effective_provenance_kind: str) -> InitialReviewPolicy:
        assert (org_id, kind, effective_provenance_kind) == ("org", "knowledge", "human")
        return InitialReviewPolicy(
            policy_digest="c" * 64,
            requirements=(
                InitialReviewRequirement("ai", "all", 1, "rubric-ai", NOW, False),
                InitialReviewRequirement("human", "all", 1, "rubric-human", NOW, False, ("reviewer",)),
            ),
        )


class _Authorization:
    def authorize_registration(self, *, principal: HumanPrincipal, artifact_id: str) -> bool:
        return principal.subject_id in {"author", "other"} and artifact_id == "artifact"

    def authorize_reviewer(self, *, principal: HumanPrincipal, contributor_subject_ids: tuple[str, ...]) -> bool:
        return principal.subject_id not in contributor_subject_ids


def _command(*, receipt_id: str = "receipt", content_digest: str = SHA) -> RegisterArtifactRevision:
    return RegisterArtifactRevision(
        receipt_id=receipt_id, artifact_id="artifact", revision_id="revision", kind="knowledge",
        content_ref="git:commit", content_sha256=content_digest,
        provenance_event_id="event", audit_id="audit", outbox_id="outbox",
    )


def _principal(subject_id: str = "author") -> HumanPrincipal:
    return HumanPrincipal(org_id="org", subject_id=subject_id, authenticated_at=NOW, authn_context_digest=SHA)


def _uow(path: Path):  # type: ignore[no-untyped-def]
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    connection.close()
    return create_sqlite_reciprocal_review_uow(path, content_verifier=_Content(), review_policy_registry=_Policy(), reviewer_authorization=_Authorization(), clock=lambda: NOW)


def _all_counts(path: Path) -> tuple[int, ...]:
    connection = sqlite3.connect(path)
    try:
        return tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in (
            "durable_reciprocal_review_artifact_revisions", "durable_reciprocal_review_provenance_events",
            "durable_reciprocal_review_cycles", "durable_reciprocal_review_requirements",
            "durable_reciprocal_review_audit_events", "durable_reciprocal_review_outbox_intents",
            "durable_reciprocal_review_command_receipts",
        ))
    finally:
        connection.close()


def test_register_revision_writes_immutable_revision_cycle_requirements_and_body_free_records(tmp_path: Path) -> None:
    uow = _uow(tmp_path / "review.sqlite")
    result = uow.register(_principal(), _command())
    assert result.revision_id == "revision" and result.cycle_id
    connection = sqlite3.connect(tmp_path / "review.sqlite")
    try:
        assert connection.execute("SELECT count(*) FROM durable_reciprocal_review_artifact_revisions").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM durable_reciprocal_review_cycles WHERE state_kind='review_open' AND active=1").fetchone() == (1,)
        assert connection.execute("SELECT reviewer_kind FROM durable_reciprocal_review_requirements ORDER BY reviewer_kind").fetchall() == [("ai",), ("human",)]
        audit = connection.execute("SELECT event_digest FROM durable_reciprocal_review_audit_events").fetchone()
        outbox = connection.execute("SELECT payload_digest FROM durable_reciprocal_review_outbox_intents").fetchone()
        assert audit == (hashlib.sha256(b"audit").hexdigest(),) and outbox == (hashlib.sha256(b"outbox").hexdigest(),)
    finally:
        connection.close()


def test_register_replay_is_exact_but_changed_principal_or_payload_conflicts(tmp_path: Path) -> None:
    uow = _uow(tmp_path / "review.sqlite")
    first = uow.register(_principal(), _command())
    assert uow.register(_principal(), _command()) == first
    with pytest.raises(SqliteReciprocalReviewConflict):
        uow.register(_principal("other"), _command())
    with pytest.raises(SqliteReciprocalReviewConflict):
        uow.register(_principal(), _command(content_digest="d" * 64))


def test_register_rejects_cycle_provenance_digest_mismatch_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "cycle-provenance.sqlite"
    uow = _uow(path)
    connection = sqlite3.connect(path)
    lineage_digest = hashlib.sha256(b'["event"]').hexdigest()
    connection.execute("INSERT INTO durable_reciprocal_review_artifact_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("org", "forged-artifact", "forged", 1, None, "git:commit", SHA, "internal", "boundary", "human", SHA, lineage_digest, SHA, None, 1, "2026-07-20T00:00:00.000Z"))
    connection.execute("INSERT INTO durable_reciprocal_review_provenance_events VALUES(?,?,?,?,?,?,?,?)", ("org", "event", "forged", "human", "author", SHA, None, "2026-07-20T00:00:00.000Z"))
    connection.execute("INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)", ("org", "forged", "event", 1))
    connection.execute("INSERT INTO durable_reciprocal_review_cycles VALUES(?,?,?,?,?,?,?,?,?)", ("org", "cycle-forged", "forged", 1, "review_open", 1, "d" * 64, "c" * 64, "2026-07-20T00:00:00.000Z"))
    connection.commit()
    connection.close()
    before = _all_counts(path)
    with pytest.raises(SqliteDurableReciprocalReviewError):
        uow.register(_principal(), _command())
    assert _all_counts(path) == before


def test_direct_provenance_content_digest_corruption_makes_open_and_registration_unavailable_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "direct-provenance-content.sqlite"
    uow = _uow(path)
    connection = sqlite3.connect(path)
    lineage_digest = hashlib.sha256(b'["event"]').hexdigest()
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute("INSERT INTO durable_reciprocal_review_artifact_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("org", "forged-artifact", "forged", 1, None, "git:commit", "a" * 64, "internal", "boundary", "human", "b" * 64, lineage_digest, "b" * 64, None, 1, "2026-07-20T00:00:00.000Z"))
    connection.execute("INSERT INTO durable_reciprocal_review_provenance_events VALUES(?,?,?,?,?,?,?,?)", ("org", "event", "forged", "human", "author", "b" * 64, None, "2026-07-20T00:00:00.000Z"))
    connection.execute("INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)", ("org", "forged", "event", 1))
    connection.commit()
    connection.close()
    before = _all_counts(path)
    with pytest.raises(SqliteDurableReciprocalReviewError):
        open_sqlite_durable_reciprocal_review_ledger(path)
    with pytest.raises(SqliteDurableReciprocalReviewError):
        uow.register(_principal(), _command())
    assert _all_counts(path) == before


def test_inherited_provenance_binds_to_its_own_revision_content_not_child_content(tmp_path: Path) -> None:
    path = tmp_path / "inherited-provenance-content.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    parent_digest = hashlib.sha256(b'["parent-event"]').hexdigest()
    child_digest = hashlib.sha256(b'["child-event","parent-event"]').hexdigest()
    for revision_id, revision_no, parent_revision_id, content_digest, lineage_digest in (
        ("parent", 1, None, "a" * 64, parent_digest),
        ("child", 2, "parent", "b" * 64, child_digest),
    ):
        connection.execute("INSERT INTO durable_reciprocal_review_artifact_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("org", "artifact", revision_id, revision_no, parent_revision_id, "git:commit", content_digest, "internal", "boundary", "human", "c" * 64, lineage_digest, "c" * 64, None, 1, "2026-07-20T00:00:00.000Z"))
    connection.execute("INSERT INTO durable_reciprocal_review_provenance_events VALUES(?,?,?,?,?,?,?,?)", ("org", "parent-event", "parent", "human", "author", "a" * 64, None, "2026-07-20T00:00:00.000Z"))
    connection.execute("INSERT INTO durable_reciprocal_review_provenance_events VALUES(?,?,?,?,?,?,?,?)", ("org", "child-event", "child", "human", "author", "b" * 64, None, "2026-07-20T00:00:00.000Z"))
    connection.execute("INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)", ("org", "parent", "parent-event", 1))
    for ordinal, event_id in enumerate(("child-event", "parent-event"), start=1):
        connection.execute("INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)", ("org", "child", event_id, ordinal))
    connection.commit()
    connection.close()
    open_sqlite_durable_reciprocal_review_ledger(path).close()


def test_same_receipt_with_changed_policy_conflicts_without_new_write(tmp_path: Path) -> None:
    class ChangedPolicy(_Policy):
        changed = False
        def initial_policy(self, **kwargs: str) -> InitialReviewPolicy:
            result = super().initial_policy(**kwargs)
            return InitialReviewPolicy("d" * 64, result.requirements) if self.changed else result

    path = tmp_path / "review.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    connection.close()
    policy = ChangedPolicy()
    uow = create_sqlite_reciprocal_review_uow(path, content_verifier=_Content(), review_policy_registry=policy, reviewer_authorization=_Authorization(), clock=lambda: NOW)
    uow.register(_principal(), _command())
    policy.changed = True
    with pytest.raises(SqliteReciprocalReviewConflict):
        uow.register(_principal(), _command())
    assert _all_counts(path) == (1, 1, 1, 2, 1, 1, 1)


def test_register_rejects_contributor_as_human_reviewer_and_rolls_back_on_fault(tmp_path: Path) -> None:
    class SameHumanPolicy(_Policy):
        def initial_policy(self, **kwargs: object) -> InitialReviewPolicy:
            return InitialReviewPolicy("c" * 64, (
                InitialReviewRequirement("ai", "all", 1, "rubric-ai", NOW, False),
                InitialReviewRequirement("human", "all", 1, "rubric", NOW, False, ("reviewer",)),
            ))

    path = tmp_path / "review.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    connection.close()
    uow = create_sqlite_reciprocal_review_uow(path, content_verifier=_Content(), review_policy_registry=SameHumanPolicy(), reviewer_authorization=_Authorization(), clock=lambda: NOW, fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError(point)) if point == "after_cycle" else None)
    with pytest.raises(RuntimeError, match="after_cycle"):
        uow.register(_principal(), _command())
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM durable_reciprocal_review_artifact_revisions").fetchone() == (0,)
    finally:
        connection.close()


def test_human_contributor_cannot_be_selected_as_own_reviewer(tmp_path: Path) -> None:
    class SelfReviewPolicy(_Policy):
        def initial_policy(self, **kwargs: str) -> InitialReviewPolicy:
            return InitialReviewPolicy("c" * 64, (
                InitialReviewRequirement("ai", "all", 1, "rubric-ai", NOW, False),
                InitialReviewRequirement("human", "all", 1, "rubric-human", NOW, False, ("author",)),
            ))

    path = tmp_path / "review.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    connection.close()
    uow = create_sqlite_reciprocal_review_uow(path, content_verifier=_Content(), review_policy_registry=SelfReviewPolicy(), reviewer_authorization=_Authorization(), clock=lambda: NOW)
    with pytest.raises(SqliteReciprocalReviewRegistrationError, match="contributor"):
        uow.register(_principal(), _command())
    assert _all_counts(path) == (0, 0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize("point", ("after_revision", "after_provenance", "after_lineage", "after_cycle", "after_requirement", "after_audit", "after_outbox", "after_receipt"))
def test_every_registration_insert_fault_rolls_back_the_entire_aggregate(tmp_path: Path, point: str) -> None:
    path = tmp_path / f"{point}.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    connection.close()
    uow = create_sqlite_reciprocal_review_uow(path, content_verifier=_Content(), review_policy_registry=_Policy(), reviewer_authorization=_Authorization(), clock=lambda: NOW, fault_injector=lambda actual: (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None)
    with pytest.raises(RuntimeError, match=point):
        uow.register(_principal(), _command())
    connection = sqlite3.connect(path)
    try:
        for table in (
            "durable_reciprocal_review_artifact_revisions", "durable_reciprocal_review_provenance_events",
            "durable_reciprocal_review_cycles", "durable_reciprocal_review_requirements",
            "durable_reciprocal_review_audit_events", "durable_reciprocal_review_outbox_intents",
            "durable_reciprocal_review_command_receipts",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    finally:
        connection.close()


def test_32_way_same_command_has_one_immutable_aggregate(tmp_path: Path) -> None:
    path = tmp_path / "review.sqlite"
    _uow(path)
    def register_once(_index: int):  # type: ignore[no-untyped-def]
        return _uow(path).register(_principal(), _command())
    with ThreadPoolExecutor(max_workers=32) as executor:
        results = list(executor.map(register_once, range(32)))
    assert len(set(results)) == 1
    assert _all_counts(path) == (1, 1, 1, 2, 1, 1, 1)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM durable_reciprocal_review_lineage_members").fetchone() == (1,)
    finally:
        connection.close()


def test_identical_ids_are_isolated_by_org(tmp_path: Path) -> None:
    class AnyContent(_Content):
        def verify(self, *, org_id: str, content_ref: str, content_sha256: str) -> ArtifactContent:
            return ArtifactContent(content_sha256, "b" * 64)

    class AnyPolicy(_Policy):
        def initial_policy(self, *, org_id: str, kind: str, effective_provenance_kind: str) -> InitialReviewPolicy:
            return InitialReviewPolicy("c" * 64, (InitialReviewRequirement("ai", "all", 1, "rubric-ai", NOW, False),))

    path = tmp_path / "review.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    connection.close()
    uow = create_sqlite_reciprocal_review_uow(path, content_verifier=AnyContent(), review_policy_registry=AnyPolicy(), reviewer_authorization=_Authorization(), clock=lambda: NOW)
    assert uow.register(_principal(), _command()).org_id == "org"
    assert uow.register(_principal("other").model_copy(update={"org_id": "otherorg"}), _command()).org_id == "otherorg"
    assert _all_counts(path) == (2, 2, 2, 2, 2, 2, 2)


@pytest.mark.parametrize("drift", ("content", "boundary", "policy", "authorization"))
def test_prewrite_drift_leaves_every_table_empty(tmp_path: Path, drift: str) -> None:
    class DriftContent(_Content):
        calls = 0
        def verify(self, **kwargs: str) -> ArtifactContent:
            self.calls += 1
            result = super().verify(**kwargs)
            if self.calls == 2 and drift == "content":
                return ArtifactContent("d" * 64, result.source_boundary_digest)
            if self.calls == 2 and drift == "boundary":
                return ArtifactContent(result.content_sha256, "d" * 64)
            return result

    class DriftPolicy(_Policy):
        calls = 0
        def initial_policy(self, **kwargs: str) -> InitialReviewPolicy:
            self.calls += 1
            result = super().initial_policy(**kwargs)
            return InitialReviewPolicy("d" * 64, result.requirements) if self.calls == 2 and drift == "policy" else result

    class DriftAuth(_Authorization):
        calls = 0
        def authorize_registration(self, **kwargs: object) -> bool:
            self.calls += 1
            return drift != "authorization" or self.calls == 1

    path = tmp_path / "review.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    connection.close()
    uow = create_sqlite_reciprocal_review_uow(path, content_verifier=DriftContent(), review_policy_registry=DriftPolicy(), reviewer_authorization=DriftAuth(), clock=lambda: NOW)
    with pytest.raises(SqliteReciprocalReviewRegistrationError):
        uow.register(_principal(), _command())
    assert _all_counts(path) == (0, 0, 0, 0, 0, 0, 0)


def test_schema_drift_at_entry_or_commit_means_no_write(tmp_path: Path) -> None:
    entry_path = tmp_path / "entry.sqlite"
    entry_uow = _uow(entry_path)
    connection = sqlite3.connect(entry_path)
    connection.execute("DROP INDEX durable_reciprocal_review_requirements_cycle_idx")
    connection.commit()
    connection.close()
    with pytest.raises(Exception):
        entry_uow.register(_principal(), _command())
    commit_path = tmp_path / "commit.sqlite"
    connection = sqlite3.connect(commit_path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    connection.close()
    def corrupt_before_commit(connection: sqlite3.Connection, point: str) -> None:
        if point == "before_commit_validation":
            connection.execute("DROP INDEX durable_reciprocal_review_requirements_cycle_idx")
    uow = create_sqlite_reciprocal_review_uow(commit_path, content_verifier=_Content(), review_policy_registry=_Policy(), reviewer_authorization=_Authorization(), clock=lambda: NOW, transaction_fault_injector=corrupt_before_commit)
    with pytest.raises(Exception):
        uow.register(_principal(), _command())
    assert _all_counts(commit_path) == (0, 0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize(("parent_kind", "expected"), (("ai", "mixed"), ("unknown", "unknown")))
def test_parent_provenance_is_unioned_server_side_and_never_downgraded(tmp_path: Path, parent_kind: str, expected: str) -> None:
    class DerivedPolicy(_Policy):
        seen: list[str] = []
        def initial_policy(self, *, org_id: str, kind: str, effective_provenance_kind: str) -> InitialReviewPolicy:
            self.seen.append(effective_provenance_kind)
            return super().initial_policy(org_id=org_id, kind=kind, effective_provenance_kind="human")

    path = tmp_path / "review.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    lineage_digest = hashlib.sha256(_canonical_lineage("parent-event")).hexdigest()
    connection.execute("INSERT INTO durable_reciprocal_review_artifact_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("org", "artifact", "parent", 1, None, "git:parent", SHA, "internal", "boundary", parent_kind, "b" * 64, lineage_digest, "b" * 64, None, 1, "2026-07-20T00:00:00.000Z"))
    principal_kind = "model_execution" if parent_kind == "ai" else "imported_unknown"
    connection.execute("INSERT INTO durable_reciprocal_review_provenance_events VALUES(?,?,?,?,?,?,?,?)", ("org", "parent-event", "parent", principal_kind, "source", SHA, None, "2026-07-20T00:00:00.000Z"))
    connection.execute("INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)", ("org", "parent", "parent-event", 1))
    connection.commit()
    connection.close()
    policy = DerivedPolicy()
    uow = create_sqlite_reciprocal_review_uow(path, content_verifier=_Content(), review_policy_registry=policy, reviewer_authorization=_Authorization(), clock=lambda: NOW)
    result = uow.register(_principal(), _command(receipt_id=f"receipt-{parent_kind}").model_copy(update={"revision_id": f"child-{parent_kind}", "parent_revision_id": "parent", "provenance_event_id": f"event-{parent_kind}", "audit_id": f"audit-{parent_kind}", "outbox_id": f"outbox-{parent_kind}"}))
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT provenance_kind,lineage_digest FROM durable_reciprocal_review_artifact_revisions WHERE revision_id=?", (result.revision_id,)).fetchone()
        assert row == (expected, hashlib.sha256(_canonical_lineage("parent-event", f"event-{parent_kind}")).hexdigest())
    finally:
        connection.close()
    assert policy.seen == [expected, expected]


def test_three_generation_closure_persists_all_events_and_preserves_unknown(tmp_path: Path) -> None:
    class AnyDerivedPolicy(_Policy):
        def initial_policy(self, *, org_id: str, kind: str, effective_provenance_kind: str) -> InitialReviewPolicy:
            return InitialReviewPolicy("c" * 64, (InitialReviewRequirement("ai", "all", 1, "rubric", NOW, False),))

    path = tmp_path / "review.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    ai_digest = hashlib.sha256(_canonical_lineage("ai-event")).hexdigest()
    connection.execute("INSERT INTO durable_reciprocal_review_artifact_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("org", "artifact", "ai-root", 1, None, "git:ai", SHA, "internal", "boundary", "ai", "b" * 64, ai_digest, "b" * 64, None, 1, "2026-07-20T00:00:00.000Z"))
    connection.execute("INSERT INTO durable_reciprocal_review_provenance_events VALUES(?,?,?,?,?,?,?,?)", ("org", "ai-event", "ai-root", "model_execution", "model", SHA, None, "2026-07-20T00:00:00.000Z"))
    connection.execute("INSERT INTO durable_reciprocal_review_lineage_members VALUES(?,?,?,?)", ("org", "ai-root", "ai-event", 1))
    connection.commit()
    connection.close()
    uow = create_sqlite_reciprocal_review_uow(path, content_verifier=_Content(), review_policy_registry=AnyDerivedPolicy(), reviewer_authorization=_Authorization(), clock=lambda: NOW)
    child = _command(receipt_id="child").model_copy(update={"revision_id": "human-child", "parent_revision_id": "ai-root", "provenance_event_id": "human-event-1", "audit_id": "audit-child", "outbox_id": "outbox-child"})
    grandchild = _command(receipt_id="grandchild").model_copy(update={"revision_id": "human-grandchild", "parent_revision_id": "human-child", "provenance_event_id": "human-event-2", "audit_id": "audit-grandchild", "outbox_id": "outbox-grandchild"})
    uow.register(_principal(), child)
    uow.register(_principal(), grandchild)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT event_id FROM durable_reciprocal_review_lineage_members WHERE revision_id='human-grandchild' ORDER BY ordinal").fetchall() == [("ai-event",), ("human-event-1",), ("human-event-2",)]
        assert connection.execute("SELECT provenance_kind FROM durable_reciprocal_review_artifact_revisions WHERE revision_id='human-grandchild'").fetchone() == ("mixed",)
    finally:
        connection.close()


def _canonical_lineage(*events: str) -> bytes:
    import json
    return json.dumps(sorted(events), ensure_ascii=False, separators=(",", ":")).encode()


def test_requirement_snapshot_persists_full_policy_without_reviewer_pii(tmp_path: Path) -> None:
    class FullPolicy(_Policy):
        def initial_policy(self, **kwargs: str) -> InitialReviewPolicy:
            return InitialReviewPolicy("c" * 64, (InitialReviewRequirement("ai", "quorum", 2, "rubric", NOW, True, (), "different_deployment", "high", "d" * 64),))

    path = tmp_path / "review.sqlite"
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    connection.close()
    uow = create_sqlite_reciprocal_review_uow(path, content_verifier=_Content(), review_policy_registry=FullPolicy(), reviewer_authorization=_Authorization(), clock=lambda: NOW)
    uow.register(_principal(), _command())
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT independence_rule,risk_class,reviewer_assignment_digest,waivable FROM durable_reciprocal_review_requirements").fetchone() == ("different_deployment", "high", "d" * 64, 1)
    finally:
        connection.close()
