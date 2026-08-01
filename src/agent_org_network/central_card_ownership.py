"""Durable Central Card Owner assignment foundation (v20→v21).

This component owns only Central metadata/control state.  It never calls an
Owner API, worker, websocket, or credential service during a transfer.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from agent_org_network.sqlite_production_agent_cards import (
    ProductionAgentCardUnavailable,
    validate_production_agent_card_rows,
    validate_production_agent_card_connection,
)


_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
SCHEMA_VERSION = 21


class CardOwnershipUnavailable(RuntimeError):
    pass


class CardOwnershipConflict(CardOwnershipUnavailable):
    pass


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class CardOwnerAssignmentView(_Frozen):
    assignment_id: str
    org_id: str
    card_id: str
    generation: int = Field(gt=0)
    owner_user_id: str
    card_revision: int = Field(gt=0)
    card_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["active", "revoked"]
    assigned_at: datetime
    revoked_at: datetime | None = None


class TransferCardOwner(_Frozen):
    kind: Literal["transfer"] = "transfer"
    expected_card_revision: int = Field(gt=0)
    expected_generation: int = Field(gt=0)
    target_owner_user_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    approval_evidence_id: str = Field(min_length=1)
    approval_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RevokeCardOwner(_Frozen):
    kind: Literal["revoke"] = "revoke"
    expected_card_revision: int = Field(gt=0)
    expected_generation: int = Field(gt=0)
    approval_evidence_id: str = Field(min_length=1)
    approval_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


OwnerCommand = TransferCardOwner | RevokeCardOwner


class CardOwnerChangeReceipt(_Frozen):
    receipt_id: str
    operation: Literal["transferred", "revoked"]
    card_id: str
    previous_assignment_id: str
    assignment_id: str
    generation: int = Field(gt=0)
    owner_user_id: str
    card_revision: int = Field(gt=0)
    card_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_evidence_id: str
    approval_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    replayed: bool


class OrganizationGraph(_Frozen):
    users: tuple[dict[str, str | None], ...]
    cards: tuple[dict[str, str | int | None], ...]
    edges: tuple[dict[str, str], ...]


class OrganizationScorecardRow(_Frozen):
    owner_user_id: str
    assignment_generation: int = Field(gt=0)
    total_answers: int = Field(ge=0)
    corrected_count: int = Field(ge=0)
    online_ratio: float | None = Field(default=None, ge=0, le=1)
    stale_ratio: float = Field(ge=0, le=1)


class OwnerApprovalPort(Protocol):
    def authorize(
        self, *, org_id: str, actor_user_id: str, card_id: str, command_digest: str,
        evidence_id: str, evidence_digest: str, operation: Literal["transfer", "revoke"],
    ) -> bool: ...


class CardRegistryMutationPort(Protocol):
    """Same-UoW production Card/Registry mutation seam.

    The production catalogs currently have immutable registration receipts.  A
    caller must provide this seam when it can atomically append the next Card
    and Registry revisions; the foundation never edits those tables itself.
    """

    def mutate(
        self, *, transaction: sqlite3.Connection, org_id: str, card_id: str,
        operation: Literal["transfer", "revoke"], from_owner_user_id: str,
        to_owner_user_id: str | None, expected_card_revision: int,
        expected_generation: int,
    ) -> bool: ...


_DDL = (
    "CREATE TABLE IF NOT EXISTS central_card_owner_component_schema "
    "(name TEXT PRIMARY KEY NOT NULL CHECK(name='central-card-owner'), version INTEGER NOT NULL CHECK(version=21))",
    "CREATE TABLE IF NOT EXISTS central_card_owner_assignments ("
    "assignment_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, card_id TEXT NOT NULL,"
    "generation INTEGER NOT NULL CHECK(generation>0), owner_user_id TEXT NOT NULL,"
    "card_revision INTEGER NOT NULL CHECK(card_revision>0), card_digest TEXT NOT NULL CHECK(length(card_digest)=64),"
    "state TEXT NOT NULL CHECK(state IN ('active','revoked')), assigned_at TEXT NOT NULL, revoked_at TEXT,"
    "UNIQUE(org_id,card_id,generation),"
    "FOREIGN KEY(org_id,card_id) REFERENCES production_agent_cards(org_id,agent_id),"
    "FOREIGN KEY(org_id,owner_user_id) REFERENCES production_registry_users(org_id,user_id))",
    "CREATE TABLE IF NOT EXISTS central_card_owner_change_receipts ("
    "receipt_id TEXT PRIMARY KEY NOT NULL, org_id TEXT NOT NULL, card_id TEXT NOT NULL, operation TEXT NOT NULL,"
    "idempotency_key TEXT NOT NULL, command_digest TEXT NOT NULL CHECK(length(command_digest)=64),"
    "previous_assignment_id TEXT NOT NULL, assignment_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>0),"
    "owner_user_id TEXT NOT NULL, card_revision INTEGER NOT NULL CHECK(card_revision>0),"
    "card_digest TEXT NOT NULL CHECK(length(card_digest)=64), evidence_id TEXT NOT NULL,"
    "evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64), created_at TEXT NOT NULL,"
    "UNIQUE(org_id,idempotency_key), FOREIGN KEY(previous_assignment_id) REFERENCES central_card_owner_assignments(assignment_id),"
    "FOREIGN KEY(assignment_id) REFERENCES central_card_owner_assignments(assignment_id))",
    "CREATE UNIQUE INDEX IF NOT EXISTS central_card_owner_active_card "
    "ON central_card_owner_assignments(org_id,card_id) WHERE state='active'",
    "CREATE INDEX IF NOT EXISTS central_card_owner_card_generation "
    "ON central_card_owner_assignments(org_id,card_id,generation)",
    "CREATE TRIGGER IF NOT EXISTS central_card_owner_assignment_immutable "
    "BEFORE UPDATE ON central_card_owner_assignments "
    "WHEN OLD.assignment_id != NEW.assignment_id OR OLD.org_id != NEW.org_id "
    "OR OLD.card_id != NEW.card_id OR OLD.generation != NEW.generation "
    "OR OLD.owner_user_id != NEW.owner_user_id OR OLD.card_revision != NEW.card_revision "
    "OR OLD.card_digest != NEW.card_digest OR OLD.assigned_at != NEW.assigned_at "
    "OR OLD.state != 'active' OR NEW.state != 'revoked' OR NEW.revoked_at IS NULL "
    "BEGIN SELECT RAISE(ABORT,'immutable assignment'); END",
    "CREATE TRIGGER IF NOT EXISTS central_card_owner_assignment_no_delete "
    "BEFORE DELETE ON central_card_owner_assignments "
    "BEGIN SELECT RAISE(ABORT,'immutable assignment'); END",
    "CREATE TRIGGER IF NOT EXISTS central_card_owner_receipt_immutable "
    "BEFORE UPDATE ON central_card_owner_change_receipts "
    "BEGIN SELECT RAISE(ABORT,'immutable receipt'); END",
    "CREATE TRIGGER IF NOT EXISTS central_card_owner_receipt_no_delete "
    "BEFORE DELETE ON central_card_owner_change_receipts "
    "BEGIN SELECT RAISE(ABORT,'immutable receipt'); END",
)


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    target = f"file:{path}?mode=ro" if readonly else str(path)
    connection = sqlite3.connect(target, uri=readonly)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise CardOwnershipUnavailable("foreign key enforcement unavailable")
    except Exception:
        connection.close()
        raise
    return connection


def _ownership_catalog(connection: sqlite3.Connection) -> tuple[object, ...]:
    names = tuple(
        (str(row[0]), str(row[1]), str(row[2]), " ".join(str(row[3] or "").split()))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name LIKE 'central_card_owner_%' ORDER BY type,name"
        )
    )
    details: list[object] = []
    for table in (
        "central_card_owner_assignments",
        "central_card_owner_change_receipts",
        "central_card_owner_component_schema",
    ):
        details.append(
            (
                table,
                tuple(tuple(row) for row in connection.execute(f"PRAGMA table_info('{table}')")),
                tuple(tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list('{table}')")),
                tuple(tuple(row) for row in connection.execute(f"PRAGMA index_list('{table}')")),
            )
        )
        for index in connection.execute(f"PRAGMA index_list('{table}')"):
            details.append(
                (
                    str(index[1]),
                    tuple(tuple(row) for row in connection.execute(f"PRAGMA index_info('{index[1]}')")),
                )
            )
    return names, tuple(details)


def _canonical_ownership_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in _DDL:
            connection.execute(statement)
        return _ownership_catalog(connection)
    finally:
        connection.close()


_CANONICAL_OWNERSHIP_CATALOG = _canonical_ownership_catalog()


def _validate_production_source(connection: sqlite3.Connection) -> None:
    """Production Card/Registry is an immutable input, never a repair target."""
    try:
        validate_production_agent_card_connection(connection)
        orgs = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT org_id FROM production_registry_revisions ORDER BY org_id"
            )
        )
        if not orgs:
            raise CardOwnershipUnavailable("production registry unavailable")
        for org_id in orgs:
            validate_production_agent_card_rows(connection, org_id)
    except (CardOwnershipUnavailable, ProductionAgentCardUnavailable):
        raise CardOwnershipUnavailable("production card/registry unavailable")
    except (sqlite3.Error, TypeError, ValueError) as error:
        raise CardOwnershipUnavailable("production card/registry unavailable") from error


def _validate_owned_schema(connection: sqlite3.Connection) -> None:
    if _ownership_catalog(connection) != _CANONICAL_OWNERSHIP_CATALOG:
        raise CardOwnershipUnavailable("ownership catalog unavailable")


def migrate_central_card_ownership_schema(path: Path, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
    connection = _connect(path)
    try:
        _validate_production_source(connection)
        connection.execute("BEGIN IMMEDIATE")
        _validate_production_source(connection)
        for statement in _DDL:
            connection.execute(statement)
        if _ownership_catalog(connection) != _CANONICAL_OWNERSHIP_CATALOG:
            raise CardOwnershipUnavailable("ownership catalog unavailable")
        marker = connection.execute(
            "SELECT version FROM central_card_owner_component_schema WHERE name='central-card-owner'"
        ).fetchone()
        if marker is None:
            connection.execute(
                "INSERT INTO central_card_owner_component_schema VALUES ('central-card-owner',21)"
            )
        elif marker[0] != SCHEMA_VERSION:
            raise CardOwnershipUnavailable("unsupported ownership marker")
        now = _utc(clock())
        cards = connection.execute(
            "SELECT org_id,agent_id,owner_id,revision,card_digest FROM production_agent_cards ORDER BY org_id,agent_id"
        ).fetchall()
        for org_id, card_id, owner_id, revision, digest in cards:
            exists = connection.execute(
                "SELECT assignment_id,state,owner_user_id,card_revision,card_digest "
                "FROM central_card_owner_assignments WHERE org_id=? AND card_id=? "
                "ORDER BY generation DESC LIMIT 1",
                (org_id, card_id),
            ).fetchone()
            if exists is None:
                connection.execute(
                    "INSERT INTO central_card_owner_assignments VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (_digest({"assignment": org_id, "card": card_id, "generation": 1}), org_id, card_id,
                     1, owner_id, revision, digest, "active", now, None),
                )
            elif exists[1] == "active" and (
                exists[2] != owner_id or int(exists[3]) != int(revision) or exists[4] != digest
            ):
                # A central-only transfer cannot silently rewrite an immutable
                # production Card receipt.  Source/assignment reconciliation
                # belongs to the next same-UoW integration slice.
                raise CardOwnershipUnavailable("production binding drift")
        connection.commit()
    except CardOwnershipUnavailable:
        connection.rollback()
        raise
    except Exception as error:
        connection.rollback()
        raise CardOwnershipUnavailable("ownership schema unavailable") from error
    finally:
        connection.close()


def ownership_schema_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with _connect(path, readonly=True) as connection:
            return (
                connection.execute(
                    "SELECT version FROM central_card_owner_component_schema WHERE name='central-card-owner'"
                ).fetchone() == (SCHEMA_VERSION,)
                and _ownership_catalog(connection) == _CANONICAL_OWNERSHIP_CATALOG
            )
    except (sqlite3.Error, CardOwnershipUnavailable):
        return False


class CardOwnerAssignmentApplication:
    def __init__(
        self, path: Path, approval: OwnerApprovalPort, *,
        mutation: CardRegistryMutationPort | None = None,
        metadata_only: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not ownership_schema_ready(path):
            raise CardOwnershipUnavailable("ownership schema unavailable")
        if mutation is not None and metadata_only:
            raise ValueError("mutation seam and metadata-only mode are exclusive")
        self._path, self._approval, self._mutation, self._metadata_only, self._clock = path, approval, mutation, metadata_only, clock

    def current(self, *, org_id: str, card_id: str) -> CardOwnerAssignmentView:
        with _connect(self._path) as connection:
            connection.row_factory = sqlite3.Row
            _validate_owned_schema(connection)
            _validate_production_source(connection)
            rows = connection.execute(
                "SELECT * FROM central_card_owner_assignments WHERE org_id=? AND card_id=? AND state='active'",
                (org_id, card_id),
            ).fetchall()
            if len(rows) != 1:
                raise CardOwnershipUnavailable("active assignment unavailable")
            return _assignment(rows[0])

    def apply(self, *, org_id: str, actor_user_id: str, card_id: str, command: OwnerCommand, idempotency_key: str) -> CardOwnerChangeReceipt:
        if not _REF.fullmatch(org_id) or not _REF.fullmatch(actor_user_id) or not _REF.fullmatch(card_id) or not _REF.fullmatch(idempotency_key):
            raise CardOwnershipUnavailable("invalid ownership command")
        digest = _command_digest(org_id, actor_user_id, card_id, command)
        with _connect(self._path) as connection:
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN IMMEDIATE")
                _validate_owned_schema(connection)
                _validate_production_source(connection)
                existing = connection.execute(
                    "SELECT * FROM central_card_owner_change_receipts WHERE org_id=? AND idempotency_key=?",
                    (org_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["command_digest"] != digest:
                        raise CardOwnershipConflict("ownership idempotency conflict")
                    if not self._approval.authorize(
                        org_id=org_id, actor_user_id=actor_user_id, card_id=card_id,
                        command_digest=digest, evidence_id=command.approval_evidence_id,
                        evidence_digest=command.approval_evidence_digest, operation=command.kind,
                    ):
                        raise CardOwnershipConflict("owner approval unavailable")
                    connection.rollback()
                    return _receipt(existing, replayed=True)
                current = connection.execute(
                    "SELECT * FROM central_card_owner_assignments WHERE org_id=? AND card_id=? AND state='active'",
                    (org_id, card_id),
                ).fetchone()
                if current is None:
                    raise CardOwnershipUnavailable("active assignment unavailable")
                if int(current["generation"]) != command.expected_generation or int(current["card_revision"]) != command.expected_card_revision:
                    raise CardOwnershipConflict("stale owner assignment")
                if isinstance(command, TransferCardOwner) and command.target_owner_user_id == current["owner_user_id"]:
                    raise CardOwnershipConflict("self transfer is a no-op")
                if not self._metadata_only and self._mutation is None:
                    raise CardOwnershipUnavailable(
                        "production Card/Registry same-UoW mutation unavailable"
                    )
                if not self._approval.authorize(
                    org_id=org_id, actor_user_id=actor_user_id, card_id=card_id,
                    command_digest=digest, evidence_id=command.approval_evidence_id,
                    evidence_digest=command.approval_evidence_digest, operation=command.kind,
                ):
                    raise CardOwnershipConflict("owner approval unavailable")
                now = _utc(self._clock())
                operation: Literal["transferred", "revoked"]
                if isinstance(command, TransferCardOwner):
                    target = connection.execute(
                        "SELECT 1 FROM production_registry_users WHERE org_id=? AND user_id=?",
                        (org_id, command.target_owner_user_id),
                    ).fetchone()
                    if target is None:
                        raise CardOwnershipConflict("target owner unavailable")
                    owner = command.target_owner_user_id
                    state = "active"
                    operation = "transferred"
                else:
                    owner = str(current["owner_user_id"])
                    state = "revoked"
                    operation = "revoked"
                if not self._approval.authorize(
                    org_id=org_id, actor_user_id=actor_user_id, card_id=card_id,
                    command_digest=digest, evidence_id=command.approval_evidence_id,
                    evidence_digest=command.approval_evidence_digest, operation=command.kind,
                ):
                    raise CardOwnershipConflict("owner approval unavailable")
                if not self._metadata_only:
                    assert self._mutation is not None
                    if not self._mutation.mutate(
                        transaction=connection, org_id=org_id, card_id=card_id,
                        operation=command.kind, from_owner_user_id=str(current["owner_user_id"]),
                        to_owner_user_id=(command.target_owner_user_id if isinstance(command, TransferCardOwner) else None),
                        expected_card_revision=int(command.expected_card_revision),
                        expected_generation=int(command.expected_generation),
                    ):
                        raise CardOwnershipUnavailable("production Card/Registry mutation unavailable")
                updated = connection.execute(
                    "UPDATE central_card_owner_assignments SET state='revoked',revoked_at=? WHERE assignment_id=? AND state='active'",
                    (now, current["assignment_id"]),
                )
                if updated.rowcount != 1:
                    raise CardOwnershipConflict("stale owner assignment")
                generation = int(current["generation"]) + 1 if isinstance(command, TransferCardOwner) else int(current["generation"])
                if isinstance(command, TransferCardOwner):
                    assignment_id = _digest({"assignment": org_id, "card": card_id, "generation": generation, "command": digest})
                    connection.execute(
                        "INSERT INTO central_card_owner_assignments VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (assignment_id, org_id, card_id, generation, owner, current["card_revision"], current["card_digest"], state, now, None),
                    )
                else:
                    # Revocation has no successor assignment; the revoked historical
                    # row remains the durable record and active projection becomes empty.
                    assignment_id = str(current["assignment_id"])
                receipt_id = _digest({"owner-receipt": org_id, "key": idempotency_key})
                connection.execute(
                    "INSERT INTO central_card_owner_change_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id, org_id, card_id, operation, idempotency_key, digest, current["assignment_id"],
                     assignment_id, generation, owner, current["card_revision"], current["card_digest"],
                     command.approval_evidence_id, command.approval_evidence_digest, now),
                )
                connection.commit()
                return CardOwnerChangeReceipt(
                    receipt_id=receipt_id, operation=operation, card_id=card_id,
                    previous_assignment_id=str(current["assignment_id"]), assignment_id=assignment_id,
                    generation=generation, owner_user_id=owner, card_revision=int(current["card_revision"]),
                    card_digest=str(current["card_digest"]), approval_evidence_id=command.approval_evidence_id,
                    approval_evidence_digest=command.approval_evidence_digest, replayed=False,
                )
            except (CardOwnershipConflict, CardOwnershipUnavailable):
                connection.rollback()
                raise
            except Exception as error:
                connection.rollback()
                raise CardOwnershipUnavailable("ownership change unavailable") from error

    def graph(self, *, org_id: str) -> OrganizationGraph:
        with _connect(self._path) as connection:
            _validate_owned_schema(connection)
            _validate_production_source(connection)
            users = tuple({"user_id": str(row[0]), "manager_id": cast(str | None, row[1])} for row in connection.execute("SELECT user_id,manager_id FROM production_registry_users WHERE org_id=? ORDER BY user_id", (org_id,)))
            cards = tuple(
                {
                    "kind": "agent_card",
                    "card_id": str(row[0]),
                    "card_revision": int(row[1]),
                    "assignment_status": str(row[2]),
                    "assignment_generation": int(row[3]),
                    "current_owner_user_id": (str(row[4]) if row[2] == "active" else None),
                    "recorded_owner_user_id": str(row[5]),
                    "team": str(row[6]),
                }
                for row in connection.execute(
                    "SELECT c.agent_id,c.revision,a.state,a.generation,a.owner_user_id,c.owner_id,c.card_json "
                    "FROM production_agent_cards AS c JOIN central_card_owner_assignments AS a "
                    "ON a.org_id=c.org_id AND a.card_id=c.agent_id "
                    "AND a.generation=(SELECT max(a2.generation) FROM central_card_owner_assignments a2 "
                    "WHERE a2.org_id=a.org_id AND a2.card_id=a.card_id) "
                    "WHERE c.org_id=? ORDER BY c.agent_id", (org_id,)
                )
            )
            # Team is safe metadata from the already validated production Card.
            cards = tuple(
                {**card, "team": _card_team(connection, org_id, str(card["card_id"]))}
                for card in cards
            )
            edges = tuple(
                {"from_id": str(row[1]), "to_id": str(row[0]), "kind": "manages"}
                for row in connection.execute("SELECT user_id,manager_id FROM production_registry_users WHERE org_id=? AND manager_id IS NOT NULL ORDER BY user_id", (org_id,))
            ) + tuple(
                {"from_id": str(row[1]), "to_id": str(row[0]), "kind": "owns"}
                for row in connection.execute("SELECT card_id,owner_user_id FROM central_card_owner_assignments WHERE org_id=? AND state='active' ORDER BY card_id", (org_id,))
            ) + tuple(
                {"from_id": str(row[1]), "to_id": str(row[0]), "kind": "maintains"}
                for row in connection.execute(
                    "SELECT agent_id,maintainer_id FROM production_agent_cards "
                    "WHERE org_id=? AND maintainer_id IS NOT NULL ORDER BY agent_id", (org_id,)
                )
            )
            return OrganizationGraph(users=users, cards=cards, edges=edges)

    def scorecard(self, *, org_id: str) -> tuple[OrganizationScorecardRow, ...]:
        with _connect(self._path) as connection:
            _validate_owned_schema(connection)
            _validate_production_source(connection)
            rows = connection.execute(
                "SELECT owner_user_id,generation,card_revision,card_digest FROM central_card_owner_assignments "
                "WHERE org_id=? AND state='active' ORDER BY owner_user_id,generation,card_revision,card_digest",
                (org_id,),
            ).fetchall()
            return tuple(
                OrganizationScorecardRow(
                    owner_user_id=str(owner), assignment_generation=int(generation),
                    total_answers=0, corrected_count=0, online_ratio=None, stale_ratio=0.0,
                )
                for owner, generation, _card_revision, _card_digest in rows
            )


def _utc(value: datetime) -> str:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise CardOwnershipUnavailable("UTC clock required")
    return value.isoformat().replace("+00:00", "Z")


def _card_team(connection: sqlite3.Connection, org_id: str, card_id: str) -> str:
    row = connection.execute(
        "SELECT card_json FROM production_agent_cards WHERE org_id=? AND agent_id=?",
        (org_id, card_id),
    ).fetchone()
    if row is None:
        raise CardOwnershipUnavailable("card unavailable")
    try:
        payload = json.loads(str(row[0]))
        team = payload["team"]
        if not isinstance(team, str) or not team.strip():
            raise ValueError
        return team
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise CardOwnershipUnavailable("card metadata unavailable") from error


def _digest(value: object) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _command_digest(org_id: str, actor_user_id: str, card_id: str, command: OwnerCommand) -> str:
    return _digest({"org_id": org_id, "actor_user_id": actor_user_id, "card_id": card_id, **command.model_dump(mode="json")})


def _assignment(row: sqlite3.Row) -> CardOwnerAssignmentView:
    return CardOwnerAssignmentView(
        assignment_id=str(row["assignment_id"]), org_id=str(row["org_id"]), card_id=str(row["card_id"]),
        generation=int(row["generation"]), owner_user_id=str(row["owner_user_id"]),
        card_revision=int(row["card_revision"]), card_digest=str(row["card_digest"]), state=cast(Literal["active", "revoked"], row["state"]),
        assigned_at=datetime.fromisoformat(str(row["assigned_at"]).replace("Z", "+00:00")),
        revoked_at=(datetime.fromisoformat(str(row["revoked_at"]).replace("Z", "+00:00")) if row["revoked_at"] else None),
    )


def _receipt(row: sqlite3.Row, *, replayed: bool) -> CardOwnerChangeReceipt:
    return CardOwnerChangeReceipt(
        receipt_id=str(row["receipt_id"]), operation=cast(Literal["transferred", "revoked"], row["operation"]),
        card_id=str(row["card_id"]), previous_assignment_id=str(row["previous_assignment_id"]),
        assignment_id=str(row["assignment_id"]), generation=int(row["generation"]), owner_user_id=str(row["owner_user_id"]),
        card_revision=int(row["card_revision"]), card_digest=str(row["card_digest"]),
        approval_evidence_id=str(row["evidence_id"]), approval_evidence_digest=str(row["evidence_digest"]), replayed=replayed,
    )
