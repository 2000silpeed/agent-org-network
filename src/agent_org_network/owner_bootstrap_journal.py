"""Secret-zero Owner installation bootstrap metadata journal."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OwnerBootstrapJournalUnavailable(Exception):
    pass


class OwnerBootstrapJournalConflict(OwnerBootstrapJournalUnavailable):
    pass


_DIGEST = re.compile(r"[0-9a-f]{64}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")


def _ref(value: str) -> str:
    if type(value) is not str or _REF.fullmatch(value) is None:
        raise ValueError("bounded reference required")
    return value


def _digest(value: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError("lowercase sha256 required")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0) or value.microsecond != 0:
        raise ValueError("canonical UTC second required")
    return value


class _Command(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    idempotency_key: str
    now: datetime

    @field_validator("idempotency_key")
    @classmethod
    def _idempotency_ref(cls, value: str) -> str:
        return _ref(value)

    @field_validator("now")
    @classmethod
    def _canonical_now(cls, value: datetime) -> datetime:
        return _utc(value)


class PrepareOwnerBootstrapAttempt(_Command, frozen=True):
    central_origin: str
    org_id: str
    owner_user_id: str
    agent_card_id: str
    device_key_thumbprint: str
    binding_digest: str

    @field_validator(
        "central_origin",
        "org_id",
        "owner_user_id",
        "agent_card_id",
        "device_key_thumbprint",
    )
    @classmethod
    def _refs(cls, value: str) -> str:
        return _ref(value)

    @field_validator("binding_digest")
    @classmethod
    def _binding_digest(cls, value: str) -> str:
        return _digest(value)


class MarkOwnerBootstrapKeyStored(_Command, frozen=True):
    owner_bootstrap_id: str
    expected_revision: int = Field(gt=0)
    bundle_revision: int = Field(gt=0)
    bundle_public_digest: str

    @field_validator("owner_bootstrap_id", "bundle_public_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return _digest(value)


class MarkOwnerBootstrapRecoveryLinked(_Command, frozen=True):
    owner_bootstrap_id: str
    expected_revision: int = Field(gt=0)
    recovery_profile_id: str
    recovery_resource_digest: str

    @field_validator("owner_bootstrap_id", "recovery_resource_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return _digest(value)

    @field_validator("recovery_profile_id")
    @classmethod
    def _profile_ref(cls, value: str) -> str:
        return _ref(value)


class CompleteOwnerBootstrap(_Command, frozen=True):
    owner_bootstrap_id: str
    expected_revision: Literal[3]
    recovery_profile_id: str
    recovery_resource_digest: str
    terminal_receipt_id: str
    terminal_receipt_digest: str
    verification: Literal["paired"]

    @field_validator(
        "owner_bootstrap_id",
        "recovery_resource_digest",
        "terminal_receipt_digest",
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        return _digest(value)

    @field_validator("recovery_profile_id", "terminal_receipt_id")
    @classmethod
    def _refs(cls, value: str) -> str:
        return _ref(value)


class OwnerBootstrapJournalResult(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal[
        "prepared",
        "joined",
        "key_stored",
        "recovery_linked",
        "completed",
        "replayed",
    ]
    owner_bootstrap_id: str
    state: Literal["prepared", "key_stored", "recovery_linked", "completed"]
    revision: int = Field(gt=0)
    updated_at: datetime

    @field_validator("owner_bootstrap_id")
    @classmethod
    def _bootstrap_digest(cls, value: str) -> str:
        return _digest(value)

    @field_validator("updated_at")
    @classmethod
    def _updated(cls, value: datetime) -> datetime:
        return _utc(value)


def _jcs(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _domain_digest(domain: bytes, value: object) -> str:
    return sha256(domain + _jcs(value).encode()).hexdigest()


def derive_owner_bootstrap_id(command: PrepareOwnerBootstrapAttempt) -> str:
    if type(command) is not PrepareOwnerBootstrapAttempt:
        raise OwnerBootstrapJournalUnavailable()
    return _domain_digest(
        b"aon.owner.bootstrap.id.v1\0",
        {
            "agent_card_id": command.agent_card_id,
            "binding_digest": command.binding_digest,
            "central_origin": command.central_origin,
            "device_key_thumbprint": command.device_key_thumbprint,
            "org_id": command.org_id,
            "owner_user_id": command.owner_user_id,
        },
    )


_SCHEMA = """
CREATE TABLE owner_bootstrap_attempts (
 owner_bootstrap_id TEXT PRIMARY KEY,
 central_origin TEXT NOT NULL, org_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
 agent_card_id TEXT NOT NULL, device_key_thumbprint TEXT NOT NULL,
 binding_digest TEXT NOT NULL, attempt_digest TEXT NOT NULL, created_at TEXT NOT NULL,
 CHECK(length(owner_bootstrap_id)=64), CHECK(length(binding_digest)=64),
 CHECK(length(attempt_digest)=64),
 UNIQUE(central_origin,org_id,owner_user_id,agent_card_id,device_key_thumbprint)
) STRICT;
CREATE TABLE owner_bootstrap_heads (
 owner_bootstrap_id TEXT PRIMARY KEY REFERENCES owner_bootstrap_attempts(owner_bootstrap_id),
 state TEXT NOT NULL, revision INTEGER NOT NULL, bundle_revision INTEGER,
 bundle_public_digest TEXT, recovery_profile_id TEXT, recovery_resource_digest TEXT,
 terminal_receipt_id TEXT, terminal_receipt_digest TEXT,
 updated_at TEXT NOT NULL,
 CHECK(state IN ('prepared','key_stored','recovery_linked','completed')), CHECK(revision>0),
 CHECK((state='prepared' AND revision=1 AND bundle_revision IS NULL
   AND bundle_public_digest IS NULL AND recovery_profile_id IS NULL
   AND recovery_resource_digest IS NULL AND terminal_receipt_id IS NULL
   AND terminal_receipt_digest IS NULL)
 OR (state='key_stored' AND revision=2 AND bundle_revision>0
   AND length(bundle_public_digest)=64 AND recovery_profile_id IS NULL
   AND recovery_resource_digest IS NULL AND terminal_receipt_id IS NULL
   AND terminal_receipt_digest IS NULL)
 OR (state='recovery_linked' AND revision=3 AND bundle_revision>0
   AND length(bundle_public_digest)=64 AND recovery_profile_id IS NOT NULL
   AND length(recovery_resource_digest)=64 AND terminal_receipt_id IS NULL
   AND terminal_receipt_digest IS NULL)
 OR (state='completed' AND revision=4 AND bundle_revision>0
   AND length(bundle_public_digest)=64 AND recovery_profile_id IS NOT NULL
   AND length(recovery_resource_digest)=64 AND terminal_receipt_id IS NOT NULL
   AND length(terminal_receipt_digest)=64))
) STRICT;
CREATE TABLE owner_bootstrap_transition_receipts (
 idempotency_key TEXT PRIMARY KEY,
 owner_bootstrap_id TEXT NOT NULL REFERENCES owner_bootstrap_attempts(owner_bootstrap_id),
 action TEXT NOT NULL, command_digest TEXT NOT NULL, command_json TEXT NOT NULL,
 result_json TEXT NOT NULL, created_at TEXT NOT NULL, receipt_digest TEXT NOT NULL,
 CHECK(action IN ('prepare','key.store','recovery.link','bootstrap.complete')),
 CHECK(length(command_digest)=64), CHECK(length(receipt_digest)=64)
) STRICT;
CREATE TRIGGER owner_bootstrap_attempts_no_update BEFORE UPDATE ON owner_bootstrap_attempts
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER owner_bootstrap_attempts_no_delete BEFORE DELETE ON owner_bootstrap_attempts
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER owner_bootstrap_receipts_no_update BEFORE UPDATE ON owner_bootstrap_transition_receipts
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER owner_bootstrap_receipts_no_delete BEFORE DELETE ON owner_bootstrap_transition_receipts
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER owner_bootstrap_head_exact_update BEFORE UPDATE ON owner_bootstrap_heads
WHEN OLD.owner_bootstrap_id!=NEW.owner_bootstrap_id OR NEW.revision!=OLD.revision+1
 OR NOT ((OLD.state='prepared' AND NEW.state='key_stored')
      OR (OLD.state='key_stored' AND NEW.state='recovery_linked')
      OR (OLD.state='recovery_linked' AND NEW.state='completed'))
