"""Canonical AuthoringRun resource calculations and current-row validation."""

from __future__ import annotations

from hashlib import sha256
import json
import re
import sqlite3
from collections.abc import Mapping


class ProductionAuthoringResourceUnavailable(Exception):
    pass


def validate_authoring_catalog(connection: sqlite3.Connection) -> None:
    """Validate the one canonical AuthoringRun schema owned by the UoW module."""
    before = connection.total_changes
    try:
        from agent_org_network import sqlite_production_authoring_runs as schema

        if schema._catalog(connection) != schema._CANONICAL_CATALOG:  # pyright: ignore[reportPrivateUsage]
            raise ProductionAuthoringResourceUnavailable()
    except ProductionAuthoringResourceUnavailable:
        raise
    except Exception as error:
        raise ProductionAuthoringResourceUnavailable() from error
    finally:
        if connection.total_changes != before:
            raise ProductionAuthoringResourceUnavailable()


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_digest(value: object) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()


def _exact_int(value: object) -> int:
    if type(value) is not int:
        raise ProductionAuthoringResourceUnavailable()
    return value


def _sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _opaque(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is not None
    )


def _timestamp(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value)
        is not None
    )


def canonical_start_run_id(org_id: str, agent_id: str, idempotency_key: str) -> str:
    return "ar_" + canonical_digest(
        {"org_id": org_id, "agent_id": agent_id, "idempotency_key": idempotency_key}
    )


def canonical_source_set_digest(sources: tuple[Mapping[str, object], ...]) -> str:
    return canonical_digest(list(sources))


def canonical_resource_fingerprint(
    *,
    org_id: str,
    agent_id: str,
    owner_id: str,
    card_revision: int,
    card_digest: str,
    run_id: str,
    source_set_digest: str,
) -> str:
    return canonical_digest(
        {
            "org_id": org_id,
            "agent_id": agent_id,
            "owner_id": owner_id,
            "card_revision": card_revision,
            "card_digest": card_digest,
            "run_id": run_id,
            "source_set_digest": source_set_digest,
        }
    )


def canonical_completion_resource_fingerprint(
    *,
    org_id: str,
    run_id: str,
    agent_id: str,
    owner_id: str,
    card_revision: int,
    card_digest: str,
    source_set_digest: str,
    admitted_bundle_digest: str,
    document_count: int,
    edge_count: int,
    dropped_count: int,
    author_profile_digest: str,
) -> str:
    return canonical_digest(
        {
            "org_id": org_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "owner_id": owner_id,
            "card_revision": card_revision,
            "card_digest": card_digest,
            "source_set_digest": source_set_digest,
            "admitted_bundle_digest": admitted_bundle_digest,
            "document_count": document_count,
            "edge_count": edge_count,
            "dropped_count": dropped_count,
            "author_profile_digest": author_profile_digest,
        }
    )


