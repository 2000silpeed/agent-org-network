"""R5.4b scoped credential revoke core contract."""

from __future__ import annotations

import runpy
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    ResourceRef,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.credential_scoped_read import (
    CredentialReadNotFoundOrDenied,
    CredentialReadUnavailable,
)
from agent_org_network.credential_scoped_revoke import (
    CredentialRevokeCommand,
    CredentialRevokeConflict,
    CredentialRevokeResult,
    create_credential_scoped_revoke,
    create_credential_scoped_revoke_capability,
)
from agent_org_network.durable_credentials import CredentialApprovalEvidence, resource_fingerprint
from agent_org_network.sqlite_durable_credential_scope_bindings import CredentialScopeSnapshot

_SCOPED = runpy.run_path("tests/test_credential_issue_scoped_operations.py")


def _authority() -> SnapshotCentralAuthorizer:
    binding = SubjectRoleBinding(org_id="org", subject_id="principal", roles=("operator",))
    permission = RolePermission(role="operator", actions=("worker_credential.revoke",))
    raw = {
        "schema_version": 1,
        "org_id": "org",
        "policy_version": "revoke",
        "content_sha256": "pending",
        "subject_roles": [binding.model_dump(mode="json")],
        "role_permissions": [permission.model_dump(mode="json")],
        "route_rules": [],
        "worker_bindings": [],
    }
    return SnapshotCentralAuthorizer(
        AuthorityPolicySnapshot(
            schema_version=1,
            org_id="org",
            policy_version="revoke",
            content_sha256=canonical_policy_digest(raw),
            subject_roles=(binding,),
            role_permissions=(permission,),
            route_rules=(),
            worker_bindings=(),
        )
    )


@dataclass
class _Source:
    available: bool = True

    def resolve_credential_read_scope(
        self, org_id: str, credential_id: str, agent_card_id: str
    ) -> CredentialScopeSnapshot | None:
        if not self.available:
            return None
        return _SCOPED["_Source"]().resolve_issue_scope(org_id, credential_id, agent_card_id)


class _Provider:
    def __init__(self) -> None:
        self.evidence: CredentialApprovalEvidence | None = None
        self.after: object | None = None
        self.calls = 0

    def acquire_revoke_approval(
        self, p: AuthenticatedPrincipal, a: str, r: ResourceRef, d: str
    ) -> CredentialApprovalEvidence:
        self.calls += 1
        self.evidence = CredentialApprovalEvidence(
            "evidence", "worker_credential.revoke", d, resource_fingerprint(r)
        )
        if callable(self.after):
            self.after()
        return self.evidence


class _Resolver:
    def __init__(self, p: _Provider) -> None:
        self.p = p
        self.available = True
        self.calls = 0

    def resolve_credential_approval_evidence(
        self, *, org_id: str, evidence_id: str
    ) -> CredentialApprovalEvidence | None:
        self.calls += 1
        return (
            self.p.evidence
            if self.available and org_id == "org" and evidence_id == "evidence"
            else None
        )


def _operation(path: Path, clock: object = lambda: datetime(2026, 7, 19, 0, 0, 2, tzinfo=UTC)) -> tuple[Any, _Source, _Provider, _Resolver]:
    ops, *_ = _SCOPED["_operations"](path)
    reservation = _SCOPED["_reservation"]()
    ops.stage(path, reservation, "e" * 64, "memory", reservation.created_at)
    ops.materialize(path, "org", "target", "2026-07-19T00:00:01.000Z")
    source = _Source()
    provider = _Provider()
    resolver = _Resolver(provider)
    cap = create_credential_scoped_revoke_capability(
        server_principal=_SCOPED["_principal"](),
        central_authorizer=_authority(),
        reader_source=source,
        approval_provider=provider,
        approval_resolver=resolver,
        server_clock=clock,
    )
    return _TestOperation(create_credential_scoped_revoke(cap)), source, provider, resolver


class _TestOperation:
    """Legacy test-call adapter; production accepts only CredentialRevokeCommand."""

    def __init__(self, operation: Any) -> None:
        self._operation = operation

    def revoke(self, path: Path, org_id: str, credential_id: str, **kwargs: Any) -> object:
        kwargs.pop("revoked_at")
        command_id = kwargs.pop("command_id")
        command = CredentialRevokeCommand(
            "none" if command_id is None else command_id,
            kwargs.pop("attempt"),
            kwargs.pop("expected_generation"),
            kwargs.pop("expected_revision"),
        )
        return self._operation.revoke(path, org_id, credential_id, command=command, **kwargs)


