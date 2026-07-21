from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.central_authority import (
    Action,
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.conflict_open_contract import (
    ConflictOpenCandidateClaim,
    RegistryConflictOpenSnapshotReader,
)
from agent_org_network.question_request import AwaitingConflict, QuestionRequest, RouteTarget
from agent_org_network.registry import Registry
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
)
from agent_org_network.sqlite_durable_conflict_open_ingress import (
    DurableConflictOpenCommand,
    DurableConflictOpenIngressError,
    DurableConflictOpenIngressUnitOfWork,
    migrate_sqlite_durable_conflict_open_ingress_schema,
    _validate,  # pyright: ignore[reportPrivateUsage]
)
from agent_org_network.user import User

NOW = datetime(2026, 7, 16, tzinfo=UTC)


def _ref(kind: str, label: str) -> str:
    return f"{kind}:{hashlib.sha256(label.encode()).hexdigest()}"


class _Authority:
    def __init__(self) -> None:
        self.allowed = True
        self.verifications = 0

    def authorize(
        self, principal: AuthenticatedPrincipal, action: str, resource: ResourceRef
    ) -> AuthorizationGrant:
        if not self.allowed:
            return object()  # type: ignore[return-value]
        return AuthorizationGrant(  # type: ignore[call-arg]
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=cast(Action, action),
            resource=resource,
            roles=("requester",),
            policy_version="p",
            policy_digest="a" * 64,
        )

    def verify(
        self,
        grant: AuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: str,
        resource: ResourceRef,
    ) -> bool:
        self.verifications += 1
        return self.allowed


class _Scope:
    def proves_registry_org(self, *, registry: Registry, org_id: str) -> bool:
        return org_id == _ref("org", "acme")


def _card(card: str, owner: str) -> AgentCard:
    from datetime import date

    return AgentCard(
        agent_id=card,
        owner=owner,
        team="t",
        summary="s",
        domains=["billing"],
        last_reviewed_at=date(2026, 1, 1),
    )


def _prepared(tmp_path: Path):
    path = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    migrate_sqlite_durable_conflict_open_ingress_schema(str(path))
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=cast(Any, object()),
        approvals=cast(Any, object()),
        responsibility_resolver=cast(Any, object()),
        record_id_factory=lambda: "r",
        clock=lambda: NOW,
    )
    org, request_id, conflict = _ref("org", "acme"), _ref("request", "one"), _ref("conflict", "one")
    request = QuestionRequest.receive(
        org_id=org,
        requester_id="requester",
        question="raw secret question",
        request_id_factory=lambda: request_id,
        clock=lambda: NOW - timedelta(minutes=1),
        due_at=NOW + timedelta(hours=1),
    )
    completion.create(request)
    registry = Registry()
    for user in ("requester", "owner-1", "owner-2"):
        registry.register_user(User(id=user))
    registry.register(_card("card-1", "owner-1"))
    registry.register(_card("card-2", "owner-2"))
    claims = tuple(
        ConflictOpenCandidateClaim(
            card, "billing", RouteTarget(intent="billing", agent_id=card, requires_approval=False)
        )
        for card in ("card-1", "card-2")
    )
    authority = _Authority()
    uow = DurableConflictOpenIngressUnitOfWork(
        completion=completion,
        central_authorizer=authority,
        registry_snapshot_reader=RegistryConflictOpenSnapshotReader(registry, _Scope()),
        clock=lambda: NOW,
        receipt_id_factory=lambda: "receipt",
        baseline_id_factory=lambda: "baseline",
    )  # type: ignore[arg-type]
    return (
        completion,
        authority,
        uow,
        AuthenticatedPrincipal(
            org_id=org, subject_id="requester", identity_provider="oidc", identity_session_id="s"
        ),
        DurableConflictOpenCommand(conflict, request_id, claims),
    )


