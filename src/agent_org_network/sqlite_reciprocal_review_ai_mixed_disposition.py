"""P18 S1b.5b: v5 companion for AI/mixed human disposition.

v4 remains the sole owner of upstream human-review evidence.  This module never
updates it (or a source aggregate): it records a separately owned BindingReady
intent after rebuilding the immutable evidence graph inside one DB-time UoW.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from agent_org_network.reciprocal_review import (
    HumanPrincipal,
    SubmitAiMixedHumanDisposition,
    SubmittedAiMixedHumanDisposition,
)
from agent_org_network.sqlite_reciprocal_review_assignment_terminal import (
    COMPONENT_ID as V4_COMPONENT_ID,
    validate_sqlite_reciprocal_review_assignment_terminal,
)
from agent_org_network.sqlite_reciprocal_review_ai_batches import (
    SqliteReciprocalReviewAiBatchError,
    validate_sqlite_reciprocal_review_ai_batches,
)

COMPONENT_ID = "durable_reciprocal_review_ledger_v5"
_MANIFEST = "schema_component_manifests"
_CYCLE = "durable_reciprocal_review_cycles_v5"
_OWNERSHIP = "reciprocal_review_v5_cycle_ownership"
_RECEIPT = "reciprocal_review_v5_human_disposition_receipts"
_RESULT = "reciprocal_review_v5_human_disposition_results"
_AUDIT = "reciprocal_review_v5_human_disposition_audit"
_OUTBOX = "reciprocal_review_v5_human_disposition_outbox"
_TABLES = (_CYCLE, _OWNERSHIP, _RECEIPT, _RESULT, _AUDIT, _OUTBOX)
_DDLS = {
    _CYCLE: "CREATE TABLE durable_reciprocal_review_cycles_v5 (org_id TEXT NOT NULL,cycle_id TEXT NOT NULL,revision_id TEXT NOT NULL,upstream_revision INTEGER NOT NULL CHECK(upstream_revision>=1),state_kind TEXT NOT NULL CHECK(state_kind IN ('awaiting_human_disposition','binding_ready')),result_revision INTEGER NOT NULL CHECK(result_revision>=1),policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),provenance_digest TEXT NOT NULL CHECK(length(provenance_digest)=64),upstream_snapshot_digest TEXT NOT NULL CHECK(length(upstream_snapshot_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,cycle_id))",
    _OWNERSHIP: "CREATE TABLE reciprocal_review_v5_cycle_ownership (org_id TEXT NOT NULL,cycle_id TEXT NOT NULL,owner TEXT NOT NULL CHECK(owner='v5'),created_at TEXT NOT NULL,PRIMARY KEY(org_id,cycle_id),FOREIGN KEY(org_id,cycle_id) REFERENCES durable_reciprocal_review_cycles_v5(org_id,cycle_id))",
    _RECEIPT: "CREATE TABLE reciprocal_review_v5_human_disposition_receipts (org_id TEXT NOT NULL,receipt_id TEXT NOT NULL,audit_id TEXT NOT NULL,outbox_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,command_digest TEXT NOT NULL CHECK(length(command_digest)=64),cycle_id TEXT NOT NULL,revision_id TEXT NOT NULL,expected_upstream_revision INTEGER NOT NULL CHECK(expected_upstream_revision>=1),result_revision INTEGER NOT NULL CHECK(result_revision>=1),subject_id TEXT NOT NULL,authn_context_digest TEXT NOT NULL CHECK(length(authn_context_digest)=64),action TEXT NOT NULL CHECK(action IN ('approve_revision','request_changes','reject_revision')),policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),provenance_digest TEXT NOT NULL CHECK(length(provenance_digest)=64),upstream_snapshot_digest TEXT NOT NULL CHECK(length(upstream_snapshot_digest)=64),ai_eligibility_digest TEXT NOT NULL CHECK(length(ai_eligibility_digest)=64),human_eligibility_digest TEXT NOT NULL CHECK(length(human_eligibility_digest)=64),overall_eligibility_digest TEXT NOT NULL CHECK(length(overall_eligibility_digest)=64),independence_digest TEXT NOT NULL CHECK(length(independence_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,receipt_id),UNIQUE(org_id,idempotency_key),UNIQUE(org_id,audit_id),UNIQUE(org_id,outbox_id),UNIQUE(org_id,cycle_id),FOREIGN KEY(org_id,cycle_id) REFERENCES durable_reciprocal_review_cycles_v5(org_id,cycle_id))",
    _RESULT: "CREATE TABLE reciprocal_review_v5_human_disposition_results (org_id TEXT NOT NULL,receipt_id TEXT NOT NULL,cycle_id TEXT NOT NULL,command_digest TEXT NOT NULL CHECK(length(command_digest)=64),upstream_revision INTEGER NOT NULL CHECK(upstream_revision>=1),result_revision INTEGER NOT NULL CHECK(result_revision>=1),cycle_state TEXT NOT NULL CHECK(cycle_state='binding_ready'),action TEXT NOT NULL CHECK(action IN ('approve_revision','request_changes','reject_revision')),created_at TEXT NOT NULL,PRIMARY KEY(org_id,receipt_id),FOREIGN KEY(org_id,receipt_id) REFERENCES reciprocal_review_v5_human_disposition_receipts(org_id,receipt_id))",
    _AUDIT: "CREATE TABLE reciprocal_review_v5_human_disposition_audit (org_id TEXT NOT NULL,audit_id TEXT NOT NULL,receipt_id TEXT NOT NULL,event_digest TEXT NOT NULL CHECK(length(event_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,audit_id),UNIQUE(org_id,receipt_id),FOREIGN KEY(org_id,receipt_id) REFERENCES reciprocal_review_v5_human_disposition_receipts(org_id,receipt_id))",
    _OUTBOX: "CREATE TABLE reciprocal_review_v5_human_disposition_outbox (org_id TEXT NOT NULL,outbox_id TEXT NOT NULL,receipt_id TEXT NOT NULL,payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,outbox_id),UNIQUE(org_id,receipt_id),FOREIGN KEY(org_id,receipt_id) REFERENCES reciprocal_review_v5_human_disposition_receipts(org_id,receipt_id))",
}
_TRIGGERS = {
    f"{table}_no_{verb}": f"CREATE TRIGGER {table}_no_{verb} BEFORE {verb.upper()} ON {table} BEGIN SELECT RAISE(ABORT,'immutable reciprocal review v5'); END"
    for table in _TABLES[1:]
    for verb in ("update", "delete")
}
_TRIGGERS[f"{_CYCLE}_legal_update"] = (
    f"CREATE TRIGGER {_CYCLE}_legal_update BEFORE UPDATE ON {_CYCLE} FOR EACH ROW WHEN NOT (OLD.state_kind='awaiting_human_disposition' AND NEW.state_kind='binding_ready' AND NEW.result_revision=OLD.result_revision+1 AND OLD.org_id=NEW.org_id AND OLD.cycle_id=NEW.cycle_id AND OLD.revision_id=NEW.revision_id AND OLD.upstream_revision=NEW.upstream_revision AND OLD.policy_digest=NEW.policy_digest AND OLD.provenance_digest=NEW.provenance_digest AND OLD.upstream_snapshot_digest=NEW.upstream_snapshot_digest AND OLD.created_at=NEW.created_at) BEGIN SELECT RAISE(ABORT,'illegal reciprocal review v5 transition'); END"
)
_TRIGGERS[f"{_CYCLE}_no_delete"] = (
    f"CREATE TRIGGER {_CYCLE}_no_delete BEFORE DELETE ON {_CYCLE} BEGIN SELECT RAISE(ABORT,'immutable reciprocal review v5'); END"
)


class SqliteReciprocalReviewAiMixedDispositionError(RuntimeError):
    pass


class SqliteReciprocalReviewAiMixedDispositionConflict(
    SqliteReciprocalReviewAiMixedDispositionError
):
    pass


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _time(value: datetime) -> str:
    if (
        value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
        or value.microsecond % 1000
    ):
        raise SqliteReciprocalReviewAiMixedDispositionError(
            "DB time은 canonical UTC milliseconds여야 합니다."
        )
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _db_now(c: sqlite3.Connection) -> datetime:
    return datetime.fromisoformat(
        c.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        .fetchone()[0]
        .replace("Z", "+00:00")
    )


def _same(a: object, b: str) -> bool:
    return isinstance(a, str) and " ".join(a.split()) == " ".join(b.split())


def _catalog() -> dict[str, object]:
    return {
        "tables": [{"name": n, "sql": " ".join(s.split())} for n, s in _DDLS.items()],
        "triggers": [{"name": n, "sql": " ".join(s.split())} for n, s in _TRIGGERS.items()],
    }


def _snapshot(row: tuple[object, ...]) -> dict[str, object]:
    return {
        "revision_id": row[0],
        "state_kind": row[1],
        "cycle_revision": row[2],
        "policy_digest": row[3],
        "provenance_digest": row[4],
    }


def validate_sqlite_reciprocal_review_ai_mixed_disposition(c: sqlite3.Connection) -> None:
    validate_sqlite_reciprocal_review_assignment_terminal(c)
    manifest = _canonical(
        {"component_id": COMPONENT_ID, "schema_version": 5, "catalog": _catalog()}
    )
    if c.execute(
        f"SELECT schema_version,manifest_json,manifest_sha256 FROM {_MANIFEST} WHERE component_id=?",
        (COMPONENT_ID,),
    ).fetchone() != (5, manifest, hashlib.sha256(manifest.encode()).hexdigest()):
        raise SqliteReciprocalReviewAiMixedDispositionError(
            "reciprocal review v5 manifest가 canonical하지 않습니다."
        )
    actual = {
        x[0]
        for x in c.execute(
            "SELECT name FROM sqlite_schema WHERE name LIKE 'durable_reciprocal_review_cycles_v5%' OR name LIKE 'reciprocal_review_v5_%'"
        )
    }
    if (
        actual != set(_TABLES) | set(_TRIGGERS)
        or any(
            not _same(
                c.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (n,)
                ).fetchone()[0],
                sql,
            )
            for n, sql in _DDLS.items()
        )
        or any(
            not _same(
                c.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (n,)
                ).fetchone()[0],
                sql,
            )
            for n, sql in _TRIGGERS.items()
        )
    ):
        raise SqliteReciprocalReviewAiMixedDispositionError(
            "reciprocal review v5 catalog가 canonical하지 않습니다."
        )
    cycles = set(c.execute(f"SELECT org_id,cycle_id FROM {_CYCLE}"))
    ownership = set(c.execute(f"SELECT org_id,cycle_id FROM {_OWNERSHIP}"))
    if cycles != ownership:
        raise SqliteReciprocalReviewAiMixedDispositionError(
            "v5 ownership graph가 exact 1:1이 아닙니다."
        )
    receipts = set(c.execute(f"SELECT org_id,receipt_id FROM {_RECEIPT}"))
    if any(
        set(c.execute(q)) != receipts
        for q in (
            f"SELECT org_id,receipt_id FROM {_RESULT}",
            f"SELECT org_id,receipt_id FROM {_AUDIT}",
            f"SELECT org_id,receipt_id FROM {_OUTBOX}",
        )
    ):
        raise SqliteReciprocalReviewAiMixedDispositionError(
            "v5 disposition evidence graph가 bijection이 아닙니다."
        )
    for (
        org,
        cycle,
        revision,
        upstream,
        state,
        result_rev,
        policy,
        provenance,
        snapshot,
        _created,
    ) in c.execute(
        f"SELECT org_id,cycle_id,revision_id,upstream_revision,state_kind,result_revision,policy_digest,provenance_digest,upstream_snapshot_digest,created_at FROM {_CYCLE}"
    ):
        v4 = c.execute(
            "SELECT revision_id,state_kind,cycle_revision,policy_digest,provenance_digest FROM durable_reciprocal_review_cycles_v4 WHERE org_id=? AND cycle_id=?",
            (org, cycle),
        ).fetchone()
        kind = c.execute(
            "SELECT provenance_kind FROM durable_reciprocal_review_artifact_revisions WHERE org_id=? AND revision_id=?",
            (org, revision),
        ).fetchone()
        if (
            v4 is None
            or kind is None
            or kind[0] not in {"ai", "mixed"}
            or v4[0] != revision
            or _digest(_snapshot(v4)) != snapshot
            or (v4[1], v4[2]) != ("awaiting_human_disposition", upstream)
            or (policy, provenance) != (v4[3], v4[4])
        ):
            raise SqliteReciprocalReviewAiMixedDispositionError(
                "forged v5 upstream snapshot이 있습니다."
            )
        if state == "binding_ready":
            linked = c.execute(
                f"SELECT count(*) FROM {_RECEIPT} WHERE org_id=? AND cycle_id=? AND result_revision=?",
                (org, cycle, result_rev),
            ).fetchone()
            if linked != (1,) or result_rev != upstream + 1:
                raise SqliteReciprocalReviewAiMixedDispositionError(
                    "v5 BindingReady receipt가 canonical하지 않습니다."
                )
    for org, receipt, audit, outbox, digest, cycle, expected, result_rev, action, created in c.execute(
        f"SELECT org_id,receipt_id,audit_id,outbox_id,command_digest,cycle_id,expected_upstream_revision,result_revision,action,created_at FROM {_RECEIPT}"
    ):
        result = c.execute(
            f"SELECT cycle_id,command_digest,upstream_revision,result_revision,cycle_state,action,created_at FROM {_RESULT} WHERE org_id=? AND receipt_id=?",
            (org, receipt),
        ).fetchone()
        audit_row = c.execute(
            f"SELECT receipt_id,event_digest,created_at FROM {_AUDIT} WHERE org_id=? AND audit_id=?",
            (org, audit),
        ).fetchone()
        outbox_row = c.execute(
            f"SELECT receipt_id,payload_digest,created_at FROM {_OUTBOX} WHERE org_id=? AND outbox_id=?",
            (org, outbox),
        ).fetchone()
        if (
            result != (cycle, digest, expected, result_rev, "binding_ready", action, created)
            or audit_row != (receipt, _digest(("v5_disposition_audit", digest)), created)
            or outbox_row != (receipt, _digest(("v5_disposition_outbox", digest)), created)
        ):
            raise SqliteReciprocalReviewAiMixedDispositionError(
                "forged v5 disposition evidence graph가 있습니다."
            )
    if c.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SqliteReciprocalReviewAiMixedDispositionError("v5 FK가 canonical하지 않습니다.")


def migrate_sqlite_reciprocal_review_ai_mixed_disposition_v5(
    c: sqlite3.Connection, *, fault_injector: Callable[[str], None] | None = None
) -> None:
    try:
        c.execute("BEGIN IMMEDIATE")
        if (
            c.execute(
                f"SELECT 1 FROM {_MANIFEST} WHERE component_id=?", (V4_COMPONENT_ID,)
            ).fetchone()
            is None
        ):
            raise SqliteReciprocalReviewAiMixedDispositionError(
                "v5에는 explicit v4 snapshot이 필요합니다."
            )
        if (
            c.execute(f"SELECT 1 FROM {_MANIFEST} WHERE component_id=?", (COMPONENT_ID,)).fetchone()
            is None
        ):
            for name, ddl in _DDLS.items():
                c.execute(ddl)
                if fault_injector is not None:
                    fault_injector(f"after_{name}")
            for ddl in _TRIGGERS.values():
                c.execute(ddl)
            manifest = _canonical(
                {"component_id": COMPONENT_ID, "schema_version": 5, "catalog": _catalog()}
            )
            c.execute(
                f"INSERT INTO {_MANIFEST} VALUES(?,?,?,?)",
                (COMPONENT_ID, 5, manifest, hashlib.sha256(manifest.encode()).hexdigest()),
            )
        validate_sqlite_reciprocal_review_ai_mixed_disposition(c)
        c.commit()
    except Exception:
        if c.in_transaction:
            c.rollback()
        raise


def provision_sqlite_reciprocal_review_v5_cycle(
    c: sqlite3.Connection, *, org_id: str, cycle_id: str
) -> None:
    """Explicitly attach one already-Awaiting v4 cycle; never backfill legacy evidence."""
    validate_sqlite_reciprocal_review_ai_mixed_disposition(c)
    if (
        c.execute(
            f"SELECT 1 FROM {_CYCLE} WHERE org_id=? AND cycle_id=?", (org_id, cycle_id)
        ).fetchone()
        is not None
    ):
        return
    row = c.execute(
        "SELECT revision_id,state_kind,cycle_revision,policy_digest,provenance_digest,created_at FROM durable_reciprocal_review_cycles_v4 WHERE org_id=? AND cycle_id=?",
        (org_id, cycle_id),
    ).fetchone()
    if row is None or row[1] != "awaiting_human_disposition":
        raise SqliteReciprocalReviewAiMixedDispositionError(
            "v5 provision에는 v4 Awaiting snapshot이 필요합니다."
        )
    kind = c.execute(
        "SELECT provenance_kind FROM durable_reciprocal_review_artifact_revisions WHERE org_id=? AND revision_id=?",
        (org_id, row[0]),
    ).fetchone()
    if kind is None or kind[0] not in {"ai", "mixed"}:
        raise SqliteReciprocalReviewAiMixedDispositionError(
            "v5는 ai/mixed provenance만 소유합니다."
        )
    snap = _digest(_snapshot(row[:5]))
    c.execute(
        f"INSERT INTO {_CYCLE} VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            org_id,
            cycle_id,
            row[0],
            row[2],
            "awaiting_human_disposition",
            row[2],
            row[3],
            row[4],
            snap,
            row[5],
        ),
    )
    c.execute(f"INSERT INTO {_OWNERSHIP} VALUES(?,?,?,?)", (org_id, cycle_id, "v5", row[5]))


def ai_mixed_disposition_authority_payload(
    *,
    org_id: str,
    principal: HumanPrincipal,
    revision_id: str,
    cycle_id: str,
    action: str,
    policy_digest: str,
    provenance_digest: str,
    upstream_snapshot_digest: str,
    expected_upstream_revision: int,
    ai_eligibility_digest: str,
    human_eligibility_digest: str,
    overall_eligibility_digest: str,
    independence_digest: str,
) -> dict[str, object]:
    issued = principal.authenticated_at
    expires = issued + timedelta(minutes=5)
    return {
        "org_id": org_id,
        "subject_id": principal.subject_id,
        "issued_at": _time(issued),
        "expires_at": _time(expires),
        "revision_id": revision_id,
        "cycle_id": cycle_id,
        "action": action,
        "policy_digest": policy_digest,
        "provenance_digest": provenance_digest,
        "upstream_snapshot_digest": upstream_snapshot_digest,
        "expected_upstream_revision": expected_upstream_revision,
        "ai_eligibility_digest": ai_eligibility_digest,
        "human_eligibility_digest": human_eligibility_digest,
        "overall_eligibility_digest": overall_eligibility_digest,
        "independence_digest": independence_digest,
    }


class AiMixedHumanDispositionUnitOfWork(Protocol):
    def submit(
        self, principal: HumanPrincipal, command: SubmitAiMixedHumanDisposition
    ) -> SubmittedAiMixedHumanDisposition: ...


class _Uow:
    def __init__(
        self,
        path: str | Path,
        human_keys: Mapping[str, bytes],
        ai_keys: Mapping[str, bytes],
        fault: Callable[[str], None] | None,
    ) -> None:
        self._path, self._human, self._ai, self._fault = (
            Path(path),
            dict(human_keys),
            dict(ai_keys),
            fault,
        )

    def submit(
        self, principal: HumanPrincipal, command: SubmitAiMixedHumanDisposition
    ) -> SubmittedAiMixedHumanDisposition:
        c = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("BEGIN IMMEDIATE")
            validate_sqlite_reciprocal_review_ai_mixed_disposition(c)
            try:
                validate_sqlite_reciprocal_review_ai_batches(
                    c, trusted_execution_keys=self._ai
                )
            except SqliteReciprocalReviewAiBatchError as error:
                raise SqliteReciprocalReviewAiMixedDispositionError(
                    "v5 AI batch evidence graph가 current하지 않습니다."
                ) from error
            now = _db_now(c)
            cycle = c.execute(
                f"SELECT revision_id,upstream_revision,state_kind,result_revision,policy_digest,provenance_digest,upstream_snapshot_digest FROM {_CYCLE} WHERE org_id=? AND cycle_id=?",
                (principal.org_id, command.cycle_id),
            ).fetchone()
            if cycle is None:
                raise SqliteReciprocalReviewAiMixedDispositionError("v5 owned cycle이 없습니다.")
            evidence = self._evidence(
                c, principal.org_id, command.cycle_id, cycle, principal.subject_id
            )
            action = command.disposition.kind
            semantic = {
                "principal": {
                    "subject_id": principal.subject_id,
                    "authn_context_digest": principal.authn_context_digest,
                },
                "command": command.model_dump(mode="json"),
                "revision_id": cycle[0],
                "upstream_snapshot": cycle[6],
                "evidence": evidence,
            }
            digest = _digest(semantic)
            old = c.execute(
                f"SELECT command_digest FROM {_RECEIPT} WHERE org_id=? AND idempotency_key=?",
                (principal.org_id, command.idempotency_key),
            ).fetchone()
            if old is not None:
                if old[0] != digest:
                    raise SqliteReciprocalReviewAiMixedDispositionConflict(
                        "idempotency semantic command가 다릅니다."
                    )
                self._authorize(principal, command.cycle_id, cycle, action, evidence, now)
                if not evidence["eligible"]:
                    raise SqliteReciprocalReviewAiMixedDispositionError(
                        "현재 immutable evidence eligibility가 없습니다."
                    )
                out = self._result(c, principal.org_id, command.receipt_id, digest)
                c.commit()
                return out
            self._authorize(principal, command.cycle_id, cycle, action, evidence, now)
            if (
                cycle[2] != "awaiting_human_disposition"
                or cycle[1] != command.expected_upstream_revision
                or not evidence["eligible"]
            ):
                raise SqliteReciprocalReviewAiMixedDispositionConflict(
                    "upstream state/revision 또는 evidence가 stale입니다."
                )
            v4 = c.execute(
                "SELECT revision_id,state_kind,cycle_revision,policy_digest,provenance_digest FROM durable_reciprocal_review_cycles_v4 WHERE org_id=? AND cycle_id=?",
                (principal.org_id, command.cycle_id),
            ).fetchone()
            if (
                v4 is None
                or _digest(_snapshot(v4)) != cycle[6]
                or v4[1] != "awaiting_human_disposition"
                or v4[2] != cycle[1]
            ):
                raise SqliteReciprocalReviewAiMixedDispositionConflict(
                    "v4 upstream drift가 있습니다."
                )
            self._authorize(principal, command.cycle_id, cycle, action, evidence, now)
            result_rev = cycle[3] + 1
            if (
                c.execute(
                    f"UPDATE {_CYCLE} SET state_kind='binding_ready',result_revision=? WHERE org_id=? AND cycle_id=? AND state_kind='awaiting_human_disposition' AND result_revision=?",
                    (result_rev, principal.org_id, command.cycle_id, cycle[3]),
                ).rowcount
                != 1
            ):
                raise SqliteReciprocalReviewAiMixedDispositionConflict(
                    "v5 cycle CAS가 stale입니다."
                )
            self._hit("after_cycle_cas")
            c.execute(
                f"INSERT INTO {_RECEIPT} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    principal.org_id,
                    command.receipt_id,
                    command.audit_id,
                    command.outbox_id,
                    command.idempotency_key,
                    digest,
                    command.cycle_id,
                    cycle[0],
                    cycle[1],
                    result_rev,
                    principal.subject_id,
                    principal.authn_context_digest,
                    action,
                    cycle[4],
                    cycle[5],
                    cycle[6],
                    evidence["ai_digest"],
                    evidence["human_digest"],
                    evidence["overall_digest"],
                    evidence["independence_digest"],
                    _time(now),
                ),
            )
            self._hit("after_receipt")
            c.execute(
                f"INSERT INTO {_RESULT} VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    principal.org_id,
                    command.receipt_id,
                    command.cycle_id,
                    digest,
                    cycle[1],
                    result_rev,
                    "binding_ready",
                    action,
                    _time(now),
                ),
            )
            c.execute(
                f"INSERT INTO {_AUDIT} VALUES(?,?,?,?,?)",
                (
                    principal.org_id,
                    command.audit_id,
                    command.receipt_id,
                    _digest(("v5_disposition_audit", digest)),
                    _time(now),
                ),
            )
            c.execute(
                f"INSERT INTO {_OUTBOX} VALUES(?,?,?,?,?)",
                (
                    principal.org_id,
                    command.outbox_id,
                    command.receipt_id,
                    _digest(("v5_disposition_outbox", digest)),
                    _time(now),
                ),
            )
            self._hit("after_outbox")
            validate_sqlite_reciprocal_review_ai_mixed_disposition(c)
            c.commit()
            return SubmittedAiMixedHumanDisposition(
                org_id=principal.org_id,
                cycle_id=command.cycle_id,
                receipt_id=command.receipt_id,
                command_digest=digest,
                upstream_revision=cycle[1],
                result_revision=result_rev,
                action=action,
                created_at=now,
            )
        except Exception:
            if c.in_transaction:
                c.rollback()
            raise
        finally:
            c.close()

    def _hit(self, point: str) -> None:
        if self._fault:
            self._fault(point)

    def _authorize(
        self,
        p: HumanPrincipal,
        cycle_id: str,
        cycle: tuple[object, ...],
        action: str,
        e: dict[str, object],
        now: datetime,
    ) -> None:
        key = self._human.get(p.subject_id)
        payload = ai_mixed_disposition_authority_payload(
            org_id=p.org_id,
            principal=p,
            revision_id=str(cycle[0]),
            cycle_id=cycle_id,
            action=action,
            policy_digest=str(cycle[4]),
            provenance_digest=str(cycle[5]),
            upstream_snapshot_digest=str(cycle[6]),
            expected_upstream_revision=cast(int, cycle[1]),
            ai_eligibility_digest=str(e["ai_digest"]),
            human_eligibility_digest=str(e["human_digest"]),
            overall_eligibility_digest=str(e["overall_digest"]),
            independence_digest=str(e["independence_digest"]),
        )
        if (
            key is None
            or now < p.authenticated_at
            or now > p.authenticated_at + timedelta(minutes=5)
            or not hmac.compare_digest(
                p.authn_context_digest,
                hmac.new(key, _canonical(payload).encode(), hashlib.sha256).hexdigest(),
            )
        ):
            raise SqliteReciprocalReviewAiMixedDispositionError(
                "v5 disposition authority가 없습니다."
            )

    def _evidence(
        self,
        c: sqlite3.Connection,
        org: str,
        cycle_id: str,
        cycle: tuple[object, ...],
        disposition_subject: str,
    ) -> dict[str, object]:
        reqs = c.execute(
            "SELECT requirement_id,reviewer_kind,completion_rule,required_count,independence_rule FROM durable_reciprocal_review_requirements WHERE org_id=? AND cycle_id=? ORDER BY requirement_id",
            (org, cycle_id),
        ).fetchall()
        ai: list[object] = []
        human: list[object] = []
        rules: list[str] = []
        good = bool(reqs)
        for req, kind, rule, k, ind in reqs:
            rules.append(ind)
            if kind == "ai":
                batches = c.execute(
                    "SELECT b.batch_id,b.signature,b.signing_key_id,b.signature_algorithm,b.signed_payload_digest FROM reciprocal_review_ai_advisory_batches b JOIN reciprocal_review_lease_reviewer_assignments a ON a.org_id=b.org_id AND a.review_run_id=b.review_run_id JOIN durable_reciprocal_review_runs r ON r.org_id=b.org_id AND r.review_run_id=b.review_run_id WHERE b.org_id=? AND a.cycle_id=? AND a.requirement_id=? AND r.state='recorded' ORDER BY b.batch_id",
                    (org, cycle_id, req),
                ).fetchall()
                valid: list[str] = []
                for bid, sig, kid, alg, signed in batches:
                    sd = self._signed(c, org, bid)
                    finding = c.execute(
                        "SELECT 1 FROM reciprocal_review_ai_advisory_findings WHERE org_id=? AND batch_id=? LIMIT 1",
                        (org, bid),
                    ).fetchone()
                    if (
                        alg == "hmac-sha256"
                        and signed == sd
                        and kid in self._ai
                        and finding is None
                        and hmac.compare_digest(
                            sig, hmac.new(self._ai[kid], sd.encode(), hashlib.sha256).hexdigest()
                        )
                    ):
                        valid.append(cast(str, bid))
                need = 1 if rule == "any" else int(k)
                good = good and len(set(valid)) >= need
                ai.append({"requirement_id": req, "required": need, "batches": tuple(valid)})
            elif kind == "human":
                candidates = c.execute(
                    "SELECT assignment_id,reviewer_ref FROM reciprocal_review_v4_reviewer_assignments WHERE org_id=? AND cycle_id=? AND requirement_id=? ORDER BY ordinal",
                    (org, cycle_id, req),
                ).fetchall()
                terminals = c.execute(
                    "SELECT assignment_id FROM reciprocal_review_v4_human_terminal_receipts WHERE org_id=? AND cycle_id=? AND requirement_id=? ORDER BY assignment_id",
                    (org, cycle_id, req),
                ).fetchall()
                need = int(k)
                n = len(candidates)
                good = (
                    good
                    and n > 0
                    and (
                        (rule == "all" and need == n)
                        or (rule == "any" and need == 1)
                        or (rule == "quorum" and 1 <= need <= n)
                    )
                    and len(set(terminals)) >= need
                )
                human.append(
                    {
                        "requirement_id": req,
                        "required": need,
                        "assignments": tuple(candidates),
                        "terminals": tuple(terminals),
                    }
                )
            else:
                good = False
        contributor = {
            x[0]
            for x in c.execute(
                "SELECT principal_ref FROM durable_reciprocal_review_provenance_events WHERE org_id=? AND revision_id=? AND principal_kind='human'",
                (org, cycle[0]),
            )
        }
        independent = disposition_subject not in contributor
        good = good and independent
        independence = _digest(
            {
                "rules": tuple(rules),
                "contributors": tuple(sorted(contributor)),
                "disposition_subject": disposition_subject,
                "independent": independent,
            }
        )
        return {
            "cycle_id": cycle_id,
            "eligible": good,
            "ai_digest": _digest(ai),
            "human_digest": _digest(human),
            "overall_digest": _digest(
                {"ai": _digest(ai), "human": _digest(human), "eligible": good}
            ),
            "independence_digest": independence,
        }

    def _signed(self, c: sqlite3.Connection, org: str, bid: str) -> str:
        row = c.execute(
            "SELECT b.batch_id,b.review_run_id,b.model_execution_ref,b.rubric_digest,b.prompt_digest,b.input_digest,a.cycle_id,a.requirement_id,a.policy_digest,a.provenance_digest,r.content_sha256,b.deployment_digest FROM reciprocal_review_ai_advisory_batches b JOIN reciprocal_review_lease_reviewer_assignments a ON a.org_id=b.org_id AND a.review_run_id=b.review_run_id JOIN durable_reciprocal_review_artifact_revisions r ON r.org_id=b.org_id JOIN durable_reciprocal_review_cycles cy ON cy.org_id=b.org_id AND cy.cycle_id=a.cycle_id AND cy.revision_id=r.revision_id WHERE b.org_id=? AND b.batch_id=?",
            (org, bid),
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
                (org, bid),
            )
        ]
        return _digest(
            {
                "org_id": org,
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

    def _result(
        self, c: sqlite3.Connection, org: str, receipt: str, digest: str
    ) -> SubmittedAiMixedHumanDisposition:
        row = c.execute(
            f"SELECT cycle_id,command_digest,upstream_revision,result_revision,action,created_at FROM {_RESULT} WHERE org_id=? AND receipt_id=?",
            (org, receipt),
        ).fetchone()
        if row is None or row[1] != digest:
            raise SqliteReciprocalReviewAiMixedDispositionError("immutable v5 result가 없습니다.")
        return SubmittedAiMixedHumanDisposition(
            org_id=org,
            cycle_id=row[0],
            receipt_id=receipt,
            command_digest=row[1],
            upstream_revision=row[2],
            result_revision=row[3],
            action=row[4],
            created_at=datetime.fromisoformat(row[5].replace("Z", "+00:00")),
        )


def _copy_keys(value: Mapping[str, bytes], name: str) -> dict[str, bytes]:
    copied = dict(value)
    if not copied or any(
        type(k) is not str or not k or type(v) is not bytes or not v for k, v in copied.items()
    ):
        raise ValueError(f"{name}가 유효하지 않습니다.")
    return copied


def create_sqlite_reciprocal_review_ai_mixed_disposition_uow(
    path: str | Path,
    *,
    trusted_human_disposition_authority_keys: Mapping[str, bytes],
    trusted_ai_execution_keys: Mapping[str, bytes],
    fault_injector: Callable[[str], None] | None = None,
) -> AiMixedHumanDispositionUnitOfWork:
    return _Uow(
        path,
        _copy_keys(
            trusted_human_disposition_authority_keys, "trusted human disposition authority keys"
        ),
        _copy_keys(trusted_ai_execution_keys, "trusted AI execution keys"),
        fault_injector,
    )
