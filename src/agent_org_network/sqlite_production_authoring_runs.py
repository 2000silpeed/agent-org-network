"""Body-free central AuthoringRun start UoW (P17.15 O3a.1)."""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from agent_org_network.knowledge_index import KnowledgeIndex
from agent_org_network.sqlite_production_agent_cards import (
    ProductionAgentCardUnavailable,
    validate_production_agent_card_connection,
)
from agent_org_network.production_authoring_identity import AuthoringInvocation
from agent_org_network.production_authoring_resource import (
    ProductionAuthoringResourceUnavailable,
    canonical_completion_resource_fingerprint,
    canonical_resource_fingerprint,
    canonical_source_set_digest,
    canonical_start_run_id,
    validate_authoring_catalog,
    validate_current_authoring_resource,
)


class ProductionAuthoringRunError(Exception):
    pass


class ProductionAuthoringRunUnavailable(ProductionAuthoringRunError):
    pass


class ProductionAuthoringRunDenied(ProductionAuthoringRunError):
    pass


class ProductionAuthoringRunConflict(ProductionAuthoringRunError):
    pass


_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_SOURCE_COUNT = 32
MAX_SOURCE_BYTES = 100 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 100 * 1024 * 1024


class AuthoringSourceRef(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_digest: str
    byte_size: int
    media_type: Literal["text/plain", "text/markdown"]

    @field_validator("source_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value

    @field_validator("byte_size")
    @classmethod
    def _size(cls, value: int) -> int:
        if not 1 <= value <= MAX_SOURCE_BYTES:
            raise ValueError("bounded positive byte size required")
        return value


class StartAuthoringRunCommand(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    org_id: str
    principal_id: str
    idempotency_key: str
    agent_id: str
    expected_card_revision: int
    expected_card_digest: str
    sources: tuple[AuthoringSourceRef, ...]

    @field_validator("org_id", "principal_id", "idempotency_key", "agent_id")
    @classmethod
    def _opaque(cls, value: str) -> str:
        if _OPAQUE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("expected_card_revision")
    @classmethod
    def _revision(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("positive card revision required")
        return value

    @field_validator("expected_card_digest")
    @classmethod
    def _card_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value

    @field_validator("sources")
    @classmethod
    def _sources(
        cls, value: tuple[AuthoringSourceRef, ...]
    ) -> tuple[AuthoringSourceRef, ...]:
        if not 1 <= len(value) <= MAX_SOURCE_COUNT:
            raise ValueError("bounded source count required")
        digests = tuple(source.source_digest for source in value)
        if digests != tuple(sorted(digests)) or len(set(digests)) != len(digests):
            raise ValueError("sources must be digest-sorted and unique")
        return value

    @model_validator(mode="after")
    def _total_source_bytes(self) -> "StartAuthoringRunCommand":
        if sum(source.byte_size for source in self.sources) > MAX_TOTAL_SOURCE_BYTES:
            raise ValueError("bounded total source bytes required")
        return self


class CompleteAuthoringRunCommand(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    organization_id: str
    principal_id: str
    idempotency_key: str
    run_id: str
    expected_revision: Literal[0] = 0
    expected_card_revision: int
    expected_card_digest: str
    admitted_bundle_digest: str
    document_count: int
    edge_count: int
    dropped_count: int
    author_profile_digest: str

    @field_validator(
        "organization_id", "principal_id", "idempotency_key", "run_id"
    )
    @classmethod
    def _opaque(cls, value: str) -> str:
        if _OPAQUE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("expected_card_revision")
    @classmethod
    def _card_revision(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("positive card revision required")
        return value

    @field_validator(
        "expected_card_digest", "admitted_bundle_digest", "author_profile_digest"
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value

    @field_validator("document_count", "edge_count", "dropped_count")
    @classmethod
    def _count(cls, value: int) -> int:
        if not 0 <= value <= 1_000_000:
            raise ValueError("bounded nonnegative count required")
        return value


class ReviewAuthoringRunCommand(BaseModel, frozen=True):
    """Body-free Owner review of the one admitted O3 bundle."""
    model_config = ConfigDict(extra="forbid", strict=True)

    organization_id: str
    principal_id: str
    idempotency_key: str
    run_id: str
    expected_revision: Literal[1] = 1
    expected_card_revision: int
    expected_card_digest: str
    concept_id: Literal["bundle"] = "bundle"
    source_digest: str
    draft_digest: str
    outcome: Literal["Approved", "Edited", "Rejected"]

    @field_validator("organization_id", "principal_id", "idempotency_key", "run_id")
    @classmethod
    def _opaque(cls, value: str) -> str:
        if _OPAQUE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("expected_card_revision")
    @classmethod
    def _card_revision(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("positive card revision required")
        return value

    @field_validator("expected_card_digest", "source_digest", "draft_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value


class BeginAuthoringRunPublishCommand(BaseModel, frozen=True):
    """Body-free claim of one exact approved Owner review."""
    model_config = ConfigDict(extra="forbid", strict=True)

    organization_id: str
    principal_id: str
    idempotency_key: str
    run_id: str
    expected_revision: Literal[2] = 2
    expected_card_revision: int
    expected_card_digest: str

    @field_validator("organization_id", "principal_id", "idempotency_key", "run_id")
    @classmethod
    def _opaque(cls, value: str) -> str:
        if _OPAQUE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("expected_card_revision")
    @classmethod
    def _card_revision(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("positive card revision required")
        return value

    @field_validator("expected_card_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value


class ExtractingRun(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    org_id: str
    run_id: str
    agent_id: str
    owner_id: str
    stage: Literal["Extracting"] = "Extracting"
    revision: Literal[0] = 0
    card_revision: int
    card_digest: str
    source_set_digest: str
    source_count: int
    total_bytes: int
    created_at: str


class AwaitingOwnerReviewRun(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    org_id: str
    run_id: str
    agent_id: str
    owner_id: str
    stage: Literal["AwaitingOwnerReview"] = "AwaitingOwnerReview"
    revision: Literal[1] = 1
    card_revision: int
    card_digest: str
    source_set_digest: str
    source_count: int
    total_bytes: int
    created_at: str
    admitted_bundle_digest: str
    document_count: int
    edge_count: int
    dropped_count: int
    author_profile_digest: str
    completed_at: str

    @field_validator(
        "card_digest",
        "source_set_digest",
        "admitted_bundle_digest",
        "author_profile_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value


class ReviewedRun(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    org_id: str
    run_id: str
    agent_id: str
    owner_id: str
    stage: Literal["Reviewed"] = "Reviewed"
    revision: Literal[2] = 2
    card_revision: int
    card_digest: str
    source_set_digest: str
    source_count: int
    total_bytes: int
    created_at: str
    admitted_bundle_digest: str
    document_count: int
    edge_count: int
    dropped_count: int
    author_profile_digest: str
    completed_at: str
    outcome: Literal["Approved", "Edited", "Rejected"]
    reviewed_at: str

    @field_validator("card_revision", "source_count", "total_bytes")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("positive value required")
        return value


class PublishingRun(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    org_id: str
    run_id: str
    agent_id: str
    owner_id: str
    stage: Literal["Publishing"] = "Publishing"
    revision: Literal[3] = 3
    card_revision: int
    card_digest: str
    source_set_digest: str
    source_count: int
    total_bytes: int
    created_at: str
    admitted_bundle_digest: str
    document_count: int
    edge_count: int
    dropped_count: int
    author_profile_digest: str
    completed_at: str
    outcome: Literal["Approved"]
    reviewed_at: str
    publish_claimed_at: str

    @field_validator("document_count", "edge_count", "dropped_count")
    @classmethod
    def _count(cls, value: int) -> int:
        if not 0 <= value <= 1_000_000:
            raise ValueError("bounded nonnegative count required")
        return value


class PublishedRun(BaseModel, frozen=True):
    """O5c terminal control record; the index payload remains in its own store."""

    model_config = ConfigDict(extra="forbid", strict=True)

    org_id: str
    run_id: str
    agent_id: str
    owner_id: str
    stage: Literal["Published"] = "Published"
    revision: Literal[4] = 4
    card_revision: int
    card_digest: str
    source_set_digest: str
    source_count: int
    total_bytes: int
    created_at: str
    admitted_bundle_digest: str
    document_count: int
    edge_count: int
    dropped_count: int
    author_profile_digest: str
    completed_at: str
    outcome: Literal["Approved"]
    reviewed_at: str
    publish_claimed_at: str
    acceptance_receipt_id: str
    acceptance_receipt_digest: str
    published_at: str


AuthoringRun = ExtractingRun | AwaitingOwnerReviewRun | ReviewedRun | PublishingRun | PublishedRun
AuthoringRunCommand = StartAuthoringRunCommand | CompleteAuthoringRunCommand | ReviewAuthoringRunCommand | BeginAuthoringRunPublishCommand


class AuthoringRunResource(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    org_id: str
    agent_id: str
    owner_id: str
    card_revision: int
    card_digest: str


class CurrentAuthoringRunAuthorization(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    policy_version: str
    policy_digest: str
    grant_evidence_digest: str
    identity_session_digest: str
    identity_evidence_digest: str
    resource_fingerprint: str

    @field_validator("policy_version")
    @classmethod
    def _version(cls, value: str) -> str:
        if _OPAQUE.fullmatch(value) is None:
            raise ValueError("opaque policy version required")
        return value

    @field_validator(
        "policy_digest",
        "grant_evidence_digest",
        "identity_session_digest",
        "identity_evidence_digest",
        "resource_fingerprint",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value


class TxCurrentAuthoringAuthorizer(Protocol):
    def current(
        self,
        command: AuthoringRunCommand,
        resource: AuthoringRunResource,
        source_set_digest: str,
        invocation: AuthoringInvocation,
        transaction: sqlite3.Connection,
    ) -> CurrentAuthoringRunAuthorization: ...

    def verify_precommit(
        self,
        command: AuthoringRunCommand,
        resource: AuthoringRunResource,
        source_set_digest: str,
        evidence: CurrentAuthoringRunAuthorization,
        invocation: AuthoringInvocation,
        transaction: sqlite3.Connection,
    ) -> bool: ...


FaultInjector = Callable[[str], None]


def _no_fault(_point: str) -> None:
    return None


_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE production_authoring_runs (
 org_id TEXT NOT NULL, run_id TEXT NOT NULL, agent_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 stage TEXT NOT NULL CHECK(stage IN ('extracting','awaiting_owner_review','reviewed','publishing','published')),
 revision INTEGER NOT NULL CHECK(revision IN (0,1,2,3,4)),
 card_revision INTEGER NOT NULL CHECK(card_revision>0), card_digest TEXT NOT NULL,
 source_set_digest TEXT NOT NULL, source_count INTEGER NOT NULL CHECK(source_count>0),
 total_bytes INTEGER NOT NULL CHECK(total_bytes>0), created_at TEXT NOT NULL,
 admitted_bundle_digest TEXT, document_count INTEGER, edge_count INTEGER,
 dropped_count INTEGER, author_profile_digest TEXT, completed_at TEXT,
 review_outcome TEXT CHECK(review_outcome IN ('Approved','Edited','Rejected')), reviewed_at TEXT, publish_claimed_at TEXT,
 acceptance_receipt_id TEXT, acceptance_receipt_digest TEXT, published_at TEXT,
 PRIMARY KEY(org_id,run_id),
 FOREIGN KEY(org_id,agent_id) REFERENCES production_agent_cards(org_id,agent_id),
 FOREIGN KEY(org_id,owner_id) REFERENCES production_registry_users(org_id,user_id)
);
CREATE TABLE production_authoring_source_refs (
 org_id TEXT NOT NULL, run_id TEXT NOT NULL, source_digest TEXT NOT NULL,
 byte_size INTEGER NOT NULL CHECK(byte_size>0), media_type TEXT NOT NULL
 CHECK(media_type IN ('text/plain','text/markdown')),
 PRIMARY KEY(org_id,run_id,source_digest),
 FOREIGN KEY(org_id,run_id) REFERENCES production_authoring_runs(org_id,run_id)
);
CREATE TABLE production_authoring_command_receipts (
 org_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, command_digest TEXT NOT NULL,
 action_kind TEXT NOT NULL CHECK(action_kind IN ('start','authoring_run.complete','authoring_run.review','authoring_run.publish_begin','authoring_run.publish_accept')),
 run_id TEXT NOT NULL, result_revision INTEGER NOT NULL CHECK(result_revision IN (0,1,2,3,4)),
 result_state TEXT NOT NULL CHECK(result_state IN ('extracting','awaiting_owner_review','reviewed','publishing','published')),
 policy_version TEXT NOT NULL, policy_digest TEXT NOT NULL,
 grant_evidence_digest TEXT NOT NULL, identity_session_digest TEXT NOT NULL,
 identity_evidence_digest TEXT NOT NULL, resource_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(org_id,idempotency_key),
 FOREIGN KEY(org_id,run_id) REFERENCES production_authoring_runs(org_id,run_id)
);
CREATE TABLE production_authoring_audit_intents (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('author.write','author.publish')),
 event_kind TEXT NOT NULL CHECK(event_kind IN ('run_started','run_completed','run_reviewed','run_publish_claimed','run_published')),
 principal_id TEXT NOT NULL,
 run_id TEXT NOT NULL, agent_id TEXT NOT NULL, source_set_digest TEXT NOT NULL,
 source_count INTEGER NOT NULL, total_bytes INTEGER NOT NULL,
 command_digest TEXT NOT NULL, policy_version TEXT NOT NULL,
 policy_digest TEXT NOT NULL, grant_evidence_digest TEXT NOT NULL,
 identity_session_digest TEXT NOT NULL, identity_evidence_digest TEXT NOT NULL,
 resource_fingerprint TEXT NOT NULL, result_revision INTEGER NOT NULL CHECK(result_revision IN (0,1,2,3,4)),
 admitted_bundle_digest TEXT, document_count INTEGER, edge_count INTEGER,
 dropped_count INTEGER, author_profile_digest TEXT, review_outcome TEXT, created_at TEXT NOT NULL,
 FOREIGN KEY(org_id,run_id) REFERENCES production_authoring_runs(org_id,run_id)
);
CREATE TABLE production_authoring_outbox_intents (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('authoring.run_started','authoring.run_completed','authoring.run_reviewed','authoring.run_publish_claimed','authoring.run_published')),
 run_id TEXT NOT NULL,
 agent_id TEXT NOT NULL, source_set_digest TEXT NOT NULL, source_count INTEGER NOT NULL,
 total_bytes INTEGER NOT NULL, command_digest TEXT NOT NULL,
 resource_fingerprint TEXT NOT NULL, result_revision INTEGER NOT NULL CHECK(result_revision IN (0,1,2,3,4)),
 admitted_bundle_digest TEXT, document_count INTEGER, edge_count INTEGER,
 dropped_count INTEGER, author_profile_digest TEXT, review_outcome TEXT,
 created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered=0),
 FOREIGN KEY(org_id,run_id) REFERENCES production_authoring_runs(org_id,run_id)
);
CREATE TRIGGER production_authoring_runs_immutable
BEFORE UPDATE ON production_authoring_runs
WHEN NOT (
 (OLD.stage='extracting' AND OLD.revision=0
 AND NEW.stage='awaiting_owner_review' AND NEW.revision=1
 AND OLD.org_id=NEW.org_id AND OLD.run_id=NEW.run_id
 AND OLD.agent_id=NEW.agent_id AND OLD.owner_id=NEW.owner_id
 AND OLD.card_revision=NEW.card_revision AND OLD.card_digest=NEW.card_digest
 AND OLD.source_set_digest=NEW.source_set_digest
 AND OLD.source_count=NEW.source_count AND OLD.total_bytes=NEW.total_bytes
 AND OLD.created_at=NEW.created_at
 AND OLD.admitted_bundle_digest IS NULL AND NEW.admitted_bundle_digest IS NOT NULL
 AND OLD.document_count IS NULL AND NEW.document_count IS NOT NULL
 AND OLD.edge_count IS NULL AND NEW.edge_count IS NOT NULL
 AND OLD.dropped_count IS NULL AND NEW.dropped_count IS NOT NULL
 AND OLD.author_profile_digest IS NULL AND NEW.author_profile_digest IS NOT NULL
 AND OLD.completed_at IS NULL AND NEW.completed_at IS NOT NULL
 AND NEW.review_outcome IS NULL AND NEW.reviewed_at IS NULL)
 OR (OLD.stage='awaiting_owner_review' AND OLD.revision=1
 AND NEW.stage='reviewed' AND NEW.revision=2
 AND OLD.org_id=NEW.org_id AND OLD.run_id=NEW.run_id
 AND OLD.agent_id=NEW.agent_id AND OLD.owner_id=NEW.owner_id
 AND OLD.card_revision=NEW.card_revision AND OLD.card_digest=NEW.card_digest
 AND OLD.source_set_digest=NEW.source_set_digest AND OLD.source_count=NEW.source_count
 AND OLD.total_bytes=NEW.total_bytes AND OLD.created_at=NEW.created_at
 AND OLD.admitted_bundle_digest=NEW.admitted_bundle_digest
 AND OLD.document_count=NEW.document_count AND OLD.edge_count=NEW.edge_count
 AND OLD.dropped_count=NEW.dropped_count AND OLD.author_profile_digest=NEW.author_profile_digest
 AND OLD.completed_at=NEW.completed_at AND OLD.review_outcome IS NULL
 AND NEW.review_outcome IS NOT NULL AND OLD.reviewed_at IS NULL AND NEW.reviewed_at IS NOT NULL)
 OR (OLD.stage='reviewed' AND OLD.revision=2 AND OLD.review_outcome='Approved'
 AND NEW.stage='publishing' AND NEW.revision=3
 AND OLD.org_id=NEW.org_id AND OLD.run_id=NEW.run_id AND OLD.agent_id=NEW.agent_id AND OLD.owner_id=NEW.owner_id
 AND OLD.card_revision=NEW.card_revision AND OLD.card_digest=NEW.card_digest AND OLD.source_set_digest=NEW.source_set_digest
 AND OLD.source_count=NEW.source_count AND OLD.total_bytes=NEW.total_bytes AND OLD.created_at=NEW.created_at
 AND OLD.admitted_bundle_digest=NEW.admitted_bundle_digest AND OLD.document_count=NEW.document_count AND OLD.edge_count=NEW.edge_count
 AND OLD.dropped_count=NEW.dropped_count AND OLD.author_profile_digest=NEW.author_profile_digest AND OLD.completed_at=NEW.completed_at
 AND OLD.review_outcome=NEW.review_outcome AND OLD.reviewed_at=NEW.reviewed_at AND OLD.publish_claimed_at IS NULL AND NEW.publish_claimed_at IS NOT NULL)
 OR (OLD.stage='publishing' AND OLD.revision=3 AND OLD.review_outcome='Approved'
 AND NEW.stage='published' AND NEW.revision=4
 AND OLD.org_id=NEW.org_id AND OLD.run_id=NEW.run_id AND OLD.agent_id=NEW.agent_id AND OLD.owner_id=NEW.owner_id
 AND OLD.card_revision=NEW.card_revision AND OLD.card_digest=NEW.card_digest AND OLD.source_set_digest=NEW.source_set_digest
 AND OLD.source_count=NEW.source_count AND OLD.total_bytes=NEW.total_bytes AND OLD.created_at=NEW.created_at
 AND OLD.admitted_bundle_digest=NEW.admitted_bundle_digest AND OLD.document_count=NEW.document_count AND OLD.edge_count=NEW.edge_count
 AND OLD.dropped_count=NEW.dropped_count AND OLD.author_profile_digest=NEW.author_profile_digest AND OLD.completed_at=NEW.completed_at
 AND OLD.review_outcome=NEW.review_outcome AND OLD.reviewed_at=NEW.reviewed_at AND OLD.publish_claimed_at=NEW.publish_claimed_at
 AND OLD.acceptance_receipt_id IS NULL AND NEW.acceptance_receipt_id IS NOT NULL
 AND OLD.acceptance_receipt_digest IS NULL AND NEW.acceptance_receipt_digest IS NOT NULL
 AND OLD.published_at IS NULL AND NEW.published_at IS NOT NULL)
)
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_authoring_runs_no_delete
BEFORE DELETE ON production_authoring_runs
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_authoring_sources_immutable
BEFORE UPDATE ON production_authoring_source_refs
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_authoring_sources_no_delete
BEFORE DELETE ON production_authoring_source_refs
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_authoring_receipts_immutable
BEFORE UPDATE ON production_authoring_command_receipts
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_authoring_receipts_no_delete
BEFORE DELETE ON production_authoring_command_receipts
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_authoring_audit_immutable
BEFORE UPDATE ON production_authoring_audit_intents
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_authoring_audit_no_delete
BEFORE DELETE ON production_authoring_audit_intents
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_authoring_outbox_immutable
BEFORE UPDATE ON production_authoring_outbox_intents
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER production_authoring_outbox_no_delete
BEFORE DELETE ON production_authoring_outbox_intents
BEGIN SELECT RAISE(ABORT,'immutable'); END;
"""
_TABLES = frozenset(
    {
        "production_authoring_runs",
        "production_authoring_source_refs",
        "production_authoring_command_receipts",
        "production_authoring_audit_intents",
        "production_authoring_outbox_intents",
    }
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _normalized_sql(value: str | None) -> str:
    return " ".join((value or "").split())


def _catalog(connection: sqlite3.Connection) -> tuple[object, ...]:
    objects = tuple(
        (row[0], row[1], row[2], _normalized_sql(row[3]))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name LIKE 'production_authoring_%' ORDER BY type,name"
        )
    )
    details: list[object] = []
    for table in sorted(_TABLES):
        details.append(
            (
                table,
                tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info('{table}')")),
                tuple(
                    tuple(row)
                    for row in connection.execute(f"PRAGMA foreign_key_list('{table}')")
                ),
                tuple(tuple(row) for row in connection.execute(f"PRAGMA index_list('{table}')")),
            )
        )
        for index in connection.execute(f"PRAGMA index_list('{table}')"):
            details.append(
                (
                    str(index[1]),
                    tuple(
                        tuple(row)
                        for row in connection.execute(f"PRAGMA index_info('{index[1]}')")
                    ),
                )
            )
    return objects, tuple(details)


def _canonical_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_SCHEMA)
        return _catalog(connection)
    finally:
        connection.close()


_CANONICAL_CATALOG = _canonical_catalog()


class SqliteProductionAuthoringRuns:
    @classmethod
    def migrate(cls, path: str | Path) -> None:
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                validate_production_agent_card_connection(connection)
            except ProductionAgentCardUnavailable as error:
                raise ProductionAuthoringRunUnavailable() from error
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name LIKE 'production_authoring_%'"
            ).fetchone():
                raise ProductionAuthoringRunUnavailable()
            connection.executescript(_SCHEMA)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def __init__(
        self,
        path: str | Path,
        *,
        authorize: TxCurrentAuthoringAuthorizer,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        if not Path(path).is_file():
            raise ProductionAuthoringRunUnavailable()
        if not callable(getattr(authorize, "current", None)) or not callable(
            getattr(authorize, "verify_precommit", None)
        ):
            raise ValueError("transaction-current authorizer required")
        self._authorize = authorize
        self._fault = fault_injector or _no_fault
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._validate()

    def _validate(self) -> None:
        try:
            validate_production_agent_card_connection(self._connection)
        except ProductionAgentCardUnavailable as error:
            raise ProductionAuthoringRunUnavailable() from error
        try:
            validate_authoring_catalog(self._connection)
        except Exception as error:
            raise ProductionAuthoringRunUnavailable() from error

    @staticmethod
    def _source_set_digest(sources: tuple[AuthoringSourceRef, ...]) -> str:
        return canonical_source_set_digest(
            tuple(source.model_dump(mode="json") for source in sources)
        )

    @staticmethod
    def _command_digest(command: AuthoringRunCommand) -> str:
        return sha256(_canonical_json(command.model_dump(mode="json")).encode()).hexdigest()

    @staticmethod
    def _run_id(command: StartAuthoringRunCommand) -> str:
        return canonical_start_run_id(
            command.org_id, command.agent_id, command.idempotency_key
        )

    @staticmethod
    def _fingerprint(
        resource: AuthoringRunResource, run_id: str, source_set_digest: str
    ) -> str:
        return canonical_resource_fingerprint(
            **resource.model_dump(mode="python"),
            run_id=run_id,
            source_set_digest=source_set_digest,
        )

    def start(
        self, command: StartAuthoringRunCommand, *, invocation: AuthoringInvocation
    ) -> "StartAuthoringRunResult":
        if type(command) is not StartAuthoringRunCommand:
            raise ProductionAuthoringRunUnavailable()
        if type(invocation) is not AuthoringInvocation:
            raise ProductionAuthoringRunUnavailable()
        with self._lock:
            tx = self._connection
            try:
                tx.execute("BEGIN IMMEDIATE")
                self._validate()
                card = tx.execute(
                    "SELECT owner_id,revision,card_digest FROM production_agent_cards "
                    "WHERE org_id=? AND agent_id=?",
                    (command.org_id, command.agent_id),
                ).fetchone()
                if (
                    card is None
                    or card["owner_id"] != command.principal_id
                    or int(card["revision"]) != command.expected_card_revision
                    or card["card_digest"] != command.expected_card_digest
                ):
                    raise ProductionAuthoringRunConflict()
                resource = AuthoringRunResource(
                    org_id=command.org_id,
                    agent_id=command.agent_id,
                    owner_id=card["owner_id"],
                    card_revision=card["revision"],
                    card_digest=card["card_digest"],
                )
                source_set_digest = self._source_set_digest(command.sources)
                evidence = self._authorize.current(
                    command, resource, source_set_digest, invocation, tx
                )
                if type(evidence) is not CurrentAuthoringRunAuthorization:
                    raise ProductionAuthoringRunDenied()
                command_digest = self._command_digest(command)
                receipt = tx.execute(
                    "SELECT * FROM production_authoring_command_receipts "
                    "WHERE org_id=? AND idempotency_key=?",
                    (command.org_id, command.idempotency_key),
                ).fetchone()
                if receipt is not None:
                    if receipt["command_digest"] != command_digest:
                        raise ProductionAuthoringRunConflict()
                    current_run = self._read_run(
                        tx, command.org_id, receipt["run_id"]
                    )
                    run = ExtractingRun(
                        org_id=current_run.org_id,
                        run_id=current_run.run_id,
                        agent_id=current_run.agent_id,
                        owner_id=current_run.owner_id,
                        card_revision=current_run.card_revision,
                        card_digest=current_run.card_digest,
                        source_set_digest=current_run.source_set_digest,
                        source_count=current_run.source_count,
                        total_bytes=current_run.total_bytes,
                        created_at=current_run.created_at,
                    )
                    self._verify_companions(command, run, receipt, evidence)
                    tx.commit()
                    return StartAuthoringRunResult(run=run, replayed=True)
                run_id = self._run_id(command)
                created_at = str(
                    tx.execute(
                        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                    ).fetchone()[0]
                )
                source_count = len(command.sources)
                total_bytes = sum(source.byte_size for source in command.sources)
                fingerprint = self._fingerprint(resource, run_id, source_set_digest)
                if evidence.resource_fingerprint != fingerprint:
                    raise ProductionAuthoringRunDenied()
                try:
                    tx.execute(
                        "INSERT INTO production_authoring_runs "
                        "(org_id,run_id,agent_id,owner_id,stage,revision,card_revision,"
                        "card_digest,source_set_digest,source_count,total_bytes,created_at) VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            command.org_id, run_id, command.agent_id, resource.owner_id,
                            "extracting", 0, resource.card_revision, resource.card_digest,
                            source_set_digest, source_count, total_bytes, created_at,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise ProductionAuthoringRunConflict() from error
                self._fault("after_run")
                for source in command.sources:
                    tx.execute(
                        "INSERT INTO production_authoring_source_refs VALUES (?,?,?,?,?)",
                        (
                            command.org_id, run_id, source.source_digest,
                            source.byte_size, source.media_type,
                        ),
                    )
                    self._fault("after_source")
                if not self._authorize.verify_precommit(
                    command, resource, source_set_digest, evidence, invocation, tx
                ):
                    raise ProductionAuthoringRunDenied()
                tx.execute(
                    "INSERT INTO production_authoring_command_receipts VALUES "
                    "(?,?,?,'start',?,0,'extracting',?,?,?,?,?,?,?)",
                    (
                        command.org_id, command.idempotency_key, command_digest, run_id,
                        evidence.policy_version, evidence.policy_digest,
                        evidence.grant_evidence_digest,
                        evidence.identity_session_digest,
                        evidence.identity_evidence_digest,
                        fingerprint, created_at,
                    ),
                )
                self._fault("after_receipt")
                tx.execute(
                    "INSERT INTO production_authoring_audit_intents "
                    "(org_id,action,event_kind,principal_id,run_id,agent_id,"
                    "source_set_digest,source_count,total_bytes,command_digest,"
                    "policy_version,policy_digest,grant_evidence_digest,"
                    "identity_session_digest,identity_evidence_digest,resource_fingerprint,"
                    "result_revision,created_at) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        command.org_id, "author.write", "run_started",
                        command.principal_id, run_id, command.agent_id,
                        source_set_digest, source_count, total_bytes, command_digest,
                        evidence.policy_version, evidence.policy_digest,
                        evidence.grant_evidence_digest,
                        evidence.identity_session_digest,
                        evidence.identity_evidence_digest,
                        fingerprint, 0, created_at,
                    ),
                )
                self._fault("after_audit")
                tx.execute(
                    "INSERT INTO production_authoring_outbox_intents "
                    "(org_id,kind,run_id,agent_id,source_set_digest,source_count,total_bytes,"
                    "command_digest,resource_fingerprint,result_revision,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        command.org_id, "authoring.run_started", run_id,
                        command.agent_id, source_set_digest,
                        source_count, total_bytes, command_digest, fingerprint, 0, created_at,
                    ),
                )
                self._fault("after_outbox")
                self._fault("before_commit")
                tx.commit()
                return StartAuthoringRunResult(
                    run=ExtractingRun(
                        org_id=command.org_id, run_id=run_id, agent_id=command.agent_id,
                        owner_id=resource.owner_id, card_revision=resource.card_revision,
                        card_digest=resource.card_digest, source_set_digest=source_set_digest,
                        source_count=source_count, total_bytes=total_bytes,
                        created_at=created_at,
                    )
                )
            except Exception:
                tx.rollback()
                raise

    def complete(
        self,
        command: CompleteAuthoringRunCommand | ReviewAuthoringRunCommand,
        *,
        invocation: AuthoringInvocation,
    ) -> "CompleteAuthoringRunResult":
        if type(command) is not CompleteAuthoringRunCommand:
            raise ProductionAuthoringRunUnavailable()
        if type(invocation) is not AuthoringInvocation:
            raise ProductionAuthoringRunUnavailable()
        with self._lock:
            tx = self._connection
            try:
                tx.execute("BEGIN IMMEDIATE")
                self._validate()
                run = self._read_run(tx, command.organization_id, command.run_id)
                command_digest = self._command_digest(command)
                receipt = tx.execute(
                    "SELECT * FROM production_authoring_command_receipts "
                    "WHERE org_id=? AND idempotency_key=?",
                    (command.organization_id, command.idempotency_key),
                ).fetchone()
                if receipt is not None:
                    if (
                        receipt["command_digest"] != command_digest
                        or receipt["action_kind"] != "authoring_run.complete"
                        or not isinstance(run, AwaitingOwnerReviewRun)
                    ):
                        raise ProductionAuthoringRunConflict()
                    resource = self._completion_resource(command, run, tx)
                    evidence = self._authorize.current(
                        command, resource, run.source_set_digest, invocation, tx
                    )
                    if type(evidence) is not CurrentAuthoringRunAuthorization:
                        raise ProductionAuthoringRunDenied()
                    self._verify_completion_replay(command, run, receipt, evidence)
                    tx.commit()
                    return CompleteAuthoringRunResult(run=run, replayed=True)
                if not isinstance(run, ExtractingRun):
                    raise ProductionAuthoringRunConflict()
                resource = self._completion_resource(command, run, tx)
                evidence = self._authorize.current(
                    command, resource, run.source_set_digest, invocation, tx
                )
                if type(evidence) is not CurrentAuthoringRunAuthorization:
                    raise ProductionAuthoringRunDenied()
                completed_at = str(
                    tx.execute(
                        "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                    ).fetchone()[0]
                )
                fingerprint = self._completion_fingerprint(command, run)
                if evidence.resource_fingerprint != fingerprint:
                    raise ProductionAuthoringRunDenied()
                changed = tx.execute(
                    "UPDATE production_authoring_runs SET "
                    "stage='awaiting_owner_review',revision=1,"
                    "admitted_bundle_digest=?,document_count=?,edge_count=?,dropped_count=?,"
                    "author_profile_digest=?,completed_at=? "
                    "WHERE org_id=? AND run_id=? AND agent_id=? AND owner_id=? "
                    "AND stage='extracting' AND revision=0 AND card_revision=? "
                    "AND card_digest=? AND source_set_digest=? AND source_count=? "
                    "AND total_bytes=? AND created_at=? "
                    "AND admitted_bundle_digest IS NULL AND document_count IS NULL "
                    "AND edge_count IS NULL AND dropped_count IS NULL "
                    "AND author_profile_digest IS NULL AND completed_at IS NULL",
                    (
                        command.admitted_bundle_digest,
                        command.document_count,
                        command.edge_count,
                        command.dropped_count,
                        command.author_profile_digest,
                        completed_at,
                        run.org_id,
                        run.run_id,
                        run.agent_id,
                        run.owner_id,
                        run.card_revision,
                        run.card_digest,
                        run.source_set_digest,
                        run.source_count,
                        run.total_bytes,
                        run.created_at,
                    ),
                ).rowcount
                if changed != 1:
                    raise ProductionAuthoringRunConflict()
                self._fault("after_transition")
                if not self._authorize.verify_precommit(
                    command, resource, run.source_set_digest, evidence, invocation, tx
                ):
                    raise ProductionAuthoringRunDenied()
                tx.execute(
                    "INSERT INTO production_authoring_command_receipts "
                    "(org_id,idempotency_key,command_digest,action_kind,run_id,"
                    "result_revision,result_state,policy_version,policy_digest,"
                    "grant_evidence_digest,identity_session_digest,"
                    "identity_evidence_digest,resource_fingerprint,created_at) "
                    "VALUES (?,?,?,'authoring_run.complete',?,1,"
                    "'awaiting_owner_review',?,?,?,?,?,?,?)",
                    (
                        command.organization_id,
                        command.idempotency_key,
                        command_digest,
                        command.run_id,
                        evidence.policy_version,
                        evidence.policy_digest,
                        evidence.grant_evidence_digest,
                        evidence.identity_session_digest,
                        evidence.identity_evidence_digest,
                        fingerprint,
                        completed_at,
                    ),
                )
                self._fault("after_receipt")
                tx.execute(
                    "INSERT INTO production_authoring_audit_intents "
                    "(org_id,action,event_kind,principal_id,run_id,agent_id,"
                    "source_set_digest,source_count,total_bytes,command_digest,"
                    "policy_version,policy_digest,grant_evidence_digest,"
                    "identity_session_digest,identity_evidence_digest,resource_fingerprint,"
                    "result_revision,admitted_bundle_digest,document_count,edge_count,"
                    "dropped_count,author_profile_digest,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        command.organization_id, "author.write", "run_completed",
                        command.principal_id,
                        run.run_id,
                        run.agent_id,
                        run.source_set_digest,
                        run.source_count,
                        run.total_bytes,
                        command_digest,
                        evidence.policy_version,
                        evidence.policy_digest,
                        evidence.grant_evidence_digest,
                        evidence.identity_session_digest,
                        evidence.identity_evidence_digest,
                        fingerprint,
                        1,
                        command.admitted_bundle_digest,
                        command.document_count,
                        command.edge_count,
                        command.dropped_count,
                        command.author_profile_digest,
                        completed_at,
                    ),
                )
                self._fault("after_audit")
                tx.execute(
                    "INSERT INTO production_authoring_outbox_intents "
                    "(org_id,kind,run_id,agent_id,source_set_digest,source_count,"
                    "total_bytes,command_digest,resource_fingerprint,result_revision,"
                    "admitted_bundle_digest,document_count,edge_count,dropped_count,"
                    "author_profile_digest,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        command.organization_id, "authoring.run_completed",
                        run.run_id,
                        run.agent_id,
                        run.source_set_digest,
                        run.source_count,
                        run.total_bytes,
                        command_digest,
                        fingerprint,
                        1,
                        command.admitted_bundle_digest,
                        command.document_count,
                        command.edge_count,
                        command.dropped_count,
                        command.author_profile_digest,
                        completed_at,
                    ),
                )
                self._fault("after_outbox")
                self._fault("before_commit")
                completed_values = run.model_dump(mode="python")
                completed_values.update(
                    {
                        "stage": "AwaitingOwnerReview",
                        "revision": 1,
                        "admitted_bundle_digest": command.admitted_bundle_digest,
                        "document_count": command.document_count,
                        "edge_count": command.edge_count,
                        "dropped_count": command.dropped_count,
                        "author_profile_digest": command.author_profile_digest,
                        "completed_at": completed_at,
                    }
                )
                completed_run = AwaitingOwnerReviewRun(**completed_values)
                tx.commit()
                return CompleteAuthoringRunResult(
                    run=completed_run
                )
            except Exception:
                tx.rollback()
                raise

    def review(
        self, command: ReviewAuthoringRunCommand, *, invocation: AuthoringInvocation
    ) -> "ReviewAuthoringRunResult":
        if type(command) is not ReviewAuthoringRunCommand or type(invocation) is not AuthoringInvocation:
            raise ProductionAuthoringRunUnavailable()
        with self._lock:
            tx = self._connection
            try:
                tx.execute("BEGIN IMMEDIATE")
                self._validate()
                run = self._read_run(tx, command.organization_id, command.run_id)
                digest = self._command_digest(command)
                receipt = tx.execute("SELECT * FROM production_authoring_command_receipts WHERE org_id=? AND idempotency_key=?", (command.organization_id, command.idempotency_key)).fetchone()
                if receipt is not None:
                    if receipt["command_digest"] != digest or receipt["action_kind"] != "authoring_run.review" or not isinstance(run, ReviewedRun):
                        raise ProductionAuthoringRunConflict()
                    resource = self._review_resource(command, run, tx)
                    evidence = self._authorize.current(
                        command, resource, run.source_set_digest, invocation, tx
                    )
                    if type(evidence) is not CurrentAuthoringRunAuthorization:
                        raise ProductionAuthoringRunDenied()
                    self._verify_review_replay(command, run, receipt, evidence)
                    tx.commit()
                    return ReviewAuthoringRunResult(run=run, replayed=True)
                if not isinstance(run, AwaitingOwnerReviewRun):
                    raise ProductionAuthoringRunConflict()
                resource = self._review_resource(command, run, tx)
                if command.source_digest != run.source_set_digest or command.draft_digest != run.admitted_bundle_digest:
                    raise ProductionAuthoringRunConflict()
                fingerprint = self._review_fingerprint(command, run)
                evidence = self._authorize.current(command, resource, run.source_set_digest, invocation, tx)
                if type(evidence) is not CurrentAuthoringRunAuthorization or evidence.resource_fingerprint != fingerprint:
                    raise ProductionAuthoringRunDenied()
                reviewed_at = str(tx.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0])
                changed = tx.execute(
                    "UPDATE production_authoring_runs SET stage='reviewed',revision=2,review_outcome=?,reviewed_at=? "
                    "WHERE org_id=? AND run_id=? AND stage='awaiting_owner_review' AND revision=1 AND review_outcome IS NULL AND reviewed_at IS NULL",
                    (command.outcome, reviewed_at, run.org_id, run.run_id),
                ).rowcount
                if changed != 1 or not self._authorize.verify_precommit(command, resource, run.source_set_digest, evidence, invocation, tx):
                    raise ProductionAuthoringRunDenied()
                self._fault("after_transition")
                tx.execute("INSERT INTO production_authoring_command_receipts VALUES (?,?,?,'authoring_run.review',?,2,'reviewed',?,?,?,?,?,?,?)", (command.organization_id, command.idempotency_key, digest, command.run_id, evidence.policy_version, evidence.policy_digest, evidence.grant_evidence_digest, evidence.identity_session_digest, evidence.identity_evidence_digest, fingerprint, reviewed_at))
                self._fault("after_receipt")
                tx.execute(
                    "INSERT INTO production_authoring_audit_intents (org_id,action,event_kind,principal_id,run_id,agent_id,source_set_digest,source_count,total_bytes,command_digest,policy_version,policy_digest,grant_evidence_digest,identity_session_digest,identity_evidence_digest,resource_fingerprint,result_revision,admitted_bundle_digest,document_count,edge_count,dropped_count,author_profile_digest,review_outcome,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run.org_id, 'author.publish', 'run_reviewed', command.principal_id, run.run_id, run.agent_id, run.source_set_digest, run.source_count, run.total_bytes, digest, evidence.policy_version, evidence.policy_digest, evidence.grant_evidence_digest, evidence.identity_session_digest, evidence.identity_evidence_digest, fingerprint, 2, run.admitted_bundle_digest, run.document_count, run.edge_count, run.dropped_count, run.author_profile_digest, command.outcome, reviewed_at),
                )
                self._fault("after_audit")
                tx.execute(
                    "INSERT INTO production_authoring_outbox_intents (org_id,kind,run_id,agent_id,source_set_digest,source_count,total_bytes,command_digest,resource_fingerprint,result_revision,admitted_bundle_digest,document_count,edge_count,dropped_count,author_profile_digest,review_outcome,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run.org_id, 'authoring.run_reviewed', run.run_id, run.agent_id, run.source_set_digest, run.source_count, run.total_bytes, digest, fingerprint, 2, run.admitted_bundle_digest, run.document_count, run.edge_count, run.dropped_count, run.author_profile_digest, command.outcome, reviewed_at),
                )
                self._fault("after_outbox")
                self._fault("before_commit")
                values = run.model_dump(mode="python")
                values.update(stage="Reviewed", revision=2, outcome=command.outcome, reviewed_at=reviewed_at)
                result = ReviewedRun(**values)
                tx.commit()
                return ReviewAuthoringRunResult(run=result)
            except Exception:
                tx.rollback()
                raise

    def begin_publish(
        self, command: BeginAuthoringRunPublishCommand, *, invocation: AuthoringInvocation
    ) -> "BeginAuthoringRunPublishResult":
        if type(command) is not BeginAuthoringRunPublishCommand or type(invocation) is not AuthoringInvocation:
            raise ProductionAuthoringRunUnavailable()
        with self._lock:
            tx = self._connection
            try:
                tx.execute("BEGIN IMMEDIATE")
                self._validate()
                run = self._read_run(tx, command.organization_id, command.run_id)
                digest = self._command_digest(command)
                receipt = tx.execute("SELECT * FROM production_authoring_command_receipts WHERE org_id=? AND idempotency_key=?", (command.organization_id, command.idempotency_key)).fetchone()
                resource = self._publish_resource(command, run, tx)
                evidence = self._authorize.current(command, resource, run.source_set_digest, invocation, tx)
                if type(evidence) is not CurrentAuthoringRunAuthorization:
                    raise ProductionAuthoringRunDenied()
                fingerprint = self._publish_fingerprint(command, run)
                if evidence.resource_fingerprint != fingerprint:
                    raise ProductionAuthoringRunDenied()
                if receipt is not None:
                    if receipt["command_digest"] != digest or receipt["action_kind"] != "authoring_run.publish_begin" or not isinstance(run, PublishingRun):
                        raise ProductionAuthoringRunConflict()
                    self._verify_publish_replay(command, run, receipt, evidence)
                    tx.commit()
                    return BeginAuthoringRunPublishResult(run=run, replayed=True)
                if not isinstance(run, ReviewedRun) or run.outcome != "Approved":
                    raise ProductionAuthoringRunConflict()
                claimed_at = str(tx.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0])
                changed = tx.execute("UPDATE production_authoring_runs SET stage='publishing',revision=3,publish_claimed_at=? WHERE org_id=? AND run_id=? AND stage='reviewed' AND revision=2 AND review_outcome='Approved' AND publish_claimed_at IS NULL", (claimed_at, run.org_id, run.run_id)).rowcount
                if changed != 1 or not self._authorize.verify_precommit(command, resource, run.source_set_digest, evidence, invocation, tx):
                    raise ProductionAuthoringRunDenied()
                self._fault("after_transition")
                tx.execute("INSERT INTO production_authoring_command_receipts VALUES (?,?,?,'authoring_run.publish_begin',?,3,'publishing',?,?,?,?,?,?,?)", (command.organization_id, command.idempotency_key, digest, command.run_id, evidence.policy_version, evidence.policy_digest, evidence.grant_evidence_digest, evidence.identity_session_digest, evidence.identity_evidence_digest, fingerprint, claimed_at))
                self._fault("after_receipt")
                tx.execute("INSERT INTO production_authoring_audit_intents (org_id,action,event_kind,principal_id,run_id,agent_id,source_set_digest,source_count,total_bytes,command_digest,policy_version,policy_digest,grant_evidence_digest,identity_session_digest,identity_evidence_digest,resource_fingerprint,result_revision,admitted_bundle_digest,document_count,edge_count,dropped_count,author_profile_digest,review_outcome,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run.org_id, 'author.publish', 'run_publish_claimed', command.principal_id, run.run_id, run.agent_id, run.source_set_digest, run.source_count, run.total_bytes, digest, evidence.policy_version, evidence.policy_digest, evidence.grant_evidence_digest, evidence.identity_session_digest, evidence.identity_evidence_digest, fingerprint, 3, run.admitted_bundle_digest, run.document_count, run.edge_count, run.dropped_count, run.author_profile_digest, run.outcome, claimed_at))
                self._fault("after_audit")
                tx.execute("INSERT INTO production_authoring_outbox_intents (org_id,kind,run_id,agent_id,source_set_digest,source_count,total_bytes,command_digest,resource_fingerprint,result_revision,admitted_bundle_digest,document_count,edge_count,dropped_count,author_profile_digest,review_outcome,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run.org_id, 'authoring.run_publish_claimed', run.run_id, run.agent_id, run.source_set_digest, run.source_count, run.total_bytes, digest, fingerprint, 3, run.admitted_bundle_digest, run.document_count, run.edge_count, run.dropped_count, run.author_profile_digest, run.outcome, claimed_at))
                self._fault("after_outbox")
                self._fault("before_commit")
                result = PublishingRun.model_validate(run.model_dump(mode="python") | {"stage": "Publishing", "revision": 3, "publish_claimed_at": claimed_at})
                tx.commit()
                return BeginAuthoringRunPublishResult(run=result)
            except Exception:
                tx.rollback()
                raise

    @staticmethod
    def _publish_resource(command: BeginAuthoringRunPublishCommand, run: AuthoringRun, tx: sqlite3.Connection) -> AuthoringRunResource:
        if not isinstance(run, (ReviewedRun, PublishingRun)) or run.outcome != "Approved":
            raise ProductionAuthoringRunConflict()
        return SqliteProductionAuthoringRuns._completion_resource(command, run, tx)

    @staticmethod
    def _publish_fingerprint(command: BeginAuthoringRunPublishCommand, run: AuthoringRun) -> str:
        return sha256(_canonical_json({"org_id": run.org_id, "agent_id": run.agent_id, "owner_id": run.owner_id, "card_revision": run.card_revision, "card_digest": run.card_digest, "run_id": run.run_id, "source_set_digest": run.source_set_digest, "review_revision": 2}).encode()).hexdigest()

    def _verify_publish_replay(self, command: BeginAuthoringRunPublishCommand, run: PublishingRun, receipt: sqlite3.Row, evidence: CurrentAuthoringRunAuthorization) -> None:
        fingerprint = self._publish_fingerprint(command, run)
        if (
            evidence.resource_fingerprint != fingerprint
            or receipt["action_kind"] != "authoring_run.publish_begin"
            or int(receipt["result_revision"]) != 3
            or receipt["result_state"] != "publishing"
            or receipt["resource_fingerprint"] != fingerprint
            or receipt["created_at"] != run.publish_claimed_at
            or receipt["policy_version"] != evidence.policy_version
            or receipt["policy_digest"] != evidence.policy_digest
            or receipt["grant_evidence_digest"] != evidence.grant_evidence_digest
            or receipt["identity_session_digest"] != evidence.identity_session_digest
            or receipt["identity_evidence_digest"] != evidence.identity_evidence_digest
        ):
            raise ProductionAuthoringRunUnavailable()
        audit = self._connection.execute("SELECT * FROM production_authoring_audit_intents WHERE org_id=? AND run_id=? AND event_kind='run_publish_claimed'", (run.org_id, run.run_id)).fetchall()
        outbox = self._connection.execute("SELECT * FROM production_authoring_outbox_intents WHERE org_id=? AND run_id=? AND kind='authoring.run_publish_claimed'", (run.org_id, run.run_id)).fetchall()
        if len(audit) != 1 or len(outbox) != 1:
            raise ProductionAuthoringRunUnavailable()
        a, o = audit[0], outbox[0]
        names = (
            "org_id", "run_id", "agent_id", "source_set_digest", "source_count",
            "total_bytes", "command_digest", "resource_fingerprint",
        )
        common = (
            run.org_id, run.run_id, run.agent_id, run.source_set_digest,
            run.source_count, run.total_bytes, receipt["command_digest"], fingerprint,
        )
        if (
            tuple(a[name] for name in names) != common
            or tuple(o[name] for name in names) != common
            or a["action"] != "author.publish"
            or a["principal_id"] != command.principal_id
            or int(a["result_revision"]) != 3
            or int(o["result_revision"]) != 3
            or a["review_outcome"] != "Approved"
            or o["review_outcome"] != "Approved"
            or any(
                a[name] != getattr(run, name) or o[name] != getattr(run, name)
                for name in (
                    "admitted_bundle_digest", "document_count", "edge_count",
                    "dropped_count", "author_profile_digest",
                )
            )
            or a["created_at"] != run.publish_claimed_at
            or o["created_at"] != run.publish_claimed_at
            or int(o["delivered"]) != 0
            or a["policy_version"] != receipt["policy_version"]
            or a["policy_digest"] != receipt["policy_digest"]
            or a["grant_evidence_digest"] != receipt["grant_evidence_digest"]
            or a["identity_session_digest"] != receipt["identity_session_digest"]
            or a["identity_evidence_digest"] != receipt["identity_evidence_digest"]
        ):
            raise ProductionAuthoringRunUnavailable()

    @staticmethod
    def _review_resource(
        command: ReviewAuthoringRunCommand,
        run: AwaitingOwnerReviewRun | ReviewedRun,
        tx: sqlite3.Connection,
    ) -> AuthoringRunResource:
        resource = SqliteProductionAuthoringRuns._completion_resource(command, run, tx)
        return resource

    @staticmethod
    def _review_fingerprint(
        command: ReviewAuthoringRunCommand,
        run: AwaitingOwnerReviewRun | ReviewedRun,
    ) -> str:
        return sha256(_canonical_json({"org_id": run.org_id, "agent_id": run.agent_id, "owner_id": run.owner_id, "card_revision": run.card_revision, "card_digest": run.card_digest, "run_id": run.run_id, "source_set_digest": run.source_set_digest, "command_digest": SqliteProductionAuthoringRuns._command_digest(command), "source_digest": command.source_digest, "draft_digest": command.draft_digest, "outcome": command.outcome}).encode()).hexdigest()

    @staticmethod
    def _completion_resource(
        command: CompleteAuthoringRunCommand | ReviewAuthoringRunCommand | BeginAuthoringRunPublishCommand,
        run: AuthoringRun,
        tx: sqlite3.Connection,
    ) -> AuthoringRunResource:
        card = tx.execute(
            "SELECT owner_id,revision,card_digest FROM production_agent_cards "
            "WHERE org_id=? AND agent_id=?",
            (run.org_id, run.agent_id),
        ).fetchone()
        if (
            card is None
            or card["owner_id"] != run.owner_id
            or int(card["revision"]) != run.card_revision
            or card["card_digest"] != run.card_digest
            or command.organization_id != run.org_id
            or command.principal_id != run.owner_id
            or command.expected_revision != (1 if type(command) is ReviewAuthoringRunCommand else (2 if type(command) is BeginAuthoringRunPublishCommand else 0))
            or command.expected_card_revision != run.card_revision
            or command.expected_card_digest != run.card_digest
        ):
            raise ProductionAuthoringRunConflict()
        return AuthoringRunResource(
            org_id=run.org_id,
            agent_id=run.agent_id,
            owner_id=run.owner_id,
            card_revision=run.card_revision,
            card_digest=run.card_digest,
        )

    @staticmethod
    def _completion_fingerprint(
        command: CompleteAuthoringRunCommand, run: AuthoringRun
    ) -> str:
        return canonical_completion_resource_fingerprint(
            org_id=run.org_id,
            run_id=run.run_id,
            agent_id=run.agent_id,
            owner_id=run.owner_id,
            card_revision=run.card_revision,
            card_digest=run.card_digest,
            source_set_digest=run.source_set_digest,
            admitted_bundle_digest=command.admitted_bundle_digest,
            document_count=command.document_count,
            edge_count=command.edge_count,
            dropped_count=command.dropped_count,
            author_profile_digest=command.author_profile_digest,
        )

    def _verify_completion_replay(
        self,
        command: CompleteAuthoringRunCommand,
        run: AwaitingOwnerReviewRun,
        receipt: sqlite3.Row,
        evidence: CurrentAuthoringRunAuthorization,
    ) -> None:
        fingerprint = self._completion_fingerprint(command, run)
        if evidence.resource_fingerprint != fingerprint:
            raise ProductionAuthoringRunDenied()
        if (
            int(receipt["result_revision"]) != 1
            or receipt["result_state"] != "awaiting_owner_review"
            or receipt["resource_fingerprint"] != fingerprint
            or receipt["created_at"] != run.completed_at
        ):
            raise ProductionAuthoringRunUnavailable()

    def _verify_review_replay(
        self,
        command: ReviewAuthoringRunCommand,
        run: ReviewedRun,
        receipt: sqlite3.Row,
        evidence: CurrentAuthoringRunAuthorization,
    ) -> None:
        fingerprint = self._review_fingerprint(command, run)
        if evidence.resource_fingerprint != fingerprint:
            raise ProductionAuthoringRunDenied()
        if (
            int(receipt["result_revision"]) != 2
            or receipt["result_state"] != "reviewed"
            or receipt["resource_fingerprint"] != fingerprint
            or receipt["created_at"] != run.reviewed_at
        ):
            raise ProductionAuthoringRunUnavailable()
        common = (
            run.org_id, run.run_id, run.agent_id, run.source_set_digest,
            run.source_count, run.total_bytes, receipt["command_digest"], fingerprint,
        )
        audit = self._connection.execute(
            "SELECT * FROM production_authoring_audit_intents "
            "WHERE org_id=? AND run_id=? AND event_kind='run_reviewed'",
            (run.org_id, run.run_id),
        ).fetchall()
        outbox = self._connection.execute(
            "SELECT * FROM production_authoring_outbox_intents "
            "WHERE org_id=? AND run_id=? AND kind='authoring.run_reviewed'",
            (run.org_id, run.run_id),
        ).fetchall()
        if len(audit) != 1 or len(outbox) != 1:
            raise ProductionAuthoringRunUnavailable()
        a, o = audit[0], outbox[0]
        names = (
            "org_id", "run_id", "agent_id", "source_set_digest", "source_count",
            "total_bytes", "command_digest", "resource_fingerprint",
        )
        if (
            tuple(a[name] for name in names) != common
            or tuple(o[name] for name in names) != common
            or a["action"] != "author.publish"
            or a["principal_id"] != command.principal_id
            or int(a["result_revision"]) != 2
            or int(o["result_revision"]) != 2
            or a["review_outcome"] != command.outcome
            or o["review_outcome"] != command.outcome
            or a["admitted_bundle_digest"] != run.admitted_bundle_digest
            or o["admitted_bundle_digest"] != run.admitted_bundle_digest
            or a["document_count"] != run.document_count
            or o["document_count"] != run.document_count
            or a["edge_count"] != run.edge_count
            or o["edge_count"] != run.edge_count
            or a["dropped_count"] != run.dropped_count
            or o["dropped_count"] != run.dropped_count
            or a["author_profile_digest"] != run.author_profile_digest
            or o["author_profile_digest"] != run.author_profile_digest
            or a["created_at"] != run.reviewed_at
            or o["created_at"] != run.reviewed_at
            or str(a["policy_version"]) != str(receipt["policy_version"])
            or a["policy_digest"] != receipt["policy_digest"]
            or a["grant_evidence_digest"] != receipt["grant_evidence_digest"]
            or a["identity_session_digest"] != receipt["identity_session_digest"]
            or a["identity_evidence_digest"] != receipt["identity_evidence_digest"]
        ):
            raise ProductionAuthoringRunUnavailable()

    def _read_run(
        self, tx: sqlite3.Connection, org_id: str, run_id: str
    ) -> AuthoringRun:
        row = tx.execute(
            "SELECT * FROM production_authoring_runs WHERE org_id=? AND run_id=?",
            (org_id, run_id),
        ).fetchone()
        if row is None:
            raise ProductionAuthoringRunUnavailable()
        try:
            common = dict(
                org_id=row["org_id"], run_id=row["run_id"], agent_id=row["agent_id"],
                owner_id=row["owner_id"], card_revision=row["card_revision"],
                card_digest=row["card_digest"], source_set_digest=row["source_set_digest"],
                source_count=row["source_count"], total_bytes=row["total_bytes"],
                created_at=row["created_at"],
            )
            if row["stage"] == "extracting" and int(row["revision"]) == 0:
                if any(
                    row[name] is not None
                    for name in (
                        "admitted_bundle_digest", "document_count", "edge_count",
                        "dropped_count", "author_profile_digest", "completed_at",
                    )
                ):
                    raise ValueError
                run: AuthoringRun = ExtractingRun(**common)
            elif row["stage"] == "awaiting_owner_review" and int(row["revision"]) == 1:
                run = AwaitingOwnerReviewRun(
                    **common,
                    admitted_bundle_digest=row["admitted_bundle_digest"],
                    document_count=row["document_count"],
                    edge_count=row["edge_count"],
                    dropped_count=row["dropped_count"],
                    author_profile_digest=row["author_profile_digest"],
                    completed_at=row["completed_at"],
                )
            elif row["stage"] == "reviewed" and int(row["revision"]) == 2:
                run = ReviewedRun(
                    **common,
                    admitted_bundle_digest=row["admitted_bundle_digest"],
                    document_count=row["document_count"], edge_count=row["edge_count"],
                    dropped_count=row["dropped_count"], author_profile_digest=row["author_profile_digest"],
                    completed_at=row["completed_at"], outcome=row["review_outcome"],
                    reviewed_at=row["reviewed_at"],
                )
            elif row["stage"] == "publishing" and int(row["revision"]) == 3:
                run = PublishingRun(
                    org_id=row["org_id"], run_id=row["run_id"], agent_id=row["agent_id"], owner_id=row["owner_id"],
                    card_revision=row["card_revision"], card_digest=row["card_digest"], source_set_digest=row["source_set_digest"],
                    source_count=row["source_count"], total_bytes=row["total_bytes"], created_at=row["created_at"],
                    admitted_bundle_digest=row["admitted_bundle_digest"], document_count=row["document_count"], edge_count=row["edge_count"],
                    dropped_count=row["dropped_count"], author_profile_digest=row["author_profile_digest"], completed_at=row["completed_at"],
                    outcome=row["review_outcome"], reviewed_at=row["reviewed_at"], publish_claimed_at=row["publish_claimed_at"],
                )
            elif row["stage"] == "published" and int(row["revision"]) == 4:
                run = PublishedRun(
                    org_id=row["org_id"], run_id=row["run_id"], agent_id=row["agent_id"], owner_id=row["owner_id"],
                    card_revision=row["card_revision"], card_digest=row["card_digest"], source_set_digest=row["source_set_digest"],
                    source_count=row["source_count"], total_bytes=row["total_bytes"], created_at=row["created_at"],
                    admitted_bundle_digest=row["admitted_bundle_digest"], document_count=row["document_count"], edge_count=row["edge_count"],
                    dropped_count=row["dropped_count"], author_profile_digest=row["author_profile_digest"], completed_at=row["completed_at"],
                    outcome=row["review_outcome"], reviewed_at=row["reviewed_at"], publish_claimed_at=row["publish_claimed_at"],
                    acceptance_receipt_id=row["acceptance_receipt_id"], acceptance_receipt_digest=row["acceptance_receipt_digest"], published_at=row["published_at"],
                )
            else:
                raise ValueError
        except Exception as error:
            raise ProductionAuthoringRunUnavailable() from error
        if isinstance(run, PublishedRun):
            self._verify_published_acceptance(tx, run)
        refs = self._read_sources(tx, org_id, run_id)
        if (
            len(refs) != run.source_count
            or sum(ref.byte_size for ref in refs) != run.total_bytes
            or self._source_set_digest(refs) != run.source_set_digest
        ):
            raise ProductionAuthoringRunUnavailable()
        resource = AuthoringRunResource(
            org_id=run.org_id,
            agent_id=run.agent_id,
            owner_id=run.owner_id,
            card_revision=run.card_revision,
            card_digest=run.card_digest,
        )
        try:
            validate_current_authoring_resource(
                tx,
                **resource.model_dump(mode="python"),
                run_id=run.run_id,
                source_set_digest=run.source_set_digest,
                allow_absent=False,
                expected_stage=(
                    "awaiting_owner_review"
                    if isinstance(run, AwaitingOwnerReviewRun)
                    else (
                        "reviewed"
                        if isinstance(run, ReviewedRun)
                    else ("published" if isinstance(run, PublishedRun) else ("publishing" if isinstance(run, PublishingRun) else "extracting"))
                    )
                ),
                completion=(
                    {
                        "admitted_bundle_digest": run.admitted_bundle_digest,
                        "document_count": run.document_count,
                        "edge_count": run.edge_count,
                        "dropped_count": run.dropped_count,
                        "author_profile_digest": run.author_profile_digest,
                    }
                    if isinstance(run, (AwaitingOwnerReviewRun, ReviewedRun, PublishingRun, PublishedRun))
                    else None
                ),
            )
        except ProductionAuthoringResourceUnavailable as error:
            raise ProductionAuthoringRunUnavailable() from error
        return run

    @staticmethod
    def _verify_published_acceptance(
        tx: sqlite3.Connection, run: PublishedRun
    ) -> None:
        """Published control is readable only with its exact O5c immutable proof."""
        required = {
            "published_index_payloads",
            "published_index_acceptance_receipts",
            "published_index_latest_events",
        }
        actual = {
            str(row[0])
            for row in tx.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not required <= actual:
            raise ProductionAuthoringRunUnavailable()
        receipt = tx.execute(
            "SELECT * FROM published_index_acceptance_receipts WHERE receipt_id=?",
            (run.acceptance_receipt_id,),
        ).fetchone()
        if receipt is None:
            raise ProductionAuthoringRunUnavailable()
        expected_receipt_digest = sha256(
            json.dumps(
                {
                    "receipt_id": receipt["receipt_id"],
                    "command_digest": receipt["command_digest"],
                    "payload_digest": receipt["payload_digest"],
                    "commit_sha": receipt["commit_sha"],
                    "committed_tree_index_digest": receipt["committed_tree_index_digest"],
                    "accepted_at": receipt["accepted_at"],
                },
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        payload = tx.execute(
            "SELECT * FROM published_index_payloads "
            "WHERE org_id=? AND agent_id=? AND run_id=? AND review_revision=2",
            (run.org_id, run.agent_id, run.run_id),
        ).fetchone()
        event = tx.execute(
            "SELECT * FROM published_index_latest_events WHERE receipt_id=?",
            (run.acceptance_receipt_id,),
        ).fetchone()
        controls = tx.execute(
            "SELECT * FROM production_authoring_command_receipts "
            "WHERE org_id=? AND run_id=? AND action_kind='authoring_run.publish_accept'",
            (run.org_id, run.run_id),
        ).fetchall()
        if payload is None or event is None or len(controls) != 1:
            raise ProductionAuthoringRunUnavailable()
        try:
            index = KnowledgeIndex.model_validate(json.loads(payload["payload_json"]))
            canonical_payload = json.dumps(
                index.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            canonical_payload_digest = sha256(canonical_payload.encode()).hexdigest()
            expected_command_digest = sha256(
                json.dumps(
                    {
                        "organization_id": run.org_id,
                        "principal_id": run.owner_id,
                        "idempotency_key": receipt["idempotency_key"],
                        "run_id": run.run_id,
                        "expected_revision": 3,
                        "expected_card_revision": run.card_revision,
                        "expected_card_digest": run.card_digest,
                        "commit_sha": receipt["commit_sha"],
                        "committed_tree_index_digest": receipt["committed_tree_index_digest"],
                        "index": index.model_dump(mode="json"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest()
        except Exception as error:
            raise ProductionAuthoringRunUnavailable() from error
        if (
            receipt["receipt_digest"] != run.acceptance_receipt_digest
            or receipt["receipt_digest"] != expected_receipt_digest
            or receipt["org_id"] != run.org_id
            or receipt["agent_id"] != run.agent_id
            or receipt["run_id"] != run.run_id
            or int(receipt["review_revision"]) != 2
            or payload["payload_json"] != canonical_payload
            or payload["payload_digest"] != canonical_payload_digest
            or index.agent_id != run.agent_id
            or _DIGEST.fullmatch(str(receipt["commit_sha"])) is None
            or _DIGEST.fullmatch(str(receipt["committed_tree_index_digest"])) is None
            or receipt["command_digest"] != expected_command_digest
            or payload["payload_digest"] != receipt["payload_digest"]
            or event["org_id"] != run.org_id
            or event["agent_id"] != run.agent_id
            or event["payload_digest"] != receipt["payload_digest"]
            or event["generated_at"] != payload["generated_at"]
            or event["accepted_at"] != receipt["accepted_at"]
        ):
            raise ProductionAuthoringRunUnavailable()
        control = controls[0]
        if (
            control["command_digest"] != receipt["command_digest"]
            or int(control["result_revision"]) != 4
            or control["result_state"] != "published"
            or control["created_at"] != run.published_at
            or control["created_at"] != receipt["accepted_at"]
            or any(
                control[name] != receipt[name]
                for name in (
                    "policy_version", "policy_digest", "grant_evidence_digest",
                    "identity_session_digest", "identity_evidence_digest",
                    "resource_fingerprint",
                )
            )
        ):
            raise ProductionAuthoringRunUnavailable()

    @staticmethod
    def _read_sources(
        tx: sqlite3.Connection, org_id: str, run_id: str
    ) -> tuple[AuthoringSourceRef, ...]:
        try:
            return tuple(
                AuthoringSourceRef(
                    source_digest=row["source_digest"],
                    byte_size=row["byte_size"],
                    media_type=row["media_type"],
                )
                for row in tx.execute(
                    "SELECT * FROM production_authoring_source_refs "
                    "WHERE org_id=? AND run_id=? ORDER BY source_digest",
                    (org_id, run_id),
                )
            )
        except Exception as error:
            raise ProductionAuthoringRunUnavailable() from error

    def _verify_companions(
        self,
        command: StartAuthoringRunCommand,
        run: ExtractingRun,
        receipt: sqlite3.Row,
        evidence: CurrentAuthoringRunAuthorization,
    ) -> None:
        resource = AuthoringRunResource(
            org_id=run.org_id, agent_id=run.agent_id, owner_id=run.owner_id,
            card_revision=run.card_revision, card_digest=run.card_digest,
        )
        fingerprint = self._fingerprint(resource, run.run_id, run.source_set_digest)
        if evidence.resource_fingerprint != fingerprint:
            raise ProductionAuthoringRunDenied()
        if (
            receipt["action_kind"] != "start"
            or receipt["result_state"] != "extracting"
            or int(receipt["result_revision"]) != 0
            or receipt["resource_fingerprint"] != fingerprint
            or receipt["created_at"] != run.created_at
        ):
            raise ProductionAuthoringRunUnavailable()
        common = (
            command.org_id, run.run_id, run.agent_id, run.source_set_digest,
            run.source_count, run.total_bytes, receipt["command_digest"], fingerprint,
        )
        audit = self._connection.execute(
            "SELECT * FROM production_authoring_audit_intents "
            "WHERE org_id=? AND run_id=? AND event_kind='run_started'",
            (command.org_id, run.run_id),
        ).fetchall()
        outbox = self._connection.execute(
            "SELECT * FROM production_authoring_outbox_intents "
            "WHERE org_id=? AND run_id=? AND kind='authoring.run_started'",
            (command.org_id, run.run_id),
        ).fetchall()
        if len(audit) != 1 or len(outbox) != 1:
            raise ProductionAuthoringRunUnavailable()
        a, o = audit[0], outbox[0]
        names = (
            "org_id", "run_id", "agent_id", "source_set_digest",
            "source_count", "total_bytes", "command_digest", "resource_fingerprint",
        )
        if (
            tuple(a[name] for name in names) != common
            or tuple(o[name] for name in names) != common
            or a["action"] != "author.write"
            or a["event_kind"] != "run_started"
            or a["principal_id"] != command.principal_id
            or str(a["policy_version"]) != str(receipt["policy_version"])
            or a["policy_digest"] != receipt["policy_digest"]
            or a["grant_evidence_digest"] != receipt["grant_evidence_digest"]
            or a["identity_session_digest"] != receipt["identity_session_digest"]
            or a["identity_evidence_digest"] != receipt["identity_evidence_digest"]
            or o["kind"] != "authoring.run_started"
            or a["created_at"] != run.created_at
            or o["created_at"] != run.created_at
        ):
            raise ProductionAuthoringRunUnavailable()

    def runs(self, org_id: str) -> tuple[AuthoringRun, ...]:
        with self._lock:
            try:
                self._connection.execute("BEGIN")
                self._validate()
                ids = self._connection.execute(
                    "SELECT run_id FROM production_authoring_runs "
                    "WHERE org_id=? ORDER BY run_id",
                    (org_id,),
                ).fetchall()
                result = tuple(
                    self._read_run(self._connection, org_id, row["run_id"])
                    for row in ids
                )
                self._connection.commit()
                return result
            except Exception:
                self._connection.rollback()
                raise

    def sources(self, org_id: str, run_id: str) -> tuple[AuthoringSourceRef, ...]:
        with self._lock:
            try:
                self._connection.execute("BEGIN")
                self._validate()
                run = self._read_run(self._connection, org_id, run_id)
                result = self._read_sources(self._connection, org_id, run.run_id)
                self._connection.commit()
                return result
            except Exception:
                self._connection.rollback()
                raise

    def get(self, org_id: str, run_id: str) -> AuthoringRun:
        with self._lock:
            try:
                self._connection.execute("BEGIN")
                self._validate()
                result = self._read_run(self._connection, org_id, run_id)
                self._connection.commit()
                return result
            except Exception:
                self._connection.rollback()
                raise

    def counts(self, org_id: str) -> dict[str, int]:
        with self._lock:
            try:
                self._connection.execute("BEGIN")
                self._validate()
                result = {
                    key: int(
                        self._connection.execute(
                            f"SELECT count(*) FROM {table} WHERE org_id=?", (org_id,)
                        ).fetchone()[0]
                    )
                    for key, table in {
                        "runs": "production_authoring_runs",
                        "sources": "production_authoring_source_refs",
                        "receipts": "production_authoring_command_receipts",
                        "audit": "production_authoring_audit_intents",
                        "outbox": "production_authoring_outbox_intents",
                    }.items()
                }
                self._connection.commit()
                return result
            except Exception:
                self._connection.rollback()
                raise


class StartAuthoringRunResult(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    run: ExtractingRun
    replayed: bool = False


class CompleteAuthoringRunResult(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    run: AwaitingOwnerReviewRun
    replayed: bool = False


class ReviewAuthoringRunResult(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    run: ReviewedRun
    replayed: bool = False


class BeginAuthoringRunPublishResult(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    run: PublishingRun
    replayed: bool = False


__all__ = [
    "AwaitingOwnerReviewRun",
    "AuthoringRun",
    "AuthoringRunCommand",
    "AuthoringRunResource",
    "AuthoringSourceRef",
    "BeginAuthoringRunPublishCommand",
    "BeginAuthoringRunPublishResult",
    "CompleteAuthoringRunCommand",
    "CompleteAuthoringRunResult",
    "ReviewAuthoringRunCommand",
    "ReviewAuthoringRunResult",
    "CurrentAuthoringRunAuthorization",
    "ExtractingRun",
    "ReviewedRun",
    "PublishingRun",
    "PublishedRun",
    "MAX_SOURCE_BYTES",
    "MAX_SOURCE_COUNT",
    "MAX_TOTAL_SOURCE_BYTES",
    "ProductionAuthoringRunConflict",
    "ProductionAuthoringRunDenied",
    "ProductionAuthoringRunError",
    "ProductionAuthoringRunUnavailable",
    "SqliteProductionAuthoringRuns",
    "StartAuthoringRunCommand",
    "StartAuthoringRunResult",
    "TxCurrentAuthoringAuthorizer",
]
