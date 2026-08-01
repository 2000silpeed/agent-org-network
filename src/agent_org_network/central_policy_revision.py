"""Durable PolicyRevision foundation for the Central v20 cutover.

The component is intentionally independent from the legacy YAML authorizer until
the v20 marker migration wires it into Central composition.  It provides the
transactional core used by the future private policy API: strict document
validation, monotonic epochs, pointer CAS, idempotent receipts, and an
approval-evidence port that never accepts actor or digest values from headers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Literal, Protocol, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field

from agent_org_network.central_authority import (
    AuthorityPolicySnapshot,
    canonical_policy_digest,
    load_authority_policy_yaml,
)


_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
SCHEMA_VERSION = 20


class PolicyRevisionUnavailable(RuntimeError):
    """Policy catalog, validation, approval, or CAS dependency failed."""


class PolicyRevisionConflict(PolicyRevisionUnavailable):
    """Expected pointer, idempotency, or approval evidence no longer matches."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ApprovalReference(_Frozen):
    evidence_id: Annotated[str, Field(min_length=1, max_length=128)]
    evidence_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PolicyApprovalEvidence(_Frozen):
    evidence_id: Annotated[str, Field(min_length=1, max_length=128)]
    evidence_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    org_id: str
    actor_user_id: str
    action: Literal["policy.write"]
    resource_kind: Literal["authority_policy"]
    resource_id: str
    command_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    active_pointer_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expires_at: datetime | None = None
    revoked: bool = False
    consumed: bool = False


class PolicyApprovalPort(Protocol):
    def resolve_and_claim(
        self,
        *,
        org_id: str,
        actor_user_id: str,
        approval: ApprovalReference,
        command_digest: str,
        active_pointer_fingerprint: str,
    ) -> PolicyApprovalEvidence | None: ...


PolicyOperation: TypeAlias = Literal["activated", "rolled_back", "imported"]


class ActivatePolicy(_Frozen):
    kind: Literal["activate"] = "activate"
    expected_epoch: Annotated[int, Field(gt=0)]
    expected_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    document: dict[str, object]
    approval: ApprovalReference


class ImportPolicy(_Frozen):
    kind: Literal["import"] = "import"
    expected_epoch: Annotated[int, Field(gt=0)]
    expected_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    document: dict[str, object]
    approval: ApprovalReference


class RollbackPolicy(_Frozen):
    kind: Literal["rollback"] = "rollback"
    expected_epoch: Annotated[int, Field(gt=0)]
    expected_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    target_revision_id: str
    target_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    approval: ApprovalReference


PolicyCommand: TypeAlias = ActivatePolicy | ImportPolicy | RollbackPolicy


class PolicyRevisionView(_Frozen):
    revision_id: str
    org_id: str
    epoch: Annotated[int, Field(gt=0)]
    policy_version: str
    policy_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    canonical_document: dict[str, object]
    activated_at: datetime


class PolicyRevisionReceipt(_Frozen):
    receipt_id: str
    operation: PolicyOperation
    revision_id: str
    epoch: Annotated[int, Field(gt=0)]
    policy_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    previous_revision_id: str
    previous_epoch: Annotated[int, Field(gt=0)]
    previous_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    evidence_id: str
    evidence_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    replayed: bool


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    offset = current.utcoffset()
    if current.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise PolicyRevisionUnavailable("UTC clock required")
    return current.isoformat().replace("+00:00", "Z")


def _document_snapshot(document: Mapping[str, object], org_id: str) -> AuthorityPolicySnapshot:
    raw = dict(document)
    declared = raw.get("content_sha256")
    raw.pop("content_sha256", None)
    try:
        digest_input = dict(raw)
        digest_input["content_sha256"] = "pending"
        computed = canonical_policy_digest(digest_input)
    except Exception as error:
        raise PolicyRevisionUnavailable("invalid policy document") from error
    if declared is not None and declared != computed:
        raise PolicyRevisionUnavailable("policy digest mismatch")
    raw["content_sha256"] = computed
    # The existing strict loader is the single document validator.  YAML is
    # only an adapter here; the stored value is the canonical JSON snapshot.
    try:
        import yaml

        snapshot = load_authority_policy_yaml(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
            expected_org_id=org_id,
        )
    except Exception as error:
        raise PolicyRevisionUnavailable("invalid policy document") from error
    return snapshot


