from __future__ import annotations

from datetime import date, datetime, timezone
from time import sleep

from fastapi.testclient import TestClient

from agent_org_network.agent_card import AgentCard
from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.central_authority import (
    AuthorityPolicySnapshot,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    WorkerBinding,
    canonical_policy_digest,
)
from agent_org_network.worker_authorization import (
    DeliveryBinding,
    StrictSnapshotWorkerBindingSource,
    WorkerAuthorization,
    WorkerConnectionPrincipal,
)
from agent_org_network.registry import Registry
from agent_org_network.knowledge_store import InMemoryKnowledgeStore
from agent_org_network.knowledge_sync import KnowledgeBundleContent, KnowledgeDoc, SyncKnowledge
from agent_org_network.knowledge_index import KnowledgeIndex
from agent_org_network.runtime import Answer
from agent_org_network.dispatch import Delivered
from agent_org_network.transport import (
    AnswerFrame,
    PublishIndex,
    PushWork,
    RegisterWorker,
    SubmitAnswer,
    WebSocketDispatcher,
    Welcome,
)
from agent_org_network.server import create_worker_app
from agent_org_network.two_stage_router import InMemoryPublishedIndexStore


def _snapshot(*, generation: int = 3) -> AuthorityPolicySnapshot:
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "v1",
        "content_sha256": "pending",
        "subject_roles": [{"org_id": "acme", "subject_id": "alice", "roles": ["owner"]}],
        "role_permissions": [
            {
                "role": "owner",
                "actions": [
                    "worker.connect",
                    "worker.submit",
                    "worker.publish_index",
                    "worker.sync_knowledge",
                ],
            }
        ],
        "route_rules": [],
        "worker_bindings": [
            {
                "org_id": "acme",
                "credential_id": "cred-1",
                "owner_subject_id": "alice",
                "connection_role": "primary",
                "generation": generation,
            }
        ],
    }
    document["content_sha256"] = canonical_policy_digest(document)
    return AuthorityPolicySnapshot(
        schema_version=1,
        org_id="acme",
        policy_version="v1",
        content_sha256=document["content_sha256"],
        subject_roles=(SubjectRoleBinding(org_id="acme", subject_id="alice", roles=("owner",)),),
        role_permissions=(
            RolePermission(
                role="owner",
                actions=(
                    "worker.connect",
                    "worker.submit",
                    "worker.publish_index",
                    "worker.sync_knowledge",
                ),
            ),
        ),
        route_rules=(),
        worker_bindings=(
            WorkerBinding(
                org_id="acme",
                credential_id="cred-1",
                owner_subject_id="alice",
                connection_role="primary",
                generation=generation,
            ),
        ),
    )


def _principal(*, generation: int = 3, epoch: str = "connection-1") -> WorkerConnectionPrincipal:
    return WorkerConnectionPrincipal(
        org_id="acme",
        owner_id="alice",
        credential_id="cred-1",
        credential_generation=generation,
        role="primary",
        connection_epoch=epoch,
    )


def _boundary(snapshot: AuthorityPolicySnapshot | None = None) -> WorkerAuthorization:
    value = snapshot or _snapshot()
    return WorkerAuthorization(
        configured_org_id="acme",
        central_authorizer=SnapshotCentralAuthorizer(value),
        binding_source=StrictSnapshotWorkerBindingSource(value),
    )


def test_exact_snapshot_binding_admits_connection_and_current_card_delivery() -> None:
    boundary = _boundary()
    principal = _principal()

    assert boundary.authorize_connection(principal) == "allowed"
    assert (
        boundary.authorize_delivery(
            principal, "worker.submit", agent_card_id="support", current_owner_id="alice"
        )
        == "allowed"
    )


def test_generation_owner_or_role_drift_denies_before_delivery() -> None:
    boundary = _boundary()
    assert boundary.authorize_connection(_principal(generation=4)) == "denied"
    assert (
        boundary.authorize_delivery(
            _principal(), "worker.sync_knowledge", agent_card_id="support", current_owner_id="bob"
        )
        == "denied"
    )


def test_missing_full_central_dependencies_are_unavailable_not_legacy_fallback() -> None:
    boundary = WorkerAuthorization(
        configured_org_id="acme", central_authorizer=None, binding_source=None
    )
    assert boundary.authorize_connection(_principal()) == "unavailable"


def test_delivery_binding_requires_exact_connection_epoch_ticket_and_current_owner() -> None:
    boundary = _boundary()
    binding = DeliveryBinding(
        ticket_id="ticket-1",
        agent_card_id="support",
        owner_id="alice",
        connection=_principal(),
        attempt=1,
    )
    assert boundary.verify_delivery_binding(
        binding,
        _principal(),
        ticket_id="ticket-1",
        agent_card_id="support",
        current_owner_id="alice",
    )
    assert not boundary.verify_delivery_binding(
        binding,
        _principal(epoch="connection-2"),
        ticket_id="ticket-1",
        agent_card_id="support",
        current_owner_id="alice",
    )
    assert not boundary.verify_delivery_binding(
        binding,
        _principal(),
        ticket_id="ticket-2",
        agent_card_id="support",
        current_owner_id="alice",
    )


