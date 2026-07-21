"""S4.3a.1 authority-gated durable Conflict-open ingress.

This is deliberately a narrow, non-MCP command boundary.  It consumes the
S4.3a.0 authority and Registry proof, writes only digest references, and shares
Completion's transaction for the Case, immutable baseline, evidence graph, and
Request state transition.  It does not wake, dispatch, escalate, lease, or
select a Manager/root.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import re
from functools import lru_cache
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    CentralAuthorizer,
)
from agent_org_network.conflict_open_contract import (
    ConflictOpenCandidateClaim,
    ConflictOpenContractError,
    ConflictOpenRegistrySnapshot,
    ConflictOpenRegistrySnapshotReader,
    conflict_open_resource,
)
from agent_org_network.question_request import AwaitingConflict, HandlingAssignment, Received
from agent_org_network.sqlite_durable_conflict_escalation_baseline import (
    validate_sqlite_durable_conflict_escalation_baseline_connection,
)


class DurableConflictOpenIngressError(RuntimeError):
    """Fail-closed Conflict-open ingress error without raw authority data."""


@dataclass(frozen=True)
class DurableConflictOpenCommand:
    conflict_id: str
    request_id: str
    claims: tuple[ConflictOpenCandidateClaim, ...]


@dataclass(frozen=True)
class DurableConflictOpenResult:
    conflict_id: str
    request_id: str
    receipt_id: str
    request_revision: int


_ACTION: Final = "conflict.open"
_COMPONENT: Final = "durable_conflict_open_ingress_v1"
_TABLES: Final = (
    "durable_conflict_open_under_claim_evidence",
    "durable_conflict_open_receipts",
    "durable_conflict_open_audit_intents",
    "durable_conflict_open_outbox_intents",
    "durable_conflict_open_results",
)
_DDLS: Final = (
    "CREATE TABLE durable_conflict_open_under_claim_evidence (conflict_id TEXT PRIMARY KEY NOT NULL, candidate_claim_sha256 TEXT NOT NULL, candidate_snapshot_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(conflict_id) REFERENCES durable_linked_conflict_cases(conflict_id) ON UPDATE RESTRICT ON DELETE RESTRICT)",
    "CREATE TABLE durable_conflict_open_receipts (receipt_id TEXT PRIMARY KEY NOT NULL, conflict_id TEXT NOT NULL UNIQUE, org_id TEXT NOT NULL, request_id TEXT NOT NULL, command_digest TEXT NOT NULL UNIQUE, principal_subject_ref TEXT NOT NULL, action TEXT NOT NULL, expected_request_revision INTEGER NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(conflict_id) REFERENCES durable_linked_conflict_cases(conflict_id) ON UPDATE RESTRICT ON DELETE RESTRICT)",
    "CREATE TABLE durable_conflict_open_audit_intents (receipt_id TEXT PRIMARY KEY NOT NULL, command_digest TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(receipt_id) REFERENCES durable_conflict_open_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT)",
    "CREATE TABLE durable_conflict_open_outbox_intents (receipt_id TEXT PRIMARY KEY NOT NULL, command_digest TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(receipt_id) REFERENCES durable_conflict_open_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT)",
    "CREATE TABLE durable_conflict_open_results (receipt_id TEXT PRIMARY KEY NOT NULL, conflict_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL, request_revision INTEGER NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(receipt_id) REFERENCES durable_conflict_open_receipts(receipt_id) ON UPDATE RESTRICT ON DELETE RESTRICT)",
)
_SHA_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_TIME_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?[+-][0-9]{2}:[0-9]{2}\Z"
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _command_digest(
    *,
    org_id: str,
    principal_ref: str,
    conflict_id: str,
    request_id: str,
    candidate_snapshot_sha256: str,
    candidate_claim_sha256: str,
) -> str:
    """Canonical persisted-only command identity; never needs raw preimages."""
    return _sha(
        _canonical(
            {
                "action": _ACTION,
                "org": org_id,
                "principal": principal_ref,
                "conflict": conflict_id,
                "request": request_id,
                "snapshot": candidate_snapshot_sha256,
                "claims": candidate_claim_sha256,
            }
        )
    )


def _no_fault(_point: str) -> None:
    return None


def _catalog(connection: sqlite3.Connection) -> dict[str, object]:
    tables: list[dict[str, object]] = []
    for table in _TABLES:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            raise DurableConflictOpenIngressError("Conflict-open canonical table이 없습니다.")
        tables.append(
            {
                "name": table,
                "ddl": " ".join(row[0].replace("\n", " ").split()).casefold().rstrip(";"),
                "columns": [
                    tuple(value) for value in connection.execute(f'PRAGMA table_xinfo("{table}")')
                ],
                "foreign_keys": [
                    tuple(value)
                    for value in connection.execute(f'PRAGMA foreign_key_list("{table}")')
                ],
            }
        )
    return {"component_id": _COMPONENT, "schema_version": 1, "tables": tables}


@lru_cache(maxsize=1)
def _expected_manifest() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "CREATE TABLE durable_linked_conflict_cases(conflict_id TEXT PRIMARY KEY)"
        )
        for ddl in _DDLS:
            connection.execute(ddl)
        return _canonical(_catalog(connection))
    finally:
        connection.close()


def migrate_sqlite_durable_conflict_open_ingress_schema(path: str) -> None:
    """Explicit companion capability install; never alters a released component."""
    import sqlite3
    from pathlib import Path
    from agent_org_network.sqlite_durable_conflict_escalation_baseline import (
        migrate_sqlite_durable_conflict_escalation_baseline_schema,
    )

    # Parent migration is explicit too; calling it here keeps this unreleased
    # companion install usable only on its declared capable parent.
    migrate_sqlite_durable_conflict_escalation_baseline_schema(path)
    con = sqlite3.connect(str(Path(path)))
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT 1 FROM schema_component_manifests WHERE component_id=?", (_COMPONENT,)
        ).fetchone()
        if existing:
            con.commit()
            return
        if any(
            con.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (t,)
            ).fetchone()
            for t in _TABLES
        ):
            raise DurableConflictOpenIngressError(
                "partial Conflict-open ingress schema는 복구하지 않습니다."
            )
        for ddl in _DDLS:
            con.execute(ddl)
        manifest = _expected_manifest()
        con.execute(
            "INSERT INTO schema_component_manifests(component_id,schema_version,manifest_json,manifest_sha256) VALUES(?,?,?,?)",
            (_COMPONENT, 1, manifest, _sha(manifest)),
        )
        con.commit()
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def _validate(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    # Component rows are only ever created by this UoW.  Parent validation is
    # intentionally repeated at every command; catalog/row corruption closes it.
    validate_sqlite_durable_conflict_escalation_baseline_connection(
        connection, org_id=org_id, reconcile_rows=reconcile_rows
    )
    for table in _TABLES:
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is None
        ):
            raise DurableConflictOpenIngressError("Conflict-open ingress capability가 없습니다.")
    marker = connection.execute(
        "SELECT schema_version,manifest_json,manifest_sha256 FROM schema_component_manifests WHERE component_id=?",
        (_COMPONENT,),
    ).fetchone()
    expected = _expected_manifest()
    if (
        marker is None
        or marker[0] != 1
        or marker[1] != expected
        or marker[2] != _sha(expected)
        or _canonical(_catalog(connection)) != expected
    ):
        raise DurableConflictOpenIngressError(
            "Conflict-open ingress manifest가 canonical하지 않습니다."
        )
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise DurableConflictOpenIngressError("Conflict-open ingress foreign key가 손상됐습니다.")
    if not reconcile_rows:
        return
    parameters: tuple[object, ...] = () if org_id is None else (org_id,)
    evidence_orphan = "SELECT 1 FROM durable_conflict_open_under_claim_evidence e JOIN durable_linked_conflict_cases c ON c.conflict_id=e.conflict_id LEFT JOIN durable_conflict_open_receipts r ON r.conflict_id=e.conflict_id WHERE r.conflict_id IS NULL"
    if org_id is not None:
        evidence_orphan += " AND c.org_id=?"
    if connection.execute(evidence_orphan, parameters).fetchone() is not None:
        raise DurableConflictOpenIngressError("Conflict-open orphan companion row가 있습니다.")
    sql = "SELECT c.conflict_id,c.org_id,c.request_id,c.created_at,c.candidate_set_sha256,e.candidate_claim_sha256,e.candidate_snapshot_sha256,e.created_at AS evidence_created,r.receipt_id,r.org_id AS receipt_org,r.request_id AS receipt_request,r.command_digest,r.principal_subject_ref,r.action,r.expected_request_revision,r.created_at AS receipt_created,a.command_digest AS audit_digest,a.created_at AS audit_created,o.command_digest AS outbox_digest,o.created_at AS outbox_created,x.conflict_id AS result_conflict,x.request_id AS result_request,x.request_revision,x.created_at AS result_created FROM durable_conflict_open_receipts r JOIN durable_linked_conflict_cases c ON c.conflict_id=r.conflict_id LEFT JOIN durable_conflict_open_under_claim_evidence e ON e.conflict_id=c.conflict_id LEFT JOIN durable_conflict_open_audit_intents a ON a.receipt_id=r.receipt_id LEFT JOIN durable_conflict_open_outbox_intents o ON o.receipt_id=r.receipt_id LEFT JOIN durable_conflict_open_results x ON x.receipt_id=r.receipt_id"
    if org_id is not None:
        sql += " WHERE c.org_id=?"
    for row in connection.execute(sql, parameters):
        values = (
            row["conflict_id"],
            row["org_id"],
            row["request_id"],
            row["created_at"],
            row["candidate_claim_sha256"],
            row["candidate_snapshot_sha256"],
            row["evidence_created"],
            row["receipt_id"],
            row["receipt_org"],
            row["receipt_request"],
            row["command_digest"],
            row["principal_subject_ref"],
            row["action"],
            row["expected_request_revision"],
            row["receipt_created"],
            row["audit_digest"],
            row["audit_created"],
            row["outbox_digest"],
            row["outbox_created"],
            row["result_conflict"],
            row["result_request"],
            row["request_revision"],
            row["result_created"],
        )
        if any(value is None for value in values):
            raise DurableConflictOpenIngressError("Conflict-open companion graph가 불완전합니다.")
        if not (
            isinstance(row["conflict_id"], str)
            and row["conflict_id"].startswith("conflict:")
            and _SHA_RE.fullmatch(row["conflict_id"][9:])
        ):
            raise DurableConflictOpenIngressError(
                "Conflict-open conflict reference가 올바르지 않습니다."
            )
        if not all(
            isinstance(row[field], str) and _SHA_RE.fullmatch(row[field])
            for field in (
                "candidate_claim_sha256",
                "candidate_snapshot_sha256",
                "command_digest",
                "audit_digest",
                "outbox_digest",
            )
        ):
            raise DurableConflictOpenIngressError(
                "Conflict-open digest evidence가 올바르지 않습니다."
            )
        if not (
            isinstance(row["principal_subject_ref"], str)
            and row["principal_subject_ref"].startswith("subject:")
            and _SHA_RE.fullmatch(row["principal_subject_ref"][8:])
        ):
            raise DurableConflictOpenIngressError(
                "Conflict-open principal reference가 올바르지 않습니다."
            )
        if (
            row["action"] != _ACTION
            or row["expected_request_revision"] != 0
            or row["request_revision"] != 1
        ):
            raise DurableConflictOpenIngressError(
                "Conflict-open action/revision가 올바르지 않습니다."
            )
        if not (
            row["created_at"]
            == row["evidence_created"]
            == row["receipt_created"]
            == row["audit_created"]
            == row["outbox_created"]
            == row["result_created"]
        ):
            raise DurableConflictOpenIngressError("Conflict-open created_at mirror가 다릅니다.")
        if not (
            row["conflict_id"] == row["result_conflict"]
            and row["org_id"] == row["receipt_org"]
            and row["request_id"] == row["result_request"]
            and row["request_id"] == row["receipt_request"]
            and row["command_digest"] == row["audit_digest"] == row["outbox_digest"]
        ):
            raise DurableConflictOpenIngressError("Conflict-open graph lineage가 다릅니다.")
        baseline = connection.execute(
            "SELECT candidate_set_sha256 FROM durable_conflict_escalation_baselines WHERE conflict_id=?",
            (row["conflict_id"],),
        ).fetchone()
        if (
            baseline is None
            or row["candidate_snapshot_sha256"] != row["candidate_set_sha256"]
            or row["candidate_snapshot_sha256"] != baseline["candidate_set_sha256"]
        ):
            raise DurableConflictOpenIngressError(
                "Conflict-open evidence candidate snapshot이 parent와 다릅니다."
            )
        if not isinstance(row["created_at"], str) or _TIME_RE.fullmatch(row["created_at"]) is None:
            raise DurableConflictOpenIngressError(
                "Conflict-open created_at이 canonical하지 않습니다."
            )
        if row["command_digest"] != _command_digest(
            org_id=row["receipt_org"],
            principal_ref=row["principal_subject_ref"],
            conflict_id=row["conflict_id"],
            request_id=row["receipt_request"],
            candidate_snapshot_sha256=row["candidate_snapshot_sha256"],
            candidate_claim_sha256=row["candidate_claim_sha256"],
        ):
            raise DurableConflictOpenIngressError(
                "Conflict-open command digest가 persisted evidence와 다릅니다."
            )


def validate_sqlite_durable_conflict_open_ingress_connection(
    connection: sqlite3.Connection, *, org_id: str | None = None, reconcile_rows: bool = True
) -> None:
    """Validate the installed ingress evidence capability without mutating it."""
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        _validate(connection, org_id=org_id, reconcile_rows=reconcile_rows)
    finally:
        connection.row_factory = previous


class DurableConflictOpenIngressUnitOfWork:
    def __init__(
        self,
        *,
        completion: SqliteQuestionCompletionUnitOfWork,
        central_authorizer: CentralAuthorizer,
        registry_snapshot_reader: ConflictOpenRegistrySnapshotReader,
        clock: Callable[[], datetime],
        receipt_id_factory: Callable[[], str],
        baseline_id_factory: Callable[[], str],
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._tx = completion.durable_transaction()
        self._authorizer = central_authorizer
        self._reader, self._clock = registry_snapshot_reader, clock
        self._receipt_id_factory, self._baseline_id_factory = (
            receipt_id_factory,
            baseline_id_factory,
        )
        self._fault: Callable[[str], None] = fault_injector or _no_fault
        with self._tx.scope():
            self._tx.validate_component(
                lambda connection: _validate(connection, reconcile_rows=False)
            )

    def open(
        self, *, principal: AuthenticatedPrincipal, command: DurableConflictOpenCommand
    ) -> DurableConflictOpenResult:
        if (
            type(principal) is not AuthenticatedPrincipal
            or type(command) is not DurableConflictOpenCommand
        ):
            raise DurableConflictOpenIngressError(
                "서버 principal과 exact Conflict-open command가 필요합니다."
            )
        if not command.conflict_id.strip() or not command.request_id.strip() or not command.claims:
            raise DurableConflictOpenIngressError("Conflict-open command가 불완전합니다.")
        if not (
            command.conflict_id.startswith("conflict:")
            and _SHA_RE.fullmatch(command.conflict_id.removeprefix("conflict:"))
            and command.request_id.startswith("request:")
            and _SHA_RE.fullmatch(command.request_id.removeprefix("request:"))
        ):
            raise DurableConflictOpenIngressError(
                "Conflict-open은 typed digest Conflict/Question Request reference만 허용합니다."
            )
        if not (
            principal.org_id.startswith("org:")
            and _SHA_RE.fullmatch(principal.org_id.removeprefix("org:"))
        ):
            raise DurableConflictOpenIngressError(
                "Conflict-open principal 조직은 typed digest reference여야 합니다."
            )
        if len({claim.intent for claim in command.claims}) != 1:
            raise DurableConflictOpenIngressError(
                "Conflict-open initial routing에는 하나의 current intent가 필요합니다."
            )
        resource = conflict_open_resource(
            org_id=principal.org_id,
            request_id=command.request_id,
            requester_subject_id=principal.subject_id,
        )
        grant = self._authorizer.authorize(principal, _ACTION, resource)
        if type(grant) is not AuthorizationGrant:
            raise DurableConflictOpenIngressError("Conflict-open authority가 거부됐습니다.")
        with self._tx.scope():
            try:
                self._tx.begin_immediate()
                self._tx.validate_component_in_transaction(
                    lambda connection: _validate(connection, org_id=principal.org_id)
                )
                snapshot = self._snapshot(principal, command)
                # The authority resolver may be stale/forged; both its seal and
                # the actual durable Request are required at start and prewrite.
                if not self._authorizer.verify(grant, principal, _ACTION, resource):
                    raise DurableConflictOpenIngressError(
                        "current Conflict-open authority가 아닙니다."
                    )
                existing = self._tx.execute(
                    "SELECT receipt_id,command_digest FROM durable_conflict_open_receipts WHERE conflict_id=?",
                    (command.conflict_id,),
                ).fetchone()
                digest = self._digest(principal, command, snapshot)
                if existing is not None:
                    if existing["command_digest"] != digest:
                        raise DurableConflictOpenIngressError("Conflict-open replay가 다릅니다.")
                    result = self._tx.execute(
                        "SELECT * FROM durable_conflict_open_results WHERE receipt_id=?",
                        (existing["receipt_id"],),
                    ).fetchone()
                    if result is None:
                        raise DurableConflictOpenIngressError(
                            "Conflict-open replay evidence가 불완전합니다."
                        )
                    self._tx.commit()
                    return DurableConflictOpenResult(
                        command.conflict_id,
                        command.request_id,
                        existing["receipt_id"],
                        result["request_revision"],
                    )
                # Linearization point.
                request = self._received_request(principal, command)
                self._reader.verify_current(snapshot, claims=command.claims)
                if not self._authorizer.verify(grant, principal, _ACTION, resource):
                    raise DurableConflictOpenIngressError(
                        "prewrite Conflict-open authority가 아닙니다."
                    )
                now = self._clock()
                created = now.isoformat(timespec="microseconds" if now.microsecond else "seconds")
                receipt_id, baseline_id = self._receipt_id_factory(), self._baseline_id_factory()
                if not receipt_id.strip() or not baseline_id.strip():
                    raise DurableConflictOpenIngressError(
                        "Conflict-open identity가 올바르지 않습니다."
                    )
                candidate_hash = snapshot.candidate_digest
                self._tx.execute(
                    "INSERT INTO durable_linked_conflict_cases VALUES(?,?,?,0,'open',?,?)",
                    (
                        command.conflict_id,
                        principal.org_id,
                        command.request_id,
                        candidate_hash,
                        created,
                    ),
                )
                self._fault("after_case")
                candidates = [
                    (
                        i,
                        _ref("card", c.card_id),
                        _ref("subject", c.owner_subject_id),
                        _ref("domain", c.intent),
                        _sha(_canonical(c.route.model_dump(mode="json"))),
                    )
                    for i, c in enumerate(snapshot.candidates, 1)
                ]
                baseline_sha = _sha(
                    _canonical(
                        {
                            "baseline": {
                                "conflict_id": command.conflict_id,
                                "org_id": principal.org_id,
                                "request_id": command.request_id,
                                "awaiting_revision": 0,
                                "candidate_set_sha256": candidate_hash,
                                "candidate_count": len(candidates),
                                "created_at": created,
                            },
                            "candidates": [
                                {
                                    "candidate_ordinal": a,
                                    "candidate_card_ref": b,
                                    "candidate_owner_subject_ref": c,
                                    "candidate_domain_ref": d,
                                    "candidate_route_sha256": e,
                                }
                                for a, b, c, d, e in candidates
                            ],
                        }
                    )
                )
                self._tx.execute(
                    "INSERT INTO durable_conflict_escalation_baselines VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        _ref("baseline", baseline_id),
                        command.conflict_id,
                        principal.org_id,
                        command.request_id,
                        0,
                        candidate_hash,
                        len(candidates),
                        baseline_sha,
                        created,
                    ),
                )
                for candidate in candidates:
                    self._tx.execute(
                        "INSERT INTO durable_conflict_escalation_baseline_candidates VALUES(?,?,?,?,?,?)",
                        (_ref("baseline", baseline_id), *candidate),
                    )
                self._fault("after_baseline")
                rid = _ref("receipt", receipt_id)
                self._tx.execute(
                    "INSERT INTO durable_conflict_open_under_claim_evidence VALUES(?,?,?,?)",
                    (
                        command.conflict_id,
                        snapshot.claim_digest,
                        snapshot.candidate_digest,
                        created,
                    ),
                )
                self._fault("after_under_claim_evidence")
                self._tx.execute(
                    "INSERT INTO durable_conflict_open_receipts VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        rid,
                        command.conflict_id,
                        principal.org_id,
                        command.request_id,
                        digest,
                        _ref("subject", principal.subject_id),
                        _ACTION,
                        0,
                        created,
                    ),
                )
                self._fault("after_receipt")
                for table in (
                    "durable_conflict_open_audit_intents",
                    "durable_conflict_open_outbox_intents",
                ):
                    self._tx.execute(f"INSERT INTO {table} VALUES(?,?,?)", (rid, digest, created))
                    self._fault(f"after_{table}")
                assert isinstance(request.state, Received)
                target = AwaitingConflict(
                    case_id=command.conflict_id,
                    handling=HandlingAssignment(
                        kind="conflict_case",
                        ref=command.conflict_id,
                        due_at=request.state.handling.due_at,
                    ),
                )
                # Conflict-open is the initial routing decision for a Received
                # Request; ``transition`` correctly rejects inventing that
                # disposition, so use the aggregate's explicit initial-routing
                # operation rather than bypassing its invariant.
                updated = request.record_initial_routing(
                    intent=command.claims[0].intent,
                    disposition="contested",
                    target=target,
                    clock=lambda: now,
                )
                if not self._tx.compare_and_set_question_request(
                    command.request_id, 0, request, updated
                ):
                    raise DurableConflictOpenIngressError(
                        "Conflict-open Request CAS에 실패했습니다."
                    )
                self._fault("after_request_transition")
                self._tx.execute(
                    "INSERT INTO durable_conflict_open_results VALUES(?,?,?,?,?)",
                    (rid, command.conflict_id, command.request_id, updated.revision, created),
                )
                self._fault("after_request")
                self._tx.commit()
                return DurableConflictOpenResult(
                    command.conflict_id, command.request_id, rid, updated.revision
                )
            except Exception:
                if self._tx.in_transaction:
                    self._tx.rollback()
                raise

    def _received_request(
        self, principal: AuthenticatedPrincipal, command: DurableConflictOpenCommand
    ):
        request = self._tx.select_question_request(command.request_id)
        if (
            request is None
            or request.org_id != principal.org_id
            or request.requester_id != principal.subject_id
            or request.revision != 0
            or not isinstance(request.state, Received)
        ):
            raise DurableConflictOpenIngressError(
                "current Received revision=0 requester Request가 아닙니다."
            )
        return request

    def _snapshot(self, principal: AuthenticatedPrincipal, command: DurableConflictOpenCommand):
        try:
            return self._reader.snapshot(org_id=principal.org_id, claims=command.claims)
        except ConflictOpenContractError as error:
            raise DurableConflictOpenIngressError(
                "current Conflict-open Registry snapshot이 없습니다."
            ) from error

    @staticmethod
    def _digest(
        principal: AuthenticatedPrincipal, command: DurableConflictOpenCommand, snapshot: object
    ) -> str:
        if type(snapshot) is not ConflictOpenRegistrySnapshot:
            raise DurableConflictOpenIngressError(
                "Conflict-open snapshot type이 올바르지 않습니다."
            )
        return _command_digest(
            org_id=principal.org_id,
            principal_ref=_ref("subject", principal.subject_id),
            conflict_id=command.conflict_id,
            request_id=command.request_id,
            candidate_snapshot_sha256=snapshot.candidate_digest,
            candidate_claim_sha256=snapshot.claim_digest,
        )