def _command_digest(command: PolicyCommand, actor_user_id: str, org_id: str) -> str:
    payload: dict[str, object] = {
        "actor_user_id": actor_user_id,
        "org_id": org_id,
        "action": "policy.write",
        "resource": {"kind": "authority_policy", "resource_id": org_id},
        "kind": command.kind,
        "expected_epoch": command.expected_epoch,
        "expected_digest": command.expected_digest,
    }
    if isinstance(command, (ActivatePolicy, ImportPolicy)):
        payload["document"] = command.document
    else:
        payload["target_revision_id"] = command.target_revision_id
        payload["target_digest"] = command.target_digest
    return _digest(payload)


def _pointer_fingerprint(org_id: str, revision_id: str, epoch: int, digest: str) -> str:
    return _digest({"org_id": org_id, "revision_id": revision_id, "epoch": epoch, "policy_digest": digest})


_DDL = (
    "CREATE TABLE IF NOT EXISTS central_policy_component_schema "
    "(name TEXT PRIMARY KEY NOT NULL CHECK(name='central-policy'), version INTEGER NOT NULL CHECK(version=20))",
    "CREATE TABLE IF NOT EXISTS central_policy_revisions ("
    "revision_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, epoch INTEGER NOT NULL,"
    "policy_version TEXT NOT NULL, policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),"
    "parent_revision_id TEXT, parent_epoch INTEGER, parent_digest TEXT, canonical_document TEXT NOT NULL,"
    "validation_receipt_id TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,"
    # A digest may be activated again by rollback.  Epoch/revision identity,
    # not the document digest, is the immutable uniqueness boundary (ABA-safe).
    "UNIQUE(org_id,epoch))",
    "CREATE TABLE IF NOT EXISTS central_active_policy_pointers ("
    "org_id TEXT PRIMARY KEY NOT NULL, revision_id TEXT NOT NULL, epoch INTEGER NOT NULL,"
    "policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64), activated_at TEXT NOT NULL,"
    "FOREIGN KEY(revision_id) REFERENCES central_policy_revisions(revision_id))",
    "CREATE TABLE IF NOT EXISTS central_policy_change_receipts ("
    "receipt_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,"
    "command_digest TEXT NOT NULL CHECK(length(command_digest)=64), operation TEXT NOT NULL,"
    "revision_id TEXT NOT NULL, epoch INTEGER NOT NULL, policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),"
    "previous_revision_id TEXT NOT NULL, previous_epoch INTEGER NOT NULL, previous_digest TEXT NOT NULL CHECK(length(previous_digest)=64),"
    "evidence_id TEXT NOT NULL, evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64), created_at TEXT NOT NULL,"
    "UNIQUE(org_id,idempotency_key), FOREIGN KEY(revision_id) REFERENCES central_policy_revisions(revision_id))",
    "CREATE TABLE IF NOT EXISTS central_policy_approval_evidence ("
    "evidence_id TEXT PRIMARY KEY NOT NULL, evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64),"
    "org_id TEXT NOT NULL, actor_user_id TEXT NOT NULL, action TEXT NOT NULL CHECK(action='policy.write'),"
    "resource_kind TEXT NOT NULL CHECK(resource_kind='authority_policy'), resource_id TEXT NOT NULL,"
    "command_digest TEXT NOT NULL CHECK(length(command_digest)=64),"
    "active_pointer_fingerprint TEXT NOT NULL CHECK(length(active_pointer_fingerprint)=64),"
    "expires_at TEXT, revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),"
    "consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0,1)), claimed_command_digest TEXT,"
    "UNIQUE(evidence_id,evidence_digest))",
    "CREATE TABLE IF NOT EXISTS central_policy_change_audits ("
    "receipt_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, actor_user_id TEXT NOT NULL,"
    "action TEXT NOT NULL CHECK(action='policy.write'), operation TEXT NOT NULL,"
    "command_digest TEXT NOT NULL CHECK(length(command_digest)=64), revision_id TEXT NOT NULL,"
    "epoch INTEGER NOT NULL CHECK(epoch>0), policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),"
    "evidence_id TEXT NOT NULL, evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64), created_at TEXT NOT NULL,"
    "FOREIGN KEY(revision_id) REFERENCES central_policy_revisions(revision_id))",
    "CREATE TABLE IF NOT EXISTS central_policy_change_outbox ("
    "intent_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, receipt_id TEXT NOT NULL,"
    "kind TEXT NOT NULL CHECK(kind IN ('policy_activated','policy_rolled_back','policy_imported')),"
    "command_digest TEXT NOT NULL CHECK(length(command_digest)=64), revision_id TEXT NOT NULL,"
    "epoch INTEGER NOT NULL CHECK(epoch>0), policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),"
    "delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1)),"
    "FOREIGN KEY(receipt_id) REFERENCES central_policy_change_receipts(receipt_id),"
    "FOREIGN KEY(revision_id) REFERENCES central_policy_revisions(revision_id), UNIQUE(receipt_id))",
    "CREATE TABLE IF NOT EXISTS central_policy_bootstrap_receipts ("
    "receipt_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, revision_id TEXT NOT NULL,"
    "validation_receipt_id TEXT NOT NULL, actor_user_id TEXT NOT NULL,"
    "policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64), created_at TEXT NOT NULL,"
    "FOREIGN KEY(revision_id) REFERENCES central_policy_revisions(revision_id), UNIQUE(org_id,revision_id))",
)


