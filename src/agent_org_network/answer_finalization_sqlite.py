"""ADR 0044 SQLite Question Completion 원자 Unit of Work.

이 객체 하나가 QuestionRequestStore, QuestionCompletionUnitOfWork,
QuestionCompletionReader를 함께 구현한다. runtime 생성자는 schema를 변경하지 않고
``question_completion`` capability를 검증한 기존 파일만 연다.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import RLock, local
from typing import Literal, NoReturn, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from agent_org_network.answer_finalization import (
    AnswerCompletion,
    ApprovalCompletionReader,
    AnswerFinalizationError,
    AnswerResponsibilitySnapshot,
    ApprovalEvidence,
    CompletionBundle,
    CompletionConcurrencyError,
    CompletionEvidenceError,
    CompletionHandoff,
    CompletionIdCollisionError,
    CompletionNotFoundError,
    DeliveryOutboxEntry,
    DirectAnsweredTransitionError,
    HumanApprovalEvidence,
    IncompleteCompletionStateError,
    NoApprovalEvidence,
    QuestionCompletionPlanner,
    ReentrantCompletionMutationError,
    ResponsibilitySnapshotResolver,
    TerminalAnswerAudit,
    canonical_completion_bundle,
    canonical_completion_handoff,
)
from agent_org_network.answer_record import AnswerRecord
from agent_org_network.approval import (
    ApprovalPolicy,
    ApprovalStore,
    ApprovedCandidate,
    AnswerCandidate,
    FinalizationCandidate,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    DuplicateQuestionRequestError,
    QuestionRequest,
    RouteTarget,
    validate_compare_and_set_semantics,
    validate_new_question_request_semantics,
)
from agent_org_network.runtime import AnswerMode
from agent_org_network.session import SessionTurn
from agent_org_network.sqlite_completion import open_sqlite_completion_connection
from agent_org_network.sqlite_stores import (
    CorruptQuestionRequestError,
    _compare_and_set_question_request_no_commit,  # pyright: ignore[reportPrivateUsage]
    _insert_question_request_no_commit,  # pyright: ignore[reportPrivateUsage]
    _select_question_request_no_commit,  # pyright: ignore[reportPrivateUsage]
)


SqliteCompletionFaultPoint: TypeAlias = Literal[
    "after_answer_record",
    "after_request",
    "after_audit",
    "after_session",
    "after_outbox",
    "after_completion_receipt",
    "before_commit",
]
SqliteCompletionFaultInjector: TypeAlias = Callable[[SqliteCompletionFaultPoint], None]

_HANDOFF_SCHEMA_VERSION = 2
_AUDIT_SCHEMA_VERSION = 1
_APPROVAL_ADAPTER: TypeAdapter[ApprovalEvidence] = TypeAdapter(ApprovalEvidence)
_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UNAVAILABLE_SQLITE_CODES = frozenset(
    {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_PROTOCOL,
        sqlite3.SQLITE_INTERRUPT,
    }
)


class SqliteCompletionStorageUnavailableError(AnswerFinalizationError):
    """SQLite lock timeout·I/O failure처럼 재시도 가능한 저장소 실패."""


class CorruptSqliteCompletionError(IncompleteCompletionStateError):
    """저장된 SQLite completion 행이 strict artifact 계약을 위반함."""


class _LegacyV1ApprovedCandidate(BaseModel):
    """v1 terminal receipt 검증 전용. 새 Finalization 권한으로 사용할 수 없다."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    request_id: str
    item_id: str
    expected_revision: int = Field(ge=0)
    attempt: int = Field(ge=1)
    route: RouteTarget
    candidate: AnswerCandidate
    approved_by: str
    approved_at: datetime
    edited: bool
    policy_version: str

    @field_validator("*", mode="after")
    @classmethod
    def _strings_must_be_nonblank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("문자열 값은 비어 있거나 공백일 수 없습니다.")
        return value

    @field_validator("approved_at", mode="after")
    @classmethod
    def _approved_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("legacy ApprovedCandidate.approved_at은 timezone-aware여야 합니다.")
        return value


_StoredHandoff: TypeAlias = FinalizationCandidate | ApprovedCandidate | _LegacyV1ApprovedCandidate


def _canonical_json(payload: object) -> str:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise CorruptSqliteCompletionError(
            "completion artifact를 canonical JSON으로 직렬화할 수 없습니다."
        ) from error


def _is_storage_unavailable(error: sqlite3.Error) -> bool:
    """extended result code의 primary byte로 transient/I/O 계열만 분류한다."""
    raw_code = getattr(error, "sqlite_errorcode", None)
    return isinstance(raw_code, int) and (raw_code & 0xFF) in _UNAVAILABLE_SQLITE_CODES


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CorruptSqliteCompletionError(f"completion JSON에 중복 key가 있습니다: {key!r}")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str) -> object:
    raise CorruptSqliteCompletionError(f"completion JSON에 비표준 상수가 있습니다: {value}")


def _strict_json_object(raw: object, *, field: str) -> dict[str, object]:
    if not isinstance(raw, str):
        raise CorruptSqliteCompletionError(f"{field}은 TEXT여야 합니다.")
    try:
        payload: object = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except CorruptSqliteCompletionError:
        raise
    except (json.JSONDecodeError, TypeError) as error:
        raise CorruptSqliteCompletionError(f"{field} JSON을 해석할 수 없습니다.") from error
    if not isinstance(payload, dict):
        raise CorruptSqliteCompletionError(f"{field} JSON은 object여야 합니다.")
    result = cast(dict[str, object], payload)
    if _canonical_json(result) != raw:
        raise CorruptSqliteCompletionError(f"{field}은 canonical JSON이어야 합니다.")
    return result


def _strict_json_array(raw: object, *, field: str) -> list[object]:
    if not isinstance(raw, str):
        raise CorruptSqliteCompletionError(f"{field}은 canonical JSON TEXT여야 합니다.")
    try:
        payload: object = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except CorruptSqliteCompletionError:
        raise
    except (json.JSONDecodeError, TypeError) as error:
        raise CorruptSqliteCompletionError(f"{field} JSON을 해석할 수 없습니다.") from error
    if not isinstance(payload, list):
        raise CorruptSqliteCompletionError(f"{field} JSON은 array여야 합니다.")
    result = cast(list[object], payload)
    if _canonical_json(result) != raw:
        raise CorruptSqliteCompletionError(f"{field}은 canonical JSON이어야 합니다.")
    return result


def _strict_text(row: sqlite3.Row, column: str, *, nonblank: bool = True) -> str:
    value = row[column]
    if not isinstance(value, str) or (nonblank and not value.strip()):
        raise CorruptSqliteCompletionError(
            f"completion row의 {column!r}은 "
            + ("nonblank " if nonblank else "")
            + "TEXT여야 합니다."
        )
    return value


def _optional_text(row: sqlite3.Row, column: str) -> str | None:
    value = row[column]
    if value is not None and not isinstance(value, str):
        raise CorruptSqliteCompletionError(
            f"completion row의 {column!r}은 TEXT 또는 NULL이어야 합니다."
        )
    return value


