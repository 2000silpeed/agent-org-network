"""O5c durable, body-free-control Published Index acceptance UoW (ADR 0069)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3

from pydantic import BaseModel, ConfigDict, field_validator

from agent_org_network.knowledge_index import KnowledgeIndex
from agent_org_network.production_authoring_identity import AuthoringInvocation
from agent_org_network.sqlite_production_authoring_runs import (
    AuthoringRunResource,
    BeginAuthoringRunPublishCommand,
    ProductionAuthoringRunConflict,
    ProductionAuthoringRunDenied,
    PublishingRun,
    PublishedRun,
    SqliteProductionAuthoringRuns,
)
from agent_org_network import sqlite_production_authoring_runs as _authoring_schema


_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
FaultInjector = Callable[[str], None]


class PublishedIndexAcceptanceUnavailable(Exception):
    pass


class AcceptPublishedIndexCommand(BaseModel, frozen=True):
    """One exact O5c semantic publish acceptance; its index is a title-only payload."""

    model_config = ConfigDict(extra="forbid", strict=True)
    organization_id: str
    principal_id: str
    idempotency_key: str
    run_id: str
    expected_revision: int = 3
    expected_card_revision: int
    expected_card_digest: str
    commit_sha: str
    committed_tree_index_digest: str
    index: KnowledgeIndex

    @field_validator("organization_id", "principal_id", "idempotency_key", "run_id")
    @classmethod
    def _opaque(cls, value: str) -> str:
        if _OPAQUE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("expected_revision")
    @classmethod
    def _revision(cls, value: int) -> int:
        if value != 3:
            raise ValueError("Publishing revision 3 required")
        return value

    @field_validator("expected_card_revision")
    @classmethod
    def _card_revision(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("positive card revision required")
        return value

    @field_validator("expected_card_digest", "commit_sha", "committed_tree_index_digest")
    @classmethod
    def _card_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value


class PublishedIndexAcceptanceReceipt(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    receipt_id: str
    receipt_digest: str
    payload_digest: str
    commit_sha: str
    committed_tree_index_digest: str
    accepted_at: str

    @field_validator("receipt_digest", "payload_digest", "commit_sha", "committed_tree_index_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value


class AcceptPublishedIndexResult(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    run: PublishedRun
    receipt: PublishedIndexAcceptanceReceipt
    replayed: bool = False


class ReconcilePublishedIndexResult(BaseModel, frozen=True):
    """O5d's historical-proof-only control reconciliation outcome."""

    model_config = ConfigDict(extra="forbid", strict=True)
    run: PublishedRun
    reconciled: bool