def test_bad_snapshot_provider_is_unavailable() -> None:
    def broken() -> AuthorityPolicySnapshot:
        raise RuntimeError("policy unavailable")

    boundary = WorkerAuthorization(
        configured_org_id="acme",
        central_authorizer=SnapshotCentralAuthorizer(_snapshot()),
        binding_source=StrictSnapshotWorkerBindingSource(broken),
    )
    assert boundary.authorize_connection(_principal()) == "unavailable"


class _Recorder:
    def __init__(self) -> None:
        self.frames: list[object] = []

    def __call__(self, frame: object) -> None:
        self.frames.append(frame)


def _card(owner: str = "alice") -> AgentCard:
    return AgentCard(
        agent_id="support",
        owner=owner,
        team="support",
        summary="s",
        domains=["support"],
        last_reviewed_at=date(2026, 7, 1),
    )


def test_central_dispatcher_requires_full_seam_and_fences_stale_epoch_or_owner_drift() -> None:
    registry = Registry()
    registry.register(_card())
    dispatcher = WebSocketDispatcher(
        registry=registry,
        worker_authorization=_boundary(),
        worker_principal_resolver=lambda _frame: _principal(),
    )
    recorder = _Recorder()
    assert isinstance(dispatcher.register(RegisterWorker(owner_id="alice"), recorder), Welcome)
    ticket = dispatcher.dispatch("question", _card())
    assert any(isinstance(frame, PushWork) for frame in recorder.frames)

    # 다른 epoch 회신은 push 때 기록한 exact delivery binding에 맞지 않아 write 0.
    dispatcher.submit(ticket.ticket_id, Answer(text="stale"), _principal(epoch="other"))
    assert not isinstance(dispatcher.poll(ticket), Delivered)

    # owner가 바뀌면 기존 세션의 같은 epoch 회신도 현재 card 재인가에서 막힌다.
    registry.replace_card(_card(owner="bob"))
    dispatcher.submit(ticket.ticket_id, Answer(text="owner drift"), _principal())
    assert not isinstance(dispatcher.poll(ticket), Delivered)


def test_partial_central_dispatcher_composition_rejects_register_without_legacy_token_fallback() -> (
    None
):
    dispatcher = WebSocketDispatcher(worker_authorization=_boundary())
    reply = dispatcher.register(RegisterWorker(owner_id="alice"), _Recorder())
    assert reply.type == "auth_error"