def _strict_int(row: sqlite3.Row, column: str) -> int:
    value = row[column]
    if type(value) is not int:
        raise CorruptSqliteCompletionError(f"completion row의 {column!r}은 INTEGER여야 합니다.")
    return value


def _parse_aware(raw: object, *, field: str) -> datetime:
    if not isinstance(raw, str):
        raise CorruptSqliteCompletionError(f"{field}은 timezone-aware ISO TEXT여야 합니다.")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as error:
        raise CorruptSqliteCompletionError(
            f"{field}은 timezone-aware ISO TEXT여야 합니다."
        ) from error
    if value.tzinfo is None or value.utcoffset() is None:
        raise CorruptSqliteCompletionError(f"{field}은 timezone-aware여야 합니다.")
    return value


def _model_json(model: BaseModel) -> str:
    payload = model.model_dump(mode="json", round_trip=True)
    return _canonical_json(payload)


def _handoff_kind(
    handoff: CompletionHandoff,
) -> Literal["finalization_candidate", "approved_candidate"]:
    if isinstance(handoff, FinalizationCandidate):
        return "finalization_candidate"
    return "approved_candidate"


def _handoff_json(handoff: CompletionHandoff) -> str:
    canonical = canonical_completion_handoff(handoff)
    return _stored_handoff_json(canonical)


def _stored_handoff_json(handoff: _StoredHandoff) -> str:
    payload = handoff.model_dump(mode="json", round_trip=True)
    kind = (
        "approved_candidate"
        if isinstance(handoff, _LegacyV1ApprovedCandidate)
        else _handoff_kind(handoff)
    )
    envelope = {"kind": kind, **payload}
    return _canonical_json(envelope)


def _decode_handoff(row: sqlite3.Row) -> _StoredHandoff:
    raw_kind = _strict_text(row, "handoff_kind")
    if raw_kind not in {"finalization_candidate", "approved_candidate"}:
        raise CorruptSqliteCompletionError("지원하지 않는 completion handoff kind입니다.")
    version = _strict_int(row, "handoff_schema_version")
    if version not in (1, _HANDOFF_SCHEMA_VERSION):
        raise CorruptSqliteCompletionError("지원하지 않는 completion handoff schema입니다.")
    raw_json = _strict_text(row, "handoff_json")
    payload = _strict_json_object(raw_json, field="handoff_json")
    discriminator = payload.pop("kind", None)
    if discriminator != raw_kind:
        raise CorruptSqliteCompletionError("handoff_kind와 handoff_json discriminator가 다릅니다.")
    body = _canonical_json(payload)
    try:
        if raw_kind == "finalization_candidate":
            decoded: object = FinalizationCandidate.model_validate_json(body, strict=True)
            handoff: _StoredHandoff = canonical_completion_handoff(decoded)
        elif version == 1:
            handoff = _LegacyV1ApprovedCandidate.model_validate_json(body, strict=True)
        else:
            decoded = ApprovedCandidate.model_validate_json(body, strict=True)
            handoff = canonical_completion_handoff(decoded)
    except Exception as error:
        raise CorruptSqliteCompletionError(
            "저장된 completion handoff가 도메인 계약과 다릅니다."
        ) from error
    if _stored_handoff_json(handoff) != raw_json:
        raise CorruptSqliteCompletionError("handoff_json canonical round-trip이 다릅니다.")
    raw_digest = _strict_text(row, "handoff_sha256")
    if _LOWER_SHA256.fullmatch(raw_digest) is None:
        raise CorruptSqliteCompletionError("handoff_sha256 형식이 올바르지 않습니다.")
    actual_digest = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
    if raw_digest != actual_digest:
        raise CorruptSqliteCompletionError("handoff_json digest가 일치하지 않습니다.")
    return handoff


def _canonical_request(raw: QuestionRequest) -> QuestionRequest:
    try:
        return QuestionRequest.model_validate(
            raw.model_dump(mode="python", round_trip=True),
            strict=True,
        )
    except Exception as error:
        raise CompletionEvidenceError(
            "Question Request aggregate canonical validation에 실패했습니다."
        ) from error


class CompletionTransactionContext:
    """현재 thread가 소유한 공유 durable write의 비위조 capability다.

    이 token은 ``SqliteCompletionTransaction``만 만들 수 있다. Completion의
    in-transaction seam은 raw ``connection.in_transaction``가 아니라 이 token을
    검증한다. 따라서 같은 connection을 우연히 열어 둔 호출자는 terminal write를
    끼워 넣을 수 없다.
    """

    __slots__ = ("_scope_token", "_thread_id", "_write_generation")

    def __init__(self, scope_token: object, thread_id: int, write_generation: int) -> None:
        self._scope_token = scope_token
        self._thread_id = thread_id
        self._write_generation = write_generation

    def matches(self, *, scope_token: object, thread_id: int, write_generation: int) -> bool:
        return (
            self._scope_token is scope_token
            and self._thread_id == thread_id
            and self._write_generation == write_generation
        )


class _SharedCompletionTransactionOwnership:
    """Completion public API와 durable seam이 함께 쓰는 thread-local ownership."""

    def __init__(self) -> None:
        self.state = local()