def test_open은_case_baseline_evidence와_awaiting_conflict를_원자적으로_만든다(
    tmp_path: Path,
) -> None:
    completion, authority, uow, principal, command = _prepared(tmp_path)
    try:
        result = uow.open(principal=principal, command=command)
        assert result.request_revision == 1 and authority.verifications == 2
        request = completion.get(command.request_id)
        assert request is not None and isinstance(request.state, AwaitingConflict)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert (
                tx.execute("SELECT COUNT(*) FROM durable_conflict_escalation_baselines").fetchone()[
                    0
                ]
                == 1
            )
            assert (
                tx.execute("SELECT COUNT(*) FROM durable_conflict_open_results").fetchone()[0] == 1
            )
            assert (
                tx.execute(
                    "SELECT candidate_card_ref FROM durable_conflict_escalation_baseline_candidates"
                ).fetchone()[0]
                != "card-1"
            )
            tx.commit()
    finally:
        completion.close()


def test_prewrite_revoke는_아무것도_남기지_않는다(tmp_path: Path) -> None:
    completion, authority, uow, principal, command = _prepared(tmp_path)
    try:

        def revoke(*args: object) -> bool:
            authority.verifications += 1
            return authority.verifications < 2

        authority.verify = revoke  # type: ignore[method-assign]
        with pytest.raises(DurableConflictOpenIngressError):
            uow.open(principal=principal, command=command)
        assert completion.get(command.request_id).revision == 0  # type: ignore[union-attr]
    finally:
        completion.close()


@pytest.mark.parametrize(
    "point",
    (
        "after_case",
        "after_baseline",
        "after_under_claim_evidence",
        "after_receipt",
        "after_durable_conflict_open_audit_intents",
        "after_durable_conflict_open_outbox_intents",
        "after_request_transition",
        "after_request",
    ),
)
def test_every_ingress_fault_rolls_back_the_entire_graph(tmp_path: Path, point: str) -> None:
    completion, _, _, principal, command = _prepared(tmp_path)
    try:
        # Build a new UoW with deterministic failure at every public commit seam.
        registry = Registry()
        for user in ("requester", "owner-1", "owner-2"):
            registry.register_user(User(id=user))
        registry.register(_card("card-1", "owner-1"))
        registry.register(_card("card-2", "owner-2"))
        failing = DurableConflictOpenIngressUnitOfWork(
            completion=completion,
            central_authorizer=_Authority(),  # type: ignore[arg-type]
            registry_snapshot_reader=RegistryConflictOpenSnapshotReader(registry, _Scope()),
            clock=lambda: NOW,
            receipt_id_factory=lambda: "receipt",
            baseline_id_factory=lambda: "baseline",
            fault_injector=lambda actual: (
                (_ for _ in ()).throw(RuntimeError("boom")) if actual == point else None
            ),
        )
        with pytest.raises(RuntimeError):
            failing.open(principal=principal, command=command)
        assert completion.get(command.request_id).revision == 0  # type: ignore[union-attr]
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert (
                tx.execute("SELECT COUNT(*) FROM durable_linked_conflict_cases").fetchone()[0] == 0
            )
            assert (
                tx.execute("SELECT COUNT(*) FROM durable_conflict_open_receipts").fetchone()[0] == 0
            )
            tx.commit()
    finally:
        completion.close()


def test_32_concurrent_same_open_leaves_exactly_one_graph(tmp_path: Path) -> None:
    completion, _, uow, principal, command = _prepared(tmp_path)
    try:

        def open_once(_: int):
            return uow.open(principal=principal, command=command)

        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(open_once, range(32)))
        assert {result.receipt_id for result in results} == {_ref("receipt", "receipt")}
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            for table in (
                "durable_linked_conflict_cases",
                "durable_conflict_escalation_baselines",
                "durable_conflict_open_under_claim_evidence",
                "durable_conflict_open_receipts",
                "durable_conflict_open_audit_intents",
                "durable_conflict_open_outbox_intents",
                "durable_conflict_open_results",
            ):
                assert tx.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
            tx.commit()
    finally:
        completion.close()


def test_postcommit_replay_requires_current_authority(tmp_path: Path) -> None:
    completion, authority, uow, principal, command = _prepared(tmp_path)
    try:
        first = uow.open(principal=principal, command=command)
        assert uow.open(principal=principal, command=command) == first
        authority.allowed = False
        with pytest.raises(DurableConflictOpenIngressError):
            uow.open(principal=principal, command=command)
    finally:
        completion.close()