_SCHEMA = """
CREATE TABLE published_index_payloads (
 org_id TEXT NOT NULL, agent_id TEXT NOT NULL, run_id TEXT NOT NULL,
 review_revision INTEGER NOT NULL CHECK(review_revision=2), payload_digest TEXT NOT NULL,
 payload_json TEXT NOT NULL, generated_at TEXT NOT NULL,
 PRIMARY KEY(org_id,agent_id,run_id,review_revision),
 FOREIGN KEY(org_id,run_id) REFERENCES production_authoring_runs(org_id,run_id)
);
CREATE TABLE published_index_acceptance_receipts (
 receipt_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, agent_id TEXT NOT NULL, run_id TEXT NOT NULL,
 review_revision INTEGER NOT NULL CHECK(review_revision=2), idempotency_key TEXT NOT NULL,
 command_digest TEXT NOT NULL, payload_digest TEXT NOT NULL,
 commit_sha TEXT NOT NULL, committed_tree_index_digest TEXT NOT NULL, receipt_digest TEXT NOT NULL,
 policy_version TEXT NOT NULL, policy_digest TEXT NOT NULL, grant_evidence_digest TEXT NOT NULL,
 identity_session_digest TEXT NOT NULL, identity_evidence_digest TEXT NOT NULL,
 resource_fingerprint TEXT NOT NULL, accepted_at TEXT NOT NULL,
 UNIQUE(org_id,idempotency_key), UNIQUE(org_id,agent_id,run_id,review_revision),
 FOREIGN KEY(org_id,agent_id,run_id,review_revision) REFERENCES published_index_payloads(org_id,agent_id,run_id,review_revision)
);
CREATE TABLE published_index_latest_events (
 org_id TEXT NOT NULL, agent_id TEXT NOT NULL, receipt_id TEXT NOT NULL,
 payload_digest TEXT NOT NULL, generated_at TEXT NOT NULL, accepted_at TEXT NOT NULL,
 PRIMARY KEY(org_id,agent_id,receipt_id),
 FOREIGN KEY(receipt_id) REFERENCES published_index_acceptance_receipts(receipt_id)
);
CREATE TRIGGER published_index_payloads_immutable BEFORE UPDATE ON published_index_payloads BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER published_index_payloads_no_delete BEFORE DELETE ON published_index_payloads BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER published_index_receipts_immutable BEFORE UPDATE ON published_index_acceptance_receipts BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER published_index_receipts_no_delete BEFORE DELETE ON published_index_acceptance_receipts BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER published_index_latest_immutable BEFORE UPDATE ON published_index_latest_events BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER published_index_latest_no_delete BEFORE DELETE ON published_index_latest_events BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER published_index_acceptance_binds_published_run
BEFORE UPDATE OF stage, revision, acceptance_receipt_id, acceptance_receipt_digest, published_at
ON production_authoring_runs
WHEN OLD.stage='publishing' AND OLD.revision=3
 AND NEW.stage='published' AND NEW.revision=4
BEGIN
 SELECT CASE WHEN NOT EXISTS (
  SELECT 1
  FROM published_index_acceptance_receipts AS receipt
  JOIN published_index_payloads AS payload
   ON payload.org_id=receipt.org_id AND payload.agent_id=receipt.agent_id
  AND payload.run_id=receipt.run_id AND payload.review_revision=receipt.review_revision
  JOIN published_index_latest_events AS event ON event.receipt_id=receipt.receipt_id
  JOIN production_authoring_command_receipts AS control
   ON control.org_id=receipt.org_id AND control.run_id=receipt.run_id
  WHERE receipt.receipt_id=NEW.acceptance_receipt_id
   AND receipt.receipt_digest=NEW.acceptance_receipt_digest
   AND receipt.org_id=NEW.org_id AND receipt.agent_id=NEW.agent_id
   AND receipt.run_id=NEW.run_id AND receipt.review_revision=2
   AND payload.payload_digest=receipt.payload_digest
   AND event.org_id=receipt.org_id AND event.agent_id=receipt.agent_id
   AND event.payload_digest=receipt.payload_digest AND event.generated_at=payload.generated_at
   AND control.action_kind='authoring_run.publish_accept'
   AND control.result_revision=4 AND control.result_state='published'
   AND control.command_digest=receipt.command_digest
 ) THEN RAISE(ABORT,'published acceptance binding required') END;
END;
"""


_ACCEPTANCE_TABLES = (
    "published_index_payloads",
    "published_index_acceptance_receipts",
    "published_index_latest_events",
)
_ACCEPTANCE_TRIGGERS = (
    "published_index_payloads_immutable",
    "published_index_payloads_no_delete",
    "published_index_receipts_immutable",
    "published_index_receipts_no_delete",
    "published_index_latest_immutable",
    "published_index_latest_no_delete",
    "published_index_acceptance_binds_published_run",
)


def _normalized_sql(value: str | None) -> str:
    return " ".join((value or "").split())


def _acceptance_catalog(connection: sqlite3.Connection) -> tuple[object, ...]:
    """The complete O5 acceptance schema contract, including its base-table binding."""
    catalog: list[object] = []
    for table in _ACCEPTANCE_TABLES:
        catalog.append(
            (
                table,
                tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info('{table}')")),
                tuple(tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list('{table}')")),
                tuple(tuple(row) for row in connection.execute(f"PRAGMA index_list('{table}')")),
            )
        )
        for index in connection.execute(f"PRAGMA index_list('{table}')"):
            catalog.append(
                (str(index[1]), tuple(tuple(row) for row in connection.execute(f"PRAGMA index_info('{index[1]}')")))
            )
    for trigger in _ACCEPTANCE_TRIGGERS:
        row = connection.execute(
            "SELECT tbl_name,sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
        ).fetchone()
        catalog.append((trigger, None if row is None else str(row[0]), None if row is None else _normalized_sql(str(row[1]))))
    return tuple(catalog)


def _canonical_acceptance_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_authoring_schema._SCHEMA)  # pyright: ignore[reportPrivateUsage]
        connection.executescript(_SCHEMA)
        return _acceptance_catalog(connection)
    finally:
        connection.close()