def _sync() -> SyncKnowledge:
    return SyncKnowledge(
        content=KnowledgeBundleContent(
            agent_id="support",
            documents=(KnowledgeDoc(path="support/a.md", body="safe"),),
            version="v1",
            synced_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
    )


def test_central_sync_rechecks_current_card_and_records_only_with_injected_audit_sink() -> None:
    registry = Registry()
    registry.register(_card())
    store = InMemoryKnowledgeStore()
    audit = InMemoryAuditLog()
    dispatcher = WebSocketDispatcher(
        registry=registry,
        knowledge_store=store,
        worker_authorization=_boundary(),
        worker_principal_resolver=lambda _frame: _principal(),
        worker_audit_log=audit,
    )
    dispatcher.register(RegisterWorker(owner_id="alice"), _Recorder())
    ack = dispatcher.accept_knowledge_sync_frame("alice", _sync(), _principal())
    assert ack is not None and ack.accepted is True
    assert store.get("support") is not None
    assert audit.records()[-1]["action"]["kind"] == "worker.sync_knowledge"
    assert audit.records()[-1]["action"]["outcome"] == "succeeded"

    registry.replace_card(_card(owner="bob"))
    denied = dispatcher.accept_knowledge_sync_frame("alice", _sync(), _principal())
    assert denied is not None and denied.accepted is False
    assert audit.records()[-1]["action"]["outcome"] == "rejected"


def test_central_sync_without_audit_sink_cannot_write() -> None:
    registry = Registry()
    registry.register(_card())
    store = InMemoryKnowledgeStore()
    dispatcher = WebSocketDispatcher(
        registry=registry,
        knowledge_store=store,
        worker_authorization=_boundary(),
        worker_principal_resolver=lambda _frame: _principal(),
    )
    dispatcher.register(RegisterWorker(owner_id="alice"), _Recorder())
    ack = dispatcher.accept_knowledge_sync_frame("alice", _sync(), _principal())
    assert ack is not None and ack.accepted is False
    assert store.get("support") is None


def test_central_publish_requires_current_session_authorization_and_audits_outcome() -> None:
    registry = Registry()
    registry.register(_card())
    audit = InMemoryAuditLog()
    indexes = InMemoryPublishedIndexStore()
    dispatcher = WebSocketDispatcher(
        registry=registry,
        published_index_store=indexes,
        worker_authorization=_boundary(),
        worker_principal_resolver=lambda _frame: _principal(),
        worker_audit_log=audit,
    )
    dispatcher.register(RegisterWorker(owner_id="alice"), _Recorder())
    frame = PublishIndex(
        index=KnowledgeIndex(
            agent_id="support",
            version="v1",
            generated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            concepts=(),
        )
    )
    assert dispatcher.accept_index("alice", frame, _principal()) is True
    assert audit.records()[-1]["action"]["outcome"] == "succeeded"

    registry.replace_card(_card(owner="bob"))
    assert dispatcher.accept_index("alice", frame, _principal()) is False
    assert audit.records()[-1]["action"]["outcome"] == "rejected"


def test_superseded_websocket_epoch_cannot_mutate_or_disconnect_current_session() -> None:
    """실 WS A→B 교체에서 A의 모든 write와 finally disconnect가 전이 0이어야 한다."""
    registry = Registry()
    registry.register(_card())
    indexes = InMemoryPublishedIndexStore()
    knowledge = InMemoryKnowledgeStore()
    audit = InMemoryAuditLog()
    dispatcher = WebSocketDispatcher(
        registry=registry,
        published_index_store=indexes,
        knowledge_store=knowledge,
        worker_authorization=_boundary(),
        worker_principal_resolver=lambda frame: _principal(epoch=frame.token or "missing"),
        worker_audit_log=audit,
    )
    client = TestClient(create_worker_app(dispatcher))
    publish = PublishIndex(
        index=KnowledgeIndex(
            agent_id="support",
            version="v1",
            generated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            concepts=(),
        )
    )

    first = client.websocket_connect("/worker")
    first.__enter__()
    first_closed = False
    try:
        first.send_json(RegisterWorker(owner_id="alice", token="epoch-1").model_dump())
        assert first.receive_json()["type"] == "welcome"
        stale_ticket = dispatcher.dispatch("stale", _card())
        assert first.receive_json()["type"] == "push_work"

        with client.websocket_connect("/worker") as second:
            second.send_json(RegisterWorker(owner_id="alice", token="epoch-2").model_dump())
            assert second.receive_json()["type"] == "welcome"

            # A는 B에 의해 supersede된 뒤에도 프레임을 보낼 수 있지만 write는 전부 0이다.
            first.send_json(
                SubmitAnswer(
                    ticket_id=stale_ticket.ticket_id,
                    answer=AnswerFrame(text="stale", sources=(), mode="full"),
                ).model_dump()
            )
            first.send_json(publish.model_dump(mode="json"))
            first.send_json(_sync().model_dump(mode="json"))
            stale_sync = first.receive_json()
            assert stale_sync["type"] == "knowledge_sync_ack"
            assert stale_sync["accepted"] is False
            assert not isinstance(dispatcher.poll(stale_ticket), Delivered)
            assert indexes.get("support") is None
            assert knowledge.get("support") is None

            # A의 실제 소켓 종료가 B mapping이나 B의 in-flight 상태를 건드리면 안 된다.
            first.__exit__(None, None, None)
            first_closed = True
            assert dispatcher.connection_principal("alice", "primary") == _principal(
                epoch="epoch-2"
            )

            second.send_json(publish.model_dump(mode="json"))
            second.send_json(_sync().model_dump(mode="json"))
            accepted_sync = second.receive_json()
            assert accepted_sync["type"] == "knowledge_sync_ack"
            assert accepted_sync["accepted"] is True
            assert indexes.get("support") is not None
            assert knowledge.get("support") is not None

            current_ticket = dispatcher.dispatch("current", _card())
            pushed = second.receive_json()
            assert pushed["type"] == "push_work"
            assert pushed["ticket"]["ticket_id"] == current_ticket.ticket_id
            second.send_json(
                SubmitAnswer(
                    ticket_id=current_ticket.ticket_id,
                    answer=AnswerFrame(text="current", sources=(), mode="full"),
                ).model_dump()
            )
            # TestClient WS 수신 루프가 다른 task이므로 submit 전달까지 짧게 양보한다.
            outcome = dispatcher.poll(current_ticket)
            for _ in range(20):
                if isinstance(outcome, Delivered):
                    break
                sleep(0.01)
                outcome = dispatcher.poll(current_ticket)
            assert isinstance(outcome, Delivered)
    finally:
        if not first_closed:
            first.__exit__(None, None, None)