def migrate_central_policy_revision_schema(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in _DDL:
            connection.execute(statement)
        marker = connection.execute(
            "SELECT version FROM central_policy_component_schema WHERE name='central-policy'"
        ).fetchone()
        if marker is None:
            connection.execute(
                "INSERT INTO central_policy_component_schema(name,version) VALUES ('central-policy',20)"
            )
        elif marker[0] != SCHEMA_VERSION:
            raise PolicyRevisionUnavailable("unsupported policy marker")
        connection.commit()
    except PolicyRevisionUnavailable:
        connection.rollback()
        raise
    except Exception as error:
        connection.rollback()
        raise PolicyRevisionUnavailable("policy schema unavailable") from error
    finally:
        connection.close()


def policy_revision_schema_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            return connection.execute(
                "SELECT version FROM central_policy_component_schema WHERE name='central-policy'"
            ).fetchone() == (SCHEMA_VERSION,)
    except sqlite3.Error:
        return False


class PolicyRevisionApplication:
    def __init__(self, path: Path, approval: PolicyApprovalPort, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if not policy_revision_schema_ready(path):
            raise PolicyRevisionUnavailable("policy schema unavailable")
        self._path = path
        self._approval = approval
        self._clock = clock

    def active(self, org_id: str) -> PolicyRevisionView:
        with sqlite3.connect(self._path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT r.*,p.activated_at FROM central_active_policy_pointers p "
                "JOIN central_policy_revisions r ON r.revision_id=p.revision_id WHERE p.org_id=?",
                (org_id,),
            ).fetchone()
            if row is None:
                raise PolicyRevisionUnavailable("active policy unavailable")
            return _view(row)

    def bootstrap(self, *, org_id: str, actor_user_id: str, document: Mapping[str, object], validation_receipt_id: str = "bootstrap") -> PolicyRevisionView:
        """Create epoch 1 exactly once from the configured strict document."""
        snapshot = _document_snapshot(document, org_id)
        canonical = snapshot.model_dump(mode="json")
        now = _now(self._clock())
        digest = snapshot.content_sha256
        revision_id = _digest({"bootstrap": org_id, "digest": digest})
        with sqlite3.connect(self._path) as connection:
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT r.*,p.activated_at FROM central_active_policy_pointers p "
                    "JOIN central_policy_revisions r ON r.revision_id=p.revision_id WHERE p.org_id=?",
                    (org_id,),
                ).fetchone()
                if existing is not None:
                    if existing["policy_digest"] != digest:
                        raise PolicyRevisionConflict("bootstrap digest conflict")
                    connection.commit()
                    return _view(existing)
                connection.execute(
                    "INSERT INTO central_policy_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (revision_id, org_id, 1, snapshot.policy_version, digest, None, None, None,
                     _canonical(canonical), validation_receipt_id, actor_user_id, now),
                )
                connection.execute(
                    "INSERT INTO central_active_policy_pointers VALUES (?,?,?,?,?)",
                    (org_id, revision_id, 1, digest, now),
                )
                connection.execute(
                    "INSERT INTO central_policy_bootstrap_receipts VALUES (?,?,?,?,?,?,?)",
                    (_digest({"policy-bootstrap": org_id, "digest": digest}), org_id,
                     revision_id, validation_receipt_id, actor_user_id, digest, now),
                )
                connection.commit()
                return PolicyRevisionView(
                    revision_id=revision_id, org_id=org_id, epoch=1,
                    policy_version=snapshot.policy_version, policy_digest=digest,
                    canonical_document=canonical, activated_at=datetime.fromisoformat(now.replace("Z", "+00:00")),
                )
            except (PolicyRevisionConflict, PolicyRevisionUnavailable):
                connection.rollback()
                raise
            except Exception as error:
                connection.rollback()
                raise PolicyRevisionUnavailable("policy bootstrap unavailable") from error

    def apply(
        self, *, org_id: str, actor_user_id: str, command: PolicyCommand,
        idempotency_key: str,
    ) -> PolicyRevisionReceipt:
        if not _REF.fullmatch(org_id) or not _REF.fullmatch(actor_user_id):
            raise PolicyRevisionUnavailable("invalid policy actor")
        if not _REF.fullmatch(idempotency_key):
            raise PolicyRevisionUnavailable("invalid policy idempotency key")
        command_digest = _command_digest(command, actor_user_id, org_id)
        with sqlite3.connect(self._path) as connection:
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN IMMEDIATE")
                pointer = connection.execute(
                    "SELECT * FROM central_active_policy_pointers WHERE org_id=?", (org_id,)
                ).fetchone()
                if pointer is None:
                    raise PolicyRevisionUnavailable("active policy unavailable")
                existing = connection.execute(
                    "SELECT * FROM central_policy_change_receipts WHERE org_id=? AND idempotency_key=?",
                    (org_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["command_digest"] != command_digest:
                        raise PolicyRevisionConflict("idempotency conflict")
                    replay_fp = _pointer_fingerprint(
                        org_id, str(existing["previous_revision_id"]),
                        int(existing["previous_epoch"]), str(existing["previous_digest"]),
                    )
                    evidence = self._approval.resolve_and_claim(
                        org_id=org_id, actor_user_id=actor_user_id, approval=command.approval,
                        command_digest=command_digest, active_pointer_fingerprint=replay_fp,
                    )
                    if (
                        evidence is None
                        or evidence.evidence_id != command.approval.evidence_id
                        or evidence.evidence_digest != command.approval.evidence_digest
                        or evidence.org_id != org_id
                        or evidence.actor_user_id != actor_user_id
                        or evidence.action != "policy.write"
                        or evidence.resource_kind != "authority_policy"
                        or evidence.resource_id != org_id
                        or evidence.command_digest != command_digest
                        or evidence.active_pointer_fingerprint != replay_fp
                    ):
                        raise PolicyRevisionConflict("policy approval unavailable")
                    replay_now = _now(self._clock())
                    if evidence.expires_at is not None and evidence.expires_at <= datetime.fromisoformat(
                        replay_now.replace("Z", "+00:00")
                    ):
                        raise PolicyRevisionConflict("policy approval expired")
                    if evidence.revoked or evidence.consumed:
                        raise PolicyRevisionConflict("policy approval unavailable")
                    connection.rollback()
                    return _receipt(existing, replayed=True)
                if int(pointer["epoch"]) != command.expected_epoch or pointer["policy_digest"] != command.expected_digest:
                    raise PolicyRevisionConflict("stale active policy pointer")
                previous_fp = _pointer_fingerprint(org_id, str(pointer["revision_id"]), int(pointer["epoch"]), str(pointer["policy_digest"]))
                evidence = self._approval.resolve_and_claim(
                    org_id=org_id, actor_user_id=actor_user_id, approval=command.approval,
                    command_digest=command_digest, active_pointer_fingerprint=previous_fp,
                )
                if (
                    evidence is None
                    or evidence.evidence_id != command.approval.evidence_id
                    or evidence.evidence_digest != command.approval.evidence_digest
                    or evidence.org_id != org_id
                    or evidence.actor_user_id != actor_user_id
                    or evidence.action != "policy.write"
                    or evidence.resource_kind != "authority_policy"
                    or evidence.resource_id != org_id
                    or evidence.command_digest != command_digest
                    or evidence.active_pointer_fingerprint != previous_fp
                ):
                    raise PolicyRevisionConflict("policy approval unavailable")
                now = _now(self._clock())
                if evidence.expires_at is not None and evidence.expires_at <= datetime.fromisoformat(now.replace("Z", "+00:00")):
                    raise PolicyRevisionConflict("policy approval expired")
                if evidence.revoked or evidence.consumed:
                    raise PolicyRevisionConflict("policy approval unavailable")
                if isinstance(command, RollbackPolicy):
                    target = connection.execute(
                        "SELECT * FROM central_policy_revisions WHERE org_id=? AND revision_id=? AND policy_digest=?",
                        (org_id, command.target_revision_id, command.target_digest),
                    ).fetchone()
                    if target is None:
                        raise PolicyRevisionConflict("rollback target unavailable")
                    snapshot_document = json.loads(str(target["canonical_document"]))
                else:
                    snapshot_document = command.document
                snapshot = _document_snapshot(cast(Mapping[str, object], snapshot_document), org_id)
                epoch = int(pointer["epoch"]) + 1
                revision_id = _digest({"org_id": org_id, "epoch": epoch, "command_digest": command_digest})
                operation: PolicyOperation = {
                    "activate": "activated", "import": "imported", "rollback": "rolled_back",
                }[command.kind]  # type: ignore[index]
                created_at = now
                connection.execute(
                    "INSERT INTO central_policy_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (revision_id, org_id, epoch, snapshot.policy_version, snapshot.content_sha256,
                     pointer["revision_id"], pointer["epoch"], pointer["policy_digest"],
                     _canonical(snapshot.model_dump(mode="json")), command.approval.evidence_id, actor_user_id, created_at),
                )
                changed = connection.execute(
                    "UPDATE central_active_policy_pointers SET revision_id=?,epoch=?,policy_digest=?,activated_at=? "
                    "WHERE org_id=? AND revision_id=? AND epoch=? AND policy_digest=?",
                    (revision_id, epoch, snapshot.content_sha256, created_at, org_id,
                     pointer["revision_id"], pointer["epoch"], pointer["policy_digest"]),
                ).rowcount
                if changed != 1:
                    raise PolicyRevisionConflict("active policy CAS conflict")
                precommit = self._approval.resolve_and_claim(
                    org_id=org_id, actor_user_id=actor_user_id, approval=command.approval,
                    command_digest=command_digest, active_pointer_fingerprint=previous_fp,
                )
                if (
                    precommit is None
                    or precommit.evidence_id != evidence.evidence_id
                    or precommit.evidence_digest != evidence.evidence_digest
                    or precommit.org_id != org_id
                    or precommit.actor_user_id != actor_user_id
                    or precommit.command_digest != command_digest
                    or precommit.active_pointer_fingerprint != previous_fp
                    or precommit.revoked or precommit.consumed
                    or (
                        precommit.expires_at is not None
                        and precommit.expires_at <= datetime.fromisoformat(now.replace("Z", "+00:00"))
                    )
                ):
                    raise PolicyRevisionConflict("policy approval changed before commit")
                receipt_id = _digest({"policy-receipt": org_id, "idempotency_key": idempotency_key})
                connection.execute(
                    "INSERT INTO central_policy_change_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id, org_id, idempotency_key, command_digest, operation, revision_id,
                     epoch, snapshot.content_sha256, pointer["revision_id"], pointer["epoch"], pointer["policy_digest"],
                    evidence.evidence_id, evidence.evidence_digest, created_at),
                )
                connection.execute(
                    "INSERT INTO central_policy_change_audits VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id, org_id, actor_user_id, "policy.write", operation, command_digest,
                     revision_id, epoch, snapshot.content_sha256, evidence.evidence_id,
                     evidence.evidence_digest, created_at),
                )
                connection.execute(
                    "INSERT INTO central_policy_change_outbox VALUES (?,?,?,?,?,?,?,?,?)",
                    (_digest({"policy-outbox": receipt_id}), org_id, receipt_id,
                     f"policy_{operation}", command_digest, revision_id, epoch,
                     snapshot.content_sha256, 0),
                )
                connection.commit()
                return PolicyRevisionReceipt(
                    receipt_id=receipt_id, operation=operation, revision_id=revision_id,
                    epoch=epoch, policy_digest=snapshot.content_sha256,
                    previous_revision_id=str(pointer["revision_id"]), previous_epoch=int(pointer["epoch"]),
                    previous_digest=str(pointer["policy_digest"]), evidence_id=evidence.evidence_id,
                    evidence_digest=evidence.evidence_digest, replayed=False,
                )
            except (PolicyRevisionConflict, PolicyRevisionUnavailable):
                connection.rollback()
                raise
            except Exception as error:
                connection.rollback()
                raise PolicyRevisionUnavailable("policy revision unavailable") from error