BEGIN SELECT RAISE(ABORT,'invalid transition'); END;
"""


def _catalog(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )


def _expected_catalog() -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(":memory:")
    connection.executescript(_SCHEMA)
    value = _catalog(connection)
    connection.close()
    return value


_EXPECTED_CATALOG = _expected_catalog()


def _no_fault(_point: str) -> None:
    return None


def _instant(value: datetime) -> str:
    return _utc(value).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_instant(value: str) -> datetime:
    try:
        result = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise OwnerBootstrapJournalUnavailable() from error
    if _instant(result) != value:
        raise OwnerBootstrapJournalUnavailable()
    return result


class ProductionOwnerBootstrapExecutionFence:
    """Per-bootstrap OS advisory lock rooted at one canonical shared directory."""

    def __init__(self, lock_root: str | Path) -> None:
        if os.name == "nt":
            raise OwnerBootstrapJournalUnavailable()
        try:
            root = Path(lock_root)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = root.lstat()
            if (
                root.is_symlink()
                or root.resolve() != root.absolute()
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
            ):
                raise OwnerBootstrapJournalUnavailable()
            os.chmod(root, 0o700)
            info = root.lstat()
            if stat.S_IMODE(info.st_mode) != 0o700:
                raise OwnerBootstrapJournalUnavailable()
            self._root = root
            self._identity = (info.st_dev, info.st_ino)
        except OwnerBootstrapJournalUnavailable:
            raise
        except Exception as error:
            raise OwnerBootstrapJournalUnavailable() from error

    @contextmanager
    def acquire(self, owner_bootstrap_id: str) -> Generator[None]:
        descriptor: int | None = None
        try:
            import fcntl

            _digest(owner_bootstrap_id)
            root_info = self._root.lstat()
            if (
                (root_info.st_dev, root_info.st_ino) != self._identity
                or self._root.is_symlink()
                or self._root.resolve() != self._root.absolute()
                or not stat.S_ISDIR(root_info.st_mode)
                or stat.S_IMODE(root_info.st_mode) != 0o700
                or root_info.st_uid != os.geteuid()
            ):
                raise OwnerBootstrapJournalUnavailable()
            lock_path = self._root / f"{owner_bootstrap_id}.lock"
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            info = os.fstat(descriptor)
            path_info = lock_path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino)
            ):
                raise OwnerBootstrapJournalUnavailable()
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        except OwnerBootstrapJournalUnavailable:
            raise
        except Exception as error:
            raise OwnerBootstrapJournalUnavailable() from error
        finally:
            if descriptor is not None:
                os.close(descriptor)


class OwnerBootstrapJournal:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self._path = Path(path)
        self._clock = clock or (lambda: datetime.now(UTC).replace(microsecond=0))
        self._fault: Callable[[str], None] = fault or _no_fault
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if (
                self._path.parent.is_symlink()
                or self._path.parent.resolve() != self._path.parent.absolute()
            ):
                raise OwnerBootstrapJournalUnavailable()
            if not self._path.exists():
                descriptor = os.open(
                    self._path,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                os.close(descriptor)
            os.chmod(self._path, 0o600)
            info = self._path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise OwnerBootstrapJournalUnavailable()
            self._identity = (info.st_dev, info.st_ino)
            with sqlite3.connect(self._path) as connection:
                connection.row_factory = sqlite3.Row
                if not _catalog(connection):
                    connection.executescript(_SCHEMA)
                self._validate(connection)
        except OwnerBootstrapJournalUnavailable:
            raise
        except Exception as error:
            raise OwnerBootstrapJournalUnavailable() from error

    def _connect(self) -> sqlite3.Connection:
        info = self._path.lstat()
        if (
            (info.st_dev, info.st_ino) != self._identity
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise OwnerBootstrapJournalUnavailable()
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _validate(connection: sqlite3.Connection) -> None:
        if _catalog(connection) != _EXPECTED_CATALOG:
            raise OwnerBootstrapJournalUnavailable()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise OwnerBootstrapJournalUnavailable()
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise OwnerBootstrapJournalUnavailable()
        attempt_ids: set[str] = set()
        for row in connection.execute("SELECT * FROM owner_bootstrap_attempts"):
            projection = {
                "agent_card_id": row["agent_card_id"],
                "binding_digest": row["binding_digest"],
                "central_origin": row["central_origin"],
                "device_key_thumbprint": row["device_key_thumbprint"],
                "org_id": row["org_id"],
                "owner_user_id": row["owner_user_id"],
            }
            if (
                _domain_digest(b"aon.owner.bootstrap.id.v1\0", projection)
                != row["owner_bootstrap_id"]
                or _domain_digest(b"aon.owner.bootstrap.attempt.v1\0", projection)
                != row["attempt_digest"]
            ):
                raise OwnerBootstrapJournalUnavailable()
            _parse_instant(row["created_at"])
            attempt_ids.add(row["owner_bootstrap_id"])
        heads: dict[str, sqlite3.Row] = {}
        for row in connection.execute("SELECT * FROM owner_bootstrap_heads"):
            state = row["state"]
            valid = (
                (
                    state == "prepared"
                    and row["revision"] == 1
                    and row["bundle_revision"] is None
                    and row["bundle_public_digest"] is None
                    and row["recovery_profile_id"] is None
                    and row["recovery_resource_digest"] is None
                    and row["terminal_receipt_id"] is None
                    and row["terminal_receipt_digest"] is None
                )
                or (
                    state == "key_stored"
                    and row["revision"] == 2
                    and type(row["bundle_revision"]) is int
                    and row["bundle_revision"] > 0
                    and type(row["bundle_public_digest"]) is str
                    and _DIGEST.fullmatch(row["bundle_public_digest"]) is not None
                    and row["recovery_profile_id"] is None
                    and row["recovery_resource_digest"] is None
                    and row["terminal_receipt_id"] is None
                    and row["terminal_receipt_digest"] is None
                )
                or (
                    state == "recovery_linked"
                    and row["revision"] == 3
                    and type(row["bundle_revision"]) is int
                    and row["bundle_revision"] > 0
                    and type(row["bundle_public_digest"]) is str
                    and _DIGEST.fullmatch(row["bundle_public_digest"]) is not None
                    and type(row["recovery_profile_id"]) is str
                    and _REF.fullmatch(row["recovery_profile_id"]) is not None
                    and type(row["recovery_resource_digest"]) is str
                    and _DIGEST.fullmatch(row["recovery_resource_digest"]) is not None
                    and row["terminal_receipt_id"] is None
                    and row["terminal_receipt_digest"] is None
                )
                or (
                    state == "completed"
                    and row["revision"] == 4
                    and type(row["bundle_revision"]) is int
                    and row["bundle_revision"] > 0
                    and type(row["bundle_public_digest"]) is str
                    and _DIGEST.fullmatch(row["bundle_public_digest"]) is not None
                    and type(row["recovery_profile_id"]) is str
                    and _REF.fullmatch(row["recovery_profile_id"]) is not None
                    and type(row["recovery_resource_digest"]) is str
                    and _DIGEST.fullmatch(row["recovery_resource_digest"]) is not None
                    and type(row["terminal_receipt_id"]) is str
                    and _REF.fullmatch(row["terminal_receipt_id"]) is not None
                    and type(row["terminal_receipt_digest"]) is str
                    and _DIGEST.fullmatch(row["terminal_receipt_digest"]) is not None
                )
            )
            if not valid:
                raise OwnerBootstrapJournalUnavailable()
            _parse_instant(row["updated_at"])
            heads[row["owner_bootstrap_id"]] = row
        if set(heads) != attempt_ids:
            raise OwnerBootstrapJournalUnavailable()
        actions_by_bootstrap: dict[str, set[str]] = {
            owner_bootstrap_id: set() for owner_bootstrap_id in attempt_ids
        }
        for row in connection.execute("SELECT * FROM owner_bootstrap_transition_receipts"):
            command = json.loads(row["command_json"])
            result = json.loads(row["result_json"])
            if (
                type(command) is not dict
                or type(result) is not dict
                or row["owner_bootstrap_id"] not in heads
            ):
                raise OwnerBootstrapJournalUnavailable()
            command_value = cast(dict[str, object], command)
            result_value = cast(dict[str, object], result)
            idempotency_key = _ref(row["idempotency_key"])
            created_at = _parse_instant(row["created_at"])
            action = row["action"]
            command_types: dict[str, type[_Command]] = {
                "prepare": PrepareOwnerBootstrapAttempt,
                "key.store": MarkOwnerBootstrapKeyStored,
                "recovery.link": MarkOwnerBootstrapRecoveryLinked,
                "bootstrap.complete": CompleteOwnerBootstrap,
            }
            command_type = command_types.get(action)
            if command_type is None:
                raise OwnerBootstrapJournalUnavailable()
            validated_command = command_type.model_validate(
                {
                    **command_value,
                    "idempotency_key": idempotency_key,
                    "now": created_at,
                }
            )
            if (
                validated_command.model_dump(mode="json", exclude={"idempotency_key", "now"})
                != command_value
            ):
                raise OwnerBootstrapJournalUnavailable()
            parsed_result = OwnerBootstrapJournalResult.model_validate_json(row["result_json"])
            head = heads[row["owner_bootstrap_id"]]
            if (
                _jcs(command_value) != row["command_json"]
                or _jcs(result_value) != row["result_json"]
                or _domain_digest(
                    f"aon.owner.bootstrap.{row['action']}.v1\0".encode(),
                    command_value,
                )
                != row["command_digest"]
                or _domain_digest(
                    b"aon.owner.bootstrap.receipt.v1\0",
                    {
                        "action": row["action"],
                        "command_digest": row["command_digest"],
                        "created_at": row["created_at"],
                        "idempotency_key": row["idempotency_key"],
                        "owner_bootstrap_id": row["owner_bootstrap_id"],
                        "result_digest": sha256(row["result_json"].encode()).hexdigest(),
                    },
                )
                != row["receipt_digest"]
                or parsed_result.owner_bootstrap_id != row["owner_bootstrap_id"]
                or parsed_result.updated_at > created_at
            ):
                raise OwnerBootstrapJournalUnavailable()
            if action == "prepare":
                if (
                    set(command_value)
                    != {
                        "agent_card_id",
                        "binding_digest",
                        "central_origin",
                        "device_key_thumbprint",
                        "org_id",
                        "owner_user_id",
                    }
                    or _domain_digest(b"aon.owner.bootstrap.id.v1\0", command_value)
                    != row["owner_bootstrap_id"]
                    or parsed_result.kind not in {"prepared", "joined"}
                    or (parsed_result.state, parsed_result.revision)
                    not in {
                        ("prepared", 1),
                        ("key_stored", 2),
                        ("recovery_linked", 3),
                        ("completed", 4),
                    }
                ):
                    raise OwnerBootstrapJournalUnavailable()
            elif action == "key.store":
                if (
                    set(command_value)
                    != {
                        "bundle_public_digest",
                        "bundle_revision",
                        "expected_revision",
                        "owner_bootstrap_id",
                    }
                    or command_value.get("owner_bootstrap_id") != row["owner_bootstrap_id"]
                    or command_value.get("expected_revision") != 1
                    or command_value.get("bundle_revision") != head["bundle_revision"]
                    or command_value.get("bundle_public_digest") != head["bundle_public_digest"]
                    or parsed_result.kind not in {"key_stored", "joined"}
                    or parsed_result.state != "key_stored"
                    or parsed_result.revision != 2
                ):
                    raise OwnerBootstrapJournalUnavailable()
            elif action == "recovery.link":
                if (
                    set(command_value)
                    != {
                        "expected_revision",
                        "owner_bootstrap_id",
                        "recovery_profile_id",
                        "recovery_resource_digest",
                    }
                    or command_value.get("owner_bootstrap_id") != row["owner_bootstrap_id"]
                    or command_value.get("expected_revision") != 2
                    or command_value.get("recovery_profile_id") != head["recovery_profile_id"]
                    or command_value.get("recovery_resource_digest")
                    != head["recovery_resource_digest"]
                    or parsed_result.kind not in {"recovery_linked", "joined"}
                    or parsed_result.state != "recovery_linked"
                    or parsed_result.revision != 3
                ):
                    raise OwnerBootstrapJournalUnavailable()
            elif action == "bootstrap.complete":
                if (
                    set(command_value)
                    != {
                        "expected_revision",
                        "owner_bootstrap_id",
                        "recovery_profile_id",
                        "recovery_resource_digest",
                        "terminal_receipt_digest",
                        "terminal_receipt_id",
                        "verification",
                    }
                    or command_value.get("owner_bootstrap_id") != row["owner_bootstrap_id"]
                    or command_value.get("expected_revision") != 3
                    or command_value.get("recovery_profile_id") != head["recovery_profile_id"]
                    or command_value.get("recovery_resource_digest")
                    != head["recovery_resource_digest"]
                    or command_value.get("terminal_receipt_id") != head["terminal_receipt_id"]
                    or command_value.get("terminal_receipt_digest")
                    != head["terminal_receipt_digest"]
                    or command_value.get("verification") != "paired"
                    or parsed_result.kind not in {"completed", "joined"}
                    or parsed_result.state != "completed"
                    or parsed_result.revision != 4
                ):
                    raise OwnerBootstrapJournalUnavailable()
            else:
                raise OwnerBootstrapJournalUnavailable()
            actions_by_bootstrap[row["owner_bootstrap_id"]].add(action)
        for owner_bootstrap_id, head in heads.items():
            actions = actions_by_bootstrap[owner_bootstrap_id]
            if (
                "prepare" not in actions
                or (head["revision"] >= 2 and "key.store" not in actions)
                or (head["revision"] == 3 and "recovery.link" not in actions)
                or (
                    head["revision"] == 4
                    and ("recovery.link" not in actions or "bootstrap.complete" not in actions)
                )
            ):
                raise OwnerBootstrapJournalUnavailable()

    def _now(self) -> datetime:
        try:
            return _utc(self._clock())
        except Exception as error:
            raise OwnerBootstrapJournalUnavailable() from error

    @staticmethod
    def _result(row: sqlite3.Row, kind: str) -> OwnerBootstrapJournalResult:
        return OwnerBootstrapJournalResult(
            kind=cast(
                Literal[
                    "prepared",
                    "joined",
                    "key_stored",
                    "recovery_linked",
                    "completed",
                    "replayed",
                ],
                kind,
            ),
            owner_bootstrap_id=row["owner_bootstrap_id"],
            state=row["state"],
            revision=row["revision"],
            updated_at=_parse_instant(row["updated_at"]),
        )

    def _replay(
        self, connection: sqlite3.Connection, command: _Command, action: str
    ) -> OwnerBootstrapJournalResult | None:
        row = connection.execute(
            "SELECT * FROM owner_bootstrap_transition_receipts WHERE idempotency_key=?",
            (command.idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        projection = command.model_dump(mode="json", exclude={"idempotency_key", "now"})
        digest = _domain_digest(f"aon.owner.bootstrap.{action}.v1\0".encode(), projection)
        if row["action"] != action or row["command_digest"] != digest:
            raise OwnerBootstrapJournalConflict()
        stored = OwnerBootstrapJournalResult.model_validate_json(row["result_json"])
        return stored.model_copy(update={"kind": "replayed"})

    def _receipt(
        self,
        connection: sqlite3.Connection,
        *,
        command: _Command,
        action: str,
        result: OwnerBootstrapJournalResult,
        now: datetime,
    ) -> None:
        command_value = command.model_dump(mode="json", exclude={"idempotency_key", "now"})
        command_json = _jcs(command_value)
        command_digest = _domain_digest(
            f"aon.owner.bootstrap.{action}.v1\0".encode(), command_value
        )
        result_json = result.model_dump_json()
        result_json = _jcs(json.loads(result_json))
        created_at = _instant(now)
        receipt_digest = _domain_digest(
            b"aon.owner.bootstrap.receipt.v1\0",
            {
                "action": action,
                "command_digest": command_digest,
                "created_at": created_at,
                "idempotency_key": command.idempotency_key,
                "owner_bootstrap_id": result.owner_bootstrap_id,
                "result_digest": sha256(result_json.encode()).hexdigest(),
            },
        )
        connection.execute(
            "INSERT INTO owner_bootstrap_transition_receipts VALUES(?,?,?,?,?,?,?,?)",
            (
                command.idempotency_key,
                result.owner_bootstrap_id,
                action,
                command_digest,
                command_json,
                result_json,
                created_at,
                receipt_digest,
            ),
        )

    def prepare(self, command: PrepareOwnerBootstrapAttempt) -> OwnerBootstrapJournalResult:
        if type(command) is not PrepareOwnerBootstrapAttempt:
            raise OwnerBootstrapJournalUnavailable()
        bootstrap_id = derive_owner_bootstrap_id(command)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._validate(connection)
                replay = self._replay(connection, command, "prepare")
                if replay is not None:
                    return replay
                now = self._now()
                head = connection.execute(
                    "SELECT head.* FROM owner_bootstrap_heads AS head "
                    "JOIN owner_bootstrap_attempts AS attempt USING(owner_bootstrap_id) "
                    "WHERE attempt.central_origin=? AND attempt.org_id=? "
                    "AND attempt.owner_user_id=? AND attempt.agent_card_id=? "
                    "AND attempt.device_key_thumbprint=?",
                    (
                        command.central_origin,
                        command.org_id,
                        command.owner_user_id,
                        command.agent_card_id,
                        command.device_key_thumbprint,
                    ),
                ).fetchone()
                if head is not None:
                    attempt = connection.execute(
                        "SELECT * FROM owner_bootstrap_attempts WHERE owner_bootstrap_id=?",
                        (head["owner_bootstrap_id"],),
                    ).fetchone()
                    if (
                        attempt["owner_bootstrap_id"] != bootstrap_id
                        or attempt["binding_digest"] != command.binding_digest
                    ):
                        raise OwnerBootstrapJournalConflict()
                    result = self._result(head, "joined")
                    self._receipt(
                        connection,
                        command=command,
                        action="prepare",
                        result=result,
                        now=now,
                    )
                    return result
                attempt_projection = {
                    "agent_card_id": command.agent_card_id,
                    "binding_digest": command.binding_digest,
                    "central_origin": command.central_origin,
                    "device_key_thumbprint": command.device_key_thumbprint,
                    "org_id": command.org_id,
                    "owner_user_id": command.owner_user_id,
                }
                connection.execute(
                    "INSERT INTO owner_bootstrap_attempts VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        bootstrap_id,
                        command.central_origin,
                        command.org_id,
                        command.owner_user_id,
                        command.agent_card_id,
                        command.device_key_thumbprint,
                        command.binding_digest,
                        _domain_digest(b"aon.owner.bootstrap.attempt.v1\0", attempt_projection),
                        _instant(now),
                    ),
                )
                connection.execute(
                    "INSERT INTO owner_bootstrap_heads VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        bootstrap_id,
                        "prepared",
                        1,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        _instant(now),
                    ),
                )
                head = connection.execute(
                    "SELECT * FROM owner_bootstrap_heads WHERE owner_bootstrap_id=?",
                    (bootstrap_id,),
                ).fetchone()
                result = self._result(head, "prepared")
                self._receipt(
                    connection,
                    command=command,
                    action="prepare",
                    result=result,
                    now=now,
                )
                self._fault("before_prepare_commit")
                return result
        except OwnerBootstrapJournalUnavailable:
            raise
        except Exception as error:
            raise OwnerBootstrapJournalUnavailable() from error

    def mark_key_stored(self, command: MarkOwnerBootstrapKeyStored) -> OwnerBootstrapJournalResult:
        return self._advance(command, action="key.store")

    def mark_recovery_linked(
        self, command: MarkOwnerBootstrapRecoveryLinked
    ) -> OwnerBootstrapJournalResult:
        return self._advance(command, action="recovery.link")

    def complete(self, command: CompleteOwnerBootstrap) -> OwnerBootstrapJournalResult:
        if type(command) is not CompleteOwnerBootstrap:
            raise OwnerBootstrapJournalUnavailable()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._validate(connection)
                replay = self._replay(connection, command, "bootstrap.complete")
                if replay is not None:
                    return replay
                now = self._now()
                head = connection.execute(
                    "SELECT * FROM owner_bootstrap_heads WHERE owner_bootstrap_id=?",
                    (command.owner_bootstrap_id,),
                ).fetchone()
                if head is None:
                    raise OwnerBootstrapJournalConflict()
                if head["state"] == "completed":
                    if (
                        command.expected_revision != 3
                        or head["recovery_profile_id"] != command.recovery_profile_id
                        or head["recovery_resource_digest"] != command.recovery_resource_digest
                        or head["terminal_receipt_id"] != command.terminal_receipt_id
                        or head["terminal_receipt_digest"] != command.terminal_receipt_digest
                    ):
                        raise OwnerBootstrapJournalConflict()
                    result = self._result(head, "joined")
                    self._receipt(
                        connection,
                        command=command,
                        action="bootstrap.complete",
                        result=result,
                        now=now,
                    )
                    return result
                if (
                    head["state"] != "recovery_linked"
                    or head["revision"] != command.expected_revision
                    or head["recovery_profile_id"] != command.recovery_profile_id
                    or head["recovery_resource_digest"] != command.recovery_resource_digest
                ):
                    raise OwnerBootstrapJournalConflict()
                cursor = connection.execute(
                    "UPDATE owner_bootstrap_heads SET state='completed',revision=4,"
                    "terminal_receipt_id=?,terminal_receipt_digest=?,updated_at=? "
                    "WHERE owner_bootstrap_id=? AND state='recovery_linked' AND revision=3 "
                    "AND recovery_profile_id=? AND recovery_resource_digest=?",
                    (
                        command.terminal_receipt_id,
                        command.terminal_receipt_digest,
                        _instant(now),
                        command.owner_bootstrap_id,
                        command.recovery_profile_id,
                        command.recovery_resource_digest,
                    ),
                )
                if cursor.rowcount != 1:
                    raise OwnerBootstrapJournalConflict()
                updated = connection.execute(
                    "SELECT * FROM owner_bootstrap_heads WHERE owner_bootstrap_id=?",
                    (command.owner_bootstrap_id,),
                ).fetchone()
                result = self._result(updated, "completed")
                self._receipt(
                    connection,
                    command=command,
                    action="bootstrap.complete",
                    result=result,
                    now=now,
                )
                self._fault("before_bootstrap.complete_commit")
                return result
        except OwnerBootstrapJournalUnavailable:
            raise
        except Exception as error:
            raise OwnerBootstrapJournalUnavailable() from error

    def _advance(
        self,
        command: MarkOwnerBootstrapKeyStored | MarkOwnerBootstrapRecoveryLinked,
        *,
        action: Literal["key.store", "recovery.link"],
    ) -> OwnerBootstrapJournalResult:
        expected_type = (
            MarkOwnerBootstrapKeyStored
            if action == "key.store"
            else MarkOwnerBootstrapRecoveryLinked
        )
        if type(command) is not expected_type:
            raise OwnerBootstrapJournalUnavailable()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._validate(connection)
                replay = self._replay(connection, command, action)
                if replay is not None:
                    return replay
                now = self._now()
                head = connection.execute(
                    "SELECT * FROM owner_bootstrap_heads WHERE owner_bootstrap_id=?",
                    (command.owner_bootstrap_id,),
                ).fetchone()
                if head is None:
                    raise OwnerBootstrapJournalConflict()
                if action == "key.store" and head["state"] == "key_stored":
                    existing = cast(MarkOwnerBootstrapKeyStored, command)
                    if (
                        existing.expected_revision != 1
                        or head["bundle_revision"] != existing.bundle_revision
                        or head["bundle_public_digest"] != existing.bundle_public_digest
                    ):
                        raise OwnerBootstrapJournalConflict()
                    result = self._result(head, "joined")
                    self._receipt(
                        connection,
                        command=command,
                        action=action,
                        result=result,
                        now=now,
                    )
                    return result
                if action == "recovery.link" and head["state"] == "recovery_linked":
                    existing_link = cast(MarkOwnerBootstrapRecoveryLinked, command)
                    if (
                        existing_link.expected_revision != 2
                        or head["recovery_profile_id"] != existing_link.recovery_profile_id
                        or head["recovery_resource_digest"]
                        != existing_link.recovery_resource_digest
                    ):
                        raise OwnerBootstrapJournalConflict()
                    result = self._result(head, "joined")
                    self._receipt(
                        connection,
                        command=command,
                        action=action,
                        result=result,
                        now=now,
                    )
                    return result
                if head["revision"] != command.expected_revision:
                    raise OwnerBootstrapJournalConflict()
                if action == "key.store":
                    typed = cast(MarkOwnerBootstrapKeyStored, command)
                    if head["state"] != "prepared":
                        raise OwnerBootstrapJournalConflict()
                    connection.execute(
                        "UPDATE owner_bootstrap_heads SET state='key_stored',revision=2,"
                        "bundle_revision=?,bundle_public_digest=?,updated_at=? "
                        "WHERE owner_bootstrap_id=? AND state='prepared' AND revision=1",
                        (
                            typed.bundle_revision,
                            typed.bundle_public_digest,
                            _instant(now),
                            typed.owner_bootstrap_id,
                        ),
                    )
                    kind = "key_stored"
                else:
                    typed_link = cast(MarkOwnerBootstrapRecoveryLinked, command)
                    if head["state"] != "key_stored":
                        raise OwnerBootstrapJournalConflict()
                    connection.execute(
                        "UPDATE owner_bootstrap_heads SET state='recovery_linked',revision=3,"
                        "recovery_profile_id=?,recovery_resource_digest=?,updated_at=? "
                        "WHERE owner_bootstrap_id=? AND state='key_stored' AND revision=2",
                        (
                            typed_link.recovery_profile_id,
                            typed_link.recovery_resource_digest,
                            _instant(now),
                            typed_link.owner_bootstrap_id,
                        ),
                    )
                    kind = "recovery_linked"
                if connection.total_changes != 1:
                    raise OwnerBootstrapJournalConflict()
                updated = connection.execute(
                    "SELECT * FROM owner_bootstrap_heads WHERE owner_bootstrap_id=?",
                    (command.owner_bootstrap_id,),
                ).fetchone()
                result = self._result(updated, kind)
                self._receipt(
                    connection,
                    command=command,
                    action=action,
                    result=result,
                    now=now,
                )
                self._fault(f"before_{action}_commit")
                return result
        except OwnerBootstrapJournalUnavailable:
            raise
        except Exception as error:
            raise OwnerBootstrapJournalUnavailable() from error


__all__ = [
    "CompleteOwnerBootstrap",
    "MarkOwnerBootstrapKeyStored",
    "MarkOwnerBootstrapRecoveryLinked",
    "OwnerBootstrapJournal",
    "OwnerBootstrapJournalConflict",
    "OwnerBootstrapJournalResult",
    "OwnerBootstrapJournalUnavailable",
    "PrepareOwnerBootstrapAttempt",
    "ProductionOwnerBootstrapExecutionFence",
    "derive_owner_bootstrap_id",
]
