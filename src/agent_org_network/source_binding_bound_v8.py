# ruff: noqa: E501, E701, E702
"""ADR 0062 v8 fake-only Bound/read SSOT; v7 Pending is never rewritten."""

from __future__ import annotations
import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from collections.abc import Callable
from agent_org_network.reciprocal_review import SourceBindingAuthorizationEnvelopeV7
from agent_org_network.source_binding_authorization import (
    SourceBindingAuthorizationDenied,
    SourceBindingAuthorizationUnavailable,
)
from agent_org_network.sqlite_reciprocal_review_source_binding_v7 import (
    _now,  # pyright: ignore[reportPrivateUsage]
    _time,  # pyright: ignore[reportPrivateUsage]
    validate_sqlite_reciprocal_review_source_binding_v7,
)  # pyright: ignore[reportPrivateUsage]
from agent_org_network.trusted_source_binding_authority import SourceBindingAuthorityCapability
from agent_org_network.trusted_source_integration import TrustedSourceIntegrationSession

_MINT = secrets.token_urlsafe(32)
COMPONENT_ID = "durable_reciprocal_review_source_binding_bound_v8"
_M = "schema_component_manifests"
_E = "source_binding_v8_enforcement"
_B = "source_binding_v8_terminals"
_A = "source_binding_v8_attestations"
_K = "source_binding_v8_kills"
_AU = "source_binding_v8_audit"
_O = "source_binding_v8_outbox"
_DDL = {
    _E: f"CREATE TABLE {_E} (org_id TEXT NOT NULL,intent_id TEXT NOT NULL,generation INTEGER NOT NULL,profile_digest TEXT NOT NULL,session_digest TEXT NOT NULL,enforcement_id TEXT NOT NULL,enforcement_digest TEXT NOT NULL,state TEXT NOT NULL CHECK(state='Active'),PRIMARY KEY(org_id,intent_id,generation),UNIQUE(org_id,enforcement_id))",
    _B: f"CREATE TABLE {_B} (org_id TEXT NOT NULL,intent_id TEXT NOT NULL,generation INTEGER NOT NULL,readback_digest TEXT NOT NULL,receipt_digest TEXT NOT NULL,enforcement_digest TEXT NOT NULL,authority_digest TEXT NOT NULL,terminal_digest TEXT NOT NULL,bound_at TEXT NOT NULL,audit_id TEXT NOT NULL,outbox_id TEXT NOT NULL,PRIMARY KEY(org_id,intent_id),UNIQUE(org_id,terminal_digest),UNIQUE(org_id,audit_id),UNIQUE(org_id,outbox_id),FOREIGN KEY(org_id,intent_id,generation) REFERENCES {_E}(org_id,intent_id,generation))",
    _A: f"CREATE TABLE {_A} (org_id TEXT NOT NULL,attestation_id TEXT NOT NULL,intent_id TEXT NOT NULL,attestation_digest TEXT NOT NULL,issued_at TEXT NOT NULL,expires_at TEXT NOT NULL,PRIMARY KEY(org_id,attestation_id),FOREIGN KEY(org_id,intent_id) REFERENCES {_B}(org_id,intent_id))",
    _K: f"CREATE TABLE {_K} (org_id TEXT NOT NULL,source_ref TEXT NOT NULL,kill_id TEXT NOT NULL,reason_digest TEXT NOT NULL,effective_at TEXT NOT NULL,PRIMARY KEY(org_id,source_ref,kill_id))",
    _AU: f"CREATE TABLE {_AU} (org_id TEXT NOT NULL,audit_id TEXT NOT NULL,intent_id TEXT NOT NULL,event_digest TEXT NOT NULL,PRIMARY KEY(org_id,audit_id),FOREIGN KEY(org_id,intent_id) REFERENCES {_B}(org_id,intent_id))",
    _O: f"CREATE TABLE {_O} (org_id TEXT NOT NULL,outbox_id TEXT NOT NULL,intent_id TEXT NOT NULL,payload_digest TEXT NOT NULL,PRIMARY KEY(org_id,outbox_id),FOREIGN KEY(org_id,intent_id) REFERENCES {_B}(org_id,intent_id))",
}
_T = {
    f"{t}_{v}": f"CREATE TRIGGER {t}_{v} BEFORE {v.upper()} ON {t} BEGIN SELECT RAISE(ABORT,'immutable v8 bound'); END"
    for t in _DDL
    for v in ("update", "delete")
}