_CANONICAL_ACCEPTANCE_CATALOG = _canonical_acceptance_catalog()


def _validate_acceptance_schema(connection: sqlite3.Connection) -> None:
    if _acceptance_catalog(connection) != _CANONICAL_ACCEPTANCE_CATALOG:
        raise PublishedIndexAcceptanceUnavailable()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return sha256(_json(value).encode()).hexdigest()


def _no_fault(_point: str) -> None:
    return None


class SqlitePublishedIndexAcceptance:
    """Shares the AuthoringRun connection so all O5c writes have one commit point."""

    @classmethod
    def migrate(cls, path: str | Path) -> None:
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='published_index_payloads'").fetchone():
                raise PublishedIndexAcceptanceUnavailable()
            connection.executescript(_SCHEMA)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def __init__(self, runs: SqliteProductionAuthoringRuns, *, fault_injector: FaultInjector | None = None) -> None:
        if type(runs) is not SqliteProductionAuthoringRuns:
            raise PublishedIndexAcceptanceUnavailable()
        self._runs = runs
        self._fault: FaultInjector = fault_injector or _no_fault
        required = {"published_index_payloads", "published_index_acceptance_receipts", "published_index_latest_events"}
        actual = {str(row[0]) for row in runs._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}  # pyright: ignore[reportPrivateUsage]
        if not required <= actual:
            raise PublishedIndexAcceptanceUnavailable()

    @staticmethod
    def _command_digest(command: AcceptPublishedIndexCommand) -> str:
        return _digest(command.model_dump(mode="json"))

    @staticmethod
    def _payload(command: AcceptPublishedIndexCommand) -> tuple[str, str, str]:
        value = command.index.model_dump(mode="json")
        encoded = _json(value)
        generated_at = command.index.generated_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return encoded, sha256(encoded.encode()).hexdigest(), generated_at

    def accept(self, command: AcceptPublishedIndexCommand, *, invocation: AuthoringInvocation) -> AcceptPublishedIndexResult:
        if type(command) is not AcceptPublishedIndexCommand or type(invocation) is not AuthoringInvocation:
            raise PublishedIndexAcceptanceUnavailable()
        if command.index.generated_at.utcoffset() is None:
            raise PublishedIndexAcceptanceUnavailable()
        if command.index.agent_id == "" or command.index.agent_id != command.index.agent_id:
            raise PublishedIndexAcceptanceUnavailable()
        with self._runs._lock:  # pyright: ignore[reportPrivateUsage]
            tx = self._runs._connection  # pyright: ignore[reportPrivateUsage]
            try:
                tx.execute("BEGIN IMMEDIATE")
                self._runs._validate()  # pyright: ignore[reportPrivateUsage]
                run = self._runs._read_run(tx, command.organization_id, command.run_id)  # pyright: ignore[reportPrivateUsage]
                if not isinstance(run, (PublishingRun, PublishedRun)) or run.outcome != "Approved" or run.agent_id != command.index.agent_id or run.card_revision != command.expected_card_revision or run.card_digest != command.expected_card_digest or command.principal_id != run.owner_id:
                    raise ProductionAuthoringRunConflict()
                resource = AuthoringRunResource(org_id=run.org_id, agent_id=run.agent_id, owner_id=run.owner_id, card_revision=run.card_revision, card_digest=run.card_digest)
                authorization_command = BeginAuthoringRunPublishCommand(organization_id=command.organization_id, principal_id=command.principal_id, idempotency_key=command.idempotency_key, run_id=command.run_id, expected_card_revision=command.expected_card_revision, expected_card_digest=command.expected_card_digest)
                evidence = self._runs._authorize.current(authorization_command, resource, run.source_set_digest, invocation, tx)  # pyright: ignore[reportPrivateUsage]
                encoded, payload_digest, generated_at = self._payload(command)
                command_digest = self._command_digest(command)
                old = tx.execute("SELECT * FROM published_index_acceptance_receipts WHERE org_id=? AND idempotency_key=?", (command.organization_id, command.idempotency_key)).fetchone()
                if old is not None:
                    if old["command_digest"] != command_digest or old["agent_id"] != run.agent_id or old["run_id"] != run.run_id or not self._runs._authorize.verify_precommit(authorization_command, resource, run.source_set_digest, evidence, invocation, tx):  # pyright: ignore[reportPrivateUsage]
                        raise ProductionAuthoringRunConflict()
                    published = self._runs._read_run(tx, command.organization_id, command.run_id)  # pyright: ignore[reportPrivateUsage]
                    if not isinstance(published, PublishedRun):
                        raise PublishedIndexAcceptanceUnavailable()
                    tx.commit()
                    return AcceptPublishedIndexResult(run=published, receipt=PublishedIndexAcceptanceReceipt(receipt_id=old["receipt_id"], receipt_digest=old["receipt_digest"], payload_digest=old["payload_digest"], commit_sha=old["commit_sha"], committed_tree_index_digest=old["committed_tree_index_digest"], accepted_at=old["accepted_at"]), replayed=True)
                if isinstance(run, PublishedRun):
                    raise ProductionAuthoringRunConflict()
                bound = tx.execute("SELECT payload_digest FROM published_index_payloads WHERE org_id=? AND agent_id=? AND run_id=? AND review_revision=2", (run.org_id, run.agent_id, run.run_id)).fetchone()
                if bound is not None:
                    raise ProductionAuthoringRunConflict()
                latest = tx.execute(
                    "SELECT generated_at FROM published_index_latest_events "
                    "WHERE org_id=? AND agent_id=? "
                    "ORDER BY generated_at DESC, accepted_at DESC, receipt_id DESC LIMIT 1",
                    (run.org_id, run.agent_id),
                ).fetchone()
                if latest is not None and generated_at <= latest["generated_at"]:
                    raise ProductionAuthoringRunConflict()
                if not self._runs._authorize.verify_precommit(authorization_command, resource, run.source_set_digest, evidence, invocation, tx):  # pyright: ignore[reportPrivateUsage]
                    raise ProductionAuthoringRunDenied()
                accepted_at = str(tx.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0])
                receipt_id = "pia_" + _digest({"org_id": run.org_id, "agent_id": run.agent_id, "run_id": run.run_id, "review_revision": 2})[:48]
                receipt_digest = _digest({"receipt_id": receipt_id, "command_digest": command_digest, "payload_digest": payload_digest, "commit_sha": command.commit_sha, "committed_tree_index_digest": command.committed_tree_index_digest, "accepted_at": accepted_at})
                tx.execute("INSERT INTO published_index_payloads VALUES (?,?,?,?,?,?,?)", (run.org_id, run.agent_id, run.run_id, 2, payload_digest, encoded, generated_at))
                self._fault("after_payload")
                tx.execute("INSERT INTO published_index_acceptance_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (receipt_id, run.org_id, run.agent_id, run.run_id, 2, command.idempotency_key, command_digest, payload_digest, command.commit_sha, command.committed_tree_index_digest, receipt_digest, evidence.policy_version, evidence.policy_digest, evidence.grant_evidence_digest, evidence.identity_session_digest, evidence.identity_evidence_digest, evidence.resource_fingerprint, accepted_at))
                tx.execute("INSERT INTO published_index_latest_events VALUES (?,?,?,?,?,?)", (run.org_id, run.agent_id, receipt_id, payload_digest, generated_at, accepted_at))
                self._fault("after_acceptance_receipt")
                control_key = "pac_" + _digest({"org_id": run.org_id, "run_id": run.run_id})[:48]
                tx.execute("INSERT INTO production_authoring_command_receipts VALUES (?,?,?,'authoring_run.publish_accept',?,4,'published',?,?,?,?,?,?,?)", (run.org_id, control_key, command_digest, run.run_id, evidence.policy_version, evidence.policy_digest, evidence.grant_evidence_digest, evidence.identity_session_digest, evidence.identity_evidence_digest, evidence.resource_fingerprint, accepted_at))
                changed = tx.execute("UPDATE production_authoring_runs SET stage='published',revision=4,acceptance_receipt_id=?,acceptance_receipt_digest=?,published_at=? WHERE org_id=? AND run_id=? AND stage='publishing' AND revision=3", (receipt_id, receipt_digest, accepted_at, run.org_id, run.run_id)).rowcount
                if changed != 1:
                    raise ProductionAuthoringRunConflict()
                tx.execute("INSERT INTO production_authoring_audit_intents (org_id,action,event_kind,principal_id,run_id,agent_id,source_set_digest,source_count,total_bytes,command_digest,policy_version,policy_digest,grant_evidence_digest,identity_session_digest,identity_evidence_digest,resource_fingerprint,result_revision,admitted_bundle_digest,document_count,edge_count,dropped_count,author_profile_digest,review_outcome,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run.org_id, 'author.publish', 'run_published', command.principal_id, run.run_id, run.agent_id, run.source_set_digest, run.source_count, run.total_bytes, command_digest, evidence.policy_version, evidence.policy_digest, evidence.grant_evidence_digest, evidence.identity_session_digest, evidence.identity_evidence_digest, evidence.resource_fingerprint, 4, run.admitted_bundle_digest, run.document_count, run.edge_count, run.dropped_count, run.author_profile_digest, run.outcome, accepted_at))
                tx.execute("INSERT INTO production_authoring_outbox_intents (org_id,kind,run_id,agent_id,source_set_digest,source_count,total_bytes,command_digest,resource_fingerprint,result_revision,admitted_bundle_digest,document_count,edge_count,dropped_count,author_profile_digest,review_outcome,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run.org_id, 'authoring.run_published', run.run_id, run.agent_id, run.source_set_digest, run.source_count, run.total_bytes, command_digest, evidence.resource_fingerprint, 4, run.admitted_bundle_digest, run.document_count, run.edge_count, run.dropped_count, run.author_profile_digest, run.outcome, accepted_at))
                self._fault("before_commit")
                result = PublishedRun.model_validate(run.model_dump(mode="python") | {"stage": "Published", "revision": 4, "acceptance_receipt_id": receipt_id, "acceptance_receipt_digest": receipt_digest, "published_at": accepted_at})
                tx.commit()
                return AcceptPublishedIndexResult(run=result, receipt=PublishedIndexAcceptanceReceipt(receipt_id=receipt_id, receipt_digest=receipt_digest, payload_digest=payload_digest, commit_sha=command.commit_sha, committed_tree_index_digest=command.committed_tree_index_digest, accepted_at=accepted_at))
            except Exception:
                tx.rollback()
                raise

    @staticmethod
    def _historical_publishing_run(tx: sqlite3.Connection, org_id: str, run_id: str) -> PublishingRun:
        """Read only the sealed O5 control row; deliberately avoids current Card/Owner lookup."""
        row = tx.execute(
            "SELECT * FROM production_authoring_runs WHERE org_id=? AND run_id=?",
            (org_id, run_id),
        ).fetchone()
        if row is None or row["stage"] != "publishing" or int(row["revision"]) != 3:
            raise PublishedIndexAcceptanceUnavailable()
        try:
            value = {
                name: row["review_outcome"] if name == "outcome" else row[name]
                for name in PublishingRun.model_fields
            }
            value["stage"] = "Publishing"
            return PublishingRun.model_validate(value)
        except Exception as error:
            raise PublishedIndexAcceptanceUnavailable() from error

    @staticmethod
    def _historical_published_run(tx: sqlite3.Connection, org_id: str, run_id: str) -> PublishedRun:
        row = tx.execute(
            "SELECT * FROM production_authoring_runs WHERE org_id=? AND run_id=?",
            (org_id, run_id),
        ).fetchone()
        if row is None or row["stage"] != "published" or int(row["revision"]) != 4:
            raise PublishedIndexAcceptanceUnavailable()
        try:
            value = {
                name: row["review_outcome"] if name == "outcome" else row[name]
                for name in PublishedRun.model_fields
            }
            value["stage"] = "Published"
            return PublishedRun.model_validate(value)
        except Exception as error:
            raise PublishedIndexAcceptanceUnavailable() from error

    @staticmethod
    def _historical_receipt(tx: sqlite3.Connection, run: PublishingRun) -> sqlite3.Row:
        receipts = tx.execute(
            "SELECT * FROM published_index_acceptance_receipts "
            "WHERE org_id=? AND agent_id=? AND run_id=? AND review_revision=2",
            (run.org_id, run.agent_id, run.run_id),
        ).fetchall()
        if len(receipts) != 1:
            raise PublishedIndexAcceptanceUnavailable()
        return receipts[0]

    @staticmethod
    def _published_from_historical(run: PublishingRun, receipt: sqlite3.Row) -> PublishedRun:
        try:
            return PublishedRun.model_validate(
                run.model_dump(mode="python")
                | {
                    "stage": "Published", "revision": 4,
                    "acceptance_receipt_id": receipt["receipt_id"],
                    "acceptance_receipt_digest": receipt["receipt_digest"],
                    "published_at": receipt["accepted_at"],
                }
            )
        except Exception as error:
            raise PublishedIndexAcceptanceUnavailable() from error

    def reconcile(self, *, organization_id: str, run_id: str) -> ReconcilePublishedIndexResult:
        """CAS a pending O5 control projection from an already exact immutable graph.

        This intentionally has no invocation or authorization argument: ADR 0069 permits
        it after Owner/Card drift only because it creates no acceptance-side evidence.
        """
        if _OPAQUE.fullmatch(organization_id) is None or _OPAQUE.fullmatch(run_id) is None:
            raise PublishedIndexAcceptanceUnavailable()
        with self._runs._lock:  # pyright: ignore[reportPrivateUsage]
            tx = self._runs._connection  # pyright: ignore[reportPrivateUsage]
            try:
                tx.execute("BEGIN IMMEDIATE")
                # O5d must never use a damaged schema as historical proof.  This
                # precedes both the Published read-only path and its only CAS.
                _validate_acceptance_schema(tx)
                existing = tx.execute(
                    "SELECT stage,revision FROM production_authoring_runs WHERE org_id=? AND run_id=?",
                    (organization_id, run_id),
                ).fetchone()
                if existing is None:
                    raise PublishedIndexAcceptanceUnavailable()
                if existing["stage"] == "published" and int(existing["revision"]) == 4:
                    published = self._historical_published_run(tx, organization_id, run_id)
                    try:
                        SqliteProductionAuthoringRuns._verify_published_acceptance(tx, published)  # pyright: ignore[reportPrivateUsage]
                    except Exception as error:
                        raise PublishedIndexAcceptanceUnavailable() from error
                    tx.commit()
                    return ReconcilePublishedIndexResult(run=published, reconciled=False)
                run = self._historical_publishing_run(tx, organization_id, run_id)
                receipt = self._historical_receipt(tx, run)
                published = self._published_from_historical(run, receipt)
                # Reuse the Published reader's graph verifier before the sole O5d write.
                try:
                    SqliteProductionAuthoringRuns._verify_published_acceptance(tx, published)  # pyright: ignore[reportPrivateUsage]
                except Exception as error:
                    raise PublishedIndexAcceptanceUnavailable() from error
                changed = tx.execute(
                    "UPDATE production_authoring_runs SET stage='published',revision=4,"
                    "acceptance_receipt_id=?,acceptance_receipt_digest=?,published_at=? "
                    "WHERE org_id=? AND run_id=? AND stage='publishing' AND revision=3",
                    (
                        published.acceptance_receipt_id, published.acceptance_receipt_digest,
                        published.published_at, run.org_id, run.run_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise ProductionAuthoringRunConflict()
                self._fault("before_reconciliation_commit")
                tx.commit()
                return ReconcilePublishedIndexResult(run=published, reconciled=True)
            except Exception:
                tx.rollback()
                raise


__all__ = ["AcceptPublishedIndexCommand", "AcceptPublishedIndexResult", "PublishedIndexAcceptanceReceipt", "PublishedIndexAcceptanceUnavailable", "ReconcilePublishedIndexResult", "SqlitePublishedIndexAcceptance"]