def test_revoke_cas_receipt_replay_and_closed_without_receipt_conflict(tmp_path: Path) -> None:
    operation, *_ = _operation(tmp_path / "revoke.sqlite")
    result = operation.revoke(
        tmp_path / "revoke.sqlite",
        "org",
        "credential",
        expected_generation=1,
        expected_revision=1,
        command_id="receipt",
        attempt=1,
        revoked_at="2026-07-19T00:00:02.000Z",
    )
    assert (
        isinstance(result, CredentialRevokeResult)
        and result.credential.status == "revoked"
        and result.credential.revision == 2
    )
    replay = operation.revoke(
        tmp_path / "revoke.sqlite",
        "org",
        "credential",
        expected_generation=1,
        expected_revision=1,
        command_id="receipt",
        attempt=1,
        revoked_at="2026-07-19T00:00:02.000Z",
    )
    assert replay == result
    assert (
        operation.revoke(
            tmp_path / "revoke.sqlite",
            "org",
            "credential",
            expected_generation=1,
            expected_revision=2,
            command_id=None,
            attempt=1,
            revoked_at="2026-07-19T00:00:03.000Z",
        )
        == CredentialRevokeConflict()
    )


def test_absence_is_neutral_and_proof_drift_or_fault_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "rollback.sqlite"
    operation, source, _provider, _resolver = _operation(path)
    assert (
        operation.revoke(
            path,
            "org",
            "absent",
            expected_generation=1,
            expected_revision=1,
            command_id="none",
            attempt=1,
            revoked_at="2026-07-19T00:00:02.000Z",
        )
        == CredentialReadNotFoundOrDenied()
    )
    source.available = False
    assert (
        operation.revoke(
            path,
            "org",
            "credential",
            expected_generation=1,
            expected_revision=1,
            command_id="drift",
            attempt=1,
            revoked_at="2026-07-19T00:00:02.000Z",
        )
        == CredentialReadUnavailable()
    )
    source.available = True

    def fail_audit(point: str) -> None:
        if point == "audit":
            raise RuntimeError(point)

    assert (
        operation.revoke(
            path,
            "org",
            "credential",
            expected_generation=1,
            expected_revision=1,
            command_id="fault",
            attempt=1,
            revoked_at="2026-07-19T00:00:02.000Z",
            fault_injector=fail_audit,
        )
        == CredentialReadUnavailable()
    )
    import sqlite3

    c = sqlite3.connect(path)
    try:
        assert (
            c.execute(
                "SELECT status FROM durable_credentials WHERE org_id='org' AND credential_id='credential'"
            ).fetchone()[0]
            == "active"
        )
    finally:
        c.close()


def test_factory_is_exact_single_use_and_receipt_semantic_mismatch_conflicts(
    tmp_path: Path,
) -> None:
    operation, *_ = _operation(tmp_path / "factory.sqlite")
    assert isinstance(
        operation.revoke(
            tmp_path / "factory.sqlite",
            "org",
            "credential",
            expected_generation=1,
            expected_revision=1,
            command_id="r",
            attempt=1,
            revoked_at="2026-07-19T00:00:02.000Z",
        ),
        CredentialRevokeResult,
    )
    assert (
        operation.revoke(
            tmp_path / "factory.sqlite",
            "org",
            "credential",
            expected_generation=1,
            expected_revision=2,
            command_id="r",
            attempt=1,
            revoked_at="2026-07-19T00:00:03.000Z",
        )
        == CredentialRevokeConflict()
    )
    with pytest.raises(TypeError):
        create_credential_scoped_revoke_capability(
            server_principal=object(),
            central_authorizer=_authority(),
            reader_source=_Source(),
            approval_provider=_Provider(),
            approval_resolver=object(),
            server_clock=object(),
        )


def test_production_revoke_accepts_only_exact_frozen_command(tmp_path: Path) -> None:
    operation, *_ = _operation(tmp_path / "typed.sqlite")
    assert (
        operation._operation.revoke(  # pyright: ignore[reportPrivateUsage]
            tmp_path / "typed.sqlite",
            "org",
            "credential",
            command=object(),
        )
        == CredentialReadUnavailable()
    )
    result = operation._operation.revoke(  # pyright: ignore[reportPrivateUsage]
        tmp_path / "typed.sqlite",
        "org",
        "credential",
        command=CredentialRevokeCommand("typed", 1, 1, 1),
    )
    assert isinstance(result, CredentialRevokeResult)
    with pytest.raises(TypeError):
        operation._operation.revoke(  # pyright: ignore[reportPrivateUsage]
            tmp_path / "typed.sqlite", "org", "credential", command=_command("other", revision=2), revoked_at="caller"
        )


