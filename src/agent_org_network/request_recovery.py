"""비종결 QuestionRequest 재기동 reconciliation(P17.2b·ADR 0042).

이 모듈은 저장된 수명을 훑고 접수/발송 준비 상태의 복구 hook을 다시 호출하는
얇은 coordinator다. 실제 Router·Runtime·lease·트랜잭션·outbox는 소유하지 않는다.
hook은 전달받은 request_id/revision을 CAS guard로 사용해야 한다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock

from agent_org_network.question_request import (
    QuestionRequest,
    QuestionRequestStore,
    ReadyToDispatch,
    Received,
    RequestStateKind,
)

RecoverReceived = Callable[[str, int], None]
RecoverReady = Callable[[str, int, int], None]


@dataclass(frozen=True, slots=True)
class RecoveryError:
    """한 Request 복구 hook 또는 재조회에서 발생한 구조화 오류."""

    request_id: str
    revision: int
    state_kind: RequestStateKind
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """한 번의 비종결 snapshot reconciliation 결과."""

    scanned: int
    received_attempted: int
    ready_attempted: int
    waiting: int
    stale: int
    errors: tuple[RecoveryError, ...]


class RequestRecoveryRunner:
    """Received/ReadyToDispatch만 재개하고 나머지 대기 상태는 관찰한다.

    `run_once` 전체를 인스턴스 RLock으로 감싸 같은 runner의 중복 실행을 막는다.
    snapshot 이후에는 매 Request를 다시 조회해 revision과 state 전체(Ready의 attempt
    포함)가 같은 경우에만 hook을 호출한다. hook 실패 시 runner 자체는 상태를 쓰지
    않으므로 같은 비종결 상태가 다음 실행에서 다시 시도된다.
    """

    def __init__(
        self,
        store: QuestionRequestStore,
        *,
        recover_received: RecoverReceived,
        recover_ready: RecoverReady,
    ) -> None:
        self._store = store
        self._recover_received = recover_received
        self._recover_ready = recover_ready
        self._lock = RLock()

    def run_once(self) -> ReconcileReport:
        with self._lock:
            snapshot = self._store.nonterminal()
            received_attempted = 0
            ready_attempted = 0
            waiting = 0
            stale = 0
            errors: list[RecoveryError] = []

            for observed in snapshot:
                try:
                    current = self._store.get(observed.request_id)
                except Exception as exc:
                    errors.append(self._error(observed, exc))
                    continue

                if not self._is_same_recovery_target(observed, current):
                    stale += 1
                    continue
                assert current is not None

                if isinstance(current.state, Received):
                    received_attempted += 1
                    try:
                        self._recover_received(
                            current.request_id,
                            current.revision,
                        )
                    except Exception as exc:
                        errors.append(self._error(current, exc))
                elif isinstance(current.state, ReadyToDispatch):
                    ready_attempted += 1
                    try:
                        self._recover_ready(
                            current.request_id,
                            current.revision,
                            current.state.attempt,
                        )
                    except Exception as exc:
                        errors.append(self._error(current, exc))
                else:
                    # nonterminal() 계약상 여기에는 Answer/Conflict/Manager/Approval
                    # 대기 상태만 온다. terminal로 바뀌었다면 위 exact recheck가 stale로 센다.
                    waiting += 1

            return ReconcileReport(
                scanned=len(snapshot),
                received_attempted=received_attempted,
                ready_attempted=ready_attempted,
                waiting=waiting,
                stale=stale,
                errors=tuple(errors),
            )

    @staticmethod
    def _is_same_recovery_target(
        observed: QuestionRequest,
        current: QuestionRequest | None,
    ) -> bool:
        return (
            current is not None
            and not current.is_terminal
            and current.request_id == observed.request_id
            and current.revision == observed.revision
            and current.state == observed.state
        )

    @staticmethod
    def _error(request: QuestionRequest, exc: Exception) -> RecoveryError:
        return RecoveryError(
            request_id=request.request_id,
            revision=request.revision,
            state_kind=request.state.kind,
            error_type=type(exc).__name__,
            message=str(exc),
        )
