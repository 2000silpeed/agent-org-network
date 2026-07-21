"""P18 S1a strict, immutable SQLite reciprocal-review ledger capability only."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Final

COMPONENT_ID: Final = "durable_reciprocal_review_ledger_v1"
_MANIFEST = "schema_component_manifests"
_TABLES: Final = (
    "durable_reciprocal_review_artifact_revisions",
    "durable_reciprocal_review_provenance_events",
    "durable_reciprocal_review_cycles",
    "durable_reciprocal_review_requirements",
    "durable_reciprocal_review_runs",
    "durable_reciprocal_review_lease_epochs",
    "durable_reciprocal_review_finding_batches",
    "durable_reciprocal_review_findings",
    "durable_reciprocal_review_disposition_receipts",
    "durable_reciprocal_review_audit_events",
    "durable_reciprocal_review_outbox_intents",
    "durable_reciprocal_review_command_receipts",
    "durable_reciprocal_review_lineage_members",
)
_INDEX = "durable_reciprocal_review_requirements_cycle_idx"
_ACTIVE_CYCLE_INDEX = "durable_reciprocal_review_one_active_cycle"
_MANIFEST_DDL = """CREATE TABLE schema_component_manifests (component_id TEXT PRIMARY KEY NOT NULL COLLATE BINARY, schema_version INTEGER NOT NULL, manifest_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL)"""
_OPAQUE = "length({x}) BETWEEN 1 AND 128 AND {x} GLOB '[A-Za-z0-9]*' AND {x} NOT GLOB '*[^A-Za-z0-9._:-]*'"
_HASH = "length({x})=64 AND {x} NOT GLOB '*[^0-9a-f]*'"
_TIME = "length({x})=24 AND {x} GLOB '????-??-??T??:??:??.???Z' AND COALESCE(strftime('%Y-%m-%dT%H:%M:%fZ',{x})={x},0)"
_OPAQUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z")


def _o(name: str) -> str:
    return _OPAQUE.format(x=name)


def _h(name: str) -> str:
    return _HASH.format(x=name)


def _t(name: str) -> str:
    return _TIME.format(x=name)


_DDLS: Final = {
    _TABLES[
        0
    ]: f"""CREATE TABLE {_TABLES[0]} (org_id TEXT NOT NULL COLLATE BINARY CHECK({_o("org_id")}), artifact_id TEXT NOT NULL COLLATE BINARY CHECK({_o("artifact_id")}), revision_id TEXT NOT NULL COLLATE BINARY CHECK({_o("revision_id")}), revision_no INTEGER NOT NULL CHECK(revision_no>=1), parent_revision_id TEXT COLLATE BINARY CHECK(parent_revision_id IS NULL OR {_o("parent_revision_id")}), content_ref TEXT NOT NULL COLLATE BINARY CHECK({_o("content_ref")}), content_sha256 TEXT NOT NULL COLLATE BINARY CHECK({_h("content_sha256")}), data_classification TEXT NOT NULL CHECK(data_classification IN ('public','internal','confidential','restricted')), boundary_snapshot_ref TEXT NOT NULL COLLATE BINARY CHECK({_o("boundary_snapshot_ref")}), provenance_kind TEXT NOT NULL CHECK(provenance_kind IN ('human','ai','mixed','unknown')), provenance_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("provenance_digest")}), lineage_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("lineage_digest")}), boundary_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("boundary_digest")}), declassification_receipt_id TEXT COLLATE BINARY CHECK(declassification_receipt_id IS NULL OR {_o("declassification_receipt_id")}), schema_version INTEGER NOT NULL CHECK(schema_version>=1), created_at TEXT NOT NULL COLLATE BINARY CHECK({_t("created_at")}), PRIMARY KEY(org_id,revision_id), UNIQUE(org_id,artifact_id,revision_no), FOREIGN KEY(org_id,parent_revision_id) REFERENCES {_TABLES[0]}(org_id,revision_id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
    _TABLES[
        1
    ]: f"""CREATE TABLE {_TABLES[1]} (org_id TEXT NOT NULL COLLATE BINARY CHECK({_o("org_id")}), event_id TEXT NOT NULL COLLATE BINARY CHECK({_o("event_id")}), revision_id TEXT NOT NULL COLLATE BINARY CHECK({_o("revision_id")}), principal_kind TEXT NOT NULL CHECK(principal_kind IN ('human','model_execution','deterministic_transform','imported_unknown')), principal_ref TEXT NOT NULL COLLATE BINARY CHECK({_o("principal_ref")}), content_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("content_digest")}), resolution_receipt_id TEXT COLLATE BINARY CHECK(resolution_receipt_id IS NULL OR {_o("resolution_receipt_id")}), created_at TEXT NOT NULL COLLATE BINARY CHECK({_t("created_at")}), PRIMARY KEY(org_id,event_id), FOREIGN KEY(org_id,revision_id) REFERENCES {_TABLES[0]}(org_id,revision_id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
    _TABLES[
        2
    ]: f"""CREATE TABLE {_TABLES[2]} (org_id TEXT NOT NULL COLLATE BINARY CHECK({_o("org_id")}), cycle_id TEXT NOT NULL COLLATE BINARY CHECK({_o("cycle_id")}), revision_id TEXT NOT NULL COLLATE BINARY CHECK({_o("revision_id")}), cycle_no INTEGER NOT NULL CHECK(cycle_no>=1), state_kind TEXT NOT NULL CHECK(state_kind IN ('review_open','awaiting_human_disposition','binding_ready','binding_pending','bound','superseded')), active INTEGER NOT NULL CHECK(active IN (0,1)), provenance_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("provenance_digest")}), policy_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("policy_digest")}), created_at TEXT NOT NULL COLLATE BINARY CHECK({_t("created_at")}), PRIMARY KEY(org_id,cycle_id), UNIQUE(org_id,revision_id,cycle_no), FOREIGN KEY(org_id,revision_id) REFERENCES {_TABLES[0]}(org_id,revision_id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
    _TABLES[
        3
    ]: f"""CREATE TABLE {_TABLES[3]} (org_id TEXT NOT NULL COLLATE BINARY CHECK({_o("org_id")}), requirement_id TEXT NOT NULL COLLATE BINARY CHECK({_o("requirement_id")}), cycle_id TEXT NOT NULL COLLATE BINARY CHECK({_o("cycle_id")}), reviewer_kind TEXT NOT NULL CHECK(reviewer_kind IN ('ai','human')), completion_rule TEXT NOT NULL CHECK(completion_rule IN ('all','any','quorum')), required_count INTEGER NOT NULL CHECK(required_count>=1), independence_rule TEXT NOT NULL COLLATE BINARY CHECK({_o("independence_rule")}), rubric_version TEXT NOT NULL COLLATE BINARY CHECK({_o("rubric_version")}), risk_class TEXT NOT NULL COLLATE BINARY CHECK({_o("risk_class")}), reviewer_assignment_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("reviewer_assignment_digest")}), deadline_at TEXT NOT NULL COLLATE BINARY CHECK({_t("deadline_at")}), waivable INTEGER NOT NULL CHECK(waivable IN (0,1)), PRIMARY KEY(org_id,requirement_id), FOREIGN KEY(org_id,cycle_id) REFERENCES {_TABLES[2]}(org_id,cycle_id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
    _TABLES[
        4
    ]: f"""CREATE TABLE {_TABLES[4]} (org_id TEXT NOT NULL COLLATE BINARY CHECK({_o("org_id")}), review_run_id TEXT NOT NULL COLLATE BINARY CHECK({_o("review_run_id")}), requirement_id TEXT NOT NULL COLLATE BINARY CHECK({_o("requirement_id")}), run_attempt INTEGER NOT NULL CHECK(run_attempt>=1), lease_epoch INTEGER NOT NULL CHECK(lease_epoch>=1), lease_token_hash TEXT NOT NULL COLLATE BINARY CHECK({_h("lease_token_hash")}), state TEXT NOT NULL CHECK(state IN ('queued','leased','recorded','expired')), created_at TEXT NOT NULL COLLATE BINARY CHECK({_t("created_at")}), PRIMARY KEY(org_id,review_run_id), UNIQUE(org_id,requirement_id,run_attempt), FOREIGN KEY(org_id,requirement_id) REFERENCES {_TABLES[3]}(org_id,requirement_id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
    _TABLES[
        5
    ]: f"""CREATE TABLE {_TABLES[5]} (org_id TEXT NOT NULL COLLATE BINARY CHECK({_o("org_id")}), review_run_id TEXT NOT NULL COLLATE BINARY CHECK({_o("review_run_id")}), lease_epoch INTEGER NOT NULL CHECK(lease_epoch>=1), token_hash TEXT NOT NULL COLLATE BINARY CHECK({_h("token_hash")}), consumed_at TEXT COLLATE BINARY CHECK(consumed_at IS NULL OR {_t("consumed_at")}), PRIMARY KEY(org_id,review_run_id,lease_epoch), FOREIGN KEY(org_id,review_run_id) REFERENCES {_TABLES[4]}(org_id,review_run_id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
    _TABLES[
        6
    ]: f"""CREATE TABLE {_TABLES[6]} (org_id TEXT NOT NULL COLLATE BINARY CHECK({_o("org_id")}), batch_id TEXT NOT NULL COLLATE BINARY CHECK({_o("batch_id")}), review_run_id TEXT NOT NULL COLLATE BINARY CHECK({_o("review_run_id")}), model_execution_ref TEXT NOT NULL COLLATE BINARY CHECK({_o("model_execution_ref")}), prompt_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("prompt_digest")}), rubric_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("rubric_digest")}), input_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("input_digest")}), signature TEXT NOT NULL COLLATE BINARY CHECK({_o("signature")}), created_at TEXT NOT NULL COLLATE BINARY CHECK({_t("created_at")}), PRIMARY KEY(org_id,batch_id), FOREIGN KEY(org_id,review_run_id) REFERENCES {_TABLES[4]}(org_id,review_run_id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
    _TABLES[
        7
    ]: f"""CREATE TABLE {_TABLES[7]} (org_id TEXT NOT NULL COLLATE BINARY CHECK({_o("org_id")}), finding_id TEXT NOT NULL COLLATE BINARY CHECK({_o("finding_id")}), batch_id TEXT NOT NULL COLLATE BINARY CHECK({_o("batch_id")}), severity TEXT NOT NULL CHECK(severity IN ('info','warning','blocking')), evidence_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("evidence_digest")}), span_start INTEGER NOT NULL CHECK(span_start>=0), span_end INTEGER NOT NULL CHECK(span_end>=span_start), PRIMARY KEY(org_id,finding_id), FOREIGN KEY(org_id,batch_id) REFERENCES {_TABLES[6]}(org_id,batch_id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
    _TABLES[
        8
    ]: f"""CREATE TABLE {_TABLES[8]} (org_id TEXT NOT NULL COLLATE BINARY CHECK({_o("org_id")}), receipt_id TEXT NOT NULL COLLATE BINARY CHECK({_o("receipt_id")}), cycle_id TEXT NOT NULL COLLATE BINARY CHECK({_o("cycle_id")}), human_subject_id TEXT NOT NULL COLLATE BINARY CHECK({_o("human_subject_id")}), action TEXT NOT NULL CHECK(action IN ('accept_finding','reject_finding','defer_finding','approve_revision','request_changes','reject_revision')), finding_id TEXT COLLATE BINARY CHECK(finding_id IS NULL OR {_o("finding_id")}), command_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("command_digest")}), created_at TEXT NOT NULL COLLATE BINARY CHECK({_t("created_at")}), CHECK((action IN ('accept_finding','reject_finding','defer_finding') AND finding_id IS NOT NULL) OR (action IN ('approve_revision','request_changes','reject_revision') AND finding_id IS NULL)), PRIMARY KEY(org_id,receipt_id), FOREIGN KEY(org_id,cycle_id) REFERENCES {_TABLES[2]}(org_id,cycle_id) ON UPDATE RESTRICT ON DELETE RESTRICT, FOREIGN KEY(org_id,finding_id) REFERENCES {_TABLES[7]}(org_id,finding_id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
    _TABLES[
        9
    ]: f"""CREATE TABLE {_TABLES[9]} (org_id TEXT NOT NULL COLLATE BINARY CHECK({_o("org_id")}), audit_id TEXT NOT NULL COLLATE BINARY CHECK({_o("audit_id")}), cycle_id TEXT NOT NULL COLLATE BINARY CHECK({_o("cycle_id")}), event_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("event_digest")}), created_at TEXT NOT NULL COLLATE BINARY CHECK({_t("created_at")}), PRIMARY KEY(org_id,audit_id), FOREIGN KEY(org_id,cycle_id) REFERENCES {_TABLES[2]}(org_id,cycle_id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
    _TABLES[
        10
    ]: f"""CREATE TABLE {_TABLES[10]} (org_id TEXT NOT NULL COLLATE BINARY CHECK({_o("org_id")}), outbox_id TEXT NOT NULL COLLATE BINARY CHECK({_o("outbox_id")}), cycle_id TEXT NOT NULL COLLATE BINARY CHECK({_o("cycle_id")}), payload_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("payload_digest")}), created_at TEXT NOT NULL COLLATE BINARY CHECK({_t("created_at")}), PRIMARY KEY(org_id,outbox_id), FOREIGN KEY(org_id,cycle_id) REFERENCES {_TABLES[2]}(org_id,cycle_id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
    _TABLES[
        11
    ]: f"""CREATE TABLE {_TABLES[11]} (org_id TEXT NOT NULL COLLATE BINARY CHECK({_o("org_id")}), receipt_id TEXT NOT NULL COLLATE BINARY CHECK({_o("receipt_id")}), command_digest TEXT NOT NULL COLLATE BINARY CHECK({_h("command_digest")}), cycle_id TEXT NOT NULL COLLATE BINARY CHECK({_o("cycle_id")}), created_at TEXT NOT NULL COLLATE BINARY CHECK({_t("created_at")}), PRIMARY KEY(org_id,receipt_id), UNIQUE(org_id,command_digest), FOREIGN KEY(org_id,cycle_id) REFERENCES {_TABLES[2]}(org_id,cycle_id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
    _TABLES[
        12
    ]: f"""CREATE TABLE {_TABLES[12]} (org_id TEXT NOT NULL COLLATE BINARY CHECK({_o("org_id")}), revision_id TEXT NOT NULL COLLATE BINARY CHECK({_o("revision_id")}), event_id TEXT NOT NULL COLLATE BINARY CHECK({_o("event_id")}), ordinal INTEGER NOT NULL CHECK(ordinal>=1), PRIMARY KEY(org_id,revision_id,event_id), UNIQUE(org_id,revision_id,ordinal), FOREIGN KEY(org_id,revision_id) REFERENCES {_TABLES[0]}(org_id,revision_id) ON UPDATE RESTRICT ON DELETE RESTRICT, FOREIGN KEY(org_id,event_id) REFERENCES {_TABLES[1]}(org_id,event_id) ON UPDATE RESTRICT ON DELETE RESTRICT)""",
}
_INDEX_DDLS = {
    _INDEX: f"CREATE INDEX {_INDEX} ON {_TABLES[3]}(org_id,cycle_id)",
    _ACTIVE_CYCLE_INDEX: f"CREATE UNIQUE INDEX {_ACTIVE_CYCLE_INDEX} ON {_TABLES[2]}(org_id,revision_id) WHERE active=1",
}
_TRIGGERS: Final = {table: (f"{table}_no_update", f"{table}_no_delete") for table in _TABLES}


class SqliteDurableReciprocalReviewError(RuntimeError):
    """The exact P18 S1a durable-ledger capability is unavailable or drifted."""


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _same(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and " ".join(actual.split()).replace("( ", "(").replace(
        " )", ")"
    ) == " ".join(expected.split()).replace("( ", "(").replace(" )", ")")


def _trigger_ddls() -> dict[str, str]:
    result: dict[str, str] = {}
    for table, (update, delete) in _TRIGGERS.items():
        if table == _TABLES[2]:
            result[update] = (
                f"CREATE TRIGGER {update} BEFORE UPDATE ON {table} FOR EACH ROW WHEN NOT (OLD.state_kind='review_open' AND NEW.state_kind='awaiting_human_disposition' AND OLD.org_id=NEW.org_id AND OLD.cycle_id=NEW.cycle_id AND OLD.revision_id=NEW.revision_id AND OLD.cycle_no=NEW.cycle_no AND OLD.active=NEW.active AND OLD.provenance_digest=NEW.provenance_digest AND OLD.policy_digest=NEW.policy_digest AND OLD.created_at=NEW.created_at) BEGIN SELECT RAISE(ABORT, 'reciprocal review ledger is immutable'); END"
            )
        elif table == _TABLES[4]:
            result[update] = (
                f"CREATE TRIGGER {update} BEFORE UPDATE ON {table} FOR EACH ROW WHEN NOT ((OLD.state='queued' AND NEW.state='leased') OR (OLD.state='leased' AND NEW.state='recorded')) OR NEW.org_id!=OLD.org_id OR NEW.review_run_id!=OLD.review_run_id OR NEW.requirement_id!=OLD.requirement_id OR NEW.run_attempt!=OLD.run_attempt OR NEW.lease_epoch!=OLD.lease_epoch OR NEW.lease_token_hash!=OLD.lease_token_hash OR NEW.created_at!=OLD.created_at BEGIN SELECT RAISE(ABORT, 'reciprocal review ledger is immutable'); END"
            )
        else:
            result[update] = (
                f"CREATE TRIGGER {update} BEFORE UPDATE ON {table} FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'reciprocal review ledger is immutable'); END"
            )
        result[delete] = (
            f"CREATE TRIGGER {delete} BEFORE DELETE ON {table} FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'reciprocal review ledger deletion is forbidden'); END"
        )
    return result


def _catalog() -> dict[str, object]:
    return {
        "tables": [{"name": n, "sql": " ".join(s.split())} for n, s in _DDLS.items()],
        "indexes": [{"name": n, "sql": " ".join(s.split())} for n, s in _INDEX_DDLS.items()],
        "triggers": [{"name": n, "sql": " ".join(s.split())} for n, s in _trigger_ddls().items()],
    }


def _sql(connection: sqlite3.Connection, kind: str, name: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?", (kind, name)
    ).fetchone()
    return None if row is None else row[0]


def _validate(connection: sqlite3.Connection) -> None:
    manifest = _canonical(
        {"component_id": COMPONENT_ID, "schema_version": 1, "catalog": _catalog()}
    )
    marker = connection.execute(
        f"SELECT schema_version,manifest_json,manifest_sha256 FROM {_MANIFEST} WHERE component_id=?",
        (COMPONENT_ID,),
    ).fetchone()
    if marker != (1, manifest, hashlib.sha256(manifest.encode()).hexdigest()):
        raise SqliteDurableReciprocalReviewError(
            "reciprocal review manifest가 canonical하지 않습니다."
        )
    expected = set(_TABLES) | set(_INDEX_DDLS) | set(_trigger_ddls())
    actual = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE name LIKE 'durable_reciprocal_review%' "
            "AND name NOT LIKE 'durable_reciprocal_review_cycles_v2%' "
                "AND name NOT LIKE 'durable_reciprocal_review_cycles_v3%' "
                    "AND name NOT LIKE 'durable_reciprocal_review_cycles_v4%' "
                        "AND name NOT LIKE 'durable_reciprocal_review_cycles_v5%' "
                        "AND name NOT LIKE 'durable_reciprocal_review_source_binding_cycles_v6%'"
                        "AND name NOT LIKE 'durable_reciprocal_review_source_binding_cycles_v7%'"
        )
    }
    if (
        actual != expected
        or any(not _same(_sql(connection, "table", name), ddl) for name, ddl in _DDLS.items())
        or any(not _same(_sql(connection, "index", name), ddl) for name, ddl in _INDEX_DDLS.items())
        or any(
            not _same(_sql(connection, "trigger", name), ddl)
            for name, ddl in _trigger_ddls().items()
        )
    ):
        raise SqliteDurableReciprocalReviewError(
            "reciprocal review catalog가 canonical하지 않습니다."
        )
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SqliteDurableReciprocalReviewError(
            "reciprocal review foreign key가 canonical하지 않습니다."
        )
    for table in _TABLES:
        cursor = connection.execute(f"SELECT * FROM {table}")
        names = tuple(column[0] for column in cursor.description or ())
        for row in cursor:
            data = dict(zip(names, row, strict=True))
            if (
                any(value is None for value in row[:2])
                or any(
                    (
                        name.endswith(("_id", "_ref"))
                        and value is not None
                        and (type(value) is not str or _OPAQUE_RE.fullmatch(value) is None)
                    )
                    or (
                        name.endswith(("_digest", "_sha256", "_hash"))
                        and (type(value) is not str or _HASH_RE.fullmatch(value) is None)
                    )
                    or (
                        name.endswith("_at")
                        and value is not None
                        and (type(value) is not str or _TIME_RE.fullmatch(value) is None)
                    )
                    for name, value in zip(names, row, strict=True)
                )
                or _row_is_forged(table, data)
            ):
                raise SqliteDurableReciprocalReviewError("forged reciprocal review row가 있습니다.")
    _validate_lineage_members(connection)


def _validate_lineage_members(connection: sqlite3.Connection) -> None:
    """Reconstruct each revision's closure only from its parent and direct events."""
    revisions = {
        (row[0], row[1]): (row[2], row[3], row[4], row[5], row[6], row[7])
        for row in connection.execute(
            f"SELECT org_id,revision_id,artifact_id,parent_revision_id,content_sha256,provenance_kind,lineage_digest,provenance_digest FROM {_TABLES[0]}"
        )
    }
    closures: dict[tuple[str, str], tuple[tuple[str, ...], tuple[str, ...]]] = {}
    visiting: set[tuple[str, str]] = set()

    def provenance_kind(kinds: tuple[str, ...]) -> str:
        if "imported_unknown" in kinds:
            return "unknown"
        human = "human" in kinds
        ai = any(kind in {"model_execution", "deterministic_transform"} for kind in kinds)
        return "mixed" if human and ai else "ai" if ai else "human"

    def reconstruct(key: tuple[str, str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if key in closures:
            return closures[key]
        if key in visiting:
            raise SqliteDurableReciprocalReviewError(
                "reciprocal review parent lineage cycle이 있습니다."
            )
        row = revisions.get(key)
        if row is None:
            raise SqliteDurableReciprocalReviewError(
                "reciprocal review parent revision이 없습니다."
            )
        (
            artifact_id,
            parent_revision_id,
            content_sha256,
            stored_kind,
            stored_digest,
            _provenance_digest,
        ) = row
        visiting.add(key)
        parent_events: tuple[str, ...] = ()
        parent_kinds: tuple[str, ...] = ()
        if parent_revision_id is not None:
            parent_key = (key[0], parent_revision_id)
            parent = revisions.get(parent_key)
            if parent is None or parent[0] != artifact_id:
                raise SqliteDurableReciprocalReviewError(
                    "reciprocal review parent artifact/org가 일치하지 않습니다."
                )
            parent_events, parent_kinds = reconstruct(parent_key)
        direct = connection.execute(
            f"SELECT event_id,principal_kind,content_digest FROM {_TABLES[1]} WHERE org_id=? AND revision_id=? ORDER BY event_id",
            key,
        ).fetchall()
        if not direct:
            raise SqliteDurableReciprocalReviewError(
                "reciprocal review direct provenance가 없습니다."
            )
        if any(event[2] != content_sha256 for event in direct):
            raise SqliteDurableReciprocalReviewError(
                "reciprocal review direct provenance content가 revision과 일치하지 않습니다."
            )
        expected_events = tuple(sorted(set((*parent_events, *(event[0] for event in direct)))))
        expected_kinds = (*parent_kinds, *(event[1] for event in direct))
        members = connection.execute(
            f"SELECT event_id,ordinal FROM {_TABLES[12]} WHERE org_id=? AND revision_id=? ORDER BY ordinal",
            key,
        ).fetchall()
        actual_events = tuple(member[0] for member in members)
        if (
            actual_events != expected_events
            or tuple(member[1] for member in members) != tuple(range(1, len(members) + 1))
            or hashlib.sha256(_canonical(expected_events).encode()).hexdigest() != stored_digest
            or provenance_kind(expected_kinds) != stored_kind
        ):
            raise SqliteDurableReciprocalReviewError(
                "reciprocal review lineage provenance가 canonical하지 않습니다."
            )
        visiting.remove(key)
        closures[key] = (expected_events, expected_kinds)
        return closures[key]

    for key in revisions:
        reconstruct(key)
    for org_id, revision_id, provenance_digest in connection.execute(
        f"SELECT org_id,revision_id,provenance_digest FROM {_TABLES[2]}"
    ):
        revision = revisions.get((org_id, revision_id))
        if revision is None or provenance_digest != revision[5]:
            raise SqliteDurableReciprocalReviewError(
                "reciprocal review cycle provenance가 revision과 일치하지 않습니다."
            )


def validate_sqlite_durable_reciprocal_review_ledger(connection: sqlite3.Connection) -> None:
    """Validate the sealed S1a catalog before a reciprocal-review write transaction."""
    _validate(connection)


def _row_is_forged(table: str, row: dict[str, object]) -> bool:
    """Recheck every sealed SQLite constraint; PRAGMA ignore_check_constraints is untrusted."""

    def integer(name: str, minimum: int = 0) -> bool:
        value = row[name]
        return type(value) is int and value >= minimum

    def opaque(name: str) -> bool:
        value = row[name]
        return isinstance(value, str) and _OPAQUE_RE.fullmatch(value) is not None

    if table == _TABLES[0]:
        return not (
            integer("revision_no", 1)
            and integer("schema_version", 1)
            and row["data_classification"] in {"public", "internal", "confidential", "restricted"}
            and row["provenance_kind"] in {"human", "ai", "mixed", "unknown"}
        )
    if table == _TABLES[1]:
        return row["principal_kind"] not in {
            "human",
            "model_execution",
            "deterministic_transform",
            "imported_unknown",
        }
    if table == _TABLES[2]:
        state = row["state_kind"]
        active = row["active"]
        return (
            state
            not in {
                "review_open",
                "awaiting_human_disposition",
                "binding_ready",
                "binding_pending",
                "bound",
                "superseded",
            }
            or type(active) is not int
            or active not in (0, 1)
            or (active == 1)
            != (
                state
                in {"review_open", "awaiting_human_disposition", "binding_ready", "binding_pending"}
            )
            or not integer("cycle_no", 1)
        )
    if table == _TABLES[3]:
        opaque_fields = ("independence_rule", "rubric_version", "risk_class")
        return (
            row["reviewer_kind"] not in {"ai", "human"}
            or row["completion_rule"] not in {"all", "any", "quorum"}
            or not integer("required_count", 1)
            or row["waivable"] not in (0, 1)
            or type(row["waivable"]) is not int
            or any(not opaque(name) for name in opaque_fields)
        )
    if table == _TABLES[4]:
        return (
            row["state"] not in {"queued", "leased", "recorded", "expired"}
            or not integer("run_attempt", 1)
            or not integer("lease_epoch", 1)
        )
    if table == _TABLES[5]:
        return not integer("lease_epoch", 1)
    if table == _TABLES[7]:
        start = row["span_start"]
        end = row["span_end"]
        if type(start) is not int or type(end) is not int or start < 0 or end < 0:
            return True
        return row["severity"] not in {"info", "warning", "blocking"} or end < start
    if table == _TABLES[8]:
        finding_actions = {"accept_finding", "reject_finding", "defer_finding"}
        revision_actions = {"approve_revision", "request_changes", "reject_revision"}
        return row["action"] not in finding_actions | revision_actions or (
            row["action"] in finding_actions
        ) != (row["finding_id"] is not None)
    if table == _TABLES[12]:
        return not integer("ordinal", 1)
    return False


def migrate_sqlite_durable_reciprocal_review_ledger(
    connection: sqlite3.Connection, *, fault_injector: Callable[[str], None] | None = None
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        manifest_exists = (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (_MANIFEST,)
            ).fetchone()
            is not None
        )
        present = manifest_exists and (
            connection.execute(
                f"SELECT 1 FROM {_MANIFEST} WHERE component_id=?", (COMPONENT_ID,)
            ).fetchone()
            is not None
            or any(_sql(connection, "table", table) is not None for table in _TABLES)
        )
        if present:
            _validate(connection)
        else:
            if not manifest_exists:
                connection.execute(_MANIFEST_DDL)
            for index, ddl in enumerate(_DDLS.values()):
                connection.execute(ddl)
                if index == 0 and fault_injector is not None:
                    fault_injector("after_artifact_revisions")
            for ddl in _INDEX_DDLS.values():
                connection.execute(ddl)
            for ddl in _trigger_ddls().values():
                connection.execute(ddl)
            manifest = _canonical(
                {"component_id": COMPONENT_ID, "schema_version": 1, "catalog": _catalog()}
            )
            connection.execute(
                f"INSERT INTO {_MANIFEST} VALUES(?,?,?,?)",
                (COMPONENT_ID, 1, manifest, hashlib.sha256(manifest.encode()).hexdigest()),
            )
            _validate(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def open_sqlite_durable_reciprocal_review_ledger(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _validate(connection)
        return connection
    except Exception as error:
        connection.close()
        if isinstance(error, SqliteDurableReciprocalReviewError):
            raise
        raise SqliteDurableReciprocalReviewError(
            "reciprocal review ledger가 unavailable입니다."
        ) from error