@pytest.mark.parametrize("point", ("update", "receipt", "audit", "outbox", "readback"))
def test_every_write_fault_rolls_back_lifecycle_and_companion(tmp_path: Path, point: str) -> None:
    path = tmp_path / f"fault-{point}.sqlite"
    operation, *_ = _operation(path)

    def fail(actual: str) -> None:
        if actual == point:
            raise RuntimeError(actual)

    assert (
        operation.revoke(
            path,
            "org",
            "credential",
            expected_generation=1,
            expected_revision=1,
            command_id="receipt",
            attempt=1,
            revoked_at="2026-07-19T00:00:02.000Z",
            fault_injector=fail,
        )
        == CredentialReadUnavailable()
    )
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT status,revision FROM durable_credentials WHERE org_id='org' AND credential_id='credential'"
        ).fetchone() == ("active", 1)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'durable_credential_revoke_%'"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_extra_companion_catalog_and_cross_org_receipt_close_boundary(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    operation, *_ = _operation(path)
    assert isinstance(
        operation.revoke(
            path,
            "org",
            "credential",
            expected_generation=1,
            expected_revision=1,
            command_id="receipt",
            attempt=1,
            revoked_at="2026-07-19T00:00:02.000Z",
        ),
        CredentialRevokeResult,
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE INDEX injected_revoke_index ON durable_credential_revoke_receipts_v1(org_id)"
        )
        connection.commit()
    finally:
        connection.close()
    assert (
        operation.revoke(
            path,
            "other",
            "credential",
            expected_generation=1,
            expected_revision=1,
            command_id="receipt",
            attempt=1,
            revoked_at="2026-07-19T00:00:03.000Z",
        )
        == CredentialReadUnavailable()
    )
    assert (
        operation.revoke(
            path,
            "org",
            "credential",
            expected_generation=1,
            expected_revision=1,
            command_id="receipt",
            attempt=1,
            revoked_at="2026-07-19T00:00:03.000Z",
        )
        == CredentialReadUnavailable()
    )


