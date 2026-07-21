"""P17.9의 credential 수직 슬라이스: SQLite conformance용 durable registry.

이 모듈은 legacy TokenStore의 확장이 아니다. SQLite는 단일 프로세스의 결정론적
adapter이며, PostgreSQL/다중 인스턴스/물리적 exactly-once를 주장하지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import secrets
import sqlite3
from threading import RLock
from typing import Literal, cast, final

from agent_org_network.central_authority import AuthenticatedPrincipal, ResourceRef

CredentialStatus = Literal["active", "revoked"]
CredentialAction = Literal[
    "worker_credential.issue", "worker_credential.read", "worker_credential.revoke"
]


class CredentialError(RuntimeError):
    """비밀·내부 DB 오류를 노출하지 않는 credential 경계 오류."""


class CredentialDeniedError(CredentialError):
    pass


class CredentialConflictError(CredentialError):
    pass


class CredentialUnavailableError(CredentialError):
    pass


@dataclass(frozen=True)
class CredentialCommand:
    """조직 안에서 유일한 command receipt key. attempt는 1 이상이어야 한다."""

    org_id: str
    request_id: str
    attempt: int

    def __post_init__(self) -> None:
        if not self.org_id.strip() or not self.request_id.strip() or self.attempt < 1:
            raise ValueError("CredentialCommand가 올바르지 않습니다.")


@dataclass(frozen=True)
class DurableCredential:
    credential_id: str
    org_id: str
    owner_subject_id: str
    role: str
    generation: int
    revision: int
    status: CredentialStatus
    issued_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class CredentialApprovalEvidence:
    """이미 검증된 사람 승인 증거의 secret-free snapshot."""

    evidence_id: str
    action: CredentialAction
    command_digest: str
    resource_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.evidence_id.strip()
            or len(self.command_digest) != 64
            or len(self.resource_fingerprint) != 64
        ):
            raise ValueError("Credential approval evidence가 올바르지 않습니다.")


@dataclass(frozen=True)
class IssuedCredential:
    credential: DurableCredential
    # 첫 issue 호출에만 값이 있다. receipt replay는 None을 돌려 비밀을 재전달하지 않는다.
    raw_secret: str | None
    # 보안 전달 port가 만든 opaque 참조다. 평문과 달리 receipt/outbox에만 남길 수 있다.
    delivery_ref: str | None


def _canonical_hash(value: object) -> str:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise CredentialDeniedError() from error
    return sha256(raw.encode("utf-8")).hexdigest()


def resource_fingerprint(resource: ResourceRef) -> str:
    return _canonical_hash(
        {
            "org_id": resource.org_id,
            "kind": resource.kind,
            "resource_id": resource.resource_id,
            "owner_subject_id": resource.owner_subject_id,
        }
    )


def canonical_credential_command_digest(
    *, action: CredentialAction, resource: ResourceRef, command: object
) -> str:
    """승인 증거와 receipt가 공유하는 결정론 command digest."""
    return _canonical_hash(
        {
            "action": action,
            "resource_fingerprint": resource_fingerprint(resource),
            "command": command,
        }
    )


class SqliteCredentialUnitOfWork:
    """SQLite 단일 인스턴스 conformance UoW.

    schema는 credential state와 receipt/audit/outbox intent를 같은 transaction에 둔다.
    secret_hash만 보관하며 raw secret column은 존재하지 않는다.
    """

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS durable_credentials (
              credential_id TEXT NOT NULL, org_id TEXT NOT NULL, owner_subject_id TEXT NOT NULL,
              role TEXT NOT NULL, generation INTEGER NOT NULL, revision INTEGER NOT NULL,
              status TEXT NOT NULL, secret_hash TEXT NOT NULL, issued_at TEXT NOT NULL,
              expires_at TEXT, revoked_at TEXT,
              PRIMARY KEY (org_id, credential_id),
              CHECK (generation >= 1), CHECK (revision >= 1), CHECK (status IN ('active','revoked'))
            );
            CREATE TABLE IF NOT EXISTS credential_command_receipts (
              org_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt INTEGER NOT NULL,
              command_digest TEXT NOT NULL, credential_id TEXT NOT NULL, result_revision INTEGER NOT NULL,
              result_json TEXT NOT NULL, delivery_ref TEXT,
              PRIMARY KEY (org_id, request_id, attempt)
            );
            CREATE TABLE IF NOT EXISTS credential_audit_intents (
              id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, action TEXT NOT NULL,
              credential_id TEXT NOT NULL, principal_subject_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
              detail_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS credential_outbox_intents (
              id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, kind TEXT NOT NULL,
              credential_id TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            """
        )
        # P17.9 이전의 개발 DB가 있더라도 replay를 현재 행으로 바꾸지 않는다.
        # 기존 receipt에는 immutable 결과가 없으므로 안전하게 unavailable로 수렴한다.
        columns = {
            cast(str, row["name"])
            for row in self._connection.execute("PRAGMA table_info(credential_command_receipts)")
        }
        if "result_json" not in columns:
            self._connection.execute(
                "ALTER TABLE credential_command_receipts ADD COLUMN result_json TEXT NOT NULL DEFAULT ''"
            )
        if "delivery_ref" not in columns:
            self._connection.execute(
                "ALTER TABLE credential_command_receipts ADD COLUMN delivery_ref TEXT"
            )

    def transaction(self):  # type: ignore[no-untyped-def]
        return _SqliteCredentialTransaction(self._connection, self._lock)

    def close(self) -> None:
        self._connection.close()

    def debug_serialized_database(self) -> str:
        """Test-only inspection; schema itself proves raw secret persistence is impossible."""
        with self._lock:
            rows: list[dict[str, object]] = []
            for table in (
                "durable_credentials",
                "credential_command_receipts",
                "credential_audit_intents",
                "credential_outbox_intents",
            ):
                for row in self._connection.execute(f"SELECT * FROM {table}"):
                    rows.append({key: row[key] for key in row.keys()})
            return json.dumps(rows, sort_keys=True, default=str)