def test_postcommit_candidate_owner_drift_rejects_replay(tmp_path: Path) -> None:
    completion, _, uow, principal, command = _prepared(tmp_path)
    try:
        uow.open(principal=principal, command=command)
        # The snapshot reader intentionally rereads Registry before replay.
        cast(Registry, getattr(uow._reader, "registry")).replace_card(  # pyright: ignore[reportPrivateUsage]
            _card("card-1", "owner-2")
        )
        with pytest.raises(DurableConflictOpenIngressError):
            uow.open(principal=principal, command=command)
    finally:
        completion.close()


def test_correct_hash_companion_graph_tamper_fails_closed_without_repair(tmp_path: Path) -> None:
    completion, authority, uow, principal, command = _prepared(tmp_path)
    try:
        uow.open(principal=principal, command=command)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_conflict_open_audit_intents SET created_at=?",
                ("2026-07-17T00:00:00+00:00",),
            )
            tx.commit()
        reopened = DurableConflictOpenIngressUnitOfWork(
            completion=completion,
            central_authorizer=authority,  # type: ignore[arg-type]
            registry_snapshot_reader=uow._reader,  # pyright: ignore[reportPrivateUsage]
            clock=lambda: NOW,
            receipt_id_factory=lambda: "receipt",
            baseline_id_factory=lambda: "baseline",
        )
        with pytest.raises(DurableConflictOpenIngressError):
            reopened.open(principal=principal, command=command)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert (
                tx.execute("SELECT created_at FROM durable_conflict_open_audit_intents").fetchone()[
                    0
                ]
                == "2026-07-17T00:00:00+00:00"
            )
            tx.commit()
    finally:
        completion.close()


@pytest.mark.parametrize("raw", ("plain conflict prose", "secret-token-value"))
def test_raw_conflict_or_request_identifier_is_rejected_before_any_write(
    tmp_path: Path, raw: str
) -> None:
    completion, _, uow, principal, command = _prepared(tmp_path)
    try:
        with pytest.raises(DurableConflictOpenIngressError):
            uow.open(
                principal=principal,
                command=DurableConflictOpenCommand(raw, command.request_id, command.claims),
            )
        assert completion.get(command.request_id).revision == 0  # type: ignore[union-attr]
    finally:
        completion.close()


def test_raw_or_secret_principal_org_is_rejected_before_authority_or_write(tmp_path: Path) -> None:
    completion, authority, uow, _, command = _prepared(tmp_path)
    principal = AuthenticatedPrincipal(
        org_id="secret-org-value",
        subject_id="requester",
        identity_provider="oidc",
        identity_session_id="s",
    )
    try:
        with pytest.raises(DurableConflictOpenIngressError):
            uow.open(principal=principal, command=command)
        assert authority.verifications == 0
        assert completion.get(command.request_id).revision == 0  # type: ignore[union-attr]
    finally:
        completion.close()


def test_own_org_underclaim_orphan_fails_closed_without_repair(tmp_path: Path) -> None:
    completion, authority, uow, principal, command = _prepared(tmp_path)
    try:
        uow.open(principal=principal, command=command)
        # Simulate an out-of-band corrupt DB; normal FK-restricted APIs cannot
        # create this graph, which is exactly why reconciliation must detect it.
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute("DELETE FROM durable_conflict_open_audit_intents")
            tx.execute("DELETE FROM durable_conflict_open_outbox_intents")
            tx.execute("DELETE FROM durable_conflict_open_results")
            tx.execute("DELETE FROM durable_conflict_open_receipts")
            tx.commit()
        reopened = DurableConflictOpenIngressUnitOfWork(
            completion=completion,
            central_authorizer=authority,  # type: ignore[arg-type]
            registry_snapshot_reader=uow._reader,  # pyright: ignore[reportPrivateUsage]
            clock=lambda: NOW,
            receipt_id_factory=lambda: "receipt",
            baseline_id_factory=lambda: "baseline",
        )
        with pytest.raises(DurableConflictOpenIngressError):
            reopened.open(principal=principal, command=command)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert (
                tx.execute(
                    "SELECT COUNT(*) FROM durable_conflict_open_under_claim_evidence"
                ).fetchone()[0]
                == 1
            )
            tx.commit()
    finally:
        completion.close()