def _validate_review_anchors(
    connection: sqlite3.Connection,
    *,
    columns: Mapping[str, tuple[str, ...]],
    values: Mapping[str, object],
    org_id: str,
    run_id: str,
    agent_id: str,
    owner_id: str,
    card_revision: int,
    card_digest: str,
    source_set_digest: str,
    completion: Mapping[str, object],
    allow_unanchored: bool,
) -> None:
    """Require the one exact O4 review companion graph for Reviewed/Publishing."""
    receipts = connection.execute(
        "SELECT * FROM production_authoring_command_receipts "
        "WHERE org_id=? AND run_id=? AND action_kind='authoring_run.review'",
        (org_id, run_id),
    ).fetchall()
    audits = connection.execute(
        "SELECT * FROM production_authoring_audit_intents "
        "WHERE org_id=? AND run_id=? AND event_kind='run_reviewed'",
        (org_id, run_id),
    ).fetchall()
    events = connection.execute(
        "SELECT * FROM production_authoring_outbox_intents "
        "WHERE org_id=? AND run_id=? AND kind='authoring.run_reviewed'",
        (org_id, run_id),
    ).fetchall()
    if allow_unanchored and not receipts and not audits and not events:
        return
    if len(receipts) != 1 or len(audits) != 1 or len(events) != 1:
        raise ProductionAuthoringResourceUnavailable()
    receipt = dict(zip(columns["receipt"], receipts[0], strict=True))
    audit = dict(zip(columns["audit"], audits[0], strict=True))
    event = dict(zip(columns["outbox"], events[0], strict=True))
    outcome = values["review_outcome"]
    reviewed_at = values["reviewed_at"]
    if outcome not in {"Approved", "Edited", "Rejected"} or not _timestamp(reviewed_at):
        raise ProductionAuthoringResourceUnavailable()
    command_digest = canonical_digest(
        {
            "organization_id": org_id,
            "principal_id": owner_id,
            "idempotency_key": receipt["idempotency_key"],
            "run_id": run_id,
            "expected_revision": 1,
            "expected_card_revision": card_revision,
            "expected_card_digest": card_digest,
            "concept_id": "bundle",
            "source_digest": source_set_digest,
            "draft_digest": completion["admitted_bundle_digest"],
            "outcome": outcome,
        }
    )
    fingerprint = canonical_digest(
        {
            "org_id": org_id,
            "agent_id": agent_id,
            "owner_id": owner_id,
            "card_revision": card_revision,
            "card_digest": card_digest,
            "run_id": run_id,
            "source_set_digest": source_set_digest,
            "command_digest": command_digest,
            "source_digest": source_set_digest,
            "draft_digest": completion["admitted_bundle_digest"],
            "outcome": outcome,
        }
    )
    common_names = (
        "org_id", "run_id", "agent_id", "source_set_digest", "source_count",
        "total_bytes", "command_digest", "resource_fingerprint",
    )
    common = (
        org_id, run_id, agent_id, source_set_digest, _exact_int(values["source_count"]),
        _exact_int(values["total_bytes"]), command_digest, fingerprint,
    )
    if (
        not _opaque(receipt["idempotency_key"])
        or not all(_sha(receipt[name]) for name in (
            "command_digest", "policy_digest", "grant_evidence_digest",
            "identity_session_digest", "identity_evidence_digest", "resource_fingerprint",
        ))
        or not _opaque(receipt["policy_version"])
        or not _timestamp(receipt["created_at"])
        or receipt["command_digest"] != command_digest
        or receipt["result_state"] != "reviewed"
        or int(receipt["result_revision"]) != 2
        or receipt["resource_fingerprint"] != fingerprint
        or tuple(audit[name] for name in common_names) != common
        or tuple(event[name] for name in common_names) != common
        or audit["action"] != "author.publish"
        or audit["principal_id"] != owner_id
        or int(audit["result_revision"]) != 2
        or int(event["result_revision"]) != 2
        or audit["review_outcome"] != outcome
        or event["review_outcome"] != outcome
        or any(
            audit[name] != completion[name] or event[name] != completion[name]
            for name in (
                "admitted_bundle_digest", "document_count", "edge_count", "dropped_count",
                "author_profile_digest",
            )
        )
        or receipt["created_at"] != reviewed_at
        or audit["created_at"] != reviewed_at
        or event["created_at"] != reviewed_at
        or int(event["delivered"]) != 0
        or receipt["policy_version"] != audit["policy_version"]
        or receipt["policy_digest"] != audit["policy_digest"]
        or receipt["grant_evidence_digest"] != audit["grant_evidence_digest"]
        or receipt["identity_session_digest"] != audit["identity_session_digest"]
        or receipt["identity_evidence_digest"] != audit["identity_evidence_digest"]
    ):
        raise ProductionAuthoringResourceUnavailable()