class _SqliteCredentialTransaction:
    def __init__(self, connection: sqlite3.Connection, lock: RLock) -> None:
        self._connection = connection
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            return self._connection
        except Exception:
            self._lock.release()
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self._connection.execute("ROLLBACK" if exc_type is not None else "COMMIT")
        finally:
            self._lock.release()


class DurableCredentialRegistry:
    """조직-scoped credential state + approval/audit/outbox UoW capability."""

    def __init__(
        self,
        uow: SqliteCredentialUnitOfWork,
        *,
        secret_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if type(uow) is not SqliteCredentialUnitOfWork:
            raise TypeError("SQLite credential UoW의 exact composition이 필요합니다.")
        self._uow = uow
        self._secret_factory = secret_factory
        self._clock = clock

    @staticmethod
    def _require_principal(principal: AuthenticatedPrincipal, org_id: str) -> None:
        if type(principal) is not AuthenticatedPrincipal or principal.org_id != org_id:
            raise CredentialDeniedError()

    @staticmethod
    def _require_resource(
        resource: ResourceRef, *, org_id: str, credential_id: str, owner: str
    ) -> None:
        if (
            type(resource) is not ResourceRef
            or resource.org_id != org_id
            or resource.kind != "worker_credential"
            or resource.resource_id != credential_id
            or resource.owner_subject_id != owner
        ):
            raise CredentialDeniedError()

    @staticmethod
    def _require_approval(
        approval: CredentialApprovalEvidence,
        *,
        action: CredentialAction,
        resource: ResourceRef,
        command_digest: str,
    ) -> None:
        if (
            type(approval) is not CredentialApprovalEvidence
            or approval.action != action
            or approval.command_digest != command_digest
            or approval.resource_fingerprint != resource_fingerprint(resource)
        ):
            raise CredentialDeniedError()

    @staticmethod
    def _dt(raw: str | None) -> datetime | None:
        return None if raw is None else datetime.fromisoformat(raw).astimezone(UTC)

    @classmethod
    def _credential(cls, row: sqlite3.Row) -> DurableCredential:
        return DurableCredential(
            credential_id=cast(str, row["credential_id"]),
            org_id=cast(str, row["org_id"]),
            owner_subject_id=cast(str, row["owner_subject_id"]),
            role=cast(str, row["role"]),
            generation=cast(int, row["generation"]),
            revision=cast(int, row["revision"]),
            status=cast(CredentialStatus, row["status"]),
            issued_at=cast(datetime, cls._dt(row["issued_at"])),
            expires_at=cls._dt(row["expires_at"]),
            revoked_at=cls._dt(row["revoked_at"]),
        )

    @staticmethod
    def _credential_snapshot(credential: DurableCredential) -> str:
        """Receipt가 재실행에 돌려줄 immutable public result다. secret은 넣지 않는다."""
        return json.dumps(
            {
                "credential_id": credential.credential_id,
                "org_id": credential.org_id,
                "owner_subject_id": credential.owner_subject_id,
                "role": credential.role,
                "generation": credential.generation,
                "revision": credential.revision,
                "status": credential.status,
                "issued_at": credential.issued_at.isoformat(),
                "expires_at": None
                if credential.expires_at is None
                else credential.expires_at.isoformat(),
                "revoked_at": None
                if credential.revoked_at is None
                else credential.revoked_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _receipt_credential(
        cls, receipt: sqlite3.Row, *, org_id: str, credential_id: str
    ) -> DurableCredential:
        """Receipt snapshot은 현재 state를 읽어 대체하지 않고 exact 결과만 복원한다."""
        try:
            decoded = json.loads(cast(str, receipt["result_json"]))
            if not isinstance(decoded, dict):
                raise ValueError
            payload = cast(dict[str, object], decoded)
            if set(payload) != {
                "credential_id",
                "org_id",
                "owner_subject_id",
                "role",
                "generation",
                "revision",
                "status",
                "issued_at",
                "expires_at",
                "revoked_at",
            }:
                raise ValueError
            string_fields = ("credential_id", "org_id", "owner_subject_id", "role", "issued_at")
            if any(type(payload[field]) is not str for field in string_fields):
                raise ValueError
            if type(payload["generation"]) is not int or type(payload["revision"]) is not int:
                raise ValueError
            if payload["expires_at"] is not None and type(payload["expires_at"]) is not str:
                raise ValueError
            if payload["revoked_at"] is not None and type(payload["revoked_at"]) is not str:
                raise ValueError
            status = payload["status"]
            if status not in ("active", "revoked"):
                raise ValueError
            issued_at = cls._dt(cast(str, payload["issued_at"]))
            if issued_at is None:
                raise ValueError
            result = DurableCredential(
                credential_id=cast(str, payload["credential_id"]),
                org_id=cast(str, payload["org_id"]),
                owner_subject_id=cast(str, payload["owner_subject_id"]),
                role=cast(str, payload["role"]),
                generation=payload["generation"],
                revision=payload["revision"],
                status=status,
                issued_at=issued_at,
                expires_at=cls._dt(payload["expires_at"]),
                revoked_at=cls._dt(payload["revoked_at"]),
            )
            if (
                result.credential_id != credential_id
                or result.org_id != org_id
                or result.revision != receipt["result_revision"]
                or not result.owner_subject_id
                or not result.role
                or result.generation < 1
                or result.revision < 1
            ):
                raise ValueError
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise CredentialUnavailableError() from error

    def _row(self, db: sqlite3.Connection, org_id: str, credential_id: str) -> sqlite3.Row | None:
        return db.execute(
            "SELECT * FROM durable_credentials WHERE org_id=? AND credential_id=?",
            (org_id, credential_id),
        ).fetchone()

    @staticmethod
    def _receipt(db: sqlite3.Connection, command: CredentialCommand) -> sqlite3.Row | None:
        return db.execute(
            "SELECT * FROM credential_command_receipts WHERE org_id=? AND request_id=? AND attempt=?",
            (command.org_id, command.request_id, command.attempt),
        ).fetchone()

    @staticmethod
    def _append_intents(
        db: sqlite3.Connection,
        *,
        action: CredentialAction,
        credential: DurableCredential,
        principal: AuthenticatedPrincipal,
        approval: CredentialApprovalEvidence,
        outbox_kind: str,
        delivery_ref: str | None = None,
    ) -> None:
        audit = {
            "org_id": credential.org_id,
            "action": action,
            "credential_id": credential.credential_id,
            "principal_subject_id": principal.subject_id,
            "evidence_id": approval.evidence_id,
        }
        db.execute(
            "INSERT INTO credential_audit_intents (org_id,action,credential_id,principal_subject_id,evidence_id,detail_json) VALUES (?,?,?,?,?,?)",
            (
                credential.org_id,
                action,
                credential.credential_id,
                principal.subject_id,
                approval.evidence_id,
                json.dumps(audit, sort_keys=True),
            ),
        )
        payload = {
            "org_id": credential.org_id,
            "credential_id": credential.credential_id,
            "generation": credential.generation,
            "revision": credential.revision,
        }
        if delivery_ref is not None:
            payload["delivery_ref"] = delivery_ref
        db.execute(
            "INSERT INTO credential_outbox_intents (org_id,kind,credential_id,payload_json) VALUES (?,?,?,?)",
            (
                credential.org_id,
                outbox_kind,
                credential.credential_id,
                json.dumps(payload, sort_keys=True),
            ),
        )

    def new_raw_secret(self) -> str:
        """MCP adapter가 stage 전에 쓰는 휘발성 secret을 만든다.

        이 값은 registry transaction에 들어가기 전 보안 전달 port로만 넘겨야 한다.
        """
        try:
            raw_secret = self._secret_factory()
        except (ValueError, OSError) as error:
            raise CredentialUnavailableError() from error
        if type(raw_secret) is not str or not raw_secret:
            raise CredentialUnavailableError()
        return raw_secret

    def find_issue_receipt(
        self,
        *,
        command: CredentialCommand,
        credential_id: str,
        command_digest: str,
    ) -> IssuedCredential | None:
        """동일 command의 immutable receipt만 조회한다. 새 state를 만들지 않는다."""
        try:
            with self._uow.transaction() as db:
                receipt = self._receipt(db, command)
                if receipt is None:
                    return None
                if (
                    receipt["command_digest"] != command_digest
                    or receipt["credential_id"] != credential_id
                ):
                    raise CredentialConflictError()
                ref = receipt["delivery_ref"]
                if ref is not None and (type(ref) is not str or not ref.strip()):
                    raise CredentialUnavailableError()
                return IssuedCredential(
                    self._receipt_credential(
                        receipt, org_id=command.org_id, credential_id=credential_id
                    ),
                    None,
                    ref,
                )
        except CredentialError:
            raise
        except sqlite3.Error as error:
            raise CredentialUnavailableError() from error

    def issue(
        self,
        *,
        principal: AuthenticatedPrincipal,
        credential_id: str,
        owner_subject_id: str,
        role: str,
        expires_at: datetime | None,
        command: CredentialCommand,
        resource: ResourceRef,
        approval: CredentialApprovalEvidence,
        raw_secret: str | None = None,
        delivery_ref: str | None = None,
    ) -> IssuedCredential:
        if (
            not credential_id.strip()
            or not owner_subject_id.strip()
            or not role.strip()
            or command.org_id != resource.org_id
        ):
            raise CredentialDeniedError()
        self._require_principal(principal, command.org_id)
        self._require_resource(
            resource, org_id=command.org_id, credential_id=credential_id, owner=owner_subject_id
        )
        payload = {
            "owner_subject_id": owner_subject_id,
            "role": role,
            "expires_at": None if expires_at is None else expires_at.astimezone(UTC).isoformat(),
        }
        digest = canonical_credential_command_digest(
            action="worker_credential.issue", resource=resource, command=payload
        )
        self._require_approval(
            approval, action="worker_credential.issue", resource=resource, command_digest=digest
        )
        try:
            with self._uow.transaction() as db:
                receipt = self._receipt(db, command)
                if receipt is not None:
                    if (
                        receipt["command_digest"] != digest
                        or receipt["credential_id"] != credential_id
                    ):
                        raise CredentialConflictError()
                    return IssuedCredential(
                        self._receipt_credential(
                            receipt, org_id=command.org_id, credential_id=credential_id
                        ),
                        None,
                        cast(str | None, receipt["delivery_ref"]),
                    )
                if self._row(db, command.org_id, credential_id) is not None:
                    raise CredentialConflictError()
                if raw_secret is None:
                    if delivery_ref is not None:
                        raise CredentialDeniedError()
                    raw_secret = self.new_raw_secret()
                elif (
                    type(raw_secret) is not str
                    or not raw_secret
                    or type(delivery_ref) is not str
                    or not delivery_ref.strip()
                ):
                    raise CredentialDeniedError()
                now = self._clock().astimezone(UTC)
                db.execute(
                    "INSERT INTO durable_credentials VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        credential_id,
                        command.org_id,
                        owner_subject_id,
                        role,
                        1,
                        1,
                        "active",
                        sha256(raw_secret.encode()).hexdigest(),
                        now.isoformat(),
                        None if expires_at is None else expires_at.astimezone(UTC).isoformat(),
                        None,
                    ),
                )
                credential = self._credential(self._row(db, command.org_id, credential_id))  # type: ignore[arg-type]
                db.execute(
                    "INSERT INTO credential_command_receipts (org_id,request_id,attempt,command_digest,credential_id,result_revision,result_json,delivery_ref) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        command.org_id,
                        command.request_id,
                        command.attempt,
                        digest,
                        credential_id,
                        credential.revision,
                        self._credential_snapshot(credential),
                        delivery_ref,
                    ),
                )
                self._append_intents(
                    db,
                    action="worker_credential.issue",
                    credential=credential,
                    principal=principal,
                    approval=approval,
                    outbox_kind="worker_credential.issued",
                    delivery_ref=delivery_ref,
                )
                return IssuedCredential(credential, raw_secret, delivery_ref)
        except CredentialError:
            raise
        except (sqlite3.Error, ValueError, OSError) as error:
            raise CredentialUnavailableError() from error

    def revoke(
        self,
        *,
        principal: AuthenticatedPrincipal,
        credential_id: str,
        expected_generation: int,
        expected_revision: int,
        command: CredentialCommand,
        resource: ResourceRef,
        approval: CredentialApprovalEvidence,
    ) -> DurableCredential:
        self._require_principal(principal, command.org_id)
        self._require_resource(
            resource,
            org_id=command.org_id,
            credential_id=credential_id,
            owner=resource.owner_subject_id or "",
        )
        payload = {
            "expected_generation": expected_generation,
            "expected_revision": expected_revision,
        }
        digest = canonical_credential_command_digest(
            action="worker_credential.revoke", resource=resource, command=payload
        )
        self._require_approval(
            approval, action="worker_credential.revoke", resource=resource, command_digest=digest
        )
        try:
            with self._uow.transaction() as db:
                receipt = self._receipt(db, command)
                if receipt is not None:
                    if (
                        receipt["command_digest"] != digest
                        or receipt["credential_id"] != credential_id
                    ):
                        raise CredentialConflictError()
                    return self._receipt_credential(
                        receipt, org_id=command.org_id, credential_id=credential_id
                    )
                row = self._row(db, command.org_id, credential_id)
                if row is None:
                    raise CredentialDeniedError()
                current = self._credential(row)
                # DB에서 방금 읽은 owner로 다시 current ResourceRef를 확인한다.
                self._require_resource(
                    resource,
                    org_id=command.org_id,
                    credential_id=credential_id,
                    owner=current.owner_subject_id,
                )
                if (
                    current.generation != expected_generation
                    or current.revision != expected_revision
                    or current.status != "active"
                ):
                    raise CredentialConflictError()
                now = self._clock().astimezone(UTC)
                changed = db.execute(
                    "UPDATE durable_credentials SET status='revoked',revision=revision+1,revoked_at=? WHERE org_id=? AND credential_id=? AND generation=? AND revision=? AND status='active'",
                    (
                        now.isoformat(),
                        command.org_id,
                        credential_id,
                        expected_generation,
                        expected_revision,
                    ),
                ).rowcount
                if changed != 1:
                    raise CredentialConflictError()
                credential = self._credential(self._row(db, command.org_id, credential_id))  # type: ignore[arg-type]
                db.execute(
                    "INSERT INTO credential_command_receipts (org_id,request_id,attempt,command_digest,credential_id,result_revision,result_json,delivery_ref) VALUES (?,?,?,?,?,?,?,NULL)",
                    (
                        command.org_id,
                        command.request_id,
                        command.attempt,
                        digest,
                        credential_id,
                        credential.revision,
                        self._credential_snapshot(credential),
                    ),
                )
                self._append_intents(
                    db,
                    action="worker_credential.revoke",
                    credential=credential,
                    principal=principal,
                    approval=approval,
                    outbox_kind="worker_credential.revoked",
                )
                return credential
        except CredentialError:
            raise
        except (sqlite3.Error, ValueError, OSError) as error:
            raise CredentialUnavailableError() from error

    def read(self, *, org_id: str, credential_id: str) -> DurableCredential | None:
        try:
            with self._uow.transaction() as db:
                row = self._row(db, org_id, credential_id)
                return None if row is None else self._credential(row)
        except sqlite3.Error as error:
            raise CredentialUnavailableError() from error

    def list(
        self, *, org_id: str, owner_subject_id: str | None = None
    ) -> tuple[DurableCredential, ...]:
        try:
            with self._uow.transaction() as db:
                if owner_subject_id is None:
                    rows = db.execute(
                        "SELECT * FROM durable_credentials WHERE org_id=? ORDER BY credential_id",
                        (org_id,),
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT * FROM durable_credentials WHERE org_id=? AND owner_subject_id=? ORDER BY credential_id",
                        (org_id, owner_subject_id),
                    ).fetchall()
                return tuple(self._credential(row) for row in rows)
        except sqlite3.Error as error:
            raise CredentialUnavailableError() from error

    def audit_intents(self, *, org_id: str) -> tuple[dict[str, str], ...]:
        """운영 read model도 조직 경계를 명시한 caller만 읽는다."""
        with self._uow.transaction() as db:
            return tuple(
                cast(dict[str, str], json.loads(row["detail_json"]))
                for row in db.execute(
                    "SELECT detail_json FROM credential_audit_intents WHERE org_id=? ORDER BY id",
                    (org_id,),
                )
            )

    def outbox_intents(self, *, org_id: str) -> tuple[dict[str, object], ...]:
        """전달 대상도 조직을 넘겨 나열하지 않는다."""
        with self._uow.transaction() as db:
            return tuple(
                {"kind": row["kind"], **cast(dict[str, object], json.loads(row["payload_json"]))}
                for row in db.execute(
                    "SELECT kind,payload_json FROM credential_outbox_intents WHERE org_id=? ORDER BY id",
                    (org_id,),
                )
            )

    def debug_serialized_database(self) -> str:
        return self._uow.debug_serialized_database()

    @property
    def credential_storage_capability(self) -> Literal["sqlite_credential_uow_v1"]:
        """후속 composition gate가 읽는 제한된 adapter capability marker."""
        return "sqlite_credential_uow_v1"


@final
class DurableCredentialCapability:
    """credential MCP 조립 전용 single-use proof.

    이 capability는 registry와 SQLite UoW가 exact concrete 객체로 함께 조립됐다는
    trusted-process proof다. MCP를 등록하지 않으며 PostgreSQL/다중 인스턴스 보증도
    하지 않는다. 후속 adapter는 이 capability를 claim한 뒤에만 registry를 노출한다.
    """

    def __init__(self, registry: DurableCredentialRegistry) -> None:
        if (
            type(registry) is not DurableCredentialRegistry
            or registry.credential_storage_capability != "sqlite_credential_uow_v1"
        ):
            raise TypeError("durable credential registry/UoW exact composition이 필요합니다.")
        self._registry = registry
        self._state: Literal["issued", "claimed", "revoked"] = "issued"

    def claim(self, registry: object) -> bool:
        if self._state != "issued":
            return False
        if type(registry) is not DurableCredentialRegistry or registry is not self._registry:
            self._state = "revoked"
            return False
        self._state = "claimed"
        return True
