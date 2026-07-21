"""P18 S1b.1 atomic registration of a revision and its first review cycle."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from agent_org_network.reciprocal_review import HumanPrincipal, RegisterArtifactRevision, RegisteredArtifactRevision
from agent_org_network.sqlite_durable_reciprocal_review import (
    validate_sqlite_durable_reciprocal_review_ledger,
)


_REVISION = "durable_reciprocal_review_artifact_revisions"
_PROVENANCE = "durable_reciprocal_review_provenance_events"
_CYCLE = "durable_reciprocal_review_cycles"
_REQUIREMENT = "durable_reciprocal_review_requirements"
_AUDIT = "durable_reciprocal_review_audit_events"
_OUTBOX = "durable_reciprocal_review_outbox_intents"
_RECEIPT = "durable_reciprocal_review_command_receipts"
_LINEAGE = "durable_reciprocal_review_lineage_members"
_CLASSIFICATION: Final = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


class SqliteReciprocalReviewRegistrationError(RuntimeError):
    """Registration was not safe to record."""


class SqliteReciprocalReviewConflict(SqliteReciprocalReviewRegistrationError):
    """An idempotency key was already used for a different semantic command."""


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    """Verifier-derived immutable content and conservative boundary snapshot."""

    content_sha256: str
    source_boundary_digest: str
    data_classification: str = "internal"
    data_boundary_snapshot_ref: str = "boundary"
    preserves_parent_boundary: bool = True


@dataclass(frozen=True, slots=True)
class InitialReviewRequirement:
    reviewer_kind: str
    completion_rule: str
    required_count: int
    rubric_version: str
    deadline_at: datetime
    waivable: bool
    reviewer_subject_ids: tuple[str, ...] = ()
    independence_rule: str = "independent"
    risk_class: str = "standard"
    reviewer_assignment_digest: str | None = None


@dataclass(frozen=True, slots=True)
class InitialReviewPolicy:
    policy_digest: str
    requirements: tuple[InitialReviewRequirement, ...]


class ArtifactContentVerifier(Protocol):
    def verify(self, *, org_id: str, content_ref: str, content_sha256: str) -> ArtifactContent: ...


class ReviewPolicyRegistry(Protocol):
    def initial_policy(self, *, org_id: str, kind: str, effective_provenance_kind: str) -> InitialReviewPolicy: ...


class ReviewerAuthorization(Protocol):
    def authorize_registration(self, *, principal: HumanPrincipal, artifact_id: str) -> bool: ...

    def authorize_reviewer(self, *, principal: HumanPrincipal, contributor_subject_ids: tuple[str, ...]) -> bool: ...


class DbTime(Protocol):
    def __call__(self) -> datetime: ...


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value) or value.microsecond % 1000:
        raise SqliteReciprocalReviewRegistrationError("DB time은 canonical UTC milliseconds여야 합니다.")
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _require_digest(value: str, name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise SqliteReciprocalReviewRegistrationError(f"{name}는 SHA-256이어야 합니다.")


class _Capability:
    pass


_CAPABILITY = _Capability()


class SqliteReciprocalReviewUnitOfWork:
    """Sealed capability: construct only through ``create_sqlite_reciprocal_review_uow``."""

    def __init__(
        self, path: str | Path, *, content_verifier: ArtifactContentVerifier,
        review_policy_registry: ReviewPolicyRegistry, reviewer_authorization: ReviewerAuthorization,
        clock: DbTime, fault_injector: Callable[[str], None] | None,
        transaction_fault_injector: Callable[[sqlite3.Connection, str], None] | None,
        _capability: _Capability,
    ) -> None:
        if _capability is not _CAPABILITY:
            raise TypeError("Use create_sqlite_reciprocal_review_uow().")
        self._path = Path(path)
        self._content_verifier = content_verifier
        self._review_policy_registry = review_policy_registry
        self._reviewer_authorization = reviewer_authorization
        self._clock = clock
        self._fault_injector = fault_injector
        self._transaction_fault_injector = transaction_fault_injector

    def register(self, principal: HumanPrincipal, command: RegisterArtifactRevision) -> RegisteredArtifactRevision:
        if principal.org_id == "":  # pydantic invariant; keeps pyright narrowing honest.
            raise SqliteReciprocalReviewRegistrationError("조직이 없습니다.")
        content, policy = self._prewrite(principal, command)
        semantic = self._semantic(principal, command, content, policy)
        command_digest = _digest(semantic)
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            validate_sqlite_durable_reciprocal_review_ledger(connection)
            existing = connection.execute(
                f"SELECT command_digest,cycle_id FROM {_RECEIPT} WHERE org_id=? AND receipt_id=?",
                (principal.org_id, command.receipt_id),
            ).fetchone()
            if existing is not None:
                if existing[0] != command_digest:
                    raise SqliteReciprocalReviewConflict("receipt의 semantic command가 다릅니다.")
                result = RegisteredArtifactRevision(org_id=principal.org_id, revision_id=command.revision_id, cycle_id=existing[1], receipt_id=command.receipt_id, command_digest=command_digest)
                validate_sqlite_durable_reciprocal_review_ledger(connection)
                connection.commit()
                return result
            # Re-read all policy-bearing inputs immediately before the first write.
            if self._prewrite(principal, command) != (content, policy):
                raise SqliteReciprocalReviewRegistrationError("provenance/content/policy authorization drift가 있습니다.")
            self._insert_all(connection, principal, command, content, policy, command_digest)
            self._transaction_fault(connection, "before_commit_validation")
            validate_sqlite_durable_reciprocal_review_ledger(connection)
            connection.commit()
            return RegisteredArtifactRevision(org_id=principal.org_id, revision_id=command.revision_id, cycle_id=self._cycle_id(command), receipt_id=command.receipt_id, command_digest=command_digest)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _prewrite(self, principal: HumanPrincipal, command: RegisterArtifactRevision) -> tuple[ArtifactContent, InitialReviewPolicy]:
        if not self._reviewer_authorization.authorize_registration(principal=principal, artifact_id=command.artifact_id):
            raise SqliteReciprocalReviewRegistrationError("등록 권한이 없습니다.")
        content = self._content_verifier.verify(org_id=principal.org_id, content_ref=command.content_ref, content_sha256=command.content_sha256)
        _require_digest(content.content_sha256, "verified content digest")
        _require_digest(content.source_boundary_digest, "boundary digest")
        if content.content_sha256 != command.content_sha256 or content.data_classification not in _CLASSIFICATION:
            raise SqliteReciprocalReviewRegistrationError("content immutability 또는 boundary가 검증되지 않았습니다.")
        policy = self._review_policy_registry.initial_policy(org_id=principal.org_id, kind=command.kind, effective_provenance_kind=self._parent_provenance_kind(principal.org_id, command))
        _require_digest(policy.policy_digest, "policy digest")
        self._validate_requirements(principal, policy)
        return content, policy

    def _validate_requirements(self, principal: HumanPrincipal, policy: InitialReviewPolicy) -> None:
        requirements = policy.requirements
        if not requirements or any(r.reviewer_kind not in {"ai", "human"} or r.completion_rule not in {"all", "any", "quorum"} or r.required_count < 1 or not r.independence_rule or not r.risk_class for r in requirements):
            raise SqliteReciprocalReviewRegistrationError("초기 review requirement가 유효하지 않습니다.")
        # A human-authored revision must receive independent AI advice; human review is a policy-selected cross-review lane.
        if not any(r.reviewer_kind == "ai" for r in requirements):
            raise SqliteReciprocalReviewRegistrationError("사람 산출물에는 AI cross-review requirement가 필요합니다.")
        assigned_humans: set[str] = set()
        for requirement in requirements:
            if requirement.reviewer_kind == "human":
                if not requirement.reviewer_subject_ids or len(set(requirement.reviewer_subject_ids)) != len(requirement.reviewer_subject_ids):
                    raise SqliteReciprocalReviewRegistrationError("사람 reviewer는 명시적이고 중복되지 않아야 합니다.")
                candidate_count = len(requirement.reviewer_subject_ids)
                if (
                    (requirement.completion_rule == "all" and requirement.required_count != candidate_count)
                    or (requirement.completion_rule == "any" and requirement.required_count != 1)
                    or (
                        requirement.completion_rule == "quorum"
                        and not 1 <= requirement.required_count <= candidate_count
                    )
                ):
                    raise SqliteReciprocalReviewRegistrationError(
                        "사람 completion policy는 explicit assignment count와 일치해야 합니다."
                    )
                for subject_id in requirement.reviewer_subject_ids:
                    if subject_id in assigned_humans:
                        raise SqliteReciprocalReviewRegistrationError(
                            "사람 reviewer는 한 cycle의 서로 다른 requirement에 재사용될 수 없습니다."
                        )
                    assigned_humans.add(subject_id)
                    reviewer = principal.model_copy(update={"subject_id": subject_id})
                    if subject_id == principal.subject_id or not self._reviewer_authorization.authorize_reviewer(principal=reviewer, contributor_subject_ids=(principal.subject_id,)):
                        raise SqliteReciprocalReviewRegistrationError("contributor는 자신의 human reviewer가 될 수 없습니다.")

    @staticmethod
    def _cycle_id(command: RegisterArtifactRevision) -> str:
        return f"cycle:{command.revision_id}"

    def _parent_provenance_kind(self, org_id: str, command: RegisterArtifactRevision) -> str:
        if command.parent_revision_id is None:
            return "human"
        connection = sqlite3.connect(self._path)
        try:
            row = connection.execute(f"SELECT provenance_kind FROM {_REVISION} WHERE org_id=? AND revision_id=? AND artifact_id=?", (org_id, command.parent_revision_id, command.artifact_id)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise SqliteReciprocalReviewRegistrationError("parent revision이 없습니다.")
        return "unknown" if row[0] == "unknown" else "mixed" if row[0] == "ai" else row[0]

    @staticmethod
    def _assignment_digest(requirement: InitialReviewRequirement) -> str:
        return requirement.reviewer_assignment_digest or _digest({"reviewer_kind": requirement.reviewer_kind, "reviewers": requirement.reviewer_subject_ids})

    def _semantic(self, principal: HumanPrincipal, command: RegisterArtifactRevision, content: ArtifactContent, policy: InitialReviewPolicy) -> dict[str, object]:
        return {"principal": principal.model_dump(mode="json"), "command": command.model_dump(mode="json"), "content": {"content_sha256": content.content_sha256, "boundary": content.source_boundary_digest, "classification": content.data_classification, "snapshot": content.data_boundary_snapshot_ref}, "policy": {"digest": policy.policy_digest, "requirements": [{"kind": r.reviewer_kind, "rule": r.completion_rule, "count": r.required_count, "independence": r.independence_rule, "rubric": r.rubric_version, "risk": r.risk_class, "assignment": self._assignment_digest(r), "deadline": _time(r.deadline_at), "waivable": r.waivable} for r in policy.requirements]}}

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    def _transaction_fault(self, connection: sqlite3.Connection, point: str) -> None:
        if self._transaction_fault_injector is not None:
            self._transaction_fault_injector(connection, point)

    def _insert_all(self, connection: sqlite3.Connection, principal: HumanPrincipal, command: RegisterArtifactRevision, content: ArtifactContent, policy: InitialReviewPolicy, command_digest: str) -> None:
        parent = None
        inherited_events: tuple[str, ...] = ()
        inherited_kinds: tuple[str, ...] = ()
        if command.parent_revision_id is not None:
            parent = connection.execute(f"SELECT artifact_id,revision_no,data_classification,boundary_digest,provenance_kind FROM {_REVISION} WHERE org_id=? AND revision_id=?", (principal.org_id, command.parent_revision_id)).fetchone()
            if parent is None or parent[0] != command.artifact_id or not content.preserves_parent_boundary or _CLASSIFICATION[content.data_classification] < _CLASSIFICATION[parent[2]]:
                raise SqliteReciprocalReviewRegistrationError("parent보다 느슨한 boundary 또는 잘못된 parent입니다.")
            inherited_events, inherited_kinds = self._verified_parent_closure(
                connection, principal.org_id, command.artifact_id, command.parent_revision_id
            )
        revision_no = 1 if parent is None else parent[1] + 1
        now = _time(self._clock())
        lineage = tuple(sorted(set((*inherited_events, command.provenance_event_id))))
        provenance_kind = self._provenance_kind((*inherited_kinds, "human"))
        lineage_digest = _digest(lineage)
        provenance_digest = _digest({"kind": provenance_kind, "principal": principal.model_dump(mode="json"), "content": command.content_sha256, "lineage": lineage})
        cycle_id = self._cycle_id(command)
        try:
            connection.execute(f"INSERT INTO {_REVISION} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (principal.org_id, command.artifact_id, command.revision_id, revision_no, command.parent_revision_id, command.content_ref, command.content_sha256, content.data_classification, content.data_boundary_snapshot_ref, provenance_kind, provenance_digest, lineage_digest, content.source_boundary_digest, None, 1, now))
        except sqlite3.IntegrityError as error:
            raise SqliteReciprocalReviewConflict("revision이 이미 존재하거나 lineage가 충돌합니다.") from error
        self._fault("after_revision")
        connection.execute(f"INSERT INTO {_PROVENANCE} VALUES(?,?,?,?,?,?,?,?)", (principal.org_id, command.provenance_event_id, command.revision_id, "human", principal.subject_id, command.content_sha256, None, now))
        self._fault("after_provenance")
        for ordinal, event_id in enumerate(lineage, start=1):
            connection.execute(f"INSERT INTO {_LINEAGE} VALUES(?,?,?,?)", (principal.org_id, command.revision_id, event_id, ordinal))
        self._fault("after_lineage")
        connection.execute(f"INSERT INTO {_CYCLE} VALUES(?,?,?,?,?,?,?,?,?)", (principal.org_id, cycle_id, command.revision_id, 1, "review_open", 1, provenance_digest, policy.policy_digest, now))
        # ADR 0055 cutover is explicit: when v3 is installed every newly registered
        # cycle receives its mirror in this same registration transaction.
        from agent_org_network.sqlite_reciprocal_review_human_terminal import (
            provision_sqlite_reciprocal_review_v3_cycle,
        )

        provision_sqlite_reciprocal_review_v3_cycle(
            connection, org_id=principal.org_id, cycle_id=cycle_id
        )
        # ADR 0056 is additive.  When v4 is installed, registration is the only
        # place allowed to create its mirror, explicit reviewer plan and first
        # assignment-scoped runs; legacy cycles are never inferred/backfilled.
        from agent_org_network.sqlite_reciprocal_review_assignment_terminal import (
            provision_sqlite_reciprocal_review_v4_cycle,
        )

        v4_assignments = tuple(
            (
                f"requirement:{command.revision_id}:{number}",
                requirement.reviewer_subject_ids,
                _digest({"rubric_version": requirement.rubric_version}),
                _digest(
                    {
                        "content": content.content_sha256,
                        "rubric_version": requirement.rubric_version,
                        "requirement": number,
                    }
                ),
                content.content_sha256,
            )
            for number, requirement in enumerate(policy.requirements, start=1)
            if requirement.reviewer_kind == "human"
        )
        self._fault("after_cycle")
        for number, requirement in enumerate(policy.requirements, start=1):
            connection.execute(f"INSERT INTO {_REQUIREMENT} VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (principal.org_id, f"requirement:{command.revision_id}:{number}", cycle_id, requirement.reviewer_kind, requirement.completion_rule, requirement.required_count, requirement.independence_rule, requirement.rubric_version, requirement.risk_class, self._assignment_digest(requirement), _time(requirement.deadline_at), int(requirement.waivable)))
            self._fault("after_requirement")
        # Requirement rows must exist before v4 validates the explicit plan.
        # A v4-enabled registration without human requirements remains a v3
        # cycle: v4 only owns cycles that have an assignment-evidence lane.
        if v4_assignments and provenance_kind in {"ai", "mixed"}:
            provision_sqlite_reciprocal_review_v4_cycle(
                connection,
                org_id=principal.org_id,
                cycle_id=cycle_id,
                assignments=v4_assignments,
            )
            self._fault("after_v4_provision")
        connection.execute(f"INSERT INTO {_AUDIT} VALUES(?,?,?,?,?)", (principal.org_id, command.audit_id, cycle_id, hashlib.sha256(b"audit").hexdigest(), now))
        self._fault("after_audit")
        connection.execute(f"INSERT INTO {_OUTBOX} VALUES(?,?,?,?,?)", (principal.org_id, command.outbox_id, cycle_id, hashlib.sha256(b"outbox").hexdigest(), now))
        self._fault("after_outbox")
        connection.execute(f"INSERT INTO {_RECEIPT} VALUES(?,?,?,?,?)", (principal.org_id, command.receipt_id, command_digest, cycle_id, now))
        self._fault("after_receipt")

    @staticmethod
    def _provenance_kind(principal_kinds: tuple[str, ...]) -> str:
        if "imported_unknown" in principal_kinds:
            return "unknown"
        has_human = "human" in principal_kinds
        has_ai = any(kind in {"model_execution", "deterministic_transform"} for kind in principal_kinds)
        if has_human and has_ai:
            return "mixed"
        if has_ai:
            return "ai"
        return "human"

    def _verified_parent_closure(self, connection: sqlite3.Connection, org_id: str, artifact_id: str, parent_revision_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Rebuild every stored ancestor closure from its direct authenticated events."""
        chain: list[tuple[str, str | None, str, str]] = []
        seen: set[str] = set()
        revision_id: str | None = parent_revision_id
        while revision_id is not None:
            if revision_id in seen:
                raise SqliteReciprocalReviewRegistrationError("parent lineage cycle이 있습니다.")
            seen.add(revision_id)
            row = connection.execute(
                f"SELECT artifact_id,parent_revision_id,provenance_kind,lineage_digest FROM {_REVISION} WHERE org_id=? AND revision_id=?",
                (org_id, revision_id),
            ).fetchone()
            if row is None or row[0] != artifact_id:
                raise SqliteReciprocalReviewRegistrationError("ancestor artifact/org가 일치하지 않습니다.")
            chain.append((revision_id, row[1], row[2], row[3]))
            revision_id = row[1]
        inherited: tuple[str, ...] = ()
        inherited_kinds: tuple[str, ...] = ()
        for revision_id, _parent_id, stored_kind, stored_digest in reversed(chain):
            sources = connection.execute(
                f"SELECT event_id,principal_kind FROM {_PROVENANCE} WHERE org_id=? AND revision_id=? ORDER BY event_id",
                (org_id, revision_id),
            ).fetchall()
            if not sources:
                raise SqliteReciprocalReviewRegistrationError("ancestor provenance가 없습니다.")
            closure = tuple(sorted(set((*inherited, *(source[0] for source in sources)))))
            members = connection.execute(
                f"SELECT event_id FROM {_LINEAGE} WHERE org_id=? AND revision_id=? ORDER BY ordinal",
                (org_id, revision_id),
            ).fetchall()
            member_ids = tuple(member[0] for member in members)
            if member_ids != closure or _digest(closure) != stored_digest:
                raise SqliteReciprocalReviewRegistrationError("ancestor lineage closure가 일치하지 않습니다.")
            event_kinds = tuple(source[1] for source in sources)
            actual_kind = self._provenance_kind((*inherited_kinds, *event_kinds))
            if stored_kind != actual_kind:
                raise SqliteReciprocalReviewRegistrationError("ancestor provenance kind가 일치하지 않습니다.")
            inherited, inherited_kinds = closure, (*inherited_kinds, *event_kinds)
        return inherited, inherited_kinds


def create_sqlite_reciprocal_review_uow(path: str | Path, *, content_verifier: ArtifactContentVerifier, review_policy_registry: ReviewPolicyRegistry, reviewer_authorization: ReviewerAuthorization, clock: DbTime, fault_injector: Callable[[str], None] | None = None, transaction_fault_injector: Callable[[sqlite3.Connection, str], None] | None = None) -> SqliteReciprocalReviewUnitOfWork:
    """Create the only public capability for P18 S1b.1 registration."""
    return SqliteReciprocalReviewUnitOfWork(path, content_verifier=content_verifier, review_policy_registry=review_policy_registry, reviewer_authorization=reviewer_authorization, clock=clock, fault_injector=fault_injector, transaction_fault_injector=transaction_fault_injector, _capability=_CAPABILITY)
