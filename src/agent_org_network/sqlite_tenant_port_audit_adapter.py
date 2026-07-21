"""S3.1b org-bound SQLite adapter for the additive safe-audit v2 component."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Final

from agent_org_network.sqlite_tenant_port_audit_v2 import open_sqlite_tenant_port_audit_v2
from agent_org_network.tenant_operational_ports import (
    ResourceFingerprint,
    SafeAuditEvent,
    ScopedUnavailable,
    TenantOrgId,
)

_TIMESTAMP: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _event_digest(event: SafeAuditEvent) -> str:
    payload = {
        "action": event.action,
        "fingerprint": event.fingerprint.value,
        "outcome": event.outcome,
        "subject_id": event.subject_id,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _canonical_timestamp(value: object) -> bool:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z" == value


def _decode(row: tuple[object, ...], org: TenantOrgId) -> tuple[int, SafeAuditEvent]:
    seq, row_org, action, subject_id, outcome, fingerprint, digest, created_at = row
    if (
        type(seq) is not int
        or seq < 0
        or row_org != org.value
        or not _canonical_timestamp(created_at)
    ):
        raise ValueError("audit v2 row scope/scalar/timestamp가 손상되었습니다.")
    try:
        event = SafeAuditEvent(action, subject_id, outcome, ResourceFingerprint(fingerprint))  # type: ignore[arg-type]
    except ValueError as error:
        raise ValueError("audit v2 safe event가 손상되었습니다.") from error
    if type(digest) is not str or digest != _event_digest(event):
        raise ValueError("audit v2 event digest가 손상되었습니다.")
    return seq, event


class _BoundAuditAdapter:
    def __init__(self, connection: sqlite3.Connection, org: TenantOrgId) -> None:
        if type(org) is not TenantOrgId:
            raise ValueError("audit adapter는 exact TenantOrgId에 결박돼야 합니다.")
        self._connection = connection
        self._org = org

    def _scope(self, org: TenantOrgId) -> None:
        if type(org) is not TenantOrgId or org != self._org:
            raise ValueError("bound audit adapter org가 일치하지 않습니다.")
        open_sqlite_tenant_port_audit_v2(self._connection).validate_only()

    def _events(self) -> list[tuple[int, SafeAuditEvent]]:
        rows = self._connection.execute(
            "SELECT seq, org_id, action, subject_id, outcome, fingerprint, event_digest, created_at "
            "FROM operational_audit_events_v2 WHERE org_id COLLATE BINARY=? ORDER BY seq",
            (self._org.value,),
        ).fetchall()
        events = [_decode(row, self._org) for row in rows]
        if [seq for seq, _ in events] != list(range(len(events))):
            raise ValueError("audit v2 sequence가 contiguous하지 않습니다.")
        return events


class SqliteTenantAuditReader(_BoundAuditAdapter):
    def list(self, org: TenantOrgId) -> tuple[SafeAuditEvent, ...] | ScopedUnavailable:
        try:
            self._scope(org)
            result = tuple(event for _, event in self._events())
            self._scope(org)
            self._events()
            return result
        except Exception:
            return ScopedUnavailable()

    def detail(self, org: TenantOrgId, sequence: int) -> SafeAuditEvent | ScopedUnavailable:
        if type(sequence) is not int or sequence < 0:
            return ScopedUnavailable()
        try:
            self._scope(org)
            events = self._events()
            self._scope(org)
            self._events()
            return events[sequence][1] if sequence < len(events) else ScopedUnavailable()
        except Exception:
            return ScopedUnavailable()


class SqliteTenantAuditWriter(_BoundAuditAdapter):
    def append(self, org: TenantOrgId, event: SafeAuditEvent) -> None | ScopedUnavailable:
        if type(event) is not SafeAuditEvent:
            return ScopedUnavailable()
        try:
            self._scope(org)
            self._events()
            self._connection.execute("BEGIN IMMEDIATE")
            self._scope(org)
            events = self._events()
            digest = _event_digest(event)
            existing = self._connection.execute(
                "SELECT seq, org_id, action, subject_id, outcome, fingerprint, event_digest, created_at "
                "FROM operational_audit_events_v2 WHERE org_id COLLATE BINARY=? AND event_digest COLLATE BINARY=?",
                (self._org.value, digest),
            ).fetchone()
            if existing is not None:
                if _decode(existing, self._org)[1] != event:
                    raise ValueError("same digest audit event가 canonical equality와 다릅니다.")
                self._scope(org)
                self._events()
                self._connection.commit()
                return None
            self._connection.execute(
                "INSERT INTO operational_audit_events_v2 "
                "(org_id, seq, action, subject_id, outcome, fingerprint, event_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (
                    self._org.value,
                    len(events),
                    event.action,
                    event.subject_id,
                    event.outcome,
                    event.fingerprint.value,
                    digest,
                ),
            )
            self._scope(org)
            self._events()
            self._connection.commit()
            return None
        except Exception:
            if self._connection.in_transaction:
                self._connection.rollback()
            return ScopedUnavailable()