def validate_current_authoring_resource(
    connection: sqlite3.Connection,
    *,
    org_id: str,
    run_id: str,
    agent_id: str,
    owner_id: str,
    card_revision: int,
    card_digest: str,
    source_set_digest: str,
    allow_absent: bool,
    expected_stage: str | None,
    idempotency_key: str | None = None,
    allow_unanchored: bool = False,
    completion: Mapping[str, object] | None = None,
    require_completion_anchors: bool = True,
) -> None:
    """Validate absent-new or the exact durable run/source/start-anchor graph."""
    before = connection.total_changes
    try:
        validate_authoring_catalog(connection)
        row = connection.execute(
            "SELECT * FROM production_authoring_runs WHERE org_id=? AND run_id=?",
            (org_id, run_id),
        ).fetchone()
        if row is None:
            if not allow_absent:
                raise ProductionAuthoringResourceUnavailable()
            if connection.execute(
                "SELECT 1 FROM production_authoring_source_refs "
                "WHERE org_id=? AND run_id=?",
                (org_id, run_id),
            ).fetchone():
                raise ProductionAuthoringResourceUnavailable()
            for table in (
                "production_authoring_command_receipts",
                "production_authoring_audit_intents",
                "production_authoring_outbox_intents",
            ):
                if connection.execute(
                    f"SELECT 1 FROM {table} WHERE org_id=? AND run_id=?",  # noqa: S608
                    (org_id, run_id),
                ).fetchone():
                    raise ProductionAuthoringResourceUnavailable()
            if idempotency_key is not None and connection.execute(
                "SELECT 1 FROM production_authoring_command_receipts "
                "WHERE org_id=? AND idempotency_key=?",
                (org_id, idempotency_key),
            ).fetchone():
                raise ProductionAuthoringResourceUnavailable()
            return
        names = tuple(description[0] for description in connection.execute(
            "SELECT * FROM production_authoring_runs LIMIT 0"
        ).description or ())
        values = dict(zip(names, tuple(row), strict=True))
        completion_names = (
            "admitted_bundle_digest",
            "document_count",
            "edge_count",
            "dropped_count",
            "author_profile_digest",
        )

        def columns(table: str) -> tuple[str, ...]:
            return tuple(
                str(item[1])
                for item in connection.execute(f"PRAGMA table_info('{table}')")
            )

        if (
            expected_stage is None
            or values["stage"] != expected_stage
            or values["org_id"] != org_id
            or values["run_id"] != run_id
            or values["agent_id"] != agent_id
            or values["owner_id"] != owner_id
            or int(values["card_revision"]) != card_revision
            or values["card_digest"] != card_digest
            or values["source_set_digest"] != source_set_digest
        ):
            raise ProductionAuthoringResourceUnavailable()
        if expected_stage == "awaiting_owner_review" and require_completion_anchors:
            assert completion is not None
            completed_receipts = connection.execute(
                "SELECT * FROM production_authoring_command_receipts "
                "WHERE org_id=? AND run_id=? AND action_kind='authoring_run.complete'",
                (org_id, run_id),
            ).fetchall()
            completed_audits = connection.execute(
                "SELECT * FROM production_authoring_audit_intents "
                "WHERE org_id=? AND run_id=? AND event_kind='run_completed'",
                (org_id, run_id),
            ).fetchall()
            completed_events = connection.execute(
                "SELECT * FROM production_authoring_outbox_intents "
                "WHERE org_id=? AND run_id=? AND kind='authoring.run_completed'",
                (org_id, run_id),
            ).fetchall()
            if (
                len(completed_receipts) != 1
                or len(completed_audits) != 1
                or len(completed_events) != 1
            ):
                raise ProductionAuthoringResourceUnavailable()
            completed_receipt = dict(
                zip(
                    columns("production_authoring_command_receipts"),
                    completed_receipts[0],
                    strict=True,
                )
            )
            completed_audit = dict(
                zip(
                    columns("production_authoring_audit_intents"),
                    completed_audits[0],
                    strict=True,
                )
            )
            completed_event = dict(
                zip(
                    columns("production_authoring_outbox_intents"),
                    completed_events[0],
                    strict=True,
                )
            )
            complete_fingerprint = canonical_completion_resource_fingerprint(
                org_id=org_id, run_id=run_id, agent_id=agent_id, owner_id=owner_id,
                card_revision=card_revision, card_digest=card_digest,
                source_set_digest=source_set_digest,
                admitted_bundle_digest=str(completion["admitted_bundle_digest"]),
                document_count=_exact_int(completion["document_count"]),
                edge_count=_exact_int(completion["edge_count"]),
                dropped_count=_exact_int(completion["dropped_count"]),
                author_profile_digest=str(completion["author_profile_digest"]),
            )
            if (
                not _sha(completed_receipt["command_digest"])
                or not _opaque(completed_receipt["idempotency_key"])
                or not _opaque(completed_receipt["policy_version"])
                or not _sha(completed_receipt["policy_digest"])
                or not _sha(completed_receipt["grant_evidence_digest"])
                or not _sha(completed_receipt["identity_session_digest"])
                or not _sha(completed_receipt["identity_evidence_digest"])
                or not _sha(completed_receipt["resource_fingerprint"])
                or not _timestamp(completed_receipt["created_at"])
                or completed_receipt["resource_fingerprint"] != complete_fingerprint
                or completed_receipt["result_state"] != "awaiting_owner_review"
                or int(completed_receipt["result_revision"]) != 1
                or completed_audit["command_digest"]
                != completed_receipt["command_digest"]
                or completed_event["command_digest"]
                != completed_receipt["command_digest"]
                or completed_audit["resource_fingerprint"] != complete_fingerprint
                or completed_event["resource_fingerprint"] != complete_fingerprint
                or completed_audit["action"] != "author.write"
                or completed_audit["principal_id"] != owner_id
                or int(completed_audit["result_revision"]) != 1
                or int(completed_event["result_revision"]) != 1
                or completed_receipt["policy_version"]
                != completed_audit["policy_version"]
                or completed_receipt["policy_digest"]
                != completed_audit["policy_digest"]
                or completed_receipt["grant_evidence_digest"]
                != completed_audit["grant_evidence_digest"]
                or completed_receipt["identity_session_digest"]
                != completed_audit["identity_session_digest"]
                or completed_receipt["identity_evidence_digest"]
                != completed_audit["identity_evidence_digest"]
                or completed_receipt["created_at"] != values["completed_at"]
                or completed_audit["created_at"] != values["completed_at"]
                or completed_event["created_at"] != values["completed_at"]
                or int(completed_event["delivered"]) != 0
                or any(
                    completed_audit[name] != completion[name]
                    or completed_event[name] != completion[name]
                    for name in completion_names
                )
            ):
                raise ProductionAuthoringResourceUnavailable()
        if expected_stage == "extracting":
            if int(values["revision"]) != 0 or any(
                values[name] is not None for name in completion_names
            ) or values["completed_at"] is not None:
                raise ProductionAuthoringResourceUnavailable()
        elif expected_stage in {"awaiting_owner_review", "reviewed", "publishing", "published"}:
            if (
                int(values["revision"]) != (
                    1 if expected_stage == "awaiting_owner_review" else (
                        2 if expected_stage == "reviewed" else (3 if expected_stage == "publishing" else 4)
                    )
                )
                or completion is None
                or any(values[name] != completion[name] for name in completion_names)
                or values["completed_at"] is None
            ):
                raise ProductionAuthoringResourceUnavailable()
            if expected_stage in {"reviewed", "publishing", "published"}:
                _validate_review_anchors(
                    connection,
                    columns={
                        "receipt": columns("production_authoring_command_receipts"),
                        "audit": columns("production_authoring_audit_intents"),
                        "outbox": columns("production_authoring_outbox_intents"),
                    },
                    values=values,
                    org_id=org_id,
                    run_id=run_id,
                    agent_id=agent_id,
                    owner_id=owner_id,
                    card_revision=card_revision,
                    card_digest=card_digest,
                    source_set_digest=source_set_digest,
                    completion=completion,
                    allow_unanchored=allow_unanchored,
                )
        sources = tuple(
            {
                "source_digest": str(source[0]),
                "byte_size": int(source[1]),
                "media_type": str(source[2]),
            }
            for source in connection.execute(
                "SELECT source_digest,byte_size,media_type "
                "FROM production_authoring_source_refs WHERE org_id=? AND run_id=? "
                "ORDER BY source_digest",
                (org_id, run_id),
            )
        )
        if (
            canonical_source_set_digest(sources) != source_set_digest
            or len(sources) != int(values["source_count"])
            or sum(int(source["byte_size"]) for source in sources)
            != int(values["total_bytes"])
        ):
            raise ProductionAuthoringResourceUnavailable()
        fingerprint = canonical_resource_fingerprint(
            org_id=org_id,
            agent_id=agent_id,
            owner_id=owner_id,
            card_revision=card_revision,
            card_digest=card_digest,
            run_id=run_id,
            source_set_digest=source_set_digest,
        )
        starts = connection.execute(
            "SELECT * FROM production_authoring_command_receipts "
            "WHERE org_id=? AND run_id=? AND action_kind='start'",
            (org_id, run_id),
        ).fetchall()
        audits = connection.execute(
            "SELECT * FROM production_authoring_audit_intents "
            "WHERE org_id=? AND run_id=? AND event_kind='run_started'",
            (org_id, run_id),
        ).fetchall()
        outbox = connection.execute(
            "SELECT * FROM production_authoring_outbox_intents "
            "WHERE org_id=? AND run_id=? AND kind='authoring.run_started'",
            (org_id, run_id),
        ).fetchall()
        if allow_unanchored and not starts and not audits and not outbox:
            return
        if len(starts) != 1 or len(audits) != 1 or len(outbox) != 1:
            raise ProductionAuthoringResourceUnavailable()
        receipt, audit, event = starts[0], audits[0], outbox[0]
        receipt = dict(zip(columns("production_authoring_command_receipts"), receipt, strict=True))
        audit = dict(zip(columns("production_authoring_audit_intents"), audit, strict=True))
        event = dict(zip(columns("production_authoring_outbox_intents"), event, strict=True))
        common = (
            org_id, run_id, agent_id, source_set_digest,
            int(values["source_count"]), int(values["total_bytes"]),
            receipt["command_digest"], fingerprint,
        )
        common_names = (
            "org_id", "run_id", "agent_id", "source_set_digest",
            "source_count", "total_bytes", "command_digest", "resource_fingerprint",
        )
        if (
            not _sha(receipt["command_digest"])
            or not _opaque(receipt["idempotency_key"])
            or not _opaque(receipt["policy_version"])
            or not _sha(receipt["policy_digest"])
            or not _sha(receipt["grant_evidence_digest"])
            or not _sha(receipt["identity_session_digest"])
            or not _sha(receipt["identity_evidence_digest"])
            or not _sha(receipt["resource_fingerprint"])
            or not _timestamp(receipt["created_at"])
            or tuple(audit[name] for name in common_names) != common
            or tuple(event[name] for name in common_names) != common
            or receipt["resource_fingerprint"] != fingerprint
            or receipt["result_state"] != "extracting"
            or int(receipt["result_revision"]) != 0
            or audit["principal_id"] != owner_id
            or audit["action"] != "author.write"
            or int(audit["result_revision"]) != 0
            or int(event["result_revision"]) != 0
            or receipt["policy_version"] != audit["policy_version"]
            or receipt["policy_digest"] != audit["policy_digest"]
            or receipt["grant_evidence_digest"] != audit["grant_evidence_digest"]
            or receipt["identity_session_digest"] != audit["identity_session_digest"]
            or receipt["identity_evidence_digest"] != audit["identity_evidence_digest"]
            or receipt["created_at"] != values["created_at"]
            or audit["created_at"] != values["created_at"]
            or event["created_at"] != values["created_at"]
            or int(event["delivered"]) != 0
        ):
            raise ProductionAuthoringResourceUnavailable()
    except ProductionAuthoringResourceUnavailable:
        raise
    except (KeyError, TypeError, ValueError, sqlite3.Error) as error:
        raise ProductionAuthoringResourceUnavailable() from error
    finally:
        if connection.total_changes != before:
            raise ProductionAuthoringResourceUnavailable()


__all__ = [
    "ProductionAuthoringResourceUnavailable",
    "canonical_digest",
    "canonical_completion_resource_fingerprint",
    "canonical_json",
    "canonical_resource_fingerprint",
    "canonical_source_set_digest",
    "canonical_start_run_id",
    "validate_authoring_catalog",
    "validate_current_authoring_resource",
]
