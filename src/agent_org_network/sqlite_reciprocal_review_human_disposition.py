# pyright: reportPrivateUsage=false
"""P18 S1b.4: v2 reciprocal-review human disposition, deliberately source-free."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Mapping, Protocol, cast

from agent_org_network.reciprocal_review import (
    HumanPrincipal,
    SubmitHumanDisposition,
    SubmittedHumanDisposition,
)
from agent_org_network.sqlite_durable_reciprocal_review import (
    validate_sqlite_durable_reciprocal_review_ledger,
)
from agent_org_network.sqlite_reciprocal_review_ai_batches import (
    _validate_catalog as _validate_ai_batches,
)  # pyright: ignore[reportPrivateUsage]
from agent_org_network.sqlite_reciprocal_review_lease import validate_sqlite_reciprocal_review_lease

COMPONENT_ID = "durable_reciprocal_review_ledger_v2"
_MANIFEST = "schema_component_manifests"
_CYCLE = "durable_reciprocal_review_cycles_v2"
_RECEIPT = "reciprocal_review_human_disposition_receipts"
_RESULT = "reciprocal_review_human_disposition_results"
_AUDIT = "reciprocal_review_human_disposition_audit"
_OUTBOX = "reciprocal_review_human_disposition_outbox"
_TABLES = (_CYCLE, _RECEIPT, _RESULT, _AUDIT, _OUTBOX)
_DDLS = {
    _CYCLE: "CREATE TABLE durable_reciprocal_review_cycles_v2 (org_id TEXT NOT NULL COLLATE BINARY,cycle_id TEXT NOT NULL COLLATE BINARY,revision_id TEXT NOT NULL COLLATE BINARY,cycle_no INTEGER NOT NULL CHECK(cycle_no>=1),state_kind TEXT NOT NULL CHECK(state_kind IN ('review_open','awaiting_human_disposition','binding_ready','binding_pending','bound','superseded')),active INTEGER NOT NULL CHECK(active IN (0,1)),provenance_digest TEXT NOT NULL CHECK(length(provenance_digest)=64),policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),created_at TEXT NOT NULL,cycle_revision INTEGER NOT NULL CHECK(cycle_revision>=1),PRIMARY KEY(org_id,cycle_id),UNIQUE(org_id,revision_id,cycle_no),FOREIGN KEY(org_id,revision_id) REFERENCES durable_reciprocal_review_artifact_revisions(org_id,revision_id))",
    _RECEIPT: "CREATE TABLE reciprocal_review_human_disposition_receipts (org_id TEXT NOT NULL,receipt_id TEXT NOT NULL,audit_id TEXT NOT NULL,outbox_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,command_digest TEXT NOT NULL CHECK(length(command_digest)=64),cycle_id TEXT NOT NULL,revision_id TEXT NOT NULL,expected_cycle_revision INTEGER NOT NULL CHECK(expected_cycle_revision>=1),result_cycle_revision INTEGER NOT NULL CHECK(result_cycle_revision>=1),subject_id TEXT NOT NULL,authn_context_digest TEXT NOT NULL CHECK(length(authn_context_digest)=64),action TEXT NOT NULL CHECK(action IN ('approve_revision','request_changes','reject_revision')),policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),provenance_digest TEXT NOT NULL CHECK(length(provenance_digest)=64),eligibility_digest TEXT NOT NULL CHECK(length(eligibility_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,receipt_id),UNIQUE(org_id,idempotency_key),UNIQUE(org_id,audit_id),UNIQUE(org_id,outbox_id),FOREIGN KEY(org_id,cycle_id) REFERENCES durable_reciprocal_review_cycles_v2(org_id,cycle_id))",
    _RESULT: "CREATE TABLE reciprocal_review_human_disposition_results (org_id TEXT NOT NULL,receipt_id TEXT NOT NULL,cycle_id TEXT NOT NULL,command_digest TEXT NOT NULL CHECK(length(command_digest)=64),cycle_revision INTEGER NOT NULL CHECK(cycle_revision>=1),cycle_state TEXT NOT NULL CHECK(cycle_state='binding_ready'),action TEXT NOT NULL CHECK(action IN ('approve_revision','request_changes','reject_revision')),created_at TEXT NOT NULL,PRIMARY KEY(org_id,receipt_id),FOREIGN KEY(org_id,receipt_id) REFERENCES reciprocal_review_human_disposition_receipts(org_id,receipt_id))",
    _AUDIT: "CREATE TABLE reciprocal_review_human_disposition_audit (org_id TEXT NOT NULL,audit_id TEXT NOT NULL,receipt_id TEXT NOT NULL,event_digest TEXT NOT NULL CHECK(length(event_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,audit_id),UNIQUE(org_id,receipt_id),FOREIGN KEY(org_id,receipt_id) REFERENCES reciprocal_review_human_disposition_receipts(org_id,receipt_id))",
    _OUTBOX: "CREATE TABLE reciprocal_review_human_disposition_outbox (org_id TEXT NOT NULL,outbox_id TEXT NOT NULL,receipt_id TEXT NOT NULL,payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,outbox_id),UNIQUE(org_id,receipt_id),FOREIGN KEY(org_id,receipt_id) REFERENCES reciprocal_review_human_disposition_receipts(org_id,receipt_id))",
}
_TRIGGERS = {
    f"{table}_no_{verb}": f"CREATE TRIGGER {table}_no_{verb} BEFORE {verb.upper()} ON {table} BEGIN SELECT RAISE(ABORT,'immutable reciprocal review v2'); END"
    for table in _TABLES[1:]
    for verb in ("update", "delete")
}
_TRIGGERS[f"{_CYCLE}_legal_update"] = (
    "CREATE TRIGGER durable_reciprocal_review_cycles_v2_legal_update BEFORE UPDATE ON durable_reciprocal_review_cycles_v2 FOR EACH ROW WHEN NOT (OLD.state_kind='awaiting_human_disposition' AND NEW.state_kind='binding_ready' AND NEW.cycle_revision=OLD.cycle_revision+1 AND OLD.org_id=NEW.org_id AND OLD.cycle_id=NEW.cycle_id AND OLD.revision_id=NEW.revision_id AND OLD.cycle_no=NEW.cycle_no AND OLD.active=NEW.active AND OLD.provenance_digest=NEW.provenance_digest AND OLD.policy_digest=NEW.policy_digest AND OLD.created_at=NEW.created_at) BEGIN SELECT RAISE(ABORT,'illegal reciprocal review v2 transition'); END"
)
_TRIGGERS[f"{_CYCLE}_no_delete"] = (
    f"CREATE TRIGGER {_CYCLE}_no_delete BEFORE DELETE ON {_CYCLE} BEGIN SELECT RAISE(ABORT,'immutable reciprocal review v2'); END"
)


class SqliteReciprocalReviewHumanDispositionError(RuntimeError):
    """Human disposition is unsafe or the v2 capability is unavailable."""


class SqliteReciprocalReviewHumanDispositionConflict(SqliteReciprocalReviewHumanDispositionError):
    """An idempotency key or CAS denotes different semantics."""


class _TrustedHumanDispositionAuthority:
    """Sealed HMAC authority: the key registry, not a caller supplied predicate, is trust."""

    def __init__(self, keys: Mapping[str, bytes]) -> None:
        if not keys or any(not key or not value for key, value in keys.items()):
            raise ValueError("trusted human disposition key registry가 비어 있습니다.")
        self._keys = dict(keys)

    def verify(
        self,
        *,
        principal: HumanPrincipal,
        revision_id: str,
        cycle_id: str,
        action: str,
        policy_digest: str,
        provenance_digest: str,
        independence_digest: str,
        now: datetime,
    ) -> bool:
        # The authenticated principal's opaque subject id selects its central authority key.
        key = self._keys.get(principal.subject_id)
        if key is None:
            return False
        payload = _canonical(
            {
                "org_id": principal.org_id,
                "subject_id": principal.subject_id,
                "authenticated_at": _time(principal.authenticated_at),
                "revision_id": revision_id,
                "cycle_id": cycle_id,
                "action": action,
                "policy_digest": policy_digest,
                "provenance_digest": provenance_digest,
                "independence_digest": independence_digest,
            }
        )
        # Authentication context is an opaque capability MAC issued by the central authority.
        return (
            now - principal.authenticated_at <= timedelta(minutes=5)
            and now >= principal.authenticated_at
            and hmac.compare_digest(
                principal.authn_context_digest,
                hmac.new(key, payload.encode(), hashlib.sha256).hexdigest(),
            )
        )


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _trusted_key_copy(value: object, name: str) -> dict[str, bytes]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{name}가 비어 있거나 유효하지 않습니다.")
    copied = dict(cast(Mapping[object, object], value))
    if any(
        type(key) is not str or not key.strip() or type(secret) is not bytes or not secret
        for key, secret in copied.items()
    ):
        raise ValueError(f"{name}의 key/value가 유효하지 않습니다.")
    return cast(dict[str, bytes], copied)


def _time(value: datetime) -> str:
    if (
        value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
        or value.microsecond % 1000
    ):
        raise SqliteReciprocalReviewHumanDispositionError(
            "DB time은 canonical UTC milliseconds여야 합니다."
        )
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _same(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and " ".join(actual.split()) == " ".join(expected.split())


def _catalog() -> dict[str, object]:
    return {
        "tables": [{"name": n, "sql": " ".join(s.split())} for n, s in _DDLS.items()],
        "triggers": [{"name": n, "sql": " ".join(s.split())} for n, s in _TRIGGERS.items()],
    }


def validate_sqlite_reciprocal_review_human_disposition(connection: sqlite3.Connection) -> None:
    validate_sqlite_durable_reciprocal_review_ledger(connection)
    manifest = _canonical(
        {"component_id": COMPONENT_ID, "schema_version": 2, "catalog": _catalog()}
    )
    marker = connection.execute(
        f"SELECT schema_version,manifest_json,manifest_sha256 FROM {_MANIFEST} WHERE component_id=?",
        (COMPONENT_ID,),
    ).fetchone()
    if marker != (2, manifest, hashlib.sha256(manifest.encode()).hexdigest()):
        raise SqliteReciprocalReviewHumanDispositionError(
            "reciprocal review v2 manifest가 canonical하지 않습니다."
        )
    actual = {
        r[0]
        for r in connection.execute(
            "SELECT name FROM sqlite_schema WHERE name LIKE 'durable_reciprocal_review_cycles_v2%' OR name LIKE 'reciprocal_review_human_disposition_%'"
        )
    }
    expected = set(_TABLES) | set(_TRIGGERS)
    if (
        actual != expected
        or any(
            not _same(
                connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (n,)
                ).fetchone()[0],
                sql,
            )
            for n, sql in _DDLS.items()
        )
        or any(
            not _same(
                connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (n,)
                ).fetchone()[0],
                sql,
            )
            for n, sql in _TRIGGERS.items()
        )
    ):
        raise SqliteReciprocalReviewHumanDispositionError(
            "reciprocal review v2 catalog가 canonical하지 않습니다."
        )
    _validate_rows(connection)


def _validate_rows(c: sqlite3.Connection) -> None:
    for row in c.execute(
        f"SELECT org_id,cycle_id,revision_id,state_kind,cycle_revision FROM {_CYCLE}"
    ):
        parent = c.execute(
            "SELECT revision_id,state_kind FROM durable_reciprocal_review_cycles WHERE org_id=? AND cycle_id=?",
            row[:2],
        ).fetchone()
        if (
            parent is None
            or parent[0] != row[2]
            or row[4] < 1
            or row[3]
            not in {
                "review_open",
                "awaiting_human_disposition",
                "binding_ready",
                "binding_pending",
                "bound",
                "superseded",
            }
        ):
            raise SqliteReciprocalReviewHumanDispositionError(
                "forged reciprocal review v2 cycle row가 있습니다."
            )
        if row[3] == "binding_ready":
            linked = c.execute(
                f"SELECT count(*) FROM {_RECEIPT} WHERE org_id=? AND cycle_id=? AND revision_id=? AND result_cycle_revision=?",
                (row[0], row[1], row[2], row[4]),
            ).fetchone()
            if linked != (1,):
                raise SqliteReciprocalReviewHumanDispositionError(
                    "BindingReady cycle에 exact human disposition receipt가 없습니다."
                )
    for row in c.execute(
        f"SELECT org_id,receipt_id,audit_id,outbox_id,command_digest,cycle_id,revision_id,expected_cycle_revision,result_cycle_revision,action,policy_digest,provenance_digest,eligibility_digest,created_at FROM {_RECEIPT}"
    ):
        (
            org_id,
            receipt_id,
            audit_id,
            outbox_id,
            digest,
            cycle_id,
            revision_id,
            expected,
            result_revision,
            action,
            policy,
            provenance,
            eligibility,
            created,
        ) = row
        result = c.execute(
            f"SELECT cycle_id,command_digest,cycle_revision,cycle_state,action,created_at FROM {_RESULT} WHERE org_id=? AND receipt_id=?",
            (org_id, receipt_id),
        ).fetchall()
        audit = c.execute(
            f"SELECT receipt_id,event_digest,created_at FROM {_AUDIT} WHERE org_id=? AND audit_id=?",
            (org_id, audit_id),
        ).fetchall()
        outbox = c.execute(
            f"SELECT receipt_id,payload_digest,created_at FROM {_OUTBOX} WHERE org_id=? AND outbox_id=?",
            (org_id, outbox_id),
        ).fetchall()
        cycle = c.execute(
            f"SELECT revision_id,state_kind,cycle_revision,policy_digest,provenance_digest FROM {_CYCLE} WHERE org_id=? AND cycle_id=?",
            (org_id, cycle_id),
        ).fetchone()
        if (
            not (len(result) == len(audit) == len(outbox) == 1)
            or result[0] != (cycle_id, digest, result_revision, "binding_ready", action, created)
            or audit[0] != (receipt_id, _digest(("human_disposition_audit", digest)), created)
            or outbox[0] != (receipt_id, _digest(("human_disposition_outbox", digest)), created)
            or cycle != (revision_id, "binding_ready", result_revision, policy, provenance)
            or result_revision != expected + 1
            or not all(
                isinstance(value, str) and len(value) == 64
                for value in (digest, policy, provenance, eligibility)
            )
        ):
            raise SqliteReciprocalReviewHumanDispositionError(
                "forged human disposition receipt/result가 있습니다."
            )
    receipt_set = set(c.execute(f"SELECT org_id,receipt_id FROM {_RECEIPT}"))
    if any(
        set(c.execute(query)) != receipt_set
        for query in (
            f"SELECT org_id,receipt_id FROM {_RESULT}",
            f"SELECT org_id,receipt_id FROM {_AUDIT}",
            f"SELECT org_id,receipt_id FROM {_OUTBOX}",
        )
    ):
        raise SqliteReciprocalReviewHumanDispositionError(
            "human disposition evidence graph가 bijection이 아닙니다."
        )


def migrate_sqlite_reciprocal_review_human_disposition_v2(
    connection: sqlite3.Connection, *, fault_injector: Callable[[str], None] | None = None
) -> None:
    """Install an empty v2 capability only; copying v1 cycles is a separate explicit upgrade."""
    try:
        connection.execute("BEGIN IMMEDIATE")
        validate_sqlite_durable_reciprocal_review_ledger(connection)
        existing = connection.execute(
            f"SELECT 1 FROM {_MANIFEST} WHERE component_id=?", (COMPONENT_ID,)
        ).fetchone()
        if existing:
            validate_sqlite_reciprocal_review_human_disposition(connection)
        else:
            for n, ddl in _DDLS.items():
                connection.execute(ddl)
                if fault_injector is not None:
                    fault_injector(f"after_{n}")
            for ddl in _TRIGGERS.values():
                connection.execute(ddl)
            manifest = _canonical(
                {"component_id": COMPONENT_ID, "schema_version": 2, "catalog": _catalog()}
            )
            connection.execute(
                f"INSERT INTO {_MANIFEST} VALUES(?,?,?,?)",
                (COMPONENT_ID, 2, manifest, hashlib.sha256(manifest.encode()).hexdigest()),
            )
            validate_sqlite_reciprocal_review_human_disposition(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def upgrade_sqlite_reciprocal_review_cycles_to_v2(connection: sqlite3.Connection) -> None:
    """The only deliberate v1→v2 cycle copy; never silently called by a UoW."""
    try:
        connection.execute("BEGIN IMMEDIATE")
        validate_sqlite_reciprocal_review_human_disposition(connection)
        if connection.execute(f"SELECT count(*) FROM {_CYCLE}").fetchone()[0]:
            raise SqliteReciprocalReviewHumanDispositionError(
                "v2 cycle upgrade는 empty v2에서 한 번만 허용됩니다."
            )
        connection.execute(
            f"INSERT INTO {_CYCLE} SELECT org_id,cycle_id,revision_id,cycle_no,state_kind,active,provenance_digest,policy_digest,created_at,1 FROM durable_reciprocal_review_cycles"
        )
        validate_sqlite_reciprocal_review_human_disposition(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


class HumanDispositionUnitOfWork(Protocol):
    def submit(
        self, principal: HumanPrincipal, command: SubmitHumanDisposition
    ) -> SubmittedHumanDisposition: ...


class _SqliteReciprocalReviewHumanDispositionUnitOfWork:
    def __init__(
        self,
        path: str | Path,
        *,
        trusted_human_authority_keys: Mapping[str, bytes],
        trusted_ai_execution_keys: Mapping[str, bytes],
        clock: Callable[[], datetime],
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        (
            self._path,
            self._authority,
            self._trusted_ai_execution_keys,
            self._clock,
            self._fault_injector,
        ) = (
            Path(path),
            _TrustedHumanDispositionAuthority(trusted_human_authority_keys),
            dict(trusted_ai_execution_keys),
            clock,
            fault_injector,
        )

    def submit(
        self, principal: HumanPrincipal, command: SubmitHumanDisposition
    ) -> SubmittedHumanDisposition:
        c = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("BEGIN IMMEDIATE")
            if c.execute(
                "SELECT 1 FROM schema_component_manifests WHERE component_id='durable_reciprocal_review_ledger_v3'"
            ).fetchone() is not None:
                raise SqliteReciprocalReviewHumanDispositionError(
                    "v3 cutover 뒤 v2 disposition writer는 사용할 수 없습니다."
                )
            action = command.disposition.kind
            validate_sqlite_reciprocal_review_human_disposition(c)
            validate_sqlite_reciprocal_review_lease(c)
            _validate_ai_batches(c)
            cycle = c.execute(
                f"SELECT revision_id,state_kind,cycle_revision,policy_digest,provenance_digest FROM {_CYCLE} WHERE org_id=? AND cycle_id=?",
                (principal.org_id, command.cycle_id),
            ).fetchone()
            if cycle is None:
                raise SqliteReciprocalReviewHumanDispositionError("v2 cycle이 없습니다.")
            eligibility = self._eligibility(c, principal.org_id, command.cycle_id, cycle)
            semantic = {
                "principal": {
                    "subject_id": principal.subject_id,
                    "authn_context_digest": principal.authn_context_digest,
                },
                "command": command.model_dump(mode="json"),
                "revision_id": cycle[0],
                "policy": cycle[3],
                "provenance": cycle[4],
                "eligibility": eligibility,
            }
            digest = _digest(semantic)
            old = c.execute(
                f"SELECT command_digest FROM {_RECEIPT} WHERE org_id=? AND idempotency_key=?",
                (principal.org_id, command.idempotency_key),
            ).fetchone()
            if old is not None:
                if old[0] != digest:
                    raise SqliteReciprocalReviewHumanDispositionConflict(
                        "idempotency semantic command가 다릅니다."
                    )
                self._authorize(principal, cycle, command.cycle_id, action, eligibility)
                if not eligibility["eligible"]:
                    raise SqliteReciprocalReviewHumanDispositionError(
                        "현재 eligibility가 없습니다."
                    )
                result = self._result(c, principal.org_id, command.receipt_id, digest)
                c.commit()
                return result
            self._authorize(principal, cycle, command.cycle_id, action, eligibility)
            if (
                cycle[1] != "awaiting_human_disposition"
                or cycle[2] != command.expected_cycle_revision
                or not eligibility["eligible"]
            ):
                raise SqliteReciprocalReviewHumanDispositionConflict(
                    "cycle state/revision 또는 eligibility가 stale입니다."
                )
            # Verify the same authority-bearing inputs immediately before the first write.
            fresh = c.execute(
                f"SELECT revision_id,state_kind,cycle_revision,policy_digest,provenance_digest FROM {_CYCLE} WHERE org_id=? AND cycle_id=?",
                (principal.org_id, command.cycle_id),
            ).fetchone()
            if fresh != cycle:
                raise SqliteReciprocalReviewHumanDispositionConflict("cycle drift가 있습니다.")
            self._authorize(principal, fresh, command.cycle_id, action, eligibility)
            now = _time(self._clock())
            next_revision = command.expected_cycle_revision + 1
            if (
                c.execute(
                    f"UPDATE {_CYCLE} SET state_kind='binding_ready',cycle_revision=? WHERE org_id=? AND cycle_id=? AND state_kind='awaiting_human_disposition' AND cycle_revision=?",
                    (
                        next_revision,
                        principal.org_id,
                        command.cycle_id,
                        command.expected_cycle_revision,
                    ),
                ).rowcount
                != 1
            ):
                raise SqliteReciprocalReviewHumanDispositionConflict("cycle CAS가 stale입니다.")
            self._fault("after_cycle_cas")
            eligibility_digest = _digest(eligibility)
            c.execute(
                f"INSERT INTO {_RECEIPT} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    principal.org_id,
                    command.receipt_id,
                    command.audit_id,
                    command.outbox_id,
                    command.idempotency_key,
                    digest,
                    command.cycle_id,
                    cycle[0],
                    command.expected_cycle_revision,
                    next_revision,
                    principal.subject_id,
                    principal.authn_context_digest,
                    action,
                    cycle[3],
                    cycle[4],
                    eligibility_digest,
                    now,
                ),
            )
            self._fault("after_receipt")
            c.execute(
                f"INSERT INTO {_RESULT} VALUES(?,?,?,?,?,?,?,?)",
                (
                    principal.org_id,
                    command.receipt_id,
                    command.cycle_id,
                    digest,
                    next_revision,
                    "binding_ready",
                    action,
                    now,
                ),
            )
            self._fault("after_result")
            c.execute(
                f"INSERT INTO {_AUDIT} VALUES(?,?,?,?,?)",
                (
                    principal.org_id,
                    command.audit_id,
                    command.receipt_id,
                    _digest(("human_disposition_audit", digest)),
                    now,
                ),
            )
            self._fault("after_audit")
            c.execute(
                f"INSERT INTO {_OUTBOX} VALUES(?,?,?,?,?)",
                (
                    principal.org_id,
                    command.outbox_id,
                    command.receipt_id,
                    _digest(("human_disposition_outbox", digest)),
                    now,
                ),
            )
            self._fault("after_outbox")
            validate_sqlite_reciprocal_review_human_disposition(c)
            c.commit()
            return SubmittedHumanDisposition(
                org_id=principal.org_id,
                cycle_id=command.cycle_id,
                receipt_id=command.receipt_id,
                command_digest=digest,
                cycle_revision=next_revision,
                action=action,
                created_at=self._clock(),
            )
        except Exception:
            if c.in_transaction:
                c.rollback()
            raise
        finally:
            c.close()

    def _authorize(
        self,
        principal: HumanPrincipal,
        cycle: tuple[object, ...],
        cycle_id: str,
        action: str,
        eligibility: dict[str, object],
    ) -> None:
        allowed = self._authority.verify(
            principal=principal,
            revision_id=str(cycle[0]),
            cycle_id=cycle_id,
            action=action,
            policy_digest=str(cycle[3]),
            provenance_digest=str(cycle[4]),
            independence_digest=_digest(eligibility["independence"]),
            now=self._clock(),
        )
        if not allowed:
            raise SqliteReciprocalReviewHumanDispositionError(
                "human disposition authority가 없습니다."
            )

    def _eligibility(
        self, c: sqlite3.Connection, org_id: str, cycle_id: str, cycle: tuple[object, ...]
    ) -> dict[str, object]:
        requirements = c.execute(
            "SELECT requirement_id,reviewer_kind,completion_rule,required_count,independence_rule,waivable FROM durable_reciprocal_review_requirements WHERE org_id=? AND cycle_id=? ORDER BY requirement_id",
            (org_id, cycle_id),
        ).fetchall()
        ai_ok = True
        independence: list[str] = []
        for requirement_id, kind, rule, count, rule_name, _waivable in requirements:
            independence.append(str(rule_name))
            if kind != "ai":
                ai_ok = False
                continue
            batches = c.execute(
                "SELECT b.batch_id FROM reciprocal_review_ai_advisory_batches b JOIN durable_reciprocal_review_runs r ON r.org_id=b.org_id AND r.review_run_id=b.review_run_id JOIN reciprocal_review_lease_reviewer_assignments a ON a.org_id=b.org_id AND a.review_run_id=b.review_run_id WHERE b.org_id=? AND a.requirement_id=? AND r.state='recorded'",
                (org_id, requirement_id),
            ).fetchall()
            required = 1 if rule == "any" else int(count)
            if len(batches) < required:
                ai_ok = False
            for (batch_id,) in batches:
                if (
                    c.execute(
                        "SELECT 1 FROM reciprocal_review_ai_advisory_findings WHERE org_id=? AND batch_id=? LIMIT 1",
                        (org_id, batch_id),
                    ).fetchone()
                    is not None
                ):
                    ai_ok = False
                signed = self._signed_batch_digest(c, org_id, batch_id)
                signature = c.execute(
                    "SELECT signature,signature_algorithm,signing_key_id,signed_payload_digest FROM reciprocal_review_ai_advisory_batches WHERE org_id=? AND batch_id=?",
                    (org_id, batch_id),
                ).fetchone()
                if signature is None or signature[1] != "hmac-sha256" or signature[3] != signed:
                    ai_ok = False
                else:
                    key = self._trusted_ai_execution_keys.get(signature[2])
                    if key is None or not hmac.compare_digest(
                        signature[0], hmac.new(key, signed.encode(), hashlib.sha256).hexdigest()
                    ):
                        ai_ok = False
        # State is checked by the write CAS; evidence eligibility itself remains re-checkable
        # for an immutable idempotent receipt after the cycle has become BindingReady.
        eligible = bool(requirements) and str(cycle[4]) != "" and ai_ok
        # human provenance is the deliberate S1b.4 boundary.
        provenance = c.execute(
            "SELECT provenance_kind FROM durable_reciprocal_review_artifact_revisions WHERE org_id=? AND revision_id=?",
            (org_id, cycle[0]),
        ).fetchone()
        eligible = eligible and provenance == ("human",)
        return {
            "eligible": eligible,
            "requirements": len(requirements),
            "independence": tuple(independence),
        }

    @staticmethod
    def _signed_batch_digest(c: sqlite3.Connection, org_id: str, batch_id: str) -> str:
        row = c.execute(
            "SELECT b.batch_id,b.review_run_id,b.model_execution_ref,b.rubric_digest,b.prompt_digest,b.input_digest,a.cycle_id,a.requirement_id,a.policy_digest,a.provenance_digest,r.content_sha256,b.deployment_digest FROM reciprocal_review_ai_advisory_batches b JOIN reciprocal_review_lease_reviewer_assignments a ON a.org_id=b.org_id AND a.review_run_id=b.review_run_id JOIN durable_reciprocal_review_artifact_revisions r ON r.org_id=b.org_id JOIN durable_reciprocal_review_cycles cy ON cy.org_id=b.org_id AND cy.cycle_id=a.cycle_id AND cy.revision_id=r.revision_id WHERE b.org_id=? AND b.batch_id=?",
            (org_id, batch_id),
        ).fetchone()
        if row is None:
            return ""
        findings = [
            {
                "finding_id": x[0],
                "criterion_ref": x[1],
                "severity": x[2],
                "evidence_digest": x[3],
                "evidence_start": x[4],
                "evidence_end": x[5],
            }
            for x in c.execute(
                "SELECT finding_id,criterion_ref,severity,evidence_digest,span_start,span_end FROM reciprocal_review_ai_advisory_findings WHERE org_id=? AND batch_id=? ORDER BY finding_id",
                (org_id, batch_id),
            )
        ]
        return _digest(
            {
                "org_id": org_id,
                "batch_id": row[0],
                "review_run_id": row[1],
                "model_execution_ref": row[2],
                "rubric_digest": row[3],
                "prompt_digest": row[4],
                "input_digest": row[5],
                "findings": findings,
                "cycle_id": row[6],
                "requirement_id": row[7],
                "policy_digest": row[8],
                "provenance_digest": row[9],
                "content_digest": row[10],
                "deployment_digest": row[11],
            }
        )

    @staticmethod
    def _result(
        c: sqlite3.Connection, org_id: str, receipt_id: str, digest: str
    ) -> SubmittedHumanDisposition:
        row = c.execute(
            f"SELECT cycle_id,command_digest,cycle_revision,action,created_at FROM {_RESULT} WHERE org_id=? AND receipt_id=?",
            (org_id, receipt_id),
        ).fetchone()
        if row is None or row[1] != digest:
            raise SqliteReciprocalReviewHumanDispositionError("immutable result가 없습니다.")
        return SubmittedHumanDisposition(
            org_id=org_id,
            cycle_id=row[0],
            receipt_id=receipt_id,
            command_digest=row[1],
            cycle_revision=row[2],
            action=row[3],
            created_at=datetime.fromisoformat(row[4].replace("Z", "+00:00")),
        )

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)


def create_sqlite_reciprocal_review_human_disposition_uow(
    path: str | Path,
    *,
    trusted_human_authority_keys: Mapping[str, bytes],
    trusted_ai_execution_keys: Mapping[str, bytes],
    clock: Callable[[], datetime],
    fault_injector: Callable[[str], None] | None = None,
) -> HumanDispositionUnitOfWork:
    human_keys = _trusted_key_copy(
        trusted_human_authority_keys, "trusted human authority key registry"
    )
    ai_keys = _trusted_key_copy(trusted_ai_execution_keys, "trusted AI execution key registry")
    return _SqliteReciprocalReviewHumanDispositionUnitOfWork(
        path,
        trusted_human_authority_keys=human_keys,
        trusted_ai_execution_keys=ai_keys,
        clock=clock,
        fault_injector=fault_injector,
    )
