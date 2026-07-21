"""S4.3b read-only evidence gate for a durable open Conflict Case.

The reader is intentionally not an escalation command.  It has no schema,
write, receipt, authority, Manager, or root-selection surface.  It merely
returns sealed typed evidence a later, separately authorized UoW may consume.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Final, TypeAlias, cast

from agent_org_network.conflict_open_contract import (
    ConflictOpenCandidateClaim,
    ConflictOpenContractError,
    ConflictOpenRegistrySnapshotReader,
    conflict_open_claim_digest,
)
from agent_org_network.sqlite_durable_conflict_escalation_baseline import (
    SqliteDurableConflictEscalationBaselineSchemaError,
    validate_sqlite_durable_conflict_escalation_baseline_connection,
)
from agent_org_network.sqlite_durable_conflict_open_ingress import (
    DurableConflictOpenIngressError,
    validate_sqlite_durable_conflict_open_ingress_connection,
)
from agent_org_network.sqlite_durable_direct_conflict_uow import (
    SqliteDurableDirectConflictUowSchemaError,
    validate_sqlite_durable_direct_conflict_uow_connection,
)

MANAGER_SELECTION_AVAILABLE: Final = False
ROOT_SELECTION_AVAILABLE: Final = False


class DurableConflictEscalationEvidenceError(RuntimeError):
    """Durable evidence is absent, corrupt, or cannot safely be interpreted."""


@dataclass(frozen=True)
class Pending:
    """A valid open Conflict round that is not sealed for escalation."""

    org_ref: str
    conflict_ref: str
    request_ref: str
    awaiting_revision: int
    concurrence_round: int
    candidate_snapshot_sha256: str
    baseline_sha256: str
    candidate_claim_sha256: str
    vote_set_sha256: str
    evaluated_at: str
    accepted_vote_count: int
    candidate_owner_count: int


@dataclass(frozen=True)
class CandidateRegistryChanged:
    """Current typed Registry snapshot differs from ingress evidence."""

    org_ref: str
    conflict_ref: str
    request_ref: str
    awaiting_revision: int
    concurrence_round: int
    candidate_snapshot_sha256: str
    current_candidate_snapshot_sha256: str
    baseline_sha256: str
    candidate_claim_sha256: str
    vote_set_sha256: str
    evaluated_at: str
    reason: Final[str] = "snapshot_digest_mismatch"


@dataclass(frozen=True)
class DivergentVotes:
    """Every candidate Owner voted, and every target is distinct."""

    org_ref: str
    conflict_ref: str
    request_ref: str
    awaiting_revision: int
    concurrence_round: int
    candidate_snapshot_sha256: str
    candidate_owner_count: int
    baseline_sha256: str
    candidate_claim_sha256: str
    vote_set_sha256: str
    evaluated_at: str
    reason: Final[str] = "divergent_votes"


SealedEscalationEvidence: TypeAlias = CandidateRegistryChanged | DivergentVotes
ConflictEscalationEvidence: TypeAlias = Pending | SealedEscalationEvidence


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _ref(kind: str, raw: str) -> str:
    return f"{kind}:{_sha(raw)}"


def _route_sha(claim: ConflictOpenCandidateClaim) -> str:
    return _sha(_canonical(claim.route.model_dump(mode="json")))


def _exact_claims(claims: object) -> tuple[ConflictOpenCandidateClaim, ...]:
    if not isinstance(claims, (tuple, list)):
        raise DurableConflictEscalationEvidenceError("ordered Conflict-open candidate claim이 필요합니다.")
    raw_values = cast(tuple[object, ...] | list[object], claims)
    if any(type(value) is not ConflictOpenCandidateClaim for value in raw_values):
        raise DurableConflictEscalationEvidenceError("ordered Conflict-open candidate claim이 필요합니다.")
    values = tuple(cast(ConflictOpenCandidateClaim, value) for value in raw_values)
    # Delegate all structural and ordering checks to the authority-owned digest.
    try:
        conflict_open_claim_digest(values)
    except ConflictOpenContractError as error:
        raise DurableConflictEscalationEvidenceError("Conflict-open claim이 exact하지 않습니다.") from error
    return values


@dataclass(frozen=True)
class DurableConflictEscalationEvidenceReader:
    """Read one scoped Conflict Case under one short Registry guard.

    The SQLite connection is URI ``mode=ro``.  The Registry reader's snapshot
    holds its own short consistency guard; no database transaction or lock is
    retained across that guard.
    """

    db_path: str | Path
    registry_snapshot_reader: ConflictOpenRegistrySnapshotReader
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def read(
        self, *, org_id: str, conflict_id: str, claims: tuple[ConflictOpenCandidateClaim, ...] | list[ConflictOpenCandidateClaim]
    ) -> ConflictEscalationEvidence:
        canonical_claims = _exact_claims(claims)
        if not re.fullmatch(r"org:[0-9a-f]{64}", org_id) or not conflict_id.startswith("conflict:"):
            # Inputs are already persistence typed references; raw principal
            # input is never silently hashed into a storage identifier.
            raise DurableConflictEscalationEvidenceError("typed organization/Conflict reference가 필요합니다.")
        if len(conflict_id) != len("conflict:") + 64 or any(c not in "0123456789abcdef" for c in conflict_id[9:]):
            raise DurableConflictEscalationEvidenceError("typed Conflict reference가 필요합니다.")
        connection = self._open()
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            self._validate_scoped(connection, org_id=org_id)
            case = connection.execute(
                "SELECT conflict_id,org_id,request_id,awaiting_revision,status,candidate_set_sha256 FROM durable_linked_conflict_cases WHERE conflict_id=? AND org_id=?",
                (conflict_id, org_id),
            ).fetchone()
            if case is None or case["status"] != "open":
                raise DurableConflictEscalationEvidenceError("open durable Conflict Case가 없습니다.")
            request = connection.execute(
                "SELECT revision,state_kind,state_json FROM question_requests WHERE request_id=? AND org_id=?",
                (case["request_id"], org_id),
            ).fetchone()
            if request is None or request["revision"] != case["awaiting_revision"] + 1 or request["state_kind"] != "awaiting_conflict":
                raise DurableConflictEscalationEvidenceError("AwaitingConflict Question Request가 없습니다.")
            try:
                state = json.loads(request["state_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise DurableConflictEscalationEvidenceError("AwaitingConflict state가 canonical하지 않습니다.") from error
            if not isinstance(state, dict):
                raise DurableConflictEscalationEvidenceError("AwaitingConflict Case lineage가 다릅니다.")
            state_object = cast(dict[str, object], state)
            if state_object.get("case_id") != conflict_id:
                raise DurableConflictEscalationEvidenceError("AwaitingConflict Case lineage가 다릅니다.")
            baseline = connection.execute(
                "SELECT baseline_id,candidate_set_sha256,candidate_count,baseline_sha256 FROM durable_conflict_escalation_baselines WHERE conflict_id=? AND org_id=? AND request_id=? AND awaiting_revision=?",
                (conflict_id, org_id, case["request_id"], case["awaiting_revision"]),
            ).fetchone()
            ingress = connection.execute(
                "SELECT candidate_claim_sha256,candidate_snapshot_sha256 FROM durable_conflict_open_under_claim_evidence WHERE conflict_id=?",
                (conflict_id,),
            ).fetchone()
            if baseline is None or ingress is None or baseline["candidate_set_sha256"] != case["candidate_set_sha256"] or ingress["candidate_snapshot_sha256"] != case["candidate_set_sha256"]:
                raise DurableConflictEscalationEvidenceError("Conflict baseline/ingress snapshot lineage가 다릅니다.")
            if ingress["candidate_claim_sha256"] != conflict_open_claim_digest(canonical_claims):
                raise DurableConflictEscalationEvidenceError("Conflict ingress claim digest가 다릅니다.")
            self._claims_match_baseline(connection, baseline["baseline_id"], canonical_claims)
            try:
                current = self.registry_snapshot_reader.snapshot(org_id=org_id, claims=canonical_claims)
            except ConflictOpenContractError as error:
                raise DurableConflictEscalationEvidenceError("current Conflict Registry snapshot이 없습니다.") from error
            if current.claim_digest != ingress["candidate_claim_sha256"]:
                raise DurableConflictEscalationEvidenceError("current Registry claim evidence가 다릅니다.")
            if current.candidate_digest != case["candidate_set_sha256"]:
                return CandidateRegistryChanged(
                    org_id, conflict_id, case["request_id"], case["awaiting_revision"], case["awaiting_revision"] + 1,
                    case["candidate_set_sha256"], current.candidate_digest, baseline["baseline_sha256"],
                    ingress["candidate_claim_sha256"], _empty_vote_set_sha256(), self._evaluated_at(),
                )
            return self._votes(
                connection,
                case=case,
                baseline_id=baseline["baseline_id"],
                candidate_owner_count=baseline["candidate_count"],
                baseline_sha256=baseline["baseline_sha256"],
                candidate_claim_sha256=ingress["candidate_claim_sha256"],
                evaluated_at=self._evaluated_at(),
            )
        except (SqliteDurableConflictEscalationBaselineSchemaError, DurableConflictOpenIngressError, SqliteDurableDirectConflictUowSchemaError) as error:
            raise DurableConflictEscalationEvidenceError("durable Conflict evidence capability가 검증되지 않았습니다.") from error
        finally:
            connection.close()

    def _open(self) -> sqlite3.Connection:
        try:
            path = Path(self.db_path).expanduser().resolve(strict=True)
            return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0)
        except (OSError, sqlite3.Error) as error:
            raise DurableConflictEscalationEvidenceError("durable Conflict evidence DB를 읽을 수 없습니다.") from error

    def _evaluated_at(self) -> str:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise DurableConflictEscalationEvidenceError("evidence clock은 timezone-aware datetime이어야 합니다.")
        return value.isoformat(timespec="microseconds" if value.microsecond else "seconds")

    @staticmethod
    def _validate_scoped(connection: sqlite3.Connection, *, org_id: str) -> None:
        # Catalog/FK validation is global; row reconciliation is organization scoped.
        validate_sqlite_durable_conflict_escalation_baseline_connection(connection, org_id=org_id)
        validate_sqlite_durable_conflict_open_ingress_connection(connection, org_id=org_id)
        validate_sqlite_durable_direct_conflict_uow_connection(connection, org_id=org_id)

    @staticmethod
    def _claims_match_baseline(connection: sqlite3.Connection, baseline_id: str, claims: tuple[ConflictOpenCandidateClaim, ...]) -> None:
        candidates = connection.execute(
            "SELECT candidate_ordinal,candidate_card_ref,candidate_domain_ref,candidate_route_sha256 FROM durable_conflict_escalation_baseline_candidates WHERE baseline_id=? ORDER BY candidate_ordinal",
            (baseline_id,),
        ).fetchall()
        if len(candidates) != len(claims):
            raise DurableConflictEscalationEvidenceError("baseline candidate cardinality가 claim과 다릅니다.")
        for ordinal, (candidate, claim) in enumerate(zip(candidates, claims, strict=True), 1):
            if (
                candidate["candidate_ordinal"] != ordinal
                or candidate["candidate_card_ref"] != _ref("card", claim.card_id)
                or candidate["candidate_domain_ref"] != _ref("domain", claim.intent)
                or candidate["candidate_route_sha256"] != _route_sha(claim)
            ):
                raise DurableConflictEscalationEvidenceError("baseline candidate ordinal/card/domain/route가 claim과 다릅니다.")

    @staticmethod
    def _votes(
        connection: sqlite3.Connection,
        *,
        case: sqlite3.Row,
        baseline_id: str,
        candidate_owner_count: int,
        baseline_sha256: str,
        candidate_claim_sha256: str,
        evaluated_at: str,
    ) -> ConflictEscalationEvidence:
        rows = connection.execute(
            "SELECT owner_subject_ref,target_card_ref,concurrence_round,candidate_owner_count,candidate_set_sha256 FROM durable_direct_conflict_votes WHERE conflict_id=? AND org_id=? AND request_id=? ORDER BY concurrence_round,owner_subject_ref",
            (case["conflict_id"], case["org_id"], case["request_id"]),
        ).fetchall()
        round_ = case["awaiting_revision"] + 1
        if not rows:
            return Pending(case["org_id"], case["conflict_id"], case["request_id"], case["awaiting_revision"], round_, case["candidate_set_sha256"], baseline_sha256, candidate_claim_sha256, _empty_vote_set_sha256(), evaluated_at, 0, candidate_owner_count)
        rounds = {row["concurrence_round"] for row in rows}
        if rounds != {round_} or any(row["candidate_owner_count"] != candidate_owner_count or row["candidate_set_sha256"] != case["candidate_set_sha256"] for row in rows):
            raise DurableConflictEscalationEvidenceError("direct Conflict vote round snapshot이 Case와 다릅니다.")
        owners = [row["owner_subject_ref"] for row in rows]
        if len(owners) != len(set(owners)) or len(rows) > candidate_owner_count:
            raise DurableConflictEscalationEvidenceError("direct Conflict vote Owner cardinality가 올바르지 않습니다.")
        targets = [row["target_card_ref"] for row in rows]
        vote_set_sha256 = _vote_set_sha256(rows)
        candidates = connection.execute(
            "SELECT candidate_owner_subject_ref,candidate_card_ref FROM durable_conflict_escalation_baseline_candidates WHERE baseline_id=?",
            (baseline_id,),
        ).fetchall()
        expected_owners = {candidate["candidate_owner_subject_ref"] for candidate in candidates}
        expected_targets = {candidate["candidate_card_ref"] for candidate in candidates}
        if len(expected_owners) != candidate_owner_count or set(owners) - expected_owners or set(targets) - expected_targets:
            raise DurableConflictEscalationEvidenceError("direct Conflict vote Owner/target가 baseline candidate와 다릅니다.")
        if len(rows) < candidate_owner_count:
            return Pending(case["org_id"], case["conflict_id"], case["request_id"], case["awaiting_revision"], round_, case["candidate_set_sha256"], baseline_sha256, candidate_claim_sha256, vote_set_sha256, evaluated_at, len(rows), candidate_owner_count)
        if len(set(targets)) == candidate_owner_count:
            return DivergentVotes(case["org_id"], case["conflict_id"], case["request_id"], case["awaiting_revision"], round_, case["candidate_set_sha256"], candidate_owner_count, baseline_sha256, candidate_claim_sha256, vote_set_sha256, evaluated_at)
        # Direct consensus must already resolve the Case; an open unanimous or
        # partial-agreement full round is not an escalation signal.
        raise DurableConflictEscalationEvidenceError("open Conflict의 full vote outcome이 안전하게 해석되지 않습니다.")


def _empty_vote_set_sha256() -> str:
    return _sha(_canonical({"votes": []}))


def _vote_set_sha256(rows: list[sqlite3.Row]) -> str:
    return _sha(_canonical({"votes": [{"owner_subject_ref": row["owner_subject_ref"], "target_card_ref": row["target_card_ref"]} for row in rows]}))
