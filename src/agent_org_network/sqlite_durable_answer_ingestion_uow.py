"""P17.9 S5.4 답 수신·ticket 종결 원자화 Unit of Work(ADR 0042 §9 ⑦⑧⑬⑭).

워커가 제출한 답을 ``durable_dispatch_answer_receipts``·dispatch lease
release·``durable_linked_work_tickets`` 종결·Completion terminal 확정까지
**한 SQLite transaction**으로 묶는다. Request의 ``AwaitingAnswer →
AnsweredRequest`` CAS는 이 UoW가 직접 하지 않는다 —
``SqliteQuestionCompletionUnitOfWork.complete_in_transaction``(``_persist_plan``)
내부가 수행한다(이중 전이 금지).

**"stale epoch submit 거부" 판정(§9 ⑧).** ``DurableWorkerAnswerCommand``에는
``lease_epoch`` 필드를 두지 않는다 — 답 경로를 dispatch lease epoch로 fence하면
TTL보다 느린 **유효한 답**을 버려 사용자 결과를 잃는 회귀가 된다. stale submit은
다음 셋으로 정확히 잡는다: ①``ticket.status != 'pending'``(중복 배달분의 두 번째
답을 흡수) ②Request 이동(revision·state·ticket_id) ③owner 불일치(durable owner
fence). dispatch lease는 그래서 이 UoW의 CAS 조건이 아니라 그저 "정리 대상"이다 —
release UPDATE의 rowcount가 0이어도 정상이다(답이 lease를 이긴다).

**CentralAuthorizer를 주입받지 않는다** — 답 수신 권한은 기존
``worker.submit`` 경계(transport의 ``_authorize_submit``)가 이미 행사했고, 이
UoW는 durable owner fence(``ticket.owner_subject_id``)만 더한다(새 중앙 action
0).

**오류 계열 규율(S5.3 §9 ⑭ 계승).** 이 모듈 자신의 명령/replay/도메인 검증
실패는 ``DurableAnswerIngestion{Error,Conflict,Unavailable,Busy}`` +
``DurableAnswerApprovalRequired``로만 신호한다. S5.1 dispatch delivery
capability(``validate_sqlite_durable_dispatch_delivery_connection``) 검증은
**S5.2가 이미 확립한 번역을 그대로 재사용**한다(``DurableDispatchLeaseUnavailable``/
``Busy``) — 같은 capability 손상이 어느 S5 모듈의 어느 진입점에서 관측되든 항상
같은 타입이어야 한다는 규율이 S5.2→S5.3에 이어 이 모듈에도 적용된다. 반대로
Completion 자신의 오류 계열(``AnswerFinalizationError`` 등, ``complete_in_transaction``
호출로 소비)은 **wrap하지 않고 그대로 통과**시킨다 — 호출자가 "내 명령이
틀렸다"(``DurableAnswerIngestion*``)와 "다른 하위 시스템이 손상됐다"(S5.1
capability·Completion)를 구분할 수 있어야 하기 때문이다. 이 규율에서 다루지
않는 raw ``sqlite3.Error``(이 UoW 자신의 write 경계에서 발생)만 BUSY/LOCKED
여부로 이 모듈 자신의 Unavailable/Busy로 분류한다.

**이 모듈의 Busy는 두 타입이다(review-s54 P2-6).** ctor·accept()의
capability 검증 단계에서 난 lock 경합은 (위 규율대로) ``DurableDispatchLeaseBusy``
로, accept() 자신의 write 경계(``begin_immediate``~``commit``, receipt
INSERT·lease release·ticket status UPDATE·``complete_in_transaction``)에서
난 lock 경합은 ``DurableAnswerIngestionBusy``로 관측된다. 둘 다
"재시도하면 풀릴 수 있는 lock 압력"이라는 같은 사실을 말하지만 타입은
다르다 — 전송/재시도 계층이 "지금 붐빈다"만 판정하려면 **이 모듈 자신의
Busy뿐 아니라 ``DurableDispatchLeaseBusy``도 함께** 잡아야 한다(하나만
잡으면 진입점에 따라 congestion 신호를 놓친다).

digest는 command-local만 계산한다(request 읽기·Registry 호출 어느 것도
digest 계산보다 선행하지 않는다 — S4.4 교정 계승). ``route``·``policy_version``은
digest에서 제외한다.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, NoReturn

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
from agent_org_network.approval import AnswerCandidate, FinalizationCandidate
from agent_org_network.question_request import AnsweredRequest, AwaitingAnswer
from agent_org_network.sqlite_durable_dispatch_delivery import (
    validate_sqlite_durable_dispatch_delivery_connection,
)
from agent_org_network.sqlite_durable_dispatch_lease_uow import (
    DurableDispatchLeaseBusy,
    DurableDispatchLeaseError,
    DurableDispatchLeaseUnavailable,
)
from agent_org_network.worker_authorization import WorkerConnectionPrincipal


class DurableAnswerIngestionError(RuntimeError):
    """Base error deliberately free of channel/Completion internals."""


class DurableAnswerIngestionConflict(DurableAnswerIngestionError):
    """stale submit·owner fence 위반 — 정상 경쟁·중복 배달 흡수."""


class DurableAnswerIngestionUnavailable(DurableAnswerIngestionError):
    """이 UoW 자신의 형식·replay·write 오류 — 재시도로 해소되지 않는다."""


class DurableAnswerIngestionBusy(DurableAnswerIngestionUnavailable):
    """이 UoW 자신의 write 경계에서 난 lock 경합(SQLITE_BUSY/LOCKED)."""


class DurableAnswerApprovalRequired(DurableAnswerIngestionError):
    """승인이 필요한 답은 S5 범위 밖이다 — 기존 Approval 경계가 종착시킨다."""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(kind: str, raw: str) -> str:
    return f"{kind}:{_sha(raw)}"


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


# S5.1/S5.2/S5.3의 canonical UTC instant 문법(고정폭·고정 `+00:00`)을 이 모듈
# 로컬로 재현한다 — S4.1 timestamp와 문자열로 비교하지 않는다.
_TIMESTAMP_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00\Z"
)


def _instant(value: datetime) -> str:
    if type(value) is not datetime or value.utcoffset() is None:
        raise DurableAnswerIngestionUnavailable(
            "dispatch answer canonical instant에는 tz-aware datetime이 필요합니다."
        )
    rendered = value.astimezone(UTC).isoformat(timespec="microseconds")
    if _TIMESTAMP_RE.fullmatch(rendered) is None:
        raise DurableAnswerIngestionUnavailable(
            "dispatch answer canonical instant 형식이 올바르지 않습니다."
        )
    return rendered


# BUSY/LOCKED만 좁힌다(S5.2 `_is_busy`와 동형·모듈 로컬 재현).
_BUSY_SQLITE_CODES: Final = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


def _is_busy(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in _BUSY_SQLITE_CODES


def _no_fault(_point: str) -> None:
    return None


_ACTION: Final = "work_ticket.complete"


@dataclass(frozen=True)
class DurableWorkerAnswerCommand:
    """워커가 제출한 답 하나 — ``ApprovalBoundary.gate_candidate`` 결과를 그대로 나른다.

    ``lease_epoch`` 필드가 **의도적으로 없다** — 답 경로를 dispatch lease로
    fence하면 TTL보다 느린 유효한 답을 버리는 회귀가 된다(모듈 docstring 참조).
    """

    ticket_id: str
    request_id: str
    expected_request_revision: int
    handoff: FinalizationCandidate


@dataclass(frozen=True)
class DurableAnswerAccepted:
    receipt_id: str
    ticket_id: str
    request_id: str
    record_id: str
    request_revision: int


def _valid_command(command: DurableWorkerAnswerCommand) -> None:
    if (
        type(command.ticket_id) is not str
        or not command.ticket_id.strip()
        or type(command.request_id) is not str
        or not command.request_id.strip()
        or type(command.expected_request_revision) is not int
        or command.expected_request_revision < 1
        or type(command.handoff) is not FinalizationCandidate
    ):
        raise DurableAnswerIngestionUnavailable(
            "typed work_ticket.complete command 형식이 올바르지 않습니다."
        )


def _answer_sha256(candidate: AnswerCandidate) -> str:
    # `_valid_command`가 `type(command.handoff) is FinalizationCandidate`를
    # 이미 exact 강제하므로(그 pydantic model의 `candidate` 필드는
    # `AnswerCandidate`), 여기서는 `object`로 느슨하게 받지 않고 정확한
    # 타입으로 좁힌다 — 그래야 `.text`/`.mode`/`.sources` 오타를 pyright가
    # 잡는다(review-s54 P2-2).
    return _sha(
        _json(
            {
                "text": candidate.text,
                "mode": candidate.mode,
                "sources": list(candidate.sources),
            }
        )
    )


def _answer_digest(
    submitter: WorkerConnectionPrincipal, command: DurableWorkerAnswerCommand
) -> str:
    # command-local만(request 읽기·Registry 호출 어느 것도 선행하지 않는다) —
    # S4.4 교정 계승. route·policy_version은 제외한다.
    return _sha(
        _json(
            {
                "action": _ACTION,
                "org_id": submitter.org_id,
                "ticket_id": command.ticket_id,
                "request_id": command.request_id,
                "expected_request_revision": command.expected_request_revision,
                "by_owner_ref": _ref("subject", submitter.owner_id),
                "answer_sha256": _answer_sha256(command.handoff.candidate),
            }
        )
    )


class DurableAnswerIngestionUnitOfWork:
    """``durable_dispatch_answer_receipts``와 ticket 종결·Completion을 원자 결박한다."""

    def __init__(
        self,
        *,
        completion: SqliteQuestionCompletionUnitOfWork,
        clock: Callable[[], datetime],
        receipt_id_factory: Callable[[], str],
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._completion = completion
        self._tx: SqliteCompletionTransaction = completion.durable_transaction()
        self._clock = clock
        self._receipt_id_factory = receipt_id_factory
        self._fault: Callable[[str], None] = fault_injector or _no_fault
        try:
            with self._tx.scope():
                self._tx.validate_component(validate_sqlite_durable_dispatch_delivery_connection)
        except Exception as error:
            _reraise_dispatch_capability(error)

    def accept(
        self,
        *,
        submitter: WorkerConnectionPrincipal,
        command: DurableWorkerAnswerCommand,
    ) -> DurableAnswerAccepted:
        if type(submitter) is not WorkerConnectionPrincipal:
            raise DurableAnswerIngestionUnavailable(
                "typed WorkerConnectionPrincipal submitter가 필요합니다."
            )
        if type(command) is not DurableWorkerAnswerCommand:
            raise DurableAnswerIngestionUnavailable(
                "typed DurableWorkerAnswerCommand가 필요합니다."
            )
        _valid_command(command)
        digest = _answer_digest(submitter, command)
        with self._tx.scope():
            try:
                self._tx.begin_immediate()
                self._validate_capability()
                receipt = self._tx.execute(
                    "SELECT * FROM durable_dispatch_answer_receipts WHERE command_digest=?",
                    (digest,),
                ).fetchone()
                result = (
                    self._stored_result(receipt, submitter, command)
                    if receipt is not None
                    else self._fresh(submitter, command, digest)
                )
                self._tx.commit()
                return result
            except Exception as error:
                if self._tx.in_transaction:
                    self._tx.rollback()
                _reraise_write(error, message="durable answer ingestion이 실패했습니다.")

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _validate_capability(self) -> None:
        try:
            self._tx.validate_component_in_transaction(
                validate_sqlite_durable_dispatch_delivery_connection
            )
        except Exception as error:
            _reraise_dispatch_capability(error)

    def _fresh(
        self,
        submitter: WorkerConnectionPrincipal,
        command: DurableWorkerAnswerCommand,
        digest: str,
    ) -> DurableAnswerAccepted:
        owner_ref = _ref("subject", submitter.owner_id)
        ticket = self._tx.execute(
            "SELECT * FROM durable_linked_work_tickets WHERE ticket_id=?",
            (command.ticket_id,),
        ).fetchone()
        if (
            ticket is None
            or ticket["org_id"] != submitter.org_id
            or ticket["request_id"] != command.request_id
            or ticket["status"] != "pending"
            or ticket["owner_subject_id"] != owner_ref
        ):
            raise DurableAnswerIngestionConflict(
                "dispatch answer 대상 WorkTicket이 유효하지 않습니다."
            )
        request = self._tx.select_question_request(command.request_id)
        if (
            request is None
            or request.org_id != submitter.org_id
            or not isinstance(request.state, AwaitingAnswer)
            or request.state.ticket_id != command.ticket_id
            or request.revision != command.expected_request_revision
            or request.revision != ticket["awaiting_revision"] + 1
        ):
            raise DurableAnswerIngestionConflict(
                "dispatch answer 대상 Question Request가 유효하지 않습니다."
            )
        # ⑧ 승인 필요 후보는 S5 범위 밖 — write 0으로 기존 Approval 경계에 넘긴다.
        if request.state.route.requires_approval or command.handoff.candidate.mode == "draft_only":
            raise DurableAnswerApprovalRequired(
                "승인이 필요한 답은 Approval 경계가 종착시킵니다."
            )
        handoff = command.handoff
        if (
            handoff.request_id != command.request_id
            or handoff.expected_revision != request.revision
            or handoff.attempt != request.state.attempt
            or handoff.attempt != ticket["attempt"]
            or handoff.route != request.state.route
        ):
            raise DurableAnswerIngestionConflict(
                "dispatch answer handoff가 대상과 결박되지 않습니다."
            )

        now = self._clock()
        created = _instant(now)
        receipt_ref = _ref("receipt", self._new_receipt_id())
        answer_sha = _answer_sha256(handoff.candidate)

        # WRITE 순서: receipt → lease 종료 → ticket status → Completion terminal.
        self._tx.execute(
            "INSERT INTO durable_dispatch_answer_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_ref,
                submitter.org_id,
                command.ticket_id,
                command.request_id,
                digest,
                owner_ref,
                _ACTION,
                command.expected_request_revision,
                answer_sha,
                created,
            ),
        )
        self._fault("after_answer_receipt")

        # dispatch lease는 이 write의 CAS 조건이 아니다 — 답이 lease를 이긴다.
        # rowcount 0(lease가 만료됐거나 애초에 없었음)도 정상이다. 단
        # acquired_at<=?(같은 이 UoW의 clock 값)는 남겨둔다 — 남의 테이블
        # (S5.1 `durable_dispatch_leases`)을 직접 쓰므로 그 행 불변식
        # (`expires_at >= acquired_at`, sqlite_durable_dispatch_delivery.py)을
        # 이 CAS의 WHERE 절 자체가 지켜야 한다(S5.2 `_release_cas`/`_renew_cas`와
        # 동형 — 이 UoW는 clock이 acquired_at보다 과거로 skew된 채 호출돼도
        # 그 불변식을 깨는 행을 커밋하지 않는다: rowcount 0이면 lease는
        # `leased`로 남지만 ticket은 곧 completed로 바뀌므로 S5.3·S5.5는
        # `status='pending'`만 스캔해 무해하다 — review-s54 P1).
        self._tx.execute(
            "UPDATE durable_dispatch_leases SET state='released', expires_at=? "
            "WHERE ticket_id=? AND state='leased' AND acquired_at<=?",
            (created, command.ticket_id, created),
        )
        self._fault("after_lease_release")

        cursor = self._tx.execute(
            "UPDATE durable_linked_work_tickets SET status='completed' "
            "WHERE ticket_id=? AND status='pending'",
            (command.ticket_id,),
        )
        if cursor.rowcount != 1:
            raise DurableAnswerIngestionConflict("WorkTicket 종결 CAS가 stale입니다.")
        self._fault("after_work_ticket_status")

        completion = self._completion.complete_in_transaction(
            handoff, transaction_context=self._tx.completion_context()
        )
        self._fault("after_completion")

        return DurableAnswerAccepted(
            receipt_id=receipt_ref,
            ticket_id=command.ticket_id,
            request_id=command.request_id,
            record_id=completion.record_id,
            request_revision=request.revision + 1,
        )

    def _stored_result(
        self,
        receipt: sqlite3.Row,
        submitter: WorkerConnectionPrincipal,
        command: DurableWorkerAnswerCommand,
    ) -> DurableAnswerAccepted:
        owner_ref = _ref("subject", submitter.owner_id)
        answer_sha = _answer_sha256(command.handoff.candidate)
        if (
            receipt["action"] != _ACTION
            or receipt["org_id"] != submitter.org_id
            or receipt["ticket_id"] != command.ticket_id
            or receipt["request_id"] != command.request_id
            or receipt["expected_request_revision"] != command.expected_request_revision
            or receipt["principal_ref"] != owner_ref
            or receipt["answer_sha256"] != answer_sha
        ):
            raise DurableAnswerIngestionUnavailable(
                "immutable dispatch answer receipt가 command와 다릅니다."
            )
        ticket = self._tx.execute(
            "SELECT * FROM durable_linked_work_tickets WHERE ticket_id=?",
            (receipt["ticket_id"],),
        ).fetchone()
        if (
            ticket is None
            or ticket["org_id"] != submitter.org_id
            or ticket["request_id"] != command.request_id
            or ticket["status"] != "completed"
        ):
            raise DurableAnswerIngestionUnavailable(
                "immutable dispatch answer receipt/ticket이 서로 다릅니다."
            )
        request = self._tx.select_question_request(command.request_id)
        if (
            request is None
            or request.org_id != submitter.org_id
            or not isinstance(request.state, AnsweredRequest)
        ):
            raise DurableAnswerIngestionUnavailable(
                "immutable dispatch answer receipt/Request 결과가 서로 다릅니다."
            )
        return DurableAnswerAccepted(
            receipt_id=receipt["receipt_id"],
            ticket_id=receipt["ticket_id"],
            request_id=receipt["request_id"],
            record_id=request.state.record_id,
            request_revision=request.revision,
        )

    def _new_receipt_id(self) -> str:
        receipt_id = self._receipt_id_factory()
        if type(receipt_id) is not str or not receipt_id.strip():
            raise DurableAnswerIngestionUnavailable("receipt identity가 올바르지 않습니다.")
        return receipt_id


def _reraise_dispatch_capability(error: Exception) -> NoReturn:
    """S5.1 dispatch delivery capability 예외를 S5.2가 이미 확립한 타입으로 재확인한다.

    같은 capability 손상은 S5.2(lease UoW)·S5.3(runner)·이 모듈 어디서 관측되든
    항상 ``DurableDispatchLeaseUnavailable``/``Busy``여야 한다 — 진입점에 따라
    다른 타입이 되면 안 된다는 규율이 여기까지 이어진다(모듈 docstring 참조).
    """
    if isinstance(error, DurableDispatchLeaseError):
        raise error
    if isinstance(error, sqlite3.Error) and _is_busy(error):
        raise DurableDispatchLeaseBusy(
            "durable answer ingestion capability를 열 수 없습니다."
        ) from error
    raise DurableDispatchLeaseUnavailable(
        "durable answer ingestion capability를 열 수 없습니다."
    ) from error


def _reraise_write(error: Exception, *, message: str) -> NoReturn:
    """accept() 바깥 경계(begin_immediate~commit)에서 이 모듈 자신의 계열로 재분류한다.

    이미 typed인 예외(이 모듈 자신·S5.1/lease capability·Completion 자신의
    ``AnswerFinalizationError`` 계열 등)는 wrap하지 않고 그대로 통과시킨다 —
    호출자가 "내 명령이 틀렸다"와 "저장소/하위 시스템이 손상됐다"를 구분해야
    한다. raw ``sqlite3.Error``만 BUSY/LOCKED 여부로 이 모듈 자신의
    Unavailable/Busy로 분류한다.
    """
    if isinstance(error, DurableAnswerIngestionError):
        raise error
    if isinstance(error, sqlite3.Error):
        if _is_busy(error):
            raise DurableAnswerIngestionBusy(message) from error
        raise DurableAnswerIngestionUnavailable(message) from error
    raise error


__all__ = [
    "DurableAnswerAccepted",
    "DurableAnswerApprovalRequired",
    "DurableAnswerIngestionBusy",
    "DurableAnswerIngestionConflict",
    "DurableAnswerIngestionError",
    "DurableAnswerIngestionUnavailable",
    "DurableAnswerIngestionUnitOfWork",
    "DurableWorkerAnswerCommand",
]