def _can(x: object) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"), default=str)


def _dig(x: object) -> str:
    return hashlib.sha256(_can(x).encode()).hexdigest()


class BoundV8Denied(RuntimeError):
    pass


def _require_bootstrap_binding(
    capability: SourceBindingAuthorityCapability, session: TrustedSourceIntegrationSession
) -> None:
    if (
        type(capability) is not SourceBindingAuthorityCapability
        or type(session) is not TrustedSourceIntegrationSession
        or getattr(session, "_capability", None) is not capability
        or not capability.is_live()
    ):
        raise BoundV8Denied("bootstrap binding unavailable")
    session._require_live_binding()  # pyright: ignore[reportPrivateUsage]


@dataclass(frozen=True, init=False)
class BoundSourceReadbackV8:
    org_id: str
    intent_id: str
    source_ref: str
    profile_digest: str
    session_digest: str
    expected_revision: str
    observed_revision: str
    content_digest: str
    receipt_payload_digest: str
    generation: int
    enforcement_id: str
    enforcement_state: str
    key_id: str
    issued_at: datetime
    signature: str

    def __init__(self, token: str, **data: object) -> None:
        if not hmac.compare_digest(token, _MINT):
            raise TypeError("sealed fake readback only")
        for k, v in data.items():
            object.__setattr__(self, k, v)

    def payload(self) -> dict[str, object]:
        return {
            k: (
                getattr(self, k).astimezone(UTC).isoformat(timespec="milliseconds")
                if k == "issued_at"
                else getattr(self, k)
            )
            for k in (
                "org_id",
                "intent_id",
                "source_ref",
                "profile_digest",
                "session_digest",
                "expected_revision",
                "observed_revision",
                "content_digest",
                "receipt_payload_digest",
                "generation",
                "enforcement_id",
                "enforcement_state",
                "key_id",
                "issued_at",
            )
        }

    @property
    def digest(self) -> str:
        return _dig(self.payload())


@dataclass(frozen=True, init=False)
class SourceServingAttestationV8:
    intent_id: str
    readback_digest: str
    enforcement_digest: str
    profile_digest: str
    generation: int
    key_id: str
    issued_at: datetime
    expires_at: datetime
    signature: str

    def __init__(self, token: str, **data: object) -> None:
        if not hmac.compare_digest(token, _MINT):
            raise TypeError("sealed fake attestation only")
        for k, v in data.items():
            object.__setattr__(self, k, v)

    def payload(self) -> dict[str, object]:
        return {
            k: (
                getattr(self, k).astimezone(UTC).isoformat(timespec="milliseconds")
                if k in ("issued_at", "expires_at")
                else getattr(self, k)
            )
            for k in (
                "intent_id",
                "readback_digest",
                "enforcement_digest",
                "profile_digest",
                "generation",
                "key_id",
                "issued_at",
                "expires_at",
            )
        }

    @property
    def digest(self) -> str:
        return _dig(self.payload())


@dataclass(frozen=True)
class BoundV8:
    org_id: str
    intent_id: str
    terminal_digest: str


def _key(session: TrustedSourceIntegrationSession) -> tuple[str, bytes]:
    session._require_live_binding()  # pyright: ignore[reportPrivateUsage]
    profile = session._registry._profiles[session._profile_id]  # pyright: ignore[reportPrivateUsage]
    return profile.signing_key_id, session._registry._keys[profile.signing_key_id]  # pyright: ignore[reportPrivateUsage]


