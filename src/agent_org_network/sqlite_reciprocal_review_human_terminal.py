"""P18 S1b.5a: AI/mixed human Review Run finding-free terminal evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from agent_org_network.reciprocal_review import (
    HumanPrincipal,
    RecordHumanReviewTerminal,
    RecordedHumanReviewTerminal,
)
from agent_org_network.sqlite_durable_reciprocal_review import (
    validate_sqlite_durable_reciprocal_review_ledger,
)
from agent_org_network.sqlite_reciprocal_review_lease import validate_sqlite_reciprocal_review_lease

COMPONENT_ID = "durable_reciprocal_review_ledger_v3"
_MANIFEST = "schema_component_manifests"
_CYCLE = "durable_reciprocal_review_cycles_v3"
_RECEIPT = "reciprocal_review_human_terminal_receipts"
_RESULT = "reciprocal_review_human_terminal_results"
_AUDIT = "reciprocal_review_human_terminal_audit"
_OUTBOX = "reciprocal_review_human_terminal_outbox"
_TABLES = (_CYCLE, _RECEIPT, _RESULT, _AUDIT, _OUTBOX)
_DDLS = {
    _CYCLE: "CREATE TABLE durable_reciprocal_review_cycles_v3 (org_id TEXT NOT NULL,cycle_id TEXT NOT NULL,revision_id TEXT NOT NULL,cycle_no INTEGER NOT NULL,state_kind TEXT NOT NULL CHECK(state_kind IN ('review_open','awaiting_human_disposition','binding_ready','binding_pending','bound','superseded')),active INTEGER NOT NULL CHECK(active IN (0,1)),provenance_digest TEXT NOT NULL CHECK(length(provenance_digest)=64),policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),created_at TEXT NOT NULL,cycle_revision INTEGER NOT NULL CHECK(cycle_revision>=1),PRIMARY KEY(org_id,cycle_id),FOREIGN KEY(org_id,revision_id) REFERENCES durable_reciprocal_review_artifact_revisions(org_id,revision_id))",
    _RECEIPT: "CREATE TABLE reciprocal_review_human_terminal_receipts (org_id TEXT NOT NULL,receipt_id TEXT NOT NULL,audit_id TEXT NOT NULL,outbox_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,command_digest TEXT NOT NULL CHECK(length(command_digest)=64),cycle_id TEXT NOT NULL,revision_id TEXT NOT NULL,requirement_id TEXT NOT NULL,review_run_id TEXT NOT NULL,lease_epoch INTEGER NOT NULL,subject_id TEXT NOT NULL,authn_context_digest TEXT NOT NULL CHECK(length(authn_context_digest)=64),assignment_digest TEXT NOT NULL CHECK(length(assignment_digest)=64),policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),provenance_digest TEXT NOT NULL CHECK(length(provenance_digest)=64),contributor_digest TEXT NOT NULL CHECK(length(contributor_digest)=64),independence_digest TEXT NOT NULL CHECK(length(independence_digest)=64),content_digest TEXT NOT NULL CHECK(length(content_digest)=64),rubric_digest TEXT NOT NULL CHECK(length(rubric_digest)=64),input_digest TEXT NOT NULL CHECK(length(input_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,receipt_id),UNIQUE(org_id,idempotency_key),UNIQUE(org_id,audit_id),UNIQUE(org_id,outbox_id))",
    _RESULT: "CREATE TABLE reciprocal_review_human_terminal_results (org_id TEXT NOT NULL,receipt_id TEXT NOT NULL,cycle_id TEXT NOT NULL,requirement_id TEXT NOT NULL,review_run_id TEXT NOT NULL,command_digest TEXT NOT NULL CHECK(length(command_digest)=64),cycle_revision INTEGER NOT NULL,cycle_state TEXT NOT NULL CHECK(cycle_state IN ('review_open','awaiting_human_disposition')),created_at TEXT NOT NULL,PRIMARY KEY(org_id,receipt_id))",
    _AUDIT: "CREATE TABLE reciprocal_review_human_terminal_audit (org_id TEXT NOT NULL,audit_id TEXT NOT NULL,receipt_id TEXT NOT NULL,event_digest TEXT NOT NULL CHECK(length(event_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,audit_id),UNIQUE(org_id,receipt_id))",
    _OUTBOX: "CREATE TABLE reciprocal_review_human_terminal_outbox (org_id TEXT NOT NULL,outbox_id TEXT NOT NULL,receipt_id TEXT NOT NULL,payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),created_at TEXT NOT NULL,PRIMARY KEY(org_id,outbox_id),UNIQUE(org_id,receipt_id))",
}
_TRIGGERS = {
    f"{t}_no_{v}": f"CREATE TRIGGER {t}_no_{v} BEFORE {v.upper()} ON {t} BEGIN SELECT RAISE(ABORT,'immutable reciprocal review v3'); END"
    for t in _TABLES[1:]
    for v in ("update", "delete")
}
_TRIGGERS[f"{_CYCLE}_legal_update"] = (
    f"CREATE TRIGGER {_CYCLE}_legal_update BEFORE UPDATE ON {_CYCLE} FOR EACH ROW WHEN NOT (OLD.state_kind='review_open' AND NEW.state_kind='awaiting_human_disposition' AND NEW.cycle_revision=OLD.cycle_revision+1 AND OLD.org_id=NEW.org_id AND OLD.cycle_id=NEW.cycle_id AND OLD.revision_id=NEW.revision_id AND OLD.cycle_no=NEW.cycle_no AND OLD.active=NEW.active AND OLD.provenance_digest=NEW.provenance_digest AND OLD.policy_digest=NEW.policy_digest AND OLD.created_at=NEW.created_at) BEGIN SELECT RAISE(ABORT,'illegal reciprocal review v3 transition'); END"
)
_TRIGGERS[f"{_CYCLE}_no_delete"] = (
    f"CREATE TRIGGER {_CYCLE}_no_delete BEFORE DELETE ON {_CYCLE} BEGIN SELECT RAISE(ABORT,'immutable reciprocal review v3'); END"
)


class SqliteReciprocalReviewHumanTerminalError(RuntimeError):
    pass


class SqliteReciprocalReviewHumanTerminalConflict(SqliteReciprocalReviewHumanTerminalError):
    pass


def _canonical(x: object) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(x: object) -> str:
    return hashlib.sha256(_canonical(x).encode()).hexdigest()


def _time(x: datetime) -> str:
    if x.tzinfo is None or x.utcoffset() != UTC.utcoffset(x) or x.microsecond % 1000:
        raise SqliteReciprocalReviewHumanTerminalError(
            "DB time은 canonical UTC milliseconds여야 합니다."
        )
    return x.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _same(a: object, b: str) -> bool:
    return isinstance(a, str) and " ".join(a.split()) == " ".join(b.split())


def _catalog() -> dict[str, object]:
    return {
        "tables": [{"name": n, "sql": " ".join(s.split())} for n, s in _DDLS.items()],
        "triggers": [{"name": n, "sql": " ".join(s.split())} for n, s in _TRIGGERS.items()],
    }


def validate_sqlite_reciprocal_review_human_terminal(c: sqlite3.Connection) -> None:
    validate_sqlite_durable_reciprocal_review_ledger(c)
    manifest = _canonical(
        {"component_id": COMPONENT_ID, "schema_version": 3, "catalog": _catalog()}
    )
    marker = c.execute(
        f"SELECT schema_version,manifest_json,manifest_sha256 FROM {_MANIFEST} WHERE component_id=?",
        (COMPONENT_ID,),
    ).fetchone()
    if marker != (3, manifest, hashlib.sha256(manifest.encode()).hexdigest()):
        raise SqliteReciprocalReviewHumanTerminalError(
            "reciprocal review v3 manifest가 canonical하지 않습니다."
        )
    actual = {
        r[0]
        for r in c.execute(
            "SELECT name FROM sqlite_schema WHERE name LIKE 'durable_reciprocal_review_cycles_v3%' OR name LIKE 'reciprocal_review_human_terminal_%'"
        )
    }
    if (
        actual != set(_TABLES) | set(_TRIGGERS)
        or any(
            not _same(
                c.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (n,)
                ).fetchone()[0],
                s,
            )
            for n, s in _DDLS.items()
        )
        or any(
            not _same(
                c.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (n,)
                ).fetchone()[0],
                s,
            )
            for n, s in _TRIGGERS.items()
        )
    ):
        raise SqliteReciprocalReviewHumanTerminalError(
            "reciprocal review v3 catalog가 canonical하지 않습니다."
        )
    if c.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SqliteReciprocalReviewHumanTerminalError(
            "reciprocal review v3 FK가 canonical하지 않습니다."
        )
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
            or row[3]
            not in {
                "review_open",
                "awaiting_human_disposition",
                "binding_ready",
                "binding_pending",
                "bound",
                "superseded",
            }
            or type(row[4]) is not int
            or row[4] < 1
        ):
            raise SqliteReciprocalReviewHumanTerminalError(
                "forged reciprocal review v3 cycle row가 있습니다."
            )
        v2 = c.execute(
            "SELECT revision_id,cycle_no,state_kind,active,provenance_digest,policy_digest,created_at,cycle_revision "
            "FROM durable_reciprocal_review_cycles_v2 WHERE org_id=? AND cycle_id=?",
            row[:2],
        ).fetchone()
        if v2 is not None:
            mirror = c.execute(
                f"SELECT revision_id,cycle_no,state_kind,active,provenance_digest,policy_digest,created_at,cycle_revision FROM {_CYCLE} WHERE org_id=? AND cycle_id=?",
                row[:2],
            ).fetchone()
            if (
                mirror is None
                or (mirror[0], mirror[1], mirror[3], mirror[4], mirror[5], mirror[6])
                != (v2[0], v2[1], v2[3], v2[4], v2[5], v2[6])
                or mirror[7] < v2[7]
            ):
                raise SqliteReciprocalReviewHumanTerminalError(
                    "v3 cycle mirror가 v2 snapshot과 다릅니다."
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
        raise SqliteReciprocalReviewHumanTerminalError(
            "human terminal evidence graph가 bijection이 아닙니다."
        )
    for receipt in c.execute(
        f"SELECT org_id,receipt_id,audit_id,outbox_id,command_digest,cycle_id,requirement_id,review_run_id,created_at FROM {_RECEIPT}"
    ):
        org, receipt_id, audit_id, outbox_id, digest, cycle_id, requirement_id, run_id, created = (
            receipt
        )
        result = c.execute(
            f"SELECT cycle_id,requirement_id,review_run_id,command_digest,cycle_revision,cycle_state,created_at FROM {_RESULT} WHERE org_id=? AND receipt_id=?",
            (org, receipt_id),
        ).fetchone()
        cycle = c.execute(
            f"SELECT state_kind,cycle_revision FROM {_CYCLE} WHERE org_id=? AND cycle_id=?",
            (org, cycle_id),
        ).fetchone()
        audit = c.execute(
            f"SELECT receipt_id,event_digest,created_at FROM {_AUDIT} WHERE org_id=? AND audit_id=?",
            (org, audit_id),
        ).fetchone()
        outbox = c.execute(
            f"SELECT receipt_id,payload_digest,created_at FROM {_OUTBOX} WHERE org_id=? AND outbox_id=?",
            (org, outbox_id),
        ).fetchone()
        if (
            result is None
            or result[0:4] != (cycle_id, requirement_id, run_id, digest)
            or result[6] != created
            or cycle is None
            or audit != (receipt_id, _digest(("human_terminal_audit", digest)), created)
            or outbox != (receipt_id, _digest(("human_terminal_outbox", digest)), created)
        ):
            raise SqliteReciprocalReviewHumanTerminalError(
                "forged human terminal receipt graph가 있습니다."
            )


def migrate_sqlite_reciprocal_review_human_terminal_v3(
    c: sqlite3.Connection, *, fault_injector: Callable[[str], None] | None = None
) -> None:
    try:
        c.execute("BEGIN IMMEDIATE")
        # v2 snapshot is the only accepted cutover source.
        if (
            c.execute(
                f"SELECT 1 FROM {_MANIFEST} WHERE component_id='durable_reciprocal_review_ledger_v2'"
            ).fetchone()
            is None
        ):
            raise SqliteReciprocalReviewHumanTerminalError(
                "v3에는 explicit v2 snapshot이 필요합니다."
            )
        if (
            c.execute(f"SELECT 1 FROM {_MANIFEST} WHERE component_id=?", (COMPONENT_ID,)).fetchone()
            is None
        ):
            for n, s in _DDLS.items():
                c.execute(s)
                if fault_injector is not None:
                    fault_injector(f"after_{n}")
            for s in _TRIGGERS.values():
                c.execute(s)
            c.execute(
                f"INSERT INTO {_CYCLE} SELECT org_id,cycle_id,revision_id,cycle_no,state_kind,active,provenance_digest,policy_digest,created_at,cycle_revision FROM durable_reciprocal_review_cycles_v2"
            )
            manifest = _canonical(
                {"component_id": COMPONENT_ID, "schema_version": 3, "catalog": _catalog()}
            )
            c.execute(
                f"INSERT INTO {_MANIFEST} VALUES(?,?,?,?)",
                (COMPONENT_ID, 3, manifest, hashlib.sha256(manifest.encode()).hexdigest()),
            )
        validate_sqlite_reciprocal_review_human_terminal(c)
        c.commit()
    except Exception:
        if c.in_transaction:
            c.rollback()
        raise


def provision_sqlite_reciprocal_review_v3_cycle(
    c: sqlite3.Connection, *, org_id: str, cycle_id: str
) -> None:
    """Explicit registration-side mirror provision; never inferred by a terminal UoW."""
    installed = c.execute(
        f"SELECT 1 FROM {_MANIFEST} WHERE component_id=?", (COMPONENT_ID,)
    ).fetchone()
    if installed is None:
        return
    if (
        c.execute(
            f"SELECT 1 FROM {_CYCLE} WHERE org_id=? AND cycle_id=?", (org_id, cycle_id)
        ).fetchone()
        is not None
    ):
        return
    parent = c.execute(
        "SELECT revision_id,cycle_no,state_kind,active,provenance_digest,policy_digest,created_at "
        "FROM durable_reciprocal_review_cycles WHERE org_id=? AND cycle_id=?",
        (org_id, cycle_id),
    ).fetchone()
    if parent is None:
        raise SqliteReciprocalReviewHumanTerminalError("v3 mirror source cycle이 없습니다.")
    c.execute(f"INSERT INTO {_CYCLE} VALUES(?,?,?,?,?,?,?,?,?,?)", (org_id, cycle_id, *parent, 1))


class HumanReviewTerminalUnitOfWork(Protocol):
    def record(
        self, principal: HumanPrincipal, command: RecordHumanReviewTerminal
    ) -> RecordedHumanReviewTerminal: ...


class _Uow:
    def __init__(
        self,
        path: str | Path,
        keys: Mapping[str, bytes],
        clock: Callable[[], datetime],
        fault: Callable[[str], None] | None,
        lease_token_key: bytes,
    ) -> None:
        self.path, self.keys, self.clock, self.fault = Path(path), dict(keys), clock, fault
        self.lease_token_key = lease_token_key

    def record(
        self, principal: HumanPrincipal, command: RecordHumanReviewTerminal
    ) -> RecordedHumanReviewTerminal:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("BEGIN IMMEDIATE")
            validate_sqlite_reciprocal_review_human_terminal(c)
            validate_sqlite_reciprocal_review_lease(c)
            # ADR 0056 ownership fence: once registration explicitly provisions
            # the assignment-level v4 cycle, this legacy v3 writer cannot
            # terminalize it (nor silently count single-run evidence).
            if c.execute(
                "SELECT 1 FROM schema_component_manifests "
                "WHERE component_id='durable_reciprocal_review_ledger_v4'"
            ).fetchone() is not None and c.execute(
                "SELECT 1 FROM reciprocal_review_v4_cycle_ownership "
                "WHERE org_id=? AND cycle_id=? AND owner='v4'",
                (principal.org_id, command.cycle_id),
            ).fetchone() is not None:
                raise SqliteReciprocalReviewHumanTerminalError(
                    "v4-provisioned cycle은 v3 writer가 terminalize할 수 없습니다."
                )
            row = c.execute(
                f"SELECT v.revision_id,v.state_kind,v.cycle_revision,v.policy_digest,v.provenance_digest,r.provenance_kind,r.content_sha256,q.reviewer_assignment_digest,q.completion_rule,q.required_count,q.independence_rule,a.reviewer_ref,a.assignment_digest,s.owner_ref,s.lease_epoch,s.token_hash,s.expires_at,s.state FROM {_CYCLE} v JOIN durable_reciprocal_review_artifact_revisions r ON r.org_id=v.org_id AND r.revision_id=v.revision_id JOIN durable_reciprocal_review_requirements q ON q.org_id=v.org_id AND q.cycle_id=v.cycle_id JOIN reciprocal_review_lease_reviewer_assignments a ON a.org_id=v.org_id AND a.requirement_id=q.requirement_id JOIN reciprocal_review_lease_state s ON s.org_id=a.org_id AND s.review_run_id=a.review_run_id WHERE v.org_id=? AND v.cycle_id=? AND q.requirement_id=? AND a.review_run_id=?",
                (principal.org_id, command.cycle_id, command.requirement_id, command.review_run_id),
            ).fetchone()
            if row is None:
                raise SqliteReciprocalReviewHumanTerminalError(
                    "v3 cycle/run assignment이 없습니다."
                )
            (
                revision,
                state,
                cycle_rev,
                policy,
                prov,
                prov_kind,
                content,
                _assign,
                _rule,
                _count,
                _ind_rule,
                assigned,
                assignment,
                owner,
                epoch,
                token_hash,
                expires,
                lease_state,
            ) = row
            contributors = tuple(
                x[0]
                for x in c.execute(
                    "SELECT principal_ref FROM durable_reciprocal_review_provenance_events WHERE org_id=? AND revision_id=? AND principal_kind='human' ORDER BY principal_ref",
                    (principal.org_id, revision),
                )
            )
            independent = (
                principal.subject_id not in contributors
                and assigned == principal.subject_id
                and owner == principal.subject_id
            )
            semantic = {
                "principal": {
                    "subject_id": principal.subject_id,
                    "authn_context_digest": principal.authn_context_digest,
                },
                "command": command.model_dump(mode="json"),
                "revision": revision,
                "policy": policy,
                "provenance": prov,
                "assignment": assignment,
                "contributors": contributors,
                "independence": independent,
            }
            digest = _digest(semantic)
            old = c.execute(
                f"SELECT command_digest FROM {_RECEIPT} WHERE org_id=? AND idempotency_key=?",
                (principal.org_id, command.idempotency_key),
            ).fetchone()
            if old is not None:
                if old[0] != digest:
                    raise SqliteReciprocalReviewHumanTerminalConflict(
                        "idempotency semantic command가 다릅니다."
                    )
                result = self._result(c, principal.org_id, command.receipt_id, digest)
                c.commit()
                return result
            now = self.clock()
            now_s = _time(now)
            payload = {
                "org_id": principal.org_id,
                "reviewer": principal.subject_id,
                "authenticated_at": _time(principal.authenticated_at),
                "revision_id": revision,
                "cycle_id": command.cycle_id,
                "requirement_id": command.requirement_id,
                "review_run_id": command.review_run_id,
                "assignment_digest": assignment,
                "policy_digest": policy,
                "provenance_digest": prov,
                "contributor_digest": _digest(contributors),
                "independence_digest": _digest(independent),
                "content_digest": content,
                "rubric_digest": command.conclusion.rubric_digest,
                "input_digest": command.conclusion.input_digest,
            }
            key = self.keys.get(principal.subject_id)
            if (
                key is None
                or now - principal.authenticated_at > timedelta(minutes=5)
                or now < principal.authenticated_at
                or not hmac.compare_digest(
                    principal.authn_context_digest,
                    hmac.new(key, _canonical(payload).encode(), hashlib.sha256).hexdigest(),
                )
            ):
                raise SqliteReciprocalReviewHumanTerminalError(
                    "human review-run authority가 없습니다."
                )
            if (
                prov_kind not in {"ai", "mixed"}
                or not independent
                or command.conclusion.finding_count != 0
                or command.conclusion.content_digest != content
                or lease_state != "leased"
                or epoch != command.lease_epoch
                or token_hash
                != hmac.new(
                    self.lease_token_key, command.lease_token.encode(), hashlib.sha256
                ).hexdigest()
                or expires < now_s
            ):
                raise SqliteReciprocalReviewHumanTerminalError(
                    "human terminal eligibility 또는 lease가 stale입니다."
                )
            if (
                c.execute(
                    "UPDATE durable_reciprocal_review_runs SET state='recorded' WHERE org_id=? AND review_run_id=? AND state='leased' AND lease_epoch=?",
                    (principal.org_id, command.review_run_id, epoch),
                ).rowcount
                != 1
            ):
                raise SqliteReciprocalReviewHumanTerminalConflict("lease CAS가 stale입니다.")
            self._fault("after_run_cas")
            # Every human requirement must have enough finding-free recorded terminals before opening disposition.
            reqs = c.execute(
                "SELECT requirement_id,completion_rule,required_count FROM durable_reciprocal_review_requirements WHERE org_id=? AND cycle_id=? AND reviewer_kind='human'",
                (principal.org_id, command.cycle_id),
            ).fetchall()
            complete = bool(reqs) and all(
                c.execute(
                    f"SELECT count(*) FROM {_RECEIPT} WHERE org_id=? AND cycle_id=? AND requirement_id=?",
                    (principal.org_id, command.cycle_id, rid),
                ).fetchone()[0]
                + (1 if rid == command.requirement_id else 0)
                >= (1 if rr == "any" else rc)
                for rid, rr, rc in reqs
            )
            next_state = (
                "awaiting_human_disposition"
                if state == "review_open" and complete
                else "review_open"
            )
            next_rev = cycle_rev + 1 if next_state != state else cycle_rev
            if (
                next_state != state
                and c.execute(
                    f"UPDATE {_CYCLE} SET state_kind=?,cycle_revision=? WHERE org_id=? AND cycle_id=? AND state_kind='review_open' AND cycle_revision=?",
                    (next_state, next_rev, principal.org_id, command.cycle_id, cycle_rev),
                ).rowcount
                != 1
            ):
                raise SqliteReciprocalReviewHumanTerminalConflict("v3 cycle CAS가 stale입니다.")
            self._fault("after_cycle_cas")
            c.execute(
                f"INSERT INTO {_RECEIPT} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    principal.org_id,
                    command.receipt_id,
                    command.audit_id,
                    command.outbox_id,
                    command.idempotency_key,
                    digest,
                    command.cycle_id,
                    revision,
                    command.requirement_id,
                    command.review_run_id,
                    epoch,
                    principal.subject_id,
                    principal.authn_context_digest,
                    assignment,
                    policy,
                    prov,
                    _digest(contributors),
                    _digest(independent),
                    content,
                    command.conclusion.rubric_digest,
                    command.conclusion.input_digest,
                    now_s,
                ),
            )
            self._fault("after_receipt")
            c.execute(
                f"INSERT INTO {_RESULT} VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    principal.org_id,
                    command.receipt_id,
                    command.cycle_id,
                    command.requirement_id,
                    command.review_run_id,
                    digest,
                    next_rev,
                    next_state,
                    now_s,
                ),
            )
            self._fault("after_result")
            c.execute(
                f"INSERT INTO {_AUDIT} VALUES(?,?,?,?,?)",
                (
                    principal.org_id,
                    command.audit_id,
                    command.receipt_id,
                    _digest(("human_terminal_audit", digest)),
                    now_s,
                ),
            )
            self._fault("after_audit")
            c.execute(
                f"INSERT INTO {_OUTBOX} VALUES(?,?,?,?,?)",
                (
                    principal.org_id,
                    command.outbox_id,
                    command.receipt_id,
                    _digest(("human_terminal_outbox", digest)),
                    now_s,
                ),
            )
            self._fault("after_outbox")
            validate_sqlite_reciprocal_review_human_terminal(c)
            self._fault("before_commit")
            c.commit()
            return self._result(c, principal.org_id, command.receipt_id, digest)
        except Exception:
            if c.in_transaction:
                c.rollback()
            raise
        finally:
            c.close()

    def _result(
        self, c: sqlite3.Connection, org: str, receipt: str, digest: str
    ) -> RecordedHumanReviewTerminal:
        x = c.execute(
            f"SELECT cycle_id,requirement_id,review_run_id,command_digest,cycle_revision,cycle_state,created_at FROM {_RESULT} WHERE org_id=? AND receipt_id=?",
            (org, receipt),
        ).fetchone()
        if x is None or x[3] != digest:
            raise SqliteReciprocalReviewHumanTerminalError("immutable result가 없습니다.")
        return RecordedHumanReviewTerminal(
            org_id=org,
            receipt_id=receipt,
            cycle_id=x[0],
            requirement_id=x[1],
            review_run_id=x[2],
            command_digest=x[3],
            cycle_revision=x[4],
            cycle_state=x[5],
            created_at=datetime.fromisoformat(x[6].replace("Z", "+00:00")),
        )

    def _fault(self, p: str) -> None:
        if self.fault:
            self.fault(p)


def create_sqlite_reciprocal_review_human_terminal_uow(
    path: str | Path,
    *,
    trusted_human_review_run_authority_keys: Mapping[str, bytes],
    trusted_ai_execution_keys: Mapping[str, bytes],
    trusted_lease_token_key: bytes,
    clock: Callable[[], datetime],
    fault_injector: Callable[[str], None] | None = None,
) -> HumanReviewTerminalUnitOfWork:
    if not trusted_human_review_run_authority_keys or any(
        type(k) is not str or type(v) is not bytes or not v
        for k, v in trusted_human_review_run_authority_keys.items()
    ):
        raise ValueError("trusted human review-run authority key registry가 유효하지 않습니다.")
    # AI keys intentionally remain a composition input: they make the v3 root consistent with v2, but S1b.5a does not consume AI evidence.
    if not trusted_ai_execution_keys or not trusted_lease_token_key:
        raise ValueError("trusted AI execution key registry가 유효하지 않습니다.")
    return _Uow(
        path,
        trusted_human_review_run_authority_keys,
        clock,
        fault_injector,
        trusted_lease_token_key,
    )