class SqliteCompletionTransaction:
    """동일 SQLite transaction을 공유하는 durable workflow용 좁은 public seam.

    새 transaction을 열거나 완료하지 않는다. 호출자는 ``BEGIN IMMEDIATE``와 commit/rollback을
    명시적으로 소유하고, 이 seam은 Question Request exact read/CAS와 component-owned SQL만
    같은 connection에서 실행하게 한다. 다른 durable component가 Completion UoW의 private
    connection이나 sqlite_stores helper를 직접 결합하지 않도록 둔 경계다.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        shared_lock: RLock,
        ownership: _SharedCompletionTransactionOwnership,
    ) -> None:
        self._connection = connection
        # A Completion UoW has one check_same_thread=False connection.  Durable
        # components must therefore participate in the same lock as every public
        # Completion read/write, rather than keeping a component-local lock.
        self._shared_lock = shared_lock
        self._ownership = ownership

    def _state(self) -> local:
        return self._ownership.state

    @contextmanager
    def scope(self) -> Generator[None]:
        """Serialise one durable component operation with all Completion APIs.

        The scope deliberately owns no transaction policy: callers still choose
        ``begin_immediate``/``commit``/``rollback``.  It does guarantee that a
        transaction cannot be observed or interleaved through this shared
        connection.  An accidentally leaked transaction is rolled back at the
        outermost scope boundary so a failed component cannot poison later work.
        """
        self._shared_lock.acquire()
        state = self._state()
        depth = getattr(state, "scope_depth", 0)
        if depth == 0 and self._connection.in_transaction:
            self._shared_lock.release()
            raise ReentrantCompletionMutationError(
                "새 durable transaction scope 전에 열린 SQLite transaction이 있습니다."
            )
        if depth == 0:
            state.scope_token = object()
            state.write_generation = getattr(state, "write_generation", 0) + 1
            state.write_depth = 0
        state.scope_depth = depth + 1
        try:
            yield
        finally:
            remaining = state.scope_depth - 1
            state.scope_depth = remaining
            if remaining == 0:
                try:
                    if self._connection.in_transaction:
                        self._connection.rollback()
                finally:
                    for name in ("scope_depth", "scope_token", "write_depth"):
                        if hasattr(state, name):
                            delattr(state, name)
                    self._shared_lock.release()
            else:
                self._shared_lock.release()

    def _require_scope(self) -> None:
        if getattr(self._state(), "scope_depth", 0) <= 0:
            raise ReentrantCompletionMutationError(
                "durable SQLite transaction은 shared transaction scope 안에서만 사용할 수 있습니다."
            )

    @contextmanager
    def read_scope(self) -> Generator[None]:
        """Run a serialised read snapshot, reusing a caller's write transaction.

        This is intentionally paired with ``scope()``.  A direct durable reader
        gets a stable snapshot; a reader invoked while a durable mutation is in
        progress simply uses that mutation's transaction and never opens a nested
        SQLite transaction.
        """
        self._require_scope()
        if getattr(self._state(), "write_depth", 0) > 0:
            raise ReentrantCompletionMutationError(
                "durable write 중 public read는 uncommitted state를 관찰할 수 없습니다."
            )
        owns_transaction = not self._connection.in_transaction
        try:
            if owns_transaction:
                self._connection.execute("BEGIN")
            yield
            if owns_transaction:
                self._connection.commit()
        except Exception:
            if owns_transaction and self._connection.in_transaction:
                self._connection.rollback()
            raise

    @property
    def in_transaction(self) -> bool:
        self._require_scope()
        return self._connection.in_transaction

    def begin_immediate(self) -> None:
        self._require_scope()
        if self._connection.in_transaction:
            raise ReentrantCompletionMutationError("이미 SQLite transaction이 열려 있습니다.")
        self._connection.execute("BEGIN IMMEDIATE")
        self._state().write_depth = 1

    def commit(self) -> None:
        self._require_scope()
        if not self._connection.in_transaction:
            raise ReentrantCompletionMutationError("commit할 SQLite transaction이 없습니다.")
        self._connection.commit()
        self._state().write_depth = 0

    def rollback(self) -> None:
        self._require_scope()
        if self._connection.in_transaction:
            self._connection.rollback()
        self._state().write_depth = 0

    def completion_context(self) -> CompletionTransactionContext:
        """같은 shared durable write에만 Completion terminal write를 위임한다."""
        self._require_scope()
        state = self._state()
        if not self._connection.in_transaction or getattr(state, "write_depth", 0) != 1:
            raise ReentrantCompletionMutationError(
                "Completion transaction context에는 현재 shared durable write가 필요합니다."
            )
        import threading

        return CompletionTransactionContext(
            state.scope_token,
            threading.get_ident(),
            state.write_generation,
        )

    def require_completion_context(self, context: CompletionTransactionContext) -> None:
        """공개 Completion seam의 token과 현재 thread/write ownership을 exact 검증한다."""
        self._require_scope()
        import threading

        state = self._state()
        if (
            type(context) is not CompletionTransactionContext
            or not self._connection.in_transaction
            or getattr(state, "write_depth", 0) != 1
            or not context.matches(
                scope_token=getattr(state, "scope_token", None),
                thread_id=threading.get_ident(),
                write_generation=getattr(state, "write_generation", -1),
            )
        ):
            raise ReentrantCompletionMutationError(
                "Completion in-transaction 확정에는 현재 shared durable transaction context가 필요합니다."
            )

    def validate_component(self, validator: Callable[[sqlite3.Connection], None]) -> None:
        """Runtime open 시 component schema를 read-only로 검증한다.

        validator는 DDL을 실행하거나 transaction을 시작해서는 안 된다. 이 method는
        connection identity를 노출하지 않고 shared database capability 검증만 허용한다.
        """
        self._require_scope()
        if self._connection.in_transaction:
            raise ReentrantCompletionMutationError(
                "schema 검증 중 열린 SQLite transaction이 있습니다."
            )
        validator(self._connection)

    def validate_component_in_transaction(
        self, validator: Callable[[sqlite3.Connection], None]
    ) -> None:
        """Validate a read-only durable capability inside the caller's write snapshot.

        Lifecycle command replay must not trust an earlier open-time capability
        check: a companion row can be corrupted between commands.  This retains
        the same typed scope/connection ownership without exposing the connection
        or allowing the validator to start/finish a transaction.
        """
        self._require_scope()
        if not self._connection.in_transaction:
            raise ReentrantCompletionMutationError(
                "in-transaction schema 검증에는 현재 shared durable write가 필요합니다."
            )
        validator(self._connection)

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        self._require_scope()
        if not self._connection.in_transaction:
            raise ReentrantCompletionMutationError("열린 SQLite transaction이 필요합니다.")
        return self._connection.execute(sql, parameters)

    def select_question_request(self, request_id: str) -> QuestionRequest | None:
        self._require_scope()
        if not self._connection.in_transaction:
            raise ReentrantCompletionMutationError("열린 SQLite transaction이 필요합니다.")
        request = _select_question_request_no_commit(self._connection, request_id)
        return None if request is None else _canonical_request(request)

    def compare_and_set_question_request(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        self._require_scope()
        if not self._connection.in_transaction:
            raise ReentrantCompletionMutationError("열린 SQLite transaction이 필요합니다.")
        canonical_current = _canonical_request(current)
        canonical_updated = _canonical_request(updated)
        validate_compare_and_set_semantics(
            request_id, expected_revision, canonical_current, canonical_updated
        )
        return _compare_and_set_question_request_no_commit(
            self._connection,
            request_id,
            expected_revision,
            canonical_current,
            canonical_updated,
        )


class SqliteQuestionCompletionUnitOfWork:
    """SQLite 파일 하나의 durable Request Store·Completion UoW·Reader."""

    workflow_durability: Literal["durable"] = "durable"
    question_completion_storage_capability: Literal["atomic_v1"] = "atomic_v1"

    def __init__(
        self,
        db_path: str | Path,
        *,
        policy: ApprovalPolicy | None,
        approvals: ApprovalStore | None,
        responsibility_resolver: ResponsibilitySnapshotResolver | None,
        record_id_factory: Callable[[], str],
        clock: Callable[[], datetime],
        fault_injector: SqliteCompletionFaultInjector | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._connection = open_sqlite_completion_connection(db_path, timeout=timeout)
        self._lock = RLock()
        self._transaction_ownership = _SharedCompletionTransactionOwnership()
        self._durable_transaction = SqliteCompletionTransaction(
            self._connection,
            shared_lock=self._lock,
            ownership=self._transaction_ownership,
        )
        self._completion_in_progress = False
        self._fault_injector = fault_injector
        try:
            self._planner = QuestionCompletionPlanner(
                policy=policy,
                approvals=approvals,
                responsibility_resolver=responsibility_resolver,
                record_id_factory=record_id_factory,
                clock=clock,
            )
        except Exception:
            self._connection.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def durable_transaction(self) -> SqliteCompletionTransaction:
        """연결을 노출하지 않는 동일-transaction durable component seam이다.

        반환된 seam의 ``scope()`` 안에서만 실행할 수 있다. 읽기는 ``read_scope()``와
        조합한다. 이 scope는 Completion UoW의 모든 public read/write와 같은 lock을
        공유한다. Completion UoW의
        ``complete_in_transaction``과 조합할 수 있지만, schema/runtime authority를
        우회하는 일반-purpose DB API는 아니다.
        """
        return self._durable_transaction

    def matches_question_completion_dependencies(
        self,
        *,
        policy: ApprovalPolicy,
        approvals: ApprovalStore,
        responsibility_resolver: ResponsibilitySnapshotResolver,
    ) -> bool:
        """ApprovalBoundary와 Finalization의 in-process 의존성 identity를 확인한다."""
        return self._planner.matches_dependencies(
            policy=policy,
            approvals=approvals,
            responsibility_resolver=responsibility_resolver,
        )

    def create(self, request: QuestionRequest) -> QuestionRequest:
        canonical = _canonical_request(request)
        validate_new_question_request_semantics(canonical)
        with self._lock:
            self._reject_reentrant_mutation()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                if (
                    _select_question_request_no_commit(self._connection, canonical.request_id)
                    is not None
                ):
                    raise DuplicateQuestionRequestError(
                        f"이미 존재하는 Question Request: {canonical.request_id!r}"
                    )
                _insert_question_request_no_commit(self._connection, canonical)
                self._connection.commit()
            except Exception as error:
                self._rollback()
                self._raise_write_error(error, operation="Question Request create")
        return _canonical_request(canonical)

    def get(self, request_id: str) -> QuestionRequest | None:
        with self._read_scope():
            request = _select_question_request_no_commit(self._connection, request_id)
            return None if request is None else _canonical_request(request)

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        canonical_current = _canonical_request(current)
        canonical_updated = _canonical_request(updated)
        validate_compare_and_set_semantics(
            request_id,
            expected_revision,
            canonical_current,
            canonical_updated,
        )
        if isinstance(canonical_updated.state, AnsweredRequest):
            raise DirectAnsweredTransitionError(
                "AnsweredRequest는 SqliteQuestionCompletionUnitOfWork만 확정할 수 있습니다."
            )
        with self._lock:
            self._reject_reentrant_mutation()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                changed = _compare_and_set_question_request_no_commit(
                    self._connection,
                    request_id,
                    expected_revision,
                    canonical_current,
                    canonical_updated,
                )
                self._connection.commit()
                return changed
            except Exception as error:
                self._rollback()
                self._raise_write_error(error, operation="Question Request CAS")

    def nonterminal(self) -> list[QuestionRequest]:
        with self._read_scope():
            rows = self._connection.execute("SELECT request_id FROM question_requests").fetchall()
            result: list[QuestionRequest] = []
            for row in rows:
                request_id = _strict_text(row, "request_id")
                request = _select_question_request_no_commit(self._connection, request_id)
                if request is None:
                    raise CorruptSqliteCompletionError(
                        "Question Request scan 중 행이 사라졌습니다."
                    )
                if not request.is_terminal:
                    result.append(_canonical_request(request))
            return sorted(result, key=lambda item: (item.created_at, item.request_id))

    def by_request(self, request_id: str) -> CompletionBundle | None:
        with self._read_scope():
            return self._bundle_for_request_no_transaction(request_id)

    def by_record(self, record_id: str) -> CompletionBundle | None:
        with self._read_scope():
            receipts = self._connection.execute(
                "SELECT * FROM question_completion_receipts WHERE record_id COLLATE BINARY = ?",
                (record_id,),
            ).fetchall()
            if not receipts:
                residual = sum(
                    self._count(
                        f"SELECT COUNT(*) FROM {table} WHERE record_id COLLATE BINARY = ?",
                        (record_id,),
                    )
                    for table in (
                        "terminal_answer_audits",
                        "request_session_turns",
                        "question_delivery_outbox",
                    )
                )
                if residual:
                    raise IncompleteCompletionStateError(
                        "receipt 없이 completion-native record artifact가 남았습니다."
                    )
                answer_rows = self._connection.execute(
                    "SELECT request_id, sources_json, snapshot_sha FROM answer_records "
                    "WHERE record_id COLLATE BINARY = ?",
                    (record_id,),
                ).fetchall()
                if len(answer_rows) > 1:
                    raise IncompleteCompletionStateError(
                        "receipt 없는 record ID에 AnswerRecord가 여러 건입니다."
                    )
                if answer_rows:
                    answer_row = answer_rows[0]
                    if answer_row["request_id"] is not None and (
                        answer_row["sources_json"] is not None
                        or answer_row["snapshot_sha"] is not None
                    ):
                        raise IncompleteCompletionStateError(
                            "receipt 없이 v2 request-aware AnswerRecord 흔적이 남았습니다."
                        )
                request_rows = self._connection.execute(
                    "SELECT request_id FROM question_requests ORDER BY request_id COLLATE BINARY"
                ).fetchall()
                for request_row in request_rows:
                    referenced_request_id = _strict_text(request_row, "request_id")
                    request = _select_question_request_no_commit(
                        self._connection,
                        referenced_request_id,
                    )
                    if request is None:
                        raise IncompleteCompletionStateError(
                            "Question Request scan 중 행이 사라졌습니다."
                        )
                    canonical = _canonical_request(request)
                    if (
                        isinstance(canonical.state, AnsweredRequest)
                        and canonical.state.record_id == record_id
                    ):
                        raise IncompleteCompletionStateError(
                            "AnsweredRequest가 없어진 AnswerRecord ID를 참조합니다."
                        )
                # receipt 없는 legacy AnswerRecord는 completion으로 승격하지 않는다.
                return None
            if len(receipts) != 1:
                raise IncompleteCompletionStateError(
                    "record ID에 completion receipt가 정확히 한 건이 아닙니다."
                )
            receipt = receipts[0]
            if _strict_text(receipt, "record_id") != record_id:
                raise CorruptSqliteCompletionError("receipt record lookup이 exact하지 않습니다.")
            request_id = _strict_text(receipt, "request_id")
            bundle = self._bundle_for_request_no_transaction(request_id)
            if bundle is None or bundle.completion.record_id != record_id:
                raise IncompleteCompletionStateError(
                    "receipt record ID와 복원된 CompletionBundle이 다릅니다."
                )
            return bundle

    def answer_record(self, record_id: str) -> AnswerRecord | None:
        """receipt-linked completion을 strict 복원한 다음 AnswerRecord만 투영한다."""
        bundle = self.by_record(record_id)
        return None if bundle is None else bundle.answer_record

    def answer_records_for_agent(self, agent_id: str) -> list[AnswerRecord]:
        """receipt 전체를 하나의 read snapshot에서 exact 복원해 카드별로 걸러낸다."""
        with self._read_scope():
            receipt_rows = self._connection.execute(
                "SELECT request_id, record_id FROM question_completion_receipts "
                "ORDER BY request_id COLLATE BINARY, record_id COLLATE BINARY"
            ).fetchall()

            def pairs(rows: list[sqlite3.Row], *, source: str) -> set[tuple[str, str]]:
                result = {
                    (_strict_text(row, "request_id"), _strict_text(row, "record_id"))
                    for row in rows
                }
                if len(result) != len(rows):
                    raise IncompleteCompletionStateError(
                        f"{source}의 request/record ID pair가 중복됐습니다."
                    )
                if len({request_id for request_id, _ in result}) != len(result) or len(
                    {record_id for _, record_id in result}
                ) != len(result):
                    raise IncompleteCompletionStateError(
                        f"{source}의 request/record ID가 일대일이 아닙니다."
                    )
                return result

            receipt_pairs = pairs(receipt_rows, source="completion receipt")
            answered_pairs: set[tuple[str, str]] = set()
            expected_turn_pairs: set[tuple[str, str]] = set()
            request_rows = self._connection.execute(
                "SELECT request_id FROM question_requests ORDER BY request_id COLLATE BINARY"
            ).fetchall()
            seen_request_ids: set[str] = set()
            for row in request_rows:
                request_id = _strict_text(row, "request_id")
                if request_id in seen_request_ids:
                    raise IncompleteCompletionStateError(
                        "Question Request scan의 request_id가 중복됐습니다."
                    )
                seen_request_ids.add(request_id)
                request = _select_question_request_no_commit(self._connection, request_id)
                if request is None:
                    raise IncompleteCompletionStateError(
                        "Question Request scan 중 행이 사라졌습니다."
                    )
                canonical = _canonical_request(request)
                if isinstance(canonical.state, AnsweredRequest):
                    pair = (request_id, canonical.state.record_id)
                    answered_pairs.add(pair)
                    if canonical.session_id is not None:
                        expected_turn_pairs.add(pair)

            audit_pairs = pairs(
                self._connection.execute(
                    "SELECT request_id, record_id FROM terminal_answer_audits"
                ).fetchall(),
                source="terminal audit",
            )
            outbox_pairs = pairs(
                self._connection.execute(
                    "SELECT request_id, record_id FROM question_delivery_outbox"
                ).fetchall(),
                source="delivery outbox",
            )
            turn_pairs = pairs(
                self._connection.execute(
                    "SELECT request_id, record_id FROM request_session_turns"
                ).fetchall(),
                source="request SessionTurn",
            )
            native_record_pairs = pairs(
                self._connection.execute(
                    "SELECT request_id, record_id FROM answer_records "
                    "WHERE sources_json IS NOT NULL OR snapshot_sha IS NOT NULL"
                ).fetchall(),
                source="completion-native AnswerRecord",
            )
            if not (
                receipt_pairs
                == answered_pairs
                == audit_pairs
                == outbox_pairs
                == native_record_pairs
                and turn_pairs == expected_turn_pairs
            ):
                raise IncompleteCompletionStateError(
                    "AnswerRecord 목록의 SQLite completion artifact 집합이 exact-link되지 않습니다."
                )

            records: list[AnswerRecord] = []
            for request_id, record_id in sorted(receipt_pairs):
                bundle = self._bundle_for_request_no_transaction(request_id)
                if bundle is None or bundle.answer_record.record_id != record_id:
                    raise IncompleteCompletionStateError(
                        "completion receipt와 AnswerRecord가 exact-link되지 않습니다."
                    )
                records.append(bundle.answer_record)
        return sorted(
            (record for record in records if record.agent_id == agent_id),
            key=lambda record: (record.answered_at, record.record_id),
        )

    def complete(self, handoff: object) -> AnswerCompletion:
        canonical = canonical_completion_handoff(handoff)
        with self._completion_scope():
            with self._durable_transaction.scope():
                try:
                    self._durable_transaction.begin_immediate()
                    completion = self.complete_in_transaction(
                        canonical,
                        transaction_context=self._durable_transaction.completion_context(),
                    )
                    self._durable_transaction.commit()
                    return completion
                except Exception as error:
                    self._durable_transaction.rollback()
                    self._raise_write_error(error, operation="Question completion")

    def complete_in_transaction(
        self,
        handoff: object,
        *,
        transaction_context: CompletionTransactionContext,
        approval_reader: ApprovalCompletionReader | None = None,
    ) -> AnswerCompletion:
        """이미 열린 동일 SQLite write transaction 안에서만 Completion을 확정한다.

        Durable Approval decision UoW가 결의 snapshot과 terminal artifact를 하나의
        commit에 결박하기 위한 제한된 seam이다. transaction 시작/종료 권한은 호출자에게
        남아 있어 이 메서드는 독립 호출을 허용하지 않는다.
        """
        self._durable_transaction.require_completion_context(transaction_context)
        canonical = canonical_completion_handoff(handoff)
        request = _select_question_request_no_commit(self._connection, canonical.request_id)
        if request is None:
            raise CompletionNotFoundError(f"Question Request가 없습니다: {canonical.request_id!r}")
        receipt = self._receipt_for_request(canonical.request_id)
        if receipt is not None:
            stored_handoff = _decode_handoff(receipt)
            if isinstance(stored_handoff, _LegacyV1ApprovedCandidate):
                raise CompletionConcurrencyError(
                    "legacy v1 approved receipt는 새 replay 증거가 아닙니다."
                )
            if _handoff_json(stored_handoff) != _handoff_json(canonical):
                raise CompletionConcurrencyError(
                    "같은 Question Request에 다른 Finalization 후보가 이미 확정됐습니다."
                )
            existing = self._bundle_for_request_no_transaction(canonical.request_id)
            if existing is None:
                raise IncompleteCompletionStateError(
                    "receipt가 있지만 CompletionBundle을 복원할 수 없습니다."
                )
            return existing.completion
        self._reject_partial_before_plan(request)
        plan = (
            self._planner.plan(request, canonical)
            if approval_reader is None
            else self._planner.plan_with_approval_reader(
                request, canonical, approval_reader=approval_reader
            )
        )
        self._persist_plan(plan.bundle, plan.handoff, expected_request=plan.expected_request)
        restored = self._bundle_for_request_no_transaction(canonical.request_id)
        if restored is None or restored != canonical_completion_bundle(plan.bundle):
            raise IncompleteCompletionStateError(
                "commit 전 exact-read CompletionBundle이 planner 결과와 다릅니다."
            )
        self._fault("before_commit")
        return restored.completion

    def _persist_plan(
        self,
        bundle: CompletionBundle,
        handoff: CompletionHandoff,
        *,
        expected_request: QuestionRequest,
    ) -> None:
        bundle = canonical_completion_bundle(bundle)
        record = bundle.answer_record
        request_id = bundle.completion.request_id
        record_id = bundle.completion.record_id
        if (
            self._connection.execute(
                "SELECT 1 FROM answer_records WHERE request_id COLLATE BINARY = ?",
                (request_id,),
            ).fetchone()
            is not None
        ):
            raise IncompleteCompletionStateError(
                "receipt 없이 request-aware AnswerRecord가 이미 존재합니다."
            )
        if (
            self._connection.execute(
                "SELECT 1 FROM answer_records WHERE record_id COLLATE BINARY = ?",
                (record_id,),
            ).fetchone()
            is not None
        ):
            raise CompletionIdCollisionError(f"record ID가 이미 존재합니다: {record_id!r}")

        sources_json = _canonical_json(list(record.sources))
        self._connection.execute(
            "INSERT INTO answer_records ("
            "record_id, question, answer_text, answered_by, agent_id, mode, session_id, "
            "answered_at, needs_correction_review, request_id, sources_json, snapshot_sha"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.record_id,
                record.question,
                record.answer_text,
                record.answered_by,
                record.agent_id,
                record.mode,
                record.session_id,
                record.answered_at.isoformat(),
                int(record.needs_correction_review),
                record.request_id,
                sources_json,
                record.snapshot_sha,
            ),
        )
        self._fault("after_answer_record")

        if not _compare_and_set_question_request_no_commit(
            self._connection,
            request_id,
            expected_request.revision,
            expected_request,
            bundle.request,
        ):
            raise CompletionConcurrencyError(
                "Finalization 중 Question Request snapshot이 바뀌었습니다."
            )
        self._fault("after_request")

        audit = bundle.terminal_audit
        self._connection.execute(
            "INSERT INTO terminal_answer_audits ("
            "request_id, record_id, org_id, requester_id, attempt, route_json, "
            "responsibility_json, candidate_mode, final_mode, approval_json, "
            "completed_at, audit_schema_version"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                audit.request_id,
                audit.record_id,
                audit.org_id,
                audit.requester_id,
                audit.attempt,
                _model_json(audit.route),
                _model_json(audit.responsibility),
                audit.candidate_mode,
                audit.final_mode,
                _model_json(audit.approval),
                audit.completed_at.isoformat(),
                _AUDIT_SCHEMA_VERSION,
            ),
        )
        self._fault("after_audit")

        turn = bundle.session_turn
        if turn is not None:
            session_id = bundle.request.session_id
            if session_id is None:
                raise IncompleteCompletionStateError(
                    "session_id 없는 Request에 SessionTurn이 생겼습니다."
                )
            self._connection.execute(
                "INSERT INTO request_session_turns ("
                "request_id, record_id, session_id, question, answer_text, answered_by, at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    record_id,
                    session_id,
                    turn.question,
                    turn.answer_text,
                    turn.answered_by,
                    turn.at.isoformat(),
                ),
            )
        self._fault("after_session")

        delivery = bundle.delivery
        self._connection.execute(
            "INSERT INTO question_delivery_outbox ("
            "request_id, record_id, kind, created_at"
            ") VALUES (?, ?, ?, ?)",
            (
                delivery.request_id,
                delivery.record_id,
                delivery.kind,
                delivery.created_at.isoformat(),
            ),
        )
        self._fault("after_outbox")

        handoff_json = _handoff_json(handoff)
        self._connection.execute(
            "INSERT INTO question_completion_receipts ("
            "request_id, record_id, handoff_kind, handoff_json, handoff_sha256, "
            "handoff_schema_version, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                record_id,
                _handoff_kind(handoff),
                handoff_json,
                hashlib.sha256(handoff_json.encode("utf-8")).hexdigest(),
                _HANDOFF_SCHEMA_VERSION,
                bundle.completion.completed_at.isoformat(),
            ),
        )
        self._fault("after_completion_receipt")

    def _bundle_for_request_no_transaction(
        self,
        request_id: str,
    ) -> CompletionBundle | None:
        request = _select_question_request_no_commit(self._connection, request_id)
        receipt_rows = self._connection.execute(
            "SELECT * FROM question_completion_receipts WHERE request_id COLLATE BINARY = ?",
            (request_id,),
        ).fetchall()
        audit_rows = self._connection.execute(
            "SELECT * FROM terminal_answer_audits WHERE request_id COLLATE BINARY = ?",
            (request_id,),
        ).fetchall()
        turn_rows = self._connection.execute(
            "SELECT * FROM request_session_turns WHERE request_id COLLATE BINARY = ?",
            (request_id,),
        ).fetchall()
        outbox_rows = self._connection.execute(
            "SELECT * FROM question_delivery_outbox WHERE request_id COLLATE BINARY = ?",
            (request_id,),
        ).fetchall()

        if not receipt_rows:
            if request is not None and isinstance(request.state, AnsweredRequest):
                raise IncompleteCompletionStateError(
                    "AnsweredRequest에 completion receipt가 없습니다."
                )
            if audit_rows or turn_rows or outbox_rows:
                raise IncompleteCompletionStateError(
                    "nonterminal Request에 completion-native artifact가 남았습니다."
                )
            v2_answer_rows = self._connection.execute(
                "SELECT record_id FROM answer_records "
                "WHERE request_id COLLATE BINARY = ? "
                "AND (sources_json IS NOT NULL OR snapshot_sha IS NOT NULL)",
                (request_id,),
            ).fetchall()
            if v2_answer_rows:
                raise IncompleteCompletionStateError(
                    "receipt 없이 v2 request-aware AnswerRecord 흔적이 남았습니다."
                )
            return None
        if len(receipt_rows) != 1:
            raise IncompleteCompletionStateError(
                "Question Request에 completion receipt가 정확히 한 건이 아닙니다."
            )
        if request is None or not isinstance(request.state, AnsweredRequest):
            raise IncompleteCompletionStateError(
                "completion receipt가 있지만 AnsweredRequest가 없습니다."
            )
        receipt = receipt_rows[0]
        handoff = _decode_handoff(receipt)
        record_id = _strict_text(receipt, "record_id")
        if (
            _strict_text(receipt, "request_id") != request_id
            or request.state.record_id != record_id
        ):
            raise IncompleteCompletionStateError(
                "Request, receipt, record ID가 exact-link되지 않습니다."
            )

        record_rows = self._connection.execute(
            "SELECT * FROM answer_records WHERE request_id COLLATE BINARY = ?",
            (request_id,),
        ).fetchall()
        if len(record_rows) != 1 or len(audit_rows) != 1 or len(outbox_rows) != 1:
            raise IncompleteCompletionStateError(
                "AnswerRecord, audit, outbox가 각각 정확히 한 건이 아닙니다."
            )
        if request.session_id is None:
            if turn_rows:
                raise IncompleteCompletionStateError(
                    "session_id 없는 Request에 request SessionTurn이 있습니다."
                )
        elif len(turn_rows) != 1:
            raise IncompleteCompletionStateError(
                "session_id가 있는 Request에 SessionTurn이 정확히 한 건이 아닙니다."
            )

        record = self._record_from_row(record_rows[0], request_id, record_id)
        audit = self._audit_from_row(audit_rows[0], request_id, record_id)
        delivery = self._delivery_from_row(outbox_rows[0], request_id, record_id)
        turn = (
            None
            if not turn_rows
            else self._turn_from_row(
                turn_rows[0], request, request_id=request_id, record_id=record_id
            )
        )
        receipt_at = _parse_aware(receipt["created_at"], field="receipt.created_at")
        raw_times = {
            _strict_text(receipt, "created_at"),
            _strict_text(record_rows[0], "answered_at"),
            _strict_text(audit_rows[0], "completed_at"),
            _strict_text(outbox_rows[0], "created_at"),
            request.updated_at.isoformat(),
        }
        if turn_rows:
            raw_times.add(_strict_text(turn_rows[0], "at"))
        if len(raw_times) != 1:
            raise CorruptSqliteCompletionError(
                "completion artifact 시각 TEXT가 exact equality를 이루지 않습니다."
            )

        approval = audit.approval
        completion = AnswerCompletion(
            request_id=request_id,
            record_id=record_id,
            text=record.answer_text,
            answered_by=record.answered_by,
            agent_id=record.agent_id,
            mode=record.mode,
            sources=record.sources,
            snapshot_sha=record.snapshot_sha,
            review_status=(
                "approved" if isinstance(approval, HumanApprovalEvidence) else "not_required"
            ),
            completed_at=receipt_at,
        )
        try:
            bundle = canonical_completion_bundle(
                CompletionBundle(
                    completion=completion,
                    request=request,
                    answer_record=record,
                    terminal_audit=audit,
                    session_turn=turn,
                    delivery=delivery,
                )
            )
        except Exception as error:
            if isinstance(error, IncompleteCompletionStateError):
                raise
            raise CorruptSqliteCompletionError(
                "저장된 completion artifact exact-link 검증에 실패했습니다."
            ) from error
        self._verify_handoff(bundle, handoff)
        return bundle

    def _record_from_row(
        self,
        row: sqlite3.Row,
        request_id: str,
        record_id: str,
    ) -> AnswerRecord:
        sources_raw = _strict_json_array(row["sources_json"], field="sources_json")
        if any(not isinstance(value, str) or not value.strip() for value in sources_raw):
            raise CorruptSqliteCompletionError("sources_json은 nonblank 문자열 배열이어야 합니다.")
        correction = _strict_int(row, "needs_correction_review")
        if correction not in (0, 1):
            raise CorruptSqliteCompletionError(
                "Finalization AnswerRecord의 correction review flag는 0 또는 1이어야 합니다."
            )
        raw_request_id = _strict_text(row, "request_id")
        raw_record_id = _strict_text(row, "record_id")
        if raw_request_id != request_id or raw_record_id != record_id:
            raise IncompleteCompletionStateError(
                "AnswerRecord request/record ID가 receipt와 다릅니다."
            )
        try:
            return AnswerRecord.model_validate(
                {
                    "record_id": raw_record_id,
                    "question": _strict_text(row, "question"),
                    "answer_text": _strict_text(row, "answer_text"),
                    "answered_by": _strict_text(row, "answered_by"),
                    "agent_id": _strict_text(row, "agent_id"),
                    "mode": _strict_text(row, "mode"),
                    "sources": tuple(cast(str, value) for value in sources_raw),
                    "snapshot_sha": _optional_text(row, "snapshot_sha"),
                    "session_id": _optional_text(row, "session_id"),
                    "answered_at": _parse_aware(
                        row["answered_at"], field="answer_records.answered_at"
                    ),
                    "needs_correction_review": bool(correction),
                    "request_id": raw_request_id,
                },
                strict=True,
            )
        except (ValidationError, ValueError, TypeError) as error:
            raise CorruptSqliteCompletionError(
                "AnswerRecord 행이 v2 도메인 계약과 다릅니다."
            ) from error

    def _audit_from_row(
        self,
        row: sqlite3.Row,
        request_id: str,
        record_id: str,
    ) -> TerminalAnswerAudit:
        if _strict_int(row, "audit_schema_version") != _AUDIT_SCHEMA_VERSION:
            raise CorruptSqliteCompletionError("지원하지 않는 terminal audit schema입니다.")
        route_json = _strict_text(row, "route_json")
        responsibility_json = _strict_text(row, "responsibility_json")
        approval_json = _strict_text(row, "approval_json")
        _strict_json_object(route_json, field="route_json")
        _strict_json_object(responsibility_json, field="responsibility_json")
        _strict_json_object(approval_json, field="approval_json")
        try:
            route = RouteTarget.model_validate_json(route_json, strict=True)
            responsibility = AnswerResponsibilitySnapshot.model_validate_json(
                responsibility_json, strict=True
            )
            approval = _APPROVAL_ADAPTER.validate_json(approval_json, strict=True)
            audit = TerminalAnswerAudit(
                request_id=_strict_text(row, "request_id"),
                record_id=_strict_text(row, "record_id"),
                org_id=_strict_text(row, "org_id"),
                requester_id=_strict_text(row, "requester_id"),
                attempt=_strict_int(row, "attempt"),
                route=route,
                responsibility=responsibility,
                candidate_mode=cast(AnswerMode, _strict_text(row, "candidate_mode")),
                final_mode=cast(AnswerMode, _strict_text(row, "final_mode")),
                approval=approval,
                completed_at=_parse_aware(
                    row["completed_at"], field="terminal_answer_audits.completed_at"
                ),
            )
        except Exception as error:
            if isinstance(error, CorruptSqliteCompletionError):
                raise
            raise CorruptSqliteCompletionError(
                "Terminal Answer Audit 행이 strict schema와 다릅니다."
            ) from error
        if audit.request_id != request_id or audit.record_id != record_id:
            raise IncompleteCompletionStateError("Terminal Answer Audit ID가 receipt와 다릅니다.")
        if _model_json(route) != route_json:
            raise CorruptSqliteCompletionError("route_json canonical round-trip이 다릅니다.")
        if _model_json(responsibility) != responsibility_json:
            raise CorruptSqliteCompletionError(
                "responsibility_json canonical round-trip이 다릅니다."
            )
        if _model_json(approval) != approval_json:
            raise CorruptSqliteCompletionError("approval_json canonical round-trip이 다릅니다.")
        return audit

    @staticmethod
    def _delivery_from_row(
        row: sqlite3.Row,
        request_id: str,
        record_id: str,
    ) -> DeliveryOutboxEntry:
        try:
            delivery = DeliveryOutboxEntry(
                kind=cast(Literal["answer_ready"], _strict_text(row, "kind")),
                request_id=_strict_text(row, "request_id"),
                record_id=_strict_text(row, "record_id"),
                created_at=_parse_aware(
                    row["created_at"], field="question_delivery_outbox.created_at"
                ),
            )
        except Exception as error:
            if isinstance(error, CorruptSqliteCompletionError):
                raise
            raise CorruptSqliteCompletionError("Delivery Outbox 행이 손상됐습니다.") from error
        if delivery.request_id != request_id or delivery.record_id != record_id:
            raise IncompleteCompletionStateError("Delivery Outbox ID가 receipt와 다릅니다.")
        return delivery

    @staticmethod
    def _turn_from_row(
        row: sqlite3.Row,
        request: QuestionRequest,
        *,
        request_id: str,
        record_id: str,
    ) -> SessionTurn:
        if (
            _strict_text(row, "request_id") != request_id
            or _strict_text(row, "record_id") != record_id
            or _strict_text(row, "session_id") != request.session_id
        ):
            raise IncompleteCompletionStateError(
                "request SessionTurn의 request/record/session ID가 다릅니다."
            )
        try:
            return SessionTurn.for_request(
                request_id=request_id,
                question=_strict_text(row, "question"),
                answer_text=_strict_text(row, "answer_text"),
                answered_by=_strict_text(row, "answered_by"),
                at=_parse_aware(row["at"], field="request_session_turns.at"),
            )
        except Exception as error:
            if isinstance(error, CorruptSqliteCompletionError):
                raise
            raise CorruptSqliteCompletionError("request SessionTurn 행이 손상됐습니다.") from error

    @staticmethod
    def _verify_handoff(bundle: CompletionBundle, handoff: _StoredHandoff) -> None:
        audit = bundle.terminal_audit
        record = bundle.answer_record
        if (
            handoff.request_id != bundle.request.request_id
            or handoff.expected_revision + 1 != bundle.request.revision
            or (bundle.request.intent is not None and bundle.request.intent != handoff.route.intent)
            or handoff.route != audit.route
            or handoff.attempt != audit.attempt
            or handoff.candidate.text != record.answer_text
            or handoff.candidate.sources != record.sources
            or handoff.candidate.snapshot_sha != record.snapshot_sha
            or handoff.candidate.mode != audit.candidate_mode
        ):
            raise CorruptSqliteCompletionError(
                "receipt handoff가 Request, AnswerRecord, audit과 다릅니다."
            )
        if isinstance(handoff, FinalizationCandidate):
            if (
                not isinstance(audit.approval, NoApprovalEvidence)
                or handoff.approval_evaluation.policy_version != audit.approval.policy_version
                or handoff.approval_evaluation.needs_correction_review
                != audit.approval.needs_correction_review
                or handoff.route.requires_approval
                or handoff.candidate.mode == "draft_only"
                or audit.final_mode != handoff.candidate.mode
            ):
                raise CorruptSqliteCompletionError(
                    "승인 불필요 receipt와 terminal Approval 증거가 다릅니다."
                )
            return
        approval = audit.approval
        expected_mode: AnswerMode = (
            "full" if handoff.candidate.mode == "draft_only" else handoff.candidate.mode
        )
        if (
            not isinstance(approval, HumanApprovalEvidence)
            or approval.item_id != handoff.item_id
            or approval.approved_by != handoff.approved_by
            or approval.approved_at.isoformat() != handoff.approved_at.isoformat()
            or approval.policy_version != handoff.policy_version
            or handoff.edited != (approval.action == "approve_with_edit")
            or audit.final_mode != expected_mode
        ):
            raise CorruptSqliteCompletionError("승인 receipt와 terminal Approval 증거가 다릅니다.")

    def _reject_partial_before_plan(self, request: QuestionRequest) -> None:
        if isinstance(request.state, AnsweredRequest):
            raise IncompleteCompletionStateError("AnsweredRequest에 completion receipt가 없습니다.")
        request_id = request.request_id
        if self._count(
            "SELECT COUNT(*) FROM answer_records WHERE request_id COLLATE BINARY = ?",
            (request_id,),
        ):
            raise IncompleteCompletionStateError(
                "receipt 없이 request-aware AnswerRecord가 이미 존재합니다."
            )
        residual = sum(
            self._count(
                f"SELECT COUNT(*) FROM {table} WHERE request_id COLLATE BINARY = ?",
                (request_id,),
            )
            for table in (
                "terminal_answer_audits",
                "request_session_turns",
                "question_delivery_outbox",
            )
        )
        if residual:
            raise IncompleteCompletionStateError(
                "nonterminal Request에 completion-native artifact가 남았습니다."
            )

    def _receipt_for_request(self, request_id: str) -> sqlite3.Row | None:
        rows = self._connection.execute(
            "SELECT * FROM question_completion_receipts WHERE request_id COLLATE BINARY = ?",
            (request_id,),
        ).fetchall()
        if len(rows) > 1:
            raise IncompleteCompletionStateError(
                "Question Request receipt가 정확히 하나가 아닙니다."
            )
        return None if not rows else rows[0]

    def _count(self, sql: str, params: tuple[object, ...]) -> int:
        row = self._connection.execute(sql, params).fetchone()
        if row is None or type(row[0]) is not int:
            raise CorruptSqliteCompletionError("completion artifact COUNT가 손상됐습니다.")
        return int(row[0])

    def _fault(self, point: SqliteCompletionFaultPoint) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    @contextmanager
    def _completion_scope(self) -> Generator[None]:
        with self._lock:
            if self._completion_in_progress:
                raise ReentrantCompletionMutationError(
                    "Finalization 중 같은 SQLite completion state에 재진입할 수 없습니다."
                )
            self._completion_in_progress = True
            try:
                yield
            finally:
                self._completion_in_progress = False

    @contextmanager
    def _read_scope(self) -> Generator[None]:
        with self._lock:
            if getattr(self._transaction_ownership.state, "write_depth", 0) > 0:
                raise ReentrantCompletionMutationError(
                    "durable write 중 Completion public read는 uncommitted state를 관찰할 수 없습니다."
                )
            owns_transaction = not self._connection.in_transaction
            try:
                if owns_transaction:
                    self._connection.execute("BEGIN")
                yield
                if owns_transaction:
                    self._connection.commit()
            except Exception as error:
                if owns_transaction and self._connection.in_transaction:
                    self._connection.rollback()
                if isinstance(error, sqlite3.Error) and _is_storage_unavailable(error):
                    raise SqliteCompletionStorageUnavailableError(
                        "SQLite completion snapshot을 읽을 수 없습니다."
                    ) from error
                if isinstance(error, sqlite3.DatabaseError):
                    raise CorruptSqliteCompletionError(
                        "SQLite completion snapshot schema/data를 읽을 수 없습니다."
                    ) from error
                raise

    def _reject_reentrant_mutation(self) -> None:
        if (
            self._completion_in_progress
            or getattr(self._transaction_ownership.state, "write_depth", 0) > 0
        ):
            raise ReentrantCompletionMutationError(
                "durable Finalization callback은 SQLite Question Request를 변경할 수 없습니다."
            )

    def _rollback(self) -> None:
        if self._connection.in_transaction:
            self._connection.rollback()

    @staticmethod
    def _raise_write_error(error: Exception, *, operation: str) -> NoReturn:
        nested: BaseException | None = error
        visited: set[int] = set()
        while nested is not None and id(nested) not in visited:
            visited.add(id(nested))
            if isinstance(nested, ReentrantCompletionMutationError):
                raise nested
            nested = nested.__cause__ or nested.__context__
        if isinstance(
            error,
            (
                AnswerFinalizationError,
                DuplicateQuestionRequestError,
                CorruptQuestionRequestError,
                ValueError,
                TypeError,
            ),
        ):
            raise error
        if isinstance(error, sqlite3.Error) and _is_storage_unavailable(error):
            raise SqliteCompletionStorageUnavailableError(
                f"{operation} 중 SQLite 저장소를 사용할 수 없습니다."
            ) from error
        if isinstance(error, sqlite3.DatabaseError):
            raise CorruptSqliteCompletionError(
                f"{operation} 중 SQLite schema/constraint 오류가 발생했습니다."
            ) from error
        raise error