def mint_bound_readback_v8(
    *,
    session: TrustedSourceIntegrationSession,
    org_id: str,
    intent_id: str,
    source_ref: str,
    expected_revision: str,
    content_digest: str,
    receipt_payload_digest: str,
    generation: int,
    enforcement_id: str,
) -> BoundSourceReadbackV8:
    key_id, key = _key(session)
    profile = session._registry._profiles[session._profile_id]  # pyright: ignore[reportPrivateUsage]
    data = dict(
        org_id=org_id,
        intent_id=intent_id,
        source_ref=source_ref,
        profile_digest=profile.profile_digest,
        session_digest=_dig(
            (profile.profile_id, profile.profile_version, profile.credential_generation)
        ),
        expected_revision=expected_revision,
        observed_revision=expected_revision,
        content_digest=content_digest,
        receipt_payload_digest=receipt_payload_digest,
        generation=generation,
        enforcement_id=enforcement_id,
        enforcement_state="Active",
        key_id=key_id,
        issued_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    unsigned = BoundSourceReadbackV8(_MINT, **data)
    return BoundSourceReadbackV8(
        _MINT,
        **data,
        signature=hmac.new(key, _can(unsigned.payload()).encode(), hashlib.sha256).hexdigest(),
    )


def mint_serving_attestation_v8(
    *,
    session: TrustedSourceIntegrationSession,
    readback: BoundSourceReadbackV8,
    enforcement_digest: str,
    ttl: timedelta = timedelta(minutes=1),
) -> SourceServingAttestationV8:
    key_id, key = _key(session)
    now = datetime.now(UTC)
    issued = now.replace(microsecond=now.microsecond // 1000 * 1000)
    data = dict(
        intent_id=readback.intent_id,
        readback_digest=readback.digest,
        enforcement_digest=enforcement_digest,
        profile_digest=readback.profile_digest,
        generation=readback.generation,
        key_id=key_id,
        issued_at=issued,
        expires_at=issued + ttl,
    )
    unsigned = SourceServingAttestationV8(_MINT, **data)
    return SourceServingAttestationV8(
        _MINT,
        **data,
        signature=hmac.new(key, _can(unsigned.payload()).encode(), hashlib.sha256).hexdigest(),
    )


def _verify(
    session: TrustedSourceIntegrationSession,
    obj: BoundSourceReadbackV8 | SourceServingAttestationV8,
) -> bool:
    try:
        key_id, key = _key(session)
    except Exception:
        return False
    return (
        type(obj) in (BoundSourceReadbackV8, SourceServingAttestationV8)
        and obj.key_id == key_id
        and hmac.compare_digest(
            obj.signature, hmac.new(key, _can(obj.payload()).encode(), hashlib.sha256).hexdigest()
        )
    )


def migrate_sqlite_source_binding_bound_v8(c: sqlite3.Connection) -> None:
    try:
        c.execute("BEGIN IMMEDIATE")
        validate_sqlite_reciprocal_review_source_binding_v7(c)
        if (
            c.execute(f"SELECT 1 FROM {_M} WHERE component_id=?", (COMPONENT_ID,)).fetchone()
            is None
        ):
            for x in _DDL.values():
                c.execute(x)
            for x in _T.values():
                c.execute(x)
            raw = _can({"tables": _DDL, "triggers": _T})
            c.execute(
                f"INSERT INTO {_M} VALUES(?,?,?,?)",
                (COMPONENT_ID, 8, raw, _dig({"tables": _DDL, "triggers": _T})),
            )
        c.commit()
    except Exception:
        if c.in_transaction:
            c.rollback()
        raise


def validate_sqlite_source_binding_bound_v8(c: sqlite3.Connection) -> None:
    validate_sqlite_reciprocal_review_source_binding_v7(c)
    raw = _can({"tables": _DDL, "triggers": _T})
    if c.execute(
        f"SELECT schema_version,manifest_json,manifest_sha256 FROM {_M} WHERE component_id=?",
        (COMPONENT_ID,),
    ).fetchone() != (8, raw, _dig({"tables": _DDL, "triggers": _T})):
        raise BoundV8Denied("v8 catalog unavailable")
    actual = {
        row[0]
        for row in c.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'source_binding_v8_%'")
    }
    if actual != set(_DDL) | set(_T):
        raise BoundV8Denied("v8 catalog tamper")
    for name, sql in {**_DDL, **_T}.items():
        row = c.execute("SELECT sql FROM sqlite_schema WHERE name=?", (name,)).fetchone()
        if row is None or " ".join(row[0].split()) != " ".join(sql.split()):
            raise BoundV8Denied("v8 catalog tamper")
    for org, intent, generation, readback, _receipt, enforcement, authority, terminal, _bound_at, audit, outbox in c.execute(
        f"SELECT org_id,intent_id,generation,readback_digest,receipt_digest,enforcement_digest,authority_digest,terminal_digest,bound_at,audit_id,outbox_id FROM {_B}"
    ):
        enforcement_row = c.execute(
            f"SELECT enforcement_digest FROM {_E} WHERE org_id=? AND intent_id=? AND generation=? AND state='Active'",
            (org, intent, generation),
        ).fetchone()
        audit_row = c.execute(f"SELECT intent_id,event_digest FROM {_AU} WHERE org_id=? AND audit_id=?", (org, audit)).fetchone()
        outbox_row = c.execute(f"SELECT intent_id,payload_digest FROM {_O} WHERE org_id=? AND outbox_id=?", (org, outbox)).fetchone()
        if enforcement_row != (enforcement,) or terminal != _dig((org, intent, readback, enforcement, authority)) or audit_row != (intent, _dig(('bound', terminal))) or outbox_row != (intent, _dig(('bound', terminal))):
            raise BoundV8Denied("v8 graph tamper")


class SourceBindingTerminalizerV8:
    def __init__(
        self,
        path: str | Path,
        capability: SourceBindingAuthorityCapability,
        session: TrustedSourceIntegrationSession,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.path = Path(path)
        _require_bootstrap_binding(capability, session)
        self.capability = capability
        self.session = session
        self.fault_injector = fault_injector

    def finalize(
        self,
        *,
        org_id: str,
        intent_id: str,
        readback: BoundSourceReadbackV8,
        audit_id: str,
        outbox_id: str,
    ) -> BoundV8:
        if not self.capability.matches_sqlite_target(self.path):
            raise BoundV8Denied("target unavailable")
        c = sqlite3.connect(self.path, isolation_level=None)
        try:
            c.execute("BEGIN IMMEDIATE")
            validate_sqlite_source_binding_bound_v8(c)
            now = _now(c)
            row = c.execute(
                "SELECT i.source_ref,i.authorization_json,c.binding_generation FROM reciprocal_review_v7_binding_intents i JOIN durable_reciprocal_review_source_binding_cycles_v7 c ON c.org_id=i.org_id AND c.cycle_id=i.cycle_id WHERE i.org_id=? AND i.receipt_id=?",
                (org_id, intent_id),
            ).fetchone()
            if (
                row is None
                or c.execute(
                    f"SELECT 1 FROM {_B} WHERE org_id=? AND intent_id=?", (org_id, intent_id)
                ).fetchone()
                or c.execute(
                    f'SELECT 1 FROM {_K} WHERE org_id=? AND source_ref IN (?,"*")', (org_id, row[0])
                ).fetchone()
            ):
                raise BoundV8Denied("Pending unchanged")
            env = SourceBindingAuthorizationEnvelopeV7.model_validate_json(row[1])
            self.session._require_live_binding()  # pyright: ignore[reportPrivateUsage]
            if (
                not self.capability.is_live()
                or not self.capability.matches_sqlite_target(self.path)
                or not self.capability.matches_source_ref(row[0])
                or not _verify(self.session, readback)
            ):
                raise BoundV8Denied("live fake unavailable")
            if (
                readback.org_id,
                readback.intent_id,
                readback.source_ref,
                readback.expected_revision,
                readback.observed_revision,
                readback.content_digest,
                readback.receipt_payload_digest,
                readback.generation,
                readback.enforcement_state,
            ) != (
                org_id,
                intent_id,
                row[0],
                env.expected_source_revision,
                env.expected_source_revision,
                env.content_digest,
                env.payload_digest,
                row[2],
                "Active",
            ):
                raise BoundV8Denied("exact readback mismatch")
            verified = self.capability.verify_current(
                receipt=env, purpose="BoundTerminal", readback=readback, db_now=now
            )
            if isinstance(
                verified, (SourceBindingAuthorizationDenied, SourceBindingAuthorizationUnavailable)
            ):
                raise BoundV8Denied("authority denied")
            ed = _dig((readback.enforcement_id, readback.digest))
            td = _dig((org_id, intent_id, readback.digest, ed, verified.payload_digest))
            c.execute(
                f"INSERT INTO {_E} VALUES(?,?,?,?,?,?,?,?)",
                (
                    org_id,
                    intent_id,
                    row[2],
                    readback.profile_digest,
                    readback.session_digest,
                    readback.enforcement_id,
                    ed,
                    "Active",
                ),
            )
            self._hit("after_enforcement")
            c.execute(
                f"INSERT INTO {_B} VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    org_id,
                    intent_id,
                    row[2],
                    readback.digest,
                    env.payload_digest,
                    ed,
                    verified.payload_digest,
                    td,
                    _time(now),
                    audit_id,
                    outbox_id,
                ),
            )
            self._hit("after_terminal")
            c.execute(
                f"INSERT INTO {_AU} VALUES(?,?,?,?)",
                (org_id, audit_id, intent_id, _dig(("bound", td))),
            )
            self._hit("after_audit")
            c.execute(
                f"INSERT INTO {_O} VALUES(?,?,?,?)",
                (org_id, outbox_id, intent_id, _dig(("bound", td))),
            )
            self._hit("after_outbox")
            c.commit()
            return BoundV8(org_id, intent_id, td)
        except Exception:
            if c.in_transaction:
                c.rollback()
            raise
        finally:
            c.close()

    def _hit(self, point: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(point)


class SourceReadGateV8:
    def __init__(
        self,
        path: str | Path,
        capability: SourceBindingAuthorityCapability,
        session: TrustedSourceIntegrationSession,
    ) -> None:
        self.path = Path(path)
        _require_bootstrap_binding(capability, session)
        self.capability = capability
        self.session = session

    def authorize_read(
        self,
        *,
        org_id: str,
        intent_id: str,
        readback: BoundSourceReadbackV8,
        attestation: SourceServingAttestationV8,
        attestation_id: str,
    ) -> bool:
        if not self.capability.matches_sqlite_target(self.path):
            raise BoundV8Denied("target unavailable")
        _require_bootstrap_binding(self.capability, self.session)
        c = sqlite3.connect(self.path, isolation_level=None)
        try:
            c.execute("BEGIN IMMEDIATE")
            validate_sqlite_source_binding_bound_v8(c)
            row = c.execute(
                f"SELECT b.generation,b.readback_digest,b.receipt_digest,b.enforcement_digest,e.profile_digest,e.session_digest,e.enforcement_id,i.source_ref,i.authorization_json FROM {_B} b JOIN {_E} e ON e.org_id=b.org_id AND e.intent_id=b.intent_id AND e.generation=b.generation AND e.state='Active' JOIN reciprocal_review_v7_binding_intents i ON i.org_id=b.org_id AND i.receipt_id=b.intent_id WHERE b.org_id=? AND b.intent_id=?",
                (org_id, intent_id),
            ).fetchone()
            if (
                row is None
                or c.execute(
                    f'SELECT 1 FROM {_K} WHERE org_id=? AND source_ref IN (?,"*")', (org_id, row[7])
                ).fetchone()
            ):
                raise BoundV8Denied("deny")
            env = SourceBindingAuthorizationEnvelopeV7.model_validate_json(row[8])
            self.session._require_live_binding()  # pyright: ignore[reportPrivateUsage]
            if (
                not self.capability.is_live()
                or not self.capability.matches_source_ref(row[7])
                or not _verify(self.session, readback)
                or not _verify(self.session, attestation)
            ):
                raise BoundV8Denied("deny")
            if (
                attestation.issued_at.tzinfo is None
                or attestation.expires_at <= _now(c)
                or attestation.expires_at <= attestation.issued_at
                or (
                    readback.digest,
                    readback.receipt_payload_digest,
                    readback.generation,
                    readback.profile_digest,
                    readback.session_digest,
                    readback.enforcement_id,
                )
                != (row[1], row[2], row[0], row[4], row[5], row[6])
                or (
                    attestation.intent_id,
                    attestation.readback_digest,
                    attestation.enforcement_digest,
                    attestation.generation,
                )
                != (intent_id, row[1], row[3], row[0])
            ):
                raise BoundV8Denied("attestation mismatch")
            verified = self.capability.verify_current(
                receipt=env, purpose="SourceRead", readback=attestation, db_now=_now(c)
            )
            if isinstance(
                verified, (SourceBindingAuthorizationDenied, SourceBindingAuthorizationUnavailable)
            ):
                raise BoundV8Denied("deny")
            c.execute(
                f"INSERT INTO {_A} VALUES(?,?,?,?,?,?)",
                (
                    org_id,
                    attestation_id,
                    intent_id,
                    attestation.digest,
                    _time(attestation.issued_at),
                    _time(attestation.expires_at),
                ),
            )
            c.commit()
            return True
        except Exception:
            if c.in_transaction:
                c.rollback()
            raise
        finally:
            c.close()


def arm_kill_switch_v8(
    c: sqlite3.Connection,
    *,
    org_id: str,
    source_ref: str,
    kill_id: str,
    reason_digest: str,
    at: datetime,
) -> None:
    c.execute(
        f"INSERT INTO {_K} VALUES(?,?,?,?,?)",
        (org_id, source_ref, kill_id, reason_digest, _time(at)),
    )
