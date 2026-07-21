"""S3.1c org-bound adapters over the S2 tenant-state repositories only."""
# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownLambdaType=false, reportOperatorIssue=false
from __future__ import annotations

from datetime import UTC, datetime
import sqlite3
from typing import Final

from agent_org_network.sqlite_operational_tenant_sources import _strict_registry_payload, _timestamp, open_sqlite_operational_tenant_sources
from agent_org_network.tenant_operational_ports import (
    ResourceFingerprint,
    ScopedUnavailable,
    TenantCard,
    TenantOrgId,
    TenantSession,
)

_CARD_TAG: Final = "tenant-card-v1"
_SESSION_TAG: Final = "tenant-session-v1"


def _now() -> str:
    value = datetime.now(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _opaque(value: object) -> str:
    if type(value) is not str or not value or len(value) > 256:
        raise ValueError("opaque id가 유효하지 않습니다.")
    return value


def _bound(org: object, expected: TenantOrgId) -> None:
    if type(org) is not TenantOrgId or org != expected:
        raise ValueError("adapter org가 정확히 일치하지 않습니다.")


def _card(org: TenantOrgId, revision: int, card_id: object, owner_id: object) -> TenantCard:
    if type(revision) is not int or revision < 0:
        raise ValueError("registry revision이 유효하지 않습니다.")
    card = _opaque(card_id)
    owner = _opaque(owner_id)
    return TenantCard(card, owner, ResourceFingerprint.from_scalars(_CARD_TAG, org.value, str(revision), card, owner))


def _session(org: TenantOrgId, row: object) -> TenantSession:
    if not isinstance(row, tuple) or len(row) != 6:
        raise ValueError("session row가 유효하지 않습니다.")
    session_id, user_id, status, started_at, last_active_at, revision = row
    if status not in {"active", "ended"} or type(revision) is not int or revision < 0:
        raise ValueError("session state가 유효하지 않습니다.")
    _timestamp(started_at)
    _timestamp(last_active_at)
    session = _opaque(session_id)
    user = _opaque(user_id)
    return TenantSession(
        session,
        user,
        status,
        ResourceFingerprint.from_scalars(_SESSION_TAG, org.value, session, user, status, str(revision)),
    )


def _graph(payload: object) -> dict[str, object]:
    result = _strict_registry_payload(payload)
    users = result["users"]
    cards = result["cards"]
    refs = result["manager_refs"]
    assert isinstance(users, list) and isinstance(cards, dict) and isinstance(refs, dict)
    if len(set(users)) != len(users):
        raise ValueError("registry user가 중복되었습니다.")
    for user in users:
        _opaque(user)
    for card_id, raw in cards.items():
        _opaque(card_id)
        if not isinstance(raw, dict) or set(raw) != {"owner"}:
            raise ValueError("registry card가 strict하지 않습니다.")
        _opaque(raw["owner"])
    for child, manager in refs.items():
        _opaque(child)
        _opaque(manager)
        if child not in cards or manager not in cards:
            raise ValueError("registry manager reference가 dangling입니다.")
    return result


class _BoundAdapter:
    def __init__(self, connection: sqlite3.Connection, org: TenantOrgId) -> None:
        if type(org) is not TenantOrgId:
            raise ValueError("adapter는 exact TenantOrgId에 결박돼야 합니다.")
        self._connection = connection
        self._org = org

    def _capability(self, org: TenantOrgId) -> object:
        _bound(org, self._org)
        return open_sqlite_operational_tenant_sources(self._connection)


class SqliteTenantRegistryAdapter(_BoundAdapter):
    def _read(self) -> tuple[int, dict[str, object]] | None:
        capability = open_sqlite_operational_tenant_sources(self._connection)
        value = capability.registry(self._org.value).read()
        return None if value is None else (value[0], _graph(value[1]))

    def card(self, org: TenantOrgId, card_id: str) -> TenantCard | ScopedUnavailable:
        try:
            self._capability(org)
            card_id = _opaque(card_id)
            current = self._read()
            if current is None or card_id not in current[1]["cards"]:
                return ScopedUnavailable()
            raw = current[1]["cards"]
            assert isinstance(raw, dict)
            item = raw[card_id]
            assert isinstance(item, dict)
            result = _card(self._org, current[0], card_id, item["owner"])
            self._capability(org)
            return result
        except Exception:
            return ScopedUnavailable()

    def admit(self, org: TenantOrgId, card: TenantCard) -> TenantCard | ScopedUnavailable:
        if type(card) is not TenantCard:
            return ScopedUnavailable()
        try:
            self._capability(org)
            current = self._read()
            if current is None:
                return ScopedUnavailable()
            revision, payload = current
            # The input is an authority-bearing snapshot, not merely a card id/owner
            # pair.  A well-formed hash from another registry revision is stale.
            if card.fingerprint != _card(self._org, revision, card.card_id, card.owner_id).fingerprint:
                return ScopedUnavailable()
            cards = payload["cards"]
            users = payload["users"]
            assert isinstance(cards, dict) and isinstance(users, list)
            prior = cards.get(card.card_id)
            if prior is not None:
                if isinstance(prior, dict) and prior.get("owner") == card.owner_id:
                    return _card(self._org, revision, card.card_id, card.owner_id)
                return ScopedUnavailable()
            if card.owner_id not in users:
                return ScopedUnavailable()
            next_payload = {**payload, "cards": {**cards, card.card_id: {"owner": card.owner_id}}}
            repository = open_sqlite_operational_tenant_sources(self._connection).registry(self._org.value)
            if repository.compare_and_set(revision, next_payload, _now()):
                result = self.card(org, card.card_id)
                return result
            fresh = self._read()
            if (
                fresh is None
                or card.fingerprint
                != _card(self._org, fresh[0], card.card_id, card.owner_id).fingerprint
            ):
                return ScopedUnavailable()
            result = self.card(org, card.card_id)
            return result if isinstance(result, TenantCard) and result.owner_id == card.owner_id else ScopedUnavailable()
        except Exception:
            return ScopedUnavailable()

    def transfer(self, org: TenantOrgId, card_id: str, owner_id: str) -> TenantCard | ScopedUnavailable:
        try:
            self._capability(org)
            card_id, owner_id = _opaque(card_id), _opaque(owner_id)
            current = self._read()
            if current is None:
                return ScopedUnavailable()
            revision, payload = current
            cards, users = payload["cards"], payload["users"]
            assert isinstance(cards, dict) and isinstance(users, list)
            prior = cards.get(card_id)
            if not isinstance(prior, dict) or owner_id not in users:
                return ScopedUnavailable()
            if prior["owner"] == owner_id:
                return _card(self._org, revision, card_id, owner_id)
            next_cards = {**cards, card_id: {"owner": owner_id}}
            repository = open_sqlite_operational_tenant_sources(self._connection).registry(self._org.value)
            if repository.compare_and_set(revision, {**payload, "cards": next_cards}, _now()):
                return self.card(org, card_id)
            result = self.card(org, card_id)
            return result if isinstance(result, TenantCard) and result.owner_id == owner_id else ScopedUnavailable()
        except Exception:
            return ScopedUnavailable()


class SqliteTenantGraphAdapter(_BoundAdapter):
    def derive(self, org: TenantOrgId) -> tuple[TenantCard, ...] | ScopedUnavailable:
        try:
            self._capability(org)
            current = SqliteTenantRegistryAdapter(self._connection, self._org)._read()
            if current is None:
                return ()
            revision, payload = current
            cards, refs = payload["cards"], payload["manager_refs"]
            assert isinstance(cards, dict) and isinstance(refs, dict)
            def depth(card_id: str) -> int:
                result = 0
                while card_id in refs:
                    card_id = refs[card_id]  # child -> manager; roots are depth zero
                    result += 1
                return result
            ordered = sorted(cards, key=lambda identifier: (depth(identifier), identifier.encode()))
            result = tuple(_card(self._org, revision, card_id, cards[card_id]["owner"]) for card_id in ordered)
            self._capability(org)
            return result
        except Exception:
            return ScopedUnavailable()


class SqliteTenantSessionAdapter(_BoundAdapter):
    def session(self, org: TenantOrgId, session_id: str) -> TenantSession | ScopedUnavailable:
        try:
            self._capability(org)
            session_id = _opaque(session_id)
            row = open_sqlite_operational_tenant_sources(self._connection).sessions(self._org.value).get(session_id)
            if row is None:
                return ScopedUnavailable()
            result = _session(self._org, tuple(row))
            self._capability(org)
            return result
        except Exception:
            return ScopedUnavailable()

    def end(self, org: TenantOrgId, session_id: str) -> TenantSession | ScopedUnavailable:
        try:
            self._capability(org)
            session_id = _opaque(session_id)
            repository = open_sqlite_operational_tenant_sources(self._connection).sessions(self._org.value)
            current = repository.get(session_id)
            if current is None:
                return ScopedUnavailable()
            decoded = _session(self._org, tuple(current))
            if decoded.status == "ended":
                return decoded
            if repository.compare_and_set_end(session_id, tuple(current)[5], _now()):
                return self.session(org, session_id)
            result = self.session(org, session_id)
            return result if isinstance(result, TenantSession) and result.status == "ended" else ScopedUnavailable()
        except Exception:
            return ScopedUnavailable()


class SqliteTenantHitlAdapter(_BoundAdapter):
    def _row(self, card_id: str) -> tuple[bool, bool, int] | None:
        value = open_sqlite_operational_tenant_sources(self._connection).hitl(self._org.value).get(card_id)
        if value is not None and not value[1] and value[0]:
            raise ValueError("implicit HITL on row는 손상입니다.")
        return value

    def read(self, org: TenantOrgId, card_id: str) -> bool | ScopedUnavailable:
        try:
            self._capability(org)
            row = self._row(_opaque(card_id))
            self._capability(org)
            return False if row is None else row[0]
        except Exception:
            return ScopedUnavailable()

    def write(self, org: TenantOrgId, card_id: str, on: bool) -> bool | ScopedUnavailable:
        if type(on) is not bool:
            return ScopedUnavailable()
        try:
            self._capability(org)
            card_id = _opaque(card_id)
            row = self._row(card_id)
            if row is None:
                if not on:
                    return False
                expected = None
            else:
                if row[0] == on:
                    return on
                expected = row[2]
            repository = open_sqlite_operational_tenant_sources(self._connection).hitl(self._org.value)
            if repository.compare_and_set(card_id, expected, on, True, _now()):
                return self.read(org, card_id)
            result = self.read(org, card_id)
            return result if isinstance(result, bool) and result == on else ScopedUnavailable()
        except Exception:
            return ScopedUnavailable()