class SqlitePolicyApprovalPort:
    """Durable approval evidence resolver used by the v20 composition seam.

    The port intentionally has no HTTP/header path.  An approval service writes
    the exact evidence row; this resolver only claims the same command binding
    and never accepts actor, org, or digest values from the caller as authority.
    """

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if not policy_revision_schema_ready(path):
            raise PolicyRevisionUnavailable("policy schema unavailable")
        self._path = path
        self._clock = clock

    def resolve_and_claim(
        self,
        *,
        org_id: str,
        actor_user_id: str,
        approval: ApprovalReference,
        command_digest: str,
        active_pointer_fingerprint: str,
    ) -> PolicyApprovalEvidence | None:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise PolicyRevisionUnavailable("UTC clock required")
        # ``PolicyRevisionApplication.apply`` already owns BEGIN IMMEDIATE.
        # This port is therefore a read-only exact resolver; the application
        # transaction is the claim boundary and its unique receipt binds the
        # evidence to one command.
        with sqlite3.connect(f"file:{self._path}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            try:
                row = connection.execute(
                    "SELECT * FROM central_policy_approval_evidence WHERE evidence_id=? AND evidence_digest=?",
                    (approval.evidence_id, approval.evidence_digest),
                ).fetchone()
                if row is None:
                    return None
                if (
                    row["org_id"] != org_id
                    or row["actor_user_id"] != actor_user_id
                    or row["action"] != "policy.write"
                    or row["resource_kind"] != "authority_policy"
                    or row["resource_id"] != org_id
                    or row["command_digest"] != command_digest
                    or row["active_pointer_fingerprint"] != active_pointer_fingerprint
                    or int(row["revoked"]) != 0
                    or int(row["consumed"]) != 0
                    or (
                        row["claimed_command_digest"] is not None
                        and row["claimed_command_digest"] != command_digest
                    )
                ):
                    return None
                expires_at = row["expires_at"]
                if expires_at is not None and datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")) <= now:
                    return None
                return PolicyApprovalEvidence(
                    evidence_id=str(row["evidence_id"]), evidence_digest=str(row["evidence_digest"]),
                    org_id=str(row["org_id"]), actor_user_id=str(row["actor_user_id"]), action="policy.write",
                    resource_kind="authority_policy", resource_id=str(row["resource_id"]),
                    command_digest=str(row["command_digest"]),
                    active_pointer_fingerprint=str(row["active_pointer_fingerprint"]),
                    expires_at=(
                        datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                        if expires_at is not None else None
                    ),
                    revoked=False, consumed=False,
                )
            except PolicyRevisionUnavailable:
                raise
            except Exception as error:
                raise PolicyRevisionUnavailable("policy approval unavailable") from error

    def issue(self, evidence: PolicyApprovalEvidence) -> None:
        """Persist an already-approved, exact evidence record for an external approver."""
        if type(evidence) is not PolicyApprovalEvidence:
            raise PolicyRevisionUnavailable("invalid policy approval")
        expires_at = evidence.expires_at.isoformat().replace("+00:00", "Z") if evidence.expires_at else None
        try:
            with sqlite3.connect(self._path) as connection:
                connection.execute(
                    "INSERT INTO central_policy_approval_evidence "
                    "(evidence_id,evidence_digest,org_id,actor_user_id,action,resource_kind,resource_id,"
                    "command_digest,active_pointer_fingerprint,expires_at,revoked,consumed,claimed_command_digest) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (evidence.evidence_id, evidence.evidence_digest, evidence.org_id, evidence.actor_user_id,
                     evidence.action, evidence.resource_kind, evidence.resource_id, evidence.command_digest,
                     evidence.active_pointer_fingerprint, expires_at, int(evidence.revoked), int(evidence.consumed)),
                )
        except sqlite3.IntegrityError as error:
            raise PolicyRevisionConflict("policy approval already exists") from error
        except sqlite3.Error as error:
            raise PolicyRevisionUnavailable("policy approval unavailable") from error


def _view(row: sqlite3.Row) -> PolicyRevisionView:
    return PolicyRevisionView(
        revision_id=str(row["revision_id"]), org_id=str(row["org_id"]), epoch=int(row["epoch"]),
        policy_version=str(row["policy_version"]), policy_digest=str(row["policy_digest"]),
        canonical_document=cast(dict[str, object], json.loads(str(row["canonical_document"]))),
        activated_at=datetime.fromisoformat(str(row["activated_at"]).replace("Z", "+00:00")),
    )


def _receipt(row: sqlite3.Row, *, replayed: bool) -> PolicyRevisionReceipt:
    return PolicyRevisionReceipt(
        receipt_id=str(row["receipt_id"]), operation=cast(PolicyOperation, row["operation"]),
        revision_id=str(row["revision_id"]), epoch=int(row["epoch"]), policy_digest=str(row["policy_digest"]),
        previous_revision_id=str(row["previous_revision_id"]), previous_epoch=int(row["previous_epoch"]),
        previous_digest=str(row["previous_digest"]), evidence_id=str(row["evidence_id"]),
        evidence_digest=str(row["evidence_digest"]), replayed=replayed,
    )
