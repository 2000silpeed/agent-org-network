# ruff: noqa: E701, E702
"""ADR 0062 fake-only Bound terminal and every-read gate.

The original Pending row is immutable.  Its one-to-one terminal companion is
therefore the authoritative Bound projection; no source I/O occurs here.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from agent_org_network.reciprocal_review import SourceBindingAuthorizationEnvelopeV7
from agent_org_network.source_binding_authorization import (
    SourceBindingAuthorizationDenied, SourceBindingAuthorizationUnavailable,
)
from agent_org_network.sqlite_reciprocal_review_source_binding_v7 import (
    _now, _time, validate_sqlite_reciprocal_review_source_binding_v7,  # pyright: ignore[reportPrivateUsage]
)
from agent_org_network.trusted_source_binding_authority import SourceBindingAuthorityCapability
from agent_org_network.trusted_source_integration import (
    StableSourceReadback, TrustedSourceIntegrationSession,
)


COMPONENT_ID = "durable_reciprocal_review_source_binding_bound_v1"
_E = "source_binding_v7_enforcement_references"
_B = "source_binding_v7_bound_terminals"
_A = "source_binding_v7_read_attestations"
_K = "source_binding_v7_kill_switches"
_M = "schema_component_manifests"
_DDL = {
    _E: f"CREATE TABLE {_E} (org_id TEXT NOT NULL,intent_id TEXT NOT NULL,binding_generation INTEGER NOT NULL,profile_digest TEXT NOT NULL,enforcement_digest TEXT NOT NULL,state TEXT NOT NULL CHECK(state='Active'),PRIMARY KEY(org_id,intent_id,binding_generation))",
    _B: f"CREATE TABLE {_B} (org_id TEXT NOT NULL,intent_id TEXT NOT NULL,binding_generation INTEGER NOT NULL,readback_digest TEXT NOT NULL,enforcement_digest TEXT NOT NULL,authority_digest TEXT NOT NULL,terminal_digest TEXT NOT NULL UNIQUE,bound_at TEXT NOT NULL,audit_id TEXT NOT NULL UNIQUE,outbox_id TEXT NOT NULL UNIQUE,PRIMARY KEY(org_id,intent_id),FOREIGN KEY(org_id,intent_id,binding_generation) REFERENCES {_E}(org_id,intent_id,binding_generation))",
    _A: f"CREATE TABLE {_A} (org_id TEXT NOT NULL,attestation_id TEXT NOT NULL, intent_id TEXT NOT NULL,attestation_digest TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(org_id,attestation_id),FOREIGN KEY(org_id,intent_id) REFERENCES {_B}(org_id,intent_id))",
    _K: f"CREATE TABLE {_K} (org_id TEXT NOT NULL,source_ref TEXT NOT NULL,kill_id TEXT NOT NULL,reason_digest TEXT NOT NULL,effective_at TEXT NOT NULL,PRIMARY KEY(org_id,source_ref,kill_id))",
}
_TRIGGERS = {f"{table}_{verb}": f"CREATE TRIGGER {table}_{verb} BEFORE {verb.upper()} ON {table} BEGIN SELECT RAISE(ABORT,'immutable bound companion'); END" for table in _DDL for verb in ("update", "delete")}


def _can(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _dig(value: object) -> str:
    return hashlib.sha256(_can(value).encode()).hexdigest()


class SourceBindingBoundUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class BoundTerminal:
    org_id: str
    intent_id: str
    binding_generation: int
    terminal_digest: str


@dataclass(frozen=True)
class SourceReadAllowed:
    org_id: str
    intent_id: str


def migrate_sqlite_source_binding_bound(c: sqlite3.Connection) -> None:
    try:
        c.execute("BEGIN IMMEDIATE")
        validate_sqlite_reciprocal_review_source_binding_v7(c)
        if c.execute(f"SELECT 1 FROM {_M} WHERE component_id=?", (COMPONENT_ID,)).fetchone() is None:
            for sql in _DDL.values(): c.execute(sql)
            for sql in _TRIGGERS.values(): c.execute(sql)
            manifest = _can({"tables": _DDL, "triggers": _TRIGGERS})
            c.execute(f"INSERT INTO {_M} VALUES(?,?,?,?)", (COMPONENT_ID, 1, manifest, _dig({"tables": _DDL, "triggers": _TRIGGERS})))
        c.commit()
    except Exception:
        if c.in_transaction: c.rollback()
        raise


class SourceBindingTerminalizer:
    def __init__(self, path: str | Path, capability: SourceBindingAuthorityCapability, session: TrustedSourceIntegrationSession) -> None:
        self.path, self.capability, self.session = Path(path), capability, session
        if type(capability) is not SourceBindingAuthorityCapability or type(session) is not TrustedSourceIntegrationSession:
            raise ValueError("sealed bootstrap capability and session required")

    def finalize(self, *, org_id: str, intent_id: str, readback: StableSourceReadback, audit_id: str, outbox_id: str) -> BoundTerminal:
        c = sqlite3.connect(self.path, isolation_level=None)
        try:
            c.execute("BEGIN IMMEDIATE"); validate_sqlite_reciprocal_review_source_binding_v7(c)
            now = _now(c)
            if self._killed(c, org_id, "") or not self.capability.is_live() or not self.capability.matches_sqlite_target(self.path):
                raise SourceBindingBoundUnavailable("current bootstrap capability unavailable")
            row = c.execute("SELECT i.source_ref,i.authorization_json,c.binding_generation FROM reciprocal_review_v7_binding_intents i JOIN durable_reciprocal_review_source_binding_cycles_v7 c ON c.org_id=i.org_id AND c.cycle_id=i.cycle_id WHERE i.org_id=? AND i.receipt_id=?", (org_id, intent_id)).fetchone()
            if row is None or c.execute(f"SELECT 1 FROM {_B} WHERE org_id=? AND intent_id=?", (org_id, intent_id)).fetchone() is not None: raise SourceBindingBoundUnavailable("Pending terminal unavailable")
            env = SourceBindingAuthorizationEnvelopeV7.model_validate_json(row[1])
            if not self.capability.matches_source_ref(row[0]) or not self.session._registry.verify_readback(self.session._profile_id, readback): raise SourceBindingBoundUnavailable("readback unavailable")  # pyright: ignore[reportPrivateUsage]
            if (readback.source_ref, readback.expected_source_revision, readback.observed_source_revision, readback.policy_digest, readback.binding_generation) != (env.source_ref, env.expected_source_revision, env.expected_source_revision, env.policy_digest, row[2]): raise SourceBindingBoundUnavailable("readback mismatch")
            verified = self.capability.verify_current(receipt=env, purpose="BoundTerminal", readback=readback, db_now=now)
            if isinstance(verified, (SourceBindingAuthorizationDenied, SourceBindingAuthorizationUnavailable)): raise SourceBindingBoundUnavailable("bound authority denied")
            enforcement = _dig((org_id, intent_id, row[2], readback.profile_digest, readback.payload_digest))
            terminal = _dig((org_id, intent_id, row[2], readback.payload_digest, enforcement, verified.payload_digest))
            c.execute(f"INSERT INTO {_E} VALUES(?,?,?,?,?,?)", (org_id, intent_id, row[2], readback.profile_digest, enforcement, "Active"))
            c.execute(f"INSERT INTO {_B} VALUES(?,?,?,?,?,?,?,?,?,?,?)", (org_id, intent_id, row[2], readback.payload_digest, enforcement, verified.payload_digest, terminal, _time(now), audit_id, outbox_id))
            c.commit(); return BoundTerminal(org_id, intent_id, row[2], terminal)
        except Exception:
            if c.in_transaction: c.rollback()
            raise
        finally: c.close()

    @staticmethod
    def _killed(c: sqlite3.Connection, org_id: str, source_ref: str) -> bool:
        return c.execute(f"SELECT 1 FROM {_K} WHERE org_id=? AND source_ref IN (?, '*')", (org_id, source_ref)).fetchone() is not None


class SourceReadGate:
    def __init__(self, path: str | Path, capability: SourceBindingAuthorityCapability, session: TrustedSourceIntegrationSession) -> None:
        self.path, self.capability, self.session = Path(path), capability, session

    def authorize_read(self, *, org_id: str, intent_id: str, readback: StableSourceReadback, attestation_id: str) -> SourceReadAllowed:
        c = sqlite3.connect(self.path, isolation_level=None)
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(f"SELECT b.binding_generation,b.readback_digest,b.enforcement_digest,i.source_ref,i.authorization_json FROM {_B} b JOIN reciprocal_review_v7_binding_intents i ON i.org_id=b.org_id AND i.receipt_id=b.intent_id JOIN {_E} e ON e.org_id=b.org_id AND e.intent_id=b.intent_id AND e.binding_generation=b.binding_generation AND e.state='Active' WHERE b.org_id=? AND b.intent_id=?", (org_id, intent_id)).fetchone()
            if row is None or SourceBindingTerminalizer._killed(c, org_id, row[3]): raise SourceBindingBoundUnavailable("read denied")  # pyright: ignore[reportPrivateUsage]
            env = SourceBindingAuthorizationEnvelopeV7.model_validate_json(row[4])
            self.session._require_live_binding()  # pyright: ignore[reportPrivateUsage]
            if not self.capability.is_live() or not self.capability.matches_source_ref(row[3]) or not self.session._registry.verify_readback(self.session._profile_id, readback): raise SourceBindingBoundUnavailable("read denied")  # pyright: ignore[reportPrivateUsage]
            if readback.payload_digest != row[1] or readback.binding_generation != row[0]: raise SourceBindingBoundUnavailable("attestation mismatch")
            verified = self.capability.verify_current(receipt=env, purpose="SourceRead", readback=readback, db_now=_now(c))
            if isinstance(verified, (SourceBindingAuthorizationDenied, SourceBindingAuthorizationUnavailable)): raise SourceBindingBoundUnavailable("read denied")
            c.execute(f"INSERT INTO {_A} VALUES(?,?,?,?,?)", (org_id, attestation_id, intent_id, readback.payload_digest, _time(_now(c))))
            c.commit(); return SourceReadAllowed(org_id, intent_id)
        except Exception:
            if c.in_transaction: c.rollback()
            raise
        finally: c.close()