def test_correct_hash_receipt_org_request_cross_tamper_fails_without_repair(tmp_path: Path) -> None:
    completion, authority, uow, principal, command = _prepared(tmp_path)
    try:
        uow.open(principal=principal, command=command)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "UPDATE durable_conflict_open_receipts SET org_id=?,request_id=?",
                (_ref("org", "other"), _ref("request", "other")),
            )
            tx.commit()
        reopened = DurableConflictOpenIngressUnitOfWork(
            completion=completion,
            central_authorizer=authority,  # type: ignore[arg-type]
            registry_snapshot_reader=uow._reader,  # pyright: ignore[reportPrivateUsage]
            clock=lambda: NOW,
            receipt_id_factory=lambda: "receipt",
            baseline_id_factory=lambda: "baseline",
        )
        with pytest.raises(DurableConflictOpenIngressError):
            reopened.open(principal=principal, command=command)
    finally:
        completion.close()


def test_foreign_org_scalar_corruption_is_scoped_but_own_org_fails(tmp_path: Path) -> None:
    completion, _, uow, principal, command = _prepared(tmp_path)
    try:
        uow.open(principal=principal, command=command)
        other_org, other_request, other_conflict = (
            _ref("org", "other"),
            _ref("request", "other"),
            _ref("conflict", "other"),
        )
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(
                "INSERT INTO question_requests(request_id,org_id,requester_id,session_id,question,context_snapshot,intent,initial_disposition,state_kind,state_json,state_schema_version,revision,created_at,updated_at) VALUES(?,?, 'u',NULL,'q',NULL,NULL,NULL,'received','{}',1,0,?,?)",
                (other_request, other_org, NOW.isoformat(), NOW.isoformat()),
            )
            tx.execute(
                "INSERT INTO durable_linked_conflict_cases VALUES(?,?,?,0,'open',?,?)",
                (other_conflict, other_org, other_request, "a" * 64, NOW.isoformat()),
            )
            tx.execute(
                "INSERT INTO durable_conflict_open_receipts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    _ref("receipt", "other"),
                    other_conflict,
                    other_org,
                    other_request,
                    "not-a-digest",
                    _ref("subject", "x"),
                    "conflict.open",
                    0,
                    NOW.isoformat(),
                ),
            )
            tx.commit()
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.validate_component_in_transaction(
                lambda connection: _validate(connection, org_id=principal.org_id)
            )
            with pytest.raises(DurableConflictOpenIngressError):
                tx.validate_component_in_transaction(
                    lambda connection: _validate(connection, org_id=other_org)
                )
            tx.rollback()
    finally:
        completion.close()


@pytest.mark.parametrize(
    ("table", "column", "value"),
    (
        ("durable_conflict_open_under_claim_evidence", "candidate_claim_sha256", "b" * 64),
        ("durable_conflict_open_under_claim_evidence", "candidate_snapshot_sha256", "c" * 64),
        ("durable_conflict_open_receipts", "principal_subject_ref", _ref("subject", "other")),
    ),
)
def test_correct_shape_evidence_or_command_tamper_fails_without_repair(
    tmp_path: Path, table: str, column: str, value: str
) -> None:
    completion, authority, uow, principal, command = _prepared(tmp_path)
    try:
        uow.open(principal=principal, command=command)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.execute(f"UPDATE {table} SET {column}=?", (value,))
            tx.commit()
        reopened = DurableConflictOpenIngressUnitOfWork(
            completion=completion,
            central_authorizer=authority,  # type: ignore[arg-type]
            registry_snapshot_reader=uow._reader,  # pyright: ignore[reportPrivateUsage]
            clock=lambda: NOW,
            receipt_id_factory=lambda: "receipt",
            baseline_id_factory=lambda: "baseline",
        )
        with pytest.raises(DurableConflictOpenIngressError):
            reopened.open(principal=principal, command=command)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert tx.execute(f"SELECT {column} FROM {table}").fetchone()[0] == value
            tx.commit()
    finally:
        completion.close()