def test_eight_concurrent_expected_revision_commands_have_one_commit(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite"
    operation, *_ = _operation(path)

    def attempt(index: int) -> object:
        return operation.revoke(
            path,
            "org",
            "credential",
            expected_generation=1,
            expected_revision=1,
            command_id=f"receipt-{index}",
            attempt=1,
            revoked_at="2026-07-19T00:00:02.000Z",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(attempt, range(8)))
    assert sum(isinstance(value, CredentialRevokeResult) for value in results) == 1
    assert all(
        isinstance(value, (CredentialRevokeResult, CredentialRevokeConflict)) for value in results
    )


def _command(command_id: str, attempt: int = 1, generation: int = 1, revision: int = 1) -> CredentialRevokeCommand:
    return CredentialRevokeCommand(command_id, attempt, generation, revision)


def _direct_revoke(operation: Any, path: Path, credential_id: str, command: CredentialRevokeCommand) -> object:
    return operation._operation.revoke(  # pyright: ignore[reportPrivateUsage]
        path, "org", credential_id, command=command
    )


@pytest.mark.parametrize(
    ("credential_id", "command", "principal"),
    (
        ("other", _command("bound"), None),
        ("credential", _command("bound", 2), None),
        ("credential", _command("bound", 1, 2), None),
        ("credential", _command("bound", 1, 1, 2), None),
        ("credential", _command("bound"), "other-principal"),
    ),
)
def test_receipt_binds_every_scalar_and_principal(tmp_path: Path, credential_id: str, command: CredentialRevokeCommand, principal: str | None) -> None:
    path = tmp_path / "bound.sqlite"
    operation, *_ = _operation(path)
    assert isinstance(_direct_revoke(operation, path, "credential", _command("bound")), CredentialRevokeResult)
    if principal is not None:
        operation._operation._principal = AuthenticatedPrincipal(  # pyright: ignore[reportPrivateUsage]
            org_id="org", subject_id=principal, identity_provider="idp", identity_session_id="session"
        )
    assert _direct_revoke(operation, path, credential_id, command) == CredentialRevokeConflict()


def test_replay_requires_current_scope_and_authority_but_never_reacquires_hitl(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite"
    operation, source, provider, resolver = _operation(path)
    command = _command("replay")
    assert isinstance(_direct_revoke(operation, path, "credential", command), CredentialRevokeResult)
    assert provider.calls == 1
    assert resolver.calls == 2
    provider_calls = provider.calls
    resolver_calls = resolver.calls
    resolver.available = False
    assert isinstance(_direct_revoke(operation, path, "credential", command), CredentialRevokeResult)
    assert provider.calls == provider_calls
    assert resolver.calls == resolver_calls
    source.available = False
    assert _direct_revoke(operation, path, "credential", command) == CredentialReadUnavailable()


def test_replay_closes_for_missing_projection_or_per_resource_reauthorization(tmp_path: Path) -> None:
    path = tmp_path / "replay-current.sqlite"
    operation, *_ = _operation(path)
    command = _command("current")
    assert isinstance(_direct_revoke(operation, path, "credential", command), CredentialRevokeResult)

    class _UnavailableAuthority:
        def authorize(self, *args: object) -> object:
            raise RuntimeError("unavailable")

    operation._operation._authorizer = cast(SnapshotCentralAuthorizer, _UnavailableAuthority())  # pyright: ignore[reportPrivateUsage]
    assert _direct_revoke(operation, path, "credential", command) == CredentialReadUnavailable()
    projection_path = tmp_path / "replay-projection.sqlite"
    operation, *_ = _operation(projection_path)
    command = _command("projection")
    assert isinstance(_direct_revoke(operation, projection_path, "credential", command), CredentialRevokeResult)
    connection = sqlite3.connect(projection_path)
    try:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='durable_credential_scope_projections_v1_no_delete'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER durable_credential_scope_projections_v1_no_delete")
        connection.execute("DELETE FROM durable_credential_scope_projections_v1")
        connection.execute(trigger)
        connection.commit()
    finally:
        connection.close()
    assert _direct_revoke(operation, projection_path, "credential", command) == CredentialReadUnavailable()


@pytest.mark.parametrize(
    "statement",
    (
        "DROP TRIGGER durable_credential_revoke_audit_v1_no_update",
        "CREATE INDEX revoke_extra ON durable_credential_revoke_audit_v1(org_id)",
        "PRAGMA foreign_keys=OFF",
    ),
)
def test_catalog_migration_drift_closes_before_revoke(tmp_path: Path, statement: str) -> None:
    path = tmp_path / "drift.sqlite"
    operation, *_ = _operation(path)
    assert isinstance(_direct_revoke(operation, path, "credential", _command("first")), CredentialRevokeResult)
    connection = sqlite3.connect(path)
    try:
        connection.execute(statement)
        if statement == "PRAGMA foreign_keys=OFF":
            connection.execute("DROP TABLE durable_credential_revoke_outbox_v1")
        connection.commit()
    finally:
        connection.close()
    assert _direct_revoke(operation, path, "credential", _command("next", revision=2)) == CredentialReadUnavailable()


@pytest.mark.parametrize("column,value", (("revoked_at", "2026-99-99T99:99:99.999Z"), ("command_id", "secret")))
def test_check_bypassed_forged_companion_rows_close(tmp_path: Path, column: str, value: str) -> None:
    path = tmp_path / "forged.sqlite"
    operation, *_ = _operation(path)
    assert isinstance(_direct_revoke(operation, path, "credential", _command("forged")), CredentialRevokeResult)
    connection = sqlite3.connect(path)
    try:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='durable_credential_revoke_receipts_v1_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER durable_credential_revoke_receipts_v1_no_update")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(f"UPDATE durable_credential_revoke_receipts_v1 SET {column}=?", (value,))
        connection.execute(trigger)
        connection.commit()
    finally:
        connection.close()
    assert _direct_revoke(operation, path, "credential", _command("forged")) == CredentialReadUnavailable()


@pytest.mark.parametrize(
    ("table", "timestamp_column"),
    (
        ("durable_credential_revoke_receipts_v1", "revoked_at"),
        ("durable_credential_revoke_audit_v1", "occurred_at"),
        ("durable_credential_revoke_outbox_v1", "occurred_at"),
    ),
)
def test_well_formed_check_bypassed_each_companion_row_closes(
    tmp_path: Path, table: str, timestamp_column: str
) -> None:
    path = tmp_path / "all-forged.sqlite"
    operation, *_ = _operation(path)
    assert isinstance(_direct_revoke(operation, path, "credential", _command("all-forged")), CredentialRevokeResult)
    trigger_name = f"{table}_no_update"
    connection = sqlite3.connect(path)
    try:
        trigger = connection.execute("SELECT sql FROM sqlite_schema WHERE name=?", (trigger_name,)).fetchone()[0]
        connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(f"UPDATE {table} SET {timestamp_column}='not-a-time'")
        connection.execute(trigger)
        connection.commit()
    finally:
        connection.close()
    assert _direct_revoke(operation, path, "credential", _command("all-forged")) == CredentialReadUnavailable()


def test_partial_companion_migration_closes_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "partial.sqlite"
    operation, *_ = _operation(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE durable_credential_revoke_receipts_v1(x INTEGER)")
        connection.commit()
    finally:
        connection.close()
    assert _direct_revoke(operation, path, "credential", _command("partial")) == CredentialReadUnavailable()


@pytest.mark.parametrize("clock", (lambda: object(), lambda: datetime(2026, 7, 19), lambda: (_ for _ in ()).throw(RuntimeError())))
def test_server_timestamp_failure_rejects_without_any_write(tmp_path: Path, clock: object) -> None:
    path = tmp_path / "time.sqlite"
    operation, *_ = _operation(path, clock)
    result = operation._operation.revoke(  # pyright: ignore[reportPrivateUsage]
        path, "org", "credential", command=_command("time")
    )
    assert result == CredentialReadUnavailable()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT status,revision FROM durable_credentials").fetchone() == ("active", 1)
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'durable_credential_revoke_%'").fetchone() == (0,)
    finally:
        connection.close()


def test_orphan_or_mixed_org_companion_rows_close_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "orphan.sqlite"
    operation, *_ = _operation(path)
    assert isinstance(_direct_revoke(operation, path, "credential", _command("orphan")), CredentialRevokeResult)
    connection = sqlite3.connect(path)
    try:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='durable_credential_revoke_audit_v1_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER durable_credential_revoke_audit_v1_no_update")
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("UPDATE durable_credential_revoke_audit_v1 SET org_id='other'")
        connection.execute(trigger)
        connection.commit()
        before = connection.execute("SELECT count(*) FROM durable_credential_revoke_audit_v1").fetchone()
    finally:
        connection.close()
    assert _direct_revoke(operation, path, "credential", _command("orphan")) == CredentialReadUnavailable()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM durable_credential_revoke_audit_v1").fetchone() == before
    finally:
        connection.close()


def test_serialized_revoke_boundary_never_exposes_forbidden_material(tmp_path: Path) -> None:
    path = tmp_path / "redaction.sqlite"
    operation, *_ = _operation(path)
    result = _direct_revoke(operation, path, "credential", _command("redacted"))
    assert isinstance(result, CredentialRevokeResult)
    serialized = repr(result)
    connection = sqlite3.connect(path)
    try:
        serialized += repr(
            tuple(
                connection.execute("SELECT * FROM durable_credential_revoke_receipts_v1").fetchone()
            )
        )
        serialized += repr(tuple(connection.execute("SELECT * FROM durable_credential_revoke_audit_v1").fetchone()))
        serialized += repr(tuple(connection.execute("SELECT * FROM durable_credential_revoke_outbox_v1").fetchone()))
    finally:
        connection.close()
    assert all(word not in serialized.lower() for word in ("secret", "hash", "delivery", "evidence", "digest", "grant", "source"))


def test_eight_concurrent_same_command_has_one_companion_set(tmp_path: Path) -> None:
    path = tmp_path / "same-command.sqlite"
    operation, *_ = _operation(path)

    def same_command(_: int) -> object:
        return _direct_revoke(operation, path, "credential", _command("same"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(same_command, range(8)))
    assert all(isinstance(value, CredentialRevokeResult) for value in results)
    connection = sqlite3.connect(path)
    try:
        assert tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "durable_credential_revoke_receipts_v1",
                "durable_credential_revoke_audit_v1",
                "durable_credential_revoke_outbox_v1",
            )
        ) == (1, 1, 1)
    finally:
        connection.close()
