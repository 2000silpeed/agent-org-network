"""S5.8 durable dispatch WebSocket production composition root."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import replace
import hashlib

from fastapi import FastAPI

from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.durable_websocket_transport import DurableWebSocketDispatchChannel
from agent_org_network.durable_dispatch_delivery import DispatchFrame
from agent_org_network.durable_dispatch_delivery import (
    DispatchOwnerDirectory,
    DurableDispatchRunner,
)
from agent_org_network.server import create_durable_worker_app
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_answer_ingestion_uow import (
    DurableAnswerIngestionUnitOfWork,
)
from agent_org_network.sqlite_durable_dispatch_delivery import (
    migrate_sqlite_durable_dispatch_delivery_schema,
)
from agent_org_network.sqlite_durable_dispatch_lease_uow import (
    DurableDispatchLeaseUnitOfWork,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
)
from agent_org_network.transport import RegisterWorker
from agent_org_network.worker_authorization import WorkerAuthorization, WorkerConnectionPrincipal
from agent_org_network.sqlite_durable_worker_credential import (
    SqliteDurableWorkerCredentialVerifier,
)
from agent_org_network.question_request import AwaitingAnswer


class _SqliteAnswerTargetReader:
    def __init__(self, completion: SqliteQuestionCompletionUnitOfWork) -> None:
        self._tx = completion.durable_transaction()

    def read(self, ticket_id: str) -> tuple[DispatchFrame, str] | None:
        with self._tx.scope():
            with self._tx.read_scope():
                ticket = self._tx.execute(
                    "SELECT * FROM durable_linked_work_tickets WHERE ticket_id=? "
                    "AND status='pending'",
                    (ticket_id,),
                ).fetchone()
                if ticket is None:
                    return None
                request = self._tx.select_question_request(ticket["request_id"])
        if (
            request is None
            or not isinstance(request.state, AwaitingAnswer)
            or request.state.ticket_id != ticket_id
            or request.revision != ticket["awaiting_revision"] + 1
        ):
            return None
        return DispatchFrame(
            ticket_id=ticket_id,
            request_id=ticket["request_id"],
            org_id=ticket["org_id"],
            attempt=ticket["attempt"],
            lease_epoch=0,
            agent_id=request.state.route.agent_id,
            owner_id="",  # current Directory owner로 아래 factory adapter가 봉인한다.
            question=request.question,
            context_snapshot=request.context_snapshot,
            session_id=request.session_id,
            intent=request.state.route.intent,
            expected_request_revision=request.revision,
            enqueued_at=datetime.fromisoformat(ticket["created_at"]),
        ), ticket["owner_subject_id"]


class DurableDispatchWebSocketCompositionError(ValueError):
    pass


def create_durable_dispatch_websocket_app(
    *,
    db_path: str | Path,
    completion: SqliteQuestionCompletionUnitOfWork,
    credential_verifier: SqliteDurableWorkerCredentialVerifier,
    clock: Callable[[], datetime],
    receipt_id_factory: Callable[[], str],
    delivery_attempt_id_factory: Callable[[], str],
    policy_version: str,
    directory: DispatchOwnerDirectory,
    worker_authorization: WorkerAuthorization,
    holder_id: str,
    lease_ttl: timedelta,
) -> FastAPI:
    """실 SQLite ingestion과 실 WS channel만 조립하는 production entry point.

    모든 policy/Approval/responsibility 의존성은 caller가 만든 ``completion``에
    명시돼 있어야 한다. 이 entry point에는 demo/runtime/test-double fallback이 없다.
    """
    path = Path(db_path)
    if (
        type(completion) is not SqliteQuestionCompletionUnitOfWork
        or type(credential_verifier) is not SqliteDurableWorkerCredentialVerifier
        or not callable(clock)
        or not callable(receipt_id_factory)
        or not callable(delivery_attempt_id_factory)
        or type(policy_version) is not str
        or not policy_version.strip()
        or str(path) in {"", ":memory:"}
        or not holder_id.strip()
        or type(lease_ttl) is not timedelta
        or lease_ttl.total_seconds() <= 0
        or not callable(getattr(directory, "resolve_owner", None))
        or type(worker_authorization) is not WorkerAuthorization
    ):
        raise DurableDispatchWebSocketCompositionError(
            "durable dispatch WS production dependency가 완전하지 않습니다."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    migrate_sqlite_durable_dispatch_delivery_schema(path)
    ingestion = DurableAnswerIngestionUnitOfWork(
        completion=completion,
        clock=clock,
        receipt_id_factory=receipt_id_factory,
    )
    def current_owner(org_id: str, agent_card_id: str) -> str | None:
        owner = directory.resolve_owner(org_id=org_id, agent_id=agent_card_id)
        return owner.owner_id if owner is not None else None

    reader = _SqliteAnswerTargetReader(completion)

    class _Reader:
        def read(self, ticket_id: str) -> DispatchFrame | None:
            target = reader.read(ticket_id)
            if target is None:
                return None
            frame, owner_subject_ref = target
            owner = current_owner(frame.org_id, frame.agent_id)
            if (
                owner is None
                or owner_subject_ref
                != f"subject:{hashlib.sha256(owner.encode('utf-8')).hexdigest()}"
            ):
                return None
            return replace(frame, owner_id=owner)

    channel = DurableWebSocketDispatchChannel(
        ingestion=ingestion,
        policy_version=policy_version,
        authorization=worker_authorization,
        answer_target_reader=_Reader(),
        current_owner=current_owner,
        principal_is_current=lambda principal: credential_verifier.is_current(
            principal, now=clock()
        ),
    )
    lease_uow = DurableDispatchLeaseUnitOfWork(
        completion=completion,
        holder_id=holder_id,
        clock=clock,
        lease_ttl=lease_ttl,
    )
    runner = DurableDispatchRunner(
        completion=completion,
        lease_uow=lease_uow,
        channel=channel,
        directory=directory,
        clock=clock,
        attempt_id_factory=delivery_attempt_id_factory,
    )
    def authenticate(register: RegisterWorker) -> WorkerConnectionPrincipal | None:
        principal = credential_verifier.authenticate(register, now=clock())
        if (
            principal is None
            or worker_authorization.authorize_connection(principal) != "allowed"
        ):
            return None
        return principal

    app = create_durable_worker_app(channel, authenticate)
    app.state.durable_dispatch_channel = channel
    app.state.durable_dispatch_runner = runner
    return app


__all__ = [
    "DurableDispatchWebSocketCompositionError",
    "create_durable_dispatch_websocket_app",
]
